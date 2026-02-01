#!/usr/bin/env python3
"""
render_min_gate_block.py 单元测试

覆盖功能:
1. 各 profile 的命令表格渲染
2. 一键 bash 块渲染
3. 通过标准渲染
4. 完整输出快照测试（确保输出稳定）
5. CLI 入口测试
"""

from __future__ import annotations

import sys
from pathlib import Path

# 添加脚本目录到 path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "iteration"))

from render_min_gate_block import (  # noqa: E402
    AUTO_GENERATED_MARKER,
    CI_GATE_COMMANDS,
    DOCS_GATE_COMMANDS,
    FULL_GATE_COMMANDS,
    GATEWAY_GATE_COMMANDS,
    PROFILE_COMMANDS,
    PROFILE_DESCRIPTIONS,
    REGRESSION_GATE_COMMANDS,
    SQL_GATE_COMMANDS,
    SUPPORTED_PROFILES,
    GateCommand,
    get_commands_for_profile,
    render_bash_block,
    render_command_table,
    render_min_gate_block,
    render_pass_criteria,
)

# ============================================================================
# 基础测试
# ============================================================================


class TestGateCommand:
    """GateCommand 数据类测试"""

    def test_gate_command_creation(self):
        """测试 GateCommand 创建"""
        cmd = GateCommand(
            command="make lint",
            check_item="代码风格检查",
            pass_criterion="退出码 0",
        )
        assert cmd.command == "make lint"
        assert cmd.check_item == "代码风格检查"
        assert cmd.pass_criterion == "退出码 0"


class TestProfileDefinitions:
    """Profile 定义测试"""

    def test_supported_profiles(self):
        """测试支持的 profile 列表"""
        assert "full" in SUPPORTED_PROFILES
        assert "regression" in SUPPORTED_PROFILES
        assert "docs-only" in SUPPORTED_PROFILES
        assert "ci-only" in SUPPORTED_PROFILES
        assert "gateway-only" in SUPPORTED_PROFILES
        assert "sql-only" in SUPPORTED_PROFILES

    def test_profile_commands_mapping(self):
        """测试 profile 到命令列表的映射"""
        for profile in SUPPORTED_PROFILES:
            assert profile in PROFILE_COMMANDS
            assert len(PROFILE_COMMANDS[profile]) > 0

    def test_profile_descriptions(self):
        """测试 profile 描述"""
        for profile in SUPPORTED_PROFILES:
            assert profile in PROFILE_DESCRIPTIONS
            assert len(PROFILE_DESCRIPTIONS[profile]) > 0

    def test_full_profile_has_most_commands(self):
        """测试 full profile 包含最多命令"""
        full_count = len(FULL_GATE_COMMANDS)
        for profile in ["regression", "docs-only", "ci-only", "gateway-only", "sql-only"]:
            assert len(PROFILE_COMMANDS[profile]) <= full_count


class TestGetCommandsForProfile:
    """get_commands_for_profile 函数测试"""

    def test_returns_correct_commands_for_full(self):
        """测试 full profile 返回正确命令"""
        commands = get_commands_for_profile("full")
        assert commands == FULL_GATE_COMMANDS

    def test_returns_correct_commands_for_regression(self):
        """测试 regression profile 返回正确命令"""
        commands = get_commands_for_profile("regression")
        assert commands == REGRESSION_GATE_COMMANDS

    def test_returns_correct_commands_for_docs_only(self):
        """测试 docs-only profile 返回正确命令"""
        commands = get_commands_for_profile("docs-only")
        assert commands == DOCS_GATE_COMMANDS

    def test_returns_correct_commands_for_ci_only(self):
        """测试 ci-only profile 返回正确命令"""
        commands = get_commands_for_profile("ci-only")
        assert commands == CI_GATE_COMMANDS

    def test_returns_correct_commands_for_gateway_only(self):
        """测试 gateway-only profile 返回正确命令"""
        commands = get_commands_for_profile("gateway-only")
        assert commands == GATEWAY_GATE_COMMANDS

    def test_returns_correct_commands_for_sql_only(self):
        """测试 sql-only profile 返回正确命令"""
        commands = get_commands_for_profile("sql-only")
        assert commands == SQL_GATE_COMMANDS


# ============================================================================
# 渲染函数测试
# ============================================================================


