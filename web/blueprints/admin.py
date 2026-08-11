#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IVD平台 - ADMIN模块"""

from flask import Blueprint, request, jsonify, session, redirect, url_for, make_response, render_template_string
from psycopg2.extras import RealDictCursor, execute_values
import secrets
import os
import logging
from werkzeug.security import generate_password_hash, check_password_hash
import json
import time
import shared
from shared import api_login_required, api_super_admin_required, login_required
from services.cache import api_cache
from services.rules import get_rules, clear_rules_cache
from services.file_utils import validate_input
from services.data_init import get_table_name

logger = logging.getLogger(__name__)

admin_bp = Blueprint('admin', __name__)

_MAX_ATTEMPTS = 5
_LOCKOUT_SECONDS = 300


def _check_login_lockout(username):
    try:
        r = shared.get_redis()
        lockout_key = f'login_lockout:{username}'
        remaining = r.ttl(lockout_key)
        if remaining and remaining > 0:
            return True, remaining

        r.delete(lockout_key)
    except Exception:
        pass
    return False, 0


def _record_login_failure(username):
    try:
        r = shared.get_redis()
        attempts_key = f'login_attempts:{username}'
        lockout_key = f'login_lockout:{username}'
        pipe = r.pipeline()
        pipe.incr(attempts_key)
        pipe.expire(attempts_key, _LOCKOUT_SECONDS)
        results = pipe.execute()
        count = results[0]
        if count >= _MAX_ATTEMPTS:
            pipe2 = r.pipeline()
            pipe2.setex(lockout_key, _LOCKOUT_SECONDS, '1')
            pipe2.delete(attempts_key)
            pipe2.execute()
            logger.warning(f"登录锁定: 用户 {username} 连续失败{_MAX_ATTEMPTS}次，锁定{_LOCKOUT_SECONDS}秒")
    except Exception:
        pass


def _record_login_success(username):
    try:
        r = shared.get_redis()
        pipe = r.pipeline()
        pipe.delete(f'login_attempts:{username}')
        pipe.delete(f'login_lockout:{username}')
        pipe.execute()
    except Exception:
        pass


def _get_login_attempts(username):
    try:
        r = shared.get_redis()
        val = r.get(f'login_attempts:{username}')
        return int(val) if val else 0
    except Exception:
        return 0

@admin_bp.route('/api/series', methods=['GET'])
def get_series():
    try:
        r = shared.get_redis()
        cached = r.get('cache:series')
        if cached:
            return jsonify(json.loads(cached))
    except Exception:
        pass
    with shared.db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('SELECT id, name FROM series ORDER BY name')
        rows = [dict(row) for row in cur.fetchall()]
    try:
        r = shared.get_redis()
        r.setex('cache:series', 3600, json.dumps(rows, ensure_ascii=False))
    except Exception:
        pass
    return jsonify(rows)

@admin_bp.route('/api/models', methods=['GET'])
def get_models():
    series_name = request.args.get('series', '')
    if not series_name:
        return jsonify([])
    cache_key = f'cache:models:{series_name.upper()}'
    try:
        r = shared.get_redis()
        cached = r.get(cache_key)
        if cached:
            return jsonify(json.loads(cached))
    except Exception:
        pass
    with shared.db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('''
            SELECT m.id, m.name
            FROM models m
            JOIN series s ON m.series_id = s.id
            WHERE UPPER(s.name) = UPPER(%s)
            ORDER BY m.name
        ''', (series_name,))
        rows = [dict(row) for row in cur.fetchall()]
    try:
        r = shared.get_redis()
        r.setex(cache_key, 3600, json.dumps(rows, ensure_ascii=False))
    except Exception:
        pass
    return jsonify(rows)

@admin_bp.route('/api/rules', methods=['GET'])
def get_rules_api():
    series = request.args.get('series', '')
    model = request.args.get('model', '')
    if not series or not model:
        return jsonify([])
    rules = get_rules(series, model)
    return jsonify(rules)



