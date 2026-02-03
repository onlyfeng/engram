"""Engram iteration CLI entrypoints."""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path
from typing import Sequence

_RERUN_ADVICE_RELATIVE = Path("scripts") / "iteration" / "rerun_advice.py"


def _find_repo_root(start: Path | None = None) -> Path | None:
    start = (start or Path.cwd()).resolve()
    for candidate in [start, *start.parents]:
        if (candidate / _RERUN_ADVICE_RELATIVE).exists():
            return candidate
    return None


def _load_rerun_advice_module(repo_root: Path):
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    try:
        return importlib.import_module("scripts.iteration.rerun_advice")
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Cannot import scripts.iteration.rerun_advice. "
            "Run from the repository root or ensure PYTHONPATH includes it."
        ) from exc


def _run_rerun_advice(argv: Sequence[str]) -> int:
    repo_root = _find_repo_root()
    if repo_root is None:
        print(
            "Cannot locate repository root. "
            "Expected scripts/iteration/rerun_advice.py under the current tree.",
            file=sys.stderr,
        )
        return 1

    try:
        module = _load_rerun_advice_module(repo_root)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    original_argv = sys.argv
    try:
        sys.argv = [original_argv[0], *argv]
        try:
            result = module.main()
        except SystemExit as exc:
            return int(exc.code) if isinstance(exc.code, int) else 0
        return int(result) if isinstance(result, int) else 0
    finally:
        sys.argv = original_argv


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="engram-iteration",
        description="Engram iteration tooling.",
        epilog="Available subcommands: rerun-advice",
    )
    parser.add_argument("command", nargs="?", help="Subcommand to run.")
    parser.add_argument("args", nargs=argparse.REMAINDER)

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command != "rerun-advice":
        print(f"Unknown subcommand: {args.command}", file=sys.stderr)
        parser.print_help()
        return 2

    return _run_rerun_advice(args.args)


if __name__ == "__main__":
    sys.exit(main())
