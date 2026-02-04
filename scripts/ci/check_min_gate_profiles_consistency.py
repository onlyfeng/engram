#!/usr/bin/env python3
"""最小门禁 profile 与渲染/Makefile 一致性检查。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path("configs/iteration_gate_profiles.v2.json")
MAKEFILE_PATH = Path("Makefile")
TARGET_PATTERN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*):")


@dataclass
class ConsistencyError:
    code: str
    message: str
    context: str


@dataclass
class ConsistencyResult:
    ok: bool = True
    errors: list[ConsistencyError] = field(default_factory=list)
    checked_profiles: list[str] = field(default_factory=list)

    def add_error(self, code: str, message: str, context: str) -> None:
        self.errors.append(ConsistencyError(code=code, message=message, context=context))
        self.ok = False


def load_config(project_root: Path) -> dict:
    path = project_root / CONFIG_PATH
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"未找到配置文件: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"配置文件 JSON 解析失败: {path}: {exc}") from exc


def extract_profiles(payload: dict) -> tuple[list[str], dict]:
    profiles = payload.get("profiles")
    order = payload.get("profiles_order")
    if not isinstance(profiles, dict) or not isinstance(order, list):
        raise ValueError("配置缺少 profiles 或 profiles_order")
    return order, profiles


def load_make_targets(project_root: Path) -> set[str]:
    path = project_root / MAKEFILE_PATH
    targets: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        match = TARGET_PATTERN.match(line)
        if not match:
            continue
        target = match.group(1)
        if target.startswith("."):
            continue
        targets.add(target)
    return targets


def parse_make_target(command: str) -> str | None:
    match = re.match(r"^make\s+([^\s]+)", command)
    if not match:
        return None
    return match.group(1)


def load_renderer_profiles() -> tuple[list[str], dict[str, str], dict[str, list[tuple[str, str, str]]]]:
    script_dir = Path(__file__).resolve().parent
    iteration_dir = script_dir.parent / "iteration"
    sys.path.insert(0, str(iteration_dir))
    try:
        import render_min_gate_block as renderer  # noqa: WPS433
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"无法加载 render_min_gate_block: {exc}") from exc

    profile_order = list(renderer.SUPPORTED_PROFILES)
    descriptions = {key: renderer.PROFILE_DESCRIPTIONS[key] for key in profile_order}
    commands = {
        key: [(cmd.command, cmd.check_item, cmd.pass_criterion) for cmd in renderer.PROFILE_COMMANDS[key]]
        for key in profile_order
    }
    return profile_order, descriptions, commands


def check_profiles_consistency(
    config_order: list[str],
    config_profiles: dict,
    renderer_order: list[str],
    renderer_descriptions: dict[str, str],
    renderer_commands: dict[str, list[tuple[str, str, str]]],
    result: ConsistencyResult,
) -> None:
    if config_order != renderer_order:
        result.add_error(
            "profile_order_mismatch",
            f"profiles_order 与渲染顺序不一致: config={config_order}, render={renderer_order}",
            "profiles_order",
        )

    for profile in config_order:
        result.checked_profiles.append(profile)
        config_profile = config_profiles.get(profile, {})
        description = config_profile.get("description")
        if description != renderer_descriptions.get(profile):
            result.add_error(
                "profile_description_mismatch",
                f"description 不一致: config='{description}', render='{renderer_descriptions.get(profile)}'",
                f"profiles.{profile}.description",
            )

        config_commands = [
            (entry.get("command"), entry.get("check_item"), entry.get("pass_criterion"))
            for entry in config_profile.get("commands", [])
        ]
        render_commands = renderer_commands.get(profile, [])
        if config_commands != render_commands:
            result.add_error(
                "profile_commands_mismatch",
                "commands 与渲染输出不一致",
                f"profiles.{profile}.commands",
            )


def check_make_targets(
    config_order: list[str],
    config_profiles: dict,
    make_targets: set[str],
    result: ConsistencyResult,
) -> None:
    for profile in config_order:
        config_profile = config_profiles.get(profile, {})
        commands = config_profile.get("commands", [])
        for entry in commands:
            command = entry.get("command")
            if not isinstance(command, str):
                continue
            make_target = parse_make_target(command)
            if make_target and make_target not in make_targets:
                result.add_error(
                    "make_target_missing",
                    f"Makefile 未定义目标: {make_target}",
                    f"profiles.{profile}.commands",
                )


def format_text_output(result: ConsistencyResult) -> str:
    lines = [
        "=" * 64,
        "Min Gate Profiles Consistency Check",
        "=" * 64,
        "",
        f"Checked profiles: {len(result.checked_profiles)}",
        f"Errors: {len(result.errors)}",
    ]
    if result.errors:
        lines.append("")
        lines.append("ERRORS:")
        for error in result.errors:
            lines.append(f"  [{error.code}] {error.context}")
            lines.append(f"    {error.message}")
    return "\n".join(lines)


def format_json_output(result: ConsistencyResult) -> str:
    payload = {
        "ok": result.ok,
        "checked_profiles": result.checked_profiles,
        "error_count": len(result.errors),
        "errors": [
            {"code": e.code, "message": e.message, "context": e.context} for e in result.errors
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="检查最小门禁 profile 一致性")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    parser.add_argument("--verbose", action="store_true", help="显示详细信息")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="项目根目录（默认自动检测）",
    )
    args = parser.parse_args()

    project_root = args.project_root or Path(__file__).resolve().parent.parent.parent
    payload = load_config(project_root)
    config_order, config_profiles = extract_profiles(payload)
    make_targets = load_make_targets(project_root)
    renderer_order, renderer_descriptions, renderer_commands = load_renderer_profiles()

    result = ConsistencyResult()
    check_profiles_consistency(
        config_order,
        config_profiles,
        renderer_order,
        renderer_descriptions,
        renderer_commands,
        result,
    )
    check_make_targets(config_order, config_profiles, make_targets, result)

    if args.json:
        print(format_json_output(result))
    else:
        if args.verbose:
            print(f"Project root: {project_root}")
            print(f"Config path: {project_root / CONFIG_PATH}")
            print(f"Makefile path: {project_root / MAKEFILE_PATH}")
            print("")
        print(format_text_output(result))

    return 0 if result.ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(2)
