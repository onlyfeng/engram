# 验收测试矩阵

本文档记录 Engram 各迭代的验收测试执行情况，包括测试范围、执行结果与已知限制。

---

## 迭代回归记录索引

| 迭代 | 日期 | 状态 | 计划 | 详细记录 | 说明 |
|------|------|------|------|----------|------|
| **Iteration 22** | 2026-02-03 | ✅ PASS | - | [iteration_22_regression.md](iteration_22_regression.md) | make ci ✅ |
| **Iteration 21** | 2026-02-03 | ✅ PASS | - | [iteration_21_regression.md](iteration_21_regression.md) | make ci ✅ |
| **Iteration 15** | 2026-02-03 | ✅ PASS | [iteration_15_plan.md](iteration_15_plan.md) | [iteration_15_regression.md](iteration_15_regression.md) | make ci ✅；pytest tests/gateway ✅（1105 passed, 206 skipped）；pytest tests/acceptance ✅（132 passed, 48 skipped） |
| **Iteration 14** | 2026-02-02 | ✅ PASS | [iteration_14_plan.md](iteration_14_plan.md) | [iteration_14_regression.md](iteration_14_regression.md) | make ci ✅，check-iteration-docs ✅，CI 测试 1351 passed，iteration 测试 440 passed |
| **Iteration 13** | 2026-02-02 | ✅ PASS | [iteration_13_plan.md](iteration_13_plan.md) | [iteration_13_regression.md](iteration_13_regression.md) | 所有最小门禁通过：Workflow 合约 (v2.13.0)、Gateway Public API、CI 测试 (608 passed)、Gateway 测试 (1042 passed) |
| Iteration 12 | 2026-02-02 | ✅ PASS | [iteration_12_plan.md](iteration_12_plan.md) | [iteration_12_regression.md](iteration_12_regression.md) | Gateway 测试全绿（1005 通过/206 跳过），修复 ImportError/patch 路径/状态隔离等问题 |
| Iteration 11 | 2026-02-01 | 🔄 SUPERSEDED | [iteration_11_plan.md](iteration_11_plan.md) | [iteration_11_regression.md](iteration_11_regression.md) | 已被 Iteration 12 取代 |
| Iteration 10 | 2026-02-01 | 🔄 SUPERSEDED | - | [iteration_10_regression.md](iteration_10_regression.md) | 已被 Iteration 11 取代；lint ✅，mypy ❌ (86 新增)，gateway 15 失败，acceptance ✅ |
| Iteration 9 | 2026-02-01 | 🔄 SUPERSEDED | - | [iteration_9_regression.md](iteration_9_regression.md) | 已被 Iteration 10 取代；lint ✅，mypy ❌，4 测试失败 |
| Iteration 8 | 2026-02-01 | 🔄 SUPERSEDED | [iteration_8_plan.md](iteration_8_plan.md) | [iteration_8_regression.md](iteration_8_regression.md) | 已被 Iteration 9 取代 |
| Iteration 7 | 2026-02-01 | 🔄 SUPERSEDED | - | [iteration_7_regression.md](iteration_7_regression.md) | 已被 Iteration 9 取代 |
| Iteration 6 | 2026-01-30 | ⚠️ PARTIAL | - | [iteration_6_regression.md](iteration_6_regression.md) | 124 个 ruff 错误（已在 Iteration 9 修复） |
| Iteration 5 | 2026-01-29 | ✅ PASS | - | [iteration_5_regression.md](iteration_5_regression.md) | - |
| Iteration 4 | 2026-01-28 | ✅ PASS | [iteration_4_plan.md](iteration_4_plan.md) | [iteration_4_regression.md](iteration_4_regression.md) | - |
| Iteration 3 | 2026-01-27 | ✅ PASS | - | [iteration_3_regression.md](iteration_3_regression.md) | - |
| Iteration 2 | 2026-01-26 | ✅ PASS | [iteration_2_plan.md](iteration_2_plan.md) | [iteration_2_regression.md](iteration_2_regression.md) | - |

---

## SUPERSEDED 一致性规则与索引完整性规则

本节定义了迭代回归记录索引的一致性规则，由 `scripts/ci/check_no_iteration_links_in_docs.py` 自动校验。

### SUPERSEDED 一致性规则 (R1-R6)

适用于索引表中状态为 `🔄 SUPERSEDED` 的迭代条目。

| 规则 ID | 规则名称 | 说明 |
|---------|----------|------|
| **R1** | 后继链接必须存在 | 说明字段必须包含后继声明 |
| **R2** | 后继必须在索引表中 | 被引用的后继迭代必须已在索引表中存在 |
| **R3** | 后继排序在上方 | 后继迭代在表格中的位置必须在被取代迭代上方 |
| **R4** | 禁止环形引用 | 不允许 A→B→A 的循环取代链 |
| **R5** | 禁止多后继 | 每个迭代只能有一个直接后继 |
| **R6** | regression 声明必须存在 | regression 文件顶部必须有标准 superseded 声明 |

#### R1: 后继链接必须存在

**要求**: SUPERSEDED 迭代的「说明」字段必须包含后继声明。

**格式要求**:
- `已被 Iteration X 取代`（中文）
- `Superseded by Iteration X`（英文）

**✅ 正确示例**:

```markdown
| Iteration 10 | 2026-02-01 | 🔄 SUPERSEDED | - | [...] | 已被 Iteration 11 取代 |
```

**❌ 失败示例**:

```markdown
| Iteration 10 | 2026-02-01 | 🔄 SUPERSEDED | - | [...] | 文档整理 |
```

**修复建议**: 在说明字段添加 `已被 Iteration X 取代`。

#### R2: 后继必须在索引表中

**要求**: 后继声明中引用的迭代必须已存在于索引表中。

**✅ 正确示例**（Iteration 12 已在索引表中）:

```markdown
| **Iteration 12** | 2026-02-02 | ⚠️ PARTIAL | [...] | [...] | 当前活跃迭代 |
| Iteration 11 | 2026-02-01 | 🔄 SUPERSEDED | [...] | [...] | 已被 Iteration 12 取代 |
```

**❌ 失败示例**（Iteration 13 不在索引表中）:

```markdown
| Iteration 11 | 2026-02-01 | 🔄 SUPERSEDED | [...] | [...] | 已被 Iteration 13 取代 |
```

**修复建议**: 先在索引表中添加后继迭代条目，再标记当前迭代为 SUPERSEDED。

#### R3: 后继排序在上方

**要求**: 后继迭代在索引表中的位置必须在被取代迭代的上方（行号更小）。

**✅ 正确示例**（Iteration 12 在 Iteration 11 上方）:

```markdown
| **Iteration 12** | 2026-02-02 | ⚠️ PARTIAL | [...] | [...] | 当前活跃迭代 |
| Iteration 11 | 2026-02-01 | 🔄 SUPERSEDED | [...] | [...] | 已被 Iteration 12 取代 |
```

**❌ 失败示例**（Iteration 12 在 Iteration 11 下方）:

```markdown
| Iteration 11 | 2026-02-01 | 🔄 SUPERSEDED | [...] | [...] | 已被 Iteration 12 取代 |
| **Iteration 12** | 2026-02-02 | ⚠️ PARTIAL | [...] | [...] | 当前活跃迭代 |
```

**修复建议**: 调整索引表行顺序，将后继迭代移到被取代迭代的上方。

#### R4: 禁止环形引用

**要求**: 不允许形成循环取代链（如 A→B→A 或 A→B→C→A）。

**❌ 失败示例**:

```markdown
| Iteration 11 | ... | 🔄 SUPERSEDED | ... | ... | 已被 Iteration 12 取代 |
| Iteration 12 | ... | 🔄 SUPERSEDED | ... | ... | 已被 Iteration 11 取代 |
```

**修复建议**: 检查取代链，确保形成单向 DAG（有向无环图）。

#### R5: 禁止多后继

**要求**: 每个迭代只能声明一个直接后继。

**✅ 正确示例**:

```markdown
| Iteration 10 | ... | 🔄 SUPERSEDED | ... | ... | 已被 Iteration 11 取代 |
```

**❌ 失败示例**:

```markdown
| Iteration 10 | ... | 🔄 SUPERSEDED | ... | ... | 已被 Iteration 11、Iteration 12 取代 |
```

**修复建议**: 保留最终后继，移除多余的后继声明。

#### R6: regression 声明必须存在

**要求**: SUPERSEDED 迭代的 regression 文件**前 20 行内**必须包含关键短语 `Superseded by Iteration M`，且后继编号 M 必须与索引表一致。

**CI 检查逻辑**（`scripts/ci/check_no_iteration_links_in_docs.py::check_regression_file_superseded_header`）:
1. 扫描文件前 20 行
2. 使用正则 `Superseded\s+by\s+Iteration\s*(\d+)`（不区分大小写）匹配
3. 验证声明中的后继编号与索引表一致

**✅ 正确示例**（文件 `iteration_10_regression.md` 顶部）:

```markdown
> **⚠️ Superseded by Iteration 11**
>
> 本迭代已被 [Iteration 11](iteration_11_regression.md) 取代，不再维护。
> 请参阅后续迭代的回归记录获取最新验收状态。

---

# Iteration 10 回归验证
（原有内容）
```

**格式约束**:

| 约束 | 要求 |
|------|------|
| **位置** | 文件前 20 行内（推荐在标题之前，以便读者第一时间看到） |
| **格式** | 使用 blockquote（`>`）包裹 |
| **关键短语** | 必须包含 `Superseded by Iteration M` 字样（M 为后继迭代编号） |
| **后继链接** | 必须使用相对路径 `[Iteration M](iteration_M_regression.md)` 格式 |
| **编号一致性** | M 必须与索引表「说明」字段声明的后继编号一致 |

**❌ 失败示例 1**（缺少 superseded 声明）:

```markdown
# Iteration 10 回归验证

本文档记录 Iteration 10 的回归验证结果。
```

**❌ 失败示例 2**（superseded 编号与索引表不一致）:

索引表声明「已被 Iteration 11 取代」，但 regression 文件写的是：

```markdown
> **⚠️ Superseded by Iteration 12**
```

**修复建议**: 在 regression 文件前 20 行内添加标准声明，确保后继编号与索引表一致。

### 索引完整性规则 (R7-R9)

适用于整个索引表的完整性校验。

| 规则 ID | 规则名称 | 说明 |
|---------|----------|------|
| **R7** | 链接文件必须存在 | 索引表中 plan_link/regression_link 指向的文件必须存在 |
| **R8** | 文件必须被索引 | `docs/acceptance/iteration_*_regression.md` 必须在索引表中 |
| **R9** | 索引降序排列 | 索引表中 iteration 编号必须降序（最新迭代在最前） |

