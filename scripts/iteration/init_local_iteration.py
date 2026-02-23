#!/usr/bin/env python3
"""初始化本地迭代草稿目录。

用法:
    python scripts/iteration/init_local_iteration.py <iteration_number>
    python scripts/iteration/init_local_iteration.py --next

示例:
    python scripts/iteration/init_local_iteration.py 4
    python scripts/iteration/init_local_iteration.py --next

功能:
    - 检测目标编号是否已在 docs/acceptance/ 中存在（SSOT 冲突检测）
    - 创建 .iteration/ 目录（如不存在）
    - 创建 .iteration/README.md（如不存在）
    - 创建 .iteration/<N>/plan.md（从模板填充）
    - 创建 .iteration/<N>/regression.md（从模板填充）
    - 支持 --next 参数自动选择下一可用编号
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# 项目根目录
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# 模板目录
TEMPLATES_DIR = REPO_ROOT / "docs" / "acceptance" / "_templates"

# SSOT 目录（docs/acceptance/）
SSOT_DIR = REPO_ROOT / "docs" / "acceptance"

# 本地迭代目录
ITERATION_DIR = REPO_ROOT / ".iteration"

# 兼容历史测试/脚本通过扁平模块名 monkeypatch（如 "init_local_iteration.SSOT_DIR"）。
sys.modules.setdefault("init_local_iteration", sys.modules[__name__])

# README 内容
README_CONTENT = """\
# .iteration/ 本地迭代草稿目录

此目录用于存放本地化的迭代计划草稿，**不纳入版本控制**。

## 目录结构

```
.iteration/
├── README.md           # 本文件
├── 4/                  # Iteration 4 草稿
│   ├── plan.md         # 迭代计划草稿
│   └── regression.md   # 回归记录草稿
└── ...
```

## 使用方法

### 初始化新迭代

```bash
python scripts/iteration/init_local_iteration.py <N>
```

### 晋升到 docs/acceptance/

当计划成熟后，将文件复制到 `docs/acceptance/` 并更新索引：

```bash
cp .iteration/<N>/plan.md docs/acceptance/iteration_<N>_plan.md
cp .iteration/<N>/regression.md docs/acceptance/iteration_<N>_regression.md
```

详细说明请参阅 [docs/dev/iteration_local_drafts.md](docs/dev/iteration_local_drafts.md)

---

