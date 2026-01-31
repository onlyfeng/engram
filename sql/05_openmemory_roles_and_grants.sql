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
    v_current_owner TEXT;
BEGIN
    -- 优先从 session 变量获取
    v_schema := NULLIF(current_setting('om.target_schema', true), '');
    
    IF v_schema IS NULL THEN
        v_schema := 'openmemory';
    END IF;
    
    -- 创建 schema（如不存在）
    EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', v_schema);
    RAISE NOTICE 'Created/verified schema: %', v_schema;
    
    -- ============================================================
    -- 设置 schema owner = openmemory_migrator（关键安全要求）
    -- ALTER SCHEMA ... OWNER TO 是幂等的，重复执行无害
    -- ============================================================
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'openmemory_migrator') THEN
        -- 检查当前 owner
        SELECT nspowner::regrole::text INTO v_current_owner
        FROM pg_namespace
        WHERE nspname = v_schema;
        
        IF v_current_owner IS DISTINCT FROM 'openmemory_migrator' THEN
            EXECUTE format('ALTER SCHEMA %I OWNER TO openmemory_migrator', v_schema);
            RAISE NOTICE 'Set schema % owner to openmemory_migrator (was: %)', v_schema, v_current_owner;
        ELSE
            RAISE NOTICE 'Schema % owner is already openmemory_migrator', v_schema;
        END IF;
    ELSE
        RAISE WARNING 'openmemory_migrator role does not exist, cannot set schema owner';
    END IF;
    
    -- ============================================================
    -- 授予 openmemory_migrator 角色 schema 所有权限
    -- 注意：ALTER DEFAULT PRIVILEGES 必须使用 FOR ROLE 指定 grantor
    -- 这样无论当前连接用户是谁，默认权限都绑定到 openmemory_migrator
    -- ============================================================
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'openmemory_migrator') THEN
        EXECUTE format('GRANT ALL PRIVILEGES ON SCHEMA %I TO openmemory_migrator', v_schema);
        -- 使用 FOR ROLE 确保默认权限绑定到 openmemory_migrator
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE openmemory_migrator IN SCHEMA %I GRANT ALL PRIVILEGES ON TABLES TO openmemory_migrator', v_schema);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE openmemory_migrator IN SCHEMA %I GRANT ALL PRIVILEGES ON SEQUENCES TO openmemory_migrator', v_schema);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE openmemory_migrator IN SCHEMA %I GRANT ALL PRIVILEGES ON FUNCTIONS TO openmemory_migrator', v_schema);
        RAISE NOTICE 'Granted ALL on schema % to openmemory_migrator (with FOR ROLE default privileges)', v_schema;
    END IF;
    
    -- ============================================================
    -- 授予 openmemory_app 角色 DML 权限（仅 USAGE，无 CREATE）
    -- 使用 FOR ROLE openmemory_migrator 确保默认权限由 migrator 创建的对象继承
    -- ============================================================
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'openmemory_app') THEN
        -- 仅授予 USAGE，不授予 CREATE
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO openmemory_app', v_schema);
        -- 明确撤销 CREATE 权限（幂等操作，确保安全）
        EXECUTE format('REVOKE CREATE ON SCHEMA %I FROM openmemory_app', v_schema);
        -- 使用 FOR ROLE openmemory_migrator 设置默认权限
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE openmemory_migrator IN SCHEMA %I GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO openmemory_app', v_schema);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE openmemory_migrator IN SCHEMA %I GRANT USAGE, SELECT ON SEQUENCES TO openmemory_app', v_schema);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE openmemory_migrator IN SCHEMA %I GRANT EXECUTE ON FUNCTIONS TO openmemory_app', v_schema);
        RAISE NOTICE 'Granted DML (USAGE only, no CREATE) on schema % to openmemory_app', v_schema;
    END IF;
    
    -- ============================================================
    -- 授予登录角色权限（如存在）
    -- ============================================================
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'openmemory_migrator_login') THEN
        EXECUTE format('GRANT ALL PRIVILEGES ON SCHEMA %I TO openmemory_migrator_login', v_schema);
        RAISE NOTICE 'Granted ALL on schema % to openmemory_migrator_login', v_schema;
    END IF;
    
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'openmemory_svc') THEN
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO openmemory_svc', v_schema);
        -- 明确撤销 CREATE（继承安全策略）
        EXECUTE format('REVOKE CREATE ON SCHEMA %I FROM openmemory_svc', v_schema);
        RAISE NOTICE 'Granted USAGE (no CREATE) on schema % to openmemory_svc', v_schema;
    END IF;
END $$;

COMMIT;

DO $$ BEGIN RAISE NOTICE 'OpenMemory schema and permissions configured successfully'; END $$;
