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

-- 1. 验证角色是否存在
-- Seek 角色验证受 seek.enabled 配置变量控制（默认启用）
-- 设置方式: SET seek.enabled = 'false'; 或通过 bootstrap_roles SEEK_ENABLE 环境变量
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
    -- Seek 角色（仅当 Seek 启用时验证）
    seek_roles TEXT[] := ARRAY[
        'seek_migrator',
        'seek_app'
    ];
    role_name TEXT;
    v_fail_count INT := 0;
    v_seek_enabled BOOLEAN;
BEGIN
    RAISE NOTICE '=== 1. NOLOGIN 角色验证 ===';
    
    -- 检查 Seek 是否启用（默认启用）
    v_seek_enabled := COALESCE(
        NULLIF(current_setting('seek.enabled', true), ''),
        'true'
    ) = 'true';
    
    IF NOT v_seek_enabled THEN
        RAISE NOTICE '[INFO] Seek 未启用 (seek.enabled=false)，跳过 Seek 角色验证';
    END IF;
    
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
    
    -- 验证 Seek 角色（仅当 Seek 启用时）
    IF v_seek_enabled THEN
        FOREACH role_name IN ARRAY seek_roles LOOP
            SELECT COUNT(*) INTO role_count FROM pg_roles WHERE rolname = role_name;
            IF role_count = 0 THEN
                RAISE WARNING 'FAIL: 角色不存在: %', role_name;
                RAISE NOTICE '  remedy: 执行 06_seek_roles_and_grants.sql 创建角色';
                v_fail_count := v_fail_count + 1;
            ELSE
                RAISE NOTICE 'OK: 角色 % 存在', role_name;
            END IF;
        END LOOP;
    END IF;
    
    IF v_fail_count > 0 THEN
        RAISE NOTICE 'NOLOGIN 角色验证: % 项 FAIL', v_fail_count;
    ELSE
        RAISE NOTICE 'NOLOGIN 角色验证: 全部通过';
    END IF;
END $$;

-- 2. 验证 LOGIN 角色存在性及 membership
DO $$
DECLARE
    v_fail_count INT := 0;
    v_warn_count INT := 0;
    v_login_exists BOOLEAN;
    v_target_exists BOOLEAN;
    v_has_membership BOOLEAN;
    v_seek_enabled BOOLEAN;
    
    -- LOGIN 角色 -> NOLOGIN 角色的映射（核心角色）
    -- 格式: login_role, target_role, description
    core_login_mappings TEXT[][] := ARRAY[
        ARRAY['logbook_migrator', 'engram_migrator', 'Logbook 迁移账号'],
        ARRAY['logbook_svc', 'engram_app_readwrite', 'Logbook 运行账号'],
        ARRAY['openmemory_migrator_login', 'openmemory_migrator', 'OpenMemory 迁移账号'],
        ARRAY['openmemory_svc', 'openmemory_app', 'OpenMemory 运行账号']
    ];
    -- Seek LOGIN 角色映射（仅当 Seek 启用时验证）
    seek_login_mappings TEXT[][] := ARRAY[
        ARRAY['seek_migrator_login', 'seek_migrator', 'Seek 迁移账号'],
        ARRAY['seek_svc', 'seek_app', 'Seek 运行账号']
    ];
    login_mappings TEXT[][];
    v_mapping TEXT[];
    v_login_role TEXT;
    v_target_role TEXT;
    v_desc TEXT;
