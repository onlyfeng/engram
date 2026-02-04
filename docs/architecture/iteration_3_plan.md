# Iteration 3 计划：提交拆分策略

> **状态**：进行中  
> **起始日期**：2026-01-31  
> **目标**：将 Iteration 2 的大型变更集合理拆分为独立、可审查的主题提交

---

## 概述

Iteration 2 积累了大量变更，涉及多个子系统。为保证代码审查质量和变更可追溯性，本计划将变更拆分为 **6 个独立主题**，每个主题形成一个独立提交。

---

## 拆分策略

### 提交顺序与主题

| 序号 | 主题 | Commit Message | 依赖 |
|------|------|----------------|------|
| 1 | SQL 迁移整理 | `chore(sql): reorganize migration numbering and cleanup` | 无 |
| 2 | 脚本入口收敛 | `refactor(cli): consolidate script entrypoints into src/engram` | #1 |
| 3 | Gateway 模块化 | `refactor(gateway): modularize main.py with DI and handlers` | #2 |
| 4 | CI 矩阵 | `ci: harden CI pipeline and add validation steps` | #1, #2 |
| 5 | 测试修复 | `test: update tests for new module structure` | #3, #4 |
| 6 | 文档对齐 | `docs: sync documentation with code changes` | #5 |

---

## 各主题详细文件清单

### 主题 1: SQL 迁移整理

**范围**：SQL 迁移脚本的重新编号和整理

**变更类型**：
- 删除旧编号文件
- 修改/新增重编号文件
- 验证脚本更新

**文件清单**：

```
删除:
  sql/05_scm_sync_runs.sql
  sql/06_scm_sync_locks.sql
  sql/07_scm_sync_jobs.sql
  sql/08_evidence_uri_column.sql
  sql/09_sync_jobs_dimension_columns.sql
  sql/10_governance_artifact_ops_audit.sql
  sql/11_governance_object_store_audit_events.sql
  sql/99_verify_permissions.sql

修改:
  sql/01_logbook_schema.sql
  sql/02_scm_migration.sql
  sql/04_roles_and_grants.sql
  sql/05_openmemory_roles_and_grants.sql
  sql/08_scm_sync_jobs.sql
  sql/13_governance_object_store_audit_events.sql

新增:
  sql/06_scm_sync_runs.sql
  sql/07_scm_sync_locks.sql
  sql/09_evidence_uri_column.sql
  sql/verify/99_verify_permissions.sql

关联:
  apps/logbook_postgres/sql/99_verify_permissions.sql
```

**验收标准**：
- [ ] `ls sql/*.sql` 文件编号连续无间隙
- [ ] `make migrate && make migrate` 幂等执行无报错
- [ ] 所有 SQL 文件包含 UP/DOWN 注释标记

---

### 主题 2: 脚本入口收敛

**范围**：CLI 入口点迁移到 `src/engram/` 包结构

**变更类型**：
- 根目录脚本迁移到 scripts/
- src/engram/logbook/cli/ 新增模块
- pyproject.toml 入口更新
- Makefile 命令更新

**文件清单**：

```
根目录脚本 (修改为 deprecation wrapper):
  artifact_audit.py
  artifact_cli.py
  artifact_gc.py
  artifact_migrate.py
  db.py
  db_bootstrap.py
  db_migrate.py
  logbook_cli.py
  logbook_cli_main.py
  scm_sync_gitlab_commits.py
  scm_sync_gitlab_mrs.py
  scm_sync_reaper.py
  scm_sync_runner.py
  scm_sync_status.py
  scm_sync_svn.py
  scm_sync_worker.py

scripts/ 目录新增:
  scripts/artifact_audit.py
  scripts/artifact_cli.py
  scripts/artifact_gc.py
  scripts/artifact_migrate.py
  scripts/db_bootstrap.py
  scripts/logbook_cli_main.py
  scripts/scm_sync_gitlab_commits.py
  scripts/scm_sync_gitlab_mrs.py
  scripts/scm_sync_reaper.py
  scripts/scm_sync_scheduler.py
  scripts/scm_sync_svn.py
  scripts/scm_sync_worker.py

src/engram/logbook/ 新增:
  src/engram/logbook/cli/db_bootstrap.py
  src/engram/logbook/cli/artifacts.py
  src/engram/logbook/cli/scm_sync.py
  src/engram/logbook/scm_db.py
  src/engram/logbook/scm_sync_executor.py
  src/engram/logbook/scm_sync_reaper_core.py
  src/engram/logbook/scm_sync_runner.py
  src/engram/logbook/scm_sync_status.py
  src/engram/logbook/scm_sync_worker_core.py

配置文件:
  pyproject.toml
  Makefile

旧别名兼容目录:
  logbook_postgres/scripts/*.py (修改)
```

