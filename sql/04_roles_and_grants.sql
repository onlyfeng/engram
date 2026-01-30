-- ============================================================
-- Engram Logbook: 角色与权限管理脚本
-- ============================================================
--
-- 本脚本定义数据库角色及其在各 schema 上的权限，支持多租户隔离。
--
-- 角色设计（NOLOGIN 权限角色）：
--   Engram Logbook 角色（用于 Engram 专属 schema）：
--     engram_admin        - 超级管理员，拥有所有 schema 的全部权限
--     engram_migrator     - 迁移专用角色，用于 DDL 操作（CREATE/ALTER/DROP）
--     engram_app_readwrite - 应用读写角色，用于 DML 操作（SELECT/INSERT/UPDATE/DELETE）
--     engram_app_readonly  - 应用只读角色，仅 SELECT 权限
--
--   OpenMemory 角色（用于 openmemory schema）：
--     openmemory_migrator  - OpenMemory 迁移角色，DDL 权限
--     openmemory_app       - OpenMemory 应用角色，DML 权限
--
-- 服务账号设计（LOGIN 角色，由 00_init_service_accounts.sh 创建）：
--   Logbook 服务账号：
--     logbook_migrator           - 迁移账号，继承 engram_migrator
--     logbook_svc                - 运行账号，继承 engram_app_readwrite
--
--   OpenMemory 服务账号：
--     openmemory_migrator_login - 迁移账号，继承 openmemory_migrator
--     openmemory_svc            - 运行账号，继承 openmemory_app
--
-- public schema 策略配置（strict 策略）：
--   本脚本强制所有角色在 public schema 没有 CREATE 权限：
--   - 所有 Engram 角色不能在 public CREATE
--   - 所有 OpenMemory 角色不能在 public CREATE
--   - 所有角色保留 public 的 USAGE 权限（用于访问扩展和系统函数）
--
--   OpenMemory 的独立 schema：
--     OpenMemory 组件应使用 05_openmemory_roles_and_grants.sql 创建独立 schema
--     openmemory_migrator 将在该独立 schema 中拥有 CREATE 权限
--
-- 多租户说明：
--   - 角色名不带租户前缀，所有租户共用同一套角色
--   - 权限通过 GRANT ON ALL TABLES IN SCHEMA 授予特定 schema
--   - 运行时通过 search_path 隔离不同租户的 schema
--
-- 执行方式：
--   psql -d <your_db> -f 04_roles_and_grants.sql
--   或在 db_migrate.py 中使用 --apply-roles 选项自动执行
--
-- 注意事项：
--   - 本脚本可重复执行（幂等）
--   - 需要使用 superuser 或具有 CREATEROLE 权限的用户执行
--   - 迁移脚本会自动替换 schema 名以支持多租户前缀
-- ============================================================

BEGIN;

-- ============================================================
-- 1. 创建角色（如不存在）
-- ============================================================

-- engram_admin: 超级管理员角色
-- 拥有所有 schema 的完整权限，可创建/删除对象
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'engram_admin') THEN
        CREATE ROLE engram_admin NOLOGIN;
        RAISE NOTICE 'Created role: engram_admin';
    END IF;
END $$;

-- engram_migrator: 迁移专用角色
-- 用于执行 schema DDL（CREATE TABLE, ALTER TABLE, CREATE INDEX 等）
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'engram_migrator') THEN
        CREATE ROLE engram_migrator NOLOGIN;
        RAISE NOTICE 'Created role: engram_migrator';
    END IF;
END $$;

-- engram_app_readwrite: 应用读写角色
-- 用于日常业务的 DML 操作
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'engram_app_readwrite') THEN
        CREATE ROLE engram_app_readwrite NOLOGIN;
        RAISE NOTICE 'Created role: engram_app_readwrite';
    END IF;
END $$;

-- engram_app_readonly: 应用只读角色
-- 仅 SELECT 权限，用于报表、分析等只读场景
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'engram_app_readonly') THEN
        CREATE ROLE engram_app_readonly NOLOGIN;
        RAISE NOTICE 'Created role: engram_app_readonly';
    END IF;
