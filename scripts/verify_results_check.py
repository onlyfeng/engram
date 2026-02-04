#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_results_check.py - Unified Stack 验证结果校验脚本

功能:
  1. 按 JSON Schema 校验验证结果 JSON
  2. 追加业务门禁检查:
     - http_only 模式: 必须包含 health_checks, memory_store, memory_query
     - standard 模式: 必须包含 health_checks, memory_store, memory_query, jsonrpc
     - full 模式: 额外必须包含 db_invariants, degradation
  3. 输出校验结果，失败时返回非零退出码

使用方法:
  python verify_results_check.py <json_file>
  python verify_results_check.py <json_file> --full         # FULL 模式校验
  python verify_results_check.py <json_file> --profile standard  # 指定 profile
  python verify_results_check.py <json_file> --schema-only  # 仅 schema 校验
  python verify_results_check.py <json_file> --json         # JSON 格式输出

退出码:
  0 - 校验通过
  1 - 校验失败
  2 - 参数错误 / 文件不存在

环境变量:
  VERIFY_FULL=1           启用 FULL 模式校验（等同于 --full）
  GATE_PROFILE            显式指定 profile (http_only/standard/full)
  HTTP_ONLY_MODE=1        HTTP_ONLY 模式（推断为 http_only profile）
  SKIP_DEGRADATION_TEST=1 跳过 degradation 测试
  VERIFY_RESULTS_SCHEMA   自定义 schema 文件路径
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

# 尝试导入 jsonschema（可选依赖）
try:
    import jsonschema
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False

# 导入门禁配置模块
try:
    from unified_stack_gate_contract import (
        PROFILE_CONFIGS,
        CapabilityReport,
        ProfileType,
        ReasonCode,
        StepName,
        detect_capabilities,
        get_profile_from_env,
        get_required_steps_for_profile,
        validate_profile,
    )
    HAS_GATE_CONTRACT = True
except ImportError:
    HAS_GATE_CONTRACT = False

# ============================================================================
# Degradation Skip Reason 分类
# ============================================================================

# 明确允许的跳过原因（用户显式设置）
ALLOWED_SKIP_REASONS = frozenset({
    "explicit_skip",              # 用户显式设置 SKIP_DEGRADATION_TEST=1
    "user_skip",                  # 用户显式跳过
    "skip_degradation_test",      # 环境变量标记
    "http_only_mode",             # HTTP_ONLY 模式不需要 degradation
    "not_required_for_profile",   # profile 不要求此步骤
})

# 不允许的跳过原因（基础设施缺失）- FULL 模式下这些原因会导致 CI 失败
DISALLOWED_SKIP_REASONS = frozenset({
    "no_docker",                  # Docker 不可用
    "docker_not_found",
    "no_postgres_dsn",            # POSTGRES_DSN 未设置
    "postgres_dsn_missing",
    "no_container",               # 容器不存在
    "container_not_found",
    "cannot_stop_container",      # 无法停止容器
    "docker_daemon_down",         # Docker daemon 未运行
    "compose_not_configured",     # docker-compose 未配置
    "no_db_access",               # 无数据库访问能力
    "capability_missing",         # 缺少必要能力
})

# Reason 到建议修复项的映射
REASON_TO_FIX_SUGGESTIONS: dict[str, list[str]] = {
    "no_docker": [
        "安装 Docker: https://docs.docker.com/get-docker/",
        "确保 Docker CLI 在 PATH 中",
    ],
    "docker_not_found": [
        "安装 Docker: https://docs.docker.com/get-docker/",
        "确保 Docker CLI 在 PATH 中",
    ],
    "docker_daemon_down": [
        "启动 Docker daemon: `sudo systemctl start docker` 或打开 Docker Desktop",
        "检查 Docker 服务状态: `docker info`",
    ],
    "no_postgres_dsn": [
        "设置环境变量: export POSTGRES_DSN='postgresql://user:pass@host:5432/dbname'",
        "确保数据库服务正在运行",
    ],
    "postgres_dsn_missing": [
        "设置环境变量: export POSTGRES_DSN='postgresql://user:pass@host:5432/dbname'",
    ],
    "no_container": [
        "启动 OpenMemory 服务: `docker-compose up -d openmemory`",
        "检查服务状态: `docker-compose ps`",
    ],
    "container_not_found": [
        "启动 OpenMemory 服务: `docker-compose up -d openmemory`",
    ],
    "cannot_stop_container": [
        "检查 Docker 权限: 用户是否在 docker 组中",
        "检查容器状态: `docker ps -a`",
        "尝试手动停止: `docker stop <container_name>`",
    ],
    "compose_not_configured": [
        "确保项目根目录有 docker-compose.yml 文件",
        "运行: `docker-compose config` 验证配置",
    ],
    "no_db_access": [
        "安装 psql: `apt-get install postgresql-client` 或 `brew install postgresql`",
        "或安装 psycopg: `pip install psycopg2-binary`",
    ],
    "capability_missing": [
        "运行 `python unified_stack_gate_contract.py detect-capabilities` 查看缺失能力",
        "根据缺失能力安装相应工具",
    ],
}

