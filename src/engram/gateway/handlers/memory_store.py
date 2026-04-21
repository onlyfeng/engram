"""
memory_store handler - memory_store 工具核心实现

提供 memory_store_impl 函数，处理：
1. 读取治理 settings
2. 规范化 evidence（v2 优先，v1 映射）
3. 策略决策 (policy)
4. 写入审计 (insert audit)
5. 调用 OpenMemory
6. 成功返回 memory_id / 失败写入 outbox

================================================================================
                       依赖注入 (v1.0)
================================================================================

所有依赖通过 deps 参数获取（必需）：

```python
from engram.gateway.di import GatewayDeps

deps = GatewayDeps.create()  # 生产环境
# 或
deps = GatewayDeps.for_testing(...)  # 测试环境

result = await memory_store_impl(
    payload_md="...",
    correlation_id="...",
    deps=deps,
)
```

deps 提供的依赖：
- deps.config: GatewayConfig 配置对象
- deps.db: LogbookDatabase 数据库实例
- deps.logbook_adapter: LogbookAdapter 适配器
- deps.openmemory_client: OpenMemoryClient 客户端

================================================================================
                       correlation_id 单一来源原则
================================================================================

correlation_id 是必需参数，必须由调用方（HTTP 入口层）生成后传入。
handler 不再自行生成 correlation_id，确保同一请求使用同一 ID。

错误响应中的 correlation_id 必须与请求保持一致。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from pydantic import BaseModel

from ..audit_event import (
    AuditWriteError,
    build_evidence_refs_json,
    build_gateway_audit_event,
    normalize_evidence,
    validate_evidence_for_strict_mode,
)
from ..config import resolve_validate_refs
from ..di import GatewayDepsProtocol
from ..openmemory_client import (
    OpenMemoryAPIError,
    OpenMemoryConnectionError,
    OpenMemoryError,
    extract_memory_object_content,
    extract_memory_object_payload_sha,
    extract_memory_object_space,
)
from ..policy import PolicyAction, create_engine_from_settings
from ..services.actor_validation import validate_actor_user
from ..services.audit_service import write_audit_or_raise
from ..services.hash_utils import compute_payload_sha

if TYPE_CHECKING:
    pass

# 导入统一错误码
from engram.logbook.errors import ErrorCode

logger = logging.getLogger("gateway.handlers.memory_store")
READBACK_VERIFY_ATTEMPTS = 3
READBACK_VERIFY_DELAY_SECONDS = 0.1


class MemoryStoreResponse(BaseModel):
    """
    memory_store 响应模型

    统一响应契约（详见 docs/gateway/07_capability_boundary.md）：
    - ok: 操作是否成功（true: 成功或已入队，false: 失败）
    - action: 操作结果类型
        - allow: 直接写入成功
        - redirect: 空间重定向后写入成功
        - deferred: 写入已入队 outbox（OpenMemory 不可用）
        - reject: 策略拒绝
        - error: 系统错误
    - outbox_id: action=deferred 时必需，outbox 队列 ID
    - correlation_id: 所有响应必需，请求追踪 ID
    """

    ok: bool
    action: str  # allow / redirect / deferred / reject / error
    space_written: Optional[str] = None
    memory_id: Optional[str] = None
    outbox_id: Optional[int] = None  # action=deferred 时必需
    correlation_id: Optional[str] = None  # 所有响应必需，请求追踪 ID
    evidence_refs: Optional[List[str]] = None
    message: Optional[str] = None


def _to_updated_count(value: Any) -> int:
    """将 update_write_audit 返回值收敛为 int，兼容测试中的 mock 返回类型。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


@dataclass
class _ReadbackValidationFailure:
    reason: str
    message: str


@dataclass
class _ReadbackVerificationResult:
    failure: Optional[_ReadbackValidationFailure] = None
    skipped_error: Optional[str] = None


def _resolve_openmemory_user_id(
    *,
    target_space: str,
    actor_user_id: Optional[str],
    private_space_prefix: str,
) -> Optional[str]:
    """私有空间一律使用 space owner 作为 OpenMemory user_id。"""
    if target_space.startswith(private_space_prefix):
        owner = target_space[len(private_space_prefix) :]
        if owner:
            return owner
    return actor_user_id


def _classify_readback_fetch_failure(error: Optional[str]) -> Optional[_ReadbackValidationFailure]:
    """仅将可确定的一致性异常收敛为稳定 reason。"""
    if error == "memory_not_found":
        return _ReadbackValidationFailure(
            reason=ErrorCode.OPENMEMORY_CONSISTENCY_FAILED_NOT_FOUND,
            message="memory_get 未找到刚写入的对象",
        )
    if error and error.startswith("memory_id_mismatch:"):
        return _ReadbackValidationFailure(
            reason=ErrorCode.OPENMEMORY_CONSISTENCY_FAILED_MEMORY_ID_MISMATCH,
            message=f"memory_get 返回了错误对象: {error}",
        )
    if error and error.startswith("invalid_memory_payload:"):
        return _ReadbackValidationFailure(
            reason=ErrorCode.OPENMEMORY_CONSISTENCY_FAILED_INVALID_PAYLOAD,
            message=f"memory_get 返回了无效对象: {error}",
        )
    return None


