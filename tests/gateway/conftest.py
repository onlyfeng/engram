# -*- coding: utf-8 -*-
"""
pytest 共享 fixtures

提供:
- PostgreSQL 临时数据库 + 自动建表（复用 Logbook 的 test DB fixture 思路）
- 支持 pytest-xdist 并行测试（每个 worker 独立数据库）
- 可复用的 migrated_db fixture
- logbook_adapter 配置

================================================================================
架构约束（多库方案）:
--------------------------------------------------------------------------------
测试隔离策略: 每个测试会话使用独立数据库（多库隔离）
- 数据库名格式: engram_test_<uuid>
- 每个 xdist worker 使用独立数据库
================================================================================
"""

import os
import uuid
from typing import Generator
from unittest.mock import patch

import psycopg
import pytest

# ---------- 环境变量名 ----------

ENV_TEST_PG_DSN = "TEST_PG_DSN"
ENV_TEST_PG_ADMIN_DSN = "TEST_PG_ADMIN_DSN"


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


# ---------- search_path 辅助函数 ----------


def build_search_path(schemas: dict) -> str:
    """
    构建 search_path 字符串

    顺序: scm, identity, logbook, analysis, governance, public
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


# ---------- 数据库连接 Fixture ----------


@pytest.fixture(scope="function")
def db_conn(migrated_db: dict) -> Generator[psycopg.Connection, None, None]:
    """
    提供一个自动回滚的数据库连接

    使用标准 schema（identity, logbook, scm, analysis, governance），
    每个测试函数使用独立的事务，测试结束后自动回滚。
    """
    dsn = migrated_db["dsn"]
    schemas = migrated_db["schemas"]

    try:
        conn = psycopg.connect(dsn, autocommit=False)
    except Exception as e:
        pytest.skip(f"无法连接测试数据库: {e}")
        return

    try:
        search_path = build_search_path(schemas)
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {search_path}")

        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture(scope="function")
def db_conn_committed(migrated_db: dict) -> Generator[psycopg.Connection, None, None]:
    """
    提供一个会提交的数据库连接

    使用标准 schema。注意：使用此 fixture 的测试需要自行清理数据。
    """
    dsn = migrated_db["dsn"]
    schemas = migrated_db["schemas"]

    try:
        conn = psycopg.connect(dsn, autocommit=False)
    except Exception as e:
        pytest.skip(f"无法连接测试数据库: {e}")
        return

    try:
        search_path = build_search_path(schemas)
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {search_path}")

        yield conn
    finally:
        conn.close()


# ---------- logbook_adapter 配置 Fixture ----------


@pytest.fixture(scope="function")
def logbook_adapter_config(migrated_db: dict):
    """
    配置 logbook_adapter 使用测试数据库

    设置 POSTGRES_DSN 环境变量，重置全局适配器实例
    """
    dsn = migrated_db["dsn"]
    old_dsn = os.environ.get("POSTGRES_DSN")
    os.environ["POSTGRES_DSN"] = dsn

    # 重置全局适配器
    from engram.gateway import logbook_adapter

    logbook_adapter.reset_adapter()

    yield dsn

    # 恢复环境变量
    if old_dsn is None:
        os.environ.pop("POSTGRES_DSN", None)
    else:
        os.environ["POSTGRES_DSN"] = old_dsn

    # 再次重置
    logbook_adapter.reset_adapter()


# ---------- Gateway 状态重置辅助函数 ----------


def reset_all_gateway_state():
    """
    集中重置所有 Gateway 相关的全局状态

    包括:
    - config: 配置单例
    - logbook_adapter: 适配器单例
    - openmemory_client: 客户端单例
    - container: 全局依赖容器
    - mcp_rpc: correlation_id 和 tool_executor
    - middleware: request correlation_id
    - engram.gateway: 懒加载缓存

    用于测试间隔离，确保每个测试从干净状态开始。
    """
    # 重置 config
    try:
        from engram.gateway.config import reset_config

        reset_config()
    except ImportError:
        pass

    # 重置 logbook_adapter
    try:
        from engram.gateway.logbook_adapter import reset_adapter

        reset_adapter()
    except (ImportError, AttributeError):
        pass

    # 重置 openmemory_client
    try:
        from engram.gateway.openmemory_client import reset_client

        reset_client()
    except (ImportError, AttributeError):
        pass

    # 重置 container
    try:
        from engram.gateway.container import reset_container

        reset_container()
    except ImportError:
        pass

    # 重置 mcp_rpc ContextVar 和全局变量
    try:
        from engram.gateway.mcp_rpc import (
            reset_current_correlation_id_for_testing,
            reset_tool_executor_for_testing,
        )

        reset_current_correlation_id_for_testing()
        reset_tool_executor_for_testing()
    except (ImportError, AttributeError):
        pass

    # 重置 middleware ContextVar
    try:
        from engram.gateway.middleware import reset_request_correlation_id_for_testing

        reset_request_correlation_id_for_testing()
    except (ImportError, AttributeError):
        pass

    # 重置 engram.gateway 懒加载缓存
    try:
        import engram.gateway as gateway_module

        if hasattr(gateway_module, "_reset_gateway_lazy_import_cache_for_testing"):
            gateway_module._reset_gateway_lazy_import_cache_for_testing()
    except (ImportError, AttributeError):
        pass


@pytest.fixture(autouse=True)
def auto_reset_gateway_state():
    """
    自动重置 Gateway 状态的 fixture（autouse=True）

    在每个测试函数前后自动重置全局状态，确保测试隔离。
    """
    # setup: 测试前重置
    reset_all_gateway_state()

    yield

    # teardown: 测试后重置
    reset_all_gateway_state()


# ---------- GatewayDeps Fixture ----------
# 提供可注入的依赖容器，让测试通过依赖注入替代 patch
#
# 使用指南:
# =========
# 1. 对于 handler 单元测试，优先使用 gateway_deps fixture：
#
#    @pytest.mark.asyncio
#    async def test_handler(gateway_deps, test_correlation_id):
#        result = await memory_store_impl(
#            payload_md="test",
#            correlation_id=test_correlation_id,
#            deps=gateway_deps,
#        )
#
# 2. 需要自定义依赖时，使用 GatewayDeps.for_testing()：
#
#    deps = GatewayDeps.for_testing(
#        config=custom_config,
#        db=custom_db,
#    )
#
# 3. 对于 FastAPI 集成测试，使用 gateway_test_container fixture。


@pytest.fixture(scope="function")
def fake_gateway_config():
    """
    创建 Fake Gateway 配置

    返回 FakeGatewayConfig 实例，可在测试中自定义配置。
    """
    from tests.gateway.fakes import FakeGatewayConfig

    return FakeGatewayConfig()


@pytest.fixture(scope="function")
def fake_logbook_db():
    """
    创建 Fake Logbook 数据库

    返回 FakeLogbookDatabase 实例，默认配置：
    - team_write_enabled=False
    - policy_json={}
    """
    from tests.gateway.fakes import FakeLogbookDatabase

    db = FakeLogbookDatabase()
    db.configure_settings(team_write_enabled=False, policy_json={})
    return db


@pytest.fixture(scope="function")
def fake_openmemory_client():
    """
    创建 Fake OpenMemory 客户端

    返回 FakeOpenMemoryClient 实例，默认配置为成功模式。
    """
    from tests.gateway.fakes import FakeOpenMemoryClient

    client = FakeOpenMemoryClient()
    client.configure_store_success(memory_id="fake_memory_id")
    client.configure_search_success()
    return client


@pytest.fixture(scope="function")
def fake_logbook_adapter():
    """
    创建 Fake Logbook Adapter

    返回 FakeLogbookAdapter 实例，默认配置为 dedup miss。
    """
    from tests.gateway.fakes import FakeLogbookAdapter

    adapter = FakeLogbookAdapter()
    adapter.configure_dedup_miss()
    return adapter


@pytest.fixture(scope="function")
def gateway_deps(fake_gateway_config, fake_logbook_db, fake_openmemory_client):
    """
    创建完整的 GatewayDeps 依赖容器

    组合 fake_gateway_config, fake_logbook_db, fake_openmemory_client
    用于 handler 测试的依赖注入。

    使用示例:
        @pytest.mark.asyncio
        async def test_memory_store(gateway_deps, test_correlation_id):
            result = await memory_store_impl(
                payload_md="test",
                correlation_id=test_correlation_id,
                deps=gateway_deps,
            )
    """
    from engram.gateway.di import GatewayDeps

    return GatewayDeps.for_testing(
        config=fake_gateway_config,
        db=fake_logbook_db,
        openmemory_client=fake_openmemory_client,
    )


@pytest.fixture(scope="function")
def gateway_deps_with_write_enabled(fake_gateway_config, fake_openmemory_client):
    """
    创建启用了 team_write 的 GatewayDeps

    用于测试允许写入的场景。
    """
    from engram.gateway.di import GatewayDeps
    from tests.gateway.fakes import FakeLogbookDatabase

    db = FakeLogbookDatabase()
    db.configure_settings(team_write_enabled=True, policy_json={})

    return GatewayDeps.for_testing(
        config=fake_gateway_config,
        db=db,
        openmemory_client=fake_openmemory_client,
    )


@pytest.fixture(scope="function")
def test_correlation_id():
    """
    生成测试用的 correlation_id

    返回格式：corr-{16位十六进制}
    """
    import secrets

    return f"corr-{secrets.token_hex(8)}"


@pytest.fixture(scope="function")
def mock_logbook_adapter_module(fake_logbook_adapter):
    """
    [已弃用] Mock logbook_adapter 模块

    此 fixture 已弃用，新测试应使用以下方式进行依赖注入:

    - handler 单元测试: 使用 GatewayDeps.for_testing(logbook_adapter=fake_adapter)
    - FastAPI 集成测试: 使用 gateway_test_container fixture

    仅保留此 fixture 用于以下场景:
    1. outbox_worker 测试（outbox_worker 使用模块级 logbook_adapter，不通过 DI）
    2. legacy 代码路径测试（正在迁移中的旧代码）

    迁移指南:
        # 旧方式（不推荐）
        async def test_old(mock_logbook_adapter_module):
            mock_logbook_adapter_module.configure_dedup_hit(memory_id="mem_123")

        # 新方式（推荐）
        async def test_new(fake_logbook_adapter, test_correlation_id):
            fake_logbook_adapter.configure_dedup_hit(memory_id="mem_123")
            deps = GatewayDeps.for_testing(logbook_adapter=fake_logbook_adapter)
            result = await memory_store_impl(deps=deps, correlation_id=test_correlation_id)
    """
    import warnings

    warnings.warn(
        "mock_logbook_adapter_module fixture 已弃用，请使用 GatewayDeps.for_testing() 进行依赖注入",
        DeprecationWarning,
        stacklevel=2,
    )
    with (
        patch("engram.gateway.handlers.memory_store.logbook_adapter", fake_logbook_adapter),
        patch("engram.gateway.handlers.memory_query.logbook_adapter", fake_logbook_adapter),
    ):
        yield fake_logbook_adapter


# ---------- FastAPI 集成测试 Fixture ----------


@pytest.fixture(scope="function")
def gateway_test_container(
    fake_gateway_config, fake_logbook_db, fake_openmemory_client, fake_logbook_adapter
):
    """
    为 FastAPI 集成测试创建测试容器

    此 fixture 会设置全局容器，使得 FastAPI app 通过 get_gateway_deps()
    获取同一组测试依赖。

    使用示例:
        def test_mcp_endpoint(gateway_test_container):
            from engram.gateway.main import app
            from fastapi.testclient import TestClient

            # app 将使用 gateway_test_container 中的 fake 依赖
            with TestClient(app) as client:
                response = client.post("/mcp", json={...})

    注意:
        - 测试结束后会自动重置全局容器（通过 auto_reset_gateway_state）
        - 如需自定义依赖，直接使用此 fixture 返回的组件字典
    """
    from engram.gateway.container import (
        GatewayContainer,
        set_container,
    )

    # 创建测试容器
    test_container = GatewayContainer.create_for_testing(
        config=fake_gateway_config,
        db=fake_logbook_db,
        logbook_adapter=fake_logbook_adapter,
        openmemory_client=fake_openmemory_client,
    )

    # 设置全局容器
    set_container(test_container)

    yield {
        "container": test_container,
        "config": fake_gateway_config,
        "db": fake_logbook_db,
        "adapter": fake_logbook_adapter,
        "client": fake_openmemory_client,
    }

    # teardown 由 auto_reset_gateway_state 处理
