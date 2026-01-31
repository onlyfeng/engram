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
                       依赖注入与迁移指引
================================================================================

推荐的依赖获取方式（优先级从高到低）:

1. 通过 deps 参数传入 GatewayDeps (推荐):
   ```python
   from engram.gateway.di import GatewayDeps

   deps = GatewayDeps.create()  # 或 GatewayDeps.for_testing(...)
   result = await memory_store_impl(
       payload_md="...",
       correlation_id="...",
       deps=deps,
   )
   ```

2. 通过 _config, _db, _openmemory_client 参数 (向后兼容):
   ```python
   result = await memory_store_impl(
       payload_md="...",
       correlation_id="...",
       _config=my_config,
       _db=my_db,
   )
   ```

3. 使用模块级全局函数 (已弃用):
   不传入任何依赖参数时，使用 get_config() / get_db() 等全局函数

================================================================================
                       correlation_id 单一来源原则
================================================================================

correlation_id 是必需参数，必须由调用方（HTTP 入口层）生成后传入。
handler 不再自行生成 correlation_id，确保同一请求使用同一 ID。

错误响应中的 correlation_id 必须与请求保持一致。
"""

import logging
import warnings
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from pydantic import BaseModel

# NOTE: logbook_db.get_db 已弃用，新代码应通过 deps.db 或 deps.logbook_adapter 获取数据库操作
from ..audit_event import (
    AuditWriteError,
    build_evidence_refs_json,
    build_gateway_audit_event,
    normalize_evidence,
    validate_evidence_for_strict_mode,
)
from ..config import GatewayConfig, get_config, resolve_validate_refs
from ..di import GatewayDeps, GatewayDepsProtocol
from ..openmemory_client import (
    OpenMemoryAPIError,
    OpenMemoryConnectionError,
    OpenMemoryError,
    get_client,
)
from ..policy import PolicyAction, create_engine_from_settings
from ..services.actor_validation import validate_actor_user
from ..services.audit_service import write_audit_or_raise
from ..services.hash_utils import compute_payload_sha

if TYPE_CHECKING:
    # LogbookDatabase 类型仅用于向后兼容，新代码应使用 LogbookAdapter
    from ..logbook_db import LogbookDatabase
    from ..openmemory_client import OpenMemoryClient

# 导入统一错误码
try:
    from engram.logbook.errors import ErrorCode
except ImportError:

    class ErrorCode:
        DEDUP_HIT = "dedup_hit"

        @staticmethod
        def policy_reason(reason):
            return f"policy:{reason}"

        @staticmethod
        def openmemory_api_error(status_code):
            return f"openmemory_api_error:{status_code}"

        OPENMEMORY_WRITE_FAILED_CONNECTION = "openmemory_write_failed:connection"
        OPENMEMORY_WRITE_FAILED_GENERIC = "openmemory_write_failed:generic"
        OPENMEMORY_WRITE_FAILED_UNKNOWN = "openmemory_write_failed:unknown"


logger = logging.getLogger("gateway.handlers.memory_store")


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
    correlation_id: Optional[str] = None,
    # 依赖注入参数（推荐方式）
    deps: Optional[GatewayDepsProtocol] = None,
    # [DEPRECATED] 以下参数将在后续版本移除，请使用 deps 参数
    # 迁移计划：这些参数仅为向后兼容保留，新代码应使用 deps=GatewayDeps.create() 或 deps=GatewayDeps.for_testing(...)
    _config: Optional[GatewayConfig] = None,
    _db: Optional["LogbookDatabase"] = None,
    _openmemory_client: Optional["OpenMemoryClient"] = None,
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
        correlation_id: 追踪 ID（必需）。必须由 HTTP 入口层生成后传入，
                        确保同一请求使用同一 ID。handler 不再自行生成。
        deps: 可选的 GatewayDeps 依赖容器，优先使用其中的依赖
        _config: 可选的 GatewayConfig 对象，不传则使用 get_config()
        _db: 可选的 LogbookDatabase 对象，用于向后兼容（推荐使用 deps.db）
        _openmemory_client: 可选的 OpenMemoryClient 对象，用于向后兼容（推荐使用 deps.openmemory_client）

    Raises:
        ValueError: 如果 correlation_id 未提供
    """
    # correlation_id 必须由调用方提供（单一来源原则）
    if correlation_id is None:
        raise ValueError(
            "correlation_id 是必需参数：必须由 HTTP 入口层生成后传入，"
            "handler 不再自行生成 correlation_id"
        )

    # [DEPRECATED] 弃用警告：_config/_db/_openmemory_client 参数将在后续版本移除
    if _config is not None or _db is not None or _openmemory_client is not None:
        warnings.warn(
            "memory_store_impl 的 _config/_db/_openmemory_client 参数已弃用，"
            "请使用 deps=GatewayDeps.create() 或 deps=GatewayDeps.for_testing(...) 替代。"
            "这些参数将在后续版本移除。",
            DeprecationWarning,
            stacklevel=2,
        )

    # 获取配置（支持依赖注入）：deps 优先 > _config 参数 > 全局 getter
    if deps is not None:
        config = deps.config
    elif _config is not None:
        config = _config
    else:
        config = get_config()

    # 确保有可用的 deps 对象（用于后续依赖统一获取）
    # 如果调用方未提供 deps，创建一个延迟初始化的 deps 容器
    if deps is None:
        deps = GatewayDeps.create(config=config)
        # [LEGACY] 兼容分支：如果有显式传入的 _db 或 _openmemory_client，注入到 deps 中
        # 此路径仅为向后兼容保留，新代码应完全使用 deps 参数
        if _db is not None:
            deps._db = _db
        if _openmemory_client is not None:
            deps._openmemory_client = _openmemory_client

    # 默认目标空间
    if not target_space:
        target_space = config.default_team_space

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
                target_space=target_space,
                payload_sha=payload_sha,
                evidence_refs=evidence_refs,
                correlation_id=correlation_id,
                deps=deps,
            )

            # 如果返回了响应对象，说明需要拒绝或降级
            if not actor_check_result.should_continue and actor_check_result.response_data:
                return MemoryStoreResponse(**actor_check_result.response_data)

            # 如果是降级（redirect），更新 target_space 并继续处理
            if actor_check_result.degraded_space:
                target_space = actor_check_result.degraded_space
                logger.info(f"Actor 降级: {actor_user_id} -> space={target_space}")

        # 获取 DB 实例（统一通过 deps 获取，支持依赖注入）
        db = deps.db

        # 获取 logbook_adapter（统一通过 deps 获取，确保 settings/audit/outbox/dedup 使用同一实例）
        adapter = deps.logbook_adapter

        # 1. Dedupe Check：检查是否已成功写入过
        dedup_record = adapter.check_dedup(
            target_space=target_space,
            payload_sha=payload_sha,
        )
        if dedup_record:
            return _handle_dedup_hit(
                dedup_record=dedup_record,
                target_space=target_space,
                payload_md=payload_md,
                payload_sha=payload_sha,
                actor_user_id=actor_user_id,
                evidence_refs=evidence_refs,
                normalized_evidence=normalized_evidence,
                evidence_source=evidence_source,
                correlation_id=correlation_id,
                db=db,  # 通过 deps.db 传入
            )

        # 2. 读取治理设置并进行策略决策
        settings = db.get_or_create_settings(config.project_key)
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
                    target_space=target_space,
                    payload_md=payload_md,
                    payload_sha=payload_sha,
                    actor_user_id=actor_user_id,
                    evidence_refs=evidence_refs,
                    normalized_evidence=normalized_evidence,
                    evidence_source=evidence_source,
                    validate_refs_effective=validate_refs_effective,
                    validate_refs_reason=validate_refs_reason,
                    correlation_id=correlation_id,
                    db=db,
                )

        # 3. 策略决策
        # 计算 evidence_present：基于规范化后的 evidence 是否存在
        evidence_present = bool(normalized_evidence and len(normalized_evidence) > 0)

        engine = create_engine_from_settings(settings)
        decision = engine.decide(
            target_space=target_space,
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
                target_space=target_space,
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
                db=db,
                policy_mode=evidence_mode,
            )

        # 确定最终写入空间
        final_space = decision.final_space
        action = decision.action.value

        # 4. 调用 OpenMemory
        # 获取 OpenMemory client（支持依赖注入）：deps 优先 > _openmemory_client 参数 > 全局 getter
        # 注意：不使用不带 config 的 get_client()，确保 base_url/api_key 来自 config
        try:
            if deps is not None:
                client = deps.openmemory_client
            elif _openmemory_client is not None:
                client = _openmemory_client
            else:
                client = get_client(config)
            result = client.store(
                content=payload_md,
                space=final_space,
                metadata=meta_json,
            )

            if not result.success:
                raise OpenMemoryError(
                    message=result.error or "存储失败",
                    status_code=None,
                    response=None,
                )

            memory_id = result.memory_id
            logger.info(f"OpenMemory 写入成功: memory_id={memory_id}, space={final_space}")

            # 写入成功审计
            return _handle_success(
                memory_id=memory_id,
                decision=decision,
                final_space=final_space,
                action=action,
                target_space=target_space,
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
                db=db,
                policy_mode=evidence_mode,
            )

        except (OpenMemoryConnectionError, OpenMemoryError) as e:
            # OpenMemory 失败：写入 outbox
            return _handle_openmemory_failure(
                error=e,
                decision=decision,
                final_space=final_space,
                target_space=target_space,
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
                db=db,
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
    db: Any,  # LogbookDatabase 或 LogbookAdapter 实例，通过 deps 传入
) -> MemoryStoreResponse:
    """处理 dedupe hit 场景"""
    logger.info(f"Dedupe hit: target_space={target_space}, payload_sha={payload_sha[:16]}...")

    # 从 last_error 中提取 memory_id
    memory_id = None
    last_error = dedup_record.get("last_error")
    if last_error and last_error.startswith("memory_id="):
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

    # 写入审计（db 实例从 deps 参数传入）
    db.insert_audit(
        actor_user_id=actor_user_id,
        target_space=target_space,
        action="allow",
        reason=ErrorCode.DEDUP_HIT,
        payload_sha=payload_sha,
        evidence_refs_json=evidence_refs_json,
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
    db: Any,
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
        db=db,
        actor_user_id=actor_user_id,
        target_space=target_space,
        action="reject",
        reason=reason,
        payload_sha=payload_sha,
        evidence_refs_json=evidence_refs_json,
        validate_refs=validate_refs_effective,
        correlation_id=correlation_id,
    )

    return MemoryStoreResponse(
        ok=False,
        action="reject",
        space_written=None,
        memory_id=None,
        outbox_id=None,
        correlation_id=correlation_id,
        evidence_refs=evidence_refs,
        message=f"strict 模式 evidence 校验失败: {', '.join(error_codes)}",
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
    db: Any,
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
        db=db,
        actor_user_id=actor_user_id,
        target_space=target_space,
        action="reject",
        reason=ErrorCode.policy_reason(decision.reason),
        payload_sha=payload_sha,
        evidence_refs_json=evidence_refs_json,
        validate_refs=validate_refs_effective,
        correlation_id=correlation_id,
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
    db: Any,
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
    post_audit_evidence_refs_json = build_evidence_refs_json(
        evidence=normalized_evidence, gateway_event=post_audit_gateway_event
    )

    write_audit_or_raise(
        db=db,
        actor_user_id=actor_user_id,
        target_space=final_space,
        action=action,
        reason=ErrorCode.policy_reason(decision.reason),
        payload_sha=payload_sha,
        evidence_refs_json=post_audit_evidence_refs_json,
        validate_refs=validate_refs_effective,
        correlation_id=correlation_id,
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
    db: Any,
    policy_mode: Optional[str] = None,
) -> MemoryStoreResponse:
    """处理 OpenMemory 写入失败场景"""
    error_msg = str(error.message if hasattr(error, "message") else error)
    logger.error(f"OpenMemory 写入失败: {error_msg}")

    # 先写入 outbox
    outbox_id = db.enqueue_outbox(
        payload_md=payload_md,
        target_space=final_space,
        item_id=item_id,
        last_error=error_msg,
    )
    logger.info(f"已入队 outbox: outbox_id={outbox_id}")

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

    # 构建失败审计
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
        outbox_id=outbox_id,
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

    try:
        db.insert_audit(
            actor_user_id=actor_user_id,
            target_space=final_space,
            action="redirect",
            reason=error_reason,
            payload_sha=payload_sha,
            evidence_refs_json=failure_evidence_refs_json,
            validate_refs=validate_refs_effective,
        )
    except Exception as failure_audit_err:
        logger.error(
            f"失败审计写入失败: {failure_audit_err}, "
            f"correlation_id={correlation_id}, outbox_id={outbox_id}"
        )

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
