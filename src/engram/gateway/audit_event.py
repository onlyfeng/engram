"""
audit_event - 统一审计事件构建模块

提供 build_audit_event() 函数，构建包含完整元数据的审计事件 dict。
用于替换各模块中手写的 evidence_refs_json dict，确保审计记录的一致性。

审计事件结构：
- schema_version: 审计事件版本号（便于后续格式演进）
- source: 事件来源（gateway/outbox_worker/reconcile_outbox）
- operation: 操作类型（memory_store/governance_update/outbox_flush 等）
- correlation_id: 关联追踪 ID
- actor_user_id: 执行操作的用户标识
- requested_space: 原始请求的目标空间
- final_space: 最终写入的空间（可能经过策略重定向）
- decision: 决策信息 {action, reason}
- payload_sha: 内容哈希
- payload_len: 内容长度
- evidence_summary: 证据摘要 {count, has_strong, uris}
- trim: 裁剪信息 {was_trimmed, why, original_len}
- 旧字段兼容: outbox_id, refs, memory_id 等

decision.reason 分层说明（见 docs/04_governance_switch.md）：

    reason 采用分层设计，区分业务层与协议/依赖层：
    
    业务层 reason（小写 + 下划线）:
        - policy_passed: 策略通过
        - team_write_disabled: 团队写入关闭
        - user_not_in_allowlist: 用户不在白名单
        - missing_evidence: 缺少证据链
        - strict:*: strict 模式特定错误
    
    校验层 reason（大写 + 下划线）:
        - EVIDENCE_*: 证据格式校验失败
        - PAYLOAD_*: 内容校验失败
    
    依赖层 reason（大写 + 下划线）:
        - OPENMEMORY_*: OpenMemory 服务错误
        - LOGBOOK_*: Logbook 数据库错误
    
    单一事实来源:
        - 业务层: policy.py 模块注释
        - 协议/依赖层: mcp_rpc.py:ErrorReason

Evidence v2 → Logbook evidence_refs_json 映射（最小字段集合）：

    Gateway 审计事件的 evidence_summary.uris 记录指针，写入 Logbook 时映射为：
    
    URI Canonical 格式（Logbook 内部资源必须包含 <sha256> 后缀）：
        - patch_blobs: memory://patch_blobs/<source_type>/<source_id>/<sha256>
        - attachments: memory://attachments/<namespace>/<id>/<sha256>
    
    patches[] 最小字段（用于 memory://patch_blobs/.../<sha256> URI）:
        - artifact_uri: str  # 必填，canonical 格式
        - sha256: str        # 必填，64 字符十六进制（从 URI 尾部提取）
        - source_type: str   # 必填，"svn" | "git" | "mr"
        - source_id: str     # 必填，"<repo_id>/<rev/sha>"
        - kind: str          # 可选，默认 "patch"
    
    attachments[] 最小字段（用于 memory://attachments/.../<sha256> URI）:
        - artifact_uri: str  # 必填，canonical 格式
        - sha256: str        # 必填，64 字符十六进制（从 URI 尾部提取）
        - source_id: str     # 可选
        - source_type: str   # 可选
        - kind: str          # 可选，默认 "attachment"
    
    external[] 最小字段（用于 git://, svn://, https:// URI）:
        - uri: str           # 必填，外部资源 URI
        - sha256: str        # 可选，外部资源可能无法获取 hash
    
    Strict / Compat 模式约束：
        - strict: 结构化 evidence(v2) + sha256 必填，启用 validate_refs，完整可回跳
        - compat: 允许 legacy evidence_refs（字符串列表），不保证可回跳

schema_version 版本演进策略：

    当前版本: "1.1"
    
    版本约束规则：
    - 主版本号变更（如 1.x → 2.x）：不兼容变更，需要迁移脚本
    - 次版本号变更（如 1.0 → 1.1）：向后兼容，仅新增可选字段
    
    演进原则：
    - 新增字段必须有默认值或标记为可选
    - 禁止删除已有字段，仅可标记为 deprecated
    - 读取时按 schema_version 做兼容处理
    - 写入时始终使用 AUDIT_EVENT_SCHEMA_VERSION 常量
    
    版本历史：
    - 1.0: 初始版本，包含 source/operation/correlation_id/decision/evidence_summary 等核心字段
    - 1.1: 新增 gateway_event.policy 和 gateway_event.validation 稳定子结构
           policy: {mode, mode_reason, policy_version, is_pointerized, policy_source}
           validation: {validate_refs_effective, validate_refs_reason, evidence_validation}

gateway_event 稳定子结构定义（v1.1+）：

    gateway_event.policy 子结构（策略决策上下文）：
        - mode: str - 策略模式 "strict" | "compat"
        - mode_reason: str - 模式判定说明
        - policy_version: str - 策略版本 "v1" | "v2"
        - is_pointerized: bool - 是否 pointerized（v2 特性）
        - policy_source: str - 策略来源 "settings" | "default" | "override"
    
    gateway_event.validation 子结构（校验状态上下文）：
        - validate_refs_effective: bool - 实际生效的 validate_refs 值
        - validate_refs_reason: str - validate_refs 决策原因
        - evidence_validation: dict | None - evidence 校验详情（strict 模式）

    gateway_event.pointer 子结构（v1.3+ 新增，redirect 且 pointerized 时）：
        - from_space: str - 原始请求空间
        - to_space: str - 最终写入空间
        - reason: str - redirect 原因
        - preserved: bool - 原始引用是否保留
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
# ===================== 审计事件结构（兼容导出） =====================


@dataclass
class AuditEvent:
    """
    兼容导出的审计事件结构。
    """

    source: str
    operation: str
    correlation_id: str
    schema_version: str = "1.1"
    actor_user_id: Optional[str] = None
    requested_space: Optional[str] = None
    final_space: Optional[str] = None
    decision: Optional[Dict[str, Any]] = None
    payload_sha: Optional[str] = None
    payload_len: Optional[int] = None
    evidence_summary: Optional[Dict[str, Any]] = None
    trim: Optional[Dict[str, Any]] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "schema_version": self.schema_version,
            "source": self.source,
            "operation": self.operation,
            "correlation_id": self.correlation_id,
            "actor_user_id": self.actor_user_id,
            "requested_space": self.requested_space,
            "final_space": self.final_space,
            "decision": self.decision,
            "payload_sha": self.payload_sha,
            "payload_len": self.payload_len,
            "evidence_summary": self.evidence_summary,
            "trim": self.trim,
        }
        if self.extra:
            data.update(self.extra)
        return {k: v for k, v in data.items() if v is not None}


# ===================== 审计写入错误 =====================


class AuditWriteError(Exception):
    """
    审计写入失败异常
    
    当 audit 写入失败时抛出此异常，用于阻断主操作继续执行。
    根据 ADR "审计不可丢" 语义：
    - Audit 写入失败：Gateway 应阻止主操作继续，避免不可审计的写入
    
    Attributes:
        message: 错误描述
        original_error: 原始异常（可选）
        audit_data: 尝试写入的审计数据（用于诊断）
    """
    
    def __init__(
        self,
        message: str,
        original_error: Optional[Exception] = None,
        audit_data: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.message = message
        self.original_error = original_error
        self.audit_data = audit_data
    
    def __str__(self) -> str:
        if self.original_error:
            return f"{self.message}: {self.original_error}"
        return self.message


# 审计事件 schema 版本号
# v1.1: 新增 gateway_event.policy 和 gateway_event.validation 稳定子结构
AUDIT_EVENT_SCHEMA_VERSION = "1.1"

# SHA256 合法性校验正则表达式（64 位十六进制）
SHA256_PATTERN = re.compile(r"^[a-fA-F0-9]{64}$")

# URI 分类正则表达式
# Canonical 格式要求末段为 sha256（与 policy.py 及 docs/03_memory_contract.md 保持一致）:
#   - patch_blobs: memory://patch_blobs/<source_type>/<source_id>/<sha256>
#     source_id 格式: <repo_id>:<revision/sha>（如 1:abc123）
#   - attachments: memory://attachments/<attachment_id>/<sha256>
#     attachment_id 必须为整数（与 Logbook parse_attachment_evidence_uri() 对齐）
# 注意: source_id 可能包含冒号（:），正则需要支持
PATCH_BLOB_URI_PATTERN = re.compile(r"^memory://patch_blobs/[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_/:.-]+)?/([a-fA-F0-9]{64})$")
# 旧版宽松的 attachment 正则（仅用于回退场景，不推荐）
ATTACHMENT_URI_PATTERN_LOOSE = re.compile(r"^memory://attachments/[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_/:.-]+)?/([a-fA-F0-9]{64})$")
# Logbook 对齐的严格 attachment 正则: memory://attachments/<int>/<64hex>
# 与 Logbook parse_attachment_evidence_uri() 保持一致: 第二段必须为 int，第三段必须为 64hex sha256
ATTACHMENT_URI_PATTERN_STRICT = re.compile(r"^memory://attachments/(\d+)/([a-fA-F0-9]{64})$")


def generate_correlation_id() -> str:
    """生成关联追踪 ID"""
    return f"corr-{uuid.uuid4().hex[:16]}"


def compute_evidence_summary(evidence: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """
    计算证据摘要信息
    
    Args:
        evidence: 规范化后的证据列表
        
    Returns:
        证据摘要 dict:
        - count: 证据数量
        - has_strong: 是否包含强证据（有 sha256）
        - uris: URI 列表（最多取前 5 个）
    """
    if not evidence:
        return {"count": 0, "has_strong": False, "uris": []}
    
    uris = []
    has_strong = False
    
    for ev in evidence:
        uri = ev.get("uri", "")
        if uri:
            uris.append(uri)
        # 强证据判断：sha256 非空
        if ev.get("sha256"):
            has_strong = True
    
    return {
        "count": len(evidence),
        "has_strong": has_strong,
        "uris": uris[:5],  # 最多取前 5 个，避免审计记录过大
    }


def build_audit_event(
    source: str,
    operation: str,
    correlation_id: Optional[str] = None,
    actor_user_id: Optional[str] = None,
    requested_space: Optional[str] = None,
    final_space: Optional[str] = None,
    action: Optional[str] = None,
    reason: Optional[str] = None,
    payload_sha: Optional[str] = None,
    payload_len: Optional[int] = None,
    evidence: Optional[List[Dict[str, Any]]] = None,
    evidence_refs: Optional[List[str]] = None,
    trim_was_trimmed: bool = False,
    trim_why: Optional[str] = None,
    trim_original_len: Optional[int] = None,
    # 兼容旧字段
    outbox_id: Optional[int] = None,
    memory_id: Optional[str] = None,
    retry_count: Optional[int] = None,
    next_attempt_at: Optional[str] = None,
    # 额外字段
    extra: Optional[Dict[str, Any]] = None,
    # policy 模式与校验结果（v1.1 新增，保留向后兼容）
    policy_mode: Optional[str] = None,
    evidence_validation: Optional[Dict[str, Any]] = None,
    # v1.1 新增: policy 子结构（稳定字段集）
    policy_mode_reason: Optional[str] = None,
    policy_version: Optional[str] = None,
    policy_is_pointerized: bool = False,
    policy_source: Optional[str] = None,
    # v1.1 新增: validation 子结构（稳定字段集）
    validate_refs_effective: Optional[bool] = None,
    validate_refs_reason: Optional[str] = None,
    # v1.3 新增: pointer 子结构（redirect 且 pointerized 时）
    pointer_from_space: Optional[str] = None,
    pointer_to_space: Optional[str] = None,
    pointer_reason: Optional[str] = None,
    pointer_preserved: bool = True,
) -> Dict[str, Any]:
    """
    构建统一的审计事件 dict
    
    此函数生成的 dict 用于传递给 insert_audit(evidence_refs_json=...) 参数。
    确保所有审计记录包含必要的追踪和元数据字段。
    
    Args:
        source: 事件来源（gateway/outbox_worker/reconcile_outbox）
        operation: 操作类型（memory_store/governance_update/outbox_flush 等）
        correlation_id: 关联追踪 ID（为空时自动生成）
        actor_user_id: 执行操作的用户标识
        requested_space: 原始请求的目标空间
        final_space: 最终写入的空间
        action: 决策动作（allow/redirect/reject）
        reason: 决策原因
        payload_sha: 内容 SHA256 哈希
        payload_len: 内容长度（字符数）
        evidence: 规范化后的证据列表
        evidence_refs: 旧版证据引用列表（兼容）
        trim_was_trimmed: 是否进行了裁剪
        trim_why: 裁剪原因
        trim_original_len: 裁剪前的原始长度
        outbox_id: Outbox 记录 ID（兼容旧字段）
        memory_id: OpenMemory 返回的 memory_id（兼容旧字段）
        retry_count: 重试次数（兼容旧字段）
        next_attempt_at: 下次尝试时间（兼容旧字段）
        extra: 额外的自定义字段
        policy_mode: 策略模式（strict/compat）
        evidence_validation: evidence 校验结果 dict，包含:
            - is_valid: bool
            - error_codes: List[str] - 细化错误码
            - compat_warnings: List[str] - compat 模式警告（用于可观测）
        policy_mode_reason: 模式判定说明（v1.1 新增）
        policy_version: 策略版本 v1/v2（v1.1 新增）
        policy_is_pointerized: 是否 pointerized（v1.1 新增）
        policy_source: 策略来源 settings/default/override（v1.1 新增）
        validate_refs_effective: 实际生效的 validate_refs 值（v1.1 新增）
        validate_refs_reason: validate_refs 决策原因（v1.1 新增）
        pointer_from_space: redirect 原始空间（v1.3 新增）
        pointer_to_space: redirect 目标空间（v1.3 新增）
        pointer_reason: redirect 原因（v1.3 新增）
        pointer_preserved: 原始引用是否保留（v1.3 新增）
        
    Returns:
        审计事件 dict，可直接传递给 evidence_refs_json 参数
    """
    # 确保有 correlation_id
    if correlation_id is None:
        correlation_id = generate_correlation_id()
    
    # 计算证据摘要
    evidence_summary = compute_evidence_summary(evidence)
    
    # 兼容旧版 refs 字段
    refs = evidence_refs or []
    if not refs and evidence:
        refs = [ev.get("uri", "") for ev in evidence if ev.get("uri")]
    
    # 构建审计事件
    event: Dict[str, Any] = {
        # 核心元数据（必须字段）
        "schema_version": AUDIT_EVENT_SCHEMA_VERSION,
        "source": source,
        "operation": operation,
        "correlation_id": correlation_id,
        
        # 参与者信息
        "actor_user_id": actor_user_id,
        
        # 空间信息
        "requested_space": requested_space,
        "final_space": final_space,
        
        # 决策信息
        "decision": {
            "action": action,
            "reason": reason,
        },
        
        # Payload 信息
        "payload_sha": payload_sha,
        "payload_len": payload_len,
        
        # 证据摘要
        "evidence_summary": evidence_summary,
        
        # 裁剪信息
        "trim": {
            "was_trimmed": trim_was_trimmed,
            "why": trim_why,
            "original_len": trim_original_len,
        },
        
        # 兼容旧字段（保留以避免下游查询断裂）
        "refs": refs,
        
        # 时间戳
        "event_ts": datetime.now(timezone.utc).isoformat(),
    }
    
    # 添加可选的旧兼容字段
    if outbox_id is not None:
        event["outbox_id"] = outbox_id
    if memory_id is not None:
        event["memory_id"] = memory_id
    if retry_count is not None:
        event["retry_count"] = retry_count
    if next_attempt_at is not None:
        event["next_attempt_at"] = next_attempt_at
    
    # v1.1 新增: policy 模式与校验结果（保留顶层字段以向后兼容）
    if policy_mode is not None:
        event["policy_mode"] = policy_mode
    if evidence_validation is not None:
        event["evidence_validation"] = evidence_validation
    
    # v1.1 新增: policy 子结构（稳定字段集）
    # 只有当至少有一个 policy 字段被设置时才创建子结构
    if any([policy_mode, policy_mode_reason, policy_version, policy_is_pointerized, policy_source]):
        event["policy"] = {
            "mode": policy_mode,
            "mode_reason": policy_mode_reason,
            "policy_version": policy_version,
            "is_pointerized": policy_is_pointerized,
            "policy_source": policy_source,
        }
    
    # v1.1 新增: validation 子结构（稳定字段集）
    # 只有当至少有一个 validation 字段被设置时才创建子结构
    if any([validate_refs_effective is not None, validate_refs_reason, evidence_validation]):
        event["validation"] = {
            "validate_refs_effective": validate_refs_effective,
            "validate_refs_reason": validate_refs_reason,
            "evidence_validation": evidence_validation,
        }
    
    # v1.3 新增: pointer 子结构（redirect 且 pointerized 时）
    # 只有当 is_pointerized=True 且提供了 pointer 信息时才创建子结构
    if policy_is_pointerized and pointer_from_space and pointer_to_space:
        event["pointer"] = {
            "from_space": pointer_from_space,
            "to_space": pointer_to_space,
            "reason": pointer_reason,
            "preserved": pointer_preserved,
        }
    
    # 合并额外字段
    if extra:
        # 放入 extra 子对象，避免与顶层字段冲突
        event["extra"] = extra
    
    return event


def build_gateway_audit_event(
    operation: str,
    correlation_id: Optional[str] = None,
    actor_user_id: Optional[str] = None,
    requested_space: Optional[str] = None,
    final_space: Optional[str] = None,
    action: Optional[str] = None,
    reason: Optional[str] = None,
    payload_sha: Optional[str] = None,
    payload_len: Optional[int] = None,
    evidence: Optional[List[Dict[str, Any]]] = None,
    evidence_refs: Optional[List[str]] = None,
    trim_was_trimmed: bool = False,
    trim_why: Optional[str] = None,
    trim_original_len: Optional[int] = None,
    outbox_id: Optional[int] = None,
    memory_id: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
    policy_mode: Optional[str] = None,
    evidence_validation: Optional[Dict[str, Any]] = None,
    # v1.1 新增: policy 子结构参数
    policy_mode_reason: Optional[str] = None,
    policy_version: Optional[str] = None,
    policy_is_pointerized: bool = False,
    policy_source: Optional[str] = None,
    # v1.1 新增: validation 子结构参数
    validate_refs_effective: Optional[bool] = None,
    validate_refs_reason: Optional[str] = None,
    # v1.3 新增: pointer 子结构参数
    pointer_from_space: Optional[str] = None,
    pointer_to_space: Optional[str] = None,
    pointer_reason: Optional[str] = None,
    pointer_preserved: bool = True,
) -> Dict[str, Any]:
    """
    构建 Gateway 来源的审计事件（简化版）
    
    自动设置 source="gateway"
    
    v1.1 新增稳定子结构:
    - policy: {mode, mode_reason, policy_version, is_pointerized, policy_source}
    - validation: {validate_refs_effective, validate_refs_reason, evidence_validation}
    
    v1.3 新增稳定子结构:
    - pointer: {from_space, to_space, reason, preserved}（redirect 且 pointerized 时）
    """
    return build_audit_event(
        source="gateway",
        operation=operation,
        correlation_id=correlation_id,
        actor_user_id=actor_user_id,
        requested_space=requested_space,
        final_space=final_space,
        action=action,
        reason=reason,
        payload_sha=payload_sha,
        payload_len=payload_len,
        evidence=evidence,
        evidence_refs=evidence_refs,
        trim_was_trimmed=trim_was_trimmed,
        trim_why=trim_why,
        trim_original_len=trim_original_len,
        outbox_id=outbox_id,
        memory_id=memory_id,
        extra=extra,
        policy_mode=policy_mode,
        evidence_validation=evidence_validation,
        policy_mode_reason=policy_mode_reason,
        policy_version=policy_version,
        policy_is_pointerized=policy_is_pointerized,
        policy_source=policy_source,
        validate_refs_effective=validate_refs_effective,
        validate_refs_reason=validate_refs_reason,
        pointer_from_space=pointer_from_space,
        pointer_to_space=pointer_to_space,
        pointer_reason=pointer_reason,
        pointer_preserved=pointer_preserved,
    )


def build_outbox_worker_audit_event(
    operation: str,
    correlation_id: Optional[str] = None,
    actor_user_id: Optional[str] = None,
    target_space: Optional[str] = None,
    action: Optional[str] = None,
    reason: Optional[str] = None,
    payload_sha: Optional[str] = None,
    outbox_id: Optional[int] = None,
    memory_id: Optional[str] = None,
    retry_count: Optional[int] = None,
    next_attempt_at: Optional[str] = None,
    worker_id: Optional[str] = None,
    attempt_id: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    构建 Outbox Worker 来源的审计事件（简化版）
    
    自动设置 source="outbox_worker"
    将 worker_id 和 attempt_id 放入 extra
    """
    worker_extra = extra.copy() if extra else {}
    if worker_id:
        worker_extra["worker_id"] = worker_id
    if attempt_id:
        worker_extra["attempt_id"] = attempt_id
    
    return build_audit_event(
        source="outbox_worker",
        operation=operation,
        correlation_id=correlation_id,
        actor_user_id=actor_user_id,
        requested_space=target_space,
        final_space=target_space,
        action=action,
        reason=reason,
        payload_sha=payload_sha,
        outbox_id=outbox_id,
        memory_id=memory_id,
        retry_count=retry_count,
        next_attempt_at=next_attempt_at,
        extra=worker_extra if worker_extra else None,
    )


