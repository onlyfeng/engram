#!/usr/bin/env python3
"""
Record acceptance test runs for reproducibility and audit.

Usage:
    python scripts/acceptance/record_acceptance_run.py \
        --name acceptance-logbook-only \
        --artifacts-dir .artifacts/acceptance-logbook-only \
        --result PASS \
        [--commit <sha>] \
        [--command <custom command>] \
        [--metadata-json '{"workflow": "ci", "profile": "http_only"}'] \
        [--metadata-kv workflow=ci --metadata-kv profile=http_only]

Output:
    .artifacts/acceptance-runs/<timestamp>_<name>.json

Fields:
    - name: Acceptance command name
    - commit: Git commit SHA (auto-detected if not provided)
    - timestamp: ISO 8601 UTC timestamp
    - result: PASS / FAIL / PARTIAL
    - os_version: Operating system and version
    - docker_version: Docker version (if available)
    - environment: Sanitized environment variables
    - command: Full command executed (default: make {name}, overridable via --command)
    - duration_seconds: Duration from artifacts summary (if available)
    - artifacts: List of artifact file paths
    - metadata: Custom metadata (optional, from --metadata-json or --metadata-kv)
"""

import argparse
import datetime
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional


# Environment variables to capture (sanitized)
CAPTURE_ENV_VARS = [
    "SKIP_DEPLOY",
    "SKIP_MIGRATE",
    "SKIP_VERIFY_PERMISSIONS",
    "SKIP_LOGBOOK_SMOKE",
    "HTTP_ONLY_MODE",
    "SKIP_DEGRADATION_TEST",
    "SKIP_JSONRPC",
    "GATE_PROFILE",
    "SEEKDB_ENABLE",
    "RUN_INTEGRATION_TESTS",
    "COMPOSE_PROJECT_NAME",
    "POSTGRES_DSN",  # Will be sanitized
    "GATEWAY_URL",
    "OPENMEMORY_URL",
    "OM_PG_SCHEMA",
]

# Environment variables that should be sanitized (credentials removed)
SENSITIVE_ENV_VARS = ["POSTGRES_DSN"]


def sanitize_value(key: str, value: str) -> str:
    """Sanitize sensitive environment variable values."""
    if key in SENSITIVE_ENV_VARS:
        # Mask password in DSN: postgresql://user:password@host -> postgresql://user:***@host
        return re.sub(r"(://[^:]+:)[^@]+(@)", r"\1***\2", value)
    return value


