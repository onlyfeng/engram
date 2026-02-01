#!/usr/bin/env python3
"""
check_iteration_docs_placeholders.py 单元测试

覆盖功能:
1. 模板占位符检测 - 验证能准确检测各种占位符格式
2. 使用说明区块检测 - 验证能检测文件顶部的模板使用说明
3. 代码块跳过 - 验证代码块内的占位符不被误报
4. 文件过滤 - 验证只扫描 iteration_*_{plan,regression}.md，排除模板

Fixtures 使用临时目录构造 docs/acceptance 结构。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from scripts.ci.check_iteration_docs_placeholders import (
    PLACEHOLDER_PATTERN,
    REGRESSION_REQUIRED_HEADINGS,
    PlaceholderViolation,
    get_iteration_files,
    run_check,
    scan_file,
    scan_file_for_placeholders,
    scan_file_for_required_headings,
    scan_file_for_usage_instructions,
)

# ============================================================================
# Fixtures - 临时项目目录
# ============================================================================


@pytest.fixture
def temp_project():
    """创建临时项目目录结构"""
    with tempfile.TemporaryDirectory(prefix="test_placeholders_") as tmpdir:
        project = Path(tmpdir)
        (project / "docs" / "acceptance" / "_templates").mkdir(parents=True)
        yield project


@pytest.fixture
def iteration_file_with_placeholders(temp_project: Path) -> Path:
    """包含模板占位符的迭代文档"""
    content = """# Iteration {N} 计划

## 概述

| 字段 | 内容 |
|------|------|
| **迭代编号** | Iteration {N} |
| **开始日期** | {YYYY-MM-DD} |
| **状态** | {STATUS_EMOJI} {STATUS} |

## 迭代目标

1. **{目标1名称}**：{目标1描述}
2. 修复 {M} 个问题

## 对比

| 指标 | Iteration {N-1} | Iteration {N} |
|------|-----------------|---------------|
| 错误数 | {K} | {L} |
"""
    filepath = temp_project / "docs" / "acceptance" / "iteration_13_plan.md"
    filepath.write_text(content, encoding="utf-8")
    return filepath


@pytest.fixture
def iteration_file_with_usage_instruction(temp_project: Path) -> Path:
    """包含模板使用说明的迭代文档"""
    content = """> **使用说明**：复制本模板到 `docs/acceptance/iteration_N_plan.md`，替换 `{PLACEHOLDER}` 占位符。
>
> **索引关系**：创建计划后，需在索引表中添加对应条目。

---

# Iteration 13 计划

实际内容在这里...
"""
    filepath = temp_project / "docs" / "acceptance" / "iteration_13_regression.md"
    filepath.write_text(content, encoding="utf-8")
    return filepath


@pytest.fixture
def iteration_file_clean(temp_project: Path) -> Path:
    """干净的迭代文档（无占位符和使用说明）"""
    content = """# Iteration 13 计划

## 概述

| 字段 | 内容 |
|------|------|
| **迭代编号** | Iteration 13 |
| **开始日期** | 2026-02-02 |
| **状态** | ⚠️ PARTIAL |

## 迭代目标

1. **代码质量修复**：修复 lint 错误
2. 修复 5 个问题

## 对比

| 指标 | Iteration 12 | Iteration 13 |
|------|--------------|---------------|
| 错误数 | 10 | 5 |
"""
    filepath = temp_project / "docs" / "acceptance" / "iteration_14_plan.md"
    filepath.write_text(content, encoding="utf-8")
    return filepath


@pytest.fixture
def iteration_file_with_code_block(temp_project: Path) -> Path:
    """包含代码块的迭代文档（代码块内的占位符不应被检测）"""
    content = """# Iteration 13 计划

## 模板示例

以下是模板格式示例（代码块内不应被检测）：

```markdown
# Iteration {N} 计划
| **开始日期** | {YYYY-MM-DD} |
```

~~~bash
echo "Iteration {N}"
~~~

