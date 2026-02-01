# 迭代回归文档 Superseded 声明审计报告

> **⚠️ 历史样例（非 SSOT）**
>
> 本文档为一次性审计报告的**历史样例**，仅供参考格式，不作为权威来源。
>
> **生成新审计报告请使用**：
> ```bash
> python scripts/iteration/audit_iteration_docs.py
> # 或输出到文件
> python scripts/iteration/audit_iteration_docs.py --output-dir .artifacts/iteration-audit
> ```
>
> **CI 门禁检查请使用**：`make check-iteration-docs`

---

> **原始草稿信息**:
>
> - **生成日期**: 2026-02-01
> - **任务 ID**: task-26a414d6

---

## 1. 审计范围

- **索引文件**: `docs/acceptance/00_acceptance_matrix.md`
- **扫描目录**: `docs/acceptance/`
- **扫描模式**: `iteration_*_{plan,regression}.md`

---

## 2. 文件扫描结果

### 2.1 发现的迭代文件

| 迭代 | Plan 文件 | Regression 文件 | 备注 |
|------|-----------|-----------------|------|
| Iteration 3 | ❌ 无 | ✅ `iteration_3_regression.md` | - |
| Iteration 4 | ✅ `iteration_4_plan.md` | ✅ `iteration_4_regression.md` | - |
| Iteration 5 | ❌ 无 | ✅ `iteration_5_regression.md` | - |
| Iteration 6 | ❌ 无 | ✅ `iteration_6_regression.md` | - |
| Iteration 7 | ❌ 无 | ✅ `iteration_7_regression.md` | - |
| Iteration 8 | ❌ 无 | ❌ 无 | **跳号**（索引中也无记录） |
| Iteration 9 | ❌ 无 | ✅ `iteration_9_regression.md` | - |
| Iteration 10 | ❌ 无 | ✅ `iteration_10_regression.md` | - |
| Iteration 11 | ✅ `iteration_11_plan.md` | ✅ `iteration_11_regression.md` | 当前活跃迭代 |

**共计**: 8 个 regression 文件，2 个 plan 文件

---

## 3. 索引与文件一致性对照

### 3.1 迭代回归记录索引（来自 `00_acceptance_matrix.md`）

| 迭代 | 日期 | 索引状态 | 索引说明 |
|------|------|----------|----------|
| Iteration 11 | 2026-02-01 | 🔄 PLANNING | 当前活跃迭代 |
| Iteration 10 | 2026-02-01 | ⚠️ PARTIAL | lint ✅，mypy ❌ (86 新增)，gateway 15 失败 |
| Iteration 9 | 2026-02-01 | 🔄 SUPERSEDED | 已被 Iteration 10 取代 |
| Iteration 7 | 2026-02-01 | 🔄 SUPERSEDED | 已被 Iteration 9 取代 |
| Iteration 6 | 2026-02-01 | ⚠️ PARTIAL | lint ✅，mypy ❌，gateway 51 失败 |
| Iteration 5 | 2026-01-29 | ✅ PASS | - |
| Iteration 4 | 2026-02-01 | ⚠️ PARTIAL | re-verified |
| Iteration 3 | 2026-01-27 | ✅ PASS | - |

### 3.2 Superseded 声明检查结果

| 迭代 | 索引状态 | 文件 Superseded 声明 | 一致性 | 备注 |
|------|----------|----------------------|--------|------|
| Iteration 3 | ✅ PASS | ❌ 无声明 | ✅ 一致 | 非 SUPERSEDED 状态，无需声明 |
| Iteration 4 | ⚠️ PARTIAL | ❌ 无声明 | ✅ 一致 | 非 SUPERSEDED 状态，无需声明 |
| Iteration 5 | ✅ PASS | ❌ 无声明 | ✅ 一致 | 非 SUPERSEDED 状态，无需声明 |
| Iteration 6 | ⚠️ PARTIAL | ❌ 无声明 | ✅ 一致 | 非 SUPERSEDED 状态，无需声明 |
| **Iteration 7** | 🔄 SUPERSEDED | ✅ 有声明 | ✅ 一致 | 声明："Superseded by Iteration 9" |
| **Iteration 9** | 🔄 SUPERSEDED | ❌ **无声明** | ❌ **不一致** | 索引标记为 SUPERSEDED，但文件缺少声明 |
| Iteration 10 | ⚠️ PARTIAL | ❌ 无声明 | ✅ 一致 | 非 SUPERSEDED 状态，无需声明 |
| Iteration 11 | 🔄 PLANNING | ❌ 无声明 | ✅ 一致 | PLANNING 状态，无需 superseded 声明 |

---

## 4. 发现的问题

### 4.1 🔴 不一致项

| # | 问题描述 | 文件 | 建议修复 |
|---|----------|------|----------|
| 1 | **Superseded 声明缺失** | `iteration_9_regression.md` | 在文件顶部添加 superseded 声明 |

**详情**:

`iteration_9_regression.md` 在索引中标记为 `🔄 SUPERSEDED`（说明："已被 Iteration 10 取代"），但文件顶部**缺少对应的 superseded 声明**。

**对比参考** - `iteration_7_regression.md` 的正确格式：

```markdown
# Iteration 7 Regression - CI 流水线验证记录

> **⚠️ Superseded by Iteration 9**
>
> 本文档已被 [Iteration 9 回归记录](iteration_9_regression.md) 取代。
> Iteration 7 的验证工作已合并到 Iteration 9 中完成。
> 以下所有 PENDING 项已在 Iteration 9 中得到验证或废弃。
```

### 4.2 ⚪ 跳号说明

- **Iteration 8**: 索引和文件系统中均不存在，属于正常跳号，无需处理。

---

## 5. 修复建议

### 5.1 为 `iteration_9_regression.md` 添加 Superseded 声明

在文件顶部（标题下方）添加以下内容：

```markdown
> **⚠️ Superseded by Iteration 10**
>
> 本文档已被 [Iteration 10 回归记录](iteration_10_regression.md) 取代。
> Iteration 9 的验证工作已合并到 Iteration 10 中完成。
```

---

## 6. 审计总结

| 指标 | 结果 |
|------|------|
| 总迭代数（索引中） | 8 |
| Regression 文件数 | 8 |
| Plan 文件数 | 2 |
| SUPERSEDED 状态迭代 | 2 (Iteration 7, 9) |
| Superseded 声明完整 | 1/2 (50%) |
| **一致性问题数** | **1** |

---

## 附录：Superseded 声明规范

根据现有实践，SUPERSEDED 状态的迭代回归文件应在顶部包含：

1. **标识符**: `> **⚠️ Superseded by Iteration N**`
2. **说明**: 指向替代文档的链接
3. **状态**: 说明原有 PENDING 项的处置方式

---

*报告生成完成*