BEGIN
    RAISE NOTICE '';
    RAISE NOTICE '=== 2. LOGIN 角色及 membership 验证 ===';
    
    -- 检查 Seek 是否启用
    v_seek_enabled := COALESCE(
        NULLIF(current_setting('seek.enabled', true), ''),
        'true'
    ) = 'true';
    
    -- 构建待验证的映射列表
    IF v_seek_enabled THEN
        login_mappings := core_login_mappings || seek_login_mappings;
    ELSE
        login_mappings := core_login_mappings;
        RAISE NOTICE '[INFO] Seek 未启用，跳过 Seek LOGIN 角色验证';
    END IF;
    
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
            RAISE NOTICE '  remedy: 执行 00_init_service_accounts.sh 创建 LOGIN 角色';
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
    SELECT pg_catalog.has_schema_privilege('PUBLIC', 'public', 'CREATE') INTO can_create;
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
    
    -- seek_migrator 不应有 public CREATE 权限（使用独立 seek schema）
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'seek_migrator') THEN
        SELECT pg_catalog.has_schema_privilege('seek_migrator', 'public', 'CREATE') INTO can_create;
        IF can_create THEN
            RAISE WARNING 'WARN: seek_migrator 有 public schema 的 CREATE 权限（应使用独立 seek schema）';
            RAISE NOTICE '  remedy: REVOKE CREATE ON SCHEMA public FROM seek_migrator;';
        ELSE
            RAISE NOTICE 'OK: seek_migrator 无 public schema CREATE 权限';
        END IF;
    END IF;
    
    -- 汇总
    IF v_fail_count > 0 THEN
        RAISE NOTICE 'public schema 验证: % 项 FAIL', v_fail_count;
    ELSE
        RAISE NOTICE 'public schema 验证: 全部通过';
    END IF;
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
END $$;

-- 4.5 验证 seek schema 存在性、owner 和权限
-- Seek 验证受 seek.enabled 配置变量控制（默认启用）
DO $$
DECLARE
    v_schema_exists BOOLEAN;
    v_schema_owner TEXT;
    can_create BOOLEAN;
    can_usage BOOLEAN;
    v_fail_count INT := 0;
    v_seek_enabled BOOLEAN;
BEGIN
    RAISE NOTICE '';
    RAISE NOTICE '=== 4.5 Seek schema 验证 ===';
    
    -- 检查 Seek 是否启用
    v_seek_enabled := COALESCE(
        NULLIF(current_setting('seek.enabled', true), ''),
        'true'
    ) = 'true';
    
    IF NOT v_seek_enabled THEN
        RAISE NOTICE 'SKIP: Seek 未启用 (seek.enabled=false)，跳过 Seek schema 验证';
        RETURN;
    END IF;
    
    -- 检查 schema 是否存在
    SELECT EXISTS(SELECT 1 FROM information_schema.schemata WHERE schema_name = 'seek') INTO v_schema_exists;
    
    IF NOT v_schema_exists THEN
        RAISE WARNING 'FAIL: seek schema 不存在';
        RAISE NOTICE '  remedy: 执行 06_seek_roles_and_grants.sql';
        RAISE NOTICE '后续权限验证跳过，请先创建 seek schema';
        RETURN;
    END IF;
    
    RAISE NOTICE 'OK: seek schema 存在';
    
    -- 验证 schema owner = seek_migrator
    SELECT nspowner::regrole::text INTO v_schema_owner
    FROM pg_namespace
    WHERE nspname = 'seek';
    
    IF v_schema_owner = 'seek_migrator' THEN
        RAISE NOTICE 'OK: seek schema owner = seek_migrator';
    ELSE
        RAISE WARNING 'FAIL: seek schema owner = % (预期: seek_migrator)', v_schema_owner;
        RAISE NOTICE '  remedy: ALTER SCHEMA seek OWNER TO seek_migrator;';
        v_fail_count := v_fail_count + 1;
    END IF;
    
    -- seek_migrator 应有 CREATE 权限
    SELECT pg_catalog.has_schema_privilege('seek_migrator', 'seek', 'CREATE') INTO can_create;
    IF can_create THEN
        RAISE NOTICE 'OK: seek_migrator 有 seek schema CREATE 权限';
    ELSE
        RAISE WARNING 'FAIL: seek_migrator 无 seek schema CREATE 权限';
        RAISE NOTICE '  remedy: GRANT ALL PRIVILEGES ON SCHEMA seek TO seek_migrator;';
        v_fail_count := v_fail_count + 1;
    END IF;
    
    -- seek_app 应有 USAGE 权限
    SELECT pg_catalog.has_schema_privilege('seek_app', 'seek', 'USAGE') INTO can_usage;
    IF can_usage THEN
        RAISE NOTICE 'OK: seek_app 有 seek schema USAGE 权限';
    ELSE
        RAISE WARNING 'FAIL: seek_app 无 seek schema USAGE 权限';
        RAISE NOTICE '  remedy: GRANT USAGE ON SCHEMA seek TO seek_app;';
        v_fail_count := v_fail_count + 1;
    END IF;
    
    -- seek_app 不应有 CREATE 权限
    SELECT pg_catalog.has_schema_privilege('seek_app', 'seek', 'CREATE') INTO can_create;
    IF can_create THEN
        RAISE WARNING 'FAIL: seek_app 有 seek schema CREATE 权限（应仅限 DML）';
        RAISE NOTICE '  remedy: REVOKE CREATE ON SCHEMA seek FROM seek_app;';
        v_fail_count := v_fail_count + 1;
    ELSE
        RAISE NOTICE 'OK: seek_app 无 seek schema CREATE 权限';
    END IF;
    
    -- 汇总
    IF v_fail_count > 0 THEN
        RAISE NOTICE 'Seek schema 验证: % 项 FAIL', v_fail_count;
    ELSE
        RAISE NOTICE 'Seek schema 验证: 全部通过';
    END IF;
