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

<!-- BEGIN:CI_JOB_TABLE -->
| Job ID | Job Name | 说明 |
|--------|----------|------|
| `test` | Test (Python ${{ matrix.python-version }}) | 单元测试、集成测试和验收测试（含数据库迁移验证） |
| `lint` | Lint | 代码风格检查（ruff）和类型检查（mypy baseline + strict-island 双层策略） |
| `no-iteration-tracked` | No .iteration/ Tracked Files | 检查 .iteration/ 目录下无被 Git 追踪的文件 |
| `env-var-consistency` | Environment Variable Consistency | 环境变量配置一致性检查 |
| `schema-validate` | Schema Validation | JSON Schema 校验 |
| `logbook-consistency` | Logbook Consistency Check | Logbook 配置一致性检查 |
| `migration-sanity` | Migration Sanity Check | SQL 迁移文件存在性和基础语法检查 |
| `sql-safety` | SQL Migration Safety Check | SQL 迁移安全性检查 |
| `gateway-di-boundaries` | Gateway DI Boundaries Check | Gateway DI 边界检查（禁止 deps.db 直接使用） |
| `scm-sync-consistency` | SCM Sync Consistency Check | SCM Sync 配置一致性检查 |
| `gateway-error-reason-usage` | Gateway ErrorReason Usage Check | Gateway ErrorReason 使用规范检查 |
| `gateway-import-surface` | Gateway Import Surface Check | Gateway __init__.py 懒加载策略检查（禁止 eager-import） |
| `gateway-public-api-surface` | Gateway Public API Import Surface Check | Gateway Public API 导入表面（__all__ 与实际导出一致性）及文档同步检查 |
| `gateway-correlation-id-single-source` | Gateway correlation_id Single Source Check | Gateway correlation_id 单一来源（SSOT 模块）检查 |
| `mcp-error-contract` | MCP Error Contract Check | MCP JSON-RPC 错误码合约与文档同步检查 |
| `iteration-docs-check` | Iteration Docs Check | 迭代文档规范检查（.iteration/ 链接和 SUPERSEDED 一致性） |
| `ci-test-isolation` | CI Test Isolation Check | CI 测试隔离检查（tests/ci/ 测试文件禁止被外部导入） |
| `iteration-tools-test` | Iteration Tools Test | 迭代工具脚本测试（无数据库依赖） |
| `workflow-contract` | Workflow Contract Validation | Workflow 合约校验（strict 模式）、文档同步、版本策略和内部一致性检查 |
<!-- END:CI_JOB_TABLE -->

### 2.2 Nightly Workflow (`nightly.yml`)

<!-- BEGIN:NIGHTLY_JOB_TABLE -->
| Job ID | Job Name | 说明 |
|--------|----------|------|
| `unified-stack-full` | Unified Stack Full Verification | 完整验证流程：环境检测 -> Gate Contract 校验 -> Docker Compose 启动 -> 服务健康检查 -> 集成测试 -> 验证 -> 清理 -> 记录 -> 上传 |
| `iteration-audit` | Iteration Docs Audit | 迭代文档审计（轻量级检查） |
| `notify-results` | Notify Results | Nightly 运行结果通知 |
<!-- END:NIGHTLY_JOB_TABLE -->

### 2.3 Release Workflow (`release.yml`) - Phase 2 预留

> **注意**: Release workflow 将在 Phase 2 引入，当前合约版本不包含此部分。

### 2.4 Phase 2 设计：Release Workflow 纳入合约

本节预定义 `release.yml` 纳入合约的设计方案，供 Phase 2 实施时参考。

#### 2.4.1 最小字段集合

当 `release.yml` 纳入合约时，需在 `workflow_contract.v1.json` 中添加以下字段：

```json
{
  "release": {
    "file": ".github/workflows/release.yml",
    "job_ids": ["build", "publish", "notify"],
    "job_names": ["Build Release", "Publish to Registry", "Notify Release"],
    "required_jobs": [
      {
        "id": "build",
        "name": "Build Release",
        "required_steps": [
          "Checkout repository",
          "Set up Python",
          "Build package",
          "Upload release artifacts"
        ]
      }
    ],
    "artifact_archive": {
      "required_artifact_paths": [
        "dist/*.whl",
        "dist/*.tar.gz"
      ],
      "artifact_step_names": [
        "Upload release artifacts"
      ]
    },
    "labels": []
  }
}
```

**字段说明：**

| 字段 | 必需 | 说明 |
|------|------|------|
| `file` | ✅ | Workflow 文件路径，必须为 `.github/workflows/release.yml` |
| `job_ids` | ✅ | Release workflow 的所有 job ID 列表 |
| `job_names` | ✅ | 与 `job_ids` 位置对应的 job name 列表 |
| `required_jobs` | ⚠️ | 至少定义核心 job（如 build）的 required_steps |
| `artifact_archive` | ⚠️ | Release 产物路径（如 wheel/sdist） |
| `labels` | ❌ | 可选，release 专用 PR labels |

#### 2.4.2 Frozen 冻结范围策略

**策略选项：**

| 选项 | 优点 | 缺点 | 推荐场景 |
|------|------|------|----------|
| **A：复用现有 allowlist** | 统一管理、避免重复 | 冻结列表可能过长 | Release jobs/steps 与 CI 高度共用 |
| **B：新增 release 专用 allowlist** | 隔离管理、灵活控制 | 维护两套列表 | Release 有独立的冻结需求 |
| **C：混合策略（推荐）** | 平衡灵活性与一致性 | 需明确边界 | 共用基础步骤，独立 release 专用步骤 |

**推荐策略 C 实现方式：**

1. **共用基础步骤**：`Checkout repository`、`Set up Python` 等基础步骤复用 `frozen_step_text.allowlist`
2. **Release 专用步骤**：如 `Build package`、`Publish to Registry` 等添加到同一 `frozen_step_text.allowlist`，并在注释中标注 `[release]`
3. **Job Names**：Release 的核心 job names 添加到 `frozen_job_names.allowlist`，标注 `[release]`

**合约配置示例（策略 C）：**

```json
{
  "frozen_step_text": {
    "_comment": "Phase 2: [release] 标注表示 release workflow 专用",
    "allowlist": [
      "Checkout repository",
      "Set up Python",
      "Install dependencies",
      "Build package",
      "Upload release artifacts",
      "Publish to Registry"
    ]
  },
  "frozen_job_names": {
    "_comment": "Phase 2: [release] 标注表示 release workflow 专用",
    "allowlist": [
      "Test (Python ${{ matrix.python-version }})",
      "Lint",
      "Workflow Contract Validation",
      "Unified Stack Full Verification",
      "Build Release",
      "Publish to Registry"
    ]
  }
}
```

#### 2.4.3 Version Policy 扩展

当纳入 `release.yml` 时，需扩展 `CRITICAL_WORKFLOW_RULES` 以触发版本策略检查。

**扩展步骤：**