#### R7: 链接文件必须存在

**要求**: 索引表中引用的 plan 或 regression 文件必须实际存在于 `docs/acceptance/` 目录。

**✅ 正确示例**（文件 `iteration_12_plan.md` 存在）:

```markdown
| **Iteration 12** | 2026-02-02 | ⚠️ PARTIAL | [iteration_12_plan.md](iteration_12_plan.md) | [...] | 当前活跃迭代 |
```

**❌ 失败示例**（文件不存在）:

```markdown
| **Iteration 12** | 2026-02-02 | ⚠️ PARTIAL | [iteration_12_plan.md](iteration_12_plan.md) | [...] | 当前活跃迭代 |
# 但 docs/acceptance/iteration_12_plan.md 文件不存在
```

**修复建议**:
- 创建缺失的文件，或
- 将链接改为 `-`（表示无计划文档）

#### R8: 文件必须被索引

**要求**: `docs/acceptance/` 目录下的 `iteration_*_regression.md` 和 `iteration_*_plan.md` 文件必须在索引表中有对应条目。

**✅ 正确示例**（文件已在索引中引用）:

文件 `docs/acceptance/iteration_10_regression.md` 存在，且索引表有：

```markdown
| Iteration 10 | 2026-02-01 | 🔄 SUPERSEDED | - | [iteration_10_regression.md](iteration_10_regression.md) | 已被 Iteration 11 取代 |
```

**❌ 失败示例**（孤儿文件）:

文件 `docs/acceptance/iteration_10_regression.md` 存在，但索引表中没有 Iteration 10 条目。

**修复建议**:
- 在索引表中添加对应的迭代条目，或
- 删除不再需要的孤儿文件

#### R9: 索引降序排列

**要求**: 索引表中的迭代编号必须按降序排列（最新迭代在最前）。

**✅ 正确示例**:

```markdown
| **Iteration 12** | 2026-02-02 | ⚠️ PARTIAL | [...] | [...] | 当前活跃迭代 |
| Iteration 11 | 2026-02-01 | 🔄 SUPERSEDED | [...] | [...] | 已被 Iteration 12 取代 |
| Iteration 10 | 2026-02-01 | 🔄 SUPERSEDED | [...] | [...] | 已被 Iteration 11 取代 |
```

**❌ 失败示例**（顺序错误）:

```markdown
| Iteration 10 | 2026-02-01 | 🔄 SUPERSEDED | [...] | [...] | 已被 Iteration 11 取代 |
| Iteration 11 | 2026-02-01 | 🔄 SUPERSEDED | [...] | [...] | 已被 Iteration 12 取代 |
| **Iteration 12** | 2026-02-02 | ⚠️ PARTIAL | [...] | [...] | 当前活跃迭代 |
```

**修复建议**: 将 Iteration 12 行移到表格最上方，Iteration 11 次之，以此类推。

### 校验命令

```bash
# 完整检查（.iteration/ 链接 + SUPERSEDED + 索引完整性）
python scripts/ci/check_no_iteration_links_in_docs.py

# 仅检查 SUPERSEDED 一致性
python scripts/ci/check_no_iteration_links_in_docs.py --superseded-only

# 仅检查索引完整性
python scripts/ci/check_no_iteration_links_in_docs.py --integrity-only

# 详细输出
python scripts/ci/check_no_iteration_links_in_docs.py --verbose

# 仅统计（不阻断 CI）
python scripts/ci/check_no_iteration_links_in_docs.py --stats-only

# 输出机器可读的 JSON 修复建议（快速定位 R3/R9 排序问题）
python scripts/ci/check_no_iteration_links_in_docs.py --suggest-fixes
```

### 快速定位排序问题（--suggest-fixes）

当遇到 R3（后继排序在下方）或 R9（索引降序排列）违规时，可使用 `--suggest-fixes` 输出机器可读的 JSON 修复建议：

```bash
# 输出 JSON 格式的修复建议
python scripts/ci/check_no_iteration_links_in_docs.py --suggest-fixes
```

**输出示例**（R3 违规）：

```json
{
  "violations_count": 1,
  "suggestions_count": 1,
  "suggestions": [
    {
      "rule_id": "R3",
      "iteration_number": 7,
      "action": "move_above",
      "description": "将 Iteration 9 行移动到 Iteration 7 行的上方",
      "target_iteration": 9,
      "file": "docs/acceptance/00_acceptance_matrix.md"
    }
  ]
}
```

**输出示例**（R9 违规）：

```json
{
  "violations_count": 1,
  "suggestions_count": 1,
  "suggestions": [
    {
      "rule_id": "R9",
      "iteration_number": 10,
      "action": "move_above",
      "description": "将 Iteration 10 行移动到 Iteration 5 行的上方（索引应按迭代编号降序排列）",
      "target_iteration": 5,
      "file": "docs/acceptance/00_acceptance_matrix.md"
    }
  ]
}
```

**字段说明**：

| 字段 | 说明 |
|------|------|
| `rule_id` | 违反的规则 ID（R1-R9） |
| `iteration_number` | 违规的迭代编号 |
| `action` | 建议的操作类型（如 `move_above`、`add_successor_declaration`） |
| `description` | 人类可读的修复说明 |
| `target_iteration` | 目标迭代编号（移动操作时指定） |
| `file` | 需要修改的文件路径 |

**使用场景**：

1. **CI 失败快速定位**：将 JSON 输出解析后直接定位需要修改的行
2. **自动化修复脚本**：基于 `action` 和 `target_iteration` 字段实现自动修复
3. **编辑器集成**：IDE 插件可解析输出并提供 Quick Fix 功能

**注意**：`--suggest-fixes` 不改变阻断逻辑，仅提供额外的机器可读输出。存在违规时仍返回退出码 1。

---

## 模板说明

每次验收记录应包含以下字段：

| 字段 | 说明 |
|------|------|
| **日期** | 验收执行日期（YYYY-MM-DD） |
| **Commit** | 被验收的 commit SHA |
| **环境** | 执行环境（OS、Docker 版本、数据库版本等） |
| **执行命令** | 实际运行的验收命令 |
| **结果** | PASS / PARTIAL / FAIL |
| **已知限制** | 当前迭代的已知限制与约束 |
| **未覆盖范围** | 本次未执行的测试及原因 |
| **风险评估** | 未覆盖范围带来的潜在风险 |

---

## 本地复现 CI 的最小命令集

本节说明如何在本地环境中复现 GitHub Actions CI 的全部检查。

### 快速运行（单命令）

```bash
# 运行所有 CI 检查（与 GitHub Actions 对齐）
make ci
```

### CI 检查项对照表

| CI Job | Makefile 目标 | 脚本/命令 | 说明 |
|--------|---------------|-----------|------|
| **lint** | `make lint` | `ruff check src/ tests/` | 代码 lint 检查 |
| **lint** | `make format-check` | `ruff format --check src/ tests/` | 代码格式检查 |
| **lint** | `make typecheck-gate` | `scripts/ci/check_mypy_gate.py --gate baseline` | mypy 基线对比检查 |
| **mypy-strict-island** | `make typecheck-strict-island` | `scripts/ci/check_mypy_gate.py --gate strict-island` | 核心模块 mypy strict 检查 |
| **mypy-metrics** | `make typecheck-metrics` | `scripts/ci/mypy_metrics.py --output artifacts/mypy_metrics.json` | mypy 指标报告（条目数、目录分布、error-code 分布） |
| **schema-validate** | `make check-schemas` | `scripts/validate_schemas.py --validate-fixtures` | JSON Schema 校验 |
| **env-var-consistency** | `make check-env-consistency` | `scripts/ci/check_env_var_consistency.py` | 环境变量一致性 |
| **logbook-consistency** | `make check-logbook-consistency` | `scripts/verify_logbook_consistency.py` | Logbook 配置一致性 |
| **migration-sanity** | `make check-migration-sanity` | SQL 文件存在性检查 | SQL 迁移文件检查 |
| **scm-sync-consistency** | `make check-scm-sync-consistency` | `scripts/verify_scm_sync_consistency.py` | SCM Sync 一致性 |
| **sql-safety** | `make check-sql-safety` | `pytest tests/logbook/test_sql_migrations_safety.py` | SQL 高危语句检测 |
| **gateway-di-boundaries** | `make check-gateway-di-boundaries` | `scripts/ci/check_gateway_di_boundaries.py` | Gateway DI 边界检查 |
| **no-root-wrappers-usage** | `make check-no-root-wrappers` | `scripts/ci/check_no_root_wrappers_usage.py` | 根目录 wrapper 导入禁令 |
| **cli-entrypoints-consistency** | `make check-cli-entrypoints` | `scripts/verify_cli_entrypoints_consistency.py` | CLI 入口点一致性 |
| **test** | `make test-gateway` | `pytest tests/gateway/ -v` | Gateway 测试（需数据库） |
| **test** | `make test-acceptance` | `pytest tests/acceptance/ -v` | 验收测试（需数据库） |

### 分步运行（调试用）

```bash
# 1. 代码质量检查（无需数据库）
make lint              # ruff lint（全量检查）
make lint-f821         # ruff lint（专项：F821 未定义名称）
make format-check      # ruff format 检查
make typecheck-gate    # mypy 基线对比
make typecheck-strict-island  # mypy strict-island 核心模块

# 2. 一致性检查（无需数据库）
make check-schemas            # JSON Schema 校验
make check-env-consistency    # 环境变量一致性
make check-logbook-consistency # Logbook 配置一致性
make check-migration-sanity   # SQL 文件检查
make check-scm-sync-consistency # SCM Sync 一致性

# 3. 边界与安全检查（无需数据库）
make check-sql-safety           # SQL 高危语句检测
make check-gateway-di-boundaries # Gateway DI 边界
make check-no-root-wrappers     # 根目录 wrapper 禁令
make check-cli-entrypoints      # CLI 入口点一致性

# 4. 测试（需要数据库）
# 先启动数据库服务
export POSTGRES_DSN="postgresql://postgres:postgres@localhost:5432/engram_test"
export TEST_PG_DSN="$POSTGRES_DSN"
export TEST_PG_ADMIN_DSN="postgresql://postgres:postgres@localhost:5432/postgres"

make test-gateway      # Gateway 测试
make test-acceptance   # 验收测试
```

### CI 与 Makefile 环境变量对照