# ============================================================================
# 常量定义
# ============================================================================

# 回退配置：当 gate_contract 模块不可用时使用
# 基础模式必需的步骤（对应 standard profile）
REQUIRED_STEPS_BASE = ["health_checks", "memory_store", "memory_query", "jsonrpc"]

# FULL 模式额外必需的步骤
REQUIRED_STEPS_FULL = ["db_invariants", "degradation"]

# HTTP_ONLY 模式必需的步骤
REQUIRED_STEPS_HTTP_ONLY = ["health_checks", "memory_store", "memory_query"]

# 默认 schema 文件路径（相对于脚本目录）
DEFAULT_SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "unified_stack_verify_results_v2.schema.json"


def _get_required_steps_fallback(profile: str, full_mode: bool = False) -> list[str]:
    """
    回退方法：获取指定 profile 的必需步骤

    当 gate_contract 模块不可用时使用
    """
    if profile == "http_only":
        return REQUIRED_STEPS_HTTP_ONLY.copy()
    elif profile == "full" or full_mode:
        return REQUIRED_STEPS_BASE + REQUIRED_STEPS_FULL
    else:  # standard
        return REQUIRED_STEPS_BASE.copy()


def get_required_steps(profile: str, full_mode: bool = False) -> list[str]:
    """
    获取指定 profile 的必需步骤

    优先使用 gate_contract 模块，回退到本地定义
    """
    if HAS_GATE_CONTRACT:
        try:
            profile_type = ProfileType(profile)
            return get_required_steps_for_profile(profile_type)
        except ValueError:
            pass
    return _get_required_steps_fallback(profile, full_mode)


def determine_profile(full_mode: bool = False) -> str:
    """
    确定当前校验使用的 profile

    优先级:
      1. gate_contract 模块的 get_profile_from_env()
      2. full_mode 参数
      3. 环境变量推断
      4. 默认 standard
    """
    if HAS_GATE_CONTRACT:
        profile = get_profile_from_env()
        # 如果 CLI 指定了 --full，覆盖为 full
        if full_mode and profile != ProfileType.FULL:
            return ProfileType.FULL.value
        return profile.value

    # 回退逻辑
    explicit = os.environ.get("GATE_PROFILE", "").lower()
    if explicit in ("http_only", "httponly"):
        return "http_only"
    elif explicit == "full":
        return "full"
    elif explicit == "standard":
        return "standard"

    if full_mode:
        return "full"
    if os.environ.get("HTTP_ONLY_MODE") == "1":
        return "http_only"
    if os.environ.get("SKIP_DEGRADATION_TEST") == "1":
        return "standard"

    return "standard"

# ============================================================================
# 颜色输出
# ============================================================================

class Colors:
    RED = '\033[0;31m'
    GREEN = '\033[0;32m'
    YELLOW = '\033[1;33m'
    BLUE = '\033[0;34m'
    NC = '\033[0m'  # No Color

    @classmethod
    def disable(cls):
        """禁用颜色输出（用于非 TTY 环境）"""
        cls.RED = ''
        cls.GREEN = ''
        cls.YELLOW = ''
        cls.BLUE = ''
        cls.NC = ''

# 非 TTY 环境禁用颜色
if not sys.stdout.isatty():
    Colors.disable()