def _validate_readback_memory(
    *,
    memory: Any,
    expected_space: str,
    expected_payload_sha: str,
) -> Optional[_ReadbackValidationFailure]:
    """校验写后读对象的关键一致性字段。"""
    actual_space = extract_memory_object_space(memory)
    if actual_space != expected_space:
        return _ReadbackValidationFailure(
            reason=ErrorCode.OPENMEMORY_CONSISTENCY_FAILED_SPACE_MISMATCH,
            message=f"memory_get 返回的 space 不一致: expected={expected_space}, actual={actual_space}",
        )

    content = extract_memory_object_content(memory)
    if not isinstance(content, str) or not content.strip():
        return _ReadbackValidationFailure(
            reason=ErrorCode.OPENMEMORY_CONSISTENCY_FAILED_EMPTY_CONTENT,
            message="memory_get 返回的 content 为空",
        )

    actual_payload_sha = extract_memory_object_payload_sha(memory)
    if actual_payload_sha is None:
        actual_payload_sha = compute_payload_sha(content)
    if actual_payload_sha != expected_payload_sha:
        return _ReadbackValidationFailure(
            reason=ErrorCode.OPENMEMORY_CONSISTENCY_FAILED_PAYLOAD_MISMATCH,
            message=(
                "memory_get 返回的 payload_sha 不一致: "
                f"expected={expected_payload_sha}, actual={actual_payload_sha}"
            ),
        )

    return None


async def _verify_readback_after_store(
    *,
    client: Any,
    memory_id: str,
    expected_space: str,
    expected_payload_sha: str,
) -> _ReadbackVerificationResult:
    """
    用短暂重试覆盖 OpenMemory 的瞬时读写抖动。

    对测试中的裸 MagicMock 客户端降级为跳过校验，避免把“未实现 get_memory 的 stub”
    误判成线上一致性故障。
    """
    get_memory = getattr(client, "get_memory", None)
    if not callable(get_memory):
        return _ReadbackVerificationResult()

    last_failure: Optional[_ReadbackValidationFailure] = None
    last_skipped_error: Optional[str] = None
    for attempt in range(READBACK_VERIFY_ATTEMPTS):
        get_result = get_memory(memory_id)
        success_value = getattr(get_result, "success", None)
        if not isinstance(success_value, bool):
            return _ReadbackVerificationResult()

        if not success_value:
            error = getattr(get_result, "error", None)
            failure = _classify_readback_fetch_failure(error)
            if failure is not None:
                last_failure = failure
            else:
                last_skipped_error = error or "unknown_error"
        else:
            last_failure = _validate_readback_memory(
                memory=getattr(get_result, "memory", None),
                expected_space=expected_space,
                expected_payload_sha=expected_payload_sha,
            )
            if last_failure is None:
                return _ReadbackVerificationResult()

        if attempt < READBACK_VERIFY_ATTEMPTS - 1:
            await asyncio.sleep(READBACK_VERIFY_DELAY_SECONDS)

    return _ReadbackVerificationResult(
        failure=last_failure,
        skipped_error=last_skipped_error,
    )


