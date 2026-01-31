# SQL 文件重编号映射表

> 版本: 2026-01-31  
> 来源: Git 提交 `c600a56ed83259c3693e373f184b2beebe3e4adc`  
> 提交信息: `chore(sql): reorganize migration numbering and cleanup`

本文档记录 SQL 迁移文件重编号过程中的文件变更映射，供运维和开发参考。

---

## 1. 变更概览

| 变更类型 | 数量 | 说明 |
|---------|------|------|
| 重命名 | 2 | 文件移动或编号变更 |
| 删除（整合） | 6 | 旧编号文件内容整合到新编号文件 |
| 新增 | 1 | 05_openmemory_roles_and_grants.sql |
| 修改 | 6 | 现有文件内容增强 |

---

## 2. 重命名文件映射表

| 旧路径 | 新路径 | 相似度 | 内容等价 | 语义变化 | 运维动作 |
|--------|--------|--------|----------|----------|----------|
| `sql/08_evidence_uri_column.sql` | `sql/09_evidence_uri_column.sql` | 98% | ✓ 等价 | 仅注释文件名 | **无需操作** |
| `sql/99_verify_permissions.sql` | `sql/verify/99_verify_permissions.sql` | 79% | ✗ 不等价 | 验证逻辑增强 | **建议重新执行验证** |

### 2.1 详细说明

#### 08 → 09 evidence_uri_column.sql

- **变更内容**: 仅文件编号从 08 变为 09，内容中注释提及的文件名同步更新
- **幂等性**: 脚本设计为幂等，可安全重复执行
- **运维动作**: 无需额外操作，DDL 迁移正常执行即可

#### 99 verify_permissions.sql → verify/99_verify_permissions.sql

- **变更内容**: 
  - 移动到 `sql/verify/` 子目录
  - 文件从 931 行增长到 1152 行
  - 新增多项验证检查（FOR ROLE 默认权限、schema owner 等）
- **目录隔离原因**: 验证脚本不应被 PostgreSQL initdb 自动执行
- **运维动作**: 
  - 如已部署，建议执行 `engram-migrate --verify` 确认权限配置正确
  - 新部署无需额外操作

---

## 3. 删除文件（已整合）映射表

以下文件在重编号过程中被删除，其内容已整合到对应的新编号文件中。

| 旧文件 | 整合目标 | 内容等价 | 语义变化 | 运维动作 |
|--------|----------|----------|----------|----------|
| `sql/05_scm_sync_runs.sql` | `sql/06_scm_sync_runs.sql` | ✓ 等价 | 无 | **无需操作** |
| `sql/06_scm_sync_locks.sql` | `sql/07_scm_sync_locks.sql` | ✓ 等价 | 新增 governance.security_events 表 | **无需操作**（新表为可选） |
| `sql/07_scm_sync_jobs.sql` | `sql/08_scm_sync_jobs.sql` | ✓ 等价 | 无 | **无需操作** |
| `sql/09_sync_jobs_dimension_columns.sql` | `sql/11_sync_jobs_dimension_columns.sql` | ✓ 等价 | 无 | **无需操作** |
| `sql/10_governance_artifact_ops_audit.sql` | `sql/12_governance_artifact_ops_audit.sql` | ✓ 等价 | 无 | **无需操作** |
| `sql/11_governance_object_store_audit_events.sql` | `sql/13_governance_object_store_audit_events.sql` | ✓ 等价 | 无 | **无需操作** |

### 3.1 整合说明

- **内容保留**: 所有删除文件的 DDL 内容完整迁移到新编号文件
- **幂等性**: 目标文件使用 `CREATE ... IF NOT EXISTS` 设计，可安全重复执行
- **新增内容**: `07_scm_sync_locks.sql` 新增了 `governance.security_events` 表定义

---

## 4. 新增文件

| 文件 | 功能 | 来源提交 |
|------|------|----------|
| `sql/05_openmemory_roles_and_grants.sql` | OpenMemory schema 权限配置 | `55a41690` |

### 4.1 说明

此文件在编号 05 位置新增，用于 OpenMemory 集成。原 05 编号的 `05_scm_sync_runs.sql` 内容已整合到 `06_scm_sync_runs.sql`。

---

## 5. 修改文件