@admin_bp.route('/api/rules', methods=['POST'])
@api_super_admin_required
def add_rule_api():
    data = request.json
    series = data.get('series', '').strip()
    model = data.get('model', '').strip()
    keywords = data.get('keywords', '').strip()
    advice = data.get('advice', '').strip()
    if not all([series, model, keywords, advice]):
        return jsonify({'error': '请填写完整信息'}), 400
    if not validate_input(keywords) or not validate_input(advice):
        return jsonify({'error': '输入包含非法字符'}), 400
    with shared.db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('SELECT id FROM series WHERE UPPER(name) = UPPER(%s)', (series,))
        series_row = cur.fetchone()
        if not series_row:
            return jsonify({'error': '系列不存在'}), 400
        cur.execute('SELECT id FROM models WHERE series_id = %s AND name = %s', (series_row['id'], model))
        model_row = cur.fetchone()
        if not model_row:
            return jsonify({'error': '型号不存在'}), 400
        cur.execute('INSERT INTO rules (model_id, keywords, advice) VALUES (%s, %s, %s) RETURNING id', (model_row['id'], keywords, advice))
        rule_id = cur.fetchone()['id']
        kw_values = [(rule_id, kw.strip()) for kw in keywords.split(',') if kw.strip()]
        if kw_values:
            execute_values(cur, 'INSERT INTO rule_keywords (rule_id, keyword) VALUES %s', kw_values)
        conn.commit()
        clear_rules_cache(series, model)
        logger.info(f"添加规则 ID:{rule_id} - {series}/{model}")
        shared.audit_log('add_rule', target_type='rule', target_id=rule_id, detail=f'添加规则#{rule_id} {series}/{model} keywords={keywords}')
        return jsonify({'success': True, 'id': rule_id})



@admin_bp.route('/api/rules/<int:rule_id>', methods=['PUT'])
@api_super_admin_required
def update_rule_api(rule_id):
    data = request.json
    keywords = data.get('keywords', '').strip()
    advice = data.get('advice', '').strip()
    if not all([keywords, advice]):
        return jsonify({'error': '请填写完整信息'}), 400
    with shared.db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('''SELECT s.name AS series, m.name AS model FROM rules r
            JOIN models m ON r.model_id = m.id JOIN series s ON m.series_id = s.id WHERE r.id = %s''', (rule_id,))
        info = cur.fetchone()
        cur.execute('UPDATE rules SET keywords = %s, advice = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s', (keywords, advice, rule_id))
        if cur.rowcount == 0:
            return jsonify({'error': '规则不存在'}), 404
        cur.execute('DELETE FROM rule_keywords WHERE rule_id = %s', (rule_id,))
        kw_values = [(rule_id, kw.strip()) for kw in keywords.split(',') if kw.strip()]
        if kw_values:
            execute_values(cur, 'INSERT INTO rule_keywords (rule_id, keyword) VALUES %s', kw_values)
        conn.commit()
        if info:
            clear_rules_cache(info['series'], info['model'])
        else:
            clear_rules_cache()
        logger.info(f"更新规则 ID:{rule_id}")
        shared.audit_log('update_rule', target_type='rule', target_id=rule_id, detail=f'更新规则#{rule_id}')
        return jsonify({'success': True})



@admin_bp.route('/api/rules/<int:rule_id>', methods=['DELETE'])
@api_super_admin_required
def delete_rule_api(rule_id):
    with shared.db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('''SELECT s.name AS series, m.name AS model FROM rules r
            JOIN models m ON r.model_id = m.id JOIN series s ON m.series_id = s.id WHERE r.id = %s''', (rule_id,))
        info = cur.fetchone()
        cur.execute('DELETE FROM rules WHERE id = %s', (rule_id,))
        if cur.rowcount == 0:
            return jsonify({'error': '规则不存在'}), 404
        conn.commit()
        if info:
            clear_rules_cache(info['series'], info['model'])
        else:
            clear_rules_cache()
        logger.info(f"删除规则 ID:{rule_id}")
        shared.audit_log('delete_rule', target_type='rule', target_id=rule_id, detail=f'删除规则#{rule_id} {info}')
        return jsonify({'success': True})