| CI 环境变量 | Makefile/本地值 | 说明 |
|-------------|-----------------|------|
| `POSTGRES_DSN` | `postgresql://postgres:postgres@localhost:5432/engram_test` | 数据库连接 |
| `TEST_PG_DSN` | 同 `POSTGRES_DSN` | 测试用数据库连接 |
| `TEST_PG_ADMIN_DSN` | `postgresql://postgres:postgres@localhost:5432/postgres` | 管理员连接（DDL 操作） |
| `ENGRAM_TESTING` | `1` | 测试模式标志 |
| `ENGRAM_VERIFY_GATE` | `strict` | 验证门禁级别 |
| `ENGRAM_MYPY_GATE` | `baseline` | mypy 门禁模式（兼容 `MYPY_GATE`） |
| `PROJECT_KEY` | `test` | 项目标识 |

### 专项 Lint 检查使用说明

除全量 `make lint` 外，还提供专项 lint 检查目标，用于快速定位特定类型问题：

| 目标 | 检查内容 | 使用场景 |
|------|----------|----------|
| `make lint` | 全量 ruff 检查 | CI 门禁、PR 合入前、完整代码审查 |
| `make lint-f821` | F821 undefined-name（未定义名称） | 快速定位变量/函数未定义错误、重构后检查遗漏 |

**何时使用专项检查**：
- **快速迭代调试**：修改代码后只关心特定类型错误时，专项检查更快
- **重构后验证**：删除/重命名函数后，用 `lint-f821` 快速检测遗漏的引用
- **CI 失败排查**：CI 报告 F821 错误时，本地用专项检查快速复现

**何时使用全量检查**：
- **PR 提交前**：确保所有 lint 规则通过
- **CI 门禁**：`make ci` 包含全量 lint
- **代码审查**：全面评估代码质量

### 注意事项

1. **无数据库检查**：`make ci` 中的大部分检查无需数据库，仅 `check-sql-safety` 需要 pytest 但不需要真实数据库连接
2. **完整测试**：运行 `make test` 需要 PostgreSQL + pgvector 服务
3. **CI 精确复现**：若需完全复现 CI 环境，建议使用 Docker:
   ```bash
   docker compose -f docker-compose.unified.yml up -d postgres
   make ci && make test
   ```

---

## mypy 类型化健康指标

> **详细策略**：参见 [ADR: mypy 基线管理与 Gate 门禁策略](../architecture/adr_mypy_baseline_and_gating.md)
>
> **CI Artifact**：`artifacts/mypy_metrics.json`（由 `scripts/ci/mypy_metrics.py` 生成）

### mypy_metrics.json 结构说明

CI 每次运行会生成 `mypy_metrics.json`，包含以下指标：

| 字段 | 说明 |
|------|------|
| `summary.total_errors` | Baseline 中的错误总数（不含 note） |
| `summary.total_notes` | Baseline 中的 note 总数（上下文信息） |
| `by_directory` | 按目录前缀（`src/engram/gateway/`、`src/engram/logbook/`）聚合的错误分布 |
| `by_error_code` | 按 mypy error-code（如 `[arg-type]`、`[return-value]`）聚合的错误分布 |
| `by_error_type` | 按错误类型描述（如 "Incompatible types in"）聚合的 top 20 |
| `strict_island.paths` | pyproject.toml 中配置的 Strict Island 路径列表 |
| `strict_island.count` | Strict Island 路径数量 |

### 核心指标定义

| 指标 | 定义 | 查询命令 | 目标值 |
|------|------|----------|--------|
| **Baseline 条目数** | 基线文件中的错误总数 | `wc -l scripts/ci/mypy_baseline.txt` | 0 |
| **Strict Island 覆盖率** | 已启用 strict 的模块占比 | 见 `pyproject.toml` 的 `[[tool.mypy.overrides]]` | 100% |
| **近 30 天新增错误** | 最近 30 天基线净增加数 | `git log -p --since="30 days ago" -- scripts/ci/mypy_baseline.txt` | 0 |

### 当前指标状态

| 指标 | 当前值 | 状态 | 更新日期 |
|------|--------|------|----------|
| Baseline 条目数 | ~143 | 🔴 红色 | 2026-02-01 |
| Strict Island 覆盖率 | ~25% (17 modules) | 🟡 黄色 | 2026-02-01 |
| 近 30 天新增错误 | 0 | 🟢 绿色 | 2026-02-01 |

### 切换阶段追踪

| 阶段 | 描述 | 触发条件 | 状态 |
|------|------|----------|------|
| **阶段 0** | Gate=baseline（所有分支） | - | **当前** |
| **阶段 1** | master=strict, PR=baseline | 基线 ≤ 20 | 待触发 |
| **阶段 2** | 所有分支=strict | 基线 = 0, 阶段 1 稳定 2 周 | 待触发 |
| **阶段 3** | 归档基线文件，全面 strict | 阶段 2 稳定 2 周 | 待触发 |

### 迭代收敛计划

| 迭代 | 收敛范围 | 目标错误数 | 状态 |
|------|----------|------------|------|
| v1.0 | `src/engram/gateway/` | < 100 | 📋 进行中 |
| v1.1 | `[no-any-return]`, `[no-untyped-def]` | < 50 | 待开始 |
| v1.2 | `src/engram/logbook/` 核心模块 (cursor, outbox, governance) | < 30 | 📋 进行中 |
| v1.3 | `[import-untyped]` | < 20 | 待开始 |
| v1.4 | 高风险模块: `di.py`, `container.py`, `migrate.py` | < 10 | 待开始 |
| v2.0 | 全量 strict | 0 | 待开始 |

---

## 验收记录

### 迭代 YYYY-MM-DD（模板示例）

| 字段 | 内容 |
|------|------|
| **日期** | YYYY-MM-DD |
| **Commit** | `abc1234...` |
| **环境** | macOS 14.x / Docker 24.x / PostgreSQL 18.x |
| **执行命令** | 见下方 |
| **结果** | PASS / PARTIAL / FAIL |

**执行命令**:

```bash
# 1. 部署
make deploy

# 2. 统一栈验证
make verify-unified

# 3. Logbook 冒烟测试
make logbook-smoke

# 4. 单元测试
make test-logbook-unit

# 5. 集成测试
make test-gateway-integration
```

**已知限制**:

- [示例] SCM 同步仅支持 GitLab，GitHub 支持待开发

**未覆盖范围**:

| 测试类型 | 未覆盖项 | 原因 | 风险等级 |
|----------|----------|------|----------|
| [示例] 性能测试 | 大规模数据压测 | 环境限制 | 中 |
| [示例] 安全测试 | 渗透测试 | 需专业团队 | 高 |

**风险评估**:

- **高风险**: [示例] 未执行渗透测试，生产部署前需安排安全审计
- **中风险**: [示例] 未进行大规模压测，高并发场景可能存在性能瓶颈

---

### 迭代 2026-01-30（当前）

| 字段 | 内容 |
|------|------|
| **日期** | 2026-01-30 |
| **Commit** | `4d5d607` |
| **环境** | macOS 15.7.3 / Darwin 24.6.0 (arm64) / Docker N/A / PostgreSQL N/A |
| **执行命令** | 见下方 |
| **结果** | **PASS** |
| **验收记录** | `.artifacts/acceptance-runs/20260130T000804Z_acceptance-logbook-only.json`（本地执行记录，commit `4d5d607`） |

**执行命令**:

```bash
# Logbook-only 分步验收（标准步骤）
make up-logbook                    # 1. 启动 Logbook 服务
make migrate-logbook-stepwise      # 2. 数据库迁移
make verify-permissions-logbook    # 3. 权限验证（Logbook-only）
make logbook-smoke                 # 4. 冒烟测试
make test-logbook-unit             # 5. 单元测试

# 或一键验收
make acceptance-logbook-only
```

**本次实际执行**:

```bash
# 1. Logbook 部署配置一致性检查（无需 Docker）
python scripts/verify_logbook_consistency.py --verbose

# 2. Makefile acceptance-logbook-only 步骤序列验证
#    验证步骤: up-logbook → migrate-logbook-stepwise → verify-permissions-logbook → logbook-smoke → test-logbook-unit

# 3. compose/logbook.yml 最小 .env 兼容性检查
```

**验证结果摘要**:

| 检查项 | 结果 | 说明 |
|--------|------|------|
| A) initdb 默认环境 | PASS | compose/logbook.yml 在缺省 .env 下不会致命失败 |
| B) acceptance compose 依赖 | PASS | 子目标正确使用 LOGBOOK_COMPOSE（logbook-smoke 支持双检测） |
| C) Makefile 一致性 | PASS | up-logbook 描述与实现一致 |

**已知限制**:

- 当前环境无 Docker，无法执行完整容器级验收
- logbook-smoke 需要 PostgreSQL 服务运行
- 文档重构迭代，核心逻辑未变更

**本次修复**:

1. **Makefile `logbook-smoke`**: 修复了容器状态检查逻辑，现在同时支持 Logbook-only 部署（`$(LOGBOOK_COMPOSE)`）和统一栈部署（`$(DOCKER_COMPOSE)`）
2. **verify_logbook_consistency.py**: 更新检查逻辑，接受"双检测模式"作为有效配置

**未覆盖范围**:

| 测试类型 | 未覆盖项 | 原因 | 风险等级 |
|----------|----------|------|----------|
| 容器验收 | `make acceptance-logbook-only` | 当前环境无 Docker | 中 |
| 冒烟测试 | `make logbook-smoke` | 需要 PostgreSQL 服务 | 中 |
| 权限验证 | `make verify-permissions-logbook` | 需要 PostgreSQL 服务 | 低 |

**风险评估**:

- **中风险**: 未执行完整容器级验收，需在 Docker 环境中补充执行
- **低风险**: 核心配置一致性已通过静态检查验证

**自动记录脚本**:

如需手动生成验收记录 JSON，可使用：

```bash
python3 scripts/acceptance/record_acceptance_run.py \
    --name acceptance-logbook-only \
    --artifacts-dir .artifacts/acceptance-logbook-only \
    --result PARTIAL \
    --commit 4d5d607
```

记录将保存至 `.artifacts/acceptance-runs/<timestamp>_acceptance-logbook-only.json`

---

### 迭代 2026-01-30（Unified 最小验收规范确认）

| 字段 | 内容 |
|------|------|
| **日期** | 2026-01-30 |
| **Commit** | `4d5d607` (同上) |
| **环境** | macOS Darwin 24.6.0 (x86_64) / Docker N/A |
| **执行命令** | 见下方 |
| **结果** | **PASS** |
| **验收记录** | `.artifacts/acceptance-runs/20260130T000805Z_acceptance-unified-min.json`（本地执行记录，commit `4d5d607`） |

**执行命令**（acceptance-unified-min 步骤）:

```bash
# acceptance-unified-min 完整步骤序列
# 1. 部署统一栈（可通过 SKIP_DEPLOY=1 跳过）
make deploy

# 2. 统一栈验证（HTTP_ONLY_MODE=1，无需 Docker 容器操作权限）
HTTP_ONLY_MODE=1 make verify-unified

# 3. Logbook 单元测试
make test-logbook-unit

# 4. Gateway 集成测试（HTTP_ONLY_MODE=1）
HTTP_ONLY_MODE=1 make test-gateway-integration
```

**规范验证内容**:

本次迭代验证了 `acceptance-unified-min` 的规范定义与文档一致性：

| 检查项 | 结果 | 说明 |
|--------|------|------|
| Makefile 目标定义 | PASS | `acceptance-unified-min` 正确定义步骤序列 |
| HTTP_ONLY_MODE 传递 | PASS | verify-unified 和 test-gateway-integration 正确接收 HTTP_ONLY_MODE=1 |
| 产出目录 | PASS | `.artifacts/acceptance-unified-min/` 和 `.artifacts/verify-results.json` |
| 文档一致性 | PASS | README.md 和 integrate_existing_project.md 参数映射一致 |

**acceptance-unified-min 与 acceptance-unified-full 对比**:

| 特性 | acceptance-unified-min | acceptance-unified-full |
|------|------------------------|-------------------------|
| **适用场景** | CI PR 快速验证 | Nightly/发布前完整验收 |
| **Docker 容器操作** | 不需要（HTTP_ONLY_MODE=1） | 需要（降级测试操作容器） |
| **VERIFY_FULL** | *(不设置)* | **1**（完整验证模式） |
| **HTTP_ONLY_MODE** | **1**（显式设置） | **0**（显式设置，允许 Docker 操作） |
| **SKIP_DEGRADATION_TEST** | **1**（显式设置） | **0**（显式设置，执行降级测试） |
| **GATE_PROFILE** | http_only | **full** |
| **降级测试** | 跳过 | 执行 |
| **logbook-smoke** | 跳过 | 执行 |
| **Gateway 集成测试** | test-gateway-integration | **test-gateway-integration-full** |
| **MinIO** | 不需要 | 不需要 |
| **执行时间** | ~2-5 分钟 | ~5-10 分钟 |
| **产出文件** | steps.log, summary.json | steps.log, summary.json, verify-results.json |

> **环境变量传递方式**: Makefile 中这些变量在调用子目标时作为前缀显式传递（如 `HTTP_ONLY_MODE=1 $(MAKE) verify-unified`），
> 而非仅依赖 shell `export`，确保子 make 进程正确接收值。

**已知限制**:

- 当前环境无 Docker，无法执行完整容器级验收
- 文档与规范一致性已验证，实际执行待 Docker 环境补充

**未覆盖范围**:

| 测试类型 | 未覆盖项 | 原因 | 风险等级 |
|----------|----------|------|----------|
| 容器执行 | `make acceptance-unified-min` 实际运行 | 当前环境无 Docker | 中 |
| 端到端验证 | HTTP 健康检查、MCP 工具调用 | 需要服务运行 | 中 |

**风险评估**:

- **中风险**: 规范验证通过但未实际执行，需在 Docker 环境中补充执行
- **低风险**: Makefile 目标与文档定义一致，规范层面无问题

---

## Gateway → Logbook 覆盖点

### 测试文件

主要测试文件：`tests/gateway/test_unified_stack_integration.py`

### 覆盖点明细表

| 覆盖点 | 测试类 | 前置条件 | HTTP_ONLY_MODE 行为 |
|--------|--------|----------|---------------------|
| Gateway 健康检查 | `TestServiceHealthCheck` | `RUN_INTEGRATION_TESTS=1`、Gateway 运行 | 正常运行 |
| OpenMemory 健康检查 | `TestServiceHealthCheck` | `RUN_INTEGRATION_TESTS=1`、OpenMemory 运行 | 正常运行 |
| PostgreSQL 连接验证 | `TestServiceHealthCheck` | `POSTGRES_DSN` 环境变量 | 正常运行 |
| memory_store 写入 | `TestMemoryOperations` | `RUN_INTEGRATION_TESTS=1`、统一栈运行 | 正常运行 |
| memory_query 查询 | `TestMemoryOperations` | `RUN_INTEGRATION_TESTS=1`、统一栈运行 | 正常运行 |
| 带元数据的 memory_store | `TestMemoryOperations` | `RUN_INTEGRATION_TESTS=1`、统一栈运行 | 正常运行 |
| 存储-查询往返测试 | `TestEndToEndFlow` | `RUN_INTEGRATION_TESTS=1`、统一栈运行 | 正常运行 |
| **真实降级流程** | `TestDegradationFlow` | Docker 容器操作权限、`POSTGRES_DSN` | **跳过** |
| Mock 降级流程 | `TestMockDegradationFlow` | `POSTGRES_DSN` | 正常运行 |
| Mock 查询降级 | `TestMockQueryDegradation` | `POSTGRES_DSN` | 正常运行 |
| 可靠性报告端点 | `TestReliabilityReport` | `POSTGRES_DSN` | 正常运行 |
| OpenMemory DB 角色权限 | `TestOpenMemoryDbRoles` | `POSTGRES_DSN`、`OM_PG_SCHEMA` | 正常运行 |
| 启动验证错误信息 | `TestStartupVerificationErrors` | `POSTGRES_DSN` | 正常运行 |
| 数据库角色验证 | `TestDatabaseRolesVerification` | `POSTGRES_DSN` | 正常运行 |
| MCP memory_store E2E | `TestMCPMemoryStoreE2E` | `POSTGRES_DSN`、统一栈运行 | 正常运行 |
| MCP Mock 降级测试 | `TestMCPMemoryStoreWithMockDegradation` | `POSTGRES_DSN` | 正常运行 |
| **Outbox Worker 真实集成** | `TestOutboxWorkerRealIntegration` | Docker 权限、`POSTGRES_DSN` | **跳过** |
| JSON-RPC 2.0 协议 | `TestJsonRpcProtocol` | `RUN_INTEGRATION_TESTS=1`、Gateway 运行 | 正常运行 |
| 旧协议兼容性 | `TestLegacyProtocol` | `RUN_INTEGRATION_TESTS=1`、Gateway 运行 | 正常运行 |

### 运行模式

| Makefile 目标 | 环境变量 | 说明 |
|---------------|----------|------|
| `make test-gateway-integration` | `HTTP_ONLY_MODE=1` | 纯 HTTP 验证，CI 推荐，跳过需要 Docker 操作的测试 |
| `make test-gateway-integration-full` | 无 `HTTP_ONLY_MODE` | 完整集成测试，含降级测试，需要 Docker 权限 |

### 必需环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `RUN_INTEGRATION_TESTS` | 启用集成测试 | 必须设为 `1` |
| `GATEWAY_URL` | Gateway 服务 URL | `http://localhost:8787` |
| `OPENMEMORY_URL` | OpenMemory 服务 URL | `http://localhost:8080` |
| `POSTGRES_DSN` | PostgreSQL 连接字符串 | 无默认值，部分测试会跳过 |

### 可选环境变量

| 变量 | 说明 |
|------|------|
| `HTTP_ONLY_MODE=1` | 仅运行纯 HTTP 验证测试（跳过 Docker 操作） |
| `SKIP_DEGRADATION_TEST=1` | 跳过降级测试 |
| `OM_PG_SCHEMA` | OpenMemory 目标 schema（默认 `openmemory`） |

### MCP 全功能验收（客户端仅连 MCP）

| 功能域 | MCP 工具 | 覆盖测试 | 说明 |
|--------|---------|----------|------|
| 工具发现 | `tools/list` | `tests/acceptance/test_mcp_e2e_full_workflow.py` | 校验核心工具集存在 |
| 治理开关 | `governance_update` | `tests/acceptance/test_mcp_e2e_full_workflow.py` | 启用 team 写入并记录审计 |
| 记忆存取 | `memory_store`, `memory_query` | `tests/acceptance/test_mcp_e2e_full_workflow.py` | 覆盖 team/private 空间与查询 |
| 证据管理 | `evidence_upload`, `evidence_read` | `tests/acceptance/test_mcp_e2e_full_workflow.py` | 上传并读取 evidence 内容 |
| 制品管理 | `artifacts_put`, `artifacts_get`, `artifacts_exists` | `tests/acceptance/test_mcp_e2e_full_workflow.py` | 本地制品写入/读取/存在性 |
| 事实账本 | `logbook_*` | `tests/acceptance/test_mcp_e2e_full_workflow.py` | item/event/kv/attachment 查询 |
| SCM Patch | `scm_patch_blob_resolve`, `scm_materialize_patch_blob` | `tests/acceptance/test_mcp_e2e_full_workflow.py` | 通过 DB 夹具 + stub 走通 |

**说明**:
- 测试仅通过 `/mcp` JSON-RPC 调用，不直连 DB/OpenMemory
- 依赖本地 PostgreSQL（通过 acceptance conftest 迁移）
- OpenMemory 调用使用测试桩（避免依赖外部服务）

---

**未覆盖范围**:

| 测试类型 | 未覆盖项 | 原因 | 风险等级 |
|----------|----------|------|----------|
| MinIO 集成测试 | `test_object_store_minio_integration.py` | 需要 MinIO 服务运行（`docker-compose.minio.yml`） | 中 |
| 对象存储审计闭环 | 生产环境 S3/MinIO 审计事件端到端验证 | 需要真实对象存储配置与审计 Webhook | 高 |
| SCM 同步测试 | GitLab/SVN 实际同步 | 需要外部 SCM 服务与凭据配置 | 中 |
| 性能测试 | 大规模数据压测 | 当前迭代不涉及性能变更 | 低 |

**风险评估**:

- **高风险**: 生产对象存储审计闭环未验证，Artifact 写入与审计事件一致性依赖人工确认
- **中风险**: MinIO 集成测试需本地 MinIO，CI 环境暂不包含
- **中风险**: SCM 同步功能依赖外部服务，本地测试覆盖有限
- **低风险**: 本迭代主要为命名重构与文档完善，核心逻辑变更小

**后续改进计划**:

1. CI 添加 MinIO sidecar 支持 `test_object_store_minio_integration.py`
2. 建立 SCM 同步 mock 测试覆盖 GitLab/SVN 核心路径
3. 设计对象存储审计端到端验证脚本（`scripts/ops/verify_bucket_governance.py`）

---

## 验收测试命令参考

### Logbook 独立验收（仅事实账本）

```bash
make acceptance-logbook-only
```

**适用场景**:
- 仅需 PostgreSQL 事实账本，不需要 OpenMemory 语义记忆
- CI/CD 中快速验证 Logbook 核心功能
- 开发环境 Logbook 组件独立调试

**执行步骤**: up-logbook → migrate-logbook-stepwise → verify-permissions-logbook → logbook-smoke → test-logbook-unit

