# CI/Nightly Workflow 维护指南

> 本文档提供 CI/Nightly workflow 变更时的同步更新 checklist，确保合约文件、文档与实际 workflow 保持一致。

---

## 1. 变更同步 Checklist

### 1.1 路径过滤（当前已简化）

> **注意**: 当前 CI workflow (v2.x) 已移除 `detect-changes` job，所有检查任务始终执行。
> 路径过滤功能已在 Phase 1 重构中简化移除。如需恢复路径过滤，可参考以下步骤。

如需新增文件变更检测（恢复 `detect-changes` job）：

- [ ] **ci.yml**: 新增 `detect-changes` job，使用 `dorny/paths-filter` action
- [ ] **ci.yml**: 在其他 job 中添加 `if` 条件引用 detect-changes 的 outputs
- [ ] **workflow_contract.v1.json**: 在 `ci.detect_changes.outputs` 数组中添加 output key
- [ ] **contract.md**: 在 "1.1 文件变更检测键" 表格中添加新条目

**当前架构说明**：

CI workflow 采用简化模式，所有检查任务无条件执行：
- `test` - 单元测试和验收测试
- `lint` - 代码风格检查
- `schema-validate` - JSON Schema 校验
- `workflow-contract` - Workflow 合约校验
- 其他检查 jobs（参见 contract.md#2-job-id-与-job-name-对照表）

### 1.2 新增 Make 目标

当新增 Makefile 目标且被 workflow 调用时：

- [ ] **Makefile**: 添加新目标及其依赖
- [ ] **workflow_contract.v1.json**: 在 `make.targets_required` 数组中添加新目标名
- [ ] **contract.md**: 如目标涉及新环境变量，更新相关章节
- [ ] 运行 `python scripts/ci/validate_workflows.py` 验证合约一致性

示例：新增 `test-new-component` 目标

```json
// workflow_contract.v1.json
"make": {
  "targets_required": [
    // ... existing targets ...
    "test-new-component"
  ]
}
```

### 1.3 新增 Job

当在 workflow 中新增 job 时：

- [ ] **ci.yml/nightly.yml**: 添加新 job 定义
- [ ] **workflow_contract.v1.json**: 
  - 在对应 workflow 的 `job_ids` 数组中添加 job ID
  - 在 `job_names` 数组中添加 job name
  - 在 `required_jobs` 数组中添加 job 详细定义（包含 `required_steps`）
- [ ] **contract.md**: 在 "2. Job ID 与 Job Name 对照表" 中添加新条目
- [ ] **README.md**: 如有必要，更新说明

**当前 CI Jobs 列表** (v2.x)：
- `test` - 单元测试和验收测试（矩阵：Python 3.10/3.11/3.12）
- `lint` - 代码风格检查（ruff + mypy）
- `schema-validate` - JSON Schema 校验
- `workflow-contract` - Workflow 合约校验
- 其他辅助检查 jobs（详见 contract.md）

**当前 Nightly Jobs 列表**：
- `unified-stack-full` - 完整统一栈验证
- `notify-results` - 结果通知

### 1.4 新增 PR Label

当新增 PR label 触发逻辑时：

- [ ] **scripts/ci/gh_pr_labels_to_outputs.py**: 添加 `LABEL_*` 常量和解析逻辑
- [ ] **workflow_contract.v1.json**: 在 `ci.labels` 数组中添加新 label（**SSOT**）
- [ ] **contract.md**: 在 "3. PR Label 列表与语义" 表格中添加新条目
- [ ] 运行 `python scripts/ci/validate_workflows.py` 验证 label 一致性

