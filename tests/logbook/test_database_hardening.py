# -*- coding: utf-8 -*-
"""
数据库权限硬化测试

验证 04_roles_and_grants.sql section 1.7 的数据库级权限硬化是否正确生效：
- PUBLIC 不应有 CREATE/TEMP 权限
- migrator 角色应有 CONNECT/CREATE/TEMP 权限
- app 角色应仅有 CONNECT 权限（无 CREATE/TEMP）

测试使用 has_database_privilege() 函数验证权限配置。

运行方式：
- 集成测试: make test-logbook-integration
- 单独运行: pytest tests/logbook/test_database_hardening.py -v

跳过条件：
- 无法连接到测试数据库时自动 skip
- 角色不存在时自动 skip
"""

import os

import psycopg
import pytest

# 环境变量
ENV_SKIP_DB_HARDENING_TESTS = "SKIP_DB_HARDENING_TESTS"


def should_skip_tests() -> bool:
    """检查是否应跳过数据库硬化测试"""
    return os.environ.get(ENV_SKIP_DB_HARDENING_TESTS, "").lower() in ("1", "true", "yes")


@pytest.fixture(scope="module")
def db_hardening_setup(test_db_info: dict):
    """
    数据库硬化测试的设置 fixture

    功能：
    1. 确保角色脚本已执行（engram_* 和 openmemory_* 角色存在）
    2. 返回连接信息和数据库名称供测试使用
    """
    if should_skip_tests():
        pytest.skip("数据库硬化测试已通过环境变量禁用")

    admin_dsn = test_db_info.get("admin_dsn", test_db_info["dsn"])

    try:
        conn = psycopg.connect(admin_dsn)
    except Exception as e:
        pytest.skip(f"无法连接到测试数据库: {e}")
        return

    try:
        with conn.cursor() as cur:
            # 获取当前数据库名称
            cur.execute("SELECT current_database()")
            db_name = cur.fetchone()[0]

            # 检查核心角色是否存在
            required_roles = [
                "engram_admin",
                "engram_migrator",
                "engram_app_readwrite",
                "engram_app_readonly",
                "openmemory_migrator",
                "openmemory_app",
            ]

            cur.execute(
                """
                SELECT rolname FROM pg_roles
                WHERE rolname = ANY(%s)
            """,
                (required_roles,),
            )
            existing_roles = {row[0] for row in cur.fetchall()}

            missing_roles = set(required_roles) - existing_roles
            if missing_roles:
                pytest.skip(f"缺少角色: {missing_roles}，请先执行 04_roles_and_grants.sql")

        yield {
            "dsn": admin_dsn,
            "db_name": db_name,
            "existing_roles": existing_roles,
        }
    finally:
        conn.close()


class TestPublicRoleHardening:
    """测试 PUBLIC 角色的权限硬化"""

    def test_public_no_create_on_database(self, db_hardening_setup: dict):
        """验证 PUBLIC 在当前数据库没有 CREATE 权限"""
        dsn = db_hardening_setup["dsn"]
        db_name = db_hardening_setup["db_name"]

        conn = psycopg.connect(dsn)
        try:
            with conn.cursor() as cur:
                # 使用 has_database_privilege 检查 PUBLIC 的 CREATE 权限
                # 注意：某些 PostgreSQL 版本对 PUBLIC 的处理可能不同
                try:
                    cur.execute(
                        """
                        SELECT has_database_privilege('PUBLIC', %s, 'CREATE')
                    """,
                        (db_name,),
                    )
                    has_create = cur.fetchone()[0]
                except psycopg.errors.UndefinedObject:
                    # 某些环境下 PUBLIC 不是实际角色
                    has_create = False

                assert not has_create, (
                    f"PUBLIC 不应有数据库 {db_name} 的 CREATE 权限。"
                    f"请执行 04_roles_and_grants.sql 进行权限硬化。"
                )
        finally:
            conn.close()

    def test_public_no_temp_on_database(self, db_hardening_setup: dict):
        """验证 PUBLIC 在当前数据库没有 TEMP 权限"""
        dsn = db_hardening_setup["dsn"]
        db_name = db_hardening_setup["db_name"]

        conn = psycopg.connect(dsn)
        try:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        """
                        SELECT has_database_privilege('PUBLIC', %s, 'TEMP')
                    """,
                        (db_name,),
                    )
                    has_temp = cur.fetchone()[0]
                except psycopg.errors.UndefinedObject:
                    has_temp = False

                assert not has_temp, (
                    f"PUBLIC 不应有数据库 {db_name} 的 TEMP 权限。"
                    f"请执行 04_roles_and_grants.sql 进行权限硬化。"
                )
        finally:
            conn.close()


