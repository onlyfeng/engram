#!/usr/bin/env python3
"""
render_iteration_evidence_snippet.py 单元测试

覆盖功能:
1. 缺文件错误处理
2. 坏 JSON 错误处理
3. 最小字段解析和渲染
4. 完整字段解析和渲染
5. CLI 入口测试
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

# 添加脚本目录到 path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "iteration"))

from render_iteration_evidence_snippet import (  # noqa: E402
    AUTO_GENERATED_MARKER,
    BLOCK_END_MARKER,
    BLOCK_START_MARKER,
    SCHEMA_NAME,
    CommandEntry,
    EvidenceData,
    EvidenceParseError,
    format_duration,
    get_evidence_path,
    load_evidence_file,
    parse_command_entry,
    parse_evidence_data,
    render_commands_table,
    render_evidence_snippet,
    render_iteration_evidence_snippet,
    render_meta_table,
    render_overall_result,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def minimal_evidence_data() -> dict:
    """最小字段的证据数据。"""
    return {
        "iteration_number": 13,
        "recorded_at": "2026-02-01T22:50:45Z",
        "commit_sha": "f442a3eb08a3ec2879109923d216d2b19bfd8f32",
        "commands": [
            {
                "name": "make-ci",
                "command": "make ci",
                "result": "PASS",
            }
        ],
    }


@pytest.fixture
def full_evidence_data() -> dict:
    """完整字段的证据数据。"""
    return {
        "$schema": "../../../schemas/iteration_evidence_v2.schema.json",
        "iteration_number": 13,
        "recorded_at": "2026-02-01T22:50:45Z",
        "commit_sha": "f442a3eb08a3ec2879109923d216d2b19bfd8f32",
        "runner": {
            "os": "darwin-24.6.0",
            "python": "3.13.2",
            "arch": "x86_64",
        },
        "commands": [
            {
                "name": "make-ci",
                "command": "make ci",
                "result": "PASS",
                "summary": "All checks passed",
                "duration_seconds": 45.3,
                "exit_code": 0,
            },
            {
                "name": "pytest-ci",
                "command": "pytest tests/ci/ -q",
                "result": "PASS",
                "summary": "142 passed, 0 failed",
                "duration_seconds": 12.8,
                "exit_code": 0,
            },
        ],
        "overall_result": "PASS",
        "sensitive_data_declaration": True,
        "links": {
            "regression_doc_url": "docs/acceptance/iteration_13_regression.md",
        },
        "notes": "所有门禁通过，验收完成。",
    }


@pytest.fixture
def temp_evidence_dir():
    """创建临时证据目录。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


# ============================================================================
# 错误处理测试
# ============================================================================


class TestMissingFile:
    """缺文件错误处理测试"""

    def test_missing_file_raises_error(self, temp_evidence_dir):
        """测试缺少文件时抛出 EvidenceParseError"""
        evidence_path = temp_evidence_dir / "iteration_99_evidence.json"

        with pytest.raises(EvidenceParseError) as exc_info:
            load_evidence_file(evidence_path)

        assert "证据文件不存在" in str(exc_info.value)
        assert str(evidence_path) in str(exc_info.value)
        assert "record_iteration_evidence.py" in str(exc_info.value)

    def test_render_missing_file_raises_error(self, temp_evidence_dir):
        """测试渲染缺少文件时抛出 EvidenceParseError"""
        with pytest.raises(EvidenceParseError) as exc_info:
            render_iteration_evidence_snippet(99, temp_evidence_dir)

        assert "证据文件不存在" in str(exc_info.value)


class TestBadJson:
    """坏 JSON 错误处理测试"""

    def test_invalid_json_raises_error(self, temp_evidence_dir):
        """测试无效 JSON 时抛出 EvidenceParseError"""
        evidence_path = temp_evidence_dir / "iteration_13_evidence.json"
        evidence_path.write_text("{ invalid json }", encoding="utf-8")

        with pytest.raises(EvidenceParseError) as exc_info:
            load_evidence_file(evidence_path)

        assert "JSON 解析失败" in str(exc_info.value)
        assert "有效的 JSON 格式" in str(exc_info.value)

    def test_empty_file_raises_error(self, temp_evidence_dir):
        """测试空文件时抛出 EvidenceParseError"""
        evidence_path = temp_evidence_dir / "iteration_13_evidence.json"
        evidence_path.write_text("", encoding="utf-8")

        with pytest.raises(EvidenceParseError) as exc_info:
            load_evidence_file(evidence_path)

        assert "JSON 解析失败" in str(exc_info.value)

    def test_truncated_json_raises_error(self, temp_evidence_dir):
        """测试截断 JSON 时抛出 EvidenceParseError"""
        evidence_path = temp_evidence_dir / "iteration_13_evidence.json"
        evidence_path.write_text('{"iteration_number": 13, "recorded_at":', encoding="utf-8")

        with pytest.raises(EvidenceParseError) as exc_info:
            load_evidence_file(evidence_path)

        assert "JSON 解析失败" in str(exc_info.value)


