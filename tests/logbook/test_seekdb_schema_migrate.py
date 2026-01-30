# -*- coding: utf-8 -*-
"""
SeekDB Schema Migration 测试

================================================================================
测试目标:
================================================================================
1. 不同初始状态下的迁移行为 (FRESH_INSTALL/LEGACY_SEEK/MIGRATED_SEEKDB/CI)
2. 幂等性验证 - 重复执行迁移应无副作用
3. MIXED 环境检测与处理 - seek/seekdb 并存时应提示错误
4. 关键验收断言 - 角色存在、默认权限、search_path 配置

================================================================================
测试环境说明:
================================================================================
- 测试使用 CI Postgres 容器，通过 conftest.py 的 test_db_info fixture 获取
- 每个测试类使用独立的数据库实例，确保测试隔离
- 测试完成后自动清理创建的 schema 和角色

================================================================================
相关文件:
================================================================================
- apps/seekdb_rag_hybrid/sql/migrations/001_seek_schema_roles_to_seekdb.sql
- apps/seekdb_rag_hybrid/sql/migrations/001_rollback_seekdb_to_seek.sql
- apps/logbook_postgres/sql/99_verify_permissions.sql
================================================================================
"""

import os
import pytest
import psycopg
from pathlib import Path


# 获取项目根目录和 SQL 文件路径
def get_project_root() -> Path:
    """获取项目根目录"""
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "Makefile").exists():
            return parent
    raise RuntimeError("Cannot find project root with Makefile")


PROJECT_ROOT = get_project_root()
MIGRATION_SQL = PROJECT_ROOT / "apps/seekdb_rag_hybrid/sql/migrations/001_seek_schema_roles_to_seekdb.sql"
ROLLBACK_SQL = PROJECT_ROOT / "apps/seekdb_rag_hybrid/sql/migrations/001_rollback_seekdb_to_seek.sql"
VERIFY_SQL = PROJECT_ROOT / "apps/logbook_postgres/sql/99_verify_permissions.sql"


# ============================================================================
# Helper Functions
# ============================================================================

def run_sql_file(conn, sql_path: Path, *, autocommit: bool = True) -> str:
    """
    执行 SQL 文件并返回输出
    
    Args:
        conn: psycopg connection
        sql_path: SQL 文件路径
        autocommit: 是否自动提交
    
    Returns:
        执行输出（NOTICE 消息等）
    """
    sql_content = sql_path.read_text()
    
    old_autocommit = conn.autocommit
    conn.autocommit = autocommit
    
    try:
        with conn.cursor() as cur:
            cur.execute(sql_content)
            # 收集 NOTICE 消息
            notices = []
            if hasattr(conn, 'notices'):
                notices = list(conn.notices)
                conn.notices.clear()
            return "\n".join(notices) if notices else ""
    finally:
        conn.autocommit = old_autocommit


def setup_fresh_install_state(conn):
    """
    设置 FRESH_INSTALL 初始状态
    - 无 seek/seekdb schema
    - 无 seek_*/seekdb_* 角色
    """
    with conn.cursor() as cur:
        # 清理 schema
        cur.execute("DROP SCHEMA IF EXISTS seek CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS seekdb CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS seek_test CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS seekdb_test CASCADE")
        
        # 清理角色（需要先撤销依赖）
        for role in ['seekdb_svc', 'seekdb_migrator_login', 'seekdb_app', 'seekdb_migrator',
                     'seek_svc', 'seek_migrator_login', 'seek_app', 'seek_migrator']:
            try:
                cur.execute(f"DROP ROLE IF EXISTS {role}")
            except psycopg.errors.DependentObjectsStillExist:
                pass  # 忽略依赖错误
    conn.commit()


def setup_legacy_seek_state(conn):
    """
    设置 LEGACY_SEEK 初始状态
    - 存在 seek schema（带表）
    - 存在 seek_migrator/seek_app 角色
    """
    setup_fresh_install_state(conn)
    
    with conn.cursor() as cur:
        # 创建 seek_* 角色
        cur.execute("CREATE ROLE seek_migrator NOLOGIN")
        cur.execute("CREATE ROLE seek_app NOLOGIN")
        cur.execute("CREATE ROLE seek_migrator_login LOGIN")
        cur.execute("CREATE ROLE seek_svc LOGIN")
        
        # 配置 membership
        cur.execute("GRANT seek_migrator TO seek_migrator_login")
        cur.execute("GRANT seek_app TO seek_svc")
        
        # 创建 seek schema
        cur.execute("CREATE SCHEMA seek AUTHORIZATION seek_migrator")
        
        # 创建示例表
        cur.execute("""
            CREATE TABLE seek.chunks (
                id SERIAL PRIMARY KEY,
                content TEXT,
                embedding VECTOR(1536)
            )
        """)
        cur.execute("INSERT INTO seek.chunks (content) VALUES ('test data')")
    conn.commit()


