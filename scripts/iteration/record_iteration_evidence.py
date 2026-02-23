#!/usr/bin/env python3
"""记录迭代验收证据到版本化目录。

用法:
    python scripts/iteration/record_iteration_evidence.py <iteration_number> [options]

示例:
    # 基本用法（自动获取当前 commit sha）
    python scripts/iteration/record_iteration_evidence.py 13

    # 指定 commit sha
    python scripts/iteration/record_iteration_evidence.py 13 --commit abc1234

    # 从 JSON 文件读取命令结果
    python scripts/iteration/record_iteration_evidence.py 13 --commands-json .artifacts/acceptance-runs/run_123.json

    # 直接传入命令结果 JSON 字符串
    python scripts/iteration/record_iteration_evidence.py 13 --commands '{"make ci": {"exit_code": 0, "summary": "passed"}}'

    # 指定 CI 运行 URL
    python scripts/iteration/record_iteration_evidence.py 13 --ci-run-url https://github.com/org/repo/actions/runs/123

    # 预览模式（不实际写入）
    python scripts/iteration/record_iteration_evidence.py 13 --dry-run

功能:
    1. 记录迭代验收测试的执行证据
    2. 自动获取当前 git commit sha（可覆盖）
    3. 支持从 JSON 文件或参数读取命令执行结果
    4. 内置敏感信息脱敏（PASSWORD/DSN/TOKEN 等）
    5. 输出到 docs/acceptance/evidence/iteration_<N>_evidence.json（固定文件名策略）
    6. 输出格式符合 iteration_evidence_v2.schema.json

安全特性:
    - 检测并拒绝写入常见敏感键（PASSWORD/DSN/TOKEN/SECRET/KEY/CREDENTIAL）
    - 敏感值会被替换为 "[REDACTED]" 占位符
    - 输出文件包含 sensitive_data_declaration=true 声明
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, cast

from scripts.iteration.iteration_evidence_naming import (
    EVIDENCE_DIR,
    canonical_evidence_filename,
)
from scripts.iteration.iteration_evidence_schema import CURRENT_SCHEMA_REF

# 项目根目录
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# 兼容扁平导入与包导入共存，避免出现两个模块实例导致 monkeypatch 失效。
sys.modules.setdefault("record_iteration_evidence", sys.modules[__name__])
sys.modules.setdefault("scripts.iteration.record_iteration_evidence", sys.modules[__name__])

# 敏感键模式（不区分大小写）
SENSITIVE_KEY_PATTERNS = [
    re.compile(r".*password.*", re.IGNORECASE),
    re.compile(r".*passwd.*", re.IGNORECASE),
    re.compile(r".*dsn.*", re.IGNORECASE),
    re.compile(r".*token.*", re.IGNORECASE),
    re.compile(r".*secret.*", re.IGNORECASE),
    re.compile(r".*api_key.*", re.IGNORECASE),
    re.compile(r".*apikey.*", re.IGNORECASE),
    re.compile(r".*credential.*", re.IGNORECASE),
    re.compile(r".*private_key.*", re.IGNORECASE),
    re.compile(r".*auth_token.*", re.IGNORECASE),
    re.compile(r".*access_key.*", re.IGNORECASE),
]

# 安全键名（不应被脱敏）
SAFE_KEY_NAMES = {
    "commit_sha",
    "commit",
    "sha",
    "hash",
    "exit_code",
    "iteration_number",
    "timestamp",
    "recorded_at",
    "command",
    "summary",
    "duration_seconds",
    "name",
    "result",
    "os",
    "python",
    "arch",
    "runner_label",
    "hostname",
    "ci_run_url",
    "pr_url",
    "artifact_url",
    "regression_doc_url",
    "notes",
    "overall_result",
    "sensitive_data_declaration",
}

# 敏感值模式（检测值本身是否像敏感信息）
SENSITIVE_VALUE_PATTERNS = [
    # PostgreSQL DSN 格式
    re.compile(r"postgres(ql)?://[^\s]+", re.IGNORECASE),
    # 通用连接字符串
    re.compile(r"(mysql|redis|mongodb|amqp)://[^\s]+", re.IGNORECASE),
    # Bearer token
    re.compile(r"Bearer\s+[A-Za-z0-9\-_.]+", re.IGNORECASE),
    # Base64 编码的长字符串（可能是密钥，排除 hex 格式如 git SHA）
    # Git SHA 只包含 0-9a-f，而 Base64 包含大写字母和 +/=
    re.compile(r"^[A-Za-z0-9+/]{40,}={1,2}$"),  # 必须有 = 结尾才算 Base64
    # AWS 风格的密钥
    re.compile(r"^AKIA[A-Z0-9]{16}$"),
    # GitHub token
    re.compile(r"^gh[ps]_[A-Za-z0-9]{36,}$"),
]

REDACTED_PLACEHOLDER = "[REDACTED]"

# commit_sha 的 schema pattern
COMMIT_SHA_PATTERN = re.compile(r"^[a-f0-9]{7,40}$")

# 命令名称的 schema pattern
COMMAND_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")


# ============================================================================
# 类型定义
# ============================================================================

CommandResultType = Literal["PASS", "FAIL", "SKIP", "ERROR"]
OverallResultType = Literal["PASS", "PARTIAL", "FAIL"]


# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class SensitiveKeyWarning:
    """检测到的敏感键警告。"""

    key_path: str
    reason: str


@dataclass
class CommandEntry:
    """单个门禁命令的执行记录（符合 iteration_evidence_v2.schema.json）。"""

    name: str
    command: str
    result: CommandResultType
    summary: Optional[str] = None
    duration_seconds: Optional[float] = None
    exit_code: Optional[int] = None


@dataclass
class RunnerInfo:
    """执行环境信息（符合 iteration_evidence_v2.schema.json）。"""

    os: str
    python: str
    arch: str
    hostname: Optional[str] = None
    runner_label: Optional[str] = None


@dataclass
class Links:
    """相关链接集合（符合 iteration_evidence_v2.schema.json）。"""

    ci_run_url: Optional[str] = None
    pr_url: Optional[str] = None
    artifact_url: Optional[str] = None
    regression_doc_url: Optional[str] = None


@dataclass
class EvidenceRecord:
    """迭代验收证据记录（符合 iteration_evidence_v2.schema.json）。"""

    iteration_number: int
    recorded_at: str
    commit_sha: str
    runner: RunnerInfo
    commands: List[CommandEntry]
    links: Optional[Links] = None
    notes: Optional[str] = None
    overall_result: Optional[OverallResultType] = None
    sensitive_data_declaration: bool = True


@dataclass
class RecordResult:
    """记录操作结果。"""

    success: bool
    message: str
    output_path: Optional[str] = None
    sensitive_warnings: List[SensitiveKeyWarning] = field(default_factory=list)
    redacted_count: int = 0


class SensitiveDataError(Exception):
    """当检测到无法脱敏的敏感数据时抛出。"""

    def __init__(self, warnings: List[SensitiveKeyWarning]) -> None:
        self.warnings = warnings
        details = "\n".join(f"  - {w.key_path}: {w.reason}" for w in warnings)
        super().__init__(f"检测到敏感数据:\n{details}")


class SchemaValidationError(Exception):
    """当数据不符合 schema 要求时抛出。"""

    def __init__(self, field: str, value: str, pattern: str, hint: str = "") -> None:
        self.field = field
        self.value = value
        self.pattern = pattern
        msg = f"字段 '{field}' 的值 '{value}' 不符合 schema pattern: {pattern}"
        if hint:
            msg += f"\n    提示: {hint}"
        super().__init__(msg)


# ============================================================================
# 环境信息收集
# ============================================================================


def get_runner_info(runner_label: Optional[str] = None) -> RunnerInfo:
    """获取当前执行环境信息。

    Args:
        runner_label: CI runner 标签（可选）

    Returns:
        RunnerInfo 对象
    """
    # 获取 OS 信息
    system = platform.system().lower()
    if system == "darwin":
        os_info = f"darwin-{platform.release()}"
    elif system == "linux":
        # 尝试获取发行版信息
        try:
            import distro

            os_info = f"{distro.id()}-{distro.version()}"
        except ImportError:
            os_info = f"linux-{platform.release()}"
    elif system == "windows":
        os_info = f"windows-{platform.release()}"
    else:
        os_info = f"{system}-{platform.release()}"

    # 获取 Python 版本
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

    # 获取架构
    machine = platform.machine().lower()
    # 规范化架构名称以匹配 schema 的 enum
    arch_map = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "arm64": "arm64",
        "aarch64": "aarch64",
        "i686": "i686",
        "i386": "i386",
    }
    arch = arch_map.get(machine, machine)

    return RunnerInfo(
        os=os_info,
        python=python_version,
        arch=arch,
        runner_label=runner_label,
    )


def exit_code_to_result(exit_code: int) -> CommandResultType:
    """将退出码转换为结果状态。

    Args:
        exit_code: 命令退出码

    Returns:
        结果状态字符串
    """
    if exit_code == 0:
        return "PASS"
    return "FAIL"


def compute_overall_result(commands: List[CommandEntry]) -> OverallResultType:
    """根据命令执行结果计算整体结果。

    Args:
        commands: 命令执行记录列表

    Returns:
        整体结果状态
    """
    if not commands:
        return "FAIL"

    results = [cmd.result for cmd in commands]
    if all(r == "PASS" for r in results):
        return "PASS"
    if all(r in ("FAIL", "ERROR") for r in results):
        return "FAIL"
    return "PARTIAL"


def derive_command_name(command: str) -> str:
    """从命令字符串推导命令名称。

    生成的名称符合 iteration_evidence_v2.schema.json 的 pattern: ^[a-z][a-z0-9_-]*$

    Args:
        command: 完整命令字符串

    Returns:
        简短命令名称（小写，符合 schema pattern）
    """
    name = ""

    # 处理常见的 make 目标
    if command.startswith("make "):
        target = command[5:].split()[0]
        name = target

    # 处理 pytest
    elif "pytest" in command:
        name = "test"

    # 处理其他命令：取第一个词
    elif command.split():
        name = command.split()[0]

    else:
        name = "unknown"

    # 规范化名称以符合 schema pattern: ^[a-z][a-z0-9_-]*$
    name = normalize_command_name(name)
    return name


def normalize_command_name(name: str) -> str:
    """规范化命令名称以符合 schema pattern。

    Schema pattern: ^[a-z][a-z0-9_-]*$

    Args:
        name: 原始名称

    Returns:
        规范化后的名称
    """
    # 转小写
    name = name.lower()

    # 替换不允许的字符为下划线
    result = []
    for i, char in enumerate(name):
        if char.isalnum() or char in "_-":
            result.append(char)
        elif char in ".":
            result.append("_")
        else:
            result.append("_")

    name = "".join(result)

    # 去除连续的下划线
    while "__" in name:
        name = name.replace("__", "_")

    # 去除首尾下划线/连字符
    name = name.strip("_-")

    # 确保以字母开头
    if not name or not name[0].isalpha():
        name = "cmd_" + name if name else "cmd"

    # 限制长度（schema maxLength: 64）
    if len(name) > 64:
        name = name[:64].rstrip("_-")

    return name


# ============================================================================
# Git 操作
# ============================================================================


def get_current_commit_sha() -> Optional[str]:
    """获取当前 git commit SHA。

    Returns:
        commit SHA 字符串，如果不在 git 仓库中则返回 None
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except (OSError, subprocess.SubprocessError):
        return None


