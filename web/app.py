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
from dotenv import load_dotenv
import redis
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool
from contextlib import contextmanager
import requests
GO_PARSER_URL = os.getenv('GO_PARSER_URL', 'http://localhost:8082/parse')

# ========== 加载环境变量 ==========
load_dotenv()

# ========== 配置 ==========
class Config:
    SECRET_KEY = os.getenv('SECRET_KEY', 'ivd-secret-key-2026')
    ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'admin123')
    DB_HOST = os.getenv('DB_HOST', 'localhost')
    DB_PORT = int(os.getenv('DB_PORT', '5432'))
    DB_USER = os.getenv('DB_USER', 'ivd_user')
    DB_PASSWORD = os.getenv('DB_PASSWORD', 'ivd_pass')
    DB_NAME = os.getenv('DB_NAME', 'ivd_fault_db')
    REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
    MAX_CONTENT_LENGTH = 200 * 1024 * 1024
    DEBUG = os.getenv('DEBUG', 'False').lower() == 'true'
    ANALYSIS_TTL_HOURS = int(os.getenv('ANALYSIS_TTL_HOURS', '2'))

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

# ========== Flask应用初始化 ==========
app = Flask(__name__)
app.config.from_object(Config)
app.secret_key = Config.SECRET_KEY
app.config['MAX_CONTENT_LENGTH'] = Config.MAX_CONTENT_LENGTH

# ========== 数据库连接池 ==========
_pool = None

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

def get_redis():
    global _redis_client
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

def get_table_name(model_name: str) -> str:
    clean_name = re.sub(r'[^a-zA-Z0-9_]', '', model_name)
    if not clean_name:
        raise ValueError(f"无效的型号名称: {model_name!r}，过滤后为空")
    return f"motor_status_{clean_name}"

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
    """初始化 PostgreSQL 表结构并插入默认数据（如无数据）"""
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
        conn.commit()
    # 插入默认数据（如果不存在）
    init_default_data()

def init_default_data():
    with db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        # 检查是否已有系列
        cur.execute("SELECT id, name FROM series")
        if cur.fetchone():
            return  # 已有数据，不重复插入

        # 插入系列
        series_data = ['SMART', 'Venus']
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
            ('Venus', 'VENUS100', '试剂空', '🔧 更换试剂，检查液位电路'),
            ('Venus', 'VENUS500', '通讯失败', '🔧 检查线缆、重启设备'),
            ('Venus', 'VENUS9000', '结果异常', '🔧 执行质控、清洁光学系统'),
            ('Venus', 'VENUS9900', '卡杯', '🔧 检查清洗针、泵阀'),
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
            WHERE s.name = %s AND m.name = %s
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
        if item['type'] == 'motor_status_match' or
           (item['type'] == 'keyword_match' and item.get('keywords') and any(kw in item['keywords'] for kw in ['样本空吸', '样本不足', '试剂空吸', '试剂不足']))
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
    if is_receive_file or file_type == 'unknown':
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
    if not has_aspiration:
        for item in filtered_analysis:
            if item['type'] == 'motor_status_match':
                orig = item.get('original_text', '').strip()
                if orig and item.get('advice'):
                    if item.get('unmatched'):
                        unmatched_map[orig] = item['advice']
                    else:
                        advice_map[orig] = item['advice']

    html_lines = []
    for line in content.splitlines():
        trimmed = line.strip()
        advice_html = ''
        if trimmed in advice_map:
            advice = escape_html(advice_map[trimmed])
            advice_html = f'<span style="margin-left:12px; padding:2px 8px; background:#e8f5e9; border-left:2px solid #4caf50; border-radius:2px; font-size:0.8rem; color:#2e7d32;">💡 {advice}</span>'
        elif trimmed in unmatched_map:
            advice = escape_html(unmatched_map[trimmed])
            advice_html = f'<span style="margin-left:12px; padding:2px 8px; background:#fff3e0; border-left:2px solid #ff9800; border-radius:2px; font-size:0.8rem; color:#e65100;">⚠️ {advice}</span>'
        else:
            for orig_key, orig_advice in advice_map.items():
                if trimmed and orig_key and (trimmed in orig_key or orig_key in trimmed):
                    advice = escape_html(orig_advice)
                    advice_html = f'<span style="margin-left:12px; padding:2px 8px; background:#e8f5e9; border-left:2px solid #4caf50; border-radius:2px; font-size:0.8rem; color:#2e7d32;">💡 {advice}</span>'
                    break
            if not advice_html:
                for orig_key, orig_advice in unmatched_map.items():
                    if trimmed and orig_key and (trimmed in orig_key or orig_key in trimmed):
                        advice = escape_html(orig_advice)
                        advice_html = f'<span style="margin-left:12px; padding:2px 8px; background:#fff3e0; border-left:2px solid #ff9800; border-radius:2px; font-size:0.8rem; color:#e65100;">⚠️ {advice}</span>'
                        break
        html_lines.append(
            f'<div style="line-height:1.5;">{escape_html(line)}{advice_html}</div>'
        )
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

                    html_lines = []
                    for line in lines[:MAX_LINES]:
                        trimmed = line.strip()
                        advice_html = ''
                        if trimmed in advice_map:
                            advice = escape_html(advice_map[trimmed])
                            advice_html = f'<span style="display:inline-block; margin-left:20px; padding:4px 10px; background:#e8f5e9; border-left:3px solid #4caf50; border-radius:3px; font-size:0.85rem; color:#2e7d32; vertical-align:middle;">💡 故障对比诊断：{advice}</span>'
                        elif trimmed in unmatched_map:
                            advice = escape_html(unmatched_map[trimmed])
                            advice_html = f'<span style="display:inline-block; margin-left:20px; padding:4px 10px; background:#fff3e0; border-left:3px solid #ff9800; border-radius:3px; font-size:0.85rem; color:#e65100; vertical-align:middle;">⚠️ {advice}</span>'
                        else:
                            for orig_key, orig_advice in advice_map.items():
                                if trimmed and orig_key and (trimmed in orig_key or orig_key in trimmed):
                                    advice = escape_html(orig_advice)
                                    advice_html = f'<span style="display:inline-block; margin-left:20px; padding:4px 10px; background:#e8f5e9; border-left:3px solid #4caf50; border-radius:3px; font-size:0.85rem; color:#2e7d32; vertical-align:middle;">💡 故障对比诊断：{advice}</span>'
                                    break
                            if not advice_html:
                                for orig_key, orig_advice in unmatched_map.items():
                                    if trimmed and orig_key and (trimmed in orig_key or orig_key in trimmed):
                                        advice = escape_html(orig_advice)
                                        advice_html = f'<span style="display:inline-block; margin-left:20px; padding:4px 10px; background:#fff3e0; border-left:3px solid #ff9800; border-radius:3px; font-size:0.85rem; color:#e65100; vertical-align:middle;">⚠️ {advice}</span>'
                                        break
                        html_lines.append(
                            f'<div class="line-with-advice" style="display:flex; align-items:center; margin-bottom:2px;"><span class="line-content" style="flex:0 1 auto;">{escape_html(line)}</span>{advice_html}</div>'
                        )
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

# ========== API路由 ==========
@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'version': '3.0.0'
    })

@app.route('/api/series', methods=['GET'])
def get_series():
    with db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('SELECT id, name FROM series ORDER BY name')
        rows = cur.fetchall()
        return jsonify([dict(row) for row in rows])

@app.route('/api/models', methods=['GET'])
def get_models():
    series_name = request.args.get('series', '')
    if not series_name:
        return jsonify([])
    with db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('''
            SELECT m.id, m.name
            FROM models m
            JOIN series s ON m.series_id = s.id
            WHERE s.name = %s
            ORDER BY m.name
        ''', (series_name,))
        rows = cur.fetchall()
        return jsonify([dict(row) for row in rows])

@app.route('/api/rules', methods=['GET'])
def get_rules_api():
    series = request.args.get('series', '')
    model = request.args.get('model', '')
    if not series or not model:
        return jsonify([])
    rules = get_rules(series, model)
    return jsonify(rules)

@app.route('/api/rules', methods=['POST'])
@api_login_required
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
    with db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('SELECT id FROM series WHERE name = %s', (series,))
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

@app.route('/api/rules/<int:rule_id>', methods=['PUT'])
@api_login_required
def update_rule_api(rule_id):
    data = request.json
    keywords = data.get('keywords', '').strip()
    advice = data.get('advice', '').strip()
    if not all([keywords, advice]):
        return jsonify({'error': '请填写完整信息'}), 400
    with db_connection() as conn:
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

@app.route('/api/rules/<int:rule_id>', methods=['DELETE'])
@api_login_required
def delete_rule_api(rule_id):
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute('DELETE FROM rules WHERE id = %s', (rule_id,))
        if cur.rowcount == 0:
            return jsonify({'error': '规则不存在'}), 404
        conn.commit()
        clear_rules_cache()
        logger.info(f"删除规则 ID:{rule_id}")
        return jsonify({'success': True})

@app.route('/api/motor_status', methods=['GET'])
def get_motor_status():
    model = request.args.get('model', '')
    limit = request.args.get('limit', 100, type=int)
    offset = request.args.get('offset', 0, type=int)
    if not model:
        return jsonify({'total': 0, 'data': [], 'limit': limit, 'offset': offset, 'model': ''})
    table_name = get_table_name(model)
    with db_connection() as conn:
        cur = conn.cursor()
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
        columns = ['id', 'board_card', 'motor_code', 'status_code', 'motor_name',
                   'action_type', 'target_value', 'sensor', 'description', 'full_description']
        data = [dict(zip(columns, row)) for row in rows]
        return jsonify({
            'total': total,
            'data': data,
            'limit': limit,
            'offset': offset,
            'model': model
        })

@app.route('/api/motor_status/clear', methods=['DELETE'])
@api_login_required
def clear_motor_status():
    model = request.args.get('model', '')
    if not model:
        return jsonify({'error': '请指定型号'}), 400
    table_name = get_table_name(model)
    with db_connection() as conn:
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

@app.route('/api/analyze', methods=['POST'])
def analyze_file():
    series = request.form.get('series', '')
    model = request.form.get('model', '')
    analysis_type = request.form.get('analysis_type', '')  # 保留特殊类型
    if not series or not model:
        return jsonify({'error': '请选择设备系列和型号'}), 400

    files_to_process = []
    if 'file' in request.files and request.files['file'].filename:
        files_to_process = [request.files['file']]
    elif 'files' in request.files:
        files_list = request.files.getlist('files')
        files_to_process = [f for f in files_list if f.filename]
    if not files_to_process:
        return jsonify({'error': '未上传文件'}), 400

    # 保存文件到临时目录（传递给 Celery Worker）
    temp_dir = tempfile.mkdtemp(prefix='ivd_upload_')
    file_paths = []
    for f in files_to_process:
        filename = sanitize_filename(f.filename)
        path = os.path.join(temp_dir, filename)
        f.save(path)
        file_paths.append(path)

    wsl_paths = [convert_to_wsl_path(p) for p in file_paths]

    from tasks import analyze_files_task
    task = analyze_files_task.delay(wsl_paths, series, model, analysis_type)
    analysis_id = task.id   # 使用 Celery 任务 ID 作为唯一标识

    # 立即返回，让前端轮询
    return jsonify({
        'status': 'accepted',
        'analysis_id': analysis_id,
        'task_id': task.id,
        'poll_url': f'/api/task_status/{analysis_id}',
        'redirect_url': f'/analysis/{analysis_id}',
        'message': '分析任务已提交，请轮询状态'
    })

@app.route('/api/task_status/<analysis_id>', methods=['GET'])
def task_status(analysis_id):
    from celery.result import AsyncResult
    from celery_app import celery
    data = get_analysis_result(analysis_id)
    if data:
        return jsonify({
            'status': 'completed',
            'redirect_url': f'/analysis/{analysis_id}',
            'total_dates': data.get('total_dates', 0),
            'total_files': data.get('total_files', 0),
            'summary': data.get('summary', {}),
        })
    res = AsyncResult(analysis_id, app=celery)
    if res.ready():
        if res.successful():
            data = get_analysis_result(analysis_id)
            if data:
                return jsonify({
                    'status': 'completed',
                    'redirect_url': f'/analysis/{analysis_id}',
                    'total_dates': data.get('total_dates', 0),
                    'total_files': data.get('total_files', 0),
                    'summary': data.get('summary', {}),
                })
            return jsonify({'status': 'completed', 'redirect_url': f'/analysis/{analysis_id}'})
        else:
            return jsonify({'status': 'failed', 'error': str(res.info)}), 500
    state = res.state
    meta = res.info if isinstance(res.info, dict) else {}
    return jsonify({'status': 'pending', 'state': state, 'progress': meta}), 202

@app.route('/analysis/<analysis_id>')
def analysis_view(analysis_id):
    data = get_analysis_result(analysis_id)
    if data:
        import json as _json
        lightweight = {
            'series': data.get('series', ''),
            'model': data.get('model', ''),
            'analysis_type': data.get('analysis_type', ''),
            'file_name': data.get('file_name', ''),
            'analyzed_at': data.get('analyzed_at', ''),
            'date_groups': data.get('date_groups', []),
            'total_dates': data.get('total_dates', 0),
            'total_files': data.get('total_files', 0),
            'summary': data.get('summary', {}),
            'has_more_files': data.get('has_more_files', False),
            'zip_total_candidates': data.get('zip_total_candidates', 0),
            'zip_processed': data.get('zip_processed', 0),
        }
        embedded_data = _json.dumps(lightweight, ensure_ascii=False).replace('</script', '<\\/script').replace('<!--', '<\\!--')
        return render_template_string(ANALYSIS_HTML, analysis_id=analysis_id, embedded_data=embedded_data)
    try:
        from celery.result import AsyncResult
        from celery_app import celery
        res = AsyncResult(analysis_id, app=celery)
        if res.state == 'FAILURE':
            return '''
            <!DOCTYPE html>
            <html>
            <head><meta charset="UTF-8"><title>分析失败</title></head>
            <body style="font-family:system-ui;text-align:center;padding:50px;">
                <h2>❌ 分析任务失败</h2>
                <p>''' + str(res.info) + '''</p>
                <a href="/" style="color:#1e6f9f;">← 返回上传页面</a>
            </body>
            </html>
            ''', 500
    except Exception:
        pass
    return '''
    <!DOCTYPE html>
    <html>
    <head><meta charset="UTF-8"><title>分析进行中</title>
    <style>
        .loader { width:48px;height:48px;border:5px solid #e5e7eb;border-top-color:#2563eb;border-radius:50%;animation:spin 1s linear infinite;margin:30px auto; }
        @keyframes spin { to { transform:rotate(360deg); } }
    </style>
    <script>
        let pollCount = 0;
        function pollStatus() {
            pollCount++;
            fetch('/api/task_status/''' + analysis_id + '''')
                .then(r => r.json())
                .then(d => {
                    if (d.status === 'completed' && d.redirect_url) {
                        window.location.href = d.redirect_url;
                    } else if (d.status === 'failed') {
                        document.getElementById('statusText').textContent = '分析失败: ' + (d.error || '未知错误');
                        document.getElementById('statusText').style.color = '#ef4444';
                        document.querySelector('.loader').style.display = 'none';
                    } else {
                        document.getElementById('statusText').textContent = '正在智能分析中... (已等待 ' + pollCount + ' 秒)';
                        setTimeout(pollStatus, 1000);
                    }
                })
                .catch(() => { setTimeout(pollStatus, 2000); });
        }
        setTimeout(pollStatus, 1000);
    </script>
    </head>
    <body style="font-family:system-ui;text-align:center;padding:50px;">
        <div class="loader"></div>
        <h2 id="statusText">正在智能分析中...</h2>
        <p style="color:#6b7280;margin-top:10px;">大文件分析可能需要较长时间，请耐心等待</p>
        <a href="/" style="color:#1e6f9f;display:inline-block;margin-top:20px;">← 返回上传页面</a>
    </body>
    </html>
    '''

@app.route('/api/analysis/<analysis_id>')
def get_analysis_data(analysis_id):
    data = get_analysis_result(analysis_id)
    if not data:
        return jsonify({'error': '分析结果不存在或已过期'}), 404
    return jsonify({
        'series': data['series'],
        'model': data['model'],
        'analysis_type': data.get('analysis_type', ''),
        'file_name': data['file_name'],
        'analyzed_at': data['analyzed_at'],
        'date_groups': data['date_groups'],
        'total_dates': data['total_dates'],
        'total_files': data['total_files'],
        'summary': data['summary'],
        'has_more_files': data.get('zip_has_more', False),
        'zip_total_candidates': data.get('zip_total_candidates', 0),
        'zip_processed': len(data.get('files', {})),
    })

@app.route('/api/analysis/<analysis_id>/load-more', methods=['POST'])
def load_more_files(analysis_id):
    data = get_analysis_result(analysis_id)
    if not data:
        return jsonify({'error': '分析结果不存在或已过期'}), 404
    if not data.get('zip_has_more'):
        return jsonify({'error': '没有更多文件需要加载', 'has_more_files': False}), 400

    temp_path = data.get('temp_zip_path')
    if not temp_path or not os.path.exists(temp_path):
        data['zip_has_more'] = False
        store_analysis_result(analysis_id, data)
        return jsonify({'error': '临时ZIP文件已清理，无法继续加载'}), 410

    try:
        rules = get_rules(data['series'], data['model'])
        next_index = data.get('zip_next_index', 100)
        with open(temp_path, 'rb') as f:
            batch_result = process_zip_file(f, rules, data['series'], data['model'], batch_size=500, start_index=next_index)

        new_files = batch_result.get('files', {})
        existing_files = data['files']
        existing_files.update(new_files)

        new_date_groups = _build_date_groups(existing_files)
        new_summary = _compute_summary(existing_files)

        data['files'] = existing_files
        data['date_groups'] = new_date_groups
        data['total_dates'] = len(new_date_groups)
        data['total_files'] = len(existing_files)
        data['summary'] = new_summary
        data['zip_next_index'] = batch_result.get('next_index', next_index + 100)
        data['zip_has_more'] = batch_result.get('has_more_files', False)

        store_analysis_result(analysis_id, data)

        return jsonify({
            'success': True,
            'new_files': len(new_files),
            'total_files_now': len(existing_files),
            'total_dates_now': len(new_date_groups),
            'date_groups': new_date_groups,
            'summary': new_summary,
            'has_more_files': batch_result.get('has_more_files', False),
            'zip_processed': len(existing_files),
            'zip_total_candidates': data.get('zip_total_candidates', 0),
        })
    except Exception as e:
        logger.error(f"分批加载失败: {analysis_id} - {e}", exc_info=True)
        return jsonify({'error': f'加载失败: {str(e)}'}), 500

