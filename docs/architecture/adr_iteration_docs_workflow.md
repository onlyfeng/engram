# ADR: 迭代文档工作流与 SSOT 策略

| 元数据 | 值 |
|--------|-----|
| **状态** | Accepted |
| **日期** | 2026-02-01 |
| **决策者** | Engram 核心团队 |
| **影响范围** | `docs/acceptance/`、`.iteration/`、CI artifacts |

---

## 背景

随着项目迭代推进，迭代计划和回归记录的管理出现以下问题：

1. **文档分散**：迭代相关文档散落在 `docs/architecture/`、`docs/acceptance/`、本地草稿等位置
2. **SSOT 不清晰**：不确定哪个位置是迭代文档的权威来源
3. **本地草稿泄漏**：`.iteration/` 本地笔记被误引用或提交
4. **证据策略缺失**：CI 产出的 artifacts 与版本化文档的关系不明确

本 ADR 补充 [adr_docs_information_architecture.md](adr_docs_information_architecture.md) 中的信息架构，专门定义迭代文档的工作流。

---

## 决策

### 1. SSOT 目录与命名规范

#### 1.1 迭代文档 Canonical 位置

**所有迭代文档的 SSOT 位于 `docs/acceptance/` 目录**。

| 文档类型 | 命名规范 | 说明 |
|----------|----------|------|
| 迭代计划 | `iteration_<N>_plan.md` | 迭代目标、范围、验收门禁 |
| 回归记录 | `iteration_<N>_regression.md` | 回归测试命令、修复记录 |
| 验收矩阵 | `00_acceptance_matrix.md` | 跨迭代验收测试执行记录 |

其中 `<N>` 为迭代编号（整数，不带前导零）。

#### 1.2 命名约束

```
docs/acceptance/
├── 00_acceptance_matrix.md          # 验收测试矩阵（跨迭代）
├── iteration_1_plan.md              # Iteration 1 计划
├── iteration_1_regression.md        # Iteration 1 回归
├── iteration_2_plan.md              # Iteration 2 计划
├── iteration_2_regression.md        # Iteration 2 回归
├── ...
└── iteration_<N>_{plan,regression}.md
```

**命名规则**：

- 文件名使用小写字母和下划线
- 迭代编号为整数，例如 `iteration_10_plan.md`（非 `iteration_010_plan.md`）
- 后缀固定为 `_plan` 或 `_regression`，不得使用其他命名

#### 1.3 内容模板

**迭代计划模板** (`iteration_<N>_plan.md`):

```markdown
# Iteration <N> 计划

> **状态**: 进行中 | 已完成 | 已取消
> **时间范围**: YYYY-MM-DD ~ YYYY-MM-DD
> **前置迭代**: [Iteration <N-1>](iteration_<N-1>_regression.md)

## 目标

- [ ] 目标 1
- [ ] 目标 2

## 范围

### 包含

- 任务 A
- 任务 B

### 排除

- 不在本迭代范围的内容

## 验收门禁

| 门禁 | 命令 | 通过标准 |
|------|------|----------|
| CI 通过 | `make ci` | 全部通过 |
| ... | ... | ... |

## 风险与缓解

| 风险 | 缓解措施 |
|------|----------|
| ... | ... |
```

**回归记录模板** (`iteration_<N>_regression.md`):

```markdown
# Iteration <N> 回归 Runbook

> **迭代计划**: [iteration_<N>_plan.md](iteration_<N>_plan.md)
> **状态**: 进行中 | 已完成

## 回归测试命令

```bash
make ci
make test
# 其他回归命令
```

## 修复记录

| 日期 | 问题 | 修复 | PR |
|------|------|------|-----|
| YYYY-MM-DD | 问题描述 | 修复说明 | #123 |

## 已知问题

- [ ] 待修复问题列表
```

---

### 2. `.iteration/` 本地草稿定位

#### 2.1 定位声明

**`.iteration/` 目录仅用于本地草稿和临时笔记**，具有以下特性：

| 特性 | 说明 |
|------|------|
| **版本控制** | 已在 `.gitignore` 中排除，**不纳入版本控制** |
| **权威性** | **非 SSOT**，不作为项目文档的权威来源 |
| **引用约束** | 可在口头/注释中提及，**禁止 Markdown 链接引用** |
| **生命周期** | 仅在本地开发期间存在，随时可删除 |

#### 2.2 引用约束

| 类型 | 示例 | 允许 |
|------|------|------|
| **Markdown 链接** | 如 `[text]` + `(.iteration/...)` 形式 | ❌ **禁止** |
| **文本提及** | `参考本地 .iteration/13/ 中的草稿` | ✅ 允许 |
| **inline code 提及** | `本地草稿位于 \`.iteration/13/plan.md\`` | ✅ 允许 |

**允许的引用方式**：

```markdown
<!-- 在代码注释或口头交流中 -->
# 参考本地 .iteration/notes.md 中的草稿思路

<!-- 在版本化文档中用文本提及（非链接） -->
本地草稿位于 `.iteration/13/plan.md`，晋升后将迁移至 docs/acceptance/
```

