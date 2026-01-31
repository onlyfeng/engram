#!/usr/bin/env python3
"""
Import Manifest Consistency Test

Validates that the unified_stack_import_v1.json manifest correctly describes
all file dependencies required by:
1. docker-compose.unified.yml (volume mounts, build contexts)
2. apps/openmemory_gateway/gateway/Dockerfile (COPY instructions)

This test ensures that when integrating Engram into another project using
the import manifest, all required files will be present.

Usage:
    pytest scripts/tests/test_import_manifest_consistency.py -v
    # Or standalone:
    python scripts/tests/test_import_manifest_consistency.py
"""

import json
import re
import sys
from pathlib import Path
from typing import Any


# Project root detection
def find_project_root() -> Path:
    """Find the project root by looking for key files."""
    current = Path(__file__).resolve()
    for parent in [current] + list(current.parents):
        if (parent / "docker-compose.unified.yml").exists() and (
            parent / "Makefile"
        ).exists():
            return parent
    raise RuntimeError("Could not find project root")


PROJECT_ROOT = find_project_root()

# Files to check
MANIFEST_PATH = PROJECT_ROOT / "docs/guides/manifests/unified_stack_import_v1.json"
COMPOSE_PATH = PROJECT_ROOT / "docker-compose.unified.yml"
GATEWAY_DOCKERFILE_PATH = PROJECT_ROOT / "docker/engram.Dockerfile"


def load_manifest() -> dict[str, Any]:
    """Load and parse the import manifest."""
    if not MANIFEST_PATH.exists():
        raise FileNotFoundError(f"Manifest not found: {MANIFEST_PATH}")
    with open(MANIFEST_PATH) as f:
        return json.load(f)


def extract_manifest_paths(manifest: dict[str, Any]) -> set[str]:
    """Extract all source paths from the manifest (required + optional)."""
    paths: set[str] = set()

    def add_paths(items: list[dict]) -> None:
        for item in items:
            if "source_path" in item:
                paths.add(item["source_path"])
            if "source_paths" in item:
                paths.update(item["source_paths"])

    files = manifest.get("files", {})
    add_paths(files.get("required", []))
    add_paths(files.get("optional", []))

    return paths


def parse_compose_paths(compose_content: str) -> dict[str, set[str]]:
    """
    Parse docker-compose.unified.yml to extract path dependencies.
    
    Returns dict with keys:
    - 'volumes': volume mount source paths (local paths)
    - 'build_contexts': build context paths
    - 'dockerfiles': Dockerfile paths
    """
    paths: dict[str, set[str]] = {
        "volumes": set(),
        "build_contexts": set(),
        "dockerfiles": set(),
    }

    # Pattern for volume mounts: ./path:/container/path or ./path:/container/path:ro
    # Only match local paths starting with ./ (not named volumes)
    volume_pattern = re.compile(r"^\s*-\s*(\./[^:]+):", re.MULTILINE)
    for match in volume_pattern.finditer(compose_content):
        path = match.group(1)
        # Normalize: remove ./ prefix
        normalized = path.lstrip("./")
        paths["volumes"].add(normalized)

    # Pattern for build context: context: ./path or context: .
    context_pattern = re.compile(r"^\s*context:\s*(\./[^\s]+|\.)\s*$", re.MULTILINE)
    for match in context_pattern.finditer(compose_content):
        ctx = match.group(1)
        if ctx != ".":
            normalized = ctx.lstrip("./")
            paths["build_contexts"].add(normalized)

    # Pattern for dockerfile: dockerfile: path/to/Dockerfile
    dockerfile_pattern = re.compile(
        r"^\s*dockerfile:\s*([^\s]+)", re.MULTILINE
    )
    for match in dockerfile_pattern.finditer(compose_content):
        paths["dockerfiles"].add(match.group(1))

    return paths