END $$;

-- 4.6 验证 seek 表级 DML 权限（如果表存在）
DO $$
DECLARE
    v_schema_exists BOOLEAN;
    v_table_count INT;
    v_fail_count INT := 0;
    v_table_name TEXT;
    v_has_select BOOLEAN;
    v_has_insert BOOLEAN;
    v_has_update BOOLEAN;
    v_has_delete BOOLEAN;
    v_has_truncate BOOLEAN;
    v_seek_enabled BOOLEAN;
BEGIN
    RAISE NOTICE '';
    RAISE NOTICE '=== 4.6 Seek 表级 DML 权限验证 ===';
    
    -- 检查 Seek 是否启用
    v_seek_enabled := COALESCE(
        NULLIF(current_setting('seek.enabled', true), ''),
        'true'
    ) = 'true';
    
    IF NOT v_seek_enabled THEN
        RAISE NOTICE 'SKIP: Seek 未启用 (seek.enabled=false)，跳过表级权限验证';
        RETURN;
    END IF;
    
    -- 检查 schema 是否存在
    SELECT EXISTS(SELECT 1 FROM information_schema.schemata WHERE schema_name = 'seek') INTO v_schema_exists;
    
    IF NOT v_schema_exists THEN
        RAISE NOTICE 'SKIP: seek schema 不存在，跳过表级权限验证';
        RETURN;
    END IF;
    
    -- 获取表数量
    SELECT COUNT(*) INTO v_table_count
    FROM information_schema.tables
    WHERE table_schema = 'seek' AND table_type = 'BASE TABLE';
    
    IF v_table_count = 0 THEN
        RAISE NOTICE 'SKIP: seek schema 中无表，跳过表级权限验证';
        RETURN;
    END IF;
    
    RAISE NOTICE '检查 % 个表的权限...', v_table_count;
    
    -- 遍历每个表检查权限
    FOR v_table_name IN 
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'seek' AND table_type = 'BASE TABLE'
    LOOP
        -- 检查 seek_app 的 DML 权限
        SELECT 
            has_table_privilege('seek_app', 'seek.' || v_table_name, 'SELECT'),
            has_table_privilege('seek_app', 'seek.' || v_table_name, 'INSERT'),
            has_table_privilege('seek_app', 'seek.' || v_table_name, 'UPDATE'),
            has_table_privilege('seek_app', 'seek.' || v_table_name, 'DELETE'),
            has_table_privilege('seek_app', 'seek.' || v_table_name, 'TRUNCATE')
        INTO v_has_select, v_has_insert, v_has_update, v_has_delete, v_has_truncate;
        
        -- seek_app 应有 SELECT/INSERT/UPDATE/DELETE，不应有 TRUNCATE
        IF v_has_select AND v_has_insert AND v_has_update AND v_has_delete THEN
            RAISE NOTICE 'OK: seek_app 对 seek.% 有 SELECT/INSERT/UPDATE/DELETE 权限', v_table_name;
        ELSE
            RAISE WARNING 'FAIL: seek_app 对 seek.% 缺少 DML 权限 (S=%,I=%,U=%,D=%)', 
                v_table_name, v_has_select, v_has_insert, v_has_update, v_has_delete;
            RAISE NOTICE '  remedy: GRANT SELECT, INSERT, UPDATE, DELETE ON seek.% TO seek_app;', v_table_name;
            v_fail_count := v_fail_count + 1;
        END IF;
        
        -- seek_app 不应有 TRUNCATE 权限（安全要求）
        IF v_has_truncate THEN
            RAISE WARNING 'WARN: seek_app 对 seek.% 有 TRUNCATE 权限（应仅限 migrator）', v_table_name;
        END IF;
        
        -- 检查 seek_migrator 的完整权限
        SELECT 
            has_table_privilege('seek_migrator', 'seek.' || v_table_name, 'SELECT'),
            has_table_privilege('seek_migrator', 'seek.' || v_table_name, 'INSERT'),
            has_table_privilege('seek_migrator', 'seek.' || v_table_name, 'UPDATE'),
            has_table_privilege('seek_migrator', 'seek.' || v_table_name, 'DELETE'),
            has_table_privilege('seek_migrator', 'seek.' || v_table_name, 'TRUNCATE')
        INTO v_has_select, v_has_insert, v_has_update, v_has_delete, v_has_truncate;
        
        IF v_has_select AND v_has_insert AND v_has_update AND v_has_delete AND v_has_truncate THEN
            RAISE NOTICE 'OK: seek_migrator 对 seek.% 有完整权限', v_table_name;
        ELSE
            RAISE WARNING 'FAIL: seek_migrator 对 seek.% 缺少权限', v_table_name;
            RAISE NOTICE '  remedy: GRANT ALL PRIVILEGES ON seek.% TO seek_migrator;', v_table_name;
            v_fail_count := v_fail_count + 1;
        END IF;
    END LOOP;
    
    -- 汇总
    IF v_fail_count > 0 THEN
        RAISE NOTICE 'Seek 表级权限验证: % 项 FAIL', v_fail_count;
    ELSE
        RAISE NOTICE 'Seek 表级权限验证: 全部通过';
    END IF;
