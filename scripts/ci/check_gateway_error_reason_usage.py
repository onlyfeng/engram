#!/usr/bin/env python3
"""
Gateway ErrorReason 使用规范检查脚本

功能:
1. 扫描 src/engram/gateway/ 目录中与 MCP 错误相关的 `reason="..."` 模式
2. 检测硬编码的错误原因字符串（如 reason="SOME_ERROR"）
3. 推荐使用 ErrorReason.X 常量

规则:
1. 禁止在 MCP 错误上下文中使用 `reason="HARD_CODED_STRING"` 形式
2. MCP 错误上下文包括：
   - ErrorData 构造
   - make_jsonrpc_error / make_business_error_response / make_dependency_error_response
   - GatewayError 构造
3. 允许的白名单场景：
   - 文档字符串（docstring）
   - 测试文件（tests/ 目录）
   - 私有常量定义（如 _INTERNAL_REASON = "..."）
   - ErrorReason 类定义本身
   - 非 MCP 错误相关的 reason（如 PolicyDecision, ValidateRefsDecision 等业务数据结构）

用法:
    # 检查 src/engram/gateway/ 目录
    python scripts/ci/check_gateway_error_reason_usage.py

    # 检查指定路径
    python scripts/ci/check_gateway_error_reason_usage.py --paths src/engram/gateway/

    # 详细输出
    python scripts/ci/check_gateway_error_reason_usage.py --verbose

    # 仅统计（不阻断）
    python scripts/ci/check_gateway_error_reason_usage.py --stats-only

退出码:
    0 - 检查通过或 --stats-only 模式
    1 - 检查失败（存在违规）
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional

# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class ErrorReasonViolation:
    """ErrorReason 使用违规记录。"""

    file: Path
    line_number: int
    line_content: str
    violation_type: str
    found_value: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line_number}: {self.violation_type} [{self.found_value}]"


@dataclass
class ErrorReasonUsage:
    """ErrorReason 使用条目。"""

    file: Path
    line_number: int
    line_content: str
    value: str  # 提取的字符串值
    is_constant: bool  # 是否使用了 ErrorReason.X 常量
    context: str  # 上下文（docstring, class_def, assignment, call）


# ============================================================================
# 正则表达式
# ============================================================================

# 匹配 reason="..." 模式（包括单引号和双引号）
# 捕获 reason 参数的值
REASON_STRING_PATTERN = re.compile(
    r'\breason\s*=\s*["\']([A-Z][A-Z0-9_]*)["\']',
    re.IGNORECASE,
)

# 匹配 ErrorReason.X 常量使用
REASON_CONSTANT_PATTERN = re.compile(
    r'\breason\s*=\s*(?:ErrorReason|McpErrorReason)\.([A-Z][A-Z0-9_]*)',
)

# 匹配私有常量定义（如 _INTERNAL_REASON = "..."）
PRIVATE_CONSTANT_PATTERN = re.compile(
    r'^(\s*)_[A-Z][A-Z0-9_]*\s*=\s*["\']',
)

# 匹配 ErrorReason 或 McpErrorReason 类定义中的常量
CLASS_CONSTANT_PATTERN = re.compile(
    r'^(\s*)([A-Z][A-Z0-9_]*)\s*=\s*["\']([A-Z][A-Z0-9_]*)["\']',
)

# MCP 错误相关的函数/类名（这些上下文中的 reason 参数需要使用 ErrorReason 常量）
MCP_ERROR_CONTEXTS = frozenset([
    "ErrorData",
    "GatewayError",
    "make_jsonrpc_error",
    "make_business_error_response",
    "make_dependency_error_response",
    "make_business_error_result",  # 已废弃但仍支持
    "make_dependency_error_result",  # 已废弃但仍支持
    "to_jsonrpc_error",
])

# 非 MCP 错误相关的数据结构（这些的 reason 字段不受检查约束）
NON_MCP_REASON_CONTEXTS = frozenset([
    "PolicyDecision",
    "ValidateRefsDecision",
    "AuditEvent",  # audit event 的 reason 字段是不同的命名空间
])


# ============================================================================
# 配置
# ============================================================================


def get_project_root() -> Path:
    """获取项目根目录。"""
    script_dir = Path(__file__).resolve().parent
    return script_dir.parent.parent


def get_default_paths() -> List[str]:
    """获取默认检查路径。"""
    return ["src/engram/gateway/"]


def expand_paths(paths: List[str], project_root: Path) -> List[Path]:
    """
    展开路径列表为具体的 Python 文件列表。

    Args:
        paths: 路径列表（可包含文件或目录）
        project_root: 项目根目录

    Returns:
        Python 文件路径列表
    """
    files: List[Path] = []

    for path_str in paths:
        path = project_root / path_str

        if path.is_file() and path.suffix == ".py":
            files.append(path)
        elif path.is_dir() or path_str.endswith("/"):
            # 目录：递归查找所有 .py 文件
            dir_path = path if path.is_dir() else project_root / path_str.rstrip("/")
            if dir_path.exists():
                files.extend(dir_path.rglob("*.py"))

    return sorted(set(files))


# ============================================================================
# MCP 错误上下文检测
# ============================================================================


def is_in_mcp_error_context(line_content: str, file_content: str, line_number: int) -> bool:
    """
    检查指定行是否在 MCP 错误上下文中。

    MCP 错误上下文包括:
    - ErrorData(...) 构造
    - GatewayError(...) 构造
    - make_jsonrpc_error / make_business_error_response 等函数调用

    如果行包含非 MCP 错误相关的数据结构（如 PolicyDecision），则返回 False。

    Args:
        line_content: 当前行内容
        file_content: 整个文件内容
        line_number: 行号（1-based）

    Returns:
        True 如果在 MCP 错误上下文中
    """
    # 首先检查是否在非 MCP 错误上下文中
    for ctx in NON_MCP_REASON_CONTEXTS:
        if ctx in line_content:
            return False

    # 检查当前行是否直接包含 MCP 错误上下文
    for ctx in MCP_ERROR_CONTEXTS:
        if ctx in line_content:
            return True

    # 查找多行调用：向上查找函数调用开始
    lines = file_content.splitlines()
    if line_number < 1 or line_number > len(lines):
        return False

    # 向上查找最多 15 行，寻找包含函数调用起始的行
    # 策略：查找包含 MCP_ERROR_CONTEXTS 或 NON_MCP_REASON_CONTEXTS 关键字的行
    for i in range(line_number - 2, max(-1, line_number - 16), -1):
        if i < 0:
            break
        line = lines[i]

        # 检查该行是否包含 MCP 错误上下文的函数调用开始
        for ctx in MCP_ERROR_CONTEXTS:
            if ctx in line and "(" in line:
                return True

        # 检查是否是非 MCP 错误上下文
        for ctx in NON_MCP_REASON_CONTEXTS:
            if ctx in line and "(" in line:
                return False

        # 如果遇到语句结束标志（没有未闭合的括号），停止搜索
        # 简化处理：如果行以 `)` 或 `;` 结尾且不包含 `(`, 可能是上一个语句的结束
        stripped = line.rstrip()
        if stripped.endswith(")") or stripped.endswith(";"):
            # 检查这行是否可能是函数调用的结尾
            open_count = line.count("(")
            close_count = line.count(")")
            if close_count > open_count:
                # 这行关闭了更多括号，可能是上一个语句
                continue

    return False


def is_in_sql_context(line_content: str) -> bool:
    """
    检查指定行是否在 SQL 上下文中。

    SQL 语句中的 reason = 'value' 不应被检查。

    Args:
        line_content: 当前行内容

    Returns:
        True 如果在 SQL 上下文中
    """
    # 检查常见的 SQL 关键字
    sql_keywords = ["SELECT", "INSERT", "UPDATE", "DELETE", "WHERE", "WHEN", "THEN", "CASE"]
    line_upper = line_content.upper()
    return any(kw in line_upper for kw in sql_keywords)


# ============================================================================
# 白名单判定
# ============================================================================


def is_in_test_file(file_path: Path, project_root: Path) -> bool:
    """检查文件是否为测试文件。"""
    try:
        rel_path = file_path.relative_to(project_root)
        rel_path_str = str(rel_path)
        return (
            rel_path_str.startswith("tests/")
            or "/tests/" in rel_path_str
            or rel_path.name.startswith("test_")
        )
    except ValueError:
        return False


def is_in_docstring(content: str, line_number: int) -> bool:
    """
    检查指定行是否在文档字符串内。

    使用 AST 解析来准确判断。
    """
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return False

    for node in ast.walk(tree):
        # 检查函数、类、模块的 docstring
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            if node.body and isinstance(node.body[0], ast.Expr):
                if isinstance(node.body[0].value, ast.Constant):
                    docstring_node = node.body[0]
                    # 检查行号范围
                    if hasattr(docstring_node, "lineno") and hasattr(docstring_node, "end_lineno"):
                        if docstring_node.lineno <= line_number <= (
                            docstring_node.end_lineno or docstring_node.lineno
                        ):
                            return True

    return False


def is_in_error_reason_class_def(file_path: Path, line_number: int, content: str) -> bool:
    """
    检查指定行是否在 ErrorReason 或 McpErrorReason 类定义内部。

    这些类定义本身需要使用 CONSTANT = "value" 的形式。
    """
    lines = content.splitlines()
    if line_number < 1 or line_number > len(lines):
        return False

    # 向上查找类定义
    current_indent = len(lines[line_number - 1]) - len(lines[line_number - 1].lstrip())

    for i in range(line_number - 1, -1, -1):
        line = lines[i]
        if not line.strip():
            continue

        line_indent = len(line) - len(line.lstrip())

        # 如果找到缩进更少的行，检查是否是类定义
        if line_indent < current_indent:
            if re.match(r"^\s*class\s+(ErrorReason|McpErrorReason)\b", line):
                return True
            # 如果是其他类定义或非类语句，停止搜索
            if re.match(r"^\s*class\s+", line) or (
                line_indent == 0 and not line.strip().startswith("#")
            ):
                return False

    return False


def is_private_constant_definition(line_content: str) -> bool:
    """检查行是否为私有常量定义（以 _ 开头的大写常量）。"""
    return bool(PRIVATE_CONSTANT_PATTERN.match(line_content))


def is_whitelisted(
    file_path: Path,
    line_number: int,
    line_content: str,
    file_content: str,
    project_root: Path,
) -> tuple[bool, str]:
    """
    检查指定行是否在白名单中。

    Returns:
        (is_whitelisted, reason)
    """
    # 1. 测试文件
    if is_in_test_file(file_path, project_root):
        return True, "测试文件"

    # 2. 文档字符串
    if is_in_docstring(file_content, line_number):
        return True, "文档字符串"

    # 3. ErrorReason 类定义内部
    if is_in_error_reason_class_def(file_path, line_number, file_content):
        return True, "ErrorReason 类定义"

    # 4. 私有常量定义
    if is_private_constant_definition(line_content):
        return True, "私有常量定义"

    # 5. 注释行
    stripped = line_content.lstrip()
    if stripped.startswith("#"):
        return True, "注释行"

    # 6. SQL 上下文
    if is_in_sql_context(line_content):
        return True, "SQL 上下文"

    # 7. 非 MCP 错误上下文（PolicyDecision, ValidateRefsDecision 等）
    if not is_in_mcp_error_context(line_content, file_content, line_number):
        return True, "非 MCP 错误上下文"

    return False, ""


# ============================================================================
# 扫描逻辑
# ============================================================================


def scan_file_for_reason_usage(
    file_path: Path,
    project_root: Path,
) -> Iterator[tuple[ErrorReasonUsage, Optional[ErrorReasonViolation]]]:
    """
    扫描单个文件中的 reason= 使用。

    Args:
        file_path: 要扫描的文件路径
        project_root: 项目根目录

    Yields:
        (ErrorReasonUsage, Optional[ErrorReasonViolation]) 元组
    """
    try:
        content = file_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError) as e:
        print(f"[WARN] 无法读取文件 {file_path}: {e}", file=sys.stderr)
        return

    for line_number, line in enumerate(content.splitlines(), start=1):
        # 检查 ErrorReason.X 常量使用（正确用法）
        const_match = REASON_CONSTANT_PATTERN.search(line)
        if const_match:
            yield ErrorReasonUsage(
                file=file_path,
                line_number=line_number,
                line_content=line.strip(),
                value=const_match.group(1),
                is_constant=True,
                context="constant",
            ), None
            continue

        # 检查 reason="..." 字符串使用
        string_match = REASON_STRING_PATTERN.search(line)
        if string_match:
            value = string_match.group(1)

            # 检查白名单
            is_wl, wl_reason = is_whitelisted(
                file_path, line_number, line, content, project_root
            )

            usage = ErrorReasonUsage(
                file=file_path,
                line_number=line_number,
                line_content=line.strip(),
                value=value,
                is_constant=False,
                context=wl_reason if is_wl else "string_literal",
            )

            if is_wl:
                yield usage, None
            else:
                violation = ErrorReasonViolation(
                    file=file_path,
                    line_number=line_number,
                    line_content=line.strip(),
                    violation_type="硬编码 reason 字符串，应使用 ErrorReason.X 常量",
                    found_value=value,
                )
                yield usage, violation


# ============================================================================
# 检查执行
# ============================================================================


def run_check(
    paths: Optional[List[str]] = None,
    verbose: bool = False,
    stats_only: bool = False,
    project_root: Optional[Path] = None,
) -> tuple[List[ErrorReasonViolation], int, int]:
    """
    执行 ErrorReason 使用规范检查。

    Args:
        paths: 要检查的路径列表（None 则使用默认路径）
        verbose: 是否显示详细输出
        stats_only: 是否仅统计（不阻断）
        project_root: 项目根目录（None 则自动检测）

    Returns:
        (违规列表, 总使用数, 正确使用数)
    """
    if project_root is None:
        project_root = get_project_root()

    # 获取要检查的路径
    if paths is None:
        paths = get_default_paths()
        print(f"[INFO] 使用默认路径: {', '.join(paths)}")

    # 展开路径为文件列表
    files = expand_paths(paths, project_root)

    if not files:
        print("[WARN] 未找到任何 Python 文件")
        return [], 0, 0

    if verbose:
        print(f"[INFO] 将检查 {len(files)} 个文件")
        for f in files[:10]:
            print(f"       - {f.relative_to(project_root)}")
        if len(files) > 10:
            print(f"       ... 及其他 {len(files) - 10} 个文件")
        print()

    # 扫描并验证
    violations: List[ErrorReasonViolation] = []
    total_usage = 0
    correct_usage = 0
    whitelisted_usage = 0

    for file_path in files:
        for usage, violation in scan_file_for_reason_usage(file_path, project_root):
            total_usage += 1

            if violation:
                violations.append(violation)
            elif usage.is_constant:
                correct_usage += 1
            else:
                whitelisted_usage += 1

            if verbose:
                rel_path = file_path.relative_to(project_root)
                if violation:
                    print(f"  ❌ {rel_path}:{usage.line_number} [{usage.value}]")
                    print(f"      {usage.line_content[:80]}")
                elif usage.is_constant:
                    print(f"  ✓  {rel_path}:{usage.line_number} [ErrorReason.{usage.value}]")
                else:
                    print(
                        f"  ⚪ {rel_path}:{usage.line_number} [{usage.value}] (白名单: {usage.context})"
                    )

    if verbose:
        print()
        print("[INFO] 统计:")
        print(f"       总使用数: {total_usage}")
        print(f"       正确使用: {correct_usage}")
        print(f"       白名单:   {whitelisted_usage}")
        print(f"       违规:     {len(violations)}")

    return violations, total_usage, correct_usage


def print_report(
    violations: List[ErrorReasonViolation],
    total_usage: int,
    correct_usage: int,
    verbose: bool = False,
) -> None:
    """
    打印检查报告。

    Args:
        violations: 违规列表
        total_usage: 总使用数
        correct_usage: 正确使用数
        verbose: 是否显示详细输出
    """
    project_root = get_project_root()

    print()
    print("=" * 70)
    print("Gateway ErrorReason 使用规范检查报告")
    print("=" * 70)
    print()

    print(f"总 reason= 使用数:     {total_usage}")
    print(f"正确使用 (常量):       {correct_usage}")
    print(f"违规使用 (硬编码):     {len(violations)}")
    print()

    if violations:
        print("违规列表:")
        print("-" * 70)

        for v in violations[:30]:  # 最多显示 30 条
            rel_path = v.file.relative_to(project_root)
            print(f"  {rel_path}:{v.line_number}")
            print(f"    发现: reason=\"{v.found_value}\"")
            print(f"    推荐: reason=ErrorReason.{v.found_value}")
            if verbose:
                print(f"    行:   {v.line_content[:70]}")
            print()

        if len(violations) > 30:
            print(f"  ... 及其他 {len(violations) - 30} 条")

        print("-" * 70)
        print()
        print("修复指南:")
        print("  1. 将硬编码的 reason 字符串替换为 ErrorReason 常量:")
        print('     ❌ reason="POLICY_REJECT"')
        print("     ✓  reason=ErrorReason.POLICY_REJECT")
        print()
        print("  2. 如果需要新的错误原因码，先在 ErrorReason 类中定义:")
        print("     class ErrorReason:")
        print('         NEW_REASON = "NEW_REASON"')
        print()
        print("  3. 白名单场景（不会报错）:")
        print("     - 测试文件 (tests/ 目录)")
        print("     - 文档字符串")
        print("     - ErrorReason/McpErrorReason 类定义内部")
        print("     - 私有常量定义 (_INTERNAL_REASON = ...)")
        print()
    else:
        print("[OK] 所有 reason= 使用符合规范")


# ============================================================================
# 主函数
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Gateway ErrorReason 使用规范检查工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--paths",
        nargs="+",
        default=None,
        help="要检查的路径列表（默认: src/engram/gateway/）",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="显示详细输出",
    )
    parser.add_argument(
        "--stats-only",
        action="store_true",
        help="仅统计，不阻断（始终返回 0）",
    )

    args = parser.parse_args()

    print("=" * 70)
    print("Gateway ErrorReason 使用规范检查")
    print("=" * 70)
    print()

    # 执行检查
    violations, total_usage, correct_usage = run_check(
        paths=args.paths,
        verbose=args.verbose,
        stats_only=args.stats_only,
    )

    # 打印报告
    print_report(violations, total_usage, correct_usage, verbose=args.verbose)

    # 确定退出码
    if args.stats_only:
        print()
        print("[INFO] --stats-only 模式: 仅统计，不阻断")
        print("[OK] 退出码: 0")
        return 0

    if violations:
        print()
        print(f"[FAIL] 存在 {len(violations)} 个违规")
        print("[FAIL] 退出码: 1")
        return 1

    print()
    print("[OK] 退出码: 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
