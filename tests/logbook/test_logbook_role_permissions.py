# -*- coding: utf-8 -*-
"""
Logbook 服务账号角色权限边界测试

验证 logbook_migrator / logbook_svc 登录角色的权限边界：
- logbook_migrator: 继承 engram_migrator，拥有 DDL 权限（CREATE TABLE 等）
- logbook_svc: 继承 engram_app_readwrite，仅 DML 权限（SELECT/INSERT/UPDATE/DELETE）

环境变量：
- LOGBOOK_MIGRATOR_PASSWORD: logbook_migrator 登录密码
- LOGBOOK_SVC_PASSWORD: logbook_svc 登录密码
- TEST_PG_DSN: 测试数据库 DSN（用于获取连接信息）

如果未设置密码环境变量，测试将使用 admin 连接临时创建服务账号。

运行方式：
- 集成测试: make test-logbook-integration
- 单独运行: pytest tests/test_logbook_role_permissions.py -v

跳过条件：
- 无法连接到测试数据库时自动 skip
- 无法创建服务账号时自动 skip
"""

import os
import uuid
from urllib.parse import urlparse, urlunparse

import psycopg
import pytest


# ---------- 环境变量名 ----------
ENV_LOGBOOK_MIGRATOR_PASSWORD = "LOGBOOK_MIGRATOR_PASSWORD"
ENV_LOGBOOK_SVC_PASSWORD = "LOGBOOK_SVC_PASSWORD"
ENV_SKIP_ROLE_PERMISSION_TESTS = "SKIP_LOGBOOK_ROLE_PERMISSION_TESTS"

# 默认测试密码（仅在自动创建账号时使用）
DEFAULT_TEST_PASSWORD = "test_password_for_role_tests_12345"


def get_password(env_var: str) -> str:
    """获取密码，优先从环境变量，否则使用默认测试密码"""
    return os.environ.get(env_var, DEFAULT_TEST_PASSWORD)


def should_skip_tests() -> bool:
    """检查是否应跳过角色权限测试"""
    return os.environ.get(ENV_SKIP_ROLE_PERMISSION_TESTS, "").lower() in ("1", "true", "yes")


def build_user_dsn(base_dsn: str, username: str, password: str) -> str:
    """构建指定用户的 DSN"""
    parsed = urlparse(base_dsn)
    # 替换用户名和密码
    new_netloc = f"{username}:{password}@{parsed.hostname}"
    if parsed.port:
        new_netloc += f":{parsed.port}"
    return urlunparse(parsed._replace(netloc=new_netloc))