> **SSOT 关系**: `scripts/ci/workflow_contract.v1.json` 的 `ci.labels` 是 PR Labels 的唯一真实来源。
> `contract.md` 第 3 节必须与该 JSON 保持同步。详见 [contract.md#3-pr-label-列表与语义](contract.md#3-pr-label-列表与语义)。

**当前支持的 Labels** (v2.x)：
- `openmemory:freeze-override` - 绕过 OpenMemory 升级冻结

> **注意**: v2.0.0 移除了 SeekDB 组件相关的 labels（`ci:dual-read`、`ci:seek-compat-strict`、`ci:seek-migrate-dry-run`）。

**当前 Workflow 消费状态**:
- CI workflow 当前**不消费** PR labels（`gh_pr_labels_to_outputs.py` 未被 ci.yml 调用）
- Labels 仅用于合约定义和一致性校验
- 未来如需启用 label 驱动行为，需在 ci.yml 添加 labels 解析步骤

**Labels 一致性自动校验：**

`validate_workflows.py` 会自动校验 `ci.labels` 与 `gh_pr_labels_to_outputs.py` 中 `LABEL_*` 常量的一致性：
- 若 contract 中有但脚本中没有：报 **ERROR** (`label_missing_in_script`)
- 若脚本中有但 contract 中没有：报 **ERROR** (`label_missing_in_contract`)

### 1.5 新增 workflow_dispatch 输入参数

当新增手动触发输入参数时：

- [ ] **ci.yml/nightly.yml**: 在 `workflow_dispatch.inputs` 中添加新参数
- [ ] **README.md**: 在对应 workflow 的"输入参数"表格中添加新条目
- [ ] **contract.md**: 如参数影响关键行为，更新相关章节

### 1.6 新增/修改 Step Name

当新增或修改 step name 时：

> **策略 A（最小冻结，v2.3.0+）**：仅核心步骤（被外部系统引用如 artifact 名称、日志搜索关键词，或核心验证流程步骤）才需要加入 `frozen_step_text.allowlist`。非核心步骤改名仅产生 WARNING，不阻止 CI。

**新增步骤：**
- [ ] **ci.yml/nightly.yml**: 添加新 step
- [ ] **workflow_contract.v1.json**: 在 `required_jobs[].required_steps` 中添加（如需合约校验）
- [ ] **仅核心步骤**：如果是 artifact 上传、核心测试/验证步骤等，同时添加到 `frozen_step_text.allowlist`
- [ ] **contract.md**: 如添加到冻结列表，在 "5.2 Frozen Step Names" 中更新

**修改冻结步骤名称：**
- [ ] **workflow_contract.v1.json**: 在 `frozen_step_text.allowlist` 中更新（移除旧名称，添加新名称）
- [ ] **workflow_contract.v1.json**: 在 `required_jobs[].required_steps` 中同步更新引用
- [ ] **contract.md**: 在 "5.2 Frozen Step Names" 中更新
- [ ] 运行 `python scripts/ci/validate_workflows.py` 验证

**修改非冻结步骤名称：**
- [ ] **workflow_contract.v1.json**: 在 `required_jobs[].required_steps` 中更新（如有引用）
- [ ] 运行 `python scripts/ci/validate_workflows.py` 验证 - 仅产生 WARNING，不阻止 CI

### 1.7 新增/修改 Artifact Upload 路径

当新增关键的 artifact upload 步骤时：

- [ ] **ci.yml/nightly.yml**: 添加 `uses: actions/upload-artifact@v4` 步骤，确保 `with.path` 包含必需路径
- [ ] **workflow_contract.v1.json**: 在对应 workflow 的 `artifact_archive.required_artifact_paths` 中添加新路径
- [ ] **workflow_contract.v1.json**: 可选：在 `artifact_archive.artifact_step_names` 中添加步骤名称（用于限定检查范围）
- [ ] **contract.md**: 在 "8. Artifact Archive 合约" 中更新说明
- [ ] 运行 `python scripts/ci/validate_workflows.py` 验证 artifact 路径覆盖

### 1.8 修改 Acceptance 验收测试

当修改 acceptance 验收测试的步骤、产物或执行方式时：

**CI 测试变更** (v2.x)：
- [ ] **ci.yml**: 修改 `test` job 中的测试步骤
- [ ] **ci.yml**: 确保 artifact 上传步骤包含新的测试结果文件
- [ ] **contract.md**: 更新 'Acceptance 验收测试合约' 节 (contract.md#9-acceptance-验收测试合约)
- [ ] **docs/acceptance/00_acceptance_matrix.md**: 更新 "CI 覆盖步骤" 表

**Nightly 验证变更**：
- [ ] **nightly.yml**: 修改 `unified-stack-full` job 中的验证步骤
- [ ] **nightly.yml**: 同步更新环境变量传递（GATE_PROFILE、SKIP_DEGRADATION_TEST 等）
- [ ] **contract.md**: 更新 'Acceptance 验收测试合约' 节中的 Nightly 直接执行合约

**当前 CI test job 关键步骤**：
```yaml
- name: Run database migrations        # 数据库迁移
- name: Verify database migrations     # 迁移验证（严格模式）
- name: Run unit and integration tests # 单元测试
- name: Run acceptance tests           # 验收测试
- name: Upload test results           # 上传测试结果
- name: Upload migration logs         # 上传迁移日志
```

**当前 Nightly unified-stack-full job 关键步骤**：
```yaml
- name: Detect environment capabilities           # 环境能力检测
- name: Validate gate contract (full profile)     # Gate Contract 校验
- name: Start unified stack with Docker Compose   # 启动统一栈
- name: Run Gateway integration tests             # 集成测试
- name: Run unified stack verification (full)     # 完整验证
- name: Record acceptance run                     # 记录 acceptance run
- name: Upload test results                       # 上传结果
```

**产物变更**:
- [ ] 更新 `workflow_contract.v1.json` 的 `artifact_archive.required_artifact_paths`
- [ ] 更新 `docs/acceptance/00_acceptance_matrix.md` 的产物记录与追溯表
- [ ] 运行 `python scripts/ci/validate_workflows.py` 验证 artifact 路径覆盖

**artifact_archive 配置示例** (v2.x)：

```json
"artifact_archive": {
  "_comment": "CI workflow 必需的 artifact 路径",
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

**路径匹配规则：**
- 通配符匹配：`test-results-*.xml` 匹配 `test-results-3.11.xml`
- 目录匹配：`artifacts/` 开头的路径匹配上传路径中以此开头的任何路径
- 多行 path 支持：upload-artifact 的 `with.path` 可以是多行 YAML 字符串

---

## 2. 合约文件说明

### 2.1 workflow_contract.v1.json

位置: `scripts/ci/workflow_contract.v1.json`

用途: 定义 workflow 的结构性合约，供 `validate_workflows.py` 自动校验

关键字段 (v2.x)：
| 字段 | 说明 |
|------|------|
| `ci.job_ids` / `ci.job_names` | Job ID 与 name 列表 |
| `ci.required_jobs` | 每个 job 的必需 steps |
| `ci.labels` | 支持的 PR labels |
| `ci.artifact_archive` | Artifact 上传合约（required_artifact_paths, artifact_step_names） |
| `nightly.job_ids` / `nightly.job_names` | Nightly Job ID 与 name 列表 |
| `nightly.required_jobs` | Nightly 每个 job 的必需 steps |
| `nightly.required_env_vars` | Nightly 必需的环境变量 |
| `make.targets_required` | workflow 依赖的 Makefile 目标 |
| `frozen_job_names.allowlist` | 禁止修改的 job name 列表（Required Checks 引用） |
| `frozen_step_text.allowlist` | 禁止修改的 step name 列表 |

### 2.2 contract.md

位置: `docs/ci_nightly_workflow_refactor/contract.md`

用途: 人类可读的合约文档，作为"禁止回归"的基准

更新原则:
- 任何 workflow 结构性变更都需同步更新
- 版本控制章节记录变更历史

---

## 3. 验证流程

### 3.1 本地验证

```bash
# 1. 安装依赖
pip install pyyaml jsonschema pytest

# 2. 运行合约校验（普通模式：frozen steps 违规报 ERROR，非冻结项改名仅 WARNING）
python scripts/ci/validate_workflows.py

# 3. 运行文档同步检查
python scripts/ci/check_workflow_contract_docs_sync.py

# 4. JSON 输出格式（用于脚本处理）
python scripts/ci/validate_workflows.py --json
python scripts/ci/check_workflow_contract_docs_sync.py --json

# 5. 运行 CI 脚本测试（确保校验脚本本身正常）
pytest tests/ci/ -q

# 6. 检查输出，确保无 ERROR
```

> **注意**: CI workflow 中直接调用 Python 脚本而非 Makefile 目标。本地验证时也推荐使用相同方式。

**参数说明与 CI 使用情况：**

| 参数 | 说明 | CI 是否启用 |
|------|------|-------------|
| `--strict` | 将 WARNING 也视为 ERROR；同时启用 `--require-job-coverage` | **是**（`validate-workflows-strict`） |
| `--require-job-coverage` | 要求所有 workflow jobs 都在 contract 中声明，否则报 ERROR | **是**（由 `--strict` 隐式启用） |
| `--check-frozen-consistency` | 检查所有 required_steps/job_names 是否在 frozen allowlist 中（WARNING 级别） | **否** |
| `--require-frozen-consistency` | 同上但作为 ERROR（策略 B：全量冻结模式） | **否** |

> **当前策略（v2.3.0+）**：CI 使用 `--strict` 模式（通过 `make validate-workflows-strict`），具体行为：
> - 冻结项（`frozen_step_text.allowlist` / `frozen_job_names.allowlist`）改名报 **ERROR**
> - 非冻结项改名的 WARNING 也被提升为 **ERROR**
> - Extra jobs（workflow 中存在但 contract 未声明）报 **ERROR**
> - 不启用 `--require-frozen-consistency`（允许 required_steps 不全在 frozen allowlist 中）

### 3.1.1 为什么 CI 选择 `--strict` 模式

**选择 `--strict` 的理由：**

1. **防止 workflow 与 contract 漂移**：严格模式确保所有 workflow 变更都被 contract 记录和追踪，避免隐性变更导致的回归风险。

2. **Extra job 覆盖**：`--strict` 隐式启用 `--require-job-coverage`，确保所有 jobs 都在合约管理下，防止遗漏。

3. **早期发现问题**：将 WARNING 提升为 ERROR，强制开发者在 PR 阶段解决潜在问题，而非累积技术债。

4. **简化合约维护**：统一的严格策略减少了"应该警告还是报错"的歧义，降低维护成本。

**为什么不启用 `--require-frozen-consistency`：**

- 策略 A（最小冻结）只冻结被外部系统引用的核心 job/step name
- 非核心项允许灵活调整，不强制全部纳入冻结列表
- 这在严格检测与灵活迭代之间取得平衡

### 3.1.2 紧急回滚方案

当 CI 因合约校验失败阻断而需要紧急绕过时，可使用以下回滚方案：

**方案 A：切换到非 strict 模式（推荐用于紧急修复）**

修改 `.github/workflows/ci.yml` 中的 `workflow-contract` job：

```yaml
# 原配置（strict 模式）
- name: Validate workflow contract
  run: make validate-workflows-strict

# 回滚配置（非 strict 模式）
- name: Validate workflow contract
  run: make validate-workflows
```

**行为差异：**
- `validate-workflows-strict`（`--strict`）：WARNING 也报 ERROR，extra jobs 报 ERROR
- `validate-workflows`（无参数）：WARNING 不阻断 CI，extra jobs 仅 WARNING

**方案 B：本地验证绕过 CI**

```bash
# 使用非 strict 模式验证，仅查看问题但不阻断
make validate-workflows

# 确认问题后强制提交，等待后续修复
git commit --no-verify -m "紧急修复: 临时绕过合约校验"
```

**⚠️ 回滚后必须执行的恢复步骤：**

1. **创建跟进 Issue**：记录回滚原因和预期恢复时间
2. **尽快恢复 strict 模式**：修复根本问题后还原 ci.yml
3. **同步更新 contract**：确保 `workflow_contract.v1.json` 与实际 workflow 一致
4. **验证完整性**：运行 `make validate-workflows-strict` 确认修复

**校验输出说明：**

当检测到问题时，输出包含：
- **workflow 文件**: 具体的 `.github/workflows/*.yml` 文件路径
- **job id**: 发生问题的 job 标识符
- **期望 step**: 合约中定义的期望 step name
- **实际命中 step**: 实际在 workflow 中找到的 step name（或 fuzzy match）
- **操作指引**: 如何更新 contract+docs 的具体步骤

**示例输出：**
```
[frozen_step_name_changed] ci:.github/workflows/ci.yml
  Key: Run CI precheck
  Message: Frozen step 'Run CI precheck' was renamed to 'Run precheck' in job 'precheck-static'. 
           此 step 属于冻结文案，不能改名。如确需改名，请执行以下步骤:
           1. 更新 scripts/ci/workflow_contract.v1.json:
              - frozen_step_text.allowlist: 添加新名称，移除旧名称
              - required_jobs[].required_steps: 如有引用，同步更新
           2. 更新 docs/ci_nightly_workflow_refactor/contract.md:
              - 'Frozen Step Names' 节 (contract.md#52-frozen-step-names)
           3. 运行 make validate-workflows 验证
           4. 详见 maintenance.md#62-冻结-step-rename-标准流程
  Location: jobs.precheck-static.steps
  Expected: Run CI precheck
  Actual: Run precheck
```

### 3.2 CI 自动验证

workflow 变更会触发 `Workflow Contract Validation` job，自动执行合约校验。

- **Job ID**: `workflow-contract`
- **Job Name**: `Workflow Contract Validation`
- **触发条件**: 始终执行（所有 push/pull_request/workflow_dispatch）
- **依赖安装**: `pip install pyyaml jsonschema pytest`

**校验步骤** (v2.x)：
1. `Run CI script tests` - 运行 CI 脚本测试 (`pytest tests/ci/ -q`)
2. `Validate workflow contract` - 运行 workflow 合约校验 (`python scripts/ci/validate_workflows.py`)
3. `Check workflow contract docs sync` - 运行文档同步检查 (`python scripts/ci/check_workflow_contract_docs_sync.py`)
4. `Generate validation report (JSON)` - 生成 JSON 格式报告
5. `Upload validation report` - 上传到 artifact

**校验行为：**
- frozen job/step name 改名: 报 ERROR（阻断 CI）
- 非 frozen job/step name 改名: 报 WARNING（不阻断）
- make target 缺失: 报 ERROR
- 文档与合约不同步: 报 ERROR

**产物上传路径**：
- `artifacts/workflow_contract_validation.json` - 合约校验结果
- `artifacts/workflow_contract_docs_sync.json` - 文档同步检查结果

**CI 配置参考** (ci.yml workflow-contract job)：
```yaml
- name: Install dependencies
  run: pip install pyyaml jsonschema pytest

- name: Run CI script tests
  run: pytest tests/ci/ -q --ignore=...

- name: Validate workflow contract
  run: python scripts/ci/validate_workflows.py

- name: Check workflow contract docs sync
  run: python scripts/ci/check_workflow_contract_docs_sync.py

- name: Upload validation report
  uses: actions/upload-artifact@v4
  with:
    name: workflow-contract-validation
    path: artifacts/workflow_contract_validation.json
```

---

## 4. Drift Report 漂移报告

### 4.1 概述

Drift Report 用于检测 workflow 文件（`.github/workflows/*.yml`）与合约定义（`workflow_contract.v1.json`）之间的差异，帮助识别潜在的配置漂移问题。

### 4.2 运行时机

| 场景 | 触发方式 | Make Target | 阻断策略 |
|------|----------|-------------|----------|
| **本地开发** | 手动执行 | `make workflow-contract-drift-report` | 默认阻断（脚本返回非零退出码） |
| **PR/CI** | workflow-contract job | 直接调用脚本 + `|| true` | 默认不阻断 |
| **夜间** | 定时触发（nightly） | N/A | 不执行 drift report |

**本地运行命令：**

```bash
# 生成 JSON + Markdown 报告到 artifacts/ 目录（不阻断）
make workflow-contract-drift-report-all

# 直接输出 JSON（阻断模式，脚本失败会返回非零退出码）
make workflow-contract-drift-report

# JSON 输出到文件
make workflow-contract-drift-report-json

# Markdown 输出
make workflow-contract-drift-report-markdown
```

### 4.3 阻断策略

**默认行为（CI）**：drift report 生成步骤使用 `|| true`，即使脚本报告差异也不会阻断 CI。报告仅供参考和审查。

**如需启用阻断**：

1. **本地阻断**：使用 `make workflow-contract-drift-report`（不加 `|| true`），脚本检测到 drift 时会返回非零退出码。

2. **CI 阻断**：修改 `.github/workflows/ci.yml` 中的 drift report 步骤，移除 `|| true`：

   ```yaml
   # 原配置（不阻断）
   - name: Generate drift report (JSON)
     run: |
       python scripts/ci/workflow_contract_drift_report.py --output artifacts/workflow_contract_drift.json || true

   # 改为阻断模式
   - name: Generate drift report (JSON)
     run: |
       python scripts/ci/workflow_contract_drift_report.py --output artifacts/workflow_contract_drift.json
   ```

3. **如需将 drift report 作为 required step**：同步更新 `scripts/ci/workflow_contract.v1.json` 的 `required_steps`，将生成步骤纳入合约管理。

### 4.4 输出位置

| 输出格式 | 文件路径 | 说明 |
|----------|----------|------|
| JSON | `artifacts/workflow_contract_drift.json` | 机器可读格式，供自动化处理 |
| Markdown | `artifacts/workflow_contract_drift.md` | 人类可读格式，适合 PR 评审 |

**CI Artifact 名称**：`workflow-contract-drift`

### 4.5 与其他校验的关系

| 校验工具 | 用途 | 阻断级别 |
|----------|------|----------|
| `validate_workflows.py` | 合约强制校验（frozen steps 等） | ERROR 阻断 |
| `check_workflow_contract_docs_sync.py` | 文档与合约同步校验 | ERROR 阻断 |
| `workflow_contract_drift_report.py` | 漂移检测报告（参考） | 默认不阻断 |

> **设计原则**：Drift Report 定位为"参考性报告"而非"强制门禁"，旨在帮助开发者识别潜在问题，而非强制阻断合并。如果项目需要更严格的控制，可按 4.3 节启用阻断。

---

## 5. 常见问题

### Q: 如何添加新的测试？

1. 在 `ci.yml` 的 `test` job 中添加测试 step
2. 如需新 Makefile 目标，更新 `Makefile` 和 `workflow_contract.v1.json`
3. 更新 `workflow_contract.v1.json` 的 `required_jobs[].required_steps`
4. 运行 `python scripts/ci/validate_workflows.py` 验证

### Q: 如何添加新的 Nightly 验证步骤？

1. 在 `nightly.yml` 的 `unified-stack-full` job 中添加 step
2. 更新 `workflow_contract.v1.json` 的 `nightly.required_jobs[].required_steps`
3. 如涉及环境变量，更新 `nightly.required_env_vars`
4. 运行 `python scripts/ci/validate_workflows.py` 验证

### Q: Step name 可以随意修改吗？

不可以。`frozen_step_text.allowlist` 中的 step name 是合约的一部分，修改将导致 CI 失败。

**冻结 step 的验证规则：**
- 如果 step name 属于 `frozen_step_text.allowlist`，改名会报告为 **ERROR** (`frozen_step_name_changed`)
- 如果 step name 不属于 frozen allowlist，改名只会报告为 **WARNING** (`step_name_changed`)

**修改冻结 step name 的步骤：**
1. 更新 `workflow_contract.v1.json` 的 `frozen_step_text.allowlist`（添加新名称，移除旧名称）
2. 更新 `workflow_contract.v1.json` 的 `required_jobs[].required_steps`（如果有引用）
3. 更新 `contract.md` 的 "6. 禁止回归的 Step 文本范围"
4. 运行 `python scripts/ci/validate_workflows.py` 验证
5. 经过 code review 确认

**错误示例：**
```
[frozen_step_name_changed] ci:.github/workflows/ci.yml
  Key: Run CI precheck
  Message: Frozen step 'Run CI precheck' was renamed to 'Run precheck' in job 'precheck-static'. 
           此 step 属于冻结文案，不能改名；如确需改名需同步更新 contract+docs
  Expected: Run CI precheck
  Actual: Run precheck
```

### Q: 如何修改 acceptance 验收测试的步骤或产物？

**CI 测试修改** (ci.yml test job)：
1. 修改 `ci.yml` 中 `test` job 的相关步骤
2. 更新 `workflow_contract.v1.json` 的 `ci.required_jobs[0].required_steps`
3. 更新 `contract.md` 'Acceptance 验收测试合约' 节 (contract.md#9-acceptance-验收测试合约)
4. 更新 `docs/acceptance/00_acceptance_matrix.md` 的 CI 覆盖步骤表

**Nightly 验证修改** (nightly.yml unified-stack-full job)：
1. 修改 `nightly.yml` 中 `unified-stack-full` job 的相关步骤
2. 更新 `workflow_contract.v1.json` 的 `nightly.required_jobs[0].required_steps`
3. 更新环境变量传递（GATE_PROFILE、SKIP_DEGRADATION_TEST 等）
4. 同步更新上述文档

**产物路径变更**：
1. 修改 `workflow_contract.v1.json` 的 `artifact_archive.required_artifact_paths`
2. 运行 `python scripts/ci/validate_workflows.py` 验证 artifact 路径覆盖

### Q: CI test job 和 Nightly unified-stack-full job 的区别？

**CI test job**：
- 使用 GitHub Actions 提供的 PostgreSQL service container
- 运行单元测试和验收测试（pytest）
- 矩阵测试多个 Python 版本（3.10/3.11/3.12）
- 产物：`test-results-*.xml`、`acceptance-results-*.xml`、`migration-*.log`

**Nightly unified-stack-full job**：
- 使用 Docker Compose 启动完整统一栈（PostgreSQL + Gateway + OpenMemory）
- 运行 Gate Contract 校验和完整集成测试
- 使用 `record_acceptance_run.py` 记录 acceptance run
- 产物：`.artifacts/verify-results.json`、`test-unified-stack-results.xml`、`compose-logs.txt`

### Q: record_acceptance_run.py 的 command 和 metadata 字段有什么含义？

**command 字段**：
- 记录实际执行的命令或步骤序列
- Nightly：使用 job 和 workflow 标识（如 `nightly.yml unified-stack-full`）

**metadata 字段**：
- 用于记录 CI/CD 上下文，便于追溯和分析
- 常用的 key：
  - `workflow`: `nightly`
  - `profile`: `full` / `standard` / `http_only`
  - `triggered_by`: `schedule` / `workflow_dispatch`
  - `run_id`, `sha`: GitHub Actions 运行标识

---

## 6. 冻结 Step 文案机器规则与 Rename 流程

### 6.1 机器检测规则

`validate_workflows.py` 对 step name 的检测逻辑如下：

1. **精确匹配检查**：首先检查 workflow 中是否存在与 `frozen_step_text.allowlist` 完全一致的 step name
2. **Fuzzy Matching 检测**：如果精确匹配失败，使用以下三层渐进式模糊匹配算法检测是否为"改名"：
   - **第一层：大小写不敏感匹配** - 忽略大小写后完全一致
   - **第二层：包含匹配** - target 包含 candidate 或 candidate 包含 target
   - **第三层：词语重叠匹配** - 以空格分词后，至少 50% 的词语重叠
3. **结果分类**：
   - 精确匹配：通过，无输出
   - Fuzzy 命中 + 属于冻结列表：**ERROR** (`frozen_step_name_changed`)
   - Fuzzy 命中 + 不属于冻结列表：**WARNING** (`step_name_changed`)
   - 无匹配：**ERROR** (`step_missing`)

### 6.2 冻结 Step Rename 标准流程

当确实需要修改冻结 step name 时，必须遵循以下流程：

**步骤 1: 准备变更**
```bash
# 1. 创建专门的重命名分支
git checkout -b ci/rename-frozen-step-xxx

# 2. 同步修改三处文件：
#    a) workflow 文件中的 step name
#    b) workflow_contract.v1.json 的 frozen_step_text.allowlist
#    c) contract.md 'Frozen Step Names' 节 (contract.md#52-frozen-step-names)
```

**步骤 2: 更新 workflow_contract.v1.json**
```json
// 示例：将 "Run unit and integration tests" 改为 "Run all tests"
{
  "frozen_step_text": {
    "allowlist": [
      // 移除旧名称: "Run unit and integration tests"
      "Run all tests",  // 添加新名称
      // ... 其他冻结 step
    ]
  },
  "ci": {
    "required_jobs": [
      {
        "id": "test",
        "required_steps": [
          // 同步更新引用
          "Run all tests"
        ]
      }
    ]
  }
}
```

**步骤 3: 更新 contract.md**

在 "5.2 Frozen Step Names" 章节同步修改对应条目。

**步骤 4: 本地验证**
```bash
# 必须通过才能提交
python scripts/ci/validate_workflows.py
python scripts/ci/check_workflow_contract_docs_sync.py

# 完整验证（推荐）
make ci
```

**步骤 5: PR 说明要求**

PR 描述中必须包含：
- **变更原因**：为何需要修改此冻结 step name
- **影响范围**：列出所有被修改的文件
- **验证结果**：`make validate-workflows-strict` 输出截图或日志

### 6.3 禁止的 Rename 模式

以下 rename 行为会被 CI 拒绝：

| 模式 | 示例 | 原因 |
|------|------|------|
| 仅改 workflow 不改合约 | 改 ci.yml 但不改 workflow_contract.v1.json | 合约一致性检查失败（ERROR） |
| 仅改合约不改 workflow | 改 workflow_contract.v1.json 但不改 ci.yml | step_missing 错误 |
| 添加到 allowlist 但不从旧条目移除 | 新旧名称同时存在 | 允许但导致冗余，应清理旧条目 |
| 批量修改多个冻结 step | 一个 PR 改 5+ 个冻结 step | 风险过高，建议拆分 PR |

**当前冻结的 Job Names** (v2.x)：
- `Test (Python ${{ matrix.python-version }})`
- `Lint`
- `Workflow Contract Validation`
- `Unified Stack Full Verification`

**当前冻结的 Step Names** (v2.x)：
- `Checkout repository`、`Set up Python`、`Install dependencies`
- `Run unit and integration tests`、`Run acceptance tests`
- `Upload test results`、`Upload migration logs`、`Upload validation results`、`Upload validation report`
- `Validate workflow contract`
- `Start unified stack with Docker Compose`、`Run unified stack verification (full)`

### 6.4 紧急绕过（不推荐）

如遇紧急情况需要绕过冻结检查：

```bash
# 冻结 step/job 改名始终报 ERROR，无法绕过
# 如需紧急修改，必须同步更新合约文件
python scripts/ci/validate_workflows.py
```

> **注意**: CI 始终执行合约校验，冻结 step 改名在任何情况下都是 ERROR，无法绕过。如需紧急修改，必须同步更新 `workflow_contract.v1.json` 和相关文档。

---

## 7. Workflow 变更前的快照对比流程

### 7.1 为什么需要快照对比？

在修改 workflow 时，很容易遗漏同步更新 `workflow_contract.v1.json`。使用快照对比可以：

- 清晰展示 workflow 的结构性变更（新增/删除 job、step、output 等）
- 帮助识别需要同步更新合约的内容
- 提供 diff 友好的 JSON 输出，便于 code review

### 7.2 使用方法

**步骤 1: 在修改前生成基线快照**

```bash
# 生成所有 workflow 的快照
python scripts/ci/generate_workflow_contract_snapshot.py --output /tmp/before.json

# 或只生成特定 workflow 的快照
python scripts/ci/generate_workflow_contract_snapshot.py --workflow ci --output /tmp/before.json
```

**步骤 2: 进行 workflow 修改**

编辑 `.github/workflows/*.yml` 文件。

**步骤 3: 生成修改后的快照**

```bash
python scripts/ci/generate_workflow_contract_snapshot.py --output /tmp/after.json
```

**步骤 4: 对比差异**

```bash
# 使用 diff 对比
diff /tmp/before.json /tmp/after.json

# 或使用更友好的 JSON diff 工具
# 例如: jd (https://github.com/josephburnett/jd)
jd /tmp/before.json /tmp/after.json
```

**步骤 5: 根据差异更新合约**

根据对比结果，同步更新：
- `scripts/ci/workflow_contract.v1.json`
- `docs/ci_nightly_workflow_refactor/contract.md`

### 7.3 脚本参数说明

| 参数 | 说明 |
|------|------|
| `--workflow, -w` | 只生成指定 workflow 的快照（如: ci, nightly, release） |
| `--output, -o` | 输出到指定文件（默认输出到 stdout） |
| `--include-step-details, -d` | 包含 step 的详细信息（uses, run preview, if 条件等） |
| `--workflows-dir` | 指定 workflows 目录路径（默认自动查找） |
| `--compact` | 使用紧凑 JSON 格式（无缩进） |

### 7.4 输出内容示例

```json
{
  "_metadata": {
    "generator": "generate_workflow_contract_snapshot.py",
    "workflows_dir": ".github/workflows",
    "workflow_filter": "ci",
    "include_details": false
  },
  "_summary": {
    "workflow_count": 1,
    "total_jobs": 12,
    "workflows_with_errors": []
  },
  "workflows": {
    "ci": {
      "file": "ci.yml",
      "name": "CI",
      "triggers": ["pull_request", "push", "workflow_dispatch"],
      "job_ids": ["detect-changes", "precheck-static", ...],
      "job_names": ["Detect Changes", "[Fast] Precheck & Static Build Verify", ...],
      "jobs": [
        {
          "id": "detect-changes",
          "name": "Detect Changes",
          "outputs": ["logbook_changed", "gateway_changed", ...],
          "steps": [
            {"name": "Checkout repository"},
            {"name": "Detect file changes"},
            ...
          ]
        },
        ...
      ]
    }
  }
}
```

### 7.5 最佳实践

1. **每次修改 workflow 前**都先生成基线快照
2. 修改完成后对比差异，**逐项检查**是否需要更新合约
3. 在 PR 描述中附上关键差异摘要
4. 运行 `make validate-workflows-strict` 验证合约一致性

---

## 8. 变更 SOP 快速检查表

在修改 workflow 或 contract 文件前，请参照此检查表确认版本策略和同步要求。

> **详细版本策略**：参见 [contract.md 第 11 章 SemVer Policy / 版本策略](contract.md#11-semver-policy--版本策略)

### 8.1 版本升级快速判断

| 变更场景 | 版本位 | 说明 |
|----------|--------|------|
| 删除/重命名 job、step、output key | **Major** | Breaking change，需评审 |
| 新增 job、step、frozen step、output key | **Minor** | 功能新增 |
| 修复错误、完善文档、调整描述 | **Patch** | 仅文档或修复 |

### 8.2 变更前检查项

- [ ] 确认变更类型（Breaking/Feature/Fix）
- [ ] 按版本策略确定版本升级位
- [ ] 检查是否涉及 frozen step name（参见 contract.md#5-禁止回归的-step-文本范围）
- [ ] 检查是否涉及 required artifact paths

### 8.3 变更后验证项

```bash
# 必须通过的验证
python scripts/ci/validate_workflows.py              # 合约校验
python scripts/ci/check_workflow_contract_docs_sync.py  # 文档同步校验
pytest tests/ci/ -q                                   # CI 脚本测试

# 完整 CI 检查（推荐）
make ci
```

### 8.4 版本更新清单

变更完成后需同步更新：

1. `scripts/ci/workflow_contract.v1.json` 的 `version` 字段
2. `contract.md` '版本控制' 节 (contract.md#14-版本控制)
3. 相关 changelog 或 PR 描述

---

## 9. 版本控制

| 版本 | 日期 | 变更说明 |
|------|------|----------|
| v2.3 | 2026-02-02 | 更新章节引用：contract.md 版本控制章节由 13 改为 14（配合 contract.md 新增第 0 章） |
| v2.2 | 2026-02-02 | 新增第 4 章"Drift Report 漂移报告"：定义运行时机、阻断策略、输出位置、Make targets |
| v2.1 | 2026-02-02 | 修正参数说明表格：`--strict` CI 是否启用改为"是"；新增 3.1.1 节说明 CI 选择 strict 的原因；新增 3.1.2 节紧急回滚方案 |
| v2.0 | 2026-02-02 | 重大更新：适配 Phase 1 重构后的 workflow 结构（移除 detect-changes job）；更新所有 job/step/产物路径示例；更新冻结列表；简化 Q&A |
| v1.6 | 2026-01-30 | 新增 Acceptance 验收测试维护 checklist：CI 组合式覆盖、Nightly 直接执行、产物变更的同步更新清单 |
| v1.5 | 2026-01-30 | 新增 Artifact Upload 路径维护 checklist：artifact_archive 合约配置说明、路径匹配规则 |
| v1.4 | 2026-01-30 | 新增 Labels 一致性校验说明：PR Label 变更时需同步更新 `ci.labels` 和 `LABEL_*` 常量；新增 Workflow 变更前的快照对比流程 |
| v1.3 | 2026-01-30 | 更新路径过滤示例：使用 `contract_changed` 示例替代 `docs_changed`，强调 outputs 导出 |
| v1.2 | 2026-01-30 | 完善冻结 step 改名说明：冻结 step 改名报 ERROR，非冻结 step 改名报 WARNING |
| v1.1 | 2026-01-30 | 新增 upstream_ref 变更时 CI 执行顺序说明 |
| v1.0 | 2026-01-30 | 初始版本，建立维护 checklist |