def parse_dockerfile_copy_paths(dockerfile_content: str) -> set[str]:
    """
    Parse Dockerfile to extract COPY source paths.
    
    Only extracts the source (first argument) of COPY instructions.
    """
    paths: set[str] = set()

    # Pattern for COPY instruction: COPY src dest
    # Handles multi-line COPY with backslash continuation
    # Only match paths that look like relative paths (not --from= or /absolute)
    copy_pattern = re.compile(
        r"^COPY\s+(?:--[^\s]+\s+)*([^\s/][^\s]*)\s+",
        re.MULTILINE,
    )

    for match in copy_pattern.finditer(dockerfile_content):
        src = match.group(1)
        # Skip options like --from=builder
        if src.startswith("--"):
            continue
        # Skip absolute paths
        if src.startswith("/"):
            continue
        paths.add(src)

    return paths


def normalize_to_directory(path: str) -> str:
    """Normalize a path to its containing directory if it's a file."""
    # Common file patterns
    file_patterns = [
        r"\.ya?ml$",
        r"\.json$",
        r"\.py$",
        r"\.sql$",
        r"\.sh$",
        r"\.txt$",
        r"\.toml$",
        r"\.md$",
        r"Dockerfile",
        r"README",
        r"Makefile",
    ]
    for pattern in file_patterns:
        if re.search(pattern, path):
            # Return parent directory
            parts = path.rsplit("/", 1)
            if len(parts) > 1:
                return parts[0]
            return "."
    return path


def is_path_covered_by_manifest(
    path: str, manifest_paths: set[str], manifest: dict[str, Any]
) -> bool:
    """
    Check if a path is covered by the manifest.
    
    A path is covered if:
    1. It's explicitly listed in the manifest
    2. It's a subdirectory of a listed directory
    3. It's a file within a listed directory
    4. It's a parent directory whose essential subdirectories are listed
    """
    # Direct match
    if path in manifest_paths:
        return True

    # Check if path is under any manifest directory
    for manifest_path in manifest_paths:
        # Manifest path is a directory containing this path
        if path.startswith(manifest_path + "/"):
            return True
        # Path is a file in the manifest directory
        normalized = normalize_to_directory(path)
        if normalized == manifest_path:
            return True
        if normalized.startswith(manifest_path + "/"):
            return True

    # Check if path is a parent directory with key subdirs in manifest
    # For example: apps/logbook_postgres is covered if both
    # apps/logbook_postgres/sql and apps/logbook_postgres/scripts are in manifest
    path_as_prefix = path.rstrip("/") + "/"
    subdirs_in_manifest = [
        mp for mp in manifest_paths if mp.startswith(path_as_prefix)
    ]
    if len(subdirs_in_manifest) >= 2:
        # If at least 2 subdirectories are in the manifest, consider it covered
        # This handles cases like apps/logbook_postgres being mounted but
        # only sql/ and scripts/ being essential
        return True

    return False


# Paths to ignore in consistency checks (output directories, not source deps)
IGNORED_PATHS = {
    ".artifacts",
    "artifacts",
}


def should_skip_path(path: str) -> bool:
    """Check if a path should be skipped (output directories, etc.)."""
    # Normalize path
    normalized = path.strip("/").split("/")[0] if "/" in path else path.strip("/")
    if normalized in IGNORED_PATHS:
        return True
    # Also check full path
    if path.strip("/") in IGNORED_PATHS:
        return True
    # Check if any component is ignored
    for part in path.split("/"):
        if part in IGNORED_PATHS:
            return True
    return False