class TestRenderCommandTable:
    """render_command_table 函数测试"""

    def test_renders_table_header(self):
        """测试渲染表格头部"""
        commands = [GateCommand("make test", "测试检查", "退出码 0")]
        result = render_command_table(commands)

        assert "| 序号 | 检查项 | 命令 | 通过标准 |" in result
        assert "|------|--------|------|----------|" in result

    def test_renders_command_rows(self):
        """测试渲染命令行"""
        commands = [
            GateCommand("make lint", "代码风格检查", "退出码 0"),
            GateCommand("make test", "运行测试", "无 FAILED"),
        ]
        result = render_command_table(commands)

        assert "| 1 | 代码风格检查 | `make lint` | 退出码 0 |" in result
        assert "| 2 | 运行测试 | `make test` | 无 FAILED |" in result

    def test_sequential_numbering(self):
        """测试序号递增"""
        commands = [
            GateCommand("cmd1", "检查1", "退出码 0"),
            GateCommand("cmd2", "检查2", "退出码 0"),
            GateCommand("cmd3", "检查3", "退出码 0"),
        ]
        result = render_command_table(commands)

        assert "| 1 |" in result
        assert "| 2 |" in result
        assert "| 3 |" in result


class TestRenderBashBlock:
    """render_bash_block 函数测试"""

    def test_renders_bash_code_block(self):
        """测试渲染 bash 代码块"""
        commands = [GateCommand("make lint", "代码风格检查", "退出码 0")]
        result = render_bash_block(commands)

        assert result.startswith("```bash")
        assert result.endswith("```")

    def test_renders_comment(self):
        """测试渲染注释"""
        commands = [GateCommand("make lint", "代码风格检查", "退出码 0")]
        result = render_bash_block(commands)

        assert "# 一键运行所有门禁" in result

    def test_chains_multiple_commands(self):
        """测试多命令链接"""
        commands = [
            GateCommand("make lint", "代码风格检查", "退出码 0"),
            GateCommand("make test", "运行测试", "退出码 0"),
        ]
        result = render_bash_block(commands)

        assert "make lint && \\" in result
        assert "make test" in result

    def test_single_command_no_continuation(self):
        """测试单命令无续行符"""
        commands = [GateCommand("make lint", "代码风格检查", "退出码 0")]
        result = render_bash_block(commands)

        # 单命令时不应有 && \\
        lines = result.split("\n")
        cmd_line = [line for line in lines if "make lint" in line][0]
        assert "&& \\" not in cmd_line


class TestRenderPassCriteria:
    """render_pass_criteria 函数测试"""

    def test_renders_header(self):
        """测试渲染标题"""
        commands = [GateCommand("make lint", "代码风格检查", "退出码 0")]
        result = render_pass_criteria(commands)

        assert "**通过标准**" in result

    def test_renders_criterion_mappings(self):
        """测试渲染通过标准映射"""
        commands = [
            GateCommand("make lint", "代码风格检查", "退出码 0"),
            GateCommand("make test", "运行测试", "无 FAILED"),
        ]
        result = render_pass_criteria(commands)

        assert "- `make lint` → 退出码 0" in result
        assert "- `make test` → 无 FAILED" in result


# ============================================================================
# 完整渲染测试
# ============================================================================


class TestRenderMinGateBlock:
    """render_min_gate_block 函数测试"""

    def test_renders_section_header(self):
        """测试渲染段落标题"""
        result = render_min_gate_block(13, "full")
        assert "## 最小门禁命令块" in result

    def test_renders_auto_generated_marker(self):
        """测试渲染自动生成标识行"""
        result = render_min_gate_block(13, "full")
        assert AUTO_GENERATED_MARKER in result

    def test_renders_iteration_number(self):
        """测试渲染迭代编号"""
        result = render_min_gate_block(13, "full")
        assert "**Iteration 13**" in result

    def test_renders_profile_description(self):
        """测试渲染 profile 描述"""
        result = render_min_gate_block(13, "docs-only")
        assert "文档代理最小门禁" in result

    def test_renders_regression_profile_description(self):
        """测试渲染 regression profile 描述"""
        result = render_min_gate_block(13, "regression")
        assert "回归最小集" in result

    def test_renders_script_generation_notice(self):
        """测试渲染脚本生成提示"""
        result = render_min_gate_block(13, "full")
        assert "此段落由脚本自动生成" in result
        assert "render_min_gate_block.py" in result

    def test_includes_command_table(self):
        """测试包含命令表格"""
        result = render_min_gate_block(13, "full")
        assert "### 命令表格" in result
        assert "| 序号 | 检查项 | 命令 | 通过标准 |" in result

    def test_includes_bash_block(self):
        """测试包含 bash 块"""
        result = render_min_gate_block(13, "full")
        assert "### 一键执行" in result
        assert "```bash" in result

    def test_includes_pass_criteria(self):
        """测试包含通过标准"""
        result = render_min_gate_block(13, "full")
        assert "### 通过标准" in result
        assert "**通过标准**" in result


