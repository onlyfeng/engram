# Engram

AI 友好的事实账本与记忆管理模块 - 为 AI Agent 提供可审计、可回放的证据链与可演化知识沉淀。

## 特性

- **Gateway（MCP 网关）**: 连接 Cursor IDE 与 OpenMemory，提供策略校验、审计落库、失败降级
- **Logbook（事实账本）**: 基于 PostgreSQL 的结构化事件日志，支持 SCM 同步、证据链追溯
- **多项目/多用户**: 支持团队空间与私有空间隔离，每项目独立数据库
- **AI 友好**: 结构化 JSON 输出，易于 LLM 理解和处理

## 推荐使用方式

**服务端部署 Gateway，客户端通过 MCP 协议连接**

```
┌─────────────────────────────────────────────────────────┐
│  Cursor IDE / MCP Client（多个客户端）                   │
└────────────────────────┬────────────────────────────────┘
                         │ MCP JSON-RPC (HTTP)
┌────────────────────────▼────────────────────────────────┐
│  服务器：Gateway + Logbook + OpenMemory                  │
│  - 统一部署，集中管理                                    │
│  - 多项目隔离（PROJECT_KEY）                            │
│  - 多用户支持（actor_user_id + Space）                  │
└─────────────────────────────────────────────────────────┘
```

客户端只需配置 MCP 连接，无需安装 Engram 库。

---

## 快速开始

### 一、服务端部署

#### 1. 环境准备

```bash
# 克隆仓库
git clone https://github.com/onlyfeng/engram.git
cd engram

# 安装依赖
make install-full
```

#### 2. 一键初始化数据库

```bash
# 设置服务账号密码
export LOGBOOK_MIGRATOR_PASSWORD=changeme1
export LOGBOOK_SVC_PASSWORD=changeme2
export OPENMEMORY_MIGRATOR_PASSWORD=changeme3
export OPENMEMORY_SVC_PASSWORD=changeme4

# 初始化数据库（需要 PostgreSQL 18+ 已安装）
make setup-db
```

> 详细安装（PostgreSQL、pgvector、多平台）请参考 [安装指南](docs/installation.md)

#### 3. 启动服务

```bash
# 设置环境变量
export POSTGRES_DSN="postgresql://logbook_svc:$LOGBOOK_SVC_PASSWORD@localhost:5432/engram"
export OPENMEMORY_BASE_URL="http://localhost:8080"
export PROJECT_KEY="default"  # 项目标识

# 启动 Gateway
make gateway
```

服务默认监听 `http://0.0.0.0:8787`

### 二、客户端配置

在 Cursor IDE 的 MCP 配置中添加（参考 `configs/mcp/.mcp.json.example`）：

```json
{
  "mcpServers": {
    "engram": {
      "type": "http",
      "url": "http://<服务器地址>:8787/mcp"
    }
  }
}
```

配置完成后，AI Agent 即可使用记忆管理功能。

---

## 多项目 / 多用户

### 项目隔离

通过 `PROJECT_KEY` 区分不同项目，每个项目使用独立数据库：

```bash
# 部署项目 A
PROJECT_KEY=proj_a POSTGRES_DB=proj_a make gateway

# 部署项目 B（另一个实例）
PROJECT_KEY=proj_b POSTGRES_DB=proj_b GATEWAY_PORT=8788 make gateway
```

### 用户隔离（Space 机制）

| 空间类型 | 格式 | 说明 |
|----------|------|------|
| 团队空间 | `team:<project_key>` | 项目成员共享，默认写入目标 |
| 私有空间 | `private:<user_id>` | 用户个人数据 |

MCP 调用时通过 `actor_user_id` 参数标识用户身份。

> 详见 [记忆契约](docs/gateway/03_memory_contract.md) 和 [治理开关](docs/gateway/04_governance_switch.md)

---

## Makefile 快速命令

