# AI Agent 协作指南

> **适用人群**：使用 AI Agent（如 Cursor、Claude）参与本项目开发的用户
>
> **快速入门**：查看根目录的 **[AGENTS.md](../../AGENTS.md)** 获取摘要版本

---

## 快速开始

### 标准执行顺序

> **重要**：任何修复工作都应遵循 **先复现 → 再修复 → 再验证** 的顺序。

```bash
# ========== 1. 先复现问题 ==========
# 理解当前状态，不要假设问题原因
make ci                 # 运行完整 CI 检查
git status              # 查看当前变更
git diff               # 查看具体修改内容

# ========== 2. 定位并修复问题 ==========
# 根据 CI 输出的错误信息进行精准修改
# 避免过度修复或引入新问题

# ========== 3. 再次验证 ==========
# 修复后必须重新运行门禁确保完整性
make ci                 # 完整 CI 检查
make test              # 可选：运行测试（需数据库）
```

**核心原则**：

| 阶段 | 目的 | 常见错误 |
|------|------|----------|
| **先复现** | 理解实际错误，避免盲目修改 | 假设问题原因直接修改 |
| **再修复** | 精准定位，最小化变更 | 过度修复，改动不相关文件 |
| **再验证** | 确保修复完整，无新增问题 | 跳过验证直接提交 |

### 最小验证工作流

在提交代码前，运行以下命令确保通过 CI 门禁：

```bash
# 一键运行所有 CI 检查（推荐）
make ci

# 或分步执行（按 make ci 依赖顺序）
make lint                               # 代码风格检查（ruff check）
make format-check                       # 格式检查（ruff format --check）
make typecheck                          # mypy 类型检查
make check-schemas                      # JSON Schema 校验
make check-env-consistency              # 环境变量一致性检查
make check-logbook-consistency          # Logbook 配置一致性检查
make check-migration-sanity             # SQL 迁移文件检查
make check-scm-sync-consistency         # SCM Sync 一致性检查
make check-gateway-error-reason-usage   # Gateway ErrorReason 使用规范检查
make check-gateway-public-api-surface   # Gateway Public API 导入表面检查
make check-gateway-public-api-docs-sync # Gateway Public API 文档同步检查
make check-gateway-di-boundaries        # Gateway DI 边界检查
make check-gateway-import-surface       # Gateway __init__.py 懒加载策略检查
make check-gateway-correlation-id-single-source  # Gateway correlation_id 单一来源检查
make check-iteration-docs               # 迭代文档规范检查
make validate-workflows-strict          # Workflow 合约校验（严格模式）
make check-workflow-contract-docs-sync  # Workflow 合约与文档同步检查
make check-workflow-contract-version-policy  # Workflow 合约版本策略检查
make check-mcp-error-contract           # MCP JSON-RPC 错误码合约检查
make check-mcp-error-docs-sync          # MCP JSON-RPC 错误码文档与 Schema 同步检查

# 可选的独立检查（未包含在 make ci 中）
make typecheck-gate                     # mypy baseline 模式检查（用于增量修复）
make check-cli-entrypoints              # CLI 入口点一致性检查
make check-noqa-policy                  # noqa 注释策略检查
make check-no-root-wrappers             # 根目录 wrapper 禁止导入检查
make check-workflow-contract-doc-anchors  # Workflow 合约文档锚点检查
pytest tests/ci/ -q                     # CI 脚本测试
make test-iteration-tools               # 迭代工具脚本测试（无需数据库）

# 建议工具（辅助开发，不阻断 CI）
python scripts/ci/suggest_workflow_contract_updates.py --json  # 生成合约更新建议（JSON）
python scripts/ci/suggest_workflow_contract_updates.py --markdown  # 生成合约更新建议（Markdown）
```

