#!/usr/bin/env python3
"""
sync_iteration_regression.py 单元测试

覆盖功能:
1. 区块查找（min_gate_block / evidence_snippet）
2. 区块内容生成
3. 区块插入位置计算
4. 同步逻辑（插入 / 更新 / 幂等性）
5. 边界情况处理
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

# 添加脚本目录到 path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "iteration"))

import generated_blocks as blocks  # noqa: E402
from generated_blocks import (  # noqa: E402
    find_evidence_block,
    find_evidence_insert_position,
    find_min_gate_block,
    find_min_gate_insert_position,
    generate_evidence_block_with_markers,
    generate_evidence_placeholder,
    generate_min_gate_block_with_markers,
    render_evidence_snippet,
)
from iteration_evidence_schema import CURRENT_SCHEMA_REF  # noqa: E402
from sync_iteration_regression import (  # noqa: E402
    sync_evidence_block,
    sync_min_gate_block,
)
from sync_iteration_regression import (  # noqa: E402
    sync_iteration_regression as sync_func,
)

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _normalize_snapshot(content: str) -> str:
    return content.replace("\r\n", "\n").strip()


# ============================================================================
# 最小门禁命令块查找测试
# ============================================================================


class TestFindMinGateBlock:
    """测试 find_min_gate_block 函数"""

    def test_finds_block_with_full_profile(self):
        """测试查找 full profile 区块"""
        content = """# Header

<!-- BEGIN GENERATED: min_gate_block profile=full -->

Some content

<!-- END GENERATED: min_gate_block -->

Footer"""
        block = find_min_gate_block(content)

        assert block is not None
        assert block.block_type == "min_gate_block"
        assert block.profile == "full"
        assert content[block.begin_pos :].startswith("<!-- BEGIN GENERATED:")
        assert content[: block.end_pos].endswith("<!-- END GENERATED: min_gate_block -->")

    def test_finds_block_with_regression_profile(self):
        """测试查找 regression profile 区块"""
        content = """<!-- BEGIN GENERATED: min_gate_block profile=regression -->
content
<!-- END GENERATED: min_gate_block -->"""

        block = find_min_gate_block(content)
        assert block is not None
        assert block.profile == "regression"

    def test_finds_block_with_legacy_end_marker(self):
        """测试兼容旧格式 END marker"""
        content = """<!-- BEGIN GENERATED: min_gate_block profile=full -->
content
<!-- END GENERATED -->"""

        block = find_min_gate_block(content)
        assert block is not None
        assert block.profile == "full"

    def test_returns_none_when_no_begin_marker(self):
        """测试无 BEGIN marker 时返回 None"""
        content = "# No markers here"
        assert find_min_gate_block(content) is None

    def test_returns_none_when_no_end_marker(self):
        """测试无 END marker 时返回 None"""
        content = """<!-- BEGIN GENERATED: min_gate_block profile=full -->
content without end"""
        assert find_min_gate_block(content) is None


# ============================================================================
# 验收证据片段查找测试
# ============================================================================


class TestFindEvidenceBlock:
    """测试 find_evidence_block 函数"""

    def test_finds_block_with_new_markers(self):
        """测试新格式 marker"""
        content = """<!-- BEGIN GENERATED: evidence_snippet -->

## 验收证据

| 项目 | 值 |
|------|-----|
| **记录时间** | 2026-02-02 |

<!-- END GENERATED: evidence_snippet -->"""

        block = find_evidence_block(content)
        assert block is not None
        assert block.block_type == "evidence_snippet"

    def test_finds_block_with_legacy_markers(self):
        """测试兼容旧格式 marker"""
        content = """<!-- AUTO-GENERATED EVIDENCE BLOCK START -->
