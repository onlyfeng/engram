#!/usr/bin/env python3
"""
tests/ci/test_workflow_contract_error_types_docs_sync.py

单元测试：check_workflow_contract_error_types_docs_sync.py 的文档同步校验
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Iterable

from scripts.ci.check_workflow_contract_error_types_docs_sync import (
    SECTION_HEADINGS,
    ErrorTypesDocsSyncChecker,
    load_code_sets,
)

TABLE_HEADERS = {
    "validate_error_types": ["error_type", "说明"],
    "validate_warning_types": ["warning_type", "说明"],
    "docs_sync_error_types": ["error_type", "说明", "分类"],
    "version_policy_error_types": ["error_type", "说明"],
    "drift_types": ["drift_type", "说明"],
    "drift_categories": ["category", "说明"],
    "drift_severities": ["severity", "说明"],
}


def _make_table(values: Iterable[str], headers: list[str]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(["---"] * len(headers)) + "|",
    ]
    for value in sorted(values):
        padding = ["-"] * (len(headers) - 1)
        lines.append("| " + " | ".join([f"`{value}`", *padding]) + " |")
    return "\n".join(lines)


def _build_doc(
    code_sets: dict[str, set[str]],
    overrides: dict[str, Iterable[str]] | None = None,
    omit_sections: set[str] | None = None,
) -> str:
    overrides = overrides or {}
    omit_sections = omit_sections or set()

    def values_for(section_key: str) -> Iterable[str]:
        return overrides.get(section_key, code_sets[section_key])

    parts = [
        "# CI/Nightly Workflow Contract",
        "",
        "## 13. Error Type 体系与版本策略",
        "",
        "### 13.1 validate_workflows.py 错误类型",
        "",
    ]

    for key in (
        "validate_error_types",
        "validate_warning_types",
        "docs_sync_error_types",
        "version_policy_error_types",
        "drift_types",
        "drift_categories",
        "drift_severities",
    ):
        if key in omit_sections:
            continue
        heading = SECTION_HEADINGS[key]
        parts.extend([heading, _make_table(values_for(key), TABLE_HEADERS[key]), ""])

    return "\n".join(parts)


def _write_doc(content: str) -> Path:
    temp_dir = Path(tempfile.mkdtemp())
    doc_path = temp_dir / "contract.md"
    doc_path.write_text(content, encoding="utf-8")
    return doc_path


class TestErrorTypesDocsSyncChecker:
    """基础同步校验测试"""

    def test_full_doc_passes(self) -> None:
        code_sets = load_code_sets()
        doc_content = _build_doc(code_sets)
        doc_path = _write_doc(doc_content)

        checker = ErrorTypesDocsSyncChecker(doc_path)
        result = checker.check()

        assert result.success is True
        assert result.errors == []

    def test_missing_item_reports_error(self) -> None:
        code_sets = load_code_sets()
        missing_value = next(iter(code_sets["validate_error_types"]))
        overrides = {
            "validate_error_types": code_sets["validate_error_types"] - {missing_value},
        }
        doc_content = _build_doc(code_sets, overrides=overrides)
        doc_path = _write_doc(doc_content)

        checker = ErrorTypesDocsSyncChecker(doc_path)
        result = checker.check()

        assert result.success is False
        missing_errors = [e for e in result.errors if e.error_type == "missing_in_doc"]
        assert any(e.value == missing_value for e in missing_errors)

    def test_extra_item_reports_error(self) -> None:
        code_sets = load_code_sets()
        overrides = {
            "version_policy_error_types": list(code_sets["version_policy_error_types"])
            + ["extra_error_type"]
        }
        doc_content = _build_doc(code_sets, overrides=overrides)
        doc_path = _write_doc(doc_content)

        checker = ErrorTypesDocsSyncChecker(doc_path)
        result = checker.check()

        assert result.success is False
        extra_errors = [e for e in result.errors if e.error_type == "extra_in_doc"]
        assert any(e.value == "extra_error_type" for e in extra_errors)

    def test_missing_section_reports_error(self) -> None:
        code_sets = load_code_sets()
        doc_content = _build_doc(code_sets, omit_sections={"drift_severities"})
        doc_path = _write_doc(doc_content)

        checker = ErrorTypesDocsSyncChecker(doc_path)
        result = checker.check()

        assert result.success is False
        section_errors = [e for e in result.errors if e.error_type == "section_missing"]
        assert any("drift_report.py severity" in e.value for e in section_errors)
