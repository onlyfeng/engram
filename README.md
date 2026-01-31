# Engram

AI 友好的事实账本与记忆管理模块 - 为 AI Agent 提供可审计、可回放的证据链与可演化知识沉淀。

## 特性

- **Logbook（事实账本）**: 基于 PostgreSQL 的结构化事件日志，支持 SCM 同步、证据链追溯
- **Gateway（MCP 网关）**: 连接 Cursor IDE 与 OpenMemory，提供策略校验、审计落库、失败降级
- **本地优先**: 设计为 Python 库，方便集成到其他项目中
- **AI 友好**: 结构化 JSON 输出，易于 LLM 理解和处理

## 快速开始

### 安装

```bash
# 基础安装（仅 Logbook）
pip install engram

# 完整安装（包含 Gateway）
pip install engram[full]

# 从源码安装
git clone https://github.com/onlyfeng/engram.git
cd engram
pip install -e ".[full]"
```

### 配置 PostgreSQL

1. 安装 PostgreSQL 18+ 和 pgvector 扩展（Homebrew 默认版本较旧，建议显式指定）：

```bash
# macOS
brew install postgresql@18 pgvector

# Ubuntu
sudo apt install postgresql-18 postgresql-18-pgvector
```

2. 创建数据库：

```bash
createdb engram
psql -d engram -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

3. 初始化角色与迁移：

```bash
export LOGBOOK_MIGRATOR_PASSWORD=changeme1
export LOGBOOK_SVC_PASSWORD=changeme2
export OPENMEMORY_MIGRATOR_PASSWORD=changeme3
export OPENMEMORY_SVC_PASSWORD=changeme4

python logbook_postgres/scripts/db_bootstrap.py \
  --dsn "postgresql://$USER@localhost:5432/postgres"

python logbook_postgres/scripts/db_migrate.py \
  --dsn "postgresql://$USER@localhost:5432/engram" \
  --apply-roles --apply-openmemory-grants
```

### 基本使用

```python
from engram.logbook import Database, Config

# 初始化
config = Config.from_env()  # 或 Config.from_file("config.toml")
db = Database(config.postgres_dsn)

# 创建条目
item_id = db.create_item(
    item_type="task",
    title="My First Task",
    project_key="my_project"
)

# 添加事件
db.add_event(item_id, event_type="progress", payload={"status": "started"})

# 键值存储
db.set_kv("my_key", {"data": "value"})
value = db.get_kv("my_key")
```

### 启动 Gateway

```bash
# 设置环境变量
export POSTGRES_DSN="postgresql://logbook_svc:password@localhost:5432/engram"
export OPENMEMORY_BASE_URL="http://localhost:8080"  # OpenMemory 服务地址

# 启动 Gateway
engram-gateway

# 或使用 make
make gateway
```

## 项目结构

```
engram/
├── src/engram/              # 源代码
│   ├── logbook/             # Logbook 核心模块
│   │   ├── db.py            # 数据库操作
│   │   ├── config.py        # 配置管理
│   │   ├── errors.py        # 错误定义
│   │   ├── outbox.py        # Outbox 队列
│   │   └── cli/             # CLI 命令
│   └── gateway/             # Gateway 模块
│       ├── main.py          # FastAPI 入口
│       ├── mcp_rpc.py       # MCP 协议
│       ├── policy.py        # 策略引擎
│       └── openmemory_client.py  # OpenMemory 客户端
├── sql/                     # 数据库迁移脚本
├── tests/                   # 测试
├── docs/                    # 文档
├── pyproject.toml           # 项目配置
└── Makefile                 # 开发工具
```

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `POSTGRES_DSN` | PostgreSQL 连接字符串 | - |
| `PROJECT_KEY` | 项目标识 | `default` |
| `OPENMEMORY_BASE_URL` | OpenMemory 服务地址 | - |
| `OPENMEMORY_API_KEY` | OpenMemory API 密钥 | - |
| `GATEWAY_PORT` | Gateway 端口 | `8787` |

## CLI 命令

```bash
# Logbook CLI
engram-logbook create_item --type task --title "My Task"
engram-logbook add_event <item_id> --type progress --payload '{"status": "done"}'
engram-logbook health

# 数据库迁移
engram-migrate

# Gateway
engram-gateway
```

## MCP 集成（Cursor IDE）

在 Cursor 的 MCP 配置中添加（或直接参考 `configs/mcp/.mcp.json.example`）：

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

## 开发

### Makefile 命令

项目提供 Makefile 简化开发流程，运行 `make help` 查看所有可用命令：

```bash
# 查看帮助
make help
```

#### 安装

```bash
make install       # 安装核心依赖
make install-full  # 安装完整依赖（包含 Gateway 和 SCM）
make install-dev   # 安装开发依赖（推荐）
```

#### 测试

```bash
make test          # 运行所有测试
make test-logbook  # 仅运行 Logbook 测试
make test-gateway  # 仅运行 Gateway 测试
make test-cov      # 运行测试并生成覆盖率报告

# pytest 标记筛选
pytest -m unit                # 只跑单元测试
pytest -m integration         # 只跑集成测试
pytest -m "not integration"   # 排除集成测试
```

#### 代码质量

```bash
make lint          # 代码检查 (ruff)
make format        # 代码格式化 (ruff)
make typecheck     # 类型检查 (mypy)
```

#### 数据库

```bash
make db-create     # 创建数据库
make migrate       # 执行 SQL 迁移脚本
make db-drop       # 删除数据库（危险操作，需确认）
```

#### 服务

```bash
make gateway       # 启动 Gateway 服务（带热重载）
```

#### 清理

```bash
make clean         # 清理临时文件（__pycache__, .egg-info 等）
```

#### 环境变量覆盖

Makefile 支持通过环境变量覆盖默认配置：

```bash
# 自定义数据库连接
POSTGRES_DSN="postgresql://user:pass@host:5432/db" make migrate

# 自定义 Gateway 端口
GATEWAY_PORT=9000 make gateway
```

## 架构

```
┌─────────────────────────────────────────────────────────┐
│  Cursor IDE / MCP Client                                │
└────────────────────────┬────────────────────────────────┘
                         │ MCP JSON-RPC
┌────────────────────────▼────────────────────────────────┐
│  Gateway (engram.gateway)                               │
│  - 策略校验                                             │
│  - 写入审计                                             │
│  - 失败降级                                             │
└────────────────┬──────────────────┬─────────────────────┘
                 │                  │
    ┌────────────▼──────┐   ┌──────▼────────────┐
    │  Logbook          │   │  OpenMemory       │
    │  (PostgreSQL)     │   │  (外部服务)       │
    │  - 事实账本       │   │  - 语义记忆       │
    │  - 治理设置       │   │  - 向量检索       │
    │  - Outbox 队列    │   │                   │
    └───────────────────┘   └───────────────────┘
```

## 文档

- [安装指南](docs/installation.md)
- [Logbook 文档](docs/logbook/)
- [Gateway 文档](docs/gateway/)
- [环境变量参考](docs/reference/environment_variables.md)

## 许可证

MIT License
