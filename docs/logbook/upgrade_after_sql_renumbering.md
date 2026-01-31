# SQL 重编号后升级 Runbook

> 版本: 2026-01-31  
> 适用版本: 2026-01-31 及以后

本文档为已部署环境在 SQL 文件重编号后的升级指南。

---

## 1. 概述

自 2026-01-31 版本起，`sql/` 目录下的迁移文件进行了重新编号。本文档指导如何安全升级现有部署环境。

**重要**: 升级操作是幂等的，可安全重复执行。

---

## 2. 兼容期规则

### 2.1 为何存在该重编号

重编号的目的是：

1. **腾出编号空位**：在 04（权限角色）之后插入 05（OpenMemory 权限），原 05-11 依次后移
2. **目录隔离**：将验证脚本（99_verify_permissions.sql）移动到 `sql/verify/` 子目录，避免被 PostgreSQL initdb 自动执行
3. **编号连续性**：统一编号风格，保留 10 作为废弃占位

### 2.2 支持的旧文件命名

迁移模块（`migrate.py`）的 `scan_sql_files()` 函数**不支持**旧编号文件名的自动兼容。

| 旧编号 | 旧文件名（已废弃） | 新编号 | 新文件名（当前） |
|--------|-------------------|--------|-----------------|
| 05 | `05_scm_sync_runs.sql` | 06 | `06_scm_sync_runs.sql` |
| 06 | `06_scm_sync_locks.sql` | 07 | `07_scm_sync_locks.sql` |
| 07 | `07_scm_sync_jobs.sql` | 08 | `08_scm_sync_jobs.sql` |
| 08 | `08_evidence_uri_column.sql` | 09 | `09_evidence_uri_column.sql` |
| 09 | `09_sync_jobs_dimension_columns.sql` | 11 | `11_sync_jobs_dimension_columns.sql` |
| 10 | `10_governance_artifact_ops_audit.sql` | 12 | `12_governance_artifact_ops_audit.sql` |
| 11 | `11_governance_object_store_audit_events.sql` | 13 | `13_governance_object_store_audit_events.sql` |

### 2.3 推荐做法

> **生产环境建议使用随版本发布的 `sql/` 目录，不要混用两套编号。**

| 场景 | 推荐操作 |
|------|----------|
| 新部署 | 直接使用当前版本的 `sql/` 目录，无需关心旧编号 |
| 已部署升级 | 1. 删除本地旧编号文件副本<br>2. 使用版本控制同步最新 `sql/` 目录<br>3. 执行 `engram-migrate` 完成升级 |
| 自定义 SQL 目录 | 可使用 `--sql-dir` 参数指定。仅在特殊打包或兼容场景下使用（见 2.5 节） |
| CI/CD 流水线 | 使用 Git checkout 或版本发布包获取 SQL 文件，避免手动维护 |

### 2.4 为何不支持旧编号兼容

1. **幂等设计**：所有 SQL 脚本使用 `CREATE ... IF NOT EXISTS`，新编号文件完全覆盖旧编号功能
2. **避免歧义**：同一前缀多文件会触发警告（`scan_sql_files()` 的 duplicates 检测）
3. **维护成本**：兼容代码增加复杂性，且旧编号仅存在于历史版本中

### 2.5 `--sql-dir` 参数说明

`engram-migrate` 命令支持 `--sql-dir` 参数，用于指定自定义 SQL 文件目录路径。

**默认行为**：使用项目根目录下的 `sql/` 目录。

**使用场景**（仅限特殊情况）：

| 场景 | 说明 |
|------|------|
| 独立打包部署 | 当迁移工具与 SQL 文件分开打包时，需指定 SQL 目录位置 |
| 多版本兼容测试 | 在同一环境测试不同版本的 SQL 脚本 |
| 容器化部署 | SQL 文件挂载到非标准路径时 |

**示例**：

```bash
# 使用默认 sql/ 目录（推荐）
engram-migrate --dsn "$POSTGRES_DSN"

# 指定自定义 SQL 目录（特殊场景）
engram-migrate --dsn "$POSTGRES_DSN" --sql-dir /opt/engram/sql

# 查看迁移计划时指定目录
engram-migrate --plan --sql-dir /custom/path/sql
```

