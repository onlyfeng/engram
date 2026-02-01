#!/usr/bin/env python3
"""
check_no_local_artifact_links_in_docs.py 单元测试

覆盖功能:
1. .artifacts/ 链接检测 - 验证能准确检测 Markdown 中的 .artifacts/ 链接
2. 代码块跳过 - 验证代码块内的链接不会被误检
3. 修复建议输出 - 验证报告包含正确的修复建议

Fixtures 使用小型 Markdown 文档，避免依赖真实文件。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from scripts.ci.check_no_local_artifact_links_in_docs import (
    ARTIFACT_LINK_PATTERN,
    ArtifactLinkViolation,
    run_check,
    scan_file_for_artifact_links,
)

# ============================================================================
# Fixtures - 小型 Markdown 文档
# ============================================================================


@pytest.fixture
def temp_project():
    """创建临时项目目录结构"""
    with tempfile.TemporaryDirectory(prefix="test_artifact_") as tmpdir:
        project = Path(tmpdir)
        (project / "docs" / "acceptance").mkdir(parents=True)
        (project / "docs" / "gateway").mkdir(parents=True)
        yield project


@pytest.fixture
def md_with_artifact_link(temp_project: Path) -> Path:
    """包含 .artifacts/ 链接的 Markdown 文件"""
    content = """# 示例文档

这里有一个合规的链接 [查看详情](../acceptance/evidence/report.png)。

但是这里有一个违规的链接 [覆盖率报告](../.artifacts/coverage/report.html)。

还有另一个 [截图](.artifacts/screenshots/test.png) 也是违规的。
"""
    filepath = temp_project / "docs" / "gateway" / "test.md"
    filepath.write_text(content, encoding="utf-8")
    return filepath


@pytest.fixture
def md_without_artifact_link(temp_project: Path) -> Path:
    """不包含 .artifacts/ 链接的 Markdown 文件"""
    content = """# 合规文档

所有链接都是合规的：