@admin_bp.route('/api/motor_status', methods=['GET'])
@api_cache(ttl=300, key_prefix='motor')
def get_motor_status():
    model = request.args.get('model', '')
    limit = request.args.get('limit', 100, type=int)
    offset = request.args.get('offset', 0, type=int)
    if not model:
        return jsonify({'total': 0, 'data': [], 'limit': limit, 'offset': offset, 'model': ''})
    table_name = get_table_name(model)
    with shared.db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT tablename FROM pg_tables WHERE tablename = %s", (table_name,))
        if not cur.fetchone():
            return jsonify({'total': 0, 'data': [], 'limit': limit, 'offset': offset, 'model': model})
        cur.execute(f'SELECT COUNT(*) AS total FROM {table_name}')
        total = cur.fetchone()['total']
        cur.execute(f'''
            SELECT id, board_card, motor_code, status_code, motor_name,
                   action_type, target_value, sensor, description, full_description
            FROM {table_name}
            ORDER BY board_card, motor_code, status_code
            LIMIT %s OFFSET %s
        ''', (limit, offset))
        rows = cur.fetchall()
        data = [dict(row) for row in rows]
        return jsonify({
            'total': total,
            'data': data,
            'limit': limit,
            'offset': offset,
            'model': model
        })



@admin_bp.route('/api/motor_status/clear', methods=['DELETE'])
@api_super_admin_required
def clear_motor_status():
    model = request.args.get('model', '')
    if not model:
        return jsonify({'error': '请指定型号'}), 400
    table_name = get_table_name(model)
    with shared.db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT tablename FROM pg_tables WHERE tablename = %s", (table_name,))
        if not cur.fetchone():
            return jsonify({'error': f'型号 {model} 不存在'}), 404
        cur.execute(f'DELETE FROM {table_name}')
        conn.commit()
        shared.audit_log('clear_motor_status', target_type='motor_status', detail=f'清空 {model} 电机状态数据')
        return jsonify({'success': True, 'message': f'已清空 {model} 的所有数据'})




@admin_bp.route('/api/verify_super_admin', methods=['POST'])
def verify_super_admin():
    data = request.json
    password = data.get('password', '') if data else ''
    locked, remaining = _check_login_lockout('__super_admin__')
    if locked:
        return jsonify({'error': f'验证已锁定，请{remaining}秒后重试'}), 429
    try:
        with shared.db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT password_hash FROM users WHERE permission = 1 LIMIT 1")
            row = cur.fetchone()
            if row and check_password_hash(row[0], password):
                _record_login_success('__super_admin__')
                session['super_admin_logged_in'] = True
                return jsonify({'success': True})
        _record_login_failure('__super_admin__')
        attempts_left = _MAX_ATTEMPTS - _get_login_attempts('__super_admin__')
        if attempts_left <= 0:
            return jsonify({'error': f'连续失败{_MAX_ATTEMPTS}次，已锁定{_LOCKOUT_SECONDS}秒'}), 429
        return jsonify({'error': f'管理员密码错误，还可尝试{attempts_left}次'}), 403
    except Exception as e:
        return jsonify({'error': '服务器内部错误'}), 500



@admin_bp.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        
        if not username or not password:
            return jsonify({'success': False, 'error': 'empty', 'message': '用户名和密码不能为空'})
        
        locked, remaining = _check_login_lockout(username)
        if locked:
            return jsonify({'success': False, 'error': 'locked', 'message': f'登录已锁定，请{remaining}秒后重试'})
        
        with shared.db_connection() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT id, password_hash, permission, is_active FROM users WHERE username = %s", (username,))
            user = cur.fetchone()
            
            if not user:
                _record_login_failure(username)
                attempts_left = _MAX_ATTEMPTS - _get_login_attempts(username)
                return jsonify({'success': False, 'error': 'no_user', 'message': f'账户不存在，请先注册'})
            
            if not user.get('is_active', True):
                return jsonify({'success': False, 'error': 'locked', 'message': '该账户已被禁用，请联系管理员'})
            
            if not check_password_hash(user['password_hash'], password):
                _record_login_failure(username)
                attempts_left = _MAX_ATTEMPTS - _get_login_attempts(username)
                if attempts_left <= 0:
                    return jsonify({'success': False, 'error': 'locked', 'message': f'连续失败{_MAX_ATTEMPTS}次，已锁定{_LOCKOUT_SECONDS}秒'})
                return jsonify({'success': False, 'error': 'wrong_password', 'message': f'密码错误，还可尝试{attempts_left}次'})
            
            _record_login_success(username)
            session.clear()
            session['user_id'] = user['id']
            session['username'] = username
            session['admin_logged_in'] = True
            if user['permission'] == 1:
                session['super_admin_logged_in'] = True
            session.permanent = True
            cur.execute('UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE id = %s', (user['id'],))
            conn.commit()
            shared.audit_log('login', target_type='user', target_id=user['id'], detail=f'用户 {username} 登录成功')
            return jsonify({'success': True, 'redirect': '/'})
    
    login_html = shared.get_template('LOGIN_HTML')
    if not login_html:
        with open(os.path.join(os.path.dirname(__file__), 'templates', 'login.html'), 'r', encoding='utf-8') as tf:
            login_html = tf.read()
        shared.set_template('LOGIN_HTML', login_html)
    return render_template_string(login_html)



