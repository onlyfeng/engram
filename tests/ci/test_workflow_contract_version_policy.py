#!/usr/bin/env python3
"""
tests/ci/test_workflow_contract_version_policy.py

单元测试：check_workflow_contract_version_policy.py 的版本策略检查功能

测试范围：
1. 无关键变更应 skip
2. 关键变更但 version 未升应 fail
3. version 升但 last_updated 未变应 fail
4. 版本表缺失该版本应 fail
5. Makefile 非 CI 相关 diff 不触发
6. ci.yml 变更必须 bump
7. scripts/ci/*.py 关键校验器触发
8. 非关键 docs 不触发
9. Makefile CI 相关 target 识别准确性
10. bump 但未更新版本表的报错信息稳定
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

# 导入被测模块
from scripts.ci.check_workflow_contract_version_policy import (
    ALL_CRITICAL_RULES,
    CRITICAL_TOOLING_RULES,
    CRITICAL_WORKFLOW_RULES,
    MAKEFILE_CI_KEYWORDS,
    MAKEFILE_RULE_DESCRIPTION,
    CriticalFileRule,
    RuleGroups,
    VersionPolicyCheckInput,
    VersionPolicyErrorTypes,
    WorkflowContractVersionChecker,
    check_version_policy_pure,
    filter_critical_files_with_reasons,
    get_matching_rule,
    is_critical_tooling_script,
    is_non_critical_doc,
    is_workflow_file,
)

# ============================================================================
# Fixtures
# ============================================================================


def create_project_structure(
    contract_data: dict[str, Any],
    doc_content: str,
) -> Path:
    """创建临时项目结构，包含 contract JSON 和 doc Markdown 文件

    Args:
        contract_data: workflow_contract.v1.json 的内容
        doc_content: contract.md 的内容

    Returns:
        project_root 路径
    """
    # 创建临时目录作为 project_root
    temp_dir = Path(tempfile.mkdtemp())

    # 创建 scripts/ci 目录并写入 contract JSON
    contract_dir = temp_dir / "scripts" / "ci"
    contract_dir.mkdir(parents=True, exist_ok=True)
    contract_path = contract_dir / "workflow_contract.v1.json"
    with open(contract_path, "w", encoding="utf-8") as f:
        json.dump(contract_data, f, indent=2)

    # 创建 docs/ci_nightly_workflow_refactor 目录并写入 doc Markdown
    doc_dir = temp_dir / "docs" / "ci_nightly_workflow_refactor"
    doc_dir.mkdir(parents=True, exist_ok=True)
    doc_path = doc_dir / "contract.md"
    with open(doc_path, "w", encoding="utf-8") as f:
        f.write(doc_content)

    return temp_dir


def make_contract(version: str, last_updated: str) -> dict[str, Any]:
    """创建最小的 contract JSON 数据

    Args:
        version: 版本号
        last_updated: 最后更新日期

    Returns:
        contract dict
    """
    return {
        "version": version,
        "last_updated": last_updated,
        "ci": {
            "file": ".github/workflows/ci.yml",
            "job_ids": ["test"],
            "job_names": ["Test"],
        },
    }


def make_doc_with_version_table(versions: list[tuple[str, str]]) -> str:
    """创建包含版本控制表的文档

    Args:
        versions: 版本列表，每项为 (version, date)

    Returns:
        文档内容
    """
    table_rows = "\n".join([f"| v{ver} | {date} | 变更说明 |" for ver, date in versions])
    return f"""# CI/Nightly Workflow Contract

## 13. 版本控制表

