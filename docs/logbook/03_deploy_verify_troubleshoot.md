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
- 无法执行 `logbook health`、`logbook-smoke` 等 CLI 命令
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
| 健康检查 | `logbook health` | 验证连接、Schema、表、索引 |
| 数据库迁移 | `make migrate-logbook-stepwise` | Logbook-only 模式迁移（使用 --profile migrate） |
| 权限验证 | `make verify-permissions-logbook` | Logbook-only 模式权限验证（跳过 OM） |
| 冒烟测试 | `make logbook-smoke` | CRUD + 视图渲染全流程 |
| 完整验收 | `make acceptance-logbook-only` | 一键完整验收（启动→迁移→验证→测试） |

**验收命令对照**：

| 命令 | 依赖级别 | 说明 |
|------|----------|------|
| `docker compose up` + `pg_isready` | A (Baseline) | 最小验证 |
| `logbook health` | B (Acceptance) | CLI 健康检查 |
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

> **服务账号策略**：Logbook-only 部署时，**不要设置** `*_PASSWORD` 环境变量（如 `LOGBOOK_MIGRATOR_PASSWORD`）。脚本检测到无密码变量时自动进入 SKIP 模式，使用 postgres 超级用户。若设置了任意一个密码变量，则进入统一栈模式，要求 4 个密码变量全部设置，否则容器初始化失败。详见 `logbook_postgres/scripts/db_bootstrap.py` 与 `sql/04_roles_and_grants.sql`。

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
# 安装 Logbook CLI
cd logbook_postgres/scripts && pip install -e .

# 设置 DSN 并检查健康状态
export POSTGRES_DSN="postgresql://postgres:${POSTGRES_PASSWORD:-postgres}@localhost:5432/${POSTGRES_DB:-engram}"
logbook health
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

**`logbook health` 检查项说明**：

| 检查项 | 说明 | 来源 |
|--------|------|------|
| `connection` | 数据库连接状态（`SELECT 1`） | 运行时检测 |
| `schemas` | Schema 存在性检查 | **固定列表**：`identity`, `logbook`, `scm`, `analysis`, `governance` |
| `tables` | 核心表存在性检查（21 张） | 代码内置的 `core_tables` 列表 |
| `matviews` | 物化视图存在性检查 | `scm.v_facts` |
| `indexes` | 关键索引存在性检查 | 代码内置的 `required_indexes` 列表 |

> **Schema 列表来源**：`logbook_cli.py` 中 `cmd_health()` 函数的 `required_schemas` 变量，硬编码为 5 个固定 schema。这是 Logbook 路线 A（多库方案）的架构约束，每个 schema 对应一个业务域。

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
| `HEALTH_CHECK_FAILED` | 健康检查失败 | 检查 DSN 配置或运行 `make migrate-logbook-stepwise`（Logbook-only）或 `make migrate-logbook`（统一栈） |
| `CREATE_ITEM_FAILED` | 创建 item 失败 | 检查数据库连接和 schema 是否存在 |
| `ADD_EVENT_FAILED` | 添加事件失败 | 检查 `logbook.events` 表权限 |
| `ATTACH_FAILED` | 添加附件失败 | 检查 `logbook.attachments` 表权限 |
| `RENDER_VIEWS_FAILED` | 渲染视图失败 | 检查 item 是否存在及输出目录权限 |

### 权限验证（db_migrate.py --verify）

权限验证脚本用于验证角色和权限配置是否正确，对应 `sql/99_verify_permissions.sql`。

**执行方式**：