class TestMissingFields:
    """缺少必要字段错误处理测试"""

    def test_missing_iteration_number(self):
        """测试缺少 iteration_number 字段"""
        data = {
            "recorded_at": "2026-02-01T22:50:45Z",
            "commit_sha": "abc1234",
            "commands": [{"name": "test", "command": "make ci", "result": "PASS"}],
        }

        with pytest.raises(EvidenceParseError) as exc_info:
            parse_evidence_data(data)

        assert "缺少必要字段: iteration_number" in str(exc_info.value)

    def test_missing_recorded_at(self):
        """测试缺少 recorded_at 字段"""
        data = {
            "iteration_number": 13,
            "commit_sha": "abc1234",
            "commands": [{"name": "test", "command": "make ci", "result": "PASS"}],
        }

        with pytest.raises(EvidenceParseError) as exc_info:
            parse_evidence_data(data)

        assert "缺少必要字段: recorded_at" in str(exc_info.value)

    def test_missing_commit_sha(self):
        """测试缺少 commit_sha 字段"""
        data = {
            "iteration_number": 13,
            "recorded_at": "2026-02-01T22:50:45Z",
            "commands": [{"name": "test", "command": "make ci", "result": "PASS"}],
        }

        with pytest.raises(EvidenceParseError) as exc_info:
            parse_evidence_data(data)

        assert "缺少必要字段: commit_sha" in str(exc_info.value)

    def test_missing_commands(self):
        """测试缺少 commands 字段"""
        data = {
            "iteration_number": 13,
            "recorded_at": "2026-02-01T22:50:45Z",
            "commit_sha": "abc1234",
        }

        with pytest.raises(EvidenceParseError) as exc_info:
            parse_evidence_data(data)

        assert "缺少必要字段: commands" in str(exc_info.value)

    def test_empty_commands_array(self):
        """测试空 commands 数组"""
        data = {
            "iteration_number": 13,
            "recorded_at": "2026-02-01T22:50:45Z",
            "commit_sha": "abc1234",
            "commands": [],
        }

        with pytest.raises(EvidenceParseError) as exc_info:
            parse_evidence_data(data)

        assert "commands 数组不能为空" in str(exc_info.value)

    def test_commands_not_array(self):
        """测试 commands 不是数组"""
        data = {
            "iteration_number": 13,
            "recorded_at": "2026-02-01T22:50:45Z",
            "commit_sha": "abc1234",
            "commands": "not an array",
        }

        with pytest.raises(EvidenceParseError) as exc_info:
            parse_evidence_data(data)

        assert "commands 字段必须是数组" in str(exc_info.value)

    def test_command_entry_missing_name(self):
        """测试命令条目缺少 name"""
        data = {"command": "make ci", "result": "PASS"}

        with pytest.raises(EvidenceParseError) as exc_info:
            parse_command_entry(data)

        assert "命令条目缺少必要字段: name" in str(exc_info.value)

    def test_command_entry_missing_command(self):
        """测试命令条目缺少 command"""
        data = {"name": "test", "result": "PASS"}

        with pytest.raises(EvidenceParseError) as exc_info:
            parse_command_entry(data)

        assert "命令条目缺少必要字段: command" in str(exc_info.value)

    def test_command_entry_missing_result(self):
        """测试命令条目缺少 result"""
        data = {"name": "test", "command": "make ci"}

        with pytest.raises(EvidenceParseError) as exc_info:
            parse_command_entry(data)

        assert "命令条目缺少必要字段: result" in str(exc_info.value)


# ============================================================================
# 最小字段测试
# ============================================================================


