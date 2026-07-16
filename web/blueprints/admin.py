#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IVD平台 - ADMIN模块"""

from flask import Blueprint, request, jsonify, session, redirect, url_for, make_response, render_template_string
import sys
from psycopg2.extras import RealDictCursor
import secrets
import os
def _get_app():
    return sys.modules['app']
def _db():
    return _get_app().db_connection
def _config():
    return _get_app().Config
def _gph():
    return _get_app().generate_password_hash
def _cph():
    return _get_app().check_password_hash
def _rts():
    return _get_app().render_template_string

admin_bp = Blueprint('admin', __name__)

@admin_bp.route('/api/series', methods=['GET'])
def get_series():
    with _db()() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('SELECT id, name FROM series ORDER BY name')
        rows = cur.fetchall()
        return jsonify([dict(row) for row in rows])

@admin_bp.route('/api/models', methods=['GET'])
def get_models():
    series_name = request.args.get('series', '')
    if not series_name:
        return jsonify([])
    with _db()() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('''
            SELECT m.id, m.name
            FROM models m
            JOIN series s ON m.series_id = s.id
            WHERE UPPER(s.name) = UPPER(%s)
            ORDER BY m.name
        ''', (series_name,))
        rows = cur.fetchall()
        return jsonify([dict(row) for row in rows])

@admin_bp.route('/api/rules', methods=['GET'])
def get_rules_api():
    series = request.args.get('series', '')
    model = request.args.get('model', '')
    if not series or not model:
        return jsonify([])
    rules = get_rules(series, model)
    return jsonify(rules)



@admin_bp.route('/api/rules', methods=['POST'])
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
    with _db()() as conn:
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
        for kw in keywords.split(','):
            kw = kw.strip()
            if kw:
                cur.execute('INSERT INTO rule_keywords (rule_id, keyword) VALUES (%s, %s)', (rule_id, kw))
        conn.commit()
        clear_rules_cache()
        logger.info(f"添加规则 ID:{rule_id} - {series}/{model}")
        return jsonify({'success': True, 'id': rule_id})



@admin_bp.route('/api/rules/<int:rule_id>', methods=['PUT'])
def update_rule_api(rule_id):
    data = request.json
    keywords = data.get('keywords', '').strip()
    advice = data.get('advice', '').strip()
    if not all([keywords, advice]):
        return jsonify({'error': '请填写完整信息'}), 400
    with _db()() as conn:
        cur = conn.cursor()
        cur.execute('UPDATE rules SET keywords = %s, advice = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s', (keywords, advice, rule_id))
        if cur.rowcount == 0:
            return jsonify({'error': '规则不存在'}), 404
        cur.execute('DELETE FROM rule_keywords WHERE rule_id = %s', (rule_id,))
        for kw in keywords.split(','):
            kw = kw.strip()
            if kw:
                cur.execute('INSERT INTO rule_keywords (rule_id, keyword) VALUES (%s, %s)', (rule_id, kw))
        conn.commit()
        clear_rules_cache()
        logger.info(f"更新规则 ID:{rule_id}")
        return jsonify({'success': True})



@admin_bp.route('/api/rules/<int:rule_id>', methods=['DELETE'])
def delete_rule_api(rule_id):
    with _db()() as conn:
        cur = conn.cursor()
        cur.execute('DELETE FROM rules WHERE id = %s', (rule_id,))
        if cur.rowcount == 0:
            return jsonify({'error': '规则不存在'}), 404
        conn.commit()
        clear_rules_cache()
        logger.info(f"删除规则 ID:{rule_id}")
        return jsonify({'success': True})



@admin_bp.route('/api/motor_status', methods=['GET'])
def get_motor_status():
    model = request.args.get('model', '')
    limit = request.args.get('limit', 100, type=int)
    offset = request.args.get('offset', 0, type=int)
    if not model:
        return jsonify({'total': 0, 'data': [], 'limit': limit, 'offset': offset, 'model': ''})
    table_name = get_table_name(model)
    with _db()() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT tablename FROM pg_tables WHERE tablename = %s", (table_name,))
        if not cur.fetchone():
            return jsonify({'total': 0, 'data': [], 'limit': limit, 'offset': offset, 'model': model})
        cur.execute(f'SELECT COUNT(*) FROM {table_name}')
        total = cur.fetchone()['count']
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
def clear_motor_status():
    model = request.args.get('model', '')
    if not model:
        return jsonify({'error': '请指定型号'}), 400
    table_name = get_table_name(model)
    with _db()() as conn:
        cur = conn.cursor()
        cur.execute("SELECT tablename FROM pg_tables WHERE tablename = %s", (table_name,))
        if not cur.fetchone():
            return jsonify({'error': f'型号 {model} 不存在'}), 404
        cur.execute(f'DELETE FROM {table_name}')
        conn.commit()
        return jsonify({'success': True, 'message': f'已清空 {model} 的所有数据'})