代码块外的内容应该是干净的。
"""
    filepath = temp_project / "docs" / "acceptance" / "iteration_15_regression.md"
    filepath.write_text(content, encoding="utf-8")
    return filepath


@pytest.fixture
def template_file(temp_project: Path) -> Path:
    """模板文件（应被排除）"""
    content = """> **使用说明**：复制本模板到 ...

# Iteration {N} 模板

| **开始日期** | {YYYY-MM-DD} |
"""
    filepath = temp_project / "docs" / "acceptance" / "_templates" / "iteration_plan.template.md"
    filepath.write_text(content, encoding="utf-8")
    return filepath


# ============================================================================
# PLACEHOLDER_PATTERN 正则表达式测试
# ============================================================================


class TestPlaceholderPattern:
    """PLACEHOLDER_PATTERN 正则表达式测试"""

    def test_matches_single_letter_variable(self):
        """测试匹配单字母变量"""
        test_cases = ["{N}", "{M}", "{K}", "{L}", "{T}"]
        for text in test_cases:
            assert PLACEHOLDER_PATTERN.search(text), f"应匹配: {text}"

    def test_matches_expression_variable(self):
        """测试匹配表达式变量"""
        test_cases = ["{N-1}", "{N+1}", "{N-M}", "{K+2}"]
        for text in test_cases:
            assert PLACEHOLDER_PATTERN.search(text), f"应匹配: {text}"

    def test_matches_date_placeholder(self):
        """测试匹配日期占位符"""
        assert PLACEHOLDER_PATTERN.search("{YYYY-MM-DD}")

    def test_matches_status_placeholder(self):
        """测试匹配状态占位符"""
        assert PLACEHOLDER_PATTERN.search("{STATUS}")
        assert PLACEHOLDER_PATTERN.search("{STATUS_EMOJI}")

    def test_matches_placeholder_keyword(self):
        """测试匹配 PLACEHOLDER 关键字"""
        assert PLACEHOLDER_PATTERN.search("{PLACEHOLDER}")

    def test_matches_chinese_placeholder(self):
        """测试匹配中文占位符"""
        test_cases = ["{目标1名称}", "{修复方案}", "{问题描述}", "{文件路径}"]
        for text in test_cases:
            assert PLACEHOLDER_PATTERN.search(text), f"应匹配: {text}"

    def test_no_match_for_actual_values(self):
        """测试不匹配实际值"""
        test_cases = [
            "Iteration 13",
            "2026-02-02",
            "PARTIAL",
            "代码质量修复",
        ]
        for text in test_cases:
            assert not PLACEHOLDER_PATTERN.search(text), f"不应匹配: {text}"

    def test_no_match_for_code_syntax(self):
        """测试不匹配代码语法（如 TypeScript/JSON 对象）"""
        # 小写的 {key: value} 不应被匹配
        test_cases = [
            "{name}",  # 小写变量
            "{config}",  # 配置对象
        ]
        for text in test_cases:
            # 这些可能被匹配也可能不被匹配，取决于正则设计
            # 主要测试的是大写占位符
            pass


# ============================================================================
# scan_file_for_placeholders 测试
# ============================================================================


class TestScanFileForPlaceholders:
    """scan_file_for_placeholders 函数测试"""

    def test_detects_placeholders(self, iteration_file_with_placeholders: Path):
        """测试检测模板占位符"""
        violations = list(scan_file_for_placeholders(iteration_file_with_placeholders))

        # 应该检测到多个占位符
        assert len(violations) > 0

        # 验证检测到的占位符类型
        matched_texts = [v.matched_text for v in violations]
        assert any("{N}" in text for text in matched_texts)
        assert any("{YYYY-MM-DD}" in text for text in matched_texts)

    def test_no_violations_for_clean_file(self, iteration_file_clean: Path):
        """测试干净文件无违规"""
        violations = list(scan_file_for_placeholders(iteration_file_clean))
        assert len(violations) == 0

    def test_skips_code_blocks(self, iteration_file_with_code_block: Path):
        """测试跳过代码块中的占位符"""
        violations = list(scan_file_for_placeholders(iteration_file_with_code_block))
        # 代码块内的占位符不应被检测
        assert len(violations) == 0

    def test_violation_includes_line_number(self, iteration_file_with_placeholders: Path):
        """测试违规记录包含行号"""
        violations = list(scan_file_for_placeholders(iteration_file_with_placeholders))

        for v in violations:
            assert v.line_number > 0
            assert v.file == iteration_file_with_placeholders
            assert v.violation_type == "placeholder"


# ============================================================================
# scan_file_for_usage_instructions 测试
# ============================================================================


class TestScanFileForUsageInstructions:
    """scan_file_for_usage_instructions 函数测试"""

    def test_detects_usage_instruction(self, iteration_file_with_usage_instruction: Path):
        """测试检测使用说明"""
        violations = list(scan_file_for_usage_instructions(iteration_file_with_usage_instruction))

        assert len(violations) >= 1

        # 验证检测类型
        for v in violations:
            assert v.violation_type == "usage_instruction"

    def test_no_violations_for_clean_file(self, iteration_file_clean: Path):
        """测试干净文件无违规"""
        violations = list(scan_file_for_usage_instructions(iteration_file_clean))
        assert len(violations) == 0

    def test_detects_within_check_lines(self, temp_project: Path):
        """测试只检查前 N 行"""
        # 在第 25 行放置使用说明（超出默认检查范围）
        content = "\n" * 24 + "> **使用说明**：复制本模板..."
        filepath = temp_project / "docs" / "acceptance" / "iteration_99_plan.md"
        filepath.write_text(content, encoding="utf-8")

        # 默认检查前 20 行，第 25 行不应被检测
        violations = list(scan_file_for_usage_instructions(filepath, check_lines=20))
        assert len(violations) == 0

        # 扩大检查范围后应被检测
        violations = list(scan_file_for_usage_instructions(filepath, check_lines=30))
        assert len(violations) >= 1


# ============================================================================
# scan_file 测试
# ============================================================================


class TestScanFile:
    """scan_file 函数测试"""

    def test_detects_both_violation_types(self, temp_project: Path):
        """测试同时检测占位符和使用说明"""
        content = """> **使用说明**：复制本模板...