**验收标准**：
- [ ] `python -m engram.logbook.cli.db_migrate --help` 正常输出
- [ ] `python -m engram.logbook.cli.db_bootstrap --help` 正常输出
- [ ] 根目录旧脚本执行输出 DeprecationWarning
- [ ] `make lint` 无导入错误

---

### 主题 3: Gateway 模块化

**范围**：拆分 `main.py` 单体，按职责分离

**变更类型**：
- main.py 瘦身
- 新增 DI/容器模块
- 新增 handlers/ 目录
- 新增 services/ 目录

**文件清单**：

```
src/engram/gateway/ 修改:
  src/engram/gateway/main.py
  src/engram/gateway/audit_event.py
  src/engram/gateway/logbook_db.py
  src/engram/gateway/mcp_rpc.py
  src/engram/gateway/openmemory_client.py
  src/engram/gateway/policy.py
  src/engram/gateway/reconcile_outbox.py

src/engram/gateway/ 新增:
  src/engram/gateway/app.py
  src/engram/gateway/container.py
  src/engram/gateway/di.py
  src/engram/gateway/startup.py
  src/engram/gateway/handlers/__init__.py
  src/engram/gateway/handlers/evidence_upload.py
  src/engram/gateway/handlers/governance_update.py
  src/engram/gateway/handlers/memory_query.py
  src/engram/gateway/handlers/memory_store.py
  src/engram/gateway/services/__init__.py
  src/engram/gateway/services/actor_validation.py
  src/engram/gateway/services/audit_service.py
  src/engram/gateway/services/hash_utils.py
```

**验收标准**：
- [ ] `wc -l src/engram/gateway/main.py` ≤ 200
- [ ] `ls src/engram/gateway/handlers/` 包含至少 4 个 handler
- [ ] `pytest tests/gateway/test_gateway_startup.py` 通过
- [ ] Gateway 可正常启动并响应 health check

---

### 主题 4: CI 矩阵

**范围**：CI 流水线强化和验证步骤

**变更类型**：
- 移除 `|| true` 宽松处理
- 新增验证脚本
- 测试矩阵扩展

**文件清单**：

```
CI 配置:
  .github/workflows/ci.yml

验证脚本:
  scripts/ci/check_env_var_consistency.py
  scripts/verify_logbook_consistency.py
```

**验收标准**：
- [ ] CI lint 步骤不包含 `|| true`
- [ ] CI 包含 schema-validate job
- [ ] CI 包含迁移验证步骤
- [ ] `make validate-workflows` 本地通过

---

### 主题 5: 测试修复

**范围**：更新测试以适配新模块结构

**变更类型**：
- 更新 import 路径
- 新增测试文件
- 修复失败测试

**文件清单**：

```
tests/gateway/ 修改:
  tests/gateway/test_actor_user_id.py
  tests/gateway/test_audit_event_contract.py
  tests/gateway/test_error_codes.py
  tests/gateway/test_main_dedup.py
  tests/gateway/test_mcp_jsonrpc_contract.py
  tests/gateway/test_policy.py
  tests/gateway/test_validate_refs.py

tests/gateway/ 新增:
  tests/gateway/fakes.py
  tests/gateway/test_gateway_startup.py
  tests/gateway/test_memory_query_fallback.py

tests/logbook/ 修改:
  tests/logbook/test_db_bootstrap.py
  tests/logbook/test_logbook_smoke.py
  tests/logbook/test_migrate_idempotency_and_missing.py
  tests/logbook/test_scm_sync_integration.py
  tests/logbook/test_scm_sync_job_payload_contract.py
  tests/logbook/test_scm_sync_lock.py
  tests/logbook/test_scm_sync_payload_contract.py
  tests/logbook/test_scm_sync_queue.py
  tests/logbook/test_scm_sync_reaper.py
  tests/logbook/test_scm_sync_run_contract.py
  tests/logbook/test_scm_sync_scheduler_policy.py
  tests/logbook/test_scm_sync_state_machine_invariants.py
  tests/logbook/test_scm_sync_status.py
  tests/logbook/test_scm_sync_worker.py
  tests/logbook/test_verify_permissions_coverage.py

tests/logbook/ 新增:
  tests/logbook/test_database_hardening.py
  tests/logbook/test_engram_logbook_alias.py
  tests/logbook/test_object_store_audit_indexes.py
  tests/logbook/test_sql_migrations_sanity.py
  tests/logbook/test_sync_jobs_dimension_columns_migration.py
  tests/logbook/test_sync_jobs_index_migration.py

tests/acceptance/ 修改:
  tests/acceptance/test_cli.py
```

