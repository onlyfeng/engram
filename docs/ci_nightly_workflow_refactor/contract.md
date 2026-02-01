# CI/Nightly Workflow Contract

> 本文档固化 workflow 的关键标识符、环境变量、标签语义等，作为"禁止回归"的基准。
> 任何修改需经过 review 并更新本文档。

> **Phase 1 范围说明**: 当前合约版本（详见第 14 章版本控制表或 `workflow_contract.v1.json` 的 `version` 字段）仅覆盖 CI 和 Nightly workflow。Release workflow (`release.yml`) 将在 Phase 2 引入。

---

## 0. 合约系统概览

本章提供合约系统的关键文件清单、检查脚本说明和 CI 阻断策略边界定义。

### 0.1 关键文件清单

| 文件路径 | 角色 | 说明 |
|----------|------|------|
| `scripts/ci/workflow_contract.v1.json` | **SSOT（合约定义）** | 所有合约字段的唯一真实来源，供校验脚本自动校验 |
| `scripts/ci/workflow_contract.v1.schema.json` | **Schema** | 定义 contract JSON 的字段约束和结构规范 |
| `.github/workflows/ci.yml` | **实现（CI workflow）** | CI workflow 的实际定义 |
| `.github/workflows/nightly.yml` | **实现（Nightly workflow）** | Nightly workflow 的实际定义 |
| `Makefile` | **实现（构建目标）** | 定义 workflow 依赖的构建目标（`validate-workflows-strict` 等） |
| `scripts/ci/validate_workflows.py` | **校验（合约校验器）** | 核心校验脚本，验证 workflow 与 contract 的一致性 |
| `scripts/ci/check_workflow_contract_docs_sync.py` | **校验（文档同步）** | 校验 contract JSON 与 contract.md 的同步状态 |
| `scripts/ci/check_workflow_contract_version_policy.py` | **校验（版本策略）** | 检查关键文件变更时版本是否已更新 |
| `scripts/ci/workflow_contract_drift_report.py` | **工具（漂移报告）** | 生成合约与 workflow 之间的差异报告 |
| `scripts/ci/generate_workflow_contract_snapshot.py` | **工具（快照生成）** | 生成 workflow 结构快照，用于变更前后对比 |

### 0.2 检查脚本详细说明

#### 0.2.1 validate_workflows.py（合约校验器）

| 属性 | 值 |
|------|-----|
| **Make Target** | `make validate-workflows-strict`（CI 使用）/ `make validate-workflows`（本地） |
| **直接调用** | `python scripts/ci/validate_workflows.py --strict` |
| **Exit Code** | `0` = 通过；`1` = 存在 ERROR（阻断 CI） |
| **Artifact 路径** | `artifacts/workflow_contract_validation.json` |
| **CI Artifact 名称** | `workflow-contract-validation` |

**功能说明**：
- 校验 workflow YAML 与 `workflow_contract.v1.json` 的一致性
- 检测 frozen job/step name 改名（ERROR）
- 检测非冻结项改名（`--strict` 模式下为 ERROR）
- 检测 extra jobs（workflow 中存在但 contract 未声明）
- 验证 `make.targets_required` 中的 target 在 Makefile 中存在
- 验证 `artifact_archive.required_artifact_paths` 被 upload 步骤覆盖

**参数说明**：

| 参数 | 说明 | CI 是否启用 |
|------|------|-------------|
| `--strict` | 将 WARNING 视为 ERROR；启用 `--require-job-coverage` | **是** |
| `--json` | JSON 格式输出（用于 artifact） | 仅报告生成 |
| `--require-job-coverage` | 要求所有 jobs 在 contract 中声明 | 由 `--strict` 隐式启用 |

#### 0.2.2 check_workflow_contract_docs_sync.py（文档同步检查）

| 属性 | 值 |
|------|-----|
| **Make Target** | `make check-workflow-contract-docs-sync` |
| **直接调用** | `python scripts/ci/check_workflow_contract_docs_sync.py` |
| **Exit Code** | `0` = 同步；`1` = 存在不同步项（阻断 CI） |
| **Artifact 路径** | `artifacts/workflow_contract_docs_sync.json` |
| **CI Artifact 名称** | `workflow-contract-docs-sync` |

**功能说明**：
- 校验 `workflow_contract.v1.json` 与 `contract.md` 的同步状态
- 检测 job_ids/job_names/labels 等字段在文档中的同步情况
- 检测版本号是否在文档第 14 章中记录

#### 0.2.3 check_workflow_contract_version_policy.py（版本策略检查）

| 属性 | 值 |
|------|-----|
| **Make Target** | `make check-workflow-contract-version-policy` |
| **直接调用** | `python scripts/ci/check_workflow_contract_version_policy.py --pr-mode` |
| **Exit Code** | `0` = 通过；`1` = 版本未更新（阻断 CI） |
| **Artifact 路径** | 无独立 artifact |

**功能说明**：
- 检查关键文件变更时是否正确更新 `version` 和 `last_updated` 字段
- 检查版本变更是否同步到 `contract.md` 版本控制表
- 关键文件范围详见第 11.2.1 节

#### 0.2.4 workflow_contract_drift_report.py（漂移报告）

| 属性 | 值 |
|------|-----|
| **Make Target** | `make workflow-contract-drift-report`（阻断）/ `make workflow-contract-drift-report-all`（非阻断） |
| **直接调用** | `python scripts/ci/workflow_contract_drift_report.py --output <path>` |
| **Exit Code** | `0` = 无漂移；`非0` = 检测到漂移（**CI 中使用 `\|\| true` 不阻断**） |
| **Artifact 路径** | `artifacts/workflow_contract_drift.json`、`artifacts/workflow_contract_drift.md` |
| **CI Artifact 名称** | `workflow-contract-drift` |

**功能说明**：
- 检测 workflow 文件与合约定义之间的差异
- 生成 JSON 和 Markdown 格式报告
- **定位为参考性报告，CI 中默认不阻断**

### 0.3 CI 阻断策略边界

#### 0.3.1 `workflow-contract` Job 检查步骤阻断表

CI workflow 的 `workflow-contract` job 包含以下检查步骤：

| Step Name | 脚本/命令 | 阻断策略 | 说明 |
|-----------|----------|----------|------|
| `Run CI script tests` | `pytest tests/ci/ -q` | **阻断** | 测试失败时阻断 |
| `Validate workflow contract` | `make validate-workflows-strict` | **阻断** | ERROR 时阻断（frozen 改名、extra jobs 等） |
| `Check workflow contract docs sync` | `make check-workflow-contract-docs-sync` | **阻断** | 文档不同步时阻断 |
| `Check workflow contract version policy` | `make check-workflow-contract-version-policy` | **阻断** | 版本未更新时阻断 |
| `Generate validation report (JSON)` | `...validate_workflows.py --json > ... \|\| true` | **非阻断** | 报告生成失败不阻断 |
| `Generate docs sync report (JSON)` | `...check_workflow_contract_docs_sync.py --json > ... \|\| true` | **非阻断** | 报告生成失败不阻断 |
| `Generate drift report (JSON)` | `...workflow_contract_drift_report.py ... \|\| true` | **非阻断** | drift 报告为参考性，不阻断 |
| `Generate drift report (Markdown)` | `...workflow_contract_drift_report.py --markdown ... \|\| true` | **非阻断** | drift 报告为参考性，不阻断 |

#### 0.3.2 阻断策略设计原则

| 检查类型 | 阻断策略 | 原因 |
|----------|----------|------|
| **合约校验**（validate_workflows.py） | 阻断 | 确保 workflow 与合约一致，防止回归 |
| **文档同步**（check_workflow_contract_docs_sync.py） | 阻断 | 确保文档作为"禁止回归"基准的有效性 |
| **版本策略**（check_workflow_contract_version_policy.py） | 阻断 | 确保版本变更可追溯 |
| **报告生成**（`--json` 输出） | 非阻断 | 报告生成失败不应阻断主流程 |
| **漂移报告**（workflow_contract_drift_report.py） | 非阻断 | 定位为参考性报告，帮助识别潜在问题 |

#### 0.3.3 Drift Report 非阻断策略详解

Drift Report 使用 `|| true` 的原因：

1. **定位差异**：Drift Report 是"参考性报告"而非"强制门禁"
2. **灵活性**：允许开发者在合理情况下存在临时性差异（如重构中间状态）
3. **避免误阻断**：某些 workflow 细节变更可能不需要立即同步到 contract