@admin_bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        
        if not username or not password:
            return '<!DOCTYPE html><html><head><meta charset="UTF-8"><title>注册失败</title></head><body style="font-family:system-ui;text-align:center;padding:50px;"><h3>❌ 用户名和密码不能为空</h3><a href="/register" style="color:#1e6f9f;">重试</a></body></html>'
        
        if password != confirm_password:
            return '<!DOCTYPE html><html><head><meta charset="UTF-8"><title>注册失败</title></head><body style="font-family:system-ui;text-align:center;padding:50px;"><h3>❌ 两次输入的密码不一致</h3><a href="/register" style="color:#1e6f9f;">重试</a></body></html>'
        
        if len(password) < 6:
            return '<!DOCTYPE html><html><head><meta charset="UTF-8"><title>注册失败</title></head><body style="font-family:system-ui;text-align:center;padding:50px;"><h3>❌ 密码长度至少6位</h3><a href="/register" style="color:#1e6f9f;">重试</a></body></html>'
        
        with shared.db_connection() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT id FROM users WHERE username = %s", (username,))
            if cur.fetchone():
                return '<!DOCTYPE html><html><head><meta charset="UTF-8"><title>注册失败</title></head><body style="font-family:system-ui;text-align:center;padding:50px;"><h3>❌ 用户名已存在</h3><a href="/register" style="color:#1e6f9f;">重试</a></body></html>'
            
            password_hash = generate_password_hash(password)
            cur.execute("INSERT INTO users (username, password_hash, permission) VALUES (%s, %s, %s)", (username, password_hash, 0))
            conn.commit()
            shared.audit_log('create_user', target_type='user', detail=f'创建用户 {username}')
        
        return '<!DOCTYPE html><html><head><meta charset="UTF-8"><title>注册成功</title></head><body style="font-family:system-ui;text-align:center;padding:50px;"><h3>✅ 注册成功！</h3><a href="/admin/login" style="color:#667eea;">立即登录</a></body></html>'
    
    register_html = shared.get_template('REGISTER_HTML')
    if not register_html:
        with open(os.path.join(os.path.dirname(__file__), 'templates', 'register.html'), 'r', encoding='utf-8') as tf:
            register_html = tf.read()
        shared.set_template('REGISTER_HTML', register_html)
    return render_template_string(register_html)



@admin_bp.route('/admin/rules')
@login_required
def admin_rules():
    return render_template_string(shared.get_template('ADMIN_HTML'))



@admin_bp.route('/admin/logout')
@login_required
def admin_logout():
    session.clear()
    return redirect('/admin/login')

# ========== 用户管理 API ==========

@admin_bp.route('/api/users', methods=['GET'])
@api_super_admin_required
def list_users():
    page = max(1, request.args.get('page', 1, type=int))
    per_page = min(50, max(1, request.args.get('per_page', 20, type=int)))
    with shared.db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('SELECT COUNT(*) AS total FROM users')
        total = cur.fetchone()['total']
        cur.execute('SELECT id, username, permission, is_active, created_at, last_login_at FROM users ORDER BY id LIMIT %s OFFSET %s', (per_page, (page - 1) * per_page))
        rows = cur.fetchall()
        for row in rows:
            shared.format_row_timestamps(row)
            if row.get('last_login_at'):
                row['last_login_at'] = row['last_login_at'].strftime('%Y-%m-%d %H:%M:%S') if hasattr(row['last_login_at'], 'strftime') else str(row['last_login_at'])
        return jsonify({'results': [dict(r) for r in rows], 'total': total, 'page': page, 'per_page': per_page})


