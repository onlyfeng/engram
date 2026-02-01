# mypy 基线管理与渐进式类型化

> 状态: **生效中**  
> 创建日期: 2026-02-01  
> 决策者: Engram Core Team

---

## 1. 概述

本项目采用 **基线对比模式** 管理 mypy 类型检查，实现渐进式类型化收敛：

- **CI 检查**：仅阻止**新增**错误，不阻止现有错误
- **基线文件**：记录当前已知的 mypy 错误
- **渐进收敛**：逐步修复基线中的错误，分模块提高类型覆盖率

> **当前状态（2026-02-01）**：
> - ✅ mypy 错误数：**0**（baseline 文件已清空）
> - ✅ strict-island 模式：**通过**（11 个核心模块零错误）
> - 🎯 **可进入 Phase 推进准备**：当前已满足 Phase 2 → Phase 3 归档条件
>
> 详见 [§6. 迁移路线](#6-迁移路线) 和 [CI 门禁 Runbook §4.3](./ci_gate_runbook.md#43-phase-2--phase-3)

> **SSOT（Single Source of Truth）声明**:
> - **基线文件**: `scripts/ci/mypy_baseline.txt` — 所有基线数据以此为准
> - **指标快照**: `scripts/ci/mypy_metrics.py` 输出 — 统计分析以此为准
> - **债务清理计划**: [mypy 类型债务清理计划](./mypy_type_debt_plan.md) — 详细快照与进度追踪

> **详细设计决策**: 参见 [ADR: mypy 基线管理与 Gate 门禁策略](../architecture/adr_mypy_baseline_and_gating.md)

---

## 2. Gate 门禁五档

| Gate 级别 | 环境变量 | 退出码 | 说明 |
|-----------|----------|--------|------|
| **baseline** | `ENGRAM_MYPY_GATE=baseline` | 0=无新增, 1=有新增 | **当前默认**，对比基线 |
| **strict** | `ENGRAM_MYPY_GATE=strict` | 0=无错误, 1=有错误 | 发布前检查 |
| **strict-island** | `ENGRAM_MYPY_GATE=strict-island` | 0=无错误, 1=有错误 | 核心模块保护 |
| **warn** | `ENGRAM_MYPY_GATE=warn` | 始终 0 | 仅警告，不阻断 |
| **off** | `ENGRAM_MYPY_GATE=off` | 始终 0 | 跳过检查 |

> **兼容性说明**：旧环境变量 `MYPY_GATE` 仍然支持，但优先级低于 `ENGRAM_MYPY_GATE`。推荐使用 `ENGRAM_MYPY_GATE`。

---

## 3. 工具使用

### 3.1 CI 检查（默认）

CI 流水线自动运行，对比当前 mypy 输出与基线：

```bash
# 使用 make 目标（推荐）
make typecheck-gate

# 或直接调用脚本
python scripts/ci/check_mypy_gate.py --gate baseline

# strict-island 模式（核心模块必须通过）
make typecheck-strict-island
```

**结果判定**：
- ✅ **通过**：无新增错误（现有错误不影响）
- ❌ **失败**：存在新增错误（必须修复或更新基线）

### 3.2 更新基线

当需要更新基线时（见 §4 何时允许更新）：

```bash
# 使用 make 目标（推荐）
make mypy-baseline-update

# 或直接调用脚本
python scripts/ci/check_mypy_gate.py --write-baseline
```

### 3.3 详细输出

显示 mypy 原始输出：

```bash
python scripts/ci/check_mypy_gate.py --verbose
```

### 3.5 统计基线错误

```bash
# 总错误数
wc -l scripts/ci/mypy_baseline.txt

# 按模块统计
grep -o 'src/engram/[^/]*/' scripts/ci/mypy_baseline.txt | sort | uniq -c | sort -rn
```

---

## 4. 基线更新流程

### 4.1 何时允许更新基线

| 场景 | 是否允许 | 说明 |
|------|----------|------|
| 修复类型错误后错误减少 | ✅ 允许 | 鼓励更新以记录进展 |
| 代码重构导致错误位置变化 | ✅ 允许 | 规范化后自动匹配 |
| 新代码引入新错误 | ⚠️ 需审批 | 优先修复，必要时更新 |
| 批量添加 `# type: ignore` | ⚠️ 需审批 | 需说明原因 |
| 降低 mypy 严格度配置 | ❌ 禁止 | 违反渐进收敛原则 |

### 4.2 评审规则：禁止无理由增长

> **核心原则**：基线只允许单调递减或等量重排，**禁止无理由增加错误数量**。

| 新增错误数 | 审批要求 | 必须提供 |
|------------|----------|----------|
| 0（减少或持平） | 无需特批 | 简要说明 |
| 1-5 条 | Reviewer 批准 | **必须说明原因** |
| 6-10 条 | 2 位 Reviewer 批准 | 详细说明 + 修复计划 |
| > 10 条 | Tech Lead 批准 | 需拆分 PR 或重大理由 |

**必须说明的原因类型**：
- 第三方库类型缺失（指明库名）
- 类型系统局限（附 issue 链接）
- 遗留代码暂无法修复（附修复计划）

### 4.3 更新步骤

1. **本地验证**：确认新增错误无法修复
   ```bash
   python scripts/ci/check_mypy_gate.py --verbose
   ```

2. **更新基线**：
   ```bash
   # 使用 make 目标（推荐）
   make mypy-baseline-update
   
   # 或直接调用脚本
   python scripts/ci/check_mypy_gate.py --write-baseline
   ```

3. **提交变更**：
   ```bash
   git add scripts/ci/mypy_baseline.txt
   git commit -m "chore: update mypy baseline

   变更原因: [必填，如：修复了 gateway 模块类型错误]
   新增错误: [如有，说明原因]
   移除错误: [列出修复的错误类型]
   "
   ```

4. **PR 审核**：基线更新需要 reviewer 明确批准

### 4.4 PR 审核检查清单

Reviewer 在批准基线更新时应检查：

- [ ] 变更原因是否合理（非"懒得修"）
- [ ] 是否已尝试 `# type: ignore[code]` 局部抑制
- [ ] 错误数量增幅是否可接受
- [ ] 是否影响核心模块（gateway/di.py, container.py 等）
- [ ] 是否有明确的修复计划（针对增长情况）

---

## 5. 分模块收敛策略

### 5.1 模块优先级

> **SSOT**: 以 `pyproject.toml` 的 `[tool.engram.mypy].strict_island_paths` 为准。

**查看当前已纳入 Strict Island 的模块**：

```bash
python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['tool']['engram']['mypy']['strict_island_paths']))"
```

**分阶段扩面计划**：

| 阶段 | 模块 | 目标 | 状态 | 验收命令 |
|------|------|------|------|----------|
| P0 | `gateway/di.py` | `disallow_untyped_defs = true` | ✅ 已纳入 | `mypy src/engram/gateway/di.py` |
| P0 | `gateway/container.py` | `disallow_untyped_defs = true` | ✅ 已纳入 | `mypy src/engram/gateway/container.py` |
| P0 | `gateway/services/` | `disallow_untyped_defs = true` | ✅ 已纳入 | `mypy src/engram/gateway/services/` |
| P0 | `logbook/config.py` | `disallow_untyped_defs = true` | ✅ 已纳入 | `mypy src/engram/logbook/config.py` |
| P0 | `logbook/uri.py` | `disallow_untyped_defs = true` | ✅ 已纳入 | `mypy src/engram/logbook/uri.py` |
| P1 | `gateway/handlers/` | 函数签名完整类型注解 | ✅ 已纳入 | `mypy src/engram/gateway/handlers/` |
| P1 | `gateway/policy.py` | 策略模块类型化 | ✅ 已纳入 | `mypy src/engram/gateway/policy.py` |
| P1 | `gateway/audit_event.py` | 审计事件模块类型化 | ✅ 已纳入 | `mypy src/engram/gateway/audit_event.py` |
| P2 | `logbook/cursor.py` | 游标管理类型化 | ✅ 已纳入 | `mypy src/engram/logbook/cursor.py` |
| P2 | `logbook/governance.py` | 治理逻辑类型化 | ✅ 已纳入 | `mypy src/engram/logbook/governance.py` |
| P2 | `logbook/outbox.py` | Outbox 模式类型化 | ✅ 已纳入 | `mypy src/engram/logbook/outbox.py` |
| P3 | `logbook/db.py` | 核心数据库操作类型化 | ✅ 已纳入 | `mypy src/engram/logbook/db.py` |
| P3 | `logbook/views.py` | 视图层类型化 | ✅ 已纳入 | `mypy src/engram/logbook/views.py` |
| P3 | `logbook/artifact_gc.py` | 制品垃圾回收类型化 | ✅ 已纳入 | `mypy src/engram/logbook/artifact_gc.py` |
| P4 | `logbook/scm_*.py` | SCM 子系统类型化 | 📋 待规划 | - |
| P5 | 其他模块 | 全面类型覆盖 | 📋 待规划 | - |

**准入条件**（模块加入 Strict Island 前必须满足）：
1. 模块在 baseline 中错误数 = 0
2. 已配置 `[[tool.mypy.overrides]]` 并启用 `disallow_untyped_defs = true`
3. `check_type_ignore_policy.py` 检查通过

### 5.2 模块级覆盖配置

在 `pyproject.toml` 中为高优先级模块启用更严格检查：

```toml
[[tool.mypy.overrides]]
module = "engram.gateway.di"
disallow_untyped_defs = true
disallow_incomplete_defs = true
```

### 5.3 模块准入流程（Baseline → Strict Island）

当需要把模块从 baseline 清零并纳入 Strict Island 时，按以下步骤操作：

#### 5.3.1 准入条件

模块加入 Strict Island 前必须满足以下条件：

| 条件 | 检查命令 | 说明 |
|------|----------|------|
| **baseline 错误数 = 0** | `grep "模块路径" scripts/ci/mypy_baseline.txt \| wc -l` | 该模块在 baseline 中无错误 |
| **已配置 overrides** | 检查 `pyproject.toml` | 存在对应的 `[[tool.mypy.overrides]]` |
| **disallow_untyped_defs = true** | 检查 overrides 配置 | 启用严格的函数定义检查 |
| **ignore_missing_imports = false** | 检查 overrides 配置 | 禁止忽略缺失导入 |

#### 5.3.2 准入步骤

**步骤 1：添加候选到清单**

编辑 `configs/mypy_strict_island_candidates.json`，添加候选模块路径：

```json
{
  "candidates": [
    "src/engram/gateway/foo.py",
    "src/engram/logbook/bar/"
  ]
}
```

**步骤 2：修复 mypy 错误**

查看候选模块在 baseline 中的错误：

```bash
# 单文件
grep "src/engram/gateway/foo.py" scripts/ci/mypy_baseline.txt

# 目录下所有文件
grep "src/engram/logbook/bar/" scripts/ci/mypy_baseline.txt
```

逐个修复错误，参考 [mypy 错误码修复 Playbook](./mypy_error_playbook.md)。

**步骤 3：添加 pyproject.toml overrides**

在 `pyproject.toml` 中添加 override 配置：

```toml
[[tool.mypy.overrides]]
module = "engram.gateway.foo"  # 或 "engram.logbook.bar.*" 用于目录
disallow_untyped_defs = true
disallow_incomplete_defs = true
ignore_missing_imports = false
warn_return_any = true
```

**步骤 4：运行准入检查**

```bash
# 使用默认候选清单
make check-strict-island-admission

# 或检查单个候选
make check-strict-island-admission CANDIDATE=src/engram/gateway/foo.py

# 或指定自定义清单
make check-strict-island-admission CANDIDATES_FILE=my_candidates.json
```

检查通过的输出示例：

```
[PASS] src/engram/gateway/foo.py
       baseline 错误数: 0
       存在 override: True
       disallow_untyped_defs: True
       ignore_missing_imports: False

[OK] 所有候选路径满足 Strict Island 准入条件
```

**步骤 5：添加到 strict_island_paths**

准入检查通过后，将模块添加到 `pyproject.toml` 的 `[tool.engram.mypy].strict_island_paths`：

```toml
[tool.engram.mypy]
strict_island_paths = [
    # ... 现有路径 ...
    "src/engram/gateway/foo.py",
    "src/engram/logbook/bar/",
]
```

**步骤 6：更新 baseline**

```bash
make mypy-baseline-update
```

**步骤 7：从候选清单移除**

准入完成后，从 `configs/mypy_strict_island_candidates.json` 中移除已纳入的路径。

**步骤 8：提交变更**

```bash
git add pyproject.toml scripts/ci/mypy_baseline.txt configs/mypy_strict_island_candidates.json
git commit -m "feat(types): 将 gateway/foo 纳入 strict-island

准入条件:
- baseline 错误数: 0
- disallow_untyped_defs: true
- ignore_missing_imports: false
"
```

#### 5.3.3 准入检查 CLI 参数

| 参数 | 说明 | 示例 |
|------|------|------|
| `--candidate PATH` | 检查单个候选路径 | `--candidate src/engram/gateway/foo.py` |
| `--candidates-file FILE` | 从 JSON 文件读取候选列表 | `--candidates-file configs/mypy_strict_island_candidates.json` |
| `--baseline-file FILE` | 指定 baseline 文件（默认: `scripts/ci/mypy_baseline.txt`） | `--baseline-file my_baseline.txt` |
| `--pyproject FILE` | 指定 pyproject.toml（默认: `pyproject.toml`） | `--pyproject pyproject.toml` |
| `--json` | JSON 格式输出 | `--json` |
| `--verbose` | 详细输出 | `--verbose` |

#### 5.3.4 常见问题

**Q: 准入检查失败，显示 "baseline 中存在 N 个错误"**

A: 需要先修复该模块的所有 mypy 错误。运行 `grep "模块路径" scripts/ci/mypy_baseline.txt` 查看具体错误。

**Q: 准入检查失败，显示 "缺少对应的 [[tool.mypy.overrides]] 配置"**

A: 在 `pyproject.toml` 中添加 override 配置，确保 module 名称与路径匹配。

**Q: 准入检查失败，显示 "disallow_untyped_defs 应为 true"**

A: 修改 override 配置，添加 `disallow_untyped_defs = true`。

**Q: 准入检查失败，显示 "ignore_missing_imports 应为 false"**

A: 修改 override 配置，添加 `ignore_missing_imports = false`。如果依赖的第三方库缺少类型信息，需要安装对应的 stubs 包或在项目级别配置 `ignore_missing_imports`。

### 5.4 收敛度量

定期检查基线错误趋势：

```bash
# 统计当前基线错误数
wc -l scripts/ci/mypy_baseline.txt

# 按模块统计
grep -o 'src/engram/[^/]*/' scripts/ci/mypy_baseline.txt | sort | uniq -c | sort -rn
```

---

## 6. 迁移路线

> **详细里程碑**: 参见 [ADR §5 迁移路线](../architecture/adr_mypy_baseline_and_gating.md#5-迁移路线baseline--strict)

### 6.1 阶段目标

| 版本 | Gate 模式 | 目标错误数 | 关键任务 |
|------|-----------|------------|----------|
| 当前 | baseline | 见 `wc -l scripts/ci/mypy_baseline.txt` | 基线模式运行中 |
| v1.0 | baseline | < 100 | gateway/ 全类型化 |
| v1.1 | baseline | < 50 | logbook/ 核心类型化 |
| v2.0 | **strict** | 0 | 默认切换到 strict |

> **注意**: 当前基线错误数请运行 `wc -l scripts/ci/mypy_baseline.txt` 获取实时统计。

### 6.2 切换到 strict 的条件

1. 基线错误数 = 0
2. 连续 2 周无基线变更
3. P0/P1 模块全部启用 `disallow_untyped_defs`
4. Tech Lead 审批

---

## 6A. 阈值与回滚控制

### 6A.1 Repository Variables

通过 GitHub Repository Variables 控制 mypy 门禁行为：

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `ENGRAM_MYPY_MIGRATION_PHASE` | 迁移阶段 (0/1/2/3) | `0` |
| `ENGRAM_MYPY_GATE_OVERRIDE` | 回滚开关 | 空 |
| `ENGRAM_MYPY_STRICT_THRESHOLD` | strict 阈值 | `0` |

### 6A.2 阈值计算方法

当 baseline 错误数 ≤ 阈值时，PR 分支也使用 strict 模式。

**统计口径**：阈值判断使用 `mypy_metrics.py` 的 `summary.total_errors` 字段（排除 note 行），与 CI 保持一致。

```bash
# 检查阈值状态
python scripts/ci/check_mypy_gate.py --check-threshold

# 输出示例：
# --- 口径说明 ---
# 统计口径:       mypy_metrics
# 实际错误数:     15  (total_errors, 排除 note 行)
# 文件总行数:     18  (wc -l, 含 note 行, 仅供参考)
#
# --- 阈值判断 ---
# strict 阈值:    20
# 判断依据:       baseline_count (15) <= threshold (20)
#
# [OK] 可以切换到 strict 模式
```

> **note 行说明**：`note:` 行是 mypy 输出的补充说明（如 import 错误的文档链接），不计入 `total_errors`。修复对应 `error:` 时，相关 `note:` 行会一并消失。

### 6A.3 紧急回滚

如需紧急回滚，设置 Repository Variable：

```yaml
ENGRAM_MYPY_GATE_OVERRIDE: baseline
```

无需修改代码，立即生效。

### 6A.4 归档操作（阶段 3）

当基线错误归零后，执行归档：

```bash
# 归档基线文件
python scripts/ci/check_mypy_gate.py --archive-baseline

# 然后提交变更并更新 ENGRAM_MYPY_MIGRATION_PHASE=3
```

---

## 7. 脚本弃用说明

### 7.1 run_mypy_with_baseline.py 已弃用

> **⚠️ 弃用通知**：`scripts/ci/run_mypy_with_baseline.py` 已弃用，请使用 `scripts/ci/check_mypy_gate.py` 替代。

**迁移指南**：

| 旧命令 | 新命令 |
|--------|--------|
| `python scripts/ci/run_mypy_with_baseline.py` | `python scripts/ci/check_mypy_gate.py --gate baseline` 或 `make typecheck-gate` |
| `python scripts/ci/run_mypy_with_baseline.py --update-baseline` | `python scripts/ci/check_mypy_gate.py --write-baseline` 或 `make mypy-baseline-update` |
| `python scripts/ci/run_mypy_with_baseline.py --diff-only` | `python scripts/ci/check_mypy_gate.py --verbose` |
| `python scripts/ci/run_mypy_with_baseline.py --verbose` | `python scripts/ci/check_mypy_gate.py --verbose` |

**向后兼容性**：旧脚本仍可运行，会自动转发到新脚本并输出弃用警告。建议尽快迁移到新命令。

---

## 8. 常见问题

### 8.1 Q: 新代码触发 CI 失败怎么办？

**A**: 优先修复类型错误。如果是误报或无法修复：

1. 添加 `# type: ignore[error-code]` 注释并说明原因
2. 如果是第三方库问题，在 `pyproject.toml` 中配置 `ignore_missing_imports`
3. 最后手段：更新基线（需 reviewer 批准）

### 8.2 Q: 为什么移除行号？

**A**: 基线对比时移除行号（规范化），使得代码移动（如重构、插入行）后仍能正确匹配错误，减少无意义的基线变更。

### 8.2a Q: baseline 文件中的 `note:` 行是什么？

**A**: `note:` 行是 mypy 输出的补充说明，通常跟随 `import-not-found` 或 `import-untyped` 错误。例如：

```
src/engram/foo.py: error: Cannot find implementation or library stub for module named "xxx"  [import-not-found]
src/engram/foo.py: note: See https://mypy.readthedocs.io/en/stable/running_mypy.html#missing-imports
```

**治理策略**：
- `note:` 行与 `error:` 行等同对待，纳入 baseline 条目计数
- 在计算净增时，`note:` 行同样被计入
- 当修复对应的 `error:` 时，相关的 `note:` 行也会一并消失

### 8.3 Q: 如何添加新模块的严格检查？

**A**: 在 `pyproject.toml` 中添加 `[[tool.mypy.overrides]]`：

```toml
[[tool.mypy.overrides]]
module = "engram.your_module"
disallow_untyped_defs = true
```

然后修复该模块的所有类型错误。

### 8.4 Q: 基线文件冲突怎么解决？

**A**: 基线文件按字母排序，合并冲突时：

1. 接受两边的变更
2. 重新运行 `python scripts/ci/check_mypy_gate.py --write-baseline` 生成最新基线
3. 提交合并后的基线

---

## 9. 配置参考

### 9.1 当前 mypy 配置

参见 `pyproject.toml` 的 `[tool.mypy]` 部分。

### 9.2 阶段性目标

| 版本 | 目标 | 预期错误数 |
|------|------|------------|
| 当前 | 基线对比模式运行中 | 见 `wc -l scripts/ci/mypy_baseline.txt` |
| v1.0 | gateway/ 模块全类型化 | < 100 |
| v1.1 | logbook/ 核心模块类型化 | < 50 |
| v2.0 | 全面类型覆盖 | 0 |

---

## 10. 相关文档

| 文档 | 说明 |
|------|------|
| [ADR: mypy 基线管理与 Gate 门禁策略](../architecture/adr_mypy_baseline_and_gating.md) | 设计决策与迁移路线 |
| [ADR: Logbook Strict Island 扩展计划](../architecture/adr_logbook_strict_island_expansion_config_uri_db.md) | **Logbook 模块纳入计划、临时 ignore 策略、清零顺序** |
| [mypy 错误码修复 Playbook](./mypy_error_playbook.md) | 错误码清理路线、修复模板 |
| [环境变量参考](../reference/environment_variables.md) | ENGRAM_MYPY_GATE 变量说明 |
| `scripts/ci/check_mypy_gate.py` | mypy 门禁检查脚本（SSOT） |
| `scripts/ci/mypy_baseline.txt` | 当前基线文件 |
| `pyproject.toml` | mypy 配置 |
| `.github/workflows/ci.yml` | CI 集成配置 |
