#!/usr/bin/env python3
"""
Tests for workflow_contract_drift_report.py

覆盖 added/removed/changed 三类 drift（job_id/job_name/step/env_var），
以及 format_json_output() 和 format_markdown_output() 的输出完整性。
"""

from __future__ import annotations

import json

# 导入被测模块
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))
from workflow_contract_drift_report import (
    DriftItem,
    DriftReport,
    WorkflowContractDriftAnalyzer,
    format_json_output,
    format_markdown_output,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def temp_workspace(tmp_path: Path) -> Path:
    """创建临时 workspace 目录结构"""
    # 创建 .github/workflows 目录
    workflows_dir = tmp_path / ".github" / "workflows"
    workflows_dir.mkdir(parents=True)
    return tmp_path


def write_contract(workspace: Path, contract: dict[str, Any]) -> Path:
    """写入 contract JSON 文件"""
    scripts_ci = workspace / "scripts" / "ci"
    scripts_ci.mkdir(parents=True, exist_ok=True)
    contract_path = scripts_ci / "workflow_contract.v1.json"
    with open(contract_path, "w", encoding="utf-8") as f:
        json.dump(contract, f, indent=2)
    return contract_path


def write_workflow(workspace: Path, name: str, workflow: dict[str, Any]) -> Path:
    """写入 workflow YAML 文件"""
    workflows_dir = workspace / ".github" / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    workflow_path = workflows_dir / f"{name}.yml"
    with open(workflow_path, "w", encoding="utf-8") as f:
        yaml.dump(workflow, f)
    return workflow_path


# ============================================================================
# Test Cases for DriftItem and DriftReport
# ============================================================================


class TestDriftItem:
    """DriftItem dataclass 测试"""

    def test_drift_item_defaults(self) -> None:
        """验证 DriftItem 默认值"""
        item = DriftItem(
            drift_type="added",
            category="job_id",
            workflow="ci",
            key="test_job",
        )
        assert item.drift_type == "added"
        assert item.category == "job_id"
        assert item.workflow == "ci"
        assert item.key == "test_job"
        assert item.contract_value is None
        assert item.actual_value is None
        assert item.location is None
        assert item.severity == "warning"

    def test_drift_item_full(self) -> None:
        """验证 DriftItem 完整字段"""
        item = DriftItem(
            drift_type="changed",
            category="step",
            workflow="ci",
            key="lint/Check code",
            contract_value="Check code",
            actual_value="Run code check",
            location="jobs.lint.steps",
            severity="error",
        )
        assert item.drift_type == "changed"
        assert item.contract_value == "Check code"
        assert item.actual_value == "Run code check"
        assert item.location == "jobs.lint.steps"
        assert item.severity == "error"


class TestDriftReport:
    """DriftReport dataclass 测试"""

    def test_empty_report(self) -> None:
        """验证空报告"""
        report = DriftReport()
        assert report.has_drift is False
        assert report.drift_items == []
        assert report.summary == {}

    def test_add_drift(self) -> None:
        """验证 add_drift 方法"""
        report = DriftReport()
        item = DriftItem(
            drift_type="added",
            category="job_id",
            workflow="ci",
            key="new_job",
        )
        report.add_drift(item)

        assert report.has_drift is True
        assert len(report.drift_items) == 1
        assert report.summary == {"job_id_added": 1}

    def test_add_multiple_drifts(self) -> None:
        """验证多个 drift 的 summary 计数"""
        report = DriftReport()

        # 添加多个 drift
        report.add_drift(
            DriftItem(drift_type="added", category="job_id", workflow="ci", key="job1")
        )
        report.add_drift(
            DriftItem(drift_type="added", category="job_id", workflow="ci", key="job2")
        )
        report.add_drift(
            DriftItem(drift_type="removed", category="step", workflow="ci", key="lint/step1")
        )
        report.add_drift(
            DriftItem(drift_type="changed", category="job_name", workflow="ci", key="lint")
        )

        assert report.summary == {
            "job_id_added": 2,
            "step_removed": 1,
            "job_name_changed": 1,
        }


# ============================================================================
# Test Cases for WorkflowContractDriftAnalyzer
# ============================================================================


class TestWorkflowContractDriftAnalyzer:
    """WorkflowContractDriftAnalyzer 测试"""

    def test_job_id_added(self, temp_workspace: Path) -> None:
        """测试 job_id added drift"""
        # Contract 定义 2 个 job
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-01",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint", "test"],
            },
        }
        write_contract(temp_workspace, contract)

        # 实际 workflow 有 3 个 job（多了 deploy）
        workflow = {
            "name": "CI",
            "jobs": {
                "lint": {"name": "Lint", "steps": []},
                "test": {"name": "Test", "steps": []},
                "deploy": {"name": "Deploy", "steps": []},  # added
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        analyzer = WorkflowContractDriftAnalyzer(
            contract_path=temp_workspace / "scripts" / "ci" / "workflow_contract.v1.json",
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        assert report.has_drift is True
        added_items = [
            i for i in report.drift_items if i.drift_type == "added" and i.category == "job_id"
        ]
        assert len(added_items) == 1
        assert added_items[0].key == "deploy"
        assert added_items[0].location == "jobs.deploy"
        assert added_items[0].severity == "warning"

    def test_job_id_removed(self, temp_workspace: Path) -> None:
        """测试 job_id removed drift"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-01",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint", "test", "deploy"],
            },
        }
        write_contract(temp_workspace, contract)

        # 实际 workflow 只有 2 个 job（少了 deploy）
        workflow = {
            "name": "CI",
            "jobs": {
                "lint": {"name": "Lint", "steps": []},
                "test": {"name": "Test", "steps": []},
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        analyzer = WorkflowContractDriftAnalyzer(
            contract_path=temp_workspace / "scripts" / "ci" / "workflow_contract.v1.json",
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        assert report.has_drift is True
        removed_items = [
            i for i in report.drift_items if i.drift_type == "removed" and i.category == "job_id"
        ]
        assert len(removed_items) == 1
        assert removed_items[0].key == "deploy"
        assert removed_items[0].severity == "error"

    def test_job_name_changed(self, temp_workspace: Path) -> None:
        """测试 job_name changed drift"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-01",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint"],
                "job_names": ["Lint Code"],  # expected name
            },
        }
        write_contract(temp_workspace, contract)

        workflow = {
            "name": "CI",
            "jobs": {
                "lint": {"name": "Run Linter", "steps": []},  # actual name changed
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        analyzer = WorkflowContractDriftAnalyzer(
            contract_path=temp_workspace / "scripts" / "ci" / "workflow_contract.v1.json",
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        assert report.has_drift is True
        changed_items = [
            i for i in report.drift_items if i.drift_type == "changed" and i.category == "job_name"
        ]
        assert len(changed_items) == 1
        assert changed_items[0].key == "lint"
        assert changed_items[0].contract_value == "Lint Code"
        assert changed_items[0].actual_value == "Run Linter"
        assert changed_items[0].location == "jobs.lint.name"

    def test_step_removed(self, temp_workspace: Path) -> None:
        """测试 step removed drift"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-01",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint"],
                "required_jobs": [
                    {
                        "id": "lint",
                        "required_steps": ["Checkout", "Run lint", "Upload results"],
                    }
                ],
            },
        }
        write_contract(temp_workspace, contract)

        # 缺少 "Upload results" step
        workflow = {
            "name": "CI",
            "jobs": {
                "lint": {
                    "name": "Lint",
                    "steps": [
                        {"name": "Checkout"},
                        {"name": "Run lint"},
                    ],
                },
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        analyzer = WorkflowContractDriftAnalyzer(
            contract_path=temp_workspace / "scripts" / "ci" / "workflow_contract.v1.json",
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        assert report.has_drift is True
        removed_items = [
            i for i in report.drift_items if i.drift_type == "removed" and i.category == "step"
        ]
        assert len(removed_items) == 1
        assert removed_items[0].key == "lint/Upload results"
        assert removed_items[0].severity == "error"

    def test_step_changed(self, temp_workspace: Path) -> None:
        """测试 step changed drift（模糊匹配）"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-01",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint"],
                "required_jobs": [
                    {
                        "id": "lint",
                        "required_steps": ["Run lint check"],
                    }
                ],
            },
        }
        write_contract(temp_workspace, contract)

        # Step name 变化但可以模糊匹配
        workflow = {
            "name": "CI",
            "jobs": {
                "lint": {
                    "name": "Lint",
                    "steps": [
                        {"name": "Run lint check (v2)"},  # changed but fuzzy match
                    ],
                },
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        analyzer = WorkflowContractDriftAnalyzer(
            contract_path=temp_workspace / "scripts" / "ci" / "workflow_contract.v1.json",
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        assert report.has_drift is True
        changed_items = [
            i for i in report.drift_items if i.drift_type == "changed" and i.category == "step"
        ]
        assert len(changed_items) == 1
        assert changed_items[0].contract_value == "Run lint check"
        assert changed_items[0].actual_value == "Run lint check (v2)"

    def test_env_var_removed(self, temp_workspace: Path) -> None:
        """测试 env_var removed drift"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-01",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint"],
                "required_env_vars": ["CI", "NODE_ENV", "DEBUG"],
            },
        }
        write_contract(temp_workspace, contract)

        # 缺少 DEBUG 环境变量
        workflow = {
            "name": "CI",
            "env": {
                "CI": "true",
                "NODE_ENV": "test",
            },
            "jobs": {
                "lint": {"name": "Lint", "steps": []},
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        analyzer = WorkflowContractDriftAnalyzer(
            contract_path=temp_workspace / "scripts" / "ci" / "workflow_contract.v1.json",
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        assert report.has_drift is True
        removed_items = [
            i for i in report.drift_items if i.drift_type == "removed" and i.category == "env_var"
        ]
        assert len(removed_items) == 1
        assert removed_items[0].key == "DEBUG"
        assert removed_items[0].severity == "error"

    def test_no_drift(self, temp_workspace: Path) -> None:
        """测试无 drift 的情况"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-01",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint", "test"],
                "job_names": ["Lint Code", "Run Tests"],
            },
        }
        write_contract(temp_workspace, contract)

        workflow = {
            "name": "CI",
            "jobs": {
                "lint": {"name": "Lint Code", "steps": []},
                "test": {"name": "Run Tests", "steps": []},
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        analyzer = WorkflowContractDriftAnalyzer(
            contract_path=temp_workspace / "scripts" / "ci" / "workflow_contract.v1.json",
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        assert report.has_drift is False
        assert len(report.drift_items) == 0

    def test_workflow_filter(self, temp_workspace: Path) -> None:
        """测试 workflow 过滤器"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-01",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint"],
            },
            "nightly": {
                "file": ".github/workflows/nightly.yml",
                "job_ids": ["build"],
            },
        }
        write_contract(temp_workspace, contract)

        # 只创建 ci workflow
        write_workflow(
            temp_workspace,
            "ci",
            {"name": "CI", "jobs": {"lint": {"name": "Lint"}}},
        )

        analyzer = WorkflowContractDriftAnalyzer(
            contract_path=temp_workspace / "scripts" / "ci" / "workflow_contract.v1.json",
            workspace_root=temp_workspace,
            workflow_filter="ci",
        )
        report = analyzer.analyze()

        # 只检查了 ci，不会报告 nightly 缺失
        assert "ci" in report.workflows_checked
        assert "nightly" not in report.workflows_checked


# ============================================================================
# Test Cases for format_json_output
# ============================================================================


class TestFormatJsonOutput:
    """format_json_output 测试"""

    def test_json_output_field_completeness(self) -> None:
        """验证 JSON 输出字段完整性"""
        report = DriftReport()
        report.contract_version = "1.0.0"
        report.contract_last_updated = "2026-02-01"
        report.report_generated_at = "2026-02-02T10:00:00"
        report.workflows_checked = ["ci"]

        report.add_drift(
            DriftItem(
                drift_type="added",
                category="job_id",
                workflow="ci",
                key="new_job",
                actual_value="new_job",
                location="jobs.new_job",
                severity="warning",
            )
        )

        output = format_json_output(report)
        data = json.loads(output)

        # 验证顶层字段
        assert "has_drift" in data
        assert "contract_version" in data
        assert "contract_last_updated" in data
        assert "report_generated_at" in data
        assert "workflows_checked" in data
        assert "summary" in data
        assert "drift_count" in data
        assert "drift_items" in data

        # 验证字段值
        assert data["has_drift"] is True
        assert data["contract_version"] == "1.0.0"
        assert data["drift_count"] == 1

        # 验证 drift_items 字段完整性
        assert len(data["drift_items"]) == 1
        item = data["drift_items"][0]
        assert "drift_type" in item
        assert "category" in item
        assert "workflow" in item
        assert "key" in item
        assert "contract_value" in item
        assert "actual_value" in item
        assert "location" in item
        assert "severity" in item

        # 验证 drift_item 值
        assert item["drift_type"] == "added"
        assert item["category"] == "job_id"
        assert item["severity"] == "warning"
        assert item["location"] == "jobs.new_job"

    def test_json_output_empty_report(self) -> None:
        """验证空报告的 JSON 输出"""
        report = DriftReport()
        report.contract_version = "1.0.0"
        report.contract_last_updated = "2026-02-01"

        output = format_json_output(report)
        data = json.loads(output)

        assert data["has_drift"] is False
        assert data["drift_count"] == 0
        assert data["drift_items"] == []
        assert data["summary"] == {}

    def test_json_output_multiple_drifts(self) -> None:
        """验证多个 drift 项的 JSON 输出"""
        report = DriftReport()
        report.contract_version = "1.0.0"

        # 添加多种类型的 drift
        report.add_drift(
            DriftItem(
                drift_type="added",
                category="job_id",
                workflow="ci",
                key="job1",
                severity="warning",
            )
        )
        report.add_drift(
            DriftItem(
                drift_type="removed",
                category="step",
                workflow="ci",
                key="lint/step1",
                severity="error",
            )
        )
        report.add_drift(
            DriftItem(
                drift_type="changed",
                category="job_name",
                workflow="ci",
                key="lint",
                severity="warning",
            )
        )

        output = format_json_output(report)
        data = json.loads(output)

        assert data["drift_count"] == 3
        assert len(data["drift_items"]) == 3

        # 验证 summary
        assert data["summary"]["job_id_added"] == 1
        assert data["summary"]["step_removed"] == 1
        assert data["summary"]["job_name_changed"] == 1

    def test_json_output_sorted_summary(self) -> None:
        """验证 summary 字段在 JSON 输出中"""
        report = DriftReport()
        report.contract_version = "1.0.0"

        # 添加多个同类 drift
        report.add_drift(DriftItem(drift_type="added", category="job_id", workflow="ci", key="a"))
        report.add_drift(DriftItem(drift_type="added", category="job_id", workflow="ci", key="b"))
        report.add_drift(DriftItem(drift_type="added", category="job_id", workflow="ci", key="c"))

        output = format_json_output(report)
        data = json.loads(output)

        assert data["summary"]["job_id_added"] == 3

    def test_json_output_new_categories(self) -> None:
        """验证 JSON 输出包含新的 categories (artifact_path, make_target, label)"""
        report = DriftReport()
        report.contract_version = "1.0.0"

        # 添加 artifact_path drift
        report.add_drift(
            DriftItem(
                drift_type="removed",
                category="artifact_path",
                workflow="ci",
                key="coverage.xml",
                contract_value="coverage.xml",
                location="artifact_archive.required_artifact_paths",
                severity="error",
            )
        )

        # 添加 make_target drift
        report.add_drift(
            DriftItem(
                drift_type="added",
                category="make_target",
                workflow="(global)",
                key="test",
                actual_value="test",
                location="workflows/*.yml",
                severity="warning",
            )
        )

        # 添加 label drift
        report.add_drift(
            DriftItem(
                drift_type="removed",
                category="label",
                workflow="ci",
                key="skip-ci",
                contract_value="skip-ci",
                location="gh_pr_labels_to_outputs.py LABEL_*",
                severity="error",
            )
        )

        output = format_json_output(report)
        data = json.loads(output)

        # 验证 summary 包含新的 categories
        assert data["summary"]["artifact_path_removed"] == 1
        assert data["summary"]["make_target_added"] == 1
        assert data["summary"]["label_removed"] == 1

        # 验证 drift_items 包含新的 categories
        categories = [item["category"] for item in data["drift_items"]]
        assert "artifact_path" in categories
        assert "make_target" in categories
        assert "label" in categories


# ============================================================================
# Test Cases for format_markdown_output
# ============================================================================


class TestFormatMarkdownOutput:
    """format_markdown_output 测试"""

    def test_markdown_output_header(self) -> None:
        """验证 Markdown 输出包含 header"""
        report = DriftReport()
        report.contract_version = "1.0.0"
        report.contract_last_updated = "2026-02-01"
        report.report_generated_at = "2026-02-02T10:00:00"
        report.workflows_checked = ["ci"]

        output = format_markdown_output(report)

        assert "# Workflow Contract Drift Report" in output
        assert "## Overview" in output
        assert "**Contract Version**: 1.0.0" in output
        assert "**Contract Last Updated**: 2026-02-01" in output
        assert "**Report Generated**: 2026-02-02T10:00:00" in output
        assert "**Workflows Checked**: ci" in output

    def test_markdown_output_summary_table(self) -> None:
        """验证 Markdown 输出包含 summary 表格"""
        report = DriftReport()
        report.contract_version = "1.0.0"

        report.add_drift(
            DriftItem(drift_type="added", category="job_id", workflow="ci", key="job1")
        )
        report.add_drift(
            DriftItem(drift_type="removed", category="step", workflow="ci", key="lint/step1")
        )

        output = format_markdown_output(report)

        assert "## Summary" in output
        assert "| Category | Type | Count |" in output
        assert "|----------|------|-------|" in output
        # 验证表格内容
        assert "job_id" in output
        assert "added" in output
        assert "step" in output
        assert "removed" in output

    def test_markdown_output_drift_details_table(self) -> None:
        """验证 Markdown 输出包含 drift details 表格"""
        report = DriftReport()
        report.contract_version = "1.0.0"

        report.add_drift(
            DriftItem(
                drift_type="changed",
                category="job_name",
                workflow="ci",
                key="lint",
                contract_value="Lint Code",
                actual_value="Run Linter",
                location="jobs.lint.name",
                severity="warning",
            )
        )

        output = format_markdown_output(report)

        assert "## Drift Details" in output
        assert "### ci" in output
        # 验证表格列
        assert "| Type | Category | Key | Contract | Actual | Severity |" in output
        assert "|------|----------|-----|----------|--------|----------|" in output
        # 验证表格内容
        assert "changed" in output
        assert "job_name" in output
        assert "lint" in output
        assert "Lint Code" in output
        assert "Run Linter" in output
        assert "warning" in output

    def test_markdown_output_no_drift(self) -> None:
        """验证无 drift 时的 Markdown 输出"""
        report = DriftReport()
        report.contract_version = "1.0.0"
        report.workflows_checked = ["ci"]

        output = format_markdown_output(report)

        assert "**Has Drift**: No" in output
        assert "No drift detected" in output
        assert "## Drift Details" not in output

    def test_markdown_output_grouped_by_workflow(self) -> None:
        """验证 Markdown 输出按 workflow 分组"""
        report = DriftReport()
        report.contract_version = "1.0.0"

        report.add_drift(
            DriftItem(drift_type="added", category="job_id", workflow="ci", key="job1")
        )
        report.add_drift(
            DriftItem(drift_type="added", category="job_id", workflow="nightly", key="job2")
        )

        output = format_markdown_output(report)

        assert "### ci" in output
        assert "### nightly" in output

    def test_markdown_output_truncates_long_values(self) -> None:
        """验证 Markdown 输出截断长值"""
        report = DriftReport()
        report.contract_version = "1.0.0"

        long_value = "A" * 50  # 超过 30 字符
        report.add_drift(
            DriftItem(
                drift_type="changed",
                category="step",
                workflow="ci",
                key="lint/step",
                contract_value=long_value,
                actual_value="short",
                severity="warning",
            )
        )

        output = format_markdown_output(report)

        # 验证长值被截断
        assert "AAA..." in output
        assert long_value not in output

    def test_markdown_output_artifact_path_table(self) -> None:
        """验证 Markdown 输出包含 artifact_path 专用表格"""
        report = DriftReport()
        report.contract_version = "1.0.0"

        report.add_drift(
            DriftItem(
                drift_type="removed",
                category="artifact_path",
                workflow="ci",
                key="coverage.xml",
                contract_value="coverage.xml",
                location="artifact_archive.required_artifact_paths",
                severity="error",
            )
        )

        output = format_markdown_output(report)

        assert "## Artifact Path Drift" in output
        assert "| Type | Workflow | Path | Location | Severity |" in output
        assert "coverage.xml" in output

    def test_markdown_output_make_target_table(self) -> None:
        """验证 Markdown 输出包含 make_target 专用表格"""
        report = DriftReport()
        report.contract_version = "1.0.0"

        report.add_drift(
            DriftItem(
                drift_type="added",
                category="make_target",
                workflow="(global)",
                key="test",
                actual_value="test",
                location="workflows/*.yml",
                severity="warning",
            )
        )

        output = format_markdown_output(report)

        assert "## Make Target Drift" in output
        assert "| Type | Workflow | Target | Location | Severity |" in output
        assert "test" in output

    def test_markdown_output_label_table(self) -> None:
        """验证 Markdown 输出包含 label 专用表格"""
        report = DriftReport()
        report.contract_version = "1.0.0"

        report.add_drift(
            DriftItem(
                drift_type="removed",
                category="label",
                workflow="ci",
                key="skip-ci",
                contract_value="skip-ci",
                location="gh_pr_labels_to_outputs.py LABEL_*",
                severity="error",
            )
        )

        output = format_markdown_output(report)

        assert "## Label Drift" in output
        assert "| Type | Workflow | Label | Location | Severity |" in output
        assert "skip-ci" in output


# ============================================================================
# Integration Tests
# ============================================================================


# ============================================================================
# Test Cases for artifact_path Drift
# ============================================================================


class TestArtifactPathDrift:
    """artifact_path drift 测试"""

    def test_artifact_path_removed(self, temp_workspace: Path) -> None:
        """测试 artifact_path removed drift"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-01",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["test"],
                "artifact_archive": {
                    "required_artifact_paths": [
                        "coverage.xml",
                        "test-results/",
                        "logs/*.log",
                    ],
                },
            },
        }
        write_contract(temp_workspace, contract)

        # 缺少 logs/*.log 路径
        workflow = {
            "name": "CI",
            "jobs": {
                "test": {
                    "name": "Test",
                    "steps": [
                        {
                            "name": "Upload coverage",
                            "uses": "actions/upload-artifact@v4",
                            "with": {
                                "name": "coverage",
                                "path": "coverage.xml",
                            },
                        },
                        {
                            "name": "Upload test results",
                            "uses": "actions/upload-artifact@v4",
                            "with": {
                                "name": "test-results",
                                "path": "test-results/",
                            },
                        },
                    ],
                },
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        analyzer = WorkflowContractDriftAnalyzer(
            contract_path=temp_workspace / "scripts" / "ci" / "workflow_contract.v1.json",
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        assert report.has_drift is True
        removed_items = [
            i
            for i in report.drift_items
            if i.drift_type == "removed" and i.category == "artifact_path"
        ]
        assert len(removed_items) == 1
        assert removed_items[0].key == "logs/*.log"
        assert removed_items[0].severity == "error"

    def test_artifact_path_added(self, temp_workspace: Path) -> None:
        """测试 artifact_path added drift"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-01",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["test"],
                "artifact_archive": {
                    "required_artifact_paths": ["coverage.xml"],
                },
            },
        }
        write_contract(temp_workspace, contract)

        # 额外上传了 extra-report.json
        workflow = {
            "name": "CI",
            "jobs": {
                "test": {
                    "name": "Test",
                    "steps": [
                        {
                            "name": "Upload coverage",
                            "uses": "actions/upload-artifact@v4",
                            "with": {
                                "name": "coverage",
                                "path": "coverage.xml",
                            },
                        },
                        {
                            "name": "Upload extra report",
                            "uses": "actions/upload-artifact@v4",
                            "with": {
                                "name": "extra",
                                "path": "extra-report.json",
                            },
                        },
                    ],
                },
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        analyzer = WorkflowContractDriftAnalyzer(
            contract_path=temp_workspace / "scripts" / "ci" / "workflow_contract.v1.json",
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        assert report.has_drift is True
        added_items = [
            i
            for i in report.drift_items
            if i.drift_type == "added" and i.category == "artifact_path"
        ]
        assert len(added_items) == 1
        assert added_items[0].key == "extra-report.json"
        assert added_items[0].severity == "info"


# ============================================================================
# Test Cases for make_target Drift
# ============================================================================


class TestMakeTargetDrift:
    """make_target drift 测试"""

    def test_make_target_removed_from_makefile(self, temp_workspace: Path) -> None:
        """测试 make_target removed drift (Makefile 中缺少)"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-01",
            "make": {
                "targets_required": ["lint", "test", "build"],
            },
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint"],
            },
        }
        write_contract(temp_workspace, contract)

        # 创建 Makefile，缺少 build target
        makefile_path = temp_workspace / "Makefile"
        makefile_path.write_text(
            """.PHONY: lint test

lint:
\t@echo "Running lint"

test:
\t@echo "Running test"
"""
        )

        workflow = {
            "name": "CI",
            "jobs": {
                "lint": {
                    "name": "Lint",
                    "steps": [{"name": "Checkout"}, {"run": "make lint"}],
                },
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        analyzer = WorkflowContractDriftAnalyzer(
            contract_path=temp_workspace / "scripts" / "ci" / "workflow_contract.v1.json",
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        assert report.has_drift is True
        removed_items = [
            i
            for i in report.drift_items
            if i.drift_type == "removed" and i.category == "make_target"
        ]
        assert len(removed_items) == 1
        assert removed_items[0].key == "build"
        assert removed_items[0].severity == "error"
        assert removed_items[0].location == "Makefile"

    def test_make_target_added_in_workflow(self, temp_workspace: Path) -> None:
        """测试 make_target added drift (workflow 中调用但 contract 未声明)"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-01",
            "make": {
                "targets_required": ["lint"],
            },
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint"],
            },
        }
        write_contract(temp_workspace, contract)

        # 创建 Makefile
        makefile_path = temp_workspace / "Makefile"
        makefile_path.write_text(
            """.PHONY: lint test

lint:
\t@echo "Running lint"

test:
\t@echo "Running test"
"""
        )

        # workflow 中调用了 test target，但 contract 未声明
        workflow = {
            "name": "CI",
            "jobs": {
                "lint": {
                    "name": "Lint",
                    "steps": [
                        {"name": "Checkout"},
                        {"run": "make lint"},
                        {"run": "make test"},  # 未在 contract 中声明
                    ],
                },
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        analyzer = WorkflowContractDriftAnalyzer(
            contract_path=temp_workspace / "scripts" / "ci" / "workflow_contract.v1.json",
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        assert report.has_drift is True
        added_items = [
            i for i in report.drift_items if i.drift_type == "added" and i.category == "make_target"
        ]
        assert len(added_items) == 1
        assert added_items[0].key == "test"
        assert added_items[0].severity == "warning"


# ============================================================================
# Test Cases for label Drift
# ============================================================================


class TestLabelDrift:
    """label drift 测试"""

    def test_label_removed_from_script(self, temp_workspace: Path) -> None:
        """测试 label removed drift (脚本中缺少)"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-01",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint"],
                "labels": ["skip-ci", "urgent", "needs-review"],
            },
        }
        write_contract(temp_workspace, contract)

        # 创建 gh_pr_labels_to_outputs.py，缺少 needs-review
        scripts_ci = temp_workspace / "scripts" / "ci"
        scripts_ci.mkdir(parents=True, exist_ok=True)
        label_script = scripts_ci / "gh_pr_labels_to_outputs.py"
        label_script.write_text(
            '''#!/usr/bin/env python3
"""PR labels to outputs script"""

LABEL_SKIP_CI = "skip-ci"
LABEL_URGENT = "urgent"
# LABEL_NEEDS_REVIEW missing!

def main():
    pass
'''
        )

        workflow = {
            "name": "CI",
            "jobs": {"lint": {"name": "Lint", "steps": []}},
        }
        write_workflow(temp_workspace, "ci", workflow)

        analyzer = WorkflowContractDriftAnalyzer(
            contract_path=temp_workspace / "scripts" / "ci" / "workflow_contract.v1.json",
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        assert report.has_drift is True
        removed_items = [
            i for i in report.drift_items if i.drift_type == "removed" and i.category == "label"
        ]
        assert len(removed_items) == 1
        assert removed_items[0].key == "needs-review"
        assert removed_items[0].severity == "error"

    def test_label_added_in_script(self, temp_workspace: Path) -> None:
        """测试 label added drift (脚本中有但 contract 未声明)"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-01",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint"],
                "labels": ["skip-ci"],
            },
        }
        write_contract(temp_workspace, contract)

        # 创建 gh_pr_labels_to_outputs.py，多了 urgent label
        scripts_ci = temp_workspace / "scripts" / "ci"
        scripts_ci.mkdir(parents=True, exist_ok=True)
        label_script = scripts_ci / "gh_pr_labels_to_outputs.py"
        label_script.write_text(
            '''#!/usr/bin/env python3
"""PR labels to outputs script"""

LABEL_SKIP_CI = "skip-ci"
LABEL_URGENT = "urgent"  # 未在 contract 中声明

def main():
    pass
'''
        )

        workflow = {
            "name": "CI",
            "jobs": {"lint": {"name": "Lint", "steps": []}},
        }
        write_workflow(temp_workspace, "ci", workflow)

        analyzer = WorkflowContractDriftAnalyzer(
            contract_path=temp_workspace / "scripts" / "ci" / "workflow_contract.v1.json",
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        assert report.has_drift is True
        added_items = [
            i for i in report.drift_items if i.drift_type == "added" and i.category == "label"
        ]
        assert len(added_items) == 1
        assert added_items[0].key == "urgent"
        assert added_items[0].severity == "warning"


# ============================================================================
# Integration Tests
# ============================================================================


class TestIntegration:
    """集成测试"""

    def test_full_workflow_analysis_to_json(self, temp_workspace: Path) -> None:
        """完整流程测试：分析 -> JSON 输出"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-01",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint", "test"],
                "job_names": ["Lint Code", "Run Tests"],
                "required_jobs": [
                    {"id": "lint", "required_steps": ["Checkout", "Run lint"]},
                ],
                "required_env_vars": ["CI"],
            },
        }
        write_contract(temp_workspace, contract)

        # 创建有各种 drift 的 workflow
        workflow = {
            "name": "CI",
            "env": {},  # 缺少 CI env var
            "jobs": {
                "lint": {
                    "name": "Linter",  # changed from "Lint Code"
                    "steps": [
                        {"name": "Checkout"},
                        # 缺少 "Run lint" step
                    ],
                },
                "test": {"name": "Run Tests", "steps": []},
                "deploy": {"name": "Deploy", "steps": []},  # added
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        analyzer = WorkflowContractDriftAnalyzer(
            contract_path=temp_workspace / "scripts" / "ci" / "workflow_contract.v1.json",
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        # 验证检测到的 drift
        assert report.has_drift is True

        # 转换为 JSON 并验证
        output = format_json_output(report)
        data = json.loads(output)

        assert data["has_drift"] is True
        assert data["contract_version"] == "1.0.0"
        assert "ci" in data["workflows_checked"]

        # 验证 drift items 按 severity 存在
        severities = [item["severity"] for item in data["drift_items"]]
        assert "error" in severities or "warning" in severities

    def test_full_workflow_analysis_to_markdown(self, temp_workspace: Path) -> None:
        """完整流程测试：分析 -> Markdown 输出"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-01",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint"],
            },
        }
        write_contract(temp_workspace, contract)

        workflow = {
            "name": "CI",
            "jobs": {
                "lint": {"name": "Lint", "steps": []},
                "new_job": {"name": "New Job", "steps": []},  # added
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        analyzer = WorkflowContractDriftAnalyzer(
            contract_path=temp_workspace / "scripts" / "ci" / "workflow_contract.v1.json",
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()
        output = format_markdown_output(report)

        # 验证 Markdown 包含关键元素
        assert "# Workflow Contract Drift Report" in output
        assert "## Overview" in output
        assert "## Summary" in output
        assert "## Drift Details" in output
        assert "### ci" in output
        assert "new_job" in output
