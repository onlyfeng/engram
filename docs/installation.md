# Engram 安装指南

本文档介绍如何在本地环境安装和配置 Engram 及其依赖。

## 系统要求

- Python 3.10+
- PostgreSQL 15+ (带 pgvector 扩展)
- OpenMemory 服务 (可选，用于语义记忆功能)

## 1. 安装 PostgreSQL

### macOS (使用 Homebrew)

```bash
# 安装 PostgreSQL
brew install postgresql@15

# 启动服务
brew services start postgresql@15

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
sudo apt-get install postgresql-15

# 安装 pgvector
sudo apt-get install postgresql-15-pgvector

# 启动服务
sudo systemctl start postgresql
sudo systemctl enable postgresql
```

### Windows

1. 从 [PostgreSQL 官网](https://www.postgresql.org/download/windows/) 下载安装程序
2. 运行安装程序，选择安装 PostgreSQL 15+
3. 安装完成后，使用 Stack Builder 安装 pgvector 扩展

## 2. 配置数据库

```bash
# 创建数据库用户和数据库
createuser -U postgres engram_user
createdb -U postgres -O engram_user engram

# 连接数据库并启用 pgvector 扩展
psql -U postgres -d engram -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

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

## 4. 数据库迁移

运行 SQL 迁移脚本初始化数据库：

```bash
# 设置环境变量
export POSTGRES_DSN="postgresql://engram_user:password@localhost:5432/engram"

# 方式一：使用 make
make migrate

# 方式二：手动执行
for f in sql/*.sql; do
    psql "$POSTGRES_DSN" -f "$f"
done
```

## 5. 安装 OpenMemory（可选）

OpenMemory 是独立的语义记忆服务，Engram 通过 HTTP API 与其通信。

### 使用 pip 安装

```bash
# 安装 OpenMemory
pip install openmemory

# 启动服务
openmemory serve --port 8080
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
export POSTGRES_DSN="postgresql://engram_user:password@localhost:5432/engram"

# 项目标识
export PROJECT_KEY="my_project"

# OpenMemory 服务（使用 Gateway 时必填）
export OPENMEMORY_BASE_URL="http://localhost:8080"
export OPENMEMORY_API_KEY="your-api-key"  # 可选

# Gateway 端口
export GATEWAY_PORT=8787
```

### 配置文件（可选）

创建 `~/.agentx/config.toml`：

```toml
[postgres]
dsn = "postgresql://engram_user:password@localhost:5432/engram"

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
      "command": "engram-gateway",
      "args": [],
      "env": {
        "POSTGRES_DSN": "postgresql://engram_user:password@localhost:5432/engram",
        "OPENMEMORY_BASE_URL": "http://localhost:8080"
      }
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
| `make db-create` | 创建数据库 |
| `make migrate` | 执行 SQL 迁移脚本 |
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

# 完整开发流程示例
make install-dev     # 1. 安装开发依赖
make db-create       # 2. 创建数据库
make migrate         # 3. 执行迁移
make test            # 4. 运行测试
make gateway         # 5. 启动服务
```

### Makefile vs CLI 工具

项目同时提供 Makefile 命令和 CLI 工具，两者功能等价但适用场景不同：

| 场景 | 推荐方式 | 说明 |
|------|---------|------|
| 本地开发 | `make xxx` | 统一入口，无需记忆参数 |
| CI/CD | `engram-xxx` | 已安装的 CLI 命令 |
| 生产部署 | `engram-xxx` | 不依赖 Makefile |

## 常见问题

### pgvector 安装失败

确保安装了正确版本的 PostgreSQL 开发头文件：

```bash
# macOS
brew install postgresql@15

# Ubuntu
sudo apt-get install postgresql-server-dev-15
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