```bash
# 方式 1: 通过 db_migrate.py（推荐）
python logbook_postgres/scripts/db_migrate.py --verify

# 方式 2: 通过 Makefile（推荐）
make verify-permissions

# 方式 3: 通过 psql 直接执行（需要指定 schema）
psql -d <your_db> -f sql/99_verify_permissions.sql

# 方式 4: 通过 psql 指定 OpenMemory schema
psql -d <your_db> \
     -c "SET om.target_schema = 'openmemory'" \
     -f sql/99_verify_permissions.sql
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

### 验收命令快速参考

| 命令 | 说明 | 适用场景 |
|------|------|----------|
| `make acceptance-logbook-only` | **一键完整验收** | CI/CD、发布前验收 |
| `make up-logbook` | 启动 Logbook 服务 | 首次部署 |
| `make migrate-logbook-stepwise` | Logbook 独立迁移 | Logbook-only 模式 |
| `make ps-logbook` | 查看服务状态 | 状态检查 |
| `logbook health` | CLI 健康检查 | 功能验证 |
| `make logbook-smoke` | 冒烟测试 | 快速验证 |
| `make verify-permissions-logbook` | 权限验证（Logbook-only） | Logbook-only 权限检查 |
| `make verify-permissions` | 权限验证（统一栈） | 统一栈权限检查 |
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
    "migrate-logbook-stepwise": "PASS",
    "verify-permissions-logbook": "PASS",
    "logbook-smoke": "PASS",
    "test-logbook-unit": "PASS"
  },
  "commands": ["up-logbook", "migrate-logbook-stepwise", "verify-permissions-logbook", "logbook-smoke", "test-logbook-unit"]
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
cd logbook_postgres/scripts && pip install -e .
export POSTGRES_DSN="postgresql://postgres:${POSTGRES_PASSWORD:-postgres}@localhost:5432/${POSTGRES_DB:-engram}"
logbook health
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
cd logbook_postgres/scripts && pip install -e . && \
POSTGRES_DSN="postgresql://postgres:${POSTGRES_PASSWORD:-postgres}@localhost:5432/${POSTGRES_DB:-engram}" logbook health && \
cd - && \
make logbook-smoke
```

**全部通过标志**：最后一行输出 `{"ok":true,...,"message":"Logbook 冒烟测试通过"}`

> **更多信息**：参见 [根 README 快速开始](../../README.md#logbook-only事实账本)

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

# 手动执行迁移（Logbook-only 模式）
make migrate-logbook-stepwise

# 手动执行迁移（统一栈模式）
make migrate-logbook
```

**解决方案**：

```bash
# 重新执行迁移
docker compose -p ${PROJECT_KEY:-engram}-logbook -f compose/logbook.yml up logbook_migrate
```

### 4. engram_logbook 安装失败

**症状**：

```
ModuleNotFoundError: No module named 'engram_logbook'
```

**解决方案**：

```bash
cd logbook_postgres/scripts
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
logbook health

# 检查服务状态
make ps-logbook
```

---

## 成功标准总结

部署成功后，以下条件应全部满足：

| 检查项 | 命令 | 成功标志 |
|--------|------|----------|
| PostgreSQL 容器 | `make ps-logbook` | 状态 `running (healthy)` |
| 数据库连接 | `logbook health` | JSON 输出 `"ok": true` |
| Schema 完整性 | `logbook health` | `schemas.status = "ok"` |
| 权限配置（Logbook-only） | `make verify-permissions-logbook` | 无 `FAIL` 输出 |
| 权限配置（统一栈） | `make verify-permissions` | 无 `FAIL` 输出 |
| 冒烟测试 | `make logbook-smoke` | `code: "SMOKE_TEST_PASSED"` |

**快速验证命令**（全部成功才算部署完成）：

```bash
# 一行命令验证部署状态
make ps-logbook && \
POSTGRES_DSN="postgresql://postgres:${POSTGRES_PASSWORD:-postgres}@localhost:5432/${POSTGRES_DB:-engram}" \
  logbook health --quiet && \
echo "✓ Logbook 部署成功"
```

---

## 相关文档

| 文档 | 说明 |
|------|------|
| [00_overview.md](00_overview.md) | Logbook 概览 |
| [01_architecture.md](01_architecture.md) | 架构设计 |
| [02_tools_contract.md](02_tools_contract.md) | 工具契约 |
| [环境变量参考](../reference/environment_variables.md) | 完整环境变量列表 |
| [根 README](../../README.md#logbook-only事实账本) | 快速开始指南 |
| [项目集成指南](../guides/integrate_existing_project.md) | 在已有项目中集成 Engram |

---

更新时间：2026-01-30（补充 health 输出字段、db_migrate --verify 使用方式、logbook-smoke 验收标准）
