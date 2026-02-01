"""
Workflow Contract 文档锚点检查脚本测试

测试 check_workflow_contract_doc_anchors.py 的功能：
1. GitHub anchor 生成规则
2. 锚点存在性检查
3. 缺失锚点错误报告
4. 自动提取锚点引用
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci.check_workflow_contract_doc_anchors import (
    AnchorCheckResult,
    WorkflowContractDocAnchorChecker,
    export_anchor_list,
    export_doc_anchors_json,
    extract_anchors_from_source,
    extract_headings_with_anchors,
    generate_github_anchor,
    get_required_anchors,
)

# ============================================================================
# Test GitHub Anchor Generation
# ============================================================================


class TestGenerateGithubAnchor:
    """测试 GitHub anchor 生成规则"""

    def test_simple_heading(self) -> None:
        """测试简单标题"""
        assert generate_github_anchor("Hello World") == "hello-world"

    def test_chinese_heading(self) -> None:
        """测试中文标题"""
        assert generate_github_anchor("第一章 介绍") == "第一章-介绍"

    def test_mixed_heading(self) -> None:
        """测试中英混合标题"""
        assert generate_github_anchor("5.2 Frozen Step Names") == "52-frozen-step-names"

    def test_special_characters_removed(self) -> None:
        """测试特殊字符被移除"""
        assert generate_github_anchor("Hello (World)") == "hello-world"
        assert generate_github_anchor("A / B") == "a-b"  # 特殊字符移除后连续连字符被合并
        assert generate_github_anchor("Test.md") == "testmd"

    def test_underscore_preserved(self) -> None:
        """测试下划线被保留"""
        assert generate_github_anchor("required_steps") == "required_steps"
        assert generate_github_anchor("5.5 required_steps 覆盖原则") == "55-required_steps-覆盖原则"

    def test_numbers_preserved(self) -> None:
        """测试数字被保留"""
        assert generate_github_anchor("Chapter 123") == "chapter-123"

    def test_consecutive_hyphens_collapsed(self) -> None:
        """测试连续连字符被合并"""
        # 实际上我们的实现是先替换再移除特殊字符，可能会有连续连字符
        result = generate_github_anchor("A - B - C")
        # 移除特殊字符后可能是 "a---b---c"，然后合并为 "a-b-c"
        assert "--" not in result or result == "a-b-c"

    def test_leading_trailing_hyphens_removed(self) -> None:
        """测试首尾连字符被移除"""
        assert generate_github_anchor(" Hello ") == "hello"
        assert generate_github_anchor("-Hello-") == "hello"


# ============================================================================
# Test Anchor Extraction from Source
# ============================================================================


class TestExtractAnchorsFromSource:
    """测试从源码提取锚点引用"""

    def test_extract_contract_anchors(self, tmp_path: Path) -> None:
        """测试提取 contract.md 锚点引用"""
        source = tmp_path / "test_source.py"
        source.write_text(
            """
            message = "See contract.md#52-frozen-step-names for details"
            link = "docs/contract.md#some-anchor"
            """,
            encoding="utf-8",
        )
        anchors = extract_anchors_from_source(source)
        assert ("contract", "52-frozen-step-names") in anchors
        assert ("contract", "some-anchor") in anchors

    def test_extract_maintenance_anchors(self, tmp_path: Path) -> None:
        """测试提取 maintenance.md 锚点引用"""
        source = tmp_path / "test_source.py"
        source.write_text(
            """
            help_link = "maintenance.md#62-冻结-step-rename-标准流程"
            """,
            encoding="utf-8",
        )
        anchors = extract_anchors_from_source(source)
        assert ("maintenance", "62-冻结-step-rename-标准流程") in anchors

    def test_extract_mixed_anchors(self, tmp_path: Path) -> None:
        """测试提取混合锚点引用"""
        source = tmp_path / "test_source.py"
        source.write_text(
            """
            f"See contract.md#55-required_steps-覆盖原则 for coverage"
            f"And maintenance.md#some-flow for the flow"
            """,
            encoding="utf-8",
        )
        anchors = extract_anchors_from_source(source)
        assert ("contract", "55-required_steps-覆盖原则") in anchors
        assert ("maintenance", "some-flow") in anchors

    def test_extract_deduplicates(self, tmp_path: Path) -> None:
        """测试提取结果自动去重"""
        source = tmp_path / "test_source.py"
        source.write_text(
            """
            "contract.md#same-anchor"
            "contract.md#same-anchor"
            "contract.md#same-anchor"
            """,
            encoding="utf-8",
        )
        anchors = extract_anchors_from_source(source)
        # 应该只有一个
        assert anchors.count(("contract", "same-anchor")) == 1

    def test_extract_nonexistent_file(self, tmp_path: Path) -> None:
        """测试不存在的文件返回空列表"""
        source = tmp_path / "nonexistent.py"
        anchors = extract_anchors_from_source(source)
        assert anchors == []

    def test_extract_empty_file(self, tmp_path: Path) -> None:
        """测试空文件返回空列表"""
        source = tmp_path / "empty.py"
        source.write_text("", encoding="utf-8")
        anchors = extract_anchors_from_source(source)
        assert anchors == []


class TestGetRequiredAnchors:
    """测试 get_required_anchors 合并逻辑"""

    def test_merge_extracted_and_explicit(self, tmp_path: Path) -> None:
        """测试合并自动提取和显式锚点"""
        source = tmp_path / "source.py"
        source.write_text(
            '"contract.md#auto-extracted"',
            encoding="utf-8",
        )
        explicit = [("maintenance", "explicit-anchor")]

        result = get_required_anchors(source, explicit)

        assert ("contract", "auto-extracted") in result
        assert ("maintenance", "explicit-anchor") in result

    def test_merge_deduplicates(self, tmp_path: Path) -> None:
        """测试合并时去重"""
        source = tmp_path / "source.py"
        source.write_text(
            '"contract.md#same-anchor"',
            encoding="utf-8",
        )
        explicit = [("contract", "same-anchor")]

        result = get_required_anchors(source, explicit)

        # 应该只有一个
        assert result.count(("contract", "same-anchor")) == 1

    def test_extracted_comes_first(self, tmp_path: Path) -> None:
        """测试自动提取的锚点在前，显式的在后"""
        source = tmp_path / "source.py"
        source.write_text(
            '"contract.md#auto-anchor"',
            encoding="utf-8",
        )
        explicit = [("maintenance", "explicit-anchor")]

        result = get_required_anchors(source, explicit)

        # 顺序：提取的在前
        auto_idx = result.index(("contract", "auto-anchor"))
        explicit_idx = result.index(("maintenance", "explicit-anchor"))
        assert auto_idx < explicit_idx


# ============================================================================
# Test Extract Headings
# ============================================================================


class TestExtractHeadingsWithAnchors:
    """测试标题提取功能"""

    def test_extract_h1_to_h6(self) -> None:
        """测试提取 h1 到 h6 标题"""
        content = """
