#!/usr/bin/env python3
"""生成最小门禁命令块的 Markdown 内容。

用法:
    python scripts/iteration/render_min_gate_block.py <iteration_number> [--profile PROFILE]

示例:
    # 生成完整的门禁命令块
    python scripts/iteration/render_min_gate_block.py 13

    # 生成回归最小集（推荐用于迭代回归验证）
    python scripts/iteration/render_min_gate_block.py 13 --profile regression

    # 仅生成文档相关门禁
    python scripts/iteration/render_min_gate_block.py 13 --profile docs-only

    # 仅生成 CI 相关门禁
    python scripts/iteration/render_min_gate_block.py 13 --profile ci-only

    # 仅生成 Gateway 相关门禁
    python scripts/iteration/render_min_gate_block.py 13 --profile gateway-only

    # 仅生成 SQL 相关门禁
    python scripts/iteration/render_min_gate_block.py 13 --profile sql-only

功能:
    根据 gate_profile 生成标准的最小门禁命令块 Markdown，包含：
    - 命令表格（检查项、命令、通过标准）
    - 一键 bash 块
    - 通过标准说明

gate_profile 映射（与 AGENTS.md 子代理分工对齐）:
    - full: 完整 make ci（默认）
    - regression: 回归最小集（推荐用于迭代回归验证）
    - docs-only: 文档代理（check-env-consistency, check-iteration-docs）
    - ci-only: CI 代理（typecheck, validate-workflows-strict, check-workflow-contract-docs-sync, check-workflow-contract-version-policy, check-workflow-contract-doc-anchors）
    - gateway-only: Gateway 代理（lint + gateway 相关检查）
    - sql-only: SQL 代理（check-migration-sanity, verify-permissions）
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Literal, cast

# gate_profile 类型定义
GateProfile = Literal["full", "regression", "docs-only", "ci-only", "gateway-only", "sql-only"]
ALLOWED_PROFILES = ("full", "regression", "docs-only", "ci-only", "gateway-only", "sql-only")

# 配置文件路径
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = REPO_ROOT / "configs" / "iteration_gate_profiles.v1.json"


@dataclass
class GateCommand:
    """门禁命令定义。"""

    command: str
    check_item: str  # 检查项名称
    pass_criterion: str  # 通过标准


# ============================================================================
# 门禁命令定义（与 AGENTS.md 和 Makefile 对齐）
# ============================================================================


def _require_str(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"配置字段缺失或为空: {context}")
    return value


def _load_profiles_config(
    path: Path,
) -> tuple[List[GateProfile], dict[GateProfile, str], dict[GateProfile, List[GateCommand]]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"未找到门禁 profile 配置: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"门禁 profile 配置 JSON 解析失败: {path}: {exc}") from exc

    profiles = payload.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError(f"门禁 profile 配置缺少 profiles: {path}")

    order = payload.get("profiles_order")
    if not isinstance(order, list) or not order or not all(isinstance(p, str) for p in order):
        raise ValueError(f"门禁 profile 配置缺少 profiles_order 或格式错误: {path}")
    if len(order) != len(set(order)):
        raise ValueError(f"profiles_order 存在重复项: {path}")

    missing_in_order = set(profiles.keys()) - set(order)
    extra_in_order = set(order) - set(profiles.keys())
    if missing_in_order or extra_in_order:
        raise ValueError(
            f"profiles_order 与 profiles 不一致: missing={sorted(missing_in_order)}, "
            f"extra={sorted(extra_in_order)}"
        )

    missing_allowed = set(ALLOWED_PROFILES) - set(order)
    extra_allowed = set(order) - set(ALLOWED_PROFILES)
    if missing_allowed or extra_allowed:
        raise ValueError(
            "profiles_order 与内置 GateProfile 不一致: "
            f"missing={sorted(missing_allowed)}, extra={sorted(extra_allowed)}"
        )

    supported_profiles: List[GateProfile] = []
    profile_descriptions: dict[GateProfile, str] = {}
    profile_commands: dict[GateProfile, List[GateCommand]] = {}

    for profile in order:
        raw_profile = profiles.get(profile)
        if not isinstance(raw_profile, dict):
            raise ValueError(f"profile 定义格式错误: {profile}")

        description = _require_str(raw_profile.get("description"), f"{profile}.description")
        raw_commands = raw_profile.get("commands")
        if not isinstance(raw_commands, list) or not raw_commands:
            raise ValueError(f"profile commands 缺失或为空: {profile}")

        commands: List[GateCommand] = []
        for idx, raw_cmd in enumerate(raw_commands, 1):
            if not isinstance(raw_cmd, dict):
                raise ValueError(f"profile command 格式错误: {profile}[{idx}]")
            command = _require_str(raw_cmd.get("command"), f"{profile}.commands[{idx}].command")
            check_item = _require_str(raw_cmd.get("check_item"), f"{profile}.commands[{idx}].check_item")
            pass_criterion = _require_str(
                raw_cmd.get("pass_criterion"), f"{profile}.commands[{idx}].pass_criterion"
            )
            commands.append(
                GateCommand(
                    command=command,
                    check_item=check_item,
                    pass_criterion=pass_criterion,
                )
            )

        profile_key = cast(GateProfile, profile)
        supported_profiles.append(profile_key)
        profile_descriptions[profile_key] = description
        profile_commands[profile_key] = commands

    return supported_profiles, profile_descriptions, profile_commands


SUPPORTED_PROFILES, PROFILE_DESCRIPTIONS, PROFILE_COMMANDS = _load_profiles_config(CONFIG_PATH)

# 完整 CI 门禁（make ci 依赖链）
FULL_GATE_COMMANDS: List[GateCommand] = PROFILE_COMMANDS["full"]

# 回归最小集（用于迭代回归验证，与 iteration_N_regression.md 对齐）
REGRESSION_GATE_COMMANDS: List[GateCommand] = PROFILE_COMMANDS["regression"]

# 文档代理门禁
DOCS_GATE_COMMANDS: List[GateCommand] = PROFILE_COMMANDS["docs-only"]

# CI 代理门禁（与 AGENTS.md 对齐）
CI_GATE_COMMANDS: List[GateCommand] = PROFILE_COMMANDS["ci-only"]

# Gateway 代理门禁（与 AGENTS.md 对齐）
GATEWAY_GATE_COMMANDS: List[GateCommand] = PROFILE_COMMANDS["gateway-only"]

# SQL 代理门禁
SQL_GATE_COMMANDS: List[GateCommand] = PROFILE_COMMANDS["sql-only"]


# ============================================================================
# 渲染函数
# ============================================================================

# 自动生成标识行（机器可识别）
AUTO_GENERATED_MARKER = "<!-- AUTO-GENERATED BY render_min_gate_block.py -->"


def render_command_table(commands: List[GateCommand]) -> str:
    """渲染命令表格。

    表格格式与 iteration_N_regression.md 对齐：
    | 序号 | 检查项 | 命令 | 通过标准 |

    Args:
        commands: 门禁命令列表

    Returns:
        Markdown 表格字符串
    """
    lines = [
        "| 序号 | 检查项 | 命令 | 通过标准 |",
        "|------|--------|------|----------|",
    ]

    for i, cmd in enumerate(commands, 1):
        lines.append(f"| {i} | {cmd.check_item} | `{cmd.command}` | {cmd.pass_criterion} |")

    return "\n".join(lines)


def render_bash_block(commands: List[GateCommand]) -> str:
    """渲染一键 bash 块。

    Args:
        commands: 门禁命令列表

    Returns:
        bash 代码块字符串
    """
    cmd_list = [cmd.command for cmd in commands]
    bash_content = " && \\\n  ".join(cmd_list)

    return f"""```bash
# 一键运行所有门禁（需全部通过）
{bash_content}
```"""


def render_pass_criteria(commands: List[GateCommand]) -> str:
    """渲染通过标准说明。

    Args:
        commands: 门禁命令列表

    Returns:
        通过标准说明字符串
    """
    lines = ["**通过标准**：每个命令需满足对应的通过条件。", ""]

    for cmd in commands:
        lines.append(f"- `{cmd.command}` → {cmd.pass_criterion}")

    return "\n".join(lines)


def render_min_gate_block(
    iteration_number: int,
    profile: GateProfile = "full",
) -> str:
    """渲染最小门禁命令块。

    Args:
        iteration_number: 迭代编号
        profile: 门禁 profile（full/regression/docs-only/ci-only/gateway-only/sql-only）

    Returns:
        完整的 Markdown 内容
    """
    commands = PROFILE_COMMANDS[profile]
    profile_desc = PROFILE_DESCRIPTIONS[profile]

    sections = [
        "## 最小门禁命令块",
        "",
        AUTO_GENERATED_MARKER,
        "",
        f"> **Iteration {iteration_number}** - {profile_desc}",
        ">",
        f"> 此段落由脚本自动生成：`python scripts/iteration/render_min_gate_block.py {iteration_number} --profile {profile}`",
        "",
        "### 命令表格",
        "",
        render_command_table(commands),
        "",
        "### 一键执行",
        "",
        render_bash_block(commands),
        "",
        "### 通过标准",
        "",
        render_pass_criteria(commands),
        "",
    ]

    return "\n".join(sections)


def get_commands_for_profile(profile: GateProfile) -> List[GateCommand]:
    """获取指定 profile 的命令列表。

    Args:
        profile: 门禁 profile

    Returns:
        命令列表
    """
    return PROFILE_COMMANDS[profile]


# ============================================================================
# CLI 入口
# ============================================================================


def main() -> int:
    """主函数。"""
    parser = argparse.ArgumentParser(
        description="生成最小门禁命令块的 Markdown 内容",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
支持的 profile:
  full         完整 CI 门禁（make ci）
  regression   回归最小集（推荐用于迭代回归验证）
  docs-only    文档代理最小门禁
  ci-only      CI 代理最小门禁
  gateway-only Gateway 代理最小门禁
  sql-only     SQL 代理最小门禁

示例:
    # 生成 Iteration 13 的完整门禁命令块
    python scripts/iteration/render_min_gate_block.py 13

    # 生成 Iteration 13 的回归最小集（推荐）
    python scripts/iteration/render_min_gate_block.py 13 --profile regression

    # 生成 Iteration 13 的文档代理门禁命令块
    python scripts/iteration/render_min_gate_block.py 13 --profile docs-only
        """,
    )
    parser.add_argument(
        "iteration_number",
        type=int,
        help="迭代编号",
    )
    parser.add_argument(
        "--profile",
        "-p",
        type=str,
        choices=SUPPORTED_PROFILES,
        default="full",
        help="门禁 profile（默认: full）",
    )

    args = parser.parse_args()

    # 渲染并输出
    output = render_min_gate_block(args.iteration_number, args.profile)
    print(output)

    return 0


if __name__ == "__main__":
    sys.exit(main())