END $$;

-- 4.7 验证 seek 序列权限（如果序列存在）
DO $$
DECLARE
    v_schema_exists BOOLEAN;
    v_seq_count INT;
    v_fail_count INT := 0;
    v_seq_name TEXT;
    v_has_usage BOOLEAN;
    v_seek_enabled BOOLEAN;
BEGIN
    RAISE NOTICE '';
    RAISE NOTICE '=== 4.7 Seek 序列权限验证 ===';
    
    -- 检查 Seek 是否启用
    v_seek_enabled := COALESCE(
        NULLIF(current_setting('seek.enabled', true), ''),
        'true'
    ) = 'true';
    
    IF NOT v_seek_enabled THEN
        RAISE NOTICE 'SKIP: Seek 未启用 (seek.enabled=false)，跳过序列权限验证';
        RETURN;
    END IF;
    
    -- 检查 schema 是否存在
    SELECT EXISTS(SELECT 1 FROM information_schema.schemata WHERE schema_name = 'seek') INTO v_schema_exists;
    
    IF NOT v_schema_exists THEN
        RAISE NOTICE 'SKIP: seek schema 不存在，跳过序列权限验证';
        RETURN;
    END IF;
    
    -- 获取序列数量
    SELECT COUNT(*) INTO v_seq_count
    FROM information_schema.sequences
    WHERE sequence_schema = 'seek';
    
    IF v_seq_count = 0 THEN
        RAISE NOTICE 'SKIP: seek schema 中无序列，跳过序列权限验证';
        RETURN;
    END IF;
    
    RAISE NOTICE '检查 % 个序列的权限...', v_seq_count;
    
    -- 遍历每个序列检查权限
    FOR v_seq_name IN 
        SELECT sequence_name FROM information_schema.sequences
        WHERE sequence_schema = 'seek'
    LOOP
        -- 检查 seek_app 的 USAGE 权限
        SELECT has_sequence_privilege('seek_app', 'seek.' || v_seq_name, 'USAGE')
        INTO v_has_usage;
        
        IF v_has_usage THEN
            RAISE NOTICE 'OK: seek_app 对 seek.% 有 USAGE 权限', v_seq_name;
        ELSE
            RAISE WARNING 'FAIL: seek_app 对 seek.% 缺少 USAGE 权限', v_seq_name;
            RAISE NOTICE '  remedy: GRANT USAGE ON SEQUENCE seek.% TO seek_app;', v_seq_name;
            v_fail_count := v_fail_count + 1;
        END IF;
    END LOOP;
    
    -- 汇总
    IF v_fail_count > 0 THEN
        RAISE NOTICE 'Seek 序列权限验证: % 项 FAIL', v_fail_count;
    ELSE
        RAISE NOTICE 'Seek 序列权限验证: 全部通过';
    END IF;
