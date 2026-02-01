#!/usr/bin/env python3
"""
tests/ci/test_bump_workflow_contract_version.py

单元测试：bump_workflow_contract_version.py 的版本升级功能

测试范围：
1. 版本号解析和升级（major/minor/patch）
2. 显式版本号设置
3. contract JSON 更新（version, last_updated, changelog）
4. 文档版本控制表更新
5. 干运行模式
6. 错误处理
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

# 导入被测模块
from scripts.ci.bump_workflow_contract_version import (
    BUMP_TYPES,
    BumpResult,
    WorkflowContractVersionBumper,
    bump_version,
    check_version_in_doc,
    insert_version_row_in_doc,
    is_valid_version,
    parse_semver,
    update_contract_version,
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
        "$schema": "workflow_contract.v1.schema.json",
        "version": version,
        "description": "Test contract",
        "last_updated": last_updated,
        "_changelog_v" + version: "Test changelog",
        "ci": {
            "file": ".github/workflows/ci.yml",
            "job_ids": ["test"],
            "job_names": ["Test"],
        },
    }


def make_doc_with_version_table(versions: list[tuple[str, str, str]]) -> str:
    """创建包含版本控制表的文档

    Args:
        versions: 版本列表，每项为 (version, date, description)

    Returns:
        文档内容
    """
    table_rows = "\n".join([f"| v{ver} | {date} | {desc} |" for ver, date, desc in versions])
    return f"""# CI/Nightly Workflow Contract

> 本文档固化 workflow 的关键标识符。

---

## 14. 版本控制

