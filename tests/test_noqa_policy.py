"""
tests/test_noqa_policy.py

测试 noqa 策略检查脚本的核心功能:
1. 正则表达式匹配
2. 策略验证逻辑
3. 文件扫描功能
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List

import pytest

# 将 scripts/ci 加入 path 以便导入
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "ci"))

from check_noqa_policy import (
    NOQA_PATTERN,
    REASON_PATTERN,
    NoqaEntry,
    NoqaViolation,
    expand_paths,
    get_default_paths,
    is_lint_island_path,
    load_lint_island_paths,
    run_check,
    scan_file_for_noqa,
    validate_noqa,
)

# ============================================================================
# Fixtures
# ============================================================================

PROJECT_ROOT = Path(__file__).parent.parent


@pytest.fixture
def temp_python_file(tmp_path: Path) -> Path:
    """创建临时 Python 文件用于测试。"""
    return tmp_path / "test_file.py"


# ============================================================================
# 正则表达式测试
# ============================================================================


class TestNoqaPattern:
    """测试 NOQA_PATTERN 正则表达式。"""

    def test_matches_bare_noqa(self) -> None:
        """匹配裸 noqa。"""
        line = "import os  # no" + "qa"  # 拆分避免自检测
        match = NOQA_PATTERN.search(line)
        assert match is not None
        assert match.group(1) is None  # 无错误码

    def test_matches_noqa_with_single_code(self) -> None:
        """匹配带单个错误码的 noqa。"""
        line = "import os  # noqa: F401"
        match = NOQA_PATTERN.search(line)
        assert match is not None
        assert match.group(1) == "F401"

    def test_matches_noqa_with_multiple_codes(self) -> None:
        """匹配带多个错误码的 noqa。"""
        line = "import os  # noqa: F401, E501"
        match = NOQA_PATTERN.search(line)
        assert match is not None
        assert match.group(1) == "F401, E501"

    def test_matches_noqa_without_space(self) -> None:
        """匹配无空格的 noqa。"""
        line = "import os  #noqa: F401"
        match = NOQA_PATTERN.search(line)
        assert match is not None
        assert match.group(1) == "F401"

    def test_matches_noqa_with_extra_spaces(self) -> None:
        """匹配带额外空格的 noqa。"""
        line = "import os  #  noqa:  F401"
        match = NOQA_PATTERN.search(line)
        assert match is not None
        assert match.group(1) == "F401"

    def test_no_match_for_regular_comment(self) -> None:
        """不匹配普通注释。"""
        line = "# This is a regular comment"
        match = NOQA_PATTERN.search(line)
        assert match is None

    def test_no_match_for_type_ignore(self) -> None:
        """不匹配 type: ignore。"""
        line = "value = []  # type: ignore[arg-type]"
        match = NOQA_PATTERN.search(line)
        assert match is None


class TestReasonPattern:
    """测试 REASON_PATTERN 正则表达式。"""

    def test_matches_inline_reason(self) -> None:
        """匹配内联原因说明。"""
        line = "import os  # noqa: F401  # re-export for public API"
        match = REASON_PATTERN.search(line)
        assert match is not None
        assert match.group(1).strip() == "re-export for public API"

    def test_matches_todo_reason(self) -> None:
        """匹配 TODO 原因说明。"""
        line = "import os  # noqa: F401  # TODO: #123"
        match = REASON_PATTERN.search(line)
        assert match is not None
        assert "TODO" in match.group(1)

    def test_no_match_without_reason(self) -> None:
        """无原因说明时不匹配。"""
        line = "import os  # noqa: F401"
        match = REASON_PATTERN.search(line)
        assert match is None

    def test_matches_url_reason(self) -> None:
        """匹配 URL 原因说明。"""
        line = "import os  # noqa: F401  # https://github.com/org/repo/issues/123"
        match = REASON_PATTERN.search(line)
        assert match is not None
        assert "https://" in match.group(1)


# ============================================================================
# 配置读取测试
# ============================================================================


class TestGetDefaultPaths:
    """测试 get_default_paths 函数。"""

    def test_returns_src_and_tests(self) -> None:
        """返回 src/ 和 tests/ 目录。"""
        paths = get_default_paths()
        assert "src/" in paths
        assert "tests/" in paths

    def test_returns_list(self) -> None:
        """返回列表类型。"""
        paths = get_default_paths()
        assert isinstance(paths, list)


# ============================================================================
# 路径展开测试
# ============================================================================


class TestExpandPaths:
    """测试 expand_paths 函数。"""

    def test_expands_single_file(self, tmp_path: Path) -> None:
        """展开单个文件。"""
        py_file = tmp_path / "test.py"
        py_file.write_text("# test", encoding="utf-8")

        paths = ["test.py"]
        result = expand_paths(paths, tmp_path)

        assert len(result) == 1
        assert result[0] == py_file

    def test_expands_directory(self, tmp_path: Path) -> None:
        """展开目录。"""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "a.py").write_text("# a", encoding="utf-8")
        (subdir / "b.py").write_text("# b", encoding="utf-8")

        paths = ["subdir/"]
        result = expand_paths(paths, tmp_path)

        assert len(result) == 2

    def test_ignores_non_python_files(self, tmp_path: Path) -> None:
        """忽略非 Python 文件。"""
        (tmp_path / "test.txt").write_text("text", encoding="utf-8")
        (tmp_path / "test.py").write_text("# py", encoding="utf-8")

        paths = ["test.txt", "test.py"]
        result = expand_paths(paths, tmp_path)

        assert len(result) == 1
        assert result[0].suffix == ".py"

    def test_deduplicates_files(self, tmp_path: Path) -> None:
        """去重文件。"""
        py_file = tmp_path / "test.py"
        py_file.write_text("# test", encoding="utf-8")

        paths = ["test.py", "test.py"]
        result = expand_paths(paths, tmp_path)

        assert len(result) == 1


# ============================================================================
# 文件扫描测试
# ============================================================================


class TestScanFileForNoqa:
    """测试 scan_file_for_noqa 函数。"""

    def test_finds_bare_noqa(self, temp_python_file: Path) -> None:
        """找到裸 noqa。"""
        temp_python_file.write_text("import os  # no" + "qa\n", encoding="utf-8")

        entries = list(scan_file_for_noqa(temp_python_file))

        assert len(entries) == 1
        assert entries[0].error_codes is None
        assert entries[0].has_reason is False

    def test_finds_noqa_with_code(self, temp_python_file: Path) -> None:
        """找到带错误码的 noqa。"""
        temp_python_file.write_text("import os  # noqa: F401\n", encoding="utf-8")

        entries = list(scan_file_for_noqa(temp_python_file))

        assert len(entries) == 1
        assert entries[0].error_codes == "F401"

    def test_finds_noqa_with_reason(self, temp_python_file: Path) -> None:
        """找到带原因说明的 noqa。"""
        temp_python_file.write_text("import os  # noqa: F401  # re-export\n", encoding="utf-8")

        entries = list(scan_file_for_noqa(temp_python_file))

        assert len(entries) == 1
        assert entries[0].has_reason is True
        assert entries[0].reason_text == "re-export"

    def test_finds_multiple_noqa(self, temp_python_file: Path) -> None:
        """找到多个 noqa。"""
        # 使用 f-string 拼接避免裸 noqa 被自检测
        bare_noqa = "no" + "qa"
        content = f"""