| 版本 | 日期 | 变更说明 |
|------|------|----------|
{table_rows}
"""


# ============================================================================
# Test: 无关键变更应 skip
# ============================================================================


class TestNoCriticalChanges:
    """测试无关键变更时应 skip"""

    def test_no_changed_files_should_pass(self) -> None:
        """当没有变更文件时，应通过"""
        contract = make_contract("2.5.0", "2026-02-01")
        doc = make_doc_with_version_table([("2.5.0", "2026-02-01")])
        project_root = create_project_structure(contract, doc)

        checker = WorkflowContractVersionChecker(project_root)
        result = checker.check(changed_files=[])

        assert result.success is True
        assert len(result.errors) == 0
        assert len(result.changed_critical_files) == 0

    def test_non_critical_files_changed_should_pass(self) -> None:
        """当只有非关键文件变更时，应通过"""
        contract = make_contract("2.5.0", "2026-02-01")
        doc = make_doc_with_version_table([("2.5.0", "2026-02-01")])
        project_root = create_project_structure(contract, doc)

        checker = WorkflowContractVersionChecker(project_root)
        result = checker.check(
            changed_files=[
                "src/some_module.py",
                "tests/test_something.py",
                "README.md",
            ]
        )

        assert result.success is True
        assert len(result.errors) == 0
        assert len(result.changed_critical_files) == 0


# ============================================================================
# Test: 关键变更但 version 未升应 fail
# ============================================================================


class TestVersionNotUpdated:
    """测试关键变更但 version 未升时应 fail"""

    def test_ci_yml_changed_version_not_updated_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """当 ci.yml 变更但 version 未更新时，应报错"""
        contract = make_contract("2.5.0", "2026-02-01")
        doc = make_doc_with_version_table([("2.5.0", "2026-02-01")])
        project_root = create_project_structure(contract, doc)

        # Mock get_old_file_content 返回相同版本的旧 contract
        old_contract = json.dumps(make_contract("2.5.0", "2026-02-01"))

        import scripts.ci.check_workflow_contract_version_policy as module

        monkeypatch.setattr(module, "get_old_file_content", lambda *args: old_contract)

        checker = WorkflowContractVersionChecker(project_root)
        result = checker.check(changed_files=[".github/workflows/ci.yml"])

        assert result.success is False
        version_errors = [e for e in result.errors if e.error_type == "version_not_updated"]
        assert len(version_errors) == 1
        assert "2.5.0" in version_errors[0].message

    def test_nightly_yml_changed_version_not_updated_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """当 nightly.yml 变更但 version 未更新时，应报错"""
        contract = make_contract("2.5.0", "2026-02-01")
        doc = make_doc_with_version_table([("2.5.0", "2026-02-01")])
        project_root = create_project_structure(contract, doc)

        old_contract = json.dumps(make_contract("2.5.0", "2026-02-01"))

        import scripts.ci.check_workflow_contract_version_policy as module

        monkeypatch.setattr(module, "get_old_file_content", lambda *args: old_contract)

        checker = WorkflowContractVersionChecker(project_root)
        result = checker.check(changed_files=[".github/workflows/nightly.yml"])

        assert result.success is False
        version_errors = [e for e in result.errors if e.error_type == "version_not_updated"]
        assert len(version_errors) == 1

    def test_contract_doc_changed_version_not_updated_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """当 ci_nightly 文档变更但 version 未更新时，应报错"""
        contract = make_contract("2.5.0", "2026-02-01")
        doc = make_doc_with_version_table([("2.5.0", "2026-02-01")])
        project_root = create_project_structure(contract, doc)

        old_contract = json.dumps(make_contract("2.5.0", "2026-02-01"))

        import scripts.ci.check_workflow_contract_version_policy as module

        monkeypatch.setattr(module, "get_old_file_content", lambda *args: old_contract)

        checker = WorkflowContractVersionChecker(project_root)
        result = checker.check(changed_files=["docs/ci_nightly_workflow_refactor/contract.md"])

        assert result.success is False
        version_errors = [e for e in result.errors if e.error_type == "version_not_updated"]
        assert len(version_errors) == 1


# ============================================================================
# Test: version 升但 last_updated 未变应 fail
# ============================================================================


class TestLastUpdatedNotUpdated:
    """测试 version 升但 last_updated 未变时应 fail"""

    def test_version_updated_but_last_updated_not_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """当 version 已更新但 last_updated 未更新时，应报错"""
        # 当前 contract 版本已升级，但 last_updated 与旧版相同
        contract = make_contract("2.6.0", "2026-02-01")  # version 升级了
        doc = make_doc_with_version_table([("2.6.0", "2026-02-02"), ("2.5.0", "2026-02-01")])
        project_root = create_project_structure(contract, doc)

        # 旧 contract 版本较低，last_updated 相同
        old_contract = json.dumps(make_contract("2.5.0", "2026-02-01"))

        import scripts.ci.check_workflow_contract_version_policy as module

        monkeypatch.setattr(module, "get_old_file_content", lambda *args: old_contract)

        checker = WorkflowContractVersionChecker(project_root)
        result = checker.check(changed_files=[".github/workflows/ci.yml"])

        assert result.success is False
        last_updated_errors = [
            e for e in result.errors if e.error_type == "last_updated_not_updated"
        ]
        assert len(last_updated_errors) == 1
        assert "last_updated" in last_updated_errors[0].message


# ============================================================================
# Test: 版本表缺失该版本应 fail
# ============================================================================


class TestVersionNotInDoc:
    """测试版本表缺失该版本时应 fail"""

    def test_version_not_in_doc_table_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """当版本不在文档版本控制表中时，应报错"""
        # contract 是 2.6.0，但文档版本表只有 2.5.0
        contract = make_contract("2.6.0", "2026-02-02")
        doc = make_doc_with_version_table([("2.5.0", "2026-02-01")])  # 缺少 2.6.0
        project_root = create_project_structure(contract, doc)

        # 旧 contract 版本较低
        old_contract = json.dumps(make_contract("2.5.0", "2026-02-01"))

        import scripts.ci.check_workflow_contract_version_policy as module

        monkeypatch.setattr(module, "get_old_file_content", lambda *args: old_contract)

        checker = WorkflowContractVersionChecker(project_root)
        result = checker.check(changed_files=[".github/workflows/ci.yml"])

        assert result.success is False
        doc_errors = [e for e in result.errors if e.error_type == "version_not_in_doc"]
        assert len(doc_errors) == 1
        assert "2.6.0" in doc_errors[0].message
        assert "contract.md" in doc_errors[0].message

    def test_all_checks_pass_when_properly_updated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """当所有条件都满足时，应通过"""
        # 正确更新的情况：version 升级、last_updated 更新、版本在文档中
        contract = make_contract("2.6.0", "2026-02-02")
        doc = make_doc_with_version_table(
            [
                ("2.6.0", "2026-02-02"),
                ("2.5.0", "2026-02-01"),
            ]
        )
        project_root = create_project_structure(contract, doc)

        # 旧 contract 版本较低
        old_contract = json.dumps(make_contract("2.5.0", "2026-02-01"))

        import scripts.ci.check_workflow_contract_version_policy as module

        monkeypatch.setattr(module, "get_old_file_content", lambda *args: old_contract)

        checker = WorkflowContractVersionChecker(project_root)
        result = checker.check(changed_files=[".github/workflows/ci.yml"])

        assert result.success is True
        assert len(result.errors) == 0
        assert result.version_updated is True
        assert result.last_updated_updated is True
        assert result.version_in_doc is True


# ============================================================================
# Test: Makefile 非 CI 相关 diff 不触发
# ============================================================================


class TestMakefileCIRelatedChange:
    """测试 Makefile 只有 CI 相关变更才触发版本检查"""

    def test_makefile_with_ci_keywords_triggers_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """当 Makefile 变更包含 CI 关键字时，应触发版本检查"""
        contract = make_contract("2.5.0", "2026-02-01")
        doc = make_doc_with_version_table([("2.5.0", "2026-02-01")])
        project_root = create_project_structure(contract, doc)

        # Mock diff 内容包含 CI 关键字
        ci_related_diff = """
+validate-workflows:
+    python scripts/ci/validate_workflows.py
"""

        old_contract = json.dumps(make_contract("2.5.0", "2026-02-01"))

        import scripts.ci.check_workflow_contract_version_policy as module

        monkeypatch.setattr(module, "get_old_file_content", lambda *args: old_contract)
        monkeypatch.setattr(module, "get_file_diff_content", lambda *args: ci_related_diff)

        checker = WorkflowContractVersionChecker(project_root)
        result = checker.check(changed_files=["Makefile"])

        # 应该触发版本检查，且因为版本未更新而失败
        assert "Makefile" in result.changed_critical_files
        assert result.success is False  # 因为版本未更新

    def test_makefile_without_ci_keywords_does_not_trigger(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """当 Makefile 变更不包含 CI 关键字时，不应触发版本检查"""
        contract = make_contract("2.5.0", "2026-02-01")
        doc = make_doc_with_version_table([("2.5.0", "2026-02-01")])
        project_root = create_project_structure(contract, doc)

        # Mock diff 内容不包含 CI 关键字
        non_ci_diff = """
