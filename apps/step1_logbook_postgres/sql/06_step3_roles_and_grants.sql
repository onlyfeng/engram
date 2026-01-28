-- ============================================================
-- Engram Step3: Seek Index Schema 权限管理脚本
-- ============================================================
--
-- 本脚本为 Step3 Seek Index（chunks 表 + 向量/全文索引）配置权限。
--
-- 角色设计：
--   step3_migrator  - Step3 迁移专用角色（DDL 权限，创建表/索引）
--   step3_app       - Step3 应用角色（DML 权限，读写数据）
--
-- 服务账号设计（LOGIN 角色）：
--   step3_migrator_login - 迁移账号，继承 step3_migrator
--   step3_svc            - 运行账号，继承 step3_app
--
-- 使用场景：
--   - Step3 Seek Index 使用独立的 step3 schema
--   - 存放 chunks 表和相关向量/全文索引
--   - 与 Step1/OpenMemory 同库部署
--
-- 关键点：
--   1. ALTER DEFAULT PRIVILEGES 仅为 step3_migrator 设置（使用 FOR ROLE）
--   2. Schema owner 设置为 step3_migrator 确保权限传递正确
--   3. 撤销 public schema 的 CREATE 权限，强制使用独立 schema
--
-- 权限说明：
--   step3_migrator 具备的 DDL 能力：
--     - CREATE TABLE / ALTER TABLE / DROP TABLE
--     - CREATE INDEX / DROP INDEX（包括 CONCURRENTLY）
--     - CREATE SEQUENCE / ALTER SEQUENCE
--     - 注意：CREATE INDEX CONCURRENTLY 不能在事务块中执行，
--            迁移工具需支持非事务模式或单独执行索引迁移
--
--   step3_app 具备的 DML 能力：
--     - SELECT / INSERT / UPDATE / DELETE（不含 TRUNCATE）
--     - USAGE on sequences（用于 SERIAL/BIGSERIAL 列）
--     - EXECUTE on functions（用于扩展函数如 vector 操作）
--
-- 迁移执行规范（重要）：
--   登录角色（如 step3_migrator_login）连接后必须执行：
--     SET ROLE step3_migrator;
--   然后再执行 DDL 操作。这样创建的对象归属于 step3_migrator，
--   默认权限才能正确生效。
--
-- 注意事项：
--   - 本脚本可重复执行（幂等）
--   - 需要使用 superuser 或具有 CREATEROLE 权限的用户执行
-- ============================================================

BEGIN;

-- ============================================================
-- 1. 创建 Step3 专用角色（NOLOGIN，如不存在）
-- ============================================================

-- step3_migrator: Step3 迁移专用角色
-- 用于执行 schema DDL（CREATE TABLE, ALTER TABLE, CREATE INDEX 等）
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'step3_migrator') THEN
        CREATE ROLE step3_migrator NOLOGIN;
        RAISE NOTICE 'Created role: step3_migrator';
    END IF;
END $$;

-- step3_app: Step3 应用角色
-- 用于日常业务的 DML 操作
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'step3_app') THEN
        CREATE ROLE step3_app NOLOGIN;
        RAISE NOTICE 'Created role: step3_app';
    END IF;
END $$;

DO $$ BEGIN RAISE NOTICE 'Step3 NOLOGIN roles created'; END $$;

-- ============================================================
-- 2. 创建 step3 schema（如不存在）并设置 owner
-- ============================================================
-- 确保 schema 存在，owner 设置为 step3_migrator
-- 这样由 step3_migrator 成员创建的对象会自动继承正确的权限

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.schemata WHERE schema_name = 'step3') THEN
        CREATE SCHEMA step3 AUTHORIZATION step3_migrator;
        RAISE NOTICE 'Created schema: step3 (owner: step3_migrator)';
    ELSE
        -- 确保 owner 正确
        ALTER SCHEMA step3 OWNER TO step3_migrator;
        RAISE NOTICE 'Schema step3 already exists, owner set to step3_migrator';
    END IF;
END $$;

-- ============================================================
-- 3. 约束 Step3 角色在 public schema 的权限
-- ============================================================
-- 禁止 Step3 在 public schema 创建对象

REVOKE CREATE ON SCHEMA public FROM step3_migrator;
REVOKE CREATE ON SCHEMA public FROM step3_app;

