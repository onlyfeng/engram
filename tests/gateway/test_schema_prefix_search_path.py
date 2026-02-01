# -*- coding: utf-8 -*-
"""
test_schema_prefix_search_path - Schema 前缀与 search_path 集成测试

测试用例覆盖：
1. get_connection() 无显式 search_path 时，自动使用全局 SchemaContext
2. search_path 包含带前缀的 schema（<prefix>_logbook, <prefix>_governance 等）
3. public 作为兜底始终在 search_path 末尾
4. 通过 LogbookAdapter 写入数据后，可用同一连接从 prefixed schema 查询

参考文档：
- src/engram/logbook/db.py: get_connection 函数
- src/engram/logbook/schema_context.py: SchemaContext 类
- docs/architecture/adr_logbook_strict_island_expansion_config_uri_db.md

跳过条件：HTTP_ONLY_MODE: Schema 前缀测试需要 Docker 和数据库
"""

import os
import uuid

import psycopg
import pytest

# ---------- 跳过条件 ----------


def is_http_only_mode() -> bool:
    """检查是否为 HTTP_ONLY_MODE"""
    return os.environ.get("HTTP_ONLY_MODE", "0") == "1"


pytestmark = pytest.mark.skipif(
    is_http_only_mode(),
    reason="HTTP_ONLY_MODE: Schema 前缀测试需要 Docker 和数据库",
)


# ---------- 测试用例 ----------


@pytest.mark.skip(reason="需要 migrated_db_prefixed fixture，尚未实现")
class TestSearchPathWithPrefixedSchema:
    """测试 search_path 与 prefixed schema 的集成

    覆盖场景：
    1. get_connection() 不显式传 search_path 时，使用全局 SchemaContext
    2. search_path 包含所有带前缀的 schema
    3. public 在 search_path 末尾作为兜底
    """

    def test_get_connection_uses_global_schema_context(
        self,
        migrated_db_prefixed: dict,
        prefixed_schema_context,
    ):
        """
        测试 get_connection() 不传 search_path 时自动使用全局 SchemaContext

        流程：
        1. 全局 SchemaContext 已通过 prefixed_schema_context fixture 设置
        2. 调用 get_connection()（不传 search_path）
        3. 执行 SHOW search_path 验证包含带前缀的 schema
        4. 验证 public 在末尾
        """
        from engram.logbook.db import get_connection

        dsn = migrated_db_prefixed["dsn"]
        schema_prefix = migrated_db_prefixed["schema_prefix"]

        # 获取连接（不传 search_path，依赖全局 SchemaContext）
        conn = get_connection(dsn=dsn)
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW search_path")
                result = cur.fetchone()
                search_path_str = result[0] if result else ""

            # 解析 search_path
            search_path_list = [s.strip().strip('"') for s in search_path_str.split(",")]

            # 验证包含带前缀的 schema
            expected_schemas = [
                f"{schema_prefix}_logbook",
                f"{schema_prefix}_scm",
                f"{schema_prefix}_identity",
                f"{schema_prefix}_analysis",
                f"{schema_prefix}_governance",
            ]

            for expected_schema in expected_schemas:
                assert expected_schema in search_path_list, (
                    f"search_path 应包含 {expected_schema}，实际: {search_path_str}"
                )

            # 验证 public 在末尾
            assert search_path_list[-1] == "public", (
                f"public 应在 search_path 末尾，实际: {search_path_list}"
            )
        finally:
            conn.close()

    def test_search_path_order_matches_schema_context(
        self,
        migrated_db_prefixed: dict,
        prefixed_schema_context,
    ):
        """
        测试 search_path 顺序与 SchemaContext.search_path 一致

        SchemaContext.search_path 顺序:
        logbook, scm, identity, analysis, governance, public
        """
        from engram.logbook.db import get_connection

        dsn = migrated_db_prefixed["dsn"]
        expected_search_path = prefixed_schema_context.search_path

        conn = get_connection(dsn=dsn)
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW search_path")
                result = cur.fetchone()
                search_path_str = result[0] if result else ""

            # 解析 search_path
            actual_search_path = [s.strip().strip('"') for s in search_path_str.split(",")]

            # 验证顺序一致
            assert actual_search_path == expected_search_path, (
                f"search_path 顺序应与 SchemaContext 一致。\n"
                f"期望: {expected_search_path}\n"
                f"实际: {actual_search_path}"
            )
        finally:
            conn.close()


