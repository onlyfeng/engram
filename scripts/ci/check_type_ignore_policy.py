#!/usr/bin/env python3
"""
type: ignore 注释策略检查脚本

功能:
1. 扫描 strict-island 路径下的 `# type: ignore` 注释
2. 验证注释是否符合策略规则
3. 输出违反规则的位置列表和统计信息

策略规则:
1. 所有 `# type: ignore` 必须带有 `[error-code]`
2. strict-island 内必须带原因说明：
   - 同一行的 `# 描述文字`
   - 或 `TODO:#issue` / `TODO: #123` 格式的 issue 引用

用法:
    # 检查 strict-island 路径
    python scripts/ci/check_type_ignore_policy.py

    # 检查指定路径
    python scripts/ci/check_type_ignore_policy.py --paths src/engram/gateway/

    # 详细输出
    python scripts/ci/check_type_ignore_policy.py --verbose

    # 仅统计（不阻断）
    python scripts/ci/check_type_ignore_policy.py --stats-only

退出码:
    0 - 检查通过或 --stats-only 模式
    1 - 检查失败（存在违规）
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List

# Python 3.11+ 内置 tomllib，3.10 需要 tomli
try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[import-not-found]


# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class TypeIgnoreViolation:
    """类型忽略注释违规记录。"""

    file: Path
    line_number: int
    line_content: str
    violation_type: str
    error_code: str | None = None

    def __str__(self) -> str:
        return (
            f"{self.file}:{self.line_number}: {self.violation_type}"
            f"{f' [{self.error_code}]' if self.error_code else ''}"
        )


@dataclass
class TypeIgnoreEntry:
    """类型忽略注释条目。"""

    file: Path
    line_number: int
    line_content: str
    error_code: str | None  # 提取的 [error-code]，如果没有则为 None
    has_reason: bool  # 是否有原因说明
    reason_text: str | None  # 原因说明文本


# ============================================================================
# 正则表达式
# ============================================================================

# 匹配 # type: ignore 注释（可选带 [error-code]）
# 支持格式:
#   # type: ignore
#   # type: ignore[error-code]
#   # type: ignore[error-code, another-code]
TYPE_IGNORE_PATTERN = re.compile(
    r"#\s*type:\s*ignore(?:\s*\[([^\]]+)\])?"
)

# 匹配 type: ignore 后面的原因说明
# 支持格式:
#   # type: ignore[code]  # 原因说明
#   # type: ignore[code] # TODO: #123
#   # type: ignore[code] # TODO:#issue
REASON_PATTERN = re.compile(
    r"#\s*type:\s*ignore(?:\s*\[[^\]]+\])?\s*#\s*(.+)"
)

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


def load_strict_island_paths(pyproject_path: Path | None = None) -> List[str]:
    """
    从 pyproject.toml 读取 strict island 路径列表。

    Args:
        pyproject_path: pyproject.toml 文件路径

    Returns:
        strict island 路径列表

    Raises:
        FileNotFoundError: 如果 pyproject.toml 不存在
        KeyError: 如果配置中缺少 [tool.engram.mypy].strict_island_paths
    """
    if pyproject_path is None:
        pyproject_path = get_project_root() / "pyproject.toml"

    with open(pyproject_path, "rb") as f:
        config = tomllib.load(f)

    try:
        paths = config["tool"]["engram"]["mypy"]["strict_island_paths"]
    except KeyError as e:
        raise KeyError(
            f"pyproject.toml 中缺少 [tool.engram.mypy].strict_island_paths 配置: {e}"
        ) from e

    if not isinstance(paths, list):
        raise TypeError(
            f"strict_island_paths 必须是列表类型，实际类型: {type(paths).__name__}"
        )

    return paths


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


def scan_file_for_type_ignores(file_path: Path) -> Iterator[TypeIgnoreEntry]:
    """
    扫描单个文件中的 type: ignore 注释。

    Args:
        file_path: 要扫描的文件路径

    Yields:
        TypeIgnoreEntry 对象
    """
    try:
        content = file_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError) as e:
        print(f"[WARN] 无法读取文件 {file_path}: {e}", file=sys.stderr)
        return

    for line_number, line in enumerate(content.splitlines(), start=1):
        match = TYPE_IGNORE_PATTERN.search(line)
        if match:
            error_code = match.group(1)  # 可能为 None

            # 检查是否有原因说明
            reason_match = REASON_PATTERN.search(line)
            has_reason = reason_match is not None
            reason_text = reason_match.group(1).strip() if reason_match else None

            yield TypeIgnoreEntry(
                file=file_path,
                line_number=line_number,
                line_content=line.strip(),
                error_code=error_code,
                has_reason=has_reason,
                reason_text=reason_text,
            )


def validate_type_ignore(
    entry: TypeIgnoreEntry, require_reason: bool = True
) -> TypeIgnoreViolation | None:
    """
    验证 type: ignore 注释是否符合策略。

    Args:
        entry: TypeIgnoreEntry 对象
        require_reason: 是否要求原因说明

    Returns:
        TypeIgnoreViolation 如果违规，否则 None
    """
    # 规则 1: 必须带有 [error-code]
    if entry.error_code is None:
        return TypeIgnoreViolation(
            file=entry.file,
            line_number=entry.line_number,
            line_content=entry.line_content,
            violation_type="缺少错误码 [error-code]",
        )

    # 规则 2: strict-island 内必须带原因说明
    if require_reason and not entry.has_reason:
        return TypeIgnoreViolation(
            file=entry.file,
            line_number=entry.line_number,
            line_content=entry.line_content,
            violation_type="缺少原因说明",
            error_code=entry.error_code,
        )

    return None


# ============================================================================
# 检查执行
# ============================================================================


def run_check(
    paths: List[str] | None = None,
    verbose: bool = False,
    stats_only: bool = False,
) -> tuple[List[TypeIgnoreViolation], int]:
    """
    执行类型忽略注释策略检查。

    Args:
        paths: 要检查的路径列表（None 则使用 strict-island 配置）
        verbose: 是否显示详细输出
        stats_only: 是否仅统计（不阻断）

    Returns:
        (违规列表, 总 ignore 条目数)
    """
    project_root = get_project_root()

    # 获取要检查的路径
    if paths is None:
        try:
            paths = load_strict_island_paths()
            print("[INFO] 使用 pyproject.toml 中的 strict_island_paths 配置")
        except (FileNotFoundError, KeyError) as e:
            print(f"[ERROR] 无法加载配置: {e}", file=sys.stderr)
            return [], 0

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
    violations: List[TypeIgnoreViolation] = []
    total_ignores = 0

    for file_path in files:
        for entry in scan_file_for_type_ignores(file_path):
            total_ignores += 1

            violation = validate_type_ignore(entry, require_reason=True)
            if violation:
                violations.append(violation)

            if verbose:
                rel_path = file_path.relative_to(project_root)
                status = "❌" if violation else "✓"
                reason_info = f" # {entry.reason_text}" if entry.reason_text else ""
                print(
                    f"  {status} {rel_path}:{entry.line_number} "
                    f"[{entry.error_code or 'NO-CODE'}]{reason_info}"
                )

    return violations, total_ignores


def print_report(
    violations: List[TypeIgnoreViolation],
    total_ignores: int,
    verbose: bool = False,
) -> None:
    """
    打印检查报告。

    Args:
        violations: 违规列表
        total_ignores: 总 ignore 条目数
        verbose: 是否显示详细输出
    """
    project_root = get_project_root()

    print()
    print("=" * 70)
    print("type: ignore 策略检查报告")
    print("=" * 70)
    print()

    print(f"总 ignore 条目数:  {total_ignores}")
    print(f"违规条目数:        {len(violations)}")
    print()

    if violations:
        print("违规列表:")
        print("-" * 70)

        # 按违规类型分组
        by_type: dict[str, List[TypeIgnoreViolation]] = {}
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
        print("  1. 所有 type: ignore 必须带 [error-code]:")
        print("     ❌ # type: ignore")
        print("     ✓  # type: ignore[arg-type]")
        print()
        print("  2. strict-island 内必须带原因说明:")
        print("     ❌ # type: ignore[arg-type]")
        print("     ✓  # type: ignore[arg-type]  # 第三方库类型不完整")
        print("     ✓  # type: ignore[arg-type]  # TODO: #123")
        print()
        print("  详见: docs/dev/mypy_error_playbook.md")
    else:
        print("[OK] 所有 type: ignore 注释符合策略")


# ============================================================================
# 主函数
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="type: ignore 注释策略检查工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--paths",
        nargs="+",
        default=None,
        help="要检查的路径列表（默认使用 pyproject.toml 中的 strict_island_paths）",
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
        "--pyproject",
        type=str,
        default=None,
        help="pyproject.toml 文件路径（默认自动检测）",
    )

    args = parser.parse_args()

    print("=" * 70)
    print("type: ignore 策略检查")
    print("=" * 70)
    print()

    # 执行检查
    violations, total_ignores = run_check(
        paths=args.paths,
        verbose=args.verbose,
        stats_only=args.stats_only,
    )

    # 打印报告
    print_report(violations, total_ignores, verbose=args.verbose)

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
