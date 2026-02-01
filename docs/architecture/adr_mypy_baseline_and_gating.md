# ADR: mypy 基线管理与 Gate 门禁策略

> 状态: **已批准**  
> 创建日期: 2026-02-01  
> 决策者: Engram Core Team

---

## 1. 背景

项目历史遗留大量无类型注解代码，直接启用 mypy 严格模式会产生 100+ 错误，阻碍正常开发。为平衡类型安全与开发效率，采用 **基线对比 + 门禁分级** 策略，实现渐进式类型化收敛。

---

## 2. Gate 五档定义与退出码

| Gate 级别 | 环境变量值 | 行为描述 | 退出码 | 使用场景 |
|-----------|------------|----------|--------|----------|
| **baseline** | `ENGRAM_MYPY_GATE=baseline` | 对比基线，仅新增错误时失败 | 0=无新增, 1=有新增 | **当前默认**，日常开发与 CI |
| **strict** | `ENGRAM_MYPY_GATE=strict` | 任何 mypy 错误都失败 | 0=无错误, 1=有错误 | 发布前检查、目标状态 |
| **strict-island** | `ENGRAM_MYPY_GATE=strict-island` | 仅检查 strict island 模块（见 §2.3），错误则失败 | 0=无错误, 1=有错误 | 核心模块保护 |
| **warn** | `ENGRAM_MYPY_GATE=warn` | 运行 mypy 并输出错误，但永远返回 0 | 始终 0 | 仅警告模式，不阻断 CI |
| **off** | `ENGRAM_MYPY_GATE=off` | 跳过检查，不运行 mypy | 始终 0 | 调试、实验性开发 |

> **兼容性说明**：旧环境变量 `MYPY_GATE` 仍然支持，但优先级低于 `ENGRAM_MYPY_GATE`。推荐使用 `ENGRAM_MYPY_GATE`。

### 2.1 退出码详解

```
Gate=baseline:
  退出码 0: 当前错误 ⊆ 基线错误（无新增）
  退出码 1: 当前错误 ⊃ 基线错误（有新增，必须处理）

Gate=strict:
  退出码 0: mypy 检查通过，无任何错误
  退出码 1: mypy 检查失败，存在错误

Gate=strict-island:
  退出码 0: strict island 模块无 mypy 错误
  退出码 1: strict island 模块存在错误

Gate=warn:
  退出码 0: 运行 mypy 并输出警告，但不阻断

Gate=off:
  退出码 0: 跳过检查，不运行 mypy
```

### 2.2 CI 配置示例

CI 采用两步流程：先由 `resolve_mypy_gate.py` 根据迁移阶段解析 gate 值，再由 `check_mypy_gate.py` 执行检查。

```yaml
# .github/workflows/ci.yml

# 步骤 1: 统计 baseline 错误数（用于阈值判断）
- name: Count mypy baseline errors
  id: baseline-count
  run: |
    BASELINE_FILE="scripts/ci/mypy_baseline.txt"
    if [ -f "$BASELINE_FILE" ]; then
      COUNT=$(wc -l < "$BASELINE_FILE" | tr -d ' ')
    else
      COUNT=0
    fi
    echo "count=${COUNT}" >> $GITHUB_OUTPUT

# 步骤 2: 解析 mypy gate（根据 phase、分支、阈值等）
- name: Resolve mypy gate
  id: resolve-mypy-gate
  run: |
    GATE=$(python scripts/ci/resolve_mypy_gate.py \
      --phase "${{ vars.ENGRAM_MYPY_MIGRATION_PHASE || '0' }}" \
      --override "${{ vars.ENGRAM_MYPY_GATE_OVERRIDE || '' }}" \
      --threshold "${{ vars.ENGRAM_MYPY_STRICT_THRESHOLD || '0' }}" \
      --baseline-count "${{ steps.baseline-count.outputs.count }}" \
      --branch "${{ github.head_ref || '' }}" \
      --ref "${{ github.ref }}" \
      --verbose)
    echo "gate=${GATE}" >> $GITHUB_OUTPUT

# 步骤 3: 执行 mypy 检查（使用解析后的 gate）
- name: mypy type check (baseline)
  run: |
    python scripts/ci/check_mypy_gate.py \
      --gate "${{ steps.resolve-mypy-gate.outputs.gate }}" \
      --baseline-file scripts/ci/mypy_baseline.txt \
      --mypy-path src/engram/ \
      --verbose

# 步骤 4: strict-island 检查（核心模块必须零错误）
- name: mypy strict-island check
  run: python scripts/ci/check_mypy_gate.py --gate strict-island --verbose
```

