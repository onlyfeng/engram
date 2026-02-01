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
import sys
from dataclasses import dataclass
from typing import List, Literal

# gate_profile 类型定义
GateProfile = Literal["full", "regression", "docs-only", "ci-only", "gateway-only", "sql-only"]

# 支持的 profile 列表
SUPPORTED_PROFILES: List[GateProfile] = [
    "full",
    "regression",
    "docs-only",
    "ci-only",
    "gateway-only",
    "sql-only",
]


@dataclass
class GateCommand:
    """门禁命令定义。"""

    command: str
    check_item: str  # 检查项名称
    pass_criterion: str  # 通过标准


# ============================================================================
# 门禁命令定义（与 AGENTS.md 和 Makefile 对齐）
# ============================================================================

# 完整 CI 门禁（make ci 依赖链）
FULL_GATE_COMMANDS: List[GateCommand] = [
    GateCommand("make lint", "代码风格检查", "退出码 0"),
    GateCommand("make format-check", "格式检查", "退出码 0"),
    GateCommand("make typecheck", "mypy 类型检查", "退出码 0"),
    GateCommand("make check-schemas", "JSON Schema 校验", "退出码 0"),
    GateCommand("make check-env-consistency", "环境变量一致性检查", "退出码 0"),
    GateCommand("make check-logbook-consistency", "Logbook 配置一致性检查", "退出码 0"),
    GateCommand("make check-migration-sanity", "SQL 迁移文件检查", "退出码 0"),
    GateCommand("make check-scm-sync-consistency", "SCM Sync 一致性检查", "退出码 0"),
    GateCommand(
        "make check-gateway-error-reason-usage", "Gateway ErrorReason 使用规范检查", "退出码 0"
    ),
    GateCommand(
        "make check-gateway-public-api-surface", "Gateway Public API 导入表面检查", "退出码 0"
    ),
    GateCommand(
        "make check-gateway-public-api-docs-sync", "Gateway Public API 文档同步检查", "退出码 0"
    ),
    GateCommand("make check-gateway-di-boundaries", "Gateway DI 边界检查", "退出码 0"),
    GateCommand("make check-gateway-import-surface", "Gateway 懒加载策略检查", "退出码 0"),
    GateCommand(
        "make check-gateway-correlation-id-single-source",
        "Gateway correlation_id 单一来源检查",
        "退出码 0",
    ),
    GateCommand("make check-iteration-docs", "迭代文档规范检查", "退出码 0"),
    GateCommand("make validate-workflows-strict", "Workflow 合约校验", "退出码 0"),
    GateCommand("make check-workflow-contract-docs-sync", "Workflow 合约文档同步检查", "退出码 0"),
    GateCommand(
        "make check-workflow-contract-version-policy", "Workflow 合约版本策略检查", "退出码 0"
    ),
    GateCommand("make check-mcp-error-contract", "MCP 错误码合约检查", "退出码 0"),
    GateCommand("make check-mcp-error-docs-sync", "MCP 错误码文档同步检查", "退出码 0"),
]

# 回归最小集（用于迭代回归验证，与 iteration_N_regression.md 对齐）
REGRESSION_GATE_COMMANDS: List[GateCommand] = [
    GateCommand("make validate-workflows-strict", "Workflow 合约校验", "退出码 0"),
    GateCommand("make check-workflow-contract-docs-sync", "Workflow 合约文档同步检查", "退出码 0"),
    GateCommand(
        "make check-gateway-public-api-surface", "Gateway Public API 导入表面检查", "退出码 0"
    ),
    GateCommand(
        "make check-gateway-public-api-docs-sync", "Gateway Public API 文档同步检查", "退出码 0"
    ),
    GateCommand("make check-iteration-docs", "迭代文档规范检查", "退出码 0"),
    GateCommand("pytest tests/ci/ -q", "CI 脚本测试", "无 FAILED，退出码 0"),
]

# 文档代理门禁
DOCS_GATE_COMMANDS: List[GateCommand] = [
    GateCommand("make check-env-consistency", "环境变量一致性检查", "退出码 0"),
    GateCommand("make check-iteration-docs", "迭代文档规范检查", "退出码 0"),
]

# CI 代理门禁（与 AGENTS.md 对齐）
CI_GATE_COMMANDS: List[GateCommand] = [
    GateCommand("make typecheck", "mypy 类型检查", "退出码 0"),
    GateCommand("make validate-workflows-strict", "Workflow 合约校验", "退出码 0"),
    GateCommand("make check-workflow-contract-docs-sync", "Workflow 合约文档同步检查", "退出码 0"),
    GateCommand(
        "make check-workflow-contract-version-policy", "Workflow 合约版本策略检查", "退出码 0"
    ),
    GateCommand(
        "make check-workflow-contract-doc-anchors", "Workflow 合约文档锚点检查", "退出码 0"
    ),
]

# Gateway 代理门禁（与 AGENTS.md 对齐）
GATEWAY_GATE_COMMANDS: List[GateCommand] = [
    GateCommand("make lint", "代码风格检查", "退出码 0"),
    GateCommand("make check-gateway-di-boundaries", "Gateway DI 边界检查", "退出码 0"),
    GateCommand(
        "make check-gateway-public-api-surface", "Gateway Public API 导入表面检查", "退出码 0"
    ),
    GateCommand(
        "make check-gateway-public-api-docs-sync", "Gateway Public API 文档同步检查", "退出码 0"
    ),
    GateCommand("make check-gateway-import-surface", "Gateway 懒加载策略检查", "退出码 0"),
    GateCommand(
        "make check-gateway-correlation-id-single-source",
        "Gateway correlation_id 单一来源检查",
        "退出码 0",
    ),
    GateCommand("make check-mcp-error-contract", "MCP 错误码合约检查", "退出码 0"),
    GateCommand("make check-mcp-error-docs-sync", "MCP 错误码文档同步检查", "退出码 0"),
]

# SQL 代理门禁
SQL_GATE_COMMANDS: List[GateCommand] = [
    GateCommand("make check-migration-sanity", "SQL 迁移文件检查", "退出码 0"),
    GateCommand("make verify-permissions", "数据库权限验证", "退出码 0"),
]

# profile 到命令列表的映射
PROFILE_COMMANDS: dict[GateProfile, List[GateCommand]] = {
    "full": FULL_GATE_COMMANDS,
    "regression": REGRESSION_GATE_COMMANDS,
    "docs-only": DOCS_GATE_COMMANDS,
    "ci-only": CI_GATE_COMMANDS,
    "gateway-only": GATEWAY_GATE_COMMANDS,
    "sql-only": SQL_GATE_COMMANDS,
}

# profile 描述
PROFILE_DESCRIPTIONS: dict[GateProfile, str] = {
    "full": "完整 CI 门禁（make ci）",
    "regression": "回归最小集",
    "docs-only": "文档代理最小门禁",
    "ci-only": "CI 代理最小门禁",
    "gateway-only": "Gateway 代理最小门禁",
    "sql-only": "SQL 代理最小门禁",
}


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
