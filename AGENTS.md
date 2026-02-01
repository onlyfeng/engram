# AI Agent 协作快速指南

> 本文档为 `docs/dev/agents.md` 的摘要版本。完整指南请参阅 **[docs/dev/agents.md](docs/dev/agents.md)**。

---

## 核心门禁命令

```bash
# 一键运行所有 CI 检查（推荐）
make ci

# 分步执行（按 make ci 依赖顺序）
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
make check-gateway-di-boundaries        # Gateway DI 边界检查（禁止 deps.db 直接使用）
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
pytest tests/ci/ -q                     # CI 脚本测试
```

> **迭代回归 Runbook**：详细的最小门禁命令块（含预期输出关键字和通过标准）请参阅：[Iteration 13 Regression Runbook](docs/acceptance/iteration_13_regression.md#最小门禁命令块)

---

## 常用工作流

### 添加新功能

```bash
make test        # 运行测试
make ci          # 检查代码质量
```

### 修改 SQL 迁移

```bash
make migrate-plan           # 查看迁移计划
make check-migration-sanity # 验证 Sanity
make verify-permissions     # 验证权限
```

### 修复 CI 失败

```bash
make format          # 修复格式问题
ruff check --fix .   # 自动修复 lint 问题
```

---

## 标准执行顺序

> **重要**：任何修复工作都应遵循以下顺序，避免盲目修改导致问题扩散。

```bash
# 1. 先复现问题（理解当前状态）
make ci                 # 或运行特定失败的门禁命令
git diff               # 查看当前变更

# 2. 定位并修复问题
# ... 根据错误信息进行修改 ...

# 3. 再次验证（确保修复完整）
make ci                 # 完整 CI 检查
make test              # 可选：运行测试（需数据库）
```

**核心原则**：
- **先复现**：不要假设问题原因，先运行门禁确认实际错误
- **再修复**：根据错误输出精准修改，避免过度修复
- **再验证**：修复后必须运行 `make ci` 确保无新增问题

---

## 子代理分工

> 完整拆分模板与门禁命令：**[docs/dev/agents.md#subagent-拆分模板](docs/dev/agents.md#subagent-拆分模板)**
>
> 外部参考：[Cursor Agent 模式](https://docs.cursor.com/chat/agent) | [MCP 集成](https://docs.cursor.com/context/model-context-protocol) | [Cursor Rules](https://docs.cursor.com/context/rules-for-ai) | [MCP Server 目录](https://cursor.directory/)

| 代理角色 | 主要职责 | 关键路径 | 最小门禁 |
|----------|----------|----------|----------|
| **SQL / 数据库** | SQL 迁移、权限配置 | `sql/*.sql` | `check-migration-sanity`, `verify-permissions` |
| **CI / 工程质量** | CI 脚本、mypy baseline | `scripts/ci/*.py` | `typecheck`, `validate-workflows-strict`, `check-workflow-contract-docs-sync`, `check-workflow-contract-version-policy` |
| **文档** | 架构文档、API 契约 | `docs/**/*.md` | `check-env-consistency`, `check-iteration-docs` |
| **Gateway / 业务逻辑** | Handler、Service 开发 | `src/engram/gateway/` | `lint`, `check-gateway-di-boundaries`, `check-gateway-public-api-surface`, `check-gateway-public-api-docs-sync`, `check-gateway-import-surface`, `check-gateway-correlation-id-single-source`, `check-mcp-error-contract`, `check-mcp-error-docs-sync` |

### 共享文件单点负责规则

以下文件必须由**单一代理**负责修改，禁止多代理并行编辑：

| 文件 | 负责代理 | 说明 |
|------|----------|------|
| `pyproject.toml` | CI 代理 | 依赖、入口点、工具配置 |
| `Makefile` | CI 代理 | 构建和门禁目标 |
| `scripts/ci/mypy_baseline.txt` | CI 代理 | mypy 基线（需串行更新） |
| `scripts/ci/workflow_contract.v1.json` | CI 代理 | CI 合约定义（需与 ci.yml 同步） |
| `.github/workflows/ci.yml` | CI 代理 | CI 流水线定义 |
| `docs/reference/environment_variables.md` | 文档代理 | 环境变量 SSOT |

> 详细说明参见 [CI 门禁架构概览](docs/dev/ci_gate_runbook.md#ci-门禁架构概览)

### 子代理协调关键规则

1. **baseline 文件串行更新**：`mypy_baseline.txt` 更新需串行执行，避免合并冲突
2. **共享文件单代理负责**：`pyproject.toml`、`Makefile` 由单一代理负责
3. **同一文件禁止并行编辑**：多代理不要同时编辑同一文件
4. **合并前统一验证**：所有代理完成后运行 `make ci` 验证集成
5. **跨代理任务标注**：使用 `# TODO: @<agent-role>` 明确责任归属

---

## Cursor 2.4+ Subagents 与本仓库实践

> **参考来源**：[Cursor Changelog](https://cursor.com/cn/changelog) - 2.4 版本（2026-01-22）

根据 Cursor 官方 Subagents 能力（独立运行、上下文隔离、并行处理），本仓库制定以下协调规则：

| Cursor 能力 | 本仓库实践 |
|-------------|-----------|
| **上下文隔离** | 按目录拆分代理职责（SQL/CI/文档/Gateway） |
| **并行运行** | 不同代理处理不同目录，避免文件冲突 |
| **可配置工具** | 每类代理有专属最小门禁命令集 |

**关键约束**：
- 共享文件（`pyproject.toml`、`Makefile`、`mypy_baseline.txt`）单点负责，禁止多代理并行编辑
- baseline 串行更新，避免合并冲突
- 合并前统一运行 `make ci` 验证

> 详细说明参见 [docs/dev/agents.md#cursor-24-subagents-能力与本仓库实践](docs/dev/agents.md#cursor-24-subagents-能力与本仓库实践)

---

## Cursor / Agent CLI 版本差异与建议

### 版本差异说明

| 特性 | Cursor Agent | Claude CLI / API |
|------|--------------|------------------|
| 工具调用 | 内置文件操作工具 | 需配置 MCP 或自定义工具 |
| 上下文窗口 | 自动管理、滚动摘要 | 手动管理或使用会话 |
| 并行执行 | 支持多工具并行调用 | 取决于集成方式 |
| 项目感知 | 自动索引工作区 | 需手动提供项目结构 |

### 子代理并行使用建议

1. **任务隔离**：不同代理处理不同目录/模块，避免文件冲突
   - SQL 代理 → `sql/`, `docs/logbook/`
   - CI 代理 → `scripts/ci/`, `.github/`
   - 文档代理 → `docs/`（非 logbook）
   - Gateway 代理 → `src/engram/`

2. **协调机制**：
   - 使用 Git 分支隔离并行工作
   - 共享文件（如 `pyproject.toml`）由单一代理负责
   - 完成后统一运行 `make ci` 验证集成

3. **通信约定**：
   - 代理间通过文档或注释传递依赖信息
   - 跨代理任务明确标注 `# TODO: @<agent-role>` 

4. **避免冲突**：
   - 同一文件不要由多个代理同时编辑
   - baseline 文件（`mypy_baseline.txt`）更新需串行执行

---

## 相关资源

| 资源 | 路径 |
|------|------|
| 完整 Agent 指南 | [docs/dev/agents.md](docs/dev/agents.md) |
| Subagent 拆分模板 | [docs/dev/agents.md#subagent-拆分模板](docs/dev/agents.md#subagent-拆分模板) |
| 草稿→SSOT 晋升流程 | [docs/dev/agents.md#5-草稿ssot-晋升流程](docs/dev/agents.md#5-草稿ssot-晋升流程) |
| CI 门禁 Runbook | [docs/dev/ci_gate_runbook.md](docs/dev/ci_gate_runbook.md) |
| 迭代回归 Runbook | [docs/acceptance/iteration_13_regression.md](docs/acceptance/iteration_13_regression.md) |
| CI 配置 | `.github/workflows/ci.yml` |
| Makefile | `Makefile` |
| mypy 配置 | `pyproject.toml [tool.mypy]` |
| 环境变量参考 | [docs/reference/environment_variables.md](docs/reference/environment_variables.md) |
| MCP 配置示例 | [configs/mcp/.mcp.json.example](configs/mcp/.mcp.json.example)（SSOT） |

> **MCP 配置 SSOT**：本仓库的 MCP 配置以 `configs/mcp/.mcp.json.example` 为权威来源，外部文档链接仅作行为参考。

---

更新时间：2026-02-02（同步 Makefile 实际目标：添加 gateway 相关检查命令、补充 alias targets）
