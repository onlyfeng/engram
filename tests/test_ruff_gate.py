"""
测试 ruff 门禁检查脚本

测试覆盖:
1. ruff_metrics.py 的指标聚合逻辑
2. check_ruff_gate.py 的门禁逻辑
3. baseline 文件的读写
4. check_ruff_metrics_thresholds.py 的阈值检查逻辑
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.ci.check_ruff_gate import (
    check_future_baseline,
    load_baseline,
    parse_violation_key,
    save_baseline,
)
from scripts.ci.check_ruff_lint_island import (
    DEFAULT_SCAN_ROOT,
    PHASE3_EXTRA_RULES,
    LintIslandConfig,
    aggregate_violations,
    load_pyproject_config,
    resolve_phase,
    run_lint_island_check,
)
from scripts.ci.check_ruff_lint_island import CheckResult as LintIslandCheckResult
from scripts.ci.check_ruff_lint_island import (
    format_json_output as lint_island_format_json_output,
)
from scripts.ci.check_ruff_lint_island import (
    format_text_output as lint_island_format_text_output,
)
from scripts.ci.check_ruff_metrics_thresholds import CheckResult as ThresholdsCheckResult
from scripts.ci.check_ruff_metrics_thresholds import (
    ThresholdConfig,
    check_thresholds,
    load_config_from_env,
    merge_config,
)
from scripts.ci.check_ruff_metrics_thresholds import (
    format_json_output as thresholds_format_json_output,
)
from scripts.ci.check_ruff_metrics_thresholds import (
    format_output as thresholds_format_output,
)
from scripts.ci.ruff_metrics import (
    aggregate_by_code,
    aggregate_by_directory,
    aggregate_by_file,
)
from tests.ci.helpers.subprocess_env import get_subprocess_env

if TYPE_CHECKING:
    import pytest

PROJECT_ROOT = Path(__file__).parent.parent


class TestRuffMetrics:
    """测试 ruff_metrics.py"""

    def test_aggregate_by_code(self) -> None:
        """测试按 code 聚合"""
        violations = [
            {"code": "F401", "fix": {"edits": []}, "message": "unused import"},
            {"code": "F401", "fix": {"edits": []}, "message": "unused import"},
            {"code": "E501", "fix": None, "message": "line too long"},
        ]

        result = aggregate_by_code(violations)

        assert "F401" in result
        assert result["F401"]["count"] == 2
        assert result["F401"]["fixable"] == 2
        assert "E501" in result
        assert result["E501"]["count"] == 1
        assert result["E501"]["fixable"] == 0

    def test_aggregate_by_directory(self) -> None:
        """测试按目录聚合"""
        violations = [
            {"filename": "src/engram/gateway/app.py", "fix": None},
            {"filename": "src/engram/gateway/routes.py", "fix": {"edits": []}},
            {"filename": "tests/gateway/test_app.py", "fix": None},
        ]

        result = aggregate_by_directory(violations)

        assert "src/engram/gateway/" in result
        assert result["src/engram/gateway/"]["count"] == 2
        assert result["src/engram/gateway/"]["fixable"] == 1
        assert "tests/gateway/" in result
        assert result["tests/gateway/"]["count"] == 1

    def test_aggregate_by_file(self) -> None:
        """测试按文件聚合"""
        violations = [
            {"filename": "src/foo.py"},
            {"filename": "src/foo.py"},
            {"filename": "src/bar.py"},
        ]

        result = aggregate_by_file(violations)

        assert result["src/foo.py"] == 2
        assert result["src/bar.py"] == 1
        # 确保按 count 降序排序
        keys = list(result.keys())
        assert keys[0] == "src/foo.py"


class TestCheckRuffGate:
    """测试 check_ruff_gate.py"""

    def test_load_baseline_not_exists(self, tmp_path: Path) -> None:
        """测试加载不存在的 baseline 文件"""
        result = load_baseline(tmp_path / "nonexistent.json")

        assert result["rules"] == []
        assert result["violations"] == {}

    def test_load_baseline_valid(self, tmp_path: Path) -> None:
        """测试加载有效的 baseline 文件"""
        baseline_file = tmp_path / "baseline.json"
        baseline_data = {
            "rules": ["E501"],
            "violations": {"E501": {"count": 2, "files": {"src/foo.py": [10, 20]}}},
        }
        baseline_file.write_text(json.dumps(baseline_data))

        result = load_baseline(baseline_file)

        assert result["rules"] == ["E501"]
        assert "E501" in result["violations"]

    def test_save_baseline(self, tmp_path: Path) -> None:
        """测试保存 baseline 文件"""
        violations = [
            {"code": "E501", "filename": "src/foo.py", "location": {"row": 10, "column": 1}},
            {"code": "E501", "filename": "src/foo.py", "location": {"row": 20, "column": 1}},
            {"code": "F401", "filename": "src/bar.py", "location": {"row": 5, "column": 1}},
        ]
        rules = ["E501"]
        baseline_file = tmp_path / "baseline.json"

        save_baseline(violations, rules, baseline_file)

        assert baseline_file.exists()
        data = json.loads(baseline_file.read_text())
        assert data["rules"] == ["E501"]
        assert "E501" in data["violations"]
        assert data["violations"]["E501"]["count"] == 2
        # F401 不在 rules 中，不应该被记录
        assert "F401" not in data["violations"]

    def test_check_future_baseline_pass(self) -> None:
        """测试 future-baseline 检查通过"""
        violations = [
            {"code": "E501", "filename": "src/foo.py", "location": {"row": 10, "column": 1}},
        ]
        baseline = {
            "rules": ["E501"],
            "violations": {"E501": {"count": 1, "files": {"src/foo.py": [10]}}},
        }

        passed, new_violations = check_future_baseline(violations, baseline)

        assert passed is True
        assert len(new_violations) == 0

    def test_check_future_baseline_fail(self) -> None:
        """测试 future-baseline 检查失败（有新增）"""
        violations = [
            {"code": "E501", "filename": "src/foo.py", "location": {"row": 10, "column": 1}},
            {
                "code": "E501",
                "filename": "src/foo.py",
                "location": {"row": 30, "column": 1},
            },  # 新增
        ]
        baseline = {
            "rules": ["E501"],
            "violations": {"E501": {"count": 1, "files": {"src/foo.py": [10]}}},
        }

        passed, new_violations = check_future_baseline(violations, baseline)

        assert passed is False
        assert len(new_violations) == 1
        assert new_violations[0]["location"]["row"] == 30

    def test_check_future_baseline_ignore_non_baseline_rules(self) -> None:
        """测试 future-baseline 忽略不在 baseline 中的规则"""
        violations = [
            {
                "code": "F401",
                "filename": "src/foo.py",
                "location": {"row": 5, "column": 1},
            },  # 不在 baseline 中
        ]
        baseline = {"rules": ["E501"], "violations": {}}

        passed, new_violations = check_future_baseline(violations, baseline)

        # F401 不在 baseline rules 中，应该被忽略
        assert passed is True
        assert len(new_violations) == 0

    def test_parse_violation_key(self) -> None:
        """测试 violation key 生成"""
        violation = {"filename": "src/foo.py", "location": {"row": 10, "column": 5}, "code": "E501"}

        key = parse_violation_key(violation)

        assert key == "src/foo.py:10:5:E501"


class TestRuffGateIntegration:
    """集成测试"""

    def test_metrics_script_runs(self) -> None:
        """测试 ruff_metrics.py 脚本能够运行"""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            output_file = f.name

        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scripts.ci.ruff_metrics",
                    "--output",
                    output_file,
                    "--scan-paths",
                    "src/",
                ],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=60,
                env=get_subprocess_env(PROJECT_ROOT),
            )
            # 脚本应该返回 0（即使有 violations）
            assert result.returncode == 0

            # 检查输出文件
            with open(output_file) as f:
                data = json.load(f)
            assert "summary" in data
            assert "by_code" in data
            assert "by_directory" in data
        finally:
            Path(output_file).unlink(missing_ok=True)

    def test_gate_script_help(self) -> None:
        """测试 check_ruff_gate.py --help 能运行"""
        result = subprocess.run(
            [sys.executable, "-m", "scripts.ci.check_ruff_gate", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
            env=get_subprocess_env(PROJECT_ROOT),
            cwd=PROJECT_ROOT,
        )

        assert result.returncode == 0
        assert "ruff 门禁检查" in result.stdout or "gate" in result.stdout


# ============================================================================
# Lint-Island 检查测试
# ============================================================================


class TestLintIslandConfig:
    """测试 Lint-Island 配置加载"""

    def test_load_pyproject_config(self, tmp_path: Path) -> None:
        """测试从 pyproject.toml 加载配置"""
        # 创建测试 pyproject.toml
        pyproject_content = """
