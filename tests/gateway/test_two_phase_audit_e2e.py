# -*- coding: utf-8 -*-
"""
test_two_phase_audit_e2e - 两阶段审计端到端测试

测试用例覆盖：
1. success 路径：pending -> success 状态转换
2. deferred 路径：pending -> redirected 状态转换（OpenMemory 不可用）
3. correlation_id 一致性追踪
4. reason 后缀追加（redirected 时包含 :outbox:<id>）

参考文档：
- ADR: docs/architecture/adr_gateway_audit_atomicity.md
- 两阶段审计协议：write_pending_audit_or_raise + finalize_audit

跳过条件：HTTP_ONLY_MODE: 两阶段审计测试需要 Docker 和数据库
"""

import os
import uuid

import pytest

# ---------- 跳过条件 ----------


def is_http_only_mode() -> bool:
    """检查是否为 HTTP_ONLY_MODE"""
    return os.environ.get("HTTP_ONLY_MODE", "0") == "1"


pytestmark = pytest.mark.skipif(
    is_http_only_mode(),
    reason="HTTP_ONLY_MODE: 两阶段审计测试需要 Docker 和数据库",
)


# ---------- Fixtures ----------


@pytest.fixture
def two_phase_env(migrated_db_prefixed: dict, logbook_adapter_config):
    """
    设置两阶段审计测试环境

    提供已迁移的数据库和配置好的 logbook_adapter。
    使用带前缀的 schema 实现测试隔离。
    """
    return {
        "dsn": migrated_db_prefixed["dsn"],
        "schemas": migrated_db_prefixed["schemas"],
        "schema_prefix": migrated_db_prefixed["schema_prefix"],
    }


# ---------- 辅助函数（从 audit_assertions 模块导入） ----------

from tests.gateway.audit_assertions import (
    assert_evidence_refs_queryable_by_outbox_id,
    assert_evidence_refs_top_level_contract,
    assert_gateway_event_substructure,
    assert_single_stage_reject_no_pending,
    assert_two_phase_finalized,
    query_audit_by_correlation_id,
)

# ---------- 测试用例 ----------


class TestTwoPhaseAuditSuccessPath:
    """测试两阶段审计的 success 路径

    覆盖场景：
    1. OpenMemory 写入成功 → pending 转为 success
    2. correlation_id 一致可追踪
    3. evidence_refs_json 包含 memory_id
    """

    @pytest.mark.asyncio
    async def test_success_path_status_transition(
        self, two_phase_env, db_conn_prefixed_committed, test_correlation_id
    ):
        """
        测试 success 路径：pending -> success

        流程：
        1. 配置 OpenMemory 成功模式
        2. 调用 memory_store_impl
        3. 验证审计记录 status=pending 后被更新为 status=success
        """
        from engram.gateway.di import GatewayDeps
        from engram.gateway.handlers.memory_store import memory_store_impl
        from engram.gateway.logbook_adapter import LogbookAdapter
        from tests.gateway.fakes import FakeGatewayConfig, FakeOpenMemoryClient

        conn = db_conn_prefixed_committed
        dsn = two_phase_env["dsn"]
        schemas = two_phase_env["schemas"]
        governance_schema = schemas["governance"]

        # 创建真实 LogbookAdapter 依赖（实现 WriteAuditPort）
        # 注意：LogbookAdapter 通过 get_connection() 自动使用全局 SchemaContext
        # prefixed_schema_context fixture 已设置了全局 context
        real_adapter = LogbookAdapter(dsn=dsn)

        # 创建 Fake OpenMemory Client（成功模式）
        fake_client = FakeOpenMemoryClient()
        fake_client.configure_store_success(memory_id="mem_success_001")

        # 创建 Fake Config
        fake_config = FakeGatewayConfig()
        fake_config.postgres_dsn = dsn

        # 组装依赖（adapter-first 模式）
        deps = GatewayDeps.for_testing(
            config=fake_config,
            logbook_adapter=real_adapter,
            openmemory_client=fake_client,
        )

        # 调用 memory_store_impl
        result = await memory_store_impl(
            payload_md="测试内容 - success 路径",
            target_space="private:user_success_test",
            correlation_id=test_correlation_id,
            deps=deps,
        )

        # 验证响应
        assert result.ok is True, f"memory_store 应成功，实际: {result}"
        assert result.action in ("allow", "redirect"), (
            f"action 应为 allow 或 redirect，实际: {result.action}"
        )
        assert result.memory_id == "mem_success_001", (
            f"memory_id 应为 mem_success_001，实际: {result.memory_id}"
        )
        assert result.correlation_id == test_correlation_id, "correlation_id 应一致"

        # 使用辅助断言验证两阶段审计已完成
        audit = assert_two_phase_finalized(conn, test_correlation_id, "success", governance_schema)
        assert audit["correlation_id"] == test_correlation_id, "correlation_id 应一致"

        # 验证 evidence_refs_json 包含 memory_id
        evidence = audit["evidence_refs_json"]
        assert evidence is not None, "evidence_refs_json 不应为空"
        assert evidence.get("memory_id") == "mem_success_001", (
            f"evidence_refs_json 应包含 memory_id=mem_success_001，实际: {evidence}"
        )

    @pytest.mark.asyncio
    async def test_success_path_single_audit_record(
        self, two_phase_env, db_conn_prefixed_committed, test_correlation_id
    ):
        """
        验证 success 路径只产生一条审计记录（不会重复写入）
        """
        from engram.gateway.di import GatewayDeps
        from engram.gateway.handlers.memory_store import memory_store_impl
        from engram.gateway.logbook_adapter import LogbookAdapter
        from tests.gateway.fakes import FakeGatewayConfig, FakeOpenMemoryClient

        conn = db_conn_prefixed_committed
        dsn = two_phase_env["dsn"]
        schemas = two_phase_env["schemas"]
        governance_schema = schemas["governance"]

        real_adapter = LogbookAdapter(dsn=dsn)
        fake_client = FakeOpenMemoryClient()
        fake_client.configure_store_success(memory_id="mem_single_001")

        fake_config = FakeGatewayConfig()
        fake_config.postgres_dsn = dsn

        deps = GatewayDeps.for_testing(
            config=fake_config,
            logbook_adapter=real_adapter,
            openmemory_client=fake_client,
        )

        await memory_store_impl(
            payload_md="测试内容 - 单条审计记录验证",
            target_space="private:user_single_test",
            correlation_id=test_correlation_id,
            deps=deps,
        )

        # 使用辅助断言验证两阶段审计已完成（包含记录数验证）
        assert_two_phase_finalized(conn, test_correlation_id, "success", governance_schema)