**禁止的引用方式**：

```markdown
<!-- 禁止在版本化文档中使用 Markdown 链接 -->
详见 [本地笔记](.iteration/notes.md)   <!-- ❌ 禁止 -->
参考 [草稿](./.iteration/draft.md)    <!-- ❌ 禁止 -->
```

**理由**：

1. `.iteration/` 不在版本控制中，链接必然失效
2. 避免将非 SSOT 内容误认为权威文档
3. 保持版本化文档的自包含性
4. CI 门禁 (`make check-iteration-docs`) 会检测并拒绝 Markdown 链接

#### 2.3 迁移路径

当 `.iteration/` 中的草稿成熟时，应迁移到 SSOT 位置：

```
.iteration/iteration_5_draft.md
        ↓ 成熟后迁移
docs/acceptance/iteration_5_plan.md
```

#### 2.4 跨人协作与可链接引用

##### 存在性声明

**`.iteration/` 的存在性不保证**：

- `.iteration/` 目录为**本地草稿**，在 `.gitignore` 中排除，**不纳入版本控制**
- **每个开发者的 `.iteration/` 内容不同**：目录可能不存在、内容可能不同、随时可能被删除或重建
- 因此，**任何指向 `.iteration/` 的链接必然不稳定**，无法在跨人场景中可靠工作

##### 跨人协作场景的入口选择

当需要**跨人协作**或**可链接引用**时，不要依赖 `.iteration/`，应根据场景选择以下入口：

| 场景 | 推荐入口 | 说明 |
|------|----------|------|
| **需要可链接引用** | `docs/acceptance/` SSOT | 版本化、永久有效、团队共享 |
| **短期协作分享** | 导出分享包 | 使用 `export_local_iteration.py` 生成可分享的文档包 |
| **仅提示路径（非链接）** | 文本 / inline code | 例如 `参考 .iteration/13/` 或 `` `.iteration/13/plan.md` `` |

##### 入口选择决策

```
是否需要跨人协作 / 可链接引用？
    │
    ├─ 否（仅本地使用）
    │     → 直接使用 .iteration/
    │
    └─ 是 → 选择入口类型
              │
              ├─ 需要永久可链接？
              │     → SSOT：docs/acceptance/iteration_<N>_*.md
              │        （需先完成晋升流程）
              │
              ├─ 短期协作（未晋升草稿）？
              │     → 使用导出脚本生成分享包：
              │        python scripts/iteration/export_local_iteration.py 13
              │
              └─ 仅提示路径（不需要链接可点击）？
                    → 文本/inline code 提及：
                       "参考 `.iteration/13/plan.md`"
```

##### 示例

**❌ 错误做法**（跨人场景依赖 `.iteration/`）：

```markdown
<!-- 在版本化文档或 PR 描述中使用 Markdown 链接 -->
详见 [迭代计划](.iteration/13/plan.md)   <!-- ❌ 链接必然不稳定 -->
```

**✅ 正确做法**（按场景选择入口）：

```markdown
<!-- 场景 1：需要可链接引用 → 使用 SSOT -->
详见 [Iteration 13 计划](docs/acceptance/iteration_13_plan.md)

<!-- 场景 2：短期协作 → 生成分享包后附件发送 -->
# 在本地执行，打包为 zip（推荐）
python scripts/iteration/export_local_iteration.py 13 --output-zip /tmp/iteration_13_draft.zip
# 然后通过 Slack / 邮件发送该 zip 文件

<!-- 场景 3：仅提示路径 → 文本 / inline code -->
本地草稿位于 `.iteration/13/plan.md`，晋升后将迁移至 docs/acceptance/
```

---

### 3. 证据策略

#### 3.1 证据分类

| 类别 | 存储位置 | 版本化 | SSOT | 示例 |
|------|----------|--------|------|------|
| **规范性文档** | `docs/` | 是 | 是 | 迭代计划、回归记录、ADR |
| **测试结果快照** | `docs/` 或 PR 描述 | 是 | 是 | 关键测试输出的文本摘要 |
| **CI Artifacts** | CI 系统 | 否 | 否 | 完整测试报告、覆盖率报告、构建日志 |
| **Nightly 审计报告** | CI Artifacts | 否 | 否 | 迭代文档审计快照（90 天保留） |
| **本地草稿** | `.iteration/` | 否 | 否 | 临时笔记、WIP 计划 |

#### 3.2 入库策略（版本化证据）

**需要入库的证据**：

| 证据类型 | 入库位置 | 入库条件 |
|----------|----------|----------|
| 迭代计划 | `docs/acceptance/iteration_<N>_plan.md` | 迭代开始前 |
| 回归记录 | `docs/acceptance/iteration_<N>_regression.md` | 迭代进行中/完成后 |
| 关键测试输出 | 内嵌在回归记录或 PR 描述中 | 验收相关的测试结果 |
| 门禁通过证据 | 内嵌在 `00_acceptance_matrix.md` | 验收检查点完成时 |

**入库格式**：

