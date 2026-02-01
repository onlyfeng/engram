#!/usr/bin/env python3
"""
update_min_gate_block_in_regression.py 单元测试

覆盖功能:
1. 生成区块查找（BEGIN/END marker）
2. 内容更新逻辑
3. profile 覆盖功能
4. 边界情况处理
"""

from __future__ import annotations

import sys
from pathlib import Path

# 添加脚本目录到 path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "iteration"))

from render_min_gate_block import render_min_gate_block  # noqa: E402
from update_min_gate_block_in_regression import (  # noqa: E402
    BEGIN_MARKER_PATTERN,
    END_MARKER_PATTERN,
    find_generated_block,
    generate_block_with_markers,
    update_min_gate_block_in_content,
)

# ============================================================================
# Marker 正则表达式测试
# ============================================================================


class TestMarkerPatterns:
    """测试 marker 正则表达式"""

    def test_begin_marker_full_profile(self):
        """测试 BEGIN marker 匹配 full profile"""
        content = "<!-- BEGIN GENERATED: min_gate_block profile=full -->"
        match = BEGIN_MARKER_PATTERN.search(content)
        assert match is not None
        assert match.group(1) == "full"

    def test_begin_marker_regression_profile(self):
        """测试 BEGIN marker 匹配 regression profile"""
        content = "<!-- BEGIN GENERATED: min_gate_block profile=regression -->"
        match = BEGIN_MARKER_PATTERN.search(content)
        assert match is not None
        assert match.group(1) == "regression"

    def test_begin_marker_hyphenated_profile(self):
        """测试 BEGIN marker 匹配带连字符的 profile"""
        content = "<!-- BEGIN GENERATED: min_gate_block profile=docs-only -->"
        match = BEGIN_MARKER_PATTERN.search(content)
        assert match is not None
        assert match.group(1) == "docs-only"

    def test_begin_marker_with_extra_spaces(self):
        """测试 BEGIN marker 允许额外空格"""
        content = "<!--  BEGIN  GENERATED:  min_gate_block  profile=full  -->"
        match = BEGIN_MARKER_PATTERN.search(content)
        assert match is not None
        assert match.group(1) == "full"

    def test_end_marker_basic(self):
        """测试 END marker 基本匹配"""
        content = "<!-- END GENERATED -->"
        match = END_MARKER_PATTERN.search(content)
        assert match is not None

    def test_end_marker_with_extra_spaces(self):
        """测试 END marker 允许额外空格"""
        content = "<!--  END  GENERATED  -->"
        match = END_MARKER_PATTERN.search(content)
        assert match is not None


# ============================================================================
# find_generated_block 测试
# ============================================================================


class TestFindGeneratedBlock:
    """测试 find_generated_block 函数"""

    def test_finds_block_with_full_profile(self):
        """测试查找 full profile 区块"""
        content = """# Header

<!-- BEGIN GENERATED: min_gate_block profile=full -->

Some content here

<!-- END GENERATED -->

More content"""

        result = find_generated_block(content)
        assert result is not None
        begin_pos, end_pos, profile = result
        assert profile == "full"
        assert content[begin_pos:].startswith("<!-- BEGIN GENERATED:")
        assert content[:end_pos].endswith("<!-- END GENERATED -->")

    def test_finds_block_with_regression_profile(self):
        """测试查找 regression profile 区块"""
        content = """<!-- BEGIN GENERATED: min_gate_block profile=regression -->
content
<!-- END GENERATED -->"""

        result = find_generated_block(content)
        assert result is not None
        _, _, profile = result
        assert profile == "regression"

    def test_returns_none_when_no_begin_marker(self):
        """测试无 BEGIN marker 时返回 None"""
        content = """# Header

Some content

<!-- END GENERATED -->"""

        result = find_generated_block(content)
        assert result is None

    def test_returns_none_when_no_end_marker(self):
        """测试无 END marker 时返回 None"""
        content = """# Header

<!-- BEGIN GENERATED: min_gate_block profile=full -->

Some content"""

        result = find_generated_block(content)
        assert result is None

    def test_returns_none_when_end_before_begin(self):
        """测试 END 在 BEGIN 之前时返回 None"""
        content = """<!-- END GENERATED -->

<!-- BEGIN GENERATED: min_gate_block profile=full -->"""

        result = find_generated_block(content)
        # BEGIN 之后没有 END，所以返回 None
        assert result is None