# Heading 1
## Heading 2
### Heading 3
#### Heading 4
##### Heading 5
###### Heading 6
"""
        anchors = extract_headings_with_anchors(content)
        assert "heading-1" in anchors
        assert "heading-2" in anchors
        assert "heading-3" in anchors
        assert "heading-4" in anchors
        assert "heading-5" in anchors
        assert "heading-6" in anchors

    def test_duplicate_headings_numbered(self) -> None:
        """测试重复标题自动编号"""
        content = """
## Introduction
## Introduction
## Introduction
"""
        anchors = extract_headings_with_anchors(content)
        assert "introduction" in anchors
        assert "introduction-1" in anchors
        assert "introduction-2" in anchors

    def test_chinese_headings(self) -> None:
        """测试中文标题"""
        content = """
## 5.2 Frozen Step Names
## 6.2 冻结 Step Rename 标准流程
"""
        anchors = extract_headings_with_anchors(content)
        assert "52-frozen-step-names" in anchors
        assert "62-冻结-step-rename-标准流程" in anchors

    def test_empty_content(self) -> None:
        """测试空内容"""
        anchors = extract_headings_with_anchors("")
        assert len(anchors) == 0

    def test_no_headings(self) -> None:
        """测试无标题内容"""
        content = "This is just plain text without any headings."
        anchors = extract_headings_with_anchors(content)
        assert len(anchors) == 0


# ============================================================================
# Test Anchor Checker
# ============================================================================


class TestWorkflowContractDocAnchorChecker:
    """测试锚点检查器"""

    @pytest.fixture
    def temp_docs(self, tmp_path: Path) -> tuple[Path, Path]:
        """创建临时文档"""
        contract_md = tmp_path / "contract.md"
        maintenance_md = tmp_path / "maintenance.md"

        contract_content = """
