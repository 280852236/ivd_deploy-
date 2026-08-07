from shared import _CLEAN_RE, invalidate_allowed_tables_cache
from psycopg2.extras import RealDictCursor
from services.config import Config
from services.db import db_connection
from werkzeug.security import generate_password_hash


def get_table_name(model_name: str) -> str:
    clean_name = _CLEAN_RE.sub('', model_name)
    if not clean_name:
        raise ValueError(f"无效的型号名称: {model_name!r}，过滤后为空")
    return f"motor_status_{clean_name.lower()}"


def get_pcba_compat_table(model_name: str) -> str:
    clean_name = _CLEAN_RE.sub('', model_name)
    if not clean_name:
        raise ValueError(f"无效的型号名称: {model_name!r}")
    return f"pcba_compat_{clean_name.lower()}"


def get_bootloader_compat_table(model_name: str) -> str:
    clean_name = _CLEAN_RE.sub('', model_name)
    if not clean_name:
        raise ValueError(f"无效的型号名称: {model_name!r}")
    return f"bootloader_compat_{clean_name.lower()}"


def ensure_pcba_compat_table(model_name: str):
    table_name = get_pcba_compat_table(model_name)
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass(%s)", (table_name,))
        if not cur.fetchone()[0]:
            cur.execute(f"""
                CREATE TABLE {table_name} (
                    id SERIAL PRIMARY KEY,
                    pcba_code TEXT NOT NULL,
                    pcb_code TEXT,
                    pcb_silkscreen TEXT,
                    latest_version TEXT,
                    board_name TEXT,
                    special_note TEXT,
                    pcba_version_compat TEXT,
                    compat_description TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{table_name}_pcba_code ON {table_name}(pcba_code)")
            for field in ['pcb_code', 'pcb_silkscreen', 'board_name', 'special_note', 'pcba_version_compat', 'compat_description']:
                cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_{field}_gin ON {table_name} USING gin({field} gin_trgm_ops)")
            invalidate_allowed_tables_cache()


def ensure_bootloader_compat_table(model_name: str):
    table_name = get_bootloader_compat_table(model_name)
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass(%s)", (table_name,))
        if not cur.fetchone()[0]:
            cur.execute(f"""
                CREATE TABLE {table_name} (
                    id SERIAL PRIMARY KEY,
                    board_mnemonic TEXT NOT NULL,
                    board_name TEXT,
                    bootloader_version TEXT,
                    bootloader_compat_note TEXT,
                    no_bootloader_version TEXT,
                    no_bootloader_compat_note TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{table_name}_mnemonic ON {table_name}(board_mnemonic)")
            for field in ['board_name', 'bootloader_version', 'bootloader_compat_note', 'no_bootloader_version', 'no_bootloader_compat_note']:
                cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_{field}_gin ON {table_name} USING gin({field} gin_trgm_ops)")
            invalidate_allowed_tables_cache()


def ensure_table_exists(model_name: str):
    table_name = get_table_name(model_name)
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass(%s)", (table_name,))
        if not cur.fetchone()[0]:
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
            cur.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{table_name}_lookup ON {table_name}(board_card, motor_code, status_code)")
            invalidate_allowed_tables_cache()


def init_db():
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
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
        cur.execute("CREATE INDEX IF NOT EXISTS idx_rule_keywords_rule_id ON rule_keywords(rule_id)")
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
                permission INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lis_protocol_templates (
                id SERIAL PRIMARY KEY,
                series TEXT NOT NULL,
                model TEXT NOT NULL,
                filename TEXT,
                content TEXT,
                pdf_data BYTEA,
                pdf_filename TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(series, model)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS analysis_results (
                analysis_id TEXT PRIMARY KEY,
                series TEXT,
                model TEXT,
                analysis_type TEXT DEFAULT '',
                summary JSONB,
                total_dates INTEGER DEFAULT 0,
                total_files INTEGER DEFAULT 0,
                matched_count INTEGER DEFAULT 0,
                analyzed_at TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_analysis_results_created ON analysis_results(created_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_analysis_results_series_model ON analysis_results(series, model)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_target ON audit_logs(target_type, target_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_is_active ON users(is_active) WHERE is_active = FALSE")
    init_default_data()


def init_default_data():
    with db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT id FROM users WHERE username = 'admin'")
        if not cur.fetchone():
            admin_password = Config.ADMIN_PASSWORD
            password_hash = generate_password_hash(admin_password)
            cur.execute("INSERT INTO users (username, password_hash, permission) VALUES (%s, %s, %s)", ('admin', password_hash, 1))
            conn.commit()
        cur.execute("SELECT id, name FROM series")
        if cur.fetchone():
            return
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