@admin_bp.route('/api/users/<int:user_id>/toggle-active', methods=['PUT'])
@api_super_admin_required
def toggle_user_active(user_id):
    current_user_id = session.get('user_id')
    if user_id == current_user_id:
        return jsonify({'error': '不能禁用自己'}), 400
    with shared.db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('SELECT username, is_active FROM users WHERE id = %s', (user_id,))
        user = cur.fetchone()
        if not user:
            return jsonify({'error': '用户不存在'}), 404
        new_status = not user['is_active']
        cur.execute('UPDATE users SET is_active = %s WHERE id = %s', (new_status, user_id))
        conn.commit()
        shared.audit_log('toggle_user', target_type='user', target_id=user_id, detail=f'{"启用" if new_status else "禁用"}用户 {user["username"]}')
        return jsonify({'success': True, 'is_active': new_status})


@admin_bp.route('/api/users/<int:user_id>/permission', methods=['PUT'])
@api_super_admin_required
def update_user_permission(user_id):
    data = request.json
    permission = data.get('permission')
    if permission not in (0, 1):
        return jsonify({'error': '权限值无效（0=普通用户, 1=超管）'}), 400
    with shared.db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('SELECT username FROM users WHERE id = %s', (user_id,))
        user = cur.fetchone()
        if not user:
            return jsonify({'error': '用户不存在'}), 404
        cur.execute('UPDATE users SET permission = %s WHERE id = %s', (permission, user_id))
        conn.commit()
        shared.audit_log('update_permission', target_type='user', target_id=user_id, detail=f'修改用户 {user["username"]} 权限为 {"超管" if permission == 1 else "普通用户"}')
        return jsonify({'success': True})


@admin_bp.route('/api/users/<int:user_id>', methods=['DELETE'])
@api_super_admin_required
def delete_user(user_id):
    current_user_id = session.get('user_id')
    if user_id == current_user_id:
        return jsonify({'error': '不能删除自己'}), 400
    with shared.db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('SELECT username FROM users WHERE id = %s', (user_id,))
        user = cur.fetchone()
        if not user:
            return jsonify({'error': '用户不存在'}), 404
        if user['username'] == 'admin':
            return jsonify({'error': '不能删除默认管理员'}), 400
        cur.execute('DELETE FROM users WHERE id = %s', (user_id,))
        conn.commit()
        shared.audit_log('delete_user', target_type='user', target_id=user_id, detail=f'删除用户 {user["username"]}')
        return jsonify({'success': True})


@admin_bp.route('/api/users/<int:user_id>/reset-password', methods=['PUT'])
@api_super_admin_required
def reset_user_password(user_id):
    data = request.json
    new_password = data.get('password', '')
    if len(new_password) < 6:
        return jsonify({'error': '密码长度至少6位'}), 400
    with shared.db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('SELECT username FROM users WHERE id = %s', (user_id,))
        user = cur.fetchone()
        if not user:
            return jsonify({'error': '用户不存在'}), 404
        cur.execute('UPDATE users SET password_hash = %s WHERE id = %s', (generate_password_hash(new_password), user_id))
        conn.commit()
        shared.audit_log('reset_password', target_type='user', target_id=user_id, detail=f'重置用户 {user["username"]} 密码')
        return jsonify({'success': True})

@admin_bp.route('/')
@login_required
def index():
    if not session.get('user_id'):
        return redirect('/admin/login')
    is_admin = session.get('super_admin_logged_in', False)
    return render_template_string(shared.get_template('MAIN_HTML'), is_admin=is_admin, username=session.get('username', ''))

# ========== 启动应用 ==========
if __name__ == '__main__':
    # 初始化数据库连接池
    init_db_pool()
    # 初始化表结构和默认数据（如果未初始化）
    init_db()
    cleanup_temp_zip_files()
    print("\n" + "="*60)
    print("🚀 IVD 智能故障分析平台 v3.0 (PostgreSQL + Redis)")
    print("="*60)
    logger.warning("管理员密码已通过环境变量配置")
    print(f"🌐 本机访问地址:")
    print(f"   HTTP:  http://localhost:8081")
    print(f"   HTTPS: https://localhost:8443")
    print(f"🔧 管理后台: http://localhost:8081/admin/rules")
    print(f"👥 同事访问地址（内网）:")
    print(f"   HTTP:  http://10.10.20.126:8081")
    print(f"   HTTPS: https://10.10.20.126:8443")
    print(f"   登录页: https://10.10.20.126:8443/admin/login")
    print("="*60 + "\n")
    from waitress import serve
    serve(app, host='0.0.0.0', port=8081, threads=4, channel_timeout=300, max_request_body_size=209715200)

