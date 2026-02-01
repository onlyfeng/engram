#!/usr/bin/env python3
"""将 SSOT 迭代文档快照到本地目录，用于阅读和实验。

用法:
    python scripts/iteration/snapshot_ssot_iteration.py <iteration_number> [options]

示例:
    # 快照 Iteration 10 到 .iteration/_export/10/
    python scripts/iteration/snapshot_ssot_iteration.py 10

    # 快照到自定义目录
    python scripts/iteration/snapshot_ssot_iteration.py 10 --output-dir .iteration/ssot/10/

    # 强制覆盖已存在的快照
    python scripts/iteration/snapshot_ssot_iteration.py 10 --force

功能:
    1. 将 docs/acceptance/iteration_<N>_plan.md 复制到 .iteration/_export/<N>/plan.md
    2. 将 docs/acceptance/iteration_<N>_regression.md 复制到 .iteration/_export/<N>/regression.md
    3. 创建 README.md 说明文件，标注来源和只读性质
    4. 幂等操作：相同内容跳过，不同内容需要 --force

警告:
    ⚠️ 快照仅供本地阅读和实验，**不可用于 promote 覆盖旧编号**。
    SSOT 编号一旦使用即为永久占用，快照副本不能替代原始文件。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

# 项目根目录
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# SSOT 目录
SSOT_DIR = REPO_ROOT / "docs" / "acceptance"

# 默认快照输出目录
DEFAULT_EXPORT_DIR = REPO_ROOT / ".iteration" / "_export"


# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class SnapshotResult:
    """快照操作结果。"""

    success: bool
    message: str
    files_copied: List[str]
    files_skipped: List[str]
    readme_created: bool


class SourceNotFoundError(Exception):
    """当 SSOT 源文件不存在时抛出。"""

    def __init__(self, iteration_number: int, available: List[int]) -> None:
        self.iteration_number = iteration_number
        self.available = available
        super().__init__(
            f"Iteration {iteration_number} 不存在于 SSOT (docs/acceptance/)"
        )


class FileConflictError(Exception):
    """当目标文件已存在且内容不同时抛出。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"目标文件已存在且内容不同: {path}\n使用 --force 参数强制覆盖")


# ============================================================================
# 辅助函数
# ============================================================================


def get_ssot_iteration_numbers() -> List[int]:
    """获取 SSOT 中所有迭代编号（降序排列）。

    Returns:
        已在 SSOT 中的迭代编号列表（降序）
    """
    import re

    numbers: set[int] = set()
    pattern = re.compile(r"^iteration_(\d+)_(plan|regression)\.md$")

    if not SSOT_DIR.exists():
        return []

    for file_path in SSOT_DIR.iterdir():
        if file_path.is_file():
            match = pattern.match(file_path.name)
            if match:
                numbers.add(int(match.group(1)))

    return sorted(numbers, reverse=True)


def files_are_identical(file1: Path, file2: Path) -> bool:
    """检查两个文件内容是否相同。

    Args:
        file1: 第一个文件路径
        file2: 第二个文件路径

    Returns:
        True 如果内容相同，否则 False
    """
    if not file1.exists() or not file2.exists():
        return False

    return file1.read_text(encoding="utf-8") == file2.read_text(encoding="utf-8")


def get_snapshot_readme_content(iteration_number: int, source_dir: Path) -> str:
    """生成快照目录的 README.md 内容。

    Args:
        iteration_number: 迭代编号
        source_dir: SSOT 源目录

    Returns:
        README 内容
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 尝试获取相对路径，失败则使用原路径
    try:
        source_display = f"{source_dir.relative_to(REPO_ROOT)}/"
    except ValueError:
        source_display = f"{source_dir}/"

    return f"""# Iteration {iteration_number} 快照（只读副本）

> **⚠️ 警告：此目录为 SSOT 的只读快照，仅供本地阅读和实验。**

## 来源信息