class TestTwoPhaseAuditDeferredPath:
    """测试两阶段审计的 deferred 路径

    覆盖场景：
    1. OpenMemory 不可用 → pending 转为 redirected
    2. reason 追加 :outbox:<id>
    3. evidence_refs_json 包含 outbox_id
    4. correlation_id 一致可追踪
    """

    @pytest.mark.asyncio
    async def test_deferred_path_status_transition(
        self, two_phase_env, db_conn_prefixed_committed, test_correlation_id
    ):
        """
        测试 deferred 路径：pending -> redirected

        流程：
        1. 配置 OpenMemory 连接失败模式
        2. 调用 memory_store_impl
        3. 验证审计记录 status=pending 后被更新为 status=redirected
        4. 验证 reason 包含 :outbox:<id>
        """
        from engram.gateway.di import GatewayDeps
        from engram.gateway.handlers.memory_store import memory_store_impl
        from engram.gateway.logbook_adapter import LogbookAdapter
        from tests.gateway.fakes import FakeGatewayConfig, FakeOpenMemoryClient

        conn = db_conn_prefixed_committed
        dsn = two_phase_env["dsn"]
        schemas = two_phase_env["schemas"]
        governance_schema = schemas["governance"]

        # 创建真实 LogbookAdapter 依赖（实现 WriteAuditPort）
        real_adapter = LogbookAdapter(dsn=dsn)

        # 创建 Fake OpenMemory Client（连接失败模式）
        fake_client = FakeOpenMemoryClient()
        fake_client.configure_store_connection_error("OpenMemory 连接超时")

        # 创建 Fake Config
        fake_config = FakeGatewayConfig()
        fake_config.postgres_dsn = dsn

        # 组装依赖（adapter-first 模式）
        deps = GatewayDeps.for_testing(
            config=fake_config,
            logbook_adapter=real_adapter,
            openmemory_client=fake_client,
        )

        # 调用 memory_store_impl
        result = await memory_store_impl(
            payload_md="测试内容 - deferred 路径",
            target_space="private:user_deferred_test",
            correlation_id=test_correlation_id,
            deps=deps,
        )

        # 验证响应
        assert result.ok is False, f"OpenMemory 失败时 ok 应为 False，实际: {result.ok}"
        assert result.action == "deferred", f"action 应为 deferred，实际: {result.action}"
        assert result.outbox_id is not None, "outbox_id 不应为空"
        assert result.correlation_id == test_correlation_id, "correlation_id 应一致"

        outbox_id = result.outbox_id

        # 使用辅助断言验证两阶段审计已完成
        audit = assert_two_phase_finalized(
            conn, test_correlation_id, "redirected", governance_schema
        )
        assert audit["correlation_id"] == test_correlation_id, "correlation_id 应一致"

        # 验证 reason 包含 :outbox:<id>
        reason = audit["reason"]
        assert reason is not None, "reason 不应为空"
        assert f":outbox:{outbox_id}" in reason, (
            f"reason 应包含 ':outbox:{outbox_id}'，实际: {reason}"
        )

        # 使用辅助断言验证 evidence_refs_json 可通过 outbox_id 查询
        assert_evidence_refs_queryable_by_outbox_id(
            conn, outbox_id, governance_schema, expected_fields={"outbox_id": outbox_id}
        )

    @pytest.mark.asyncio
    async def test_deferred_path_outbox_record_created(
        self, two_phase_env, db_conn_prefixed_committed, test_correlation_id
    ):
        """
        验证 deferred 路径创建了 outbox 记录
        """
        from engram.gateway.di import GatewayDeps
        from engram.gateway.handlers.memory_store import memory_store_impl
        from engram.gateway.logbook_adapter import LogbookAdapter
        from tests.gateway.fakes import FakeGatewayConfig, FakeOpenMemoryClient

        conn = db_conn_prefixed_committed
        dsn = two_phase_env["dsn"]
        schemas = two_phase_env["schemas"]
        logbook_schema = schemas["logbook"]

        real_adapter = LogbookAdapter(dsn=dsn)
        fake_client = FakeOpenMemoryClient()
        fake_client.configure_store_connection_error("OpenMemory 不可用")

        fake_config = FakeGatewayConfig()
        fake_config.postgres_dsn = dsn

        deps = GatewayDeps.for_testing(
            config=fake_config,
            logbook_adapter=real_adapter,
            openmemory_client=fake_client,
        )

        result = await memory_store_impl(
            payload_md="测试内容 - outbox 验证",
            target_space="private:user_outbox_test",
            correlation_id=test_correlation_id,
            deps=deps,
        )

        outbox_id = result.outbox_id
        assert outbox_id is not None, "outbox_id 不应为空"

        # 查询 outbox 记录（使用显式 schema 限定名）
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT outbox_id, target_space, status, payload_md
                FROM {logbook_schema}.outbox_memory
                WHERE outbox_id = %s
                """,
                (outbox_id,),
            )
            row = cur.fetchone()
            assert row is not None, f"应存在 outbox_id={outbox_id} 的记录"
            assert row[1] == "private:user_outbox_test", f"target_space 应匹配，实际: {row[1]}"
            assert row[2] == "pending", f"outbox status 应为 pending，实际: {row[2]}"
            assert "测试内容 - outbox 验证" in row[3], f"payload_md 应包含测试内容，实际: {row[3]}"

    @pytest.mark.asyncio
    async def test_deferred_path_single_audit_record(
        self, two_phase_env, db_conn_prefixed_committed, test_correlation_id
    ):
        """
        验证 deferred 路径只产生一条审计记录（不会重复写入）
        """
        from engram.gateway.di import GatewayDeps
        from engram.gateway.handlers.memory_store import memory_store_impl
        from engram.gateway.logbook_adapter import LogbookAdapter
        from tests.gateway.fakes import FakeGatewayConfig, FakeOpenMemoryClient

        conn = db_conn_prefixed_committed
        dsn = two_phase_env["dsn"]
        schemas = two_phase_env["schemas"]
        governance_schema = schemas["governance"]

        real_adapter = LogbookAdapter(dsn=dsn)
        fake_client = FakeOpenMemoryClient()
        fake_client.configure_store_api_error(status_code=503)

        fake_config = FakeGatewayConfig()
        fake_config.postgres_dsn = dsn

        deps = GatewayDeps.for_testing(
            config=fake_config,
            logbook_adapter=real_adapter,
            openmemory_client=fake_client,
        )

        await memory_store_impl(
            payload_md="测试内容 - 单条审计记录（deferred）",
            target_space="private:user_single_deferred_test",
            correlation_id=test_correlation_id,
            deps=deps,
        )

        # 使用辅助断言验证两阶段审计已完成（包含记录数验证）
        assert_two_phase_finalized(conn, test_correlation_id, "redirected", governance_schema)


class TestTwoPhaseAuditCorrelationIdTracking:
    """测试 correlation_id 一致性追踪

    覆盖场景：
    1. correlation_id 在审计记录中正确存储
    2. 多次调用使用不同 correlation_id 时不互相干扰
    3. 响应中的 correlation_id 与请求一致
    """

    @pytest.mark.asyncio
    async def test_correlation_id_consistency(self, two_phase_env, db_conn_prefixed_committed):
        """
        测试多次调用使用不同 correlation_id 时不互相干扰
        """
        from engram.gateway.di import GatewayDeps
        from engram.gateway.handlers.memory_store import memory_store_impl
        from engram.gateway.logbook_adapter import LogbookAdapter
        from tests.gateway.fakes import FakeGatewayConfig, FakeOpenMemoryClient

        conn = db_conn_prefixed_committed
        dsn = two_phase_env["dsn"]
        schemas = two_phase_env["schemas"]
        governance_schema = schemas["governance"]

        real_adapter = LogbookAdapter(dsn=dsn)
        fake_client = FakeOpenMemoryClient()
        fake_client.configure_store_success(memory_id="mem_corr_test")

        fake_config = FakeGatewayConfig()
        fake_config.postgres_dsn = dsn

        deps = GatewayDeps.for_testing(
            config=fake_config,
            logbook_adapter=real_adapter,
            openmemory_client=fake_client,
        )

        # 生成两个不同的 correlation_id
        corr_id_1 = f"corr-{uuid.uuid4().hex[:16]}"
        corr_id_2 = f"corr-{uuid.uuid4().hex[:16]}"

        # 调用两次
        result1 = await memory_store_impl(
            payload_md="测试内容 1",
            target_space="private:user_corr_test",
            correlation_id=corr_id_1,
            deps=deps,
        )

        result2 = await memory_store_impl(
            payload_md="测试内容 2",
            target_space="private:user_corr_test",
            correlation_id=corr_id_2,
            deps=deps,
        )

        # 验证响应中的 correlation_id
        assert result1.correlation_id == corr_id_1, "result1 correlation_id 应一致"
        assert result2.correlation_id == corr_id_2, "result2 correlation_id 应一致"

        # 验证审计记录中的 correlation_id（使用显式 schema 限定名）
        audit1 = query_audit_by_correlation_id(conn, corr_id_1, governance_schema)
        audit2 = query_audit_by_correlation_id(conn, corr_id_2, governance_schema)

        assert audit1 is not None, f"应存在 correlation_id={corr_id_1} 的审计记录"
        assert audit2 is not None, f"应存在 correlation_id={corr_id_2} 的审计记录"
        assert audit1["audit_id"] != audit2["audit_id"], "两条审计记录应有不同的 audit_id"

    @pytest.mark.asyncio
    async def test_correlation_id_in_response_on_error(
        self, two_phase_env, db_conn_prefixed_committed, test_correlation_id
    ):
        """
        验证即使在错误路径下，响应中也包含正确的 correlation_id
        """
        from engram.gateway.di import GatewayDeps
        from engram.gateway.handlers.memory_store import memory_store_impl
        from engram.gateway.logbook_adapter import LogbookAdapter
        from tests.gateway.fakes import FakeGatewayConfig, FakeOpenMemoryClient

        dsn = two_phase_env["dsn"]

        real_adapter = LogbookAdapter(dsn=dsn)
        fake_client = FakeOpenMemoryClient()
        fake_client.configure_store_connection_error("网络错误")

        fake_config = FakeGatewayConfig()
        fake_config.postgres_dsn = dsn

        deps = GatewayDeps.for_testing(
            config=fake_config,
            logbook_adapter=real_adapter,
            openmemory_client=fake_client,
        )

        result = await memory_store_impl(
            payload_md="测试内容 - 错误路径",
            target_space="private:user_error_test",
            correlation_id=test_correlation_id,
            deps=deps,
        )

        # 验证响应中的 correlation_id（即使失败也应存在）
        assert result.correlation_id == test_correlation_id, (
            f"错误响应中的 correlation_id 应一致，实际: {result.correlation_id}"
        )


class TestTwoPhaseAuditIntendedAction:
    """测试两阶段审计的 intended_action 记录

    覆盖场景：
    1. deferred 路径时 evidence_refs_json 包含 intended_action
    2. intended_action 反映原策略决策
    """

    @pytest.mark.asyncio
    async def test_deferred_path_records_intended_action(
        self, two_phase_env, db_conn_prefixed_committed, test_correlation_id
    ):
        """
        验证 deferred 路径记录 intended_action
        """
        from engram.gateway.di import GatewayDeps
        from engram.gateway.handlers.memory_store import memory_store_impl
        from engram.gateway.logbook_adapter import LogbookAdapter
        from tests.gateway.fakes import FakeGatewayConfig, FakeOpenMemoryClient

        conn = db_conn_prefixed_committed
        dsn = two_phase_env["dsn"]
        schemas = two_phase_env["schemas"]
        governance_schema = schemas["governance"]

        real_adapter = LogbookAdapter(dsn=dsn)
        fake_client = FakeOpenMemoryClient()
        fake_client.configure_store_connection_error("OpenMemory 不可用")

        fake_config = FakeGatewayConfig()
        fake_config.postgres_dsn = dsn

        deps = GatewayDeps.for_testing(
            config=fake_config,
            logbook_adapter=real_adapter,
            openmemory_client=fake_client,
        )

        await memory_store_impl(
            payload_md="测试内容 - intended_action",
            target_space="private:user_intended_test",
            correlation_id=test_correlation_id,
            deps=deps,
        )

        # 查询审计记录（使用显式 schema 限定名）
        audit = query_audit_by_correlation_id(conn, test_correlation_id, governance_schema)
        assert audit is not None

        # 验证 evidence_refs_json 包含 intended_action
        evidence = audit["evidence_refs_json"]
        assert evidence is not None, "evidence_refs_json 不应为空"
        assert "intended_action" in evidence, (
            f"evidence_refs_json 应包含 intended_action，实际: {evidence.keys()}"
        )
        # intended_action 应为 allow 或 redirect（取决于策略决策）
        assert evidence["intended_action"] in ("allow", "redirect"), (
            f"intended_action 应为 allow 或 redirect，实际: {evidence['intended_action']}"
        )


class TestTwoPhaseAuditVerificationSmokeTest:
    """
    两阶段审计验收冒烟测试

    在 FULL profile 下作为验收项，验证完整的两阶段审计流程。
    """

    @pytest.mark.asyncio
    async def test_full_two_phase_audit_flow(
        self, two_phase_env, db_conn_prefixed_committed, test_correlation_id
    ):
        """
        完整流程验收测试：

        1. 调用 memory_store_impl（success 路径）
        2. 验证审计记录状态转换完整
        3. 验证所有必需字段存在
        """
        from tests.gateway.gate_helpers import ProfileType, require_profile

        require_profile(ProfileType.FULL, step="db_invariants")

        from engram.gateway.di import GatewayDeps
        from engram.gateway.handlers.memory_store import memory_store_impl
        from engram.gateway.logbook_adapter import LogbookAdapter
        from tests.gateway.fakes import FakeGatewayConfig, FakeOpenMemoryClient

        conn = db_conn_prefixed_committed
        dsn = two_phase_env["dsn"]
        schemas = two_phase_env["schemas"]
        governance_schema = schemas["governance"]

        real_adapter = LogbookAdapter(dsn=dsn)
        fake_client = FakeOpenMemoryClient()
        fake_client.configure_store_success(memory_id="mem_acceptance_001")

        fake_config = FakeGatewayConfig()
        fake_config.postgres_dsn = dsn

        deps = GatewayDeps.for_testing(
            config=fake_config,
            logbook_adapter=real_adapter,
            openmemory_client=fake_client,
        )

        result = await memory_store_impl(
            payload_md="验收测试内容",
            target_space="private:user_acceptance_test",
            correlation_id=test_correlation_id,
            deps=deps,
        )

        # 验证响应
        assert result.ok is True, "验收测试应成功"
        assert result.correlation_id == test_correlation_id

        # 查询审计记录（使用显式 schema 限定名）
        audit = query_audit_by_correlation_id(conn, test_correlation_id, governance_schema)
        assert audit is not None, "应存在审计记录"

        # 验证审计记录完整性
        assert audit["status"] == "success", "最终状态应为 success"
        assert audit["correlation_id"] == test_correlation_id
        assert audit["target_space"] == "private:user_acceptance_test"
        assert audit["action"] in ("allow", "redirect")
        assert audit["payload_sha"] is not None, "payload_sha 不应为空"
        assert audit["evidence_refs_json"] is not None, "evidence_refs_json 不应为空"
        assert audit["created_at"] is not None, "created_at 不应为空"
        assert audit["updated_at"] is not None, "updated_at 不应为空（finalize 时更新）"


class TestEvidenceRefsJsonCrossStageQuery:
    """
    测试 evidence_refs_json 跨阶段关联查询能力

    验证：
    1. deferred 的同步审计可用 (evidence_refs_json->>'outbox_id')::int 查询
    2. success 可用 evidence_refs_json->>'memory_id' 查询
    """

    @pytest.mark.asyncio
    async def test_success_path_memory_id_queryable(
        self, two_phase_env, db_conn_prefixed_committed, test_correlation_id
    ):
        """
        验证 success 路径的 memory_id 可通过 jsonb 操作符查询

        契约：success 审计的 evidence_refs_json 顶层必须包含 memory_id，
        且可通过 evidence_refs_json->>'memory_id' 查询。
        """
        from engram.gateway.di import GatewayDeps
        from engram.gateway.handlers.memory_store import memory_store_impl
        from engram.gateway.logbook_adapter import LogbookAdapter
        from tests.gateway.fakes import FakeGatewayConfig, FakeOpenMemoryClient

        conn = db_conn_prefixed_committed
        dsn = two_phase_env["dsn"]
        schemas = two_phase_env["schemas"]
        governance_schema = schemas["governance"]

        real_adapter = LogbookAdapter(dsn=dsn)
        fake_client = FakeOpenMemoryClient()
        expected_memory_id = "mem_query_test_001"
        fake_client.configure_store_success(memory_id=expected_memory_id)

        fake_config = FakeGatewayConfig()
        fake_config.postgres_dsn = dsn

        deps = GatewayDeps.for_testing(
            config=fake_config,
            logbook_adapter=real_adapter,
            openmemory_client=fake_client,
        )

        result = await memory_store_impl(
            payload_md="测试内容 - memory_id 查询",
            target_space="private:user_memory_query_test",
            correlation_id=test_correlation_id,
            deps=deps,
        )

        assert result.ok is True, "memory_store 应成功"
        assert result.memory_id == expected_memory_id

        # 验证可通过 jsonb 操作符查询 memory_id（使用显式 schema 限定名）
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT audit_id, correlation_id, status, evidence_refs_json
                FROM {governance_schema}.write_audit
                WHERE evidence_refs_json->>'memory_id' = %s
                """,
                (expected_memory_id,),
            )
            row = cur.fetchone()

            assert row is not None, (
                f"应能通过 evidence_refs_json->>'memory_id' 查询到审计记录，"
                f"expected_memory_id={expected_memory_id}"
            )
            assert row[1] == test_correlation_id, "查询到的 correlation_id 应匹配"
            assert row[2] == "success", "查询到的 status 应为 success"
            assert row[3].get("memory_id") == expected_memory_id, (
                "evidence_refs_json.memory_id 应匹配"
            )

    @pytest.mark.asyncio
    async def test_deferred_path_outbox_id_queryable(
        self, two_phase_env, db_conn_prefixed_committed, test_correlation_id
    ):
        """
        验证 deferred 路径的 outbox_id 可通过 jsonb 操作符查询

        契约：deferred/redirected 审计的 evidence_refs_json 顶层必须包含 outbox_id，
        且可通过 (evidence_refs_json->>'outbox_id')::int 查询。
        """
        from engram.gateway.di import GatewayDeps
        from engram.gateway.handlers.memory_store import memory_store_impl
        from engram.gateway.logbook_adapter import LogbookAdapter
        from tests.gateway.fakes import FakeGatewayConfig, FakeOpenMemoryClient

        conn = db_conn_prefixed_committed
        dsn = two_phase_env["dsn"]
        schemas = two_phase_env["schemas"]
        governance_schema = schemas["governance"]

        real_adapter = LogbookAdapter(dsn=dsn)
        fake_client = FakeOpenMemoryClient()
        fake_client.configure_store_connection_error("OpenMemory 不可用")

        fake_config = FakeGatewayConfig()
        fake_config.postgres_dsn = dsn

        deps = GatewayDeps.for_testing(
            config=fake_config,
            logbook_adapter=real_adapter,
            openmemory_client=fake_client,
        )

        result = await memory_store_impl(
            payload_md="测试内容 - outbox_id 查询",
            target_space="private:user_outbox_query_test",
            correlation_id=test_correlation_id,
            deps=deps,
        )

        assert result.ok is False, "OpenMemory 失败时 ok 应为 False"
        assert result.action == "deferred", "action 应为 deferred"
        assert result.outbox_id is not None, "outbox_id 不应为空"

        expected_outbox_id = result.outbox_id

        # 验证可通过 jsonb 操作符查询 outbox_id（整数转换，使用显式 schema 限定名）
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT audit_id, correlation_id, status, evidence_refs_json
                FROM {governance_schema}.write_audit
                WHERE (evidence_refs_json->>'outbox_id')::int = %s
                """,
                (expected_outbox_id,),
            )
            row = cur.fetchone()

            assert row is not None, (
                f"应能通过 (evidence_refs_json->>'outbox_id')::int 查询到审计记录，"
                f"expected_outbox_id={expected_outbox_id}"
            )
            assert row[1] == test_correlation_id, "查询到的 correlation_id 应匹配"
            assert row[2] == "redirected", "查询到的 status 应为 redirected"
            assert row[3].get("outbox_id") == expected_outbox_id, (
                "evidence_refs_json.outbox_id 应匹配"
            )

    @pytest.mark.asyncio
    async def test_deferred_path_intended_action_queryable(
        self, two_phase_env, db_conn_prefixed_committed, test_correlation_id
    ):
        """
        验证 deferred 路径的 intended_action 可通过 jsonb 操作符查询

        契约：deferred/redirected 审计的 evidence_refs_json 顶层必须包含 intended_action。
        """
        from engram.gateway.di import GatewayDeps
        from engram.gateway.handlers.memory_store import memory_store_impl
        from engram.gateway.logbook_adapter import LogbookAdapter
        from tests.gateway.fakes import FakeGatewayConfig, FakeOpenMemoryClient

        conn = db_conn_prefixed_committed
        dsn = two_phase_env["dsn"]
        schemas = two_phase_env["schemas"]
        governance_schema = schemas["governance"]

        real_adapter = LogbookAdapter(dsn=dsn)
        fake_client = FakeOpenMemoryClient()
        fake_client.configure_store_connection_error("OpenMemory 连接失败")

        fake_config = FakeGatewayConfig()
        fake_config.postgres_dsn = dsn

        deps = GatewayDeps.for_testing(
            config=fake_config,
            logbook_adapter=real_adapter,
            openmemory_client=fake_client,
        )

        result = await memory_store_impl(
            payload_md="测试内容 - intended_action 查询",
            target_space="private:user_intended_action_query_test",
            correlation_id=test_correlation_id,
            deps=deps,
        )

        assert result.ok is False
        assert result.action == "deferred"

        expected_outbox_id = result.outbox_id

        # 验证 intended_action 存在且可查询（使用显式 schema 限定名）
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT evidence_refs_json->>'intended_action'
                FROM {governance_schema}.write_audit
                WHERE (evidence_refs_json->>'outbox_id')::int = %s
                """,
                (expected_outbox_id,),
            )
            row = cur.fetchone()

            assert row is not None, "应能查询到审计记录"
            intended_action = row[0]
            assert intended_action in ("allow", "redirect"), (
                f"intended_action 应为 allow 或 redirect，实际: {intended_action}"
            )


