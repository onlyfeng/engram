#!/usr/bin/env python3
"""
Workflow Make Targets 一致性检查

解析 .github/workflows/*.yml 中的 `run: make ...` 命令，提取 make targets，
然后与 Makefile 的 .PHONY/定义目标和 workflow_contract.v1.json 的 make.targets_required 比对。

检查项：
1. workflow 中使用的 make target 必须在 Makefile 中定义
2. workflow_contract.v1.json 的 make.targets_required 必须在 Makefile 中定义
3. 报告 workflow 中使用但未在 contract 中声明的 targets（警告）

用法:
    python scripts/ci/check_workflow_make_targets_consistency.py [--verbose] [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scripts.ci.workflow_contract_common import (
    MakeTargetUsage,
    extract_make_targets_from_workflow,
)

# ============================================================================
# Error Types
# ============================================================================


class ErrorTypes:
    """错误类型常量"""

    UNKNOWN_MAKE_TARGET = "unknown_make_target"
    CONTRACT_TARGET_NOT_IN_MAKEFILE = "contract_target_not_in_makefile"
    WORKFLOW_TARGET_NOT_IN_CONTRACT = "workflow_target_not_in_contract"
    PARSE_ERROR = "parse_error"


class WarningTypes:
    """警告类型常量"""

    TARGET_NOT_IN_CONTRACT = "target_not_in_contract"
    VARIABLE_IN_TARGET = "variable_in_target"


# ============================================================================
# Data Classes
# ============================================================================


@dataclass
class CheckError:
    """检查错误"""

    error_type: str
    message: str
    target: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckWarning:
    """检查警告"""

    warning_type: str
    message: str
    target: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckResult:
    """检查结果"""

    errors: list[CheckError] = field(default_factory=list)
    warnings: list[CheckWarning] = field(default_factory=list)
    makefile_targets: set[str] = field(default_factory=set)
    workflow_targets: list[MakeTargetUsage] = field(default_factory=list)
    contract_targets: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return len(self.errors) == 0


# ============================================================================
# Makefile Parser
# ============================================================================


def parse_makefile_targets(makefile_path: Path) -> set[str]:
    """
    解析 Makefile 中定义的目标

    提取：
    1. .PHONY 声明的目标
    2. 规则定义的目标（target: [prerequisites]）

    Args:
        makefile_path: Makefile 文件路径

    Returns:
        定义的目标集合
    """
    targets: set[str] = set()

    if not makefile_path.exists():
        return targets

    content = makefile_path.read_text(encoding="utf-8")

    # 解析 .PHONY 行
    # 格式: .PHONY: target1 target2 ...
    phony_pattern = re.compile(r"^\.PHONY:\s*(.+)$", re.MULTILINE)
    for match in phony_pattern.finditer(content):
        phony_line = match.group(1)
        # 拆分目标（考虑续行）
        phony_targets = phony_line.replace("\\", " ").split()
        targets.update(phony_targets)

    # 解析规则定义
    # 格式: target: [prerequisites]
    # 排除 .PHONY, 变量赋值 (:=, ?=, +=), 特殊目标 (.DEFAULT_GOAL)
    rule_pattern = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_-]*):\s*", re.MULTILINE)
    for match in rule_pattern.finditer(content):
        target = match.group(1)
        # 排除特殊目标
        if not target.startswith(".") and target not in ("PHONY",):
            targets.add(target)

    return targets


# ============================================================================
# Contract Parser
# ============================================================================


def load_contract_make_targets(contract_path: Path) -> list[str]:
    """
    从 workflow contract 中加载 make.targets_required

    Args:
        contract_path: contract 文件路径

    Returns:
        targets_required 列表
    """
    if not contract_path.exists():
        return []

    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        targets: list[str] = contract.get("make", {}).get("targets_required", [])
        return targets
    except (json.JSONDecodeError, KeyError):
        return []


# ============================================================================
# Checker
# ============================================================================


class WorkflowMakeTargetsChecker:
    """Workflow Make Targets 一致性检查器"""

    def __init__(
        self,
        workspace: Path,
        makefile_path: Path | None = None,
        contract_path: Path | None = None,
        workflow_dir: Path | None = None,
    ):
        self.workspace = workspace
        self.makefile_path = makefile_path or workspace / "Makefile"
        self.contract_path = contract_path or workspace / "scripts" / "ci" / "workflow_contract.v1.json"
        self.workflow_dir = workflow_dir or workspace / ".github" / "workflows"

    def check(self) -> CheckResult:
        """执行检查"""
        result = CheckResult()

        # 1. 解析 Makefile 目标
        result.makefile_targets = parse_makefile_targets(self.makefile_path)

        # 2. 解析 workflow 中的 make 使用
        for workflow_file in self.workflow_dir.glob("*.yml"):
            usages = extract_make_targets_from_workflow(workflow_file)
            result.workflow_targets.extend(usages)

        # 3. 加载 contract 的 targets_required
        result.contract_targets = load_contract_make_targets(self.contract_path)

        # 4. 检查 workflow 中使用的 targets 是否在 Makefile 中定义
        seen_targets: set[str] = set()
        for usage in result.workflow_targets:
            if usage.target in seen_targets:
                continue
            seen_targets.add(usage.target)

            if usage.target not in result.makefile_targets:
                result.errors.append(
                    CheckError(
                        error_type=ErrorTypes.UNKNOWN_MAKE_TARGET,
                        message=f"Workflow uses undefined make target '{usage.target}'",
                        target=usage.target,
                        context={
                            "workflow_file": usage.workflow_file,
                            "job_id": usage.job_id,
                            "step_name": usage.step_name,
                        },
                    )
                )

        # 5. 检查 contract 的 targets_required 是否在 Makefile 中定义
        for target in result.contract_targets:
            if target not in result.makefile_targets:
                result.errors.append(
                    CheckError(
                        error_type=ErrorTypes.CONTRACT_TARGET_NOT_IN_MAKEFILE,
                        message=f"Contract requires undefined make target '{target}'",
                        target=target,
                        context={"contract_file": self.contract_path.name},
                    )
                )

        # 6. 警告：workflow 中使用但未在 contract 中声明的 targets
        contract_targets_set = set(result.contract_targets)
        workflow_unique_targets = {u.target for u in result.workflow_targets}
        for target in workflow_unique_targets:
            if target not in contract_targets_set and target in result.makefile_targets:
                # 只对存在于 Makefile 但不在 contract 中的发出警告
                result.warnings.append(
                    CheckWarning(
                        warning_type=WarningTypes.TARGET_NOT_IN_CONTRACT,
                        message=f"Workflow uses make target '{target}' not declared in contract",
                        target=target,
                        context={},
                    )
                )

        return result


# ============================================================================
# Output Formatting
# ============================================================================


def format_result_text(result: CheckResult, verbose: bool = False) -> str:
    """格式化文本输出"""
    lines: list[str] = []

    lines.append("=" * 60)
    lines.append("Workflow Make Targets Consistency Check")
    lines.append("=" * 60)
    lines.append("")

    # 统计信息
    lines.append(f"Makefile targets: {len(result.makefile_targets)}")
    lines.append(f"Workflow make usages: {len(result.workflow_targets)}")
    lines.append(f"Contract required targets: {len(result.contract_targets)}")
    lines.append("")

    # 错误
    if result.errors:
        lines.append(f"Errors ({len(result.errors)}):")
        lines.append("-" * 40)
        for error in result.errors:
            lines.append(f"  [{error.error_type}] {error.message}")
            if verbose and error.context:
                for key, value in error.context.items():
                    lines.append(f"    {key}: {value}")
        lines.append("")

    # 警告
    if result.warnings:
        lines.append(f"Warnings ({len(result.warnings)}):")
        lines.append("-" * 40)
        for warning in result.warnings:
            lines.append(f"  [{warning.warning_type}] {warning.message}")
        lines.append("")

    # 结果
    if result.passed:
        lines.append("[OK] All checks passed")
    else:
        lines.append(f"[FAILED] {len(result.errors)} error(s) found")

    return "\n".join(lines)


def format_result_json(result: CheckResult) -> str:
    """格式化 JSON 输出"""
    data = {
        "passed": result.passed,
        "summary": {
            "makefile_targets_count": len(result.makefile_targets),
            "workflow_usages_count": len(result.workflow_targets),
            "contract_targets_count": len(result.contract_targets),
            "error_count": len(result.errors),
            "warning_count": len(result.warnings),
        },
        "errors": [
            {
                "error_type": e.error_type,
                "message": e.message,
                "target": e.target,
                "context": e.context,
            }
            for e in result.errors
        ],
        "warnings": [
            {
                "warning_type": w.warning_type,
                "message": w.message,
                "target": w.target,
                "context": w.context,
            }
            for w in result.warnings
        ],
        "makefile_targets": sorted(result.makefile_targets),
        "contract_targets": result.contract_targets,
        "workflow_usages": [
            {
                "target": u.target,
                "workflow_file": u.workflow_file,
                "job_id": u.job_id,
                "step_name": u.step_name,
            }
            for u in result.workflow_targets
        ],
    }
    return json.dumps(data, indent=2, ensure_ascii=False)


# ============================================================================
# Main
# ============================================================================


def main() -> int:
    """主函数"""
    parser = argparse.ArgumentParser(
        description="Check workflow make targets consistency with Makefile and contract"
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root directory (default: current directory)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed error context",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output in JSON format",
    )

    args = parser.parse_args()

    checker = WorkflowMakeTargetsChecker(workspace=args.workspace)
    result = checker.check()

    if args.json:
        print(format_result_json(result))
    else:
        print(format_result_text(result, verbose=args.verbose))

    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