| 属性 | 值 |
|------|-----|
| **迭代编号** | {iteration_number} |
| **快照时间** | {timestamp} |
| **SSOT 来源** | `{source_display}` |

## 文件列表

- `plan.md` - 来自 `docs/acceptance/iteration_{iteration_number}_plan.md`
- `regression.md` - 来自 `docs/acceptance/iteration_{iteration_number}_regression.md`

## 重要提醒

### ❌ 不可用于 promote

此快照**不可用于 promote 覆盖旧编号**。SSOT 编号一旦使用即为永久占用：

- Iteration {iteration_number} 已在 SSOT 中存在
- 不能通过修改此副本然后 promote 来"更新"原迭代
- 如需创建新迭代，请使用下一可用编号

### ✅ 正确用法

- **阅读参考**: 查阅历史迭代的计划和回归记录
- **本地实验**: 修改副本进行实验（不影响 SSOT）
- **模板参考**: 参考已完成迭代的结构编写新迭代

### 获取最新 SSOT

如需获取最新版本，请直接查阅 SSOT：

```bash
# 查看 SSOT 中的原始文件
cat docs/acceptance/iteration_{iteration_number}_plan.md
cat docs/acceptance/iteration_{iteration_number}_regression.md

# 重新快照（覆盖本地副本）
python scripts/iteration/snapshot_ssot_iteration.py {iteration_number} --force
```

---