```bash
make help          # 查看所有命令

# 安装
make install-full  # 安装完整依赖
make install-dev   # 安装开发依赖

# 数据库（一键初始化）
make setup-db      # 一键初始化数据库（创建库 + DDL + 角色 + 权限 + 验证）

# 数据库（分步操作）
make db-create              # 创建数据库并启用 pgvector
make bootstrap-roles        # 初始化服务账号
make migrate-ddl            # 仅执行 DDL 迁移（Schema/表/索引）
make apply-roles            # 应用 Logbook 角色和权限
make apply-openmemory-grants # 应用 OpenMemory 权限
make verify-permissions     # 验证数据库权限配置

# 服务
make gateway       # 启动 Gateway（带热重载）

# 测试
make test          # 运行所有测试
make test-quick    # 快速冒烟测试

# 代码质量
make lint          # 代码检查
make format        # 代码格式化
```

---

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
    │  (PostgreSQL)     │   │  (语义记忆服务)   │
    │  - 事实账本       │   │  - 向量检索       │
    │  - 治理设置       │   │  - 记忆存储       │
    │  - Outbox 队列    │   │                   │
    └───────────────────┘   └───────────────────┘
```

---

## 项目结构

```
engram/
├── src/engram/              # 源代码
│   ├── gateway/             # Gateway 模块（MCP 网关）
│   └── logbook/             # Logbook 模块（事实账本）
├── sql/                     # 数据库迁移脚本
├── configs/mcp/             # MCP 配置示例
├── docs/                    # 文档
├── tests/                   # 测试
├── Makefile                 # 快速命令
└── pyproject.toml           # 项目配置
```

---

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `POSTGRES_DSN` | PostgreSQL 连接字符串 | - |
| `PROJECT_KEY` | 项目标识（多项目隔离） | `default` |
| `OPENMEMORY_BASE_URL` | OpenMemory 服务地址 | - |
| `GATEWAY_PORT` | Gateway 端口 | `8787` |

> 完整变量列表见 [环境变量参考](docs/reference/environment_variables.md)

---

## 文档索引

| 文档 | 说明 |
|------|------|
| [安装指南](docs/installation.md) | 详细安装步骤（多平台、PostgreSQL、pgvector） |
| [Gateway 文档](docs/gateway/) | MCP 集成、治理开关、降级策略 |
| [Logbook 文档](docs/logbook/) | 架构设计、工具契约、部署运维 |
| [环境变量参考](docs/reference/environment_variables.md) | 所有环境变量说明 |
| [架构文档](docs/architecture/) | 架构决策记录（ADR）、命名规范、迭代计划 |
| [Iteration 2 计划](docs/architecture/iteration_2_plan.md) | 当前迭代：脚本收敛、SQL 整理、CI 硬化、Gateway 模块化 |
| [回归 Runbook](docs/acceptance/iteration_3_regression.md) | 本地与 CI 对齐的回归测试命令 |
| [验收测试矩阵](docs/acceptance/00_acceptance_matrix.md) | 验收测试执行记录与覆盖点 |

---

## 其他使用方式

### 作为 Python 库使用

如需在自己的项目中编程式调用 Logbook：

```bash
pip install engram
```

```python
from engram.logbook import Database, Config

config = Config.from_env()
db = Database(config.postgres_dsn)

# 创建条目
item_id = db.create_item(
    item_type="task",
    title="My Task",
    project_key="my_project"
)

# 添加事件
db.add_event(item_id, event_type="progress", payload={"status": "done"})
```

### CLI 命令

```bash
engram-logbook health              # 健康检查
engram-logbook create_item ...     # 创建条目
engram-gateway                     # 启动 Gateway
```

### SCM 同步工具

SCM 同步子系统提供以下 CLI 工具：

```bash
# 调度器 - 扫描仓库并入队同步任务
engram-scm-scheduler --once --dry-run

# Worker - 从队列处理同步任务
engram-scm-worker --worker-id worker-1

# Reaper - 回收过期任务
engram-scm-reaper --grace-seconds 60

# 状态查看 - 查看同步健康状态
engram-scm-status --prometheus

# 运行器 - 手动执行同步
engram-scm run incremental --repo gitlab:123
```

> **弃用说明**: 根目录的 `python scm_sync_*.py` 脚本已弃用，将在 v1.0 移除。请迁移至 `engram-scm-*` 命令。

> 详细配置参见 [SCM Sync 子系统文档](docs/logbook/06_scm_sync_subsystem.md) 和 [环境变量参考](docs/reference/environment_variables.md#scm-同步服务)

---

## 许可证

MIT License
