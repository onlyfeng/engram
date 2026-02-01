"""
tests/test_mypy_baseline_policy.py

测试 mypy baseline 变更策略检查脚本的核心功能:
1. diff 解析与统计
2. GitHub 事件解析
3. 策略检查规则
4. 退出码与摘要输出

使用 fixtures/mypy_baseline_policy/ 作为测试数据源。
"""

from __future__ import annotations

import json
import os
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.ci.check_mypy_baseline_policy import (
    ALLOWED_LABELS,
    REQUIRED_PR_SECTION,
    THRESHOLD_REQUIRE_EXPLANATION,
    THRESHOLD_REQUIRE_LABELS,
    THRESHOLD_STRICT_REVIEW,
    PRContext,
    check_issue_reference,
    check_labels,
    check_policy,
    check_pr_section,
    extract_pr_context_from_event,
    parse_diff,
)

# ============================================================================
# Fixtures
# ============================================================================

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "mypy_baseline_policy"


@pytest.fixture
def diff_net_increase_3() -> str:
    """净增 3 个错误的 diff."""
    return (FIXTURES_DIR / "diff_net_increase_3.txt").read_text(encoding="utf-8")


@pytest.fixture
def diff_net_increase_7() -> str:
    """净增 7 个错误的 diff (> 5, 需要 labels)."""
    return (FIXTURES_DIR / "diff_net_increase_7.txt").read_text(encoding="utf-8")


@pytest.fixture
def diff_net_increase_12() -> str:
    """净增 12 个错误的 diff (> 10, 严格审核)."""
    return (FIXTURES_DIR / "diff_net_increase_12.txt").read_text(encoding="utf-8")


@pytest.fixture
def diff_net_decrease() -> str:
    """净减少 3 个错误的 diff."""
    return (FIXTURES_DIR / "diff_net_decrease.txt").read_text(encoding="utf-8")


@pytest.fixture
def diff_no_change() -> str:
    """无变化的 diff."""
    return (FIXTURES_DIR / "diff_no_change.txt").read_text(encoding="utf-8")