**脚本职责分工**：

| 脚本 | 职责 | 主要参数 |
|------|------|----------|
| `resolve_mypy_gate.py` | 根据 phase/分支/阈值解析 gate 值 | `--phase`, `--branch`, `--threshold`, `--override` |
| `check_mypy_gate.py` | 执行 mypy 检查 | `--gate`, `--baseline-file`, `--mypy-path` |

**本地使用（Makefile 目标）**：

```bash
# baseline 模式（CI 默认）
make typecheck-gate

# strict-island 模式（核心模块必须通过）
make typecheck-strict-island

# strict 模式（发布前检查）
make typecheck-strict

# 更新基线（需 reviewer 批准）
make mypy-baseline-update
```

**直接调用脚本**：

```bash
# baseline 模式
python scripts/ci/check_mypy_gate.py --gate baseline

# strict-island 模式
python scripts/ci/check_mypy_gate.py --gate strict-island --verbose

# 更新基线
python scripts/ci/check_mypy_gate.py --write-baseline
```

### 2.3 Strict Island 定义与约束

> **Strict Island**（严格岛屿）是一组经过类型修复、mypy 错误为 0 的核心模块集合。

#### 配置位置

> **SSOT**: 以 `pyproject.toml` 的 `[tool.engram.mypy].strict_island_paths` 为准。

**查看当前 Strict Island 列表**：

```bash
# 方式 1: 使用 grep 提取
grep -A 20 'strict_island_paths' pyproject.toml | grep '"src/'

# 方式 2: 使用 Python 解析
python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['tool']['engram']['mypy']['strict_island_paths']))"
```

**当前配置示例**（以 SSOT 为准，下方仅为参考格式）：

```toml
# pyproject.toml
[tool.engram.mypy]
strict_island_paths = [
    # Gateway 核心模块（DI 相关）
    "src/engram/gateway/di.py",
    "src/engram/gateway/container.py",
    "src/engram/gateway/services/",
    # Logbook 核心配置模块
    "src/engram/logbook/config.py",
    "src/engram/logbook/uri.py",
]
```

#### 岛屿约束（Island Constraints）

纳入 `strict_island_paths` 的模块必须满足以下约束：

| 约束 | 配置项 | 说明 |
|------|--------|------|
| 严格类型定义 | `disallow_untyped_defs = true` | 禁止未类型化的函数定义 |
| 严格导入检查 | `ignore_missing_imports = false` | 强制要求导入类型信息 |

#### 与 mypy overrides 的关系

```
strict_island_paths ⊆ mypy_strict_overrides

其中:
- strict_island_paths: CI 强阻断岛屿（[tool.engram.mypy].strict_island_paths）
- mypy_strict_overrides: 所有 disallow_untyped_defs=true 的模块
```

**重要区别**：

- `strict_island_paths` **不等于** 所有 `disallow_untyped_defs=true` 的模块
- 某些模块（如 `engram.logbook.cursor`, `engram.logbook.outbox` 等）可能启用了 `disallow_untyped_defs=true`，但未纳入 `strict_island_paths`
- 纳入岛屿的模块有更高要求：必须同时配置 `ignore_missing_imports=false`

#### 一致性验证

CI 自动验证以下一致性约束（见 `tests/test_mypy_gate.py`）：