END $$;

-- 5. 验证 pg_default_acl 默认权限配置
-- 检查 openmemory_migrator 对 openmemory_app 的 TABLE/SEQUENCE 默认授权
-- 检查 engram_migrator 对 engram_app_* 的 TABLE/SEQUENCE 默认授权
-- 检查 seek_migrator 对 seek_app 的 TABLE/SEQUENCE 默认授权
DO $$
DECLARE
    v_schema TEXT;
    v_fail_count INT := 0;
    v_warn_count INT := 0;
    v_count INT;
    v_grantor_oid OID;
    v_grantee_oid OID;
    v_schema_oid OID;
    engram_schemas TEXT[] := ARRAY['identity', 'logbook', 'scm', 'analysis', 'governance'];
    v_engram_schema TEXT;
BEGIN
    RAISE NOTICE '';
    RAISE NOTICE '=== 5. pg_default_acl 默认权限验证 ===';
    
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
    
    -- 5.2 验证 seek_migrator -> seek_app 默认权限
    RAISE NOTICE '';
    RAISE NOTICE '--- 5.2 Seek 默认权限 ---';
    
    -- 检查 Seek 是否启用
    IF COALESCE(NULLIF(current_setting('seek.enabled', true), ''), 'true') != 'true' THEN
        RAISE NOTICE 'SKIP: Seek 未启用 (seek.enabled=false)，跳过默认权限验证';
    ELSE
    
    -- 获取 seek schema OID
    SELECT oid INTO v_schema_oid FROM pg_namespace WHERE nspname = 'seek';
    
    IF v_schema_oid IS NULL THEN
        RAISE NOTICE 'SKIP: seek schema 不存在，跳过默认权限验证';
    ELSE
        -- 获取角色 OID
        SELECT oid INTO v_grantor_oid FROM pg_roles WHERE rolname = 'seek_migrator';
        SELECT oid INTO v_grantee_oid FROM pg_roles WHERE rolname = 'seek_app';
        
        IF v_grantor_oid IS NULL THEN
            RAISE WARNING 'FAIL: seek_migrator 角色不存在';
            RAISE NOTICE '  remedy: 执行 06_seek_roles_and_grants.sql';
            v_fail_count := v_fail_count + 1;
        ELSIF v_grantee_oid IS NULL THEN
            RAISE WARNING 'FAIL: seek_app 角色不存在';
            RAISE NOTICE '  remedy: 执行 06_seek_roles_and_grants.sql';
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
                RAISE NOTICE 'OK: seek_migrator 在 seek 对 seek_app 有 TABLE 默认授权';
            ELSE
                RAISE WARNING 'FAIL: seek_migrator 在 seek 对 seek_app 无 TABLE 默认授权';
                RAISE NOTICE '  remedy: ALTER DEFAULT PRIVILEGES FOR ROLE seek_migrator IN SCHEMA seek GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO seek_app;';
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
                RAISE NOTICE 'OK: seek_migrator 在 seek 对 seek_app 有 SEQUENCE 默认授权';
            ELSE
                RAISE WARNING 'FAIL: seek_migrator 在 seek 对 seek_app 无 SEQUENCE 默认授权';
                RAISE NOTICE '  remedy: ALTER DEFAULT PRIVILEGES FOR ROLE seek_migrator IN SCHEMA seek GRANT USAGE ON SEQUENCES TO seek_app;';
                v_fail_count := v_fail_count + 1;
            END IF;
        END IF;
    END IF;
    END IF;  -- END IF for Seek enabled check
    
    -- 5.3 验证 engram_migrator -> engram_app_readwrite/readonly 默认权限
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
END $$;

