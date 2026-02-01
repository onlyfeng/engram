"""
tests/test_type_ignore_policy.py

测试 type: ignore 策略检查脚本的核心功能:
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

from check_type_ignore_policy import (
    REASON_PATTERN,
    TYPE_IGNORE_PATTERN,
    TypeIgnoreEntry,
    TypeIgnoreViolation,
    expand_paths,
    load_strict_island_paths,
    scan_file_for_type_ignores,
    validate_type_ignore,
)

# ============================================================================
# Fixtures
# ============================================================================

PROJECT_ROOT = Path(__file__).parent.parent
PYPROJECT_PATH = PROJECT_ROOT / "pyproject.toml"


@pytest.fixture
def temp_python_file(tmp_path: Path) -> Path:
    """创建临时 Python 文件用于测试。"""
    return tmp_path / "test_file.py"


# ============================================================================
# 正则表达式测试
# ============================================================================


class TestTypeIgnorePattern:
    """测试 TYPE_IGNORE_PATTERN 正则表达式。"""

    def test_matches_bare_ignore(self) -> None:
        """匹配裸 type: ignore。"""
        line = "value = func()  # type: ignore"
        match = TYPE_IGNORE_PATTERN.search(line)
        assert match is not None
        assert match.group(1) is None  # 无错误码

    def test_matches_ignore_with_error_code(self) -> None:
        """匹配带错误码的 type: ignore。"""
        line = "value = func()  # type: ignore[arg-type]"
        match = TYPE_IGNORE_PATTERN.search(line)
        assert match is not None
        assert match.group(1) == "arg-type"

    def test_matches_ignore_with_multiple_codes(self) -> None:
        """匹配带多个错误码的 type: ignore。"""
        line = "value = func()  # type: ignore[arg-type, return-value]"
        match = TYPE_IGNORE_PATTERN.search(line)
        assert match is not None
        assert match.group(1) == "arg-type, return-value"

    def test_matches_with_extra_spaces(self) -> None:
        """匹配带额外空格的 type: ignore。"""
        line = "value = func()  #  type:  ignore  [arg-type]"
        match = TYPE_IGNORE_PATTERN.search(line)
        assert match is not None
        assert match.group(1) == "arg-type"

    def test_no_match_for_regular_comment(self) -> None:
        """不匹配普通注释。"""
        line = "# This is a regular comment"
        match = TYPE_IGNORE_PATTERN.search(line)
        assert match is None

    def test_no_match_for_type_comment(self) -> None:
        """不匹配其他 type 注释。"""
        line = "value = []  # type: List[str]"
        match = TYPE_IGNORE_PATTERN.search(line)
        assert match is None


class TestReasonPattern:
    """测试 REASON_PATTERN 正则表达式。"""

    def test_matches_inline_reason(self) -> None:
        """匹配内联原因说明。"""
        line = "value = func()  # type: ignore[arg-type]  # 第三方库类型不完整"
        match = REASON_PATTERN.search(line)
        assert match is not None
        assert match.group(1).strip() == "第三方库类型不完整"

    def test_matches_todo_reason(self) -> None:
        """匹配 TODO 原因说明。"""
        line = "value = func()  # type: ignore[arg-type]  # TODO: #123"
        match = REASON_PATTERN.search(line)
        assert match is not None
        assert "TODO" in match.group(1)

    def test_no_match_without_reason(self) -> None:
        """无原因说明时不匹配。"""
        line = "value = func()  # type: ignore[arg-type]"
        match = REASON_PATTERN.search(line)
        assert match is None

    def test_matches_url_reason(self) -> None:
        """匹配 URL 原因说明。"""
        line = "value = func()  # type: ignore[arg-type]  # https://github.com/org/repo/issues/123"
        match = REASON_PATTERN.search(line)
        assert match is not None
        assert "https://" in match.group(1)


# ============================================================================
# 配置读取测试
# ============================================================================


class TestLoadStrictIslandPaths:
    """测试 load_strict_island_paths 函数。"""

    def test_loads_from_pyproject(self) -> None:
        """从 pyproject.toml 加载配置。"""
        paths = load_strict_island_paths(PYPROJECT_PATH)
        assert isinstance(paths, list)
        assert len(paths) > 0

    def test_paths_are_strings(self) -> None:
        """路径应为字符串。"""
        paths = load_strict_island_paths(PYPROJECT_PATH)
        for path in paths:
            assert isinstance(path, str)

    def test_paths_start_with_src(self) -> None:
        """路径应以 src/ 开头。"""
        paths = load_strict_island_paths(PYPROJECT_PATH)
        for path in paths:
            assert path.startswith("src/"), f"路径应以 'src/' 开头: {path}"

    def test_missing_file_raises_error(self, tmp_path: Path) -> None:
        """文件不存在时抛出错误。"""
        with pytest.raises(FileNotFoundError):
            load_strict_island_paths(tmp_path / "nonexistent.toml")

    def test_missing_config_raises_error(self, tmp_path: Path) -> None:
        """缺少配置节时抛出错误。"""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project]\nname = 'test'\n", encoding="utf-8")

        with pytest.raises(KeyError):
            load_strict_island_paths(pyproject)


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


class TestScanFileForTypeIgnores:
    """测试 scan_file_for_type_ignores 函数。"""

    def test_finds_bare_ignore(self, temp_python_file: Path) -> None:
        """找到裸 type: ignore。"""
        temp_python_file.write_text("value = func()  # type: ignore\n", encoding="utf-8")

        entries = list(scan_file_for_type_ignores(temp_python_file))

        assert len(entries) == 1
        assert entries[0].error_code is None
        assert entries[0].has_reason is False

    def test_finds_ignore_with_code(self, temp_python_file: Path) -> None:
        """找到带错误码的 type: ignore。"""
        temp_python_file.write_text("value = func()  # type: ignore[arg-type]\n", encoding="utf-8")

        entries = list(scan_file_for_type_ignores(temp_python_file))

        assert len(entries) == 1
        assert entries[0].error_code == "arg-type"

    def test_finds_ignore_with_reason(self, temp_python_file: Path) -> None:
        """找到带原因说明的 type: ignore。"""
        temp_python_file.write_text(
            "value = func()  # type: ignore[arg-type]  # 第三方库\n", encoding="utf-8"
        )

        entries = list(scan_file_for_type_ignores(temp_python_file))

        assert len(entries) == 1
        assert entries[0].has_reason is True
        assert entries[0].reason_text == "第三方库"

    def test_finds_multiple_ignores(self, temp_python_file: Path) -> None:
        """找到多个 type: ignore。"""
        content = """
