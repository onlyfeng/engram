#!/usr/bin/env python3
"""
check_no_step_flow_numbers.py 单元测试

覆盖场景：
1. 带空格的流程编号（Step 1/2/3）应被检测到
2. 不带空格的别名（StepN）、单词（stepwise）、中文（步骤 1）等不应命中
3. 白名单路径不会被扫描
4. 排除规则正确工作
5. CLI 参数 --fail/--no-fail/--json 行为一致

注意：
- 所有测试使用 pytest tmp_path，不依赖真实仓库扫描
- 使用运行时拼接字符串构造测试数据，避免在源码中出现旧 token
"""

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

# 将 scripts 目录添加到 path
sys.path.insert(0, str(Path(__file__).parent.parent))

from check_no_step_flow_numbers import (
    ALLOWED_PATHS,
    STEP_FLOW_PATTERN,
    Finding,
    ScanResult,
    is_allowed_path,
    main,
    scan_directory,
    scan_file,
    should_exclude_dir,
    should_exclude_file,
    should_scan_file,
)

# ============================================================================
# 辅助函数：运行时构造测试 token（避免源码中出现旧 token）
# ============================================================================

def _make_step_flow(num: int, case: str = "title", sep: str = " ") -> str:
    """
    运行时构造带空格的流程编号 token

    Args:
        num: 阶段编号 (1, 2, 3)
        case: "lower", "title", "upper"
        sep: 分隔符（默认单个空格）

    Returns:
        构造的 token，如 "Step 1", "step 2", "STEP 3"
    """
    base = "Step"
    if case == "lower":
        base = "step"
    elif case == "upper":
        base = "STEP"
    return base + sep + str(num)


def _make_alias(num: int, case: str = "lower") -> str:
    """
    运行时构造无空格别名 token（不应被本脚本命中）

    Args:
        num: 阶段编号 (1, 2, 3)
        case: "lower", "title", "upper"

    Returns:
        构造的 token，如 "step1", "Step2", "STEP3"
    """
    base = "step"
    if case == "title":
        base = "Step"
    elif case == "upper":
        base = "STEP"
    return f"{base}{num}"


# ============================================================================
# Test: STEP_FLOW_PATTERN 正则匹配 - 应命中场景
# ============================================================================

class TestStepFlowPatternMatches:
    """测试流程编号正则匹配 - 应命中的场景"""

    def test_pattern_matches_title_case_space(self):
        """应该匹配 Step + 空格 + 数字"""
        for num in [1, 2, 3]:
            token = _make_step_flow(num, "title")
            match = STEP_FLOW_PATTERN.search(token)
            assert match is not None, f"Should match '{token}'"
            assert match.group().lower() == f"step {num}"

    def test_pattern_matches_lower_case_space(self):
        """应该匹配 step + 空格 + 数字"""
        for num in [1, 2, 3]:
            token = _make_step_flow(num, "lower")
            match = STEP_FLOW_PATTERN.search(token)
            assert match is not None, f"Should match '{token}'"
            assert match.group().lower() == f"step {num}"

    def test_pattern_matches_upper_case_space(self):
        """应该匹配 STEP + 空格 + 数字"""
        for num in [1, 2, 3]:
            token = _make_step_flow(num, "upper")
            match = STEP_FLOW_PATTERN.search(token)
            assert match is not None, f"Should match '{token}'"
            assert match.group().lower() == f"step {num}"

    def test_pattern_matches_mixed_case(self):
        """应该匹配混合大小写"""
        mixed_cases = [
            "sTeP" + " " + "1",
            "StEp" + " " + "2",
            "sTEP" + " " + "3",
        ]
        for token in mixed_cases:
            match = STEP_FLOW_PATTERN.search(token)
            assert match is not None, f"Should match '{token}'"

    def test_pattern_matches_in_sentence(self):
        """应该匹配句子中的 token"""
        token = _make_step_flow(1, "title")
        text = f"This is {token}: Initialize the system"
        match = STEP_FLOW_PATTERN.search(text)
        assert match is not None
        assert match.group().lower() == "step 1"

    def test_pattern_matches_with_punctuation(self):
        """应该匹配带标点符号的 token"""
        token = _make_step_flow(2, "title")
        texts = [
            f"{token}.",
            f"{token},",
            f"({token})",
            f"'{token}'",
            f"{token}:",
            f"{token};",
        ]
        for text in texts:
            match = STEP_FLOW_PATTERN.search(text)
            assert match is not None, f"Should match in '{text}'"
            assert match.group().lower() == "step 2"

    def test_pattern_matches_at_line_start(self):
        """应该匹配行首的 token"""
        token = _make_step_flow(1, "title")
        text = f"{token} - Introduction"
        match = STEP_FLOW_PATTERN.search(text)
        assert match is not None
        assert match.start() == 0

    def test_pattern_matches_at_line_end(self):
        """应该匹配行尾的 token"""
        token = _make_step_flow(3, "title")
        text = f"Final phase is {token}"
        match = STEP_FLOW_PATTERN.search(text)
        assert match is not None

    def test_pattern_matches_multiple_spaces(self):
        """应该匹配多个空格的情况"""
        # Step  1（双空格）
        token = _make_step_flow(1, "title", sep="  ")
        match = STEP_FLOW_PATTERN.search(token)
        assert match is not None, f"Should match '{token}' (double space)"

    def test_pattern_matches_tab_separator(self):
        """应该匹配 Tab 分隔的情况"""
        token = _make_step_flow(1, "title", sep="\t")
        match = STEP_FLOW_PATTERN.search(token)
        assert match is not None, f"Should match '{token}' (tab separator)"


