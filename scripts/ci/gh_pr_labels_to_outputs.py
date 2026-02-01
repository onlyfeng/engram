#!/usr/bin/env python3
"""
Parse PR labels and output to GITHUB_OUTPUT.

Usage:
    python gh_pr_labels_to_outputs.py

Environment Variables:
    GITHUB_EVENT_NAME: Event type (pull_request, push, etc.)
    PR_LABELS: Comma-separated list of PR labels
    GITHUB_OUTPUT: Path to GitHub Actions output file

Outputs (to GITHUB_OUTPUT):
    has_migrate_dry_run_label: true/false
    has_dual_read_label: true/false
    has_freeze_override_label: true/false
    has_compat_strict_label: true/false
"""

import os
import sys

# Label constants
LABEL_MIGRATE_DRY_RUN = "ci:seek-migrate-dry-run"
LABEL_DUAL_READ = "ci:dual-read"
LABEL_FREEZE_OVERRIDE = "openmemory:freeze-override"
LABEL_COMPAT_STRICT = "ci:seek-compat-strict"


def parse_labels(labels_str: str) -> set[str]:
    """Parse comma-separated labels string into a set."""
    if not labels_str:
        return set()
    return {label.strip() for label in labels_str.split(",") if label.strip()}


def write_output(name: str, value: str) -> None:
    """Write output to GITHUB_OUTPUT file."""
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"{name}={value}\n")
    # Also print for debugging
    print(f"{name}={value}")


def main() -> int:
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    labels_str = os.environ.get("PR_LABELS", "")

    # Default values
    has_migrate_label = "false"
    has_dual_read_label = "false"
    has_freeze_override_label = "false"
    has_compat_strict_label = "false"

    if event_name == "pull_request":
        labels = parse_labels(labels_str)
        print(f"PR Labels: {labels_str}")

        if LABEL_MIGRATE_DRY_RUN in labels:
            has_migrate_label = "true"
            print(f"Found label: {LABEL_MIGRATE_DRY_RUN}")

        if LABEL_DUAL_READ in labels:
            has_dual_read_label = "true"
            print(f"Found label: {LABEL_DUAL_READ}")

        if LABEL_FREEZE_OVERRIDE in labels:
            has_freeze_override_label = "true"
            print(f"Found label: {LABEL_FREEZE_OVERRIDE}")

        if LABEL_COMPAT_STRICT in labels:
            has_compat_strict_label = "true"
            print(f"Found label: {LABEL_COMPAT_STRICT}")
    else:
        print(f"Event type: {event_name} (not pull_request, skipping label check)")

    # Write outputs
    write_output("has_migrate_dry_run_label", has_migrate_label)
    write_output("has_dual_read_label", has_dual_read_label)
    write_output("has_freeze_override_label", has_freeze_override_label)
    write_output("has_compat_strict_label", has_compat_strict_label)

    return 0


if __name__ == "__main__":
    sys.exit(main())