import os  # noqa: F401
import sys  # noqa: F401  # reason
import json  # {bare_noqa}
"""
        temp_python_file.write_text(content, encoding="utf-8")

        entries = list(scan_file_for_noqa(temp_python_file))

        assert len(entries) == 3

    def test_correct_line_numbers(self, temp_python_file: Path) -> None:
        """行号正确。"""
        content = """# line 1
# line 2
import os  # noqa: F401
# line 4
"""
        temp_python_file.write_text(content, encoding="utf-8")

        entries = list(scan_file_for_noqa(temp_python_file))

        assert len(entries) == 1
        assert entries[0].line_number == 3


# ============================================================================
# 验证逻辑测试
# ============================================================================


class TestValidateNoqa:
    """测试 validate_noqa 函数。"""

    def test_bare_noqa_violates(self, temp_python_file: Path) -> None:
        """裸 noqa 违规。"""
        entry = NoqaEntry(
            file=temp_python_file,
            line_number=1,
            line_content="import os  # no" + "qa",  # 拆分避免自检测
            error_codes=None,
            has_reason=False,
            reason_text=None,
        )

        violation = validate_noqa(entry, require_reason=False)

        assert violation is not None
        assert "裸 no" + "qa" in violation.violation_type

    def test_noqa_with_code_passes(self, temp_python_file: Path) -> None:
        """带错误码的 noqa 通过。"""
        entry = NoqaEntry(
            file=temp_python_file,
            line_number=1,
            line_content="import os  # noqa: F401",
            error_codes="F401",
            has_reason=False,
            reason_text=None,
        )

        violation = validate_noqa(entry, require_reason=False)

        assert violation is None

    def test_noqa_without_reason_violates_in_strict(self, temp_python_file: Path) -> None:
        """缺少原因说明在严格模式下违规。"""
        entry = NoqaEntry(
            file=temp_python_file,
            line_number=1,
            line_content="import os  # noqa: F401",
            error_codes="F401",
            has_reason=False,
            reason_text=None,
        )

        violation = validate_noqa(entry, require_reason=True)

        assert violation is not None
        assert "原因" in violation.violation_type

    def test_noqa_without_reason_passes_when_not_required(self, temp_python_file: Path) -> None:
        """不要求原因时无违规。"""
        entry = NoqaEntry(
            file=temp_python_file,
            line_number=1,
            line_content="import os  # noqa: F401",
            error_codes="F401",
            has_reason=False,
            reason_text=None,
        )

        violation = validate_noqa(entry, require_reason=False)

        assert violation is None

    def test_complete_noqa_passes(self, temp_python_file: Path) -> None:
        """完整的 noqa 通过验证。"""
        entry = NoqaEntry(
            file=temp_python_file,
            line_number=1,
            line_content="import os  # noqa: F401  # reason",
            error_codes="F401",
            has_reason=True,
            reason_text="reason",
        )

        violation = validate_noqa(entry, require_reason=True)

        assert violation is None


# ============================================================================
# 违规记录测试
# ============================================================================


class TestNoqaViolation:
    """测试 NoqaViolation 数据类。"""

    def test_str_format_without_code(self, temp_python_file: Path) -> None:
        """字符串格式（无错误码）。"""
        violation = NoqaViolation(
            file=temp_python_file,
            line_number=10,
            line_content="import os  # no" + "qa",  # 拆分避免自检测
            violation_type="裸 no" + "qa 禁止使用",
        )

        result = str(violation)

        assert str(temp_python_file) in result
        assert "10" in result
        assert "裸 no" + "qa" in result

    def test_str_format_with_code(self, temp_python_file: Path) -> None:
        """字符串格式（有错误码）。"""
        violation = NoqaViolation(
            file=temp_python_file,
            line_number=10,
            line_content="import os  # noqa: F401",
            violation_type="缺少原因说明",
            error_codes="F401",
        )

        result = str(violation)

        assert "[F401]" in result
        assert "缺少原因说明" in result


# ============================================================================
# 集成测试
# ============================================================================


class TestIntegration:
    """集成测试。"""

    def test_scan_and_validate_compliant_file(self, temp_python_file: Path) -> None:
        """扫描并验证合规文件。"""
        content = """