class TestTwoPhaseAuditRejectPath:
    """
    测试两阶段审计的 REJECT/REDIRECT 路径

    覆盖场景：
    1. 策略重定向（team_write_enabled=False）
    2. Strict 模式 evidence 校验失败（缺少 sha256）
    3. correlation_id 一致性追踪
    4. 验证顶层 evidence_refs_json 字段满足查询契约

    契约验证（docs/contracts/gateway_audit_evidence_correlation_contract.md）：
    - evidence_refs_json 顶层必须包含：source, correlation_id, payload_sha
    - action='reject' 时，status 必须不为 'pending'
    """

    @pytest.mark.asyncio
    async def test_policy_redirect_single_audit_record(
        self, two_phase_env, db_conn_prefixed_committed, test_correlation_id
    ):
        """
        测试策略重定向场景：只产生一条 redirect 审计记录

        场景：team_write_enabled=False，策略判定为 REDIRECT
        验证：
        1. 用 SQL 统计该 correlation_id 的 write_audit 行数应为 1
        2. 断言 status != 'pending' 且 action='redirect'
        3. 验证顶层 evidence_refs_json 包含 source/correlation_id/payload_sha
        """
        from engram.gateway.di import GatewayDeps
        from engram.gateway.handlers.memory_store import memory_store_impl
        from engram.gateway.logbook_adapter import LogbookAdapter
        from tests.gateway.fakes import FakeGatewayConfig, FakeOpenMemoryClient

        conn = db_conn_prefixed_committed
        dsn = two_phase_env["dsn"]
        schemas = two_phase_env["schemas"]
        governance_schema = schemas["governance"]

        # 创建真实 LogbookAdapter 依赖
        real_adapter = LogbookAdapter(dsn=dsn)

        # 创建 Fake OpenMemory Client（redirect 路径仍会调用）
        fake_client = FakeOpenMemoryClient()
        fake_client.configure_store_success(memory_id="should_not_be_called")

        # 创建 Fake Config
        fake_config = FakeGatewayConfig()
        fake_config.postgres_dsn = dsn

        # 配置 settings：禁用团队写入以触发策略重定向
        # 注意：使用 real_adapter，需要通过数据库配置 settings
        # 先更新治理设置
        real_adapter.update_governance_settings(
            project_key=fake_config.project_key,
            team_write_enabled=False,  # 禁用团队写入
            policy_json={},
        )

        deps = GatewayDeps.for_testing(
            config=fake_config,
            logbook_adapter=real_adapter,
            openmemory_client=fake_client,
        )

        # 调用 memory_store_impl
        result = await memory_store_impl(
            payload_md="测试内容 - 策略重定向路径",
            target_space="team:restricted_project",  # 使用 team: 前缀触发 team_write 检查
            correlation_id=test_correlation_id,
            deps=deps,
        )

        # 验证响应：策略重定向
        assert result.ok is True, f"策略重定向时 ok 应为 True，实际: {result.ok}"
        assert result.action == "redirect", f"action 应为 redirect，实际: {result.action}"
        assert result.correlation_id == test_correlation_id, "correlation_id 应一致"

        # 使用辅助断言验证审计记录（无 pending 状态）
        audit = assert_two_phase_finalized(conn, test_correlation_id, "success", governance_schema)
        assert audit["action"] == "redirect", f"action 应为 redirect，实际: {audit['action']}"

        # 使用辅助断言验证 evidence_refs_json 顶层契约
        evidence = audit["evidence_refs_json"]
        assert_evidence_refs_top_level_contract(evidence, expected_source="gateway")

        # 使用辅助断言验证 gateway_event 子结构
        gateway_event = assert_gateway_event_substructure(evidence, test_correlation_id)

        # 验证 gateway_event 中的 payload_sha（与顶层一致）
        assert gateway_event.get("payload_sha") == evidence["payload_sha"], (
            "gateway_event.payload_sha 应与顶层 payload_sha 一致"
        )

    @pytest.mark.asyncio
    async def test_strict_evidence_validation_reject(
        self, two_phase_env, db_conn_prefixed_committed, test_correlation_id
    ):
        """
        测试 strict 模式 evidence 校验失败场景

        场景：evidence_mode=strict，evidence 缺少 sha256
        验证：
        1. 用 SQL 统计该 correlation_id 的 write_audit 行数应为 1
        2. 断言 status != 'pending' 且 action='reject'
        3. 验证 reason 包含 EVIDENCE_VALIDATION_FAILED
        4. 验证顶层 evidence_refs_json 字段满足查询契约
        """
        from engram.gateway.di import GatewayDeps
        from engram.gateway.handlers.memory_store import memory_store_impl
        from engram.gateway.logbook_adapter import LogbookAdapter
        from tests.gateway.fakes import FakeGatewayConfig, FakeOpenMemoryClient

        conn = db_conn_prefixed_committed
        dsn = two_phase_env["dsn"]
        schemas = two_phase_env["schemas"]
        governance_schema = schemas["governance"]

        # 创建真实 LogbookAdapter 依赖
        real_adapter = LogbookAdapter(dsn=dsn)

        # 创建 Fake OpenMemory Client（不会被调用，因为 evidence 校验失败）
        fake_client = FakeOpenMemoryClient()
        fake_client.configure_store_success(memory_id="should_not_be_called")

        # 创建 Fake Config
        fake_config = FakeGatewayConfig()
        fake_config.postgres_dsn = dsn

        # 配置 settings：启用 strict 模式
        real_adapter.update_governance_settings(
            project_key=fake_config.project_key,
            team_write_enabled=True,
            policy_json={"evidence_mode": "strict"},  # 启用 strict 模式
        )

        deps = GatewayDeps.for_testing(
            config=fake_config,
            logbook_adapter=real_adapter,
            openmemory_client=fake_client,
        )

        # 构造缺少 sha256 的 evidence（strict 模式下会校验失败）
        invalid_evidence = [{"uri": "memory://attachments/test/placeholder_without_sha256"}]

        # 调用 memory_store_impl
        result = await memory_store_impl(
            payload_md="测试内容 - strict 模式校验失败",
            target_space="team:test_project",
            evidence=invalid_evidence,  # 传入无效 evidence
            correlation_id=test_correlation_id,
            deps=deps,
        )

        # 验证响应：strict 模式校验失败
        assert result.ok is False, f"strict 校验失败时 ok 应为 False，实际: {result.ok}"
        assert result.action == "reject", f"action 应为 reject，实际: {result.action}"
        assert result.correlation_id == test_correlation_id, "correlation_id 应一致"
        assert "EVIDENCE_VALIDATION_FAILED" in (result.message or ""), (
            f"message 应包含 EVIDENCE_VALIDATION_FAILED，实际: {result.message}"
        )

        # 使用辅助断言验证单阶段 reject（无 pending 状态）
        audit = assert_single_stage_reject_no_pending(conn, test_correlation_id, governance_schema)

        # 验证：reason 包含 EVIDENCE_VALIDATION_FAILED
        reason = audit["reason"]
        assert reason is not None, "reason 不应为空"
        assert "EVIDENCE_VALIDATION_FAILED" in reason, (
            f"reason 应包含 EVIDENCE_VALIDATION_FAILED，实际: {reason}"
        )

        # 使用辅助断言验证 evidence_refs_json 顶层契约
        evidence_refs = audit["evidence_refs_json"]
        assert_evidence_refs_top_level_contract(evidence_refs, expected_source="gateway")

        # 使用辅助断言验证 gateway_event 子结构
        gateway_event = assert_gateway_event_substructure(evidence_refs, test_correlation_id)

        # 验证 gateway_event 中的 payload_sha（与顶层一致）
        assert gateway_event.get("payload_sha") == evidence_refs["payload_sha"], (
            "gateway_event.payload_sha 应与顶层 payload_sha 一致"
        )

        # 验证 validation 子结构（strict 模式特有）
        assert "validation" in gateway_event, "strict 模式 gateway_event 必须包含 validation 子结构"
        validation = gateway_event["validation"]
        assert "evidence_validation" in validation, "validation 必须包含 evidence_validation"
        ev_val = validation["evidence_validation"]
        assert ev_val.get("is_valid") is False, (
            f"strict 校验失败时 is_valid 应为 False，实际: {ev_val.get('is_valid')}"
        )

    @pytest.mark.asyncio
    async def test_redirect_path_evidence_refs_json_queryable(
        self, two_phase_env, db_conn_prefixed_committed, test_correlation_id
    ):
        """
        测试 redirect 路径的 evidence_refs_json 可通过 jsonb 操作符查询

        契约：redirect 审计的 evidence_refs_json 顶层 gateway_event 必须包含
        correlation_id/payload_sha，且可通过 jsonb 操作符查询。
        """
        from engram.gateway.di import GatewayDeps
        from engram.gateway.handlers.memory_store import memory_store_impl
        from engram.gateway.logbook_adapter import LogbookAdapter
        from tests.gateway.fakes import FakeGatewayConfig, FakeOpenMemoryClient

        conn = db_conn_prefixed_committed
        dsn = two_phase_env["dsn"]
        schemas = two_phase_env["schemas"]
        governance_schema = schemas["governance"]

        real_adapter = LogbookAdapter(dsn=dsn)
        fake_client = FakeOpenMemoryClient()
        fake_config = FakeGatewayConfig()
        fake_config.postgres_dsn = dsn

        # 配置 settings：禁用团队写入以触发策略重定向
        real_adapter.update_governance_settings(
            project_key=fake_config.project_key,
            team_write_enabled=False,
            policy_json={},
        )

        deps = GatewayDeps.for_testing(
            config=fake_config,
            logbook_adapter=real_adapter,
            openmemory_client=fake_client,
        )

        await memory_store_impl(
            payload_md="测试内容 - redirect 路径 jsonb 查询",
            target_space="team:redirect_query_test",
            correlation_id=test_correlation_id,
            deps=deps,
        )

        # ========== 验证顶层字段可通过 jsonb 操作符查询 ==========
        # 这是 _assert_reconcile_outbox_contract 期望的契约

        # 1. 通过顶层 correlation_id 查询
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT audit_id, action, status, evidence_refs_json
                FROM {governance_schema}.write_audit
                WHERE evidence_refs_json->>'correlation_id' = %s
                """,
                (test_correlation_id,),
            )
            row = cur.fetchone()

            assert row is not None, (
                f"应能通过 evidence_refs_json->>'correlation_id' 查询到审计记录，"
                f"correlation_id={test_correlation_id}"
            )
            assert row[1] == "redirect", f"查询到的 action 应为 redirect，实际: {row[1]}"
            assert row[2] != "pending", f"查询到的 status 不应为 pending，实际: {row[2]}"

            evidence_refs = row[3]

            # 验证顶层字段
            assert evidence_refs.get("source") == "gateway", (
                f"顶层 source 应为 gateway，实际: {evidence_refs.get('source')}"
            )
            assert evidence_refs.get("correlation_id") == test_correlation_id, (
                f"顶层 correlation_id 应匹配，实际: {evidence_refs.get('correlation_id')}"
            )
            assert evidence_refs.get("payload_sha") is not None, "顶层 payload_sha 不应为空"

            # 验证 gateway_event 子结构
            assert "gateway_event" in evidence_refs
            gateway_event = evidence_refs["gateway_event"]
            assert gateway_event["source"] == "gateway"
            assert gateway_event["correlation_id"] == test_correlation_id
            assert gateway_event["payload_sha"] is not None

        # 2. 通过顶层 source 查询（验证可组合查询）
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COUNT(*)
                FROM {governance_schema}.write_audit
                WHERE evidence_refs_json->>'source' = 'gateway'
                  AND evidence_refs_json->>'correlation_id' = %s
                """,
                (test_correlation_id,),
            )
            count = cur.fetchone()[0]
            assert count == 1, (
                f"通过顶层 source + correlation_id 组合查询应返回 1 条记录，实际: {count}"
            )

    @pytest.mark.asyncio
    async def test_redirect_path_full_profile_verification(
        self, two_phase_env, db_conn_prefixed_committed, test_correlation_id
    ):
        """
        FULL profile 验收测试：完整验证 redirect 路径的审计契约

        验证：
        1. 策略重定向只产生一条审计记录
        2. status 为最终状态（非 pending）
        3. evidence_refs_json 包含完整的 gateway_event 结构
        4. 所有必需字段存在且格式正确
        """
        from tests.gateway.gate_helpers import ProfileType, require_profile

        require_profile(ProfileType.FULL, step="db_invariants")

        from engram.gateway.di import GatewayDeps
        from engram.gateway.handlers.memory_store import memory_store_impl
        from engram.gateway.logbook_adapter import LogbookAdapter
        from tests.gateway.fakes import FakeGatewayConfig, FakeOpenMemoryClient

        conn = db_conn_prefixed_committed
        dsn = two_phase_env["dsn"]
        schemas = two_phase_env["schemas"]
        governance_schema = schemas["governance"]

        real_adapter = LogbookAdapter(dsn=dsn)
        fake_client = FakeOpenMemoryClient()
        fake_config = FakeGatewayConfig()
        fake_config.postgres_dsn = dsn

        # 配置 settings：禁用团队写入（触发重定向）
        real_adapter.update_governance_settings(
            project_key=fake_config.project_key,
            team_write_enabled=False,
            policy_json={},
        )

        deps = GatewayDeps.for_testing(
            config=fake_config,
            logbook_adapter=real_adapter,
            openmemory_client=fake_client,
        )

        result = await memory_store_impl(
            payload_md="FULL profile 验收测试 - redirect 路径",
            target_space="team:full_profile_redirect_test",
            correlation_id=test_correlation_id,
            deps=deps,
        )

        # 验证响应
        assert result.ok is True, "验收测试 redirect 路径应返回 ok=True"
        assert result.action == "redirect"
        assert result.correlation_id == test_correlation_id

        # 使用辅助断言验证审计记录
        audit = assert_two_phase_finalized(conn, test_correlation_id, "success", governance_schema)
        assert audit["correlation_id"] == test_correlation_id
        assert audit["payload_sha"] is not None, "payload_sha 不应为空"
        assert audit["created_at"] is not None, "created_at 不应为空"

        # 使用辅助断言验证 evidence_refs_json 顶层契约
        evidence = audit["evidence_refs_json"]
        assert_evidence_refs_top_level_contract(evidence, expected_source="gateway")

        # 使用辅助断言验证 gateway_event 子结构
        gateway_event = assert_gateway_event_substructure(evidence, test_correlation_id)

        # 验证 gateway_event 必需字段
        required_fields = [
            "schema_version",
            "source",
            "operation",
            "correlation_id",
            "decision",
            "payload_sha",
        ]
        for field in required_fields:
            assert field in gateway_event, f"gateway_event 缺少必需字段: {field}"

        # 验证 gateway_event 字段与顶层一致
        assert gateway_event["payload_sha"] == evidence["payload_sha"], (
            "gateway_event.payload_sha 应与顶层 payload_sha 一致"
        )

        # 验证 decision 子结构
        decision = gateway_event["decision"]
        assert decision["action"] == "redirect"
        assert decision["reason"] is not None