# Contract

## 2. Job ID 与 Job Name 对照表

Some content here.

## 5. 禁止回归的 Step 文本范围

### 5.1 Frozen Job Names

Job names content.

### 5.2 Frozen Step Names

Step names content.

### 5.5 required_steps 覆盖原则

Coverage principles.
"""
        contract_md.write_text(contract_content, encoding="utf-8")

        maintenance_content = """
# Maintenance

## 6. 冻结规则

### 6.2 冻结 Step Rename 标准流程

Rename flow content.
"""
        maintenance_md.write_text(maintenance_content, encoding="utf-8")

        return contract_md, maintenance_md

    @pytest.fixture
    def temp_source_with_anchors(self, tmp_path: Path) -> Path:
        """创建包含锚点引用的临时源文件"""
        source = tmp_path / "validate_workflows.py"
        source.write_text(
            """
            # 测试源文件
            msg1 = "See contract.md#52-frozen-step-names"
            msg2 = "See contract.md#55-required_steps-覆盖原则"
            msg3 = "See contract.md#51-frozen-job-names"
            msg4 = "See contract.md#2-job-id-与-job-name-对照表"
            msg5 = "See maintenance.md#62-冻结-step-rename-标准流程"
            """,
            encoding="utf-8",
        )
        return source

    def test_all_anchors_present_with_extraction(
        self, temp_docs: tuple[Path, Path], temp_source_with_anchors: Path
    ) -> None:
        """测试通过自动提取检测所有锚点"""
        contract_md, maintenance_md = temp_docs

        checker = WorkflowContractDocAnchorChecker(
            contract_doc_path=contract_md,
            maintenance_doc_path=maintenance_md,
            validate_workflows_path=temp_source_with_anchors,
        )
        result = checker.check()

        # 应该检查了 5 个锚点
        assert len(result.checked_anchors) == 5
        assert isinstance(result, AnchorCheckResult)
        # 所有锚点都存在于测试文档中
        assert result.success

    def test_missing_anchor_reports_error(self, tmp_path: Path) -> None:
        """测试缺失锚点报告错误"""
        # 创建缺少必需锚点的文档
        contract_md = tmp_path / "contract.md"
        maintenance_md = tmp_path / "maintenance.md"
        source = tmp_path / "source.py"

        contract_md.write_text("# Empty Contract\n", encoding="utf-8")
        maintenance_md.write_text("# Empty Maintenance\n", encoding="utf-8")
        source.write_text('"contract.md#required-anchor"', encoding="utf-8")

        checker = WorkflowContractDocAnchorChecker(
            contract_doc_path=contract_md,
            maintenance_doc_path=maintenance_md,
            validate_workflows_path=source,
        )
        result = checker.check()

        # 应该有错误（缺失锚点）
        assert not result.success
        assert len(result.errors) > 0
        # 检查错误类型
        anchor_missing_errors = [e for e in result.errors if e.error_type == "anchor_missing"]
        assert len(anchor_missing_errors) > 0

    def test_missing_file_reports_error(self, tmp_path: Path) -> None:
        """测试文件不存在报告错误"""
        contract_md = tmp_path / "nonexistent_contract.md"
        maintenance_md = tmp_path / "nonexistent_maintenance.md"

        # 不提供 validate_workflows_path，使用显式锚点
        checker = WorkflowContractDocAnchorChecker(
            contract_doc_path=contract_md,
            maintenance_doc_path=maintenance_md,
            explicit_anchors=[("contract", "test-anchor")],
        )
        result = checker.check()

        # 应该有文件错误
        assert not result.success
        file_errors = [e for e in result.errors if e.error_type == "file_not_found"]
        assert len(file_errors) == 2

    def test_new_anchor_added_without_modifying_constants(self, tmp_path: Path) -> None:
        """测试新增锚点引用时无需修改脚本常量"""
        # 创建包含新锚点的文档
        contract_md = tmp_path / "contract.md"
        maintenance_md = tmp_path / "maintenance.md"
        source = tmp_path / "source.py"

        contract_md.write_text(
            """
