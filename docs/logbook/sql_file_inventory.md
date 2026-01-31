# SQL 文件清单与迁移指南

> 版本: 2026-01-31  
> 维护者: engram team

本文档记录 `sql/` 目录下所有 SQL 文件的分类、功能、执行顺序及各入口点的使用说明。

---

## 1. 文件清单总览

| 前缀 | 文件名 | 功能域 | 类型 | 说明 |
|-----|-------|--------|------|------|
| 01 | 01_logbook_schema.sql | Core | DDL | 核心 schema 与表定义 |
| 02 | 02_scm_migration.sql | SCM | DDL | SCM 表结构升级迁移 |
| 03 | 03_pgvector_extension.sql | Extension | DDL | pgvector 扩展初始化 |
| 04 | 04_roles_and_grants.sql | Roles/Grants | Permission | Engram 角色与权限 |
| 05 | 05_openmemory_roles_and_grants.sql | OpenMemory | Permission | OpenMemory schema 权限 |
| 06 | 06_scm_sync_runs.sql | SCM Sync | DDL | sync_runs 同步运行记录表 |
| 07 | 07_scm_sync_locks.sql | SCM Sync + Governance | DDL | sync_locks 分布式锁表 + security_events |
| 08 | 08_scm_sync_jobs.sql | SCM Sync | DDL | sync_jobs 任务队列表 |
| 09 | 09_evidence_uri_column.sql | SCM Migration | DDL | patch_blobs 添加 evidence_uri 列 |
| ~~10~~ | *(已废弃)* | - | - | 编号保留，不再使用 |
| 11 | 11_sync_jobs_dimension_columns.sql | SCM Sync | DDL | sync_jobs 添加维度列 |
| 12 | 12_governance_artifact_ops_audit.sql | Governance | DDL | artifact 操作审计表 |
| 13 | 13_governance_object_store_audit_events.sql | Governance | DDL | 对象存储审计事件表 |
| 99 | verify/99_verify_permissions.sql | Verification | Verify | 权限验证脚本（位于 verify/ 子目录） |

---

## 2. 功能域分组

### 2.1 Core Schema (01-03)

| 文件 | 内容 | 创建的对象 |
|-----|------|-----------|
| `01_logbook_schema.sql` | 核心 DDL | identity.*, logbook.*, scm.*, analysis.*, governance.* schema 及表 |
| `02_scm_migration.sql` | SCM 表升级 | svn_revisions/git_commits/mrs 的 source_id、代理主键等 |
| `03_pgvector_extension.sql` | 扩展 | pgvector (vector) 扩展 |

**保留版本**: 当前版本（幂等设计，可重复执行）

### 2.2 Roles & Grants (04-05)

| 文件 | 内容 | 创建的角色 |
|-----|------|-----------|
| `04_roles_and_grants.sql` | Engram 权限 | engram_admin, engram_migrator, engram_app_readwrite, engram_app_readonly, openmemory_migrator, openmemory_app |
| `05_openmemory_roles_and_grants.sql` | OM schema | openmemory schema 及对应权限 |

**保留版本**: 当前版本
**执行条件**: 需要 superuser 或 CREATEROLE 权限

### 2.3 SCM Sync (06-09, 11)

| 文件 | 内容 | 创建的表 |
|-----|------|---------|
| `06_scm_sync_runs.sql` | 同步运行记录 | scm.sync_runs |
| `07_scm_sync_locks.sql` | 分布式锁 + 安全事件 | scm.sync_locks, governance.security_events |
| `08_scm_sync_jobs.sql` | 任务队列 | scm.sync_jobs |
| `09_evidence_uri_column.sql` | evidence_uri 列迁移 | scm.patch_blobs.evidence_uri |
| `11_sync_jobs_dimension_columns.sql` | 维度列 | scm.sync_jobs.gitlab_instance, tenant_id |

**保留版本**: 当前版本
**注意**: 07 文件同时包含 governance.security_events 表

### 2.4 Governance (12-13)

| 文件 | 内容 | 创建的表 |
|-----|------|---------|
| `12_governance_artifact_ops_audit.sql` | Artifact 操作审计 | governance.artifact_ops_audit |
| `13_governance_object_store_audit_events.sql` | 对象存储审计 | governance.object_store_audit_events |

**保留版本**: 当前版本

### 2.5 Verification (99)

| 文件 | 位置 | 内容 |
|-----|------|------|
| `99_verify_permissions.sql` | `sql/verify/` | 权限验证脚本，检查角色/schema/权限配置 |

