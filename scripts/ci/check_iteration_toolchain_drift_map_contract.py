#!/usr/bin/env python3
"""检查 iteration_toolchain_drift_map 合约中的命令可用性。"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from dataclasses import dataclass, field
from difflib import get_close_matches
from pathlib import Path

CONFIG_PATH = Path("configs/iteration_toolchain_drift_map.v2.json")
MAKEFILE_PATH = Path("Makefile")
TARGET_PATTERN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*):")

PYTHON_BINARIES = {"python", "python3"}


@dataclass
class DriftMapError:
    code: str
    message: str
    context: str
    hint: str = ""


@dataclass
class DriftMapResult:
    ok: bool = True
    errors: list[DriftMapError] = field(default_factory=list)
    checked_rules: list[str] = field(default_factory=list)
    checked_commands: int = 0

    def add_error(self, code: str, message: str, context: str, hint: str = "") -> None:
        self.errors.append(DriftMapError(code=code, message=message, context=context, hint=hint))
        self.ok = False


def load_config(project_root: Path) -> dict:
    path = project_root / CONFIG_PATH
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"未找到配置文件: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"配置文件 JSON 解析失败: {path}: {exc}") from exc


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


def parse_python_script_path(command: str) -> str | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens or tokens[0] not in PYTHON_BINARIES:
        return None
    if len(tokens) < 2:
        return None
    script = tokens[1]
    if script.startswith("scripts/"):
        return script
    return None


def parse_pytest_paths(command: str) -> list[str]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []
    if not tokens or tokens[0] != "pytest":
        return []
    paths: list[str] = []
    for token in tokens[1:]:
        if token.startswith("-"):
            continue
        candidate = token.split("::", 1)[0]
        if candidate.startswith("tests/"):
            paths.append(candidate)
    return paths


def suggest_make_target(target: str, make_targets: set[str]) -> str:
    matches = get_close_matches(target, sorted(make_targets), n=3)
    if matches:
        return f"可用相近目标: {', '.join(matches)}"
    return "请确认 Makefile 已定义该目标"


def suggest_path(path: Path, project_root: Path) -> str:
    target_path = project_root / path
    parent = target_path.parent
    if parent.exists():
        names = [entry.name for entry in parent.iterdir()]
        matches = get_close_matches(target_path.name, names, n=3)
        if matches:
            candidates = [str((parent / name).relative_to(project_root)) for name in matches]
            return f"可能的路径: {', '.join(candidates)}"
    search_root = project_root / (path.parts[0] if path.parts else "")
    if search_root.exists() and target_path.name:
        matches = [p for p in search_root.rglob(target_path.name)]
        if matches:
            candidates = ", ".join(str(p.relative_to(project_root)) for p in matches[:3])
            return f"可能的路径: {candidates}"
    return "请检查路径拼写或文件是否存在"


def validate_command(
    command: str,
    context: str,
    project_root: Path,
    make_targets: set[str],
    result: DriftMapResult,
) -> None:
    make_target = parse_make_target(command)
    if make_target:
        if make_target not in make_targets:
            result.add_error(
                "make_target_missing",
                f"Makefile 未定义目标: {make_target}",
                context,
                hint=suggest_make_target(make_target, make_targets),
            )
        return

    python_script = parse_python_script_path(command)
    if python_script:
        script_path = project_root / python_script
        if not script_path.exists():
            result.add_error(
                "python_script_missing",
                f"脚本路径不存在: {python_script}",
                context,
                hint=suggest_path(Path(python_script), project_root),
            )
        return

    pytest_paths = parse_pytest_paths(command)
    for pytest_path in pytest_paths:
        resolved = project_root / pytest_path
        if not resolved.exists():
            result.add_error(
                "pytest_path_missing",
                f"pytest 路径不存在: {pytest_path}",
                context,
                hint=suggest_path(Path(pytest_path), project_root),
            )


def validate_actions(payload: dict, project_root: Path, make_targets: set[str], result: DriftMapResult) -> None:
    if not isinstance(payload, dict):
        result.add_error("payload_invalid", "配置内容必须为 JSON object", "root", hint="检查配置文件结构")
        return
    rules = payload.get("rules")
    if not isinstance(rules, list) or not rules:
        result.add_error("rules_missing", "rules 必须为非空列表", "rules", hint="检查配置结构")
        return

    for idx, rule in enumerate(rules, 1):
        if not isinstance(rule, dict):
            result.add_error(
                "rule_invalid",
                "rule 必须为对象",
                f"rules[{idx}]",
                hint="检查 rules 配置结构",
            )
            continue
        rule_id = rule.get("id") if isinstance(rule.get("id"), str) else f"rules[{idx}]"
        result.checked_rules.append(str(rule_id))
        actions = rule.get("actions")
        if not isinstance(actions, dict):
            result.add_error(
                "actions_invalid",
                "actions 必须为对象",
                f"rules.{rule_id}.actions",
                hint="检查 actions 配置结构",
            )
            continue
        for action_name, commands in actions.items():
            if commands is None:
                continue
            if not isinstance(commands, list):
                result.add_error(
                    "action_commands_invalid",
                    "actions 下的命令必须为列表",
                    f"rules.{rule_id}.actions.{action_name}",
                    hint="请将命令改为字符串数组",
                )
                continue
            for command_index, command in enumerate(commands, 1):
                result.checked_commands += 1
                context = f"rules.{rule_id}.actions.{action_name}[{command_index}]"
                if not isinstance(command, str):
                    result.add_error(
                        "command_invalid",
                        "命令必须为字符串",
                        context,
                        hint="请检查命令类型或 JSON 格式",
                    )
                    continue
                validate_command(command, context, project_root, make_targets, result)


def format_text_output(result: DriftMapResult) -> str:
    lines = [
        "=" * 64,
        "Iteration Toolchain Drift Map Contract Check",
        "=" * 64,
        "",
        f"Checked rules: {len(result.checked_rules)}",
        f"Checked commands: {result.checked_commands}",
        f"Errors: {len(result.errors)}",
    ]
    if result.errors:
        lines.append("")
        lines.append("ERRORS:")
        for error in result.errors:
            lines.append(f"  [{error.code}] {error.context}")
            lines.append(f"    {error.message}")
            if error.hint:
                lines.append(f"    Fix: {error.hint}")
    return "\n".join(lines)


def format_json_output(result: DriftMapResult) -> str:
    payload = {
        "ok": result.ok,
        "checked_rules": result.checked_rules,
        "checked_commands": result.checked_commands,
        "error_count": len(result.errors),
        "errors": [
            {
                "code": e.code,
                "message": e.message,
                "context": e.context,
                "hint": e.hint,
            }
            for e in result.errors
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 iteration toolchain drift map 合约命令")
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
    make_targets = load_make_targets(project_root)

    result = DriftMapResult()
    validate_actions(payload, project_root, make_targets, result)

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
    except (FileNotFoundError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(2)