@pytest.fixture(scope="module")
def role_test_setup(test_db_info: dict):
    """
    角色权限测试的设置 fixture
    
    功能：
    1. 确保角色脚本已执行（engram_migrator, engram_app_readwrite 存在）
    2. 创建或更新 logbook_migrator / logbook_svc 登录角色
    3. 授予角色 membership
    4. 返回连接信息供测试使用
    
    清理：
    测试结束后删除测试过程中创建的临时表（不删除角色，保持幂等）
    """
    if should_skip_tests():
        pytest.skip("角色权限测试已通过环境变量禁用")
    
    admin_dsn = test_db_info.get("admin_dsn", test_db_info["dsn"])
    base_dsn = test_db_info["dsn"]
    
    migrator_password = get_password(ENV_LOGBOOK_MIGRATOR_PASSWORD)
    svc_password = get_password(ENV_LOGBOOK_SVC_PASSWORD)
    
    # 生成唯一的测试 schema 后缀（用于临时表）
    test_suffix = uuid.uuid4().hex[:8]
    temp_table_name = f"test_role_perm_{test_suffix}"
    
    try:
        admin_conn = psycopg.connect(admin_dsn, autocommit=True)
    except Exception as e:
        pytest.skip(f"无法连接到管理员数据库: {e}")
        return
    
    try:
        with admin_conn.cursor() as cur:
            # 1. 确保 NOLOGIN 权限角色存在（由 04_roles_and_grants.sql 创建）
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'engram_migrator'")
            if not cur.fetchone():
                pytest.skip("engram_migrator 角色不存在，请先执行 db_migrate.py --apply-roles")
            
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'engram_app_readwrite'")
            if not cur.fetchone():
                pytest.skip("engram_app_readwrite 角色不存在，请先执行 db_migrate.py --apply-roles")
            
            # 2. 创建或更新 logbook_migrator 登录角色
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'logbook_migrator'")
            if cur.fetchone():
                cur.execute(f"ALTER ROLE logbook_migrator WITH LOGIN PASSWORD %s", (migrator_password,))
            else:
                cur.execute(f"CREATE ROLE logbook_migrator LOGIN PASSWORD %s", (migrator_password,))
            
            # 3. 创建或更新 logbook_svc 登录角色
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'logbook_svc'")
            if cur.fetchone():
                cur.execute(f"ALTER ROLE logbook_svc WITH LOGIN PASSWORD %s", (svc_password,))
            else:
                cur.execute(f"CREATE ROLE logbook_svc LOGIN PASSWORD %s", (svc_password,))
            
            # 4. 授予角色 membership
            cur.execute("GRANT engram_migrator TO logbook_migrator")
            cur.execute("GRANT engram_app_readwrite TO logbook_svc")
            
            # 5. 确保 logbook schema 存在
            cur.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = 'logbook'")
            if not cur.fetchone():
                pytest.skip("logbook schema 不存在，请先执行 db_migrate.py")
        
        yield {
            "admin_dsn": admin_dsn,
            "base_dsn": base_dsn,
            "migrator_dsn": build_user_dsn(base_dsn, "logbook_migrator", migrator_password),
            "svc_dsn": build_user_dsn(base_dsn, "logbook_svc", svc_password),
            "temp_table_name": temp_table_name,
            "test_schema": "logbook",
        }
        
    finally:
        # 清理：删除测试过程中可能创建的临时表
        try:
            with admin_conn.cursor() as cur:
                cur.execute(f'DROP TABLE IF EXISTS logbook."{temp_table_name}" CASCADE')
        except Exception:
            pass
        admin_conn.close()


