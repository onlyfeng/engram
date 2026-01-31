# Engram 安装指南

本文档介绍如何在本地环境安装和配置 Engram 及其依赖。

## 系统要求

- Python 3.10+
- PostgreSQL 18+（建议 18，Homebrew 默认版本较旧）
- Node.js（需 >=18，建议最新 LTS）
- OpenMemory 服务（Gateway 必需）

## 1. 安装 PostgreSQL

### Windows

1. 从 [PostgreSQL 官网](https://www.postgresql.org/download/windows/) 下载安装程序
2. 运行安装程序，选择安装 PostgreSQL 18+
3. 安装完成后，按 Windows 详细指南完成 pgvector 安装与服务托管  
   参考：[`docs/gateway/01_openmemory_deploy_windows.md`](gateway/01_openmemory_deploy_windows.md)

### macOS (使用 Homebrew)

```bash
# 安装 PostgreSQL
brew install postgresql@18

# 启动服务
brew services start postgresql@18

# 安装 pgvector 扩展
brew install pgvector

# 验证安装
psql -c "SELECT version();"
```

### Ubuntu/Debian

```bash
# 添加 PostgreSQL 官方仓库
sudo sh -c 'echo "deb http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list'
wget --quiet -O - https://www.postgresql.org/media/keys/ACCC4CF8.asc | sudo apt-key add -

# 安装 PostgreSQL
sudo apt-get update
sudo apt-get install postgresql-18

# 安装 pgvector
sudo apt-get install postgresql-18-pgvector

# 启动服务
sudo systemctl start postgresql
sudo systemctl enable postgresql
```

## 2. 初始化数据库与角色（推荐）

### 推荐流程

数据库初始化遵循三步流程：**bootstrap_roles → migrate → verify**

| 步骤 | 说明 | 需要权限 |
|------|------|----------|
| 1. bootstrap_roles | 创建服务账号（logbook_svc, openmemory_svc 等） | CREATEROLE 或 SUPERUSER |
| 2. migrate | 执行 SQL 迁移脚本，创建 schema/表/权限 | SUPERUSER（apply-roles 需要） |
| 3. verify | 验证所有权限配置正确 | 任意连接 |

### 本地 vs Docker Compose 命令对照

| 步骤 | 推荐命令 | Docker Compose 等价服务 |
|------|---------|------------------------|
| bootstrap_roles | `engram-bootstrap-roles --dsn ...` | `bootstrap_roles` service |
| migrate | `engram-migrate --dsn ... --apply-roles --apply-openmemory-grants` | `logbook_migrate` service |
| verify | `engram-migrate --dsn ... --verify` | `permissions_verify` service |
| 一键完成 | `make setup-db` | `docker compose up -d` (自动按依赖顺序执行) |

### 本地手动初始化

```bash
# 创建数据库
createdb engram

# 连接数据库并启用 pgvector 扩展
psql -d engram -c "CREATE EXTENSION IF NOT EXISTS vector;"

# 配置服务账号密码（必填）
export LOGBOOK_MIGRATOR_PASSWORD=changeme1
export LOGBOOK_SVC_PASSWORD=changeme2
export OPENMEMORY_MIGRATOR_PASSWORD=changeme3
export OPENMEMORY_SVC_PASSWORD=changeme4

# Step 1: 初始化服务账号（需要 admin 权限）
# macOS 默认管理员通常是当前用户，Linux/Windows 常用 postgres
engram-bootstrap-roles \
  --dsn "postgresql://<admin_user>@localhost:5432/postgres"

# Step 2: 执行迁移与权限脚本（需要 admin 权限）
engram-migrate \
  --dsn "postgresql://<admin_user>@localhost:5432/engram" \
  --apply-roles --apply-openmemory-grants

# Step 3: 验证权限配置
engram-migrate \
  --dsn "postgresql://<admin_user>@localhost:5432/engram" \
  --verify
```

### 使用 Makefile 一键初始化（推荐）

```bash
# 设置密码环境变量后，一键完成上述所有步骤
make setup-db
```

### 脚本选择指南

| 场景 | 推荐工具 | 说明 |
|------|---------|------|
| 本地开发 | `make setup-db` | 一键完成，无需记忆参数 |
| CI/CD 部署 | `engram-migrate` | 已安装的 CLI，支持所有选项 |
| Docker 部署 | docker-compose services | 自动按依赖顺序执行 |
| 仅迁移（无角色） | `engram-migrate --dsn ...` | 适用于已有服务账号的场景 |

> **入口策略说明**: 
> - `pyproject.toml [project.scripts]` + `python -m engram.*` 为**权威入口**
> - 根目录与 `logbook_postgres/scripts/` 的脚本均为**薄包装器，已弃用**
>
> 推荐使用:
> - Bootstrap: `engram-bootstrap-roles`
> - 迁移: `engram-migrate`
> - Logbook CLI: `engram-logbook`
> - SCM Sync: `engram-scm-scheduler`, `engram-scm-worker`, `engram-scm-reaper`, `engram-scm-status`, `engram-scm run`
> - Artifacts: `engram-artifacts`
>
> **弃用说明**: `python scripts/db_bootstrap.py`、`python scm_sync_*.py`、`python artifact_cli.py` 等脚本已弃用，将在 v1.0 移除。

## 3. 安装 Engram

### 基础安装（仅 Logbook）

```bash
pip install engram
# 或从源码安装
pip install -e .
```

### 完整安装（包含 Gateway）

```bash
pip install engram[full]
# 或从源码安装
pip install -e ".[full]"
```

### 开发环境安装

```bash
pip install engram[full,dev]
# 或从源码安装
pip install -e ".[full,dev]"
```

### 使用 Makefile 安装（推荐）

项目提供 Makefile 简化开发流程：

```bash
make install       # 安装核心依赖
make install-full  # 安装完整依赖（包含 Gateway 和 SCM）
make install-dev   # 安装开发依赖（推荐）
```

## 4. 数据库迁移（手动或开发场景）

```bash
# 使用统一迁移入口 engram-migrate（推荐）
export POSTGRES_DSN="postgresql://postgres@localhost:5432/engram"
engram-migrate --dsn "$POSTGRES_DSN"

# 或使用 python -m 方式调用（无需 pip install）
python -m engram.logbook.cli.db_migrate --dsn "$POSTGRES_DSN"

# 或使用 Makefile（开发场景，内部调用 engram-migrate）
POSTGRES_DSN="$POSTGRES_DSN" make migrate
```

## 4.1 统一栈 Docker Compose 快速开始（推荐）

统一栈包含 Postgres + OpenMemory + Gateway + Worker，适合快速落地与联调。
配置模板见 [`.env.example`](../.env.example)，编排入口见 [`docker-compose.unified.yml`](../docker-compose.unified.yml)。

### Docker Compose 初始化流程

Docker Compose 启动时自动按依赖顺序执行：

```
postgres (健康检查)
    ↓
bootstrap_roles (创建服务账号)
    ↓
logbook_migrate (执行迁移 + 权限)
    ↓
permissions_verify (验证权限)
    ↓
openmemory / gateway / worker (应用服务)
```

### 快速开始步骤

1) 复制并编辑环境变量：
```bash
# 在仓库根目录执行
cp .env.example .env

# 必填密码（统一栈强制要求）
# LOGBOOK_MIGRATOR_PASSWORD / LOGBOOK_SVC_PASSWORD
# OPENMEMORY_MIGRATOR_PASSWORD / OPENMEMORY_SVC_PASSWORD
```

