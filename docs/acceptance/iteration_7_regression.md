> **⚠️ Superseded by Iteration 9**
>
> 本文档已被 [Iteration 9 回归记录](iteration_9_regression.md) 取代。

---

# Iteration 7 Regression - CI 流水线验证记录

> Iteration 7 的验证工作已合并到 Iteration 9 中完成。
> 以下所有 PENDING 项已在 Iteration 9 中得到验证或废弃。

## 执行信息

| 项目 | 值 |
|------|-----|
| **执行日期** | 2026-02-01 |
| **执行环境** | 本机 (darwin 24.6.0) |
| **执行者** | - |
| **CI 运行 ID** | - |
| **状态** | **SUPERSEDED** (已被 Iteration 9 取代) |

## 本次迭代关注点

本迭代聚焦以下四个核心目标：

1. **Ruff 修复**: 清理 Iteration 6 遗留的 124 个 lint 错误（F401/I001/F811/F821/W293）
2. **No-root-wrappers 例外收敛**: 将根目录 wrapper 脚本例外清单归零或最小化
3. **Mypy Gate 文档对齐**: 确保 mypy 门禁配置与文档 `docs/dev/mypy_baseline.md` 一致
4. **Strict-island 清零**: 消除所有 strict-island 类型检查遗留问题

---

## 执行结果汇总

> **注意**: 以下结果已在 [Iteration 9](iteration_9_regression.md) 中重新验证。

