# SCM Sync 运维指南

> **适用人群**：运维工程师、SRE、DevOps  
> **前置阅读**：[06_scm_sync_subsystem.md](./06_scm_sync_subsystem.md)

本文档提供 SCM 同步子系统的运维操作指南，包括部署拓扑、参数调优、常见故障处理。

---

## 目录导航

- [最小闭环](#最小闭环)
- [CLI 入口说明](#cli-入口说明)
- [运行拓扑](#运行拓扑)
- [Docker Compose 部署](#docker-compose-部署)
- [参数推荐](#参数推荐)
- [统一故障处置](#统一故障处置)
- [常见故障处理](#常见故障处理)
- [监控告警](#监控告警)
- [环境变量对齐](#环境变量对齐)
- [参考配置示例](#参考配置示例)

---

## 最小闭环

本节提供快速验证 SCM Sync 子系统的最小步骤，适用于新部署或问题排查时快速确认系统可用性。

### 验证步骤

1. **检查数据库连接**
2. **启动 Scheduler 单次调度**
3. **启动 Worker 处理任务**
4. **查看状态确认同步完成**
5. **启动 Reaper 清理过期资源**

### 命令矩阵

以下矩阵列出三种调用方式，按优先级推荐使用：

| 功能 | console_scripts（推荐） | python -m 模块调用 | docker compose |
|------|------------------------|-------------------|----------------|
| **Scheduler 单次** | `engram-scm-scheduler --once` | `python -m engram.logbook.cli.scm_sync scheduler --once` | `docker compose run --rm scm_scheduler engram-scm-scheduler --once` |
| **Scheduler 循环** | `engram-scm-scheduler --loop` | `python -m engram.logbook.cli.scm_sync scheduler --loop` | `docker compose --profile scm_sync up -d scm_scheduler` |
| **Worker 启动** | `engram-scm-worker --worker-id W1` | `python -m engram.logbook.cli.scm_sync worker --worker-id W1` | `docker compose --profile scm_sync up -d scm_worker` |
| **Worker 单任务** | `engram-scm-worker --worker-id W1 --once` | `python -m engram.logbook.cli.scm_sync worker --worker-id W1 --once` | `docker compose run --rm scm_worker engram-scm-worker --worker-id W1 --once` |
| **Reaper 单次** | `engram-scm-reaper --once` | `python -m engram.logbook.cli.scm_sync reaper --once` | `docker compose run --rm scm_reaper engram-scm-reaper --once` |
| **Reaper 循环** | `engram-scm-reaper --loop` | `python -m engram.logbook.cli.scm_sync reaper --loop` | `docker compose --profile scm_sync up -d scm_reaper` |
| **状态查看** | `engram-scm-status --json` | `python -m engram.logbook.cli.scm_sync status --json` | `docker compose run --rm scm_scheduler engram-scm-status --json` |
| **手动同步** | `engram-scm-sync runner incremental --repo gitlab:123` | `python -m engram.logbook.cli.scm_sync runner incremental --repo gitlab:123` | `docker compose run --rm scm_worker engram-scm-sync runner incremental --repo gitlab:123` |
| **Admin 操作** | `engram-scm-sync admin jobs list --status dead` | `python -m engram.logbook.cli.scm_sync admin jobs list --status dead` | `docker compose run --rm scm_scheduler engram-scm-sync admin jobs list --status dead` |

### 最小闭环示例（本地开发）

```bash
# 1. 设置环境变量
export POSTGRES_DSN="postgresql://logbook_svc:password@localhost:5432/engram"
export GITLAB_URL="https://gitlab.example.com"
export GITLAB_TOKEN="your_token_here"

# 2. 执行一次调度（干运行查看将入队的任务）
engram-scm-scheduler --once --dry-run --json

# 3. 实际调度
engram-scm-scheduler --once

# 4. 查看任务队列状态
engram-scm-status --json | jq '.jobs_by_status'

# 5. 处理一个任务
engram-scm-worker --worker-id local-test --once

# 6. 再次查看状态确认任务完成
engram-scm-status --json

# 7. 执行清理
engram-scm-reaper --once
```

### 最小闭环示例（Docker Compose）

```bash
# 1. 配置 .env 文件
cat >> .env << 'EOF'
GITLAB_URL=https://gitlab.example.com
GITLAB_TOKEN=your_token_here
EOF

# 2. 启动基础服务（PostgreSQL）
docker compose up -d postgres

# 3. 执行一次调度
docker compose run --rm scm_scheduler engram-scm-scheduler --once --dry-run

# 4. 查看状态
docker compose run --rm scm_scheduler engram-scm-status --json

# 5. 启动完整 SCM Sync 服务
docker compose --profile scm_sync up -d
```

---

## CLI 入口说明

SCM Sync 子系统通过统一的 CLI 入口管理，**推荐使用 `engram-scm-*` 命令**：

```bash
# 推荐方式（console_scripts 入口）
engram-scm-scheduler [args]   # 调度器
engram-scm-worker [args]      # Worker
engram-scm-reaper [args]      # Reaper
engram-scm-status [args]      # 状态查看
engram-scm-sync runner <子命令> [args]  # 运行器（手动执行同步）

# 或使用模块调用
python -m engram.logbook.cli.scm_sync <子命令> [args]
```

> **弃用说明**: 根目录的 `python scm_sync_*.py` 脚本已移除。请使用 `engram-scm-*` 命令或 `python -m engram.logbook.cli.scm_sync`。

### Runner 命令详解

Runner 是手动执行同步的工具，支持增量同步和回填同步两种模式。与 Scheduler/Worker 自动调度不同，Runner 适用于：
- 手动触发单个仓库的同步
- 回填历史数据
- 调试和测试同步逻辑

**返回码说明**：

| 返回码 | 常量 | 含义 |
|--------|------|------|
| 0 | EXIT_SUCCESS | 全部成功 |
| 1 | EXIT_PARTIAL | 部分成功（有失败但非全部失败） |
| 2 | EXIT_FAILED | 全部失败或严重错误 |

**增量同步示例**：

```bash
# 基本增量同步
python -m engram.logbook.cli.scm_sync runner incremental --repo gitlab:123

# 指定任务类型
python -m engram.logbook.cli.scm_sync runner incremental --repo gitlab:123 --job mrs

# 循环模式（持续同步）
python -m engram.logbook.cli.scm_sync runner incremental --repo gitlab:123 --loop --loop-interval 60

# 循环模式限制迭代次数
python -m engram.logbook.cli.scm_sync runner incremental --repo gitlab:123 --loop --max-iterations 10

# JSON 输出（便于日志采集）
python -m engram.logbook.cli.scm_sync runner incremental --repo gitlab:123 --json
```

**回填同步示例**：

```bash
# 回填最近 24 小时
python -m engram.logbook.cli.scm_sync runner backfill --repo gitlab:123 --last-hours 24

# 回填最近 7 天
python -m engram.logbook.cli.scm_sync runner backfill --repo gitlab:123 --last-days 7

# 回填指定时间范围
python -m engram.logbook.cli.scm_sync runner backfill --repo gitlab:123 \
    --since 2025-01-01T00:00:00Z --until 2025-01-31T23:59:59Z

# SVN 回填指定版本范围
python -m engram.logbook.cli.scm_sync runner backfill --repo svn:https://svn.example.com/repo \
    --start-rev 100 --end-rev 500

# 回填并更新游标（仅当全部成功时更新）
python -m engram.logbook.cli.scm_sync runner backfill --repo gitlab:123 --last-hours 24 --update-watermark

# 模拟运行（不实际执行）
python -m engram.logbook.cli.scm_sync runner backfill --repo gitlab:123 --last-hours 24 --dry-run

# JSON 输出（包含详细 chunk 结果）
python -m engram.logbook.cli.scm_sync runner backfill --repo gitlab:123 --last-hours 24 --json
```

**回填分片机制**：

回填模式会将时间/版本窗口分割为多个 chunk，逐个执行同步。这样做的优点：
- 避免单次同步过大导致超时
- 支持断点续传（部分失败时可重试失败的 chunk）
- 提供详细的进度和错误信息

默认配置：
- 时间窗口：每 4 小时一个 chunk
- 版本窗口：每 100 个版本一个 chunk

**JSON 输出结构**：

增量同步输出：
```json
{
  "phase": "incremental",
  "repo": "gitlab:123",
  "job": "commits",
  "status": "success",
  "items_synced": 50,
  "message": null,
  "error": null,
  "started_at": "2025-01-31T10:00:00Z",
  "finished_at": "2025-01-31T10:00:30Z",
  "vfacts_refreshed": true,
  "exit_code": 0
}
```

回填同步输出：
```json
{
  "phase": "backfill",
  "repo": "gitlab:123",
  "job": "commits",
  "status": "partial",
  "total_chunks": 6,
  "success_chunks": 5,
  "partial_chunks": 0,
  "failed_chunks": 1,
  "total_items_synced": 500,
  "total_items_skipped": 10,
  "total_items_failed": 20,
  "chunk_results": [
    {"chunk_index": 0, "status": "success", "synced_count": 100},
    {"chunk_index": 1, "status": "success", "synced_count": 100},
    ...
  ],
  "errors": ["chunk 5 failed: timeout"],
  "watermark_updated": false,
  "vfacts_refreshed": true,
  "exit_code": 1
}
```

### 循环模式参数

Scheduler 和 Reaper 支持循环模式参数，便于作为常驻进程部署：

| 参数 | 说明 |
|------|------|
| `--loop` | 启用循环模式，持续执行 |
| `--interval-seconds N` | 循环间隔秒数（默认 60） |
| `--once` | 执行一次后退出（默认行为，与 `--loop` 互斥） |
| `--json` | JSON 格式输出（`--loop` 模式下每轮输出单行 JSON 便于采集）|

示例：

```bash
# Scheduler 循环模式
engram-scm-sync scheduler --loop --interval-seconds 30 --json

# Reaper 循环模式
engram-scm-sync reaper --loop --interval-seconds 60 --json

# 执行一次（默认行为）
engram-scm-sync scheduler --once
engram-scm-sync reaper --once
```

---

## Docker Compose 部署

SCM Sync 服务通过 `scm_sync` profile 提供，包含以下容器：

| 服务 | 命令 | 说明 |
|------|------|------|
| `scm_scheduler` | `engram-scm-scheduler --loop` | 调度器（单实例） |
| `scm_worker` | `engram-scm-worker` | Worker（支持多副本） |
| `scm_reaper` | `engram-scm-reaper --loop` | 清理器（单实例） |

### 启动服务

```bash
# 统一栈部署
docker compose --profile scm_sync up -d

# Logbook-only 部署
docker compose -f compose/logbook.yml --profile scm_sync up -d

# 扩展 Worker 副本数
docker compose --profile scm_sync up -d --scale scm_worker=3
```

### 必需的环境变量

**敏感凭证（不提供默认值，必须通过 `.env` 或环境变量设置）**：

| 变量 | 说明 |
|------|------|
| `GITLAB_URL` | GitLab 服务地址 |
| `GITLAB_TOKEN` | GitLab 访问令牌 |
| `GITLAB_PRIVATE_TOKEN` | GitLab 私有令牌（别名） |
| `SVN_USERNAME` | SVN 用户名 |
| `SVN_PASSWORD` | SVN 密码 |

> **安全提示**: 以上敏感变量**不提供默认值**，必须在 `.env` 文件或环境变量中显式设置。切勿将凭证提交到版本控制。

**功能开关与配置**：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ENGRAM_SCM_SYNC_ENABLED` | `true` | 启用 SCM 同步功能 |
| `SCM_SCHEDULER_GLOBAL_CONCURRENCY` | `20` | 全局队列深度 |
| `SCM_SCHEDULER_MAX_RUNNING` | `10` | 最大运行任务数 |
| `SCM_SCHEDULER_SCAN_INTERVAL_SECONDS` | `60` | 扫描间隔 |
| `SCM_WORKER_LEASE_SECONDS` | `300` | 任务租约时长 |
| `SCM_WORKER_POLL_INTERVAL` | `10` | Worker 轮询间隔 |
| `SCM_WORKER_PARALLELISM` | `2` | Worker 内部并行度 |

详细变量说明请参考 [环境变量参考](../reference/environment_variables.md#scm-同步服务)。

### .env 示例

```bash
# ===== SCM Sync 凭证（敏感，必填）=====
GITLAB_URL=https://gitlab.example.com
GITLAB_TOKEN=your_gitlab_token_here
# SVN_USERNAME=svn_user
# SVN_PASSWORD=svn_password

# ===== SCM Sync 配置（可选）=====
ENGRAM_SCM_SYNC_ENABLED=true
SCM_SCHEDULER_GLOBAL_CONCURRENCY=20
SCM_SCHEDULER_MAX_RUNNING=10
```

---

## 运行拓扑

本节描述 SCM Sync 子系统的运行拓扑，包括组件实例数约束和扩展策略。

### 组件实例数约束

| 组件 | 实例数 | 说明 |
|------|--------|------|
| **Scheduler** | **必须 1** | 单实例运行，避免重复入队。多实例会导致任务重复 |
| **Reaper** | **建议 1** | 单实例运行即可满足清理需求。多实例不会造成问题但无必要 |
| **Worker** | **N（可扩展）** | 支持多实例水平扩展，根据任务负载调整副本数 |

### 部署拓扑

### 单 Worker 部署

适用于小型环境（< 10 仓库），简化运维复杂度。

```
┌─────────────────────────────────────────────────────────────┐
│                      单 Worker 部署                          │
│                                                             │
│  ┌───────────┐     ┌───────────┐     ┌───────────┐         │
│  │ Scheduler │────▶│   Queue   │────▶│  Worker   │         │
│  │ (定时扫描) │     │(sync_jobs)│     │ (单进程)   │         │
│  └───────────┘     └───────────┘     └───────────┘         │
│        │                                    │               │
│        └────────────────┬───────────────────┘               │
│                         ▼                                   │
│                  ┌─────────────┐                            │
│                  │  PostgreSQL │                            │
│                  └─────────────┘                            │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**配置示例**：

```bash
# 单 Worker 环境变量
SCM_SCHEDULER_GLOBAL_CONCURRENCY=5
SCM_SCHEDULER_MAX_RUNNING=3
SCM_WORKER_LEASE_SECONDS=300
SCM_WORKER_PARALLELISM=1
```

### 多 Worker 部署

适用于中大型环境，通过多个 Worker 并行处理提升吞吐。

```
┌─────────────────────────────────────────────────────────────┐
│                      多 Worker 部署                          │
│                                                             │
│  ┌───────────┐                                              │
│  │ Scheduler │─────────────────────┐                        │
│  │ (单实例)   │                     │                        │
│  └───────────┘                     ▼                        │
│                              ┌───────────┐                  │
│                              │   Queue   │                  │
│                              │(sync_jobs)│                  │
│                              └─────┬─────┘                  │
│                    ┌───────────────┼───────────────┐        │
│                    ▼               ▼               ▼        │
│              ┌───────────┐  ┌───────────┐  ┌───────────┐    │
│              │ Worker-1  │  │ Worker-2  │  │ Worker-N  │    │
│              └───────────┘  └───────────┘  └───────────┘    │
│                    │               │               │        │
│                    └───────────────┴───────────────┘        │
│                                    ▼                        │
│                             ┌─────────────┐                 │
│                             │  PostgreSQL │                 │
│                             └─────────────┘                 │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**部署要点**：

| 组件 | 实例数 | 说明 |
|------|--------|------|
| Scheduler | 1 | **必须单实例**，避免重复入队 |
| Worker | N | 支持多实例水平扩展 |
| Reaper | 1 | 建议单实例，清理过期任务和锁 |

**配置示例**：

```bash
# Scheduler（单实例）
SCM_SCHEDULER_GLOBAL_CONCURRENCY=20
SCM_SCHEDULER_MAX_RUNNING=10
SCM_SCHEDULER_SCAN_INTERVAL_SECONDS=30

# Worker（多实例，每个 Worker 相同配置）
SCM_WORKER_LEASE_SECONDS=300
SCM_WORKER_RENEW_INTERVAL_SECONDS=60
SCM_WORKER_PARALLELISM=2
```

### Worker Pool 分组部署

适用于多 GitLab 实例或多租户环境，通过 Pool 隔离实现资源分配。

```
┌─────────────────────────────────────────────────────────────────────┐
│                       Worker Pool 分组部署                            │
│                                                                     │
│  ┌───────────┐                                                      │
│  │ Scheduler │──────────────────────────────────┐                   │
│  └───────────┘                                  │                   │
│                                                 ▼                   │
│                                          ┌───────────┐              │
│                                          │   Queue   │              │
│                                          └─────┬─────┘              │
│                           ┌────────────────────┼────────────────────┐
│                           │                    │                    │
│         ┌─────────────────▼─────────────────┐  │  ┌────────────────▼────────────────┐
│         │       Pool: gitlab-prod            │  │  │       Pool: gitlab-staging      │
│         │  ┌─────────┐  ┌─────────┐          │  │  │  ┌─────────┐  ┌─────────┐      │
│         │  │Worker-1 │  │Worker-2 │          │  │  │  │Worker-1 │  │Worker-2 │      │
│         │  └─────────┘  └─────────┘          │  │  │  └─────────┘  └─────────┘      │
│         │  instance_allowlist:               │  │  │  instance_allowlist:           │
│         │  ["gitlab.prod.example.com"]       │  │  │  ["gitlab.staging.example.com"]│
│         └───────────────────────────────────-┘  │  └────────────────────────────────┘
│                           │                     │                    │
│                           └─────────────────────┼────────────────────┘
│                                                 ▼
│                                          ┌─────────────┐
│                                          │  PostgreSQL │
│                                          └─────────────┘
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

**Worker Pool 配置**：

```bash
# Pool: gitlab-prod（生产环境 GitLab）
SCM_WORKER_INSTANCE_ALLOWLIST="gitlab.prod.example.com"
SCM_WORKER_TENANT_ALLOWLIST=""  # 不限制租户
SCM_WORKER_PARALLELISM=4

# Pool: gitlab-staging（测试环境 GitLab）
SCM_WORKER_INSTANCE_ALLOWLIST="gitlab.staging.example.com"
SCM_WORKER_TENANT_ALLOWLIST=""
SCM_WORKER_PARALLELISM=2

# Pool: tenant-vip（VIP 租户专用）
SCM_WORKER_INSTANCE_ALLOWLIST=""  # 不限制实例
SCM_WORKER_TENANT_ALLOWLIST="tenant-vip-001,tenant-vip-002"
SCM_WORKER_PARALLELISM=2
```

**Scheduler 并发限制**：

```bash
# 按实例限制（防止单个 GitLab 过载）
SCM_SCHEDULER_PER_INSTANCE_CONCURRENCY=5

# 按租户限制（防止大租户饥饿其他租户）
SCM_SCHEDULER_PER_TENANT_CONCURRENCY=3

# 启用租户公平调度
SCM_SCHEDULER_ENABLE_TENANT_FAIRNESS=true
SCM_SCHEDULER_TENANT_FAIRNESS_MAX_PER_ROUND=1
```

---

## 参数推荐

### 小型环境（<10 仓库）

| 参数 | 推荐值 | 说明 |
|------|--------|------|
| `SCM_SCHEDULER_GLOBAL_CONCURRENCY` | `5` | 队列深度 |
| `SCM_SCHEDULER_MAX_RUNNING` | `3` | 最大运行数 |
| `SCM_SCHEDULER_SCAN_INTERVAL_SECONDS` | `60` | 扫描间隔 |
| `SCM_WORKER_LEASE_SECONDS` | `300` | 租约时长 |
| `SCM_WORKER_PARALLELISM` | `1` | 内部并行度 |
| `SCM_CB_MIN_SAMPLES` | `3` | 熔断最小样本 |

```bash
# .env 配置示例
SCM_SCHEDULER_GLOBAL_CONCURRENCY=5
SCM_SCHEDULER_MAX_RUNNING=3
SCM_SCHEDULER_SCAN_INTERVAL_SECONDS=60
SCM_WORKER_LEASE_SECONDS=300
SCM_CB_MIN_SAMPLES=3
```

### 中型环境（10-100 仓库）

| 参数 | 推荐值 | 说明 |
|------|--------|------|
| `SCM_SCHEDULER_GLOBAL_CONCURRENCY` | `20` | 队列深度 |
| `SCM_SCHEDULER_MAX_RUNNING` | `10` | 最大运行数 |
| `SCM_SCHEDULER_PER_INSTANCE_CONCURRENCY` | `5` | 每实例并发 |
| `SCM_SCHEDULER_SCAN_INTERVAL_SECONDS` | `30` | 扫描间隔 |
| `SCM_WORKER_LEASE_SECONDS` | `300` | 租约时长 |
| `SCM_WORKER_PARALLELISM` | `2` | 内部并行度 |
| `SCM_CB_MIN_SAMPLES` | `5` | 熔断最小样本 |
| `SCM_CB_WINDOW_COUNT` | `20` | 统计窗口 |

```bash
# .env 配置示例
SCM_SCHEDULER_GLOBAL_CONCURRENCY=20
SCM_SCHEDULER_MAX_RUNNING=10
SCM_SCHEDULER_PER_INSTANCE_CONCURRENCY=5
SCM_SCHEDULER_SCAN_INTERVAL_SECONDS=30
SCM_WORKER_LEASE_SECONDS=300
SCM_WORKER_PARALLELISM=2
SCM_CB_MIN_SAMPLES=5
SCM_CB_WINDOW_COUNT=20
```

### 大型环境（>100 仓库）

| 参数 | 推荐值 | 说明 |
|------|--------|------|
| `SCM_SCHEDULER_GLOBAL_CONCURRENCY` | `50` | 队列深度 |
| `SCM_SCHEDULER_MAX_RUNNING` | `20` | 最大运行数 |
| `SCM_SCHEDULER_PER_INSTANCE_CONCURRENCY` | `10` | 每实例并发 |
| `SCM_SCHEDULER_PER_TENANT_CONCURRENCY` | `5` | 每租户并发 |
| `SCM_SCHEDULER_ENABLE_TENANT_FAIRNESS` | `true` | 启用公平调度 |
| `SCM_SCHEDULER_SCAN_INTERVAL_SECONDS` | `15` | 扫描间隔 |
| `SCM_WORKER_LEASE_SECONDS` | `600` | 租约时长（大任务） |
| `SCM_WORKER_PARALLELISM` | `4` | 内部并行度 |
| `SCM_CB_MIN_SAMPLES` | `10` | 熔断最小样本 |
| `SCM_CB_ENABLE_SMOOTHING` | `true` | 启用平滑 |

```bash
# .env 配置示例
SCM_SCHEDULER_GLOBAL_CONCURRENCY=50
SCM_SCHEDULER_MAX_RUNNING=20
SCM_SCHEDULER_PER_INSTANCE_CONCURRENCY=10
SCM_SCHEDULER_PER_TENANT_CONCURRENCY=5
SCM_SCHEDULER_ENABLE_TENANT_FAIRNESS=true
SCM_SCHEDULER_SCAN_INTERVAL_SECONDS=15
SCM_WORKER_LEASE_SECONDS=600
SCM_WORKER_PARALLELISM=4
SCM_CB_MIN_SAMPLES=10
SCM_CB_ENABLE_SMOOTHING=true
SCM_CB_SMOOTHING_ALPHA=0.3
```

---

## 运维操作

### Admin CLI 命令（推荐）

SCM Sync 子系统提供了统一的 admin CLI 工具，用于运维管理操作。**推荐使用 admin CLI 而非直接执行 SQL**，因为它：
- 自动处理敏感信息脱敏
- 提供 JSON/文本两种输出格式
- 内置参数校验和错误处理

#### admin CLI 概览

```bash
# 查看帮助
engram-scm-sync admin --help

# 子命令结构
engram-scm-sync admin <子命令> <操作> [选项]

# 可用子命令:
#   jobs        任务管理 (list/reset-dead/mark-dead)
#   locks       锁管理 (list/force-release/list-expired)
#   pauses      暂停管理 (set/unset/list)
#   cursors     游标管理 (list/get/set/delete)
#   rate-limit  速率限制管理 (buckets list/pause/unpause)
```

#### jobs - 任务管理

```bash
# 列出 dead 任务（默认）
engram-scm-sync admin jobs list --status dead

# 列出指定状态的任务
engram-scm-sync admin jobs list --status pending --limit 50

# 按仓库和任务类型过滤
engram-scm-sync admin jobs list --status failed --repo-id 123 --job-type commits

# JSON 输出
engram-scm-sync admin jobs list --status dead --json

# 重置所有 dead 任务为 pending（模拟运行）
engram-scm-sync admin jobs reset-dead --dry-run

# 重置所有 dead 任务
engram-scm-sync admin jobs reset-dead

# 重置指定任务
engram-scm-sync admin jobs reset-dead --job-ids "job-1,job-2,job-3"

# 重置指定仓库的 dead 任务
engram-scm-sync admin jobs reset-dead --repo-id 123

# 将任务标记为 dead
engram-scm-sync admin jobs mark-dead --job-id job-123 --reason "manual_intervention"
```

#### locks - 锁管理

```bash
# 列出所有锁
engram-scm-sync admin locks list

# 按仓库过滤
engram-scm-sync admin locks list --repo-id 123

# JSON 输出
engram-scm-sync admin locks list --json

# 列出过期锁
engram-scm-sync admin locks list-expired

# 列出宽限期内的过期锁
engram-scm-sync admin locks list-expired --grace-seconds 60

# 强制释放锁
engram-scm-sync admin locks force-release --lock-id 456
```

#### pauses - 暂停管理

```bash
# 列出所有暂停
engram-scm-sync admin pauses list

# 包含已过期的暂停
engram-scm-sync admin pauses list --include-expired

# JSON 输出
engram-scm-sync admin pauses list --json

# 设置暂停（暂停 1 小时）
engram-scm-sync admin pauses set --repo-id 123 --job-type commits --duration 3600 --reason "maintenance"

# 取消暂停
engram-scm-sync admin pauses unset --repo-id 123 --job-type commits
```

#### cursors - 游标管理

```bash
# 列出所有游标
engram-scm-sync admin cursors list

# 按 key 前缀过滤
engram-scm-sync admin cursors list --key-prefix "cursor:123"

# JSON 输出
engram-scm-sync admin cursors list --json

# 获取游标值
engram-scm-sync admin cursors get --repo-id 123 --job-type commits

# 设置游标值（JSON 格式）
engram-scm-sync admin cursors set --repo-id 123 --job-type commits --value '{"watermark": "2025-01-01T00:00:00Z"}'

# 删除游标（触发全量同步）
engram-scm-sync admin cursors delete --repo-id 123 --job-type commits
```

#### rate-limit - 速率限制管理

```bash
# 列出所有速率限制桶
engram-scm-sync admin rate-limit buckets list

# JSON 输出
engram-scm-sync admin rate-limit buckets list --json

# 暂停桶（暂停 1 小时）
engram-scm-sync admin rate-limit buckets pause --instance-key "gitlab.example.com" --duration 3600 --reason "rate_limit_hit"

# 取消暂停桶
engram-scm-sync admin rate-limit buckets unpause --instance-key "gitlab.example.com"
```

#### 通用选项

所有 admin 命令支持以下通用选项：

| 选项 | 说明 |
|------|------|
| `--dsn` | 数据库连接字符串（默认从 LOGBOOK_DSN 环境变量读取） |
| `--json` | JSON 格式输出 |
| `-v, --verbose` | 详细日志输出 |

---

### 查看系统状态

**CLI 方式**：

```bash
# 查看同步状态摘要（JSON 格式）
engram-scm-status --json

# 查看 Prometheus 格式指标
engram-scm-status --prometheus

# 查看任务队列状态
engram-scm-status --json | jq '.jobs_by_status'
```

### 健康检查（Health Gate）

健康检查功能用于检测系统不变量违规，适用于监控告警和自动化运维。

**CLI 用法**：

```bash
# 基本健康检查（文本输出）
engram-scm-status --health

# JSON 格式输出（便于程序解析）
engram-scm-status --health --json

# 包含详细违规记录
engram-scm-status --health --include-details

# 自定义 grace_seconds（running job 过期宽限时间）
engram-scm-status --health --grace-seconds 120
```

**退出码**：

| 退出码 | 含义 |
|--------|------|
| 0 | 健康（无违规） |
| 1 | 有 warning 级别违规 |
| 2 | 有 critical 级别违规 |

**检查项说明**：

| 检查项 ID | 严重程度 | 说明 | 修复建议 |
|-----------|----------|------|----------|
| `expired_running_jobs` | CRITICAL | running 状态任务租约已过期 | 运行 `engram-scm-sync reaper --once` |
| `orphan_locks` | WARNING | sync_locks 存在但无对应 running job | 运行 `engram-scm-sync admin locks force-release --lock-id <id>` |
| `gitlab_jobs_missing_dimensions` | WARNING | gitlab_* jobs 缺失 gitlab_instance/tenant_id | 检查 scheduler 入队逻辑或 SQL 补填 |
| `expired_pauses` | INFO | 过期的暂停记录仍存在数据库 | 等待自动清理或手动删除 |
| `circuit_breaker_inconsistencies` | WARNING | 熔断器状态与 error_budget 不一致 | 运行 `engram-scm-sync admin jobs reset-dead` |

**JSON 输出 Schema**：

```json
{
  "healthy": false,
  "exit_code": 2,
  "checked_at": 1738300800.0,
  "total_checks": 5,
  "passed_checks": 4,
  "failed_checks": 1,
  "violations": [
    {
      "check_id": "expired_running_jobs",
      "name": "过期的 Running 任务",
      "severity": "critical",
      "count": 3,
      "description": "有 3 个 running 状态的任务租约已过期",
      "remediation_hint": "运行 `engram-scm-sync reaper --once` 回收过期任务",
      "details": [
        {"job_id": "1", "repo_id": 100, "job_type": "gitlab_commits"}
      ]
    }
  ]
}
```

**监控集成示例**：

```bash
#!/bin/bash
# health_check.sh - 用于 Prometheus Node Exporter 或 cron 告警

result=$(engram-scm-status --health --json 2>/dev/null)
exit_code=$?

# 输出指标（供 Prometheus Pushgateway）
echo "scm_sync_health_status $exit_code"
echo "scm_sync_health_violations $(echo $result | jq '.violations | length')"

# 退出码作为健康状态
exit $exit_code
```

**Prometheus 告警规则**：

```yaml
groups:
  - name: scm_sync_health_gate
    rules:
      - alert: SCMSyncUnhealthy
        expr: scm_sync_health_status > 0
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "SCM Sync 健康检查失败"
          description: "健康检查退出码: {{ $value }}，请运行 engram-scm-status --health 查看详情"

      - alert: SCMSyncCriticalViolation
        expr: scm_sync_health_status == 2
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "SCM Sync 存在 Critical 级别违规"
          description: "需要立即处理，请运行 engram-scm-status --health --include-details 查看详情"
```

**SQL 方式**：

```sql
-- 查看各状态任务数量
SELECT status, COUNT(*) FROM scm.sync_jobs GROUP BY status;

-- 查看 running 任务详情
SELECT job_id, repo_id, job_type, locked_by, locked_at, 
       NOW() - locked_at AS running_duration
FROM scm.sync_jobs 
WHERE status = 'running'
ORDER BY locked_at;

-- 查看最近失败的任务
SELECT job_id, repo_id, job_type, attempts, error_summary_json
FROM scm.sync_jobs 
WHERE status IN ('failed', 'dead')
ORDER BY updated_at DESC
LIMIT 20;

-- 查看熔断状态
SELECT key, value_json 
FROM logbook.kv 
WHERE namespace = 'scm.sync_health';

-- 查看暂停状态
SELECT key, value_json 
FROM logbook.kv 
WHERE namespace = 'scm.sync_pauses';
```

### 重置 Dead 任务

Dead 任务是重试耗尽后标记为不可恢复的任务，需人工介入处理。

**场景分析**：

| 场景 | 原因 | 建议操作 |
|------|------|----------|
| 临时网络故障恢复 | 网络已恢复 | 直接重置 |
| GitLab 维护结束 | 服务已恢复 | 直接重置 |
| 配置错误 | token/权限问题 | 先修复配置再重置 |
| 仓库不存在 | 已删除 | 删除任务而非重置 |

**推荐方式（Admin CLI）**：

```bash
# 查看 dead 任务
engram-scm-sync admin jobs list --status dead

# 查看 dead 任务（JSON 格式，便于脚本处理）
engram-scm-sync admin jobs list --status dead --json

# 模拟重置（查看将被重置的任务）
engram-scm-sync admin jobs reset-dead --dry-run

# 重置所有 dead 任务
engram-scm-sync admin jobs reset-dead

# 重置指定任务
engram-scm-sync admin jobs reset-dead --job-ids "job-1,job-2"

# 重置指定仓库的 dead 任务
engram-scm-sync admin jobs reset-dead --repo-id 123

# 将问题任务标记为 dead（不再重试）
engram-scm-sync admin jobs mark-dead --job-id job-456 --reason "repo_deleted"
```

**SQL 方式**（直接操作，需谨慎）：

```sql
-- 查看 dead 任务
SELECT * FROM scm.sync_jobs WHERE status = 'dead';

-- 重置指定 dead 任务
UPDATE scm.sync_jobs 
SET status = 'pending', 
    attempts = 0, 
    not_before = NOW(),
    locked_by = NULL,
    locked_at = NULL,
    error_summary_json = NULL
WHERE job_id = '<JOB_ID>' AND status = 'dead';

-- 重置所有 dead 任务（谨慎）
UPDATE scm.sync_jobs 
SET status = 'pending', 
    attempts = 0, 
    not_before = NOW(),
    locked_by = NULL,
    locked_at = NULL,
    error_summary_json = NULL
WHERE status = 'dead';
```

### 强制释放锁

当 Worker 异常退出时，可能遗留未释放的锁，导致任务无法被其他 Worker 获取。

**推荐方式（Admin CLI）**：

```bash
# 列出所有锁
engram-scm-sync admin locks list

# 列出指定仓库的锁
engram-scm-sync admin locks list --repo-id 123

# 列出过期锁
engram-scm-sync admin locks list-expired

# 列出宽限期内的过期锁
engram-scm-sync admin locks list-expired --grace-seconds 60

# 强制释放锁（通过 lock_id）
engram-scm-sync admin locks force-release --lock-id 456

# JSON 输出（便于脚本处理）
engram-scm-sync admin locks list-expired --json
```

**SQL 方式**：

```sql
-- 查看过期锁（默认租约 300 秒）
SELECT * FROM scm.sync_locks 
WHERE locked_at < NOW() - INTERVAL '300 seconds';

-- 强制释放特定锁
DELETE FROM scm.sync_locks 
WHERE repo_id = <REPO_ID> AND job_type = 'commits';

-- 强制释放所有过期锁
DELETE FROM scm.sync_locks 
WHERE locked_at < NOW() - INTERVAL '300 seconds';
```

**同时重置卡住的任务**：

```sql
-- 将锁过期的 running 任务重置为 pending
UPDATE scm.sync_jobs 
SET status = 'pending',
    locked_by = NULL,
    locked_at = NULL,
    not_before = NOW()
WHERE status = 'running' 
  AND locked_at < NOW() - INTERVAL '600 seconds';
```

### 暂停与恢复熔断

熔断器用于保护下游 GitLab/SVN 服务，当错误率超过阈值时自动触发。

**查看熔断状态**：

```bash
# 查看所有熔断器状态
python -c "
import json
import db as db_api
with db_api.get_connection() as conn:
    with conn.cursor() as cur:
        cur.execute(\"SELECT key, value_json FROM logbook.kv WHERE namespace = 'scm.sync_health'\")
        for row in cur.fetchall():
            state = row[1] if isinstance(row[1], dict) else json.loads(row[1])
            print(f\"key={row[0]}, state={state.get('state')}, opened_at={state.get('opened_at')}, reason={state.get('last_failure_reason')}\")"
```

**强制打开熔断器（紧急暂停）**：

```bash
# 强制熔断所有同步
python -c "
from src.engram.logbook.scm_sync_policy import CircuitBreakerController

controller = CircuitBreakerController(key='default:global')
controller.force_open(reason='manual_pause_for_maintenance')

# 持久化状态
import json
import db as db_api
with db_api.get_connection() as conn:
    db_api.kv_set(conn, 'scm.sync_health', 'default:global', json.dumps(controller.get_state_dict()))
    print('Circuit breaker opened')
"
```

**强制关闭熔断器（恢复同步）**：

```bash
# 强制恢复同步
python -c "
from src.engram.logbook.scm_sync_policy import CircuitBreakerController

controller = CircuitBreakerController(key='default:global')
controller.force_close()

# 持久化状态
import json
import db as db_api
with db_api.get_connection() as conn:
    db_api.kv_set(conn, 'scm.sync_health', 'default:global', json.dumps(controller.get_state_dict()))
    print('Circuit breaker closed')
"
```

**SQL 方式**：

```sql
-- 查看熔断状态
SELECT key, value_json FROM logbook.kv 
WHERE namespace = 'scm.sync_health';

-- 强制关闭熔断（设置为 closed 状态）
UPDATE logbook.kv 
SET value_json = jsonb_set(
    COALESCE(value_json::jsonb, '{}'::jsonb),
    '{state}',
    '"closed"'
)
WHERE namespace = 'scm.sync_health' AND key = 'default:global';

-- 删除熔断状态（完全重置）
DELETE FROM logbook.kv 
WHERE namespace = 'scm.sync_health' AND key = 'default:global';
```

**暂停特定仓库/任务类型（Admin CLI 推荐）**：

```bash
# 列出所有暂停
engram-scm-sync admin pauses list

# 包含已过期的暂停
engram-scm-sync admin pauses list --include-expired

# 暂停特定 (repo_id, job_type) 组合（暂停 1 小时）
engram-scm-sync admin pauses set --repo-id 123 --job-type commits --duration 3600 --reason "maintenance"

# 取消暂停
engram-scm-sync admin pauses unset --repo-id 123 --job-type commits

# JSON 输出
engram-scm-sync admin pauses list --json
```

**脚本方式**（仅在需要自定义逻辑时使用）：

```bash
# 暂停特定 (repo_id, job_type) 组合
python -c "
import time
import json
import db as db_api

repo_id = <REPO_ID>
job_type = 'commits'
pause_seconds = 3600  # 暂停 1 小时

pause_data = {
    'repo_id': repo_id,
    'job_type': job_type,
    'reason': 'manual_pause',
    'reason_code': 'maintenance',
    'paused_at': time.time(),
    'paused_until': time.time() + pause_seconds,
}

with db_api.get_connection() as conn:
    db_api.kv_set(conn, 'scm.sync_pauses', f'{repo_id}:{job_type}', json.dumps(pause_data))
    print(f'Paused repo_id={repo_id}, job_type={job_type} for {pause_seconds}s')
"
```

### 重置游标

当需要重新同步历史数据时，可以重置游标位置。

**⚠️ 警告**：重置游标会导致重新拉取历史数据，可能产生重复数据（依赖 upsert 幂等性）。

**推荐方式（Admin CLI）**：

```bash
# 列出所有游标
engram-scm-sync admin cursors list

# 按 key 前缀过滤
engram-scm-sync admin cursors list --key-prefix "cursor:123"

# 获取特定游标
engram-scm-sync admin cursors get --repo-id 123 --job-type commits

# 删除游标（触发全量同步）
engram-scm-sync admin cursors delete --repo-id 123 --job-type commits

# 设置游标到特定时间点
engram-scm-sync admin cursors set --repo-id 123 --job-type commits --value '{"watermark": "2025-01-01T00:00:00Z", "run_id": "manual_reset"}'

# JSON 输出
engram-scm-sync admin cursors list --json
```

**脚本方式**（仅在需要自定义逻辑时使用）：

```bash
# 查看所有游标
python -c "
import db as db_api
with db_api.get_connection() as conn:
    with conn.cursor() as cur:
        cur.execute(\"SELECT key, value_json FROM logbook.kv WHERE namespace = 'scm.sync' AND key LIKE '%_cursor:%'\")
        for row in cur.fetchall():
            print(f\"key={row[0]}, value={row[1]}\")"
```

**SQL 方式**：

```sql
-- 查看游标
SELECT * FROM logbook.kv 
WHERE namespace = 'scm.sync' AND key LIKE '%_cursor:%';

-- 删除游标（触发全量同步）
DELETE FROM logbook.kv 
WHERE namespace = 'scm.sync' AND key = 'gitlab_cursor:<REPO_ID>';

-- 设置游标到特定时间
UPDATE logbook.kv 
SET value_json = '{"watermark": "2024-01-01T00:00:00Z", "run_id": "manual_reset"}'::jsonb
WHERE namespace = 'scm.sync' AND key = 'gitlab_cursor:<REPO_ID>';
```

---

## 统一故障处置

本节提供常见故障的快速处置命令矩阵，便于运维人员快速定位和处理问题。**所有操作推荐使用 Admin CLI**，支持 `--dry-run` 模拟运行和 `--json` 结构化输出。

### 故障处置快速参考表

| 故障类型 | 诊断命令 | 处置命令 |
|----------|----------|----------|
| **Dead 任务堆积** | `engram-scm-sync admin jobs list --status dead` | `engram-scm-sync admin jobs reset-dead` |
| **锁泄漏/Worker 异常退出** | `engram-scm-sync admin locks list-expired` | `engram-scm-sync admin locks force-release --lock-id <ID>` |
| **任务暂停/恢复** | `engram-scm-sync admin pauses list` | `engram-scm-sync admin pauses set/unset ...` |
| **游标重置** | `engram-scm-sync admin cursors get --repo-id <ID>` | `engram-scm-sync admin cursors delete --repo-id <ID>` |
| **Rate Limit 触发** | `engram-scm-sync admin rate-limit buckets list` | `engram-scm-sync admin rate-limit buckets pause/unpause ...` |
| **熔断器打开** | `engram-scm-status --json \| jq '.circuit_breakers'` | 修复根因后手动关闭（见下文） |

### Dead 任务重置

Dead 任务是重试次数耗尽后标记为不可恢复的任务。处置前需分析原因：

| 场景 | 原因分析 | 处置建议 |
|------|----------|----------|
| 网络恢复 | 临时网络故障已恢复 | 直接重置 |
| 服务恢复 | GitLab/SVN 维护结束 | 直接重置 |
| 配置错误 | Token/权限问题 | 先修复配置，再重置 |
| 仓库不存在 | 仓库已删除 | 删除任务（mark-dead），不重置 |

```bash
# 1. 查看 dead 任务
engram-scm-sync admin jobs list --status dead --json

# 2. 模拟重置（不实际执行）
engram-scm-sync admin jobs reset-dead --dry-run

# 3. 重置所有 dead 任务
engram-scm-sync admin jobs reset-dead

# 4. 重置指定仓库的 dead 任务
engram-scm-sync admin jobs reset-dead --repo-id 123

# 5. 将问题任务标记为 dead（不再重试）
engram-scm-sync admin jobs mark-dead --job-id job-456 --reason "repo_deleted"
```

### 锁释放

当 Worker 异常退出时，可能遗留未释放的锁，导致任务无法被其他 Worker 获取。

```bash
# 1. 列出过期锁
engram-scm-sync admin locks list-expired

# 2. 带宽限期列出
engram-scm-sync admin locks list-expired --grace-seconds 60

# 3. 强制释放指定锁
engram-scm-sync admin locks force-release --lock-id 456
```

### 暂停与恢复

暂停特定仓库或任务类型的同步，用于维护或故障隔离。

```bash
# 1. 列出当前暂停
engram-scm-sync admin pauses list

# 2. 暂停指定仓库的 commits 同步（1 小时）
engram-scm-sync admin pauses set --repo-id 123 --job-type commits --duration 3600 --reason "maintenance"

# 3. 取消暂停
engram-scm-sync admin pauses unset --repo-id 123 --job-type commits
```

### 游标重置

重置游标会触发重新拉取历史数据。**警告**：可能产生重复数据（依赖 upsert 幂等性）。

```bash
# 1. 查看当前游标
engram-scm-sync admin cursors get --repo-id 123 --job-type commits

# 2. 删除游标（触发全量同步）
engram-scm-sync admin cursors delete --repo-id 123 --job-type commits

# 3. 设置游标到特定时间点
engram-scm-sync admin cursors set --repo-id 123 --job-type commits \
    --value '{"watermark": "2025-01-01T00:00:00Z", "run_id": "manual_reset"}'
```

### Rate Limit Bucket 处理

当 GitLab API 返回 429 过多时，可暂停特定实例的请求桶。

```bash
# 1. 列出所有桶状态
engram-scm-sync admin rate-limit buckets list --json

# 2. 暂停指定实例（等待限流窗口过期）
engram-scm-sync admin rate-limit buckets pause \
    --instance-key "gitlab.example.com" \
    --duration 300 \
    --reason "rate_limit_429"

# 3. 取消暂停
engram-scm-sync admin rate-limit buckets unpause --instance-key "gitlab.example.com"
```

### 熔断器手动控制

熔断器通常自动管理，但紧急情况下可手动控制。

**查看熔断状态**：
```bash
engram-scm-status --json | jq '.circuit_breakers'
```

**强制关闭熔断（恢复同步）**：
```sql
-- 方式 1: 更新状态为 closed
UPDATE logbook.kv 
SET value_json = jsonb_set(
    COALESCE(value_json::jsonb, '{}'::jsonb),
    '{state}',
    '"closed"'
)
WHERE namespace = 'scm.sync_health' AND key = 'default:global';

-- 方式 2: 完全删除状态（重置）
DELETE FROM logbook.kv 
WHERE namespace = 'scm.sync_health' AND key = 'default:global';
```

---

## 常见故障处理

### 任务堆积

**症状**：pending 任务持续增加，处理速度跟不上入队速度。

**诊断**：

```bash
# 检查队列状态
engram-scm-status --json | jq '.jobs_by_status'

# 检查 Worker 是否正常运行
ps aux | grep engram-scm-worker

# 检查是否被熔断
engram-scm-status --json | jq '.circuit_breakers'
```

**处理步骤**：

1. **检查 Worker 健康**：
   ```bash
   # 查看 Worker 日志
   docker logs scm_sync_worker --tail 100
   ```

2. **增加 Worker 实例**：
   ```bash
   # 启动额外 Worker
   docker compose up -d --scale scm_sync_worker=3
   ```

3. **调整并发限制**：
   ```bash
   # 临时增加并发
   export SCM_SCHEDULER_MAX_RUNNING=20
   export SCM_SCHEDULER_GLOBAL_CONCURRENCY=40
   ```

4. **清理低优先级任务**：
   ```sql
   -- 删除长期 pending 的低优先级任务
   DELETE FROM scm.sync_jobs 
   WHERE status = 'pending' 
     AND created_at < NOW() - INTERVAL '7 days'
     AND priority > 500;
   ```

### Rate Limit 429 频繁

**症状**：大量任务因 429 失败，GitLab API 限流。

**诊断**：

```bash
# 检查 429 统计
engram-scm-status --json | jq '.error_budget.rate_limit_429'

# 检查 Rate Limit Bucket 状态（使用 admin CLI）
engram-scm-sync admin rate-limit buckets list

# JSON 格式
engram-scm-sync admin rate-limit buckets list --json

# 暂停特定实例的桶（等待限流窗口过期）
engram-scm-sync admin rate-limit buckets pause --instance-key "gitlab.example.com" --duration 300 --reason "rate_limit_429"

# 取消暂停
engram-scm-sync admin rate-limit buckets unpause --instance-key "gitlab.example.com"
```

**处理步骤**：

1. **降低请求频率**：
   ```bash
   # 减少每实例并发
   export SCM_SCHEDULER_PER_INSTANCE_CONCURRENCY=2
   
   # 增加扫描间隔
   export SCM_SCHEDULER_SCAN_INTERVAL_SECONDS=120
   ```

2. **启用 Rate Limit 保护**：
   ```bash
   # 启用 Postgres 分布式限流
   export SCM_GITLAB_POSTGRES_RATE_LIMIT_ENABLED=true
   export SCM_GITLAB_POSTGRES_RATE_LIMIT_RATE=5.0
   export SCM_GITLAB_POSTGRES_RATE_LIMIT_BURST=10
   ```

3. **调整降级策略**：
   ```bash
   # 降低 batch_size
   export SCM_CB_DEGRADED_BATCH_SIZE=10
   
   # 启用平滑策略减少抖动
   export SCM_CB_ENABLE_SMOOTHING=true
   export SCM_CB_SMOOTHING_ALPHA=0.3
   ```

4. **暂时暂停同步**：
   ```bash
   # 强制熔断，等待限流窗口过期
   # 参见"暂停与恢复熔断"章节
   ```

### 熔断器持续 OPEN

**症状**：熔断器长时间处于 OPEN 状态，无法自动恢复。

**诊断**：

```bash
# 查看熔断原因
python -c "
import json
from engram.logbook import scm_db
with scm_db.get_connection() as conn:
    with conn.cursor() as cur:
        cur.execute(\"SELECT key, value_json FROM logbook.kv WHERE namespace = 'scm.sync_health'\")
        for row in cur.fetchall():
            state = row[1] if isinstance(row[1], dict) else json.loads(row[1])
            print(f\"reason: {state.get('last_failure_reason')}\")"

# 查看最近运行的错误
engram-scm-status --json | jq '.error_budget'
```

**处理步骤**：

1. **分析根因**：
   - 失败率高：检查 GitLab/SVN 服务状态
   - 429 率高：参见"Rate Limit 429 频繁"
   - 超时率高：检查网络或调整超时配置

2. **修复根因后强制恢复**：
   ```bash
   # 强制关闭熔断器
   # 参见"暂停与恢复熔断"章节
   ```

3. **调整熔断阈值**（如果阈值过于敏感）：
   ```bash
   # 提高失败率阈值
   export SCM_CB_FAILURE_RATE_THRESHOLD=0.5
   
   # 增加最小样本数
   export SCM_CB_MIN_SAMPLES=10
   
   # 缩短熔断时长
   export SCM_CB_OPEN_DURATION_SECONDS=180
   ```

### Worker 卡死

**症状**：Worker 进程存在但不处理任务，任务持续为 running 状态。

**诊断**：

```bash
# 检查 running 任务时长
python -c "
import db as db_api
with db_api.get_connection() as conn:
    with conn.cursor() as cur:
        cur.execute('''
            SELECT job_id, repo_id, locked_by, locked_at, 
                   EXTRACT(EPOCH FROM (NOW() - locked_at)) AS running_seconds
            FROM scm.sync_jobs 
            WHERE status = 'running'
            ORDER BY locked_at
        ''')
        for row in cur.fetchall():
            print(f\"job_id={row[0]}, repo_id={row[1]}, worker={row[2]}, running={row[4]:.0f}s\")"

# 检查 Worker 进程状态
ps aux | grep scm_sync_worker
```

**处理步骤**：

1. **重启卡死的 Worker**：
   ```bash
   docker restart scm_sync_worker
   ```

2. **强制释放锁和任务**：
   ```bash
   # 释放过期锁
   # 参见"强制释放锁"章节
   
   # 重置卡住的任务
   python -c "
   import db as db_api
   with db_api.get_connection() as conn:
       with conn.cursor() as cur:
           cur.execute('''
               UPDATE scm.sync_jobs 
               SET status = 'pending', locked_by = NULL, locked_at = NULL, not_before = NOW()
               WHERE status = 'running' 
                 AND locked_at < NOW() - INTERVAL '1800 seconds'
               RETURNING job_id
           ''')
           for row in cur.fetchall():
               print(f'Reset job_id={row[0]}')
           conn.commit()
   "
   ```

3. **增加租约时长**（如果任务确实需要长时间运行）：
   ```bash
   export SCM_WORKER_LEASE_SECONDS=600
   ```

---

## 监控告警

### 关键指标

| 指标 | 告警阈值 | 说明 |
|------|----------|------|
| `scm_jobs_by_status{status="pending"}` | > 50 | 任务堆积 |
| `scm_jobs_by_status{status="dead"}` | > 0 | 需人工处理 |
| `scm_expired_locks` | > 0 | 可能有 Worker 异常 |
| `scm_error_budget_failure_rate` | > 0.3 | 错误率高 |
| `scm_error_budget_429_rate` | > 0.1 | Rate Limit 问题 |
| `scm_circuit_breaker_state{state="open"}` | = 1 | 熔断中 |
| `scm_cursors_age_seconds` | > 86400 | 游标滞后超过 1 天 |

### Prometheus 告警规则示例

```yaml
groups:
  - name: scm_sync_alerts
    rules:
      - alert: SCMSyncTaskBacklog
        expr: scm_jobs_by_status{status="pending"} > 50
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "SCM Sync 任务堆积"
          description: "Pending 任务数 {{ $value }} 超过阈值"

      - alert: SCMSyncDeadTasks
        expr: scm_jobs_by_status{status="dead"} > 0
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "存在 Dead 任务需要人工处理"
          description: "Dead 任务数: {{ $value }}"

      - alert: SCMSyncCircuitOpen
        expr: scm_circuit_breaker_state{state="open"} == 1
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "SCM Sync 熔断器打开"
          description: "熔断器 {{ $labels.key }} 处于 OPEN 状态"

      - alert: SCMSyncHighErrorRate
        expr: scm_error_budget_failure_rate > 0.3
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "SCM Sync 错误率过高"
          description: "错误率 {{ $value | humanizePercentage }}"

      - alert: SCMSyncRateLimited
        expr: scm_error_budget_429_rate > 0.1
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "GitLab API 限流"
          description: "429 命中率 {{ $value | humanizePercentage }}"
```

---

## 参考配置示例

### 完整 .env 配置

```bash
# ============ SCM Sync 配置 ============

# --- 功能开关 ---
ENGRAM_SCM_SYNC_ENABLED=true

# --- Scheduler 配置 ---
SCM_SCHEDULER_GLOBAL_CONCURRENCY=20
SCM_SCHEDULER_MAX_RUNNING=10
SCM_SCHEDULER_PER_INSTANCE_CONCURRENCY=5
SCM_SCHEDULER_PER_TENANT_CONCURRENCY=3
SCM_SCHEDULER_SCAN_INTERVAL_SECONDS=30
SCM_SCHEDULER_MAX_ENQUEUE_PER_SCAN=100
SCM_SCHEDULER_ERROR_BUDGET_THRESHOLD=0.3
SCM_SCHEDULER_PAUSE_DURATION_SECONDS=300
SCM_SCHEDULER_CURSOR_AGE_THRESHOLD_SECONDS=3600
SCM_SCHEDULER_BACKFILL_REPAIR_WINDOW_HOURS=24

# 公平调度（可选）
SCM_SCHEDULER_ENABLE_TENANT_FAIRNESS=false
SCM_SCHEDULER_TENANT_FAIRNESS_MAX_PER_ROUND=1

# MVP 模式（可选）
SCM_SCHEDULER_MVP_MODE_ENABLED=false
SCM_SCHEDULER_MVP_JOB_TYPE_ALLOWLIST=commits

# --- Worker 配置 ---
SCM_WORKER_LEASE_SECONDS=300
SCM_WORKER_RENEW_INTERVAL_SECONDS=60
SCM_WORKER_MAX_RENEW_FAILURES=3
SCM_WORKER_POLL_INTERVAL=10
SCM_WORKER_PARALLELISM=2
SCM_WORKER_BATCH_SIZE=50

# --- Reaper 配置 ---
SCM_REAPER_INTERVAL_SECONDS=60
SCM_REAPER_JOB_GRACE_SECONDS=60
SCM_REAPER_LOCK_GRACE_SECONDS=120

# --- 熔断器配置 ---
SCM_CB_FAILURE_RATE_THRESHOLD=0.3
SCM_CB_RATE_LIMIT_THRESHOLD=0.2
SCM_CB_TIMEOUT_RATE_THRESHOLD=0.2
SCM_CB_MIN_SAMPLES=5
SCM_CB_ENABLE_SMOOTHING=true
SCM_CB_SMOOTHING_ALPHA=0.5
SCM_CB_WINDOW_COUNT=20
SCM_CB_WINDOW_MINUTES=30
SCM_CB_OPEN_DURATION_SECONDS=300
SCM_CB_HALF_OPEN_MAX_REQUESTS=3
SCM_CB_RECOVERY_SUCCESS_COUNT=2
SCM_CB_DEGRADED_BATCH_SIZE=10
SCM_CB_BACKFILL_ONLY_MODE=true

# --- GitLab 凭证 ---
GITLAB_URL=https://gitlab.example.com
GITLAB_TOKEN=your_gitlab_token_here

# --- SVN 凭证（可选）---
# SVN_USERNAME=svn_user
# SVN_PASSWORD=svn_password
```

### TOML 配置文件示例

```toml
[scm.scheduler]
global_concurrency = 20
max_running = 10
per_instance_concurrency = 5
per_tenant_concurrency = 3
scan_interval_seconds = 30
max_enqueue_per_scan = 100
error_budget_threshold = 0.3
pause_duration_seconds = 300
cursor_age_threshold_seconds = 3600
backfill_repair_window_hours = 24
enable_tenant_fairness = false

[scm.worker]
lease_seconds = 300
renew_interval_seconds = 60
max_renew_failures = 3

[scm.circuit_breaker]
failure_rate_threshold = 0.3
rate_limit_threshold = 0.2
timeout_rate_threshold = 0.2
min_samples = 5
enable_smoothing = true
smoothing_alpha = 0.5
window_count = 20
window_minutes = 30
open_duration_seconds = 300
half_open_max_requests = 3
recovery_success_count = 2
degraded_batch_size = 10
backfill_only_mode = true

[scm.degradation]
min_batch_size = 10
max_batch_size = 500
default_batch_size = 100
batch_shrink_factor = 0.5
batch_grow_factor = 1.2
min_forward_window_seconds = 300
max_forward_window_seconds = 86400
rate_limit_threshold = 3
timeout_threshold = 3
base_sleep_seconds = 1.0
max_sleep_seconds = 300.0
recovery_success_count = 5

[scm.gitlab]
url = "https://gitlab.example.com"
# token 从环境变量 GITLAB_TOKEN 读取
```

---

## 环境变量对齐

本节汇总 SCM Sync 相关的环境变量，与 [环境变量参考](../reference/environment_variables.md#scm-同步服务) 保持一致。

### 敏感变量声明

> **⚠️ 安全警告**：以下变量包含敏感凭证，**不提供默认值**。必须通过 `.env` 文件或环境变量显式设置。**切勿将凭证提交到版本控制系统**。

| 变量 | 说明 | 敏感 | 必填条件 |
|------|------|:----:|----------|
| `GITLAB_URL` | GitLab 服务地址 | | 使用 GitLab 时 |
| `GITLAB_TOKEN` | GitLab 访问令牌 | ✅ | 使用 GitLab 时 |
| `GITLAB_PRIVATE_TOKEN` | GitLab 私有令牌（`GITLAB_TOKEN` 别名） | ✅ | |
| `SVN_USERNAME` | SVN 用户名 | | 使用 SVN 时 |
| `SVN_PASSWORD` | SVN 密码 | ✅ | 使用 SVN 时 |

### 数据库连接

SCM CLI 工具按以下优先级获取数据库连接：

1. `--dsn` 命令行参数（最高优先级）
2. `--config` 指定的配置文件中的 `[postgres].dsn`
3. `POSTGRES_DSN` 环境变量
4. `ENGRAM_LOGBOOK_CONFIG` 指定的配置文件

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `POSTGRES_DSN` | PostgreSQL 连接字符串 | - |
| `ENGRAM_LOGBOOK_CONFIG` | 配置文件路径（TOML 格式） | `.agentx/config.toml` |

### Scheduler 变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SCM_SCHEDULER_MAX_RUNNING` | `5` | 全局最大运行任务数 |
| `SCM_SCHEDULER_GLOBAL_CONCURRENCY` | `10` | 全局最大队列深度 |
| `SCM_SCHEDULER_PER_INSTANCE_CONCURRENCY` | `3` | 单实例并发数 |
| `SCM_SCHEDULER_PER_TENANT_CONCURRENCY` | `5` | 单租户并发数 |
| `SCM_SCHEDULER_SCAN_INTERVAL_SECONDS` | `60` | 扫描间隔（秒） |
| `SCM_SCHEDULER_MAX_ENQUEUE_PER_SCAN` | `100` | 单次入队最大数 |
| `SCM_SCHEDULER_ERROR_BUDGET_THRESHOLD` | `0.3` | 错误预算阈值 |
| `SCM_SCHEDULER_PAUSE_DURATION_SECONDS` | `300` | 暂停持续时间（秒） |
| `SCM_SCHEDULER_ENABLE_TENANT_FAIRNESS` | `false` | 启用 Tenant 公平调度 |
| `SCM_SCHEDULER_MVP_MODE_ENABLED` | `false` | 启用 MVP 模式 |
| `SCM_SCHEDULER_LOG_LEVEL` | `INFO` | 日志级别 |

### Worker 变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SCM_WORKER_LEASE_SECONDS` | `300` | 任务租约时长（秒） |
| `SCM_WORKER_RENEW_INTERVAL_SECONDS` | `60` | 租约续期间隔（秒） |
| `SCM_WORKER_MAX_RENEW_FAILURES` | `3` | 最大续期失败次数 |
| `SCM_WORKER_POLL_INTERVAL` | `10` | 轮询间隔（秒） |
| `SCM_WORKER_PARALLELISM` | `1` | 内部并行度 |
| `SCM_WORKER_BATCH_SIZE` | `50` | 批处理大小 |
| `SCM_WORKER_LOCK_TIMEOUT` | `300` | 分布式锁超时（秒） |
| `SCM_WORKER_LOG_LEVEL` | `INFO` | 日志级别 |

### Reaper 变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SCM_REAPER_INTERVAL_SECONDS` | `60` | 清理间隔（秒） |
| `SCM_REAPER_JOB_GRACE_SECONDS` | `60` | 任务宽限期（秒） |
| `SCM_REAPER_RUN_MAX_SECONDS` | `3600` | 运行最大时长（秒） |
| `SCM_REAPER_LOCK_GRACE_SECONDS` | `120` | 锁宽限期（秒） |
| `SCM_REAPER_LOG_LEVEL` | `INFO` | 日志级别 |

### 熔断器变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SCM_CB_FAILURE_RATE_THRESHOLD` | `0.3` | 失败率阈值 |
| `SCM_CB_RATE_LIMIT_THRESHOLD` | `0.2` | 429 命中率阈值 |
| `SCM_CB_TIMEOUT_RATE_THRESHOLD` | `0.2` | 超时率阈值 |
| `SCM_CB_MIN_SAMPLES` | `5` | 最小样本数（小样本保护） |
| `SCM_CB_ENABLE_SMOOTHING` | `true` | 启用 EMA 平滑 |
| `SCM_CB_SMOOTHING_ALPHA` | `0.5` | EMA 平滑系数 |
| `SCM_CB_OPEN_DURATION_SECONDS` | `300` | 熔断持续时间（秒） |
| `SCM_CB_HALF_OPEN_MAX_REQUESTS` | `3` | 半开状态最大探测数 |
| `SCM_CB_RECOVERY_SUCCESS_COUNT` | `2` | 恢复所需连续成功数 |
| `SCM_CB_DEGRADED_BATCH_SIZE` | `10` | 熔断时的 batch_size |
| `SCM_CB_BACKFILL_ONLY_MODE` | `true` | 熔断时仅执行 backfill |

> **完整变量列表**：参见 [环境变量参考 - SCM 同步服务](../reference/environment_variables.md#scm-同步服务)

---

## 参考文档

| 文档 | 说明 |
|------|------|
| [06_scm_sync_subsystem.md](./06_scm_sync_subsystem.md) | 子系统架构详解 |
| [01_architecture.md](./01_architecture.md) | 整体架构 |
| [环境变量参考](../reference/environment_variables.md) | 完整变量列表 |
| [CLI 入口清单](../architecture/cli_entrypoints.md) | CLI 命令对照表 |

---

更新时间：2026-01-31
