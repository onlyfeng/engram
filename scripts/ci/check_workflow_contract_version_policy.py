#!/usr/bin/env python3
"""
Workflow Contract 版本策略检查脚本

当关键文件变更时，强制要求：
1. workflow_contract.v1.json 的 version 字段已更新
2. workflow_contract.v1.json 的 last_updated 字段已更新
3. contract.md 版本控制表（第 13 章）包含该版本

关键文件（变更时触发版本更新检查）：
- .github/workflows/ci.yml
- .github/workflows/nightly.yml
- scripts/ci/workflow_contract.v1.json
- Makefile（仅当变更涉及 workflow/CI 相关目标时）
- docs/ci_nightly_workflow_refactor/*.md

工具脚本关键文件（变更影响合约执行逻辑）：
- scripts/ci/validate_workflows.py
- scripts/ci/workflow_contract.v1.schema.json
- scripts/ci/check_workflow_contract_docs_sync.py
- scripts/ci/workflow_contract_drift_report.py
- scripts/ci/generate_workflow_contract_snapshot.py

使用方式：
    # 基于 git diff 检测变更（默认比较 HEAD~1）
    python scripts/ci/check_workflow_contract_version_policy.py

    # 比较指定 commit
    python scripts/ci/check_workflow_contract_version_policy.py --base HEAD~3

    # 使用 PR 模式（检测 PR 中的所有变更）
    python scripts/ci/check_workflow_contract_version_policy.py --pr-mode

    # 指定变更文件列表（用于测试或 CI 集成）
    python scripts/ci/check_workflow_contract_version_policy.py --changed-files file1.yml file2.json

    # JSON 输出
    python scripts/ci/check_workflow_contract_version_policy.py --json

退出码：
    0: 检查通过（无关键变更，或版本已正确更新）
    1: 检查失败（关键变更但版本未更新）
    2: 文件读取/解析错误
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

# ============================================================================
# Constants
# ============================================================================

DEFAULT_CONTRACT_PATH = "scripts/ci/workflow_contract.v1.json"
DEFAULT_DOC_PATH = "docs/ci_nightly_workflow_refactor/contract.md"

# 关键文件列表：这些文件变更时需要检查版本策略
CRITICAL_FILES = [
    ".github/workflows/ci.yml",
    ".github/workflows/nightly.yml",
    "scripts/ci/workflow_contract.v1.json",
]

# 扩展关键文件模式（使用 glob 匹配）
CRITICAL_FILE_PATTERNS = [
    r"^\.github/workflows/(ci|nightly)\.yml$",
    r"^scripts/ci/workflow_contract\.v\d+\.json$",
    r"^docs/ci_nightly_workflow_refactor/.*\.md$",
]

# 工具脚本关键文件模式（变更时需要更新 contract 版本）
# - 校验脚本变更影响合约执行逻辑
# - Schema 变更影响合约校验规则
# 版本策略：
#   Major: 校验逻辑不兼容变更（如删除校验规则、修改错误码含义）
#   Minor: 新增校验功能、新增错误类型
#   Patch: 修复 bug、优化性能、完善错误提示
CRITICAL_TOOLING_PATTERNS = [
    r"^scripts/ci/validate_workflows\.py$",  # 合约校验器核心脚本
    r"^scripts/ci/workflow_contract\.v\d+\.schema\.json$",  # 合约 JSON Schema
    r"^scripts/ci/check_workflow_contract_docs_sync\.py$",  # 文档同步校验脚本
    r"^scripts/ci/workflow_contract_drift_report\.py$",  # 漂移报告生成脚本
    r"^scripts/ci/generate_workflow_contract_snapshot\.py$",  # 快照生成脚本
]

# Makefile 中与 workflow/CI 相关的目标关键字
# 只有当 Makefile 变更涉及这些关键字时才触发版本检查
MAKEFILE_CI_KEYWORDS = [
    "validate-workflows",
    "check-workflow",
    "ci:",
    "workflow-contract",
]


# ============================================================================
# Data Classes
# ============================================================================


@dataclass
class VersionError:
    """版本策略错误"""

    error_type: str
    message: str
    suggestion: str


@dataclass
class VersionCheckResult:
    """版本检查结果"""

    success: bool = True
    errors: list[VersionError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    changed_critical_files: list[str] = field(default_factory=list)
    contract_version: str = ""
    contract_last_updated: str = ""
    doc_versions: list[str] = field(default_factory=list)
    version_updated: bool = False
    last_updated_updated: bool = False
    version_in_doc: bool = False

    def add_error(self, error: VersionError) -> None:
        """添加错误"""
        self.errors.append(error)
        self.success = False

    def add_warning(self, warning: str) -> None:
        """添加警告"""
        self.warnings.append(warning)


# ============================================================================
# Git Operations
# ============================================================================


def get_git_diff_files(base: str = "HEAD~1") -> list[str]:
    """获取 git diff 中变更的文件列表

    Args:
        base: 比较基准（默认 HEAD~1）

    Returns:
        变更文件路径列表
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", base, "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        files = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
        return files
    except subprocess.CalledProcessError:
        # 如果 git diff 失败（如首次提交），尝试列出所有已暂存文件
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", "--cached"],
                capture_output=True,
                text=True,
                check=True,
            )
            files = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
            return files
        except subprocess.CalledProcessError:
            return []