2) 启动统一栈：
```bash
docker compose -f docker-compose.unified.yml up -d --build
```

3) 可选启用 profile：
```bash
# 管理看板（metabase/pgadmin）
docker compose -f docker-compose.unified.yml --profile dashboard up -d

# MinIO
docker compose -f docker-compose.unified.yml --profile minio up -d
```

4) 验证：
```bash
make verify-unified
```

### 本地 vs Docker Compose 命令对照

| 步骤 | 本地 Makefile | Docker Compose 服务 |
|------|--------------|-------------------|
| 一键初始化 | `make setup-db` | `docker compose up -d` (自动执行) |
| 仅 bootstrap | `make bootstrap-roles` | `bootstrap_roles` service |
| 仅迁移 | `make migrate` | `logbook_migrate` service |
| 仅验证 | `make verify` | `permissions_verify` service |
| 启动 Gateway | `make gateway` | `gateway` service |

5) 安全与备份建议：
- 最小安全清单：[`docs/guides/security_minimal.md`](guides/security_minimal.md)
- Docker 备份脚本：[`scripts/ops/backup/docker/README.md`](../scripts/ops/backup/docker/README.md)

## 5. 安装 OpenMemory（Gateway 必需）

OpenMemory 是独立的语义记忆服务，Engram 通过 HTTP API 与其通信。

