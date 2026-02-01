# Iteration Regression 模板

> **使用说明**：复制本模板到 `docs/acceptance/iteration_N_regression.md`，替换 `{PLACEHOLDER}` 占位符。
>
> **索引关系**：创建回归记录后，需在 [00_acceptance_matrix.md](../00_acceptance_matrix.md) 的「迭代回归记录索引」表中添加对应条目。

---

## 必须字段

以下字段在创建回归记录时**必须填写**：

| 字段 | 说明 | 示例 |
|------|------|------|
| **执行日期** | YYYY-MM-DD 格式 | `2026-02-01` |
| **执行环境** | OS 和架构信息 | `darwin 24.6.0 (arm64)` |
| **Python 版本** | Python 版本号 | `3.13.2` |
| **pytest 版本** | pytest 版本号 | `9.0.2` |
| **执行结果总览** | 各测试阶段状态表 | 见下方模板 |
| **详细执行记录** | 每个测试阶段的详细结果 | 见下方模板 |

---

## 推荐段落结构

```markdown
# Iteration {N} Regression - CI 流水线验证记录

## 执行信息

| 项目 | 值 |
|------|-----|
| **执行日期** | {YYYY-MM-DD} |
| **执行环境** | {OS} ({arch}) |
| **Python 版本** | {python_version} |
| **pytest 版本** | {pytest_version} |
| **执行者** | {执行者/Cursor Agent} |
| **CI 运行 ID** | {GitHub Actions Run ID 或 -} |

---

## 执行结果总览

| 序号 | 测试阶段 | 命令 | 状态 | 详情 |
|------|----------|------|------|------|
| 1 | make ci | `make ci` | {STATUS} | {简要说明} |
| 2 | Gateway 单元测试 | `pytest tests/gateway/ -q` | {STATUS} | {N} 失败, {M} 通过, {K} 跳过 |
| 3 | Acceptance 测试 | `pytest tests/acceptance/ -q` | {STATUS} | {N} 通过, {M} 跳过, {K} 失败 |

**状态图例**：
- ✅ PASS - 全部通过
- ⚠️ PARTIAL - 部分通过（存在失败但非阻断）
- ❌ FAIL - 存在阻断性失败

---

## 详细执行记录

### 1. make ci

**命令**: `make ci`

**状态**: {STATUS}

**执行流程**:

| 步骤 | 检查项 | 状态 | 说明 |
|------|--------|------|------|
| 1 | `ruff check src/ tests/` | {STATUS} | {说明} |
| 2 | `ruff format --check` | {STATUS} | {N} files already formatted |
| 3 | `typecheck-gate` (mypy baseline) | {STATUS} | {说明} |

#### 1.1 {子问题标题}（如有失败）

**错误统计**:
- 门禁级别: {baseline/strict}
- 当前错误数: {N}
- 基线错误数: {M}
- 已修复: {K} 个
- **新增错误: {L} 个**

**新增错误涉及的主要模块**:

| 模块路径 | 主要错误类型 |
|----------|--------------|
| `{path}` | {error_type} |

---

### 2. Gateway 单元测试

**命令**: `pytest tests/gateway/ -q`

**状态**: {STATUS}

**统计**:
- 通过: {N}
- 失败: {M}
- 跳过: {K}
- 执行时间: {T}s

**失败用例分类**（如有失败）:

#### 2.1 {test_file.py} ({N} 失败)

| 测试用例 | 失败原因 |
|----------|----------|
| `{test_name}` | {失败原因} |

**根本原因**: {分析}

---

### 3. Acceptance 测试

**命令**: `pytest tests/acceptance/ -q`

**状态**: {STATUS}

**统计**:
- 通过: {N}
- 失败: {M}
- 跳过: {K}
- 执行时间: {T}s

**跳过原因**: {说明}

---

## 失败修复建议

### P0 - CI 阻断（必须修复）

1. **{问题1}** ({N}个)
   - 选项 A: {修复方案A}
   - 选项 B: {修复方案B}
   
   修复命令:
   ```bash
   {修复命令}
   ```

### P1 - 测试修复

2. **{问题2}** ({N}个失败)
   - {修复建议}
   - 文件: `{file_path}`

---

## 失败修复追踪

| 失败项 | 类型 | 涉及文件 | 修复 PR/Commit | 状态 |
|--------|------|----------|----------------|------|
| {失败项描述} | {类型检查/测试失败} | `{file_path}` | {PR 或 Commit} | ⏳ 待修复 / ✅ 已修复 |

---

## 与上一迭代对比

| 指标 | Iteration {N-1} | Iteration {N} | 变化 |
|------|-----------------|---------------|------|
| ruff lint | {STATUS} | {STATUS} | {变化} |
| ruff format | {STATUS} | {STATUS} | {变化} |
| mypy 新增错误 | {N} | {M} | {+/-K} |
| Gateway 测试失败 | {N} | {M} | {+/-K} |
| Acceptance 测试 | {N} 通过 | {M} 通过 | {+/-K} |

**分析**:
- {分析要点1}
- {分析要点2}

---

## 下一步行动

### 立即行动（Iteration {N+1}）

1. **{行动1}** ({N} 失败)
   - {详细说明}

2. **{行动2}** ({N} 失败)
   - {详细说明}

### 验证命令

```bash
# 完整 CI 验证
make ci && pytest tests/gateway/ -q && pytest tests/acceptance/ -q
```

## 最小门禁命令块

> **说明**：此区块由脚本自动生成和更新。
>
> **手动生成**：
> ```bash
> python scripts/iteration/render_min_gate_block.py {N} --profile full
> ```
>
> **自动同步**（推荐）：
> ```bash
> # 同步所有自动生成区块（min_gate_block + evidence_snippet）
> python scripts/iteration/sync_iteration_regression.py {N} --write
>
> # 仅同步最小门禁命令块
> python scripts/iteration/sync_iteration_regression.py {N} --only-min-gate --write
> ```
>
> 可选 `--profile` 参数：`full`（默认）、`regression`、`docs-only`、`ci-only`、`gateway-only`、`sql-only`

<!-- BEGIN GENERATED: min_gate_block profile=full -->

（使用上述脚本生成内容后，此区块将被自动替换）

<!-- END GENERATED: min_gate_block -->

---

<!-- BEGIN GENERATED: evidence_snippet -->

## 验收证据

<!-- 此段落由脚本自动生成，请勿手动编辑 -->

| 项目 | 值 |
|------|-----|
| **证据文件** | [`iteration_{N}_evidence.json`](evidence/iteration_{N}_evidence.json) |
| **Schema 版本** | `iteration_evidence_v1.schema.json` |
| **记录时间** | {YYYY-MM-DDTHH:MM:SSZ} |
| **Commit SHA** | `{commit_sha}` |

### 门禁命令执行摘要

> 以下表格由脚本从证据文件自动渲染。

| 命令 | 结果 | 耗时 | 摘要 |
|------|------|------|------|
| `make ci` | ✅ PASS | - | - |

### 整体验收结果

- **结果**: ✅ PASS
- **说明**: 所有门禁通过

<!-- END GENERATED: evidence_snippet -->

> **重要**：证据文件应由脚本自动生成，禁止手工创建或修改 JSON 文件。

### 证据文件生成与同步流程

**步骤 1: 生成证据文件**

使用 `record_iteration_evidence.py` 脚本生成符合 schema 的证据文件：

```bash
# 1. 预览模式（推荐先执行，确认输出路径和内容）
python scripts/iteration/record_iteration_evidence.py {N} --dry-run

