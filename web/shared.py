#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IVD平台 - 共享模块（统一入口）"""

import os
import re as _re
import time
import threading
import secrets as _secrets
import logging as _logging
from contextlib import contextmanager
from psycopg2.pool import SimpleConnectionPool
from psycopg2.extras import RealDictCursor
import redis as _redis_mod
import json as _json

from hybrid_connection import get_db_host, get_redis_url, get_go_parser_url

from utils.auth import login_required, api_login_required, api_super_admin_required
from utils.templates import load_templates, get_template, set_template
from utils.audit import audit_log


class JsonFormatter(_logging.Formatter):
    def format(self, record):
        log_entry = {
            'timestamp': self.formatTime(record, self.datefmt),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            log_entry['exception'] = self.formatException(record.exc_info)
        try:
            return _json.dumps(log_entry, ensure_ascii=False)
        except Exception:
            return f'{{"timestamp":"{log_entry["timestamp"]}","level":"{log_entry["level"]}","logger":"{log_entry["logger"]}","message":"log serialize error"}}'


def safe_img_table(model, prefix):
    clean = _CLEAN_RE.sub('', model.lower())
    if not clean:
        return None
    return f"{prefix}_{clean}"

_UUID_RE = _re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', _re.IGNORECASE)
_NUMERIC_SEGMENT_RE = _re.compile(r'/\d+(?=/|$)')
_METRICS_SKIP_PATHS = {'/metrics', '/favicon.ico', '/health', '/robots.txt'}
_CLEAN_RE = _re.compile(r'[^a-zA-Z0-9_]')
ALLOWED_IMAGE_TYPES = frozenset({'image/jpeg', 'image/png', 'image/gif', 'image/webp'})

_table_cache = {}
_table_cache_time = {}
_table_cache_lock = threading.Lock()
_TABLE_CACHE_TTL = 300

def resolve_table(model: str, prefix: str):
    m = _CLEAN_RE.sub('', model.lower())
    if not m:
        return None
    tbl = f'{prefix}_{m}'
    now = time.time()
    with _table_cache_lock:
        if tbl in _table_cache:
            cache_time = _table_cache_time.get(tbl, 0)
            if now - cache_time < _TABLE_CACHE_TTL:
                return _table_cache[tbl]
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass(%s)", (tbl,))
        exists = cur.fetchone()[0] is not None
    result = tbl if exists else None
    with _table_cache_lock:
        _table_cache[tbl] = result
        _table_cache_time[tbl] = now
    return result

def format_row_timestamps(row: dict, fields=('created_at', 'updated_at')) -> dict:
    for f in fields:
        if row.get(f):
            row[f] = row[f].strftime('%Y-%m-%d %H:%M:%S')
    return row

def normalize_endpoint(path):
    if path in _METRICS_SKIP_PATHS:
        return None
    p = _UUID_RE.sub(':id', path)
    p = _NUMERIC_SEGMENT_RE.sub('/:id', p)
    if p.endswith('/') and len(p) > 1:
        p = p.rstrip('/')
    return p

_SECRET_KEY_DEFAULT = 'ivd-secret-key-2026'

class Config:
    SECRET_KEY = os.getenv('SECRET_KEY', _SECRET_KEY_DEFAULT)
    if SECRET_KEY == _SECRET_KEY_DEFAULT:
        SECRET_KEY = _secrets.token_hex(32)
        _logging.getLogger(__name__).warning("SECRET_KEY未配置，已自动生成随机密钥")
    ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'admin123')
    SUPER_ADMIN_PASSWORD = os.getenv('SUPER_ADMIN_PASSWORD', 'super2026')
    DB_HOST = get_db_host()
    DB_PORT = int(os.getenv('DB_PORT', '5432'))
    DB_USER = os.getenv('DB_USER', 'ivd_user')
    DB_PASSWORD = os.getenv('DB_PASSWORD', 'ivd_pass')
    DB_NAME = os.getenv('DB_NAME', 'ivd_fault_db')
    REDIS_URL = get_redis_url()
    MAX_CONTENT_LENGTH = int(os.getenv('MAX_CONTENT_LENGTH', str(200 * 1024 * 1024)))
    DEBUG = os.getenv('DEBUG', 'False').lower() == 'true'
    ANALYSIS_TTL_HOURS = int(os.getenv('ANALYSIS_TTL_HOURS', '2'))
    UPLOAD_DIR = os.getenv('UPLOAD_DIR', '/app/uploads')
    GO_PARSER_URL = get_go_parser_url()
    DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY', 'your-deepseek-api-key')