def log_info(msg: str):
    print(f"{Colors.BLUE}[INFO]{Colors.NC} {msg}")

def log_success(msg: str):
    print(f"{Colors.GREEN}[PASS]{Colors.NC} {msg}")

def log_warn(msg: str):
    print(f"{Colors.YELLOW}[WARN]{Colors.NC} {msg}")

def log_error(msg: str):
    print(f"{Colors.RED}[FAIL]{Colors.NC} {msg}")

# ============================================================================
# 校验逻辑
# ============================================================================

def load_json_file(path: Path) -> Optional[dict]:
    """加载 JSON 文件"""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        log_error(f"文件不存在: {path}")
        return None
    except json.JSONDecodeError as e:
        log_error(f"JSON 解析失败: {e}")
        return None

def validate_schema(data: dict, schema_path: Path) -> tuple[bool, list[str]]:
    """
    按 JSON Schema 校验数据

    Returns:
        (is_valid, error_messages)
    """
    if not HAS_JSONSCHEMA:
        log_warn("jsonschema 未安装，跳过 schema 校验")
        return True, []

    schema = load_json_file(schema_path)
    if schema is None:
        return False, [f"无法加载 schema 文件: {schema_path}"]

    try:
        jsonschema.validate(instance=data, schema=schema)
        return True, []
    except jsonschema.ValidationError as e:
        error_path = ".".join(str(p) for p in e.absolute_path)
        error_msg = f"Schema 校验失败: {e.message}"
        if error_path:
            error_msg += f" (path: {error_path})"
        return False, [error_msg]
    except jsonschema.SchemaError as e:
        return False, [f"Schema 定义错误: {e.message}"]

def validate_required_steps(
    data: dict,
    full_mode: bool = False,
    profile: Optional[str] = None
) -> tuple[bool, list[str], list[str]]:
    """
    校验必需步骤是否存在，且在 FULL 模式下不能为 skipped

    Args:
        data: 验证结果 JSON
        full_mode: 是否为 FULL 模式（兼容旧接口）
        profile: profile 名称（优先于 full_mode）

    Returns:
        (is_valid, error_messages, fix_suggestions)
    """
    steps = data.get("steps", [])
    step_map = {step.get("name"): step for step in steps if isinstance(step, dict)}
    step_names = set(step_map.keys())

    # 确定使用的 profile
    if profile is None:
        profile = determine_profile(full_mode)

    # 获取必需步骤列表
    required_steps = get_required_steps(profile, full_mode)

    errors = []
    fix_suggestions = []
    missing_steps = []
    skipped_steps = []

    for step_name in required_steps:
        if step_name not in step_names:
            missing_steps.append(step_name)
        elif full_mode or profile == "full":
            # FULL 模式下：必需步骤不能为 skipped
            step = step_map.get(step_name)
            if step and step.get("status") == "skipped":
                reason = step.get("reason", "unknown")
                # 检查是否为允许的跳过原因
                if not _is_allowed_skip_reason(reason):
                    skipped_steps.append((step_name, reason))

    profile_str = profile.upper() if profile else ("FULL" if full_mode else "STANDARD")

    if missing_steps:
        errors.append(f"{profile_str} 模式缺少必需步骤: {', '.join(missing_steps)}")
        fix_suggestions.append("确保验证脚本运行了所有必需步骤")

    if skipped_steps:
        for step_name, reason in skipped_steps:
            errors.append(
                f"{profile_str} 模式必需步骤 {step_name} 被跳过（原因: {reason}）"
            )
            # 添加针对性修复建议
            suggestions = REASON_TO_FIX_SUGGESTIONS.get(reason, [])
            fix_suggestions.extend(suggestions)

    return len(errors) == 0, errors, fix_suggestions


def _is_allowed_skip_reason(reason: str) -> bool:
    """
    判断跳过原因是否为明确允许的

    Args:
        reason: 跳过原因字符串

    Returns:
        True 表示允许跳过，False 表示不允许
    """
    if not reason:
        return False

    reason_lower = reason.lower().replace("-", "_").replace(" ", "_")

    # 检查是否在允许列表中
    for allowed in ALLOWED_SKIP_REASONS:
        if allowed in reason_lower or reason_lower in allowed:
            return True

    # 检查是否包含明确的跳过标识
    if "explicit" in reason_lower or "user" in reason_lower:
        return True
    if "skip_degradation_test" in reason_lower:
        return True

    return False

