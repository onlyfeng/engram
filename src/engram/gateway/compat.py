"""
Gateway 兼容层模块 (Compatibility Layer)

本模块包含向后兼容的包装器函数，仅供旧代码使用。

重要说明:
=========
- 本模块中的函数将在 v1.0 版本中移除
- 新代码必须使用新 API，显式传入 deps 参数
- 旧代码应尽快迁移到新 API

移除版本: v1.0
替代方案: 见各函数的 docstring

使用示例（旧代码 - 不推荐）:
    from engram.gateway.compat import validate_actor_user_compat
    result = validate_actor_user_compat(actor_user_id, config, ...)

使用示例（新代码 - 推荐）:
    from engram.gateway.services.actor_validation import validate_actor_user
    from engram.gateway.container import get_container
    deps = get_container().deps
    result = validate_actor_user(actor_user_id, config, ..., deps=deps)
"""

import warnings
from typing import Any, List, Optional

from .container import get_container
from .handlers import MemoryStoreResponse
from .services.actor_validation import validate_actor_user as _validate_actor_user_v2

__all__ = [
    "validate_actor_user_compat",
]


def validate_actor_user_compat(
    actor_user_id: str,
    config: Any,
    target_space: str,
    payload_sha: str,
    evidence_refs: Optional[List[str]],
    correlation_id: str,
    deps: Any = None,
) -> Optional[MemoryStoreResponse]:
    """
    向后兼容包装器：调用新的 validate_actor_user 并转换返回值

    .. deprecated:: v0.9
        此函数将在 v1.0 中移除。
        请直接使用 `engram.gateway.services.actor_validation.validate_actor_user`，
        并显式传入 `deps` 参数。

    旧签名返回 Optional[MemoryStoreResponse]，新签名返回 ActorValidationResult。

    迁移指南:
    ---------
    旧代码::

        from engram.gateway.compat import validate_actor_user_compat
        response = validate_actor_user_compat(
            actor_user_id=user_id,
            config=config,
            target_space=space,
            payload_sha=sha,
            evidence_refs=refs,
            correlation_id=corr_id,
        )
        if response is not None:
            return response

    新代码::

        from engram.gateway.services.actor_validation import validate_actor_user
        from engram.gateway.container import get_container

        deps = get_container().deps
        result = validate_actor_user(
            actor_user_id=user_id,
            config=config,
            target_space=space,
            payload_sha=sha,
            evidence_refs=refs,
            correlation_id=corr_id,
            deps=deps,  # 必需参数
        )
        if not result.should_continue:
            return MemoryStoreResponse(**result.response_data)
        if result.degraded_space:
            # 处理降级场景
            ...

    Args:
        actor_user_id: 操作者用户标识
        config: GatewayConfig 配置对象
        target_space: 原始目标空间
        payload_sha: 内容哈希
        evidence_refs: 证据引用
        correlation_id: 关联 ID
        deps: 可选的 GatewayDeps（不传则从全局 container 获取）

    Returns:
        Optional[MemoryStoreResponse]:
        - None: 用户存在或已自动创建，继续正常流程
        - MemoryStoreResponse: 需要返回给调用方的响应（拒绝或降级）
    """
    # 发出弃用警告
    warnings.warn(
        "validate_actor_user_compat 已弃用，将在 v1.0 中移除。"
        "请使用 engram.gateway.services.actor_validation.validate_actor_user 并显式传入 deps 参数。",
        DeprecationWarning,
        stacklevel=2,
    )

    # 如果没有提供 deps，从全局 container 获取
    if deps is None:
        deps = get_container().deps

    result = _validate_actor_user_v2(
        actor_user_id=actor_user_id,
        config=config,
        target_space=target_space,
        payload_sha=payload_sha,
        evidence_refs=evidence_refs,
        correlation_id=correlation_id,
        deps=deps,
    )

    if not result.should_continue and result.response_data:
        return MemoryStoreResponse(**result.response_data)

    if result.degraded_space:
        # 降级场景：返回 redirect 响应
        return MemoryStoreResponse(
            ok=True,
            action="redirect",
            space_written=result.degraded_space,
            memory_id=None,
            outbox_id=None,
            correlation_id=correlation_id,
            evidence_refs=evidence_refs,
            message=f"用户不存在，降级到 {result.degraded_space}",
        )

    return None
