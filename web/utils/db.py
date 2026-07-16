#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IVD平台 - 数据库工具"""

import atexit
import threading
from contextlib import contextmanager
from psycopg2.pool import ThreadedConnectionPool

from config import Config

_pool = None
_redis_client = None
_redis_lock = threading.Lock()


def _close_db_pool():
    global _pool
    if _pool is not None:
        try:
            _pool.closeall()
        except Exception:
            pass
        _pool = None

atexit.register(_close_db_pool)


def init_db_pool():
    global _pool
    if _pool is None:
        _pool = ThreadedConnectionPool(
            1, 20,
            host=Config.DB_HOST,
            port=Config.DB_PORT,
            user=Config.DB_USER,
            password=Config.DB_PASSWORD,
            dbname=Config.DB_NAME
        )
    return _pool


def get_db_connection():
    if _pool is None:
        init_db_pool()
    conn = _pool.getconn()
    conn.autocommit = False
    return conn


def put_db_connection(conn):
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


def get_redis():
    global _redis_client
    with _redis_lock:
        if _redis_client is None:
            import redis
            _redis_client = redis.Redis.from_url(
                Config.REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5
            )
        return _redis_client