-- ============================================================
-- Engram Logbook: 权限验证脚本（仅用于测试验证）
-- ============================================================
--
-- 本脚本用于验证角色权限配置是否正确。
--
-- 参数化设计：
--   通过 PostgreSQL 自定义配置变量 'om.target_schema' 传入目标 schema 名称
--   若未设置，默认为 'openmemory'
--
-- Schema 前缀支持（测试模式）：
--   通过 PostgreSQL 自定义配置变量 'engram.schema_prefix' 传入 schema 前缀
--   若未设置或为空，使用默认 schema 名（identity, logbook, scm, analysis, governance）
--   若设置为 'test'，则验证 test_identity, test_logbook 等带前缀的 schema
--
--   启用方式：
--     方式1 - CLI 参数（需要测试模式）：
--       ENGRAM_TESTING=1 python -m engram.logbook.cli.db_migrate --schema-prefix test --verify
--     方式2 - psql 直接设置：
--       psql -d <your_db> -c "SET engram.schema_prefix = 'test'" -f 99_verify_permissions.sql
--
-- Strict 模式：
--   通过 PostgreSQL 自定义配置变量 'engram.verify_strict' 启用
--   当设置为 '1' 时，如果有任何 FAIL 或 WARN 项，脚本最终会 RAISE EXCEPTION
--   用于 CI/CD 流水线门禁，确保权限配置完全正确
--
--   启用方式：
--     方式1 - CLI 参数：
--       python -m engram.logbook.cli.db_migrate --verify --verify-strict
--     方式2 - 环境变量：
--       ENGRAM_VERIFY_STRICT=1 python -m engram.logbook.cli.db_migrate --verify
--     方式3 - psql 直接设置：
--       psql -d <your_db> -c "SET engram.verify_strict = '1'" -f 99_verify_permissions.sql
--
-- 执行方式：
--   方式1 - psql 直接执行（使用默认 schema）：
--     psql -d <your_db> -f 99_verify_permissions.sql
--
--   方式2 - psql 通过 SET 指定 schema：
--     psql -d <your_db> -c "SET om.target_schema = 'myproject_openmemory'" -f 99_verify_permissions.sql
--
--   方式3 - psql 通过变量注入 schema（wrapper 脚本推荐）：
--     psql -d <your_db> -v target_schema="'myproject_openmemory'" -f 99_verify_permissions.sql
--
--   方式4 - db_migrate.py 自动执行：
--     OM_PG_SCHEMA=myproject_openmemory python db_migrate.py --apply-roles --apply-openmemory-grants
--
-- 核心验证项：
--   1. 角色是否存在（engram_* 和 openmemory_* 角色）
--   2. LOGIN 角色存在且具备正确 membership
--   3. public schema 无 CREATE 权限（所有应用角色）
--   4. 目标 OM schema 存在且 owner 正确
--   5. openmemory_migrator 在目标 schema 有 CREATE 权限
--   6. openmemory_app 在目标 schema 有 USAGE 且无 CREATE 权限
--   7. pg_default_acl 默认权限配置正确
--   8. 数据库级权限（CONNECT/CREATE/TEMP）符合预期
--   9. Engram schema 权限（如已创建）
--
-- 输出级别：
--   FAIL - 严重问题，必须修复才能正常工作
--   WARN - 潜在问题，可能影响安全或功能
--   OK   - 检查通过
--   SKIP - 条件不满足，跳过检查
-- ============================================================

-- psql 变量注入支持：如果通过 -v target_schema="'xxx'" 传入，则设置到 om.target_schema
\if :{?target_schema}
  SET om.target_schema = :target_schema;
\endif

-- 创建临时表存储各段的 fail_count（用于最终汇总）
CREATE TEMP TABLE IF NOT EXISTS _verify_fail_counts (
    section_id INT PRIMARY KEY,
    section_name TEXT NOT NULL,
    fail_count INT NOT NULL DEFAULT 0,
    warn_count INT NOT NULL DEFAULT 0
);
-- SAFE: 清空会话内临时表，支持重复执行验证
TRUNCATE _verify_fail_counts;

-- 辅助函数：获取带前缀的 engram schema 名称数组
-- 根据 engram.schema_prefix 配置变量动态构造 schema 名称
CREATE OR REPLACE FUNCTION _get_engram_schemas()
RETURNS TEXT[] AS $$
DECLARE
    v_prefix TEXT;
    base_schemas TEXT[] := ARRAY['identity', 'logbook', 'scm', 'analysis', 'governance'];
    result TEXT[];
    s TEXT;
BEGIN
    -- 尝试读取 schema_prefix 配置变量
    v_prefix := NULLIF(current_setting('engram.schema_prefix', true), '');
    
    IF v_prefix IS NULL OR v_prefix = '' THEN
        -- 无前缀，返回默认 schema 名
        RETURN base_schemas;
    ELSE
        -- 有前缀，构造带前缀的 schema 名
        result := ARRAY[]::TEXT[];
        FOREACH s IN ARRAY base_schemas LOOP
            result := array_append(result, v_prefix || '_' || s);
        END LOOP;
        RETURN result;
    END IF;
END;
$$ LANGUAGE plpgsql;

-- 辅助函数：获取 schema_prefix（用于日志输出）
CREATE OR REPLACE FUNCTION _get_schema_prefix()
RETURNS TEXT AS $$
BEGIN
    RETURN NULLIF(current_setting('engram.schema_prefix', true), '');
END;
$$ LANGUAGE plpgsql;

-- 1. 验证角色是否存在
DO $$
DECLARE
    role_count INT;
    -- 核心角色（必须存在）
    core_roles TEXT[] := ARRAY[
        'engram_admin',
        'engram_migrator', 
        'engram_app_readwrite',
        'engram_app_readonly',
        'openmemory_migrator',
        'openmemory_app'
    ];
    role_name TEXT;
    v_fail_count INT := 0;
BEGIN
    RAISE NOTICE '=== 1. NOLOGIN 角色验证 ===';
    
    -- 验证核心角色
    FOREACH role_name IN ARRAY core_roles LOOP
        SELECT COUNT(*) INTO role_count FROM pg_roles WHERE rolname = role_name;
        IF role_count = 0 THEN
            RAISE WARNING 'FAIL: 角色不存在: %', role_name;
            RAISE NOTICE '  remedy: 执行 04_roles_and_grants.sql 创建角色';
            v_fail_count := v_fail_count + 1;
        ELSE
            RAISE NOTICE 'OK: 角色 % 存在', role_name;
        END IF;
    END LOOP;
    
    IF v_fail_count > 0 THEN
        RAISE NOTICE 'NOLOGIN 角色验证: % 项 FAIL', v_fail_count;
    ELSE
        RAISE NOTICE 'NOLOGIN 角色验证: 全部通过';
    END IF;
    
    -- 记录到汇总表
    INSERT INTO _verify_fail_counts (section_id, section_name, fail_count, warn_count)
    VALUES (1, 'NOLOGIN 角色验证', v_fail_count, 0);
END $$;