# 2. 基本用法（自动获取当前 commit SHA）
python scripts/iteration/record_iteration_evidence.py {N}

# 3. 添加门禁命令执行结果
python scripts/iteration/record_iteration_evidence.py {N} \
  --add-command "ci:make ci:PASS" \
  --add-command "gateway-test:pytest tests/gateway/ -q:PASS" \
  --add-command "acceptance-test:pytest tests/acceptance/ -q:PASS"

# 4. 关联 CI 运行 URL（适用于 GitHub Actions 触发的验收）
python scripts/iteration/record_iteration_evidence.py {N} \
  --ci-run-url https://github.com/org/repo/actions/runs/{run_id} \
  --add-command "ci:make ci:PASS"

# 5. 添加备注说明
python scripts/iteration/record_iteration_evidence.py {N} \
  --notes "所有门禁通过，验收完成" \
  --add-command "ci:make ci:PASS"
```

**步骤 2: 同步到回归文档**

使用 `sync_iteration_regression.py` 将证据和门禁命令块同步到回归文档：

```bash
# 预览模式（推荐先执行）
python scripts/iteration/sync_iteration_regression.py {N}

# 写入模式（同步 min_gate_block 和 evidence_snippet）
python scripts/iteration/sync_iteration_regression.py {N} --write

