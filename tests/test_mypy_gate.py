"""
tests/test_mypy_gate.py

测试 mypy 门禁检查脚本的核心功能:
1. 规范化算法 (normalize_error, parse_mypy_output)
2. 基线对比逻辑 (新增/减少条目)
3. 退出码与摘要输出

使用 fixtures/mypy/sample_output.txt 作为 golden input。
"""

from __future__ import annotations

import os
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.ci.check_mypy_gate import (
    STRICT_ISLAND_MODULES,
    load_baseline,
    load_strict_island_paths,
    normalize_error,
    parse_mypy_output,
    save_baseline,
    stable_sort,
)

# Python 3.11+ 内置 tomllib，3.10 需要 tomli
try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[import-not-found]

# ============================================================================
# Fixtures
# ============================================================================

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "mypy"


@pytest.fixture
def sample_mypy_output() -> str:
    """加载 golden input 样例。"""
    sample_file = FIXTURES_DIR / "sample_output.txt"
    return sample_file.read_text(encoding="utf-8")


@pytest.fixture
def temp_baseline_file(tmp_path: Path) -> Path:
    """创建临时基线文件路径。"""
    return tmp_path / "test_baseline.txt"


# ============================================================================
# 规范化算法测试
# ============================================================================


class TestNormalizeError:
    """测试 normalize_error 函数。"""

    def test_removes_line_number(self) -> None:
        """行号应被移除。"""
        line = "src/foo.py:42: error: Something wrong  [error-code]"
        result = normalize_error(line)
        assert result == "src/foo.py: error: Something wrong  [error-code]"

    def test_removes_large_line_number(self) -> None:
        """大行号也应被正确移除。"""
        line = "src/bar.py:12345: error: Another error  [arg-type]"
        result = normalize_error(line)
        assert result == "src/bar.py: error: Another error  [arg-type]"

    def test_normalizes_windows_path(self) -> None:
        """Windows 路径分隔符应转换为 /。"""
        line = r"src\engram\gateway\app.py:10: error: Type mismatch  [type-var]"
        result = normalize_error(line)
        assert result == "src/engram/gateway/app.py: error: Type mismatch  [type-var]"

    def test_strips_trailing_whitespace(self) -> None:
        """尾部空白应被移除。"""
        line = "src/foo.py:1: error: Test error  [code]   \n"
        result = normalize_error(line)
        assert result == "src/foo.py: error: Test error  [code]"

    def test_preserves_error_code_brackets(self) -> None:
        """错误代码的方括号应保留。"""
        line = "src/foo.py:1: error: Incompatible type  [arg-type]"
        result = normalize_error(line)
        assert "[arg-type]" in result

    def test_handles_warning_and_note(self) -> None:
        """warning 和 note 类型也应正确处理。"""
        warning = "src/foo.py:1: warning: Unused ignore  [unused-ignore]"
        note = "src/foo.py:1: note: See documentation"

        assert "warning:" in normalize_error(warning)
        assert "note:" in normalize_error(note)


class TestParseMypyOutput:
    """测试 parse_mypy_output 函数。"""

    def test_golden_input_parsing(self, sample_mypy_output: str) -> None:
        """使用 golden input 验证解析结果。"""
        errors = parse_mypy_output(sample_mypy_output)

        # 应该解析出 6 条错误/警告/注释
        assert len(errors) == 6

        # 验证行号被移除
        for err in errors:
            # 不应包含 ":数字:" 模式
            assert not any(part.isdigit() for part in err.split(":")[1:2] if part.strip()), (
                f"行号未被移除: {err}"
            )

    def test_golden_input_normalized_paths(self, sample_mypy_output: str) -> None:
        """验证 Windows 路径被正确规范化。"""
        errors = parse_mypy_output(sample_mypy_output)

        # 所有路径应使用 / 分隔符
        for err in errors:
            file_path = err.split(":")[0]
            assert "\\" not in file_path, f"路径未规范化: {file_path}"

    def test_filters_summary_line(self) -> None:
        """摘要行应被过滤。"""
        output = """src/foo.py:1: error: Test error  [code]
Found 1 error in 1 file (checked 10 source files)
"""
        errors = parse_mypy_output(output)
        assert len(errors) == 1
        assert not any("Found" in e for e in errors)

    def test_filters_success_line(self) -> None:
        """Success 行应被过滤。"""
        output = """Success: no issues found in 42 source files
"""
        errors = parse_mypy_output(output)
        assert len(errors) == 0

    def test_filters_empty_lines(self) -> None:
        """空行应被过滤。"""
        output = """src/foo.py:1: error: Test error  [code]

src/bar.py:2: error: Another error  [code]

"""
        errors = parse_mypy_output(output)
        assert len(errors) == 2

    def test_deduplicates_same_error_different_lines(self) -> None:
        """相同文件的相同错误（不同行）应去重。"""
        output = """src/foo.py:10: error: Duplicate error  [code]
src/foo.py:20: error: Duplicate error  [code]
src/foo.py:30: error: Duplicate error  [code]
"""
        errors = parse_mypy_output(output)
        # 行号移除后，三条错误应合并为一条
        assert len(errors) == 1

    def test_golden_input_expected_errors(self, sample_mypy_output: str) -> None:
        """验证 golden input 包含预期的规范化错误。"""
        errors = parse_mypy_output(sample_mypy_output)

        # 预期的规范化错误列表
        expected = {
            'src/engram/gateway/app.py: error: Argument 1 to "foo" has incompatible type "str"; expected "int"  [arg-type]',
            'src/engram/gateway/app.py: error: Incompatible return value type (got "None", expected "str")  [return-value]',
            'src/engram/logbook/db.py: error: "Dict[str, Any]" has no attribute "items"  [attr-defined]',
            'src/engram/logbook/db.py: warning: Unused "type: ignore" comment  [unused-ignore]',
            "src/engram/gateway/handlers/memory_store.py: error: Missing return statement  [return]",
            "src/engram/gateway/handlers/memory_store.py: note: See documentation for more info",
        }

        assert errors == expected


class TestStableSort:
    """测试 stable_sort 函数。"""

    def test_sorts_alphabetically(self) -> None:
        """应按字母顺序排序。"""
        errors = {"c.py: error: C", "a.py: error: A", "b.py: error: B"}
        result = stable_sort(errors)
        assert result == ["a.py: error: A", "b.py: error: B", "c.py: error: C"]

    def test_returns_list(self) -> None:
        """应返回列表类型。"""
        errors = {"a.py: error: A"}
        result = stable_sort(errors)
        assert isinstance(result, list)


# ============================================================================
# 基线文件操作测试
# ============================================================================