def validate_step_statuses(
    data: dict,
    full_mode: bool = False,
    profile: Optional[str] = None
) -> tuple[bool, list[str], list[str], list[str]]:
    """
    校验步骤状态是否符合预期

    - 基础步骤: 必须为 ok（不能为 fail）
    - jsonrpc: http_only 模式下可以为 skipped
    - degradation: full 模式下，根据 reason 判定:
      - 明确允许的原因（SKIP_DEGRADATION_TEST=1）: 记录 warning 但允许
      - 其他原因（no_docker/no_postgres_dsn/no_container 等）: error 并失败
    - db_invariants: full 模式下必须执行（缺 DSN 或无 DB 工具必须 fail）

    Returns:
        (is_valid, error_messages, warnings, fix_suggestions)
    """
    steps = data.get("steps", [])
    step_map = {step.get("name"): step for step in steps if isinstance(step, dict)}

    # 确定使用的 profile
    if profile is None:
        profile = determine_profile(full_mode)

    errors = []
    warnings = []
    fix_suggestions = []

    # 环境变量检查
    skip_degradation = os.environ.get("SKIP_DEGRADATION_TEST") == "1"
    http_only_mode = os.environ.get("HTTP_ONLY_MODE") == "1"

    # 检查基础步骤状态
    for step_name in ["health_checks", "memory_store", "memory_query"]:
        step = step_map.get(step_name)
        if step and step.get("status") == "fail":
            errors.append(f"步骤 {step_name} 失败")
            # 添加通用修复建议
            if step_name == "health_checks":
                fix_suggestions.append("检查 OpenMemory 服务是否正常运行: `docker-compose ps`")
            elif step_name in ("memory_store", "memory_query"):
                fix_suggestions.append("检查 OpenMemory API 配置和连接: `curl $OPENMEMORY_ENDPOINT/health`")

    # jsonrpc: http_only 模式下允许 skipped
    jsonrpc_step = step_map.get("jsonrpc")
    if jsonrpc_step:
        status = jsonrpc_step.get("status")
        if status == "fail":
            errors.append("步骤 jsonrpc 失败")
            fix_suggestions.append("检查 MCP JSON-RPC 端点配置")
        elif status == "skipped":
            if profile == "http_only" or http_only_mode:
                warnings.append("jsonrpc 被跳过（HTTP_ONLY_MODE）")
            else:
                # 非 http_only 模式下跳过 jsonrpc 是错误
                errors.append("步骤 jsonrpc 被意外跳过（非 HTTP_ONLY 模式）")
                fix_suggestions.append("确保 MCP JSON-RPC 测试被正确执行")

    # full 模式下的硬规则检查
    if profile == "full" or full_mode:
        # db_invariants: full 模式下必须执行
        db_invariants_step = step_map.get("db_invariants")
        if db_invariants_step:
            status = db_invariants_step.get("status")
            if status == "fail":
                errors.append("步骤 db_invariants 失败")
                fix_suggestions.append("检查数据库不变量约束，查看详细日志")
            elif status == "skipped":
                reason = db_invariants_step.get("reason", "unknown")
                # 检查是否为明确允许的跳过原因
                if _is_allowed_skip_reason(reason):
                    warnings.append(f"db_invariants 被跳过（原因: {reason}）- 明确允许")
                else:
                    # FULL 模式下 db_invariants 跳过是错误（缺 DSN 或无 DB 工具必须 fail）
                    errors.append(f"FULL 模式下 db_invariants 被跳过是禁止的: {reason}")
                    # 添加针对性修复建议
                    suggestions = REASON_TO_FIX_SUGGESTIONS.get(reason, [])
                    fix_suggestions.extend(suggestions)
                    if not suggestions:
                        fix_suggestions.extend([
                            "设置 POSTGRES_DSN 环境变量",
                            "安装 psql 或 psycopg: `pip install psycopg2-binary`",
                        ])

        # degradation: full 模式下检查
        degradation_step = step_map.get("degradation")
        if degradation_step:
            status = degradation_step.get("status")
            if status == "fail":
                errors.append("步骤 degradation 失败")
                fix_suggestions.append("检查 degradation 测试日志，定位具体失败原因")
            elif status == "skipped":
                reason = degradation_step.get("reason", "unknown")

                # 检查是否有明确跳过指令（来自环境变量）
                if skip_degradation:
                    warnings.append("degradation 被 SKIP_DEGRADATION_TEST=1 明确跳过")
                elif http_only_mode:
                    warnings.append("degradation 被 HTTP_ONLY_MODE=1 明确跳过")
                elif _is_allowed_skip_reason(reason):
                    # reason 本身表示允许跳过
                    warnings.append(f"degradation 被跳过（原因: {reason}）- 明确允许")
                else:
                    # FULL 模式下，没有明确跳过指令时，degradation 跳过是错误
                    errors.append(
                        f"FULL 模式下 degradation 缺前置条件必须 fail（不能静默跳过）: {reason}"
                    )
                    # 添加针对性修复建议
                    suggestions = REASON_TO_FIX_SUGGESTIONS.get(reason, [])
                    fix_suggestions.extend(suggestions)
                    if not suggestions:
                        # 通用修复建议
                        fix_suggestions.extend([
                            "确保 Docker 已安装并运行: `docker info`",
                            "确保 docker-compose.yml 配置正确: `docker-compose config`",
                            "运行 `python unified_stack_gate_contract.py detect-capabilities` 查看环境能力",
                        ])

    # 输出警告
    for warn in warnings:
        log_warn(warn)

    return len(errors) == 0, errors, warnings, fix_suggestions