# ============================================================================
# Test: STEP_FLOW_PATTERN 正则匹配 - 不应命中场景
# ============================================================================

class TestStepFlowPatternNotMatches:
    """测试流程编号正则匹配 - 不应命中的场景"""

    def test_pattern_not_matches_no_space(self):
        """不应该匹配无空格的别名（StepN）"""
        for num in [1, 2, 3]:
            for case in ["lower", "title", "upper"]:
                token = _make_alias(num, case)
                match = STEP_FLOW_PATTERN.search(token)
                assert match is None, f"Should NOT match '{token}'"

    def test_pattern_not_matches_stepwise(self):
        """不应该匹配 stepwise 等单词"""
        words = ["stepwise", "Stepwise", "STEPWISE", "stepwisely"]
        for word in words:
            match = STEP_FLOW_PATTERN.search(word)
            assert match is None, f"Should NOT match '{word}'"

    def test_pattern_not_matches_chinese(self):
        """不应该匹配中文步骤描述"""
        chinese_texts = [
            "步骤 1",
            "步骤1",
            "第一步",
            "阶段 1",
            "步骤 2",
            "步骤 3",
        ]
        for text in chinese_texts:
            match = STEP_FLOW_PATTERN.search(text)
            assert match is None, f"Should NOT match '{text}'"

    def test_pattern_not_matches_numbered_list(self):
        """不应该匹配数字序号列表（如 1. 2. 3.）"""
        numbered_items = ["1.", "2.", "3.", "1)", "2)", "3)"]
        for item in numbered_items:
            match = STEP_FLOW_PATTERN.search(item)
            assert match is None, f"Should NOT match '{item}'"

    def test_pattern_not_matches_other_numbers(self):
        """不应该匹配其他数字（0, 4, 5 等）"""
        other_nums = [0, 4, 5, 10, 123]
        for num in other_nums:
            token = "Step" + " " + str(num)
            match = STEP_FLOW_PATTERN.search(token)
            assert match is None, f"Should NOT match '{token}'"

    def test_pattern_not_matches_substring(self):
        """不应该在更长单词中匹配"""
        # footstep 中的 step 不应匹配
        words = ["footstep", "doorstep", "misstep", "sidestep"]
        for word in words:
            # 即使后面加空格和数字也不应匹配，因为 step 前有字母
            text = f"{word} 1"
            match = STEP_FLOW_PATTERN.search(text)
            assert match is None, f"Should NOT match '{text}'"

    def test_pattern_not_matches_step_n_pattern(self):
        """不应该匹配 StepN 模式（无空格）"""
        # 这些应由 check_no_legacy_stage_aliases.py 检测
        patterns = [
            "step" + "1",
            "Step" + "2",
            "STEP" + "3",
            "step" + "1" + "_logbook",
            "_" + "step" + "2" + "_",
        ]
        for pattern in patterns:
            match = STEP_FLOW_PATTERN.search(pattern)
            assert match is None, f"Should NOT match '{pattern}'"

    def test_pattern_not_matches_step_without_number(self):
        """不应该匹配不带数字的 step"""
        texts = ["step", "Step", "STEP", "step forward", "next step"]
        for text in texts:
            match = STEP_FLOW_PATTERN.search(text)
            assert match is None, f"Should NOT match '{text}'"