-- 2. 验证 LOGIN 角色存在性及 membership
DO $$
DECLARE
    v_fail_count INT := 0;
    v_warn_count INT := 0;
    v_login_exists BOOLEAN;
    v_target_exists BOOLEAN;
    v_has_membership BOOLEAN;
    
    -- LOGIN 角色 -> NOLOGIN 角色的映射
    -- 格式: login_role, target_role, description
    login_mappings TEXT[][] := ARRAY[
        ARRAY['logbook_migrator', 'engram_migrator', 'Logbook 迁移账号'],
        ARRAY['logbook_svc', 'engram_app_readwrite', 'Logbook 运行账号'],
        ARRAY['openmemory_migrator_login', 'openmemory_migrator', 'OpenMemory 迁移账号'],
        ARRAY['openmemory_svc', 'openmemory_app', 'OpenMemory 运行账号']
    ];
    v_mapping TEXT[];
    v_login_role TEXT;
    v_target_role TEXT;
    v_desc TEXT;
BEGIN
    RAISE NOTICE '';
    RAISE NOTICE '=== 2. LOGIN 角色及 membership 验证 ===';
    
    FOREACH v_mapping SLICE 1 IN ARRAY login_mappings LOOP
        v_login_role := v_mapping[1];
        v_target_role := v_mapping[2];
        v_desc := v_mapping[3];
        
        -- 检查 LOGIN 角色是否存在
        SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = v_login_role AND rolcanlogin = true)
        INTO v_login_exists;
        
        -- 检查 target 角色是否存在
        SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = v_target_role)
        INTO v_target_exists;
        
        IF NOT v_login_exists THEN
            -- LOGIN 角色不存在，可能尚未创建
            RAISE WARNING 'WARN: LOGIN 角色 % 不存在（%）', v_login_role, v_desc;
            RAISE NOTICE '  remedy: 执行 python logbook_postgres/scripts/db_bootstrap.py 创建 LOGIN 角色';
            v_warn_count := v_warn_count + 1;
            CONTINUE;
        END IF;
        
        IF NOT v_target_exists THEN
            -- target 角色不存在是严重问题
            RAISE WARNING 'FAIL: 目标角色 % 不存在', v_target_role;
            RAISE NOTICE '  remedy: 执行 04_roles_and_grants.sql 创建角色';
            v_fail_count := v_fail_count + 1;
            CONTINUE;
        END IF;
        
        -- 检查 membership：v_login_role 是否是 v_target_role 的成员
        SELECT EXISTS(
            SELECT 1 FROM pg_auth_members am
            JOIN pg_roles r_member ON r_member.oid = am.member
            JOIN pg_roles r_role ON r_role.oid = am.roleid
            WHERE r_member.rolname = v_login_role
              AND r_role.rolname = v_target_role
        ) INTO v_has_membership;
        
        IF v_has_membership THEN
            RAISE NOTICE 'OK: % -> % membership 正确（%）', v_login_role, v_target_role, v_desc;
        ELSE
            RAISE WARNING 'FAIL: % 未继承 %（%）', v_login_role, v_target_role, v_desc;
            RAISE NOTICE '  remedy: GRANT % TO %;', v_target_role, v_login_role;
            v_fail_count := v_fail_count + 1;
        END IF;
    END LOOP;
    
    -- 汇总
    IF v_fail_count > 0 THEN
        RAISE NOTICE 'LOGIN 角色验证: % 项 FAIL, % 项 WARN', v_fail_count, v_warn_count;
    ELSIF v_warn_count > 0 THEN
        RAISE NOTICE 'LOGIN 角色验证: % 项 WARN', v_warn_count;
    ELSE
        RAISE NOTICE 'LOGIN 角色验证: 全部通过';
    END IF;
    
    -- 记录到汇总表
    INSERT INTO _verify_fail_counts (section_id, section_name, fail_count, warn_count)
    VALUES (2, 'LOGIN 角色验证', v_fail_count, v_warn_count);
END $$;

-- 3. 验证 public schema 的 CREATE 权限
DO $$
DECLARE
    can_create BOOLEAN;
    v_fail_count INT := 0;
BEGIN
    RAISE NOTICE '';
    RAISE NOTICE '=== 3. public schema CREATE 权限验证 ===';
    
    -- PUBLIC 不应有 CREATE 权限
    BEGIN
        SELECT pg_catalog.has_schema_privilege('PUBLIC', 'public', 'CREATE') INTO can_create;
    EXCEPTION WHEN undefined_object THEN
        -- 某些环境下 PUBLIC 不是实际角色，视为无权限
        can_create := false;
    END;
    IF can_create THEN
        RAISE WARNING 'FAIL: PUBLIC 仍有 public schema 的 CREATE 权限';
        RAISE NOTICE '  remedy: REVOKE CREATE ON SCHEMA public FROM PUBLIC;';
        v_fail_count := v_fail_count + 1;
    ELSE
        RAISE NOTICE 'OK: PUBLIC 无 public schema CREATE 权限';
    END IF;
    
    -- engram_admin 不应有 CREATE 权限
    SELECT pg_catalog.has_schema_privilege('engram_admin', 'public', 'CREATE') INTO can_create;
    IF can_create THEN
        RAISE WARNING 'FAIL: engram_admin 有 public schema 的 CREATE 权限';
        RAISE NOTICE '  remedy: REVOKE CREATE ON SCHEMA public FROM engram_admin;';
        v_fail_count := v_fail_count + 1;
    ELSE
        RAISE NOTICE 'OK: engram_admin 无 public schema CREATE 权限';
    END IF;
    
    -- engram_app_readwrite 不应有 CREATE 权限
    SELECT pg_catalog.has_schema_privilege('engram_app_readwrite', 'public', 'CREATE') INTO can_create;
    IF can_create THEN
        RAISE WARNING 'FAIL: engram_app_readwrite 有 public schema 的 CREATE 权限';
        RAISE NOTICE '  remedy: REVOKE CREATE ON SCHEMA public FROM engram_app_readwrite;';
        v_fail_count := v_fail_count + 1;
    ELSE
        RAISE NOTICE 'OK: engram_app_readwrite 无 public schema CREATE 权限';
    END IF;
    
    -- openmemory_migrator 不应有 public CREATE 权限（使用独立 schema）
    SELECT pg_catalog.has_schema_privilege('openmemory_migrator', 'public', 'CREATE') INTO can_create;
    IF can_create THEN
        RAISE WARNING 'WARN: openmemory_migrator 有 public schema 的 CREATE 权限（应使用独立 openmemory schema）';
        RAISE NOTICE '  remedy: REVOKE CREATE ON SCHEMA public FROM openmemory_migrator;';
    ELSE
        RAISE NOTICE 'OK: openmemory_migrator 无 public schema CREATE 权限';
    END IF;
    
    -- 汇总
    IF v_fail_count > 0 THEN
        RAISE NOTICE 'public schema 验证: % 项 FAIL', v_fail_count;
    ELSE
        RAISE NOTICE 'public schema 验证: 全部通过';
    END IF;
    
    -- 记录到汇总表
    INSERT INTO _verify_fail_counts (section_id, section_name, fail_count, warn_count)
    VALUES (3, 'public schema 验证', v_fail_count, 0);
END $$;

-- 4. 验证目标 openmemory schema 存在性、owner 和权限
DO $$
DECLARE
    v_schema TEXT;
    v_schema_exists BOOLEAN;
    v_schema_owner TEXT;
    can_create BOOLEAN;
    can_usage BOOLEAN;
    v_fail_count INT := 0;