**环境变量**:

| 变量 | 说明 |
|------|------|
| `SKIP_DEPLOY=1` | 跳过 up-logbook（复用已有 PostgreSQL） |
| `SKIP_MIGRATE=1` | 跳过迁移（Schema 已存在） |
| `ENGRAM_VERIFY_GATE=off` | 跳过权限验证（替代已移除的 `SKIP_VERIFY_PERMISSIONS`） |

> **注意**：`SKIP_VERIFY_PERMISSIONS=1` 已移除（无实际实现）。如需跳过权限验证，请使用 `ENGRAM_VERIFY_GATE=off` 或 `--verify-gate=off`。

**产出**: `.artifacts/acceptance-logbook-only/`（summary.json、steps.log、health.json、test-results-index.json、diagnostics/）

### 最小验收（CI PR 推荐）

```bash
make acceptance-unified-min
```

包含: 部署 → verify-unified → test-logbook-unit → test-gateway-integration

**环境语义（固定）**:
- `HTTP_ONLY_MODE=1`
- `SKIP_DEGRADATION_TEST=1`
- `GATE_PROFILE=http_only`

**产出**: `.artifacts/acceptance-unified-min/`（summary.json、steps.log、verify-results.json、test-results-index.json、diagnostics/）

### 完整验收（Nightly/发布前推荐）

```bash
make acceptance-unified-full
```

包含: 部署 → logbook-smoke → verify-unified（VERIFY_FULL=1）→ test-logbook-unit → test-gateway-integration-full

**FULL 语义（固定）**:

- `VERIFY_FULL=1`（完整验证模式）
- `HTTP_ONLY_MODE=0`（允许 Docker 操作）
- `SKIP_DEGRADATION_TEST=0`（执行降级测试）
- `GATE_PROFILE=full`

**产出**: `.artifacts/acceptance-unified-full/`（steps.log、summary.json、verify-results.json、test-results-index.json、diagnostics/）

### CI 期望覆盖表

> **单一来源**: `.github/workflows/ci.yml` 和 `.github/workflows/nightly.yml`

本表记录 CI/Nightly 工作流的期望覆盖范围，与 Makefile acceptance 目标的映射关系。

#### CI 覆盖模式说明

CI 工作流中的 `unified-standard` job 采用 **组合式覆盖** 策略：
- **不直接执行** `make acceptance-unified-min`
- 而是在 workflow 中 **分步执行** acceptance-unified-min 的核心步骤
- 这样设计是为了支持：细粒度的条件检查、独立的 artifact 收集、灵活的错误处理

**组合式覆盖 vs 真实执行**：

| 特性 | CI 组合式覆盖 | 本地 `make acceptance-unified-min` |
|------|--------------|-----------------------------------|
| 执行方式 | workflow 分步执行 | Makefile 单命令执行 |
| 步骤控制 | 可按条件跳过某些步骤 | 固定步骤序列 |
| artifact 收集 | 每步独立上传 | 统一收集到 `.artifacts/acceptance-unified-min/` |
| 失败处理 | 可 continue-on-error | 失败立即退出 |
| record_acceptance_run | 显式调用，传入 metadata | 自动调用 |

#### CI 工作流覆盖（ci.yml）

| Profile | 触发条件 | 覆盖语义 | 覆盖范围 | Capability 要求 |
|---------|----------|----------|----------|-----------------|
| **http_only** | PR 变更检测触发 | `acceptance-unified-min` 组合式覆盖 | 健康检查、memory_store、memory_query | Docker、PostgreSQL |

**CI Matrix 策略**：
- 当前启用 profile: `[http_only]`
- 可扩展: `[http_only, standard]`（standard 含 JSON-RPC 验证）

**CI 覆盖步骤**（组合式覆盖 acceptance-unified-min）：

| 步骤 | http_only Profile | 对应 acceptance-unified-min 步骤 | 说明 |
|------|-------------------|----------------------------------|------|
| deploy | ✅ | Step 1: deploy | 启动统一栈 |
| verify-unified | ✅ (HTTP_ONLY_MODE=1) | Step 2: verify-unified | 跳过 MCP JSON-RPC |
| test-logbook-unit | ✅ | Step 3: test-logbook-unit | Logbook 单元测试 |
| test-gateway-integration | ✅ (HTTP_ONLY_MODE=1) | Step 4: test-gateway-integration | 跳过降级测试 |
| record_acceptance_run.py | ✅ | 记录步骤 | 记录验收运行（含 CI metadata） |

#### Nightly 工作流覆盖（nightly.yml）

| 执行方式 | 触发条件 | 覆盖范围 | Capability 要求 |
|----------|----------|----------|-----------------|
| **直接执行** `make acceptance-unified-full` | schedule 或 workflow_dispatch | 完整验收（含降级测试） | Docker、PostgreSQL、POSTGRES_DSN |

> **架构说明（v1.11.0+）**: Nightly 工作流 **直接调用** `make acceptance-unified-full`（非组合式覆盖）。
> 核心验证链（verify-unified + gateway-integration）已收敛到 `acceptance-unified-full` 内部执行，
> nightly.yml 不再独立运行这些步骤。MinIO/Build 测试保持为额外测试。

**Nightly 完整覆盖步骤**（由 `make acceptance-unified-full` 内部执行）：

| 步骤 | acceptance-unified-full 内部步骤 | 说明 |
|------|----------------------------------|------|
| deploy | Step 1 | 启动统一栈（SKIP_DEPLOY=1 可跳过） |
| logbook-smoke | Step 2 | Logbook 冒烟测试 |
| verify-unified | Step 3 (VERIFY_FULL=1, HTTP_ONLY_MODE=0) | 完整验证（含降级测试） |
| test-logbook-unit | Step 4 | Logbook 单元测试 |
| test-gateway-integration-full | Step 5 | 完整集成测试（含真实降级测试） |
| record_acceptance_run.py | 自动调用 | 记录验收运行（失败时仍执行） |

**Nightly 额外测试**（独立于 acceptance-unified-full）：

| 测试 | 说明 |
|------|------|
| Logbook Integration (MinIO) | MinIO 对象存储集成测试 |
| Seek PGVector Integration | PGVector 向量检索测试 |
| Seek Migrate (dry-run) | 迁移脚本验证 |
| Seek Smoke Test | 索引/检索/一致性检查 |
| Seek Nightly Rebuild | 标准化索引重建 |
| Seek Dual-Read | primary/shadow 一致性 |
| Seek Migration Drill | 迁移演练集成测试 |
| Artifact Audit | 制品一致性审计 |
| Docker Build | 完整 Docker 构建验证 |

#### CI/Nightly 与 Profile 映射

| Workflow | Profile | HTTP_ONLY_MODE | SKIP_DEGRADATION_TEST | VERIFY_FULL | GATE_PROFILE |
|----------|---------|----------------|----------------------|-------------|--------------|
| ci.yml (PR) | http_only | **1** | **1** | *(不设置)* | http_only |
| nightly.yml | full | **0** | **0** | **1** | full |

> **重要**: CI/Nightly workflow 中的环境变量设置必须与 Makefile acceptance targets 的显式设置保持一致。
> 静态检查脚本 `scripts/ci/check_env_consistency.py` 会自动校验这些值的一致性。

#### 产物记录与追溯

所有 acceptance 运行通过 `scripts/acceptance/record_acceptance_run.py` 记录：

| Workflow | 产物目录 | 关键产物 | 记录文件 | 保留天数 |
|----------|----------|----------|----------|----------|
| ci.yml | `.artifacts/acceptance-unified-min/` | `summary.json`, `steps.log`, `verify-results.json` | `.artifacts/acceptance-runs/<timestamp>_acceptance-unified-min.json` | 30 |
| nightly.yml | `.artifacts/acceptance-unified-full/` | `summary.json`, `steps.log`, `verify-results.json` | `.artifacts/acceptance-runs/<timestamp>_acceptance-unified-full.json` | 30 |

#### 实现产物清单

验收测试执行后生成的产物文件说明：

| 产物文件 | 生成方式 | 内容说明 |
|----------|----------|----------|
| `summary.json` | Makefile acceptance target 生成 | 验收摘要：name、result、环境变量、耗时 |
| `steps.log` | Makefile acceptance target 生成 | 各步骤执行日志（含时间戳和状态） |
| `verify-results.json` | `verify-unified` 步骤生成 | 统一栈验证详细结果（健康检查、API 测试） |
| `test-results-index.json` | Makefile acceptance target 生成 | 测试报告文件索引 |
| `diagnostics/` | 失败时收集 | 服务状态、日志、配置诊断信息 |
| `<timestamp>_<name>.json` | `record_acceptance_run.py` 生成 | 标准化的验收运行记录（含 metadata） |

**summary.json 示例结构**：

```json
{
  "name": "acceptance-unified-min",
  "result": "PASS",
  "failed_step": null,
  "start": "2026-01-30T14:30:22Z",
  "end": "2026-01-30T14:33:45Z",
  "duration_seconds": 203,
  "environment": {
    "HTTP_ONLY_MODE": "1",
    "SKIP_DEGRADATION_TEST": "1",
    "GATE_PROFILE": "http_only"
  }
}
```

**steps.log 示例**：

```
# acceptance-unified-min run started at 2026-01-30T14:30:22Z

环境语义:
  HTTP_ONLY_MODE=1
  SKIP_DEGRADATION_TEST=1
  GATE_PROFILE=http_only

[OK] deploy - 统一栈部署完成
[OK] verify-unified - 统一栈验证通过
[OK] test-logbook-unit - Logbook 单元测试通过
[OK] test-gateway-integration - Gateway 集成测试通过

# Run ended at 2026-01-30T14:33:45Z
# Result: PASS
```

---

### 验收入口对照表

| 命令 | 适用场景 | 包含组件 | 产出目录 | 关键产物 |
|------|----------|----------|----------|----------|
| `acceptance-logbook-only` | Logbook 独立验证 | PostgreSQL + Logbook | `.artifacts/acceptance-logbook-only/` | summary.json, steps.log, health.json, test-results-index.json, diagnostics/ |
| `acceptance-unified-min` | CI PR 快速验证 | 统一栈（HTTP 模式） | `.artifacts/acceptance-unified-min/` | summary.json, steps.log, verify-results.json, test-results-index.json, diagnostics/ |
| `acceptance-unified-full` | Nightly/发布前 | 完整统一栈 | `.artifacts/acceptance-unified-full/` | summary.json, steps.log, verify-results.json, test-results-index.json, diagnostics/ |

### 产出目录结构说明

每个 acceptance 目标会在 `.artifacts/acceptance-<target>/` 目录下生成以下文件：