END $$;

-- ============================================================
-- 1.5 创建 OpenMemory 角色（用于 mem0/qdrant 等第三方组件）
-- ============================================================

-- openmemory_migrator: OpenMemory 迁移角色
-- 用于在 public schema 中创建表结构（DDL）
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'openmemory_migrator') THEN
        CREATE ROLE openmemory_migrator NOLOGIN;
        RAISE NOTICE 'Created role: openmemory_migrator';
    END IF;
END $$;

-- openmemory_app: OpenMemory 应用角色
-- 用于 public schema 的 DML 操作
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'openmemory_app') THEN
        CREATE ROLE openmemory_app NOLOGIN;
        RAISE NOTICE 'Created role: openmemory_app';
    END IF;
END $$;

-- ============================================================
-- 1.6 授予 LOGIN 角色 membership（继承权限）
-- ============================================================
-- LOGIN 角色由 00_init_service_accounts.sh 创建
-- 本节为这些 LOGIN 角色授予对应 NOLOGIN 角色的成员身份
--
-- 服务账号映射：
--   Logbook 服务账号：
--     logbook_migrator           -> engram_migrator      (DDL)
--     logbook_svc                -> engram_app_readwrite (DML)
--
--   OpenMemory 服务账号：
--     openmemory_migrator_login -> openmemory_migrator  (DDL)
--     openmemory_svc            -> openmemory_app       (DML)

-- Logbook 服务账号 membership
DO $$
BEGIN
    -- logbook_migrator -> engram_migrator
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'logbook_migrator') THEN
        EXECUTE 'GRANT engram_migrator TO logbook_migrator';
        RAISE NOTICE 'Granted engram_migrator TO logbook_migrator';
    END IF;
    
    -- logbook_svc -> engram_app_readwrite
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'logbook_svc') THEN
        EXECUTE 'GRANT engram_app_readwrite TO logbook_svc';
        RAISE NOTICE 'Granted engram_app_readwrite TO logbook_svc';
    END IF;
END $$;

-- OpenMemory 服务账号 membership
DO $$
BEGIN
    -- openmemory_migrator_login -> openmemory_migrator
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'openmemory_migrator_login') THEN
        EXECUTE 'GRANT openmemory_migrator TO openmemory_migrator_login';
        RAISE NOTICE 'Granted openmemory_migrator TO openmemory_migrator_login';
    END IF;
    
    -- openmemory_svc -> openmemory_app
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'openmemory_svc') THEN
        EXECUTE 'GRANT openmemory_app TO openmemory_svc';
        RAISE NOTICE 'Granted openmemory_app TO openmemory_svc';
    END IF;
END $$;

DO $$ BEGIN RAISE NOTICE 'Service account memberships configured'; END $$;

-- ============================================================
-- 2. 约束 public schema（strict 策略）
-- ============================================================
-- 禁用 public schema 的 CREATE 权限，防止对象被误创建到 public
-- 这确保所有对象都显式创建在正确的 schema 中
--
-- 策略说明：
--   所有角色都不能在 public CREATE，仅保留 USAGE 权限
--   OpenMemory 应使用独立 schema（通过 05_openmemory_roles_and_grants.sql 创建）

-- 撤销 PUBLIC 在 public schema 上的 CREATE 权限（所有策略都执行）
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- 撤销 Engram 角色在 public schema 上的 CREATE 权限（所有策略都执行）
REVOKE CREATE ON SCHEMA public FROM engram_admin;
REVOKE CREATE ON SCHEMA public FROM engram_migrator;
REVOKE CREATE ON SCHEMA public FROM engram_app_readwrite;
REVOKE CREATE ON SCHEMA public FROM engram_app_readonly;

-- 授予 public schema 的 USAGE 权限（用于访问扩展和系统函数）
GRANT USAGE ON SCHEMA public TO engram_admin;
GRANT USAGE ON SCHEMA public TO engram_migrator;
GRANT USAGE ON SCHEMA public TO engram_app_readwrite;
GRANT USAGE ON SCHEMA public TO engram_app_readonly;