def get_short_commit_sha(full_sha: str) -> str:
    """获取短格式 commit SHA。

    Args:
        full_sha: 完整的 commit SHA

    Returns:
        7 位短格式 SHA
    """
    return full_sha[:7] if len(full_sha) >= 7 else full_sha


def validate_commit_sha(commit_sha: str) -> None:
    """验证 commit_sha 是否符合 schema pattern。

    Schema pattern: ^[a-f0-9]{7,40}$

    Args:
        commit_sha: 待验证的 commit SHA

    Raises:
        SchemaValidationError: 如果不符合 pattern
    """
    if not COMMIT_SHA_PATTERN.match(commit_sha):
        raise SchemaValidationError(
            field="commit_sha",
            value=commit_sha,
            pattern="^[a-f0-9]{7,40}$",
            hint="commit_sha 必须是 7-40 位的十六进制字符串（小写）。"
            "如果提供的值被脱敏或格式不正确，请使用 --commit 参数提供有效的 git SHA。",
        )


def validate_command_name(name: str) -> None:
    """验证命令名称是否符合 schema pattern。

    Schema pattern: ^[a-z][a-z0-9_-]*$

    Args:
        name: 待验证的命令名称

    Raises:
        SchemaValidationError: 如果不符合 pattern
    """
    if not COMMAND_NAME_PATTERN.match(name):
        raise SchemaValidationError(
            field="command.name",
            value=name,
            pattern="^[a-z][a-z0-9_-]*$",
            hint="命令名称必须以小写字母开头，只能包含小写字母、数字、下划线和连字符。",
        )