@pytest.mark.skip(reason="需要 migrated_db_prefixed fixture，尚未实现")
class TestLogbookAdapterWithPrefixedSchema:
    """测试 LogbookAdapter 在 prefixed schema 下的读写操作

    覆盖场景：
    1. insert_audit 写入数据到 prefixed schema
    2. enqueue_outbox 写入数据到 prefixed schema
    3. 用同一连接可从 prefixed schema 查询到写入的数据
    """

    def test_insert_audit_to_prefixed_schema(
        self,
        migrated_db_prefixed: dict,
        prefixed_schema_context,
    ):
        """
        测试 LogbookAdapter.insert_audit 写入 prefixed governance schema

        流程：
        1. 使用 LogbookAdapter 写入审计记录
        2. 用带 prefixed search_path 的连接查询 governance.write_audit
        3. 验证数据存在于 prefixed schema
        """
        from engram.gateway.logbook_adapter import LogbookAdapter
        from engram.logbook.db import get_connection

        dsn = migrated_db_prefixed["dsn"]
        governance_schema = prefixed_schema_context.governance
        correlation_id = f"corr-{uuid.uuid4().hex[:16]}"

        # 使用 LogbookAdapter 写入审计记录
        adapter = LogbookAdapter(dsn=dsn)
        audit_id = adapter.insert_audit(
            actor_user_id="test_user",
            target_space="private:test_space",
            action="allow",
            reason="test_schema_prefix",
            correlation_id=correlation_id,
            status="success",
        )

        assert audit_id > 0, "应返回有效的 audit_id"

        # 用同一 search_path 的连接查询
        conn = get_connection(dsn=dsn)
        try:
            with conn.cursor() as cur:
                # 使用完全限定表名查询，确认数据在 prefixed schema
                cur.execute(
                    f"""
                    SELECT audit_id, actor_user_id, target_space, action, reason, correlation_id
                    FROM {governance_schema}.write_audit
                    WHERE audit_id = %s
                    """,
                    (audit_id,),
                )
                row = cur.fetchone()

            assert row is not None, (
                f"应能从 {governance_schema}.write_audit 查询到 audit_id={audit_id}"
            )
            assert row[0] == audit_id
            assert row[1] == "test_user"
            assert row[2] == "private:test_space"
            assert row[3] == "allow"
            assert row[4] == "test_schema_prefix"
            assert row[5] == correlation_id
        finally:
            conn.close()

    def test_enqueue_outbox_to_prefixed_schema(
        self,
        migrated_db_prefixed: dict,
        prefixed_schema_context,
    ):
        """
        测试 LogbookAdapter.enqueue_outbox 写入 prefixed logbook schema

        流程：
        1. 使用 LogbookAdapter 入队 outbox 记录
        2. 用带 prefixed search_path 的连接查询 logbook.outbox_memory
        3. 验证数据存在于 prefixed schema
        """
        from engram.gateway.logbook_adapter import LogbookAdapter
        from engram.logbook.db import get_connection

        dsn = migrated_db_prefixed["dsn"]
        logbook_schema = prefixed_schema_context.logbook
        test_payload = f"test payload {uuid.uuid4().hex[:8]}"
        target_space = f"team:test_{uuid.uuid4().hex[:8]}"

        # 使用 LogbookAdapter 入队
        adapter = LogbookAdapter(dsn=dsn)
        outbox_id = adapter.enqueue_outbox(
            payload_md=test_payload,
            target_space=target_space,
            last_error="test error",
        )

        assert outbox_id > 0, "应返回有效的 outbox_id"

        # 用同一 search_path 的连接查询
        conn = get_connection(dsn=dsn)
        try:
            with conn.cursor() as cur:
                # 使用完全限定表名查询，确认数据在 prefixed schema
                cur.execute(
                    f"""
                    SELECT outbox_id, payload_md, target_space, status, last_error
                    FROM {logbook_schema}.outbox_memory
                    WHERE outbox_id = %s
                    """,
                    (outbox_id,),
                )
                row = cur.fetchone()

            assert row is not None, (
                f"应能从 {logbook_schema}.outbox_memory 查询到 outbox_id={outbox_id}"
            )
            assert row[0] == outbox_id
            assert row[1] == test_payload
            assert row[2] == target_space
            assert row[3] == "pending"
            assert row[4] == "test error"
        finally:
            conn.close()

    def test_search_path_based_query_without_schema_prefix(
        self,
        migrated_db_prefixed: dict,
        prefixed_schema_context,
    ):
        """
        测试通过 search_path 解析（不带 schema 前缀）可正确查询 prefixed schema

        此测试验证 search_path 机制正常工作：
        1. 写入数据到 prefixed schema
        2. 使用不带 schema 前缀的表名查询
        3. 验证通过 search_path 解析到正确的 prefixed schema
        """
        from engram.gateway.logbook_adapter import LogbookAdapter
        from engram.logbook.db import get_connection

        dsn = migrated_db_prefixed["dsn"]
        correlation_id = f"corr-{uuid.uuid4().hex[:16]}"

        # 写入审计记录
        adapter = LogbookAdapter(dsn=dsn)
        audit_id = adapter.insert_audit(
            actor_user_id="search_path_test_user",
            target_space="private:search_path_test",
            action="redirect",
            reason="search_path_resolution_test",
            correlation_id=correlation_id,
            status="pending",
        )

        # 使用不带 schema 前缀的表名查询（依赖 search_path 解析）
        conn = get_connection(dsn=dsn)
        try:
            with conn.cursor() as cur:
                # 不带 schema 前缀的查询
                cur.execute(
                    """
                    SELECT audit_id, correlation_id, status
                    FROM write_audit
                    WHERE correlation_id = %s
                    """,
                    (correlation_id,),
                )
                row = cur.fetchone()

            assert row is not None, (
                f"应能通过 search_path 解析从 write_audit 查询到 correlation_id={correlation_id}"
            )
            assert row[0] == audit_id
            assert row[1] == correlation_id
            assert row[2] == "pending"
        finally:
            conn.close()