REAGENT_COOLING_RULES = [
    {'keywords': ['温度异常', '温度超限', '温度失控'], 'advice': '🔧 排查：检查温度传感器、TEC制冷片供电', 'source': '制冷排查'},
    {'keywords': ['TEC', 'tec', '珀尔帖', '制冷片'], 'advice': '🔧 排查：检查TEC制冷片工作状态、驱动电流是否正常', 'source': '制冷排查'},
    {'keywords': ['制冷', '冷端', '散热'], 'advice': '🔧 排查：检查制冷模块散热器、风扇是否正常运转', 'source': '制冷排查'},
    {'keywords': ['试剂温度', '试剂制冷'], 'advice': '🔧 排查：检查试剂仓制冷模块、温度传感器校准', 'source': '制冷排查'},
    {'keywords': ['过热', '过温', '高温报警'], 'advice': '🔧 排查：检查制冷系统是否失效、散热通道是否堵塞', 'source': '制冷排查'},
    {'keywords': ['ADC', 'adc', 'NTC', 'ntc'], 'advice': '🔧 排查：检查温度采集ADC/NTC传感器读数是否异常', 'source': '制冷排查'},
    {'keywords': ['PID', 'pid'], 'advice': '🔧 排查：检查温度控制PID参数是否异常', 'source': '制冷排查'},
]



@admin_bp.route('/api/verify_super_admin', methods=['POST'])
def verify_super_admin():
    data = request.json
    password = data.get('password', '') if data else ''
    try:
        with _db()() as conn:
            cur = conn.cursor()
            cur.execute("SELECT password_hash FROM users WHERE permission = 1 LIMIT 1")
            row = cur.fetchone()
            if row and _cph()(row[0], password):
                session['super_admin_logged_in'] = True
                return jsonify({'success': True})
        return jsonify({'error': '管理员密码错误'}), 403
    except Exception as e:
        return jsonify({'error': str(e)}), 500



