#!/usr/bin/env python3
"""
promote_iteration.py 单元测试

覆盖功能:
1. 正常晋升 N（插入索引置顶、文件存在）
2. SSOT 冲突时报错并建议 next available
3. --supersede oldN 时同时更新 oldN regression 头部与索引说明
4. 幂等/重复运行策略（覆盖、跳过、报错）
5. 与 check_no_iteration_links_in_docs.py 的一致性断言

Fixtures 使用临时目录构造 .iteration/ + docs/acceptance/ 结构。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

# 添加脚本目录到 path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "iteration"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))

# 导入检查脚本的解析函数用于一致性断言
from check_no_iteration_links_in_docs import (
    check_index_integrity,
    check_superseded_consistency,
    parse_acceptance_matrix,
)
from promote_iteration import (
    SourceNotFoundError,
    SSOTConflictError,
    SupersedeValidationError,
    add_superseded_header,
    check_ssot_conflict,
    create_index_entry,
    files_are_identical,
    get_next_available_number,
    get_ssot_iteration_numbers,
    insert_index_entry,
    parse_index_table_position,
    promote_iteration,
    update_matrix_for_supersede,
    validate_supersede_target,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def temp_project():
    """创建临时项目目录结构，模拟完整的项目布局。"""
    with tempfile.TemporaryDirectory(prefix="test_promote_") as tmpdir:
        project = Path(tmpdir)

        # 创建目录结构
        (project / ".iteration").mkdir(parents=True)
        (project / "docs" / "acceptance").mkdir(parents=True)

        yield project


@pytest.fixture
def temp_project_with_matrix(temp_project: Path) -> Path:
    """创建带有索引表的临时项目。"""
    matrix_content = """# 验收测试矩阵

## 迭代回归记录索引

| 迭代 | 日期 | 状态 | 计划 | 详细记录 | 说明 |
|------|------|------|------|----------|------|
| **Iteration 10** | 2026-02-01 | ⚠️ PARTIAL | - | [iteration_10_regression.md](iteration_10_regression.md) | 当前活跃迭代 |
| Iteration 9 | 2026-02-01 | ✅ PASS | - | [iteration_9_regression.md](iteration_9_regression.md) | 已完成 |

---

## 其他内容
"""
    matrix_file = temp_project / "docs" / "acceptance" / "00_acceptance_matrix.md"
    matrix_file.write_text(matrix_content, encoding="utf-8")

    # 创建已存在的 regression 文件
    for n in [9, 10]:
        regression = temp_project / "docs" / "acceptance" / f"iteration_{n}_regression.md"
        regression.write_text(f"# Iteration {n} 回归记录\n\n内容...\n", encoding="utf-8")

    return temp_project


@pytest.fixture
def temp_project_with_iteration(temp_project_with_matrix: Path) -> Path:
    """创建带有本地迭代草稿的临时项目。"""
    # 创建 .iteration/11/ 目录和文件
    iter_dir = temp_project_with_matrix / ".iteration" / "11"
    iter_dir.mkdir(parents=True)

    (iter_dir / "plan.md").write_text(
        """# Iteration 11 计划

## 目标

测试晋升功能。
""",
        encoding="utf-8",
    )

    (iter_dir / "regression.md").write_text(
        """# Iteration 11 回归记录

## 验收结果

