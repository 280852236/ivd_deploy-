#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IVD 智能故障分析平台 - v3.0 (PostgreSQL + Redis)
功能: 完整保留 v2.6 所有功能，底层全面升级为 PostgreSQL + Redis
"""

import os
import re
import json
import zipfile
import rarfile
import logging
import uuid
import threading
import tempfile
from datetime import datetime, timedelta
from functools import wraps, lru_cache
from typing import Dict, List, Optional
from urllib.parse import unquote

from flask import (
    Flask, request, jsonify, render_template_string,
    redirect, url_for, session
)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import redis
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool
from contextlib import contextmanager
import requests
GO_PARSER_URL = os.getenv('GO_PARSER_URL', 'http://localhost:8082/parse')

from prometheus_flask_exporter import PrometheusMetrics
from prometheus_client import Counter, Histogram
import time

app = Flask(__name__)
app.json.ensure_ascii = False


metrics = PrometheusMetrics(app, static_labels={'app': 'ivd'})

# 手动添加HTTP请求计数器
REQUEST_COUNT = Counter('http_request_count', 'HTTP Request Count', ['method', 'endpoint', 'status'])
REQUEST_LATENCY = Histogram('http_request_latency_seconds', 'HTTP Request Latency', ['method', 'endpoint'])

@app.before_request
def before_request():
    request.start_time = time.time()

@app.after_request
def after_request(response):
    if hasattr(request, 'start_time') and not request.path.startswith('/metrics'):
        latency = time.time() - request.start_time
        REQUEST_LATENCY.labels(method=request.method, endpoint=request.path).observe(latency)
        REQUEST_COUNT.labels(method=request.method, endpoint=request.path, status=response.status_code).inc()
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

# 为Blueprint的写操作端点添加认证装饰器
for bp_name, bp in [('bugs', bugs_bp), ('hardware', hardware_bp), ('admin', admin_bp)]:
    for key, func in list(bp.view_functions.items()):
        original = func
        if 'delete_bug_image' in key or 'delete_all_bug_images' in key:
            def make_super_wrapper(orig):
                def wrapper(*args, **kwargs):
                    if not session.get('super_admin_logged_in'):
                        return jsonify({'error': '需要高等级管理员权限'}), 403
                    return orig(*args, **kwargs)
                wrapper.__name__ = orig.__name__
                return wrapper
            bp.view_functions[key] = make_super_wrapper(original)
        elif any(m in key for m in ['delete', 'update', 'create', 'post', 'add', 'edit', 'import']):
            def make_wrapper(orig):
                def wrapper(*args, **kwargs):
                    if not session.get('admin_logged_in'):
                        return jsonify({'error': '请先登录'}), 401
                    return orig(*args, **kwargs)
                wrapper.__name__ = orig.__name__
                return wrapper
            bp.view_functions[key] = make_wrapper(original)

# ========== 混合连接方案 ==========
from hybrid_connection import get_db_host, get_redis_url, get_go_parser_url

# ========== 配置 ==========
class Config:
    SECRET_KEY = os.getenv('SECRET_KEY', 'ivd-secret-key-2026')
    ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'admin123')
    SUPER_ADMIN_PASSWORD = os.getenv('SUPER_ADMIN_PASSWORD', 'super2026')
    # 混合方案：优先服务名，fallback到固定IP
    DB_HOST = get_db_host()  # 智能选择：postgres 或 172.28.0.10
    DB_PORT = int(os.getenv('DB_PORT', '5432'))
    DB_USER = os.getenv('DB_USER', 'ivd_user')
    DB_PASSWORD = os.getenv('DB_PASSWORD', 'ivd_pass')
    DB_NAME = os.getenv('DB_NAME', 'ivd_fault_db')
    REDIS_URL = get_redis_url()  # 智能选择：redis://redis:6379/0 或 redis://172.28.0.11:6379/0
    MAX_CONTENT_LENGTH = int(os.getenv('MAX_CONTENT_LENGTH', 200 * 1024 * 1024))
    DEBUG = os.getenv('DEBUG', 'False').lower() == 'true'
    ANALYSIS_TTL_HOURS = int(os.getenv('ANALYSIS_TTL_HOURS', '2'))
    UPLOAD_DIR = os.getenv('UPLOAD_DIR', '/app/uploads')  
    GO_PARSER_URL = get_go_parser_url()


app.config.from_object(Config)
app.secret_key = Config.SECRET_KEY
app.config['MAX_CONTENT_LENGTH'] = Config.MAX_CONTENT_LENGTH
app.config['JSON_AS_ASCII'] = False

# ========== 日志配置 ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('ivd_app.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# ========== 数据库连接池 ==========
_pool = None

import atexit
def _close_db_pool():
    global _pool
    if _pool is not None:
        try:
            _pool.closeall()
        except Exception:
            pass
        _pool = None

atexit.register(_close_db_pool)

def convert_to_wsl_path(win_path: str) -> str:
    import re
    drive, rest = os.path.splitdrive(win_path)
    if drive:
        drive_letter = drive[0].lower()
        rest = rest.replace('\\', '/')
        return f"/mnt/{drive_letter}{rest}"
    return win_path.replace('\\', '/')

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

# ========== Redis 客户端 ==========
_redis_client = None
_redis_lock = threading.Lock()

def get_redis():
    global _redis_client
    with _redis_lock:
        if _redis_client is None:
            _redis_client = redis.Redis.from_url(
                Config.REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
                retry_on_timeout=True,
                health_check_interval=30,
            )
        try:
            _redis_client.ping()
        except Exception:
            logger.warning("Redis连接断开，尝试重连...")
            try:
                _redis_client = redis.Redis.from_url(
                    Config.REDIS_URL,
                    decode_responses=True,
                    socket_connect_timeout=5,
                    socket_timeout=5,
                    retry_on_timeout=True,
                    health_check_interval=30,
                )
                _redis_client.ping()
                logger.info("Redis重连成功")
            except Exception as e:
                logger.error(f"Redis重连失败: {e}")
                _redis_client = None
                raise
        return _redis_client

# ========== 工具函数 ==========
def escape_html(text):
    if not text:
        return ''
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;').replace("'", '&#039;')

def highlight_line_text(escaped_line, keywords):
    if not keywords:
        return escaped_line
    import re as _re
    for kw in keywords:
        escaped_kw = _re.escape(escape_html(kw))
        escaped_line = _re.sub(
            f'({escaped_kw})',
            r'<span style="background:#fef08a;border-radius:2px;padding:0 2px;">\1</span>',
            escaped_line,
            flags=_re.IGNORECASE
        )
    return escaped_line

def get_table_name(model_name: str) -> str:
    clean_name = re.sub(r'[^a-zA-Z0-9_]', '', model_name)
    if not clean_name:
        raise ValueError(f"无效的型号名称: {model_name!r}，过滤后为空")
    return f"motor_status_{clean_name.lower()}"

def ensure_table_exists(model_name: str):
    table_name = get_table_name(model_name)
    with db_connection() as conn:
        cur = conn.cursor()
        # 检查表是否存在
        cur.execute("SELECT to_regclass(%s)", (table_name,))
        if not cur.fetchone()['to_regclass']:
            cur.execute(f"""
                CREATE TABLE {table_name} (
                    id SERIAL PRIMARY KEY,
                    board_card TEXT NOT NULL,
                    motor_code TEXT NOT NULL,
                    status_code TEXT NOT NULL,
                    motor_name TEXT,
                    action_type TEXT,
                    target_value TEXT,
                    sensor TEXT,
                    description TEXT,
                    full_description TEXT,
                    source_file TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_lookup ON {table_name}(board_card, motor_code, status_code)")
            logger.info(f"创建表: {table_name}")
        else:
            # 检查 source_file 列是否存在
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = %s AND column_name = 'source_file'
            """, (table_name,))
            if not cur.fetchone():
                cur.execute(f"ALTER TABLE {table_name} ADD COLUMN source_file TEXT")
                logger.info(f"已为表 {table_name} 添加 source_file 列")

# ========== 数据库初始化（仅用于默认数据，需改用 PostgreSQL） ==========
def init_db():
    """初始化 PostgreSQL <div class="background-vision">
        <h2>科来思愿景</h2>
        <p>成为全球体外诊断行业的信赖伙伴，通过持续的技术创新和卓越的制造服务，引领行业发展，科技呵护生命健康。</p>
    </div>结构并插入默认数据（如无数据）"""
    with db_connection() as conn:
        cur = conn.cursor()
        # 创建基础表（如果不存在）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS series (
                id SERIAL PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS models (
                id SERIAL PRIMARY KEY,
                series_id INTEGER NOT NULL REFERENCES series(id),
                name TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(series_id, name)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS rules (
                id SERIAL PRIMARY KEY,
                model_id INTEGER NOT NULL REFERENCES models(id),
                keywords TEXT NOT NULL,
                advice TEXT NOT NULL,
                source TEXT DEFAULT 'manual',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS rule_keywords (
                id SERIAL PRIMARY KEY,
                rule_id INTEGER NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
                keyword TEXT NOT NULL
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_keyword ON rule_keywords(keyword)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS version_history (
                id SERIAL PRIMARY KEY,
                version INTEGER NOT NULL,
                action TEXT NOT NULL,
                rule_id INTEGER,
                rule_snapshot TEXT,
                operator TEXT DEFAULT 'system',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_admin BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("SELECT name FROM models")
        for (model_name,) in cur.fetchall():
            tbl = f"software_bugs_{model_name.lower()}"
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {tbl} (
                    id SERIAL PRIMARY KEY,
                    software_version TEXT NOT NULL,
                    title TEXT NOT NULL,
                    cause TEXT DEFAULT '',
                    workaround TEXT DEFAULT '',
                    solution TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute(f"CREATE INDEX IF NOT EXISTS idx_swbugs_{model_name.lower()}_ver ON {tbl}(software_version)")
            img_tbl = f"bug_images_{model_name.lower()}"
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {img_tbl} (
                    id SERIAL PRIMARY KEY,
                    bug_id INTEGER NOT NULL REFERENCES {tbl}(id) ON DELETE CASCADE,
                    image_data BYTEA NOT NULL,
                    image_mime TEXT NOT NULL,
                    sort_order INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute(f"CREATE INDEX IF NOT EXISTS idx_bugimg_{model_name.lower()}_bug ON {img_tbl}(bug_id)")

    init_default_data()

def init_default_data():
    with db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # 创建默认管理员账户
        cur.execute("SELECT id FROM users WHERE username = 'admin'")
        if not cur.fetchone():
            admin_password = os.getenv('ADMIN_PASSWORD', 'admin123')
            password_hash = generate_password_hash(admin_password)
            cur.execute("INSERT INTO users (username, password_hash, permission) VALUES (%s, %s, %s)", ('admin', password_hash, 1))
            conn.commit()
            print(f"✅ 已创建默认管理员账户: admin / {admin_password}")
        
        # 检查是否已有系列
        cur.execute("SELECT id, name FROM series")
        if cur.fetchone():
            return  # 已有数据，不重复插入

        # 插入系列
        series_data = ['SMART', 'VENUS']
        for name in series_data:
            cur.execute("INSERT INTO series (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (name,))
        conn.commit()

        cur.execute("SELECT id, name FROM series")
        series_map = {row['name']: row['id'] for row in cur.fetchall()}

        default_rules = [
            ('SMART', 'SMART6500', '样本空吸,样本不足', '🔧 排查：检查样本管液位、加样针'),
            ('SMART', 'SMART6500', '试剂空吸,试剂不足', '🔧 排查：检查试剂瓶、试剂针'),
            ('SMART', 'SMART6500', '压力异常', '🔧 排查：检查管路、泵膜'),
            ('SMART', 'SMART500', '温度失控', '🔧 排查：检查加热片、传感器'),
            ('VENUS', 'VENUS100', '试剂空', '🔧 更换试剂，检查液位电路'),
            ('VENUS', 'VENUS500', '通讯失败', '🔧 检查线缆、重启设备'),
            ('VENUS', 'VENUS9000', '结果异常', '🔧 执行质控、清洁光学系统'),
            ('VENUS', 'VENUS9900', '卡杯', '🔧 检查清洗针、泵阀'),
        ]
        for series_name, model_name, keywords, advice in default_rules:
            series_id = series_map.get(series_name)
            if not series_id:
                continue
            cur.execute("INSERT INTO models (series_id, name) VALUES (%s, %s) ON CONFLICT (series_id, name) DO NOTHING", (series_id, model_name))
            conn.commit()
            cur.execute("SELECT id FROM models WHERE series_id=%s AND name=%s", (series_id, model_name))
            row = cur.fetchone()
            if row:
                model_id = row['id']
                cur.execute("SELECT id FROM rules WHERE model_id=%s AND keywords=%s", (model_id, keywords))
                if not cur.fetchone():
                    cur.execute("INSERT INTO rules (model_id, keywords, advice) VALUES (%s, %s, %s) RETURNING id", (model_id, keywords, advice))
                    rule_id = cur.fetchone()['id']
                    for kw in keywords.split(','):
                        kw = kw.strip()
                        if kw:
                            cur.execute("INSERT INTO rule_keywords (rule_id, keyword) VALUES (%s, %s)", (rule_id, kw))
        conn.commit()

# ========== Redis 缓存函数 ==========
def store_analysis_result(analysis_id, data):
    for attempt in range(3):
        try:
            r = get_redis()
            key = f"analysis:{analysis_id}"
            
            files = data.get('files', {})
            if files:
                for filename, file_data in files.items():
                    file_key = f"file_content:{analysis_id}:{filename}"
                    r.set(file_key, json.dumps(file_data, ensure_ascii=False), ex=Config.ANALYSIS_TTL_HOURS * 3600)
            
            data_without_files = {k: v for k, v in data.items() if k != 'files'}
            data_without_files['has_separate_files'] = bool(files)
            data_without_files['file_names'] = list(files.keys())
            
            r.set(key, json.dumps(data_without_files, ensure_ascii=False), ex=Config.ANALYSIS_TTL_HOURS * 3600)
            return
        except Exception as e:
            logger.warning(f"store_analysis_result 第{attempt+1}次失败: {e}")
            if attempt == 2:
                raise

def get_analysis_result(analysis_id, include_files=False):
    for attempt in range(3):
        try:
            r = get_redis()
            key = f"analysis:{analysis_id}"
            data = r.get(key)
            if not data:
                return None
            
            result = json.loads(data)
            
            if include_files and result.get('has_separate_files'):
                file_names = result.get('file_names', [])
                result['files'] = {}
                for filename in file_names:
                    file_key = f"file_content:{analysis_id}:{filename}"
                    file_data = r.get(file_key)
                    if file_data:
                        result['files'][filename] = json.loads(file_data)
            
            return result
        except Exception as e:
            logger.warning(f"get_analysis_result 第{attempt+1}次失败: {e}")
            if attempt == 2:
                return None

def get_file_content(analysis_id, filename):
    for attempt in range(3):
        try:
            r = get_redis()
            file_key = f"file_content:{analysis_id}:{filename}"
            content = r.get(file_key)
            if content:
                return json.loads(content)
            return None
        except Exception as e:
            logger.warning(f"get_file_content 第{attempt+1}次失败: {e}")
            if attempt == 2:
                return None

def delete_analysis_result(analysis_id):
    for attempt in range(3):
        try:
            r = get_redis()
            key = f"analysis:{analysis_id}"
            data = r.get(key)
            if data:
                parsed = json.loads(data)
                temp_path = parsed.get('temp_zip_path')
                if temp_path and os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                        logger.info(f"已清理临时ZIP文件: {temp_path}")
                    except Exception as e:
                        logger.warning(f"清理临时ZIP文件失败: {temp_path} - {e}")
                file_names = parsed.get('file_names', [])
                for fname in file_names:
                    file_key = f"file_content:{analysis_id}:{fname}"
                    r.delete(file_key)
            r.delete(key)
            return
        except Exception as e:
            logger.warning(f"delete_analysis_result 第{attempt+1}次失败: {e}")
            if attempt == 2:
                return

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
    if count > 0:
        logger.info(f"启动时清理了 {count} 个临时ZIP文件")

# ========== 规则缓存 ==========
@lru_cache(maxsize=128)
def get_rules(series: str, model: str) -> List[Dict]:
    with db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('''
            SELECT r.id, r.keywords, r.advice, r.source
            FROM rules r
            JOIN models m ON r.model_id = m.id
            JOIN series s ON m.series_id = s.id
            WHERE UPPER(s.name) = UPPER(%s) AND m.name = %s
        ''', (series, model))
        rows = cur.fetchall()
        return [
            {
                'id': row['id'],
                'keywords': [kw.strip() for kw in row['keywords'].split(',') if kw.strip()],
                'advice': row['advice'],
                'source': row['source']
            }
            for row in rows
        ]

def clear_rules_cache():
    get_rules.cache_clear()

# ========== 匹配函数 ==========
def extract_line_context(text: str, index: int) -> str:
    start = text.rfind('\n', 0, index) + 1
    end = text.find('\n', index)
    if end == -1:
        end = len(text)
    return text[start:end].strip()

def extract_nearest_timestamp(text: str, index: int) -> Optional[str]:
    window = 250
    start = max(0, index - window)
    end = min(len(text), index + window)
    context = text[start:end]
    patterns = [
        r'\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2}:\d{2}',
        r'\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2}',
        r'\d{1,2}:\d{2}:\d{2}',
        r'\d{1,2}:\d{2}'
    ]
    for pattern in patterns:
        matches = list(re.finditer(pattern, context))
        if matches:
            return matches[-1].group(0)
    return None

def normalize_event_date(event_time: Optional[str]) -> str:
    if not event_time:
        return '未识别日期'
    date_only = event_time.split(' ')[0]
    if '-' in date_only or '/' in date_only:
        return date_only.replace('/', '-').strip()
    return date_only

def lookup_motor_status_by_code(board_card: str, motor_code: str, status_code: str) -> Optional[Dict]:
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT tablename FROM pg_tables WHERE tablename LIKE 'motor_status_%'")
        tables = [row[0] for row in cur.fetchall()]
        for table_name in tables:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute(f"""
                SELECT board_card, motor_code, status_code, motor_name,
                       action_type, target_value, sensor, description, full_description
                FROM {table_name}
                WHERE board_card = %s AND motor_code = %s AND status_code = %s
            """, (board_card, motor_code, status_code))
            row = cur.fetchone()
            if row:
                result = dict(row)
                diagnosis = result['full_description'] if result['full_description'] else (result['description'] + '失败/异常')
                command_parts = []
                if result['action_type']:
                    command_parts.append(result['action_type'])
                if result['target_value']:
                    command_parts.append(result['target_value'])
                if result['sensor']:
                    command_parts.append(result['sensor'])
                command_text = ' | '.join(command_parts) if command_parts else (result['description'] or diagnosis)
                db_desc = result['full_description'] or result['description'] or diagnosis
                return {
                    'type': 'motor_status_match',
                    'board_card': result['board_card'],
                    'motor_code': result['motor_code'],
                    'status_code': result['status_code'],
                    'motor_name': result['motor_name'],
                    'action_type': result['action_type'],
                    'target_value': result['target_value'],
                    'sensor': result['sensor'],
                    'description': result['description'],
                    'full_description': result['full_description'],
                    'db_match_text': db_desc,
                    'db_command': command_text,
                    'diagnosis': diagnosis,
                    'command': command_text,
                    'keywords': [f"{board_card} {motor_code} {status_code}"],
                    'advice': diagnosis,
                    'source': '电机状态表'
                }
    return None

def find_motor_status_matches(text: str) -> List[Dict]:
    matches = []
    seen = set()
    unmatched_hex = []

    long_hex_pattern = r'([A-F0-9]{12,32})'
    for match in re.finditer(long_hex_pattern, text, re.IGNORECASE):
        hex_str = match.group(1).upper()
        groups = [hex_str[i:i+2] for i in range(0, len(hex_str), 2)]
        if len(groups) >= 4:
            board = groups[0]
            motor = groups[2]
            status = groups[3]
            key = (board, motor, status, match.start())
            if key not in seen:
                seen.add(key)
                result = lookup_motor_status_by_code(board, motor, status)
                if result:
                    result['original_text'] = extract_line_context(text, match.start())
                    result['event_time'] = extract_nearest_timestamp(text, match.start()) or ''
                    result['event_date'] = normalize_event_date(result['event_time'])
                    result['raw_hex'] = hex_str
                    matches.append(result)
                else:
                    unmatched_hex.append({
                        'hex_str': hex_str,
                        'board': board,
                        'motor': motor,
                        'status': status,
                        'original_text': extract_line_context(text, match.start()),
                        'event_time': extract_nearest_timestamp(text, match.start()) or '',
                        'event_date': normalize_event_date(extract_nearest_timestamp(text, match.start()))
                    })

    patterns = [
        r'(\d{2})\s+(\d{2})\s+(\d{2})',
        r'([0-9A-Fa-f]{2})\s+([0-9A-Fa-f]{2})\s+([0-9A-Fa-f]{2})',
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            board_card, motor_code, status_code = match.group(1).upper(), match.group(2).upper(), match.group(3).upper()
            key = (board_card, motor_code, status_code, match.start())
            if key in seen:
                continue
            seen.add(key)
            result = lookup_motor_status_by_code(board_card, motor_code, status_code)
            if result:
                result['original_text'] = extract_line_context(text, match.start())
                result['event_time'] = extract_nearest_timestamp(text, match.start()) or ''
                result['event_date'] = normalize_event_date(result['event_time'])
                matches.append(result)
            else:
                unmatched_hex.append({
                    'hex_str': f'{board_card} {motor_code} {status_code}',
                    'board': board_card,
                    'motor': motor_code,
                    'status': status_code,
                    'original_text': extract_line_context(text, match.start()),
                    'event_time': extract_nearest_timestamp(text, match.start()) or '',
                    'event_date': normalize_event_date(extract_nearest_timestamp(text, match.start()))
                })

    for uh in unmatched_hex:
        matches.append({
            'type': 'motor_status_match',
            'board_card': uh['board'],
            'motor_code': uh['motor'],
            'status_code': uh['status'],
            'motor_name': '',
            'action_type': '',
            'target_value': '',
            'sensor': '',
            'description': '',
            'full_description': '',
            'db_match_text': '',
            'db_command': '',
            'diagnosis': f"未识别故障码 [{uh['hex_str']}]，板卡:{uh['board']} 电机:{uh['motor']} 状态:{uh['status']}，请补充电机状态表数据",
            'command': '',
            'keywords': [f"{uh['board']} {uh['motor']} {uh['status']}"],
            'advice': f"未识别故障码 [{uh['hex_str']}]，板卡:{uh['board']} 电机:{uh['motor']} 状态:{uh['status']}，请补充电机状态表数据",
            'source': '电机状态表(未匹配)',
            'original_text': uh['original_text'],
            'event_time': uh['event_time'],
            'event_date': uh['event_date'],
            'raw_hex': uh.get('hex_str', ''),
            'unmatched': True
        })

    return matches

def check_aspiration_anomaly(line: str, series: str = 'SMART') -> Dict:
    conditions = []
    matched = False
    file_type = 'none'
    if '样本' in line and ('空吸' in line or '取样本' in line or '取稀释样本' in line):
        file_type = 'sample'
    elif '试剂' in line and ('空吸' in line or '试剂针' in line):
        file_type = 'reagent'
    if file_type == 'none':
        return {'matched': False, 'conditions': [], 'type': 'none'}
    if '执行过重测：True' in line or '执行过重测: True' in line:
        conditions.append('执行过重测'); matched = True
    if '余量不足：True' in line or '余量不足: True' in line:
        conditions.append('余量不足'); matched = True
    if '电路异常：True' in line or '电路异常: True' in line:
        conditions.append('电路异常'); matched = True
    if '脱离液面失败：True' in line or '脱离液面失败: True' in line:
        conditions.append('脱离液面失败'); matched = True
    if '空吸：True' in line or '空吸: True' in line:
        conditions.append('空吸'); matched = True
    if '重测3次失败：True' in line or '重测3次失败: True' in line:
        conditions.append('重测3次失败'); matched = True
    if '液位探测有效：False' in line or '液位探测有效: False' in line:
        conditions.append('液位探测无效'); matched = True
    return {'matched': matched, 'conditions': conditions, 'type': file_type}


CONDITION_HIGHLIGHT_MAP = {
    '执行过重测': ['执行过重测：True', '执行过重测: True'],
    '余量不足': ['余量不足：True', '余量不足: True'],
    '电路异常': ['电路异常：True', '电路异常: True'],
    '脱离液面失败': ['脱离液面失败：True', '脱离液面失败: True'],
    '空吸': ['空吸：True', '空吸: True'],
    '重测3次失败': ['重测3次失败：True', '重测3次失败: True'],
    '液位探测无效': ['液位探测有效：False', '液位探测有效: False'],
}


def convert_go_results(go_results: list) -> list:
    """将 Go 解析结果转换为 Python 内部格式"""
    converted = []
    for item in go_results:
        if item['type'] == 'motor_status_match':
            mm = item.get('motor_match', {})
            converted.append({
                'type': 'motor_status_match',
                'board_card': mm.get('board_card', ''),
                'motor_code': mm.get('motor_code', ''),
                'status_code': mm.get('status_code', ''),
                'motor_name': mm.get('motor_name', ''),
                'action_type': mm.get('action_type', ''),
                'target_value': mm.get('target_value', ''),
                'sensor': mm.get('sensor', ''),
                'description': mm.get('description', ''),
                'full_description': mm.get('full_description', ''),
                'db_match_text': mm.get('diagnosis', ''),
                'db_command': mm.get('command', ''),
                'diagnosis': mm.get('diagnosis', ''),
                'command': mm.get('command', ''),
                'keywords': mm.get('keywords', []),
                'advice': mm.get('advice', ''),
                'source': mm.get('source', ''),
                'original_text': item.get('original_text', ''),
                'event_time': item.get('event_time', ''),
                'event_date': item.get('event_date', ''),
                'unmatched': mm.get('unmatched', False),
            })
        elif item['type'] == 'keyword_match':
            advice = item.get('advice', '')
            matched_conditions = item.get('matched_conditions', [])
            if matched_conditions and re.search(r'[\x00-\x1f]', advice):
                advice = f"检测到 {len(matched_conditions)} 个异常条件：" + '、'.join(matched_conditions)
            converted.append({
                'type': 'keyword_match',
                'keywords': item.get('keywords', []),
                'advice': advice,
                'source': item.get('source', ''),
                'original_text': item.get('original_text', ''),
                'event_time': item.get('event_time', ''),
                'event_date': item.get('event_date', ''),
                'matched_conditions': matched_conditions,
                'matched_count': item.get('matched_count', 0),
            })
    return converted

def analyze_text(text: str, rules: List[Dict], series: str = '', model: str = '', skip_motor_status: bool = False) -> List[Dict]:
    """
    分析文本，优先使用 Go 微服务，失败时回退到 Python 解析。
    """
    if not text:
        return []

    # 如果提供了 series 和 model，尝试调用 Go 服务
    if series and model and GO_PARSER_URL:
        try:
            resp = requests.post(
                GO_PARSER_URL,
                json={
                    'text': text,
                    'series': series,
                    'model': model,
                    'skip_motor_status': skip_motor_status
                },
                timeout=30
            )
            if resp.status_code == 200:
                data = resp.json()
                go_results = data.get('results', [])
                if go_results is not None:
                    return convert_go_results(go_results)
            else:
                logger.warning(f"Go 服务返回非200: {resp.status_code}")
        except Exception as e:
            logger.warning(f"Go 解析服务调用失败，回退到 Python: {e}")

    # 原有 Python 解析逻辑（保持不变）
    matched = []
    if not skip_motor_status:
        matched.extend(find_motor_status_matches(text))

    lines = text.splitlines()
    matched_lines = set()
    for ln in lines:
        result = check_aspiration_anomaly(ln)
        if result['matched']:
            keyword = '样本空吸' if result['type'] == 'sample' else '试剂空吸'
            conditions_str = '、'.join(result['conditions'])
            advice_text = f"检测到 {len(result['conditions'])} 个异常条件：{conditions_str}。建议检查样本/试剂供应情况和管路连接。"
            matched.append({
                'type': 'keyword_match',
                'keywords': [keyword],
                'advice': advice_text,
                'source': '异常条件检测',
                'original_text': ln.strip(),
                'event_time': extract_nearest_timestamp(ln, 0) or '',
                'event_date': normalize_event_date(extract_nearest_timestamp(ln, 0)),
                'matched_conditions': result['conditions'],
                'matched_count': len(result['conditions'])
            })
            matched_lines.add(ln.strip())

    text_lower = text.lower()
    for rule in rules:
        keywords = rule.get('keywords', [])
        if isinstance(keywords, str):
            keywords = [kw.strip() for kw in keywords.split(',') if kw.strip()]
        for keyword in keywords:
            start_pos = 0
            while True:
                idx = text_lower.find(keyword.lower(), start_pos)
                if idx == -1:
                    break
                original_text = extract_line_context(text, idx)
                if original_text.strip() in matched_lines:
                    start_pos = idx + 1
                    continue
                matched.append({
                    'type': 'keyword_match',
                    'keywords': [keyword],
                    'advice': rule['advice'],
                    'source': '手动规则' if rule.get('source') != 'pdf' else 'PDF知识库',
                    'original_text': original_text,
                    'event_time': extract_nearest_timestamp(text, idx) or '',
                    'event_date': normalize_event_date(extract_nearest_timestamp(text, idx))
                })
                matched_lines.add(original_text.strip())
                start_pos = idx + 1
    return matched

# ========== PDF提取函数 ==========
def extract_fault_entries(text: str) -> List[Dict]:
    text = re.sub(r'<[^>]+>', '', text)
    lines = text.splitlines()
    entries = []
    seen = set()
    i = 0
    total = len(lines)
    skip_phrases = ['版本号', '第', '页', '审核', '审批', '文件编号', '编制', '批准', '日期', '修改', '受控状态', 'SMART', '电机状态表']
    long_hex_pattern = re.compile(r'([A-F0-9]{12,32})', re.IGNORECASE)
    line_start_pattern = re.compile(r'^(?:[A-Z0-9]{6}|[0-9A-Fa-f]{2}[ \t\u00A0\u3000]+[0-9A-Fa-f]{2}[ \t\u00A0\u3000]+[0-9A-Fa-f]{2})')

    while i < total:
        line = lines[i].rstrip('\r\n').strip()
        if not line:
            i += 1
            continue
        if any(phrase in line for phrase in skip_phrases) and len(line) < 50:
            i += 1
            continue
        match = long_hex_pattern.search(line)
        if not match:
            i += 1
            continue
        hex_str = match.group(1).upper()
        board = hex_str[0:2].upper()
        motor = hex_str[2:4].upper()
        status = hex_str[4:6].upper()
        key = (board, motor, status, i, match.start())
        if key in seen:
            i += 1
            continue
        seen.add(key)
        description = line[match.end():].strip()
        description = re.sub(r'^[\s:：\-–—]+', '', description)
        j = i + 1
        while j < total:
            next_line = lines[j].strip()
            if not next_line:
                j += 1
                continue
            if long_hex_pattern.search(next_line) and line_start_pattern.match(next_line):
                break
            if line_start_pattern.match(next_line):
                break
            description += (' ' + next_line) if description else next_line
            j += 1
        i = j
        description = re.sub(r'\s+', ' ', description).strip()
        full_description = (description + '失败/异常') if description else '失败/异常'
        entry = {
            'board_card': board,
            'motor_code': motor,
            'status_code': status,
            'motor_name': '',
            'action_type': '',
            'target_value': '',
            'sensor': '',
            'description': description,
            'full_description': full_description,
            'keywords': f"{board} {motor} {status}"
        }
        entries.append(entry)
    return entries

def store_pdf_entries(entries: List[Dict], series: str, model: str) -> int:
    if not entries:
        return 0
    ensure_table_exists(model)
    table_name = get_table_name(model)
    with db_connection() as conn:
        cur = conn.cursor()
        added = 0
        for entry in entries:
            board_card = entry.get('board_card', '').strip().upper()
            motor_code = entry.get('motor_code', '').strip().upper()
            status_code = entry.get('status_code', '').strip().upper()
            if not (board_card and motor_code and status_code):
                continue
            cur.execute(f'''
                SELECT id FROM {table_name}
                WHERE board_card = %s AND motor_code = %s AND status_code = %s
            ''', (board_card, motor_code, status_code))
            if cur.fetchone():
                continue
            cur.execute(f'''
                INSERT INTO {table_name} (
                    board_card, motor_code, status_code, motor_name,
                    action_type, target_value, sensor, description,
                    full_description, source_file
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ''', (
                board_card, motor_code, status_code,
                entry.get('motor_name', ''),
                entry.get('action_type', ''),
                entry.get('target_value', ''),
                entry.get('sensor', ''),
                entry.get('description', ''),
                entry.get('full_description', ''),
                'PDF导入'
            ))
            added += 1
        conn.commit()
        return added

# ========== 文件处理函数 ==========
def sanitize_filename(filename: str) -> str:
    filename = filename.replace('\\', '/').split('/')[-1]
    dangerous_chars = ['<', '>', ':', '"', '|', '?', '*']
    for char in dangerous_chars:
        filename = filename.replace(char, '_')
    return filename if filename else 'unnamed'

def validate_input(text: str, max_length: int = 5000) -> bool:
    return bool(text) and len(text) <= max_length

def is_error_document(name: str, content: str) -> bool:
    lowered = content.lower()
    if '样本' in content and ('空吸' in content or '余量探测' in content):
        return False
    if '试剂' in content and ('空吸' in content or '余量探测' in content):
        return False
    return name.lower().endswith('.log') or any(kw in lowered for kw in ['error', 'fault', '异常', '故障', '报警'])

def filter_relevant_analysis(analysis: List[Dict]) -> List[Dict]:
    return [
        item for item in analysis
        if item['type'] in ['motor_status_match', 'keyword_match']
    ]

def _contains_keywords(content: str) -> bool:
    lower = content.lower()
    sample_kw = ['样本空吸', '样本不足']
    reagent_kw = ['试剂空吸', '试剂不足']
    fault_kw = ['error', 'fault', '异常', '故障', '报警', '失败']
    receive_kw = ['接收数据记录', '接收数据']
    if any(kw in lower for kw in sample_kw) or any(kw in lower for kw in reagent_kw) or any(kw in lower for kw in fault_kw) or any(kw in lower for kw in receive_kw):
        return True
    if '空吸' in content:
        for line in content.splitlines():
            if '空吸' in line:
                if '样本' in line or '试剂' in line:
                    return True
    return False

def _extract_file_date(fname: str, fdata: dict) -> str:
    date_match = re.search(r'(\d{4}-\d{1,2}-\d{1,2})', fname)
    if date_match:
        return date_match.group(1)
    analysis_list = fdata.get('analysis', [])
    for item in analysis_list:
        ed = item.get('event_date')
        if ed and ed != '未识别日期':
            return ed
    content = fdata.get('content', '')
    if content:
        date_match = re.search(r'(\d{4}[-/]\d{1,2}[-/]\d{1,2})', content)
        if date_match:
            return date_match.group(1)
    return '未识别日期'

def _build_date_groups(files: Dict) -> List[Dict]:
    date_map = {}
    sample_keywords = ['样本空吸', '样本不足']
    reagent_keywords = ['试剂空吸', '试剂不足']
    fault_keywords = ['error', 'fault', '异常', '故障', '报警', '失败']
    receive_keywords = ['接收数据记录', '接收数据']
    for fname, fdata in files.items():
        file_date = _extract_file_date(fname, fdata)
        if file_date not in date_map:
            date_map[file_date] = {}
        if fname not in date_map[file_date]:
            date_map[file_date][fname] = set()
        ft = fdata.get('file_type', 'unknown')
        if ft == 'sample':
            date_map[file_date][fname].add('sample')
        elif ft == 'reagent':
            date_map[file_date][fname].add('reagent')
        elif ft == 'receive':
            date_map[file_date][fname].add('receive')
        elif ft == 'fault':
            date_map[file_date][fname].add('fault')
        else:
            for item in fdata.get('analysis', []):
                if item['type'] == 'motor_status_match':
                    date_map[file_date][fname].add('fault')
                elif item['type'] == 'keyword_match':
                    for kw in item.get('keywords', []):
                        if kw in sample_keywords:
                            date_map[file_date][fname].add('sample')
                        elif kw in reagent_keywords:
                            date_map[file_date][fname].add('reagent')
                        elif kw in fault_keywords:
                            date_map[file_date][fname].add('fault')
            if not date_map[file_date][fname]:
                content = fdata.get('content', '')
                lower_content = content.lower()
                if any(kw in fname for kw in receive_keywords):
                    date_map[file_date][fname].add('receive')
                elif any(kw in fname for kw in sample_keywords):
                    date_map[file_date][fname].add('sample')
                elif any(kw in fname for kw in reagent_keywords):
                    date_map[file_date][fname].add('reagent')
                elif any(kw in fname for kw in fault_keywords):
                    date_map[file_date][fname].add('fault')
                else:
                    detected_type = None
                    for line in content.splitlines():
                        if '空吸' in line:
                            if '样本' in line:
                                detected_type = 'sample'
                                break
                            if '试剂' in line:
                                detected_type = 'reagent'
                                break
                    if detected_type:
                        date_map[file_date][fname].add(detected_type)
                    elif any(kw in lower_content for kw in [k.lower() for k in sample_keywords]):
                        date_map[file_date][fname].add('sample')
                    elif any(kw in lower_content for kw in [k.lower() for k in reagent_keywords]):
                        date_map[file_date][fname].add('reagent')
                    elif any(kw in lower_content for kw in [k.lower() for k in fault_keywords]):
                        date_map[file_date][fname].add('fault')
    date_groups = []
    for date in sorted(date_map.keys(), reverse=True):
        file_list = []
        for fname in sorted(date_map[date].keys()):
            fdata = files.get(fname, {})
            file_list.append({
                'name': fname,
                'size': fdata.get('size', 0),
                'is_critical': fdata.get('is_critical', False),
                'types': sorted(list(date_map[date][fname])),
                'has_fault': fdata.get('has_fault', False),
                'is_aspiration_file': fdata.get('is_aspiration_file', False),
                'has_aspiration_match': fdata.get('has_aspiration_match', False)
            })
        date_groups.append({'date': date, 'files': file_list})
    return date_groups

def _compute_summary(files: Dict) -> Dict:
    fault_count = 0
    sample_count = 0
    reagent_count = 0
    receive_count = 0
    sample_keywords = ['样本空吸', '样本不足']
    reagent_keywords = ['试剂空吸', '试剂不足']
    fault_keywords = ['error', 'fault', '异常', '故障', '报警', '失败']
    receive_keywords = ['接收数据记录', '接收数据']
    for fname, fdata in files.items():
        types = set()
        ft = fdata.get('file_type', 'unknown')
        if ft == 'sample':
            types.add('sample')
        elif ft == 'reagent':
            types.add('reagent')
        elif ft == 'receive':
            types.add('receive')
        elif ft == 'fault':
            types.add('fault')
        else:
            if any(kw in fname for kw in receive_keywords):
                types.add('receive')
            if any(kw in fname for kw in sample_keywords):
                types.add('sample')
            if any(kw in fname for kw in reagent_keywords):
                types.add('reagent')
            if any(kw in fname for kw in fault_keywords):
                types.add('fault')
            for item in fdata.get('analysis', []):
                if item['type'] == 'motor_status_match':
                    types.add('fault')
                elif item['type'] == 'keyword_match':
                    for kw in item.get('keywords', []):
                        if kw in sample_keywords:
                            types.add('sample')
                        elif kw in reagent_keywords:
                            types.add('reagent')
                        elif kw in fault_keywords:
                            types.add('fault')
            if not types:
                content = fdata.get('content', '')
                lower_content = content.lower()
                for line in content.splitlines():
                    if '空吸' in line:
                        if '样本' in line:
                            types.add('sample')
                        if '试剂' in line:
                            types.add('reagent')
                if any(kw in lower_content for kw in [k.lower() for k in sample_keywords]):
                    types.add('sample')
                if any(kw in lower_content for kw in [k.lower() for k in reagent_keywords]):
                    types.add('reagent')
                if any(kw in lower_content for kw in [k.lower() for k in fault_keywords]):
                    types.add('fault')
        if 'fault' in types and 'sample' not in types and 'reagent' not in types:
            fault_count += 1
        if 'sample' in types:
            sample_count += 1
        if 'reagent' in types:
            reagent_count += 1
        if 'receive' in types:
            receive_count += 1
    return {'fault': fault_count, 'sample': sample_count, 'reagent': reagent_count, 'receive': receive_count}

def is_relevant_filename(filename: str) -> bool:
    keywords = ['样本空吸', '试剂空吸', '接收数据记录', '故障代码']
    return any(kw in filename for kw in keywords)

def process_text_file_from_bytes(file_bytes: bytes, filename: str, rules: List[Dict], series: str, model: str) -> Dict:
    MAX_FILE_CONTENT = 3000000
    MAX_LINES = 300000
    content = None
    for encoding in ['utf-8', 'gbk', 'gb2312', 'latin-1']:
        try:
            content = file_bytes.decode(encoding)
            logger.info(f"文件 {filename} 使用 {encoding} 编码解码成功")
            break
        except UnicodeDecodeError:
            continue
    if content is None:
        content = file_bytes.decode('utf-8', errors='replace')
        logger.warning(f"文件 {filename} 编码解码失败，使用UTF-8替换模式")
    return _process_text_content(content, filename, rules, series, model, MAX_FILE_CONTENT, MAX_LINES)
    
def _detect_file_type(filename: str) -> str:
    if '样本空吸' in filename:
        return 'sample'
    if '试剂空吸' in filename:
        return 'reagent'
    if '接收数据记录' in filename:
        return 'receive'
    if '故障代码' in filename:
        return 'fault'
    return 'unknown'

def _process_text_content(content: str, filename: str, rules: List[Dict], series: str, model: str, MAX_FILE_CONTENT: int, MAX_LINES: int) -> Dict:
    content = ''.join(char for char in content if ord(char) >= 32 or char in '\n\r\t')
    original_content = content
    lines = original_content.splitlines()
    if len(lines) > MAX_LINES:
        content = '\n'.join(lines[:MAX_LINES]) + f'\n... (内容已截断，仅显示前{MAX_LINES}行)'
    else:
        content = original_content

    file_type = _detect_file_type(filename)

    is_aspiration_file = file_type in ('sample', 'reagent')
    is_receive_file = file_type == 'receive'
    is_fault_file = file_type == 'fault'

    # ---- 分析（使用新函数） ----
    if is_receive_file:
        analysis = []
    elif is_fault_file:
        analysis = analyze_text(original_content, rules, series, model, skip_motor_status=False)
    elif is_aspiration_file:
        analysis = analyze_text(original_content, rules, series, model, skip_motor_status=True)
    else:
        analysis = analyze_text(original_content, rules, series, model, skip_motor_status=False)

    for item in analysis:
        item['source_file'] = filename
        if file_type != 'unknown':
            item['file_type'] = file_type
    filtered_analysis = filter_relevant_analysis(analysis)
    advice_map = {}
    unmatched_map = {}
    has_aspiration = any(item['type'] == 'keyword_match' and '空吸' in str(item.get('keywords', [])) for item in filtered_analysis)
    
    # 调试日志
    logger.info(f"[DEBUG] filtered_analysis数量: {len(filtered_analysis)}")
    for i, item in enumerate(filtered_analysis[:3]):
        logger.info(f"[DEBUG] 匹配{i}: type={item.get('type')}, original_text='{item.get('original_text', '')[:50]}', advice='{item.get('advice', '')[:30]}'")
    
    # 处理所有匹配结果，包括motor_status_match和keyword_match
    for item in filtered_analysis:
        if item['type'] == 'motor_status_match':
            orig = item.get('original_text', '').strip()
            if orig and item.get('advice'):
                if item.get('unmatched'):
                    unmatched_map[orig] = item['advice']
                else:
                    advice_map[orig] = item['advice']
        elif item['type'] == 'keyword_match':
            # 对于keyword_match，使用original_text作为key
            orig = item.get('original_text', '').strip()
            if orig and item.get('advice'):
                advice_map[orig] = item['advice']
    
    highlight_keywords = []

    for item in filtered_analysis:
        if item['type'] == 'keyword_match':
            for kw in item.get('keywords', []):
                if kw and kw not in highlight_keywords:
                    highlight_keywords.append(kw)
            orig = item.get('original_text', '').strip()
            if orig and len(orig) <= 500 and orig not in highlight_keywords:
                highlight_keywords.append(orig)
            for cond in item.get('matched_conditions', []):
                for cond_text in CONDITION_HIGHLIGHT_MAP.get(cond, []):
                    if cond_text not in highlight_keywords:
                        highlight_keywords.append(cond_text)
        elif item['type'] == 'motor_status_match':
            orig = item.get('original_text', '').strip()
            if orig and len(orig) <= 500 and orig not in highlight_keywords:
                highlight_keywords.append(orig)


    # 调试日志
    logger.info(f"[DEBUG] advice_map数量: {len(advice_map)}, unmatched_map数量: {len(unmatched_map)}")
    if advice_map:
        for orig, advice in list(advice_map.items())[:2]:
            logger.info(f"[DEBUG] advice_map示例: '{orig[:50]}' -> '{advice[:30]}'")
    

    html_lines = []
    for line in content.splitlines():
        trimmed = line.strip()
        advice_html = ''
        line_has_match = False
        if trimmed in advice_map:
            advice = escape_html(advice_map[trimmed])
            if is_aspiration_file:
                advice_html = f'<div style="margin:4px 0 8px 0; padding:6px 12px; background:linear-gradient(135deg,#e8f5e9 0%,#c8e6c9 100%); border-left:3px solid #4caf50; border-radius:4px; font-size:0.82rem; color:#2e7d32;">💡 故障对比诊断：{advice}</div>'
            else:
                advice_html = f'<span style="margin-left:8px; padding:2px 8px; background:linear-gradient(135deg,#e8f5e9 0%,#c8e6c9 100%); border-left:3px solid #4caf50; border-radius:4px; font-size:0.82rem; color:#2e7d32;">💡 {advice}</span>'
            line_has_match = True
        elif trimmed in unmatched_map:
            advice = escape_html(unmatched_map[trimmed])
            if is_aspiration_file:
                advice_html = f'<div style="margin:4px 0 8px 0; padding:6px 12px; background:linear-gradient(135deg,#fff3e0 0%,#ffe0b2 100%); border-left:3px solid #ff9800; border-radius:4px; font-size:0.82rem; color:#e65100;">⚠️ {advice}</div>'
            else:
                advice_html = f'<span style="margin-left:8px; padding:2px 8px; background:linear-gradient(135deg,#fff3e0 0%,#ffe0b2 100%); border-left:3px solid #ff9800; border-radius:4px; font-size:0.82rem; color:#e65100;">⚠️ {advice}</span>'
            line_has_match = True
        else:
            for orig_key, orig_advice in advice_map.items():
                if trimmed and orig_key and (trimmed in orig_key or orig_key in trimmed):
                    advice = escape_html(orig_advice)
                    if is_aspiration_file:
                        advice_html = f'<div style="margin:4px 0 8px 0; padding:6px 12px; background:linear-gradient(135deg,#e8f5e9 0%,#c8e6c9 100%); border-left:3px solid #4caf50; border-radius:4px; font-size:0.82rem; color:#2e7d32;">💡 故障对比诊断：{advice}</div>'
                    else:
                        advice_html = f'<span style="margin-left:8px; padding:2px 8px; background:linear-gradient(135deg,#e8f5e9 0%,#c8e6c9 100%); border-left:3px solid #4caf50; border-radius:4px; font-size:0.82rem; color:#2e7d32;">💡 {advice}</span>'
                    line_has_match = True
                    break
            if not advice_html:
                for orig_key, orig_advice in unmatched_map.items():
                    if trimmed and orig_key and (trimmed in orig_key or orig_key in trimmed):
                        advice = escape_html(orig_advice)
                        if is_aspiration_file:
                            advice_html = f'<div style="margin:4px 0 8px 0; padding:6px 12px; background:linear-gradient(135deg,#fff3e0 0%,#ffe0b2 100%); border-left:3px solid #ff9800; border-radius:4px; font-size:0.82rem; color:#e65100;">⚠️ {advice}</div>'
                        else:
                            advice_html = f'<span style="margin-left:8px; padding:2px 8px; background:linear-gradient(135deg,#fff3e0 0%,#ffe0b2 100%); border-left:3px solid #ff9800; border-radius:4px; font-size:0.82rem; color:#e65100;">⚠️ {advice}</span>'
                        line_has_match = True
                        break
        if line_has_match and is_aspiration_file:
            html_lines.append(f'<div style="line-height:1.6; padding:4px 8px; background:linear-gradient(135deg,#fef9c3 0%,#fef08a 100%); border-left:3px solid #eab308; border-radius:4px; margin:2px 0;">{escape_html(line)}</div>')
        else:
            if is_aspiration_file:
                highlighted = highlight_line_text(escape_html(line), highlight_keywords)
                html_lines.append(f'<div style="line-height:1.6; padding:1px 0;">{highlighted}</div>')
            else:
                html_lines.append(f'<div style="line-height:1.6; padding:1px 0;">{escape_html(line)}{advice_html}</div>')
        if is_aspiration_file and advice_html:
            html_lines.append(advice_html)
    html_content = '\n'.join(html_lines)

    has_fault = False
    if is_fault_file:
        has_fault = True
    elif not is_aspiration_file and not is_receive_file:
        has_fault = any(item['type'] == 'motor_status_match' for item in filtered_analysis)

    file_metadata = []
    file_contents = {}
    files = {}
    file_metadata.append({
        'name': filename,
        'size': len(content),
        'is_critical': is_error_document(filename, content),
        'preview': content[:200]
    })
    file_contents[filename] = content[:50000]
    files[filename] = {
        'content': content[:MAX_FILE_CONTENT],
        'html_content': html_content,
        'has_fault': has_fault,
        'size': len(content),
        'is_critical': is_error_document(filename, content),
        'analysis': filtered_analysis,
        'file_type': file_type,
        'is_aspiration_file': is_aspiration_file,
        'has_aspiration_match': has_aspiration
    }
    return {
        'analysis': filtered_analysis,
        'file_size': len(content),
        'matched_count': len(filtered_analysis),
        'preview': content[:1000] + ('...' if len(content) > 1000 else ''),
        'file_metadata': file_metadata,
        'file_contents': file_contents,
        'files': files
    }

def process_zip_file(file, rules: List[Dict], series: str, model: str, batch_size: int = 0, start_index: int = 0) -> Dict:
    MAX_FILES = 2000
    MAX_FILE_SIZE = 5 * 1024 * 1024
    MAX_CONTENTS_MAP = 500
    MAX_FILE_CONTENT = 3000000
    MAX_LINES = 300000

    file_metadata = []
    file_contents = {}
    files = {}
    combined_analysis = []
    preview_text = ''

    with zipfile.ZipFile(file) as zf:
        all_names_raw = zf.namelist()
        name_map = {}
        for raw_name in all_names_raw:
            fixed = raw_name
            try:
                fixed = raw_name.encode('cp437').decode('gbk')
            except (UnicodeDecodeError, UnicodeEncodeError):
                try:
                    fixed = raw_name.encode('cp437').decode('utf-8')
                except (UnicodeDecodeError, UnicodeEncodeError):
                    fixed = raw_name
            name_map[raw_name] = fixed
        candidate_raw = [rn for rn, fn in name_map.items() if fn.lower().endswith(('.txt', '.log', '.md', '.csv'))]
        relevant_raw = [rn for rn in candidate_raw if is_relevant_filename(name_map[rn])]
        logger.info(f"ZIP 共 {len(all_names_raw)} 条目, 筛选出 {len(candidate_raw)} 个文本文件 (关心文件 {len(relevant_raw)} 个)")

        for raw_name in relevant_raw:
            name = name_map[raw_name]
            if len(file_metadata) >= MAX_FILES:
                logger.warning(f"已达累计处理上限 {MAX_FILES} 个文件，跳过剩余")
                break
            try:
                info = zf.getinfo(raw_name)
                if hasattr(info, 'file_size') and info.file_size > MAX_FILE_SIZE:
                    logger.info(f"跳过过大文件 ({info.file_size} 字节): {name}")
                    continue
            except Exception:
                pass
            try:
                raw = zf.read(raw_name)
                if len(raw) > MAX_FILE_SIZE:
                    logger.info(f"跳过过大文件 ({len(raw)} 字节): {name}")
                    continue
                content = None
                for encoding in ['utf-8', 'gbk', 'gb2312', 'latin-1']:
                    try:
                        content = raw.decode(encoding)
                        logger.info(f"文件 {name} 使用 {encoding} 编码解码成功")
                        break
                    except UnicodeDecodeError:
                        continue
                if content is None:
                    content = raw.decode('utf-8', errors='replace')
                    logger.warning(f"文件 {name} 编码解码失败，使用UTF-8替换模式")
                content = ''.join(char for char in content if ord(char) >= 32 or char in '\n\r\t')

                file_type = _detect_file_type(name)

                is_aspiration_file = file_type in ('sample', 'reagent')
                is_receive_file = file_type == 'receive'
                is_fault_file = file_type == 'fault'

                lines = content.splitlines()
                if len(lines) > MAX_LINES:
                    content_for_analysis = '\n'.join(lines[:MAX_LINES])
                else:
                    content_for_analysis = content

                if is_receive_file:
                    file_analysis = []
                elif file_type == 'unknown':
                    file_analysis = []
                elif is_fault_file:
                    file_analysis = analyze_text(content_for_analysis, rules, series, model, skip_motor_status=False)
                elif is_aspiration_file:
                    file_analysis = analyze_text(content_for_analysis, rules, series, model, skip_motor_status=True)
                else:
                    file_analysis = analyze_text(content_for_analysis, rules, series, model, skip_motor_status=False)

                for item in file_analysis:
                    item['source_file'] = name
                    if file_type != 'unknown':
                        item['file_type'] = file_type
                filtered_file_analysis = filter_relevant_analysis(file_analysis)

                is_relevant = file_type != 'unknown' or is_relevant_filename(name)
                has_relevant_content = len(filtered_file_analysis) > 0
                if is_relevant or has_relevant_content:
                    is_critical = is_error_document(name, content)
                    has_fault = False
                    if is_fault_file:
                        has_fault = True
                    elif not is_aspiration_file and not is_receive_file:
                        has_fault = any(item['type'] == 'motor_status_match' for item in filtered_file_analysis)
                    has_aspiration = any(item['type'] == 'keyword_match' and
                                        any(kw in ['样本空吸', '样本不足', '试剂空吸', '试剂不足']
                                            for kw in item.get('keywords', []))
                                        for item in filtered_file_analysis)

                    advice_map = {}
                    unmatched_map = {}
                    if not has_aspiration:
                        for item in filtered_file_analysis:
                            if item['type'] == 'motor_status_match':
                                orig = item.get('original_text', '').strip()
                                if orig and item.get('advice'):
                                    if item.get('unmatched'):
                                        unmatched_map[orig] = item['advice']
                                    else:
                                        advice_map[orig] = item['advice']
                    for item in filtered_file_analysis:
                        if item['type'] == 'keyword_match':
                            orig = item.get('original_text', '').strip()
                            if orig and item.get('advice'):
                                advice_map[orig] = item['advice']

                    zip_hl_kw = []
                    for item in filtered_file_analysis:
                        if item['type'] == 'keyword_match':
                            for kw in item.get('keywords', []):
                                if kw and kw not in zip_hl_kw:
                                    zip_hl_kw.append(kw)
                            orig = item.get('original_text', '').strip()
                            if orig and len(orig) <= 500 and orig not in zip_hl_kw:
                                zip_hl_kw.append(orig)
                            for cond in item.get('matched_conditions', []):
                                for cond_text in CONDITION_HIGHLIGHT_MAP.get(cond, []):
                                    if cond_text not in zip_hl_kw:
                                        zip_hl_kw.append(cond_text)
                        elif item['type'] == 'motor_status_match':
                            orig = item.get('original_text', '').strip()
                            if orig and len(orig) <= 500 and orig not in zip_hl_kw:
                                zip_hl_kw.append(orig)

                    html_lines = []
                    for line in lines[:MAX_LINES]:
                        trimmed = line.strip()
                        advice_html = ''
                        line_has_match = False
                        if trimmed in advice_map:
                            advice = escape_html(advice_map[trimmed])
                            if is_aspiration_file:
                                advice_html = f'<div style="margin:4px 0 8px 0; padding:6px 12px; background:linear-gradient(135deg,#e8f5e9 0%,#c8e6c9 100%); border-left:3px solid #4caf50; border-radius:4px; font-size:0.82rem; color:#2e7d32;">💡 故障对比诊断：{advice}</div>'
                            else:
                                advice_html = f'<span style="margin-left:8px; padding:2px 8px; background:linear-gradient(135deg,#e8f5e9 0%,#c8e6c9 100%); border-left:3px solid #4caf50; border-radius:4px; font-size:0.82rem; color:#2e7d32;">💡 {advice}</span>'
                            line_has_match = True
                        elif trimmed in unmatched_map:
                            advice = escape_html(unmatched_map[trimmed])
                            if is_aspiration_file:
                                advice_html = f'<div style="margin:4px 0 8px 0; padding:6px 12px; background:linear-gradient(135deg,#fff3e0 0%,#ffe0b2 100%); border-left:3px solid #ff9800; border-radius:4px; font-size:0.82rem; color:#e65100;">⚠️ {advice}</div>'
                            else:
                                advice_html = f'<span style="margin-left:8px; padding:2px 8px; background:linear-gradient(135deg,#fff3e0 0%,#ffe0b2 100%); border-left:3px solid #ff9800; border-radius:4px; font-size:0.82rem; color:#e65100;">⚠️ {advice}</span>'
                            line_has_match = True
                        else:
                            for orig_key, orig_advice in advice_map.items():
                                if trimmed and orig_key and (trimmed in orig_key or orig_key in trimmed):
                                    advice = escape_html(orig_advice)
                                    if is_aspiration_file:
                                        advice_html = f'<div style="margin:4px 0 8px 0; padding:6px 12px; background:linear-gradient(135deg,#e8f5e9 0%,#c8e6c9 100%); border-left:3px solid #4caf50; border-radius:4px; font-size:0.82rem; color:#2e7d32;">💡 故障对比诊断：{advice}</div>'
                                    else:
                                        advice_html = f'<span style="margin-left:8px; padding:2px 8px; background:linear-gradient(135deg,#e8f5e9 0%,#c8e6c9 100%); border-left:3px solid #4caf50; border-radius:4px; font-size:0.82rem; color:#2e7d32;">💡 {advice}</span>'
                                    line_has_match = True
                                    break
                            if not advice_html:
                                for orig_key, orig_advice in unmatched_map.items():
                                    if trimmed and orig_key and (trimmed in orig_key or orig_key in trimmed):
                                        advice = escape_html(orig_advice)
                                        if is_aspiration_file:
                                            advice_html = f'<div style="margin:4px 0 8px 0; padding:6px 12px; background:linear-gradient(135deg,#fff3e0 0%,#ffe0b2 100%); border-left:3px solid #ff9800; border-radius:4px; font-size:0.82rem; color:#e65100;">⚠️ {advice}</div>'
                                        else:
                                            advice_html = f'<span style="margin-left:8px; padding:2px 8px; background:linear-gradient(135deg,#fff3e0 0%,#ffe0b2 100%); border-left:3px solid #ff9800; border-radius:4px; font-size:0.82rem; color:#e65100;">⚠️ {advice}</span>'
                                        line_has_match = True
                                        break
                        if line_has_match and is_aspiration_file:
                            html_lines.append(f'<div style="line-height:1.6; padding:4px 8px; background:linear-gradient(135deg,#fef9c3 0%,#fef08a 100%); border-left:3px solid #eab308; border-radius:4px; margin:2px 0;">{escape_html(line)}</div>')
                        else:
                            if is_aspiration_file:
                                highlighted = highlight_line_text(escape_html(line), zip_hl_kw)
                                html_lines.append(f'<div style="line-height:1.6; padding:1px 0;">{highlighted}</div>')
                            else:
                                html_lines.append(f'<div style="line-height:1.6; padding:1px 0;">{escape_html(line)}{advice_html}</div>')
                        if is_aspiration_file and advice_html:
                            html_lines.append(advice_html)
                    html_content = '\n'.join(html_lines)

                    file_metadata.append({
                        'name': name,
                        'size': len(content),
                        'is_critical': is_critical,
                        'preview': content[:200]
                    })
                    if len(file_contents) < MAX_CONTENTS_MAP:
                        file_contents[name] = content[:50000]
                    if len(files) < MAX_CONTENTS_MAP:
                        files[name] = {
                            'content': content[:MAX_FILE_CONTENT],
                            'html_content': html_content,
                            'has_fault': has_fault,
                            'size': len(content),
                            'is_critical': is_critical,
                            'analysis': filtered_file_analysis,
                            'file_type': file_type,
                            'is_aspiration_file': is_aspiration_file,
                            'has_aspiration_match': has_aspiration
                        }
                combined_analysis.extend(filtered_file_analysis)
                if len(preview_text) < 1000:
                    preview_text += content[:1000 - len(preview_text)]
            except Exception as e:
                logger.warning(f"读取文件失败: {name} - {e}")
                continue

    return {
        'analysis': combined_analysis,
        'file_metadata': file_metadata[:200],
        'file_contents': file_contents,
        'files': files,
        'total_files': len(file_metadata),
        'matched_count': len(combined_analysis),
        'preview': preview_text + ('...' if len(preview_text) >= 1000 else ''),
        'has_more_files': False,
        'next_index': 0,
        'total_candidates': len(relevant_raw),
    }


def process_rar_file(file_path, rules: List[Dict], series: str, model: str) -> Dict:
    MAX_FILES = 2000
    MAX_FILE_SIZE = 5 * 1024 * 1024
    MAX_CONTENTS_MAP = 500
    MAX_FILE_CONTENT = 3000000
    MAX_LINES = 300000

    file_metadata = []
    file_contents = {}
    files = {}
    combined_analysis = []
    preview_text = ''

    with rarfile.RarFile(file_path) as rf:
        all_names_raw = rf.namelist()
        name_map = {}
        for raw_name in all_names_raw:
            try:
                raw_name.encode('cp437').decode('gbk')
                name_map[raw_name] = raw_name
            except (UnicodeDecodeError, UnicodeEncodeError):
                name_map[raw_name] = raw_name
        candidate_raw = [rn for rn, fn in name_map.items() if fn.lower().endswith(('.txt', '.log', '.md', '.csv'))]
        relevant_raw = [rn for rn in candidate_raw if is_relevant_filename(name_map[rn])]
        logger.info(f"RAR 共 {len(all_names_raw)} 条目, 筛选出 {len(candidate_raw)} 个文本文件 (关心文件 {len(relevant_raw)} 个)")

        for raw_name in relevant_raw:
            name = name_map[raw_name]
            if len(file_metadata) >= MAX_FILES:
                logger.warning(f"已达累计处理上限 {MAX_FILES} 个文件，跳过剩余")
                break
            try:
                info = rf.getinfo(raw_name)
                if hasattr(info, 'file_size') and info.file_size > MAX_FILE_SIZE:
                    logger.info(f"跳过过大文件 ({info.file_size} 字节): {name}")
                    continue
            except Exception:
                pass
            try:
                raw = rf.read(raw_name)
                if len(raw) > MAX_FILE_SIZE:
                    logger.info(f"跳过过大文件 ({len(raw)} 字节): {name}")
                    continue
                content = None
                for encoding in ['utf-8', 'gbk', 'gb2312', 'latin-1']:
                    try:
                        content = raw.decode(encoding)
                        logger.info(f"文件 {name} 使用 {encoding} 编码解码成功")
                        break
                    except UnicodeDecodeError:
                        continue
                if content is None:
                    content = raw.decode('utf-8', errors='replace')
                    logger.warning(f"文件 {name} 编码解码失败，使用UTF-8替换模式")
                content = ''.join(char for char in content if ord(char) >= 32 or char in '\n\r\t')

                file_type = _detect_file_type(name)

                is_aspiration_file = file_type in ('sample', 'reagent')
                is_receive_file = file_type == 'receive'
                is_fault_file = file_type == 'fault'

                lines = content.splitlines()
                if len(lines) > MAX_LINES:
                    content_for_analysis = '\n'.join(lines[:MAX_LINES])
                else:
                    content_for_analysis = content

                if is_receive_file:
                    file_analysis = []
                elif file_type == 'unknown':
                    file_analysis = []
                elif is_fault_file:
                    file_analysis = analyze_text(content_for_analysis, rules, series, model, skip_motor_status=False)
                elif is_aspiration_file:
                    file_analysis = analyze_text(content_for_analysis, rules, series, model, skip_motor_status=True)
                else:
                    file_analysis = analyze_text(content_for_analysis, rules, series, model, skip_motor_status=False)

                for item in file_analysis:
                    item['source_file'] = name
                    if file_type != 'unknown':
                        item['file_type'] = file_type
                filtered_file_analysis = filter_relevant_analysis(file_analysis)

                is_relevant = file_type != 'unknown' or is_relevant_filename(name)
                has_relevant_content = len(filtered_file_analysis) > 0
                if is_relevant or has_relevant_content:
                    is_critical = is_error_document(name, content)
                    has_fault = False
                    if is_fault_file:
                        has_fault = True
                    elif not is_aspiration_file and not is_receive_file:
                        has_fault = any(item['type'] == 'motor_status_match' for item in filtered_file_analysis)
                    has_aspiration = any(item['type'] == 'keyword_match' and
                                        any(kw in ['样本空吸', '样本不足', '试剂空吸', '试剂不足']
                                            for kw in item.get('keywords', []))
                                        for item in filtered_file_analysis)

                    advice_map = {}
                    unmatched_map = {}
                    if not has_aspiration:
                        for item in filtered_file_analysis:
                            if item['type'] == 'motor_status_match':
                                orig = item.get('original_text', '').strip()
                                if orig and item.get('advice'):
                                    if item.get('unmatched'):
                                        unmatched_map[orig] = item['advice']
                                    else:
                                        advice_map[orig] = item['advice']
                    for item in filtered_file_analysis:
                        if item['type'] == 'keyword_match':
                            orig = item.get('original_text', '').strip()
                            if orig and item.get('advice'):
                                advice_map[orig] = item['advice']

                    rar_hl_kw = []
                    for item in filtered_file_analysis:
                        if item['type'] == 'keyword_match':
                            for kw in item.get('keywords', []):
                                if kw and kw not in rar_hl_kw:
                                    rar_hl_kw.append(kw)
                            orig = item.get('original_text', '').strip()
                            if orig and len(orig) <= 500 and orig not in rar_hl_kw:
                                rar_hl_kw.append(orig)
                            for cond in item.get('matched_conditions', []):
                                for cond_text in CONDITION_HIGHLIGHT_MAP.get(cond, []):
                                    if cond_text not in rar_hl_kw:
                                        rar_hl_kw.append(cond_text)
                        elif item['type'] == 'motor_status_match':
                            orig = item.get('original_text', '').strip()
                            if orig and len(orig) <= 500 and orig not in rar_hl_kw:
                                rar_hl_kw.append(orig)

                    html_lines = []
                    for line in lines[:MAX_LINES]:
                        trimmed = line.strip()
                        advice_html = ''
                        line_has_match = False
                        if trimmed in advice_map:
                            advice = escape_html(advice_map[trimmed])
                            if is_aspiration_file:
                                advice_html = f'<div style="margin:4px 0 8px 0; padding:6px 12px; background:linear-gradient(135deg,#e8f5e9 0%,#c8e6c9 100%); border-left:3px solid #4caf50; border-radius:4px; font-size:0.82rem; color:#2e7d32;">💡 故障对比诊断：{advice}</div>'
                            else:
                                advice_html = f'<span style="margin-left:8px; padding:2px 8px; background:linear-gradient(135deg,#e8f5e9 0%,#c8e6c9 100%); border-left:3px solid #4caf50; border-radius:4px; font-size:0.82rem; color:#2e7d32;">💡 {advice}</span>'
                            line_has_match = True
                        elif trimmed in unmatched_map:
                            advice = escape_html(unmatched_map[trimmed])
                            if is_aspiration_file:
                                advice_html = f'<div style="margin:4px 0 8px 0; padding:6px 12px; background:linear-gradient(135deg,#fff3e0 0%,#ffe0b2 100%); border-left:3px solid #ff9800; border-radius:4px; font-size:0.82rem; color:#e65100;">⚠️ {advice}</div>'
                            else:
                                advice_html = f'<span style="margin-left:8px; padding:2px 8px; background:linear-gradient(135deg,#fff3e0 0%,#ffe0b2 100%); border-left:3px solid #ff9800; border-radius:4px; font-size:0.82rem; color:#e65100;">⚠️ {advice}</span>'
                            line_has_match = True
                        else:
                            for orig_key, orig_advice in advice_map.items():
                                if trimmed and orig_key and (trimmed in orig_key or orig_key in trimmed):
                                    advice = escape_html(orig_advice)
                                    if is_aspiration_file:
                                        advice_html = f'<div style="margin:4px 0 8px 0; padding:6px 12px; background:linear-gradient(135deg,#e8f5e9 0%,#c8e6c9 100%); border-left:3px solid #4caf50; border-radius:4px; font-size:0.82rem; color:#2e7d32;">💡 故障对比诊断：{advice}</div>'
                                    else:
                                        advice_html = f'<span style="margin-left:8px; padding:2px 8px; background:linear-gradient(135deg,#e8f5e9 0%,#c8e6c9 100%); border-left:3px solid #4caf50; border-radius:4px; font-size:0.82rem; color:#2e7d32;">💡 {advice}</span>'
                                    line_has_match = True
                                    break
                            if not advice_html:
                                for orig_key, orig_advice in unmatched_map.items():
                                    if trimmed and orig_key and (trimmed in orig_key or orig_key in trimmed):
                                        advice = escape_html(orig_advice)
                                        if is_aspiration_file:
                                            advice_html = f'<div style="margin:4px 0 8px 0; padding:6px 12px; background:linear-gradient(135deg,#fff3e0 0%,#ffe0b2 100%); border-left:3px solid #ff9800; border-radius:4px; font-size:0.82rem; color:#e65100;">⚠️ {advice}</div>'
                                        else:
                                            advice_html = f'<span style="margin-left:8px; padding:2px 8px; background:linear-gradient(135deg,#fff3e0 0%,#ffe0b2 100%); border-left:3px solid #ff9800; border-radius:4px; font-size:0.82rem; color:#e65100;">⚠️ {advice}</span>'
                                        line_has_match = True
                                        break
                        if line_has_match and is_aspiration_file:
                            html_lines.append(f'<div style="line-height:1.6; padding:4px 8px; background:linear-gradient(135deg,#fef9c3 0%,#fef08a 100%); border-left:3px solid #eab308; border-radius:4px; margin:2px 0;">{escape_html(line)}</div>')
                        else:
                            if is_aspiration_file:
                                highlighted = highlight_line_text(escape_html(line), rar_hl_kw)
                                html_lines.append(f'<div style="line-height:1.6; padding:1px 0;">{highlighted}</div>')
                            else:
                                html_lines.append(f'<div style="line-height:1.6; padding:1px 0;">{escape_html(line)}{advice_html}</div>')
                        if is_aspiration_file and advice_html:
                            html_lines.append(advice_html)
                    html_content = '\n'.join(html_lines)

                    file_metadata.append({
                        'name': name,
                        'size': len(content),
                        'is_critical': is_critical,
                        'preview': content[:200]
                    })
                    if len(file_contents) < MAX_CONTENTS_MAP:
                        file_contents[name] = content[:50000]
                    if len(files) < MAX_CONTENTS_MAP:
                        files[name] = {
                            'content': content[:MAX_FILE_CONTENT],
                            'html_content': html_content,
                            'has_fault': has_fault,
                            'size': len(content),
                            'is_critical': is_critical,
                            'analysis': filtered_file_analysis,
                            'file_type': file_type,
                            'is_aspiration_file': is_aspiration_file,
                            'has_aspiration_match': has_aspiration
                        }
                combined_analysis.extend(filtered_file_analysis)
                if len(preview_text) < 1000:
                    preview_text += content[:1000 - len(preview_text)]
            except Exception as e:
                logger.warning(f"读取文件失败: {name} - {e}")
                continue

    return {
        'analysis': combined_analysis,
        'file_metadata': file_metadata[:200],
        'file_contents': file_contents,
        'files': files,
        'total_files': len(file_metadata),
        'matched_count': len(combined_analysis),
        'preview': preview_text + ('...' if len(preview_text) >= 1000 else ''),
        'has_more_files': False,
        'next_index': 0,
        'total_candidates': len(relevant_raw),
    }

# ========== 试剂制冷排查规则 ==========
REAGENT_COOLING_RULES = [
    {'keywords': ['温度异常', '温度超限', '温度失控'], 'advice': '🔧 排查：检查温度传感器、TEC制冷片供电', 'source': '制冷排查'},
    {'keywords': ['TEC', 'tec', '珀尔帖', '制冷片'], 'advice': '🔧 排查：检查TEC制冷片工作状态、驱动电流是否正常', 'source': '制冷排查'},
    {'keywords': ['制冷', '冷端', '散热'], 'advice': '🔧 排查：检查制冷模块散热器、风扇是否正常运转', 'source': '制冷排查'},
    {'keywords': ['试剂温度', '试剂制冷'], 'advice': '🔧 排查：检查试剂仓制冷模块、温度传感器校准', 'source': '制冷排查'},
    {'keywords': ['过热', '过温', '高温报警'], 'advice': '🔧 排查：检查制冷系统是否失效、散热通道是否堵塞', 'source': '制冷排查'},
    {'keywords': ['ADC', 'adc', 'NTC', 'ntc'], 'advice': '🔧 排查：检查温度采集ADC/NTC传感器读数是否异常', 'source': '制冷排查'},
    {'keywords': ['PID', 'pid'], 'advice': '🔧 排查：检查温度控制PID参数是否异常', 'source': '制冷排查'},
]

# ========== 认证装饰器 ==========
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated

def api_login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return jsonify({'error': '请先登录管理员'}), 401
        return f(*args, **kwargs)
    return decorated

def api_super_admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('super_admin_logged_in'):
            return jsonify({'error': '需要高等级管理员权限'}), 403
        return f(*args, **kwargs)
    return decorated

# ========== API路由 ==========
@app.route('/metrics', methods=['GET'])
def metrics_endpoint():
    """手动添加的metrics端点"""
    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
    from flask import Response
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)

@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'version': '3.0'
    })

def _bug_table(model):
    m = re.sub(r'[^a-zA-Z0-9_]', '', model.lower())
    if not m:
        return None
    tbl = f'software_bugs_{m}'
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass(%s)", (tbl,))
        if not cur.fetchone()[0]:
            return None
    return tbl


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
    print(f"🔐 管理员密码: {Config.ADMIN_PASSWORD}")
    print(f"🌐 访问地址: http://localhost:8081")
    print(f"🔧 管理后台: http://localhost:8081/admin/rules")
    print("="*60 + "\n")
    from waitress import serve
    serve(app, host='0.0.0.0', port=8081, threads=4, channel_timeout=300, max_request_body_size=209715200)