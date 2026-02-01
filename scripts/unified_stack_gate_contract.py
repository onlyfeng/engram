#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
unified_stack_gate_contract.py - Unified Stack 门禁配置/能力模型

本模块定义了 Unified Stack 验证的门禁规则，可被 bash/python 同步引用。

Profile 定义:
  - http_only:  仅 HTTP 接口验证（无 MCP JSON-RPC）
  - standard:   标准模式（HTTP + JSON-RPC，无降级测试）
  - full:       完整模式（所有步骤，包括降级测试）

Capability 定义:
  - docker_available:           Docker CLI 可用
  - docker_daemon_ok:           Docker daemon 运行中
  - compose_configured:         docker-compose.yml 存在且有效
  - can_stop_openmemory:        可以停止 openmemory 容器（用于 degradation 测试）
  - psql_available:             psql CLI 可用
  - psycopg_available:          psycopg2/psycopg 库可用
  - postgres_dsn_present:       POSTGRES_DSN 环境变量已设置
  - openmemory_endpoint_present: OPENMEMORY_ENDPOINT 环境变量已设置

使用方法:
  # Python 侧
  from unified_stack_gate_contract import GateProfile, detect_capabilities, validate_profile

  # Bash 侧（输出 reason code）
  python unified_stack_gate_contract.py --detect-capabilities
  python unified_stack_gate_contract.py --validate-profile full
  python unified_stack_gate_contract.py --dump-rules
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

# ============================================================================
# Reason Codes（Bash/Python 统一使用）
# ============================================================================

class ReasonCode(str, Enum):
    """
    门禁失败原因码（Bash 侧可通过名称对齐）

    命名规则:
      - 大写 + 下划线
      - 前缀表示类别: CAP_ = capability, PROF_ = profile, STEP_ = step
    """
    # Capability 检测
    CAP_DOCKER_NOT_FOUND = "CAP_DOCKER_NOT_FOUND"
    CAP_DOCKER_DAEMON_DOWN = "CAP_DOCKER_DAEMON_DOWN"
    CAP_COMPOSE_NOT_CONFIGURED = "CAP_COMPOSE_NOT_CONFIGURED"
    CAP_CANNOT_STOP_OPENMEMORY = "CAP_CANNOT_STOP_OPENMEMORY"
    CAP_PSQL_NOT_FOUND = "CAP_PSQL_NOT_FOUND"
    CAP_PSYCOPG_NOT_FOUND = "CAP_PSYCOPG_NOT_FOUND"
    CAP_NO_DB_ACCESS = "CAP_NO_DB_ACCESS"
    CAP_POSTGRES_DSN_MISSING = "CAP_POSTGRES_DSN_MISSING"
    CAP_OPENMEMORY_ENDPOINT_MISSING = "CAP_OPENMEMORY_ENDPOINT_MISSING"

    # Profile 校验
    PROF_INVALID = "PROF_INVALID"
    PROF_MISSING_CAPABILITY = "PROF_MISSING_CAPABILITY"
    PROF_DEGRADATION_BLOCKED = "PROF_DEGRADATION_BLOCKED"
    PROF_DB_INVARIANTS_BLOCKED = "PROF_DB_INVARIANTS_BLOCKED"

    # Step 状态
    STEP_MISSING = "STEP_MISSING"
    STEP_FAILED = "STEP_FAILED"
    STEP_SKIPPED_UNEXPECTEDLY = "STEP_SKIPPED_UNEXPECTEDLY"

    # 通用
    OK = "OK"


# ============================================================================
# Step 定义
# ============================================================================

class StepName(str, Enum):
    """验证步骤名称"""
    HEALTH_CHECKS = "health_checks"
    DB_INVARIANTS = "db_invariants"
    MEMORY_STORE = "memory_store"
    MEMORY_QUERY = "memory_query"
    JSONRPC = "jsonrpc"
    DEGRADATION = "degradation"
    AUDIT_GC_SMOKE = "audit_gc_smoke"
    TOOL_CHECK = "tool_check"