# Contract
## New Feature Section
Some content.
""",
            encoding="utf-8",
        )
        maintenance_md.write_text("# Maintenance\n", encoding="utf-8")

        # 源文件引用新锚点
        source.write_text('"contract.md#new-feature-section"', encoding="utf-8")

        checker = WorkflowContractDocAnchorChecker(
            contract_doc_path=contract_md,
            maintenance_doc_path=maintenance_md,
            validate_workflows_path=source,
        )
        result = checker.check()

        # 新锚点应该被自动提取并验证通过
        assert result.success
        assert ("contract", "new-feature-section") in result.checked_anchors

    def test_explicit_anchors_supplement_extracted(self, tmp_path: Path) -> None:
        """测试显式锚点作为自动提取的补充"""
        contract_md = tmp_path / "contract.md"
        maintenance_md = tmp_path / "maintenance.md"
        source = tmp_path / "source.py"

        contract_md.write_text(
            """
# Contract
## Extracted Anchor
## Explicit Anchor
""",
            encoding="utf-8",
        )
        maintenance_md.write_text("# Maintenance\n", encoding="utf-8")

        # 源文件只引用一个锚点
        source.write_text('"contract.md#extracted-anchor"', encoding="utf-8")

        # 但我们显式添加另一个
        checker = WorkflowContractDocAnchorChecker(
            contract_doc_path=contract_md,
            maintenance_doc_path=maintenance_md,
            validate_workflows_path=source,
            explicit_anchors=[("contract", "explicit-anchor")],
        )
        result = checker.check()

        # 两个锚点都应该被检查
        assert result.success
        assert ("contract", "extracted-anchor") in result.checked_anchors
        assert ("contract", "explicit-anchor") in result.checked_anchors


# ============================================================================
# Integration Test with Real Docs
# ============================================================================


class TestRealDocAnchors:
    """测试真实文档中的锚点"""

    @pytest.fixture
    def project_root(self) -> Path:
        """获取项目根目录"""
        return Path(__file__).resolve().parent.parent.parent

    @pytest.fixture
    def real_docs_paths(self, project_root: Path) -> tuple[Path, Path] | None:
        """获取真实文档路径"""
        contract_md = project_root / "docs/ci_nightly_workflow_refactor/contract.md"
        maintenance_md = project_root / "docs/ci_nightly_workflow_refactor/maintenance.md"

        if contract_md.exists() and maintenance_md.exists():
            return contract_md, maintenance_md
        return None

    @pytest.fixture
    def real_validate_workflows_path(self, project_root: Path) -> Path | None:
        """获取真实 validate_workflows.py 路径"""
        path = project_root / "scripts/ci/validate_workflows.py"
        return path if path.exists() else None

    def test_real_docs_have_all_required_anchors(
        self,
        real_docs_paths: tuple[Path, Path] | None,
        real_validate_workflows_path: Path | None,
    ) -> None:
        """测试真实文档包含所有必需的锚点"""
        if real_docs_paths is None:
            pytest.skip("Real documentation files not found")
        if real_validate_workflows_path is None:
            pytest.skip("Real validate_workflows.py not found")

        contract_md, maintenance_md = real_docs_paths

        checker = WorkflowContractDocAnchorChecker(
            contract_doc_path=contract_md,
            maintenance_doc_path=maintenance_md,
            validate_workflows_path=real_validate_workflows_path,
        )
        result = checker.check()

        # 真实文档应该通过所有检查
        if not result.success:
            # 打印错误详情以便调试
            for error in result.errors:
                print(f"Error: {error.error_type} - {error.doc}#{error.anchor}")
                print(f"  {error.message}")

        assert result.success, f"Found {len(result.errors)} missing anchors"

    def test_auto_extraction_covers_known_anchors(
        self, real_validate_workflows_path: Path | None
    ) -> None:
        """测试自动提取能覆盖现有的 5 个已知锚点"""
        if real_validate_workflows_path is None:
            pytest.skip("Real validate_workflows.py not found")

        # 从真实 validate_workflows.py 提取锚点
        anchors = extract_anchors_from_source(real_validate_workflows_path)

        # 应该能提取到原来硬编码的 5 个锚点
        expected_anchors = [
            ("contract", "52-frozen-step-names"),
            ("contract", "55-required_steps-覆盖原则"),
            ("contract", "51-frozen-job-names"),
            ("contract", "2-job-id-与-job-name-对照表"),
            ("maintenance", "62-冻结-step-rename-标准流程"),
        ]

        for expected in expected_anchors:
            assert expected in anchors, f"Expected anchor {expected} not found in extracted anchors"

    def test_extraction_finds_at_least_five_anchors(
        self, real_validate_workflows_path: Path | None
    ) -> None:
        """测试自动提取至少能找到 5 个锚点"""
        if real_validate_workflows_path is None:
            pytest.skip("Real validate_workflows.py not found")

        anchors = extract_anchors_from_source(real_validate_workflows_path)

        # 至少应该有 5 个（原来硬编码的数量）
        assert len(anchors) >= 5, f"Expected at least 5 anchors, got {len(anchors)}"


# ============================================================================
# Test GitHub Anchor Generation - Special Characters
# ============================================================================


class TestGenerateGithubAnchorSpecialCharacters:
    """测试 GitHub anchor 生成规则 - 特殊字符场景"""

    def test_backticks_removed(self) -> None:
        """测试反引号被移除"""
        assert generate_github_anchor("Code: `example`") == "code-example"
        assert generate_github_anchor("`function_name()` 用法") == "function_name-用法"

    def test_brackets_and_parens_removed(self) -> None:
        """测试括号被移除"""
        assert generate_github_anchor("List [items]") == "list-items"
        assert generate_github_anchor("Method(args)") == "methodargs"
        assert generate_github_anchor("{ braces }") == "braces"

    def test_punctuation_removed(self) -> None:
        """测试标点符号被移除"""
        assert generate_github_anchor("Hello, World!") == "hello-world"
        assert generate_github_anchor("Question? Answer.") == "question-answer"
        assert generate_github_anchor("A: B; C") == "a-b-c"

    def test_html_like_tags_removed(self) -> None:
        """测试 HTML 标签被移除"""
        assert generate_github_anchor("Text <tag> more") == "text-tag-more"
        assert generate_github_anchor("Link: <https://example.com>") == "link-httpsexamplecom"

    def test_ampersand_removed(self) -> None:
        """测试 & 符号被移除"""
        assert generate_github_anchor("A & B") == "a-b"
        assert generate_github_anchor("C&D") == "cd"

    def test_quotes_removed(self) -> None:
        """测试引号被移除"""
        assert generate_github_anchor('Say "Hello"') == "say-hello"
        assert generate_github_anchor("It's fine") == "its-fine"

    def test_complex_mixed_special_chars(self) -> None:
        """测试复杂混合特殊字符"""
        result = generate_github_anchor("5.2.1 `step_name` (冻结) & [重要]")
        assert result == "521-step_name-冻结-重要"

    def test_emoji_removed(self) -> None:
        """测试 emoji 被移除（不在保留范围内）"""
        # Emoji 不在 a-z0-9\u4e00-\u9fff_- 范围内，应被移除
        result = generate_github_anchor("📝 Notes")
        # 注意：emoji 会被移除，但空格转换后的连字符会保留
        assert "notes" in result.lower()

    def test_math_symbols_removed(self) -> None:
        """测试数学符号被移除"""
        assert generate_github_anchor("a + b = c") == "a-b-c"
        assert generate_github_anchor("x * y / z") == "x-y-z"


class TestGenerateGithubAnchorChinese:
    """测试 GitHub anchor 生成规则 - 中文场景"""

    def test_pure_chinese(self) -> None:
        """测试纯中文标题"""
        assert generate_github_anchor("快速开始") == "快速开始"
        assert generate_github_anchor("第一章 简介") == "第一章-简介"

    def test_chinese_with_numbers(self) -> None:
        """测试中文与数字混合"""
        assert generate_github_anchor("步骤 1：安装") == "步骤-1安装"
        assert generate_github_anchor("5.2 冻结规则") == "52-冻结规则"

    def test_chinese_with_english(self) -> None:
        """测试中文与英文混合"""
        assert generate_github_anchor("GitHub 工作流") == "github-工作流"
        assert generate_github_anchor("CI/CD 流水线") == "cicd-流水线"

    def test_chinese_punctuation_removed(self) -> None:
        """测试中文标点被移除"""
        assert generate_github_anchor("问题：答案") == "问题答案"
        assert generate_github_anchor("示例（重要）") == "示例重要"
        assert generate_github_anchor("选项：A、B、C") == "选项abc"


# ============================================================================
# Test Duplicate Heading Disambiguation
# ============================================================================


class TestDuplicateHeadingDisambiguation:
    """测试重复标题的 disambiguation 规则"""

    def test_github_disambiguation_rule(self) -> None:
        """测试 GitHub 风格的 disambiguation：第一个无后缀，后续加 -1, -2..."""
        content = """