# ============================================================================
# 敏感信息检测与脱敏
# ============================================================================


def is_sensitive_key(key: str) -> bool:
    """检查键名是否为敏感键。

    Args:
        key: 键名

    Returns:
        是否为敏感键
    """
    # 安全键名不应被脱敏
    if key.lower() in {k.lower() for k in SAFE_KEY_NAMES}:
        return False
    return any(pattern.match(key) for pattern in SENSITIVE_KEY_PATTERNS)


def is_sensitive_value(value: Any) -> bool:
    """检查值是否像敏感信息。

    Args:
        value: 要检查的值

    Returns:
        是否为敏感值
    """
    if not isinstance(value, str):
        return False
    return any(pattern.search(value) for pattern in SENSITIVE_VALUE_PATTERNS)


def redact_sensitive_data(
    data: Any,
    path: str = "",
) -> tuple[Any, List[SensitiveKeyWarning], int]:
    """递归脱敏敏感数据。

    Args:
        data: 要脱敏的数据
        path: 当前键路径（用于报告）

    Returns:
        (脱敏后的数据, 警告列表, 脱敏计数)
    """
    warnings: List[SensitiveKeyWarning] = []
    redacted_count = 0

    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            current_path = f"{path}.{key}" if path else key

            # 安全键名跳过脱敏检查
            is_safe_key = key.lower() in {k.lower() for k in SAFE_KEY_NAMES}

            # 检查键名是否敏感
            if not is_safe_key and is_sensitive_key(key):
                warnings.append(
                    SensitiveKeyWarning(
                        key_path=current_path,
                        reason=f"敏感键名匹配: {key}",
                    )
                )
                result[key] = REDACTED_PLACEHOLDER
                redacted_count += 1
            # 检查值是否像敏感信息（安全键名的值不检查）
            elif not is_safe_key and isinstance(value, str) and is_sensitive_value(value):
                warnings.append(
                    SensitiveKeyWarning(
                        key_path=current_path,
                        reason="值匹配敏感信息模式",
                    )
                )
                result[key] = REDACTED_PLACEHOLDER
                redacted_count += 1
            else:
                # 递归处理
                redacted_value, sub_warnings, sub_count = redact_sensitive_data(value, current_path)
                result[key] = redacted_value
                warnings.extend(sub_warnings)
                redacted_count += sub_count
        return result, warnings, redacted_count

    elif isinstance(data, list):
        result_list: List[Any] = []
        for i, item in enumerate(data):
            current_path = f"{path}[{i}]"
            redacted_item, sub_warnings, sub_count = redact_sensitive_data(item, current_path)
            result_list.append(redacted_item)
            warnings.extend(sub_warnings)
            redacted_count += sub_count
        return result_list, warnings, redacted_count

    elif isinstance(data, str) and is_sensitive_value(data):
        warnings.append(
            SensitiveKeyWarning(
                key_path=path or "(root)",
                reason="值匹配敏感信息模式",
            )
        )
        return REDACTED_PLACEHOLDER, warnings, 1

    return data, warnings, redacted_count


