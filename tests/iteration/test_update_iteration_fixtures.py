#!/usr/bin/env python3
"""
update_iteration_fixtures.py 测试

覆盖:
1. --all 生成的文件列表完整
2. 重复执行幂等（两次运行后文件内容不变）
3. 输出稳定性规则（LF、末尾换行、无三连空行）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# 添加脚本目录到 path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "iteration"))

from render_min_gate_block import SUPPORTED_PROFILES  # noqa: E402
from update_iteration_fixtures import (  # noqa: E402
    DEFAULT_EVIDENCE_SNAPSHOT_ITERATION,
    main,
)


def _write_evidence_json(evidence_dir: Path, iteration_number: int) -> Path:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "iteration_number": iteration_number,
        "recorded_at": "2026-02-02T12:00:00Z",
        "commit_sha": "deadbeef1234567890abcdef1234567890abcdef",
        "commands": [
            {
                "name": "lint",
                "command": "make lint",
                "result": "PASS",
                "summary": "ok",
                "duration_seconds": 1.2,
            }
        ],
        "overall_result": "PASS",
    }
    path = evidence_dir / f"iteration_{iteration_number}_evidence.json"
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    return path


def _write_v2_minimal(fixtures_root: Path) -> Path:
    fixtures_root.mkdir(parents=True, exist_ok=True)
    payload = {
        "iteration_number": DEFAULT_EVIDENCE_SNAPSHOT_ITERATION,
        "recorded_at": "2026-02-02T12:00:00Z",
        "commit_sha": "deadbeef1234567890abcdef1234567890abcdef",
        "overall_result": "PASS",
        "commands": [
            {
                "name": "ci",
                "command": "make ci",
                "result": "PASS",
                "summary": "ok",
                "duration_seconds": 1.0,
            }
        ],
        "links": {"ci_run_url": "https://example.com/ci/1"},
    }
    path = fixtures_root / "iteration_evidence_v2_minimal.json"
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    return path


def _run_all(monkeypatch, fixtures_root: Path, evidence_dir: Path) -> None:
    argv = [
        "update_iteration_fixtures.py",
        "--all",
        "--fixtures-root",
        str(fixtures_root),
        "--evidence-dir",
        str(evidence_dir),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    exit_code = main()
    assert exit_code == 0


def _expected_all_files() -> set[str]:
    min_gate_files = {f"render_min_gate_block/{profile}.md" for profile in SUPPORTED_PROFILES}
    other_files = {
        "sync_iteration_regression/expected_regression.md",
        "iteration_cycle/execute.md",
        "iteration_cycle/summary.md",
        "iteration_cycle/verify_report.md",
        "iteration_cycle/verify_regression_expected.md",
        "iteration_cycle_smoke/min_gate_block_docs_only.md",
        "iteration_cycle_smoke/evidence_snippet.md",
        f"render_iteration_evidence_snippet/iteration_{DEFAULT_EVIDENCE_SNAPSHOT_ITERATION}.md",
        "evidence_snippet_v2_snapshot.md",
    }
    return min_gate_files | other_files


def _read_output_files(fixtures_root: Path) -> dict[str, str]:
    outputs = {}
    for path in fixtures_root.rglob("*"):
        if not path.is_file():
            continue
        rel_path = path.relative_to(fixtures_root).as_posix()
        if rel_path == "iteration_evidence_v2_minimal.json":
            continue
        outputs[rel_path] = path.read_text(encoding="utf-8")
    return outputs


def test_all_generates_complete_file_list(tmp_path, monkeypatch):
    fixtures_root = tmp_path / "fixtures"
    evidence_dir = tmp_path / "evidence"
    _write_v2_minimal(fixtures_root)
    _write_evidence_json(evidence_dir, DEFAULT_EVIDENCE_SNAPSHOT_ITERATION)

    _run_all(monkeypatch, fixtures_root, evidence_dir)

    actual = set(_read_output_files(fixtures_root).keys())
    assert actual == _expected_all_files()


def test_all_is_idempotent(tmp_path, monkeypatch):
    fixtures_root = tmp_path / "fixtures"
    evidence_dir = tmp_path / "evidence"
    _write_v2_minimal(fixtures_root)
    _write_evidence_json(evidence_dir, DEFAULT_EVIDENCE_SNAPSHOT_ITERATION)

    _run_all(monkeypatch, fixtures_root, evidence_dir)
    first_snapshot = _read_output_files(fixtures_root)

    _run_all(monkeypatch, fixtures_root, evidence_dir)
    second_snapshot = _read_output_files(fixtures_root)

    assert first_snapshot == second_snapshot


def test_all_outputs_follow_stability_rules(tmp_path, monkeypatch):
    fixtures_root = tmp_path / "fixtures"
    evidence_dir = tmp_path / "evidence"
    _write_v2_minimal(fixtures_root)
    _write_evidence_json(evidence_dir, DEFAULT_EVIDENCE_SNAPSHOT_ITERATION)

    _run_all(monkeypatch, fixtures_root, evidence_dir)

    for rel_path in _expected_all_files():
        content = (fixtures_root / rel_path).read_text(encoding="utf-8")
        assert "\r" not in content
        assert content.endswith("\n")
        assert "\n\n\n\n" not in content