```
.artifacts/acceptance-<target>/
├── summary.json           # 验收摘要（结果、时间、环境变量等）
├── steps.log              # 步骤执行日志
├── verify-results.json    # verify-unified 结果（仅 unified-* 目标）
├── health.json            # 健康检查结果（仅 logbook-only 目标）
├── test-results-index.json  # 本次运行产生的 JUnit XML 文件路径索引
└── diagnostics/           # 失败时的诊断信息（仅在失败时生成）
    ├── summary.txt        # 诊断摘要
    ├── compose-ps.txt     # Docker Compose 服务状态
    ├── compose-config.yml # 渲染后的 Compose 配置
    ├── compose-logs.txt   # 容器日志（最后 500 行）
    ├── pg-extension.txt   # PostgreSQL 扩展和 Schema 信息
    ├── health-gateway.json     # Gateway 健康检查结果
    └── health-openmemory.json  # OpenMemory 健康检查结果
```

#### test-results-index.json 格式

`test-results-index.json` 提供本次验收运行产生的所有 JUnit XML 测试报告路径，便于 CI 追溯：

```json
{
  "generated_at": "2026-01-30T14:30:22+00:00",
  "acceptance_target": "acceptance-unified-min",
  "result": "PASS",
  "test_results": [
    ".artifacts/test-results/logbook-unit.xml",
    ".artifacts/test-results/gateway.xml"
  ]
}
```

#### diagnostics/ 目录说明

`diagnostics/` 子目录仅在验收步骤失败时生成，包含用于调试的诊断信息。可通过 `DIAG_OUTPUT_DIR` 环境变量自定义诊断输出位置。

### 分步验收（统一栈）

```bash
make deploy                    # 1. 部署
make verify-unified            # 2. 统一栈验证
make logbook-smoke             # 3. Logbook 冒烟测试
make test-logbook-unit         # 4. Logbook 单元测试
make test-gateway-integration  # 5. Gateway 集成测试
```

### 分步验收（Logbook-only）

```bash
make up-logbook                    # 1. 启动 Logbook 服务
make migrate-logbook-stepwise      # 2. 数据库迁移
make verify-permissions-logbook    # 3. 权限验证
make logbook-smoke                 # 4. 冒烟测试
make test-logbook-unit             # 5. 单元测试
```

---

## 输出级别定义（SKIP/NOTICE/WARN/FAIL）

### 输出级别说明

| 级别 | 含义 | 退出码 | 使用场景 |
|------|------|--------|----------|
| **PASS** | 测试/检查通过 | 0 | 正常通过的测试或验证 |
| **SKIP** | 测试被跳过 | 0 | 功能被禁用或前置条件不满足 |
| **NOTICE** | 提示信息 | 0 | 非关键信息，供参考 |
| **WARN** | 警告 | 0 | 可能存在问题但不阻塞 |
| **FAIL** | 失败 | 1 | 必须修复的问题 |

### 失败场景

| 失败场景 | 期望输出 | 修复指导 |
|----------|----------|----------|
| OM_PG_SCHEMA=public | `[FAIL] OM_PG_SCHEMA=public 是禁止的配置！` | 改为 `openmemory` 或其他非 public schema |
| 缺少服务账号密码 | `[FAIL] *_PASSWORD 未设置` | 设置对应的 PASSWORD 环境变量 |
| 权限验证失败 | `FAIL: 角色 xxx 不存在` | 执行 `make bootstrap-roles` 修复权限 |

### 降级场景

| 降级场景 | 期望输出 | 影响范围 |
|----------|----------|----------|
| OpenMemory 不可用 | `[WARN] OpenMemory 不可达，降级到本地缓存` | 语义记忆暂不可用 |
| PostgreSQL 连接失败 | `[FAIL] 数据库连接失败` | 服务不可用 |
| MinIO 不可用 | `[WARN] 对象存储不可用，制品上传降级` | 制品存储功能受限 |
| 嵌入服务不可用 | `[NOTICE] 降级到 synthetic 嵌入` | 向量检索质量下降 |

### Acceptance 测试输出契约

验收测试应遵循以下输出格式：

```bash
# 成功场景
[OK] 组件名称 完成
[PASS] 测试名称

# 跳过场景
[SKIP] 组件名称 已跳过 (原因)
SKIPPED (原因)

# 警告场景
[WARN] 警告消息

# 失败场景
[FAIL] 组件名称 失败
[ERROR] 错误消息
```

---

## 自动验收记录

每次执行 `make acceptance-*` 命令会自动生成验收记录，存储在 `.artifacts/acceptance-runs/` 目录下。

### 记录文件格式

文件名格式：`<timestamp>_<name>.json`

示例：`20260130T143022Z_acceptance-logbook-only.json`

### 记录字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | 验收命令名称（如 `acceptance-logbook-only`） |
| `timestamp` | string | ISO 8601 UTC 时间戳 |
| `result` | string | `PASS` / `FAIL` / `PARTIAL` |
| `commit` | string | Git commit SHA（自动检测或手动指定） |
| `os_version` | string | 操作系统及版本（如 `Darwin 24.6.0 (arm64)`） |
| `docker_version` | string | Docker 版本（如有） |
| `environment` | object | 关键环境变量（敏感值已脱敏） |
| `command` | string | 执行的命令（默认 `make {name}`，可通过 `--command` 覆盖） |
| `duration_seconds` | number | 执行耗时（秒，如可用） |
| `artifacts_dir` | string | 产物目录路径 |
| `artifacts` | array | 产物文件路径列表 |
| `metadata` | object | **可选**，自定义元数据（通过 `--metadata-json` 或 `--metadata-kv` 传入） |

#### metadata 字段说明

`metadata` 是可选的扩展字段，用于记录 CI/CD 上下文信息。常见的 metadata key 包括：

| Key | 说明 | 示例值 |
|-----|------|--------|
| `workflow` | 工作流类型 | `ci` / `nightly` |
| `profile` | 验收 profile | `http_only` / `full` |
| `github_run_id` | GitHub Actions run ID | `12345678` |
| `github_sha` | GitHub Actions 触发的 commit | `abc123...` |
| `triggered_by` | 触发方式 | `push` / `schedule` / `workflow_dispatch` |
| `run_number` | GitHub Actions workflow run 序号 | `42` |
| `event_name` | GitHub event 类型 | `pull_request` / `push` / `schedule` |
| `http_only_mode` | HTTP_ONLY_MODE 设置值 | `1` / `0` |
| `skip_degradation` | SKIP_DEGRADATION_TEST 设置值 | `1` / `0` |

#### command 字段说明

`command` 字段记录实际执行的命令或步骤序列：

| 场景 | command 值示例 |
|------|---------------|
| 本地 `make acceptance-unified-min` | `make acceptance-unified-min` |
| CI 组合式覆盖 | `deploy → verify-unified(profile=http_only) → openmemory-audit → test-gateway-integration [depends: precheck-static, ...]` |
| Nightly 直接执行 | `make acceptance-unified-full SKIP_DEPLOY=1` |

CI 组合式覆盖的 command 格式说明：
- `→` 分隔顺序执行的步骤
- `[depends: ...]` 列出前置依赖的 job（非 acceptance 步骤本身）
- 括号内参数如 `profile=http_only` 表示环境变量设置

### 示例记录

**基础示例**（无 metadata）：

```json
{
  "name": "acceptance-logbook-only",
  "timestamp": "2026-01-30T14:30:22+00:00",
  "result": "PASS",
  "commit": "abc1234def5678...",
  "os_version": "Darwin 24.6.0 (arm64)",
  "docker_version": "Docker version 24.0.6, build ed223bc",
  "environment": {
    "SKIP_DEPLOY": "0",
    "POSTGRES_DSN": "postgresql://user:***@localhost:5432/engram"
  },
  "command": "make acceptance-logbook-only",
  "duration_seconds": 45,
  "artifacts_dir": ".artifacts/acceptance-logbook-only",
  "artifacts": [
    ".artifacts/acceptance-logbook-only/summary.json",
    ".artifacts/acceptance-logbook-only/steps.log",
    ".artifacts/acceptance-logbook-only/health.json"
  ]
}
```

**CI 示例**（含 metadata）：

```json
{
  "name": "acceptance-unified-min",
  "timestamp": "2026-01-30T15:00:00+00:00",
  "result": "PASS",
  "commit": "def5678abc1234...",
  "os_version": "Linux 5.15.0 (x86_64)",
  "docker_version": "Docker version 24.0.6, build ed223bc",
  "environment": {
    "HTTP_ONLY_MODE": "1",
    "GATE_PROFILE": "http_only"
  },
  "command": "make acceptance-unified-min HTTP_ONLY_MODE=1",
  "duration_seconds": 180,
  "artifacts_dir": ".artifacts/acceptance-unified-min",
  "artifacts": [
    ".artifacts/acceptance-unified-min/summary.json",
    ".artifacts/acceptance-unified-min/steps.log"
  ],
  "metadata": {
    "workflow": "ci",
    "profile": "http_only",
    "github_run_id": "12345678",
    "triggered_by": "push"
  }
}
```

### 手动生成记录

如需手动记录验收运行（例如分步执行后），可使用：

```bash
python3 scripts/acceptance/record_acceptance_run.py \
    --name acceptance-logbook-only \
    --artifacts-dir .artifacts/acceptance-logbook-only \
    --result PASS \
    [--commit <sha>] \
    [--command <custom command>] \
    [--metadata-json '{"workflow": "ci", "profile": "http_only"}'] \
    [--metadata-kv workflow=ci --metadata-kv profile=http_only]
```

#### 参数说明

| 参数 | 必需 | 说明 |
|------|------|------|
| `--name` | ✅ | 验收命令名称 |
| `--artifacts-dir` | ✅ | 产物目录路径 |
| `--result` | ✅ | 结果（`PASS` / `FAIL` / `PARTIAL`） |
| `--commit` | ❌ | Git commit SHA（自动检测） |
| `--command` | ❌ | 自定义命令（默认 `make {name}`） |
| `--metadata-json` | ❌ | JSON 格式的元数据 |
| `--metadata-kv` | ❌ | key=value 格式的元数据（可多次使用） |

#### CI 集成示例

```bash
# GitHub Actions CI 示例
python3 scripts/acceptance/record_acceptance_run.py \
    --name acceptance-unified-min \
    --artifacts-dir .artifacts/acceptance-unified-min \
    --result PASS \
    --command "make acceptance-unified-min HTTP_ONLY_MODE=1" \
    --metadata-json '{"workflow": "ci", "profile": "http_only"}' \
    --metadata-kv "github_run_id=${GITHUB_RUN_ID}" \
    --metadata-kv "triggered_by=${GITHUB_EVENT_NAME}"

# Nightly 验收示例
python3 scripts/acceptance/record_acceptance_run.py \
    --name acceptance-unified-full \
    --artifacts-dir .artifacts/acceptance-unified-full \
    --result PASS \
    --command "make acceptance-unified-full VERIFY_FULL=1" \
    --metadata-kv workflow=nightly \
    --metadata-kv profile=full
```

