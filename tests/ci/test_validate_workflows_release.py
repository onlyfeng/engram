#!/usr/bin/env python3
"""
Workflow Contract Validator - Release workflow 单元测试

覆盖功能:
1. release workflow 被发现并校验
2. release workflow artifact_archive 缺失检测
"""

import json
import tempfile
from pathlib import Path

import yaml

from scripts.ci.validate_workflows import WorkflowContractValidator


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2))


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def _build_release_contract() -> dict:
    return {
        "version": "0.1.0",
        "last_updated": "2026-02-03",
        "release": {
            "file": ".github/workflows/release.yml",
            "job_ids": ["build", "publish", "notify"],
            "job_names": [
                "Build Release",
                "Publish Release Artifacts",
                "Notify Release",
            ],
            "required_jobs": [
                {
                    "id": "build",
                    "name": "Build Release",
                    "required_steps": [
                        "Checkout repository",
                        "Set up Python",
                        "Install dependencies",
                        "Build package",
                        "Upload release artifacts",
                    ],
                },
                {
                    "id": "publish",
                    "name": "Publish Release Artifacts",
                    "required_steps": [
                        "Download release artifacts",
                        "Publish release artifacts",
                    ],
                },
                {
                    "id": "notify",
                    "name": "Notify Release",
                    "required_steps": ["Notify release status"],
                },
            ],
            "artifact_archive": {
                "required_artifact_paths": ["dist/*.whl", "dist/*.tar.gz"],
                "artifact_step_names": ["Upload release artifacts"],
            },
        },
    }


def _build_release_workflow(upload_paths: list[str]) -> dict:
    return {
        "name": "Release",
        "on": {
            "workflow_dispatch": {
                "inputs": {
                    "publish": {
                        "type": "boolean",
                        "required": False,
                        "default": False,
                    }
                }
            }
        },
        "jobs": {
            "build": {
                "name": "Build Release",
                "runs-on": "ubuntu-latest",
                "steps": [
                    {"name": "Checkout repository", "uses": "actions/checkout@v4"},
                    {"name": "Set up Python", "uses": "actions/setup-python@v5"},
                    {"name": "Install dependencies", "run": "python -m pip install build"},
                    {"name": "Build package", "run": "python -m build"},
                    {
                        "name": "Upload release artifacts",
                        "uses": "actions/upload-artifact@v4",
                        "with": {
                            "name": "release-artifacts",
                            "path": "\n".join(upload_paths),
                        },
                    },
                ],
            },
            "publish": {
                "name": "Publish Release Artifacts",
                "runs-on": "ubuntu-latest",
                "needs": "build",
                "steps": [
                    {
                        "name": "Download release artifacts",
                        "uses": "actions/download-artifact@v4",
                        "with": {"name": "release-artifacts", "path": "dist"},
                    },
                    {"name": "Publish release artifacts", "run": "echo publish"},
                ],
            },
            "notify": {
                "name": "Notify Release",
                "runs-on": "ubuntu-latest",
                "needs": ["build", "publish"],
                "steps": [
                    {"name": "Notify release status", "run": "echo notify"},
                ],
            },
        },
    }


def _run_validator(workspace: Path, contract: dict) -> WorkflowContractValidator:
    contract_path = workspace / "workflow_contract.v2.json"
    _write_json(contract_path, contract)
    validator = WorkflowContractValidator(contract_path, workspace)
    assert validator.load_contract() is True
    validator.validate()
    return validator


def test_release_workflow_validates_successfully() -> None:
    with tempfile.TemporaryDirectory(prefix="test_release_workflow_") as tmpdir:
        workspace = Path(tmpdir)
        (workspace / ".github" / "workflows").mkdir(parents=True)

        workflow_path = workspace / ".github" / "workflows" / "release.yml"
        workflow = _build_release_workflow(["dist/*.whl", "dist/*.tar.gz"])
        _write_yaml(workflow_path, workflow)

        validator = _run_validator(workspace, _build_release_contract())
        result = validator.result

        assert result.success is True
        assert "release" in result.validated_workflows
        assert result.errors == []


def test_release_workflow_missing_artifact_paths() -> None:
    with tempfile.TemporaryDirectory(prefix="test_release_workflow_") as tmpdir:
        workspace = Path(tmpdir)
        (workspace / ".github" / "workflows").mkdir(parents=True)

        workflow_path = workspace / ".github" / "workflows" / "release.yml"
        workflow = _build_release_workflow(["dist/*.zip"])
        _write_yaml(workflow_path, workflow)

        validator = _run_validator(workspace, _build_release_contract())
        result = validator.result

        assert result.success is False
        assert any(error.error_type == "missing_artifact_path" for error in result.errors)
        assert any(error.workflow == "release" for error in result.errors)