# ============================================================================
# 快照测试（确保输出稳定）
# ============================================================================


class TestOutputStability:
    """输出稳定性快照测试"""

    # full profile 快照
    FULL_SNAPSHOT = """## 最小门禁命令块

<!-- AUTO-GENERATED BY render_min_gate_block.py -->

> **Iteration 13** - 完整 CI 门禁（make ci）
>
> 此段落由脚本自动生成：`python scripts/iteration/render_min_gate_block.py 13 --profile full`

### 命令表格

| 序号 | 检查项 | 命令 | 通过标准 |
|------|--------|------|----------|
| 1 | 代码风格检查 | `make lint` | 退出码 0 |
| 2 | 格式检查 | `make format-check` | 退出码 0 |
| 3 | mypy 类型检查 | `make typecheck` | 退出码 0 |
| 4 | JSON Schema 校验 | `make check-schemas` | 退出码 0 |
| 5 | 环境变量一致性检查 | `make check-env-consistency` | 退出码 0 |
| 6 | Logbook 配置一致性检查 | `make check-logbook-consistency` | 退出码 0 |
| 7 | SQL 迁移文件检查 | `make check-migration-sanity` | 退出码 0 |
| 8 | SCM Sync 一致性检查 | `make check-scm-sync-consistency` | 退出码 0 |
| 9 | Gateway ErrorReason 使用规范检查 | `make check-gateway-error-reason-usage` | 退出码 0 |
| 10 | Gateway Public API 导入表面检查 | `make check-gateway-public-api-surface` | 退出码 0 |
| 11 | Gateway Public API 文档同步检查 | `make check-gateway-public-api-docs-sync` | 退出码 0 |
| 12 | Gateway DI 边界检查 | `make check-gateway-di-boundaries` | 退出码 0 |
| 13 | Gateway 懒加载策略检查 | `make check-gateway-import-surface` | 退出码 0 |
| 14 | Gateway correlation_id 单一来源检查 | `make check-gateway-correlation-id-single-source` | 退出码 0 |
| 15 | 迭代文档规范检查 | `make check-iteration-docs` | 退出码 0 |
| 16 | Workflow 合约校验 | `make validate-workflows-strict` | 退出码 0 |
| 17 | Workflow 合约文档同步检查 | `make check-workflow-contract-docs-sync` | 退出码 0 |
| 18 | Workflow 合约版本策略检查 | `make check-workflow-contract-version-policy` | 退出码 0 |
| 19 | MCP 错误码合约检查 | `make check-mcp-error-contract` | 退出码 0 |
| 20 | MCP 错误码文档同步检查 | `make check-mcp-error-docs-sync` | 退出码 0 |

### 一键执行

```bash
# 一键运行所有门禁（需全部通过）
make lint && \\
  make format-check && \\
  make typecheck && \\
  make check-schemas && \\
  make check-env-consistency && \\
  make check-logbook-consistency && \\
  make check-migration-sanity && \\
  make check-scm-sync-consistency && \\
  make check-gateway-error-reason-usage && \\
  make check-gateway-public-api-surface && \\
  make check-gateway-public-api-docs-sync && \\
  make check-gateway-di-boundaries && \\
  make check-gateway-import-surface && \\
  make check-gateway-correlation-id-single-source && \\
  make check-iteration-docs && \\
  make validate-workflows-strict && \\
  make check-workflow-contract-docs-sync && \\
  make check-workflow-contract-version-policy && \\
  make check-mcp-error-contract && \\
  make check-mcp-error-docs-sync
```

### 通过标准

**通过标准**：每个命令需满足对应的通过条件。

- `make lint` → 退出码 0
- `make format-check` → 退出码 0
- `make typecheck` → 退出码 0
- `make check-schemas` → 退出码 0
- `make check-env-consistency` → 退出码 0
- `make check-logbook-consistency` → 退出码 0
- `make check-migration-sanity` → 退出码 0
- `make check-scm-sync-consistency` → 退出码 0
- `make check-gateway-error-reason-usage` → 退出码 0
- `make check-gateway-public-api-surface` → 退出码 0
- `make check-gateway-public-api-docs-sync` → 退出码 0
- `make check-gateway-di-boundaries` → 退出码 0
- `make check-gateway-import-surface` → 退出码 0
- `make check-gateway-correlation-id-single-source` → 退出码 0
- `make check-iteration-docs` → 退出码 0
- `make validate-workflows-strict` → 退出码 0
- `make check-workflow-contract-docs-sync` → 退出码 0
- `make check-workflow-contract-version-policy` → 退出码 0
- `make check-mcp-error-contract` → 退出码 0
- `make check-mcp-error-docs-sync` → 退出码 0
"""

    # regression profile 快照
    REGRESSION_SNAPSHOT = """## 最小门禁命令块

<!-- AUTO-GENERATED BY render_min_gate_block.py -->

> **Iteration 13** - 回归最小集
>
> 此段落由脚本自动生成：`python scripts/iteration/render_min_gate_block.py 13 --profile regression`

### 命令表格

| 序号 | 检查项 | 命令 | 通过标准 |
|------|--------|------|----------|
| 1 | Workflow 合约校验 | `make validate-workflows-strict` | 退出码 0 |
| 2 | Workflow 合约文档同步检查 | `make check-workflow-contract-docs-sync` | 退出码 0 |
| 3 | Gateway Public API 导入表面检查 | `make check-gateway-public-api-surface` | 退出码 0 |
| 4 | Gateway Public API 文档同步检查 | `make check-gateway-public-api-docs-sync` | 退出码 0 |
| 5 | 迭代文档规范检查 | `make check-iteration-docs` | 退出码 0 |
| 6 | CI 脚本测试 | `pytest tests/ci/ -q` | 无 FAILED，退出码 0 |

### 一键执行

```bash
# 一键运行所有门禁（需全部通过）
make validate-workflows-strict && \\
  make check-workflow-contract-docs-sync && \\
  make check-gateway-public-api-surface && \\
  make check-gateway-public-api-docs-sync && \\
  make check-iteration-docs && \\
  pytest tests/ci/ -q
```

### 通过标准

**通过标准**：每个命令需满足对应的通过条件。

- `make validate-workflows-strict` → 退出码 0
- `make check-workflow-contract-docs-sync` → 退出码 0
- `make check-gateway-public-api-surface` → 退出码 0
- `make check-gateway-public-api-docs-sync` → 退出码 0
- `make check-iteration-docs` → 退出码 0
- `pytest tests/ci/ -q` → 无 FAILED，退出码 0
"""

    # docs-only profile 快照
    DOCS_ONLY_SNAPSHOT = """## 最小门禁命令块

<!-- AUTO-GENERATED BY render_min_gate_block.py -->

> **Iteration 13** - 文档代理最小门禁
>
> 此段落由脚本自动生成：`python scripts/iteration/render_min_gate_block.py 13 --profile docs-only`

### 命令表格

| 序号 | 检查项 | 命令 | 通过标准 |
|------|--------|------|----------|
| 1 | 环境变量一致性检查 | `make check-env-consistency` | 退出码 0 |
| 2 | 迭代文档规范检查 | `make check-iteration-docs` | 退出码 0 |

### 一键执行

```bash
# 一键运行所有门禁（需全部通过）
make check-env-consistency && \\
  make check-iteration-docs
```

### 通过标准

**通过标准**：每个命令需满足对应的通过条件。

- `make check-env-consistency` → 退出码 0
- `make check-iteration-docs` → 退出码 0
"""

    # ci-only profile 快照
    CI_ONLY_SNAPSHOT = """## 最小门禁命令块

<!-- AUTO-GENERATED BY render_min_gate_block.py -->

> **Iteration 13** - CI 代理最小门禁
>
> 此段落由脚本自动生成：`python scripts/iteration/render_min_gate_block.py 13 --profile ci-only`

### 命令表格

| 序号 | 检查项 | 命令 | 通过标准 |
|------|--------|------|----------|
| 1 | mypy 类型检查 | `make typecheck` | 退出码 0 |
| 2 | Workflow 合约校验 | `make validate-workflows-strict` | 退出码 0 |
| 3 | Workflow 合约文档同步检查 | `make check-workflow-contract-docs-sync` | 退出码 0 |
| 4 | Workflow 合约版本策略检查 | `make check-workflow-contract-version-policy` | 退出码 0 |
| 5 | Workflow 合约文档锚点检查 | `make check-workflow-contract-doc-anchors` | 退出码 0 |

### 一键执行

```bash
# 一键运行所有门禁（需全部通过）
make typecheck && \\
  make validate-workflows-strict && \\
  make check-workflow-contract-docs-sync && \\
  make check-workflow-contract-version-policy && \\
  make check-workflow-contract-doc-anchors
```

### 通过标准

**通过标准**：每个命令需满足对应的通过条件。

- `make typecheck` → 退出码 0
- `make validate-workflows-strict` → 退出码 0
- `make check-workflow-contract-docs-sync` → 退出码 0
- `make check-workflow-contract-version-policy` → 退出码 0
- `make check-workflow-contract-doc-anchors` → 退出码 0
"""

    # sql-only profile 快照
    SQL_ONLY_SNAPSHOT = """## 最小门禁命令块

<!-- AUTO-GENERATED BY render_min_gate_block.py -->

> **Iteration 13** - SQL 代理最小门禁
>
> 此段落由脚本自动生成：`python scripts/iteration/render_min_gate_block.py 13 --profile sql-only`

### 命令表格

| 序号 | 检查项 | 命令 | 通过标准 |
|------|--------|------|----------|
| 1 | SQL 迁移文件检查 | `make check-migration-sanity` | 退出码 0 |
| 2 | 数据库权限验证 | `make verify-permissions` | 退出码 0 |

### 一键执行

```bash
# 一键运行所有门禁（需全部通过）
make check-migration-sanity && \\
  make verify-permissions
```

### 通过标准

**通过标准**：每个命令需满足对应的通过条件。

- `make check-migration-sanity` → 退出码 0
- `make verify-permissions` → 退出码 0
"""

    # gateway-only profile 快照
    GATEWAY_ONLY_SNAPSHOT = """## 最小门禁命令块

<!-- AUTO-GENERATED BY render_min_gate_block.py -->

> **Iteration 13** - Gateway 代理最小门禁
>
> 此段落由脚本自动生成：`python scripts/iteration/render_min_gate_block.py 13 --profile gateway-only`

### 命令表格

| 序号 | 检查项 | 命令 | 通过标准 |
|------|--------|------|----------|
| 1 | 代码风格检查 | `make lint` | 退出码 0 |
| 2 | Gateway DI 边界检查 | `make check-gateway-di-boundaries` | 退出码 0 |
| 3 | Gateway Public API 导入表面检查 | `make check-gateway-public-api-surface` | 退出码 0 |
| 4 | Gateway Public API 文档同步检查 | `make check-gateway-public-api-docs-sync` | 退出码 0 |
| 5 | Gateway 懒加载策略检查 | `make check-gateway-import-surface` | 退出码 0 |
| 6 | Gateway correlation_id 单一来源检查 | `make check-gateway-correlation-id-single-source` | 退出码 0 |
| 7 | MCP 错误码合约检查 | `make check-mcp-error-contract` | 退出码 0 |
| 8 | MCP 错误码文档同步检查 | `make check-mcp-error-docs-sync` | 退出码 0 |

### 一键执行

```bash
# 一键运行所有门禁（需全部通过）
make lint && \\
  make check-gateway-di-boundaries && \\
  make check-gateway-public-api-surface && \\
  make check-gateway-public-api-docs-sync && \\
  make check-gateway-import-surface && \\
  make check-gateway-correlation-id-single-source && \\
  make check-mcp-error-contract && \\
  make check-mcp-error-docs-sync
```

### 通过标准

**通过标准**：每个命令需满足对应的通过条件。

- `make lint` → 退出码 0
- `make check-gateway-di-boundaries` → 退出码 0
- `make check-gateway-public-api-surface` → 退出码 0
- `make check-gateway-public-api-docs-sync` → 退出码 0
- `make check-gateway-import-surface` → 退出码 0
- `make check-gateway-correlation-id-single-source` → 退出码 0
- `make check-mcp-error-contract` → 退出码 0
- `make check-mcp-error-docs-sync` → 退出码 0
"""

    def test_full_snapshot(self):
        """测试 full profile 输出稳定性"""
        result = render_min_gate_block(13, "full")
        assert result == self.FULL_SNAPSHOT, (
            f"full profile 输出与快照不一致\n实际输出:\n{result}\n期望输出:\n{self.FULL_SNAPSHOT}"
        )

    def test_regression_snapshot(self):
        """测试 regression profile 输出稳定性"""
        result = render_min_gate_block(13, "regression")
        assert result == self.REGRESSION_SNAPSHOT, (
            f"regression profile 输出与快照不一致\n"
            f"实际输出:\n{result}\n"
            f"期望输出:\n{self.REGRESSION_SNAPSHOT}"
        )

    def test_docs_only_snapshot(self):
        """测试 docs-only profile 输出稳定性"""
        result = render_min_gate_block(13, "docs-only")
        assert result == self.DOCS_ONLY_SNAPSHOT, (
            f"docs-only profile 输出与快照不一致\n"
            f"实际输出:\n{result}\n"
            f"期望输出:\n{self.DOCS_ONLY_SNAPSHOT}"
        )

    def test_ci_only_snapshot(self):
        """测试 ci-only profile 输出稳定性"""
        result = render_min_gate_block(13, "ci-only")
        assert result == self.CI_ONLY_SNAPSHOT, (
            f"ci-only profile 输出与快照不一致\n"
            f"实际输出:\n{result}\n"
            f"期望输出:\n{self.CI_ONLY_SNAPSHOT}"
        )

    def test_sql_only_snapshot(self):
        """测试 sql-only profile 输出稳定性"""
        result = render_min_gate_block(13, "sql-only")
        assert result == self.SQL_ONLY_SNAPSHOT, (
            f"sql-only profile 输出与快照不一致\n"
            f"实际输出:\n{result}\n"
            f"期望输出:\n{self.SQL_ONLY_SNAPSHOT}"
        )

    def test_gateway_only_snapshot(self):
        """测试 gateway-only profile 输出稳定性"""
        result = render_min_gate_block(13, "gateway-only")
        assert result == self.GATEWAY_ONLY_SNAPSHOT, (
            f"gateway-only profile 输出与快照不一致\n"
            f"实际输出:\n{result}\n"
            f"期望输出:\n{self.GATEWAY_ONLY_SNAPSHOT}"
        )

    def test_iteration_number_changes_output(self):
        """测试迭代编号变化会改变输出"""
        result_13 = render_min_gate_block(13, "docs-only")
        result_14 = render_min_gate_block(14, "docs-only")

        assert "Iteration 13" in result_13
        assert "Iteration 14" in result_14
        assert result_13 != result_14

    def test_profile_changes_output(self):
        """测试 profile 变化会改变输出"""
        result_docs = render_min_gate_block(13, "docs-only")
        result_ci = render_min_gate_block(13, "ci-only")

        assert "文档代理" in result_docs
        assert "CI 代理" in result_ci
        assert result_docs != result_ci


