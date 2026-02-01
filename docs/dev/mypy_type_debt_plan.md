# mypy 类型债务清理计划

> 状态: **生效中**  
> 创建日期: 2026-02-01  
> 最后更新: 2026-02-01T06:46:37+00:00  
> 决策者: Engram Core Team

---

## 1. 概述

本文档记录当前 mypy 类型错误的详细分布，制定 Iteration 10 及后续迭代的债务清理预算与策略，
并明确 strict-island 扩面准入条件。

**关联文档**：
- [mypy 基线管理操作指南](./mypy_baseline.md)
- [mypy 错误码修复 Playbook](./mypy_error_playbook.md)
- [ADR: mypy 基线管理与 Gate 门禁策略](../architecture/adr_mypy_baseline_and_gating.md)
- [ADR: Logbook Strict Island 扩展计划](../architecture/adr_logbook_strict_island_expansion_config_uri_db.md)

---

## 2. 当前指标快照

> **⚠️ 快照格式规范（强制）**
>
> | 字段 | 要求 |
> |------|------|
> | **SSOT 数据源** | 基线文件: `scripts/ci/mypy_baseline.txt`<br>指标脚本: `scripts/ci/mypy_metrics.py` |
> | **生成命令** | `python scripts/ci/mypy_metrics.py --stdout --verbose` |
> | **更新时间戳** | 必须标注 ISO 8601 格式时间 |
> | **更新频率** | 每周五或 baseline 变更后更新 |

---

> **数据来源**: `scripts/ci/mypy_baseline.txt` → `scripts/ci/mypy_metrics.py`  
> **生成命令**: `make typecheck-gate && make typecheck-strict-island && make mypy-metrics`  
> **快照时间**: 2026-02-01T09:08:01+00:00

### 2.1 汇总统计

> **⚠️ 动态口径**：以下数值以 `scripts/ci/mypy_baseline.txt` 为准，请运行验证命令获取实时数据。

| 指标 | 获取方式 |
|------|----------|
| 总错误数 | `grep -c "error:" scripts/ci/mypy_baseline.txt` |
| 总 note 数 | `grep -c "note:" scripts/ci/mypy_baseline.txt` |
| 总行数 | `wc -l < scripts/ci/mypy_baseline.txt` |

**验证命令**:
```bash
# 获取当前统计
echo "总行数: $(wc -l < scripts/ci/mypy_baseline.txt)"
echo "错误数: $(grep -c 'error:' scripts/ci/mypy_baseline.txt || echo 0)"
echo "note数: $(grep -c 'note:' scripts/ci/mypy_baseline.txt || echo 0)"

# 完整检查
make typecheck-gate
make typecheck-strict-island
```

### 2.2 按目录分布

> **动态查询**：
> ```bash
> grep -o 'src/engram/[^/]*/' scripts/ci/mypy_baseline.txt | sort | uniq -c | sort -rn
> ```

### 2.3 按 error-code 分布

> **动态查询**：
> ```bash
> grep -oE '\[[a-z-]+\]' scripts/ci/mypy_baseline.txt | sort | uniq -c | sort -rn
> ```

---

## 3. 文件级债务清单

> **数据来源**: `scripts/ci/mypy_baseline.txt`（`wc -l` 获取行数，逐行检查文件）  
> **查询命令**: `grep -c "error:" scripts/ci/mypy_baseline.txt` 或 `python scripts/ci/mypy_metrics.py --stdout`

### 3.1 Top 错误文件表格（动态查询）

> **动态查询**（按文件聚合错误数）：
> ```bash
> grep "error:" scripts/ci/mypy_baseline.txt | cut -d: -f1 | sort | uniq -c | sort -rn | head -10
> ```

### 3.2 按模块聚合

> **动态查询**：
> ```bash
> grep "error:" scripts/ci/mypy_baseline.txt | grep -oE 'src/engram/(gateway|logbook)' | sort | uniq -c
> ```

> **验证清零状态**：当查询结果为空时，表示该模块已清零。

---

## 3A. 清零决策规则

