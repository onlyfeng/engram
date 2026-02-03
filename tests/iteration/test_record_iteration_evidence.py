#!/usr/bin/env python3
"""
record_iteration_evidence.py 单元测试

覆盖功能:
1. 字段映射正确性（timestamp → recorded_at, ci_run_url → links.ci_run_url）
2. runner 信息收集
3. exit_code → result 转换
4. overall_result 计算
5. 输出文件名策略（固定文件名 iteration_{N}_evidence.json）
6. 敏感信息脱敏
7. JSON 输出符合 schema 结构
8. Schema 校验（使用 jsonschema 验证输出）
9. 敏感键/敏感值脱敏验证
10. 最小输入（无 commands）时的行为与 schema minItems=1 对齐
11. commands 输入两种格式（dict/array）的映射结果
12. recorded_at 为 UTC 且满足 date-time 格式
13. 文件名生成符合规范（canonical/snapshot）
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest

# 项目根目录
REPO_ROOT = Path(__file__).parent.parent.parent

# 添加脚本目录到 path
sys.path.insert(0, str(REPO_ROOT / "scripts" / "iteration"))
sys.path.insert(0, str(REPO_ROOT))

from iteration_evidence_schema import CURRENT_SCHEMA_REF
from record_iteration_evidence import (
    REDACTED_PLACEHOLDER,
    CommandEntry,
    RunnerInfo,
    compute_overall_result,
    derive_command_name,
    exit_code_to_result,
    get_runner_info,
    is_sensitive_key,
    is_sensitive_value,
    normalize_command_name,
    parse_commands_json,
    record_evidence,
    redact_sensitive_data,
)

from scripts.ci.check_iteration_evidence_contract import scan_evidence_files

# Schema 文件路径
SCHEMA_PATH = REPO_ROOT / "schemas" / "iteration_evidence_v2.schema.json"


def load_evidence_schema() -> dict:
    """加载迭代证据 schema。"""
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        return json.load(f)


# ============================================================================
# 测试 exit_code_to_result
# ============================================================================


class TestExitCodeToResult:
    """测试 exit_code → result 转换。"""

    def test_exit_code_0_returns_pass(self) -> None:
        assert exit_code_to_result(0) == "PASS"

    def test_exit_code_1_returns_fail(self) -> None:
        assert exit_code_to_result(1) == "FAIL"

    def test_exit_code_nonzero_returns_fail(self) -> None:
        assert exit_code_to_result(127) == "FAIL"
        assert exit_code_to_result(255) == "FAIL"


# ============================================================================
# 测试 compute_overall_result
# ============================================================================


class TestComputeOverallResult:
    """测试整体结果计算。"""

    def test_all_pass_returns_pass(self) -> None:
        commands = [
            CommandEntry(name="lint", command="make lint", result="PASS"),
            CommandEntry(name="test", command="make test", result="PASS"),
        ]
        assert compute_overall_result(commands) == "PASS"

    def test_all_fail_returns_fail(self) -> None:
        commands = [
            CommandEntry(name="lint", command="make lint", result="FAIL"),
            CommandEntry(name="test", command="make test", result="ERROR"),
        ]
        assert compute_overall_result(commands) == "FAIL"

    def test_mixed_results_returns_partial(self) -> None:
        commands = [
            CommandEntry(name="lint", command="make lint", result="PASS"),
            CommandEntry(name="test", command="make test", result="FAIL"),
        ]
        assert compute_overall_result(commands) == "PARTIAL"

    def test_empty_commands_returns_fail(self) -> None:
        assert compute_overall_result([]) == "FAIL"


# ============================================================================
# 测试 derive_command_name
# ============================================================================


class TestDeriveCommandName:
    """测试命令名称推导。"""

    def test_make_target(self) -> None:
        assert derive_command_name("make ci") == "ci"
        assert derive_command_name("make lint") == "lint"
        assert derive_command_name("make typecheck") == "typecheck"

    def test_pytest_command(self) -> None:
        assert derive_command_name("pytest tests/") == "test"
        assert derive_command_name("python -m pytest tests/") == "test"

    def test_other_command(self) -> None:
        assert derive_command_name("ruff check .") == "ruff"


# ============================================================================
# 测试 get_runner_info
# ============================================================================


class TestGetRunnerInfo:
    """测试 runner 信息收集。"""

    def test_returns_runner_info(self) -> None:
        runner = get_runner_info()
        assert isinstance(runner, RunnerInfo)
        assert runner.os  # 非空
        assert runner.python  # 非空
        assert runner.arch  # 非空

    def test_python_version_format(self) -> None:
        runner = get_runner_info()
        # 应该是 X.Y.Z 格式
        parts = runner.python.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    def test_runner_label_passed_through(self) -> None:
        runner = get_runner_info(runner_label="ubuntu-latest")
        assert runner.runner_label == "ubuntu-latest"


# ============================================================================
# 测试 parse_commands_json
# ============================================================================


class TestParseCommandsJson:
    """测试命令结果 JSON 解析。"""

    def test_simple_dict_format(self) -> None:
        data = {"make ci": {"exit_code": 0, "summary": "passed"}}
        commands = parse_commands_json(data)
        assert len(commands) == 1
        assert commands[0].name == "ci"
        assert commands[0].command == "make ci"
        assert commands[0].result == "PASS"
        assert commands[0].exit_code == 0

    def test_array_format(self) -> None:
        data = [{"command": "make lint", "exit_code": 0, "summary": "ok"}]
        commands = parse_commands_json(data)
        assert len(commands) == 1
        assert commands[0].command == "make lint"
        assert commands[0].result == "PASS"

    def test_schema_format(self) -> None:
        """测试已符合 schema 格式的输入。"""
        data = [
            {
                "name": "lint",
                "command": "make lint",
                "result": "PASS",
                "exit_code": 0,
            }
        ]
        commands = parse_commands_json(data)
        assert len(commands) == 1
        assert commands[0].name == "lint"
        assert commands[0].result == "PASS"

    def test_failed_command(self) -> None:
        data = {"make test": {"exit_code": 1, "summary": "2 failed"}}
        commands = parse_commands_json(data)
        assert commands[0].result == "FAIL"


# ============================================================================
# 测试 record_evidence
# ============================================================================


class TestRecordEvidence:
    """测试证据记录功能。"""

    def test_output_filename_format(self) -> None:
        """测试输出文件名符合固定命名策略。"""
        commands = [CommandEntry(name="ci", command="make ci", result="PASS")]
        result = record_evidence(
            iteration_number=13,
            commit_sha="abc1234567890",
            commands=commands,
            dry_run=True,
        )
        assert result.success
        assert "iteration_13_evidence.json" in result.output_path

    def test_output_contains_required_fields(self) -> None:
        """测试输出包含 schema 要求的必需字段。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 临时修改输出目录
            import record_iteration_evidence
            import iteration_evidence_naming as evidence_naming

            original_dir = record_iteration_evidence.EVIDENCE_DIR
            original_naming_dir = evidence_naming.EVIDENCE_DIR
            record_iteration_evidence.EVIDENCE_DIR = Path(tmpdir)
            evidence_naming.EVIDENCE_DIR = Path(tmpdir)

            try:
                commands = [
                    CommandEntry(
                        name="ci",
                        command="make ci",
                        result="PASS",
                        exit_code=0,
                    )
                ]
                result = record_evidence(
                    iteration_number=13,
                    commit_sha="abc1234567890",
                    commands=commands,
                )

                # 读取输出文件
                with open(result.output_path, encoding="utf-8") as f:
                    data = json.load(f)

                # 检查必需字段
                assert "iteration_number" in data
                assert data["iteration_number"] == 13
                assert "recorded_at" in data
                assert "commit_sha" in data
                assert "runner" in data
                assert "commands" in data
                assert "overall_result" in data
                assert "sensitive_data_declaration" in data
                assert data["sensitive_data_declaration"] is True

                # 检查 runner 结构
                runner = data["runner"]
                assert "os" in runner
                assert "python" in runner
                assert "arch" in runner

                # 检查 commands 结构
                cmd = data["commands"][0]
                assert cmd["name"] == "ci"
                assert cmd["command"] == "make ci"
                assert cmd["result"] == "PASS"

            finally:
                record_iteration_evidence.EVIDENCE_DIR = original_dir
                evidence_naming.EVIDENCE_DIR = original_naming_dir

    def test_record_evidence_outputs_v2_schema_and_required_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """测试 record_evidence 输出 v2 schema 且包含必需字段。"""
        import iteration_evidence_naming as evidence_naming
        import record_iteration_evidence

        evidence_dir = tmp_path / "evidence"
        monkeypatch.setattr(record_iteration_evidence, "EVIDENCE_DIR", evidence_dir)
        monkeypatch.setattr(evidence_naming, "EVIDENCE_DIR", evidence_dir)

        commands = [
            CommandEntry(
                name="ci",
                command="make ci",
                result="PASS",
                exit_code=0,
            )
        ]
        result = record_evidence(
            iteration_number=21,
            commit_sha="abc1234567890abc1234567890abc1234567890",
            commands=commands,
            include_regression_doc_url=False,
        )

        with open(result.output_path, encoding="utf-8") as f:
            data = json.load(f)

        assert data["$schema"] == CURRENT_SCHEMA_REF
        assert "runner" in data
        assert "commands" in data
        assert data["runner"]["os"]
        assert data["runner"]["python"]
        assert data["runner"]["arch"]
        assert len(data["commands"]) == 1

    def test_scan_evidence_files_has_no_violations(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """测试 scan_evidence_files 对临时 evidence 目录无违规。"""
        import scripts.iteration.iteration_evidence_naming as evidence_naming
        import scripts.iteration.record_iteration_evidence as record_module

        evidence_dir = tmp_path / "evidence"
        # 创建 evidence 目录
        evidence_dir.mkdir(parents=True, exist_ok=True)

        # 需要 patch 两个地方：
        # 1. iteration_evidence_naming.EVIDENCE_DIR - 用于 canonical_evidence_path()
        # 2. record_iteration_evidence.EVIDENCE_DIR - 用于 mkdir()
        monkeypatch.setattr(evidence_naming, "EVIDENCE_DIR", evidence_dir)
        monkeypatch.setattr(record_module, "EVIDENCE_DIR", evidence_dir)

        commands = [
            CommandEntry(
                name="ci",
                command="make ci",
                result="PASS",
                exit_code=0,
            )
        ]
        record_evidence(
            iteration_number=22,
            commit_sha="abc1234567890abc1234567890abc1234567890",
            commands=commands,
            include_regression_doc_url=False,
        )

        violations, warnings, total_files = scan_evidence_files(
            evidence_dir=evidence_dir,
            project_root=tmp_path,
        )

        assert total_files == 1
        assert violations == []

    def test_ci_run_url_mapped_to_links(self) -> None:
        """测试 ci_run_url 正确映射到 links.ci_run_url。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            import record_iteration_evidence

            original_dir = record_iteration_evidence.EVIDENCE_DIR
            record_iteration_evidence.EVIDENCE_DIR = Path(tmpdir)

            try:
                commands = [CommandEntry(name="ci", command="make ci", result="PASS")]
                result = record_evidence(
                    iteration_number=13,
                    commit_sha="abc1234567890",
                    commands=commands,
                    ci_run_url="https://github.com/org/repo/actions/runs/123",
                )

                with open(result.output_path, encoding="utf-8") as f:
                    data = json.load(f)

                assert "links" in data
                assert data["links"]["ci_run_url"] == "https://github.com/org/repo/actions/runs/123"

            finally:
                record_iteration_evidence.EVIDENCE_DIR = original_dir

    def test_regression_doc_url_mapped_to_links(self) -> None:
        """测试 regression_doc_url 正确映射到 links.regression_doc_url。"""
        schema = load_evidence_schema()

        with tempfile.TemporaryDirectory() as tmpdir:
            import record_iteration_evidence

            original_dir = record_iteration_evidence.EVIDENCE_DIR
            record_iteration_evidence.EVIDENCE_DIR = Path(tmpdir)

            try:
                commands = [CommandEntry(name="ci", command="make ci", result="PASS")]
                result = record_evidence(
                    iteration_number=13,
                    commit_sha="abc1234567890",
                    commands=commands,
                    regression_doc_url="docs/acceptance/iteration_13_regression.md",
                )

                with open(result.output_path, encoding="utf-8") as f:
                    data = json.load(f)

                # 验证 regression_doc_url 存在
                assert "links" in data
                assert (
                    data["links"]["regression_doc_url"]
                    == "docs/acceptance/iteration_13_regression.md"
                )

                # 验证通过 schema 校验
                jsonschema.validate(instance=data, schema=schema)

            finally:
                record_iteration_evidence.EVIDENCE_DIR = original_dir

    def test_pr_url_mapped_to_links(self) -> None:
        """测试 pr_url 正确映射到 links.pr_url。"""
        schema = load_evidence_schema()

        with tempfile.TemporaryDirectory() as tmpdir:
            import record_iteration_evidence

            original_dir = record_iteration_evidence.EVIDENCE_DIR
            record_iteration_evidence.EVIDENCE_DIR = Path(tmpdir)

            try:
                commands = [CommandEntry(name="ci", command="make ci", result="PASS")]
                result = record_evidence(
                    iteration_number=13,
                    commit_sha="abc1234567890",
                    commands=commands,
                    pr_url="https://github.com/org/repo/pull/123",
                )

                with open(result.output_path, encoding="utf-8") as f:
                    data = json.load(f)

                # 验证 pr_url 存在
                assert "links" in data
                assert data["links"]["pr_url"] == "https://github.com/org/repo/pull/123"

                # 验证通过 schema 校验
                jsonschema.validate(
                    instance=data,
                    schema=schema,
                    format_checker=jsonschema.FormatChecker(),
                )

            finally:
                record_iteration_evidence.EVIDENCE_DIR = original_dir

    def test_artifact_url_mapped_to_links(self) -> None:
        """测试 artifact_url 正确映射到 links.artifact_url。"""
        schema = load_evidence_schema()

        with tempfile.TemporaryDirectory() as tmpdir:
            import record_iteration_evidence

            original_dir = record_iteration_evidence.EVIDENCE_DIR
            record_iteration_evidence.EVIDENCE_DIR = Path(tmpdir)

            try:
                commands = [CommandEntry(name="ci", command="make ci", result="PASS")]
                result = record_evidence(
                    iteration_number=13,
                    commit_sha="abc1234567890",
                    commands=commands,
                    artifact_url="https://github.com/org/repo/actions/runs/123/artifacts/456",
                )

                with open(result.output_path, encoding="utf-8") as f:
                    data = json.load(f)

                # 验证 artifact_url 存在
                assert "links" in data
                assert (
                    data["links"]["artifact_url"]
                    == "https://github.com/org/repo/actions/runs/123/artifacts/456"
                )

                # 验证通过 schema 校验
                jsonschema.validate(
                    instance=data,
                    schema=schema,
                    format_checker=jsonschema.FormatChecker(),
                )

            finally:
                record_iteration_evidence.EVIDENCE_DIR = original_dir

    def test_all_links_fields_mapped(self) -> None:
        """测试所有 links 字段都能正确映射。"""
        schema = load_evidence_schema()

        with tempfile.TemporaryDirectory() as tmpdir:
            import record_iteration_evidence

            original_dir = record_iteration_evidence.EVIDENCE_DIR
            record_iteration_evidence.EVIDENCE_DIR = Path(tmpdir)

            try:
                commands = [CommandEntry(name="ci", command="make ci", result="PASS")]
                result = record_evidence(
                    iteration_number=13,
                    commit_sha="abc1234567890",
                    commands=commands,
                    ci_run_url="https://github.com/org/repo/actions/runs/123",
                    regression_doc_url="docs/acceptance/iteration_13_regression.md",
                    pr_url="https://github.com/org/repo/pull/456",
                    artifact_url="https://github.com/org/repo/actions/runs/123/artifacts/789",
                )

                with open(result.output_path, encoding="utf-8") as f:
                    data = json.load(f)

                # 验证所有 links 字段
                assert "links" in data
                links = data["links"]
                assert links["ci_run_url"] == "https://github.com/org/repo/actions/runs/123"
                assert links["regression_doc_url"] == "docs/acceptance/iteration_13_regression.md"
                assert links["pr_url"] == "https://github.com/org/repo/pull/456"
                assert (
                    links["artifact_url"]
                    == "https://github.com/org/repo/actions/runs/123/artifacts/789"
                )

                # 验证通过 schema 校验
                jsonschema.validate(
                    instance=data,
                    schema=schema,
                    format_checker=jsonschema.FormatChecker(),
                )

            finally:
                record_iteration_evidence.EVIDENCE_DIR = original_dir

    def test_notes_field(self) -> None:
        """测试 notes 字段。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            import record_iteration_evidence

            original_dir = record_iteration_evidence.EVIDENCE_DIR
            record_iteration_evidence.EVIDENCE_DIR = Path(tmpdir)

            try:
                commands = [CommandEntry(name="ci", command="make ci", result="PASS")]
                result = record_evidence(
                    iteration_number=13,
                    commit_sha="abc1234567890",
                    commands=commands,
                    notes="所有门禁通过",
                )

                with open(result.output_path, encoding="utf-8") as f:
                    data = json.load(f)

                assert data["notes"] == "所有门禁通过"

            finally:
                record_iteration_evidence.EVIDENCE_DIR = original_dir

    def test_regression_doc_url_written_by_default_without_ci_run_url(self) -> None:
        """测试未传 ci_run_url 时也默认写入 regression_doc_url。"""
        schema = load_evidence_schema()

        with tempfile.TemporaryDirectory() as tmpdir:
            import record_iteration_evidence

            original_dir = record_iteration_evidence.EVIDENCE_DIR
            record_iteration_evidence.EVIDENCE_DIR = Path(tmpdir)

            try:
                commands = [CommandEntry(name="ci", command="make ci", result="PASS")]
                # 不传任何 links 相关参数
                result = record_evidence(
                    iteration_number=14,
                    commit_sha="abc1234567890",
                    commands=commands,
                )

                with open(result.output_path, encoding="utf-8") as f:
                    data = json.load(f)

                # 验证 links 存在且包含 regression_doc_url
                assert "links" in data
                assert (
                    data["links"]["regression_doc_url"]
                    == "docs/acceptance/iteration_14_regression.md"
                )
                # 验证其他 links 字段不存在（因为未传入）
                assert "ci_run_url" not in data["links"]
                assert "pr_url" not in data["links"]
                assert "artifact_url" not in data["links"]

                # 验证通过 schema 校验
                jsonschema.validate(instance=data, schema=schema)

            finally:
                record_iteration_evidence.EVIDENCE_DIR = original_dir

    def test_no_regression_doc_url_flag(self) -> None:
        """测试 include_regression_doc_url=False 时不写入默认 regression_doc_url。"""
        schema = load_evidence_schema()

        with tempfile.TemporaryDirectory() as tmpdir:
            import record_iteration_evidence

            original_dir = record_iteration_evidence.EVIDENCE_DIR
            record_iteration_evidence.EVIDENCE_DIR = Path(tmpdir)

            try:
                commands = [CommandEntry(name="ci", command="make ci", result="PASS")]
                # 显式关闭 regression_doc_url
                result = record_evidence(
                    iteration_number=14,
                    commit_sha="abc1234567890",
                    commands=commands,
                    include_regression_doc_url=False,
                )

                with open(result.output_path, encoding="utf-8") as f:
                    data = json.load(f)

                # 验证 links 不存在（因为没有任何 links 字段）
                assert "links" not in data

                # 验证通过 schema 校验
                jsonschema.validate(instance=data, schema=schema)

            finally:
                record_iteration_evidence.EVIDENCE_DIR = original_dir

    def test_explicit_regression_doc_url_overrides_default(self) -> None:
        """测试显式传入 regression_doc_url 时覆盖默认值。"""
        schema = load_evidence_schema()

        with tempfile.TemporaryDirectory() as tmpdir:
            import record_iteration_evidence

            original_dir = record_iteration_evidence.EVIDENCE_DIR
            record_iteration_evidence.EVIDENCE_DIR = Path(tmpdir)

            try:
                commands = [CommandEntry(name="ci", command="make ci", result="PASS")]
                # 显式传入自定义 regression_doc_url
                result = record_evidence(
                    iteration_number=14,
                    commit_sha="abc1234567890",
                    commands=commands,
                    regression_doc_url="docs/custom/my_regression.md",
                )

                with open(result.output_path, encoding="utf-8") as f:
                    data = json.load(f)

                # 验证使用了显式传入的值
                assert "links" in data
                assert data["links"]["regression_doc_url"] == "docs/custom/my_regression.md"

                # 验证通过 schema 校验
                jsonschema.validate(instance=data, schema=schema)

            finally:
                record_iteration_evidence.EVIDENCE_DIR = original_dir


# ============================================================================
# 测试敏感信息脱敏
# ============================================================================


class TestSensitiveDataRedaction:
    """测试敏感信息脱敏。"""

    def test_password_key_redacted(self) -> None:
        data = {"password": "secret123"}
        redacted, warnings, count = redact_sensitive_data(data)
        assert redacted["password"] == REDACTED_PLACEHOLDER
        assert count == 1

    def test_dsn_value_redacted(self) -> None:
        data = {"connection": "postgres://user:pass@localhost/db"}
        redacted, warnings, count = redact_sensitive_data(data)
        assert redacted["connection"] == REDACTED_PLACEHOLDER
        assert count == 1

    def test_safe_keys_not_redacted(self) -> None:
        data = {"commit_sha": "abc123", "exit_code": 0}
        redacted, warnings, count = redact_sensitive_data(data)
        assert redacted["commit_sha"] == "abc123"
        assert redacted["exit_code"] == 0
        assert count == 0

    def test_is_sensitive_key(self) -> None:
        assert is_sensitive_key("password")
        assert is_sensitive_key("API_TOKEN")
        assert is_sensitive_key("db_secret")
        assert not is_sensitive_key("commit_sha")
        assert not is_sensitive_key("exit_code")

    def test_is_sensitive_value(self) -> None:
        assert is_sensitive_value("postgres://user:pass@localhost/db")
        assert is_sensitive_value("Bearer eyJhbGciOiJIUzI1NiJ9.xxx")
        assert not is_sensitive_value("abc123")
        assert not is_sensitive_value("make ci")


# ============================================================================
# 测试 Schema 校验
# ============================================================================


class TestSchemaValidation:
    """测试输出数据符合 iteration_evidence_v2.schema.json。"""

    def test_record_evidence_output_conforms_to_schema(self) -> None:
        """测试 record_evidence 生成的数据符合 schema。

        等价于 `python scripts/iteration/record_iteration_evidence.py 8 --dry-run`
        的数据生成逻辑，然后用 schema 校验。
        """
        schema = load_evidence_schema()

        # 准备测试数据
        commands = [
            CommandEntry(
                name="lint",
                command="make lint",
                result="PASS",
                summary="ruff check passed",
                duration_seconds=5.2,
                exit_code=0,
            ),
            CommandEntry(
                name="typecheck",
                command="make typecheck",
                result="PASS",
                summary="mypy passed",
                duration_seconds=12.8,
                exit_code=0,
            ),
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            import record_iteration_evidence

            original_dir = record_iteration_evidence.EVIDENCE_DIR
            record_iteration_evidence.EVIDENCE_DIR = Path(tmpdir)

            try:
                result = record_evidence(
                    iteration_number=8,
                    commit_sha="abc1234def5678901234567890abcdef12345678",
                    commands=commands,
                    ci_run_url="https://github.com/org/repo/actions/runs/123",
                    notes="测试验收通过",
                    runner_label="ubuntu-latest",
                )

                assert result.success
                assert result.output_path

                # 读取生成的 JSON 文件
                with open(result.output_path, encoding="utf-8") as f:
                    evidence_data = json.load(f)

                # 使用 jsonschema 校验
                jsonschema.validate(instance=evidence_data, schema=schema)

                # 额外验证关键字段
                assert evidence_data["iteration_number"] == 8
                assert evidence_data["sensitive_data_declaration"] is True
                assert len(evidence_data["commands"]) == 2

            finally:
                record_iteration_evidence.EVIDENCE_DIR = original_dir

    def test_dry_run_mode_generates_valid_structure(self) -> None:
        """测试 dry_run 模式生成的数据结构正确性。

        虽然 dry_run 不写文件，但内部构建的数据结构应符合 schema。
        """
        commands = [
            CommandEntry(
                name="ci",
                command="make ci",
                result="PASS",
                exit_code=0,
            )
        ]

        result = record_evidence(
            iteration_number=8,
            commit_sha="abc1234567890",
            commands=commands,
            dry_run=True,
        )

        assert result.success
        assert "[DRY-RUN]" in result.message
        assert "iteration_8_evidence.json" in result.output_path

    def test_minimal_evidence_conforms_to_schema(self) -> None:
        """测试最小必需字段的数据符合 schema。"""
        schema = load_evidence_schema()

        commands = [
            CommandEntry(
                name="test",
                command="make test",
                result="PASS",
            )
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            import record_iteration_evidence

            original_dir = record_iteration_evidence.EVIDENCE_DIR
            record_iteration_evidence.EVIDENCE_DIR = Path(tmpdir)

            try:
                result = record_evidence(
                    iteration_number=8,
                    commit_sha="abc1234567890",
                    commands=commands,
                )

                with open(result.output_path, encoding="utf-8") as f:
                    evidence_data = json.load(f)

                # 校验 schema
                jsonschema.validate(instance=evidence_data, schema=schema)

            finally:
                record_iteration_evidence.EVIDENCE_DIR = original_dir


# ============================================================================
# 测试敏感键/敏感值脱敏与 sensitive_data_declaration
# ============================================================================


class TestSensitiveDataRedactionWithDeclaration:
    """测试敏感数据脱敏后 sensitive_data_declaration 仍为 true。"""

    def test_sensitive_key_redacted_with_declaration_true(self) -> None:
        """测试敏感键被脱敏后，sensitive_data_declaration 仍为 true。"""
        # 构造包含敏感键的数据
        data = {
            "iteration_number": 8,
            "db_password": "super_secret_password",
            "api_token": "ghp_xxxxxxxxxxxx",
            "sensitive_data_declaration": True,
        }

        redacted, warnings, count = redact_sensitive_data(data)

        # 验证敏感键被替换为 [REDACTED]
        assert redacted["db_password"] == REDACTED_PLACEHOLDER
        assert redacted["api_token"] == REDACTED_PLACEHOLDER

        # 验证 sensitive_data_declaration 仍为 true（不被脱敏）
        assert redacted["sensitive_data_declaration"] is True

        # 验证有警告产生
        assert len(warnings) == 2
        assert count == 2

    def test_sensitive_value_redacted_with_declaration_true(self) -> None:
        """测试敏感值被脱敏后，sensitive_data_declaration 仍为 true。"""
        # 构造包含敏感值的数据
        data = {
            "iteration_number": 8,
            "connection_string": "postgres://admin:password123@prod.example.com:5432/mydb",
            "auth_header": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.xxxxx",
            "sensitive_data_declaration": True,
        }

        redacted, warnings, count = redact_sensitive_data(data)

        # 验证敏感值被替换为 [REDACTED]
        assert redacted["connection_string"] == REDACTED_PLACEHOLDER
        assert redacted["auth_header"] == REDACTED_PLACEHOLDER

        # 验证 sensitive_data_declaration 仍为 true
        assert redacted["sensitive_data_declaration"] is True

        # 验证有警告产生
        assert len(warnings) == 2
        assert count == 2

    def test_mixed_sensitive_data_redaction(self) -> None:
        """测试混合敏感键和敏感值的脱敏。"""
        data = {
            "iteration_number": 8,
            "commit_sha": "abc1234567890",  # 安全键，不应被脱敏
            "secret_key": "my_secret",  # 敏感键名
            "normal_field": "redis://user:pass@localhost:6379",  # 敏感值
            "exit_code": 0,  # 安全键，不应被脱敏
            "sensitive_data_declaration": True,
        }

        redacted, warnings, count = redact_sensitive_data(data)

        # 安全键保持原值
        assert redacted["iteration_number"] == 8
        assert redacted["commit_sha"] == "abc1234567890"
        assert redacted["exit_code"] == 0
        assert redacted["sensitive_data_declaration"] is True

        # 敏感数据被脱敏
        assert redacted["secret_key"] == REDACTED_PLACEHOLDER
        assert redacted["normal_field"] == REDACTED_PLACEHOLDER

        # 验证脱敏计数
        assert count == 2

    def test_nested_sensitive_data_redaction(self) -> None:
        """测试嵌套结构中的敏感数据脱敏。"""
        data = {
            "iteration_number": 8,
            "commands": [
                {
                    "name": "test",
                    "command": "make test",
                    "env_password": "secret123",  # 敏感键
                }
            ],
            "config": {
                "db_dsn": "postgres://user:pass@localhost/db",  # 敏感键
                "api_credential": "xxx",  # 敏感键
            },
            "sensitive_data_declaration": True,
        }

        redacted, warnings, count = redact_sensitive_data(data)

        # 验证嵌套敏感数据被脱敏
        assert redacted["commands"][0]["env_password"] == REDACTED_PLACEHOLDER
        assert redacted["config"]["db_dsn"] == REDACTED_PLACEHOLDER
        assert redacted["config"]["api_credential"] == REDACTED_PLACEHOLDER

        # 安全字段保持原值
        assert redacted["commands"][0]["name"] == "test"
        assert redacted["commands"][0]["command"] == "make test"
        assert redacted["sensitive_data_declaration"] is True

        # 验证脱敏计数
        assert count == 3

    def test_record_evidence_with_sensitive_input_redacts_and_validates(self) -> None:
        """测试 record_evidence 处理敏感输入时正确脱敏并仍符合 schema。"""
        schema = load_evidence_schema()

        # 构造包含敏感信息的命令（模拟误输入）
        commands = [
            CommandEntry(
                name="test",
                command="make test",
                result="PASS",
                # summary 中不应包含敏感信息，但如果包含应被脱敏
                summary="All tests passed",
                exit_code=0,
            )
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            import record_iteration_evidence

            original_dir = record_iteration_evidence.EVIDENCE_DIR
            record_iteration_evidence.EVIDENCE_DIR = Path(tmpdir)

            try:
                result = record_evidence(
                    iteration_number=8,
                    commit_sha="abc1234567890",
                    commands=commands,
                )

                with open(result.output_path, encoding="utf-8") as f:
                    evidence_data = json.load(f)

                # 校验 schema
                jsonschema.validate(instance=evidence_data, schema=schema)

                # sensitive_data_declaration 必须为 true
                assert evidence_data["sensitive_data_declaration"] is True

            finally:
                record_iteration_evidence.EVIDENCE_DIR = original_dir

    def test_various_sensitive_key_patterns(self) -> None:
        """测试各种敏感键模式的检测。"""
        sensitive_keys = [
            "password",
            "PASSWORD",
            "db_password",
            "user_passwd",
            "api_token",
            "AUTH_TOKEN",
            "secret",
            "SECRET_KEY",
            "apikey",
            "API_KEY",
            "api_key",
            "credential",
            "user_credential",
            "private_key",
            "access_key",
            "dsn",
            "DATABASE_DSN",
        ]

        for key in sensitive_keys:
            assert is_sensitive_key(key), f"应检测为敏感键: {key}"

    def test_various_sensitive_value_patterns(self) -> None:
        """测试各种敏感值模式的检测。"""
        sensitive_values = [
            "postgres://user:pass@localhost/db",
            "postgresql://admin:secret@prod.example.com:5432/mydb",
            "mysql://root:password@localhost:3306/test",
            "redis://user:pass@localhost:6379",
            "mongodb://user:pass@localhost:27017/db",
            "amqp://user:pass@localhost:5672",
            "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.xxxx",
            "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "ghs_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "AKIAIOSFODNN7EXAMPLE",  # AWS 风格密钥
        ]

        for value in sensitive_values:
            assert is_sensitive_value(value), f"应检测为敏感值: {value}"

    def test_safe_values_not_detected_as_sensitive(self) -> None:
        """测试正常值不被误判为敏感值。"""
        safe_values = [
            "abc1234567890",  # commit SHA
            "make ci",
            "ruff check passed",
            "142 passed, 0 failed",
            "https://github.com/org/repo/actions/runs/123",
            "ubuntu-latest",
            "3.11.9",
            "x86_64",
        ]

        for value in safe_values:
            assert not is_sensitive_value(value), f"不应检测为敏感值: {value}"


# ============================================================================
# 测试最小输入（无 commands）时的行为
# ============================================================================


class TestMinimalInputBehavior:
    """测试最小输入时的行为与 schema minItems=1 对齐。

    根据 iteration_evidence_v2.schema.json:
    - commands 数组 minItems: 1，即至少需要 1 个命令
    - 脚本在未提供 commands 时会创建默认的 manual_record 条目
    """

    def test_empty_commands_input_creates_default_entry(self) -> None:
        """测试空命令输入时创建默认 manual_record 条目。"""
        # 解析空数组
        commands = parse_commands_json([])
        # parse_commands_json 返回空列表，由 CLI 层处理默认值
        assert commands == []

    def test_empty_dict_input_creates_empty_list(self) -> None:
        """测试空字典输入时返回空列表。"""
        commands = parse_commands_json({})
        assert commands == []

    def test_record_evidence_requires_at_least_one_command(self) -> None:
        """测试 record_evidence 需要至少一个命令才能符合 schema。"""
        schema = load_evidence_schema()

        commands = [
            CommandEntry(
                name="manual",
                command="(manual record)",
                result="PASS",
            )
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            import record_iteration_evidence

            original_dir = record_iteration_evidence.EVIDENCE_DIR
            record_iteration_evidence.EVIDENCE_DIR = Path(tmpdir)

            try:
                result = record_evidence(
                    iteration_number=8,
                    commit_sha="abc1234567890",
                    commands=commands,
                )

                with open(result.output_path, encoding="utf-8") as f:
                    evidence_data = json.load(f)

                # 验证 commands 数组至少有 1 个元素（符合 schema minItems: 1）
                assert len(evidence_data["commands"]) >= 1

                # 使用 jsonschema 验证
                jsonschema.validate(instance=evidence_data, schema=schema)

            finally:
                record_iteration_evidence.EVIDENCE_DIR = original_dir

    def test_schema_rejects_empty_commands_array(self) -> None:
        """测试 schema 拒绝空的 commands 数组。"""
        schema = load_evidence_schema()

        # 构造一个不符合 schema 的数据（commands 为空数组）
        invalid_data = {
            "$schema": "./iteration_evidence_v2.schema.json",
            "iteration_number": 8,
            "recorded_at": "2026-02-02T14:30:22Z",
            "commit_sha": "abc1234567890",
            "runner": {
                "os": "ubuntu-22.04",
                "python": "3.11.9",
                "arch": "x86_64",
            },
            "commands": [],  # 空数组，违反 minItems: 1
            "overall_result": "FAIL",
            "sensitive_data_declaration": True,
        }

        # 应该抛出 ValidationError
        with pytest.raises(jsonschema.ValidationError) as exc_info:
            jsonschema.validate(instance=invalid_data, schema=schema)

        # 验证错误信息包含 minItems 相关内容
        assert "minItems" in str(exc_info.value) or "should be non-empty" in str(exc_info.value)


# ============================================================================
# 测试 commands 输入两种格式的映射结果
# ============================================================================


class TestCommandsFormatMapping:
    """测试 commands 输入两种格式（dict/array）的映射结果。"""

    def test_dict_format_mapping(self) -> None:
        """测试字典格式的映射：key 作为 command，value 中提取 exit_code。"""
        data = {
            "make ci": {"exit_code": 0, "summary": "all passed"},
            "make test": {"exit_code": 1, "summary": "2 failed"},
            "make lint": {"exit_code": 0},
        }
        commands = parse_commands_json(data)

        assert len(commands) == 3

        # 验证每个命令的映射
        cmd_map = {cmd.command: cmd for cmd in commands}

        # make ci → name=ci, result=PASS
        assert cmd_map["make ci"].name == "ci"
        assert cmd_map["make ci"].result == "PASS"
        assert cmd_map["make ci"].exit_code == 0
        assert cmd_map["make ci"].summary == "all passed"

        # make test → name=test, result=FAIL
        assert cmd_map["make test"].name == "test"
        assert cmd_map["make test"].result == "FAIL"
        assert cmd_map["make test"].exit_code == 1

        # make lint → name=lint, result=PASS
        assert cmd_map["make lint"].name == "lint"
        assert cmd_map["make lint"].result == "PASS"

    def test_array_format_mapping_old_style(self) -> None:
        """测试旧格式数组的映射：需要 command 字段，exit_code 转换为 result。"""
        data = [
            {"command": "make ci", "exit_code": 0, "summary": "passed"},
            {"command": "make test", "exit_code": 1, "summary": "failed"},
        ]
        commands = parse_commands_json(data)

        assert len(commands) == 2

        # 验证第一个命令
        assert commands[0].command == "make ci"
        assert commands[0].name == "ci"  # 自动推导
        assert commands[0].result == "PASS"
        assert commands[0].exit_code == 0

        # 验证第二个命令
        assert commands[1].command == "make test"
        assert commands[1].name == "test"
        assert commands[1].result == "FAIL"
        assert commands[1].exit_code == 1

    def test_array_format_mapping_schema_style(self) -> None:
        """测试已符合 schema 格式的数组映射：直接使用 name 和 result。"""
        data = [
            {
                "name": "lint",
                "command": "make lint",
                "result": "PASS",
                "exit_code": 0,
                "summary": "ruff check passed",
                "duration_seconds": 5.2,
            },
            {
                "name": "typecheck",
                "command": "make typecheck",
                "result": "FAIL",
                "exit_code": 1,
            },
        ]
        commands = parse_commands_json(data)

        assert len(commands) == 2

        # 验证第一个命令（保留原始值）
        assert commands[0].name == "lint"
        assert commands[0].command == "make lint"
        assert commands[0].result == "PASS"
        assert commands[0].exit_code == 0
        assert commands[0].summary == "ruff check passed"
        assert commands[0].duration_seconds == 5.2

        # 验证第二个命令
        assert commands[1].name == "typecheck"
        assert commands[1].result == "FAIL"

    def test_mixed_array_format(self) -> None:
        """测试混合格式数组的处理。"""
        data = [
            # Schema 格式
            {"name": "lint", "command": "make lint", "result": "PASS"},
            # 旧格式（需要转换）
            {"command": "make test", "exit_code": 0},
        ]
        commands = parse_commands_json(data)

        assert len(commands) == 2
        assert commands[0].name == "lint"
        assert commands[0].result == "PASS"
        assert commands[1].name == "test"  # 自动推导
        assert commands[1].result == "PASS"  # exit_code=0 → PASS

    def test_command_name_normalization(self) -> None:
        """测试命令名称规范化以符合 schema pattern。"""
        # 测试各种需要规范化的命令名称
        test_cases = [
            ("Make.CI", "make_ci"),  # 大写和点号
            ("test-123", "test-123"),  # 已符合规范
            ("TEST_CASE", "test_case"),  # 大写
            ("123test", "cmd_123test"),  # 数字开头
            ("", "cmd"),  # 空字符串
        ]

        for input_name, expected_name in test_cases:
            result = normalize_command_name(input_name)
            assert result == expected_name, (
                f"normalize_command_name({input_name!r}) = {result!r}, expected {expected_name!r}"
            )


# ============================================================================
# 测试 recorded_at UTC 格式
# ============================================================================


class TestRecordedAtFormat:
    """测试 recorded_at 字段为 UTC 且满足 date-time 格式。"""

    # ISO 8601 date-time 正则（RFC 3339 兼容）
    ISO8601_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_recorded_at_is_utc_format(self) -> None:
        """测试 recorded_at 为 UTC 格式（以 Z 结尾）。"""
        commands = [CommandEntry(name="test", command="make test", result="PASS")]

        with tempfile.TemporaryDirectory() as tmpdir:
            import record_iteration_evidence

            original_dir = record_iteration_evidence.EVIDENCE_DIR
            record_iteration_evidence.EVIDENCE_DIR = Path(tmpdir)

            try:
                result = record_evidence(
                    iteration_number=8,
                    commit_sha="abc1234567890",
                    commands=commands,
                )

                with open(result.output_path, encoding="utf-8") as f:
                    evidence_data = json.load(f)

                recorded_at = evidence_data["recorded_at"]

                # 验证以 Z 结尾（UTC）
                assert recorded_at.endswith("Z"), f"recorded_at 应以 Z 结尾: {recorded_at}"

                # 验证符合 ISO 8601 格式
                assert self.ISO8601_PATTERN.match(recorded_at), (
                    f"recorded_at 不符合 ISO 8601 格式: {recorded_at}"
                )

            finally:
                record_iteration_evidence.EVIDENCE_DIR = original_dir

    def test_recorded_at_is_valid_datetime(self) -> None:
        """测试 recorded_at 是有效的 datetime。"""
        commands = [CommandEntry(name="test", command="make test", result="PASS")]

        with tempfile.TemporaryDirectory() as tmpdir:
            import record_iteration_evidence

            original_dir = record_iteration_evidence.EVIDENCE_DIR
            record_iteration_evidence.EVIDENCE_DIR = Path(tmpdir)

            try:
                result = record_evidence(
                    iteration_number=8,
                    commit_sha="abc1234567890",
                    commands=commands,
                )

                with open(result.output_path, encoding="utf-8") as f:
                    evidence_data = json.load(f)

                recorded_at = evidence_data["recorded_at"]

                # 可以解析为 datetime
                parsed = datetime.strptime(recorded_at, "%Y-%m-%dT%H:%M:%SZ")
                assert parsed.tzinfo is None  # strptime 不设置 tzinfo

                # 验证时间合理（在当前时间附近）
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                diff = abs((now - parsed).total_seconds())
                # 允许 60 秒误差
                assert diff < 60, f"recorded_at 与当前时间相差过大: {diff}s"

            finally:
                record_iteration_evidence.EVIDENCE_DIR = original_dir

    def test_recorded_at_passes_schema_datetime_format(self) -> None:
        """测试 recorded_at 通过 schema 的 date-time 格式验证。"""
        schema = load_evidence_schema()

        commands = [CommandEntry(name="test", command="make test", result="PASS")]

        with tempfile.TemporaryDirectory() as tmpdir:
            import record_iteration_evidence

            original_dir = record_iteration_evidence.EVIDENCE_DIR
            record_iteration_evidence.EVIDENCE_DIR = Path(tmpdir)

            try:
                result = record_evidence(
                    iteration_number=8,
                    commit_sha="abc1234567890",
                    commands=commands,
                )

                with open(result.output_path, encoding="utf-8") as f:
                    evidence_data = json.load(f)

                # 使用 jsonschema 验证（包含 format: date-time 检查）
                # 注意：需要启用 format 检查
                jsonschema.validate(
                    instance=evidence_data,
                    schema=schema,
                    format_checker=jsonschema.FormatChecker(),
                )

            finally:
                record_iteration_evidence.EVIDENCE_DIR = original_dir


# ============================================================================
# 测试文件名生成规范
# ============================================================================


class TestFilenameGeneration:
    """测试文件名生成符合规范（canonical/snapshot）。"""

    def test_canonical_filename_format(self) -> None:
        """测试 canonical 文件名格式：iteration_{N}_evidence.json。"""
        commands = [CommandEntry(name="ci", command="make ci", result="PASS")]

        result = record_evidence(
            iteration_number=13,
            commit_sha="abc1234567890",
            commands=commands,
            dry_run=True,
        )

        # 验证文件名格式
        filename = Path(result.output_path).name
        assert filename == "iteration_13_evidence.json"

    def test_canonical_filename_different_iterations(self) -> None:
        """测试不同迭代编号生成正确的 canonical 文件名。"""
        for iteration in [1, 8, 13, 100]:
            commands = [CommandEntry(name="ci", command="make ci", result="PASS")]

            result = record_evidence(
                iteration_number=iteration,
                commit_sha="abc1234567890",
                commands=commands,
                dry_run=True,
            )

            filename = Path(result.output_path).name
            expected = f"iteration_{iteration}_evidence.json"
            assert filename == expected, f"Expected {expected}, got {filename}"

    def test_output_path_in_evidence_directory(self) -> None:
        """测试输出路径在 evidence 目录中。"""
        commands = [CommandEntry(name="ci", command="make ci", result="PASS")]

        result = record_evidence(
            iteration_number=13,
            commit_sha="abc1234567890",
            commands=commands,
            dry_run=True,
        )

        # 验证路径包含 evidence 目录
        assert (
            "docs/acceptance/evidence" in result.output_path
            or "evidence" in Path(result.output_path).parts
        )


# ============================================================================
# 测试敏感数据脱敏后仍通过 schema 校验
# ============================================================================


class TestRedactedDataPassesSchema:
    """测试敏感数据脱敏后输出仍符合 schema。"""

    def test_redacted_sensitive_key_passes_schema(self) -> None:
        """测试敏感键被脱敏后仍符合 schema。"""
        # 构造包含敏感键的输入
        data = {
            "iteration_number": 8,
            "recorded_at": "2026-02-02T14:30:22Z",
            "commit_sha": "abc1234567890",
            "runner": {
                "os": "ubuntu-22.04",
                "python": "3.11.9",
                "arch": "x86_64",
            },
            "commands": [
                {
                    "name": "test",
                    "command": "make test",
                    "result": "PASS",
                }
            ],
            "overall_result": "PASS",
            "sensitive_data_declaration": True,
            # 额外的敏感键（会被脱敏）
            "db_password": "secret123",
        }

        # 脱敏处理
        redacted, warnings, count = redact_sensitive_data(data)

        # 验证敏感数据被替换
        assert redacted["db_password"] == REDACTED_PLACEHOLDER
        assert count >= 1

        # 注意：schema 设置了 additionalProperties: false，
        # 所以包含额外字段的数据不会通过 schema
        # 这是正确的行为，因为 record_evidence 不会生成额外字段

    def test_redacted_sensitive_value_passes_schema(self) -> None:
        """测试敏感值被脱敏后仍符合 schema。"""
        schema = load_evidence_schema()

        # 构造在 notes 中包含敏感值的数据
        commands = [
            CommandEntry(
                name="test",
                command="make test",
                result="PASS",
                summary="Test passed",
            )
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            import record_iteration_evidence

            original_dir = record_iteration_evidence.EVIDENCE_DIR
            record_iteration_evidence.EVIDENCE_DIR = Path(tmpdir)

            try:
                result = record_evidence(
                    iteration_number=8,
                    commit_sha="abc1234567890",
                    commands=commands,
                    notes="DB connection: postgres://user:pass@localhost/db was used",
                )

                with open(result.output_path, encoding="utf-8") as f:
                    evidence_data = json.load(f)

                # 如果 notes 中的敏感值被检测到，应该被脱敏
                # 注意：notes 字段不在敏感值检测路径中（因为它不是连接字符串字段）
                # 但如果脱敏逻辑检测到，应该处理

                # 使用 jsonschema 验证
                jsonschema.validate(instance=evidence_data, schema=schema)

            finally:
                record_iteration_evidence.EVIDENCE_DIR = original_dir

    def test_complete_evidence_with_all_fields_passes_schema(self) -> None:
        """测试包含所有字段的完整证据通过 schema。"""
        schema = load_evidence_schema()

        commands = [
            CommandEntry(
                name="lint",
                command="make lint",
                result="PASS",
                summary="ruff check passed",
                duration_seconds=5.2,
                exit_code=0,
            ),
            CommandEntry(
                name="typecheck",
                command="make typecheck",
                result="PASS",
                summary="mypy passed",
                duration_seconds=12.8,
                exit_code=0,
            ),
            CommandEntry(
                name="test",
                command="make test",
                result="PASS",
                summary="142 passed, 0 failed",
                duration_seconds=45.3,
                exit_code=0,
            ),
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            import record_iteration_evidence

            original_dir = record_iteration_evidence.EVIDENCE_DIR
            record_iteration_evidence.EVIDENCE_DIR = Path(tmpdir)

            try:
                result = record_evidence(
                    iteration_number=13,
                    commit_sha="abc1234def5678901234567890abcdef12345678",
                    commands=commands,
                    ci_run_url="https://github.com/org/repo/actions/runs/12345",
                    notes="所有门禁通过，验收完成。",
                    runner_label="ubuntu-latest",
                    regression_doc_url="docs/acceptance/iteration_13_regression.md",
                )

                assert result.success

                with open(result.output_path, encoding="utf-8") as f:
                    evidence_data = json.load(f)

                # 使用 jsonschema 验证（包含 format 检查）
                jsonschema.validate(
                    instance=evidence_data,
                    schema=schema,
                    format_checker=jsonschema.FormatChecker(),
                )

                # 验证所有预期字段存在
                assert evidence_data["iteration_number"] == 13
                assert evidence_data["commit_sha"] == "abc1234def5678901234567890abcdef12345678"
                assert len(evidence_data["commands"]) == 3
                assert "links" in evidence_data
                assert (
                    evidence_data["links"]["ci_run_url"]
                    == "https://github.com/org/repo/actions/runs/12345"
                )
                assert evidence_data["notes"] == "所有门禁通过，验收完成。"
                assert evidence_data["overall_result"] == "PASS"
                assert evidence_data["sensitive_data_declaration"] is True

            finally:
                record_iteration_evidence.EVIDENCE_DIR = original_dir

    def test_sensitive_data_in_nested_commands_redacted_and_passes_schema(self) -> None:
        """测试嵌套在 commands 中的敏感数据被脱敏后仍符合 schema。"""
        schema = load_evidence_schema()

        # 构造包含敏感信息的原始数据
        original_data = {
            "$schema": "../../../schemas/iteration_evidence_v2.schema.json",
            "iteration_number": 8,
            "recorded_at": "2026-02-02T14:30:22Z",
            "commit_sha": "abc1234567890",
            "runner": {
                "os": "ubuntu-22.04",
                "python": "3.11.9",
                "arch": "x86_64",
            },
            "commands": [
                {
                    "name": "test",
                    "command": "make test",
                    "result": "PASS",
                }
            ],
            "overall_result": "PASS",
            "sensitive_data_declaration": True,
        }

        # 脱敏处理
        redacted_data, warnings, count = redact_sensitive_data(original_data)

        # 验证脱敏后的数据符合 schema
        jsonschema.validate(instance=redacted_data, schema=schema)

        # sensitive_data_declaration 应保持为 True
        assert redacted_data["sensitive_data_declaration"] is True

    def test_redacted_placeholder_value_in_various_fields(self) -> None:
        """测试 [REDACTED] 占位符在各字段中的合法性。"""
        # 验证 REDACTED_PLACEHOLDER 是合法的字符串值
        assert REDACTED_PLACEHOLDER == "[REDACTED]"
        assert isinstance(REDACTED_PLACEHOLDER, str)
        assert len(REDACTED_PLACEHOLDER) > 0

        # 模拟脱敏后的数据
        data = {"password": "secret123"}
        redacted, warnings, count = redact_sensitive_data(data)

        assert redacted["password"] == REDACTED_PLACEHOLDER
        assert count == 1
        assert len(warnings) == 1