| 版本 | 日期 | 变更说明 |
|------|------|----------|
{table_rows}
"""


# ============================================================================
# Test: 版本号解析
# ============================================================================


class TestParseSemver:
    """测试版本号解析"""

    def test_parse_valid_version(self) -> None:
        """测试解析有效版本号"""
        assert parse_semver("2.18.0") == (2, 18, 0)
        assert parse_semver("0.0.1") == (0, 0, 1)
        assert parse_semver("10.20.30") == (10, 20, 30)

    def test_parse_invalid_version(self) -> None:
        """测试解析无效版本号"""
        assert parse_semver("v2.18.0") is None  # 带 v 前缀
        assert parse_semver("2.18") is None  # 缺少 patch
        assert parse_semver("2.18.0.1") is None  # 过多部分
        assert parse_semver("abc") is None  # 非数字
        assert parse_semver("") is None  # 空字符串


class TestIsValidVersion:
    """测试版本号有效性检查"""

    def test_valid_versions(self) -> None:
        """测试有效版本号"""
        assert is_valid_version("1.0.0") is True
        assert is_valid_version("2.18.0") is True
        assert is_valid_version("0.0.0") is True

    def test_invalid_versions(self) -> None:
        """测试无效版本号"""
        assert is_valid_version("v1.0.0") is False
        assert is_valid_version("1.0") is False
        assert is_valid_version("abc") is False


# ============================================================================
# Test: 版本号升级
# ============================================================================


class TestBumpVersion:
    """测试版本号升级"""

    def test_bump_patch(self) -> None:
        """测试 patch 版本升级"""
        assert bump_version("2.18.0", "patch") == "2.18.1"
        assert bump_version("1.0.0", "patch") == "1.0.1"
        assert bump_version("0.0.9", "patch") == "0.0.10"

    def test_bump_minor(self) -> None:
        """测试 minor 版本升级"""
        assert bump_version("2.18.0", "minor") == "2.19.0"
        assert bump_version("1.0.5", "minor") == "1.1.0"
        assert bump_version("0.9.9", "minor") == "0.10.0"

    def test_bump_major(self) -> None:
        """测试 major 版本升级"""
        assert bump_version("2.18.0", "major") == "3.0.0"
        assert bump_version("1.5.3", "major") == "2.0.0"
        assert bump_version("0.0.1", "major") == "1.0.0"

    def test_bump_invalid_version(self) -> None:
        """测试升级无效版本号"""
        assert bump_version("invalid", "patch") is None
        assert bump_version("v1.0.0", "patch") is None

    def test_bump_invalid_type(self) -> None:
        """测试无效升级类型"""
        assert bump_version("1.0.0", "invalid") is None


class TestBumpTypes:
    """测试升级类型常量"""

    def test_bump_types_defined(self) -> None:
        """BUMP_TYPES 应包含所有升级类型"""
        assert "major" in BUMP_TYPES
        assert "minor" in BUMP_TYPES
        assert "patch" in BUMP_TYPES
        assert len(BUMP_TYPES) == 3


# ============================================================================
# Test: Contract 更新
# ============================================================================


class TestUpdateContractVersion:
    """测试 contract 版本更新"""

    def test_update_version_and_date(self) -> None:
        """测试更新版本号和日期"""
        contract = make_contract("2.18.0", "2026-02-01")
        updated = update_contract_version(contract, "2.19.0", "2026-02-02")

        assert updated["version"] == "2.19.0"
        assert updated["last_updated"] == "2026-02-02"

    def test_add_changelog_entry(self) -> None:
        """测试添加 changelog 条目"""
        contract = make_contract("2.18.0", "2026-02-01")
        updated = update_contract_version(
            contract,
            "2.19.0",
            "2026-02-02",
            changelog_message="新增功能",
            add_changelog=True,
        )

        assert "_changelog_v2.19.0" in updated
        assert updated["_changelog_v2.19.0"] == "新增功能"

    def test_no_changelog_when_disabled(self) -> None:
        """测试禁用 changelog"""
        contract = make_contract("2.18.0", "2026-02-01")
        updated = update_contract_version(
            contract,
            "2.19.0",
            "2026-02-02",
            add_changelog=False,
        )

        assert "_changelog_v2.19.0" not in updated

    def test_preserve_existing_fields(self) -> None:
        """测试保留现有字段"""
        contract = make_contract("2.18.0", "2026-02-01")
        updated = update_contract_version(contract, "2.19.0", "2026-02-02")

        # 保留的字段
        assert updated["$schema"] == "workflow_contract.v1.schema.json"
        assert updated["description"] == "Test contract"
        assert "ci" in updated
        assert updated["ci"]["job_ids"] == ["test"]

    def test_preserve_old_changelog_entries(self) -> None:
        """测试保留旧的 changelog 条目"""
        contract = make_contract("2.18.0", "2026-02-01")
        updated = update_contract_version(contract, "2.19.0", "2026-02-02")

        # 旧的 changelog 应保留
        assert "_changelog_v2.18.0" in updated

    def test_default_changelog_placeholder(self) -> None:
        """测试默认 changelog 占位符"""
        contract = make_contract("2.18.0", "2026-02-01")
        updated = update_contract_version(
            contract,
            "2.19.0",
            "2026-02-02",
            add_changelog=True,
        )

        # 没有提供 message 时应使用占位符
        assert "_changelog_v2.19.0" in updated
        assert "填写" in updated["_changelog_v2.19.0"]


# ============================================================================
# Test: 文档更新
# ============================================================================


class TestInsertVersionRowInDoc:
    """测试文档版本行插入"""

    def test_insert_row_at_top(self) -> None:
        """测试在表格顶部插入新行"""
        doc = make_doc_with_version_table(
            [
                ("2.18.0", "2026-02-01", "旧版本"),
            ]
        )
        updated, success = insert_version_row_in_doc(doc, "2.19.0", "2026-02-02", "新版本")

        assert success is True
        # 新版本应在旧版本之前
        v219_pos = updated.find("v2.19.0")
        v218_pos = updated.find("v2.18.0")
        assert v219_pos < v218_pos

    def test_insert_with_default_message(self) -> None:
        """测试使用默认消息插入"""
        doc = make_doc_with_version_table([("2.18.0", "2026-02-01", "旧版本")])
        updated, success = insert_version_row_in_doc(doc, "2.19.0", "2026-02-02", None)

        assert success is True
        assert "v2.19.0" in updated
        assert "填写" in updated  # 默认占位符

    def test_insert_preserves_existing_content(self) -> None:
        """测试插入保留现有内容"""
        doc = make_doc_with_version_table(
            [
                ("2.18.0", "2026-02-01", "版本 2.18.0"),
                ("2.17.0", "2026-01-31", "版本 2.17.0"),
            ]
        )
        updated, success = insert_version_row_in_doc(doc, "2.19.0", "2026-02-02", "版本 2.19.0")

        assert success is True
        # 所有版本都应存在
        assert "v2.19.0" in updated
        assert "v2.18.0" in updated
        assert "v2.17.0" in updated

    def test_insert_fails_without_table(self) -> None:
        """测试无版本表时插入失败"""
        doc = "# No Version Table Here"
        updated, success = insert_version_row_in_doc(doc, "2.19.0", "2026-02-02", "新版本")

        assert success is False
        assert updated == doc  # 内容不变


class TestCheckVersionInDoc:
    """测试检查版本是否在文档中"""

    def test_version_exists_with_v_prefix(self) -> None:
        """测试带 v 前缀的版本存在"""
        doc = make_doc_with_version_table([("2.18.0", "2026-02-01", "test")])
        assert check_version_in_doc(doc, "2.18.0") is True

    def test_version_not_exists(self) -> None:
        """测试版本不存在"""
        doc = make_doc_with_version_table([("2.18.0", "2026-02-01", "test")])
        assert check_version_in_doc(doc, "2.19.0") is False

    def test_version_exists_without_v_prefix(self) -> None:
        """测试不带 v 前缀的版本存在"""
        doc = """| 版本 | 日期 |