BEGIN
    RAISE NOTICE '';
    RAISE NOTICE '=== 4. 目标 OM schema 验证 ===';
    
    -- 尝试读取配置变量，若未设置则使用默认值 'openmemory'
    v_schema := COALESCE(
        NULLIF(current_setting('om.target_schema', true), ''),
        'openmemory'
    );
    
    RAISE NOTICE '目标 schema: %', v_schema;
    
    -- 4.1 检查 schema 是否存在（核心验证项）
    SELECT EXISTS(SELECT 1 FROM information_schema.schemata WHERE schema_name = v_schema) INTO v_schema_exists;
    
    IF NOT v_schema_exists THEN
        RAISE WARNING 'FAIL: % schema 不存在', v_schema;
        RAISE NOTICE '  remedy: 执行 05_openmemory_roles_and_grants.sql 或 --apply-openmemory-grants';
        RAISE NOTICE '后续权限验证跳过，请先创建目标 schema';
        RETURN;
    END IF;
    
    RAISE NOTICE 'OK: % schema 存在', v_schema;
    
    -- 4.2 验证 schema owner = openmemory_migrator（关键安全要求）
    SELECT nspowner::regrole::text INTO v_schema_owner
    FROM pg_namespace
    WHERE nspname = v_schema;
    
    IF v_schema_owner = 'openmemory_migrator' THEN
        RAISE NOTICE 'OK: % schema owner = openmemory_migrator', v_schema;
    ELSE
        RAISE WARNING 'FAIL: % schema owner = % (预期: openmemory_migrator)', v_schema, v_schema_owner;
        RAISE NOTICE '  remedy: ALTER SCHEMA % OWNER TO openmemory_migrator;', v_schema;
        v_fail_count := v_fail_count + 1;
    END IF;
    
    -- 4.3 openmemory_migrator 应有 CREATE 权限（核心验证项）
    EXECUTE format('SELECT pg_catalog.has_schema_privilege(%L, %L, %L)', 'openmemory_migrator', v_schema, 'CREATE') INTO can_create;
    IF can_create THEN
        RAISE NOTICE 'OK: openmemory_migrator 有 % schema CREATE 权限', v_schema;
    ELSE
        RAISE WARNING 'FAIL: openmemory_migrator 无 % schema CREATE 权限', v_schema;
        RAISE NOTICE '  remedy: GRANT ALL PRIVILEGES ON SCHEMA % TO openmemory_migrator;', v_schema;
        v_fail_count := v_fail_count + 1;
    END IF;
    
    -- 4.4 openmemory_app 应有 USAGE 权限（核心验证项）
    EXECUTE format('SELECT pg_catalog.has_schema_privilege(%L, %L, %L)', 'openmemory_app', v_schema, 'USAGE') INTO can_usage;
    IF can_usage THEN
        RAISE NOTICE 'OK: openmemory_app 有 % schema USAGE 权限', v_schema;
    ELSE
        RAISE WARNING 'FAIL: openmemory_app 无 % schema USAGE 权限', v_schema;
        RAISE NOTICE '  remedy: GRANT USAGE ON SCHEMA % TO openmemory_app;', v_schema;
        v_fail_count := v_fail_count + 1;
    END IF;
    
    -- 4.5 openmemory_app 不应有 CREATE 权限（核心验证项）
    EXECUTE format('SELECT pg_catalog.has_schema_privilege(%L, %L, %L)', 'openmemory_app', v_schema, 'CREATE') INTO can_create;
    IF can_create THEN
        RAISE WARNING 'FAIL: openmemory_app 有 % schema CREATE 权限（应仅限 DML）', v_schema;
        RAISE NOTICE '  remedy: REVOKE CREATE ON SCHEMA % FROM openmemory_app;', v_schema;
        v_fail_count := v_fail_count + 1;
    ELSE
        RAISE NOTICE 'OK: openmemory_app 无 % schema CREATE 权限', v_schema;
    END IF;
    
    -- 汇总
    IF v_fail_count > 0 THEN
        RAISE NOTICE '目标 OM schema 验证: % 项 FAIL', v_fail_count;
    ELSE
        RAISE NOTICE '目标 OM schema 验证: 全部通过';
    END IF;
    
    -- 记录到汇总表
    INSERT INTO _verify_fail_counts (section_id, section_name, fail_count, warn_count)
    VALUES (4, '目标 OM schema 验证', v_fail_count, 0);
END $$;

-- 5. 验证 pg_default_acl 默认权限配置
-- 检查 openmemory_migrator 对 openmemory_app 的 TABLE/SEQUENCE 默认授权
-- 检查 engram_migrator 对 engram_app_* 的 TABLE/SEQUENCE 默认授权
DO $$
DECLARE
    v_schema TEXT;
    v_fail_count INT := 0;
    v_warn_count INT := 0;
    v_count INT;
    v_grantor_oid OID;
    v_grantee_oid OID;
    v_schema_oid OID;
    engram_schemas TEXT[] := _get_engram_schemas();  -- 使用辅助函数获取（支持 schema_prefix）
    v_engram_schema TEXT;
    v_schema_prefix TEXT := _get_schema_prefix();