**存放位置**: `sql/verify/` 子目录（不被 PostgreSQL initdb 自动执行）

**执行条件**: 仅通过 `--verify` 参数显式触发

---

## 3. 编号规范

### 3.1 当前编号状态

- **无重复编号**: 每个前缀对应唯一文件
- **缺失编号**: 10（已废弃保留）
- **目录结构**:
  - `sql/` - 初始化脚本（被 initdb 自动执行）
  - `sql/verify/` - 验证脚本（仅通过 CLI 显式触发）
- **预留范围**:
  - 01-03: Core/Extension
  - 04-05: Roles/Grants
  - 06-19: Feature DDL (SCM Sync, Governance 等)
  - 20-89: 未来扩展
  - 90-98: 保留
  - 99: Verification（位于 `sql/verify/` 子目录）

### 3.2 新文件编号指南

添加新 SQL 文件时：
1. 使用下一个可用编号（当前为 14）
2. 更新 `src/engram/logbook/migrate.py` 中的 `DDL_SCRIPT_PREFIXES` 常量
3. 更新本文档

---

## 4. 入口点与执行顺序

### 4.1 Python 迁移入口 (推荐)

```bash
# 默认执行 DDL 脚本
python -m engram.logbook.cli.db_migrate --dsn "postgresql://..."

# 包含角色权限脚本
python -m engram.logbook.cli.db_migrate --dsn "..." --apply-roles

# 包含 OpenMemory schema 权限
python -m engram.logbook.cli.db_migrate --dsn "..." --apply-openmemory-grants

# 完整执行（包含验证）
python -m engram.logbook.cli.db_migrate --dsn "..." \
    --apply-roles --apply-openmemory-grants --verify

# 查看迁移计划（不连接数据库）
python -m engram.logbook.cli.db_migrate --plan
python -m engram.logbook.cli.db_migrate --plan --apply-roles --verify
```

#### 4.1.1 迁移计划模式 (`--plan`)

使用 `--plan` 参数可在**不连接数据库**的情况下查看迁移计划：

```bash
# 查看默认迁移计划
engram-migrate --plan

# 查看包含角色权限的迁移计划
engram-migrate --plan --apply-roles

# 查看完整迁移计划（包含验证脚本）
engram-migrate --plan --apply-roles --apply-openmemory-grants --verify

# 跳过配置预检
engram-migrate --plan --no-precheck
```

**输出 JSON 结构**:

```json
{
  "ok": true,
  "plan_mode": true,
  "sql_dir": "/path/to/sql",
  "ddl": ["01_logbook_schema.sql", "02_scm_migration.sql", ...],
  "permissions": ["04_roles_and_grants.sql", "05_openmemory_roles_and_grants.sql"],
  "verify": ["verify/99_verify_permissions.sql"],
  "execute": ["01_logbook_schema.sql", ...],
  "duplicates": {},
  "precheck": {"ok": true, "checks": {...}},
  "flags": {
    "apply_roles": false,
    "apply_openmemory_grants": false,
    "verify": false
  },
  "script_prefixes": {
    "ddl": ["01", "02", "03", "06", "07", "08", "09", "11", "12", "13"],
    "permissions": ["04", "05"],
    "verify": ["99"]
  },
  "summary": {
    "total_files": 14,
    "ddl_count": 11,
    "permissions_count": 2,
    "verify_count": 1,
    "execute_count": 11,
    "duplicate_prefixes": []
  }
}
```

**字段说明**:

| 字段 | 说明 |
|------|------|
| `plan_mode` | 标识为计划模式（始终为 `true`） |
| `sql_dir` | SQL 文件目录的绝对路径 |
| `ddl` | DDL 脚本列表（默认执行） |
| `permissions` | 权限脚本列表（需要 `--apply-roles` 或 `--apply-openmemory-grants`） |
| `verify` | 验证脚本列表（需要 `--verify`） |
| `execute` | 本次将执行的脚本列表（根据开关决定） |
| `duplicates` | 同一前缀对应多个文件的映射（警告） |
| `precheck` | 配置预检结果（不连接数据库） |
| `flags` | 当前开关状态 |
| `script_prefixes` | 脚本前缀分类配置（SSOT） |
| `summary` | 统计摘要 |

**执行顺序（按前缀数字排序）**:

| 阶段 | 文件前缀 | 条件 |
|-----|---------|------|
| DDL | 01, 02, 03, 06, 07, 08, 09, 11, 12, 13 | 默认执行 |
| Permission | 04 | `--apply-roles` |
| Permission | 05 | `--apply-openmemory-grants` |
| Verify | 99 | `--verify` |