# ============================================================================
# Profile 定义
# ============================================================================

class ProfileType(str, Enum):
    """门禁 profile 类型"""
    HTTP_ONLY = "http_only"
    STANDARD = "standard"
    FULL = "full"


@dataclass
class ProfileConfig:
    """Profile 配置"""
    name: ProfileType
    description: str
    required_steps: list[StepName]
    optional_steps: list[StepName]
    required_capabilities: list[str]
    # 硬规则：这些步骤如果缺少前置条件必须 fail（不能静默跳过）
    must_fail_if_blocked: list[StepName] = field(default_factory=list)


# Profile 配置表
PROFILE_CONFIGS: dict[ProfileType, ProfileConfig] = {
    ProfileType.HTTP_ONLY: ProfileConfig(
        name=ProfileType.HTTP_ONLY,
        description="仅 HTTP 接口验证（无 MCP JSON-RPC）",
        required_steps=[
            StepName.HEALTH_CHECKS,
            StepName.MEMORY_STORE,
            StepName.MEMORY_QUERY,
        ],
        optional_steps=[
            StepName.DB_INVARIANTS,
        ],
        required_capabilities=[
            "openmemory_endpoint_present",
        ],
        must_fail_if_blocked=[],
    ),
    ProfileType.STANDARD: ProfileConfig(
        name=ProfileType.STANDARD,
        description="标准模式（HTTP + JSON-RPC，无降级测试）",
        required_steps=[
            StepName.HEALTH_CHECKS,
            StepName.MEMORY_STORE,
            StepName.MEMORY_QUERY,
            StepName.JSONRPC,
        ],
        optional_steps=[
            StepName.DB_INVARIANTS,
        ],
        required_capabilities=[
            "openmemory_endpoint_present",
        ],
        must_fail_if_blocked=[],
    ),
    ProfileType.FULL: ProfileConfig(
        name=ProfileType.FULL,
        description="完整模式（所有步骤，包括降级测试和 DB 不变量检查）",
        required_steps=[
            StepName.HEALTH_CHECKS,
            StepName.DB_INVARIANTS,
            StepName.MEMORY_STORE,
            StepName.MEMORY_QUERY,
            StepName.JSONRPC,
            StepName.DEGRADATION,
        ],
        optional_steps=[],
        required_capabilities=[
            "openmemory_endpoint_present",
            "docker_available",
            "docker_daemon_ok",
            "can_stop_openmemory",
            "db_access_available",  # psql 或 psycopg 之一
            "postgres_dsn_present",
        ],
        # FULL 模式硬规则：这些步骤缺前置条件必须 fail
        must_fail_if_blocked=[
            StepName.DEGRADATION,
            StepName.DB_INVARIANTS,
        ],
    ),
}


# ============================================================================
# Capability 检测
# ============================================================================

@dataclass
class CapabilityStatus:
    """单个 capability 的检测结果"""
    name: str
    available: bool
    reason_code: ReasonCode
    message: str


@dataclass
class CapabilityReport:
    """Capability 检测报告"""
    capabilities: dict[str, CapabilityStatus]
    all_capabilities: list[str]

    def is_available(self, name: str) -> bool:
        """检查某个 capability 是否可用"""
        status = self.capabilities.get(name)
        return status.available if status else False

    def get_missing(self, required: list[str]) -> list[CapabilityStatus]:
        """获取缺失的 capabilities"""
        missing = []
        for name in required:
            status = self.capabilities.get(name)
            if status and not status.available:
                missing.append(status)
        return missing

    def to_dict(self) -> dict[str, Any]:
        """转换为字典（用于 JSON 输出）"""
        return {
            "capabilities": {
                name: {
                    "available": status.available,
                    "reason_code": status.reason_code.value,
                    "message": status.message,
                }
                for name, status in self.capabilities.items()
            },
            "all_capabilities": self.all_capabilities,
        }


def _check_command_exists(cmd: str) -> bool:
    """检查命令是否存在"""
    return shutil.which(cmd) is not None