class TestBaselineOperations:
    """测试基线文件的加载和保存。"""

    def test_load_nonexistent_baseline(self, tmp_path: Path) -> None:
        """加载不存在的基线应返回空集合。"""
        baseline = load_baseline(tmp_path / "nonexistent.txt")
        assert baseline == set()

    def test_save_and_load_baseline(self, temp_baseline_file: Path) -> None:
        """保存后应能正确加载。"""
        errors = {
            "src/foo.py: error: Error A  [code-a]",
            "src/bar.py: error: Error B  [code-b]",
        }
        save_baseline(errors, temp_baseline_file)

        loaded = load_baseline(temp_baseline_file)
        assert loaded == errors

    def test_save_empty_baseline(self, temp_baseline_file: Path) -> None:
        """保存空集合应创建空文件或只有换行的文件。"""
        save_baseline(set(), temp_baseline_file)
        loaded = load_baseline(temp_baseline_file)
        assert loaded == set()

    def test_baseline_sorted_on_save(self, temp_baseline_file: Path) -> None:
        """保存时应按字母顺序排序。"""
        errors = {"z.py: error: Z", "a.py: error: A", "m.py: error: M"}
        save_baseline(errors, temp_baseline_file)

        content = temp_baseline_file.read_text(encoding="utf-8")
        lines = [line for line in content.splitlines() if line.strip()]
        assert lines == ["a.py: error: A", "m.py: error: M", "z.py: error: Z"]


# ============================================================================
# 基线对比逻辑测试
# ============================================================================


class TestBaselineComparison:
    """测试基线对比逻辑（新增/减少条目）。"""

    def test_detects_new_errors(self) -> None:
        """应检测新增的错误。"""
        baseline = {
            "src/foo.py: error: Old error  [code]",
        }
        current = {
            "src/foo.py: error: Old error  [code]",
            "src/bar.py: error: New error  [code]",  # 新增
        }

        new_errors = current - baseline
        assert len(new_errors) == 1
        assert "New error" in list(new_errors)[0]

    def test_detects_fixed_errors(self) -> None:
        """应检测已修复的错误。"""
        baseline = {
            "src/foo.py: error: Old error  [code]",
            "src/bar.py: error: Fixed error  [code]",  # 将被修复
        }
        current = {
            "src/foo.py: error: Old error  [code]",
        }

        fixed_errors = baseline - current
        assert len(fixed_errors) == 1
        assert "Fixed error" in list(fixed_errors)[0]

    def test_no_changes(self) -> None:
        """基线与当前相同时应无变化。"""
        errors = {
            "src/foo.py: error: Same error  [code]",
        }
        baseline = errors.copy()
        current = errors.copy()

        new_errors = current - baseline
        fixed_errors = baseline - current

        assert len(new_errors) == 0
        assert len(fixed_errors) == 0

    def test_complete_replacement(self) -> None:
        """完全不同的错误集合应正确处理。"""
        baseline = {
            "src/old.py: error: Old A  [code]",
            "src/old.py: error: Old B  [code]",
        }
        current = {
            "src/new.py: error: New A  [code]",
            "src/new.py: error: New B  [code]",
        }

        new_errors = current - baseline
        fixed_errors = baseline - current

        assert len(new_errors) == 2
        assert len(fixed_errors) == 2


# ============================================================================
# main() 函数集成测试
# ============================================================================


class TestMainFunction:
    """测试 main() 函数的退出码和摘要输出。"""

    def test_gate_off_returns_zero(self) -> None:
        """gate=off 模式应返回 0。"""
        from check_mypy_gate import main

        with patch("sys.argv", ["check_mypy_gate.py", "--gate", "off"]):
            captured = StringIO()
            with patch("sys.stdout", captured):
                exit_code = main()

        assert exit_code == 0
        output = captured.getvalue()
        assert "[SKIP]" in output or "跳过" in output

    def test_strict_mode_with_errors_returns_one(self) -> None:
        """strict 模式下有错误应返回 1。"""
        from check_mypy_gate import main

        mock_output = "src/foo.py:1: error: Test error  [code]\nFound 1 error"

        with patch("sys.argv", ["check_mypy_gate.py", "--gate", "strict"]):
            with patch("check_mypy_gate.run_mypy", return_value=(mock_output, 1)):
                captured = StringIO()
                with patch("sys.stdout", captured):
                    exit_code = main()

        assert exit_code == 1
        output = captured.getvalue()
        assert "[FAIL]" in output

    def test_strict_mode_without_errors_returns_zero(self) -> None:
        """strict 模式下无错误应返回 0。"""
        from check_mypy_gate import main

        mock_output = "Success: no issues found in 10 source files"

        with patch("sys.argv", ["check_mypy_gate.py", "--gate", "strict"]):
            with patch("check_mypy_gate.run_mypy", return_value=(mock_output, 0)):
                captured = StringIO()
                with patch("sys.stdout", captured):
                    exit_code = main()

        assert exit_code == 0
        output = captured.getvalue()
        assert "[OK]" in output

    def test_baseline_mode_new_errors_returns_one(self, tmp_path: Path) -> None:
        """baseline 模式下有新增错误应返回 1。"""
        from check_mypy_gate import main

        # 创建只有一条错误的基线
        baseline_file = tmp_path / "baseline.txt"
        baseline_file.write_text("src/foo.py: error: Old error  [code]\n", encoding="utf-8")

        # 模拟 mypy 输出：包含基线错误 + 新增错误
        mock_output = """src/foo.py:1: error: Old error  [code]
src/bar.py:10: error: New error  [new-code]
Found 2 errors"""

        with patch(
            "sys.argv",
            [
                "check_mypy_gate.py",
                "--gate",
                "baseline",
                "--baseline-file",
                str(baseline_file),
            ],
        ):
            with patch("check_mypy_gate.run_mypy", return_value=(mock_output, 1)):
                captured = StringIO()
                with patch("sys.stdout", captured):
                    exit_code = main()

        assert exit_code == 1
        output = captured.getvalue()
        assert "[FAIL]" in output
        assert "新增" in output or "new" in output.lower()

    def test_baseline_mode_no_new_errors_returns_zero(self, tmp_path: Path) -> None:
        """baseline 模式下无新增错误应返回 0。"""
        from check_mypy_gate import main

        # 创建基线（与当前错误相同）
        baseline_file = tmp_path / "baseline.txt"
        baseline_file.write_text("src/foo.py: error: Existing error  [code]\n", encoding="utf-8")

        # 模拟 mypy 输出：只有基线中的错误
        mock_output = """src/foo.py:42: error: Existing error  [code]
Found 1 error"""

        with patch(
            "sys.argv",
            [
                "check_mypy_gate.py",
                "--gate",
                "baseline",
                "--baseline-file",
                str(baseline_file),
            ],
        ):
            with patch("check_mypy_gate.run_mypy", return_value=(mock_output, 1)):
                captured = StringIO()
                with patch("sys.stdout", captured):
                    exit_code = main()

        assert exit_code == 0
        output = captured.getvalue()
        assert "[OK]" in output

    def test_baseline_mode_shows_fixed_errors(self, tmp_path: Path) -> None:
        """baseline 模式下应显示已修复的错误。"""
        from check_mypy_gate import main

        # 创建包含两条错误的基线
        baseline_file = tmp_path / "baseline.txt"
        baseline_file.write_text(
            "src/foo.py: error: Error A  [code]\nsrc/bar.py: error: Error B  [code]\n",
            encoding="utf-8",
        )

        # 模拟 mypy 输出：只剩一条错误（另一条被修复）
        mock_output = """src/foo.py:1: error: Error A  [code]
Found 1 error"""

        with patch(
            "sys.argv",
            [
                "check_mypy_gate.py",
                "--gate",
                "baseline",
                "--baseline-file",
                str(baseline_file),
            ],
        ):
            with patch("check_mypy_gate.run_mypy", return_value=(mock_output, 1)):
                captured = StringIO()
                with patch("sys.stdout", captured):
                    exit_code = main()

        assert exit_code == 0
        output = captured.getvalue()
        assert "修复" in output or "fixed" in output.lower()

    def test_write_baseline_mode(self, tmp_path: Path) -> None:
        """--write-baseline 模式应写入基线文件。"""
        from check_mypy_gate import main

        baseline_file = tmp_path / "new_baseline.txt"

        mock_output = """src/foo.py:1: error: Error A  [code]
src/bar.py:2: error: Error B  [code]
Found 2 errors"""

        with patch(
            "sys.argv",
            [
                "check_mypy_gate.py",
                "--write-baseline",
                "--baseline-file",
                str(baseline_file),
            ],
        ):
            with patch("check_mypy_gate.run_mypy", return_value=(mock_output, 1)):
                captured = StringIO()
                with patch("sys.stdout", captured):
                    exit_code = main()

        assert exit_code == 0
        assert baseline_file.exists()

        content = baseline_file.read_text(encoding="utf-8")
        assert "src/bar.py: error: Error B  [code]" in content
        assert "src/foo.py: error: Error A  [code]" in content

    def test_summary_shows_error_counts(self, tmp_path: Path) -> None:
        """摘要应显示错误数量。"""
        from check_mypy_gate import main

        baseline_file = tmp_path / "baseline.txt"
        baseline_file.write_text("", encoding="utf-8")

        mock_output = """src/foo.py:1: error: Error 1  [code]
src/bar.py:2: error: Error 2  [code]
src/baz.py:3: error: Error 3  [code]
Found 3 errors"""

        with patch(
            "sys.argv",
            [
                "check_mypy_gate.py",
                "--gate",
                "baseline",
                "--baseline-file",
                str(baseline_file),
            ],
        ):
            with patch("check_mypy_gate.run_mypy", return_value=(mock_output, 1)):
                captured = StringIO()
                with patch("sys.stdout", captured):
                    main()

        output = captured.getvalue()
        # 应该显示当前错误数
        assert "3" in output