def validate_overall_status(data: dict) -> tuple[bool, list[str]]:
    """
    校验整体状态一致性

    - overall_status=pass 时 total_failed 必须为 0
    - overall_status=fail 时必须有失败的步骤

    Returns:
        (is_valid, error_messages)
    """
    overall_status = data.get("overall_status")
    total_failed = data.get("total_failed", 0)

    errors = []

    if overall_status == "pass" and total_failed != 0:
        errors.append(f"overall_status=pass 但 total_failed={total_failed}")

    if overall_status == "fail" and total_failed == 0:
        # 检查是否有步骤实际失败
        steps = data.get("steps", [])
        failed_steps = [s.get("name") for s in steps if s.get("status") == "fail"]
        if not failed_steps:
            errors.append("overall_status=fail 但没有失败的步骤")

    return len(errors) == 0, errors


def extract_capabilities(data: dict) -> dict[str, Any]:
    """
    从验证结果中提取 capabilities 信息

    Args:
        data: 验证结果 JSON

    Returns:
        capabilities 字典，如果不存在则返回空字典
    """
    return data.get("capabilities", {})


def get_missing_capabilities(capabilities: dict[str, Any], profile: str) -> list[str]:
    """
    根据 profile 检查缺失的必要能力

    Args:
        capabilities: capabilities 字典
        profile: 当前 profile

    Returns:
        缺失能力的列表
    """
    missing = []

    # 基础能力检查（所有 profile 都需要）
    if not capabilities.get("python", True):
        missing.append("python")

    # standard/full profile 需要的能力
    if profile in ("standard", "full"):
        # 无额外必要能力（jsonrpc 通过 HTTP 测试）
        pass

    # full profile 需要的能力
    if profile == "full":
        if not capabilities.get("docker", capabilities.get("docker_available", True)):
            missing.append("docker")
        if not capabilities.get("docker_connectable", True):
            missing.append("docker_connectable")
        if not capabilities.get("postgres_dsn", True):
            missing.append("postgres_dsn")
        # db_invariants 需要 psql 或 python_psycopg
        has_db_tool = (
            capabilities.get("psql", capabilities.get("psql_available", True)) or
            capabilities.get("python_psycopg", True)
        )
        if not has_db_tool:
            missing.append("db_tool (psql or python_psycopg)")

    return missing