```markdown
## 验收证据

### CI 门禁

```bash
$ make ci
✅ lint: passed
✅ typecheck-gate: passed
✅ test: 142 passed, 0 failed
```

> **CI Run**: [GitHub Actions #1234](https://github.com/...)
```

#### 3.3 CI Artifacts 策略（非版本化证据）

**CI Artifacts 不作为 SSOT**，但可通过以下方式引用：

| 引用方式 | 示例 | 说明 |
|----------|------|------|
| **CI Run 链接** | `[CI #1234](https://...)` | 链接到 CI 运行页面 |
| **Artifact 下载链接** | GitHub Actions 产出 | 有时效性，通常 90 天 |
| **摘要嵌入** | 将关键输出粘贴到文档 | 持久化关键信息 |

**在文档中引用 CI Artifacts**：

```markdown
## 测试报告

完整测试报告见 [CI Run #1234](https://github.com/.../actions/runs/1234) 的 Artifacts。

关键指标摘要：
- 测试通过率: 100% (142/142)
- 覆盖率: 85.2%
- Lint 错误: 0
```

#### 3.4 Nightly 审计报告策略

**Nightly 审计报告定位**：

| 属性 | 值 |
|------|-----|
| **来源** | `nightly.yml` 中的 `iteration-audit` job |
| **生成命令** | `make iteration-audit` |
| **输出目录** | `.artifacts/iteration-audit/` |
| **Artifact 名称** | `iteration-audit-<run_number>-<run_id>` |
| **保留周期** | 90 天 |
| **SSOT 状态** | **否**（仅观察性报告） |
| **阻断行为** | **非阻断**（审计问题不影响 Nightly 流水线状态） |

**定位说明**：

- **仅观察**：Nightly 审计报告用于定期观察迭代文档的一致性状态，**不作为 SSOT**
- **非阻断**：审计发现的问题仅产生警告，不阻断 Nightly 流水线
- **回溯性**：90 天保留期便于按日期回溯历史审计状态
- **与 CI 门禁的关系**：阻断式检查由 `make check-iteration-docs` 负责（在 PR CI 中执行），Nightly 审计为补充观察

**使用场景**：

1. **定期巡检**：每日自动生成，无需人工触发
2. **问题追溯**：当发现文档一致性问题时，可回溯历史报告定位引入时间
3. **审计留痕**：为代码审查或合规需求提供历史证据

#### 3.5 版本化证据文件

**定义**：版本化证据文件是指纳入 Git 版本控制、用于记录迭代验收证据的结构化文件。

**存储位置**：`docs/acceptance/evidence/`

**命名规范（统一）**：

| 类型 | 格式 | 示例 | 说明 |
|------|------|------|------|
| **Canonical（规范）** | `iteration_<N>_evidence.json` | `iteration_13_evidence.json` | 单一迭代的综合证据文件，**推荐使用**，每次更新覆盖 |
| **Snapshot（快照）** | `iteration_<N>_<YYYYMMDD_HHMMSS>.json` | `iteration_13_20260202_143000.json` | 历史快照，用于需要保留多次验收记录的场景 |
| **Snapshot+SHA** | `iteration_<N>_<YYYYMMDD_HHMMSS>_<sha7>.json` | `iteration_13_20260202_143000_abc1234.json` | 带 commit SHA 的历史快照 |

**生成方式**：

| 类型 | 生成命令 | 说明 |
|------|----------|------|
| Canonical | `python scripts/iteration/record_iteration_evidence.py <N>` | 脚本默认输出，覆盖已有文件 |
| Snapshot | 手动复制：`cp iteration_<N>_evidence.json iteration_<N>_<YYYYMMDD_HHMMSS>.json` | 需保留历史时手动创建 |

**回归文档引用策略**：

在 `iteration_<N>_regression.md` 中引用证据文件时，**推荐使用 canonical 文件**：

```markdown
## 验收证据

详细证据见 [iteration_13_evidence.json](evidence/iteration_13_evidence.json)。
```

如有多次验收需要追溯，可同时引用快照：

```markdown
## 验收证据

- 最新证据：[iteration_13_evidence.json](evidence/iteration_13_evidence.json)
- 历史快照：[iteration_13_20260201_103000.json](evidence/iteration_13_20260201_103000.json)（首次验收）
```

**格式 SSOT**：[schemas/iteration_evidence_v1.schema.json](../../schemas/iteration_evidence_v1.schema.json)

**Schema 约束摘要**：

| 字段 | 必填 | 说明 |
|------|------|------|
| `iteration_number` | ✅ | 迭代编号（正整数） |
| `recorded_at` | ✅ | 记录时间（ISO 8601 UTC） |
| `commit_sha` | ✅ | Git commit SHA（7-40 字符） |
| `runner` | ✅ | 执行环境（os/python/arch 必填） |
| `commands` | ✅ | 门禁命令数组（name/command/result 必填） |
| `links` | ❌ | 可选链接（ci_run_url、pr_url 等） |
| `notes` | ❌ | 补充说明 |
| `overall_result` | ❌ | 整体结果（PASS/PARTIAL/FAIL） |

**安全约束**：Schema 明确禁止在任何字段中包含敏感信息（密码、API 密钥、DSN 原文、内部 IP 等）。