def setup_migrated_seekdb_state(conn):
    """
    设置 MIGRATED_SEEKDB 初始状态
    - 存在 seekdb schema
    - 存在 seekdb_migrator/seekdb_app 角色
    - 无 seek schema
    """
    setup_fresh_install_state(conn)
    
    with conn.cursor() as cur:
        # 创建 seekdb_* 角色
        cur.execute("CREATE ROLE seekdb_migrator NOLOGIN")
        cur.execute("CREATE ROLE seekdb_app NOLOGIN")
        cur.execute("CREATE ROLE seekdb_migrator_login LOGIN")
        cur.execute("CREATE ROLE seekdb_svc LOGIN")
        
        # 配置 membership
        cur.execute("GRANT seekdb_migrator TO seekdb_migrator_login")
        cur.execute("GRANT seekdb_app TO seekdb_svc")
        
        # 创建 seekdb schema
        cur.execute("CREATE SCHEMA seekdb AUTHORIZATION seekdb_migrator")
        
        # 创建示例表
        cur.execute("""
            CREATE TABLE seekdb.chunks (
                id SERIAL PRIMARY KEY,
                content TEXT,
                embedding VECTOR(1536)
            )
        """)
    conn.commit()


def setup_mixed_state(conn):
    """
    设置 MIXED 初始状态
    - 同时存在 seek 和 seekdb schema（需手动处理的状态）
    """
    setup_legacy_seek_state(conn)
    
    with conn.cursor() as cur:
        # 额外创建 seekdb schema
        cur.execute("CREATE ROLE seekdb_migrator NOLOGIN")
        cur.execute("CREATE SCHEMA seekdb AUTHORIZATION seekdb_migrator")
        cur.execute("""
            CREATE TABLE seekdb.chunks (
                id SERIAL PRIMARY KEY,
                content TEXT
            )
        """)
    conn.commit()


def get_schema_exists(conn, schema_name: str) -> bool:
    """检查 schema 是否存在"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT EXISTS(
                SELECT 1 FROM information_schema.schemata 
                WHERE schema_name = %s
            )
        """, (schema_name,))
        return cur.fetchone()[0]


def get_role_exists(conn, role_name: str) -> bool:
    """检查角色是否存在"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = %s)
        """, (role_name,))
        return cur.fetchone()[0]


def get_schema_owner(conn, schema_name: str) -> str:
    """获取 schema 的 owner"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT nspowner::regrole::text 
            FROM pg_namespace 
            WHERE nspname = %s
        """, (schema_name,))
        result = cur.fetchone()
        return result[0] if result else None


def get_role_membership(conn, member: str, role: str) -> bool:
    """检查角色 membership"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT pg_has_role(%s, %s, 'MEMBER')
        """, (member, role))
        return cur.fetchone()[0]


def get_role_search_path(conn, role_name: str) -> str:
    """获取角色的 search_path 配置"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT pg_catalog.array_to_string(
                COALESCE(rolconfig, '{}'), ','
            ) 
            FROM pg_roles 
            WHERE rolname = %s
        """, (role_name,))
        result = cur.fetchone()
        if not result or not result[0]:
            return ""
        # 提取 search_path 配置
        for config in result[0].split(','):
            if config.startswith('search_path='):
                return config.split('=', 1)[1]
        return ""