# Title
## Section
## Section
## Section
"""
        anchors = extract_headings_with_anchors(content)
        assert "section" in anchors
        assert "section-1" in anchors
        assert "section-2" in anchors
        # 确保第一个没有后缀
        assert anchors["section"] == "Section"
        assert anchors["section-1"] == "Section"
        assert anchors["section-2"] == "Section"

    def test_five_duplicate_headings(self) -> None:
        """测试 5 个重复标题"""
        content = """
## API
## API
## API
## API
## API
"""
        anchors = extract_headings_with_anchors(content)
        assert "api" in anchors
        assert "api-1" in anchors
        assert "api-2" in anchors
        assert "api-3" in anchors
        assert "api-4" in anchors
        # 确保没有 api-5（只有 5 个）
        assert "api-5" not in anchors

    def test_mixed_duplicate_and_unique(self) -> None:
        """测试混合重复和唯一标题"""
        content = """
# Main Title
## Introduction
## Details
## Introduction
## Summary
## Introduction
"""
        anchors = extract_headings_with_anchors(content)
        # 唯一标题
        assert "main-title" in anchors
        assert "details" in anchors
        assert "summary" in anchors
        # 重复标题
        assert "introduction" in anchors  # 第一个
        assert "introduction-1" in anchors  # 第二个
        assert "introduction-2" in anchors  # 第三个

    def test_chinese_duplicate_headings(self) -> None:
        """测试中文重复标题"""
        content = """