**文件内容示例**：

```json
{
  "$schema": "./iteration_evidence_v1.schema.json",
  "iteration_number": 13,
  "recorded_at": "2026-02-02T14:30:22Z",
  "commit_sha": "abc1234def5678901234567890abcdef12345678",
  "runner": {
    "os": "ubuntu-22.04",
    "python": "3.11.9",
    "arch": "x86_64"
  },
  "commands": [
    {
      "name": "ci",
      "command": "make ci",
      "result": "PASS",
      "summary": "所有检查通过",
      "duration_seconds": 45
    }
  ],
  "links": {
    "ci_run_url": "https://github.com/.../actions/runs/1234"
  },
  "notes": "所有最小门禁通过",
  "overall_result": "PASS",
  "sensitive_data_declaration": true
}
```

**与 `.artifacts/` 的区别**：

| 属性 | `docs/acceptance/evidence/` | `.artifacts/` |
|------|----------------------------|---------------|
| **版本控制** | ✅ 纳入 Git | ❌ 在 `.gitignore` 中排除 |
| **SSOT** | ✅ 作为迭代证据的 SSOT | ❌ 非 SSOT，仅运行时产物 |
| **Markdown 链接** | ✅ 允许 | ❌ **禁止** |
| **生命周期** | 永久保留 | 临时产物，CI 90 天保留 |

**`.artifacts/` 引用约束**：

`.artifacts/` 与 `.iteration/` 一样，**不得在版本化文档中以 Markdown 链接形式出现**。

| 类型 | 示例 | 允许 |
|------|------|------|
| **Markdown 链接** | `[报告](.artifacts/test-results.xml)` | ❌ **禁止** |
| **文本提及** | `本地产物位于 .artifacts/acceptance-runs/` | ✅ 允许 |
| **inline code 提及** | `` `.artifacts/acceptance-unified-min/summary.json` `` | ✅ 允许 |

**理由**：

1. `.artifacts/` 不在版本控制中，链接必然失效
2. 同一 commit 在不同机器上的 `.artifacts/` 内容不同
3. CI 产物有保留期限（通常 30-90 天），链接会过期

**证据文件占位符/草稿策略**：

| 场景 | 策略 | 说明 |
|------|------|------|
| **正式证据** | 使用 `record_iteration_evidence.py` 脚本生成 | ✅ 推荐，自动脱敏、格式合规 |
| **占位符/草稿** | ❌ **禁止提交到版本库** | 模板仅供参考，不要手动创建草稿文件提交 |
| **测试/调试** | 使用 `--dry-run` 预览 | 脚本支持预览模式，不实际写入 |

**推荐流程**：

```
1. 运行门禁命令（make ci 等）
            ↓
2. 使用 record_iteration_evidence.py 生成证据
            ↓
3. 验证生成的 JSON 符合 schema
            ↓
4. 提交到版本库（git add docs/acceptance/evidence/）
```

**禁止的做法**：

- ❌ 手动创建包含 `{PLACEHOLDER}` 或 `TODO` 的草稿证据文件并提交
- ❌ 复制模板文件到 `evidence/` 目录并手动编辑
- ❌ 提交不完整或未脱敏的证据文件

**模板用途**：`docs/acceptance/_templates/iteration_evidence.template.json` 仅用于：

- 文档参考（了解字段含义）
- 脚本开发参考
- **不用于直接复制生成证据文件**

**可执行清单（生成合规证据文件）**：

照做以下步骤即可得到符合规范的版本化证据文件：

```bash
# ====== 步骤 1：运行门禁命令 ======
make ci
# 记录运行结果（退出码、关键输出）

# ====== 步骤 2：生成证据文件 ======
# 基本用法（自动获取 commit sha）
python scripts/iteration/record_iteration_evidence.py <N>

# 推荐：指定 CI 运行 URL
python scripts/iteration/record_iteration_evidence.py <N> \
  --ci-run-url https://github.com/<org>/<repo>/actions/runs/<run_id>

# 可选：传入命令执行结果
python scripts/iteration/record_iteration_evidence.py <N> \
  --commands '{"make ci": {"exit_code": 0, "summary": "passed"}}'

# ====== 步骤 3：验证 schema 合规 ======
# 可选但推荐：使用 jsonschema 校验
python -c "
import json
from jsonschema import validate
with open('schemas/iteration_evidence_v1.schema.json') as f:
    schema = json.load(f)
with open('docs/acceptance/evidence/iteration_<N>_evidence.json') as f:
    data = json.load(f)
validate(data, schema)
print('✅ Schema 校验通过')
"

# ====== 步骤 4：提交到版本库 ======
git add docs/acceptance/evidence/iteration_<N>_evidence.json
git commit -m "evidence: Iteration <N> 验收证据"
```

**检查清单**：

