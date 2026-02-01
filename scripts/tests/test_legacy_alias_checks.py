#!/usr/bin/env python3
"""
check_no_legacy_stage_aliases.py 单元测试

覆盖场景：
1. 不带空格的阶段别名（大小写变体）会被检测到
2. 带空格的流程编号写法（如 "Step 1"）不应命中
3. 白名单路径不会被扫描
4. 排除规则正确工作
5. CLI 参数 --fail 与 --no-fail 行为一致

注意：
- 所有测试使用 pytest tmp_path，不依赖真实仓库扫描
- 使用运行时拼接字符串构造测试数据，避免在源码中出现旧 token
"""

import sys
from pathlib import Path
from unittest import mock

import pytest

# 将 scripts 目录添加到 path
sys.path.insert(0, str(Path(__file__).parent.parent))

from check_no_legacy_stage_aliases import (
    ALLOWED_PATHS,
    LEGACY_ALIAS_PATTERN,
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

def _make_alias(num: int, case: str = "lower") -> str:
    """
    运行时构造旧别名 token

    Args:
        num: 阶段编号 (1, 2, 3)
        case: "lower", "title", "upper"

    Returns:
        构造的 token，如 "stepN", "StepN", "STEPN"（N 为传入的 num）
    """
    base = "step"
    if case == "title":
        base = "Step"
    elif case == "upper":
        base = "STEP"
    return f"{base}{num}"


def _make_flow_num(num: int, case: str = "title") -> str:
    """
    运行时构造流程编号写法（带空格）

    Args:
        num: 编号 (1, 2, 3, ...)
        case: "lower", "title", "upper"

    Returns:
        构造的 token，如 "Step 1", "step 2", "STEP 3"
    """
    base = "Step"
    if case == "lower":
        base = "step"
    elif case == "upper":
        base = "STEP"
    return f"{base} {num}"


# ============================================================================
# Test: LEGACY_ALIAS_PATTERN 正则匹配
# ============================================================================

class TestLegacyAliasPattern:
    """测试旧别名正则匹配"""

    def test_pattern_matches_lower_case(self):
        """应该匹配小写 step + 数字"""
        for num in [1, 2, 3]:
            token = _make_alias(num, "lower")
            match = LEGACY_ALIAS_PATTERN.search(token)
            assert match is not None, f"Should match '{token}'"
            # 动态验证：match 结果应与 _make_alias 返回值一致
            expected = _make_alias(num, "lower")
            assert match.group().lower() == expected.lower()

    def test_pattern_matches_title_case(self):
        """应该匹配首字母大写 Step + 数字"""
        for num in [1, 2, 3]:
            token = _make_alias(num, "title")
            match = LEGACY_ALIAS_PATTERN.search(token)
            assert match is not None, f"Should match '{token}'"
            # 动态验证：忽略大小写比较
            expected = _make_alias(num, "title")
            assert match.group().lower() == expected.lower()

    def test_pattern_matches_upper_case(self):
        """应该匹配全大写 STEP + 数字"""
        for num in [1, 2, 3]:
            token = _make_alias(num, "upper")
            match = LEGACY_ALIAS_PATTERN.search(token)
            assert match is not None, f"Should match '{token}'"
            # 动态验证
            expected = _make_alias(num, "upper")
            assert match.group().lower() == expected.lower()

    def test_pattern_matches_mixed_case(self):
        """应该匹配混合大小写"""
        mixed_cases = ["sTeP1", "StEp2", "sTEP3"]
        for token in mixed_cases:
            match = LEGACY_ALIAS_PATTERN.search(token)
            assert match is not None, f"Should match '{token}'"
            # 动态验证：从 token 中提取数字构造期望值
            num = int(token[-1])
            expected = _make_alias(num, "lower")
            assert match.group().lower() == expected

    def test_pattern_matches_in_sentence(self):
        """应该匹配句子中的 token"""
        token = _make_alias(1, "lower")
        text = f"This is {token} function"
        match = LEGACY_ALIAS_PATTERN.search(text)
        assert match is not None
        assert match.group().lower() == token

    def test_pattern_matches_with_punctuation(self):
        """应该匹配带标点符号的 token"""
        token = _make_alias(2, "title")
        texts = [f"{token}.", f"{token},", f"({token})", f"'{token}'"]
        for text in texts:
            match = LEGACY_ALIAS_PATTERN.search(text)
            assert match is not None, f"Should match in '{text}'"
            assert match.group().lower() == token.lower()

    def test_pattern_matches_underscore_prefix(self):
        """应该匹配下划线前缀的 token（stepN_foo 场景）"""
        for num in [1, 2, 3]:
            token = _make_alias(num, "lower")
            # stepN_foo 格式
            text = f"{token}_logbook"
            match = LEGACY_ALIAS_PATTERN.search(text)
            assert match is not None, f"Should match in '{text}'"
            assert match.group().lower() == token

    def test_pattern_matches_underscore_surrounded(self):
        """应该匹配下划线包围的 token（foo_stepN_bar 场景）"""
        for num in [1, 2, 3]:
            token = _make_alias(num, "lower")
            # foo_stepN_bar 格式
            text = f"my_{token}_code"
            match = LEGACY_ALIAS_PATTERN.search(text)
            assert match is not None, f"Should match in '{text}'"
            assert match.group().lower() == token

    def test_pattern_matches_path_segment(self):
        """应该匹配路径片段中的 token（/stepN/ 场景）"""
        for num in [1, 2, 3]:
            token = _make_alias(num, "lower")
            # 路径片段
            paths = [
                f"/apps/{token}/scripts",
                f"apps/{token}_logbook/",
                f"/data/{token}-data/",
            ]
            for path in paths:
                match = LEGACY_ALIAS_PATTERN.search(path)
                assert match is not None, f"Should match in '{path}'"
                assert match.group().lower() == token

    def test_pattern_matches_hyphen_connected(self):
        """应该匹配连字符连接的 token"""
        for num in [1, 2, 3]:
            token = _make_alias(num, "lower")
            text = f"{token}-config"
            match = LEGACY_ALIAS_PATTERN.search(text)
            assert match is not None, f"Should match in '{text}'"
            assert match.group().lower() == token

    def test_pattern_not_matches_with_space(self):
        """不应该匹配带空格的流程编号（Step 1、step 2 等）"""
        for num in [1, 2, 3]:
            for case in ["lower", "title", "upper"]:
                token = _make_flow_num(num, case)
                match = LEGACY_ALIAS_PATTERN.search(token)
                assert match is None, f"Should NOT match '{token}'"

    def test_pattern_not_matches_with_space_various_formats(self):
        """不应该匹配各种带空格的流程编号格式"""
        # 各种带空格的写法都不应命中
        # 使用运行时拼接避免字面量
        space_formats = [
            _make_flow_num(1, "title"), _make_flow_num(1, "lower"), _make_flow_num(1, "upper"),  # 标准格式
            _make_flow_num(2, "title"), _make_flow_num(2, "lower"), _make_flow_num(2, "upper"),
            _make_flow_num(3, "title"), _make_flow_num(3, "lower"), _make_flow_num(3, "upper"),
            "Step" + "  " + "1",  # 双空格（运行时拼接）
            "Step" + "\t" + "1",  # Tab（运行时拼接）
        ]
        for text in space_formats:
            match = LEGACY_ALIAS_PATTERN.search(text)
            assert match is None, f"Should NOT match '{text}'"

    def test_pattern_not_matches_chinese(self):
        """不应该匹配中文步骤描述"""
        chinese_texts = ["步骤 1", "步骤1", "第一步"]
        for text in chinese_texts:
            match = LEGACY_ALIAS_PATTERN.search(text)
            assert match is None, f"Should NOT match '{text}'"

    def test_pattern_not_matches_other_numbers(self):
        """不应该匹配其他数字（0, 4, 5 等）"""
        other_nums = [0, 4, 5, 10, 123]
        for num in other_nums:
            token = f"step{num}"
            match = LEGACY_ALIAS_PATTERN.search(token)
            assert match is None, f"Should NOT match '{token}'"

    def test_pattern_not_matches_similar_words(self):
        """不应该匹配类似单词（前面有字母）"""
        # 这些词中 step 前面有字母，不满足 (?<![a-zA-Z]) 条件
        # 运行时拼接避免源码出现旧 token
        similar = [
            f"foot{_make_alias(1, 'lower')}",  # footstepN
            f"my{_make_alias(2, 'lower')}",    # mystepN
            f"pre{_make_alias(3, 'lower')}",   # prestepN
        ]
        for word in similar:
            match = LEGACY_ALIAS_PATTERN.search(word)
            assert match is None, f"Should NOT match '{word}'"

    def test_pattern_not_matches_followed_by_alphanumeric(self):
        """不应该匹配后面紧跟字母数字的情况"""
        # stepN 后面紧跟字母或数字时不应匹配
        # 运行时拼接避免源码出现旧 token
        no_match_cases = [
            f"{_make_alias(1, 'lower')}a",   # stepNa - 后面是字母
            f"{_make_alias(1, 'lower')}2",   # stepN2 - 后面是数字
            f"{_make_alias(1, 'lower')}st",  # stepNst - 后面是字母
            f"{_make_alias(2, 'upper')}nd",  # STEPNnd - 后面是字母
        ]
        for text in no_match_cases:
            match = LEGACY_ALIAS_PATTERN.search(text)
            assert match is None, f"Should NOT match '{text}'"


# ============================================================================
# Test: scan_file 单文件扫描
# ============================================================================

class TestScanFile:
    """测试单文件扫描功能"""

    def test_scan_file_detects_alias(self, tmp_path: Path):
        """扫描文件应检测到旧别名"""
        test_file = tmp_path / "test.py"
        token1 = _make_alias(1, "lower")
        token2 = _make_alias(2, "title")
        # 使用空格/标点分隔确保 word boundary
        test_file.write_text(f"# This is {token1} code\n# Deploy {token2}.\n")

        findings = scan_file(test_file, tmp_path)

        assert len(findings) == 2
        # 动态验证：使用 _make_alias 构造期望值
        assert findings[0].match.lower() == _make_alias(1, "lower")
        assert findings[1].match.lower() == _make_alias(2, "lower")

    def test_scan_file_detects_underscore_patterns(self, tmp_path: Path):
        """扫描文件应检测下划线连接的 stepN 模式"""
        test_file = tmp_path / "test.py"
        token1 = _make_alias(1, "lower")
        token2 = _make_alias(2, "lower")
        # stepN_foo 和 foo_stepN_bar 格式都应命中
        content = f"""import {token1}_logbook  # should match - underscore suffix
from my_{token2}_module import X  # should match - underscore surrounded
"""
        test_file.write_text(content)

        findings = scan_file(test_file, tmp_path)

        assert len(findings) == 2
        assert findings[0].match.lower() == token1
        assert findings[1].match.lower() == token2

    def test_scan_file_detects_path_segments(self, tmp_path: Path):
        """扫描文件应检测路径片段中的 stepN"""
        test_file = tmp_path / "test.py"
        token = _make_alias(1, "lower")
        content = f"""# Path: apps/{token}_logbook/scripts/main.py
"""
        test_file.write_text(content)

        findings = scan_file(test_file, tmp_path)

        assert len(findings) == 1
        assert findings[0].match.lower() == token

    def test_scan_file_ignores_flow_numbers(self, tmp_path: Path):
        """扫描文件应忽略带空格的流程编号"""
        test_file = tmp_path / "test.py"
        flow1 = _make_flow_num(1, "title")
        flow2 = _make_flow_num(2, "lower")
        flow3 = _make_flow_num(3, "upper")
        test_file.write_text(f"# {flow1}: Initialize\n# {flow2}: Configure\n# {flow3}: Deploy\n")

        findings = scan_file(test_file, tmp_path)

        assert len(findings) == 0

    def test_scan_file_mixed_content(self, tmp_path: Path):
        """扫描包含混合内容的文件"""
        test_file = tmp_path / "test.py"
        alias = _make_alias(1, "lower")
        flow_num = _make_flow_num(1, "title")
        # 带空格的流程编号不匹配，下划线连接的会匹配
        content = f"""# {flow_num}: Introduction (should NOT match - has space)
# 步骤 1: 介绍 (should NOT match - Chinese)
import {alias}_module  # should match - underscore pattern
    # Run {alias} here  # should match - space boundary
"""
        test_file.write_text(content)

        findings = scan_file(test_file, tmp_path)

        # 应匹配第 3 行和第 4 行
        assert len(findings) == 2
        assert findings[0].line == 3
        assert findings[1].line == 4
        for f in findings:
            assert f.match.lower() == alias


# ============================================================================
# Test: scan_directory 目录扫描
# ============================================================================

class TestScanDirectory:
    """测试目录扫描功能"""

    def test_scan_directory_finds_violations(self, tmp_path: Path):
        """扫描目录应找到违规"""
        alias1 = _make_alias(1, "lower")
        alias2 = _make_alias(2, "title")

        py_file = tmp_path / "test.py"
        py_file.write_text(f"# Run {alias1} here\n")

        md_file = tmp_path / "README.md"
        md_file.write_text(f"# {alias2} Guide\n")

        result = scan_directory(tmp_path)

        assert result.files_scanned >= 2
        assert len(result.findings) == 2

    def test_scan_directory_excludes_dirs(self, tmp_path: Path):
        """扫描目录应排除特定目录"""
        alias = _make_alias(1, "lower")

        # 创建 __pycache__ 目录（应被排除）
        cache_dir = tmp_path / "__pycache__"
        cache_dir.mkdir()
        cache_file = cache_dir / "test.py"
        cache_file.write_text(f"# Use {alias} here\n")

        # 创建正常目录
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        alias2 = _make_alias(2, "lower")
        src_file = src_dir / "main.py"
        src_file.write_text(f"# Deploy {alias2} now\n")

        result = scan_directory(tmp_path)

        # 只应找到 src/main.py 中的违规
        assert len(result.findings) == 1
        assert result.findings[0].file == "src/main.py"

    def test_scan_directory_respects_file_extensions(self, tmp_path: Path):
        """扫描目录应只扫描指定扩展名的文件"""
        alias = _make_alias(1, "lower")

        # 创建 .py 文件（应被扫描）
        py_file = tmp_path / "test.py"
        py_file.write_text(f"# Run {alias} here\n")

        # 创建 .txt 文件（不应被扫描，不在 SCAN_EXTENSIONS 中）
        txt_file = tmp_path / "test.txt"
        txt_file.write_text(f"# Run {alias} here\n")

        result = scan_directory(tmp_path)

        # 只应找到 .py 文件中的违规
        assert len(result.findings) == 1
        assert result.findings[0].file == "test.py"

    def test_scan_directory_scans_makefile(self, tmp_path: Path):
        """扫描目录应扫描 Makefile（无扩展名特殊文件）"""
        alias = _make_alias(1, "lower")
        makefile = tmp_path / "Makefile"
        makefile.write_text(f"target:\n\techo {alias}\n")

        result = scan_directory(tmp_path)

        assert any(f.file == "Makefile" for f in result.findings)


# ============================================================================
# Test: 白名单路径
# ============================================================================

class TestAllowedPaths:
    """测试白名单路径功能"""

    def test_allowed_path_exact_match(self):
        """精确匹配的白名单路径"""
        # 检查脚本自身
        assert is_allowed_path("scripts/check_no_legacy_stage_aliases.py") is True
        # 互补检查脚本（文档中引用 stepN 示例）
        assert is_allowed_path("scripts/check_no_step_flow_numbers.py") is True
        # 测试文件自身
        assert is_allowed_path("scripts/tests/test_legacy_alias_checks.py") is True
        # 架构文档（包含禁止词示例代码块）
        assert is_allowed_path("docs/architecture/naming.md") is True
        # ADR 文档（解释命名约束）
        assert is_allowed_path("docs/architecture/adr_step_flow_wording.md") is True
        # 旧命名治理文档
        assert is_allowed_path("docs/architecture/legacy_naming_governance.md") is True

    def test_allowed_path_prefix_match(self):
        """前缀匹配的白名单路径（目录）"""
        # .git/ 是目录前缀
        assert is_allowed_path(".git/objects/abc") is True
        assert is_allowed_path(".git/config") is True

    def test_not_allowed_path(self):
        """非白名单路径"""
        assert is_allowed_path("src/main.py") is False
        assert is_allowed_path("scripts/other.py") is False
        # docs/legacy/ 不在白名单中
        assert is_allowed_path("docs/legacy/guide.md") is False
        # docs/architecture/ 下其他文件不在白名单中（仅特定文档被白名单）
        assert is_allowed_path("docs/architecture/README.md") is False
        # CI workflow contract 不在白名单中（已清理完毕）
        assert is_allowed_path("scripts/ci/workflow_contract.v1.json") is False

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


# ============================================================================
# Test: Finding 类
# ============================================================================

class TestFinding:
    """测试 Finding 类"""

    def test_finding_to_dict(self):
        """Finding 序列化"""
        alias = _make_alias(2, "lower")
        f = Finding(file="test.py", line=5, column=3, match=alias, context=f"{alias}_context")

        d = f.to_dict()
        assert d["file"] == "test.py"
        assert d["line"] == 5
        assert d["column"] == 3
        assert d["match"] == alias


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
        ("src", False),
        ("lib", False),
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
        # 创建包含违规的文件
        test_file = tmp_path / "test.py"
        alias = _make_alias(1, "lower")
        test_file.write_text(f"# Use {alias} here\n")

        with mock.patch("sys.argv", ["prog", "--root", str(tmp_path)]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            # 发现问题时退出码为 1
            assert exc_info.value.code == 1

    def test_fail_mode_explicit(self, tmp_path: Path):
        """显式 --fail 模式：发现问题时退出码为 1"""
        test_file = tmp_path / "test.py"
        alias = _make_alias(2, "lower")
        test_file.write_text(f"# Deploy {alias} now\n")

        with mock.patch("sys.argv", ["prog", "--fail", "--root", str(tmp_path)]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def test_no_fail_mode(self, tmp_path: Path):
        """--no-fail 模式：发现问题时退出码为 0"""
        test_file = tmp_path / "test.py"
        alias = _make_alias(3, "lower")
        test_file.write_text(f"# Run {alias} task\n")

        with mock.patch("sys.argv", ["prog", "--no-fail", "--root", str(tmp_path)]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            # --no-fail 模式下退出码为 0
            assert exc_info.value.code == 0

    def test_no_fail_overrides_fail(self, tmp_path: Path):
        """--no-fail 应该覆盖 --fail"""
        test_file = tmp_path / "test.py"
        alias = _make_alias(1, "title")
        test_file.write_text(f"# {alias} deployment\n")

        # 同时指定 --fail 和 --no-fail，--no-fail 应该生效
        with mock.patch("sys.argv", ["prog", "--fail", "--no-fail", "--root", str(tmp_path)]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0

    def test_no_issues_exit_zero(self, tmp_path: Path):
        """无问题时，任何模式下退出码都为 0"""
        test_file = tmp_path / "test.py"
        # 只包含流程编号（带空格），不包含旧别名
        flow_num = _make_flow_num(1, "title")
        test_file.write_text(f"# {flow_num}: Introduction\n# Clean code\n")

        # 默认模式
        with mock.patch("sys.argv", ["prog", "--root", str(tmp_path)]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0

    def test_json_output_mode(self, tmp_path: Path, capsys):
        """--json 模式应输出 JSON 格式"""
        test_file = tmp_path / "test.py"
        alias = _make_alias(1, "lower")
        test_file.write_text(f"# {alias} here\n")

        with mock.patch("sys.argv", ["prog", "--json", "--no-fail", "--root", str(tmp_path)]):
            with pytest.raises(SystemExit):
                main()

        captured = capsys.readouterr()
        import json
        output = json.loads(captured.out)
        assert "status" in output
        assert "findings" in output
        assert len(output["findings"]) == 1

    def test_verbose_mode(self, tmp_path: Path, capsys):
        """--verbose 模式应输出详细信息"""
        test_file = tmp_path / "test.py"
        alias = _make_alias(2, "lower")
        test_file.write_text(f"# Deploy {alias} now\n")

        with mock.patch("sys.argv", ["prog", "--verbose", "--no-fail", "--root", str(tmp_path)]):
            with pytest.raises(SystemExit):
                main()

        captured = capsys.readouterr()
        # verbose 模式应包含上下文行
        assert alias in captured.out


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
