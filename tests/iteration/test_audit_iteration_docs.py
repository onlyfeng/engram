#!/usr/bin/env python3
"""
audit_iteration_docs.py 单元测试

覆盖功能:
1. 无 inconsistency 返回码 0
2. 有 inconsistency 返回码 1
3. 报告中关键段落存在性（标题/范围/总结）
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from scripts.iteration.audit_iteration_docs import (
    IterationIndexEntry,
    generate_report,
    parse_acceptance_matrix,
    run_audit,
    scan_iteration_files,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def temp_project():
    """创建临时项目目录结构。"""
    with tempfile.TemporaryDirectory(prefix="test_audit_") as tmpdir:
        project = Path(tmpdir)
        (project / "docs" / "acceptance").mkdir(parents=True)
        yield project


@pytest.fixture
def temp_project_with_matrix(temp_project: Path) -> Path:
    """创建带有索引表的临时项目（无 inconsistency）。"""
    matrix_content = """# 验收测试矩阵

## 迭代回归记录索引

| 迭代 | 日期 | 状态 | 计划 | 详细记录 | 说明 |
|------|------|------|------|----------|------|
| **Iteration 2** | 2026-02-01 | ✅ PASS | - | [iteration_2_regression.md](iteration_2_regression.md) | 已完成 |
| Iteration 1 | 2026-01-31 | ✅ PASS | - | [iteration_1_regression.md](iteration_1_regression.md) | 已完成 |

---

## 其他内容
"""
    matrix_file = temp_project / "docs" / "acceptance" / "00_acceptance_matrix.md"
    matrix_file.write_text(matrix_content, encoding="utf-8")

    # 创建对应的 regression 文件
    for n in [1, 2]:
        regression = temp_project / "docs" / "acceptance" / f"iteration_{n}_regression.md"
        regression.write_text(f"# Iteration {n} 回归记录\n\n内容...\n", encoding="utf-8")

    return temp_project


@pytest.fixture
def temp_project_with_inconsistency(temp_project: Path) -> Path:
    """创建带有 inconsistency 的临时项目（SUPERSEDED 但缺少声明）。"""
    matrix_content = """# 验收测试矩阵

## 迭代回归记录索引

| 迭代 | 日期 | 状态 | 计划 | 详细记录 | 说明 |
|------|------|------|------|----------|------|
| **Iteration 2** | 2026-02-01 | ✅ PASS | - | [iteration_2_regression.md](iteration_2_regression.md) | 当前活跃 |
| Iteration 1 | 2026-01-31 | 🔄 SUPERSEDED | - | [iteration_1_regression.md](iteration_1_regression.md) | 已被 Iteration 2 取代 |

---
"""
    matrix_file = temp_project / "docs" / "acceptance" / "00_acceptance_matrix.md"
    matrix_file.write_text(matrix_content, encoding="utf-8")

    # 创建 regression 文件，但 iteration_1 缺少 superseded 声明
    (temp_project / "docs" / "acceptance" / "iteration_2_regression.md").write_text(
        "# Iteration 2 回归记录\n\n内容...\n", encoding="utf-8"
    )
    # 故意不添加 superseded 声明
    (temp_project / "docs" / "acceptance" / "iteration_1_regression.md").write_text(
        "# Iteration 1 回归记录\n\n内容...\n", encoding="utf-8"
    )

    return temp_project


# ============================================================================
# scan_iteration_files 测试
# ============================================================================


class TestScanIterationFiles:
    """scan_iteration_files 函数测试"""

    def test_returns_empty_for_empty_dir(self, temp_project: Path):
        """测试空目录返回空列表"""
        acceptance_dir = temp_project / "docs" / "acceptance"
        result = scan_iteration_files(acceptance_dir)
        assert result == []

    def test_finds_iteration_files(self, temp_project_with_matrix: Path):
        """测试能找到迭代文件"""
        acceptance_dir = temp_project_with_matrix / "docs" / "acceptance"
        result = scan_iteration_files(acceptance_dir)

        assert len(result) == 2
        iter_nums = {f.iteration_number for f in result}
        assert iter_nums == {1, 2}

    def test_detects_superseded_header(self, temp_project: Path):
        """测试检测 superseded 声明"""
        acceptance_dir = temp_project / "docs" / "acceptance"

        # 创建带 superseded 声明的文件
        content = """> **⚠️ Superseded by Iteration 5**