## 概述
## 安装
## 概述
## 配置
## 概述
"""
        anchors = extract_headings_with_anchors(content)
        assert "概述" in anchors
        assert "概述-1" in anchors
        assert "概述-2" in anchors
        assert "安装" in anchors
        assert "配置" in anchors


# ============================================================================
# Test Export Anchor List
# ============================================================================


class TestExportAnchorList:
    """测试 anchor 清单导出功能"""

    def test_export_simple_list(self) -> None:
        """测试简单列表导出"""
        content = """
# Title
## Section A
## Section B
"""
        anchors = export_anchor_list(content)
        assert anchors == ["title", "section-a", "section-b"]

    def test_export_with_heading_text(self) -> None:
        """测试包含标题文本的导出"""
        content = """
# Main Title
## 中文标题
"""
        anchors = export_anchor_list(content, include_heading_text=True)
        assert len(anchors) == 2
        assert anchors[0] == {"anchor": "main-title", "heading": "Main Title"}
        assert anchors[1] == {"anchor": "中文标题", "heading": "中文标题"}

    def test_export_preserves_order(self) -> None:
        """测试导出保持文档顺序"""
        content = """
## Third
## First
## Second
"""
        anchors = export_anchor_list(content)
        assert anchors == ["third", "first", "second"]

    def test_export_handles_duplicates(self) -> None:
        """测试导出处理重复标题"""
        content = """