def get_git_commit() -> Optional[str]:
    """Get current git commit SHA."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def get_os_version() -> str:
    """Get OS and version information."""
    return f"{platform.system()} {platform.release()} ({platform.machine()})"


def get_docker_version() -> Optional[str]:
    """Get Docker version if available."""
    try:
        result = subprocess.run(
            ["docker", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            # "Docker version 24.0.6, build ed223bc"
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def get_captured_env() -> dict[str, str]:
    """Capture and sanitize relevant environment variables."""
    env = {}
    for key in CAPTURE_ENV_VARS:
        value = os.environ.get(key)
        if value is not None:
            env[key] = sanitize_value(key, value)
    return env


def list_artifacts(artifacts_dir: Path) -> list[str]:
    """List all artifact files in the directory."""
    if not artifacts_dir.exists():
        return []
    
    artifacts = []
    for path in sorted(artifacts_dir.rglob("*")):
        if path.is_file():
            # Use relative path from workspace root
            try:
                rel_path = path.relative_to(Path.cwd())
                artifacts.append(str(rel_path))
            except ValueError:
                artifacts.append(str(path))
    return artifacts


def load_summary_duration(artifacts_dir: Path) -> Optional[int]:
    """Load duration from summary.json if available."""
    summary_path = artifacts_dir / "summary.json"
    if summary_path.exists():
        try:
            with open(summary_path) as f:
                data = json.load(f)
                return data.get("duration_seconds")
        except (json.JSONDecodeError, OSError):
            pass
    return None


def parse_metadata_kv(kv_pairs: Optional[list[str]]) -> dict[str, str]:
    """
    Parse key=value pairs into a dictionary.
    
    Args:
        kv_pairs: List of "key=value" strings
        
    Returns:
        Dictionary of parsed key-value pairs
        
    Raises:
        ValueError: If a pair is malformed (missing '=')
    """
    if not kv_pairs:
        return {}
    
    result = {}
    for pair in kv_pairs:
        if "=" not in pair:
            raise ValueError(f"Invalid key=value format: '{pair}' (missing '=')")
        key, _, value = pair.partition("=")
        if not key:
            raise ValueError(f"Invalid key=value format: '{pair}' (empty key)")
        result[key] = value
    return result


def merge_metadata(
    metadata_json: Optional[str],
    metadata_kv: Optional[list[str]],
) -> Optional[dict[str, Any]]:
    """
    Merge metadata from JSON string and key=value pairs.
    
    Key=value pairs take precedence over JSON values for the same key.
    
    Args:
        metadata_json: JSON string with metadata
        metadata_kv: List of "key=value" strings
        
    Returns:
        Merged metadata dictionary, or None if no metadata provided
        
    Raises:
        ValueError: If JSON is invalid or kv format is malformed
    """
    result: dict[str, Any] = {}
    
    # Parse JSON metadata first
    if metadata_json:
        try:
            parsed = json.loads(metadata_json)
            if not isinstance(parsed, dict):
                raise ValueError("--metadata-json must be a JSON object, not array or primitive")
            result.update(parsed)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in --metadata-json: {e}")
    
    # Parse and merge key=value pairs (these override JSON values)
    kv_dict = parse_metadata_kv(metadata_kv)
    result.update(kv_dict)
    
    return result if result else None


def record_acceptance_run(
    name: str,
    artifacts_dir: str,
    result: str,
    commit: Optional[str] = None,
    command: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> str:
    """
    Record an acceptance test run.
    
    Args:
        name: Acceptance command name (e.g., "acceptance-logbook-only")
        artifacts_dir: Path to artifacts directory
        result: Test result (PASS/FAIL/PARTIAL)
        commit: Git commit SHA (auto-detected if not provided)
        command: Custom command (default: "make {name}")
        metadata: Optional metadata dictionary (workflow, profile, github_run_id, etc.)
    
    Returns:
        Path to the created record file.
    """
    timestamp = datetime.datetime.now(datetime.timezone.utc)
    timestamp_str = timestamp.strftime("%Y%m%dT%H%M%SZ")
    timestamp_iso = timestamp.isoformat()
    
    # Auto-detect commit if not provided
    if not commit:
        commit = get_git_commit()
    
    # Use custom command or default to make {name}
    effective_command = command if command is not None else f"make {name}"
    
    artifacts_path = Path(artifacts_dir)
    
    # Build record
    record: dict[str, Any] = {
        "name": name,
        "timestamp": timestamp_iso,
        "result": result,
        "commit": commit,
        "os_version": get_os_version(),
        "docker_version": get_docker_version(),
        "environment": get_captured_env(),
        "command": effective_command,
        "artifacts_dir": artifacts_dir,
        "artifacts": list_artifacts(artifacts_path),
    }
    
    # Add duration if available
    duration = load_summary_duration(artifacts_path)
    if duration is not None:
        record["duration_seconds"] = duration
    
    # Add metadata if provided
    if metadata:
        record["metadata"] = metadata
    
    # Create output directory
    output_dir = Path(".artifacts/acceptance-runs")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Write record file
    output_file = output_dir / f"{timestamp_str}_{name}.json"
    with open(output_file, "w") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)
        f.write("\n")
    
    return str(output_file)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Record acceptance test run for audit and reproducibility",
    )
    parser.add_argument(
        "--name",
        required=True,
        help="Acceptance command name (e.g., acceptance-logbook-only)",
    )
    parser.add_argument(
        "--artifacts-dir",
        required=True,
        help="Path to artifacts directory (e.g., .artifacts/acceptance-logbook-only)",
    )
    parser.add_argument(
        "--result",
        required=True,
        choices=["PASS", "FAIL", "PARTIAL"],
        help="Acceptance test result",
    )
    parser.add_argument(
        "--commit",
        help="Git commit SHA (auto-detected if not provided)",
    )
    parser.add_argument(
        "--command",
        help="Custom command to record (default: 'make {name}')",
    )
    parser.add_argument(
        "--metadata-json",
        help="JSON string with metadata (e.g., '{\"workflow\": \"ci\", \"profile\": \"http_only\"}')",
    )
    parser.add_argument(
        "--metadata-kv",
        action="append",
        metavar="KEY=VALUE",
        help="Key=value metadata pairs (can be used multiple times, e.g., --metadata-kv workflow=ci --metadata-kv profile=http_only)",
    )
    
    args = parser.parse_args()
    
    try:
        # Merge metadata from JSON and key=value pairs
        metadata = merge_metadata(args.metadata_json, args.metadata_kv)
        
        output_path = record_acceptance_run(
            name=args.name,
            artifacts_dir=args.artifacts_dir,
            result=args.result,
            commit=args.commit,
            command=args.command,
            metadata=metadata,
        )
        print(f"Acceptance run recorded: {output_path}")
        return 0
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error recording acceptance run: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