BEGIN
    RAISE NOTICE '';
    RAISE NOTICE '=== 5. pg_default_acl 默认权限验证 ===';
    
    -- 输出 schema_prefix 信息（如有）
    IF v_schema_prefix IS NOT NULL THEN
        RAISE NOTICE '使用 schema_prefix: %，验证 schema 列表: %', v_schema_prefix, engram_schemas;
    END IF;
    
    -- 获取目标 schema
    v_schema := COALESCE(
        NULLIF(current_setting('om.target_schema', true), ''),
        'openmemory'
    );
    
    -- 获取 schema OID
    SELECT oid INTO v_schema_oid FROM pg_namespace WHERE nspname = v_schema;
    
    -- 5.1 验证 openmemory_migrator -> openmemory_app 默认权限
    RAISE NOTICE '';
    RAISE NOTICE '--- 5.1 OpenMemory 默认权限 ---';
    
    IF v_schema_oid IS NULL THEN
        RAISE NOTICE 'SKIP: % schema 不存在，跳过默认权限验证', v_schema;
    ELSE
        -- 获取角色 OID
        SELECT oid INTO v_grantor_oid FROM pg_roles WHERE rolname = 'openmemory_migrator';
        SELECT oid INTO v_grantee_oid FROM pg_roles WHERE rolname = 'openmemory_app';
        
        IF v_grantor_oid IS NULL THEN
            RAISE WARNING 'FAIL: openmemory_migrator 角色不存在';
            RAISE NOTICE '  remedy: 执行 05_openmemory_roles_and_grants.sql';
            v_fail_count := v_fail_count + 1;
        ELSIF v_grantee_oid IS NULL THEN
            RAISE WARNING 'FAIL: openmemory_app 角色不存在';
            RAISE NOTICE '  remedy: 执行 05_openmemory_roles_and_grants.sql';
            v_fail_count := v_fail_count + 1;
        ELSE
            -- 检查 TABLE 默认权限 (defaclobjtype = 'r')
            SELECT COUNT(*) INTO v_count
            FROM pg_default_acl da
            WHERE da.defaclnamespace = v_schema_oid
              AND da.defaclrole = v_grantor_oid
              AND da.defaclobjtype = 'r'  -- TABLE
              AND da.defaclacl::text LIKE '%' || v_grantee_oid::text || '%';
            
            IF v_count > 0 THEN
                RAISE NOTICE 'OK: openmemory_migrator 在 % 对 openmemory_app 有 TABLE 默认授权', v_schema;
            ELSE
                RAISE WARNING 'FAIL: openmemory_migrator 在 % 对 openmemory_app 无 TABLE 默认授权', v_schema;
                RAISE NOTICE '  remedy: ALTER DEFAULT PRIVILEGES FOR ROLE openmemory_migrator IN SCHEMA % GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO openmemory_app;', v_schema;
                v_fail_count := v_fail_count + 1;
            END IF;
            
            -- 检查 SEQUENCE 默认权限 (defaclobjtype = 'S')
            SELECT COUNT(*) INTO v_count
            FROM pg_default_acl da
            WHERE da.defaclnamespace = v_schema_oid
              AND da.defaclrole = v_grantor_oid
              AND da.defaclobjtype = 'S'  -- SEQUENCE
              AND da.defaclacl::text LIKE '%' || v_grantee_oid::text || '%';
            
            IF v_count > 0 THEN
                RAISE NOTICE 'OK: openmemory_migrator 在 % 对 openmemory_app 有 SEQUENCE 默认授权', v_schema;
            ELSE
                RAISE WARNING 'FAIL: openmemory_migrator 在 % 对 openmemory_app 无 SEQUENCE 默认授权', v_schema;
                RAISE NOTICE '  remedy: ALTER DEFAULT PRIVILEGES FOR ROLE openmemory_migrator IN SCHEMA % GRANT USAGE ON SEQUENCES TO openmemory_app;', v_schema;
                v_fail_count := v_fail_count + 1;
            END IF;
        END IF;
    END IF;
    
    -- 5.2 验证 engram_migrator -> engram_app_readwrite/readonly 默认权限
    RAISE NOTICE '';
    RAISE NOTICE '--- 5.3 Engram 默认权限 ---';
    
    -- 获取 engram_migrator OID
    SELECT oid INTO v_grantor_oid FROM pg_roles WHERE rolname = 'engram_migrator';
    
    IF v_grantor_oid IS NULL THEN
        RAISE WARNING 'WARN: engram_migrator 角色不存在，跳过验证';
        v_warn_count := v_warn_count + 1;
    ELSE
        FOREACH v_engram_schema IN ARRAY engram_schemas LOOP
            -- 检查 schema 是否存在
            SELECT oid INTO v_schema_oid FROM pg_namespace WHERE nspname = v_engram_schema;
            
            IF v_schema_oid IS NULL THEN
                RAISE NOTICE 'SKIP: schema % 不存在', v_engram_schema;
                CONTINUE;
            END IF;
            
            -- 检查对 engram_app_readwrite 的 TABLE 默认权限
            SELECT oid INTO v_grantee_oid FROM pg_roles WHERE rolname = 'engram_app_readwrite';
            
            IF v_grantee_oid IS NOT NULL THEN
                SELECT COUNT(*) INTO v_count
                FROM pg_default_acl da
                WHERE da.defaclnamespace = v_schema_oid
                  AND da.defaclrole = v_grantor_oid
                  AND da.defaclobjtype = 'r'  -- TABLE
                  AND da.defaclacl::text LIKE '%' || v_grantee_oid::text || '%';
                
                IF v_count > 0 THEN
                    RAISE NOTICE 'OK: engram_migrator 在 % 对 engram_app_readwrite 有 TABLE 默认授权', v_engram_schema;
                ELSE
                    RAISE WARNING 'FAIL: engram_migrator 在 % 对 engram_app_readwrite 无 TABLE 默认授权', v_engram_schema;
                    RAISE NOTICE '  remedy: ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA % GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO engram_app_readwrite;', v_engram_schema;
                    v_fail_count := v_fail_count + 1;
                END IF;
            END IF;
            
            -- 检查对 engram_app_readonly 的 TABLE 默认权限
            SELECT oid INTO v_grantee_oid FROM pg_roles WHERE rolname = 'engram_app_readonly';
            
            IF v_grantee_oid IS NOT NULL THEN
                SELECT COUNT(*) INTO v_count
                FROM pg_default_acl da
                WHERE da.defaclnamespace = v_schema_oid
                  AND da.defaclrole = v_grantor_oid
                  AND da.defaclobjtype = 'r'  -- TABLE
                  AND da.defaclacl::text LIKE '%' || v_grantee_oid::text || '%';
                
                IF v_count > 0 THEN
                    RAISE NOTICE 'OK: engram_migrator 在 % 对 engram_app_readonly 有 TABLE 默认授权', v_engram_schema;
                ELSE
                    RAISE WARNING 'FAIL: engram_migrator 在 % 对 engram_app_readonly 无 TABLE 默认授权', v_engram_schema;
                    RAISE NOTICE '  remedy: ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA % GRANT SELECT ON TABLES TO engram_app_readonly;', v_engram_schema;
                    v_fail_count := v_fail_count + 1;
                END IF;
            END IF;
            
            -- 检查对 engram_app_readwrite 的 SEQUENCE 默认权限
            SELECT oid INTO v_grantee_oid FROM pg_roles WHERE rolname = 'engram_app_readwrite';
            
            IF v_grantee_oid IS NOT NULL THEN
                SELECT COUNT(*) INTO v_count
                FROM pg_default_acl da
                WHERE da.defaclnamespace = v_schema_oid
                  AND da.defaclrole = v_grantor_oid
                  AND da.defaclobjtype = 'S'  -- SEQUENCE
                  AND da.defaclacl::text LIKE '%' || v_grantee_oid::text || '%';
                
                IF v_count > 0 THEN
                    RAISE NOTICE 'OK: engram_migrator 在 % 对 engram_app_readwrite 有 SEQUENCE 默认授权', v_engram_schema;
                ELSE
                    RAISE WARNING 'FAIL: engram_migrator 在 % 对 engram_app_readwrite 无 SEQUENCE 默认授权', v_engram_schema;
                    RAISE NOTICE '  remedy: ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA % GRANT USAGE ON SEQUENCES TO engram_app_readwrite;', v_engram_schema;
                    v_fail_count := v_fail_count + 1;
                END IF;
            END IF;
        END LOOP;
    END IF;
    
    -- 汇总
    IF v_fail_count > 0 THEN
        RAISE NOTICE 'pg_default_acl 验证: % 项 FAIL, % 项 WARN', v_fail_count, v_warn_count;
    ELSIF v_warn_count > 0 THEN
        RAISE NOTICE 'pg_default_acl 验证: % 项 WARN', v_warn_count;
    ELSE
        RAISE NOTICE 'pg_default_acl 验证: 全部通过';
    END IF;
    
    -- 记录到汇总表
    INSERT INTO _verify_fail_counts (section_id, section_name, fail_count, warn_count)
    VALUES (5, 'pg_default_acl 验证', v_fail_count, v_warn_count);
