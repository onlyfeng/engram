#!/usr/bin/env python3
"""
迭代文档契约测试

覆盖功能:
1. 验证 iteration_regression.template.md 包含必需的 Superseded 关键字和链接格式
2. 验证 iteration_superseded_workflow.md 包含必需的 Superseded 关键字和链接格式

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