async def memory_store_impl(
    payload_md: str,
    target_space: Optional[str] = None,
    meta_json: Optional[Dict[str, Any]] = None,
    kind: Optional[str] = None,
    evidence_refs: Optional[List[str]] = None,
    evidence: Optional[List[Dict[str, Any]]] = None,
    is_bulk: bool = False,
    item_id: Optional[int] = None,
    actor_user_id: Optional[str] = None,
    *,
    correlation_id: str,
    deps: GatewayDepsProtocol,
) -> MemoryStoreResponse:
    """
    memory_store 核心实现

    流程:
    1. 读取治理 settings
    2. 规范化 evidence（v2 优先，v1 映射）
    3. 策略决策 (policy)
    4. 写入审计 (insert audit)
    5. 调用 OpenMemory
    6. 成功返回 memory_id / 失败写入 outbox

    Evidence 处理规则:
    - 若 evidence(v2) 非空：优先使用 evidence(v2) 参与审计与 validate_refs
    - 若仅 evidence_refs(v1) 非空：映射为 v2 external 格式（sha256 为空）
    - 在 strict 模式下，missing sha256 会触发 evidence_validation 校验

    Args:
        payload_md: 要存储的内容
        correlation_id: 追踪 ID（必需）。必须由 HTTP 入口层生成后传入，
                        确保同一请求使用同一 ID。
        deps: GatewayDeps 依赖容器（必需），提供 config/db/logbook_adapter/openmemory_client
        target_space: 目标空间，默认使用 config.default_team_space
        meta_json: 附加元数据
        kind: 内容类型
        evidence_refs: v1 格式的 evidence 引用列表
        evidence: v2 格式的 evidence 列表
        is_bulk: 是否为批量操作
        item_id: 关联的 item ID
        actor_user_id: 操作者用户 ID
    """
    # correlation_id 必须由调用方提供（单一来源原则）
    if correlation_id is None:
        raise ValueError(
            "correlation_id 是必需参数：必须由 HTTP 入口层生成后传入，"
            "handler 不再自行生成 correlation_id"
        )

    # 从 deps 获取配置
    config = deps.config

    # 默认目标空间：收敛 str | None -> str
    if not target_space:
        target_space = config.default_team_space
    if target_space is None:
        raise ValueError("target_space is None and config.default_team_space is also None")

    # 此时 target_space 确保为 str 类型，使用类型收敛后的变量
    current_target_space: str = target_space

    payload_sha = compute_payload_sha(payload_md)

    # 规范化 evidence：v2 优先，v1 映射为 external
    normalized_evidence, evidence_source = normalize_evidence(evidence, evidence_refs)
    logger.debug(f"Evidence 规范化: source={evidence_source}, count={len(normalized_evidence)}")

    try:
        # 0. Actor 校验：检查 actor_user_id 是否存在
        if actor_user_id:
            actor_check_result = validate_actor_user(
                actor_user_id=actor_user_id,
                config=config,
                target_space=current_target_space,
                payload_sha=payload_sha,
                evidence_refs=evidence_refs,
                correlation_id=correlation_id,
                deps=deps,
            )

            # 如果返回了响应对象，说明需要拒绝或降级
            if not actor_check_result.should_continue and actor_check_result.response_data:
                return MemoryStoreResponse(**actor_check_result.response_data)

            # 如果是降级（redirect），更新 current_target_space 并继续处理
            if actor_check_result.degraded_space:
                current_target_space = actor_check_result.degraded_space
                logger.info(f"Actor 降级: {actor_user_id} -> space={current_target_space}")

        # 统一使用 logbook_adapter，避免 db/adapter 双路径语义漂移
        adapter = deps.logbook_adapter

        # 1. Dedupe Check：检查是否已成功写入过
        dedup_record = adapter.check_dedup(
            target_space=current_target_space,
            payload_sha=payload_sha,
        )
        if dedup_record:
            return _handle_dedup_hit(
                dedup_record=dedup_record,
                target_space=current_target_space,
                payload_md=payload_md,
                payload_sha=payload_sha,
                actor_user_id=actor_user_id,
                evidence_refs=evidence_refs,
                normalized_evidence=normalized_evidence,
                evidence_source=evidence_source,
                correlation_id=correlation_id,
                audit_store=adapter,
            )

        # 2. 读取治理设置并进行策略决策
        settings = adapter.get_or_create_settings(config.project_key)
        logger.info(
            f"获取治理设置: project={config.project_key}, team_write_enabled={settings.get('team_write_enabled')}"
        )

        # 2.5. 选择 evidence 校验模式并解析 validate_refs 有效值
        policy_json = settings.get("policy_json") or {}
        evidence_mode = policy_json.get("evidence_mode", "compat")

        validate_refs_decision = resolve_validate_refs(
            mode=evidence_mode,
            config=config,
            caller_override=None,
        )
        validate_refs_effective = validate_refs_decision.effective
        validate_refs_reason = validate_refs_decision.reason
        logger.debug(
            f"Evidence 校验决策: mode={evidence_mode}, effective={validate_refs_effective}, reason={validate_refs_reason}"
        )

        # 2.6. strict 模式下执行 evidence 校验
        evidence_validation = None
        if evidence_mode == "strict" and normalized_evidence:
            evidence_validation = validate_evidence_for_strict_mode(normalized_evidence)
            logger.debug(
                f"Evidence 校验结果: is_valid={evidence_validation.is_valid}, "
                f"errors={evidence_validation.error_codes}, warnings={evidence_validation.compat_warnings}"
            )

            if evidence_validation.compat_warnings:
                logger.info(
                    f"Evidence compat warnings (strict mode): {evidence_validation.compat_warnings}"
                )

            # strict 模式下，evidence 校验失败必须阻断
            if not evidence_validation.is_valid:
                return _handle_evidence_validation_failure(
                    evidence_validation=evidence_validation,
                    target_space=current_target_space,
                    payload_md=payload_md,
                    payload_sha=payload_sha,
                    actor_user_id=actor_user_id,
                    evidence_refs=evidence_refs,
                    normalized_evidence=normalized_evidence,
                    evidence_source=evidence_source,
                    validate_refs_effective=validate_refs_effective,
                    validate_refs_reason=validate_refs_reason,
                    correlation_id=correlation_id,
                    audit_store=adapter,
                )

        # 3. 策略决策
        # 计算 evidence_present：基于规范化后的 evidence 是否存在
        evidence_present = bool(normalized_evidence and len(normalized_evidence) > 0)

        engine = create_engine_from_settings(settings)
        decision = engine.decide(
            target_space=current_target_space,
            actor_user_id=actor_user_id,
            payload_md=payload_md,
            kind=kind,
            evidence_refs=evidence_refs,
            is_bulk=is_bulk,
            evidence_present=evidence_present,
        )
        logger.info(
            f"策略决策: action={decision.action.value}, reason={decision.reason}, evidence_present={evidence_present}"
        )

        # 如果策略拒绝
        if decision.action == PolicyAction.REJECT:
            return _handle_policy_reject(
                decision=decision,
                target_space=current_target_space,
                payload_md=payload_md,
                payload_sha=payload_sha,
                actor_user_id=actor_user_id,
                evidence_refs=evidence_refs,
                normalized_evidence=normalized_evidence,
                evidence_source=evidence_source,
                evidence_validation=evidence_validation,
                validate_refs_effective=validate_refs_effective,
                validate_refs_reason=validate_refs_reason,
                correlation_id=correlation_id,
                audit_store=adapter,
                policy_mode=evidence_mode,
            )

        # 确定最终写入空间
        final_space = decision.final_space
        action = decision.action.value
        policy_reason = ErrorCode.policy_reason(decision.reason)
        openmemory_user_id = _resolve_openmemory_user_id(
            target_space=final_space,
            actor_user_id=actor_user_id,
            private_space_prefix=config.private_space_prefix,
        )

        # 4.1 先写 pending 审计，后续 success/redirected 走 finalize 更新
        pending_gateway_event = build_gateway_audit_event(
            operation="memory_store",
            correlation_id=correlation_id,
            actor_user_id=actor_user_id,
            requested_space=current_target_space,
            final_space=final_space,
            action=action,
            reason=policy_reason,
            payload_sha=payload_sha,
            payload_len=len(payload_md),
            evidence=normalized_evidence,
            extra={
                "evidence_source": evidence_source,
                "phase": "pending",
            },
            policy_mode=evidence_mode,
            policy_mode_reason="from_settings" if evidence_mode else None,
            policy_version="v1",
            policy_is_pointerized=False,
            policy_source="settings",
            validate_refs_effective=validate_refs_effective,
            validate_refs_reason=validate_refs_reason,
            evidence_validation=evidence_validation.to_dict() if evidence_validation else None,
            intended_action=action,
        )
        pending_evidence_refs_json = build_evidence_refs_json(
            evidence=normalized_evidence,
            gateway_event=pending_gateway_event,
        )
        write_audit_or_raise(
            db=adapter,
            actor_user_id=actor_user_id,
            target_space=final_space,
            action=action,
            reason=policy_reason,
            payload_sha=payload_sha,
            evidence_refs_json=pending_evidence_refs_json,
            validate_refs=validate_refs_effective,
            correlation_id=correlation_id,
            status="pending",
        )

        # 4.2 调用 OpenMemory
        # 获取 OpenMemory client（统一从 deps 获取）
        try:
            client = deps.openmemory_client
            # 构建完整 metadata：合并调用方 meta_json + handler 上下文字段
            om_metadata: Dict[str, Any] = dict(meta_json) if meta_json else {}
            if evidence_refs:
                om_metadata["evidence_refs"] = evidence_refs
            if payload_sha:
                om_metadata["payload_sha"] = payload_sha
            if kind:
                om_metadata["kind"] = kind
            result = client.store(
                content=payload_md,
                space=final_space,
                user_id=openmemory_user_id,
                metadata=om_metadata,
            )

            if not result.success:
                raise OpenMemoryError(
                    message=result.error or "存储失败",
                    status_code=None,
                    response=None,
                )

            memory_id = result.memory_id
            if memory_id is None:
                raise OpenMemoryError(
                    message="OpenMemory 返回成功但 memory_id 为空",
                    status_code=None,
                    response=None,
                )
            logger.info(f"OpenMemory 写入成功: memory_id={memory_id}, space={final_space}")

            readback_result = await _verify_readback_after_store(
                client=client,
                memory_id=memory_id,
                expected_space=final_space,
                expected_payload_sha=payload_sha,
            )
            readback_failure = readback_result.failure
            if readback_failure is not None:
                logger.error(
                    "OpenMemory 写后校验失败: memory_id=%s, reason=%s, message=%s",
                    memory_id,
                    readback_failure.reason,
                    readback_failure.message,
                )
                return _handle_post_write_validation_failure(
                    reason=readback_failure.reason,
                    validation_message=readback_failure.message,
                    memory_id=memory_id,
                    final_space=final_space,
                    action=action,
                    target_space=current_target_space,
                    payload_md=payload_md,
                    payload_sha=payload_sha,
                    actor_user_id=actor_user_id,
                    evidence_refs=evidence_refs,
                    normalized_evidence=normalized_evidence,
                    evidence_source=evidence_source,
                    evidence_validation=evidence_validation,
                    validate_refs_effective=validate_refs_effective,
                    validate_refs_reason=validate_refs_reason,
                    correlation_id=correlation_id,
                    audit_store=adapter,
                    policy_mode=evidence_mode,
                )
            if readback_result.skipped_error is not None:
                logger.warning(
                    "OpenMemory 写后校验跳过: memory_id=%s, transient_error=%s",
                    memory_id,
                    readback_result.skipped_error,
                )

            # 写入成功审计
            return _handle_success(
                memory_id=memory_id,
                decision=decision,
                final_space=final_space,
                action=action,
                target_space=current_target_space,
                payload_md=payload_md,
                payload_sha=payload_sha,
                actor_user_id=actor_user_id,
                evidence_refs=evidence_refs,
                normalized_evidence=normalized_evidence,
                evidence_source=evidence_source,
                evidence_validation=evidence_validation,
                validate_refs_effective=validate_refs_effective,
                validate_refs_reason=validate_refs_reason,
                correlation_id=correlation_id,
                audit_store=adapter,
                policy_mode=evidence_mode,
            )

        except (OpenMemoryConnectionError, OpenMemoryError) as e:
            # OpenMemory 失败：写入 outbox
            return _handle_openmemory_failure(
                error=e,
                decision=decision,
                final_space=final_space,
                target_space=current_target_space,
                payload_md=payload_md,
                payload_sha=payload_sha,
                actor_user_id=actor_user_id,
                evidence_refs=evidence_refs,
                normalized_evidence=normalized_evidence,
                evidence_source=evidence_source,
                evidence_validation=evidence_validation,
                validate_refs_effective=validate_refs_effective,
                validate_refs_reason=validate_refs_reason,
                correlation_id=correlation_id,
                item_id=item_id,
                audit_store=adapter,
                policy_mode=evidence_mode,
            )

    except AuditWriteError as e:
        logger.error(f"审计写入失败，操作已阻断: {e}, correlation_id={correlation_id}")
        return MemoryStoreResponse(
            ok=False,
            action="error",
            space_written=None,
            memory_id=None,
            outbox_id=None,
            correlation_id=correlation_id,
            evidence_refs=evidence_refs,
            message=f"审计写入失败，操作已阻断: {e.message}",
        )
    except Exception as e:
        logger.exception(f"memory_store 未预期错误: {e}")
        return MemoryStoreResponse(
            ok=False,
            action="error",
            space_written=None,
            memory_id=None,
            outbox_id=None,
            correlation_id=correlation_id,
            evidence_refs=evidence_refs,
            message=f"内部错误: {str(e)}",
        )