END $$;

-- 6. 验证数据库级权限硬化（04_roles_and_grants.sql section 1.7）
-- 首先验证 PUBLIC 的权限被正确撤销
DO $$
DECLARE
    v_db TEXT;
    v_fail_count INT := 0;
    v_warn_count INT := 0;
    v_has_connect BOOLEAN;
    v_has_create BOOLEAN;
    v_has_temp BOOLEAN;
    
    -- 预期的数据库权限配置
    -- 格式: role_name, should_have_connect, should_have_create, should_have_temp
    -- 策略说明：
    --   - PUBLIC: CONNECT=N (需显式授权), CREATE=N, TEMP=N
    --   - migrator 角色: CONNECT=Y, CREATE=Y (需执行 DDL), TEMP=Y
    --   - app 角色: CONNECT=Y, CREATE=N, TEMP=N/Y (取决于是否需要临时表)
    --   - admin 角色: CONNECT=Y, CREATE=Y, TEMP=Y
    db_priv_checks TEXT[][] := ARRAY[
        -- Logbook/Engram 角色
        ARRAY['engram_admin', 'Y', 'Y', 'Y'],
        ARRAY['engram_migrator', 'Y', 'Y', 'Y'],          -- 迁移角色需要 CREATE/TEMP
        ARRAY['engram_app_readwrite', 'Y', 'N', 'N'],     -- 应用角色无需 CREATE/TEMP
        ARRAY['engram_app_readonly', 'Y', 'N', 'N'],
        -- OpenMemory 角色
        ARRAY['openmemory_migrator', 'Y', 'Y', 'Y'],      -- 迁移角色需要 CREATE/TEMP
        ARRAY['openmemory_app', 'Y', 'N', 'N'],           -- 应用角色无需 CREATE/TEMP
        -- LOGIN 角色（如存在）
        ARRAY['logbook_migrator', 'Y', 'Y', 'Y'],           -- 继承 engram_migrator
        ARRAY['logbook_svc', 'Y', 'N', 'N'],                -- 继承 engram_app_readwrite
        ARRAY['openmemory_migrator_login', 'Y', 'Y', 'Y'],-- 继承 openmemory_migrator
        ARRAY['openmemory_svc', 'Y', 'N', 'N']            -- 继承 openmemory_app
    ];
    v_check TEXT[];
    v_role TEXT;
    v_expect_connect TEXT;
    v_expect_create TEXT;
    v_expect_temp TEXT;
    v_role_exists BOOLEAN;
BEGIN
    RAISE NOTICE '';
    RAISE NOTICE '=== 6. 数据库级权限硬化验证（04_roles_and_grants.sql） ===';
    
    v_db := current_database();
    RAISE NOTICE '当前数据库: %', v_db;
    
    -- ========================================
    -- 6.1 验证 PUBLIC 的权限被撤销（核心硬化）
    -- ========================================
    RAISE NOTICE '';
    RAISE NOTICE '--- 6.1 PUBLIC 权限硬化验证 ---';
    
    -- PUBLIC 不应有 CREATE 权限
    BEGIN
        SELECT has_database_privilege('PUBLIC', v_db, 'CREATE') INTO v_has_create;
    EXCEPTION WHEN undefined_object THEN
        v_has_create := false;
    END;
    IF v_has_create THEN
        RAISE WARNING 'FAIL: PUBLIC 有数据库 CREATE 权限（硬化未生效）';
        RAISE NOTICE '  remedy: 执行 04_roles_and_grants.sql 或手动执行:';
        RAISE NOTICE '          REVOKE CREATE ON DATABASE % FROM PUBLIC;', v_db;
        v_fail_count := v_fail_count + 1;
    ELSE
        RAISE NOTICE 'OK: PUBLIC 无数据库 CREATE 权限';
    END IF;
    
    -- PUBLIC 不应有 TEMP 权限
    BEGIN
        SELECT has_database_privilege('PUBLIC', v_db, 'TEMP') INTO v_has_temp;
    EXCEPTION WHEN undefined_object THEN
        v_has_temp := false;
    END;
    IF v_has_temp THEN
        RAISE WARNING 'FAIL: PUBLIC 有数据库 TEMP 权限（硬化未生效）';
        RAISE NOTICE '  remedy: 执行 04_roles_and_grants.sql 或手动执行:';
        RAISE NOTICE '          REVOKE TEMP ON DATABASE % FROM PUBLIC;', v_db;
        v_fail_count := v_fail_count + 1;
    ELSE
        RAISE NOTICE 'OK: PUBLIC 无数据库 TEMP 权限';
    END IF;
    
    -- ========================================
    -- 6.2 验证各角色的数据库权限
    -- ========================================
    RAISE NOTICE '';
    RAISE NOTICE '--- 6.2 角色数据库权限验证 ---';
    
    FOREACH v_check SLICE 1 IN ARRAY db_priv_checks LOOP
        v_role := v_check[1];
        v_expect_connect := v_check[2];
        v_expect_create := v_check[3];
        v_expect_temp := v_check[4];
        
        -- 检查角色是否存在
        SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = v_role) INTO v_role_exists;
        
        IF NOT v_role_exists THEN
            -- LOGIN 角色可能不存在，作为 WARN 而非 FAIL
            IF v_role IN ('logbook_migrator', 'logbook_svc', 'openmemory_migrator_login', 'openmemory_svc') THEN
                -- 跳过不存在的 LOGIN 角色（静默）
                CONTINUE;
            ELSE
                RAISE NOTICE 'SKIP: 角色 % 不存在', v_role;
                CONTINUE;
            END IF;
        END IF;
        
        -- 检查 CONNECT 权限
        SELECT has_database_privilege(v_role, v_db, 'CONNECT') INTO v_has_connect;
        -- 检查 CREATE 权限
        SELECT has_database_privilege(v_role, v_db, 'CREATE') INTO v_has_create;
        -- 检查 TEMP 权限
        SELECT has_database_privilege(v_role, v_db, 'TEMP') INTO v_has_temp;
        
        -- 验证 CONNECT
        IF v_expect_connect = 'Y' AND NOT v_has_connect THEN
            RAISE WARNING 'FAIL: % 无法 CONNECT 到数据库 %', v_role, v_db;
            RAISE NOTICE '  remedy: GRANT CONNECT ON DATABASE % TO %;', v_db, v_role;
            v_fail_count := v_fail_count + 1;
        ELSIF v_expect_connect = 'N' AND v_has_connect THEN
            RAISE WARNING 'WARN: % 可以 CONNECT 到数据库 %（预期不应有）', v_role, v_db;
            RAISE NOTICE '  remedy: REVOKE CONNECT ON DATABASE % FROM %;', v_db, v_role;
            v_warn_count := v_warn_count + 1;
        END IF;
        
        -- 验证 CREATE（迁移角色需要，应用角色不需要）
        IF v_expect_create = 'Y' AND NOT v_has_create THEN
            RAISE WARNING 'FAIL: % 无数据库 CREATE 权限（迁移需要）', v_role;
            RAISE NOTICE '  remedy: GRANT CREATE ON DATABASE % TO %;', v_db, v_role;
            v_fail_count := v_fail_count + 1;
        ELSIF v_expect_create = 'N' AND v_has_create THEN
            RAISE WARNING 'WARN: % 有数据库级 CREATE 权限（应仅限迁移角色）', v_role;
            RAISE NOTICE '  remedy: REVOKE CREATE ON DATABASE % FROM %;', v_db, v_role;
            v_warn_count := v_warn_count + 1;
        END IF;
        
        -- 验证 TEMP（迁移角色需要，应用角色可选）
        IF v_expect_temp = 'Y' AND NOT v_has_temp THEN
            RAISE WARNING 'FAIL: % 无数据库 TEMP 权限（迁移需要）', v_role;
            RAISE NOTICE '  remedy: GRANT TEMP ON DATABASE % TO %;', v_db, v_role;
            v_fail_count := v_fail_count + 1;
        END IF;
        
        -- 输出详细状态（仅对重要角色）
        IF v_role IN ('engram_migrator', 'engram_app_readwrite', 'openmemory_migrator', 'openmemory_app') THEN
            RAISE NOTICE '  %: CONNECT=%, CREATE=%, TEMP=%',
                v_role,
                CASE WHEN v_has_connect THEN 'Y' ELSE 'N' END,
                CASE WHEN v_has_create THEN 'Y' ELSE 'N' END,
                CASE WHEN v_has_temp THEN 'Y' ELSE 'N' END;
        END IF;
    END LOOP;
    
    -- 汇总
    IF v_fail_count > 0 THEN
        RAISE NOTICE '数据库权限硬化验证: % 项 FAIL, % 项 WARN', v_fail_count, v_warn_count;
    ELSIF v_warn_count > 0 THEN
        RAISE NOTICE '数据库权限硬化验证: % 项 WARN', v_warn_count;
    ELSE
        RAISE NOTICE '数据库权限硬化验证: 全部通过';
    END IF;
    
    -- 记录到汇总表
    INSERT INTO _verify_fail_counts (section_id, section_name, fail_count, warn_count)
    VALUES (6, '数据库权限硬化验证', v_fail_count, v_warn_count);