**注意**：`--metadata-kv` 的值会覆盖 `--metadata-json` 中的同名 key。

### 查询历史记录

```bash
# 列出所有验收记录
ls -la .artifacts/acceptance-runs/

# 查看最新记录
cat .artifacts/acceptance-runs/$(ls -t .artifacts/acceptance-runs/ | head -1)

# 按名称筛选
ls .artifacts/acceptance-runs/*acceptance-logbook-only*
```

### 自动汇总产物

CI/Nightly 工作流会自动生成验收矩阵汇总产物，聚合 `.artifacts/acceptance-runs/*.json` 中的记录。

#### 产物文件

| 文件 | 格式 | 说明 |
|------|------|------|
| `.artifacts/acceptance-matrix.md` | Markdown | 人类可读的验收趋势表格 |
| `.artifacts/acceptance-matrix.json` | JSON | 结构化数据，可用于进一步分析 |

#### 生成方式

**手动生成**:

```bash
# 使用 Makefile 目标
make acceptance-matrix

# 自定义参数
make acceptance-matrix MATRIX_LIMIT=10 MATRIX_OUTPUT_DIR=.artifacts

# 直接调用脚本
python3 scripts/acceptance/render_acceptance_matrix.py \
    --limit 5 \
    --output-dir .artifacts \
    --runs-dir .artifacts/acceptance-runs
```

**CI/Nightly 自动生成**:

- `ci.yml`: 在 `unified-standard` job 完成后自动生成（`if: always()`）
- `nightly.yml`: 在 `acceptance-unified-full` 完成后自动生成（`if: always()`）

#### 产物内容示例

**Markdown 汇总 (`acceptance-matrix.md`)**:

| Name | Profile | Workflow | Pass Rate | Latest | Commit | Avg Duration |
|------|---------|----------|-----------|--------|--------|--------------|
| acceptance-unified-min | http_only | ci | ✅ 100% | ✅ PASS | `abc1234` | 180s |
| acceptance-unified-full | full | nightly | 🟡 80% | ✅ PASS | `def5678` | 420s |

**JSON 结构 (`acceptance-matrix.json`)**:

```json
{
  "generated_at": "2026-01-30T12:00:00Z",
  "limit_per_group": 5,
  "groups": [
    {
      "name": "acceptance-unified-min",
      "profile": "http_only",
      "workflow": "ci",
      "stats": {
        "count": 5,
        "pass_count": 5,
        "fail_count": 0,
        "pass_rate": 1.0,
        "avg_duration_seconds": 180.0,
        "latest_result": "PASS"
      },
      "records": [...]
    }
  ],
  "summary": {
    "total_groups": 3,
    "total_records": 15,
    "overall_pass_rate": 0.93
  }
}
```

#### CI Artifact 下载

验收矩阵作为 CI artifact 上传，可从 GitHub Actions 页面下载：

- **CI**: `acceptance-matrix-{profile}-{run_number}`
- **Nightly**: `nightly-acceptance-matrix-{run_number}`

#### 脚本参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--limit N` | 每组显示最近 N 条记录 | 5 |
| `--output-dir` | 输出目录 | `.artifacts` |
| `--runs-dir` | acceptance-runs 目录 | `.artifacts/acceptance-runs` |
| `--json-only` | 仅输出 JSON | false |
| `--md-only` | 仅输出 Markdown | false |

### 在验收矩阵中引用记录

在填写迭代验收记录时，可引用自动生成的记录文件：

```markdown
**验收记录**: `.artifacts/acceptance-runs/<timestamp>_<name>.json`（本地执行记录，commit `<sha>`）
```

---

## 证据引用规范

本节定义在迭代文档和验收记录中引用证据的规范格式。

