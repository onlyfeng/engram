# -*- coding: utf-8 -*-
"""
Gate Profile 测试辅助函数

提供统一的 profile 检查和能力验证，替代各测试文件中分散的实现。

使用方式:
    from tests.gateway.gate_helpers import (
        get_current_profile,
        is_full_profile,
        require_profile,
        enforce_capability_or_fail,
        ProfileType,
    )

    # 方式 1: 简单的 profile 检查（非 FULL 跳过，FULL 执行）
    require_profile(ProfileType.FULL)

    # 方式 2: 带能力检查的 profile 要求（FULL 缺能力 fail）
    require_profile(ProfileType.FULL, step="degradation")

    # 方式 3: 直接检查能力（更细粒度控制）
    enforce_capability_or_fail("degradation")
"""

import os
import subprocess
from typing import Optional, Tuple

import pytest

# ======================== Gate Contract 集成 ========================

try:
    from engram.unified_stack.gate_contract import (
        PROFILE_CONFIGS,
        ProfileType,
        StepName,
        detect_capabilities,
        get_profile_from_env,
    )

    GATE_CONTRACT_AVAILABLE = True
except ImportError:
    GATE_CONTRACT_AVAILABLE = False

    # 定义回退类型以避免 NameError
    from enum import Enum

    class ProfileType(str, Enum):  # type: ignore[no-redef]
        """回退 Profile 类型枚举"""

        HTTP_ONLY = "http_only"
        STANDARD = "standard"
        FULL = "full"

    class StepName(str, Enum):  # type: ignore[no-redef]
        """回退步骤名枚举"""

        DEGRADATION = "degradation"
        DB_INVARIANTS = "db_invariants"
        HEALTH_CHECKS = "health_checks"
        MEMORY_STORE = "memory_store"
        MEMORY_QUERY = "memory_query"
        JSONRPC = "jsonrpc"

    # 回退 PROFILE_CONFIGS：标记为回退模式
    PROFILE_CONFIGS = None

    # 回退常量：FULL profile 下必须 fail 的步骤
    _FALLBACK_MUST_FAIL_STEPS: frozenset = frozenset({"degradation", "db_invariants"})


# ======================== 核心 API ========================


def get_current_profile() -> str:
    """
    获取当前 profile（优先使用 gate_contract）

    优先级:
      1. engram.unified_stack.gate_contract.get_profile_from_env()
      2. 回退逻辑: GATE_PROFILE -> HTTP_ONLY_MODE -> SKIP_DEGRADATION_TEST

    Returns:
        当前 profile 名称: http_only, standard, full
    """
    if GATE_CONTRACT_AVAILABLE:
        return str(get_profile_from_env().value)

    # 回退逻辑
    explicit = os.environ.get("GATE_PROFILE", "").lower()
    if explicit in ("http_only", "httponly"):
        return "http_only"
    elif explicit == "full":
        return "full"
    elif explicit == "standard":
        return "standard"

    if os.environ.get("HTTP_ONLY_MODE") == "1":
        return "http_only"
    if os.environ.get("SKIP_DEGRADATION_TEST") == "1":
        return "standard"

    return "standard"


def is_full_profile() -> bool:
    """检查当前是否为 FULL profile"""
    return get_current_profile() == "full"


def is_http_only_profile() -> bool:
    """检查当前是否为 HTTP_ONLY profile"""
    return get_current_profile() == "http_only"


def check_capability_for_step(step_name: str) -> Tuple[bool, str]:
    """
    检查指定步骤所需的能力是否可用

    Args:
        step_name: 步骤名称（如 "degradation", "db_invariants"）

    Returns:
        (可用, 原因消息) 元组
    """
    if not GATE_CONTRACT_AVAILABLE:
        # 回退到简单检查
        if step_name == "degradation":
            import shutil

            if not shutil.which("docker"):
                return False, "Docker CLI 不可用"
            try:
                result = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
                if result.returncode != 0:
                    return False, "Docker daemon 不可用"
            except Exception as e:
                return False, f"Docker 检查失败: {e}"
            return True, "Docker 可用"
        elif step_name == "db_invariants":
            if not os.environ.get("POSTGRES_DSN"):
                return False, "POSTGRES_DSN 未设置"
            return True, "POSTGRES_DSN 已设置"
        return True, "能力检查跳过"

    # 使用 gate_contract 检查
    capabilities = detect_capabilities()

    if step_name == StepName.DEGRADATION.value:
        if not capabilities.is_available("can_stop_openmemory"):
            status = capabilities.capabilities.get("can_stop_openmemory")
            return False, status.message if status else "无法停止 OpenMemory 容器"
        return True, "降级测试能力可用"

    elif step_name == StepName.DB_INVARIANTS.value:
        if not capabilities.is_available("postgres_dsn_present"):
            return False, "POSTGRES_DSN 未设置"
        if not capabilities.is_available("db_access_available"):
            return False, "无 DB 访问能力（需要 psql 或 psycopg）"
        return True, "DB 不变量检查能力可用"

    return True, "能力检查通过"


def _get_must_fail_steps() -> frozenset:
    """
    获取 FULL profile 下必须 fail 的步骤集合（委托到 SSoT）

    Returns:
        步骤名称字符串的 frozenset
    """
    if GATE_CONTRACT_AVAILABLE and PROFILE_CONFIGS is not None:
        profile_config = PROFILE_CONFIGS.get(ProfileType.FULL)
        if profile_config:
            return frozenset(s.value for s in profile_config.must_fail_if_blocked)
    # 回退逻辑：与 SSoT 保持一致
    return _FALLBACK_MUST_FAIL_STEPS


