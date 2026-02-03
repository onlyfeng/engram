# CI 门禁 Runbook

> 本文档定义所有 CI 门禁的环境变量、阈值、变更窗口、回滚策略、阶段推进检查表和例外审批流程。

---

## 目录

- [CI 门禁架构概览](#ci-门禁架构概览)
  - [SSOT（Single Source of Truth）文件列表](#ssotsingle-source-of-truth文件列表)
  - [本地最小复现命令集](#本地最小复现命令集)
  - [CI Job/Step 映射表](#ci-jobstep-映射表)
  - [共享文件单代理负责规则](#共享文件单代理负责规则)
  - [共享文件变更最小验证命令集（ci.yml/Makefile/workflow_contract）](#共享文件变更最小验证命令集ciymlmakefileworkflow_contract)
- [0. 门禁快速参考](#0-门禁快速参考)
  - [0.1 Makefile 门禁目标汇总](#01-makefile-门禁目标汇总)
  - [0.2 Makefile 与 CI Workflow 差异对照](#02-makefile-与-ci-workflow-差异对照)
  - [0.3 门禁详细参考表](#03-门禁详细参考表)
  - [0.4 CI 测试隔离门禁详解](#04-ci-测试隔离门禁详解)
  - [0.5 缓存清理排障流程](#05-缓存清理排障流程)
  - [0.6 MCP Doctor 诊断 Runbook](#06-mcp-doctor-诊断-runbook)
  - [0.6.4 MCP Doctor 检查矩阵（附录）](#064-mcp-doctor-检查矩阵附录)
- [1. 门禁变量总览](#1-门禁变量总览)
  - [1.1 mypy 门禁变量](#11-mypy-门禁变量)
  - [1.2 ruff 门禁变量](#12-ruff-门禁变量)
  - [1.3 SQL 迁移门禁变量](#13-sql-迁移门禁变量)
  - [1.4 Workflow Contract 门禁](#14-workflow-contract-门禁)
  - [1.5 noqa 策略门禁](#15-noqa-策略门禁)
  - [1.6 no_root_wrappers 门禁](#16-no_root_wrappers-门禁)
  - [1.7 Gateway DI 边界门禁](#17-gateway-di-边界门禁)
- [2. 推荐变更窗口](#2-推荐变更窗口)
- [3. 回滚步骤](#3-回滚步骤)
  - [3.1 mypy 门禁回滚](#31-mypy-门禁回滚)
  - [3.2 ruff 门禁回滚](#32-ruff-门禁回滚)
  - [3.3 SQL 迁移门禁回滚](#33-sql-迁移门禁回滚)
  - [3.4 Workflow Contract 门禁回滚](#34-workflow-contract-门禁回滚)
  - [3.5 noqa 策略门禁回滚](#35-noqa-策略门禁回滚)
  - [3.6 no_root_wrappers 门禁回滚](#36-no_root_wrappers-门禁回滚)
  - [3.7 Gateway DI 边界门禁回滚](#37-gateway-di-边界门禁回滚)
- [4. 阶段推进 Checklist](#4-阶段推进-checklist)
  - [4.1 Phase 0 → Phase 1](#41-phase-0--phase-1)
  - [4.2 Phase 1 → Phase 2](#42-phase-1--phase-2)
  - [4.3 Phase 2 → Phase 3](#43-phase-2--phase-3)
- [5. 例外审批模板](#5-例外审批模板)
  - [5.1 Baseline 净增例外](#51-baseline-净增例外)
  - [5.2 noqa 例外](#52-noqa-例外)
  - [5.3 type: ignore 例外](#53-type-ignore-例外)
  - [5.4 no_root_wrappers 例外](#54-no_root_wrappers-例外)
  - [5.5 Gateway deps.db 例外](#55-gateway-depsdb-例外)
- [6. 监控指标](#6-监控指标)
- [7. 相关文档](#7-相关文档)
- [8. 变更检查清单](#8-变更检查清单)

---

## CI 门禁架构概览

> 本小节提供 CI 门禁的架构级概览，包括 SSOT 文件、本地复现命令、CI Job 映射以及共享文件修改规则。

### SSOT（Single Source of Truth）文件列表

CI 门禁依赖以下 3 条主线的配置文件作为 SSOT：

| 主线 | SSOT 文件 | 说明 | CI 对应 Job |
|------|-----------|------|-------------|
| **类型检查** | `pyproject.toml` | mypy 配置、strict-island 路径、工具配置 | `lint` |
| | `scripts/ci/mypy_baseline.txt` | mypy 错误基线（当前已知错误列表） | `lint` |
| **代码风格** | `pyproject.toml` | ruff lint/format 规则、lint-island 路径 | `lint` |
| **Workflow 合约** | `scripts/ci/workflow_contract.v1.json` | CI job/step 合约定义 | `workflow-contract` |

**配置位置详情**：

| SSOT 文件 | 配置节 | 用途 |
|-----------|--------|------|
| `pyproject.toml` | `[tool.mypy]` | mypy 全局配置 |
| | `[tool.engram.mypy].strict_island_paths` | mypy strict-island 模块列表 |
| | `[tool.ruff]` | ruff 格式化配置 |
| | `[tool.ruff.lint]` | ruff lint 规则 |
| | `[tool.engram.ruff].lint_island_paths` | ruff lint-island 模块列表 |
| | `[tool.engram.ruff].p1_rules` | P1 规则集（B, UP, SIM, PERF, PTH） |
| `scripts/ci/mypy_baseline.txt` | - | 行格式：`file:line: error: message [error-code]` |
| `scripts/ci/workflow_contract.v1.json` | `ci.job_ids` | 必须存在的 Job ID |
| | `ci.job_names` | Job 显示名称 |
| | `required_jobs[].required_steps` | 必须存在的 Step |
| | `frozen_step_text.allowlist` | 冻结的 Step 名称 |
| | `make.targets_required` | 必须存在的 Makefile 目标 |

### 本地最小复现命令集

以下命令可在本地复现 CI 门禁检查（注意：`make ci` 为本地聚合，可能包含 CI 未覆盖项，差异见 0.2）：

```bash
# ============================================
# 一键运行所有 CI 检查（本地聚合）
# ============================================
make ci

# ============================================
# lint job
# ============================================
make lint
make format-check
make typecheck-gate
make typecheck-strict-island

# ============================================
# Workflow 合约 job
# ============================================
make validate-workflows-strict
make check-workflow-contract-docs-sync
make check-workflow-contract-error-types-docs-sync
make check-workflow-contract-version-policy
make check-workflow-contract-doc-anchors
make check-workflow-contract-internal-consistency
make check-workflow-make-targets-consistency

# ============================================
# Gateway/MCP 相关 job
# ============================================
make check-gateway-di-boundaries
make check-gateway-error-reason-usage
make check-gateway-import-surface
make check-gateway-public-api-surface
make check-gateway-public-api-docs-sync
make check-gateway-correlation-id-single-source
make check-mcp-error-contract
make check-mcp-error-docs-sync

# ============================================
# 迭代相关 job
# ============================================
make check-iteration-docs
make test-iteration-tools
make check-iteration-fixtures-freshness

# ============================================
# 其他 job
# ============================================
make check-env-consistency
make check-schemas
make check-logbook-consistency
make check-migration-sanity
make check-scm-sync-consistency
make check-cli-entrypoints
make check-ci-test-isolation
pytest tests/logbook/test_sql_migrations_safety.py -v
git ls-files .iteration  # 期望无输出
```

#### MCP/Gateway 最小回归矩阵

当修改 `src/engram/gateway/api_models.py`、`src/engram/gateway/routes.py`、`src/engram/gateway/middleware.py` 或 `scripts/ops/mcp_doctor.py` 任一处时，**必须**执行以下最小回归矩阵：

```bash
pytest scripts/tests/test_mcp_doctor.py -q
pytest tests/gateway/test_mcp_cors_preflight.py -q
pytest tests/gateway/test_mcp_jsonrpc_contract.py -q

# 本地运行 Gateway 后执行
python scripts/ops/mcp_doctor.py --gateway-url http://127.0.0.1:8787

# 需要鉴权时（示例）
python scripts/ops/mcp_doctor.py --gateway-url http://127.0.0.1:8787 \
  --header "Authorization: Bearer <token>"
```

### CI Job/Step 映射表

从 `.github/workflows/ci.yml` 摘要的 Job 与本地命令对应关系：

| CI Job | 主要 Step | 本地复现命令 | 说明 |
|--------|-----------|--------------|------|
| `lint` | Run ruff check (lint) | `make lint` | 代码风格检查 |
| | Run ruff format check | `make format-check` | 格式检查 |
| | Count/resolve mypy gate | - | baseline_count + gate 解析在 CI 内部完成 |
| | Run mypy type check | `make typecheck-gate` | mypy 检查（baseline gate） |
| | Run mypy strict-island check | `make typecheck-strict-island` | 核心模块零错误 |
| `no-iteration-tracked` | Check no .iteration files tracked | `git ls-files .iteration` | 期望无输出 |
| `env-var-consistency` | Check environment variable consistency | `make check-env-consistency` | 环境变量一致性 |
| `schema-validate` | Run schema validation | `make check-schemas` | JSON Schema 校验 |
| `logbook-consistency` | Check logbook configuration consistency | `make check-logbook-consistency` | Logbook 配置一致性 |
| `migration-sanity` | Check SQL migration plan sanity | `make check-migration-sanity` | SQL 迁移计划 Sanity |
| `sql-safety` | Run SQL safety check | `pytest tests/logbook/test_sql_migrations_safety.py -v` | 高危语句检测（仅 CI） |
| `gateway-di-boundaries` | Check Gateway DI boundaries | `make check-gateway-di-boundaries` | DI 边界检查 |
| `gateway-error-reason-usage` | Check Gateway ErrorReason usage | `make check-gateway-error-reason-usage` | ErrorReason 使用规范 |
| `gateway-import-surface` | Check Gateway import surface | `make check-gateway-import-surface` | __init__ 懒加载策略 |
| `gateway-public-api-surface` | Check Gateway Public API import surface | `make check-gateway-public-api-surface` | Public API 导入表面 |
| | Check Gateway Public API docs sync | `make check-gateway-public-api-docs-sync` | Public API 文档同步 |
| `gateway-correlation-id-single-source` | Check correlation_id single source | `make check-gateway-correlation-id-single-source` | correlation_id 单一来源 |
| `mcp-error-contract` | Check MCP JSON-RPC error contract | `make check-mcp-error-contract` | MCP 错误码合约 |
| | Check MCP JSON-RPC error docs sync | `make check-mcp-error-docs-sync` | MCP 错误码文档同步 |
| `iteration-docs-check` | Check iteration docs consistency | `make check-iteration-docs` | 本地为超集（含占位符/本地产物链接检查） |
| `ci-test-isolation` | Check CI test isolation | `make check-ci-test-isolation` | CI 测试隔离 |
| `iteration-tools-test` | Run iteration tools tests | `make test-iteration-tools` | tests/iteration |
| | Check iteration fixtures freshness | `make check-iteration-fixtures-freshness` | fixtures 新鲜度 |
| `scm-sync-consistency` | Check SCM Sync consistency | `make check-scm-sync-consistency` | SCM Sync 一致性 |
| `cli-entrypoints-consistency` | Check CLI entrypoints consistency | `make check-cli-entrypoints` | CLI 入口点一致性 |
| `workflow-contract` | Validate workflow contract | `make validate-workflows-strict` | Workflow 合约校验 |
| | Check workflow contract docs sync | `make check-workflow-contract-docs-sync` | 文档同步 |
| | Check workflow contract error types docs sync | `make check-workflow-contract-error-types-docs-sync` | Error Types 同步 |
| | Check workflow contract version policy | `make check-workflow-contract-version-policy` | 版本策略 |
| | Check workflow contract doc anchors | `make check-workflow-contract-doc-anchors` | 文档锚点 |
| | Check workflow contract internal consistency | `make check-workflow-contract-internal-consistency` | 合约不变量 |
| | Check workflow make targets consistency | `make check-workflow-make-targets-consistency` | Make targets 一致性 |
| `test` | Run unit and integration tests | `pytest tests/gateway/ -v` + `pytest tests/acceptance/ -v` | 需要数据库 |

### 共享文件单代理负责规则

以下文件必须由**单一代理/开发者**串行修改，禁止多代理并行编辑：

| 文件 | 负责角色 | 原因 | 冲突风险 |
|------|----------|------|----------|
| `pyproject.toml` | CI 代理 | 依赖、入口点、工具配置集中管理 | 高：多处配置交叉引用 |
| `Makefile` | CI 代理 | 构建和门禁目标定义 | 中：目标名称变更影响文档和 CI |
| `.github/workflows/ci.yml` | CI 代理 | CI 流水线定义 | 高：与 workflow_contract 强关联 |
| `scripts/ci/mypy_baseline.txt` | CI 代理 | mypy 基线文件 | 高：行级冲突难以解决 |
| `scripts/ci/workflow_contract.v1.json` | CI 代理 | CI 合约定义（合约系统关键路径，建议单 PR/单 owner 串行修改） | 高：与 ci.yml/文档/校验脚本强耦合 |
| `scripts/ci/check_workflow_contract_version_policy.py` | CI 代理 | 合约系统关键路径（与 workflow_contract.v1.json 强耦合，建议单 PR/单 owner 串行修改） | 高：版本策略与合约字段同步 |
| `scripts/ci/check_workflow_contract_docs_sync.py` | CI 代理 | 合约系统关键路径（与 workflow_contract.v1.json 强耦合，建议单 PR/单 owner 串行修改） | 高：文档/合约同步风险 |
| `scripts/ci/check_workflow_contract_coupling_map_sync.py` | CI 代理 | 合约系统关键路径（与 workflow_contract.v1.json 强耦合，建议单 PR/单 owner 串行修改） | 高：耦合映射同步风险 |
| `scripts/ci/render_workflow_contract_docs.py` | CI 代理 | 合约系统关键路径（与 workflow_contract.v1.json 强耦合，建议单 PR/单 owner 串行修改） | 高：生成文档一致性风险 |
| `scripts/ci/workflow_contract_common.py` | CI 代理 | 合约系统关键路径（与 workflow_contract.v1.json 强耦合，建议单 PR/单 owner 串行修改） | 高：共享逻辑影响多脚本 |
| `docs/reference/environment_variables.md` | 文档代理 | 环境变量 SSOT | 中：多处文档引用 |

这些路径由 CODEOWNERS 强制双审。
即使是非 CI 相关的 `Makefile` 变更也会触发双审，这是有意的保守策略。

**协作规则**：

1. **串行更新**：修改上述文件时，确保同一时间只有一个代理在操作
2. **合并前验证**：完成修改后运行 `make ci` 确保无冲突
3. **变更同步**：
   - 修改 `ci.yml` 时，同步更新 `workflow_contract.v1.json`
   - 修改 `pyproject.toml` 中的 mypy/ruff 配置时，同步更新本文档
   - 新增环境变量时，同步更新 `docs/reference/environment_variables.md`

### 共享文件变更最小验证命令集（ci.yml/Makefile/workflow_contract）

当修改 `.github/workflows/ci.yml`、`Makefile`、`scripts/ci/workflow_contract.v1.json` 或其相关文档时，至少执行以下命令以确保合约与文档一致。

**上位规则**：任何变更完成后**必须**运行 `make ci`。更完整的流程与顺序请参见 [维护指南：0-快速变更流程（SSOT-first）](../ci_nightly_workflow_refactor/maintenance.md#0-快速变更流程ssot-first)。

```bash
# 核心合约校验（CI 使用的严格模式）
make validate-workflows-strict

# 合约与文档同步（contract.md、maintenance.md 等）
make check-workflow-contract-docs-sync

# 合约版本策略（版本升级规则、兼容性）
make check-workflow-contract-version-policy

# 文档锚点一致性（contract.md 内部锚点）
make check-workflow-contract-doc-anchors

# 依赖耦合映射一致性（coupling map 同步）
make check-workflow-contract-coupling-map-sync

# 文档生成一致性（生成块/片段与源数据一致）
make check-workflow-contract-docs-generated

# 合约内部一致性（字段结构、交叉引用）
make check-workflow-contract-internal-consistency

# Makefile 目标一致性（合约声明的目标必须存在）
make check-workflow-make-targets-consistency

# CI 脚本测试（验证脚本逻辑与契约行为）
pytest tests/ci/ -q
```

---

## 0. 门禁快速参考

### 0.1 Makefile 门禁目标汇总

| 类别 | Make 目标 | 说明 | CI 对应 Job |
|------|-----------|------|-------------|
| **代码风格** | `lint` | ruff check（lint 规则检查） | `lint` |
| | `format-check` | ruff format（格式检查） | `lint` |
| **类型检查** | `typecheck` | mypy 直接检查 | - |
| | `typecheck-gate` | mypy baseline 模式（推荐） | `lint` |
| | `typecheck-strict-island` | mypy strict-island 模式 | `lint` |
| | `mypy-baseline-update` | 更新 mypy 基线文件 | - |
| | `mypy-metrics` | 收集 mypy 指标 | `lint`（baseline_count 口径） |
| | `check-mypy-metrics-thresholds` | 检查 mypy 指标阈值（warn only） | - |
| | `check-mypy-metrics-thresholds-fail` | 检查 mypy 指标阈值（fail 模式） | - |
| **一致性检查** | `check-env-consistency` | 环境变量一致性检查 | `env-var-consistency` |
| | `check-logbook-consistency` | Logbook 配置一致性检查 | `logbook-consistency` |
| | `check-schemas` | JSON Schema 校验 | `schema-validate` |
| | `check-cli-entrypoints` | CLI 入口点一致性检查 | `cli-entrypoints-consistency` |
| | `check-scm-sync-consistency` | SCM Sync 一致性检查 | `scm-sync-consistency` |
| **Gateway/MCP** | `check-gateway-error-reason-usage` | Gateway ErrorReason 使用规范 | `gateway-error-reason-usage` |
| | `check-gateway-public-api-surface` | Gateway Public API 导入表面 | `gateway-public-api-surface` |
| | `check-gateway-public-api-docs-sync` | Gateway Public API 文档同步 | `gateway-public-api-surface` |
| | `check-gateway-di-boundaries` | Gateway DI 边界检查（严格模式） | `gateway-di-boundaries` |
| | `check-gateway-import-surface` | Gateway __init__ 懒加载策略检查 | `gateway-import-surface` |
| | `check-gateway-correlation-id-single-source` | correlation_id 单一来源检查 | `gateway-correlation-id-single-source` |
| | `check-mcp-error-contract` | MCP JSON-RPC 错误码合约检查 | `mcp-error-contract` |
| | `check-mcp-error-docs-sync` | MCP JSON-RPC 错误码文档同步 | `mcp-error-contract` |
| | `check-mcp-config-docs-sync` | MCP 配置文档与 SSOT 同步 | - |
| **迭代文档** | `check-iteration-docs` | 迭代文档规范检查 | `iteration-docs-check` |
| | `check-iteration-evidence` | 迭代证据合约检查 | `iteration-docs-check` |
| | `check-iteration-fixtures-freshness` | 迭代 fixtures 新鲜度 | `iteration-tools-test` |
| | `check-min-gate-profiles-consistency` | 最小门禁 profile 一致性 | - |
| | `check-iteration-gate-profiles-contract` | 迭代门禁 profile 合约 | - |
| | `check-iteration-toolchain-drift-map-contract` | toolchain drift map 合约 | - |
| | `check-iteration-docs-generated-blocks` | 回归文档受控块一致性 | - |
| | `check-iteration-docs-headings` | regression 标题检查（阻断） | - |
| | `check-iteration-docs-headings-warn` | regression 标题检查（警告） | - |
| | `check-iteration-docs-superseded-only` | 仅检查 SUPERSEDED 一致性 | - |
| **Workflow 合约** | `validate-workflows` | Workflow 合约校验（默认模式） | - |
| | `validate-workflows-strict` | Workflow 合约校验（严格模式） | `workflow-contract` |
| | `check-workflow-contract-docs-sync` | 合约与文档同步 | `workflow-contract` |
| | `check-workflow-contract-error-types-docs-sync` | Error Types 文档同步 | `workflow-contract` |
| | `check-workflow-contract-version-policy` | 版本策略检查 | `workflow-contract` |
| | `check-workflow-contract-doc-anchors` | 文档锚点检查 | `workflow-contract` |
| | `check-workflow-contract-internal-consistency` | 合约内部一致性 | `workflow-contract` |
| | `check-workflow-contract-coupling-map-sync` | Coupling Map 同步 | - |
| | `check-workflow-make-targets-consistency` | Make targets 一致性 | `workflow-contract` |
| | `check-workflow-contract-docs-generated` | 文档受控块生成一致性 | - |
| **策略/隔离** | `check-noqa-policy` | noqa 注释策略检查 | - |
| | `check-no-root-wrappers` | 根目录 wrapper 禁止导入检查 | - |
| | `check-ci-test-isolation` | CI 测试隔离检查 | `ci-test-isolation` |
| **SQL 迁移** | `check-migration-sanity` | SQL 迁移计划 Sanity 检查 | `migration-sanity` |
| **综合** | `ci` | 运行所有 CI 检查 | 全部 jobs |
| | `regression` | 运行回归测试 | - |

### 0.2 Makefile 与 CI Workflow 差异对照

| 门禁 | Makefile 目标 | CI 执行位置 | 差异说明 |
|------|---------------|-------------|----------|
| **mypy metrics thresholds** | `check-mypy-metrics-thresholds` | - | 本地可选；CI 仅用 `mypy_metrics` 计算 baseline_count |
| **iteration docs** | `check-iteration-docs` | `iteration-docs-check` job | CI 仅跑 no-iteration-links + evidence contract；本地额外检查本地产物链接与占位符（warn-only） |
| **iteration toolchain drift map** | `check-iteration-toolchain-drift-map-contract` | - | 本地可选；CI 未覆盖 |
| **workflow doc anchors** | `check-workflow-contract-doc-anchors` | `workflow-contract` job | CI 有此检查；`make ci` 当前不包含 |
| **CI script tests** | `pytest tests/ci/ -q` | `workflow-contract` job | CI 运行 tests/ci（带 ignore 列表）；本地需手动执行 |
| **no-iteration-tracked** | - | `no-iteration-tracked` job | CI 额外检查 `.iteration/` 未被跟踪；本地可用 `git ls-files .iteration` |
| **noqa/no_root_wrappers** | `check-noqa-policy` / `check-no-root-wrappers` | - | 当前仅本地可选门禁，未接入 CI |
| **MCP config docs sync** | `check-mcp-config-docs-sync` | - | 仅本地校验，CI 未覆盖 |

### 0.3 门禁详细参考表

| 门禁名称 | 本地命令 | CI Job | 可配置变量 | 回滚方式 | 常见失败原因 | 修复路径 |
|----------|----------|--------|------------|----------|--------------|----------|
| **mypy baseline** | `make typecheck-gate` | `lint` | `ENGRAM_MYPY_GATE`, `ENGRAM_MYPY_MIGRATION_PHASE`, `ENGRAM_MYPY_GATE_OVERRIDE` | 设置 `ENGRAM_MYPY_GATE_OVERRIDE=baseline` | 新增类型错误 | 1) 修复错误 2) 更新 baseline `make mypy-baseline-update` |
| **mypy strict-island** | `make typecheck-strict-island` | `lint` | `pyproject.toml [tool.engram.mypy].strict_island_paths` | 从 strict_island_paths 移除模块 | 核心模块类型错误 | 修复错误，不建议移除模块 |
| **mypy baseline policy** | `python -m scripts.ci.check_mypy_baseline_policy` | `lint` (PR only) | - | - | baseline 净增缺少说明/标签 | 在 PR body 添加说明 section 和 issue 引用 |
| **ruff lint** | `make lint` | `lint` | `pyproject.toml [tool.ruff.lint]` | 添加规则到 `ignore` 列表 | 代码风格违规 | `ruff check --fix src/ tests/` |
| **ruff format** | `make format-check` | `lint` | `pyproject.toml [tool.ruff]` | - | 格式不一致 | `make format` |
| **noqa policy** | `make check-noqa-policy` | `lint` | `pyproject.toml [tool.engram.ruff].lint_island_paths` | CI 中添加 `\|\| true` | 裸 noqa 注释 | 添加错误码：`# noqa: E501` |
| **noqa policy (island)** | `python -m scripts.ci.check_noqa_policy --lint-island-strict` | - | 同上 | - | lint-island 路径缺少原因说明 | 添加原因：`# noqa: E501  # 原因` |
| **type: ignore policy** | `python -m scripts.ci.check_type_ignore_policy` | `lint` | - | CI 中添加 `\|\| true` | strict-island 路径下缺少错误码或说明 | 补充：`# type: ignore[code] - reason` |
| **no_root_wrappers** | `make check-no-root-wrappers` | `lint` | `scripts/ci/no_root_wrappers_allowlist.json` | 添加到 allowlist 或使用 inline marker | 从根目录导入 wrapper 模块 | 1) 改用 `engram.logbook.xxx` 导入 2) 添加到 allowlist 3) 使用 inline marker |
| **ci test isolation** | `make check-ci-test-isolation` | `lint` | - | - | 模块级 sys.path 污染、顶层 CI 模块导入、双模式导入 | 1) 改用 `scripts.ci.*` 导入 2) 移除模块级 sys.path 修改 3) 参见 [CI 测试隔离规范](./ci_test_isolation.md) |
| **env consistency** | `make check-env-consistency` | `env-var-consistency` | - | - | 环境变量文档/代码不一致 | 同步 `.env.example`, docs, 代码 |
| **schema validation** | `make check-schemas` | `schema-validate` | - | - | JSON Schema 校验失败 | 修复 schema 或 fixture |
| **migration sanity** | `make check-migration-sanity` | `migration-sanity` | - | - | SQL 文件命名/分类违规 | 参见 `docs/logbook/sql_file_inventory.md` |
| **workflow contract** | `make validate-workflows-strict` | `workflow-contract` | `scripts/ci/workflow_contract.v1.json` | 改用非严格模式 `validate-workflows` | job/step 名称变更 | 同步更新合约文件 |
| **cli entrypoints** | `make check-cli-entrypoints` | `cli-entrypoints-consistency` | - | - | pyproject.toml 与代码/文档不一致 | 同步 CLI 入口点 |
| **gateway di** | `make check-gateway-di-boundaries` | `gateway-di-boundaries` | `--phase removal --disallow-allow-markers` | 改用 `check-gateway-di-boundaries-compat` | DI 边界违规、残留 allow-markers | 参见 `docs/architecture/adr_gateway_di_and_entry_boundary.md` |
| **strict-island admission** | `python -m scripts.ci.check_strict_island_admission` | - | `configs/mypy_strict_island_candidates.json`、`CANDIDATE`、`CANDIDATES_FILE` | - | baseline 错误非 0 / 缺少 override / 配置错误 | 参见 `docs/dev/mypy_baseline.md` §5.3 |

### 0.4 CI 测试隔离门禁详解

> CI 测试隔离门禁检查 `tests/ci/` 和 `scripts/ci/` 目录下的导入规范，防止 `sys.path` 和 `sys.modules` 污染。

**核心约束**：

| 约束点 | 规则 | 说明 |
|--------|------|------|
| **唯一导入路径** | `scripts.ci.*` | 所有 CI 脚本必须通过此命名空间导入 |
| **唯一执行方式** | `python -m scripts.ci.<module>` 或从项目根目录运行 | 确保 `sys.path` 包含项目根目录 |
| **禁止顶层导入** | 不允许 `import validate_workflows` | 会污染 `sys.modules` |
| **禁止 sys.path 修改** | 模块顶层禁止 `sys.path.insert/append` | 影响所有测试 |

**本地复现命令**：

```bash
# Make 目标
make check-ci-test-isolation

# 直接调用脚本（带详细输出）
python -m scripts.ci.check_ci_test_isolation --verbose
```

**失败样例与定位**：

| 失败类型 | 错误输出示例 | 定位方式 | 修复方法 |
|----------|--------------|----------|----------|
| 顶层 CI 模块导入 | `tests/ci/test_xxx.py:5: from workflow_contract_common import ...` | `grep -rn "^from \w\+ import" tests/ci/*.py \| grep -v "scripts.ci"` | 改为 `from scripts.ci.workflow_contract_common import ...` |
| 模块级 sys.path 修改 | `scripts/ci/xxx.py:3: sys.path.insert(0, ...)` | `grep -rn "sys.path.insert\|sys.path.append" scripts/ci/*.py` | 移至 `if __name__ == "__main__":` 块内 |
| 双模式导入 | `scripts/ci/xxx.py:8: try: from .xxx ... except: from xxx` | `grep -A2 "^try:" scripts/ci/*.py \| grep "ImportError"` | 仅使用 `from scripts.ci.xxx import ...` |
| 运行时 sys.modules 污染 | `Failed: Test '...' has forbidden top-level CI modules in sys.modules: ['validate_workflows']` | 检查测试文件顶层导入 | 同"顶层 CI 模块导入" |

**典型错误输出与修复**：

```bash
# 错误示例 1: 静态检查失败
$ make check-ci-test-isolation
[ERROR] 发现违规导入:
  tests/ci/test_workflow_contract.py:5: from workflow_contract_common import discover_workflow_keys
  scripts/ci/check_xxx.py:8: import validate_workflows

# 修复：
# 将 `from workflow_contract_common import ...` 
# 改为 `from scripts.ci.workflow_contract_common import ...`

# 错误示例 2: pytest 运行时检测到污染
$ pytest tests/ci/ -v
FAILED tests/ci/test_xxx.py::test_foo
  Failed: Test 'test_foo' has forbidden top-level CI modules in sys.modules:
    ['check_workflow_contract_docs_sync', 'workflow_contract_common']

# 修复步骤：
# 1. 定位问题测试文件
grep -l "workflow_contract_common" tests/ci/*.py

# 2. 修改导入方式
# 错误: from workflow_contract_common import discover_workflow_keys
# 正确: from scripts.ci.workflow_contract_common import discover_workflow_keys

# 3. 清除缓存并重新测试
rm -rf tests/ci/__pycache__ scripts/ci/__pycache__
pytest tests/ci/ -q
```

**配置要求**：

`pyproject.toml` 必须配置 `pythonpath` 以确保 pytest 能正确解析 `scripts.ci.*` 导入：

```toml
[tool.pytest.ini_options]
pythonpath = ["."]
```

**相关文档**：

- [CI 测试隔离规范](./ci_test_isolation.md) - 完整规范与修复指南
- `tests/ci/conftest.py` - 运行时隔离 fixture 实现
- `scripts/ci/check_ci_test_isolation.py` - 静态检查脚本

### 0.5 缓存清理排障流程

> 当 CI 测试失败但本地无法复现时，缓存问题是常见原因。以下是标准排障流程。

**CI 缓存防护措施**：

CI 环境已配置 `PYTHONDONTWRITEBYTECODE=1`，从源头防止 .pyc 字节码缓存生成，减少缓存相关问题。

**本地清缓存复现步骤**：

```bash
# ============================================
# 步骤 1：清理 Python 字节码缓存
# ============================================
# 删除所有 __pycache__ 目录和 .pyc 文件
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find . -type f -name "*.pyc" -delete 2>/dev/null || true

# ============================================
# 步骤 2：清理 pytest 缓存
# ============================================
rm -rf .pytest_cache

# ============================================
# 步骤 3：清理 mypy 缓存
# ============================================
rm -rf .mypy_cache

# ============================================
# 步骤 4：使用 -B 标志运行 Python（可选）
# ============================================
# -B 标志等效于 PYTHONDONTWRITEBYTECODE=1
python -B -m pytest tests/ci/ -v

# ============================================
# 步骤 5：设置环境变量后运行（推荐）
# ============================================
PYTHONDONTWRITEBYTECODE=1 make ci
```

**一键清理脚本**：

```bash
# 将以下内容保存为 scripts/clean_caches.sh（可选）
#!/bin/bash
set -e
echo "清理 Python 字节码缓存..."
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find . -type f -name "*.pyc" -delete 2>/dev/null || true
echo "清理 pytest 缓存..."
rm -rf .pytest_cache
echo "清理 mypy 缓存..."
rm -rf .mypy_cache
echo "缓存清理完成"
```

**常见缓存问题症状**：

| 症状 | 可能原因 | 解决方案 |
|------|----------|----------|
| CI 失败但本地通过 | .pyc 缓存了旧代码 | 清理 `__pycache__`，使用 `PYTHONDONTWRITEBYTECODE=1` |
| import 错误间歇性出现 | 残留的 .pyc 文件与源码不匹配 | 清理所有 .pyc 文件 |
| mypy 报告与代码不一致 | mypy 缓存过期 | 清理 `.mypy_cache` |
| pytest 收集测试失败 | pytest 缓存了旧的测试发现结果 | 清理 `.pytest_cache` |

**子进程测试的缓存隔离**：

`tests/ci/helpers/subprocess_env.py` 中的 `get_subprocess_env()` 函数已默认设置 `PYTHONDONTWRITEBYTECODE=1`，确保子进程测试不会生成缓存文件。

---

### 0.6 MCP Doctor 诊断 Runbook

> 用途：定位 Cursor MCP 接入失败、CORS 预检异常、tools/list 返回异常。
> 脚本：`scripts/ops/mcp_doctor.py`（Make 目标：`make mcp-doctor`）

#### 0.6.1 最小复现命令与预期

```bash
# 统一入口（建议）
make mcp-doctor

# JSON 输出（用于字段定位）
python scripts/ops/mcp_doctor.py --json --pretty
```

**预期**：
- 输出 7 个 `[OK]` 且退出码为 0
- JSON 中 `checks[].passed` 均为 `true`

**最小 curl 复现**：

```bash
GATEWAY_URL="http://127.0.0.1:8787"

curl -sf "$GATEWAY_URL/health"
# 预期：HTTP 200 + {"status":"ok",...}

curl -i -X OPTIONS "$GATEWAY_URL/mcp"
# 预期：HTTP 200/204 + Access-Control-Allow-* 头齐全

curl -sf -X POST "$GATEWAY_URL/mcp" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
# 预期：result.tools 为非空数组
```

**可用环境变量**：

| 变量 | 作用 |
|------|------|
| `GATEWAY_URL` | 覆盖 Gateway 地址 |
| `MCP_DOCTOR_TIMEOUT` | 超时时间（秒） |
| `MCP_DOCTOR_AUTHORIZATION` | 注入 Authorization 头 |

#### 0.6.2 失败分类（doctor JSON 输出字段）

**字段速览**：

| 字段 | 含义 |
|------|------|
| `status_code` | HTTP 状态码（health/options/tools/list） |
| `missing_headers` | CORS 响应缺失项（Allow-Origin/Allow-Methods/Allow-Headers 或缺失的允许 header 值） |
| `missing_expose_headers` | Access-Control-Expose-Headers 缺失的值（提示项，不单独导致失败） |
| `error` | 请求层错误（DNS/连接/超时/解析失败） |
| `response_preview` | 响应体预览（最多 200 字符） |

**分类与首要定位文件/函数**：
- **请求失败（`error` 非空）**：多为网络/超时/证书问题。首要定位 `scripts/ops/mcp_doctor.py:_request`（确认错误类型），以及对应入口 `src/engram/gateway/routes.py:health_check`、`src/engram/gateway/routes.py:mcp_options`、`src/engram/gateway/routes.py:mcp_endpoint`。
- **状态码异常（`status_code` ≠ 200/204）**：401/403 优先看 `src/engram/gateway/middleware.py:GatewayAuthMiddleware`；404/405 优先看 `src/engram/gateway/routes.py:register_routes` 与 `src/engram/gateway/routes.py:mcp_options`/`mcp_endpoint`；5xx 优先看 `src/engram/gateway/mcp_rpc.py:dispatch_jsonrpc_request` 与 `src/engram/gateway/mcp_rpc.py:to_jsonrpc_error`。
- **CORS 头缺失（`missing_headers`/`missing_expose_headers`）**：优先定位 `src/engram/gateway/api_models.py:MCP_CORS_HEADERS`、`src/engram/gateway/api_models.py:build_mcp_allow_headers`、`src/engram/gateway/routes.py:mcp_options`（OPTIONS 200/204），以及 `src/engram/gateway/middleware.py:_build_mcp_cors_headers`（401/403 注入）。若 `X-Correlation-ID` 或 Expose-Headers 异常，继续检查 `src/engram/gateway/routes.py:_make_cors_headers_with_correlation_id` 与 `src/engram/gateway/middleware.py:CorrelationIdMiddleware`。
- **响应结构异常（`response_preview` 非预期）**：tools/list 返回非 JSON 或结构不符时，优先定位 `src/engram/gateway/mcp_rpc.py:handle_tools_list`、`src/engram/gateway/mcp_rpc.py:get_tool_definitions`、`src/engram/gateway/routes.py:mcp_endpoint`。

#### 0.6.3 最小回归命令集合

```bash
make mcp-doctor
pytest scripts/tests/test_mcp_doctor.py -q
pytest tests/gateway/test_mcp_cors_preflight.py -q
pytest tests/gateway/test_mcp_jsonrpc_contract.py -q
```

#### 0.6.4 MCP Doctor 检查矩阵（附录）

| 检查项 | Doctor 请求/断言 | 通过标准 | 来源 |
|--------|------------------|----------|------|
| Gateway 健康检查 | `GET /health` | HTTP 200 | 来源：文档（`docs/gateway/02_mcp_integration_cursor.md`） |
| CORS 预检响应 | `OPTIONS /mcp` + `Access-Control-Request-Headers` | 200/204；Allow-Origin/Methods/Headers 完整 | 来源：测试（`tests/gateway/test_mcp_cors_preflight.py`） |
| JSON-RPC initialize | `POST /mcp` method=initialize | `result.protocolVersion`/`capabilities.tools`/`serverInfo` 字段齐全 | 来源：测试（`tests/gateway/test_mcp_jsonrpc_contract.py`） |
| JSON-RPC ping | `POST /mcp` method=ping | `result == {}` | 来源：测试（`tests/gateway/test_mcp_jsonrpc_contract.py`） |
| tools/list 工具集合 | `POST /mcp` method=tools/list | tools 数量=5；名称集合匹配 | 来源：测试（`tests/gateway/test_mcp_jsonrpc_contract.py`） |
| tools/list 输入 Schema | tools.*.inputSchema.type=object；required 字段匹配 | required 对齐（`memory_store`/`memory_query`/`evidence_upload`） | 来源：测试（`tests/gateway/test_mcp_jsonrpc_contract.py`） |
| evidence_upload Schema | `evidence_upload.inputSchema.properties` | keys 与 `content`/`content_type` 类型匹配 | 来源：测试（`tests/gateway/test_mcp_jsonrpc_contract.py`） |
| correlation_id 暴露 | tools/list 响应头 | `X-Correlation-ID` 格式 + Expose-Headers 含 `X-Correlation-ID` | 来源：测试（`tests/gateway/test_mcp_jsonrpc_contract.py`） |
| correlation_id 唯一性 | 连续 tools/list 多次 | `X-Correlation-ID` 不重复 | 来源：测试（`tests/gateway/test_mcp_jsonrpc_contract.py`） |
| 未知方法错误契约 | `POST /mcp` method=unknown/method | `error.code=-32601` 且 `error.data` 必填字段齐全 | 来源：测试（`tests/gateway/test_mcp_jsonrpc_contract.py`） |

---

## 1. 门禁变量总览

### 1.1 mypy 门禁变量

| 变量名 | 说明 | 有效值 | 默认值 | 配置位置 |
|--------|------|--------|--------|----------|
| `ENGRAM_MYPY_GATE` | mypy 门禁级别 | `baseline`, `strict`, `strict-island`, `warn`, `off` | `baseline` | 环境变量 / CI |
| `ENGRAM_MYPY_MIGRATION_PHASE` | 迁移阶段 | `0`, `1`, `2`, `3` | `0` | GitHub Repository Variables |
| `ENGRAM_MYPY_GATE_OVERRIDE` | 强制覆盖 gate（回滚用） | `baseline`, `strict`, `warn`, `off`, 空 | 空（不覆盖） | GitHub Repository Variables |
| `ENGRAM_MYPY_STRICT_THRESHOLD` | PR 切 strict 的阈值（Phase 1 时生效） | 非负整数 | `0` | GitHub Repository Variables |
| `ENGRAM_MYPY_METRICS_FAIL_ON_THRESHOLD` | 指标超阈值是否失败 | `true`, `false` | `false` | GitHub Repository Variables |
| `MYPY_GATE` | [兼容] 旧变量名 | 同 `ENGRAM_MYPY_GATE` | - | 环境变量（优先级低） |

**Gate 级别说明**：

| Gate 级别 | 行为 | 退出码 | 使用场景 |
|-----------|------|--------|----------|
| `baseline` | 对比基线，仅新增错误时失败 | 0=无新增, 1=有新增 | **当前默认**，日常开发与 CI |
| `strict` | 任何 mypy 错误都失败 | 0=无错误, 1=有错误 | 发布前检查、目标状态 |
| `strict-island` | 仅检查 strict island 模块 | 0=无错误, 1=有错误 | 核心模块保护 |
| `warn` | 输出错误但不阻断 | 始终 0 | 仅警告模式 |
| `off` | 跳过检查 | 始终 0 | 调试、实验性开发 |

**Gate 解析优先级**：

`ENGRAM_MYPY_GATE_OVERRIDE` 的优先级高于 `ENGRAM_MYPY_MIGRATION_PHASE`，可用于紧急回滚：

```
override > phase 逻辑
```

| 优先级 | 来源 | 说明 |
|--------|------|------|
| 1（最高） | `ENGRAM_MYPY_GATE_OVERRIDE` | 直接返回指定值，跳过 phase 逻辑 |
| 2 | `ENGRAM_MYPY_MIGRATION_PHASE` | 根据迁移阶段和分支类型解析 |
| 3（兜底） | 默认值 | phase=0 时返回 `baseline` |

**紧急回滚示例**：

```bash
# 方式 1: 强制使用 warn 模式（仅输出警告，不阻断 CI）
# 在 GitHub Settings > Secrets and variables > Actions > Variables 中设置
ENGRAM_MYPY_GATE_OVERRIDE: warn

# 方式 2: 强制跳过 mypy 检查
ENGRAM_MYPY_GATE_OVERRIDE: off
```

**配置文件位置**：

| 文件 | 说明 |
|------|------|
| `scripts/ci/mypy_baseline.txt` | mypy 基线文件 |
| `pyproject.toml [tool.mypy]` | mypy 配置 |
| `pyproject.toml [tool.engram.mypy].strict_island_paths` | Strict Island 路径列表 |

**mypy baseline policy 本地复现命令**：

当 PR 修改了 `scripts/ci/mypy_baseline.txt` 时，CI 会检查变更是否符合策略（净增需说明/标签）。

```bash
# 直接调用 Python 脚本（默认 base 为 origin/master 或 origin/main）
python -m scripts.ci.check_mypy_baseline_policy \
  --base-sha origin/main \
  --head-sha HEAD \
  --verbose

# 测试模式：使用预生成的 diff 文件
python -m scripts.ci.check_mypy_baseline_policy \
  --diff-file tests/fixtures/mypy_baseline_policy/sample_diff.txt

# 测试模式：模拟 PR body 和 labels
python -m scripts.ci.check_mypy_baseline_policy \
  --base-sha origin/main \
  --pr-body "### CI Baseline 变更检查\n关联 Issue: #123" \
  --pr-labels "tech-debt" \
  --verbose
```

**策略阈值说明**（以 [ADR §4](../architecture/adr_mypy_baseline_and_gating.md#4-baseline-变更评审规则) 为准）：

| 净增范围 | 要求 | 审批要求 | 说明 |
|----------|------|----------|------|
| 净增 ≤ 0 | 无 | 无需特批 | 直接通过（修复或不变） |
| **净增 > 0** | PR body 包含 `### CI Baseline 变更检查` section + issue 引用 | 1 位 Reviewer | 说明变更原因 |
| **净增 > 5** | PR labels 包含 `tech-debt` 或 `type-coverage` | 2 位 Reviewer | 标记技术债务 |
| **净增 > 10** | 严格警告，需说明拆分方案 | Tech Lead 审批 | 建议拆分 PR |

> **note 行说明**：baseline 包含 `error:`、`warning:`、`note:` 三类行，均纳入净增计算。`note:` 行通常跟随 import 错误，修复 error 时会一并消失。

**常见失败原因与修复路径**：

| 失败原因 | 修复方法 |
|----------|----------|
| 缺少 `### CI Baseline 变更检查` section | 在 PR body 中添加 baseline 变更说明小节 |
| 缺少 issue 引用 | 在 PR body 中添加 `#123` 或完整 GitHub issue URL |
| 净增 > 5 缺少标签 | 添加 `tech-debt` 或 `type-coverage` 标签 |
| 净增 > 10 | 考虑拆分 PR，减少单次 baseline 增量 |

> 详见：[5.1 Baseline 净增例外](#51-baseline-净增例外) 中的 PR 描述模板

### 1.2 ruff 门禁变量

| 变量名 | 说明 | 有效值 | 默认值 | 配置位置 |
|--------|------|--------|--------|----------|
| `ENGRAM_RUFF_PHASE` | ruff lint-island 门禁 Phase | `0`, `1`, `2`, `3` | `0` | GitHub Repository Variables |
| `ENGRAM_RUFF_TOTAL_THRESHOLD` | 总违规数阈值 | 非负整数 | 无限制 | GitHub Repository Variables |
| `ENGRAM_NOQA_TOTAL_THRESHOLD` | noqa 总数阈值（预留） | 非负整数 | 无限制 | GitHub Repository Variables |
| `ENGRAM_RUFF_FAIL_ON_THRESHOLD` | 超阈值是否失败 | `true`, `false` | `false` | GitHub Repository Variables |
| `RUFF_NO_FIX` | 禁用自动修复 | `1`, 空 | 空（允许 --fix） | 环境变量 |
| `RUFF_OUTPUT_FORMAT` | 输出格式 | `text`, `json`, `github` | `text` | 环境变量 |

**ruff 指标阈值检查说明**：

| 变量 | 用途 | 建议值 |
|------|------|--------|
| `ENGRAM_RUFF_TOTAL_THRESHOLD` | 控制总违规数上限，超出时警告或失败 | 根据项目现状设置，如 `200` |
| `ENGRAM_NOQA_TOTAL_THRESHOLD` | 控制 noqa 注释总数上限（预留） | 根据项目现状设置，如 `50` |
| `ENGRAM_RUFF_FAIL_ON_THRESHOLD` | 默认 `false` 仅警告，设为 `true` 启用失败模式 | `false`（初期建议） |

**阈值检查行为**：

| `ENGRAM_RUFF_FAIL_ON_THRESHOLD` | 超阈值时的行为 | 适用场景 |
|----------------------------------|----------------|----------|
| `false`（默认） | 输出警告，CI 不失败 | 监控阶段，观察指标趋势 |
| `true` | CI 失败，阻断合并 | 严格控制阶段 |

**本地复现命令**：

```bash
# 仅警告模式（默认）
python -m scripts.ci.check_ruff_metrics_thresholds \
    --metrics-file artifacts/ruff_metrics.json \
    --verbose

# 指定阈值并启用失败模式
python -m scripts.ci.check_ruff_metrics_thresholds \
    --metrics-file artifacts/ruff_metrics.json \
    --total-threshold 200 \
    --fail-on-threshold true \
    --verbose

# JSON 输出
python -m scripts.ci.check_ruff_metrics_thresholds \
    --metrics-file artifacts/ruff_metrics.json \
    --json
```

**ENGRAM_RUFF_PHASE Phase 定义**：

| Phase | 行为 | 说明 |
|-------|------|------|
| `0` | 跳过 lint-island 检查 | **当前默认**，仅运行基础 ruff 规则 |
| `1` | 对 lint-island 路径执行 P1 规则检查 | 失败阻断，P1 规则: B, UP, SIM, PERF, PTH |
| `2` | 对全仓 (src/, tests/) 执行 P1 规则检查 | 扫描全仓而非仅 lint-island 路径 |
| `3` | 全仓 P1 + RUF100 冗余 noqa 检查 | 额外检测冗余 noqa 注释 |

**Phase 3 与 check_noqa_policy.py 分工**：

| 检查工具 | 检查内容 | 说明 |
|----------|----------|------|
| `check_noqa_policy.py` | noqa 语法规范 | 禁止裸 noqa，要求指定错误码 |
| `check_ruff_lint_island.py --phase 3` | noqa 语义有效性 | 使用 RUF100 检测冗余 noqa |

**Phase 解析优先级**（`check_ruff_lint_island.py`）：

| 优先级 | 来源 | 说明 |
|--------|------|------|
| 1（最高） | CLI 参数 `--phase` | 用于本地调试/测试 |
| 2 | 环境变量 `ENGRAM_RUFF_PHASE` | **CI 使用此方式注入** |
| 3 | `pyproject.toml [tool.engram.ruff].current_phase` | 项目默认配置 |
| 4（兜底） | 默认值 `0` | Phase 0 跳过 lint-island 检查 |

> **CI 配置**：CI 中通过 `${{ vars.ENGRAM_RUFF_PHASE || '0' }}` 注入 Phase 值。

**lint-island 路径配置**（`pyproject.toml [tool.engram.ruff]`）：

```toml
[tool.engram.ruff]
lint_island_paths = [
    "src/engram/gateway/di.py",
    "src/engram/gateway/container.py",
    "src/engram/gateway/services/",
    "src/engram/logbook/config.py",
    "src/engram/logbook/uri.py",
]
p1_rules = ["B", "UP", "SIM", "PERF", "PTH"]
current_phase = 0
```

**future-baseline 模式**（仅本地使用，不接入 CI）：

`future-baseline` 模式用于手动预演新规则的影响范围，**不作为 CI 门禁**。

| 命令 | 用途 |
|------|------|
| `make ruff-gate-future` | 检查当前代码对 future-baseline 规则的违规情况 |
| `make ruff-baseline-update RULES=B,UP` | 为指定规则生成/更新 baseline |

**典型使用场景**：

```bash
# 场景 1：评估新规则影响（Phase 推进前）
python -m scripts.ci.check_ruff_gate --gate future-baseline --verbose

# 场景 2：为新规则生成 baseline 快照
make ruff-baseline-update RULES=B,UP,SIM

# 场景 3：持续跟踪清理进度（手动执行，不阻断）
make ruff-gate-future

# 场景 4：验证规则清理完成后，准备推进 Phase
# 若 make ruff-gate-future 报告 0 个新增 violation，则可考虑：
# - 将规则加入 lint_island_paths（Phase 1）
# - 或全仓启用（Phase 2）
```

**配置文件**：

| 文件 | 说明 |
|------|------|
| `scripts/ci/ruff_baseline_future.json` | future-baseline 规则的已知违规快照 |

> **注意**：future-baseline 与 lint-island 机制是互补的。lint-island 用于保护高质量模块不引入新违规，future-baseline 用于评估新规则的全仓影响。详见 [ADR: Ruff Gate 门禁与 Rollout 分阶段策略](../architecture/adr_ruff_gate_and_rollout.md#64-future-baseline-模式定位)。

**ruff 规则配置**（`pyproject.toml [tool.ruff]`）：

```toml
[tool.ruff]
line-length = 100
target-version = "py310"

[tool.ruff.lint]
select = ["E", "F", "I", "W"]
ignore = ["E501"]
```

**per-file-ignores**（当前配置）：

| 路径 | 忽略规则 | 原因 |
|------|----------|------|
| `tests/**/*.py` | `E402` | 测试文件延迟导入 |
| `src/engram/logbook/db.py` | `E402` | 历史原因 |
| `src/engram/logbook/scm_auth.py` | `E402` | 历史原因 |

### 1.3 SQL 迁移门禁变量

| 变量名 | 说明 | 有效值 | 默认值 | 配置位置 |
|--------|------|--------|--------|----------|
| `ENGRAM_VERIFY_GATE` | 权限验证门禁级别 | `strict`, `warn`, `off` | `warn` | 环境变量 |
| `ENGRAM_VERIFY_GATE_POLICY` | 门禁触发策略 | `fail_only`, `fail_and_warn` | `fail_only` | 环境变量 |
| `ENGRAM_TESTING` | 测试模式（允许 schema-prefix） | `1`, 空 | 空 | 环境变量 |
| `ENGRAM_VERIFY_STRICT` | [废弃] 等价于 GATE=strict | - | - | - |

**策略说明**：

| 策略值 | 含义 | 推荐场景 |
|--------|------|----------|
| `fail_only` | 仅 FAIL 触发异常 | 生产环境（默认） |
| `fail_and_warn` | FAIL 或 WARN 都触发异常 | **CI 门禁（推荐）** |

### 1.4 Workflow Contract 门禁

Workflow Contract 校验 GitHub Actions workflow 文件是否符合 `workflow_contract.v1.json` 定义的合约。

> **场景化操作指南**：常见 workflow 变更场景（新增 Job、修改冻结名称、新增 Make Target 等）的完整操作流程和辅助工具使用方法，请参见 **[maintenance.md 第 9 章"常见场景最小演练"](../ci_nightly_workflow_refactor/maintenance.md#9-常见场景最小演练)**。

**本地复现命令**：

```bash
# 默认模式（仅 errors 失败）
make validate-workflows

# 严格模式（warnings 也失败，CI 使用此模式）
make validate-workflows-strict

# 合约与文档同步检查
make check-workflow-contract-docs-sync

# JSON 输出（适合脚本处理）
make -s validate-workflows-json

# JSON 输出写入文件（与 CI 产物路径一致）
mkdir -p artifacts
make -s validate-workflows-json > artifacts/workflow_contract_validation.json
```

**检查内容**：

| 检查项 | 说明 | 错误类型 |
|--------|------|----------|
| Job 存在性 | 合约定义的 job 必须存在 | `missing_job` |
| Step 存在性 | 合约定义的 step 必须存在 | `missing_step` |
| Frozen Step Name | 冻结的 step 名称不能修改 | `frozen_step_name_changed` |
| Frozen Job Name | 冻结的 job 名称不能修改 | `frozen_job_name_changed` |
| Make Target | 合约声明的 target 必须在 Makefile 中存在 | `missing_makefile_target` |
| CI Labels | PR labels 在合约和脚本中保持同步 | `label_missing_*` |

**Workflow 变更标准 SOP**：

> **重要**：任何 CI/Nightly workflow 变更都必须遵循以下 SOP，确保合约文件与实际 workflow 保持同步。

```bash
# ============================================
# 1. 变更前：生成 before 快照
# ============================================
python -m scripts.ci.generate_workflow_contract_snapshot \
  --workflow ci \
  --output /tmp/before.json

# Nightly workflow（如涉及）
python -m scripts.ci.generate_workflow_contract_snapshot \
  --workflow nightly \
  --output /tmp/before_nightly.json

# ============================================
# 2. 执行变更：修改 .github/workflows/*.yml
# ============================================
# ... 编辑 workflow 文件 ...

# ============================================
# 3. 变更后：生成 after 快照并 diff
# ============================================
python -m scripts.ci.generate_workflow_contract_snapshot \
  --workflow ci \
  --output /tmp/after.json

# 对比差异（推荐使用 jq 格式化）
diff <(jq -S . /tmp/before.json) <(jq -S . /tmp/after.json)

# ============================================
# 4. 同步更新合约文件（根据 diff 结果）
# ============================================
# - scripts/ci/workflow_contract.v1.json
# - docs/ci_nightly_workflow_refactor/contract.md
# 详见下方 "合约文件修改指南"

# ============================================
# 5. 本地验证（必须通过）
# ============================================
make validate-workflows-strict
make check-workflow-contract-docs-sync
```

**合约文件修改指南**：

| 变更类型 | `workflow_contract.v1.json` 修改位置 | `contract.md` 修改位置 |
|----------|--------------------------------------|------------------------|
| 新增 Job | `job_ids`, `job_names`, `required_jobs`, `frozen_job_names.allowlist` | 第 2 章 Job ID 对照表 |
| 修改 Job Name | `job_names`, `frozen_job_names.allowlist`, `required_jobs[].name` | 第 2 章 Job ID 对照表 |
| 新增 Step | `required_jobs[].required_steps`, `frozen_step_text.allowlist` | 第 3 章对应 job 的 step 表格 |
| 修改 Step Name | `required_steps`, `frozen_step_text.allowlist` | 第 3 章和第 4 章 |
| 新增 Make Target | `make.targets_required` | - |

**相关文件**：

| 文件 | 说明 |
|------|------|
| `scripts/ci/workflow_contract.v1.json` | 合约定义文件 |
| `scripts/ci/validate_workflows.py` | 校验脚本 |
| `scripts/ci/generate_workflow_contract_snapshot.py` | 快照生成脚本 |
| `.github/workflows/ci.yml` | CI workflow 文件 |
| `docs/ci_nightly_workflow_refactor/contract.md` | 合约设计文档 |
| `docs/ci_nightly_workflow_refactor/maintenance.md` | **维护指南（完整 SOP）** |

### 1.5 noqa 策略门禁

noqa 策略门禁检查 `src/` 和 `tests/` 目录下的 noqa 注释是否符合规范。

**ENGRAM_RUFF_PHASE 联动策略**：

CI 中的 noqa 检查与 `ENGRAM_RUFF_PHASE` 变量联动：

| Phase | CI 行为 | 说明 |
|-------|---------|------|
| `0` | 仅禁止裸 noqa（默认） | 必须带错误码，无需原因说明 |
| `1+` | lint-island 严格模式 | lint-island 路径额外要求原因说明 |

**本地复现命令**：

```bash
# Make 目标（默认模式：禁止裸 noqa）
make check-noqa-policy

# 直接调用 Python 脚本
python -m scripts.ci.check_noqa_policy --verbose

# lint-island 严格模式
python -m scripts.ci.check_noqa_policy --lint-island-strict --verbose

# 全部文件要求原因说明
python -m scripts.ci.check_noqa_policy --require-reason --verbose
```

**检查规则**：

| 规则 | 说明 | 示例 |
|------|------|------|
| 禁止裸 noqa | 必须指定错误码（全仓强制） | ❌ `# noqa` → ✅ `# noqa: E501` |
| lint-island 原因说明 | lint-island 路径要求原因（Phase >= 1） | `# noqa: E501  # URL 过长` |
| 全部原因说明 | 所有文件要求原因（`--require-reason`） | `# noqa: E501  # 原因说明` |

**检查模式说明**：

| 模式 | CLI 参数 | Make 目标 | CI 触发条件 | 说明 |
|------|----------|-----------|-------------|------|
| 默认 | （无） | `check-noqa-policy` | Phase 0 | 仅禁止裸 noqa，必须带错误码 |
| lint-island-strict | `--lint-island-strict` | - | Phase >= 1 | lint-island 路径额外要求原因说明 |
| require-reason | `--require-reason` | - | - | 所有文件都要求原因说明 |

**lint-island 路径配置**：

lint-island 路径从 `pyproject.toml` 的 `[tool.engram.ruff].lint_island_paths` 读取：

```toml
[tool.engram.ruff]
lint_island_paths = [
    "src/engram/gateway/di.py",
    "src/engram/gateway/container.py",
    "src/engram/gateway/services/",
    "src/engram/logbook/config.py",
    "src/engram/logbook/uri.py",
]
```

**相关文件**：

| 文件 | 说明 |
|------|------|
| `scripts/ci/check_noqa_policy.py` | 检查脚本 |
| `pyproject.toml [tool.ruff.lint]` | ruff 规则配置 |
| `pyproject.toml [tool.engram.ruff].lint_island_paths` | lint-island 路径列表 |

### 1.6 no_root_wrappers 门禁

no_root_wrappers 门禁禁止从项目根目录导入 wrapper 模块（如 `artifact_cli.py`），强制使用包内导入路径（如 `engram.logbook.cli.artifacts`）。

**本地复现命令**：

```bash
# Make 目标（组合检查：allowlist + usage）
make check-no-root-wrappers

# 单独检查 allowlist 有效性
make check-no-root-wrappers-allowlist

# 单独检查 usage
make check-no-root-wrappers-usage

# 直接调用 Python 脚本
python -m scripts.ci.check_no_root_wrappers_allowlist  # allowlist 有效性
python -m scripts.ci.check_no_root_wrappers_usage --verbose  # 使用检查
python -m scripts.ci.check_no_root_wrappers_usage --json    # JSON 输出
```

**检查规则**：

| 规则 | 说明 |
|------|------|
| Allowlist 有效性 | allowlist 中的条目必须有效（未过期、字段完整） |
| 禁止导入 | 非 allowlist/inline marker 允许的文件不能导入根目录 wrapper |
| 导入路径 | 应使用 `from engram.logbook.xxx import yyy` |
| 过期检查 | 过期的 allowlist 条目或 inline marker 会导致检查失败 |

**可配置文件**：

| 文件 | 说明 |
|------|------|
| `scripts/ci/no_root_wrappers_allowlist.json` | 允许导入根目录 wrapper 的文件列表 |
| `schemas/no_root_wrappers_allowlist_v1.schema.json` | allowlist 的 JSON Schema |
| `configs/import_migration_map.json` | 导入迁移映射（SSOT）|
| `scripts/ci/check_no_root_wrappers_allowlist.py` | allowlist 有效性检查脚本 |
| `scripts/ci/check_no_root_wrappers_usage.py` | 使用检查脚本 |

**例外机制**（两种方式）：

1. **Allowlist 引用**（推荐用于持久例外）:

```python
# 在代码中引用 allowlist 条目
from artifact_cli import main  # ROOT-WRAPPER-ALLOW: test-deprecated-forwarding
```

2. **Inline 声明**（适用于临时例外）:

```python
# 直接在代码中声明例外（必须包含过期日期和负责人）
from artifact_cli import main  # ROOT-WRAPPER-ALLOW: 迁移过渡期; expires=2026-06-30; owner=@platform-team
```

**Allowlist 条目格式（Schema v1）**：

```json
{
  "version": "1",
  "entries": [
    {
      "id": "test-deprecated-forwarding",
      "scope": "import",
      "module": "artifact_cli",
      "file_glob": "tests/acceptance/test_deprecated_*.py",
      "reason": "验收测试：验证根目录 wrapper 的转发功能",
      "owner": "@platform-team",
      "expires_on": "2026-12-31",
      "category": "testing"
    }
  ]
}
```

**常见失败原因与修复路径**：

| 失败原因 | 修复方法 |
|----------|----------|
| 直接导入根目录 wrapper | 改用包内导入：`from artifact_cli import x` → `from engram.logbook.cli.artifacts import x` |
| Allowlist 条目已过期 | 更新 `expires_on` 日期或移除已废弃的依赖 |
| Inline marker 已过期 | 更新过期日期或完成迁移移除 marker |
| Inline marker 格式错误 | 使用正确格式：`# ROOT-WRAPPER-ALLOW: <reason>; expires=YYYY-MM-DD; owner=<team>` |
| 无效的 allowlist 引用 | 确保代码中引用的 id 在 allowlist 文件中存在且未过期 |

**迁移路径参考**：

| 旧模块 | 新导入路径 / CLI 命令 |
|--------|----------------------|
| `artifact_cli` | `engram.logbook.cli.artifacts:main` 或 `engram-artifacts` |
| `db_migrate` | `engram.logbook.cli.db_migrate:main` 或 `engram-migrate` |
| `db_bootstrap` | `engram.logbook.cli.db_bootstrap:main` 或 `engram-bootstrap-roles` |
| `scm_sync_runner` | `engram.logbook.cli.scm_sync:runner_main` 或 `engram-scm-runner` |
| `logbook_cli` | `engram.logbook.cli.logbook:main` 或 `engram-logbook` |

> 完整迁移映射参见 `configs/import_migration_map.json`

**预警阈值与期限策略**：

**CI 门禁变量**：

| 变量名 | 说明 | 有效值 | 默认值 | CI 默认启用 | 配置位置 |
|--------|------|--------|--------|-------------|----------|
| `ENGRAM_ALLOWLIST_MAX_EXPIRY_DAYS` | 最大过期期限天数 | 正整数 | `180` | - | GitHub Repository Variables |
| `ENGRAM_ALLOWLIST_EXPIRING_SOON_DAYS` | 即将过期预警天数 | 正整数 | `14` | - | GitHub Repository Variables |
| `ENGRAM_ALLOWLIST_FAIL_ON_MAX_EXPIRY` | 超过最大期限时失败 | `true`, `false` | `true` | **是** | GitHub Repository Variables |

**脚本默认值**（`scripts/ci/check_no_root_wrappers_allowlist.py`）：

| 常量名 | 值 | 说明 |
|--------|-----|------|
| `DEFAULT_EXPIRING_SOON_DAYS` | `14` | 即将过期预警天数 |
| `DEFAULT_MAX_EXPIRY_DAYS` | `180` | 最大过期期限（约 6 个月）|

| 阈值 | 默认值 | CLI 参数 | CI 变量 | 说明 |
|------|--------|----------|---------|------|
| 即将过期（expiring-soon） | 14 天 | `--expiring-soon-days` | `ENGRAM_ALLOWLIST_EXPIRING_SOON_DAYS` | 即将过期的条目会显示警告，不阻断 CI |
| 最大期限（max-expiry） | 180 天 | `--max-expiry-days` | `ENGRAM_ALLOWLIST_MAX_EXPIRY_DAYS` | 超过 6 个月的过期日期需要审批 |
| 超期限失败 | **是** | `--fail-on-max-expiry` | `ENGRAM_ALLOWLIST_FAIL_ON_MAX_EXPIRY` | **CI 默认启用**，超过最大期限的条目会导致 CI 失败 |

**预警输出示例**：

```
[WARN] 即将过期的条目（14 天内）: 2 个
  id: legacy-db-import (3 天后过期, owner: @platform-team)
  id: test-fixture-import (10 天后过期, owner: @qa-team)

[WARN] 超过最大期限（180 天）的条目: 1 个
  id: long-term-exception (距离过期 365 天, owner: @infra-team)
```

**治理规则与审批要求**：

| 期限类型 | 审批要求 | 说明 |
|----------|----------|------|
| 短期例外（≤ 90 天） | 团队 Lead 审批 | 临时迁移过渡，无需额外理由 |
| 中期例外（91-180 天） | Tech Lead 审批 | 需要提供明确的迁移计划 |
| 长期例外（> 180 天） | 架构组审批 | 需要 ADR 记录原因和迁移策略 |

**紧急回滚方式**：

如果新增的过期检测导致 CI 失败，可通过以下方式临时绕过：

**方式 1：关闭超期限失败开关（推荐，快速回滚）**

```bash
# 在 GitHub Settings > Secrets and variables > Actions > Variables 中设置
ENGRAM_ALLOWLIST_FAIL_ON_MAX_EXPIRY: false
```

这将使超过最大期限（180 天）的条目仅警告，不阻断 CI。

**方式 2：临时提高最大期限天数**

```bash
# 在 GitHub Settings > Secrets and variables > Actions > Variables 中设置
ENGRAM_ALLOWLIST_MAX_EXPIRY_DAYS: 365  # 临时放宽到 1 年
```

**方式 3：更新过期日期**

- 在 `scripts/ci/no_root_wrappers_allowlist.json` 中更新 `expires_on` 字段
- 或修改代码中的 inline marker `expires=YYYY-MM-DD` 部分

**方式 4：临时禁用门禁**（仅紧急情况）

```bash
# CI 中可设置环境变量跳过特定检查
SKIP_NO_ROOT_WRAPPERS_CHECK=1 make ci
```

> **重要**：使用方式 1、2 或 4 进行紧急回滚后，**必须**在下一个工作日内：
> 1. 创建跟踪 Issue 记录临时变更
> 2. 处理超期限的 allowlist 条目（更新过期日期或完成迁移）
> 3. 恢复原始配置（关闭开关 → 开启，或恢复默认 max-expiry-days）

**相关文档**：

| 文档 | 说明 |
|------|------|
| [docs/architecture/no_root_wrappers_migration_map.md](../architecture/no_root_wrappers_migration_map.md) | 迁移映射详细说明、**Allowlist 用途与治理规则** |
| [docs/architecture/no_root_wrappers_exceptions.md](../architecture/no_root_wrappers_exceptions.md) | 例外机制设计、**Deprecated vs Preserved 治理差异** |
| [docs/architecture/cli_entrypoints.md](../architecture/cli_entrypoints.md) | CLI 入口点架构 |
| `configs/import_migration_map.json` | **SSOT**：模块分类定义（deprecated/preserved） |

> **关键概念**：并非所有根目录模块都需要 allowlist。`preserved` 模块（db, kv, artifacts）无需任何豁免声明即可导入。详见 [no_root_wrappers_exceptions.md](../architecture/no_root_wrappers_exceptions.md) 第 1.3 节。

### 1.7 Gateway DI 边界门禁

Gateway DI 边界门禁检查 `handlers/` 和 `services/` 目录下是否存在禁止的全局依赖调用。

**SSOT 文档**: [docs/architecture/gateway_module_boundaries.md](../architecture/gateway_module_boundaries.md)

**本地复现命令**：

```bash
# Make 目标（推荐，严格模式：--phase removal --disallow-allow-markers）
make check-gateway-di-boundaries

# compat 模式（仅警告 deps.db 违规，允许 allow-markers）
make check-gateway-di-boundaries-compat

# 直接调用 Python 脚本（严格模式）
python -m scripts.ci.check_gateway_di_boundaries --phase removal --disallow-allow-markers --verbose

# JSON 输出（适合脚本处理）
python -m scripts.ci.check_gateway_di_boundaries --phase removal --disallow-allow-markers --json

# compat 模式（迁移过渡期使用）
python -m scripts.ci.check_gateway_di_boundaries --phase compat --verbose
```

**禁止的调用模式**：

| 禁止调用 | 原因 | 替代方案 |
|----------|------|----------|
| `get_container()` | 绕过 DI 层 | `deps` 参数 |
| `get_config()` | 隐式依赖 | `deps.config` |
| `get_client()` | 隐式依赖 | `deps.openmemory_client` |
| `get_gateway_deps()` | 应由入口层调用 | `deps` 参数 |
| `logbook_adapter.get_adapter()` | 隐式依赖 | `deps.logbook_adapter` |
| `GatewayDeps.create()` | 不应直接创建 | `deps` 参数 |
| `deps is None` | deps 必须由调用方提供 | 移除兼容分支 |
| `generate_correlation_id()` | 应由入口层生成 | `correlation_id` 参数 |
| `deps.db` | 直接访问 db 绕过适配器封装 | `deps.logbook_adapter` |

**例外标记**：

v0.9 兼容期可使用 `# DI-BOUNDARY-ALLOW:` 标记临时豁免：

```python
# 行尾注释标记
if deps is None:  # DI-BOUNDARY-ALLOW: v0.9 兼容期 legacy fallback
    deps = get_gateway_deps()

# 上一行注释标记
# DI-BOUNDARY-ALLOW: v0.9 兼容期 legacy fallback，v1.0 移除
if deps is None:
    deps = get_gateway_deps()
```

**DEPS-DB-ALLOW 豁免标记**（针对 `deps.db` 禁止模式）：

当确实需要直接访问 `deps.db` 时，使用 `# DEPS-DB-ALLOW:` 标记进行豁免。

```python
# 格式：# DEPS-DB-ALLOW: <reason>; expires=YYYY-MM-DD; owner=<team>

# ✅ 正确示例
conn = deps.db  # DEPS-DB-ALLOW: adapter 内部实现需直接访问连接; expires=2026-06-30; owner=@platform-team

# 上一行注释标记
# DEPS-DB-ALLOW: 迁移脚本需要原始连接; expires=2026-03-31; owner=@data-team
conn = deps.db

# ❌ 错误示例（缺少必要字段）
conn = deps.db  # DEPS-DB-ALLOW: 临时使用
conn = deps.db  # DEPS-DB-ALLOW: expires=2026-06-30
```

**DEPS-DB-ALLOW 字段说明**：

| 字段 | 必填 | 格式 | 说明 |
|------|------|------|------|
| `reason` | 是 | 自由文本（分号前） | 说明为何需要直接访问 db |
| `expires` | 是 | `YYYY-MM-DD` | 豁免过期日期 |
| `owner` | 是 | `@team` 或 `@user` | 负责人/团队 |

**过期语义**：

- **过期判定**：`today > expires` 时视为过期（即 expires 当天仍有效）
- **时区处理**：使用 `datetime.now(timezone.utc).date()` 获取当前 UTC 日期
- **CI 行为**：过期的 DEPS-DB-ALLOW 标记会导致门禁失败
- **最大期限策略**：建议不超过 6 个月；超过 6 个月需 Tech Lead 审批
- **测试支持**：核心判定函数支持 `today` 参数注入，便于单元测试边界条件

**JSON 输出字段**（`--json` 模式）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `deps_db_allowed_hits` | array | 被有效 DEPS-DB-ALLOW 标记放行的命中记录 |
| `expired_deps_db_markers` | array | 过期的 DEPS-DB-ALLOW 标记列表 |
| `invalid_deps_db_markers` | array | 格式无效的 DEPS-DB-ALLOW 标记列表 |
| `expired_deps_db_marker_count` | int | 过期标记数量 |
| `invalid_deps_db_marker_count` | int | 无效标记数量 |

```bash
# 示例：查看 JSON 输出
python -m scripts.ci.check_gateway_di_boundaries --json

# 示例输出字段结构
{
  "deps_db_allowed_hits": [
    {"file": "...", "line_number": 42, "reason": "...", "expires": "2026-06-30", "owner": "@team"}
  ],
  "expired_deps_db_markers": [
    {"file": "...", "line_number": 42, "reason": "...", "expires": "2025-01-01", "owner": "@team", "fix_hint": "..."}
  ]
}
```

**替代方案**（优先考虑）：

| 场景 | 推荐方案 |
|------|----------|
| 查询操作 | `deps.logbook_adapter.query(...)` |
| 写入操作 | `deps.logbook_adapter.execute(...)` |
| 事务操作 | `deps.logbook_adapter.transaction(...)` |
| 特殊需求 | 定义新的 port 接口 |

**常见失败原因与修复路径**：

| 失败原因 | 修复方法 |
|----------|----------|
| handlers 中调用 `get_config()` | 改为 `deps.config` |
| handlers 中调用 `get_container()` | 通过 `deps` 参数获取依赖 |
| handlers 中检查 `deps is None` | 移除兼容分支或添加 allow-marker |
| handlers 中生成 `correlation_id` | 由入口层生成后透传 |
| handlers 中直接访问 `deps.db` | 改为 `deps.logbook_adapter` 或添加 DEPS-DB-ALLOW 标记 |
| DEPS-DB-ALLOW 标记已过期 | 更新 `expires` 日期或完成迁移移除标记 |
| DEPS-DB-ALLOW 格式错误 | 使用正确格式：`# DEPS-DB-ALLOW: <reason>; expires=YYYY-MM-DD; owner=<team>` |

**CI 门禁变量（Gateway DI 边界）**：

| 变量名 | 说明 | 有效值 | 默认值 | CI 默认启用 | 配置位置 |
|--------|------|--------|--------|-------------|----------|
| `ENGRAM_DEPS_DB_MAX_EXPIRY_DAYS` | deps.db allowlist 最大过期期限天数 | 正整数 | `180` | - | GitHub Repository Variables |
| `ENGRAM_DEPS_DB_EXPIRING_SOON_DAYS` | deps.db allowlist 即将过期预警天数 | 正整数 | `14` | - | GitHub Repository Variables |
| `ENGRAM_DEPS_DB_FAIL_ON_MAX_EXPIRY` | deps.db allowlist 超过最大期限时失败 | `true`, `false` | `true` | **是** | GitHub Repository Variables |
| `ENGRAM_DI_BOUNDARIES_FAIL_ON_MAX_EXPIRY` | inline DEPS-DB-ALLOW 超过最大期限时失败 | `true`, `false` | `true` | **是** | GitHub Repository Variables |

**脚本默认值**（`scripts/ci/check_gateway_di_boundaries.py`）：

| 常量名 | 值 | 说明 |
|--------|-----|------|
| `DEPS_DB_MAX_EXPIRY_DAYS` | `180` | 最大过期期限（约 6 个月）|

**预警阈值与期限策略**：

| 阈值 | 默认值 | CLI 参数 | CI 变量 | 说明 |
|------|--------|----------|---------|------|
| 即将过期（expiring-soon） | 14 天 | `--expiring-soon-days` | `ENGRAM_DEPS_DB_EXPIRING_SOON_DAYS` | 即将过期的标记会显示警告，不阻断 CI |
| 最大期限（max-expiry） | 180 天 | `--max-expiry-days` | `ENGRAM_DEPS_DB_MAX_EXPIRY_DAYS` | 超过 6 个月的过期日期需要审批 |
| 超期限失败（allowlist） | **是** | `--fail-on-max-expiry` | `ENGRAM_DEPS_DB_FAIL_ON_MAX_EXPIRY` | **CI 默认启用**，allowlist 超期限条目导致 CI 失败 |
| 超期限失败（inline） | **是** | `--fail-on-max-expiry` | `ENGRAM_DI_BOUNDARIES_FAIL_ON_MAX_EXPIRY` | **CI 默认启用**，inline 标记超期限导致 CI 失败 |

**预警输出示例**：

```
[WARN] 即将过期的 DEPS-DB-ALLOW 标记（14 天内）: 2 处
  src/engram/gateway/handlers/memory_store.py:42 （3 天后过期，owner: @platform-team）
  src/engram/gateway/services/audit_service.py:128 （10 天后过期，owner: @qa-team）

[WARN] 超过最大期限（180 天）的 DEPS-DB-ALLOW 标记: 1 处
  src/engram/gateway/handlers/evidence_upload.py:85 （距离过期 365 天，owner: @infra-team）
```

**治理规则与审批要求**（DEPS-DB-ALLOW 标记）：

| 期限类型 | 审批要求 | 说明 |
|----------|----------|------|
| 短期例外（≤ 90 天） | 团队 Lead 审批 | 临时迁移过渡，无需额外理由 |
| 中期例外（91-180 天） | Tech Lead 审批 | 需要提供明确的迁移计划 |
| 长期例外（> 180 天） | 架构组审批 | 需要 ADR 记录原因和迁移策略 |

**紧急回滚方式**：

如果 DEPS-DB-ALLOW 过期检测或超期限检测导致 CI 失败，可通过以下方式临时绕过：

**方式 1：关闭超期限失败开关（推荐，快速回滚）**

```bash
# 在 GitHub Settings > Secrets and variables > Actions > Variables 中设置

# 关闭 deps.db allowlist 超期限失败
ENGRAM_DEPS_DB_FAIL_ON_MAX_EXPIRY: false

# 关闭 inline DEPS-DB-ALLOW 超期限失败
ENGRAM_DI_BOUNDARIES_FAIL_ON_MAX_EXPIRY: false
```

这将使超过最大期限（180 天）的条目/标记仅警告，不阻断 CI。

**方式 2：临时提高最大期限天数**

```bash
# 在 GitHub Settings > Secrets and variables > Actions > Variables 中设置
ENGRAM_DEPS_DB_MAX_EXPIRY_DAYS: 365  # 临时放宽到 1 年
```

**方式 3：更新过期日期**

- 在 `scripts/ci/gateway_deps_db_allowlist.json` 中更新 `expires_on` 字段
- 或修改代码中的 inline marker `expires=YYYY-MM-DD` 部分

**方式 4：切换到 compat 模式**（仅紧急情况）

```bash
# 使用 compat 模式，deps.db 违规仅警告
python -m scripts.ci.check_gateway_di_boundaries --phase compat
```

> **重要**：使用方式 1、2 或 4 进行紧急回滚后，**必须**在下一个工作日内：
> 1. 创建跟踪 Issue 记录临时变更
> 2. 处理超期限的 allowlist 条目或 inline 标记（更新过期日期或完成迁移）
> 3. 恢复原始配置（开启 fail-on 开关、恢复默认 max-expiry-days、或恢复 removal 模式）

**相关文档**：

| 文档 | 说明 |
|------|------|
| [docs/architecture/gateway_module_boundaries.md](../architecture/gateway_module_boundaries.md) | SSOT 文档：模块边界与 import 规则 |
| [docs/architecture/adr_gateway_di_and_entry_boundary.md](../architecture/adr_gateway_di_and_entry_boundary.md) | ADR：设计决策与迁移计划 |
| [docs/gateway/upgrade_v1_0_remove_handler_di_compat.md](../gateway/upgrade_v1_0_remove_handler_di_compat.md) | v1.0 升级指南 |

### 1.8 Gateway 测试规范

Gateway 测试必须遵循统一的测试模板和隔离规范，确保测试可靠性和可维护性。

**SSOT 文档**: [docs/architecture/gateway_test_isolation_state_model.md](../architecture/gateway_test_isolation_state_model.md)

#### 三类测试模板

| 测试类型 | 推荐 Fixture | 核心原则 |
|----------|-------------|----------|
| **Handler 单元测试** | `gateway_deps` / `GatewayDeps.for_testing()` | 只用 DI + fake ports |
| **FastAPI 集成测试** | `gateway_test_app` / `gateway_test_app_factory` | 不直接依赖全局 app |
| **ImportError 测试** | `sys_modules_patcher` | 不直接操作 sys.modules |

#### 禁止项汇总

| 禁止模式 | 检测方式 | 替代方案 |
|----------|----------|----------|
| `get_container()` in handlers | `check_gateway_di_boundaries` 门禁 | `deps` 参数 |
| `sys.modules[...] = ...` | Code Review | `sys_modules_patcher` fixture |
| `no_singleton_reset` in unit tests | `test_opt_out_policy_contract.py` | 标记为 `@pytest.mark.integration` |
| 直接 `TestClient(app)` | Code Review | `gateway_test_app` fixture |
| 直接 `set_container()` | Code Review | `gateway_test_container` fixture |

#### 测试写法示例

```python
# ✅ Handler 单元测试（推荐）
@pytest.mark.asyncio
async def test_memory_store(gateway_deps, test_correlation_id):
    result = await memory_store_impl(
        payload_md="test",
        correlation_id=test_correlation_id,
        deps=gateway_deps,  # 显式传入 deps
    )
    assert result["ok"] is True

# ✅ FastAPI 集成测试（推荐）
def test_mcp_endpoint(gateway_test_app):
    response = gateway_test_app.post("/mcp", json={...})
    assert response.status_code == 200

# ✅ ImportError 测试（推荐）
def test_dependency_missing(sys_modules_patcher):
    patcher = sys_modules_patcher(["engram.gateway.evidence_store"])
    patcher.inject_failing_import("engram.gateway.evidence_store", "mocked")
    # ... 测试降级行为
```

---

## 2. 推荐变更窗口

| 变更类型 | 推荐窗口 | 原因 |
|----------|----------|------|
| mypy gate 级别提升 | 周一～周三 | 留出时间修复潜在阻塞 |
| mypy baseline 更新 | 随 PR 合并 | 需 code review |
| ruff 规则新增 | 周一～周三 | 可能触发大量修复 |
| SQL 迁移门禁变更 | 非发布周 | 避免影响发布流程 |
| Phase 阶段推进 | 迭代末尾/开头 | 便于追踪和观察 |

**变更前检查清单**：

```bash
# 1. 确认当前状态
# 推荐：使用 mypy_metrics.py 获取准确的错误数（排除 note 行）
python -m scripts.ci.mypy_metrics --output /dev/stdout | jq '.summary.total_errors'

# 备选：使用 wc -l 统计行数（包含 note 行，会略高于实际错误数）
wc -l scripts/ci/mypy_baseline.txt

make typecheck-gate                          # baseline 模式检查
make typecheck-strict-island                 # strict-island 检查

# 2. 验证 CI 通过
make ci                                      # 本地 CI 检查

# 3. 评估影响范围
git log --oneline -20 -- scripts/ci/mypy_baseline.txt  # 近期 baseline 变更
```

**baseline_count 统计口径说明**：

| 口径 | 命令 | 说明 | 使用场景 |
|------|------|------|----------|
| **mypy_metrics**（主口径） | `python -m scripts.ci.mypy_metrics --output - \| jq '.summary.total_errors'` | 实际错误数（排除 note 行） | **CI 主口径**、Phase 阈值判断、`--check-threshold` |
| **wc -l**（备选） | `wc -l < scripts/ci/mypy_baseline.txt` | 文件总行数（包含 note 行） | Fallback（当 mypy_metrics 不可用时） |

> **Phase 阈值判断字段**：CI 使用 `mypy_metrics.py` 的 **`summary.total_errors`** 字段进行 Phase 阈值判断。
>
> **选择 `total_errors` 而非 `total_lines` 的原因**：
> - `total_errors` 只计入 `error:` 行，更准确反映实际待修复的类型错误数量
> - `note:` 行是 mypy 输出的补充说明（如 import 错误的文档链接），修复对应 error 时会一并消失
> - 使用 `total_errors` 可避免 note 行数量波动导致的阈值判断不稳定
>
> **一致性保证**：`check_mypy_gate.py --check-threshold` 现已与 CI 使用相同口径（优先 mypy_metrics，fallback 到 wc -l）。两者差异约 10-20%（取决于 note 行比例）。

---

## 3. 回滚步骤

### 3.1 mypy 门禁回滚

**方式 1：使用回滚开关（推荐，无需代码变更）**

```bash
# 在 GitHub Settings > Secrets and variables > Actions > Variables 中设置
ENGRAM_MYPY_GATE_OVERRIDE: baseline
```

**方式 2：降低迁移阶段**

```bash
# 从 Phase 2 回滚到 Phase 1
ENGRAM_MYPY_MIGRATION_PHASE: 1
```

**方式 3：调整 Phase 1 阈值（针对 Phase 1 的软回滚）**

```bash
# 在 GitHub Settings > Secrets and variables > Actions > Variables 中设置
# 提高阈值可让更多 PR 保持 baseline 模式
ENGRAM_MYPY_STRICT_THRESHOLD: 100  # 例如：baseline_count <= 100 时才切换 strict
```

**方式 4：代码级回滚（最后手段）**

```bash
# 1. 修改 CI 配置
# .github/workflows/ci.yml
env:
  ENGRAM_MYPY_GATE: baseline

# 2. 如已归档基线，从归档恢复
git checkout HEAD~1 -- scripts/ci/mypy_baseline.txt

# 或从归档目录恢复
mv scripts/ci/archived/mypy_baseline.txt.archived scripts/ci/mypy_baseline.txt
```

**回滚触发条件**：

- 切换后 24 小时内出现 > 5 个被阻塞的紧急 PR
- 发现误报（false positive）影响正常开发
- CI 排队时间显著增加（> 30 分钟）
- Phase 1 阈值提升后仍有大量 PR 失败

### 3.2 ruff 门禁回滚

**方式 1：临时跳过检查**

```bash
# 在特定 PR 中添加
# .github/workflows/ci.yml (临时)
- name: Run ruff check (lint)
  run: ruff check src/ tests/ || true  # 添加 || true 临时跳过
```

**方式 2：规则回滚**

```bash
# pyproject.toml
[tool.ruff.lint]
ignore = ["E501", "NEW_RULE_CODE"]  # 添加需要回滚的规则
```

**方式 3：文件级豁免**

```bash
# pyproject.toml
[tool.ruff.lint.per-file-ignores]
"path/to/problematic/file.py" = ["RULE_CODE"]
```

**方式 4：lint-island Phase 回滚（针对 ENGRAM_RUFF_PHASE）**

```bash
# 回滚到 Phase 0（跳过 lint-island 检查）
# 在 GitHub Settings > Secrets and variables > Actions > Variables 中设置
ENGRAM_RUFF_PHASE: 0
```

如果在 CI 中遇到 lint-island 检查失败：

1. **临时回滚**：在 GitHub Repository Variables 中设置 `ENGRAM_RUFF_PHASE=0`
2. **路径豁免**：从 `pyproject.toml [tool.engram.ruff].lint_island_paths` 中移除问题路径
3. **规则豁免**：从 `pyproject.toml [tool.engram.ruff].p1_rules` 中移除问题规则

**CI 配置参考**（`.github/workflows/ci.yml`）：

```yaml
# lint-island 检查步骤
- name: Check ruff lint-island
  run: |
    python -m scripts.ci.check_ruff_lint_island \
      --phase "${{ vars.ENGRAM_RUFF_PHASE || '0' }}" \
      --verbose
```

> **注意**：CI 通过 `--phase` 参数注入 Phase 值，优先级高于 pyproject.toml 中的 `current_phase`。

**回滚触发条件**：

- lint-island 路径存在大量 P1 violation 无法快速修复
- P1 规则误报影响正常开发
- 紧急发布需要临时绕过检查

### 3.3 SQL 迁移门禁回滚

```bash
# 1. 降低门禁级别
export ENGRAM_VERIFY_GATE=warn

# 2. 或降低策略严格度
export ENGRAM_VERIFY_GATE_POLICY=fail_only

# 3. 紧急情况关闭验证
export ENGRAM_VERIFY_GATE=off
```

### 3.4 Workflow Contract 门禁回滚

**方式 1：降级为非严格模式（推荐）**

修改 `.github/workflows/ci.yml` 中的 `workflow-contract` job：

```yaml
# 从严格模式
make validate-workflows-strict

# 改为非严格模式（仅 errors 失败，warnings 忽略）
make validate-workflows
```

**方式 2：临时跳过检查**

```yaml
# .github/workflows/ci.yml (临时)
- name: Validate workflow contract
  run: |
    make validate-workflows-strict || true
```

**方式 3：更新合约文件**

如果需要永久性修改 step/job 名称，按以下步骤更新合约：

```bash
# 1. 查看当前失败详情
make validate-workflows

# 2. 编辑合约文件，更新对应的 job/step 名称
# scripts/ci/workflow_contract.v1.json

# 3. 验证更新后合约通过
make validate-workflows-strict
```

**合约文件修改指南**：

| 修改类型 | 修改位置 | 说明 |
|----------|----------|------|
| 新增 Job | `job_ids`, `job_names`, `required_jobs` | 同时更新三处 |
| 修改 Job Name | `job_names`, `frozen_job_names.allowlist` | 如果是冻结的 job |
| 新增 Step | `required_jobs[].required_steps` | 对应 job 的 steps 列表 |
| 修改 Step Name | `required_steps`, `frozen_step_text.allowlist` | 如果是冻结的 step |
| 新增 Make Target | `make.targets_required` | 确保 Makefile 中存在 |

**回滚触发条件**：

- 合约校验误报（合约定义与实际 workflow 不同步）
- CI 变更导致的批量失败
- 紧急修复需要临时绕过检查

### 3.5 noqa 策略门禁回滚

**方式 1：降低 Phase 级别（推荐，针对 lint-island-strict 模式）**

当 CI 使用 `--lint-island-strict` 模式（Phase >= 1）导致失败时，可以回滚到 Phase 0：

```bash
# 在 GitHub Settings > Secrets and variables > Actions > Variables 中设置
ENGRAM_RUFF_PHASE: 0
```

这将使 CI 从 lint-island-strict 模式回滚到默认模式（仅禁止裸 noqa）。

**方式 2：临时跳过检查（CI 中）**

```yaml
# .github/workflows/ci.yml (临时)
- name: Check noqa policy
  run: |
    python -m scripts.ci.check_noqa_policy --verbose || true
```

**方式 3：修改 Makefile ci 目标**

```makefile
# 临时从 ci 目标中移除 check-noqa-policy
ci: lint format-check typecheck-gate ... # 移除 check-noqa-policy
```

**方式 4：修复代码**

```bash
# 查找所有裸 noqa
grep -r "# noqa$" src/ tests/

# 添加错误码
# 将 `# noqa` 改为 `# noqa: E501` 等具体错误码

# 如果是 lint-island 路径缺少原因说明
# 将 `# noqa: E501` 改为 `# noqa: E501  # 原因说明`
```

**回滚触发条件**：

- 大量历史代码存在裸 noqa，无法快速修复
- lint-island 路径存在大量 noqa 缺少原因说明，无法快速补充
- 规则误报影响正常开发

**Phase 联动说明**：

noqa 检查与 `ENGRAM_RUFF_PHASE` 变量联动，与 ruff lint-island 检查使用相同的 Phase 变量：

| Phase | noqa 检查行为 | ruff lint-island 检查行为 |
|-------|---------------|---------------------------|
| `0` | 仅禁止裸 noqa | 跳过检查 |
| `1+` | lint-island 严格模式 | 执行 P1 规则检查 |

因此，回滚 `ENGRAM_RUFF_PHASE` 到 `0` 会同时影响 noqa 和 ruff lint-island 两个检查。

### 3.6 no_root_wrappers 门禁回滚

**方式 0：关闭超期限失败开关（快速回滚）**

当 allowlist 条目超过最大期限（180 天）导致 CI 失败时，可通过关闭失败开关快速回滚：

```bash
# 在 GitHub Settings > Secrets and variables > Actions > Variables 中设置
ENGRAM_ALLOWLIST_FAIL_ON_MAX_EXPIRY: false
```

这将使超过最大期限的条目仅警告，不阻断 CI。

> **注意**：此方式仅用于紧急修复，应在下一个工作日内创建跟踪 issue 并处理超期限条目。

**方式 1：添加到 Allowlist（推荐）**

```bash
# 编辑 allowlist 文件
# scripts/ci/no_root_wrappers_allowlist.json

{
  "allowlist": [
    {
      "file": "path/to/file.py",
      "reason": "说明为何需要豁免"
    }
  ]
}

# 验证 allowlist
python -m scripts.ci.check_no_root_wrappers_allowlist
```

**方式 2：临时跳过检查（CI 中）**

```yaml
# .github/workflows/ci.yml 中 ci 目标不包含此检查
# 如需跳过，修改 Makefile:
ci: lint format-check typecheck-gate ... # 移除 check-no-root-wrappers
```

**方式 3：修复导入路径**

```python
# 错误（从根目录导入）
from artifact_cli import main

# 正确（从包内导入）
from engram.logbook.cli.artifacts import main
```

**回滚触发条件**：

- 历史代码依赖根目录 wrapper 无法快速迁移
- 需要保持向后兼容性

### 3.7 Gateway DI 边界门禁回滚

**方式 0：关闭超期限失败开关（快速回滚）**

当 deps.db allowlist 或 inline DEPS-DB-ALLOW 标记超过最大期限（180 天）导致 CI 失败时，可通过关闭失败开关快速回滚：

```bash
# 在 GitHub Settings > Secrets and variables > Actions > Variables 中设置

# 关闭 deps.db allowlist 超期限失败
ENGRAM_DEPS_DB_FAIL_ON_MAX_EXPIRY: false

# 关闭 inline DEPS-DB-ALLOW 超期限失败
ENGRAM_DI_BOUNDARIES_FAIL_ON_MAX_EXPIRY: false
```

这将使超过最大期限的条目/标记仅警告，不阻断 CI。

> **注意**：此方式仅用于紧急修复，应在下一个工作日内创建跟踪 issue 并处理超期限的 allowlist 条目或 inline 标记。

**方式 1：添加 DI-BOUNDARY-ALLOW 标记（推荐）**

```python
# 在需要豁免的代码行添加标记
if deps is None:  # DI-BOUNDARY-ALLOW: v0.9 兼容期 legacy fallback
    deps = get_gateway_deps()

# 或在上一行添加注释
# DI-BOUNDARY-ALLOW: v0.9 兼容期 legacy fallback，v1.0 移除
if deps is None:
    deps = get_gateway_deps()
```

**方式 1b：添加 DEPS-DB-ALLOW 标记（针对 deps.db 访问）**

```python
# 格式必须包含 reason、expires、owner 三个字段
conn = deps.db  # DEPS-DB-ALLOW: adapter 内部实现; expires=2026-06-30; owner=@platform-team

# 或在上一行添加注释
# DEPS-DB-ALLOW: 迁移脚本需要原始连接; expires=2026-03-31; owner=@data-team
conn = deps.db
```

> **过期语义**：`today > expires` 时视为过期（expires 当天仍有效）。过期标记会导致 CI 失败。

**方式 2：临时跳过检查（CI 中）**

```yaml
# .github/workflows/ci.yml (临时)
- name: Check gateway DI boundaries
  run: |
    python -m scripts.ci.check_gateway_di_boundaries --verbose || true
```

**方式 3：修改 Makefile ci 目标**

```makefile
# 临时从 ci 目标中移除 check-gateway-di-boundaries
ci: lint format-check typecheck-gate ... # 移除 check-gateway-di-boundaries
```

**方式 4：修复代码（推荐长期方案）**

参考 [SSOT 文档](../architecture/gateway_module_boundaries.md) 第 7 节的修复指南：

```python
# 修复前：使用全局获取函数
from ..config import get_config
config = get_config()

# 修复后：通过 deps 参数获取
from ..di import GatewayDeps

async def handler_impl(..., deps: GatewayDeps):
    config = deps.config
```

**回滚触发条件**：

- 紧急发布需要临时绕过检查
- 大量历史代码存在违规，无法快速修复
- 迁移过程中需要兼容期

---

## 4. 阶段推进 Checklist

> **当前状态（2026-02-01）**：
> - ✅ mypy 错误数：**0**（baseline 文件已清空）
> - ✅ strict-island 模式：**通过**
> - 🎯 **可进入 Phase 3 归档准备**：当前已满足 Phase 2 → Phase 3 的所有验收条件
>
> 推荐下一步操作：
> ```bash
> # 1. 验证当前状态
> make typecheck-gate                    # 确认 0 错误
> make typecheck-strict-island           # 确认核心模块通过
>
> # 2. 执行 Phase 3 归档（可选，需团队讨论后执行）
> python -m scripts.ci.check_mypy_gate --archive-baseline
> # 然后更新 GitHub Repository Variable: ENGRAM_MYPY_MIGRATION_PHASE=3
> ```

### 4.0 推进通用前置条件

> **重要**：每次阶段推进前，必须确认以下通用条件全部满足。

**baseline_count 口径说明**：

| 口径 | 命令 | 含义 | 优先级 |
|------|------|------|--------|
| **mypy_metrics**（主口径） | `python -m scripts.ci.mypy_metrics --output - \| jq '.summary.total_errors'` | 实际错误数（排除 note 行） | **CI 主口径** |
| **wc -l**（备选） | `wc -l < scripts/ci/mypy_baseline.txt` | 文件总行数（含 note） | Fallback |

> CI 使用 `mypy_metrics.py` 口径进行 Phase 阈值判断。两者差异约 10-20%（note 行比例）。

**通用推进条件检查表**：

| 条件 | 检查命令 | 通过标准 | 说明 |
|------|----------|----------|------|
| **strict-island 必过** | `make typecheck-strict-island` | 退出码 0 | 核心模块零错误 |
| **近 30 天净增=0** | `git log --since="30 days ago" -p -- scripts/ci/mypy_baseline.txt \| grep "^+" \| grep -v "^+++" \| wc -l` | = 已修复行数 | 无净增新错误 |
| **CI 近期稳定** | 查看 GitHub Actions 近 10 次运行 | 无 mypy 相关失败 | 门禁稳定运行 |
| **无回滚标记** | 检查 `ENGRAM_MYPY_GATE_OVERRIDE` 变量 | 空或未设置 | 无紧急回滚状态 |

**紧急回滚步骤**（推进后发现问题时使用）：

```bash
# 方式 1: 使用回滚开关（推荐，无需代码变更，最快）
# 在 GitHub Settings > Secrets and variables > Actions > Variables 中设置
ENGRAM_MYPY_GATE_OVERRIDE: baseline

# 方式 2: 回退 Phase（降级）
# 在 GitHub Settings > Secrets and variables > Actions > Variables 中设置
ENGRAM_MYPY_MIGRATION_PHASE: <上一阶段值>  # 例如 1 → 0

# 方式 3: 调整阈值（Phase 1 专用软回滚）
# 提高阈值可让更多 PR 保持 baseline 模式
ENGRAM_MYPY_STRICT_THRESHOLD: 100  # 提高阈值
```

**回滚后必须执行**：

1. 创建 GitHub Issue 记录回滚原因和时间
2. 通知团队回滚状态
3. 制定修复计划和恢复时间表
4. 修复问题后，先在 staging 环境验证再恢复推进

---

### 4.1 Phase 0 → Phase 1

**触发条件**：基线错误数（`total_errors`）≤ 20 且无高风险模块错误

**建议阈值与观察周期**：

| 指标 | 建议值 | 说明 |
|------|--------|------|
| **total_errors 阈值** | ≤ 20 | 使用 `mypy_metrics.py` 的 `summary.total_errors` 口径 |
| **高风险模块错误** | = 0 | `di.py`、`container.py`、`migrate.py` 无错误 |
| **持续观察窗口** | 2-4 周 | 在 Phase 0 达到阈值后，观察稳定性再推进 |
| **推荐变更窗口** | 周一～周三上午 | 留出工作日时间观察和修复潜在问题 |

**Phase 1 行为说明**：

| 分支类型 | Gate 模式 | 条件 |
|----------|-----------|------|
| main/master | strict | 默认分支始终使用 strict |
| PR 分支 | strict | 当 `baseline_count <= ENGRAM_MYPY_STRICT_THRESHOLD` 时 |
| PR 分支 | baseline | 当 `baseline_count > ENGRAM_MYPY_STRICT_THRESHOLD` 时 |

**CI 行为**：

1. `Count mypy baseline errors` 步骤使用 `mypy_metrics.py` 计算当前 baseline 错误数
   - 主口径：`summary.total_errors`（排除 note 行）
   - Fallback：`wc -l`（当 metrics 文件不可用时）
2. `Resolve mypy gate` 步骤根据 `--baseline-count` 和 `--threshold` 决定 gate 级别
3. 当 `baseline_count <= threshold` 时，PR 可自动提升为 strict 模式

**验收条件**：

| 条件 | 检查方法 | 通过标准 |
|------|----------|----------|
| 基线错误数 | `python -m scripts.ci.mypy_metrics --output - \| jq '.summary.total_errors'` | ≤ 20 |
| 高风险模块 | `grep -E "di\.py\|container\.py\|migrate\.py" scripts/ci/mypy_baseline.txt` | 0 条 |
| Strict Island 通过 | `make typecheck-strict-island` | 退出码 0 |
| CI 稳定 | 最近 10 次 CI 运行 | 无 mypy 相关失败 |
| 观察期稳定 | 近 2 周 baseline 无净增 | 无回滚记录 |

**推进操作**：

```bash
# 1. 验证条件
# 推荐使用 mypy_metrics.py（CI 实际使用的口径）
python -m scripts.ci.mypy_metrics --output /dev/stdout | jq '.summary.total_errors'
# 或使用 wc -l 快速估算
wc -l < scripts/ci/mypy_baseline.txt
make typecheck-strict-island
grep -c "di\.py\|container\.py\|migrate\.py" scripts/ci/mypy_baseline.txt || echo "0"

# 2. 检查近 2 周稳定性
git log --since="14 days ago" --oneline -- scripts/ci/mypy_baseline.txt

# 3. 更新 GitHub Repository Variable
# Settings > Secrets and variables > Actions > Variables
# ENGRAM_MYPY_MIGRATION_PHASE = 1
# ENGRAM_MYPY_STRICT_THRESHOLD = 0  # 可选：设置阈值

# 4. 验证新配置生效
# 触发一次 CI 运行，确认 master=strict, PR=baseline (或 strict 若 baseline_count <= threshold)
```

**监控指标**（Phase 1 期间）：

| 指标 | 告警阈值 | 监控方式 |
|------|----------|----------|
| 被阻塞 PR 数 | > 3/天 | GitHub PR 列表 |
| CI 失败率 | > 10% | Actions 统计 |
| 基线变更频率 | > 2 次/周 | git log |

### 4.2 Phase 1 → Phase 2

**触发条件**：

| 条件 | 说明 | 必须满足 |
|------|------|----------|
| **Baseline 清零** | `total_errors = 0`（排除 note 行） | ✅ 强制 |
| **稳定期** | Phase 1 稳定运行 ≥ 2 周无回滚 | ✅ 强制 |
| **近 30 天净增** | Baseline 近 30 天净增 = 0 | ✅ 强制 |
| **无 Override** | `ENGRAM_MYPY_GATE_OVERRIDE` 为空 | ✅ 强制 |

**Baseline 清零定义**：

使用 `mypy_metrics.py` 的 `summary.total_errors` 口径（排除 note 行）。当 `total_errors = 0` 时视为 baseline 清零。

> **注意**：`wc -l` 口径包含 note 行，可能非零。但 CI 阈值判断使用 `total_errors`，因此以 `total_errors = 0` 为准。

**验收条件**：

| 条件 | 检查方法 | 通过标准 |
|------|----------|----------|
| **Baseline 清零** | `python -m scripts.ci.mypy_metrics --output - \| jq '.summary.total_errors'` | **= 0** |
| Phase 1 稳定期 | 查看变量设置时间、CI 历史 | ≥ 2 周无回滚 |
| 近 30 天净增 | `git log --since="30 days ago" -p -- scripts/ci/mypy_baseline.txt \| grep "^+" \| grep -v "^+++" \| wc -l` | = 已删除行数 |
| 无回滚记录 | 查看 `ENGRAM_MYPY_GATE_OVERRIDE` | 空或无设置 |
| Strict Island 通过 | `make typecheck-strict-island` | 退出码 0 |

**推进操作**：

```bash
# 1. 验证 Baseline 清零（使用 CI 主口径）
python -m scripts.ci.mypy_metrics --output /dev/stdout | jq '.summary.total_errors'  # 必须为 0

# 2. 验证 wc -l 口径（参考，可能包含 note 行）
wc -l < scripts/ci/mypy_baseline.txt  # 应为 0 或仅含 note 行

# 3. 检查近 30 天净增
git log --since="30 days ago" -p -- scripts/ci/mypy_baseline.txt | grep "^+" | grep -v "^+++" | wc -l
git log --since="30 days ago" -p -- scripts/ci/mypy_baseline.txt | grep "^-" | grep -v "^---" | wc -l
# 新增行数应 <= 删除行数（净增 <= 0）

# 4. 验证 strict-island 通过
make typecheck-strict-island

# 5. 更新 GitHub Repository Variable
# ENGRAM_MYPY_MIGRATION_PHASE = 2

# 6. 观察期（1-2 周）
# 监控 CI 失败率和开发者反馈
```

**监控指标**（Phase 2 期间）：

| 指标 | 告警阈值 | 监控方式 |
|------|----------|----------|
| CI mypy 失败率 | > 5% | Actions 统计 |
| type: ignore 新增 | > 5/周 | `git diff --stat` |
| 开发者升级请求 | > 2/周 | Issue/Slack |

### 4.3 Phase 2 → Phase 3

**触发条件**：Phase 2 稳定运行 ≥ 2 周且 baseline 仍为空

**验收条件**：

| 条件 | 检查方法 | 通过标准 |
|------|----------|----------|
| Phase 2 稳定期 | 查看变量设置时间、CI 历史 | ≥ 2 周无回滚 |
| **Baseline 仍为空** | `python -m scripts.ci.mypy_metrics --output - \| jq '.summary.total_errors'` | **= 0** |
| 无 Gate Override | 查看 `ENGRAM_MYPY_GATE_OVERRIDE` | 空或未设置 |
| Strict Island 通过 | `make typecheck-strict-island` | 退出码 0 |

**归档操作步骤**：

> **重要**：归档 baseline 文件**必须**使用 `python -m scripts.ci.check_mypy_gate --archive-baseline` 命令。
> 该命令会自动执行以下检查：
> 1. 验证 baseline 文件存在
> 2. 验证 baseline 错误数为 0
> 3. 创建归档目录 `scripts/ci/archived/`
> 4. 移动文件到 `scripts/ci/archived/mypy_baseline.txt.archived`

```bash
# ============================================
# Phase 3 归档操作（完整步骤）
# ============================================

# 1. 验证 baseline 清零（使用 CI 主口径）
python -m scripts.ci.mypy_metrics --output /dev/stdout | jq '.summary.total_errors'
# 必须输出 0，否则不允许归档

# 2. 验证 strict-island 通过
make typecheck-strict-island
# 必须退出码 0

# 3. 检查当前阈值状态（可选，查看详细报告）
python -m scripts.ci.check_mypy_gate --check-threshold

# 4. 执行归档（必须使用此命令）
python -m scripts.ci.check_mypy_gate --archive-baseline
# 成功输出示例:
# [OK] 基线文件已归档: scripts/ci/mypy_baseline.txt -> scripts/ci/archived/mypy_baseline.txt.archived

# 5. 提交归档变更
git add -A
git commit -m "chore: archive mypy baseline (phase 3)"
git push

# 6. 更新 GitHub Repository Variable
# 在 GitHub Settings > Secrets and variables > Actions > Variables 中设置:
# ENGRAM_MYPY_MIGRATION_PHASE = 3

# 7. 验证 Phase 3 配置生效
# 触发一次 CI 运行，确认所有分支使用 strict 模式
```

**归档后的文件位置**：

```
scripts/ci/archived/mypy_baseline.txt.archived
```

**归档失败处理**：

| 失败原因 | 错误信息 | 修复方法 |
|----------|----------|----------|
| Baseline 仍有错误 | `[ERROR] 基线文件仍有 N 个错误，不建议归档` | 继续修复错误直到 `total_errors = 0` |
| Baseline 文件不存在 | `[WARN] 基线文件不存在` | 检查路径是否正确，或已被意外删除 |
| 权限不足 | 文件移动失败 | 检查目录权限 |

**CI 简化（可选）**：

归档完成后，可选择简化 CI 配置：

1. 移除 `Count mypy baseline errors` 步骤
2. 移除 `Resolve mypy gate` 步骤中的 `--baseline-count` 参数
3. 直接在 mypy 检查步骤中使用 `--gate strict`

> **建议**：保留归档文件作为历史记录，便于追溯和回滚（如需要）。

---

## 5. 例外审批模板

### 5.1 Baseline 净增例外

> 当 PR 导致 `scripts/ci/mypy_baseline.txt` 净增加时使用

**PR 描述模板**：

```markdown
## Baseline 变更说明

### 变更原因（必选一项）
- [ ] 第三方库类型缺失（指明库名：_________）
- [ ] 类型系统局限（附 issue 链接：#___）
- [ ] 遗留代码暂无法修复（附修复计划）
- [ ] 其他：___________

### 新增错误明细

| 文件 | 错误类型 | 错误消息 | 原因 |
|------|----------|----------|------|
| src/engram/xxx.py | [import-untyped] | Module has no attribute | requests 库无 stubs |
| ... | ... | ... | ... |

### 影响评估
- 新增错误数：___
- 影响模块：___
- 是否涉及核心模块（di.py/container.py/migrate.py）：是/否

### 修复计划
- [ ] 下个迭代修复（Issue #___）
- [ ] 待上游修复（链接：___）
- [ ] 长期技术债务（标记 tech-debt 标签）

### 审批要求
- 1-5 条错误：1 位 Reviewer
- 6-10 条错误：2 位 Reviewer
- > 10 条错误：Tech Lead
```

**Reviewer 检查清单**：

```markdown
Baseline 变更审核：
- [ ] 变更原因是否合理（非"懒得修"）
- [ ] 是否已尝试 `# type: ignore[code]` 局部抑制
- [ ] 是否影响核心模块
- [ ] 错误数量增幅是否可接受（≤ 10）
- [ ] 是否有明确的修复计划（Issue 链接）
```

### 5.2 noqa 例外

> 当需要在代码中添加 `# noqa: XXXX` 时使用

**代码注释格式要求**：

```python
# 必须带有错误码和说明
result = some_function()  # noqa: E501 - URL 字符串超长无法拆分

# 禁止裸 noqa
result = some_function()  # noqa  # ❌ 禁止
```

**PR 描述模板**：

```markdown
## noqa 例外申请

### 新增 noqa 列表

| 文件:行号 | 规则码 | 原因 |
|-----------|--------|------|
| src/engram/xxx.py:42 | E501 | URL 字符串超长，无法合理拆分 |
| ... | ... | ... |

### 替代方案评估
- [ ] 已尝试代码重构但不可行（说明：___）
- [ ] 已尝试配置调整但不适用（说明：___）

### 审批要求
所有 noqa 例外需 1 位 Reviewer 批准
```

### 5.3 type: ignore 例外

> 当需要在代码中添加 `# type: ignore` 时使用

**代码注释格式要求**：

```python
# 必须带有错误码和说明
result = some_function()  # type: ignore[arg-type] - httpx 类型定义不完整

# 或引用 Issue/TODO
result = some_function()  # type: ignore[return-value] TODO: fix in #123

# Strict Island 路径下的额外要求
# - 必须带有 [error-code]
# - 必须带有原因说明（同行注释或 TODO/issue 引用）
```

**PR 描述模板**：

```markdown
## type: ignore 例外申请

### 新增 type: ignore 列表

| 文件:行号 | 错误码 | 原因 | 修复计划 |
|-----------|--------|------|----------|
| src/engram/xxx.py:42 | [arg-type] | httpx 类型定义不完整 | 待上游修复 |
| src/engram/yyy.py:100 | [return-value] | 动态返回类型 | Issue #456 |

### Strict Island 影响
- [ ] 涉及 Strict Island 路径（需额外审批）
- [ ] 不涉及 Strict Island 路径

### 审批要求
- 非 Strict Island：1 位 Reviewer
- Strict Island：2 位 Reviewer + 详细说明
```

### 5.4 no_root_wrappers 例外

> 当需要在代码中导入根目录 wrapper 模块时使用

**两种例外方式**：

1. **Allowlist 条目**（推荐用于持久例外，如测试文件）
2. **Inline Marker**（推荐用于临时例外，有明确过期日期）

**Allowlist 条目申请模板**：

```markdown
## no_root_wrappers Allowlist 申请

### 条目信息

| 字段 | 值 |
|------|-----|
| id | `my-exception-id` |
| scope | `import` |
| module | `artifact_cli` |
| file_glob | `tests/acceptance/test_*.py` |
| owner | `@my-team` |
| expires_on | `2026-12-31` |
| category | `testing` / `migration` / `other` |

### 期限说明（根据 expires_on 选择）
- [ ] 短期例外（≤ 90 天）：无需额外说明
- [ ] 中期例外（91-180 天）：需提供迁移计划
- [ ] **长期例外（> 180 天）**：必填以下字段 ⬇️

### 长期例外必填字段（> 180 天）
> 注意：过期日期超过 180 天的条目在 CI 中会触发 `ENGRAM_ALLOWLIST_FAIL_ON_MAX_EXPIRY` 检查

| 必填项 | 值 |
|--------|-----|
| ADR 文档链接 | `docs/architecture/adr_xxx.md` 或 GitHub URL |
| 跟踪 Issue | `#123` 或完整 GitHub Issue URL |
| 架构组审批人 | `@architect-team` 或具体 @username |
| 无法 6 个月内完成的原因 | （详细说明）|

### 申请原因（必选一项）
- [ ] 测试代码需要验证旧接口兼容性
- [ ] 迁移过渡期，计划于 YYYY-MM-DD 前完成迁移
- [ ] 外部依赖限制，无法直接修改
- [ ] 其他：___________

### 迁移计划
- [ ] 已创建跟踪 Issue（Issue #___）
- [ ] 预计完成日期：___
- [ ] 不需要迁移（说明原因：___）

### 审批要求
| 期限类型 | 审批要求 |
|----------|----------|
| ≤ 90 天 | 1 位团队 Lead |
| 91-180 天 | 1 位 Tech Lead + 迁移计划 |
| > 180 天 | 架构组审批 + ADR 文档 + Issue 链接 |
```

**Inline Marker 使用规范**：

```python
# 格式：# ROOT-WRAPPER-ALLOW: <reason>; expires=YYYY-MM-DD; owner=<team>

# ✅ 正确示例
from artifact_cli import main  # ROOT-WRAPPER-ALLOW: 迁移过渡期; expires=2026-06-30; owner=@platform-team

# ❌ 错误示例（缺少必要字段）
from artifact_cli import main  # ROOT-WRAPPER-ALLOW: 临时使用
from artifact_cli import main  # ROOT-WRAPPER-ALLOW: expires=2026-06-30
```

**Reviewer 检查清单**：

```markdown
no_root_wrappers 例外审核：
- [ ] 是否已尝试改用包内导入路径
- [ ] 过期时间是否合理（建议不超过 6 个月）
- [ ] 是否有明确的迁移计划
- [ ] owner 是否正确指向负责团队/个人
- [ ] 是否使用正确的 scope 和 category
```

### 5.5 Gateway deps.db 例外

> 当需要在 handlers/services 中直接访问 `deps.db` 时使用

**两种例外方式**：

1. **Allowlist 条目**（推荐用于持久例外，如 adapter 内部实现）
2. **Inline Marker**（推荐用于临时例外，有明确过期日期）

**Allowlist 条目申请模板**：

```markdown
## Gateway deps.db Allowlist 申请

### 条目信息

| 字段 | 值 |
|------|-----|
| id | `adapter-internal-xxx-v1` |
| file_glob | `src/engram/gateway/services/*.py` |
| owner | `@platform-team` |
| expires_on | `2026-12-31` |
| category | `adapter_internal` / `migration_script` / `legacy_compat` / `testing` / `other` |

### 期限说明（根据 expires_on 选择）
- [ ] 短期例外（≤ 90 天）：无需额外说明
- [ ] 中期例外（91-180 天）：需提供迁移计划
- [ ] **长期例外（> 180 天）**：必填以下字段 ⬇️

### 长期例外必填字段（> 180 天）
> 注意：过期日期超过 180 天的条目/标记在 CI 中会触发 `ENGRAM_DEPS_DB_FAIL_ON_MAX_EXPIRY` 或 `ENGRAM_DI_BOUNDARIES_FAIL_ON_MAX_EXPIRY` 检查

| 必填项 | 值 |
|--------|-----|
| ADR 文档链接 | `docs/architecture/adr_xxx.md` 或 GitHub URL |
| 跟踪 Issue | `#123` 或完整 GitHub Issue URL |
| 架构组审批人 | `@architect-team` 或具体 @username |
| 无法 6 个月内完成的原因 | （详细说明）|

### 申请原因（必选一项）
- [ ] Adapter 内部实现需要直接访问连接进行优化
- [ ] 迁移脚本需要原始数据库连接
- [ ] Legacy 兼容期，计划于 YYYY-MM-DD 前完成迁移
- [ ] 测试代码需要验证底层行为
- [ ] 其他：___________

### 替代方案评估
- [ ] 已评估使用 `deps.logbook_adapter` 但不适用（说明原因：___）
- [ ] 已评估定义新的 port 接口但成本过高（说明原因：___）

### 迁移计划
- [ ] 已创建跟踪 Issue（Issue #___）
- [ ] 预计完成日期：___
- [ ] 不需要迁移（说明原因：___）

### 审批要求
| 期限类型 | 审批要求 |
|----------|----------|
| ≤ 90 天 | 1 位团队 Lead |
| 91-180 天 | 1 位 Tech Lead + 迁移计划 |
| > 180 天 | 架构组审批 + ADR 文档 + Issue 链接 |
```

**Inline Marker 使用规范**：

```python
# 格式：# DEPS-DB-ALLOW: <reason>; expires=YYYY-MM-DD; owner=<team>

# ✅ 正确示例 - inline 声明
conn = deps.db  # DEPS-DB-ALLOW: adapter 内部优化需直接访问; expires=2026-06-30; owner=@platform-team

# ✅ 正确示例 - allowlist id 引用（推荐）
conn = deps.db  # DEPS-DB-ALLOW: adapter-internal-audit-v1

# 上一行注释标记
# DEPS-DB-ALLOW: 迁移脚本需要原始连接; expires=2026-03-31; owner=@data-team
conn = deps.db

# ❌ 错误示例（缺少必要字段）
conn = deps.db  # DEPS-DB-ALLOW: 临时使用
conn = deps.db  # DEPS-DB-ALLOW: expires=2026-06-30
conn = deps.db  # DEPS-DB-ALLOW: invalid-id-not-in-allowlist
```

**Allowlist 条目格式（Schema v1）**：

```json
{
  "version": "1",
  "entries": [
    {
      "id": "adapter-internal-audit-v1",
      "file_glob": "src/engram/gateway/services/audit_service.py",
      "reason": "Audit service 内部需要直接访问连接进行批量写入优化",
      "owner": "@platform-team",
      "expires_on": "2026-06-30",
      "category": "adapter_internal",
      "migration_target": "deps.logbook_adapter.batch_execute(...)"
    }
  ]
}
```

**常见失败原因与修复路径**：

| 失败原因 | 修复方法 |
|----------|----------|
| 直接访问 deps.db | 改用 `deps.logbook_adapter` 或添加 DEPS-DB-ALLOW 标记 |
| Allowlist 条目已过期 | 更新 `expires_on` 日期或移除已完成迁移的条目 |
| Inline marker 已过期 | 更新过期日期或完成迁移移除 marker |
| Inline marker 格式错误 | 使用正确格式：`# DEPS-DB-ALLOW: <reason>; expires=YYYY-MM-DD; owner=<team>` |
| 无效的 allowlist id 引用 | 确保代码中引用的 id 在 allowlist 文件中存在且未过期 |
| 文件路径不匹配 allowlist | 确保当前文件路径匹配 allowlist 条目的 file_glob |

**可配置文件**：

| 文件 | 说明 |
|------|------|
| `scripts/ci/gateway_deps_db_allowlist.json` | deps.db 直接访问例外允许列表 |
| `schemas/gateway_deps_db_allowlist_v1.schema.json` | allowlist 的 JSON Schema |

**Reviewer 检查清单**：

```markdown
Gateway deps.db 例外审核：
- [ ] 是否已尝试改用 deps.logbook_adapter
- [ ] 是否已评估定义新的 port 接口
- [ ] 过期时间是否合理（建议不超过 6 个月）
- [ ] 是否有明确的迁移计划
- [ ] owner 是否正确指向负责团队/个人
- [ ] file_glob 是否足够精确（避免过度宽泛）
- [ ] 是否使用正确的 category
```

---

## 6. 监控指标

### 6.0 指标→阈值→行动 速查表

> 本节汇总所有指标检查的阈值、启用策略、回滚方式和推荐行动。

#### mypy 指标阈值（`check_mypy_metrics_thresholds.py`）

| 指标 | 环境变量/参数 | 默认阈值 | 推荐阈值 | 超阈值行为 | 推荐行动 |
|------|---------------|----------|----------|------------|----------|
| **总错误数** | `ENGRAM_MYPY_TOTAL_ERROR_THRESHOLD` / `--total-error-threshold` | 50 | 根据项目现状设定 | 告警或失败 | 定期清理 baseline |
| **Gateway 错误数** | `ENGRAM_MYPY_GATEWAY_ERROR_THRESHOLD` / `--gateway-error-threshold` | 10 | ≤ 5 | 告警或失败 | 优先修复核心模块 |
| **Logbook 错误数** | `ENGRAM_MYPY_LOGBOOK_ERROR_THRESHOLD` / `--logbook-error-threshold` | 40 | ≤ 20 | 告警或失败 | 逐步清理 |

#### ruff 指标阈值（`check_ruff_metrics_thresholds.py`）

| 指标 | 环境变量/参数 | 默认阈值 | 推荐阈值 | 超阈值行为 | 推荐行动 |
|------|---------------|----------|----------|------------|----------|
| **总违规数** | `ENGRAM_RUFF_TOTAL_THRESHOLD` / `--total-threshold` | 无限制 | 根据项目现状设定 | 告警或失败 | 定期 `ruff check --fix` |
| **noqa 总数** | `ENGRAM_NOQA_TOTAL_THRESHOLD` / `--noqa-threshold` | 无限制 | ≤ 30 | 告警或失败 | 减少 noqa 注释 |

#### 指标阈值启用策略

| 变量名 | 默认值 | Phase 0（观测） | Phase 1（推进） | 回滚方式 |
|--------|--------|-----------------|-----------------|----------|
| `ENGRAM_MYPY_METRICS_FAIL_ON_THRESHOLD` | `false` | **禁用**（仅警告，脚本返回 0） | 设为 `true` 启用失败模式（超阈值脚本返回 1，CI 失败） | 设为 `false` 或删除变量 |
| `ENGRAM_RUFF_FAIL_ON_THRESHOLD` | `false` | **禁用**（仅警告） | 设为 `true` 启用失败模式 | 设为 `false` 或删除变量 |

**CI 行为说明**：

- 脚本 `check_mypy_metrics_thresholds.py` 根据 `--fail-on-threshold` 参数控制退出码：
  - `--fail-on-threshold=false`（默认）：超阈值仅输出 `[WARN]`，返回 0，CI 不阻断
  - `--fail-on-threshold=true`：超阈值输出 `[FAIL]`，返回 1，CI 失败
- CI 中直接使用脚本的退出码，无 `continue-on-error` 覆盖

#### 启用策略推荐流程

```
Phase 0: 观测期（2-4 周）
├── 设置 FAIL_ON_THRESHOLD=false（默认）
├── 收集指标数据，观察波动范围
├── 确定合理的阈值基准线
└── 阈值 = 当前值 + 10% 缓冲

Phase 1: 推进期
├── 设置阈值（GitHub Repository Variables）
├── 保持 FAIL_ON_THRESHOLD=false 再观测 1 周
├── 确认无误报后，设置 FAIL_ON_THRESHOLD=true
└── 监控 CI 失败率，必要时调整阈值

紧急回滚
├── 设置 FAIL_ON_THRESHOLD=false
└── 或提高阈值到安全值
```

#### 本地复现命令

```bash
# ============================================
# mypy 指标阈值检查
# ============================================
# 仅警告模式（默认，与 Phase 0 一致）
make check-mypy-metrics-thresholds

# 启用失败模式（与 Phase 1 启用后一致）
make check-mypy-metrics-thresholds-fail

# 自定义阈值
python -m scripts.ci.check_mypy_metrics_thresholds \
    --metrics-file artifacts/mypy_metrics.json \
    --total-error-threshold 30 \
    --fail-on-threshold true \
    --verbose

# ============================================
# ruff 指标阈值检查
# ============================================
# 仅警告模式（默认，与 Phase 0 一致）
make check-ruff-metrics-thresholds

# 启用失败模式（与 Phase 1 启用后一致）
make check-ruff-metrics-thresholds-fail

# 自定义阈值
python -m scripts.ci.check_ruff_metrics_thresholds \
    --metrics-file artifacts/ruff_metrics.json \
    --total-threshold 200 \
    --fail-on-threshold true \
    --verbose
```

#### 推荐观测周期与阈值设置

| 阶段 | 时长 | 配置 | 说明 |
|------|------|------|------|
| **Phase 0: 观测** | 2-4 周 | `FAIL_ON_THRESHOLD=false`，不设阈值 | 收集数据，了解指标波动范围 |
| **Phase 0.5: 试验** | 1 周 | 设置阈值，`FAIL_ON_THRESHOLD=false` | 观察告警频率，调整阈值 |
| **Phase 1: 启用** | 持续 | 设置阈值，`FAIL_ON_THRESHOLD=true` | 超阈值 CI 失败 |

**阈值设置建议**：

```bash
# 1. 获取当前指标
python -m scripts.ci.mypy_metrics --output artifacts/mypy_metrics.json
python -m scripts.ci.ruff_metrics --output artifacts/ruff_metrics.json

# 2. 查看当前值
jq '.summary.total_errors' artifacts/mypy_metrics.json    # mypy 总错误数
jq '.summary.total_violations' artifacts/ruff_metrics.json # ruff 总违规数

# 3. 设置阈值 = 当前值 + 10-20% 缓冲
# 例如：当前 mypy 错误 45，设置阈值 50
# 例如：当前 ruff 违规 180，设置阈值 200
```

---

### 6.1 mypy 健康指标

| 指标 | 计算方式 | 绿色 | 黄色 | 红色 |
|------|----------|------|------|------|
| Baseline 错误数 | `python -m scripts.ci.mypy_metrics --output - \| jq '.summary.total_errors'` | ≤ 50 | 51-100 | > 100 |
| Strict Island 覆盖率 | 已配置 strict 模块数 / 总模块数 | ≥ 80% | 50-79% | < 50% |
| 近 30 天新增错误 | git diff 统计 | 0 | 1-5 | > 5 |
| type: ignore 总数 | `grep -r "type: ignore" src/ \| wc -l` | ≤ 20 | 21-50 | > 50 |

### 6.2 ruff 健康指标

| 指标 | 计算方式 | 绿色 | 黄色 | 红色 |
|------|----------|------|------|------|
| noqa 总数 | `grep -r "# noqa" src/ \| wc -l` | ≤ 10 | 11-30 | > 30 |
| per-file-ignores 文件数 | pyproject.toml 配置 | ≤ 5 | 6-10 | > 10 |

### 6.3 CI 效率指标

| 指标 | 目标值 | 告警阈值 |
|------|--------|----------|
| mypy 检查耗时 | < 60s | > 120s |
| ruff 检查耗时 | < 10s | > 30s |
| 总 lint job 耗时 | < 3min | > 5min |

### 6.4 快速检查命令

```bash
# mypy 指标
# 推荐：使用 mypy_metrics.py 获取准确的错误数（CI 实际使用的口径）
python -m scripts.ci.mypy_metrics --output /dev/stdout | jq '.summary.total_errors'

# 备选：使用 wc -l 快速估算（包含 note 行，数值略高）
wc -l < scripts/ci/mypy_baseline.txt

# 完整 mypy 指标报告
python -m scripts.ci.mypy_metrics --output /dev/stdout --verbose

# type: ignore 总数
grep -r "type: ignore" src/ | wc -l

# ruff 指标
grep -r "# noqa" src/ | wc -l
grep -c "per-file-ignores" pyproject.toml

# CI 效率（本地估算）
time make typecheck-gate
time make lint
```

---

## 7. 相关文档

| 文档 | 说明 |
|------|------|
| [ADR: mypy 基线管理与 Gate 门禁策略](../architecture/adr_mypy_baseline_and_gating.md) | mypy 门禁设计决策 |
| [mypy 错误码修复 Playbook](./mypy_error_playbook.md) | 错误码清理路线、修复模板 |
| [mypy 基线管理](./mypy_baseline.md) | 操作指南与常见问题 |
| [环境变量参考](../reference/environment_variables.md) | 所有环境变量说明 |
| [ADR: 权限验证门控策略](../architecture/adr_verify_permissions_gate_policy.md) | SQL 迁移门禁设计 |
| [no_root_wrappers 迁移文档](../architecture/no_root_wrappers_migration_map.md) | 根目录 wrapper 迁移计划、Allowlist 用途 |
| [no_root_wrappers 例外规范](../architecture/no_root_wrappers_exceptions.md) | 例外机制、Deprecated vs Preserved 治理差异 |
| [Workflow Contract 维护指南](../ci_nightly_workflow_refactor/maintenance.md) | Workflow 合约维护 |
| [CI 测试隔离规范](./ci_test_isolation.md) | CI 脚本导入规范、禁止写法、ImportError 修复 |

---

## 8. 变更检查清单

> 当修改门禁相关的脚本、阈值、例外列表或 workflow 时，必须同步更新以下文件。

### 8.1 修改 CI 检查脚本时

| 变更场景 | 必须同步的文件 |
|----------|----------------|
| 修改 `scripts/ci/check_mypy_gate.py` | `Makefile` (typecheck-* 目标)、`.github/workflows/ci.yml` (lint job)、`docs/dev/mypy_baseline.md` |
| 修改 `scripts/ci/check_noqa_policy.py` | `Makefile` (check-noqa-policy)、`.github/workflows/ci.yml` (lint job)、本文档 |
| 修改 `scripts/ci/check_no_root_wrappers_usage.py` | `Makefile` (check-no-root-wrappers)、本文档 |
| 修改 `scripts/ci/validate_workflows.py` | `scripts/ci/workflow_contract.v1.json`、`docs/ci_nightly_workflow_refactor/maintenance.md` |
| 修改 `scripts/ci/check_ruff_gate.py` | `Makefile` (ruff-gate*)、本文档 |
| 新增 CI 检查脚本 | `Makefile`、`.github/workflows/ci.yml`、本文档、`AGENTS.md` |

### 8.2 修改阈值或配置时

| 变更场景 | 必须同步的文件 |
|----------|----------------|
| 修改 mypy 基线 `scripts/ci/mypy_baseline.txt` | PR 描述需包含 Baseline 变更说明（见 5.1 节） |
| 修改 mypy 阶段 `ENGRAM_MYPY_MIGRATION_PHASE` | GitHub Repository Variables、本文档 Section 4 |
| 修改 ruff 规则 `pyproject.toml [tool.ruff]` | 本文档 Section 1.2 |
| 修改 strict-island 路径 `pyproject.toml [tool.engram.mypy]` | `docs/dev/mypy_baseline.md` |
| 修改 mypy 指标阈值 `ENGRAM_MYPY_METRICS_FAIL_ON_THRESHOLD` | GitHub Repository Variables、本文档 |

### 8.3 修改例外/豁免列表时

| 变更场景 | 必须同步的文件 |
|----------|----------------|
| 修改 `scripts/ci/no_root_wrappers_allowlist.json` | 确保符合 `schemas/no_root_wrappers_allowlist_v1.schema.json`、本文档 Section 1.6 |
| 新增 `per-file-ignores` (ruff) | `pyproject.toml [tool.ruff.lint.per-file-ignores]`、本文档 Section 1.2 |
| 新增 type: ignore (strict-island) | PR 描述需包含例外申请（见 5.3 节） |

### 8.4 修改 GitHub Actions Workflow 时

> **重要**：任何 workflow 变更都必须遵循标准 SOP。详见 [1.4 Workflow Contract 门禁](#14-workflow-contract-门禁) 和 [维护指南](../ci_nightly_workflow_refactor/maintenance.md#0-快速变更流程ssot-first)。
>
> **场景化最小演练**：按场景列出先改哪里（SSOT/workflow/docs）、运行哪些命令、辅助工具使用、常见失败与修复路径，请参见 **[maintenance.md 第 9 章"常见场景最小演练"](../ci_nightly_workflow_refactor/maintenance.md#9-常见场景最小演练)**。

**标准变更流程**：

```bash
# 1. 变更前：生成 before 快照
python -m scripts.ci.generate_workflow_contract_snapshot --workflow ci --output /tmp/before.json

# 2. 执行变更：修改 .github/workflows/*.yml

# 3. 变更后：生成 after 快照并 diff
python -m scripts.ci.generate_workflow_contract_snapshot --workflow ci --output /tmp/after.json
diff <(jq -S . /tmp/before.json) <(jq -S . /tmp/after.json)

# 4. 同步更新合约文件（根据 diff）

# 5. 本地验证
make validate-workflows-strict
make check-workflow-contract-docs-sync
```

| 变更场景 | 必须同步的文件 |
|----------|----------------|
| 新增/删除 job | `.github/workflows/ci.yml`、`scripts/ci/workflow_contract.v1.json`、`docs/ci_nightly_workflow_refactor/contract.md`、`Makefile` |
| 修改 job name | `scripts/ci/workflow_contract.v1.json` (job_names, frozen_job_names)、`docs/ci_nightly_workflow_refactor/contract.md` |
| 修改 step name | `scripts/ci/workflow_contract.v1.json` (required_steps, frozen_step_text)、`docs/ci_nightly_workflow_refactor/contract.md` |
| 新增环境变量 | `docs/reference/environment_variables.md`、本文档 |
| 修改 artifact 名称 | `scripts/ci/workflow_contract.v1.json`、下游依赖脚本 |

### 8.5 修改 Makefile 时

| 变更场景 | 必须同步的文件 |
|----------|----------------|
| 新增 Make 目标 | `AGENTS.md` (门禁命令)、本文档 Section 0.1 |
| 修改 `ci` 目标依赖 | `.github/workflows/ci.yml`、本文档 Section 0.2 |
| 删除 Make 目标 | `scripts/ci/workflow_contract.v1.json` (make.targets_required) |
| 修改目标名称 | 全局搜索 `make <target>` 引用 |

### 8.6 快速检查命令

```bash
# 本地验证所有门禁
make ci

# 检查 workflow 合约是否同步
make validate-workflows-strict

# 检查 workflow 合约与文档同步
make check-workflow-contract-docs-sync

# 检查 CLI 入口点是否同步
make check-cli-entrypoints

# 检查 SQL 迁移计划是否正常
make check-migration-sanity

# 检查环境变量是否同步
make check-env-consistency
```

### 8.7 PR 提交前检查清单

- [ ] 运行 `make ci` 本地验证通过
- [ ] 若修改了 CI 脚本：更新 Makefile 对应目标
- [ ] 若修改了阈值/例外：PR 描述包含变更原因
- [ ] 若修改了 workflow：
  - [ ] 遵循标准 SOP（变更前 before 快照、变更后 after 快照并 diff）
  - [ ] 同步更新 `workflow_contract.v1.json` 和 `contract.md`
  - [ ] 运行 `make validate-workflows-strict`
  - [ ] 运行 `make check-workflow-contract-docs-sync`
- [ ] 若新增了门禁：更新本文档 Section 0.1 和 0.3
- [ ] 若修改了环境变量：更新 `docs/reference/environment_variables.md`

---

> 更新时间：2026-02-03（补充 MCP Doctor 检查矩阵与 JSON-RPC 检查项）