待填写。
""",
        encoding="utf-8",
    )

    return temp_project_with_matrix


# ============================================================================
# 辅助函数测试
# ============================================================================


class TestGetSSOTIterationNumbers:
    """get_ssot_iteration_numbers 函数测试"""

    def test_returns_empty_for_empty_dir(self, temp_project: Path, monkeypatch):
        """测试空目录返回空集合"""
        monkeypatch.setattr("promote_iteration.SSOT_DIR", temp_project / "docs" / "acceptance")
        result = get_ssot_iteration_numbers()
        assert result == set()

    def test_finds_iteration_files(self, temp_project: Path, monkeypatch):
        """测试能找到迭代文件"""
        ssot_dir = temp_project / "docs" / "acceptance"
        monkeypatch.setattr("promote_iteration.SSOT_DIR", ssot_dir)

        # 创建一些迭代文件
        (ssot_dir / "iteration_5_plan.md").write_text("# Plan 5", encoding="utf-8")
        (ssot_dir / "iteration_5_regression.md").write_text("# Regression 5", encoding="utf-8")
        (ssot_dir / "iteration_10_regression.md").write_text("# Regression 10", encoding="utf-8")

        result = get_ssot_iteration_numbers()
        assert result == {5, 10}


class TestGetNextAvailableNumber:
    """get_next_available_number 函数测试"""

    def test_returns_1_for_empty(self, temp_project: Path, monkeypatch):
        """测试空目录返回 1"""
        monkeypatch.setattr("promote_iteration.SSOT_DIR", temp_project / "docs" / "acceptance")
        result = get_next_available_number()
        assert result == 1

    def test_returns_max_plus_1(self, temp_project: Path, monkeypatch):
        """测试返回最大编号 + 1"""
        ssot_dir = temp_project / "docs" / "acceptance"
        monkeypatch.setattr("promote_iteration.SSOT_DIR", ssot_dir)

        (ssot_dir / "iteration_5_regression.md").write_text("# 5", encoding="utf-8")
        (ssot_dir / "iteration_10_regression.md").write_text("# 10", encoding="utf-8")

        result = get_next_available_number()
        assert result == 11


class TestCheckSSOTConflict:
    """check_ssot_conflict 函数测试"""

    def test_no_conflict_for_new_number(self, temp_project: Path, monkeypatch):
        """测试新编号无冲突"""
        monkeypatch.setattr("promote_iteration.SSOT_DIR", temp_project / "docs" / "acceptance")
        # 不应该抛出异常
        check_ssot_conflict(1)

    def test_raises_for_existing_number(self, temp_project: Path, monkeypatch):
        """测试已存在编号抛出异常"""
        ssot_dir = temp_project / "docs" / "acceptance"
        monkeypatch.setattr("promote_iteration.SSOT_DIR", ssot_dir)

        (ssot_dir / "iteration_5_regression.md").write_text("# 5", encoding="utf-8")

        with pytest.raises(SSOTConflictError) as exc_info:
            check_ssot_conflict(5)

        assert exc_info.value.iteration_number == 5
        assert exc_info.value.suggested_number == 6


class TestValidateSupersedeTarget:
    """validate_supersede_target 函数测试（与 R6/R7 规则对齐）"""

    def test_raises_when_matrix_not_exists(self, temp_project: Path, monkeypatch):
        """测试索引表不存在时抛出错误"""
        monkeypatch.setattr("promote_iteration.MATRIX_FILE", temp_project / "nonexistent.md")
        monkeypatch.setattr("promote_iteration.SSOT_DIR", temp_project / "docs" / "acceptance")

        with pytest.raises(SupersedeValidationError) as exc_info:
            validate_supersede_target(10)

        assert exc_info.value.old_iteration == 10
        assert "索引表" in exc_info.value.reason
        assert "00_acceptance_matrix.md" in exc_info.value.reason

    def test_raises_when_iteration_not_in_index(self, temp_project_with_matrix: Path, monkeypatch):
        """测试迭代不在索引表中时抛出错误"""
        monkeypatch.setattr("promote_iteration.REPO_ROOT", temp_project_with_matrix)
        monkeypatch.setattr(
            "promote_iteration.SSOT_DIR",
            temp_project_with_matrix / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "promote_iteration.MATRIX_FILE",
            temp_project_with_matrix / "docs" / "acceptance" / "00_acceptance_matrix.md",
        )

        # 尝试 supersede 一个不在索引中的迭代
        with pytest.raises(SupersedeValidationError) as exc_info:
            validate_supersede_target(99)

        assert exc_info.value.old_iteration == 99
        assert "不在索引表中" in exc_info.value.reason
        assert "promote_iteration.py" in exc_info.value.suggestion

    def test_raises_when_regression_file_missing(self, temp_project_with_matrix: Path, monkeypatch):
        """测试 regression 文件不存在时抛出错误（R7 对齐）"""
        monkeypatch.setattr("promote_iteration.REPO_ROOT", temp_project_with_matrix)
        monkeypatch.setattr(
            "promote_iteration.SSOT_DIR",
            temp_project_with_matrix / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "promote_iteration.MATRIX_FILE",
            temp_project_with_matrix / "docs" / "acceptance" / "00_acceptance_matrix.md",
        )

        # 删除 iteration_10_regression.md 文件
        regression_file = (
            temp_project_with_matrix / "docs" / "acceptance" / "iteration_10_regression.md"
        )
        regression_file.unlink()

        with pytest.raises(SupersedeValidationError) as exc_info:
            validate_supersede_target(10)

        assert exc_info.value.old_iteration == 10
        assert "regression" in exc_info.value.reason.lower()
        assert "不存在" in exc_info.value.reason

    def test_passes_when_all_conditions_met(self, temp_project_with_matrix: Path, monkeypatch):
        """测试所有条件满足时不抛出错误"""
        monkeypatch.setattr("promote_iteration.REPO_ROOT", temp_project_with_matrix)
        monkeypatch.setattr(
            "promote_iteration.SSOT_DIR",
            temp_project_with_matrix / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "promote_iteration.MATRIX_FILE",
            temp_project_with_matrix / "docs" / "acceptance" / "00_acceptance_matrix.md",
        )

        # 不应该抛出异常（Iteration 10 在索引中且有 regression 文件）
        validate_supersede_target(10)

    def test_error_message_includes_r7_hint(self, temp_project_with_matrix: Path, monkeypatch):
        """测试错误信息包含 R7 相关提示"""
        monkeypatch.setattr("promote_iteration.REPO_ROOT", temp_project_with_matrix)
        monkeypatch.setattr(
            "promote_iteration.SSOT_DIR",
            temp_project_with_matrix / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "promote_iteration.MATRIX_FILE",
            temp_project_with_matrix / "docs" / "acceptance" / "00_acceptance_matrix.md",
        )

        # 删除 regression 文件
        regression_file = (
            temp_project_with_matrix / "docs" / "acceptance" / "iteration_10_regression.md"
        )
        regression_file.unlink()

        with pytest.raises(SupersedeValidationError) as exc_info:
            validate_supersede_target(10)

        # 建议应该包含创建文件或修复索引的提示
        assert "创建" in exc_info.value.suggestion or "修复" in exc_info.value.suggestion


class TestFilesAreIdentical:
    """files_are_identical 函数测试"""

    def test_identical_files(self, temp_project: Path):
        """测试相同内容的文件"""
        file1 = temp_project / "file1.md"
        file2 = temp_project / "file2.md"

        content = "Same content"
        file1.write_text(content, encoding="utf-8")
        file2.write_text(content, encoding="utf-8")

        assert files_are_identical(file1, file2) is True

    def test_different_files(self, temp_project: Path):
        """测试不同内容的文件"""
        file1 = temp_project / "file1.md"
        file2 = temp_project / "file2.md"

        file1.write_text("Content A", encoding="utf-8")
        file2.write_text("Content B", encoding="utf-8")

        assert files_are_identical(file1, file2) is False

    def test_missing_file(self, temp_project: Path):
        """测试文件不存在"""
        file1 = temp_project / "file1.md"
        file2 = temp_project / "missing.md"

        file1.write_text("Content", encoding="utf-8")

        assert files_are_identical(file1, file2) is False


# ============================================================================
# 索引表操作测试
# ============================================================================


class TestParseIndexTablePosition:
    """parse_index_table_position 函数测试"""

    def test_finds_insert_position(self):
        """测试找到正确的插入位置"""
        content = """# 验收测试矩阵

## 迭代回归记录索引

| 迭代 | 日期 | 状态 | 计划 | 详细记录 | 说明 |
|------|------|------|------|----------|------|
| **Iteration 10** | 2026-02-01 | ⚠️ PARTIAL | - | - | 当前活跃 |
| Iteration 9 | 2026-02-01 | ✅ PASS | - | - | 已完成 |

---
"""
        insert_pos, table_end = parse_index_table_position(content)

        # 插入位置应该在分隔行之后（第 6 行，0-indexed）
        assert insert_pos == 6
        assert table_end > insert_pos


class TestCreateIndexEntry:
    """create_index_entry 函数测试"""

    def test_creates_entry_with_links(self):
        """测试创建带链接的条目"""
        entry = create_index_entry(
            11,
            "2026-02-02",
            plan_link="plan",
            regression_link="regression",
        )

        assert "**Iteration 11**" in entry
        assert "2026-02-02" in entry
        assert "iteration_11_plan.md" in entry
        assert "iteration_11_regression.md" in entry

    def test_creates_entry_without_links(self):
        """测试创建不带链接的条目"""
        entry = create_index_entry(11, "2026-02-02")

        assert "**Iteration 11**" in entry
        assert "- |" in entry  # 无链接时显示 -


class TestInsertIndexEntry:
    """insert_index_entry 函数测试"""

    def test_inserts_at_correct_position(self):
        """测试在正确位置插入"""
        content = """Line 0
