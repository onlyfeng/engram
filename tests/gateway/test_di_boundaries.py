"""
Gateway DI 边界测试

验证 src/engram/gateway/handlers/**/*.py 中不包含禁止的 import/调用模式。
确保 handlers 模块遵循依赖注入原则。

禁止的模式:
- get_container( : handlers 应通过 deps 获取依赖
- get_config(    : handlers 应通过 deps.config 获取配置
- get_client(    : handlers 应通过 deps.openmemory_client 获取客户端
- logbook_adapter.get_adapter( : handlers 应通过 deps.logbook_adapter 获取适配器
- GatewayDeps.create( : handlers 不应直接创建依赖容器
- deps is None   : handlers 不应检查 deps 是否为 None（依赖必须由调用方提供）

例外:
- 类型注释 (TYPE_CHECKING 块) 中的 import 不计入
- 带有 `# DI-BOUNDARY-ALLOW:` 标记的行（legacy fallback 兼容期）

此测试与 scripts/ci/check_gateway_di_boundaries.py 使用相同的检测逻辑，
可作为 pytest 的一部分运行，确保 PR 一旦引入违规调用即失败。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Set

import pytest

# ============================================================================
# 配置区（与 scripts/ci/check_gateway_di_boundaries.py 保持一致）
# ============================================================================

# 禁止的调用模式（正则表达式）
FORBIDDEN_PATTERNS: List[tuple[str, str]] = [
    (r"\bget_container\s*\(", "get_container("),
    (r"\bget_config\s*\(", "get_config("),
    (r"\bget_client\s*\(", "get_client("),
    (r"\blogbook_adapter\.get_adapter\s*\(", "logbook_adapter.get_adapter("),
    (r"\bGatewayDeps\.create\s*\(", "GatewayDeps.create("),
    (r"\bdeps\s+is\s+None\b", "deps is None"),
]

# 允许例外的文件（相对于 handlers 目录）
# 当前无文件级例外
ALLOWED_EXCEPTIONS: dict[str, Set[str]] = {}

# DI 边界允许标记（用于标识 legacy fallback 兼容分支）
# 格式: # DI-BOUNDARY-ALLOW: <reason>
DI_BOUNDARY_ALLOW_MARKER = "# DI-BOUNDARY-ALLOW:"


# ============================================================================
# 辅助函数
# ============================================================================


def get_project_root() -> Path:
    """获取项目根目录"""
    # 从测试文件位置向上查找项目根
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "pyproject.toml").exists() or (parent / "setup.py").exists():
            return parent
        if (parent / "src" / "engram").exists():
            return parent
    # 回退到假设的目录结构
    return current.parent.parent.parent


def is_in_type_checking_block(lines: List[str], line_index: int) -> bool:
    """检查指定行是否在 TYPE_CHECKING 块内"""
    in_block = False
    indent_level = -1

    for i in range(line_index - 1, -1, -1):
        line = lines[i]
        stripped = line.strip()

        if not stripped or stripped.startswith("#"):
            continue

        if re.match(r"^if\s+(typing\.)?TYPE_CHECKING\s*:", stripped):
            indent_level = len(line) - len(line.lstrip())
            in_block = True
            break

        current_indent = len(line) - len(line.lstrip())
        if current_indent == 0 and not stripped.startswith("if"):
            break

    if not in_block:
        return False

    current_line = lines[line_index]
    current_indent = len(current_line) - len(current_line.lstrip())
    return current_indent > indent_level


def is_in_docstring(lines: List[str], line_index: int) -> bool:
    """检查指定行是否在 docstring 中"""
    TRIPLE_DOUBLE = '"""'
    TRIPLE_SINGLE = "'''"

    in_docstring = False
    docstring_char = None

    for i in range(line_index):
        line = lines[i]
        for delim in [TRIPLE_DOUBLE, TRIPLE_SINGLE]:
            count = line.count(delim)
            if count > 0:
                if not in_docstring:
                    in_docstring = True
                    docstring_char = delim
                    if count % 2 == 0:
                        in_docstring = False
                        docstring_char = None
                elif delim == docstring_char:
                    in_docstring = False
                    docstring_char = None

    current_line = lines[line_index]
    if in_docstring:
        if docstring_char and docstring_char in current_line:
            return True
        return True

    for delim in [TRIPLE_DOUBLE, TRIPLE_SINGLE]:
        if delim in current_line:
            idx = current_line.find(delim)
            rest = current_line[idx + 3 :]
            if delim in rest:
                return True

    return False


def is_in_string_literal(line: str, match_start: int) -> bool:
    """检查匹配位置是否在字符串字面量内"""
    prefix = line[:match_start].replace('\\"', "").replace("\\'", "")
    double_quotes = prefix.count('"')
    single_quotes = prefix.count("'")
    return (double_quotes % 2 == 1) or (single_quotes % 2 == 1)