def should_fail_if_blocked(step_name: str) -> bool:
    """
    检查指定步骤在 FULL profile 下缺少能力时是否应该 FAIL（而非 skip）

    Args:
        step_name: 步骤名称（使用 StepName 枚举值）

    Returns:
        True 表示应该 FAIL，False 表示可以 skip
    """
    if not is_full_profile():
        # 非 FULL 模式下，缺能力可以 skip
        return False

    # 委托到 SSoT（或回退常量）
    must_fail_steps = _get_must_fail_steps()
    return step_name in must_fail_steps


def enforce_capability_or_fail(step_name: str, reason_prefix: str = "") -> None:
    """
    检查能力，若在 FULL 模式下缺失则 pytest.fail，否则 pytest.skip

    用于测试 fixture 或 test 方法开头，统一处理 skip/fail 逻辑。

    Args:
        step_name: 步骤名称（如 "degradation", "db_invariants"）
        reason_prefix: 失败原因前缀

    Raises:
        pytest.fail: FULL 模式下能力缺失且步骤在 must_fail_if_blocked 列表中
        pytest.skip: 非 FULL 模式或能力缺失但步骤不在 must_fail_if_blocked 列表中
    """
    available, reason = check_capability_for_step(step_name)

    if available:
        return  # 能力可用，继续执行

    full_reason = f"{reason_prefix}{reason}" if reason_prefix else reason

    if should_fail_if_blocked(step_name):
        # FULL 模式下缺能力必须 FAIL
        pytest.fail(
            f"{FULL_FAIL_PREFIX} {full_reason}。"
            f"FULL 模式下 {step_name} 步骤不能跳过，必须修复环境后重试。"
        )
    else:
        # 非 FULL 模式可以 skip
        pytest.skip(full_reason)


def require_profile(
    required_profile: ProfileType,
    step: Optional[str] = None,
    reason: Optional[str] = None,
) -> None:
    """
    要求特定 profile，非满足时跳过或失败

    这是最常用的 API，用于测试方法开头声明 profile 要求。

    Args:
        required_profile: 要求的 profile（ProfileType.HTTP_ONLY/STANDARD/FULL）
        step: 可选的步骤名称，用于 FULL profile 的能力检查
        reason: 可选的自定义跳过原因

    行为:
        - 若当前 profile < 要求 profile: pytest.skip
        - 若 required_profile == FULL 且指定了 step:
          - 能力可用: 继续执行
          - 能力缺失且 step 在 must_fail_if_blocked: pytest.fail
          - 能力缺失但 step 不在 must_fail_if_blocked: pytest.skip

    使用示例:
        # 简单要求 FULL profile
        require_profile(ProfileType.FULL)

        # 带能力检查（FULL 缺能力 fail）
        require_profile(ProfileType.FULL, step="degradation")

        # 自定义跳过原因
        require_profile(ProfileType.FULL, reason="需要 Docker 环境")
    """
    # Profile 优先级
    PROFILE_PRIORITY = {
        "http_only": 1,
        "standard": 2,
        "full": 3,
    }

    current = get_current_profile()
    required_value = (
        required_profile.value if hasattr(required_profile, "value") else required_profile
    )

    current_priority = PROFILE_PRIORITY.get(current, 2)
    required_priority = PROFILE_PRIORITY.get(required_value, 2)

    # 如果当前 profile 不满足要求，skip
    if current_priority < required_priority:
        skip_reason = reason or f"需要 {required_value} profile，当前为 {current}"
        pytest.skip(f"[gate_profile:skip] {skip_reason}")

    # 如果是 FULL profile 且指定了 step，执行能力检查
    if required_value == "full" and step:
        enforce_capability_or_fail(step)


# ======================== Profile Skip/Fail 消息常量 ========================

# HTTP_ONLY_MODE 跳过原因前缀（grep 锚点：用于统一搜索）
HTTP_ONLY_SKIP_REASON_PREFIX = "HTTP_ONLY_MODE: "

# FULL profile 要求跳过原因模板
FULL_PROFILE_REQUIRED_REASON = "需要 FULL profile，当前为 {current}"

# FULL profile pytest.fail 前缀（锚点测试验证此字符串）
FULL_FAIL_PREFIX = "[FULL profile]"


# ======================== 导出步骤常量 ========================

# 步骤名称常量（便于使用）
STEP_DEGRADATION = "degradation"
STEP_DB_INVARIANTS = "db_invariants"
STEP_HEALTH_CHECKS = "health_checks"
STEP_MEMORY_STORE = "memory_store"
STEP_MEMORY_QUERY = "memory_query"
STEP_JSONRPC = "jsonrpc"


# ======================== 导出列表 ========================

__all__ = [
    # 核心 API
    "get_current_profile",
    "is_full_profile",
    "is_http_only_profile",
    "require_profile",
    "enforce_capability_or_fail",
    "check_capability_for_step",
    "should_fail_if_blocked",
    # 类型
    "ProfileType",
    "StepName",
    # 常量
    "GATE_CONTRACT_AVAILABLE",
    "STEP_DEGRADATION",
    "STEP_DB_INVARIANTS",
    "STEP_HEALTH_CHECKS",
    "STEP_MEMORY_STORE",
    "STEP_MEMORY_QUERY",
    "STEP_JSONRPC",
    # Profile Skip/Fail 消息常量
    "HTTP_ONLY_SKIP_REASON_PREFIX",
    "FULL_PROFILE_REQUIRED_REASON",
    "FULL_FAIL_PREFIX",
]
