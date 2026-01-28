# scripts

Step1 Logbook 脚本工具集，提供 CLI 命令用于操作 Logbook 和 SCM 同步。

## 快速启动测试环境

### 方式 A：Docker Run（无需额外文件）

```bash
# 1. 启动 PostgreSQL 容器
docker run --rm -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=engram_test -p 5432:5432 postgres:16

# 2. 设置环境变量
export TEST_PG_DSN=postgresql://postgres:postgres@localhost:5432/engram_test

# 3. 安装依赖并运行测试
cd step1_logbook_postgres/scripts
pip install -r requirements.txt
pytest -q
```

### 方式 B：Docker Compose（有配置文件）

```bash
# 1. 启动 PostgreSQL（后台运行）
cd step1_logbook_postgres
docker-compose up -d

# 2. 设置环境变量
export TEST_PG_DSN=postgresql://postgres:postgres@localhost:5432/engram_test

# 3. 安装依赖并运行测试
cd scripts
pip install -r requirements.txt
pytest -q

# 4. 停止容器（测试完成后）
cd ..
docker-compose down
```

**连接参数说明：**
- 主机: `localhost`
- 端口: `5432`
- 用户: `postgres`
- 密码: `postgres`
- 数据库: `engram_test`

## 安装依赖

```bash
cd step1_logbook_postgres/scripts
pip install -r requirements.txt
```

依赖包括：
- `psycopg[binary]` - PostgreSQL 驱动
- `pyyaml` - YAML 解析
- `tomli` - TOML 解析（Python < 3.11）
- `requests` - HTTP 请求
- `typer>=0.9.0` - CLI 框架

## 配置文件

配置文件优先级（从高到低）：

1. `--config` / `-c` 命令行参数指定的路径
2. 环境变量 `ENGRAM_STEP1_CONFIG` 指定的路径
3. `./.agentx/config.toml`（工作目录）
4. `~/.agentx/config.toml`（用户目录）

配置文件示例（TOML 格式）：

```toml
[postgres]
dsn = "postgresql://user:pass@localhost:5432/engram"
pool_min_size = 1
pool_max_size = 10
connect_timeout = 10.0

[project]
project_key = "my_project"
description = "项目描述"
tags = ["tag1", "tag2"]

[logging]
level = "INFO"
```

## 脚本说明

| 脚本 | 说明 |
|------|------|
| `logbook_cli.py` | Logbook 操作命令（create_item/add_event/attach/set_kv/health/validate/render_views） |
| `step1_cli.py` | SCM 同步统一入口（ensure-repo/sync-svn/sync-gitlab-commits/sync-gitlab-mrs/sync-gitlab-reviews） |
| `identity_sync.py` | 读取 user.config 写入 identity.* |
| `render_views.py` | 从 DB 生成 manifest.csv / index.md（只读视图） |

## 命令示例

### logbook_cli.py

#### 创建 Item

```bash
python logbook_cli.py create_item \
  --item-type "task" \
  --title "完成数据迁移" \
  --status "open" \
  --owner "user_001"
```

返回示例：
```json
{
  "ok": true,
  "item_id": 42,
  "item_type": "task",
  "title": "完成数据迁移",
  "status": "open"
}
```

#### 添加事件

```bash
python logbook_cli.py add_event \
  --item-id 42 \
  --event-type "status_change" \
  --status-from "open" \
  --status-to "in_progress" \
  --actor "user_001"
```

返回示例：
```json
{
  "ok": true,
  "event_id": 128,
  "item_id": 42,
  "event_type": "status_change",
  "status_updated": true,
  "status_to": "in_progress"
}
```

#### 健康检查

```bash
python logbook_cli.py health --pretty
```

返回示例：
```json
{
  "ok": true,
  "checks": {
    "connection": {"status": "ok", "message": "数据库连接正常"},
    "tables": {
      "status": "ok",
      "details": {
        "logbook.items": {"exists": true},
        "logbook.events": {"exists": true},
        "logbook.attachments": {"exists": true},
        "logbook.kv": {"exists": true},
        "logbook.outbox_memory": {"exists": true}
      }
    }
  }
}
```

#### 渲染视图

```bash
python logbook_cli.py render_views \
  --out-dir ./.agentx/logbook/views \
  --limit 50 \
  --pretty
```

返回示例：
```json
{
  "ok": true,
  "out_dir": "/path/to/.agentx/logbook/views",
  "items_count": 35,
  "files": {
    "manifest": {
      "path": "/path/to/.agentx/logbook/views/manifest.csv",
      "size": 4096,
      "sha256": "abc123..."
    },
    "index": {
      "path": "/path/to/.agentx/logbook/views/index.md",
      "size": 2048,
      "sha256": "def456..."
    }
  },
  "rendered_at": "2025-01-26T12:00:00Z"
}
```

### step1_cli.py

#### 确保仓库存在

```bash
python step1_cli.py scm ensure-repo \
  --repo-type git \
  --repo-url https://gitlab.com/ns/proj \
  --project-key my_project \
  --pretty
```

返回示例：
```json
{
  "success": true,
  "item_id": 10,
  "repo_id": 5,
  "repo_type": "git",
  "url": "https://gitlab.com/ns/proj",
  "project_key": "my_project"
}
```

#### 同步 SVN 日志

```bash
python step1_cli.py scm sync-svn \
  --repo-url svn://example.com/repo \
  --batch-size 100 \
  --loop \
  --pretty
```

返回示例：
```json
{
  "success": true,
  "item_id": 11,
  "total_synced": 250,
  "loop_count": 3,
  "results": {
    "synced_count": 250,
    "has_more": false
  }
}
```

#### 同步 GitLab Commits

```bash
python step1_cli.py scm sync-gitlab-commits \
  --project-id 123 \
  --from 2024-01-01 \
  --to 2024-12-31 \
  --pretty
```

返回示例：
```json
{
  "success": true,
  "item_id": 12,
  "total_synced": 180,
  "total_diffs": 150,
  "loop_count": 2,
  "results": {
    "synced_count": 180,
    "diff_count": 150,
    "has_more": false
  }
}
```

## 视图文件说明

> **重要警告**：`manifest.csv` 和 `index.md` 是由 `render_views` 命令从数据库自动生成的**只读视图**，**严禁手动修改**！

这两个文件：
- 每次执行 `render_views` 时会被完全覆盖重写
- 手动修改的内容会在下次渲染时丢失
- 用于提供快速的本地缓存视图，不应作为数据源

如需修改数据，请使用 `create_item`、`add_event` 等命令操作数据库，然后重新执行 `render_views` 生成最新视图。

## CI 一键测试

```bash
# 一键运行测试（自动启动 PostgreSQL）
cd step1_logbook_postgres/scripts
./ci/run_tests.sh

# 跳过 PG 启动（假设已有数据库）
./ci/run_tests.sh --no-pg

# 测试后清理容器
./ci/run_tests.sh --cleanup
```