# Compliant file
import os  # noqa: F401  # re-export
import sys  # noqa: F401, E501  # TODO: #123
"""
        temp_python_file.write_text(content, encoding="utf-8")

        entries = list(scan_file_for_noqa(temp_python_file))
        violations: List[NoqaViolation] = []

        for entry in entries:
            violation = validate_noqa(entry, require_reason=True)
            if violation:
                violations.append(violation)

        assert len(entries) == 2
        assert len(violations) == 0

    def test_scan_and_validate_non_compliant_file(self, temp_python_file: Path) -> None:
        """扫描并验证不合规文件。"""
        # 使用 f-string 拼接避免裸 noqa 被自检测
        bare_noqa = "no" + "qa"
        content = f"""
# Non-compliant file
import os  # {bare_noqa}
import sys  # noqa: F401
import json  # noqa: F401  # reason
"""
        temp_python_file.write_text(content, encoding="utf-8")

        entries = list(scan_file_for_noqa(temp_python_file))
        violations: List[NoqaViolation] = []

        for entry in entries:
            violation = validate_noqa(entry, require_reason=True)
            if violation:
                violations.append(violation)

        assert len(entries) == 3
        assert len(violations) == 2  # 2 违规（裸 noqa + 无原因）

    def test_scan_and_validate_bare_noqa_only(self, temp_python_file: Path) -> None:
        """仅检查裸 noqa（不要求原因）。"""
        # 使用 f-string 拼接避免裸 noqa 被自检测
        bare_noqa = "no" + "qa"
        content = f"""