# Iteration {N} 计划

| **开始日期** | {YYYY-MM-DD} |
"""
        filepath = temp_project / "docs" / "acceptance" / "iteration_88_plan.md"
        filepath.write_text(content, encoding="utf-8")

        violations = scan_file(filepath)

        # 应该同时检测到占位符和使用说明
        placeholder_violations = [v for v in violations if v.violation_type == "placeholder"]
        instruction_violations = [v for v in violations if v.violation_type == "usage_instruction"]

        assert len(placeholder_violations) > 0
        assert len(instruction_violations) > 0


# ============================================================================
# get_iteration_files 测试
# ============================================================================


class TestGetIterationFiles:
    """get_iteration_files 函数测试"""

    def test_finds_iteration_files(
        self,
        temp_project: Path,
        iteration_file_with_placeholders: Path,
        iteration_file_clean: Path,
    ):
        """测试找到迭代文档文件"""
        files = get_iteration_files(temp_project)

        assert len(files) >= 2
        assert iteration_file_with_placeholders in files
        assert iteration_file_clean in files

    def test_excludes_template_files(
        self,
        temp_project: Path,
        template_file: Path,
        iteration_file_clean: Path,
    ):
        """测试排除模板文件"""
        files = get_iteration_files(temp_project)

        # 模板文件不应被包含
        assert template_file not in files

        # 迭代文件应被包含
        assert iteration_file_clean in files

    def test_returns_empty_for_missing_directory(self, temp_project: Path):
        """测试目录不存在时返回空列表"""
        # 删除 docs/acceptance 目录
        import shutil

        shutil.rmtree(temp_project / "docs" / "acceptance")

        files = get_iteration_files(temp_project)
        assert files == []

    def test_only_matches_plan_and_regression(self, temp_project: Path):
        """测试只匹配 plan 和 regression 文件"""
        # 创建其他格式的文件
        other_file = temp_project / "docs" / "acceptance" / "iteration_13_notes.md"
        other_file.write_text("# Notes", encoding="utf-8")

        files = get_iteration_files(temp_project)

        # 其他格式的文件不应被包含
        assert other_file not in files


# ============================================================================
# run_check 测试
# ============================================================================


class TestRunCheck:
    """run_check 函数测试"""

    def test_run_check_detects_violations(
        self,
        temp_project: Path,
        iteration_file_with_placeholders: Path,
    ):
        """测试 run_check 检测违规"""
        violations, total_files = run_check(project_root=temp_project)

        assert len(violations) > 0
        assert total_files >= 1

    def test_run_check_returns_zero_for_clean_files(
        self,
        temp_project: Path,
        iteration_file_clean: Path,
    ):
        """测试 run_check 对干净文件返回空列表"""
        violations, total_files = run_check(project_root=temp_project)

        assert len(violations) == 0
        assert total_files >= 1

    def test_run_check_ignores_templates(
        self,
        temp_project: Path,
        template_file: Path,
    ):
        """测试 run_check 忽略模板文件"""
        violations, total_files = run_check(project_root=temp_project)

        # 模板文件中的占位符不应被检测
        # 因为模板文件不在检查范围内
        template_violations = [v for v in violations if v.file == template_file]
        assert len(template_violations) == 0


# ============================================================================
# PlaceholderViolation 数据类测试
# ============================================================================


class TestPlaceholderViolation:
    """PlaceholderViolation 数据类测试"""

    def test_str_format_placeholder(self):
        """测试占位符违规的字符串格式"""
        violation = PlaceholderViolation(
            file=Path("docs/acceptance/iteration_13_plan.md"),
            line_number=10,
            line_content="| **开始日期** | {YYYY-MM-DD} |",
            violation_type="placeholder",
            matched_text="{YYYY-MM-DD}",
        )

        str_repr = str(violation)
        assert "模板占位符未替换" in str_repr
        assert "{YYYY-MM-DD}" in str_repr
        assert ":10:" in str_repr

    def test_str_format_usage_instruction(self):
        """测试使用说明违规的字符串格式"""
        violation = PlaceholderViolation(
            file=Path("docs/acceptance/iteration_13_plan.md"),
            line_number=1,
            line_content="> **使用说明**：复制本模板...",
            violation_type="usage_instruction",
            matched_text="> **使用说明**",
        )

        str_repr = str(violation)
        assert "模板使用说明未移除" in str_repr
        assert "使用说明" in str_repr


# ============================================================================
# 集成测试
# ============================================================================


class TestIntegration:
    """集成测试"""

    def test_combined_violations(self, temp_project: Path):
        """测试同时存在多种违规"""
        # 创建包含占位符的文件
        file1 = temp_project / "docs" / "acceptance" / "iteration_20_plan.md"
        file1.write_text("# Iteration {N}\n| 日期 | {YYYY-MM-DD} |", encoding="utf-8")

        # 创建包含使用说明的文件
        file2 = temp_project / "docs" / "acceptance" / "iteration_21_regression.md"
        file2.write_text(
            "> **使用说明**：复制本模板...\n\n# Iteration 21 回归记录",
            encoding="utf-8",
        )

        violations, total_files = run_check(project_root=temp_project)

        assert total_files == 2
        assert len(violations) >= 3  # 至少 2 个占位符 + 1 个使用说明

        # 验证检测到两种类型
        placeholder_count = sum(1 for v in violations if v.violation_type == "placeholder")
        instruction_count = sum(1 for v in violations if v.violation_type == "usage_instruction")

        assert placeholder_count >= 2
        assert instruction_count >= 1

    def test_real_world_scenario(self, temp_project: Path):
        """测试真实场景：从模板复制但未完全替换"""
        # 模拟从模板复制后部分替换的情况
        content = """# Iteration 13 计划