END $$;

-- 7. 验证 Engram schema 权限
DO $$
DECLARE
    v_schema_name TEXT;
    can_create BOOLEAN;
    can_usage BOOLEAN;
    engram_schemas TEXT[] := _get_engram_schemas();  -- 使用辅助函数获取（支持 schema_prefix）
    v_fail_count INT := 0;
    v_schema_prefix TEXT := _get_schema_prefix();
BEGIN
    RAISE NOTICE '';
    RAISE NOTICE '=== 7. Engram schema 权限验证 ===';
    
    -- 输出 schema_prefix 信息（如有）
    IF v_schema_prefix IS NOT NULL THEN
        RAISE NOTICE '使用 schema_prefix: %，验证 schema 列表: %', v_schema_prefix, engram_schemas;
    END IF;
    
    FOREACH v_schema_name IN ARRAY engram_schemas LOOP
        -- 检查 schema 是否存在
        IF NOT EXISTS(
            SELECT 1 FROM information_schema.schemata s WHERE s.schema_name = v_schema_name
        ) THEN
            RAISE NOTICE 'SKIP: schema % 不存在', v_schema_name;
            CONTINUE;
        END IF;
        
        -- engram_migrator 应有 CREATE 权限
        SELECT pg_catalog.has_schema_privilege('engram_migrator', v_schema_name, 'CREATE') INTO can_create;
        IF can_create THEN
            RAISE NOTICE 'OK: engram_migrator 有 % schema CREATE 权限', v_schema_name;
        ELSE
            RAISE WARNING 'FAIL: engram_migrator 无 % schema CREATE 权限', v_schema_name;
            RAISE NOTICE '  remedy: GRANT ALL PRIVILEGES ON SCHEMA % TO engram_migrator;', v_schema_name;
            v_fail_count := v_fail_count + 1;
        END IF;
        
        -- engram_app_readwrite 应有 USAGE 权限
        SELECT pg_catalog.has_schema_privilege('engram_app_readwrite', v_schema_name, 'USAGE') INTO can_usage;
        IF can_usage THEN
            RAISE NOTICE 'OK: engram_app_readwrite 有 % schema USAGE 权限', v_schema_name;
        ELSE
            RAISE WARNING 'FAIL: engram_app_readwrite 无 % schema USAGE 权限', v_schema_name;
            RAISE NOTICE '  remedy: GRANT USAGE ON SCHEMA % TO engram_app_readwrite;', v_schema_name;
            v_fail_count := v_fail_count + 1;
        END IF;
        
        -- engram_app_readwrite 不应有 CREATE 权限
        SELECT pg_catalog.has_schema_privilege('engram_app_readwrite', v_schema_name, 'CREATE') INTO can_create;
        IF can_create THEN
            RAISE WARNING 'WARN: engram_app_readwrite 有 % schema CREATE 权限（应仅限 DML）', v_schema_name;
            RAISE NOTICE '  remedy: REVOKE CREATE ON SCHEMA % FROM engram_app_readwrite;', v_schema_name;
        END IF;
    END LOOP;
    
    IF v_fail_count > 0 THEN
        RAISE NOTICE 'Engram schema 验证: % 项 FAIL', v_fail_count;
    ELSE
        RAISE NOTICE 'Engram schema 验证: 全部通过';
    END IF;
    
    -- 记录到汇总表
    INSERT INTO _verify_fail_counts (section_id, section_name, fail_count, warn_count)
    VALUES (7, 'Engram schema 验证', v_fail_count, 0);
END $$;

-- 8. 验证 logbook_migrator 默认权限配置
DO $$
DECLARE
    v_schema TEXT;
    v_defacl_count INT;
    v_fail_count INT := 0;
    engram_schemas TEXT[] := _get_engram_schemas();  -- 使用辅助函数获取（支持 schema_prefix）
    v_grantor_exists BOOLEAN;
    v_schema_prefix TEXT := _get_schema_prefix();
