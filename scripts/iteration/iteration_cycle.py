#!/usr/bin/env python3
"""Iteration cycle helpers."""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DRIFT_MAP_PATH = REPO_ROOT / "configs" / "iteration_toolchain_drift_map.v1.json"
RERUN_ADVICE_KEYS = ("fixture_refresh_commands", "minimal_tests", "minimal_gates")


@dataclass(frozen=True)
class DriftTriggers:
    prefixes: tuple[str, ...]
    globs: tuple[str, ...]


@dataclass(frozen=True)
class DriftActions:
    fixture_refresh_commands: tuple[str, ...]
    minimal_tests: tuple[str, ...]
    minimal_gates: tuple[str, ...]


@dataclass(frozen=True)
class DriftRule:
    rule_id: str
    description: str
    triggers: DriftTriggers
    actions: DriftActions


def _require_str(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing or empty string: {context}")
    return value


def _require_str_list(value: object, context: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Expected list[str]: {context}")
    return [item for item in value if item.strip()]


def _load_drift_map(path: Path) -> list[DriftRule]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Drift map config not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Drift map config JSON parse failed: {path}: {exc}") from exc

    rules = payload.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError(f"Drift map config missing rules: {path}")

    parsed: list[DriftRule] = []
    for idx, raw_rule in enumerate(rules, 1):
        if not isinstance(raw_rule, dict):
            raise ValueError(f"Rule must be an object: rules[{idx}]")

        rule_id = _require_str(raw_rule.get("id"), f"rules[{idx}].id")
        description = _require_str(raw_rule.get("description"), f"rules[{idx}].description")

        triggers = raw_rule.get("triggers")
        if not isinstance(triggers, dict):
            raise ValueError(f"rules[{idx}].triggers must be an object")
        prefixes = _require_str_list(
            triggers.get("prefixes"), f"rules[{idx}].triggers.prefixes"
        )
        globs = _require_str_list(triggers.get("globs"), f"rules[{idx}].triggers.globs")
        if not prefixes and not globs:
            raise ValueError(f"rules[{idx}] triggers must not be empty")

        actions = raw_rule.get("actions")
        if not isinstance(actions, dict):
            raise ValueError(f"rules[{idx}].actions must be an object")
        fixture_refresh_commands = _require_str_list(
            actions.get("fixture_refresh_commands"),
            f"rules[{idx}].actions.fixture_refresh_commands",
        )
        minimal_tests = _require_str_list(
            actions.get("minimal_tests"), f"rules[{idx}].actions.minimal_tests"
        )
        minimal_gates = _require_str_list(
            actions.get("minimal_gates"), f"rules[{idx}].actions.minimal_gates"
        )

        parsed.append(
            DriftRule(
                rule_id=rule_id,
                description=description,
                triggers=DriftTriggers(tuple(prefixes), tuple(globs)),
                actions=DriftActions(
                    tuple(fixture_refresh_commands),
                    tuple(minimal_tests),
                    tuple(minimal_gates),
                ),
            )
        )

    return parsed


def _normalize_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _looks_like_windows_abs(path: str) -> bool:
    if path.startswith("\\\\"):
        return True
    if len(path) < 3:
        return False
    return path[1] == ":" and (path[2] == "\\" or path[2] == "/")


def _normalize_changed_paths(paths: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    repo_root = _normalize_path(str(REPO_ROOT))
    repo_root_suffix = repo_root.lstrip("/")
    for raw_path in paths:
        if not raw_path:
            continue
        raw_str = str(raw_path)
        path_obj = Path(raw_str)
        if path_obj.is_absolute():
            try:
                raw_str = path_obj.relative_to(REPO_ROOT).as_posix()
            except ValueError:
                raw_str = path_obj.as_posix()
        normalized_path = _normalize_path(raw_str)
        if _looks_like_windows_abs(raw_str) and repo_root_suffix:
            marker = f"{repo_root_suffix}/"
            idx = normalized_path.find(marker)
            if idx != -1:
                normalized_path = normalized_path[idx + len(marker) :]
        normalized.append(normalized_path)
    return normalized


def _prefix_match(path: str, prefix: str) -> bool:
    prefix = _normalize_path(prefix)
    if not prefix:
        return False
    if prefix.endswith("/"):
        return path.startswith(prefix)
    if path == prefix:
        return True
    return path.startswith(prefix + "/")


def _matches_triggers(path: str, triggers: DriftTriggers) -> bool:
    for prefix in triggers.prefixes:
        if _prefix_match(path, prefix):
            return True
    for glob_pattern in triggers.globs:
        if fnmatch.fnmatch(path, _normalize_path(glob_pattern)):
            return True
    return False


def _merge_unique(target: list[str], additions: Iterable[str], seen: set[str]) -> None:
    for item in additions:
        if item in seen:
            continue
        target.append(item)
        seen.add(item)


def _format_suggestion_message(suggested: dict[str, list[str]]) -> str:
    lines = [
        "Suggested rerun commands:",
        "Note: fixture_refresh_commands, minimal_tests, minimal_gates are executable commands.",
    ]
    for key in RERUN_ADVICE_KEYS:
        values = suggested.get(key, [])
        if not values:
            continue
        lines.append(f"- {key}: {', '.join(values)}")
    return "\n".join(lines)


def format_rerun_advice_markdown(suggested: dict[str, list[str]]) -> str:
    lines = [
        "Suggested rerun commands:",
        "Note: fixture_refresh_commands, minimal_tests, minimal_gates are executable commands.",
    ]
    for key in RERUN_ADVICE_KEYS:
        values = suggested.get(key, [])
        if not values:
            continue
        lines.append(f"- {key}:")
        for value in values:
            lines.append(f"  - {value}")
    return "\n".join(lines)


def collect_rerun_advice(
    changed_paths: Iterable[str],
    issues: list[str] | None = None,
    allow_suggested_commands: bool = True,
    drift_map_path: Path | None = None,
) -> dict[str, object]:
    """Collect rerun advice based on toolchain drift mapping."""
    issues = list(issues or [])
    rules = _load_drift_map(drift_map_path or DRIFT_MAP_PATH)
    normalized_paths = _normalize_changed_paths(changed_paths)

    fixture_refresh_commands: list[str] = []
    minimal_tests: list[str] = []
    minimal_gates: list[str] = []

    seen_refresh: set[str] = set()
    seen_tests: set[str] = set()
    seen_gates: set[str] = set()

    for rule in rules:
        if not any(_matches_triggers(path, rule.triggers) for path in normalized_paths):
            continue
        _merge_unique(fixture_refresh_commands, rule.actions.fixture_refresh_commands, seen_refresh)
        _merge_unique(minimal_tests, rule.actions.minimal_tests, seen_tests)
        _merge_unique(minimal_gates, rule.actions.minimal_gates, seen_gates)

    suggested = {
        "fixture_refresh_commands": fixture_refresh_commands,
        "minimal_tests": minimal_tests,
        "minimal_gates": minimal_gates,
    }

    if allow_suggested_commands and any(suggested.values()):
        return {
            "issues": issues,
            "suggested_commands": suggested,
        }

    if any(suggested.values()):
        issues.append(_format_suggestion_message(suggested))

    return {"issues": issues}


def _read_changed_paths_from_stdin() -> list[str]:
    return [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]


def _collect_changed_paths_from_cli(args: argparse.Namespace) -> list[str]:
    if args.paths:
        return [path for path in args.paths if path.strip()]
    if args.stdin or not sys.stdin.isatty():
        return _read_changed_paths_from_stdin()
    return []


def _extract_suggested_commands(changed_paths: Iterable[str], drift_map_path: Path | None) -> dict[str, list[str]]:
    advice = collect_rerun_advice(
        changed_paths,
        allow_suggested_commands=True,
        drift_map_path=drift_map_path,
    )
    suggested = advice.get("suggested_commands")
    if not isinstance(suggested, dict):
        return {key: [] for key in RERUN_ADVICE_KEYS}
    return {key: list(suggested.get(key, [])) for key in RERUN_ADVICE_KEYS}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="根据变更路径输出 iteration_cycle 建议（JSON）。",
    )
    parser.add_argument(
        "--paths",
        nargs="+",
        help="变更路径列表（例如 git diff --name-only 的输出）",
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="从 stdin 读取变更路径（每行一个）",
    )
    parser.add_argument(
        "--drift-map",
        type=Path,
        default=DRIFT_MAP_PATH,
        help=f"指定 drift map 路径（默认: {DRIFT_MAP_PATH})",
    )

    args = parser.parse_args()

    changed_paths = _collect_changed_paths_from_cli(args)
    try:
        suggested = _extract_suggested_commands(changed_paths, args.drift_map)
    except (FileNotFoundError, ValueError) as exc:
        print(f"❌ Drift map 解析失败: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(suggested, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