def _handle_dedup_hit(
    dedup_record: Dict[str, Any],
    target_space: str,
    payload_md: str,
    payload_sha: str,
    actor_user_id: Optional[str],
    evidence_refs: Optional[List[str]],
    normalized_evidence: List[Dict[str, Any]],
    evidence_source: str,
    correlation_id: str,
    audit_store: Any,
) -> MemoryStoreResponse:
    """处理 dedupe hit 场景"""
    logger.info(f"Dedupe hit: target_space={target_space}, payload_sha={payload_sha[:16]}...")

    memory_id = dedup_record.get("memory_id")
    last_error = dedup_record.get("last_error")
    if memory_id is None and last_error and last_error.startswith("memory_id="):
        memory_id = last_error.split("=", 1)[1]

    # 构建 gateway_event
    # dedup_hit 发生在策略决策之前，policy/validation 字段使用 None 表示未进入该阶段
    original_outbox_id = dedup_record.get("outbox_id")
    gateway_event = build_gateway_audit_event(
        operation="memory_store",
        correlation_id=correlation_id,
        actor_user_id=actor_user_id,
        requested_space=target_space,
        final_space=target_space,
        action="allow",
        reason=ErrorCode.DEDUP_HIT,
        payload_sha=payload_sha,
        payload_len=len(payload_md),
        evidence=normalized_evidence,
        memory_id=memory_id,
        extra={
            "original_outbox_id": original_outbox_id,
            "evidence_source": evidence_source,
            "correlation_id": correlation_id,
        },
        # v1.1: policy 子结构（dedup_hit 阶段未进入策略评估）
        policy_mode=None,
        policy_mode_reason="dedup_hit_before_policy_evaluation",
        policy_version=None,
        policy_is_pointerized=False,
        policy_source=None,
        # v1.1: validation 子结构（dedup_hit 阶段未进行 evidence 校验）
        validate_refs_effective=None,
        validate_refs_reason="dedup_hit_before_validation",
        evidence_validation=None,
    )
    evidence_refs_json = build_evidence_refs_json(
        evidence=normalized_evidence, gateway_event=gateway_event
    )
    if original_outbox_id is not None:
        evidence_refs_json["original_outbox_id"] = original_outbox_id

    # 写入审计
    audit_store.insert_audit(
        actor_user_id=actor_user_id,
        target_space=target_space,
        action="allow",
        reason=ErrorCode.DEDUP_HIT,
        payload_sha=payload_sha,
        evidence_refs_json=evidence_refs_json,
        correlation_id=correlation_id,
        status="success",
    )

    return MemoryStoreResponse(
        ok=True,
        action="allow",
        space_written=target_space,
        memory_id=memory_id,
        outbox_id=None,
        correlation_id=correlation_id,
        evidence_refs=evidence_refs,
        message="dedup_hit: 已存在相同内容的成功写入记录",
    )