def _check_docker_daemon() -> tuple[bool, str]:
    """检查 Docker daemon 是否运行"""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
        )
        if result.returncode == 0:
            return True, "Docker daemon is running"
        return False, "Docker daemon not responding"
    except FileNotFoundError:
        return False, "Docker command not found"
    except subprocess.TimeoutExpired:
        return False, "Docker daemon timeout"
    except Exception as e:
        return False, f"Docker check failed: {e}"


def _check_compose_configured() -> tuple[bool, str]:
    """检查 docker-compose.yml 是否存在"""
    # 常见位置
    compose_paths = [
        Path("docker-compose.yml"),
        Path("docker-compose.yaml"),
        Path("compose.yml"),
        Path("compose.yaml"),
    ]

    for path in compose_paths:
        if path.exists():
            return True, f"Found {path}"

    return False, "No docker-compose file found"


def _check_can_stop_openmemory() -> tuple[bool, str]:
    """检查是否可以停止 openmemory 容器（基于 docker + compose 可用性）"""
    # 先检查 docker
    if not _check_command_exists("docker"):
        return False, "Docker not available"

    # 检查 daemon
    daemon_ok, msg = _check_docker_daemon()
    if not daemon_ok:
        return False, msg

    # 检查 compose
    compose_ok, msg = _check_compose_configured()
    if not compose_ok:
        return False, msg

    # 检查是否有 openmemory 服务定义
    # （简化检查：假设有 compose 文件就可以停止）
    return True, "Can stop openmemory container"


def _check_psycopg() -> tuple[bool, str]:
    """检查 psycopg2 或 psycopg 是否可用"""
    try:
        import psycopg2
        return True, "psycopg2 available"
    except ImportError:
        pass

    try:
        import psycopg
        return True, "psycopg3 available"
    except ImportError:
        pass

    return False, "Neither psycopg2 nor psycopg is installed"


def _check_db_access() -> tuple[bool, str]:
    """检查是否有数据库访问能力（psql 或 psycopg）"""
    has_psql = _check_command_exists("psql")
    psycopg_ok, _ = _check_psycopg()

    if has_psql and psycopg_ok:
        return True, "Both psql and psycopg available"
    elif has_psql:
        return True, "psql available"
    elif psycopg_ok:
        return True, "psycopg available"
    else:
        return False, "Neither psql nor psycopg available"


