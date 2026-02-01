# Iteration 8 计划

## 概述

| 字段 | 内容 |
|------|------|
| **迭代编号** | Iteration 8 |
| **开始日期** | 2026-02-01 |
| **状态** | 🔄 PLANNING |
| **SSOT** | 本文档 + [iteration_8_regression.md](iteration_8_regression.md) |

---

## 迭代目标

### 主要目标

1. **收敛 `make ci` 阻塞项**：系统性修复 CI 门禁阻塞问题，包括：
   - lint 错误清零
   - format 错误清零
   - typecheck-gate (mypy baseline) 新增错误清零
   - 一致性检查门禁全部通过

2. **完成一次全量门禁通过并记录证据**：执行完整 `make ci` 并在 [iteration_8_regression.md](iteration_8_regression.md) 记录：
   - 执行日期
   - Commit SHA
   - 每个子门禁的命令和结果
   - 最终通过状态

3. **将高风险改动拆分为小提交**：避免大型单次提交带来的回归风险：
   - 每次提交聚焦单一门禁修复
   - 提交后立即验证相关门禁
   - 出现问题可快速定位和回滚

### 范围边界

| 范围 | 包含 | 不包含 |
|------|------|--------|
| **CI 门禁修复** | lint、format、typecheck-gate、一致性检查、workflows 校验 | 新功能开发、性能优化 |
| **代码质量** | 现有代码的类型注解修复、格式对齐 | 全量 strict 模式迁移 |
| **测试范围** | `make ci` 包含的所有检查 | 需要数据库的集成测试 |

---

## 验收门禁

### 必须通过的门禁

| 门禁 | 命令 | 通过标准 |
|------|------|----------|
| **完整 CI** | `make ci` | 退出码 0，所有子门禁通过 |
| **格式检查** | `make format-check` | 退出码 0，`ruff format --check` 无需修改 |
| **类型检查** | `make typecheck-gate` | baseline 模式下无新增错误 |
| **Gateway DI 边界检查** | `make check-gateway-di-boundaries` | 退出码 0，无 deps.db 直接使用 |
| **Workflows 校验** | `make validate-workflows-strict` | 退出码 0，workflows 合约一致 |
| **SQL 迁移 Sanity** | `make check-migration-sanity` | 退出码 0，迁移计划无异常 |

### 关键子门禁（包含在 `make ci` 中）

| 门禁 | 命令 | 说明 |
|------|------|------|
| **Lint 检查** | `make lint` | ruff check，代码质量 |
| **Strict Island 类型检查** | `make typecheck-strict-island` | 核心模块类型保护 |
| **Schema 校验** | `make check-schemas` | JSON Schema 和 fixtures |
| **环境变量一致性** | `make check-env-consistency` | .env.example / docs / code 对齐 |
| **CLI 入口点一致性** | `make check-cli-entrypoints` | pyproject.toml / docs 对齐 |
| **noqa 策略** | `make check-noqa-policy` | 禁止裸 noqa |
| **根目录 wrapper** | `make check-no-root-wrappers` | 禁止导入根目录 wrapper |
| **废弃导入检查** | `make check-deprecated-logbook-db` | 无废弃 logbook_db 导入 |
| **SQL 清单一致性** | `make check-sql-inventory-consistency` | SQL 迁移清单文档对齐 |
| **迭代文档检查** | `make check-iteration-docs` | .iteration/ 链接检查 |

---

## 证据要求

### 回归记录

每次门禁执行后，**必须**在 [iteration_8_regression.md](iteration_8_regression.md) 记录以下信息：

| 字段 | 格式 | 示例 |
|------|------|------|
| **执行日期** | YYYY-MM-DD HH:MM | `2026-02-01 16:30` |
| **Commit** | 完整 SHA 或短 SHA | `abc1234` |
| **执行命令** | 完整命令 | `make ci` |
| **结果** | PASS / PARTIAL / FAIL | `PASS` |
| **耗时** | 秒数 | `45s` |
| **备注** | 失败原因或特殊说明 | `typecheck-gate 新增 2 个错误` |

### 回归记录格式

```markdown
## YYYY-MM-DD 门禁执行记录

### 执行信息

| 项目 | 值 |
|------|-----|
| **执行日期** | YYYY-MM-DD HH:MM |
| **Commit** | {SHA} |
| **执行者** | {Cursor Agent / 手动} |

### 门禁结果

| 门禁 | 命令 | 结果 | 耗时 | 备注 |
|------|------|------|------|------|
| make ci | `make ci` | {PASS/FAIL} | {N}s | {备注} |
| format-check | `make format-check` | {PASS/FAIL} | {N}s | - |
| typecheck-gate | `make typecheck-gate` | {PASS/FAIL} | {N}s | - |
| check-gateway-di-boundaries | `make check-gateway-di-boundaries` | {PASS/FAIL} | {N}s | - |
| validate-workflows-strict | `make validate-workflows-strict` | {PASS/FAIL} | {N}s | - |
| check-migration-sanity | `make check-migration-sanity` | {PASS/FAIL} | {N}s | - |

### 失败详情（如有）

{失败的具体错误信息}
```

### 产物目录

| 产物 | 路径 | 说明 |
|------|------|------|
| **回归记录** | `docs/acceptance/iteration_8_regression.md` | 本迭代回归记录（SSOT） |
| **CI 日志** | `.artifacts/ci-runs/` | CI 运行产物（可选） |

---

## 任务清单

### 已完成

- [ ] （待执行后更新）

### 进行中

- [ ] 收敛 `make ci` 阻塞项
- [ ] 系统性修复 lint / format / typecheck 错误

### 待开始

- [ ] 完整 `make ci` 执行并记录证据
- [ ] 回归记录文档更新
- [ ] 将修复拆分为小提交

---

## 风险评估

| 风险 | 等级 | 缓解措施 |
|------|------|----------|
| **大型提交引入回归** | **高** | 遵循"小提交"原则：每次提交聚焦单一修复，提交后立即验证 |
| **mypy 新增错误无法清零** | **中** | 按照 [ADR: mypy 基线管理](../architecture/adr_mypy_baseline_and_gating.md) 策略：必须附 issue 链接、说明原因 |
| **DI 边界修复影响运行时** | **中** | 修改后运行 Gateway 测试验证：`pytest tests/gateway/ -q` |
| **workflow 合约变更不兼容** | **低** | 先运行 `make validate-workflows-strict` 确认影响范围 |

---

## 相关文档

| 文档 | 说明 |
|------|------|
| [iteration_8_regression.md](iteration_8_regression.md) | 详细回归记录与门禁执行日志 |
| [AGENTS.md](../../AGENTS.md) | AI Agent 协作快速指南 |
| [docs/dev/agents.md](../dev/agents.md) | 完整 Agent 指南 |
| [ADR: mypy 基线管理](../architecture/adr_mypy_baseline_and_gating.md) | mypy baseline 变更评审规则 |
| [CI 门禁 Runbook](../dev/ci_gate_runbook.md) | CI 门禁操作指南 |

---

更新时间：2026-02-01
