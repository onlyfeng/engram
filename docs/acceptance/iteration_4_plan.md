# Iteration 4 计划

## 概述

| 字段 | 内容 |
|------|------|
| **迭代编号** | Iteration 4 |
| **开始日期** | 2026-01-31 |
| **状态** | ⚠️ PARTIAL (进行中) |
| **SSOT** | 本文档 + [iteration_4_regression.md](iteration_4_regression.md) |

---

## 迭代目标

### 主要目标

1. **代码质量修复**：执行 `make format`、`make lint`、`make typecheck` 并修复发现的问题
2. **测试 DI 重构**：统一 Gateway 测试文件中的依赖注入模式
3. **文档一致性**：确保 CI 命令、环境变量文档与实际实现一致

### 范围边界

| 范围 | 包含 | 不包含 |
|------|------|--------|
| **代码质量** | lint 错误修复、格式化、类型注解补充 | 全量 strict mypy（降级到 baseline 模式） |
| **测试** | DI 模式统一、Mock 配置更新 | 新增功能测试 |
| **文档** | CI/环境变量文档同步 | 新架构设计文档 |

---

## 验收门禁

### 必须通过的门禁

| 门禁 | 命令 | 通过标准 |
|------|------|----------|
| **格式检查** | `make format-check` | 退出码 0 |
| **Lint 检查** | `make lint` | 0 errors |
| **类型检查** | `make typecheck-gate` | baseline 模式下无新增错误 |
| **CLI 入口一致性** | `make check-cli-entrypoints` | 退出码 0 |
| **环境变量一致性** | `make check-env-consistency` | 无 ERROR（WARN 可接受） |

### 可选/降级门禁

| 门禁 | 命令 | 说明 |
|------|------|------|
| **Strict mypy** | `make typecheck-gate --gate strict` | 当前 263 errors，降级为 baseline 模式 |
| **两阶段审计 E2E** | `pytest tests/gateway/test_two_phase_audit_e2e.py` | 需要 POSTGRES_DSN，可选执行 |

---

## 证据要求

### 回归记录

每次验收执行后，需在 [iteration_4_regression.md](iteration_4_regression.md) 记录：

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
| **回归记录** | `docs/acceptance/iteration_4_regression.md` | 版本化的回归记录 |
| **本地迭代笔记** | `.iteration/` | 本地化，不纳入版本控制 |

---

## 任务清单

### 已完成

- [x] 格式化修复：172 files reformatted
- [x] Lint 修复：2074 → 0 errors
- [x] 测试 DI 重构：`test_mcp_jsonrpc_contract.py` 等
- [x] 文档索引更新：README.md 添加 iteration-4 回归链接
- [x] 文档一致性检查脚本更新

### 进行中

- [ ] 类型错误修复：当前 263 errors（baseline 模式通过）
- [ ] 审计契约测试修复：13 failed / 214 passed

### 待开始

- [ ] 两阶段审计 E2E 验证（需要数据库环境）
- [ ] Strict Island 扩展覆盖

---

## 风险评估

| 风险 | 等级 | 缓解措施 |
|------|------|----------|
| 类型错误数量大 | **中** | 使用 baseline 模式，逐步修复 |
| 审计测试失败 | **高** | 需审查 Mock 配置与实现一致性 |
| E2E 测试未覆盖 | **高** | 在数据库环境补充执行 |

---

## 相关文档

| 文档 | 说明 |
|------|------|
| [iteration_4_regression.md](iteration_4_regression.md) | 详细回归记录与修复清单 |
| [00_acceptance_matrix.md](00_acceptance_matrix.md) | 验收测试矩阵总览 |
| [adr_mypy_baseline_and_gating.md](../architecture/adr_mypy_baseline_and_gating.md) | mypy 基线策略 ADR |

---

更新时间：2026-02-01