def format_capabilities_report(capabilities: dict[str, Any]) -> list[str]:
    """
    格式化 capabilities 报告

    Args:
        capabilities: capabilities 字典

    Returns:
        格式化的行列表
    """
    if not capabilities:
        return ["  (无 capabilities 信息)"]

    lines = []

    # boolean 能力
    bool_caps = [
        ("docker", "Docker CLI"),
        ("docker_available", "Docker CLI (alias)"),
        ("docker_connectable", "Docker Daemon"),
        ("compose", "Docker Compose"),
        ("psql", "psql CLI"),
        ("psql_available", "psql CLI (alias)"),
        ("python", "Python"),
        ("python_psycopg", "psycopg/psycopg2"),
        ("postgres_dsn", "POSTGRES_DSN"),
        ("container_resolvable", "Container Resolvable"),
        ("container_stoppable", "Container Stoppable"),
    ]

    for key, label in bool_caps:
        if key in capabilities:
            value = capabilities[key]
            status = "✓" if value else "✗"
            lines.append(f"  [{status}] {label}")

    # string 能力
    str_caps = [
        ("compose_project_name", "Compose Project"),
        ("compose_file", "Compose File"),
        ("profile", "Profile"),
    ]

    for key, label in str_caps:
        if key in capabilities and capabilities[key]:
            lines.append(f"  {label}: {capabilities[key]}")

    return lines if lines else ["  (无 capabilities 信息)"]

def run_validation(
    json_path: Path,
    full_mode: bool = False,
    schema_only: bool = False,
    schema_path: Optional[Path] = None,
    profile: Optional[str] = None
) -> tuple[bool, dict]:
    """
    运行完整校验流程

    Args:
        json_path: 验证结果 JSON 文件路径
        full_mode: 是否为 FULL 模式（兼容旧接口）
        schema_only: 是否仅执行 schema 校验
        schema_path: 自定义 schema 文件路径
        profile: 门禁 profile 名称（优先于 full_mode）

    Returns:
        (is_valid, result_dict)
    """
    # 确定使用的 profile
    if profile is None:
        profile = determine_profile(full_mode)

    result = {
        "json_path": str(json_path),
        "profile": profile,
        "full_mode": full_mode,
        "schema_only": schema_only,
        "checks": {},
        "overall_valid": False,
        "errors": [],
        "warnings": [],
        "fix_suggestions": []
    }

    # 1. 加载 JSON 文件
    data = load_json_file(json_path)
    if data is None:
        result["errors"].append(f"无法加载 JSON 文件: {json_path}")
        return False, result

    result["verify_mode"] = data.get("verify_mode", "unknown")
    result["overall_status"] = data.get("overall_status", "unknown")
    result["total_failed"] = data.get("total_failed", -1)

    # 提取 capabilities 信息
    capabilities = extract_capabilities(data)
    result["capabilities"] = capabilities
    result["missing_capabilities"] = get_missing_capabilities(capabilities, profile)

    # 2. Schema 校验
    if schema_path is None:
        schema_path = Path(os.environ.get("VERIFY_RESULTS_SCHEMA", DEFAULT_SCHEMA_PATH))

    schema_valid, schema_errors = validate_schema(data, schema_path)
    result["checks"]["schema"] = {"valid": schema_valid, "errors": schema_errors}

    if not schema_valid:
        result["errors"].extend(schema_errors)
        if schema_only:
            return False, result

    if schema_only:
        result["overall_valid"] = schema_valid
        return schema_valid, result

    # 3. 必需步骤检查
    steps_valid, steps_errors, steps_fixes = validate_required_steps(data, full_mode, profile)
    result["checks"]["required_steps"] = {
        "valid": steps_valid,
        "errors": steps_errors,
        "fix_suggestions": steps_fixes
    }

    if not steps_valid:
        result["errors"].extend(steps_errors)
        result["fix_suggestions"].extend(steps_fixes)

    # 4. 步骤状态检查
    status_valid, status_errors, status_warnings, status_fixes = validate_step_statuses(
        data, full_mode, profile
    )
    result["checks"]["step_statuses"] = {
        "valid": status_valid,
        "errors": status_errors,
        "warnings": status_warnings,
        "fix_suggestions": status_fixes
    }

    if not status_valid:
        result["errors"].extend(status_errors)
        result["fix_suggestions"].extend(status_fixes)
    result["warnings"].extend(status_warnings)

    # 5. 整体状态一致性检查
    overall_valid, overall_errors = validate_overall_status(data)
    result["checks"]["overall_status"] = {"valid": overall_valid, "errors": overall_errors}

    if not overall_valid:
        result["errors"].extend(overall_errors)

    # 去重 fix_suggestions
    seen = set()
    unique_fixes = []
    for fix in result["fix_suggestions"]:
        if fix not in seen:
            seen.add(fix)
            unique_fixes.append(fix)
    result["fix_suggestions"] = unique_fixes

    # 汇总结果
    all_valid = all([
        schema_valid,
        steps_valid,
        status_valid,
        overall_valid
    ])

    result["overall_valid"] = all_valid
    return all_valid, result

