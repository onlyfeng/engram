# -*- coding: utf-8 -*-
"""
pytest 共享 fixtures

提供:
- PostgreSQL 临时数据库 + 自动建表（隔离测试环境）
- 支持 pytest-xdist 并行测试（每个 worker 独立数据库）
- 可复用的 migrated_db fixture
- Mock 配置
- 角色权限测试支持

================================================================================
架构约束（路线A - 多库方案）:
--------------------------------------------------------------------------------
测试隔离策略: 每个测试会话使用独立数据库（多库隔离）
- 数据库名格式: engram_test_<uuid>
- 每个 xdist worker 使用独立数据库
- 不再使用 schema_prefix 进行隔离（已废弃）

角色权限测试支持:
- 通过环境变量注入服务账号密码: LOGBOOK_MIGRATOR_PASSWORD, LOGBOOK_SVC_PASSWORD
- 或在测试中临时创建服务账号并授予 membership
================================================================================
"""

import os
import uuid
from typing import Generator
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse, urlunparse

import psycopg
import pytest

from engram.logbook.schema_context import SchemaContext, reset_schema_context, set_schema_context

# ---------- 角色权限测试相关环境变量 ----------

ENV_LOGBOOK_MIGRATOR_PASSWORD = "LOGBOOK_MIGRATOR_PASSWORD"
ENV_LOGBOOK_SVC_PASSWORD = "LOGBOOK_SVC_PASSWORD"


# ---------- 环境变量名 ----------

ENV_TEST_PG_DSN = "TEST_PG_DSN"
ENV_TEST_PG_ADMIN_DSN = "TEST_PG_ADMIN_DSN"


# ---------- 角色权限测试辅助函数 ----------


def get_service_account_password(env_var: str, default: str = "test_password_12345") -> str:
    """
    获取服务账号密码

    优先从环境变量读取，否则返回默认测试密码。
    """
    return os.environ.get(env_var, default)


def build_user_dsn(base_dsn: str, username: str, password: str) -> str:
    """
    构建指定用户的 DSN

    替换 base_dsn 中的用户名和密码。
    """
    parsed = urlparse(base_dsn)
    new_netloc = f"{username}:{password}@{parsed.hostname}"
    if parsed.port:
        new_netloc += f":{parsed.port}"
    return urlunparse(parsed._replace(netloc=new_netloc))


# ---------- DSN 获取 ----------


def get_test_dsn() -> str:
    """
    获取测试数据库 DSN

    优先从环境变量 TEST_PG_DSN 读取，
    否则使用默认的本地测试数据库。
    """
    return os.environ.get(
        ENV_TEST_PG_DSN, "postgresql://postgres:postgres@localhost:5432/engram_test"
    )


def get_admin_dsn() -> str:
    """
    获取具有 CREATE/DROP DATABASE 权限的管理员 DSN

    优先从环境变量 TEST_PG_ADMIN_DSN 读取，
    否则回退到 TEST_PG_DSN，最后使用默认值。
    默认连接到 postgres 系统数据库。
    """
    return os.environ.get(
        ENV_TEST_PG_ADMIN_DSN,
        os.environ.get(ENV_TEST_PG_DSN, "postgresql://postgres:postgres@localhost:5432/postgres"),
    )


def generate_db_name() -> str:
    """生成唯一的测试数据库名，格式: engram_test_<uuid>"""
    short_id = uuid.uuid4().hex[:12]
    return f"engram_test_{short_id}"


def get_worker_id() -> str:
    """
    获取 pytest-xdist worker ID

    返回 'master' 或 'gw0', 'gw1' 等
    """
    return os.environ.get("PYTEST_XDIST_WORKER", "master")


def replace_db_in_dsn(dsn: str, new_db: str) -> str:
    """
    替换 DSN 中的数据库名

    支持格式: postgresql://user:pass@host:port/dbname
    """
    # 简单替换最后一个 / 后面的部分
    if "/" in dsn:
        base = dsn.rsplit("/", 1)[0]
        return f"{base}/{new_db}"
    return dsn


# ---------- 数据库创建/删除 ----------