-- 配置 OpenMemory 角色权限（strict 策略）
-- 撤销 public schema 的 CREATE 权限
-- OpenMemory 应使用独立的 openmemory schema（由 05_openmemory_roles_and_grants.sql 创建）
REVOKE CREATE ON SCHEMA public FROM openmemory_migrator;
REVOKE CREATE ON SCHEMA public FROM openmemory_app;

-- 保留 USAGE 权限（用于访问扩展和系统函数）
GRANT USAGE ON SCHEMA public TO openmemory_migrator;
GRANT USAGE ON SCHEMA public TO openmemory_app;

DO $$ BEGIN RAISE NOTICE 'All roles denied CREATE on public schema (USAGE preserved)'; END $$;

DO $$ BEGIN RAISE NOTICE 'Configured public schema permissions'; END $$;

-- ============================================================
-- 3. 授予 schema 级别权限
-- ============================================================

-- ---------- identity schema ----------
GRANT ALL PRIVILEGES ON SCHEMA identity TO engram_admin;
GRANT ALL PRIVILEGES ON SCHEMA identity TO engram_migrator;
GRANT USAGE ON SCHEMA identity TO engram_app_readwrite;
GRANT USAGE ON SCHEMA identity TO engram_app_readonly;

-- ---------- logbook schema ----------
GRANT ALL PRIVILEGES ON SCHEMA logbook TO engram_admin;
GRANT ALL PRIVILEGES ON SCHEMA logbook TO engram_migrator;
GRANT USAGE ON SCHEMA logbook TO engram_app_readwrite;
GRANT USAGE ON SCHEMA logbook TO engram_app_readonly;

-- ---------- scm schema ----------
GRANT ALL PRIVILEGES ON SCHEMA scm TO engram_admin;
GRANT ALL PRIVILEGES ON SCHEMA scm TO engram_migrator;
GRANT USAGE ON SCHEMA scm TO engram_app_readwrite;
GRANT USAGE ON SCHEMA scm TO engram_app_readonly;

-- ---------- analysis schema ----------
GRANT ALL PRIVILEGES ON SCHEMA analysis TO engram_admin;
GRANT ALL PRIVILEGES ON SCHEMA analysis TO engram_migrator;
GRANT USAGE ON SCHEMA analysis TO engram_app_readwrite;
GRANT USAGE ON SCHEMA analysis TO engram_app_readonly;

-- ---------- governance schema ----------
GRANT ALL PRIVILEGES ON SCHEMA governance TO engram_admin;
GRANT ALL PRIVILEGES ON SCHEMA governance TO engram_migrator;
GRANT USAGE ON SCHEMA governance TO engram_app_readwrite;
GRANT USAGE ON SCHEMA governance TO engram_app_readonly;

-- ============================================================
-- 4. 授予表级别权限
-- ============================================================

-- ---------- engram_admin: 所有表的完整权限 ----------
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA identity TO engram_admin;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA logbook TO engram_admin;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA scm TO engram_admin;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA analysis TO engram_admin;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA governance TO engram_admin;

-- ---------- engram_migrator: DDL 和 DML 权限 ----------
-- migrator 需要能够操作表结构和数据（用于迁移）
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA identity TO engram_migrator;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA logbook TO engram_migrator;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA scm TO engram_migrator;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA analysis TO engram_migrator;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA governance TO engram_migrator;

-- ---------- engram_app_readwrite: SELECT/INSERT/UPDATE/DELETE 权限 ----------
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA identity TO engram_app_readwrite;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA logbook TO engram_app_readwrite;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA scm TO engram_app_readwrite;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA analysis TO engram_app_readwrite;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA governance TO engram_app_readwrite;

-- ---------- engram_app_readonly: 仅 SELECT 权限 ----------
GRANT SELECT ON ALL TABLES IN SCHEMA identity TO engram_app_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA logbook TO engram_app_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA scm TO engram_app_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA analysis TO engram_app_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA governance TO engram_app_readonly;