# =============================================================================
# 两阶段审计冒烟测试套件（FULL Profile 强制执行）
# =============================================================================
#
# 本测试类作为验收矩阵中引用的冒烟测试套件，覆盖：
# 1. OpenMemory 成功 finalize success
# 2. OpenMemory 失败 deferred + outbox 入队
# 3. （可选）reconcile 补写审计
#
# 在 FULL profile 下强制执行（缺能力 fail 而非 skip）
#
# 验收矩阵引用: docs/acceptance/00_acceptance_matrix.md
# - 审计两阶段写入验收矩阵（Audit Two-Phase）节
# =============================================================================


@pytest.mark.gate_profile("full")
class TestTwoPhaseAuditSmokeTestFull:
    """
    两阶段审计冒烟测试套件（FULL Profile 强制执行）

    验收矩阵覆盖点：
    1. memory_store 成功写入 → 审计 pending→success
    2. memory_store 失败入队 → 审计 pending→redirected，outbox 入队
    3. reconcile 补写审计 → outbox sent 状态的审计补写

    测试策略：
    - 使用 require_profile(ProfileType.FULL, step="db_invariants")
    - 缺少 DB 能力时 pytest.fail（而非 skip）
    - 复用 prefixed schema fixture 实现测试隔离
    """

    @pytest.mark.asyncio
    async def test_smoke_openmemory_success_finalize_success(
        self, two_phase_env, db_conn_prefixed_committed, test_correlation_id
    ):
        """
        冒烟测试 1: OpenMemory 成功 → finalize success

        验证点：
        1. 调用 memory_store_impl（OpenMemory 成功）
        2. 审计状态 pending → success
        3. evidence_refs_json 包含 memory_id
        4. 只有一条审计记录

        验收矩阵引用：memory_store 成功写入 → 审计 pending→success
        """
        from tests.gateway.gate_helpers import ProfileType, require_profile

        require_profile(ProfileType.FULL, step="db_invariants")

        from engram.gateway.di import GatewayDeps
        from engram.gateway.handlers.memory_store import memory_store_impl
        from engram.gateway.logbook_adapter import LogbookAdapter
        from tests.gateway.fakes import FakeGatewayConfig, FakeOpenMemoryClient

        conn = db_conn_prefixed_committed
        dsn = two_phase_env["dsn"]
        schemas = two_phase_env["schemas"]
        governance_schema = schemas["governance"]

        # 配置依赖
        real_adapter = LogbookAdapter(dsn=dsn)
        fake_client = FakeOpenMemoryClient()
        fake_client.configure_store_success(memory_id="mem_smoke_success_001")

        fake_config = FakeGatewayConfig()
        fake_config.postgres_dsn = dsn

        deps = GatewayDeps.for_testing(
            config=fake_config,
            logbook_adapter=real_adapter,
            openmemory_client=fake_client,
        )

        # 执行
        result = await memory_store_impl(
            payload_md="冒烟测试内容 - OpenMemory 成功",
            target_space="private:user_smoke_success",
            correlation_id=test_correlation_id,
            deps=deps,
        )

        # 验证响应
        assert result.ok is True, f"OpenMemory 成功时 ok 应为 True，实际: {result}"
        assert result.memory_id == "mem_smoke_success_001", (
            f"memory_id 应为 mem_smoke_success_001，实际: {result.memory_id}"
        )
        assert result.correlation_id == test_correlation_id, "correlation_id 应一致"

        # 验证审计状态流转
        audit = assert_two_phase_finalized(conn, test_correlation_id, "success", governance_schema)

        # 验证 evidence_refs_json 包含 memory_id
        evidence = audit["evidence_refs_json"]
        assert evidence is not None, "evidence_refs_json 不应为空"
        assert evidence.get("memory_id") == "mem_smoke_success_001", (
            f"evidence_refs_json 应包含 memory_id=mem_smoke_success_001，实际: {evidence}"
        )

    @pytest.mark.asyncio
    async def test_smoke_openmemory_failure_deferred_outbox_enqueue(
        self, two_phase_env, db_conn_prefixed_committed, test_correlation_id
    ):
        """
        冒烟测试 2: OpenMemory 失败 → deferred + outbox 入队

        验证点：
        1. 调用 memory_store_impl（OpenMemory 连接失败）
        2. 审计状态 pending → redirected
        3. reason 包含 :outbox:<id>
        4. outbox_memory 表中存在对应记录
        5. evidence_refs_json 包含 outbox_id

        验收矩阵引用：memory_store 失败入队 → 审计 pending→redirected，outbox 入队
        """
        from tests.gateway.gate_helpers import ProfileType, require_profile

        require_profile(ProfileType.FULL, step="db_invariants")

        from engram.gateway.di import GatewayDeps
        from engram.gateway.handlers.memory_store import memory_store_impl
        from engram.gateway.logbook_adapter import LogbookAdapter
        from tests.gateway.fakes import FakeGatewayConfig, FakeOpenMemoryClient

        conn = db_conn_prefixed_committed
        dsn = two_phase_env["dsn"]
        schemas = two_phase_env["schemas"]
        governance_schema = schemas["governance"]
        logbook_schema = schemas["logbook"]

        # 配置依赖：OpenMemory 连接失败
        real_adapter = LogbookAdapter(dsn=dsn)
        fake_client = FakeOpenMemoryClient()
        fake_client.configure_store_connection_error("OpenMemory 不可用 - 冒烟测试")

        fake_config = FakeGatewayConfig()
        fake_config.postgres_dsn = dsn

        deps = GatewayDeps.for_testing(
            config=fake_config,
            logbook_adapter=real_adapter,
            openmemory_client=fake_client,
        )

        # 执行
        result = await memory_store_impl(
            payload_md="冒烟测试内容 - OpenMemory 失败",
            target_space="private:user_smoke_deferred",
            correlation_id=test_correlation_id,
            deps=deps,
        )

        # 验证响应
        assert result.ok is False, f"OpenMemory 失败时 ok 应为 False，实际: {result.ok}"
        assert result.action == "deferred", f"action 应为 deferred，实际: {result.action}"
        assert result.outbox_id is not None, "outbox_id 不应为空"
        assert result.correlation_id == test_correlation_id, "correlation_id 应一致"

        outbox_id = result.outbox_id

        # 验证审计状态流转
        audit = assert_two_phase_finalized(
            conn, test_correlation_id, "redirected", governance_schema
        )

        # 验证 reason 包含 :outbox:<id>
        reason = audit["reason"]
        assert reason is not None, "reason 不应为空"
        assert f":outbox:{outbox_id}" in reason, (
            f"reason 应包含 ':outbox:{outbox_id}'，实际: {reason}"
        )

        # 验证 outbox_memory 表中存在对应记录
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT outbox_id, target_space, status, payload_md
                FROM {logbook_schema}.outbox_memory
                WHERE outbox_id = %s
                """,
                (outbox_id,),
            )
            row = cur.fetchone()
            assert row is not None, f"应存在 outbox_id={outbox_id} 的记录"
            assert row[1] == "private:user_smoke_deferred", f"target_space 应匹配，实际: {row[1]}"
            assert row[2] == "pending", f"outbox status 应为 pending，实际: {row[2]}"

        # 验证 evidence_refs_json 包含 outbox_id
        assert_evidence_refs_queryable_by_outbox_id(
            conn, outbox_id, governance_schema, expected_fields={"outbox_id": outbox_id}
        )

    @pytest.mark.asyncio
    async def test_smoke_reconcile_outbox_flush_audit(
        self, two_phase_env, db_conn_prefixed_committed, test_correlation_id
    ):
        """
        冒烟测试 3: Reconcile 补写审计（outbox sent 状态）

        验证点：
        1. 手动插入 sent 状态的 outbox 记录（无对应审计）
        2. 运行 reconcile
        3. 验证补写了 outbox_flush_success 审计
        4. evidence_refs_json 包含 outbox_id 且 source 为 reconcile_outbox

        验收矩阵引用：Outbox flush 成功 → outbox pending→sent，审计 outbox_flush_success

        注意：此测试模拟 outbox_worker 处理后但审计未写入的场景，
        通过 reconcile 补写审计来保证最终一致性。
        """
        from tests.gateway.gate_helpers import ProfileType, require_profile

        require_profile(ProfileType.FULL, step="db_invariants")

        from engram.gateway.reconcile_outbox import ReconcileConfig, run_reconcile

        conn = db_conn_prefixed_committed
        dsn = two_phase_env["dsn"]
        schemas = two_phase_env["schemas"]
        governance_schema = schemas["governance"]
        logbook_schema = schemas["logbook"]

        # 1. 手动插入 sent 状态的 outbox 记录（模拟 worker 处理完成但审计丢失）
        target_space = "private:user_smoke_reconcile"
        payload_sha = f"sha256_{test_correlation_id[-16:]}"

        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {logbook_schema}.outbox_memory
                    (target_space, payload_md, payload_sha, status, correlation_id)
                VALUES (%s, %s, %s, 'sent', %s)
                RETURNING outbox_id
                """,
                (
                    target_space,
                    "冒烟测试内容 - reconcile 补写",
                    payload_sha,
                    test_correlation_id,
                ),
            )
            outbox_id = cur.fetchone()[0]
        conn.commit()

        # 2. 验证 sent 记录存在但无审计
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COUNT(*)
                FROM {governance_schema}.write_audit
                WHERE (evidence_refs_json->>'outbox_id')::int = %s
                """,
                (outbox_id,),
            )
            count_before = cur.fetchone()[0]
            assert count_before == 0, f"reconcile 前不应有 outbox_id={outbox_id} 的审计记录"

        # 3. 运行 reconcile
        config = ReconcileConfig(dsn=dsn, auto_fix=True)
        result = run_reconcile(config)

        # 4. 验证 reconcile 检测并修复了缺失审计
        assert result.sent_missing_audit_fixed > 0 or result.sent_missing_audit_detected > 0, (
            f"reconcile 应检测到 sent 状态缺失审计，result: {result}"
        )

        # 5. 验证补写的审计记录
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT audit_id, action, reason, evidence_refs_json
                FROM {governance_schema}.write_audit
                WHERE (evidence_refs_json->>'outbox_id')::int = %s
                """,
                (outbox_id,),
            )
            row = cur.fetchone()

        assert row is not None, f"reconcile 后应存在 outbox_id={outbox_id} 的审计记录"

        audit_id, action, reason, evidence = row
        assert evidence is not None, "evidence_refs_json 不应为空"
        assert evidence.get("outbox_id") == outbox_id, (
            f"evidence_refs_json.outbox_id 应为 {outbox_id}，实际: {evidence.get('outbox_id')}"
        )
        assert evidence.get("source") == "reconcile_outbox", (
            f"evidence_refs_json.source 应为 'reconcile_outbox'，实际: {evidence.get('source')}"
        )

    @pytest.mark.asyncio
    async def test_smoke_full_redirect_outbox_reconcile_loop(
        self, two_phase_env, db_conn_prefixed_committed
    ):
        """
        冒烟测试 4: 完整闭环 redirect → outbox → reconcile

        验证完整的数据流：
        1. memory_store 失败 → redirect 决策 → outbox 入队 → redirected 审计
        2. 模拟 worker 处理：status 改为 sent
        3. reconcile 检测 sent 无 flush 审计 → 补写 outbox_flush_success

        此测试验证两阶段审计与 reconcile 的完整闭环一致性。

        验收矩阵引用：
        - memory_store 失败入队 → 审计 pending→redirected，outbox 入队
        - Outbox flush 成功 → outbox pending→sent，审计 outbox_flush_success
        """
        import uuid

        from tests.gateway.gate_helpers import ProfileType, require_profile

        require_profile(ProfileType.FULL, step="db_invariants")

        from engram.gateway.di import GatewayDeps
        from engram.gateway.handlers.memory_store import memory_store_impl
        from engram.gateway.logbook_adapter import LogbookAdapter
        from engram.gateway.reconcile_outbox import ReconcileConfig, run_reconcile
        from tests.gateway.fakes import FakeGatewayConfig, FakeOpenMemoryClient

        conn = db_conn_prefixed_committed
        dsn = two_phase_env["dsn"]
        schemas = two_phase_env["schemas"]
        governance_schema = schemas["governance"]
        logbook_schema = schemas["logbook"]

        # 使用唯一的 correlation_id
        corr_id = f"corr-{uuid.uuid4().hex[:16]}"

        # ========== Phase 1: memory_store 失败 → redirect ==========
        real_adapter = LogbookAdapter(dsn=dsn)
        fake_client = FakeOpenMemoryClient()
        fake_client.configure_store_connection_error("闭环测试 - OpenMemory 不可用")

        fake_config = FakeGatewayConfig()
        fake_config.postgres_dsn = dsn

        deps = GatewayDeps.for_testing(
            config=fake_config,
            logbook_adapter=real_adapter,
            openmemory_client=fake_client,
        )

        result = await memory_store_impl(
            payload_md="闭环测试内容",
            target_space="private:user_smoke_loop",
            correlation_id=corr_id,
            deps=deps,
        )

        assert result.action == "deferred", f"Phase 1: action 应为 deferred，实际: {result.action}"
        outbox_id = result.outbox_id
        assert outbox_id is not None, "Phase 1: outbox_id 不应为空"

        # 验证 redirected 审计
        audit = assert_two_phase_finalized(conn, corr_id, "redirected", governance_schema)
        assert f":outbox:{outbox_id}" in audit["reason"], "Phase 1: reason 应包含 outbox_id"

        # ========== Phase 2: 模拟 worker 处理完成 ==========
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {logbook_schema}.outbox_memory
                SET status = 'sent'
                WHERE outbox_id = %s
                """,
                (outbox_id,),
            )
        conn.commit()

        # 验证 outbox 状态已更新
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT status FROM {logbook_schema}.outbox_memory
                WHERE outbox_id = %s
                """,
                (outbox_id,),
            )
            status = cur.fetchone()[0]
            assert status == "sent", f"Phase 2: outbox status 应为 sent，实际: {status}"

        # ========== Phase 3: reconcile 补写 flush 审计 ==========
        config = ReconcileConfig(dsn=dsn, auto_fix=True)
        reconcile_result = run_reconcile(config)

        assert reconcile_result.sent_missing_audit_fixed > 0, (
            f"Phase 3: reconcile 应修复 sent 缺失审计，result: {reconcile_result}"
        )

        # 验证 flush 审计存在且可查询
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT evidence_refs_json
                FROM {governance_schema}.write_audit
                WHERE (evidence_refs_json->>'outbox_id')::int = %s
                  AND evidence_refs_json->>'source' = 'reconcile_outbox'
                """,
                (outbox_id,),
            )
            row = cur.fetchone()

        assert row is not None, f"Phase 3: 应存在 reconcile 补写的审计记录，outbox_id={outbox_id}"
        evidence = row[0]
        assert evidence.get("outbox_id") == outbox_id, "Phase 3: evidence.outbox_id 应匹配"
        assert evidence.get("source") == "reconcile_outbox", (
            "Phase 3: evidence.source 应为 reconcile_outbox"
        )