**脚本分类逻辑** (见 `migrate.py`):
```python
DDL_SCRIPT_PREFIXES = {"01", "02", "03", "06", "07", "08", "09", "11", "12", "13"}
PERMISSION_SCRIPT_PREFIXES = {"04", "05"}
VERIFY_SCRIPT_PREFIXES = {"99"}
```

### 4.2 Makefile 入口

```bash
# 一键初始化（推荐）
make setup-db

# 仅迁移
make migrate

# 仅验证
make verify

# 仅初始化服务账号
make bootstrap-roles
```

**setup-db 执行流程**:
1. 创建数据库
2. 启用 pgvector 扩展
3. `scripts/db_bootstrap.py` - 初始化服务账号
4. `engram.logbook.cli.db_migrate --apply-roles --apply-openmemory-grants` - 完整迁移
5. `engram.logbook.cli.db_migrate --verify` - 验证权限

### 4.3 Docker initdb 入口

通过 `docker-compose.unified.yml` 配置：

```yaml
services:
  postgres:
    volumes:
      - ./sql:/docker-entrypoint-initdb.d:ro
```

**执行顺序**: 按文件名字母序自动执行 `sql/` 目录下的 `*.sql` 文件

**重要**: 
- initdb 仅在首次初始化空 volume 时执行，已有数据时跳过
- **initdb 不会递归执行子目录**，因此 `sql/verify/` 下的验证脚本不会被自动执行
- 验证脚本（99_verify_permissions.sql）需要通过 `--verify` 参数显式触发

**initdb 执行策略**:

| 目录 | 是否 initdb 自动执行 | 说明 |
|-----|---------------------|------|
| `sql/*.sql` | ✓ 是 | DDL/权限脚本，按文件名顺序执行 |
| `sql/verify/*.sql` | ✗ 否 | 验证脚本，仅通过 CLI 显式触发 |

此策略确保：
1. 数据库初始化不会因验证脚本依赖（如 LOGIN 角色）不满足而失败
2. 验证操作由用户在适当时机显式触发
3. 清晰分离初始化脚本与验证脚本

### 4.4 手动 psql 执行

```bash
# 按顺序执行初始化脚本
psql -d <db> -f sql/01_logbook_schema.sql
psql -d <db> -f sql/02_scm_migration.sql
psql -d <db> -f sql/03_pgvector_extension.sql
psql -d <db> -f sql/04_roles_and_grants.sql  # 需要 superuser
psql -d <db> -c "SET om.target_schema = 'openmemory'" -f sql/05_openmemory_roles_and_grants.sql
psql -d <db> -f sql/06_scm_sync_runs.sql
psql -d <db> -f sql/07_scm_sync_locks.sql
psql -d <db> -f sql/08_scm_sync_jobs.sql
psql -d <db> -f sql/09_evidence_uri_column.sql
psql -d <db> -f sql/11_sync_jobs_dimension_columns.sql
psql -d <db> -f sql/12_governance_artifact_ops_audit.sql
psql -d <db> -f sql/13_governance_object_store_audit_events.sql

# 可选：执行验证脚本（位于 verify/ 子目录）
psql -d <db> -f sql/verify/99_verify_permissions.sql
```

---

## 5. 文件详细说明

### 5.1 01_logbook_schema.sql

**功能**: 创建所有核心 schema 和表

**创建的 Schema**:
- `identity` - 用户身份
- `logbook` - 事件记录
- `scm` - 源码管理
- `analysis` - 分析结果
- `governance` - 治理策略

**创建的表**:
- identity: users, accounts, role_profiles
- logbook: items, events, attachments, kv, outbox_memory
- scm: repos, svn_revisions, git_commits, patch_blobs, mrs, review_events, sync_rate_limits
- analysis: runs, knowledge_candidates
- governance: settings, write_audit, promotion_queue

**特殊对象**:
- `scm.v_facts` - 物化视图
- `logbook.sync_events_payload()` - 触发器函数
- `scm.update_patch_blobs_updated_at()` - 触发器函数

### 5.2 scm.repos 兼容字段

**背景**: `scm.repos` 表历史上使用了两套不同的字段名：
- **新字段**（推荐）: `repo_type`, `url`
- **旧字段**（弃用）: `vcs_type`, `remote_url`

**兼容策略**: 通过触发器实现双向同步