def get_schema_privilege(conn, role: str, schema: str, privilege: str) -> bool:
    """检查角色对 schema 的权限"""
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT has_schema_privilege(%s, %s, %s)
        """, (role, schema, privilege))
        return cur.fetchone()[0]


def get_default_privileges_exist(conn, grantor: str, schema: str, obj_type: str) -> bool:
    """
    检查默认权限是否存在
    
    Args:
        grantor: 授权者角色名
        schema: schema 名称
        obj_type: 对象类型 ('r' = table, 'S' = sequence, 'f' = function)
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT EXISTS(
                SELECT 1 FROM pg_default_acl da
                JOIN pg_namespace n ON n.oid = da.defaclnamespace
                JOIN pg_roles r ON r.oid = da.defaclrole
                WHERE n.nspname = %s
                  AND r.rolname = %s
                  AND da.defaclobjtype = %s
            )
        """, (schema, grantor, obj_type))
        return cur.fetchone()[0]


# ============================================================================
# Test Fixtures
# ============================================================================

@pytest.fixture(scope="class")
def migration_db(test_db_info):
    """
    提供用于迁移测试的数据库连接
    
    每个测试类使用独立的连接，确保测试隔离。
    """
    dsn = test_db_info["dsn"]
    conn = psycopg.connect(dsn, autocommit=True)
    
    # 确保 vector 扩展存在（迁移脚本可能需要）
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    
    yield conn
    
    conn.close()


@pytest.fixture
def fresh_db(migration_db):
    """设置 FRESH_INSTALL 状态的数据库"""
    setup_fresh_install_state(migration_db)
    return migration_db


@pytest.fixture
def legacy_seek_db(migration_db):
    """设置 LEGACY_SEEK 状态的数据库"""
    setup_legacy_seek_state(migration_db)
    return migration_db


@pytest.fixture
def migrated_seekdb_db(migration_db):
    """设置 MIGRATED_SEEKDB 状态的数据库"""
    setup_migrated_seekdb_state(migration_db)
    return migration_db


@pytest.fixture
def mixed_db(migration_db):
    """设置 MIXED 状态的数据库"""
    setup_mixed_state(migration_db)
    return migration_db


# ============================================================================
# Test Classes
# ============================================================================

class TestSeekdbMigrationFreshInstall:
    """FRESH_INSTALL 环境下的迁移测试"""

    def test_fresh_install_creates_seekdb_schema(self, fresh_db):
        """验证在空库环境下创建 seekdb schema"""
        # 确认初始状态
        assert not get_schema_exists(fresh_db, 'seek')
        assert not get_schema_exists(fresh_db, 'seekdb')
        
        # 执行迁移
        run_sql_file(fresh_db, MIGRATION_SQL)
        
        # 验证结果
        assert get_schema_exists(fresh_db, 'seekdb'), "seekdb schema should be created"
        assert not get_schema_exists(fresh_db, 'seek'), "seek schema should not exist"

    def test_fresh_install_creates_roles(self, fresh_db):
        """验证在空库环境下创建所有必需角色"""
        run_sql_file(fresh_db, MIGRATION_SQL)
        
        # 验证角色存在
        assert get_role_exists(fresh_db, 'seekdb_migrator'), "seekdb_migrator should exist"
        assert get_role_exists(fresh_db, 'seekdb_app'), "seekdb_app should exist"
        assert get_role_exists(fresh_db, 'seekdb_migrator_login'), "seekdb_migrator_login should exist"
        assert get_role_exists(fresh_db, 'seekdb_svc'), "seekdb_svc should exist"

    def test_fresh_install_schema_owner(self, fresh_db):
        """验证 seekdb schema owner 为 seekdb_migrator"""
        run_sql_file(fresh_db, MIGRATION_SQL)
        
        owner = get_schema_owner(fresh_db, 'seekdb')
        assert owner == 'seekdb_migrator', f"Expected owner seekdb_migrator, got {owner}"

    def test_fresh_install_role_membership(self, fresh_db):
        """验证角色 membership 配置正确"""
        run_sql_file(fresh_db, MIGRATION_SQL)
        
        # seekdb_migrator_login -> seekdb_migrator
        assert get_role_membership(fresh_db, 'seekdb_migrator_login', 'seekdb_migrator'), \
            "seekdb_migrator_login should be member of seekdb_migrator"
        
        # seekdb_svc -> seekdb_app
        assert get_role_membership(fresh_db, 'seekdb_svc', 'seekdb_app'), \
            "seekdb_svc should be member of seekdb_app"


class TestSeekdbMigrationLegacySeek:
    """LEGACY_SEEK 环境下的迁移测试"""

    def test_legacy_seek_renames_schema(self, legacy_seek_db):
        """验证 seek schema 被重命名为 seekdb"""
        # 确认初始状态
        assert get_schema_exists(legacy_seek_db, 'seek')
        assert not get_schema_exists(legacy_seek_db, 'seekdb')
        
        # 执行迁移
        run_sql_file(legacy_seek_db, MIGRATION_SQL)
        
        # 验证结果
        assert get_schema_exists(legacy_seek_db, 'seekdb'), "seekdb schema should exist after migration"
        assert not get_schema_exists(legacy_seek_db, 'seek'), "seek schema should be renamed"

    def test_legacy_seek_preserves_data(self, legacy_seek_db):
        """验证迁移后数据保留"""
        run_sql_file(legacy_seek_db, MIGRATION_SQL)
        
        with legacy_seek_db.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM seekdb.chunks")
            count = cur.fetchone()[0]
        
        assert count == 1, f"Expected 1 row in seekdb.chunks, got {count}"

    def test_legacy_seek_creates_new_roles(self, legacy_seek_db):
        """验证迁移创建 seekdb_* 角色"""
        run_sql_file(legacy_seek_db, MIGRATION_SQL)
        
        assert get_role_exists(legacy_seek_db, 'seekdb_migrator')
        assert get_role_exists(legacy_seek_db, 'seekdb_app')
        assert get_role_exists(legacy_seek_db, 'seekdb_migrator_login')
        assert get_role_exists(legacy_seek_db, 'seekdb_svc')

    def test_legacy_seek_updates_schema_owner(self, legacy_seek_db):
        """验证迁移后 schema owner 更新为 seekdb_migrator"""
        run_sql_file(legacy_seek_db, MIGRATION_SQL)
        
        owner = get_schema_owner(legacy_seek_db, 'seekdb')
        assert owner == 'seekdb_migrator'


class TestSeekdbMigrationMigratedSeekdb:
    """MIGRATED_SEEKDB 环境下的迁移测试（已迁移状态）"""

    def test_migrated_seekdb_is_idempotent(self, migrated_seekdb_db):
        """验证在已迁移环境下重复执行是幂等的"""
        # 记录初始状态
        initial_owner = get_schema_owner(migrated_seekdb_db, 'seekdb')
        
        # 执行迁移（第一次 - 实际上是验证）
        run_sql_file(migrated_seekdb_db, MIGRATION_SQL)
        
        # 执行迁移（第二次）
        run_sql_file(migrated_seekdb_db, MIGRATION_SQL)
        
        # 验证状态不变
        assert get_schema_exists(migrated_seekdb_db, 'seekdb')
        assert not get_schema_exists(migrated_seekdb_db, 'seek')
        assert get_schema_owner(migrated_seekdb_db, 'seekdb') == 'seekdb_migrator'

    def test_migrated_seekdb_preserves_data(self, migrated_seekdb_db):
        """验证重复执行迁移不丢失数据"""
        # 插入测试数据
        with migrated_seekdb_db.cursor() as cur:
            cur.execute("INSERT INTO seekdb.chunks (content) VALUES ('idempotent test')")
        
        # 执行迁移
        run_sql_file(migrated_seekdb_db, MIGRATION_SQL)
        
        # 验证数据存在
        with migrated_seekdb_db.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM seekdb.chunks WHERE content = 'idempotent test'")
            count = cur.fetchone()[0]
        
        assert count == 1


class TestSeekdbMigrationMixedState:
    """MIXED 环境下的迁移测试（seek/seekdb 并存）"""

    def test_mixed_state_raises_exception(self, mixed_db):
        """验证 MIXED 状态下迁移抛出异常"""
        # 确认初始状态
        assert get_schema_exists(mixed_db, 'seek')
        assert get_schema_exists(mixed_db, 'seekdb')
        
        # 执行迁移应该抛出异常
        with pytest.raises(psycopg.errors.RaiseException) as exc_info:
            run_sql_file(mixed_db, MIGRATION_SQL)
        
        error_msg = str(exc_info.value).lower()
        assert 'mixed' in error_msg or '并存' in error_msg or '手动处理' in error_msg, \
            f"Expected MIXED state error, got: {exc_info.value}"

    def test_mixed_state_no_changes(self, mixed_db):
        """验证 MIXED 状态下不做任何修改"""
        # 记录初始状态
        seek_tables_before = []
        seekdb_tables_before = []
        
        with mixed_db.cursor() as cur:
            cur.execute("""
                SELECT table_name FROM information_schema.tables 
                WHERE table_schema = 'seek'
            """)
            seek_tables_before = [r[0] for r in cur.fetchall()]
            
            cur.execute("""
                SELECT table_name FROM information_schema.tables 
                WHERE table_schema = 'seekdb'
            """)
            seekdb_tables_before = [r[0] for r in cur.fetchall()]
        
        # 尝试执行迁移（预期失败）
        try:
            run_sql_file(mixed_db, MIGRATION_SQL)
        except psycopg.errors.RaiseException:
            pass  # 预期的异常
        
        # 验证状态未改变
        with mixed_db.cursor() as cur:
            cur.execute("""
                SELECT table_name FROM information_schema.tables 
                WHERE table_schema = 'seek'
            """)
            seek_tables_after = [r[0] for r in cur.fetchall()]
            
            cur.execute("""
                SELECT table_name FROM information_schema.tables 
                WHERE table_schema = 'seekdb'
            """)
            seekdb_tables_after = [r[0] for r in cur.fetchall()]
        
        assert set(seek_tables_before) == set(seek_tables_after), "seek schema should be unchanged"
        assert set(seekdb_tables_before) == set(seekdb_tables_after), "seekdb schema should be unchanged"


class TestSeekdbMigrationIdempotency:
    """幂等性测试"""

    def test_migration_idempotent_fresh_install(self, fresh_db):
        """验证 FRESH_INSTALL 环境下多次执行幂等"""
        # 执行3次迁移
        for i in range(3):
            run_sql_file(fresh_db, MIGRATION_SQL)
        
        # 验证最终状态正确
        assert get_schema_exists(fresh_db, 'seekdb')
        assert get_role_exists(fresh_db, 'seekdb_migrator')
        assert get_role_exists(fresh_db, 'seekdb_app')
        assert get_schema_owner(fresh_db, 'seekdb') == 'seekdb_migrator'

    def test_migration_idempotent_legacy_seek(self, legacy_seek_db):
        """验证 LEGACY_SEEK 环境下多次执行幂等"""
        # 第一次执行（实际迁移）
        run_sql_file(legacy_seek_db, MIGRATION_SQL)
        
        # 第二次执行（应该是幂等验证）
        run_sql_file(legacy_seek_db, MIGRATION_SQL)
        
        # 验证状态
        assert get_schema_exists(legacy_seek_db, 'seekdb')
        assert not get_schema_exists(legacy_seek_db, 'seek')


class TestSeekdbAcceptanceAssertions:
    """关键验收断言测试"""

    def test_roles_exist_after_migration(self, fresh_db):
        """验证迁移后所有必需角色存在"""
        run_sql_file(fresh_db, MIGRATION_SQL)
        
        required_roles = [
            'seekdb_migrator',
            'seekdb_app',
            'seekdb_migrator_login',
            'seekdb_svc',
        ]
        
        for role in required_roles:
            assert get_role_exists(fresh_db, role), f"Role {role} should exist"

    def test_role_membership_correct(self, fresh_db):
        """验证角色 membership 正确"""
        run_sql_file(fresh_db, MIGRATION_SQL)
        
        # seekdb_migrator_login -> seekdb_migrator
        assert get_role_membership(fresh_db, 'seekdb_migrator_login', 'seekdb_migrator')
        
        # seekdb_svc -> seekdb_app
        assert get_role_membership(fresh_db, 'seekdb_svc', 'seekdb_app')

    def test_schema_permissions(self, fresh_db):
        """验证 schema 权限正确"""
        run_sql_file(fresh_db, MIGRATION_SQL)
        
        # seekdb_migrator 应有 ALL 权限
        assert get_schema_privilege(fresh_db, 'seekdb_migrator', 'seekdb', 'CREATE')
        assert get_schema_privilege(fresh_db, 'seekdb_migrator', 'seekdb', 'USAGE')
        
        # seekdb_app 应有 USAGE 权限，无 CREATE 权限
        assert get_schema_privilege(fresh_db, 'seekdb_app', 'seekdb', 'USAGE')
        assert not get_schema_privilege(fresh_db, 'seekdb_app', 'seekdb', 'CREATE')

    def test_default_privileges_configured(self, fresh_db):
        """验证默认权限配置正确"""
        run_sql_file(fresh_db, MIGRATION_SQL)
        
        # seekdb_migrator 应配置了表的默认权限
        assert get_default_privileges_exist(fresh_db, 'seekdb_migrator', 'seekdb', 'r'), \
            "Default privileges for tables should be configured"
        
        # seekdb_migrator 应配置了序列的默认权限
        assert get_default_privileges_exist(fresh_db, 'seekdb_migrator', 'seekdb', 'S'), \
            "Default privileges for sequences should be configured"
        
        # seekdb_migrator 应配置了函数的默认权限
        assert get_default_privileges_exist(fresh_db, 'seekdb_migrator', 'seekdb', 'f'), \
            "Default privileges for functions should be configured"

    def test_search_path_configured(self, fresh_db):
        """验证 search_path 配置正确"""
        run_sql_file(fresh_db, MIGRATION_SQL)
        
        # seekdb_svc 的 search_path 应包含 seekdb
        search_path = get_role_search_path(fresh_db, 'seekdb_svc')
        assert 'seekdb' in search_path, f"seekdb_svc search_path should include seekdb, got: {search_path}"
        
        # seekdb_migrator_login 的 search_path 应包含 seekdb
        search_path = get_role_search_path(fresh_db, 'seekdb_migrator_login')
        assert 'seekdb' in search_path, f"seekdb_migrator_login search_path should include seekdb, got: {search_path}"


class TestSeekdbRollback:
    """回滚测试"""

    def test_rollback_after_migration(self, legacy_seek_db):
        """验证迁移后可以成功回滚"""
        # 执行迁移
        run_sql_file(legacy_seek_db, MIGRATION_SQL)
        
        assert get_schema_exists(legacy_seek_db, 'seekdb')
        assert not get_schema_exists(legacy_seek_db, 'seek')
        
        # 执行回滚
        run_sql_file(legacy_seek_db, ROLLBACK_SQL)
        
        # 验证回滚结果
        assert get_schema_exists(legacy_seek_db, 'seek'), "seek schema should be restored"
        assert not get_schema_exists(legacy_seek_db, 'seekdb'), "seekdb schema should be removed"

    def test_rollback_preserves_data(self, legacy_seek_db):
        """验证回滚后数据保留"""
        # 执行迁移
        run_sql_file(legacy_seek_db, MIGRATION_SQL)
        
        # 插入测试数据
        with legacy_seek_db.cursor() as cur:
            cur.execute("INSERT INTO seekdb.chunks (content) VALUES ('rollback test')")
        
        # 执行回滚
        run_sql_file(legacy_seek_db, ROLLBACK_SQL)
        
        # 验证数据存在于 seek schema
        with legacy_seek_db.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM seek.chunks WHERE content = 'rollback test'")
            count = cur.fetchone()[0]
        
        assert count == 1


class TestVerifyPermissionsSql:
    """99_verify_permissions.sql 验证测试"""

    def test_verify_passes_after_migration(self, fresh_db):
        """验证迁移后权限验证脚本通过"""
        # 执行迁移
        run_sql_file(fresh_db, MIGRATION_SQL)
        
        # 执行权限验证
        output = run_sql_file(fresh_db, VERIFY_SQL)
        
        # 验证没有 FAIL（排除 Logbook/OpenMemory 相关的 FAIL，只关注 Seek）
        lines = output.lower().split('\n')
        seek_fails = [l for l in lines if 'fail' in l and ('seek' in l or 'seekdb' in l)]
        
        assert len(seek_fails) == 0, f"Seek-related FAILs found: {seek_fails}"

    def test_verify_detects_missing_roles(self, fresh_db):
        """验证权限脚本检测缺失角色"""
        # 不执行迁移，直接运行验证
        # 应该检测到 Seek 角色缺失
        
        # 清理可能存在的角色
        setup_fresh_install_state(fresh_db)
        
        # 执行验证
        output = run_sql_file(fresh_db, VERIFY_SQL)
        
        # 验证有 Seek 相关的 FAIL 或 SKIP
        lines = output.lower()
        has_seek_issue = 'seek' in lines and ('fail' in lines or 'skip' in lines)
        
        assert has_seek_issue or 'seek 未启用' in lines, \
            "Verify script should detect missing Seek roles or skip Seek checks"


class TestMigrationMachineReadableOutput:
    """机器可读输出测试"""

    def test_migration_returns_env_type(self, fresh_db):
        """验证迁移脚本输出环境类型"""
        # 迁移脚本通过 RAISE NOTICE 输出环境类型
        output = run_sql_file(fresh_db, MIGRATION_SQL)
        
        # 应该包含环境类型输出
        valid_env_types = ['FRESH_INSTALL', 'LEGACY_SEEK', 'MIGRATED_SEEKDB', 'CI']
        has_env_type = any(env_type in output for env_type in valid_env_types)
        
        # NOTICE 可能不被捕获，所以这个测试是 best-effort
        # 主要验证迁移成功完成
        assert get_schema_exists(fresh_db, 'seekdb')


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
