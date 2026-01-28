-- ============================================================
-- Engram Step1: OpenMemory Schema 权限管理脚本（参数化版本）
-- ============================================================
--
-- 本脚本为 OpenMemory schema 配置权限，确保其不再依赖 public schema 的 CREATE 权限。
--
-- 参数化设计：
--   通过 PostgreSQL 自定义配置变量 'om.target_schema' 传入目标 schema 名称
--   若未设置，默认为 'openmemory'
--
-- 使用方式：
--   方式1 - psql 直接执行（推荐使用 wrapper 脚本）：
--     psql -c "SET om.target_schema = 'myproject_openmemory'" -f 05_openmemory_roles_and_grants.sql
--
--   方式2 - 使用 wrapper 脚本（读取 OM_PG_SCHEMA 环境变量）：
--     OM_PG_SCHEMA=myproject_openmemory ./05_openmemory_roles_and_grants.sh
--
--   方式3 - db_migrate.py 自动执行：
--     python db_migrate.py --public-policy openmemory
--
-- 角色设计：
--   openmemory_migrator  - OpenMemory 迁移专用角色（DDL 权限，创建表/索引）
--   openmemory_app       - OpenMemory 应用角色（DML 权限，读写数据）
--
-- 使用场景：
--   - 同库多 Schema 模式：OpenMemory 与 Step1 共用同一 PostgreSQL 数据库
--   - OpenMemory 默认 schema：openmemory
--   - 多租户部署：可设置为 <PROJECT_KEY>_openmemory，如 proj_a_openmemory
--
-- 关键点：
--   1. ALTER DEFAULT PRIVILEGES 仅为 openmemory_migrator 设置（使用 FOR ROLE）
--   2. Schema owner 设置为 openmemory_migrator 确保权限传递正确
--   3. 撤销 public schema 的 CREATE 权限，强制 OpenMemory 使用独立 schema
--
-- 迁移执行规范（重要）：
--   登录角色（如 openmemory_migrator_login）连接后必须执行：
--     SET ROLE openmemory_migrator;
--   然后再执行 DDL 操作。这样创建的对象归属于 openmemory_migrator，
--   默认权限才能正确生效。
--
-- 注意事项：
--   - 本脚本可重复执行（幂等）
--   - 需要使用 superuser 或具有 CREATEROLE 权限的用户执行
--   - OpenMemory 连接用户需要被授予 openmemory_migrator 或 openmemory_app 角色
--   - 迁移脚本必须在 DDL 前执行 SET ROLE openmemory_migrator
-- ============================================================

BEGIN;

-- ============================================================
-- 0. 解析目标 schema 参数
-- ============================================================
-- 使用 current_setting 读取自定义配置变量，支持默认值
-- 配置变量通过 SET om.target_schema = '...' 或 db_migrate.py 注入

DO $$
DECLARE
    v_schema TEXT;
BEGIN
    -- 尝试读取配置变量，若未设置则使用默认值 'openmemory'
    v_schema := COALESCE(
        NULLIF(current_setting('om.target_schema', true), ''),
        'openmemory'
    );
    
    -- 存储到临时表供后续使用
    CREATE TEMP TABLE IF NOT EXISTS _om_config (key TEXT PRIMARY KEY, value TEXT);
    INSERT INTO _om_config (key, value) VALUES ('target_schema', v_schema)
    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
    
    RAISE NOTICE 'OpenMemory target schema: %', v_schema;
END $$;

-- ============================================================
-- 1. 创建 OpenMemory 专用角色（如不存在）
-- ============================================================

-- openmemory_migrator: OpenMemory 迁移专用角色
-- 用于执行 schema DDL（CREATE TABLE, ALTER TABLE, CREATE INDEX 等）
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'openmemory_migrator') THEN
        CREATE ROLE openmemory_migrator NOLOGIN;
        RAISE NOTICE 'Created role: openmemory_migrator';
    END IF;
END $$;

