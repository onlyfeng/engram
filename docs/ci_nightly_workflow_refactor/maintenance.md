# CI/Nightly Workflow 维护指南

> 本文档提供 CI/Nightly workflow 变更时的同步更新 checklist，确保合约文件、文档与实际 workflow 保持一致。

---

## 1. 变更同步 Checklist

### 1.1 新增路径过滤（paths-filter）

当需要新增文件变更检测时：

- [ ] **ci.yml**: 在 `detect-changes` job 的 `dorny/paths-filter` 中添加新的 filter key
- [ ] **ci.yml**: 在 `detect-changes` job 的 `outputs` 中导出新的 output key
- [ ] **workflow_contract.v1.json**: 在 `ci.detect_changes.outputs` 数组中添加新的 output key
- [ ] **contract.md**: 在 "1.1 文件变更检测键" 表格中添加新条目
- [ ] **README.md**: 如有必要，更新 CI 分层策略说明

示例：新增 `contract_changed` 检测（触发 workflow 合约校验）

```yaml
# ci.yml - detect-changes job outputs
outputs:
  # ... existing outputs ...
  contract_changed: ${{ steps.filter.outputs.contract_changed }}

# ci.yml - detect-changes job filters
filters: |
  contract_changed:
    - '.github/workflows/**'
    - 'scripts/ci/workflow_contract*.json'
    - 'scripts/ci/validate_workflows.py'
    - 'Makefile'
    - 'docs/ci_nightly_workflow_refactor/**'
```

```json
// workflow_contract.v1.json
"detect_changes": {
  "outputs": [
    // ... existing outputs ...
    "contract_changed"
  ]
}
```

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
  - 在 `job_names` 数组中添加 job name（注意层级前缀如 `[Fast]`/`[Standard]`/`[Optional]`）
  - 在 `required_jobs` 数组中添加 job 详细定义（包含 `required_steps`）
- [ ] **contract.md**: 在 "2. Job ID 与 Job Name 对照表" 中添加新条目
- [ ] **README.md**: 如 job 影响 CI 分层策略，更新说明

Job Name 命名规范：
- `[Fast]` - Fast 层 job，PR 必跑或条件跑
- `[Standard]` - Standard 层 job，需变更检测触发
- `[Optional]` - 可选 job，需 label 或 dispatch input 触发
- 无前缀 - 通用 job（如 `Detect Changes`）

### 1.4 新增 PR Label

当新增 PR label 触发逻辑时：

- [ ] **ci.yml**: 在 `detect-changes` job 的 `Check PR labels` step 中处理新 label
- [ ] **scripts/ci/gh_pr_labels_to_outputs.py**: 添加 `LABEL_*` 常量和解析逻辑
- [ ] **workflow_contract.v1.json**: 在 `ci.labels` 数组中添加新 label
- [ ] **contract.md**: 在 "3. PR Label 列表与语义" 表格中添加新条目
- [ ] **README.md**: 在 "PR Label 说明" 表格中添加新条目
- [ ] 运行 `python scripts/ci/validate_workflows.py` 验证 label 一致性

**Labels 一致性自动校验：**

`validate_workflows.py` 会自动校验 `ci.labels` 与 `gh_pr_labels_to_outputs.py` 中 `LABEL_*` 常量的一致性：
- 若 contract 中有但脚本中没有：报 **ERROR** (`label_missing_in_script`)
- 若脚本中有但 contract 中没有：报 **ERROR** (`label_missing_in_contract`)

确保两处定义保持同步可避免 CI 阻断。

### 1.5 新增 workflow_dispatch 输入参数

当新增手动触发输入参数时：

- [ ] **ci.yml/nightly.yml**: 在 `workflow_dispatch.inputs` 中添加新参数
- [ ] **README.md**: 在对应 workflow 的"输入参数"表格中添加新条目
- [ ] **contract.md**: 如参数影响关键行为，更新相关章节

### 1.6 新增/修改 Step Name

当新增或修改 step name 时：

