#!/usr/bin/env python3
"""Run `make ci` equivalent checks without requiring GNU make."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

CI_TARGET_ORDER: tuple[str, ...] = (
    "lint",
    "format-check",
    "typecheck-gate",
    "typecheck-strict-island",
    "mypy-metrics",
    "check-mypy-metrics-thresholds",
    "check-schemas",
    "check-env-consistency",
    "check-logbook-consistency",
    "check-migration-sanity",
    "check-scm-sync-consistency",
    "check-gateway-error-reason-usage",
    "check-gateway-public-api-surface",
    "check-gateway-public-api-docs-sync",
    "check-gateway-di-boundaries",
    "check-gateway-import-surface",
    "check-gateway-correlation-id-single-source",
    "check-iteration-docs",
    "check-iteration-fixtures-freshness",
    "check-iteration-toolchain-drift-map-contract",
    "validate-workflows-strict",
    "check-workflow-contract-docs-sync",
    "check-workflow-contract-error-types-docs-sync",
    "check-workflow-contract-version-policy",
    "check-workflow-contract-internal-consistency",
    "check-workflow-make-targets-consistency",
    "check-mcp-error-contract",
    "check-mcp-error-docs-sync",
    "check-ci-test-isolation",
    "check-local-ci-smoke",
    "check-agent-rule-sync",
)

# "{python}" is resolved to the current Python interpreter at runtime.
TARGET_COMMAND_TEMPLATES: dict[str, tuple[tuple[str, ...], ...]] = {
    "lint": (("ruff", "check", "src/", "tests/"),),
    "format-check": (("ruff", "format", "--check", "src/", "tests/"),),
    "typecheck-gate": (
        ("{python}", "-m", "scripts.ci.check_mypy_gate", "--gate", "baseline", "--verbose"),
    ),
    "typecheck-strict-island": (
        ("{python}", "-m", "scripts.ci.check_mypy_gate", "--gate", "strict-island", "--verbose"),
    ),
    "mypy-metrics": (
        (
            "{python}",
            "-m",
            "scripts.ci.mypy_metrics",
            "--output",
            "artifacts/mypy_metrics.json",
            "--verbose",
        ),
    ),
    "check-mypy-metrics-thresholds": (
        ("{python}", "-m", "scripts.ci.check_mypy_metrics_thresholds", "--verbose"),
    ),
    "check-schemas": (
        ("{python}", "scripts/validate_schemas.py", "--validate-fixtures", "--verbose"),
    ),
    "check-env-consistency": (
        ("{python}", "-m", "scripts.ci.check_env_var_consistency", "--verbose"),
    ),
    "check-logbook-consistency": (
        ("{python}", "scripts/verify_logbook_consistency.py", "--verbose"),
    ),
    "check-migration-sanity": (
        ("{python}", "-m", "scripts.ci.check_migration_sanity", "--verbose"),
    ),
    "check-scm-sync-consistency": (
        ("{python}", "scripts/verify_scm_sync_consistency.py", "--verbose"),
    ),
    "check-gateway-error-reason-usage": (
        ("{python}", "-m", "scripts.ci.check_gateway_error_reason_usage", "--verbose"),
    ),
    "check-gateway-public-api-surface": (
        ("{python}", "-m", "scripts.ci.check_gateway_public_api_import_surface", "--verbose"),
    ),
    "check-gateway-public-api-docs-sync": (
        ("{python}", "-m", "scripts.ci.check_gateway_public_api_docs_sync", "--verbose"),
    ),
    "check-gateway-di-boundaries": (
        ("{python}", "-m", "scripts.ci.check_gateway_di_boundaries", "--verbose"),
    ),
    "check-gateway-import-surface": (
        ("{python}", "-m", "scripts.ci.check_gateway_import_surface", "--verbose"),
    ),
    "check-gateway-correlation-id-single-source": (
        ("{python}", "-m", "scripts.ci.check_gateway_correlation_id_single_source", "--verbose"),
    ),
    "check-iteration-docs": (
        ("{python}", "-m", "scripts.ci.check_no_iteration_links_in_docs", "--verbose"),
        ("{python}", "-m", "scripts.ci.check_no_local_artifact_links_in_docs", "--verbose"),
        (
            "{python}",
            "-m",
            "scripts.ci.check_iteration_docs_placeholders",
            "--verbose",
            "--warn-only",
        ),
        ("{python}", "-m", "scripts.ci.check_iteration_evidence_contract", "--verbose"),
    ),
    "check-iteration-fixtures-freshness": (
        ("{python}", "-m", "scripts.ci.check_iteration_fixtures_freshness", "--verbose"),
    ),
    "check-iteration-toolchain-drift-map-contract": (
        ("{python}", "-m", "scripts.ci.check_iteration_toolchain_drift_map_contract", "--verbose"),
    ),
    "validate-workflows-strict": (("{python}", "-m", "scripts.ci.validate_workflows", "--strict"),),
    "check-workflow-contract-docs-sync": (
        ("{python}", "-m", "scripts.ci.check_workflow_contract_docs_sync"),
    ),
    "check-workflow-contract-error-types-docs-sync": (
        ("{python}", "-m", "scripts.ci.check_workflow_contract_error_types_docs_sync"),
    ),
    "check-workflow-contract-version-policy": (
        ("{python}", "-m", "scripts.ci.check_workflow_contract_version_policy", "--pr-mode"),
    ),
    "check-workflow-contract-internal-consistency": (
        ("{python}", "-m", "scripts.ci.check_workflow_contract_internal_consistency", "--verbose"),
    ),
    "check-workflow-make-targets-consistency": (
        ("{python}", "-m", "scripts.ci.check_workflow_make_targets_consistency", "--verbose"),
    ),
    "check-mcp-error-contract": (
        ("{python}", "-m", "scripts.ci.check_mcp_jsonrpc_error_contract", "--verbose"),
    ),
    "check-mcp-error-docs-sync": (
        ("{python}", "-m", "scripts.ci.check_mcp_jsonrpc_error_docs_sync", "--verbose"),
    ),
    "check-ci-test-isolation": (
        ("{python}", "-m", "scripts.ci.check_ci_test_isolation", "--verbose"),
    ),
    "check-local-ci-smoke": (
        (
            "{python}",
            "-m",
            "pytest",
            "scripts/tests/test_ci_no_make.py",
            "tests/ci/test_start_openmemory_launcher.py",
            "-q",
        ),
    ),
    "check-agent-rule-sync": (("{python}", "scripts/docs/sync_agent_rules.py", "--check"),),
}


@dataclass
class StepResult:
    target: str
    command: list[str]
    exit_code: int

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


def extract_make_ci_targets(makefile_path: Path) -> list[str]:
    """Extract `ci` dependencies from Makefile."""
    for raw_line in makefile_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line.startswith("ci:"):
            continue
        body = line.split(":", 1)[1]
        body = body.split("##", 1)[0].strip()
        if not body:
            return []
        return [tok for tok in body.split() if tok]
    raise ValueError(f"Could not find `ci:` target in {makefile_path}")


def _resolve_targets(only: Sequence[str]) -> list[str]:
    if not only:
        return list(CI_TARGET_ORDER)

    deduped: list[str] = []
    seen: set[str] = set()
    for target in only:
        if target not in TARGET_COMMAND_TEMPLATES:
            known = ", ".join(CI_TARGET_ORDER)
            raise ValueError(f"Unknown target: {target}. Known targets: {known}")
        if target in seen:
            continue
        seen.add(target)
        deduped.append(target)
    return deduped


def _build_target_commands(target: str, python_bin: str) -> list[list[str]]:
    templates = TARGET_COMMAND_TEMPLATES[target]
    commands: list[list[str]] = []
    for template in templates:
        commands.append([part.replace("{python}", python_bin) for part in template])
    return commands


def _fmt_command(command: Sequence[str]) -> str:
    return " ".join(shlex.quote(token) for token in command)


def _run_command(
    command: Sequence[str],
    cwd: Path,
    dry_run: bool,
    *,
    log_enabled: bool,
) -> int:
    if dry_run:
        if log_enabled:
            print(f"  [dry-run] {_fmt_command(command)}")
        return 0
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    proc = subprocess.run(list(command), cwd=str(cwd), env=env, check=False)
    return int(proc.returncode)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run make ci equivalent checks without make.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands only; do not execute.",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="TARGET",
        help="Run only selected target(s); can be specified multiple times.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON summary.",
    )
    parser.add_argument(
        "--project-root",
        default=None,
        help="Project root path (default: auto-detect from script location).",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    project_root = (
        Path(args.project_root).resolve()
        if args.project_root
        else Path(__file__).resolve().parents[2]
    )
    python_bin = sys.executable or "python"
    targets = _resolve_targets(args.only)

    results: list[StepResult] = []
    total = len(targets)
    overall_ok = True
    log_enabled = not args.json

    for i, target in enumerate(targets, start=1):
        if log_enabled:
            print(f"[{i}/{total}] {target}")
        target_commands = _build_target_commands(target, python_bin)
        for command in target_commands:
            exit_code = _run_command(
                command,
                project_root,
                args.dry_run,
                log_enabled=log_enabled,
            )
            result = StepResult(target=target, command=command, exit_code=exit_code)
            results.append(result)
            if exit_code != 0:
                overall_ok = False
                if log_enabled:
                    print(f"[FAIL] {target} -> {_fmt_command(command)} (exit={exit_code})")
                break
        if not overall_ok:
            break

    if overall_ok and log_enabled:
        print("[OK] ci_no_make completed successfully.")

    if args.json:
        payload = {
            "ok": overall_ok,
            "dry_run": bool(args.dry_run),
            "project_root": str(project_root),
            "targets_selected": targets,
            "results": [
                {
                    "target": item.target,
                    "command": item.command,
                    "exit_code": item.exit_code,
                    "passed": item.passed,
                }
                for item in results
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