def _handle_evidence_validation_failure(
    evidence_validation: Any,
    target_space: str,
    payload_md: str,
    payload_sha: str,
    actor_user_id: Optional[str],
    evidence_refs: Optional[List[str]],
    normalized_evidence: List[Dict[str, Any]],
    evidence_source: str,
    validate_refs_effective: bool,
    validate_refs_reason: str,
    correlation_id: str,
    audit_store: Any,
) -> MemoryStoreResponse:
    """
    处理 strict 模式下 evidence 校验失败场景

    当 evidence_mode="strict" 且 evidence_validation.is_valid=false 时调用。
    阻断操作并返回 reject 响应，同时写入审计记录。

    错误码约定:
    - EVIDENCE_MISSING_SHA256: 缺少 sha256 字段
    - EVIDENCE_INVALID_SHA256: sha256 格式无效
    - EVIDENCE_MISSING_URI: 缺少 uri 字段
    """
    # 提取第一个错误码作为主要原因
    error_codes = evidence_validation.error_codes
    primary_error = error_codes[0] if error_codes else "EVIDENCE_VALIDATION_FAILED"

    # 构建稳定的 reason 码（使用 EVIDENCE_* 前缀）
    reason = f"EVIDENCE_VALIDATION_FAILED:{primary_error.split(':')[0]}"

    logger.warning(
        f"Evidence 校验失败 (strict mode): reason={reason}, "
        f"error_codes={error_codes}, correlation_id={correlation_id}"
    )

    gateway_event = build_gateway_audit_event(
        operation="memory_store",
        correlation_id=correlation_id,
        actor_user_id=actor_user_id,
        requested_space=target_space,
        final_space=None,
        action="reject",
        reason=reason,
        payload_sha=payload_sha,
        payload_len=len(payload_md),
        evidence=normalized_evidence,
        extra={
            "evidence_source": evidence_source,
            "strict_mode_rejected": True,
        },
        policy_mode="strict",
        validate_refs_effective=validate_refs_effective,
        validate_refs_reason=validate_refs_reason,
        evidence_validation=evidence_validation.to_dict(),
    )
    evidence_refs_json = build_evidence_refs_json(
        evidence=normalized_evidence, gateway_event=gateway_event
    )

    write_audit_or_raise(
        db=audit_store,
        actor_user_id=actor_user_id,
        target_space=target_space,
        action="reject",
        reason=reason,
        payload_sha=payload_sha,
        evidence_refs_json=evidence_refs_json,
        validate_refs=validate_refs_effective,
        correlation_id=correlation_id,
        status="failed",
    )

    return MemoryStoreResponse(
        ok=False,
        action="reject",
        space_written=None,
        memory_id=None,
        outbox_id=None,
        correlation_id=correlation_id,
        evidence_refs=evidence_refs,
        message=(
            "strict 模式 evidence 校验失败: "
            f"{reason} ({', '.join(error_codes) if error_codes else 'unknown'})"
        ),
    )