def detect_capabilities() -> CapabilityReport:
    """
    检测当前环境的所有 capabilities

    Returns:
        CapabilityReport 包含所有 capability 的检测结果
    """
    capabilities: dict[str, CapabilityStatus] = {}

    # docker_available
    docker_available = _check_command_exists("docker")
    capabilities["docker_available"] = CapabilityStatus(
        name="docker_available",
        available=docker_available,
        reason_code=ReasonCode.OK if docker_available else ReasonCode.CAP_DOCKER_NOT_FOUND,
        message="Docker CLI found" if docker_available else "Docker CLI not found",
    )

    # docker_daemon_ok
    daemon_ok, daemon_msg = _check_docker_daemon()
    capabilities["docker_daemon_ok"] = CapabilityStatus(
        name="docker_daemon_ok",
        available=daemon_ok,
        reason_code=ReasonCode.OK if daemon_ok else ReasonCode.CAP_DOCKER_DAEMON_DOWN,
        message=daemon_msg,
    )

    # compose_configured
    compose_ok, compose_msg = _check_compose_configured()
    capabilities["compose_configured"] = CapabilityStatus(
        name="compose_configured",
        available=compose_ok,
        reason_code=ReasonCode.OK if compose_ok else ReasonCode.CAP_COMPOSE_NOT_CONFIGURED,
        message=compose_msg,
    )

    # can_stop_openmemory
    stop_ok, stop_msg = _check_can_stop_openmemory()
    capabilities["can_stop_openmemory"] = CapabilityStatus(
        name="can_stop_openmemory",
        available=stop_ok,
        reason_code=ReasonCode.OK if stop_ok else ReasonCode.CAP_CANNOT_STOP_OPENMEMORY,
        message=stop_msg,
    )

    # psql_available
    psql_available = _check_command_exists("psql")
    capabilities["psql_available"] = CapabilityStatus(
        name="psql_available",
        available=psql_available,
        reason_code=ReasonCode.OK if psql_available else ReasonCode.CAP_PSQL_NOT_FOUND,
        message="psql CLI found" if psql_available else "psql CLI not found",
    )

    # psycopg_available
    psycopg_ok, psycopg_msg = _check_psycopg()
    capabilities["psycopg_available"] = CapabilityStatus(
        name="psycopg_available",
        available=psycopg_ok,
        reason_code=ReasonCode.OK if psycopg_ok else ReasonCode.CAP_PSYCOPG_NOT_FOUND,
        message=psycopg_msg,
    )

    # db_access_available（psql 或 psycopg 之一）
    db_ok, db_msg = _check_db_access()
    capabilities["db_access_available"] = CapabilityStatus(
        name="db_access_available",
        available=db_ok,
        reason_code=ReasonCode.OK if db_ok else ReasonCode.CAP_NO_DB_ACCESS,
        message=db_msg,
    )

    # postgres_dsn_present
    dsn_present = bool(os.environ.get("POSTGRES_DSN"))
    capabilities["postgres_dsn_present"] = CapabilityStatus(
        name="postgres_dsn_present",
        available=dsn_present,
        reason_code=ReasonCode.OK if dsn_present else ReasonCode.CAP_POSTGRES_DSN_MISSING,
        message="POSTGRES_DSN is set" if dsn_present else "POSTGRES_DSN not set",
    )

    # openmemory_endpoint_present
    endpoint_present = bool(os.environ.get("OPENMEMORY_ENDPOINT"))
    capabilities["openmemory_endpoint_present"] = CapabilityStatus(
        name="openmemory_endpoint_present",
        available=endpoint_present,
        reason_code=ReasonCode.OK if endpoint_present else ReasonCode.CAP_OPENMEMORY_ENDPOINT_MISSING,
        message="OPENMEMORY_ENDPOINT is set" if endpoint_present else "OPENMEMORY_ENDPOINT not set",
    )

    return CapabilityReport(
        capabilities=capabilities,
        all_capabilities=list(capabilities.keys()),
    )


# ============================================================================
# Profile 校验
# ============================================================================

@dataclass
class ProfileValidationResult:
    """Profile 校验结果"""
    profile: ProfileType
    valid: bool
    reason_code: ReasonCode
    message: str
    missing_capabilities: list[CapabilityStatus]
    blocked_steps: list[tuple[StepName, ReasonCode, str]]

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "profile": self.profile.value,
            "valid": self.valid,
            "reason_code": self.reason_code.value,
            "message": self.message,
            "missing_capabilities": [
                {
                    "name": cap.name,
                    "reason_code": cap.reason_code.value,
                    "message": cap.message,
                }
                for cap in self.missing_capabilities
            ],
            "blocked_steps": [
                {
                    "step": step.value,
                    "reason_code": code.value,
                    "message": msg,
                }
                for step, code, msg in self.blocked_steps
            ],
        }