> **注意**：正常情况下不需要使用此参数。使用自定义 SQL 目录时，请确保目录中的文件编号与当前版本一致，否则可能导致迁移失败或数据库结构不完整。

---

## 3. 升级步骤

### 3.1 标准升级（推荐）

```bash
# 1. 同步最新代码（包含新编号的 sql/ 目录）
git pull origin master

# 2. 执行 DDL 迁移（幂等，可安全重复执行）
engram-migrate --dsn "$POSTGRES_DSN"

# 3. 重新应用角色权限（推荐，因 FOR ROLE 语法增强）
engram-migrate --dsn "$POSTGRES_DSN" --apply-roles

# 4. 重新应用 OpenMemory 权限（推荐，因 owner 设置增强）
engram-migrate --dsn "$POSTGRES_DSN" --apply-openmemory-grants

# 5. 执行验证（验证脚本已增强）
engram-migrate --dsn "$POSTGRES_DSN" --verify
```

### 3.2 一键升级

```bash
# 完整迁移（包含权限和验证）
engram-migrate --dsn "$POSTGRES_DSN" \
    --apply-roles \
    --apply-openmemory-grants \
    --verify
```

### 3.3 Docker 环境升级

```bash
# 1. 停止服务
docker compose -f docker-compose.unified.yml down

# 2. 拉取最新镜像
docker compose -f docker-compose.unified.yml pull

# 3. 启动迁移服务
docker compose -f docker-compose.unified.yml up bootstrap_roles logbook_migrate openmemory_migrate

# 4. 启动应用服务
docker compose -f docker-compose.unified.yml up -d
```

---

## 4. 验证清单

升级完成后，执行以下验证：

```bash
# 检查所有表是否存在
engram-logbook health

# 严格模式验证权限（CI 门禁，默认 fail_only 策略：仅 FAIL 触发失败）
engram-migrate --dsn "$POSTGRES_DSN" --verify --verify-strict

# 查看迁移计划确认文件列表
engram-migrate --plan
```

**预期输出**:
- `engram-logbook health` 返回 `ok: true`
- `--verify` 无错误输出
- `--plan` 显示新编号文件列表（01-13, 不含 10）

### 4.1 迁移计划预览（--plan 组合用法）

`--plan` 参数可与其他参数组合，预览不同场景的执行计划：

```bash
# 仅查看 DDL 脚本（默认）
engram-migrate --plan

# 查看包含角色权限的迁移计划
engram-migrate --plan --apply-roles

# 查看完整迁移计划（DDL + 权限 + 验证）
engram-migrate --plan --apply-roles --apply-openmemory-grants --verify

# 跳过预检（仅查看脚本列表）
engram-migrate --plan --no-precheck
```

**Makefile 等价命令**：

```bash
# 查看迁移计划
make migrate-plan

# 查看完整迁移计划（含权限和验证）
make migrate-plan-full
```

### 4.2 预检模式（--precheck-only）

在执行实际迁移前，使用 `--precheck-only` 仅验证配置和环境：

```bash
# 仅执行预检（需要数据库连接）
engram-migrate --dsn "$POSTGRES_DSN" --precheck-only

# 组合使用：预检 + 查看计划
engram-migrate --dsn "$POSTGRES_DSN" --precheck-only --apply-roles
```

**Makefile 等价命令**：

```bash
make migrate-precheck
```

**预检验证项**：

| 检查项 | 说明 |
|--------|------|
| DSN 有效性 | 数据库连接字符串格式正确 |
| 数据库连接 | 能够成功连接数据库 |
| SQL 文件存在性 | 所有必需的迁移文件存在 |
| OM_PG_SCHEMA 配置 | OpenMemory schema 配置有效（若启用 OM） |

### 4.3 严格验证与日志留存（CI/运维 Runbook）

在 CI/CD 流水线或运维场景中，使用 `tee` + `PIPESTATUS` 确保日志完整留存且正确传递退出码：