content
<!-- AUTO-GENERATED EVIDENCE BLOCK END -->"""

        block = find_evidence_block(content)
        assert block is not None

    def test_returns_none_when_no_markers(self):
        """测试无 marker 时返回 None"""
        content = "# No evidence block here"
        assert find_evidence_block(content) is None


# ============================================================================
# 区块内容生成测试
# ============================================================================


class TestGenerateMinGateBlock:
    """测试 min_gate_block 生成"""

    def test_generates_with_correct_markers(self):
        """测试生成正确的 marker"""
        result = generate_min_gate_block_with_markers(13, "full")

        assert "<!-- BEGIN GENERATED: min_gate_block profile=full -->" in result
        assert "<!-- END GENERATED: min_gate_block -->" in result
        assert "Iteration 13" in result

    def test_generates_regression_profile(self):
        """测试生成 regression profile"""
        result = generate_min_gate_block_with_markers(13, "regression")

        assert "profile=regression" in result
        assert "回归最小集" in result


class TestRenderEvidenceSnippet:
    """测试 evidence snippet 渲染"""

    def test_renders_basic_evidence(self):
        """测试渲染基本证据"""
        evidence = {
            "iteration_number": 13,
            "recorded_at": "2026-02-02T14:30:00Z",
            "commit_sha": "abc1234def5678",
            "overall_result": "PASS",
            "commands": [
                {"name": "ci", "command": "make ci", "result": "PASS"},
            ],
        }

        result = render_evidence_snippet(evidence)

        assert "## 验收证据" in result
        assert "iteration_13_evidence.json" in result
        assert "2026-02-02T14:30:00Z" in result
        assert "`abc1234`" in result
        assert "✅ PASS" in result

    def test_renders_failed_commands(self):
        """测试渲染失败命令"""
        evidence = {
            "iteration_number": 13,
            "recorded_at": "2026-02-02T14:30:00Z",
            "commit_sha": "abc1234",
            "overall_result": "FAIL",
            "commands": [
                {"name": "ci", "command": "make ci", "result": "FAIL"},
            ],
        }

        result = render_evidence_snippet(evidence)
        assert "❌ FAIL" in result

    def test_renders_with_ci_url(self):
        """测试渲染 CI URL"""
        evidence = {
            "iteration_number": 13,
            "recorded_at": "2026-02-02T14:30:00Z",
            "commit_sha": "abc1234",
            "overall_result": "PASS",
            "commands": [],
            "links": {
                "ci_run_url": "https://github.com/org/repo/actions/runs/123",
            },
        }

        result = render_evidence_snippet(evidence)
        assert "https://github.com/org/repo/actions/runs/123" in result

    def test_renders_with_notes(self):
        """测试渲染备注"""
        evidence = {
            "iteration_number": 13,
            "recorded_at": "2026-02-02T14:30:00Z",
            "commit_sha": "abc1234",
            "overall_result": "PASS",
            "commands": [],
            "notes": "所有门禁通过",
        }

        result = render_evidence_snippet(evidence)
        assert "所有门禁通过" in result

    def test_renders_with_duration(self):
        """测试渲染执行时间"""
        evidence = {
            "iteration_number": 13,
            "recorded_at": "2026-02-02T14:30:00Z",
            "commit_sha": "abc1234",
            "overall_result": "PASS",
            "commands": [
                {
                    "name": "ci",
                    "command": "make ci",
                    "result": "PASS",
                    "duration_seconds": 45.5,
                },
            ],
        }

        result = render_evidence_snippet(evidence)
        assert "45.5s" in result

    def test_renders_v2_snapshot(self):
        """测试 v2 evidence snippet 快照"""
        evidence_path = FIXTURES_DIR / "iteration_evidence_v2_minimal.json"
        snapshot_path = FIXTURES_DIR / "evidence_snippet_v2_snapshot.md"
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))

        result = render_evidence_snippet(evidence)
        expected = snapshot_path.read_text(encoding="utf-8")

        assert _normalize_snapshot(result) == _normalize_snapshot(expected), (
            "evidence snippet 快照不一致。"
            "如需更新快照，请运行: "
            "python scripts/iteration/update_iteration_fixtures.py --evidence-snippet"
        )


class TestGenerateEvidenceBlock:
    """测试 evidence block 生成"""

    def test_generates_with_correct_markers(self):
        """测试生成正确的 marker"""
        evidence = {
            "iteration_number": 13,
            "recorded_at": "2026-02-02T14:30:00Z",
            "commit_sha": "abc1234",
            "overall_result": "PASS",
            "commands": [],
        }

        result = generate_evidence_block_with_markers(evidence)

        assert "<!-- BEGIN GENERATED: evidence_snippet -->" in result
        assert "<!-- END GENERATED: evidence_snippet -->" in result

    def test_placeholder_has_correct_markers(self):
        """测试占位符有正确的 marker"""
        result = generate_evidence_placeholder()

        assert "<!-- BEGIN GENERATED: evidence_snippet -->" in result
        assert "<!-- END GENERATED: evidence_snippet -->" in result
        assert "证据文件尚未生成" in result


# ============================================================================
# 插入位置测试
# ============================================================================


class TestFindMinGateInsertPosition:
    """测试 min_gate_block 插入位置"""

    def test_inserts_after_exec_info(self):
        """测试在执行信息后插入"""
        content = """# Title