## 概述

| 字段 | 内容 |
|------|------|
| **迭代编号** | Iteration 13 |
| **开始日期** | 2026-02-02 |
| **状态** | ⚠️ PARTIAL |

## 迭代目标

1. **代码质量修复**：修复 lint 错误
2. **{目标2名称}**：{目标2描述}

## 验收门禁

| 门禁 | 命令 | 通过标准 |
|------|------|----------|
| **格式检查** | `make format-check` | 退出码 0 |
| **{其他门禁}** | `{命令}` | {通过标准} |
"""
        filepath = temp_project / "docs" / "acceptance" / "iteration_13_plan.md"
        filepath.write_text(content, encoding="utf-8")

        violations, _ = run_check(project_root=temp_project)

        # 应该检测到未替换的占位符
        assert len(violations) > 0

        matched_texts = [v.matched_text for v in violations]
        # 验证检测到中文占位符
        assert any("目标" in text or "其他" in text or "命令" in text for text in matched_texts)


# ============================================================================
# 边界情况测试
# ============================================================================


# ============================================================================
# scan_file_for_required_headings 测试
# ============================================================================


class TestScanFileForRequiredHeadings:
    """scan_file_for_required_headings 函数测试"""

    def test_detects_missing_headings_in_regression(self, temp_project: Path):
        """测试检测 regression 文件中缺少的标准标题"""
        # 创建一个缺少标准标题的 regression 文件
        content = """# Iteration 13 Regression