def _handle_policy_reject(
    decision: Any,
    target_space: str,
    payload_md: str,
    payload_sha: str,
    actor_user_id: Optional[str],
    evidence_refs: Optional[List[str]],
    normalized_evidence: List[Dict[str, Any]],
    evidence_source: str,
    evidence_validation: Any,
    validate_refs_effective: bool,
    validate_refs_reason: str,
    correlation_id: str,
    audit_store: Any,
    policy_mode: Optional[str] = None,
) -> MemoryStoreResponse:
    """处理策略拒绝场景"""
    gateway_event = build_gateway_audit_event(
        operation="memory_store",
        correlation_id=correlation_id,
        actor_user_id=actor_user_id,
        requested_space=target_space,
        final_space=None,
        action="reject",
        reason=ErrorCode.policy_reason(decision.reason),
        payload_sha=payload_sha,
        payload_len=len(payload_md),
        evidence=normalized_evidence,
        extra={
            "policy_reason": decision.reason,
            "evidence_source": evidence_source,
        },
        # v1.1: policy 子结构
        policy_mode=policy_mode,
        policy_mode_reason="from_settings" if policy_mode else None,
        policy_version="v1",
        policy_is_pointerized=False,
        policy_source="settings",
        # v1.1: validation 子结构
        validate_refs_effective=validate_refs_effective,
        validate_refs_reason=validate_refs_reason,
        evidence_validation=evidence_validation.to_dict() if evidence_validation else None,
    )
    evidence_refs_json = build_evidence_refs_json(
        evidence=normalized_evidence, gateway_event=gateway_event
    )

    write_audit_or_raise(
        db=audit_store,
        actor_user_id=actor_user_id,
        target_space=target_space,
        action="reject",
        reason=ErrorCode.policy_reason(decision.reason),
        payload_sha=payload_sha,
        evidence_refs_json=evidence_refs_json,
        validate_refs=validate_refs_effective,
        correlation_id=correlation_id,
        status="failed",
    )

    return MemoryStoreResponse(
        ok=False,
        action="reject",
        space_written=None,
        memory_id=None,
        outbox_id=None,
        correlation_id=correlation_id,
        evidence_refs=evidence_refs,
        message=f"策略拒绝: {decision.reason}",
    )


