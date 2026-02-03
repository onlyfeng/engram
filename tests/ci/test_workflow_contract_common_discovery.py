#!/usr/bin/env python3
"""
tests/ci/test_workflow_contract_common_discovery.py

聚焦 discover_workflow_keys 的边界测试：
1. 最小 contract（含 metadata）只识别 ci/nightly
2. 额外 metadata（不含 file）不被识别
3. 新增 release（含 file）应被识别
"""

from __future__ import annotations

from scripts.ci.workflow_contract_common import discover_workflow_keys


def test_discover_minimal_contract_with_metadata() -> None:
    """最小 contract（含 metadata）应只识别 ci/nightly"""
    contract = {
        "version": "1.0.0",
        "description": "Minimal contract for discovery",
        "last_updated": "2026-02-03",
        "ci": {"file": ".github/workflows/ci.yml"},
        "nightly": {"file": ".github/workflows/nightly.yml"},
    }

    result = discover_workflow_keys(contract)

    assert result == ["ci", "nightly"]


def test_discover_ignores_extra_metadata_without_file() -> None:
    """额外 metadata（无 file）不应被识别为 workflow"""
    contract = {
        "version": "1.0.0",
        "ci": {"file": ".github/workflows/ci.yml"},
        "nightly": {"file": ".github/workflows/nightly.yml"},
        "workflow_policy": {
            "enforce": True,
            "rules": ["must_have_jobs"],
        },
        "artifact_policy": {
            "allowlist": ["artifacts/*.json"],
        },
    }

    result = discover_workflow_keys(contract)

    assert result == ["ci", "nightly"]


def test_discover_includes_release_with_file() -> None:
    """新增 release（含 file）应被识别"""
    contract = {
        "version": "1.0.0",
        "ci": {"file": ".github/workflows/ci.yml"},
        "nightly": {"file": ".github/workflows/nightly.yml"},
        "release": {"file": ".github/workflows/release.yml"},
    }

    result = discover_workflow_keys(contract)

    assert result == ["ci", "nightly", "release"]
