from functools import wraps
import time


_RATE_LIMIT_WINDOW = 60
_RATE_LIMIT_MAX = 60


def rate_limit(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        from flask import request, jsonify
        from services.cache import get_redis
        try:
            r = get_redis()
            ip = request.headers.get('X-Real-IP') or (request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()) or 'unknown'
            key = f'ratelimit:{ip}'
            now = int(time.time())
            window = now - (now % _RATE_LIMIT_WINDOW)
            rk = f'{key}:{window}'
            pipe = r.pipeline()
            pipe.incr(rk)
            pipe.expire(rk, _RATE_LIMIT_WINDOW * 2)
            count = pipe.execute()[0]
            if count > _RATE_LIMIT_MAX:
                return jsonify({'error': '请求过于频繁，请稍后再试'}), 429
        except Exception:
            pass
        return f(*args, **kwargs)
    return decorated


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        from flask import session, redirect, url_for
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin.admin_login'))
        return f(*args, **kwargs)
    return decorated


def api_login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        from flask import session, jsonify
        if not session.get('admin_logged_in'):
            return jsonify({'error': '请先登录'}), 401
        return f(*args, **kwargs)
    return decorated


def api_super_admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        from flask import session, jsonify
        if not session.get('super_admin_logged_in'):
            return jsonify({'error': '需要超管权限'}), 403
        return f(*args, **kwargs)
    return decorated