#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IVD平台 - API缓存模块"""

import json
import hashlib
import logging
from functools import wraps
from flask import request, jsonify, make_response
from shared import get_redis

logger = logging.getLogger(__name__)

def api_cache(ttl=60, key_prefix='api'):
    """API响应缓存装饰器

    Args:
        ttl: 缓存存活时间(秒)
        key_prefix: 缓存键前缀
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if request.method != 'GET':
                return func(*args, **kwargs)

            cache_key = _build_cache_key(key_prefix)
            try:
                r = get_redis()
                cached = r.get(cache_key)
                if cached:
                    resp = make_response(cached)
                    resp.headers['Content-Type'] = 'application/json'
                    resp.headers['X-Cache'] = 'HIT'
                    return resp
            except Exception:
                pass

            result = func(*args, **kwargs)

            try:
                if hasattr(result, 'get_json'):
                    data = result.get_json()
                elif isinstance(result, (dict, list)):
                    data = result
                else:
                    return result
                r = get_redis()
                r.setex(cache_key, ttl, json.dumps(data, ensure_ascii=False, default=str))
            except Exception:
                pass

            if hasattr(result, 'headers'):
                result.headers['X-Cache'] = 'MISS'
            return result
        return wrapper
    return decorator

def _build_cache_key(prefix):
    path = request.path
    args = sorted(request.args.items())
    raw = f"{path}:{args}"
    h = hashlib.md5(raw.encode()).hexdigest()[:12]
    return f"{prefix}:{h}"

def invalidate_cache(prefix):
    """清除指定前缀的所有缓存"""
    try:
        r = get_redis()
        keys = r.keys(f"{prefix}:*")
        if keys:
            r.delete(*keys)
            logger.info(f"清除缓存: {prefix}, {len(keys)}个键")
    except Exception as e:
        logger.warning(f"清除缓存失败: {e}")