> **迭代回归 Runbook**：详细的最小门禁命令块（含预期输出关键字和通过标准）请参阅当前活跃迭代的回归记录：
> - [Iteration 13 Regression Runbook](../acceptance/iteration_13_regression.md#最小门禁命令块)

### 核心门禁脚本

| 脚本 | 用途 | Makefile 目标 |
|------|------|---------------|
| `scripts/ci/check_mypy_gate.py` | mypy 类型检查门禁 | `make typecheck-gate` |
| `scripts/verify_cli_entrypoints_consistency.py` | CLI 入口点一致性 | `make check-cli-entrypoints` |
| `scripts/ci/check_env_var_consistency.py` | 环境变量一致性 | `make check-env-consistency` |
| `scripts/ci/check_sql_migration_plan_sanity.py` | SQL 迁移计划检查 | `make check-migration-sanity` |
| `scripts/verify_scm_sync_consistency.py` | SCM Sync 一致性 | `make check-scm-sync-consistency` |
| `scripts/ci/check_noqa_policy.py` | noqa 注释策略检查 | `make check-noqa-policy` |
| `scripts/ci/check_no_root_wrappers_usage.py` | 根目录 wrapper 禁止导入 | `make check-no-root-wrappers` |
| `scripts/ci/check_gateway_error_reason_usage.py` | Gateway ErrorReason 使用规范 | `make check-gateway-error-reason-usage` |
| `scripts/ci/check_gateway_public_api_import_surface.py` | Gateway Public API 导入表面 | `make check-gateway-public-api-surface` |
| `scripts/ci/check_gateway_di_boundaries.py` | Gateway DI 边界 | `make check-gateway-di-boundaries` |
| `scripts/ci/check_gateway_correlation_id_single_source.py` | Gateway correlation_id 单一来源 | `make check-gateway-correlation-id-single-source` |
| `scripts/ci/check_mcp_jsonrpc_error_contract.py` | MCP JSON-RPC 错误码合约 | `make check-mcp-error-contract` |
| `scripts/ci/check_mcp_jsonrpc_error_docs_sync.py` | MCP JSON-RPC 错误码文档同步 | `make check-mcp-error-docs-sync` |
| `scripts/ci/validate_workflows.py` | Workflow 合约校验 | `make validate-workflows-strict` |
| `scripts/ci/check_workflow_contract_docs_sync.py` | Workflow 合约与文档同步 | `make check-workflow-contract-docs-sync` |
| `scripts/ci/check_workflow_contract_version_policy.py` | Workflow 合约版本策略 | `make check-workflow-contract-version-policy` |
| `scripts/ci/check_workflow_contract_doc_anchors.py` | Workflow 合约文档锚点 | `make check-workflow-contract-doc-anchors` |
| `scripts/ci/suggest_workflow_contract_updates.py` | 合约更新建议（辅助工具） | 手动执行 |

---

## 推荐的 Agent CLI 用法

### mypy 类型检查

```bash
# 默认 baseline 模式（对比基线，仅新增错误阻断）
python scripts/ci/check_mypy_gate.py

# 或使用 Makefile
make typecheck-gate

# 严格模式（任何错误阻断）
python scripts/ci/check_mypy_gate.py --gate strict

# Strict Island 模式（仅检查核心模块）
python scripts/ci/check_mypy_gate.py --gate strict-island

# 更新基线文件（修复错误后执行）
python scripts/ci/check_mypy_gate.py --write-baseline
```

**门禁模式说明**：

| 模式 | 说明 | 退出码 |
|------|------|--------|
| `baseline` | 对比基线，仅新增错误失败（默认） | 新增错误=1，否则=0 |
| `strict` | 任何 mypy 错误都失败 | 有错误=1，否则=0 |
| `strict-island` | 仅检查核心模块 | 核心模块有错误=1 |
| `warn` | 仅警告，不阻断 | 永远=0 |
| `off` | 跳过检查 | 永远=0 |

### CLI 入口点一致性检查

```bash
# 检查入口点一致性
python scripts/verify_cli_entrypoints_consistency.py --verbose

# JSON 格式输出
python scripts/verify_cli_entrypoints_consistency.py --json

# 或使用 Makefile
make check-cli-entrypoints
```

**检查项**：
- A) `pyproject.toml` 入口点模块可导入
- B) `docs/architecture/cli_entrypoints.md` 与 `pyproject.toml` 一致
- C) 文档中引用的 `engram-*` 命令存在
- D) 无根目录 wrapper 导入
- E) subprocess 调用使用官方 CLI
- F) `import_migration_map.json` 中的 `cli_target` 有效

---

## 子代理分工建议