-- 6. 验证数据库级权限硬化（08_database_hardening.sql）
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
        -- Seek 角色
        ARRAY['seek_migrator', 'Y', 'Y', 'Y'],            -- 迁移角色需要 CREATE/TEMP
        ARRAY['seek_app', 'Y', 'N', 'N'],                 -- 应用角色无需 CREATE/TEMP
        -- LOGIN 角色（如存在）
        ARRAY['logbook_migrator', 'Y', 'Y', 'Y'],           -- 继承 engram_migrator
        ARRAY['logbook_svc', 'Y', 'N', 'N'],                -- 继承 engram_app_readwrite
        ARRAY['openmemory_migrator_login', 'Y', 'Y', 'Y'],-- 继承 openmemory_migrator
        ARRAY['openmemory_svc', 'Y', 'N', 'N'],           -- 继承 openmemory_app
        ARRAY['seek_migrator_login', 'Y', 'Y', 'Y'],      -- 继承 seek_migrator
        ARRAY['seek_svc', 'Y', 'N', 'N']                  -- 继承 seek_app
    ];
    v_check TEXT[];
    v_role TEXT;
    v_expect_connect TEXT;
    v_expect_create TEXT;
    v_expect_temp TEXT;
    v_role_exists BOOLEAN;
BEGIN
    RAISE NOTICE '';
    RAISE NOTICE '=== 6. 数据库级权限硬化验证（08_database_hardening.sql） ===';
    
    v_db := current_database();
    RAISE NOTICE '当前数据库: %', v_db;
    
    -- ========================================
    -- 6.1 验证 PUBLIC 的权限被撤销（核心硬化）
    -- ========================================
    RAISE NOTICE '';
    RAISE NOTICE '--- 6.1 PUBLIC 权限硬化验证 ---';
    
    -- PUBLIC 不应有 CREATE 权限
    SELECT has_database_privilege('PUBLIC', v_db, 'CREATE') INTO v_has_create;
    IF v_has_create THEN
        RAISE WARNING 'FAIL: PUBLIC 有数据库 CREATE 权限（硬化未生效）';
        RAISE NOTICE '  remedy: REVOKE CREATE ON DATABASE % FROM PUBLIC;', v_db;
        v_fail_count := v_fail_count + 1;
    ELSE
        RAISE NOTICE 'OK: PUBLIC 无数据库 CREATE 权限';
    END IF;
    
    -- PUBLIC 不应有 TEMP 权限
    SELECT has_database_privilege('PUBLIC', v_db, 'TEMP') INTO v_has_temp;
    IF v_has_temp THEN
        RAISE WARNING 'FAIL: PUBLIC 有数据库 TEMP 权限（硬化未生效）';
        RAISE NOTICE '  remedy: REVOKE TEMP ON DATABASE % FROM PUBLIC;', v_db;
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
            IF v_role IN ('logbook_migrator', 'logbook_svc', 'openmemory_migrator_login', 'openmemory_svc', 'seek_migrator_login', 'seek_svc') THEN
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
        IF v_role IN ('engram_migrator', 'engram_app_readwrite', 'openmemory_migrator', 'openmemory_app', 'seek_migrator', 'seek_app') THEN
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
END $$;