## 执行信息

| 项目 | 值 |
|------|-----|
| 日期 | 2026-02-02 |

## 执行结果总览

| 序号 | 测试 |
"""
        pos = find_min_gate_insert_position(content)

        # 应该在执行信息 section 结束后，执行结果总览之前
        assert content[pos:].startswith("## 执行结果总览")

    def test_inserts_before_result_overview(self):
        """测试在执行结果总览前插入（无执行信息时）"""
        content = """# Title

## 执行结果总览

内容
"""
        pos = find_min_gate_insert_position(content)
        assert content[pos:].startswith("## 执行结果总览")


class TestFindEvidenceInsertPosition:
    """测试 evidence_snippet 插入位置"""

    def test_inserts_at_existing_evidence_section(self):
        """测试在已有验收证据 section 位置插入"""
        content = """# Title

## 验收证据

旧内容

## 相关文档

链接
"""
        pos = find_evidence_insert_position(content)
        assert content[pos:].startswith("## 验收证据")

    def test_inserts_before_related_docs(self):
        """测试在相关文档前插入（无验收证据时）"""
        content = """# Title

## 执行结果

结果

## 相关文档

链接
"""
        pos = find_evidence_insert_position(content)
        assert content[pos:].startswith("## 相关文档")

    def test_inserts_at_end_when_no_markers(self):
        """测试无标记时在文件末尾插入"""
        content = """# Title

## Content

内容
"""
        pos = find_evidence_insert_position(content)
        assert pos == len(content)


# ============================================================================
# 同步逻辑测试
# ============================================================================


class TestSyncMinGateBlock:
    """测试 sync_min_gate_block 函数"""

    def test_updates_existing_block(self):
        """测试更新已存在的区块"""
        content = """# Header

<!-- BEGIN GENERATED: min_gate_block profile=full -->
Old content
<!-- END GENERATED: min_gate_block -->

Footer"""

        updated, changed, inserted = sync_min_gate_block(content, 13, "full")

        assert changed is True
        assert inserted is False
        assert "Iteration 13" in updated
        assert "Old content" not in updated
        assert "Footer" in updated

    def test_inserts_new_block(self):
        """测试插入新区块"""
        content = """# Title

## 执行信息

| 项目 | 值 |

## 执行结果总览

结果
"""
        updated, changed, inserted = sync_min_gate_block(content, 13, "full")

        assert changed is True
        assert inserted is True
        assert "<!-- BEGIN GENERATED: min_gate_block profile=full -->" in updated
        assert "## 执行结果总览" in updated

    def test_no_change_when_content_same(self):
        """测试内容相同时不变更"""
        block = generate_min_gate_block_with_markers(13, "full")
        content = f"# Header\n\n{block}\n\nFooter"

        updated, changed, inserted = sync_min_gate_block(content, 13, "full")

        assert changed is False
        assert inserted is False


class TestSyncEvidenceBlock:
    """测试 sync_evidence_block 函数"""

    def test_updates_existing_block(self, tmp_path: Path, monkeypatch: "MonkeyPatch"):
        """测试更新已存在的区块"""
        # 创建临时证据文件
        evidence_dir = tmp_path / "evidence"
        evidence_dir.mkdir()
        evidence_file = evidence_dir / "iteration_99_evidence.json"
        evidence_file.write_text(
            json.dumps(
                {
                    "iteration_number": 99,
                    "recorded_at": "2026-02-02T14:30:00Z",
                    "commit_sha": "abc1234",
                    "overall_result": "PASS",
                    "commands": [],
                }
            ),
            encoding="utf-8",
        )

        monkeypatch.setattr(blocks, "EVIDENCE_DIR", evidence_dir)

        content = """# Header

<!-- BEGIN GENERATED: evidence_snippet -->
Old content
<!-- END GENERATED: evidence_snippet -->