- [ ] **workflow_contract.v1.json**: 在 `frozen_step_text.allowlist` 中添加新 step name
- [ ] **contract.md**: 在 "5. 禁止回归的 Step 文本范围" 中更新
- [ ] 运行 `python scripts/ci/validate_workflows.py` 验证 - 脚本会检测 step name 变化并输出 ERROR（冻结 step）或 WARNING（非冻结 step）

### 1.7 新增/修改 Artifact Upload 路径

当新增关键的 artifact upload 步骤时：

- [ ] **ci.yml/nightly.yml**: 添加 `uses: actions/upload-artifact@v4` 步骤，确保 `with.path` 包含必需路径
- [ ] **workflow_contract.v1.json**: 在对应 workflow 的 `artifact_archive.required_artifact_paths` 中添加新路径
- [ ] **workflow_contract.v1.json**: 可选：在 `artifact_archive.artifact_step_names` 中添加步骤名称（用于限定检查范围）
- [ ] **contract.md**: 在 "8. Artifact Archive 合约" 中更新说明
- [ ] 运行 `python scripts/ci/validate_workflows.py` 验证 artifact 路径覆盖

### 1.8 修改 Acceptance 验收测试

当修改 acceptance 验收测试的步骤、产物或执行方式时：

**CI 组合式覆盖变更**:
- [ ] **ci.yml**: 修改 `unified-standard` job 中对应的步骤
- [ ] **ci.yml**: 确保 `record_acceptance_run.py` 的 `--command` 参数正确描述步骤序列
- [ ] **ci.yml**: 确保 `--metadata-kv` 包含必要的 CI 上下文（workflow、profile、run_number 等）
- [ ] **contract.md**: 更新第 11 章 "Acceptance 验收测试合约" 的组合式覆盖步骤映射
- [ ] **coupling_map.md**: 更新第 5 章 "Acceptance 产物归档路径"
- [ ] **docs/acceptance/00_acceptance_matrix.md**: 更新 "CI 覆盖步骤" 表

**Nightly 直接执行变更**:
- [ ] **Makefile**: 修改 `acceptance-unified-full` 目标的步骤序列
- [ ] **nightly.yml**: 同步更新环境变量传递（SKIP_DEPLOY、GATE_PROFILE 等）
- [ ] **contract.md**: 更新第 11 章中的 Nightly 直接执行合约
- [ ] **coupling_map.md**: 更新 Nightly Workflow 使用的 Targets 表

**产物变更**:
- [ ] 更新 `workflow_contract.v1.json` 的 `artifact_archive.required_artifact_paths`
- [ ] 更新 `coupling_map.md` 第 5 章的产物归档路径表
- [ ] 更新 `docs/acceptance/00_acceptance_matrix.md` 的产物记录与追溯表
- [ ] 运行 `python scripts/ci/validate_workflows.py` 验证 artifact 路径覆盖

**artifact_archive 配置示例：**

```json
"artifact_archive": {
  "_comment": "CI workflow 必需的 artifact 路径",
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

**路径匹配规则：**
- 精确匹配：`.artifacts/verify-results.json` 匹配 `.artifacts/verify-results.json`
- 目录匹配：`.artifacts/acceptance-runs/` 匹配上传路径中以此开头的任何路径
- 多行 path 支持：upload-artifact 的 `with.path` 可以是多行 YAML 字符串

---

## 2. 合约文件说明

### 2.1 workflow_contract.v1.json

位置: `scripts/ci/workflow_contract.v1.json`

用途: 定义 workflow 的结构性合约，供 `validate_workflows.py` 自动校验

关键字段:
| 字段 | 说明 |
|------|------|
| `ci.detect_changes.outputs` | 变更检测 job 的所有 output keys |
| `ci.labels` | 支持的 PR labels |
| `ci.job_ids` / `ci.job_names` | Job ID 与 name 列表 |
| `ci.required_jobs` | 每个 job 的必需 steps 和 outputs |
| `ci.artifact_archive` | Artifact 上传合约（required_artifact_paths, artifact_step_names） |
| `make.targets_required` | workflow 依赖的 Makefile 目标 |
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

# 2. 运行合约校验（普通模式：frozen steps 违规报 ERROR，其他警告不阻止）
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
              - 第 6 章 '禁止回归的 Step 文本范围'
           3. 运行 make validate-workflows 验证
           4. 详见 docs/ci_nightly_workflow_refactor/maintenance.md
  Location: jobs.precheck-static.steps
  Expected: Run CI precheck
  Actual: Run precheck
```

