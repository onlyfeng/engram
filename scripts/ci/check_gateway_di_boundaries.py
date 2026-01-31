#!/usr/bin/env python3
"""
Gateway DI 边界检查脚本

扫描以下目录中禁止的 import/调用模式：
- src/engram/gateway/handlers/**/*.py
- src/engram/gateway/services/**/*.py

确保 handlers 和 services 模块遵循依赖注入原则，不直接调用全局容器/配置获取函数。

禁止的模式:
- get_container( : handlers/services 应通过 deps 获取依赖，不应直接调用容器
- get_config(    : handlers/services 应通过 deps.config 获取配置
- get_client(    : handlers/services 应通过 deps.openmemory_client 获取客户端
- logbook_adapter.get_adapter( : handlers/services 应通过 deps.logbook_adapter 获取适配器
- GatewayDeps.create( : handlers/services 不应直接创建依赖容器
- deps is None   : handlers/services 不应检查 deps 是否为 None（依赖必须由调用方提供）

例外:
- 类型注释 (TYPE_CHECKING 块) 中的 import 不计入
- 带有 `# DI-BOUNDARY-ALLOW:` 标记的行（legacy fallback 兼容期）

稳定标记:
- `# DI-BOUNDARY-ALLOW: <reason>` - 单行标记，允许当前行或下一行的禁止调用
- 这些标记用于标识 v0.9 兼容期的 legacy fallback 分支，将在 v1.0 移除

用法:
    python scripts/ci/check_gateway_di_boundaries.py [--verbose] [--json]

退出码:
    0 - 检查通过，无违规
    1 - 发现违规调用
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Set


# ============================================================================
# 配置区
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

# 扫描目标目录（相对于项目根）
SCAN_DIRECTORIES = [
    Path("src/engram/gateway/handlers"),
    Path("src/engram/gateway/services"),
]

# 向后兼容别名
HANDLERS_DIR = Path("src/engram/gateway/handlers")


# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class Violation:
    """单个违规记录"""

    file: str
    line_number: int
    line_content: str
    pattern: str
    message: str


@dataclass
class CheckResult:
    """检查结果"""

    violations: List[Violation] = field(default_factory=list)
    files_scanned: int = 0
    files_with_violations: Set[str] = field(default_factory=set)

    def has_violations(self) -> bool:
        return len(self.violations) > 0

    def to_dict(self) -> dict:
        return {
            "ok": not self.has_violations(),
            "violation_count": len(self.violations),
            "files_scanned": self.files_scanned,
            "files_with_violations": sorted(self.files_with_violations),
            "violations": [
                {
                    "file": v.file,
                    "line_number": v.line_number,
                    "line_content": v.line_content.strip(),
                    "pattern": v.pattern,
                    "message": v.message,
                }
                for v in self.violations
            ],
        }


# ============================================================================
# 扫描逻辑
# ============================================================================


def is_in_docstring(lines: List[str], line_index: int) -> bool:
    """
    检查指定行是否在 docstring 中

    简单启发式：检查该行是否在三引号包围的区域内
    """
    # 定义分隔符
    TRIPLE_DOUBLE = '"""'
    TRIPLE_SINGLE = "'''"

    in_docstring = False
    docstring_char = None

    for i in range(line_index):
        line = lines[i]

        # 检查双引号三引号
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

    # 检查当前行是否也包含 docstring 分隔符
    current_line = lines[line_index]
    if in_docstring:
        if docstring_char and docstring_char in current_line:
            return True
        return True

    # 检查当前行是否开始一个同行关闭的 docstring
    for delim in [TRIPLE_DOUBLE, TRIPLE_SINGLE]:
        if delim in current_line:
            idx = current_line.find(delim)
            rest = current_line[idx + 3:]
            if delim in rest:
                return True

    return False


def is_in_string_literal(line: str, match_start: int) -> bool:
    """
    检查匹配位置是否在字符串字面量内

    简单启发式：计算匹配位置之前的引号数量
    """
    # 移除转义引号以简化计算
    prefix = line[:match_start].replace('\\"', '').replace("\\'", '')

    # 计算单引号和双引号数量
    double_quotes = prefix.count('"')
    single_quotes = prefix.count("'")

    # 如果引号数量为奇数，说明在字符串内
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


