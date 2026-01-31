# 最小部署、验收与排错指南

> **适用人群**：运维、开发者、首次部署者
> **前置知识**：Docker、Docker Compose 基础

---

## 部署级别与验收能力

Logbook 支持两种部署级别，每种级别具有不同的验收能力：

### 级别定义

| 级别 | 名称 | 依赖 | 验收能力 | 适用场景 |
|------|------|------|----------|----------|
| **A** | DB Baseline-only | Docker + Compose | `pg_isready` | 最小化部署、CI smoke |
| **B** | Acceptance-ready | A + Python scripts | 完整验收套件 | 功能验证、发布前检查 |

### A) DB Baseline-only（最小部署）

仅依赖 Docker 和 Docker Compose，**无需安装 Python 脚本**。

**启动命令**：

```bash
docker compose -f compose/logbook.yml up -d
```

**验收能力**：

| 检查项 | 命令 | 成功标志 |
|--------|------|----------|
| 容器运行 | `docker compose -f compose/logbook.yml ps` | postgres 状态 `running` |
| DB 就绪 | `docker exec postgres pg_isready -U postgres` | `accepting connections` |

**限制**：
- 无法执行 `engram-logbook health`、`logbook-smoke` 等 CLI 命令
- 无法验证 Schema 完整性
- 仅适用于验证基础设施就绪

### B) Acceptance-ready（完整验收）

在 DB Baseline 基础上安装 `engram_logbook` Python 包，启用完整验收能力。

**额外依赖**：

```bash
# 安装 Logbook CLI
cd logbook_postgres/scripts && pip install -e .

# 设置 DSN
export POSTGRES_DSN="postgresql://postgres:${POSTGRES_PASSWORD:-postgres}@localhost:5432/${POSTGRES_DB:-engram}"
```

**验收能力**：

| 验收目标 | 命令 | 说明 |
|----------|------|------|
| 健康检查 | `engram-logbook health` | 验证连接、Schema、表、索引 |
| 数据库迁移 | `make migrate-ddl` | 仅执行 DDL 迁移（Schema/表/索引） |
| 角色权限 | `make apply-roles` | 应用 Logbook 角色和权限 |
| OM 权限 | `make apply-openmemory-grants` | 应用 OpenMemory 权限 |
| 权限验证 | `make verify-permissions` | 验证数据库权限配置 |
| 冒烟测试 | `make logbook-smoke` | CRUD + 视图渲染全流程 |
| 完整验收 | `make acceptance-logbook-only` | 一键完整验收（启动→迁移→验证→测试） |

**验收命令对照**：

| 命令 | 依赖级别 | 说明 |
|------|----------|------|
| `docker compose up` + `pg_isready` | A (Baseline) | 最小验证 |
| `engram-logbook health` | B (Acceptance) | CLI 健康检查 |
| `make logbook-smoke` | B (Acceptance) | 冒烟测试 |
| `make acceptance-logbook-only` | B (Acceptance) | 完整验收套件 |

---

## 最小部署

### 环境要求

| 组件 | 版本要求 |
|------|----------|
| Docker | 20.10+ |
| Docker Compose | v2.0+ |
| 可用端口 | 5432（PostgreSQL） |

### 1. 配置 `.env`

在项目根目录创建 `.env` 文件：

```bash
PROJECT_KEY=myproject
POSTGRES_DB=myproject
POSTGRES_PASSWORD=changeme
```

| 变量 | 说明 | 示例 |
|------|------|------|
| `PROJECT_KEY` | 项目标识（用于 Logbook 表前缀） | `myproject` |
| `POSTGRES_DB` | 数据库名（应与 PROJECT_KEY 一致） | `myproject` |
| `POSTGRES_PASSWORD` | PostgreSQL 密码 | `changeme` |

> **服务账号策略（SKIP 模式）**：
>
> | 密码设置情况 | 部署模式 | 行为 |
> |-------------|---------|------|
> | 全部不设置（0/4） | logbook-only | 跳过服务账号创建，使用 postgres 超级用户 |
> | 全部设置（4/4） | unified-stack | 创建独立服务账号 |
> | 部分设置（1-3/4） | **配置错误** | 容器初始化失败，需修正配置 |
>
> **Logbook-only 部署时**，**不要设置**任何 `*_PASSWORD` 环境变量（如 `LOGBOOK_MIGRATOR_PASSWORD`）。脚本检测到无密码变量时自动进入 SKIP 模式，使用 postgres 超级用户。
>
> **unified-stack 部署时**，必须设置全部 4 个密码变量：
> - `LOGBOOK_MIGRATOR_PASSWORD`
> - `LOGBOOK_SVC_PASSWORD`
> - `OPENMEMORY_MIGRATOR_PASSWORD`
> - `OPENMEMORY_SVC_PASSWORD`
>
> 详见 `scripts/db_bootstrap.py` 中的 `detect_deployment_mode()` 函数。

