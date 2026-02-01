"""
tests/ci/test_strict_island_admission.py

测试 scripts/ci/check_strict_island_admission.py 的核心功能:
1. baseline 错误计数
2. module 路径转换
3. override 匹配逻辑
4. override 配置验证
5. 完整准入检查流程
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# 将 scripts/ci 加入 path 以便导入
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))

from check_strict_island_admission import (
    AdmissionResult,
    CheckResult,
    check_candidate,
    check_override_config,
    count_baseline_errors,
    find_matching_override,
    load_candidates_from_file,
    load_pyproject_overrides,
    main,
    module_matches_path,
    path_to_module,
    run_check,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def temp_baseline_file(tmp_path: Path) -> Path:
    """创建临时 baseline 文件路径。"""
    return tmp_path / "mypy_baseline.txt"


@pytest.fixture
def temp_pyproject_file(tmp_path: Path) -> Path:
    """创建临时 pyproject.toml 文件路径。"""
    return tmp_path / "pyproject.toml"


@pytest.fixture
def temp_candidates_file(tmp_path: Path) -> Path:
    """创建临时 candidates.json 文件路径。"""
    return tmp_path / "candidates.json"


@pytest.fixture
def sample_baseline_content() -> str:
    """示例 baseline 内容。"""
    return """src/engram/logbook/artifact_gc.py: error: Argument 1 has incompatible type  [arg-type]
src/engram/logbook/artifact_gc.py: error: Cannot find module  [import-not-found]
src/engram/logbook/views.py: error: Argument 1 has incompatible type  [arg-type]
src/engram/gateway/foo.py: note: See https://mypy.readthedocs.io/en/stable/
"""


@pytest.fixture
def sample_pyproject_content() -> str:
    """示例 pyproject.toml 内容（包含正确的 overrides 配置）。"""
    return """
[tool.mypy]
python_version = "3.10"

[[tool.mypy.overrides]]
module = "engram.gateway.di"
disallow_untyped_defs = true
disallow_incomplete_defs = true
ignore_missing_imports = false
warn_return_any = true

[[tool.mypy.overrides]]
module = "engram.gateway.services.*"
disallow_untyped_defs = true
ignore_missing_imports = false