-- 7. 验证 Engram schema 权限
DO $$
DECLARE
    schema_name TEXT;
    can_create BOOLEAN;
    can_usage BOOLEAN;
    engram_schemas TEXT[] := ARRAY['identity', 'logbook', 'scm', 'analysis', 'governance'];
    v_fail_count INT := 0;
BEGIN
    RAISE NOTICE '';
    RAISE NOTICE '=== 7. Engram schema 权限验证 ===';
    
    FOREACH schema_name IN ARRAY engram_schemas LOOP
        -- 检查 schema 是否存在
        IF NOT EXISTS(SELECT 1 FROM information_schema.schemata WHERE schema_name = schema_name) THEN
            RAISE NOTICE 'SKIP: schema % 不存在', schema_name;
            CONTINUE;
        END IF;
        
        -- engram_migrator 应有 CREATE 权限
        SELECT pg_catalog.has_schema_privilege('engram_migrator', schema_name, 'CREATE') INTO can_create;
        IF can_create THEN
            RAISE NOTICE 'OK: engram_migrator 有 % schema CREATE 权限', schema_name;
        ELSE
            RAISE WARNING 'FAIL: engram_migrator 无 % schema CREATE 权限', schema_name;
            RAISE NOTICE '  remedy: GRANT ALL PRIVILEGES ON SCHEMA % TO engram_migrator;', schema_name;
            v_fail_count := v_fail_count + 1;
        END IF;
        
        -- engram_app_readwrite 应有 USAGE 权限
        SELECT pg_catalog.has_schema_privilege('engram_app_readwrite', schema_name, 'USAGE') INTO can_usage;
        IF can_usage THEN
            RAISE NOTICE 'OK: engram_app_readwrite 有 % schema USAGE 权限', schema_name;
        ELSE
            RAISE WARNING 'FAIL: engram_app_readwrite 无 % schema USAGE 权限', schema_name;
            RAISE NOTICE '  remedy: GRANT USAGE ON SCHEMA % TO engram_app_readwrite;', schema_name;
            v_fail_count := v_fail_count + 1;
        END IF;
        
        -- engram_app_readwrite 不应有 CREATE 权限
        SELECT pg_catalog.has_schema_privilege('engram_app_readwrite', schema_name, 'CREATE') INTO can_create;
        IF can_create THEN
            RAISE WARNING 'WARN: engram_app_readwrite 有 % schema CREATE 权限（应仅限 DML）', schema_name;
            RAISE NOTICE '  remedy: REVOKE CREATE ON SCHEMA % FROM engram_app_readwrite;', schema_name;
        END IF;
    END LOOP;
    
    IF v_fail_count > 0 THEN
        RAISE NOTICE 'Engram schema 验证: % 项 FAIL', v_fail_count;
    ELSE
        RAISE NOTICE 'Engram schema 验证: 全部通过';
    END IF;
END $$;

-- 8. 验证 logbook_migrator 默认权限配置
DO $$
DECLARE
    v_schema TEXT;
    v_defacl_count INT;
    v_fail_count INT := 0;
    engram_schemas TEXT[] := ARRAY['identity', 'logbook', 'scm', 'analysis', 'governance'];
    v_grantor_exists BOOLEAN;
BEGIN
    RAISE NOTICE '';
    RAISE NOTICE '=== 8. logbook_migrator 默认权限验证 ===';
    
    -- 检查 logbook_migrator 角色是否存在
    SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = 'logbook_migrator') INTO v_grantor_exists;
    IF NOT v_grantor_exists THEN
        RAISE NOTICE 'SKIP: logbook_migrator 角色不存在（可能未执行 00_init_service_accounts.sh）';
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
    IF v_fail_count > 0 THEN
        RAISE NOTICE 'logbook_migrator 默认权限验证: % 个 schema 无配置（正常，使用 engram_migrator 设置）', v_fail_count;
    ELSE
        RAISE NOTICE 'logbook_migrator 默认权限验证: 全部通过';
    END IF;