```bash
# CI/运维标准 Runbook（可直接复制）
# ----------------------------------------

# 步骤 1: 执行迁移（含权限），留存日志
engram-migrate --dsn "$POSTGRES_DSN" \
    --apply-roles \
    --apply-openmemory-grants \
    2>&1 | tee migration.log
exit ${PIPESTATUS[0]}  # 确保传递 Python 脚本的退出码

# 步骤 2: 严格模式验证，留存日志（默认 fail_only 策略）
engram-migrate --dsn "$POSTGRES_DSN" \
    --verify \
    --verify-strict \
    2>&1 | tee verify.log
exit ${PIPESTATUS[0]}  # 仅 FAIL 会导致非零退出码（默认策略）
```

**关键点说明**：

| 技术点 | 说明 |
|--------|------|
| `2>&1` | 合并 stdout 和 stderr 到同一输出流 |
| `tee file.log` | 同时输出到终端和日志文件 |
| `${PIPESTATUS[0]}` | 获取管道中第一个命令的退出码（而非 tee 的退出码） |

**Gate 策略说明**：

| 策略 | 行为 | 使用场景 |
|------|------|----------|
| `fail_only`（默认） | 仅 FAIL 触发异常，WARN 仅输出警告 | 生产 CI 门禁，允许小问题存在 |
| `fail_and_warn` | FAIL 或 WARN 任一触发异常 | 严格合规环境，零容忍模式 |

如需 WARN 也触发失败，可通过 SQL 配置变量设置：
```bash
# 在 psql 中直接设置
psql -d "$DB" -c "SET engram.verify_gate_policy = 'fail_and_warn'" -f sql/verify/99_verify_permissions.sql
```

**Makefile 等价命令（带日志留存）**：

```bash
# 迁移并留存日志
make migrate-ddl 2>&1 | tee migration.log; exit ${PIPESTATUS[0]}

# 严格验证并留存日志
make verify-permissions-strict 2>&1 | tee verify.log; exit ${PIPESTATUS[0]}
```

---

## 5. 故障排查

### 5.1 发现旧编号文件

如果 `engram-migrate --plan` 输出中出现 `duplicates` 警告：

```json
{
  "duplicates": {
    "05": ["05_scm_sync_runs.sql", "05_openmemory_roles_and_grants.sql"]
  }
}
```

**原因**: 本地 `sql/` 目录存在旧编号文件副本

**解决方案**:
```bash
# 删除旧编号文件（确认后执行）
rm sql/05_scm_sync_runs.sql
rm sql/06_scm_sync_locks.sql
rm sql/07_scm_sync_jobs.sql
rm sql/08_evidence_uri_column.sql
rm sql/09_sync_jobs_dimension_columns.sql
rm sql/10_governance_artifact_ops_audit.sql
rm sql/11_governance_object_store_audit_events.sql

# 或直接使用 Git 重置
git checkout -- sql/
```

### 5.2 验证脚本失败

如果 `--verify` 报告权限错误：

```bash
# 重新应用权限脚本
engram-migrate --dsn "$POSTGRES_DSN" --apply-roles --apply-openmemory-grants

# 再次验证
engram-migrate --dsn "$POSTGRES_DSN" --verify
```

### 5.3 缺少新增表

如果报告表缺失（如 `governance.security_events`）：

```bash
# 完整执行 DDL 迁移
engram-migrate --dsn "$POSTGRES_DSN"

# 验证
engram-migrate --dsn "$POSTGRES_DSN" --verify
```

---

## 6. 相关文档

| 文档 | 说明 |
|------|------|
| [sql_file_inventory.md](sql_file_inventory.md) | SQL 文件完整清单与功能说明 |
| [sql_renumbering_map.md](sql_renumbering_map.md) | SQL 文件重编号映射表（旧编号 → 新编号对照） |
| [03_deploy_verify_troubleshoot.md](03_deploy_verify_troubleshoot.md) | 部署、验收与排错指南 |

---

## 7. 变更历史

| 日期 | 变更内容 |
|------|----------|
| 2026-01-31 | 初始版本，创建 SQL 重编号后升级 Runbook |
