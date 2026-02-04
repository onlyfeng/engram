# Iteration 12 计划

## 概述

| 字段 | 内容 |
|------|------|
| **迭代编号** | Iteration 12 |
| **开始日期** | 2026-02-01 |
| **状态** | 🔄 PLANNING |
| **SSOT** | 本文档 + [iteration_12_regression.md](iteration_12_regression.md) |

---

## 迭代目标

### 主要目标

1. **私有函数导入修复**：解决 `test_correlation_id_proxy.py` 中 `_infer_value_error_reason` 和 `_infer_runtime_error_reason` 私有函数不存在的导入问题（2 失败）
2. **ErrorReason 契约收敛**：修复 `DEPENDENCY_MISSING` 常量缺失及 `MISSING_REQUIRED_PARAM` vs `MISSING_REQUIRED_PARAMETER` 命名不一致问题（4 失败）
3. **两阶段审计语义对齐**：修复 `test_two_phase_audit_adapter_first.py` 中 API error 路由策略（action='error' vs 'deferred'）问题（2 失败）

### 范围边界

| 范围 | 包含 | 不包含 |
|------|------|--------|
| **测试修复** | Gateway 单元测试 8 个失败用例 | Acceptance 测试（已全部通过） |
| **契约更新** | ErrorReason 公开常量白名单 | 新增错误码 |
| **审计行为** | 两阶段写入路由策略验证 | Outbox Worker 完整集成测试 |

---

## 验收门禁

### 必须通过的门禁

| 门禁 | 命令 | 通过标准 |
|------|------|----------|
| **CI 门禁** | `make ci` | 退出码 0 |
| **Gateway 测试** | `pytest tests/gateway/ -q` | 0 失败 |
| **Acceptance 测试** | `pytest tests/acceptance/ -q` | 0 失败 |

### 可选/降级门禁

| 门禁 | 命令 | 说明 |
|------|------|------|
| **Strict Island** | `make typecheck-strict-island` | 暂不强制要求 |

---

## 证据要求

### 回归记录

每次验收执行后，需在 [iteration_12_regression.md](iteration_12_regression.md) 记录：

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
| **回归记录** | `docs/acceptance/iteration_12_regression.md` | 版本化的回归记录 |
| **本地迭代笔记** | `.iteration/` | 本地化，不纳入版本控制 |

---

## 任务清单

### 待开始

- [ ] 修复 `test_correlation_id_proxy.py` 私有函数导入问题（2 失败）
  - `test_infer_value_error_reason`
  - `test_infer_runtime_error_reason`
- [ ] 修复 `DEPENDENCY_MISSING` 常量缺失问题（3 失败）
  - `test_error_codes.py::test_dependency_reasons_exist`
  - `test_importerror_optional_deps_contract.py::test_make_dependency_missing_error_field_semantics`
  - `test_importerror_optional_deps_contract.py::test_error_reason_constant_exported`
- [ ] 修复错误码命名不一致问题（1 失败）
  - `test_importerror_optional_deps_contract.py::test_evidence_upload_missing_content_returns_error`
- [ ] 修复两阶段审计行为测试（2 失败）
  - `test_two_phase_audit_adapter_first.py::test_pending_to_redirected_adapter_first_path`
  - `test_two_phase_audit_adapter_first.py::test_redirected_branch_evidence_refs_correlation_id_consistency`

### 进行中

*(无)*

### 已完成

- [x] 创建 Iteration 12 计划文档
- [x] 创建 Iteration 12 回归记录文档
- [x] 更新 00_acceptance_matrix.md 索引

---

## 风险评估

| 风险 | 等级 | 缓解措施 |
|------|------|----------|
| 私有函数已重构，测试需删除 | **低** | 确认函数是否迁移到其他模块，或删除过时测试 |
| ErrorReason 常量变更影响下游 | **中** | 检查公开 API 契约，确保向后兼容 |
| 两阶段审计路由策略变更 | **中** | 与 ADR 文档对齐，确认 503 错误应路由到 outbox 还是直接返回 error |

---

## 相关文档

| 文档 | 说明 |
|------|------|
| [iteration_12_regression.md](iteration_12_regression.md) | 详细回归记录与修复清单 |
| [iteration_11_regression.md](iteration_11_regression.md) | 上一迭代回归记录（基准） |
| [00_acceptance_matrix.md](00_acceptance_matrix.md) | 验收测试矩阵总览 |
| [adr_gateway_audit_atomicity.md](../architecture/adr_gateway_audit_atomicity.md) | 两阶段审计原子性方案 |
| [mcp_jsonrpc_error_v2.md](../contracts/mcp_jsonrpc_error_v2.md) | MCP JSON-RPC 错误码契约 |

---

更新时间：2026-02-01