### 使用 Node.js 后端（推荐）

1) 获取 OpenMemory 后端源码（按上游 README）  
2) 配置环境变量（示例）：
```bash
export OM_METADATA_BACKEND=postgres
export OM_PG_HOST=localhost
export OM_PG_PORT=5432
export OM_PG_DB=engram
export OM_PG_USER=openmemory_svc
export OM_PG_PASSWORD=<your_openmemory_svc_password>
export OM_PG_SCHEMA=openmemory
export OM_API_KEY=<your_api_key>
export OM_PORT=8080
```
3) 启动服务（以上游 README 为准）：
```bash
npm install
npm run dev
```

### 验证 OpenMemory 连接

```bash
curl http://localhost:8080/health
```

## 6. 配置

### 环境变量配置

创建 `.env` 文件或设置环境变量：

```bash
# PostgreSQL 连接（必填）
export POSTGRES_DSN="postgresql://logbook_svc:password@localhost:5432/engram"

# 项目标识
export PROJECT_KEY="my_project"

# OpenMemory 服务（Gateway 必填）
export OPENMEMORY_BASE_URL="http://localhost:8080"
export OM_API_KEY="your-api-key"  # 推荐

# Gateway 端口
export GATEWAY_PORT=8787
```

### 配置文件（可选）

创建 `~/.agentx/config.toml`：

```toml
[postgres]
dsn = "postgresql://logbook_svc:password@localhost:5432/engram"

[project]
project_key = "my_project"
description = "我的项目"

[openmemory]
base_url = "http://localhost:8080"
# api_key = "your-api-key"

[logging]
level = "INFO"
```

## 7. 验证安装

### 测试 Logbook

```bash
# 使用 CLI
engram-logbook health --dsn "$POSTGRES_DSN"

# 或使用 Python
python -c "
from engram.logbook import Database, Config
config = Config.from_env()
db = Database(config.postgres_dsn)
print('连接成功！')
"
```

### 启动 Gateway

```bash
# 使用 make
make gateway

# 或直接启动
engram-gateway

# 或使用 uvicorn
uvicorn engram.gateway.main:app --host 0.0.0.0 --port 8787
```

### 测试 Gateway

```bash
curl http://localhost:8787/health
```

## 8. MCP 集成（Cursor IDE）

在 Cursor 的 MCP 配置中添加 Gateway：

```json
{
  "mcpServers": {
    "engram": {
      "type": "http",
      "url": "http://localhost:8787/mcp"
    }
  }
}
```

## Makefile 参考

项目提供 Makefile 作为本地开发的统一入口，所有开发任务都可以通过 `make` 命令完成。

### 查看帮助

```bash
make help
```

### 完整命令列表

| 命令 | 说明 |
|------|------|
| `make install` | 安装核心依赖 |
| `make install-full` | 安装完整依赖（包含 Gateway 和 SCM） |
| `make install-dev` | 安装开发依赖 |
| `make test` | 运行所有测试 |
| `make test-logbook` | 仅运行 Logbook 测试 |
| `make test-gateway` | 仅运行 Gateway 测试 |
| `make test-cov` | 运行测试并生成覆盖率报告 |
| `make lint` | 代码检查 (ruff) |
| `make format` | 代码格式化 (ruff) |
| `make typecheck` | 类型检查 (mypy) |
| `make setup-db` | **一键初始化数据库（推荐）** |
| `make bootstrap-roles` | 仅初始化服务账号 |
| `make migrate` | 执行 SQL 迁移脚本 |
| `make verify` | 验证数据库权限配置 |
| `make db-create` | 创建数据库 |
| `make db-drop` | 删除数据库（危险操作） |
| `make gateway` | 启动 Gateway 服务（带热重载） |
| `make clean` | 清理临时文件 |

