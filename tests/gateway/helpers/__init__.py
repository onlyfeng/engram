# -*- coding: utf-8 -*-
"""
Gateway 测试 Helper 模块

提供用于 Gateway 测试的辅助工具，包括：
- sys.modules 模拟（用于 optional-deps/ImportError 测试）
- correlation_id 生成与验证（符合 schema 契约）

================================================================================
                       correlation_id 规范
================================================================================

根据 schemas/audit_event_v1.schema.json 定义：
- correlation_id 格式必须是: ^corr-[a-fA-F0-9]{16}$
- 即: "corr-" 前缀 + 16 位十六进制字符

示例合规值:
- corr-a1b2c3d4e5f67890
- corr-ABCDEF1234567890
- corr-0000000000000000

不合规示例:
- corr-test123 (包含非十六进制字符 t, e, s)
- corr-abc (长度不足)
- test-a1b2c3d4e5f67890 (前缀不对)
"""

import re
import uuid
from typing import Optional

from tests.gateway.helpers.sys_modules_patch import FailingImport, patch_sys_modules

# correlation_id 格式正则表达式（与 schema 定义对齐）
CORRELATION_ID_PATTERN = re.compile(r"^corr-[a-fA-F0-9]{16}$")

# 测试用的固定 correlation_id（符合 schema 格式）
# 用于需要稳定值的测试断言
TEST_CORRELATION_ID = "corr-0000000000000000"
TEST_CORRELATION_ID_ALT = "corr-1111111111111111"
TEST_CORRELATION_ID_2 = "corr-2222222222222222"
TEST_CORRELATION_ID_3 = "corr-3333333333333333"


def generate_compliant_correlation_id(suffix: Optional[str] = None) -> str:
    """
    生成符合 schema 规范的 correlation_id

    格式: corr-{16位十六进制}

    Args:
        suffix: 可选的后缀标识（仅用于调试，会被转换为十六进制或忽略）

    Returns:
        符合规范的 correlation_id 字符串

    示例:
        >>> generate_compliant_correlation_id()
        'corr-a1b2c3d4e5f67890'  # 随机生成

        >>> generate_compliant_correlation_id("test")
        'corr-a1b2c3d4e5f67890'  # 随机生成，suffix 参数仅用于调试标识
    """
    # 使用 uuid4 生成随机十六进制字符串
    return f"corr-{uuid.uuid4().hex[:16]}"


def make_test_correlation_id(index: int = 0) -> str:
    """
    生成测试用的 correlation_id（基于索引，便于追踪）

    Args:
        index: 测试索引号 (0-9999)

    Returns:
        符合规范的 correlation_id

    示例:
        >>> make_test_correlation_id(0)
        'corr-0000000000000000'
        >>> make_test_correlation_id(42)
        'corr-000000000000002a'
        >>> make_test_correlation_id(255)
        'corr-00000000000000ff'
    """
    # 将索引转换为 16 位十六进制（左侧补零）
    hex_value = f"{index:016x}"
    return f"corr-{hex_value}"


def is_valid_correlation_id(correlation_id: str) -> bool:
    """
    验证 correlation_id 是否符合 schema 规范

    Args:
        correlation_id: 待验证的 correlation_id

    Returns:
        True 如果格式合规
    """
    return bool(CORRELATION_ID_PATTERN.match(correlation_id))


# ==================== 测试用固定值（按用途分组）====================

# 用于 dedup 相关测试
CORR_ID_DEDUP = "corr-ded0000000000001"
CORR_ID_DEDUP_HIT = "corr-ded0000000000002"

# 用于策略相关测试
CORR_ID_POLICY_ALLOW = "corr-a110000000000001"
CORR_ID_POLICY_REJECT = "corr-0ece000000000001"
CORR_ID_POLICY_REDIRECT = "corr-0ed10ec000000001"

# 用于 OpenMemory 失败测试
CORR_ID_OM_FAIL = "corr-0f00000000000001"
CORR_ID_OM_TIMEOUT = "corr-0f00000000000002"

# 用于审计测试
CORR_ID_AUDIT_TEST = "corr-a0d1000000000001"
CORR_ID_AUDIT_FAIL = "corr-a0d1fa1100000001"

# 用于 outbox 测试
CORR_ID_OUTBOX = "corr-0b00000000000001"
CORR_ID_OUTBOX_WORKER = "corr-0b00000000000002"

# 用于 reconcile 测试
CORR_ID_RECONCILE = "corr-0ec0000000000001"

# 用于证据校验测试
CORR_ID_EVIDENCE = "corr-e01dece000000001"
CORR_ID_EVIDENCE_VALID = "corr-e01dece000000002"

# 用于 strict 模式测试
CORR_ID_STRICT = "corr-5010c00000000001"
CORR_ID_STRICT_FAIL = "corr-5010cfa110000001"


__all__ = [
    # sys.modules 模拟工具
    "FailingImport",
    "patch_sys_modules",
    # 生成函数
    "generate_compliant_correlation_id",
    "make_test_correlation_id",
    "is_valid_correlation_id",
    # 正则
    "CORRELATION_ID_PATTERN",
    # 固定测试值
    "TEST_CORRELATION_ID",
    "TEST_CORRELATION_ID_ALT",
    "TEST_CORRELATION_ID_2",
    "TEST_CORRELATION_ID_3",
    # 按用途分组的固定值
    "CORR_ID_DEDUP",
    "CORR_ID_DEDUP_HIT",
    "CORR_ID_POLICY_ALLOW",
    "CORR_ID_POLICY_REJECT",
    "CORR_ID_POLICY_REDIRECT",
    "CORR_ID_OM_FAIL",
    "CORR_ID_OM_TIMEOUT",
    "CORR_ID_AUDIT_TEST",
    "CORR_ID_AUDIT_FAIL",
    "CORR_ID_OUTBOX",
    "CORR_ID_OUTBOX_WORKER",
    "CORR_ID_RECONCILE",
    "CORR_ID_EVIDENCE",
    "CORR_ID_EVIDENCE_VALID",
    "CORR_ID_STRICT",
    "CORR_ID_STRICT_FAIL",
]