BEGIN
    RAISE NOTICE '';
    RAISE NOTICE '=== 8. logbook_migrator 默认权限验证 ===';
    
    -- 输出 schema_prefix 信息（如有）
    IF v_schema_prefix IS NOT NULL THEN
        RAISE NOTICE '使用 schema_prefix: %，验证 schema 列表: %', v_schema_prefix, engram_schemas;
    END IF;
    
    -- 检查 logbook_migrator 角色是否存在
    SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = 'logbook_migrator') INTO v_grantor_exists;
    IF NOT v_grantor_exists THEN
        RAISE NOTICE 'SKIP: logbook_migrator 角色不存在（可能未执行 python logbook_postgres/scripts/db_bootstrap.py）';
        RETURN;
    END IF;
    
    RAISE NOTICE 'logbook_migrator 角色存在，检查默认权限...';
    
    -- 遍历每个 schema 检查默认权限
    FOREACH v_schema IN ARRAY engram_schemas LOOP
        -- 检查 schema 是否存在
        IF NOT EXISTS(SELECT 1 FROM information_schema.schemata WHERE schema_name = v_schema) THEN
            RAISE NOTICE 'SKIP: schema % 不存在', v_schema;
            CONTINUE;
        END IF;
        
        -- 检查 logbook_migrator 是否为该 schema 设置了默认权限
        -- pg_default_acl 存储默认权限，defaclrole 是 grantor
        SELECT COUNT(*) INTO v_defacl_count
        FROM pg_default_acl da
        JOIN pg_namespace n ON n.oid = da.defaclnamespace
        JOIN pg_roles r ON r.oid = da.defaclrole
        WHERE n.nspname = v_schema
          AND r.rolname = 'logbook_migrator';
        
        IF v_defacl_count > 0 THEN
            RAISE NOTICE 'OK: logbook_migrator 在 % schema 有 % 条默认权限配置', v_schema, v_defacl_count;
        ELSE
            RAISE WARNING 'WARN: logbook_migrator 在 % schema 无默认权限配置（应使用 engram_migrator）', v_schema;
            v_fail_count := v_fail_count + 1;
        END IF;
    END LOOP;
    
    -- 汇总
    -- 注意：这里的 v_fail_count 实际上是 WARN 计数（logbook_migrator 无默认权限是正常的）
    IF v_fail_count > 0 THEN
        RAISE NOTICE 'logbook_migrator 默认权限验证: % 个 schema 无配置（正常，使用 engram_migrator 设置）', v_fail_count;
    ELSE
        RAISE NOTICE 'logbook_migrator 默认权限验证: 全部通过';
    END IF;
    
    -- 记录到汇总表（这些不是真正的 FAIL，而是 WARN/INFO）
    INSERT INTO _verify_fail_counts (section_id, section_name, fail_count, warn_count)
    VALUES (8, 'logbook_migrator 默认权限验证', 0, v_fail_count);
END $$;

-- 9. 验证默认权限详情（TABLES/SEQUENCES/FUNCTIONS）
DO $$
DECLARE
    v_rec RECORD;
    v_grantor_exists BOOLEAN;
    v_om_schema TEXT;
    v_engram_schemas TEXT[] := _get_engram_schemas();  -- 使用辅助函数获取（支持 schema_prefix）
    v_schema_prefix TEXT := _get_schema_prefix();
BEGIN
    RAISE NOTICE '';
    RAISE NOTICE '=== 9. 默认权限详情 ===';
    
    -- 输出 schema_prefix 信息（如有）
    IF v_schema_prefix IS NOT NULL THEN
        RAISE NOTICE '使用 schema_prefix: %，查询 schema 列表: %', v_schema_prefix, v_engram_schemas;
    END IF;
    
    -- 获取目标 schema
    v_om_schema := COALESCE(
        NULLIF(current_setting('om.target_schema', true), ''),
        'openmemory'
    );
    
    -- 9.1 列出 engram_migrator 的所有默认权限
    RAISE NOTICE '';
    RAISE NOTICE '--- 9.1 engram_migrator 默认权限 ---';
    
    SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = 'engram_migrator') INTO v_grantor_exists;
    IF NOT v_grantor_exists THEN
        RAISE NOTICE 'SKIP: engram_migrator 角色不存在';
    ELSE
        FOR v_rec IN
            SELECT 
                n.nspname AS schema_name,
                CASE da.defaclobjtype
                    WHEN 'r' THEN 'TABLES'
                    WHEN 'S' THEN 'SEQUENCES'
                    WHEN 'f' THEN 'FUNCTIONS'
                    WHEN 'T' THEN 'TYPES'
                    ELSE da.defaclobjtype::text
                END AS obj_type,
                array_agg(DISTINCT 
                    CASE 
                        WHEN a.grantee = 0 THEN 'PUBLIC'
                        ELSE (SELECT rolname FROM pg_roles WHERE oid = a.grantee)
                    END
                ) AS grantees
            FROM pg_default_acl da
            JOIN pg_namespace n ON n.oid = da.defaclnamespace
            JOIN pg_roles r ON r.oid = da.defaclrole
            CROSS JOIN LATERAL aclexplode(da.defaclacl) AS a
            WHERE r.rolname = 'engram_migrator'
              AND n.nspname = ANY(v_engram_schemas)  -- 使用动态 schema 列表
            GROUP BY n.nspname, da.defaclobjtype
            ORDER BY n.nspname, da.defaclobjtype
        LOOP
            RAISE NOTICE '  %: % -> %', v_rec.schema_name, v_rec.obj_type, v_rec.grantees;
        END LOOP;
    END IF;
    
    -- 9.2 列出 openmemory_migrator 的所有默认权限
    RAISE NOTICE '';
    RAISE NOTICE '--- 9.2 openmemory_migrator 默认权限 ---';
    
    SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = 'openmemory_migrator') INTO v_grantor_exists;
    IF NOT v_grantor_exists THEN
        RAISE NOTICE 'SKIP: openmemory_migrator 角色不存在';
    ELSE
        FOR v_rec IN
            SELECT 
                n.nspname AS schema_name,
                CASE da.defaclobjtype
                    WHEN 'r' THEN 'TABLES'
                    WHEN 'S' THEN 'SEQUENCES'
                    WHEN 'f' THEN 'FUNCTIONS'
                    WHEN 'T' THEN 'TYPES'
                    ELSE da.defaclobjtype::text
                END AS obj_type,
                array_agg(DISTINCT 
                    CASE 
                        WHEN a.grantee = 0 THEN 'PUBLIC'
                        ELSE (SELECT rolname FROM pg_roles WHERE oid = a.grantee)
                    END
                ) AS grantees
            FROM pg_default_acl da
            JOIN pg_namespace n ON n.oid = da.defaclnamespace
            JOIN pg_roles r ON r.oid = da.defaclrole
            CROSS JOIN LATERAL aclexplode(da.defaclacl) AS a
            WHERE r.rolname = 'openmemory_migrator'
              AND n.nspname = v_om_schema
            GROUP BY n.nspname, da.defaclobjtype
            ORDER BY n.nspname, da.defaclobjtype
        LOOP
            RAISE NOTICE '  %: % -> %', v_rec.schema_name, v_rec.obj_type, v_rec.grantees;
        END LOOP;
    END IF;
    
END $$;

-- 10. 输出总结
DO $$
DECLARE
    v_schema TEXT;
    v_schema_exists BOOLEAN;
    v_schema_owner TEXT;
    v_migrator_exists BOOLEAN;
    v_db TEXT;
    v_schema_prefix TEXT := _get_schema_prefix();
    v_engram_schemas TEXT[] := _get_engram_schemas();