Footer"""

        updated, changed, inserted = sync_evidence_block(content, 99)

        assert changed is True
        assert inserted is False
        assert "abc1234" in updated
        assert "Old content" not in updated

    def test_inserts_placeholder_when_no_evidence(self, tmp_path: Path, monkeypatch: "MonkeyPatch"):
        """测试无证据文件时插入占位符"""
        monkeypatch.setattr(blocks, "EVIDENCE_DIR", tmp_path)

        content = """# Title

## 相关文档

链接
"""
        updated, changed, inserted = sync_evidence_block(content, 99)

        assert changed is True
        assert inserted is True
        assert "证据文件尚未生成" in updated

    def test_invalid_json_placeholder_does_not_leak_sensitive(self, tmp_path: Path, monkeypatch):
        """测试坏 JSON 含敏感串时占位符不泄露原文"""
        evidence_dir = tmp_path / "evidence"
        evidence_dir.mkdir()
        evidence_file = evidence_dir / "iteration_99_evidence.json"
        evidence_file.write_text(
            '{"notes": "glpat-secret-123", '
            '"dsn": "postgresql://user:pass@localhost/db", '
            '"auth": "Bearer super-secret"',
            encoding="utf-8",
        )

        monkeypatch.setattr(blocks, "EVIDENCE_DIR", evidence_dir)

        content = """# Title

## 相关文档

- Link
"""
        updated, changed, inserted = sync_evidence_block(content, 99)

        assert changed is True
        assert inserted is True
        assert "glpat-" not in updated
        assert "postgresql://" not in updated
        assert "Bearer " not in updated


# ============================================================================
# 集成测试
# ============================================================================


class TestSyncIterationRegression:
    """集成测试"""

    def test_full_sync_workflow(self, tmp_path: Path, monkeypatch: "MonkeyPatch"):
        """测试完整同步工作流"""
        # 创建临时目录结构
        docs_dir = tmp_path / "docs" / "acceptance"
        docs_dir.mkdir(parents=True)
        evidence_dir = docs_dir / "evidence"
        evidence_dir.mkdir()

        # 创建回归文档
        doc_content = """# Iteration 99 Regression

## 执行信息

| 项目 | 值 |
|------|-----|
| 日期 | 2026-02-02 |

## 执行结果总览

| 序号 | 测试 |

## 相关文档

- Link 1
"""
        doc_path = docs_dir / "iteration_99_regression.md"
        doc_path.write_text(doc_content, encoding="utf-8")

        # 创建证据文件
        evidence_file = evidence_dir / "iteration_99_evidence.json"
        evidence_file.write_text(
            json.dumps(
                {
                    "iteration_number": 99,
                    "recorded_at": "2026-02-02T14:30:00Z",
                    "commit_sha": "abc1234",
                    "overall_result": "PASS",
                    "commands": [
                        {"name": "ci", "command": "make ci", "result": "PASS"},
                    ],
                }
            ),
            encoding="utf-8",
        )

        # Mock 路径
        monkeypatch.setattr(blocks, "REGRESSION_DOCS_DIR", docs_dir)
        monkeypatch.setattr(blocks, "EVIDENCE_DIR", evidence_dir)

        # 执行同步（预览模式）
        result = sync_func(99, "full", write=False)

        assert result.success is True
        assert result.min_gate_changed is True
        assert result.evidence_changed is True
        assert result.min_gate_inserted is True
        assert result.evidence_inserted is True
        assert "Iteration 99" in result.updated_content
        assert "abc1234" in result.updated_content

    def test_sync_includes_v2_schema_line_and_markers(
        self, tmp_path: Path, monkeypatch: "MonkeyPatch"
    ):
        """测试 evidence 受控块包含 v2 schema 行且 marker 完整。"""
        docs_dir = tmp_path / "docs" / "acceptance"
        docs_dir.mkdir(parents=True)
        evidence_dir = docs_dir / "evidence"
        evidence_dir.mkdir()

        doc_content = """# Iteration 99 Regression

## 执行信息

| 项目 | 值 |
|------|-----|
| 日期 | 2026-02-02 |

## 执行结果总览

