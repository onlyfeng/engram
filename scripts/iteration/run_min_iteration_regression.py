#!/usr/bin/env python3
"""Run minimal iteration regression commands by change type.

Usage:
    python scripts/iteration/run_min_iteration_regression.py profiles
    python scripts/iteration/run_min_iteration_regression.py profiles blocks
    python scripts/iteration/run_min_iteration_regression.py all
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent


@dataclass(frozen=True)
class ChangePlan:
    pytest_commands: tuple[str, ...]
    gate_commands: tuple[str, ...]
    description: str


CHANGE_PLANS: dict[str, ChangePlan] = {
    "profiles": ChangePlan(
        pytest_commands=("pytest tests/iteration/test_render_min_gate_block.py -q",),
        gate_commands=("make check-iteration-docs",),
        description="Gate profile or min-gate block changes.",
    ),
    "blocks": ChangePlan(
        pytest_commands=("pytest tests/iteration/test_sync_iteration_regression.py -q",),
        gate_commands=("make check-iteration-docs",),
        description="Generated blocks or regression sync changes.",
    ),
    "evidence": ChangePlan(
        pytest_commands=("pytest tests/iteration/test_render_iteration_evidence_snippet.py -q",),
        gate_commands=("make check-iteration-evidence",),
        description="Evidence snippet rendering or evidence data changes.",
    ),
    "schema": ChangePlan(
        pytest_commands=("pytest tests/iteration/test_render_iteration_evidence_snippet.py -q",),
        gate_commands=("make check-iteration-evidence",),
        description="Evidence schema changes.",
    ),
    "cycle": ChangePlan(
        pytest_commands=("pytest tests/iteration/test_update_iteration_fixtures.py -q",),
        gate_commands=("make check-iteration-docs",),
        description="Iteration cycle or fixture refresh pipeline changes.",
    ),
}

ALLOWED_TYPES = tuple(CHANGE_PLANS.keys())


def _merge_unique(target: list[str], additions: Iterable[str], seen: set[str]) -> None:
    for item in additions:
        if item in seen:
            continue
        target.append(item)
        seen.add(item)


def _collect_commands(change_types: Iterable[str]) -> tuple[list[str], list[str]]:
    selected = list(change_types)
    if "all" in selected:
        selected = list(ALLOWED_TYPES)

    pytest_commands: list[str] = []
    gate_commands: list[str] = []
    seen_pytest: set[str] = set()
    seen_gate: set[str] = set()

    for change_type in selected:
        plan = CHANGE_PLANS[change_type]
        _merge_unique(pytest_commands, plan.pytest_commands, seen_pytest)
        _merge_unique(gate_commands, plan.gate_commands, seen_gate)

    return pytest_commands, gate_commands


def _print_plan(change_types: Iterable[str], pytest_commands: list[str], gate_commands: list[str]) -> None:
    change_types_str = ", ".join(change_types)
    print(f"Selected change types: {change_types_str}")
    if pytest_commands:
        print("Pytest commands:")
        for cmd in pytest_commands:
            print(f"  - {cmd}")
    if gate_commands:
        print("Make targets:")
        for cmd in gate_commands:
            print(f"  - {cmd}")


def _run_command(command: str) -> bool:
    print(f"[RUN] {command}")
    try:
        result = subprocess.run(
            shlex.split(command),
            cwd=REPO_ROOT,
            check=False,
        )
    except OSError as exc:
        print(f"[ERROR] failed to run: {command}: {exc}", file=sys.stderr)
        return False
    if result.returncode != 0:
        print(f"[ERROR] command failed ({result.returncode}): {command}", file=sys.stderr)
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run minimal iteration regression commands by change type.",
    )
    parser.add_argument(
        "change_types",
        nargs="+",
        choices=ALLOWED_TYPES + ("all",),
        help=f"Change types: {', '.join(ALLOWED_TYPES)} (or 'all')",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands without running them.",
    )

    args = parser.parse_args()
    change_types = list(args.change_types)

    pytest_commands, gate_commands = _collect_commands(change_types)
    if not pytest_commands and not gate_commands:
        print("[WARN] No commands selected.")
        return 0

    _print_plan(change_types, pytest_commands, gate_commands)
    if args.dry_run:
        return 0

    for command in pytest_commands:
        if not _run_command(command):
            return 1

    for command in gate_commands:
        if not _run_command(command):
            return 1

    print("[OK] All commands passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