## Item
## Item
## Item
"""
        anchors = export_anchor_list(content)
        assert anchors == ["item", "item-1", "item-2"]


class TestExportDocAnchorsJson:
    """测试文档 anchor JSON 导出功能"""

    def test_export_both_docs(self, tmp_path: Path) -> None:
        """测试同时导出两个文档"""
        contract_md = tmp_path / "contract.md"
        maintenance_md = tmp_path / "maintenance.md"

        contract_md.write_text("# Contract\n## Section A\n", encoding="utf-8")
        maintenance_md.write_text("# Maintenance\n## Section B\n", encoding="utf-8")

        result = export_doc_anchors_json(contract_md, maintenance_md)

        assert "contract" in result
        assert "maintenance" in result
        assert result["contract"]["anchor_count"] == 2
        assert result["maintenance"]["anchor_count"] == 2

    def test_export_missing_file(self, tmp_path: Path) -> None:
        """测试处理缺失文件"""
        contract_md = tmp_path / "contract.md"
        maintenance_md = tmp_path / "nonexistent.md"

        contract_md.write_text("# Contract\n", encoding="utf-8")

        result = export_doc_anchors_json(contract_md, maintenance_md)

        assert result["contract"]["anchor_count"] == 1
        assert result["maintenance"]["anchor_count"] == 0
        assert result["maintenance"]["error"] == "file_not_found"


# ============================================================================
# Integration Test: validate_workflows.py Anchors in Docs
# ============================================================================


class TestValidateWorkflowsAnchorsIntegration:
    """
    集成测试：验证 validate_workflows.py 中引用的所有锚点
    都存在于 contract.md 和 maintenance.md 中
    """

    @pytest.fixture
    def project_root(self) -> Path:
        """获取项目根目录"""
        return Path(__file__).resolve().parent.parent.parent

    def test_all_referenced_anchors_exist_in_docs(self, project_root: Path) -> None:
        """
        测试 validate_workflows.py 中所有引用的锚点都存在于文档中。

        此测试确保：
        1. 从 validate_workflows.py 提取所有 contract.md#xxx 和 maintenance.md#xxx 引用
        2. 验证每个引用的锚点在对应文档中存在
        """
        validate_workflows_path = project_root / "scripts/ci/validate_workflows.py"
        contract_doc_path = project_root / "docs/ci_nightly_workflow_refactor/contract.md"
        maintenance_doc_path = project_root / "docs/ci_nightly_workflow_refactor/maintenance.md"

        # 跳过条件：文件不存在
        if not validate_workflows_path.exists():
            pytest.skip("validate_workflows.py not found")
        if not contract_doc_path.exists():
            pytest.skip("contract.md not found")
        if not maintenance_doc_path.exists():
            pytest.skip("maintenance.md not found")

        # 提取 validate_workflows.py 中的锚点引用
        referenced_anchors = extract_anchors_from_source(validate_workflows_path)
        assert len(referenced_anchors) > 0, "Should find at least one anchor reference"

        # 加载两个文档的锚点
        contract_content = contract_doc_path.read_text(encoding="utf-8")
        maintenance_content = maintenance_doc_path.read_text(encoding="utf-8")

        contract_anchors = set(extract_headings_with_anchors(contract_content).keys())
        maintenance_anchors = set(extract_headings_with_anchors(maintenance_content).keys())

        # 检查每个引用的锚点是否存在
        missing_anchors = []
        for doc_key, anchor in referenced_anchors:
            if doc_key == "contract":
                if anchor not in contract_anchors:
                    missing_anchors.append(f"contract.md#{anchor}")
            elif doc_key == "maintenance":
                if anchor not in maintenance_anchors:
                    missing_anchors.append(f"maintenance.md#{anchor}")

        # 断言：所有引用的锚点都存在
        assert len(missing_anchors) == 0, (
            f"Missing anchors in docs: {missing_anchors}\nReferenced from validate_workflows.py"
        )

    def test_known_critical_anchors_exist(self, project_root: Path) -> None:
        """
        测试已知的关键锚点存在于文档中。

        这些锚点在错误消息中被引用，用于指导用户修复问题。
        """
        contract_doc_path = project_root / "docs/ci_nightly_workflow_refactor/contract.md"
        maintenance_doc_path = project_root / "docs/ci_nightly_workflow_refactor/maintenance.md"

        if not contract_doc_path.exists() or not maintenance_doc_path.exists():
            pytest.skip("Documentation files not found")

        contract_content = contract_doc_path.read_text(encoding="utf-8")
        maintenance_content = maintenance_doc_path.read_text(encoding="utf-8")

        contract_anchors = set(extract_headings_with_anchors(contract_content).keys())
        maintenance_anchors = set(extract_headings_with_anchors(maintenance_content).keys())

        # 关键锚点列表（来自 validate_workflows.py 的错误消息）
        critical_contract_anchors = [
            "52-frozen-step-names",
            "55-required_steps-覆盖原则",
            "51-frozen-job-names",
            "2-job-id-与-job-name-对照表",
        ]
        critical_maintenance_anchors = [
            "62-冻结-step-rename-标准流程",
        ]

        # 检查 contract.md 的关键锚点
        for anchor in critical_contract_anchors:
            assert anchor in contract_anchors, (
                f"Critical anchor '{anchor}' not found in contract.md"
            )

        # 检查 maintenance.md 的关键锚点
        for anchor in critical_maintenance_anchors:
            assert anchor in maintenance_anchors, (
                f"Critical anchor '{anchor}' not found in maintenance.md"
            )


# ============================================================================
# Test Markdown Fixture with Mixed Content
# ============================================================================


class TestMarkdownFixtureWithMixedContent:
    """测试包含多种内容的 Markdown fixture"""

    @pytest.fixture
    def mixed_content_markdown(self) -> str:
        """创建包含特殊字符、中文、重复标题的 Markdown fixture"""
        return """