> **完整策略**：参见 [ADR: 迭代文档工作流](../architecture/adr_iteration_docs_workflow.md#35-版本化证据文件)

### 引用格式对照表

| 证据来源 | 引用格式 | 示例 | 说明 |
|----------|----------|------|------|
| **版本化文档** | 相对路径 Markdown 链接 | `[计划](iteration_13_plan.md)` | ✅ 推荐 |
| **版本化证据文件** | 相对路径 Markdown 链接 | `[证据](evidence/iteration_13_evidence.json)` | ✅ 推荐，用于结构化证据 |
| **CI Run URL** | 完整 URL | `[CI #1234](https://github.com/.../runs/1234)` | ✅ 最推荐，永久有效 |
| **CI Artifact** | URL + 说明 | `报告见 CI Artifacts (90 天有效)` | ⚠️ 有时效性 |
| **本地草稿 (`.iteration/`)** | **禁止链接**，仅文本提及 | `参考 .iteration/ 中的草稿` | ❌ Markdown 链接禁止 |
| **运行时产物 (`.artifacts/`)** | **禁止链接**，仅文本提及 | `本地产物位于 .artifacts/` | ❌ Markdown 链接禁止 |

### `.artifacts/` 和 `.iteration/` 引用约束

**核心规则**：`.artifacts/` 与 `.iteration/` 一样，**不得在版本化文档中以 Markdown 链接形式出现**。

| 类型 | `.artifacts/` 示例 | `.iteration/` 示例 | 允许 |
|------|-------------------|-------------------|------|
| **Markdown 链接** | `[报告]` + `(.artifacts/...)` | `[草稿]` + `(.iteration/...)` | ❌ **禁止** |
| **文本提及** | `本地产物位于 .artifacts/` | `参考 .iteration/ 中的草稿` | ✅ 允许 |
| **inline code** | `` `.artifacts/test-results.xml` `` | `` `.iteration/13/plan.md` `` | ✅ 允许 |

**理由**：

1. `.artifacts/` 和 `.iteration/` 均在 `.gitignore` 中，不纳入版本控制
2. 链接指向的文件在其他机器或 CI 环境中不存在，必然失效
3. CI Artifacts 有保留期限（通常 30-90 天），链接会过期

### 版本化证据文件

当需要持久化结构化证据时，应将证据文件存储在 `docs/acceptance/evidence/` 目录。

> **完整策略**：参见 [ADR: 迭代文档工作流 - 3.5 版本化证据文件](../architecture/adr_iteration_docs_workflow.md#35-版本化证据文件)

**命名规范（统一）**：

| 类型 | 格式 | 示例 | 说明 |
|------|------|------|------|
| **Canonical（规范）** | `iteration_<N>_evidence.json` | `iteration_13_evidence.json` | ✅ 推荐，单一迭代综合证据，每次更新覆盖 |
| **Snapshot（快照）** | `iteration_<N>_<YYYYMMDD_HHMMSS>.json` | `iteration_13_20260202_143000.json` | 历史快照，需保留多次验收记录时手动创建 |
| **Snapshot+SHA** | `iteration_<N>_<YYYYMMDD_HHMMSS>_<sha7>.json` | `iteration_13_20260202_143000_abc1234.json` | 带 commit SHA 的历史快照 |

**生成命令**：

```bash
# 生成 canonical 证据文件（推荐）
python scripts/iteration/record_iteration_evidence.py <N>

# 指定 CI 运行 URL
python scripts/iteration/record_iteration_evidence.py <N> \
  --ci-run-url https://github.com/<org>/<repo>/actions/runs/<run_id>

# 预览模式（不实际写入）
python scripts/iteration/record_iteration_evidence.py <N> --dry-run
```

**回归文档引用示例**：

```markdown
## 验收证据

详细证据见 [iteration_13_evidence.json](evidence/iteration_13_evidence.json)。

关键指标摘要：
- CI 门禁：✅ 全部通过
- 测试覆盖：608 passed, 0 failed
- CI Run: [GitHub Actions #1234](https://github.com/.../actions/runs/1234)
```

**注意**：❌ 禁止手动创建包含占位符的草稿证据文件并提交，应使用脚本生成。

### 推荐引用方式优先级

1. **CI Run URL**（最推荐）：永久有效、可追溯、包含完整上下文
2. **版本化证据文件链接**：适用于需要结构化数据的场景
3. **文档内嵌摘要**：关键信息在文档中直接可见，无需跳转

---

## 历史验收记录索引

| 日期 | Commit | 结果 | 记录文件 | 备注 |
|------|--------|------|----------|------|
| 2026-02-01 | - | **PARTIAL** | [iteration_10_regression.md](iteration_10_regression.md) | **Iteration 10 回归验证**：lint ✅，mypy ❌ (86 新增错误)，gateway 测试 ⚠️ (15 失败/807 通过)，acceptance ✅ (158 通过) |
| 2026-02-01 | - | **PARTIAL** | [iteration_9_regression.md](iteration_9_regression.md) | **Iteration 9 回归验证**：lint ✅，mypy ❌ (77 新增错误)，gateway 测试 ⚠️ (4 失败/813 通过)，acceptance ✅ (143 通过) |
| 2026-01-30 | `4d5d607` | **PASS** | `.artifacts/acceptance-runs/20260130T000804Z_acceptance-logbook-only.json`（本地） | acceptance-logbook-only 通过；执行步骤：`up-logbook` → `migrate-logbook-stepwise` → `verify-permissions-logbook` → `logbook-smoke` → `test-logbook-unit` |
| 2026-01-30 | `4d5d607` | **PASS** | `.artifacts/acceptance-runs/20260130T000805Z_acceptance-unified-min.json`（本地） | acceptance-unified-min 通过；执行步骤：`deploy` → `verify-unified (HTTP_ONLY_MODE)` → `test-logbook-unit` → `test-gateway-integration (HTTP_ONLY_MODE)` |
| 2026-01-30 | - | PASS | `.artifacts/naming-audit/iteration10_step_tokens.txt`, `.artifacts/naming-audit/iteration10_legacy_alias_scan.json`（本地审计记录） | **迭代 10 命名治理审计**：step token 扫描 95 处（均为合规的 `Step N` 流程编号格式）；legacy alias 检测 0 处违规（570 文件扫描通过） |

### 未覆盖范围（2026-01-30）

| 测试命令 | 状态 | 原因 | 风险等级 |
|----------|------|------|----------|
| `acceptance-unified-full` | 未执行 | 当前迭代侧重文档整理，full 验收待 Nightly 补充 | **中** |

**风险评估**:

- **中风险**: `acceptance-unified-full` 未执行，降级测试（degradation）和完整集成测试（`test-gateway-integration-full`）未验证。建议在下次 Nightly 或发布前补充执行。
- **低风险**: `acceptance-logbook-only` 和 `acceptance-unified-min` 已通过，核心功能和 CI PR 场景已覆盖。

---

## 门禁 Profile 与验证步骤映射

> **单一来源**: `scripts/unified_stack_gate_contract.py`

验证流程的门禁规则由 `unified_stack_gate_contract.py` 定义，本节文档与其保持同步。

### Profile 定义

| Profile | 环境变量条件 | 描述 |
|---------|--------------|------|
| **http_only** | `HTTP_ONLY_MODE=1` 或 `GATE_PROFILE=http_only` | 仅 HTTP 接口验证（无 MCP JSON-RPC） |
| **standard** | 默认，或 `SKIP_DEGRADATION_TEST=1` | 标准模式（HTTP + JSON-RPC，无降级测试） |
| **full** | `GATE_PROFILE=full` 或显式调用 `--full` | 完整模式（所有步骤，包括降级测试和 DB 不变量检查） |

### 各 Profile 的 required_steps 与 must_fail_if_blocked 映射

引用自 `scripts/unified_stack_gate_contract.py::PROFILE_CONFIGS`:

#### http_only Profile

```python
# required_steps:
- health_checks
- memory_store
- memory_query

# optional_steps:
- db_invariants

# required_capabilities:
- openmemory_endpoint_present

# must_fail_if_blocked: []  # 无强制失败项
```

**行为**: 缺少能力时可跳过（skip），不会强制失败。

#### standard Profile

```python
# required_steps:
- health_checks
- memory_store
- memory_query
- jsonrpc

# optional_steps:
- db_invariants

# required_capabilities:
- openmemory_endpoint_present

# must_fail_if_blocked: []  # 无强制失败项
```

**行为**: 与 http_only 类似，增加了 JSON-RPC 协议验证。缺少能力时可跳过。

#### full Profile

```python
# required_steps:
- health_checks
- db_invariants
- memory_store
- memory_query
- jsonrpc
- degradation

# optional_steps: []  # 无可选步骤

# required_capabilities:
- openmemory_endpoint_present
- docker_available
- docker_daemon_ok
- can_stop_openmemory
- db_access_available  # psql 或 psycopg 之一
- postgres_dsn_present

# must_fail_if_blocked:
- degradation      # 缺少 can_stop_openmemory 必须 FAIL
- db_invariants    # 缺少 postgres_dsn 或 db_access 必须 FAIL
```

**行为**: 关键步骤缺少能力时**必须 FAIL**，不能静默跳过。这是生产发布前的硬性门禁。

### Capability 检测

| Capability | 检测方式 | 影响的步骤 |
|------------|----------|------------|
| `docker_available` | `shutil.which("docker")` | degradation |
| `docker_daemon_ok` | `docker info` 返回 0 | degradation |
| `can_stop_openmemory` | Docker + compose 可用且有配置 | degradation |
| `psql_available` | `shutil.which("psql")` | db_invariants |
| `psycopg_available` | `import psycopg2` 或 `import psycopg` | db_invariants |
| `db_access_available` | psql 或 psycopg 之一可用 | db_invariants |
| `postgres_dsn_present` | `POSTGRES_DSN` 环境变量已设置 | db_invariants |
| `openmemory_endpoint_present` | `OPENMEMORY_BASE_URL` 或 `OPENMEMORY_ENDPOINT` 环境变量已设置 | 所有步骤 |

### 验证 Profile 命令

```bash
# 检测当前环境能力
python scripts/unified_stack_gate_contract.py detect-capabilities

# 校验指定 profile 是否可执行
python scripts/unified_stack_gate_contract.py validate-profile full

# 获取指定 profile 的必需步骤
python scripts/unified_stack_gate_contract.py get-required-steps full

# 从环境变量推断当前 profile
python scripts/unified_stack_gate_contract.py get-profile

# 导出完整规则表（JSON 格式，供 Bash/其他工具解析）
python scripts/unified_stack_gate_contract.py dump-rules
```

### Profile 与 Makefile 目标对照

| Makefile 目标 | 对应 Profile | 说明 |
|---------------|--------------|------|
| `make test-gateway-integration` | http_only/standard | `HTTP_ONLY_MODE=1` 时为 http_only |
| `make test-gateway-integration-full` | full | 需要 Docker 权限和 POSTGRES_DSN |
| `make verify-unified` | standard | 基础验证（自动判断模式） |
| `VERIFY_FULL=1 make verify-unified` | full | 完整验证（含降级测试） |
| `make acceptance-unified-min` | standard | CI PR 快速验证 |
| `make acceptance-unified-full` | full | Nightly/发布前完整验收 |

### 测试行为矩阵

| 测试类/场景 | http_only | standard | full（缺能力时） |
|-------------|-----------|----------|------------------|
| `TestServiceHealthCheck` | 运行 | 运行 | 运行 |
| `TestMemoryOperations` | 运行 | 运行 | 运行 |
| `TestJsonRpcProtocol` | 跳过 | 运行 | 运行 |
| `TestDegradationFlow` | 跳过 | 跳过 | **FAIL** |
| `TestOutboxWorkerRealIntegration` | 跳过 | 跳过 | **FAIL** |
| `TestDatabaseRolesVerification` | 跳过（无 DSN） | 跳过（无 DSN） | **FAIL** |

### Outbox Worker 真实集成测试（FULL 必测）

**重要**：以下 Outbox Worker 集成测试在 `acceptance-unified-full` 中为**必测项**：

| 测试类 | 测试方法 | 验证点 | HTTP_ONLY 行为 |
|--------|----------|--------|----------------|
| `TestOutboxWorkerIntegrationSuccess` | `test_success_path_status_transition` | outbox 状态 `pending→sent`，审计 `outbox_flush_success` | SKIP |
| `TestOutboxWorkerIntegrationRetry` | `test_retry_path_status_and_retry_count` | outbox 状态保持 `pending`，审计 `outbox_flush_retry` | SKIP |
| `TestOutboxWorkerIntegrationRetry` | `test_retry_path_becomes_dead_after_max_retries` | outbox 状态 `pending→dead`，审计 `outbox_flush_dead` | SKIP |
| `TestOutboxDegradationRecoveryE2E` | `test_degradation_to_outbox_recovery_flush_audit_consistency` | 完整降级→恢复流程，含 Docker stop/start | SKIP |

**必需能力**：
- `docker_available`: Docker 可执行文件存在
- `docker_daemon_ok`: Docker daemon 运行中
- `can_stop_openmemory`: 可以 stop/start OpenMemory 容器
- `postgres_dsn_present`: `POSTGRES_DSN` 环境变量已设置

**审计验证点**（FULL 必测）：
1. **状态流转断言**：outbox 记录在 `pending`/`sent`/`dead` 三种状态之间正确流转
2. **审计 reason 断言**：`governance.write_audit` 记录的 `reason` 字段正确为：
   - `outbox_flush_success`：成功写入 OpenMemory
   - `outbox_flush_retry`：可重试失败，已安排重试
   - `outbox_flush_dead`：不可恢复失败，标记为死信
3. **evidence_refs_json 可查询**：审计记录中 `(evidence_refs_json->>'outbox_id')::int` 可正确关联回 outbox 记录

**跳过输出契约**：
当 `HTTP_ONLY_MODE=1` 时，上述测试应输出明确的 SKIP 信息：
```
SKIPPED (HTTP_ONLY_MODE: Outbox Worker 集成测试需要 Docker 和数据库)
```

---

## Import 迁移验收检查

本节说明根目录 Wrapper 模块迁移的验收检查项。

### 迁移验收 Checklist

| # | 检查项 | 验证命令 | 通过标准 |
|---|--------|----------|----------|
| 1 | 无新增弃用模块导入 | `make check-no-root-wrappers` | 退出码 0 |
| 2 | Allowlist 条目未过期 | `python scripts/ci/check_no_root_wrappers_allowlist.py` | 无过期警告 |
| 3 | CLI 入口一致性 | `make check-cli-entrypoints` | pyproject.toml 与文档一致 |
| 4 | 弃用警告正确输出 | 手动验证（见下方） | 警告格式正确 |
| 5 | 测试覆盖迁移后路径 | `pytest tests/ -v -k "cli or import"` | 无测试失败 |

### 弃用警告验证

迁移后，弃用入口应输出统一格式的警告：

```bash
# 验证根目录入口
python artifact_cli.py --help 2>&1 | head -15

# 期望输出包含：
# ⚠️  DEPRECATION WARNING
# [DEPRECATED] 'artifact_cli.py' 已弃用，计划在 v2.0 版本移除。
# 请使用以下方式替代:
#     - engram-artifacts [args]
```

### 迁移相关测试用例

| 测试文件 | 验证内容 | 需要例外声明 |
|----------|----------|--------------|
| `tests/logbook/test_artifacts_cli.py` | `engram-artifacts` CLI 功能 | 否 |
| `tests/logbook/test_artifact_gc.py` | Artifact GC 功能（使用新路径） | 否 |
| `tests/logbook/test_identity_sync.py` | 身份同步功能 | 否 |
| `tests/gateway/test_importerror_optional_deps_contract.py` | 可选依赖缺失时的错误处理 | 否 |
| `tests/logbook/test_deprecation_warnings.py`（如有） | 弃用警告正确发出 | **是**（测试弃用行为） |

### 批量迁移 PR 验收

提交批量迁移 PR 时，除常规 CI 检查外，还需验证：

| 检查项 | 说明 |
|--------|------|
| **测试无新增失败** | 对比 PR 前后的测试通过数 |
| **无遗漏的 import** | `make check-no-root-wrappers --verbose` 无新增违规 |
| **例外声明有效** | 新增的 inline marker 格式正确、未过期 |
| **文档同步更新** | 迁移映射文档与代码一致 |

### 迁移验收与常规验收的关系

迁移验收检查已集成到常规 CI 流程：

| CI Job | 包含的迁移检查 |
|--------|----------------|
| **lint** | - |
| **no-root-wrappers-usage** | `check_no_root_wrappers_usage.py`（核心检查） |
| **cli-entrypoints-consistency** | CLI 入口与 pyproject.toml 一致性 |
| **test** | 测试迁移后的 import 路径是否正常工作 |

无需单独运行迁移验收，`make ci` 已覆盖所有迁移相关检查。

---

## 附录：验收标准

### PASS 标准

- 所有核心功能测试通过
- 健康检查端点正常响应
- 无阻塞性缺陷

### PARTIAL 标准

- 核心功能可用
- 存在未覆盖的测试范围
- 已知限制已记录且风险可控

### FAIL 标准

- 核心功能不可用
- 阻塞性缺陷未解决
- 关键测试失败