1. **修改 `check_workflow_contract_version_policy.py`**：
   ```python
   # 在 CRITICAL_WORKFLOW_RULES 中扩展正则
   # 当前: r"^\.github/workflows/(ci|nightly)\.yml$"
   # Phase 2: r"^\.github/workflows/(ci|nightly|release)\.yml$"
   ```

2. **修改 `WORKFLOW_DOC_ANCHORS`**（在 `check_workflow_contract_docs_sync.py`）：
   ```python
   WORKFLOW_DOC_ANCHORS = {
       "ci": [...],
       "nightly": [...],
       "release": ["### 2.3 Release Workflow", "Release Workflow (`release.yml`)"],
   }
   ```

3. **更新 `contract.md` 文档**：
   - 将 "2.3 Release Workflow - Phase 2 预留" 改为正式章节
   - 添加 Release workflow 的 Job ID / Job Name 对照表
   - 更新第 14 章版本控制表

**迁移 Checklist：**

| 步骤 | 文件 | 操作 |
|------|------|------|
| 1 | `workflow_contract.v1.json` | 添加 `release` 字段定义 |
| 2 | `workflow_contract.v1.json` | 更新 `frozen_*` allowlist（如需） |
| 3 | `workflow_contract.v1.json` | 更新 `make.targets_required`（如有 release 专用 targets） |
| 4 | `check_workflow_contract_version_policy.py` | 扩展 `CRITICAL_WORKFLOW_RULES` 正则 |
| 5 | `check_workflow_contract_docs_sync.py` | 扩展 `WORKFLOW_DOC_ANCHORS` |
| 6 | `validate_workflows.py` | 无需修改（自动发现 workflow keys） |
| 7 | `suggest_workflow_contract_updates.py` | 无需修改（自动发现 workflow keys） |
| 8 | `contract.md` | 更新 2.3 节为正式章节 |
| 9 | `contract.md` | 更新 version 字段（Minor 升级） |
| 10 | `contract.md` | 更新第 14 章版本控制表 |

---

## 3. PR Label 列表与语义

> **SSOT 说明**: `scripts/ci/workflow_contract.v1.json` 的 `ci.labels` 字段是 PR Labels 的唯一真实来源（SSOT）。本节内容必须与该 JSON 文件保持同步。
>
> **当前状态**: CI workflow 当前**不消费** PR labels。`gh_pr_labels_to_outputs.py` 脚本存在但未被 ci.yml 调用。Labels 仅用于合约定义和一致性校验。

<!-- BEGIN:LABELS_TABLE -->
| Label | Workflow | 语义 |
|-------|----------|------|
| `openmemory:freeze-override` | ci | PR label |
<!-- END:LABELS_TABLE -->

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

### 3.2 当前不消费 Labels 的原因与启用流程

#### 3.2.1 当前状态与约束

**当前状态**：CI workflow 当前**不消费** PR labels。虽然 `gh_pr_labels_to_outputs.py` 脚本存在且功能完整，但 `ci.yml` 中未调用该脚本。Labels 仅用于合约定义和一致性校验。

**不消费的原因**：

| 原因类别 | 说明 |
|----------|------|
| **简化架构** | Phase 1 重构移除了 `detect-changes` job，采用"所有检查始终执行"的简化模式 |
| **降低复杂度** | Labels 驱动的条件执行增加了 workflow 的复杂度和调试难度 |
| **合约优先** | 当前优先保证合约定义的完整性，labels 消费作为 Phase 2 的可选功能预留 |
| **SeekDB 移除** | v2.0.0 移除了 SeekDB 组件相关的 labels，当前仅剩 `openmemory:freeze-override` 一个 label |

**保留脚本的原因**：

1. **合约校验依赖**：`validate_workflows.py` 会校验 `ci.labels` 与 `gh_pr_labels_to_outputs.py` 中 `LABEL_*` 常量的一致性
2. **未来扩展性**：当需要 label 驱动行为时，无需重写解析逻辑
3. **向后兼容**：保持与旧版本 workflow 的兼容性

#### 3.2.2 启用 Labels 消费的流程

如需启用 PR labels 消费（使 workflow 行为依赖于 labels），按以下步骤操作：

**Step 1: 在 ci.yml 中添加 Labels 解析步骤**

在需要消费 labels 的 job 中添加以下步骤（推荐添加在 `Checkout repository` 之后）：

```yaml
# 新增 job 或在现有 job 中添加
parse-labels:
  name: Parse PR Labels
  runs-on: ubuntu-latest
  outputs:
    has_freeze_override_label: ${{ steps.labels.outputs.has_freeze_override_label }}
  steps:
    - name: Checkout repository
      uses: actions/checkout@v4

    - name: Set up Python
      uses: actions/setup-python@v5
      with:
        python-version: '3.11'

    - name: Parse PR labels
      id: labels
      env:
        GITHUB_EVENT_NAME: ${{ github.event_name }}
        PR_LABELS: ${{ join(github.event.pull_request.labels.*.name, ',') }}
      run: |
        python scripts/ci/gh_pr_labels_to_outputs.py
```

**关键环境变量说明**：

| 环境变量 | 来源 | 说明 |
|----------|------|------|
| `GITHUB_EVENT_NAME` | `${{ github.event_name }}` | 事件类型（`pull_request`、`push`、`workflow_dispatch`） |
| `PR_LABELS` | `${{ join(github.event.pull_request.labels.*.name, ',') }}` | 逗号分隔的 PR labels 列表 |
| `GITHUB_OUTPUT` | GitHub Actions 自动设置 | outputs 文件路径 |

**Step 2: 在后续 job 中引用 outputs**

```yaml
some-job:
  name: Some Job
  needs: [parse-labels]
  if: needs.parse-labels.outputs.has_freeze_override_label == 'true'
  # 或使用 outputs 作为条件
  steps:
    - name: Conditional step
      if: needs.parse-labels.outputs.has_freeze_override_label == 'true'
      run: echo "Freeze override is active"
```

**Step 3: 更新合约与文档**

启用 labels 消费后，需要同步更新以下内容：

| 文件 | 更新内容 |
|------|----------|
| `workflow_contract.v1.json` | 如有新 label：更新 `ci.labels`；如有新 job：更新 `job_ids`、`job_names`、`required_jobs` |
| `contract.md` | 更新第 2 章（job 列表）、第 3 章（labels 说明） |
| `maintenance.md` | 更新 1.4 节启用 checklist |

#### 3.2.3 输出如何影响后续 Job

`gh_pr_labels_to_outputs.py` 脚本当前定义的输出：

| Output Key | 值类型 | 说明 | 使用场景 |
|------------|--------|------|----------|
| `has_freeze_override_label` | `true`/`false` | 是否存在 `openmemory:freeze-override` label | 绕过冻结检查 |

**扩展新 label 时的输出约定**：