class TestMinimalFields:
    """最小字段解析和渲染测试"""

    def test_parse_minimal_evidence(self, minimal_evidence_data):
        """测试解析最小字段证据"""
        evidence = parse_evidence_data(minimal_evidence_data)

        assert evidence.iteration_number == 13
        assert evidence.recorded_at == "2026-02-01T22:50:45Z"
        assert evidence.commit_sha == "f442a3eb08a3ec2879109923d216d2b19bfd8f32"
        assert len(evidence.commands) == 1
        assert evidence.commands[0].name == "make-ci"
        assert evidence.commands[0].command == "make ci"
        assert evidence.commands[0].result == "PASS"
        assert evidence.overall_result is None
        assert evidence.notes is None

    def test_parse_minimal_command_entry(self):
        """测试解析最小字段命令条目"""
        data = {"name": "test", "command": "make ci", "result": "PASS"}
        cmd = parse_command_entry(data)

        assert cmd.name == "test"
        assert cmd.command == "make ci"
        assert cmd.result == "PASS"
        assert cmd.summary is None
        assert cmd.duration_seconds is None
        assert cmd.exit_code is None

    def test_render_minimal_evidence(self, minimal_evidence_data, temp_evidence_dir):
        """测试渲染最小字段证据"""
        evidence_path = temp_evidence_dir / "iteration_13_evidence.json"
        evidence_path.write_text(json.dumps(minimal_evidence_data), encoding="utf-8")

        result = render_iteration_evidence_snippet(13, temp_evidence_dir)

        # 验证基本结构
        assert "## 验收证据" in result
        assert BLOCK_START_MARKER in result
        assert BLOCK_END_MARKER in result
        assert AUTO_GENERATED_MARKER in result

        # 验证元信息表格
        assert "iteration_13_evidence.json" in result
        assert SCHEMA_NAME in result
        assert "2026-02-01T22:50:45Z" in result
        assert "f442a3e" in result  # 短 SHA

        # 验证命令表格
        assert "| `make ci` | PASS |" in result

        # 验证整体结果（未指定）
        assert "**结果**: 未指定" in result


# ============================================================================
# 完整字段测试
# ============================================================================


class TestFullFields:
    """完整字段解析和渲染测试"""

    def test_parse_full_evidence(self, full_evidence_data):
        """测试解析完整字段证据"""
        evidence = parse_evidence_data(full_evidence_data)

        assert evidence.iteration_number == 13
        assert evidence.recorded_at == "2026-02-01T22:50:45Z"
        assert evidence.commit_sha == "f442a3eb08a3ec2879109923d216d2b19bfd8f32"
        assert len(evidence.commands) == 2
        assert evidence.overall_result == "PASS"
        assert evidence.notes == "所有门禁通过，验收完成。"

    def test_parse_full_command_entry(self):
        """测试解析完整字段命令条目"""
        data = {
            "name": "make-ci",
            "command": "make ci",
            "result": "PASS",
            "summary": "All checks passed",
            "duration_seconds": 45.3,
            "exit_code": 0,
        }
        cmd = parse_command_entry(data)

        assert cmd.name == "make-ci"
        assert cmd.command == "make ci"
        assert cmd.result == "PASS"
        assert cmd.summary == "All checks passed"
        assert cmd.duration_seconds == 45.3
        assert cmd.exit_code == 0

    def test_render_full_evidence(self, full_evidence_data, temp_evidence_dir):
        """测试渲染完整字段证据"""
        evidence_path = temp_evidence_dir / "iteration_13_evidence.json"
        evidence_path.write_text(json.dumps(full_evidence_data), encoding="utf-8")

        result = render_iteration_evidence_snippet(13, temp_evidence_dir)

        # 验证基本结构
        assert "## 验收证据" in result
        assert BLOCK_START_MARKER in result
        assert BLOCK_END_MARKER in result

        # 验证元信息表格
        assert "iteration_13_evidence.json" in result
        assert "2026-02-01T22:50:45Z" in result

        # 验证命令表格包含所有命令
        assert "| `make ci` | PASS |" in result
        assert "| `pytest tests/ci/ -q` | PASS |" in result

        # 验证耗时格式化
        assert "45.3s" in result
        assert "12.8s" in result

        # 验证摘要
        assert "All checks passed" in result
        assert "142 passed, 0 failed" in result

        # 验证整体结果
        assert "**结果**: PASS" in result
        assert "所有门禁通过，验收完成" in result