-- openmemory_app: OpenMemory 应用角色
-- 用于日常业务的 DML 操作
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'openmemory_app') THEN
        CREATE ROLE openmemory_app NOLOGIN;
        RAISE NOTICE 'Created role: openmemory_app';
    END IF;
END $$;

DO $$ BEGIN RAISE NOTICE 'OpenMemory roles created'; END $$;

-- ============================================================
-- 2. 创建目标 schema（如不存在）并设置 owner
-- ============================================================
-- 确保 schema 存在，owner 设置为 openmemory_migrator
-- 这样由 openmemory_migrator 成员创建的对象会自动继承正确的权限

DO $$
DECLARE
    v_schema TEXT;
BEGIN
    SELECT value INTO v_schema FROM _om_config WHERE key = 'target_schema';
    
    IF NOT EXISTS (SELECT 1 FROM information_schema.schemata WHERE schema_name = v_schema) THEN
        EXECUTE format('CREATE SCHEMA %I AUTHORIZATION openmemory_migrator', v_schema);
        RAISE NOTICE 'Created schema: % (owner: openmemory_migrator)', v_schema;
    ELSE
        -- 确保 owner 正确
        EXECUTE format('ALTER SCHEMA %I OWNER TO openmemory_migrator', v_schema);
        RAISE NOTICE 'Schema % already exists, owner set to openmemory_migrator', v_schema;
    END IF;
END $$;

-- ============================================================
-- 3. 约束 OpenMemory 角色在 public schema 的权限
-- ============================================================
-- 禁止 OpenMemory 在 public schema 创建对象

REVOKE CREATE ON SCHEMA public FROM openmemory_migrator;
REVOKE CREATE ON SCHEMA public FROM openmemory_app;

-- 授予 public schema 的 USAGE 权限（用于访问扩展和系统函数）
GRANT USAGE ON SCHEMA public TO openmemory_migrator;
GRANT USAGE ON SCHEMA public TO openmemory_app;

DO $$ BEGIN RAISE NOTICE 'Restricted OpenMemory roles on public schema'; END $$;

-- ============================================================
-- 4. 授予目标 schema 级别权限
-- ============================================================

-- openmemory_migrator: 完全控制权限（CREATE/USAGE）
DO $$
DECLARE
    v_schema TEXT;
BEGIN
    SELECT value INTO v_schema FROM _om_config WHERE key = 'target_schema';
    EXECUTE format('GRANT ALL PRIVILEGES ON SCHEMA %I TO openmemory_migrator', v_schema);
END $$;

-- openmemory_app: 仅 USAGE 权限（不能创建对象）
DO $$
DECLARE
    v_schema TEXT;
BEGIN
    SELECT value INTO v_schema FROM _om_config WHERE key = 'target_schema';
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO openmemory_app', v_schema);
END $$;

-- engram 角色访问目标 schema（如果需要跨 schema 查询）
DO $$
DECLARE
    v_schema TEXT;
BEGIN
    SELECT value INTO v_schema FROM _om_config WHERE key = 'target_schema';
    
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'engram_admin') THEN
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO engram_admin', v_schema);
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO engram_app_readwrite', v_schema);
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO engram_app_readonly', v_schema);
        RAISE NOTICE 'Granted % USAGE to engram roles', v_schema;
    END IF;
END $$;

-- ============================================================
-- 5. 授予现有表和序列的权限
-- ============================================================

-- openmemory_migrator: 所有表/序列的完整权限
DO $$
DECLARE
    v_schema TEXT;
BEGIN
    SELECT value INTO v_schema FROM _om_config WHERE key = 'target_schema';
    EXECUTE format('GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA %I TO openmemory_migrator', v_schema);
    EXECUTE format('GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA %I TO openmemory_migrator', v_schema);
END $$;

-- openmemory_app: 表的 DML 权限和序列的 USAGE 权限
DO $$
DECLARE
    v_schema TEXT;
BEGIN
    SELECT value INTO v_schema FROM _om_config WHERE key = 'target_schema';
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I TO openmemory_app', v_schema);
    EXECUTE format('GRANT USAGE ON ALL SEQUENCES IN SCHEMA %I TO openmemory_app', v_schema);
