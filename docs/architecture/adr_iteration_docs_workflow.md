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

**允许的引用方式**：

```markdown
<!-- 在代码注释或口头交流中 -->
# 参考本地 .iteration/notes.md 中的草稿思路
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

#### 2.3 迁移路径

当 `.iteration/` 中的草稿成熟时，应迁移到 SSOT 位置：

```
.iteration/iteration_5_draft.md
        ↓ 成熟后迁移
docs/acceptance/iteration_5_plan.md
```

---

### 3. 证据策略

#### 3.1 证据分类

| 类别 | 存储位置 | 版本化 | SSOT | 示例 |
|------|----------|--------|------|------|
| **规范性文档** | `docs/` | 是 | 是 | 迭代计划、回归记录、ADR |
| **测试结果快照** | `docs/` 或 PR 描述 | 是 | 是 | 关键测试输出的文本摘要 |
| **CI Artifacts** | CI 系统 | 否 | 否 | 完整测试报告、覆盖率报告、构建日志 |
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

#### 3.4 证据引用规范

| 证据来源 | 引用格式 | 示例 |
|----------|----------|------|
| 版本化文档 | 相对路径 Markdown 链接 | `[计划](iteration_5_plan.md)` |
| CI Run | 完整 URL | `[CI #1234](https://github.com/.../runs/1234)` |
| CI Artifact | URL + 说明 | `报告见 CI Artifacts (90 天有效)` |
| 本地草稿 | **禁止链接**，仅文本提及 | `参考 .iteration/ 中的草稿` |

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
| **审计报告脚本** | 生成完整审计报告（非阻断） | `python scripts/iteration/audit_iteration_docs.py` |

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

当迭代状态变更为 `🔄 SUPERSEDED` 时，对应的 `iteration_N_regression.md` 文件**顶部**（任何其他内容之前）必须添加以下标准声明：

```markdown
> **⚠️ Superseded by Iteration M**
>
> 本迭代已被 [Iteration M](iteration_M_regression.md) 取代，不再维护。
> 请参阅后续迭代的回归记录获取最新验收状态。

---
```

**格式约束**：

| 约束 | 要求 |
|------|------|
| **位置** | 文件最开头，在任何其他内容（包括标题）之前 |
| **格式** | 使用 blockquote（`>`）包裹 |
| **标识符** | 必须包含 `⚠️ Superseded by Iteration M` 字样（M 为后继迭代编号） |
| **后继链接** | 必须使用相对路径 `[Iteration M](iteration_M_regression.md)` 格式 |
| **分隔线** | 声明后必须添加 `---` 分隔线，与原有内容分隔 |

> **统一示例来源**：本格式规范与 [iteration_regression.template.md](../acceptance/_templates/iteration_regression.template.md) 和 [iteration_superseded_workflow.md](../dev/iteration_superseded_workflow.md) 保持一致。

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