Line 1
Line 2"""
        entry = "NEW ENTRY"

        result = insert_index_entry(content, entry, 1)

        lines = result.splitlines()
        assert lines[0] == "Line 0"
        assert lines[1] == "NEW ENTRY"
        assert lines[2] == "Line 1"


class TestUpdateMatrixForSupersede:
    """update_matrix_for_supersede 函数测试"""

    def test_updates_old_iteration_status(self):
        """测试更新旧迭代状态（6列表格）"""
        # 6 列表格格式: | 迭代 | 日期 | 状态 | 计划 | 详细记录 | 说明 |
        content = """| **Iteration 10** | 2026-02-01 | ⚠️ PARTIAL | - | [iteration_10_regression.md](iteration_10_regression.md) | 当前活跃 |
| Iteration 9 | 2026-02-01 | ✅ PASS | - | [iteration_9_regression.md](iteration_9_regression.md) | 已完成 |"""

        result = update_matrix_for_supersede(content, 10, 11)

        assert "🔄 SUPERSEDED" in result
        assert "已被 Iteration 11 取代" in result

    def test_updates_correct_columns_in_6col_table(self):
        """测试确保更新正确的列位置（状态列和说明列）"""
        # 6 列表格格式: | 迭代 | 日期 | 状态 | 计划 | 详细记录 | 说明 |
        content = """| **Iteration 10** | 2026-02-01 | ⚠️ PARTIAL | [plan.md](plan.md) | [regression.md](regression.md) | 原说明 |"""

        result = update_matrix_for_supersede(content, 10, 11)
        cells = result.split("|")

        # 验证状态列 (index 3) 被更新
        assert "SUPERSEDED" in cells[3]
        # 验证说明列 (index 6) 被更新为唯一后继声明
        assert "已被 Iteration 11 取代" in cells[6]
        # 验证计划列 (index 4) 未被修改
        assert "plan.md" in cells[4]
        # 验证详细记录列 (index 5) 未被修改
        assert "regression.md" in cells[5]

    def test_preserves_other_rows(self):
        """测试不影响其他行"""
        content = """| **Iteration 11** | 2026-02-02 | 🔄 PLANNING | - | - | 新迭代 |
| **Iteration 10** | 2026-02-01 | ⚠️ PARTIAL | - | - | 当前活跃 |
| Iteration 9 | 2026-02-01 | ✅ PASS | - | - | 已完成 |"""

        result = update_matrix_for_supersede(content, 10, 11)

        # Iteration 9 的行应该保持不变
        assert "| Iteration 9 | 2026-02-01 | ✅ PASS | - | - | 已完成 |" in result
        # Iteration 11 的行应该保持不变
        assert "**Iteration 11**" in result
        assert "🔄 PLANNING" in result


# ============================================================================
# Regression 文件更新测试
# ============================================================================


class TestAddSupersededHeader:
    """add_superseded_header 函数测试"""

    def test_adds_header_to_file_without(self):
        """测试向无声明的文件添加 superseded 头部"""
        content = """# Iteration 10 回归记录

## 验收结果

测试通过。
"""
        result = add_superseded_header(content, 11)

        assert "Superseded by Iteration 11" in result
        assert "iteration_11_regression.md" in result

    def test_updates_existing_header(self):
        """测试更新现有的 superseded 声明"""
        content = """> **⚠️ Superseded by Iteration 10**

# Iteration 9 回归记录
"""
        result = add_superseded_header(content, 11)

        assert "Superseded by Iteration 11" in result
        assert "Superseded by Iteration 10" not in result

    def test_header_includes_separator(self):
        """测试 superseded 头部包含 --- 分隔线"""
        content = """# Iteration 10 回归记录

内容...
"""
        result = add_superseded_header(content, 11)

        # 验证输出包含 ---
        assert "---" in result
        # 验证 --- 在 Superseded 声明之后
        superseded_pos = result.find("Superseded by Iteration 11")
        separator_pos = result.find("---")
        assert superseded_pos < separator_pos

    def test_inserts_before_first_non_empty_content(self):
        """测试插入到首个非空内容之前"""
        content = """# Iteration 10 回归记录

## 验收结果
"""
        result = add_superseded_header(content, 11)

        # 验证 Superseded 声明在标题之前
        lines = result.splitlines()
        superseded_line_idx = None
        title_line_idx = None

        for i, line in enumerate(lines):
            if "Superseded by Iteration 11" in line:
                superseded_line_idx = i
            if "# Iteration 10 回归记录" in line:
                title_line_idx = i

        assert superseded_line_idx is not None
        assert title_line_idx is not None
        assert superseded_line_idx < title_line_idx

    def test_preserves_leading_empty_lines(self):
        """测试保留开头的空行"""
        content = """