-- ============================================================
-- 5. 授予序列（SEQUENCE）权限
-- ============================================================
-- 应用需要 USAGE 权限来使用 SERIAL/BIGSERIAL 列

GRANT USAGE ON ALL SEQUENCES IN SCHEMA identity TO engram_app_readwrite;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA logbook TO engram_app_readwrite;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA scm TO engram_app_readwrite;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA analysis TO engram_app_readwrite;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA governance TO engram_app_readwrite;

GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA identity TO engram_admin;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA logbook TO engram_admin;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA scm TO engram_admin;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA analysis TO engram_admin;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA governance TO engram_admin;

GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA identity TO engram_migrator;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA logbook TO engram_migrator;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA scm TO engram_migrator;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA analysis TO engram_migrator;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA governance TO engram_migrator;

-- ============================================================
-- 6. 设置默认权限（对未来创建的对象生效）
-- ============================================================
-- 
-- 重要说明：
--   ALTER DEFAULT PRIVILEGES 必须指定 FOR ROLE <grantor>，否则只对当前用户创建的对象生效。
--   engram_migrator 是用于执行 DDL 的 NOLOGIN 权限角色。
--   登录角色（如 logbook_migrator）执行迁移时需要先 SET ROLE engram_migrator，
--   这样创建的对象的 owner 就是 engram_migrator，默认权限才能正确生效。
--
-- 迁移脚本执行规范：
--   登录后必须执行 SET ROLE engram_migrator; 再执行 DDL 操作。
--   这确保创建的对象归属于 engram_migrator，从而触发默认权限授予。
--
-- 权限设计原则（最小权限）：
--   - engram_admin:        ALL PRIVILEGES（完整管理权限）
--   - engram_migrator:     ALL PRIVILEGES（DDL + DML，用于迁移）
--   - engram_app_readwrite: SELECT/INSERT/UPDATE/DELETE（仅 DML，无 DDL）
--   - engram_app_readonly:  SELECT（仅读取）
--
-- 覆盖对象类型：
--   - TABLES:    表
--   - SEQUENCES: 序列（用于 SERIAL/BIGSERIAL 列）
--   - FUNCTIONS: 函数（存储过程和函数）

-- ============================================================
-- 6.1 为 engram_migrator 设置默认权限（关键：使用 FOR ROLE）
-- ============================================================
-- engram_migrator 是执行迁移的 NOLOGIN 权限角色
-- 登录角色（如 logbook_migrator）通过 SET ROLE engram_migrator 执行 DDL
-- 这样创建的对象归属于 engram_migrator，以下默认权限生效

-- identity schema - engram_migrator 创建对象的默认权限
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA identity
    GRANT ALL PRIVILEGES ON TABLES TO engram_admin, engram_migrator;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA identity
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO engram_app_readwrite;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA identity
    GRANT SELECT ON TABLES TO engram_app_readonly;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA identity
    GRANT ALL PRIVILEGES ON SEQUENCES TO engram_admin, engram_migrator;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA identity
    GRANT USAGE ON SEQUENCES TO engram_app_readwrite;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA identity
    GRANT EXECUTE ON FUNCTIONS TO engram_admin, engram_migrator, engram_app_readwrite;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA identity
    GRANT EXECUTE ON FUNCTIONS TO engram_app_readonly;

-- logbook schema - engram_migrator 创建对象的默认权限
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA logbook
    GRANT ALL PRIVILEGES ON TABLES TO engram_admin, engram_migrator;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA logbook
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO engram_app_readwrite;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA logbook
    GRANT SELECT ON TABLES TO engram_app_readonly;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA logbook
    GRANT ALL PRIVILEGES ON SEQUENCES TO engram_admin, engram_migrator;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA logbook
    GRANT USAGE ON SEQUENCES TO engram_app_readwrite;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA logbook
    GRANT EXECUTE ON FUNCTIONS TO engram_admin, engram_migrator, engram_app_readwrite;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA logbook
    GRANT EXECUTE ON FUNCTIONS TO engram_app_readonly;