# Mixed file
import os  # {bare_noqa}
import sys  # noqa: F401
"""
        temp_python_file.write_text(content, encoding="utf-8")

        entries = list(scan_file_for_noqa(temp_python_file))
        violations: List[NoqaViolation] = []

        for entry in entries:
            violation = validate_noqa(entry, require_reason=False)
            if violation:
                violations.append(violation)

        assert len(entries) == 2
        assert len(violations) == 1  # 仅裸 noqa 违规

    def test_project_default_paths_exist(self) -> None:
        """项目默认路径应存在。"""
        paths = get_default_paths()
        files = expand_paths(paths, PROJECT_ROOT)

        # 至少应该有一些文件
        assert len(files) > 0, "默认路径下应有 Python 文件"

        # 所有文件应存在
        for f in files:
            assert f.exists(), f"文件应存在: {f}"


# ============================================================================
# lint-island 路径测试
# ============================================================================


class TestLoadLintIslandPaths:
    """测试 load_lint_island_paths 函数。"""

    def test_loads_from_project_root(self) -> None:
        """从项目根目录加载 lint-island 路径。"""
        paths = load_lint_island_paths(PROJECT_ROOT)

        # 应该能读取到配置（pyproject.toml 中有配置）
        assert isinstance(paths, list)
        # 当前配置中应该有一些路径
        assert len(paths) > 0, "应该从 pyproject.toml 读取到 lint-island 路径"

    def test_returns_empty_for_missing_config(self, tmp_path: Path) -> None:
        """配置缺失时返回空列表。"""
        # 创建一个空的 pyproject.toml
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n", encoding="utf-8")

        paths = load_lint_island_paths(tmp_path)
        assert paths == []

    def test_returns_empty_for_no_pyproject(self, tmp_path: Path) -> None:
        """无 pyproject.toml 时返回空列表。"""
        paths = load_lint_island_paths(tmp_path)
        assert paths == []


class TestIsLintIslandPath:
    """测试 is_lint_island_path 函数。"""

    def test_exact_file_match(self, tmp_path: Path) -> None:
        """精确文件匹配。"""
        file_path = tmp_path / "src" / "engram" / "gateway" / "di.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.touch()

        lint_island_paths = ["src/engram/gateway/di.py"]

        assert is_lint_island_path(file_path, lint_island_paths, tmp_path) is True

    def test_directory_match(self, tmp_path: Path) -> None:
        """目录匹配。"""
        file_path = tmp_path / "src" / "engram" / "gateway" / "services" / "audit.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.touch()

        lint_island_paths = ["src/engram/gateway/services/"]

        assert is_lint_island_path(file_path, lint_island_paths, tmp_path) is True

    def test_directory_match_without_trailing_slash(self, tmp_path: Path) -> None:
        """目录匹配（无尾部斜杠）。"""
        file_path = tmp_path / "src" / "engram" / "gateway" / "services" / "audit.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.touch()

        lint_island_paths = ["src/engram/gateway/services"]

        assert is_lint_island_path(file_path, lint_island_paths, tmp_path) is True

    def test_no_match_outside_island(self, tmp_path: Path) -> None:
        """非 island 路径不匹配。"""
        file_path = tmp_path / "src" / "engram" / "logbook" / "db.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.touch()

        lint_island_paths = ["src/engram/gateway/di.py", "src/engram/gateway/services/"]

        assert is_lint_island_path(file_path, lint_island_paths, tmp_path) is False

    def test_empty_island_paths(self, tmp_path: Path) -> None:
        """空 island 路径列表。"""
        file_path = tmp_path / "src" / "engram" / "gateway" / "di.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.touch()

        assert is_lint_island_path(file_path, [], tmp_path) is False


# ============================================================================
# lint-island 严格模式测试
# ============================================================================


class TestLintIslandStrictCLI:
    """测试 lint-island 严格模式 CLI 独立运行。"""

    def test_cli_lint_island_strict_can_run_independently(self) -> None:
        """验证 --lint-island-strict 模式可以独立运行。

        这个测试确保 CI 中使用的 lint-island-strict 模式可以正确执行，
        与 ENGRAM_RUFF_PHASE >= 1 时的行为一致。
        """
        import subprocess

        # 运行脚本（stats-only 模式，不阻断）
        result = subprocess.run(
            [
                "python",
                "scripts/ci/check_noqa_policy.py",
                "--lint-island-strict",
                "--stats-only",
            ],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )

        # 验证脚本可以正常执行
        assert result.returncode == 0, f"脚本执行失败: {result.stderr}"
        assert "lint-island-strict" in result.stdout.lower() or "lint-island" in result.stdout

    def test_cli_default_mode_can_run_independently(self) -> None:
        """验证默认模式可以独立运行。

        这个测试确保 CI 中 ENGRAM_RUFF_PHASE == 0 时的默认模式可以正确执行。
        """
        import subprocess

        # 运行脚本（stats-only 模式，不阻断）
        result = subprocess.run(
            [
                "python",
                "scripts/ci/check_noqa_policy.py",
                "--stats-only",
            ],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )

        # 验证脚本可以正常执行
        assert result.returncode == 0, f"脚本执行失败: {result.stderr}"
        assert "noqa" in result.stdout.lower()


class TestLintIslandStrict:
    """测试 lint-island 严格模式。"""

    def test_island_file_without_reason_fails(self, tmp_path: Path) -> None:
        """lint-island 文件中无 reason 的 noqa 应失败。"""
        # 创建 lint-island 文件
        island_dir = tmp_path / "src" / "engram" / "gateway" / "services"
        island_dir.mkdir(parents=True, exist_ok=True)
        island_file = island_dir / "test_service.py"
        island_file.write_text("import os  # noqa: F401\n", encoding="utf-8")

        lint_island_paths = ["src/engram/gateway/services/"]

        violations, total_noqa = run_check(
            paths=["src/"],
            verbose=False,
            stats_only=False,
            require_reason=False,
            lint_island_strict=True,
            lint_island_paths=lint_island_paths,
            project_root=tmp_path,
        )

        # island 文件中无 reason 应产生违规
        assert total_noqa == 1
        assert len(violations) == 1
        assert "原因" in violations[0].violation_type

    def test_island_file_with_reason_passes(self, tmp_path: Path) -> None:
        """lint-island 文件中有 reason 的 noqa 应通过。"""
        # 创建 lint-island 文件
        island_dir = tmp_path / "src" / "engram" / "gateway" / "services"
        island_dir.mkdir(parents=True, exist_ok=True)
        island_file = island_dir / "test_service.py"
        island_file.write_text(
            "import os  # noqa: F401  # re-export for public API\n", encoding="utf-8"
        )

        lint_island_paths = ["src/engram/gateway/services/"]

        violations, total_noqa = run_check(
            paths=["src/"],
            verbose=False,
            stats_only=False,
            require_reason=False,
            lint_island_strict=True,
            lint_island_paths=lint_island_paths,
            project_root=tmp_path,
        )

        # island 文件中有 reason 应通过
        assert total_noqa == 1
        assert len(violations) == 0

    def test_non_island_file_without_reason_passes(self, tmp_path: Path) -> None:
        """非 island 文件中无 reason 的 noqa 应通过（仅要求错误码）。"""
        # 创建非 island 文件
        non_island_dir = tmp_path / "src" / "engram" / "logbook"
        non_island_dir.mkdir(parents=True, exist_ok=True)
        non_island_file = non_island_dir / "db.py"
        non_island_file.write_text("import os  # noqa: F401\n", encoding="utf-8")

        lint_island_paths = ["src/engram/gateway/services/"]

        violations, total_noqa = run_check(
            paths=["src/"],
            verbose=False,
            stats_only=False,
            require_reason=False,
            lint_island_strict=True,
            lint_island_paths=lint_island_paths,
            project_root=tmp_path,
        )

        # 非 island 文件中无 reason 应通过（只要有错误码）
        assert total_noqa == 1
        assert len(violations) == 0

    def test_non_island_bare_noqa_fails(self, tmp_path: Path) -> None:
        """非 island 文件中裸 noqa 应失败。"""
        # 使用 f-string 拼接避免裸 noqa 被自检测
        bare_noqa = "no" + "qa"

        # 创建非 island 文件
        non_island_dir = tmp_path / "src" / "engram" / "logbook"
        non_island_dir.mkdir(parents=True, exist_ok=True)
        non_island_file = non_island_dir / "db.py"
        non_island_file.write_text(f"import os  # {bare_noqa}\n", encoding="utf-8")

        lint_island_paths = ["src/engram/gateway/services/"]

        violations, total_noqa = run_check(
            paths=["src/"],
            verbose=False,
            stats_only=False,
            require_reason=False,
            lint_island_strict=True,
            lint_island_paths=lint_island_paths,
            project_root=tmp_path,
        )

        # 非 island 文件中裸 noqa 仍应失败（全仓策略）
        assert total_noqa == 1
        assert len(violations) == 1
        assert f"裸 {bare_noqa}" in violations[0].violation_type

    def test_mixed_files_correct_violations(self, tmp_path: Path) -> None:
        """混合文件检查：island 和非 island 文件各自遵循正确策略。"""
        bare_noqa = "no" + "qa"

        # 创建 island 文件（无 reason）
        island_dir = tmp_path / "src" / "engram" / "gateway" / "services"
        island_dir.mkdir(parents=True, exist_ok=True)
        island_file = island_dir / "test_service.py"
        island_file.write_text("import os  # noqa: F401\n", encoding="utf-8")

        # 创建非 island 文件（有错误码，无 reason）
        non_island_dir = tmp_path / "src" / "engram" / "logbook"
        non_island_dir.mkdir(parents=True, exist_ok=True)
        non_island_file = non_island_dir / "db.py"
        non_island_file.write_text("import sys  # noqa: F401\n", encoding="utf-8")

        # 创建非 island 文件（裸 noqa）
        non_island_file2 = non_island_dir / "utils.py"
        non_island_file2.write_text(f"import json  # {bare_noqa}\n", encoding="utf-8")

        lint_island_paths = ["src/engram/gateway/services/"]

        violations, total_noqa = run_check(
            paths=["src/"],
            verbose=False,
            stats_only=False,
            require_reason=False,
            lint_island_strict=True,
            lint_island_paths=lint_island_paths,
            project_root=tmp_path,
        )

        # 共 3 个 noqa 条目
        assert total_noqa == 3
        # 2 个违规：island 文件无 reason + 非 island 文件裸 noqa
        assert len(violations) == 2