value1 = func1()  # type: ignore[arg-type]
value2 = func2()  # type: ignore[return-value]  # reason
value3 = func3()  # type: ignore
"""
        temp_python_file.write_text(content, encoding="utf-8")

        entries = list(scan_file_for_type_ignores(temp_python_file))

        assert len(entries) == 3

    def test_correct_line_numbers(self, temp_python_file: Path) -> None:
        """行号正确。"""
        content = """# line 1
# line 2
value = func()  # type: ignore[arg-type]
# line 4
"""
        temp_python_file.write_text(content, encoding="utf-8")

        entries = list(scan_file_for_type_ignores(temp_python_file))

        assert len(entries) == 1
        assert entries[0].line_number == 3


# ============================================================================
# 验证逻辑测试
# ============================================================================


class TestValidateTypeIgnore:
    """测试 validate_type_ignore 函数。"""

    def test_bare_ignore_violates(self, temp_python_file: Path) -> None:
        """裸 type: ignore 违规。"""
        entry = TypeIgnoreEntry(
            file=temp_python_file,
            line_number=1,
            line_content="value = func()  # type: ignore",
            error_code=None,
            has_reason=False,
            reason_text=None,
        )

        violation = validate_type_ignore(entry, require_reason=True)

        assert violation is not None
        assert "错误码" in violation.violation_type

    def test_ignore_without_reason_violates_in_strict(self, temp_python_file: Path) -> None:
        """缺少原因说明在 strict 模式下违规。"""
        entry = TypeIgnoreEntry(
            file=temp_python_file,
            line_number=1,
            line_content="value = func()  # type: ignore[arg-type]",
            error_code="arg-type",
            has_reason=False,
            reason_text=None,
        )

        violation = validate_type_ignore(entry, require_reason=True)

        assert violation is not None
        assert "原因" in violation.violation_type

    def test_ignore_without_reason_passes_when_not_required(self, temp_python_file: Path) -> None:
        """不要求原因时无违规。"""
        entry = TypeIgnoreEntry(
            file=temp_python_file,
            line_number=1,
            line_content="value = func()  # type: ignore[arg-type]",
            error_code="arg-type",
            has_reason=False,
            reason_text=None,
        )

        violation = validate_type_ignore(entry, require_reason=False)

        assert violation is None

    def test_complete_ignore_passes(self, temp_python_file: Path) -> None:
        """完整的 type: ignore 通过验证。"""
        entry = TypeIgnoreEntry(
            file=temp_python_file,
            line_number=1,
            line_content="value = func()  # type: ignore[arg-type]  # reason",
            error_code="arg-type",
            has_reason=True,
            reason_text="reason",
        )

        violation = validate_type_ignore(entry, require_reason=True)

        assert violation is None


# ============================================================================
# 违规记录测试
# ============================================================================


class TestTypeIgnoreViolation:
    """测试 TypeIgnoreViolation 数据类。"""

    def test_str_format_without_code(self, temp_python_file: Path) -> None:
        """字符串格式（无错误码）。"""
        violation = TypeIgnoreViolation(
            file=temp_python_file,
            line_number=10,
            line_content="value = func()  # type: ignore",
            violation_type="缺少错误码 [error-code]",
        )

        result = str(violation)

        assert str(temp_python_file) in result
        assert "10" in result
        assert "缺少错误码" in result

    def test_str_format_with_code(self, temp_python_file: Path) -> None:
        """字符串格式（有错误码）。"""
        violation = TypeIgnoreViolation(
            file=temp_python_file,
            line_number=10,
            line_content="value = func()  # type: ignore[arg-type]",
            violation_type="缺少原因说明",
            error_code="arg-type",
        )

        result = str(violation)

        assert "[arg-type]" in result
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
value1 = func1()  # type: ignore[arg-type]  # 第三方库类型不完整
value2 = func2()  # type: ignore[return-value]  # TODO: #123
"""
        temp_python_file.write_text(content, encoding="utf-8")

        entries = list(scan_file_for_type_ignores(temp_python_file))
        violations: List[TypeIgnoreViolation] = []

        for entry in entries:
            violation = validate_type_ignore(entry, require_reason=True)
            if violation:
                violations.append(violation)

        assert len(entries) == 2
        assert len(violations) == 0

    def test_scan_and_validate_non_compliant_file(self, temp_python_file: Path) -> None:
        """扫描并验证不合规文件。"""
        content = """
# Non-compliant file
value1 = func1()  # type: ignore
value2 = func2()  # type: ignore[arg-type]
value3 = func3()  # type: ignore[return-value]  # reason
"""
        temp_python_file.write_text(content, encoding="utf-8")

        entries = list(scan_file_for_type_ignores(temp_python_file))
        violations: List[TypeIgnoreViolation] = []

        for entry in entries:
            violation = validate_type_ignore(entry, require_reason=True)
            if violation:
                violations.append(violation)

        assert len(entries) == 3
        assert len(violations) == 2  # 2 违规（裸 ignore + 无原因）

    def test_project_strict_island_paths_exist(self) -> None:
        """项目 strict island 路径应存在。"""
        paths = load_strict_island_paths(PYPROJECT_PATH)
        files = expand_paths(paths, PROJECT_ROOT)

        # 至少应该有一些文件
        assert len(files) > 0, "strict island 路径下应有 Python 文件"

        # 所有文件应存在
        for f in files:
            assert f.exists(), f"文件应存在: {f}"