[tool.engram.ruff]
lint_island_paths = [
    "src/engram/gateway/di.py",
    "src/engram/gateway/services/",
]
p1_rules = ["B", "UP", "SIM"]
current_phase = 1
"""
        pyproject_path = tmp_path / "pyproject.toml"
        pyproject_path.write_text(pyproject_content)

        config = load_pyproject_config(tmp_path)

        assert len(config.lint_island_paths) == 2
        assert "src/engram/gateway/di.py" in config.lint_island_paths
        assert config.p1_rules == ["B", "UP", "SIM"]
        assert config.current_phase == 1

    def test_load_pyproject_config_missing_file(self, tmp_path: Path) -> None:
        """测试 pyproject.toml 不存在时返回默认配置"""
        config = load_pyproject_config(tmp_path)

        assert config.lint_island_paths == []
        assert config.current_phase == 0


class TestLintIslandPhaseResolution:
    """测试 Phase 解析优先级"""

    def test_phase_priority_cli(self) -> None:
        """测试 CLI 参数优先级最高"""
        # 设置环境变量
        old_env = os.environ.get("ENGRAM_RUFF_PHASE")
        os.environ["ENGRAM_RUFF_PHASE"] = "2"

        try:
            phase, source = resolve_phase(cli_phase=1, pyproject_phase=3)
            assert phase == 1
            assert source == "cli"
        finally:
            if old_env is not None:
                os.environ["ENGRAM_RUFF_PHASE"] = old_env
            else:
                os.environ.pop("ENGRAM_RUFF_PHASE", None)

    def test_phase_priority_env(self) -> None:
        """测试环境变量优先级"""
        old_env = os.environ.get("ENGRAM_RUFF_PHASE")
        os.environ["ENGRAM_RUFF_PHASE"] = "2"

        try:
            phase, source = resolve_phase(cli_phase=None, pyproject_phase=1)
            assert phase == 2
            assert source == "env"
        finally:
            if old_env is not None:
                os.environ["ENGRAM_RUFF_PHASE"] = old_env
            else:
                os.environ.pop("ENGRAM_RUFF_PHASE", None)

    def test_phase_priority_pyproject(self) -> None:
        """测试 pyproject.toml 优先级"""
        # 确保环境变量未设置
        old_env = os.environ.pop("ENGRAM_RUFF_PHASE", None)

        try:
            phase, source = resolve_phase(cli_phase=None, pyproject_phase=1)
            assert phase == 1
            assert source == "pyproject"
        finally:
            if old_env is not None:
                os.environ["ENGRAM_RUFF_PHASE"] = old_env

    def test_phase_priority_default(self) -> None:
        """测试默认值"""
        old_env = os.environ.pop("ENGRAM_RUFF_PHASE", None)

        try:
            phase, source = resolve_phase(cli_phase=None, pyproject_phase=0)
            assert phase == 0
            assert source == "default"
        finally:
            if old_env is not None:
                os.environ["ENGRAM_RUFF_PHASE"] = old_env


class TestLintIslandCheck:
    """测试 Lint-Island 检查逻辑"""

    def test_phase_0_skip(self) -> None:
        """测试 Phase 0 跳过检查"""
        config = LintIslandConfig(
            lint_island_paths=["src/engram/gateway/di.py"],
            p1_rules=["B", "UP"],
            current_phase=0,
        )

        result = run_lint_island_check(
            phase=0,
            phase_source="default",
            config=config,
            project_root=PROJECT_ROOT,
        )

        assert result.success is True
        assert result.skipped is True
        assert "Phase 0" in result.skip_reason

    def test_empty_lint_island_paths_skip(self) -> None:
        """测试空 lint_island_paths 跳过检查"""
        config = LintIslandConfig(
            lint_island_paths=[],
            p1_rules=["B", "UP"],
            current_phase=1,
        )

        result = run_lint_island_check(
            phase=1,
            phase_source="pyproject",
            config=config,
            project_root=PROJECT_ROOT,
        )

        assert result.success is True
        assert result.skipped is True
        assert "lint_island_paths 为空" in result.skip_reason

    def test_aggregate_violations(self) -> None:
        """测试 violation 聚合"""
        violations = [
            {"code": "B006", "filename": "src/foo.py"},
            {"code": "B006", "filename": "src/foo.py"},
            {"code": "UP035", "filename": "src/bar.py"},
        ]

        by_code, by_file = aggregate_violations(violations)

        assert by_code["B006"] == 2
        assert by_code["UP035"] == 1
        assert by_file["src/foo.py"] == 2
        assert by_file["src/bar.py"] == 1


class TestLintIslandOutput:
    """测试 Lint-Island 输出格式"""

    def test_json_output_format(self) -> None:
        """测试 JSON 输出格式"""
        result = LintIslandCheckResult(
            success=True,
            phase=1,
            phase_source="env",
            lint_island_paths=["src/engram/gateway/di.py"],
            p1_rules=["B", "UP"],
            violations=[],
            violation_count=0,
        )

        output = lint_island_format_json_output(result)
        parsed = json.loads(output)

        assert parsed["success"] is True
        assert parsed["phase"] == 1
        assert parsed["phase_source"] == "env"
        assert parsed["violation_count"] == 0

    def test_text_output_format(self) -> None:
        """测试文本输出格式"""
        result = LintIslandCheckResult(
            success=False,
            phase=1,
            phase_source="pyproject",
            lint_island_paths=["src/engram/gateway/di.py"],
            p1_rules=["B", "UP"],
            violations=[{"code": "B006", "filename": "src/foo.py", "location": {"row": 10}}],
            violation_count=1,
            by_code={"B006": 1},
        )

        output = lint_island_format_text_output(result)

        assert "Lint-Island" in output
        assert "Phase:" in output
        assert "[FAIL]" in output
        assert "B006" in output


class TestLintIslandScriptIntegration:
    """Lint-Island 脚本集成测试"""

    def test_lint_island_script_help(self) -> None:
        """测试 check_ruff_lint_island.py --help 能运行"""
        result = subprocess.run(
            [sys.executable, "-m", "scripts.ci.check_ruff_lint_island", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
            env=get_subprocess_env(PROJECT_ROOT),
            cwd=PROJECT_ROOT,
        )

        assert result.returncode == 0
        assert "Lint-Island" in result.stdout or "phase" in result.stdout.lower()

    def test_lint_island_script_phase_0(self) -> None:
        """测试 Phase 0 跳过检查"""
        result = subprocess.run(
            [sys.executable, "-m", "scripts.ci.check_ruff_lint_island", "--phase", "0"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            env=get_subprocess_env(PROJECT_ROOT),
        )

        assert result.returncode == 0
        assert "Phase 0" in result.stdout or "跳过" in result.stdout

    def test_lint_island_script_json_output(self) -> None:
        """测试 JSON 输出模式"""
        result = subprocess.run(
            [sys.executable, "-m", "scripts.ci.check_ruff_lint_island", "--phase", "0", "--json"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            env=get_subprocess_env(PROJECT_ROOT),
        )

        assert result.returncode == 0

        # 验证 JSON 输出
        data = json.loads(result.stdout)
        assert "success" in data
        assert "phase" in data
        assert data["phase"] == 0


# ============================================================================
# ruff 指标阈值检查测试
# ============================================================================


class TestRuffMetricsThresholdsConfig:
    """测试阈值配置加载"""

    def test_load_config_from_env(self, monkeypatch: "pytest.MonkeyPatch") -> None:
        """测试从环境变量加载配置"""
        monkeypatch.setenv("ENGRAM_RUFF_TOTAL_THRESHOLD", "100")
        monkeypatch.setenv("ENGRAM_NOQA_TOTAL_THRESHOLD", "50")
        monkeypatch.setenv("ENGRAM_RUFF_FAIL_ON_THRESHOLD", "true")

        config = load_config_from_env()

        assert config.total_threshold == 100
        assert config.noqa_threshold == 50
        assert config.fail_on_threshold is True

    def test_load_config_from_env_defaults(self, monkeypatch: "pytest.MonkeyPatch") -> None:
        """测试环境变量未设置时的默认值"""
        monkeypatch.delenv("ENGRAM_RUFF_TOTAL_THRESHOLD", raising=False)
        monkeypatch.delenv("ENGRAM_NOQA_TOTAL_THRESHOLD", raising=False)
        monkeypatch.delenv("ENGRAM_RUFF_FAIL_ON_THRESHOLD", raising=False)

        config = load_config_from_env()

        assert config.total_threshold is None
        assert config.noqa_threshold is None
        assert config.fail_on_threshold is False

    def test_merge_config_cli_priority(self) -> None:
        """测试 CLI 参数优先级"""
        env_config = ThresholdConfig(
            total_threshold=100,
            noqa_threshold=50,
            fail_on_threshold=False,
        )

        merged = merge_config(
            env_config,
            cli_total_threshold=200,
            cli_noqa_threshold=None,
            cli_fail_on_threshold="true",
        )

        assert merged.total_threshold == 200  # CLI 覆盖
        assert merged.noqa_threshold == 50  # 保持环境变量
        assert merged.fail_on_threshold is True  # CLI 覆盖


class TestRuffMetricsThresholdsCheck:
    """测试阈值检查逻辑"""

    def test_check_thresholds_pass(self) -> None:
        """测试阈值检查通过（未超阈值）"""
        metrics = {
            "summary": {
                "total_violations": 50,
                "total_files": 10,
                "total_fixable": 30,
            }
        }
        config = ThresholdConfig(
            total_threshold=100,
            noqa_threshold=None,
            fail_on_threshold=True,
        )

        result = check_thresholds(metrics, config)

        assert result.success is True
        assert result.total_exceeded is False
        assert result.total_violations == 50
        assert len(result.errors) == 0
        assert len(result.warnings) == 0

    def test_check_thresholds_exceed_warn_only(self) -> None:
        """测试超阈值但仅警告（fail_on_threshold=False）"""
        metrics = {
            "summary": {
                "total_violations": 150,
                "total_files": 20,
                "total_fixable": 100,
            }
        }
        config = ThresholdConfig(
            total_threshold=100,
            noqa_threshold=None,
            fail_on_threshold=False,  # 仅警告
        )

        result = check_thresholds(metrics, config)

        assert result.success is True  # 仍然成功
        assert result.total_exceeded is True
        assert len(result.warnings) == 1
        assert len(result.errors) == 0
        assert "超出阈值" in result.warnings[0]

    def test_check_thresholds_exceed_fail(self) -> None:
        """测试超阈值失败（fail_on_threshold=True）"""
        metrics = {
            "summary": {
                "total_violations": 150,
                "total_files": 20,
                "total_fixable": 100,
            }
        }
        config = ThresholdConfig(
            total_threshold=100,
            noqa_threshold=None,
            fail_on_threshold=True,  # 失败模式
        )

        result = check_thresholds(metrics, config)

        assert result.success is False
        assert result.total_exceeded is True
        assert len(result.errors) == 1
        assert len(result.warnings) == 0
        assert "超出阈值" in result.errors[0]

    def test_check_thresholds_no_limit(self) -> None:
        """测试无阈值限制"""
        metrics = {
            "summary": {
                "total_violations": 10000,  # 很高的数量
                "total_files": 100,
                "total_fixable": 5000,
            }
        }
        config = ThresholdConfig(
            total_threshold=None,  # 无限制
            noqa_threshold=None,
            fail_on_threshold=True,
        )

        result = check_thresholds(metrics, config)

        assert result.success is True
        assert result.total_exceeded is False
        assert len(result.errors) == 0
        assert len(result.warnings) == 0


class TestRuffMetricsThresholdsOutput:
    """测试输出格式"""

    def test_format_json_output(self) -> None:
        """测试 JSON 输出格式"""
        result = ThresholdsCheckResult(
            success=True,
            total_violations=50,
            total_threshold=100,
            total_exceeded=False,
            noqa_count=None,
            noqa_threshold=None,
            noqa_exceeded=False,
            warnings=[],
            errors=[],
        )
        config = ThresholdConfig(
            total_threshold=100,
            noqa_threshold=None,
            fail_on_threshold=False,
        )

        output = thresholds_format_json_output(result, config)
        parsed = json.loads(output)

        assert parsed["success"] is True
        assert parsed["total_violations"] == 50
        assert parsed["total_threshold"] == 100
        assert parsed["total_exceeded"] is False
        assert parsed["fail_on_threshold"] is False

    def test_format_text_output_success(self) -> None:
        """测试文本输出格式（成功）"""
        result = ThresholdsCheckResult(
            success=True,
            total_violations=50,
            total_threshold=100,
            total_exceeded=False,
            noqa_count=None,
            noqa_threshold=None,
            noqa_exceeded=False,
            warnings=[],
            errors=[],
        )
        config = ThresholdConfig(
            total_threshold=100,
            noqa_threshold=None,
            fail_on_threshold=False,
        )

        output = thresholds_format_output(result, config, verbose=True)

        assert "ruff 指标阈值检查" in output
        assert "[OK]" in output
        assert "总违规数: 50" in output

    def test_format_text_output_warning(self) -> None:
        """测试文本输出格式（警告）"""
        result = ThresholdsCheckResult(
            success=True,
            total_violations=150,
            total_threshold=100,
            total_exceeded=True,
            noqa_count=None,
            noqa_threshold=None,
            noqa_exceeded=False,
            warnings=["总违规数 (150) 超出阈值 (100)"],
            errors=[],
        )
        config = ThresholdConfig(
            total_threshold=100,
            noqa_threshold=None,
            fail_on_threshold=False,
        )

        output = thresholds_format_output(result, config)

        assert "[WARN]" in output
        assert "超出阈值" in output


class TestRuffMetricsThresholdsIntegration:
    """集成测试"""

    def test_script_help(self) -> None:
        """测试 check_ruff_metrics_thresholds.py --help 能运行"""
        result = subprocess.run(
            [sys.executable, "-m", "scripts.ci.check_ruff_metrics_thresholds", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
            env=get_subprocess_env(PROJECT_ROOT),
            cwd=PROJECT_ROOT,
        )

        assert result.returncode == 0
        assert "ruff 指标阈值检查" in result.stdout or "metrics" in result.stdout

    def test_script_with_metrics_file(self, tmp_path: Path) -> None:
        """测试读取指标文件"""
        # 创建测试指标文件
        metrics_file = tmp_path / "ruff_metrics.json"
        metrics_data = {
            "generated_at": "2026-02-01T00:00:00Z",
            "summary": {
                "total_violations": 50,
                "total_files": 10,
                "total_fixable": 30,
            },
            "by_code": {},
            "by_directory": {},
            "top_files": [],
            "violations_by_file": {},
        }
        metrics_file.write_text(json.dumps(metrics_data))

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.ci.check_ruff_metrics_thresholds",
                "--metrics-file",
                str(metrics_file),
                "--total-threshold",
                "100",
                "--verbose",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env=get_subprocess_env(PROJECT_ROOT),
            cwd=PROJECT_ROOT,
        )

        assert result.returncode == 0
        assert "[OK]" in result.stdout

    def test_script_threshold_exceed_warn(self, tmp_path: Path) -> None:
        """测试超阈值警告模式"""
        # 创建测试指标文件
        metrics_file = tmp_path / "ruff_metrics.json"
        metrics_data = {
            "summary": {
                "total_violations": 150,
                "total_files": 20,
                "total_fixable": 100,
            }
        }
        metrics_file.write_text(json.dumps(metrics_data))

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.ci.check_ruff_metrics_thresholds",
                "--metrics-file",
                str(metrics_file),
                "--total-threshold",
                "100",
                "--fail-on-threshold",
                "false",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env=get_subprocess_env(PROJECT_ROOT),
            cwd=PROJECT_ROOT,
        )

        assert result.returncode == 0  # 仅警告，不失败
        assert "[WARN]" in result.stdout

    def test_script_threshold_exceed_fail(self, tmp_path: Path) -> None:
        """测试超阈值失败模式"""
        # 创建测试指标文件
        metrics_file = tmp_path / "ruff_metrics.json"
        metrics_data = {
            "summary": {
                "total_violations": 150,
                "total_files": 20,
                "total_fixable": 100,
            }
        }
        metrics_file.write_text(json.dumps(metrics_data))

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.ci.check_ruff_metrics_thresholds",
                "--metrics-file",
                str(metrics_file),
                "--total-threshold",
                "100",
                "--fail-on-threshold",
                "true",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env=get_subprocess_env(PROJECT_ROOT),
            cwd=PROJECT_ROOT,
        )

        assert result.returncode == 1  # 失败
        assert "[FAIL]" in result.stdout


# ============================================================================
# Phase 2 测试（全仓 P1 规则检查）
# ============================================================================


class TestLintIslandPhase2:
    """测试 Phase 2 全仓 P1 规则检查"""

    def test_phase_2_uses_full_scan_paths(self) -> None:
        """测试 Phase 2 使用全仓扫描路径"""
        config = LintIslandConfig(
            lint_island_paths=["src/engram/gateway/di.py"],  # Phase 2 不使用这个
            p1_rules=["B", "UP"],
            current_phase=2,
        )

        result = run_lint_island_check(
            phase=2,
            phase_source="cli",
            config=config,
            project_root=PROJECT_ROOT,
        )

        # Phase 2 应使用 DEFAULT_SCAN_ROOT 而不是 lint_island_paths
        assert result.lint_island_paths == DEFAULT_SCAN_ROOT
        assert result.phase == 2
        assert result.skipped is False

    def test_phase_2_does_not_require_lint_island_paths(self) -> None:
        """测试 Phase 2 不需要 lint_island_paths"""
        config = LintIslandConfig(
            lint_island_paths=[],  # 空
            p1_rules=["B", "UP"],
            current_phase=2,
        )

        result = run_lint_island_check(
            phase=2,
            phase_source="cli",
            config=config,
            project_root=PROJECT_ROOT,
        )

        # Phase 2 即使 lint_island_paths 为空也不跳过
        assert result.skipped is False
        assert result.phase == 2

    def test_phase_2_script_execution(self) -> None:
        """测试 Phase 2 脚本执行（集成测试）"""
        result = subprocess.run(
            [sys.executable, "-m", "scripts.ci.check_ruff_lint_island", "--phase", "2", "--json"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            env=get_subprocess_env(PROJECT_ROOT),
        )

        # 解析 JSON 输出
        data = json.loads(result.stdout)
        assert data["phase"] == 2
        assert "src/" in data["lint_island_paths"] or data["lint_island_paths"] == [
            "src/",
            "tests/",
        ]

    def test_phase_2_text_output_format(self) -> None:
        """测试 Phase 2 文本输出格式"""
        result = LintIslandCheckResult(
            success=True,
            phase=2,
            phase_source="cli",
            lint_island_paths=["src/", "tests/"],
            p1_rules=["B", "UP"],
            violations=[],
            violation_count=0,
        )

        output = lint_island_format_text_output(result)

        assert "Phase 2" in output or "全仓" in output
        assert "扫描路径" in output


# ============================================================================
# Phase 3 测试（全仓 P1 + RUF100 noqa 清理检查）
# ============================================================================


class TestLintIslandPhase3:
    """测试 Phase 3 全仓 P1 + RUF100 检查"""

    def test_phase_3_includes_ruf100_rule(self) -> None:
        """测试 Phase 3 包含 RUF100 规则"""
        config = LintIslandConfig(
            lint_island_paths=[],
            p1_rules=["B", "UP"],
            current_phase=3,
        )

        result = run_lint_island_check(
            phase=3,
            phase_source="cli",
            config=config,
            project_root=PROJECT_ROOT,
        )

        # Phase 3 的 p1_rules 应包含 RUF100
        assert "RUF100" in result.p1_rules
        assert result.phase == 3
        assert result.skipped is False

    def test_phase_3_uses_full_scan_paths(self) -> None:
        """测试 Phase 3 使用全仓扫描路径"""
        config = LintIslandConfig(
            lint_island_paths=["src/engram/gateway/di.py"],
            p1_rules=["B", "UP"],
            current_phase=3,
        )

        result = run_lint_island_check(
            phase=3,
            phase_source="cli",
            config=config,
            project_root=PROJECT_ROOT,
        )

        # Phase 3 应使用 DEFAULT_SCAN_ROOT
        assert result.lint_island_paths == DEFAULT_SCAN_ROOT
        assert result.phase == 3

    def test_phase_3_script_execution(self) -> None:
        """测试 Phase 3 脚本执行（集成测试）"""
        result = subprocess.run(
            [sys.executable, "-m", "scripts.ci.check_ruff_lint_island", "--phase", "3", "--json"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            env=get_subprocess_env(PROJECT_ROOT),
        )

        # 解析 JSON 输出
        data = json.loads(result.stdout)
        assert data["phase"] == 3
        assert "RUF100" in data["p1_rules"]

    def test_phase_3_text_output_format(self) -> None:
        """测试 Phase 3 文本输出格式（显示 RUF100 统计）"""
        result = LintIslandCheckResult(
            success=False,
            phase=3,
            phase_source="cli",
            lint_island_paths=["src/", "tests/"],
            p1_rules=["B", "UP", "RUF100"],
            violations=[
                {"code": "B006", "filename": "src/foo.py", "location": {"row": 10}},
                {"code": "RUF100", "filename": "src/bar.py", "location": {"row": 20}},
            ],
            violation_count=2,
            by_code={"B006": 1, "RUF100": 1},
        )

        output = lint_island_format_text_output(result)

        assert "Phase 3" in output or "noqa 清理" in output
        assert "RUF100" in output
        assert "[FAIL]" in output


# ============================================================================
# Phase 退出码测试
# ============================================================================


class TestLintIslandExitCodes:
    """测试各 Phase 的退出码"""

    def test_phase_0_exit_code_0(self) -> None:
        """测试 Phase 0 跳过时退出码为 0"""
        result = subprocess.run(
            [sys.executable, "-m", "scripts.ci.check_ruff_lint_island", "--phase", "0"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            env=get_subprocess_env(PROJECT_ROOT),
        )

        assert result.returncode == 0

    def test_phase_1_exit_code_depends_on_violations(self) -> None:
        """测试 Phase 1 退出码取决于是否有 violations（使用 JSON 检查）"""
        result = subprocess.run(
            [sys.executable, "-m", "scripts.ci.check_ruff_lint_island", "--phase", "1", "--json"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            env=get_subprocess_env(PROJECT_ROOT),
        )

        data = json.loads(result.stdout)

        # 退出码应该与 success 状态一致
        if data["success"]:
            assert result.returncode == 0
        else:
            assert result.returncode == 1

    def test_phase_2_exit_code_depends_on_violations(self) -> None:
        """测试 Phase 2 退出码取决于是否有 violations"""
        result = subprocess.run(
            [sys.executable, "-m", "scripts.ci.check_ruff_lint_island", "--phase", "2", "--json"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            env=get_subprocess_env(PROJECT_ROOT),
        )

        data = json.loads(result.stdout)

        if data["success"]:
            assert result.returncode == 0
        else:
            assert result.returncode == 1

    def test_phase_3_exit_code_depends_on_violations(self) -> None:
        """测试 Phase 3 退出码取决于是否有 violations"""
        result = subprocess.run(
            [sys.executable, "-m", "scripts.ci.check_ruff_lint_island", "--phase", "3", "--json"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            env=get_subprocess_env(PROJECT_ROOT),
        )

        data = json.loads(result.stdout)

        if data["success"]:
            assert result.returncode == 0
        else:
            assert result.returncode == 1


# ============================================================================
# Phase 常量测试
# ============================================================================


class TestPhaseConstants:
    """测试 Phase 相关常量"""

    def test_default_scan_root(self) -> None:
        """测试默认扫描路径包含 src/ 和 tests/"""
        assert "src/" in DEFAULT_SCAN_ROOT
        assert "tests/" in DEFAULT_SCAN_ROOT

    def test_phase3_extra_rules(self) -> None:
        """测试 Phase 3 额外规则包含 RUF100"""
        assert "RUF100" in PHASE3_EXTRA_RULES
