# Phase 1 差距报告：CI/Nightly Workflow Contract 一致性审计

> [!WARNING]
> **历史文档 - 仅供参考**
>
> 本文档记录的差距已在后续合约版本中**完全解决**。当前合约版本为 **v2.12.0**（2026-02-02），已实施"方向 A：更新 Contract + Docs"策略，workflow 与合约保持一致。
>
> **当前推荐的实时检测手段**：
> - `make validate-workflows-strict` - 严格模式合约校验（CI 门禁）
> - `make workflow-contract-drift-report-all` - 生成实时漂移报告
> - CI Artifacts - 每次 CI 运行自动生成 `artifacts/workflow_contract_validation.json`
>
> **当前入口文档**：
> - [contract.md](../contract.md) - 人类可读合约文档（SSOT）
> - [maintenance.md](../maintenance.md) - 维护指南与变更 Checklist
> - [coupling_map.md](../coupling_map.md) - Workflow 耦合映射

---

> **生成日期**: 2026-02-02（更新）
> **审计范围**: `.github/workflows/ci.yml`、`.github/workflows/nightly.yml`、`scripts/ci/workflow_contract.v2.json`
> **快照来源**: `artifacts/workflow_snapshot.after.json`（由 `generate_workflow_contract_snapshot.py --include-step-details` 生成）
> **文档目的**: 记录当前 CI/Nightly workflow 与合约定义之间的**结构性差距**，为后续决策提供基准
> **文档状态**: **已归档** - 差距已在 v2.0.0+ 合约版本中解决

---

## 0. 执行摘要（Executive Summary）

| 类别 | 合约期望 | 实际状态 | 差距严重度 |
|------|----------|----------|------------|
| **CI job_ids** | 10 个 | 11 个（完全不匹配） | 🔴 **严重** |
| **CI job_names** | 10 个 | 11 个（完全不匹配） | 🔴 **严重** |
| **Nightly job_ids** | 1 个（`nightly-full`） | 2 个（`unified-stack-full`, `notify-results`） | 🟡 **中等** |
| **Nightly job_names** | 1 个 | 2 个（名称不匹配） | 🟡 **中等** |
| **Release.yml** | 定义了 3 个 jobs | **文件不存在** | 🔴 **严重** |
| **Required Steps** | 合约定义多个 | 实际不匹配 | 🔴 **严重** |
| **Frozen Labels** | `openmemory:freeze-override` | 未在 workflow 中引用 | 🟡 **中等** |
| **Artifact Paths** | 定义了必需路径 | 部分缺失 | 🟡 **中等** |

**结论**: 合约定义与实际 workflow 存在**根本性不一致**，当前合约描述的是一个"理想状态"的 workflow 结构，而非实际实现。

---

## 1. Job IDs 差距矩阵

### 1.1 CI Workflow Job IDs

| 合约期望 (`workflow_contract.v2.json`) | 实际存在 (`ci.yml`) | 状态 |
|----------------------------------------|---------------------|------|
| `detect-changes` | ❌ 不存在 | 🔴 GAP |
| `precheck-static` | ❌ 不存在 | 🔴 GAP |
| `workflow-contract-check` | ❌ 不存在（有 `workflow-contract`） | 🔴 GAP（名称不匹配） |
| `schema-validate` | ✅ 存在 | ✅ MATCH |
| `docs-check` | ❌ 不存在 | 🔴 GAP |
| `python-logbook-unit` | ❌ 不存在 | 🔴 GAP |
| `python-gateway-unit` | ❌ 不存在 | 🔴 GAP |
| `openmemory-governance-check` | ❌ 不存在 | 🔴 GAP |
| `unified-standard` | ❌ 不存在 | 🔴 GAP |
| `openmemory-sdk` | ❌ 不存在 | 🔴 GAP |
| — | `test` | 🟡 合约未定义 |
| — | `lint` | 🟡 合约未定义 |
| — | `env-var-consistency` | 🟡 合约未定义 |
| — | `logbook-consistency` | 🟡 合约未定义 |
| — | `migration-sanity` | 🟡 合约未定义 |
| — | `sql-safety` | 🟡 合约未定义 |
| — | `gateway-di-boundaries` | 🟡 合约未定义 |
| — | `scm-sync-consistency` | 🟡 合约未定义 |
| — | `gateway-error-reason-usage` | 🟡 合约未定义 |
| — | `workflow-contract` | 🟡 合约定义为 `workflow-contract-check` |