# ============================================================================
# Test: scan_file 单文件扫描
# ============================================================================

class TestScanFile:
    """测试单文件扫描功能"""

    def test_scan_file_detects_flow_number(self, tmp_path: Path):
        """扫描文件应检测到流程编号"""
        test_file = tmp_path / "test.py"
        token1 = _make_step_flow(1, "title")
        token2 = _make_step_flow(2, "lower")
        test_file.write_text(f"# {token1}: Initialize\n# {token2}: Configure\n")

        findings = scan_file(test_file, tmp_path)

        assert len(findings) == 2
        assert findings[0].match.lower() == "step 1"
        assert findings[1].match.lower() == "step 2"

    def test_scan_file_ignores_no_space_aliases(self, tmp_path: Path):
        """扫描文件应忽略无空格的别名"""
        test_file = tmp_path / "test.py"
        alias1 = _make_alias(1, "lower")
        alias2 = _make_alias(2, "title")
        test_file.write_text(f"import {alias1}_logbook\nfrom {alias2}_module import X\n")

        findings = scan_file(test_file, tmp_path)

        assert len(findings) == 0

    def test_scan_file_mixed_content(self, tmp_path: Path):
        """扫描包含混合内容的文件"""
        test_file = tmp_path / "test.py"
        flow_num = _make_step_flow(1, "title")
        alias = _make_alias(1, "lower")
        content = f"""# {flow_num}: Introduction (should match - has space)
# 步骤 1: 介绍 (should NOT match - Chinese)
import {alias}_module  # should NOT match - no space (legacy alias)
# stepwise approach  # should NOT match - different word
"""
        test_file.write_text(content)

        findings = scan_file(test_file, tmp_path)

        # 只应匹配第 1 行的带空格流程编号
        assert len(findings) == 1
        assert findings[0].line == 1
        assert findings[0].match.lower() == "step 1"

    def test_scan_file_multiple_on_same_line(self, tmp_path: Path):
        """扫描同一行有多个匹配的情况"""
        test_file = tmp_path / "test.py"
        token1 = _make_step_flow(1, "title")
        token2 = _make_step_flow(2, "title")
        test_file.write_text(f"# {token1} and {token2}\n")

        findings = scan_file(test_file, tmp_path)

        assert len(findings) == 2
        assert findings[0].match.lower() == "step 1"
        assert findings[1].match.lower() == "step 2"


# ============================================================================
# Test: scan_directory 目录扫描
# ============================================================================

class TestScanDirectory:
    """测试目录扫描功能"""

    def test_scan_directory_finds_violations(self, tmp_path: Path):
        """扫描目录应找到违规"""
        flow1 = _make_step_flow(1, "title")
        flow2 = _make_step_flow(2, "upper")

        py_file = tmp_path / "test.py"
        py_file.write_text(f"# {flow1}: Initialize\n")

        md_file = tmp_path / "README.md"
        md_file.write_text(f"# {flow2} Guide\n")

        result = scan_directory(tmp_path)

        assert result.files_scanned >= 2
        assert len(result.findings) == 2

    def test_scan_directory_excludes_dirs(self, tmp_path: Path):
        """扫描目录应排除特定目录"""
        flow_num = _make_step_flow(1, "title")

        # 创建 __pycache__ 目录（应被排除）
        cache_dir = tmp_path / "__pycache__"
        cache_dir.mkdir()
        cache_file = cache_dir / "test.py"
        cache_file.write_text(f"# {flow_num}: Cache\n")

        # 创建正常目录
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        flow2 = _make_step_flow(2, "title")
        src_file = src_dir / "main.py"
        src_file.write_text(f"# {flow2}: Main\n")

        result = scan_directory(tmp_path)

        # 只应找到 src/main.py 中的违规
        assert len(result.findings) == 1
        assert result.findings[0].file == "src/main.py"

    def test_scan_directory_excludes_libs(self, tmp_path: Path):
        """扫描目录应排除 libs 目录（上游依赖）"""
        flow_num = _make_step_flow(1, "title")

        # 创建 libs 目录（应被排除）
        libs_dir = tmp_path / "libs"
        libs_dir.mkdir()
        lib_file = libs_dir / "upstream.py"
        lib_file.write_text(f"# {flow_num}: Upstream code\n")

        result = scan_directory(tmp_path)

        # libs 目录应被排除，不应有发现
        assert len(result.findings) == 0

    def test_scan_directory_respects_file_extensions(self, tmp_path: Path):
        """扫描目录应只扫描指定扩展名的文件"""
        flow_num = _make_step_flow(1, "title")

        # 创建 .py 文件（应被扫描）
        py_file = tmp_path / "test.py"
        py_file.write_text(f"# {flow_num}: Python\n")

        # 创建 .txt 文件（不应被扫描，不在 SCAN_EXTENSIONS 中）
        txt_file = tmp_path / "test.txt"
        txt_file.write_text(f"# {flow_num}: Text\n")

        result = scan_directory(tmp_path)

        # 只应找到 .py 文件中的违规
        assert len(result.findings) == 1
        assert result.findings[0].file == "test.py"


