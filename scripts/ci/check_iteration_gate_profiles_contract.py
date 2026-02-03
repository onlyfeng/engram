#!/usr/bin/env python3
"""迭代门禁 profile 合约检查。"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path("configs/iteration_gate_profiles.v1.json")


@dataclass
class ContractError:
    code: str
    message: str
    field: str


@dataclass
class ContractResult:
    ok: bool = True
    errors: list[ContractError] = field(default_factory=list)
    checked_profiles: list[str] = field(default_factory=list)

    def add_error(self, code: str, message: str, field_path: str) -> None:
        self.errors.append(ContractError(code=code, message=message, field=field_path))
        self.ok = False


def _is_non_empty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def load_config(project_root: Path) -> dict:
    path = project_root / CONFIG_PATH
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"未找到配置文件: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"配置文件 JSON 解析失败: {path}: {exc}") from exc


def validate_config(payload: dict, result: ContractResult) -> None:
    if not isinstance(payload, dict):
        result.add_error("invalid_payload", "配置内容必须为 JSON object", "root")
        return

    version = payload.get("version")
    if not _is_non_empty_str(version):
        result.add_error("missing_version", "version 必须为非空字符串", "version")

    default_profile = payload.get("default_profile")
    default_profile_value: str | None = None
    if not _is_non_empty_str(default_profile):
        result.add_error("missing_default_profile", "default_profile 必须为非空字符串", "default_profile")
    else:
        default_profile_value = default_profile.strip()

    profiles = payload.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        result.add_error("missing_profiles", "profiles 必须为非空对象", "profiles")
        return

    order = payload.get("profiles_order")
    if not isinstance(order, list) or not order or not all(isinstance(p, str) for p in order):
        result.add_error("missing_profiles_order", "profiles_order 必须为非空字符串列表", "profiles_order")
        return

    if len(order) != len(set(order)):
        result.add_error("profiles_order_duplicate", "profiles_order 存在重复项", "profiles_order")

    missing_in_order = set(profiles.keys()) - set(order)
    extra_in_order = set(order) - set(profiles.keys())
    if missing_in_order:
        result.add_error(
            "profiles_order_missing",
            f"profiles_order 缺少: {sorted(missing_in_order)}",
            "profiles_order",
        )
    if extra_in_order:
        result.add_error(
            "profiles_order_extra",
            f"profiles_order 多余: {sorted(extra_in_order)}",
            "profiles_order",
        )

    if default_profile_value and isinstance(profiles, dict):
        if default_profile_value not in profiles:
            result.add_error(
                "default_profile_missing",
                f"default_profile 未在 profiles 中定义: {default_profile_value}",
                "default_profile",
            )
    if default_profile_value and isinstance(order, list):
        if default_profile_value not in order:
            result.add_error(
                "default_profile_not_in_order",
                f"default_profile 未在 profiles_order 中出现: {default_profile_value}",
                "default_profile",
            )

    for profile in order:
        raw_profile = profiles.get(profile)
        result.checked_profiles.append(profile)
        if not isinstance(raw_profile, dict):
            result.add_error("profile_invalid", "profile 定义必须为对象", f"profiles.{profile}")
            continue

        if not _is_non_empty_str(raw_profile.get("description")):
            result.add_error(
                "profile_description_missing",
                "description 必须为非空字符串",
                f"profiles.{profile}.description",
            )

        commands = raw_profile.get("commands")
        if not isinstance(commands, list) or not commands:
            result.add_error(
                "profile_commands_missing",
                "commands 必须为非空列表",
                f"profiles.{profile}.commands",
            )
            continue

        command_names: list[str] = []
        for idx, entry in enumerate(commands, 1):
            if not isinstance(entry, dict):
                result.add_error(
                    "command_invalid",
                    "command 必须为对象",
                    f"profiles.{profile}.commands[{idx}]",
                )
                continue
            command = entry.get("command")
            if not _is_non_empty_str(command):
                result.add_error(
                    "command_missing",
                    "command 必须为非空字符串",
                    f"profiles.{profile}.commands[{idx}].command",
                )
            else:
                command_names.append(command.strip())

            if not _is_non_empty_str(entry.get("check_item")):
                result.add_error(
                    "check_item_missing",
                    "check_item 必须为非空字符串",
                    f"profiles.{profile}.commands[{idx}].check_item",
                )
            if not _is_non_empty_str(entry.get("pass_criterion")):
                result.add_error(
                    "pass_criterion_missing",
                    "pass_criterion 必须为非空字符串",
                    f"profiles.{profile}.commands[{idx}].pass_criterion",
                )

        duplicates = [name for name, count in Counter(command_names).items() if count > 1]
        if duplicates:
            result.add_error(
                "command_duplicate",
                f"commands 存在重复项: {duplicates}",
                f"profiles.{profile}.commands",
            )


def format_text_output(result: ContractResult) -> str:
    lines = [
        "=" * 64,
        "Iteration Gate Profiles Contract Check",
        "=" * 64,
        "",
        f"Checked profiles: {len(result.checked_profiles)}",
        f"Errors: {len(result.errors)}",
    ]
    if result.errors:
        lines.append("")
        lines.append("ERRORS:")
        for error in result.errors:
            lines.append(f"  [{error.code}] {error.field}")
            lines.append(f"    {error.message}")
    return "\n".join(lines)


def format_json_output(result: ContractResult) -> str:
    payload = {
        "ok": result.ok,
        "checked_profiles": result.checked_profiles,
        "error_count": len(result.errors),
        "errors": [
            {"code": e.code, "message": e.message, "field": e.field} for e in result.errors
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="检查迭代门禁 profile 合约")
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

    result = ContractResult()
    validate_config(payload, result)

    if args.json:
        print(format_json_output(result))
    else:
        if args.verbose:
            print(f"Project root: {project_root}")
            print(f"Config path: {project_root / CONFIG_PATH}")
            print("")
        print(format_text_output(result))

    return 0 if result.ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (FileNotFoundError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(2)