**匹配率**: 1/10 (10%)

### 1.2 Nightly Workflow Job IDs

| 合约期望 (`workflow_contract.v2.json`) | 实际存在 (`nightly.yml`) | 状态 |
|----------------------------------------|--------------------------|------|
| `nightly-full` | ❌ 不存在 | 🔴 GAP |
| — | `unified-stack-full` | 🟡 合约未定义 |
| — | `notify-results` | 🟡 合约未定义 |

**匹配率**: 0/1 (0%)

---

## 2. Job Names 差距矩阵

### 2.1 CI Workflow Job Names

| 合约期望 | 实际存在 | 状态 |
|----------|----------|------|
| `Detect Changes` | ❌ 不存在 | 🔴 GAP |
| `[Fast] Precheck & Static Build Verify` | ❌ 不存在 | 🔴 GAP |
| `[Fast] Workflow Contract Check` | ❌ 不存在（有 `Workflow Contract Validation`） | 🔴 GAP |
| `[Fast] Schema Validation` | ❌ 不存在（有 `Schema Validation`） | 🟡 部分匹配 |
| `[Fast] Docs Link Check` | ❌ 不存在 | 🔴 GAP |
| `[Fast] Logbook Unit Tests` | ❌ 不存在 | 🔴 GAP |
| `[Fast] Gateway Unit Tests` | ❌ 不存在 | 🔴 GAP |
| `[Fast] OpenMemory Governance Check` | ❌ 不存在 | 🔴 GAP |
| `[Standard] Unified Stack Integration Test (${{ matrix.profile }})` | ❌ 不存在 | 🔴 GAP |
| `[Fast] OpenMemory SDK Tests` | ❌ 不存在 | 🔴 GAP |

**实际存在但合约未定义的 Job Names**:
- `Test (Python ${{ matrix.python-version }})`
- `Lint`
- `Environment Variable Consistency`
- `Schema Validation`
- `Logbook Consistency Check`
- `Migration Sanity Check`
- `SQL Migration Safety Check`
- `Gateway DI Boundaries Check`
- `SCM Sync Consistency Check`
- `Gateway ErrorReason Usage Check`
- `Workflow Contract Validation`

### 2.2 Nightly Workflow Job Names

| 合约期望 | 实际存在 | 状态 |
|----------|----------|------|
| `Nightly Full Test Suite` | ❌ 不存在 | 🔴 GAP |
| — | `Unified Stack Full Verification` | 🟡 合约未定义 |
| — | `Notify Results` | 🟡 合约未定义 |

---

## 3. Required Steps 差距矩阵

### 3.1 CI Workflow Required Steps

合约定义了 `detect-changes` job 需要以下 steps，但该 job 在实际 workflow 中不存在：

| 合约期望的 Required Steps (`detect-changes`) | 状态 |
|---------------------------------------------|------|
| `Checkout repository` | 🔴 Job 不存在 |
| `Detect file changes` | 🔴 Job 不存在 |
| `Check PR labels` | 🔴 Job 不存在 |
| `Check if upstream_ref changed` | 🔴 Job 不存在 |

类似地，`precheck-static`、`workflow-contract-check` 等 jobs 的 required_steps 也无法校验。

### 3.2 Nightly Workflow Required Steps

合约定义了 `nightly-full` job 需要以下 steps，但该 job 在实际 workflow 中不存在：

| 合约期望的 Required Steps (`nightly-full`) | 状态 |
|--------------------------------------------|------|
| `Checkout repository` | 🔴 Job ID 不匹配 |
| `Set up Python` | 🔴 Job ID 不匹配 |
| `Install Python dependencies` | 🔴 Job ID 不匹配 |
| `Verify OpenMemory vendor structure` | 🔴 Job ID 不匹配 |
| `Verify OpenMemory.upstream.lock.json format` | 🔴 Job ID 不匹配 |
| `Deploy unified stack` | 🔴 Step 不存在（有 `Start unified stack with Docker Compose`） |
| `Run acceptance-unified-full` | 🔴 Step 不存在 |
| `Upload acceptance-unified-full results` | 🔴 Step 不存在 |