# ============================================================================
# generate_block_with_markers 测试
# ============================================================================


class TestGenerateBlockWithMarkers:
    """测试 generate_block_with_markers 函数"""

    def test_generates_full_profile_block(self):
        """测试生成 full profile 区块"""
        result = generate_block_with_markers(13, "full")

        assert "<!-- BEGIN GENERATED: min_gate_block profile=full -->" in result
        assert "<!-- END GENERATED -->" in result
        assert "Iteration 13" in result

    def test_generates_regression_profile_block(self):
        """测试生成 regression profile 区块"""
        result = generate_block_with_markers(13, "regression")

        assert "<!-- BEGIN GENERATED: min_gate_block profile=regression -->" in result
        assert "<!-- END GENERATED -->" in result
        assert "回归最小集" in result

    def test_generated_content_matches_render_output(self):
        """测试生成的内容与 render_min_gate_block 输出一致"""
        result = generate_block_with_markers(13, "docs-only")
        expected_content = render_min_gate_block(13, "docs-only")

        assert expected_content in result


# ============================================================================
# update_min_gate_block_in_content 测试
# ============================================================================


class TestUpdateMinGateBlockInContent:
    """测试 update_min_gate_block_in_content 函数"""

    def test_updates_existing_block(self):
        """测试更新已存在的区块"""
        original = """# Header

<!-- BEGIN GENERATED: min_gate_block profile=full -->

Old content

<!-- END GENERATED -->

Footer"""

        updated, changed, profile = update_min_gate_block_in_content(original, 13)

        assert changed is True
        assert profile == "full"
        assert "Iteration 13" in updated
        assert "Old content" not in updated
        assert "<!-- BEGIN GENERATED: min_gate_block profile=full -->" in updated
        assert "<!-- END GENERATED -->" in updated
        assert "# Header" in updated
        assert "Footer" in updated

    def test_preserves_surrounding_content(self):
        """测试保留周围内容"""
        original = """# Important Header

Some intro text.

<!-- BEGIN GENERATED: min_gate_block profile=full -->
placeholder
<!-- END GENERATED -->

## Footer Section

More content here."""

        updated, _, _ = update_min_gate_block_in_content(original, 13)

        assert "# Important Header" in updated
        assert "Some intro text." in updated
        assert "## Footer Section" in updated
        assert "More content here." in updated

    def test_respects_profile_from_document(self):
        """测试尊重文档中的 profile 设置"""
        original = """<!-- BEGIN GENERATED: min_gate_block profile=regression -->
old
<!-- END GENERATED -->"""

        updated, _, profile = update_min_gate_block_in_content(original, 13)

        assert profile == "regression"
        assert "回归最小集" in updated

    def test_profile_override_works(self):
        """测试 profile 覆盖功能"""
        original = """<!-- BEGIN GENERATED: min_gate_block profile=full -->
old
<!-- END GENERATED -->"""

        updated, _, profile = update_min_gate_block_in_content(
            original, 13, profile_override="docs-only"
        )

        assert profile == "docs-only"
        assert "文档代理最小门禁" in updated

    def test_no_change_when_content_same(self):
        """测试内容相同时不变更"""
        # 先生成一次
        block = generate_block_with_markers(13, "full")
        original = f"# Header\n\n{block}\n\nFooter"

        updated, changed, _ = update_min_gate_block_in_content(original, 13)

        assert changed is False
        assert updated == original

    def test_returns_original_when_no_block(self):
        """测试无区块时返回原内容"""
        original = "# Header\n\nSome content without markers"

        updated, changed, _ = update_min_gate_block_in_content(original, 13)

        assert changed is False
        assert updated == original

    def test_invalid_profile_defaults_to_full(self):
        """测试无效 profile 默认使用 full"""
        # 手动构造一个无效 profile 的内容
        original = """<!-- BEGIN GENERATED: min_gate_block profile=invalid -->
old
<!-- END GENERATED -->"""

        updated, _, profile = update_min_gate_block_in_content(original, 13)

        assert profile == "full"
        assert "完整 CI 门禁" in updated