def has_di_boundary_allow_marker(lines: List[str], line_index: int) -> bool:
    """
    检查指定行是否有 DI-BOUNDARY-ALLOW 标记

    允许标记出现在：
    1. 当前行本身（行尾注释或上方注释）
    2. 上一行（注释行标记下一行代码）

    Args:
        lines: 文件所有行
        line_index: 当前行索引（0-based）

    Returns:
        True 如果该行被 DI-BOUNDARY-ALLOW 标记允许
    """
    current_line = lines[line_index]

    # 检查当前行是否包含标记（行尾注释）
    if DI_BOUNDARY_ALLOW_MARKER in current_line:
        return True

    # 检查上一行是否是 DI-BOUNDARY-ALLOW 注释行
    if line_index > 0:
        prev_line = lines[line_index - 1].strip()
        if prev_line.startswith(DI_BOUNDARY_ALLOW_MARKER):
            return True

    return False


def scan_file_for_violations(file_path: Path) -> List[tuple[int, str, str]]:
    """
    扫描单个文件中的违规调用

    Returns:
        List of (line_number, pattern_name, line_content) tuples
    """
    violations: List[tuple[int, str, str]] = []

    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception:
        return violations

    lines = content.splitlines()
    file_name = file_path.name
    allowed_patterns = ALLOWED_EXCEPTIONS.get(file_name, set())

    for line_index, line in enumerate(lines):
        line_number = line_index + 1
        stripped = line.strip()

        if stripped.startswith("#"):
            continue

        if is_in_type_checking_block(lines, line_index):
            continue

        # 跳过 docstring 中的内容
        if is_in_docstring(lines, line_index):
            continue

        # 跳过带有 DI-BOUNDARY-ALLOW 标记的行
        if has_di_boundary_allow_marker(lines, line_index):
            continue

        for pattern_regex, pattern_name in FORBIDDEN_PATTERNS:
            if pattern_name in allowed_patterns:
                continue

            match = re.search(pattern_regex, line)
            if match:
                # 检查匹配是否在字符串字面量内
                if is_in_string_literal(line, match.start()):
                    continue

                violations.append((line_number, pattern_name, line.strip()))

    return violations


# ============================================================================
# 测试用例
# ============================================================================


class TestGatewayDIBoundaries:
    """Gateway DI 边界测试套件"""

    @pytest.fixture(scope="class")
    def handlers_dir(self) -> Path:
        """获取 handlers 目录路径"""
        project_root = get_project_root()
        return project_root / "src" / "engram" / "gateway" / "handlers"

    @pytest.fixture(scope="class")
    def handler_files(self, handlers_dir: Path) -> List[Path]:
        """获取所有 handler Python 文件"""
        if not handlers_dir.exists():
            pytest.skip(f"handlers 目录不存在: {handlers_dir}")
        return list(handlers_dir.rglob("*.py"))

    def test_handlers_dir_exists(self, handlers_dir: Path) -> None:
        """验证 handlers 目录存在"""
        assert handlers_dir.exists(), f"handlers 目录不存在: {handlers_dir}"

    def test_no_forbidden_calls_in_handlers(self, handler_files: List[Path]) -> None:
        """
        验证 handlers 中无禁止的 DI 边界违规调用

        此测试扫描所有 handler 文件，检查是否存在:
        - get_container()
        - get_config()
        - get_client()
        - logbook_adapter.get_adapter()

        如发现违规，测试失败并报告具体位置。
        """
        all_violations: List[tuple[Path, int, str, str]] = []

        for file_path in handler_files:
            violations = scan_file_for_violations(file_path)
            for line_number, pattern, line_content in violations:
                all_violations.append((file_path, line_number, pattern, line_content))

        if all_violations:
            # 构建详细的错误消息
            error_lines = [f"\n发现 {len(all_violations)} 处 DI 边界违规调用:\n"]
            for file_path, line_number, pattern, line_content in all_violations:
                relative_path = file_path.relative_to(get_project_root())
                error_lines.append(f"  {relative_path}:{line_number}")
                error_lines.append(f"    禁止模式: {pattern}")
                error_lines.append(f"    代码: {line_content}")
                error_lines.append("")

            error_lines.append(
                "handlers 应通过 deps 参数获取依赖，不应直接调用全局获取函数。\n"
                "参见 docs/gateway/06_gateway_design.md"
            )

            pytest.fail("\n".join(error_lines))

    @pytest.mark.parametrize(
        "pattern_name,pattern_regex",
        [
            ("get_container(", r"\bget_container\s*\("),
            ("get_config(", r"\bget_config\s*\("),
            ("get_client(", r"\bget_client\s*\("),
            ("logbook_adapter.get_adapter(", r"\blogbook_adapter\.get_adapter\s*\("),
            ("GatewayDeps.create(", r"\bGatewayDeps\.create\s*\("),
            ("deps is None", r"\bdeps\s+is\s+None\b"),
        ],
    )
    def test_individual_pattern_not_in_handlers(
        self,
        handler_files: List[Path],
        pattern_name: str,
        pattern_regex: str,
    ) -> None:
        """
        分别测试每个禁止模式

        这提供了更细粒度的测试反馈，便于定位具体问题。
        """
        violations_for_pattern: List[tuple[Path, int, str]] = []

        for file_path in handler_files:
            file_name = file_path.name
            allowed = ALLOWED_EXCEPTIONS.get(file_name, set())

            if pattern_name in allowed:
                continue

            try:
                content = file_path.read_text(encoding="utf-8")
            except Exception:
                continue

            lines = content.splitlines()

            for line_index, line in enumerate(lines):
                line_number = line_index + 1
                stripped = line.strip()

                if stripped.startswith("#"):
                    continue

                if is_in_type_checking_block(lines, line_index):
                    continue

                # 跳过 docstring 中的内容
                if is_in_docstring(lines, line_index):
                    continue

                # 跳过带有 DI-BOUNDARY-ALLOW 标记的行
                if has_di_boundary_allow_marker(lines, line_index):
                    continue

                match = re.search(pattern_regex, line)
                if match:
                    # 检查匹配是否在字符串字面量内
                    if is_in_string_literal(line, match.start()):
                        continue

                    violations_for_pattern.append((file_path, line_number, line.strip()))

        if violations_for_pattern:
            error_lines = [f"\n发现 {len(violations_for_pattern)} 处 '{pattern_name}' 违规调用:\n"]
            for file_path, line_number, line_content in violations_for_pattern:
                relative_path = file_path.relative_to(get_project_root())
                error_lines.append(f"  {relative_path}:{line_number}: {line_content}")

            pytest.fail("\n".join(error_lines))


