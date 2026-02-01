# -*- coding: utf-8 -*-
"""
Unified Stack 模块

包含 Unified Stack 验证的门禁配置、能力检测与 profile 校验逻辑。
"""

from engram.unified_stack.gate_contract import (
    PROFILE_CONFIGS,
    CapabilityReport,
    CapabilityStatus,
    ProfileConfig,
    ProfileType,
    ProfileValidationResult,
    ReasonCode,
    StepName,
    detect_capabilities,
    dump_rules_table,
    get_profile_from_env,
    get_required_steps_for_profile,
    validate_profile,
)

__all__ = [
    # Enums
    "ReasonCode",
    "StepName",
    "ProfileType",
    # Dataclasses
    "ProfileConfig",
    "CapabilityStatus",
    "CapabilityReport",
    "ProfileValidationResult",
    # Constants
    "PROFILE_CONFIGS",
    # Functions
    "detect_capabilities",
    "validate_profile",
    "dump_rules_table",
    "get_required_steps_for_profile",
    "get_profile_from_env",
]