> **外部参考**：
> - [Cursor Agent 模式文档](https://docs.cursor.com/chat/agent) - Agent 基础用法
> - [Model Context Protocol (MCP)](https://docs.cursor.com/context/model-context-protocol) - 上下文管理
> - [MCP Server 安装指南](https://docs.cursor.com/context/model-context-protocol#adding-mcp-servers) - MCP Server 添加方法
> - [MCP Server 目录](https://cursor.directory/) - 社区 MCP Server 列表
> - [Cursor Rules 配置](https://docs.cursor.com/context/rules-for-ai) - 自定义 Agent 行为
> - [Project Rules](https://docs.cursor.com/context/rules-for-ai#project-rules) - 项目级 Rules 配置
>
> **本仓库映射**：
> - `AGENTS.md`（根目录）→ Cursor Workspace Rules（自动加载）
> - `configs/mcp/.mcp.json.example` → MCP 配置 SSOT
> - `docs/gateway/02_mcp_integration_cursor.md` → Gateway MCP 集成指南

对于复杂任务，建议按职责划分子代理：

### SQL / 数据库代理

**职责**：
- 编写和审查 SQL 迁移脚本（`sql/*.sql`）
- 维护权限配置（`04_roles_and_grants.sql`, `05_openmemory_roles_and_grants.sql`）
- 更新权限验证脚本（`sql/verify/99_verify_permissions.sql`）

**相关命令**：
```bash
# 查看迁移计划
make migrate-plan

# 执行 DDL 迁移
make migrate-ddl

# 验证权限配置
make verify-permissions

# 检查 SQL 迁移计划 Sanity
make check-migration-sanity
```

**相关文档**：
- `docs/logbook/sql_file_inventory.md` - SQL 文件清单
- `docs/logbook/sql_renumbering_map.md` - SQL 文件编号映射

### CI / 工程质量代理

**职责**：
- 维护 CI 脚本（`scripts/ci/*.py`）
- 更新 mypy baseline（`scripts/ci/mypy_baseline.txt`）
- 管理 allowlist（`scripts/ci/no_root_wrappers_allowlist.json`）
- 维护 GitHub Actions（`.github/workflows/ci.yml`）

**相关命令**：
```bash
# 运行完整 CI 检查
make ci

# 更新 mypy baseline
make mypy-baseline-update

# 检查阈值状态
python scripts/ci/check_mypy_gate.py --check-threshold
```

**相关文档**：
- `docs/dev/mypy_baseline.md` - mypy baseline 机制说明
- `docs/dev/mypy_error_playbook.md` - mypy 错误修复手册
- `docs/architecture/adr_mypy_baseline_and_gating.md` - mypy 门禁 ADR

### 文档代理

**职责**：
- 维护架构文档（`docs/architecture/*.md`）
- 更新 API 契约（`docs/contracts/*.md`）
- 同步 CLI 入口文档（`docs/architecture/cli_entrypoints.md`）
- 维护环境变量文档（`docs/reference/environment_variables.md`）

**相关命令**：
```bash
# 检查文档中的命令引用
make check-cli-entrypoints

# 检查环境变量一致性
make check-env-consistency
```

**相关文档**：
- `docs/architecture/README.md` - 架构文档索引
- `docs/gateway/README.md` - Gateway 文档索引

### Gateway / 业务逻辑代理

**职责**：
- 开发 Gateway 功能（`src/engram/gateway/`）
- 编写 Handler 和 Service（`src/engram/gateway/handlers/`, `src/engram/gateway/services/`）
- 维护 Logbook 核心（`src/engram/logbook/`）

**相关命令**：
```bash
# 运行 Gateway 测试
make test-gateway

# 运行 Logbook 测试
make test-logbook

# 启动开发服务器
make gateway
```

---

## Cursor 2.4+ Subagents 能力与本仓库实践

> **参考来源**：[Cursor Changelog](https://cursor.com/cn/changelog) - 2.4 版本（2026-01-22）

### Cursor 官方 Subagents 要点

根据 [Cursor 2.4 更新日志](https://cursor.com/changelog/2-4)，Subagents 具有以下核心能力：

| 特性 | 说明 |
|------|------|
| **独立运行** | 子代理并行运行，使用各自独立的上下文 |
| **可配置** | 支持自定义提示词、工具访问权限和模型 |
| **上下文隔离** | 主对话保持聚焦，子任务独立处理 |
| **执行效率** | 并行处理独立任务，整体执行更快 |

Cursor 默认提供用于分析代码库、运行终端命令和执行并行工作流的内置子代理。

### 本仓库落地实践

基于 Cursor Subagents 能力，本仓库制定以下协调规则：

| Cursor 能力 | 本仓库实践 | 相关文档 |
|-------------|-----------|----------|
| **上下文隔离** | 按目录拆分代理职责（SQL/CI/文档/Gateway） | [Subagent 拆分模板](#subagent-拆分模板) |
| **并行运行** | 不同代理处理不同目录，避免文件冲突 | [协调规则汇总](#协调规则汇总) |
| **可配置工具** | 每类代理有专属的最小门禁命令集 | [门禁命令速查表](#门禁命令速查表) |

**关键约束**（针对 Subagents 并行场景）：

1. **共享文件单点负责**：`pyproject.toml`、`Makefile`、`mypy_baseline.txt` 等共享文件由单一代理负责，禁止多代理并行编辑（即使 Subagents 支持并行，共享文件仍需串行）
2. **baseline 串行更新**：`scripts/ci/mypy_baseline.txt` 更新必须串行执行，避免合并冲突
3. **按目录拆分**：SQL 代理 → `sql/`、CI 代理 → `scripts/ci/`、文档代理 → `docs/`、Gateway 代理 → `src/engram/`
4. **合并前统一验证**：所有子代理完成后，运行 `make ci` 验证集成

### 与 Cursor Skills 的关系

Cursor 2.4 引入的 [Skills](https://cursor.com/docs/context/skills) 与声明式 Rules 不同，更适合动态上下文发现和过程式"操作指南"。本仓库的 `AGENTS.md` 作为 workspace rule 提供声明式规则，而具体任务的 Subagent 拆分则参考本章节的实践指导。

---

## Subagent 拆分模板

> 本节定义基于目录结构的子代理职责划分，及每类代理的最小门禁命令集。

### 拆分原则

1. **任务隔离**：不同代理处理不同目录/模块，避免文件冲突
2. **串行更新 baseline**：`mypy_baseline.txt` 等共享文件更新需串行执行
3. **共享文件单代理负责**：`pyproject.toml`、`Makefile` 等配置文件由单一代理负责
4. **完成后统一验证**：代理完成后运行 `make ci` 验证集成
5. **同一文件禁止并行编辑**：避免合并冲突
6. **先复现再修复**：任何修复工作都应先运行门禁复现问题，再进行修改

### 共享文件单点负责规则

以下文件必须由**指定代理**负责修改，其他代理禁止直接编辑：

| 文件 | 负责代理 | 说明 |
|------|----------|------|
| `pyproject.toml` | CI 代理 | 依赖版本、入口点、工具配置（mypy/ruff/pytest） |
| `Makefile` | CI 代理 | 构建目标、门禁命令、变量定义 |
| `scripts/ci/mypy_baseline.txt` | CI 代理 | mypy 错误基线（**必须串行更新**） |
| `scripts/ci/no_root_wrappers_allowlist.json` | CI 代理 | 根目录 wrapper 迁移 allowlist |
| `scripts/ci/gateway_deps_db_allowlist.json` | CI 代理 | Gateway deps.db 迁移 allowlist |
| `.github/workflows/ci.yml` | CI 代理 | CI 流水线定义 |
| `.github/workflows/nightly.yml` | CI 代理 | Nightly 流水线定义 |
| `scripts/ci/workflow_contract.v1.json` | CI 代理 | Workflow 合约定义（需与 ci.yml 同步） |
| `docs/reference/environment_variables.md` | 文档代理 | 环境变量参考（SSOT） |
| `docs/architecture/cli_entrypoints.md` | 文档代理 | CLI 入口点文档 |
| `configs/import_migration_map.json` | CI 代理 | 导入迁移映射 |

**违反后果**：
- 合并冲突：多代理同时编辑导致 Git 冲突
- 数据丢失：后提交覆盖先提交的修改
- CI 失败：不一致的配置导致门禁检查失败

### SQL / 数据库代理

| 属性 | 值 |
|------|-----|
| **职责范围** | SQL 迁移脚本、权限配置、DDL 变更 |
| **主要路径** | `sql/*.sql`, `sql/verify/`, `docs/logbook/` |
| **禁止触碰** | `src/engram/gateway/`, `scripts/ci/` |

**最小门禁命令**：

```bash
# 必须运行
make check-migration-sanity      # SQL 迁移计划 Sanity 检查
make verify-permissions          # 权限验证
make check-sql-inventory-consistency  # SQL 清单文档一致性

# 推荐运行
make migrate-plan                # 查看迁移计划
make ci                          # 完整 CI 检查（合并前）
```

### CI / 工程质量代理

| 属性 | 值 |
|------|-----|
| **职责范围** | CI 脚本、mypy baseline、allowlist、GitHub Actions |
| **主要路径** | `scripts/ci/*.py`, `.github/workflows/`, `scripts/ci/mypy_baseline.txt` |
| **禁止触碰** | `src/engram/gateway/handlers/`, `sql/*.sql` |

**最小门禁命令**：

```bash
# 必须运行
make typecheck                   # mypy 类型检查
make validate-workflows-strict   # Workflow 合约校验（严格模式）
make check-workflow-contract-docs-sync   # Workflow 合约与文档同步检查
make check-workflow-contract-version-policy  # Workflow 合约版本策略检查

# 可选独立检查
make typecheck-gate              # mypy baseline 模式检查（用于增量修复）
make check-noqa-policy           # noqa 注释策略检查
make check-no-root-wrappers      # 根目录 wrapper 禁止导入检查
make check-workflow-contract-doc-anchors  # Workflow 合约文档锚点检查

# 建议工具（辅助更新合约）
python scripts/ci/suggest_workflow_contract_updates.py --json  # JSON 格式
python scripts/ci/suggest_workflow_contract_updates.py --markdown  # Markdown 格式

# baseline 更新时
make mypy-baseline-update        # 更新 mypy 基线（需串行）

# 推荐运行
make ci                          # 完整 CI 检查（合并前）
```

### 文档代理

| 属性 | 值 |
|------|-----|
| **职责范围** | 架构文档、API 契约、CLI 入口文档、环境变量文档 |
| **主要路径** | `docs/architecture/`, `docs/contracts/`, `docs/gateway/`, `docs/reference/` |
| **禁止触碰** | `src/`, `scripts/ci/*.py`, `sql/*.sql` |

**最小门禁命令**：

```bash
# 必须运行
make check-env-consistency       # 环境变量一致性检查
make check-iteration-docs        # 迭代文档规范检查

# 可选独立检查
make check-cli-entrypoints       # CLI 入口点一致性检查

# 推荐运行
make check-schemas               # JSON Schema 校验（若修改 schemas/）
make ci                          # 完整 CI 检查（合并前）
```

### Gateway / 业务逻辑代理

| 属性 | 值 |
|------|-----|
| **职责范围** | Gateway 功能开发、Handler/Service 实现、Logbook 核心 |
| **主要路径** | `src/engram/gateway/`, `src/engram/logbook/`, `tests/gateway/`, `tests/logbook/` |
| **禁止触碰** | `sql/*.sql`（除非协调）, `.github/workflows/ci.yml` |

**最小门禁命令**：

```bash
# 必须运行
make lint                        # 代码风格检查
make format-check                # 格式检查
make typecheck                   # mypy 类型检查
make check-gateway-di-boundaries # Gateway DI 边界检查
make check-gateway-public-api-surface  # Gateway Public API 导入表面检查
make check-gateway-public-api-docs-sync  # Gateway Public API 文档同步检查
make check-gateway-import-surface  # Gateway __init__.py 懒加载策略检查
make check-gateway-correlation-id-single-source  # Gateway correlation_id 单一来源检查
make check-mcp-error-contract    # MCP JSON-RPC 错误码合约检查
make check-mcp-error-docs-sync   # MCP JSON-RPC 错误码文档同步检查
make test-gateway                # Gateway 测试

# 推荐运行
make test-logbook                # Logbook 测试
make ci                          # 完整 CI 检查（合并前）
```

### 协调规则汇总

| 规则 | 说明 | 违反后果 |
|------|------|----------|
| **baseline 串行更新** | `mypy_baseline.txt` 更新需串行执行 | 合并冲突、错误覆盖 |
| **共享文件单代理** | `pyproject.toml`、`Makefile` 由单一代理负责 | 合并冲突 |
| **同文件禁止并行** | 同一文件不要由多个代理同时编辑 | 合并冲突、数据丢失 |
| **合并前统一验证** | 完成后运行 `make ci` | CI 失败 |
| **跨代理任务标注** | 使用 `# TODO: @<agent-role>` 标注 | 任务遗漏 |

### 门禁命令速查表

> 与 [CI 门禁 Runbook](./ci_gate_runbook.md) 保持同步

| 代理类型 | 必须运行 | 推荐运行 |
|----------|----------|----------|
| **SQL** | `check-migration-sanity`, `verify-permissions` | `migrate-plan`, `ci` |
| **CI** | `typecheck`, `validate-workflows-strict`, `check-workflow-contract-docs-sync`, `check-workflow-contract-version-policy` | `typecheck-gate`, `check-noqa-policy`, `check-workflow-contract-doc-anchors`, `mypy-baseline-update`, `ci` |
| **文档** | `check-env-consistency`, `check-iteration-docs` | `check-cli-entrypoints`, `check-schemas`, `ci` |
| **Gateway** | `lint`, `format-check`, `typecheck`, `check-gateway-di-boundaries`, `check-gateway-public-api-surface`, `check-gateway-public-api-docs-sync`, `check-gateway-import-surface`, `check-gateway-correlation-id-single-source`, `check-mcp-error-contract`, `check-mcp-error-docs-sync`, `test-gateway` | `test-logbook`, `ci` |

---

## Gateway 测试编写规范

### GatewayDeps.for_testing() 严格模式

`GatewayDeps.for_testing()` 默认启用**严格模式**（`_testing_strict=True`）：

- 访问未显式注入的依赖时，会抛出 `RuntimeError` 而非延迟初始化
- 这确保测试不会意外连接真实数据库或外部服务
- 测试必须显式注入所有被测代码路径实际使用的依赖

**正确用法**（显式注入所有依赖）：

```python
from engram.gateway.di import GatewayDeps
from tests.gateway.fakes import (
    FakeGatewayConfig,
    FakeLogbookAdapter,
    FakeOpenMemoryClient,
)

# 创建 fake 依赖
fake_config = FakeGatewayConfig()
fake_adapter = FakeLogbookAdapter()
fake_client = FakeOpenMemoryClient()

# 配置 fake 行为
fake_adapter.configure_dedup_miss()
fake_client.configure_store_success(memory_id="mem_123")

# 显式注入所有依赖（严格模式）
deps = GatewayDeps.for_testing(
    config=fake_config,
    logbook_adapter=fake_adapter,
    openmemory_client=fake_client,
)

# 调用被测函数
result = await memory_store_impl(
    payload_md="test",
    correlation_id="corr-0000000000000001",
    deps=deps,
)
```

**错误用法**（缺少必要依赖）：

```python
# 如果 handler 使用 deps.openmemory_client，但未注入：
deps = GatewayDeps.for_testing(config=fake_config)  # 缺少 openmemory_client
await memory_store_impl(deps=deps, ...)
# RuntimeError: 测试严格模式下 openmemory_client 未注入
```

### 推荐使用 conftest.py 的 fixtures

对于大多数测试，推荐使用 `tests/gateway/conftest.py` 中预定义的 fixtures：

```python
@pytest.mark.asyncio
async def test_memory_store(gateway_deps, test_correlation_id):
    """gateway_deps fixture 已注入所有依赖"""
    result = await memory_store_impl(
        payload_md="test",
        correlation_id=test_correlation_id,
        deps=gateway_deps,
    )
    assert result.ok is True
```

可用的 fixtures：
- `gateway_deps`: 完整的依赖容器（config + logbook_adapter + openmemory_client）
- `fake_gateway_config`: FakeGatewayConfig 实例
- `fake_logbook_adapter`: FakeLogbookAdapter 实例
- `fake_openmemory_client`: FakeOpenMemoryClient 实例
- `test_correlation_id`: 格式正确的测试用 correlation_id

---

## 常见工作流

### 1. 添加新功能

```bash
# 1. 编写代码
# 2. 运行测试
make test

# 3. 检查代码质量
make ci

# 4. 如有类型错误，修复后更新 baseline（如需要）
make mypy-baseline-update
```

### 2. 修改 SQL 迁移

```bash
# 1. 编辑 sql/*.sql 文件
# 2. 检查迁移计划
make migrate-plan

# 3. 验证 Sanity
make check-migration-sanity

# 4. 执行迁移（需要数据库）
make migrate-ddl

# 5. 验证权限
make verify-permissions
```

### 3. 更新文档

```bash
# 1. 编辑文档
# 2. 检查命令引用
make check-cli-entrypoints

# 3. 检查环境变量
make check-env-consistency
```

### 4. 修复 CI 失败

```bash
# 查看失败原因
make ci

# 常见修复
make format          # 修复格式问题
ruff check --fix .   # 自动修复 lint 问题

# 类型错误
python scripts/ci/check_mypy_gate.py --verbose  # 查看详细错误
```

### 5. 草稿→SSOT 晋升流程

> 本节描述如何将 `.iteration/` 或 `.artifacts/` 中的草稿文档晋升为 SSOT（Single Source of Truth）正式文档。

**推荐流程**：

1. **草稿阶段**：在 `.iteration/` 或 `.artifacts/` 目录中编写草稿文档
   - 这些目录不纳入 Git 版本控制
   - 适合快速迭代、尚未定稿的内容

2. **审查与定稿**：草稿内容稳定后，准备晋升
   - 确认内容完整、准确
   - 移除草稿标记（如有）

3. **晋升操作**：将文档移动到正式目录
   ```bash
   # 示例：将草稿迁移到正式文档目录
   mv .iteration/my_design_doc.md docs/architecture/my_design_doc.md
   
   # 或使用 cp 后删除草稿（保留备份）
   cp .iteration/my_design_doc.md docs/architecture/my_design_doc.md
   rm .iteration/my_design_doc.md
   ```

4. **清理引用**：确保晋升后的文档不包含指向草稿目录的链接
   ```bash
   # 检查是否有草稿目录链接残留
   grep -r "\.iteration/" docs/
   grep -r "\.artifacts/" docs/
   ```

5. **更新验收矩阵**（如涉及迭代文档）
   - 同步更新 `docs/acceptance/00_acceptance_matrix.md`

**注意事项**：

- **禁止在 SSOT 文档中保留草稿目录链接**：`.iteration/` 和 `.artifacts/` 是临时草稿目录，不应在正式文档中被引用
- **PR 模板检查项**：提交 PR 时需确认无草稿目录可点击链接
- **CI 检查**（如启用）：`make check-no-iteration-links` 会检测草稿目录链接
- **草稿分享推荐方式**：若需与团队共享本地草稿，推荐使用 `make iteration-export N=<编号>` 导出分享包，或直接晋升为 SSOT（`--status PLANNING`），**禁止在文档中链接 `.iteration/` 路径**。详见 [迭代文档本地草稿工作流](iteration_local_drafts.md#草稿分享与协作)

---

## 配置与环境变量

### CI 相关环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `ENGRAM_MYPY_GATE` | mypy 门禁级别 | `baseline` |
| `ENGRAM_MYPY_BASELINE_FILE` | baseline 文件路径 | `scripts/ci/mypy_baseline.txt` |
| `ENGRAM_MYPY_PATH` | mypy 扫描路径 | `src/engram/` |
| `ENGRAM_MYPY_MIGRATION_PHASE` | 迁移阶段 (0/1/2/3) | `0` |

### 开发环境变量

| 变量 | 说明 |
|------|------|
| `POSTGRES_DSN` | PostgreSQL 连接字符串 |
| `PROJECT_KEY` | 项目标识 |
| `OPENMEMORY_BASE_URL` | OpenMemory 服务地址 |

> 完整列表见 [环境变量参考](../reference/environment_variables.md)

---

## 相关资源

### 仓库内文档

| 资源 | 路径 |
|------|------|
| CI 配置 | `.github/workflows/ci.yml` |
| Makefile | `Makefile` |
| pyproject.toml | `pyproject.toml` |
| mypy 配置 | `pyproject.toml [tool.mypy]` |
| Strict Island 配置 | `pyproject.toml [tool.engram.mypy]` |
| 环境变量参考 | `docs/reference/environment_variables.md` |
| CI 门禁 Runbook | `docs/dev/ci_gate_runbook.md` |
| 迭代操作手册 | `docs/dev/iteration_runbook.md` |
| 迭代本地草稿指南 | `docs/dev/iteration_local_drafts.md` |

### 外部参考文档

| 资源 | 链接 |
|------|------|
| Cursor Agent 模式 | [docs.cursor.com/chat/agent](https://docs.cursor.com/chat/agent) |
| Model Context Protocol | [docs.cursor.com/context/model-context-protocol](https://docs.cursor.com/context/model-context-protocol) |
| MCP Server 添加方法 | [docs.cursor.com/.../model-context-protocol#adding-mcp-servers](https://docs.cursor.com/context/model-context-protocol#adding-mcp-servers) |
| MCP Server 目录 | [cursor.directory](https://cursor.directory/) |
| Cursor Rules 配置 | [docs.cursor.com/context/rules-for-ai](https://docs.cursor.com/context/rules-for-ai) |
| Project Rules | [docs.cursor.com/.../rules-for-ai#project-rules](https://docs.cursor.com/context/rules-for-ai#project-rules) |
| Claude API 文档 | [docs.anthropic.com](https://docs.anthropic.com) |

> **MCP 配置 SSOT**：本仓库的 MCP 配置以 `configs/mcp/.mcp.json.example` 为权威来源，外部链接仅作行为参考。

---

## 故障排查

### mypy baseline 模式失败

```bash
# 查看新增错误
python scripts/ci/check_mypy_gate.py --verbose

# 如果是误报，考虑添加 type: ignore 注释
# 如果需要更新 baseline（需 reviewer 批准）
python scripts/ci/check_mypy_gate.py --write-baseline
```

### CLI 入口点检查失败

```bash
# 查看详细信息
python scripts/verify_cli_entrypoints_consistency.py --verbose

# 常见原因：
# - 文档中引用了未定义的命令
# - pyproject.toml 与 cli_entrypoints.md 不同步
# - 使用了根目录 wrapper 脚本而非官方 CLI
```

### 格式检查失败

```bash
# 自动修复
make format

# 或使用 ruff
ruff format src/ tests/
```

---

更新时间：2026-02-02（添加 check-workflow-contract-doc-anchors 门禁、suggest_workflow_contract_updates 建议工具）

---

## 变更日志

### v1.8 (2026-02-02)
- 同步 `make ci` 依赖：将 `check-mcp-error-contract` 和 `check-mcp-error-docs-sync` 移到「分步执行」部分
- 更新核心门禁脚本表格：添加 `check_mcp_jsonrpc_error_docs_sync.py` 和 `check_workflow_contract_version_policy.py` 映射
- 更新 Gateway 代理最小门禁命令：添加 `check-gateway-public-api-docs-sync`、`check-gateway-import-surface`、`check-mcp-error-contract`、`check-mcp-error-docs-sync`
- 更新门禁命令速查表：Gateway 代理完整包含所有 gateway 和 MCP 相关检查
- 更新共享文件单点负责表格：添加 `scripts/ci/workflow_contract.v1.json`
- 同步更新根目录 AGENTS.md

### v1.7 (2026-02-02)
- 增强子代理分工建议：添加「本仓库映射」说明 Cursor Rules/MCP SSOT 位置
- 同步更新外部参考链接，确认 Cursor Rules/MCP install/MCP directory 链接有效

### v1.6 (2026-02-02)
- 同步 Makefile 实际目标：按 `make ci` 依赖顺序列出所有检查命令
- 添加 Makefile alias targets：`typecheck-gate`, `check-cli-entrypoints`, `check-noqa-policy`, `check-no-root-wrappers`, `check-mcp-error-contract`, `mypy-baseline-update`
- 更新核心门禁脚本表格：添加 gateway 相关检查脚本映射
- 更新子代理门禁命令：区分「必须运行」（`make ci` 包含）和「可选独立检查」

### v1.5 (2026-02-02)
- `make ci` 依赖链对齐 GitHub Actions：添加 `check-iteration-docs`, `validate-workflows-strict`, `check-workflow-contract-docs-sync`
- 更新门禁命令速查表：CI 代理添加 `check-workflow-contract-docs-sync`；文档代理添加 `check-iteration-docs`；Gateway 代理添加 `check-gateway-public-api-surface`
- 同步更新 `workflow_contract.v1.json` 的 `make.targets_required`

### v1.4 (2026-02-02)
- 新增「最小验证工作流」段落中对迭代回归 Runbook 的链接
- 创建 Iteration 13 Regression Runbook（含最小门禁命令块、预期输出关键字、通过标准）
- 同步更新根目录 AGENTS.md

### v1.3 (2026-02-01)
- 新增「Cursor 2.4+ Subagents 能力与本仓库实践」章节
- 引用 Cursor Changelog 官方 Subagents 要点
- 落地共享文件单点负责、baseline 串行更新、按目录拆分等实践
- 同步更新根目录 AGENTS.md 摘要

### v1.2 (2026-02-01)
- 新增「标准执行顺序」章节：先复现 → 再修复 → 再验证
- 新增「共享文件单点负责规则」详细表格
- 添加 Cursor Agent/MCP 外部文档链接
- 补充外部参考文档资源列表

### v1.1 (2026-02-01)
- 新增 `GatewayDeps.for_testing()` 严格模式，访问未注入依赖时抛出 RuntimeError
- 新增 Gateway 测试编写规范章节