1. **子集约束**：`strict_island_paths` 中的每个模块必须在 `[[tool.mypy.overrides]]` 中配置 `disallow_untyped_defs=true`
2. **岛屿约束**：`strict_island_paths` 中的每个模块必须在 `[[tool.mypy.overrides]]` 中配置 `ignore_missing_imports=false`

---

## 3. Baseline 文件管理

### 3.1 文件位置

```
scripts/ci/mypy_baseline.txt
```

### 3.2 生成/更新命令

```bash
# 首次生成基线
python scripts/ci/check_mypy_gate.py --write-baseline

# 更新基线（修复错误后）
python scripts/ci/check_mypy_gate.py --write-baseline

# 或使用 make 目标
make mypy-baseline-update

# 详细输出
python scripts/ci/check_mypy_gate.py --verbose
```

### 3.3 基线文件格式

基线文件为纯文本，每行一条规范化错误（移除行号）：

```
src/engram/foo.py: error: Something wrong  [error-code]
src/engram/bar.py: error: Incompatible types  [assignment]
src/engram/baz.py: note: See https://mypy.readthedocs.io/en/stable/running_mypy.html#missing-imports
```

**支持的行类型**：

| 类型 | 格式 | 纳入净增计算 | 说明 |
|------|------|--------------|------|
| `error:` | `file.py: error: message [code]` | ✅ 是 | 类型错误 |
| `warning:` | `file.py: warning: message` | ✅ 是 | 类型警告 |
| `note:` | `file.py: note: message` | ✅ 是 | 补充说明（通常跟随 import 错误） |

> **注意**：`note:` 行通常由 mypy 自动生成，跟随 `import-not-found` 或 `import-untyped` 错误。这些行与 `error:` 行等同对待，纳入 baseline 条目计数和净增计算。

**规范化规则**：
- 移除行号（`file.py:123:` → `file.py:`）
- 按字母排序
- 去重

---

## 4. Baseline 变更评审规则

### 4.1 核心原则与合并准则

> **禁止无理由增长**：基线只允许单调递减或等量重排，不允许无理由增加错误数量。

#### 合并准则（Merge Criteria）

| 准则 | 说明 | 示例 |
|------|------|------|
| **净减少** | ✅ 始终允许合并 | 修复 5 个错误，减少 5 行 |
| **等量变更** | ✅ 允许合并（需说明重构内容） | 文件重命名导致路径变化 |
| **净增加** | ⚠️ **必须附 issue 链接** 方可合并 | 新模块引入 `[import-untyped]`，附 #456 |

**净增加时的强制要求：**

1. PR 描述必须包含关联的 Issue 编号（用于追踪后续修复）
2. Issue 必须标记 `tech-debt` 或 `type-coverage` 标签
3. Issue 中需描述：增加原因、影响范围、计划修复时间

### 4.2 变更决策表

| 场景 | 是否允许 | 必须说明 | 审批要求 |
|------|----------|----------|----------|
| 错误减少（修复后） | ✅ 允许 | 列出修复的错误类型 | 无需特批 |
| 等量变更（重构移位） | ✅ 允许 | 说明重构内容 | 无需特批 |
| 新增 1-5 条错误 | ⚠️ 需审批 | **必须说明原因**，如第三方库类型缺失 | Reviewer 批准 |
| 新增 6-10 条错误 | ⚠️ 严格审批 | **详细说明**，附解决计划 | 2 位 Reviewer 批准 |
| 新增 > 10 条错误 | ❌ 原则禁止 | 需拆分 PR 或提供重大理由 | Tech Lead 批准 |

### 4.3 必须说明的内容

基线增长时，PR 描述必须包含：

```markdown
## Baseline 变更说明

### 变更原因
- [ ] 第三方库类型缺失（指明库名）
- [ ] 类型系统局限（附 issue 链接）
- [ ] 遗留代码暂无法修复（附计划）
- [ ] 其他：___________

### 新增错误明细
| 文件 | 错误类型 | 原因 |
|------|----------|------|
| src/engram/foo.py | [import-untyped] | requests 库无 stubs |
| ... | ... | ... |

### 修复计划
- [ ] 下个迭代修复
- [ ] 待上游修复
- [ ] 长期技术债务
```

