#!/usr/bin/env python3
"""
check_no_iteration_links_in_docs.py 单元测试

覆盖功能:
1. .iteration/ 链接检测 - 验证能准确检测 Markdown 中的 .iteration/ 链接
2. SUPERSEDED 一致性校验 - 验证各种违规场景:
   - R1: 缺后继链接
   - R2: 后继不存在于索引表
   - R3: 后继排序错误
   - R4: 环形引用
   - R5: 多后继
   - R6: regression 文件缺声明

Fixtures 使用小型 Markdown 文档，避免依赖真实文件。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

# 导入被测模块
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))

from check_no_iteration_links_in_docs import (
    ITERATION_LINK_PATTERN,
    IterationIndexEntry,
    SupersededViolation,
    check_index_integrity,
    check_regression_file_superseded_header,
    check_superseded_consistency,
    parse_acceptance_matrix,
    run_check,
    scan_file_for_iteration_links,
)

# ============================================================================
# Fixtures - 小型 Markdown 文档
# ============================================================================


@pytest.fixture
def temp_project():
    """创建临时项目目录结构"""
    with tempfile.TemporaryDirectory(prefix="test_iteration_") as tmpdir:
        project = Path(tmpdir)
        (project / "docs" / "acceptance").mkdir(parents=True)
        (project / "docs" / "gateway").mkdir(parents=True)
        yield project


@pytest.fixture
def md_with_iteration_link(temp_project: Path) -> Path:
    """包含 .iteration/ 链接的 Markdown 文件"""
    content = """# 示例文档

这里有一个合规的链接 [查看详情](../acceptance/plan.md)。

但是这里有一个违规的链接 [迭代计划](../.iteration/plan.md)。

还有另一个 [笔记](.iteration/notes.md) 也是违规的。
"""
    filepath = temp_project / "docs" / "gateway" / "test.md"
    filepath.write_text(content, encoding="utf-8")
    return filepath


@pytest.fixture
def md_without_iteration_link(temp_project: Path) -> Path:
    """不包含 .iteration/ 链接的 Markdown 文件"""
    content = """# 合规文档

所有链接都是合规的：