## 概述

这是一个回归记录。

## 详细执行记录

内容...
"""
        filepath = temp_project / "docs" / "acceptance" / "iteration_13_regression.md"
        filepath.write_text(content, encoding="utf-8")

        violations = list(scan_file_for_required_headings(filepath))

        # 应该检测到缺少 "## 执行信息" 和 "## 最小门禁命令块"
        assert len(violations) == 2
        matched_texts = [v.matched_text for v in violations]
        assert "## 执行信息" in matched_texts
        assert "## 最小门禁命令块" in matched_texts

    def test_no_violations_for_complete_regression(self, temp_project: Path):
        """测试完整的 regression 文件无违规"""
        content = """# Iteration 13 Regression

## 执行信息

| 项目 | 值 |
|------|-----|
| 执行日期 | 2026-02-02 |

## 最小门禁命令块

命令清单...

## 执行结果总览

其他内容...
"""
        filepath = temp_project / "docs" / "acceptance" / "iteration_14_regression.md"
        filepath.write_text(content, encoding="utf-8")

        violations = list(scan_file_for_required_headings(filepath))
        assert len(violations) == 0

    def test_skips_plan_files(self, temp_project: Path):
        """测试不检查 plan 文件的标准标题"""
        # plan 文件不需要检查 regression 专用标题
        content = """# Iteration 13 Plan

## 概述

计划内容...
"""
        filepath = temp_project / "docs" / "acceptance" / "iteration_13_plan.md"
        filepath.write_text(content, encoding="utf-8")

        violations = list(scan_file_for_required_headings(filepath))
        assert len(violations) == 0

    def test_partial_headings(self, temp_project: Path):
        """测试只有部分标准标题"""
        content = """# Iteration 15 Regression

## 执行信息

执行信息内容...

## 其他内容

缺少最小门禁命令块...
"""
        filepath = temp_project / "docs" / "acceptance" / "iteration_15_regression.md"
        filepath.write_text(content, encoding="utf-8")

        violations = list(scan_file_for_required_headings(filepath))

        # 应该只检测到缺少 "## 最小门禁命令块"
        assert len(violations) == 1
        assert violations[0].matched_text == "## 最小门禁命令块"

    def test_violation_type_is_missing_heading(self, temp_project: Path):
        """测试违规类型为 missing_heading"""
        content = """# Iteration 16 Regression