### 环境变量

Makefile 支持以下环境变量（可覆盖默认值）：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `POSTGRES_DSN` | `postgresql://postgres:postgres@localhost:5432/engram` | PostgreSQL 连接字符串 |
| `POSTGRES_USER` | `postgres` | PostgreSQL 用户名 |
| `POSTGRES_DB` | `engram` | 数据库名称 |
| `GATEWAY_PORT` | `8787` | Gateway 服务端口 |
| `OPENMEMORY_BASE_URL` | `http://localhost:8080` | OpenMemory 服务地址 |

### 使用示例

```bash
# 自定义数据库连接执行迁移
POSTGRES_DSN="postgresql://myuser:mypass@localhost:5432/mydb" make migrate

# 自定义端口启动 Gateway
GATEWAY_PORT=9000 make gateway

# 完整开发流程示例（推荐）
make install-dev     # 1. 安装开发依赖
make setup-db        # 2. 一键初始化数据库（包含 bootstrap + migrate + verify）
make test            # 3. 运行测试
make gateway         # 4. 启动服务

# 分步执行（手动控制）
make install-dev     # 1. 安装开发依赖
make db-create       # 2. 创建数据库
make bootstrap-roles # 3. 初始化服务账号
make migrate         # 4. 执行迁移
make verify          # 5. 验证权限
make gateway         # 6. 启动服务
```

### Makefile vs CLI 工具

项目同时提供 Makefile 命令和 CLI 工具，两者功能等价但适用场景不同：

| 场景 | 推荐方式 | 说明 |
|------|---------|------|
| 本地开发 | `make xxx` | 统一入口，无需记忆参数 |
| CI/CD | `engram-xxx` | 已安装的 CLI 命令 |
| 生产部署 | `engram-xxx` | 不依赖 Makefile |

## macOS 本地详细部署

> 以下步骤默认使用 PostgreSQL 18。请在不同终端执行带有“新终端”的步骤。

