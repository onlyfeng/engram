#!/usr/bin/env python3
"""根据变更路径生成最小重跑建议。

用法:
    python scripts/iteration/rerun_advice.py
    python scripts/iteration/rerun_advice.py --git-range origin/master...HEAD
    python scripts/iteration/rerun_advice.py --staged
    python scripts/iteration/rerun_advice.py --worktree
    python scripts/iteration/rerun_advice.py --paths scripts/iteration/iteration_cycle.py
    python scripts/iteration/rerun_advice.py --paths docs/acceptance/iteration_15_plan.md --format json
    python scripts/iteration/rerun_advice.py --paths docs/acceptance/iteration_15_plan.md --drift-map-path /tmp/drift_map.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Sequence

from scripts.iteration.iteration_cycle import (
    DRIFT_MAP_PATH,
    collect_rerun_advice,
    format_rerun_advice_markdown,
)

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent


def _dedupe(paths: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for path in paths:
        if not path:
            continue
        if path in seen:
            continue
        seen.add(path)
        result.append(path)
    return result


def _run_git_diff(args: Sequence[str]) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", *args],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError(f"无法运行 git diff: {exc}") from exc
    if result.returncode != 0:
        stderr = result.stderr.strip()
        suffix = f": {stderr}" if stderr else ""
        raise RuntimeError(f"git diff 失败{suffix}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _collect_changed_paths(args: argparse.Namespace) -> list[str]:
    if args.paths:
        return list(args.paths)

    collected: list[str] = []
    if args.staged:
        collected.extend(_run_git_diff(["--staged"]))
    if args.worktree:
        collected.extend(_run_git_diff([]))
    if not args.staged and not args.worktree:
        collected.extend(_run_git_diff([args.git_range]))

    return _dedupe(collected)


def _print_no_advice() -> None:
    print("无建议")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="根据变更路径生成最小重跑建议。",
    )
    parser.add_argument(
        "--git-range",
        default="origin/master...HEAD",
        help="git diff 范围（默认: origin/master...HEAD）",
    )
    parser.add_argument(
        "--staged",
        action="store_true",
        help="仅基于已暂存的变更",
    )
    parser.add_argument(
        "--worktree",
        action="store_true",
        help="仅基于工作区未暂存的变更",
    )
    parser.add_argument(
        "--paths",
        nargs="+",
        help="显式指定路径列表（优先级最高）",
    )
    parser.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="输出格式（markdown 或 json）",
    )
    parser.add_argument(
        "--drift-map-path",
        type=Path,
        default=None,
        help="覆盖 drift map 配置路径",
    )

    args = parser.parse_args()

    try:
        changed_paths = _collect_changed_paths(args)
    except RuntimeError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1

    drift_map_path = args.drift_map_path or DRIFT_MAP_PATH
    try:
        advice = collect_rerun_advice(
            changed_paths,
            allow_suggested_commands=True,
            drift_map_path=drift_map_path,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"❌ Drift map 解析失败: {exc}", file=sys.stderr)
        print(
            f"修复提示: 请检查并修复 drift map 文件: {drift_map_path}",
            file=sys.stderr,
        )
        return 1

    suggested = advice.get("suggested_commands")
    if not isinstance(suggested, dict) or not any(suggested.values()):
        _print_no_advice()
        return 0

    if args.format == "json":
        print(json.dumps(suggested, ensure_ascii=False, indent=2))
    else:
        print(format_rerun_advice_markdown(suggested))
    return 0


if __name__ == "__main__":
    sys.exit(main())