[[tool.mypy.overrides]]
module = "engram.logbook.config"
disallow_untyped_defs = true
ignore_missing_imports = false
"""


# ============================================================================
# path_to_module 测试
# ============================================================================


class TestPathToModule:
    """测试 path_to_module 函数。"""

    def test_simple_file(self) -> None:
        """简单 .py 文件路径转换。"""
        assert path_to_module("src/engram/gateway/di.py") == "engram.gateway.di"

    def test_file_without_src_prefix(self) -> None:
        """不带 src/ 前缀的路径。"""
        assert path_to_module("engram/gateway/di.py") == "engram.gateway.di"

    def test_directory_path(self) -> None:
        """目录路径（以 / 结尾）。"""
        assert path_to_module("src/engram/gateway/services/") == "engram.gateway.services.*"

    def test_wildcard_path(self) -> None:
        """带通配符的路径。"""
        assert path_to_module("src/engram/gateway/services/*") == "engram.gateway.services.*"

    def test_nested_file(self) -> None:
        """深层嵌套文件。"""
        assert (
            path_to_module("src/engram/logbook/cli/db_migrate.py")
            == "engram.logbook.cli.db_migrate"
        )


# ============================================================================
# module_matches_path 测试
# ============================================================================


class TestModuleMatchesPath:
    """测试 module_matches_path 函数。"""

    def test_exact_match_file(self) -> None:
        """精确匹配文件。"""
        assert module_matches_path("engram.gateway.di", "src/engram/gateway/di.py")

    def test_exact_match_module_wildcard(self) -> None:
        """模块通配符匹配。"""
        assert module_matches_path(
            "engram.gateway.services.*", "src/engram/gateway/services/audit_service.py"
        )

    def test_directory_matches_wildcard(self) -> None:
        """目录路径匹配通配符模块。"""
        assert module_matches_path("engram.gateway.services.*", "src/engram/gateway/services/")

    def test_no_match_different_module(self) -> None:
        """不同模块不匹配。"""
        assert not module_matches_path("engram.gateway.di", "src/engram/logbook/config.py")

    def test_wildcard_matches_nested(self) -> None:
        """通配符匹配嵌套模块。"""
        assert module_matches_path(
            "engram.gateway.*", "src/engram/gateway/services/audit_service.py"
        )

    def test_path_directory_matches_submodule(self) -> None:
        """路径目录匹配子模块。"""
        assert module_matches_path(
            "engram.gateway.services.audit_service", "src/engram/gateway/services/"
        )


# ============================================================================
# count_baseline_errors 测试
# ============================================================================


class TestCountBaselineErrors:
    """测试 count_baseline_errors 函数。"""

    def test_count_file_errors(
        self, temp_baseline_file: Path, sample_baseline_content: str
    ) -> None:
        """统计单个文件的错误数。"""
        temp_baseline_file.write_text(sample_baseline_content)
        count = count_baseline_errors(temp_baseline_file, "src/engram/logbook/artifact_gc.py")
        assert count == 2  # 2 个 error 行

    def test_count_directory_errors(
        self, temp_baseline_file: Path, sample_baseline_content: str
    ) -> None:
        """统计目录下所有文件的错误数。"""
        temp_baseline_file.write_text(sample_baseline_content)
        count = count_baseline_errors(temp_baseline_file, "src/engram/logbook/")
        assert count == 3  # artifact_gc.py (2) + views.py (1)

    def test_no_errors(self, temp_baseline_file: Path, sample_baseline_content: str) -> None:
        """无错误的文件。"""
        temp_baseline_file.write_text(sample_baseline_content)
        count = count_baseline_errors(temp_baseline_file, "src/engram/gateway/di.py")
        assert count == 0

    def test_nonexistent_baseline(self, temp_baseline_file: Path) -> None:
        """baseline 文件不存在时返回 0。"""
        count = count_baseline_errors(temp_baseline_file, "src/engram/gateway/di.py")
        assert count == 0

    def test_empty_baseline(self, temp_baseline_file: Path) -> None:
        """空 baseline 文件。"""
        temp_baseline_file.write_text("")
        count = count_baseline_errors(temp_baseline_file, "src/engram/gateway/di.py")
        assert count == 0

    def test_note_lines_counted(
        self, temp_baseline_file: Path, sample_baseline_content: str
    ) -> None:
        """note 行也被计数。"""
        temp_baseline_file.write_text(sample_baseline_content)
        count = count_baseline_errors(temp_baseline_file, "src/engram/gateway/foo.py")
        assert count == 1  # 1 个 note 行


# ============================================================================
# find_matching_override 测试
# ============================================================================


class TestFindMatchingOverride:
    """测试 find_matching_override 函数。"""

    def test_find_exact_match(self) -> None:
        """精确匹配。"""
        overrides = [
            {"module": "engram.gateway.di", "disallow_untyped_defs": True},
            {"module": "engram.logbook.config", "disallow_untyped_defs": True},
        ]
        result = find_matching_override(overrides, "src/engram/gateway/di.py")
        assert result is not None
        assert result["module"] == "engram.gateway.di"

    def test_find_wildcard_match(self) -> None:
        """通配符匹配。"""
        overrides = [
            {"module": "engram.gateway.services.*", "disallow_untyped_defs": True},
        ]
        result = find_matching_override(overrides, "src/engram/gateway/services/audit_service.py")
        assert result is not None
        assert result["module"] == "engram.gateway.services.*"

    def test_no_match(self) -> None:
        """无匹配。"""
        overrides = [
            {"module": "engram.gateway.di", "disallow_untyped_defs": True},
        ]
        result = find_matching_override(overrides, "src/engram/logbook/foo.py")
        assert result is None


# ============================================================================
# check_override_config 测试
# ============================================================================


class TestCheckOverrideConfig:
    """测试 check_override_config 函数。"""

    def test_valid_config(self) -> None:
        """有效配置。"""
        override = {
            "module": "engram.gateway.di",
            "disallow_untyped_defs": True,
            "ignore_missing_imports": False,
        }
        disallow_ok, ignore_ok, errors = check_override_config(override)
        assert disallow_ok is True
        assert ignore_ok is True
        assert len(errors) == 0

    def test_missing_disallow_untyped_defs(self) -> None:
        """缺少 disallow_untyped_defs。"""
        override = {
            "module": "engram.gateway.di",
            "ignore_missing_imports": False,
        }
        disallow_ok, ignore_ok, errors = check_override_config(override)
        assert disallow_ok is False
        assert ignore_ok is True
        assert len(errors) == 1
        assert "disallow_untyped_defs" in errors[0]

    def test_wrong_ignore_missing_imports(self) -> None:
        """ignore_missing_imports 设为 True。"""
        override = {
            "module": "engram.gateway.di",
            "disallow_untyped_defs": True,
            "ignore_missing_imports": True,
        }
        disallow_ok, ignore_ok, errors = check_override_config(override)
        assert disallow_ok is True
        assert ignore_ok is False
        assert len(errors) == 1
        assert "ignore_missing_imports" in errors[0]

    def test_both_wrong(self) -> None:
        """两个配置都不正确。"""
        override = {
            "module": "engram.gateway.di",
            "disallow_untyped_defs": False,
            "ignore_missing_imports": True,
        }
        disallow_ok, ignore_ok, errors = check_override_config(override)
        assert disallow_ok is False
        assert ignore_ok is False
        assert len(errors) == 2


# ============================================================================
# load_candidates_from_file 测试
# ============================================================================


class TestLoadCandidatesFromFile:
    """测试 load_candidates_from_file 函数。"""

    def test_load_simple_list(self, temp_candidates_file: Path) -> None:
        """加载简单列表格式。"""
        temp_candidates_file.write_text('["path1", "path2"]')
        candidates = load_candidates_from_file(temp_candidates_file)
        assert candidates == ["path1", "path2"]

    def test_load_object_with_candidates_key(self, temp_candidates_file: Path) -> None:
        """加载带 candidates 键的对象格式。"""
        temp_candidates_file.write_text('{"candidates": ["path1", "path2"]}')
        candidates = load_candidates_from_file(temp_candidates_file)
        assert candidates == ["path1", "path2"]

    def test_load_object_with_strict_island_candidates_key(
        self, temp_candidates_file: Path
    ) -> None:
        """加载带 strict_island_candidates 键的对象格式。"""
        temp_candidates_file.write_text('{"strict_island_candidates": ["path1", "path2"]}')
        candidates = load_candidates_from_file(temp_candidates_file)
        assert candidates == ["path1", "path2"]

    def test_load_invalid_format(self, temp_candidates_file: Path) -> None:
        """无效格式应抛出异常。"""
        temp_candidates_file.write_text('{"unknown_key": ["path1"]}')
        with pytest.raises(ValueError, match="未找到有效的候选路径列表"):
            load_candidates_from_file(temp_candidates_file)


# ============================================================================
# load_pyproject_overrides 测试
# ============================================================================


class TestLoadPyprojectOverrides:
    """测试 load_pyproject_overrides 函数。"""

    def test_load_overrides(self, temp_pyproject_file: Path, sample_pyproject_content: str) -> None:
        """正常加载 overrides。"""
        temp_pyproject_file.write_text(sample_pyproject_content)
        overrides = load_pyproject_overrides(temp_pyproject_file)
        assert len(overrides) == 3
        assert overrides[0]["module"] == "engram.gateway.di"

    def test_empty_pyproject(self, temp_pyproject_file: Path) -> None:
        """空 pyproject.toml。"""
        temp_pyproject_file.write_text("")
        overrides = load_pyproject_overrides(temp_pyproject_file)
        assert overrides == []

    def test_no_overrides(self, temp_pyproject_file: Path) -> None:
        """pyproject.toml 无 overrides。"""
        temp_pyproject_file.write_text('[tool.mypy]\npython_version = "3.10"')
        overrides = load_pyproject_overrides(temp_pyproject_file)
        assert overrides == []


# ============================================================================
# check_candidate 测试
# ============================================================================


class TestCheckCandidate:
    """测试 check_candidate 函数。"""

    def test_pass_all_conditions(
        self,
        temp_baseline_file: Path,
    ) -> None:
        """满足所有条件。"""
        temp_baseline_file.write_text("")  # 空 baseline
        overrides = [
            {
                "module": "engram.gateway.di",
                "disallow_untyped_defs": True,
                "ignore_missing_imports": False,
            }
        ]

        result = check_candidate(
            "src/engram/gateway/di.py",
            temp_baseline_file,
            overrides,
        )

        assert result.passed is True
        assert result.baseline_error_count == 0
        assert result.has_override is True
        assert len(result.errors) == 0

    def test_fail_baseline_errors(
        self,
        temp_baseline_file: Path,
        sample_baseline_content: str,
    ) -> None:
        """baseline 中存在错误。"""
        temp_baseline_file.write_text(sample_baseline_content)
        overrides = [
            {
                "module": "engram.logbook.artifact_gc",
                "disallow_untyped_defs": True,
                "ignore_missing_imports": False,
            }
        ]

        result = check_candidate(
            "src/engram/logbook/artifact_gc.py",
            temp_baseline_file,
            overrides,
        )

        assert result.passed is False
        assert result.baseline_error_count == 2
        assert any(e.error_type == "baseline_errors" for e in result.errors)

    def test_fail_missing_override(self, temp_baseline_file: Path) -> None:
        """缺少 override 配置。"""
        temp_baseline_file.write_text("")
        overrides: list[dict] = []

        result = check_candidate(
            "src/engram/gateway/di.py",
            temp_baseline_file,
            overrides,
        )

        assert result.passed is False
        assert result.has_override is False
        assert any(e.error_type == "missing_override" for e in result.errors)

    def test_fail_wrong_config(self, temp_baseline_file: Path) -> None:
        """override 配置不正确。"""
        temp_baseline_file.write_text("")
        overrides = [
            {
                "module": "engram.gateway.di",
                "disallow_untyped_defs": False,  # 错误
                "ignore_missing_imports": True,  # 错误
            }
        ]

        result = check_candidate(
            "src/engram/gateway/di.py",
            temp_baseline_file,
            overrides,
        )

        assert result.passed is False
        assert result.has_override is True
        config_errors = [e for e in result.errors if e.error_type == "config_error"]
        assert len(config_errors) == 2


# ============================================================================
# run_check 集成测试
# ============================================================================


class TestRunCheck:
    """测试 run_check 函数。"""

    def test_all_pass(
        self,
        temp_baseline_file: Path,
        temp_pyproject_file: Path,
        sample_pyproject_content: str,
    ) -> None:
        """所有候选都通过。"""
        temp_baseline_file.write_text("")  # 空 baseline
        temp_pyproject_file.write_text(sample_pyproject_content)

        result = run_check(
            candidates=["src/engram/gateway/di.py", "src/engram/logbook/config.py"],
            baseline_path=temp_baseline_file,
            pyproject_path=temp_pyproject_file,
        )

        assert result.ok is True
        assert result.candidates_checked == 2
        assert result.passed_count == 2
        assert result.failed_count == 0

    def test_some_fail(
        self,
        temp_baseline_file: Path,
        temp_pyproject_file: Path,
        sample_pyproject_content: str,
        sample_baseline_content: str,
    ) -> None:
        """部分候选失败。"""
        temp_baseline_file.write_text(sample_baseline_content)
        temp_pyproject_file.write_text(sample_pyproject_content)

        result = run_check(
            candidates=[
                "src/engram/gateway/di.py",  # 通过
                "src/engram/logbook/artifact_gc.py",  # 失败（有 baseline 错误，无 override）
            ],
            baseline_path=temp_baseline_file,
            pyproject_path=temp_pyproject_file,
        )

        assert result.ok is False
        assert result.candidates_checked == 2
        assert result.passed_count == 1
        assert result.failed_count == 1

    def test_pyproject_not_exists(self, temp_baseline_file: Path, tmp_path: Path) -> None:
        """pyproject.toml 不存在。"""
        temp_baseline_file.write_text("")
        nonexistent = tmp_path / "nonexistent.toml"

        result = run_check(
            candidates=["src/engram/gateway/di.py"],
            baseline_path=temp_baseline_file,
            pyproject_path=nonexistent,
        )

        assert result.ok is False
        assert len(result.config_errors) >= 1
        assert "不存在" in result.config_errors[0]


# ============================================================================
# main() CLI 测试
# ============================================================================


class TestMain:
    """测试 main() 函数。"""

    def test_pass_returns_zero(
        self,
        tmp_path: Path,
        sample_pyproject_content: str,
    ) -> None:
        """检查通过返回 0。"""
        baseline_file = tmp_path / "mypy_baseline.txt"
        pyproject_file = tmp_path / "pyproject.toml"

        baseline_file.write_text("")
        pyproject_file.write_text(sample_pyproject_content)

        with patch(
            "sys.argv",
            [
                "check_strict_island_admission.py",
                "--candidate",
                "src/engram/gateway/di.py",
                "--baseline-file",
                str(baseline_file),
                "--pyproject",
                str(pyproject_file),
            ],
        ):
            exit_code = main()

        assert exit_code == 0

    def test_fail_returns_one(
        self,
        tmp_path: Path,
        sample_pyproject_content: str,
        sample_baseline_content: str,
    ) -> None:
        """检查失败返回 1。"""
        baseline_file = tmp_path / "mypy_baseline.txt"
        pyproject_file = tmp_path / "pyproject.toml"

        baseline_file.write_text(sample_baseline_content)
        pyproject_file.write_text(sample_pyproject_content)

        with patch(
            "sys.argv",
            [
                "check_strict_island_admission.py",
                "--candidate",
                "src/engram/logbook/artifact_gc.py",  # 有 baseline 错误
                "--baseline-file",
                str(baseline_file),
                "--pyproject",
                str(pyproject_file),
            ],
        ):
            exit_code = main()

        assert exit_code == 1

    def test_no_candidates_returns_two(self, tmp_path: Path) -> None:
        """未指定候选路径返回 2。"""
        with patch(
            "sys.argv",
            [
                "check_strict_island_admission.py",
                "--baseline-file",
                str(tmp_path / "baseline.txt"),
            ],
        ):
            exit_code = main()

        assert exit_code == 2

    def test_json_output(
        self,
        tmp_path: Path,
        sample_pyproject_content: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """JSON 输出格式正确。"""
        baseline_file = tmp_path / "mypy_baseline.txt"
        pyproject_file = tmp_path / "pyproject.toml"

        baseline_file.write_text("")
        pyproject_file.write_text(sample_pyproject_content)

        with patch(
            "sys.argv",
            [
                "check_strict_island_admission.py",
                "--candidate",
                "src/engram/gateway/di.py",
                "--baseline-file",
                str(baseline_file),
                "--pyproject",
                str(pyproject_file),
                "--json",
            ],
        ):
            main()

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert "ok" in data
        assert "candidates_checked" in data
        assert "results" in data
        assert data["ok"] is True

    def test_candidates_file(
        self,
        tmp_path: Path,
        sample_pyproject_content: str,
    ) -> None:
        """从文件读取候选路径。"""
        baseline_file = tmp_path / "mypy_baseline.txt"
        pyproject_file = tmp_path / "pyproject.toml"
        candidates_file = tmp_path / "candidates.json"

        baseline_file.write_text("")
        pyproject_file.write_text(sample_pyproject_content)
        candidates_file.write_text('["src/engram/gateway/di.py"]')

        with patch(
            "sys.argv",
            [
                "check_strict_island_admission.py",
                "--candidates-file",
                str(candidates_file),
                "--baseline-file",
                str(baseline_file),
                "--pyproject",
                str(pyproject_file),
            ],
        ):
            exit_code = main()

        assert exit_code == 0


# ============================================================================
# CheckResult 和 AdmissionResult 测试
# ============================================================================


class TestDataClasses:
    """测试数据类。"""

    def test_admission_result_to_dict(self) -> None:
        """AdmissionResult.to_dict 正确序列化。"""
        result = AdmissionResult(
            candidate="src/engram/gateway/di.py",
            passed=True,
            baseline_error_count=0,
            has_override=True,
            disallow_untyped_defs=True,
            ignore_missing_imports=False,
        )

        data = result.to_dict()

        assert data["candidate"] == "src/engram/gateway/di.py"
        assert data["passed"] is True
        assert data["baseline_error_count"] == 0
        assert data["has_override"] is True
        assert data["disallow_untyped_defs"] is True
        assert data["ignore_missing_imports"] is False

    def test_check_result_to_dict(self) -> None:
        """CheckResult.to_dict 正确序列化。"""
        result = CheckResult(
            ok=True,
            candidates_checked=2,
            passed_count=2,
            failed_count=0,
        )

        data = result.to_dict()

        assert data["ok"] is True
        assert data["candidates_checked"] == 2
        assert data["passed_count"] == 2
        assert data["failed_count"] == 0