def build_reconcile_audit_event(
    operation: str,
    correlation_id: Optional[str] = None,
    actor_user_id: Optional[str] = None,
    target_space: Optional[str] = None,
    action: Optional[str] = None,
    reason: Optional[str] = None,
    payload_sha: Optional[str] = None,
    outbox_id: Optional[int] = None,
    memory_id: Optional[str] = None,
    retry_count: Optional[int] = None,
    original_locked_by: Optional[str] = None,
    original_locked_at: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    构建 Reconcile Outbox 来源的审计事件（简化版）
    
    自动设置 source="reconcile_outbox"
    将原始锁定信息放入 extra
    """
    reconcile_extra = extra.copy() if extra else {}
    reconcile_extra["reconciled"] = True
    if original_locked_by:
        reconcile_extra["original_locked_by"] = original_locked_by
    if original_locked_at:
        reconcile_extra["original_locked_at"] = original_locked_at
    
    return build_audit_event(
        source="reconcile_outbox",
        operation=operation,
        correlation_id=correlation_id,
        actor_user_id=actor_user_id,
        requested_space=target_space,
        final_space=target_space,
        action=action,
        reason=reason,
        payload_sha=payload_sha,
        outbox_id=outbox_id,
        memory_id=memory_id,
        retry_count=retry_count,
        extra=reconcile_extra,
    )


def is_valid_sha256(value: Optional[str]) -> bool:
    """
    校验 SHA256 值是否合法（64 位十六进制字符串）
    
    Args:
        value: 待校验的值
        
    Returns:
        True 如果是合法的 SHA256
    """
    if not value:
        return False
    return bool(SHA256_PATTERN.match(value))


def parse_attachment_evidence_uri(uri: str) -> Optional[dict]:
    """
    解析 attachment evidence URI，提取其中的 attachment_id、sha256
    
    此函数复刻 Logbook engram_logbook.uri.parse_attachment_evidence_uri() 的规则：
    - 第二段必须为 int attachment_id
    - 第三段必须为 64hex sha256
    
    Args:
        uri: attachment evidence URI 字符串
    
    Returns:
        解析结果字典，包含 attachment_id、sha256；
        如果不是有效的 attachment evidence URI，返回 None
    
    示例:
        parse_attachment_evidence_uri("memory://attachments/12345/sha256hash64hex...")
        # => {"attachment_id": 12345, "sha256": "sha256hash64hex..."}
        
        parse_attachment_evidence_uri("memory://attachments/not_int/sha256")
        # => None（attachment_id 非整数）
        
        parse_attachment_evidence_uri("memory://attachments/123/short")
        # => None（sha256 非 64hex）
    """
    match = ATTACHMENT_URI_PATTERN_STRICT.match(uri)
    if not match:
        return None
    
    try:
        attachment_id = int(match.group(1))
    except ValueError:
        return None
    
    sha256 = match.group(2)
    # 严格校验 sha256 格式（64 位十六进制）
    if not is_valid_sha256(sha256):
        return None
    
    return {
        "attachment_id": attachment_id,
        "sha256": sha256,
    }


def classify_evidence_uri(uri: str, sha256: Optional[str] = None) -> tuple:
    """
    根据 URI 类型对证据进行分类
    
    分类规则（与 Logbook 对齐）：
    - patch_blobs: memory://patch_blobs/<source_type>/<source_id>/<sha256>
    - attachments: memory://attachments/<int attachment_id>/<64hex sha256>
      （优先使用 parse_attachment_evidence_uri() 严格规则）
    - external: 其他 URI（包括解析失败的 attachment URI）
    
    Args:
        uri: 证据 URI
        sha256: 可选的 sha256 值（用于校验）
        
    Returns:
        tuple: (category, extracted_sha256)
        - category: "patches" / "attachments" / "external"
        - extracted_sha256: 从 URI 中提取的 sha256（如有）
    """
    if not uri:
        return "external", None
    
    # 尝试匹配 patch_blobs URI
    match = PATCH_BLOB_URI_PATTERN.match(uri)
    if match:
        extracted_sha = match.group(1)
        # 校验 sha256 是否合法
        if is_valid_sha256(extracted_sha):
            return "patches", extracted_sha
    
    # 尝试使用严格规则解析 attachments URI
    # 与 Logbook parse_attachment_evidence_uri() 对齐：
    # - 第二段必须为 int attachment_id
    # - 第三段必须为 64hex sha256
    parsed = parse_attachment_evidence_uri(uri)
    if parsed:
        return "attachments", parsed["sha256"]
    
    # 解析失败的 attachment URI（包括非数字 attachment_id、非 64hex sha256、多段路径等）
    # 降级分类为 external
    # 其他 URI（git://, https://, svn://, memory://refs/ 等）同样归类为 external
    return "external", None


def build_evidence_refs_json(
    evidence: Optional[List[Dict[str, Any]]],
    gateway_event: Dict[str, Any],
) -> Dict[str, Any]:
    """
    构建 Logbook 兼容的 evidence_refs_json 结构
    
    将 normalized evidence (v2 列表) 与 gateway_event 元数据合并，
    输出 Logbook 兼容的结构：
    - patches: artifact_uri/sha256/source_id/source_type/kind
    - attachments: artifact_uri/sha256/source_id/source_type/kind
    - external: 其他 URI
    - gateway_event: 原有审计事件元数据
    
    分类规则：
    - memory://patch_blobs/.../<sha256>（且 sha256 合法）→ patches
    - memory://attachments/.../<sha256>（且 sha256 合法）→ attachments
    - 其他 URI（git://, https://, svn://, memory://refs/ 等）→ external
    
    Args:
        evidence: 规范化后的证据列表（v2 格式），每项包含:
            - uri: 证据 URI（必填）
            - sha256: 内容哈希（可选）
            - event_id, svn_rev, git_commit, mr: 来源信息（可选）
        gateway_event: 由 build_*_audit_event 构建的审计事件 dict
        
    Returns:
        Logbook 兼容的 evidence_refs_json 结构:
        {
            "patches": [{artifact_uri, sha256, source_id, source_type, kind}, ...],
            "attachments": [{artifact_uri, sha256, source_id, source_type, kind}, ...],
            "external": [{uri, ...}, ...],
            "gateway_event": {...}
        }
    """
    patches: List[Dict[str, Any]] = []
    attachments: List[Dict[str, Any]] = []
    external: List[Dict[str, Any]] = []
    
    if evidence:
        for ev in evidence:
            uri = ev.get("uri", "")
            sha256 = ev.get("sha256", "")
            
            # 根据 URI 分类
            category, extracted_sha = classify_evidence_uri(uri, sha256)
            
            if category == "patches":
                # patches 字段对齐 Logbook schema
                patch_item: Dict[str, Any] = {
                    "artifact_uri": uri,
                    "sha256": extracted_sha or sha256,
                }
                # 添加可选的来源信息
                if ev.get("source_id"):
                    patch_item["source_id"] = ev["source_id"]
                elif ev.get("event_id"):
                    patch_item["source_id"] = str(ev["event_id"])
                
                if ev.get("source_type"):
                    patch_item["source_type"] = ev["source_type"]
                elif ev.get("svn_rev"):
                    patch_item["source_type"] = "svn"
                    if not patch_item.get("source_id"):
                        patch_item["source_id"] = str(ev["svn_rev"])
                elif ev.get("git_commit"):
                    patch_item["source_type"] = "git"
                    if not patch_item.get("source_id"):
                        patch_item["source_id"] = ev["git_commit"]
                elif ev.get("mr"):
                    patch_item["source_type"] = "mr"
                    if not patch_item.get("source_id"):
                        patch_item["source_id"] = str(ev["mr"])
                
                if ev.get("kind"):
                    patch_item["kind"] = ev["kind"]
                
                patches.append(patch_item)
            
            elif category == "attachments":
                # attachments 字段对齐 Logbook schema
                attachment_item: Dict[str, Any] = {
                    "artifact_uri": uri,
                    "sha256": extracted_sha or sha256,
                }
                # 添加可选的来源信息
                if ev.get("source_id"):
                    attachment_item["source_id"] = ev["source_id"]
                if ev.get("source_type"):
                    attachment_item["source_type"] = ev["source_type"]
                if ev.get("kind"):
                    attachment_item["kind"] = ev["kind"]
                
                attachments.append(attachment_item)
            
            else:
                # external: 保留原始证据结构
                external_item: Dict[str, Any] = {"uri": uri}
                # 保留其他有效字段
                if sha256:
                    external_item["sha256"] = sha256
                if ev.get("event_id"):
                    external_item["event_id"] = ev["event_id"]
                if ev.get("svn_rev"):
                    external_item["svn_rev"] = ev["svn_rev"]
                if ev.get("git_commit"):
                    external_item["git_commit"] = ev["git_commit"]
                if ev.get("mr"):
                    external_item["mr"] = ev["mr"]
                
                external.append(external_item)
    
    # 构建最终结构
    result: Dict[str, Any] = {
        "gateway_event": gateway_event,
    }
    
    # 仅包含非空列表
    if patches:
        result["patches"] = patches
    if attachments:
        result["attachments"] = attachments
    if external:
        result["external"] = external
    
    # 顶层添加 evidence_summary（从 gateway_event 复制，保持兼容）
    # 这确保 dedup_hit / reject / success / outbox 等路径审计字段一致
    if gateway_event.get("evidence_summary"):
        result["evidence_summary"] = gateway_event["evidence_summary"]
    
    # 顶层添加 refs（从 gateway_event 复制，保持向后兼容）
    # refs 用于旧版证据引用格式
    if gateway_event.get("refs"):
        result["refs"] = gateway_event["refs"]
    
    # ========================================================================
    # 顶层兼容字段提升（Logbook 查询契约）
    # ========================================================================
    # reconcile_outbox.py 使用 evidence_refs_json->>'outbox_id' 查询
    # 为保持与 SQL 查询的契约一致，将关键字段提升到顶层
    # 这些字段同时保留在 gateway_event 中以保持完整的元数据追踪
    
    # 核心追踪字段（用于 SQL 查询）
    if gateway_event.get("outbox_id") is not None:
        result["outbox_id"] = gateway_event["outbox_id"]
    if gateway_event.get("memory_id") is not None:
        result["memory_id"] = gateway_event["memory_id"]
    if gateway_event.get("source"):
        result["source"] = gateway_event["source"]
    
    # 状态跟踪字段（用于审计追踪和查询）
    if gateway_event.get("retry_count") is not None:
        result["retry_count"] = gateway_event["retry_count"]
    if gateway_event.get("next_attempt_at") is not None:
        result["next_attempt_at"] = gateway_event["next_attempt_at"]
    if gateway_event.get("payload_sha"):
        result["payload_sha"] = gateway_event["payload_sha"]
    
    # 额外追踪字段（用于可观测性）
    if gateway_event.get("extra"):
        result["extra"] = gateway_event["extra"]
    
    return result


# ===================== Evidence V2 规范化与校验 =====================


@dataclass
class EvidenceValidationResult:
    """Evidence 校验结果"""
    is_valid: bool
    error_codes: List[str] = field(default_factory=list)
    compat_warnings: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为 dict 用于审计记录"""
        return {
            "is_valid": self.is_valid,
            "error_codes": self.error_codes,
            "compat_warnings": self.compat_warnings,
        }


def map_evidence_refs_to_v2_external(
    evidence_refs: Optional[List[str]],
) -> List[Dict[str, Any]]:
    """
    将 legacy evidence_refs（字符串列表）映射为 v2 evidence external 格式
    
    映射规则：
    - 每个 ref 字符串映射为一个 external 项
    - sha256 为空（legacy refs 无法获取）
    - uri 为原始 ref 字符串
    
    Args:
        evidence_refs: legacy 证据引用列表
        
    Returns:
        v2 格式的 external 证据列表
        
    示例:
        >>> map_evidence_refs_to_v2_external(["https://example.com/doc.md", "git://repo/commit/abc"])
        [
            {"uri": "https://example.com/doc.md", "sha256": ""},
            {"uri": "git://repo/commit/abc", "sha256": ""}
        ]
    """
    if not evidence_refs:
        return []
    
    result = []
    for ref in evidence_refs:
        if ref:  # 跳过空字符串
            result.append({
                "uri": ref,
                "sha256": "",  # legacy refs 无 sha256
                "_source": "evidence_refs_legacy",  # 标记来源便于追踪
            })
    
    return result


def validate_evidence_for_strict_mode(
    evidence: Optional[List[Dict[str, Any]]],
) -> EvidenceValidationResult:
    """
    在 strict 模式下校验 evidence 结构
    
    校验规则：
    - 每项必须包含 uri 字段
    - 每项应包含 sha256 字段且为有效的 64 位十六进制（否则记录 error_code）
    - _source == "evidence_refs_legacy" 的项触发 missing_sha256 警告
    
    Args:
        evidence: v2 格式的证据列表
        
    Returns:
        EvidenceValidationResult 包含校验结果
        
    错误码说明：
    - EVIDENCE_MISSING_URI: 证据项缺少 uri 字段
    - EVIDENCE_MISSING_SHA256: 证据项缺少 sha256 字段
    - EVIDENCE_INVALID_SHA256: sha256 格式无效（非 64 位十六进制）
    - EVIDENCE_LEGACY_NO_SHA256: legacy 来源的证据无 sha256（仅警告）
    """
    result = EvidenceValidationResult(is_valid=True)
    
    if not evidence:
        return result
    
    for idx, ev in enumerate(evidence):
        prefix = f"evidence[{idx}]"
        
        # 校验 uri 字段
        uri = ev.get("uri")
        if not uri:
            result.is_valid = False
            result.error_codes.append(f"EVIDENCE_MISSING_URI:{prefix}")
            continue
        
        # 校验 sha256 字段
        sha256 = ev.get("sha256")
        source = ev.get("_source")
        
        if not sha256:
            if source == "evidence_refs_legacy":
                # legacy 来源的证据无 sha256，记录警告而非错误
                result.compat_warnings.append(f"EVIDENCE_LEGACY_NO_SHA256:{prefix}:{uri}")
            else:
                # v2 证据缺少 sha256 是错误
                result.is_valid = False
                result.error_codes.append(f"EVIDENCE_MISSING_SHA256:{prefix}:{uri}")
        elif not is_valid_sha256(sha256):
            # sha256 格式无效
            result.is_valid = False
            result.error_codes.append(f"EVIDENCE_INVALID_SHA256:{prefix}:{sha256[:16]}...")
    
    return result


def normalize_evidence(
    evidence: Optional[List[Dict[str, Any]]],
    evidence_refs: Optional[List[str]],
) -> tuple:
    """
    统一规范化 evidence 输入
    
    优先级规则：
    1. 若 evidence(v2) 非空，优先使用
    2. 若仅 evidence_refs(v1) 非空，映射为 v2 external 格式
    3. 若均为空，返回空列表
    
    Args:
        evidence: v2 格式的证据列表（优先）
        evidence_refs: v1 legacy 格式的证据引用列表
        
    Returns:
        tuple: (normalized_evidence, source)
        - normalized_evidence: 规范化后的 v2 证据列表
        - source: 来源标识 "v2" / "v1_mapped" / "none"
        
    示例:
        >>> normalize_evidence([{"uri": "...", "sha256": "..."}], None)
        ([{"uri": "...", "sha256": "..."}], "v2")
        
        >>> normalize_evidence(None, ["https://..."])
        ([{"uri": "https://...", "sha256": "", "_source": "evidence_refs_legacy"}], "v1_mapped")
        
        >>> normalize_evidence(None, None)
        ([], "none")
    """
    if evidence:
        # v2 evidence 优先
        return evidence, "v2"
    
    if evidence_refs:
        # 将 v1 evidence_refs 映射为 v2 external 格式
        mapped = map_evidence_refs_to_v2_external(evidence_refs)
        return mapped, "v1_mapped"
    
    return [], "none"
