"""Regression contract for the OpenMemory init-race recovery script.

Locks in the lessons from the 2026-04-16 nightly failure where the inline
recovery hardcoded ``openmemory_users`` and used ``DROP TYPE`` against what
was actually a TABLE.  Future edits that re-introduce either mistake will
fail this test before they reach CI.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "recover_openmemory_init_race.sh"


@pytest.fixture(scope="module")
def emitted_sql() -> str:
    bash = shutil.which("bash")
    assert bash, "bash required to exercise the recovery script"
    result = subprocess.run(
        [bash, str(SCRIPT), "--print-sql"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.exists(), f"missing {SCRIPT}"
    assert SCRIPT.stat().st_mode & 0o111, "recovery script must be executable"


def test_no_hardcoded_openmemory_table_names(emitted_sql: str) -> None:
    """The recovery must enumerate tables, not bake in a single name.

    The 2026-04-16 nightly broke because the script was wired only to
    ``openmemory_users`` while the actual conflict was ``openmemory_memories``.
    Any literal ``openmemory.openmemory_<name>`` reference in the SQL is a
    regression of that pattern.
    """
    offenders = re.findall(r"openmemory\.openmemory_[a-z_]+", emitted_sql)
    assert not offenders, (
        f"recovery SQL must not hardcode openmemory table names; found: {sorted(set(offenders))}"
    )


def test_drops_tables_not_just_types(emitted_sql: str) -> None:
    """A row type backed by a TABLE cannot be removed via DROP TYPE.

    The script must DROP TABLE so postgres also removes the implicit row
    type that triggered ``pg_type_typname_nsp_index``.
    """
    assert "DROP TABLE" in emitted_sql, "recovery must drop tables"
    assert "CASCADE" in emitted_sql, "drops must cascade"


def test_scopes_to_openmemory_schema(emitted_sql: str) -> None:
    """Guard against accidentally widening the blast radius to other schemas."""
    assert "schemaname = 'openmemory'" in emitted_sql
    assert "nspname = 'openmemory'" in emitted_sql
    for forbidden in ("logbook", "governance", "public", "pg_catalog"):
        assert f"'{forbidden}'" not in emitted_sql, (
            f"recovery must not reference {forbidden} schema"
        )


def test_workflow_invokes_extracted_script() -> None:
    """nightly.yml must call the script — not re-inline the SQL."""
    workflow = (
        Path(__file__).resolve().parents[2] / ".github" / "workflows" / "nightly.yml"
    ).read_text()
    assert "recover_openmemory_init_race.sh" in workflow, (
        "nightly.yml should invoke scripts/ci/recover_openmemory_init_race.sh"
    )
    # The old broken pattern must not return.
    assert "DROP TYPE openmemory.openmemory_users" not in workflow