# ============================================================================
# 辅助函数测试
# ============================================================================


class TestFormatDuration:
    """format_duration 函数测试"""

    def test_none_returns_dash(self):
        """测试 None 返回 -"""
        assert format_duration(None) == "-"

    def test_seconds_under_60(self):
        """测试 60 秒以下"""
        assert format_duration(0) == "0.0s"
        assert format_duration(5.2) == "5.2s"
        assert format_duration(45.3) == "45.3s"
        assert format_duration(59.9) == "59.9s"

    def test_seconds_over_60(self):
        """测试 60 秒以上"""
        assert format_duration(60) == "1m0s"
        assert format_duration(90) == "1m30s"
        assert format_duration(125.5) == "2m6s"


class TestGetEvidencePath:
    """get_evidence_path 函数测试"""

    def test_default_path(self):
        """测试默认路径"""
        path = get_evidence_path(13)
        assert path == Path("docs/acceptance/evidence/iteration_13_evidence.json")

    def test_custom_path(self, temp_evidence_dir):
        """测试自定义路径"""
        path = get_evidence_path(13, temp_evidence_dir)
        assert path == temp_evidence_dir / "iteration_13_evidence.json"


# ============================================================================
# 渲染函数测试
# ============================================================================


class TestRenderMetaTable:
    """render_meta_table 函数测试"""

    def test_renders_table_header(self):
        """测试渲染表格头部"""
        evidence = EvidenceData(
            iteration_number=13,
            recorded_at="2026-02-01T22:50:45Z",
            commit_sha="abc1234567890",
            commands=[CommandEntry("test", "make ci", "PASS")],
        )
        result = render_meta_table(evidence, "iteration_13_evidence.json")

        assert "| 项目 | 值 |" in result
        assert "|------|-----|" in result

    def test_renders_evidence_filename(self):
        """测试渲染证据文件名"""
        evidence = EvidenceData(
            iteration_number=13,
            recorded_at="2026-02-01T22:50:45Z",
            commit_sha="abc1234567890",
            commands=[CommandEntry("test", "make ci", "PASS")],
        )
        result = render_meta_table(evidence, "iteration_13_evidence.json")

        assert "`iteration_13_evidence.json`" in result
        assert "(evidence/iteration_13_evidence.json)" in result

    def test_renders_short_sha(self):
        """测试渲染短 SHA"""
        evidence = EvidenceData(
            iteration_number=13,
            recorded_at="2026-02-01T22:50:45Z",
            commit_sha="abc1234567890abcdef1234567890abcdef123456",
            commands=[CommandEntry("test", "make ci", "PASS")],
        )
        result = render_meta_table(evidence, "iteration_13_evidence.json")

        # 应该只显示前 7 位
        assert "`abc1234`" in result
        assert "abc1234567890" not in result


class TestRenderCommandsTable:
    """render_commands_table 函数测试"""

    def test_renders_table_header(self):
        """测试渲染表格头部"""
        commands = [CommandEntry("test", "make ci", "PASS")]
        result = render_commands_table(commands)

        assert "| 命令 | 结果 | 耗时 | 摘要 |" in result
        assert "|------|------|------|------|" in result

    def test_renders_command_row(self):
        """测试渲染命令行"""
        commands = [
            CommandEntry("test", "make ci", "PASS", "All passed", 45.3, 0),
        ]
        result = render_commands_table(commands)

        assert "| `make ci` | PASS | 45.3s | All passed |" in result

    def test_renders_missing_optional_fields(self):
        """测试渲染缺少可选字段"""
        commands = [CommandEntry("test", "make ci", "PASS")]
        result = render_commands_table(commands)

        assert "| `make ci` | PASS | - | - |" in result

    def test_truncates_long_summary(self):
        """测试截断过长摘要"""
        long_summary = "A" * 100
        commands = [CommandEntry("test", "make ci", "PASS", long_summary)]
        result = render_commands_table(commands)

        # 应该截断为 47 字符 + "..."
        assert "A" * 47 + "..." in result
        assert long_summary not in result


