"""
Gateway 共享服务模块

提供纯函数和共享服务实现，供 handlers 调用。

模块结构:
- hash_utils: 哈希计算（payload_sha）
- actor_validation: actor_user_id 校验与策略处理
- audit_service: 审计写入封装
"""

from .actor_validation import ActorValidationResult, validate_actor_user
from .audit_service import write_audit_or_raise
from .hash_utils import compute_payload_sha

__all__ = [
    "compute_payload_sha",
    "validate_actor_user",
    "ActorValidationResult",
    "write_audit_or_raise",
]