def derive_redaction_rules(warnings: List[SensitiveKeyWarning]) -> List[str]:
    """根据脱敏警告推导规则标识。"""
    rules: List[str] = []
    if any("敏感键名匹配" in warning.reason for warning in warnings):
        rules.append("sensitive-key")
    if any("值匹配敏感信息模式" in warning.reason for warning in warnings):
        rules.append("sensitive-value")
    if not rules and warnings:
        rules.append("sensitive-data")
    return rules


# ============================================================================
# 命令结果解析
# ============================================================================


def parse_commands_json(json_data: Dict[str, Any]) -> List[CommandEntry]:
    """解析命令结果 JSON。

    支持多种格式:
    1. 简单格式: {"command": {"exit_code": 0, "summary": "..."}}
    2. 数组格式: [{"command": "...", "exit_code": 0, "summary": "..."}]
    3. Schema 格式: [{"name": "...", "command": "...", "result": "PASS", ...}]

    Args:
        json_data: JSON 数据

    Returns:
        CommandEntry 列表
    """
    results: List[CommandEntry] = []

    if isinstance(json_data, list):
        # 数组格式
        for item in json_data:
            if isinstance(item, dict):
                # 检查是否为 schema 格式（已有 name 和 result）
                if "name" in item and "result" in item:
                    # 规范化命令名称以符合 schema
                    results.append(
                        CommandEntry(
                            name=normalize_command_name(item["name"]),
                            command=item.get("command", item["name"]),
                            result=item["result"],
                            summary=item.get("summary"),
                            duration_seconds=item.get("duration_seconds"),
                            exit_code=item.get("exit_code"),
                        )
                    )
                elif "command" in item:
                    # 旧格式：需要转换
                    exit_code = item.get("exit_code", 0)
                    # 规范化命令名称以符合 schema
                    raw_name = item.get("name", derive_command_name(item["command"]))
                    results.append(
                        CommandEntry(
                            name=normalize_command_name(raw_name),
                            command=item["command"],
                            result=exit_code_to_result(exit_code),
                            summary=item.get("summary"),
                            duration_seconds=item.get("duration_seconds"),
                            exit_code=exit_code,
                        )
                    )
    elif isinstance(json_data, dict):
        # 简单格式: {"make ci": {"exit_code": 0, ...}}
        for key, value in json_data.items():
            if isinstance(value, dict):
                exit_code = value.get("exit_code", 0)
                # 规范化命令名称以符合 schema
                results.append(
                    CommandEntry(
                        name=normalize_command_name(derive_command_name(key)),
                        command=key,
                        result=exit_code_to_result(exit_code),
                        summary=value.get("summary"),
                        duration_seconds=value.get("duration_seconds"),
                        exit_code=exit_code,
                    )
                )

    return results