_pool = None
_pool_lock = threading.Lock()

def _close_db_pool():
    global _pool
    if _pool is not None:
        try:
            _pool.closeall()
        except Exception:
            pass
        _pool = None

def init_db_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            min_conn = int(os.getenv('DB_MIN_CONN', '2'))
            max_conn = int(os.getenv('DB_MAX_CONN', '15'))
            _pool = SimpleConnectionPool(
                min_conn, max_conn,
                host=Config.DB_HOST,
                port=Config.DB_PORT,
                user=Config.DB_USER,
                password=Config.DB_PASSWORD,
                dbname=Config.DB_NAME,
                keepalives=1,
                keepalives_idle=30,
                keepalives_interval=10,
                keepalives_count=5,
                connect_timeout=5,
            )
            _warmup_conns = min(10, max_conn)
            _warmup = []
            try:
                for _ in range(_warmup_conns):
                    _warmup.append(_pool.getconn())
            except Exception:
                pass
            for c in _warmup:
                try:
                    _pool.putconn(c)
                except Exception:
                    pass
    return _pool

_VALIDATE_INTERVAL = 30
_conn_last_validated = {}
_MAX_VALIDATED_ENTRIES = 100
_conn_validate_lock = threading.Lock()

def get_db_connection():
    global _pool
    if _pool is None:
        init_db_pool()
    conn = _pool.getconn()
    conn_id = id(conn)
    now = time.time()
    with _conn_validate_lock:
        if len(_conn_last_validated) > _MAX_VALIDATED_ENTRIES:
            _conn_last_validated.clear()
        last = _conn_last_validated.get(conn_id, 0)
    if now - last > _VALIDATE_INTERVAL:
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
            with _conn_validate_lock:
                _conn_last_validated[conn_id] = now
        except Exception:
            _pool.putconn(conn, close=True)
            with _conn_validate_lock:
                _conn_last_validated.pop(conn_id, None)
            conn = _pool.getconn()
            with _conn_validate_lock:
                _conn_last_validated[id(conn)] = time.time()
    return conn

def put_db_connection(conn):
    global _pool
    if _pool is not None:
        with _conn_validate_lock:
            _conn_last_validated.pop(id(conn), None)
        _pool.putconn(conn)

@contextmanager
def db_connection():
    conn = get_db_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_connection(conn)

_redis_client = None
_redis_lock = threading.Lock()

def get_redis():
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    with _redis_lock:
        if _redis_client is None:
            _redis_client = _redis_mod.Redis.from_url(
                Config.REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
                retry_on_timeout=True,
                health_check_interval=30,
                max_connections=25,
                socket_keepalive=True,
            )
    return _redis_client

_ALLOWED_TABLES = None

def get_allowed_tables():
    global _ALLOWED_TABLES
    if _ALLOWED_TABLES is not None:
        return _ALLOWED_TABLES
    try:
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
            _ALLOWED_TABLES = frozenset(row[0] for row in cur.fetchall())
    except Exception:
        _ALLOWED_TABLES = frozenset()
    return _ALLOWED_TABLES

def invalidate_allowed_tables_cache():
    global _ALLOWED_TABLES
    with _table_cache_lock:
        _ALLOWED_TABLES = None
        _table_cache.clear()
        _table_cache_time.clear()

def validate_table_name(tbl):
    allowed = get_allowed_tables()
    if tbl not in allowed:
        raise ValueError(f"非法表名: {tbl}")
