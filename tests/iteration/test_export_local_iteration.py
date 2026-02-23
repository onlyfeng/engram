#!/usr/bin/env python3
"""
export_local_iteration.py 单元测试

覆盖功能:
1. 正常导出（stdout 和文件两种模式）
2. 输出包含必要的"非 SSOT"声明和下一步指令
3. 检测草稿中的 .iteration/ 链接并发出警告
4. 不包含可点击的 .iteration/ 链接（使用正则断言）
5. 源文件不存在时的错误处理

Fixtures 使用临时目录构造 .iteration/<N>/ 结构。
"""

from __future__ import annotations

import re
import tempfile
import zipfile
from pathlib import Path

import pytest

from scripts.iteration.export_local_iteration import (
    IterationLinkWarning,
    SourceNotFoundError,
    detect_iteration_links,
    export_iteration,
    export_iteration_zip,
    format_warnings,
    get_export_footer,
    get_export_header,
    get_zip_readme_content,
)

# ============================================================================
# 正则模式：用于断言导出内容不包含可点击的 .iteration/ 链接
# ============================================================================

# 匹配 Markdown 链接格式: [text](.../.iteration/...) 或 [text](.iteration/...)
CLICKABLE_ITERATION_LINK_PATTERN = re.compile(
    r"\[([^\]]*)\]\(([^)]*\.iteration[^)]*)\)",
    re.IGNORECASE,
)


def has_clickable_iteration_link(content: str) -> bool:
    """检查内容是否包含可点击的 .iteration/ 链接。

    Args:
        content: 要检查的内容

    Returns:
        True 如果包含可点击链接，否则 False
    """
    return bool(CLICKABLE_ITERATION_LINK_PATTERN.search(content))


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def temp_project():
    """创建临时项目目录结构。"""
    with tempfile.TemporaryDirectory(prefix="test_export_") as tmpdir:
        project = Path(tmpdir)

        # 创建 .iteration/ 目录
        (project / ".iteration").mkdir(parents=True)

        yield project


@pytest.fixture
def temp_project_with_iteration(temp_project: Path) -> Path:
    """创建带有本地迭代草稿的临时项目。"""
    # 创建 .iteration/13/ 目录和文件
    iter_dir = temp_project / ".iteration" / "13"
    iter_dir.mkdir(parents=True)

    (iter_dir / "plan.md").write_text(
        """# Iteration 13 计划

## 目标

测试导出功能。

## 任务列表

- [ ] 任务 1
- [ ] 任务 2
""",
        encoding="utf-8",
    )

    (iter_dir / "regression.md").write_text(
        """# Iteration 13 回归记录

## 验收结果

待填写。

## 门禁命令

```bash
make ci
```
""",
        encoding="utf-8",
    )

    return temp_project


@pytest.fixture
def temp_project_with_bad_links(temp_project: Path) -> Path:
    """创建包含 .iteration/ 链接的临时项目。"""
    iter_dir = temp_project / ".iteration" / "14"
    iter_dir.mkdir(parents=True)

    # plan.md 包含 .iteration/ 链接
    (iter_dir / "plan.md").write_text(
        """# Iteration 14 计划

## 参考

- 参见 [本地草稿](.iteration/14/regression.md)
- 另见 [旧计划](../.iteration/13/plan.md)
""",
        encoding="utf-8",
    )

    # regression.md 也包含 .iteration/ 链接
    (iter_dir / "regression.md").write_text(
        """# Iteration 14 回归记录

## 依赖

详见 [计划文件](.iteration/14/plan.md)
""",
        encoding="utf-8",
    )

    return temp_project


# ============================================================================
# 辅助函数测试
# ============================================================================