-- scm schema - engram_migrator 创建对象的默认权限
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA scm
    GRANT ALL PRIVILEGES ON TABLES TO engram_admin, engram_migrator;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA scm
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO engram_app_readwrite;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA scm
    GRANT SELECT ON TABLES TO engram_app_readonly;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA scm
    GRANT ALL PRIVILEGES ON SEQUENCES TO engram_admin, engram_migrator;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA scm
    GRANT USAGE ON SEQUENCES TO engram_app_readwrite;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA scm
    GRANT EXECUTE ON FUNCTIONS TO engram_admin, engram_migrator, engram_app_readwrite;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA scm
    GRANT EXECUTE ON FUNCTIONS TO engram_app_readonly;

-- analysis schema - engram_migrator 创建对象的默认权限
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA analysis
    GRANT ALL PRIVILEGES ON TABLES TO engram_admin, engram_migrator;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA analysis
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO engram_app_readwrite;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA analysis
    GRANT SELECT ON TABLES TO engram_app_readonly;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA analysis
    GRANT ALL PRIVILEGES ON SEQUENCES TO engram_admin, engram_migrator;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA analysis
    GRANT USAGE ON SEQUENCES TO engram_app_readwrite;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA analysis
    GRANT EXECUTE ON FUNCTIONS TO engram_admin, engram_migrator, engram_app_readwrite;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA analysis
    GRANT EXECUTE ON FUNCTIONS TO engram_app_readonly;

-- governance schema - engram_migrator 创建对象的默认权限
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA governance
    GRANT ALL PRIVILEGES ON TABLES TO engram_admin, engram_migrator;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA governance
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO engram_app_readwrite;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA governance
    GRANT SELECT ON TABLES TO engram_app_readonly;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA governance
    GRANT ALL PRIVILEGES ON SEQUENCES TO engram_admin, engram_migrator;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA governance
    GRANT USAGE ON SEQUENCES TO engram_app_readwrite;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA governance
    GRANT EXECUTE ON FUNCTIONS TO engram_admin, engram_migrator, engram_app_readwrite;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA governance
    GRANT EXECUTE ON FUNCTIONS TO engram_app_readonly;

-- ============================================================
-- 6.2 为 engram_admin 设置默认权限（可选，用于管理员操作）
-- ============================================================
-- 当管理员通过 SET ROLE engram_admin 创建对象时，这些权限生效
-- 这是一个安全网，确保管理员操作创建的对象也能正确授权
--
-- 同时为当前执行用户（通常是 superuser/postgres）保留默认权限作为备份
-- 当 superuser 直接创建对象时（如手动修复、紧急操作），这些权限生效

-- identity schema - engram_admin 创建对象的默认权限
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA identity
    GRANT ALL PRIVILEGES ON TABLES TO engram_admin, engram_migrator;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA identity
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO engram_app_readwrite;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA identity
    GRANT SELECT ON TABLES TO engram_app_readonly;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA identity
    GRANT ALL PRIVILEGES ON SEQUENCES TO engram_admin, engram_migrator;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA identity
    GRANT USAGE ON SEQUENCES TO engram_app_readwrite;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA identity
    GRANT EXECUTE ON FUNCTIONS TO engram_admin, engram_migrator, engram_app_readwrite;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA identity
    GRANT EXECUTE ON FUNCTIONS TO engram_app_readonly;

-- logbook schema - engram_admin 创建对象的默认权限
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA logbook
    GRANT ALL PRIVILEGES ON TABLES TO engram_admin, engram_migrator;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA logbook
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO engram_app_readwrite;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA logbook
    GRANT SELECT ON TABLES TO engram_app_readonly;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA logbook
    GRANT ALL PRIVILEGES ON SEQUENCES TO engram_admin, engram_migrator;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA logbook
    GRANT USAGE ON SEQUENCES TO engram_app_readwrite;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA logbook
    GRANT EXECUTE ON FUNCTIONS TO engram_admin, engram_migrator, engram_app_readwrite;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA logbook
    GRANT EXECUTE ON FUNCTIONS TO engram_app_readonly;

