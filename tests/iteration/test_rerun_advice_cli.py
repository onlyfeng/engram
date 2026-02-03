#!/usr/bin/env python3
"""rerun_advice CLI tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "iteration" / "rerun_advice.py"


def _write_drift_map(path: Path) -> None:
    payload = {
        "rules": [
            {
                "id": "docs-prefix",
                "description": "Docs prefix rule",
                "triggers": {"prefixes": ["docs/acceptance/"], "globs": []},
                "actions": {
                    "fixture_refresh_commands": ["make refresh-docs"],
                    "minimal_tests": ["pytest tests/iteration/test_rerun_advice_cli.py"],
                    "minimal_gates": ["make check-iteration-docs"],
                },
            },
            {
                "id": "scripts-prefix",
                "description": "Scripts prefix rule",
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


def _run_cli(args: list[str]) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{REPO_ROOT}{os.pathsep}{existing}" if existing else str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=15,
    )


def test_rerun_advice_cli_json_output(tmp_path: Path) -> None:
    drift_map_path = tmp_path / "drift_map.json"
    _write_drift_map(drift_map_path)

    result = _run_cli(
        [
            "--paths",
            "docs/acceptance/iteration_15_plan.md",
            "--format",
            "json",
            "--drift-map-path",
            str(drift_map_path),
        ]
    )

    assert result.returncode == 0, f"stderr: {result.stderr}"
    data = json.loads(result.stdout)
    assert isinstance(data, dict)
    assert data["fixture_refresh_commands"] == ["make refresh-docs"]
    assert data["minimal_tests"] == ["pytest tests/iteration/test_rerun_advice_cli.py"]
    assert data["minimal_gates"] == ["make check-iteration-docs"]


def test_rerun_advice_cli_markdown_output(tmp_path: Path) -> None:
    drift_map_path = tmp_path / "drift_map.json"
    _write_drift_map(drift_map_path)

    result = _run_cli(
        [
            "--paths",
            "docs/acceptance/iteration_15_plan.md",
            "--drift-map-path",
            str(drift_map_path),
        ]
    )

    assert result.returncode == 0, f"stderr: {result.stderr}"
    output = result.stdout
    assert "Suggested rerun commands:" in output
    assert "- fixture_refresh_commands:" in output
    assert "- minimal_tests:" in output
    assert "- minimal_gates:" in output


@pytest.mark.parametrize("content", [None, "{not-json"])
def test_rerun_advice_cli_invalid_drift_map(tmp_path: Path, content: str | None) -> None:
    drift_map_path = tmp_path / "broken.json"
    if content is not None:
        drift_map_path.write_text(content, encoding="utf-8")

    result = _run_cli(
        [
            "--paths",
            "docs/acceptance/iteration_15_plan.md",
            "--drift-map-path",
            str(drift_map_path),
        ]
    )

    combined = result.stderr + result.stdout
    assert result.returncode != 0
    assert str(drift_map_path) in combined