def check_compose_dependencies(
    compose_paths: dict[str, set[str]],
    manifest_paths: set[str],
    manifest: dict[str, Any],
) -> list[str]:
    """Check that all compose dependencies are covered by manifest."""
    issues: list[str] = []

    # Check volume mounts
    for vol_path in compose_paths["volumes"]:
        # Skip ignored paths (output directories)
        if should_skip_path(vol_path):
            continue
        if not is_path_covered_by_manifest(vol_path, manifest_paths, manifest):
            # Check if this is an internal SQL init script path
            # These are standard Postgres init paths
            if "docker-entrypoint-initdb.d" in vol_path:
                continue
            issues.append(f"Volume mount not in manifest: {vol_path}")

    # Check build contexts
    for ctx_path in compose_paths["build_contexts"]:
        if should_skip_path(ctx_path):
            continue
        if not is_path_covered_by_manifest(ctx_path, manifest_paths, manifest):
            issues.append(f"Build context not in manifest: {ctx_path}")

    return issues


def check_dockerfile_dependencies(
    dockerfile_paths: set[str],
    manifest_paths: set[str],
    manifest: dict[str, Any],
    dockerfile_context: str = ".",
) -> list[str]:
    """Check that all Dockerfile COPY sources are covered by manifest."""
    issues: list[str] = []

    for copy_path in dockerfile_paths:
        # Resolve path relative to build context
        if dockerfile_context != ".":
            full_path = f"{dockerfile_context}/{copy_path}"
        else:
            full_path = copy_path

        if not is_path_covered_by_manifest(full_path, manifest_paths, manifest):
            issues.append(f"Dockerfile COPY not in manifest: {full_path}")

    return issues


def test_manifest_exists():
    """Test that the import manifest file exists."""
    assert MANIFEST_PATH.exists(), f"Manifest not found: {MANIFEST_PATH}"


def test_manifest_valid_json():
    """Test that the manifest is valid JSON."""
    manifest = load_manifest()
    assert "files" in manifest, "Manifest missing 'files' section"
    assert "required" in manifest["files"], "Manifest missing 'files.required' section"


def test_manifest_paths_exist():
    """Test that all paths in the manifest actually exist in the repo."""
    manifest = load_manifest()
    paths = extract_manifest_paths(manifest)

    missing: list[str] = []
    for path in paths:
        full_path = PROJECT_ROOT / path
        if not full_path.exists():
            missing.append(path)

    assert not missing, f"Manifest references non-existent paths: {missing}"


def test_compose_paths_covered_by_manifest():
    """Test that all docker-compose.unified.yml dependencies are in manifest."""
    manifest = load_manifest()
    manifest_paths = extract_manifest_paths(manifest)

    with open(COMPOSE_PATH) as f:
        compose_content = f.read()

    compose_paths = parse_compose_paths(compose_content)
    issues = check_compose_dependencies(compose_paths, manifest_paths, manifest)

    assert not issues, f"Compose dependencies not in manifest:\n" + "\n".join(
        f"  - {issue}" for issue in issues
    )


def test_gateway_dockerfile_paths_covered_by_manifest():
    """Test that Gateway Dockerfile COPY sources are in manifest."""
    if not GATEWAY_DOCKERFILE_PATH.exists():
        # Skip if Dockerfile doesn't exist yet
        return

    manifest = load_manifest()
    manifest_paths = extract_manifest_paths(manifest)

    with open(GATEWAY_DOCKERFILE_PATH) as f:
        dockerfile_content = f.read()

    dockerfile_paths = parse_dockerfile_copy_paths(dockerfile_content)
    # Gateway Dockerfile uses root as build context (per compose)
    issues = check_dockerfile_dependencies(
        dockerfile_paths, manifest_paths, manifest, dockerfile_context="."
    )

    assert not issues, f"Dockerfile dependencies not in manifest:\n" + "\n".join(
        f"  - {issue}" for issue in issues
    )


def test_manifest_constraints_valid():
    """Test that manifest constraints reference valid file IDs."""
    manifest = load_manifest()

    # Collect all file IDs
    file_ids: set[str] = set()
    for item in manifest.get("files", {}).get("required", []):
        if "id" in item:
            file_ids.add(item["id"])
    for item in manifest.get("files", {}).get("optional", []):
        if "id" in item:
            file_ids.add(item["id"])

    # Check constraints reference valid IDs
    constraints = manifest.get("constraints", [])
    for constraint in constraints:
        applies_to = constraint.get("applies_to", [])
        for ref_id in applies_to:
            assert ref_id in file_ids, (
                f"Constraint '{constraint.get('id', 'unknown')}' references "
                f"unknown file ID: {ref_id}"
            )


