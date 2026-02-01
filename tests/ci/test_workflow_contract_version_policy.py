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
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

# 导入被测模块
from scripts.ci.check_workflow_contract_version_policy import (
    MAKEFILE_CI_KEYWORDS,
    WorkflowContractVersionChecker,
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