| 文件 | 变更性质 | 运维动作 |
|------|----------|----------|
| `sql/01_logbook_schema.sql` | 新增表/列定义 | **无需操作**（幂等） |
| `sql/02_scm_migration.sql` | 新增迁移逻辑 | **无需操作**（幂等） |
| `sql/04_roles_and_grants.sql` | 权限增强 | **建议重新执行** `--apply-roles` |
| `sql/05_openmemory_roles_and_grants.sql` | FOR ROLE 语法增强、owner 设置 | **建议重新执行** `--apply-openmemory-grants` |
| `sql/08_scm_sync_jobs.sql` | 表定义整合 | **无需操作**（幂等） |
| `sql/13_governance_object_store_audit_events.sql` | UP/DOWN 标记完善 | **无需操作** |

---

## 6. 编号对照表（完整）

### 6.1 旧编号 → 新编号映射

| 旧编号 | 旧文件名 | 新编号 | 新文件名 | 状态 |
|--------|----------|--------|----------|------|
| 01 | 01_logbook_schema.sql | 01 | 01_logbook_schema.sql | 保留（修改） |
| 02 | 02_scm_migration.sql | 02 | 02_scm_migration.sql | 保留（修改） |
| 03 | 03_pgvector_extension.sql | 03 | 03_pgvector_extension.sql | 保留 |
| 04 | 04_roles_and_grants.sql | 04 | 04_roles_and_grants.sql | 保留（修改） |
| 05 | 05_scm_sync_runs.sql | 06 | 06_scm_sync_runs.sql | **整合** |
| - | （新增） | 05 | 05_openmemory_roles_and_grants.sql | **新增** |
| 06 | 06_scm_sync_locks.sql | 07 | 07_scm_sync_locks.sql | **整合** |
| 07 | 07_scm_sync_jobs.sql | 08 | 08_scm_sync_jobs.sql | **整合** |
| 08 | 08_evidence_uri_column.sql | 09 | 09_evidence_uri_column.sql | **重命名** |
| 09 | 09_sync_jobs_dimension_columns.sql | 11 | 11_sync_jobs_dimension_columns.sql | **整合** |
| 10 | 10_governance_artifact_ops_audit.sql | 12 | 12_governance_artifact_ops_audit.sql | **整合** |
| 11 | 11_governance_object_store_audit_events.sql | 13 | 13_governance_object_store_audit_events.sql | **整合** |
| 99 | 99_verify_permissions.sql | 99 | verify/99_verify_permissions.sql | **迁移到子目录** |

### 6.2 缺失编号说明

| 编号 | 状态 | 说明 |
|------|------|------|
| 10 | 保留未用 | 原计划预留，当前无文件 |

---

## 7. 运维检查清单

### 7.1 已部署环境升级

对于已部署的环境，执行以下检查：

```bash
# 1. 执行 DDL 迁移（幂等，可安全重复执行）
engram-migrate --dsn "$POSTGRES_DSN"

# 2. 重新应用角色权限（推荐，因 FOR ROLE 语法增强）
engram-migrate --dsn "$POSTGRES_DSN" --apply-roles

# 3. 重新应用 OpenMemory 权限（推荐，因 owner 设置增强）
engram-migrate --dsn "$POSTGRES_DSN" --apply-openmemory-grants

# 4. 执行验证（验证脚本已增强）
engram-migrate --dsn "$POSTGRES_DSN" --verify
```

### 7.2 新环境部署

新环境部署无需特殊处理，正常执行迁移即可：

```bash
# 完整迁移（包含权限和验证）
engram-migrate --dsn "$POSTGRES_DSN" \
    --apply-roles \
    --apply-openmemory-grants \
    --verify
```

### 7.3 验证迁移完整性

```bash
# 检查所有表是否存在
engram-logbook health

# 严格模式验证权限（CI 门禁）
engram-migrate --dsn "$POSTGRES_DSN" --verify --verify-strict
```

---

## 8. 相关文档

| 文档 | 说明 |
|------|------|
| [sql_file_inventory.md](sql_file_inventory.md) | SQL 文件完整清单与功能说明 |
| [upgrade_after_sql_renumbering.md](upgrade_after_sql_renumbering.md) | SQL 重编号后升级 Runbook（兼容期规则与升级步骤） |
| [03_deploy_verify_troubleshoot.md](03_deploy_verify_troubleshoot.md) | 部署、验收与排错指南 |
| [upgrade_after_sql_renumbering.md](upgrade_after_sql_renumbering.md) | **SQL 重编号后升级指南**（完整升级流程、诊断与修复） |

---

## 9. 变更历史

| 日期 | 变更内容 |
|------|----------|
| 2026-01-31 | 初始版本，记录 SQL 文件重编号映射 |