# ============================================================================
# 命令内容验证测试
# ============================================================================


class TestCommandContent:
    """命令内容验证测试"""

    def test_docs_only_commands_match_agents_md(self):
        """测试 docs-only 命令与 AGENTS.md 一致"""
        # AGENTS.md 中文档代理的最小门禁：check-env-consistency, check-iteration-docs
        command_names = [cmd.command for cmd in DOCS_GATE_COMMANDS]
        assert "make check-env-consistency" in command_names
        assert "make check-iteration-docs" in command_names

    def test_ci_only_commands_match_agents_md(self):
        """测试 ci-only 命令与 AGENTS.md 一致"""
        # AGENTS.md 中 CI 代理的最小门禁
        command_names = [cmd.command for cmd in CI_GATE_COMMANDS]
        assert "make typecheck" in command_names
        assert "make validate-workflows-strict" in command_names
        assert "make check-workflow-contract-docs-sync" in command_names
        assert "make check-workflow-contract-version-policy" in command_names
        assert "make check-workflow-contract-doc-anchors" in command_names

    def test_gateway_only_commands_match_agents_md(self):
        """测试 gateway-only 命令与 AGENTS.md 一致"""
        # AGENTS.md 中 Gateway 代理的最小门禁
        command_names = [cmd.command for cmd in GATEWAY_GATE_COMMANDS]
        assert "make lint" in command_names
        assert "make check-gateway-di-boundaries" in command_names
        assert "make check-gateway-public-api-surface" in command_names
        assert "make check-gateway-public-api-docs-sync" in command_names
        assert "make check-gateway-import-surface" in command_names
        assert "make check-gateway-correlation-id-single-source" in command_names
        assert "make check-mcp-error-contract" in command_names
        assert "make check-mcp-error-docs-sync" in command_names

    def test_sql_only_commands_match_agents_md(self):
        """测试 sql-only 命令与 AGENTS.md 一致"""
        # AGENTS.md 中 SQL 代理的最小门禁
        command_names = [cmd.command for cmd in SQL_GATE_COMMANDS]
        assert "make check-migration-sanity" in command_names
        assert "make verify-permissions" in command_names

    def test_full_profile_includes_make_ci_commands(self):
        """测试 full profile 包含 make ci 的主要命令"""
        command_names = [cmd.command for cmd in FULL_GATE_COMMANDS]
        # make ci 的核心命令
        assert "make lint" in command_names
        assert "make format-check" in command_names
        assert "make typecheck" in command_names
        assert "make check-schemas" in command_names

    def test_regression_profile_matches_iteration_regression(self):
        """测试 regression profile 与 iteration_N_regression.md 对齐"""
        # regression profile 应包含迭代回归验证的核心检查项
        command_names = [cmd.command for cmd in REGRESSION_GATE_COMMANDS]
        assert "make validate-workflows-strict" in command_names
        assert "make check-workflow-contract-docs-sync" in command_names
        assert "make check-gateway-public-api-surface" in command_names
        assert "make check-gateway-public-api-docs-sync" in command_names
        assert "make check-iteration-docs" in command_names
        assert "pytest tests/ci/ -q" in command_names