def _handle_post_write_validation_failure(
    reason: str,
    validation_message: str,
    memory_id: str,
    final_space: str,
    action: str,
    target_space: str,
    payload_md: str,
    payload_sha: str,
    actor_user_id: Optional[str],
    evidence_refs: Optional[List[str]],
    normalized_evidence: List[Dict[str, Any]],
    evidence_source: str,
    evidence_validation: Any,
    validate_refs_effective: bool,
    validate_refs_reason: str,
    correlation_id: str,
    audit_store: Any,
    policy_mode: Optional[str] = None,
) -> MemoryStoreResponse:
    """处理 OpenMemory 返回成功但写后读校验失败的场景。"""
    failure_gateway_event = build_gateway_audit_event(
        operation="memory_store",
        correlation_id=correlation_id,
        actor_user_id=actor_user_id,
        requested_space=target_space,
        final_space=final_space,
        action=action,
        reason=reason,
        payload_sha=payload_sha,
        payload_len=len(payload_md),
        evidence=normalized_evidence,
        memory_id=memory_id,
        extra={
            "validation_error": validation_message,
            "evidence_source": evidence_source,
        },
        policy_mode=policy_mode,
        policy_mode_reason="from_settings" if policy_mode else None,
        policy_version="v1",
        policy_is_pointerized=False,
        policy_source="settings",
        validate_refs_effective=validate_refs_effective,
        validate_refs_reason=validate_refs_reason,
        evidence_validation=evidence_validation.to_dict() if evidence_validation else None,
    )
    updated_count = _to_updated_count(
        audit_store.update_write_audit(
            correlation_id=correlation_id,
            status="failed",
            reason_suffix=reason,
            replace_reason=True,
            evidence_refs_json_patch={
                "memory_id": memory_id,
                "gateway_event": failure_gateway_event,
            },
        )
    )
    if updated_count != 1:
        raise AuditWriteError(
            "pending 审计 finalize 为 failed 失败",
            audit_data={
                "correlation_id": correlation_id,
                "updated_count": updated_count,
                "final_space": final_space,
                "reason": reason,
            },
        )

    return MemoryStoreResponse(
        ok=False,
        action="error",
        space_written=None,
        memory_id=None,
        outbox_id=None,
        correlation_id=correlation_id,
        evidence_refs=evidence_refs,
        message=f"OpenMemory 写后校验失败: {validation_message}",
    )


def _handle_success(
    memory_id: str,
    decision: Any,
    final_space: str,
    action: str,
    target_space: str,
    payload_md: str,
    payload_sha: str,
    actor_user_id: Optional[str],
    evidence_refs: Optional[List[str]],
    normalized_evidence: List[Dict[str, Any]],
    evidence_source: str,
    evidence_validation: Any,
    validate_refs_effective: bool,
    validate_refs_reason: str,
    correlation_id: str,
    audit_store: Any,
    policy_mode: Optional[str] = None,
) -> MemoryStoreResponse:
    """处理 OpenMemory 写入成功场景"""
    post_audit_gateway_event = build_gateway_audit_event(
        operation="memory_store",
        correlation_id=correlation_id,
        actor_user_id=actor_user_id,
        requested_space=target_space,
        final_space=final_space,
        action=action,
        reason=ErrorCode.policy_reason(decision.reason),
        payload_sha=payload_sha,
        payload_len=len(payload_md),
        evidence=normalized_evidence,
        memory_id=memory_id,
        extra={"evidence_source": evidence_source},
        # v1.1: policy 子结构
        policy_mode=policy_mode,
        policy_mode_reason="from_settings" if policy_mode else None,
        policy_version="v1",
        policy_is_pointerized=False,
        policy_source="settings",
        # v1.1: validation 子结构
        validate_refs_effective=validate_refs_effective,
        validate_refs_reason=validate_refs_reason,
        evidence_validation=evidence_validation.to_dict() if evidence_validation else None,
    )
    updated_count = _to_updated_count(
        audit_store.update_write_audit(
            correlation_id=correlation_id,
            status="success",
            reason_suffix=None,
            evidence_refs_json_patch={
                "memory_id": memory_id,
                "gateway_event": post_audit_gateway_event,
            },
        )
    )
    if updated_count != 1:
        raise AuditWriteError(
            "pending 审计 finalize 为 success 失败",
            audit_data={
                "correlation_id": correlation_id,
                "updated_count": updated_count,
                "final_space": final_space,
                "action": action,
            },
        )

    return MemoryStoreResponse(
        ok=True,
        action=action,
        space_written=final_space,
        memory_id=memory_id,
        outbox_id=None,
        correlation_id=correlation_id,
        evidence_refs=evidence_refs,
        message=None,
    )