# ============================================================================
# Test: 白名单路径
# ============================================================================

class TestAllowedPaths:
    """测试白名单路径功能"""

    def test_allowed_path_exact_match(self):
        """精确匹配的白名单路径"""
        # 检查脚本自身
        assert is_allowed_path("scripts/check_no_step_flow_numbers.py") is True
        # 互补脚本
        assert is_allowed_path("scripts/check_no_legacy_stage_aliases.py") is True
        # 测试文件
        assert is_allowed_path("scripts/tests/test_legacy_alias_checks.py") is True
        assert is_allowed_path("scripts/tests/test_step_flow_checks.py") is True
        # 架构文档
        assert is_allowed_path("docs/architecture/naming.md") is True

    def test_allowed_path_prefix_match(self):
        """前缀匹配的白名单路径（目录）"""
        # .git/ 是目录前缀
        assert is_allowed_path(".git/objects/abc") is True
        assert is_allowed_path(".git/config") is True

    def test_not_allowed_path(self):
        """非白名单路径"""
        assert is_allowed_path("src/main.py") is False
        assert is_allowed_path("scripts/other.py") is False
        assert is_allowed_path("docs/README.md") is False
        # docs/architecture/ 下其他文件不在白名单中
        assert is_allowed_path("docs/architecture/README.md") is False

    def test_allowed_paths_matches_source_definition(self):
        """验证测试覆盖了 ALLOWED_PATHS 中的所有路径"""
        # 验证所有精确匹配路径
        exact_paths = [p for p in ALLOWED_PATHS if not p.endswith("/")]
        for path in exact_paths:
            assert is_allowed_path(path) is True, f"Expected {path} to be allowed"

        # 验证所有目录前缀路径
        dir_prefixes = [p for p in ALLOWED_PATHS if p.endswith("/")]
        for prefix in dir_prefixes:
            test_path = prefix + "some/nested/file.txt"
            assert is_allowed_path(test_path) is True, f"Expected {test_path} to be allowed"

    def test_scan_skips_allowed_path(self, tmp_path: Path):
        """扫描时应跳过白名单中的文件"""
        # 模拟白名单路径
        flow_num = _make_step_flow(1, "title")

        # 创建测试文件
        test_file = tmp_path / "test.py"
        test_file.write_text(f"# {flow_num}: Test\n")

        # 使用 mock 将该文件路径加入白名单
        with mock.patch(
            "check_no_step_flow_numbers.is_allowed_path",
            side_effect=lambda p: p == "test.py"
        ):
            findings = scan_file(test_file, tmp_path)

        # 白名单文件不应有发现
        assert len(findings) == 0


# ============================================================================
# Test: Finding 类
# ============================================================================

class TestFinding:
    """测试 Finding 类"""

    def test_finding_to_dict(self):
        """Finding 序列化"""
        flow_num = _make_step_flow(2, "title")
        f = Finding(
            file="test.py",
            line=5,
            column=3,
            match=flow_num,
            context=f"{flow_num}: context"
        )

        d = f.to_dict()
        assert d["file"] == "test.py"
        assert d["line"] == 5
        assert d["column"] == 3
        assert d["match"] == flow_num

    def test_finding_to_ci_format(self):
        """Finding CI 格式输出"""
        flow_num = _make_step_flow(1, "title")
        f = Finding(file="test.py", line=10, column=5, match=flow_num)

        ci_output = f.to_ci_format()
        assert "test.py:10:5:" in ci_output
        assert flow_num in ci_output


# ============================================================================
# Test: 辅助函数
# ============================================================================