# Iteration 10 回归记录
"""
        result = add_superseded_header(content, 11)

        # 验证开头空行被保留
        assert result.startswith("\n")
        # 验证 Superseded 声明存在
        assert "Superseded by Iteration 11" in result


# ============================================================================
# 核心晋升功能测试
# ============================================================================


class TestPromoteIterationNormal:
    """正常晋升场景测试"""

    def test_promotes_new_iteration(self, temp_project_with_iteration: Path, monkeypatch):
        """测试正常晋升新迭代"""
        monkeypatch.setattr("promote_iteration.REPO_ROOT", temp_project_with_iteration)
        monkeypatch.setattr(
            "promote_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )
        monkeypatch.setattr(
            "promote_iteration.SSOT_DIR",
            temp_project_with_iteration / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "promote_iteration.MATRIX_FILE",
            temp_project_with_iteration / "docs" / "acceptance" / "00_acceptance_matrix.md",
        )

        result = promote_iteration(11)

        assert result.success is True
        assert len(result.files_copied) == 2
        assert result.index_updated is True

        # 验证文件已创建
        ssot_dir = temp_project_with_iteration / "docs" / "acceptance"
        assert (ssot_dir / "iteration_11_plan.md").exists()
        assert (ssot_dir / "iteration_11_regression.md").exists()

    def test_index_entry_inserted_at_top(self, temp_project_with_iteration: Path, monkeypatch):
        """测试索引条目插入到顶部"""
        monkeypatch.setattr("promote_iteration.REPO_ROOT", temp_project_with_iteration)
        monkeypatch.setattr(
            "promote_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )
        monkeypatch.setattr(
            "promote_iteration.SSOT_DIR",
            temp_project_with_iteration / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "promote_iteration.MATRIX_FILE",
            temp_project_with_iteration / "docs" / "acceptance" / "00_acceptance_matrix.md",
        )

        promote_iteration(11)

        # 读取更新后的索引表
        matrix_file = (
            temp_project_with_iteration / "docs" / "acceptance" / "00_acceptance_matrix.md"
        )
        content = matrix_file.read_text(encoding="utf-8")
        lines = content.splitlines()

        # 找到第一个数据行（在分隔行之后）
        for i, line in enumerate(lines):
            if line.strip().startswith("|") and "Iteration 11" in line:
                # 确认 Iteration 11 在 Iteration 10 之前
                for j in range(i + 1, len(lines)):
                    if "Iteration 10" in lines[j]:
                        # 成功：11 在 10 之前
                        return

        pytest.fail("Iteration 11 未插入到索引表顶部")


class TestPromoteIterationSSOTConflict:
    """SSOT 冲突场景测试"""

    def test_raises_conflict_for_existing_iteration(
        self, temp_project_with_matrix: Path, monkeypatch
    ):
        """测试已存在迭代时抛出冲突错误"""
        monkeypatch.setattr("promote_iteration.REPO_ROOT", temp_project_with_matrix)
        monkeypatch.setattr(
            "promote_iteration.ITERATION_DIR",
            temp_project_with_matrix / ".iteration",
        )
        monkeypatch.setattr(
            "promote_iteration.SSOT_DIR",
            temp_project_with_matrix / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "promote_iteration.MATRIX_FILE",
            temp_project_with_matrix / "docs" / "acceptance" / "00_acceptance_matrix.md",
        )

        # 创建与已存在迭代相同编号的本地草稿
        iter_dir = temp_project_with_matrix / ".iteration" / "10"
        iter_dir.mkdir(parents=True)
        (iter_dir / "plan.md").write_text("# New plan", encoding="utf-8")
        (iter_dir / "regression.md").write_text("# New regression", encoding="utf-8")

        with pytest.raises(SSOTConflictError) as exc_info:
            promote_iteration(10)

        assert exc_info.value.iteration_number == 10
        assert exc_info.value.suggested_number == 11

    def test_suggests_next_available_number(self, temp_project_with_matrix: Path, monkeypatch):
        """测试冲突时建议下一个可用编号"""
        monkeypatch.setattr("promote_iteration.REPO_ROOT", temp_project_with_matrix)
        monkeypatch.setattr(
            "promote_iteration.ITERATION_DIR",
            temp_project_with_matrix / ".iteration",
        )
        monkeypatch.setattr(
            "promote_iteration.SSOT_DIR",
            temp_project_with_matrix / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "promote_iteration.MATRIX_FILE",
            temp_project_with_matrix / "docs" / "acceptance" / "00_acceptance_matrix.md",
        )

        iter_dir = temp_project_with_matrix / ".iteration" / "9"
        iter_dir.mkdir(parents=True)
        (iter_dir / "regression.md").write_text("# New", encoding="utf-8")

        with pytest.raises(SSOTConflictError) as exc_info:
            promote_iteration(9)

        # 已存在 9 和 10，下一个应该是 11
        assert exc_info.value.suggested_number == 11


class TestPromoteIterationSupersede:
    """--supersede 参数测试"""

    def test_supersede_raises_when_old_iteration_not_in_index(
        self, temp_project_with_iteration: Path, monkeypatch
    ):
        """测试 --supersede 目标不在索引中时报错（非 dry-run）"""
        monkeypatch.setattr("promote_iteration.REPO_ROOT", temp_project_with_iteration)
        monkeypatch.setattr(
            "promote_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )
        monkeypatch.setattr(
            "promote_iteration.SSOT_DIR",
            temp_project_with_iteration / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "promote_iteration.MATRIX_FILE",
            temp_project_with_iteration / "docs" / "acceptance" / "00_acceptance_matrix.md",
        )

        # 尝试 supersede 一个不存在于索引中的迭代
        with pytest.raises(SupersedeValidationError) as exc_info:
            promote_iteration(11, supersede=99)

        assert exc_info.value.old_iteration == 99
        assert "不在索引表中" in exc_info.value.reason

    def test_supersede_raises_when_regression_missing(
        self, temp_project_with_iteration: Path, monkeypatch
    ):
        """测试 --supersede 目标的 regression 文件不存在时报错（R7 对齐）"""
        monkeypatch.setattr("promote_iteration.REPO_ROOT", temp_project_with_iteration)
        monkeypatch.setattr(
            "promote_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )
        monkeypatch.setattr(
            "promote_iteration.SSOT_DIR",
            temp_project_with_iteration / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "promote_iteration.MATRIX_FILE",
            temp_project_with_iteration / "docs" / "acceptance" / "00_acceptance_matrix.md",
        )

        # 删除 iteration_10_regression.md
        regression_file = (
            temp_project_with_iteration / "docs" / "acceptance" / "iteration_10_regression.md"
        )
        regression_file.unlink()

        with pytest.raises(SupersedeValidationError) as exc_info:
            promote_iteration(11, supersede=10)

        assert exc_info.value.old_iteration == 10
        assert "regression" in exc_info.value.reason.lower()

    def test_supersede_dry_run_skips_validation(
        self, temp_project_with_iteration: Path, monkeypatch
    ):
        """测试 --dry-run 模式下跳过前置校验"""
        monkeypatch.setattr("promote_iteration.REPO_ROOT", temp_project_with_iteration)
        monkeypatch.setattr(
            "promote_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )
        monkeypatch.setattr(
            "promote_iteration.SSOT_DIR",
            temp_project_with_iteration / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "promote_iteration.MATRIX_FILE",
            temp_project_with_iteration / "docs" / "acceptance" / "00_acceptance_matrix.md",
        )

        # 删除 regression 文件
        regression_file = (
            temp_project_with_iteration / "docs" / "acceptance" / "iteration_10_regression.md"
        )
        regression_file.unlink()

        # 使用 dry-run 模式不应该抛出错误
        result = promote_iteration(11, supersede=10, dry_run=True)
        assert result.success is True

    def test_supersede_updates_old_regression(self, temp_project_with_iteration: Path, monkeypatch):
        """测试 --supersede 更新旧 regression 文件头部"""
        monkeypatch.setattr("promote_iteration.REPO_ROOT", temp_project_with_iteration)
        monkeypatch.setattr(
            "promote_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )
        monkeypatch.setattr(
            "promote_iteration.SSOT_DIR",
            temp_project_with_iteration / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "promote_iteration.MATRIX_FILE",
            temp_project_with_iteration / "docs" / "acceptance" / "00_acceptance_matrix.md",
        )

        result = promote_iteration(11, supersede=10)

        assert result.superseded_updated is True

        # 验证旧 regression 文件已更新
        old_regression = (
            temp_project_with_iteration / "docs" / "acceptance" / "iteration_10_regression.md"
        )
        content = old_regression.read_text(encoding="utf-8")
        assert "Superseded by Iteration 11" in content

    def test_supersede_updates_index_status(self, temp_project_with_iteration: Path, monkeypatch):
        """测试 --supersede 更新索引表中旧迭代的状态"""
        monkeypatch.setattr("promote_iteration.REPO_ROOT", temp_project_with_iteration)
        monkeypatch.setattr(
            "promote_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )
        monkeypatch.setattr(
            "promote_iteration.SSOT_DIR",
            temp_project_with_iteration / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "promote_iteration.MATRIX_FILE",
            temp_project_with_iteration / "docs" / "acceptance" / "00_acceptance_matrix.md",
        )

        promote_iteration(11, supersede=10)

        # 验证索引表已更新
        matrix_file = (
            temp_project_with_iteration / "docs" / "acceptance" / "00_acceptance_matrix.md"
        )
        content = matrix_file.read_text(encoding="utf-8")

        # 检查 Iteration 10 行是否已更新
        for line in content.splitlines():
            if "Iteration 10" in line and "SUPERSEDED" in line:
                assert "已被 Iteration 11 取代" in line
                return

        pytest.fail("Iteration 10 未被标记为 SUPERSEDED")


class TestPromoteIterationIdempotent:
    """幂等性测试"""

    def test_skips_identical_files(self, temp_project_with_iteration: Path, monkeypatch):
        """测试相同内容的文件被跳过"""
        monkeypatch.setattr("promote_iteration.REPO_ROOT", temp_project_with_iteration)
        monkeypatch.setattr(
            "promote_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )
        monkeypatch.setattr(
            "promote_iteration.SSOT_DIR",
            temp_project_with_iteration / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "promote_iteration.MATRIX_FILE",
            temp_project_with_iteration / "docs" / "acceptance" / "00_acceptance_matrix.md",
        )

        # 第一次晋升
        result1 = promote_iteration(11)
        assert len(result1.files_copied) == 2
        assert len(result1.files_skipped) == 0

        # 第二次晋升（相同内容）
        result2 = promote_iteration(11)
        assert len(result2.files_copied) == 0
        assert len(result2.files_skipped) == 2

    def test_raises_for_different_content_without_force(
        self, temp_project_with_iteration: Path, monkeypatch
    ):
        """测试内容不同时不使用 --force 会报错"""
        monkeypatch.setattr("promote_iteration.REPO_ROOT", temp_project_with_iteration)
        monkeypatch.setattr(
            "promote_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )
        monkeypatch.setattr(
            "promote_iteration.SSOT_DIR",
            temp_project_with_iteration / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "promote_iteration.MATRIX_FILE",
            temp_project_with_iteration / "docs" / "acceptance" / "00_acceptance_matrix.md",
        )

        # 第一次晋升
        promote_iteration(11)

        # 修改源文件内容
        src_plan = temp_project_with_iteration / ".iteration" / "11" / "plan.md"
        src_plan.write_text("# Modified content", encoding="utf-8")

        # 第二次晋升（内容不同，无 --force）
        with pytest.raises(SSOTConflictError):
            promote_iteration(11)

    def test_force_overwrites_different_content(
        self, temp_project_with_iteration: Path, monkeypatch
    ):
        """测试 --force 可以覆盖不同内容"""
        monkeypatch.setattr("promote_iteration.REPO_ROOT", temp_project_with_iteration)
        monkeypatch.setattr(
            "promote_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )
        monkeypatch.setattr(
            "promote_iteration.SSOT_DIR",
            temp_project_with_iteration / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "promote_iteration.MATRIX_FILE",
            temp_project_with_iteration / "docs" / "acceptance" / "00_acceptance_matrix.md",
        )

        # 第一次晋升
        promote_iteration(11)

        # 修改源文件内容
        src_plan = temp_project_with_iteration / ".iteration" / "11" / "plan.md"
        new_content = "# Modified content"
        src_plan.write_text(new_content, encoding="utf-8")

        # 使用 --force 晋升
        result = promote_iteration(11, force=True)
        assert result.success is True

        # 验证目标文件已更新
        dst_plan = temp_project_with_iteration / "docs" / "acceptance" / "iteration_11_plan.md"
        assert dst_plan.read_text(encoding="utf-8") == new_content

    def test_skips_index_update_if_already_indexed(
        self, temp_project_with_iteration: Path, monkeypatch
    ):
        """测试已索引的迭代不重复更新索引"""
        monkeypatch.setattr("promote_iteration.REPO_ROOT", temp_project_with_iteration)
        monkeypatch.setattr(
            "promote_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )
        monkeypatch.setattr(
            "promote_iteration.SSOT_DIR",
            temp_project_with_iteration / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "promote_iteration.MATRIX_FILE",
            temp_project_with_iteration / "docs" / "acceptance" / "00_acceptance_matrix.md",
        )

        # 第一次晋升
        result1 = promote_iteration(11)
        assert result1.index_updated is True

        # 第二次晋升
        result2 = promote_iteration(11)
        assert result2.index_updated is False


class TestPromoteIterationDryRun:
    """--dry-run 参数测试"""

    def test_dry_run_does_not_modify_files(self, temp_project_with_iteration: Path, monkeypatch):
        """测试 --dry-run 不修改任何文件"""
        monkeypatch.setattr("promote_iteration.REPO_ROOT", temp_project_with_iteration)
        monkeypatch.setattr(
            "promote_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )
        monkeypatch.setattr(
            "promote_iteration.SSOT_DIR",
            temp_project_with_iteration / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "promote_iteration.MATRIX_FILE",
            temp_project_with_iteration / "docs" / "acceptance" / "00_acceptance_matrix.md",
        )

        # 记录原始索引内容
        matrix_file = (
            temp_project_with_iteration / "docs" / "acceptance" / "00_acceptance_matrix.md"
        )
        original_content = matrix_file.read_text(encoding="utf-8")

        result = promote_iteration(11, dry_run=True)

        assert result.success is True
        assert len(result.files_copied) == 2  # 报告要复制的文件

        # 验证文件未被创建
        ssot_dir = temp_project_with_iteration / "docs" / "acceptance"
        assert not (ssot_dir / "iteration_11_plan.md").exists()
        assert not (ssot_dir / "iteration_11_regression.md").exists()

        # 验证索引表未被修改
        assert matrix_file.read_text(encoding="utf-8") == original_content


class TestPromoteIterationSourceNotFound:
    """源文件不存在测试"""

    def test_raises_for_missing_source_dir(self, temp_project_with_matrix: Path, monkeypatch):
        """测试源目录不存在时抛出错误"""
        monkeypatch.setattr("promote_iteration.REPO_ROOT", temp_project_with_matrix)
        monkeypatch.setattr(
            "promote_iteration.ITERATION_DIR",
            temp_project_with_matrix / ".iteration",
        )
        monkeypatch.setattr(
            "promote_iteration.SSOT_DIR",
            temp_project_with_matrix / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "promote_iteration.MATRIX_FILE",
            temp_project_with_matrix / "docs" / "acceptance" / "00_acceptance_matrix.md",
        )

        with pytest.raises(SourceNotFoundError):
            promote_iteration(99)  # 不存在的迭代


# ============================================================================
# 与 check_no_iteration_links_in_docs.py 一致性测试
# ============================================================================


class TestConsistencyWithCheckScript:
    """与 check_no_iteration_links_in_docs.py 的一致性测试"""

    def test_promoted_iteration_passes_integrity_check(
        self, temp_project_with_iteration: Path, monkeypatch
    ):
        """测试晋升后的迭代能通过索引完整性检查"""
        monkeypatch.setattr("promote_iteration.REPO_ROOT", temp_project_with_iteration)
        monkeypatch.setattr(
            "promote_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )
        monkeypatch.setattr(
            "promote_iteration.SSOT_DIR",
            temp_project_with_iteration / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "promote_iteration.MATRIX_FILE",
            temp_project_with_iteration / "docs" / "acceptance" / "00_acceptance_matrix.md",
        )

        # 晋升迭代
        promote_iteration(11)

        # 使用 check_no_iteration_links_in_docs 的函数检查完整性
        integrity_result = check_index_integrity(temp_project_with_iteration)

        # 不应该有 R7（缺失文件）违规
        r7_violations = [v for v in integrity_result.violations if v.rule_id == "R7"]
        assert len(r7_violations) == 0, f"R7 violations: {r7_violations}"

        # 不应该有 R8（孤儿文件）违规
        r8_violations = [v for v in integrity_result.violations if v.rule_id == "R8"]
        assert len(r8_violations) == 0, f"R8 violations: {r8_violations}"

    def test_supersede_passes_consistency_check(
        self, temp_project_with_iteration: Path, monkeypatch
    ):
        """测试 --supersede 后通过 SUPERSEDED 一致性检查"""
        monkeypatch.setattr("promote_iteration.REPO_ROOT", temp_project_with_iteration)
        monkeypatch.setattr(
            "promote_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )
        monkeypatch.setattr(
            "promote_iteration.SSOT_DIR",
            temp_project_with_iteration / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "promote_iteration.MATRIX_FILE",
            temp_project_with_iteration / "docs" / "acceptance" / "00_acceptance_matrix.md",
        )

        # 晋升并标记 supersede
        promote_iteration(11, supersede=10)

        # 使用 check_no_iteration_links_in_docs 的函数检查一致性
        superseded_result = check_superseded_consistency(temp_project_with_iteration)

        # 检查 R1（缺后继声明）- 不应该有
        r1_violations = [v for v in superseded_result.violations if v.rule_id == "R1"]
        assert len(r1_violations) == 0, f"R1 violations: {r1_violations}"

        # 检查 R6（regression 缺 superseded 头部）- 不应该有
        r6_violations = [v for v in superseded_result.violations if v.rule_id == "R6"]
        assert len(r6_violations) == 0, f"R6 violations: {r6_violations}"

    def test_index_order_is_descending(self, temp_project_with_iteration: Path, monkeypatch):
        """测试索引表保持降序排列"""
        monkeypatch.setattr("promote_iteration.REPO_ROOT", temp_project_with_iteration)
        monkeypatch.setattr(
            "promote_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )
        monkeypatch.setattr(
            "promote_iteration.SSOT_DIR",
            temp_project_with_iteration / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "promote_iteration.MATRIX_FILE",
            temp_project_with_iteration / "docs" / "acceptance" / "00_acceptance_matrix.md",
        )

        # 晋升迭代
        promote_iteration(11)

        # 使用 parse_acceptance_matrix 解析索引表
        matrix_file = (
            temp_project_with_iteration / "docs" / "acceptance" / "00_acceptance_matrix.md"
        )
        entries = parse_acceptance_matrix(matrix_file)

        # 验证降序排列
        iteration_numbers = [e.iteration_number for e in entries]
        assert iteration_numbers == sorted(iteration_numbers, reverse=True), (
            f"索引表未按降序排列: {iteration_numbers}"
        )

    def test_promoted_iteration_appears_in_parsed_entries(
        self, temp_project_with_iteration: Path, monkeypatch
    ):
        """测试晋升的迭代出现在解析结果中"""
        monkeypatch.setattr("promote_iteration.REPO_ROOT", temp_project_with_iteration)
        monkeypatch.setattr(
            "promote_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )
        monkeypatch.setattr(
            "promote_iteration.SSOT_DIR",
            temp_project_with_iteration / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "promote_iteration.MATRIX_FILE",
            temp_project_with_iteration / "docs" / "acceptance" / "00_acceptance_matrix.md",
        )

        promote_iteration(11)

        matrix_file = (
            temp_project_with_iteration / "docs" / "acceptance" / "00_acceptance_matrix.md"
        )
        entries = parse_acceptance_matrix(matrix_file)

        iteration_numbers = [e.iteration_number for e in entries]
        assert 11 in iteration_numbers

        # 验证条目属性
        entry_11 = next(e for e in entries if e.iteration_number == 11)
        assert entry_11.regression_link == "iteration_11_regression.md"


# ============================================================================
# R1-R9 规则全覆盖测试
# ============================================================================


class TestSupersedeR1ToR9Compliance:
    """--supersede 后 R1-R9 规则全通过测试"""

    def test_supersede_passes_all_rules(self, temp_project_with_iteration: Path, monkeypatch):
        """测试 --supersede 后 R1-R9 规则全通过"""
        monkeypatch.setattr("promote_iteration.REPO_ROOT", temp_project_with_iteration)
        monkeypatch.setattr(
            "promote_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )
        monkeypatch.setattr(
            "promote_iteration.SSOT_DIR",
            temp_project_with_iteration / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "promote_iteration.MATRIX_FILE",
            temp_project_with_iteration / "docs" / "acceptance" / "00_acceptance_matrix.md",
        )

        # 晋升 Iteration 11 并标记 Iteration 10 为 superseded
        promote_iteration(11, supersede=10)

        # 检查 R1-R6 (SUPERSEDED 一致性)
        superseded_result = check_superseded_consistency(temp_project_with_iteration)
        r1_to_r6_violations = [
            v
            for v in superseded_result.violations
            if v.rule_id in ["R1", "R2", "R3", "R4", "R5", "R6"]
        ]
        assert len(r1_to_r6_violations) == 0, f"R1-R6 violations: {r1_to_r6_violations}"

        # 检查 R7-R9 (索引完整性)
        integrity_result = check_index_integrity(temp_project_with_iteration)
        r7_to_r9_violations = [
            v for v in integrity_result.violations if v.rule_id in ["R7", "R8", "R9"]
        ]
        assert len(r7_to_r9_violations) == 0, f"R7-R9 violations: {r7_to_r9_violations}"

    def test_r1_successor_declaration_present(self, temp_project_with_iteration: Path, monkeypatch):
        """R1: 后继链接必须存在 - 测试说明字段包含后继声明"""
        monkeypatch.setattr("promote_iteration.REPO_ROOT", temp_project_with_iteration)
        monkeypatch.setattr(
            "promote_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )
        monkeypatch.setattr(
            "promote_iteration.SSOT_DIR",
            temp_project_with_iteration / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "promote_iteration.MATRIX_FILE",
            temp_project_with_iteration / "docs" / "acceptance" / "00_acceptance_matrix.md",
        )

        promote_iteration(11, supersede=10)

        # 读取索引表
        matrix_file = (
            temp_project_with_iteration / "docs" / "acceptance" / "00_acceptance_matrix.md"
        )
        content = matrix_file.read_text(encoding="utf-8")

        # 找到 Iteration 10 行并验证包含 "已被 Iteration 11 取代"
        for line in content.splitlines():
            if "Iteration 10" in line and "SUPERSEDED" in line:
                assert "已被 Iteration 11 取代" in line, "R1 violation: 缺少后继声明"
                return

        pytest.fail("未找到 Iteration 10 的 SUPERSEDED 行")

    def test_r3_successor_ordering(self, temp_project_with_iteration: Path, monkeypatch):
        """R3: 后继排序在上方 - 测试后继迭代在被取代迭代上方"""
        monkeypatch.setattr("promote_iteration.REPO_ROOT", temp_project_with_iteration)
        monkeypatch.setattr(
            "promote_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )
        monkeypatch.setattr(
            "promote_iteration.SSOT_DIR",
            temp_project_with_iteration / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "promote_iteration.MATRIX_FILE",
            temp_project_with_iteration / "docs" / "acceptance" / "00_acceptance_matrix.md",
        )

        promote_iteration(11, supersede=10)

        # 解析索引表并验证顺序
        matrix_file = (
            temp_project_with_iteration / "docs" / "acceptance" / "00_acceptance_matrix.md"
        )
        entries = parse_acceptance_matrix(matrix_file)

        # 找到 Iteration 11 和 Iteration 10 的 row_index
        iter_11_idx = None
        iter_10_idx = None
        for entry in entries:
            if entry.iteration_number == 11:
                iter_11_idx = entry.row_index
            if entry.iteration_number == 10:
                iter_10_idx = entry.row_index

        assert iter_11_idx is not None, "Iteration 11 未在索引中"
        assert iter_10_idx is not None, "Iteration 10 未在索引中"
        assert iter_11_idx < iter_10_idx, (
            f"R3 violation: Iteration 11 (行 {iter_11_idx + 1}) 应在 "
            f"Iteration 10 (行 {iter_10_idx + 1}) 上方"
        )

    def test_r6_regression_superseded_header(self, temp_project_with_iteration: Path, monkeypatch):
        """R6: regression 声明必须存在 - 测试 regression 文件顶部有 superseded 声明"""
        monkeypatch.setattr("promote_iteration.REPO_ROOT", temp_project_with_iteration)
        monkeypatch.setattr(
            "promote_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )
        monkeypatch.setattr(
            "promote_iteration.SSOT_DIR",
            temp_project_with_iteration / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "promote_iteration.MATRIX_FILE",
            temp_project_with_iteration / "docs" / "acceptance" / "00_acceptance_matrix.md",
        )

        promote_iteration(11, supersede=10)

        # 读取 Iteration 10 的 regression 文件
        regression_file = (
            temp_project_with_iteration / "docs" / "acceptance" / "iteration_10_regression.md"
        )
        content = regression_file.read_text(encoding="utf-8")

        # 验证前 20 行包含 superseded 声明
        lines = content.splitlines()[:20]
        has_superseded = any("Superseded by Iteration 11" in line for line in lines)
        assert has_superseded, (
            "R6 violation: regression 文件前 20 行缺少 'Superseded by Iteration 11' 声明"
        )

    def test_r9_descending_order(self, temp_project_with_iteration: Path, monkeypatch):
        """R9: 索引降序排列 - 测试迭代编号按降序排列"""
        monkeypatch.setattr("promote_iteration.REPO_ROOT", temp_project_with_iteration)
        monkeypatch.setattr(
            "promote_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )
        monkeypatch.setattr(
            "promote_iteration.SSOT_DIR",
            temp_project_with_iteration / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "promote_iteration.MATRIX_FILE",
            temp_project_with_iteration / "docs" / "acceptance" / "00_acceptance_matrix.md",
        )

        promote_iteration(11)

        # 解析索引表
        matrix_file = (
            temp_project_with_iteration / "docs" / "acceptance" / "00_acceptance_matrix.md"
        )
        entries = parse_acceptance_matrix(matrix_file)

        # 验证迭代编号降序排列
        iteration_numbers = [e.iteration_number for e in entries]
        assert iteration_numbers == sorted(iteration_numbers, reverse=True), (
            f"R9 violation: 索引表未按降序排列: {iteration_numbers}"
        )


class TestSupersedeValidationR6R7Alignment:
    """--supersede 前置校验与 check_no_iteration_links_in_docs.py R6/R7 对齐测试"""

    def test_r7_missing_file_detected_before_supersede(
        self, temp_project_with_matrix: Path, monkeypatch
    ):
        """R7 对齐: 索引中有链接但文件不存在时，supersede 应报错"""
        monkeypatch.setattr("promote_iteration.REPO_ROOT", temp_project_with_matrix)
        monkeypatch.setattr(
            "promote_iteration.SSOT_DIR",
            temp_project_with_matrix / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "promote_iteration.MATRIX_FILE",
            temp_project_with_matrix / "docs" / "acceptance" / "00_acceptance_matrix.md",
        )
        monkeypatch.setattr(
            "promote_iteration.ITERATION_DIR",
            temp_project_with_matrix / ".iteration",
        )

        # 创建本地迭代
        iter_dir = temp_project_with_matrix / ".iteration" / "11"
        iter_dir.mkdir(parents=True)
        (iter_dir / "plan.md").write_text("# Plan", encoding="utf-8")
        (iter_dir / "regression.md").write_text("# Regression", encoding="utf-8")

        # 删除 regression 文件（但索引中仍有链接）
        regression_file = (
            temp_project_with_matrix / "docs" / "acceptance" / "iteration_10_regression.md"
        )
        regression_file.unlink()

        # 运行 check_index_integrity 应该检测到 R7 违规
        integrity_result = check_index_integrity(temp_project_with_matrix)
        r7_violations = [v for v in integrity_result.violations if v.rule_id == "R7"]
        assert len(r7_violations) > 0, "check_index_integrity 应该检测到 R7 违规"

        # promote_iteration 的 supersede 前置校验也应该报错
        with pytest.raises(SupersedeValidationError) as exc_info:
            promote_iteration(11, supersede=10)

        assert "regression" in exc_info.value.reason.lower()

    def test_supersede_validation_and_r6_r7_check_consistent(
        self, temp_project_with_iteration: Path, monkeypatch
    ):
        """测试 supersede 前置校验与 R6/R7 检查行为一致"""
        monkeypatch.setattr("promote_iteration.REPO_ROOT", temp_project_with_iteration)
        monkeypatch.setattr(
            "promote_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )
        monkeypatch.setattr(
            "promote_iteration.SSOT_DIR",
            temp_project_with_iteration / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "promote_iteration.MATRIX_FILE",
            temp_project_with_iteration / "docs" / "acceptance" / "00_acceptance_matrix.md",
        )

        # 场景: 所有条件满足时，两种检查都应该通过
        # 1. supersede 前置校验应该通过（不抛出异常）
        validate_supersede_target(10)

        # 2. 执行 supersede 后，R6/R7 检查应该通过
        promote_iteration(11, supersede=10)

        integrity_result = check_index_integrity(temp_project_with_iteration)
        r7_violations = [v for v in integrity_result.violations if v.rule_id == "R7"]
        assert len(r7_violations) == 0, f"R7 violations after supersede: {r7_violations}"

        superseded_result = check_superseded_consistency(temp_project_with_iteration)
        r6_violations = [v for v in superseded_result.violations if v.rule_id == "R6"]
        assert len(r6_violations) == 0, f"R6 violations after supersede: {r6_violations}"

    def test_supersede_validation_error_suggests_fix(
        self, temp_project_with_matrix: Path, monkeypatch
    ):
        """测试 SupersedeValidationError 包含有用的修复建议"""
        monkeypatch.setattr("promote_iteration.REPO_ROOT", temp_project_with_matrix)
        monkeypatch.setattr(
            "promote_iteration.SSOT_DIR",
            temp_project_with_matrix / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "promote_iteration.MATRIX_FILE",
            temp_project_with_matrix / "docs" / "acceptance" / "00_acceptance_matrix.md",
        )

        # 测试不在索引中的情况
        with pytest.raises(SupersedeValidationError) as exc_info:
            validate_supersede_target(99)

        # 建议应该包含如何添加到索引的提示
        assert (
            "promote_iteration.py" in exc_info.value.suggestion
            or "添加" in exc_info.value.suggestion
        )


class TestCreateIndexEntry6Columns:
    """create_index_entry 6 列表格输出测试"""

    def test_output_has_6_columns(self):
        """测试输出行有 6 列"""
        entry = create_index_entry(
            11,
            "2026-02-02",
            status="PLANNING",
            plan_link="plan",
            regression_link="regression",
            description="测试迭代",
        )

        # 用 | 分隔后应有 8 个元素（空 + 6列 + 空）
        cells = entry.split("|")
        assert len(cells) == 8, f"期望 8 个元素 (空+6列+空)，实际: {len(cells)}"

        # 验证列顺序: 迭代、日期、状态、计划、详细记录、说明
        assert "Iteration 11" in cells[1]  # 迭代
        assert "2026-02-02" in cells[2]  # 日期
        assert "PLANNING" in cells[3]  # 状态
        assert "iteration_11_plan.md" in cells[4]  # 计划
        assert "iteration_11_regression.md" in cells[5]  # 详细记录
        assert "测试迭代" in cells[6]  # 说明

    def test_column_order_matches_matrix(self):
        """测试列顺序与 00_acceptance_matrix.md 一致"""
        entry = create_index_entry(
            12,
            "2026-02-03",
            status="PARTIAL",
            plan_link="plan",
            regression_link="regression",
            description="当前活跃迭代",
        )

        # 验证格式与矩阵一致
        # | 迭代 | 日期 | 状态 | 计划 | 详细记录 | 说明 |
        assert entry.startswith("| **Iteration 12**")
        assert "| 2026-02-03 |" in entry
        assert "| ⚠️ PARTIAL |" in entry
        assert "[iteration_12_plan.md]" in entry
        assert "[iteration_12_regression.md]" in entry
        assert "| 当前活跃迭代 |" in entry