_此文件由 scripts/iteration/init_local_iteration.py 自动生成_
"""


def get_ssot_iteration_numbers() -> set[int]:
    """扫描 docs/acceptance/ 获取已存在的迭代编号。

    Returns:
        已在 SSOT 中使用的迭代编号集合
    """
    numbers: set[int] = set()
    pattern = re.compile(r"^iteration_(\d+)_(plan|regression)\.md$")

    if not SSOT_DIR.exists():
        return numbers

    for file_path in SSOT_DIR.iterdir():
        if file_path.is_file():
            match = pattern.match(file_path.name)
            if match:
                numbers.add(int(match.group(1)))

    return numbers


def get_next_available_number() -> int:
    """获取下一个可用的迭代编号。

    Returns:
        当前最大编号 + 1，若无已存在编号则返回 1
    """
    existing = get_ssot_iteration_numbers()
    if not existing:
        return 1
    return max(existing) + 1


class SSOTConflictError(Exception):
    """当请求的迭代编号已在 SSOT 中存在时抛出。"""

    def __init__(self, iteration_number: int, suggested_number: int) -> None:
        self.iteration_number = iteration_number
        self.suggested_number = suggested_number
        super().__init__(f"Iteration {iteration_number} 已在 docs/acceptance/ 中存在（SSOT 冲突）")


def check_ssot_conflict(iteration_number: int) -> None:
    """检查迭代编号是否与 SSOT 冲突。

    Args:
        iteration_number: 要检查的迭代编号

    Raises:
        SSOTConflictError: 如果编号已在 SSOT 中存在
    """
    existing = get_ssot_iteration_numbers()
    if iteration_number in existing:
        suggested = get_next_available_number()
        raise SSOTConflictError(iteration_number, suggested)


def create_or_refresh_readme(*, force_refresh: bool = False) -> str:
    """创建或刷新 .iteration/README.md。

    Args:
        force_refresh: 是否强制刷新（覆盖已存在的文件）

    Returns:
        状态字符串: "created"（新创建）、"refreshed"（强制刷新）、"exists"（已存在未变更）
    """
    readme_path = ITERATION_DIR / "README.md"

    if readme_path.exists():
        if force_refresh:
            readme_path.write_text(README_CONTENT, encoding="utf-8")
            return "refreshed"
        return "exists"

    readme_path.write_text(README_CONTENT, encoding="utf-8")
    return "created"


def read_template(template_name: str) -> str:
    """读取模板文件内容。

    Args:
        template_name: 模板文件名

    Returns:
        模板文件内容

    Raises:
        FileNotFoundError: 如果模板文件不存在
    """
    template_path = TEMPLATES_DIR / template_name
    if not template_path.exists():
        raise FileNotFoundError(f"模板文件不存在: {template_path}")
    return template_path.read_text(encoding="utf-8")


def init_iteration(
    iteration_number: int, *, force: bool = False, refresh_readme: bool = False
) -> dict[str, str]:
    """初始化指定迭代的本地草稿目录。

    Args:
        iteration_number: 迭代编号
        force: 是否强制覆盖已存在的文件（同时刷新 README）
        refresh_readme: 是否强制刷新 README（即使已存在）

    Returns:
        创建的文件路径和状态的字典

    Raises:
        ValueError: 如果迭代编号无效
        SSOTConflictError: 如果编号已在 docs/acceptance/ 中存在
        FileExistsError: 如果目录已存在且 force=False
    """
    if iteration_number < 1:
        raise ValueError(f"迭代编号必须大于 0: {iteration_number}")

    # 检查是否与 SSOT 冲突（优先于本地目录检查）
    check_ssot_conflict(iteration_number)

    # 创建 .iteration/ 目录
    ITERATION_DIR.mkdir(parents=True, exist_ok=True)

    # 创建迭代子目录
    iteration_path = ITERATION_DIR / str(iteration_number)

    if iteration_path.exists() and not force:
        raise FileExistsError(f"迭代目录已存在: {iteration_path}\n使用 --force 参数强制覆盖")

    iteration_path.mkdir(parents=True, exist_ok=True)

    results: dict[str, str] = {}

    # 创建或刷新 README.md（--force 或 --refresh-readme 时强制刷新）
    readme_status = create_or_refresh_readme(force_refresh=force or refresh_readme)
    results[str(ITERATION_DIR / "README.md")] = readme_status

    # 读取模板
    plan_template = read_template("iteration_plan.template.md")
    regression_template = read_template("iteration_regression.template.md")

    # 创建 plan.md（先检查是否存在，再写入）
    plan_path = iteration_path / "plan.md"
    plan_existed = plan_path.exists()
    plan_path.write_text(plan_template, encoding="utf-8")
    results[str(plan_path)] = "overwritten" if plan_existed else "created"

    # 创建 regression.md（先检查是否存在，再写入）
    regression_path = iteration_path / "regression.md"
    regression_existed = regression_path.exists()
    regression_path.write_text(regression_template, encoding="utf-8")
    results[str(regression_path)] = "overwritten" if regression_existed else "created"

    return results


def main() -> int:
    """主函数。"""
    parser = argparse.ArgumentParser(
        description="初始化本地迭代草稿目录",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    python scripts/iteration/init_local_iteration.py 4
    python scripts/iteration/init_local_iteration.py 5 --force
    python scripts/iteration/init_local_iteration.py 5 --refresh-readme
    python scripts/iteration/init_local_iteration.py --next

详细说明请参阅 docs/dev/iteration_local_drafts.md
        """,
    )

    # 迭代编号组：iteration_number 与 --next 互斥
    number_group = parser.add_mutually_exclusive_group(required=True)
    number_group.add_argument(
        "iteration_number",
        type=int,
        nargs="?",
        default=None,
        help="迭代编号（正整数）",
    )
    number_group.add_argument(
        "--next",
        "-n",
        dest="use_next",
        action="store_true",
        help="自动选择下一可用编号（当前最大编号 + 1）",
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="强制覆盖已存在的文件（同时刷新 README）",
    )
    parser.add_argument(
        "--refresh-readme",
        action="store_true",
        help="强制刷新 .iteration/README.md（用于修复异常内容）",
    )

    args = parser.parse_args()

    # 确定迭代编号
    if args.use_next:
        iteration_number = get_next_available_number()
        print(f"📌 自动选择下一可用编号: {iteration_number}")
        print()
    else:
        iteration_number = args.iteration_number

    try:
        results = init_iteration(
            iteration_number, force=args.force, refresh_readme=args.refresh_readme
        )

        print(f"✅ Iteration {iteration_number} 本地草稿已初始化")
        print()
        print("创建的文件:")
        for path, status in results.items():
            rel_path = Path(path).relative_to(REPO_ROOT)
            if status == "created":
                status_icon = "📄"
            elif status in ("overwritten", "refreshed"):
                status_icon = "📝"
            else:
                status_icon = "✓"
            print(f"  {status_icon} {rel_path} ({status})")

        print()
        print("下一步:")
        print(f"  1. 编辑 .iteration/{iteration_number}/plan.md 起草迭代计划")
        print(f"  2. 编辑 .iteration/{iteration_number}/regression.md 记录回归测试")
        print("  3. 计划成熟后，参照 docs/dev/iteration_local_drafts.md 晋升到 docs/acceptance/")
        print()
        print("同步自动生成区块（晋升后执行）:")
        print(f"  python scripts/iteration/sync_iteration_regression.py {iteration_number} --write")

        return 0

    except SSOTConflictError as e:
        print(f"❌ 错误: {e}", file=sys.stderr)
        print(file=sys.stderr)
        print("SSOT 中已存在以下文件:", file=sys.stderr)
        plan_file = SSOT_DIR / f"iteration_{e.iteration_number}_plan.md"
        regression_file = SSOT_DIR / f"iteration_{e.iteration_number}_regression.md"
        if plan_file.exists():
            print(f"  - {plan_file.relative_to(REPO_ROOT)}", file=sys.stderr)
        if regression_file.exists():
            print(f"  - {regression_file.relative_to(REPO_ROOT)}", file=sys.stderr)
        print(file=sys.stderr)
        print(f"💡 建议: 使用下一可用编号 {e.suggested_number}", file=sys.stderr)
        print(
            f"   python scripts/iteration/init_local_iteration.py {e.suggested_number}",
            file=sys.stderr,
        )
        return 1
    except FileExistsError as e:
        print(f"❌ 错误: {e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"❌ 错误: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"❌ 错误: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