# 项目文档 (Project Docs)

## 1. 快速开始

这是介绍部分。

## 2. Installation & Setup

### 2.1 `pip install` 方法

使用 pip 安装。

### 2.2 Docker 方法

使用 Docker 安装。

## 3. 配置说明

### 3.1 环境变量 (Environment Variables)

配置环境变量。

### 3.2 配置文件: `config.json`

配置文件说明。

## 4. API 参考

### 4.1 API 端点

API 端点列表。

### 4.1 API 端点

重复的 API 端点（disambiguation 测试）。

### 4.1 API 端点

第三个重复。

## 5. FAQ & Troubleshooting

常见问题。

## 6. 附录

### 6.1 术语表 [Glossary]

术语定义。

### 6.2 变更日志 (Changelog)

变更记录。
"""

    def test_fixture_anchor_generation(self, mixed_content_markdown: str) -> None:
        """测试 fixture 中的 anchor 生成"""
        anchors = extract_headings_with_anchors(mixed_content_markdown)

        # 测试特殊字符处理
        assert "项目文档-project-docs" in anchors  # 括号被移除
        assert "2-installation-setup" in anchors  # & 被移除，连续连字符合并
        assert "21-pip-install-方法" in anchors  # 反引号被移除
        assert "31-环境变量-environment-variables" in anchors
        assert "32-配置文件-configjson" in anchors  # 反引号和冒号被移除

        # 测试重复标题 disambiguation
        assert "41-api-端点" in anchors  # 第一个
        assert "41-api-端点-1" in anchors  # 第二个
        assert "41-api-端点-2" in anchors  # 第三个

        # 测试中文标题
        assert "1-快速开始" in anchors
        assert "3-配置说明" in anchors
        assert "6-附录" in anchors

        # 测试混合内容
        assert "5-faq-troubleshooting" in anchors  # & 被移除，连续连字符合并
        assert "61-术语表-glossary" in anchors  # 方括号被移除
        assert "62-变更日志-changelog" in anchors

    def test_fixture_export_anchor_list(self, mixed_content_markdown: str) -> None:
        """测试 fixture 的 anchor 清单导出"""
        anchors = export_anchor_list(mixed_content_markdown)

        # 验证数量（应该有 16 个标题：1 个 h1 + 6 个 h2 + 9 个 h3，含 3 个重复）
        assert len(anchors) == 16

        # 验证顺序（前几个）
        assert anchors[0] == "项目文档-project-docs"
        assert anchors[1] == "1-快速开始"
        assert anchors[2] == "2-installation-setup"  # 连续连字符被合并

    def test_fixture_export_with_heading_text(self, mixed_content_markdown: str) -> None:
        """测试 fixture 导出包含标题文本"""
        anchors = export_anchor_list(mixed_content_markdown, include_heading_text=True)

        # 验证第一个
        assert anchors[0]["anchor"] == "项目文档-project-docs"
        assert anchors[0]["heading"] == "项目文档 (Project Docs)"

        # 找到重复标题
        api_anchors = [a for a in anchors if "41-api-端点" in a["anchor"]]
        assert len(api_anchors) == 3
        # 所有重复标题的 heading 文本应该相同
        assert all(a["heading"] == "4.1 API 端点" for a in api_anchors)