def get_pr_changed_files() -> list[str]:
    """获取 PR 中所有变更的文件列表（从 merge-base 开始）

    Returns:
        变更文件路径列表
    """
    try:
        # 找到与 main/master 分支的 merge-base
        for main_branch in ["origin/main", "origin/master", "main", "master"]:
            result = subprocess.run(
                ["git", "merge-base", main_branch, "HEAD"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                merge_base = result.stdout.strip()
                break
        else:
            # 找不到 main/master，回退到 HEAD~10
            merge_base = "HEAD~10"

        result = subprocess.run(
            ["git", "diff", "--name-only", merge_base, "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        files = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
        return files
    except subprocess.CalledProcessError:
        return []


def get_file_diff_content(file_path: str, base: str = "HEAD~1") -> str | None:
    """获取指定文件的 diff 内容

    Args:
        file_path: 文件路径
        base: 比较基准

    Returns:
        diff 内容，如果文件未变更则返回 None
    """
    try:
        result = subprocess.run(
            ["git", "diff", base, "HEAD", "--", file_path],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout if result.stdout.strip() else None
    except subprocess.CalledProcessError:
        return None


def get_old_file_content(file_path: str, base: str = "HEAD~1") -> str | None:
    """获取文件在 base commit 时的内容

    Args:
        file_path: 文件路径
        base: commit 引用

    Returns:
        文件内容，如果文件不存在则返回 None
    """
    try:
        result = subprocess.run(
            ["git", "show", f"{base}:{file_path}"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout
    except subprocess.CalledProcessError:
        return None


# ============================================================================
# File Pattern Matching
# ============================================================================


def is_critical_file(file_path: str) -> bool:
    """检查文件是否为关键文件

    Args:
        file_path: 文件路径

    Returns:
        是否为关键文件
    """
    # 精确匹配
    if file_path in CRITICAL_FILES:
        return True

    # 模式匹配（核心合约文件）
    for pattern in CRITICAL_FILE_PATTERNS:
        if re.match(pattern, file_path):
            return True

    # 模式匹配（工具脚本文件）
    for pattern in CRITICAL_TOOLING_PATTERNS:
        if re.match(pattern, file_path):
            return True

    return False


def is_makefile_ci_related_change(diff_content: str) -> bool:
    """检查 Makefile 变更是否涉及 CI/workflow 相关目标

    Args:
        diff_content: Makefile 的 diff 内容

    Returns:
        是否涉及 CI/workflow 相关变更
    """
    if not diff_content:
        return False

    for keyword in MAKEFILE_CI_KEYWORDS:
        if keyword in diff_content:
            return True

    return False


def filter_critical_files(
    changed_files: list[str],
    makefile_diff: str | None = None,
) -> list[str]:
    """过滤出关键文件列表

    Args:
        changed_files: 所有变更文件列表
        makefile_diff: Makefile 的 diff 内容（可选）

    Returns:
        关键文件列表
    """
    critical: list[str] = []

    for file_path in changed_files:
        # Makefile 特殊处理：只有 CI 相关变更才算关键
        if file_path == "Makefile":
            if makefile_diff and is_makefile_ci_related_change(makefile_diff):
                critical.append(file_path)
            continue

        if is_critical_file(file_path):
            critical.append(file_path)

    return critical


# ============================================================================
# Version Comparison
# ============================================================================


def parse_semver(version: str) -> tuple[int, int, int] | None:
    """解析 SemVer 版本号

    Args:
        version: 版本字符串（如 "2.6.0"）

    Returns:
        (major, minor, patch) 元组，解析失败返回 None
    """
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)$", version)
    if match:
        return (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    return None


def is_version_updated(old_version: str, new_version: str) -> bool:
    """检查版本是否已更新（新版本大于旧版本）

    Args:
        old_version: 旧版本
        new_version: 新版本

    Returns:
        新版本是否大于旧版本
    """
    old_semver = parse_semver(old_version)
    new_semver = parse_semver(new_version)

    if old_semver is None or new_semver is None:
        # 如果无法解析，使用字符串比较
        return new_version != old_version

    return new_semver > old_semver


def is_date_current(date_str: str) -> bool:
    """检查日期是否为当前日期

    Args:
        date_str: 日期字符串（格式 YYYY-MM-DD）

    Returns:
        是否为当前日期
    """
    today = date.today().isoformat()
    return date_str == today


# ============================================================================
# Core Logic
# ============================================================================


class WorkflowContractVersionChecker:
    """Workflow Contract 版本策略检查器"""

    def __init__(
        self,
        project_root: Path,
        contract_path: str = DEFAULT_CONTRACT_PATH,
        doc_path: str = DEFAULT_DOC_PATH,
        verbose: bool = False,
    ) -> None:
        self.project_root = project_root
        self.contract_path = project_root / contract_path
        self.doc_path = project_root / doc_path
        self.verbose = verbose
        self.result = VersionCheckResult()
        self.contract: dict[str, Any] = {}
        self.doc_content: str = ""

    def _get_merge_base(self) -> str:
        """获取与 main/master 分支的 merge-base commit

        Returns:
            merge-base commit hash，找不到时返回 HEAD~1
        """
        try:
            for main_branch in ["origin/main", "origin/master", "main", "master"]:
                result = subprocess.run(
                    ["git", "merge-base", main_branch, "HEAD"],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    return result.stdout.strip()
        except Exception:
            pass
        return "HEAD~1"

    def load_contract(self) -> bool:
        """加载 contract JSON 文件"""
        if not self.contract_path.exists():
            self.result.add_error(
                VersionError(
                    error_type="contract_not_found",
                    message=f"Contract file not found: {self.contract_path}",
                    suggestion="Ensure workflow_contract.v1.json exists",
                )
            )
            return False

        try:
            with open(self.contract_path, "r", encoding="utf-8") as f:
                self.contract = json.load(f)
            self.result.contract_version = self.contract.get("version", "")
            self.result.contract_last_updated = self.contract.get("last_updated", "")
            return True
        except json.JSONDecodeError as e:
            self.result.add_error(
                VersionError(
                    error_type="contract_parse_error",
                    message=f"Failed to parse contract JSON: {e}",
                    suggestion="Fix JSON syntax in workflow_contract.v1.json",
                )
            )
            return False

    def load_doc(self) -> bool:
        """加载文档文件"""
        if not self.doc_path.exists():
            self.result.add_error(
                VersionError(
                    error_type="doc_not_found",
                    message=f"Documentation file not found: {self.doc_path}",
                    suggestion="Ensure contract.md exists",
                )
            )
            return False

        try:
            with open(self.doc_path, "r", encoding="utf-8") as f:
                self.doc_content = f.read()
            return True
        except Exception as e:
            self.result.add_error(
                VersionError(
                    error_type="doc_read_error",
                    message=f"Failed to read documentation file: {e}",
                    suggestion="Check file permissions",
                )
            )
            return False

    def extract_doc_versions(self) -> list[str]:
        """从文档版本控制表中提取版本号列表

        文档第 13 章的版本控制表格式：
        | 版本 | 日期 | 变更说明 |
        |------|------|----------|
        | v2.7.1 | 2026-02-02 | ... |

        Returns:
            版本号列表（如 ["2.7.1", "2.7.0", ...]）
        """
        versions: list[str] = []

        # 匹配版本控制表中的版本号
        # 格式：| v2.7.1 | 或 | 2.7.1 |
        pattern = r"\|\s*v?(\d+\.\d+\.\d+)\s*\|"
        matches = re.findall(pattern, self.doc_content)
        versions.extend(matches)

        self.result.doc_versions = versions
        return versions

    def check_version_in_doc(self, version: str) -> bool:
        """检查版本是否在文档版本控制表中

        Args:
            version: 版本号

        Returns:
            版本是否在文档中
        """
        doc_versions = self.extract_doc_versions()
        # 支持 v2.7.1 和 2.7.1 两种格式
        version_normalized = version.lstrip("v")
        return version_normalized in doc_versions

    def check_version_updated(self, base: str) -> bool:
        """检查 version 字段是否已更新

        Args:
            base: 比较基准 commit

        Returns:
            版本是否已更新
        """
        contract_rel_path = str(self.contract_path.relative_to(self.project_root))
        old_content = get_old_file_content(contract_rel_path, base)

        if old_content is None:
            # 新文件，视为已更新
            self.result.version_updated = True
            return True

        try:
            old_contract = json.loads(old_content)
            old_version = old_contract.get("version", "")
            new_version = self.result.contract_version

            if is_version_updated(old_version, new_version):
                self.result.version_updated = True
                if self.verbose:
                    print(f"  Version updated: {old_version} -> {new_version}")
                return True
            else:
                if self.verbose:
                    print(f"  Version NOT updated: {old_version} == {new_version}")
                return False
        except json.JSONDecodeError:
            # 旧文件无法解析，视为已更新
            self.result.version_updated = True
            return True

    def check_last_updated_updated(self, base: str) -> bool:
        """检查 last_updated 字段是否已更新

        Args:
            base: 比较基准 commit

        Returns:
            last_updated 是否已更新
        """
        contract_rel_path = str(self.contract_path.relative_to(self.project_root))
        old_content = get_old_file_content(contract_rel_path, base)

        if old_content is None:
            # 新文件，视为已更新
            self.result.last_updated_updated = True
            return True

        try:
            old_contract = json.loads(old_content)
            old_date = old_contract.get("last_updated", "")
            new_date = self.result.contract_last_updated

            if new_date != old_date:
                self.result.last_updated_updated = True
                if self.verbose:
                    print(f"  last_updated updated: {old_date} -> {new_date}")
                return True
            else:
                if self.verbose:
                    print(f"  last_updated NOT updated: {old_date}")
                return False
        except json.JSONDecodeError:
            # 旧文件无法解析，视为已更新
            self.result.last_updated_updated = True
            return True

    def check(
        self,
        changed_files: list[str] | None = None,
        base: str = "HEAD~1",
        pr_mode: bool = False,
    ) -> VersionCheckResult:
        """执行版本策略检查

        Args:
            changed_files: 变更文件列表（可选，不提供则从 git diff 获取）
            base: 比较基准 commit
            pr_mode: 是否为 PR 模式（从 merge-base 开始比较）

        Returns:
            检查结果
        """
        # 1. 获取变更文件列表
        # 在 PR 模式下，同时更新 base 为 merge-base，确保版本比较一致
        if changed_files is None:
            if pr_mode:
                changed_files = get_pr_changed_files()
                # 更新 base 为 merge-base，确保版本比较与变更检测使用同一基准
                base = self._get_merge_base()
                if self.verbose:
                    print(f"Using PR mode: comparing from merge-base ({base})")
            else:
                changed_files = get_git_diff_files(base)

        if self.verbose:
            print(f"Changed files ({len(changed_files)}):")
            for f in changed_files:
                print(f"  - {f}")
            print()

        # 2. 过滤关键文件
        makefile_diff = None
        if "Makefile" in changed_files:
            makefile_diff = get_file_diff_content("Makefile", base)

        critical_files = filter_critical_files(changed_files, makefile_diff)
        self.result.changed_critical_files = critical_files

        if self.verbose:
            print(f"Critical files ({len(critical_files)}):")
            for f in critical_files:
                print(f"  - {f}")
            print()

        # 3. 如果没有关键文件变更，直接通过
        if not critical_files:
            if self.verbose:
                print("No critical files changed. Version policy check SKIPPED.")
            return self.result

        # 4. 加载 contract 和文档
        if self.verbose:
            print(f"Loading contract: {self.contract_path}")

        if not self.load_contract():
            return self.result

        if self.verbose:
            print(f"Loading documentation: {self.doc_path}")

        if not self.load_doc():
            return self.result

        # 5. 检查 version 是否更新
        if self.verbose:
            print("\nChecking version field...")

        version_updated = self.check_version_updated(base)

        if not version_updated:
            self.result.add_error(
                VersionError(
                    error_type="version_not_updated",
                    message=(
                        f"Critical files changed but 'version' field not updated in "
                        f"workflow_contract.v1.json (current: {self.result.contract_version})"
                    ),
                    suggestion=(
                        "Update 'version' field according to SemVer policy: "
                        "Major for breaking changes, Minor for new features, Patch for fixes"
                    ),
                )
            )

        # 6. 检查 last_updated 是否更新
        if self.verbose:
            print("\nChecking last_updated field...")

        last_updated_updated = self.check_last_updated_updated(base)

        if not last_updated_updated:
            self.result.add_error(
                VersionError(
                    error_type="last_updated_not_updated",
                    message=(
                        f"Critical files changed but 'last_updated' field not updated in "
                        f"workflow_contract.v1.json (current: {self.result.contract_last_updated})"
                    ),
                    suggestion=f"Update 'last_updated' to today's date: {date.today().isoformat()}",
                )
            )

        # 7. 检查版本是否在文档版本控制表中
        if self.verbose:
            print("\nChecking version in documentation...")

        version_in_doc = self.check_version_in_doc(self.result.contract_version)
        self.result.version_in_doc = version_in_doc

        if not version_in_doc:
            self.result.add_error(
                VersionError(
                    error_type="version_not_in_doc",
                    message=(
                        f"Version '{self.result.contract_version}' not found in "
                        f"contract.md version control table (Section 13)"
                    ),
                    suggestion=(
                        "Add a new row to the version control table in contract.md:\n"
                        f"| v{self.result.contract_version} | {date.today().isoformat()} | <变更说明> |"
                    ),
                )
            )

        return self.result


# ============================================================================
# Output Formatting
# ============================================================================


def format_text_output(result: VersionCheckResult) -> str:
    """格式化文本输出"""
    lines: list[str] = []

    if result.success:
        lines.append("=" * 60)
        lines.append("Workflow Contract Version Policy Check: PASSED")
        lines.append("=" * 60)
    else:
        lines.append("=" * 60)
        lines.append("Workflow Contract Version Policy Check: FAILED")
        lines.append("=" * 60)

    lines.append("")
    lines.append("Summary:")
    lines.append(f"  - Critical files changed: {len(result.changed_critical_files)}")
    if result.changed_critical_files:
        for f in result.changed_critical_files:
            lines.append(f"      - {f}")
    lines.append(f"  - Contract version: {result.contract_version or '(not loaded)'}")
    lines.append(f"  - Contract last_updated: {result.contract_last_updated or '(not loaded)'}")
    lines.append(f"  - Version updated: {'Yes' if result.version_updated else 'No'}")
    lines.append(f"  - last_updated updated: {'Yes' if result.last_updated_updated else 'No'}")
    lines.append(f"  - Version in doc: {'Yes' if result.version_in_doc else 'No'}")
    lines.append(f"  - Errors: {len(result.errors)}")
    lines.append(f"  - Warnings: {len(result.warnings)}")

    if result.errors:
        lines.append("")
        lines.append("ERRORS:")
        for error in result.errors:
            lines.append(f"  [{error.error_type}]")
            lines.append(f"    {error.message}")
            lines.append(f"    Suggestion: {error.suggestion}")

    if result.warnings:
        lines.append("")
        lines.append("WARNINGS:")
        for warning in result.warnings:
            lines.append(f"  {warning}")

    return "\n".join(lines)


def format_json_output(result: VersionCheckResult) -> str:
    """格式化 JSON 输出"""
    output = {
        "success": result.success,
        "error_count": len(result.errors),
        "warning_count": len(result.warnings),
        "changed_critical_files": result.changed_critical_files,
        "contract_version": result.contract_version,
        "contract_last_updated": result.contract_last_updated,
        "doc_versions": result.doc_versions,
        "version_updated": result.version_updated,
        "last_updated_updated": result.last_updated_updated,
        "version_in_doc": result.version_in_doc,
        "errors": [
            {
                "error_type": e.error_type,
                "message": e.message,
                "suggestion": e.suggestion,
            }
            for e in result.errors
        ],
        "warnings": result.warnings,
    }
    return json.dumps(output, indent=2, ensure_ascii=False)


# ============================================================================
# Main
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="检查 workflow contract 版本策略：关键文件变更时强制版本更新",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
关键文件列表：
  - .github/workflows/ci.yml
  - .github/workflows/nightly.yml
  - scripts/ci/workflow_contract.v1.json
  - docs/ci_nightly_workflow_refactor/*.md
  - Makefile（仅 CI 相关变更）

工具脚本关键文件（变更影响合约执行逻辑）：
  - scripts/ci/validate_workflows.py
  - scripts/ci/workflow_contract.v1.schema.json
  - scripts/ci/check_workflow_contract_docs_sync.py
  - scripts/ci/workflow_contract_drift_report.py
  - scripts/ci/generate_workflow_contract_snapshot.py

版本策略要求：
  1. workflow_contract.v1.json 的 version 字段必须更新
  2. workflow_contract.v1.json 的 last_updated 字段必须更新
  3. contract.md 版本控制表必须包含新版本

工具脚本版本策略建议：
  - Major: 校验逻辑不兼容变更（如删除校验规则、修改错误码含义）
  - Minor: 新增校验功能、新增错误类型
  - Patch: 修复 bug、优化性能、完善错误提示
        """,
    )
    parser.add_argument(
        "--base",
        type=str,
        default="HEAD~1",
        help="Git diff 比较基准 (default: HEAD~1)",
    )
    parser.add_argument(
        "--pr-mode",
        action="store_true",
        help="PR 模式：从 merge-base 开始比较（适用于 CI PR 检查）",
    )
    parser.add_argument(
        "--changed-files",
        nargs="*",
        type=str,
        default=None,
        help="指定变更文件列表（用于测试或 CI 集成）",
    )
    parser.add_argument(
        "--contract",
        type=str,
        default=DEFAULT_CONTRACT_PATH,
        help=f"Contract JSON 文件路径 (default: {DEFAULT_CONTRACT_PATH})",
    )
    parser.add_argument(
        "--doc",
        type=str,
        default=DEFAULT_DOC_PATH,
        help=f"Documentation Markdown 文件路径 (default: {DEFAULT_DOC_PATH})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出结果",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="显示详细检查过程",
    )
    parser.add_argument(
        "--project-root",
        type=str,
        default=None,
        help="项目根目录（默认使用当前工作目录）",
    )

    args = parser.parse_args()

    # 确定项目根目录
    if args.project_root:
        project_root = Path(args.project_root)
    else:
        # 尝试从脚本位置推断项目根目录
        script_path = Path(__file__).resolve()
        # scripts/ci/check_workflow_contract_version_policy.py -> project_root
        project_root = script_path.parent.parent.parent
        if not (project_root / "scripts" / "ci").exists():
            # 回退到当前工作目录
            project_root = Path.cwd()

    if args.verbose and not args.json:
        print(f"Project root: {project_root}")
        print()

    checker = WorkflowContractVersionChecker(
        project_root=project_root,
        contract_path=args.contract,
        doc_path=args.doc,
        verbose=args.verbose and not args.json,
    )

    result = checker.check(
        changed_files=args.changed_files,
        base=args.base,
        pr_mode=args.pr_mode,
    )

    if args.json:
        print(format_json_output(result))
    else:
        print(format_text_output(result))

    # 返回退出码
    if result.errors:
        # 区分文件错误和版本策略错误
        file_errors = [
            e
            for e in result.errors
            if e.error_type in ("contract_not_found", "doc_not_found", "contract_parse_error", "doc_read_error")
        ]
        if file_errors:
            return 2
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