| # | 检查项 | 通过标准 |
|---|--------|----------|
| 1 | 文件命名 | `iteration_<N>_evidence.json`（N 为整数） |
| 2 | 存储位置 | `docs/acceptance/evidence/` 目录下 |
| 3 | Schema 声明 | 包含 `"$schema": "..."` 字段 |
| 4 | 必填字段 | `iteration_number`, `recorded_at`, `commit_sha`, `runner`, `commands` |
| 5 | 敏感信息 | 无 PASSWORD/DSN/TOKEN 等敏感值（脚本自动脱敏） |
| 6 | 格式 | JSON 格式正确，缩进 2 空格 |

#### 3.6 证据引用规范

**推荐引用格式**：

| 证据来源 | 引用格式 | 示例 |
|----------|----------|------|
| 版本化文档 | 相对路径 Markdown 链接 | `[计划](iteration_5_plan.md)` |
| 版本化证据文件 | 相对路径 Markdown 链接 | `[证据](evidence/iteration_13_evidence.json)` |
| CI Run | 完整 URL | `[CI #1234](https://github.com/.../runs/1234)` |
| CI Artifact | URL + 说明 | `报告见 CI Artifacts (90 天有效)` |
| 本地草稿 (`.iteration/`) | **禁止链接**，仅文本提及 | `参考 .iteration/ 中的草稿` |
| 运行时产物 (`.artifacts/`) | **禁止链接**，仅文本提及 | `本地产物位于 .artifacts/` |

**推荐的证据引用方式**（按优先级）：

1. **CI Run URL**：最推荐，永久有效且可追溯
   ```markdown
   > **CI Run**: [GitHub Actions #1234](https://github.com/.../actions/runs/1234)
   ```

2. **版本化证据文件链接**：适用于需要结构化数据的场景
   ```markdown
   详细证据见 [iteration_13_evidence.json](evidence/iteration_13_evidence.json)
   ```

3. **文档内嵌摘要**：适用于关键信息需要在文档中直接可见的场景
   ```markdown
   ## 验收证据

   | 门禁 | 状态 | 说明 |
   |------|------|------|
   | `make ci` | ✅ PASS | 全部通过 |
   | `make test` | ✅ PASS | 608 passed, 0 failed |
   ```

---

### 4. 迁移与回滚策略

#### 4.1 从旧文档迁移

**迁移场景**：将 `docs/architecture/iteration_X_plan.md` 迁移到 `docs/acceptance/`。

**迁移步骤**：

1. **创建目标文档**
   ```bash
   cp docs/architecture/iteration_2_plan.md docs/acceptance/iteration_2_plan.md
   ```

2. **更新内容格式**（按模板调整）

3. **创建 Stub**（在原位置）
   ```markdown
   # Iteration 2 计划

   > **Canonical 文档**: [docs/acceptance/iteration_2_plan.md](../acceptance/iteration_2_plan.md)
   >
   > 本文件为 stub，请参阅上述 canonical 文档获取最新内容。
   >
   > **迁移日期**: 2026-02-01
   ```

4. **更新引用**
   ```bash
   # 搜索所有指向旧路径的链接
   rg -l 'architecture/iteration_2_plan' docs/
   # 更新链接指向新位置
   ```

5. **验证**
   ```bash
   make docs-check  # 验证链接有效性
   ```

#### 4.2 Stub 保留策略

| 条件 | 处理方式 |
|------|----------|
| 外部有链接指向旧路径 | 保留 Stub，等待引用更新 |
| 仅内部链接 | 更新链接后可删除 Stub |
| 高频访问路径 | 保留 Stub 至少 1 个迭代周期 |

#### 4.3 回滚策略

**如需回滚迁移**：

1. 恢复原文档内容（从 Git 历史）
2. 删除或更新 Stub
3. 更新 `docs/acceptance/` 中的文档为 Stub（指向原位置）
4. 在 ADR 中记录回滚原因

---

### 5. 编号与晋升决策

#### 5.1 编号规则

| 规则 | 名称 | 说明 |
|------|------|------|
| **A** | **SSOT 编号不复用** | `docs/acceptance/` 中已存在的 Iteration N 编号，无论状态（PASS/PARTIAL/SUPERSEDED/...），均不可被新迭代复用 |
| **B** | **草稿编号约束** | `.iteration/<N>/` 仅允许用于**尚未晋升**的同编号草稿；若 N 已在 `docs/acceptance/` 出现（无论是否 superseded），草稿必须改用新编号 |
| **C** | **晋升条件** | 若本轮产物需要**版本化、团队对齐、可审计**，则应晋升为新 Iteration（建议取 next available N） |
| **D** | **禁止修改旧文档承载新产物** | 不得通过修改旧 superseded 文档来承载新迭代产物；应创建新 Iteration 并在新文档中引用历史背景 |

#### 5.2 编号分配示例

**场景 1：新功能迭代**

假设当前 `docs/acceptance/` 最高编号为 Iteration 10：

```
当前状态:
  docs/acceptance/iteration_10_regression.md  # 状态: ⚠️ PARTIAL

新迭代应使用:
  .iteration/11/plan.md                       # 本地草稿
  .iteration/11/regression.md
        ↓ 晋升后
  docs/acceptance/iteration_11_plan.md        # SSOT
  docs/acceptance/iteration_11_regression.md
```

