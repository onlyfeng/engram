"""
Gateway 共享服务模块

提供纯函数和共享服务实现，供 handlers 调用。

模块结构:
- ports: 服务端口 Protocol 定义（import-safe，无可选依赖）
- hash_utils: 哈希计算（payload_sha）
- actor_validation: actor_user_id 校验与策略处理
- audit_service: 审计写入封装

导入策略:
- ports 模块直接导出（纯 Protocol 定义，import-safe）
- 其他模块延迟导入（依赖可选模块）

使用示例:
    # 推荐：直接从子模块导入
    from engram.gateway.services.ports import WriteAuditPort
    from engram.gateway.services.hash_utils import compute_payload_sha

    # 兼容：从包导入（触发延迟加载）
    from engram.gateway.services import compute_payload_sha
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# ports 模块是 import-safe 的（纯 Protocol 定义），直接导出
from .ports import (
    ActorPolicyConfigPort,
    OpenMemoryPort,
    SearchResult,
    StoreResult,
    ToolCallContext,
    ToolCallResult,
    ToolDefinition,
    ToolExecutorPort,
    ToolRouterPort,
    UserDirectoryPort,
    UserDirectoryWithAuditPort,
    WriteAuditPort,
)

__all__ = [
    # ports 模块符号（import-safe）
    "WriteAuditPort",
    "UserDirectoryPort",
    "ActorPolicyConfigPort",
    "UserDirectoryWithAuditPort",
    "OpenMemoryPort",
    "StoreResult",
    "SearchResult",
    "ToolExecutorPort",
    "ToolRouterPort",
    "ToolDefinition",
    "ToolCallContext",
    "ToolCallResult",
    # 延迟导入符号
    "compute_payload_sha",
    "validate_actor_user",
    "ActorValidationResult",
    "write_audit_or_raise",
]

# TYPE_CHECKING 块用于静态类型提示
if TYPE_CHECKING:
    from .actor_validation import ActorValidationResult as ActorValidationResult
    from .actor_validation import validate_actor_user as validate_actor_user
    from .audit_service import write_audit_or_raise as write_audit_or_raise
    from .hash_utils import compute_payload_sha as compute_payload_sha

# 延迟导入映射
_LAZY_IMPORTS = {
    "compute_payload_sha": (".hash_utils", "compute_payload_sha"),
    "validate_actor_user": (".actor_validation", "validate_actor_user"),
    "ActorValidationResult": (".actor_validation", "ActorValidationResult"),
    "write_audit_or_raise": (".audit_service", "write_audit_or_raise"),
}


def __getattr__(name: str):
    """延迟导入非 ports 模块的符号"""
    if name in _LAZY_IMPORTS:
        import importlib

        module_path, attr_name = _LAZY_IMPORTS[name]
        module = importlib.import_module(module_path, __name__)
        obj = getattr(module, attr_name)
        globals()[name] = obj
        return obj
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