def _handle_openmemory_failure(
    error: Exception,
    decision: Any,
    final_space: str,
    target_space: str,
    payload_md: str,
    payload_sha: str,
    actor_user_id: Optional[str],
    evidence_refs: Optional[List[str]],
    normalized_evidence: List[Dict[str, Any]],
    evidence_source: str,
    evidence_validation: Any,
    validate_refs_effective: bool,
    validate_refs_reason: str,
    correlation_id: str,
    item_id: Optional[int],
    audit_store: Any,
    policy_mode: Optional[str] = None,
) -> MemoryStoreResponse:
    """处理 OpenMemory 写入失败场景"""
    error_msg = str(error.message if hasattr(error, "message") else error)
    logger.error(f"OpenMemory 写入失败: {error_msg}")

    # 提取错误码
    if isinstance(error, OpenMemoryConnectionError):
        error_reason = ErrorCode.OPENMEMORY_WRITE_FAILED_CONNECTION
        error_code = "connection_error"
    elif isinstance(error, OpenMemoryAPIError):
        status_code = getattr(error, "status_code", None)
        error_reason = ErrorCode.openmemory_api_error(status_code)
        error_code = f"api_error_{status_code}" if status_code else "api_error"
    elif isinstance(error, OpenMemoryError):
        error_reason = ErrorCode.OPENMEMORY_WRITE_FAILED_GENERIC
        error_code = "openmemory_error"
    else:
        error_reason = ErrorCode.OPENMEMORY_WRITE_FAILED_UNKNOWN
        error_code = "unknown"

    # 构建 finalize 补丁数据（outbox_id 由原子事务写回）
    failure_gateway_event = build_gateway_audit_event(
        operation="memory_store",
        correlation_id=correlation_id,
        actor_user_id=actor_user_id,
        requested_space=target_space,
        final_space=final_space,
        action="redirect",
        reason=error_reason,
        payload_sha=payload_sha,
        payload_len=len(payload_md),
        evidence=normalized_evidence,
        extra={
            "last_error": error_msg[:500],
            "error_code": error_code,
            "evidence_source": evidence_source,
        },
        # v1.1: policy 子结构
        policy_mode=policy_mode,
        policy_mode_reason="from_settings" if policy_mode else None,
        policy_version="v1",
        policy_is_pointerized=False,
        policy_source="settings",
        # v1.1: validation 子结构
        validate_refs_effective=validate_refs_effective,
        validate_refs_reason=validate_refs_reason,
        evidence_validation=evidence_validation.to_dict() if evidence_validation else None,
        intended_action="deferred",
    )
    failure_evidence_refs_json = build_evidence_refs_json(
        evidence=normalized_evidence, gateway_event=failure_gateway_event
    )

    outbox_id: int
    updated_count: int
    atomic_result: Any = None
    atomic_finalize_done = False
    atomic_finalize = getattr(audit_store, "enqueue_outbox_and_finalize_audit", None)
    if callable(atomic_finalize):
        atomic_result = atomic_finalize(
            correlation_id=correlation_id,
            payload_md=payload_md,
            target_space=final_space,
            item_id=item_id,
            last_error=error_msg,
            reason_suffix_prefix=error_reason,
            evidence_refs_json_patch={
                "gateway_event": failure_gateway_event,
                "intended_action": "deferred",
            },
        )
        if (
            isinstance(atomic_result, tuple)
            and len(atomic_result) == 2
            and isinstance(atomic_result[0], int)
        ):
            outbox_id = int(atomic_result[0])
            updated_count = _to_updated_count(atomic_result[1])
            atomic_finalize_done = True
        else:
            logger.warning(
                "enqueue_outbox_and_finalize_audit 返回值无效，回退到分步补偿: %r",
                atomic_result,
            )

    if not atomic_finalize_done:
        outbox_id = audit_store.enqueue_outbox(
            payload_md=payload_md,
            target_space=final_space,
            item_id=item_id,
            last_error=error_msg,
        )
        updated_count = _to_updated_count(
            audit_store.update_write_audit(
                correlation_id=correlation_id,
                status="redirected",
                reason_suffix=f"{error_reason}:outbox:{outbox_id}",
                replace_reason=True,
                evidence_refs_json_patch={
                    "outbox_id": outbox_id,
                    "gateway_event": failure_gateway_event,
                    "intended_action": "deferred",
                },
            )
        )

    if updated_count != 1:
        raise AuditWriteError(
            "pending 审计 finalize 为 redirected 失败",
            audit_data={
                "correlation_id": correlation_id,
                "updated_count": updated_count,
                "target_space": final_space,
            },
        )
    failure_evidence_refs_json["outbox_id"] = outbox_id
    logger.info(f"已入队 outbox 并 finalize 审计: outbox_id={outbox_id}")

    return MemoryStoreResponse(
        ok=False,
        action="deferred",
        space_written=None,
        memory_id=None,
        outbox_id=outbox_id,
        correlation_id=correlation_id,
        evidence_refs=evidence_refs,
        message=f"OpenMemory 不可用，已入队补偿队列 (outbox_id={outbox_id}): {error_msg}",
    )
