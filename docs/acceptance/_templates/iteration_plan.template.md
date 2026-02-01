# Iteration Plan 模板

> **使用说明**：复制本模板到 `docs/acceptance/iteration_N_plan.md`，替换 `{PLACEHOLDER}` 占位符。
>
> **索引关系**：创建计划后，需在 [00_acceptance_matrix.md](../00_acceptance_matrix.md) 的「迭代回归记录索引」表中添加对应条目。

---

## 必须字段

以下字段在创建迭代计划时**必须填写**：

| 字段 | 说明 | 示例 |
|------|------|------|
| **迭代编号** | Iteration N 格式 | `Iteration 4` |
| **开始日期** | YYYY-MM-DD 格式 | `2026-01-31` |
| **状态** | 🔄 PLANNING / ⚠️ PARTIAL / ✅ PASS / ❌ FAIL | `⚠️ PARTIAL (进行中)` |
| **SSOT** | 单一来源文档引用 | `本文档 + iteration_N_regression.md` |
| **迭代目标** | 至少 1 条主要目标 | 代码质量修复、测试重构等 |
| **验收门禁** | 至少列出必须通过的门禁 | `make lint`、`make typecheck-gate` |

---

## 推荐段落结构

```markdown
# Iteration {N} 计划

## 概述

| 字段 | 内容 |
|------|------|
| **迭代编号** | Iteration {N} |
| **开始日期** | {YYYY-MM-DD} |
| **状态** | {STATUS_EMOJI} {STATUS} |
| **SSOT** | 本文档 + [iteration_{N}_regression.md](iteration_{N}_regression.md) |

---

## 迭代目标

### 主要目标

1. **{目标1名称}**：{目标1描述}
2. **{目标2名称}**：{目标2描述}
3. **{目标3名称}**：{目标3描述}

### 范围边界

| 范围 | 包含 | 不包含 |
|------|------|--------|
| **{范围1}** | {包含内容} | {不包含内容} |
| **{范围2}** | {包含内容} | {不包含内容} |

---

## 验收门禁

### 必须通过的门禁

| 门禁 | 命令 | 通过标准 |
|------|------|----------|
| **格式检查** | `make format-check` | 退出码 0 |
| **Lint 检查** | `make lint` | 0 errors |
| **类型检查** | `make typecheck-gate` | baseline 模式下无新增错误 |
| **{其他门禁}** | `{命令}` | {通过标准} |

### 可选/降级门禁

| 门禁 | 命令 | 说明 |
|------|------|------|
| **{可选门禁}** | `{命令}` | {说明} |

---

## 证据要求

### 回归记录

每次验收执行后，需在 [iteration_{N}_regression.md](iteration_{N}_regression.md) 记录：

| 字段 | 说明 |
|------|------|
| **执行日期** | YYYY-MM-DD |
| **Commit** | 被验证的 commit SHA |
| **执行命令** | 实际运行的命令 |
| **结果** | PASS / PARTIAL / FAIL |
| **修复文件清单** | 本次修复的文件列表 |

### 产物目录

| 产物 | 路径 | 说明 |
|------|------|------|
| **回归记录** | `docs/acceptance/iteration_{N}_regression.md` | 版本化的回归记录 |
| **验收证据** | `docs/acceptance/evidence/iteration_{N}_evidence.json` | 结构化验收证据（符合 `iteration_evidence_v1.schema.json`） |
| **本地迭代笔记** | `.iteration/` | 本地化，不纳入版本控制 |

---

## 任务清单

### 已完成

- [x] {已完成任务1}
- [x] {已完成任务2}

### 进行中

- [ ] {进行中任务1}
- [ ] {进行中任务2}

### 待开始

- [ ] {待开始任务1}
- [ ] {待开始任务2}

---

## 风险评估

| 风险 | 等级 | 缓解措施 |
|------|------|----------|
| {风险1描述} | **高/中/低** | {缓解措施} |
| {风险2描述} | **高/中/低** | {缓解措施} |

---

## 相关文档

| 文档 | 说明 |
|------|------|
| [iteration_{N}_regression.md](iteration_{N}_regression.md) | 详细回归记录与修复清单 |
| [00_acceptance_matrix.md](00_acceptance_matrix.md) | 验收测试矩阵总览 |
| [{相关ADR}]({路径}) | {ADR 说明} |

---

更新时间：{YYYY-MM-DD}
```

---

## 与 00_acceptance_matrix.md 的索引关系

创建新的迭代计划后，需在 [00_acceptance_matrix.md](../00_acceptance_matrix.md) 的「迭代回归记录索引」表中添加条目：

```markdown
| 迭代 | 日期 | 状态 | 计划 | 详细记录 | 说明 |
|------|------|------|------|----------|------|
| **Iteration {N}** | {YYYY-MM-DD} | {STATUS} | [iteration_{N}_plan.md](iteration_{N}_plan.md) | [iteration_{N}_regression.md](iteration_{N}_regression.md) | {简要说明} |
```

**索引更新规则**：

1. **新迭代**：添加到表格最上方（最新迭代在最前）
2. **状态更新**：迭代状态变化时同步更新索引表中的状态字段
3. **被取代**：旧迭代被新迭代取代时，状态改为 `🔄 SUPERSEDED`

---

## 状态定义

| 状态 | Emoji | 说明 |
|------|-------|------|
| **PLANNING** | 🔄 | 计划阶段，尚未开始执行 |
| **PARTIAL** | ⚠️ | 部分完成，存在未修复的问题 |
| **PASS** | ✅ | 所有门禁通过，迭代完成 |
| **FAIL** | ❌ | 存在阻断性问题，迭代失败 |
| **SUPERSEDED** | 🔄 | 已被后续迭代取代 |

---

## 快速创建检查清单

- [ ] 复制本模板到 `docs/acceptance/iteration_{N}_plan.md`
- [ ] 填写所有必须字段（迭代编号、日期、状态、SSOT、目标、门禁）
- [ ] 创建对应的 `iteration_{N}_regression.md`（使用 [iteration_regression.template.md](iteration_regression.template.md)）
- [ ] 创建证据目录 `docs/acceptance/evidence/`（如不存在）
- [ ] 在 [00_acceptance_matrix.md](../00_acceptance_matrix.md) 添加索引条目
- [ ] 移除模板说明（本文件顶部的使用说明区块）

---

_模板版本：v1.1 | 更新日期：2026-02-02（添加证据文件引用）_