@pytest.mark.skip(reason="需要 migrated_db_prefixed fixture，尚未实现")
class TestSearchPathIsolation:
    """测试 search_path 隔离性

    验证不同 prefix 的 schema 之间数据隔离
    """

    def test_data_isolated_between_prefixed_and_default_schema(
        self,
        migrated_db_prefixed: dict,
        prefixed_schema_context,
    ):
        """
        测试 prefixed schema 与默认 schema 之间的数据隔离

        验证：
        1. 写入数据到 prefixed schema
        2. 使用默认 search_path 的连接无法查询到该数据
        """
        from engram.gateway.logbook_adapter import LogbookAdapter
        from engram.logbook.db import DEFAULT_SEARCH_PATH, get_connection

        dsn = migrated_db_prefixed["dsn"]
        correlation_id = f"corr-{uuid.uuid4().hex[:16]}"

        # 写入数据到 prefixed schema
        adapter = LogbookAdapter(dsn=dsn)
        adapter.insert_audit(
            actor_user_id="isolation_test_user",
            target_space="private:isolation_test",
            action="allow",
            reason="isolation_test",
            correlation_id=correlation_id,
            status="success",
        )

        # 使用默认 search_path 的连接查询
        # 注意：需要显式指定 search_path 覆盖全局 SchemaContext
        conn = get_connection(dsn=dsn, search_path=DEFAULT_SEARCH_PATH)
        try:
            with conn.cursor() as cur:
                # 尝试从默认 governance.write_audit 查询
                # 由于数据在 prefixed schema，应该查询不到
                try:
                    cur.execute(
                        """
                        SELECT audit_id FROM governance.write_audit
                        WHERE correlation_id = %s
                        """,
                        (correlation_id,),
                    )
                    row = cur.fetchone()
                    # 如果默认 governance schema 不存在，查询会失败
                    # 这是预期的隔离行为
                    if row is not None:
                        pytest.fail("数据应隔离：prefixed schema 的数据不应在默认 schema 中可见")
                except psycopg.errors.UndefinedTable:
                    # 表不存在（默认 schema 未创建）是合理的隔离行为
                    pass
        finally:
            conn.close()


@pytest.mark.skip(reason="需要 migrated_db_prefixed fixture，尚未实现")
class TestPublicSchemaFallback:
    """测试 public schema 作为兜底的行为"""

    def test_public_always_at_end_of_search_path(
        self,
        migrated_db_prefixed: dict,
        prefixed_schema_context,
    ):
        """
        验证 public 始终在 search_path 末尾

        即使使用 prefixed schema，public 也应作为最后的兜底
        """
        from engram.logbook.db import get_connection

        dsn = migrated_db_prefixed["dsn"]

        conn = get_connection(dsn=dsn)
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW search_path")
                result = cur.fetchone()
                search_path_str = result[0] if result else ""

            search_path_list = [s.strip().strip('"') for s in search_path_str.split(",")]

            assert "public" in search_path_list, "search_path 应包含 public"
            assert search_path_list.index("public") == len(search_path_list) - 1, (
                f"public 应在 search_path 末尾，实际位置: {search_path_list.index('public')}，"
                f"总长度: {len(search_path_list)}"
            )
        finally:
            conn.close()

    def test_explicit_search_path_also_appends_public(
        self,
        migrated_db_prefixed: dict,
    ):
        """
        测试显式传入 search_path 时，如果不含 public，会自动追加

        验证 get_connection 的 public 兜底逻辑
        """
        from engram.logbook.db import get_connection

        dsn = migrated_db_prefixed["dsn"]

        # 显式传入不含 public 的 search_path
        custom_search_path = ["logbook", "governance"]
        conn = get_connection(dsn=dsn, search_path=custom_search_path)
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW search_path")
                result = cur.fetchone()
                search_path_str = result[0] if result else ""

            search_path_list = [s.strip().strip('"') for s in search_path_str.split(",")]

            # 验证 public 被自动追加
            assert "public" in search_path_list, "即使显式 search_path 不含 public，也应自动追加"
            assert search_path_list[-1] == "public", "自动追加的 public 应在末尾"
        finally:
            conn.close()