| 序号 | 检查项 | 状态 | 失败原因 | 修复链接 |
|------|--------|------|----------|----------|
| 1 | `make ci` (lint) | ✅ PASS | - | [Iteration 9](iteration_9_regression.md#11-ruff-修复记录) |
| 2 | `make ci` (test) | ⚠️ PARTIAL | mypy baseline 新增错误 | [Iteration 9](iteration_9_regression.md#12-mypy-baseline-gate-失败详情) |
| 3 | `pytest tests/gateway/ -v` | ⚠️ PARTIAL | 4 失败 / 813 通过 | [Iteration 9](iteration_9_regression.md#2-gateway-单元测试) |
| 4 | `pytest tests/acceptance/ -v` | ✅ PASS | 143 通过 / 50 跳过 | [Iteration 9](iteration_9_regression.md#3-acceptance-测试) |
| 5 | DI 边界门禁 | ✅ PASS | - | - |
| 6 | SQL 安全检查门禁 | ✅ PASS | - | - |
| 7 | SQL 完整性检查门禁 | ✅ PASS | - | - |
| 8 | Logbook 一致性检查 | ✅ PASS | - | - |
| 9 | SCM Sync 一致性检查 | ✅ PASS | - | - |
| 10 | 环境变量一致性检查 | ✅ PASS | - | - |
| 11 | No-root-wrappers 门禁 | ✅ PASS | - | - |
| 12 | Mypy Gate 门禁 | ❌ FAIL | 77 个新增错误 | [Iteration 9](iteration_9_regression.md#12-mypy-baseline-gate-失败详情) |

---

## 关注点验收矩阵

### 1. Ruff 修复

> **已在 Iteration 9 完成**: 52 个自动修复 + 6 个手动修复

| 错误类型 | Iteration 6 数量 | Iteration 7/9 数量 | 状态 | 修复文件/行 |
|----------|------------------|------------------|------|-------------|
| F401 (unused-import) | 99 | 0 | ✅ PASS | 自动修复 |
| I001 (unsorted-imports) | 17 | 0 | ✅ PASS | 自动修复 |
| F811 (redefinition) | 4 | 0 | ✅ PASS | 自动修复 |
| F821 (undefined-name) | 3 | 0 | ✅ PASS | 手动修复 |
| W293 (blank-line-whitespace) | 1 | 0 | ✅ PASS | 自动修复 |
| **总计** | 124 | **0** | ✅ | [详见 Iteration 9](iteration_9_regression.md#11-ruff-修复记录) |

**修复命令**:
```bash
ruff check --fix src/ tests/
```

### 2. No-root-wrappers 例外收敛

| 检查项 | 状态 | 说明 | 修复链接 |
|--------|------|------|----------|
| 根目录 wrapper 数量 | ✅ PASS | 门禁通过 | - |
| 例外清单文件更新 | ✅ PASS | `scripts/ci/no_root_wrappers_allowlist.json` | - |
| CI 门禁通过 | ✅ PASS | `check_no_root_wrappers_usage.py` | - |

**相关文件**:
- `scripts/ci/check_no_root_wrappers_usage.py`
- `scripts/ci/no_root_wrappers_allowlist.json`
- `schemas/no_root_wrappers_allowlist_v2.schema.json`

### 3. Mypy Gate 文档对齐

| 检查项 | 状态 | 说明 | 修复链接 |
|--------|------|------|----------|
| 基线文件存在 | ✅ PASS | `scripts/ci/mypy_baseline.txt` | - |
| 文档一致性 | ✅ PASS | `docs/dev/mypy_baseline.md` 与实际配置对齐 | - |
| CI 门禁通过 | ❌ FAIL | 77 个新增错误 | [Iteration 9](iteration_9_regression.md#12-mypy-baseline-gate-失败详情) |
| 错误数 ≤ 基线 | ❌ FAIL | 当前 126，新增 77 | [Iteration 9](iteration_9_regression.md#修复建议) |

**相关文件**:
- `scripts/ci/run_mypy_with_baseline.py`
- `scripts/ci/check_mypy_gate.py`
- `scripts/ci/mypy_baseline.txt`
- `docs/dev/mypy_baseline.md`

### 4. Strict-island 清零

| 检查项 | 状态 | 说明 | 修复链接 |
|--------|------|------|----------|
| strict-island 模块数 | ⚠️ 进行中 | 部分覆盖 | - |
| 类型标注覆盖率 | ⚠️ 进行中 | ~20% | - |
| `--strict` 兼容模块数 | ⚠️ 进行中 | 待扩展 | - |

---

## 详细结果

> **详细结果请参见 [Iteration 9 回归记录](iteration_9_regression.md)**

### 1. `make ci` - 静态检查

- **状态**: ⚠️ PARTIAL (在 Iteration 9 中验证)
- **失败阶段**: mypy baseline gate
- **错误统计**: 77 个新增类型错误

### 2. `pytest tests/gateway/ -v` - Gateway 测试

- **状态**: ⚠️ PARTIAL (在 Iteration 9 中验证)
- **结果**: 813 通过 / 4 失败 / 156 跳过
- **耗时**: 11.61s

### 3. `pytest tests/acceptance/ -v` - 验收测试

- **状态**: ✅ PASS (在 Iteration 9 中验证)
- **结果**: 143 通过 / 50 跳过
- **耗时**: 27.32s

### 4. 门禁测试

#### DI 边界门禁
- **命令**: `pytest tests/gateway/test_di_boundaries.py -v`
- **状态**: ✅ PASS

#### SQL 安全检查门禁
- **命令**: `pytest tests/logbook/test_sql_migrations_safety.py -v`
- **状态**: ✅ PASS

#### SQL 完整性检查门禁
- **命令**: `pytest tests/logbook/test_sql_migrations_sanity.py -v`
- **状态**: ✅ PASS

#### No-root-wrappers 门禁
- **命令**: `python scripts/ci/check_no_root_wrappers_usage.py`
- **状态**: ✅ PASS

#### Mypy Gate 门禁
- **命令**: `python scripts/ci/run_mypy_with_baseline.py`
- **状态**: ❌ FAIL (77 个新增错误)

### 5. 一致性检查

#### Logbook 一致性
- **命令**: `python scripts/verify_logbook_consistency.py --verbose`
- **状态**: ✅ PASS

#### SCM Sync 一致性
- **命令**: `python scripts/verify_scm_sync_consistency.py --verbose`
- **状态**: ✅ PASS

#### 环境变量一致性
- **命令**: `python scripts/ci/check_env_var_consistency.py --verbose`
- **状态**: ✅ PASS
- **结果**:
  - .env.example 变量数: 39
  - 文档变量数: 165
  - 代码变量数: 42
  - 错误: 0
  - 警告: 0
- **修复内容**:
  - 在文档中添加 `ENGRAM_MYPY_BASELINE_FILE` 和 `ENGRAM_MYPY_PATH` 变量
  - 标注 `MYPY_GATE` 为已废弃
  - 在 `DOC_ONLY_VARS` 中添加新的 mypy 变量

---

## 相对 Iteration 6 的变化摘要

### 关注点进展

| 关注点 | Iteration 6 状态 | Iteration 7/9 状态 | 变化 |
|--------|------------------|------------------|------|
| Ruff 错误 | 124 errors | **0** | ✅ 已清零 |
| No-root-wrappers 例外 | - | ✅ PASS | 门禁通过 |
| Mypy Gate 对齐 | - | ❌ FAIL | 77 新增错误 |
| Strict-island | - | ⚠️ 进行中 | ~20% 覆盖 |

### 测试结果对比

| 测试套件 | Iteration 6 | Iteration 7/9 | 变化 |
|----------|-------------|-------------|------|
| Gateway 全量 | 786 passed | 813 passed / 4 failed | +27 用例 |
| Acceptance | 124 passed | 143 passed | +19 用例 |
| DI 边界门禁 | 21 passed | ✅ PASS | - |

---

## 下一步行动

### 高优先级（CI 通过必需）

1. **执行 Ruff 自动修复**:
   ```bash
   ruff check --fix src/ tests/
   ```

2. **验证 No-root-wrappers 门禁**:
   ```bash
   python scripts/ci/check_no_root_wrappers_usage.py
   ```

3. **验证 Mypy Gate 门禁**:
   ```bash
   python scripts/ci/run_mypy_with_baseline.py
   ```

### 低优先级（非阻断）

1. 更新文档与配置对齐
2. 清理遗留的 strict-island 问题

---

## 验证命令

```bash
# 完整 CI 检查
make ci

# 门禁测试
pytest tests/gateway/test_di_boundaries.py -v
pytest tests/logbook/test_sql_migrations_safety.py -v
pytest tests/logbook/test_sql_migrations_sanity.py -v

# 新增门禁
python scripts/ci/check_no_root_wrappers_usage.py
python scripts/ci/run_mypy_with_baseline.py

# 全量测试
pytest tests/gateway/ -q
pytest tests/acceptance/ -q

# 一致性检查
python scripts/verify_logbook_consistency.py --verbose
python scripts/verify_scm_sync_consistency.py --verbose
python scripts/ci/check_env_var_consistency.py --verbose
```

---

## 相关文档
- [CI 流水线配置](../../.github/workflows/ci.yml)
- [Iteration 6 回归记录](iteration_6_regression.md)
- [Mypy 基线文档](../dev/mypy_baseline.md)
- [Gateway 设计](../gateway/06_gateway_design.md)
- [CLI 入口文档](../architecture/cli_entrypoints.md)
- [DI 边界 ADR](../architecture/adr_gateway_di_and_entry_boundary.md)
