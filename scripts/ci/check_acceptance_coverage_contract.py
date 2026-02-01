#!/usr/bin/env python3
"""
Acceptance Coverage Contract Checker

校验 CI 和 Nightly workflow 中的 acceptance 相关覆盖是否符合合约。

功能:
1. 解析 workflow YAML 文件，抽取 run/uses/env/with.path
2. 校验 composed coverage 的"等价性规则"
3. 校验 acceptance 记录与矩阵：必须出现 record_acceptance_run.py 与 render_acceptance_matrix.py
4. 输出 JSON 格式结果（供 CI artifact/调试）

用法:
    python scripts/ci/check_acceptance_coverage_contract.py
    python scripts/ci/check_acceptance_coverage_contract.py --json
    python scripts/ci/check_acceptance_coverage_contract.py --verbose
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

# ============================================================================
# Data Classes
# ============================================================================


@dataclass
class CoverageError:
    """覆盖检查错误"""

    error_type: str  # missing_script, missing_make_target, equivalence_violation, matcher_failed
    category: str  # acceptance_record, acceptance_matrix, equivalence, coverage
    key: str
    message: str
    file: str = ""
    location: str = ""
    expected: Optional[str] = None
    actual: Optional[str] = None


@dataclass
class CoverageWarning:
    """覆盖检查警告"""

    warning_type: str
    category: str
    key: str
    message: str
    file: str = ""
    location: str = ""


@dataclass
class WorkflowExtraction:
    """从 workflow 中提取的信息"""

    run_commands: list[dict[str, Any]] = field(default_factory=list)
    uses_actions: list[dict[str, Any]] = field(default_factory=list)
    env_vars: dict[str, Any] = field(default_factory=dict)
    with_paths: list[dict[str, Any]] = field(default_factory=list)
    make_calls: list[dict[str, Any]] = field(default_factory=list)
    python_scripts: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class CoverageCheckResult:
    """覆盖检查结果"""

    success: bool = True
    errors: list[CoverageError] = field(default_factory=list)
    warnings: list[CoverageWarning] = field(default_factory=list)
    checked_workflows: list[str] = field(default_factory=list)
    ci_extraction: Optional[WorkflowExtraction] = None
    nightly_extraction: Optional[WorkflowExtraction] = None
    acceptance_record_found: bool = False
    acceptance_matrix_found: bool = False
    equivalence_checks: list[dict[str, Any]] = field(default_factory=list)


# ============================================================================
# Matchers - 可维护的等价性规则
# ============================================================================


@dataclass
class Matcher:
    """等价性匹配器基类"""

    name: str
    description: str

    def matches(self, text: str) -> bool:
        """检查文本是否匹配"""
        raise NotImplementedError


@dataclass
class ContainsMatcher(Matcher):
    """包含匹配器"""

    pattern: str

    def matches(self, text: str) -> bool:
        return self.pattern in text


@dataclass
class RegexMatcher(Matcher):
    """正则匹配器"""

    pattern: str
    flags: int = 0

    def matches(self, text: str) -> bool:
        return bool(re.search(self.pattern, text, self.flags))


@dataclass
class StepNameAnchorMatcher(Matcher):
    """Step name 锚点匹配器"""

    step_name: str
    content_pattern: Optional[str] = None

    def matches(self, text: str) -> bool:
        # 这个匹配器用于检查特定 step 名称下的内容
        # 需要在 step 级别使用
        return False

    def matches_step(self, step: dict[str, Any]) -> bool:
        """检查 step 是否匹配"""
        step_name = step.get("name", "")
        if self.step_name.lower() not in step_name.lower():
            return False
        if self.content_pattern:
            run_content = step.get("run", "")
            return bool(re.search(self.content_pattern, run_content))
        return True


# ============================================================================
# Acceptance Coverage Rules
# ============================================================================


# 必须存在的 acceptance 脚本
REQUIRED_ACCEPTANCE_SCRIPTS = [
    "record_acceptance_run.py",
    "render_acceptance_matrix.py",
]

# 对应的 make targets
ACCEPTANCE_MAKE_TARGETS = [
    "acceptance-record",
    "acceptance-matrix",
]

# 等价性规则定义
EQUIVALENCE_RULES = [
    {
        "name": "acceptance_record_coverage",
        "description": "CI 或 Nightly 必须调用 record_acceptance_run.py 或 make acceptance-record",
        "matchers": [
            ContainsMatcher(
                name="script_call",
                description="直接调用 record_acceptance_run.py",
                pattern="record_acceptance_run.py",
            ),
            ContainsMatcher(
                name="make_target",
                description="调用 make acceptance-record",
                pattern="acceptance-record",
            ),
        ],
    },
    {
        "name": "acceptance_matrix_coverage",
        "description": "CI 或 Nightly 必须调用 render_acceptance_matrix.py 或 make acceptance-matrix",
        "matchers": [
            ContainsMatcher(
                name="script_call",
                description="直接调用 render_acceptance_matrix.py",
                pattern="render_acceptance_matrix.py",
            ),
            ContainsMatcher(
                name="make_target",
                description="调用 make acceptance-matrix",
                pattern="acceptance-matrix",
            ),
        ],
    },
]


# ============================================================================
# Workflow Parsing
# ============================================================================


def load_workflow(workflow_path: Path) -> Optional[dict[str, Any]]:
    """加载 workflow YAML 文件"""
    if not workflow_path.exists():
        return None
    try:
        with open(workflow_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except yaml.YAMLError:
        return None


def extract_workflow_info(workflow_data: dict[str, Any], workflow_file: str) -> WorkflowExtraction:
    """
    从 workflow 中提取关键信息。

    提取内容:
    - run_commands: 所有 run 命令
    - uses_actions: 所有 uses 的 actions
    - env_vars: 全局和 job 级别的环境变量
    - with_paths: 所有 with.path 配置
    - make_calls: 所有 make target 调用
    - python_scripts: 所有 Python 脚本调用
    """
    extraction = WorkflowExtraction()

    if not workflow_data:
        return extraction

    # 提取全局环境变量
    global_env = workflow_data.get("env", {})
    if global_env:
        extraction.env_vars.update(global_env)

    jobs = workflow_data.get("jobs", {})
    if not jobs:
        return extraction

    # make 调用模式
    make_pattern = re.compile(r"\bmake\s+([a-zA-Z][a-zA-Z0-9_-]*)")
    # Python 脚本调用模式
    python_pattern = re.compile(r"python\s+(?:-m\s+)?([a-zA-Z0-9_./]+\.py|\S+)")

    for job_id, job_data in jobs.items():
        if not isinstance(job_data, dict):
            continue

        # 提取 job 级别环境变量
        job_env = job_data.get("env", {})
        if job_env:
            for key, value in job_env.items():
                if key not in extraction.env_vars:
                    extraction.env_vars[key] = value

        steps = job_data.get("steps", [])
        for step_index, step in enumerate(steps):
            if not isinstance(step, dict):
                continue

            step_name = step.get("name", "")
            location = f"jobs.{job_id}.steps[{step_index}]"

            # 提取 run 命令
            run_content = step.get("run", "")
            if run_content:
                extraction.run_commands.append(
                    {
                        "content": run_content,
                        "job_id": job_id,
                        "step_name": step_name,
                        "step_index": step_index,
                        "location": location,
                        "file": workflow_file,
                    }
                )

                # 提取 make 调用
                for match in make_pattern.finditer(run_content):
                    target = match.group(1)
                    extraction.make_calls.append(
                        {
                            "target": target,
                            "job_id": job_id,
                            "step_name": step_name,
                            "location": location,
                            "file": workflow_file,
                        }
                    )

                # 提取 Python 脚本调用
                for match in python_pattern.finditer(run_content):
                    script = match.group(1)
                    extraction.python_scripts.append(
                        {
                            "script": script,
                            "job_id": job_id,
                            "step_name": step_name,
                            "location": location,
                            "file": workflow_file,
                        }
                    )

            # 提取 uses actions
            uses = step.get("uses", "")
            if uses:
                extraction.uses_actions.append(
                    {
                        "action": uses,
                        "job_id": job_id,
                        "step_name": step_name,
                        "step_index": step_index,
                        "location": location,
                        "file": workflow_file,
                        "with": step.get("with", {}),
                    }
                )

            # 提取 with.path
            with_block = step.get("with", {})
            if isinstance(with_block, dict) and "path" in with_block:
                path_value = with_block["path"]
                # 解析多行 path
                if isinstance(path_value, str):
                    paths = [p.strip() for p in path_value.strip().split("\n") if p.strip()]
                else:
                    paths = [str(path_value)]

                extraction.with_paths.append(
                    {
                        "paths": paths,
                        "raw_path": path_value,
                        "job_id": job_id,
                        "step_name": step_name,
                        "step_index": step_index,
                        "location": location,
                        "file": workflow_file,
                    }
                )

    return extraction


# ============================================================================
# Coverage Checker
# ============================================================================


class AcceptanceCoverageChecker:
    """Acceptance 覆盖检查器"""

    def __init__(self, workspace_root: Path, verbose: bool = False):
        self.workspace_root = workspace_root
        self.verbose = verbose
        self.result = CoverageCheckResult()

    def log(self, message: str) -> None:
        """输出调试信息"""
        if self.verbose:
            print(f"[DEBUG] {message}")

    def check(self) -> CoverageCheckResult:
        """执行覆盖检查"""
        # 加载 CI workflow
        ci_path = self.workspace_root / ".github" / "workflows" / "ci.yml"
        ci_data = load_workflow(ci_path)
        if ci_data:
            self.result.ci_extraction = extract_workflow_info(ci_data, str(ci_path))
            self.result.checked_workflows.append("ci.yml")
            self.log(f"Loaded ci.yml: {len(self.result.ci_extraction.run_commands)} run commands")
        else:
            self.result.warnings.append(
                CoverageWarning(
                    warning_type="workflow_not_found",
                    category="file",
                    key="ci.yml",
                    message="CI workflow file not found or invalid",
                    file=str(ci_path),
                )
            )

        # 加载 Nightly workflow
        nightly_path = self.workspace_root / ".github" / "workflows" / "nightly.yml"
        nightly_data = load_workflow(nightly_path)
        if nightly_data:
            self.result.nightly_extraction = extract_workflow_info(nightly_data, str(nightly_path))
            self.result.checked_workflows.append("nightly.yml")
            self.log(
                f"Loaded nightly.yml: {len(self.result.nightly_extraction.run_commands)} run commands"
            )
        else:
            self.result.warnings.append(
                CoverageWarning(
                    warning_type="workflow_not_found",
                    category="file",
                    key="nightly.yml",
                    message="Nightly workflow file not found or invalid",
                    file=str(nightly_path),
                )
            )

        # 执行等价性规则检查
        self._check_equivalence_rules()

        # 检查 acceptance 脚本覆盖
        self._check_acceptance_scripts_coverage()

        return self.result

    def _get_all_run_content(self) -> str:
        """获取所有 run 命令的内容（用于全局匹配）"""
        all_content = []

        if self.result.ci_extraction:
            for cmd in self.result.ci_extraction.run_commands:
                all_content.append(cmd.get("content", ""))

        if self.result.nightly_extraction:
            for cmd in self.result.nightly_extraction.run_commands:
                all_content.append(cmd.get("content", ""))

        return "\n".join(all_content)

    def _get_all_make_targets(self) -> set[str]:
        """获取所有 make target 调用"""
        targets = set()

        if self.result.ci_extraction:
            for call in self.result.ci_extraction.make_calls:
                targets.add(call.get("target", ""))

        if self.result.nightly_extraction:
            for call in self.result.nightly_extraction.make_calls:
                targets.add(call.get("target", ""))

        return targets

    def _get_all_python_scripts(self) -> set[str]:
        """获取所有 Python 脚本调用"""
        scripts = set()

        if self.result.ci_extraction:
            for call in self.result.ci_extraction.python_scripts:
                script = call.get("script", "")
                # 提取脚本文件名
                if "/" in script:
                    script = script.split("/")[-1]
                scripts.add(script)

        if self.result.nightly_extraction:
            for call in self.result.nightly_extraction.python_scripts:
                script = call.get("script", "")
                if "/" in script:
                    script = script.split("/")[-1]
                scripts.add(script)

        return scripts

    def _check_equivalence_rules(self) -> None:
        """检查等价性规则"""
        all_run_content = self._get_all_run_content()

        for rule in EQUIVALENCE_RULES:
            rule_name = rule["name"]
            rule_desc = rule["description"]
            matchers = rule["matchers"]

            matched = False
            matched_by = None

            for matcher in matchers:
                if matcher.matches(all_run_content):
                    matched = True
                    matched_by = matcher.name
                    break

            check_result = {
                "rule": rule_name,
                "description": rule_desc,
                "matched": matched,
                "matched_by": matched_by,
            }
            self.result.equivalence_checks.append(check_result)

            if not matched:
                self.log(f"Equivalence rule failed: {rule_name}")
                # 此处不添加 error，因为等价性规则的失败会在脚本覆盖检查中报告

    def _check_acceptance_scripts_coverage(self) -> None:
        """检查 acceptance 脚本覆盖"""
        all_run_content = self._get_all_run_content()
        all_make_targets = self._get_all_make_targets()
        all_python_scripts = self._get_all_python_scripts()

        # 检查 record_acceptance_run.py
        record_found = (
            "record_acceptance_run.py" in all_run_content
            or "record_acceptance_run.py" in all_python_scripts
            or "acceptance-record" in all_make_targets
        )
        self.result.acceptance_record_found = record_found

        if not record_found:
            self.result.errors.append(
                CoverageError(
                    error_type="missing_acceptance_coverage",
                    category="acceptance_record",
                    key="record_acceptance_run.py",
                    message=(
                        "Acceptance record script not found in CI or Nightly workflow. "
                        "Expected: 'record_acceptance_run.py' call or 'make acceptance-record' target."
                    ),
                    expected="record_acceptance_run.py or make acceptance-record",
                    actual="(not found)",
                )
            )
            self.result.success = False

        # 检查 render_acceptance_matrix.py
        matrix_found = (
            "render_acceptance_matrix.py" in all_run_content
            or "render_acceptance_matrix.py" in all_python_scripts
            or "acceptance-matrix" in all_make_targets
        )
        self.result.acceptance_matrix_found = matrix_found

        if not matrix_found:
            self.result.errors.append(
                CoverageError(
                    error_type="missing_acceptance_coverage",
                    category="acceptance_matrix",
                    key="render_acceptance_matrix.py",
                    message=(
                        "Acceptance matrix script not found in CI or Nightly workflow. "
                        "Expected: 'render_acceptance_matrix.py' call or 'make acceptance-matrix' target."
                    ),
                    expected="render_acceptance_matrix.py or make acceptance-matrix",
                    actual="(not found)",
                )
            )
            self.result.success = False


# ============================================================================
# Output Formatters
# ============================================================================


def format_text_output(result: CoverageCheckResult) -> str:
    """格式化文本输出"""
    lines = []

    lines.append("=" * 60)
    lines.append("Acceptance Coverage Contract Check Report")
    lines.append("=" * 60)
    lines.append("")

    # Summary
    status = "PASSED" if result.success else "FAILED"
    lines.append(f"Status: {status}")
    lines.append(f"Checked workflows: {', '.join(result.checked_workflows) or 'none'}")
    lines.append(f"Acceptance record found: {result.acceptance_record_found}")
    lines.append(f"Acceptance matrix found: {result.acceptance_matrix_found}")
    lines.append(f"Errors: {len(result.errors)}")
    lines.append(f"Warnings: {len(result.warnings)}")
    lines.append("")

    # Equivalence checks
    if result.equivalence_checks:
        lines.append("-" * 60)
        lines.append("Equivalence Checks")
        lines.append("-" * 60)
        for check in result.equivalence_checks:
            status_icon = "✓" if check["matched"] else "✗"
            matched_info = f" (via {check['matched_by']})" if check["matched_by"] else ""
            lines.append(f"  {status_icon} {check['rule']}: {check['description']}{matched_info}")
        lines.append("")

    # Errors
    if result.errors:
        lines.append("-" * 60)
        lines.append("ERRORS")
        lines.append("-" * 60)
        for error in result.errors:
            lines.append("")
            lines.append(f"  [{error.error_type}] {error.category}")
            lines.append(f"  Key: {error.key}")
            lines.append(f"  Message: {error.message}")
            if error.expected:
                lines.append(f"  Expected: {error.expected}")
            if error.actual:
                lines.append(f"  Actual: {error.actual}")
        lines.append("")

    # Warnings
    if result.warnings:
        lines.append("-" * 60)
        lines.append("WARNINGS")
        lines.append("-" * 60)
        for warning in result.warnings:
            lines.append("")
            lines.append(f"  [{warning.warning_type}] {warning.category}")
            lines.append(f"  Key: {warning.key}")
            lines.append(f"  Message: {warning.message}")
        lines.append("")

    lines.append("=" * 60)
    return "\n".join(lines)


def format_json_output(result: CoverageCheckResult) -> str:
    """格式化 JSON 输出"""

    def extraction_to_dict(extraction: Optional[WorkflowExtraction]) -> Optional[dict[str, Any]]:
        if extraction is None:
            return None
        return {
            "run_commands_count": len(extraction.run_commands),
            "uses_actions_count": len(extraction.uses_actions),
            "env_vars_count": len(extraction.env_vars),
            "with_paths_count": len(extraction.with_paths),
            "make_calls": [c["target"] for c in extraction.make_calls],
            "python_scripts": [c["script"] for c in extraction.python_scripts],
        }

    output = {
        "success": result.success,
        "checked_workflows": result.checked_workflows,
        "acceptance_record_found": result.acceptance_record_found,
        "acceptance_matrix_found": result.acceptance_matrix_found,
        "error_count": len(result.errors),
        "warning_count": len(result.warnings),
        "equivalence_checks": result.equivalence_checks,
        "ci_extraction": extraction_to_dict(result.ci_extraction),
        "nightly_extraction": extraction_to_dict(result.nightly_extraction),
        "errors": [
            {
                "error_type": e.error_type,
                "category": e.category,
                "key": e.key,
                "message": e.message,
                "file": e.file,
                "location": e.location,
                "expected": e.expected,
                "actual": e.actual,
            }
            for e in result.errors
        ],
        "warnings": [
            {
                "warning_type": w.warning_type,
                "category": w.category,
                "key": w.key,
                "message": w.message,
                "file": w.file,
                "location": w.location,
            }
            for w in result.warnings
        ],
    }
    return json.dumps(output, indent=2, ensure_ascii=False)


# ============================================================================
# Main
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check acceptance coverage contract in CI/Nightly workflows"
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root directory (default: current directory)",
    )
    parser.add_argument("--json", action="store_true", help="Output in JSON format")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")
    parser.add_argument("--output", type=Path, help="Write JSON output to file")

    args = parser.parse_args()

    # 执行检查
    checker = AcceptanceCoverageChecker(args.workspace.resolve(), verbose=args.verbose)
    result = checker.check()

    # 输出结果
    if args.json:
        output = format_json_output(result)
        print(output)
    else:
        output = format_text_output(result)
        print(output)

    # 写入文件（如果指定）
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(format_json_output(result))
        if args.verbose:
            print(f"[INFO] JSON output written to: {args.output}")

    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
