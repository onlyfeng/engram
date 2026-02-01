#!/usr/bin/env python3
"""
noqa 注释策略检查脚本

功能:
1. 扫描 src/ 和 tests/ 目录中的 `# noqa` 注释
2. 验证注释是否符合策略规则
3. 输出违反规则的位置列表和统计信息

策略规则:
1. 禁止裸 `# noqa`，必须带有错误码（如 `# noqa: F401` 或 `# noqa: F401, E501`）
2. 可选：对 lint-island 路径要求原因说明（同一行 `# <reason>` 或 `TODO: #issue`）

与 RUF100 的分工边界:
- 本脚本: 检查 noqa 语法规范（禁止裸 noqa，要求指定错误码）
- RUF100 (check_ruff_lint_island.py Phase 3): 检查 noqa 语义有效性（检测冗余 noqa）
  * RUF100 是 ruff 内置规则，用于检测已不再需要的 noqa 注释
  * 例如：代码已修复但 noqa 未移除，或 noqa 指定的错误码与实际不匹配
  * Phase 3 时由 check_ruff_lint_island.py 统一执行

用法:
    # 检查 src/ 和 tests/ 目录
    python scripts/ci/check_noqa_policy.py

    # 检查指定路径
    python scripts/ci/check_noqa_policy.py --paths src/engram/gateway/

    # 详细输出
    python scripts/ci/check_noqa_policy.py --verbose

    # 仅统计（不阻断）
    python scripts/ci/check_noqa_policy.py --stats-only

    # 要求原因说明（严格模式，全部文件）
    python scripts/ci/check_noqa_policy.py --require-reason

    # lint-island 严格模式（仅 lint-island 路径要求原因）
    python scripts/ci/check_noqa_policy.py --lint-island-strict

退出码:
    0 - 检查通过或 --stats-only 模式
    1 - 检查失败（存在违规）
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Iterator, List

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[import-not-found]

# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class NoqaViolation:
    """noqa 注释违规记录。"""

    file: Path
    line_number: int
    line_content: str
    violation_type: str
    error_codes: str | None = None

    def __str__(self) -> str:
        return (
            f"{self.file}:{self.line_number}: {self.violation_type}"
            f"{f' [{self.error_codes}]' if self.error_codes else ''}"
        )


@dataclass
class NoqaEntry:
    """noqa 注释条目。"""

    file: Path
    line_number: int
    line_content: str
    error_codes: str | None  # 提取的错误码（如 "F401" 或 "F401, E501"），如果是裸 noqa 则为 None
    has_reason: bool  # 是否有原因说明
    reason_text: str | None  # 原因说明文本


# ============================================================================
# 正则表达式
# ============================================================================

# 匹配 # noqa 注释（可选带错误码）
# 支持格式:
#   # noqa
#   # noqa: F401
#   # noqa: F401, E501
#   #noqa
#   #noqa: F401
NOQA_PATTERN = re.compile(r"#\s*noqa(?::\s*([A-Z][0-9]+(?:\s*,\s*[A-Z][0-9]+)*))?")

# 匹配 noqa 后面的原因说明
# 支持格式:
#   # noqa: F401  # 原因说明
#   # noqa: F401 # TODO: #123
#   # noqa: F401 # TODO:#issue
REASON_PATTERN = re.compile(r"#\s*noqa(?::\s*[A-Z][0-9]+(?:\s*,\s*[A-Z][0-9]+)*)?\s*#\s*(.+)")

# 匹配 TODO/FIXME 引用 issue 的格式
TODO_ISSUE_PATTERN = re.compile(
    r"(?:TODO|FIXME|XXX):\s*(?:#\d+|#[a-zA-Z][\w-]*)|"  # TODO: #123 或 TODO: #issue-name
    r"(?:TODO|FIXME|XXX)#\d+|"  # TODO#123
    r"https?://[^\s]+"  # URL 链接
)


# ============================================================================
# 配置读取
# ============================================================================


def get_project_root() -> Path:
    """获取项目根目录。"""
    script_dir = Path(__file__).resolve().parent
    return script_dir.parent.parent


def get_default_paths() -> List[str]:
    """获取默认检查路径。"""
    return ["src/", "tests/"]


def load_lint_island_paths(project_root: Path | None = None) -> List[str]:
    """
    从 pyproject.toml 加载 lint-island 路径列表。

    Args:
        project_root: 项目根目录（None 则自动检测）

    Returns:
        lint-island 路径列表（如 ["src/engram/gateway/di.py", "src/engram/gateway/services/"]）
    """
    if project_root is None:
        project_root = get_project_root()

    pyproject_path = project_root / "pyproject.toml"
    if not pyproject_path.exists():
        return []

    try:
        with open(pyproject_path, "rb") as f:
            config = tomllib.load(f)

        paths = config.get("tool", {}).get("engram", {}).get("ruff", {}).get("lint_island_paths", [])
        return list(paths) if paths else []
    except Exception as e:
        print(f"[WARN] 无法读取 pyproject.toml: {e}", file=sys.stderr)
        return []


def is_lint_island_path(file_path: Path, lint_island_paths: List[str], project_root: Path) -> bool:
    """
    检查文件是否在 lint-island 路径中。

    Args:
        file_path: 文件绝对路径
        lint_island_paths: lint-island 路径列表（相对于项目根目录）
        project_root: 项目根目录

    Returns:
        True 如果文件在 lint-island 路径中
    """
    if not lint_island_paths:
        return False

    try:
        rel_path = file_path.relative_to(project_root)
        rel_path_str = str(rel_path)
    except ValueError:
        return False

    for island_path in lint_island_paths:
        # 移除尾部斜杠以统一处理
        island_path = island_path.rstrip("/")

        # 精确匹配文件
        if rel_path_str == island_path:
            return True

        # 目录匹配（路径以目录开头）
        if rel_path_str.startswith(island_path + "/"):
            return True

        # 支持 glob 模式
        if fnmatch(rel_path_str, island_path) or fnmatch(rel_path_str, island_path + "/**"):
            return True

    return False


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
# 扫描逻辑
# ============================================================================


def scan_file_for_noqa(file_path: Path) -> Iterator[NoqaEntry]:
    """
    扫描单个文件中的 noqa 注释。

    Args:
        file_path: 要扫描的文件路径

    Yields:
        NoqaEntry 对象
    """
    try:
        content = file_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError) as e:
        print(f"[WARN] 无法读取文件 {file_path}: {e}", file=sys.stderr)
        return

    for line_number, line in enumerate(content.splitlines(), start=1):
        match = NOQA_PATTERN.search(line)
        if match:
            error_codes = match.group(1)  # 可能为 None

            # 检查是否有原因说明
            reason_match = REASON_PATTERN.search(line)
            has_reason = reason_match is not None
            reason_text = reason_match.group(1).strip() if reason_match else None

            yield NoqaEntry(
                file=file_path,
                line_number=line_number,
                line_content=line.strip(),
                error_codes=error_codes,
                has_reason=has_reason,
                reason_text=reason_text,
            )


def validate_noqa(entry: NoqaEntry, require_reason: bool = False) -> NoqaViolation | None:
    """
    验证 noqa 注释是否符合策略。

    Args:
        entry: NoqaEntry 对象
        require_reason: 是否要求原因说明

    Returns:
        NoqaViolation 如果违规，否则 None
    """
    # 规则 1: 禁止裸 noqa，必须带有错误码
    if entry.error_codes is None:
        return NoqaViolation(
            file=entry.file,
            line_number=entry.line_number,
            line_content=entry.line_content,
            violation_type="裸 noqa 禁止使用，必须指定错误码（如 # noqa: F401）",
        )

    # 规则 2: 可选要求原因说明
    if require_reason and not entry.has_reason:
        return NoqaViolation(
            file=entry.file,
            line_number=entry.line_number,
            line_content=entry.line_content,
            violation_type="缺少原因说明",
            error_codes=entry.error_codes,
        )

    return None


# ============================================================================
# 检查执行
# ============================================================================


def run_check(
    paths: List[str] | None = None,
    verbose: bool = False,
    stats_only: bool = False,
    require_reason: bool = False,
    lint_island_strict: bool = False,
    lint_island_paths: List[str] | None = None,
    project_root: Path | None = None,
) -> tuple[List[NoqaViolation], int]:
    """
    执行 noqa 注释策略检查。

    Args:
        paths: 要检查的路径列表（None 则使用默认路径 src/ 和 tests/）
        verbose: 是否显示详细输出
        stats_only: 是否仅统计（不阻断）
        require_reason: 是否要求原因说明（全部文件）
        lint_island_strict: 是否仅对 lint-island 路径要求原因说明
        lint_island_paths: lint-island 路径列表（None 则从 pyproject.toml 加载）
        project_root: 项目根目录（None 则自动检测）

    Returns:
        (违规列表, 总 noqa 条目数)
    """
    if project_root is None:
        project_root = get_project_root()

    # 获取要检查的路径
    if paths is None:
        paths = get_default_paths()
        print(f"[INFO] 使用默认路径: {', '.join(paths)}")

    # 加载 lint-island 路径
    if lint_island_strict and lint_island_paths is None:
        lint_island_paths = load_lint_island_paths(project_root)
        if verbose and lint_island_paths:
            print(f"[INFO] lint-island 路径 ({len(lint_island_paths)} 个):")
            for p in lint_island_paths:
                print(f"       - {p}")
            print()

    # 展开路径为文件列表
    files = expand_paths(paths, project_root)

    if not files:
        print("[WARN] 未找到任何 Python 文件")
        return [], 0

    if verbose:
        print(f"[INFO] 将检查 {len(files)} 个文件")
        for f in files[:10]:
            print(f"       - {f.relative_to(project_root)}")
        if len(files) > 10:
            print(f"       ... 及其他 {len(files) - 10} 个文件")
        print()

    # 扫描并验证
    violations: List[NoqaViolation] = []
    total_noqa = 0
    island_noqa = 0

    for file_path in files:
        # 判断是否为 lint-island 文件
        is_island = False
        if lint_island_strict and lint_island_paths:
            is_island = is_lint_island_path(file_path, lint_island_paths, project_root)

        for entry in scan_file_for_noqa(file_path):
            total_noqa += 1
            if is_island:
                island_noqa += 1

            # 决定是否要求原因说明
            # 1. require_reason=True: 全部文件都要求原因
            # 2. lint_island_strict=True: 仅 lint-island 文件要求原因
            file_require_reason = require_reason or (lint_island_strict and is_island)

            violation = validate_noqa(entry, require_reason=file_require_reason)
            if violation:
                violations.append(violation)

            if verbose:
                rel_path = file_path.relative_to(project_root)
                status = "❌" if violation else "✓"
                codes_info = entry.error_codes or "NO-CODE"
                reason_info = f" # {entry.reason_text}" if entry.reason_text else ""
                island_marker = " [ISLAND]" if is_island else ""
                print(f"  {status} {rel_path}:{entry.line_number} [{codes_info}]{reason_info}{island_marker}")

    if lint_island_strict and verbose:
        print()
        print(f"[INFO] lint-island 文件中的 noqa 条目数: {island_noqa}")

    return violations, total_noqa


def print_report(
    violations: List[NoqaViolation],
    total_noqa: int,
    verbose: bool = False,
) -> None:
    """
    打印检查报告。

    Args:
        violations: 违规列表
        total_noqa: 总 noqa 条目数
        verbose: 是否显示详细输出
    """
    project_root = get_project_root()

    print()
    print("=" * 70)
    print("noqa 策略检查报告")
    print("=" * 70)
    print()

    print(f"总 noqa 条目数:  {total_noqa}")
    print(f"违规条目数:      {len(violations)}")
    print()

    if violations:
        print("违规列表:")
        print("-" * 70)

        # 按违规类型分组
        by_type: dict[str, List[NoqaViolation]] = {}
        for v in violations:
            by_type.setdefault(v.violation_type, []).append(v)

        for vtype, vlist in sorted(by_type.items()):
            print(f"\n【{vtype}】({len(vlist)} 条)")
            for v in vlist[:20]:  # 最多显示 20 条
                rel_path = v.file.relative_to(project_root)
                print(f"  {rel_path}:{v.line_number}")
                if verbose:
                    print(f"    {v.line_content[:80]}")
            if len(vlist) > 20:
                print(f"  ... 及其他 {len(vlist) - 20} 条")

        print()
        print("-" * 70)
        print()
        print("修复指南:")
        print("  1. 所有 noqa 必须带有错误码:")
        print("     ❌ # noqa")
        print("     ✓  # noqa: F401")
        print("     ✓  # noqa: F401, E501")
        print()
        print("  2. 可选：添加原因说明（严格模式下必须）:")
        print("     ❌ # noqa: F401")
        print("     ✓  # noqa: F401  # re-export for public API")
        print("     ✓  # noqa: F401  # TODO: #123")
        print()
    else:
        print("[OK] 所有 noqa 注释符合策略")


# ============================================================================
# 主函数
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="noqa 注释策略检查工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--paths",
        nargs="+",
        default=None,
        help="要检查的路径列表（默认: src/ tests/）",
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
    parser.add_argument(
        "--require-reason",
        action="store_true",
        help="要求原因说明（严格模式，全部文件）",
    )
    parser.add_argument(
        "--lint-island-strict",
        action="store_true",
        help="仅对 lint-island 路径要求原因说明（从 pyproject.toml 读取路径）",
    )

    args = parser.parse_args()

    print("=" * 70)
    print("noqa 策略检查")
    if args.lint_island_strict:
        print("模式: lint-island-strict（仅 lint-island 路径要求原因说明）")
    elif args.require_reason:
        print("模式: require-reason（全部文件要求原因说明）")
    else:
        print("模式: 默认（仅禁止裸 noqa）")
    print("=" * 70)
    print()

    # 执行检查
    violations, total_noqa = run_check(
        paths=args.paths,
        verbose=args.verbose,
        stats_only=args.stats_only,
        require_reason=args.require_reason,
        lint_island_strict=args.lint_island_strict,
    )

    # 打印报告
    print_report(violations, total_noqa, verbose=args.verbose)

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
