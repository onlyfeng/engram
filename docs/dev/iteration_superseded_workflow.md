# 迭代 SUPERSEDED 工作流 Runbook

本文档说明如何将旧迭代标记为 SUPERSEDED，以及如何创建新迭代承载新增产物。

---

## 快速参考

| 操作 | 涉及文件 | 关键锚点 |
|------|----------|----------|
| 更新索引表 | [00_acceptance_matrix.md](../acceptance/00_acceptance_matrix.md) | `## 迭代回归记录索引` |
| 添加 Superseded 声明 | `iteration_N_regression.md` | 文件顶部（`---` 之前） |
| 查看模板格式 | [iteration_regression.template.md](../acceptance/_templates/iteration_regression.template.md) | `## Superseded by …（可选区块）` |
| 编号规则 | [adr_iteration_docs_workflow.md](../architecture/adr_iteration_docs_workflow.md) | `### 5. 编号与晋升决策` |
| SUPERSEDED 一致性规则 | [00_acceptance_matrix.md](../acceptance/00_acceptance_matrix.md) | `#### SUPERSEDED 一致性规则` |

---

## 1. 将旧迭代标记为 SUPERSEDED

### 步骤 1.1：确认后继迭代已存在

**前置条件**：后继迭代（Iteration M）必须已在 `docs/acceptance/` 中存在，且已添加到索引表。

```bash
# 检查后继迭代文件是否存在
ls docs/acceptance/iteration_M_regression.md

# 检查索引表中是否有后继迭代条目
grep "Iteration M" docs/acceptance/00_acceptance_matrix.md
```

> **重要**：若后继迭代尚未创建，必须先创建后继迭代（见第 2 节），再执行 SUPERSEDED 标记。

### 步骤 1.2：在旧迭代文档头部添加 Superseded 声明

在 `docs/acceptance/iteration_N_regression.md` 文件**最开头**（任何其他内容之前）添加：

```markdown
> **⚠️ Superseded by Iteration M**
>
> 本迭代已被 [Iteration M](iteration_M_regression.md) 取代，不再维护。
> 请参阅后续迭代的回归记录获取最新验收状态。

---
```

**格式约束**（来源：[adr_iteration_docs_workflow.md](../architecture/adr_iteration_docs_workflow.md) R6 规则）：

| 约束 | 要求 |
|------|------|
| **位置** | 文件最开头，在任何其他内容（包括标题）之前 |
| **格式** | 使用 blockquote（`>`）包裹 |
| **标识符** | 必须包含 `Superseded by Iteration M` 字样（M 为后继迭代编号） |
| **后继链接** | 必须使用相对路径 `[Iteration M](iteration_M_regression.md)` 格式 |
| **分隔线** | 声明后必须添加 `---` 分隔线，与原有内容分隔 |

### 步骤 1.3：更新索引表

在 [00_acceptance_matrix.md](../acceptance/00_acceptance_matrix.md) 的「迭代回归记录索引」表中：

1. **修改旧迭代状态**为 `🔄 SUPERSEDED`
2. **说明字段**添加后继链接文本：`已被 Iteration M 取代`

```markdown
| 迭代 | 日期 | 状态 | 计划 | 详细记录 | 说明 |
|------|------|------|------|----------|------|
| **Iteration M** | 2026-02-01 | ⚠️ PARTIAL | - | [iteration_M_regression.md](...) | 当前活跃迭代 |
| Iteration N | 2026-02-01 | 🔄 SUPERSEDED | - | [iteration_N_regression.md](...) | 已被 Iteration M 取代 |
```