class TestDIBoundaryExceptions:
    """测试 DI 边界例外配置"""

    def test_no_file_level_exceptions(self) -> None:
        """验证当前无文件级例外"""
        assert len(ALLOWED_EXCEPTIONS) == 0, "不应有文件级例外"

    def test_forbidden_patterns_defined(self) -> None:
        """验证禁止模式列表非空"""
        assert len(FORBIDDEN_PATTERNS) > 0

    def test_new_patterns_included(self) -> None:
        """验证新增的禁止模式已包含"""
        pattern_names = [name for _, name in FORBIDDEN_PATTERNS]
        assert "GatewayDeps.create(" in pattern_names
        assert "deps is None" in pattern_names

    def test_all_patterns_have_names(self) -> None:
        """验证所有禁止模式都有描述性名称"""
        for pattern_regex, pattern_name in FORBIDDEN_PATTERNS:
            assert pattern_name, f"模式 {pattern_regex} 缺少名称"

    def test_di_boundary_allow_marker_defined(self) -> None:
        """验证 DI-BOUNDARY-ALLOW 标记已定义"""
        assert DI_BOUNDARY_ALLOW_MARKER == "# DI-BOUNDARY-ALLOW:"


class TestDIBoundaryAllowMarker:
    """测试 DI-BOUNDARY-ALLOW 标记功能"""

    def test_marker_on_same_line(self) -> None:
        """测试同行标记（行尾注释）"""
        lines = [
            "def foo():",
            "    config = get_config()  # DI-BOUNDARY-ALLOW: legacy fallback",
            "    return config",
        ]
        # 第 1 行（index=1）应该被允许
        assert has_di_boundary_allow_marker(lines, 1) is True
        # 第 2 行不应该被允许
        assert has_di_boundary_allow_marker(lines, 2) is False

    def test_marker_on_previous_line(self) -> None:
        """测试上一行标记（注释行标记下一行代码）"""
        lines = [
            "def foo():",
            "    # DI-BOUNDARY-ALLOW: legacy fallback",
            "    config = get_config()",
            "    return config",
        ]
        # 第 2 行（index=2）应该被允许（因为上一行有标记）
        assert has_di_boundary_allow_marker(lines, 2) is True
        # 第 3 行不应该被允许
        assert has_di_boundary_allow_marker(lines, 3) is False

    def test_no_marker(self) -> None:
        """测试没有标记的行"""
        lines = [
            "def foo():",
            "    config = get_config()",
            "    return config",
        ]
        assert has_di_boundary_allow_marker(lines, 1) is False
        assert has_di_boundary_allow_marker(lines, 2) is False

    def test_marker_with_reason(self) -> None:
        """测试带原因的标记"""
        lines = [
            "# DI-BOUNDARY-ALLOW: v0.9 兼容期，v1.0 移除",
            "deps = GatewayDeps.create(config=config)",
        ]
        assert has_di_boundary_allow_marker(lines, 1) is True