# Iteration 4 回归记录
"""
        (acceptance_dir / "iteration_4_regression.md").write_text(content, encoding="utf-8")

        result = scan_iteration_files(acceptance_dir)
        assert len(result) == 1
        assert result[0].has_superseded_header is True
        assert result[0].superseded_successor == 5


# ============================================================================
# parse_acceptance_matrix 测试
# ============================================================================


class TestParseAcceptanceMatrix:
    """parse_acceptance_matrix 函数测试"""

    def test_returns_empty_for_missing_file(self, temp_project: Path):
        """测试文件不存在返回空列表"""
        matrix_path = temp_project / "docs" / "acceptance" / "nonexistent.md"
        result = parse_acceptance_matrix(matrix_path)
        assert result == []

    def test_parses_index_entries(self, temp_project_with_matrix: Path):
        """测试解析索引条目"""
        matrix_path = temp_project_with_matrix / "docs" / "acceptance" / "00_acceptance_matrix.md"
        result = parse_acceptance_matrix(matrix_path)

        assert len(result) == 2
        iter_nums = [e.iteration_number for e in result]
        assert 1 in iter_nums
        assert 2 in iter_nums

    def test_detects_superseded_status(self, temp_project_with_inconsistency: Path):
        """测试检测 SUPERSEDED 状态"""
        matrix_path = (
            temp_project_with_inconsistency / "docs" / "acceptance" / "00_acceptance_matrix.md"
        )
        result = parse_acceptance_matrix(matrix_path)

        superseded_entries = [e for e in result if e.is_superseded]
        assert len(superseded_entries) == 1
        assert superseded_entries[0].iteration_number == 1


# ============================================================================
# run_audit 测试
# ============================================================================


class TestRunAudit:
    """run_audit 函数测试"""

    def test_no_inconsistency_when_all_valid(self, temp_project_with_matrix: Path):
        """测试无 inconsistency 场景"""
        result = run_audit(temp_project_with_matrix)

        assert len(result.inconsistencies) == 0
        assert len(result.missing_files) == 0
        assert len(result.files) == 2
        assert len(result.index_entries) == 2

    def test_detects_superseded_missing_header(self, temp_project_with_inconsistency: Path):
        """测试检测 SUPERSEDED 缺少声明"""
        result = run_audit(temp_project_with_inconsistency)

        assert len(result.inconsistencies) > 0
        # 应该有 SUPERSEDED_MISSING_HEADER 类型的不一致
        types = [t for t, _, _ in result.inconsistencies]
        assert "SUPERSEDED_MISSING_HEADER" in types

    def test_detects_orphan_files(self, temp_project_with_matrix: Path):
        """测试检测孤儿文件"""
        # 创建一个不在索引中的文件
        orphan = temp_project_with_matrix / "docs" / "acceptance" / "iteration_99_regression.md"
        orphan.write_text("# Orphan file\n", encoding="utf-8")

        result = run_audit(temp_project_with_matrix)

        assert "iteration_99_regression.md" in result.orphan_files


# ============================================================================
# generate_report 测试
# ============================================================================


class TestGenerateReport:
    """generate_report 函数测试"""

    def test_report_contains_title(self, temp_project_with_matrix: Path):
        """测试报告包含标题"""
        result = run_audit(temp_project_with_matrix)
        report = generate_report(result, temp_project_with_matrix)

        assert "# 迭代文档审计报告" in report

    def test_report_contains_scope_section(self, temp_project_with_matrix: Path):
        """测试报告包含审计范围段落"""
        result = run_audit(temp_project_with_matrix)
        report = generate_report(result, temp_project_with_matrix)

        assert "## 1. 审计范围" in report
        assert "00_acceptance_matrix.md" in report
        assert "docs/acceptance/" in report

    def test_report_contains_summary_section(self, temp_project_with_matrix: Path):
        """测试报告包含审计总结段落"""
        result = run_audit(temp_project_with_matrix)
        report = generate_report(result, temp_project_with_matrix)

        assert "## 5. 审计总结" in report
        assert "总迭代数" in report
        assert "一致性问题数" in report

    def test_report_contains_file_scan_section(self, temp_project_with_matrix: Path):
        """测试报告包含文件扫描结果段落"""
        result = run_audit(temp_project_with_matrix)
        report = generate_report(result, temp_project_with_matrix)

        assert "## 2. 文件扫描结果" in report
        assert "发现的迭代文件" in report

    def test_report_contains_consistency_section(self, temp_project_with_matrix: Path):
        """测试报告包含一致性对照段落"""
        result = run_audit(temp_project_with_matrix)
        report = generate_report(result, temp_project_with_matrix)

        assert "## 3. 索引与文件一致性对照" in report

    def test_report_shows_no_issues_when_clean(self, temp_project_with_matrix: Path):
        """测试无问题时显示相应信息"""
        result = run_audit(temp_project_with_matrix)
        report = generate_report(result, temp_project_with_matrix)

        assert "## 4. 发现的问题" in report
        assert "✅ 未发现问题" in report

    def test_report_shows_issues_when_present(self, temp_project_with_inconsistency: Path):
        """测试有问题时显示问题列表"""
        result = run_audit(temp_project_with_inconsistency)
        report = generate_report(result, temp_project_with_inconsistency)

        assert "## 4. 发现的问题" in report
        assert "🔴 不一致项" in report
        assert "SUPERSEDED_MISSING_HEADER" in report


# ============================================================================
# 返回码测试（main 函数行为）
# ============================================================================


class TestReturnCode:
    """返回码测试"""

    def test_returns_zero_when_no_inconsistency(self, temp_project_with_matrix: Path):
        """测试无 inconsistency 时返回码为 0"""
        result = run_audit(temp_project_with_matrix)

        # 模拟 main 函数的返回码逻辑
        exit_code = 1 if result.inconsistencies or result.missing_files else 0
        assert exit_code == 0

    def test_returns_one_when_has_inconsistency(self, temp_project_with_inconsistency: Path):
        """测试有 inconsistency 时返回码为 1"""
        result = run_audit(temp_project_with_inconsistency)

        # 模拟 main 函数的返回码逻辑
        exit_code = 1 if result.inconsistencies or result.missing_files else 0
        assert exit_code == 1

    def test_returns_one_when_has_missing_files(self, temp_project: Path):
        """测试有缺失文件时返回码为 1"""
        # 创建索引表引用不存在的文件
        matrix_content = """# 验收测试矩阵