1. **命名规范**：output key 使用 `has_<label_slug>` 格式（如 `has_skip_test_label`）
2. **值类型**：统一使用 `true`/`false` 字符串（便于 YAML 条件判断）
3. **同步更新**：
   - 在 `gh_pr_labels_to_outputs.py` 添加 `LABEL_*` 常量
   - 在 `workflow_contract.v1.json` 的 `ci.labels` 添加 label
   - 运行 `make validate-workflows-strict` 验证一致性

**示例：添加 `ci:skip-slow-tests` label**

```python
# gh_pr_labels_to_outputs.py
LABEL_SKIP_SLOW_TESTS = "ci:skip-slow-tests"

# 在 main() 中添加
has_skip_slow_tests = "true" if LABEL_SKIP_SLOW_TESTS in labels else "false"
write_output("has_skip_slow_tests_label", has_skip_slow_tests)
```

```yaml
# ci.yml - 在后续 job 中使用
- name: Run slow tests
  if: needs.parse-labels.outputs.has_skip_slow_tests_label != 'true'
  run: pytest tests/slow/ -v
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

<!-- BEGIN:FROZEN_JOB_NAMES_TABLE -->
| Job Name | 原因 |
|----------|------|
| `Lint` | Required Check |
| `Test (Python ${{ matrix.python-version }})` | Required Check |
| `Unified Stack Full Verification` | Required Check |
| `Workflow Contract Validation` | Required Check |
<!-- END:FROZEN_JOB_NAMES_TABLE -->

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

<!-- BEGIN:FROZEN_STEP_NAMES_TABLE -->
| Step Name | 冻结原因 |
|-----------|----------|
| `Checkout repository` | 核心步骤 |
| `Install dependencies` | 核心步骤 |
| `Run acceptance tests` | 核心步骤 |
| `Run unified stack verification (full)` | 核心步骤 |
| `Run unit and integration tests` | 核心步骤 |
| `Set up Python` | 核心步骤 |
| `Start unified stack with Docker Compose` | 核心步骤 |
| `Upload drift report` | 核心步骤 |
| `Upload migration logs` | 核心步骤 |
| `Upload test results` | 核心步骤 |
| `Upload validation report` | 核心步骤 |
| `Upload validation results` | 核心步骤 |
| `Validate workflow contract` | 核心步骤 |
<!-- END:FROZEN_STEP_NAMES_TABLE -->

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

### 5.6 Step Name Aliases 别名映射

`step_name_aliases` 字段允许为 `required_steps` 中的 canonical step name 定义一组可接受的别名。当 validator 在 workflow 中找不到 canonical step name 时，会检查是否存在匹配的 alias。

#### 5.6.1 配置格式

```json
"step_name_aliases": {
  "Canonical Step Name": ["Alias 1", "Alias 2"],
  "Run unit tests": ["Run tests", "Execute unit tests"]
}
```

**说明**：
- Key 是 canonical step name（与 `required_steps` 中的名称一致）
- Value 是 alias 数组，每个 alias 必须精确匹配（区分大小写）

#### 5.6.2 匹配行为

当 `required_step` 在 workflow 中不存在时，validator 按以下顺序检查：

1. **Alias 匹配**：检查是否有 alias 在实际步骤中存在
   - 匹配成功：报告 `step_name_alias_matched` WARNING（不阻断 CI）
2. **Fuzzy 匹配**：尝试模糊匹配（部分匹配、词语重叠）
   - 冻结步骤改名：报告 `frozen_step_name_changed` ERROR
   - 非冻结步骤改名：报告 `step_name_changed` WARNING
3. **无匹配**：报告 `missing_step` ERROR

#### 5.6.3 使用场景

| 场景 | 推荐做法 |
|------|----------|
| **步骤名称临时变更** | 添加 alias，后续统一修改为 canonical name |
| **多语言/格式变体** | 使用 alias 容纳变体（如 `Run tests` / `Execute tests`） |
| **渐进式迁移** | 新旧名称共存期间使用 alias，迁移完成后移除 |

#### 5.6.4 版本策略影响

| 变更类型 | 版本位 | 说明 |
|----------|--------|------|
| **添加 `step_name_aliases` 字段** | Minor (0.X.0) | 新增可选字段 |
| **添加新的 alias 映射** | Patch (0.0.X) | 扩展现有配置 |
| **删除 alias 映射** | Patch (0.0.X) | 可能导致某些 workflow 校验失败 |
| **删除整个 `step_name_aliases` 字段** | Major (X.0.0) | 移除功能 |

#### 5.6.5 维护建议

1. **优先使用 canonical name**：alias 是临时方案，应尽快统一为 canonical name
2. **避免 alias 泛滥**：每个 canonical step 的 alias 不应超过 3-5 个
3. **定期清理**：移除不再使用的 alias 映射

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

<!-- BEGIN:MAKE_TARGETS_TABLE -->
| Make Target | 用途 |
|-------------|------|
| `apply-openmemory-grants` | CI 必需目标 |
| `apply-roles` | CI 必需目标 |
| `check-ci-test-isolation` | CI 必需目标 |
| `check-env-consistency` | CI 必需目标 |
| `check-gateway-correlation-id-single-source` | CI 必需目标 |
| `check-gateway-di-boundaries` | CI 必需目标 |
| `check-gateway-error-reason-usage` | CI 必需目标 |
| `check-gateway-import-surface` | CI 必需目标 |
| `check-gateway-public-api-surface` | CI 必需目标 |
| `check-iteration-docs` | CI 必需目标 |
| `check-logbook-consistency` | CI 必需目标 |
| `check-migration-sanity` | CI 必需目标 |
| `check-schemas` | CI 必需目标 |
| `check-scm-sync-consistency` | CI 必需目标 |
| `check-workflow-contract-doc-anchors` | CI 必需目标 |
| `check-workflow-contract-docs-sync` | CI 必需目标 |
| `check-workflow-contract-internal-consistency` | CI 必需目标 |
| `check-workflow-contract-version-policy` | CI 必需目标 |
| `check-workflow-make-targets-consistency` | CI 必需目标 |
| `ci` | CI 必需目标 |
| `format` | CI 必需目标 |
| `format-check` | CI 必需目标 |
| `iteration-audit` | CI 必需目标 |
| `lint` | CI 必需目标 |
| `migrate-ddl` | CI 必需目标 |
| `migrate-plan` | CI 必需目标 |
| `typecheck` | CI 必需目标 |
| `validate-workflows-strict` | CI 必需目标 |
| `verify-permissions` | CI 必需目标 |
| `verify-permissions-strict` | CI 必需目标 |
| `verify-unified` | CI 必需目标 |
<!-- END:MAKE_TARGETS_TABLE -->

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
  "_comment": "drift report 产物为 Optional（不列入 required_artifact_paths），使用 if-no-files-found: ignore",
  "required_artifact_paths": [
    "test-results-*.xml",
    "acceptance-results-*.xml",
    "migration-output-*.log",
    "verify-output-*.log",
    "schema-validation-results.json",
    "artifacts/workflow_contract_validation.json",
    "artifacts/workflow_contract_docs_sync.json"
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

### 8.7 产物分级与合约策略

#### 8.7.1 产物分级定义

CI 产物按重要性分为三级：

| 分级 | 定义 | 示例 | 合约要求 |
|------|------|------|----------|
| **Critical（关键）** | 门禁判定必需的产物；缺失会导致无法判定 CI 结果 | `test-results-*.xml`, `schema-validation-results.json` | **必须**列入 `required_artifact_paths` |
| **Core（核心）** | 调试/审计必需的产物；缺失会严重影响问题排查 | `migration-output-*.log`, `artifacts/workflow_contract_validation.json` | **必须**列入 `required_artifact_paths` |
| **Optional（可选）** | 辅助性产物；缺失不影响 CI 判定和基本调试 | Drift 报告、临时调试输出 | **可不列入** `required_artifact_paths`；使用 `if-no-files-found: ignore` |

#### 8.7.2 分级对应的合约策略

| 分级 | `required_artifact_paths` | `artifact_step_names` | `if-no-files-found` | 校验行为 |
|------|--------------------------|----------------------|---------------------|----------|
| **Critical** | ✅ 必须列入 | ✅ 列入（推荐） | `warn` 或 `error` | 缺失时 CI 报 ERROR |
| **Core** | ✅ 必须列入 | ✅ 列入（推荐） | `warn` | 缺失时 CI 报 ERROR |
| **Optional** | ❌ 不列入 | ⚠️ 可选 | `ignore` | 不校验 |

**当前 CI Workflow 产物分级：**

| 产物路径 | 分级 | 说明 |
|----------|------|------|
| `test-results-*.xml` | Critical | 单元测试 JUnit 报告 |
| `acceptance-results-*.xml` | Critical | 验收测试 JUnit 报告 |
| `schema-validation-results.json` | Critical | Schema 校验报告 |
| `artifacts/workflow_contract_validation.json` | Core | 合约校验报告 |
| `artifacts/workflow_contract_docs_sync.json` | Core | 文档同步报告 |
| `migration-output-*.log` | Core | 迁移执行日志 |
| `verify-output-*.log` | Core | 迁移验证日志 |
| `artifacts/workflow_contract_drift.json` | Optional | 漂移报告（JSON）—— **不列入** `required_artifact_paths` |
| `artifacts/workflow_contract_drift.md` | Optional | 漂移报告（Markdown）—— **不列入** `required_artifact_paths` |

> **注意**：Drift Report 产物明确为 Optional 级别，**不列入** `required_artifact_paths`。CI 中的 `Upload drift report` 步骤使用 `if-no-files-found: ignore`，即使文件不存在也不会报错。这与其"参考性报告"的定位一致（详见 8.10 节）。

### 8.8 Artifact 命名与 Retention 稳定性要求

#### 8.8.1 Artifact Name 命名规范

| 规则 | 说明 | 示例 |
|------|------|------|
| **小写 kebab-case** | Artifact name 使用小写字母和连字符 | `test-results-3.11`, `workflow-contract-drift` |
| **含版本/矩阵后缀** | 矩阵 job 的产物需包含区分后缀 | `test-results-${{ matrix.python-version }}` |
| **禁止空格和特殊字符** | 仅允许 `[a-z0-9-]` | ❌ `Test Results`, ✅ `test-results` |
| **稳定性承诺** | Artifact name 一旦发布，不可随意更改（影响外部引用） | 改名需 Major 版本升级 |

**当前 CI Workflow Artifact Names：**

| Artifact Name | Job | 说明 |
|---------------|-----|------|
| `test-results-${{ matrix.python-version }}` | test | 测试结果（按 Python 版本） |
| `migration-logs-${{ matrix.python-version }}` | test | 迁移日志（按 Python 版本） |
| `schema-validation-results` | schema-validate | Schema 校验结果 |
| `workflow-contract-validation` | workflow-contract | 合约校验报告 |
| `workflow-contract-docs-sync` | workflow-contract | 文档同步报告 |
| `workflow-contract-drift` | workflow-contract | 漂移报告 |
| `mcp-error-contract` | mcp-error-contract | MCP 错误码合约报告 |
| `mcp-error-docs-sync` | mcp-error-contract | MCP 错误码文档同步报告 |

#### 8.8.2 Retention Days 策略

| 场景 | `retention-days` | 说明 |
|------|-----------------|------|
| **CI Workflow** | 14 | 标准保留期，满足日常调试和回溯需求 |
| **Nightly Workflow** | 14 | 与 CI 一致 |
| **发布分支** | 30-90 | 可通过 workflow 条件增加（Phase 2 预留） |

**稳定性约束：**
- `retention-days` 不应随意减少（可能影响正在排查的问题）
- 增加 `retention-days` 需考虑存储成本
- 建议保持 workflow 内所有 artifact 使用一致的 retention

#### 8.8.3 版本策略影响

| 变更类型 | 版本位 | 说明 |
|----------|--------|------|
| **Artifact name 改名** | Major (X.0.0) | Breaking Change，影响外部引用 |
| **新增 artifact path** | Minor (0.X.0) | 功能新增 |
| **删除 required_artifact_path** | Major (X.0.0) | Breaking Change |
| **调整 retention-days** | Patch (0.0.X) | 非功能性变更 |

### 8.9 新增/调整 Artifact 回归流程

#### 8.9.1 新增 Artifact 路径

当需要新增 artifact 上传路径时：

```bash
# Step 1: 更新 workflow 文件
# 在 .github/workflows/ci.yml 中添加 upload-artifact 步骤