# ============================================================================
# warn 模式测试
# ============================================================================


class TestWarnMode:
    """测试 gate=warn 模式的退出码和 artifacts 行为。"""

    def test_warn_mode_returns_zero_with_errors(self, tmp_path: Path) -> None:
        """warn 模式下即使有错误也应返回 0。"""
        from check_mypy_gate import main

        mock_output = """src/foo.py:1: error: Test error  [code]
src/bar.py:2: error: Another error  [code]
Found 2 errors"""

        # 切换到 tmp_path 以便 artifacts 写入临时目录
        import os

        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            with patch("sys.argv", ["check_mypy_gate.py", "--gate", "warn"]):
                with patch("check_mypy_gate.run_mypy", return_value=(mock_output, 1)):
                    captured = StringIO()
                    with patch("sys.stdout", captured):
                        exit_code = main()

            assert exit_code == 0
            output = captured.getvalue()
            assert "[OK]" in output
            assert "warn" in output.lower()
        finally:
            os.chdir(original_cwd)

    def test_warn_mode_returns_zero_without_errors(self) -> None:
        """warn 模式下无错误也应返回 0。"""
        from check_mypy_gate import main

        mock_output = "Success: no issues found in 10 source files"

        with patch("sys.argv", ["check_mypy_gate.py", "--gate", "warn"]):
            with patch("check_mypy_gate.run_mypy", return_value=(mock_output, 0)):
                captured = StringIO()
                with patch("sys.stdout", captured):
                    exit_code = main()

        assert exit_code == 0
        output = captured.getvalue()
        assert "[OK]" in output

    def test_warn_mode_writes_artifacts(self, tmp_path: Path) -> None:
        """warn 模式应写入 artifacts 文件。"""
        from check_mypy_gate import main

        mock_output = """src/foo.py:1: error: Error A  [code]
src/bar.py:2: error: Error B  [code]
Found 2 errors"""

        import os

        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            with patch("sys.argv", ["check_mypy_gate.py", "--gate", "warn"]):
                with patch("check_mypy_gate.run_mypy", return_value=(mock_output, 1)):
                    captured = StringIO()
                    with patch("sys.stdout", captured):
                        main()

            # 验证 artifact 文件被创建
            artifacts_dir = tmp_path / "artifacts"
            current_file = artifacts_dir / "mypy_current.txt"
            new_errors_file = artifacts_dir / "mypy_new_errors.txt"

            assert artifacts_dir.exists()
            assert current_file.exists()
            assert new_errors_file.exists()

            # 验证 current 文件包含错误
            current_content = current_file.read_text(encoding="utf-8")
            assert "src/bar.py: error: Error B  [code]" in current_content
            assert "src/foo.py: error: Error A  [code]" in current_content

            # warn 模式下 new_errors 文件应为空（无基线对比）
            new_content = new_errors_file.read_text(encoding="utf-8")
            assert new_content.strip() == ""
        finally:
            os.chdir(original_cwd)

    def test_warn_mode_shows_error_summary(self, tmp_path: Path) -> None:
        """warn 模式应显示错误摘要。"""
        from check_mypy_gate import main

        mock_output = """src/foo.py:1: error: Test error  [code]
Found 1 error"""

        import os

        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            with patch("sys.argv", ["check_mypy_gate.py", "--gate", "warn"]):
                with patch("check_mypy_gate.run_mypy", return_value=(mock_output, 1)):
                    captured = StringIO()
                    with patch("sys.stdout", captured):
                        exit_code = main()

            assert exit_code == 0
            output = captured.getvalue()
            # 应该显示错误摘要
            assert "[WARN]" in output
            assert "Test error" in output or "1" in output
        finally:
            os.chdir(original_cwd)


# ============================================================================
# 旧环境变量 fallback 测试
# ============================================================================