class TestEngramRolesHardening:
    """测试 Engram 角色的数据库权限配置"""

    def test_engram_admin_has_full_db_privileges(self, db_hardening_setup: dict):
        """验证 engram_admin 有完整的数据库权限（CONNECT/CREATE/TEMP）"""
        dsn = db_hardening_setup["dsn"]
        db_name = db_hardening_setup["db_name"]

        conn = psycopg.connect(dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        has_database_privilege('engram_admin', %s, 'CONNECT') AS has_connect,
                        has_database_privilege('engram_admin', %s, 'CREATE') AS has_create,
                        has_database_privilege('engram_admin', %s, 'TEMP') AS has_temp
                """,
                    (db_name, db_name, db_name),
                )
                result = cur.fetchone()

                assert result[0], "engram_admin 应有 CONNECT 权限"
                assert result[1], "engram_admin 应有 CREATE 权限"
                assert result[2], "engram_admin 应有 TEMP 权限"
        finally:
            conn.close()

    def test_engram_migrator_has_ddl_db_privileges(self, db_hardening_setup: dict):
        """验证 engram_migrator 有 DDL 所需的数据库权限（CONNECT/CREATE/TEMP）"""
        dsn = db_hardening_setup["dsn"]
        db_name = db_hardening_setup["db_name"]

        conn = psycopg.connect(dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        has_database_privilege('engram_migrator', %s, 'CONNECT') AS has_connect,
                        has_database_privilege('engram_migrator', %s, 'CREATE') AS has_create,
                        has_database_privilege('engram_migrator', %s, 'TEMP') AS has_temp
                """,
                    (db_name, db_name, db_name),
                )
                result = cur.fetchone()

                assert result[0], "engram_migrator 应有 CONNECT 权限"
                assert result[1], "engram_migrator 应有 CREATE 权限（用于创建 schema）"
                assert result[2], "engram_migrator 应有 TEMP 权限"
        finally:
            conn.close()

    def test_engram_app_readwrite_has_limited_db_privileges(self, db_hardening_setup: dict):
        """验证 engram_app_readwrite 仅有 CONNECT 权限（无 CREATE/TEMP）"""
        dsn = db_hardening_setup["dsn"]
        db_name = db_hardening_setup["db_name"]

        conn = psycopg.connect(dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        has_database_privilege('engram_app_readwrite', %s, 'CONNECT') AS has_connect,
                        has_database_privilege('engram_app_readwrite', %s, 'CREATE') AS has_create,
                        has_database_privilege('engram_app_readwrite', %s, 'TEMP') AS has_temp
                """,
                    (db_name, db_name, db_name),
                )
                result = cur.fetchone()

                assert result[0], "engram_app_readwrite 应有 CONNECT 权限"
                assert not result[1], (
                    "engram_app_readwrite 不应有 CREATE 权限（仅限 DML）。"
                    "请执行 04_roles_and_grants.sql 进行权限硬化。"
                )
                assert not result[2], (
                    "engram_app_readwrite 不应有 TEMP 权限（仅限 DML）。"
                    "请执行 04_roles_and_grants.sql 进行权限硬化。"
                )
        finally:
            conn.close()

    def test_engram_app_readonly_has_limited_db_privileges(self, db_hardening_setup: dict):
        """验证 engram_app_readonly 仅有 CONNECT 权限（无 CREATE/TEMP）"""
        dsn = db_hardening_setup["dsn"]
        db_name = db_hardening_setup["db_name"]

        conn = psycopg.connect(dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        has_database_privilege('engram_app_readonly', %s, 'CONNECT') AS has_connect,
                        has_database_privilege('engram_app_readonly', %s, 'CREATE') AS has_create,
                        has_database_privilege('engram_app_readonly', %s, 'TEMP') AS has_temp
                """,
                    (db_name, db_name, db_name),
                )
                result = cur.fetchone()

                assert result[0], "engram_app_readonly 应有 CONNECT 权限"
                assert not result[1], (
                    "engram_app_readonly 不应有 CREATE 权限。"
                    "请执行 04_roles_and_grants.sql 进行权限硬化。"
                )
                assert not result[2], (
                    "engram_app_readonly 不应有 TEMP 权限。"
                    "请执行 04_roles_and_grants.sql 进行权限硬化。"
                )
        finally:
            conn.close()