-- scm schema - engram_admin 创建对象的默认权限
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA scm
    GRANT ALL PRIVILEGES ON TABLES TO engram_admin, engram_migrator;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA scm
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO engram_app_readwrite;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA scm
    GRANT SELECT ON TABLES TO engram_app_readonly;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA scm
    GRANT ALL PRIVILEGES ON SEQUENCES TO engram_admin, engram_migrator;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA scm
    GRANT USAGE ON SEQUENCES TO engram_app_readwrite;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA scm
    GRANT EXECUTE ON FUNCTIONS TO engram_admin, engram_migrator, engram_app_readwrite;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA scm
    GRANT EXECUTE ON FUNCTIONS TO engram_app_readonly;

-- analysis schema - engram_admin 创建对象的默认权限
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA analysis
    GRANT ALL PRIVILEGES ON TABLES TO engram_admin, engram_migrator;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA analysis
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO engram_app_readwrite;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA analysis
    GRANT SELECT ON TABLES TO engram_app_readonly;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA analysis
    GRANT ALL PRIVILEGES ON SEQUENCES TO engram_admin, engram_migrator;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA analysis
    GRANT USAGE ON SEQUENCES TO engram_app_readwrite;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA analysis
    GRANT EXECUTE ON FUNCTIONS TO engram_admin, engram_migrator, engram_app_readwrite;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA analysis
    GRANT EXECUTE ON FUNCTIONS TO engram_app_readonly;

-- governance schema - engram_admin 创建对象的默认权限
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA governance
    GRANT ALL PRIVILEGES ON TABLES TO engram_admin, engram_migrator;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA governance
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO engram_app_readwrite;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA governance
    GRANT SELECT ON TABLES TO engram_app_readonly;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA governance
    GRANT ALL PRIVILEGES ON SEQUENCES TO engram_admin, engram_migrator;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA governance
    GRANT USAGE ON SEQUENCES TO engram_app_readwrite;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA governance
    GRANT EXECUTE ON FUNCTIONS TO engram_admin, engram_migrator, engram_app_readwrite;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA governance
    GRANT EXECUTE ON FUNCTIONS TO engram_app_readonly;

-- ============================================================
-- 6.3 为当前执行用户（superuser/postgres）保留默认权限作为备份
-- ============================================================
-- 当 superuser 直接创建对象时（如手动修复、紧急操作），这些权限生效
-- 这是最后的安全网

-- identity schema 默认权限（superuser 创建时）
ALTER DEFAULT PRIVILEGES IN SCHEMA identity
    GRANT ALL PRIVILEGES ON TABLES TO engram_admin, engram_migrator;
ALTER DEFAULT PRIVILEGES IN SCHEMA identity
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO engram_app_readwrite;
ALTER DEFAULT PRIVILEGES IN SCHEMA identity
    GRANT SELECT ON TABLES TO engram_app_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA identity
    GRANT ALL PRIVILEGES ON SEQUENCES TO engram_admin, engram_migrator;
ALTER DEFAULT PRIVILEGES IN SCHEMA identity
    GRANT USAGE ON SEQUENCES TO engram_app_readwrite;

-- logbook schema 默认权限（superuser 创建时）
ALTER DEFAULT PRIVILEGES IN SCHEMA logbook
    GRANT ALL PRIVILEGES ON TABLES TO engram_admin, engram_migrator;
ALTER DEFAULT PRIVILEGES IN SCHEMA logbook
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO engram_app_readwrite;
ALTER DEFAULT PRIVILEGES IN SCHEMA logbook
    GRANT SELECT ON TABLES TO engram_app_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA logbook
    GRANT ALL PRIVILEGES ON SEQUENCES TO engram_admin, engram_migrator;
ALTER DEFAULT PRIVILEGES IN SCHEMA logbook
    GRANT USAGE ON SEQUENCES TO engram_app_readwrite;

-- scm schema 默认权限（superuser 创建时）
ALTER DEFAULT PRIVILEGES IN SCHEMA scm
    GRANT ALL PRIVILEGES ON TABLES TO engram_admin, engram_migrator;