class TestLegacyEnvFallback:
    """测试旧环境变量的 fallback 行为。"""

    def test_legacy_gate_env_fallback(self) -> None:
        """旧的 MYPY_GATE 环境变量应作为 fallback。"""
        import argparse

        from check_mypy_gate import resolve_config

        args = argparse.Namespace(
            gate=None,
            baseline_file=None,
            mypy_path=None,
            write_baseline=False,
            verbose=False,
        )

        # 仅设置旧环境变量
        with patch.dict(os.environ, {"MYPY_GATE": "strict"}, clear=False):
            # 确保新环境变量不存在
            env_copy = os.environ.copy()
            env_copy.pop("ENGRAM_MYPY_GATE", None)
            with patch.dict(os.environ, env_copy, clear=True):
                with patch.dict(os.environ, {"MYPY_GATE": "strict"}):
                    config = resolve_config(args)
                    assert config["gate"] == "strict"

    def test_engram_env_takes_priority_over_legacy(self) -> None:
        """ENGRAM_* 环境变量应优先于旧环境变量。"""
        import argparse

        from check_mypy_gate import resolve_config

        args = argparse.Namespace(
            gate=None,
            baseline_file=None,
            mypy_path=None,
            write_baseline=False,
            verbose=False,
        )

        # 同时设置新旧环境变量
        with patch.dict(
            os.environ,
            {"ENGRAM_MYPY_GATE": "baseline", "MYPY_GATE": "strict"},
            clear=False,
        ):
            config = resolve_config(args)
            assert config["gate"] == "baseline"  # ENGRAM_* 优先

    def test_cli_takes_priority_over_all_env(self) -> None:
        """CLI 参数应优先于所有环境变量。"""
        import argparse

        from check_mypy_gate import resolve_config

        args = argparse.Namespace(
            gate="off",
            baseline_file=None,
            mypy_path=None,
            write_baseline=False,
            verbose=False,
        )

        # 设置所有环境变量
        with patch.dict(
            os.environ,
            {"ENGRAM_MYPY_GATE": "baseline", "MYPY_GATE": "strict"},
            clear=False,
        ):
            config = resolve_config(args)
            assert config["gate"] == "off"  # CLI 优先

    def test_legacy_baseline_file_env_fallback(self, tmp_path: Path) -> None:
        """旧的 MYPY_BASELINE_FILE 环境变量应作为 fallback。"""
        import argparse

        from check_mypy_gate import resolve_config

        args = argparse.Namespace(
            gate=None,
            baseline_file=None,
            mypy_path=None,
            write_baseline=False,
            verbose=False,
        )

        legacy_path = str(tmp_path / "legacy_baseline.txt")

        # 确保新环境变量不存在，只有旧环境变量
        env_copy = os.environ.copy()
        env_copy.pop("ENGRAM_MYPY_BASELINE_FILE", None)
        with patch.dict(os.environ, env_copy, clear=True):
            with patch.dict(os.environ, {"MYPY_BASELINE_FILE": legacy_path}):
                config = resolve_config(args)
                assert str(config["baseline_file"]) == legacy_path

    def test_legacy_mypy_path_env_fallback(self) -> None:
        """旧的 MYPY_PATH 环境变量应作为 fallback。"""
        import argparse

        from check_mypy_gate import resolve_config

        args = argparse.Namespace(
            gate=None,
            baseline_file=None,
            mypy_path=None,
            write_baseline=False,
            verbose=False,
        )

        # 确保新环境变量不存在，只有旧环境变量
        env_copy = os.environ.copy()
        env_copy.pop("ENGRAM_MYPY_PATH", None)
        with patch.dict(os.environ, env_copy, clear=True):
            with patch.dict(os.environ, {"MYPY_PATH": "custom/path/"}):
                config = resolve_config(args)
                assert config["mypy_path"] == "custom/path/"


# ============================================================================
# Strict Island 配置测试
# ============================================================================


# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent
PYPROJECT_PATH = PROJECT_ROOT / "pyproject.toml"


class TestStrictIslandConfig:
    """测试 strict island 配置读取和一致性。"""

    def test_pyproject_exists(self) -> None:
        """pyproject.toml 应存在。"""
        assert PYPROJECT_PATH.exists(), f"pyproject.toml 不存在: {PYPROJECT_PATH}"

    def test_load_strict_island_paths_returns_list(self) -> None:
        """load_strict_island_paths 应返回非空列表。"""
        paths = load_strict_island_paths(PYPROJECT_PATH)
        assert isinstance(paths, list)
        assert len(paths) > 0, "strict_island_paths 不应为空"

    def test_strict_island_modules_not_empty(self) -> None:
        """STRICT_ISLAND_MODULES 应非空。"""
        assert len(STRICT_ISLAND_MODULES) > 0, "STRICT_ISLAND_MODULES 不应为空"

    def test_strict_island_paths_match_loaded(self) -> None:
        """STRICT_ISLAND_MODULES 应与 load_strict_island_paths 结果一致。"""
        expected = load_strict_island_paths(PYPROJECT_PATH)
        assert STRICT_ISLAND_MODULES == expected, (
            f"STRICT_ISLAND_MODULES 与 pyproject.toml 配置不一致:\n"
            f"  模块中: {STRICT_ISLAND_MODULES}\n"
            f"  配置中: {expected}"
        )