class TestHelperFunctions:
    """测试辅助函数"""

    @pytest.mark.parametrize("dir_name,expected", [
        ("__pycache__", True),
        (".git", True),
        ("node_modules", True),
        (".venv", True),
        ("venv", True),
        ("dist", True),
        ("build", True),
        ("libs", True),     # 上游依赖目录
        ("patches", True),  # 补丁目录
        ("src", False),
        ("scripts", False),
    ])
    def test_should_exclude_dir(self, dir_name: str, expected: bool):
        """测试目录排除规则"""
        assert should_exclude_dir(dir_name) == expected

    @pytest.mark.parametrize("file_name,expected", [
        ("package-lock.json", True),
        ("poetry.lock", True),
        ("test.min.js", True),
        ("style.min.css", True),
        ("file.pyc", True),
        ("image.png", True),
        ("test.py", False),
        ("README.md", False),
        ("config.json", False),
    ])
    def test_should_exclude_file(self, file_name: str, expected: bool):
        """测试文件排除规则"""
        assert should_exclude_file(file_name) == expected

    @pytest.mark.parametrize("file_path,expected", [
        (Path("test.py"), True),
        (Path("script.sh"), True),
        (Path("README.md"), True),
        (Path("config.yml"), True),
        (Path("config.yaml"), True),
        (Path("data.json"), True),
        (Path("schema.sql"), True),
        (Path("pyproject.toml"), True),
        (Path("Makefile"), True),
        (Path("test.txt"), False),
        (Path("image.png"), False),
        (Path("file.exe"), False),
    ])
    def test_should_scan_file(self, file_path: Path, expected: bool):
        """测试文件扫描规则"""
        assert should_scan_file(file_path) == expected


# ============================================================================
# Test: ScanResult 类
# ============================================================================

class TestScanResult:
    """测试 ScanResult 类"""

    def test_scan_result_default_values(self):
        """ScanResult 默认值"""
        result = ScanResult()
        assert result.findings == []
        assert result.files_scanned == 0
        assert result.files_skipped == 0


# ============================================================================
# Test: CLI 参数
# ============================================================================

