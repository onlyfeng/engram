# -*- coding: utf-8 -*-
"""
审计断言辅助模块

提供可复用的审计契约断言函数，用于 e2e/contract/integration 测试。

主要函数：
- assert_two_phase_finalized: 验证两阶段审计已完成（pending -> success/redirected/rejected）
- assert_single_stage_reject_no_pending: 验证单阶段 reject 没有 pending 状态
- assert_evidence_refs_queryable_by_outbox_id: 验证 evidence_refs 可通过 outbox_id 查询
- assert_evidence_refs_top_level_contract: 验证 evidence_refs 顶层契约

参考文档：
- ADR: docs/architecture/adr_gateway_audit_atomicity.md
- 契约: docs/contracts/gateway_audit_evidence_correlation_contract.md
"""

from typing import Any, Dict, Optional

import psycopg


def query_audit_by_correlation_id(
    conn: psycopg.Connection,
    correlation_id: str,
    governance_schema: str = "governance",
) -> Optional[Dict[str, Any]]:
    """
    根据 correlation_id 查询审计记录

    Args:
        conn: 数据库连接（需已设置 search_path）
        correlation_id: 追踪 ID
        governance_schema: governance schema 名称（支持带前缀的 schema）

    Returns:
        审计记录字典，或 None（未找到）
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT audit_id, actor_user_id, target_space, action, reason,
                   payload_sha, evidence_refs_json, correlation_id, status,
                   created_at, updated_at
            FROM {governance_schema}.write_audit
            WHERE correlation_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (correlation_id,),
        )
        row = cur.fetchone()
        if row:
            return {
                "audit_id": row[0],
                "actor_user_id": row[1],
                "target_space": row[2],
                "action": row[3],
                "reason": row[4],
                "payload_sha": row[5],
                "evidence_refs_json": row[6],
                "correlation_id": row[7],
                "status": row[8],
                "created_at": row[9],
                "updated_at": row[10],
            }
        return None


def count_audits_by_correlation_id(
    conn: psycopg.Connection,
    correlation_id: str,
    governance_schema: str = "governance",
) -> int:
    """
    统计指定 correlation_id 的审计记录数

    Args:
        conn: 数据库连接（需已设置 search_path）
        correlation_id: 追踪 ID
        governance_schema: governance schema 名称（支持带前缀的 schema）

    Returns:
        记录数
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(*)
            FROM {governance_schema}.write_audit
            WHERE correlation_id = %s
            """,
            (correlation_id,),
        )
        return cur.fetchone()[0]


def assert_two_phase_finalized(
    conn: psycopg.Connection,
    correlation_id: str,
    expected_status: str,
    governance_schema: str = "governance",
) -> Dict[str, Any]:
    """
    断言两阶段审计已完成（pending -> 最终状态）

    验证：
    1. 审计记录存在
    2. status 为预期的最终状态（success/redirected/rejected）
    3. status 不为 pending（两阶段已完成）
    4. 只有一条审计记录（无重复写入）

    Args:
        conn: 数据库连接
        correlation_id: 追踪 ID
        expected_status: 预期的最终状态（"success", "redirected", "rejected"）
        governance_schema: governance schema 名称

    Returns:
        审计记录字典

    Raises:
        AssertionError: 断言失败
    """
    # 验证记录存在
    audit = query_audit_by_correlation_id(conn, correlation_id, governance_schema)
    assert audit is not None, f"[两阶段审计] 应存在 correlation_id={correlation_id} 的审计记录"

    # 验证状态不为 pending（两阶段已完成）
    assert audit["status"] != "pending", (
        f"[两阶段审计] status 不应为 pending（两阶段应已完成），"
        f"实际: {audit['status']}, correlation_id={correlation_id}"
    )

    # 验证状态为预期值
    assert audit["status"] == expected_status, (
        f"[两阶段审计] status 应为 {expected_status}，"
        f"实际: {audit['status']}, correlation_id={correlation_id}"
    )

    # 验证只有一条记录（无重复写入）
    count = count_audits_by_correlation_id(conn, correlation_id, governance_schema)
    assert count == 1, (
        f"[两阶段审计] 应只有 1 条审计记录，实际: {count}, correlation_id={correlation_id}"
    )

    return audit


def assert_single_stage_reject_no_pending(
    conn: psycopg.Connection,
    correlation_id: str,
    governance_schema: str = "governance",
) -> Dict[str, Any]:
    """
    断言单阶段 reject 没有 pending 状态

    适用于策略拒绝、evidence 校验失败等场景，这些情况下：
    1. 不会进入两阶段流程
    2. 直接写入最终状态的审计记录
    3. action 必须为 reject
    4. status 不能为 pending

    Args:
        conn: 数据库连接
        correlation_id: 追踪 ID
        governance_schema: governance schema 名称

    Returns:
        审计记录字典

    Raises:
        AssertionError: 断言失败
    """
    # 验证记录存在
    audit = query_audit_by_correlation_id(conn, correlation_id, governance_schema)
    assert audit is not None, f"[单阶段 reject] 应存在 correlation_id={correlation_id} 的审计记录"

    # 验证 action 为 reject
    assert audit["action"] == "reject", (
        f"[单阶段 reject] action 应为 reject，"
        f"实际: {audit['action']}, correlation_id={correlation_id}"
    )

    # 验证 status 不为 pending
    assert audit["status"] != "pending", (
        f"[单阶段 reject] status 不应为 pending（reject 不经过两阶段），"
        f"实际: {audit['status']}, correlation_id={correlation_id}"
    )

    # 验证只有一条记录
    count = count_audits_by_correlation_id(conn, correlation_id, governance_schema)
    assert count == 1, (
        f"[单阶段 reject] 应只有 1 条审计记录，实际: {count}, correlation_id={correlation_id}"
    )

    return audit


def assert_evidence_refs_queryable_by_outbox_id(
    conn: psycopg.Connection,
    outbox_id: int,
    governance_schema: str = "governance",
    expected_fields: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    断言 evidence_refs_json 可通过 outbox_id 查询

    契约：deferred/redirected 审计的 evidence_refs_json 顶层必须包含 outbox_id，
    且可通过 (evidence_refs_json->>'outbox_id')::int 查询。

    Args:
        conn: 数据库连接
        outbox_id: outbox 记录 ID
        governance_schema: governance schema 名称
        expected_fields: 可选的额外字段验证（如 {"source": "outbox_worker"}）

    Returns:
        审计记录字典（包含 audit_id, action, status, evidence_refs_json 等）

    Raises:
        AssertionError: 断言失败
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT audit_id, correlation_id, action, status, reason, evidence_refs_json
            FROM {governance_schema}.write_audit
            WHERE (evidence_refs_json->>'outbox_id')::int = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (outbox_id,),
        )
        row = cur.fetchone()

    assert row is not None, (
        f"[evidence_refs 查询] 应能通过 (evidence_refs_json->>'outbox_id')::int "
        f"查询到审计记录，outbox_id={outbox_id}"
    )

    audit = {
        "audit_id": row[0],
        "correlation_id": row[1],
        "action": row[2],
        "status": row[3],
        "reason": row[4],
        "evidence_refs_json": row[5],
    }

    evidence = audit["evidence_refs_json"]
    assert evidence is not None, (
        f"[evidence_refs 查询] evidence_refs_json 不应为空，outbox_id={outbox_id}"
    )

    # 验证 outbox_id 正确
    assert evidence.get("outbox_id") == outbox_id, (
        f"[evidence_refs 查询] evidence_refs_json.outbox_id 应为 {outbox_id}，"
        f"实际: {evidence.get('outbox_id')}"
    )

    # 验证额外字段
    if expected_fields:
        for field, expected_value in expected_fields.items():
            actual_value = evidence.get(field)
            assert actual_value == expected_value, (
                f"[evidence_refs 查询] evidence_refs_json.{field} 应为 {expected_value}，"
                f"实际: {actual_value}, outbox_id={outbox_id}"
            )

    return audit


def assert_evidence_refs_top_level_contract(
    evidence_json: Dict[str, Any],
    expected_source: str,
    require_correlation_id: bool = True,
    require_payload_sha: bool = True,
) -> None:
    """
    断言 evidence_refs_json 顶层契约

    根据 docs/contracts/gateway_audit_evidence_correlation_contract.md，
    evidence_refs_json 顶层必须包含特定字段以支持 SQL 查询。

    Args:
        evidence_json: evidence_refs_json 字典
        expected_source: 预期的 source 值（"gateway" 或 "outbox_worker"）
        require_correlation_id: 是否要求顶层包含 correlation_id
        require_payload_sha: 是否要求顶层包含 payload_sha

    Raises:
        AssertionError: 断言失败
    """
    assert evidence_json is not None, "[顶层契约] evidence_refs_json 不应为空"

    # 验证顶层 source
    assert "source" in evidence_json, (
        "[顶层契约] evidence_refs_json 顶层必须包含 source（用于 SQL 查询）"
    )
    assert evidence_json["source"] == expected_source, (
        f"[顶层契约] 顶层 source 应为 {expected_source}，实际: {evidence_json['source']}"
    )

    # 验证顶层 correlation_id（可选）
    if require_correlation_id:
        assert "correlation_id" in evidence_json, (
            "[顶层契约] evidence_refs_json 顶层必须包含 correlation_id（用于追踪）"
        )
        corr_id = evidence_json["correlation_id"]
        assert corr_id is not None, "[顶层契约] 顶层 correlation_id 不应为 None"
        # 验证格式：corr- 前缀 + 16 位十六进制
        assert corr_id.startswith("corr-"), (
            f"[顶层契约] correlation_id 应以 corr- 开头，实际: {corr_id}"
        )

    # 验证顶层 payload_sha（可选）
    if require_payload_sha:
        assert "payload_sha" in evidence_json, (
            "[顶层契约] evidence_refs_json 顶层必须包含 payload_sha（用于去重）"
        )
        payload_sha = evidence_json["payload_sha"]
        assert payload_sha is not None, "[顶层契约] 顶层 payload_sha 不应为 None"
        assert len(payload_sha) == 64, (
            f"[顶层契约] 顶层 payload_sha 应为 64 位十六进制，实际长度: {len(payload_sha)}"
        )


def assert_gateway_event_substructure(
    evidence_json: Dict[str, Any],
    expected_correlation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    断言 evidence_refs_json 包含 gateway_event 子结构

    Args:
        evidence_json: evidence_refs_json 字典
        expected_correlation_id: 可选的预期 correlation_id

    Returns:
        gateway_event 子结构字典

    Raises:
        AssertionError: 断言失败
    """
    assert evidence_json is not None, "[gateway_event] evidence_refs_json 不应为空"
    assert "gateway_event" in evidence_json, (
        "[gateway_event] evidence_refs_json 必须包含 gateway_event 子结构"
    )

    gateway_event = evidence_json["gateway_event"]
    assert gateway_event is not None, "[gateway_event] gateway_event 不应为 None"

    # 验证必需字段
    required_fields = ["source", "operation", "correlation_id", "decision"]
    for field in required_fields:
        assert field in gateway_event, f"[gateway_event] gateway_event 必须包含 {field} 字段"

    # 验证 source
    assert gateway_event["source"] == "gateway", (
        f"[gateway_event] gateway_event.source 应为 gateway，实际: {gateway_event['source']}"
    )

    # 验证 correlation_id（如果提供）
    if expected_correlation_id:
        assert gateway_event["correlation_id"] == expected_correlation_id, (
            f"[gateway_event] gateway_event.correlation_id 应为 {expected_correlation_id}，"
            f"实际: {gateway_event['correlation_id']}"
        )

    return gateway_event


