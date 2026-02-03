#!/usr/bin/env python3
"""
iteration_cycle_smoke tests.

Coverage:
1. evidence_snippet render output does not leak sensitive prefixes.
2. iteration_cycle CLI outputs suggested commands.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# Add scripts directory to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "iteration"))

from render_iteration_evidence_snippet import (  # noqa: E402
    parse_evidence_data,
    render_evidence_snippet,
)


def _write_drift_map(path: Path) -> None:
    payload = {
        "rules": [
            {
                "id": "docs",
                "description": "Docs change",
                "triggers": {"prefixes": ["docs/acceptance/"], "globs": []},
                "actions": {
                    "fixture_refresh_commands": ["make refresh-docs"],
                    "minimal_tests": ["pytest tests/iteration/test_iteration_cycle_smoke.py -q"],
                    "minimal_gates": ["make check-iteration-docs"],
                },
            }
        ]
    }
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def test_evidence_snippet_redacts_sensitive_prefixes() -> None:
    evidence = {
        "iteration_number": 21,
        "recorded_at": "2026-02-02T12:30:00Z",
        "commit_sha": "feedface1234567890abcdef1234567890abcdef",
        "commands": [
            {
                "name": "ci",
                "command": "psql postgresql://user:pass@localhost/db",
                "result": "FAIL",
                "summary": "Auth failed for glpat-secret-123",
                "duration_seconds": 12.3,
            }
        ],
        "overall_result": "FAIL",
        "notes": "Bearer super-secret-token should not leak",
    }

    parsed = parse_evidence_data(evidence)
    result = render_evidence_snippet(parsed, "iteration_21_evidence.json")

    assert "glpat-" not in result
    assert "postgresql://" not in result
    assert "Bearer " not in result


def test_iteration_cycle_cli_outputs_suggested_commands(tmp_path: Path) -> None:
    drift_map_path = tmp_path / "drift_map.json"
    _write_drift_map(drift_map_path)

    repo_root = Path(__file__).parent.parent.parent
    cli_path = repo_root / "scripts" / "iteration" / "iteration_cycle.py"

    result = subprocess.run(
        [
            sys.executable,
            str(cli_path),
            "--paths",
            "docs/acceptance/iteration_15_plan.md",
            "--drift-map",
            str(drift_map_path),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["fixture_refresh_commands"] == ["make refresh-docs"]
    assert payload["minimal_tests"] == ["pytest tests/iteration/test_iteration_cycle_smoke.py -q"]
    assert payload["minimal_gates"] == ["make check-iteration-docs"]