END $$;

-- 9. 验证默认权限详情（TABLES/SEQUENCES/FUNCTIONS）
DO $$
DECLARE
    v_rec RECORD;
    v_grantor_exists BOOLEAN;
    v_om_schema TEXT;
BEGIN
    RAISE NOTICE '';
    RAISE NOTICE '=== 9. 默认权限详情 ===';
    
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
              AND n.nspname IN ('identity', 'logbook', 'scm', 'analysis', 'governance')
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
    
    -- 9.3 列出 seek_migrator 的所有默认权限
    RAISE NOTICE '';
    RAISE NOTICE '--- 9.3 seek_migrator 默认权限 ---';
    
    SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = 'seek_migrator') INTO v_grantor_exists;
    IF NOT v_grantor_exists THEN
        RAISE NOTICE 'SKIP: seek_migrator 角色不存在';
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
            WHERE r.rolname = 'seek_migrator'
              AND n.nspname = 'seek'
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
    RAISE NOTICE '  - logbook_migrator 存在 = %', v_migrator_exists;
    IF v_schema_exists THEN
        RAISE NOTICE '  - % schema owner = %', v_schema, v_schema_owner;
    END IF;
    RAISE NOTICE '';
    RAISE NOTICE '核心验证预期：';
    RAISE NOTICE '  1. NOLOGIN 角色存在（engram_*, openmemory_*, seek_*）';
    RAISE NOTICE '  2. LOGIN 角色正确继承对应 NOLOGIN 角色';
    RAISE NOTICE '     - logbook_migrator -> engram_migrator';
    RAISE NOTICE '     - logbook_svc -> engram_app_readwrite';
    RAISE NOTICE '     - openmemory_migrator_login -> openmemory_migrator';
    RAISE NOTICE '     - openmemory_svc -> openmemory_app';
    RAISE NOTICE '     - seek_migrator_login -> seek_migrator';
    RAISE NOTICE '     - seek_svc -> seek_app';
    RAISE NOTICE '  3. public schema 无 CREATE 权限（所有应用角色）';
    RAISE NOTICE '  4. 目标 OM schema (%) 存在且 owner=openmemory_migrator', v_schema;
    RAISE NOTICE '  5. openmemory_migrator 在 % 有 CREATE 权限', v_schema;
    RAISE NOTICE '  6. openmemory_app 在 % 有 USAGE 且无 CREATE 权限', v_schema;
    RAISE NOTICE '  7. seek schema 存在且 owner=seek_migrator';
    RAISE NOTICE '  8. seek_migrator 在 seek 有 CREATE 权限（含 CREATE INDEX）';
    RAISE NOTICE '  9. seek_app 在 seek 有 USAGE 且无 CREATE 权限';
    RAISE NOTICE '  10. seek_app 对表有 SELECT/INSERT/UPDATE/DELETE（无 TRUNCATE）';
    RAISE NOTICE '  11. seek_app 对序列有 USAGE 权限';
    RAISE NOTICE '  12. pg_default_acl 默认权限正确配置';
    RAISE NOTICE '  13. 数据库权限：CONNECT=Y, CREATE=N (非admin), TEMP=Y';
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
        RAISE NOTICE '状态: logbook_migrator 未创建，请先执行 00_init_service_accounts.sh';
    END IF;
    
    RAISE NOTICE '';
    RAISE NOTICE '输出级别说明：';
    RAISE NOTICE '  FAIL - 严重问题，必须修复才能正常工作';
    RAISE NOTICE '  WARN - 潜在问题，可能影响安全或功能';
    RAISE NOTICE '  OK   - 检查通过';
    RAISE NOTICE '  SKIP - 条件不满足，跳过检查';
END $$;