**实际 `unified-stack-full` job 的 steps**:
- Checkout repository ✅
- Set up Python ✅
- Install dependencies（名称不匹配）
- Detect environment capabilities（合约未定义）
- Validate gate contract (full profile)（合约未定义）
- Start unified stack with Docker Compose（名称不匹配）
- Wait for services to be healthy（合约未定义）
- Run Gateway integration tests (full profile)（合约未定义）
- Run unified stack verification (full)（合约未定义）
- Run make verify-unified (full mode)（合约未定义）
- Stop unified stack（合约未定义）
- Record acceptance run ✅（部分匹配）
- Render acceptance matrix（合约未定义）
- Upload test results（名称不匹配）

---

## 4. Artifact Archive 差距矩阵

### 4.1 CI Workflow Artifacts

合约定义 `ci.artifact_archive.required_artifact_paths`:
- `.artifacts/acceptance-runs/`
- `.artifacts/verify-results.json`
- `.artifacts/acceptance-unified-min/`

**实际 ci.yml 上传的 artifacts**:
- `test-results-*.xml` ❌ 不在合约路径
- `acceptance-results-*.xml` ❌ 不在合约路径
- `migration-output-*.log` ❌ 不在合约路径
- `schema-validation-results.json` ❌ 不在合约路径
- `artifacts/workflow_contract_validation.json` ❌ 不在合约路径

**匹配率**: 0/3 (0%)

### 4.2 Nightly Workflow Artifacts

合约定义 `nightly.artifact_archive.required_artifact_paths`:
- `.artifacts/acceptance-unified-full/`
- `.artifacts/acceptance-runs/`
- `.artifacts/verify-results.json`

**实际 nightly.yml 上传的 artifacts**:
- `test-unified-stack-results.xml` ❌ 不在合约路径
- `.artifacts/verify-results.json` ✅ 匹配
- `.artifacts/acceptance-runs/*` ✅ 匹配
- `.artifacts/acceptance-matrix.md` ❌ 不在合约路径
- `caps.json` ❌ 不在合约路径
- `validate.json` ❌ 不在合约路径
- `compose-logs.txt` ❌ 不在合约路径

**匹配率**: 2/3 (67%)

---

## 5. Release.yml 缺失分析

合约定义了 `release` workflow，但 `.github/workflows/release.yml` **文件不存在**。

| 合约定义 | 状态 |
|----------|------|
| `release.file: .github/workflows/release.yml` | 🔴 文件不存在 |
| `release.job_ids: [gate, build, summary]` | 🔴 无法校验 |
| `release.job_names` | 🔴 无法校验 |
| `release.required_jobs[].required_steps` | 🔴 无法校验 |
| `release.required_env_vars` | 🔴 无法校验 |

---

## 6. Labels 差距

合约定义 `ci.labels`:
- `openmemory:freeze-override`

**实际 ci.yml 状态**: 未见 PR labels 检查逻辑，合约期望的 `has_freeze_override_label` output 也不存在（因为 `detect-changes` job 不存在）。

---

## 7. Make Targets 差距

合约定义 `make.targets_required` 包含 47 个 targets，通过 `make -qp` 验证 Makefile 实际定义。

**抽样检查**:

