# Iteration 15 计划

## 概述

| 字段 | 内容 |
|------|------|
| **迭代编号** | Iteration 15 |
| **开始日期** | 2026-02-03 |
| **状态** | ✅ PASS |
| **SSOT** | 本文档 + [iteration_15_regression.md](iteration_15_regression.md) |
| **本地目录** | `.iteration/15/` |

---

## 迭代目标

### 主要目标

1. **迭代门禁与证据链对齐**：梳理并更新迭代相关脚本/配置的执行路径，确保门禁与证据输出一致。
2. **MCP/配置一致性校验收敛**：验证并补齐 MCP 配置文档与检查脚本的同步点，降低配置漂移。
3. **回归记录可复用性提升**：规范回归记录模板与生成区块的使用，减少手工修改。

### 范围边界

| 范围 | 包含 | 不包含 |
|------|------|--------|
| **迭代脚本** | `scripts/iteration/` 中的计划/证据/回归相关逻辑 | 大规模重构或新 CLI 入口 |
| **CI/门禁** | 与迭代文档/证据相关的检查项 | 新增门禁目标或调整 CI 流程 |
| **文档同步** | MCP 配置与验收文档一致性修正 | 新增文档体系或迁移旧文档 |

---

## 验收门禁

### 必须通过的门禁

| 门禁 | 命令 | 通过标准 |
|------|------|----------|
| **格式检查** | `make format-check` | 退出码 0 |
| **Lint 检查** | `make lint` | 0 errors |
| **类型检查** | `make typecheck` | 退出码 0 |
| **Schema 校验** | `make check-schemas` | 退出码 0 |
| **迭代文档检查** | `make check-iteration-docs` | 退出码 0 |
| **迭代证据检查** | `make check-iteration-evidence` | 退出码 0 |
| **完整 CI 门禁** | `make ci` | 退出码 0 |

### 可选/降级门禁

| 门禁 | 命令 | 说明 |
|------|------|------|
| **CI 脚本测试** | `pytest tests/ci/ -q` | 验证 CI 检查脚本 |
| **迭代脚本测试** | `pytest tests/iteration/ -q` | 验证迭代工具脚本 |
| **类型检查基线** | `make typecheck-gate` | baseline 无新增错误 |

---

## 证据要求

### 回归记录

每次验收执行后，需在 [iteration_15_regression.md](iteration_15_regression.md) 记录：

| 字段 | 说明 |
|------|------|
| **执行日期** | YYYY-MM-DD |
| **Commit** | 被验证的 commit SHA |
| **执行命令** | 实际运行的命令 |
| **结果** | PASS / PARTIAL / FAIL |
| **关键输出摘要** | 每条命令的关键输出与结论 |
| **修复文件清单** | 本次修复涉及的文件 |

### 产物目录

| 产物 | 路径 | 说明 |
|------|------|------|
| **回归记录** | `docs/acceptance/iteration_15_regression.md` | 版本化回归记录 |
| **验收证据** | `docs/acceptance/evidence/iteration_15_evidence.json` | 结构化证据 |
| **本地迭代笔记** | `.iteration/15/` | 本地草稿，不纳入版本控制 |

---

## 任务清单

### 已完成

- [x] 生成 Iteration 15 草稿结构与 README 刷新
- [x] 填写本轮目标、范围边界与门禁要求
- [x] 梳理迭代相关脚本与门禁的依赖关系
- [x] 明确 MCP 配置与文档同步点
- [x] 执行必须门禁并记录回归结果
- [x] 生成并落盘验收证据

### 进行中

- 无

### 待开始

- 无

---

## 风险评估

| 风险 | 等级 | 缓解措施 |
|------|------|----------|
| 门禁范围变更导致回归成本上升 | 中 | 先在本地列出受影响门禁，再分批执行 |
| MCP 配置/文档存在历史漂移 | 中 | 以 `configs/mcp/.mcp.json.example` 为 SSOT 校正 |

---

## 相关文档

| 文档 | 说明 |
|------|------|
| [iteration_15_regression.md](iteration_15_regression.md) | 详细回归记录与修复清单 |
| [00_acceptance_matrix.md](00_acceptance_matrix.md) | 验收矩阵索引 |
| [docs/dev/iteration_runbook.md](../dev/iteration_runbook.md) | 迭代执行指引 |

---

更新时间：2026-02-03