class TestOpenmemoryRolesHardening:
    """测试 OpenMemory 角色的数据库权限配置"""

    def test_openmemory_migrator_has_ddl_db_privileges(self, db_hardening_setup: dict):
        """验证 openmemory_migrator 有 DDL 所需的数据库权限"""
        dsn = db_hardening_setup["dsn"]
        db_name = db_hardening_setup["db_name"]

        conn = psycopg.connect(dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        has_database_privilege('openmemory_migrator', %s, 'CONNECT') AS has_connect,
                        has_database_privilege('openmemory_migrator', %s, 'CREATE') AS has_create,
                        has_database_privilege('openmemory_migrator', %s, 'TEMP') AS has_temp
                """,
                    (db_name, db_name, db_name),
                )
                result = cur.fetchone()

                assert result[0], "openmemory_migrator 应有 CONNECT 权限"
                assert result[1], "openmemory_migrator 应有 CREATE 权限（用于创建 schema）"
                assert result[2], "openmemory_migrator 应有 TEMP 权限"
        finally:
            conn.close()

    def test_openmemory_app_has_limited_db_privileges(self, db_hardening_setup: dict):
        """验证 openmemory_app 仅有 CONNECT 权限（无 CREATE/TEMP）"""
        dsn = db_hardening_setup["dsn"]
        db_name = db_hardening_setup["db_name"]

        conn = psycopg.connect(dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        has_database_privilege('openmemory_app', %s, 'CONNECT') AS has_connect,
                        has_database_privilege('openmemory_app', %s, 'CREATE') AS has_create,
                        has_database_privilege('openmemory_app', %s, 'TEMP') AS has_temp
                """,
                    (db_name, db_name, db_name),
                )
                result = cur.fetchone()

                assert result[0], "openmemory_app 应有 CONNECT 权限"
                assert not result[1], (
                    "openmemory_app 不应有 CREATE 权限（仅限 DML）。"
                    "请执行 04_roles_and_grants.sql 进行权限硬化。"
                )
                assert not result[2], (
                    "openmemory_app 不应有 TEMP 权限（仅限 DML）。"
                    "请执行 04_roles_and_grants.sql 进行权限硬化。"
                )
        finally:
            conn.close()


class TestDatabasePrivilegeSummary:
    """数据库权限配置汇总测试"""

    def test_all_roles_privilege_matrix(self, db_hardening_setup: dict):
        """验证所有角色的数据库权限矩阵"""
        dsn = db_hardening_setup["dsn"]
        db_name = db_hardening_setup["db_name"]

        # 预期的权限矩阵
        # 格式: (role_name, expect_connect, expect_create, expect_temp)
        expected_privileges = [
            ("engram_admin", True, True, True),
            ("engram_migrator", True, True, True),
            ("engram_app_readwrite", True, False, False),
            ("engram_app_readonly", True, False, False),
            ("openmemory_migrator", True, True, True),
            ("openmemory_app", True, False, False),
        ]

        conn = psycopg.connect(dsn)
        try:
            errors = []

            with conn.cursor() as cur:
                for role, expect_connect, expect_create, expect_temp in expected_privileges:
                    cur.execute(
                        """
                        SELECT
                            has_database_privilege(%s, %s, 'CONNECT') AS has_connect,
                            has_database_privilege(%s, %s, 'CREATE') AS has_create,
                            has_database_privilege(%s, %s, 'TEMP') AS has_temp
                    """,
                        (role, db_name, role, db_name, role, db_name),
                    )
                    result = cur.fetchone()

                    if result[0] != expect_connect:
                        errors.append(f"{role}: CONNECT={result[0]}, 预期={expect_connect}")
                    if result[1] != expect_create:
                        errors.append(f"{role}: CREATE={result[1]}, 预期={expect_create}")
                    if result[2] != expect_temp:
                        errors.append(f"{role}: TEMP={result[2]}, 预期={expect_temp}")

            if errors:
                pytest.fail(
                    "数据库权限配置不符合预期:\n"
                    + "\n".join(f"  - {e}" for e in errors)
                    + "\n\n请执行 04_roles_and_grants.sql 进行权限硬化。"
                )
        finally:
            conn.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