def validate_profile(
    profile: ProfileType,
    capabilities: Optional[CapabilityReport] = None,
    skip_degradation: bool = False,
    http_only_mode: bool = False,
) -> ProfileValidationResult:
    """
    校验指定 profile 是否可以执行

    Args:
        profile: 要校验的 profile
        capabilities: 已检测的 capabilities（如果为 None 则自动检测）
        skip_degradation: 是否跳过 degradation 测试（SKIP_DEGRADATION_TEST=1）
        http_only_mode: 是否为 HTTP_ONLY_MODE（HTTP_ONLY_MODE=1）

    Returns:
        ProfileValidationResult 包含校验结果
    """
    if capabilities is None:
        capabilities = detect_capabilities()

    config = PROFILE_CONFIGS.get(profile)
    if config is None:
        return ProfileValidationResult(
            profile=profile,
            valid=False,
            reason_code=ReasonCode.PROF_INVALID,
            message=f"Unknown profile: {profile}",
            missing_capabilities=[],
            blocked_steps=[],
        )

    # 检查必需的 capabilities
    missing = capabilities.get_missing(config.required_capabilities)

    # 检查被阻塞的步骤
    blocked_steps: list[tuple[StepName, ReasonCode, str]] = []

    for step in config.must_fail_if_blocked:
        if step == StepName.DEGRADATION:
            # degradation 的特殊处理
            if skip_degradation:
                # SKIP_DEGRADATION_TEST=1 明确跳过，不视为阻塞
                continue
            if http_only_mode:
                # HTTP_ONLY_MODE=1 明确跳过，不视为阻塞
                continue
            # 检查是否有前置条件
            if not capabilities.is_available("can_stop_openmemory"):
                blocked_steps.append((
                    StepName.DEGRADATION,
                    ReasonCode.PROF_DEGRADATION_BLOCKED,
                    "Cannot run degradation test: can_stop_openmemory not available",
                ))

        elif step == StepName.DB_INVARIANTS:
            # db_invariants 的特殊处理
            if not capabilities.is_available("postgres_dsn_present"):
                blocked_steps.append((
                    StepName.DB_INVARIANTS,
                    ReasonCode.PROF_DB_INVARIANTS_BLOCKED,
                    "Cannot run db_invariants: POSTGRES_DSN not set",
                ))
            elif not capabilities.is_available("db_access_available"):
                blocked_steps.append((
                    StepName.DB_INVARIANTS,
                    ReasonCode.PROF_DB_INVARIANTS_BLOCKED,
                    "Cannot run db_invariants: no DB access (psql or psycopg)",
                ))

    # 判断是否有效
    if blocked_steps:
        first_blocked = blocked_steps[0]
        return ProfileValidationResult(
            profile=profile,
            valid=False,
            reason_code=first_blocked[1],
            message=first_blocked[2],
            missing_capabilities=missing,
            blocked_steps=blocked_steps,
        )

    if missing:
        first_missing = missing[0]
        return ProfileValidationResult(
            profile=profile,
            valid=False,
            reason_code=ReasonCode.PROF_MISSING_CAPABILITY,
            message=f"Missing capability: {first_missing.name} - {first_missing.message}",
            missing_capabilities=missing,
            blocked_steps=blocked_steps,
        )

    return ProfileValidationResult(
        profile=profile,
        valid=True,
        reason_code=ReasonCode.OK,
        message=f"Profile {profile.value} is valid",
        missing_capabilities=[],
        blocked_steps=[],
    )


# ============================================================================
# 规则表导出（供 Bash 侧使用）
# ============================================================================

def dump_rules_table() -> dict[str, Any]:
    """
    导出完整的规则表（JSON 格式）

    可被 Bash 侧解析使用
    """
    rules = {
        "version": "1.0.0",
        "reason_codes": {code.name: code.value for code in ReasonCode},
        "step_names": {step.name: step.value for step in StepName},
        "profile_types": {p.name: p.value for p in ProfileType},
        "profiles": {},
    }

    for profile_type, config in PROFILE_CONFIGS.items():
        rules["profiles"][profile_type.value] = {
            "name": config.name.value,
            "description": config.description,
            "required_steps": [s.value for s in config.required_steps],
            "optional_steps": [s.value for s in config.optional_steps],
            "required_capabilities": config.required_capabilities,
            "must_fail_if_blocked": [s.value for s in config.must_fail_if_blocked],
        }

    return rules


def get_required_steps_for_profile(profile: ProfileType) -> list[str]:
    """获取指定 profile 的必需步骤（供外部模块使用）"""
    config = PROFILE_CONFIGS.get(profile)
    if config is None:
        return []
    return [s.value for s in config.required_steps]