BEGIN
    -- 获取目标 schema 名称
    v_schema := COALESCE(
        NULLIF(current_setting('om.target_schema', true), ''),
        'openmemory'
    );
    
    v_db := current_database();
    
    -- 检查 schema 是否存在
    SELECT EXISTS(SELECT 1 FROM information_schema.schemata WHERE schema_name = v_schema) INTO v_schema_exists;
    
    -- 获取 schema owner
    IF v_schema_exists THEN
        SELECT nspowner::regrole::text INTO v_schema_owner
        FROM pg_namespace
        WHERE nspname = v_schema;
    END IF;
    
    -- 检查 logbook_migrator 是否存在
    SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = 'logbook_migrator') INTO v_migrator_exists;
    
    RAISE NOTICE '';
    RAISE NOTICE '============================================================';
    RAISE NOTICE '=== 验证完成 ===';
    RAISE NOTICE '============================================================';
    RAISE NOTICE '';
    RAISE NOTICE '请检查上方输出，确认没有 FAIL 或 WARNING 消息。';
    RAISE NOTICE '';
    RAISE NOTICE '当前配置：';
    RAISE NOTICE '  - 数据库 = %', v_db;
    RAISE NOTICE '  - om.target_schema = %', v_schema;
    IF v_schema_prefix IS NOT NULL THEN
        RAISE NOTICE '  - engram.schema_prefix = %', v_schema_prefix;
        RAISE NOTICE '  - 验证的 engram schema 列表 = %', v_engram_schemas;
    END IF;
    RAISE NOTICE '  - logbook_migrator 存在 = %', v_migrator_exists;
    IF v_schema_exists THEN
        RAISE NOTICE '  - % schema owner = %', v_schema, v_schema_owner;
    END IF;
    RAISE NOTICE '';
    RAISE NOTICE '核心验证预期：';
    RAISE NOTICE '  1. NOLOGIN 角色存在（engram_*, openmemory_*）';
    RAISE NOTICE '  2. LOGIN 角色正确继承对应 NOLOGIN 角色';
    RAISE NOTICE '     - logbook_migrator -> engram_migrator';
    RAISE NOTICE '     - logbook_svc -> engram_app_readwrite';
    RAISE NOTICE '     - openmemory_migrator_login -> openmemory_migrator';
    RAISE NOTICE '     - openmemory_svc -> openmemory_app';
    RAISE NOTICE '  3. public schema 无 CREATE 权限（所有应用角色）';
    RAISE NOTICE '  4. 目标 OM schema (%) 存在且 owner=openmemory_migrator', v_schema;
    RAISE NOTICE '  5. openmemory_migrator 在 % 有 CREATE 权限', v_schema;
    RAISE NOTICE '  6. openmemory_app 在 % 有 USAGE 且无 CREATE 权限', v_schema;
    RAISE NOTICE '  7. pg_default_acl 默认权限正确配置';
    RAISE NOTICE '  8. 数据库权限：CONNECT=Y, CREATE=N (非admin), TEMP=Y';
    RAISE NOTICE '';
    
    -- 状态总结
    IF v_schema_exists THEN
        IF v_schema_owner = 'openmemory_migrator' THEN
            RAISE NOTICE '状态: % schema 已创建，owner 正确', v_schema;
        ELSE
            RAISE NOTICE '状态: % schema 已创建，但 owner 不正确 (当前: %, 预期: openmemory_migrator)', v_schema, v_schema_owner;
        END IF;
    ELSE
        RAISE NOTICE '状态: % schema 未创建，请执行 05 脚本或 --apply-openmemory-grants', v_schema;
    END IF;
    
    IF v_migrator_exists THEN
        RAISE NOTICE '状态: logbook_migrator 已创建，默认权限应已配置';
    ELSE
        RAISE NOTICE '状态: logbook_migrator 未创建，请先执行 python logbook_postgres/scripts/db_bootstrap.py';
    END IF;
    
    RAISE NOTICE '';
    RAISE NOTICE '输出级别说明：';
    RAISE NOTICE '  FAIL - 严重问题，必须修复才能正常工作';
    RAISE NOTICE '  WARN - 潜在问题，可能影响安全或功能';
    RAISE NOTICE '  OK   - 检查通过';
    RAISE NOTICE '  SKIP - 条件不满足，跳过检查';
END $$;

-- 11. Strict 模式汇总与异常处理
-- 当 engram.verify_strict = '1' 时，如果有任何 FAIL 则抛出异常
DO $$
DECLARE
    v_total_fail INT;
    v_total_warn INT;
    v_is_strict BOOLEAN;
    v_strict_setting TEXT;
    v_rec RECORD;
    v_failed_sections TEXT := '';
BEGIN
    -- 检查是否启用 strict 模式
    v_strict_setting := COALESCE(
        NULLIF(current_setting('engram.verify_strict', true), ''),
        '0'
    );
    v_is_strict := (v_strict_setting = '1');
    
    -- 汇总所有 fail_count
    SELECT COALESCE(SUM(fail_count), 0), COALESCE(SUM(warn_count), 0)
    INTO v_total_fail, v_total_warn
    FROM _verify_fail_counts;
    
    RAISE NOTICE '';
    RAISE NOTICE '============================================================';
    RAISE NOTICE '=== 验证汇总 ===';
    RAISE NOTICE '============================================================';
    RAISE NOTICE '';
    RAISE NOTICE 'Strict 模式: %', CASE WHEN v_is_strict THEN '启用' ELSE '禁用' END;
    RAISE NOTICE '总计 FAIL: %', v_total_fail;
    RAISE NOTICE '总计 WARN: %', v_total_warn;
    RAISE NOTICE '';
    
    -- 列出有 FAIL 的 section
    IF v_total_fail > 0 THEN
        RAISE NOTICE '有 FAIL 的验证项：';
        FOR v_rec IN 
            SELECT section_name, fail_count 
            FROM _verify_fail_counts 
            WHERE fail_count > 0 
            ORDER BY section_id
        LOOP
            RAISE NOTICE '  - %: % 项 FAIL', v_rec.section_name, v_rec.fail_count;
            v_failed_sections := v_failed_sections || v_rec.section_name || ' (' || v_rec.fail_count || '), ';
        END LOOP;
        RAISE NOTICE '';
    END IF;
    
    -- 列出有 WARN 的 section
    IF v_total_warn > 0 THEN
        RAISE NOTICE '有 WARN 的验证项：';
        FOR v_rec IN 
            SELECT section_name, warn_count 
            FROM _verify_fail_counts 
            WHERE warn_count > 0 
            ORDER BY section_id
        LOOP
            RAISE NOTICE '  - %: % 项 WARN', v_rec.section_name, v_rec.warn_count;
        END LOOP;
        RAISE NOTICE '';
    END IF;
    
    -- Strict 模式下，有 FAIL 或 WARN 则抛出异常（用于 CI 门禁）
    IF v_is_strict AND (v_total_fail > 0 OR v_total_warn > 0) THEN
        -- 移除末尾的 ", "
        v_failed_sections := rtrim(v_failed_sections, ', ');
        
        RAISE EXCEPTION 'VERIFY_STRICT_FAILED: 权限验证失败，共 % 项 FAIL，% 项 WARN。失败的验证项: [%]。请修复上述问题后重试。', 
            v_total_fail, v_total_warn, v_failed_sections;
    END IF;
    
    -- 非 strict 模式，输出结论
    IF v_total_fail > 0 THEN
        RAISE WARNING '权限验证完成，但存在 % 项 FAIL，请检查并修复', v_total_fail;
    ELSIF v_total_warn > 0 THEN
        RAISE NOTICE '权限验证完成，存在 % 项 WARN（可选修复）', v_total_warn;
    ELSE
        RAISE NOTICE '权限验证完成，全部通过';
    END IF;
END $$;

-- SAFE: 清理会话内创建的临时表和辅助函数
DROP TABLE IF EXISTS _verify_fail_counts;
DROP FUNCTION IF EXISTS _get_engram_schemas();
DROP FUNCTION IF EXISTS _get_schema_prefix();