# ============================================================================
# 边界情况测试
# ============================================================================


class TestEdgeCases:
    """边界情况测试"""

    def test_iteration_number_zero(self):
        """测试迭代编号为 0"""
        result = render_min_gate_block(0, "docs-only")
        assert "Iteration 0" in result

    def test_large_iteration_number(self):
        """测试大迭代编号"""
        result = render_min_gate_block(999, "docs-only")
        assert "Iteration 999" in result

    def test_all_profiles_render_successfully(self):
        """测试所有 profile 都能成功渲染"""
        for profile in SUPPORTED_PROFILES:
            result = render_min_gate_block(1, profile)
            assert "## 最小门禁命令块" in result
            assert "### 命令表格" in result
            assert "### 一键执行" in result
            assert "### 通过标准" in result


# ============================================================================
# 格式验证测试
# ============================================================================


class TestOutputFormat:
    """输出格式验证测试"""

    def test_markdown_table_format(self):
        """测试 Markdown 表格格式正确"""
        result = render_min_gate_block(13, "docs-only")

        # 表格行应该以 | 开头和结尾
        lines = result.split("\n")
        table_lines = [line for line in lines if line.startswith("|")]

        for line in table_lines:
            assert line.endswith("|"), f"表格行应以 | 结尾: {line}"

    def test_bash_block_format(self):
        """测试 bash 代码块格式正确"""
        result = render_min_gate_block(13, "docs-only")

        assert "```bash" in result
        # 确保有配对的结束标记
        assert result.count("```") >= 2

    def test_no_trailing_whitespace_in_table(self):
        """测试表格无尾随空格"""
        commands = [GateCommand("make lint", "代码风格检查", "退出码 0")]
        result = render_command_table(commands)

        for line in result.split("\n"):
            # 行末不应有额外空格（除了表格分隔符）
            if not line.startswith("|---"):
                assert line == line.rstrip() or line.endswith("|"), f"发现尾随空格: '{line}'"

    def test_script_generation_notice_format(self):
        """测试脚本生成提示格式正确"""
        result = render_min_gate_block(13, "docs-only")

        # 应该包含完整的命令示例
        assert "python scripts/iteration/render_min_gate_block.py 13 --profile docs-only" in result

    def test_auto_generated_marker_is_machine_readable(self):
        """测试自动生成标识行是机器可识别的"""
        result = render_min_gate_block(13, "full")

        # 标识行应该是 HTML 注释格式，便于机器解析
        assert "<!-- AUTO-GENERATED BY render_min_gate_block.py -->" in result