def parse_add_command_arg(arg: str) -> Optional[CommandEntry]:
    """解析 --add-command 参数的 NAME:COMMAND:RESULT 格式。

    格式: NAME:COMMAND:RESULT
    - NAME: 命令标识符（会自动规范化为符合 schema 的格式）
    - COMMAND: 实际执行的命令
    - RESULT: PASS/FAIL/SKIP/ERROR

    Args:
        arg: 命令行参数字符串

    Returns:
        CommandEntry 或 None（如果解析失败）
    """
    # 支持用冒号分隔，但 COMMAND 部分可能包含冒号（如 URL）
    # 使用从右边分割的方式：最后一个部分是 RESULT，第一个部分是 NAME，中间是 COMMAND
    parts = arg.split(":")

    if len(parts) < 3:
        return None

    # 最后一个是 RESULT
    result_str = parts[-1].strip().upper()
    if result_str not in ("PASS", "FAIL", "SKIP", "ERROR"):
        return None

    # 第一个是 NAME
    name = parts[0].strip()
    if not name:
        return None

    # 中间的都是 COMMAND（用冒号重新连接）
    command = ":".join(parts[1:-1]).strip()
    if not command:
        return None

    return CommandEntry(
        name=normalize_command_name(name),
        command=command,
        result=cast(CommandResultType, result_str),
    )


def extract_summary_from_acceptance_run(json_data: Dict[str, Any]) -> List[CommandEntry]:
    """从 .artifacts/acceptance-runs/*.json 格式提取摘要。

    Args:
        json_data: acceptance run JSON 数据

    Returns:
        CommandEntry 列表
    """
    # 尝试多种可能的格式
    if "results" in json_data:
        # 格式: {"results": [...]}
        return parse_commands_json(json_data["results"])

    if "commands" in json_data:
        # 格式: {"commands": {...}} 或 {"commands": [...]}
        return parse_commands_json(json_data["commands"])

    # 尝试作为简单格式解析
    return parse_commands_json(json_data)


# ============================================================================
# 核心记录逻辑
# ============================================================================


