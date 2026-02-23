#!/usr/bin/env python3
"""Tests for scripts/ops/ci_no_make.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))

import ci_no_make


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_ci_target_order_matches_makefile() -> None:
    makefile = _repo_root() / "Makefile"
    make_targets = ci_no_make.extract_make_ci_targets(makefile)
    assert make_targets == list(ci_no_make.CI_TARGET_ORDER)


def test_all_ci_targets_have_equivalent_commands() -> None:
    missing = [
        target for target in ci_no_make.CI_TARGET_ORDER
        if target not in ci_no_make.TARGET_COMMAND_TEMPLATES
    ]
    assert missing == []


def test_ci_no_make_has_no_extra_targets() -> None:
    extras = [
        target for target in ci_no_make.TARGET_COMMAND_TEMPLATES
        if target not in ci_no_make.CI_TARGET_ORDER
    ]
    assert extras == []


def test_resolve_targets_rejects_unknown_target() -> None:
    with pytest.raises(ValueError, match="Unknown target"):
        ci_no_make._resolve_targets(["not-a-real-target"])