**验收标准**：
- [ ] `pytest tests/gateway/` 全部通过
- [ ] `pytest tests/logbook/` 全部通过
- [ ] `pytest tests/acceptance/` 全部通过
- [ ] 无 import 错误或 module not found

---

### 主题 6: 文档对齐

**范围**：文档与代码变更同步

**变更类型**：
- 更新现有文档
- 新增架构文档
- 补充操作指南

**文件清单**：

```
docs/architecture/ 修改:
  docs/architecture/README.md
  docs/architecture/naming.md

docs/architecture/ 新增:
  docs/architecture/adr_gateway_di_and_entry_boundary.md
  docs/architecture/cli_entrypoints.md
  docs/architecture/iteration_2_plan.md

docs/logbook/ 修改:
  docs/logbook/02_tools_contract.md
  docs/logbook/03_deploy_verify_troubleshoot.md
  docs/logbook/04_acceptance_criteria.md
  docs/logbook/05_definition_of_done.md
  docs/logbook/README.md

docs/logbook/ 新增:
  docs/logbook/06_scm_sync_subsystem.md
  docs/logbook/07_scm_sync_ops_guide.md
  docs/logbook/sql_file_inventory.md

docs/ 其他修改:
  docs/contracts/gateway_policy_v2.md
  docs/gateway/07_capability_boundary.md
  docs/installation.md
  docs/reference/environment_variables.md

其他:
  README.md
  schemas/scm_sync_job_payload_v2.schema.json
  schemas/fixtures/scm_sync_job_payload_v2/probe_mode.json
  .agentx/ (新增)
```

**验收标准**：
- [ ] `README.md` 索引与 `docs/` 结构一致
- [ ] 所有文档内链接可访问
- [ ] ADR 状态标注正确
- [ ] 环境变量文档与代码默认值一致

---

## 执行指南

### 1. 执行顺序

```
┌─────────────────────┐
│  1. SQL 迁移整理     │  (基础，无依赖)
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│  2. 脚本入口收敛     │  (依赖 #1 的迁移脚本路径)
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│  3. Gateway 模块化   │  (依赖 #2 的 CLI 结构)
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│  4. CI 矩阵          │  (依赖 #1, #2 的验证脚本)
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│  5. 测试修复         │  (依赖 #3, #4 的模块结构)
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│  6. 文档对齐         │  (最后，依赖所有变更完成)
└─────────────────────┘
```

### 2. 提交模板

每个提交应遵循以下格式：

```
<type>(<scope>): <subject>

<body>

Refs: iteration-3
Part: X/6 (<主题名>)
```

**示例**：

```
chore(sql): reorganize migration numbering and cleanup

- Renumber SQL migration files for continuous sequence
- Add verify/ subdirectory for validation scripts
- Ensure UP/DOWN markers in all migration files

Refs: iteration-3
Part: 1/6 (SQL 迁移整理)
```

### 3. 分支策略

建议采用以下分支结构：

```
main
  └── feat/iteration-3-split
        ├── sql-migration-cleanup      (主题 1)
        ├── cli-entrypoint-consolidation  (主题 2)
        ├── gateway-modularization     (主题 3)
        ├── ci-hardening               (主题 4)
        ├── test-fixes                 (主题 5)
        └── docs-alignment             (主题 6)
```

---

## 验收检查清单

在合并前，确保以下所有检查通过：

### 全局验收

| 检查项 | 命令 | 预期结果 |
|--------|------|----------|
| Lint 通过 | `make lint` | Exit 0 |
| 单元测试通过 | `make test` | Exit 0 |
| 迁移验证通过 | `make migrate-verify` | Exit 0 |
| 文档链接有效 | `make docs-check` | Exit 0 |

### 主题级验收

每个主题提交前须通过其对应验收标准（见上文各主题章节）。

---

## 回滚计划

如某主题提交导致问题，可按以下步骤回滚：

1. **识别问题主题**：通过 CI 失败或测试报告定位
2. **Revert 提交**：`git revert <commit-sha>`
3. **修复后重新提交**：修复问题后按原主题重新提交
4. **更新本计划**：记录回滚原因和修复内容

---

## 相关文档

| 文档 | 说明 |
|------|------|
| [iteration_2_plan.md](iteration_2_plan.md) | 原始迭代计划 |
| [cli_entrypoints.md](cli_entrypoints.md) | CLI 入口规范 |
| [adr_gateway_di_and_entry_boundary.md](adr_gateway_di_and_entry_boundary.md) | Gateway DI 架构决策 |
| [../logbook/sql_file_inventory.md](../logbook/sql_file_inventory.md) | SQL 文件清单 |

---

更新时间：2026-01-31