@app.route('/api/analysis/<analysis_id>/file')
def get_analysis_file(analysis_id):
    filename = request.args.get('name', '')
    if not filename:
        return jsonify({'error': '请指定文件名'}), 400
    
    file_data = get_file_content(analysis_id, filename)
    
    if file_data:
        return jsonify(file_data)
    
    data = get_analysis_result(analysis_id)
    if not data:
        return jsonify({'error': '分析结果不存在或已过期'}), 404
    
    file_data = data.get('files', {}).get(filename)
    if not file_data:
        decoded = unquote(filename)
        file_data = data.get('files', {}).get(decoded)
    if not file_data:
        return jsonify({'error': f'文件 {filename} 不在分析结果中'}), 404
    
    return jsonify({
        'name': filename,
        'content': file_data.get('content', ''),
        'html_content': file_data.get('html_content', ''),
        'has_fault': file_data.get('has_fault', False),
        'size': file_data.get('size', 0),
        'is_critical': file_data.get('is_critical', False),
        'analysis': file_data.get('analysis', [])
    })

@app.route('/api/analysis/<analysis_id>/tree')
def get_analysis_tree(analysis_id):
    offset = request.args.get('offset', 0, type=int)
    limit = request.args.get('limit', 50, type=int)
    data = get_analysis_result(analysis_id)
    if not data:
        return jsonify({'error': '分析结果不存在或已过期'}), 404
    all_groups = data['date_groups']
    page = all_groups[offset:offset + limit]
    return jsonify({
        'date_groups': page,
        'offset': offset,
        'limit': limit,
        'total': len(all_groups),
        'has_more': (offset + limit) < len(all_groups)
    })

# ========== PDF导入API ==========
@app.route('/api/import_pdf', methods=['POST'])
def import_pdf():
    if not session.get('admin_logged_in'):
        return jsonify({'error': '请先登录管理员'}), 401

    series = request.form.get('series', '').strip()
    model = request.form.get('model', '').strip()
    if not series or not model:
        return jsonify({'error': '请选择设备系列和型号'}), 400
    if 'file' not in request.files:
        return jsonify({'error': '未上传PDF文件'}), 400
    file = request.files['file']
    filename = file.filename
    if not filename.lower().endswith('.pdf'):
        return jsonify({'error': '请上传PDF格式文件'}), 400

    try:
        from PyPDF2 import PdfReader
    except ImportError:
        return jsonify({'error': '请安装PyPDF2库: pip install PyPDF2'}), 500

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as tmp:
            file.save(tmp.name)
            tmp_path = tmp.name
        reader = PdfReader(tmp_path)
        full_text = ''
        for page in reader.pages:
            text = page.extract_text()
            if text:
                full_text += text + '\n'
        os.unlink(tmp_path)
        if not full_text.strip():
            return jsonify({'error': 'PDF内容为空或无法提取文本（可能是扫描件）'}), 400

        entries = extract_fault_entries(full_text)
        if not entries:
            return jsonify({'error': '未能从PDF中提取到电机状态数据'}), 400

        added_count = store_pdf_entries(entries, series, model)
        if added_count == 0:
            return jsonify({'error': '所有条目已存在，无需导入'}), 400

        logger.info(f"PDF导入完成: {added_count} 条电机状态 - {series}/{model}")
        return jsonify({
            'success': True,
            'message': f'成功导入 {added_count} 条电机状态数据',
            'count': added_count,
            'total_extracted': len(entries),
            'text_length': len(full_text)
        })
    except Exception as e:
        logger.error(f"PDF导入失败: {e}", exc_info=True)
        return jsonify({'error': f'PDF处理失败: {str(e)}'}), 500