class TestCLIArguments:
    """测试 CLI 参数解析和行为"""

    def test_fail_mode_default(self, tmp_path: Path):
        """默认模式（--fail）：发现问题时退出码为 1"""
        test_file = tmp_path / "test.py"
        flow_num = _make_step_flow(1, "title")
        test_file.write_text(f"# {flow_num}: Test\n")

        with mock.patch("sys.argv", ["prog", "--root", str(tmp_path)]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            # 发现问题时退出码为 1
            assert exc_info.value.code == 1

    def test_fail_mode_explicit(self, tmp_path: Path):
        """显式 --fail 模式：发现问题时退出码为 1"""
        test_file = tmp_path / "test.py"
        flow_num = _make_step_flow(2, "title")
        test_file.write_text(f"# {flow_num}: Test\n")

        with mock.patch("sys.argv", ["prog", "--fail", "--root", str(tmp_path)]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def test_no_fail_mode(self, tmp_path: Path):
        """--no-fail 模式：发现问题时退出码为 0"""
        test_file = tmp_path / "test.py"
        flow_num = _make_step_flow(3, "title")
        test_file.write_text(f"# {flow_num}: Test\n")

        with mock.patch("sys.argv", ["prog", "--no-fail", "--root", str(tmp_path)]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            # --no-fail 模式下退出码为 0
            assert exc_info.value.code == 0

    def test_no_fail_overrides_fail(self, tmp_path: Path):
        """--no-fail 应该覆盖 --fail"""
        test_file = tmp_path / "test.py"
        flow_num = _make_step_flow(1, "upper")
        test_file.write_text(f"# {flow_num}: Test\n")

        # 同时指定 --fail 和 --no-fail，--no-fail 应该生效
        with mock.patch("sys.argv", ["prog", "--fail", "--no-fail", "--root", str(tmp_path)]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0

    def test_no_issues_exit_zero(self, tmp_path: Path):
        """无问题时，任何模式下退出码都为 0"""
        test_file = tmp_path / "test.py"
        # 使用无空格别名（不应被本脚本检测）
        alias = _make_alias(1, "lower")
        test_file.write_text(f"import {alias}_module\n# Clean code\n")

        # 默认模式
        with mock.patch("sys.argv", ["prog", "--root", str(tmp_path)]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0

    def test_no_issues_exit_zero_with_fail(self, tmp_path: Path):
        """无问题时，--fail 模式下退出码也为 0"""
        test_file = tmp_path / "test.py"
        test_file.write_text("# Clean code without any step flow numbers\n")

        with mock.patch("sys.argv", ["prog", "--fail", "--root", str(tmp_path)]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0

    def test_json_output_mode(self, tmp_path: Path, capsys):
        """--json 模式应输出 JSON 格式"""
        test_file = tmp_path / "test.py"
        flow_num = _make_step_flow(1, "title")
        test_file.write_text(f"# {flow_num}: Test\n")

        with mock.patch("sys.argv", ["prog", "--json", "--no-fail", "--root", str(tmp_path)]):
            with pytest.raises(SystemExit):
                main()

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert "status" in output
        assert "findings" in output
        assert len(output["findings"]) == 1
        assert output["findings"][0]["match"].lower() == "step 1"

    def test_json_output_status_ok(self, tmp_path: Path, capsys):
        """--json 模式无问题时 status 为 ok"""
        test_file = tmp_path / "test.py"
        test_file.write_text("# Clean code\n")

        with mock.patch("sys.argv", ["prog", "--json", "--root", str(tmp_path)]):
            with pytest.raises(SystemExit):
                main()

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["status"] == "ok"
        assert output["errors"] == 0

    def test_json_output_status_error_fail_mode(self, tmp_path: Path, capsys):
        """--json --fail 模式有问题时 status 为 error"""
        test_file = tmp_path / "test.py"
        flow_num = _make_step_flow(1, "title")
        test_file.write_text(f"# {flow_num}: Test\n")

        with mock.patch("sys.argv", ["prog", "--json", "--fail", "--root", str(tmp_path)]):
            with pytest.raises(SystemExit):
                main()

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["status"] == "error"

    def test_json_output_status_warning_no_fail_mode(self, tmp_path: Path, capsys):
        """--json --no-fail 模式有问题时 status 为 warning"""
        test_file = tmp_path / "test.py"
        flow_num = _make_step_flow(1, "title")
        test_file.write_text(f"# {flow_num}: Test\n")

        with mock.patch("sys.argv", ["prog", "--json", "--no-fail", "--root", str(tmp_path)]):
            with pytest.raises(SystemExit):
                main()

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["status"] == "warning"

    def test_verbose_mode(self, tmp_path: Path, capsys):
        """--verbose 模式应输出详细信息"""
        test_file = tmp_path / "test.py"
        flow_num = _make_step_flow(2, "title")
        test_file.write_text(f"# {flow_num}: Configuration phase\n")

        with mock.patch("sys.argv", ["prog", "--verbose", "--no-fail", "--root", str(tmp_path)]):
            with pytest.raises(SystemExit):
                main()

        captured = capsys.readouterr()
        # verbose 模式应包含上下文行
        assert flow_num in captured.out or "Configuration" in captured.out


# ============================================================================
# Test: 边界场景
# ============================================================================

class TestEdgeCases:
    """测试边界场景"""

    def test_empty_file(self, tmp_path: Path):
        """空文件不应有发现"""
        test_file = tmp_path / "empty.py"
        test_file.write_text("")

        findings = scan_file(test_file, tmp_path)
        assert len(findings) == 0

    def test_binary_like_content(self, tmp_path: Path):
        """包含特殊字符的文件应正常处理"""
        test_file = tmp_path / "test.py"
        flow_num = _make_step_flow(1, "title")
        # 包含一些特殊字符
        test_file.write_text(f"# {flow_num}\n# \x00\x01\x02\n")

        findings = scan_file(test_file, tmp_path)
        assert len(findings) == 1

    def test_unicode_content(self, tmp_path: Path):
        """Unicode 内容应正常处理"""
        test_file = tmp_path / "test.py"
        flow_num = _make_step_flow(1, "title")
        test_file.write_text(f"# {flow_num}: 初始化 🚀\n# 步骤 1: 中文不匹配\n")

        findings = scan_file(test_file, tmp_path)
        # 只应匹配英文的 Step 1
        assert len(findings) == 1
        assert findings[0].match.lower() == "step 1"

    def test_all_three_steps(self, tmp_path: Path):
        """应检测所有三个步骤编号"""
        test_file = tmp_path / "test.py"
        step1 = _make_step_flow(1, "title")
        step2 = _make_step_flow(2, "title")
        step3 = _make_step_flow(3, "title")
        test_file.write_text(f"# {step1}\n# {step2}\n# {step3}\n")

        findings = scan_file(test_file, tmp_path)
        assert len(findings) == 3
        matches = {f.match.lower() for f in findings}
        assert matches == {"step 1", "step 2", "step 3"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