### 3.2 CI 自动验证

workflow 变更会触发 `Workflow Contract Validation` job，自动执行合约校验。

- **Job ID**: `workflow-contract`
- **Job Name**: `Workflow Contract Validation`
- **触发条件**: 始终执行（所有 push/pull_request）
- **依赖安装**: `pip install pyyaml jsonschema pytest`

**校验步骤：**
1. 运行 CI 脚本测试 (`pytest tests/ci/ -q`)
2. 运行 workflow 合约校验 (`python scripts/ci/validate_workflows.py`)
3. 运行文档同步检查 (`python scripts/ci/check_workflow_contract_docs_sync.py`)
4. 生成 JSON 格式报告（用于 artifact 上传）

**校验行为：**
- frozen step/job name 改名: 报 ERROR
- make target 缺失: 报 ERROR
- 文档与合约不同步: 报 ERROR

**CI 配置参考：**
```yaml
# .github/workflows/ci.yml - workflow-contract job
- name: Install dependencies
  run: pip install pyyaml jsonschema pytest

- name: Run CI script tests
  run: pytest tests/ci/ -q --ignore=...

- name: Validate workflow contract
  run: python scripts/ci/validate_workflows.py

- name: Check workflow contract docs sync
  run: python scripts/ci/check_workflow_contract_docs_sync.py
```

---

## 4. 常见问题

### Q: 如何添加新的 Seek 相关测试？

1. 在 `ci.yml` 或 `nightly.yml` 中添加测试 step
2. 如需新 Makefile 目标，更新 `Makefile` 和 `workflow_contract.v1.json`
3. 如使用 `SEEK_*` 环境变量，确保在 `contract.md` 第 4 章记录

### Q: 如何添加新的 OpenMemory 检查？

1. 在 `openmemory-governance-check` job 中添加 step
2. 更新 `workflow_contract.v1.json` 的 `required_steps`
3. 如涉及冻结检查，更新 `openmemory:freeze-override` 相关文档

### Q: upstream_ref 变更时 CI 的执行顺序是什么？

当 `upstream_ref_changed == true` 时，CI 按以下顺序执行：

1. `Generate OpenMemory patch bundle (strict mode)` - 先生成补丁包
2. `Run OpenMemory patch check` - 再执行 sync check/verify
3. `Run lock consistency check` - 验证 lock 文件字段完整性
4. `Verify upstream_ref change requirements` - 汇总检查状态

这个顺序确保补丁包在校验前可用，补丁包作为 CI artifact 上传（保留 30 天）供调试。

详见 `contract.md` 第 7 章"upstream_ref 变更要求"。

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

**CI 组合式覆盖（ci.yml unified-standard job）**:
1. 修改 `ci.yml` 中的相关步骤
2. 确保 `record_acceptance_run.py` 调用的 `--command` 参数同步更新
3. 更新 `contract.md` 第 11 章 "Acceptance 验收测试合约"
4. 更新 `coupling_map.md` 第 5 章 "Acceptance 产物归档路径"
5. 更新 `docs/acceptance/00_acceptance_matrix.md` 的 CI 覆盖步骤表

**Nightly 直接执行（nightly.yml）**:
1. 修改 `Makefile` 中的 `acceptance-unified-full` 目标
2. 更新 `nightly.yml` 中的环境变量传递（如有变化）
3. 同步更新上述文档

**产物路径变更**:
1. 修改 `workflow_contract.v1.json` 的 `artifact_archive.required_artifact_paths`
2. 更新 `coupling_map.md` 第 5 章
3. 运行 `python scripts/ci/validate_workflows.py` 验证 artifact 路径覆盖

