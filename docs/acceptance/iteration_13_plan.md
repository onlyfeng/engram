# Iteration 13 计划

## 概述

| 字段 | 内容 |
|------|------|
| **迭代编号** | Iteration 13 |
| **开始日期** | 2026-02-02 |
| **状态** | ✅ PASS (所有门禁通过) |
| **SSOT** | 本文档 + [iteration_13_regression.md](iteration_13_regression.md) |

---

## 迭代目标

### 主要目标

1. **Gateway Public API Surface 验收**：验证 `check-gateway-public-api-surface` 门禁正确检测 Gateway 公开 API 导出一致性
2. **Workflow 合约验证**：确保 `validate-workflows-strict` 和 `check-workflow-contract-docs-sync` 门禁通过
3. **CI 脚本测试覆盖**：`pytest tests/ci` 测试套件验收通过
4. **Gateway 测试稳定性**：`pytest tests/gateway` 按影响域裁剪后测试通过

### 范围边界

| 范围 | 包含 | 不包含 |
|------|------|--------|
| **Workflow 合约** | `validate-workflows-strict`、`check-workflow-contract-docs-sync` | 完整 CI 流水线执行 |
| **Gateway API** | `check-gateway-public-api-surface` 门禁 | Gateway 新功能开发 |
| **测试套件** | `tests/ci/`、`tests/gateway/`（影响域裁剪） | `tests/acceptance/`、`tests/logbook/` |

---

## 验收门禁

### 必须通过的门禁

| 门禁 | 命令 | 通过标准 |
|------|------|----------|
| **Workflow 合约（严格模式）** | `make validate-workflows-strict` | 退出码 0 |
| **Workflow 合约文档同步** | `make check-workflow-contract-docs-sync` | 退出码 0 |
| **Gateway Public API Surface** | `make check-gateway-public-api-surface` | 退出码 0（如存在） 或 `python scripts/ci/check_gateway_public_api_import_surface.py` |
| **CI 脚本测试** | `pytest tests/ci/ -q` | 0 失败 |
| **Gateway 测试（影响域裁剪）** | `pytest tests/gateway/ -q` | 0 失败（或按影响域裁剪后 0 失败） |

### 可选/降级门禁

| 门禁 | 命令 | 说明 |
|------|------|------|
| **完整 CI 门禁** | `make ci` | 完整门禁验证（覆盖上述所有） |
| **mypy 类型检查** | `make typecheck-gate` | baseline 模式下无新增错误 |

---

## 证据要求

### 回归记录

每次验收执行后，需在 [iteration_13_regression.md](iteration_13_regression.md) 记录：

| 字段 | 说明 |
|------|------|
| **执行日期** | YYYY-MM-DD |
| **Commit** | 被验证的 commit SHA |
| **执行命令** | 实际运行的命令 |
| **结果** | PASS / PARTIAL / FAIL |
| **关键输出摘要** | 每条命令的结果摘要（PASS/PARTIAL/FAIL） |

### 产物目录

| 产物 | 路径 | 说明 |
|------|------|------|
| **回归记录** | `docs/acceptance/iteration_13_regression.md` | 版本化的回归记录 |
| **本地迭代笔记** | `.iteration/` | 本地化，不纳入版本控制 |

---

## 任务清单

### 待开始

*(无)*

### 进行中

*(无)*

### 已完成

- [x] 创建 Iteration 13 计划文档
- [x] 创建 Iteration 13 回归记录文档
- [x] 更新 00_acceptance_matrix.md 索引
- [x] 执行 `make validate-workflows-strict` 并记录结果 → ✅ PASS (复测 2026-02-02)
- [x] 执行 `make check-workflow-contract-docs-sync` 并记录结果 → ✅ PASS (复测 2026-02-02)
- [x] 执行 `make check-gateway-public-api-surface` 并记录结果 → ✅ PASS
- [x] 执行 `make check-gateway-public-api-docs-sync` 并记录结果 → ✅ PASS
- [x] 执行 `make check-iteration-docs` 并记录结果 → ✅ PASS
- [x] 执行 `pytest tests/ci/ -q` 并记录结果 → ✅ PASS (608 passed, 复测 2026-02-02)
- [x] 执行 `pytest tests/gateway/ -q` 并记录结果（按影响域裁剪）→ ✅ PASS (1042 passed)
- [x] 修复 Workflow 合约 iteration-audit job 未声明问题 → ✅ 已修复 (2820db8)
- [x] 修复 Workflow 文档 frozen step 未同步问题 → ✅ 已修复 (2820db8)
- [x] 修复 test_consistent_all_and_tier_b 测试失败 → ✅ 已修复 (2820db8)
- [x] 修复 test_nightly_schema_validation_passes 测试失败 → ✅ 已修复 (2820db8)

---

## 风险评估

| 风险 | 等级 | 缓解措施 |
|------|------|----------|
| `check-gateway-public-api-surface` 脚本不存在或 Makefile 目标未定义 | **低** | 直接调用 `python scripts/ci/check_gateway_public_api_import_surface.py` |
| Gateway 测试存在未修复的失败 | **中** | 按影响域裁剪，仅验证本迭代相关测试 |
| Workflow 合约文档与实现不同步 | **低** | 修复文档或实现使其一致 |

---

## 相关文档

| 文档 | 说明 |
|------|------|
| [iteration_13_regression.md](iteration_13_regression.md) | 详细回归记录与修复清单 |
| [iteration_12_regression.md](iteration_12_regression.md) | 上一迭代回归记录（基准） |
| [00_acceptance_matrix.md](00_acceptance_matrix.md) | 验收测试矩阵总览 |
| [gateway_public_api_surface.md](../architecture/gateway_public_api_surface.md) | Gateway 公开 API 契约 |
| [workflow_contract.v1.json](../../scripts/ci/workflow_contract.v1.json) | CI Workflow 合约定义 |

---

更新时间：2026-02-02 (所有最小门禁通过，4 个 pending failures 已修复)