class TestDetectIterationLinks:
    """detect_iteration_links 函数测试"""

    def test_detects_simple_link(self):
        """测试检测简单的 .iteration/ 链接"""
        content = "参见 [草稿](.iteration/13/plan.md)"
        warnings = detect_iteration_links(content, "test.md")

        assert len(warnings) == 1
        assert warnings[0].file_name == "test.md"
        assert warnings[0].line_number == 1
        assert ".iteration" in warnings[0].link_text

    def test_detects_relative_link(self):
        """测试检测相对路径的 .iteration/ 链接"""
        content = "参见 [旧草稿](../.iteration/12/plan.md)"
        warnings = detect_iteration_links(content, "test.md")

        assert len(warnings) == 1
        assert ".iteration" in warnings[0].link_text

    def test_detects_multiple_links(self):
        """测试检测多个链接"""
        content = """第一行 [a](.iteration/1/a.md)
第二行 [b](.iteration/2/b.md)
第三行 [c](.iteration/3/c.md)"""
        warnings = detect_iteration_links(content, "test.md")

        assert len(warnings) == 3
        assert warnings[0].line_number == 1
        assert warnings[1].line_number == 2
        assert warnings[2].line_number == 3

    def test_no_warnings_for_clean_content(self):
        """测试无 .iteration/ 链接的内容不产生警告"""
        content = """# 正常内容

参见 [SSOT 文档](docs/acceptance/iteration_13_plan.md)
使用 `.iteration/13/` 目录（纯文本引用）
"""
        warnings = detect_iteration_links(content, "test.md")

        assert len(warnings) == 0

    def test_ignores_inline_code(self):
        """测试忽略 inline code 中的 .iteration/"""
        # 注意：当前实现会检测到链接格式，但 inline code 不是链接格式
        content = "使用 `.iteration/13/` 目录"
        warnings = detect_iteration_links(content, "test.md")

        assert len(warnings) == 0


class TestGetExportHeader:
    """get_export_header 函数测试"""

    def test_header_contains_non_ssot_warning(self):
        """测试头部包含"非 SSOT"警告"""
        header = get_export_header(13)

        assert "非 SSOT" in header
        assert "本地草稿" in header

    def test_header_contains_iteration_number(self):
        """测试头部包含迭代编号"""
        header = get_export_header(42)

        assert "42" in header
        assert ".iteration/42/" in header

    def test_header_contains_do_not_link_warning(self):
        """测试头部包含"请勿链接"警告"""
        header = get_export_header(13)

        assert "请勿" in header or "不应链接" in header
        assert ".iteration/" in header

    def test_header_has_no_clickable_iteration_link(self):
        """测试头部不包含可点击的 .iteration/ 链接"""
        header = get_export_header(13)

        assert not has_clickable_iteration_link(header), (
            f"头部包含可点击的 .iteration/ 链接: {header}"
        )


class TestGetExportFooter:
    """get_export_footer 函数测试"""

    def test_footer_contains_promote_command(self):
        """测试尾部包含晋升命令"""
        footer = get_export_footer(13)

        assert "promote_iteration.py" in footer
        assert "13" in footer

    def test_footer_contains_gate_commands(self):
        """测试尾部包含门禁命令"""
        footer = get_export_footer(13)

        assert "make ci" in footer
        # 验证使用正确的门禁 target（防止格式漂移）
        assert "make check-iteration-docs" in footer, (
            "footer 应使用 'make check-iteration-docs' 而非其他变体"
        )
        # 确保不使用旧的/错误的 target 名称
        assert "check-no-iteration-links-in-docs" not in footer, (
            "footer 不应使用 'check-no-iteration-links-in-docs'，应使用 'check-iteration-docs'"
        )

    def test_footer_contains_do_not_link_reminder(self):
        """测试尾部包含"不要链接"提醒"""
        footer = get_export_footer(13)

        assert "不要链接" in footer or "请勿" in footer
        assert ".iteration/" in footer

    def test_footer_has_no_clickable_iteration_link(self):
        """测试尾部不包含可点击的 .iteration/ 链接"""
        footer = get_export_footer(13)

        assert not has_clickable_iteration_link(footer), (
            f"尾部包含可点击的 .iteration/ 链接: {footer}"
        )


class TestFormatWarnings:
    """format_warnings 函数测试"""

    def test_empty_warnings(self):
        """测试空警告列表"""
        result = format_warnings([])
        assert result == ""

    def test_formats_warnings(self):
        """测试格式化警告"""
        warnings = [
            IterationLinkWarning(
                file_name="plan.md",
                line_number=5,
                line_content="参见 [草稿](.iteration/13/x.md)",
                link_text="[草稿](.iteration/13/x.md)",
            ),
        ]
        result = format_warnings(warnings)

        assert "plan.md:5" in result
        assert ".iteration/" in result
        assert "建议" in result


