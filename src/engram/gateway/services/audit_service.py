"""
audit_service - 审计写入服务模块

封装审计写入逻辑，实现 "审计不可丢" 语义。
"""

import logging
from typing import Any, Dict, Optional

from ..audit_event import AuditWriteError

logger = logging.getLogger("gateway.services.audit_service")


def write_audit_or_raise(
    db: Any,
    actor_user_id: Optional[str],
    target_space: str,
    action: str,
    reason: str,
    payload_sha: Optional[str],
    evidence_refs_json: Dict[str, Any],
    validate_refs: bool = False,
    correlation_id: str = "",
) -> int:
    """
    写入审计记录，失败时抛出 AuditWriteError

    实现 "审计不可丢" 语义：如果审计写入失败，阻断主操作继续执行。

    Args:
        db: LogbookDatabase 实例
        actor_user_id: 操作者用户 ID
        target_space: 目标空间
        action: 操作类型 (allow/redirect/reject)
        reason: 操作原因
        payload_sha: 内容 SHA256 哈希
        evidence_refs_json: 证据引用 JSON
        validate_refs: 是否验证 evidence_refs_json 结构
        correlation_id: 关联 ID（用于日志）

    Returns:
        audit_id: 创建的审计记录 ID

    Raises:
        AuditWriteError: 审计写入失败时抛出，阻断主操作
    """
    try:
        audit_id = db.insert_audit(
            actor_user_id=actor_user_id,
            target_space=target_space,
            action=action,
            reason=reason,
            payload_sha=payload_sha,
            evidence_refs_json=evidence_refs_json,
            validate_refs=validate_refs,
        )
        logger.debug(f"审计记录写入成功: audit_id={audit_id}, correlation_id={correlation_id}")
        return int(audit_id)
    except Exception as e:
        logger.error(f"审计写入失败，阻断操作: {e}, correlation_id={correlation_id}")
        raise AuditWriteError(
            message="审计写入失败，操作已阻断",
            original_error=e,
            audit_data={
                "actor_user_id": actor_user_id,
                "target_space": target_space,
                "action": action,
                "reason": reason,
                "correlation_id": correlation_id,
            },
        )
