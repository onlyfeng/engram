-- ============================================================
-- OpenMemory Schema 与角色权限脚本
-- ============================================================
--
-- 本脚本创建 OpenMemory 专用 schema 并配置权限。
-- 
-- 目标 schema 名称通过以下方式确定（优先级从高到低）：
--   1. PostgreSQL session 变量 om.target_schema
--   2. 环境变量 OM_PG_SCHEMA
--   3. 默认值 'openmemory'
--
-- 执行方式：
--   psql -d <your_db> -c "SET om.target_schema = 'openmemory'" -f 05_openmemory_roles_and_grants.sql
--   或在 db_migrate.py 中使用 --apply-openmemory-grants 选项自动执行
--
-- 注意事项：
--   - 本脚本可重复执行（幂等）
--   - 需要使用 superuser 或具有 CREATEROLE 权限的用户执行
--   - OpenMemory 组件会在此 schema 中创建自己的表
-- ============================================================

BEGIN;

-- ============================================================
-- 1. 获取目标 schema 名称
-- ============================================================
DO $$
DECLARE
    v_schema TEXT;
BEGIN
    -- 优先从 session 变量获取
    v_schema := NULLIF(current_setting('om.target_schema', true), '');
    
    IF v_schema IS NULL THEN
        v_schema := 'openmemory';
    END IF;
    
    -- 创建 schema（如不存在）
    EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', v_schema);
    RAISE NOTICE 'Created/verified schema: %', v_schema;
    
    -- 授予 openmemory_migrator 角色 schema 所有权限
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'openmemory_migrator') THEN
        EXECUTE format('GRANT ALL PRIVILEGES ON SCHEMA %I TO openmemory_migrator', v_schema);
        EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT ALL PRIVILEGES ON TABLES TO openmemory_migrator', v_schema);
        EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT ALL PRIVILEGES ON SEQUENCES TO openmemory_migrator', v_schema);
        EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT ALL PRIVILEGES ON FUNCTIONS TO openmemory_migrator', v_schema);
        RAISE NOTICE 'Granted ALL on schema % to openmemory_migrator', v_schema;
    END IF;
    
    -- 授予 openmemory_app 角色 DML 权限
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'openmemory_app') THEN
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO openmemory_app', v_schema);
        EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO openmemory_app', v_schema);
        EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT USAGE, SELECT ON SEQUENCES TO openmemory_app', v_schema);
        EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT EXECUTE ON FUNCTIONS TO openmemory_app', v_schema);
        RAISE NOTICE 'Granted DML on schema % to openmemory_app', v_schema;
    END IF;
    
    -- 授予登录角色权限（如存在）
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'openmemory_migrator_login') THEN
        EXECUTE format('GRANT ALL PRIVILEGES ON SCHEMA %I TO openmemory_migrator_login', v_schema);
        RAISE NOTICE 'Granted ALL on schema % to openmemory_migrator_login', v_schema;
    END IF;
    
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'openmemory_svc') THEN
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO openmemory_svc', v_schema);
        RAISE NOTICE 'Granted USAGE on schema % to openmemory_svc', v_schema;
    END IF;
END $$;

COMMIT;

DO $$ BEGIN RAISE NOTICE 'OpenMemory schema and permissions configured successfully'; END $$;