| 序号 | 测试 |
"""
        doc_path = docs_dir / "iteration_99_regression.md"
        doc_path.write_text(doc_content, encoding="utf-8")

        evidence_file = evidence_dir / "iteration_99_evidence.json"
        evidence_file.write_text(
            json.dumps(
                {
                    "$schema": CURRENT_SCHEMA_REF,
                    "iteration_number": 99,
                    "recorded_at": "2026-02-02T14:30:00Z",
                    "commit_sha": "abc1234",
                    "runner": {
                        "os": "darwin-24.6.0",
                        "python": "3.12.1",
                        "arch": "arm64",
                    },
                    "source": {
                        "source_path": "docs/acceptance/iteration_99_regression.md",
                    },
                    "overall_result": "PASS",
                    "commands": [
                        {"name": "ci", "command": "make ci", "result": "PASS"},
                    ],
                    "sensitive_data_declaration": True,
                }
            ),
            encoding="utf-8",
        )

        monkeypatch.setattr(blocks, "REGRESSION_DOCS_DIR", docs_dir)
        monkeypatch.setattr(blocks, "EVIDENCE_DIR", evidence_dir)

        result = sync_func(99, "full", write=False)

        assert result.success is True
        assert "<!-- BEGIN GENERATED: evidence_snippet -->" in result.updated_content
        assert "<!-- END GENERATED: evidence_snippet -->" in result.updated_content
        assert "| **Schema 版本** | `iteration_evidence_v2.schema.json` |" in result.updated_content

    def test_idempotent_sync(self, tmp_path: Path, monkeypatch: "MonkeyPatch"):
        """测试同步幂等性"""
        # 创建临时目录结构
        docs_dir = tmp_path / "docs" / "acceptance"
        docs_dir.mkdir(parents=True)
        evidence_dir = docs_dir / "evidence"
        evidence_dir.mkdir()

        # 创建包含已有区块的回归文档
        block = generate_min_gate_block_with_markers(99, "full")
        doc_content = f"""# Iteration 99 Regression

## 执行信息

{block}

## 相关文档
"""
        doc_path = docs_dir / "iteration_99_regression.md"
        doc_path.write_text(doc_content, encoding="utf-8")

        # Mock 路径
        monkeypatch.setattr(blocks, "REGRESSION_DOCS_DIR", docs_dir)
        monkeypatch.setattr(blocks, "EVIDENCE_DIR", evidence_dir)

        # 第一次同步
        result1 = sync_func(99, "full", write=True, sync_evidence=False)

        # 第二次同步
        result2 = sync_func(99, "full", write=False, sync_evidence=False)

        # 第二次应该无变化
        assert result1.success is True
        assert result2.success is True
        assert result2.min_gate_changed is False

    def test_write_mode_updates_file(self, tmp_path: Path, monkeypatch: "MonkeyPatch"):
        """测试写入模式更新文件"""
        # 创建临时目录结构
        docs_dir = tmp_path / "docs" / "acceptance"
        docs_dir.mkdir(parents=True)
        evidence_dir = docs_dir / "evidence"
        evidence_dir.mkdir()

        # 创建回归文档
        doc_content = """# Iteration 99 Regression

## 执行信息

<!-- BEGIN GENERATED: min_gate_block profile=full -->
Old content
<!-- END GENERATED: min_gate_block -->