# 仅同步证据片段
python scripts/iteration/sync_iteration_regression.py {N} --only-evidence --write

# 使用 regression profile
python scripts/iteration/sync_iteration_regression.py {N} --profile regression --write
```

**脚本功能**：
- 自动收集环境信息（OS、Python 版本、架构）
- 自动获取当前 git commit SHA
- 内置敏感信息脱敏（PASSWORD/DSN/TOKEN 等）
- 输出格式符合 `iteration_evidence_v1.schema.json`

**文件命名规范**：
- Canonical path（固定文件名）：`iteration_{N}_evidence.json`
- 详见 `scripts/iteration/iteration_evidence_naming.py` 的命名策略说明

### Schema 校验（可选）

证据文件生成后可手动校验格式正确性：

```bash
python -m jsonschema -i docs/acceptance/evidence/iteration_{N}_evidence.json schemas/iteration_evidence_v1.schema.json
```

---

## 相关文档

- [Iteration {N-1} 回归记录](iteration_{N-1}_regression.md)
- [验收测试矩阵](00_acceptance_matrix.md)
- [Mypy 基线策略](../architecture/adr_mypy_baseline_and_gating.md)
- [CLI 入口文档](../architecture/cli_entrypoints.md)
```

---

## 与 00_acceptance_matrix.md 的索引关系

创建新的回归记录后，需在 [00_acceptance_matrix.md](../00_acceptance_matrix.md) 的「迭代回归记录索引」表中添加或更新条目：

```markdown
| 迭代 | 日期 | 状态 | 计划 | 详细记录 | 说明 |
|------|------|------|------|----------|------|
| **Iteration {N}** | {YYYY-MM-DD} | {STATUS} | [iteration_{N}_plan.md](iteration_{N}_plan.md) | [iteration_{N}_regression.md](iteration_{N}_regression.md) | {简要说明} |
```

**索引更新规则**：

1. **首次记录**：在表格中添加新行（最新迭代在最前）
2. **后续更新**：更新同一行的状态和说明字段
3. **无 Plan 文件**：如果迭代没有独立的计划文件，计划列填 `-`

---

## 状态定义

| 状态 | Emoji | 适用场景 |
|------|-------|----------|
| **PASS** | ✅ | 所有门禁通过，无阻断性失败 |
| **PARTIAL** | ⚠️ | 部分通过：lint/format 通过，但存在 mypy 新增错误或测试失败 |
| **FAIL** | ❌ | 存在阻断性问题：lint 失败、关键测试失败 |
| **SUPERSEDED** | ⚠️ | 已被后续迭代取代（使用 `⚠️ Superseded by Iteration {K}` 格式标注） |

---

## Superseded by …（可选区块）

当迭代被后续迭代取代时，**必须**在回归记录**前 20 行内**添加 superseded 声明。

### CI 检查逻辑

`scripts/ci/check_no_iteration_links_in_docs.py::check_regression_file_superseded_header`：
1. 扫描文件前 20 行
2. 使用正则 `Superseded\s+by\s+Iteration\s*(\d+)`（不区分大小写）匹配
3. 验证声明中的后继编号与索引表一致

### 推荐格式

推荐在标题之前添加，以便读者第一时间看到：

```markdown
> **⚠️ Superseded by Iteration {M}**
>
> 本迭代已被 [Iteration {M}](iteration_{M}_regression.md) 取代，不再维护。
> 请参阅后续迭代的回归记录获取最新验收状态。

---

# Iteration {N} 回归验证
（原有内容）
```

**格式约束**：

| 约束 | 要求 |
|------|------|
| **位置** | 文件前 20 行内（推荐在标题之前，以便读者第一时间看到） |
| **格式** | 使用 blockquote（`>`）包裹 |
| **关键短语** | 必须包含 `Superseded by Iteration {M}` 字样（{M} 为后继迭代编号） |
| **后继链接** | **必须**使用相对路径 `[Iteration {M}](iteration_{M}_regression.md)` 格式，指向实际存在的后继迭代回归记录 |
| **编号一致性** | {M} 必须与索引表「说明」字段声明的后继编号一致 |
| **分隔线** | 声明后建议添加 `---` 分隔线，与原有内容分隔 |