### 2. 启动服务

```bash
make up-logbook
```

此命令执行：
1. 启动 PostgreSQL 容器
2. 执行 Logbook 数据库迁移（创建 identity/logbook/scm/analysis/governance schema）
3. 等待服务健康检查通过

**预期输出**：

```
启动 Logbook 服务
Creating network "myproject-logbook_default" with the default driver
Creating postgres ... done
Creating logbook_migrate ... done
[OK] Logbook 服务已启动
```

### 3. 验证服务状态

```bash
make ps-logbook
```

**预期输出**：

```
NAME                STATUS              PORTS
postgres            running (healthy)   0.0.0.0:5432->5432/tcp
```

---

## 验收测试

### 健康检查

#### 1. PostgreSQL 健康检查

```bash
docker compose -p ${PROJECT_KEY:-engram}-logbook -f compose/logbook.yml ps
```

**预期**：postgres 状态为 `running (healthy)`

#### 2. Logbook CLI 健康检查

```bash
# 安装 Engram（包含 Logbook CLI）
pip install -e .

# 设置 DSN 并检查健康状态
export POSTGRES_DSN="postgresql://postgres:${POSTGRES_PASSWORD:-postgres}@localhost:5432/${POSTGRES_DB:-engram}"
engram-logbook health
```

**预期输出**：

```json
{
  "ok": true,
  "checks": {
    "connection": {"status": "ok", "message": "数据库连接正常"},
    "schemas": {"status": "ok", "details": {...}},
    "tables": {"status": "ok", "total": 21, "existing": 21, "missing": []},
    "matviews": {"status": "ok", "missing": []},
    "indexes": {"status": "ok", "missing": []}
  }
}
```

**`engram-logbook health` 检查项说明**：

| 检查项 | 说明 | 来源 |
|--------|------|------|
| `connection` | 数据库连接状态（`SELECT 1`） | 运行时检测 |
| `schemas` | Schema 存在性检查 | **固定列表**：`identity`, `logbook`, `scm`, `analysis`, `governance` |
| `tables` | 核心表存在性检查（21 张） | 代码内置的 `core_tables` 列表 |
| `matviews` | 物化视图存在性检查 | `scm.v_facts` |
| `indexes` | 关键索引存在性检查 | 代码内置的 `required_indexes` 列表 |

> **Schema 列表来源**：`src/engram/logbook/cli/logbook.py` 中 `cmd_health()` 函数的 `required_schemas` 变量，硬编码为 5 个固定 schema。这是 Logbook 路线 A（多库方案）的架构约束，每个 schema 对应一个业务域。

### 冒烟测试

```bash
make logbook-smoke
```

此命令执行完整的功能验证：

| 步骤 | 验证内容 | 失败码 |
|------|----------|--------|
| 0 | 安装 `engram_logbook` 依赖 | `INSTALL_FAILED` |
| 1 | 检查 PostgreSQL 服务状态 | `SERVICE_NOT_RUNNING` |
| 2 | 执行健康检查 | `HEALTH_CHECK_FAILED` |
| 3 | 创建测试 item | `CREATE_ITEM_FAILED` |
| 4 | 添加事件 | `ADD_EVENT_FAILED` |
| 5 | 添加附件 | `ATTACH_FAILED` |
| 6 | 渲染视图 | `RENDER_VIEWS_FAILED` |

**预期输出**：

```
[OK] engram_logbook 已安装
[OK] 服务已运行
[OK] 健康检查通过
[OK] 创建 item 成功: item_id=1
[OK] 添加事件成功: event_id=1
[OK] 添加附件成功: attachment_id=1
[OK] 渲染视图成功
Logbook 冒烟测试完成！
{"ok":true,"code":"SMOKE_TEST_PASSED","message":"Logbook 冒烟测试通过","ids":{...}}
```

**失败码说明**：

| 失败码 | 说明 | 修复建议 |
|--------|------|----------|
| `INSTALL_FAILED` | `engram_logbook` 安装失败 | `cd logbook_postgres/scripts && pip install -e .` |
| `SERVICE_NOT_RUNNING` | PostgreSQL 服务未运行 | `make deploy` 或设置 `POSTGRES_DSN` 环境变量 |
| `HEALTH_CHECK_FAILED` | 健康检查失败 | 检查 DSN 配置或运行 `make migrate-ddl` |
| `CREATE_ITEM_FAILED` | 创建 item 失败 | 检查数据库连接和 schema 是否存在 |
| `ADD_EVENT_FAILED` | 添加事件失败 | 检查 `logbook.events` 表权限 |
| `ATTACH_FAILED` | 添加附件失败 | 检查 `logbook.attachments` 表权限 |
| `RENDER_VIEWS_FAILED` | 渲染视图失败 | 检查 item 是否存在及输出目录权限 |