def assert_outbox_worker_evidence_contract(
    evidence_json: Dict[str, Any],
    expected_outbox_id: int,
) -> None:
    """
    断言 outbox_worker 写入的 evidence_refs_json 契约

    outbox_worker 写入的审计记录 evidence_refs_json 必须包含：
    - outbox_id: 关联的 outbox 记录 ID
    - source: "outbox_worker"
    - extra.correlation_id: 追踪 ID
    - extra.attempt_id: 尝试 ID

    Args:
        evidence_json: evidence_refs_json 字典
        expected_outbox_id: 预期的 outbox_id

    Raises:
        AssertionError: 断言失败
    """
    assert evidence_json is not None, "[outbox_worker 契约] evidence_refs_json 不应为空"

    # 验证 outbox_id
    assert "outbox_id" in evidence_json, "[outbox_worker 契约] evidence 顶层应包含 outbox_id"
    assert evidence_json["outbox_id"] == expected_outbox_id, (
        f"[outbox_worker 契约] outbox_id 应为 {expected_outbox_id}，"
        f"实际: {evidence_json['outbox_id']}"
    )

    # 验证 source
    assert evidence_json.get("source") == "outbox_worker", (
        f"[outbox_worker 契约] source 应为 outbox_worker，实际: {evidence_json.get('source')}"
    )

    # 验证 extra 结构（包含 correlation_id 和 attempt_id）
    extra = evidence_json.get("extra")
    assert extra is not None, "[outbox_worker 契约] evidence 应包含 extra 字段"
    assert "correlation_id" in extra, "[outbox_worker 契约] extra 应包含 correlation_id"
    assert extra["correlation_id"].startswith("corr-"), (
        f"[outbox_worker 契约] extra.correlation_id 应以 corr- 开头，"
        f"实际: {extra['correlation_id']}"
    )
    assert "attempt_id" in extra, "[outbox_worker 契约] extra 应包含 attempt_id"
    assert extra["attempt_id"].startswith("attempt-"), (
        f"[outbox_worker 契约] extra.attempt_id 应以 attempt- 开头，实际: {extra['attempt_id']}"
    )