def get_profile_from_env() -> ProfileType:
    """
    从环境变量推断当前 profile

    优先级:
      1. GATE_PROFILE 环境变量（显式指定）
      2. HTTP_ONLY_MODE=1 -> http_only
      3. SKIP_DEGRADATION_TEST=1 -> standard
      4. 默认 -> standard
    """
    explicit = os.environ.get("GATE_PROFILE", "").lower()
    if explicit in ("http_only", "httponly"):
        return ProfileType.HTTP_ONLY
    elif explicit == "standard":
        return ProfileType.STANDARD
    elif explicit == "full":
        return ProfileType.FULL

    # 根据其他环境变量推断
    if os.environ.get("HTTP_ONLY_MODE") == "1":
        return ProfileType.HTTP_ONLY
    if os.environ.get("SKIP_DEGRADATION_TEST") == "1":
        return ProfileType.STANDARD

    return ProfileType.STANDARD


# ============================================================================
# CLI 入口
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Unified Stack 门禁配置/能力模型",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # detect-capabilities
    detect_parser = subparsers.add_parser(
        "detect-capabilities",
        help="检测当前环境的 capabilities",
    )
    detect_parser.add_argument(
        "--json",
        action="store_true",
        help="JSON 格式输出",
    )

    # validate-profile
    validate_parser = subparsers.add_parser(
        "validate-profile",
        help="校验指定 profile 是否可执行",
    )
    validate_parser.add_argument(
        "profile",
        choices=["http_only", "standard", "full"],
        help="要校验的 profile",
    )
    validate_parser.add_argument(
        "--json",
        action="store_true",
        help="JSON 格式输出",
    )

    # dump-rules
    dump_parser = subparsers.add_parser(
        "dump-rules",
        help="导出完整规则表",
    )

    # get-required-steps
    steps_parser = subparsers.add_parser(
        "get-required-steps",
        help="获取指定 profile 的必需步骤",
    )
    steps_parser.add_argument(
        "profile",
        choices=["http_only", "standard", "full"],
        help="profile 名称",
    )

    # get-profile
    profile_parser = subparsers.add_parser(
        "get-profile",
        help="从环境变量推断当前 profile",
    )

    args = parser.parse_args()

    if args.command == "detect-capabilities":
        report = detect_capabilities()
        if args.json:
            print(json.dumps(report.to_dict(), indent=2))
        else:
            print("Capability Detection Report")
            print("=" * 50)
            for name, status in report.capabilities.items():
                mark = "✓" if status.available else "✗"
                print(f"  [{mark}] {name}: {status.message}")
                if not status.available:
                    print(f"      Reason: {status.reason_code.value}")

    elif args.command == "validate-profile":
        profile = ProfileType(args.profile)
        skip_degradation = os.environ.get("SKIP_DEGRADATION_TEST") == "1"
        http_only_mode = os.environ.get("HTTP_ONLY_MODE") == "1"

        result = validate_profile(
            profile=profile,
            skip_degradation=skip_degradation,
            http_only_mode=http_only_mode,
        )

        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            mark = "✓" if result.valid else "✗"
            print(f"[{mark}] Profile: {profile.value}")
            print(f"    Valid: {result.valid}")
            print(f"    Reason: {result.reason_code.value}")
            print(f"    Message: {result.message}")

            if result.missing_capabilities:
                print("    Missing Capabilities:")
                for cap in result.missing_capabilities:
                    print(f"      - {cap.name}: {cap.message}")

            if result.blocked_steps:
                print("    Blocked Steps:")
                for step, code, msg in result.blocked_steps:
                    print(f"      - {step.value}: {msg}")

        sys.exit(0 if result.valid else 1)

    elif args.command == "dump-rules":
        rules = dump_rules_table()
        print(json.dumps(rules, indent=2))

    elif args.command == "get-required-steps":
        profile = ProfileType(args.profile)
        steps = get_required_steps_for_profile(profile)
        print(" ".join(steps))

    elif args.command == "get-profile":
        profile = get_profile_from_env()
        print(profile.value)

    else:
        parser.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()