# ============================================================================
# 集成测试（使用临时文件）
# ============================================================================


class TestIntegration:
    """集成测试"""

    def test_full_workflow_with_temp_file(self, tmp_path: Path):
        """测试完整工作流（使用临时文件）"""
        # 创建临时回归文档
        doc_content = """# Iteration 99 Regression

## 执行信息

| 项目 | 值 |
|------|-----|
| **执行日期** | 2026-02-02 |

## 最小门禁命令块

<!-- BEGIN GENERATED: min_gate_block profile=full -->

（占位内容）

<!-- END GENERATED -->

## 后续内容

更多文档内容...
"""
        doc_path = tmp_path / "iteration_99_regression.md"
        doc_path.write_text(doc_content, encoding="utf-8")

        # 读取并更新
        content = doc_path.read_text(encoding="utf-8")
        updated, changed, profile = update_min_gate_block_in_content(content, 99)

        assert changed is True
        assert profile == "full"

        # 验证更新后的内容包含正确的生成内容
        expected_render = render_min_gate_block(99, "full")
        assert expected_render in updated

        # 写入并重新读取验证
        doc_path.write_text(updated, encoding="utf-8")
        reread = doc_path.read_text(encoding="utf-8")

        assert "# Iteration 99 Regression" in reread
        assert "Iteration 99" in reread
        assert "## 后续内容" in reread
        assert "<!-- BEGIN GENERATED: min_gate_block profile=full -->" in reread
        assert "<!-- END GENERATED -->" in reread

    def test_idempotent_updates(self, tmp_path: Path):
        """测试更新幂等性"""
        doc_content = """# Test

<!-- BEGIN GENERATED: min_gate_block profile=regression -->
old
<!-- END GENERATED -->
"""
        doc_path = tmp_path / "test.md"
        doc_path.write_text(doc_content, encoding="utf-8")

        # 第一次更新
        content = doc_path.read_text(encoding="utf-8")
        updated1, changed1, _ = update_min_gate_block_in_content(content, 13)
        doc_path.write_text(updated1, encoding="utf-8")

        # 第二次更新（应该无变化）
        content2 = doc_path.read_text(encoding="utf-8")
        updated2, changed2, _ = update_min_gate_block_in_content(content2, 13)

        assert changed1 is True
        assert changed2 is False
        assert updated1 == updated2


# ============================================================================
# 边界情况测试
# ============================================================================


class TestEdgeCases:
    """边界情况测试"""

    def test_empty_content_between_markers(self):
        """测试 marker 之间无内容"""
        original = """<!-- BEGIN GENERATED: min_gate_block profile=full --><!-- END GENERATED -->"""

        updated, changed, _ = update_min_gate_block_in_content(original, 13)

        assert changed is True
        assert "Iteration 13" in updated

    def test_multiline_content_between_markers(self):
        """测试 marker 之间多行内容"""
        original = """<!-- BEGIN GENERATED: min_gate_block profile=full -->
Line 1
Line 2
Line 3

| Table | Row |
|-------|-----|
| A | B |

```bash
echo "code"
```
<!-- END GENERATED -->"""

        updated, changed, _ = update_min_gate_block_in_content(original, 13)

        assert changed is True
        assert "Line 1" not in updated
        assert 'echo "code"' not in updated

    def test_all_supported_profiles(self):
        """测试所有支持的 profile"""
        from update_min_gate_block_in_regression import SUPPORTED_PROFILES

        for profile in SUPPORTED_PROFILES:
            original = f"""<!-- BEGIN GENERATED: min_gate_block profile={profile} -->
old
<!-- END GENERATED -->"""

            updated, changed, result_profile = update_min_gate_block_in_content(original, 13)

            assert changed is True
            assert result_profile == profile
            assert f"profile={profile}" in updated