-- 授予 public schema 的 USAGE 权限（用于访问扩展和系统函数）
-- 这是访问 pgvector、pg_trgm 等扩展函数所必需的
GRANT USAGE ON SCHEMA public TO step3_migrator;
GRANT USAGE ON SCHEMA public TO step3_app;

-- 授予 public schema 中扩展函数的 EXECUTE 权限
-- 确保可以使用 vector 操作符、全文搜索函数等
DO $$
BEGIN
    -- 授予现有函数的 EXECUTE 权限
    GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO step3_migrator;
    GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO step3_app;
    RAISE NOTICE 'Granted EXECUTE on public schema functions to Step3 roles';
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'Note: Could not grant all functions in public (may be empty): %', SQLERRM;
END $$;

DO $$ BEGIN RAISE NOTICE 'Restricted Step3 roles on public schema'; END $$;

-- ============================================================
-- 4. 授予 step3 schema 级别权限
-- ============================================================

-- step3_migrator: 完全控制权限（CREATE/USAGE）
GRANT ALL PRIVILEGES ON SCHEMA step3 TO step3_migrator;

-- step3_app: 仅 USAGE 权限（不能创建对象）
GRANT USAGE ON SCHEMA step3 TO step3_app;

-- engram 角色访问 step3 schema（如果需要跨 schema 查询）
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'engram_admin') THEN
        GRANT USAGE ON SCHEMA step3 TO engram_admin;
        GRANT USAGE ON SCHEMA step3 TO engram_app_readwrite;
        GRANT USAGE ON SCHEMA step3 TO engram_app_readonly;
        RAISE NOTICE 'Granted step3 USAGE to engram roles';
    END IF;
END $$;

-- openmemory 角色访问 step3 schema（如果需要跨 schema 查询）
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'openmemory_app') THEN
        GRANT USAGE ON SCHEMA step3 TO openmemory_app;
        RAISE NOTICE 'Granted step3 USAGE to openmemory_app';
    END IF;
END $$;

DO $$ BEGIN RAISE NOTICE 'Granted step3 schema permissions'; END $$;

-- ============================================================
-- 5. 授予现有表和序列的权限
-- ============================================================

-- step3_migrator: 所有表/序列的完整权限
-- 包括 SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
-- 注意：CREATE INDEX 权限通过 ALL PRIVILEGES ON SCHEMA 已获得
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA step3 TO step3_migrator;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA step3 TO step3_migrator;

-- step3_app: 表的 DML 权限和序列的 USAGE 权限
-- 注意：不包含 TRUNCATE（仅 migrator 可执行表清空操作）
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA step3 TO step3_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA step3 TO step3_app;

-- engram 角色可选的只读访问
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'engram_admin') THEN
        GRANT SELECT ON ALL TABLES IN SCHEMA step3 TO engram_app_readonly;
        GRANT SELECT ON ALL TABLES IN SCHEMA step3 TO engram_app_readwrite;
        RAISE NOTICE 'Granted step3 tables SELECT to engram roles';
    END IF;
END $$;

-- ============================================================
-- 6. 设置默认权限（对未来创建的对象生效）
-- ============================================================
-- 关键：仅为 step3_migrator 设置默认权限（使用 FOR ROLE）
-- 登录角色必须先执行 SET ROLE step3_migrator 再执行 DDL
-- 这样创建的对象归属于 step3_migrator，以下默认权限自动生效

-- 默认权限：step3_migrator 创建的表
ALTER DEFAULT PRIVILEGES FOR ROLE step3_migrator IN SCHEMA step3
    GRANT ALL PRIVILEGES ON TABLES TO step3_migrator;
ALTER DEFAULT PRIVILEGES FOR ROLE step3_migrator IN SCHEMA step3
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO step3_app;

-- 默认权限：step3_migrator 创建的序列
ALTER DEFAULT PRIVILEGES FOR ROLE step3_migrator IN SCHEMA step3
    GRANT ALL PRIVILEGES ON SEQUENCES TO step3_migrator;
ALTER DEFAULT PRIVILEGES FOR ROLE step3_migrator IN SCHEMA step3
    GRANT USAGE ON SEQUENCES TO step3_app;

-- 默认权限：step3_migrator 创建的函数
ALTER DEFAULT PRIVILEGES FOR ROLE step3_migrator IN SCHEMA step3
    GRANT EXECUTE ON FUNCTIONS TO step3_app;

