# CI/Nightly/Release Workflow Contract

> 本文档固化 workflow 的关键标识符、环境变量、标签语义等，作为"禁止回归"的基准。
> 任何修改需经过 review 并更新本文档。

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

| Job ID | Job Name | 层级 | 触发条件 |
|--------|----------|------|----------|
| `detect-changes` | Detect Changes | - | 始终执行 |
| `precheck-static` | [Fast] Precheck & Static Build Verify | Fast | 始终执行 |
| `workflow-contract-check` | [Fast] Workflow Contract Check | Fast | contract_changed |
| `schema-validate` | [Fast] Schema Validation | Fast | schemas/logbook/gateway 任一变更 |
| `docs-check` | [Fast] Docs Link Check | Fast | docs_changed 或 scripts_changed |
| `python-logbook-unit` | [Fast] Logbook Unit Tests | Fast | logbook_changed |
| `python-gateway-unit` | [Fast] Gateway Unit Tests | Fast | gateway_changed |
| `openmemory-governance-check` | [Fast] OpenMemory Governance Check | Fast | openmemory_governance_changed |
| `unified-standard` | [Standard] Unified Stack Integration Test | Standard | stack/logbook/gateway/openmemory_governance/upstream_ref 任一变更 |
| `openmemory-sdk` | [Fast] OpenMemory SDK Tests | Fast | openmemory_sdk_changed |

### 2.2 Nightly Workflow (`nightly.yml`)

| Job ID | Job Name | 说明 |
|--------|----------|------|
| `nightly-full` | Nightly Full Test Suite | Full 层完整测试套件 |

### 2.3 Release Workflow (`release.yml`)

| Job ID | Job Name | 说明 |
|--------|----------|------|
| `gate` | Release Gate Check | 发布门禁检查 |
| `build` | Build & Push Images | 构建并推送镜像 |
| `summary` | Release Summary | 发布摘要 |

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

### 4.3 Release Gate

```yaml
env:
  VERIFY_FULL: "1"
  RUN_INTEGRATION_TESTS: "1"
  HTTP_ONLY_MODE: "1"
```

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

### 5.1 Job Name 层级

以下 Job Name 格式为"禁止回归"基准：

| 前缀 | 含义 | 示例 |
|------|------|------|
| `[Fast]` | Fast 层 job，PR 必跑或条件跑 | `[Fast] Precheck & Static Build Verify` |
| `[Standard]` | Standard 层 job，PR 条件跑（需变更检测） | `[Standard] Unified Stack Integration Test` |

### 5.2 关键 Step Name

以下 Step Name 为"禁止回归"基准（不允许随意修改）。这些 step name 在 `workflow_contract.v1.json` 的 `frozen_step_text.allowlist` 中定义。

**冻结 step 验证规则：**
- `validate_workflows.py` 会检查所有 `required_steps` 中的 step name
- 如果冻结的 step name 被改名（即使是微小变化），会报告 **ERROR** (`frozen_step_name_changed`)
- 非冻结的 step name 改名只会报告 **WARNING** (`step_name_changed`)
- 错误信息会提示："此 step 属于冻结文案，不能改名；如确需改名需同步更新 contract+docs"

**CI Workflow:**
- `Run CI precheck`
- `Verify build static (Dockerfile/compose config check)`
- `Check legacy step naming`
- `Check deprecated env var usage`
- `Verify OpenMemory vendor structure`
- `Verify OpenMemory.upstream.lock.json format`
- `Check OpenMemory freeze status`
- `Generate OpenMemory patch bundle (strict mode)`
- `Run OpenMemory sync check`
- `Run OpenMemory sync verify`
- `Run lock consistency check (hard gate when upstream_ref changed)`
- `Verify upstream_ref change requirements`

**Nightly Workflow:**
- `Deploy unified stack`
- `Verify unified stack`
- `Run OpenMemory release preflight (optional aggregated check)`
- `Run OpenMemory upstream drift check`
- `Run Artifact Audit`
- `Generate Summary`

**Release Workflow:**
- `Extract version from tag`
- `Run release gate checks` (封装 verify-build + deploy + verify-unified FULL + gateway tests)
- `Generate Release Summary`

### 5.3 Summary 标题/关键提示语

以下 Summary 标题为"禁止回归"基准：

| Summary 标题 | 出现场景 |
|--------------|----------|
| `## Nightly Build Summary` | nightly.yml Generate Summary step |
| `## 🚀 Release Summary` | release.yml Generate Release Summary step |
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

### 7.1 CI/Nightly/Release 聚合目标