END $$;

-- engram 角色可选的只读访问
DO $$
DECLARE
    v_schema TEXT;
BEGIN
    SELECT value INTO v_schema FROM _om_config WHERE key = 'target_schema';
    
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'engram_admin') THEN
        EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO engram_app_readonly', v_schema);
        EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO engram_app_readwrite', v_schema);
        RAISE NOTICE 'Granted % tables SELECT to engram roles', v_schema;
    END IF;
END $$;

-- ============================================================
-- 6. 设置默认权限（对未来创建的对象生效）
-- ============================================================
-- 关键：仅为 openmemory_migrator 设置默认权限（使用 FOR ROLE）
-- 登录角色必须先执行 SET ROLE openmemory_migrator 再执行 DDL
-- 这样创建的对象归属于 openmemory_migrator，以下默认权限自动生效

-- 默认权限：openmemory_migrator 创建的表
DO $$
DECLARE
    v_schema TEXT;
BEGIN
    SELECT value INTO v_schema FROM _om_config WHERE key = 'target_schema';
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE openmemory_migrator IN SCHEMA %I GRANT ALL PRIVILEGES ON TABLES TO openmemory_migrator', v_schema);
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE openmemory_migrator IN SCHEMA %I GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO openmemory_app', v_schema);
END $$;

-- 默认权限：openmemory_migrator 创建的序列
DO $$
DECLARE
    v_schema TEXT;
BEGIN
    SELECT value INTO v_schema FROM _om_config WHERE key = 'target_schema';
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE openmemory_migrator IN SCHEMA %I GRANT ALL PRIVILEGES ON SEQUENCES TO openmemory_migrator', v_schema);
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE openmemory_migrator IN SCHEMA %I GRANT USAGE ON SEQUENCES TO openmemory_app', v_schema);
END $$;

-- 默认权限：openmemory_migrator 创建的函数
DO $$
DECLARE
    v_schema TEXT;
BEGIN
    SELECT value INTO v_schema FROM _om_config WHERE key = 'target_schema';
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE openmemory_migrator IN SCHEMA %I GRANT EXECUTE ON FUNCTIONS TO openmemory_app', v_schema);
END $$;

-- 可选：engram 角色的默认只读权限
DO $$
DECLARE
    v_schema TEXT;
BEGIN
    SELECT value INTO v_schema FROM _om_config WHERE key = 'target_schema';
    
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'engram_app_readonly') THEN
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE openmemory_migrator IN SCHEMA %I GRANT SELECT ON TABLES TO engram_app_readonly', v_schema);
        RAISE NOTICE 'Set default SELECT privileges for engram_app_readonly';
    END IF;
END $$;

-- 注意：不为 openmemory_migrator_login 设置默认权限
-- 登录角色执行迁移时必须先 SET ROLE openmemory_migrator
-- 这样所有创建的对象都归属于 openmemory_migrator，权限管理更加统一

DO $$ BEGIN RAISE NOTICE 'Default privileges for target schema configured (FOR ROLE openmemory_migrator only)'; END $$;

-- ============================================================
-- 7. 设置 schema 级别默认权限（备用方案）
-- ============================================================
-- 当 schema owner 自己创建对象时生效

DO $$
DECLARE
    v_schema TEXT;
BEGIN
    SELECT value INTO v_schema FROM _om_config WHERE key = 'target_schema';
    EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT ALL PRIVILEGES ON TABLES TO openmemory_migrator', v_schema);
    EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO openmemory_app', v_schema);
    EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT ALL PRIVILEGES ON SEQUENCES TO openmemory_migrator', v_schema);
    EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT USAGE ON SEQUENCES TO openmemory_app', v_schema);
END $$;

-- ============================================================
-- 8. 清理临时表并输出确认信息
-- ============================================================

DO $$
DECLARE
    v_schema TEXT;
BEGIN
    SELECT value INTO v_schema FROM _om_config WHERE key = 'target_schema';
    RAISE NOTICE 'OpenMemory schema permissions applied for schema: %', v_schema;