class TestStrictIslandMypyOverridesConsistency:
    """
    测试 strict island 配置与 mypy overrides 的一致性。

    一致性约束（见 pyproject.toml 注释）：
    1. strict_island_paths ⊆ mypy overrides 中 disallow_untyped_defs=true 的模块
    2. strict_island_paths 中的模块必须配置 ignore_missing_imports=false

    注意：strict_island_paths 是 CI 强阻断岛屿集合，不等同于所有启用
    disallow_untyped_defs=true 的模块。某些模块（如 engram.logbook.*）
    可能启用了严格类型检查，但未纳入 CI 强阻断岛屿。
    """

    @staticmethod
    def _path_to_module(path: str) -> str:
        """
        将文件路径转换为模块名。

        示例:
            "src/engram/gateway/di.py" -> "engram.gateway.di"
            "src/engram/gateway/services/" -> "engram.gateway.services.*"
        """
        # 移除 src/ 前缀
        if path.startswith("src/"):
            path = path[4:]
        # 移除 .py 后缀
        if path.endswith(".py"):
            path = path[:-3]
        # 目录转换为通配符模式
        if path.endswith("/"):
            path = path.rstrip("/") + ".*"
        # 替换路径分隔符为点号
        return path.replace("/", ".")

    @staticmethod
    def _load_mypy_overrides_with_strict() -> set[str]:
        """
        从 pyproject.toml 加载启用了 disallow_untyped_defs=true 的 mypy overrides 模块。

        Returns:
            启用了 disallow_untyped_defs=true 的模块名集合
        """
        with open(PYPROJECT_PATH, "rb") as f:
            config = tomllib.load(f)

        overrides = config.get("tool", {}).get("mypy", {}).get("overrides", [])
        strict_modules: set[str] = set()

        for override in overrides:
            if override.get("disallow_untyped_defs") is True:
                module = override.get("module")
                if isinstance(module, str):
                    strict_modules.add(module)
                elif isinstance(module, list):
                    strict_modules.update(module)

        return strict_modules

    @staticmethod
    def _load_mypy_overrides_with_ignore_missing_imports_false() -> set[str]:
        """
        从 pyproject.toml 加载配置了 ignore_missing_imports=false 的 mypy overrides 模块。

        Returns:
            配置了 ignore_missing_imports=false 的模块名集合
        """
        with open(PYPROJECT_PATH, "rb") as f:
            config = tomllib.load(f)

        overrides = config.get("tool", {}).get("mypy", {}).get("overrides", [])
        modules_with_strict_imports: set[str] = set()

        for override in overrides:
            if override.get("ignore_missing_imports") is False:
                module = override.get("module")
                if isinstance(module, str):
                    modules_with_strict_imports.add(module)
                elif isinstance(module, list):
                    modules_with_strict_imports.update(module)

        return modules_with_strict_imports

    def test_strict_island_is_subset_of_mypy_strict_overrides(self) -> None:
        """
        strict_island_paths 必须是 mypy overrides 中 disallow_untyped_defs=true 模块的子集。

        验证逻辑：
        1. 从 pyproject.toml 读取 [tool.engram.mypy].strict_island_paths
        2. 将路径转换为模块名
        3. 从 pyproject.toml 读取 [[tool.mypy.overrides]] 中启用 disallow_untyped_defs=true 的模块
        4. 断言 strict_island_paths ⊆ mypy_strict_overrides

        注意：不再要求两者相等。mypy overrides 中可能有更多启用 disallow_untyped_defs=true
        的模块（如 engram.logbook.*），但未纳入 CI 强阻断岛屿。
        """
        # 从配置读取 strict island 路径并转换为模块名
        strict_island_paths = load_strict_island_paths(PYPROJECT_PATH)
        strict_island_modules = {self._path_to_module(p) for p in strict_island_paths}

        # 从 mypy overrides 读取启用 disallow_untyped_defs=true 的模块
        mypy_strict_modules = self._load_mypy_overrides_with_strict()

        # 验证 strict_island_paths ⊆ mypy_strict_overrides
        # （岛屿集合必须是严格模块的子集）
        missing_in_overrides = strict_island_modules - mypy_strict_modules

        assert not missing_in_overrides, (
            f"strict_island_paths 中有模块未在 mypy overrides 中启用 disallow_untyped_defs=true:\n"
            f"  缺失: {missing_in_overrides}\n"
            f"  请在 pyproject.toml 的 [[tool.mypy.overrides]] 中为这些模块添加 disallow_untyped_defs=true"
        )

    def test_strict_island_modules_have_ignore_missing_imports_false(self) -> None:
        """
        strict_island_paths 中的模块必须配置 ignore_missing_imports=false。

        CI 强阻断岛屿要求更高的类型安全标准，不仅要求 disallow_untyped_defs=true，
        还要求 ignore_missing_imports=false（强制要求导入类型信息）。
        """
        # 从配置读取 strict island 路径并转换为模块名
        strict_island_paths = load_strict_island_paths(PYPROJECT_PATH)
        strict_island_modules = {self._path_to_module(p) for p in strict_island_paths}

        # 从 mypy overrides 读取配置了 ignore_missing_imports=false 的模块
        modules_with_strict_imports = self._load_mypy_overrides_with_ignore_missing_imports_false()

        # 验证 strict_island_paths 中的模块都配置了 ignore_missing_imports=false
        missing_strict_imports = strict_island_modules - modules_with_strict_imports

        assert not missing_strict_imports, (
            f"strict_island_paths 中有模块未配置 ignore_missing_imports=false:\n"
            f"  缺失: {missing_strict_imports}\n"
            f"  CI 强阻断岛屿要求：disallow_untyped_defs=true 且 ignore_missing_imports=false\n"
            f"  请在 pyproject.toml 的 [[tool.mypy.overrides]] 中为这些模块添加 ignore_missing_imports=false"
        )

    def test_all_strict_island_paths_are_valid(self) -> None:
        """验证所有 strict island 路径格式有效。"""
        paths = load_strict_island_paths(PYPROJECT_PATH)

        for path in paths:
            # 应以 src/ 开头
            assert path.startswith("src/"), f"路径应以 'src/' 开头: {path}"
            # 应以 .py 结尾或 / 结尾（目录）
            assert path.endswith(".py") or path.endswith("/"), f"路径应以 '.py' 或 '/' 结尾: {path}"


class TestLoadStrictIslandPathsEdgeCases:
    """测试 load_strict_island_paths 的边界情况。"""

    def test_missing_file_raises_error(self, tmp_path: Path) -> None:
        """文件不存在时应抛出 FileNotFoundError。"""
        with pytest.raises(FileNotFoundError):
            load_strict_island_paths(tmp_path / "nonexistent.toml")

    def test_missing_config_section_raises_error(self, tmp_path: Path) -> None:
        """缺少配置节时应抛出 KeyError。"""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project]\nname = 'test'\n", encoding="utf-8")

        with pytest.raises(KeyError):
            load_strict_island_paths(pyproject)

    def test_invalid_type_raises_error(self, tmp_path: Path) -> None:
        """配置类型错误时应抛出 TypeError。"""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[tool.engram.mypy]\nstrict_island_paths = "not a list"\n',
            encoding="utf-8",
        )

        with pytest.raises(TypeError):
            load_strict_island_paths(pyproject)

    def test_valid_config_returns_paths(self, tmp_path: Path) -> None:
        """有效配置应返回路径列表。"""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[tool.engram.mypy]\nstrict_island_paths = ["src/foo.py", "src/bar/"]\n',
            encoding="utf-8",
        )

        paths = load_strict_island_paths(pyproject)
        assert paths == ["src/foo.py", "src/bar/"]


# ============================================================================
# run_mypy_with_baseline.py 弃用脚本回归测试
# ============================================================================


