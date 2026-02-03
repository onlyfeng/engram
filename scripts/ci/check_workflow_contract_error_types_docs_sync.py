#!/usr/bin/env python3
"""
Workflow Contract Error Types 文档同步校验脚本

校验 docs/ci_nightly_workflow_refactor/contract.md 第 13 章中
error_type / warning_type / drift_type 的文档表格是否与代码常量一致。

校验范围：
1. validate_workflows.py: ErrorTypes / WarningTypes
2. check_workflow_contract_docs_sync.py: DOCS_SYNC_ERROR_TYPES
3. check_workflow_contract_version_policy.py: VERSION_POLICY_ERROR_TYPES
4. workflow_contract_drift_report.py: DRIFT_TYPES / DRIFT_CATEGORIES / DRIFT_SEVERITIES

使用方式：
    python scripts/ci/check_workflow_contract_error_types_docs_sync.py
    python scripts/ci/check_workflow_contract_error_types_docs_sync.py --json
    python scripts/ci/check_workflow_contract_error_types_docs_sync.py --verbose

退出码：
    0: 校验通过
    1: 校验失败（存在不同步项）
    2: 文件读取/解析错误
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path


# ============================================================================
# Constants
# ============================================================================

DEFAULT_DOC_PATH = "docs/ci_nightly_workflow_refactor/contract.md"

# 第 13 章关键表格的标题锚点（用于章节切片）
SECTION_HEADINGS = {
    "validate_error_types": "#### 13.1.1 ValidationError.error_type 列表",
    "validate_warning_types": "#### 13.1.2 ValidationWarning.warning_type 列表",
    "docs_sync_error_types": "### 13.2 check_workflow_contract_docs_sync.py 错误类型",
    "version_policy_error_types": "### 13.3 check_workflow_contract_version_policy.py 错误类型",
    "drift_types": "#### 13.4.1 drift_type 列表",
    "drift_categories": "#### 13.4.2 category 列表",
    "drift_severities": "#### 13.4.3 severity 列表",
}

SECTION_TITLES = {
    "validate_error_types": "validate_workflows.py error_type",
    "validate_warning_types": "validate_workflows.py warning_type",
    "docs_sync_error_types": "check_workflow_contract_docs_sync.py error_type",
    "version_policy_error_types": "check_workflow_contract_version_policy.py error_type",
    "drift_types": "workflow_contract_drift_report.py drift_type",
    "drift_categories": "workflow_contract_drift_report.py category",
    "drift_severities": "workflow_contract_drift_report.py severity",
}

SECTION_DOC_HINTS = {
    "validate_error_types": "contract.md 第 13.1.1 节表格",
    "validate_warning_types": "contract.md 第 13.1.2 节表格",
    "docs_sync_error_types": "contract.md 第 13.2 节表格",
    "version_policy_error_types": "contract.md 第 13.3 节表格",
    "drift_types": "contract.md 第 13.4.1 节表格",
    "drift_categories": "contract.md 第 13.4.2 节表格",
    "drift_severities": "contract.md 第 13.4.3 节表格",
}

SECTION_CODE_HINTS = {
    "validate_error_types": "scripts/ci/validate_workflows.py ErrorTypes",
    "validate_warning_types": "scripts/ci/validate_workflows.py WarningTypes",
    "docs_sync_error_types": "scripts/ci/check_workflow_contract_docs_sync.py DOCS_SYNC_ERROR_TYPES",
    "version_policy_error_types": "scripts/ci/check_workflow_contract_version_policy.py VERSION_POLICY_ERROR_TYPES",
    "drift_types": "scripts/ci/workflow_contract_drift_report.py DRIFT_TYPES",
    "drift_categories": "scripts/ci/workflow_contract_drift_report.py DRIFT_CATEGORIES",
    "drift_severities": "scripts/ci/workflow_contract_drift_report.py DRIFT_SEVERITIES",
}


# ============================================================================
# Data Classes
# ============================================================================


@dataclass
class SyncError:
    """同步错误"""

    error_type: str  # missing_in_doc | extra_in_doc | section_missing | doc_read_error | code_import_error
    category: str
    value: str
    message: str
    fix_suggestion: str = ""


@dataclass
class SyncResult:
    """同步校验结果"""

    success: bool = True
    errors: list[SyncError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    code_values: dict[str, list[str]] = field(default_factory=dict)
    doc_values: dict[str, list[str]] = field(default_factory=dict)
    missing_sections: list[str] = field(default_factory=list)

    def add_error(self, error: SyncError) -> None:
        """添加错误"""
        self.errors.append(error)
        self.success = False

    def add_warning(self, warning: str) -> None:
        """添加警告"""
        self.warnings.append(warning)


# ============================================================================
# Parser Utilities
# ============================================================================


def _heading_level(line: str) -> int:
    stripped = line.lstrip()
    if not stripped.startswith("#"):
        return 0
    return len(stripped) - len(stripped.lstrip("#"))


def extract_section(content: str, heading: str) -> str | None:
    """提取从指定标题到下一个同级/更高标题的文本"""
    lines = content.splitlines()
    start_index = None
    start_level = 0

    for idx, line in enumerate(lines):
        if heading in line:
            start_index = idx
            start_level = _heading_level(line)
            break

    if start_index is None:
        return None

    end_index = len(lines)
    for idx in range(start_index + 1, len(lines)):
        level = _heading_level(lines[idx])
        if level and level <= start_level:
            end_index = idx
            break

    return "\n".join(lines[start_index:end_index])


def parse_table_values(section_text: str) -> list[str]:
    """解析 Markdown 表格第一列中的反引号值"""
    pattern = re.compile(r"^\|\s*`([^`]+)`\s*\|", re.MULTILINE)
    return [value.strip() for value in pattern.findall(section_text)]


def _collect_class_constants(target_cls: type) -> set[str]:
    return {
        value
        for name, value in target_cls.__dict__.items()
        if not name.startswith("_") and isinstance(value, str)
    }


def load_code_sets() -> dict[str, set[str]]:
    """加载代码中的 error_type / warning_type / drift 常量集合"""
    from scripts.ci.check_workflow_contract_docs_sync import DOCS_SYNC_ERROR_TYPES
    from scripts.ci.check_workflow_contract_version_policy import VERSION_POLICY_ERROR_TYPES
    from scripts.ci.validate_workflows import ErrorTypes, WarningTypes
    from scripts.ci.workflow_contract_drift_report import (
        DRIFT_CATEGORIES,
        DRIFT_SEVERITIES,
        DRIFT_TYPES,
    )

    return {
        "validate_error_types": _collect_class_constants(ErrorTypes),
        "validate_warning_types": _collect_class_constants(WarningTypes),
        "docs_sync_error_types": set(DOCS_SYNC_ERROR_TYPES),
        "version_policy_error_types": set(VERSION_POLICY_ERROR_TYPES),
        "drift_types": set(DRIFT_TYPES),
        "drift_categories": set(DRIFT_CATEGORIES),
        "drift_severities": set(DRIFT_SEVERITIES),
    }


# ============================================================================
# Core Logic
# ============================================================================


class ErrorTypesDocsSyncChecker:
    """Error Types 文档同步校验器"""

    def __init__(self, doc_path: Path, verbose: bool = False) -> None:
        self.doc_path = doc_path
        self.verbose = verbose
        self.result = SyncResult()
        self.doc_content = ""

    def load_doc(self) -> bool:
        """加载文档"""
        if not self.doc_path.exists():
            self.result.add_error(
                SyncError(
                    error_type="doc_not_found",
                    category="file",
                    value=str(self.doc_path),
                    message=f"Documentation file not found: {self.doc_path}",
                )
            )
            return False

        try:
            self.doc_content = self.doc_path.read_text(encoding="utf-8")
            return True
        except Exception as exc:
            self.result.add_error(
                SyncError(
                    error_type="doc_read_error",
                    category="file",
                    value=str(self.doc_path),
                    message=f"Failed to read documentation file: {exc}",
                )
            )
            return False

    def _compare_sets(
        self,
        section_key: str,
        code_values: set[str],
        doc_values: set[str],
    ) -> None:
        missing_in_doc = code_values - doc_values
        extra_in_doc = doc_values - code_values

        doc_hint = SECTION_DOC_HINTS.get(section_key, "contract.md 第 13 章")
        code_hint = SECTION_CODE_HINTS.get(section_key, "对应脚本常量")
        category = section_key

        for value in sorted(missing_in_doc):
            self.result.add_error(
                SyncError(
                    error_type="missing_in_doc",
                    category=category,
                    value=value,
                    message=(
                        f"Value '{value}' exists in code but not documented ({doc_hint})."
                    ),
                    fix_suggestion=f"Add `{value}` to {doc_hint} (source: {code_hint}).",
                )
            )

        for value in sorted(extra_in_doc):
            self.result.add_error(
                SyncError(
                    error_type="extra_in_doc",
                    category=category,
                    value=value,
                    message=(
                        f"Value '{value}' documented in {doc_hint} but not defined in code."
                    ),
                    fix_suggestion=f"Remove `{value}` from {doc_hint} or add it to {code_hint}.",
                )
            )

    def _parse_section_values(self, section_key: str) -> set[str] | None:
        heading = SECTION_HEADINGS[section_key]
        section_text = extract_section(self.doc_content, heading)
        if section_text is None:
            self.result.add_error(
                SyncError(
                    error_type="section_missing",
                    category="doc_structure",
                    value=SECTION_TITLES.get(section_key, section_key),
                    message=(
                        f"Documentation missing section for '{SECTION_TITLES.get(section_key, section_key)}'."
                    ),
                    fix_suggestion=f"Add section heading '{heading}' in contract.md Chapter 13.",
                )
            )
            self.result.missing_sections.append(section_key)
            return None

        values = parse_table_values(section_text)
        counts = Counter(values)
        duplicates = sorted([value for value, count in counts.items() if count > 1])
        if duplicates:
            self.result.add_warning(
                f"Duplicate entries in {SECTION_DOC_HINTS.get(section_key, section_key)}: "
                f"{', '.join(duplicates)}"
            )

        return set(values)

    def check(self) -> SyncResult:
        """执行完整校验"""
        if self.verbose:
            print(f"Loading documentation: {self.doc_path}")

        if not self.load_doc():
            return self.result

        try:
            code_sets = load_code_sets()
        except Exception as exc:
            self.result.add_error(
                SyncError(
                    error_type="code_import_error",
                    category="code",
                    value="code_sets",
                    message=f"Failed to load code error type sets: {exc}",
                    fix_suggestion="Ensure required modules are available and importable.",
                )
            )
            return self.result

        for key, values in code_sets.items():
            self.result.code_values[key] = sorted(values)

        for section_key in SECTION_HEADINGS:
            doc_values = self._parse_section_values(section_key)
            if doc_values is None:
                continue

            self.result.doc_values[section_key] = sorted(doc_values)
            self._compare_sets(section_key, code_sets[section_key], doc_values)

            if self.verbose:
                code_count = len(code_sets[section_key])
                doc_count = len(doc_values)
                print(f"[{section_key}] code={code_count}, doc={doc_count}")

        return self.result


# ============================================================================
# Output Formatting
# ============================================================================


def format_text_output(result: SyncResult) -> str:
    """格式化文本输出"""
    lines: list[str] = []

    title = "Workflow Contract Error Types Docs Sync Check"
    lines.append("=" * 60)
    lines.append(f"{title}: {'PASSED' if result.success else 'FAILED'}")
    lines.append("=" * 60)
    lines.append("")
    lines.append("Summary:")
    lines.append(f"  - Sections checked: {len(result.doc_values)}")
    lines.append(f"  - Missing sections: {len(result.missing_sections)}")
    lines.append(f"  - Errors: {len(result.errors)}")
    lines.append(f"  - Warnings: {len(result.warnings)}")

    if result.errors:
        lines.append("")
        lines.append("ERRORS:")
        for error in result.errors:
            lines.append(f"  [{error.error_type}] {error.category}: {error.value}")
            lines.append(f"    {error.message}")
            if error.fix_suggestion:
                lines.append(f"    Fix: {error.fix_suggestion}")

    if result.warnings:
        lines.append("")
        lines.append("WARNINGS:")
        for warning in result.warnings:
            lines.append(f"  {warning}")

    return "\n".join(lines)


def format_json_output(result: SyncResult) -> str:
    """格式化 JSON 输出"""
    output = {
        "success": result.success,
        "error_count": len(result.errors),
        "warning_count": len(result.warnings),
        "missing_sections": result.missing_sections,
        "code_values": result.code_values,
        "doc_values": result.doc_values,
        "errors": [
            {
                "error_type": error.error_type,
                "category": error.category,
                "value": error.value,
                "message": error.message,
                "fix_suggestion": error.fix_suggestion,
            }
            for error in result.errors
        ],
        "warnings": result.warnings,
    }
    return json.dumps(output, indent=2, ensure_ascii=False)


# ============================================================================
# Main
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="校验 workflow contract error types 文档同步",
        formatter_class=argparse.RawDescriptionHelpFormatter,
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

    if args.project_root:
        project_root = Path(args.project_root)
    else:
        script_path = Path(__file__).resolve()
        project_root = script_path.parent.parent.parent
        if not (project_root / "scripts" / "ci").exists():
            project_root = Path.cwd()

    doc_path = project_root / args.doc

    if args.verbose and not args.json:
        print(f"Project root: {project_root}")
        print(f"Doc path: {doc_path}")
        print()

    checker = ErrorTypesDocsSyncChecker(doc_path=doc_path, verbose=args.verbose and not args.json)
    result = checker.check()

    if args.json:
        print(format_json_output(result))
    else:
        print(format_text_output(result))

    if result.errors:
        file_errors = [e for e in result.errors if e.category in ("file", "code")]
        if file_errors:
            return 2
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