### 4.4 Reviewer 检查清单

```markdown
Baseline 变更审核：
- [ ] 变更原因是否合理（非"懒得修"）
- [ ] 是否已尝试 `# type: ignore[code]` 局部抑制
- [ ] 是否影响核心模块（gateway/di.py, container.py 等）
- [ ] 错误数量增幅是否可接受
- [ ] 是否有明确的修复计划
```

---

## 5. 迁移路线：Baseline → Strict

### 5.1 核心指标定义

> **健康状态仪表盘**：以下三个指标用于跟踪 mypy 类型化进度。

| 指标 | 定义 | 计算方式 | 目标值 |
|------|------|----------|--------|
| **Baseline 错误数** | 基线文件中的实际错误数（排除 note 行） | `python scripts/ci/mypy_metrics.py --output - \| jq '.summary.total_errors'` | 0 |
| **Strict Island 覆盖率** | 已启用 `disallow_untyped_defs` 的模块占比 | `已配置 strict 模块数 / 总模块数 × 100%` | 100% |
| **近 30 天新增错误数** | 最近 30 天内基线文件净增加的错误条目 | `git log -p --since="30 days ago" -- scripts/ci/mypy_baseline.txt \| grep "^+" \| grep -v "^+++" \| wc -l` | 0 |

> **口径说明**：
> - **主口径**（CI 使用）：`mypy_metrics.py` 的 `summary.total_errors`，仅计入 `error:` 行
> - **备选口径**（快速估算）：`wc -l`，包含 note 行，数值略高于实际错误数
>
> 详见 [CI 门禁 Runbook §2 变更前检查清单](../dev/ci_gate_runbook.md#2-推荐变更窗口)。

#### 指标阈值与告警

| 指标 | 绿色（健康） | 黄色（警告） | 红色（阻塞） |
|------|-------------|-------------|-------------|
| Baseline 条目数 | ≤ 50 | 51-100 | > 100 |
| Strict Island 覆盖率 | ≥ 80% | 50%-79% | < 50% |
| 近 30 天新增错误 | 0 | 1-5 | > 5 |

**告警机制**：
- 当指标进入红色区域时，CI 应输出 `[WARN]` 提示
- 近 30 天新增错误 > 0 时，PR 必须包含修复计划

### 5.2 迁移前提条件

| 条件 | 当前状态 | 目标 |
|------|----------|------|
| 基线错误数 | 见 `wc -l scripts/ci/mypy_baseline.txt` | ≤ 0 |
| P0 模块类型化 | ✅ | 全部完成 |
| P1 模块类型化 | 📋 进行中 | 全部完成 |
| 第三方库 stubs | 部分缺失 | 全部安装或豁免 |

> **注意**: 基线错误数请运行 `wc -l scripts/ci/mypy_baseline.txt` 获取实时统计，避免文档与实际不一致。

### 5.3 迭代收敛节奏

> **原则**：每个迭代聚焦特定收敛范围，避免全面铺开导致进度失控。

#### 收敛维度优先级

1. **按目录收敛**（推荐首选）
2. **按错误码收敛**（针对高频错误类型）
3. **按高风险模块收敛**（DI、核心业务逻辑）

#### 迭代收敛计划表

| 迭代 | 收敛范围 | 目标错误数 | 收敛维度 | 验收标准 |
|------|----------|------------|----------|----------|
| **v1.0** | `src/engram/gateway/` | < 100 | 目录 | gateway/ 无新增错误 |
| **v1.1** | `[no-any-return]`, `[no-untyped-def]` | < 50 | 错误码 | 这两类错误清零 |
| **v1.2** | `src/engram/logbook/` 核心模块 | < 30 | 目录 | logbook/*.py 无新增错误 |
| **v1.3** | `[import-untyped]` | < 20 | 错误码 | 全部 stubs 安装或豁免 |
| **v1.4** | 高风险模块: `di.py`, `container.py`, `migrate.py` | < 10 | 高风险模块 | 这三个文件 strict 通过 |
| **v2.0** | 全量 strict | 0 | 全量 | Gate=strict 通过 |

#### 高风险模块定义

以下模块因影响面广或复杂度高，需优先类型化：

| 模块 | 风险原因 | 类型化优先级 |
|------|----------|-------------|
| `src/engram/gateway/di.py` | 依赖注入核心，影响所有组件 | P0 |
| `src/engram/gateway/container.py` | 容器配置，影响服务启动 | P0 |
| `src/engram/logbook/migrate.py` | 数据库迁移，影响数据安全 | P0 |
| `src/engram/logbook/scm_sync_runner.py` | 同步核心逻辑 | P1 |
| `src/engram/gateway/handlers/*.py` | 业务处理层 | P1 |

### 5.4 阶段性里程碑

```
当前: Gate=baseline（错误数见 wc -l scripts/ci/mypy_baseline.txt）
    ↓
v1.0: Gate=baseline, < 100 errors
    - gateway/ 模块全类型化
    - 安装 types-requests, boto3-stubs
    ↓
v1.1: Gate=baseline, < 50 errors
    - logbook/ 核心模块类型化
    - 清理所有 [no-any-return]
    ↓
v1.2: Gate=baseline, < 20 errors
    - 处理所有 [import-untyped]
    - 模块级 overrides 配置完善
    ↓
v2.0: Gate=strict（默认切换）
    - 基线错误数 = 0
    - 删除基线文件
    - CI 改用 strict 模式
```

### 5.5 切换决策标准

当满足以下**全部条件**时，默认 Gate 从 baseline 切换到 strict：

1. **基线错误数归零**：`wc -l scripts/ci/mypy_baseline.txt` = 0
2. **连续 2 周无基线变更**：基线文件稳定
3. **全部 P0/P1 模块已启用 `disallow_untyped_defs`**
4. **Tech Lead 审批**：确认切换时机

### 5.6 三阶段切换策略

> **渐进式切换**：避免一次性切换导致大量 PR 阻塞。
>
> **实现方式**：通过 Repository Variables 控制 `ENGRAM_MYPY_MIGRATION_PHASE`，由 `resolve_mypy_gate.py` 解析 gate 值。无需修改 CI 配置文件。

#### 阶段 1：默认分支 Strict，PR 保持 Baseline

**触发条件**：基线错误数（`total_errors`）≤ 20 且无高风险模块错误

> **口径说明**：使用 `mypy_metrics.py` 的 `summary.total_errors`（排除 note 行）。详见 [CI 门禁 Runbook §4.1](../dev/ci_gate_runbook.md#41-phase-0--phase-1)。

**配置变更**（仅需设置 Repository Variable）：

```yaml
# GitHub Settings > Secrets and variables > Actions > Variables
ENGRAM_MYPY_MIGRATION_PHASE: "1"
ENGRAM_MYPY_STRICT_THRESHOLD: "20"  # 可选：当 baseline ≤ 20 时，PR 也使用 strict
```

**CI 自动解析逻辑**（由 `resolve_mypy_gate.py` 处理）：

```python
# resolve_mypy_gate.py 的 phase=1 逻辑
if branch in {"main", "master"}:
    return "strict"
elif baseline_count <= threshold:
    return "strict"  # 阈值提升
else:
    return "baseline"
```

**预期效果**：
- `master` 分支：必须 mypy 零错误才能合并
- PR 分支：仅要求不新增错误（baseline 对比）
- 当 baseline_count ≤ threshold 时，PR 也可提升为 strict
- 开发者有缓冲期修复存量错误

**持续时间**：约 2-4 周，视存量错误修复进度

#### 阶段 2：PR 也改为 Strict

**触发条件**：

| 条件 | 说明 |
|------|------|
| **阶段 1 稳定期** | 稳定运行 ≥ 2 周无回滚 |
| **Baseline 清零** | `total_errors = 0`（使用 `mypy_metrics.py` 口径，排除 note 行）|
| **近 30 天净增** | Baseline 近 30 天净增 = 0 |

> **口径说明**：Baseline 清零以 `mypy_metrics.py` 的 `summary.total_errors = 0` 为准。详见 [CI 门禁 Runbook §4.2](../dev/ci_gate_runbook.md#42-phase-1--phase-2)。

**配置变更**（仅需更新 Repository Variable）：

```yaml
# GitHub Settings > Secrets and variables > Actions > Variables
ENGRAM_MYPY_MIGRATION_PHASE: "2"
```

**CI 自动解析逻辑**：

```python
# resolve_mypy_gate.py 的 phase=2 逻辑
return "strict"  # 所有分支统一 strict
```

**预期效果**：
- 所有新代码必须类型完整
- 基线文件不再更新

**持续时间**：观察 1-2 周确认稳定

#### 阶段 3：全面 Strict + 清理

**触发条件**：阶段 2 稳定运行 ≥ 2 周且 Baseline 仍为空

**操作清单**：

> **重要**：归档 baseline 文件**必须**使用 `python scripts/ci/check_mypy_gate.py --archive-baseline` 命令。
> 详见 [CI 门禁 Runbook §4.3](../dev/ci_gate_runbook.md#43-phase-2--phase-3)。

| 步骤 | 类型 | 操作 | 说明 |
|------|------|------|------|
| 1 | **必须** | 验证基线为空 | 使用 CI 主口径确认 `total_errors = 0` |
| 2 | **必须** | 归档基线文件 | 使用 `--archive-baseline` 命令 |
| 3 | **必须** | 提交归档变更 | Git commit & push |
| 4 | **必须** | 更新 repository variable | 设置 `ENGRAM_MYPY_MIGRATION_PHASE=3` |
| 5 | 可选 | 简化 CI 脚本 | 移除 baseline 对比逻辑 |
| 6 | 可选 | 更新 pyproject.toml | 启用更严格的检查项 |
| 7 | **必须** | 更新文档 | 本 ADR 状态改为"已完成迁移" |

**必须项操作命令**：

```bash
# 1. 验证基线为空（使用 CI 主口径）
python scripts/ci/mypy_metrics.py --output /dev/stdout | jq '.summary.total_errors'
# 必须输出 0

# 2. 归档基线文件（必须使用此命令）
python scripts/ci/check_mypy_gate.py --archive-baseline
# 该命令会自动：
# - 验证 baseline 错误数为 0
# - 创建 scripts/ci/archived/ 目录
# - 移动文件到 scripts/ci/archived/mypy_baseline.txt.archived

# 3. 提交归档变更
git add -A
git commit -m "chore: archive mypy baseline (phase 3)"
git push

# 4. 更新 repository variable
# 在 GitHub Settings 中设置: ENGRAM_MYPY_MIGRATION_PHASE=3

# 7. 更新文档
# 本 ADR 状态改为 "已完成迁移"
```

**可选项详细说明**：

> **评估时机**：以下可选项应在 Phase 3 稳定运行 ≥ 2 周后再评估是否执行。

**步骤 5: 简化 CI 脚本（可选）**

| 改动点 | 文件 | 操作 |
|--------|------|------|
| 移除 baseline policy 检查 | `scripts/ci/check_mypy_baseline_policy.py` | 可删除或标记为弃用 |
| 移除 baseline 相关 artifact 输出 | `scripts/ci/check_mypy_gate.py` | 移除 `write_artifacts()` 中的 `mypy_new_errors.txt` 输出 |
| 简化 CI workflow | `.github/workflows/ci.yml` | 移除 baseline-count 计算步骤 |
| 移除 resolve_mypy_gate.py 的 baseline 分支逻辑 | `scripts/ci/resolve_mypy_gate.py` | Phase 3 后可简化为仅返回 strict |

```bash
# 5.1 移除 baseline 相关 artifact 输出（可选）
# 在 check_mypy_gate.py 中：
# - 可保留 artifacts/mypy_current.txt（当前错误列表）
# - 可移除 artifacts/mypy_new_errors.txt（Phase 3 后无基线对比）

# 5.2 简化 CI workflow（可选）
# 在 ci.yml 中移除以下步骤：
# - "Count mypy baseline errors" 步骤
# - resolve_mypy_gate.py 的 --baseline-count 参数
```

**步骤 6: 更新 pyproject.toml（可选）**

| 改动点 | 配置项 | 操作 |
|--------|--------|------|
| 移除 warn_unused_ignores 禁用 | `warn_unused_ignores = false` | 改为 `true` 或删除（启用默认严格检查）|
| 启用更严格检查 | `disallow_any_generics` 等 | 根据团队需求启用 |

```toml
# pyproject.toml 可选修改示例
[tool.mypy]
# 移除以下行（如有）：
# warn_unused_ignores = false

# 可选启用更严格检查：
# disallow_any_generics = true
# disallow_subclassing_any = true
```

### 5.7 切换阶段追踪表

| 阶段 | 触发条件 | 配置变更 | 状态 |
|------|----------|----------|------|
| 阶段 0（当前） | - | Gate=baseline（所有分支） | **进行中** |
| 阶段 1 | `total_errors` ≤ 20 + 无高风险模块错误 + 观察 2-4 周 | master=strict, PR=baseline | 待触发 |
| 阶段 2 | `total_errors` = 0 + 阶段 1 稳定 ≥ 2 周 + 近 30 天净增 = 0 | 所有分支=strict | 待触发 |
| 阶段 3 | 阶段 2 稳定 ≥ 2 周 | 归档基线文件（使用 `--archive-baseline`）| 待触发 |

> **口径说明**：`total_errors` 使用 `mypy_metrics.py` 的 `summary.total_errors`（排除 note 行）。详见 [CI 门禁 Runbook §4](../dev/ci_gate_runbook.md#4-阶段推进-checklist)。

### 5.8 回滚策略与控制开关

#### 5.8.1 Repository Variables 控制

通过 GitHub Repository Variables 控制 mypy 门禁行为，无需修改代码即可调整：

| 变量名 | 说明 | 有效值 | 默认值 |
|--------|------|--------|--------|
| `ENGRAM_MYPY_MIGRATION_PHASE` | 当前迁移阶段 | `0`, `1`, `2`, `3` | `0` |
| `ENGRAM_MYPY_GATE_OVERRIDE` | 回滚开关，强制使用指定 gate | `baseline`, `strict`, `warn`, `off` | 空（不覆盖） |
| `ENGRAM_MYPY_STRICT_THRESHOLD` | PR 切换到 strict 的阈值 | 非负整数 | `0` |

#### 5.8.2 阈值计算方法

**阈值定义**：当 baseline 错误数 ≤ `ENGRAM_MYPY_STRICT_THRESHOLD` 时，PR 分支也使用 strict 模式。

**计算公式**：

```
baseline_count = wc -l < scripts/ci/mypy_baseline.txt
should_use_strict = (baseline_count <= ENGRAM_MYPY_STRICT_THRESHOLD)
```

**阈值建议值**：

| 阶段 | 建议阈值 | 说明 |
|------|----------|------|
| 阶段 0 | 不适用 | 所有分支使用 baseline |
| 阶段 1 | `20` | PR 在 baseline ≤ 20 时切换到 strict |
| 阶段 2 | `0` | 所有分支使用 strict |

**检查阈值状态**：

```bash
# 查看当前阈值状态
python scripts/ci/check_mypy_gate.py --check-threshold
```

#### 5.8.3 回滚操作

**方式 1：使用回滚开关（推荐，优先级: override > phase）**

无需修改代码，直接设置 Repository Variable。`ENGRAM_MYPY_GATE_OVERRIDE` 的优先级高于 `ENGRAM_MYPY_MIGRATION_PHASE`：

```yaml
# 在 GitHub Settings > Secrets and variables > Actions > Variables 中设置

# 回滚到 baseline 模式（常用）
ENGRAM_MYPY_GATE_OVERRIDE: baseline

# 仅警告模式，不阻断 CI（紧急情况）
ENGRAM_MYPY_GATE_OVERRIDE: warn

# 跳过 mypy 检查（仅用于调试/实验）
ENGRAM_MYPY_GATE_OVERRIDE: off
```

| Override 值 | 行为 | 使用场景 |
|-------------|------|----------|
| `baseline` | 回退到基线对比模式 | 常规回滚 |
| `warn` | 输出警告但不阻断 CI | 紧急发布、误报排查 |
| `off` | 跳过检查 | 调试、实验性开发 |

**方式 2：降低迁移阶段**

```yaml
# 从阶段 2 回滚到阶段 1
ENGRAM_MYPY_MIGRATION_PHASE: 1
```

**方式 3：代码级回滚（最后手段）**

```bash
# 回滚到 baseline 模式
# 修改 .github/workflows/ci.yml
env:
  ENGRAM_MYPY_GATE: baseline

# 如已归档基线文件，从归档恢复
mv scripts/ci/archived/mypy_baseline.txt.archived scripts/ci/mypy_baseline.txt

# 或从 git 历史恢复
git checkout HEAD~1 -- scripts/ci/mypy_baseline.txt
```

**回滚触发条件**：
- 切换后 24 小时内出现 > 5 个被阻塞的紧急 PR
- 发现误报（false positive）影响正常开发

#### 5.8.4 阶段 3 归档操作

当满足阶段 3 条件时，执行 baseline 归档：

```bash
# 1. 验证基线为空
wc -l scripts/ci/mypy_baseline.txt  # 应输出 0

# 2. 执行归档（自动检查并移动文件）
python scripts/ci/check_mypy_gate.py --archive-baseline

# 3. 提交归档变更
git add -A
git commit -m "chore: archive mypy baseline (phase 3)"

# 4. 更新 repository variable
# 在 GitHub Settings 中设置: ENGRAM_MYPY_MIGRATION_PHASE=3
```

**归档后的文件位置**：

```
scripts/ci/archived/mypy_baseline.txt.archived
```

**注意**：归档后仍保留历史记录，如需回滚可从归档目录恢复

---

## 6. 相关文档

| 文档 | 说明 |
|------|------|
| [CI 门禁 Runbook](../dev/ci_gate_runbook.md) | **门禁变量总览、回滚步骤、阶段推进 Checklist、例外审批模板** |
| [ADR: Logbook Strict Island 扩展计划](./adr_logbook_strict_island_expansion_config_uri_db.md) | **Logbook 模块纳入 Strict Island 的详细计划** |
| [mypy 错误码修复 Playbook](../dev/mypy_error_playbook.md) | **错误码清理路线、修复模板、抑制策略** |
| [mypy 基线管理](../dev/mypy_baseline.md) | 操作指南与常见问题 |
| [环境变量参考](../reference/environment_variables.md) | ENGRAM_MYPY_GATE 变量说明 |
| `scripts/ci/check_mypy_gate.py` | mypy 门禁检查脚本（SSOT） |
| `scripts/ci/mypy_baseline.txt` | 当前基线文件 |
| `pyproject.toml` | mypy 配置 |

---

## 7. 决策记录

| 日期 | 决策 | 原因 |
|------|------|------|
| 2026-02-01 | 采用 baseline 模式作为默认 | 平衡类型安全与开发效率 |
| 2026-02-01 | 禁止无理由基线增长 | 防止类型债务无限膨胀 |
| TBD | 切换到 strict 模式 | 待基线归零后执行 |