class TestRenderOverallResult:
    """render_overall_result 函数测试"""

    def test_renders_pass_result(self):
        """测试渲染 PASS 结果"""
        evidence = EvidenceData(
            iteration_number=13,
            recorded_at="2026-02-01T22:50:45Z",
            commit_sha="abc1234",
            commands=[CommandEntry("test", "make ci", "PASS")],
            overall_result="PASS",
        )
        result = render_overall_result(evidence)

        assert "**结果**: PASS" in result

    def test_renders_unspecified_result(self):
        """测试渲染未指定结果"""
        evidence = EvidenceData(
            iteration_number=13,
            recorded_at="2026-02-01T22:50:45Z",
            commit_sha="abc1234",
            commands=[CommandEntry("test", "make ci", "PASS")],
        )
        result = render_overall_result(evidence)

        assert "**结果**: 未指定" in result

    def test_renders_notes(self):
        """测试渲染说明"""
        evidence = EvidenceData(
            iteration_number=13,
            recorded_at="2026-02-01T22:50:45Z",
            commit_sha="abc1234",
            commands=[CommandEntry("test", "make ci", "PASS")],
            notes="所有门禁通过。",
        )
        result = render_overall_result(evidence)

        assert "**说明**: 所有门禁通过。" in result

    def test_truncates_long_notes(self):
        """测试截断过长说明"""
        long_notes = "B" * 300
        evidence = EvidenceData(
            iteration_number=13,
            recorded_at="2026-02-01T22:50:45Z",
            commit_sha="abc1234",
            commands=[CommandEntry("test", "make ci", "PASS")],
            notes=long_notes,
        )
        result = render_overall_result(evidence)

        # 应该截断为 197 字符 + "..."
        assert "B" * 197 + "..." in result
        assert long_notes not in result


# ============================================================================
# 完整渲染测试
# ============================================================================


class TestRenderEvidenceSnippet:
    """render_evidence_snippet 函数测试"""

    def test_renders_section_header(self):
        """测试渲染段落标题"""
        evidence = EvidenceData(
            iteration_number=13,
            recorded_at="2026-02-01T22:50:45Z",
            commit_sha="abc1234",
            commands=[CommandEntry("test", "make ci", "PASS")],
        )
        result = render_evidence_snippet(evidence, "iteration_13_evidence.json")

        assert "## 验收证据" in result

    def test_renders_block_markers(self):
        """测试渲染块标记"""
        evidence = EvidenceData(
            iteration_number=13,
            recorded_at="2026-02-01T22:50:45Z",
            commit_sha="abc1234",
            commands=[CommandEntry("test", "make ci", "PASS")],
        )
        result = render_evidence_snippet(evidence, "iteration_13_evidence.json")

        assert BLOCK_START_MARKER in result
        assert BLOCK_END_MARKER in result
        assert AUTO_GENERATED_MARKER in result

    def test_renders_all_sections(self):
        """测试渲染所有段落"""
        evidence = EvidenceData(
            iteration_number=13,
            recorded_at="2026-02-01T22:50:45Z",
            commit_sha="abc1234",
            commands=[CommandEntry("test", "make ci", "PASS")],
        )
        result = render_evidence_snippet(evidence, "iteration_13_evidence.json")

        assert "## 验收证据" in result
        assert "### 门禁命令执行摘要" in result
        assert "### 整体验收结果" in result
        assert "| 项目 | 值 |" in result
        assert "| 命令 | 结果 | 耗时 | 摘要 |" in result


# ============================================================================
# 集成测试
# ============================================================================


class TestIntegration:
    """集成测试"""

    def test_load_and_render_evidence_file(self, full_evidence_data, temp_evidence_dir):
        """测试加载并渲染证据文件"""
        evidence_path = temp_evidence_dir / "iteration_13_evidence.json"
        evidence_path.write_text(json.dumps(full_evidence_data), encoding="utf-8")

        # 加载
        evidence = load_evidence_file(evidence_path)
        assert evidence.iteration_number == 13

        # 渲染
        result = render_evidence_snippet(evidence, evidence_path.name)
        assert "## 验收证据" in result
        assert "iteration_13_evidence.json" in result

    def test_render_iteration_evidence_snippet_full_pipeline(
        self, full_evidence_data, temp_evidence_dir
    ):
        """测试完整管道"""
        evidence_path = temp_evidence_dir / "iteration_13_evidence.json"
        evidence_path.write_text(json.dumps(full_evidence_data), encoding="utf-8")

        result = render_iteration_evidence_snippet(13, temp_evidence_dir)

        # 验证输出包含所有预期内容
        assert "## 验收证据" in result
        assert "iteration_13_evidence.json" in result
        assert "2026-02-01T22:50:45Z" in result
        assert "| `make ci` | PASS |" in result
        assert "| `pytest tests/ci/ -q` | PASS |" in result
        assert "**结果**: PASS" in result


