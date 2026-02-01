"""
Correlation ID 生成与校验模块

提供 correlation_id 的单一来源实现，不依赖 pydantic/fastapi。

核心函数:
- generate_correlation_id(): 生成新的 correlation_id
- is_valid_correlation_id(): 校验格式是否合规
- normalize_correlation_id(): 归一化（不合规则重新生成）

格式规范:
- 格式: ^corr-[a-fA-F0-9]{16}$
- 长度: 21 字符 (5 + 16)
- 示例: corr-a1b2c3d4e5f67890

设计原则:
================
1. 纯 Python 实现，无第三方依赖
2. 不依赖 pydantic/fastapi，可在任意模块安全导入
3. 与 schemas/audit_event_v1.schema.json 中定义的格式一致

使用方式:
================
    from .correlation_id import (
        generate_correlation_id,
        is_valid_correlation_id,
        normalize_correlation_id,
        CORRELATION_ID_PATTERN,
    )

    # 生成新的 correlation_id
    corr_id = generate_correlation_id()  # -> "corr-a1b2c3d4e5f67890"

    # 校验格式
    is_valid_correlation_id("corr-a1b2c3d4e5f67890")  # -> True
    is_valid_correlation_id("corr-test")  # -> False

    # 归一化（不合规则重新生成）
    normalize_correlation_id("corr-a1b2c3d4e5f67890")  # -> 原值
    normalize_correlation_id("invalid")  # -> 生成新值

单一来源原则:
================
所有 correlation_id 的生成、校验、归一化都应通过本模块。
其他模块（mcp_rpc.py、di.py、dependencies.py、middleware.py）
应从本模块导入这些函数，确保行为一致。

详见:
- docs/contracts/mcp_jsonrpc_error_v1.md
- docs/gateway/07_capability_boundary.md
"""

from __future__ import annotations

import re
import uuid
from typing import Optional


def generate_correlation_id() -> str:
    """
    生成关联 ID

    格式: corr-{16位十六进制}
    与 schemas/audit_event_v1.schema.json 中定义的格式一致。

    此函数是 correlation_id 生成的单一来源，其他模块应从本模块导入使用。

    Returns:
        格式为 corr-{16位十六进制} 的关联 ID

    Example:
        >>> generate_correlation_id()
        'corr-a1b2c3d4e5f67890'
    """
    return f"corr-{uuid.uuid4().hex[:16]}"


# correlation_id 格式校验正则表达式（与 schemas/audit_event_v1.schema.json 对齐）
# 格式: corr-{16位十六进制}
CORRELATION_ID_PATTERN = re.compile(r"^corr-[a-fA-F0-9]{16}$")


def is_valid_correlation_id(correlation_id: Optional[str]) -> bool:
    """
    校验 correlation_id 是否符合 schema 规范

    格式要求: ^corr-[a-fA-F0-9]{16}$

    Args:
        correlation_id: 待校验的 correlation_id

    Returns:
        True 如果格式合规，False 否则

    Example:
        >>> is_valid_correlation_id("corr-a1b2c3d4e5f67890")
        True
        >>> is_valid_correlation_id("corr-test123")
        False
        >>> is_valid_correlation_id(None)
        False
    """
    if not correlation_id:
        return False
    return bool(CORRELATION_ID_PATTERN.match(correlation_id))


def normalize_correlation_id(correlation_id: Optional[str]) -> str:
    """
    归一化 correlation_id

    如果传入的 correlation_id 不合规，则重新生成一个合规的。
    这确保系统内部始终使用合规格式的 correlation_id。

    Args:
        correlation_id: 外部传入的 correlation_id（可能不合规）

    Returns:
        合规的 correlation_id

    Example:
        >>> normalize_correlation_id("corr-a1b2c3d4e5f67890")
        'corr-a1b2c3d4e5f67890'  # 合规，直接返回

        >>> normalize_correlation_id("corr-test123")
        'corr-abc123def456789a'  # 不合规，重新生成

        >>> normalize_correlation_id(None)
        'corr-abc123def456789a'  # 空值，生成新的
    """
    if is_valid_correlation_id(correlation_id):
        return correlation_id  # type: ignore[return-value]
    # 不合规或为空，生成新的
    return generate_correlation_id()


__all__ = [
    "generate_correlation_id",
    "is_valid_correlation_id",
    "normalize_correlation_id",
    "CORRELATION_ID_PATTERN",
]