- [查看详情](../acceptance/plan.md)
- [回归记录](./iteration_3_regression.md)
- [外部链接](https://example.com)

代码块中的链接不应被检测：

```markdown
[示例](.iteration/example.md)
```
"""
    filepath = temp_project / "docs" / "gateway" / "compliant.md"
    filepath.write_text(content, encoding="utf-8")
    return filepath


@pytest.fixture
def md_with_code_block_iteration_link(temp_project: Path) -> Path:
    """代码块中包含 .iteration/ 链接的 Markdown 文件（不应被检测）"""
    content = """# 示例文档

正常文本不包含违规链接。

```markdown
# 这是代码块中的示例
[计划](.iteration/plan.md)
```

~~~bash
echo "另一种代码块"
# [笔记](.iteration/notes.md)
~~~

代码块外的内容。
"""
    filepath = temp_project / "docs" / "gateway" / "codeblock.md"
    filepath.write_text(content, encoding="utf-8")
    return filepath


@pytest.fixture
def acceptance_matrix_valid(temp_project: Path) -> Path:
    """有效的 acceptance_matrix.md（无 SUPERSEDED 违规）"""
    content = """# 验收测试矩阵

## 迭代回归记录索引

| 迭代 | 日期 | 状态 | 计划 | 详细记录 | 说明 |
|------|------|------|------|----------|------|
| **Iteration 10** | 2026-02-01 | ⚠️ PARTIAL | - | [iteration_10_regression.md](iteration_10_regression.md) | 当前活跃迭代 |
| Iteration 9 | 2026-02-01 | 🔄 SUPERSEDED | - | [iteration_9_regression.md](iteration_9_regression.md) | 已被 Iteration 10 取代 |
| Iteration 7 | 2026-02-01 | 🔄 SUPERSEDED | - | [iteration_7_regression.md](iteration_7_regression.md) | 已被 Iteration 9 取代 |
| Iteration 5 | 2026-01-29 | ✅ PASS | - | [iteration_5_regression.md](iteration_5_regression.md) | - |

---

## 其他内容
"""
    filepath = temp_project / "docs" / "acceptance" / "00_acceptance_matrix.md"
    filepath.write_text(content, encoding="utf-8")

    # 创建 regression 文件，包含正确的 superseded 声明
    regression_9 = temp_project / "docs" / "acceptance" / "iteration_9_regression.md"
    regression_9.write_text(
        """# Iteration 9 回归记录

> **⚠️ Superseded by Iteration 10**

本文档已被取代。
""",
        encoding="utf-8",
    )

    regression_7 = temp_project / "docs" / "acceptance" / "iteration_7_regression.md"
    regression_7.write_text(
        """# Iteration 7 回归记录

> **⚠️ Superseded by Iteration 9**

本文档已被取代。
""",
        encoding="utf-8",
    )

    return filepath


@pytest.fixture
def acceptance_matrix_r1_violation(temp_project: Path) -> Path:
    """R1 违规: 缺后继链接"""
    content = """# 验收测试矩阵

## 迭代回归记录索引

| 迭代 | 日期 | 状态 | 计划 | 详细记录 | 说明 |
|------|------|------|------|----------|------|
| **Iteration 10** | 2026-02-01 | ⚠️ PARTIAL | - | - | 当前活跃 |
| Iteration 9 | 2026-02-01 | 🔄 SUPERSEDED | - | - | 已废弃（缺少后继声明） |
"""
    filepath = temp_project / "docs" / "acceptance" / "00_acceptance_matrix.md"
    filepath.write_text(content, encoding="utf-8")
    return filepath


@pytest.fixture
def acceptance_matrix_r2_violation(temp_project: Path) -> Path:
    """R2 违规: 后继不存在于索引表"""
    content = """# 验收测试矩阵

## 迭代回归记录索引

| 迭代 | 日期 | 状态 | 计划 | 详细记录 | 说明 |
|------|------|------|------|----------|------|
| **Iteration 10** | 2026-02-01 | ⚠️ PARTIAL | - | - | 当前活跃 |
| Iteration 9 | 2026-02-01 | 🔄 SUPERSEDED | - | - | 已被 Iteration 99 取代 |
"""
    filepath = temp_project / "docs" / "acceptance" / "00_acceptance_matrix.md"
    filepath.write_text(content, encoding="utf-8")
    return filepath


@pytest.fixture
def acceptance_matrix_r3_violation(temp_project: Path) -> Path:
    """R3 违规: 后继排序在下方"""
    content = """# 验收测试矩阵

## 迭代回归记录索引

| 迭代 | 日期 | 状态 | 计划 | 详细记录 | 说明 |
|------|------|------|------|----------|------|
| Iteration 7 | 2026-02-01 | 🔄 SUPERSEDED | - | - | 已被 Iteration 9 取代 |
| Iteration 9 | 2026-02-01 | ⚠️ PARTIAL | - | - | 当前活跃（但排在 7 下面） |
"""
    filepath = temp_project / "docs" / "acceptance" / "00_acceptance_matrix.md"
    filepath.write_text(content, encoding="utf-8")
    return filepath


@pytest.fixture
def acceptance_matrix_r4_violation(temp_project: Path) -> Path:
    """R4 违规: 环形引用"""
    content = """# 验收测试矩阵

## 迭代回归记录索引

| 迭代 | 日期 | 状态 | 计划 | 详细记录 | 说明 |
|------|------|------|------|----------|------|
| Iteration 10 | 2026-02-01 | 🔄 SUPERSEDED | - | - | 已被 Iteration 9 取代 |
| Iteration 9 | 2026-02-01 | 🔄 SUPERSEDED | - | - | 已被 Iteration 10 取代 |
"""
    filepath = temp_project / "docs" / "acceptance" / "00_acceptance_matrix.md"
    filepath.write_text(content, encoding="utf-8")
    return filepath


@pytest.fixture
def acceptance_matrix_r5_violation(temp_project: Path) -> Path:
    """R5 违规: 多后继"""
    content = """# 验收测试矩阵

## 迭代回归记录索引

| 迭代 | 日期 | 状态 | 计划 | 详细记录 | 说明 |
|------|------|------|------|----------|------|
| **Iteration 11** | 2026-02-01 | ⚠️ PARTIAL | - | - | 当前活跃 |
| **Iteration 10** | 2026-02-01 | ⚠️ PARTIAL | - | - | 当前活跃 |
| Iteration 9 | 2026-02-01 | 🔄 SUPERSEDED | - | - | 已被 Iteration 10 取代，已被 Iteration 11 取代 |
"""
    filepath = temp_project / "docs" / "acceptance" / "00_acceptance_matrix.md"
    filepath.write_text(content, encoding="utf-8")
    return filepath


@pytest.fixture
def acceptance_matrix_r6_violation(temp_project: Path) -> Path:
    """R6 违规: regression 文件缺 superseded 声明"""
    content = """# 验收测试矩阵

## 迭代回归记录索引

| 迭代 | 日期 | 状态 | 计划 | 详细记录 | 说明 |
|------|------|------|------|----------|------|
| **Iteration 10** | 2026-02-01 | ⚠️ PARTIAL | - | [iteration_10_regression.md](iteration_10_regression.md) | 当前活跃 |
| Iteration 9 | 2026-02-01 | 🔄 SUPERSEDED | - | [iteration_9_regression.md](iteration_9_regression.md) | 已被 Iteration 10 取代 |
"""
    filepath = temp_project / "docs" / "acceptance" / "00_acceptance_matrix.md"
    filepath.write_text(content, encoding="utf-8")

    # 创建缺少 superseded 声明的 regression 文件
    regression_9 = temp_project / "docs" / "acceptance" / "iteration_9_regression.md"
    regression_9.write_text(
        """# Iteration 9 回归记录

这是一个普通文档，缺少 superseded 声明。
""",
        encoding="utf-8",
    )

    return filepath


@pytest.fixture
def acceptance_matrix_r6_mismatch(temp_project: Path) -> Path:
    """R6 违规: regression 文件 superseded 声明的后继编号不一致"""
    content = """# 验收测试矩阵

## 迭代回归记录索引

| 迭代 | 日期 | 状态 | 计划 | 详细记录 | 说明 |
|------|------|------|------|----------|------|
| **Iteration 10** | 2026-02-01 | ⚠️ PARTIAL | - | [iteration_10_regression.md](iteration_10_regression.md) | 当前活跃 |
| Iteration 9 | 2026-02-01 | 🔄 SUPERSEDED | - | [iteration_9_regression.md](iteration_9_regression.md) | 已被 Iteration 10 取代 |
"""
    filepath = temp_project / "docs" / "acceptance" / "00_acceptance_matrix.md"
    filepath.write_text(content, encoding="utf-8")

    # 创建 superseded 声明后继编号不一致的 regression 文件
    regression_9 = temp_project / "docs" / "acceptance" / "iteration_9_regression.md"
    regression_9.write_text(
        """# Iteration 9 回归记录

> **⚠️ Superseded by Iteration 11**

后继编号与索引表不一致（索引表声明为 10，这里写的是 11）。
""",
        encoding="utf-8",
    )

    return filepath


# ============================================================================
# .iteration/ 链接检测测试
# ============================================================================


class TestIterationLinkPattern:
    """ITERATION_LINK_PATTERN 正则表达式测试"""

    def test_matches_simple_iteration_link(self):
        """测试匹配简单的 .iteration/ 链接"""
        line = "[计划](.iteration/plan.md)"
        matches = ITERATION_LINK_PATTERN.findall(line)
        assert len(matches) == 1
        assert ".iteration/plan.md" in matches[0]

    def test_matches_parent_directory_link(self):
        """测试匹配 ../.iteration/ 链接"""
        line = "[详情](../.iteration/notes.md)"
        matches = ITERATION_LINK_PATTERN.findall(line)
        assert len(matches) == 1
        assert "../.iteration/notes.md" in matches[0]

    def test_matches_deep_nested_link(self):
        """测试匹配多层嵌套的 .iteration/ 链接"""
        line = "[文档](../../.iteration/deep/path/file.md)"
        matches = ITERATION_LINK_PATTERN.findall(line)
        assert len(matches) == 1
        assert "../../.iteration/deep/path/file.md" in matches[0]

    def test_matches_path_with_iteration_in_middle(self):
        """测试匹配路径中间包含 .iteration/ 的链接"""
        line = "[文档](some/path/.iteration/file.md)"
        matches = ITERATION_LINK_PATTERN.findall(line)
        assert len(matches) == 1

    def test_no_match_for_regular_link(self):
        """测试不匹配普通链接"""
        line = "[文档](../acceptance/plan.md)"
        matches = ITERATION_LINK_PATTERN.findall(line)
        assert len(matches) == 0

    def test_no_match_for_iteration_text_without_dot(self):
        """测试不匹配不带点号的 iteration 目录"""
        line = "[文档](iteration/plan.md)"
        matches = ITERATION_LINK_PATTERN.findall(line)
        assert len(matches) == 0

    def test_multiple_links_in_line(self):
        """测试一行中的多个链接"""
        line = "[A](.iteration/a.md) 和 [B](../.iteration/b.md)"
        matches = ITERATION_LINK_PATTERN.findall(line)
        assert len(matches) == 2


class TestScanFileForIterationLinks:
    """scan_file_for_iteration_links 函数测试"""

    def test_detects_iteration_links(self, md_with_iteration_link: Path):
        """测试检测 .iteration/ 链接"""
        violations = list(scan_file_for_iteration_links(md_with_iteration_link))

        assert len(violations) == 2

        # 验证违规记录
        links = [v.matched_link for v in violations]
        assert any(".iteration/plan.md" in link for link in links)
        assert any(".iteration/notes.md" in link for link in links)

    def test_no_violations_for_compliant_file(self, md_without_iteration_link: Path):
        """测试合规文件无违规"""
        violations = list(scan_file_for_iteration_links(md_without_iteration_link))
        assert len(violations) == 0

    def test_skips_code_blocks(self, md_with_code_block_iteration_link: Path):
        """测试跳过代码块中的链接"""
        violations = list(scan_file_for_iteration_links(md_with_code_block_iteration_link))
        assert len(violations) == 0

    def test_violation_includes_line_number(self, md_with_iteration_link: Path):
        """测试违规记录包含行号"""
        violations = list(scan_file_for_iteration_links(md_with_iteration_link))

        for v in violations:
            assert v.line_number > 0
            assert v.file == md_with_iteration_link


class TestRunCheck:
    """run_check 函数测试"""

    def test_run_check_detects_violations(self, temp_project: Path, md_with_iteration_link: Path):
        """测试 run_check 检测违规"""
        violations, total_files = run_check(
            paths=["docs/"],
            project_root=temp_project,
        )

        assert len(violations) == 2
        assert total_files >= 1

    def test_run_check_returns_zero_for_compliant(
        self, temp_project: Path, md_without_iteration_link: Path
    ):
        """测试 run_check 对合规项目返回空列表"""
        violations, total_files = run_check(
            paths=["docs/"],
            project_root=temp_project,
        )

        assert len(violations) == 0
        assert total_files >= 1


# ============================================================================
# SUPERSEDED 解析测试
# ============================================================================


class TestParseAcceptanceMatrix:
    """parse_acceptance_matrix 函数测试"""

    def test_parses_iteration_entries(self, acceptance_matrix_valid: Path):
        """测试解析迭代条目"""
        entries = parse_acceptance_matrix(acceptance_matrix_valid)

        assert len(entries) == 4

        # 验证迭代编号
        iter_nums = [e.iteration_number for e in entries]
        assert 10 in iter_nums
        assert 9 in iter_nums
        assert 7 in iter_nums
        assert 5 in iter_nums

    def test_parses_superseded_status(self, acceptance_matrix_valid: Path):
        """测试解析 SUPERSEDED 状态"""
        entries = parse_acceptance_matrix(acceptance_matrix_valid)

        # Iteration 9 和 7 应该是 SUPERSEDED
        superseded = [e for e in entries if e.is_superseded]
        assert len(superseded) == 2

        iter_9 = next(e for e in entries if e.iteration_number == 9)
        assert iter_9.is_superseded
        assert iter_9.get_successor_number() == 10

        iter_7 = next(e for e in entries if e.iteration_number == 7)
        assert iter_7.is_superseded
        assert iter_7.get_successor_number() == 9

    def test_parses_row_index(self, acceptance_matrix_valid: Path):
        """测试解析行索引"""
        entries = parse_acceptance_matrix(acceptance_matrix_valid)

        # 验证行索引顺序
        iter_10 = next(e for e in entries if e.iteration_number == 10)
        iter_9 = next(e for e in entries if e.iteration_number == 9)

        assert iter_10.row_index < iter_9.row_index  # 10 应该在 9 上方

    def test_parses_regression_link(self, acceptance_matrix_valid: Path):
        """测试解析 regression 链接"""
        entries = parse_acceptance_matrix(acceptance_matrix_valid)

        iter_9 = next(e for e in entries if e.iteration_number == 9)
        assert iter_9.regression_link == "iteration_9_regression.md"

    def test_returns_empty_for_missing_file(self, temp_project: Path):
        """测试文件不存在时返回空列表"""
        non_existent = temp_project / "docs" / "acceptance" / "non_existent.md"
        entries = parse_acceptance_matrix(non_existent)
        assert entries == []


class TestIterationIndexEntry:
    """IterationIndexEntry 数据类测试"""

    def test_get_successor_number_chinese(self):
        """测试提取中文格式的后继编号"""
        entry = IterationIndexEntry(
            iteration_number=9,
            date="2026-02-01",
            status="🔄 SUPERSEDED",
            plan_link=None,
            regression_link=None,
            description="已被 Iteration 10 取代",
            row_index=1,
        )
        assert entry.get_successor_number() == 10

    def test_get_successor_number_english(self):
        """测试提取英文格式的后继编号"""
        entry = IterationIndexEntry(
            iteration_number=9,
            date="2026-02-01",
            status="SUPERSEDED",
            plan_link=None,
            regression_link=None,
            description="Superseded by Iteration 10",
            row_index=1,
        )
        assert entry.get_successor_number() == 10

    def test_get_successor_number_returns_none_if_missing(self):
        """测试缺少后继声明时返回 None"""
        entry = IterationIndexEntry(
            iteration_number=9,
            date="2026-02-01",
            status="SUPERSEDED",
            plan_link=None,
            regression_link=None,
            description="已废弃",
            row_index=1,
        )
        assert entry.get_successor_number() is None


# ============================================================================
# SUPERSEDED 一致性校验测试
# ============================================================================


class TestCheckSupersededConsistency:
    """check_superseded_consistency 函数测试"""

    def test_valid_superseded_no_violations(
        self, temp_project: Path, acceptance_matrix_valid: Path
    ):
        """测试有效的 SUPERSEDED 配置无违规"""
        result = check_superseded_consistency(temp_project)

        assert len(result.violations) == 0
        assert result.superseded_count == 2  # Iteration 9 和 7

    def test_r1_missing_successor_link(
        self, temp_project: Path, acceptance_matrix_r1_violation: Path
    ):
        """测试 R1 违规: 缺后继链接"""
        result = check_superseded_consistency(temp_project)

        r1_violations = [v for v in result.violations if v.rule_id == "R1"]
        assert len(r1_violations) == 1
        assert r1_violations[0].iteration_number == 9
        assert "后继声明" in r1_violations[0].message or "后继" in r1_violations[0].message

    def test_r2_successor_not_in_index(
        self, temp_project: Path, acceptance_matrix_r2_violation: Path
    ):
        """测试 R2 违规: 后继不存在于索引表"""
        result = check_superseded_consistency(temp_project)

        r2_violations = [v for v in result.violations if v.rule_id == "R2"]
        assert len(r2_violations) == 1
        assert r2_violations[0].iteration_number == 9
        assert "99" in r2_violations[0].message  # 后继 99 不存在

    def test_r3_successor_below(self, temp_project: Path, acceptance_matrix_r3_violation: Path):
        """测试 R3 违规: 后继排序在下方"""
        result = check_superseded_consistency(temp_project)

        r3_violations = [v for v in result.violations if v.rule_id == "R3"]
        assert len(r3_violations) == 1
        assert r3_violations[0].iteration_number == 7
        assert "上方" in r3_violations[0].message

    def test_r4_cycle_detection(self, temp_project: Path, acceptance_matrix_r4_violation: Path):
        """测试 R4 违规: 环形引用"""
        result = check_superseded_consistency(temp_project)

        r4_violations = [v for v in result.violations if v.rule_id == "R4"]
        assert len(r4_violations) >= 1  # 至少检测到一个环
        assert any("环形" in v.message or "→" in v.message for v in r4_violations)

    def test_r5_multiple_successors(self, temp_project: Path, acceptance_matrix_r5_violation: Path):
        """测试 R5 违规: 多后继"""
        result = check_superseded_consistency(temp_project)

        r5_violations = [v for v in result.violations if v.rule_id == "R5"]
        assert len(r5_violations) == 1
        assert r5_violations[0].iteration_number == 9
        assert "多个后继" in r5_violations[0].message

    def test_r6_missing_regression_header(
        self, temp_project: Path, acceptance_matrix_r6_violation: Path
    ):
        """测试 R6 违规: regression 文件缺 superseded 声明"""
        result = check_superseded_consistency(temp_project)

        r6_violations = [v for v in result.violations if v.rule_id == "R6"]
        assert len(r6_violations) == 1
        assert r6_violations[0].iteration_number == 9
        assert "superseded 声明" in r6_violations[0].message.lower()

    def test_r6_successor_mismatch(self, temp_project: Path, acceptance_matrix_r6_mismatch: Path):
        """测试 R6 违规: regression 文件 superseded 声明后继编号不一致"""
        result = check_superseded_consistency(temp_project)

        r6_violations = [v for v in result.violations if v.rule_id == "R6"]
        assert len(r6_violations) == 1
        assert r6_violations[0].iteration_number == 9
        assert "不一致" in r6_violations[0].message


class TestCheckRegressionFileSupersededHeader:
    """check_regression_file_superseded_header 函数测试"""

    def test_valid_header(self, temp_project: Path):
        """测试有效的 superseded 声明"""
        filepath = temp_project / "test.md"
        filepath.write_text(
            """# Test

> **⚠️ Superseded by Iteration 10**

Content here.
""",
            encoding="utf-8",
        )

        violation = check_regression_file_superseded_header(filepath, expected_successor=10)
        assert violation is None

    def test_missing_header(self, temp_project: Path):
        """测试缺少 superseded 声明"""
        filepath = temp_project / "test.md"
        filepath.write_text(
            """# Test

No superseded header here.
""",
            encoding="utf-8",
        )

        violation = check_regression_file_superseded_header(filepath, expected_successor=10)
        assert violation is not None
        assert violation.rule_id == "R6"
        assert "superseded 声明" in violation.message.lower()

    def test_mismatched_successor(self, temp_project: Path):
        """测试后继编号不一致"""
        filepath = temp_project / "test.md"
        filepath.write_text(
            """# Test

> **⚠️ Superseded by Iteration 11**

Content here.
""",
            encoding="utf-8",
        )

        violation = check_regression_file_superseded_header(filepath, expected_successor=10)
        assert violation is not None
        assert violation.rule_id == "R6"
        assert "不一致" in violation.message

    def test_file_not_exists(self, temp_project: Path):
        """测试文件不存在"""
        filepath = temp_project / "non_existent.md"

        violation = check_regression_file_superseded_header(filepath, expected_successor=10)
        assert violation is not None
        assert violation.rule_id == "R6"
        assert "不存在" in violation.message


# ============================================================================
# SupersededViolation 数据类测试
# ============================================================================


class TestSupersededViolation:
    """SupersededViolation 数据类测试"""

    def test_str_format_with_file(self):
        """测试带文件路径的字符串格式"""
        violation = SupersededViolation(
            rule_id="R1",
            iteration_number=9,
            message="缺少后继声明",
            file=Path("docs/acceptance/00_acceptance_matrix.md"),
            line_number=10,
        )

        str_repr = str(violation)
        assert "[R1]" in str_repr
        assert "Iteration 9" in str_repr
        assert "00_acceptance_matrix.md" in str_repr

    def test_str_format_without_file(self):
        """测试不带文件路径的字符串格式"""
        violation = SupersededViolation(
            rule_id="R4",
            iteration_number=9,
            message="存在环形引用: 9 → 10 → 9",
        )

        str_repr = str(violation)
        assert "[R4]" in str_repr
        assert "Iteration 9" in str_repr
        assert "环形引用" in str_repr


# ============================================================================
# 集成测试
# ============================================================================


class TestIntegration:
    """集成测试: 同时验证 iteration 链接和 SUPERSEDED 一致性"""

    def test_combined_violations(self, temp_project: Path):
        """测试同时存在 iteration 链接和 SUPERSEDED 违规"""
        # 创建包含 iteration 链接的文档
        doc = temp_project / "docs" / "gateway" / "test.md"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text(
            """# 测试文档

[计划](.iteration/plan.md)
""",
            encoding="utf-8",
        )

        # 创建有 R1 违规的 acceptance matrix
        matrix = temp_project / "docs" / "acceptance" / "00_acceptance_matrix.md"
        matrix.parent.mkdir(parents=True, exist_ok=True)
        matrix.write_text(
            """# 验收测试矩阵

## 迭代回归记录索引

| 迭代 | 日期 | 状态 | 计划 | 详细记录 | 说明 |
|------|------|------|------|----------|------|
| Iteration 9 | 2026-02-01 | 🔄 SUPERSEDED | - | - | 已废弃 |
""",
            encoding="utf-8",
        )

        # 检查 iteration 链接
        link_violations, _ = run_check(paths=["docs/"], project_root=temp_project)
        assert len(link_violations) == 1

        # 检查 SUPERSEDED 一致性
        superseded_result = check_superseded_consistency(temp_project)
        assert len(superseded_result.violations) >= 1

        # 验证 R1 违规
        r1_violations = [v for v in superseded_result.violations if v.rule_id == "R1"]
        assert len(r1_violations) == 1


# ============================================================================
# 索引完整性检查测试 (R7, R8, R9)
# ============================================================================


@pytest.fixture
def acceptance_matrix_with_missing_file(temp_project: Path) -> Path:
    """R7 违规: 索引表中链接的文件不存在"""
    content = """# 验收测试矩阵

## 迭代回归记录索引

| 迭代 | 日期 | 状态 | 计划 | 详细记录 | 说明 |
|------|------|------|------|----------|------|
| **Iteration 10** | 2026-02-01 | ⚠️ PARTIAL | [iteration_10_plan.md](iteration_10_plan.md) | [iteration_10_regression.md](iteration_10_regression.md) | 当前活跃 |
"""
    filepath = temp_project / "docs" / "acceptance" / "00_acceptance_matrix.md"
    filepath.write_text(content, encoding="utf-8")
    # 注意：不创建 iteration_10_plan.md 和 iteration_10_regression.md 文件
    return filepath


@pytest.fixture
def acceptance_matrix_with_orphan_file(temp_project: Path) -> Path:
    """R8 违规: 存在未被索引的 iteration 文件"""
    content = """# 验收测试矩阵

## 迭代回归记录索引

| 迭代 | 日期 | 状态 | 计划 | 详细记录 | 说明 |
|------|------|------|------|----------|------|
| **Iteration 10** | 2026-02-01 | ⚠️ PARTIAL | - | [iteration_10_regression.md](iteration_10_regression.md) | 当前活跃 |
"""
    filepath = temp_project / "docs" / "acceptance" / "00_acceptance_matrix.md"
    filepath.write_text(content, encoding="utf-8")

    # 创建索引中存在的文件
    regression_10 = temp_project / "docs" / "acceptance" / "iteration_10_regression.md"
    regression_10.write_text("# Iteration 10\n", encoding="utf-8")

    # 创建孤儿文件（迭代 9 不在索引中）
    regression_9 = temp_project / "docs" / "acceptance" / "iteration_9_regression.md"
    regression_9.write_text("# Iteration 9\n", encoding="utf-8")

    return filepath


@pytest.fixture
def acceptance_matrix_wrong_order(temp_project: Path) -> Path:
    """R9 违规: 索引表未按降序排列"""
    content = """# 验收测试矩阵

## 迭代回归记录索引

| 迭代 | 日期 | 状态 | 计划 | 详细记录 | 说明 |
|------|------|------|------|----------|------|
| **Iteration 8** | 2026-02-01 | ⚠️ PARTIAL | - | [iteration_8_regression.md](iteration_8_regression.md) | 较旧迭代 |
| **Iteration 10** | 2026-02-01 | ⚠️ PARTIAL | - | [iteration_10_regression.md](iteration_10_regression.md) | 较新迭代（排序错误） |
| **Iteration 9** | 2026-02-01 | ⚠️ PARTIAL | - | [iteration_9_regression.md](iteration_9_regression.md) | 中间迭代 |
"""
    filepath = temp_project / "docs" / "acceptance" / "00_acceptance_matrix.md"
    filepath.write_text(content, encoding="utf-8")

    # 创建所有文件
    for n in [8, 9, 10]:
        f = temp_project / "docs" / "acceptance" / f"iteration_{n}_regression.md"
        f.write_text(f"# Iteration {n}\n", encoding="utf-8")

    return filepath


@pytest.fixture
def acceptance_matrix_valid_integrity(temp_project: Path) -> Path:
    """有效的索引表（无完整性违规）"""
    content = """# 验收测试矩阵

## 迭代回归记录索引

| 迭代 | 日期 | 状态 | 计划 | 详细记录 | 说明 |
|------|------|------|------|----------|------|
| **Iteration 10** | 2026-02-01 | ⚠️ PARTIAL | [iteration_10_plan.md](iteration_10_plan.md) | [iteration_10_regression.md](iteration_10_regression.md) | 当前活跃 |
| Iteration 9 | 2026-02-01 | ✅ PASS | - | [iteration_9_regression.md](iteration_9_regression.md) | 已完成 |
| Iteration 8 | 2026-02-01 | ✅ PASS | - | [iteration_8_regression.md](iteration_8_regression.md) | 已完成 |
"""
    filepath = temp_project / "docs" / "acceptance" / "00_acceptance_matrix.md"
    filepath.write_text(content, encoding="utf-8")

    # 创建所有引用的文件
    plan_10 = temp_project / "docs" / "acceptance" / "iteration_10_plan.md"
    plan_10.write_text("# Iteration 10 Plan\n", encoding="utf-8")

    for n in [8, 9, 10]:
        f = temp_project / "docs" / "acceptance" / f"iteration_{n}_regression.md"
        f.write_text(f"# Iteration {n}\n", encoding="utf-8")

    return filepath


class TestCheckIndexIntegrity:
    """check_index_integrity 函数测试"""

    def test_valid_index_no_violations(
        self, temp_project: Path, acceptance_matrix_valid_integrity: Path
    ):
        """测试有效的索引配置无违规"""
        result = check_index_integrity(temp_project)

        assert len(result.violations) == 0
        assert len(result.missing_files) == 0
        assert len(result.orphan_files) == 0
        assert len(result.order_violations) == 0

    def test_r7_missing_file(self, temp_project: Path, acceptance_matrix_with_missing_file: Path):
        """测试 R7 违规: 链接文件不存在"""
        result = check_index_integrity(temp_project)

        r7_violations = [v for v in result.violations if v.rule_id == "R7"]
        assert len(r7_violations) == 2  # plan 和 regression 都不存在
        assert len(result.missing_files) == 2

        # 验证错误消息
        messages = [v.message for v in r7_violations]
        assert any("plan_link" in msg for msg in messages)
        assert any("regression_link" in msg for msg in messages)

    def test_r8_orphan_file(self, temp_project: Path, acceptance_matrix_with_orphan_file: Path):
        """测试 R8 违规: 文件未被索引"""
        result = check_index_integrity(temp_project)

        r8_violations = [v for v in result.violations if v.rule_id == "R8"]
        assert len(r8_violations) == 1
        assert r8_violations[0].iteration_number == 9
        assert len(result.orphan_files) == 1
        assert "iteration_9_regression.md" in result.orphan_files[0]

    def test_r9_wrong_order(self, temp_project: Path, acceptance_matrix_wrong_order: Path):
        """测试 R9 违规: 索引表未按降序排列"""
        result = check_index_integrity(temp_project)

        r9_violations = [v for v in result.violations if v.rule_id == "R9"]
        assert len(r9_violations) >= 1
        assert len(result.order_violations) >= 1

        # 验证检测到的排序问题
        # 索引顺序是 8, 10, 9 -> 10 应在 8 之前，9 应在 10 之前
        assert any("降序" in v.message or "修复建议" in v.message for v in r9_violations)

    def test_r9_detects_out_of_order(self, temp_project: Path):
        """测试 R9 能检测单个升序错误"""
        content = """# 验收测试矩阵

## 迭代回归记录索引

| 迭代 | 日期 | 状态 | 计划 | 详细记录 | 说明 |
|------|------|------|------|----------|------|
| **Iteration 5** | 2026-02-01 | ⚠️ PARTIAL | - | - | 旧迭代 |
| **Iteration 10** | 2026-02-01 | ⚠️ PARTIAL | - | - | 新迭代（应在前面） |
"""
        matrix = temp_project / "docs" / "acceptance" / "00_acceptance_matrix.md"
        matrix.parent.mkdir(parents=True, exist_ok=True)
        matrix.write_text(content, encoding="utf-8")

        result = check_index_integrity(temp_project)

        r9_violations = [v for v in result.violations if v.rule_id == "R9"]
        assert len(r9_violations) == 1
        assert r9_violations[0].iteration_number == 10
        assert (5, 10) in result.order_violations

    def test_missing_matrix_file(self, temp_project: Path):
        """测试索引文件不存在时返回空结果"""
        # 不创建 00_acceptance_matrix.md
        result = check_index_integrity(temp_project)

        assert len(result.violations) == 0
        assert len(result.missing_files) == 0
        assert len(result.orphan_files) == 0

    def test_plan_orphan_detected(self, temp_project: Path):
        """测试 plan 孤儿文件也被检测到"""
        content = """# 验收测试矩阵

## 迭代回归记录索引

| 迭代 | 日期 | 状态 | 计划 | 详细记录 | 说明 |
|------|------|------|------|----------|------|
| **Iteration 10** | 2026-02-01 | ⚠️ PARTIAL | - | [iteration_10_regression.md](iteration_10_regression.md) | 当前活跃 |
"""
        matrix = temp_project / "docs" / "acceptance" / "00_acceptance_matrix.md"
        matrix.parent.mkdir(parents=True, exist_ok=True)
        matrix.write_text(content, encoding="utf-8")

        # 创建索引中的文件
        regression_10 = temp_project / "docs" / "acceptance" / "iteration_10_regression.md"
        regression_10.write_text("# Iteration 10\n", encoding="utf-8")

        # 创建孤儿 plan 文件（迭代 9 不在索引中）
        plan_9 = temp_project / "docs" / "acceptance" / "iteration_9_plan.md"
        plan_9.write_text("# Iteration 9 Plan\n", encoding="utf-8")

        result = check_index_integrity(temp_project)

        r8_violations = [v for v in result.violations if v.rule_id == "R8"]
        assert len(r8_violations) == 1
        assert r8_violations[0].iteration_number == 9


class TestIntegrityIntegration:
    """完整性检查集成测试"""

    def test_multiple_integrity_violations(self, temp_project: Path):
        """测试同时存在多种完整性违规"""
        content = """# 验收测试矩阵

## 迭代回归记录索引

| 迭代 | 日期 | 状态 | 计划 | 详细记录 | 说明 |
|------|------|------|------|----------|------|
| **Iteration 5** | 2026-02-01 | ⚠️ PARTIAL | [iteration_5_plan.md](iteration_5_plan.md) | [iteration_5_regression.md](iteration_5_regression.md) | 旧迭代 |
| **Iteration 10** | 2026-02-01 | ⚠️ PARTIAL | - | [iteration_10_regression.md](iteration_10_regression.md) | 新迭代（排序错误） |
"""
        matrix = temp_project / "docs" / "acceptance" / "00_acceptance_matrix.md"
        matrix.parent.mkdir(parents=True, exist_ok=True)
        matrix.write_text(content, encoding="utf-8")

        # 创建部分文件
        regression_10 = temp_project / "docs" / "acceptance" / "iteration_10_regression.md"
        regression_10.write_text("# Iteration 10\n", encoding="utf-8")

        # 创建孤儿文件
        regression_7 = temp_project / "docs" / "acceptance" / "iteration_7_regression.md"
        regression_7.write_text("# Iteration 7\n", encoding="utf-8")

        # 注意：iteration_5_plan.md 和 iteration_5_regression.md 不存在（R7）
        # iteration_7_regression.md 是孤儿（R8）
        # 10 在 5 后面（R9）

        result = check_index_integrity(temp_project)

        # 应该检测到 R7 (缺失文件)
        r7_violations = [v for v in result.violations if v.rule_id == "R7"]
        assert len(r7_violations) == 2  # plan 和 regression 都缺失

        # 应该检测到 R8 (孤儿文件)
        r8_violations = [v for v in result.violations if v.rule_id == "R8"]
        assert len(r8_violations) == 1

        # 应该检测到 R9 (排序错误)
        r9_violations = [v for v in result.violations if v.rule_id == "R9"]
        assert len(r9_violations) == 1