def is_in_type_checking_block(lines: List[str], line_index: int) -> bool:
    """
    检查指定行是否在 TYPE_CHECKING 块内

    简单启发式：向上查找最近的 if TYPE_CHECKING: 或 if typing.TYPE_CHECKING:
    如果找到且未遇到对应的顶层代码，则认为在 TYPE_CHECKING 块内
    """
    # 向上扫描查找 TYPE_CHECKING 块开始
    in_block = False
    indent_level = -1

    for i in range(line_index - 1, -1, -1):
        line = lines[i]
        stripped = line.strip()

        # 跳过空行和纯注释
        if not stripped or stripped.startswith("#"):
            continue

        # 检查是否是 TYPE_CHECKING 块开始
        if re.match(r"^if\s+(typing\.)?TYPE_CHECKING\s*:", stripped):
            # 计算该行的缩进级别
            indent_level = len(line) - len(line.lstrip())
            in_block = True
            break

        # 如果遇到顶层代码（无缩进），停止搜索
        current_indent = len(line) - len(line.lstrip())
        if current_indent == 0 and not stripped.startswith("if"):
            break

    if not in_block:
        return False

    # 检查当前行的缩进是否大于 TYPE_CHECKING 块的缩进
    current_line = lines[line_index]
    current_indent = len(current_line) - len(current_line.lstrip())
    return current_indent > indent_level


def scan_file(file_path: Path, relative_path: str) -> List[Violation]:
    """扫描单个文件中的违规调用"""
    violations: List[Violation] = []

    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception as e:
        # 无法读取文件，跳过
        return violations

    lines = content.splitlines()
    file_name = file_path.name

    # 获取该文件的例外列表
    allowed_patterns = ALLOWED_EXCEPTIONS.get(file_name, set())

    for line_index, line in enumerate(lines):
        line_number = line_index + 1

        # 跳过注释行
        stripped = line.strip()
        if stripped.startswith("#"):
            continue

        # 跳过 TYPE_CHECKING 块内的 import
        if is_in_type_checking_block(lines, line_index):
            continue

        # 跳过 docstring 中的内容
        if is_in_docstring(lines, line_index):
            continue

        # 检查该行是否有 DI-BOUNDARY-ALLOW 标记
        if has_di_boundary_allow_marker(lines, line_index):
            continue

        # 检查每个禁止的模式
        for pattern_regex, pattern_name in FORBIDDEN_PATTERNS:
            # 检查是否在文件级例外列表中
            if pattern_name in allowed_patterns:
                continue

            match = re.search(pattern_regex, line)
            if match:
                # 检查匹配是否在字符串字面量内
                if is_in_string_literal(line, match.start()):
                    continue

                violations.append(
                    Violation(
                        file=relative_path,
                        line_number=line_number,
                        line_content=line,
                        pattern=pattern_name,
                        message=f"禁止在 handlers 中直接调用 {pattern_name}，应通过 deps 参数获取依赖",
                    )
                )

    return violations


def scan_handlers_directory(project_root: Path) -> CheckResult:
    """扫描 handlers 和 services 目录中的所有 Python 文件"""
    result = CheckResult()

    for scan_dir in SCAN_DIRECTORIES:
        dir_path = project_root / scan_dir

        if not dir_path.exists():
            print(f"[WARN] 目录不存在: {dir_path}", file=sys.stderr)
            continue

        # 递归查找所有 .py 文件
        py_files = list(dir_path.rglob("*.py"))
        result.files_scanned += len(py_files)

        for py_file in py_files:
            relative_path = str(py_file.relative_to(project_root))
            violations = scan_file(py_file, relative_path)

            if violations:
                result.violations.extend(violations)
                result.files_with_violations.add(relative_path)

    return result


# ============================================================================
# 主函数
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="检查 Gateway handlers 模块中禁止的 DI 边界违规调用"
    )
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
    result = scan_handlers_directory(project_root)

    # 输出结果
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        print("=" * 70)
        print("Gateway DI 边界检查")
        print("=" * 70)
        print()
        print("扫描目录:")
        for scan_dir in SCAN_DIRECTORIES:
            print(f"  - {project_root / scan_dir}")
        print(f"扫描文件数: {result.files_scanned}")
        print()

        if not result.has_violations():
            print("[OK] 未发现 DI 边界违规")
        else:
            print(f"[ERROR] 发现 {len(result.violations)} 处违规调用:")
            print()

            for v in result.violations:
                print(f"  {v.file}:{v.line_number}")
                print(f"    模式: {v.pattern}")
                print(f"    代码: {v.line_content.strip()}")
                if args.verbose:
                    print(f"    说明: {v.message}")
                print()

        print("-" * 70)
        print(f"违规总数: {len(result.violations)}")
        print(f"涉及文件: {len(result.files_with_violations)}")
        print()

        if result.has_violations():
            print("[FAIL] DI 边界检查失败")
        else:
            print("[OK] DI 边界检查通过")

    # 退出码
    return 1 if result.has_violations() else 0


if __name__ == "__main__":
    sys.exit(main())