### Q: CI 的 acceptance-unified-min 是 "组合式覆盖" 还是 "真实执行 make 目标"？

**CI 使用组合式覆盖**，而非直接调用 `make acceptance-unified-min`。

原因：
1. **细粒度错误处理**：workflow 分步执行可以对每个步骤单独设置 `continue-on-error`
2. **独立 artifact 收集**：每个步骤可以独立上传 artifact，便于调试
3. **条件执行**：某些步骤可以根据变更检测结果跳过
4. **metadata 注入**：`record_acceptance_run.py` 可以传入 CI 特定的 metadata

**Nightly 使用直接执行**（调用 `make acceptance-unified-full`），因为：
1. Nightly 是完整验收，需要所有步骤执行
2. 核心验证链收敛到单一入口，减少维护复杂度
3. 产物自动收集到标准目录

### Q: record_acceptance_run.py 的 command 和 metadata 字段有什么含义？

**command 字段**：
- 记录实际执行的命令或步骤序列
- CI 组合式覆盖：使用 `→` 分隔的步骤序列描述（如 `deploy → verify-unified(profile=http_only) → ...`）
- 本地/Nightly 直接执行：使用实际的 make 命令（如 `make acceptance-unified-full`）

**metadata 字段**：
- 用于记录 CI/CD 上下文，便于追溯和分析
- CI 中常用的 key：
  - `workflow`: `ci` / `nightly`
  - `profile`: `http_only` / `full`
  - `run_number`, `run_id`: GitHub Actions 运行标识
  - `event_name`: `pull_request` / `push` / `schedule`
  - `http_only_mode`, `skip_degradation`: 环境变量设置值

---

## 6. 冻结 Step 文案机器规则与 Rename 流程

### 6.1 机器检测规则

`validate_workflows.py` 对 step name 的检测逻辑如下：