**场景 2：草稿编号冲突**

假设 `.iteration/9/` 存在草稿，但 Iteration 9 已在 SSOT 中（状态 SUPERSEDED）：

```
❌ 错误做法:
  .iteration/9/plan.md 晋升到 docs/acceptance/iteration_9_plan.md
  # 违反规则 A：编号已被使用

✅ 正确做法:
  重命名 .iteration/9/ 为 .iteration/11/
  然后晋升到 docs/acceptance/iteration_11_plan.md
```

**场景 3：延续旧迭代工作**

若需要延续 Iteration 9 的未完成工作：

```
❌ 错误做法:
  修改 docs/acceptance/iteration_9_regression.md 添加新内容
  # 违反规则 D：不可修改旧文档承载新产物

✅ 正确做法:
  创建 docs/acceptance/iteration_11_plan.md
  在新文档中引用: "延续 [Iteration 9](iteration_9_regression.md) 的工作..."
```

#### 5.3 查询下一可用编号

```bash
# 查看当前最高编号
ls docs/acceptance/iteration_*_*.md | \
  sed -E 's/.*iteration_([0-9]+)_.*/\1/' | \
  sort -n | tail -1

# 输出示例: 10
# 下一可用编号: 11
```

#### 5.4 晋升决策流程图

```
是否需要版本化/团队对齐/可审计？
    │
    ├─ 否 → 保留在 .iteration/ 本地草稿
    │
    └─ 是 → 检查目标编号 N
              │
              ├─ N 已在 docs/acceptance/ 出现？
              │     │
              │     ├─ 是 → 分配新编号 (next available)
              │     │
              │     └─ 否 → 使用原编号 N 晋升
              │
              └─ 创建 docs/acceptance/iteration_N_{plan,regression}.md
                 更新 00_acceptance_matrix.md 索引
```

---

### 6. SSOT 边界与非 SSOT 文档定义

#### 6.1 SSOT 边界定义

**迭代相关文档的 SSOT（Single Source of Truth）边界如下**：

| 文档类型 | SSOT 位置 | 权威性 | 说明 |
|----------|-----------|--------|------|
| **迭代索引（跨迭代状态）** | `docs/acceptance/00_acceptance_matrix.md` | **唯一权威** | 所有迭代的状态、链接、说明的单一来源 |
| **迭代计划** | `docs/acceptance/iteration_<N>_plan.md` | **唯一权威** | 特定迭代的目标、范围、验收门禁 |
| **迭代回归记录** | `docs/acceptance/iteration_<N>_regression.md` | **唯一权威** | 特定迭代的回归测试执行记录 |

#### 6.2 非 SSOT 文档（辅助索引）

以下文档**不是 SSOT**，仅作为辅助视图或快速参考：

| 文档 | 位置 | 定位 | 与 SSOT 的关系 |
|------|------|------|----------------|
| **迭代/变更日志** | `docs/dev/iteration_changelog.md` | **辅助索引** | 提供按日期窗口的变更摘要视图；状态、链接以 `00_acceptance_matrix.md` 为准 |
| **本地草稿** | `.iteration/<N>/...` | **非 SSOT** | 仅本地临时笔记，不纳入版本控制，不可链接 |
| **审计报告草稿** | `docs/acceptance/_drafts/` | **非 SSOT（历史样例）** | 一次性审计快照，仅作为历史样例保留；新审计请使用脚本生成 |
| **审计报告输出** | `.artifacts/iteration-audit/` | **非 SSOT** | 脚本生成的审计报告，不纳入版本控制 |

#### 6.3 审计工具与报告

迭代文档审计有两种方式：

| 工具 | 用途 | 命令 |
|------|------|------|
| **CI 门禁检查** | 自动化检查 SUPERSEDED 一致性（阻断式） | `make check-iteration-docs` |
| **审计报告脚本** | 生成完整审计报告（非阻断） | `make iteration-audit` |

**审计脚本用法**：

```bash
# 输出到 stdout
python scripts/iteration/audit_iteration_docs.py

# 输出到文件
python scripts/iteration/audit_iteration_docs.py --output-dir .artifacts/iteration-audit

# 详细模式
python scripts/iteration/audit_iteration_docs.py --verbose
```

**注意**：审计报告为一次性快照，**不是 SSOT**。CI 门禁以 `check_no_iteration_links_in_docs.py` 为准。

#### 6.3 索引一致性规则

为避免多个索引长期互相矛盾，遵循以下规则：

| 规则 | 要求 |
|------|------|
| **R-SSOT-1: 状态以 SSOT 为准** | 迭代状态（PASS/PARTIAL/SUPERSEDED 等）的权威来源是 `00_acceptance_matrix.md`，其他文档引用时应使用相对链接而非复制状态 |
| **R-SSOT-2: 辅助索引不新增状态** | `iteration_changelog.md` 等辅助文档可聚合/摘要信息，但不得定义新的状态值或与 SSOT 矛盾的状态 |
| **R-SSOT-3: 变更时同步更新** | 当迭代状态变更（如标记为 SUPERSEDED）时，**必须首先更新 SSOT**（`00_acceptance_matrix.md` + regression 头部），辅助索引的更新为可选 |
| **R-SSOT-4: 辅助索引声明来源** | 辅助索引文档应在顶部声明 SSOT 来源，例如 `> **单一来源**：各迭代详细记录位于 docs/acceptance/iteration_*_regression.md` |