```bash
# Step 1: 安装依赖 + 启动 PostgreSQL 18
brew install postgresql@18 pgvector node
brew services start postgresql@18
export PATH="$(brew --prefix postgresql@18)/bin:$PATH"

# Step 2: 创建数据库 + 启用 pgvector
createdb engram
psql -d engram -c "CREATE EXTENSION IF NOT EXISTS vector;"

# Step 3: Python 环境与 Engram 安装
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[full]"

# Step 4: 初始化服务账号与迁移（需要管理员 DSN）
# 注意: 必须设置这 4 个密码环境变量，否则 bootstrap 会失败
export LOGBOOK_MIGRATOR_PASSWORD=changeme1
export LOGBOOK_SVC_PASSWORD=changeme2
export OPENMEMORY_MIGRATOR_PASSWORD=changeme3
export OPENMEMORY_SVC_PASSWORD=changeme4
export OM_PG_SCHEMA=openmemory

# Step 4.1: bootstrap_roles - 创建服务账号
engram-bootstrap-roles \
  --dsn "postgresql://$USER@localhost:5432/postgres"

# Step 4.2: migrate - 执行迁移脚本
engram-migrate \
  --dsn "postgresql://$USER@localhost:5432/engram" \
  --apply-roles --apply-openmemory-grants

# Step 4.3: verify - 验证权限配置
engram-migrate \
  --dsn "postgresql://$USER@localhost:5432/engram" \
  --verify

# 补充授权（OpenMemory 运行时需要完整权限）
psql -d engram -c "
GRANT ALL PRIVILEGES ON SCHEMA openmemory TO openmemory_svc;
ALTER DEFAULT PRIVILEGES IN SCHEMA openmemory GRANT ALL ON TABLES TO openmemory_svc;
ALTER DEFAULT PRIVILEGES IN SCHEMA openmemory GRANT ALL ON SEQUENCES TO openmemory_svc;
"

# Step 5: 安装并启动 OpenMemory（新终端）
# OpenMemory 是独立的经验记忆引擎，Engram Gateway 通过 HTTP API 与其通信
# 参考: https://github.com/CaviraOSS/OpenMemory
#
# 注意: Engram 需要 OpenMemory 的 HTTP API 服务（端口 8080），
#       不能只用 Python SDK（pip install openmemory-py）的嵌入式模式

git clone https://github.com/caviraoss/openmemory.git ~/openmemory
cd ~/openmemory

# 配置环境变量（连接到 Step 4 创建的 PostgreSQL）
export OM_METADATA_BACKEND=postgres
export OM_PG_HOST=localhost
export OM_PG_PORT=5432
export OM_PG_DB=engram
export OM_PG_USER=openmemory_svc
export OM_PG_PASSWORD=$OPENMEMORY_SVC_PASSWORD
export OM_PG_SCHEMA=openmemory
export OM_API_KEY=change_me
export OM_PORT=8080
export OM_VEC_DIM=1536          # vector 维度，需与 pgvector 列定义一致
export OM_TIER=hybrid           # 可选: hybrid/fast/smart/deep

# 构建并安装 opm CLI
cd packages/openmemory-js
npm install
npm run build
npm link   # 将 opm 添加到 PATH

# 首次启动前：修复 pgvector 列维度（PostgreSQL 18 必需）
psql -d engram -c "ALTER TABLE openmemory.openmemory_vectors ALTER COLUMN v TYPE vector(1536);" 2>/dev/null || true

# 启动 API 服务
opm serve
# 服务将在 http://localhost:8080 启动

# Step 6: 启动 Gateway（新终端）
cd /Users/a4399/Documents/ai/onlyfeng/engram
source .venv/bin/activate
export PROJECT_KEY=default
export POSTGRES_DSN="postgresql://logbook_svc:$LOGBOOK_SVC_PASSWORD@localhost:5432/engram"
export OPENMEMORY_BASE_URL="http://localhost:8080"
export OM_API_KEY=change_me
engram-gateway

# Step 7: 启动 Outbox Worker + 验证（新终端）
cd /Users/a4399/Documents/ai/onlyfeng/engram
source .venv/bin/activate
export PROJECT_KEY=default
export POSTGRES_DSN="postgresql://logbook_svc:$LOGBOOK_SVC_PASSWORD@localhost:5432/engram"
export OPENMEMORY_BASE_URL="http://localhost:8080"
export OM_API_KEY=change_me
python -m engram.gateway.outbox_worker --loop

curl -sf http://localhost:8080/health && echo "OpenMemory OK"
curl -sf http://localhost:8787/health && echo "Gateway OK"
```

### 常见问题排查

<details>
<summary><b>db_bootstrap 报错 "服务账号创建失败"</b></summary>

确保设置了 4 个密码环境变量：
```bash
export LOGBOOK_MIGRATOR_PASSWORD=xxx
export LOGBOOK_SVC_PASSWORD=xxx
export OPENMEMORY_MIGRATOR_PASSWORD=xxx
export OPENMEMORY_SVC_PASSWORD=xxx
```
</details>

<details>
<summary><b>OpenMemory 报错 "permission denied for schema openmemory"</b></summary>

执行补充授权：
```bash
psql -d engram -c "
GRANT ALL PRIVILEGES ON SCHEMA openmemory TO openmemory_svc;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA openmemory TO openmemory_svc;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA openmemory TO openmemory_svc;
ALTER DEFAULT PRIVILEGES IN SCHEMA openmemory GRANT ALL ON TABLES TO openmemory_svc;
ALTER DEFAULT PRIVILEGES IN SCHEMA openmemory GRANT ALL ON SEQUENCES TO openmemory_svc;
"
```
</details>

<details>
<summary><b>OpenMemory 报错 "column does not have dimensions"（PostgreSQL 18）</b></summary>

pgvector HNSW 索引要求 vector 列必须指定维度：
```bash
psql -d engram -c "DROP INDEX IF EXISTS openmemory.openmemory_vectors_v_idx;"
psql -d engram -c "ALTER TABLE openmemory.openmemory_vectors ALTER COLUMN v TYPE vector(1536);"
```
然后重启 `opm serve`。
</details>

