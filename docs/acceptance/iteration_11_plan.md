# Iteration 11 计划

> **背景说明**：此前在本地/讨论中使用 `iteration_id=7` 追踪，本次晋升为 Iteration 11 以避免与历史 Iteration 7 冲突。
> 历史 Iteration 7 的记录保留于 [iteration_7_regression.md](iteration_7_regression.md)（状态：SUPERSEDED）。

## 概述

| 字段 | 内容 |
|------|------|
| **迭代编号** | Iteration 11 |
| **开始日期** | 2026-02-01 |
| **状态** | 🔄 PLANNING |
| **SSOT** | 本文档 + [iteration_11_regression.md](iteration_11_regression.md) |

---

## 迭代目标

### 主要目标

1. **修复 Gateway 测试失败**：解决 Iteration 10 遗留的 15 个 Gateway 测试失败
2. **Mypy Baseline 收敛**：处理 86 个新增的 mypy 类型错误
3. **test_mcp_jsonrpc_contract.py mock 路径修复**：修复 `get_reliability_report` 函数位置问题
4. **correlation_id 逻辑验证**：确保 `build_audit_event` 正确使用传入的 correlation_id

### 范围边界

| 范围 | 包含 | 不包含 |
|------|------|--------|
| **测试修复** | Gateway 单元测试、Audit Event 测试 | Acceptance 测试（已通过） |
| **类型检查** | mypy baseline 维护 | strict-island 扩展 |
| **CLI 兼容** | 错误消息更新 | 新增 CLI 命令 |

---

## 验收门禁

### 必须通过的门禁

| 门禁 | 命令 | 通过标准 |
|------|------|----------|
| **格式检查** | `make format-check` | 退出码 0 |
| **Lint 检查** | `make lint` | 0 errors |
| **类型检查** | `make typecheck-gate` | baseline 模式下无新增错误 |
| **Gateway 测试** | `pytest tests/gateway/ -q` | 0 失败 |
| **Acceptance 测试** | `pytest tests/acceptance/ -q` | 0 失败 |

### 可选/降级门禁

| 门禁 | 命令 | 说明 |
|------|------|------|
| **Strict Island** | `make typecheck-strict-island` | 暂不强制要求 |

---

## 证据要求

### 回归记录

每次验收执行后，需在 [iteration_11_regression.md](iteration_11_regression.md) 记录：

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
| **回归记录** | `docs/acceptance/iteration_11_regression.md` | 版本化的回归记录 |
| **本地迭代笔记** | `.iteration/` | 本地化，不纳入版本控制 |

---

## 任务清单

### 待开始

- [ ] 修复 `test_correlation_id_proxy.py` 私有函数导入问题（2 失败）
- [ ] 修复 DEPENDENCY_MISSING 常量相关测试（4 失败）
- [ ] 修复 `test_two_phase_audit_adapter_first.py` 两阶段审计行为（2 失败）

### 进行中

*(无)*

### 已完成

- [x] 迭代编号规划（避免与历史 Iteration 7 冲突）
- [x] 创建 Iteration 11 文档
- [x] 修复 `test_logbook_db.py` 错误消息断言 - 模块已废弃，测试迁移
- [x] 处理 mypy baseline 86 个新增错误 ✅ 已清零
- [x] 修复 ruff format 问题（4 文件）
- [x] Gateway 测试失败收敛（21 → 8）

---

## 风险评估

| 风险 | 等级 | 缓解措施 |
|------|------|----------|
| mypy 新增错误过多 | **中** | 可更新 baseline 文件（需 reviewer 批准） |
| mock 路径变更影响范围 | **低** | 仅涉及测试文件，不影响生产代码 |

---

## 相关文档

| 文档 | 说明 |
|------|------|
| [iteration_11_regression.md](iteration_11_regression.md) | 详细回归记录与修复清单 |
| [iteration_10_regression.md](iteration_10_regression.md) | 上一迭代回归记录（基准） |
| [00_acceptance_matrix.md](00_acceptance_matrix.md) | 验收测试矩阵总览 |
| [iteration_7_regression.md](iteration_7_regression.md) | 历史 Iteration 7（已被取代） |
| [adr_mypy_baseline_and_gating.md](../architecture/adr_mypy_baseline_and_gating.md) | Mypy 基线策略 |

---

更新时间：2026-02-01