**如需启用阻断**：参见 [maintenance.md#4.3 阻断策略](maintenance.md#43-阻断策略)

---

## 1. detect-changes.outputs 全量键集合

### 1.1 文件变更检测键（dorny/paths-filter）

| Output Key | 触发条件（paths） |
|------------|------------------|
| `logbook_changed` | `sql/**`, `src/engram/logbook/**`, `logbook_postgres/**` |
| `gateway_changed` | `src/engram/gateway/**`, `docker/engram.Dockerfile`, `docker-compose.unified.yml` |
| `stack_changed` | `docker-compose.unified.yml`, `docker/**`, `sql/**`, `src/**`, `logbook_postgres/**`, `Makefile`, `scripts/**` |
| `openmemory_sdk_changed` | `docker/openmemory.Dockerfile`, `.env.example` |
| `openmemory_governance_changed` | `docker/openmemory.Dockerfile`, `.env.example` |
| `schemas_changed` | `schemas/**`, `scripts/validate_schemas.py` |
| `workflows_changed` | `.github/workflows/**`, `scripts/ci/workflow_contract*.json`, `scripts/ci/validate_workflows.py` |
| `contract_changed` | `.github/workflows/**`, `scripts/ci/workflow_contract*.json`, `scripts/ci/validate_workflows.py`, `Makefile`, `docs/ci_nightly_workflow_refactor/**` |
| `docs_changed` | `docs/**`, `README.md`, `scripts/docs/**`, `Makefile` |
| `scripts_changed` | `scripts/**`, `src/**/*.py`, `logbook_postgres/**/*.py` |

### 1.2 特殊检测键

| Output Key | 检测逻辑 |
|------------|----------|
| `upstream_ref_changed` | 比较 `HEAD^` 与 `HEAD` 的 `OpenMemory.upstream.lock.json` 中 `upstream_ref` 字段是否变化 |
| `has_freeze_override_label` | PR 是否有 `openmemory:freeze-override` label |

---

## 2. Job ID 与 Job Name 对照表

### 2.1 CI Workflow (`ci.yml`)

| Job ID | Job Name | 说明 |
|--------|----------|------|
| `test` | Test (Python ${{ matrix.python-version }}) | 单元测试和验收测试（矩阵：3.10, 3.11, 3.12） |
| `lint` | Lint | 代码风格检查（ruff check, ruff format, mypy） |
| `no-iteration-tracked` | No .iteration/ Tracked Files | 检查 .iteration/ 目录未被 git 跟踪 |
| `env-var-consistency` | Environment Variable Consistency | 环境变量一致性检查 |
| `schema-validate` | Schema Validation | JSON Schema 和 fixtures 校验 |
| `logbook-consistency` | Logbook Consistency Check | Logbook 配置一致性检查 |
| `migration-sanity` | Migration Sanity Check | SQL 迁移文件存在性和基本语法检查 |
| `sql-safety` | SQL Migration Safety Check | SQL 迁移安全性检查（高危语句检测） |
| `gateway-di-boundaries` | Gateway DI Boundaries Check | Gateway DI 边界检查 |
| `scm-sync-consistency` | SCM Sync Consistency Check | SCM Sync 一致性检查 |
| `gateway-error-reason-usage` | Gateway ErrorReason Usage Check | Gateway ErrorReason 使用规范检查 |
| `gateway-import-surface` | Gateway Import Surface Check | Gateway __init__.py 懒加载策略检查（禁止 eager-import） |
| `gateway-public-api-surface` | Gateway Public API Import Surface Check | Gateway Public API 导入表面检查（__all__ 与实际导出一致性、可选依赖隔离） |
| `gateway-correlation-id-single-source` | Gateway correlation_id Single Source Check | Gateway correlation_id 单一来源检查（禁止重复定义） |
| `mcp-error-contract` | MCP Error Contract Check | MCP JSON-RPC 错误码合约与文档同步检查 |
| `iteration-docs-check` | Iteration Docs Check | 迭代文档检查（.iteration/ 链接 + SUPERSEDED 一致性） |
| `iteration-tools-test` | Iteration Tools Test | 迭代工具脚本测试（无需数据库依赖） |
| `workflow-contract` | Workflow Contract Validation | Workflow 合约校验和文档同步检查 |

### 2.2 Nightly Workflow (`nightly.yml`)

| Job ID | Job Name | 说明 |
|--------|----------|------|
| `unified-stack-full` | Unified Stack Full Verification | 完整统一栈验证（Docker Compose + Gate Contract + 集成测试） |
| `iteration-audit` | Iteration Docs Audit | 迭代文档审计（轻量级 job） |
| `notify-results` | Notify Results | Nightly 汇总通知 |

### 2.3 Release Workflow (`release.yml`) - Phase 2 预留

> **注意**: Release workflow 将在 Phase 2 引入，当前合约版本不包含此部分。

---

## 3. PR Label 列表与语义

> **SSOT 说明**: `scripts/ci/workflow_contract.v1.json` 的 `ci.labels` 字段是 PR Labels 的唯一真实来源（SSOT）。本节内容必须与该 JSON 文件保持同步。
>
> **当前状态**: CI workflow 当前**不消费** PR labels。`gh_pr_labels_to_outputs.py` 脚本存在但未被 ci.yml 调用。Labels 仅用于合约定义和一致性校验。

| Label | 语义 | 使用场景 |
|-------|------|----------|
| `openmemory:freeze-override` | 绕过 OpenMemory 升级冻结 | 冻结期间的紧急修复（需配合 Override Reason） |

> **Labels 一致性校验**: `validate_workflows.py` 会自动校验 `ci.labels` 与 `gh_pr_labels_to_outputs.py` 中 `LABEL_*` 常量的一致性。若不一致会报 ERROR 并提示同步更新脚本/contract/docs。
>
> **历史变更**: v2.0.0 移除了 SeekDB 组件相关的 labels（`ci:dual-read`、`ci:seek-compat-strict`、`ci:seek-migrate-dry-run`）。

### 3.1 Override Reason 要求

当使用 `openmemory:freeze-override` label 时，PR body 中必须包含 **Override Reason**：

- **最小长度**: 20 字符
- **格式示例**:
  ```markdown
  ## OpenMemory Freeze Override
  **Override Reason**: Security fix for CVE-XXXX - 紧急安全修复，需要立即部署
  ```

---

## 4. Workflow 环境变量基线

### 4.1 CI Standard 层

```yaml
env:
  RUN_INTEGRATION_TESTS: "1"
  HTTP_ONLY_MODE: "1"
  SKIP_DEGRADATION_TEST: "1"
  # SKIP_JSONRPC 保持未设置 (default: false)
```

### 4.2 Nightly Full 层

```yaml
env:
  RUN_INTEGRATION_TESTS: "1"
  VERIFY_FULL: "1"
  HTTP_ONLY_MODE: "0"            # 显式设置为 0（允许 Docker 操作）
  SKIP_DEGRADATION_TEST: "0"     # 显式设置为 0（执行降级测试）
```

### 4.3 Release Gate - Phase 2 预留

> **注意**: Release Gate 环境变量将在 Phase 2 Release workflow 引入时定义。

### 4.4 Acceptance 目标环境变量绑定

Makefile acceptance targets 在调用子目标时会**显式设置**以下环境变量，确保语义绑定一致：

| Makefile 目标 | HTTP_ONLY_MODE | SKIP_DEGRADATION_TEST | VERIFY_FULL |
|---------------|----------------|----------------------|-------------|
| `acceptance-unified-min` | **1** | **1** | *(不设置)* |
| `acceptance-unified-full` | **0** | **0** | **1** |

> **注意**: 这些变量在调用 `verify-unified` 和 `test-gateway-integration[-full]` 时会作为前缀显式传递，
> 而非仅通过 `export` 设置。这确保子 make 进程能正确接收到这些值。

---

## 5. "禁止回归"的 Step 文本范围

> **设计原则**: 冻结范围应最小化，只冻结真正需要稳定的名称：
> - 被 GitHub Required Checks 引用的 Job Names
> - 被外部系统依赖的 Step Names（如 artifact 上传、日志搜索关键词）
> - 核心验证流程步骤
>
> **策略 A（最小冻结，v2.3.0+）**: 
> - `frozen_job_names.allowlist` 和 `frozen_step_text.allowlist` 仅包含核心项
> - `required_jobs[].required_steps` 和 `job_names[]` **不要求**全部在 allowlist 中
> - CI 门禁（`validate-workflows` / `validate-workflows-strict`）**不启用** `--require-frozen-consistency`
> - 改名非冻结项仅产生 WARNING，不阻止 CI
> - 如需强制全量冻结，可通过 `--require-frozen-consistency` 参数启用（策略 B）

### 5.1 Frozen Job Names

以下 Job Name 为"禁止回归"基准，在 `workflow_contract.v1.json` 的 `frozen_job_names.allowlist` 中定义。

**仅冻结被 GitHub Required Checks 引用的核心 Jobs（共 4 个）：**

| Job Name | 原因 |
|----------|------|
| `Test (Python ${{ matrix.python-version }})` | Required Check，单元测试门禁 |
| `Lint` | Required Check，代码质量门禁 |
| `Workflow Contract Validation` | Required Check，合约校验门禁 |
| `Unified Stack Full Verification` | Nightly 核心验证 |

**非冻结 Jobs（改名仅产生 WARNING）：**
- 辅助检查 jobs（如 `Schema Validation`、`Migration Sanity Check` 等）
- 通知类 jobs（如 `Notify Results`）

### 5.2 Frozen Step Names

以下 Step Name 为"禁止回归"基准，在 `workflow_contract.v1.json` 的 `frozen_step_text.allowlist` 中定义。

**冻结 step 验证规则：**
- `validate_workflows.py` 会检查所有 `required_steps` 中的 step name
- 如果冻结的 step name 被改名，会报告 **ERROR** (`frozen_step_name_changed`)
- 非冻结的 step name 改名只会报告 **WARNING** (`step_name_changed`)
- 错误信息会提示改名流程

**仅冻结核心步骤（共 13 个）：**

| Step Name | 冻结原因 |
|-----------|----------|
| `Checkout repository` | 基础步骤，所有 job 依赖 |
| `Set up Python` | 基础步骤，所有 job 依赖 |
| `Install dependencies` | 基础步骤，所有 job 依赖 |
| `Run unit and integration tests` | 核心测试步骤 |
| `Run acceptance tests` | 核心验收步骤 |
| `Upload test results` | Artifact 上传，被 CI 系统引用 |
| `Upload migration logs` | Artifact 上传，被 CI 系统引用 |
| `Upload validation results` | Artifact 上传，被 CI 系统引用 |
| `Upload validation report` | Artifact 上传，被 CI 系统引用 |
| `Upload drift report` | Artifact 上传，被 CI 系统引用 |
| `Validate workflow contract` | 核心合约校验步骤 |
| `Start unified stack with Docker Compose` | Nightly 核心部署步骤 |
| `Run unified stack verification (full)` | Nightly 核心验证步骤 |

**非冻结 Steps（改名仅产生 WARNING）：**
- 辅助检查步骤（如 `Check SQL migration files exist`、`Run ruff check (lint)` 等）
- 中间处理步骤（如 `Cache pip dependencies`、`Generate validation report (JSON)` 等）

### 5.3 Rename 流程

当确需改名冻结的 Job/Step Name 时，按以下步骤操作：

**Job Name 改名流程：**
1. 更新 `.github/workflows/*.yml` 中的 job name
2. 更新 `scripts/ci/workflow_contract.v1.json`:
   - `frozen_job_names.allowlist`: 添加新名称，移除旧名称
   - `job_names[]`: 同步更新对应位置
   - `required_jobs[].name`: 同步更新
3. 更新本文档第 2 章和第 5.1 节
4. 如有 GitHub Required Checks 引用，同步更新 repo settings
5. 运行 `make validate-workflows` 验证

**Step Name 改名流程：**
1. 更新 `.github/workflows/*.yml` 中的 step name
2. 更新 `scripts/ci/workflow_contract.v1.json`:
   - `frozen_step_text.allowlist`: 添加新名称，移除旧名称
   - `required_jobs[].required_steps`: 如有引用，同步更新
3. 更新本文档第 5.2 节
4. 运行 `make validate-workflows` 验证

### 5.4 Summary 标题/关键提示语

以下 Summary 标题为"禁止回归"基准：

| Summary 标题 | 出现场景 |
|--------------|----------|
| `## Nightly Build Summary` | nightly.yml Generate Summary step |
| `## :no_entry: OpenMemory Freeze Check Failed` | 冻结检查失败 |
| `## :no_entry: Override Reason 校验失败` | Override Reason 校验失败 |
| `## :warning: OpenMemory Freeze Override Active` | 使用 override 绕过冻结 |
| `## :no_entry: Lock 文件一致性检查失败` | Lock 一致性检查失败 |
| `### OpenMemory Sync Check` | Nightly sync 状态输出 |
| `### OpenMemory Upstream Drift` | 上游漂移检测结果 |

### 5.5 required_steps 覆盖原则

`required_jobs[].required_steps` 定义了每个 job 中必须存在的步骤。本节说明哪些步骤必须纳入 required_steps，哪些步骤可以不纳入。

#### 5.5.1 两档覆盖策略

| 策略 | 说明 | 适用场景 |
|------|------|----------|
| **核心子集（当前默认）** | 仅纳入关键验证步骤和产物生成步骤 | 日常开发，允许灵活调整非核心步骤 |
| **全量覆盖** | 纳入所有步骤，严格锁定 workflow 结构 | 发布冻结期、合规审计场景 |

**当前采用核心子集策略**：`required_steps` 只包含对 CI 结果和产物有直接影响的步骤，辅助步骤（如缓存、日志打印）不强制纳入。

#### 5.5.2 必须纳入 required_steps 的步骤类型

以下类型的步骤**必须**纳入 `required_jobs[].required_steps`：

| 步骤类型 | 示例 | 原因 |
|----------|------|------|
| **基础设置步骤** | `Checkout repository`, `Set up Python`, `Install dependencies` | 所有 job 的前置依赖 |
| **合约自身校验步骤** | `Validate workflow contract`, `Check workflow contract docs sync` | 确保合约一致性门禁生效 |
| **CI 脚本测试步骤** | `Run CI script tests` | 确保 CI 脚本本身的正确性 |
| **核心验证/测试步骤** | `Run unit and integration tests`, `Run acceptance tests` | 主要质量门禁 |
| **Artifact 上传步骤** | `Upload test results`, `Upload validation report` | 确保 CI 产物可追溯 |
| **报告生成步骤** | `Generate validation report (JSON)` | artifact 上传的前置依赖 |

#### 5.5.3 允许不纳入 required_steps 的步骤类型

以下类型的步骤**可以**不纳入 `required_steps`：

| 步骤类型 | 示例 | 原因 |
|----------|------|------|
| **缓存步骤** | `Cache pip dependencies` | 性能优化，不影响功能正确性 |
| **诊断/调试步骤** | `Print environment info`, `Debug output` | 仅用于故障排查 |
| **条件执行步骤** | `if: failure()` 类步骤 | 非主路径执行 |
| **通知步骤** | `Send Slack notification` | 不影响 CI 结果判定 |

#### 5.5.4 验证规则

`validate_workflows.py` 对 `required_steps` 的校验规则：

1. **存在性检查**：`required_steps` 中的每个步骤必须在对应 job 的 steps 中存在
2. **冻结检查**：如果步骤在 `frozen_step_text.allowlist` 中，改名会报 **ERROR**
3. **非冻结容忍**：如果步骤不在冻结列表中，改名只会报 **WARNING**

**注意**：`required_steps` 与 `frozen_step_text.allowlist` 是独立的概念：
- `required_steps`：定义 job 必须包含的步骤（存在性约束）
- `frozen_step_text.allowlist`：定义不可改名的步骤（名称约束）
- 一个步骤可以在 `required_steps` 中但不在冻结列表中（必须存在但可以改名）

#### 5.5.5 扩展 required_steps 的流程

当需要将新步骤纳入 `required_steps` 时：

1. 评估步骤是否属于 5.5.2 的必须类型
2. 更新 `scripts/ci/workflow_contract.v1.json` 的 `required_jobs[].required_steps`
3. 如果步骤名称需要冻结保护，同步添加到 `frozen_step_text.allowlist`
4. 运行 `make validate-workflows` 验证
5. 更新本文档（如影响第 5.2 节的冻结步骤列表）

#### 5.5.6 减少 required_steps 覆盖以避免非关键改动阻断

当非关键步骤的命名改动频繁阻断 CI 时，可以通过减少 `required_steps` 覆盖来缓解。以下是具体策略和示例：

**策略：精简 required_steps 到核心子集**

```json
// 修改前（过度覆盖）
"required_steps": [
  "Checkout repository",
  "Set up Python",
  "Install dependencies",
  "Cache pip dependencies",        // ← 辅助步骤，可移除
  "Print environment info",        // ← 诊断步骤，可移除
  "Run unit tests",
  "Upload test results"
]

// 修改后（核心子集）
"required_steps": [
  "Checkout repository",
  "Set up Python",
  "Install dependencies",
  "Run unit tests",
  "Upload test results"
]
```

**示例场景：`Cache pip dependencies` 步骤改名**

假设团队决定将 `Cache pip dependencies` 改为 `Setup pip cache`：

| 配置 | 行为 |
|------|------|
| step 在 `required_steps` 中 | 校验报 `missing_step` ERROR，CI 阻断 |
| step 不在 `required_steps` 中 | 无校验，CI 正常通过 |
| step 在 `required_steps` 且在 `frozen_step_text.allowlist` 中 | 校验报 `frozen_step_name_changed` ERROR，CI 阻断 |

**建议：**

1. **辅助步骤不纳入 required_steps**：缓存、诊断、通知类步骤不影响 CI 结果，改名不应阻断 CI
2. **非冻结步骤改名容忍**：`required_steps` 中但不在 `frozen_step_text.allowlist` 的步骤改名仅产生 WARNING
3. **定期审视覆盖范围**：随着 workflow 演进，定期检查 `required_steps` 是否过度覆盖

---

## 6. upstream_ref 变更要求

### 6.1 概述

当 `upstream_ref_changed == true`（即 `OpenMemory.upstream.lock.json` 中的 `upstream_ref` 字段发生变化）时，CI 执行更严格的验证流程。

### 6.2 CI 验证顺序（严格模式）

当检测到 `upstream_ref` 变更时，CI 按以下顺序执行：

1. **生成补丁包** (`Generate OpenMemory patch bundle (strict mode)`)
   - 调用 `make openmemory-patches-strict-bundle`
   - 输出到 `.artifacts/openmemory-patches/`
   - 生成的补丁包作为 CI artifact 上传
   
2. **执行同步检查** (`Run OpenMemory sync check`)
   - 环境变量 `OPENMEMORY_PATCH_FILES_REQUIRED=true`
   - 验证 patch 文件完整性
   
3. **执行同步验证** (`Run OpenMemory sync verify`)
   - 验证 patched_files checksums 匹配
   
4. **Lock 文件一致性检查** (`Run lock consistency check`)
   - 验证必需字段完整性（archive_info.sha256, upstream_commit_sha 等）

### 6.3 补丁产物要求

| 产物 | 说明 | 是否必需 |
|------|------|----------|
| `.artifacts/openmemory-patches/` | 严格模式补丁包目录 | 是（CI 生成） |
| `openmemory-patches-bundle-{run_number}` | CI artifact 名称 | 是（自动上传） |
| `openmemory_patches.json` | 补丁索引文件 | 是（已提交到仓库） |

### 6.4 流程说明

选择 **"CI 先生成补丁再校验"** 路线的原因：

1. **可重现性**: 补丁包在 CI 环境中生成，确保与校验环境一致
2. **调试便利**: 补丁包作为 artifact 保留 30 天，便于问题排查
3. **减少提交**: 不强制要求将补丁包提交到 git，降低仓库膨胀

> **注意**: 如果 `make openmemory-patches-strict-bundle` 执行失败，会输出警告但不阻止 CI（`bundle_generated=false`），后续的 sync check/verify 步骤仍会执行并可能因缺少补丁文件而失败。

---

## 7. Make Target 清单

### 7.1 CI 核心目标（workflow 必需）

以下 Make targets 在 `workflow_contract.v1.json` 的 `make.targets_required` 中定义，CI 校验会验证这些目标的存在：

| Make Target | 用途 | 对应 CI Job |
|-------------|------|-------------|
| `ci` | CI 聚合目标 | 本地开发验证 |
| `lint` | 代码风格检查（ruff check） | lint |
| `format` | 代码格式化 | - |
| `format-check` | 代码格式检查（不修改） | lint |
| `typecheck` | 类型检查（mypy） | lint |
| `check-env-consistency` | 环境变量一致性检查 | env-var-consistency |
| `check-logbook-consistency` | Logbook 配置一致性检查 | logbook-consistency |
| `check-schemas` | JSON Schema 和 fixtures 校验 | schema-validate |
| `check-migration-sanity` | SQL 迁移文件存在性检查 | migration-sanity |
| `check-scm-sync-consistency` | SCM Sync 一致性检查 | scm-sync-consistency |
| `check-gateway-error-reason-usage` | Gateway ErrorReason 使用规范检查 | gateway-error-reason-usage |
| `check-gateway-public-api-surface` | Gateway Public API 导入表面检查 | gateway-public-api-surface |
| `check-gateway-di-boundaries` | Gateway DI 边界检查（禁止 deps.db 直接使用） | gateway-di-boundaries |
| `check-gateway-import-surface` | Gateway Import Surface 检查（懒加载策略） | gateway-import-surface |
| `check-gateway-correlation-id-single-source` | Gateway correlation_id 单一来源检查 | gateway-correlation-id-single-source |
| `check-iteration-docs` | 迭代文档规范检查（.iteration/ 链接禁止 + SUPERSEDED 一致性） | iteration-docs-check |
| `iteration-audit` | 迭代文档审计（详细报告生成） | iteration-audit (nightly) |
| `validate-workflows-strict` | Workflow 合约校验（严格模式） | workflow-contract |
| `check-workflow-contract-docs-sync` | Workflow 合约与文档同步检查 | workflow-contract |
| `check-workflow-contract-version-policy` | Workflow 合约版本策略检查（关键文件变更时强制版本更新） | workflow-contract |

### 7.2 数据库相关目标

| Make Target | 用途 |
|-------------|------|
| `verify-unified` | 统一栈验证（支持 VERIFY_FULL=1 模式） |
| `verify-permissions` | 数据库权限验证 |
| `verify-permissions-strict` | 数据库权限验证（严格模式） |
| `migrate-ddl` | 执行 DDL 迁移 |
| `migrate-plan` | 查看迁移计划 |
| `apply-roles` | 应用 Logbook 角色和权限 |
| `apply-openmemory-grants` | 应用 OpenMemory 权限 |

### 7.3 Release 相关 Make Targets - Phase 2 预留

> **注意**: Release 专用 Make targets 将在 Phase 2 Release workflow 引入时定义。

---

## 8. Artifact Archive 合约

### 8.1 概述

`artifact_archive` 合约定义了 workflow 中必须上传的 artifact 路径，确保关键测试结果和验证报告被正确上传到 CI artifacts。

### 8.2 合约字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `required_artifact_paths` | `string[]` | 必需上传的 artifact 路径列表（支持通配符和目录路径） |
| `artifact_step_names` | `string[]` | 可选：限制校验范围到指定名称的步骤 |

### 8.3 CI Workflow Artifact 要求

```json
"artifact_archive": {
  "required_artifact_paths": [
    "test-results-*.xml",
    "acceptance-results-*.xml",
    "migration-output-*.log",
    "verify-output-*.log",
    "schema-validation-results.json",
    "artifacts/workflow_contract_validation.json",
    "artifacts/workflow_contract_docs_sync.json",
    "artifacts/workflow_contract_drift.json",
    "artifacts/workflow_contract_drift.md"
  ],
  "artifact_step_names": [
    "Upload test results",
    "Upload migration logs",
    "Upload validation results",
    "Upload validation report",
    "Upload docs sync report",
    "Upload drift report"
  ]
}
```

**Artifact 上传步骤说明：**

| Step Name | 上传内容 | Job |
|-----------|----------|-----|
| `Upload test results` | `test-results-*.xml`, `acceptance-results-*.xml` | test |
| `Upload migration logs` | `migration-output-*.log`, `verify-output-*.log` | test |
| `Upload validation results` | `schema-validation-results.json` | schema-validate |
| `Upload validation report` | `artifacts/workflow_contract_validation.json` | workflow-contract |
| `Upload docs sync report` | `artifacts/workflow_contract_docs_sync.json` | workflow-contract |

### 8.4 Nightly Workflow Artifact 要求

```json
"artifact_archive": {
  "required_artifact_paths": [],
  "artifact_step_names": [
    "Upload test results"
  ]
}
```

**Nightly Artifact 说明：**

Nightly workflow 的 `Upload test results` 步骤上传以下内容：
- `test-unified-stack-results.xml` - 集成测试结果
- `.artifacts/verify-results.json` - 验证结果
- `.artifacts/acceptance-runs/*` - Acceptance 运行记录
- `.artifacts/acceptance-matrix.md` / `.json` - Acceptance 矩阵
- `caps.json`, `validate.json` - Gate Contract 校验结果
- `compose-logs.txt` - Docker Compose 日志

### 8.5 验证规则

`validate_workflows.py` 执行以下检查：

1. **扫描 upload-artifact 步骤**：解析 workflow 中所有 `uses: actions/upload-artifact@v*` 步骤
2. **提取 path 配置**：支持单行和多行 `with.path` 配置
3. **覆盖检查**：验证 `required_artifact_paths` 中的每个路径都被某个 upload 步骤覆盖
4. **步骤过滤**：如果定义了 `artifact_step_names`，仅检查名称匹配的步骤

### 8.6 错误示例

```
[missing_artifact_path] ci:.github/workflows/ci.yml
  Key: test-results-*.xml
  Message: Required artifact path 'test-results-*.xml' is not uploaded in workflow. 
           Please ensure an upload-artifact step includes this path in its 'with.path' configuration.
  Location: artifact_archive.required_artifact_paths
```

### 8.7 Drift Report 漂移报告合约

#### 8.7.1 概述

Drift Report 用于检测 workflow 文件（`.github/workflows/*.yml`）与合约定义（`workflow_contract.v1.json`）之间的差异，生成漂移报告供开发者参考。

#### 8.7.2 运行时机

| 场景 | 触发方式 | Make Target | 阻断策略 |
|------|----------|-------------|----------|
| **本地开发** | 手动执行 | `make workflow-contract-drift-report` | 脚本失败时阻断（返回非零退出码） |
| **本地批量** | 手动执行 | `make workflow-contract-drift-report-all` | 不阻断（使用 `|| true`） |
| **PR/CI** | workflow-contract job | 直接调用脚本 + `|| true` | 默认不阻断 |
| **夜间** | N/A | N/A | 不执行 drift report |

#### 8.7.3 输出位置

| 输出格式 | 文件路径 | CI Artifact 名称 |
|----------|----------|------------------|
| JSON | `artifacts/workflow_contract_drift.json` | `workflow-contract-drift` |
| Markdown | `artifacts/workflow_contract_drift.md` | `workflow-contract-drift` |

#### 8.7.4 阻断策略

**默认行为**：CI 中的 drift report 步骤使用 `|| true`，即使检测到差异也不阻断 CI。报告仅供参考。

**启用阻断**：

1. **本地阻断**：使用 `make workflow-contract-drift-report`（不加 `|| true`）
2. **CI 阻断**：修改 `.github/workflows/ci.yml` 中 drift report 步骤，移除 `|| true`
3. **添加到 required_steps**：如需作为强制门禁，同步更新 `workflow_contract.v1.json` 的 `required_jobs[].required_steps`

> **设计原则**：Drift Report 定位为"参考性报告"，帮助识别潜在问题，而非强制门禁。详细说明参见 [maintenance.md#4-drift-report-漂移报告](maintenance.md#4-drift-report-漂移报告)。

---

## 9. Acceptance 验收测试合约

### 9.1 概述

本节定义 CI/Nightly 工作流中 acceptance 验收测试的执行合约，包括步骤序列、产物要求和环境语义。

### 9.2 CI 组合式覆盖合约

CI 工作流的 `unified-standard` job 采用 **组合式覆盖** 策略实现 `acceptance-unified-min` 语义：

| 合约项 | 要求 |
|--------|------|
| 执行方式 | workflow 分步执行（非直接调用 `make acceptance-unified-min`） |
| 环境变量绑定 | `HTTP_ONLY_MODE=1`, `SKIP_DEGRADATION_TEST=1`, `GATE_PROFILE=http_only` |
| 必需步骤 | deploy → verify-unified → test-logbook-unit → test-gateway-integration |
| 记录步骤 | 必须调用 `record_acceptance_run.py`，传入 `--metadata-kv workflow=ci` |
| 产物路径 | `.artifacts/acceptance-unified-min/`, `.artifacts/acceptance-runs/` |

**组合式覆盖的步骤映射**：

```yaml
# ci.yml unified-standard job 步骤与 acceptance-unified-min 的对应关系
steps:
  - name: Start unified stack           # → acceptance-unified-min Step 1: deploy
    run: make deploy
  - name: Verify unified stack          # → acceptance-unified-min Step 2: verify-unified
    run: make verify-unified VERIFY_JSON_OUT=.artifacts/verify-results.json
  - name: Run Gateway integration tests # → acceptance-unified-min Step 4: test-gateway-integration
    run: make test-gateway-integration
  # test-logbook-unit 在前置 job 中执行（条件触发）
  - name: Record acceptance run         # → acceptance-unified-min 记录步骤
    run: python3 scripts/acceptance/record_acceptance_run.py ...
```

### 9.3 Nightly 直接执行合约

Nightly 工作流直接调用 `make acceptance-unified-full`：

| 合约项 | 要求 |
|--------|------|
| 执行方式 | 直接调用 `make acceptance-unified-full` |
| 环境变量绑定 | `VERIFY_FULL=1`, `HTTP_ONLY_MODE=0`, `SKIP_DEGRADATION_TEST=0`, `GATE_PROFILE=full` |
| 跳过选项 | 支持 `SKIP_DEPLOY=1`（服务已运行时） |
| 产物路径 | `.artifacts/acceptance-unified-full/`, `.artifacts/acceptance-runs/` |

**nightly.yml 调用示例**：

```yaml
- name: Run acceptance-unified-full
  env:
    SKIP_DEPLOY: "1"  # 服务已在前面步骤启动
    SKIP_DEGRADATION_TEST: "0"
    HTTP_ONLY_MODE: "0"
    GATE_PROFILE: full
  run: make acceptance-unified-full
```

### 9.4 产物合约

| 产物 | CI 组合式覆盖 | Nightly 直接执行 | 必需 |
|------|--------------|------------------|------|
| `.artifacts/acceptance-*/summary.json` | ✅ 自动生成 | ✅ 自动生成 | 是 |
| `.artifacts/acceptance-*/steps.log` | ✅ 自动生成 | ✅ 自动生成 | 是 |
| `.artifacts/acceptance-*/verify-results.json` | ✅ 需显式传入 VERIFY_JSON_OUT | ✅ 自动生成 | 是 |
| `.artifacts/acceptance-runs/*.json` | ✅ record_acceptance_run.py | ✅ record_acceptance_run.py | 是 |
| `.artifacts/acceptance-matrix.md` | ✅ render_acceptance_matrix.py | ✅ render_acceptance_matrix.py | 否（趋势追踪用） |
| `.artifacts/acceptance-matrix.json` | ✅ render_acceptance_matrix.py | ✅ render_acceptance_matrix.py | 否（趋势追踪用） |

### 9.5 record_acceptance_run.py 合约

记录脚本的调用参数要求：

```bash
# 必需参数
--name <acceptance-target-name>       # acceptance-unified-min / acceptance-unified-full / acceptance-logbook-only
--artifacts-dir <path>                # 产物目录路径
--result <PASS|FAIL|PARTIAL>          # 验收结果

# 可选参数（CI 推荐使用）
--command <command-description>       # 执行的命令或步骤序列描述
--metadata-kv workflow=<ci|nightly>   # 工作流类型
--metadata-kv profile=<profile>       # 验收 profile
--metadata-kv run_number=<n>          # GitHub Actions run number
--metadata-kv run_id=<id>             # GitHub Actions run ID
--metadata-kv event_name=<event>      # GitHub event 类型
```

---

## 10. Extra Job Coverage 策略

### 10.1 概述

当 contract 定义了 `job_ids` 字段时，`validate_workflows.py` 会检测 workflow 中存在但未在 contract 中声明的 "extra jobs"。这有助于确保所有 jobs 都被合约管理，防止遗漏。

### 10.2 检测规则

| 场景 | 行为 |
|------|------|
| workflow 中的 job 在 contract.job_ids 中 | 正常校验 job name 等 |
| workflow 中的 job **不在** contract.job_ids 中 | 默认：WARNING；`--require-job-coverage`/`--strict`：ERROR |
| contract 未定义 job_ids 字段 | 跳过 extra job 检测 |

### 10.2.1 Contract 内部一致性检查

`validate_workflows.py` 在校验 workflow 之前，会先检查 contract 文件自身的内部一致性。以下错误会在 `--strict` 模式下阻断 CI：

| 错误类型 | 说明 | 修复方法 |
|----------|------|----------|
| `contract_job_ids_names_length_mismatch` | `<workflow>.job_ids` 与 `<workflow>.job_names` 长度不一致 | 确保两个数组长度相同，位置一一对应 |
| `contract_job_ids_duplicate` | `<workflow>.job_ids` 中存在重复的 job_id | 移除重复的 job_id |
| `contract_required_job_id_duplicate` | `<workflow>.required_jobs` 中存在重复的 id | 移除重复的 required_job 条目 |
| `contract_required_job_not_in_job_ids` | `<workflow>.required_jobs[*].id` 不在 `<workflow>.job_ids` 中 | 将 id 添加到 job_ids，或从 required_jobs 移除该条目 |

**设计意图**：

- `job_ids` 和 `job_names` 是位置对应的数组，长度必须一致
- `required_jobs` 定义的 job 必须在 `job_ids` 中声明，确保合约完整性
- 重复检测避免配置错误导致的不确定行为

### 10.3 使用方式

```bash
# 默认模式：extra jobs 产生 WARNING（不阻止 CI）
python scripts/ci/validate_workflows.py

# 严格模式：extra jobs 产生 ERROR（阻止 CI）
python scripts/ci/validate_workflows.py --require-job-coverage

# --strict 模式：将 WARNING 视为 ERROR，同时启用 extra job 检测为 ERROR
python scripts/ci/validate_workflows.py --strict

# CI 实际使用（通过 Makefile）
make validate-workflows-strict  # 等价于 --strict
```

**CI 当前配置**：CI workflow (`ci.yml`) 使用 `make validate-workflows-strict`，即 `--strict` 模式。

### 10.4 修复 extra job 警告/错误

如需将 extra job 纳入合约管理：

1. 更新 `scripts/ci/workflow_contract.v1.json`:
   - `<workflow>.job_ids`: 添加 job ID
   - `<workflow>.job_names`: 添加对应的 job name
   - 如果 job name 需要冻结，添加到 `frozen_job_names.allowlist`
2. 更新 `docs/ci_nightly_workflow_refactor/contract.md`:
   - 第 2 章 'Job ID 与 Job Name 对照表'
3. 运行 `make validate-workflows` 验证

### 10.5 CI 选择 `--strict` 模式的原因

**为什么 CI 使用 `--strict` 模式：**

| 理由 | 说明 |
|------|------|
| 防止漂移 | 确保所有 workflow 变更都被 contract 记录，避免隐性变更导致回归 |
| Extra job 覆盖 | `--strict` 隐式启用 `--require-job-coverage`，确保所有 jobs 在合约管理下 |
| 早期发现 | 将 WARNING 提升为 ERROR，强制 PR 阶段解决问题 |
| 简化维护 | 统一策略减少歧义，降低合约维护成本 |

**为什么不启用 `--require-frozen-consistency`：**

- 策略 A（最小冻结）只冻结被外部系统引用的核心 job/step name
- 非核心项允许灵活调整，不强制纳入冻结列表
- 在严格检测与灵活迭代之间取得平衡

### 10.6 紧急回滚方案

当 CI 因合约校验失败阻断而需要紧急绕过时：

**方案 A：切换到非 strict 模式**

修改 `.github/workflows/ci.yml` 的 `workflow-contract` job：

```yaml
# 原配置（strict 模式）
- name: Validate workflow contract
  run: make validate-workflows-strict

# 回滚配置（非 strict 模式，WARNING 不阻断）
- name: Validate workflow contract
  run: make validate-workflows
```

**方案 B：本地强制提交**

```bash
# 非 strict 验证查看问题
make validate-workflows
# 强制提交等待后续修复
git commit --no-verify -m "紧急修复: 临时绕过合约校验"
```

**⚠️ 回滚后必须执行：**

1. 创建跟进 Issue 记录回滚原因
2. 尽快修复根本问题并恢复 strict 模式
3. 运行 `make validate-workflows-strict` 确认修复

### 10.7 变更类型行为对照表

以下表格列出各类变更在不同校验模式下的行为：

| 变更类型 | CI 默认（`--strict`） | 本地默认（非 strict） | 启用 `--require-frozen-consistency` |
|----------|----------------------|---------------------|-----------------------------------|
| **新增 job** | ERROR（job 不在 `job_ids` 中） | WARNING | ERROR |
| **删除 job** | ERROR（`job_ids` 中的 job 不存在） | ERROR | ERROR |
| **重命名 frozen job name** | ERROR（`frozen_job_name_changed`） | ERROR | ERROR |
| **重命名非 frozen job name** | WARNING（`job_name_mismatch`） | WARNING | ERROR（要求全部冻结） |
| **重命名 frozen step（在 required_steps 中）** | ERROR（`frozen_step_name_changed`） | ERROR | ERROR |
| **重命名非 frozen step（在 required_steps 中）** | WARNING（`step_name_changed`） | WARNING | ERROR（要求全部冻结） |
| **重命名非 required step** | 无校验（通过） | 无校验（通过） | 无校验（通过） |
| **增加 upload-artifact path** | 无校验（通过） | 无校验（通过） | 无校验（通过） |
| **删除 required_artifact_path** | ERROR（`missing_artifact_path`） | ERROR | ERROR |
| **Makefile target 新增** | 无校验（通过） | 无校验（通过） | 无校验（通过） |
| **Makefile target 删除（在 targets_required 中）** | ERROR（`missing_makefile_target`） | ERROR | ERROR |
| **工具脚本变更（无版本更新）** | ERROR（版本策略检查） | WARNING | ERROR |
| **工具脚本变更（含版本更新）** | 通过 | 通过 | 通过 |

**关键说明：**

1. **CI 默认使用 `--strict` 模式**：所有 WARNING 提升为 ERROR，extra job 检测启用
2. **本地默认使用非 strict 模式**：WARNING 不阻断，便于快速迭代
3. **`--require-frozen-consistency`**：强制所有 `required_steps` 和 `job_names` 都必须在冻结列表中

**避免非关键命名改动阻断的建议：**

1. **精简 required_steps**：仅纳入核心验证/产物生成步骤（参见 5.5.6 节）
2. **仅冻结外部引用的名称**：只将被 GitHub Required Checks、artifact 名称引用的 job/step 加入冻结列表
3. **使用 WARNING 容忍机制**：非冻结项改名产生 WARNING 而非 ERROR，本地开发时不阻断

---

## 11. SemVer Policy / 版本策略

本节定义 workflow contract 文件（`workflow_contract.v1.json`）、workflow 文件（`.github/workflows/*.yml`）及相关文档的版本变更规则。

### 11.1 版本变更分类

| 变更类型 | 版本位 | 示例 |
|----------|--------|------|
| **Breaking Change**（不兼容变更） | Major (X.0.0) | 删除必需的 job/step、修改 output key 名称、修改 artifact 路径 |
| **Feature Addition**（功能新增） | Minor (0.X.0) | 新增 job、新增 output key、新增 frozen step |
| **Fix / Docs Only**（修复/仅文档） | Patch (0.0.X) | 修复错误、文档完善、注释更新 |

### 11.2 版本策略规则

1. **Workflow 文件变更**
   - 删除或重命名已有 job/step：**Major** 升级
   - 新增 job/step：**Minor** 升级
   - 修改 step 内部实现（name 不变）：**Patch** 升级

2. **Contract 字段变更**
   - 删除或重命名必需字段（如 `job_ids`、`required_steps`）：**Major** 升级
   - 新增字段、新增校验规则：**Minor** 升级
   - 修正字段值错误、调整描述：**Patch** 升级

3. **仅文档变更**
   - 不涉及 contract JSON 或 workflow 文件的纯文档更新：**Patch** 升级
   - 文档结构重组但内容不变：**Patch** 升级

4. **工具脚本 / Schema 变更**（详见 11.2.1）
   - 校验逻辑不兼容变更：**Major** 升级
   - 新增校验功能或错误类型：**Minor** 升级
   - 修复 bug、优化性能、完善提示：**Patch** 升级

### 11.2.1 工具脚本版本策略细则

以下脚本和 Schema 文件被纳入版本策略检查范围（`check_workflow_contract_version_policy.py`）：

| 文件路径 | 角色 | 版本影响 |
|----------|------|----------|
| `scripts/ci/validate_workflows.py` | 合约校验器核心脚本 | 校验逻辑变更需版本更新 |
| `scripts/ci/workflow_contract.v1.schema.json` | 合约 JSON Schema | 字段约束变更需版本更新 |
| `scripts/ci/check_workflow_contract_docs_sync.py` | 文档同步校验脚本 | 同步规则变更需版本更新 |
| `scripts/ci/workflow_contract_drift_report.py` | 漂移报告生成脚本 | 报告格式变更需版本更新 |
| `scripts/ci/generate_workflow_contract_snapshot.py` | 快照生成脚本 | 快照格式变更需版本更新 |

**工具脚本变更分类：**

| 变更类型 | 版本位 | 示例 |
|----------|--------|------|
| **不兼容变更** | Major | 删除校验规则、修改错误码含义、移除命令行参数 |
| **功能新增** | Minor | 新增校验规则、新增错误类型、新增命令行参数 |
| **修复/优化** | Patch | 修复 bug、优化性能、完善错误提示文案 |

> **注意**：Schema 字段约束变更（如新增 required 字段）通常为 **Minor**；删除字段或收紧约束为 **Major**。

### 11.3 版本更新流程

```bash
# 1. 更新 workflow_contract.v1.json 中的 version 字段
# 2. 更新 contract.md 第 14 章版本控制表
# 3. 运行 make validate-workflows 验证一致性
# 4. 运行 make check-workflow-contract-docs-sync 验证文档同步
```

### 11.4 向后兼容性承诺

- **Frozen Step Names**：在 `frozen_step_text.allowlist` 中的 step name 不得随意变更
- **Required Artifact Paths**：`artifact_archive.required_artifact_paths` 中的路径不得随意删除
- **Output Keys**：`detect_changes.outputs` 中的 key 不得随意删除或重命名

---

## 12. SSOT & 同步矩阵

本章定义 workflow 合约的唯一真实来源（SSOT）及各类变更的同步更新要求。

### 12.1 合约字段 SSOT 定义

以下表格定义了 `workflow_contract.v1.json` 中各字段的角色、存储位置及同步要求：

| 字段 | SSOT 位置 | 描述 | 校验脚本 |
|------|-----------|------|----------|
| `version` | `workflow_contract.v1.json` | 合约版本号（SemVer 格式） | `validate_workflows.py` |
| `last_updated` | `workflow_contract.v1.json` | 最后更新日期 | 手动维护 |
| `ci.job_ids` | `workflow_contract.v1.json` | CI workflow 的 Job ID 列表 | `validate_workflows.py` |
| `ci.job_names` | `workflow_contract.v1.json` | CI workflow 的 Job Name 列表 | `validate_workflows.py` |
| `nightly.job_ids` | `workflow_contract.v1.json` | Nightly workflow 的 Job ID 列表 | `validate_workflows.py` |
| `nightly.job_names` | `workflow_contract.v1.json` | Nightly workflow 的 Job Name 列表 | `validate_workflows.py` |
| `*.required_jobs[].required_steps` | `workflow_contract.v1.json` | 每个 Job 的必需 Step 列表 | `validate_workflows.py` |
| `frozen_job_names.allowlist` | `workflow_contract.v1.json` | 禁止改名的 Job Name 冻结列表 | `validate_workflows.py` |
| `frozen_step_text.allowlist` | `workflow_contract.v1.json` | 禁止改名的 Step Name 冻结列表 | `validate_workflows.py` |
| `ci.artifact_archive` | `workflow_contract.v1.json` | CI workflow 必需上传的 Artifact 路径和步骤 | `validate_workflows.py` |
| `nightly.artifact_archive` | `workflow_contract.v1.json` | Nightly workflow 的 Artifact 配置 | `validate_workflows.py` |
| `make.targets_required` | `workflow_contract.v1.json` | workflow 依赖的 Makefile 目标列表 | `validate_workflows.py` |
| `ci.labels` | `workflow_contract.v1.json` | 支持的 PR Label 列表 | `validate_workflows.py` |

#### 12.1.1 字段约束规则

以下约束由 `validate_workflows.py` 自动校验，违反时报告 ERROR（`--strict` 模式下阻断 CI）：

| 约束 | 校验规则 | 错误类型 |
|------|----------|----------|
| **job_ids/job_names 长度一致** | `<workflow>.job_ids.length == <workflow>.job_names.length` | `contract_job_ids_names_length_mismatch` |
| **job_ids 无重复** | `<workflow>.job_ids` 中每个 id 唯一 | `contract_job_ids_duplicate` |
| **required_jobs id 无重复** | `<workflow>.required_jobs[*].id` 唯一 | `contract_required_job_id_duplicate` |
| **required_jobs id 在 job_ids 中** | `<workflow>.required_jobs[*].id` ∈ `<workflow>.job_ids` | `contract_required_job_not_in_job_ids` |

**设计意图**：
- `job_ids` 和 `job_names` 是位置对应的数组，用于 job ID 到 job name 的映射
- `required_jobs` 定义了需要详细校验（steps/outputs）的 job，其 id 必须在 `job_ids` 中声明
- 这些约束确保 contract 文件自身的结构完整性

### 12.2 同步矩阵

当修改某类合约字段时，需要同步更新以下文件。**✅** 表示必须更新，**⚠️** 表示可能需要更新：

| 变更类型 | workflow YAML | contract JSON | contract.md | coupling_map.md | maintenance.md | Makefile | 脚本文件 |
|----------|---------------|---------------|-------------|-----------------|----------------|----------|----------|
| **job_ids** | ✅ 添加/删除 job | ✅ `job_ids[]` | ✅ 第 2 章 | ✅ 对应章节 | ⚠️ checklist | - | - |
| **job_names** | ✅ 修改 job name | ✅ `job_names[]` | ✅ 第 2 章 | ✅ 对应章节 | ⚠️ checklist | - | - |
| **required_steps** | ✅ 修改 step | ✅ `required_jobs[].required_steps` | ⚠️ 第 5 章（若冻结） | ⚠️ 对应 job 章节 | ⚠️ checklist | - | - |
| **frozen_job_names** | - | ✅ `frozen_job_names.allowlist` | ✅ 第 5.1 节 | - | ✅ 6.3 节 | - | - |
| **frozen_step_text** | - | ✅ `frozen_step_text.allowlist` | ✅ 第 5.2 节 | - | ✅ 6.3 节 | - | - |
| **artifact_archive** | ✅ upload-artifact 步骤 | ✅ `artifact_archive.*` | ✅ 第 8 章 | ✅ 产物映射表 | ✅ 1.7 节 | - | - |
| **make.targets_required** | ⚠️ 若 job 调用新 target | ✅ `make.targets_required[]` | ✅ 第 7 章 | ✅ 3.1/3.2 节 | ✅ 1.2 节 | ✅ 添加 target | ⚠️ 若涉及新脚本 |
| **labels** | ⚠️ 若消费 label | ✅ `ci.labels[]` | ✅ 第 3 章 | - | ✅ 1.4 节 | - | ✅ `gh_pr_labels_to_outputs.py` |
| **version** | - | ✅ `version` 字段 | ✅ 第 14 章 | - | ⚠️ 6.4 节 | - | - |
| **last_updated** | - | ✅ `last_updated` 字段 | - | - | - | - | - |
| **校验脚本** | - | ✅ `version` (触发更新) | ✅ 第 11.2.1 节 | - | - | - | ✅ 脚本自身 |
| **Schema** | - | ✅ `version` (触发更新) | ✅ 第 11.2.1 节 | - | - | - | ✅ Schema 文件 |

### 12.3 文件角色定义

| 文件路径 | 角色 | 说明 |
|----------|------|------|
| `scripts/ci/workflow_contract.v1.json` | **SSOT（机器可读合约）** | 所有合约字段的唯一真实来源，供 `validate_workflows.py` 自动校验 |
| `.github/workflows/ci.yml` | **实现（CI workflow）** | CI workflow 的实际定义，必须与 contract JSON 保持一致 |
| `.github/workflows/nightly.yml` | **实现（Nightly workflow）** | Nightly workflow 的实际定义，必须与 contract JSON 保持一致 |
| `docs/ci_nightly_workflow_refactor/contract.md` | **文档（人类可读合约）** | 合约的人类可读版本，作为"禁止回归"的参考基准 |
| `docs/ci_nightly_workflow_refactor/coupling_map.md` | **文档（耦合映射）** | 记录 workflow 与 Makefile/环境变量/产物路径的耦合关系 |
| `docs/ci_nightly_workflow_refactor/maintenance.md` | **文档（维护指南）** | 提供变更同步 checklist 和操作指南 |
| `Makefile` | **实现（构建目标）** | 定义 workflow 依赖的构建目标 |
| `scripts/ci/gh_pr_labels_to_outputs.py` | **实现（Labels 解析）** | PR Labels 解析脚本，`LABEL_*` 常量必须与 `ci.labels` 同步 |
| `scripts/ci/validate_workflows.py` | **校验（合约校验器）** | 自动校验 workflow 与 contract 的一致性；变更触发版本更新检查 |
| `scripts/ci/check_workflow_contract_docs_sync.py` | **校验（文档同步校验器）** | 校验 contract JSON 与 contract.md 的同步状态；变更触发版本更新检查 |
| `scripts/ci/workflow_contract.v1.schema.json` | **Schema（合约结构定义）** | 定义 contract JSON 的字段约束；变更触发版本更新检查 |
| `scripts/ci/workflow_contract_drift_report.py` | **工具（漂移报告）** | 生成合约漂移报告；变更触发版本更新检查 |
| `scripts/ci/generate_workflow_contract_snapshot.py` | **工具（快照生成）** | 生成合约快照；变更触发版本更新检查 |
| `scripts/ci/check_workflow_contract_version_policy.py` | **校验（版本策略）** | 检查关键文件变更时版本是否已更新 |

### 12.4 变更同步 Quick Reference

以下是常见变更场景的快速同步指引：

#### 12.4.1 新增 CI Job

```bash
# 需要更新的文件：
1. .github/workflows/ci.yml          # 添加 job 定义
2. scripts/ci/workflow_contract.v1.json:
   - ci.job_ids[]                     # 添加 job ID
   - ci.job_names[]                   # 添加 job name
   - ci.required_jobs[]               # 添加 job 详细定义（含 required_steps）
3. docs/ci_nightly_workflow_refactor/contract.md:
   - 第 2.1 章                        # Job ID 与 Job Name 对照表
4. docs/ci_nightly_workflow_refactor/coupling_map.md:
   - 对应章节                         # Job 产物和环境变量映射

# 验证命令：
make validate-workflows-strict
make check-workflow-contract-docs-sync
```

#### 12.4.2 修改冻结 Job/Step Name

```bash
# 需要更新的文件：
1. .github/workflows/*.yml            # 修改 job/step name
2. scripts/ci/workflow_contract.v1.json:
   - frozen_job_names.allowlist       # 或 frozen_step_text.allowlist
   - job_names[] / required_steps[]   # 同步更新引用
3. docs/ci_nightly_workflow_refactor/contract.md:
   - 第 5.1 或 5.2 节                 # Frozen Names 列表

# 验证命令：
make validate-workflows-strict
```

#### 12.4.3 新增 Makefile Target（被 workflow 调用）

```bash
# 需要更新的文件：
1. Makefile                           # 添加新 target
2. scripts/ci/workflow_contract.v1.json:
   - make.targets_required[]          # 添加 target 名称
3. docs/ci_nightly_workflow_refactor/contract.md:
   - 第 7 章                          # Make Target 清单
4. docs/ci_nightly_workflow_refactor/coupling_map.md:
   - 第 3 章                          # Makefile Targets 清单

# 验证命令：
make validate-workflows-strict
```

#### 12.4.4 新增 Artifact 上传路径

```bash
# 需要更新的文件：
1. .github/workflows/*.yml            # 添加 upload-artifact 步骤
2. scripts/ci/workflow_contract.v1.json:
   - artifact_archive.required_artifact_paths[]
   - artifact_archive.artifact_step_names[]（可选）
3. docs/ci_nightly_workflow_refactor/contract.md:
   - 第 8 章                          # Artifact Archive 合约
4. docs/ci_nightly_workflow_refactor/coupling_map.md:
   - 产物上传表格

# 验证命令：
make validate-workflows-strict
```

#### 12.4.5 新增 PR Label

```bash
# 需要更新的文件：
1. scripts/ci/workflow_contract.v1.json:
   - ci.labels[]                      # SSOT，必须先更新
2. scripts/ci/gh_pr_labels_to_outputs.py:
   - LABEL_* 常量                     # 添加新常量
3. docs/ci_nightly_workflow_refactor/contract.md:
   - 第 3 章                          # PR Label 列表与语义

# 验证命令：
make validate-workflows-strict        # 会自动校验 labels 一致性
```

#### 12.4.6 工具脚本 / Schema 变更

```bash
# 触发版本更新检查的文件：
- scripts/ci/validate_workflows.py           # 合约校验器核心脚本
- scripts/ci/workflow_contract.v1.schema.json # 合约 JSON Schema
- scripts/ci/check_workflow_contract_docs_sync.py # 文档同步校验脚本
- scripts/ci/workflow_contract_drift_report.py    # 漂移报告生成脚本
- scripts/ci/generate_workflow_contract_snapshot.py # 快照生成脚本

# 需要更新的文件：
1. scripts/ci/workflow_contract.v1.json:
   - version 字段                     # 版本号升级（按 11.2.1 节规则）
   - last_updated 字段                # 更新日期
2. docs/ci_nightly_workflow_refactor/contract.md:
   - 第 11.2.1 节                     # 如变更影响版本策略细则
   - 第 14 章                         # 版本控制表

# 版本升级规则（详见 11.2.1 节）：
- Major: 删除校验规则、修改错误码含义、移除命令行参数
- Minor: 新增校验规则、新增错误类型、新增命令行参数
- Patch: 修复 bug、优化性能、完善错误提示

# 验证命令：
make check-workflow-contract-version-policy   # 版本策略检查
make validate-workflows-strict                # 合约校验
```

### 12.5 版本更新触发条件

| 变更类型 | 版本位 | 触发条件示例 |
|----------|--------|--------------|
| **Major (X.0.0)** | Breaking Change | 删除 job/step、重命名 output key、删除 artifact 路径 |
| **Minor (0.X.0)** | Feature Addition | 新增 job/step、新增 frozen name、新增 output key |
| **Patch (0.0.X)** | Fix / Docs Only | 文档完善、描述修正、注释更新 |

> **详细版本策略**：参见第 11 章 [SemVer Policy / 版本策略](#11-semver-policy--版本策略)

---

## 14. 版本控制

| 版本 | 日期 | 变更说明 |
|------|------|----------|
| v2.14.0 | 2026-02-02 | 新增第 0 章"合约系统概览"：关键文件清单、检查脚本详细说明（运行命令/exit code/artifact 路径）、CI 阻断策略边界定义；章节重编号（原 13 章改为 14 章） |
| v2.13.0 | 2026-02-02 | 新增 8.7 节"Drift Report 漂移报告合约"：定义运行时机、输出位置、阻断策略；更新 artifact_archive 配置包含 drift report 输出 |
| v2.12.0 | 2026-02-02 | 新增 mcp-error-contract job 到 CI workflow 合约：MCP JSON-RPC 错误码合约与文档同步检查 |
| v2.11.0 | 2026-02-02 | 新增 iteration-audit job 到 nightly workflow 合约：迭代文档审计（轻量级 job）；新增 `iteration-audit` make target；补充 `Upload drift report` 到 frozen steps 列表 |
| v2.10.0 | 2026-02-02 | 新增 contract 内部一致性检查：`contract_job_ids_names_length_mismatch`（job_ids/job_names 长度一致）、`contract_job_ids_duplicate`（job_ids 无重复）、`contract_required_job_id_duplicate`（required_jobs id 无重复）、`contract_required_job_not_in_job_ids`（required_jobs id 在 job_ids 中）；新增 10.2.1 和 12.1.1 节文档 |
| v2.9.0 | 2026-02-02 | 新增 iteration-tools-test job：运行迭代工具脚本测试（无需数据库依赖）；新增 `test-iteration-tools` Makefile target |
| v2.8.0 | 2026-02-02 | 新增 workflow contract version policy 检查：关键文件变更时强制要求 version/last_updated 更新并同步到 contract.md 版本控制表；新增 `check-workflow-contract-version-policy` make target 和 CI step |
| v2.7.1 | 2026-02-02 | 新增 required_steps 覆盖原则文档（5.5 节）：定义核心子集/全量两档策略；明确必须纳入和允许不纳入的步骤类型；更新 validate_workflows.py 报错文案 |
| v2.7.0 | 2026-02-02 | 新增第 12 章"SSOT & 同步矩阵"：定义合约字段 SSOT 位置、同步更新矩阵、文件角色定义、变更同步 Quick Reference |
| v2.6.1 | 2026-02-02 | 补充 CI `--strict` 模式说明（10.5 节）：解释为什么 CI 选择 strict 模式；新增紧急回滚方案（10.6 节）；修正参数文档与 CI 实际调用的一致性 |
| v2.6.0 | 2026-02-02 | 新增 Extra Job Coverage 策略（第 10 章）：检测 workflow 中未在 contract 声明的 extra jobs；新增 `--require-job-coverage` 参数 |
| v2.5.0 | 2026-02-02 | 新增 gateway-import-surface job：检查 Gateway __init__.py 懒加载策略（禁止 eager-import） |
| v2.4.0 | 2026-02-02 | 新增 gateway-public-api-surface job：检查 Gateway Public API 导入表面（__all__ 与实际导出一致性） |
| v2.3.0 | 2026-02-02 | 重构冻结范围：frozen_job_names 精简为 4 个核心 job；frozen_step_text 精简为 12 个核心 step；新增 Rename 流程文档（5.3 节） |
| v2.2.0 | 2026-02-02 | 新增 iteration-docs-check job：检查 .iteration/ 链接和 SUPERSEDED 一致性 |
| v2.0.0 | 2026-02-02 | Phase 1 范围收敛：移除 release workflow 合约（Phase 2 预留）；统一版本号到 semver 格式；移除 SeekDB 组件 |
| v1.12 | 2026-01-30 | 新增 Acceptance 验收测试合约：定义 CI 组合式覆盖 vs Nightly 直接执行的合约、产物要求、record_acceptance_run.py 调用规范 |
| v1.11 | 2026-01-30 | 新增 Artifact Archive 合约：定义 ci/nightly 必需的 artifact paths；validate_workflows.py 新增 upload-artifact 步骤扫描验证 |
| v1.10 | 2026-01-30 | 新增 Labels 一致性校验：`validate_workflows.py` 自动校验 `ci.labels` 与 `gh_pr_labels_to_outputs.py` 中 `LABEL_*` 常量的一致性 |
| v1.9 | 2026-01-30 | 新增 `contract_changed` 输出键：Makefile 和 `docs/ci_nightly_workflow_refactor/**` 变更触发 workflow-contract-check；新增 `docs-check` job 定义 |
| v1.8 | 2026-01-30 | 冻结 step 验证强化：`frozen_step_text.allowlist` 中的 step 改名现为 ERROR（非 WARNING），阻止 CI 通过 |
| v1.3 | 2026-01-30 | upstream_ref 变更要求：新增补丁产物要求，CI 先生成补丁再校验流程 |
| v1.2 | 2026-01-30 | Release Gate 封装：新增 `release-gate` 聚合目标，合并多个独立 make 调用为单一步骤 |
| v1.0 | 2026-01-30 | 初始版本，固化 CI/Nightly/Release 合约 |