ALTER DEFAULT PRIVILEGES IN SCHEMA scm
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO engram_app_readwrite;
ALTER DEFAULT PRIVILEGES IN SCHEMA scm
    GRANT SELECT ON TABLES TO engram_app_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA scm
    GRANT ALL PRIVILEGES ON SEQUENCES TO engram_admin, engram_migrator;
ALTER DEFAULT PRIVILEGES IN SCHEMA scm
    GRANT USAGE ON SEQUENCES TO engram_app_readwrite;

-- analysis schema 默认权限（superuser 创建时）
ALTER DEFAULT PRIVILEGES IN SCHEMA analysis
    GRANT ALL PRIVILEGES ON TABLES TO engram_admin, engram_migrator;
ALTER DEFAULT PRIVILEGES IN SCHEMA analysis
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO engram_app_readwrite;
ALTER DEFAULT PRIVILEGES IN SCHEMA analysis
    GRANT SELECT ON TABLES TO engram_app_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA analysis
    GRANT ALL PRIVILEGES ON SEQUENCES TO engram_admin, engram_migrator;
ALTER DEFAULT PRIVILEGES IN SCHEMA analysis
    GRANT USAGE ON SEQUENCES TO engram_app_readwrite;

-- governance schema 默认权限（superuser 创建时）
ALTER DEFAULT PRIVILEGES IN SCHEMA governance
    GRANT ALL PRIVILEGES ON TABLES TO engram_admin, engram_migrator;
ALTER DEFAULT PRIVILEGES IN SCHEMA governance
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO engram_app_readwrite;
ALTER DEFAULT PRIVILEGES IN SCHEMA governance
    GRANT SELECT ON TABLES TO engram_app_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA governance
    GRANT ALL PRIVILEGES ON SEQUENCES TO engram_admin, engram_migrator;
ALTER DEFAULT PRIVILEGES IN SCHEMA governance
    GRANT USAGE ON SEQUENCES TO engram_app_readwrite;

-- ============================================================
-- 7. search_path 配置说明
-- ============================================================
--
-- 运行期连接必须显式设置 search_path，禁止依赖默认值。
-- 推荐的 search_path 格式：
--
--   单租户模式（无前缀）:
--     SET search_path TO logbook, scm, identity, analysis, governance, public;
--
--   多租户模式（带前缀）:
--     SET search_path TO tenant_abc_logbook, tenant_abc_scm, tenant_abc_identity,
--                       tenant_abc_analysis, tenant_abc_governance, public;
--
-- 注意：
--   - public 必须放在最后作为兜底（用于访问扩展和系统函数）
--   - 应用代码应通过 SchemaContext 或配置获取正确的 search_path
--   - 连接池应在获取连接时设置 search_path
--
-- 示例（Python psycopg）:
--   conn = psycopg.connect(dsn)
--   conn.execute(f"SET search_path TO {schema_context.search_path_sql}")
--

COMMIT;

-- ============================================================
-- 验证脚本（可选，取消注释执行）
-- ============================================================
/*
-- 验证角色是否创建
SELECT rolname, rolcanlogin, rolcreaterole, rolcreatedb 
FROM pg_roles 
WHERE rolname LIKE 'engram_%';

-- 验证 schema 权限
SELECT 
    nspname AS schema_name,
    pg_catalog.has_schema_privilege('engram_admin', nspname, 'CREATE') AS admin_create,
    pg_catalog.has_schema_privilege('engram_app_readwrite', nspname, 'USAGE') AS rw_usage,
    pg_catalog.has_schema_privilege('engram_app_readonly', nspname, 'USAGE') AS ro_usage
FROM pg_namespace
WHERE nspname IN ('identity', 'logbook', 'scm', 'analysis', 'governance', 'public');

-- 验证 public schema 的 CREATE 权限已撤销
SELECT 
    pg_catalog.has_schema_privilege('PUBLIC', 'public', 'CREATE') AS public_can_create,
    pg_catalog.has_schema_privilege('engram_app_readwrite', 'public', 'CREATE') AS rw_can_create;
*/
