#!/usr/bin/env python3
"""
Tests for suggest_workflow_contract_updates.py

覆盖以下场景：
- missing_job_id: workflow 中有但 contract 未声明的 job
- job_name_mismatch: job name 不匹配
- missing_step: contract 中声明但 workflow 中不存在的 step
- extra_job: contract 中声明但 workflow 中不存在的 job
- new_step_in_workflow: workflow 中存在但 contract 未记录的 step
- frozen_allowlist_update: 与 frozen allowlist 相似但不完全匹配
- format_json_output: JSON 输出格式验证
- format_markdown_output: Markdown 输出格式验证
- apply: 应用建议更新到 contract 文件
- apply_scope: 按范围过滤应用的更新
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.ci.suggest_workflow_contract_updates import (
    APPLY_SCOPE_JOBS,
    APPLY_SCOPE_STEPS,
    PRIORITY_HIGH,
    PRIORITY_INFO,
    PRIORITY_LOW,
    PRIORITY_MEDIUM,
    SUGGESTION_TYPE_EXTRA_JOB,
    SUGGESTION_TYPE_FROZEN_ALLOWLIST_UPDATE,
    SUGGESTION_TYPE_JOB_NAME_MISMATCH,
    SUGGESTION_TYPE_MISSING_JOB_ID,
    SUGGESTION_TYPE_MISSING_STEP,
    SUGGESTION_TYPE_NEW_STEP_IN_WORKFLOW,
    ApplyResult,
    ContractApplier,
    Suggestion,
    SuggestionReport,
    WorkflowContractSuggestionAnalyzer,
    format_apply_result,
    format_json_output,
    format_markdown_output,
)
from scripts.ci.workflow_contract_common import (
    classify_step_change,
    compute_set_diff,
    is_string_similar,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def temp_workspace(tmp_path: Path) -> Path:
    """创建临时 workspace 目录结构"""
    workflows_dir = tmp_path / ".github" / "workflows"
    workflows_dir.mkdir(parents=True)
    scripts_ci = tmp_path / "scripts" / "ci"
    scripts_ci.mkdir(parents=True)
    return tmp_path


def write_contract(workspace: Path, contract: dict[str, Any]) -> Path:
    """写入 contract JSON 文件"""
    contract_path = workspace / "scripts" / "ci" / "workflow_contract.v1.json"
    with open(contract_path, "w", encoding="utf-8") as f:
        json.dump(contract, f, indent=2)
    return contract_path


def write_workflow(workspace: Path, name: str, workflow: dict[str, Any]) -> Path:
    """写入 workflow YAML 文件"""
    workflows_dir = workspace / ".github" / "workflows"
    workflow_path = workflows_dir / f"{name}.yml"
    with open(workflow_path, "w", encoding="utf-8") as f:
        yaml.dump(workflow, f)
    return workflow_path


# ============================================================================
# Test Cases for Suggestion and SuggestionReport
# ============================================================================


class TestSuggestion:
    """Suggestion dataclass 测试"""

    def test_suggestion_defaults(self) -> None:
        """验证 Suggestion 默认值"""
        s = Suggestion(
            suggestion_type=SUGGESTION_TYPE_MISSING_JOB_ID,
            workflow="ci",
            key="new_job",
            message="Test message",
        )
        assert s.suggestion_type == SUGGESTION_TYPE_MISSING_JOB_ID
        assert s.workflow == "ci"
        assert s.key == "new_job"
        assert s.message == "Test message"
        assert s.priority == PRIORITY_MEDIUM
        assert s.contract_value is None
        assert s.actual_value is None
        assert s.location is None
        assert s.action is None

    def test_suggestion_full(self) -> None:
        """验证 Suggestion 完整字段"""
        s = Suggestion(
            suggestion_type=SUGGESTION_TYPE_JOB_NAME_MISMATCH,
            workflow="ci",
            key="lint",
            message="Job name mismatch",
            priority=PRIORITY_HIGH,
            contract_value="Lint Code",
            actual_value="Run Linter",
            location="jobs.lint.name",
            action="Update job_names[0]",
        )
        assert s.priority == PRIORITY_HIGH
        assert s.contract_value == "Lint Code"
        assert s.actual_value == "Run Linter"
        assert s.location == "jobs.lint.name"
        assert s.action == "Update job_names[0]"


class TestSuggestionReport:
    """SuggestionReport dataclass 测试"""

    def test_empty_report(self) -> None:
        """验证空报告"""
        report = SuggestionReport()
        assert report.has_suggestions is False
        assert report.suggestions == []
        assert report.summary == {}

    def test_add_suggestion(self) -> None:
        """验证 add_suggestion 方法"""
        report = SuggestionReport()
        s = Suggestion(
            suggestion_type=SUGGESTION_TYPE_MISSING_JOB_ID,
            workflow="ci",
            key="new_job",
            message="Test",
        )
        report.add_suggestion(s)

        assert report.has_suggestions is True
        assert len(report.suggestions) == 1
        assert report.summary == {SUGGESTION_TYPE_MISSING_JOB_ID: 1}

    def test_multiple_suggestions_summary(self) -> None:
        """验证多个建议的 summary 计数"""
        report = SuggestionReport()

        report.add_suggestion(
            Suggestion(
                suggestion_type=SUGGESTION_TYPE_MISSING_JOB_ID,
                workflow="ci",
                key="job1",
                message="",
            )
        )
        report.add_suggestion(
            Suggestion(
                suggestion_type=SUGGESTION_TYPE_MISSING_JOB_ID,
                workflow="ci",
                key="job2",
                message="",
            )
        )
        report.add_suggestion(
            Suggestion(
                suggestion_type=SUGGESTION_TYPE_JOB_NAME_MISMATCH,
                workflow="ci",
                key="lint",
                message="",
            )
        )
        report.add_suggestion(
            Suggestion(
                suggestion_type=SUGGESTION_TYPE_EXTRA_JOB,
                workflow="ci",
                key="old_job",
                message="",
            )
        )

        assert report.summary == {
            SUGGESTION_TYPE_MISSING_JOB_ID: 2,
            SUGGESTION_TYPE_JOB_NAME_MISMATCH: 1,
            SUGGESTION_TYPE_EXTRA_JOB: 1,
        }


# ============================================================================
# Test Cases for WorkflowContractSuggestionAnalyzer
# ============================================================================


class TestMissingJobId:
    """测试 missing_job_id 建议"""

    def test_workflow_has_extra_job(self, temp_workspace: Path) -> None:
        """workflow 中有但 contract 未声明的 job"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-02",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint", "test"],
                "job_names": ["Lint", "Test"],
            },
        }
        write_contract(temp_workspace, contract)

        workflow = {
            "name": "CI",
            "jobs": {
                "lint": {"name": "Lint", "steps": []},
                "test": {"name": "Test", "steps": []},
                "deploy": {"name": "Deploy", "steps": []},  # 新增的 job
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        analyzer = WorkflowContractSuggestionAnalyzer(
            contract_path=temp_workspace / "scripts" / "ci" / "workflow_contract.v1.json",
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        assert report.has_suggestions is True
        missing_job_suggestions = [
            s for s in report.suggestions if s.suggestion_type == SUGGESTION_TYPE_MISSING_JOB_ID
        ]
        assert len(missing_job_suggestions) == 1
        assert missing_job_suggestions[0].key == "deploy"
        assert missing_job_suggestions[0].priority == PRIORITY_HIGH
        assert "job_ids" in missing_job_suggestions[0].action


class TestExtraJob:
    """测试 extra_job 建议"""

    def test_contract_has_extra_job(self, temp_workspace: Path) -> None:
        """contract 中声明但 workflow 中不存在的 job"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-02",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint", "test", "deploy"],
                "job_names": ["Lint", "Test", "Deploy"],
            },
        }
        write_contract(temp_workspace, contract)

        workflow = {
            "name": "CI",
            "jobs": {
                "lint": {"name": "Lint", "steps": []},
                "test": {"name": "Test", "steps": []},
                # deploy 不存在
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        analyzer = WorkflowContractSuggestionAnalyzer(
            contract_path=temp_workspace / "scripts" / "ci" / "workflow_contract.v1.json",
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        assert report.has_suggestions is True
        extra_job_suggestions = [
            s for s in report.suggestions if s.suggestion_type == SUGGESTION_TYPE_EXTRA_JOB
        ]
        assert len(extra_job_suggestions) == 1
        assert extra_job_suggestions[0].key == "deploy"
        assert extra_job_suggestions[0].priority == PRIORITY_HIGH


class TestJobNameMismatch:
    """测试 job_name_mismatch 建议"""

    def test_job_name_changed(self, temp_workspace: Path) -> None:
        """job name 不匹配"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-02",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint"],
                "job_names": ["Lint Code"],
            },
        }
        write_contract(temp_workspace, contract)

        workflow = {
            "name": "CI",
            "jobs": {
                "lint": {"name": "Run Linter", "steps": []},  # name 变更
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        analyzer = WorkflowContractSuggestionAnalyzer(
            contract_path=temp_workspace / "scripts" / "ci" / "workflow_contract.v1.json",
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        assert report.has_suggestions is True
        mismatch_suggestions = [
            s for s in report.suggestions if s.suggestion_type == SUGGESTION_TYPE_JOB_NAME_MISMATCH
        ]
        assert len(mismatch_suggestions) == 1
        assert mismatch_suggestions[0].key == "lint"
        assert mismatch_suggestions[0].contract_value == "Lint Code"
        assert mismatch_suggestions[0].actual_value == "Run Linter"


class TestMissingStep:
    """测试 missing_step 建议"""

    def test_step_removed_from_workflow(self, temp_workspace: Path) -> None:
        """contract 中声明但 workflow 中不存在的 step"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-02",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint"],
                "job_names": ["Lint"],
                "required_jobs": [
                    {
                        "id": "lint",
                        "required_steps": ["Checkout", "Run lint", "Upload results"],
                    }
                ],
            },
        }
        write_contract(temp_workspace, contract)

        workflow = {
            "name": "CI",
            "jobs": {
                "lint": {
                    "name": "Lint",
                    "steps": [
                        {"name": "Checkout"},
                        {"name": "Run lint"},
                        # "Upload results" 被移除
                    ],
                },
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        analyzer = WorkflowContractSuggestionAnalyzer(
            contract_path=temp_workspace / "scripts" / "ci" / "workflow_contract.v1.json",
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        assert report.has_suggestions is True
        missing_step_suggestions = [
            s
            for s in report.suggestions
            if s.suggestion_type == SUGGESTION_TYPE_MISSING_STEP and "Upload results" in s.key
        ]
        assert len(missing_step_suggestions) == 1
        assert missing_step_suggestions[0].priority == PRIORITY_HIGH

    def test_step_renamed_with_fuzzy_match(self, temp_workspace: Path) -> None:
        """step 被重命名但可以模糊匹配"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-02",
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

        workflow = {
            "name": "CI",
            "jobs": {
                "lint": {
                    "name": "Lint",
                    "steps": [
                        {"name": "Run lint check (v2)"},  # 重命名但可模糊匹配
                    ],
                },
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        analyzer = WorkflowContractSuggestionAnalyzer(
            contract_path=temp_workspace / "scripts" / "ci" / "workflow_contract.v1.json",
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        assert report.has_suggestions is True
        missing_step_suggestions = [
            s for s in report.suggestions if s.suggestion_type == SUGGESTION_TYPE_MISSING_STEP
        ]
        assert len(missing_step_suggestions) == 1
        assert missing_step_suggestions[0].contract_value == "Run lint check"
        assert missing_step_suggestions[0].actual_value == "Run lint check (v2)"
        assert missing_step_suggestions[0].priority == PRIORITY_MEDIUM


class TestNewStepInWorkflow:
    """测试 new_step_in_workflow 建议"""

    def test_new_step_added_to_workflow(self, temp_workspace: Path) -> None:
        """workflow 中有但 contract 未记录的 step"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-02",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint"],
                "required_jobs": [
                    {
                        "id": "lint",
                        "required_steps": ["Checkout", "Run lint"],
                    }
                ],
            },
        }
        write_contract(temp_workspace, contract)

        workflow = {
            "name": "CI",
            "jobs": {
                "lint": {
                    "name": "Lint",
                    "steps": [
                        {"name": "Checkout"},
                        {"name": "Run lint"},
                        {"name": "Upload coverage"},  # 新增的 step
                    ],
                },
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        analyzer = WorkflowContractSuggestionAnalyzer(
            contract_path=temp_workspace / "scripts" / "ci" / "workflow_contract.v1.json",
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        assert report.has_suggestions is True
        new_step_suggestions = [
            s
            for s in report.suggestions
            if s.suggestion_type == SUGGESTION_TYPE_NEW_STEP_IN_WORKFLOW
        ]
        assert len(new_step_suggestions) == 1
        assert "Upload coverage" in new_step_suggestions[0].key
        assert new_step_suggestions[0].priority == PRIORITY_LOW


class TestFrozenAllowlistUpdate:
    """测试 frozen_allowlist_update 建议"""

    def test_similar_job_name_to_frozen(self, temp_workspace: Path) -> None:
        """job name 与 frozen_job_names 相似但不完全匹配"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-02",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint"],
                "job_names": ["Lint Code Check"],
            },
            "frozen_job_names": {
                "allowlist": ["Lint Code"],  # 与实际 "Lint Code Check" 相似
            },
        }
        write_contract(temp_workspace, contract)

        workflow = {
            "name": "CI",
            "jobs": {
                "lint": {"name": "Lint Code Check", "steps": []},
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        analyzer = WorkflowContractSuggestionAnalyzer(
            contract_path=temp_workspace / "scripts" / "ci" / "workflow_contract.v1.json",
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        frozen_suggestions = [
            s
            for s in report.suggestions
            if s.suggestion_type == SUGGESTION_TYPE_FROZEN_ALLOWLIST_UPDATE
        ]
        assert len(frozen_suggestions) >= 1
        assert frozen_suggestions[0].priority == PRIORITY_INFO

    def test_similar_step_name_to_frozen(self, temp_workspace: Path) -> None:
        """step name 与 frozen_step_text 相似但不完全匹配"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-02",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["test"],
                "required_jobs": [
                    {
                        "id": "test",
                        "required_steps": ["Run unit tests (v2)"],
                    }
                ],
            },
            "frozen_step_text": {
                "allowlist": ["Run unit tests"],  # 与实际 "Run unit tests (v2)" 相似
            },
        }
        write_contract(temp_workspace, contract)

        workflow = {
            "name": "CI",
            "jobs": {
                "test": {
                    "name": "Test",
                    "steps": [
                        {"name": "Run unit tests (v2)"},
                    ],
                },
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        analyzer = WorkflowContractSuggestionAnalyzer(
            contract_path=temp_workspace / "scripts" / "ci" / "workflow_contract.v1.json",
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        frozen_suggestions = [
            s
            for s in report.suggestions
            if s.suggestion_type == SUGGESTION_TYPE_FROZEN_ALLOWLIST_UPDATE
        ]
        assert len(frozen_suggestions) >= 1
        assert "frozen_step_text" in frozen_suggestions[0].location


class TestWorkflowFilter:
    """测试 workflow 过滤器"""

    def test_filter_specific_workflow(self, temp_workspace: Path) -> None:
        """只分析指定的 workflow"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-02",
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

        write_workflow(
            temp_workspace,
            "ci",
            {"name": "CI", "jobs": {"lint": {"name": "Lint"}, "test": {"name": "Test"}}},
        )
        write_workflow(
            temp_workspace,
            "nightly",
            {"name": "Nightly", "jobs": {"build": {"name": "Build"}, "deploy": {"name": "Deploy"}}},
        )

        # 只分析 ci
        analyzer = WorkflowContractSuggestionAnalyzer(
            contract_path=temp_workspace / "scripts" / "ci" / "workflow_contract.v1.json",
            workspace_root=temp_workspace,
            workflow_filter="ci",
        )
        report = analyzer.analyze()

        assert "ci" in report.workflows_checked
        assert "nightly" not in report.workflows_checked

        # 只有 ci 的 missing_job_id (test)
        missing_suggestions = [
            s for s in report.suggestions if s.suggestion_type == SUGGESTION_TYPE_MISSING_JOB_ID
        ]
        assert len(missing_suggestions) == 1
        assert missing_suggestions[0].key == "test"
        assert missing_suggestions[0].workflow == "ci"


class TestDynamicWorkflowDiscovery:
    """测试动态 workflow key 发现（使用 discover_workflow_keys）"""

    def test_discover_extra_workflow_key_staging(self, temp_workspace: Path) -> None:
        """验证能发现并处理额外的 workflow key（如 staging）"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-02",
            # 元数据字段（应被忽略）
            "$schema": "workflow_contract.v1.schema.json",
            "make": {"targets": ["ci"]},
            "frozen_job_names": {"allowlist": []},
            "_changelog_v1.0.0": "initial version",
            # 标准 workflow keys
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint"],
                "job_names": ["Lint"],
            },
            # 额外的 workflow key（staging）- 应被发现并处理
            "staging": {
                "file": ".github/workflows/staging.yml",
                "job_ids": ["deploy-staging"],
                "job_names": ["Deploy to Staging"],
            },
        }
        write_contract(temp_workspace, contract)

        # 创建对应的 workflow 文件
        write_workflow(
            temp_workspace,
            "ci",
            {"name": "CI", "jobs": {"lint": {"name": "Lint", "steps": []}}},
        )
        write_workflow(
            temp_workspace,
            "staging",
            {
                "name": "Staging",
                "jobs": {
                    "deploy-staging": {"name": "Deploy to Staging", "steps": []},
                    "new-job": {"name": "New Staging Job", "steps": []},  # 新增的 job
                },
            },
        )

        analyzer = WorkflowContractSuggestionAnalyzer(
            contract_path=temp_workspace / "scripts" / "ci" / "workflow_contract.v1.json",
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        # 验证 staging workflow 被发现并分析
        assert "staging" in report.workflows_checked
        assert "ci" in report.workflows_checked

        # 验证 staging 中的 new-job 被检测为 missing_job_id
        missing_suggestions = [
            s
            for s in report.suggestions
            if s.suggestion_type == SUGGESTION_TYPE_MISSING_JOB_ID and s.workflow == "staging"
        ]
        assert len(missing_suggestions) == 1
        assert missing_suggestions[0].key == "new-job"

    def test_metadata_keys_excluded(self, temp_workspace: Path) -> None:
        """验证 metadata keys 被正确排除（不作为 workflow 处理）"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-02",
            # 这些 metadata 字段不应被当作 workflow 处理
            "$schema": "workflow_contract.v1.schema.json",
            "description": "Test contract",
            "make": {"file": ".github/workflows/make.yml"},  # 有 file 字段但是 metadata
            "frozen_step_text": {"file": "should-not-match", "allowlist": []},
            "frozen_job_names": {"file": "should-not-match", "allowlist": []},
            "step_name_aliases": {"file": "should-not-match"},
            "_changelog_v1.0.0": {"file": "should-not-match"},
            "_note": {"file": "should-not-match"},
            # 只有这个是真正的 workflow
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint"],
            },
        }
        write_contract(temp_workspace, contract)

        write_workflow(
            temp_workspace,
            "ci",
            {"name": "CI", "jobs": {"lint": {"name": "Lint", "steps": []}}},
        )

        analyzer = WorkflowContractSuggestionAnalyzer(
            contract_path=temp_workspace / "scripts" / "ci" / "workflow_contract.v1.json",
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        # 只有 ci 被分析，metadata keys 全部被排除
        assert report.workflows_checked == ["ci"]

    def test_filter_works_with_dynamic_discovery(self, temp_workspace: Path) -> None:
        """验证 workflow 过滤器与动态发现配合正常"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-02",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint"],
            },
            "staging": {
                "file": ".github/workflows/staging.yml",
                "job_ids": ["deploy"],
            },
            "production": {
                "file": ".github/workflows/production.yml",
                "job_ids": ["release"],
            },
        }
        write_contract(temp_workspace, contract)

        write_workflow(
            temp_workspace,
            "ci",
            {"name": "CI", "jobs": {"lint": {"name": "Lint"}, "test": {"name": "Test"}}},
        )
        write_workflow(
            temp_workspace,
            "staging",
            {
                "name": "Staging",
                "jobs": {"deploy": {"name": "Deploy"}, "verify": {"name": "Verify"}},
            },
        )
        write_workflow(
            temp_workspace,
            "production",
            {"name": "Production", "jobs": {"release": {"name": "Release"}}},
        )

        # 只分析 staging（动态发现的 key）
        analyzer = WorkflowContractSuggestionAnalyzer(
            contract_path=temp_workspace / "scripts" / "ci" / "workflow_contract.v1.json",
            workspace_root=temp_workspace,
            workflow_filter="staging",
        )
        report = analyzer.analyze()

        assert report.workflows_checked == ["staging"]

        # 只有 staging 的 missing_job_id (verify)
        missing_suggestions = [
            s for s in report.suggestions if s.suggestion_type == SUGGESTION_TYPE_MISSING_JOB_ID
        ]
        assert len(missing_suggestions) == 1
        assert missing_suggestions[0].key == "verify"
        assert missing_suggestions[0].workflow == "staging"

    def test_filter_unknown_workflow_returns_empty(self, temp_workspace: Path) -> None:
        """验证过滤不存在的 workflow 返回空结果"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-02",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint"],
            },
        }
        write_contract(temp_workspace, contract)

        write_workflow(
            temp_workspace,
            "ci",
            {"name": "CI", "jobs": {"lint": {"name": "Lint"}}},
        )

        # 过滤一个不存在的 workflow key
        analyzer = WorkflowContractSuggestionAnalyzer(
            contract_path=temp_workspace / "scripts" / "ci" / "workflow_contract.v1.json",
            workspace_root=temp_workspace,
            workflow_filter="nonexistent",
        )
        report = analyzer.analyze()

        assert report.workflows_checked == []
        assert len(report.suggestions) == 0


class TestNoSuggestions:
    """测试无建议的情况"""

    def test_contract_in_sync(self, temp_workspace: Path) -> None:
        """contract 与 workflow 完全同步"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-02",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint", "test"],
                "job_names": ["Lint", "Test"],
                "required_jobs": [
                    {"id": "lint", "required_steps": ["Checkout", "Run lint"]},
                    {"id": "test", "required_steps": ["Checkout", "Run tests"]},
                ],
            },
        }
        write_contract(temp_workspace, contract)

        workflow = {
            "name": "CI",
            "jobs": {
                "lint": {
                    "name": "Lint",
                    "steps": [{"name": "Checkout"}, {"name": "Run lint"}],
                },
                "test": {
                    "name": "Test",
                    "steps": [{"name": "Checkout"}, {"name": "Run tests"}],
                },
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        analyzer = WorkflowContractSuggestionAnalyzer(
            contract_path=temp_workspace / "scripts" / "ci" / "workflow_contract.v1.json",
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        # 没有高优先级的建议
        high_priority = [s for s in report.suggestions if s.priority == PRIORITY_HIGH]
        assert len(high_priority) == 0


# ============================================================================
# Test Cases for format_json_output
# ============================================================================


class TestFormatJsonOutput:
    """format_json_output 测试"""

    def test_json_output_field_completeness(self) -> None:
        """验证 JSON 输出字段完整性"""
        report = SuggestionReport()
        report.contract_version = "1.0.0"
        report.contract_last_updated = "2026-02-02"
        report.report_generated_at = "2026-02-02T10:00:00"
        report.workflows_checked = ["ci"]

        report.add_suggestion(
            Suggestion(
                suggestion_type=SUGGESTION_TYPE_MISSING_JOB_ID,
                workflow="ci",
                key="new_job",
                message="Test message",
                priority=PRIORITY_HIGH,
                actual_value="New Job",
                location="jobs.new_job",
                action="Add to job_ids",
            )
        )

        output = format_json_output(report)
        data = json.loads(output)

        # 验证顶层字段
        assert "has_suggestions" in data
        assert "contract_version" in data
        assert "contract_last_updated" in data
        assert "report_generated_at" in data
        assert "workflows_checked" in data
        assert "summary" in data
        assert "suggestion_count" in data
        assert "suggestions" in data

        # 验证字段值
        assert data["has_suggestions"] is True
        assert data["contract_version"] == "1.0.0"
        assert data["suggestion_count"] == 1

        # 验证 suggestions 字段完整性
        assert len(data["suggestions"]) == 1
        item = data["suggestions"][0]
        assert item["suggestion_type"] == SUGGESTION_TYPE_MISSING_JOB_ID
        assert item["workflow"] == "ci"
        assert item["key"] == "new_job"
        assert item["priority"] == PRIORITY_HIGH
        assert item["action"] == "Add to job_ids"

    def test_json_output_empty_report(self) -> None:
        """验证空报告的 JSON 输出"""
        report = SuggestionReport()
        report.contract_version = "1.0.0"
        report.contract_last_updated = "2026-02-02"

        output = format_json_output(report)
        data = json.loads(output)

        assert data["has_suggestions"] is False
        assert data["suggestion_count"] == 0
        assert data["suggestions"] == []
        assert data["summary"] == {}


# ============================================================================
# Test Cases for format_markdown_output
# ============================================================================


class TestFormatMarkdownOutput:
    """format_markdown_output 测试"""

    def test_markdown_output_header(self) -> None:
        """验证 Markdown 输出包含 header"""
        report = SuggestionReport()
        report.contract_version = "1.0.0"
        report.contract_last_updated = "2026-02-02"
        report.report_generated_at = "2026-02-02T10:00:00"
        report.workflows_checked = ["ci"]

        output = format_markdown_output(report)

        assert "# Workflow Contract Update Suggestions" in output
        assert "## Overview" in output
        assert "**Contract Version**: 1.0.0" in output
        assert "**Contract Last Updated**: 2026-02-02" in output
        assert "**Report Generated**: 2026-02-02T10:00:00" in output
        assert "**Workflows Checked**: ci" in output

    def test_markdown_output_no_suggestions(self) -> None:
        """验证无建议时的 Markdown 输出"""
        report = SuggestionReport()
        report.contract_version = "1.0.0"
        report.workflows_checked = ["ci"]

        output = format_markdown_output(report)

        assert "**Has Suggestions**: No" in output
        assert "No suggestions - contract is in sync with workflows!" in output

    def test_markdown_output_with_suggestions(self) -> None:
        """验证有建议时的 Markdown 输出"""
        report = SuggestionReport()
        report.contract_version = "1.0.0"

        report.add_suggestion(
            Suggestion(
                suggestion_type=SUGGESTION_TYPE_MISSING_JOB_ID,
                workflow="ci",
                key="new_job",
                message="Test message",
                priority=PRIORITY_HIGH,
                action="Add to job_ids",
            )
        )

        output = format_markdown_output(report)

        assert "## Summary" in output
        assert "## 🔴 High Priority" in output
        assert "### ci" in output
        assert "## Detailed Actions" in output
        assert "new_job" in output

    def test_markdown_output_grouped_by_priority(self) -> None:
        """验证 Markdown 输出按优先级分组"""
        report = SuggestionReport()
        report.contract_version = "1.0.0"

        report.add_suggestion(
            Suggestion(
                suggestion_type=SUGGESTION_TYPE_MISSING_JOB_ID,
                workflow="ci",
                key="job1",
                message="High",
                priority=PRIORITY_HIGH,
            )
        )
        report.add_suggestion(
            Suggestion(
                suggestion_type=SUGGESTION_TYPE_JOB_NAME_MISMATCH,
                workflow="ci",
                key="job2",
                message="Medium",
                priority=PRIORITY_MEDIUM,
            )
        )
        report.add_suggestion(
            Suggestion(
                suggestion_type=SUGGESTION_TYPE_NEW_STEP_IN_WORKFLOW,
                workflow="ci",
                key="job3",
                message="Low",
                priority=PRIORITY_LOW,
            )
        )
        report.add_suggestion(
            Suggestion(
                suggestion_type=SUGGESTION_TYPE_FROZEN_ALLOWLIST_UPDATE,
                workflow="ci",
                key="job4",
                message="Info",
                priority=PRIORITY_INFO,
            )
        )

        output = format_markdown_output(report)

        # 验证优先级分组存在
        assert "## 🔴 High Priority" in output
        assert "## 🟡 Medium Priority" in output
        assert "## 🟢 Low Priority" in output
        assert "## ℹ️ Info" in output


# ============================================================================
# Integration Tests
# ============================================================================


class TestIntegration:
    """集成测试"""

    def test_full_analysis_to_json(self, temp_workspace: Path) -> None:
        """完整流程测试：分析 -> JSON 输出"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-02",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint", "test"],
                "job_names": ["Lint Code", "Run Tests"],
                "required_jobs": [
                    {"id": "lint", "required_steps": ["Checkout", "Run lint"]},
                ],
            },
        }
        write_contract(temp_workspace, contract)

        workflow = {
            "name": "CI",
            "jobs": {
                "lint": {
                    "name": "Linter",  # name changed
                    "steps": [{"name": "Checkout"}],  # "Run lint" missing
                },
                "test": {"name": "Run Tests", "steps": []},
                "deploy": {"name": "Deploy", "steps": []},  # new job
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        analyzer = WorkflowContractSuggestionAnalyzer(
            contract_path=temp_workspace / "scripts" / "ci" / "workflow_contract.v1.json",
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        # 转换为 JSON 并验证
        output = format_json_output(report)
        data = json.loads(output)

        assert data["has_suggestions"] is True
        assert data["contract_version"] == "1.0.0"
        assert "ci" in data["workflows_checked"]

        # 验证检测到多种类型的建议
        types = [s["suggestion_type"] for s in data["suggestions"]]
        assert SUGGESTION_TYPE_MISSING_JOB_ID in types  # deploy
        assert SUGGESTION_TYPE_JOB_NAME_MISMATCH in types  # lint name changed
        assert SUGGESTION_TYPE_MISSING_STEP in types  # "Run lint" missing

    def test_full_analysis_to_markdown(self, temp_workspace: Path) -> None:
        """完整流程测试：分析 -> Markdown 输出"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-02",
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
                "new_job": {"name": "New Job", "steps": []},
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        analyzer = WorkflowContractSuggestionAnalyzer(
            contract_path=temp_workspace / "scripts" / "ci" / "workflow_contract.v1.json",
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()
        output = format_markdown_output(report)

        # 验证 Markdown 包含关键元素
        assert "# Workflow Contract Update Suggestions" in output
        assert "## Overview" in output
        assert "## Summary" in output
        assert "new_job" in output


# ============================================================================
# Test Cases for ApplyResult
# ============================================================================


class TestApplyResult:
    """ApplyResult dataclass 测试"""

    def test_empty_result(self) -> None:
        """验证空结果"""
        result = ApplyResult()
        assert result.has_changes is False
        assert result.applied_count == 0
        assert result.skipped_count == 0

    def test_result_with_changes(self) -> None:
        """验证有更改的结果"""
        result = ApplyResult()
        result.applied_count = 2
        result.applied_suggestions = [
            Suggestion(
                suggestion_type=SUGGESTION_TYPE_MISSING_JOB_ID,
                workflow="ci",
                key="new_job",
                message="Test",
            )
        ]
        assert result.has_changes is True


# ============================================================================
# Test Cases for ContractApplier
# ============================================================================


class TestContractApplierMissingJobId:
    """测试 ContractApplier 处理 missing_job_id"""

    def test_apply_missing_job_id(self, temp_workspace: Path) -> None:
        """应用 missing_job_id 建议后 contract 包含新增的 job_id"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-02",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint"],
                "job_names": ["Lint"],
            },
        }
        contract_path = write_contract(temp_workspace, contract)

        workflow = {
            "name": "CI",
            "jobs": {
                "lint": {"name": "Lint", "steps": []},
                "deploy": {"name": "Deploy App", "steps": []},
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        # 分析
        analyzer = WorkflowContractSuggestionAnalyzer(
            contract_path=contract_path,
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        # 应用
        applier = ContractApplier(
            contract_path=contract_path,
            report=report,
            scopes={APPLY_SCOPE_JOBS},
        )
        result = applier.apply()

        assert result.has_changes is True
        assert result.applied_count >= 1

        # 验证 contract 内容
        updated_contract = json.loads(result.contract_after)
        assert "deploy" in updated_contract["ci"]["job_ids"]
        assert "Deploy App" in updated_contract["ci"]["job_names"]


class TestContractApplierExtraJob:
    """测试 ContractApplier 处理 extra_job"""

    def test_apply_extra_job_removal(self, temp_workspace: Path) -> None:
        """应用 extra_job 建议后 contract 不再包含多余的 job"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-02",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint", "old_job"],
                "job_names": ["Lint", "Old Job"],
                "required_jobs": [
                    {"id": "lint", "required_steps": ["Checkout"]},
                    {"id": "old_job", "required_steps": ["Run old"]},
                ],
            },
        }
        contract_path = write_contract(temp_workspace, contract)

        workflow = {
            "name": "CI",
            "jobs": {
                "lint": {"name": "Lint", "steps": [{"name": "Checkout"}]},
                # old_job 不存在
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        # 分析
        analyzer = WorkflowContractSuggestionAnalyzer(
            contract_path=contract_path,
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        # 应用
        applier = ContractApplier(
            contract_path=contract_path,
            report=report,
            scopes={APPLY_SCOPE_JOBS},
        )
        result = applier.apply()

        assert result.has_changes is True

        # 验证 contract 内容
        updated_contract = json.loads(result.contract_after)
        assert "old_job" not in updated_contract["ci"]["job_ids"]
        assert "Old Job" not in updated_contract["ci"]["job_names"]
        # required_jobs 中也应该移除
        rj_ids = [rj["id"] for rj in updated_contract["ci"]["required_jobs"]]
        assert "old_job" not in rj_ids


class TestContractApplierJobNameMismatch:
    """测试 ContractApplier 处理 job_name_mismatch"""

    def test_apply_job_name_update(self, temp_workspace: Path) -> None:
        """应用 job_name_mismatch 建议后 contract 的 job_name 被更新"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-02",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint"],
                "job_names": ["Old Lint Name"],
                "required_jobs": [
                    {"id": "lint", "name": "Old Lint Name", "required_steps": ["Checkout"]},
                ],
            },
        }
        contract_path = write_contract(temp_workspace, contract)

        workflow = {
            "name": "CI",
            "jobs": {
                "lint": {"name": "New Lint Name", "steps": [{"name": "Checkout"}]},
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        # 分析
        analyzer = WorkflowContractSuggestionAnalyzer(
            contract_path=contract_path,
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        # 应用
        applier = ContractApplier(
            contract_path=contract_path,
            report=report,
            scopes={APPLY_SCOPE_JOBS},
        )
        result = applier.apply()

        assert result.has_changes is True

        # 验证 contract 内容
        updated_contract = json.loads(result.contract_after)
        assert "New Lint Name" in updated_contract["ci"]["job_names"]
        assert updated_contract["ci"]["required_jobs"][0]["name"] == "New Lint Name"


class TestContractApplierMissingStep:
    """测试 ContractApplier 处理 missing_step（step 重命名）"""

    def test_apply_step_rename(self, temp_workspace: Path) -> None:
        """应用 missing_step 建议后 contract 的 step 被重命名"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-02",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint"],
                "job_names": ["Lint"],
                "required_jobs": [
                    {"id": "lint", "required_steps": ["Checkout", "Run lint check"]},
                ],
            },
        }
        contract_path = write_contract(temp_workspace, contract)

        workflow = {
            "name": "CI",
            "jobs": {
                "lint": {
                    "name": "Lint",
                    "steps": [
                        {"name": "Checkout"},
                        {"name": "Run lint check (v2)"},  # 重命名
                    ],
                },
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        # 分析
        analyzer = WorkflowContractSuggestionAnalyzer(
            contract_path=contract_path,
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        # 应用
        applier = ContractApplier(
            contract_path=contract_path,
            report=report,
            scopes={APPLY_SCOPE_STEPS},
        )
        result = applier.apply()

        assert result.has_changes is True

        # 验证 contract 内容
        updated_contract = json.loads(result.contract_after)
        steps = updated_contract["ci"]["required_jobs"][0]["required_steps"]
        assert "Run lint check (v2)" in steps
        assert "Run lint check" not in steps


class TestContractApplierNewStep:
    """测试 ContractApplier 处理 new_step_in_workflow"""

    def test_apply_new_step_low_priority_skipped(self, temp_workspace: Path) -> None:
        """new_step_in_workflow 是 LOW 优先级，默认不应用"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-02",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint"],
                "job_names": ["Lint"],
                "required_jobs": [
                    {"id": "lint", "required_steps": ["Checkout"]},
                ],
            },
        }
        contract_path = write_contract(temp_workspace, contract)

        workflow = {
            "name": "CI",
            "jobs": {
                "lint": {
                    "name": "Lint",
                    "steps": [
                        {"name": "Checkout"},
                        {"name": "New step"},  # 新 step（LOW 优先级）
                    ],
                },
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        # 分析
        analyzer = WorkflowContractSuggestionAnalyzer(
            contract_path=contract_path,
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        # 应用
        applier = ContractApplier(
            contract_path=contract_path,
            report=report,
            scopes={APPLY_SCOPE_STEPS},
        )
        result = applier.apply()

        # LOW 优先级不应用
        assert result.applied_count == 0
        assert result.skipped_count >= 1


class TestContractApplierScopeFiltering:
    """测试 ContractApplier 的 scope 过滤功能"""

    def test_scope_filtering_jobs_only(self, temp_workspace: Path) -> None:
        """只应用 jobs scope 的建议"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-02",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint"],
                "job_names": ["Old Name"],
                "required_jobs": [
                    {"id": "lint", "required_steps": ["Old step"]},
                ],
            },
        }
        contract_path = write_contract(temp_workspace, contract)

        workflow = {
            "name": "CI",
            "jobs": {
                "lint": {
                    "name": "New Name",  # job_name 变更
                    "steps": [
                        {"name": "New step"},  # step 变更
                    ],
                },
                "deploy": {"name": "Deploy", "steps": []},  # 新 job
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        # 分析
        analyzer = WorkflowContractSuggestionAnalyzer(
            contract_path=contract_path,
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        # 只应用 jobs scope
        applier = ContractApplier(
            contract_path=contract_path,
            report=report,
            scopes={APPLY_SCOPE_JOBS},
        )
        result = applier.apply()

        # 验证只有 jobs 相关的建议被应用
        updated_contract = json.loads(result.contract_after)

        # jobs 应该被更新
        assert "deploy" in updated_contract["ci"]["job_ids"]
        assert "New Name" in updated_contract["ci"]["job_names"]

        # steps 不应该被更新（因为 scope 不包含 steps）
        steps = updated_contract["ci"]["required_jobs"][0]["required_steps"]
        assert "Old step" in steps


class TestContractApplierDiff:
    """测试 ContractApplier 生成的 diff"""

    def test_diff_generation(self, temp_workspace: Path) -> None:
        """验证 diff 生成正确"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-02",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint"],
                "job_names": ["Lint"],
            },
        }
        contract_path = write_contract(temp_workspace, contract)

        workflow = {
            "name": "CI",
            "jobs": {
                "lint": {"name": "Lint", "steps": []},
                "deploy": {"name": "Deploy", "steps": []},
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        # 分析
        analyzer = WorkflowContractSuggestionAnalyzer(
            contract_path=contract_path,
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        # 应用
        applier = ContractApplier(
            contract_path=contract_path,
            report=report,
        )
        result = applier.apply()

        # 验证 diff 包含关键信息
        assert result.diff != ""
        assert "deploy" in result.diff
        assert "---" in result.diff
        assert "+++" in result.diff


class TestContractApplierSave:
    """测试 ContractApplier 的保存功能"""

    def test_save_updates_file(self, temp_workspace: Path) -> None:
        """验证 save 方法正确更新文件"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-02",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint"],
                "job_names": ["Lint"],
            },
        }
        contract_path = write_contract(temp_workspace, contract)

        workflow = {
            "name": "CI",
            "jobs": {
                "lint": {"name": "Lint", "steps": []},
                "deploy": {"name": "Deploy", "steps": []},
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        # 分析
        analyzer = WorkflowContractSuggestionAnalyzer(
            contract_path=contract_path,
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        # 应用并保存
        applier = ContractApplier(
            contract_path=contract_path,
            report=report,
        )
        result = applier.apply()
        applier.save(result)

        # 验证文件已更新
        with open(contract_path, encoding="utf-8") as f:
            saved_contract = json.load(f)

        assert "deploy" in saved_contract["ci"]["job_ids"]


class TestFormatApplyResult:
    """测试 format_apply_result 输出格式"""

    def test_format_with_changes(self) -> None:
        """验证有更改时的输出格式"""
        result = ApplyResult()
        result.applied_count = 2
        result.skipped_count = 1
        result.applied_suggestions = [
            Suggestion(
                suggestion_type=SUGGESTION_TYPE_MISSING_JOB_ID,
                workflow="ci",
                key="deploy",
                message="Test",
            ),
            Suggestion(
                suggestion_type=SUGGESTION_TYPE_JOB_NAME_MISMATCH,
                workflow="ci",
                key="lint",
                message="Test",
            ),
        ]
        result.skipped_suggestions = [
            Suggestion(
                suggestion_type=SUGGESTION_TYPE_NEW_STEP_IN_WORKFLOW,
                workflow="ci",
                key="lint/new_step",
                message="Test",
                priority=PRIORITY_LOW,
            ),
        ]
        result.diff = "--- before\n+++ after\n@@ -1 +1 @@\n-old\n+new"

        output = format_apply_result(result)

        assert "Applied: 2" in output
        assert "Skipped: 1" in output
        assert "Applied Changes:" in output
        assert "deploy" in output
        assert "lint" in output
        assert "Diff:" in output

    def test_format_no_changes(self) -> None:
        """验证无更改时的输出格式"""
        result = ApplyResult()
        result.applied_count = 0
        result.skipped_count = 0

        output = format_apply_result(result)

        assert "Applied: 0" in output
        assert "Skipped: 0" in output


# ============================================================================
# Test Cases for Apply with Real-world Fixtures
# ============================================================================


# ============================================================================
# Test Cases for Shared Diff Utilities (workflow_contract_common)
# ============================================================================


class TestComputeSetDiff:
    """测试 compute_set_diff 函数"""

    def test_compute_set_diff_basic(self) -> None:
        """基本的集合差异计算"""
        contract_set = {"a", "b", "c"}
        actual_set = {"b", "c", "d"}

        removed, added = compute_set_diff(contract_set, actual_set)

        assert removed == {"a"}
        assert added == {"d"}

    def test_compute_set_diff_identical(self) -> None:
        """相同集合无差异"""
        contract_set = {"a", "b", "c"}
        actual_set = {"a", "b", "c"}

        removed, added = compute_set_diff(contract_set, actual_set)

        assert removed == set()
        assert added == set()

    def test_compute_set_diff_empty_contract(self) -> None:
        """空合约集合"""
        contract_set: set[str] = set()
        actual_set = {"a", "b"}

        removed, added = compute_set_diff(contract_set, actual_set)

        assert removed == set()
        assert added == {"a", "b"}

    def test_compute_set_diff_empty_actual(self) -> None:
        """空实际集合"""
        contract_set = {"a", "b"}
        actual_set: set[str] = set()

        removed, added = compute_set_diff(contract_set, actual_set)

        assert removed == {"a", "b"}
        assert added == set()


class TestIsStringSimilar:
    """测试 is_string_similar 函数"""

    def test_exact_match_case_insensitive(self) -> None:
        """完全相同（忽略大小写）"""
        assert is_string_similar("Run lint", "run lint") is True
        assert is_string_similar("RUN LINT", "run lint") is True

    def test_substring_contains(self) -> None:
        """包含关系"""
        assert is_string_similar("Run lint check", "Run lint") is True
        assert is_string_similar("Run lint", "Run lint check (v2)") is True

    def test_word_overlap(self) -> None:
        """词语重叠"""
        assert is_string_similar("Run unit tests", "Execute unit tests") is True
        assert is_string_similar("Build project", "Build artifacts") is True

    def test_not_similar(self) -> None:
        """不相似的字符串"""
        assert is_string_similar("Build", "Deploy") is False
        assert is_string_similar("Lint code", "Test coverage") is False

    def test_custom_threshold(self) -> None:
        """自定义阈值"""
        # 低阈值更容易匹配
        assert is_string_similar("a b c d", "a e f g", threshold=0.25) is True
        # 高阈值更难匹配
        assert is_string_similar("a b c d", "a b e f", threshold=0.8) is False


class TestClassifyStepChange:
    """测试 classify_step_change 函数"""

    def test_exact_match(self) -> None:
        """精确匹配"""
        change_type, matched = classify_step_change("Run lint", ["Run lint", "Run tests", "Deploy"])
        assert change_type == "exact"
        assert matched == "Run lint"

    def test_fuzzy_match(self) -> None:
        """模糊匹配（重命名）"""
        change_type, matched = classify_step_change(
            "Run lint check", ["Run lint check (v2)", "Run tests"]
        )
        assert change_type == "fuzzy"
        assert matched == "Run lint check (v2)"

    def test_removed(self) -> None:
        """未找到匹配"""
        change_type, matched = classify_step_change(
            "Deploy to production", ["Run lint", "Run tests"]
        )
        assert change_type == "removed"
        assert matched is None

    def test_fuzzy_match_stability(self) -> None:
        """验证 fuzzy match 的稳定性"""
        actual_steps = ["Setup", "Run lint check (v2)", "Cleanup", "Run lint (old)"]

        # 多次调用应该返回相同结果
        results = []
        for _ in range(5):
            change_type, matched = classify_step_change("Run lint check", actual_steps)
            results.append((change_type, matched))

        # 所有结果应该相同
        assert all(r == results[0] for r in results)
        assert results[0][0] == "fuzzy"


class TestApplyWithFixtures:
    """使用模拟真实场景的 fixtures 测试 apply 功能"""

    def test_apply_multiple_changes(self, temp_workspace: Path) -> None:
        """测试同时应用多种类型的更改"""
        contract = {
            "version": "1.0.0",
            "last_updated": "2026-02-02",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint", "test", "old_deploy"],
                "job_names": ["Lint", "Test", "Old Deploy"],
                "required_jobs": [
                    {"id": "lint", "name": "Lint", "required_steps": ["Checkout", "Run lint"]},
                    {"id": "test", "name": "Test", "required_steps": ["Checkout", "Run tests"]},
                    {"id": "old_deploy", "name": "Old Deploy", "required_steps": ["Deploy"]},
                ],
            },
        }
        contract_path = write_contract(temp_workspace, contract)

        workflow = {
            "name": "CI",
            "jobs": {
                "lint": {
                    "name": "Lint Code",  # name 变更
                    "steps": [
                        {"name": "Checkout"},
                        {"name": "Run lint (v2)"},  # step 重命名
                    ],
                },
                "test": {
                    "name": "Test",
                    "steps": [{"name": "Checkout"}, {"name": "Run tests"}],
                },
                "new_job": {  # 新 job
                    "name": "New Job",
                    "steps": [{"name": "Do something"}],
                },
                # old_deploy 被移除
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        # 分析
        analyzer = WorkflowContractSuggestionAnalyzer(
            contract_path=contract_path,
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        # 应用所有 scope
        applier = ContractApplier(
            contract_path=contract_path,
            report=report,
        )
        result = applier.apply()

        # 验证所有更改
        updated_contract = json.loads(result.contract_after)

        # 新 job 被添加
        assert "new_job" in updated_contract["ci"]["job_ids"]
        assert "New Job" in updated_contract["ci"]["job_names"]

        # 旧 job 被移除
        assert "old_deploy" not in updated_contract["ci"]["job_ids"]

        # job name 被更新
        lint_idx = updated_contract["ci"]["job_ids"].index("lint")
        assert updated_contract["ci"]["job_names"][lint_idx] == "Lint Code"

        # step 被更新
        lint_job = next(rj for rj in updated_contract["ci"]["required_jobs"] if rj["id"] == "lint")
        assert "Run lint (v2)" in lint_job["required_steps"]
        assert "Run lint" not in lint_job["required_steps"]

    def test_apply_preserves_metadata(self, temp_workspace: Path) -> None:
        """验证 apply 保持 metadata 字段（如 _changelog）不变"""
        contract = {
            "$schema": "workflow_contract.v1.schema.json",
            "version": "1.0.0",
            "description": "Test contract",
            "last_updated": "2026-02-02",
            "_changelog_v1.0.0": "Initial version",
            "_comment": "This is a comment",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint"],
                "job_names": ["Lint"],
            },
        }
        contract_path = write_contract(temp_workspace, contract)

        workflow = {
            "name": "CI",
            "jobs": {
                "lint": {"name": "Lint", "steps": []},
                "deploy": {"name": "Deploy", "steps": []},
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        # 分析
        analyzer = WorkflowContractSuggestionAnalyzer(
            contract_path=contract_path,
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()

        # 应用
        applier = ContractApplier(
            contract_path=contract_path,
            report=report,
        )
        result = applier.apply()

        # 验证 metadata 保持不变
        updated_contract = json.loads(result.contract_after)
        assert updated_contract["$schema"] == "workflow_contract.v1.schema.json"
        assert updated_contract["version"] == "1.0.0"
        assert updated_contract["description"] == "Test contract"
        assert updated_contract["_changelog_v1.0.0"] == "Initial version"
        assert updated_contract["_comment"] == "This is a comment"

        # 同时验证更改被应用
        assert "deploy" in updated_contract["ci"]["job_ids"]
