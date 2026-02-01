# CI/Nightly Workflow Contract

> 本文档固化 workflow 的关键标识符、环境变量、标签语义等，作为"禁止回归"的基准。
> 任何修改需经过 review 并更新本文档。

> **Phase 1 范围说明**: 当前合约版本 (v2.0.0) 仅覆盖 CI 和 Nightly workflow。Release workflow (`release.yml`) 将在 Phase 2 引入。

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
| `iteration-docs-check` | Iteration Docs Check | 迭代文档检查（.iteration/ 链接 + SUPERSEDED 一致性） |
| `workflow-contract` | Workflow Contract Validation | Workflow 合约校验和文档同步检查 |

### 2.2 Nightly Workflow (`nightly.yml`)

| Job ID | Job Name | 说明 |
|--------|----------|------|
| `unified-stack-full` | Unified Stack Full Verification | 完整统一栈验证（Docker Compose + Gate Contract + 集成测试） |
| `notify-results` | Notify Results | Nightly 汇总通知 |

### 2.3 Release Workflow (`release.yml`) - Phase 2 预留

> **注意**: Release workflow 将在 Phase 2 引入，当前合约版本不包含此部分。

---

## 3. PR Label 列表与语义

| Label | 语义 | 使用场景 |
|-------|------|----------|
| `openmemory:freeze-override` | 绕过 OpenMemory 升级冻结 | 冻结期间的紧急修复（需配合 Override Reason） |

> **Labels 一致性校验**: `validate_workflows.py` 会自动校验 `ci.labels` 与 `gh_pr_labels_to_outputs.py` 中 `LABEL_*` 常量的一致性。若不一致会报 ERROR 并提示同步更新脚本/contract/docs。

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

**仅冻结核心步骤（共 12 个）：**

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
    "artifacts/workflow_contract_validation.json"
  ],
  "artifact_step_names": [
    "Upload test results",
    "Upload migration logs",
    "Upload validation results",
    "Upload validation report"
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

## 10. SemVer Policy / 版本策略

本节定义 workflow contract 文件（`workflow_contract.v1.json`）、workflow 文件（`.github/workflows/*.yml`）及相关文档的版本变更规则。

### 10.1 版本变更分类

| 变更类型 | 版本位 | 示例 |
|----------|--------|------|
| **Breaking Change**（不兼容变更） | Major (X.0.0) | 删除必需的 job/step、修改 output key 名称、修改 artifact 路径 |
| **Feature Addition**（功能新增） | Minor (0.X.0) | 新增 job、新增 output key、新增 frozen step |
| **Fix / Docs Only**（修复/仅文档） | Patch (0.0.X) | 修复错误、文档完善、注释更新 |

### 10.2 版本策略规则

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

### 10.3 版本更新流程

```bash
# 1. 更新 workflow_contract.v1.json 中的 version 字段
# 2. 更新 contract.md 第 11 章版本控制表
# 3. 运行 make validate-workflows 验证一致性
# 4. 运行 make check-workflow-contract-docs-sync 验证文档同步
```

### 10.4 向后兼容性承诺

- **Frozen Step Names**：在 `frozen_step_text.allowlist` 中的 step name 不得随意变更
- **Required Artifact Paths**：`artifact_archive.required_artifact_paths` 中的路径不得随意删除
- **Output Keys**：`detect_changes.outputs` 中的 key 不得随意删除或重命名

---

## 11. 版本控制

| 版本 | 日期 | 变更说明 |
|------|------|----------|
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
