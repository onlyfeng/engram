#!/usr/bin/env python3
"""
rerun_advice tests.

Coverage:
1. prefix and glob matches
2. Windows path separator normalization
3. absolute path conversion to repo-relative
4. dedupe with stable ordering across multiple rules
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Add scripts directory to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "iteration"))

from iteration_cycle import REPO_ROOT, collect_rerun_advice  # noqa: E402


def _write_drift_map(path: Path) -> None:
    payload = {
        "rules": [
            {
                "id": "docs-prefix",
                "description": "Docs prefix rule",
                "triggers": {"prefixes": ["docs/acceptance/"], "globs": []},
                "actions": {
                    "fixture_refresh_commands": [
                        "make refresh-docs",
                        "make refresh-common",
                    ],
                    "minimal_tests": ["pytest tests/iteration/test_rerun_advice.py"],
                    "minimal_gates": ["make check-iteration-docs"],
                },
            },
            {
                "id": "iteration-glob",
                "description": "Iteration glob rule",
                "triggers": {"prefixes": [], "globs": ["scripts/iteration/*.py"]},
                "actions": {
                    "fixture_refresh_commands": [
                        "make refresh-common",
                        "make refresh-scripts",
                    ],
                    "minimal_tests": [
                        "pytest tests/iteration/test_iteration_cycle_smoke.py",
                        "pytest tests/iteration/test_rerun_advice.py",
                    ],
                    "minimal_gates": ["make lint", "make check-iteration-docs"],
                },
            },
            {
                "id": "iteration-prefix",
                "description": "Iteration prefix rule",
                "triggers": {"prefixes": ["scripts/iteration/"], "globs": []},
                "actions": {
                    "fixture_refresh_commands": ["make refresh-scripts"],
                    "minimal_tests": ["pytest tests/iteration/test_iteration_cycle_smoke.py"],
                    "minimal_gates": ["make lint"],
                },
            },
        ]
    }
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def test_collect_rerun_advice_normalizes_paths_and_dedupes(tmp_path: Path) -> None:
    drift_map_path = tmp_path / "drift_map.json"
    _write_drift_map(drift_map_path)

    windows_repo_root = "C:\\" + REPO_ROOT.as_posix().lstrip("/").replace("/", "\\")
    changed_paths = [
        f"{windows_repo_root}\\docs\\acceptance\\iteration_15_plan.md",
        r"scripts\iteration\iteration_cycle.py",
    ]

    advice = collect_rerun_advice(changed_paths, drift_map_path=drift_map_path)

    assert advice["issues"] == []
    suggested = advice["suggested_commands"]

    assert suggested["fixture_refresh_commands"] == [
        "make refresh-docs",
        "make refresh-common",
        "make refresh-scripts",
    ]
    assert suggested["minimal_tests"] == [
        "pytest tests/iteration/test_rerun_advice.py",
        "pytest tests/iteration/test_iteration_cycle_smoke.py",
    ]
    assert suggested["minimal_gates"] == [
        "make check-iteration-docs",
        "make lint",
    ]