<details>
<summary><b>db_migrate 报错 "OPENMEMORY_SCHEMA_MISSING"</b></summary>

确保迁移时带 `--apply-openmemory-grants` 参数，并且 `05_openmemory_roles_and_grants.sql` 存在于 `sql/` 目录。
</details>

## macOS launchd 服务托管（可选）

以下示例使用用户级 LaunchAgents 持久化运行 Gateway / Outbox Worker / OpenMemory。  
请将路径替换为你本机的实际路径（可用 `which engram-gateway` / `which python` / `which npm` 查询）。

### 1) Gateway

```xml
<!-- ~/Library/LaunchAgents/ai.engram.gateway.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>ai.engram.gateway</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/engram-gateway</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PROJECT_KEY</key><string>default</string>
    <key>POSTGRES_DSN</key><string>postgresql://logbook_svc:***@localhost:5432/engram</string>
    <key>OPENMEMORY_BASE_URL</key><string>http://localhost:8080</string>
    <key>OM_API_KEY</key><string>change_me</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/your_user/Library/Logs/engram-gateway.log</string>
  <key>StandardErrorPath</key><string>/Users/your_user/Library/Logs/engram-gateway.err.log</string>
</dict>
</plist>
```

### 2) Outbox Worker

```xml
<!-- ~/Library/LaunchAgents/ai.engram.outbox_worker.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>ai.engram.outbox_worker</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/python</string>
    <string>-m</string>
    <string>engram.gateway.outbox_worker</string>
    <string>--loop</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PROJECT_KEY</key><string>default</string>
    <key>POSTGRES_DSN</key><string>postgresql://logbook_svc:***@localhost:5432/engram</string>
    <key>OPENMEMORY_BASE_URL</key><string>http://localhost:8080</string>
    <key>OM_API_KEY</key><string>change_me</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/your_user/Library/Logs/engram-outbox.log</string>
  <key>StandardErrorPath</key><string>/Users/your_user/Library/Logs/engram-outbox.err.log</string>
</dict>
</plist>
```

### 3) OpenMemory（Node 后端）

```xml
<!-- ~/Library/LaunchAgents/ai.openmemory.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>ai.openmemory</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/npm</string>
    <string>run</string>
    <string>start</string>
  </array>
  <key>WorkingDirectory</key><string>/path/to/openmemory/backend</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>OM_METADATA_BACKEND</key><string>postgres</string>
    <key>OM_PG_HOST</key><string>localhost</string>
    <key>OM_PG_PORT</key><string>5432</string>
    <key>OM_PG_DB</key><string>engram</string>
    <key>OM_PG_USER</key><string>openmemory_svc</string>
    <key>OM_PG_PASSWORD</key><string>***</string>
    <key>OM_PG_SCHEMA</key><string>openmemory</string>
    <key>OM_API_KEY</key><string>change_me</string>
    <key>OM_PORT</key><string>8080</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/your_user/Library/Logs/openmemory.log</string>
  <key>StandardErrorPath</key><string>/Users/your_user/Library/Logs/openmemory.err.log</string>
</dict>
</plist>
```

### 4) 启用与查看状态

```bash
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/ai.openmemory.plist
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/ai.engram.gateway.plist
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/ai.engram.outbox_worker.plist

launchctl kickstart -k gui/$UID/ai.openmemory
launchctl kickstart -k gui/$UID/ai.engram.gateway
launchctl kickstart -k gui/$UID/ai.engram.outbox_worker

launchctl print gui/$UID/ai.engram.gateway
```

## macOS launchd 生产安全版本（LaunchDaemons）

适用于：后台长期运行、分离用户权限、最小化权限与可控日志轮转。  
以下示例使用系统级 LaunchDaemons（`/Library/LaunchDaemons`），并通过独立用户运行服务。

### 1) 创建服务用户（示例）