def record_evidence(
    iteration_number: int,
    commit_sha: str,
    commands: List[CommandEntry],
    ci_run_url: Optional[str] = None,
    notes: Optional[str] = None,
    runner_label: Optional[str] = None,
    regression_doc_url: Optional[str] = None,
    pr_url: Optional[str] = None,
    artifact_url: Optional[str] = None,
    source_type: Optional[str] = None,
    source_ref: Optional[str] = None,
    include_regression_doc_url: bool = True,
    *,
    dry_run: bool = False,
) -> RecordResult:
    """记录迭代验收证据。

    Args:
        iteration_number: 迭代编号
        commit_sha: commit SHA
        commands: 命令执行结果列表
        ci_run_url: CI 运行 URL（可选）
        notes: 补充说明（可选）
        runner_label: CI runner 标签（可选）
        regression_doc_url: 回归文档 URL（可选，默认自动生成）
        pr_url: Pull Request URL（可选）
        artifact_url: CI Artifacts 下载 URL（可选）
        source_type: 证据来源类型（可选）
        source_ref: 证据来源引用标识（可选）
        include_regression_doc_url: 是否包含 regression_doc_url（默认 True）
        dry_run: 是否为预览模式

    Returns:
        RecordResult 操作结果

    Raises:
        SchemaValidationError: 如果 commit_sha 或 command.name 不符合 schema pattern
    """
    # 验证 commit_sha 符合 schema pattern（fail-fast）
    validate_commit_sha(commit_sha)

    # 验证所有命令名称符合 schema pattern
    for cmd in commands:
        validate_command_name(cmd.name)

    # 获取 UTC 时间
    recorded_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 获取 runner 信息
    runner = get_runner_info(runner_label)

    default_regression_doc_path = f"docs/acceptance/iteration_{iteration_number}_regression.md"
    source_path = regression_doc_url or default_regression_doc_path

    # 构建 links 对象
    # 默认总是写入 regression_doc_url，除非 include_regression_doc_url=False
    actual_regression_doc_url: Optional[str] = None
    if include_regression_doc_url:
        actual_regression_doc_url = regression_doc_url or default_regression_doc_path
    else:
        # 如果用户关闭自动生成但仍显式传入，则使用传入值
        actual_regression_doc_url = regression_doc_url

    links: Optional[Links] = None
    if ci_run_url or actual_regression_doc_url or pr_url or artifact_url:
        links = Links(
            ci_run_url=ci_run_url,
            regression_doc_url=actual_regression_doc_url,
            pr_url=pr_url,
            artifact_url=artifact_url,
        )

    # 计算整体结果
    overall_result = compute_overall_result(commands)

    # 创建证据记录
    record = EvidenceRecord(
        iteration_number=iteration_number,
        recorded_at=recorded_at,
        commit_sha=commit_sha,
        runner=runner,
        commands=commands,
        links=links,
        notes=notes,
        overall_result=overall_result,
        sensitive_data_declaration=True,
    )

    # 转换为字典（符合 iteration_evidence_v2.schema.json）
    record_dict: Dict[str, Any] = {
        "$schema": CURRENT_SCHEMA_REF,
        "iteration_number": record.iteration_number,
        "recorded_at": record.recorded_at,
        "commit_sha": record.commit_sha,
        "runner": {
            "os": record.runner.os,
            "python": record.runner.python,
            "arch": record.runner.arch,
        },
        "source": {
            "source_path": source_path,
        },
        "commands": [],
        "overall_result": record.overall_result,
        "sensitive_data_declaration": record.sensitive_data_declaration,
    }

    # 添加可选 runner 字段
    if record.runner.runner_label:
        record_dict["runner"]["runner_label"] = record.runner.runner_label
    if record.runner.hostname:
        record_dict["runner"]["hostname"] = record.runner.hostname

    if source_type:
        record_dict["source"]["source_type"] = source_type
    if source_ref:
        record_dict["source"]["source_ref"] = source_ref

    # 添加 commands
    for cmd in record.commands:
        cmd_dict: Dict[str, Any] = {
            "name": cmd.name,
            "command": cmd.command,
            "result": cmd.result,
        }
        if cmd.summary:
            cmd_dict["summary"] = cmd.summary
        if cmd.duration_seconds is not None:
            cmd_dict["duration_seconds"] = cmd.duration_seconds
        if cmd.exit_code is not None:
            cmd_dict["exit_code"] = cmd.exit_code
        record_dict["commands"].append(cmd_dict)

    # 添加 links（如果有）
    if record.links:
        links_dict: Dict[str, Any] = {}
        if record.links.ci_run_url:
            links_dict["ci_run_url"] = record.links.ci_run_url
        if record.links.pr_url:
            links_dict["pr_url"] = record.links.pr_url
        if record.links.artifact_url:
            links_dict["artifact_url"] = record.links.artifact_url
        if record.links.regression_doc_url:
            links_dict["regression_doc_url"] = record.links.regression_doc_url
        if links_dict:
            record_dict["links"] = links_dict

    # 添加 notes（如果有）
    if record.notes:
        record_dict["notes"] = record.notes

    # 脱敏处理
    redacted_dict, warnings, redacted_count = redact_sensitive_data(record_dict)

    if redacted_count > 0:
        redacted_dict["redaction_applied"] = True
        redacted_dict["redaction_summary"] = f"检测并脱敏 {redacted_count} 处敏感信息"
        redacted_dict["redaction_rules"] = derive_redaction_rules(warnings)

    # 生成输出文件名（使用 iteration_evidence_naming helper）
    output_path = EVIDENCE_DIR / canonical_evidence_filename(iteration_number)

    if dry_run:
        return RecordResult(
            success=True,
            message=f"[DRY-RUN] 将写入: {output_path}",
            output_path=str(output_path),
            sensitive_warnings=warnings,
            redacted_count=redacted_count,
        )

    # 确保输出目录存在
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

    # 写入 JSON 文件
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(redacted_dict, f, ensure_ascii=False, indent=2)

    return RecordResult(
        success=True,
        message=f"证据已记录: {output_path}",
        output_path=str(output_path),
        sensitive_warnings=warnings,
        redacted_count=redacted_count,
    )


# ============================================================================
# CLI 入口
# ============================================================================