# ============================================================================
# 主入口
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Unified Stack 验证结果校验脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument(
        "json_file",
        type=Path,
        help="验证结果 JSON 文件路径"
    )

    parser.add_argument(
        "--profile",
        choices=["http_only", "standard", "full"],
        default=None,
        help="门禁 profile (http_only/standard/full)，优先于 --full"
    )

    parser.add_argument(
        "--full",
        action="store_true",
        default=os.environ.get("VERIFY_FULL", "0") == "1",
        help="FULL 模式校验（等同于 --profile full）"
    )

    parser.add_argument(
        "--schema-only",
        action="store_true",
        help="仅执行 JSON Schema 校验"
    )

    parser.add_argument(
        "--schema",
        type=Path,
        default=None,
        help="自定义 schema 文件路径"
    )

    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="以 JSON 格式输出结果"
    )

    args = parser.parse_args()

    # 检查文件是否存在
    if not args.json_file.exists():
        log_error(f"文件不存在: {args.json_file}")
        sys.exit(2)

    # 确定使用的 profile
    profile = args.profile
    if profile is None:
        profile = determine_profile(args.full)

    # 运行校验
    profile_str = profile.upper()
    log_info(f"开始校验验证结果（{profile_str} 模式）: {args.json_file}")

    is_valid, result = run_validation(
        json_path=args.json_file,
        full_mode=args.full,
        schema_only=args.schema_only,
        schema_path=args.schema,
        profile=profile
    )

    # 输出结果
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("")
        print("=" * 50)
        print("校验结果汇总")
        print("=" * 50)
        print(f"  文件:          {result['json_path']}")
        print(f"  验证模式:      {result.get('verify_mode', 'unknown')}")
        print(f"  门禁 Profile:  {result.get('profile', profile_str)}")
        print(f"  整体状态:      {result.get('overall_status', 'unknown')}")
        print(f"  失败步骤数:    {result.get('total_failed', -1)}")
        print("")

        # 输出 capabilities 信息
        capabilities = result.get("capabilities", {})
        if capabilities:
            print("环境能力检测:")
            for line in format_capabilities_report(capabilities):
                print(line)
            print("")

        # 输出缺失能力警告
        missing_caps = result.get("missing_capabilities", [])
        if missing_caps:
            print(f"{Colors.YELLOW}缺失的必要能力 ({profile_str} profile):{Colors.NC}")
            for cap in missing_caps:
                print(f"  - {cap}")
            print("")

        # 输出各检查项结果
        for check_name, check_result in result.get("checks", {}).items():
            status = "✓" if check_result.get("valid") else "✗"
            print(f"  [{status}] {check_name}")
            for error in check_result.get("errors", []):
                print(f"      - {error}")

        print("")

        # 输出警告
        if result.get("warnings"):
            print("警告列表:")
            for warn in result["warnings"]:
                log_warn(warn)
            print("")

        if result.get("errors"):
            print("错误列表:")
            for error in result["errors"]:
                log_error(error)
            print("")

        # 输出建议修复项
        if result.get("fix_suggestions"):
            print("建议修复项:")
            for i, suggestion in enumerate(result["fix_suggestions"], 1):
                print(f"  {i}. {suggestion}")
            print("")

        if is_valid:
            log_success("校验通过！")
        else:
            log_error("校验失败！")

    sys.exit(0 if is_valid else 1)


if __name__ == "__main__":
    main()
