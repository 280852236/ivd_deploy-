-- 性能优化索引脚本（修正版）
-- 修正: hardware_failures用phenomenon, rules无series_id/enabled, keywords是text

\echo '=== 开始添加性能优化索引 ==='

-- 1. software_bugs表: 全文搜索 + 时间索引
\echo '[1/6] software_bugs表...'
DO $$
DECLARE
    tbl text;
BEGIN
    FOR tbl IN SELECT tablename FROM pg_tables WHERE tablename LIKE 'software_bugs_%' LOOP
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%s_fulltext ON %s USING gin(to_tsvector(''simple'', coalesce(title,'''')||'' ''||coalesce(cause,'''')||'' ''||coalesce(workaround,'''')||'' ''||coalesce(solution,'''')))', tbl, tbl);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%s_created_at ON %s(created_at DESC)', tbl, tbl);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%s_software_version ON %s(software_version)', tbl, tbl);
        RAISE NOTICE 'OK: %', tbl;
    END LOOP;
END $$;

-- 2. hardware_failures表: 全文搜索(phenomenon) + 时间索引
\echo '[2/6] hardware_failures表...'
DO $$
DECLARE
    tbl text;
BEGIN
    FOR tbl IN SELECT tablename FROM pg_tables WHERE tablename LIKE 'hardware_failures_%' LOOP
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%s_fulltext ON %s USING gin(to_tsvector(''simple'', coalesce(phenomenon,'''')||'' ''||coalesce(cause,'''')||'' ''||coalesce(workaround,'''')||'' ''||coalesce(solution,'''')))', tbl, tbl);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%s_created_at ON %s(created_at DESC)', tbl, tbl);
        RAISE NOTICE 'OK: %', tbl;
    END LOOP;
END $$;

-- 3. motor_status表: 复合索引
\echo '[3/6] motor_status表...'
DO $$
DECLARE
    tbl text;
BEGIN
    FOR tbl IN SELECT tablename FROM pg_tables WHERE tablename LIKE 'motor_status_%' LOOP
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%s_lookup ON %s(board_card, motor_code, status_code)', tbl, tbl);
        RAISE NOTICE 'OK: %', tbl;
    END LOOP;
END $$;

-- 4. rules表: model_id已有索引, 补充advice全文搜索 + source索引
\echo '[4/6] rules表...'
CREATE INDEX IF NOT EXISTS idx_rules_advice_fts ON rules USING gin(to_tsvector('simple', coalesce(advice,'')));
CREATE INDEX IF NOT EXISTS idx_rules_source ON rules(source);

-- 5. bug_images + hardware_failure_images
\echo '[5/6] images表...'
DO $$
DECLARE
    tbl text;
BEGIN
    FOR tbl IN SELECT tablename FROM pg_tables WHERE tablename LIKE 'bug_images_%' LOOP
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%s_bug_id ON %s(bug_id)', tbl, tbl);
    END LOOP;
    FOR tbl IN SELECT tablename FROM pg_tables WHERE tablename LIKE 'hardware_failure_images_%' LOOP
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%s_failure_id ON %s(failure_id)', tbl, tbl);
    END LOOP;
END $$;

-- 6. rule_keywords + models + version_history
\echo '[6/6] 其他表...'
CREATE INDEX IF NOT EXISTS idx_rule_keywords_keyword ON rule_keywords(keyword);
CREATE INDEX IF NOT EXISTS idx_models_series_id ON models(series_id);
CREATE INDEX IF NOT EXISTS idx_version_history_created_at ON version_history(created_at DESC);

\echo '=== 索引添加完成 ==='
