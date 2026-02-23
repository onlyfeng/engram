#!/usr/bin/env python3
"""Check required SQL migration files exist."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

REQUIRED_MIGRATION_FILES: tuple[str, ...] = (
    "sql/01_logbook_schema.sql",
    "sql/02_scm_migration.sql",
    "sql/04_roles_and_grants.sql",
    "sql/05_openmemory_roles_and_grants.sql",
    "sql/06_scm_sync_runs.sql",
    "sql/07_scm_sync_locks.sql",
    "sql/08_scm_sync_jobs.sql",
    "sql/11_sync_jobs_dimension_columns.sql",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check required SQL migration files exist.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output machine-readable JSON.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-file status lines.",
    )
    parser.add_argument(
        "--project-root",
        default=".",
        help="Project root path (default: current directory).",
    )
    return parser.parse_args()


def _check_files(project_root: Path, required_files: Sequence[str]) -> dict[str, list[str]]:
    missing: list[str] = []
    present: list[str] = []

    for rel_path in required_files:
        file_path = project_root / rel_path
        if file_path.is_file():
            present.append(rel_path)
        else:
            missing.append(rel_path)

    return {"missing": missing, "present": present}


def main() -> int:
    args = _parse_args()
    project_root = Path(args.project_root).resolve()
    result = _check_files(project_root, REQUIRED_MIGRATION_FILES)
    missing = result["missing"]
    present = result["present"]

    if args.json:
        payload = {
            "ok": not missing,
            "project_root": str(project_root),
            "required_count": len(REQUIRED_MIGRATION_FILES),
            "present_count": len(present),
            "missing_count": len(missing),
            "present": present,
            "missing": missing,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if not missing else 1

    if args.verbose:
        print("Checking SQL migration files...")
        for rel_path in present:
            print(f"[OK] found: {rel_path}")
        for rel_path in missing:
            print(f"[ERROR] missing: {rel_path}")

    if missing:
        print(f"Missing {len(missing)} required migration file(s).")
        return 1

    print("SQL migration files check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
