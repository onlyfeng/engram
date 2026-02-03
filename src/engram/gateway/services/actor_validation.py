"""
actor_validation - Actor 用户校验服务模块

提供 actor_user_id 校验与策略处理逻辑。

策略说明:
- reject: 用户不存在时拒绝请求
- degrade: 用户不存在时降级到 private:unknown 空间
- auto_create: 用户不存在时自动创建

依赖注入:
- 通过 deps: GatewayDeps 参数统一获取 db 和 logbook_adapter
- 确保 actor 校验阶段写入审计的 correlation_id 与入口一致
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..audit_event import build_evidence_refs_json, build_gateway_audit_event
from ..config import UnknownActorPolicy
from ..di import GatewayDepsProtocol

# 导入统一错误码
try:
    from engram.logbook.errors import ErrorCode
except ImportError:
    # Fallback for testing
    class ErrorCode:  # type: ignore[no-redef]
        ACTOR_UNKNOWN_REJECT = "actor_unknown:reject"
        ACTOR_UNKNOWN_DEGRADE = "actor_unknown:degrade"
        ACTOR_AUTOCREATED = "actor_autocreated"
        ACTOR_AUTOCREATE_FAILED = "actor_autocreate_failed"


logger = logging.getLogger("gateway.services.actor_validation")


@dataclass
class ActorValidationResult:
    """
    Actor 校验结果

    Attributes:
        should_continue: 是否应继续正常流程
        response_data: 需要返回给调用方的响应数据（当 should_continue=False 时）
        degraded_space: 降级后的目标空间（当发生降级时）
    """

    should_continue: bool
    response_data: Optional[Dict[str, Any]] = None
    degraded_space: Optional[str] = None


def validate_actor_user(
    actor_user_id: str,
    config: Any,
    target_space: str,
    payload_sha: str,
    evidence_refs: Optional[List[str]],
    correlation_id: str,
    deps: GatewayDepsProtocol,
) -> ActorValidationResult:
    """
    校验 actor_user_id 是否存在，根据配置执行相应策略

    策略说明:
    - reject: 用户不存在时拒绝请求
    - degrade: 用户不存在时降级到 private:unknown 空间
    - auto_create: 用户不存在时自动创建

    Args:
        actor_user_id: 操作者用户标识
        config: GatewayConfig 配置对象
        target_space: 原始目标空间
        payload_sha: 内容哈希
        evidence_refs: 证据引用
        correlation_id: 关联 ID（确保与入口一致用于审计追踪）
        deps: GatewayDeps 依赖容器，提供 db 和 logbook_adapter

    Returns:
        ActorValidationResult:
        - should_continue=True: 用户存在或已自动创建，继续正常流程
        - should_continue=False: 需要拒绝或返回特定响应
    """
    # 通过 deps 获取依赖
    db = deps.db
    logbook_adapter = deps.logbook_adapter

    # 检查用户是否存在
    user_exists = logbook_adapter.check_user_exists(actor_user_id)

    if user_exists:
        # 用户存在，正常继续
        return ActorValidationResult(should_continue=True)

    # 用户不存在，根据策略处理
    policy = config.unknown_actor_policy

    if policy == UnknownActorPolicy.REJECT:
        return _handle_reject_policy(
            actor_user_id=actor_user_id,
            target_space=target_space,
            payload_sha=payload_sha,
            evidence_refs=evidence_refs,
            correlation_id=correlation_id,
            db=db,
        )

    elif policy == UnknownActorPolicy.DEGRADE:
        return _handle_degrade_policy(
            actor_user_id=actor_user_id,
            config=config,
            target_space=target_space,
            payload_sha=payload_sha,
            evidence_refs=evidence_refs,
            correlation_id=correlation_id,
            db=db,
        )

    elif policy == UnknownActorPolicy.AUTO_CREATE:
        return _handle_auto_create_policy(
            actor_user_id=actor_user_id,
            target_space=target_space,
            payload_sha=payload_sha,
            evidence_refs=evidence_refs,
            correlation_id=correlation_id,
            db=db,
            logbook_adapter=logbook_adapter,
        )

    # 未知策略（不应发生）
    logger.error(f"未知 actor 策略: {policy}")
    return ActorValidationResult(should_continue=True)


def _handle_reject_policy(
    actor_user_id: str,
    target_space: str,
    payload_sha: str,
    evidence_refs: Optional[List[str]],
    correlation_id: str,
    db: Any,
) -> ActorValidationResult:
    """处理 reject 策略"""
    logger.warning(f"actor_user_id 不存在且策略为 reject: {actor_user_id}")

    # 构建 gateway_event
    # actor 校验发生在策略决策之前，policy/validation 字段使用 None 表示未进入该阶段
    gateway_event = build_gateway_audit_event(
        operation="memory_store",
        correlation_id=correlation_id,
        actor_user_id=actor_user_id,
        requested_space=target_space,
        final_space=None,
        action="reject",
        reason=ErrorCode.ACTOR_UNKNOWN_REJECT,
        payload_sha=payload_sha,
        evidence_refs=evidence_refs,
        extra={"actor_policy": "reject"},
        # v1.1: policy 子结构（actor 校验阶段未进入策略评估）
        policy_mode=None,
        policy_mode_reason="actor_validation_before_policy_evaluation",
        policy_version=None,
        policy_is_pointerized=False,
        policy_source=None,
        # v1.1: validation 子结构（actor 校验阶段未进行 evidence 校验）
        validate_refs_effective=None,
        validate_refs_reason="actor_validation_before_validation",
        evidence_validation=None,
    )
    evidence_refs_json = build_evidence_refs_json(evidence=None, gateway_event=gateway_event)

    # 写入审计
    db.insert_audit(
        actor_user_id=actor_user_id,
        target_space=target_space,
        action="reject",
        reason=ErrorCode.ACTOR_UNKNOWN_REJECT,
        payload_sha=payload_sha,
        evidence_refs_json=evidence_refs_json,
        correlation_id=correlation_id,
        status="failed",
    )

    return ActorValidationResult(
        should_continue=False,
        response_data={
            "ok": False,
            "action": "reject",
            "space_written": None,
            "memory_id": None,
            "outbox_id": None,
            "correlation_id": correlation_id,
            "evidence_refs": evidence_refs,
            "message": f"用户不存在: {actor_user_id}",
        },
    )


def _handle_degrade_policy(
    actor_user_id: str,
    config: Any,
    target_space: str,
    payload_sha: str,
    evidence_refs: Optional[List[str]],
    correlation_id: str,
    db: Any,
) -> ActorValidationResult:
    """处理 degrade 策略"""
    degrade_space = f"{config.private_space_prefix}unknown"
    logger.info(f"actor_user_id 不存在，降级到 {degrade_space}: {actor_user_id}")

    # 构建 gateway_event
    # actor 校验发生在策略决策之前，policy/validation 字段使用 None 表示未进入该阶段
    gateway_event = build_gateway_audit_event(
        operation="memory_store",
        correlation_id=correlation_id,
        actor_user_id=actor_user_id,
        requested_space=target_space,
        final_space=degrade_space,
        action="redirect",
        reason=ErrorCode.ACTOR_UNKNOWN_DEGRADE,
        payload_sha=payload_sha,
        evidence_refs=evidence_refs,
        extra={
            "actor_policy": "degrade",
            "original_space": target_space,
            "degrade_space": degrade_space,
        },
        # v1.1: policy 子结构（actor 校验阶段未进入策略评估）
        policy_mode=None,
        policy_mode_reason="actor_validation_before_policy_evaluation",
        policy_version=None,
        policy_is_pointerized=False,
        policy_source=None,
        # v1.1: validation 子结构（actor 校验阶段未进行 evidence 校验）
        validate_refs_effective=None,
        validate_refs_reason="actor_validation_before_validation",
        evidence_validation=None,
    )
    evidence_refs_json = build_evidence_refs_json(evidence=None, gateway_event=gateway_event)

    # 写入审计
    db.insert_audit(
        actor_user_id=actor_user_id,
        target_space=target_space,
        action="redirect",
        reason=ErrorCode.ACTOR_UNKNOWN_DEGRADE,
        payload_sha=payload_sha,
        evidence_refs_json=evidence_refs_json,
        correlation_id=correlation_id,
        status="success",
    )

    # 返回降级结果
    return ActorValidationResult(
        should_continue=True,
        degraded_space=degrade_space,
        response_data={
            "ok": True,
            "action": "redirect",
            "space_written": degrade_space,
            "memory_id": None,
            "outbox_id": None,
            "correlation_id": correlation_id,
            "evidence_refs": evidence_refs,
            "message": f"用户不存在，降级到 {degrade_space}",
        },
    )


def _handle_auto_create_policy(
    actor_user_id: str,
    target_space: str,
    payload_sha: str,
    evidence_refs: Optional[List[str]],
    correlation_id: str,
    db: Any,
    logbook_adapter: Any,
) -> ActorValidationResult:
    """处理 auto_create 策略"""
    logger.info(f"actor_user_id 不存在，自动创建: {actor_user_id}")

    try:
        logbook_adapter.ensure_user(user_id=actor_user_id, display_name=actor_user_id)

        # 构建 gateway_event
        # actor 校验发生在策略决策之前，policy/validation 字段使用 None 表示未进入该阶段
        gateway_event = build_gateway_audit_event(
            operation="memory_store",
            correlation_id=correlation_id,
            actor_user_id=actor_user_id,
            requested_space=target_space,
            final_space=target_space,
            action="allow",
            reason=ErrorCode.ACTOR_AUTOCREATED,
            payload_sha=payload_sha,
            evidence_refs=evidence_refs,
            extra={"actor_policy": "auto_create"},
            # v1.1: policy 子结构（actor 校验阶段未进入策略评估）
            policy_mode=None,
            policy_mode_reason="actor_validation_before_policy_evaluation",
            policy_version=None,
            policy_is_pointerized=False,
            policy_source=None,
            # v1.1: validation 子结构（actor 校验阶段未进行 evidence 校验）
            validate_refs_effective=None,
            validate_refs_reason="actor_validation_before_validation",
            evidence_validation=None,
        )
        evidence_refs_json = build_evidence_refs_json(evidence=None, gateway_event=gateway_event)

        # 写入审计
        db.insert_audit(
            actor_user_id=actor_user_id,
            target_space=target_space,
            action="allow",
            reason=ErrorCode.ACTOR_AUTOCREATED,
            payload_sha=payload_sha,
            evidence_refs_json=evidence_refs_json,
            correlation_id=correlation_id,
            status="success",
        )

        # 返回 None 表示继续正常流程
        return ActorValidationResult(should_continue=True)

    except Exception as e:
        logger.error(f"自动创建用户失败: {actor_user_id}, error={e}")

        # 构建 gateway_event
        # actor 校验发生在策略决策之前，policy/validation 字段使用 None 表示未进入该阶段
        gateway_event = build_gateway_audit_event(
            operation="memory_store",
            correlation_id=correlation_id,
            actor_user_id=actor_user_id,
            requested_space=target_space,
            final_space=None,
            action="reject",
            reason=ErrorCode.ACTOR_AUTOCREATE_FAILED,
            payload_sha=payload_sha,
            evidence_refs=evidence_refs,
            extra={
                "actor_policy": "auto_create",
                "error": str(e)[:500],
            },
            # v1.1: policy 子结构（actor 校验阶段未进入策略评估）
            policy_mode=None,
            policy_mode_reason="actor_validation_before_policy_evaluation",
            policy_version=None,
            policy_is_pointerized=False,
            policy_source=None,
            # v1.1: validation 子结构（actor 校验阶段未进行 evidence 校验）
            validate_refs_effective=None,
            validate_refs_reason="actor_validation_before_validation",
            evidence_validation=None,
        )
        evidence_refs_json = build_evidence_refs_json(evidence=None, gateway_event=gateway_event)

        # 写入审计
        db.insert_audit(
            actor_user_id=actor_user_id,
            target_space=target_space,
            action="reject",
            reason=ErrorCode.ACTOR_AUTOCREATE_FAILED,
            payload_sha=payload_sha,
            evidence_refs_json=evidence_refs_json,
            correlation_id=correlation_id,
            status="failed",
        )

        return ActorValidationResult(
            should_continue=False,
            response_data={
                "ok": False,
                "action": "error",
                "space_written": None,
                "memory_id": None,
                "outbox_id": None,
                "correlation_id": correlation_id,
                "evidence_refs": evidence_refs,
                "message": f"自动创建用户失败: {str(e)}",
            },
        )