```bash
# 选择未占用的 UID（示例使用 5020/5021/5022）
dscl . -list /Users UniqueID | tail

sudo dscl . -create /Users/engram_gateway
sudo dscl . -create /Users/engram_gateway UserShell /usr/bin/false
sudo dscl . -create /Users/engram_gateway NFSHomeDirectory /var/empty
sudo dscl . -create /Users/engram_gateway UniqueID 5020
sudo dscl . -create /Users/engram_gateway PrimaryGroupID 20
sudo dscl . -create /Users/engram_gateway Password "*"

sudo dscl . -create /Users/engram_outbox
sudo dscl . -create /Users/engram_outbox UserShell /usr/bin/false
sudo dscl . -create /Users/engram_outbox NFSHomeDirectory /var/empty
sudo dscl . -create /Users/engram_outbox UniqueID 5021
sudo dscl . -create /Users/engram_outbox PrimaryGroupID 20
sudo dscl . -create /Users/engram_outbox Password "*"

sudo dscl . -create /Users/openmemory
sudo dscl . -create /Users/openmemory UserShell /usr/bin/false
sudo dscl . -create /Users/openmemory NFSHomeDirectory /var/empty
sudo dscl . -create /Users/openmemory UniqueID 5022
sudo dscl . -create /Users/openmemory PrimaryGroupID 20
sudo dscl . -create /Users/openmemory Password "*"
```

### 2) 目录与权限

```bash
sudo mkdir -p /var/db/engram/gateway /var/db/engram/outbox /var/db/engram/openmemory
sudo mkdir -p /var/log/engram/gateway /var/log/engram/outbox /var/log/engram/openmemory

sudo chown -R engram_gateway:staff /var/db/engram/gateway /var/log/engram/gateway
sudo chown -R engram_outbox:staff /var/db/engram/outbox /var/log/engram/outbox
sudo chown -R openmemory:staff /var/db/engram/openmemory /var/log/engram/openmemory

sudo chmod 700 /var/db/engram/gateway /var/db/engram/outbox /var/db/engram/openmemory
sudo chmod 750 /var/log/engram/gateway /var/log/engram/outbox /var/log/engram/openmemory
```

### 3) 环境文件（仅服务用户可读）

```bash
sudo tee /var/db/engram/gateway/env <<'EOF'
PROJECT_KEY=default
POSTGRES_DSN=postgresql://logbook_svc:***@localhost:5432/engram
OPENMEMORY_BASE_URL=http://localhost:8080
OM_API_KEY=change_me
EOF
sudo chown engram_gateway:staff /var/db/engram/gateway/env
sudo chmod 600 /var/db/engram/gateway/env

sudo tee /var/db/engram/outbox/env <<'EOF'
PROJECT_KEY=default
POSTGRES_DSN=postgresql://logbook_svc:***@localhost:5432/engram
OPENMEMORY_BASE_URL=http://localhost:8080
OM_API_KEY=change_me
EOF
sudo chown engram_outbox:staff /var/db/engram/outbox/env
sudo chmod 600 /var/db/engram/outbox/env

sudo tee /var/db/engram/openmemory/env <<'EOF'
OM_METADATA_BACKEND=postgres
OM_PG_HOST=localhost
OM_PG_PORT=5432
OM_PG_DB=engram
OM_PG_USER=openmemory_svc
OM_PG_PASSWORD=***
OM_PG_SCHEMA=openmemory
OM_API_KEY=change_me
OM_PORT=8080
EOF
sudo chown openmemory:staff /var/db/engram/openmemory/env
sudo chmod 600 /var/db/engram/openmemory/env
```

### 4) Wrapper 脚本（隔离 env 与可执行路径）

```bash
sudo mkdir -p /usr/local/engram/bin

sudo tee /usr/local/engram/bin/engram-gateway.sh <<'EOF'
#!/bin/zsh
set -euo pipefail
source /var/db/engram/gateway/env
exec /path/to/engram-gateway
EOF

sudo tee /usr/local/engram/bin/engram-outbox.sh <<'EOF'
#!/bin/zsh
set -euo pipefail
source /var/db/engram/outbox/env
exec /path/to/python -m engram.gateway.outbox_worker --loop
EOF

sudo tee /usr/local/engram/bin/openmemory.sh <<'EOF'
#!/bin/zsh
set -euo pipefail
source /var/db/engram/openmemory/env
cd /path/to/openmemory/backend
exec /path/to/npm run start
EOF

sudo chmod 755 /usr/local/engram/bin/*.sh
sudo chown root:wheel /usr/local/engram/bin/*.sh
```