#### 6.4 iteration_changelog 定位说明

`docs/dev/iteration_changelog.md` 的定位：

- **用途**：提供按日期窗口聚合的变更摘要，便于快速浏览近期变更
- **非 SSOT**：不作为迭代状态的权威来源
- **更新策略**：
  - 状态变更时**可选**同步更新（非强制）
  - 若与 SSOT 矛盾，以 `00_acceptance_matrix.md` 为准
  - 长期不更新不影响 CI 门禁

---

## 后果

### 正面

- **SSOT 明确**：迭代文档统一在 `docs/acceptance/`，便于查找
- **本地隔离**：`.iteration/` 草稿不会污染版本控制
- **证据可追溯**：明确哪些证据版本化、如何引用 CI artifacts
- **平滑迁移**：Stub 策略保持向后兼容

### 负面

- **目录调整**：现有 `docs/architecture/iteration_X_plan.md` 需迁移
- **学习成本**：团队需了解新的命名规范和引用约束

### 风险与缓解

| 风险 | 缓解措施 |
|------|----------|
| 迁移遗漏 | CI 检查 `docs/architecture/iteration_*` 不应存在 |
| `.iteration/` 链接泄漏 | CI 检查版本化文档中不得有 `.iteration/` 链接 |
| Stub 链接失效 | 定期审计 Stub 指向 |

---

## 合规检查

### CI 已实现检查

以下检查已在 CI 中实现并自动执行：

| 检查项 | Makefile 目标 | CI Job | 脚本 |
|--------|---------------|--------|------|
| `.iteration/` 链接禁止 + SUPERSEDED 一致性 | `make check-iteration-docs` | `docs-iteration-links` | `scripts/ci/check_no_iteration_links_in_docs.py` |
| 仅 SUPERSEDED 一致性检查 | `make check-iteration-docs-superseded-only` | - | `scripts/ci/check_no_iteration_links_in_docs.py --superseded-only` |

### 本地执行

```bash
# 一键执行所有迭代文档检查（全量：.iteration/ 链接 + SUPERSEDED 一致性）
make check-iteration-docs

# 仅执行 SUPERSEDED 一致性检查
make check-iteration-docs-superseded-only

# 或直接调用脚本（全量检查）
python scripts/ci/check_no_iteration_links_in_docs.py --verbose

# 仅执行 SUPERSEDED 一致性检查（脚本方式）
python scripts/ci/check_no_iteration_links_in_docs.py --superseded-only --verbose

# 跳过 SUPERSEDED 检查（仅 .iteration/ 链接检查）
python scripts/ci/check_no_iteration_links_in_docs.py --skip-superseded-check --verbose
```

### SUPERSEDED 一致性检查规则

参见 [00_acceptance_matrix.md](../acceptance/00_acceptance_matrix.md) 中的「SUPERSEDED 一致性规则」节：

| 规则 | 要求 | 示例 |
|------|------|------|
| **R1: 后继链接必须存在** | 说明字段必须包含"已被 Iteration X 取代" | `已被 Iteration 10 取代` ✅ |
| **R2: 后继必须在索引表中** | 被引用的后继迭代必须已在本索引表中存在 | Iteration 10 必须有对应行 |
| **R3: 后继排序在上方** | 后继迭代在表格中的位置必须在被取代迭代上方 | Iteration 10 行在 Iteration 9 行之上 |
| **R4: 禁止环形引用** | 不允许 A→B→A 的循环取代链 | `9→10→9` ❌ |
| **R5: 禁止多后继** | 每个迭代只能有一个直接后继 | `已被 Iteration 10 和 11 取代` ❌ |
| **R6: regression 文档头部声明** | regression 文件顶部必须有标准 superseded 声明区块 | 见下方 R6 格式规范 |

#### R6 格式规范与示例

当迭代状态变更为 `🔄 SUPERSEDED` 时，对应的 `iteration_N_regression.md` 文件**前 20 行内**必须包含关键短语 `Superseded by Iteration M`，且后继编号 M 必须与索引表一致。

**CI 检查逻辑**（`scripts/ci/check_no_iteration_links_in_docs.py::check_regression_file_superseded_header`）：
1. 扫描文件前 20 行
2. 使用正则 `Superseded\s+by\s+Iteration\s*(\d+)`（不区分大小写）匹配
3. 验证声明中的后继编号与索引表一致

**推荐格式**（在标题之前添加，以便读者第一时间看到）：

```markdown
> **⚠️ Superseded by Iteration M**
>
> 本迭代已被 [Iteration M](iteration_M_regression.md) 取代，不再维护。
> 请参阅后续迭代的回归记录获取最新验收状态。

---

# Iteration N 回归验证
（原有内容）
```

**格式约束**：