1. **精确匹配检查**：首先检查 workflow 中是否存在与 `frozen_step_text.allowlist` 完全一致的 step name
2. **Fuzzy Matching 检测**：如果精确匹配失败，使用模糊匹配算法检测是否为"改名"（相似度阈值：0.8）
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
#    c) contract.md 第 6 章对应条目
```

**步骤 2: 更新 workflow_contract.v1.json**
```json
// 示例：将 "Run CI precheck" 改为 "Run precheck validation"
{
  "frozen_step_text": {
    "allowlist": [
      // 移除旧名称: "Run CI precheck"
      "Run precheck validation",  // 添加新名称
      // ... 其他冻结 step
    ]
  },
  "ci": {
    "required_jobs": [
      {
        "job_id": "precheck-static",
        "required_steps": [
          // 同步更新引用
          "Run precheck validation"
        ]
      }
    ]
  }
}
```

**步骤 3: 更新 contract.md**

在 "6.2 关键 Step Name" 章节同步修改对应条目。

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
| 仅改 workflow 不改合约 | 改 ci.yml 但不改 contract.json | 合约一致性检查失败 |
| 仅改合约不改 workflow | 改 contract.json 但不改 ci.yml | step_missing 错误 |
| 添加到 allowlist 但不从旧条目移除 | 新旧名称同时存在 | 允许但导致冗余，应清理旧条目 |
| 批量修改多个冻结 step | 一个 PR 改 5+ 个冻结 step | 风险过高，建议拆分 PR |

### 6.4 紧急绕过（不推荐）

如遇紧急情况需要绕过冻结检查：

```bash
# 冻结 step/job 改名始终报 ERROR，无法绕过
# 如需紧急修改，必须同步更新合约文件
python scripts/ci/validate_workflows.py
```

> **注意**: CI 始终执行合约校验，冻结 step 改名在任何情况下都是 ERROR，无法绕过。如需紧急修改，必须同步更新 `workflow_contract.v1.json` 和相关文档。

---

## 5. Workflow 变更前的快照对比流程

### 5.1 为什么需要快照对比？

在修改 workflow 时，很容易遗漏同步更新 `workflow_contract.v1.json`。使用快照对比可以：

- 清晰展示 workflow 的结构性变更（新增/删除 job、step、output 等）
- 帮助识别需要同步更新合约的内容
- 提供 diff 友好的 JSON 输出，便于 code review

### 5.2 使用方法

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

### 5.3 脚本参数说明

| 参数 | 说明 |
|------|------|
| `--workflow, -w` | 只生成指定 workflow 的快照（如: ci, nightly, release） |
| `--output, -o` | 输出到指定文件（默认输出到 stdout） |
| `--include-step-details, -d` | 包含 step 的详细信息（uses, run preview, if 条件等） |
| `--workflows-dir` | 指定 workflows 目录路径（默认自动查找） |
| `--compact` | 使用紧凑 JSON 格式（无缩进） |

### 5.4 输出内容示例

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

### 5.5 最佳实践

1. **每次修改 workflow 前**都先生成基线快照
2. 修改完成后对比差异，**逐项检查**是否需要更新合约
3. 在 PR 描述中附上关键差异摘要
4. 运行 `make validate-workflows-strict` 验证合约一致性

---

## 6. 变更 SOP 快速检查表

在修改 workflow 或 contract 文件前，请参照此检查表确认版本策略和同步要求。

> **详细版本策略**：参见 [contract.md 第 10 章 SemVer Policy / 版本策略](contract.md#10-semver-policy--版本策略)

### 6.1 版本升级快速判断

| 变更场景 | 版本位 | 说明 |
|----------|--------|------|
| 删除/重命名 job、step、output key | **Major** | Breaking change，需评审 |
| 新增 job、step、frozen step、output key | **Minor** | 功能新增 |
| 修复错误、完善文档、调整描述 | **Patch** | 仅文档或修复 |

### 6.2 变更前检查项

- [ ] 确认变更类型（Breaking/Feature/Fix）
- [ ] 按版本策略确定版本升级位
- [ ] 检查是否涉及 frozen step name（参见 contract.md 第 5 章）
- [ ] 检查是否涉及 required artifact paths

### 6.3 变更后验证项

```bash
# 必须通过的验证
python scripts/ci/validate_workflows.py              # 合约校验
python scripts/ci/check_workflow_contract_docs_sync.py  # 文档同步校验
pytest tests/ci/ -q                                   # CI 脚本测试

# 完整 CI 检查（推荐）
make ci
```

### 6.4 版本更新清单

变更完成后需同步更新：

1. `scripts/ci/workflow_contract.v1.json` 的 `version` 字段
2. `docs/ci_nightly_workflow_refactor/contract.md` 第 11 章版本控制表
3. 相关 changelog 或 PR 描述

---

## 7. 版本控制

| 版本 | 日期 | 变更说明 |
|------|------|----------|
| v1.6 | 2026-01-30 | 新增 Acceptance 验收测试维护 checklist：CI 组合式覆盖、Nightly 直接执行、产物变更的同步更新清单 |
| v1.5 | 2026-01-30 | 新增 Artifact Upload 路径维护 checklist：artifact_archive 合约配置说明、路径匹配规则 |
| v1.0 | 2026-01-30 | 初始版本，建立维护 checklist |
| v1.1 | 2026-01-30 | 新增 upstream_ref 变更时 CI 执行顺序说明 |
| v1.2 | 2026-01-30 | 完善冻结 step 改名说明：冻结 step 改名报 ERROR，非冻结 step 改名报 WARNING |
| v1.3 | 2026-01-30 | 更新路径过滤示例：使用 `contract_changed` 示例替代 `docs_changed`，强调 outputs 导出 |
| v1.4 | 2026-01-30 | 新增 Labels 一致性校验说明：PR Label 变更时需同步更新 `ci.labels` 和 `LABEL_*` 常量 |
| v1.4 | 2026-01-30 | 新增 Workflow 变更前的快照对比流程（generate_workflow_contract_snapshot.py） |