def test_manifest_profiles_reference_valid_paths():
    """Test that profile required_files reference valid manifest paths."""
    manifest = load_manifest()
    manifest_paths = extract_manifest_paths(manifest)

    profiles = manifest.get("profiles", {}).get("available", [])
    issues: list[str] = []

    for profile in profiles:
        profile_name = profile.get("name", "unknown")
        required_files = profile.get("required_files", [])

        for req_path in required_files:
            # Remove trailing slash for comparison
            normalized = req_path.rstrip("/")
            # Skip if it should be ignored
            if should_skip_path(normalized):
                continue
            if not is_path_covered_by_manifest(normalized, manifest_paths, manifest):
                issues.append(
                    f"Profile '{profile_name}' requires '{req_path}' not in manifest"
                )

    assert not issues, f"Profile required_files issues:\n" + "\n".join(
        f"  - {issue}" for issue in issues
    )


def run_all_checks() -> tuple[bool, list[str]]:
    """Run all consistency checks and return (success, issues)."""
    issues: list[str] = []

    # Load manifest
    try:
        manifest = load_manifest()
    except FileNotFoundError as e:
        return False, [str(e)]

    manifest_paths = extract_manifest_paths(manifest)

    # Check manifest paths exist
    for path in manifest_paths:
        full_path = PROJECT_ROOT / path
        if not full_path.exists():
            issues.append(f"Manifest path does not exist: {path}")

    # Check compose dependencies
    if COMPOSE_PATH.exists():
        with open(COMPOSE_PATH) as f:
            compose_content = f.read()
        compose_paths = parse_compose_paths(compose_content)
        issues.extend(
            check_compose_dependencies(compose_paths, manifest_paths, manifest)
        )

    # Check Dockerfile dependencies
    if GATEWAY_DOCKERFILE_PATH.exists():
        with open(GATEWAY_DOCKERFILE_PATH) as f:
            dockerfile_content = f.read()
        dockerfile_paths = parse_dockerfile_copy_paths(dockerfile_content)
        issues.extend(
            check_dockerfile_dependencies(
                dockerfile_paths, manifest_paths, manifest, dockerfile_context="."
            )
        )

    # Check constraints
    file_ids: set[str] = set()
    for item in manifest.get("files", {}).get("required", []):
        if "id" in item:
            file_ids.add(item["id"])
    for item in manifest.get("files", {}).get("optional", []):
        if "id" in item:
            file_ids.add(item["id"])

    for constraint in manifest.get("constraints", []):
        for ref_id in constraint.get("applies_to", []):
            if ref_id not in file_ids:
                issues.append(
                    f"Constraint '{constraint.get('id', 'unknown')}' "
                    f"references unknown file ID: {ref_id}"
                )

    # Check profiles
    profiles = manifest.get("profiles", {}).get("available", [])
    for profile in profiles:
        profile_name = profile.get("name", "unknown")
        for req_path in profile.get("required_files", []):
            normalized = req_path.rstrip("/")
            if should_skip_path(normalized):
                continue
            if not is_path_covered_by_manifest(normalized, manifest_paths, manifest):
                issues.append(
                    f"Profile '{profile_name}' requires '{req_path}' not in manifest"
                )

    return len(issues) == 0, issues


def main() -> int:
    """Main entry point for standalone execution."""
    print("=" * 60)
    print("Import Manifest Consistency Check")
    print("=" * 60)
    print()

    success, issues = run_all_checks()

    if success:
        print("[OK] All consistency checks passed")
        return 0
    else:
        print("[FAIL] Consistency check failed:")
        for issue in issues:
            print(f"  - {issue}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
