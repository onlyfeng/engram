#!/usr/bin/env python3
"""
Workflow contract drift report schema tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml
from jsonschema import validate

from scripts.ci.workflow_contract_drift_report import (
    DRIFT_REPORT_SCHEMA_VERSION,
    WorkflowContractDriftAnalyzer,
    format_json_output,
)


def _find_schema_path() -> Path:
    """Find schema file path across execution contexts."""
    current = Path(__file__).resolve().parent
    for _ in range(10):
        candidate = current / "schemas" / "workflow_contract_drift_report_v2.schema.json"
        if candidate.exists():
            return candidate
        current = current.parent
    return candidate


SCHEMA_PATH = _find_schema_path()


def load_schema() -> Dict[str, Any]:
    """Load workflow_contract_drift_report_v2 schema."""
    if not SCHEMA_PATH.exists():
        pytest.skip(f"Schema file not found: {SCHEMA_PATH}")
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def write_contract(workspace: Path, contract: dict[str, Any]) -> Path:
    """Write contract JSON file."""
    scripts_ci = workspace / "scripts" / "ci"
    scripts_ci.mkdir(parents=True, exist_ok=True)
    contract_path = scripts_ci / "workflow_contract.v2.json"
    contract_path.write_text(json.dumps(contract, indent=2), encoding="utf-8")
    return contract_path


def write_workflow(workspace: Path, name: str, workflow: dict[str, Any]) -> Path:
    """Write workflow YAML file."""
    workflows_dir = workspace / ".github" / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    workflow_path = workflows_dir / f"{name}.yml"
    workflow_path.write_text(yaml.safe_dump(workflow), encoding="utf-8")
    return workflow_path


@pytest.fixture
def temp_workspace(tmp_path: Path) -> Path:
    """Create a temporary workspace layout."""
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    return tmp_path


class TestWorkflowContractDriftReportSchema:
    """Drift report schema validation."""

    def test_drift_report_output_matches_schema(self, temp_workspace: Path) -> None:
        """Generate drift report and validate against schema."""
        schema = load_schema()

        contract = {
            "version": "2.24.0",
            "last_updated": "2026-02-03",
            "ci": {
                "file": ".github/workflows/ci.yml",
                "job_ids": ["lint"],
            },
        }
        write_contract(temp_workspace, contract)

        workflow = {
            "name": "CI",
            "jobs": {
                "lint": {"name": "Lint", "steps": []},
                "deploy": {"name": "Deploy", "steps": []},
            },
        }
        write_workflow(temp_workspace, "ci", workflow)

        analyzer = WorkflowContractDriftAnalyzer(
            contract_path=temp_workspace / "scripts" / "ci" / "workflow_contract.v2.json",
            workspace_root=temp_workspace,
        )
        report = analyzer.analyze()
        data = json.loads(format_json_output(report))

        assert data["schema_version"] == DRIFT_REPORT_SCHEMA_VERSION
        validate(instance=data, schema=schema)