+test-unit:
+    pytest tests/
+
+clean:
+    rm -rf __pycache__
"""

        import scripts.ci.check_workflow_contract_version_policy as module

        monkeypatch.setattr(module, "get_file_diff_content", lambda *args: non_ci_diff)

        checker = WorkflowContractVersionChecker(project_root)
        result = checker.check(changed_files=["Makefile"])

        # 不应触发版本检查
        assert "Makefile" not in result.changed_critical_files
        assert result.success is True  # 无关键变更，直接通过

    def test_makefile_ci_keywords_constant_defined(self) -> None:
        """验证 MAKEFILE_CI_KEYWORDS 常量已正确定义"""
        assert len(MAKEFILE_CI_KEYWORDS) >= 1
        assert "validate-workflows" in MAKEFILE_CI_KEYWORDS
        assert "ci:" in MAKEFILE_CI_KEYWORDS

    def test_makefile_with_workflow_contract_keyword(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """当 Makefile diff 包含 workflow-contract 关键字时，应触发检查"""
        contract = make_contract("2.5.0", "2026-02-01")
        doc = make_doc_with_version_table([("2.5.0", "2026-02-01")])
        project_root = create_project_structure(contract, doc)

        diff_with_workflow_contract = """
+check-workflow-contract:
+    python scripts/ci/check_workflow_contract_version_policy.py
"""

        old_contract = json.dumps(make_contract("2.5.0", "2026-02-01"))

        import scripts.ci.check_workflow_contract_version_policy as module

        monkeypatch.setattr(module, "get_old_file_content", lambda *args: old_contract)
        monkeypatch.setattr(
            module, "get_file_diff_content", lambda *args: diff_with_workflow_contract
        )

        checker = WorkflowContractVersionChecker(project_root)
        result = checker.check(changed_files=["Makefile"])

        assert "Makefile" in result.changed_critical_files


# ============================================================================
# Test: 工具脚本变更触发检查
# ============================================================================


class TestToolingScriptChanges:
    """测试工具脚本变更触发版本检查"""

    def test_validate_workflows_script_triggers_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """当 validate_workflows.py 变更时，应触发版本检查"""
        contract = make_contract("2.5.0", "2026-02-01")
        doc = make_doc_with_version_table([("2.5.0", "2026-02-01")])
        project_root = create_project_structure(contract, doc)

        old_contract = json.dumps(make_contract("2.5.0", "2026-02-01"))

        import scripts.ci.check_workflow_contract_version_policy as module

        monkeypatch.setattr(module, "get_old_file_content", lambda *args: old_contract)

        checker = WorkflowContractVersionChecker(project_root)
        result = checker.check(changed_files=["scripts/ci/validate_workflows.py"])

        assert "scripts/ci/validate_workflows.py" in result.changed_critical_files
        assert result.success is False  # 因为版本未更新

    def test_schema_file_triggers_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """当 schema 文件变更时，应触发版本检查"""
        contract = make_contract("2.5.0", "2026-02-01")
        doc = make_doc_with_version_table([("2.5.0", "2026-02-01")])
        project_root = create_project_structure(contract, doc)

        old_contract = json.dumps(make_contract("2.5.0", "2026-02-01"))

        import scripts.ci.check_workflow_contract_version_policy as module

        monkeypatch.setattr(module, "get_old_file_content", lambda *args: old_contract)

        checker = WorkflowContractVersionChecker(project_root)
        result = checker.check(changed_files=["scripts/ci/workflow_contract.v1.schema.json"])

        assert "scripts/ci/workflow_contract.v1.schema.json" in result.changed_critical_files


# ============================================================================
# Test: 版本表格式解析
# ============================================================================


class TestVersionTableParsing:
    """测试版本控制表的解析"""

    def test_version_with_v_prefix_matches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """测试带 v 前缀的版本号能正确匹配"""
        contract = make_contract("2.6.0", "2026-02-02")
        # 文档中版本带 v 前缀
        doc = """# Contract

## 13. 版本控制表

| 版本 | 日期 | 变更说明 |
|------|------|----------|
| v2.6.0 | 2026-02-02 | 新版本 |
| v2.5.0 | 2026-02-01 | 旧版本 |
"""
        project_root = create_project_structure(contract, doc)

        old_contract = json.dumps(make_contract("2.5.0", "2026-02-01"))

        import scripts.ci.check_workflow_contract_version_policy as module

        monkeypatch.setattr(module, "get_old_file_content", lambda *args: old_contract)

        checker = WorkflowContractVersionChecker(project_root)
        result = checker.check(changed_files=[".github/workflows/ci.yml"])

        assert result.version_in_doc is True

    def test_version_without_v_prefix_matches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """测试不带 v 前缀的版本号能正确匹配"""
        contract = make_contract("2.6.0", "2026-02-02")
        # 文档中版本不带 v 前缀
        doc = """# Contract

## 13. 版本控制表