# ============================================================================
# 核心导出功能测试
# ============================================================================


class TestExportIterationStdout:
    """stdout 输出模式测试"""

    def test_exports_plan_content(self, temp_project_with_iteration: Path, monkeypatch):
        """测试导出 plan.md 内容"""
        monkeypatch.setattr(
            "export_local_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )

        result = export_iteration(13)

        assert result.success is True
        assert result.plan_content is not None
        assert "Iteration 13 计划" in result.plan_content

    def test_exports_regression_content(self, temp_project_with_iteration: Path, monkeypatch):
        """测试导出 regression.md 内容"""
        monkeypatch.setattr(
            "export_local_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )

        result = export_iteration(13)

        assert result.success is True
        assert result.regression_content is not None
        assert "Iteration 13 回归记录" in result.regression_content

    def test_content_includes_header_and_footer(
        self, temp_project_with_iteration: Path, monkeypatch
    ):
        """测试导出内容包含头部和尾部"""
        monkeypatch.setattr(
            "export_local_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )

        result = export_iteration(13)

        # 检查头部
        assert "非 SSOT" in result.plan_content
        assert "非 SSOT" in result.regression_content

        # 检查尾部
        assert "promote_iteration.py" in result.plan_content
        assert "promote_iteration.py" in result.regression_content

    def test_exported_content_has_no_clickable_iteration_links(
        self, temp_project_with_iteration: Path, monkeypatch
    ):
        """测试导出内容（头部和尾部）不包含可点击的 .iteration/ 链接"""
        monkeypatch.setattr(
            "export_local_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )

        # 调用 export_iteration 确保函数正常工作
        export_iteration(13)

        # 提取头部和尾部（排除原始内容）
        # 头部和尾部是脚本添加的，应该不包含可点击链接
        header = get_export_header(13)
        footer = get_export_footer(13)

        assert not has_clickable_iteration_link(header)
        assert not has_clickable_iteration_link(footer)


class TestExportIterationFile:
    """文件输出模式测试"""

    def test_creates_output_files(self, temp_project_with_iteration: Path, monkeypatch):
        """测试创建输出文件"""
        monkeypatch.setattr(
            "export_local_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )

        output_dir = temp_project_with_iteration / ".artifacts" / "export"
        result = export_iteration(13, output_dir=output_dir)

        assert result.success is True
        assert len(result.output_files) == 2
        assert (output_dir / "plan.md").exists()
        assert (output_dir / "regression.md").exists()

    def test_file_content_includes_header_footer(
        self, temp_project_with_iteration: Path, monkeypatch
    ):
        """测试文件内容包含头部和尾部"""
        monkeypatch.setattr(
            "export_local_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )

        output_dir = temp_project_with_iteration / ".artifacts" / "export"
        export_iteration(13, output_dir=output_dir)

        plan_content = (output_dir / "plan.md").read_text(encoding="utf-8")
        regression_content = (output_dir / "regression.md").read_text(encoding="utf-8")

        # 检查头部
        assert "非 SSOT" in plan_content
        assert "非 SSOT" in regression_content

        # 检查尾部
        assert "promote_iteration.py" in plan_content
        assert "promote_iteration.py" in regression_content

    def test_creates_output_directory(self, temp_project_with_iteration: Path, monkeypatch):
        """测试自动创建输出目录"""
        monkeypatch.setattr(
            "export_local_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )

        output_dir = temp_project_with_iteration / "deep" / "nested" / "dir"
        assert not output_dir.exists()

        result = export_iteration(13, output_dir=output_dir)

        assert result.success is True
        assert output_dir.exists()


class TestExportIterationWarnings:
    """.iteration/ 链接警告测试"""

    def test_detects_bad_links_in_plan(self, temp_project_with_bad_links: Path, monkeypatch):
        """测试检测 plan.md 中的 .iteration/ 链接"""
        monkeypatch.setattr(
            "export_local_iteration.ITERATION_DIR",
            temp_project_with_bad_links / ".iteration",
        )

        result = export_iteration(14)

        plan_warnings = [w for w in result.warnings if w.file_name == "plan.md"]
        assert len(plan_warnings) >= 2  # plan.md 中有至少 2 个链接

    def test_detects_bad_links_in_regression(self, temp_project_with_bad_links: Path, monkeypatch):
        """测试检测 regression.md 中的 .iteration/ 链接"""
        monkeypatch.setattr(
            "export_local_iteration.ITERATION_DIR",
            temp_project_with_bad_links / ".iteration",
        )

        result = export_iteration(14)

        regression_warnings = [w for w in result.warnings if w.file_name == "regression.md"]
        assert len(regression_warnings) >= 1

    def test_no_warnings_for_clean_draft(self, temp_project_with_iteration: Path, monkeypatch):
        """测试干净的草稿无警告"""
        monkeypatch.setattr(
            "export_local_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )

        result = export_iteration(13)

        assert len(result.warnings) == 0


class TestExportIterationSourceNotFound:
    """源文件不存在测试"""

    def test_raises_for_missing_iteration_dir(self, temp_project: Path, monkeypatch):
        """测试迭代目录不存在时抛出错误"""
        monkeypatch.setattr(
            "export_local_iteration.ITERATION_DIR",
            temp_project / ".iteration",
        )

        with pytest.raises(SourceNotFoundError):
            export_iteration(99)  # 不存在的迭代

    def test_raises_for_empty_iteration_dir(self, temp_project: Path, monkeypatch):
        """测试迭代目录为空时抛出错误"""
        monkeypatch.setattr(
            "export_local_iteration.ITERATION_DIR",
            temp_project / ".iteration",
        )

        # 创建空的迭代目录
        iter_dir = temp_project / ".iteration" / "20"
        iter_dir.mkdir(parents=True)

        with pytest.raises(SourceNotFoundError):
            export_iteration(20)


class TestExportIterationPartialFiles:
    """部分文件存在测试"""

    def test_exports_with_only_plan(self, temp_project: Path, monkeypatch):
        """测试仅有 plan.md 时正常导出"""
        monkeypatch.setattr(
            "export_local_iteration.ITERATION_DIR",
            temp_project / ".iteration",
        )

        # 创建仅有 plan.md 的迭代
        iter_dir = temp_project / ".iteration" / "15"
        iter_dir.mkdir(parents=True)
        (iter_dir / "plan.md").write_text("# Plan only", encoding="utf-8")

        result = export_iteration(15)

        assert result.success is True
        assert result.plan_content is not None
        assert result.regression_content is None

    def test_exports_with_only_regression(self, temp_project: Path, monkeypatch):
        """测试仅有 regression.md 时正常导出"""
        monkeypatch.setattr(
            "export_local_iteration.ITERATION_DIR",
            temp_project / ".iteration",
        )

        # 创建仅有 regression.md 的迭代
        iter_dir = temp_project / ".iteration" / "16"
        iter_dir.mkdir(parents=True)
        (iter_dir / "regression.md").write_text("# Regression only", encoding="utf-8")

        result = export_iteration(16)

        assert result.success is True
        assert result.plan_content is None
        assert result.regression_content is not None


# ============================================================================
# 导出内容合规性测试（正则断言）
# ============================================================================


class TestExportedContentCompliance:
    """导出内容合规性测试 - 使用正则断言"""

    def test_header_contains_required_disclaimers(self):
        """测试头部包含必要的免责声明"""
        header = get_export_header(13)

        # 必须包含"非 SSOT"
        assert re.search(r"非\s*SSOT", header), "头部缺少'非 SSOT'声明"

        # 必须包含"本地草稿"
        assert re.search(r"本地.*草稿|草稿.*本地", header), "头部缺少'本地草稿'声明"

        # 必须包含"不应链接"或"请勿链接"
        assert re.search(r"不应.*链接|请勿.*链接|禁止.*链接", header), "头部缺少'不应链接'警告"

    def test_footer_contains_required_instructions(self):
        """测试尾部包含必要的下一步指令"""
        footer = get_export_footer(13)

        # 必须包含晋升命令
        assert "promote_iteration.py" in footer, "尾部缺少晋升命令"

        # 必须包含门禁命令
        assert re.search(r"make\s+ci", footer), "尾部缺少门禁命令"

    def test_no_markdown_links_to_iteration_in_header(self):
        """测试头部不包含 Markdown 格式的 .iteration/ 链接"""
        header = get_export_header(13)

        # 正则匹配 [text](.../.iteration/...) 格式
        matches = CLICKABLE_ITERATION_LINK_PATTERN.findall(header)
        assert len(matches) == 0, f"头部包含违规链接: {matches}"

    def test_no_markdown_links_to_iteration_in_footer(self):
        """测试尾部不包含 Markdown 格式的 .iteration/ 链接"""
        footer = get_export_footer(13)

        matches = CLICKABLE_ITERATION_LINK_PATTERN.findall(footer)
        assert len(matches) == 0, f"尾部包含违规链接: {matches}"

    def test_iteration_path_references_use_text_or_inline_code(self):
        """测试 .iteration/ 路径引用使用文本或 inline code 格式"""
        header = get_export_header(13)
        footer = get_export_footer(13)

        # 头部中的 .iteration/ 应该使用 inline code 格式
        # 检查是否有 `.iteration/` 格式
        inline_code_refs = re.findall(r"`[^`]*\.iteration[^`]*`", header + footer)

        # 至少应该有一些 inline code 格式的引用
        assert len(inline_code_refs) >= 1, "应使用 inline code 格式引用 .iteration/"

    def test_exported_content_is_self_contained(
        self, temp_project_with_iteration: Path, monkeypatch
    ):
        """测试导出内容是自包含的（包含所有必要信息）"""
        monkeypatch.setattr(
            "export_local_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )

        result = export_iteration(13)

        # 检查 plan 导出内容
        plan = result.plan_content
        assert plan is not None

        # 包含原始内容
        assert "Iteration 13 计划" in plan

        # 包含来源声明
        assert ".iteration/13/" in plan

        # 包含下一步指令
        assert "promote_iteration.py 13" in plan


# ============================================================================
# 边界情况测试
# ============================================================================


class TestEdgeCases:
    """边界情况测试"""

    def test_handles_unicode_content(self, temp_project: Path, monkeypatch):
        """测试处理 Unicode 内容"""
        monkeypatch.setattr(
            "export_local_iteration.ITERATION_DIR",
            temp_project / ".iteration",
        )

        iter_dir = temp_project / ".iteration" / "17"
        iter_dir.mkdir(parents=True)
        (iter_dir / "plan.md").write_text(
            "# 中文标题 🎉\n\n内容包含 emoji 和特殊字符 ™©®",
            encoding="utf-8",
        )

        result = export_iteration(17)

        assert result.success is True
        assert "中文标题" in result.plan_content
        assert "🎉" in result.plan_content

    def test_handles_large_content(self, temp_project: Path, monkeypatch):
        """测试处理大文件"""
        monkeypatch.setattr(
            "export_local_iteration.ITERATION_DIR",
            temp_project / ".iteration",
        )

        iter_dir = temp_project / ".iteration" / "18"
        iter_dir.mkdir(parents=True)

        # 创建一个较大的文件
        large_content = "# Large File\n\n" + ("Line of content.\n" * 10000)
        (iter_dir / "plan.md").write_text(large_content, encoding="utf-8")

        result = export_iteration(18)

        assert result.success is True
        assert "Large File" in result.plan_content

    def test_iteration_number_zero_raises(self, temp_project: Path, monkeypatch):
        """测试迭代编号 0 的处理"""
        monkeypatch.setattr(
            "export_local_iteration.ITERATION_DIR",
            temp_project / ".iteration",
        )

        # 迭代 0 目录不存在，应该抛出 SourceNotFoundError
        with pytest.raises(SourceNotFoundError):
            export_iteration(0)

    def test_negative_iteration_number_raises(self, temp_project: Path, monkeypatch):
        """测试负数迭代编号的处理"""
        monkeypatch.setattr(
            "export_local_iteration.ITERATION_DIR",
            temp_project / ".iteration",
        )

        with pytest.raises(SourceNotFoundError):
            export_iteration(-1)


# ============================================================================
# ZIP 导出功能测试
# ============================================================================


class TestGetZipReadmeContent:
    """get_zip_readme_content 函数测试"""

    def test_contains_iteration_number(self):
        """测试 README 包含迭代编号"""
        readme = get_zip_readme_content(13)

        assert "Iteration 13" in readme
        assert ".iteration/13/" in readme

    def test_contains_non_ssot_warning(self):
        """测试 README 包含"非 SSOT"警告"""
        readme = get_zip_readme_content(13)

        assert "非 SSOT" in readme
        assert "本地草稿" in readme

    def test_contains_usage_instructions(self):
        """测试 README 包含使用说明"""
        readme = get_zip_readme_content(13)

        assert "promote_iteration.py" in readme
        assert "使用说明" in readme

    def test_has_no_clickable_iteration_link(self):
        """测试 README 不包含可点击的 .iteration/ 链接"""
        readme = get_zip_readme_content(13)

        assert not has_clickable_iteration_link(readme), (
            f"README 包含可点击的 .iteration/ 链接: {readme}"
        )


class TestExportIterationZip:
    """export_iteration_zip 函数测试"""

    def test_creates_zip_file(self, temp_project_with_iteration: Path, monkeypatch):
        """测试创建 zip 文件"""
        monkeypatch.setattr(
            "export_local_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )

        output_zip = temp_project_with_iteration / ".artifacts" / "export.zip"
        result = export_iteration_zip(13, output_zip=output_zip)

        assert result.success is True
        assert result.zip_path == str(output_zip)
        assert output_zip.exists()

    def test_zip_contains_readme(self, temp_project_with_iteration: Path, monkeypatch):
        """测试 zip 包含 README.md"""
        monkeypatch.setattr(
            "export_local_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )

        output_zip = temp_project_with_iteration / ".artifacts" / "export.zip"
        export_iteration_zip(13, output_zip=output_zip)

        with zipfile.ZipFile(output_zip, "r") as zf:
            assert "README.md" in zf.namelist()
            readme_content = zf.read("README.md").decode("utf-8")
            assert "Iteration 13" in readme_content

    def test_zip_contains_plan(self, temp_project_with_iteration: Path, monkeypatch):
        """测试 zip 包含 plan.md"""
        monkeypatch.setattr(
            "export_local_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )

        output_zip = temp_project_with_iteration / ".artifacts" / "export.zip"
        export_iteration_zip(13, output_zip=output_zip)

        with zipfile.ZipFile(output_zip, "r") as zf:
            assert "plan.md" in zf.namelist()
            plan_content = zf.read("plan.md").decode("utf-8")
            assert "Iteration 13 计划" in plan_content

    def test_zip_contains_regression(self, temp_project_with_iteration: Path, monkeypatch):
        """测试 zip 包含 regression.md"""
        monkeypatch.setattr(
            "export_local_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )

        output_zip = temp_project_with_iteration / ".artifacts" / "export.zip"
        export_iteration_zip(13, output_zip=output_zip)

        with zipfile.ZipFile(output_zip, "r") as zf:
            assert "regression.md" in zf.namelist()
            regression_content = zf.read("regression.md").decode("utf-8")
            assert "Iteration 13 回归记录" in regression_content

    def test_zip_content_includes_header_footer(
        self, temp_project_with_iteration: Path, monkeypatch
    ):
        """测试 zip 中的文件包含头部和尾部"""
        monkeypatch.setattr(
            "export_local_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )

        output_zip = temp_project_with_iteration / ".artifacts" / "export.zip"
        export_iteration_zip(13, output_zip=output_zip)

        with zipfile.ZipFile(output_zip, "r") as zf:
            plan_content = zf.read("plan.md").decode("utf-8")
            regression_content = zf.read("regression.md").decode("utf-8")

            # 检查头部
            assert "非 SSOT" in plan_content
            assert "非 SSOT" in regression_content

            # 检查尾部
            assert "promote_iteration.py" in plan_content
            assert "promote_iteration.py" in regression_content

    def test_creates_parent_directories(self, temp_project_with_iteration: Path, monkeypatch):
        """测试自动创建输出目录"""
        monkeypatch.setattr(
            "export_local_iteration.ITERATION_DIR",
            temp_project_with_iteration / ".iteration",
        )

        output_zip = temp_project_with_iteration / "deep" / "nested" / "export.zip"
        assert not output_zip.parent.exists()

        result = export_iteration_zip(13, output_zip=output_zip)

        assert result.success is True
        assert output_zip.exists()

    def test_detects_bad_links_in_zip_mode(self, temp_project_with_bad_links: Path, monkeypatch):
        """测试 zip 模式也检测 .iteration/ 链接"""
        monkeypatch.setattr(
            "export_local_iteration.ITERATION_DIR",
            temp_project_with_bad_links / ".iteration",
        )

        output_zip = temp_project_with_bad_links / ".artifacts" / "export.zip"
        result = export_iteration_zip(14, output_zip=output_zip)

        assert result.success is True
        assert len(result.warnings) > 0

    def test_raises_for_missing_source(self, temp_project: Path, monkeypatch):
        """测试源目录不存在时抛出错误"""
        monkeypatch.setattr(
            "export_local_iteration.ITERATION_DIR",
            temp_project / ".iteration",
        )

        output_zip = temp_project / ".artifacts" / "export.zip"
        with pytest.raises(SourceNotFoundError):
            export_iteration_zip(99, output_zip=output_zip)


class TestExportIterationZipPartialFiles:
    """部分文件存在时的 zip 导出测试"""

    def test_zip_with_only_plan(self, temp_project: Path, monkeypatch):
        """测试仅有 plan.md 时的 zip 导出"""
        monkeypatch.setattr(
            "export_local_iteration.ITERATION_DIR",
            temp_project / ".iteration",
        )

        # 创建仅有 plan.md 的迭代
        iter_dir = temp_project / ".iteration" / "15"
        iter_dir.mkdir(parents=True)
        (iter_dir / "plan.md").write_text("# Plan only", encoding="utf-8")

        output_zip = temp_project / ".artifacts" / "export.zip"
        result = export_iteration_zip(15, output_zip=output_zip)

        assert result.success is True
        assert result.plan_content is not None
        assert result.regression_content is None

        with zipfile.ZipFile(output_zip, "r") as zf:
            namelist = zf.namelist()
            assert "README.md" in namelist
            assert "plan.md" in namelist
            assert "regression.md" not in namelist

    def test_zip_with_only_regression(self, temp_project: Path, monkeypatch):
        """测试仅有 regression.md 时的 zip 导出"""
        monkeypatch.setattr(
            "export_local_iteration.ITERATION_DIR",
            temp_project / ".iteration",
        )

        # 创建仅有 regression.md 的迭代
        iter_dir = temp_project / ".iteration" / "16"
        iter_dir.mkdir(parents=True)
        (iter_dir / "regression.md").write_text("# Regression only", encoding="utf-8")

        output_zip = temp_project / ".artifacts" / "export.zip"
        result = export_iteration_zip(16, output_zip=output_zip)

        assert result.success is True
        assert result.plan_content is None
        assert result.regression_content is not None

        with zipfile.ZipFile(output_zip, "r") as zf:
            namelist = zf.namelist()
            assert "README.md" in namelist
            assert "plan.md" not in namelist
            assert "regression.md" in namelist


class TestZipUnicodeContent:
    """ZIP 导出 Unicode 内容测试"""

    def test_handles_unicode_in_zip(self, temp_project: Path, monkeypatch):
        """测试 zip 正确处理 Unicode 内容"""
        monkeypatch.setattr(
            "export_local_iteration.ITERATION_DIR",
            temp_project / ".iteration",
        )

        iter_dir = temp_project / ".iteration" / "17"
        iter_dir.mkdir(parents=True)
        (iter_dir / "plan.md").write_text(
            "# 中文标题 🎉\n\n内容包含 emoji 和特殊字符 ™©®",
            encoding="utf-8",
        )

        output_zip = temp_project / ".artifacts" / "export.zip"
        result = export_iteration_zip(17, output_zip=output_zip)

        assert result.success is True

        with zipfile.ZipFile(output_zip, "r") as zf:
            plan_content = zf.read("plan.md").decode("utf-8")
            assert "中文标题" in plan_content
            assert "🎉" in plan_content