# ========== Web界面路由 ==========
@app.route('/')
def index():
    return render_template_string(MAIN_HTML)

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        password = request.form.get('password', '')
        if password == Config.ADMIN_PASSWORD:
            session['admin_logged_in'] = True
            session.permanent = True
            return redirect(url_for('admin_rules'))
        return '''
        <!DOCTYPE html>
        <html>
        <head><meta charset="UTF-8"><title>登录失败</title></head>
        <body style="font-family:system-ui;text-align:center;padding:50px;">
            <h3>❌ 密码错误</h3>
            <a href="/admin/login" style="color:#1e6f9f;">重试</a>
        </body>
        </html>
        '''
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>管理员登录</title>
        <style>
            body { font-family: system-ui; background: linear-gradient(135deg, #e8ecf2 0%, #d0d9e8 100%); min-height: 100vh; display: flex; justify-content: center; align-items: center; margin: 0; }
            .login-box { background: white; padding: 40px; border-radius: 20px; box-shadow: 0 10px 30px rgba(0,0,0,0.1); width: 400px; }
            h2 { color: #1e6f9f; text-align: center; margin-bottom: 30px; }
            input { width: 100%; padding: 12px; border: 2px solid #ddd; border-radius: 10px; margin: 10px 0; font-size: 1rem; box-sizing: border-box; }
            input:focus { outline: none; border-color: #1e6f9f; }
            button { width: 100%; padding: 12px; background: linear-gradient(135deg, #1e6f9f 0%, #2a8fd4 100%); color: white; border: none; border-radius: 10px; font-size: 1.1rem; font-weight: 600; cursor: pointer; margin-top: 10px; }
            button:hover { transform: translateY(-2px); box-shadow: 0 5px 15px rgba(30,111,159,0.3); }
            .hint { text-align: center; color: #999; margin-top: 15px; font-size: 0.9rem; }
        </style>
    </head>
    <body>
        <div class="login-box">
            <h2>🔐 管理员登录</h2>
            <form method="post">
                <input type="password" name="password" placeholder="请输入密码" required>
                <button type="submit">登录</button>
            </form>
            <div class="hint">默认密码: admin123</div>
        </div>
    </body>
    </html>
    '''

@app.route('/admin/rules')
@login_required
def admin_rules():
    return render_template_string(ADMIN_HTML)

@app.route('/admin/logout')
def admin_logout():
    session.clear()
    return redirect('/admin/login')

# ======================================================================
# ========== 以下为三个 HTML 模板字符串（请勿删除） ==========
# ========== 模板 1: MAIN_HTML (主页) ==========
MAIN_HTML = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>IVD 智能故障分析平台</title>
    <!-- Bootstrap 5 CSS (仅样式) -->
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <!-- Font Awesome 6 -->
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        /* 保留原有自定义样式，并做微调 */
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; 
            background: linear-gradient(135deg, #f5f7fa 0%, #e8ecf1 100%);
            padding: 20px;
            min-height: 100vh;
        }
        .container { max-width: 1400px; margin: 0 auto; }
        /* 自定义卡片，与Bootstrap的.card不冲突 */
        .ivd-card { 
            background: white; 
            border-radius: 16px; 
            padding: 28px; 
            box-shadow: 0 4px 20px rgba(0,0,0,0.08);
            border: 1px solid #e8ecf1;
        }
        .ivd-card h2 { 
            color: #0084a8; 
            margin-bottom: 20px; 
            font-size: 1.4rem;
            display: flex;
            align-items: center;
            gap: 10px;
            padding-bottom: 15px;
            border-bottom: 2px solid #f0f4f8;
        }
        /* 自定义按钮（避免与Bootstrap的.btn冲突） */
        .ivd-btn { 
            background: linear-gradient(135deg, #00a8cc 0%, #0084a8 100%); 
            color: white; 
            border: none; 
            padding: 14px 24px; 
            border-radius: 12px; 
            cursor: pointer !important; 
            font-weight: 600; 
            width: 100%; 
            font-size: 1rem; 
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1); 
            margin: 12px 0;
            box-shadow: 0 4px 12px rgba(0,168,204,0.3);
            position: relative;
            overflow: hidden;
        }
        .ivd-btn i {
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            display: inline-block;
        }
        .ivd-btn:hover:not(:disabled) { 
            transform: translateY(-3px) scale(1.02); 
            box-shadow: 0 8px 24px rgba(0,168,204,0.4);
        }
        .ivd-btn:hover:not(:disabled) i {
            transform: scale(1.2) rotate(10deg);
        }
        .ivd-btn:active:not(:disabled) {
            transform: translateY(-1px) scale(0.98);
        }
        .ivd-btn:active:not(:disabled) i {
            transform: scale(0.9);
        }
        .ivd-btn:disabled { opacity: 0.6; cursor: not-allowed !important; transform: none; }
        .series-btn { padding: 12px 20px !important; font-size: 0.95rem !important; cursor: pointer !important; }
        .series-btn:hover i { transform: scale(1.2) rotate(10deg); }
        .admin-link { 
            display: block; 
            text-align: center; 
            color: #0084a8; 
            text-decoration: none; 
            padding: 12px; 
            margin-top: 12px; 
            border-radius: 10px; 
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            font-weight: 500;
            border: 1px solid transparent;
            cursor: pointer !important;
        }
        .admin-link:hover { 
            background: #f0f9ff; 
            border-color: #00a8cc;
            transform: translateY(-2px);
            box-shadow: 0 4px 12px rgba(0,168,204,0.15);
        }
        .admin-link:active {
            transform: translateY(0) scale(0.98);
        }
        .admin-link i {
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            display: inline-block;
        }
        .admin-link:hover i {
            transform: scale(1.2) rotate(10deg);
        }
        .upload-area { 
            border: 2px dashed #00a8cc; 
            border-radius: 14px; 
            text-align: center; 
            padding: 35px 20px; 
            cursor: pointer !important; 
            background: linear-gradient(135deg, #f0f9ff 0%, #e0f2fe 100%); 
            margin: 15px 0; 
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }
        .upload-area:hover { 
            background: linear-gradient(135deg, #e0f2fe 0%, #bae6fd 100%); 
            border-color: #0084a8;
            transform: scale(1.02) translateY(-2px);
            box-shadow: 0 8px 24px rgba(0,168,204,0.2);
        }
        .upload-area:active {
            transform: scale(0.99);
        }
        .upload-area i {
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            display: inline-block;
        }
        .upload-area:hover i {
            transform: scale(1.2) rotate(10deg);
        }
        .result-box { 
            background: #f8fafc; 
            border-radius: 14px; 
            padding: 20px; 
            max-height: 800px; 
            overflow-y: auto; 
            border: 2px solid #e8ecf1; 
            font-size: 0.95rem; 
        }
        /* 自定义网格 */
        .ivd-grid { display: grid; grid-template-columns: 380px 1fr; gap: 30px; }
        @media (max-width: 900px) { .ivd-grid { grid-template-columns: 1fr; } }
        /* 文件列表样式 */
        .file-item { 
            padding: 10px 14px; 
            margin: 6px 0; 
            background: white; 
            border-radius: 8px; 
            border: 1px solid #e8ecf1; 
            cursor: pointer !important; 
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1); 
            font-size: 0.85rem;
        }
        .file-item:hover { 
            background: #f0f9ff; 
            transform: translateX(5px) translateY(-2px); 
            box-shadow: 0 4px 12px rgba(0,168,204,0.15);
            border-color: #00a8cc;
        }
        .file-item:active {
            transform: translateX(3px) scale(0.98);
        }
        .file-item i {
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            display: inline-block;
        }
        .file-item:hover i {
            transform: scale(1.2) rotate(5deg);
        }
        .file-item.critical { 
            border-left: 4px solid #ef4444; 
            background: linear-gradient(135deg, #fef2f2 0%, #fee2e2 100%);
        }
        .analysis-item { 
            padding: 14px; 
            margin: 10px 0; 
            background: linear-gradient(135deg, #fffbeb 0%, #fef3c7 100%); 
            border-left: 4px solid #f59e0b; 
            border-radius: 10px; 
            line-height: 1.5; 
            font-size: 0.9rem;
            box-shadow: 0 2px 8px rgba(245,158,11,0.1);
        }
        .analysis-item.pdf { background: linear-gradient(135deg, #ecfdf5 0%, #d1fae5 100%); border-color: #10b981; }
        .analysis-item.smart { background: linear-gradient(135deg, #f0fdf4 0%, #dcfce7 100%); border-color: #22c55e; }
        .tag { 
            background: linear-gradient(135deg, #00a8cc 0%, #0084a8 100%); 
            color: white; 
            padding: 6px 14px; 
            border-radius: 20px; 
            font-size: 0.75rem; 
            display: inline-block; 
            margin: 3px;
            font-weight: 600;
            cursor: pointer !important;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            border: 2px solid transparent;
            position: relative;
            overflow: hidden;
            user-select: none;
        }
        .tag::before {
            content: '';
            position: absolute;
            top: 50%;
            left: 50%;
            width: 0;
            height: 0;
            border-radius: 50%;
            background: rgba(255, 255, 255, 0.3);
            transform: translate(-50%, -50%);
            transition: width 0.6s, height 0.6s;
        }
        .tag::after {
            content: '👆';
            position: absolute;
            right: -20px;
            top: 50%;
            transform: translateY(-50%);
            font-size: 0.7rem;
            opacity: 0;
            transition: all 0.3s ease;
        }
        .tag i {
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            display: inline-block;
        }
        .tag:hover {
            transform: translateY(-3px) scale(1.05);
            box-shadow: 0 6px 20px rgba(0,168,204,0.4);
        }
        .tag:hover i {
            transform: scale(1.3) rotate(10deg);
        }
        .tag:hover::after {
            right: 4px;
            opacity: 1;
        }
        .tag:active::before {
            width: 200px;
            height: 200px;
        }
        .tag:active {
            transform: translateY(-1px) scale(0.98);
        }
        .tag:active i {
            transform: scale(0.9);
        }
        .tag.active {
            transform: scale(1.1);
            box-shadow: 0 8px 24px rgba(0,168,204,0.5);
            border-color: rgba(255, 255, 255, 0.5);
        }
        .tag.active i {
            animation: icon-bounce 0.6s ease infinite;
        }
        @keyframes icon-bounce {
            0%, 100% { transform: scale(1.2); }
            50% { transform: scale(1.4); }
        }
        .tag-all {
            background: linear-gradient(135deg, #64748b 0%, #475569 100%) !important;
        }
        .tag-all:hover {
            box-shadow: 0 6px 20px rgba(100,116,139,0.4) !important;
        }
        .tag-all.active {
            box-shadow: 0 8px 24px rgba(100,116,139,0.5) !important;
        }
        /* 标签特定颜色 */
        .tag-fault {
            background: linear-gradient(135deg, #fef2f2 0%, #fee2e2 100%) !important;
            color: #dc2626 !important;
            border: 2px solid #fecaca !important;
        }
        .tag-fault:hover {
            box-shadow: 0 6px 20px rgba(220, 38, 38, 0.4) !important;
        }
        .tag-fault.active {
            box-shadow: 0 8px 24px rgba(220, 38, 38, 0.5) !important;
            background: linear-gradient(135deg, #fee2e2 0%, #fecaca 100%) !important;
        }
        .tag-sample {
            background: linear-gradient(135deg, #eff6ff 0%, #dbeafe 100%) !important;
            color: #1d4ed8 !important;
            border: 2px solid #bfdbfe !important;
        }
        .tag-sample:hover {
            box-shadow: 0 6px 20px rgba(29, 78, 216, 0.4) !important;
        }
        .tag-sample.active {
            box-shadow: 0 8px 24px rgba(29, 78, 216, 0.5) !important;
            background: linear-gradient(135deg, #dbeafe 0%, #bfdbfe 100%) !important;
        }
        .tag-reagent {
            background: linear-gradient(135deg, #faf5ff 0%, #f3e8ff 100%) !important;
            color: #7c3aed !important;
            border: 2px solid #e9d5ff !important;
        }
        .tag-reagent:hover {
            box-shadow: 0 6px 20px rgba(124, 58, 237, 0.4) !important;
        }
        .tag-reagent.active {
            box-shadow: 0 8px 24px rgba(124, 58, 237, 0.5) !important;
            background: linear-gradient(135deg, #f3e8ff 0%, #e9d5ff 100%) !important;
        }
        .tag-receive {
            background: linear-gradient(135deg, #f0fdf4 0%, #dcfce7 100%) !important;
            color: #15803d !important;
            border: 2px solid #bbf7d0 !important;
        }
        .tag-receive:hover {
            box-shadow: 0 6px 20px rgba(21, 128, 61, 0.4) !important;
        }
        .tag-receive.active {
            box-shadow: 0 8px 24px rgba(21, 128, 61, 0.5) !important;
            background: linear-gradient(135deg, #dcfce7 0%, #bbf7d0 100%) !important;
        }
        /* 统计卡片 */
        .stat { 
            background: linear-gradient(135deg, #f8fafc 0%, #f0f4f8 100%); 
            padding: 18px; 
            border-radius: 12px; 
            text-align: center;
            border: 1px solid #e8ecf1;
            transition: all 0.3s ease;
        }
        .stat:hover {
            transform: translateY(-3px);
            box-shadow: 0 4px 12px rgba(0,168,204,0.15);
        }
        .stat .number { 
            font-size: 2rem; 
            font-weight: 700; 
            background: linear-gradient(135deg, #00a8cc 0%, #0084a8 100%); 
            -webkit-background-clip: text; 
            -webkit-text-fill-color: transparent;
        }
        .stat .label { font-size: 0.9rem; color: #64748b; margin-top: 5px; }
        /* 模态框 */
        .modal-overlay { 
            display: none; 
            position: fixed; 
            inset: 0; 
            background: rgba(0,0,0,0.6); 
            justify-content: center; 
            align-items: center; 
            z-index: 9999; 
            padding: 20px;
            backdrop-filter: blur(4px);
        }
        .modal-card { 
            background: white; 
            border-radius: 20px; 
            width: min(100%, 1100px); 
            max-height: 90vh; 
            overflow: hidden; 
            box-shadow: 0 25px 80px rgba(0,0,0,0.25); 
            display: flex; 
            flex-direction: column;
        }
        .modal-header { 
            padding: 20px 24px; 
            display: flex; 
            justify-content: space-between; 
            align-items: center; 
            border-bottom: 2px solid #f0f4f8;
            background: linear-gradient(135deg, #f8fafc 0%, #f0f4f8 100%);
        }
        .modal-title { font-size: 1.25rem; font-weight: 700; color: #0084a8; }
        .modal-close { 
            border: none; 
            background: #f1f5f9; 
            color: #64748b;
            width: 36px;
            height: 36px;
            border-radius: 50%; 
            cursor: pointer; 
            font-size: 1.3rem;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.2s ease;
        }
        .modal-close:hover { background: #e2e8f0; color: #ef4444; }
        .modal-body { 
            padding: 24px; 
            overflow-y: auto; 
            white-space: pre-wrap; 
            word-break: break-word; 
            font-family: 'Courier New', monospace; 
            font-size: 0.95rem; 
            line-height: 1.7; 
            color: #334155;
        }
        .btn-load-more {
            background: linear-gradient(135deg, #ecfdf5 0%, #d1fae5 100%);
            border: 2px solid #10b981;
            color: #047857;
            padding: 12px 24px;
            border-radius: 10px;
            cursor: pointer;
            font-weight: 600;
            font-size: 0.95rem;
            transition: all 0.3s ease;
            display: inline-flex;
            align-items: center;
            gap: 8px;
        }
        .btn-load-more:hover {
            background: linear-gradient(135deg, #d1fae5 0%, #a7f3d0 100%);
            transform: translateY(-2px);
            box-shadow: 0 4px 12px rgba(16,185,129,0.2);
        }
        .btn-load-more:disabled {
            opacity: 0.5;
            cursor: not-allowed;
            transform: none;
        }
        footer { 
            text-align: center; 
            padding: 40px; 
            color: #64748b; 
            margin-top: 40px;
            font-size: 0.95rem;
        }
        /* 滚动条美化 */
        ::-webkit-scrollbar { width: 8px; }
        ::-webkit-scrollbar-track { background: #f1f5f9; border-radius: 10px; }
        ::-webkit-scrollbar-thumb { background: linear-gradient(135deg, #00a8cc 0%, #0084a8 100%); border-radius: 10px; }
        ::-webkit-scrollbar-thumb:hover { background: #0084a8; }
        /* Bootstrap 辅助类覆盖 */
        .bg-primary-gradient { background: linear-gradient(135deg, #00a8cc 0%, #0084a8 100%); }
    </style>
</head>
<body>
<div class="container">
    <div class="header text-center p-4 mb-4 bg-primary-gradient rounded-3 text-white shadow">
        <h1><i class="fas fa-flask me-2"></i> IVD 智能故障分析平台</h1>
        <p class="lead">SMART · Venus 系列设备智能诊断系统</p>
    </div>
    <div class="ivd-grid">
        <div class="ivd-card shadow-sm p-4">
            <h2><i class="fas fa-upload me-2"></i> 上传文件</h2>
            <div style="display:flex;gap:10px;margin-bottom:10px;">
                <button class="ivd-btn series-btn active" data-series="SMART" style="flex:1;background:#1e6f9f;">SMART</button>
                <button class="ivd-btn series-btn" data-series="Venus" style="flex:1;background:#6c757d;">Venus</button>
            </div>
            <select id="modelSelect" class="form-select"><option>请先选择系列</option></select>
            <div class="upload-area" id="uploadArea">
                <div style="font-size:1.2rem;margin-bottom:5px;"><i class="fas fa-cloud-upload-alt me-2"></i>点击或拖拽文件</div>
                <div style="font-size:0.9rem;color:#999;">支持 .txt .log .zip | 可多选 | 最大 50MB</div>
                <input type="file" id="fileInput" accept=".txt,.log,.zip" multiple style="display:none">
                <div id="fileName" style="margin-top:10px;font-weight:500;color:#1e6f9f;"></div>
            </div>
            <div style="margin-top:12px;">
                <div style="font-size:0.9rem;color:#0891b2;margin-bottom:6px;display:flex;align-items:center;gap:6px;"><i class="fas fa-snowflake"></i> 试剂制冷排查</div>
                <div class="upload-area" id="coolingUploadArea" style="border-color:#0891b2;background:linear-gradient(135deg, #f0fdfa 0%, #e0f2fe 100%);padding:18px;">
                    <div style="font-size:1rem;margin-bottom:4px;"><i class="fas fa-cloud-upload-alt me-2" style="color:#0891b2;"></i>点击或拖拽上传</div>
                    <div style="font-size:0.8rem;color:#64748b;">仅支持 .txt .log | 单文件 | 最大 50MB</div>
                    <input type="file" id="coolingFileInput" accept=".txt,.log" style="display:none">
                    <div id="coolingFileName" style="margin-top:8px;font-weight:500;color:#0891b2;font-size:0.85rem;"></div>
                </div>
            </div>
            <button class="ivd-btn" id="analyzeBtn"><i class="fas fa-search me-1"></i> 开始分析</button>
            <a href="/admin/rules" class="admin-link"><i class="fas fa-cog me-1"></i> 管理知识库 →</a>
        </div>
        <div class="ivd-card shadow-sm p-4">
            <h2><i class="fas fa-clipboard-list me-2"></i> 诊断报告</h2>
            <div class="result-box" id="resultArea">
                <div class="empty-state text-center py-5 text-secondary">
                    <i class="fas fa-microscope" style="font-size:4rem;opacity:0.4;"></i>
                    <div class="mt-3">请上传文件并开始分析</div>
                    <div style="font-size:0.9rem;color:#999;margin-top:5px;">系统将自动识别故障并提供解决方案</div>
                </div>
            </div>
        </div>
    </div>
    <footer><i class="far fa-copyright me-1"></i> 2026 IVD 智能故障分析平台 v2.6</footer>
</div>

<script>
let currentSeries = 'SMART';
let selectedFiles = [];

// 诊断报告分页状态
let reportGroups = {
    fault: { data: [], limit: 50, total: 0, displayed: 0 },
    sample: { data: [], limit: 50, total: 0, displayed: 0 },
    reagent: { data: [], limit: 50, total: 0, displayed: 0 }
};

// 文件列表数据（用于查看全文）
let fileContentsMap = {};
let fileMetadataList = [];

// 系列切换
async function loadModelsForSeries(series, defaultModel = '') {
    currentSeries = series;
    document.querySelectorAll('.series-btn').forEach(b => {
        b.style.background = b.dataset.series === series ? '#1e6f9f' : '#6c757d';
    });
    const resp = await fetch(`/api/models?series=${currentSeries}`);
    const models = await resp.json();
    let opts = '<option value="">请选择型号</option>';
    models.forEach(m => opts += `<option value="${m.name}">${m.name}</option>`);
    const modelSelect = document.getElementById('modelSelect');
    modelSelect.innerHTML = opts;
    if (defaultModel && models.some(m => m.name === defaultModel)) {
        modelSelect.value = defaultModel;
    }
}

document.querySelectorAll('.series-btn').forEach(btn => {
    btn.onclick = function() {
        loadModelsForSeries(this.dataset.series);
    };
});

window.addEventListener('DOMContentLoaded', () => {
    loadModelsForSeries('SMART', 'SMART6500');
});

// 文件上传
const uploadArea = document.getElementById('uploadArea');
const fileInput = document.getElementById('fileInput');

uploadArea.onclick = () => fileInput.click();
fileInput.onchange = e => {
    if (e.target.files.length) {
        selectedFiles = Array.from(e.target.files);
        // 清除冷却上传框的文件，避免混淆
        coolingFile = null;
        document.getElementById('coolingFileName').innerHTML = '';
        const totalSize = selectedFiles.reduce((sum, f) => sum + f.size, 0);
        if (selectedFiles.length === 1) {
            document.getElementById('fileName').innerHTML = `<i class="fas fa-check-circle text-success me-1"></i> ${selectedFiles[0].name} (${(selectedFiles[0].size/1024).toFixed(0)} KB)`;
        } else {
            document.getElementById('fileName').innerHTML = `<i class="fas fa-check-circle text-success me-1"></i> 已选择 ${selectedFiles.length} 个文件 (${(totalSize/1024).toFixed(0)} KB)`;
        }
    }
};

uploadArea.ondragover = e => e.preventDefault();
uploadArea.ondrop = e => {
    e.preventDefault();
    if (e.dataTransfer.files.length) {
        selectedFiles = Array.from(e.dataTransfer.files);
        // 清除冷却上传框的文件，避免混淆
        coolingFile = null;
        document.getElementById('coolingFileName').innerHTML = '';
        const totalSize = selectedFiles.reduce((sum, f) => sum + f.size, 0);
        if (selectedFiles.length === 1) {
            document.getElementById('fileName').innerHTML = `<i class="fas fa-check-circle text-success me-1"></i> ${selectedFiles[0].name} (${(selectedFiles[0].size/1024).toFixed(0)} KB)`;
        } else {
            document.getElementById('fileName').innerHTML = `<i class="fas fa-check-circle text-success me-1"></i> 已选择 ${selectedFiles.length} 个文件 (${(totalSize/1024).toFixed(0)} KB)`;
        }
    }
};

// 分析按钮
document.getElementById('analyzeBtn').onclick = async function() {
    const isCoolingMode = coolingFile !== null;
    
    if (isCoolingMode) {
        this.disabled = true;
        this.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i> 排查中...';
        document.getElementById('resultArea').innerHTML = '<div class="text-center py-5"><i class="fas fa-snowflake" style="font-size:3rem;color:#0891b2;"></i><div class="mt-3">正在排查制冷异常...</div></div>';
        const fd = new FormData();
        fd.append('file', coolingFile);
        fd.append('series', 'SMART');
        fd.append('model', 'SMART6500');
        fd.append('analysis_type', 'reagent_cooling');
        try {
            const resp = await fetchWithTimeout('/api/analyze', { method: 'POST', body: fd }, 300000);
            if (!resp.ok) { const text = await resp.text(); throw new Error('服务器错误: ' + text.slice(0, 200)); }
            const data = await resp.json();
            if (data.error) { document.getElementById('resultArea').innerHTML = '<div class="alert alert-danger">❌ ' + data.error + '</div>'; return; }
            if (data.status === 'accepted' && data.analysis_id) {
                document.getElementById('resultArea').innerHTML = '<div class="text-center py-5"><i class="fas fa-snowflake fa-spin" style="font-size:3rem;color:#0891b2;"></i><div class="mt-3">正在排查制冷异常...</div></div>';
                pollTaskStatus(data.analysis_id, this, true);
                return;
            }
            if (data.redirect_url) { window.location.href = data.redirect_url; return; }
        } catch (err) {
            document.getElementById('resultArea').innerHTML = '<div class="alert alert-danger">❌ 排查失败: ' + err.message + '</div>';
        } finally {
            if (!isCoolingMode || !document.getElementById('analyzeBtn').dataset.polling) {
                this.disabled = false;
                this.innerHTML = '<i class="fas fa-search me-1"></i> 开始分析';
            }
        }
        return;
    }
    
    const model = document.getElementById('modelSelect').value;
    if (!model) {
        document.getElementById('resultArea').innerHTML = '<div class="alert alert-danger">⚠️ 请选择设备型号</div>';
        return;
    }
    if (!selectedFiles || selectedFiles.length === 0) {
        document.getElementById('resultArea').innerHTML = '<div class="alert alert-danger">⚠️ 请选择文件</div>';
        return;
    }
    this.disabled = true;
    this.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i> 分析中...';
    document.getElementById('resultArea').innerHTML = '<div class="text-center py-5"><i class="fas fa-hourglass-half" style="font-size:3rem;color:#0084a8;"></i><div class="mt-3">正在智能分析...</div></div>';

    const fd = new FormData();
    if (selectedFiles.length === 1) {
        fd.append('file', selectedFiles[0]);
    } else {
        selectedFiles.forEach((file) => { fd.append('files', file); });
    }
    fd.append('series', currentSeries);
    fd.append('model', model);

    try {
        const resp = await fetchWithTimeout('/api/analyze', { method: 'POST', body: fd }, 300000);
        if (!resp.ok) {
            const text = await resp.text();
            throw new Error(`服务器错误 ${resp.status}: ${text.slice(0, 300)}`);
        }
        const data = await resp.json();
        if (data.error) {
            document.getElementById('resultArea').innerHTML = `<div class="alert alert-danger">❌ ${data.error}</div>`;
            return;
        }
        if (data.status === 'accepted' && data.analysis_id) {
            document.getElementById('resultArea').innerHTML = '<div class="text-center py-5"><i class="fas fa-cog fa-spin" style="font-size:3rem;color:#2563eb;"></i><div class="mt-3">任务已提交，正在智能分析...</div></div>';
            pollTaskStatus(data.analysis_id, this, false);
            return;
        }
        if (data.redirect_url) {
            window.location.href = data.redirect_url;
            return;
        }
    } catch (err) {
        let message;
        if (err.name === 'TimeoutError') {
            message = '⏰ 分析请求超时，请重试';
        } else if (err.message && err.message.includes('Failed to fetch')) {
            message = '🔌 无法连接到服务器，请确认服务已启动';
        } else {
            message = `请求失败: ${err.message}`;
        }
        document.getElementById('resultArea').innerHTML = `<div class="alert alert-danger">❌ ${message}</div>`;
    } finally {
        if (!document.getElementById('analyzeBtn').dataset.polling) {
            this.disabled = false;
            this.innerHTML = '<i class="fas fa-search me-1"></i> 开始分析';
        }
    }
};

function pollTaskStatus(analysisId, btn, isCooling) {
    btn.dataset.polling = 'true';
    let pollCount = 0;
    const maxPolls = 600;
    const interval = 1000;
    function poll() {
        pollCount++;
        if (pollCount > maxPolls) {
            delete btn.dataset.polling;
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-search me-1"></i> 开始分析';
            document.getElementById('resultArea').innerHTML = '<div class="alert alert-warning">⏰ 分析超时，请重试</div>';
            return;
        }
        fetch(`/api/task_status/${analysisId}`)
            .then(r => r.json())
            .then(d => {
                if (d.status === 'completed' && d.redirect_url) {
                    delete btn.dataset.polling;
                    window.location.href = d.redirect_url;
                } else if (d.status === 'failed') {
                    delete btn.dataset.polling;
                    btn.disabled = false;
                    btn.innerHTML = '<i class="fas fa-search me-1"></i> 开始分析';
                    document.getElementById('resultArea').innerHTML = `<div class="alert alert-danger">❌ 分析失败: ${d.error || '未知错误'}</div>`;
                } else {
                    const icon = isCooling ? 'fa-snowflake' : 'fa-cog';
                    const color = isCooling ? '#0891b2' : '#2563eb';
                    const text = isCooling ? '正在排查制冷异常' : '正在智能分析';
                    document.getElementById('resultArea').innerHTML = `<div class="text-center py-5"><i class="fas ${icon} fa-spin" style="font-size:3rem;color:${color};"></i><div class="mt-3">${text}... (已等待 ${pollCount} 秒)</div></div>`;
                    setTimeout(poll, interval);
                }
            })
            .catch(() => { setTimeout(poll, 2000); });
    }
    setTimeout(poll, 1000);
}

// ========== 试剂制冷排查 ==========
const coolingUploadArea = document.getElementById('coolingUploadArea');
const coolingFileInput = document.getElementById('coolingFileInput');
let coolingFile = null;

coolingUploadArea.onclick = () => coolingFileInput.click();
coolingUploadArea.ondragover = e => { e.preventDefault(); coolingUploadArea.style.borderColor = '#0e7490'; coolingUploadArea.style.background = 'linear-gradient(135deg, #e0f2fe 0%, #bae6fd 100%)'; };
coolingUploadArea.ondragleave = e => { e.preventDefault(); coolingUploadArea.style.borderColor = '#0891b2'; coolingUploadArea.style.background = ''; };
coolingUploadArea.ondrop = e => { e.preventDefault(); coolingUploadArea.style.borderColor = '#0891b2'; coolingUploadArea.style.background = ''; if (e.dataTransfer.files.length > 0) handleCoolingFile(e.dataTransfer.files[0]); };

coolingFileInput.onchange = e => { if (e.target.files.length > 0) handleCoolingFile(e.target.files[0]); };

function handleCoolingFile(file) {
    const name = file.name.toLowerCase();
    if (!name.endsWith('.txt') && !name.endsWith('.log')) {
        alert('仅支持 .txt 和 .log 文件！');
        coolingFileInput.value = '';
        return;
    }
    if (file.size > 50 * 1024 * 1024) {
        alert('文件大小不能超过50MB！');
        coolingFileInput.value = '';
        return;
    }
    coolingFile = file;
    // 清除上方上传框的文件，避免混淆
    selectedFiles = [];
    document.getElementById('fileName').innerHTML = '';
    const sizeMB = (file.size / 1024 / 1024).toFixed(2);
    document.getElementById('coolingFileName').innerHTML = '<i class="fas fa-check-circle" style="color:#10b981;margin-right:4px;"></i>' + file.name + ' (' + sizeMB + ' MB)';
}

// ========== 渲染结果 ==========
function renderResult(data) {
    // 重置分页
    reportGroups = {
        fault: { data: [], limit: 50, total: 0, displayed: 0 },
        sample: { data: [], limit: 50, total: 0, displayed: 0 },
        reagent: { data: [], limit: 50, total: 0, displayed: 0 }
    };

    const faultAlerts = data.analysis ? data.analysis.filter(item => item.type === 'motor_status_match') : [];
    const hasFaultDoc = faultAlerts.length > 0;
    const sampleEmpty = hasFaultDoc ? [] : (data.analysis ? data.analysis.filter(item => 
        item.type === 'keyword_match' && 
        item.keywords && item.keywords.some(kw => ['样本空吸', '样本不足'].includes(kw))
    ) : []);
    const reagentEmpty = hasFaultDoc ? [] : (data.analysis ? data.analysis.filter(item => 
        item.type === 'keyword_match' && 
        item.keywords && item.keywords.some(kw => ['试剂空吸', '试剂不足'].includes(kw))
    ) : []);

    reportGroups.fault.data = faultAlerts;
    reportGroups.fault.total = faultAlerts.length;
    reportGroups.fault.timestamp = data.analyzed_at ? data.analyzed_at : '';
    reportGroups.sample.data = sampleEmpty;
    reportGroups.sample.total = sampleEmpty.length;
    reportGroups.reagent.data = reagentEmpty;
    reportGroups.reagent.total = reagentEmpty.length;

    const displayedCount = faultAlerts.length + sampleEmpty.length + reagentEmpty.length;
    const showSampleReagentGroups = !hasFaultDoc;

    // 统计卡片
    let html = `
        <div class="row g-3 mb-3">
            <div class="col-6 col-md-3"><div class="stat"><div class="number">${displayedCount}</div><div class="label">总命令/故障数</div></div></div>
            <div class="col-6 col-md-3"><div class="stat"><div class="number">${faultAlerts.length}</div><div class="label">故障表</div></div></div>
            <div class="col-6 col-md-3"><div class="stat"><div class="number">${showSampleReagentGroups ? sampleEmpty.length : 0}</div><div class="label">样本发送表</div></div></div>
            <div class="col-6 col-md-3"><div class="stat"><div class="number">${showSampleReagentGroups ? reagentEmpty.length : 0}</div><div class="label">试剂发送表</div></div></div>
        </div>
    `;

    if (hasFaultDoc) {
        html += `<div class="alert alert-warning"><i class="fas fa-exclamation-triangle me-1"></i> 检测到故障文档，样本/试剂匹配结果已隐藏。</div>`;
    }

    // 文件列表
    if (fileMetadataList.length > 0) {
        html += `<div class="card shadow-sm p-3 mb-3"><div class="fw-bold fs-6 text-primary mb-2"><i class="fas fa-folder-open me-1"></i> 分析文件列表 (${fileMetadataList.length} 个)</div><div style="max-height:250px;overflow-y:auto;">`;
        fileMetadataList.forEach((f, idx) => {
            const icon = f.is_critical ? '<i class="fas fa-circle text-danger"></i>' : '<i class="fas fa-file-alt"></i>';
            html += `<div class="file-item ${f.is_critical ? 'critical' : ''}" data-file-index="${idx}" style="cursor:pointer;">
                ${icon} ${escapeHtml(f.name)} <span class="text-secondary" style="font-size:0.85rem;">(${f.size} 字符)</span>
                ${f.is_critical ? '<span class="badge bg-danger ms-2">关键文件</span>' : ''}
            </div>`;
        });
        html += `</div></div>`;
    }

    // 三个分组
    html += renderGroup('fault', '🚨 故障表', '故障命令记录');
    if (!hasFaultDoc) {
        html += renderGroup('sample', '🧪 样本发送表', '样本发送命令');
        html += renderGroup('reagent', '🧫 试剂发送表', '试剂发送命令');
    }

    document.getElementById('resultArea').innerHTML = html;

    // 绑定文件点击
    document.querySelectorAll('.file-item[data-file-index]').forEach(el => {
        el.addEventListener('click', () => openFilePreviewByIndex(el.dataset.fileIndex));
    });

    renderGroupItems('fault');
    renderGroupItems('sample');
    renderGroupItems('reagent');
}

function escapeHtml(text) {
    if (!text) return '';
    return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#039;');
}

function groupFaultItemsByDate(items) {
    const groups = {};
    items.forEach(item => {
        const date = item.event_date || '未识别日期';
        if (!groups[date]) {
            groups[date] = [];
        }
        groups[date].push(item);
    });
    return Object.keys(groups).sort().map(date => ({ date, items: groups[date] }));
}

function openFilePreviewByIndex(index) {
    const idx = parseInt(index, 10);
    const fileMeta = fileMetadataList[idx];
    const filename = fileMeta ? fileMeta.name : '';
    const content = filename ? (fileContentsMap[filename] || '') : '';
    const previewModal = document.getElementById('filePreviewModal');
    const previewContent = document.getElementById('filePreviewContent');
    const previewTitle = document.getElementById('filePreviewTitle');
    if (!previewModal || !previewContent || !previewTitle) return;
    previewTitle.textContent = filename || '文件预览';
    previewContent.textContent = content || '⚠️ 文件内容为空或未加载。';
    previewModal.style.display = 'flex';
}

function closeFilePreview() {
    const previewModal = document.getElementById('filePreviewModal');
    if (previewModal) {
        previewModal.style.display = 'none';
    }
}

function fetchWithTimeout(url, options = {}, timeout = 300000) {
    const controller = new AbortController();
    const signal = controller.signal;
    const fetchOptions = { ...options, signal };
    const timeoutId = setTimeout(() => {
        controller.abort();
    }, timeout);
    return fetch(url, fetchOptions)
        .finally(() => clearTimeout(timeoutId))
        .catch(err => {
            if (err.name === 'AbortError') {
                const timeoutErr = new Error('请求超时');
                timeoutErr.name = 'TimeoutError';
                throw timeoutErr;
            }
            throw err;
        });
}

function renderGroup(groupKey, title, emptyMsg) {
    const total = reportGroups[groupKey].total;
    const timestamp = groupKey === 'fault' ? reportGroups[groupKey].timestamp : '';
    return `
        <div class="card shadow-sm p-3 mt-3">
            <div class="fw-bold fs-6 text-primary">${title} <span class="text-secondary fw-normal fs-6">(共 ${total} 条)</span></div>
            ${timestamp ? `<div class="small text-muted mb-2"><i class="far fa-clock me-1"></i> 故障分析时间: ${escapeHtml(timestamp)}</div>` : ''}
            <div id="group-${groupKey}-container">
                ${total === 0 ? `<div class="text-secondary">暂无 ${emptyMsg}</div>` : ''}
            </div>
            <div id="group-${groupKey}-loadmore" class="text-center mt-2"></div>
        </div>
    `;
}

function renderGroupItems(groupKey) {
    const group = reportGroups[groupKey];
    const container = document.getElementById(`group-${groupKey}-container`);
    const loadMoreDiv = document.getElementById(`group-${groupKey}-loadmore`);
    if (!container) return;
    const total = group.total;
    if (total === 0) {
        container.innerHTML = `<div class="text-secondary">暂无记录</div>`;
        if (loadMoreDiv) loadMoreDiv.innerHTML = '';
        return;
    }
    let displayed = group.displayed || 0;
    if (displayed === 0) {
        displayed = Math.min(50, total);
        group.displayed = displayed;
    }
    const items = group.data.slice(0, displayed);
    let html = '';
    if (groupKey === 'fault') {
        const dateGroups = groupFaultItemsByDate(items);
        dateGroups.forEach(groupItem => {
            const headerTime = groupItem.items[0] && groupItem.items[0].event_time ? groupItem.items[0].event_time : '';
            html += `
                <div class="border rounded p-3 mb-3 bg-light">
                    <div class="d-flex justify-content-between align-items-start flex-wrap mb-2">
                        <div><div class="fw-bold text-warning">故障日期：${escapeHtml(groupItem.date)}</div>
                        ${headerTime ? `<div class="small text-muted">首次记录时间：${escapeHtml(headerTime)}</div>` : ''}</div>
                        <span class="badge bg-secondary">共 ${groupItem.items.length} 条故障</span>
                    </div>
            `;
            groupItem.items.forEach(item => {
                const original = item.original_text || ''; 
                const diagnosisText = item.db_match_text || item.db_command || item.advice || '';
                const diagnosis = diagnosisText ? `<div class="flex-shrink-0 ms-3" style="min-width:220px;max-width:42%;"><strong>诊断建议：</strong><span class="text-success">${escapeHtml(diagnosisText)}</span></div>` : '';
                html += `
                    <div class="d-flex align-items-start justify-content-between flex-wrap p-3 mb-2 bg-white rounded border-start border-4 border-warning">
                        <div class="flex-grow-1" style="min-width:220px;font-size:0.88rem;">${escapeHtml(original)}</div>
                        ${diagnosis}
                    </div>
                `;
            });
            html += `</div>`;
        });
    } else {
        items.forEach(item => {
            const original = item.original_text || '';
            html += `
                <div class="p-3 mb-2 bg-white rounded border-start border-4 border-info">
                    <div style="white-space:pre-wrap;word-break:break-word;font-size:0.88rem;">${escapeHtml(original)}</div>
                </div>
            `;
        });
    }
    container.innerHTML = html;
    if (loadMoreDiv) {
        if (displayed < total) {
            const remaining = total - displayed;
            loadMoreDiv.innerHTML = `
                <button class="ivd-btn" onclick="loadMoreGroup('${groupKey}')" style="width:auto;padding:8px 20px;display:inline-block;margin:0 auto;font-size:0.9rem;">
                    <i class="fas fa-arrow-down me-1"></i> 加载更多 ${Math.min(50, remaining)} 条 (已显示 ${displayed}/${total})
                </button>
            `;
        } else {
            loadMoreDiv.innerHTML = `<div class="text-secondary"><i class="fas fa-check-circle text-success me-1"></i> 已显示全部 ${total} 条</div>`;
        }
    }
}

function loadMoreGroup(groupKey) {
    const group = reportGroups[groupKey];
    let newDisplayed = (group.displayed || 0) + 50;
    if (newDisplayed > group.total) newDisplayed = group.total;
    group.displayed = newDisplayed;
    renderGroupItems(groupKey);
}
</script>

<div id="filePreviewModal" class="modal-overlay" onclick="if(event.target === this) closeFilePreview();">
    <div class="modal-card">
        <div class="modal-header">
            <div id="filePreviewTitle" class="modal-title"><i class="fas fa-file-alt me-2"></i>文件预览</div>
            <button type="button" class="modal-close" onclick="closeFilePreview()"><i class="fas fa-times"></i></button>
        </div>
        <div id="filePreviewContent" class="modal-body"></div>
    </div>
</div>
</body>
</html>
'''
# ========== MAIN_HTML 结束 ==========

# ========== 模板 2: ADMIN_HTML (管理后台) ==========
ADMIN_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>知识库管理</title>
    <!-- Bootstrap 5 CSS -->
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <!-- Font Awesome -->
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        /* 自定义样式，补充Bootstrap */
        body { font-family: system-ui; background: #f5f7fb; padding: 30px; }
        .container { max-width: 1200px; margin: auto; background: white; padding: 30px; border-radius: 20px; box-shadow: 0 10px 30px rgba(0,0,0,0.1); }
        h2 { color: #1e6f9f; margin-bottom: 20px; }
        /* 自定义按钮（避免与Bootstrap冲突） */
        .ivd-btn { 
            padding: 10px 20px; 
            border: none; 
            border-radius: 10px; 
            cursor: pointer; 
            font-weight: 600; 
            transition: all 0.3s; 
            background: #1e6f9f; 
            color: white; 
        }
        .ivd-btn:hover { background: #2a8fd4; }
        .ivd-btn-danger { background: #dc3545; color: white; }
        .ivd-btn-danger:hover { background: #c82333; }
        .ivd-btn-success { background: #28a745; color: white; }
        .ivd-btn-success:hover { background: #20c997; }
        .ivd-btn-warning { background: #ffc107; color: #333; }
        .ivd-btn-warning:hover { background: #e0a800; }
        .ivd-btn-sm { padding: 5px 10px; font-size: 0.8rem; }
        /* Tabs自定义 */
        .ivd-tabs { display: flex; gap: 10px; border-bottom: 2px solid #eee; margin-bottom: 20px; flex-wrap:wrap; }
        .ivd-tab { padding: 12px 24px; cursor: pointer; border-radius: 10px 10px 0 0; transition: all 0.3s; }
        .ivd-tab:hover { background: #f0f4f8; }
        .ivd-tab.active { background: #1e6f9f; color: white; }
        .ivd-tab-content { display: none; padding: 20px 0; }
        .ivd-tab-content.active { display: block; }
        /* 其他原有样式 */
        .rule-item { border: 2px solid #e0e8f0; border-radius: 12px; padding: 15px; margin: 10px 0; }
        .rule-item .actions { display: flex; gap: 10px; margin-top: 10px; }
        .badge { display: inline-block; padding: 4px 12px; border-radius: 20px; font-size: 0.85rem; font-weight: 600; }
        .badge-pdf { background: #d4edda; color: #155724; }
        .badge-manual { background: #fff3cd; color: #856404; }
        .logout { float: right; color: #dc3545; text-decoration: none; }
        .logout:hover { text-decoration: underline; }
        .upload-area { border: 3px dashed #1e6f9f; border-radius: 15px; padding: 30px; text-align: center; cursor: pointer; background: #f8faff; margin: 15px 0; }
        .upload-area:hover { background: #f0f7ff; }
        .result-box { padding: 15px; border-radius: 10px; margin: 10px 0; }
        .result-success { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
        .result-error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        .result-info { background: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }
        .table-container { max-height: 500px; overflow-y: auto; margin-top: 10px; }
        .flex-row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    </style>
</head>
<body>
<div class="container">
    <h2><i class="fas fa-book me-2"></i>知识库管理 <a href="/admin/logout" class="logout"><i class="fas fa-sign-out-alt me-1"></i>退出</a></h2>
    <div class="ivd-tabs">
        <div class="ivd-tab active" onclick="switchTab('rules')"><i class="fas fa-pen me-1"></i> 规则管理</div>
        <div class="ivd-tab" onclick="switchTab('pdf')"><i class="fas fa-file-pdf me-1"></i> PDF导入</div>
        <div class="ivd-tab" onclick="switchTab('versions')"><i class="fas fa-history me-1"></i> 版本历史</div>
    </div>
    <div id="tab-rules" class="ivd-tab-content active">
        <div class="d-flex gap-2 mb-3">
            <select id="adminSeries" class="form-select" style="flex:1;"><option value="">选择系列</option><option value="SMART">SMART</option><option value="Venus">Venus</option></select>
            <select id="adminModel" class="form-select" style="flex:1;" disabled><option>请先选择系列</option></select>
            <button class="ivd-btn" onclick="loadRules()"><i class="fas fa-folder-open me-1"></i> 加载规则</button>
        </div>
        <div id="rulesList"></div>
        <hr style="margin:30px 0;">
        <h3><i class="fas fa-plus-circle me-1"></i> 添加规则</h3>
        <div class="mb-3"><input id="newKeywords" class="form-control" placeholder="关键词（逗号分隔）"></div>
        <div class="mb-3"><textarea id="newAdvice" class="form-control" rows="3" placeholder="建议内容"></textarea></div>
        <button class="ivd-btn ivd-btn-success" onclick="addRule()"><i class="fas fa-save me-1"></i> 保存</button>
    </div>
    <div id="tab-pdf" class="ivd-tab-content">
        <h3><i class="fas fa-file-pdf me-1"></i> PDF知识库导入</h3>
        <p class="text-secondary">将PDF故障文档导入知识库，自动提取电机状态数据</p>
        <div class="d-flex gap-2 mb-3">
            <select id="pdfSeries" class="form-select" style="flex:1;"><option value="">选择系列</option><option value="SMART">SMART</option><option value="Venus">Venus</option></select>
            <select id="pdfModel" class="form-select" style="flex:1;" disabled><option>请先选择系列</option></select>
        </div>
        <div class="upload-area" onclick="document.getElementById('pdfFileInput').click()">
            <div style="font-size:1.2rem;"><i class="fas fa-cloud-upload-alt me-2"></i>点击选择PDF文件</div>
            <div style="font-size:0.9rem;color:#999;">支持 .pdf 格式</div>
            <input type="file" id="pdfFileInput" accept=".pdf" style="display:none" onchange="handlePdfSelect(event)">
            <div id="pdfFileName" style="margin-top:10px;font-weight:500;color:#1e6f9f;"></div>
        </div>
        <button class="ivd-btn ivd-btn-success w-100 py-3" onclick="importPdf()" id="importPdfBtn"><i class="fas fa-upload me-1"></i> 导入PDF知识库</button>
        <div id="pdfResult" class="mt-3"></div>
        <hr style="margin:30px 0;">
        <div class="d-flex flex-wrap gap-2 align-items-center">
            <h3 style="margin:0;color:#1e6f9f;"><i class="fas fa-table me-1"></i> 电机状态数据</h3>
            <span class="text-secondary" style="font-size:0.9rem;">(选择系列和型号后查看)</span>
            <button class="ivd-btn ivd-btn-sm" onclick="loadMotorStatus()"><i class="fas fa-sync me-1"></i> 刷新</button>
            <button class="ivd-btn ivd-btn-danger ivd-btn-sm" onclick="clearMotorStatus()"><i class="fas fa-trash me-1"></i> 清空</button>
            <button class="ivd-btn ivd-btn-success ivd-btn-sm" onclick="exportMotorStatus()"><i class="fas fa-file-csv me-1"></i> 导出CSV</button>
        </div>
        <div id="motorStatusList" class="mt-3"><div class="result-box result-info"><i class="fas fa-info-circle me-1"></i> 请选择系列和型号后点击"刷新"</div></div>
    </div>
    <div id="tab-versions" class="ivd-tab-content"><div id="versionList"><div class="result-box result-info"><i class="fas fa-spinner fa-spin me-1"></i> 加载中...</div></div></div>
</div>

<script>
// ========== 全局变量 ==========
let motorStatusOffset = 0;
let motorStatusLimit = 50;
let motorStatusTotal = 0;
let motorStatusModel = '';
let motorStatusLoading = false;

// ========== 系列联动 ==========
document.getElementById('adminSeries').onchange = async function() {
    const series = this.value;
    if (!series) { document.getElementById('adminModel').disabled = true; return; }
    const resp = await fetch(`/api/models?series=${series}`);
    const models = await resp.json();
    const sel = document.getElementById('adminModel');
    sel.disabled = false;
    sel.innerHTML = '<option value="">选择型号</option>' + models.map(m => `<option value="${m.name}">${m.name}</option>`).join('');
};

document.getElementById('pdfSeries').onchange = async function() {
    const series = this.value;
    if (!series) { document.getElementById('pdfModel').disabled = true; return; }
    const resp = await fetch(`/api/models?series=${series}`);
    const models = await resp.json();
    const sel = document.getElementById('pdfModel');
    sel.disabled = false;
    sel.innerHTML = '<option value="">选择型号</option>' + models.map(m => `<option value="${m.name}">${m.name}</option>`).join('');
    document.getElementById('motorStatusList').innerHTML = '<div class="result-box result-info"><i class="fas fa-info-circle me-1"></i> 请选择系列和型号后点击"刷新"</div>';
};

document.getElementById('pdfModel').onchange = function() {
    if (this.value) loadMotorStatus();
};

// ========== Tab切换 ==========
function switchTab(name) {
    document.querySelectorAll('.ivd-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.ivd-tab-content').forEach(t => t.classList.remove('active'));
    const tabMap = {'rules':0, 'pdf':1, 'versions':2};
    document.querySelectorAll('.ivd-tab')[tabMap[name]].classList.add('active');
    document.getElementById(`tab-${name}`).classList.add('active');
    if (name === 'versions') loadVersions();
    if (name === 'pdf') {
        const series = document.getElementById('pdfSeries').value;
        const model = document.getElementById('pdfModel').value;
        if (series && model) loadMotorStatus();
        else document.getElementById('motorStatusList').innerHTML = '<div class="result-box result-info"><i class="fas fa-info-circle me-1"></i> 请选择系列和型号后点击"刷新"</div>';
    }
}

// ========== 规则管理 ==========
async function loadRules() {
    const series = document.getElementById('adminSeries').value;
    const model = document.getElementById('adminModel').value;
    if (!series || !model) { alert('请选择系列和型号'); return; }
    const resp = await fetch(`/api/rules?series=${series}&model=${model}`);
    const rules = await resp.json();
    const container = document.getElementById('rulesList');
    if (rules.length === 0) { container.innerHTML = '<div class="text-secondary text-center py-3">暂无规则</div>'; return; }
    let html = '';
    rules.forEach(r => {
        html += `<div class="rule-item">
            <div><strong>ID: ${r.id}</strong> <span class="badge ${r.source === 'pdf' ? 'badge-pdf' : 'badge-manual'}">${r.source === 'pdf' ? '📖 PDF导入' : '⚙️ 手动'}</span></div>
            <div class="mb-2"><input id="kw_${r.id}" class="form-control" value="${r.keywords.join(', ')}"></div>
            <div class="mb-2"><textarea id="adv_${r.id}" class="form-control" rows="2">${r.advice}</textarea></div>
            <div class="actions">
                <button class="ivd-btn ivd-btn-sm" onclick="updateRule(${r.id})"><i class="fas fa-save me-1"></i> 保存</button>
                <button class="ivd-btn ivd-btn-danger ivd-btn-sm" onclick="deleteRule(${r.id})"><i class="fas fa-trash me-1"></i> 删除</button>
            </div>
        </div>`;
    });
    container.innerHTML = html;
}

async function updateRule(id) {
    const keywords = document.getElementById(`kw_${id}`).value.trim();
    const advice = document.getElementById(`adv_${id}`).value.trim();
    if (!keywords || !advice) { alert('请填写完整'); return; }
    const resp = await fetch(`/api/rules/${id}`, { method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({keywords, advice}) });
    const data = await resp.json();
    if (data.success) { alert('✅ 更新成功'); loadRules(); } else alert('❌ 更新失败: ' + data.error);
}

async function deleteRule(id) {
    if (!confirm('确定删除？')) return;
    const resp = await fetch(`/api/rules/${id}`, { method:'DELETE' });
    const data = await resp.json();
    if (data.success) { alert('✅ 删除成功'); loadRules(); } else alert('❌ 删除失败: ' + data.error);
}

async function addRule() {
    const series = document.getElementById('adminSeries').value;
    const model = document.getElementById('adminModel').value;
    const keywords = document.getElementById('newKeywords').value.trim();
    const advice = document.getElementById('newAdvice').value.trim();
    if (!series || !model) { alert('请选择系列和型号'); return; }
    if (!keywords || !advice) { alert('请填写完整'); return; }
    const resp = await fetch('/api/rules', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({series, model, keywords, advice}) });
    const data = await resp.json();
    if (data.success) { alert('✅ 添加成功'); document.getElementById('newKeywords').value = ''; document.getElementById('newAdvice').value = ''; loadRules(); }
    else alert('❌ 添加失败: ' + data.error);
}

// ========== PDF导入 ==========
let selectedPdfFile = null;
function handlePdfSelect(event) {
    if (event.target.files.length) {
        selectedPdfFile = event.target.files[0];
        document.getElementById('pdfFileName').innerHTML = `<i class="fas fa-check-circle text-success me-1"></i> ${selectedPdfFile.name} (${(selectedPdfFile.size/1024).toFixed(0)} KB)`;
        document.getElementById('pdfResult').innerHTML = '';
    }
}

async function importPdf() {
    const series = document.getElementById('pdfSeries').value;
    const model = document.getElementById('pdfModel').value;
    if (!series || !model) { document.getElementById('pdfResult').innerHTML = '<div class="result-box result-error"><i class="fas fa-exclamation-triangle me-1"></i> 请选择系列和型号</div>'; return; }
    if (!selectedPdfFile) { document.getElementById('pdfResult').innerHTML = '<div class="result-box result-error"><i class="fas fa-exclamation-triangle me-1"></i> 请选择PDF文件</div>'; return; }
    if (!selectedPdfFile.name.toLowerCase().endsWith('.pdf')) { document.getElementById('pdfResult').innerHTML = '<div class="result-box result-error"><i class="fas fa-exclamation-triangle me-1"></i> 请上传PDF格式文件</div>'; return; }
    const btn = document.getElementById('importPdfBtn');
    const resultDiv = document.getElementById('pdfResult');
    btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i> 导入中...';
    resultDiv.innerHTML = '<div class="result-box result-info"><i class="fas fa-spinner fa-spin me-1"></i> 处理中，请稍候...</div>';
    const fd = new FormData(); fd.append('file', selectedPdfFile); fd.append('series', series); fd.append('model', model);
    try {
        const resp = await fetch('/api/import_pdf', { method:'POST', body:fd });
        const data = await resp.json();
        if (data.success) {
            resultDiv.innerHTML = `<div class="result-box result-success"><i class="fas fa-check-circle me-1"></i> ${data.message} (提取了 ${data.total_extracted || 0} 条，导入了 ${data.count} 条)</div>`;
            selectedPdfFile = null; document.getElementById('pdfFileInput').value = ''; document.getElementById('pdfFileName').innerHTML = '';
            loadMotorStatus();
        } else {
            resultDiv.innerHTML = `<div class="result-box result-error"><i class="fas fa-exclamation-triangle me-1"></i> ${data.error}</div>`;
        }
    } catch (err) {
        resultDiv.innerHTML = `<div class="result-box result-error"><i class="fas fa-exclamation-triangle me-1"></i> 导入失败: ${err.message}</div>`;
        console.error('PDF导入错误:', err);
    } finally {
        btn.disabled = false; btn.innerHTML = '<i class="fas fa-upload me-1"></i> 导入PDF知识库';
    }
}

// ========== 电机状态数据（分页加载） ==========
async function loadMotorStatus() {
    motorStatusOffset = 0;
    motorStatusTotal = 0;
    motorStatusModel = document.getElementById('pdfModel').value;
    const container = document.getElementById('motorStatusList');
    container.innerHTML = '<div class="result-box result-info"><i class="fas fa-spinner fa-spin me-1"></i> 加载中...</div>';
    if (!motorStatusModel) {
        container.innerHTML = '<div class="result-box result-info"><i class="fas fa-info-circle me-1"></i> 请先选择型号</div>';
        return;
    }
    try {
        const resp = await fetch(`/api/motor_status?model=${encodeURIComponent(motorStatusModel)}&limit=${motorStatusLimit}&offset=0`);
        const data = await resp.json();
        motorStatusTotal = data.total;
        if (data.total === 0) {
            container.innerHTML = `<div class="result-box result-info"><i class="fas fa-inbox me-1"></i> 型号 "${motorStatusModel}" 暂无电机状态数据</div>`;
            return;
        }
        renderMotorStatusTable(data.data, false);
        motorStatusOffset = data.data.length;
        showLoadMoreButton();
    } catch (err) {
        container.innerHTML = `<div class="result-box result-error"><i class="fas fa-exclamation-triangle me-1"></i> 加载失败: ${err.message}</div>`;
    }
}

function renderMotorStatusTable(data, append) {
    const container = document.getElementById('motorStatusList');
    const series = document.getElementById('pdfSeries').value || 'SMART';
    let html = '';
    if (!append) {
        html = `<div class="result-box result-success"><i class="fas fa-check-circle me-1"></i> ${series}/${motorStatusModel} - 共 ${motorStatusTotal} 条电机状态数据</div>`;
        html += `<div class="table-container"><table class="table table-striped table-hover"><thead><tr><th>板卡</th><th>电机</th><th>状态</th><th>完整描述</th></tr></thead><tbody>`;
        data.forEach(row => {
            html += `<tr><td><strong>${row.board_card || ''}</strong></td><td><strong>${row.motor_code || ''}</strong></td><td><strong>${row.status_code || ''}</strong></td><td>${row.full_description || row.description || '-'}</td></tr>`;
        });
        html += `</tbody></table></div>`;
        html += `<div id="motorStatusFooter" class="text-secondary mt-2">已加载 ${data.length} 条，共 ${motorStatusTotal} 条</div>`;
        html += `<div id="loadMoreContainer"></div>`;
        container.innerHTML = html;
    } else {
        const tbody = container.querySelector('tbody');
        if (!tbody) return;
        data.forEach(row => {
            const tr = document.createElement('tr');
            tr.innerHTML = `<td><strong>${row.board_card || ''}</strong></td><td><strong>${row.motor_code || ''}</strong></td><td><strong>${row.status_code || ''}</strong></td><td>${row.full_description || row.description || '-'}</td>`;
            tbody.appendChild(tr);
        });
        updateFooter();
    }
}

function updateFooter() {
    const footer = document.getElementById('motorStatusFooter');
    if (footer) footer.textContent = `已加载 ${motorStatusOffset} 条，共 ${motorStatusTotal} 条`;
}

function showLoadMoreButton() {
    const container = document.getElementById('loadMoreContainer');
    if (!container) return;
    if (motorStatusOffset >= motorStatusTotal) {
        container.innerHTML = `<div class="text-secondary"><i class="fas fa-check-circle text-success me-1"></i> 已加载全部 ${motorStatusTotal} 条</div>`;
        return;
    }
    container.innerHTML = `<button class="ivd-btn ivd-btn-sm" onclick="loadMoreMotorStatus()" style="width:auto;padding:8px 20px;margin:10px auto;display:block;"><i class="fas fa-arrow-down me-1"></i> 加载更多 50 条 (已加载 ${motorStatusOffset}/${motorStatusTotal})</button>`;
}

async function loadMoreMotorStatus() {
    if (motorStatusLoading) return;
    if (motorStatusOffset >= motorStatusTotal) {
        alert('已全部加载');
        return;
    }
    motorStatusLoading = true;
    const btn = document.querySelector('#loadMoreContainer button');
    if (btn) btn.disabled = true;
    try {
        const resp = await fetch(`/api/motor_status?model=${encodeURIComponent(motorStatusModel)}&limit=${motorStatusLimit}&offset=${motorStatusOffset}`);
        const data = await resp.json();
        if (data.data.length === 0) {
            showLoadMoreButton();
            motorStatusLoading = false;
            return;
        }
        renderMotorStatusTable(data.data, true);
        motorStatusOffset += data.data.length;
        updateFooter();
        showLoadMoreButton();
    } catch (err) {
        alert('加载更多失败: ' + err.message);
    } finally {
        motorStatusLoading = false;
        if (btn) btn.disabled = false;
    }
}

async function clearMotorStatus() {
    const model = document.getElementById('pdfModel').value;
    if (!model) { alert('⚠️ 请先选择型号'); return; }
    if (!confirm(`⚠️ 确定要清空 ${model} 的所有电机状态数据吗？此操作不可恢复！`)) return;
    try {
        const resp = await fetch(`/api/motor_status/clear?model=${encodeURIComponent(model)}`, { method:'DELETE' });
        const data = await resp.json();
        if (data.success) {
            alert('✅ 已清空所有数据');
            motorStatusOffset = 0; motorStatusTotal = 0;
            loadMotorStatus();
        } else alert('❌ 清空失败: ' + data.error);
    } catch (err) { alert('❌ 清空失败: ' + err.message); }
}

async function exportMotorStatus() {
    const model = document.getElementById('pdfModel').value;
    if (!model) { alert('⚠️ 请先选择型号'); return; }
    try {
        const resp = await fetch(`/api/motor_status?model=${encodeURIComponent(model)}&limit=10000`);
        const data = await resp.json();
        if (data.total === 0) { alert('⚠️ 没有数据可导出'); return; }
        let csv = '板卡,电机,状态,完整描述\\n';
        data.data.forEach(row => {
            const desc = row.full_description || row.description || '';
            csv += `${row.board_card},${row.motor_code},${row.status_code},"${desc.replace(/"/g,'""')}"\\n`;
        });
        const blob = new Blob(['\\uFEFF' + csv], { type: 'text/csv;charset=utf-8;' });
        const link = document.createElement('a');
        link.href = URL.createObjectURL(blob);
        link.download = `motor_status_${model}_${new Date().toISOString().slice(0,10)}.csv`;
        link.click();
    } catch (err) { alert('❌ 导出失败: ' + err.message); }
}

// ========== 版本历史 ==========
async function loadVersions() {
    document.getElementById('versionList').innerHTML = '<div class="result-box result-info"><i class="fas fa-info-circle me-1"></i> 📜 版本历史功能开发中...</div>';
}
</script>
</body>
</html>
'''
# ========== ADMIN_HTML 结束 ==========

# ========== 模板 3: ANALYSIS_HTML (分析结果视图) ==========
ANALYSIS_HTML = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>IVD 分析报告 - 层级视图</title>
    <!-- Font Awesome 6 (已有) -->
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <!-- 新增 Bootstrap 5 CSS -->
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        :root {
            --primary: #2563eb;
            --primary-dark: #1e40af;
            --primary-light: #3b82f6;
            --success: #10b981;
            --warning: #f59e0b;
            --danger: #ef4444;
            --info: #06b6d4;
            --purple: #8b5cf6;
            --gray-50: #f9fafb;
            --gray-100: #f3f4f6;
            --gray-200: #e5e7eb;
            --gray-300: #d1d5db;
            --gray-600: #4b5563;
            --gray-700: #374151;
            --gray-800: #1f2937;
            --shadow-sm: 0 1px 2px 0 rgba(0, 0, 0, 0.05);
            --shadow: 0 1px 3px 0 rgba(0, 0, 0, 0.1), 0 1px 2px -1px rgba(0, 0, 0, 0.1);
            --shadow-md: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -2px rgba(0, 0, 0, 0.1);
            --shadow-lg: 0 10px 15px -3px rgba(0, 0, 0, 0.1), 0 4px 6px -4px rgba(0, 0, 0, 0.1);
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; 
            background: var(--gray-100);
            height: 100vh; 
            overflow: hidden; 
            font-size: 13px; 
        }
        .app { display: flex; flex-direction: column; height: 100vh; }
        .topbar {
            background: linear-gradient(135deg, var(--primary) 0%, var(--primary-dark) 100%);
            color: white; 
            padding: 10px 24px; 
            display: flex; 
            align-items: center;
            justify-content: space-between; 
            flex-shrink: 0; 
            z-index: 10;
            box-shadow: var(--shadow-lg);
            font-size: 0.85rem;
        }
        .topbar h2 { 
            font-size: 1.1rem; 
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .topbar h2 i {
            font-size: 1.2rem;
            opacity: 0.9;
        }
        .topbar .meta { 
            font-size: 0.75rem; 
            opacity: 0.95; 
            display: flex; 
            gap: 12px; 
            align-items: center; 
            flex-wrap: wrap; 
        }
        .topbar .meta span { 
            background: rgba(255,255,255,0.2); 
            padding: 4px 12px; 
            border-radius: 20px;
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255,255,255,0.1);
        }
        .topbar a { 
            color: white; 
            text-decoration: none; 
            font-size: 0.78rem; 
            opacity: 0.9; 
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            padding: 4px 12px;
            border-radius: 16px;
            background: rgba(255,255,255,0.1);
            display: flex;
            align-items: center;
            gap: 6px;
            cursor: pointer !important;
        }
        .topbar a:hover { 
            opacity: 1;
            background: rgba(255,255,255,0.2);
            transform: translateY(-2px) scale(1.05);
            box-shadow: 0 4px 12px rgba(0,0,0,0.2);
        }
        .topbar a:active {
            transform: translateY(0) scale(0.95);
        }
        .topbar a i {
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            display: inline-block;
        }
        .topbar a:hover i {
            transform: scale(1.2) rotate(10deg);
        }
        .topbar label {
            cursor: pointer !important;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }
        .topbar label:hover {
            transform: translateY(-2px) scale(1.05);
            background: rgba(255,255,255,0.2) !important;
            box-shadow: 0 4px 12px rgba(0,0,0,0.2);
        }
        .topbar label:active {
            transform: translateY(0) scale(0.95);
        }
        .topbar label i {
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            display: inline-block;
        }
        .topbar label:hover i {
            transform: scale(1.2) rotate(10deg);
        }
        .main { display: flex; flex: 1; overflow: hidden; }
        .left-panel {
            width: 260px; 
            min-width: 200px; 
            background: white; 
            display: flex;
            flex-direction: column; 
            border-right: 1px solid var(--gray-200);
            box-shadow: var(--shadow-md); 
            z-index: 5;
            font-size: 0.82rem;
        }
        .left-header { 
            padding: 12px 14px; 
            border-bottom: 1px solid var(--gray-200); 
            flex-shrink: 0;
            background: var(--gray-50);
            position: relative;
        }
        .search-wrapper {
            position: relative;
            width: 100%;
        }
        .search-icon {
            position: absolute;
            left: 12px;
            top: 50%;
            transform: translateY(-50%);
            color: var(--gray-500);
            font-size: 0.9rem;
            cursor: pointer;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            z-index: 1;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 24px;
            height: 24px;
            border-radius: 6px;
        }
        .search-icon:hover {
            color: var(--primary);
            transform: translateY(-50%) scale(1.2);
            background: rgba(0, 168, 204, 0.1);
        }
        .search-icon:active {
            transform: translateY(-50%) scale(0.9);
        }
        .left-header input {
            width: 100%; 
            padding: 8px 14px 8px 36px; 
            border: 2px solid var(--gray-200); 
            border-radius: 20px;
            font-size: 0.8rem; 
            outline: none; 
            transition: all 0.3s ease;
            background: white;
        }
        .left-header input:focus { 
            border-color: var(--primary);
            box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.1);
        }
        .left-header input:focus + .search-icon,
        .left-header input:not(:placeholder-shown) + .search-icon {
            color: var(--primary);
            animation: search-pulse 1s ease infinite;
        }
        @keyframes search-pulse {
            0%, 100% { transform: translateY(-50%) scale(1); }
            50% { transform: translateY(-50%) scale(1.1); }
        }
        .left-header .summary-row { 
            display: flex; 
            gap: 8px; 
            margin-top: 10px; 
            font-size: 0.7rem; 
            color: var(--gray-600); 
            flex-wrap: wrap; 
        }
        .left-header .summary-row .tag { 
            padding: 4px 10px; 
            border-radius: 12px; 
            font-weight: 600; 
            font-size: 0.7rem;
            box-shadow: var(--shadow-sm);
            display: flex;
            align-items: center;
            gap: 4px;
            cursor: pointer !important;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            position: relative;
            overflow: hidden;
            user-select: none;
        }
        .left-header .summary-row .tag:hover {
            transform: translateY(-2px) scale(1.08);
            box-shadow: 0 6px 16px rgba(0,0,0,0.2);
        }
        .left-header .summary-row .tag:active {
            transform: translateY(0) scale(0.96);
        }
        .left-header .summary-row .tag.active {
            transform: scale(1.1);
            box-shadow: 0 8px 20px rgba(0,0,0,0.25);
        }
        .left-header .summary-row .tag i {
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            display: inline-block;
        }
        .left-header .summary-row .tag:hover i {
            transform: scale(1.3) rotate(10deg);
        }
        .left-header .summary-row .tag:active i {
            transform: scale(0.9);
        }
        .left-header .summary-row .tag.active i {
            animation: icon-bounce 0.6s ease infinite;
        }
        .tag-fault { background: linear-gradient(135deg, #fef2f2 0%, #fee2e2 100%); color: #dc2626; border: 1px solid #fecaca; cursor: pointer !important; }
        .tag-fault:hover { box-shadow: 0 6px 16px rgba(220,38,38,0.3) !important; }
        .tag-sample { background: linear-gradient(135deg, #eff6ff 0%, #dbeafe 100%); color: #1d4ed8; border: 1px solid #bfdbfe; cursor: pointer !important; }
        .tag-sample:hover { box-shadow: 0 6px 16px rgba(29,78,216,0.3) !important; }
        .tag-reagent { background: linear-gradient(135deg, #faf5ff 0%, #f3e8ff 100%); color: #7c3aed; border: 1px solid #e9d5ff; cursor: pointer !important; }
        .tag-reagent:hover { box-shadow: 0 6px 16px rgba(124,58,237,0.3) !important; }
        .tag-receive { background: linear-gradient(135deg, #f0fdf4 0%, #dcfce7 100%); color: #15803d; border: 1px solid #bbf7d0; cursor: pointer !important; }
        .tag-receive:hover { box-shadow: 0 6px 16px rgba(21,128,61,0.3) !important; }
        .left-tree { flex: 1; overflow-y: auto; padding: 8px 0; }
        .date-group { margin: 8px 0; }
        .date-header {
            padding: 10px 16px; 
            cursor: pointer; 
            display: flex; 
            align-items: center; 
            gap: 10px;
            font-weight: 600; 
            font-size: 0.85rem; 
            color: var(--gray-800);
            transition: all 0.2s ease;
            user-select: none; 
            border-left: 4px solid transparent;
            background: linear-gradient(135deg, #f8fafc 0%, #f1f5f9 100%);
            margin: 2px 10px;
            border-radius: 10px;
            box-shadow: var(--shadow-sm);
        }
        .date-header:hover { 
            background: linear-gradient(135deg, #eff6ff 0%, #dbeafe 100%);
            border-left-color: var(--primary);
        }
        .date-header .arrow { 
            font-size: 0.7rem; 
            transition: transform 0.3s ease; 
            color: var(--primary);
            width: 14px;
        }
        .date-header.collapsed .arrow { transform: rotate(-90deg); }
        .date-header .date-text { flex: 1; display: flex; align-items: center; gap: 8px; font-weight: 600; }
        .date-icon {
            color: var(--primary);
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 22px;
            height: 22px;
            border-radius: 6px;
        }
        .date-header:hover .date-icon {
            transform: scale(1.2) rotate(10deg);
            background: rgba(37,99,235,0.1);
        }
        .date-header:active .date-icon {
            transform: scale(0.9);
        }
        .date-header .count { 
            font-size: 0.7rem; 
            color: var(--gray-700);
            font-weight: 600;
            background: white;
            padding: 3px 10px;
            border-radius: 12px;
            border: 1px solid var(--gray-200);
        }
        .file-list { overflow: hidden; padding: 4px 0; }
        .file-list.collapsed { display: none; }
        .file-node {
            padding: 8px 16px 8px 36px; 
            cursor: pointer; 
            display: flex;
            align-items: center;
            gap: 10px; 
            font-size: 0.78rem; 
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            border-left: 3px solid transparent;
            color: var(--gray-700);
            margin: 2px 10px;
            border-radius: 8px;
            background: white;
            position: relative;
            overflow: hidden;
        }
        .file-node::before {
            content: '';
            position: absolute;
            top: 50%;
            left: 50%;
            width: 0;
            height: 0;
            border-radius: 50%;
            background: rgba(0, 168, 204, 0.1);
            transform: translate(-50%, -50%);
            transition: width 0.5s, height 0.5s;
        }
        .file-node:hover { 
            background: linear-gradient(135deg, #f0f9ff 0%, #e0f2fe 100%);
            border-left-color: var(--primary);
            transform: translateX(4px);
        }
        .file-node:active::before {
            width: 300px;
            height: 300px;
        }
        .file-node:active {
            transform: translateX(2px) scale(0.98);
        }
        .file-node.active { 
            background: linear-gradient(135deg, #dbeafe 0%, #bfdbfe 100%);
            border-left-color: var(--primary);
            color: var(--primary-dark);
            font-weight: 600;
            box-shadow: var(--shadow);
            animation: pulse-border 2s infinite;
        }
        @keyframes pulse-border {
            0%, 100% { border-left-width: 3px; }
            50% { border-left-width: 5px; }
        }
        .file-node .icon { 
            font-size: 0.9rem; 
            flex-shrink: 0; 
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 24px;
            height: 24px;
            border-radius: 6px;
        }
        .file-node:hover .icon {
            transform: scale(1.2) rotate(5deg);
        }
        .file-node:active .icon {
            transform: scale(0.9);
        }
        .file-node .fname { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 500; }
        .file-node .fsize { font-size: 0.68rem; color: var(--gray-600); flex-shrink: 0; font-weight: 500; }
        .file-node .type-tags { display: flex; gap: 4px; flex-shrink: 0; }
        .file-node .type-tag { 
            padding: 3px 8px; 
            border-radius: 10px; 
            font-size: 0.6rem; 
            font-weight: 700;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            cursor: pointer;
        }
        .file-node .type-tag:hover {
            transform: scale(1.15) translateY(-2px);
            box-shadow: 0 4px 12px rgba(0,0,0,0.15);
        }
        .file-node .type-tag:active {
            transform: scale(0.95);
        }
        .type-tag-fault { background: linear-gradient(135deg, #fef2f2 0%, #fee2e2 100%); color: #dc2626; border: 1px solid #fecaca; }
        .type-tag-sample { background: linear-gradient(135deg, #eff6ff 0%, #dbeafe 100%); color: #1d4ed8; border: 1px solid #bfdbfe; }
        .type-tag-reagent { background: linear-gradient(135deg, #faf5ff 0%, #f3e8ff 100%); color: #7c3aed; border: 1px solid #e9d5ff; }
        .type-tag-receive { background: linear-gradient(135deg, #f0fdf4 0%, #dcfce7 100%); color: #15803d; border: 1px solid #bbf7d0; }
        .left-footer { 
            padding: 10px 14px; 
            border-top: 1px solid #f0f4f8; 
            text-align: center; 
            flex-shrink: 0;
            background: linear-gradient(135deg, #f8fafc 0%, #f0f4f8 100%);
        }
        .btn-load-more {
            padding: 6px 16px; 
            background: linear-gradient(135deg, #ecfdf5 0%, #d1fae5 100%);
            border: 2px solid #10b981;
            border-radius: 20px; 
            cursor: pointer !important; 
            font-size: 0.72rem; 
            color: #047857;
            font-weight: 600; 
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1); 
            width: 100%;
            box-shadow: 0 2px 8px rgba(16,185,129,0.15);
        }
        .btn-load-more:hover { 
            background: linear-gradient(135deg, #d1fae5 0%, #a7f3d0 100%);
            transform: translateY(-2px) scale(1.02);
            box-shadow: 0 6px 16px rgba(16,185,129,0.3);
        }
        .btn-load-more:active {
            transform: translateY(0) scale(0.98);
        }
        .btn-load-more:disabled { opacity: 0.5; cursor: not-allowed !important; transform: none; }
        .right-panel {
            flex: 1; 
            display: flex; 
            flex-direction: column; 
            background: linear-gradient(135deg, #f8fafc 0%, #f0f4f8 100%);
            overflow: hidden;
        }
        .right-placeholder {
            display: flex; 
            align-items: center; 
            justify-content: center;
            height: 100%; 
            color: #94a3b8;
            font-size: 0.95rem; 
            flex-direction: column; 
            gap: 12px;
        }
        .right-placeholder::before {
            content: '📋';
            font-size: 4rem;
            opacity: 0.3;
        }
        .right-content { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
        .right-header {
            padding: 10px 20px; 
            background: white; 
            border-bottom: 1px solid #e0e4e8;
            display: flex; 
            align-items: center; 
            justify-content: space-between; 
            flex-shrink: 0;
            font-size: 0.85rem;
            box-shadow: 0 2px 8px rgba(0,0,0,0.04);
        }
        .right-header .file-title { 
            font-weight: 600; 
            font-size: 0.95rem; 
            color: #0084a8;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .right-header .file-meta { font-size: 0.7rem; color: #64748b; }
        .btn-modal {
            padding: 6px 16px; 
            background: linear-gradient(135deg, #00a8cc 0%, #0084a8 100%);
            color: white;
            border: none;
            border-radius: 20px;
            cursor: pointer !important;
            font-size: 0.72rem;
            font-weight: 600;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            box-shadow: 0 2px 8px rgba(0,168,204,0.25);
        }
        .btn-modal:hover { 
            transform: translateY(-2px) scale(1.05);
            box-shadow: 0 6px 16px rgba(0,168,204,0.4);
        }
        .btn-modal:active {
            transform: translateY(0) scale(0.96);
        }
        .btn-modal i {
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            display: inline-block;
        }
        .btn-modal:hover i {
            transform: scale(1.2) rotate(10deg);
        }
        .btn-view-full {
            padding: 6px 14px;
            background: linear-gradient(135deg, var(--primary) 0%, var(--primary-dark) 100%);
            color: white;
            border: none;
            border-radius: 20px;
            cursor: pointer !important;
            font-size: 0.72rem;
            font-weight: 600;
            box-shadow: var(--shadow-md);
            display: flex;
            align-items: center;
            gap: 6px;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }
        .btn-view-full:hover {
            transform: translateY(-2px) scale(1.05);
            box-shadow: 0 6px 16px rgba(37,99,235,0.4);
        }
        .btn-view-full:active {
            transform: translateY(0) scale(0.96);
        }
        .btn-view-full i {
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            display: inline-block;
        }
        .btn-view-full:hover i {
            transform: scale(1.2) rotate(10deg);
        }
        .right-body { flex: 1; overflow-y: auto; padding: 14px 18px; }
        .content-section { 
            background: white; 
            border-radius: 12px; 
            padding: 16px; 
            margin-bottom: 8px;
            border: 1px solid #e0e4e8;
            box-shadow: 0 2px 8px rgba(0,0,0,0.04);
        }
        .content-section .section-label { 
            font-size: 0.8rem;
            font-weight: 700;
            color: #0084a8;
            margin-bottom: 6px;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .content-section .section-label::before {
            content: '';
            width: 4px;
            height: 16px;
            background: linear-gradient(135deg, #00a8cc 0%, #0084a8 100%);
            border-radius: 2px;
        }
        .raw-content {
            white-space: pre-wrap;
            word-break: break-word;
            font-family: 'Cascadia Code', 'Fira Code', 'Consolas', monospace;
            font-size: 0.75rem;
            line-height: 1.6;
            color: #334155;
            background: linear-gradient(135deg, #fafbfc 0%, #f1f5f9 100%);
            padding: 14px;
            border-radius: 8px;
            border: 1px solid #e2e8f0;
            max-height: calc(100vh - 300px);
            min-height: 300px;
            overflow-y: auto;
        }
        .db-analysis-section { margin-top: 8px; }
        .db-separator {
            display: flex;
            align-items: center;
            gap: 10px;
            margin: 8px 0;
            color: #0084a8;
            font-weight: 600;
            font-size: 0.8rem;
        }
        .db-separator::before, .db-separator::after {
            content: '';
            flex: 1;
            height: 2px;
            background: linear-gradient(to right, transparent, #00a8cc, transparent);
        }
        .db-match-item {
            background: linear-gradient(135deg, #f0fdf4 0%, #dcfce7 100%);
            border: 1px solid #bbf7d0;
            border-radius: 10px;
            padding: 8px;
            margin: 6px 0;
            border-left: 4px solid #22c55e;
            font-size: 0.78rem;
            box-shadow: 0 2px 8px rgba(34,197,94,0.1);
        }
        .db-match-item .match-header { 
            font-weight: 600;
            font-size: 0.8rem;
            color: #166534;
            margin-bottom: 6px;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .db-match-item .match-row { font-size: 0.75rem; margin: 2px 0; line-height: 1.4; }
        .db-match-item .match-row strong { color: #475569; }
        .db-match-item .match-original {
            background: linear-gradient(135deg, #fefce8 0%, #fef9c3 100%);
            border: 1px solid #fde047;
            padding: 4px 8px;
            border-radius: 6px;
            margin: 2px 0;
            font-family: monospace;
            font-size: 0.72rem;
            white-space: pre-wrap;
            word-break: break-word;
        }
        .empty-state { 
            text-align: center;
            padding: 50px 20px;
            color: #94a3b8;
            font-size: 0.85rem;
        }
        .empty-state .icon { font-size: 3rem; margin-bottom: 10px; opacity: 0.4; }
        .modal-overlay {
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(0,0,0,0.7);
            justify-content: center;
            align-items: center;
            z-index: 9999;
            padding: 20px;
            backdrop-filter: blur(8px);
        }
        .modal-card {
            background: white;
            border-radius: 16px;
            width: min(100%, 1400px);
            max-height: 95vh;
            overflow: hidden;
            display: flex;
            flex-direction: column;
            box-shadow: 0 25px 80px rgba(0,0,0,0.35);
        }
        .modal-header {
            padding: 16px 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 2px solid #f0f4f8;
            flex-shrink: 0;
            background: linear-gradient(135deg, #f8fafc 0%, #f0f4f8 100%);
        }
        .modal-title { 
            font-size: 1.05rem;
            font-weight: 700;
            color: #0084a8;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .modal-close { 
            border: none;
            background: #f1f5f9;
            color: #64748b;
            width: 32px;
            height: 32px;
            border-radius: 50%;
            cursor: pointer !important;
            font-size: 1.2rem;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }
        .modal-close:hover { 
            background: #fee2e2;
            color: #ef4444;
            transform: scale(1.15) rotate(90deg);
            box-shadow: 0 4px 12px rgba(239,68,68,0.3);
        }
        .modal-close:active {
            transform: scale(0.9);
        }
        .modal-body { 
            padding: 20px;
            overflow-y: auto;
            white-space: pre-wrap;
            word-break: break-word;
            font-family: monospace;
            font-size: 0.78rem;
            line-height: 1.6;
            color: #334155;
            flex: 1;
        }
        @keyframes pulse {
            0%, 100% { opacity: 1; transform: scale(1); }
            50% { opacity: 0.7; transform: scale(1.1); }
        }
        @media (max-width: 768px) {
            .main { flex-direction: column; }
            .left-panel { width: 100%; min-width: 0; max-height: 40vh; }
            .right-panel { flex: 1; }
        }
        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { 
            background: linear-gradient(135deg, #00a8cc 0%, #0084a8 100%);
            border-radius: 3px;
        }
        ::-webkit-scrollbar-thumb:hover { background: #0084a8; }
        .highlight { 
            background: linear-gradient(135deg, #fef3c7 0%, #fde68a 100%);
            padding: 2px 6px;
            border-radius: 4px;
        }
        #uploadedInfo { font-size: 0.75rem; color: #475569; margin-top: 6px; }
        .date-group .file-list .file-node:last-child { margin-bottom: 4px; }
        
        .line-advice {
            padding: 4px 0 6px 24px;
            color: #166534;
            background: linear-gradient(135deg, #f0fdf4 0%, #dcfce7 100%);
            border-left: 3px solid #22c55e;
            margin: 0 0 6px 0;
            font-weight: 500;
            font-size: 0.82rem;
            border-radius: 0 6px 6px 0;
        }
        
        .line-with-advice {
            margin-bottom: 3px;
        }
        
        .line-content {
            color: #334155;
        }
        
        .inline-advice {
            display: inline-block;
            margin-left: 16px;
            padding: 6px 14px;
            background: linear-gradient(135deg, #f0fdf4 0%, #dcfce7 100%);
            border-left: 3px solid #22c55e;
            border-radius: 6px;
            font-size: 0.85rem;
            color: #166534;
            vertical-align: middle;
            box-shadow: 0 2px 6px rgba(34,197,94,0.1);
        }
    </style>

</head>
<body>
<div class="app">
    <div class="topbar">
        <div>
            <h2><i class="fas fa-microscope"></i> IVD 智能分析 - 诊断报告</h2>
        </div>
        <div class="meta">
            <span id="metaSeries">-</span>
            <span id="metaModel">-</span>
            <span id="metaFile">-</span>
            <span id="metaTime">-</span>
            <select id="quickSeries" style="padding:4px 10px;border-radius:16px;border:1px solid rgba(255,255,255,0.3);background:rgba(255,255,255,0.15);color:white;font-size:0.75rem;">
                <option value="" style="background:#1e40af;color:white;">选择系列</option>
                <option value="SMART" style="background:#1e40af;color:white;">SMART系列</option>
                <option value="Venus" style="background:#1e40af;color:white;">Venus系列</option>
            </select>
            <select id="quickModel" style="padding:4px 10px;border-radius:16px;border:1px solid rgba(255,255,255,0.3);background:rgba(255,255,255,0.15);color:white;font-size:0.75rem;">
                <option value="" style="background:#1e40af;color:white;">选择型号</option>
            </select>
            <label style="padding:4px 12px;background:linear-gradient(135deg, rgba(255,255,255,0.2) 0%, rgba(255,255,255,0.1) 100%);border-radius:16px;cursor:pointer;font-size:0.75rem;display:flex;align-items:center;gap:6px;border:1px solid rgba(255,255,255,0.2);">
                <i class="fas fa-upload"></i> 快速上传
                <input type="file" id="quickFile" style="display:none;" accept=".txt,.log,.md,.csv,.zip" multiple>
            </label>
            <a href="/"><i class="fas fa-arrow-left"></i> 返回上传</a>
        </div>
    </div>
    <div class="main">
        <div class="left-panel">
            <div class="left-header">
                <div class="search-wrapper">
                    <input type="text" id="searchInput" placeholder="搜索文件名..." oninput="filterTree()">
                    <i class="fas fa-search search-icon" onclick="document.getElementById('searchInput').focus()"></i>
                </div>
                <div class="summary-row" id="summaryRow"></div>
                <div id="uploadedInfo"></div>
            </div>
            <div class="left-tree" id="leftTree">
                <div class="empty-state"><div class="icon">⏳</div><div>加载中...</div></div>
            </div>
            <div class="left-footer" id="leftFooter"></div>
        </div>
        <div class="right-panel" id="rightPanel">
            <div class="right-placeholder" id="rightPlaceholder">
                <div style="font-size:2.5rem;">📂</div>
                <div>请从左侧选择一个文件查看详情</div>
            </div>
            <div class="right-content" id="rightContent" style="display:none;">
                <div class="right-header">
                    <div>
                        <div class="file-title" id="fileTitle">-</div>
                        <div class="file-meta" id="fileMeta">-</div>
                    </div>
                    <button class="btn-modal" onclick="openFullModal()"><i class="fas fa-expand"></i> 弹窗查看全文</button>
                </div>
                <div class="right-body" id="rightBody"></div>
            </div>
        </div>
    </div>
</div>

<div class="modal-overlay" id="fullModal" onclick="if(event.target===this)closeFullModal()">
    <div class="modal-card">
        <div class="modal-header">
            <div class="modal-title" id="modalTitle">文件全文</div>
            <button class="modal-close" onclick="closeFullModal()"><i class="fas fa-times"></i></button>
        </div>
        <div class="modal-body" id="modalBody"></div>
    </div>
</div>

<script>
const ANALYSIS_ID = '{{ analysis_id }}';
let _embeddedData = null;
try { _embeddedData = {{ embedded_data | safe }}; } catch(e) {}
let allDateGroups = [];
let currentFileName = null;
let searchQuery = '';
let hasMoreFiles = false;
let zipTotalCandidates = 0;
let zipProcessed = 0;
let isLoadingMoreFiles = false;

function fetchWithTimeout(url, options = {}, timeout = 300000) {
    const controller = new AbortController();
    const signal = controller.signal;
    const fetchOptions = { ...options, signal };
    const timeoutId = setTimeout(() => {
        controller.abort();
    }, timeout);
    return fetch(url, fetchOptions)
        .finally(() => clearTimeout(timeoutId))
        .catch(err => {
            if (err.name === 'AbortError') {
                const timeoutErr = new Error('请求超时');
                timeoutErr.name = 'TimeoutError';
                throw timeoutErr;
            }
            throw err;
        });
}

// 快速上传 - 系列型号联动
document.getElementById('quickSeries').addEventListener('change', async function() {
    const series = this.value;
    const modelSelect = document.getElementById('quickModel');
    if (!series) {
        modelSelect.innerHTML = '<option value="" style="background:#1e40af;color:white;">选择型号</option>';
        return;
    }
    try {
        const resp = await fetch(`/api/models?series=${series}`);
        const models = await resp.json();
        let opts = '<option value="" style="background:#1e40af;color:white;">选择型号</option>';
        models.forEach(m => opts += `<option value="${m.name}" style="background:#1e40af;color:white;">${m.name}</option>`);
        modelSelect.innerHTML = opts;
        if (models.length > 0) modelSelect.value = models[0].name;
    } catch (e) {
        modelSelect.innerHTML = '<option value="" style="background:#1e40af;color:white;">加载失败</option>';
    }
});

// 快速上传功能 - 直接刷新当前页面显示新结果
document.getElementById('quickFile').addEventListener('change', async function(e) {
    const files = e.target.files;
    if (!files || files.length === 0) return;
    
    const series = document.getElementById('quickSeries').value;
    const model = document.getElementById('quickModel').value;
    if (!series) { alert('请先选择设备系列'); return; }
    if (!model) { alert('请先选择设备型号'); return; }
    
    const formData = new FormData();
    formData.append('series', series);
    formData.append('model', model);
    if (files.length === 1) { formData.append('file', files[0]); }
    else { for (let i = 0; i < files.length; i++) { formData.append('files', files[i]); } }
    
    const originalTreeContent = document.getElementById('leftTree').innerHTML;
    document.getElementById('leftTree').innerHTML = '<div class="empty-state"><div class="icon">⏳</div><div>正在智能分析...</div></div>';
    
    try {
        const resp = await fetchWithTimeout('/api/analyze', { method: 'POST', body: formData }, 300000);
        if (!resp.ok) { const text = await resp.text(); throw new Error(`服务器错误 ${resp.status}: ${text.slice(0, 300)}`); }
        const data = await resp.json();
        if (data.error) { alert('上传失败: ' + data.error); document.getElementById('leftTree').innerHTML = originalTreeContent; return; }
        
        if (data.status === 'accepted' && data.analysis_id) {
            document.getElementById('leftTree').innerHTML = '<div class="empty-state"><div class="icon">⏳</div><div>任务已提交，正在智能分析...</div></div>';
            let pollCount = 0;
            function quickPoll() {
                pollCount++;
                if (pollCount > 600) { alert('分析超时，请重试'); document.getElementById('leftTree').innerHTML = originalTreeContent; return; }
                fetch(`/api/task_status/${data.analysis_id}`)
                    .then(r => r.json())
                    .then(d => {
                        if (d.status === 'completed' && d.redirect_url) { window.location.href = d.redirect_url; }
                        else if (d.status === 'failed') { alert('分析失败: ' + (d.error || '未知错误')); document.getElementById('leftTree').innerHTML = originalTreeContent; }
                        else { document.getElementById('leftTree').innerHTML = `<div class="empty-state"><div class="icon">⏳</div><div>正在智能分析... (已等待 ${pollCount} 秒)</div></div>`; setTimeout(quickPoll, 1000); }
                    })
                    .catch(() => { setTimeout(quickPoll, 2000); });
            }
            setTimeout(quickPoll, 1000);
            return;
        }
        
        if (data.redirect_url) { window.location.href = data.redirect_url; }
        else { alert('服务器返回异常，请重试'); document.getElementById('leftTree').innerHTML = originalTreeContent; }
    } catch (err) {
        let message;
        if (err.name === 'TimeoutError') { message = '分析请求超时，请重试'; }
        else if (err.message && err.message.includes('Failed to fetch')) { message = '无法连接到服务器，请确认服务是否已启动'; }
        else { message = `请求失败: ${err.message}`; }
        alert(message);
        document.getElementById('leftTree').innerHTML = originalTreeContent;
    }
});

function buildDateTree(dateGroups) {
    return dateGroups || [];
}

function renderDateGroupedTree(dateGroups) {
    console.log('renderDateGroupedTree called with', dateGroups && dateGroups.length, dateGroups);
    const tree = document.getElementById('leftTree');
    tree.innerHTML = '';
    if (!dateGroups || dateGroups.length === 0) {
        tree.innerHTML = '<div class="empty-state"><div class="icon">📭</div><div>暂无匹配文件</div></div>';
        return;
    }

    dateGroups.forEach(group => {
        const dateDiv = document.createElement('div');
        dateDiv.className = 'date-group';
        let fileNodesHtml = '';
        (group.files || []).forEach(f => {
            const isAspirationFile = f.is_aspiration_file || false;
            const hasAspirationMatch = f.has_aspiration_match || false;
            const hasFault = f.has_fault || false;
            const hasTypes = (f.types || []).length > 0;
            const isReceiveFile = (f.types || []).includes('receive');
            
            let iconClass = 'fas fa-file-alt';
            let iconColor = '#64748b';
            let alertIcon = '';
            
            if (hasFault) {
                iconClass = 'fas fa-bug';
                iconColor = '#dc2626';
            } else if (isAspirationFile && hasAspirationMatch) {
                iconClass = 'fas fa-exclamation-triangle';
                iconColor = '#dc2626';
                alertIcon = '<i class="fas fa-bell" style="color:#dc2626;margin-left:6px;animation:pulse 2s infinite;"></i>';
            } else if (isReceiveFile) {
                iconClass = 'fas fa-download';
                iconColor = '#15803d';
            }
            
            const sizeKB = (f.size / 1024).toFixed(0);
            const tagsHtml = (f.types || []).map(t => {
                const label = t === 'fault' ? '故障' : t === 'sample' ? '样本' : t === 'reagent' ? '试剂' : t === 'receive' ? '接收' : '';
                return `<span class="type-tag type-tag-${t}" onclick="event.stopPropagation(); filterByType('${t}')" title="点击筛选${label}文件">${label}</span>`;
            }).join('');
            
            const displayName = f.name.split('/').pop();
            
            fileNodesHtml += `
                <div class="file-node" data-filename="${escapeAttr(f.name)}" data-searchable="${escapeHtml(f.name).toLowerCase()}" title="${escapeHtml(f.name)} (${sizeKB} KB)">
                    <span class="icon"><i class="${iconClass}" style="color:${iconColor};"></i></span>
                    <span class="fname">${escapeHtml(displayName)}${alertIcon}</span>
                    <span class="type-tags">${tagsHtml}</span>
                    <span class="fsize">${sizeKB}K</span>
                </div>
            `;
        });
        dateDiv.innerHTML = `
            <div class="date-header" onclick="toggleDateGroup(this)">
                <span class="arrow"><i class="fas fa-chevron-down"></i></span>
                <span class="date-text"><i class="fas fa-calendar-alt date-icon"></i>${escapeHtml(group.date || '未识别日期')}</span>
                <span class="count">${(group.files || []).length} 个文件</span>
            </div>
            <div class="file-list">${fileNodesHtml}</div>
        `;
        tree.appendChild(dateDiv);
    });

    tree.querySelectorAll('.file-node').forEach(node => {
        node.addEventListener('click', () => {
            selectFile(node.dataset.filename);
        });
    });
}

function toggleDateGroup(header) {
    header.classList.toggle('collapsed');
    const fileList = header.nextElementSibling;
    if (fileList) fileList.classList.toggle('collapsed');
    const arrow = header.querySelector('.arrow i');
    if (arrow) {
        arrow.className = header.classList.contains('collapsed') ? 'fas fa-chevron-right' : 'fas fa-chevron-down';
    }
}

async function loadAnalysisData() {
    let data = _embeddedData;
    if (!data) {
        try {
            const resp = await fetch(`/api/analysis/${ANALYSIS_ID}`);
            if (!resp.ok) {
                document.getElementById('leftTree').innerHTML = '<div class="empty-state"><div class="icon">⚠️</div><div>分析数据加载失败</div></div>';
                return;
            }
            data = await resp.json();
        } catch (err) {
            document.getElementById('leftTree').innerHTML = '<div class="empty-state"><div class="icon">❌</div><div>加载失败: ' + err.message + '</div></div>';
            return;
        }
    }
    try {
    document.getElementById('metaSeries').textContent = '📋 ' + (data.series || '-');
    document.getElementById('metaModel').textContent = '🔧 ' + (data.model || '-');
    
    if (data.analysis_type === 'reagent_cooling') {
        document.querySelector('.topbar h2').innerHTML = '<i class="fas fa-snowflake"></i> 试剂制冷排查报告';
    }
    
    if (data.series) {
        document.getElementById('quickSeries').value = data.series;
        document.getElementById('quickSeries').dispatchEvent(new Event('change'));
    }
    if (data.model) {
        setTimeout(() => { document.getElementById('quickModel').value = data.model; }, 300);
    }
    
    const uploadedInfoEl = document.getElementById('uploadedInfo');
    if (uploadedInfoEl) {
        uploadedInfoEl.innerHTML = `文件: <strong>${escapeHtml(data.file_name || '-')}</strong><br>分析时间: ${escapeHtml(data.analyzed_at || '-')}`;
    }
    document.getElementById('metaFile').textContent = '';
    document.getElementById('metaTime').textContent = '';

    allDateGroups = data.date_groups || [];
    hasMoreFiles = data.has_more_files || false;
    zipTotalCandidates = data.zip_total_candidates || 0;
    zipProcessed = data.zip_processed || 0;

    const s = data.summary || {};
    document.getElementById('summaryRow').innerHTML = `
        <span class="tag tag-fault" onclick="filterByType('fault')" title="点击筛选故障文件"><i class="fas fa-bug"></i> 故障 ${s.fault||0}</span>
        <span class="tag tag-sample" onclick="filterByType('sample')" title="点击筛选样本空吸文件"><i class="fas fa-vial"></i> 样本 ${s.sample||0}</span>
        <span class="tag tag-reagent" onclick="filterByType('reagent')" title="点击筛选试剂空吸文件"><i class="fas fa-flask"></i> 试剂 ${s.reagent||0}</span>
        <span class="tag tag-receive" onclick="filterByType('receive')" title="点击筛选接收数据文件"><i class="fas fa-download"></i> 接收 ${s.receive||0}</span>
        <span class="tag tag-all" onclick="filterByType('all')" title="显示全部文件"><i class="fas fa-list"></i> 全部</span>
    `;
    
    let displayedFileCount = 0;
    allDateGroups.forEach(group => {
        displayedFileCount += (group.files || []).length;
    });

    renderDateGroupedTree(allDateGroups);
    updateFooter();

    if (zipTotalCandidates > 0) {
        document.getElementById('metaFile').textContent = ' (已加载 ' + displayedFileCount + '/' + zipTotalCandidates + ' 文件)';
    }
    } catch (err) {
        console.error('加载分析数据失败:', err);
        document.getElementById('leftTree').innerHTML = `<div class="empty-state"><div class="icon">❌</div><div>加载失败: ${err.message}</div></div>`;
    }
}

function updateFooter() {
    const footer = document.getElementById('leftFooter');
    let html = '';
    if (hasMoreFiles) {
        // 计算当前显示的文件数量
        let displayedFileCount = 0;
        allDateGroups.forEach(group => {
            displayedFileCount += (group.files || []).length;
        });
        
        html += `<button class="btn-load-more" id="btnLoadMoreFiles" onclick="loadMoreFiles()" style="background:#e8f5e9;border-color:#4caf50;color:#2e7d32;margin-top:4px;">
            📦 加载更多文件 (已加载 ${displayedFileCount}/${zipTotalCandidates})
        </button>`;
    }
    footer.innerHTML = html;
}

async function loadMoreFiles() {
    if (isLoadingMoreFiles) return;
    isLoadingMoreFiles = true;
    const btn = document.getElementById('btnLoadMoreFiles');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ 加载中...'; }
    try {
        const resp = await fetch(`/api/analysis/${ANALYSIS_ID}/load-more`, { method: 'POST' });
        const data = await resp.json();
        if (!data.success) {
            alert('加载失败: ' + (data.error || '未知错误'));
            return;
        }
        allDateGroups = data.date_groups || [];
        hasMoreFiles = data.has_more_files || false;
        zipProcessed = data.zip_processed || 0;
        zipTotalCandidates = data.zip_total_candidates || 0;

        const s = data.summary || {};
        document.getElementById('summaryRow').innerHTML = `
            <span class="tag tag-fault" onclick="filterByType('fault')" title="点击筛选故障文件"><i class="fas fa-bug"></i> 故障 ${s.fault||0}</span>
            <span class="tag tag-sample" onclick="filterByType('sample')" title="点击筛选样本空吸文件"><i class="fas fa-vial"></i> 样本 ${s.sample||0}</span>
            <span class="tag tag-reagent" onclick="filterByType('reagent')" title="点击筛选试剂空吸文件"><i class="fas fa-flask"></i> 试剂 ${s.reagent||0}</span>
            <span class="tag tag-receive" onclick="filterByType('receive')" title="点击筛选接收数据文件"><i class="fas fa-download"></i> 接收 ${s.receive||0}</span>
            <span class="tag tag-all" onclick="filterByType('all')" title="显示全部文件"><i class="fas fa-list"></i> 全部</span>
        `;

        renderDateGroupedTree(allDateGroups);
        updateFooter();
        
        // 计算当前显示的文件数量
        let displayedFileCount = 0;
        allDateGroups.forEach(group => {
            displayedFileCount += (group.files || []).length;
        });
        document.getElementById('metaFile').textContent = ' (已加载 ' + displayedFileCount + '/' + zipTotalCandidates + ' 文件)';
    } catch (err) {
        console.error('加载更多文件失败:', err);
        alert('加载更多失败: ' + err.message);
    } finally {
        isLoadingMoreFiles = false;
        if (btn) { btn.disabled = false; btn.textContent = '📦 加载更多文件'; }
    }
}

// 文件类型筛选
let currentFilterType = 'all';

function filterByType(type) {
    console.log('filterByType called:', type);
    
    // 防止重复点击
    if (currentFilterType === type && type !== 'all') {
        // 双击取消筛选，显示全部
        type = 'all';
    }
    
    currentFilterType = type;
    
    // 更新标签激活状态
    document.querySelectorAll('.tag').forEach(tag => {
        tag.classList.remove('active');
        // 添加点击动画
        tag.style.transform = 'scale(0.95)';
        setTimeout(() => {
            tag.style.transform = '';
        }, 150);
    });
    
    // 激活当前标签
    const currentTag = event.target.closest('.tag');
    currentTag.classList.add('active');
    currentTag.style.transform = 'scale(1.15)';
    setTimeout(() => {
        currentTag.style.transform = '';
    }, 200);
    
    // 筛选文件
    const filteredGroups = [];
    allDateGroups.forEach(group => {
        const filteredFiles = (group.files || []).filter(f => {
            if (type === 'all') return true;
            const types = f.types || [];
            return types.includes(type);
        });
        
        if (filteredFiles.length > 0) {
            filteredGroups.push({
                date: group.date,
                files: filteredFiles
            });
        }
    });
    
    // 添加淡出淡入动画
    const tree = document.getElementById('leftTree');
    tree.style.opacity = '0';
    tree.style.transform = 'translateY(-10px)';
    
    setTimeout(() => {
        // 渲染筛选后的文件树
        renderDateGroupedTree(filteredGroups);
        
        // 淡入动画
        tree.style.transition = 'all 0.3s ease';
        tree.style.opacity = '1';
        tree.style.transform = 'translateY(0)';
    }, 150);
}

function selectFile(filename) {
    console.log('selectFile called', filename);
    currentFileName = filename;
    document.querySelectorAll('.file-node.active').forEach(el => el.classList.remove('active'));
    const nodes = document.querySelectorAll('.file-node');
    let foundNode = false;
    nodes.forEach(node => {
        if (node.dataset.filename === filename) {
            node.classList.add('active');
            node.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            foundNode = true;
        }
    });
    console.log('selectFile foundNode', foundNode, 'nodesCount', nodes.length);
    document.getElementById('rightPlaceholder').style.display = 'none';
    document.getElementById('rightContent').style.display = 'flex';
    document.getElementById('fileTitle').textContent = '📄 ' + filename;
    if (window._fileCache && window._fileCache[filename]) {
        document.getElementById('fileMeta').textContent = window._fileCache[filename]._meta;
        renderFileContent(window._fileCache[filename]);
        return;
    }
    document.getElementById('fileMeta').textContent = '加载中...';
    document.getElementById('rightBody').innerHTML = '<div style="text-align:center;padding:40px;color:#999;font-size:0.85rem;">⏳ 加载文件内容...</div>';
    loadFileContent(filename);
}

async function loadFileContent(filename) {
    try {
        const resp = await fetch(`/api/analysis/${ANALYSIS_ID}/file?name=${encodeURIComponent(filename)}`);
        if (!resp.ok) {
            document.getElementById('rightBody').innerHTML = `<div class="empty-state"><div class="icon">❌</div><div>文件加载失败</div></div>`;
            return;
        }
        const data = await resp.json();
        if (!window._fileCache) window._fileCache = {};
        data._meta = `${(data.size / 1024).toFixed(0)} KB | ${data.analysis.length} 条匹配`;
        window._fileCache[filename] = data;
        document.getElementById('fileMeta').textContent = data._meta;
        renderFileContent(data);
    } catch (err) {
        document.getElementById('rightBody').innerHTML = `<div class="empty-state"><div class="icon">❌</div><div>加载失败: ${err.message}</div></div>`;
    }
}
function renderFileContent(data) {
    const body = document.getElementById('rightBody');
    const analysis = data.analysis || [];
    if (!Array.isArray(analysis)) {
        body.innerHTML = '<div class="empty-state"><div class="icon">⚠️</div><div>分析数据格式异常</div></div>';
        return;
    }

    try {
        // ===== 优先使用后端生成的 html_content =====
        if (data.html_content) {
            // 样本/试剂分组（仅当没有故障时才显示）
            const hasFault = data.has_fault || false;
            let sampleMatches = [];
            let reagentMatches = [];
            let hasMatches = false;
            
            if (!hasFault) {
                sampleMatches = analysis.filter(item => 
                    item.type === 'keyword_match' && 
                    Array.isArray(item.keywords) && 
                    item.keywords.some(kw => ['样本空吸', '样本不足'].includes(kw))
                );
                reagentMatches = analysis.filter(item => 
                    item.type === 'keyword_match' && 
                    Array.isArray(item.keywords) && 
                    item.keywords.some(kw => ['试剂空吸', '试剂不足'].includes(kw))
                );
                hasMatches = sampleMatches.length > 0 || reagentMatches.length > 0;
            }

            function renderGroup(title, icon, items, emptyMsg, borderColor, bgColor, groupId) {
                if (!items || items.length === 0) return '';
                let groupHtml = `<div class="db-analysis-section" style="margin-top:12px;">`;
                groupHtml += `<div class="db-separator" style="display:flex;align-items:center;justify-content:space-between;">
                    <span><i class="${icon}" style="margin-right:6px;"></i>${title} (${items.length} 条)</span>
                    <button class="btn-view-full" onclick="openMatchModal('${groupId}', '${title}')"><i class="fas fa-expand"></i>查看全文</button>
                </div>`;
                groupHtml += `<div id="${groupId}" style="display:none;"></div>`;
                let previewHtml = '';
                items.slice(0, 10).forEach((item, idx) => {
                    previewHtml += `<div style="background:linear-gradient(135deg, #fefce8 0%, #fef9c3 100%);border-left:3px solid var(--warning);padding:6px 10px;margin:2px 0;border-radius:6px;font-family:monospace;font-size:0.75rem;line-height:1.4;white-space:pre-wrap;word-break:break-word;box-shadow:var(--shadow-sm);"><i class="fas fa-file-alt" style="color:var(--warning);margin-right:6px;"></i>${escapeHtml(item.original_text || '')}</div>`;
                    previewHtml += `<div style="color:var(--success);padding:4px 10px;font-size:0.75rem;line-height:1.4;background:rgba(16,185,129,0.05);border-radius:4px;margin:2px 0;"><i class="fas fa-lightbulb" style="margin-right:6px;"></i><strong>诊断建议:</strong> ${escapeHtml(item.advice || '')}</div>`;
                    if (idx < Math.min(items.length, 10) - 1) {
                        previewHtml += `<div style="height:8px;"></div>`;
                    }
                });
                groupHtml += `<div style="background:${bgColor};border-left:3px solid ${borderColor};border-radius:8px;padding:10px;overflow-y:auto;box-shadow:var(--shadow);">${previewHtml}</div>`;
                if (items.length > 10) {
                    groupHtml += `<div style="text-align:center;color:#64748b;font-size:0.72rem;padding:4px;">... 还有 ${items.length - 10} 条匹配，点击"查看全文"查看全部</div>`;
                }
                groupHtml += `</div>`;
                groupHtml += `</div>`;
                return groupHtml;
            }
            
            window.matchData = {};
            window.matchData['sampleMatches'] = sampleMatches;
            window.matchData['reagentMatches'] = reagentMatches;

            let html = '';
            
            // 如果有匹配信息，分为60%和40%
            if (hasMatches) {
                html = `<div class="content-section" style="height:60%;display:flex;flex-direction:column;">
                    <div class="section-label"><i class="fas fa-file-medical-alt" style="margin-right:6px;"></i>原始文档与故障对比</div>
                    <div class="raw-content" style="flex:1;white-space:pre-wrap; word-break:break-word; font-family:monospace; font-size:0.82rem; line-height:1.6; background:#fafbfc; padding:12px; border-radius:6px; border:1px solid #e8ecf1;overflow-y:auto;">
                        ${data.html_content}
                    </div>
                </div>
                <div class="content-section" style="height:40%;display:flex;flex-direction:column;margin-top:8px;">
                    <div class="section-label"><i class="fas fa-search" style="margin-right:6px;"></i>匹配信息</div>
                    <div style="flex:1;overflow-y:auto;">
                        ${renderGroup('样本空吸匹配', 'fas fa-vial', sampleMatches, '暂无样本空吸匹配', 'var(--info)', '#e0f2fe', 'sampleMatches')}
                        ${renderGroup('试剂空吸匹配', 'fas fa-flask', reagentMatches, '暂无试剂空吸匹配', 'var(--purple)', '#f3e8ff', 'reagentMatches')}
                    </div>
                </div>`;
            } else if (hasFault) {
                html = `<div class="content-section">
                    <div class="section-label"><i class="fas fa-file-medical-alt" style="margin-right:6px;"></i>原始文档与故障对比</div>
                    <div class="raw-content" style="white-space:pre-wrap; word-break:break-word; font-family:monospace; font-size:0.82rem; line-height:1.6; background:#fafbfc; padding:12px; border-radius:6px; border:1px solid #e8ecf1;">
                        ${data.html_content}
                    </div>
                </div>
                <div style="margin-top:12px;padding:10px 14px;background:linear-gradient(135deg, #fef3c7 0%, #fde68a 100%);border-radius:8px;color:#92400e;font-size:0.85rem;border-left:4px solid var(--warning);box-shadow:var(--shadow-sm);"><i class="fas fa-exclamation-triangle" style="margin-right:8px;"></i>检测到故障匹配，仅显示故障诊断信息</div>`;
            } else {
                // 没有匹配信息，正常显示
                html = `<div class="content-section">
                    <div class="section-label"><i class="fas fa-file-medical-alt" style="margin-right:6px;"></i>原始文档与故障对比</div>
                    <div class="raw-content" style="white-space:pre-wrap; word-break:break-word; font-family:monospace; font-size:0.82rem; line-height:1.6; background:#fafbfc; padding:12px; border-radius:6px; border:1px solid #e8ecf1;">
                        ${data.html_content}
                    </div>
                </div>`;
            }

            body.innerHTML = html;
            return;  // 使用后端生成的内容，直接结束
        }

        // ===== 兼容旧逻辑（如果后端没有 html_content，则走原流程） =====
        // 以下为原有的渲染逻辑（您恢复后的版本），保留作为后备
        const content = data.content || '';
        const adviceMap = {};
        analysis.forEach(item => {
            if (item.type === 'motor_status_match') {
                const orig = item.original_text ? item.original_text.trim() : '';
                if (orig && item.advice) {
                    if (!adviceMap[orig]) {
                        adviceMap[orig] = item.advice;
                    }
                }
            }
        });

        const lines = content.split('\\n');
        let html = `
            <div class="content-section">
                <div class="section-label">📝 原始文档内容</div>
                <div class="raw-content" style="white-space:pre-wrap; word-break:break-word; font-family:monospace; font-size:0.82rem; line-height:1.6; background:#fafbfc; padding:12px; border-radius:6px; border:1px solid #e8ecf1;">
        `;
        lines.forEach(line => {
            const trimmed = line.trim();
            html += escapeHtml(line) + '\\n';
            if (trimmed && adviceMap[trimmed]) {
                html += '<span style="color:#155724; font-weight:600; display:block; margin-left:1.2em;">诊断建议：</span>';
                html += '<span style="display:block; margin-left:2.4em; color:#2d6a4f;">' + escapeHtml(adviceMap[trimmed]) + '</span>\\n';
            }
        });
        html += `</div></div>`;

        const sampleMatches = analysis.filter(item => 
            item.type === 'keyword_match' && 
            Array.isArray(item.keywords) && 
            item.keywords.some(kw => ['样本空吸', '样本不足'].includes(kw))
        );
        const reagentMatches = analysis.filter(item => 
            item.type === 'keyword_match' && 
            Array.isArray(item.keywords) && 
            item.keywords.some(kw => ['试剂空吸', '试剂不足'].includes(kw))
        );

        function renderGroup(title, icon, items, emptyMsg, borderColor, bgColor, groupId) {
            if (!items || items.length === 0) return '';
            let groupHtml = `<div class="db-analysis-section" style="margin-top:16px;">`;
            groupHtml += `<div class="db-separator" style="display:flex;align-items:center;justify-content:space-between;">
                <span>${icon} ${title} (${items.length} 条)</span>
                <button class="btn-view-full" onclick="openMatchModal('${groupId}', '${title}')"><i class="fas fa-expand"></i> 查看全文</button>
            </div>`;
            groupHtml += `<div id="${groupId}" style="display:none;">`;
            items.forEach((item, idx) => {
                groupHtml += `<div style="padding:8px;margin:6px 0;background:${bgColor};border-left:3px solid ${borderColor};border-radius:6px;">
                    <div style="font-family:monospace;font-size:0.75rem;white-space:pre-wrap;word-break:break-word;background:#fefce8;border:1px solid #fde047;padding:6px 10px;border-radius:4px;margin-bottom:4px;">${escapeHtml(item.original_text || '')}</div>
                    <div style="font-size:0.75rem;color:#166534;"><strong>诊断建议:</strong> ${escapeHtml(item.advice || '')}</div>
                </div>`;
            });
            groupHtml += `</div>`;
            let previewHtml = '';
            items.slice(0, 10).forEach((item, idx) => {
                previewHtml += `<div style="background:#fefce8;border-left:3px solid #fde047;padding:4px 8px;margin:2px 0;border-radius:4px;font-family:monospace;font-size:0.75rem;line-height:1.3;white-space:pre-wrap;word-break:break-word;">${escapeHtml(item.original_text || '')}</div>`;
                previewHtml += `<div style="color:#166534;padding:2px 8px;font-size:0.75rem;line-height:1.3;"><strong>💡 诊断建议:</strong> ${escapeHtml(item.advice || '')}</div>`;
                if (idx < Math.min(items.length, 10) - 1) {
                    previewHtml += `<div style="height:6px;"></div>`;
                }
            });
            groupHtml += `<div style="background:${bgColor};border-left:3px solid ${borderColor};border-radius:6px;padding:8px;max-height:300px;overflow-y:auto;">${previewHtml}</div>`;
            if (items.length > 10) {
                groupHtml += `<div style="text-align:center;color:#64748b;font-size:0.72rem;padding:4px;">... 还有 ${items.length - 10} 条匹配，点击"查看全文"查看全部</div>`;
            }
            groupHtml += `</div>`;
            groupHtml += `</div>`;
            return groupHtml;
        }
        
        window.matchData = {};
        window.matchData['sampleMatches'] = sampleMatches;
        window.matchData['reagentMatches'] = reagentMatches;

        html += renderGroup('样本空吸匹配', '🧪', sampleMatches, '暂无样本空吸匹配', '#17a2b8', '#e3f2fd', 'sampleMatches');
        html += renderGroup('试剂空吸匹配', '🧫', reagentMatches, '暂无试剂空吸匹配', '#6f42c1', '#f3e5f5', 'reagentMatches');

        body.innerHTML = html;
    } catch (err) {
        console.error('渲染文件内容失败:', err);
        body.innerHTML = `<div class="empty-state"><div class="icon">❌</div><div>渲染失败: ${escapeHtml(err.message)}</div></div>`;
    }
}

function openFullModal() {
    if (!currentFileName) return;
    const modal = document.getElementById('fullModal');
    document.getElementById('modalTitle').textContent = '📄 ' + currentFileName;
    const bodyContent = document.getElementById('rightBody').innerHTML;
    document.getElementById('modalBody').innerHTML = bodyContent;
    modal.style.display = 'flex';
}

function closeFullModal() {
    document.getElementById('fullModal').style.display = 'none';
}

function openMatchModal(groupId, title) {
    const modal = document.getElementById('fullModal');
    const icon = groupId === 'sampleMatches' ? 'fa-vial' : 'fa-flask';
    document.getElementById('modalTitle').innerHTML = `<i class="fas ${icon}" style="margin-right:8px;"></i>${title} - 全文查看`;
    const items = window.matchData[groupId] || [];
    const bgColor = groupId === 'sampleMatches' ? '#e0f2fe' : '#f3e8ff';
    const borderColor = groupId === 'sampleMatches' ? 'var(--info)' : 'var(--purple)';
    let contentHtml = '';
    items.forEach((item, idx) => {
        contentHtml += `<div style="background:linear-gradient(135deg, #fefce8 0%, #fef9c3 100%);border-left:3px solid var(--warning);padding:6px 10px;margin:2px 0;border-radius:6px;font-family:monospace;font-size:0.75rem;line-height:1.4;white-space:pre-wrap;word-break:break-word;box-shadow:var(--shadow-sm);"><i class="fas fa-file-alt" style="color:var(--warning);margin-right:6px;"></i>${escapeHtml(item.original_text || '')}</div>`;
        contentHtml += `<div style="color:var(--success);padding:4px 10px;font-size:0.75rem;line-height:1.4;background:rgba(16,185,129,0.05);border-radius:4px;margin:2px 0;"><i class="fas fa-lightbulb" style="margin-right:6px;"></i><strong>诊断建议:</strong> ${escapeHtml(item.advice || '')}</div>`;
        if (idx < items.length - 1) {
            contentHtml += `<div style="height:8px;"></div>`;
        }
    });
    let html = `<div style="background:${bgColor};border-left:3px solid ${borderColor};border-radius:8px;padding:10px;max-height:calc(95vh - 120px);overflow-y:auto;box-shadow:var(--shadow);">${contentHtml}</div>`;
    document.getElementById('modalBody').innerHTML = html || '<div style="text-align:center;color:var(--gray-600);padding:40px;"><i class="fas fa-inbox" style="font-size:2rem;opacity:0.5;margin-bottom:10px;"></i><br>暂无匹配数据</div>';
    modal.style.display = 'flex';
}

function filterTree() {
    searchQuery = document.getElementById('searchInput').value.toLowerCase().trim();
    const allDateGroups = document.querySelectorAll('.date-group');
    allDateGroups.forEach(group => {
        let hasVisible = false;
        const fileNodes = group.querySelectorAll('.file-node');
        fileNodes.forEach(node => {
            const searchable = node.getAttribute('data-searchable') || '';
            if (!searchQuery || searchable.includes(searchQuery)) {
                node.style.display = '';
                hasVisible = true;
            } else {
                node.style.display = 'none';
            }
        });
        const dateHeader = group.querySelector('.date-header');
        if (dateHeader) {
            dateHeader.style.display = hasVisible ? '' : 'none';
        }
    });
}

function matchesSearch(filename) {
    if (!searchQuery) return true;
    return filename.toLowerCase().includes(searchQuery);
}

function escapeHtml(text) {
    if (!text) return '';
    return text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#039;');
}

function escapeAttr(text) {
    if (!text) return '';
    return text.replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#039;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
loadAnalysisData();
</script>
</body>
</html>
'''
# ========== ANALYSIS_HTML 结束 ==========

# ======================================================================
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