> **统一来源**：本格式规范与 [adr_iteration_docs_workflow.md](../../architecture/adr_iteration_docs_workflow.md) 的 R6 规则保持一致。

### 链接约束（R1-R5 规则）

| 约束 | 要求 |
|------|------|
| **R1: 后继链接必须存在** | `Iteration {M}` 必须是有效的相对链接，指向实际存在的回归记录文件 |
| **R2: 后继必须在索引中** | 被引用的后继迭代必须已在 `00_acceptance_matrix.md` 的索引表中出现 |
| **R3: 后继排序在上方** | 后继迭代在索引表中的位置必须在本迭代**上方**（最新迭代在最前） |
| **R4: 禁止环形引用** | 不允许 A→B→A 的循环 superseded 链 |
| **R5: 禁止多后继** | 每个迭代只能有**一个**直接后继（单一 superseded by 链接） |

### 何时使用

- ✅ 新迭代完全替代旧迭代的验收范围（如 Iteration 9 被 Iteration 10 取代）
- ✅ 旧迭代存在的问题已在新迭代中修复并重新验收
- ✅ 迭代编号重新规划，旧编号不再使用

### 何时禁止

- ❌ 新迭代仅是旧迭代的**补充**而非**取代**（此时两者应并存）
- ❌ 旧迭代仍在活跃维护中
- ❌ 后继迭代尚未创建或未通过初始验收

### 最小示例

**场景**：Iteration 7 被 Iteration 9 取代

在 `iteration_7_regression.md` 前 20 行内（推荐在标题之前）添加：

```markdown
> **⚠️ Superseded by Iteration 9**
>
> 本迭代已被 [Iteration 9](iteration_9_regression.md) 取代，不再维护。
> 请参阅后续迭代的回归记录获取最新验收状态。

---

# Iteration 7 Regression - CI 流水线验证记录
（原有内容保持不变）
```

同时在 `00_acceptance_matrix.md` 索引表中更新（注意：后继编号必须与 regression 文件中的一致）：

```markdown
| Iteration 9 | 2026-02-01 | ⚠️ PARTIAL | - | [iteration_9_regression.md](iteration_9_regression.md) | 当前活跃迭代 |
| Iteration 7 | 2026-02-01 | 🔄 SUPERSEDED | - | [iteration_7_regression.md](iteration_7_regression.md) | 已被 Iteration 9 取代 |
```

---

## 必须记录的测试阶段

以下测试阶段在回归记录中**必须包含**：

| 序号 | 测试阶段 | 命令 | 重要性 |
|------|----------|------|--------|
| 1 | **make ci** | `make ci` | **必须** - CI 门禁核心 |
| 2 | **Gateway 单元测试** | `pytest tests/gateway/ -q` | **必须** - 核心功能验证 |
| 3 | **Acceptance 测试** | `pytest tests/acceptance/ -q` | **必须** - 集成验收 |

可选记录的测试阶段：

| 序号 | 测试阶段 | 命令 | 适用场景 |
|------|----------|------|----------|
| 4 | DI 边界门禁 | `pytest tests/gateway/test_di_boundaries.py -v` | 涉及 DI 变更时 |
| 5 | SQL 安全检查 | `pytest tests/logbook/test_sql_migrations_safety.py -v` | 涉及 SQL 变更时 |
| 6 | 一致性检查 | `python scripts/verify_*_consistency.py` | 涉及配置变更时 |

---

## 快速创建检查清单

- [ ] 复制本模板到 `docs/acceptance/iteration_{N}_regression.md`
- [ ] 填写所有必须字段（执行信息、结果总览、详细记录）
- [ ] 执行 `make ci` 并记录结果
- [ ] 执行 `pytest tests/gateway/ -q` 并记录结果
- [ ] 执行 `pytest tests/acceptance/ -q` 并记录结果
- [ ] 如有失败，填写失败修复建议和追踪表
- [ ] 与上一迭代对比，填写变化分析
- [ ] 使用 `python scripts/iteration/record_iteration_evidence.py {N}` 生成证据文件
- [ ] 填写"验收证据"段落，链接到证据文件
- [ ] 在 [00_acceptance_matrix.md](../00_acceptance_matrix.md) 更新索引条目
- [ ] 移除模板说明（本文件顶部的使用说明区块）

---

_模板版本：v1.4 | 更新日期：2026-02-02（统一证据文件路径与校验命令格式）_
