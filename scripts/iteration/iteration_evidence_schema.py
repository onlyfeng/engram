#!/usr/bin/env python3
"""SSOT constants and helpers for iteration evidence schemas."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

# Project root
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA_DIR = REPO_ROOT / "schemas"

# Current schema (default v2)
CURRENT_SCHEMA_FILENAME = "iteration_evidence_v2.schema.json"
CURRENT_SCHEMA_PATH = SCHEMA_DIR / CURRENT_SCHEMA_FILENAME
CURRENT_SCHEMA_ID = "https://github.com/engram/schemas/iteration_evidence_v2.schema.json"
# $schema reference as used by evidence files (from docs/acceptance/evidence/)
CURRENT_SCHEMA_REF = f"../../../schemas/{CURRENT_SCHEMA_FILENAME}"

# Legacy schema (v1) compatibility
LEGACY_SCHEMA_FILENAME = "iteration_evidence_v1.schema.json"
LEGACY_SCHEMA_PATH = SCHEMA_DIR / LEGACY_SCHEMA_FILENAME
LEGACY_SCHEMA_ID = "https://github.com/engram/schemas/iteration_evidence_v1.schema.json"
LEGACY_SCHEMA_REF = f"../../../schemas/{LEGACY_SCHEMA_FILENAME}"

SUPPORTED_SCHEMA_REFS = (
    CURRENT_SCHEMA_REF,
    CURRENT_SCHEMA_ID,
    LEGACY_SCHEMA_REF,
    LEGACY_SCHEMA_ID,
)

SUPPORTED_SCHEMA_FILENAMES = (
    CURRENT_SCHEMA_FILENAME,
    LEGACY_SCHEMA_FILENAME,
)


def resolve_schema_name(schema_value: Optional[str]) -> str:
    """Resolve a display schema filename from a $schema value."""
    if not schema_value:
        return CURRENT_SCHEMA_FILENAME
    schema_value = schema_value.strip()
    if schema_value.endswith(LEGACY_SCHEMA_FILENAME) or schema_value == LEGACY_SCHEMA_ID:
        return LEGACY_SCHEMA_FILENAME
    if schema_value.endswith(CURRENT_SCHEMA_FILENAME) or schema_value == CURRENT_SCHEMA_ID:
        return CURRENT_SCHEMA_FILENAME
    return CURRENT_SCHEMA_FILENAME


def resolve_schema_path(schema_value: Optional[str]) -> Path:
    """Resolve a schema path from a $schema value."""
    schema_name = resolve_schema_name(schema_value)
    if schema_name == LEGACY_SCHEMA_FILENAME:
        return LEGACY_SCHEMA_PATH
    return CURRENT_SCHEMA_PATH


__all__ = [
    "CURRENT_SCHEMA_FILENAME",
    "CURRENT_SCHEMA_PATH",
    "CURRENT_SCHEMA_ID",
    "CURRENT_SCHEMA_REF",
    "LEGACY_SCHEMA_FILENAME",
    "LEGACY_SCHEMA_PATH",
    "LEGACY_SCHEMA_ID",
    "LEGACY_SCHEMA_REF",
    "SUPPORTED_SCHEMA_REFS",
    "SUPPORTED_SCHEMA_FILENAMES",
    "resolve_schema_name",
    "resolve_schema_path",
]
