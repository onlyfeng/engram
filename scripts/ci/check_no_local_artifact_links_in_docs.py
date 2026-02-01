#!/usr/bin/env python3
"""
检查文档中的 .artifacts/ 链接

功能:
1. 扫描 docs/**/*.md 目录中的 Markdown 文件
2. 检测链接到 .artifacts/ 目录的相对路径链接
3. 输出违反规则的位置列表和修复建议

策略规则:
- 禁止在文档中使用指向 .artifacts/ 目录的相对链接
- 原因：.artifacts/ 是临时构建产物目录，不应被文档引用
- 建议：改为 CI Run URL、或改为 inline code、或改为链接到 docs/acceptance/evidence/

用法:
    # 检查 docs/ 目录
    python scripts/ci/check_no_local_artifact_links_in_docs.py

    # 检查指定路径
    python scripts/ci/check_no_local_artifact_links_in_docs.py --paths docs/gateway/

    # 详细输出
    python scripts/ci/check_no_local_artifact_links_in_docs.py --verbose

    # 仅统计（不阻断）
    python scripts/ci/check_no_local_artifact_links_in_docs.py --stats-only

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

# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class ArtifactLinkViolation:
    """本地 artifact 链接违规记录。"""

    file: Path
    line_number: int
    line_content: str
    matched_link: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line_number}: 包含 .artifacts/ 链接: {self.matched_link}"


# ============================================================================
# 正则表达式
# ============================================================================

# 匹配 Markdown 链接中包含 .artifacts/ 的相对路径
# 支持格式:
#   [text](../.artifacts/foo.md)
#   [text](.artifacts/bar.md)
#   [text](../../.artifacts/baz/qux.md)
#   [text](path/to/.artifacts/file.md)
ARTIFACT_LINK_PATTERN = re.compile(
    r"\]\("  # 链接开始 ](
    r"([^)]*"  # 捕获组：链接内容
    r"\.artifacts/"  # 必须包含 .artifacts/
    r"[^)]*)"  # 继续匹配到链接结束
    r"\)"  # 链接结束 )
)


# ============================================================================
# 配置
# ============================================================================


def get_project_root() -> Path:
    """获取项目根目录。"""
    script_dir = Path(__file__).resolve().parent
    return script_dir.parent.parent


def get_default_paths() -> List[str]:
    """获取默认检查路径。"""
    return ["docs/"]


def expand_paths(paths: List[str], project_root: Path) -> List[Path]:
    """
    展开路径列表为具体的 Markdown 文件列表。

    Args:
        paths: 路径列表（可包含文件或目录）
        project_root: 项目根目录

    Returns:
        Markdown 文件路径列表
    """
    files: List[Path] = []

    for path_str in paths:
        path = project_root / path_str

        if path.is_file() and path.suffix == ".md":
            files.append(path)
        elif path.is_dir() or path_str.endswith("/"):
            # 目录：递归查找所有 .md 文件
            dir_path = path if path.is_dir() else project_root / path_str.rstrip("/")
            if dir_path.exists():
                files.extend(dir_path.rglob("*.md"))

    return sorted(set(files))


# ============================================================================
# 扫描逻辑
# ============================================================================


def remove_inline_code(line: str) -> str:
    """
    移除行内的 inline code（反引号包围的内容）。

    这样可以避免匹配到 inline code 中作为示例展示的链接。

    Args:
        line: 原始行内容

    Returns:
        移除 inline code 后的行内容
    """
    # 匹配 `...` 形式的 inline code（包括 `` ` `` 形式的转义反引号）
    # 使用非贪婪匹配，处理多个 inline code
    return re.sub(r"`+[^`]+`+", "", line)


def scan_file_for_artifact_links(file_path: Path) -> Iterator[ArtifactLinkViolation]:
    """
    扫描单个文件中的 .artifacts/ 链接。

    跳过以下内容：
    1. Markdown 代码块（```...```）内的内容
    2. Inline code（`...`）内的内容

    Args:
        file_path: 要扫描的文件路径

    Yields:
        ArtifactLinkViolation 对象
    """
    try:
        content = file_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError) as e:
        print(f"[WARN] 无法读取文件 {file_path}: {e}", file=sys.stderr)
        return

    in_code_block = False
    for line_number, line in enumerate(content.splitlines(), start=1):
        # 检测代码块边界（支持 ``` 和 ~~~）
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code_block = not in_code_block
            continue

        # 跳过代码块内的内容
        if in_code_block:
            continue

        # 移除 inline code，避免匹配到示例内容
        line_without_inline_code = remove_inline_code(line)

        for match in ARTIFACT_LINK_PATTERN.finditer(line_without_inline_code):
            yield ArtifactLinkViolation(
                file=file_path,
                line_number=line_number,
                line_content=line.strip(),
                matched_link=match.group(1),
            )


# ============================================================================
# 检查执行
# ============================================================================


def run_check(
    paths: List[str] | None = None,
    verbose: bool = False,
    project_root: Path | None = None,
    quiet: bool = False,
) -> tuple[List[ArtifactLinkViolation], int]:
    """
    执行 .artifacts/ 链接检查。

    Args:
        paths: 要检查的路径列表（None 则使用默认路径 docs/）
        verbose: 是否显示详细输出
        project_root: 项目根目录（None 则自动检测）
        quiet: 是否静默模式（抑制所有输出）

    Returns:
        (违规列表, 总扫描文件数)
    """
    if project_root is None:
        project_root = get_project_root()

    # 获取要检查的路径
    if paths is None:
        paths = get_default_paths()
        if not quiet:
            print(f"[INFO] 使用默认路径: {', '.join(paths)}")

    # 展开路径为文件列表
    files = expand_paths(paths, project_root)

    if not files:
        if not quiet:
            print("[WARN] 未找到任何 Markdown 文件")
        return [], 0

    if verbose and not quiet:
        print(f"[INFO] 将检查 {len(files)} 个文件")
        for f in files[:10]:
            print(f"       - {f.relative_to(project_root)}")
        if len(files) > 10:
            print(f"       ... 及其他 {len(files) - 10} 个文件")
        print()

    # 扫描
    violations: List[ArtifactLinkViolation] = []

    for file_path in files:
        for violation in scan_file_for_artifact_links(file_path):
            violations.append(violation)

            if verbose and not quiet:
                rel_path = file_path.relative_to(project_root)
                print(f"  ❌ {rel_path}:{violation.line_number}")
                print(f"     链接: {violation.matched_link}")

    return violations, len(files)


def print_report(
    violations: List[ArtifactLinkViolation],
    total_files: int,
    verbose: bool = False,
) -> None:
    """
    打印检查报告。

    Args:
        violations: .artifacts/ 链接违规列表
        total_files: 总扫描文件数
        verbose: 是否显示详细输出
    """
    project_root = get_project_root()

    print()
    print("=" * 70)
    print(".artifacts/ 链接检查报告")
    print("=" * 70)
    print()

    print(f"扫描文件数:      {total_files}")
    print(f"违规条目数:      {len(violations)}")
    print()

    if violations:
        print("违规列表:")
        print("-" * 70)

        # 按文件分组
        by_file: dict[Path, List[ArtifactLinkViolation]] = {}
        for v in violations:
            by_file.setdefault(v.file, []).append(v)

        for file_path, vlist in sorted(by_file.items()):
            rel_path = file_path.relative_to(project_root)
            print(f"\n【{rel_path}】({len(vlist)} 条)")
            for v in vlist[:20]:  # 最多显示 20 条
                print(f"  第 {v.line_number} 行: {v.matched_link}")
                if verbose:
                    print(f"    {v.line_content[:80]}")
            if len(vlist) > 20:
                print(f"  ... 及其他 {len(vlist) - 20} 条")

        print()
        print("-" * 70)
        print()
        print("修复指南:")
        print("  .artifacts/ 是临时构建产物目录，不应在文档中被引用。")
        print()
        print("  建议修复方式:")
        print()
        print("  1. 若需要引用 CI 产物：使用 CI Run URL")
        print("     ❌ [报告](../.artifacts/coverage/report.html)")
        print("     ✓  [报告](https://github.com/org/repo/actions/runs/123456)")
        print()
        print("  2. 若是正式证据文件：迁移到 docs/acceptance/evidence/")
        print("     ❌ [截图](../.artifacts/screenshots/test.png)")
        print("     ✓  [截图](../acceptance/evidence/test.png)")
        print()
        print("  3. 若仅需提及路径：改为 inline code 或纯文本，不要 Markdown 链接")
        print("     ❌ [详见](.artifacts/notes.md)")
        print("     ✓  详见 `.artifacts/notes.md`")
        print()
    else:
        print("[OK] 未发现 .artifacts/ 链接")


# ============================================================================
# 主函数
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="检查文档中的 .artifacts/ 本地链接",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--paths",
        nargs="+",
        default=None,
        help="要检查的路径列表（默认: docs/）",
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

    project_root = get_project_root()

    print("=" * 70)
    print(".artifacts/ 链接检查")
    print("=" * 70)
    print()

    violations, total_files = run_check(
        paths=args.paths,
        verbose=args.verbose,
        project_root=project_root,
    )

    # 打印报告
    print_report(violations, total_files, verbose=args.verbose)

    # 确定退出码
    if args.stats_only:
        print()
        print("[INFO] --stats-only 模式: 仅统计，不阻断")
        print("[OK] 退出码: 0")
        return 0

    if len(violations) > 0:
        print()
        print(f"[FAIL] 存在 {len(violations)} 个违规")
        print("[FAIL] 退出码: 1")
        return 1

    print()
    print("[OK] 所有检查通过")
    print("[OK] 退出码: 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
