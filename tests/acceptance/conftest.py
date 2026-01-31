# -*- coding: utf-8 -*-
"""
验收测试 pytest fixtures

提供:
- 独立的测试数据库（复用 logbook conftest 的模式）
- 未迁移的空数据库 fixture（用于测试迁移流程）
- TCP 端口分配（用于 Gateway 启动测试）
- 环境变量隔离

================================================================================
测试隔离策略:
- 每个测试会话使用独立数据库 (engram_test_<uuid>)
- 支持 pytest-xdist 并行测试
- 测试结束后自动清理
================================================================================
"""

import os
import socket
import sys
import uuid
from pathlib import Path
from typing import Generator

import psycopg
import pytest

# ---------- 路径设置 ----------


def ensure_src_in_path():
    """确保 src 目录在 sys.path 中"""
    src_dir = Path(__file__).parent.parent.parent / "src"
    if src_dir.exists() and str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))


ensure_src_in_path()


# ---------- 环境变量名 ----------

ENV_TEST_PG_DSN = "TEST_PG_DSN"
ENV_TEST_PG_ADMIN_DSN = "TEST_PG_ADMIN_DSN"


# ---------- DSN 工具函数 ----------


def get_test_dsn() -> str:
    """获取测试数据库 DSN"""
    return os.environ.get(
        ENV_TEST_PG_DSN, "postgresql://postgres:postgres@localhost:5432/engram_test"
    )


def get_admin_dsn() -> str:
    """获取管理员 DSN（用于创建/删除数据库）"""
    return os.environ.get(
        ENV_TEST_PG_ADMIN_DSN,
        os.environ.get(ENV_TEST_PG_DSN, "postgresql://postgres:postgres@localhost:5432/postgres"),
    )


def generate_db_name() -> str:
    """生成唯一的测试数据库名"""
    short_id = uuid.uuid4().hex[:12]
    return f"engram_test_{short_id}"


def get_worker_id() -> str:
    """获取 pytest-xdist worker ID"""
    return os.environ.get("PYTEST_XDIST_WORKER", "master")


def replace_db_in_dsn(dsn: str, new_db: str) -> str:
    """替换 DSN 中的数据库名"""
    if "/" in dsn:
        base = dsn.rsplit("/", 1)[0]
        return f"{base}/{new_db}"
    return dsn


# ---------- 数据库操作 ----------


def create_test_database(admin_dsn: str, db_name: str) -> str:
    """创建测试数据库"""
    conn = psycopg.connect(admin_dsn, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        conn.close()
    return replace_db_in_dsn(admin_dsn, db_name)


def drop_test_database(admin_dsn: str, db_name: str):
    """删除测试数据库"""
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
            cur.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
    finally:
        conn.close()


# ---------- Session-scoped 数据库 Fixtures ----------


@pytest.fixture(scope="session")
def test_db_info(request) -> Generator[dict, None, None]:
    """
    为每个测试会话创建独立的测试数据库

    - 数据库名格式: engram_test_<uuid>
    - 测试结束后自动删除
    """
    admin_dsn = get_admin_dsn()
    worker_id = get_worker_id()
    db_name = generate_db_name()

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

    # 清理
    try:
        drop_test_database(admin_dsn, db_name)
    except Exception as e:
        import warnings

        warnings.warn(f"清理测试数据库失败 {db_name}: {e}")


@pytest.fixture(scope="session")
def empty_db(test_db_info: dict) -> dict:
    """
    返回空数据库信息（未执行迁移）

    用于测试迁移流程。
    """
    return test_db_info


@pytest.fixture(scope="session")
def migrated_db(test_db_info: dict) -> Generator[dict, None, None]:
    """
    在测试数据库中执行迁移

    返回包含 dsn 和数据库信息的字典。
    """
    from engram.logbook.migrate import run_migrate

    dsn = test_db_info["dsn"]

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


# ---------- 数据库连接 Fixtures ----------


@pytest.fixture(scope="function")
def db_conn(migrated_db: dict) -> Generator[psycopg.Connection, None, None]:
    """提供自动回滚的数据库连接"""
    dsn = migrated_db["dsn"]
    schemas = migrated_db["schemas"]

    try:
        conn = psycopg.connect(dsn, autocommit=False)
    except Exception as e:
        pytest.skip(f"无法连接测试数据库: {e}")
        return

    try:
        search_path = ", ".join(
            [
                schemas["scm"],
                schemas["identity"],
                schemas["logbook"],
                schemas["analysis"],
                schemas["governance"],
                "public",
            ]
        )
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {search_path}")

        yield conn
    finally:
        conn.rollback()
        conn.close()


# ---------- 端口分配 ----------


@pytest.fixture(scope="function")
def unused_tcp_port() -> int:
    """获取一个未使用的 TCP 端口"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
    return port


# ---------- 环境变量隔离 ----------


@pytest.fixture(scope="function")
def isolated_env(monkeypatch):
    """
    提供隔离的环境变量上下文

    测试结束后自动恢复原有环境变量。
    """
    # 清除可能影响测试的环境变量
    env_vars_to_clear = [
        "POSTGRES_DSN",
        "PROJECT_KEY",
        "OPENMEMORY_BASE_URL",
        "OPENMEMORY_API_KEY",
        "GATEWAY_PORT",
        "ENGRAM_LOGBOOK_CONFIG",
    ]

    for var in env_vars_to_clear:
        monkeypatch.delenv(var, raising=False)

    return monkeypatch


# ---------- 跳过标记 ----------

requires_postgres = pytest.mark.skipif(
    not os.environ.get("TEST_PG_DSN") and not os.path.exists("/var/run/postgresql"),
    reason="需要 PostgreSQL 数据库",
)

requires_gateway_deps = pytest.mark.skipif(
    os.environ.get("SKIP_GATEWAY_TESTS", "").lower() in ("1", "true", "yes"),
    reason="Gateway 依赖未安装或被跳过",
)