|------|------|
| 2.18.0 | 2026-02-01 |"""
        assert check_version_in_doc(doc, "2.18.0") is True


# ============================================================================
# Test: WorkflowContractVersionBumper
# ============================================================================


class TestBumperPatchVersion:
    """测试 Bumper patch 版本升级"""

    def test_bump_patch_success(self) -> None:
        """测试 patch 升级成功"""
        contract = make_contract("2.18.0", "2026-02-01")
        doc = make_doc_with_version_table([("2.18.0", "2026-02-01", "旧版本")])
        project_root = create_project_structure(contract, doc)

        bumper = WorkflowContractVersionBumper(project_root)
        result = bumper.bump(bump_type="patch", dry_run=True)

        assert result.success is True
        assert result.old_version == "2.18.0"
        assert result.new_version == "2.18.1"

    def test_bump_minor_success(self) -> None:
        """测试 minor 升级成功"""
        contract = make_contract("2.18.0", "2026-02-01")
        doc = make_doc_with_version_table([("2.18.0", "2026-02-01", "旧版本")])
        project_root = create_project_structure(contract, doc)

        bumper = WorkflowContractVersionBumper(project_root)
        result = bumper.bump(bump_type="minor", dry_run=True)

        assert result.success is True
        assert result.old_version == "2.18.0"
        assert result.new_version == "2.19.0"

    def test_bump_major_success(self) -> None:
        """测试 major 升级成功"""
        contract = make_contract("2.18.0", "2026-02-01")
        doc = make_doc_with_version_table([("2.18.0", "2026-02-01", "旧版本")])
        project_root = create_project_structure(contract, doc)

        bumper = WorkflowContractVersionBumper(project_root)
        result = bumper.bump(bump_type="major", dry_run=True)

        assert result.success is True
        assert result.old_version == "2.18.0"
        assert result.new_version == "3.0.0"


class TestBumperExplicitVersion:
    """测试 Bumper 显式版本设置"""

    def test_explicit_version_success(self) -> None:
        """测试显式版本设置成功"""
        contract = make_contract("2.18.0", "2026-02-01")
        doc = make_doc_with_version_table([("2.18.0", "2026-02-01", "旧版本")])
        project_root = create_project_structure(contract, doc)

        bumper = WorkflowContractVersionBumper(project_root)
        result = bumper.bump(explicit_version="3.0.0", dry_run=True)

        assert result.success is True
        assert result.new_version == "3.0.0"

    def test_explicit_invalid_version_fails(self) -> None:
        """测试无效显式版本失败"""
        contract = make_contract("2.18.0", "2026-02-01")
        doc = make_doc_with_version_table([("2.18.0", "2026-02-01", "旧版本")])
        project_root = create_project_structure(contract, doc)

        bumper = WorkflowContractVersionBumper(project_root)
        result = bumper.bump(explicit_version="invalid", dry_run=True)

        assert result.success is False
        assert result.errors is not None
        assert any("Invalid version" in e for e in result.errors)


class TestBumperWithMessage:
    """测试 Bumper 带变更说明"""

    def test_bump_with_message(self) -> None:
        """测试带变更说明升级"""
        contract = make_contract("2.18.0", "2026-02-01")
        doc = make_doc_with_version_table([("2.18.0", "2026-02-01", "旧版本")])
        project_root = create_project_structure(contract, doc)

        bumper = WorkflowContractVersionBumper(project_root)
        result = bumper.bump(
            bump_type="minor",
            message="新增 XXX 功能",
            dry_run=True,
        )

        assert result.success is True
        assert result.message == "新增 XXX 功能"


class TestBumperNoChangelog:
    """测试 Bumper 不添加 changelog"""

    def test_bump_without_changelog(self) -> None:
        """测试不添加 changelog"""
        contract = make_contract("2.18.0", "2026-02-01")
        doc = make_doc_with_version_table([("2.18.0", "2026-02-01", "旧版本")])
        project_root = create_project_structure(contract, doc)

        bumper = WorkflowContractVersionBumper(project_root)
        result = bumper.bump(
            bump_type="minor",
            add_changelog=False,
            dry_run=True,
        )

        assert result.success is True
        assert result.changelog_key == ""


class TestBumperWriteFiles:
    """测试 Bumper 写入文件"""

    def test_bump_writes_contract(self) -> None:
        """测试升级写入 contract 文件"""
        contract = make_contract("2.18.0", "2026-02-01")
        doc = make_doc_with_version_table([("2.18.0", "2026-02-01", "旧版本")])
        project_root = create_project_structure(contract, doc)

        bumper = WorkflowContractVersionBumper(project_root)
        result = bumper.bump(bump_type="minor", dry_run=False)

        assert result.success is True

        # 验证文件已更新
        contract_path = project_root / "scripts" / "ci" / "workflow_contract.v1.json"
        with open(contract_path, "r", encoding="utf-8") as f:
            updated_contract = json.load(f)

        assert updated_contract["version"] == "2.19.0"
        assert "_changelog_v2.19.0" in updated_contract

    def test_bump_writes_doc(self) -> None:
        """测试升级写入文档文件"""
        contract = make_contract("2.18.0", "2026-02-01")
        doc = make_doc_with_version_table([("2.18.0", "2026-02-01", "旧版本")])
        project_root = create_project_structure(contract, doc)

        bumper = WorkflowContractVersionBumper(project_root)
        result = bumper.bump(bump_type="minor", dry_run=False)

        assert result.success is True

        # 验证文档已更新
        doc_path = project_root / "docs" / "ci_nightly_workflow_refactor" / "contract.md"
        with open(doc_path, "r", encoding="utf-8") as f:
            updated_doc = f.read()

        assert "v2.19.0" in updated_doc

    def test_dry_run_does_not_write(self) -> None:
        """测试干运行不写入文件"""
        contract = make_contract("2.18.0", "2026-02-01")
        doc = make_doc_with_version_table([("2.18.0", "2026-02-01", "旧版本")])
        project_root = create_project_structure(contract, doc)

        bumper = WorkflowContractVersionBumper(project_root)
        result = bumper.bump(bump_type="minor", dry_run=True)

        assert result.success is True

        # 验证文件未更新
        contract_path = project_root / "scripts" / "ci" / "workflow_contract.v1.json"
        with open(contract_path, "r", encoding="utf-8") as f:
            original_contract = json.load(f)

        assert original_contract["version"] == "2.18.0"


class TestBumperErrorHandling:
    """测试 Bumper 错误处理"""

    def test_missing_contract_fails(self) -> None:
        """测试缺失 contract 文件失败"""
        # 创建只有文档的项目结构
        temp_dir = Path(tempfile.mkdtemp())
        doc_dir = temp_dir / "docs" / "ci_nightly_workflow_refactor"
        doc_dir.mkdir(parents=True, exist_ok=True)
        doc_path = doc_dir / "contract.md"
        with open(doc_path, "w", encoding="utf-8") as f:
            f.write("# Test")

        bumper = WorkflowContractVersionBumper(temp_dir)
        result = bumper.bump(bump_type="minor", dry_run=True)

        assert result.success is False
        assert result.errors is not None
        assert any("not found" in e for e in result.errors)

    def test_missing_doc_shows_warning(self) -> None:
        """测试缺失文档文件显示警告"""
        contract = make_contract("2.18.0", "2026-02-01")
        # 创建只有 contract 的项目结构
        temp_dir = Path(tempfile.mkdtemp())
        contract_dir = temp_dir / "scripts" / "ci"
        contract_dir.mkdir(parents=True, exist_ok=True)
        contract_path = contract_dir / "workflow_contract.v1.json"
        with open(contract_path, "w", encoding="utf-8") as f:
            json.dump(contract, f, indent=2)

        bumper = WorkflowContractVersionBumper(temp_dir)
        result = bumper.bump(bump_type="minor", dry_run=True)

        # 应该成功（只是有警告）
        assert result.success is True
        assert result.errors is not None
        assert any("Warning" in e for e in result.errors)

    def test_version_already_exists_warning(self) -> None:
        """测试版本已存在显示警告"""
        contract = make_contract("2.18.0", "2026-02-01")
        # 文档中已有 2.18.1
        doc = make_doc_with_version_table(
            [
                ("2.18.1", "2026-02-02", "新版本"),
                ("2.18.0", "2026-02-01", "旧版本"),
            ]
        )
        project_root = create_project_structure(contract, doc)

        bumper = WorkflowContractVersionBumper(project_root)
        result = bumper.bump(bump_type="patch", dry_run=True)

        # 应该成功（但有警告）
        assert result.success is True
        assert result.errors is not None
        assert any("already exists" in e for e in result.errors)


class TestBumperDefaultBehavior:
    """测试 Bumper 默认行为"""

    def test_default_bump_is_patch(self) -> None:
        """测试默认升级类型是 patch"""
        contract = make_contract("2.18.0", "2026-02-01")
        doc = make_doc_with_version_table([("2.18.0", "2026-02-01", "旧版本")])
        project_root = create_project_structure(contract, doc)

        bumper = WorkflowContractVersionBumper(project_root)
        # 不指定 bump_type
        result = bumper.bump(dry_run=True)

        assert result.success is True
        assert result.new_version == "2.18.1"  # patch 升级


class TestBumpResult:
    """测试 BumpResult 数据类"""

    def test_to_dict(self) -> None:
        """测试转换为字典"""
        result = BumpResult(
            success=True,
            old_version="2.18.0",
            new_version="2.19.0",
            old_last_updated="2026-02-01",
            new_last_updated="2026-02-02",
            message="test message",
            changelog_key="_changelog_v2.19.0",
            errors=None,
        )

        d = result.to_dict()
        assert d["success"] is True
        assert d["old_version"] == "2.18.0"
        assert d["new_version"] == "2.19.0"
        assert d["message"] == "test message"
        assert d["changelog_key"] == "_changelog_v2.19.0"

    def test_to_dict_with_errors(self) -> None:
        """测试带错误的转换"""
        result = BumpResult(
            success=False,
            old_version="2.18.0",
            new_version="",
            old_last_updated="2026-02-01",
            new_last_updated="",
            message="",
            errors=["Error 1", "Error 2"],
        )

        d = result.to_dict()
        assert d["success"] is False
        assert d["errors"] == ["Error 1", "Error 2"]


# ============================================================================
# Test: 版本控制表章节匹配
# ============================================================================


class TestVersionTableSectionMatching:
    """测试版本控制表章节匹配"""

    def test_match_section_14(self) -> None:
        """测试匹配第 14 章"""
        doc = """# Contract

## 14. 版本控制

| 版本 | 日期 | 变更说明 |
|------|------|----------|
| v2.18.0 | 2026-02-01 | 旧版本 |
"""
        updated, success = insert_version_row_in_doc(doc, "2.19.0", "2026-02-02", "新版本")
        assert success is True
        assert "v2.19.0" in updated

    def test_match_section_13(self) -> None:
        """测试匹配第 13 章（旧文档结构）"""
        doc = """# Contract

## 13. 版本控制

| 版本 | 日期 | 变更说明 |
|------|------|----------|
| v2.18.0 | 2026-02-01 | 旧版本 |
"""
        updated, success = insert_version_row_in_doc(doc, "2.19.0", "2026-02-02", "新版本")
        assert success is True
        assert "v2.19.0" in updated

    def test_no_match_without_version_control_section(self) -> None:
        """测试无版本控制章节时不匹配"""
        doc = """# Contract

## 其他章节

Some content here.
"""
        updated, success = insert_version_row_in_doc(doc, "2.19.0", "2026-02-02", "新版本")
        assert success is False