- [查看详情](../acceptance/evidence/report.png)
- [外部链接](https://example.com)
- [CI 运行](https://github.com/org/repo/actions/runs/123456)

代码块中的链接不应被检测：

```markdown
[示例](.artifacts/example.md)
```
"""
    filepath = temp_project / "docs" / "gateway" / "compliant.md"
    filepath.write_text(content, encoding="utf-8")
    return filepath


@pytest.fixture
def md_with_code_block_artifact_link(temp_project: Path) -> Path:
    """代码块中包含 .artifacts/ 链接的 Markdown 文件（不应被检测）"""
    content = """# 示例文档

正常文本不包含违规链接。

```markdown
# 这是代码块中的示例
[覆盖率报告](.artifacts/coverage/report.html)
```

~~~bash
echo "另一种代码块"
# [截图](.artifacts/screenshots/test.png)
~~~

代码块外的内容。
"""
    filepath = temp_project / "docs" / "gateway" / "codeblock.md"
    filepath.write_text(content, encoding="utf-8")
    return filepath


@pytest.fixture
def md_with_inline_code_artifact(temp_project: Path) -> Path:
    """包含 inline code 形式的 .artifacts/ 路径（合规，不应检测）"""
    content = """# 合规文档

这里提到路径 `.artifacts/coverage/` 是使用 inline code 形式的。

还可以这样写：运行后输出到 `.artifacts/` 目录。

但这个是违规的链接：[报告](../.artifacts/report.html)
"""
    filepath = temp_project / "docs" / "gateway" / "inline.md"
    filepath.write_text(content, encoding="utf-8")
    return filepath


@pytest.fixture
def md_with_inline_code_link_example(temp_project: Path) -> Path:
    """包含 inline code 中链接示例的 Markdown 文件（不应被检测）"""
    content = """# 示例说明文档

以下是链接使用规范表格：

| 类型 | 示例 | 允许 |
|------|------|------|
| **Markdown 链接** | `[报告](.artifacts/test-results.xml)` | ❌ **禁止** |
| **文本提及** | `本地产物位于 .artifacts/acceptance-runs/` | ✅ 允许 |

上面 inline code 中的链接是作为示例展示的，不应被检测。
"""
    filepath = temp_project / "docs" / "gateway" / "example_table.md"
    filepath.write_text(content, encoding="utf-8")
    return filepath


# ============================================================================
# ARTIFACT_LINK_PATTERN 正则表达式测试
# ============================================================================


class TestArtifactLinkPattern:
    """ARTIFACT_LINK_PATTERN 正则表达式测试"""

    def test_matches_simple_artifact_link(self):
        """测试匹配简单的 .artifacts/ 链接"""
        line = "[报告](.artifacts/report.html)"
        matches = ARTIFACT_LINK_PATTERN.findall(line)
        assert len(matches) == 1
        assert ".artifacts/report.html" in matches[0]

    def test_matches_parent_directory_link(self):
        """测试匹配 ../.artifacts/ 链接"""
        line = "[覆盖率](../.artifacts/coverage/index.html)"
        matches = ARTIFACT_LINK_PATTERN.findall(line)
        assert len(matches) == 1
        assert "../.artifacts/coverage/index.html" in matches[0]

    def test_matches_deep_nested_link(self):
        """测试匹配多层嵌套的 .artifacts/ 链接"""
        line = "[截图](../../.artifacts/screenshots/step1.png)"
        matches = ARTIFACT_LINK_PATTERN.findall(line)
        assert len(matches) == 1
        assert "../../.artifacts/screenshots/step1.png" in matches[0]

    def test_matches_path_with_artifacts_in_middle(self):
        """测试匹配路径中间包含 .artifacts/ 的链接"""
        line = "[文件](some/path/.artifacts/file.txt)"
        matches = ARTIFACT_LINK_PATTERN.findall(line)
        assert len(matches) == 1

    def test_no_match_for_regular_link(self):
        """测试不匹配普通链接"""
        line = "[文档](../acceptance/evidence/report.png)"
        matches = ARTIFACT_LINK_PATTERN.findall(line)
        assert len(matches) == 0

    def test_no_match_for_artifacts_without_dot(self):
        """测试不匹配不带点号的 artifacts 目录"""
        line = "[文档](artifacts/report.html)"
        matches = ARTIFACT_LINK_PATTERN.findall(line)
        assert len(matches) == 0

    def test_no_match_for_inline_code(self):
        """测试不匹配 inline code 中的路径"""
        # 正则只匹配 Markdown 链接格式 ](...)
        line = "路径是 `.artifacts/coverage/`"
        matches = ARTIFACT_LINK_PATTERN.findall(line)
        assert len(matches) == 0

    def test_multiple_links_in_line(self):
        """测试一行中的多个链接"""
        line = "[A](.artifacts/a.txt) 和 [B](../.artifacts/b.txt)"
        matches = ARTIFACT_LINK_PATTERN.findall(line)
        assert len(matches) == 2


# ============================================================================
# scan_file_for_artifact_links 函数测试
# ============================================================================


class TestScanFileForArtifactLinks:
    """scan_file_for_artifact_links 函数测试"""

    def test_detects_artifact_links(self, md_with_artifact_link: Path):
        """测试检测 .artifacts/ 链接"""
        violations = list(scan_file_for_artifact_links(md_with_artifact_link))

        assert len(violations) == 2

        # 验证违规记录
        links = [v.matched_link for v in violations]
        assert any(".artifacts/coverage/report.html" in link for link in links)
        assert any(".artifacts/screenshots/test.png" in link for link in links)

    def test_no_violations_for_compliant_file(self, md_without_artifact_link: Path):
        """测试合规文件无违规"""
        violations = list(scan_file_for_artifact_links(md_without_artifact_link))
        assert len(violations) == 0

    def test_skips_code_blocks(self, md_with_code_block_artifact_link: Path):
        """测试跳过代码块中的链接"""
        violations = list(scan_file_for_artifact_links(md_with_code_block_artifact_link))
        assert len(violations) == 0

    def test_violation_includes_line_number(self, md_with_artifact_link: Path):
        """测试违规记录包含行号"""
        violations = list(scan_file_for_artifact_links(md_with_artifact_link))

        for v in violations:
            assert v.line_number > 0
            assert v.file == md_with_artifact_link

    def test_inline_code_not_detected(self, md_with_inline_code_artifact: Path):
        """测试 inline code 形式的路径不被检测为违规"""
        violations = list(scan_file_for_artifact_links(md_with_inline_code_artifact))

        # 只有一个真正的链接违规
        assert len(violations) == 1
        assert ".artifacts/report.html" in violations[0].matched_link

    def test_inline_code_link_example_not_detected(self, md_with_inline_code_link_example: Path):
        """测试 inline code 中的链接示例不被检测为违规"""
        violations = list(scan_file_for_artifact_links(md_with_inline_code_link_example))

        # inline code 中的链接示例不应被检测
        assert len(violations) == 0


# ============================================================================
# run_check 函数测试
# ============================================================================


class TestRunCheck:
    """run_check 函数测试"""

    def test_run_check_detects_violations(self, temp_project: Path, md_with_artifact_link: Path):
        """测试 run_check 检测违规"""
        violations, total_files = run_check(
            paths=["docs/"],
            project_root=temp_project,
        )

        assert len(violations) == 2
        assert total_files >= 1

    def test_run_check_returns_zero_for_compliant(
        self, temp_project: Path, md_without_artifact_link: Path
    ):
        """测试 run_check 对合规项目返回空列表"""
        violations, total_files = run_check(
            paths=["docs/"],
            project_root=temp_project,
        )

        assert len(violations) == 0
        assert total_files >= 1

    def test_run_check_quiet_mode(self, temp_project: Path, md_with_artifact_link: Path, capsys):
        """测试 run_check 静默模式"""
        violations, total_files = run_check(
            paths=["docs/"],
            project_root=temp_project,
            quiet=True,
        )

        # 静默模式下不应有输出
        captured = capsys.readouterr()
        assert "[INFO]" not in captured.out

        # 但仍然应该检测到违规
        assert len(violations) == 2


# ============================================================================
# ArtifactLinkViolation 数据类测试
# ============================================================================


class TestArtifactLinkViolation:
    """ArtifactLinkViolation 数据类测试"""

    def test_str_format(self):
        """测试字符串格式"""
        violation = ArtifactLinkViolation(
            file=Path("docs/gateway/test.md"),
            line_number=10,
            line_content="[报告](.artifacts/report.html)",
            matched_link=".artifacts/report.html",
        )

        str_repr = str(violation)
        assert "docs/gateway/test.md" in str_repr
        assert "10" in str_repr
        assert ".artifacts/" in str_repr


# ============================================================================
# 修复建议测试
# ============================================================================


class TestPrintReportSuggestions:
    """测试 print_report 输出的修复建议文本"""

    def test_report_contains_ci_url_suggestion(
        self, temp_project: Path, md_with_artifact_link: Path, capsys, monkeypatch
    ):
        """测试报告包含 CI Run URL 的建议"""
        from scripts.ci import check_no_local_artifact_links_in_docs as module
        from scripts.ci.check_no_local_artifact_links_in_docs import print_report

        # Mock get_project_root 返回临时项目目录
        monkeypatch.setattr(module, "get_project_root", lambda: temp_project)

        violations, total_files = run_check(
            paths=["docs/"],
            project_root=temp_project,
        )

        # 确保有违规
        assert len(violations) > 0

        # 打印报告
        print_report(violations, total_files)

        # 捕获输出
        captured = capsys.readouterr()

        # 验证包含 CI URL 建议
        assert "CI Run URL" in captured.out
        assert "github.com" in captured.out or "actions/runs" in captured.out

    def test_report_contains_evidence_migration_suggestion(
        self, temp_project: Path, md_with_artifact_link: Path, capsys, monkeypatch
    ):
        """测试报告包含迁移到 evidence 目录的建议"""
        from scripts.ci import check_no_local_artifact_links_in_docs as module
        from scripts.ci.check_no_local_artifact_links_in_docs import print_report

        # Mock get_project_root 返回临时项目目录
        monkeypatch.setattr(module, "get_project_root", lambda: temp_project)

        violations, total_files = run_check(
            paths=["docs/"],
            project_root=temp_project,
        )

        # 确保有违规
        assert len(violations) > 0

        # 打印报告
        print_report(violations, total_files)

        # 捕获输出
        captured = capsys.readouterr()

        # 验证包含 evidence 目录建议
        assert "docs/acceptance/evidence/" in captured.out
        assert "迁移到" in captured.out or "正式证据" in captured.out

    def test_report_contains_inline_code_suggestion(
        self, temp_project: Path, md_with_artifact_link: Path, capsys, monkeypatch
    ):
        """测试报告包含 inline code 修复建议"""
        from scripts.ci import check_no_local_artifact_links_in_docs as module
        from scripts.ci.check_no_local_artifact_links_in_docs import print_report

        # Mock get_project_root 返回临时项目目录
        monkeypatch.setattr(module, "get_project_root", lambda: temp_project)

        violations, total_files = run_check(
            paths=["docs/"],
            project_root=temp_project,
        )

        # 确保有违规
        assert len(violations) > 0

        # 打印报告
        print_report(violations, total_files)

        # 捕获输出
        captured = capsys.readouterr()

        # 验证包含 inline code 建议
        assert "inline code" in captured.out
        assert "`.artifacts/" in captured.out

    def test_no_suggestions_when_no_violations(
        self, temp_project: Path, md_without_artifact_link: Path, capsys, monkeypatch
    ):
        """测试无违规时不显示修复建议"""
        from scripts.ci import check_no_local_artifact_links_in_docs as module
        from scripts.ci.check_no_local_artifact_links_in_docs import print_report

        # Mock get_project_root 返回临时项目目录
        monkeypatch.setattr(module, "get_project_root", lambda: temp_project)

        violations, total_files = run_check(
            paths=["docs/"],
            project_root=temp_project,
        )

        # 确保无违规
        assert len(violations) == 0

        # 打印报告
        print_report(violations, total_files)

        # 捕获输出
        captured = capsys.readouterr()

        # 验证不包含修复建议
        assert "CI Run URL" not in captured.out
        assert "[OK] 未发现 .artifacts/ 链接" in captured.out


# ============================================================================
# 集成测试
# ============================================================================


class TestIntegration:
    """集成测试"""

    def test_multiple_files_with_mixed_content(self, temp_project: Path):
        """测试多个文件混合内容的场景"""
        # 创建违规文件
        violation_file = temp_project / "docs" / "gateway" / "violation.md"
        violation_file.write_text(
            """# 违规文档
[报告](.artifacts/report.html)
""",
            encoding="utf-8",
        )

        # 创建合规文件
        compliant_file = temp_project / "docs" / "gateway" / "compliant.md"
        compliant_file.write_text(
            """# 合规文档
[详情](../acceptance/evidence/screenshot.png)
""",
            encoding="utf-8",
        )

        # 创建代码块中包含链接的文件
        codeblock_file = temp_project / "docs" / "gateway" / "codeblock.md"
        codeblock_file.write_text(
            """# 代码示例
```markdown
[报告](.artifacts/report.html)
```
""",
            encoding="utf-8",
        )

        violations, total_files = run_check(
            paths=["docs/"],
            project_root=temp_project,
        )

        # 只应检测到 violation.md 中的一个违规
        assert len(violations) == 1
        assert total_files == 3

    def test_nested_directory_scan(self, temp_project: Path):
        """测试嵌套目录扫描"""
        # 创建嵌套目录结构
        nested_dir = temp_project / "docs" / "gateway" / "subdir" / "deep"
        nested_dir.mkdir(parents=True)

        nested_file = nested_dir / "nested.md"
        nested_file.write_text(
            """# 嵌套文档
[报告](../../../../.artifacts/report.html)
""",
            encoding="utf-8",
        )

        violations, total_files = run_check(
            paths=["docs/"],
            project_root=temp_project,
        )

        # 应检测到嵌套文件中的违规
        assert len(violations) == 1
        assert violations[0].file == nested_file


# ============================================================================
# 真实仓库测试（CI 集成）
# ============================================================================


class TestRealRepository:
    """
    真实仓库测试

    验证当前仓库的 docs/ 目录不包含 .artifacts/ 链接。
    """

    @pytest.fixture
    def real_project_root(self) -> Path:
        """获取真实项目根目录"""
        return Path(__file__).parent.parent.parent

    def test_real_docs_no_artifact_links(self, real_project_root: Path):
        """测试真实 docs/ 目录不包含 .artifacts/ 链接"""
        docs_dir = real_project_root / "docs"

        if not docs_dir.exists():
            pytest.skip("docs/ 目录不存在")

        violations, total_files = run_check(
            paths=["docs/"],
            project_root=real_project_root,
            quiet=True,
        )

        # 如果有违规，打印详细信息便于调试
        if violations:
            print(f"\n[ERROR] 发现 {len(violations)} 个 .artifacts/ 链接违规:")
            for v in violations[:10]:
                rel_path = v.file.relative_to(real_project_root)
                print(f"  - {rel_path}:{v.line_number}: {v.matched_link}")
            if len(violations) > 10:
                print(f"  ... 及其他 {len(violations) - 10} 个违规")

        assert len(violations) == 0, (
            f"docs/ 目录中存在 {len(violations)} 个 .artifacts/ 链接违规，请参考修复指南进行修复"
        )