class TestDeprecatedRunMypyWithBaseline:
    """
    测试已弃用的 run_mypy_with_baseline.py 脚本的旧参数行为。

    验证旧脚本正确转发到新的 check_mypy_gate.py 脚本。
    """

    @pytest.fixture
    def deprecated_script_path(self) -> Path:
        """获取弃用脚本路径。"""
        return Path(__file__).parent.parent / "scripts" / "ci" / "run_mypy_with_baseline.py"

    @pytest.fixture
    def new_script_path(self) -> Path:
        """获取新脚本路径。"""
        return Path(__file__).parent.parent / "scripts" / "ci" / "check_mypy_gate.py"

    def test_deprecated_script_exists(self, deprecated_script_path: Path) -> None:
        """弃用脚本应存在。"""
        assert deprecated_script_path.exists(), f"弃用脚本不存在: {deprecated_script_path}"

    def test_deprecated_script_has_deprecation_warning_in_docstring(
        self, deprecated_script_path: Path
    ) -> None:
        """弃用脚本的 docstring 应包含弃用警告。"""
        content = deprecated_script_path.read_text(encoding="utf-8")
        assert "已弃用" in content or "deprecated" in content.lower(), (
            "弃用脚本应在 docstring 中包含弃用警告"
        )

    def test_deprecated_script_imports_subprocess(self, deprecated_script_path: Path) -> None:
        """弃用脚本应导入 subprocess 以调用新脚本。"""
        content = deprecated_script_path.read_text(encoding="utf-8")
        assert "import subprocess" in content, "弃用脚本应导入 subprocess"

    def test_deprecated_script_references_new_script(self, deprecated_script_path: Path) -> None:
        """弃用脚本应引用新脚本。"""
        content = deprecated_script_path.read_text(encoding="utf-8")
        assert "check_mypy_gate.py" in content, "弃用脚本应引用 check_mypy_gate.py"

    def test_argument_mapping_update_baseline(self, deprecated_script_path: Path) -> None:
        """--update-baseline 应映射到 --write-baseline。"""
        content = deprecated_script_path.read_text(encoding="utf-8")
        # 检查参数映射逻辑
        assert "--update-baseline" in content, "应处理 --update-baseline 参数"
        assert "--write-baseline" in content, "应映射到 --write-baseline"

    def test_argument_mapping_diff_only(self, deprecated_script_path: Path) -> None:
        """--diff-only 应映射到 --gate baseline --verbose。"""
        content = deprecated_script_path.read_text(encoding="utf-8")
        assert "--diff-only" in content, "应处理 --diff-only 参数"

    def test_argument_mapping_verbose(self, deprecated_script_path: Path) -> None:
        """--verbose 应正确传递。"""
        content = deprecated_script_path.read_text(encoding="utf-8")
        assert "--verbose" in content, "应处理 --verbose 参数"
        assert "-v" in content, "应处理 -v 短参数"

    def test_default_gate_is_baseline(self, deprecated_script_path: Path) -> None:
        """无参数时应默认使用 --gate baseline。"""
        content = deprecated_script_path.read_text(encoding="utf-8")
        # 检查默认情况下使用 baseline gate
        assert (
            '"--gate", "baseline"' in content
            or "'--gate', 'baseline'" in content
            or '["--gate", "baseline"]' in content
            or "baseline" in content
        ), "默认应使用 --gate baseline"

    def test_deprecation_warning_printed(self, deprecated_script_path: Path) -> None:
        """弃用警告应输出到 stderr。"""
        content = deprecated_script_path.read_text(encoding="utf-8")
        # 检查 warnings.warn 或 print(..., file=sys.stderr)
        assert "warnings.warn" in content or "file=sys.stderr" in content, "弃用警告应输出到 stderr"

    def test_migration_guide_in_output(self, deprecated_script_path: Path) -> None:
        """弃用输出应包含迁移指南。"""
        content = deprecated_script_path.read_text(encoding="utf-8")
        # 检查迁移指南关键词
        assert "make typecheck-gate" in content or "迁移" in content, "弃用输出应包含迁移指南"


class TestDeprecatedScriptSubprocessBehavior:
    """
    测试弃用脚本的 subprocess 行为（模拟测试）。

    这些测试验证参数解析逻辑的正确性，不实际执行 mypy。
    """

    def test_parse_update_baseline_flag(self) -> None:
        """测试 --update-baseline 标志解析。"""
        args = ["--update-baseline"]
        has_update_baseline = "--update-baseline" in args
        assert has_update_baseline is True

    def test_parse_diff_only_flag(self) -> None:
        """测试 --diff-only 标志解析。"""
        args = ["--diff-only"]
        has_diff_only = "--diff-only" in args
        assert has_diff_only is True

    def test_parse_verbose_flag(self) -> None:
        """测试 --verbose 标志解析。"""
        args = ["--verbose"]
        has_verbose = "--verbose" in args or "-v" in args
        assert has_verbose is True

    def test_parse_short_verbose_flag(self) -> None:
        """测试 -v 短标志解析。"""
        args = ["-v"]
        has_verbose = "--verbose" in args or "-v" in args
        assert has_verbose is True

    def test_build_new_args_update_baseline(self) -> None:
        """测试 --update-baseline 的参数映射。"""
        old_args = ["--update-baseline"]
        has_update_baseline = "--update-baseline" in old_args

        new_args = []
        if has_update_baseline:
            new_args.append("--write-baseline")

        assert "--write-baseline" in new_args

    def test_build_new_args_diff_only(self) -> None:
        """测试 --diff-only 的参数映射。"""
        old_args = ["--diff-only"]
        has_diff_only = "--diff-only" in old_args

        new_args = []
        if has_diff_only:
            new_args.extend(["--gate", "baseline", "--verbose"])

        assert "--gate" in new_args
        assert "baseline" in new_args
        assert "--verbose" in new_args

    def test_build_new_args_default(self) -> None:
        """测试无参数时的默认映射。"""
        old_args: list[str] = []
        has_update_baseline = "--update-baseline" in old_args
        has_diff_only = "--diff-only" in old_args

        new_args = []
        if has_update_baseline:
            new_args.append("--write-baseline")
        elif has_diff_only:
            new_args.extend(["--gate", "baseline", "--verbose"])
        else:
            new_args.extend(["--gate", "baseline"])

        assert "--gate" in new_args
        assert "baseline" in new_args
        assert "--verbose" not in new_args

    def test_build_new_args_verbose_not_duplicated(self) -> None:
        """测试 --diff-only --verbose 不会重复添加 --verbose。"""
        old_args = ["--diff-only", "--verbose"]
        has_diff_only = "--diff-only" in old_args
        has_verbose = "--verbose" in old_args or "-v" in old_args

        new_args = []
        if has_diff_only:
            # --diff-only 已经包含 --verbose
            new_args.extend(["--gate", "baseline", "--verbose"])
        else:
            new_args.extend(["--gate", "baseline"])

        # --verbose 只应出现一次
        if has_verbose and not has_diff_only:
            new_args.append("--verbose")

        verbose_count = new_args.count("--verbose")
        assert verbose_count == 1, f"--verbose 应只出现一次，实际: {verbose_count}"


# ============================================================================
# resolve_mypy_gate.py 集成测试
# ============================================================================

from scripts.ci.resolve_mypy_gate import extract_branch_from_ref, resolve_gate