| 合约期望 Target | 实际状态 |
|-----------------|----------|
| `ci-precheck` | 🔴 不存在 |
| `deploy` | 🔴 不存在 |
| `verify-build-static` | 🔴 不存在 |
| `verify-build` | 🔴 不存在 |
| `verify-unified` | ✅ 存在 |
| `verify-import-manifest` | 🔴 不存在 |
| `release-gate` | 🔴 不存在 |
| `test-logbook-unit` | 🔴 不存在（有 `test-logbook`） |
| `test-logbook-integration` | 🔴 不存在 |
| `test-gateway-integration` | 🔴 不存在（有 `test-gateway`） |
| `openmemory-vendor-check` | 🔴 不存在 |
| `openmemory-lock-format-check` | 🔴 不存在 |
| `openmemory-audit` | 🔴 不存在 |
| `openmemory-sync-check` | 🔴 不存在 |
| `openmemory-sync-verify` | 🔴 不存在 |
| `openmemory-release-preflight` | 🔴 不存在 |
| `openmemory-patches-strict-bundle` | 🔴 不存在 |
| `openmemory-test-multi-schema` | 🔴 不存在 |
| `validate-schemas` | 🔴 不存在（有 `check-schemas`） |
| `validate-workflows` | 🔴 不存在 |
| `validate-workflows-strict` | 🔴 不存在 |
| `docs-check` | 🔴 不存在 |
| `docs-lint` | 🔴 不存在 |
| `docs-check-refs` | 🔴 不存在 |
| `acceptance-unified-min` | 🔴 不存在 |
| `acceptance-unified-full` | 🔴 不存在 |
| `acceptance-logbook-only` | 🔴 不存在 |
| `verify-logbook-consistency` | 🔴 不存在（有 `check-logbook-consistency`） |
| `openmemory-base-snapshot` | 🔴 不存在 |
| `openmemory-patches-generate` | 🔴 不存在 |
| `openmemory-patches-backfill` | 🔴 不存在 |

**Makefile 实际存在的关键 targets**:
- `ci`, `lint`, `format`, `test`, `typecheck`
- `verify-unified`, `verify-permissions`, `verify-permissions-strict`
- `check-env-consistency`, `check-logbook-consistency`, `check-migration-sanity`
- `check-scm-sync-consistency`, `check-schemas`
- `migrate`, `migrate-ddl`, `migrate-plan`

**匹配率**: 约 1/47 (2%)

---

## 8. 建议改动方向

基于上述差距分析，有两个可选方向：

### 方向 A：更新 Contract + Docs（推荐）

**理由**:
1. 实际 workflow 是**正在工作的实现**，变更风险较大
2. 合约定义的是一个"理想状态"，可能是未来规划而非当前实现
3. 合约更新成本较低，不影响 CI 流程

**操作步骤**:
1. 根据快照更新 `workflow_contract.v2.json` 的 `ci.job_ids`、`ci.job_names`、`ci.required_jobs`
2. 根据快照更新 `nightly.job_ids`、`nightly.job_names`、`nightly.required_jobs`
3. 移除或标记 `release` 定义为 "planned"
4. 更新 `frozen_job_names.allowlist` 和 `frozen_step_text.allowlist`
5. 更新 artifact_archive 路径定义
6. 同步更新相关文档

### 方向 B：回滚/重构 Workflows

**理由**:
1. 合约定义的结构可能更清晰（如 `[Fast]`/`[Standard]` 前缀分类）
2. 合约定义的 `detect-changes` job 可以实现增量检测，减少不必要的 job 运行

**操作步骤**:
1. 保持合约不变
2. 重构 ci.yml 以匹配合约定义的 job 结构
3. 重构 nightly.yml 以匹配合约定义
4. 创建 release.yml
5. 风险：需要大量测试验证，可能引入回归

### 建议

**推荐方向 A**，原因：
1. 当前 CI 是可工作的，没有紧迫的重构需求
2. 合约应该反映实际状态，而非理想状态
3. 如果未来需要重构 workflow，应先更新合约作为 RFC，再实施变更

---

## 9. 附录：快照数据摘要

### 9.1 CI Workflow 快照

```json
{
  "job_count": 11,
  "job_ids": [
    "env-var-consistency",
    "gateway-di-boundaries",
    "gateway-error-reason-usage",
    "lint",
    "logbook-consistency",
    "migration-sanity",
    "schema-validate",
    "scm-sync-consistency",
    "sql-safety",
    "test",
    "workflow-contract"
  ]
}
```

### 9.2 Nightly Workflow 快照

```json
{
  "job_count": 2,
  "job_ids": [
    "notify-results",
    "unified-stack-full"
  ]
}
```

### 9.3 合约版本

```
version: 1.11.0
last_updated: 2026-01-30
```

---

## 10. 版本历史

| 日期 | 版本 | 变更说明 |
|------|------|----------|
| 2026-02-01 | v1.0 | 初版，基于静态分析 |
| 2026-02-02 | v2.0 | **重大更新**：基于 `generate_workflow_contract_snapshot.py` 快照进行系统性对比，发现合约与实际 workflow 存在根本性结构差距 |