@admin_bp.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        
        if not username or not password:
            return jsonify({'success': False, 'error': 'empty', 'message': '用户名和密码不能为空'})
        
        with _db()() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT id, password_hash, permission FROM users WHERE username = %s", (username,))
            user = cur.fetchone()
            
            if not user:
                return jsonify({'success': False, 'error': 'no_user', 'message': '账户不存在，请先注册'})
            
            if not _cph()(user['password_hash'], password):
                return jsonify({'success': False, 'error': 'wrong_password', 'message': '密码错误，请重试'})
            
            session['user_id'] = user['id']
            session['username'] = username
            session['admin_logged_in'] = True
            if user['permission'] == 1:
                session['super_admin_logged_in'] = True
            session.permanent = True
            return jsonify({'success': True, 'redirect': '/'})
    
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Keylights - IVD智能故障分析平台</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body { 
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif;
                background: white;
                min-height: 100vh;
                display: flex;
                align-items: stretch;
            }
            .left-panel {
                flex: 1 1 55%;
                display: flex;
                flex-direction: column;
                justify-content: flex-end;
                align-items: center;
                padding: clamp(20px, 3vh, 40px) clamp(20px, 3vw, 60px);
                position: relative;
                overflow: hidden;
                background: white;
            }
            .left-panel img {
                width: 100%;
                height: auto;
                max-height: 85vh;
                object-fit: contain;
                display: block;
            }
            .right-panel {
                flex: 0 0 clamp(320px, 30vw, 480px);
                display: flex;
                flex-direction: column;
                justify-content: center;
                align-items: flex-start;
                padding: clamp(20px, 3vh, 40px) clamp(30px, 3vw, 50px);
                padding-right: clamp(40px, 5vw, 80px);
                background: white;
            }
            .vision-top {
                background: linear-gradient(135deg, rgba(173, 216, 230, 0.95) 0%, rgba(135, 206, 235, 0.95) 100%);
                padding: clamp(10px, 1.5vh, 20px) clamp(20px, 3vw, 50px);
                border-radius: 14px;
                box-shadow: 0 8px 32px rgba(0, 132, 168, 0.25);
                text-align: center;
                backdrop-filter: blur(10px);
                margin-bottom: clamp(16px, 2.5vh, 30px);
                width: 100%;
            }
            .vision-top h3 {
                font-size: clamp(0.95rem, 1.2vw, 1.2rem);
                font-weight: 600;
                color: #1a3a4a;
                margin-bottom: clamp(4px, 0.6vh, 10px);
                letter-spacing: 1px;
            }
            .vision-top p {
                font-size: clamp(0.72rem, 0.85vw, 0.95rem);
                line-height: 1.6;
                color: #2d3748;
                white-space: normal;
            }
            .right-panel {
                flex: 0 0 clamp(320px, 30vw, 480px);
                display: flex;
                flex-direction: column;
                justify-content: center;
                align-items: center;
                padding: clamp(20px, 3vh, 40px) clamp(20px, 2.5vw, 40px);
                background: white;
                box-shadow: -8px 0 40px rgba(0, 0, 0, 0.06);
            }
            .login-container {
                width: 100%;
                max-width: 360px;
            }
            .login-container::before {
                content: '';
                display: block;
                height: 4px;
                background: linear-gradient(90deg, #0084a8 0%, #00a8cc 100%);
                border-radius: 4px;
                margin-bottom: clamp(20px, 3vh, 36px);
            }
            .logo {
                text-align: center;
                margin-bottom: clamp(20px, 3vh, 32px);
            }
            .logo h1 {
                font-size: clamp(1.5rem, 2.2vw, 2.2rem);
                font-weight: 700;
                background: linear-gradient(135deg, #0084a8 0%, #00a8cc 100%);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                background-clip: text;
                margin-bottom: 6px;
            }
            .logo p {
                color: #64748b;
                font-size: clamp(0.75rem, 0.9vw, 0.95rem);
                font-weight: 500;
            }
            .form-group {
                margin-bottom: clamp(14px, 2vh, 22px);
            }
            .form-group label {
                display: block;
                color: #2d3748;
                font-weight: 600;
                margin-bottom: 6px;
                font-size: clamp(0.72rem, 0.85vw, 0.88rem);
            }
            input {
                width: 100%;
                padding: clamp(10px, 1.2vh, 14px) clamp(10px, 1vw, 16px);
                border: 2px solid #e2e8f0;
                border-radius: 10px;
                font-size: clamp(0.82rem, 0.95vw, 1rem);
                transition: all 0.3s;
                background: #f7fafc;
            }
            input:focus {
                outline: none;
                border-color: #0084a8;
                background: white;
                box-shadow: 0 0 0 3px rgba(0, 132, 168, 0.15);
            }
            button {
                width: 100%;
                padding: clamp(10px, 1.2vh, 14px);
                background: linear-gradient(135deg, #0084a8 0%, #00a8cc 100%);
                color: white;
                border: none;
                border-radius: 10px;
                font-size: clamp(0.85rem, 0.95vw, 1rem);
                font-weight: 600;
                cursor: pointer;
                transition: all 0.3s;
                margin-top: 8px;
            }
            button:hover {
                transform: translateY(-2px);
                box-shadow: 0 8px 20px rgba(0, 132, 168, 0.4);
            }
            button:disabled {
                opacity: 0.6;
                cursor: not-allowed;
                transform: none;
            }
            .link {
                text-align: center;
                margin-top: clamp(14px, 2vh, 22px);
                padding-top: clamp(12px, 1.5vh, 18px);
                border-top: 1px solid #e2e8f0;
            }
            .link a {
                color: #0084a8;
                text-decoration: none;
                font-weight: 500;
                transition: color 0.3s;
                font-size: clamp(0.8rem, 0.9vw, 0.95rem);
            }
            .link a:hover {
                color: #00a8cc;
            }

            .error-modal {
                display: none;
                position: fixed;
                top: 0;
                left: 0;
                right: 0;
                bottom: 0;
                background: rgba(0, 0, 0, 0.5);
                justify-content: center;
                align-items: center;
                z-index: 1000;
            }
            .error-box {
                background: white;
                padding: 30px 40px;
                border-radius: 16px;
                box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
                text-align: center;
                max-width: 400px;
            }
            .error-icon {
                font-size: 3rem;
                margin-bottom: 15px;
            }
            .error-title {
                font-size: 1.3rem;
                font-weight: 600;
                color: #374151;
                margin-bottom: 10px;
            }
            .error-message {
                font-size: 1rem;
                color: #64748b;
                margin-bottom: 20px;
            }
            .error-btn {
                padding: 10px 30px;
                background: linear-gradient(135deg, #0d9488 0%, #0891b2 100%);
                color: white;
                border: none;
                border-radius: 8px;
                font-size: 1rem;
                font-weight: 600;
                cursor: pointer;
                transition: all 0.3s;
            }
            .error-btn:hover {
                transform: translateY(-2px);
                box-shadow: 0 5px 15px rgba(13, 148, 136, 0.3);
            }
            .register-link {
                color: #0d9488;
                text-decoration: none;
                font-weight: 500;
            }
            .register-link:hover {
                text-decoration: underline;
            }
            @media (max-width: 900px) {
                body { flex-direction: column; }
                .left-panel {
                    flex: 0 0 auto;
                    padding: clamp(16px, 2vh, 24px);
                }
                .left-panel img {
                    max-height: 35vh;
                }
                .right-panel {
                    flex: 1 1 auto;
                    box-shadow: 0 -4px 20px rgba(0,0,0,0.06);
                }
            }
        </style>
    </head>
    <body>
        <div class="left-panel">
            <img src="/static/images/login-bg.png" alt="Keylights">
        </div>
        <div class="right-panel">
            <div class="vision-top">
                <h3>科来思愿景</h3>
                <p>成为全球体外诊断行业的信赖伙伴，通过持续的技术创新和卓越的制造服务，引领行业发展，科技呵护生命健康。</p>
            </div>
            <div class="login-container">
            <div class="logo">
                <h1>Keylights</h1>
                <p>IVD 智能故障分析平台</p>
            </div>
            
            <form id="loginForm" method="post">
                <div class="form-group">
                    <label>用户名</label>
                    <input type="text" name="username" id="username" placeholder="请输入用户名" required autocomplete="username">
                </div>
                <div class="form-group">
                    <label>密码</label>
                    <input type="password" name="password" id="password" placeholder="请输入密码" required autocomplete="current-password">
                </div>
                <button type="submit" id="submitBtn">登 录</button>
            </form>
            <div class="link">
                 <a href="/register">没有账号？立即注册</a>
             </div>
         </div>
        </div>
        
        <div class="error-modal" id="errorModal">
            <div class="error-box">
                <div class="error-icon" id="errorIcon">❌</div>
                <div class="error-title" id="errorTitle">登录失败</div>
                <div class="error-message" id="errorMessage"></div>
                <div id="errorAction"></div>
            </div>
        </div>
        
        <script>
            document.getElementById('loginForm').addEventListener('submit', async function(e) {
                e.preventDefault();
                
                const username = document.getElementById('username').value.trim();
                const password = document.getElementById('password').value;
                const submitBtn = document.getElementById('submitBtn');
                
                if (!username || !password) {
                    showError('empty', '用户名和密码不能为空');
                    return;
                }
                
                submitBtn.disabled = true;
                submitBtn.textContent = '登录中...';
                
                try {
                    const formData = new FormData();
                    formData.append('username', username);
                    formData.append('password', password);
                    
                    const response = await fetch('/admin/login', {
                        method: 'POST',
                        body: formData
                    });
                    
                    const data = await response.json();
                    
                    if (data.success) {
                        window.location.href = data.redirect;
                    } else {
                        showError(data.error, data.message);
                    }
                } catch (err) {
                    showError('network', '网络错误，请重试');
                } finally {
                    submitBtn.disabled = false;
                    submitBtn.textContent = '登 录';
                }
            });
            
            function showError(errorType, message) {
                const modal = document.getElementById('errorModal');
                const icon = document.getElementById('errorIcon');
                const title = document.getElementById('errorTitle');
                const msg = document.getElementById('errorMessage');
                const action = document.getElementById('errorAction');
                
                msg.textContent = message;
                
                if (errorType === 'no_user') {
                    icon.textContent = '👤';
                    title.textContent = '账户不存在';
                    action.innerHTML = '<a href="/register" class="register-link">立即注册</a><br><br><button class="error-btn" onclick="closeError()">确定</button>';
                } else if (errorType === 'wrong_password') {
                    icon.textContent = '';
                    title.textContent = '密码错误';
                    action.innerHTML = '<button class="error-btn" onclick="closeError()">重新输入</button>';
                } else {
                    icon.textContent = '⚠️';
                    title.textContent = '提示';
                    action.innerHTML = '<button class="error-btn" onclick="closeError()">确定</button>';
                }
                
                modal.style.display = 'flex';
            }
            
            function closeError() {
                document.getElementById('errorModal').style.display = 'none';
            }
            
            document.getElementById('errorModal').addEventListener('click', function(e) {
                if (e.target === this) {
                    closeError();
                }
            });
        </script>
    </body>
    </html>
    '''



@admin_bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        
        if not username or not password:
            return '''
            <!DOCTYPE html>
            <html>
            <head><meta charset="UTF-8"><title>注册失败</title></head>
            <body style="font-family:system-ui;text-align:center;padding:50px;">
                <h3>❌ 用户名和密码不能为空</h3>
                <a href="/register" style="color:#1e6f9f;">重试</a>
            </body>
            </html>
            '''
        
        if password != confirm_password:
            return '''
            <!DOCTYPE html>
            <html>
            <head><meta charset="UTF-8"><title>注册失败</title></head>
            <body style="font-family:system-ui;text-align:center;padding:50px;">
                <h3>❌ 两次输入的密码不一致</h3>
                <a href="/register" style="color:#1e6f9f;">重试</a>
            </body>
            </html>
            '''
        
        if len(password) < 6:
            return '''
            <!DOCTYPE html>
            <html>
            <head><meta charset="UTF-8"><title>注册失败</title></head>
            <body style="font-family:system-ui;text-align:center;padding:50px;">
                <h3>❌ 密码长度至少6位</h3>
                <a href="/register" style="color:#1e6f9f;">重试</a>
            </body>
            </html>
            '''
        
        with _db()() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT id FROM users WHERE username = %s", (username,))
            if cur.fetchone():
                return '''
                <!DOCTYPE html>
                <html>
                <head><meta charset="UTF-8"><title>注册失败</title></head>
                <body style="font-family:system-ui;text-align:center;padding:50px;">
                    <h3>❌ 用户名已存在</h3>
                    <a href="/register" style="color:#1e6f9f;">重试</a>
                </body>
                </html>
                '''
            
            password_hash = _gph()(password)
            cur.execute("INSERT INTO users (username, password_hash, permission) VALUES (%s, %s, %s)", (username, password_hash, 0))
            conn.commit()
        
        return '''
        <!DOCTYPE html>
        <html>
        <head><meta charset="UTF-8"><title>注册成功</title></head>
        <body style="font-family:system-ui;text-align:center;padding:50px;">
            <h3>✅ 注册成功！</h3>
            <a href="/admin/login" style="color:#667eea;">立即登录</a>
        </body>
        </html>
        '''
    
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Keylights - 用户注册</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body { 
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                display: flex;
                justify-content: center;
                align-items: center;
                margin: 0;
            }
            .register-container {
                background: white;
                padding: 50px 40px;
                border-radius: 24px;
                box-shadow: 0 20px 60px rgba(0,0,0,0.3);
                width: 420px;
                position: relative;
                overflow: hidden;
            }
            .register-container::before {
                content: '';
                position: absolute;
                top: 0;
                left: 0;
                right: 0;
                height: 4px;
                background: linear-gradient(90deg, #10b981 0%, #059669 100%);
            }
            .logo {
                text-align: center;
                margin-bottom: 35px;
            }
            .logo h1 {
                font-size: 2.5rem;
                font-weight: 700;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                background-clip: text;
                margin-bottom: 8px;
                letter-spacing: -1px;
            }
            .logo p {
                color: #64748b;
                font-size: 0.95rem;
                font-weight: 500;
            }
            .form-group {
                margin-bottom: 20px;
            }
            .form-group label {
                display: block;
                color: #374151;
                font-weight: 600;
                margin-bottom: 8px;
                font-size: 0.9rem;
            }
            input {
                width: 100%;
                padding: 14px 16px;
                border: 2px solid #e5e7eb;
                border-radius: 12px;
                font-size: 1rem;
                transition: all 0.3s;
                background: #f9fafb;
            }
            input:focus {
                outline: none;
                border-color: #10b981;
                background: white;
                box-shadow: 0 0 0 3px rgba(16, 185, 129, 0.1);
            }
            button {
                width: 100%;
                padding: 14px;
                background: linear-gradient(135deg, #10b981 0%, #059669 100%);
                color: white;
                border: none;
                border-radius: 12px;
                font-size: 1.05rem;
                font-weight: 600;
                cursor: pointer;
                transition: all 0.3s;
                margin-top: 10px;
            }
            button:hover {
                transform: translateY(-2px);
                box-shadow: 0 10px 25px rgba(16, 185, 129, 0.4);
            }
            .link {
                text-align: center;
                margin-top: 25px;
                padding-top: 20px;
                border-top: 1px solid #e5e7eb;
            }
            .link a {
                color: #667eea;
                text-decoration: none;
                font-weight: 500;
                transition: color 0.3s;
            }
            .link a:hover {
                color: #764ba2;
            }
        </style>
    </head>
    <body>
        <div class="register-container">
            <div class="logo">
                <h1>Keylights</h1>
                <p>IVD 智能故障分析平台</p>
            </div>
            <form method="post">
                <div class="form-group">
                    <label>用户名</label>
                    <input type="text" name="username" placeholder="请输入用户名" required autocomplete="username">
                </div>
                <div class="form-group">
                    <label>密码</label>
                    <input type="password" name="password" placeholder="至少6位密码" required autocomplete="new-password">
                </div>
                <div class="form-group">
                    <label>确认密码</label>
                    <input type="password" name="confirm_password" placeholder="再次输入密码" required autocomplete="new-password">
                </div>
                <button type="submit">注 册</button>
            </form>
            <div class="link">
                <a href="/admin/login">已有账号？立即登录</a>
            </div>
        </div>
    </body>
    </html>
    '''



@admin_bp.route('/admin/rules')
def admin_rules():
    return _rts()(ADMIN_HTML)



@admin_bp.route('/admin/logout')
def admin_logout():
    session.clear()
    return redirect('/admin/login')

# ======================================================================
# ========== 以下为三个 HTML 模板字符串（请勿删除） ==========
# ========== 模板 1: MAIN_HTML (主页) ==========
with open(os.path.join(os.path.dirname(__file__), 'templates', 'main.html'), 'r') as _f:
    MAIN_HTML = _f.read()
_get_app().MAIN_HTML = MAIN_HTML
# ========== MAIN_HTML 结束 ==========

# ========== 模板: BUGS_HTML (上位机Bug库) ==========
with open(os.path.join(os.path.dirname(__file__), 'templates', 'bugs.html'), 'r') as _f:
    BUGS_HTML = _f.read()
_get_app().BUGS_HTML = BUGS_HTML

# ========== 模板: LIS_ISSUES_HTML (LIS问题功能) ==========
with open(os.path.join(os.path.dirname(__file__), 'templates', 'lis_issues.html'), 'r') as _f:
    LIS_ISSUES_HTML = _f.read()
_get_app().LIS_ISSUES_HTML = LIS_ISSUES_HTML

# ========== 模板: HARDWARE_FAILURES_HTML (硬件故障案例) ==========
with open(os.path.join(os.path.dirname(__file__), 'templates', 'hardware_failures.html'), 'r') as _f:
    HARDWARE_FAILURES_HTML = _f.read()
_get_app().HARDWARE_FAILURES_HTML = HARDWARE_FAILURES_HTML

# ========== 模板: BOARD_COMPATIBILITY_HTML (电路板兼容) ==========
with open(os.path.join(os.path.dirname(__file__), 'templates', 'board_compatibility.html'), 'r') as _f:
    BOARD_COMPATIBILITY_HTML = _f.read()
_get_app().BOARD_COMPATIBILITY_HTML = BOARD_COMPATIBILITY_HTML

# ========== 模板 2: ADMIN_HTML (管理后台) ==========
with open(os.path.join(os.path.dirname(__file__), 'templates', 'admin.html'), 'r') as _f:
    ADMIN_HTML = _f.read()
_get_app().ADMIN_HTML = ADMIN_HTML
# ========== ADMIN_HTML 结束 ==========

# ========== 模板 3: ANALYSIS_HTML (分析结果视图) ==========
with open(os.path.join(os.path.dirname(__file__), 'templates', 'analysis.html'), 'r') as _f:
    ANALYSIS_HTML = _f.read()
_get_app().ANALYSIS_HTML = ANALYSIS_HTML
# ========== ANALYSIS_HTML 结束 ==========

# ========== 模板: MAIN_HTML (主页) ==========
with open(os.path.join(os.path.dirname(__file__), 'templates', 'main.html'), 'r') as _f:
    MAIN_HTML = _f.read()
_get_app().MAIN_HTML = MAIN_HTML

# ========== 主页路由 ==========
@admin_bp.route('/')
def index():
    if not session.get('user_id'):
        return redirect('/admin/login')
    is_admin = session.get('super_admin_logged_in', False)
    return _rts()(MAIN_HTML, is_admin=is_admin, username=session.get('username', ''))

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
    print(f"🔐 管理员密码: {_config().ADMIN_PASSWORD}")
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

