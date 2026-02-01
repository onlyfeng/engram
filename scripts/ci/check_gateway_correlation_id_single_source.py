#!/usr/bin/env python3
"""
Gateway correlation_id 单一来源检查脚本

确保 correlation_id 的生成和校验逻辑只在 correlation_id.py 中定义，
其他模块必须从该模块导入，不允许重复实现。

检查规则:
=========
1. 禁止在 correlation_id.py 以外的文件中定义 generate_correlation_id 函数
2. 禁止在 correlation_id.py 以外的文件中出现特定实现片段:
   - uuid.uuid4().hex[:16] (生成逻辑)
   - re.compile(r"^corr-[a-fA-F0-9]{16}$") (校验模式定义)
3. 允许从 .correlation_id 或 correlation_id 模块导入（re-export 模式）

例外:
=====
- correlation_id.py 本身（单一来源定义位置）
- TYPE_CHECKING 块内的类型注解
- 字符串字面量和文档字符串中的内容
- 带有 # CORRELATION-ID-SINGLE-SOURCE-ALLOW: 标记的行

用法:
    python scripts/ci/check_gateway_correlation_id_single_source.py [--verbose] [--json]

退出码:
    0 - 检查通过，无违规
    1 - 发现违规
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Set

# ============================================================================
# 配置区
# ============================================================================

# 单一来源文件（相对于 gateway 目录）
SINGLE_SOURCE_FILE = "correlation_id.py"

# 扫描目录（相对于项目根目录）
SCAN_DIRECTORY = Path("src/engram/gateway")

# 禁止的实现模式（正则表达式）
# 这些模式不应出现在 correlation_id.py 以外的文件中
FORBIDDEN_PATTERNS: List[tuple[str, str, str]] = [
    # (正则表达式, 模式名称, 说明)
    (
        r"uuid\.uuid4\(\)\.hex\[:16\]",
        "uuid4_hex_slice",
        "correlation_id 生成逻辑应从 correlation_id.py 导入",
    ),
    (
        r're\.compile\s*\(\s*r?["\'].*corr-\[a-fA-F0-9\].*["\']\s*\)',
        "corr_id_pattern_compile",
        "correlation_id 校验模式应从 correlation_id.py 导入 CORRELATION_ID_PATTERN",
    ),
    (
        r'["\']corr-["\'].*uuid',
        "corr_prefix_uuid",
        "correlation_id 格式字符串应使用 generate_correlation_id()",
    ),
]

# 禁止的函数定义（在单一来源文件以外不允许定义）
FORBIDDEN_FUNCTION_DEFINITIONS: List[str] = [
    "generate_correlation_id",
    "is_valid_correlation_id",
    "normalize_correlation_id",
]

# 允许标记（用于特殊情况的豁免）
ALLOW_MARKER = "# CORRELATION-ID-SINGLE-SOURCE-ALLOW:"


# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class Violation:
    """单个违规记录"""

    file: str
    line_number: int
    line_content: str
    violation_type: str  # "function_definition" | "forbidden_pattern"
    pattern_name: str
    message: str


@dataclass
class CheckResult:
    """检查结果"""

    violations: List[Violation] = field(default_factory=list)
    files_scanned: int = 0
    files_with_violations: Set[str] = field(default_factory=set)
    single_source_file_exists: bool = False
    single_source_file_valid: bool = False

    def has_violations(self) -> bool:
        return len(self.violations) > 0

    def to_dict(self) -> dict:
        return {
            "ok": not self.has_violations(),
            "violation_count": len(self.violations),
            "files_scanned": self.files_scanned,
            "files_with_violations": sorted(self.files_with_violations),
            "single_source_file_exists": self.single_source_file_exists,
            "single_source_file_valid": self.single_source_file_valid,
            "violations": [
                {
                    "file": v.file,
                    "line_number": v.line_number,
                    "line_content": v.line_content.strip(),
                    "violation_type": v.violation_type,
                    "pattern_name": v.pattern_name,
                    "message": v.message,
                }
                for v in self.violations
            ],
        }


# ============================================================================
# AST 分析
# ============================================================================


class FunctionDefinitionVisitor(ast.NodeVisitor):
    """AST 访问器：查找函数定义"""

    def __init__(self) -> None:
        self.function_defs: List[tuple[str, int]] = []  # (函数名, 行号)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.function_defs.append((node.name, node.lineno))
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.function_defs.append((node.name, node.lineno))
        self.generic_visit(node)


def find_function_definitions(content: str) -> List[tuple[str, int]]:
    """
    使用 AST 查找文件中的函数定义

    Returns:
        [(函数名, 行号), ...]
    """
    try:
        tree = ast.parse(content)
        visitor = FunctionDefinitionVisitor()
        visitor.visit(tree)
        return visitor.function_defs
    except SyntaxError:
        return []


# ============================================================================
# 文本扫描辅助函数
# ============================================================================


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
    """
    检查匹配位置是否在普通字符串字面量内（非 f-string 表达式部分）

    对于 f-string，其 {} 内的表达式部分仍是代码，应该被检查。
    """
    prefix = line[:match_start].replace('\\"', "").replace("\\'", "")

    # 检查是否在 f-string 的 {} 表达式内
    # f-string 的表达式部分是代码，不应被跳过
    if 'f"' in prefix or "f'" in prefix:
        # 简化检查：如果前缀中有未闭合的 {，则认为在表达式内
        # 找到 f-string 开始位置后计算 { 和 } 的数量
        for quote_char in ['"', "'"]:
            fstring_marker = f"f{quote_char}"
            if fstring_marker in prefix:
                # 找到最后一个 f-string 开始位置
                idx = prefix.rfind(fstring_marker)
                after_fstring_start = prefix[idx + 2 :]
                # 计算未闭合的大括号数量（忽略转义的 {{ 和 }}）
                open_braces = 0
                i = 0
                while i < len(after_fstring_start):
                    char = after_fstring_start[i]
                    if char == "{":
                        # 检查是否是转义的 {{
                        if i + 1 < len(after_fstring_start) and after_fstring_start[i + 1] == "{":
                            i += 2
                            continue
                        open_braces += 1
                    elif char == "}":
                        # 检查是否是转义的 }}
                        if i + 1 < len(after_fstring_start) and after_fstring_start[i + 1] == "}":
                            i += 2
                            continue
                        if open_braces > 0:
                            open_braces -= 1
                    i += 1

                # 如果有未闭合的 {，则在表达式内，应该检查
                if open_braces > 0:
                    return False

    # 普通字符串检查
    double_quotes = prefix.count('"')
    single_quotes = prefix.count("'")
    return (double_quotes % 2 == 1) or (single_quotes % 2 == 1)


def has_allow_marker(lines: List[str], line_index: int) -> bool:
    """检查指定行是否有允许标记"""
    current_line = lines[line_index]

    if ALLOW_MARKER in current_line:
        return True

    if line_index > 0:
        prev_line = lines[line_index - 1].strip()
        if prev_line.startswith(ALLOW_MARKER):
            return True

    return False


def is_import_statement(line: str) -> bool:
    """检查是否是 import 语句（允许 re-export）"""
    stripped = line.strip()
    # 允许从 correlation_id 导入
    if "from .correlation_id import" in stripped:
        return True
    if "from engram.gateway.correlation_id import" in stripped:
        return True
    # 允许带有 as 的 re-export 形式
    if re.search(r"from\s+\.correlation_id\s+import.*as", stripped):
        return True
    return False


# ============================================================================
# 扫描逻辑
# ============================================================================


def scan_file(file_path: Path, relative_path: str) -> List[Violation]:
    """扫描单个文件中的违规"""
    violations: List[Violation] = []

    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception:
        return violations

    lines = content.splitlines()

    # 1. 检查禁止的函数定义（使用 AST）
    func_defs = find_function_definitions(content)
    for func_name, line_number in func_defs:
        if func_name in FORBIDDEN_FUNCTION_DEFINITIONS:
            line_content = lines[line_number - 1] if line_number <= len(lines) else ""
            # 检查是否有允许标记
            if not has_allow_marker(lines, line_number - 1):
                violations.append(
                    Violation(
                        file=relative_path,
                        line_number=line_number,
                        line_content=line_content,
                        violation_type="function_definition",
                        pattern_name=func_name,
                        message=(
                            f"禁止在 {SINGLE_SOURCE_FILE} 以外定义 {func_name}()，"
                            f"应从 correlation_id.py 导入"
                        ),
                    )
                )

    # 2. 检查禁止的实现模式（使用正则）
    for line_index, line in enumerate(lines):
        line_number = line_index + 1

        # 跳过注释行
        stripped = line.strip()
        if stripped.startswith("#"):
            continue

        # 跳过 TYPE_CHECKING 块
        if is_in_type_checking_block(lines, line_index):
            continue

        # 跳过 docstring
        if is_in_docstring(lines, line_index):
            continue

        # 跳过带允许标记的行
        if has_allow_marker(lines, line_index):
            continue

        # 跳过 import 语句（允许 re-export）
        if is_import_statement(line):
            continue

        # 检查禁止的模式
        for pattern_regex, pattern_name, message in FORBIDDEN_PATTERNS:
            match = re.search(pattern_regex, line)
            if match:
                # 检查是否在字符串字面量内（文档字符串中的示例等）
                if is_in_string_literal(line, match.start()):
                    continue

                violations.append(
                    Violation(
                        file=relative_path,
                        line_number=line_number,
                        line_content=line,
                        violation_type="forbidden_pattern",
                        pattern_name=pattern_name,
                        message=message,
                    )
                )

    return violations


def verify_single_source_file(project_root: Path) -> tuple[bool, bool]:
    """
    验证单一来源文件存在且包含必要的定义

    Returns:
        (文件存在, 文件有效)
    """
    file_path = project_root / SCAN_DIRECTORY / SINGLE_SOURCE_FILE
    if not file_path.exists():
        return False, False

    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception:
        return True, False

    # 验证必要的函数定义存在
    func_defs = find_function_definitions(content)
    defined_funcs = {name for name, _ in func_defs}

    required_funcs = set(FORBIDDEN_FUNCTION_DEFINITIONS)
    if not required_funcs.issubset(defined_funcs):
        return True, False

    # 验证 CORRELATION_ID_PATTERN 常量存在
    if "CORRELATION_ID_PATTERN" not in content:
        return True, False

    return True, True


def scan_gateway_directory(project_root: Path) -> CheckResult:
    """扫描 gateway 目录中的所有 Python 文件"""
    result = CheckResult()

    # 验证单一来源文件
    result.single_source_file_exists, result.single_source_file_valid = verify_single_source_file(
        project_root
    )

    if not result.single_source_file_exists:
        result.violations.append(
            Violation(
                file=str(SCAN_DIRECTORY / SINGLE_SOURCE_FILE),
                line_number=0,
                line_content="",
                violation_type="missing_source",
                pattern_name="single_source_file",
                message=f"单一来源文件 {SINGLE_SOURCE_FILE} 不存在",
            )
        )
        return result

    if not result.single_source_file_valid:
        result.violations.append(
            Violation(
                file=str(SCAN_DIRECTORY / SINGLE_SOURCE_FILE),
                line_number=0,
                line_content="",
                violation_type="invalid_source",
                pattern_name="single_source_file",
                message=f"单一来源文件 {SINGLE_SOURCE_FILE} 缺少必要的函数定义或常量",
            )
        )

    # 扫描所有 .py 文件
    gateway_dir = project_root / SCAN_DIRECTORY
    if not gateway_dir.exists():
        return result

    py_files = list(gateway_dir.rglob("*.py"))
    result.files_scanned = len(py_files)

    for py_file in py_files:
        relative_path = str(py_file.relative_to(project_root))

        # 跳过单一来源文件本身
        if py_file.name == SINGLE_SOURCE_FILE:
            continue

        violations = scan_file(py_file, relative_path)

        if violations:
            result.violations.extend(violations)
            result.files_with_violations.add(relative_path)

    return result


# ============================================================================
# 主函数
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 Gateway correlation_id 单一来源契约")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="显示详细信息",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="JSON 格式输出",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="项目根目录（默认自动检测）",
    )
    args = parser.parse_args()

    # 确定项目根目录
    if args.project_root:
        project_root = args.project_root.resolve()
    else:
        script_dir = Path(__file__).resolve().parent
        project_root = script_dir.parent.parent  # scripts/ci/ 的父父目录

    if not project_root.exists():
        print(f"[ERROR] 项目根目录不存在: {project_root}", file=sys.stderr)
        return 1

    # 执行扫描
    result = scan_gateway_directory(project_root)

    # 输出结果
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        print("=" * 70)
        print("Gateway correlation_id 单一来源检查")
        print("=" * 70)
        print()
        print(f"扫描目录: {project_root / SCAN_DIRECTORY}")
        print(f"单一来源文件: {SINGLE_SOURCE_FILE}")
        print(f"扫描文件数: {result.files_scanned}")
        print()

        if result.single_source_file_exists:
            print(f"[OK] 单一来源文件存在: {SINGLE_SOURCE_FILE}")
        else:
            print(f"[ERROR] 单一来源文件不存在: {SINGLE_SOURCE_FILE}")

        if result.single_source_file_valid:
            print("[OK] 单一来源文件包含必要定义")
        elif result.single_source_file_exists:
            print("[ERROR] 单一来源文件缺少必要定义")

        print()

        if not result.has_violations():
            print("[OK] 未发现单一来源违规")
        else:
            print(f"[ERROR] 发现 {len(result.violations)} 处违规:")
            print()

            for v in result.violations:
                print(f"  {v.file}:{v.line_number}")
                print(f"    类型: {v.violation_type}")
                print(f"    模式: {v.pattern_name}")
                if v.line_content:
                    print(f"    代码: {v.line_content.strip()}")
                if args.verbose:
                    print(f"    说明: {v.message}")
                print()

        print("-" * 70)
        print(f"违规总数: {len(result.violations)}")
        print(f"涉及文件: {len(result.files_with_violations)}")
        print()

        if result.has_violations():
            print("[FAIL] correlation_id 单一来源检查失败")
        else:
            print("[OK] correlation_id 单一来源检查通过")

    # 退出码
    return 1 if result.has_violations() else 0


if __name__ == "__main__":
    sys.exit(main())
