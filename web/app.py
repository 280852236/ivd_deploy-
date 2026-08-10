#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IVD 智能故障分析平台 - v3.0 (PostgreSQL + Redis)
功能: 完整保留 v2.6 所有功能，底层全面升级为 PostgreSQL + Redis
"""

import os
import logging
import logging.handlers
import secrets
import tempfile
from datetime import datetime
from functools import wraps

from flask import (
    Flask, request, jsonify,
    redirect, url_for, session
)
from dotenv import load_dotenv

from prometheus_client import Counter, Histogram
import time

try:
    import gevent.monkey
    gevent.monkey.patch_all()

except ImportError:
    pass

app = Flask(__name__)
app.json.ensure_ascii = False


from shared import normalize_endpoint

REQUEST_COUNT = Counter('http_request_count', 'HTTP Request Count', ['method', 'endpoint', 'status'])
REQUEST_LATENCY = Histogram('http_request_latency_seconds', 'HTTP Request Latency', ['method', 'endpoint'])

@app.before_request
def before_request():
    request.start_time = time.time()
    if request.path.startswith('/api/') and request.path not in ('/api/health', '/api/csrf-token'):
        try:
            from services.cache import get_redis as _get_redis
            _r = _get_redis()
            _ip = request.headers.get('X-Real-IP') or (request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()) or 'unknown'
            _now = int(time.time())
            _window = _now - (_now % 60)
            _rk = f'ratelimit:{_ip}:{_window}'
            _pipe = _r.pipeline()
            _pipe.incr(_rk)
            _pipe.expire(_rk, 120)
            _count = _pipe.execute()[0]
            if _count > 60:
                return jsonify({'error': '请求过于频繁，请稍后再试'}), 429
        except Exception:
            pass

    if request.method in ('POST', 'PUT', 'DELETE', 'PATCH') and request.path.startswith('/api/') and request.path not in ('/api/health', '/api/csrf-token', '/api/login'):
        session_token = session.get('csrf_token')
        if session_token:
            provided = request.headers.get('X-CSRFToken') or request.args.get('csrf_token') or request.form.get('csrf_token')
            if not provided:
                try:
                    provided = request.get_json(silent=True, force=True).get('csrf_token')
                except Exception:
                    pass
            if provided and provided != session_token:
                logger.warning(f"CSRF验证失败: {request.method} {request.path}")
                return jsonify({'error': 'CSRF验证失败'}), 403

@app.after_request
def after_request(response):
    if hasattr(request, 'start_time'):
        endpoint = normalize_endpoint(request.path)
        if endpoint is not None:
            latency = time.time() - request.start_time
            REQUEST_LATENCY.labels(method=request.method, endpoint=endpoint).observe(latency)
            REQUEST_COUNT.labels(method=request.method, endpoint=endpoint, status=response.status_code).inc()
    return response


# ========== 加载环境变量 ==========
load_dotenv()

# ========== 注册Blueprints ==========
from blueprints import analysis_bp, lis_bp, bugs_bp, hardware_bp, admin_bp
app.register_blueprint(analysis_bp)
app.register_blueprint(lis_bp)
app.register_blueprint(bugs_bp)
app.register_blueprint(hardware_bp)
app.register_blueprint(admin_bp)


# ========== 全局错误处理 ==========
@app.errorhandler(404)
def not_found_error(e):
    if request.path.startswith('/api/'):
        return jsonify({'error': '资源不存在'}), 404
    return redirect(url_for('index'))

@app.errorhandler(500)
def internal_error(e):
    logger.exception(f"内部错误: {request.method} {request.path}")
    if request.path.startswith('/api/'):
        return jsonify({'error': '服务器内部错误'}), 500
    return jsonify({'error': '服务器内部错误'}), 500

@app.errorhandler(Exception)
def unhandled_exception(e):
    logger.exception(f"未捕获异常: {request.method} {request.path}: {e}")
    if request.path.startswith('/api/'):
        return jsonify({'error': '服务器内部错误'}), 500
    return jsonify({'error': '服务器内部错误'}), 500


# ========== 加载模板 ==========
import shared
template_dir = os.path.join(os.path.dirname(__file__), 'blueprints', 'templates')
shared.load_templates(template_dir)




# ========== 配置 & 共享模块 ==========
from shared import Config, db_connection, init_db_pool, get_redis

# ========== 从services导入工具函数 ==========
from services.data_init import init_db

app.config.from_object(Config)
app.secret_key = Config.SECRET_KEY
app.config['MAX_CONTENT_LENGTH'] = Config.MAX_CONTENT_LENGTH
app.config['JSON_AS_ASCII'] = False
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = 28800  # 8小时超时
app.config['SESSION_COOKIE_SECURE'] = True  # 仅HTTPS传输


import atexit
atexit.register(shared._close_db_pool)

# ========== 日志配置（JSON结构化） ==========
_json_fmt = shared.JsonFormatter(datefmt='%Y-%m-%dT%H:%M:%S')
logging.basicConfig(
    level=os.getenv('LOG_LEVEL', 'INFO'),
    handlers=[
        logging.handlers.RotatingFileHandler('ivd_app.log', maxBytes=100*1024*1024, backupCount=10, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
for h in logging.root.handlers:
    h.setFormatter(_json_fmt)
logger = logging.getLogger(__name__)


def cleanup_temp_zip_files():
    tmp_dir = tempfile.gettempdir()
    count = 0
    for f in os.listdir(tmp_dir):
        if f.startswith('ivd_analysis_') and f.endswith('.zip'):
            try:
                os.remove(os.path.join(tmp_dir, f))
                count += 1
            except Exception:
                pass
    if count:
        logger.info(f"已清理 {count} 个临时ZIP文件")

# ========== 认证装饰器（从shared导入） ==========
from shared import login_required, api_login_required, api_super_admin_required

# ========== CSRF Token ==========
@app.route('/api/csrf-token', methods=['GET'])
def csrf_token():
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(32)
    return jsonify({'csrf_token': session['csrf_token']})

# ========== API路由 ==========
@app.route('/metrics', methods=['GET'])
def metrics_endpoint():
    """手动添加的metrics端点"""
    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
    from flask import Response
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)

@app.route('/api/health', methods=['GET'])
def health_check():
    health = {'status': 'healthy', 'timestamp': datetime.now().isoformat(), 'version': '3.0', 'services': {}}
    try:
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
        health['services']['database'] = 'ok'
    except Exception as e:
        health['services']['database'] = f'error: {str(e)}'
        health['status'] = 'degraded'
    try:
        r = get_redis()
        r.ping()
        health['services']['redis'] = 'ok'
    except Exception as e:
        health['services']['redis'] = f'error: {str(e)}'
        health['status'] = 'degraded'
    try:
        if shared._pool is not None:
            health['pool'] = {
                'min': shared._pool.minconn,
                'max': shared._pool.maxconn,
                'idle': len(shared._pool._idle_cache) if hasattr(shared._pool, '_idle_cache') else -1,
                'size': shared._pool._pool_size if hasattr(shared._pool, '_pool_size') else -1,
            }
    except Exception:
        pass
    status_code = 200 if health['status'] == 'healthy' else 503
    return jsonify(health), status_code



# ========== 初始化数据库 ==========
init_db()

# ========== 启动应用 ==========
if __name__ == '__main__':
    init_db_pool()
    cleanup_temp_zip_files()
    print("\n" + "="*60)
    print("🚀 IVD 智能故障分析平台 v3.0 (PostgreSQL + Redis)")
    print("="*60)
    logger.warning("管理员密码已通过环境变量配置")
    print(f"🌐 访问地址: http://localhost:8081")
    print(f"🔧 管理后台: http://localhost:8081/admin/rules")
    print("="*60 + "\n")
    from waitress import serve
    serve(app, host='0.0.0.0', port=8081, threads=4, channel_timeout=300, max_request_body_size=209715200)