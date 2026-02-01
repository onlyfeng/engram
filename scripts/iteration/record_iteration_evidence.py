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
    5. 输出到 docs/acceptance/evidence/iteration_<N>_<timestamp>.json

安全特性:
    - 检测并拒绝写入常见敏感键（PASSWORD/DSN/TOKEN/SECRET/KEY/CREDENTIAL）
    - 敏感值会被替换为 "[REDACTED]" 占位符
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# 项目根目录
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# 证据输出目录
EVIDENCE_DIR = REPO_ROOT / "docs" / "acceptance" / "evidence"

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
    "command",
    "summary",
    "duration_seconds",
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


# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class SensitiveKeyWarning:
    """检测到的敏感键警告。"""

    key_path: str
    reason: str


@dataclass
class CommandResult:
    """单个命令的执行结果。"""

    command: str
    exit_code: int
    summary: Optional[str] = None
    duration_seconds: Optional[float] = None


@dataclass
class EvidenceRecord:
    """迭代验收证据记录。"""

    iteration_number: int
    commit_sha: str
    timestamp: str
    commands: List[CommandResult]
    ci_run_url: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


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
        result = []
        for i, item in enumerate(data):
            current_path = f"{path}[{i}]"
            redacted_item, sub_warnings, sub_count = redact_sensitive_data(item, current_path)
            result.append(redacted_item)
            warnings.extend(sub_warnings)
            redacted_count += sub_count
        return result, warnings, redacted_count

    elif isinstance(data, str) and is_sensitive_value(data):
        warnings.append(
            SensitiveKeyWarning(
                key_path=path or "(root)",
                reason="值匹配敏感信息模式",
            )
        )
        return REDACTED_PLACEHOLDER, warnings, 1

    return data, warnings, redacted_count


# ============================================================================
# 命令结果解析
# ============================================================================


def parse_commands_json(json_data: Dict[str, Any]) -> List[CommandResult]:
    """解析命令结果 JSON。

    支持两种格式:
    1. 简单格式: {"command": {"exit_code": 0, "summary": "..."}}
    2. 数组格式: [{"command": "...", "exit_code": 0, "summary": "..."}]

    Args:
        json_data: JSON 数据

    Returns:
        CommandResult 列表
    """
    results: List[CommandResult] = []

    if isinstance(json_data, list):
        # 数组格式
        for item in json_data:
            if isinstance(item, dict) and "command" in item:
                results.append(
                    CommandResult(
                        command=item["command"],
                        exit_code=item.get("exit_code", 0),
                        summary=item.get("summary"),
                        duration_seconds=item.get("duration_seconds"),
                    )
                )
    elif isinstance(json_data, dict):
        # 检查是否为简单格式
        for key, value in json_data.items():
            if isinstance(value, dict):
                results.append(
                    CommandResult(
                        command=key,
                        exit_code=value.get("exit_code", 0),
                        summary=value.get("summary"),
                        duration_seconds=value.get("duration_seconds"),
                    )
                )

    return results


def extract_summary_from_acceptance_run(json_data: Dict[str, Any]) -> List[CommandResult]:
    """从 .artifacts/acceptance-runs/*.json 格式提取摘要。

    Args:
        json_data: acceptance run JSON 数据

    Returns:
        CommandResult 列表
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
    commands: List[CommandResult],
    ci_run_url: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    *,
    dry_run: bool = False,
) -> RecordResult:
    """记录迭代验收证据。

    Args:
        iteration_number: 迭代编号
        commit_sha: commit SHA
        commands: 命令执行结果列表
        ci_run_url: CI 运行 URL（可选）
        metadata: 额外元数据（可选）
        dry_run: 是否为预览模式

    Returns:
        RecordResult 操作结果
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 创建证据记录
    record = EvidenceRecord(
        iteration_number=iteration_number,
        commit_sha=commit_sha,
        timestamp=datetime.now().isoformat(),
        commands=commands,
        ci_run_url=ci_run_url,
        metadata=metadata or {},
    )

    # 转换为字典
    record_dict = {
        "iteration_number": record.iteration_number,
        "commit_sha": record.commit_sha,
        "timestamp": record.timestamp,
        "commands": [asdict(cmd) for cmd in record.commands],
        "ci_run_url": record.ci_run_url,
        "metadata": record.metadata,
    }

    # 脱敏处理
    redacted_dict, warnings, redacted_count = redact_sensitive_data(record_dict)

    # 生成输出文件名
    short_sha = get_short_commit_sha(commit_sha)
    output_filename = f"iteration_{iteration_number}_{timestamp}_{short_sha}.json"
    output_path = EVIDENCE_DIR / output_filename

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

安全说明:
    脚本会自动检测并脱敏常见敏感信息（PASSWORD/DSN/TOKEN 等）。
    敏感值会被替换为 "[REDACTED]" 占位符。
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
        "--dry-run",
        "-n",
        action="store_true",
        help="预览模式，不实际写入文件",
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
    commands: List[CommandResult] = []

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

    # 如果没有提供命令结果，创建一个空的占位记录
    if not commands:
        commands = [
            CommandResult(
                command="(manual record)",
                exit_code=0,
                summary="手动记录，无命令执行结果",
            )
        ]

    # 记录证据
    try:
        result = record_evidence(
            iteration_number=args.iteration_number,
            commit_sha=commit_sha,
            commands=commands,
            ci_run_url=args.ci_run_url,
            dry_run=args.dry_run,
        )
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
