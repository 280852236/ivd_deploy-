-- IVD故障分析平台数据库初始化脚本

-- 创建必要扩展
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- 创建users表（用户账户）
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    permission INTEGER DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE,
    last_login_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 创建series表
CREATE TABLE IF NOT EXISTS series (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 创建models表
CREATE TABLE IF NOT EXISTS models (
    id SERIAL PRIMARY KEY,
    series_id INTEGER NOT NULL REFERENCES series(id),
    name TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(series_id, name)
);

-- 创建rules表
CREATE TABLE IF NOT EXISTS rules (
    id SERIAL PRIMARY KEY,
    model_id INTEGER NOT NULL REFERENCES models(id),
    keywords TEXT NOT NULL,
    advice TEXT NOT NULL,
    source TEXT DEFAULT 'manual',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 创建rule_keywords表
CREATE TABLE IF NOT EXISTS rule_keywords (
    id SERIAL PRIMARY KEY,
    rule_id INTEGER NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
    keyword TEXT NOT NULL
);



-- 创建version_history表
CREATE TABLE IF NOT EXISTS version_history (
    id SERIAL PRIMARY KEY,
    version INTEGER NOT NULL,
    action TEXT NOT NULL,
    rule_id INTEGER,
    rule_snapshot TEXT,
    operator TEXT DEFAULT 'system',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);


-- 插入默认series数据
INSERT INTO series (name) VALUES ('SMART') ON CONFLICT (name) DO NOTHING;
INSERT INTO series (name) VALUES ('VENUS') ON CONFLICT (name) DO NOTHING;

-- 插入默认models数据
INSERT INTO models (series_id, name) 
SELECT s.id, 'SMART6500' FROM series s WHERE s.name = 'SMART' 
ON CONFLICT (series_id, name) DO NOTHING;

INSERT INTO models (series_id, name)   
SELECT s.id, 'SMART500' FROM series s WHERE s.name = 'SMART' 
ON CONFLICT (series_id, name) DO NOTHING;

INSERT INTO models (series_id, name) 
SELECT s.id, 'VENUS100' FROM series s WHERE s.name = 'VENUS' 
ON CONFLICT (series_id, name) DO NOTHING;

INSERT INTO models (series_id, name) 
SELECT s.id, 'VENUS500' FROM series s WHERE s.name = 'VENUS' 
ON CONFLICT (series_id, name) DO NOTHING;

INSERT INTO models (series_id, name) 
SELECT s.id, 'VENUS9000' FROM series s WHERE s.name = 'VENUS' 
ON CONFLICT (series_id, name) DO NOTHING;

INSERT INTO models (series_id, name) 
SELECT s.id, 'VENUS9900' FROM series s WHERE s.name = 'VENUS' 
ON CONFLICT (series_id, name) DO NOTHING;

-- 插入默认rules数据
INSERT INTO rules (model_id, keywords, advice)
SELECT m.id, '样本空吸,样本不足', '🔧 排查：检查样本管液位、加样针'
FROM models m JOIN series s ON m.series_id = s.id
WHERE s.name = 'SMART' AND m.name = 'SMART6500'
ON CONFLICT DO NOTHING;

INSERT INTO rules (model_id, keywords, advice)
SELECT m.id, '试剂空吸,试剂不足', '🔧 排查：检查试剂瓶、试剂针'
FROM models m JOIN series s ON m.series_id = s.id
WHERE s.name = 'SMART' AND m.name = 'SMART6500'
ON CONFLICT DO NOTHING;

INSERT INTO rules (model_id, keywords, advice)
SELECT m.id, '压力异常', '🔧 排查：检查管路、泵膜'
FROM models m JOIN series s ON m.series_id = s.id
WHERE s.name = 'SMART' AND m.name = 'SMART6500'
ON CONFLICT DO NOTHING;

INSERT INTO rules (model_id, keywords, advice)
SELECT m.id, '温度失控', '🔧 排查：检查加热片、传感器'
FROM models m JOIN series s ON m.series_id = s.id
WHERE s.name = 'SMART' AND m.name = 'SMART500'
ON CONFLICT DO NOTHING;

INSERT INTO rules (model_id, keywords, advice)
SELECT m.id, '试剂空', '🔧 更换试剂，检查液位电路'
FROM models m JOIN series s ON m.series_id = s.id
WHERE s.name = 'VENUS' AND m.name = 'VENUS100'
ON CONFLICT DO NOTHING;

INSERT INTO rules (model_id, keywords, advice)
SELECT m.id, '通讯失败', '🔧 检查线缆、重启设备'
FROM models m JOIN series s ON m.series_id = s.id
WHERE s.name = 'VENUS' AND m.name = 'VENUS500'
ON CONFLICT DO NOTHING;

INSERT INTO rules (model_id, keywords, advice)
SELECT m.id, '结果异常', '🔧 执行质控、清洁光学系统'
FROM models m JOIN series s ON m.series_id = s.id
WHERE s.name = 'VENUS' AND m.name = 'VENUS9000'
ON CONFLICT DO NOTHING;

INSERT INTO rules (model_id, keywords, advice)
SELECT m.id, '卡杯', '🔧 检查清洗针、泵阀'
FROM models m JOIN series s ON m.series_id = s.id
WHERE s.name = 'VENUS' AND m.name = 'VENUS9900'
ON CONFLICT DO NOTHING;
-- ========== 硬件故障案例表 ==========
-- 为每个型号创建硬件故障表和图片表
-- SMART系列
CREATE TABLE IF NOT EXISTS hardware_failures_smart6500 (
    id SERIAL PRIMARY KEY,
    phenomenon TEXT NOT NULL,
    cause TEXT,
    workaround TEXT,
    process TEXT,
    solution TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS hardware_failure_images_smart6500 (
    id SERIAL PRIMARY KEY,
    failure_id INTEGER NOT NULL REFERENCES hardware_failures_smart6500(id) ON DELETE CASCADE,
    image_data BYTEA NOT NULL,
    image_mime TEXT DEFAULT 'image/jpeg',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS hardware_failures_smart8000 (
    id SERIAL PRIMARY KEY,
    phenomenon TEXT NOT NULL,
    cause TEXT,
    workaround TEXT,
    process TEXT,
    solution TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS hardware_failure_images_smart8000 (
    id SERIAL PRIMARY KEY,
    failure_id INTEGER NOT NULL REFERENCES hardware_failures_smart8000(id) ON DELETE CASCADE,
    image_data BYTEA NOT NULL,
    image_mime TEXT DEFAULT 'image/jpeg',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- VENUS系列
CREATE TABLE IF NOT EXISTS hardware_failures_venus100 (
    id SERIAL PRIMARY KEY,
    phenomenon TEXT NOT NULL,
    cause TEXT,
    workaround TEXT,
    process TEXT,
    solution TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS hardware_failure_images_venus100 (
    id SERIAL PRIMARY KEY,
    failure_id INTEGER NOT NULL REFERENCES hardware_failures_venus100(id) ON DELETE CASCADE,
    image_data BYTEA NOT NULL,
    image_mime TEXT DEFAULT 'image/jpeg',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ========== 索引 ==========



-- 审计日志表
CREATE TABLE IF NOT EXISTS audit_logs (
    id SERIAL PRIMARY KEY,
    user_id INTEGER,
    username TEXT,
    action TEXT NOT NULL,
    target_type TEXT,
    target_id TEXT,
    detail TEXT,
    ip_address TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