### 5) LaunchDaemon 配置（示例）

```xml
<!-- /Library/LaunchDaemons/ai.engram.gateway.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>ai.engram.gateway</string>
  <key>UserName</key><string>engram_gateway</string>
  <key>GroupName</key><string>staff</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/local/engram/bin/engram-gateway.sh</string>
  </array>
  <key>Umask</key><integer>77</integer>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/var/log/engram/gateway/gateway.log</string>
  <key>StandardErrorPath</key><string>/var/log/engram/gateway/gateway.err.log</string>
</dict>
</plist>
```

```xml
<!-- /Library/LaunchDaemons/ai.engram.outbox.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>ai.engram.outbox</string>
  <key>UserName</key><string>engram_outbox</string>
  <key>GroupName</key><string>staff</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/local/engram/bin/engram-outbox.sh</string>
  </array>
  <key>Umask</key><integer>77</integer>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/var/log/engram/outbox/outbox.log</string>
  <key>StandardErrorPath</key><string>/var/log/engram/outbox/outbox.err.log</string>
</dict>
</plist>
```

```xml
<!-- /Library/LaunchDaemons/ai.openmemory.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>ai.openmemory</string>
  <key>UserName</key><string>openmemory</string>
  <key>GroupName</key><string>staff</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/local/engram/bin/openmemory.sh</string>
  </array>
  <key>Umask</key><integer>77</integer>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/var/log/engram/openmemory/openmemory.log</string>
  <key>StandardErrorPath</key><string>/var/log/engram/openmemory/openmemory.err.log</string>
</dict>
</plist>
```

### 6) 启用与管理

```bash
sudo chown root:wheel /Library/LaunchDaemons/ai.*.plist
sudo chmod 644 /Library/LaunchDaemons/ai.*.plist

sudo launchctl bootstrap system /Library/LaunchDaemons/ai.openmemory.plist
sudo launchctl bootstrap system /Library/LaunchDaemons/ai.engram.gateway.plist
sudo launchctl bootstrap system /Library/LaunchDaemons/ai.engram.outbox.plist

sudo launchctl kickstart -k system/ai.openmemory
sudo launchctl kickstart -k system/ai.engram.gateway
sudo launchctl kickstart -k system/ai.engram.outbox

sudo launchctl print system/ai.engram.gateway
```

### 7) 日志轮转（newsyslog）

```bash
sudo tee /etc/newsyslog.d/engram.conf <<'EOF'
/var/log/engram/gateway/gateway.log    640  10  1000  *  -
/var/log/engram/gateway/gateway.err.log 640 10  1000  *  -
/var/log/engram/outbox/outbox.log      640  10  1000  *  -
/var/log/engram/outbox/outbox.err.log  640  10  1000  *  -
/var/log/engram/openmemory/openmemory.log     640  10  1000  *  -
/var/log/engram/openmemory/openmemory.err.log 640  10  1000  *  -
EOF

# 预检与手动触发
sudo newsyslog -n -f /etc/newsyslog.d/engram.conf
```

## 常见问题

### pgvector 安装失败

确保安装了正确版本的 PostgreSQL 开发头文件：

```bash
# macOS
brew install postgresql@18

# Ubuntu
sudo apt-get install postgresql-server-dev-18
```

### 连接被拒绝

检查 PostgreSQL 是否正在运行：

```bash
# macOS
brew services list | grep postgresql

# Linux
sudo systemctl status postgresql
```

### OpenMemory 连接超时

确保 OpenMemory 服务已启动并监听正确的端口：

```bash
curl -v http://localhost:8080/health
```

## 下一步

- 阅读 [集成指南](guides/integrate_existing_project.md) 了解如何集成到现有项目
- 查看 [Gateway 文档](gateway/00_overview.md) 了解 MCP 功能
- 查看 [Logbook 文档](logbook/00_overview.md) 了解事实账本功能