| 推荐字段 | 弃用字段 | 类型 | 说明 |
|---------|---------|------|------|
| `repo_type` | `vcs_type` | text | 仓库类型 ('svn', 'git') |
| `url` | `remote_url` | text | 仓库 URL |

**触发器行为** (`trg_repos_compat_sync`):
- INSERT/UPDATE 时自动同步两组字段
- 如果只提供旧字段，自动复制到新字段
- 新字段优先，同步到旧字段

**唯一约束**:
- 主约束: `UNIQUE(repo_type, url)`
- 兼容索引: `idx_repos_vcs_type_remote_url` (部分索引)

**迁移注意事项**:
- `project_key` 字段已改为可空，兼容不提供此字段的旧代码
- 新代码应使用 `repo_type`, `url`
- 旧代码可继续使用 `vcs_type`, `remote_url`（将自动同步）

### 5.3 04_roles_and_grants.sql

**功能**: 创建 NOLOGIN 权限角色并配置默认权限

**角色体系**:
```
engram_admin          (ALL)
engram_migrator       (DDL + DML) <- logbook_migrator
engram_app_readwrite  (DML)       <- logbook_svc
engram_app_readonly   (SELECT)
openmemory_migrator   (DDL)       <- openmemory_migrator_login
openmemory_app        (DML)       <- openmemory_svc
```

**public schema 策略**: 所有角色 REVOKE CREATE，仅保留 USAGE

### 5.4 05_openmemory_roles_and_grants.sql

**功能**: 创建 OpenMemory 专用 schema 并配置权限

**参数化**: 通过 `SET om.target_schema = 'xxx'` 指定目标 schema

**默认 schema**: `openmemory`

---

## 6. 常见问题

### Q1: 迁移失败后如何重试？

所有 SQL 脚本设计为幂等，可直接重新执行:
```bash
python -m engram.logbook.cli.db_migrate --dsn "..." --apply-roles --apply-openmemory-grants
```

### Q2: 如何验证迁移是否成功？

```bash
python -m engram.logbook.cli.db_migrate --dsn "..." --verify
```

或直接执行:
```bash
psql -d <db> -f sql/verify/99_verify_permissions.sql
```

### Q3: 编号 10 去哪了？

编号 10 已废弃，不再使用。原有内容已合并或移除。

### Q4: 如何添加新的 SQL 迁移脚本？

1. 使用下一个可用编号创建文件（如 `14_xxx.sql`）
2. 编辑 `src/engram/logbook/migrate.py`，将新前缀添加到 `DDL_SCRIPT_PREFIXES`
3. 更新本文档

### Q5: 测试如何覆盖 SQL 文件？

`tests/logbook/test_sql_migrations_sanity.py` 提供以下测试覆盖：

| 测试项 | 覆盖范围 | 说明 |
|-------|---------|------|
| 重复内容检测 | `sql/` + `sql/verify/` | 确保所有 SQL 文件内容不重复 |
| 索引冲突检测 | `sql/` + `sql/verify/` | 确保不同文件中同名索引定义一致 |
| 前缀规范检测 | `sql/` + `sql/verify/` | 确保所有 SQL 文件有两位数前缀 |
| 关键前缀存在性 | `sql/` | 验证关键 DDL 脚本（01-08）存在 |
| 99 前缀验证脚本 | `sql/verify/` | 验证 99 前缀脚本存在于 verify 子目录 |
| 目录隔离检测 | 跨目录 | 主目录不应有 99 前缀，verify 子目录仅允许 99 前缀 |

---

## 7. 相关文档

| 文档 | 说明 |
|------|------|
| [03_deploy_verify_troubleshoot.md](03_deploy_verify_troubleshoot.md) | 部署、验收与排错指南 |
| [03_deploy_verify_troubleshoot.md#部署入口职责边界](03_deploy_verify_troubleshoot.md#部署入口职责边界) | Compose/Makefile/CLI 职责边界对比 |
| [03_deploy_verify_troubleshoot.md#推荐部署流程](03_deploy_verify_troubleshoot.md#推荐部署流程) | 新库初始化/升级/验证流程 |
| [03_deploy_verify_troubleshoot.md#verify-输出判定标准](03_deploy_verify_troubleshoot.md#verify-输出判定标准) | Verify 成功判定与 CI 门禁 |

---

## 8. 变更历史

| 日期 | 变更内容 |
|-----|---------|
| 2026-01-31 | 添加相关文档交叉引用 |
| 2026-01-31 | 添加 scm.repos 兼容字段说明 (vcs_type/remote_url) |
| 2026-01-31 | 初始版本，记录 13 个 SQL 文件 |