-- 默认权限：step3_migrator 创建的类型（如自定义枚举）
ALTER DEFAULT PRIVILEGES FOR ROLE step3_migrator IN SCHEMA step3
    GRANT USAGE ON TYPES TO step3_app;

-- 可选：engram 角色的默认只读权限
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'engram_app_readonly') THEN
        EXECUTE 'ALTER DEFAULT PRIVILEGES FOR ROLE step3_migrator IN SCHEMA step3 GRANT SELECT ON TABLES TO engram_app_readonly';
        RAISE NOTICE 'Set default SELECT privileges for engram_app_readonly';
    END IF;
END $$;

DO $$ BEGIN RAISE NOTICE 'Default privileges for step3 schema configured (FOR ROLE step3_migrator only)'; END $$;

-- ============================================================
-- 7. 设置 schema 级别默认权限（备用方案）
-- ============================================================
-- 当 schema owner 自己创建对象时生效（如 superuser 直接创建）

ALTER DEFAULT PRIVILEGES IN SCHEMA step3
    GRANT ALL PRIVILEGES ON TABLES TO step3_migrator;
ALTER DEFAULT PRIVILEGES IN SCHEMA step3
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO step3_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA step3
    GRANT ALL PRIVILEGES ON SEQUENCES TO step3_migrator;
ALTER DEFAULT PRIVILEGES IN SCHEMA step3
    GRANT USAGE ON SEQUENCES TO step3_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA step3
    GRANT EXECUTE ON FUNCTIONS TO step3_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA step3
    GRANT USAGE ON TYPES TO step3_app;

-- ============================================================
-- 8. 授予 LOGIN 角色 membership（继承权限）
-- ============================================================
-- LOGIN 角色由 00_init_service_accounts.sh 创建
-- 本节为这些 LOGIN 角色授予对应 NOLOGIN 角色的成员身份

-- step3_migrator_login -> step3_migrator
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'step3_migrator_login') THEN
        EXECUTE 'GRANT step3_migrator TO step3_migrator_login';
        RAISE NOTICE 'Granted step3_migrator TO step3_migrator_login';
    ELSE
        RAISE NOTICE 'step3_migrator_login not found, skipping membership grant';
    END IF;
END $$;

-- step3_svc -> step3_app
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'step3_svc') THEN
        EXECUTE 'GRANT step3_app TO step3_svc';
        RAISE NOTICE 'Granted step3_app TO step3_svc';
    ELSE
        RAISE NOTICE 'step3_svc not found, skipping membership grant';
    END IF;
END $$;

DO $$ BEGIN RAISE NOTICE 'Step3 service account memberships configured'; END $$;

-- ============================================================
-- 9. 授予数据库级权限
-- ============================================================
-- 迁移角色需要 CONNECT、CREATE、TEMP 权限
-- 应用角色仅需 CONNECT 权限

DO $$
DECLARE
    v_db TEXT;
BEGIN
    v_db := current_database();
    
    -- step3_migrator: CONNECT, CREATE, TEMP
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO step3_migrator', v_db);
    EXECUTE format('GRANT CREATE ON DATABASE %I TO step3_migrator', v_db);
    EXECUTE format('GRANT TEMP ON DATABASE %I TO step3_migrator', v_db);
    
    -- step3_app: 仅 CONNECT
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO step3_app', v_db);
    
    RAISE NOTICE 'Granted database privileges for Step3 roles on %', v_db;
END $$;

COMMIT;

DO $$ BEGIN RAISE NOTICE 'Step3 schema permissions applied successfully'; END $$;

-- ============================================================
-- 登录角色绑定说明
-- ============================================================
--
-- Step3 需要登录用户来执行迁移和应用操作。
-- LOGIN 角色由 00_init_service_accounts.sh 创建，需设置环境变量：
--   STEP3_MIGRATOR_PASSWORD - Step3 迁移账号密码
--   STEP3_SVC_PASSWORD      - Step3 运行账号密码
--
-- 重要：迁移执行时必须 SET ROLE
--   登录角色连接后，执行 DDL 前必须先执行：
--     SET ROLE step3_migrator;
--   这样创建的对象归属于 step3_migrator，默认权限才能正确生效。
--
-- 服务账号映射：
--   step3_migrator_login -> step3_migrator (DDL)
--   step3_svc            -> step3_app      (DML)
--
