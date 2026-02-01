#!/usr/bin/env python3
"""
迭代文档契约测试

覆盖功能:
1. 验证 iteration_regression.template.md 包含必需的 Superseded 关键字和链接格式
2. 验证 iteration_superseded_workflow.md 包含必需的 Superseded 关键字和链接格式
3. 验证 iteration_local_drafts.md 包含必需的 Superseded 关键字和链接格式示例

这些测试确保模板和工作流文档中的示例代码片段符合契约规范。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent.parent

# 待测文档路径
TEMPLATE_PATH = (
    PROJECT_ROOT / "docs" / "acceptance" / "_templates" / "iteration_regression.template.md"
)
WORKFLOW_PATH = PROJECT_ROOT / "docs" / "dev" / "iteration_superseded_workflow.md"
LOCAL_DRAFTS_PATH = PROJECT_ROOT / "docs" / "dev" / "iteration_local_drafts.md"


class TestIterationRegressionTemplate:
    """iteration_regression.template.md 契约测试"""

    @pytest.fixture
    def template_content(self) -> str:
        """读取模板文件内容"""
        assert TEMPLATE_PATH.exists(), f"模板文件不存在: {TEMPLATE_PATH}"
        return TEMPLATE_PATH.read_text(encoding="utf-8")

    def test_template_file_exists(self):
        """测试模板文件存在"""
        assert TEMPLATE_PATH.exists(), f"模板文件不存在: {TEMPLATE_PATH}"

    def test_contains_superseded_by_iteration_keyword(self, template_content: str):
        """测试模板包含 'Superseded by Iteration' 关键字"""
        # 匹配 "Superseded by Iteration" （可能带有 emoji 前缀和编号占位符）
        pattern = r"Superseded by Iteration"
        matches = re.findall(pattern, template_content, re.IGNORECASE)

        assert len(matches) > 0, (
            f"模板文件必须包含 'Superseded by Iteration' 关键字示例。\n文件路径: {TEMPLATE_PATH}"
        )

    def test_contains_iteration_link_format(self, template_content: str):
        """测试模板包含 iteration_ 链接格式示例"""
        # 匹配 iteration_N_regression.md 或 iteration_{K}_regression.md 等格式
        # 支持实际数字、占位符 {N}, {K}, {N-1} 等
        patterns = [
            r"iteration_\d+_regression\.md",  # 实际数字: iteration_9_regression.md
            r"iteration_\{[^}]+\}_regression\.md",  # 占位符: iteration_{K}_regression.md
        ]

        found_any = False
        for pattern in patterns:
            if re.search(pattern, template_content):
                found_any = True
                break

        assert found_any, (
            "模板文件必须包含 iteration_ 链接格式示例 "
            "(如 iteration_N_regression.md 或 iteration_{K}_regression.md)。"
            f"\n文件路径: {TEMPLATE_PATH}"
        )

    def test_contains_superseded_section(self, template_content: str):
        """测试模板包含 Superseded 相关章节"""
        # 验证模板包含 Superseded 章节标题
        superseded_section_patterns = [
            r"#{1,3}\s+.*[Ss]uperseded",  # 标题中包含 Superseded
            r"\*\*.*SUPERSEDED.*\*\*",  # 加粗的 SUPERSEDED
        ]

        found_any = False
        for pattern in superseded_section_patterns:
            if re.search(pattern, template_content):
                found_any = True
                break

        assert found_any, f"模板文件应包含 Superseded 相关章节。\n文件路径: {TEMPLATE_PATH}"


class TestIterationSupersededWorkflow:
    """iteration_superseded_workflow.md 契约测试"""

    @pytest.fixture
    def workflow_content(self) -> str:
        """读取工作流文档内容"""
        assert WORKFLOW_PATH.exists(), f"工作流文档不存在: {WORKFLOW_PATH}"
        return WORKFLOW_PATH.read_text(encoding="utf-8")

    def test_workflow_file_exists(self):
        """测试工作流文档存在"""
        assert WORKFLOW_PATH.exists(), f"工作流文档不存在: {WORKFLOW_PATH}"

    def test_contains_superseded_by_iteration_keyword(self, workflow_content: str):
        """测试工作流文档包含 'Superseded by Iteration' 关键字"""
        # 匹配 "Superseded by Iteration" （可能带有 emoji 前缀和编号）
        pattern = r"Superseded by Iteration"
        matches = re.findall(pattern, workflow_content, re.IGNORECASE)

        assert len(matches) > 0, (
            f"工作流文档必须包含 'Superseded by Iteration' 关键字示例。\n文件路径: {WORKFLOW_PATH}"
        )

    def test_contains_iteration_link_format(self, workflow_content: str):
        """测试工作流文档包含 iteration_ 链接格式示例"""
        # 匹配 iteration_N_regression.md 等格式
        patterns = [
            r"iteration_\d+_regression\.md",  # 实际数字
            r"iteration_[MN]_regression\.md",  # 占位符 M 或 N
        ]

        found_any = False
        for pattern in patterns:
            if re.search(pattern, workflow_content):
                found_any = True
                break

        assert found_any, (
            "工作流文档必须包含 iteration_ 链接格式示例 "
            "(如 iteration_9_regression.md 或 iteration_M_regression.md)。"
            f"\n文件路径: {WORKFLOW_PATH}"
        )

    def test_contains_superseded_status_marker(self, workflow_content: str):
        """测试工作流文档包含 SUPERSEDED 状态标记"""
        # 验证包含 🔄 SUPERSEDED 或类似标记
        superseded_markers = [
            r"🔄\s*SUPERSEDED",
            r"SUPERSEDED",
        ]

        found_any = False
        for pattern in superseded_markers:
            if re.search(pattern, workflow_content):
                found_any = True
                break

        assert found_any, f"工作流文档必须包含 SUPERSEDED 状态标记。\n文件路径: {WORKFLOW_PATH}"

    def test_contains_workflow_steps(self, workflow_content: str):
        """测试工作流文档包含操作步骤"""
        # 验证包含步骤编号（如 "步骤 1.1" 或 "### 步骤"）
        step_patterns = [
            r"步骤\s+\d+",
            r"Step\s+\d+",
        ]

        found_any = False
        for pattern in step_patterns:
            if re.search(pattern, workflow_content, re.IGNORECASE):
                found_any = True
                break

        assert found_any, f"工作流文档应包含操作步骤说明。\n文件路径: {WORKFLOW_PATH}"


class TestIterationLocalDrafts:
    """iteration_local_drafts.md 契约测试

    验证本地草稿管理指南包含必需的 SUPERSEDED 关键字和链接格式示例，
    防止文档被"本地化重写"导致契约丢失。
    """

    @pytest.fixture
    def local_drafts_content(self) -> str:
        """读取本地草稿文档内容"""
        assert LOCAL_DRAFTS_PATH.exists(), f"本地草稿文档不存在: {LOCAL_DRAFTS_PATH}"
        return LOCAL_DRAFTS_PATH.read_text(encoding="utf-8")

    def test_local_drafts_file_exists(self):
        """测试本地草稿文档存在"""
        assert LOCAL_DRAFTS_PATH.exists(), f"本地草稿文档不存在: {LOCAL_DRAFTS_PATH}"

    def test_contains_superseded_keyword(self, local_drafts_content: str):
        """测试文档包含 SUPERSEDED 关键字

        验证文档中包含 SUPERSEDED 状态标记，确保晋升流程中
        有关于如何标记旧迭代为已取代的说明。
        """
        # 匹配 SUPERSEDED 关键字（大写或 emoji 前缀）
        patterns = [
            r"🔄\s*SUPERSEDED",  # emoji 前缀形式
            r"\*\*.*SUPERSEDED.*\*\*",  # 加粗形式
            r"SUPERSEDED",  # 普通形式
        ]

        found_any = False
        for pattern in patterns:
            if re.search(pattern, local_drafts_content):
                found_any = True
                break

        assert found_any, (
            f"本地草稿文档必须包含 SUPERSEDED 关键字示例。\n文件路径: {LOCAL_DRAFTS_PATH}"
        )

    def test_contains_superseded_by_iteration_pattern(self, local_drafts_content: str):
        """测试文档包含 'Superseded by Iteration' 或等效中文表述

        CI 检查依赖此模式识别 SUPERSEDED 声明，文档中必须包含
        此关键短语的示例以确保契约不被意外删除。
        """
        # 匹配 "Superseded by Iteration" 或中文等效表述
        # 中文形式: "被 [Iteration M]... 取代" 或 "已被 Iteration ... 取代"
        patterns = [
            r"Superseded by Iteration",  # 英文标准形式
            r"被\s*\[?Iteration\s+[A-Z0-9]+\]?.*取代",  # 中文形式: 被 Iteration M 取代
            r"已被\s*\[?Iteration",  # 中文简化形式
        ]

        found_any = False
        for pattern in patterns:
            if re.search(pattern, local_drafts_content, re.IGNORECASE):
                found_any = True
                break

        assert found_any, (
            "本地草稿文档必须包含 'Superseded by Iteration' 或等效中文表述示例。\n"
            "CI 检查依赖此模式识别 SUPERSEDED 声明。\n"
            f"文件路径: {LOCAL_DRAFTS_PATH}"
        )

    def test_contains_iteration_regression_link_format(self, local_drafts_content: str):
        """测试文档包含 iteration_*_regression.md 链接格式示例

        验证文档中包含标准的迭代回归文档链接格式，
        如 iteration_M_regression.md 或 iteration_<N>_regression.md。
        """
        # 匹配各种形式的 iteration regression 链接
        patterns = [
            r"iteration_\d+_regression\.md",  # 实际数字: iteration_9_regression.md
            r"iteration_[A-Z]_regression\.md",  # 占位符: iteration_M_regression.md
            r"iteration_<[^>]+>_regression\.md",  # 模板形式: iteration_<N>_regression.md
            r"iteration_\{[^}]+\}_regression\.md",  # 大括号占位符: iteration_{K}_regression.md
        ]

        found_any = False
        for pattern in patterns:
            if re.search(pattern, local_drafts_content):
                found_any = True
                break

        assert found_any, (
            "本地草稿文档必须包含 iteration_*_regression.md 链接格式示例。\n"
            f"文件路径: {LOCAL_DRAFTS_PATH}"
        )

    def test_superseded_example_matches_ci_regex(self, local_drafts_content: str):
        """测试至少一个 SUPERSEDED 示例满足 CI regex 关键短语

        此测试确保文档中的 SUPERSEDED 示例代码块符合 CI 检查的
        正则表达式匹配要求，防止文档被重写后导致 CI 契约失效。
        """
        # CI 检查使用的核心正则模式（与 check_iteration_docs.py 一致）
        ci_patterns = [
            # 头部声明格式: > **🔄 SUPERSEDED**
            r">\s*\*\*🔄\s*SUPERSEDED\*\*",
            # 链接格式: [Iteration M](iteration_M_regression.md)
            r"\[Iteration\s+[A-Z0-9]+\]\(iteration_[A-Za-z0-9_]+_regression\.md\)",
        ]

        matched_patterns = []
        for pattern in ci_patterns:
            if re.search(pattern, local_drafts_content):
                matched_patterns.append(pattern)

        # 至少匹配一个 CI 核心模式
        assert len(matched_patterns) > 0, (
            "本地草稿文档必须包含至少一个符合 CI regex 的 SUPERSEDED 示例。\n"
            "缺少以下模式之一:\n"
            "  - 头部声明: > **🔄 SUPERSEDED**\n"
            "  - 链接格式: [Iteration M](iteration_M_regression.md)\n"
            f"文件路径: {LOCAL_DRAFTS_PATH}"
        )

    def test_contains_r6_format_example(self, local_drafts_content: str):
        """测试文档包含 R6 规范格式的完整示例

        R6 规范定义了 SUPERSEDED 头部声明的标准格式，
        文档中必须包含此格式的示例以指导用户正确操作。
        """
        # R6 规范要求的关键元素
        r6_elements = [
            r"R6\s*规范",  # 提及 R6 规范
            r"头部声明格式",  # 提及头部声明格式
            r">\s*\*\*🔄\s*SUPERSEDED\*\*",  # 实际的格式示例
        ]

        found_elements = []
        for pattern in r6_elements:
            if re.search(pattern, local_drafts_content):
                found_elements.append(pattern)

        # 至少包含 R6 相关提及和实际格式示例
        assert len(found_elements) >= 2, (
            "本地草稿文档应包含 R6 规范格式说明和示例。\n"
            f"找到的元素: {found_elements}\n"
            f"文件路径: {LOCAL_DRAFTS_PATH}"
        )


class TestBothDocumentsConsistency:
    """两个文档的一致性测试"""

    def test_both_documents_exist(self):
        """测试两个文档都存在"""
        assert TEMPLATE_PATH.exists(), f"模板文件不存在: {TEMPLATE_PATH}"
        assert WORKFLOW_PATH.exists(), f"工作流文档不存在: {WORKFLOW_PATH}"

    def test_both_use_consistent_superseded_format(self):
        """测试两个文档使用一致的 Superseded 格式"""
        template_content = TEMPLATE_PATH.read_text(encoding="utf-8")
        workflow_content = WORKFLOW_PATH.read_text(encoding="utf-8")

        # 两个文档都应该包含 "Superseded by Iteration" 格式
        pattern = r"Superseded by Iteration"

        template_has = bool(re.search(pattern, template_content, re.IGNORECASE))
        workflow_has = bool(re.search(pattern, workflow_content, re.IGNORECASE))

        assert template_has and workflow_has, (
            "两个文档应该使用一致的 'Superseded by Iteration' 格式。"
            f"\n模板文件包含: {template_has}"
            f"\n工作流文档包含: {workflow_has}"
        )

    def test_both_reference_iteration_regression_format(self):
        """测试两个文档都引用 iteration_*_regression.md 格式"""
        template_content = TEMPLATE_PATH.read_text(encoding="utf-8")
        workflow_content = WORKFLOW_PATH.read_text(encoding="utf-8")

        # 通用的 iteration regression 链接格式
        pattern = r"iteration_.*_regression\.md"

        template_has = bool(re.search(pattern, template_content))
        workflow_has = bool(re.search(pattern, workflow_content))

        assert template_has and workflow_has, (
            "两个文档应该都引用 iteration_*_regression.md 链接格式。"
            f"\n模板文件包含: {template_has}"
            f"\n工作流文档包含: {workflow_has}"
        )


class TestAllDocumentsConsistency:
    """所有三个文档的一致性测试"""

    def test_all_documents_exist(self):
        """测试三个文档都存在"""
        assert TEMPLATE_PATH.exists(), f"模板文件不存在: {TEMPLATE_PATH}"
        assert WORKFLOW_PATH.exists(), f"工作流文档不存在: {WORKFLOW_PATH}"
        assert LOCAL_DRAFTS_PATH.exists(), f"本地草稿文档不存在: {LOCAL_DRAFTS_PATH}"

    def test_all_reference_iteration_regression_format(self):
        """测试三个文档都引用 iteration_*_regression.md 格式"""
        template_content = TEMPLATE_PATH.read_text(encoding="utf-8")
        workflow_content = WORKFLOW_PATH.read_text(encoding="utf-8")
        local_drafts_content = LOCAL_DRAFTS_PATH.read_text(encoding="utf-8")

        # 通用的 iteration regression 链接格式
        pattern = r"iteration_.*_regression\.md"

        template_has = bool(re.search(pattern, template_content))
        workflow_has = bool(re.search(pattern, workflow_content))
        local_drafts_has = bool(re.search(pattern, local_drafts_content))

        assert template_has and workflow_has and local_drafts_has, (
            "三个文档应该都引用 iteration_*_regression.md 链接格式。"
            f"\n模板文件包含: {template_has}"
            f"\n工作流文档包含: {workflow_has}"
            f"\n本地草稿文档包含: {local_drafts_has}"
        )

    def test_all_contain_superseded_keyword(self):
        """测试三个文档都包含 SUPERSEDED 关键字"""
        template_content = TEMPLATE_PATH.read_text(encoding="utf-8")
        workflow_content = WORKFLOW_PATH.read_text(encoding="utf-8")
        local_drafts_content = LOCAL_DRAFTS_PATH.read_text(encoding="utf-8")

        pattern = r"SUPERSEDED"

        template_has = bool(re.search(pattern, template_content, re.IGNORECASE))
        workflow_has = bool(re.search(pattern, workflow_content, re.IGNORECASE))
        local_drafts_has = bool(re.search(pattern, local_drafts_content, re.IGNORECASE))

        assert template_has and workflow_has and local_drafts_has, (
            "三个文档应该都包含 SUPERSEDED 关键字。"
            f"\n模板文件包含: {template_has}"
            f"\n工作流文档包含: {workflow_has}"
            f"\n本地草稿文档包含: {local_drafts_has}"
        )