> **阈值 ≤ 30 时适用**：当 baseline 错误数处于可修复范围内，**默认策略是修复并清零**。
>
> 详细规则参见 [mypy 基线管理 §4.2a 清零决策规则](./mypy_baseline.md#42a-清零决策规则阈值--30)

### 3A.1 决策要点

| 场景 | 策略 | 是否允许净增 |
|------|------|--------------|
| 错误属于可修复类型（导入/Optional/属性/返回值） | **必须修复** | ❌ 禁止 |
| 第三方 stubs 缺失（无 `types-XXX` 包） | 允许净增 | ✅ 必须绑定 issue |
| 类型系统局限（mypy bug） | 允许净增 | ✅ 必须绑定 issue |
| 待上游修复 | 允许净增 | ✅ 必须绑定 issue |

### 3A.2 可修复错误类型

以下错误码**必须修复**，不允许更新 baseline 逃逸：

- `[import-not-found]` / `[import-untyped]` → 安装 stubs 或创建本地 stub
- `[arg-type]` / `[assignment]` → 添加 None 检查或类型收窄
- `[attr-defined]` → 修复类型定义
- `[no-any-return]` / `[return-value]` → 明确返回类型
- `[operator]` → 修复操作数类型

---

## 4. PR 执行节奏

> **重要**：每个修复类型债务的 PR 必须遵循以下节奏规范。

### 4.1 单主题原则

每个 PR **只做一个主题**，便于 review 和回滚。主题示例：

- ✅ `scm_db no-any-return 清零`
- ✅ `artifact_store Optional temp_path 收敛`
- ✅ `gateway/logbook_db.py 类型修复`
- ❌ 混合多个模块的类型修复（难以 review）
- ❌ 同时修复 `no-any-return` 和 `arg-type`（拆分为两个 PR）

### 4.2 PR 验证命令（必须执行）

每个 PR **提交前**必须运行以下命令并在 PR body 附上结果：

```bash
# 1. 运行 baseline 模式检查
python scripts/ci/check_mypy_gate.py --gate baseline --verbose
```

### 4.3 Baseline 回写规则

若 `--gate baseline --verbose` 输出显示 **"已修复 N 个错误"**，必须执行回写：

```bash
# 2. 回写 baseline（确保净减少）
python scripts/ci/check_mypy_gate.py --write-baseline

# 3. 提交 baseline 变更
git add scripts/ci/mypy_baseline.txt
git commit --amend --no-edit  # 或新提交
```

> **注意**：PR 必须确保 baseline **净减少**或**不变**，详见 [baseline policy](../../.github/pull_request_template.md#ci-baseline-变更检查如修改-scriptscimy py_baselinetxt-则必填)。

### 4.4 禁止行为

以下行为 **严禁**，违反将导致 PR 被拒绝：

| 禁止行为 | 说明 | 替代方案 |
|----------|------|----------|
| 批量新增 `# type: ignore` | 为了"过 CI"批量添加忽略注释 | 修复错误或拆分 PR |
| 裸 `# type: ignore`（无 error-code） | 违反 type:ignore 策略 | 使用 `# type: ignore[error-code]` |
| 无原因说明的 ignore（strict-island 内） | 违反 strict-island 策略 | 添加 `# 原因说明` 或 `# TODO: #issue` |
| baseline 净增 > 0 且无说明 | 违反 baseline policy | 在 PR body 填写变更说明 |

> **策略检查脚本**：
> - `python scripts/ci/check_type_ignore_policy.py` — type:ignore 策略
> - `python scripts/ci/check_mypy_baseline_policy.py` — baseline 变更策略

### 4.5 PR 检查清单

```markdown
### mypy 类型修复 PR 检查清单

- [ ] PR 标题清晰表明修复主题（如"fix(mypy): scm_db no-any-return 清零"）
- [ ] 只修复一个主题（单模块或单错误码）
- [ ] 已运行 `python scripts/ci/check_mypy_gate.py --gate baseline --verbose`
- [ ] 如有修复，已运行 `--write-baseline` 回写并提交
- [ ] baseline 净减少或不变（如净增需填写说明）
- [ ] 无裸 `# type: ignore`（均带 `[error-code]`）
- [ ] strict-island 内的 ignore 均有原因说明
```

---

## 5. Iteration 10 预算与策略

### 5.1 预算定义

| 预算项 | 目标值 | 当前状态 | 说明 |
|--------|--------|----------|------|
| **每周净减少** | ≥ 5 条 | ✅ 已超额完成 | 从 37 条降至 8 条 |
| **单 PR 净增** | = 0（默认） | ✅ 保持 | 单个 PR 不允许净增错误 |
| **Iter 10 总目标** | 净减少 ≥ 15 条 | ✅ 已完成（-29 条） | 当前仅剩 8 条 |
| **重点模块** | gateway/ 清零 | ✅ 已完成 | gateway/ 错误数 = 0 |

### 5.2 特殊情况处理

当需要 PR 净增错误时，按 baseline policy 执行：

| 净增数量 | 审批要求 | 必须提供 |
|----------|----------|----------|
| 1-5 条 | Reviewer 批准 | `### CI Baseline 变更检查` section + 原因说明 |
| 6-10 条 | 2 位 Reviewer + `tech-debt` label | 详细说明 + 修复计划 + issue 关联 |
| > 10 条 | ❌ 禁止 | 应拆分 PR |

> **详见**: `scripts/ci/check_mypy_baseline_policy.py` 策略检查脚本

### 5.3 迭代里程碑

| 迭代 | 目标错误数 | 关键任务 | 验收标准 | 状态 |
|------|------------|----------|----------|------|
| **Iter 10**（当前） | ≤ 22 | gateway/ 清零，logbook/ 核心减半 | CI 通过，无 gateway 错误 | ✅ 超额完成（0 条） |
| Iter 11 | ≤ 5 | logbook/ 剩余清理 | logbook/ 模块 ≤ 5 条 | ✅ 提前完成 |
| Iter 12 | 0 | 全面清零 | 可切换到 strict 模式 | ✅ **已达成** |

---

## 6. Strict-Island 扩面准入条件

### 6.1 准入检查清单

模块申请加入 strict-island 前，**必须满足以下全部条件**：

| # | 条件 | 检查方式 | 必须通过 |
|---|------|----------|----------|
| 1 | **mypy 错误数 = 0** | 模块在 baseline 中无任何错误 | ✅ |
| 2 | **overrides 满足 island 约束** | `pyproject.toml` 中配置 `disallow_untyped_defs = true` | ✅ |
| 3 | **type: ignore 合规** | `check_type_ignore_policy.py` 检查通过 | ✅ |
| 4 | **关键测试通过** | 相关单元测试 100% 通过 | ✅ |
| 5 | **无 Any 泛滥** | 函数签名不使用裸 `Any`（允许 `dict[str, Any]` 等） | ✅ |

### 6.2 mypy 错误数 = 0

```bash
# 检查特定模块是否有错误
grep "src/engram/your_module/" scripts/ci/mypy_baseline.txt | wc -l
# 输出必须为 0
```

### 6.3 overrides 配置要求

在 `pyproject.toml` 中添加模块 override：

```toml
[[tool.mypy.overrides]]
module = "engram.your_module"
disallow_untyped_defs = true
disallow_incomplete_defs = true
# 可选：对于核心模块
# warn_return_any = true
# no_implicit_reexport = true
```

### 6.4 type: ignore 合规检查

运行 type: ignore 策略检查：

```bash
# 检查指定模块
python scripts/ci/check_type_ignore_policy.py --paths src/engram/your_module/

# 要求：
# 1. 所有 type: ignore 必须带 [error-code]
# 2. 所有 type: ignore 必须带原因说明
# 3. 检查退出码 = 0
```

### 6.5 关键测试通过

```bash
# 运行模块相关测试
pytest tests/your_module/ -v

# 要求：100% 通过，无 skip/xfail
```

### 6.6 扩面流程

1. **申请**：在 PR 描述中声明加入 strict-island 意图
2. **检查**：CI 自动验证上述 5 项条件
3. **审批**：需要至少 1 位 Reviewer 确认
4. **生效**：合并后将模块路径添加到 `pyproject.toml` 的 `strict_island_paths`

### 6.7 strict-island 扩面队列

> **SSOT**: 以 `configs/mypy_strict_island_candidates.json` 为准。
>
> **检查脚本**: `python scripts/ci/check_strict_island_admission.py --candidates-file configs/mypy_strict_island_candidates.json`

**扩面节奏规则**：

| 规则 | 说明 |
|------|------|
| **每迭代最多纳入** | 1-3 个候选模块 |
| **优先级排序** | Logbook 高风险文件 > Gateway handlers > 其他模块 |
| **准入前提** | 候选模块 baseline 错误数 = 0 |
| **验收标准** | CI 通过 + `check_strict_island_admission.py` 检查通过 |

**候选队列（计划/目标）**：

> **SSOT**: 以 `configs/mypy_strict_island_candidates.json` 为准。
>
> **查看当前候选**：
> ```bash
> cat configs/mypy_strict_island_candidates.json | jq '.candidates[]'
> ```
>
> **下一阶段扩面目标**（待加入候选队列）：
> - `src/engram/logbook/scm_*.py`（SCM 子系统）
> - `src/engram/gateway/app.py`（Gateway 应用入口）
> - `src/engram/gateway/main.py`（主入口）

| 优先级 | 候选路径 | 目标 | 计划状态 | 备注 |
|--------|----------|------|----------|------|
| P4 | `src/engram/logbook/scm_db.py` | 纳入 Island | 待加入候选 | SCM 数据库层 |
| P4 | `src/engram/logbook/scm_sync_runner.py` | 纳入 Island | 待加入候选 | SCM 同步核心 |
| P5 | `src/engram/gateway/app.py` | 纳入 Island | 待加入候选 | Gateway 应用入口 |
| P5 | `src/engram/gateway/main.py` | 纳入 Island | 待加入候选 | 主入口 |

> **验证已纳入状态**：
> ```bash
> python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['tool']['engram']['mypy']['strict_island_paths']))"
> ```

**候选晋升流程**：

1. **检查准入条件**：运行 `python scripts/ci/check_strict_island_admission.py --candidate <path>`
2. **配置 override**：在 `pyproject.toml` 添加 `[[tool.mypy.overrides]]` 配置
3. **验证 CI**：确保 `make typecheck-strict-island` 通过
4. **更新清单**：将候选从队列移至 `strict_island_paths`

---

### 6.8 当前 Strict Island 清单

> **SSOT**: 以 `pyproject.toml` 的 `[tool.engram.mypy].strict_island_paths` 为准。

**查看当前列表**：

```bash
python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['tool']['engram']['mypy']['strict_island_paths']))"
```

**当前已纳入的模块**：

> **SSOT 提取命令**（权威来源）：
> ```bash
> python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['tool']['engram']['mypy']['strict_island_paths']))"
> ```

```
# Gateway 核心模块（DI 相关）
src/engram/gateway/di.py
src/engram/gateway/container.py
src/engram/gateway/services/
src/engram/gateway/handlers/

# Gateway 策略与审计模块
src/engram/gateway/policy.py
src/engram/gateway/audit_event.py

# Logbook 核心配置模块
src/engram/logbook/config.py
src/engram/logbook/uri.py

# Logbook 数据结构模块（阶段 3 纳入）
src/engram/logbook/cursor.py
src/engram/logbook/governance.py
src/engram/logbook/outbox.py

# Logbook 数据库与视图模块（阶段 4 纳入）
src/engram/logbook/db.py
src/engram/logbook/views.py
src/engram/logbook/artifact_gc.py
```

**分阶段扩面计划（目标）**：

> **SSOT**:
> - 候选队列：`configs/mypy_strict_island_candidates.json`
> - 已纳入列表：`pyproject.toml` 的 `[tool.engram.mypy].strict_island_paths`

| 阶段 | 模块 | 计划目标 | 备注 |
|------|------|----------|------|
| 阶段 1 | `gateway/di.py`, `container.py`, `services/` | 目标：纳入 Island | 初始核心模块 |
| 阶段 2 | `logbook/config.py`, `uri.py` | 目标：纳入 Island | Logbook 配置模块 |
| 阶段 3 | `gateway/handlers/`, `policy.py`, `audit_event.py` | 目标：纳入 Island | Gateway 扩展模块 |
| 阶段 3 | `logbook/cursor.py`, `governance.py`, `outbox.py` | 目标：纳入 Island | Logbook 数据结构 |
| 阶段 4 | `logbook/db.py`, `views.py`, `artifact_gc.py` | 目标：纳入 Island | Logbook 数据库层 |
| 阶段 5 | `logbook/scm_*.py` | 待加入候选队列 | SCM 子系统 |

> **验证实际状态**：以 `pyproject.toml` 中的 `strict_island_paths` 为准。

---

## 7. 指标追踪

### 7.1 定期更新

每周五更新本文档的指标快照：

```bash
# 生成最新指标
python scripts/ci/mypy_metrics.py --stdout --verbose

# 更新 baseline 统计
wc -l scripts/ci/mypy_baseline.txt
```

### 7.2 历史趋势

> **更新规则**: 每次 baseline 变更后更新此表，记录趋势变化。
>
> **获取当前错误数**：`wc -l < scripts/ci/mypy_baseline.txt`

| 日期 | 总错误数 | 净变化 | 备注 |
|------|----------|--------|------|
| 2026-02-01 | 37 | - | 初始快照 |
| 2026-02-01 | 8 | **-29** | gateway/ 清零，logbook/ 大幅减少 |
| 2026-02-02 | 30 | +22 | 新增 logbook 模块错误（见 baseline 文件） |

> **注意**: 历史记录保留供参考，当前状态以 `scripts/ci/mypy_baseline.txt` 为准。

---

## 8. 相关脚本

| 脚本 | 用途 | SSOT 角色 |
|------|------|-----------|
| `scripts/ci/mypy_baseline.txt` | 当前错误基线文件 | **基线 SSOT** |
| `scripts/ci/mypy_metrics.py` | 指标聚合与报告生成 | **指标快照 SSOT** |
| `scripts/ci/check_mypy_gate.py` | mypy 门禁检查主脚本 | - |
| `scripts/ci/check_mypy_metrics_thresholds.py` | 指标阈值检查（CI 集成） | - |
| `scripts/ci/check_mypy_baseline_policy.py` | PR baseline 变更策略检查 | - |
| `scripts/ci/check_type_ignore_policy.py` | type: ignore 注释策略检查 | - |
| `scripts/ci/check_doc_snapshot_freshness.py` | 文档快照时间新鲜度检查（仅 warn） | - |

### 8.1 CI 集成

mypy 指标已集成到 CI 的 `lint` job 中：

1. **指标收集**：每次 CI 运行会自动执行 `mypy_metrics.py` 生成指标报告
2. **阈值检查**：`check_mypy_metrics_thresholds.py` 检查是否超过配置的阈值
3. **Artifact 上传**：指标报告作为 CI artifact 保留 30 天

**GitHub Actions Variables 配置**：

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `ENGRAM_MYPY_TOTAL_ERROR_THRESHOLD` | 50 | 总错误数阈值 |
| `ENGRAM_MYPY_GATEWAY_ERROR_THRESHOLD` | 10 | Gateway 模块错误数阈值 |
| `ENGRAM_MYPY_LOGBOOK_ERROR_THRESHOLD` | 40 | Logbook 模块错误数阈值 |
| `ENGRAM_MYPY_METRICS_FAIL_ON_THRESHOLD` | false | 超阈值时是否 fail CI |

**升级为 fail 模式**：

在 phase >= 1 时，可将 `ENGRAM_MYPY_METRICS_FAIL_ON_THRESHOLD` 设为 `true`，
超阈值将导致 CI 失败。

---

## 9. 附录：生成本文档数据的命令

```bash
# 生成完整指标报告（JSON）
python scripts/ci/mypy_metrics.py --output artifacts/mypy_metrics.json --verbose

# 仅输出到 stdout（用于快速查看）
python scripts/ci/mypy_metrics.py --stdout

# 检查 baseline 策略（模拟 PR 环境）
python scripts/ci/check_mypy_baseline_policy.py --verbose

# 检查 type: ignore 策略
python scripts/ci/check_type_ignore_policy.py --verbose
```
