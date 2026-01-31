#!/usr/bin/env python3
"""
mypy 基线对比包装脚本（已弃用）

⚠️ 此脚本已弃用，请使用 check_mypy_gate.py 替代。

迁移指南：
    旧命令                                      新命令
    ─────────────────────────────────────────────────────────────────────
    python scripts/ci/run_mypy_with_baseline.py
        → python scripts/ci/check_mypy_gate.py --gate baseline
        → make typecheck-gate

    python scripts/ci/run_mypy_with_baseline.py --update-baseline
        → python scripts/ci/check_mypy_gate.py --write-baseline
        → make mypy-baseline-update

    python scripts/ci/run_mypy_with_baseline.py --diff-only
        → python scripts/ci/check_mypy_gate.py --verbose

    python scripts/ci/run_mypy_with_baseline.py --verbose
        → python scripts/ci/check_mypy_gate.py --verbose

文档参考：docs/dev/mypy_baseline.md
"""

from __future__ import annotations

import subprocess
import sys
import warnings
from pathlib import Path


def main() -> int:
    """薄封装：调用 check_mypy_gate.py，保持向后兼容。"""
    # 输出弃用警告
    warnings.warn(
        "run_mypy_with_baseline.py 已弃用，请使用 check_mypy_gate.py 替代。\n"
        "迁移指南：\n"
        "  旧: python scripts/ci/run_mypy_with_baseline.py\n"
        "  新: python scripts/ci/check_mypy_gate.py --gate baseline\n"
        "  或: make typecheck-gate\n"
        "详见: docs/dev/mypy_baseline.md",
        DeprecationWarning,
        stacklevel=2,
    )

    # 打印弃用提示到 stderr（确保用户看到）
    print(
        "\n"
        "╔════════════════════════════════════════════════════════════════════╗\n"
        "║ ⚠️  弃用警告                                                        ║\n"
        "║                                                                    ║\n"
        "║ run_mypy_with_baseline.py 已弃用，请使用 check_mypy_gate.py        ║\n"
        "║                                                                    ║\n"
        "║ 迁移指南：                                                          ║\n"
        "║   旧: python scripts/ci/run_mypy_with_baseline.py                  ║\n"
        "║   新: python scripts/ci/check_mypy_gate.py --gate baseline         ║\n"
        "║   或: make typecheck-gate                                          ║\n"
        "║                                                                    ║\n"
        "║ 详见: docs/dev/mypy_baseline.md                                    ║\n"
        "╚════════════════════════════════════════════════════════════════════╝\n",
        file=sys.stderr,
    )

    # 构建新命令
    script_dir = Path(__file__).parent
    new_script = script_dir / "check_mypy_gate.py"

    # 参数映射
    new_args = [sys.executable, str(new_script)]

    # 解析旧参数并映射到新参数
    old_args = sys.argv[1:]
    has_update_baseline = "--update-baseline" in old_args
    has_diff_only = "--diff-only" in old_args
    has_verbose = "--verbose" in old_args or "-v" in old_args

    if has_update_baseline:
        new_args.append("--write-baseline")
    elif has_diff_only:
        # --diff-only 在新脚本中用 --verbose 模拟（只显示不退出）
        new_args.extend(["--gate", "baseline", "--verbose"])
        print(
            "[INFO] --diff-only 已映射为 --gate baseline --verbose",
            file=sys.stderr,
        )
    else:
        new_args.extend(["--gate", "baseline"])

    if has_verbose and not has_diff_only:
        new_args.append("--verbose")

    # 执行新脚本
    print(f"[INFO] 执行: {' '.join(new_args)}", file=sys.stderr)
    print("", file=sys.stderr)

    result = subprocess.run(new_args)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