END $$;

-- 清理临时配置表
DROP TABLE IF EXISTS _om_config;

-- ============================================================
-- 9. 登录角色绑定说明
-- ============================================================
--
-- OpenMemory 需要一个登录用户来执行迁移和应用操作。
-- 推荐创建专用登录角色并授予相应权限：
--
-- 重要：迁移执行时必须 SET ROLE
--   登录角色连接后，执行 DDL 前必须先执行：
--     SET ROLE openmemory_migrator;
--   这样创建的对象归属于 openmemory_migrator，默认权限才能正确生效。
--
-- 方案 A：单一登录用户（同时用于迁移和应用）
--   CREATE ROLE openmemory_login LOGIN PASSWORD 'your_password';
--   GRANT openmemory_migrator TO openmemory_login;
--   GRANT openmemory_app TO openmemory_login;
--   -- 在 OpenMemory 配置中设置:
--   -- OM_PG_USER=openmemory_login
--   -- OM_PG_PASSWORD=your_password
--   -- OM_PG_SCHEMA=openmemory
--   -- 迁移脚本执行前: SET ROLE openmemory_migrator;
--
-- 方案 B：分离迁移和应用用户
--   -- 迁移用户（用于 DDL）
--   CREATE ROLE openmemory_migrator_login LOGIN PASSWORD 'migrator_password';
--   GRANT openmemory_migrator TO openmemory_migrator_login;
--   -- 迁移时必须: SET ROLE openmemory_migrator;
--   
--   -- 应用用户（用于 DML）
--   CREATE ROLE openmemory_app_login LOGIN PASSWORD 'app_password';
--   GRANT openmemory_app TO openmemory_app_login;
--
-- 方案 C：使用现有 engram 用户（如果已配置）
--   GRANT openmemory_migrator TO engram_migrator_login;
--   GRANT openmemory_app TO engram_app_login;
--   -- 迁移时必须: SET ROLE openmemory_migrator;
--

COMMIT;

-- ============================================================
-- 验证脚本（可选，取消注释执行）
-- ============================================================
/*
-- 验证角色是否创建
SELECT rolname, rolcanlogin, rolcreaterole, rolcreatedb 
FROM pg_roles 
WHERE rolname LIKE 'openmemory%';

-- 验证 schema 权限（需要手动替换 schema 名）
SELECT 
    nspname AS schema_name,
    pg_catalog.has_schema_privilege('openmemory_migrator', nspname, 'CREATE') AS migrator_create,
    pg_catalog.has_schema_privilege('openmemory_app', nspname, 'USAGE') AS app_usage
FROM pg_namespace
WHERE nspname IN ('openmemory', 'public');

-- 验证 public schema 的 CREATE 权限已撤销
SELECT 
    pg_catalog.has_schema_privilege('openmemory_migrator', 'public', 'CREATE') AS migrator_public_create,
    pg_catalog.has_schema_privilege('openmemory_app', 'public', 'CREATE') AS app_public_create;

-- 验证默认权限配置（应只显示 openmemory_migrator 作为 grantor）
SELECT 
    defaclrole::regrole AS grantor,
    defaclnamespace::regnamespace AS schema,
    defaclobjtype AS object_type,
    defaclacl AS acl
FROM pg_default_acl
WHERE defaclnamespace IN (SELECT oid FROM pg_namespace WHERE nspname LIKE '%openmemory%');

-- ============================================================
-- 验证 SET ROLE 迁移流程（迁移脚本必须遵循）
-- ============================================================
-- 1. 使用 openmemory_migrator_login 连接
-- 2. 执行 SET ROLE openmemory_migrator;
-- 3. 验证当前角色：SELECT current_user, session_user, current_role;
--    应显示 current_role = openmemory_migrator
-- 4. 执行 DDL（CREATE TABLE 等）
-- 5. 验证对象 owner：SELECT tableowner FROM pg_tables WHERE tablename = 'your_table';
--    应显示 owner = openmemory_migrator
*/