## 相关文档
"""
        doc_path = docs_dir / "iteration_99_regression.md"
        doc_path.write_text(doc_content, encoding="utf-8")

        # Mock 路径
        monkeypatch.setattr(blocks, "REGRESSION_DOCS_DIR", docs_dir)
        monkeypatch.setattr(blocks, "EVIDENCE_DIR", evidence_dir)

        # 写入模式同步
        result = sync_func(99, "full", write=True, sync_evidence=False)

        assert result.success is True

        # 验证文件已更新
        updated = doc_path.read_text(encoding="utf-8")
        assert "Old content" not in updated
        assert "Iteration 99" in updated

    def test_error_when_doc_not_exists(self, tmp_path: Path, monkeypatch: "MonkeyPatch"):
        """测试文档不存在时返回错误"""
        docs_dir = tmp_path / "docs" / "acceptance"

        monkeypatch.setattr(blocks, "REGRESSION_DOCS_DIR", docs_dir)

        result = sync_func(99, "full")

        assert result.success is False
        assert "不存在" in result.message


# ============================================================================
# 边界情况测试
# ============================================================================


class TestEdgeCases:
    """边界情况测试"""

    def test_handles_empty_commands_list(self):
        """测试空命令列表"""
        evidence = {
            "iteration_number": 13,
            "recorded_at": "2026-02-02T14:30:00Z",
            "commit_sha": "abc1234",
            "overall_result": "PASS",
            "commands": [],
        }

        result = render_evidence_snippet(evidence)
        assert "## 验收证据" in result
        assert "门禁命令执行摘要" not in result  # 空列表不渲染表格

    def test_handles_missing_optional_fields(self):
        """测试缺少可选字段"""
        evidence = {
            "iteration_number": 13,
            "recorded_at": "2026-02-02T14:30:00Z",
            "commit_sha": "abc1234",
            "overall_result": "PASS",
            "commands": [
                {"name": "ci", "command": "make ci", "result": "PASS"},
            ],
            # 无 notes, links 等
        }

        result = render_evidence_snippet(evidence)
        assert "## 验收证据" in result
        # 不应该崩溃

    def test_handles_very_long_commit_sha(self):
        """测试超长 commit SHA"""
        evidence = {
            "iteration_number": 13,
            "recorded_at": "2026-02-02T14:30:00Z",
            "commit_sha": "abc1234567890def1234567890abc1234567890def",
            "overall_result": "PASS",
            "commands": [],
        }

        result = render_evidence_snippet(evidence)
        assert "`abc1234`" in result  # 截断到 7 位

    def test_handles_short_commit_sha(self):
        """测试短 commit SHA"""
        evidence = {
            "iteration_number": 13,
            "recorded_at": "2026-02-02T14:30:00Z",
            "commit_sha": "abc",
            "overall_result": "PASS",
            "commands": [],
        }

        result = render_evidence_snippet(evidence)
        assert "`abc`" in result  # 保持原样

    def test_profile_override_in_sync(self, tmp_path: Path, monkeypatch: "MonkeyPatch"):
        """测试同步时 profile 覆盖"""
        docs_dir = tmp_path / "docs" / "acceptance"
        docs_dir.mkdir(parents=True)
        evidence_dir = docs_dir / "evidence"
        evidence_dir.mkdir()

        # 创建带 full profile 的文档
        doc_content = """# Test

<!-- BEGIN GENERATED: min_gate_block profile=full -->
old
<!-- END GENERATED: min_gate_block -->
"""
        doc_path = docs_dir / "iteration_99_regression.md"
        doc_path.write_text(doc_content, encoding="utf-8")

        monkeypatch.setattr(blocks, "REGRESSION_DOCS_DIR", docs_dir)
        monkeypatch.setattr(blocks, "EVIDENCE_DIR", evidence_dir)

        # 使用 regression profile 覆盖
        result = sync_func(99, "regression", write=False, sync_evidence=False)

        assert result.success is True
        assert "profile=regression" in result.updated_content
        assert "回归最小集" in result.updated_content


# ============================================================================
# 向后兼容测试
# ============================================================================


class TestBackwardCompatibility:
    """向后兼容性测试"""

    def test_handles_legacy_min_gate_end_marker(self):
        """测试处理旧格式 min_gate END marker"""
        content = """<!-- BEGIN GENERATED: min_gate_block profile=full -->
old content
<!-- END GENERATED -->"""

        block = find_min_gate_block(content)
        assert block is not None

        updated, changed, _ = sync_min_gate_block(content, 13, "full")
        assert changed is True
        # 应该更新为新格式
        assert "<!-- END GENERATED: min_gate_block -->" in updated

    def test_handles_legacy_evidence_markers(self):
        """测试处理旧格式 evidence markers"""
        content = """<!-- AUTO-GENERATED EVIDENCE BLOCK START -->
old content
<!-- AUTO-GENERATED EVIDENCE BLOCK END -->"""

        block = find_evidence_block(content)
        assert block is not None

        # 同步后应该使用新格式
        evidence = {
            "iteration_number": 13,
            "recorded_at": "2026-02-02T14:30:00Z",
            "commit_sha": "abc1234",
            "overall_result": "PASS",
            "commands": [],
        }
        new_block = generate_evidence_block_with_markers(evidence)
        assert "<!-- BEGIN GENERATED: evidence_snippet -->" in new_block
        assert "<!-- END GENERATED: evidence_snippet -->" in new_block