def main() -> int:
    """主函数。"""
    parser = argparse.ArgumentParser(
        description="记录迭代验收证据到版本化目录",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # 基本用法（自动获取当前 commit sha）
    python scripts/iteration/record_iteration_evidence.py 13

    # 指定 commit sha
    python scripts/iteration/record_iteration_evidence.py 13 --commit abc1234

    # 从 JSON 文件读取命令结果
    python scripts/iteration/record_iteration_evidence.py 13 --commands-json .artifacts/acceptance-runs/run_123.json

    # 直接传入命令结果 JSON 字符串
    python scripts/iteration/record_iteration_evidence.py 13 --commands '{"make ci": {"exit_code": 0}}'

    # 指定 CI 运行 URL
    python scripts/iteration/record_iteration_evidence.py 13 --ci-run-url https://github.com/org/repo/actions/runs/123

    # 添加备注
    python scripts/iteration/record_iteration_evidence.py 13 --notes "所有门禁通过，验收完成"

输出格式:
    输出文件为 docs/acceptance/evidence/iteration_<N>_evidence.json（固定文件名策略）
    格式符合 iteration_evidence_v2.schema.json

安全说明:
    脚本会自动检测并脱敏常见敏感信息（PASSWORD/DSN/TOKEN 等）。
    敏感值会被替换为 "[REDACTED]" 占位符。
    输出文件包含 sensitive_data_declaration=true 声明。
        """,
    )
    parser.add_argument(
        "iteration_number",
        type=int,
        help="迭代编号",
    )
    parser.add_argument(
        "--commit",
        "-c",
        type=str,
        default=None,
        help="commit SHA（默认自动获取当前 HEAD）",
    )
    parser.add_argument(
        "--commands",
        type=str,
        default=None,
        help="命令结果 JSON 字符串",
    )
    parser.add_argument(
        "--commands-json",
        type=str,
        default=None,
        help="命令结果 JSON 文件路径（支持 .artifacts/acceptance-runs/*.json 格式）",
    )
    parser.add_argument(
        "--ci-run-url",
        type=str,
        default=None,
        help="CI 运行 URL",
    )
    parser.add_argument(
        "--notes",
        type=str,
        default=None,
        help="补充说明（可选）",
    )
    parser.add_argument(
        "--runner-label",
        type=str,
        default=None,
        help="CI runner 标签（如 'ubuntu-latest', 'self-hosted'）",
    )
    parser.add_argument(
        "--regression-doc-url",
        type=str,
        default=None,
        help="回归文档 URL 或相对路径（默认自动生成 'docs/acceptance/iteration_<N>_regression.md'）",
    )
    parser.add_argument(
        "--no-regression-doc-url",
        action="store_true",
        help="不自动添加 regression_doc_url（默认会自动添加）",
    )
    parser.add_argument(
        "--pr-url",
        type=str,
        default=None,
        help="关联的 Pull Request URL",
    )
    parser.add_argument(
        "--artifact-url",
        type=str,
        default=None,
        help="CI Artifacts 下载 URL（注意：有时效性，通常 90 天）",
    )
    parser.add_argument(
        "--source-type",
        type=str,
        default=None,
        help="证据来源类型（如 manual/ci/automation）",
    )
    parser.add_argument(
        "--source-ref",
        type=str,
        default=None,
        help="证据来源引用标识（可选）",
    )
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="预览模式，不实际写入文件",
    )
    parser.add_argument(
        "--add-command",
        "-a",
        action="append",
        default=[],
        metavar="NAME:COMMAND:RESULT",
        help=(
            "添加单个命令记录，格式: NAME:COMMAND:RESULT（可多次使用）。"
            "NAME 为命令标识符（小写字母开头），COMMAND 为实际命令，RESULT 为 PASS/FAIL/SKIP/ERROR。"
            "示例: --add-command 'lint:make lint:PASS'"
        ),
    )
    parser.add_argument(
        "--add-command-json",
        action="append",
        default=[],
        metavar="JSON",
        help=(
            "添加单个命令记录（JSON 格式，可多次使用）。"
            '示例: --add-command-json \'{"name":"lint","command":"make lint","result":"PASS"}\''
        ),
    )

    args = parser.parse_args()

    # 验证参数
    if args.commands and args.commands_json:
        print("❌ 错误: --commands 和 --commands-json 不能同时使用", file=sys.stderr)
        return 1

    # 获取 commit SHA
    commit_sha = args.commit
    if not commit_sha:
        commit_sha = get_current_commit_sha()
        if not commit_sha:
            print("❌ 错误: 无法获取当前 commit SHA", file=sys.stderr)
            print("    请使用 --commit 参数手动指定", file=sys.stderr)
            return 1

    # 解析命令结果
    commands: List[CommandEntry] = []

    if args.commands_json:
        # 从文件读取
        json_path = Path(args.commands_json)
        if not json_path.exists():
            print(f"❌ 错误: 文件不存在: {json_path}", file=sys.stderr)
            return 1
        try:
            with open(json_path, encoding="utf-8") as f:
                json_data = json.load(f)
            commands = extract_summary_from_acceptance_run(json_data)
        except json.JSONDecodeError as e:
            print(f"❌ 错误: JSON 解析失败: {e}", file=sys.stderr)
            return 1

    elif args.commands:
        # 从参数解析
        try:
            json_data = json.loads(args.commands)
            commands = parse_commands_json(json_data)
        except json.JSONDecodeError as e:
            print(f"❌ 错误: JSON 解析失败: {e}", file=sys.stderr)
            return 1

    # 处理 --add-command 参数（NAME:COMMAND:RESULT 格式）
    for add_cmd in args.add_command:
        parsed_cmd = parse_add_command_arg(add_cmd)
        if parsed_cmd is None:
            print(f"❌ 错误: --add-command 格式错误: {add_cmd}", file=sys.stderr)
            print("    期望格式: NAME:COMMAND:RESULT", file=sys.stderr)
            print("    示例: lint:make lint:PASS", file=sys.stderr)
            return 1
        commands.append(parsed_cmd)

    # 处理 --add-command-json 参数
    for add_cmd_json in args.add_command_json:
        try:
            cmd_data = json.loads(add_cmd_json)
            if not isinstance(cmd_data, dict):
                raise ValueError("必须是 JSON 对象")
            if "name" not in cmd_data or "command" not in cmd_data or "result" not in cmd_data:
                raise ValueError("缺少必需字段: name, command, result")
            # 验证 result 值
            result_str = cmd_data["result"].upper()
            if result_str not in ("PASS", "FAIL", "SKIP", "ERROR"):
                raise ValueError(f"无效的 result 值: {result_str}")
            commands.append(
                CommandEntry(
                    name=normalize_command_name(cmd_data["name"]),
                    command=cmd_data["command"],
                    result=cast(CommandResultType, result_str),
                    summary=cmd_data.get("summary"),
                    duration_seconds=cmd_data.get("duration_seconds"),
                    exit_code=cmd_data.get("exit_code"),
                )
            )
        except (json.JSONDecodeError, ValueError) as e:
            print(f"❌ 错误: --add-command-json 解析失败: {e}", file=sys.stderr)
            print(f"    输入: {add_cmd_json}", file=sys.stderr)
            return 1

    # 如果没有提供命令结果，创建一个空的占位记录
    if not commands:
        commands = [
            CommandEntry(
                name="manual_record",
                command="(manual record)",
                result="PASS",
                summary="手动记录，无命令执行结果",
                exit_code=0,
            )
        ]

    # 记录证据
    try:
        result = record_evidence(
            iteration_number=args.iteration_number,
            commit_sha=commit_sha,
            commands=commands,
            ci_run_url=args.ci_run_url,
            notes=args.notes,
            runner_label=args.runner_label,
            regression_doc_url=args.regression_doc_url,
            pr_url=args.pr_url,
            artifact_url=args.artifact_url,
            source_type=args.source_type,
            source_ref=args.source_ref,
            include_regression_doc_url=not args.no_regression_doc_url,
            dry_run=args.dry_run,
        )
    except SchemaValidationError as e:
        print(f"❌ Schema 验证失败: {e}", file=sys.stderr)
        return 1
    except SensitiveDataError as e:
        print(f"❌ 错误: {e}", file=sys.stderr)
        return 1

    # 输出结果
    if result.sensitive_warnings:
        print("⚠️  检测到敏感信息并已脱敏:", file=sys.stderr)
        for w in result.sensitive_warnings:
            print(f"    - {w.key_path}: {w.reason}", file=sys.stderr)
        print(f"    共脱敏 {result.redacted_count} 处", file=sys.stderr)
        print(file=sys.stderr)

    if args.dry_run:
        print(f"🔍 [DRY-RUN] Iteration {args.iteration_number} 证据预览")
    else:
        print(f"✅ Iteration {args.iteration_number} 证据已记录")

    print()
    print(f"📄 {result.output_path}")
    print()
    print(f"Commit: {commit_sha[:7]}...{commit_sha[-4:]}")
    print(f"命令数: {len(commands)}")
    if args.ci_run_url:
        print(f"CI URL: {args.ci_run_url}")

    if args.dry_run:
        print()
        print("ℹ️  预览模式，未实际写入文件")
        print("    移除 --dry-run 参数以执行实际写入")

    return 0


if __name__ == "__main__":
    sys.exit(main())