### 权限验证（engram-migrate --verify）

权限验证脚本用于验证角色和权限配置是否正确，对应 `sql/verify/99_verify_permissions.sql`。

> **注意**: 验证脚本位于 `sql/verify/` 子目录，不被 PostgreSQL initdb 自动执行。这确保数据库初始化不会因验证脚本的依赖（如 LOGIN 角色可能尚未创建）而失败。

**执行方式**：

```bash
# 方式 1: 通过 engram-migrate（推荐）
engram-migrate --verify

# 方式 2: 通过 Makefile（推荐）
make verify-permissions

# 方式 3: 通过 psql 直接执行（需要指定 schema）
# 注意：验证脚本位于 sql/verify/ 子目录
psql -d <your_db> -f sql/verify/99_verify_permissions.sql

# 方式 4: 通过 psql 指定 OpenMemory schema
psql -d <your_db> \
     -c "SET om.target_schema = 'openmemory'" \
     -f sql/verify/99_verify_permissions.sql
```

**所需权限**：

| 权限要求 | 说明 |
|----------|------|
| `SELECT` on `pg_roles` | 读取角色信息 |
| `SELECT` on `pg_default_acl` | 读取默认权限配置 |
| `SELECT` on `pg_namespace` | 读取 schema 信息 |
| `SELECT` on `information_schema.*` | 读取表/视图元数据 |

> **注意**：通常使用 `logbook_migrator` 或 `postgres` 超级用户执行验证。普通应用账号（如 `logbook_svc`）可能因权限不足而无法完整执行所有检查。

**`99_verify_permissions.sql` 核心验证项**：

| 验证项 | 说明 |
|--------|------|
| NOLOGIN 角色存在性 | `engram_*`、`openmemory_*` 角色 |
| LOGIN 角色 membership | 登录角色是否正确继承 NOLOGIN 角色 |
| public schema 权限 | 所有应用角色不应有 `CREATE` 权限 |
| 目标 schema owner | OM schema owner 应为 `openmemory_migrator` |
| 默认权限配置 | `pg_default_acl` 中的 TABLE/SEQUENCE 授权 |
| 数据库级权限硬化 | `PUBLIC` 不应有 `CREATE`/`TEMP` 权限 |

**输出级别**：

| 级别 | 说明 |
|------|------|
| `OK` | 检查通过（最终态） |
| `FAIL` | 严重问题，必须修复 |
| `WARN` | 潜在问题，可能影响安全 |
| `SKIP` | 条件不满足，跳过检查 |
| `COMPAT` | 兼容期警告（使用旧命名，需迁移） |

### 迁移计划预览（engram-migrate --plan）

使用 `--plan` 参数可在**不连接数据库**的情况下查看迁移计划，适用于 CI/CD 预检或部署前审查。

**执行方式**：

```bash
# 查看默认迁移计划
engram-migrate --plan

# 查看包含角色权限的迁移计划
engram-migrate --plan --apply-roles

# 查看完整迁移计划（包含验证脚本）
engram-migrate --plan --apply-roles --apply-openmemory-grants --verify

# 跳过配置预检（仅查看脚本列表）
engram-migrate --plan --no-precheck
```

**输出内容**：

| 字段 | 说明 |
|------|------|
| `ddl` | DDL 脚本列表（默认执行） |
| `permissions` | 权限脚本列表（需要 `--apply-roles` 或 `--apply-openmemory-grants`） |
| `verify` | 验证脚本列表（需要 `--verify`） |
| `execute` | 本次将执行的脚本列表 |
| `duplicates` | 同一前缀多文件的警告 |
| `precheck` | 配置预检结果（检查 OM_PG_SCHEMA 等） |
| `script_prefixes` | 脚本前缀分类配置（SSOT） |

**使用场景**：

| 场景 | 命令 |
|------|------|
| CI 预检 | `engram-migrate --plan --quiet` |
| 部署前审查 | `engram-migrate --plan --pretty` |
| 脚本清单导出 | `engram-migrate --plan \| jq '.execute'` |