**排序要求**（来源：[00_acceptance_matrix.md](../acceptance/00_acceptance_matrix.md#superseded-一致性规则) R3）：

- **后继迭代必须在被取代迭代的上方**
- 索引表按迭代编号降序排列（最新在最前）

### 步骤 1.4：验证一致性（必须）

> **重要**：此步骤为必须执行，确保 SUPERSEDED 标记符合所有规则约束。

```bash
# 运行 SUPERSEDED 一致性检查（必须）
make check-iteration-docs

# 或直接调用脚本
python scripts/ci/check_no_iteration_links_in_docs.py --superseded-only --verbose
```

检查项（来源：[00_acceptance_matrix.md](../acceptance/00_acceptance_matrix.md#superseded-一致性规则)）：

| 规则 | 检查内容 |
|------|----------|
| R1 | 说明字段包含"已被 Iteration X 取代" |
| R2 | 后继迭代在索引表中存在 |
| R3 | 后继迭代排在被取代迭代上方 |
| R4 | 无环形引用（A→B→A） |
| R5 | 无多后继（仅一个直接后继） |
| R6 | regression 文件有 `Superseded by Iteration X` 声明（CI 脚本正则匹配） |

---

## 2. 创建新迭代承载新增产物

### 原则

> **禁止修改旧文档承载新产物**（来源：[adr_iteration_docs_workflow.md](../architecture/adr_iteration_docs_workflow.md#51-编号规则) 规则 D）
>
> 不得通过修改旧 superseded 文档来承载新迭代产物；应创建新 Iteration 并在新文档中引用历史背景。

### 步骤 2.1：查询下一可用编号

```bash
# 获取当前最高编号
CURRENT_MAX=$(ls docs/acceptance/iteration_*_*.md 2>/dev/null | \
  sed -E 's/.*iteration_([0-9]+)_.*/\1/' | sort -n | tail -1)
echo "当前最高编号: ${CURRENT_MAX:-0}"

# 下一可用编号
NEXT_N=$((${CURRENT_MAX:-0} + 1))
echo "下一可用编号: $NEXT_N"
```

### 步骤 2.2：创建新迭代文档

**方式 A：使用初始化脚本（推荐）**

```bash
# 在本地草稿中初始化
python scripts/iteration/init_local_iteration.py $NEXT_N

# 脚本会自动检测 SSOT 冲突
```

**方式 B：直接创建 SSOT 文档**

```bash
# 从模板复制
cp docs/acceptance/_templates/iteration_plan.template.md \
   docs/acceptance/iteration_${NEXT_N}_plan.md
cp docs/acceptance/_templates/iteration_regression.template.md \
   docs/acceptance/iteration_${NEXT_N}_regression.md
```

### 步骤 2.3：在新文档中引用旧迭代背景

在新迭代计划或回归记录中，使用**引用**而非修改旧文档：

```markdown
## 背景

本迭代延续 [Iteration N](iteration_N_regression.md) 的未完成工作，
重点解决以下遗留问题：

- 问题 1（来自 Iteration N）
- 问题 2（来自 Iteration N）
```

### 步骤 2.4：更新索引表

在 [00_acceptance_matrix.md](../acceptance/00_acceptance_matrix.md) 索引表**最上方**添加新迭代条目：

```markdown
| 迭代 | 日期 | 状态 | 计划 | 详细记录 | 说明 |
|------|------|------|------|----------|------|
| **Iteration M** | YYYY-MM-DD | 🔄 PLANNING | [iteration_M_plan.md](...) | [iteration_M_regression.md](...) | 新迭代说明 |
```

### 步骤 2.5：（可选）将旧迭代标记为 SUPERSEDED

若新迭代完全取代旧迭代，按第 1 节步骤将旧迭代标记为 SUPERSEDED。

### 步骤 2.6：执行最终验证

```bash
# 执行迭代文档一致性检查（必须）
make check-iteration-docs

# 或直接调用脚本
python scripts/ci/check_no_iteration_links_in_docs.py --verbose
```

> **重要**：所有迭代文档修改完成后，必须执行 `make check-iteration-docs` 验证，确保无 R1-R6 规则违规。

---

## 3. 常见坑与避免方法

### 坑 1：编号复用

**错误示例**：

```
❌ docs/acceptance/iteration_9_regression.md 已存在（状态 SUPERSEDED）
❌ 但仍创建 .iteration/9/regression.md 并尝试晋升
```

**避免方法**：

- 使用 `init_local_iteration.py` 脚本，自动检测冲突
- 晋升前执行：`ls docs/acceptance/iteration_${N}_*.md`

**规则来源**：[adr_iteration_docs_workflow.md](../architecture/adr_iteration_docs_workflow.md#51-编号规则) 规则 A/B

### 坑 2：链接到 .iteration/ 目录

**错误示例**：

```markdown
❌ 详见 [草稿笔记](.iteration/11/notes.md)
```

**正确做法**：

```markdown
✅ 详见 [Iteration 11 回归记录](iteration_11_regression.md)
```

**避免方法**：

- CI 自动检查：`make check-iteration-docs`
- `.iteration/` 不在版本控制中，链接必然失效

**规则来源**：[adr_iteration_docs_workflow.md](../architecture/adr_iteration_docs_workflow.md#22-引用约束)

### 坑 3：索引排序错误

**错误示例**（后继在下方）：

```markdown
❌
| Iteration 7 | ... | 🔄 SUPERSEDED | ... | 已被 Iteration 9 取代 |
| Iteration 9 | ... | ⚠️ PARTIAL    | ... | 当前活跃迭代 |
```

**正确排序**（后继在上方）：

```markdown
✅
| Iteration 9 | ... | ⚠️ PARTIAL    | ... | 当前活跃迭代 |
| Iteration 7 | ... | 🔄 SUPERSEDED | ... | 已被 Iteration 9 取代 |
```

**避免方法**：

- 索引表按编号**降序**排列
- 新增迭代总是插入表格**最上方**

**规则来源**：[00_acceptance_matrix.md](../acceptance/00_acceptance_matrix.md#superseded-一致性规则) R3

### 坑 4：多个后继

**错误示例**：

```markdown
❌ | Iteration 7 | ... | 🔄 SUPERSEDED | ... | 已被 Iteration 9 和 10 取代 |
```

**正确做法**（单一后继链）：

```markdown
✅ | Iteration 7 | ... | 🔄 SUPERSEDED | ... | 已被 Iteration 9 取代 |
✅ | Iteration 9 | ... | 🔄 SUPERSEDED | ... | 已被 Iteration 10 取代 |
```

**避免方法**：

- 每个迭代只能有**一个直接后继**
- 若需要分支，创建独立的迭代编号

**规则来源**：[00_acceptance_matrix.md](../acceptance/00_acceptance_matrix.md#superseded-一致性规则) R5

### 坑 5：环形引用

**错误示例**：

```markdown
❌ Iteration 9 → Iteration 10 → Iteration 9（循环）
```

**避免方法**：

- CI 检查会自动检测环形引用
- 确保 SUPERSEDED 链是单向的

**规则来源**：[00_acceptance_matrix.md](../acceptance/00_acceptance_matrix.md#superseded-一致性规则) R4

### 坑 6：缺少 Superseded 声明

**错误示例**：

```markdown
❌ 索引表标记为 SUPERSEDED，但 regression 文件头部无 Superseded 声明
```

**正确格式**（必须包含 `Superseded by Iteration X`）：

```markdown
> **⚠️ Superseded by Iteration 10**
>
> 本迭代已被 [Iteration 10](iteration_10_regression.md) 取代，不再维护。
```

**避免方法**：

- 两处必须同步更新：索引表 + 文档头部
- 声明必须包含 `Superseded by Iteration X` 字样（CI 脚本 R6 规则会检测此格式）
- 参考 R6 格式规范：[adr_iteration_docs_workflow.md](../architecture/adr_iteration_docs_workflow.md#r6-格式规范与示例)
- 模板示例：[iteration_regression.template.md](../acceptance/_templates/iteration_regression.template.md#superseded-by-可选区块)

**规则来源**：[00_acceptance_matrix.md](../acceptance/00_acceptance_matrix.md#r6-文档头部锚点格式规范) R6

---

## 4. 完整操作示例

### 场景：Iteration 10 取代 Iteration 9

**步骤 1**：确认 Iteration 10 已存在

```bash
ls docs/acceptance/iteration_10_regression.md  # ✅ 存在
grep "Iteration 10" docs/acceptance/00_acceptance_matrix.md  # ✅ 已在索引
```

**步骤 2**：在 `iteration_9_regression.md` 头部添加声明

```markdown
> **⚠️ Superseded by Iteration 10**
>
> 本迭代已被 [Iteration 10](iteration_10_regression.md) 取代，不再维护。
> 请参阅后续迭代的回归记录获取最新验收状态。

---

# Iteration 9 Regression - CI 流水线验证记录
（原有内容保持不变）
```

**步骤 3**：更新 `00_acceptance_matrix.md` 索引表

```markdown
| 迭代 | 日期 | 状态 | 计划 | 详细记录 | 说明 |
|------|------|------|------|----------|------|
| Iteration 10 | 2026-02-01 | ⚠️ PARTIAL | - | [iteration_10_regression.md](...) | 当前活跃迭代 |
| Iteration 9 | 2026-02-01 | 🔄 SUPERSEDED | - | [iteration_9_regression.md](...) | 已被 Iteration 10 取代 |
```

**步骤 4**：验证（必须执行）

```bash
# 执行迭代文档一致性检查
make check-iteration-docs
# 预期：全部通过，无 R1-R6 违规
```

**步骤 5**：提交

```bash
git add docs/acceptance/iteration_9_regression.md docs/acceptance/00_acceptance_matrix.md
git commit -m "docs: 将 Iteration 9 标记为 SUPERSEDED，被 Iteration 10 取代"
```

---

## 5. CI 检查命令

| 命令 | 说明 |
|------|------|
| `make check-iteration-docs` | 一键执行所有迭代文档检查 |
| `python scripts/ci/check_no_iteration_links_in_docs.py --verbose` | 完整检查（含 .iteration 链接） |
| `python scripts/ci/check_no_iteration_links_in_docs.py --superseded-only --verbose` | 仅 SUPERSEDED 一致性检查 |

---

## 相关文档

| 文档 | 说明 |
|------|------|
| [adr_iteration_docs_workflow.md](../architecture/adr_iteration_docs_workflow.md) | 迭代文档工作流 ADR |
| [00_acceptance_matrix.md](../acceptance/00_acceptance_matrix.md) | 验收测试矩阵（索引表 SSOT） |
| [iteration_regression.template.md](../acceptance/_templates/iteration_regression.template.md) | 回归记录模板 |
| [iteration_local_drafts.md](iteration_local_drafts.md) | 本地草稿管理指南 |

---

_更新时间：2026-02-01（统一 Superseded by Iteration X 格式，与 CI 脚本 R6 逻辑一致）_