_此文件由 `snapshot_ssot_iteration.py` 自动生成_
"""


# ============================================================================
# 核心快照逻辑
# ============================================================================


def snapshot_iteration(
    iteration_number: int,
    *,
    output_dir: Optional[Path] = None,
    force: bool = False,
) -> SnapshotResult:
    """将 SSOT 迭代文档快照到本地目录。

    Args:
        iteration_number: 要快照的迭代编号
        output_dir: 输出目录（默认 .iteration/_export/<N>/）
        force: 是否强制覆盖已存在的文件

    Returns:
        SnapshotResult 操作结果

    Raises:
        SourceNotFoundError: 如果 SSOT 中不存在该迭代
        FileConflictError: 如果目标文件已存在且内容不同（未使用 --force）
    """
    # 检查 SSOT 中是否存在该迭代
    available = get_ssot_iteration_numbers()
    if iteration_number not in available:
        raise SourceNotFoundError(iteration_number, available)

    # 确定源文件路径
    src_plan = SSOT_DIR / f"iteration_{iteration_number}_plan.md"
    src_regression = SSOT_DIR / f"iteration_{iteration_number}_regression.md"

    # 确定输出目录
    if output_dir is None:
        output_dir = DEFAULT_EXPORT_DIR / str(iteration_number)

    # 确定目标文件路径
    dst_plan = output_dir / "plan.md"
    dst_regression = output_dir / "regression.md"
    dst_readme = output_dir / "README.md"

    files_copied: List[str] = []
    files_skipped: List[str] = []
    readme_created = False

    # 创建输出目录
    output_dir.mkdir(parents=True, exist_ok=True)

    # 复制文件
    file_pairs = []
    if src_plan.exists():
        file_pairs.append((src_plan, dst_plan))
    if src_regression.exists():
        file_pairs.append((src_regression, dst_regression))

    for src, dst in file_pairs:
        if dst.exists():
            if files_are_identical(src, dst):
                files_skipped.append(str(dst.relative_to(REPO_ROOT)))
                continue
            elif not force:
                raise FileConflictError(dst)

        shutil.copy2(src, dst)
        files_copied.append(str(dst.relative_to(REPO_ROOT)))

    # 创建或更新 README.md
    readme_content = get_snapshot_readme_content(iteration_number, SSOT_DIR)
    if not dst_readme.exists() or force:
        dst_readme.write_text(readme_content, encoding="utf-8")
        readme_created = True

    return SnapshotResult(
        success=True,
        message=f"Iteration {iteration_number} 快照完成",
        files_copied=files_copied,
        files_skipped=files_skipped,
        readme_created=readme_created,
    )


# ============================================================================
# CLI 入口
# ============================================================================


def main() -> int:
    """主函数。"""
    parser = argparse.ArgumentParser(
        description="将 SSOT 迭代文档快照到本地目录，用于阅读和实验",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # 快照 Iteration 10 到默认目录
    python scripts/iteration/snapshot_ssot_iteration.py 10

    # 快照到自定义目录
    python scripts/iteration/snapshot_ssot_iteration.py 10 --output-dir .iteration/ssot/10/

    # 强制覆盖已存在的快照
    python scripts/iteration/snapshot_ssot_iteration.py 10 --force

    # 列出可用的迭代编号
    python scripts/iteration/snapshot_ssot_iteration.py --list

警告:
    ⚠️ 快照仅供本地阅读和实验，不可用于 promote 覆盖旧编号。
    SSOT 编号一旦使用即为永久占用。
        """,
    )

    # 迭代编号组：iteration_number 与 --list 互斥
    number_group = parser.add_mutually_exclusive_group(required=True)
    number_group.add_argument(
        "iteration_number",
        type=int,
        nargs="?",
        default=None,
        help="要快照的迭代编号",
    )
    number_group.add_argument(
        "--list",
        "-l",
        action="store_true",
        help="列出 SSOT 中可用的迭代编号",
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        type=str,
        default=None,
        help="输出目录（默认 .iteration/_export/<N>/）",
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="强制覆盖已存在的文件",
    )

    args = parser.parse_args()

    # 处理 --list
    if args.list:
        available = get_ssot_iteration_numbers()
        if not available:
            print("❌ SSOT 中没有任何迭代文档", file=sys.stderr)
            return 1

        print("📋 SSOT 中可用的迭代编号（降序）:")
        print()
        for n in available:
            plan_exists = (SSOT_DIR / f"iteration_{n}_plan.md").exists()
            regression_exists = (SSOT_DIR / f"iteration_{n}_regression.md").exists()
            files = []
            if plan_exists:
                files.append("plan")
            if regression_exists:
                files.append("regression")
            print(f"  - Iteration {n} ({', '.join(files)})")
        print()
        print("💡 使用 `python scripts/iteration/snapshot_ssot_iteration.py <N>` 快照指定迭代")
        return 0

    # 快照操作
    try:
        output_dir = Path(args.output_dir) if args.output_dir else None
        result = snapshot_iteration(
            args.iteration_number,
            output_dir=output_dir,
            force=args.force,
        )

        print(f"✅ Iteration {args.iteration_number} 快照完成")
        print()

        if result.files_copied:
            print("复制的文件:")
            for f in result.files_copied:
                print(f"  📄 {f}")

        if result.files_skipped:
            print("\n跳过的文件（内容相同）:")
            for f in result.files_skipped:
                print(f"  ✓ {f}")

        if result.readme_created:
            output_path = (
                Path(args.output_dir)
                if args.output_dir
                else DEFAULT_EXPORT_DIR / str(args.iteration_number)
            )
            readme_path = output_path / "README.md"
            print(f"\n📝 README 已创建: {readme_path.relative_to(REPO_ROOT)}")

        print()
        print("⚠️  重要提醒:")
        print("    此快照仅供本地阅读和实验，不可用于 promote 覆盖旧编号。")
        print("    SSOT 编号一旦使用即为永久占用。")

        return 0

    except SourceNotFoundError as e:
        print(f"❌ 错误: {e}", file=sys.stderr)
        print(file=sys.stderr)
        if e.available:
            print("SSOT 中可用的迭代编号:", file=sys.stderr)
            for n in e.available[:10]:  # 只显示前 10 个
                print(f"  - Iteration {n}", file=sys.stderr)
            if len(e.available) > 10:
                print(f"  ... 共 {len(e.available)} 个", file=sys.stderr)
        else:
            print("SSOT 中没有任何迭代文档", file=sys.stderr)
        return 1

    except FileConflictError as e:
        print(f"❌ 错误: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