> **详细文档**：参见 [SQL 文件清单](sql_file_inventory.md#411-迁移计划模式---plan)

### 验收命令快速参考

| 命令 | 说明 | 适用场景 |
|------|------|----------|
| `make acceptance-logbook-only` | **一键完整验收** | CI/CD、发布前验收 |
| `make up-logbook` | 启动 Logbook 服务 | 首次部署 |
| `make migrate-ddl` | DDL 迁移（Schema/表/索引） | 仅 DDL 变更 |
| `make apply-roles` | 应用 Logbook 角色和权限 | 角色权限变更 |
| `make apply-openmemory-grants` | 应用 OpenMemory 权限 | OM 权限变更 |
| `make verify-permissions` | 权限验证 | 权限检查 |
| `engram-migrate --plan` | 查看迁移计划（不连接数据库） | CI 预检、部署审查 |
| `make ps-logbook` | 查看服务状态 | 状态检查 |
| `engram-logbook health` | CLI 健康检查 | 功能验证 |
| `make logbook-smoke` | 冒烟测试 | 快速验证 |
| `make test-logbook-unit` | 单元测试 | 代码质量 |
| `make logs-logbook` | 查看日志 | 调试排错 |
| `make down-logbook` | 停止服务 | 清理环境 |

### Logbook 独立验收（推荐）

```bash
# 完整验收（启动服务 → 迁移 → 权限验证 → 冒烟测试 → 单元测试）
make acceptance-logbook-only

# 复用已有 PostgreSQL（跳过服务启动）
make acceptance-logbook-only SKIP_DEPLOY=1

# 跳过迁移（Schema 已存在）
make acceptance-logbook-only SKIP_DEPLOY=1 SKIP_MIGRATE=1
```

**产出文件**（位于 `.artifacts/acceptance-logbook-only/`）:

| 文件 | 说明 |
|------|------|
| `summary.json` | 验收摘要（PASS/FAIL、时间、命令列表） |
| `steps.log` | 执行步骤详细日志 |
| `health.json` | 健康检查 JSON 输出 |

**summary.json 示例**:

```json
{
  "result": "PASS",
  "timestamp": "2026-01-30T10:00:00Z",
  "duration_seconds": 45,
  "failed_step": "",
  "steps": {
    "up-logbook": "PASS",
    "migrate-ddl": "PASS",
    "apply-roles": "PASS",
    "verify-permissions": "PASS",
    "logbook-smoke": "PASS",
    "test-logbook-unit": "PASS"
  },
  "commands": ["up-logbook", "migrate-ddl", "apply-roles", "verify-permissions", "logbook-smoke", "test-logbook-unit"]
}
```

---

## 最小验收序列

以下是部署后的最小验收步骤，确保 Logbook 正常运行。

### 步骤 1：启动服务

```bash
make up-logbook
```

### 步骤 2：检查容器状态

```bash
make ps-logbook
# 或
docker compose -p ${PROJECT_KEY:-engram}-logbook -f compose/logbook.yml ps
```

**成功标准**：`postgres` 状态显示 `running (healthy)`

### 步骤 3：CLI 健康检查

```bash
pip install -e .
export POSTGRES_DSN="postgresql://postgres:${POSTGRES_PASSWORD:-postgres}@localhost:5432/${POSTGRES_DB:-engram}"
engram-logbook health
```

**成功标准**：输出 JSON 包含 `"ok": true`，所有检查项 `status = "ok"`

```json
{
  "ok": true,
  "checks": {
    "connection": {"status": "ok", "message": "数据库连接正常"},
    "schemas": {"status": "ok", "details": {
      "identity": {"exists": true},
      "logbook": {"exists": true},
      "scm": {"exists": true},
      "analysis": {"exists": true},
      "governance": {"exists": true}
    }},
    "tables": {"status": "ok", "total": 21, "existing": 21, "missing": []},
    "matviews": {"status": "ok", "missing": []},
    "indexes": {"status": "ok", "missing": []}
  }
}
```

**Schema 列表说明**：健康检查验证的 5 个 schema 是固定的架构约束：

| Schema | 业务域 |
|--------|--------|
| `identity` | 用户身份与账号 |
| `logbook` | 事件日志与附件 |
| `scm` | 代码仓库同步 |
| `analysis` | 分析运行记录 |
| `governance` | 项目治理设置 |

### 步骤 4：冒烟测试

```bash
make logbook-smoke
```

**成功标准**：输出包含以下内容

```
[OK] engram_logbook 已安装
[OK] 健康检查通过
[OK] 创建 item 成功: item_id=1
[OK] 添加事件成功
[OK] 添加附件成功
[OK] 渲染视图成功
Logbook 冒烟测试完成！
{"ok":true,"item_id":1,"message":"Logbook 冒烟测试通过"}
```

### 一键验收（复制粘贴）

```bash
# 完整的最小验收序列（可直接复制执行）
make up-logbook && \
make ps-logbook && \
pip install -e . && \
POSTGRES_DSN="postgresql://postgres:${POSTGRES_PASSWORD:-postgres}@localhost:5432/${POSTGRES_DB:-engram}" engram-logbook health && \
make logbook-smoke
```

**全部通过标志**：最后一行输出 `{"ok":true,...,"message":"Logbook 冒烟测试通过"}`

> **更多信息**：参见 [根 README 快速开始](../../README.md#logbook-only事实账本)

---

## 部署入口职责边界

Engram 提供多个部署入口，各有不同的职责和适用场景。

### 入口对比表

| 入口 | 职责 | 适用场景 | 部署模式 |
|------|------|----------|----------|
| `docker-compose.unified.yml` | 完整栈编排（PostgreSQL + 迁移 + Gateway + OpenMemory） | 生产部署、unified-stack 模式 | 强制要求 4 个密码 |
| `compose/logbook.yml` | 最小栈编排（PostgreSQL + 迁移） | 开发测试、logbook-only 模式 | 支持 SKIP 模式 |
| `Makefile` | 本地命令入口（调用 Python CLI） | 开发、CI、手动运维 | 两种模式均支持 |
| `engram-migrate` | DDL/权限迁移执行器 | 被上述入口调用 | 无关模式 |

### 各入口职责详解

#### docker-compose.unified.yml

**用途**：生产级完整栈部署

**包含服务**：
- `postgres`：数据库实例（挂载 `sql/` 用于 initdb）
- `bootstrap_roles`：创建服务账号（`--require-roles` 强制）
- `logbook_migrate`：执行 DDL + 权限迁移
- `permissions_verify`：执行权限验证
- `openmemory_migrate`：OpenMemory schema 迁移
- `openmemory`：OpenMemory 服务
- `gateway`：Engram Gateway 服务
- `worker`：Outbox worker

**启动命令**：
```bash
# 必须先设置全部 4 个密码环境变量
export LOGBOOK_MIGRATOR_PASSWORD=xxx
export LOGBOOK_SVC_PASSWORD=xxx
export OPENMEMORY_MIGRATOR_PASSWORD=xxx
export OPENMEMORY_SVC_PASSWORD=xxx

docker compose -f docker-compose.unified.yml up -d
```

**执行顺序**：
1. `postgres` 健康 → 2. `bootstrap_roles` 完成 → 3. `logbook_migrate` 完成 → 4. `permissions_verify` 完成 → 5. 应用服务启动

#### compose/logbook.yml

**用途**：最小化 logbook-only 部署

**包含服务**：
- `postgres`：数据库实例（挂载 `../sql/` 用于 initdb）
- `logbook_migrate`：执行 DDL + 权限迁移

**服务账号策略（SKIP 模式）**：

| 密码设置情况 | 模式 | 行为 |
|-------------|------|------|
| 全部不设置（0/4） | logbook-only | 跳过服务账号创建，使用 postgres 超级用户 |
| 全部设置（4/4） | unified-stack | 创建独立服务账号 |
| 部分设置（1-3/4） | **错误** | 容器初始化失败 |

**启动命令**：
```bash
# logbook-only 模式（无需密码）
docker compose -f compose/logbook.yml up -d

# 或 unified-stack 模式（需要全部密码）
export LOGBOOK_MIGRATOR_PASSWORD=xxx
# ... 其他密码
docker compose -f compose/logbook.yml up -d
```

#### Makefile

**用途**：本地开发和 CI 的命令入口

**核心目标**：

| 目标 | 说明 | 对应 CLI 参数 |
|------|------|--------------|
| `setup-db` | 一键初始化（创建库 + 角色 + DDL + 权限 + 验证） | 组合多个目标 |
| `setup-db-logbook-only` | logbook-only 初始化（跳过服务账号） | 组合多个目标 |
| `migrate-ddl` | 仅执行 DDL 迁移 | `engram-migrate --dsn ...` |
| `apply-roles` | 应用角色权限 | `engram-migrate --apply-roles` |
| `apply-openmemory-grants` | 应用 OM 权限 | `engram-migrate --apply-openmemory-grants` |
| `verify-permissions` | 验证权限 | `engram-migrate --verify` |
| `verify-permissions-strict` | 严格验证权限 | `engram-migrate --verify --verify-strict` |
| `bootstrap-roles` | 初始化服务账号 | `engram-bootstrap-roles` |

**CI 集成**：
```bash
# CI 中的典型用法
make ci                    # 运行所有静态检查
make migrate-ddl          # 执行迁移
make verify-permissions-strict  # 严格模式验证
```

#### engram-migrate（Python CLI）

**用途**：实际执行迁移和验证的核心工具

**命令参数**：

| 参数 | 说明 | 执行的 SQL 文件 |
|------|------|----------------|
| （无参数） | 仅执行 DDL 迁移 | 01, 02, 03, 06-09, 11-13 |
| `--apply-roles` | 应用角色权限 | 04_roles_and_grants.sql |
| `--apply-openmemory-grants` | 应用 OM 权限 | 05_openmemory_roles_and_grants.sql |
| `--verify` | 执行权限验证 | verify/99_verify_permissions.sql |
| `--verify-strict` | 严格模式（配合 `--verify`） | 有 FAIL 或 WARN 时退出码非零（CI 门禁） |

**完整迁移示例**：
```bash
engram-migrate --dsn "$POSTGRES_DSN" \
    --apply-roles \
    --apply-openmemory-grants \
    --verify \
    --verify-strict
```

---

## 推荐部署流程

根据不同场景选择合适的部署流程。

### 场景 A：新库初始化

#### 方式 1：Docker Compose（推荐）

```bash
# logbook-only 模式
docker compose -f compose/logbook.yml up -d

# 或 unified-stack 模式
export LOGBOOK_MIGRATOR_PASSWORD=xxx
export LOGBOOK_SVC_PASSWORD=xxx
export OPENMEMORY_MIGRATOR_PASSWORD=xxx
export OPENMEMORY_SVC_PASSWORD=xxx
docker compose -f docker-compose.unified.yml up -d
```

**特点**：自动按顺序执行 initdb → bootstrap → migrate → verify

#### 方式 2：Makefile（本地开发）

```bash
# 完整初始化
make setup-db

# 或 logbook-only 模式
make setup-db-logbook-only
```

**特点**：需要本地 PostgreSQL 实例或连接信息

### 场景 B：已有库升级

```bash
# 1. 执行 DDL 迁移（幂等）
make migrate-ddl

# 2. 应用角色权限（如有变更）
make apply-roles
make apply-openmemory-grants

# 3. 验证权限
make verify-permissions
```

**注意**：
- 所有 SQL 脚本设计为幂等，可安全重复执行
- 迁移失败可直接重试，无需回滚

### 场景 C：权限变更后验证

```bash
# 标准验证（查看结果）
make verify-permissions

# 严格验证（CI 门禁）
make verify-permissions-strict
```

### 流程总结表

| 场景 | Compose 命令 | Makefile 命令 | CLI 命令 |
|------|-------------|---------------|----------|
| 新库初始化 | `docker compose up -d` | `make setup-db` | 组合多个 CLI 命令 |
| DDL 升级 | 重启 migrate 容器 | `make migrate-ddl` | `engram-migrate --dsn ...` |
| 权限变更 | 重启 migrate 容器 | `make apply-roles` | `engram-migrate --apply-roles` |
| 权限验证 | 重启 verify 容器 | `make verify-permissions` | `engram-migrate --verify` |
| CI 验证 | N/A | `make verify-permissions-strict` | `engram-migrate --verify --verify-strict` |

---

## Verify 输出判定标准

### 输出级别定义

`99_verify_permissions.sql` 输出以下级别：

| 级别 | 含义 | 标准模式 | 严格模式 |
|------|------|----------|----------|
| `OK` | 检查通过 | 继续 | 继续 |
| `SKIP` | 条件不满足，跳过检查 | 继续 | 继续 |
| `COMPAT` | 使用旧命名（兼容期） | 继续 | 继续 |
| `WARN` | 潜在问题，可能影响安全 | 继续（仅提示） | **退出码非零** |
| `FAIL` | 严重问题，必须修复 | 继续（输出警告） | **退出码非零** |

### 判定逻辑

#### 标准模式（`--verify`）

```bash
engram-migrate --dsn "$POSTGRES_DSN" --verify
```

- 输出所有检查结果
- 退出码始终为 0（除非执行出错）
- 用于人工查看和诊断

#### 严格模式（`--verify --verify-strict`）

```bash
engram-migrate --dsn "$POSTGRES_DSN" --verify --verify-strict
```

- 输出所有检查结果
- **存在 `FAIL` 或 `WARN` 任一项时退出码非零**
- **用于 CI 门禁**，确保权限配置完全正确
- WARN 在标准模式下仅作为潜在问题提示，但在严格模式下视为必须修复的问题

### CI 集成示例

`.github/workflows/ci.yml` 中的配置：

```yaml
- name: Verify database migrations (strict mode)
  env:
    POSTGRES_DSN: postgresql://postgres:postgres@localhost:5432/engram_test
  run: |
    python -m engram.logbook.cli.db_migrate \
      --dsn "$POSTGRES_DSN" \
      --verify \
      --verify-strict \
      2>&1 | tee verify-output.log
    exit ${PIPESTATUS[0]}
```

**CI 门禁策略**：
1. 迁移完成后执行 `--verify --verify-strict`
2. 任何 `FAIL` 或 `WARN` 导致 CI 失败
3. 失败时上传 verify 日志供诊断

### 成功判定总结

| 模式 | 成功条件 | 退出码 | 典型用途 |
|------|----------|--------|----------|
| 标准模式 | 执行完成即成功 | 0 | 本地调试、查看状态 |
| 严格模式 | 无 FAIL **且** 无 WARN | 0 | CI 门禁、发布前检查 |
| 严格模式（失败） | 有 FAIL **或** 有 WARN | 非零 | 阻断 CI 流水线 |

> **设计原则**：严格模式将 WARN 升级为阻断条件，确保 CI 门禁不放过任何潜在的权限配置问题。

### 常见验证失败修复

| 输出示例 | 可能原因 | 修复方法 |
|----------|----------|----------|
| `FAIL: Role xxx not found` | 服务账号未创建 | `make bootstrap-roles` |
| `FAIL: Schema owner incorrect` | 迁移顺序错误 | 按顺序执行迁移 |
| `WARN: PUBLIC has CREATE on schema` | 权限未收紧 | `make apply-roles` |
| `SKIP: LOGIN role xxx not exist` | logbook-only 模式 | 预期行为，非错误 |

---

## 常见问题排错

### 1. 端口被占用

**症状**：

```
Error starting userland proxy: listen tcp 0.0.0.0:5432: bind: address already in use
```

**排查**：

```bash
lsof -i :5432
```

**解决方案**：

```bash
# 方案 1: 停止占用端口的服务
sudo kill -9 <PID>

# 方案 2: 使用其他端口
POSTGRES_PORT=5433 make up-logbook
```

### 2. 数据库连接失败

**症状**：

```json
{"ok": false, "code": "CONNECTION_ERROR", "message": "无法连接数据库"}
```

**排查步骤**：

```bash
# 1. 检查容器是否运行
make ps-logbook

# 2. 检查 PostgreSQL 日志
docker logs postgres

# 3. 测试连接
docker exec postgres pg_isready -U postgres
```

**解决方案**：

| 原因 | 解决方案 |
|------|----------|
| 容器未启动 | `make up-logbook` |
| DSN 配置错误 | 检查 `POSTGRES_DSN` 格式 |
| 密码错误 | 确认 `.env` 中的 `POSTGRES_PASSWORD` |
| 数据库不存在 | 确认 `POSTGRES_DB` 配置正确 |

**正确的 DSN 格式**：

```bash
# 环境变量方式
export POSTGRES_DSN="postgresql://postgres:changeme@localhost:5432/myproject"

# 配置文件方式 (.agentx/config.toml)
[postgres]
dsn = "postgresql://postgres:changeme@localhost:5432/myproject"
```

### 3. Schema 不存在

**症状**：

```
ERROR: schema "logbook" does not exist
```

**排查**：

```bash
# 检查迁移是否执行
docker logs logbook_migrate

# 手动执行 DDL 迁移
make migrate-ddl

# 应用角色权限（如需要）
make apply-roles
```

**解决方案**：

```bash
# 重新执行迁移
docker compose -p ${PROJECT_KEY:-engram}-logbook -f compose/logbook.yml up logbook_migrate
```

### 4. engram 安装失败

**症状**：

```
ModuleNotFoundError: No module named 'engram'
```

**解决方案**：

```bash
# 在项目根目录执行
pip install -e .
```

### 5. 健康检查超时

**症状**：

```
Container postgres is starting
Timeout waiting for container to become healthy
```

**排查**：

```bash
# 查看容器状态
docker inspect postgres | grep -A 10 "Health"

# 查看 PostgreSQL 日志
docker logs postgres --tail 50
```

**解决方案**：

| 原因 | 解决方案 |
|------|----------|
| 资源不足 | 增加 Docker 内存分配 |
| 初始化慢 | 增加 healthcheck 超时时间 |
| 数据目录权限 | 检查 volume 挂载权限 |

### 6. 迁移失败

**症状**：

```
Migration failed: relation "xxx" already exists
```

**解决方案**：

```bash
# 方案 1: 清理后重建
make down-logbook
docker volume rm ${PROJECT_KEY:-engram}-logbook_postgres_data
make up-logbook

# 方案 2: 手动修复（保留数据）
# 连接数据库检查具体问题
docker exec -it postgres psql -U postgres -d ${POSTGRES_DB}
```

---

## 日志与诊断

### 查看服务日志

```bash
# 所有服务日志
make logs-logbook

# 跟踪实时日志
docker compose -p ${PROJECT_KEY:-engram}-logbook -f compose/logbook.yml logs -f

# 最近 100 行
docker compose -p ${PROJECT_KEY:-engram}-logbook -f compose/logbook.yml logs --tail=100
```

### 连接数据库调试

```bash
# 进入 PostgreSQL 容器
docker exec -it postgres psql -U postgres -d ${POSTGRES_DB:-engram}

# 常用诊断 SQL
\dn                          -- 列出所有 schema
\dt logbook.*                -- 列出 logbook schema 下的表
SELECT * FROM logbook.items LIMIT 5;  -- 查看数据
```

### 导出诊断信息

```bash
# 收集诊断信息
mkdir -p .artifacts/diag
docker compose -p ${PROJECT_KEY:-engram}-logbook -f compose/logbook.yml ps > .artifacts/diag/ps.txt
docker logs postgres > .artifacts/diag/postgres.log 2>&1
make logbook-smoke > .artifacts/diag/smoke.log 2>&1 || true
```

---

## 清理与重置

### 停止服务

```bash
make down-logbook
```

### 完全清理（含数据）

```bash
make down-logbook
docker volume rm ${PROJECT_KEY:-engram}-logbook_postgres_data
```

### 清理 Python 缓存

```bash
make clean-logbook
```

---

## 验收标准

### `make logbook-smoke` 验收标准

冒烟测试是 Logbook 部署验收的核心环节，验证完整的 CRUD 工作流。

**执行步骤与验收点**：

| 步骤 | 验收点 | 成功标志 | 失败码 |
|------|--------|----------|--------|
| 0 | 依赖安装 | `[OK] engram_logbook 已安装` | `INSTALL_FAILED` |
| 1 | 服务状态 | `[OK] 服务已运行` | `SERVICE_NOT_RUNNING` |
| 2 | 健康检查 | `health` 命令返回 `ok: true` | `HEALTH_CHECK_FAILED` |
| 3 | 创建 Item | 返回有效 `item_id` | `CREATE_ITEM_FAILED` |
| 4 | 添加事件 | 返回有效 `event_id` | `ADD_EVENT_FAILED` |
| 5 | 添加附件 | 返回有效 `attachment_id` | `ATTACH_FAILED` |
| 6 | 渲染视图 | 生成 `manifest.csv` 和 `index.md` | `RENDER_VIEWS_FAILED` |

**最终成功输出**：

```json
{
  "ok": true,
  "code": "SMOKE_TEST_PASSED",
  "message": "Logbook 冒烟测试通过",
  "ids": {
    "item_id": 1,
    "event_id": 1,
    "attachment_id": 1
  }
}
```

**失败时的诊断**：

```bash
# 查看详细错误
cat .artifacts/logbook-smoke.json

# 检查数据库连接
engram-logbook health

# 检查服务状态
make ps-logbook
```

---

## 成功标准总结

部署成功后，以下条件应全部满足：

| 检查项 | 命令 | 成功标志 |
|--------|------|----------|
| PostgreSQL 容器 | `make ps-logbook` | 状态 `running (healthy)` |
| 数据库连接 | `engram-logbook health` | JSON 输出 `"ok": true` |
| Schema 完整性 | `engram-logbook health` | `schemas.status = "ok"` |
| 权限配置 | `make verify-permissions` | 无 `FAIL` 输出 |
| 冒烟测试 | `make logbook-smoke` | `code: "SMOKE_TEST_PASSED"` |

**快速验证命令**（全部成功才算部署完成）：

```bash
# 一行命令验证部署状态
make ps-logbook && \
POSTGRES_DSN="postgresql://postgres:${POSTGRES_PASSWORD:-postgres}@localhost:5432/${POSTGRES_DB:-engram}" \
  engram-logbook health --quiet && \
echo "✓ Logbook 部署成功"
```

---

## 相关文档

| 文档 | 说明 |
|------|------|
| [00_overview.md](00_overview.md) | Logbook 概览 |
| [01_architecture.md](01_architecture.md) | 架构设计 |
| [02_tools_contract.md](02_tools_contract.md) | 工具契约 |
| [sql_file_inventory.md](sql_file_inventory.md) | SQL 文件清单与执行顺序 |
| [环境变量参考](../reference/environment_variables.md) | 完整环境变量列表 |
| [根 README](../../README.md#logbook-only事实账本) | 快速开始指南 |
| [项目集成指南](../guides/integrate_existing_project.md) | 在已有项目中集成 Engram |

---

更新时间：2026-01-31（新增部署入口职责边界、推荐部署流程、Verify 输出判定标准）