| 约束 | 要求 |
|------|------|
| **位置** | 文件前 20 行内（推荐在标题之前，以便读者第一时间看到） |
| **格式** | 使用 blockquote（`>`）包裹 |
| **关键短语** | 必须包含 `Superseded by Iteration M` 字样（M 为后继迭代编号） |
| **后继链接** | **必须**使用相对路径 `[Iteration M](iteration_M_regression.md)` 格式，指向实际存在的后继迭代回归记录 |
| **编号一致性** | M 必须与索引表「说明」字段声明的后继编号一致 |
| **分隔线** | 声明后建议添加 `---` 分隔线，与原有内容分隔 |

> **统一示例来源**：本格式规范与 [iteration_regression.template.md](../acceptance/_templates/iteration_regression.template.md) 保持一致。CI 检查使用 `scripts/ci/check_no_iteration_links_in_docs.py` 的 `check_regression_file_superseded_header` 函数验证。

**历史写法兼容性**：

CI 检查使用宽松正则 `Superseded\s+by\s+Iteration\s*(\d+)`（忽略大小写），可兼容以下历史变体：
- `Superseded by Iteration 10` ✅
- `superseded by iteration10` ✅
- `Superseded  by  Iteration  10` ✅

但**新建文件必须使用上述标准格式**。历史文件如不符合标准格式，建议在下次编辑时一并更新为标准格式（无兼容窗口，直接迁移）。

### 手动检查项（建议）

```bash
# 1. 检查迭代文档命名规范
ls docs/acceptance/iteration_*.md | grep -v -E 'iteration_[0-9]+_(plan|regression)\.md' && echo "FAIL: 命名不规范"

# 2. 检查旧位置迭代文档（应迁移或为 Stub）
for f in docs/architecture/iteration_*_plan.md; do
  head -5 "$f" | grep -q 'Canonical' || echo "WARN: $f 应迁移或转为 Stub"
done
```

---

## 参考

- [adr_docs_information_architecture.md](adr_docs_information_architecture.md) - 文档信息架构
- [docs_legacy_retention_policy.md](docs_legacy_retention_policy.md) - 遗留资产保留策略
- [docs/acceptance/00_acceptance_matrix.md](../acceptance/00_acceptance_matrix.md) - 验收测试矩阵

---

## 变更记录

| 日期 | 版本 | 变更内容 |
|------|------|----------|
| 2026-02-01 | v1.0 | 初始版本：定义迭代文档 SSOT、`.iteration/` 定位、证据策略、迁移流程 |
| 2026-02-01 | v1.1 | 增补「编号与晋升决策」章节：规则 A/B/C/D、编号分配示例、晋升决策流程图 |
| 2026-02-01 | v1.2 | 增补「合规检查」章节：记录 CI 已实现检查（.iteration/ 链接 + SUPERSEDED 一致性）、本地执行命令、SUPERSEDED 规则 R1-R6 |
| 2026-02-01 | v1.3 | 增补「R6 格式规范与示例」：统一文档头部锚点格式，与 template 和 workflow 保持一致 |
| 2026-02-01 | v1.4 | 增补「SSOT 边界与非 SSOT 文档定义」章节：定义 SSOT 边界、非 SSOT 文档（如 iteration_changelog）定位、索引一致性规则 R-SSOT-1~4 |
| 2026-02-02 | v1.5 | 增补「审计工具与报告」：定义 `_drafts/` 为历史样例（非 SSOT）、新增 `audit_iteration_docs.py` 脚本生成报告 |
| 2026-02-02 | v1.6 | 统一 R6 格式规范：前 20 行内必须包含 `Superseded by Iteration M`、后继编号与索引表一致、与 CI 脚本实现对齐 |
| 2026-02-02 | v1.7 | 增补「2.2 引用约束」：添加引用类型表格、明确禁止项（Markdown 链接）与允许项（文本/inline code 提及） |
| 2026-02-02 | v1.8 | 更新审计工具命令：`make iteration-audit` 替代直接脚本调用 |
| 2026-02-02 | v1.8 | 增补「2.4 跨人协作与可链接引用」：声明 `.iteration/` 存在性不保证、定义跨人协作场景的入口选择（SSOT/导出分享包/文本提示） |
| 2026-02-02 | v1.9 | 增补「3.4 Nightly 审计报告策略」：定义 nightly 审计报告定位（非 SSOT、仅观察、90 天保留） |
| 2026-02-02 | v1.10 | 增补「3.5 版本化证据文件」：定义 `docs/acceptance/evidence/` 版本化证据文件规范，明确 `.artifacts/` 引用约束（与 `.iteration/` 一致禁止 Markdown 链接），扩展证据引用推荐格式 |
| 2026-02-02 | v1.11 | 增补证据格式 SSOT：引用 `schemas/iteration_evidence_v1.schema.json` 作为版本化证据文件的格式 SSOT，添加 Schema 约束摘要表格及安全约束说明 |
| 2026-02-02 | v1.12 | 收敛「3.5 版本化证据文件」命名规范为统一套（canonical + snapshot）、添加回归文档引用策略、补充占位符/草稿策略及可执行清单 |