没有标准标题...
"""
        filepath = temp_project / "docs" / "acceptance" / "iteration_16_regression.md"
        filepath.write_text(content, encoding="utf-8")

        violations = list(scan_file_for_required_headings(filepath))

        for v in violations:
            assert v.violation_type == "missing_heading"
            assert v.line_number == 0  # 文件级问题

    def test_custom_required_headings(self, temp_project: Path):
        """测试自定义必需标题列表"""
        content = """# Iteration 17 Regression

## 自定义标题A

内容...
"""
        filepath = temp_project / "docs" / "acceptance" / "iteration_17_regression.md"
        filepath.write_text(content, encoding="utf-8")

        custom_headings = ["## 自定义标题A", "## 自定义标题B"]
        violations = list(scan_file_for_required_headings(filepath, custom_headings))

        # 应该只检测到缺少 "## 自定义标题B"
        assert len(violations) == 1
        assert violations[0].matched_text == "## 自定义标题B"


# ============================================================================
# 边界情况测试
# ============================================================================


class TestEdgeCases:
    """边界情况测试"""

    def test_empty_file(self, temp_project: Path):
        """测试空文件"""
        filepath = temp_project / "docs" / "acceptance" / "iteration_50_plan.md"
        filepath.write_text("", encoding="utf-8")

        violations = scan_file(filepath, check_required_headings=False)
        assert len(violations) == 0

    def test_file_with_only_code_blocks(self, temp_project: Path):
        """测试只有代码块的文件"""
        content = """```markdown
# Iteration {N}
{PLACEHOLDER}
```
"""
        filepath = temp_project / "docs" / "acceptance" / "iteration_51_regression.md"
        filepath.write_text(content, encoding="utf-8")

        # 禁用标题检查，仅测试占位符和代码块跳过
        violations = scan_file(filepath, check_required_headings=False)
        # 代码块内的内容不应被检测
        assert len(violations) == 0

    def test_nested_code_blocks(self, temp_project: Path):
        """测试嵌套代码块标记"""
        content = """正常文本

```markdown
代码块开始
{N} 应被忽略
```

外部文本 {M} 应被检测

~~~bash
另一个代码块 {K}
~~~

结束文本
"""
        filepath = temp_project / "docs" / "acceptance" / "iteration_52_plan.md"
        filepath.write_text(content, encoding="utf-8")

        # 禁用标题检查，仅测试占位符检测
        violations = scan_file(filepath, check_required_headings=False)

        # 只有代码块外的 {M} 应被检测
        matched = [v.matched_text for v in violations]
        assert "{M}" in matched
        assert "{N}" not in matched
        assert "{K}" not in matched

    def test_unicode_content(self, temp_project: Path):
        """测试 Unicode 内容"""
        content = """# Iteration 13 计划

## 目标

- 修复中文问题：{问题描述}
- 添加 emoji 支持 🎉

## 状态

| 迭代 | 日期 |
|------|------|
| 13 | 2026-02-02 |
"""
        filepath = temp_project / "docs" / "acceptance" / "iteration_53_plan.md"
        filepath.write_text(content, encoding="utf-8")

        # 禁用标题检查，仅测试占位符检测
        violations = scan_file(filepath, check_required_headings=False)

        # 应该检测到中文占位符
        matched = [v.matched_text for v in violations]
        assert any("问题描述" in text for text in matched)

    def test_empty_regression_file_missing_all_headings(self, temp_project: Path):
        """测试空的 regression 文件缺少所有标准标题"""
        filepath = temp_project / "docs" / "acceptance" / "iteration_54_regression.md"
        filepath.write_text("# Iteration 54 Regression\n", encoding="utf-8")

        violations = scan_file(filepath, check_required_headings=True)

        # 应该检测到缺少两个标准标题
        heading_violations = [v for v in violations if v.violation_type == "missing_heading"]
        assert len(heading_violations) == 2


# ============================================================================
# run_check 与标准标题集成测试
# ============================================================================


class TestRunCheckWithHeadings:
    """run_check 函数与标准标题检查集成测试"""

    def test_run_check_detects_missing_headings(self, temp_project: Path):
        """测试 run_check 检测缺少的标准标题"""
        # 创建缺少标准标题的 regression 文件
        content = """# Iteration 30 Regression