# Step 2: 更新合约（如果是 Critical/Core 级别产物）
# 编辑 scripts/ci/workflow_contract.v1.json:
#   - artifact_archive.required_artifact_paths: 添加路径
#   - artifact_archive.artifact_step_names: 添加步骤名称（可选）

# Step 3: 回归验证
make validate-workflows-strict

# Step 4: 运行 artifact 相关测试
pytest tests/ci/test_validate_workflows_artifacts.py -v

# Step 5: 更新文档（本文档第 8 章）
# 编辑 docs/ci_nightly_workflow_refactor/contract.md
```

#### 8.9.2 调整 Artifact 路径模式

当调整已有 artifact 的路径模式时（如 `*.xml` → `results/*.xml`）：

```bash
# Step 1: 确认影响范围
# 检查是否有外部系统依赖当前路径模式

# Step 2: 同步更新
# - workflow 文件的 upload-artifact 步骤
# - workflow_contract.v1.json 的 required_artifact_paths

# Step 3: 回归验证（重要！）
make validate-workflows-strict
pytest tests/ci/test_validate_workflows_artifacts.py -v

# Step 4: 版本升级
# 如果是 Breaking Change，需要 Major 版本升级
```

#### 8.9.3 回归命令清单

| 命令 | 用途 | 必需 |
|------|------|------|
| `make validate-workflows-strict` | 校验 required_artifact_paths 覆盖 | ✅ |
| `pytest tests/ci/test_validate_workflows_artifacts.py -v` | 测试路径匹配逻辑 | ✅ |
| `make check-workflow-contract-docs-sync` | 检查文档同步 | ✅ |
| `make check-workflow-contract-version-policy` | 检查版本更新 | ✅ |

#### 8.9.4 路径匹配规则

`validate_workflows.py` 使用以下匹配规则校验 artifact 路径覆盖：

| 规则 | 说明 | 示例 |
|------|------|------|
| **Glob 模式** | 含 `*?[]` 字符时使用 `fnmatch` | `test-results-*.xml` 匹配 `test-results-3.11.xml` |
| **目录匹配** | 以 `/` 结尾时匹配该目录下所有路径 | `.artifacts/runs/` 匹配 `.artifacts/runs/run1.json` |
| **精确匹配** | 其他情况使用精确匹配 | `schema-validation-results.json` |

**边界情况说明：**

| 场景 | required_path | uploaded_path | 是否匹配 |
|------|---------------|---------------|----------|
| 目录本身 | `.artifacts/runs/` | `.artifacts/runs` | ✅ 匹配 |
| 目录下文件 | `.artifacts/runs/` | `.artifacts/runs/data.json` | ✅ 匹配 |
| Glob 模式 | `*.xml` | `test.xml` | ✅ 匹配 |
| Glob 模式（目录前缀） | `artifacts/*.json` | `artifacts/report.json` | ✅ 匹配 |
| 大小写不同 | `TEST.xml` | `test.xml` | ❌ 不匹配（区分大小写） |

### 8.10 Drift Report 漂移报告合约

#### 8.10.1 概述

Drift Report 用于检测 workflow 文件（`.github/workflows/*.yml`）与合约定义（`workflow_contract.v1.json`）之间的差异，生成漂移报告供开发者参考。

#### 8.10.2 运行时机

| 场景 | 触发方式 | Make Target | 阻断策略 |
|------|----------|-------------|----------|
| **本地开发** | 手动执行 | `make workflow-contract-drift-report` | 脚本失败时阻断（返回非零退出码） |
| **本地批量** | 手动执行 | `make workflow-contract-drift-report-all` | 不阻断（使用 `|| true`） |
| **PR/CI** | workflow-contract job | 直接调用脚本 + `|| true` | 默认不阻断 |
| **夜间** | N/A | N/A | 不执行 drift report |

#### 8.10.3 输出位置

| 输出格式 | 文件路径 | CI Artifact 名称 |
|----------|----------|------------------|
| JSON | `artifacts/workflow_contract_drift.json` | `workflow-contract-drift` |
| Markdown | `artifacts/workflow_contract_drift.md` | `workflow-contract-drift` |

#### 8.10.4 阻断策略

**默认行为**：CI 中的 drift report 步骤使用 `|| true`，即使检测到差异也不阻断 CI。报告仅供参考。

**Artifact 上传策略**：
- Drift Report 产物 **不列入** `required_artifact_paths`（详见 8.7.1 节 Optional 分级）
- CI 中 `Upload drift report` 步骤使用 `if-no-files-found: ignore`
- 即使文件不存在，artifact 上传步骤也不会失败

**启用阻断**（如需强化）：

1. **本地阻断**：使用 `make workflow-contract-drift-report`（不加 `|| true`）
2. **CI 阻断**：
   - 修改 `.github/workflows/ci.yml` 中 drift report 步骤，移除 `|| true`
   - 将 drift 产物添加到 `artifact_archive.required_artifact_paths`
   - 将 `Upload drift report` 步骤的 `if-no-files-found` 改为 `warn` 或 `error`
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

**Schema (`workflow_contract.v1.schema.json`) 变更分类：**

| 变更类型 | 版本位 | 示例 |
|----------|--------|------|
| **新增可选字段** | Minor | 添加 `step_name_aliases`（不在 required 中） |
| **新增必需字段** | Major | 添加字段并加入 required 数组 |
| **删除字段** | Major | 移除已有字段定义 |
| **收紧约束** | Major | `minItems: 0` → `minItems: 1`，放宽 pattern |
| **放宽约束** | Minor | `minItems: 1` → `minItems: 0`，收紧 pattern |
| **添加注释字段 (`^_`)** | Patch | 添加 `_note_*`、`_changelog_v*` 等元数据字段 |
| **仅改描述** | Patch | 更新 `description` 文本内容 |

> **Schema 变更 Checklist**：计划引入新字段（如 `step_name_aliases`）时，需按顺序完成：
> 1. 先在 Schema 中定义字段
> 2. 再在 `validate_workflows.py` 中使用
> 3. 更新 `check_workflow_contract_docs_sync.py` 同步规则
> 4. 补充 `contract.md` 文档
> 5. 更新 `version` 触发版本策略检查
>
> **详细 Checklist**：参见第 12.4.7 节 [Schema 字段变更 Checklist](#1247-schema-字段变更-checklist)

### 11.3 版本更新流程

**方式一：使用自动化工具（推荐）**

```bash
# 升级 patch 版本（默认）
python scripts/ci/bump_workflow_contract_version.py patch

# 升级 minor 版本
python scripts/ci/bump_workflow_contract_version.py minor

# 升级 major 版本
python scripts/ci/bump_workflow_contract_version.py major

# 指定显式版本
python scripts/ci/bump_workflow_contract_version.py --version 3.0.0

# 带变更说明
python scripts/ci/bump_workflow_contract_version.py minor --message "新增 XXX 功能"

# 干运行模式（预览变更，不写入文件）
python scripts/ci/bump_workflow_contract_version.py minor --dry-run
```

该工具会自动：
1. 更新 `workflow_contract.v1.json` 的 `version` 和 `last_updated` 字段
2. 在 JSON 顶层插入 `_changelog_vX.Y.Z` 空模板
3. 在 `contract.md` 第 14 章版本控制表顶部插入新行模板

**方式二：手动更新**

```bash
# 1. 更新 workflow_contract.v1.json 中的 version 字段
# 2. 更新 workflow_contract.v1.json 中的 last_updated 字段
# 3. 更新 contract.md 第 14 章版本控制表
# 4. 运行 make validate-workflows 验证一致性
# 5. 运行 make check-workflow-contract-docs-sync 验证文档同步
```

### 11.4 向后兼容性承诺

- **Frozen Step Names**：在 `frozen_step_text.allowlist` 中的 step name 不得随意变更
- **Required Artifact Paths**：`artifact_archive.required_artifact_paths` 中的路径不得随意删除
- **Output Keys**：`detect_changes.outputs` 中的 key 不得随意删除或重命名

### 11.5 版本策略检查失败时的最小修复步骤

当 `make check-workflow-contract-version-policy` 失败时，按以下步骤修复：

#### 11.5.1 诊断失败原因

```bash
# 1. 查看详细错误信息
python scripts/ci/check_workflow_contract_version_policy.py --verbose

# 2. 使用 JSON 输出查看 trigger_reasons（了解哪些文件触发了检查）
python scripts/ci/check_workflow_contract_version_policy.py --json | jq '.trigger_reasons'

# 3. PR 模式下检查（与 CI 一致）
python scripts/ci/check_workflow_contract_version_policy.py --pr-mode --verbose
```

#### 11.5.2 常见错误及修复方法

| 错误类型 | 错误信息 | 修复方法 |
|----------|----------|----------|
| `version_not_updated` | version 字段未更新 | 按 11.2 节规则升级 `workflow_contract.v1.json` 的 `version` 字段 |
| `last_updated_not_updated` | last_updated 字段未更新 | 更新 `last_updated` 为当前日期（格式：`YYYY-MM-DD`） |
| `version_not_in_doc` | 版本不在文档版本控制表中 | 在 `contract.md` 第 14 章添加版本记录行 |

#### 11.5.3 最小修复命令序列

**方式一：使用自动化工具（推荐）**

```bash
# Step 1: 确定版本升级类型（参考 11.2 节）
# - Major: 不兼容变更
# - Minor: 新增功能
# - Patch: 修复/优化

# Step 2: 使用 bump 工具自动更新版本和文档
python scripts/ci/bump_workflow_contract_version.py minor --message "变更说明"
# 或 major / patch，根据 Step 1 确定的类型

# Step 3: 编辑生成的占位符内容
# - 修改 workflow_contract.v1.json 中 _changelog_vX.Y.Z 的内容
# - 修改 contract.md 版本控制表中新行的变更说明

# Step 4: 验证修复
make check-workflow-contract-version-policy
make check-workflow-contract-docs-sync
make validate-workflows-strict
```

**方式二：手动更新**

```bash
# Step 1: 确定版本升级类型（参考 11.2 节）
# - Major: 不兼容变更
# - Minor: 新增功能
# - Patch: 修复/优化

# Step 2: 更新 workflow_contract.v1.json
# 编辑 scripts/ci/workflow_contract.v1.json，更新：
#   - "version": "X.Y.Z"  （按 SemVer 升级）
#   - "last_updated": "YYYY-MM-DD"  （当前日期）

# Step 3: 更新 contract.md 版本控制表（第 14 章）
# 在表格顶部添加新行：
#   | vX.Y.Z | YYYY-MM-DD | <变更说明> |

# Step 4: 验证修复
make check-workflow-contract-version-policy
make check-workflow-contract-docs-sync
make validate-workflows-strict
```

#### 11.5.4 trigger_reasons 字段说明

`--json` 输出中的 `trigger_reasons` 字段说明每个关键文件触发检查的原因：

```json
{
  "trigger_reasons": {
    ".github/workflows/ci.yml": "Phase 1 workflow 文件（ci.yml/nightly.yml）",
    "scripts/ci/validate_workflows.py": "合约校验器核心脚本",
    "Makefile": "Makefile CI/workflow 相关目标变更"
  }
}
```

**规则类别说明**：

| 类别 | 描述 | 示例文件 |
|------|------|----------|
| `workflow_core` | Phase 1 workflow 文件 | `ci.yml`, `nightly.yml` |
| `contract_definition` | 合约定义和文档 | `workflow_contract.v1.json`, `contract.md` |
| `tooling` | 工具脚本（影响合约执行） | `validate_workflows.py`, `*.schema.json` |
| `special` | 特殊规则（如 Makefile CI 相关） | `Makefile`（仅 CI 相关变更） |

> **注意**：Phase 1 仅覆盖 `ci.yml` 和 `nightly.yml`。扩展支持其他 workflow 文件时，需修改 `check_workflow_contract_version_policy.py` 中的 `CRITICAL_WORKFLOW_RULES`。

---

## 12. SSOT & 同步矩阵

本章定义 workflow 合约的唯一真实来源（SSOT）及各类变更的同步更新要求。

> **快速变更流程**：标准变更顺序和最小验证矩阵请参见 [maintenance.md 第 0 章](maintenance.md#0-快速变更流程ssot-first)。该流程明确了 SSOT-first 原则：先改 `workflow_contract.v1.json` → 同步 workflow YAML → 同步文档 → 必要时改 Makefile → 最后补测试。

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
| `step_name_aliases` | `workflow_contract.v1.json` | Step 名称别名映射（canonical → aliases） | `validate_workflows.py` |

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

#### 12.4.7 Schema 字段变更 Checklist

当需要在 `workflow_contract.v1.schema.json` 中新增字段（如 `step_name_aliases`、`job_timeout_minutes` 等）时，按以下顺序操作：

**Step 1: 更新 Schema 文件**
```bash
# 文件：scripts/ci/workflow_contract.v1.schema.json
# 操作：
1. 在 definitions 或 properties 中添加新字段定义
2. 指定 type、description、pattern（如适用）
3. 如为可选字段，不添加到 required 数组
4. 如为必需字段，添加到对应 required 数组（Breaking Change！）

# 示例：添加 step_name_aliases 字段
"step_name_aliases": {
  "type": "object",
  "additionalProperties": {
    "type": "array",
    "items": { "type": "string" }
  },
  "description": "Map of canonical step name to allowed aliases. Version policy: Adding this field is Minor; removing is Major."
}
```

**Step 2: 更新 Validator 脚本**
```bash
# 文件：scripts/ci/validate_workflows.py
# 操作：
1. 读取新字段（如有）并添加校验逻辑
2. 新增对应的 error_type（如需要）
3. 添加单元测试覆盖新逻辑

# 对应测试文件：
- tests/ci/test_validate_workflows*.py
```

**Step 3: 更新文档同步规则**
```bash
# 文件：scripts/ci/check_workflow_contract_docs_sync.py
# 操作：
1. 如新字段需要同步到 contract.md，添加同步检查规则
2. 更新 SYNC_FIELDS 或对应的字段映射

# 验证：
make check-workflow-contract-docs-sync
```

**Step 4: 更新 Contract 文档**
```bash
# 文件：docs/ci_nightly_workflow_refactor/contract.md
# 操作：
1. 在对应章节描述新字段的语义和用法
2. 更新第 12.1 节 SSOT 定义表（如适用）
3. 更新第 14 章版本控制表
```

**Step 5: 更新版本号**
```bash
# 文件：scripts/ci/workflow_contract.v1.json
# 操作：
1. version 字段升级（按下表规则）
2. last_updated 更新为当前日期
```

**Schema 变更版本位规则：**

| 变更类型 | 版本位 | 示例 |
|----------|--------|------|
| **新增可选字段** | Minor (0.X.0) | 添加 `step_name_aliases`（可选） |
| **新增必需字段** | Major (X.0.0) | 添加 `required_outputs`（必需） |
| **删除字段** | Major (X.0.0) | 移除 `deprecated_field` |
| **收紧约束** | Major (X.0.0) | `minItems: 0` → `minItems: 1` |
| **放宽约束** | Minor (0.X.0) | `minItems: 1` → `minItems: 0` |
| **仅改描述/注释** | Patch (0.0.X) | 更新 `description` 文本 |
| **添加注释字段 (`^_`)** | Patch (0.0.X) | 添加 `_note_usage` |

**验证命令：**
```bash
# 完整验证流程
make check-schemas                            # Schema 语法校验
make validate-workflows-strict                # 合约校验
make check-workflow-contract-docs-sync        # 文档同步检查
make check-workflow-contract-version-policy   # 版本策略检查
```

> **注意**：Schema 变更属于"基础设施变更"，建议在独立 PR 中完成，便于 review 和回滚。

### 12.5 版本更新触发条件

| 变更类型 | 版本位 | 触发条件示例 |
|----------|--------|--------------|
| **Major (X.0.0)** | Breaking Change | 删除 job/step、重命名 output key、删除 artifact 路径 |
| **Minor (0.X.0)** | Feature Addition | 新增 job/step、新增 frozen name、新增 output key |
| **Patch (0.0.X)** | Fix / Docs Only | 文档完善、描述修正、注释更新 |

> **详细版本策略**：参见第 11 章 [SemVer Policy / 版本策略](#11-semver-policy--版本策略)

---

## 13. Error Type 体系与版本策略

本章罗列所有校验脚本的 `error_type`、`warning_type` 和 drift 报告的 `drift_type` 定义，便于维护和测试覆盖。

### 13.1 validate_workflows.py 错误类型

#### 13.1.1 ValidationError.error_type 列表

| error_type | 说明 | 严重程度 | `--strict` 行为 |
|------------|------|----------|-----------------|
| `contract_not_found` | 合约文件未找到 | ERROR | 阻断 |
| `contract_parse_error` | 合约 JSON 解析失败 | ERROR | 阻断 |
| `schema_parse_error` | Schema 文件解析失败 | ERROR | 阻断 |
| `schema_error` | Schema 校验失败 | ERROR | 阻断 |
| `workflow_not_found` | Workflow 文件未找到 | ERROR | 阻断 |
| `workflow_parse_error` | Workflow YAML 解析失败 | ERROR | 阻断 |
| `makefile_not_found` | Makefile 不存在 | ERROR | 阻断 |
| `missing_job` | 必需 Job 不存在 | ERROR | 阻断 |
| `missing_job_id` | Job ID 在 workflow 中不存在 | ERROR | 阻断 |
| `extra_job_not_in_contract` | workflow 中存在但 contract 未声明的 job | WARNING/ERROR | `--require-job-coverage` 时为 ERROR |
| `frozen_job_name_changed` | 冻结的 Job Name 被改名 | ERROR | 阻断 |
| `missing_step` | 必需 Step 不存在 | ERROR | 阻断 |
| `frozen_step_name_changed` | 冻结的 Step Name 被改名 | ERROR | 阻断 |
| `missing_output` | 必需 Output 不存在 | ERROR | 阻断 |
| `missing_env_var` | 必需环境变量不存在 | ERROR | 阻断 |
| `missing_artifact_path` | 必需 Artifact 路径缺失 | ERROR | 阻断 |
| `missing_makefile_target` | 必需 Makefile Target 缺失 | ERROR | 阻断 |
| `undeclared_make_target` | workflow 调用但 contract 未声明的 make target | ERROR | 阻断 |
| `label_missing_in_script` | Label 在 contract 中定义但脚本中缺失 | ERROR | 阻断 |
| `label_missing_in_contract` | Label 在脚本中定义但 contract 中缺失 | ERROR | 阻断 |
| `contract_job_ids_names_length_mismatch` | job_ids/job_names 数组长度不一致 | ERROR | 阻断 |
| `contract_job_ids_duplicate` | job_ids 中存在重复项 | ERROR | 阻断 |
| `contract_required_job_id_duplicate` | required_jobs 中 id 重复 | ERROR | 阻断 |
| `contract_required_job_not_in_job_ids` | required_jobs 的 id 不在 job_ids 中 | ERROR | 阻断 |
| `contract_frozen_step_missing` | required_steps 不在 frozen allowlist 中 | ERROR | `--require-frozen-consistency` 时报告 |
| `contract_frozen_job_missing` | job_names 不在 frozen allowlist 中 | ERROR | `--require-frozen-consistency` 时报告 |
| `unfrozen_required_step` | required step 未冻结 | WARNING/ERROR | `--require-frozen-consistency` 时为 ERROR |
| `unfrozen_required_job` | required job 未冻结 | WARNING/ERROR | `--require-frozen-consistency` 时为 ERROR |

#### 13.1.2 ValidationWarning.warning_type 列表

| warning_type | 说明 | `--strict` 行为 |
|--------------|------|-----------------|
| `schema_skip` | jsonschema 库未安装，跳过 Schema 校验 | 不提升 |
| `job_name_changed` | Job Name 变更（非冻结） | 提升为 ERROR |
| `job_name_mismatch` | Job Name 与 contract 定义不匹配 | 提升为 ERROR |
| `extra_job_not_in_contract` | workflow 中存在但 contract 未声明的 job | 提升为 ERROR（`--require-job-coverage`） |
| `step_name_changed` | Step Name 变更（非冻结） | 提升为 ERROR |
| `step_name_alias_matched` | required step 通过 alias 映射匹配到实际步骤 | 保持 WARNING |
| `unfrozen_required_step` | required step 未冻结（非 strict 模式） | 保持 WARNING |
| `unfrozen_required_job` | required job 未冻结（非 strict 模式） | 保持 WARNING |
| `label_script_parse_warning` | Label 脚本解析警告 | 保持 WARNING |

### 13.2 check_workflow_contract_docs_sync.py 错误类型

| error_type | 说明 | 分类 |
|------------|------|------|
| `contract_not_found` | 合约文件未找到 | file |
| `contract_parse_error` | 合约 JSON 解析失败 | file |
| `doc_not_found` | 文档文件未找到 | file |
| `doc_read_error` | 文档读取失败 | file |
| `workflow_section_missing` | 文档中缺少 workflow 章节 | section |
| `frozen_step_section_missing` | 冻结 Step 章节缺失 | section |
| `frozen_job_names_section_missing` | Frozen Job Names 章节缺失 | section |
| `labels_section_missing` | PR Labels 章节缺失 | section |
| `make_targets_section_missing` | Make Targets 章节缺失 | section |
| `semver_policy_section_missing` | SemVer Policy 章节缺失 | section |
| `job_id_not_in_doc` | Job ID 在文档中未找到 | content |
| `job_name_not_in_doc` | Job Name 在文档中未找到 | content |
| `frozen_step_not_in_doc` | Frozen Step 在文档中未找到 | content |
| `frozen_job_name_not_in_doc` | Frozen Job Name 在文档中未找到 | content |
| `label_not_in_doc` | Label 在文档中未找到 | content |
| `version_not_in_doc` | 版本号在文档中未找到 | content |
| `make_target_not_in_doc` | Make Target 在文档中未找到 | content |

### 13.3 check_workflow_contract_version_policy.py 错误类型

| error_type | 说明 | Exit Code |
|------------|------|-----------|
| `contract_not_found` | 合约文件未找到 | 2 |
| `contract_parse_error` | 合约 JSON 解析失败 | 2 |
| `doc_not_found` | 文档文件未找到 | 2 |
| `doc_read_error` | 文档读取失败 | 2 |
| `version_not_updated` | 关键文件变更但 version 未更新 | 1 |
| `last_updated_not_updated` | 关键文件变更但 last_updated 未更新 | 1 |
| `version_not_in_doc` | 版本号不在文档版本控制表中 | 1 |

### 13.4 workflow_contract_drift_report.py 漂移类型

#### 13.4.1 drift_type 列表

| drift_type | 说明 |
|------------|------|
| `added` | 实际存在但合约未声明 |
| `removed` | 合约声明但实际不存在 |
| `changed` | 存在但值/名称不同 |

#### 13.4.2 category 列表

| category | 说明 | `removed` 严重程度 | `added` 严重程度 |
|----------|------|-------------------|------------------|
| `workflow` | Workflow 文件级别 | ERROR | - |
| `job_id` | Job ID | ERROR | WARNING |
| `job_name` | Job Name | - | - |
| `step` | Step Name | ERROR | - |
| `env_var` | 环境变量 | ERROR | - |
| `artifact_path` | Artifact 路径 | ERROR | INFO |
| `make_target` | Makefile Target | ERROR | WARNING |
| `label` | PR Label | ERROR | WARNING |

#### 13.4.3 severity 列表

| severity | 说明 | 使用场景 |
|----------|------|----------|
| `info` | 信息性提示 | 新增了合约未要求的项（如 artifact path） |
| `warning` | 警告 | 名称变更、新增未声明的 job/target |
| `error` | 错误 | 必需项缺失 |

### 13.5 Error Type 版本策略

#### 13.5.1 新增 error_type 流程

新增 error_type 属于 **Minor** 版本变更（0.X.0）：

1. 在对应脚本的 `*ErrorTypes` 类中添加常量定义
2. 在对应的 `*_ERROR_TYPES` 集合中添加该常量
3. 更新本文档第 13 章对应的表格
4. 更新 `workflow_contract.v1.json` 的 `version` 字段（Minor 升级）
5. 在测试文件中添加覆盖测试

**示例：新增 `missing_label` 错误类型**

```python
# scripts/ci/validate_workflows.py
class ErrorTypes:
    # ...
    MISSING_LABEL = "missing_label"  # 新增
```

#### 13.5.2 弃用 error_type 流程

弃用 error_type 属于 **Major** 版本变更（X.0.0），需要提供迁移路径：

1. **标记阶段**（Minor 版本）：
   - 在代码中添加 `# DEPRECATED: v3.0.0 移除，替换为 xxx` 注释
   - 在文档中标记 `（已弃用，将在 vX.0.0 移除）`
   - 保持向后兼容性

2. **移除阶段**（Major 版本）：
   - 从 `*ErrorTypes` 类中移除常量
   - 从 `*_ERROR_TYPES` 集合中移除
   - 更新文档移除相关条目
   - 更新 `version` 字段（Major 升级）

3. **迁移指南**：
   - 在版本控制表中记录替换关系
   - 提供代码迁移示例

#### 13.5.3 修改 error_type 含义

修改 error_type 含义属于 **Major** 版本变更（X.0.0）：

- 如果语义变化影响调用方的判断逻辑，必须升级 Major 版本
- 建议新增新的 error_type 而非修改现有的
- 在文档中明确记录语义变化

### 13.6 常量定义位置索引

| 脚本文件 | 常量类 | 集合常量 |
|----------|--------|----------|
| `validate_workflows.py` | `ErrorTypes`, `WarningTypes` | `CRITICAL_ERROR_TYPES`, `STRICT_PROMOTED_WARNING_TYPES` |
| `check_workflow_contract_docs_sync.py` | `DocsSyncErrorTypes` | `DOCS_SYNC_ERROR_TYPES` |
| `check_workflow_contract_version_policy.py` | `VersionPolicyErrorTypes` | `VERSION_POLICY_ERROR_TYPES`, `VERSION_POLICY_FILE_ERROR_TYPES` |
| `workflow_contract_drift_report.py` | `DriftTypes`, `DriftCategories`, `DriftSeverities` | `DRIFT_TYPES`, `DRIFT_CATEGORIES`, `DRIFT_SEVERITIES`, `DRIFT_SEVERITY_MAP` |

---

## 14. 版本控制

| 版本 | 日期 | 变更说明 |
|------|------|----------|
| v2.22.1 | 2026-02-02 | docs: enable controlled blocks for contract.md/coupling_map.md |
| v2.22.0 | 2026-02-02 | 新增 ci-test-isolation job 到 CI workflow 合约：CI 测试隔离检查；新增 make.targets_required: 'check-ci-test-isolation' |
| v2.21.0 | 2026-02-02 | 新增 workflow make targets 一致性检查：workflow-contract job required_steps 添加 'Check workflow make targets consistency'；make.targets_required 添加 'check-workflow-make-targets-consistency' |
| v2.20.0 | 2026-02-02 | 新增 workflow-contract job required_steps：'Check workflow contract internal consistency'；新增 make.targets_required：'check-workflow-contract-internal-consistency' |
| v2.19.0 | 2026-02-02 | 更新 lint job required_steps：将 'Run mypy (type check)' 重命名为 'Run mypy (baseline mode)'；新增 'Run mypy (strict-island mode)' 步骤（双层 mypy 检查策略） |
| v2.18.0 | 2026-02-02 | 新增 `step_name_aliases` 可选字段：支持为 required_steps 定义别名映射；当 step 通过 alias 匹配时报告 `step_name_alias_matched` WARNING 而非 ERROR；新增 contract.md 5.6 节文档 |
| v2.17.1 | 2026-02-02 | 将 drift report 产物（artifacts/workflow_contract_drift.json, .md）从 required_artifact_paths 移除，改为 Optional 分级；CI upload-artifact 添加 if-no-files-found: ignore；更新 contract.md 8.7/8.10 节明确策略边界 |
| v2.17.0 | 2026-02-02 | 补全 CI workflow 中缺失的 required_steps：gateway-public-api-surface 添加 'Check Gateway Public API docs sync'；workflow-contract 添加 'Check workflow contract doc anchors'；make.targets_required 添加 'check-workflow-contract-doc-anchors' |
| v2.16.0 | 2026-02-02 | 新增第 13 章"Error Type 体系与版本策略"：罗列 validate_workflows.py 全部 error_type/warning_type；罗列 docs sync/version policy/drift report 的 error_type 体系；规定新增/弃用 error_type 的版本策略；各脚本 error_type 收敛为常量集合 |
| v2.15.0 | 2026-02-02 | 重构版本策略检查脚本：统一 critical file 规则定义（CRITICAL_*_RULES）；新增 trigger_reasons 字段支持；新增 11.5 节"版本策略失败时的最小修复步骤" |
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