| 版本 | 日期 | 变更说明 |
|------|------|----------|
| 2.6.0 | 2026-02-02 | 新版本 |
| 2.5.0 | 2026-02-01 | 旧版本 |
"""
        project_root = create_project_structure(contract, doc)

        old_contract = json.dumps(make_contract("2.5.0", "2026-02-01"))

        import scripts.ci.check_workflow_contract_version_policy as module

        monkeypatch.setattr(module, "get_old_file_content", lambda *args: old_contract)

        checker = WorkflowContractVersionChecker(project_root)
        result = checker.check(changed_files=[".github/workflows/ci.yml"])

        assert result.version_in_doc is True


# ============================================================================
# Test: 新文件场景
# ============================================================================


class TestNewFileScenario:
    """测试新文件场景（旧 commit 中不存在）"""

    def test_new_contract_file_treated_as_updated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """当 contract 文件是新文件时，视为已更新"""
        contract = make_contract("1.0.0", "2026-02-02")
        doc = make_doc_with_version_table([("1.0.0", "2026-02-02")])
        project_root = create_project_structure(contract, doc)

        import scripts.ci.check_workflow_contract_version_policy as module

        # 旧文件不存在，返回 None
        monkeypatch.setattr(module, "get_old_file_content", lambda *args: None)

        checker = WorkflowContractVersionChecker(project_root)
        result = checker.check(changed_files=[".github/workflows/ci.yml"])

        # 新文件视为已更新
        assert result.version_updated is True
        assert result.last_updated_updated is True


# ============================================================================
# Test: 错误信息清晰度
# ============================================================================


class TestErrorMessageClarity:
    """测试错误信息的清晰度"""

    def test_version_error_contains_suggestion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """版本错误应包含修复建议"""
        contract = make_contract("2.5.0", "2026-02-01")
        doc = make_doc_with_version_table([("2.5.0", "2026-02-01")])
        project_root = create_project_structure(contract, doc)

        old_contract = json.dumps(make_contract("2.5.0", "2026-02-01"))

        import scripts.ci.check_workflow_contract_version_policy as module

        monkeypatch.setattr(module, "get_old_file_content", lambda *args: old_contract)

        checker = WorkflowContractVersionChecker(project_root)
        result = checker.check(changed_files=[".github/workflows/ci.yml"])

        version_errors = [e for e in result.errors if e.error_type == "version_not_updated"]
        assert len(version_errors) == 1
        assert "SemVer" in version_errors[0].suggestion

    def test_doc_error_contains_example(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """文档缺失版本错误应包含添加示例"""
        contract = make_contract("2.6.0", "2026-02-02")
        doc = make_doc_with_version_table([("2.5.0", "2026-02-01")])
        project_root = create_project_structure(contract, doc)

        old_contract = json.dumps(make_contract("2.5.0", "2026-02-01"))

        import scripts.ci.check_workflow_contract_version_policy as module

        monkeypatch.setattr(module, "get_old_file_content", lambda *args: old_contract)

        checker = WorkflowContractVersionChecker(project_root)
        result = checker.check(changed_files=[".github/workflows/ci.yml"])

        doc_errors = [e for e in result.errors if e.error_type == "version_not_in_doc"]
        assert len(doc_errors) == 1
        # 建议中应包含如何添加版本行的示例
        assert "v2.6.0" in doc_errors[0].suggestion


# ============================================================================
# Test: trigger_reasons 字段
# ============================================================================


class TestTriggerReasons:
    """测试 trigger_reasons 功能"""

    def test_trigger_reasons_populated_for_workflow_files(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """workflow 文件变更应在 trigger_reasons 中记录原因"""
        contract = make_contract("2.5.0", "2026-02-01")
        doc = make_doc_with_version_table([("2.5.0", "2026-02-01")])
        project_root = create_project_structure(contract, doc)

        old_contract = json.dumps(make_contract("2.5.0", "2026-02-01"))

        import scripts.ci.check_workflow_contract_version_policy as module

        monkeypatch.setattr(module, "get_old_file_content", lambda *args: old_contract)

        checker = WorkflowContractVersionChecker(project_root)
        result = checker.check(changed_files=[".github/workflows/ci.yml"])

        # 验证 trigger_reasons 包含 ci.yml 的原因
        assert ".github/workflows/ci.yml" in result.trigger_reasons
        assert "Phase 1" in result.trigger_reasons[".github/workflows/ci.yml"]
        assert "workflow" in result.trigger_reasons[".github/workflows/ci.yml"].lower()

    def test_trigger_reasons_populated_for_tooling_files(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """工具脚本变更应在 trigger_reasons 中记录原因"""
        contract = make_contract("2.5.0", "2026-02-01")
        doc = make_doc_with_version_table([("2.5.0", "2026-02-01")])
        project_root = create_project_structure(contract, doc)

        old_contract = json.dumps(make_contract("2.5.0", "2026-02-01"))

        import scripts.ci.check_workflow_contract_version_policy as module

        monkeypatch.setattr(module, "get_old_file_content", lambda *args: old_contract)

        checker = WorkflowContractVersionChecker(project_root)
        result = checker.check(changed_files=["scripts/ci/validate_workflows.py"])

        # 验证 trigger_reasons 包含工具脚本的原因
        assert "scripts/ci/validate_workflows.py" in result.trigger_reasons
        assert "校验" in result.trigger_reasons["scripts/ci/validate_workflows.py"]

    def test_trigger_reasons_populated_for_makefile_ci_changes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Makefile CI 相关变更应在 trigger_reasons 中记录原因"""
        contract = make_contract("2.5.0", "2026-02-01")
        doc = make_doc_with_version_table([("2.5.0", "2026-02-01")])
        project_root = create_project_structure(contract, doc)

        ci_related_diff = "+validate-workflows:\n+    python scripts/ci/validate_workflows.py"
        old_contract = json.dumps(make_contract("2.5.0", "2026-02-01"))

        import scripts.ci.check_workflow_contract_version_policy as module

        monkeypatch.setattr(module, "get_old_file_content", lambda *args: old_contract)
        monkeypatch.setattr(module, "get_file_diff_content", lambda *args: ci_related_diff)

        checker = WorkflowContractVersionChecker(project_root)
        result = checker.check(changed_files=["Makefile"])

        # 验证 Makefile 在 trigger_reasons 中
        assert "Makefile" in result.trigger_reasons
        assert result.trigger_reasons["Makefile"] == MAKEFILE_RULE_DESCRIPTION

    def test_trigger_reasons_empty_for_non_critical_files(self) -> None:
        """非关键文件不应在 trigger_reasons 中"""
        contract = make_contract("2.5.0", "2026-02-01")
        doc = make_doc_with_version_table([("2.5.0", "2026-02-01")])
        project_root = create_project_structure(contract, doc)

        checker = WorkflowContractVersionChecker(project_root)
        result = checker.check(changed_files=["src/some_module.py", "README.md"])

        # 非关键文件不触发，trigger_reasons 应为空
        assert result.trigger_reasons == {}
        assert result.changed_critical_files == []

    def test_multiple_files_have_individual_reasons(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """多个关键文件变更时，每个都应有独立的 trigger_reason"""
        contract = make_contract("2.5.0", "2026-02-01")
        doc = make_doc_with_version_table([("2.5.0", "2026-02-01")])
        project_root = create_project_structure(contract, doc)

        old_contract = json.dumps(make_contract("2.5.0", "2026-02-01"))

        import scripts.ci.check_workflow_contract_version_policy as module

        monkeypatch.setattr(module, "get_old_file_content", lambda *args: old_contract)

        checker = WorkflowContractVersionChecker(project_root)
        result = checker.check(
            changed_files=[
                ".github/workflows/ci.yml",
                "scripts/ci/validate_workflows.py",
                "docs/ci_nightly_workflow_refactor/contract.md",
            ]
        )

        # 每个关键文件都应有独立的原因
        assert len(result.trigger_reasons) == 3
        assert ".github/workflows/ci.yml" in result.trigger_reasons
        assert "scripts/ci/validate_workflows.py" in result.trigger_reasons
        assert "docs/ci_nightly_workflow_refactor/contract.md" in result.trigger_reasons

        # 原因应各不相同（除非同类）
        reasons_set = set(result.trigger_reasons.values())
        assert len(reasons_set) == 3  # 三种不同的文件类型


# ============================================================================
# Test: CriticalFileRule 和规则定义
# ============================================================================


class TestCriticalFileRules:
    """测试关键文件规则定义"""

    def test_critical_workflow_rules_cover_phase1_files(self) -> None:
        """CRITICAL_WORKFLOW_RULES 应覆盖 Phase 1 的 ci/nightly 文件"""
        assert any(rule.matches(".github/workflows/ci.yml") for rule in CRITICAL_WORKFLOW_RULES)
        assert any(
            rule.matches(".github/workflows/nightly.yml") for rule in CRITICAL_WORKFLOW_RULES
        )

    def test_critical_workflow_rules_exclude_non_phase1_files(self) -> None:
        """CRITICAL_WORKFLOW_RULES 不应匹配非 Phase 1 的 workflow 文件"""
        # release.yml 是 Phase 2 的，当前不应匹配
        assert not any(
            rule.matches(".github/workflows/release.yml") for rule in CRITICAL_WORKFLOW_RULES
        )
        # 其他 workflow 文件也不应匹配
        assert not any(
            rule.matches(".github/workflows/deploy.yml") for rule in CRITICAL_WORKFLOW_RULES
        )

    def test_critical_tooling_rules_defined(self) -> None:
        """CRITICAL_TOOLING_RULES 应定义工具脚本"""
        tooling_patterns = [rule.pattern for rule in CRITICAL_TOOLING_RULES]
        # 至少应包含核心校验脚本
        assert any("validate_workflows" in p for p in tooling_patterns)
        assert any("schema" in p for p in tooling_patterns)

    def test_all_critical_rules_merged(self) -> None:
        """ALL_CRITICAL_RULES 应合并所有规则"""
        assert len(ALL_CRITICAL_RULES) >= len(CRITICAL_WORKFLOW_RULES)
        assert len(ALL_CRITICAL_RULES) >= len(CRITICAL_TOOLING_RULES)

    def test_get_matching_rule_returns_correct_rule(self) -> None:
        """get_matching_rule 应返回正确的匹配规则"""
        rule = get_matching_rule(".github/workflows/ci.yml")
        assert rule is not None
        assert rule.category == "workflow_core"

        rule = get_matching_rule("scripts/ci/validate_workflows.py")
        assert rule is not None
        assert rule.category == "tooling"

        rule = get_matching_rule("src/unrelated.py")
        assert rule is None

    def test_critical_file_rule_dataclass(self) -> None:
        """CriticalFileRule dataclass 应正常工作"""
        rule = CriticalFileRule(
            pattern=r"^test/.*\.py$",
            description="Test files",
            category="test",
        )
        assert rule.matches("test/foo.py")
        assert not rule.matches("src/foo.py")


# ============================================================================
# Test: filter_critical_files_with_reasons
# ============================================================================


class TestFilterCriticalFilesWithReasons:
    """测试 filter_critical_files_with_reasons 函数"""

    def test_returns_tuple_of_files_and_reasons(self) -> None:
        """应返回 (文件列表, 原因字典) 元组"""
        files, reasons = filter_critical_files_with_reasons(
            [".github/workflows/ci.yml", "src/foo.py"]
        )

        assert isinstance(files, list)
        assert isinstance(reasons, dict)
        assert ".github/workflows/ci.yml" in files
        assert "src/foo.py" not in files
        assert ".github/workflows/ci.yml" in reasons

    def test_makefile_with_ci_diff_included(self) -> None:
        """Makefile CI 相关变更应被包含"""
        ci_diff = "+validate-workflows:\n+    test"
        files, reasons = filter_critical_files_with_reasons(["Makefile"], ci_diff)

        assert "Makefile" in files
        assert "Makefile" in reasons
        assert reasons["Makefile"] == MAKEFILE_RULE_DESCRIPTION

    def test_makefile_without_ci_diff_excluded(self) -> None:
        """Makefile 非 CI 相关变更不应被包含"""
        non_ci_diff = "+test-unit:\n+    pytest"
        files, reasons = filter_critical_files_with_reasons(["Makefile"], non_ci_diff)

        assert "Makefile" not in files
        assert "Makefile" not in reasons


# ============================================================================
# Test: 纯函数接口 check_version_policy_pure
# ============================================================================


class TestCheckVersionPolicyPure:
    """测试纯函数接口 check_version_policy_pure"""

    def test_no_critical_files_returns_empty_result(self) -> None:
        """无关键文件变更时返回空结果"""
        input_data = VersionPolicyCheckInput(
            changed_files=["src/some_module.py", "README.md"],
            old_contract_content=json.dumps({"version": "2.5.0", "last_updated": "2026-02-01"}),
            new_contract_content=json.dumps({"version": "2.5.0", "last_updated": "2026-02-01"}),
            doc_content="| v2.5.0 | 2026-02-01 | test |",
        )

        result = check_version_policy_pure(input_data)

        assert result.critical_files == []
        assert result.violations == []

    def test_critical_file_changed_version_not_updated_returns_violation(self) -> None:
        """关键文件变更但版本未更新时返回违规"""
        input_data = VersionPolicyCheckInput(
            changed_files=[".github/workflows/ci.yml"],
            old_contract_content=json.dumps({"version": "2.5.0", "last_updated": "2026-02-01"}),
            new_contract_content=json.dumps({"version": "2.5.0", "last_updated": "2026-02-01"}),
            doc_content="| v2.5.0 | 2026-02-01 | test |",
        )

        result = check_version_policy_pure(input_data)

        assert ".github/workflows/ci.yml" in result.critical_files
        assert len(result.violations) >= 1
        version_violations = [
            v
            for v in result.violations
            if v.error_type == VersionPolicyErrorTypes.VERSION_NOT_UPDATED
        ]
        assert len(version_violations) == 1

    def test_version_updated_but_not_in_doc_returns_violation(self) -> None:
        """版本已更新但不在文档中时返回违规"""
        input_data = VersionPolicyCheckInput(
            changed_files=[".github/workflows/ci.yml"],
            old_contract_content=json.dumps({"version": "2.5.0", "last_updated": "2026-02-01"}),
            new_contract_content=json.dumps({"version": "2.6.0", "last_updated": "2026-02-02"}),
            doc_content="| v2.5.0 | 2026-02-01 | test |",  # 缺少 2.6.0
        )

        result = check_version_policy_pure(input_data)

        assert result.version_updated is True
        assert result.last_updated_updated is True
        assert result.version_in_doc is False
        doc_violations = [
            v
            for v in result.violations
            if v.error_type == VersionPolicyErrorTypes.VERSION_NOT_IN_DOC
        ]
        assert len(doc_violations) == 1

    def test_all_conditions_met_returns_no_violations(self) -> None:
        """所有条件满足时返回无违规"""
        input_data = VersionPolicyCheckInput(
            changed_files=[".github/workflows/ci.yml"],
            old_contract_content=json.dumps({"version": "2.5.0", "last_updated": "2026-02-01"}),
            new_contract_content=json.dumps({"version": "2.6.0", "last_updated": "2026-02-02"}),
            doc_content="| v2.6.0 | 2026-02-02 | new | \n| v2.5.0 | 2026-02-01 | test |",
        )

        result = check_version_policy_pure(input_data)

        assert result.version_updated is True
        assert result.last_updated_updated is True
        assert result.version_in_doc is True
        assert result.violations == []


# ============================================================================
# Test: ci.yml 变更必须 bump
# ============================================================================


class TestCiYmlChangesMustBump:
    """测试 .github/workflows/ci.yml 变更必须触发 bump"""

    def test_ci_yml_is_workflow_file(self) -> None:
        """ci.yml 应被识别为 workflow 文件"""
        assert is_workflow_file(".github/workflows/ci.yml") is True

    def test_ci_yml_change_triggers_version_check(self) -> None:
        """ci.yml 变更应触发版本检查"""
        files, reasons = filter_critical_files_with_reasons([".github/workflows/ci.yml"])

        assert ".github/workflows/ci.yml" in files
        assert ".github/workflows/ci.yml" in reasons
        assert "workflow" in reasons[".github/workflows/ci.yml"].lower()

    def test_ci_yml_change_requires_bump_pure_function(self) -> None:
        """ci.yml 变更必须 bump（纯函数测试）"""
        input_data = VersionPolicyCheckInput(
            changed_files=[".github/workflows/ci.yml"],
            old_contract_content=json.dumps({"version": "2.5.0", "last_updated": "2026-02-01"}),
            new_contract_content=json.dumps({"version": "2.5.0", "last_updated": "2026-02-01"}),
            doc_content="| v2.5.0 | 2026-02-01 | test |",
        )

        result = check_version_policy_pure(input_data)

        # 必须有违规（版本未更新）
        assert len(result.violations) >= 1
        assert any(
            v.error_type == VersionPolicyErrorTypes.VERSION_NOT_UPDATED for v in result.violations
        )

    def test_nightly_yml_also_requires_bump(self) -> None:
        """nightly.yml 变更也必须 bump"""
        assert is_workflow_file(".github/workflows/nightly.yml") is True

        files, reasons = filter_critical_files_with_reasons([".github/workflows/nightly.yml"])
        assert ".github/workflows/nightly.yml" in files


# ============================================================================
# Test: scripts/ci/*.py 关键校验器触发
# ============================================================================


class TestCriticalToolingScriptsTrigger:
    """测试 scripts/ci/*.py 关键校验器变更触发版本检查"""

    def test_validate_workflows_is_critical(self) -> None:
        """validate_workflows.py 应被识别为关键工具脚本"""
        assert is_critical_tooling_script("scripts/ci/validate_workflows.py") is True

    def test_check_workflow_contract_docs_sync_is_critical(self) -> None:
        """check_workflow_contract_docs_sync.py 应被识别为关键工具脚本"""
        assert is_critical_tooling_script("scripts/ci/check_workflow_contract_docs_sync.py") is True

    def test_workflow_contract_drift_report_is_critical(self) -> None:
        """workflow_contract_drift_report.py 应被识别为关键工具脚本"""
        assert is_critical_tooling_script("scripts/ci/workflow_contract_drift_report.py") is True

    def test_schema_file_is_critical(self) -> None:
        """workflow_contract.v1.schema.json 应被识别为关键工具脚本"""
        assert is_critical_tooling_script("scripts/ci/workflow_contract.v1.schema.json") is True
        assert is_critical_tooling_script("scripts/ci/workflow_contract.v2.schema.json") is True

    def test_non_critical_script_not_flagged(self) -> None:
        """非关键脚本不应被标记"""
        # 假设这些脚本存在但不是关键的
        assert is_critical_tooling_script("scripts/ci/some_other_script.py") is False
        assert is_critical_tooling_script("scripts/docs/generate_docs.py") is False

    def test_critical_script_change_triggers_version_check(self) -> None:
        """关键脚本变更应触发版本检查"""
        files, reasons = filter_critical_files_with_reasons(["scripts/ci/validate_workflows.py"])

        assert "scripts/ci/validate_workflows.py" in files
        assert "scripts/ci/validate_workflows.py" in reasons

    def test_critical_script_change_requires_bump_pure_function(self) -> None:
        """关键脚本变更必须 bump（纯函数测试）"""
        input_data = VersionPolicyCheckInput(
            changed_files=["scripts/ci/validate_workflows.py"],
            old_contract_content=json.dumps({"version": "2.5.0", "last_updated": "2026-02-01"}),
            new_contract_content=json.dumps({"version": "2.5.0", "last_updated": "2026-02-01"}),
            doc_content="| v2.5.0 | 2026-02-01 | test |",
        )

        result = check_version_policy_pure(input_data)

        assert len(result.violations) >= 1
        assert any(
            v.error_type == VersionPolicyErrorTypes.VERSION_NOT_UPDATED for v in result.violations
        )


# ============================================================================
# Test: 非关键 docs 不触发
# ============================================================================


class TestNonCriticalDocsDoNotTrigger:
    """测试非关键文档不触发版本检查"""

    def test_architecture_docs_are_non_critical(self) -> None:
        """docs/architecture/ 下的文档应被识别为非关键"""
        assert is_non_critical_doc("docs/architecture/adr_something.md") is True
        assert is_non_critical_doc("docs/architecture/design.md") is True

    def test_dev_docs_are_non_critical(self) -> None:
        """docs/dev/ 下的文档应被识别为非关键"""
        assert is_non_critical_doc("docs/dev/mypy_baseline.md") is True
        assert is_non_critical_doc("docs/dev/agents.md") is True

    def test_guides_docs_are_non_critical(self) -> None:
        """docs/guides/ 下的文档应被识别为非关键"""
        assert is_non_critical_doc("docs/guides/quick_start.md") is True

    def test_acceptance_docs_are_non_critical(self) -> None:
        """docs/acceptance/ 下的文档应被识别为非关键"""
        assert is_non_critical_doc("docs/acceptance/iteration_8_regression.md") is True

    def test_ci_nightly_workflow_refactor_docs_are_critical(self) -> None:
        """docs/ci_nightly_workflow_refactor/ 下的文档应被识别为关键"""
        # 这些是合约文档，应该触发版本检查
        assert is_non_critical_doc("docs/ci_nightly_workflow_refactor/contract.md") is False
        assert is_non_critical_doc("docs/ci_nightly_workflow_refactor/maintenance.md") is False

    def test_non_critical_docs_do_not_trigger_version_check(self) -> None:
        """非关键文档变更不触发版本检查"""
        non_critical_files = [
            "docs/architecture/adr_new.md",
            "docs/dev/setup.md",
            "docs/guides/tutorial.md",
        ]

        files, reasons = filter_critical_files_with_reasons(non_critical_files)

        # 所有这些文件都不应触发
        assert len(files) == 0
        assert len(reasons) == 0

    def test_non_critical_docs_no_violations_pure_function(self) -> None:
        """非关键文档变更无违规（纯函数测试）"""
        input_data = VersionPolicyCheckInput(
            changed_files=[
                "docs/architecture/adr_new.md",
                "docs/dev/setup.md",
                "README.md",
            ],
            old_contract_content=json.dumps({"version": "2.5.0", "last_updated": "2026-02-01"}),
            new_contract_content=json.dumps({"version": "2.5.0", "last_updated": "2026-02-01"}),
            doc_content="| v2.5.0 | 2026-02-01 | test |",
        )

        result = check_version_policy_pure(input_data)

        # 无关键文件变更，无违规
        assert result.critical_files == []
        assert result.violations == []


# ============================================================================
# Test: Makefile CI 相关 target 识别准确性
# ============================================================================


class TestMakefileCITargetRecognitionAccuracy:
    """测试 Makefile CI 相关 target 识别的准确性"""

    def test_validate_workflows_keyword_triggers(self) -> None:
        """validate-workflows 关键字应触发"""
        diff = "+validate-workflows:\n+\t$(PYTHON) -m scripts.ci.validate_workflows"
        files, reasons = filter_critical_files_with_reasons(["Makefile"], diff)

        assert "Makefile" in files
        assert reasons["Makefile"] == MAKEFILE_RULE_DESCRIPTION

    def test_check_workflow_keyword_triggers(self) -> None:
        """check-workflow 关键字应触发"""
        diff = "+check-workflow-contract-docs-sync:\n+\tpython check.py"
        files, reasons = filter_critical_files_with_reasons(["Makefile"], diff)

        assert "Makefile" in files

    def test_ci_target_keyword_triggers(self) -> None:
        """ci: 关键字应触发"""
        diff = "-ci: lint typecheck\n+ci: lint typecheck validate-workflows"
        files, reasons = filter_critical_files_with_reasons(["Makefile"], diff)

        assert "Makefile" in files

    def test_workflow_contract_keyword_triggers(self) -> None:
        """workflow-contract 关键字应触发"""
        diff = "+workflow-contract-drift-report:\n+\tpython drift.py"
        files, reasons = filter_critical_files_with_reasons(["Makefile"], diff)

        assert "Makefile" in files

    def test_unrelated_target_does_not_trigger(self) -> None:
        """无关 target 不触发"""
        diff = "+test-unit:\n+\tpytest tests/\n+clean:\n+\trm -rf __pycache__"
        files, reasons = filter_critical_files_with_reasons(["Makefile"], diff)

        assert "Makefile" not in files

    def test_format_target_does_not_trigger(self) -> None:
        """format target 不触发（非 CI 相关）"""
        diff = "+format:\n+\truff format src/"
        files, reasons = filter_critical_files_with_reasons(["Makefile"], diff)

        assert "Makefile" not in files

    def test_lint_target_alone_does_not_trigger(self) -> None:
        """单独的 lint target 不触发（除非与 ci: 一起）"""
        diff = "+lint:\n+\truff check src/"
        files, reasons = filter_critical_files_with_reasons(["Makefile"], diff)

        assert "Makefile" not in files

    def test_makefile_ci_keywords_completeness(self) -> None:
        """验证 MAKEFILE_CI_KEYWORDS 常量完整性"""
        expected_keywords = [
            "validate-workflows",
            "check-workflow",
            "ci:",
            "workflow-contract",
        ]
        for keyword in expected_keywords:
            assert keyword in MAKEFILE_CI_KEYWORDS, f"Missing keyword: {keyword}"

    def test_makefile_ci_diff_with_multiple_keywords(self) -> None:
        """Makefile diff 包含多个 CI 关键字"""
        diff = """
+validate-workflows-strict:
+\t$(PYTHON) -m scripts.ci.validate_workflows --strict
+
+check-workflow-contract-docs-sync:
+\t$(PYTHON) -m scripts.ci.check_workflow_contract_docs_sync
+
+ci: lint typecheck validate-workflows-strict check-workflow-contract-docs-sync
"""
        files, reasons = filter_critical_files_with_reasons(["Makefile"], diff)

        assert "Makefile" in files


# ============================================================================
# Test: bump 但未更新 contract.md 版本表的报错信息稳定
# ============================================================================


class TestVersionNotInDocErrorMessageStability:
    """测试 bump 但未更新 contract.md 版本表的报错信息稳定性"""

    def test_version_not_in_doc_error_message_contains_version(self) -> None:
        """报错信息应包含版本号"""
        input_data = VersionPolicyCheckInput(
            changed_files=[".github/workflows/ci.yml"],
            old_contract_content=json.dumps({"version": "2.5.0", "last_updated": "2026-02-01"}),
            new_contract_content=json.dumps({"version": "2.6.0", "last_updated": "2026-02-02"}),
            doc_content="| v2.5.0 | 2026-02-01 | test |",  # 缺少 2.6.0
        )

        result = check_version_policy_pure(input_data)

        doc_violations = [
            v
            for v in result.violations
            if v.error_type == VersionPolicyErrorTypes.VERSION_NOT_IN_DOC
        ]
        assert len(doc_violations) == 1
        assert "2.6.0" in doc_violations[0].message

    def test_version_not_in_doc_error_message_contains_contract_md(self) -> None:
        """报错信息应提及 contract.md"""
        input_data = VersionPolicyCheckInput(
            changed_files=[".github/workflows/ci.yml"],
            old_contract_content=json.dumps({"version": "2.5.0", "last_updated": "2026-02-01"}),
            new_contract_content=json.dumps({"version": "2.6.0", "last_updated": "2026-02-02"}),
            doc_content="| v2.5.0 | 2026-02-01 | test |",
        )

        result = check_version_policy_pure(input_data)

        doc_violations = [
            v
            for v in result.violations
            if v.error_type == VersionPolicyErrorTypes.VERSION_NOT_IN_DOC
        ]
        assert len(doc_violations) == 1
        assert "contract.md" in doc_violations[0].message

    def test_version_not_in_doc_suggestion_contains_example(self) -> None:
        """建议应包含添加版本行的示例"""
        input_data = VersionPolicyCheckInput(
            changed_files=[".github/workflows/ci.yml"],
            old_contract_content=json.dumps({"version": "2.5.0", "last_updated": "2026-02-01"}),
            new_contract_content=json.dumps({"version": "2.6.0", "last_updated": "2026-02-02"}),
            doc_content="| v2.5.0 | 2026-02-01 | test |",
        )

        result = check_version_policy_pure(input_data)

        doc_violations = [
            v
            for v in result.violations
            if v.error_type == VersionPolicyErrorTypes.VERSION_NOT_IN_DOC
        ]
        assert len(doc_violations) == 1
        # 建议应包含版本号和格式示例
        assert "v2.6.0" in doc_violations[0].suggestion
        assert "|" in doc_violations[0].suggestion  # 表格格式

    def test_error_type_is_stable(self) -> None:
        """error_type 应保持稳定"""
        input_data = VersionPolicyCheckInput(
            changed_files=[".github/workflows/ci.yml"],
            old_contract_content=json.dumps({"version": "2.5.0", "last_updated": "2026-02-01"}),
            new_contract_content=json.dumps({"version": "2.6.0", "last_updated": "2026-02-02"}),
            doc_content="| v2.5.0 | 2026-02-01 | test |",
        )

        result = check_version_policy_pure(input_data)

        doc_violations = [
            v
            for v in result.violations
            if v.error_type == VersionPolicyErrorTypes.VERSION_NOT_IN_DOC
        ]
        assert len(doc_violations) == 1
        # error_type 应为常量定义的值
        assert doc_violations[0].error_type == "version_not_in_doc"

    def test_version_not_in_doc_with_integration_style(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """集成风格测试：使用 WorkflowContractVersionChecker"""
        contract = make_contract("2.6.0", "2026-02-02")
        # 版本表缺少 2.6.0
        doc = make_doc_with_version_table([("2.5.0", "2026-02-01")])
        project_root = create_project_structure(contract, doc)

        # Mock 旧版本
        old_contract = json.dumps(make_contract("2.5.0", "2026-02-01"))

        import scripts.ci.check_workflow_contract_version_policy as module

        monkeypatch.setattr(module, "get_old_file_content", lambda *args: old_contract)

        checker = WorkflowContractVersionChecker(project_root)
        result = checker.check(changed_files=[".github/workflows/ci.yml"])

        # 应有 version_not_in_doc 错误
        assert result.success is False
        doc_errors = [e for e in result.errors if e.error_type == "version_not_in_doc"]
        assert len(doc_errors) == 1
        # 报错信息稳定性检查
        assert "2.6.0" in doc_errors[0].message
        assert "contract.md" in doc_errors[0].message


# ============================================================================
# Test: RuleGroups 常量定义
# ============================================================================


class TestRuleGroupsConstants:
    """测试 RuleGroups 常量定义"""

    def test_workflow_files_defined(self) -> None:
        """WORKFLOW_FILES 应定义 Phase 1 workflow 文件"""
        assert ".github/workflows/ci.yml" in RuleGroups.WORKFLOW_FILES
        assert ".github/workflows/nightly.yml" in RuleGroups.WORKFLOW_FILES
        # Phase 2 的 release.yml 当前不在列表中
        assert ".github/workflows/release.yml" not in RuleGroups.WORKFLOW_FILES

    def test_critical_tooling_scripts_defined(self) -> None:
        """CRITICAL_TOOLING_SCRIPTS 应定义关键工具脚本"""
        assert "scripts/ci/validate_workflows.py" in RuleGroups.CRITICAL_TOOLING_SCRIPTS
        assert (
            "scripts/ci/check_workflow_contract_docs_sync.py" in RuleGroups.CRITICAL_TOOLING_SCRIPTS
        )

    def test_non_critical_doc_prefixes_defined(self) -> None:
        """NON_CRITICAL_DOC_PREFIXES 应定义非关键文档前缀"""
        assert "docs/architecture/" in RuleGroups.NON_CRITICAL_DOC_PREFIXES
        assert "docs/dev/" in RuleGroups.NON_CRITICAL_DOC_PREFIXES
        # ci_nightly_workflow_refactor 不应在非关键列表中
        assert "docs/ci_nightly_workflow_refactor/" not in RuleGroups.NON_CRITICAL_DOC_PREFIXES

    def test_makefile_ci_target_keywords_defined(self) -> None:
        """MAKEFILE_CI_TARGET_KEYWORDS 应定义 CI 相关关键字"""
        assert "validate-workflows" in RuleGroups.MAKEFILE_CI_TARGET_KEYWORDS
        assert "ci:" in RuleGroups.MAKEFILE_CI_TARGET_KEYWORDS