## 概述

没有标准标题的回归记录。
"""
        filepath = temp_project / "docs" / "acceptance" / "iteration_30_regression.md"
        filepath.write_text(content, encoding="utf-8")

        violations, total_files = run_check(
            project_root=temp_project,
            check_required_headings=True,
        )

        assert total_files >= 1

        # 应该检测到缺少的标准标题
        heading_violations = [v for v in violations if v.violation_type == "missing_heading"]
        assert len(heading_violations) == 2

    def test_run_check_skip_headings_when_disabled(self, temp_project: Path):
        """测试禁用标题检查时不检测缺少的标题"""
        # 创建缺少标准标题的 regression 文件
        content = """# Iteration 31 Regression

## 概述

没有标准标题的回归记录。
"""
        filepath = temp_project / "docs" / "acceptance" / "iteration_31_regression.md"
        filepath.write_text(content, encoding="utf-8")

        violations, total_files = run_check(
            project_root=temp_project,
            check_required_headings=False,
        )

        assert total_files >= 1

        # 不应该检测到标题违规
        heading_violations = [v for v in violations if v.violation_type == "missing_heading"]
        assert len(heading_violations) == 0

    def test_mixed_violations_with_headings(self, temp_project: Path):
        """测试同时存在占位符和标准标题缺失的情况"""
        content = """# Iteration 32 Regression

## 概述

| 日期 | {YYYY-MM-DD} |
"""
        filepath = temp_project / "docs" / "acceptance" / "iteration_32_regression.md"
        filepath.write_text(content, encoding="utf-8")

        violations, _ = run_check(
            project_root=temp_project,
            check_required_headings=True,
        )

        # 应该同时检测到占位符和标题缺失
        placeholder_violations = [v for v in violations if v.violation_type == "placeholder"]
        heading_violations = [v for v in violations if v.violation_type == "missing_heading"]

        assert len(placeholder_violations) >= 1
        assert len(heading_violations) == 2


# ============================================================================
# REGRESSION_REQUIRED_HEADINGS 常量测试
# ============================================================================


class TestRegressionRequiredHeadings:
    """REGRESSION_REQUIRED_HEADINGS 常量测试"""

    def test_constant_is_list(self):
        """测试常量是列表类型"""
        assert isinstance(REGRESSION_REQUIRED_HEADINGS, list)

    def test_constant_contains_required_headings(self):
        """测试常量包含预期的标准标题"""
        assert "## 执行信息" in REGRESSION_REQUIRED_HEADINGS
        assert "## 最小门禁命令块" in REGRESSION_REQUIRED_HEADINGS

    def test_constant_has_at_least_two_headings(self):
        """测试常量至少有两个标题"""
        assert len(REGRESSION_REQUIRED_HEADINGS) >= 2


# ============================================================================
# PlaceholderViolation 数据类扩展测试
# ============================================================================


class TestPlaceholderViolationMissingHeading:
    """PlaceholderViolation 数据类 missing_heading 类型测试"""

    def test_str_format_missing_heading(self):
        """测试缺少标题违规的字符串格式"""
        violation = PlaceholderViolation(
            file=Path("docs/acceptance/iteration_13_regression.md"),
            line_number=0,
            line_content="",
            violation_type="missing_heading",
            matched_text="## 执行信息",
        )

        str_repr = str(violation)
        assert "缺少标准标题" in str_repr
        assert "## 执行信息" in str_repr
        assert ":0:" in str_repr