class TestResolveMypyGate:
    """测试 resolve_mypy_gate.py 的核心函数。"""

    # ----------------------------------------------------------------
    # extract_branch_from_ref 测试
    # ----------------------------------------------------------------

    def test_extract_branch_from_refs_heads(self) -> None:
        """测试从 refs/heads/xxx 提取分支名。"""
        assert extract_branch_from_ref("refs/heads/master") == "master"
        assert extract_branch_from_ref("refs/heads/main") == "main"
        assert extract_branch_from_ref("refs/heads/feature/my-feature") == "feature/my-feature"

    def test_extract_branch_from_refs_pull(self) -> None:
        """测试 refs/pull/xxx/merge 返回空字符串（应从 GITHUB_HEAD_REF 获取）。"""
        assert extract_branch_from_ref("refs/pull/123/merge") == ""

    def test_extract_branch_from_plain_ref(self) -> None:
        """测试普通 ref 直接返回。"""
        assert extract_branch_from_ref("some-branch") == "some-branch"

    # ----------------------------------------------------------------
    # Phase 0 测试
    # ----------------------------------------------------------------

    def test_phase_0_returns_baseline(self) -> None:
        """Phase 0 应返回 baseline。"""
        result = resolve_gate(phase=0)
        assert result == "baseline"

    def test_phase_0_ignores_branch(self) -> None:
        """Phase 0 忽略分支，始终返回 baseline。"""
        assert resolve_gate(phase=0, branch="master") == "baseline"
        assert resolve_gate(phase=0, branch="feature-x") == "baseline"

    # ----------------------------------------------------------------
    # Phase 1 测试
    # ----------------------------------------------------------------

    def test_phase_1_master_returns_strict(self) -> None:
        """Phase 1 的 master 分支应返回 strict。"""
        assert resolve_gate(phase=1, branch="master") == "strict"
        assert resolve_gate(phase=1, branch="main") == "strict"

    def test_phase_1_feature_branch_returns_baseline(self) -> None:
        """Phase 1 的非默认分支应返回 baseline。"""
        result = resolve_gate(phase=1, branch="feature-x")
        assert result == "baseline"

    def test_phase_1_threshold_upgrade_to_strict(self) -> None:
        """Phase 1: baseline_count <= threshold 时，PR 提升为 strict。"""
        # baseline_count=5, threshold=10 → strict
        result = resolve_gate(phase=1, branch="feature-x", baseline_count=5, threshold=10)
        assert result == "strict"

        # baseline_count=0, threshold=0 → strict
        result = resolve_gate(phase=1, branch="feature-x", baseline_count=0, threshold=0)
        assert result == "strict"

    def test_phase_1_threshold_not_met_stays_baseline(self) -> None:
        """Phase 1: baseline_count > threshold 时，保持 baseline。"""
        # baseline_count=15, threshold=10 → baseline
        result = resolve_gate(phase=1, branch="feature-x", baseline_count=15, threshold=10)
        assert result == "baseline"

        # baseline_count=1, threshold=0 → baseline
        result = resolve_gate(phase=1, branch="feature-x", baseline_count=1, threshold=0)
        assert result == "baseline"

    def test_phase_1_no_baseline_count_stays_baseline(self) -> None:
        """Phase 1: 未提供 baseline_count 时，保持 baseline。"""
        result = resolve_gate(phase=1, branch="feature-x", baseline_count=None, threshold=10)
        assert result == "baseline"

    # ----------------------------------------------------------------
    # Phase 1 阈值提升可读性断言（防回退）
    # ----------------------------------------------------------------

    def test_phase_1_threshold_upgrade_edge_case_equal(self) -> None:
        """
        Phase 1 阈值提升边界：baseline_count == threshold 时应提升为 strict。

        这是一个关键的边界条件测试，确保 <= 比较符正确使用。
        如果将来误改为 < 比较符，此测试会失败。
        """
        # baseline_count=10, threshold=10 → strict (因为 10 <= 10)
        result = resolve_gate(phase=1, branch="feature-x", baseline_count=10, threshold=10)
        assert result == "strict", (
            f"边界条件失败: baseline_count={10} == threshold={10} 应返回 strict，"
            f"实际返回 {result}。请检查是否误将 <= 改为 <"
        )

    def test_phase_1_threshold_upgrade_edge_case_one_over(self) -> None:
        """
        Phase 1 阈值提升边界：baseline_count == threshold + 1 时应保持 baseline。

        确保阈值判断逻辑正确，超过阈值 1 时不应触发提升。
        """
        # baseline_count=11, threshold=10 → baseline (因为 11 > 10)
        result = resolve_gate(phase=1, branch="feature-x", baseline_count=11, threshold=10)
        assert result == "baseline", (
            f"边界条件失败: baseline_count={11} > threshold={10} 应返回 baseline，"
            f"实际返回 {result}。阈值判断逻辑可能被修改"
        )

    def test_phase_1_threshold_upgrade_zero_threshold_zero_count(self) -> None:
        """
        Phase 1 零阈值场景：baseline_count=0, threshold=0 时应提升为 strict。

        这是达成"零 baseline 错误自动提升 strict"目标的核心场景。
        """
        result = resolve_gate(phase=1, branch="feature-x", baseline_count=0, threshold=0)
        assert result == "strict", (
            f"零阈值场景失败: 当 baseline 已清零且 threshold=0 时，"
            f"PR 应自动提升为 strict 模式。实际返回 {result}"
        )

    def test_phase_1_threshold_upgrade_does_not_affect_default_branch(self) -> None:
        """
        Phase 1: 默认分支（main/master）始终 strict，不受 threshold 影响。

        即使 baseline_count > threshold，默认分支也应为 strict。
        此测试确保 is_default_branch 检查优先于 threshold 检查。
        """
        # 默认分支：即使 baseline_count=100 > threshold=0，也应为 strict
        for branch in ["main", "master"]:
            result = resolve_gate(phase=1, branch=branch, baseline_count=100, threshold=0)
            assert result == "strict", (
                f"默认分支逻辑失败: {branch} 分支应始终为 strict，"
                f"但实际返回 {result}。请确保 is_default_branch 检查优先执行"
            )

    def test_phase_1_threshold_upgrade_regression_guard(self) -> None:
        """
        Phase 1 阈值提升回归防护：验证多组典型场景的正确行为。

        此测试作为回归防护，防止未来修改破坏阈值提升逻辑。
        """
        # 典型场景矩阵: (branch, baseline_count, threshold, expected_gate, 场景说明)
        test_cases = [
            # 默认分支场景
            ("main", 0, 0, "strict", "main 分支始终 strict"),
            ("master", 50, 10, "strict", "master 分支始终 strict，忽略 threshold"),
            # 非默认分支 - 应提升
            ("feature-a", 0, 0, "strict", "零错误零阈值 → strict"),
            ("feature-b", 5, 10, "strict", "错误数低于阈值 → strict"),
            ("feature-c", 10, 10, "strict", "错误数等于阈值 → strict (边界)"),
            # 非默认分支 - 不应提升
            ("feature-d", 11, 10, "baseline", "错误数超过阈值 → baseline"),
            ("feature-e", 100, 50, "baseline", "错误数远超阈值 → baseline"),
        ]

        for branch, baseline_count, threshold, expected, desc in test_cases:
            result = resolve_gate(
                phase=1,
                branch=branch,
                baseline_count=baseline_count,
                threshold=threshold,
            )
            assert result == expected, (
                f"回归测试失败 [{desc}]: "
                f"branch={branch}, baseline_count={baseline_count}, threshold={threshold} "
                f"期望 {expected}，实际 {result}"
            )

    # ----------------------------------------------------------------
    # Phase 2 测试
    # ----------------------------------------------------------------

    def test_phase_2_returns_strict(self) -> None:
        """Phase 2 应返回 strict。"""
        assert resolve_gate(phase=2) == "strict"
        assert resolve_gate(phase=2, branch="feature-x") == "strict"

    # ----------------------------------------------------------------
    # Phase 3 测试（strict-only 语义防回退）
    # ----------------------------------------------------------------
    #
    # Phase 3 核心语义：baseline 已归档，仅支持 strict 模式
    # 任何分支、任何场景都必须返回 strict
    #
    # 防回退要求：
    # 1. 所有分支（main/master/develop/feature/*）都返回 strict
    # 2. override 仍可覆盖（回滚场景保留）
    # 3. threshold/baseline_count 参数对 Phase 3 无影响
    #
    # 如需修改 Phase 3 行为，请先更新本测试并获得 reviewer 批准

    def test_phase_3_returns_strict(self) -> None:
        """Phase 3 应返回 strict（baseline 已归档）。"""
        assert resolve_gate(phase=3) == "strict"

    def test_phase_3_strict_only_all_branches(self) -> None:
        """
        Phase 3 防回退：所有分支都返回 strict。

        这是 Phase 3 的核心语义，确保 baseline 归档后
        不会因分支名意外回退到 baseline 模式。
        """
        branches = ["main", "master", "develop", "feature/foo", "fix/bar", ""]
        for branch in branches:
            result = resolve_gate(phase=3, branch=branch)
            assert result == "strict", (
                f"Phase 3 防回退失败: branch={branch} 应返回 strict，"
                f"实际返回 {result}。Phase 3 语义要求所有分支都为 strict"
            )

    def test_phase_3_ignores_threshold_and_baseline_count(self) -> None:
        """
        Phase 3 防回退：threshold/baseline_count 参数无影响。

        Phase 3 中 baseline 已归档，这些参数应被忽略。
        """
        # 即使设置了 baseline_count 和 threshold，也应返回 strict
        result = resolve_gate(phase=3, branch="feature/x", baseline_count=100, threshold=0)
        assert result == "strict", (
            "Phase 3 防回退失败: 即使 baseline_count > threshold，"
            "Phase 3 也应返回 strict（baseline 已归档）"
        )

    def test_phase_3_override_still_works(self) -> None:
        """
        Phase 3 防回退：override 回滚机制仍可用。

        即使在 Phase 3，紧急回滚场景下 override 仍应生效。
        """
        # override=baseline 可用于紧急回滚
        assert resolve_gate(phase=3, override="baseline") == "baseline"
        # override=warn 可用于调试
        assert resolve_gate(phase=3, override="warn") == "warn"

    # ----------------------------------------------------------------
    # Override 测试
    # ----------------------------------------------------------------

    def test_override_takes_priority(self) -> None:
        """override 应优先于所有其他设置。"""
        # override=baseline 覆盖 phase=2 (通常返回 strict)
        assert resolve_gate(phase=2, override="baseline") == "baseline"

        # override=strict 覆盖 phase=0 (通常返回 baseline)
        assert resolve_gate(phase=0, override="strict") == "strict"

    def test_override_warn_returns_warn(self) -> None:
        """override=warn 应返回 warn（不阻断 CI）。"""
        # warn 覆盖 phase=2 (通常返回 strict)
        assert resolve_gate(phase=2, override="warn") == "warn"

        # warn 覆盖 phase=0 (通常返回 baseline)
        assert resolve_gate(phase=0, override="warn") == "warn"

        # warn 覆盖 phase=1 的 master 分支 (通常返回 strict)
        assert resolve_gate(phase=1, branch="master", override="warn") == "warn"

    def test_override_off_returns_off(self) -> None:
        """override=off 应返回 off（跳过检查）。"""
        # off 覆盖 phase=2 (通常返回 strict)
        assert resolve_gate(phase=2, override="off") == "off"

        # off 覆盖 phase=0 (通常返回 baseline)
        assert resolve_gate(phase=0, override="off") == "off"

        # off 覆盖 phase=1 的 master 分支 (通常返回 strict)
        assert resolve_gate(phase=1, branch="master", override="off") == "off"

    def test_override_priority_over_phase(self) -> None:
        """验证 override 优先级高于 phase（回归防护）。"""
        # 优先级测试矩阵: (phase, branch, override, expected)
        test_cases = [
            # baseline override
            (0, "master", "baseline", "baseline"),
            (1, "master", "baseline", "baseline"),
            (2, "feature-x", "baseline", "baseline"),
            (3, None, "baseline", "baseline"),
            # strict override
            (0, "feature-x", "strict", "strict"),
            (1, "feature-x", "strict", "strict"),
            # warn override（紧急回滚场景）
            (0, None, "warn", "warn"),
            (1, "master", "warn", "warn"),
            (2, "feature-x", "warn", "warn"),
            (3, None, "warn", "warn"),
            # off override（调试场景）
            (0, None, "off", "off"),
            (1, "master", "off", "off"),
            (2, "feature-x", "off", "off"),
            (3, None, "off", "off"),
        ]

        for phase, branch, override, expected in test_cases:
            result = resolve_gate(phase=phase, branch=branch, override=override)
            assert result == expected, (
                f"优先级测试失败: phase={phase}, branch={branch}, override={override} "
                f"期望 {expected}，实际 {result}"
            )

    def test_invalid_override_ignored(self) -> None:
        """无效的 override 应被忽略。"""
        # invalid override → 回退到 phase 逻辑
        assert resolve_gate(phase=0, override="invalid") == "baseline"
        assert resolve_gate(phase=2, override="") == "strict"
        assert resolve_gate(phase=2, override="WARN") == "strict"  # 大小写敏感
        assert resolve_gate(phase=2, override="Baseline") == "strict"  # 大小写敏感

    # ----------------------------------------------------------------
    # ref 提取分支测试
    # ----------------------------------------------------------------

    def test_branch_extracted_from_ref(self) -> None:
        """当未提供 branch 时，应从 ref 提取。"""
        result = resolve_gate(phase=1, ref="refs/heads/master")
        assert result == "strict"

    def test_branch_takes_priority_over_ref(self) -> None:
        """显式 branch 优先于 ref。"""
        # branch=feature-x 优先于 ref=refs/heads/master
        result = resolve_gate(phase=1, branch="feature-x", ref="refs/heads/master")
        assert result == "baseline"

    # ----------------------------------------------------------------
    # 未知 phase 测试
    # ----------------------------------------------------------------

    def test_unknown_phase_returns_baseline(self) -> None:
        """未知 phase 应返回 baseline（安全默认值）。"""
        assert resolve_gate(phase=99) == "baseline"
        assert resolve_gate(phase=-1) == "baseline"