| Make Target | 用途 | 封装内容 |
|-------------|------|----------|
| `ci-precheck` | CI 预检 | 数据库配置验证 |
| `ci-unified-standard` | CI Standard 层聚合 | deploy + verify-unified + openmemory-audit + test-gateway-integration |
| `nightly-full-suite` | Nightly Full 层聚合 | vendor-check + lock-format + deploy + verify-full + 全部测试 |
| `release-gate` | Release Gate 聚合 | verify-build-static + verify-build + deploy + verify-unified FULL + test-gateway-integration |

### 7.2 Release 相关 Make Targets

| Make Target | 说明 |
|-------------|------|
| `release-gate` | Release 门禁检查聚合目标 |
| `verify-build-static` | Docker 构建静态检查（Dockerfile/compose 配置校验） |
| `verify-build` | Docker 实际构建验证 |
| `deploy` | 完整部署（预检 + 启动所有服务） |
| `verify-unified` | 统一栈验证（支持 VERIFY_FULL=1 模式） |
| `test-gateway-integration` | Gateway 集成测试 |

---

## 8. Artifact Archive 合约

### 8.1 概述

`artifact_archive` 合约定义了 workflow 中必须上传的 artifact 路径，确保关键验收测试结果和验证报告被正确上传到 CI artifacts。

### 8.2 合约字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `required_artifact_paths` | `string[]` | 必需上传的 artifact 路径列表（支持目录和文件路径） |
| `artifact_step_names` | `string[]` | 可选：限制校验范围到指定名称的步骤 |

### 8.3 CI Workflow Artifact 要求

```json
"artifact_archive": {
  "required_artifact_paths": [
    ".artifacts/acceptance-runs/",
    ".artifacts/verify-results.json"
  ],
  "artifact_step_names": [
    "Upload verification results",
    "Upload acceptance run records"
  ]
}
```

### 8.4 Nightly Workflow Artifact 要求

```json
"artifact_archive": {
  "required_artifact_paths": [
    ".artifacts/acceptance-unified-full/",
    ".artifacts/acceptance-runs/",
    ".artifacts/verify-results.json"
  ],
  "artifact_step_names": [
    "Upload acceptance-unified-full results",
    "Upload verification results"
  ]
}
```

### 8.5 验证规则

`validate_workflows.py` 执行以下检查：

1. **扫描 upload-artifact 步骤**：解析 workflow 中所有 `uses: actions/upload-artifact@v*` 步骤
2. **提取 path 配置**：支持单行和多行 `with.path` 配置
3. **覆盖检查**：验证 `required_artifact_paths` 中的每个路径都被某个 upload 步骤覆盖
4. **步骤过滤**：如果定义了 `artifact_step_names`，仅检查名称匹配的步骤

### 8.6 错误示例

```
[missing_artifact_path] ci:.github/workflows/ci.yml
  Key: .artifacts/acceptance-runs/
  Message: Required artifact path '.artifacts/acceptance-runs/' is not uploaded in workflow. 
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

## 10. 版本控制

| 版本 | 日期 | 变更说明 |
|------|------|----------|
| v2.0 | 2026-01-31 | 移除 SeekDB 组件：删除 SeekDB 环境变量、命名迁移验收矩阵、相关 Job/Step 定义 |
| v1.12 | 2026-01-30 | 新增 Acceptance 验收测试合约：定义 CI 组合式覆盖 vs Nightly 直接执行的合约、产物要求、record_acceptance_run.py 调用规范 |
| v1.11 | 2026-01-30 | 新增 Artifact Archive 合约：定义 ci/nightly 必需的 artifact paths；validate_workflows.py 新增 upload-artifact 步骤扫描验证 |
| v1.10 | 2026-01-30 | 新增 Labels 一致性校验：`validate_workflows.py` 自动校验 `ci.labels` 与 `gh_pr_labels_to_outputs.py` 中 `LABEL_*` 常量的一致性 |
| v1.9 | 2026-01-30 | 新增 `contract_changed` 输出键：Makefile 和 `docs/ci_nightly_workflow_refactor/**` 变更触发 workflow-contract-check；新增 `docs-check` job 定义 |
| v1.8 | 2026-01-30 | 冻结 step 验证强化：`frozen_step_text.allowlist` 中的 step 改名现为 ERROR（非 WARNING），阻止 CI 通过 |
| v1.3 | 2026-01-30 | upstream_ref 变更要求：新增补丁产物要求，CI 先生成补丁再校验流程 |
| v1.2 | 2026-01-30 | Release Gate 封装：新增 `release-gate` 聚合目标，合并多个独立 make 调用为单一步骤 |
| v1.0 | 2026-01-30 | 初始版本，固化 CI/Nightly/Release 合约 |
