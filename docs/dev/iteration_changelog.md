# 迭代/变更日志

> 本文档记录 Engram 各迭代周期的变更内容、影响域、关键文件和验收门禁。
>
> **单一来源**：各迭代详细记录位于 `docs/acceptance/iteration_*_regression.md`

---

## 目录

- [变更日志索引](#变更日志索引)
- [按日期窗口详情](#按日期窗口详情)
  - [2026-02-02](#2026-02-02)
  - [2026-02-01](#2026-02-01)
  - [2026-01-31](#2026-01-31)
- [验收门禁速查](#验收门禁速查)

---

## 变更日志索引

| 日期 | 迭代 | 类别 | 影响域 | 摘要 | 状态 | 回归记录 |
|------|------|------|--------|------|------|----------|
| 2026-02-02 | Iteration 12 | feature/fix | Gateway/Tests | Gateway 测试全通过，状态隔离修复 | ✅ PASS | [iteration_12_regression.md](../acceptance/iteration_12_regression.md) |
| 2026-02-01 | Iteration 11 | feature/fix | CI/Gateway | mypy baseline 清零 (86→0)，Gateway 测试收敛 (21→8) | 🔄 SUPERSEDED | [iteration_11_regression.md](../acceptance/iteration_11_regression.md) |
| 2026-02-01 | Iteration 10 | fix | CI/Gateway | lint 修复，mypy baseline 86 新增错误 | 🔄 SUPERSEDED | [iteration_10_regression.md](../acceptance/iteration_10_regression.md) |
| 2026-02-01 | Iteration 9 | fix | CI | Ruff 修复 (52 自动+6 手动)，mypy baseline 77 新增错误 | 🔄 SUPERSEDED | [iteration_9_regression.md](../acceptance/iteration_9_regression.md) |
| 2026-02-01 | Iteration 8 | feature | CI/Gateway/SQL | DI 边界门禁、Workflow Contract、CLI 入口点一致性 | 🔄 SUPERSEDED | [iteration_8_regression.md](../acceptance/iteration_8_regression.md) |
| 2026-02-01 | Iteration 7 | fix | CI | Ruff 修复 124→0，No-root-wrappers 门禁 | 🔄 SUPERSEDED | [iteration_7_regression.md](../acceptance/iteration_7_regression.md) |
| 2026-02-01 | Iteration 6 | feature/fix | CI/Gateway/Tests | lint 44 错误，Gateway 测试 7 失败 | ⚠️ PARTIAL | [iteration_6_regression.md](../acceptance/iteration_6_regression.md) |
| 2026-02-01 | Iteration 5 | fix | CI/Gateway/Tests | CI 流水线验证，28 个 Gateway 测试失败 | ⚠️ PARTIAL | [iteration_5_regression.md](../acceptance/iteration_5_regression.md) |
| 2026-01-31 | Iteration 4 | fix | Gateway/Logbook/Tests | Format/Lint 修复，DI 测试重构 | ⚠️ PARTIAL | [iteration_4_regression.md](../acceptance/iteration_4_regression.md) |
| 2026-01-31 | Iteration 3 | feature | SQL/CLI/Gateway/CI/Tests/Docs | SQL 迁移重构，6 主题提交 | ✅ PASS | [iteration_3_regression.md](../acceptance/iteration_3_regression.md) |

---

## 按日期窗口详情

### 2026-02-02

#### Iteration 12

| 项目 | 内容 |
|------|------|
| **类别** | feature/fix |
| **影响域** | Gateway/Tests |
| **成果** | Gateway 测试全通过（1005 通过, 206 跳过）；状态隔离机制完善 |

**关键文件**：

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `tests/gateway/helpers/__init__.py` | fix | 合并 helpers.py 解决模块路径冲突 |
| `tests/gateway/test_worker_importerror_fast_fail.py` | fix | 更新 ErrorCode 导入路径断言 |
| `tests/gateway/test_evidence_upload.py` | fix | 使用 `patch_sys_modules()` 替代直接写入 |
| `tests/gateway/test_migrate_import.py` | fix | 更新断言匹配实际 CLI 入口 |
| `tests/gateway/conftest.py` | fix | 添加 mcp_rpc/middleware/lazy-import 状态重置 |
| `src/engram/gateway/mcp_rpc.py` | feature | 添加 `reset_current_correlation_id_for_testing()` |
| `src/engram/gateway/__init__.py` | feature | 添加 `_reset_gateway_lazy_import_cache_for_testing()` |

**验收门禁**：

```bash
make ci && pytest tests/gateway/ -q && pytest tests/acceptance/ -q
```

**链接**：[iteration_12_regression.md](../acceptance/iteration_12_regression.md)

---

### 2026-02-01

#### Iteration 11 (SUPERSEDED by Iteration 12)

| 项目 | 内容 |
|------|------|
| **类别** | feature/fix |
| **影响域** | CI/Gateway |
| **成果** | mypy baseline 清零（86→0）；Gateway 测试失败收敛（21→8） |

**关键文件**：

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `src/engram/logbook/gitlab_client.py` | fix | 类型注解完善（GitLab REST API） |
| `src/engram/logbook/artifact_store.py` | fix | boto3 S3 客户端类型安全 |
| `src/engram/logbook/artifact_gc.py` | fix | dataclass 定义 GCCandidate/GCResult |
| `src/engram/logbook/scm_db.py` | fix | psycopg 游标返回类型 |
| `src/engram/logbook/scm_integrity_check.py` | fix | TypedDict 定义 PatchBlobRowDict |
| `scripts/ci/mypy_baseline.txt` | fix | 基线清零 |

**验收门禁**：

```bash
make ci  # 全部 14 项检查通过
pytest tests/gateway/ -q  # 1188 通过, 8 失败, 204 跳过
pytest tests/acceptance/ -q  # 132 通过, 0 失败, 48 跳过
```

**链接**：[iteration_11_regression.md](../acceptance/iteration_11_regression.md)

---

#### Iteration 10 (SUPERSEDED by Iteration 11)

| 项目 | 内容 |
|------|------|
| **类别** | fix |
| **影响域** | CI/Gateway |
| **成果** | lint 通过；发现 86 个 mypy 新增错误；Acceptance 测试 158 通过 |

**关键文件**：

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `src/engram/gateway/app.py` | 待修复 | Missing named argument "error_code" |
| `src/engram/gateway/evidence_store.py` | 待修复 | Incompatible types |
| `src/engram/logbook/artifact_delete.py` | 待修复 | ParsedUri has no attribute |
| `src/engram/logbook/cli/db_bootstrap.py` | 待修复 | arg-type, call-overload |

**验收门禁**：

```bash
make ci  # mypy baseline gate 失败 (86 新增错误)
pytest tests/gateway/ -q  # 15 失败, 807 通过, 156 跳过
pytest tests/acceptance/ -q  # 158 通过, 50 跳过, 0 失败
```

**链接**：[iteration_10_regression.md](../acceptance/iteration_10_regression.md)

---

#### Iteration 9 (SUPERSEDED by Iteration 10)

| 项目 | 内容 |
|------|------|
| **类别** | fix |
| **影响域** | CI |
| **成果** | Ruff 修复（52 自动 + 6 手动）；mypy baseline 77 新增错误；Acceptance 143 通过 |

**关键文件**：

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `src/engram/gateway/mcp_rpc.py` | fix | 移动 `import re` 到文件顶部 |
| `src/engram/gateway/audit_event.py` | fix | 重新导出 `generate_correlation_id` |
| `tests/acceptance/test_gateway_startup.py` | fix | 移除未使用变量 |
| `tests/test_mypy_gate.py` | fix | 移除未使用变量 (2处) |

**验收门禁**：

```bash
make regression
pytest tests/gateway/ -v  # 813 通过, 4 失败, 156 跳过
pytest tests/acceptance/ -v  # 143 通过, 50 跳过
```

**链接**：[iteration_9_regression.md](../acceptance/iteration_9_regression.md)

---

#### Iteration 8 (SUPERSEDED by Iteration 9)

| 项目 | 内容 |
|------|------|
| **类别** | feature |
| **影响域** | CI/Gateway/SQL |
| **成果** | DI 边界门禁通过；Workflow Contract 验证通过；CLI 入口点一致性通过；SQL 迁移清单一致 |

**关键文件**：

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `scripts/ci/check_gateway_di_boundaries.py` | feature | DI 边界检查脚本 |
| `scripts/ci/workflow_contract.v2.json` | feature | Workflow 合约定义 |
| `scripts/verify_cli_entrypoints_consistency.py` | feature | CLI 入口点检查 |
| `sql/*.sql` | verified | 14 个文件通过一致性检查 |

**验收门禁**：

```bash
make check-gateway-di-boundaries  # 0 违规
make validate-workflows-strict  # 通过
make check-cli-entrypoints  # 6/6 检查通过
make check-migration-sanity  # 14 文件通过
```

**链接**：[iteration_8_regression.md](../acceptance/iteration_8_regression.md)

---

#### Iteration 7 (SUPERSEDED by Iteration 9)

| 项目 | 内容 |
|------|------|
| **类别** | fix |
| **影响域** | CI |
| **成果** | Ruff 错误 124→0；No-root-wrappers 门禁通过；mypy 77 新增错误 |

**关键文件**：

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `scripts/ci/check_no_root_wrappers_usage.py` | feature | 根目录 wrapper 检查 |
| `scripts/ci/no_root_wrappers_allowlist.json` | feature | 例外清单 |
| `scripts/ci/run_mypy_with_baseline.py` | feature | mypy baseline 运行器 |

**验收门禁**：

```bash
ruff check --fix src/ tests/  # 自动修复 124 个错误
python scripts/ci/check_no_root_wrappers_usage.py  # 通过
python scripts/ci/run_mypy_with_baseline.py  # 77 新增错误
```

**链接**：[iteration_7_regression.md](../acceptance/iteration_7_regression.md)

---

#### Iteration 6

| 项目 | 内容 |
|------|------|
| **类别** | feature/fix |
| **影响域** | CI/Gateway/Tests |
| **成果** | lint 错误 124→44；Gateway 测试 28 失败→7 失败；Acceptance 141 通过 |

**关键文件**：

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `tests/logbook/test_scm_sync_reaper.py` | 待修复 | 33 处 F821 导入语法错误 |
| `tests/logbook/test_scm_sync_integration.py` | 待修复 | 2 处 F821 |
| `tests/gateway/test_gateway_startup.py` | fix | 断言更新 |
| `tests/gateway/test_logbook_db.py` | fix | 断言更新 |

**验收门禁**：

```bash
make ci  # lint 44 errors
pytest tests/gateway/ -v  # 7 failed, 798 passed, 156 skipped
pytest tests/acceptance/ -v  # 141 passed, 50 skipped
```

**链接**：[iteration_6_regression.md](../acceptance/iteration_6_regression.md)

---

#### Iteration 5

| 项目 | 内容 |
|------|------|
| **类别** | fix |
| **影响域** | CI/Gateway/Tests |
| **成果** | CI 流水线验证；Gateway 28 失败待修复；DI 边界门禁 21 通过 |

**关键文件**：

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `tests/gateway/test_validate_refs.py` | 待修复 | 8 个环境变量污染 |
| `tests/gateway/test_audit_event_contract.py` | 待修复 | 7 个审计 schema 问题 |
| `tests/gateway/test_correlation_id_proxy.py` | 待修复 | 2 个 DI 边界问题 |
| `tests/gateway/test_evidence_upload.py` | 待修复 | 4 个 DI 边界问题 |

**验收门禁**：

```bash
make ci  # lint 20 errors
pytest tests/gateway/ -q  # 28 failed, 762 passed, 152 skipped
pytest tests/acceptance/ -q  # 2 failed, 7 skipped
pytest tests/gateway/test_di_boundaries.py -q  # 21 passed
```

**链接**：[iteration_5_regression.md](../acceptance/iteration_5_regression.md)

---

### 2026-01-31

#### Iteration 4

| 项目 | 内容 |
|------|------|
| **类别** | fix |
| **影响域** | Gateway/Logbook/Tests |
| **成果** | Format 172 文件修复；Lint 2074→0；Type 289→263；DI 测试重构完成 |

**关键文件**：

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `src/engram/gateway/__init__.py` | fix | 显式重导出 |
| `src/engram/gateway/logbook_adapter.py` | fix | Optional 类型修复 |
| `src/engram/logbook/config.py` | fix | import 顺序, Optional 类型 |
| `src/engram/logbook/errors.py` | fix | Optional 类型 |
| `pyproject.toml` | fix | 添加 types-requests, boto3-stubs 依赖 |
| `tests/gateway/test_mcp_jsonrpc_contract.py` | refactor | DI 测试重构 |

**验收门禁**：

```bash
make format  # 172 files reformatted
make lint  # 2074→0 errors
make typecheck  # 289→263 errors
```

**链接**：[iteration_4_regression.md](../acceptance/iteration_4_regression.md)

---

#### Iteration 3

| 项目 | 内容 |
|------|------|
| **类别** | feature |
| **影响域** | SQL/CLI/Gateway/CI/Tests/Docs |
| **成果** | SQL 迁移重构完成；6 主题提交（SQL→CLI→Gateway→CI→Tests→Docs） |

**关键文件**：

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `sql/01_logbook_schema.sql` ~ `sql/14_*.sql` | refactor | SQL 迁移编号重整 |
| `sql/verify/99_verify_permissions.sql` | move | 验证脚本移到子目录 |
| `src/engram/logbook/cli/db_migrate.py` | feature | CLI 入口收敛 |
| `src/engram/gateway/handlers/` | refactor | main.py 模块化 |
| `.github/workflows/ci.yml` | feature | CI 矩阵强化 |

**验收门禁**：

```bash
make lint  # I001/F401/W293 警告（不阻塞）
make check-schemas  # 7 schema, 19 fixtures 通过
make check-migration-sanity  # 所有必需 SQL 存在
pytest tests/logbook/test_schema_conventions.py -v  # 29 passed
```

**链接**：[iteration_3_regression.md](../acceptance/iteration_3_regression.md)

---

## 验收门禁速查

### 常用命令

| 命令 | 说明 | 适用场景 |
|------|------|----------|
| `make ci` | 完整 CI 检查（14 项） | 所有变更 |
| `pytest tests/gateway/ -q` | Gateway 测试 | Gateway 域变更 |
| `pytest tests/acceptance/ -q` | 验收测试 | 所有变更 |
| `make typecheck-gate` | mypy 基线检查 | 类型相关变更 |
| `make lint` | ruff lint 检查 | 代码质量变更 |
| `make check-schemas` | JSON Schema 校验 | Schema 变更 |
| `make check-migration-sanity` | SQL 迁移检查 | SQL 域变更 |
| `make check-gateway-di-boundaries` | Gateway DI 边界检查 | Gateway 依赖注入变更 |
| `make validate-workflows-strict` | Workflow 合约校验 | CI 配置变更 |
| `make check-cli-entrypoints` | CLI 入口点一致性 | CLI 变更 |

### 按影响域推荐门禁

| 影响域 | 最小门禁 | 完整门禁 |
|--------|----------|----------|
| **CI** | `make lint && make typecheck-gate` | `make ci` |
| **Gateway** | `pytest tests/gateway/ -q` | `make ci && pytest tests/gateway/ -q` |
| **SQL** | `make check-migration-sanity` | `make ci && make verify-permissions` |
| **Docs** | `make check-cli-entrypoints` | `make ci` |
| **Tests** | `pytest tests/gateway/ -q && pytest tests/acceptance/ -q` | `make ci && pytest tests/ -q` |

### 迭代状态说明

| 状态 | 说明 |
|------|------|
| ✅ PASS | 全部门禁通过 |
| ⚠️ PARTIAL | 部分通过，存在非阻断问题 |
| ❌ FAIL | 存在阻断性失败 |
| 🔄 SUPERSEDED | 已被后续迭代取代 |

---

## 相关文档

| 文档 | 说明 |
|------|------|
| [00_acceptance_matrix.md](../acceptance/00_acceptance_matrix.md) | 验收测试矩阵 SSOT |
| [iteration_local_drafts.md](iteration_local_drafts.md) | 本地迭代草稿管理 |
| [adr_iteration_docs_workflow.md](../architecture/adr_iteration_docs_workflow.md) | 迭代文档工作流 ADR |
| [ci_gate_runbook.md](ci_gate_runbook.md) | CI 门禁 Runbook |
| [mypy_baseline.md](mypy_baseline.md) | Mypy 基线管理指南 |

---

## 变更记录

| 日期 | 变更内容 |
|------|----------|
| 2026-02-02 | 补充 Iteration 3-12 完整记录；移除无 regression 文档的占位日期窗口；添加迭代状态说明 |
| 2026-02-01 | 初始版本：创建迭代/变更日志文档，填入 2026-02-01 日期窗口记录 |

_更新时间：2026-02-02_