@pytest.fixture
def event_pr_with_section() -> dict:
    """包含 baseline section 和 issue 的 PR 事件."""
    with open(FIXTURES_DIR / "github_event_pr_with_section.json", "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def event_pr_no_section() -> dict:
    """不包含 baseline section 的 PR 事件."""
    with open(FIXTURES_DIR / "github_event_pr_no_section.json", "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def event_pr_with_section_no_issue() -> dict:
    """包含 baseline section 但无 issue 的 PR 事件."""
    with open(
        FIXTURES_DIR / "github_event_pr_with_section_no_issue.json", "r", encoding="utf-8"
    ) as f:
        return json.load(f)


# ============================================================================
# Diff 解析测试
# ============================================================================


class TestParseDiff:
    """测试 parse_diff 函数."""

    def test_net_increase_3(self, diff_net_increase_3: str) -> None:
        """应正确解析净增 3 个错误的 diff."""
        result = parse_diff(diff_net_increase_3)

        assert result.added_lines == 3
        assert result.removed_lines == 0
        assert result.net_change == 3
        assert len(result.added_errors) == 3
        assert len(result.removed_errors) == 0

    def test_net_increase_7(self, diff_net_increase_7: str) -> None:
        """应正确解析净增 7 个错误的 diff (8 added - 1 removed)."""
        result = parse_diff(diff_net_increase_7)

        assert result.added_lines == 8
        assert result.removed_lines == 1
        assert result.net_change == 7

    def test_net_decrease(self, diff_net_decrease: str) -> None:
        """应正确解析净减少错误的 diff."""
        result = parse_diff(diff_net_decrease)

        assert result.removed_lines == 3
        assert result.added_lines == 0
        assert result.net_change == -3

    def test_no_change(self, diff_no_change: str) -> None:
        """应正确处理空 diff."""
        result = parse_diff(diff_no_change)

        assert result.added_lines == 0
        assert result.removed_lines == 0
        assert result.net_change == 0

    def test_skips_diff_headers(self) -> None:
        """应跳过 diff header 行."""
        diff_text = """diff --git a/file.txt b/file.txt
index abc123..def456 100644
--- a/file.txt
+++ b/file.txt
@@ -1,3 +1,4 @@
 unchanged line
+new error line
"""
        result = parse_diff(diff_text)

        assert result.added_lines == 1
        assert result.removed_lines == 0


class TestParseDiffEdgeCases:
    """测试 parse_diff 的边界情况."""

    def test_empty_added_lines_ignored(self) -> None:
        """空的新增行应被忽略."""
        diff_text = """+
+
+actual error line
"""
        result = parse_diff(diff_text)

        assert result.added_lines == 1
        assert result.added_errors == ["actual error line"]

    def test_mixed_changes(self) -> None:
        """应正确处理混合的新增和删除."""
        diff_text = """+new error 1
-old error 1
+new error 2
-old error 2
+new error 3
"""
        result = parse_diff(diff_text)

        assert result.added_lines == 3
        assert result.removed_lines == 2
        assert result.net_change == 1


# ============================================================================
# GitHub 事件解析测试
# ============================================================================


class TestExtractPRContext:
    """测试 extract_pr_context_from_event 函数."""

    def test_extracts_body(self, event_pr_with_section: dict) -> None:
        """应正确提取 PR body."""
        context = extract_pr_context_from_event(event_pr_with_section)

        assert REQUIRED_PR_SECTION in context.body
        assert "#456" in context.body

    def test_extracts_labels(self, event_pr_with_section: dict) -> None:
        """应正确提取 PR labels."""
        context = extract_pr_context_from_event(event_pr_with_section)

        assert "enhancement" in context.labels
        assert "tech-debt" in context.labels

    def test_extracts_refs(self, event_pr_with_section: dict) -> None:
        """应正确提取 base/head ref."""
        context = extract_pr_context_from_event(event_pr_with_section)

        assert context.base_ref == "main"
        assert context.head_ref == "feature/new-feature"

    def test_handles_empty_body(self) -> None:
        """应处理空 body."""
        event = {"pull_request": {"body": None, "labels": [], "base": {}, "head": {}}}
        context = extract_pr_context_from_event(event)

        assert context.body == ""

    def test_handles_no_labels(self) -> None:
        """应处理无 labels 的情况."""
        event = {"pull_request": {"body": "test", "labels": [], "base": {}, "head": {}}}
        context = extract_pr_context_from_event(event)

        assert context.labels == set()


# ============================================================================
# 策略检查辅助函数测试
# ============================================================================


class TestCheckHelpers:
    """测试策略检查辅助函数."""

    def test_check_pr_section_present(self) -> None:
        """body 包含 section 时应返回 True."""
        body = f"## Summary\n\n{REQUIRED_PR_SECTION}\n\n内容"
        assert check_pr_section(body) is True

    def test_check_pr_section_absent(self) -> None:
        """body 不包含 section 时应返回 False."""
        body = "## Summary\n\n内容"
        assert check_pr_section(body) is False

    def test_check_issue_reference_with_hash(self) -> None:
        """body 包含 #数字 时应返回 True."""
        body = "关联 Issue: #123"
        assert check_issue_reference(body) is True

    def test_check_issue_reference_with_url(self) -> None:
        """body 包含 GitHub issue URL 时应返回 True."""
        body = "关联: https://github.com/org/repo/issues/456"
        assert check_issue_reference(body) is True

    def test_check_issue_reference_absent(self) -> None:
        """body 不包含 issue 引用时应返回 False."""
        body = "关联 Issue: (待填写)"
        assert check_issue_reference(body) is False

    def test_check_labels_with_tech_debt(self) -> None:
        """labels 包含 tech-debt 时应返回 True."""
        labels = {"enhancement", "tech-debt"}
        assert check_labels(labels) is True

    def test_check_labels_with_type_coverage(self) -> None:
        """labels 包含 type-coverage 时应返回 True."""
        labels = {"bug", "type-coverage"}
        assert check_labels(labels) is True

    def test_check_labels_without_allowed(self) -> None:
        """labels 不包含允许的标签时应返回 False."""
        labels = {"bug", "enhancement"}
        assert check_labels(labels) is False


# ============================================================================
# 策略检查主逻辑测试
# ============================================================================


class TestCheckPolicy:
    """测试 check_policy 函数."""

    def test_net_decrease_passes(self, diff_net_decrease: str) -> None:
        """净减少应直接通过."""
        diff = parse_diff(diff_net_decrease)
        result = check_policy(diff, None)

        assert result.passed is True
        assert any("净减少" in msg or "感谢修复" in msg for msg in result.messages)

    def test_no_change_passes(self, diff_no_change: str) -> None:
        """无变化应直接通过."""
        diff = parse_diff(diff_no_change)
        result = check_policy(diff, None)

        assert result.passed is True

    def test_net_increase_without_pr_context_passes_with_warning(
        self, diff_net_increase_3: str
    ) -> None:
        """净增但无 PR 上下文时应通过但有警告."""
        diff = parse_diff(diff_net_increase_3)
        result = check_policy(diff, None)

        assert result.passed is True
        assert len(result.warnings) > 0

    def test_net_increase_with_valid_pr_passes(
        self,
        diff_net_increase_3: str,
        event_pr_with_section: dict,
    ) -> None:
        """净增但 PR 有完整说明时应通过."""
        diff = parse_diff(diff_net_increase_3)
        context = extract_pr_context_from_event(event_pr_with_section)
        result = check_policy(diff, context)

        assert result.passed is True

    def test_net_increase_without_section_fails(
        self,
        diff_net_increase_3: str,
        event_pr_no_section: dict,
    ) -> None:
        """净增但 PR 无说明 section 时应失败."""
        diff = parse_diff(diff_net_increase_3)
        context = extract_pr_context_from_event(event_pr_no_section)
        result = check_policy(diff, context)

        assert result.passed is False
        assert any(REQUIRED_PR_SECTION in msg for msg in result.messages)

    def test_net_increase_without_issue_fails(
        self,
        diff_net_increase_3: str,
        event_pr_with_section_no_issue: dict,
    ) -> None:
        """净增但 PR 无 issue 引用时应失败."""
        diff = parse_diff(diff_net_increase_3)
        context = extract_pr_context_from_event(event_pr_with_section_no_issue)
        result = check_policy(diff, context)

        assert result.passed is False
        assert any("Issue" in msg for msg in result.messages)


class TestCheckPolicyThresholds:
    """测试不同阈值的策略检查."""

    def test_net_increase_above_5_needs_labels(self, diff_net_increase_7: str) -> None:
        """净增 > 5 时需要特定 labels."""
        diff = parse_diff(diff_net_increase_7)
        # PR 有 section 和 issue 但无正确 labels
        context = PRContext(
            body=f"{REQUIRED_PR_SECTION}\n关联 Issue: #123",
            labels={"enhancement"},  # 无 tech-debt 或 type-coverage
            base_ref="main",
            head_ref="feature",
        )
        result = check_policy(diff, context)

        assert result.passed is False
        assert any("标签" in msg or "label" in msg.lower() for msg in result.messages)

    def test_net_increase_above_5_with_labels_passes(self, diff_net_increase_7: str) -> None:
        """净增 > 5 但有正确 labels 时应通过."""
        diff = parse_diff(diff_net_increase_7)
        context = PRContext(
            body=f"{REQUIRED_PR_SECTION}\n关联 Issue: #123",
            labels={"enhancement", "tech-debt"},
            base_ref="main",
            head_ref="feature",
        )
        result = check_policy(diff, context)

        assert result.passed is True

    def test_net_increase_above_10_has_warnings(self, diff_net_increase_12: str) -> None:
        """净增 > 10 时应有严格警告."""
        diff = parse_diff(diff_net_increase_12)
        context = PRContext(
            body=f"{REQUIRED_PR_SECTION}\n关联 Issue: #123",
            labels={"tech-debt"},
            base_ref="main",
            head_ref="feature",
        )
        result = check_policy(diff, context)

        # 应通过但有警告
        assert result.passed is True
        assert any("严重警告" in w or "额外审批" in w for w in result.warnings)
        assert any("拆分" in msg or "审批" in msg for msg in result.messages)


# ============================================================================
# main() 函数集成测试
# ============================================================================


class TestMainFunction:
    """测试 main() 函数的退出码和输出."""

    def test_main_with_net_decrease_returns_zero(self, tmp_path: Path) -> None:
        """净减少时应返回 0."""
        from check_mypy_baseline_policy import main

        diff_file = tmp_path / "diff.txt"
        diff_file.write_text(
            """-removed error 1
-removed error 2
""",
            encoding="utf-8",
        )

        with patch(
            "sys.argv",
            ["check_mypy_baseline_policy.py", "--diff-file", str(diff_file)],
        ):
            captured = StringIO()
            with patch("sys.stdout", captured):
                exit_code = main()

        assert exit_code == 0

    def test_main_with_net_increase_no_pr_returns_zero(self, tmp_path: Path) -> None:
        """净增但非 PR 环境时应返回 0（仅警告）."""
        from check_mypy_baseline_policy import main

        diff_file = tmp_path / "diff.txt"
        diff_file.write_text(
            """+new error 1
+new error 2
""",
            encoding="utf-8",
        )

        # 清除 PR 相关环境变量
        env_backup = {k: os.environ.pop(k, None) for k in ["GITHUB_EVENT_PATH", "GITHUB_BASE_REF"]}
        try:
            with patch(
                "sys.argv",
                ["check_mypy_baseline_policy.py", "--diff-file", str(diff_file)],
            ):
                captured = StringIO()
                with patch("sys.stdout", captured):
                    exit_code = main()

            assert exit_code == 0
        finally:
            # 恢复环境变量
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v

    def test_main_with_pr_body_and_labels_args(self, tmp_path: Path) -> None:
        """使用 --pr-body 和 --pr-labels 参数时应正确检查."""
        from check_mypy_baseline_policy import main

        diff_file = tmp_path / "diff.txt"
        diff_file.write_text(
            """+new error 1
+new error 2
+new error 3
""",
            encoding="utf-8",
        )

        # 完整的 PR body 和 labels
        pr_body = f"{REQUIRED_PR_SECTION}\n关联 Issue: #123"

        with patch(
            "sys.argv",
            [
                "check_mypy_baseline_policy.py",
                "--diff-file",
                str(diff_file),
                "--pr-body",
                pr_body,
                "--pr-labels",
                "enhancement",
            ],
        ):
            captured = StringIO()
            with patch("sys.stdout", captured):
                exit_code = main()

        assert exit_code == 0

    def test_main_fails_without_section(self, tmp_path: Path) -> None:
        """PR body 无 section 时应返回 1."""
        from check_mypy_baseline_policy import main

        diff_file = tmp_path / "diff.txt"
        diff_file.write_text(
            """+new error 1
""",
            encoding="utf-8",
        )

        with patch(
            "sys.argv",
            [
                "check_mypy_baseline_policy.py",
                "--diff-file",
                str(diff_file),
                "--pr-body",
                "没有 section 的 PR body",
                "--pr-labels",
                "enhancement",
            ],
        ):
            captured = StringIO()
            with patch("sys.stdout", captured):
                exit_code = main()

        assert exit_code == 1
        assert "FAIL" in captured.getvalue()


# ============================================================================
# 常量验证测试
# ============================================================================


class TestConstants:
    """验证常量配置."""

    def test_thresholds_are_ordered(self) -> None:
        """阈值应按从小到大排序."""
        assert THRESHOLD_REQUIRE_EXPLANATION <= THRESHOLD_REQUIRE_LABELS
        assert THRESHOLD_REQUIRE_LABELS <= THRESHOLD_STRICT_REVIEW

    def test_allowed_labels_not_empty(self) -> None:
        """允许的 labels 不应为空."""
        assert len(ALLOWED_LABELS) > 0

    def test_required_section_is_markdown_header(self) -> None:
        """必需的 section 应是 markdown header."""
        assert REQUIRED_PR_SECTION.startswith("#")


# 需要导入 os 模块