# ============================================================================
# 边界情况测试
# ============================================================================


class TestEdgeCases:
    """边界情况测试"""

    def test_very_short_commit_sha(self):
        """测试非常短的 commit SHA"""
        evidence = EvidenceData(
            iteration_number=13,
            recorded_at="2026-02-01T22:50:45Z",
            commit_sha="abc1234",  # 7 字符
            commands=[CommandEntry("test", "make ci", "PASS")],
        )
        result = render_meta_table(evidence, "test.json")

        assert "`abc1234`" in result

    def test_multiple_commands(self, temp_evidence_dir):
        """测试多个命令"""
        data = {
            "iteration_number": 13,
            "recorded_at": "2026-02-01T22:50:45Z",
            "commit_sha": "abc1234",
            "commands": [
                {"name": "cmd1", "command": "make lint", "result": "PASS"},
                {"name": "cmd2", "command": "make test", "result": "PASS"},
                {"name": "cmd3", "command": "make build", "result": "FAIL"},
            ],
        }
        evidence_path = temp_evidence_dir / "iteration_13_evidence.json"
        evidence_path.write_text(json.dumps(data), encoding="utf-8")

        result = render_iteration_evidence_snippet(13, temp_evidence_dir)

        assert "| `make lint` | PASS |" in result
        assert "| `make test` | PASS |" in result
        assert "| `make build` | FAIL |" in result

    def test_special_characters_in_summary(self):
        """测试摘要中的特殊字符"""
        commands = [
            CommandEntry("test", "make ci", "PASS", "Test | with | pipes"),
        ]
        result = render_commands_table(commands)

        # 管道符应该被保留（Markdown 表格会处理）
        assert "Test | with | pipes" in result

    def test_unicode_in_notes(self):
        """测试说明中的 Unicode"""
        evidence = EvidenceData(
            iteration_number=13,
            recorded_at="2026-02-01T22:50:45Z",
            commit_sha="abc1234",
            commands=[CommandEntry("test", "make ci", "PASS")],
            notes="所有门禁通过 ✅",
        )
        result = render_overall_result(evidence)

        assert "所有门禁通过 ✅" in result


# ============================================================================
# 输出格式验证测试
# ============================================================================


class TestOutputFormat:
    """输出格式验证测试"""

    def test_markdown_table_format(self, minimal_evidence_data, temp_evidence_dir):
        """测试 Markdown 表格格式正确"""
        evidence_path = temp_evidence_dir / "iteration_13_evidence.json"
        evidence_path.write_text(json.dumps(minimal_evidence_data), encoding="utf-8")

        result = render_iteration_evidence_snippet(13, temp_evidence_dir)

        # 表格行应该以 | 开头和结尾
        lines = result.split("\n")
        table_lines = [line for line in lines if line.startswith("|")]

        for line in table_lines:
            assert line.endswith("|"), f"表格行应以 | 结尾: {line}"

    def test_block_markers_in_correct_order(self, minimal_evidence_data, temp_evidence_dir):
        """测试块标记顺序正确"""
        evidence_path = temp_evidence_dir / "iteration_13_evidence.json"
        evidence_path.write_text(json.dumps(minimal_evidence_data), encoding="utf-8")

        result = render_iteration_evidence_snippet(13, temp_evidence_dir)

        start_pos = result.find(BLOCK_START_MARKER)
        auto_gen_pos = result.find(AUTO_GENERATED_MARKER)
        end_pos = result.find(BLOCK_END_MARKER)

        assert start_pos < auto_gen_pos < end_pos, "块标记顺序不正确"

    def test_ends_with_newline(self, minimal_evidence_data, temp_evidence_dir):
        """测试输出以换行符结尾"""
        evidence_path = temp_evidence_dir / "iteration_13_evidence.json"
        evidence_path.write_text(json.dumps(minimal_evidence_data), encoding="utf-8")

        result = render_iteration_evidence_snippet(13, temp_evidence_dir)

        assert result.endswith("\n"), "输出应以换行符结尾"