def create_test_database(admin_dsn: str, db_name: str) -> str:
    """
    创建测试数据库

    Returns:
        新数据库的 DSN
    """
    conn = psycopg.connect(admin_dsn, autocommit=True)
    try:
        with conn.cursor() as cur:
            # 使用安全的标识符引用
            cur.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        conn.close()

    return replace_db_in_dsn(admin_dsn, db_name)


def drop_test_database(admin_dsn: str, db_name: str):
    """
    删除测试数据库

    会先终止所有连接再删除
    """
    conn = psycopg.connect(admin_dsn, autocommit=True)
    try:
        with conn.cursor() as cur:
            # 终止该数据库的所有连接
            cur.execute(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = %s AND pid != pg_backend_pid()
            """,
                (db_name,),
            )
            # 删除数据库
            cur.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
    finally:
        conn.close()


# ---------- Session-scoped 数据库 Fixture ----------


@pytest.fixture(scope="session")
def test_db_info(request) -> Generator[dict, None, None]:
    """
    为每个测试会话（或 xdist worker）创建独立的测试数据库

    - 使用 TEST_PG_ADMIN_DSN 创建数据库
    - 数据库名格式: engram_test_<uuid>
    - 测试结束后自动删除
    - 支持 pytest -n auto 并发测试
    """
    admin_dsn = get_admin_dsn()
    worker_id = get_worker_id()
    db_name = generate_db_name()

    # 创建测试数据库
    try:
        test_dsn = create_test_database(admin_dsn, db_name)
    except Exception as e:
        pytest.skip(f"无法创建测试数据库 (worker={worker_id}): {e}")
        return

    yield {
        "db_name": db_name,
        "dsn": test_dsn,
        "admin_dsn": admin_dsn,
        "worker_id": worker_id,
    }

    # 清理：删除测试数据库
    try:
        drop_test_database(admin_dsn, db_name)
    except Exception as e:
        # 清理失败只记录警告，不影响测试结果
        import warnings

        warnings.warn(f"清理测试数据库失败 {db_name}: {e}")


@pytest.fixture(scope="session")
def db_dsn(test_db_info: dict) -> str:
    """返回测试数据库 DSN"""
    return test_db_info["dsn"]


@pytest.fixture(scope="session")
def migrated_db(test_db_info: dict) -> Generator[dict, None, None]:
    """
    在测试数据库中执行迁移（创建完整的表结构）

    使用 db_migrate.run_migrate 执行迁移脚本，确保自检通过。
    返回包含 dsn 和数据库信息的字典。
    """
    from engram.logbook.migrate import run_migrate

    dsn = test_db_info["dsn"]

    # 执行迁移
    result = run_migrate(dsn=dsn, quiet=True)

    if not result.get("ok"):
        pytest.fail(
            f"数据库迁移失败: {result.get('message', 'unknown error')}\n"
            f"Detail: {result.get('detail')}"
        )

    yield {
        "dsn": dsn,
        "db_name": test_db_info["db_name"],
        "worker_id": test_db_info["worker_id"],
        "schemas": {
            "identity": "identity",
            "logbook": "logbook",
            "scm": "scm",
            "analysis": "analysis",
            "governance": "governance",
        },
    }


# ---------- 临时 schema Fixture（仅测试模式，路线A 约束下保留用于向后兼容） ----------


def generate_temp_prefix() -> str:
    """生成临时 schema 前缀，格式: test_<short_uuid>"""
    short_id = uuid.uuid4().hex[:8]
    return f"test_{short_id}"


@pytest.fixture(scope="module")
def temp_schemas(migrated_db: dict) -> Generator[dict, None, None]:
    """
    [测试专用/已废弃] 创建临时 schema 集合用于隔离测试

    ============================================================================
    注意：路线A（多库方案）下，推荐使用 migrated_db fixture 进行数据库级隔离。
    此 fixture 保留仅用于向后兼容，新测试应使用 db_conn fixture。
    ============================================================================

    使用 run_migrate 带 schema_prefix 参数创建完整表结构:
    - test_xxx_identity, test_xxx_logbook, test_xxx_scm,
      test_xxx_analysis, test_xxx_governance

    测试完成后自动清理所有临时 schema。
    """

    from engram.logbook.migrate import run_migrate

    # [路线A 约束] 设置测试模式环境变量，允许使用 schema_prefix
    old_testing = os.environ.get("ENGRAM_TESTING")
    os.environ["ENGRAM_TESTING"] = "1"

    prefix = generate_temp_prefix()
    dsn = migrated_db["dsn"]

    schema_names = {
        "identity": f"{prefix}_identity",
        "logbook": f"{prefix}_logbook",
        "scm": f"{prefix}_scm",
        "analysis": f"{prefix}_analysis",
        "governance": f"{prefix}_governance",
    }

    try:
        # 使用 run_migrate 创建带前缀的完整表结构
        result = run_migrate(dsn=dsn, schema_prefix=prefix, quiet=True)

        if not result.get("ok"):
            pytest.fail(
                f"临时 schema 迁移失败 (prefix={prefix}): {result.get('message', 'unknown error')}\n"
                f"Detail: {result.get('detail')}"
            )

        yield {
            "prefix": prefix,
            "schemas": schema_names,
            "dsn": dsn,
        }
    finally:
        # 恢复环境变量
        if old_testing is None:
            os.environ.pop("ENGRAM_TESTING", None)
        else:
            os.environ["ENGRAM_TESTING"] = old_testing

        # 清理临时 schema
        try:
            conn = psycopg.connect(dsn, autocommit=True)
            with conn.cursor() as cur:
                for schema_name in schema_names.values():
                    cur.execute(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE")
            conn.close()
        except Exception as e:
            import warnings

            warnings.warn(f"清理临时 schema 失败 (prefix={prefix}): {e}")


# ---------- search_path 验证 ----------


def build_search_path(schemas: dict, prefix: str = None) -> str:
    """
    构建 search_path 字符串

    顺序: scm, identity, logbook, analysis, governance, public
    确保租户 schema 在前，public 在最后
    """
    return ", ".join(
        [
            schemas["scm"],
            schemas["identity"],
            schemas["logbook"],
            schemas["analysis"],
            schemas["governance"],
            "public",
        ]
    )


def verify_search_path(conn, expected_schemas: dict) -> None:
    """
    验证 search_path 设置正确

    断言：
    1. 租户 schema 在 public 之前
    2. search_path 包含所有必需的 schema
    3. public 是最后一个

    Raises:
        AssertionError: 如果 search_path 不符合期望
    """
    with conn.cursor() as cur:
        cur.execute("SHOW search_path")
        actual_path = cur.fetchone()[0]

    # 解析实际的 search_path（去除引号和空格）
    actual_schemas = [s.strip().strip('"') for s in actual_path.split(",")]

    # 构建期望的 schema 列表
    expected_list = [
        expected_schemas["scm"],
        expected_schemas["identity"],
        expected_schemas["logbook"],
        expected_schemas["analysis"],
        expected_schemas["governance"],
        "public",
    ]

    # 验证: public 必须是最后一个
    assert actual_schemas[-1] == "public", f"search_path 中 public 应在最后，实际: {actual_path}"

    # 验证: 所有期望的 schema 都存在且顺序正确
    for i, expected_schema in enumerate(expected_list):
        assert expected_schema in actual_schemas, (
            f"search_path 缺少 {expected_schema}，实际: {actual_path}"
        )

    # 验证: 租户 schema 在 public 之前
    public_index = actual_schemas.index("public")
    for schema_name in expected_list[:-1]:  # 不包括 public
        schema_index = actual_schemas.index(schema_name)
        assert schema_index < public_index, (
            f"租户 schema {schema_name} 应在 public 之前，实际: {actual_path}"
        )


# ---------- 数据库连接 Fixture ----------


@pytest.fixture(scope="function")
def db_conn(migrated_db: dict) -> Generator[psycopg.Connection, None, None]:
    """
    提供一个自动回滚的数据库连接

    使用标准 schema（identity, logbook, scm, analysis, governance），
    每个测试函数使用独立的事务，测试结束后自动回滚。
    这确保测试之间不会相互影响。

    隔离策略: 多库（每个 xdist worker 使用独立数据库）
    """
    dsn = migrated_db["dsn"]
    schemas = migrated_db["schemas"]

    try:
        conn = psycopg.connect(dsn, autocommit=False)
    except Exception as e:
        pytest.skip(f"无法连接测试数据库: {e}")
        return

    try:
        # 设置 search_path 到标准 schema
        search_path = build_search_path(schemas)
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {search_path}")

        # 验证 search_path 设置正确
        verify_search_path(conn, schemas)

        yield conn
    finally:
        # 回滚所有更改，保持数据库干净
        conn.rollback()
        conn.close()


@pytest.fixture(scope="function")
def db_conn_prefixed(temp_schemas: dict) -> Generator[psycopg.Connection, None, None]:
    """
    [测试专用/已废弃] 提供使用临时 schema 前缀的数据库连接

    ============================================================================
    注意：路线A（多库方案）下，推荐使用 db_conn fixture 进行数据库级隔离。
    此 fixture 保留仅用于向后兼容，新测试应使用 db_conn fixture。
    ============================================================================

    使用 temp_schemas 的带前缀 schema，每个测试函数使用独立事务，
    测试结束后自动回滚。
    """
    dsn = temp_schemas["dsn"]
    schemas = temp_schemas["schemas"]

    try:
        conn = psycopg.connect(dsn, autocommit=False)
    except Exception as e:
        pytest.skip(f"无法连接测试数据库: {e}")
        return

    try:
        # 设置 search_path 到带前缀的 schema
        search_path = build_search_path(schemas)
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {search_path}")

        # 验证 search_path 设置正确
        verify_search_path(conn, schemas)

        yield conn
    finally:
        # 回滚所有更改，保持数据库干净
        conn.rollback()
        conn.close()


@pytest.fixture(scope="function")
def db_conn_committed(migrated_db: dict) -> Generator[psycopg.Connection, None, None]:
    """
    提供一个会提交的数据库连接

    使用标准 schema（identity, logbook, scm, analysis, governance）。
    注意: 使用此 fixture 的测试需要自行清理数据。

    隔离策略: 多库（每个 xdist worker 使用独立数据库）
    """
    dsn = migrated_db["dsn"]
    schemas = migrated_db["schemas"]

    try:
        conn = psycopg.connect(dsn, autocommit=False)
    except Exception as e:
        pytest.skip(f"无法连接测试数据库: {e}")
        return

    try:
        # 设置 search_path 到标准 schema
        search_path = build_search_path(schemas)
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {search_path}")

        # 验证 search_path 设置正确
        verify_search_path(conn, schemas)

        yield conn
    finally:
        conn.close()


# ---------- Mock Fixtures ----------


@pytest.fixture
def mock_config(migrated_db):
    """
    Mock 配置对象

    支持 postgres.search_path 配置，指向标准 schema。
    """
    schemas = migrated_db.get("schemas", {})

    # 构建 search_path 列表
    search_path_list = [
        schemas.get("scm", "scm"),
        schemas.get("identity", "identity"),
        schemas.get("logbook", "logbook"),
        schemas.get("analysis", "analysis"),
        schemas.get("governance", "governance"),
    ]

    config = MagicMock()

    # 配置 get 方法返回值
    def get_side_effect(key, default=None):
        values = {
            "postgres.search_path": search_path_list,
        }
        return values.get(key, default)

    config.get.side_effect = get_side_effect

    config.require.side_effect = lambda key: {
        "postgres.dsn": migrated_db["dsn"],
    }.get(key, f"mock_{key}")

    return config


@pytest.fixture
def mock_config_simple():
    """
    简单的 Mock 配置对象（不依赖 migrated_db）

    用于不需要数据库隔离的单元测试。
    """
    config = MagicMock()
    config.get.return_value = None
    config.require.side_effect = lambda key: {
        "postgres.dsn": get_test_dsn(),
    }.get(key, f"mock_{key}")
    return config


@pytest.fixture
def mock_subprocess():
    """Mock subprocess.run"""
    with patch("subprocess.run") as mock_run:
        yield mock_run


# ---------- 角色权限测试 Fixtures ----------


@pytest.fixture(scope="session")
def roles_applied_db(test_db_info: dict) -> Generator[dict, None, None]:
    """
    确保测试数据库已应用角色脚本的 fixture

    - 执行 db_migrate.py --apply-roles
    - 创建/更新 logbook_migrator, logbook_svc 登录角色
    - 返回包含角色连接信息的字典

    适用于需要测试角色权限边界的集成测试。
    """

    from engram.logbook.migrate import run_migrate

    dsn = test_db_info["dsn"]
    admin_dsn = test_db_info.get("admin_dsn", dsn)

    # 执行迁移并应用角色
    result = run_migrate(
        dsn=dsn,
        quiet=True,
        apply_roles=True,
        public_policy="strict",
    )

    if not result.get("ok"):
        pytest.fail(
            f"迁移或应用角色失败: {result.get('message', 'unknown error')}\n"
            f"Detail: {result.get('detail')}"
        )

    # 获取服务账号密码
    migrator_password = get_service_account_password(ENV_LOGBOOK_MIGRATOR_PASSWORD)
    svc_password = get_service_account_password(ENV_LOGBOOK_SVC_PASSWORD)

    # 创建或更新服务账号
    try:
        conn = psycopg.connect(admin_dsn, autocommit=True)
        with conn.cursor() as cur:
            # logbook_migrator
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'logbook_migrator'")
            if cur.fetchone():
                cur.execute(
                    "ALTER ROLE logbook_migrator WITH LOGIN PASSWORD %s", (migrator_password,)
                )
            else:
                cur.execute("CREATE ROLE logbook_migrator LOGIN PASSWORD %s", (migrator_password,))
            cur.execute("GRANT engram_migrator TO logbook_migrator")

            # logbook_svc
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'logbook_svc'")
            if cur.fetchone():
                cur.execute("ALTER ROLE logbook_svc WITH LOGIN PASSWORD %s", (svc_password,))
            else:
                cur.execute("CREATE ROLE logbook_svc LOGIN PASSWORD %s", (svc_password,))
            cur.execute("GRANT engram_app_readwrite TO logbook_svc")
        conn.close()
    except Exception as e:
        pytest.skip(f"无法创建服务账号: {e}")
        return

    yield {
        "dsn": dsn,
        "admin_dsn": admin_dsn,
        "migrator_dsn": build_user_dsn(dsn, "logbook_migrator", migrator_password),
        "svc_dsn": build_user_dsn(dsn, "logbook_svc", svc_password),
        "db_name": test_db_info["db_name"],
        "roles_applied": True,
    }


# ---------- SchemaContext Fixtures ----------


@pytest.fixture(scope="function")
def schema_context(migrated_db: dict) -> Generator[SchemaContext, None, None]:
    """
    提供 SchemaContext 实例并设置为全局上下文

    使用标准 schema（无前缀），测试结束后重置全局上下文。
    """
    # 创建 SchemaContext（标准 schema，无前缀）
    ctx = SchemaContext(schema_prefix=None, tenant="test")

    # 设置为全局上下文
    set_schema_context(ctx)

    yield ctx

    # 重置全局上下文
    reset_schema_context()


@pytest.fixture(scope="function")
def schema_context_prefixed(temp_schemas: dict) -> Generator[SchemaContext, None, None]:
    """
    [测试专用/已废弃] 提供带前缀的 SchemaContext 实例用于隔离测试

    ============================================================================
    注意：路线A（多库方案）下，推荐使用 schema_context fixture（无前缀）。
    此 fixture 保留仅用于向后兼容。
    ============================================================================

    使用 temp_schemas 的前缀，测试结束后重置全局上下文。
    """
    prefix = temp_schemas["prefix"]

    # 创建带前缀的 SchemaContext
    ctx = SchemaContext(schema_prefix=prefix, tenant="test_isolated")

    # 设置为全局上下文
    set_schema_context(ctx)

    yield ctx

    # 重置全局上下文
    reset_schema_context()