class TestLogbookMigratorPermissions:
    """测试 logbook_migrator 角色权限（DDL 权限）"""
    
    def test_migrator_can_connect(self, role_test_setup: dict):
        """测试 logbook_migrator 可以登录数据库"""
        migrator_dsn = role_test_setup["migrator_dsn"]
        
        try:
            conn = psycopg.connect(migrator_dsn)
            with conn.cursor() as cur:
                cur.execute("SELECT current_user")
                current_user = cur.fetchone()[0]
                assert current_user == "logbook_migrator", f"当前用户应为 logbook_migrator，实际为 {current_user}"
            conn.close()
        except psycopg.OperationalError as e:
            pytest.fail(f"logbook_migrator 无法登录: {e}")
    
    def test_migrator_can_create_table_in_logbook(self, role_test_setup: dict):
        """测试 logbook_migrator 可以在 logbook schema 创建表"""
        migrator_dsn = role_test_setup["migrator_dsn"]
        temp_table = role_test_setup["temp_table_name"]
        schema = role_test_setup["test_schema"]
        admin_dsn = role_test_setup["admin_dsn"]
        
        conn = psycopg.connect(migrator_dsn, autocommit=True)
        try:
            with conn.cursor() as cur:
                # 设置 role 以使用 engram_migrator 权限
                cur.execute("SET ROLE engram_migrator")
                
                # 创建临时表
                cur.execute(f"""
                    CREATE TABLE {schema}."{temp_table}" (
                        id SERIAL PRIMARY KEY,
                        name TEXT NOT NULL,
                        created_at TIMESTAMPTZ DEFAULT now()
                    )
                """)
                
                # 验证表已创建
                cur.execute("""
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.tables 
                        WHERE table_schema = %s AND table_name = %s
                    )
                """, (schema, temp_table))
                exists = cur.fetchone()[0]
                assert exists, f"表 {schema}.{temp_table} 应该创建成功"
        finally:
            conn.close()
            # 清理：使用 admin 连接删除临时表
            admin_conn = psycopg.connect(admin_dsn, autocommit=True)
            with admin_conn.cursor() as cur:
                cur.execute(f'DROP TABLE IF EXISTS {schema}."{temp_table}" CASCADE')
            admin_conn.close()
    
    def test_migrator_can_alter_table(self, role_test_setup: dict):
        """测试 logbook_migrator 可以修改表结构"""
        migrator_dsn = role_test_setup["migrator_dsn"]
        temp_table = role_test_setup["temp_table_name"] + "_alter"
        schema = role_test_setup["test_schema"]
        admin_dsn = role_test_setup["admin_dsn"]
        
        conn = psycopg.connect(migrator_dsn, autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute("SET ROLE engram_migrator")
                
                # 创建表
                cur.execute(f"""
                    CREATE TABLE {schema}."{temp_table}" (
                        id SERIAL PRIMARY KEY
                    )
                """)
                
                # 添加列
                cur.execute(f"""
                    ALTER TABLE {schema}."{temp_table}" 
                    ADD COLUMN description TEXT
                """)
                
                # 验证列已添加
                cur.execute("""
                    SELECT column_name FROM information_schema.columns 
                    WHERE table_schema = %s AND table_name = %s AND column_name = 'description'
                """, (schema, temp_table))
                result = cur.fetchone()
                assert result is not None, "description 列应该存在"
        finally:
            conn.close()
            # 清理
            admin_conn = psycopg.connect(admin_dsn, autocommit=True)
            with admin_conn.cursor() as cur:
                cur.execute(f'DROP TABLE IF EXISTS {schema}."{temp_table}" CASCADE')
            admin_conn.close()
    
    def test_migrator_cannot_create_in_public(self, role_test_setup: dict):
        """测试 logbook_migrator 不能在 public schema 创建表（strict 策略）"""
        migrator_dsn = role_test_setup["migrator_dsn"]
        temp_table = role_test_setup["temp_table_name"] + "_public"
        admin_dsn = role_test_setup["admin_dsn"]
        
        conn = psycopg.connect(migrator_dsn, autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute("SET ROLE engram_migrator")
                
                # 尝试在 public 创建表（应失败）
                try:
                    cur.execute(f"""
                        CREATE TABLE public."{temp_table}" (
                            id SERIAL PRIMARY KEY
                        )
                    """)
                    # 如果成功，清理并报错
                    cur.execute(f'DROP TABLE IF EXISTS public."{temp_table}"')
                    pytest.fail("engram_migrator 不应能在 public schema 创建表")
                except psycopg.errors.InsufficientPrivilege:
                    # 预期的权限不足错误
                    pass
        finally:
            conn.close()


class TestLogbookSvcPermissions:
    """测试 logbook_svc 角色权限（仅 DML 权限）"""
    
    def test_svc_can_connect(self, role_test_setup: dict):
        """测试 logbook_svc 可以登录数据库"""
        svc_dsn = role_test_setup["svc_dsn"]
        
        try:
            conn = psycopg.connect(svc_dsn)
            with conn.cursor() as cur:
                cur.execute("SELECT current_user")
                current_user = cur.fetchone()[0]
                assert current_user == "logbook_svc", f"当前用户应为 logbook_svc，实际为 {current_user}"
            conn.close()
        except psycopg.OperationalError as e:
            pytest.fail(f"logbook_svc 无法登录: {e}")
    
    def test_svc_can_select_from_logbook_items(self, role_test_setup: dict):
        """测试 logbook_svc 可以从 logbook.items 表查询"""
        svc_dsn = role_test_setup["svc_dsn"]
        
        conn = psycopg.connect(svc_dsn)
        try:
            with conn.cursor() as cur:
                # 查询 items 表
                cur.execute("SELECT COUNT(*) FROM logbook.items")
                count = cur.fetchone()[0]
                assert count >= 0, "应能查询 logbook.items"
        finally:
            conn.close()
    
    def test_svc_can_insert_into_logbook_items(self, role_test_setup: dict):
        """测试 logbook_svc 可以向 logbook.items 表插入数据"""
        svc_dsn = role_test_setup["svc_dsn"]
        
        conn = psycopg.connect(svc_dsn, autocommit=False)
        try:
            with conn.cursor() as cur:
                # 插入一条记录
                cur.execute("""
                    INSERT INTO logbook.items 
                    (item_type, title, status)
                    VALUES ('task', 'SVC Permission Test', 'open')
                    RETURNING item_id
                """)
                item_id = cur.fetchone()[0]
                assert item_id > 0, "应能插入记录并返回 item_id"
                
                # 回滚，不污染测试数据
                conn.rollback()
        finally:
            conn.close()
    
    def test_svc_can_update_logbook_items(self, role_test_setup: dict):
        """测试 logbook_svc 可以更新 logbook.items 表"""
        svc_dsn = role_test_setup["svc_dsn"]
        
        conn = psycopg.connect(svc_dsn, autocommit=False)
        try:
            with conn.cursor() as cur:
                # 先插入
                cur.execute("""
                    INSERT INTO logbook.items 
                    (item_type, title, status)
                    VALUES ('bug', 'Update Test', 'open')
                    RETURNING item_id
                """)
                item_id = cur.fetchone()[0]
                
                # 更新
                cur.execute("""
                    UPDATE logbook.items 
                    SET status = 'closed'
                    WHERE item_id = %s
                    RETURNING status
                """, (item_id,))
                new_status = cur.fetchone()[0]
                assert new_status == "closed", "应能更新 status"
                
                conn.rollback()
        finally:
            conn.close()
    
    def test_svc_can_delete_from_logbook_items(self, role_test_setup: dict):
        """测试 logbook_svc 可以从 logbook.items 表删除数据"""
        svc_dsn = role_test_setup["svc_dsn"]
        
        conn = psycopg.connect(svc_dsn, autocommit=False)
        try:
            with conn.cursor() as cur:
                # 先插入
                cur.execute("""
                    INSERT INTO logbook.items 
                    (item_type, title, status)
                    VALUES ('task', 'Delete Test', 'open')
                    RETURNING item_id
                """)
                item_id = cur.fetchone()[0]
                
                # 删除
                cur.execute("""
                    DELETE FROM logbook.items 
                    WHERE item_id = %s
                    RETURNING item_id
                """, (item_id,))
                deleted_id = cur.fetchone()[0]
                assert deleted_id == item_id, "应能删除记录"
                
                conn.rollback()
        finally:
            conn.close()
    
    def test_svc_cannot_create_table(self, role_test_setup: dict):
        """测试 logbook_svc 不能创建表（无 DDL 权限）"""
        svc_dsn = role_test_setup["svc_dsn"]
        temp_table = role_test_setup["temp_table_name"] + "_svc"
        schema = role_test_setup["test_schema"]
        
        conn = psycopg.connect(svc_dsn, autocommit=True)
        try:
            with conn.cursor() as cur:
                # 尝试创建表（应失败）
                try:
                    cur.execute(f"""
                        CREATE TABLE {schema}."{temp_table}" (
                            id SERIAL PRIMARY KEY
                        )
                    """)
                    # 如果成功，这是个 bug
                    pytest.fail("logbook_svc 不应能创建表（仅有 DML 权限）")
                except psycopg.errors.InsufficientPrivilege:
                    # 预期的权限不足错误
                    pass
        finally:
            conn.close()
    
    def test_svc_cannot_drop_table(self, role_test_setup: dict):
        """测试 logbook_svc 不能删除表"""
        svc_dsn = role_test_setup["svc_dsn"]
        admin_dsn = role_test_setup["admin_dsn"]
        temp_table = role_test_setup["temp_table_name"] + "_drop"
        schema = role_test_setup["test_schema"]
        
        # 先用 admin 创建一个临时表
        admin_conn = psycopg.connect(admin_dsn, autocommit=True)
        with admin_conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE {schema}."{temp_table}" (
                    id SERIAL PRIMARY KEY
                )
            """)
            # 授予 svc 用户 SELECT 权限，但不授予 DROP 权限
            cur.execute(f'GRANT SELECT ON {schema}."{temp_table}" TO logbook_svc')
        admin_conn.close()
        
        try:
            svc_conn = psycopg.connect(svc_dsn, autocommit=True)
            try:
                with svc_conn.cursor() as cur:
                    # 尝试删除表（应失败）
                    try:
                        cur.execute(f'DROP TABLE {schema}."{temp_table}"')
                        pytest.fail("logbook_svc 不应能删除表")
                    except psycopg.errors.InsufficientPrivilege:
                        # 预期的权限不足错误
                        pass
            finally:
                svc_conn.close()
        finally:
            # 清理：用 admin 删除临时表
            admin_conn = psycopg.connect(admin_dsn, autocommit=True)
            with admin_conn.cursor() as cur:
                cur.execute(f'DROP TABLE IF EXISTS {schema}."{temp_table}" CASCADE')
            admin_conn.close()
    
    def test_svc_cannot_alter_table(self, role_test_setup: dict):
        """测试 logbook_svc 不能修改表结构"""
        svc_dsn = role_test_setup["svc_dsn"]
        admin_dsn = role_test_setup["admin_dsn"]
        temp_table = role_test_setup["temp_table_name"] + "_alter_svc"
        schema = role_test_setup["test_schema"]
        
        # 先用 admin 创建一个临时表
        admin_conn = psycopg.connect(admin_dsn, autocommit=True)
        with admin_conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE {schema}."{temp_table}" (
                    id SERIAL PRIMARY KEY
                )
            """)
            cur.execute(f'GRANT SELECT ON {schema}."{temp_table}" TO logbook_svc')
        admin_conn.close()
        
        try:
            svc_conn = psycopg.connect(svc_dsn, autocommit=True)
            try:
                with svc_conn.cursor() as cur:
                    # 尝试添加列（应失败）
                    try:
                        cur.execute(f"""
                            ALTER TABLE {schema}."{temp_table}" 
                            ADD COLUMN new_col TEXT
                        """)
                        pytest.fail("logbook_svc 不应能修改表结构")
                    except psycopg.errors.InsufficientPrivilege:
                        # 预期的权限不足错误
                        pass
            finally:
                svc_conn.close()
        finally:
            # 清理
            admin_conn = psycopg.connect(admin_dsn, autocommit=True)
            with admin_conn.cursor() as cur:
                cur.execute(f'DROP TABLE IF EXISTS {schema}."{temp_table}" CASCADE')
            admin_conn.close()


class TestCrossSchemaPermissions:
    """测试跨 schema 的权限边界"""
    
    def test_svc_can_access_multiple_schemas(self, role_test_setup: dict):
        """测试 logbook_svc 可以访问多个 Engram schema"""
        svc_dsn = role_test_setup["svc_dsn"]
        
        schemas_to_check = ["logbook", "identity", "scm", "analysis", "governance"]
        
        conn = psycopg.connect(svc_dsn)
        try:
            with conn.cursor() as cur:
                for schema in schemas_to_check:
                    # 检查 USAGE 权限
                    cur.execute("""
                        SELECT has_schema_privilege(current_user, %s, 'USAGE')
                    """, (schema,))
                    has_usage = cur.fetchone()[0]
                    assert has_usage, f"logbook_svc 应在 {schema} schema 有 USAGE 权限"
        finally:
            conn.close()
    
    def test_migrator_has_create_on_all_schemas(self, role_test_setup: dict):
        """测试 engram_migrator 在所有 Engram schema 有 CREATE 权限"""
        migrator_dsn = role_test_setup["migrator_dsn"]
        
        schemas_to_check = ["logbook", "identity", "scm", "analysis", "governance"]
        
        conn = psycopg.connect(migrator_dsn)
        try:
            with conn.cursor() as cur:
                cur.execute("SET ROLE engram_migrator")
                
                for schema in schemas_to_check:
                    cur.execute("""
                        SELECT has_schema_privilege('engram_migrator', %s, 'CREATE')
                    """, (schema,))
                    has_create = cur.fetchone()[0]
                    assert has_create, f"engram_migrator 应在 {schema} schema 有 CREATE 权限"
        finally:
            conn.close()
    
    def test_svc_no_create_on_any_schema(self, role_test_setup: dict):
        """测试 engram_app_readwrite 在所有 schema 没有 CREATE 权限"""
        svc_dsn = role_test_setup["svc_dsn"]
        
        # engram_app_readwrite 仅有 USAGE 权限，没有 CREATE
        schemas_to_check = ["logbook", "identity", "scm", "analysis", "governance", "public"]
        
        conn = psycopg.connect(svc_dsn)
        try:
            with conn.cursor() as cur:
                for schema in schemas_to_check:
                    cur.execute("""
                        SELECT has_schema_privilege('engram_app_readwrite', %s, 'CREATE')
                    """, (schema,))
                    has_create = cur.fetchone()[0]
                    assert not has_create, f"engram_app_readwrite 不应在 {schema} schema 有 CREATE 权限"
        finally:
            conn.close()


class TestDefaultPrivilegesWork:
    """测试默认权限是否正确生效"""
    
    def test_new_table_by_migrator_accessible_by_svc(self, role_test_setup: dict):
        """测试 engram_migrator 创建的新表可被 logbook_svc 访问"""
        migrator_dsn = role_test_setup["migrator_dsn"]
        svc_dsn = role_test_setup["svc_dsn"]
        admin_dsn = role_test_setup["admin_dsn"]
        temp_table = role_test_setup["temp_table_name"] + "_default_priv"
        schema = role_test_setup["test_schema"]
        
        # 使用 migrator 创建表
        migrator_conn = psycopg.connect(migrator_dsn, autocommit=True)
        try:
            with migrator_conn.cursor() as cur:
                cur.execute("SET ROLE engram_migrator")
                cur.execute(f"""
                    CREATE TABLE {schema}."{temp_table}" (
                        id SERIAL PRIMARY KEY,
                        name TEXT
                    )
                """)
        finally:
            migrator_conn.close()
        
        try:
            # 使用 svc 访问表（应该通过默认权限自动获得）
            svc_conn = psycopg.connect(svc_dsn)
            try:
                with svc_conn.cursor() as cur:
                    # 测试 SELECT
                    cur.execute(f'SELECT COUNT(*) FROM {schema}."{temp_table}"')
                    count = cur.fetchone()[0]
                    assert count == 0, "新表应为空"
                    
                    # 测试 INSERT
                    cur.execute(f"""
                        INSERT INTO {schema}."{temp_table}" (name) 
                        VALUES ('test')
                        RETURNING id
                    """)
                    inserted_id = cur.fetchone()[0]
                    assert inserted_id > 0, "应能插入数据"
                    
                    svc_conn.rollback()
            finally:
                svc_conn.close()
        finally:
            # 清理
            admin_conn = psycopg.connect(admin_dsn, autocommit=True)
            with admin_conn.cursor() as cur:
                cur.execute(f'DROP TABLE IF EXISTS {schema}."{temp_table}" CASCADE')
            admin_conn.close()