## 迭代回归记录索引

| 迭代 | 日期 | 状态 | 计划 | 详细记录 | 说明 |
|------|------|------|------|----------|------|
| **Iteration 1** | 2026-01-31 | ✅ PASS | - | [iteration_1_regression.md](iteration_1_regression.md) | 已完成 |

---
"""
        matrix_file = temp_project / "docs" / "acceptance" / "00_acceptance_matrix.md"
        matrix_file.write_text(matrix_content, encoding="utf-8")
        # 不创建 iteration_1_regression.md 文件

        result = run_audit(temp_project)

        assert len(result.missing_files) > 0
        exit_code = 1 if result.inconsistencies or result.missing_files else 0
        assert exit_code == 1


# ============================================================================
# 数据结构测试
# ============================================================================


class TestIterationIndexEntry:
    """IterationIndexEntry 数据结构测试"""

    def test_is_superseded_property(self):
        """测试 is_superseded 属性"""
        entry_superseded = IterationIndexEntry(
            iteration_number=1,
            date="2026-01-31",
            status="🔄 SUPERSEDED",
            plan_link=None,
            regression_link="iteration_1_regression.md",
            description="已被 Iteration 2 取代",
            row_index=0,
        )
        assert entry_superseded.is_superseded is True

        entry_pass = IterationIndexEntry(
            iteration_number=2,
            date="2026-02-01",
            status="✅ PASS",
            plan_link=None,
            regression_link="iteration_2_regression.md",
            description="已完成",
            row_index=0,
        )
        assert entry_pass.is_superseded is False

    def test_get_successor_number(self):
        """测试 get_successor_number 方法"""
        entry = IterationIndexEntry(
            iteration_number=1,
            date="2026-01-31",
            status="🔄 SUPERSEDED",
            plan_link=None,
            regression_link="iteration_1_regression.md",
            description="已被 Iteration 2 取代",
            row_index=0,
        )
        assert entry.get_successor_number() == 2

        entry_en = IterationIndexEntry(
            iteration_number=3,
            date="2026-01-31",
            status="🔄 SUPERSEDED",
            plan_link=None,
            regression_link="iteration_3_regression.md",
            description="Superseded by Iteration 4",
            row_index=0,
        )
        assert entry_en.get_successor_number() == 4

        entry_no_successor = IterationIndexEntry(
            iteration_number=5,
            date="2026-01-31",
            status="🔄 SUPERSEDED",
            plan_link=None,
            regression_link="iteration_5_regression.md",
            description="已废弃",
            row_index=0,
        )
        assert entry_no_successor.get_successor_number() is None
