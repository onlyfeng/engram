# SCM Sync 子系统详解

本文档详细描述 SCM 同步子系统的架构设计、状态机、数据流与配置开关。

> **源码位置**: `src/engram/logbook/scm_sync_*.py`  
> **CLI 入口**: `src/engram/logbook/cli/scm_sync.py`  
> **测试覆盖**: `tests/logbook/test_scm_sync_state_machine_invariants.py`

---

## CLI 入口

SCM Sync 子系统提供统一的 CLI 入口，可通过以下方式调用：

```bash
# 统一入口
python -m engram.logbook.cli.scm_sync <子命令> [args]

# 或使用 console_scripts（需要 pip install -e .）
engram-scm-sync <子命令> [args]

# 子命令快捷入口
engram-scm-scheduler [args]   # 调度器
engram-scm-worker [args]      # Worker
engram-scm-reaper [args]      # 清理器
engram-scm-status [args]      # 状态查询
```

### 子命令示例

```bash
# Scheduler - 执行一次调度
python -m engram.logbook.cli.scm_sync scheduler --once --dry-run

# Worker - 启动 worker
python -m engram.logbook.cli.scm_sync worker --worker-id worker-1

# Reaper - 回收过期任务
python -m engram.logbook.cli.scm_sync reaper --dry-run

# Status - 查看状态
python -m engram.logbook.cli.scm_sync status --json
```

---

## 目录

- [架构概览](#架构概览)
- [核心模块](#核心模块)
- [数据流](#数据流)
- [状态机](#状态机)
  - [Job 状态机（sync_jobs）](#job-状态机sync_jobs)
  - [熔断器状态机](#熔断器状态机)
- [不变量清单](#不变量清单)
- [配置开关与环境变量](#配置开关与环境变量)
  - [Scheduler 配置](#scheduler-配置)
  - [Worker 配置](#worker-配置)
  - [熔断器配置](#熔断器配置)
  - [降级策略配置](#降级策略配置)

---

## 架构概览

SCM Sync 子系统采用 **Scheduler → Queue → Worker → Runs → Status → Reaper** 的流水线架构，实现可靠的增量同步。

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              SCM Sync 子系统                                 │
│                                                                             │
│  ┌─────────────┐   enqueue   ┌─────────────┐   claim    ┌─────────────┐    │
│  │  Scheduler  │────────────▶│   Queue     │───────────▶│   Worker    │    │
│  │  (策略层)    │             │ (sync_jobs) │            │  (执行层)    │    │
│  └─────────────┘             └─────────────┘            └─────────────┘    │
│        │                            │                          │           │
│        │ 熔断检查                    │                          │ ack/fail  │
│        ▼                            │                          ▼           │
│  ┌─────────────┐             ┌──────┴──────┐            ┌─────────────┐    │
│  │ Circuit     │◀────────────│  logbook.kv │◀───────────│ sync_runs   │    │
│  │ Breaker     │   健康统计   │  (cursors)  │   写入结果   │ (运行记录)   │    │
│  └─────────────┘             └─────────────┘            └─────────────┘    │
│        │                                                       │           │
│        │ 熔断状态                                               │           │
│        ▼                                                       ▼           │
│  ┌─────────────┐                                        ┌─────────────┐    │
│  │ Rate Limit  │                                        │   Reaper    │    │
│  │ Buckets     │                                        │  (清理器)    │    │
│  └─────────────┘                                        └─────────────┘    │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 核心模块

| 模块 | 文件 | 职责 |
|------|------|------|
| **Scheduler Core** | `scm_sync_scheduler_core.py` | 调度器核心逻辑：构建任务、执行调度 tick |
| **Worker Core** | `scm_sync_worker_core.py` | Worker 核心逻辑：任务获取、执行、心跳续租 |
| **Reaper Core** | `scm_sync_reaper_core.py` | 清理器核心逻辑：回收过期任务、runs、锁 |
| **Executor** | `scm_sync_executor.py` | 执行器层：job_type 到 handler 的映射和调度 |
| **Status** | `scm_sync_status.py` | 状态摘要与 Prometheus 指标输出 |
| **Policy** | `scm_sync_policy.py` | 调度策略纯函数：候选选择、优先级计算、熔断决策、降级建议 |
| **Queue** | `scm_sync_queue.py` | 任务队列：enqueue/claim/ack/fail 操作，基于 PostgreSQL 的可靠队列 |
| **Lock** | `scm_sync_lock.py` | 分布式锁：确保单仓库同一时间只有一个 worker 执行 |
| **Payload** | `scm_sync_payload.py` | Payload 契约：定义 `sync_jobs.payload_json` 的字段规范 |
| **Run Contract** | `scm_sync_run_contract.py` | 运行记录契约：统一 worker 的 run start/finish 写入格式 |
| **CLI** | `cli/scm_sync.py` | 统一 CLI 入口：scheduler/worker/reaper/status 子命令 |

---

## 数据流

### 1. Scheduler → Queue（任务入队）

```
Scheduler
    │
    ├── 1. 扫描 scm.repos（获取待同步仓库）
    │
    ├── 2. 查询 logbook.kv（获取 cursor 位置、健康统计）
    │
    ├── 3. 调用 select_jobs_to_enqueue()（纯函数）
    │       ├── 检查 queued_pairs（避免重复入队）
    │       ├── 检查 budget_snapshot（并发预算）
    │       ├── 检查 bucket_statuses（Rate Limit 暂停）
    │       ├── 计算优先级（游标年龄、失败率、429 命中率）
    │       └── 应用 MVP/Tenant 公平调度策略
    │
    └── 4. 调用 enqueue()（写入 scm.sync_jobs）
```

### 2. Queue → Worker（任务获取与执行）

```
Worker
    │
    ├── 1. 调用 claim()（FOR UPDATE SKIP LOCKED）
    │       ├── 条件: pending + not_before <= now
    │       ├── 或: running + 锁过期
    │       ├── 或: failed + 可重试
    │       └── 可选: instance_allowlist / tenant_allowlist 过滤
    │
    ├── 2. 获取分布式锁 scm_sync_lock.claim()
    │       └── 失败则调用 requeue_without_penalty()
    │
    ├── 3. 执行同步（GitLab/SVN API 调用）
    │       ├── 定期调用 renew_lease()（续租）
    │       └── 写入 scm.* 表（commits/mrs/review_events）
    │
    ├── 4. 写入 scm.sync_runs（运行记录）
    │       └── 使用 build_run_finish_payload() 构建契约
    │
    └── 5. 完成处理
            ├── 成功: ack() → 更新 cursor
            ├── 失败: fail_retry() → 指数退避
            └── 死信: mark_dead() → 不再重试
```

### 3. Status → Prometheus（监控输出）

```
scm_sync_status.get_sync_summary()
    │
    ├── 聚合 scm.sync_jobs（按状态分组）
    ├── 聚合 scm.sync_runs（24h 统计）
    ├── 加载 logbook.kv（熔断状态）
    ├── 加载 scm.sync_rate_limits（Rate Limit Bucket）
    └── 加载 logbook.kv（暂停状态）
            │
            ▼
format_prometheus_metrics() → Prometheus 格式输出
```

### 4. Reaper（清理器）

```
Reaper（定期运行）
    │
    ├── 清理 running 但锁过期的任务（租约回收）
    ├── 清理完成的旧任务（cleanup_completed_jobs）
    └── 清理孤立锁（list_expired_locks + force_release）
```

---

## 状态机

### Job 状态机（sync_jobs）

> **参考测试**: `tests/logbook/test_scm_sync_state_machine_invariants.py`

#### 状态转换矩阵

| 源状态 | 目标状态 | 操作 | 条件 |
|--------|----------|------|------|
| (新建) | `pending` | `enqueue` | `(repo_id, job_type, mode)` 无活跃任务 |
| `pending` | `running` | `claim` | `not_before <= now()` |
| `running` | `running` | `claim`（抢占） | `locked_at + lease_seconds < now()` |
| `failed` | `running` | `claim`（重试） | `not_before <= now() AND attempts < max_attempts` |
| `running` | `completed` | `ack` | `locked_by = worker_id AND status = running` |
| `running` | `failed` | `fail_retry` | `locked_by = worker_id AND attempts < max_attempts` |
| `running` | `dead` | `fail_retry` | `locked_by = worker_id AND attempts >= max_attempts` |
| `running` | `dead` | `mark_dead` | `locked_by = worker_id` |
| `running` | `pending` | `requeue_without_penalty` | `locked_by = worker_id`（attempts -1 补偿） |
| `dead` | `pending` | `reset_dead_jobs` | 管理员操作（attempts 重置为 0） |

#### 状态流转图

```
                    ┌──────────────────────────────────┐
                    │            enqueue()             │
                    └──────────────┬───────────────────┘
                                   ▼
                            ┌─────────────┐
                            │   pending   │◀────────────────────────┐
                            └──────┬──────┘                         │
                                   │ claim()                        │
                                   │ not_before <= now()            │
                                   ▼                                │
     ┌────────────────────┬───────────────┬──────────────────┐      │
     │                    │               │                  │      │
     │ requeue_without_   │               │                  │      │
     │ penalty()          │               │                  │      │
     │ (attempts -1)      │               │                  │      │
     │                    ▼               ▼                  ▼      │
     │              ┌─────────────┐ fail_retry()      mark_dead()   │
     │              │   running   │──────────────┬──────────────┐   │
     │              └──────┬──────┘              │              │   │
     │                     │                     ▼              ▼   │
     │                     │              ┌─────────────┐ ┌───────────┐
     │                     │              │   failed    │ │   dead    │
     │                     │              └──────┬──────┘ └───────────┘
     │                     │                     │              │
     │                     │ ack()               │ claim()      │ reset_dead_jobs()
     │                     │                     │ (重试)        │ (管理员)
     │                     ▼                     │              │
     │              ┌─────────────┐              │              │
     └──────────────│  completed  │◀─────────────┴──────────────┘
                    └─────────────┘
```

#### 字段语义

| 字段 | 类型 | 说明 |
|------|------|------|
| `status` | `enum` | `pending` / `running` / `completed` / `failed` / `dead` |
| `attempts` | `int` | 已尝试次数（claim 时 +1，requeue 时 -1） |
| `not_before` | `timestamp` | 任务在此时间之前不会被 claim |
| `locked_by` | `varchar` | 当前持有锁的 worker 标识符 |
| `locked_at` | `timestamp` | 锁定时间（用于判断租约过期） |
| `lease_seconds` | `int` | 租约时长（默认 300 秒） |
| `gitlab_instance` | `varchar` | GitLab 实例（用于 claim 过滤） |
| `tenant_id` | `varchar` | 租户 ID（用于公平调度） |

#### 并发边界条件

| 场景 | 保证机制 |
|------|----------|
| 并发 claim | `FOR UPDATE SKIP LOCKED` 确保原子性 |
| 重复 ack | 状态非 running 或 locked_by 不匹配返回 False |
| 过期租约 | running 但 `locked_at + lease_seconds < now()` 可被重新 claim |
| Worker 身份验证 | ack/fail_retry/mark_dead 要求 `locked_by = worker_id` |

---

### 熔断器状态机

#### 状态枚举

```python
class CircuitState(str, Enum):
    CLOSED = "closed"      # 正常状态，允许全量同步
    HALF_OPEN = "half_open"  # 半开状态，探测性恢复
    OPEN = "open"          # 熔断状态，仅 backfill 或暂停
```

#### 状态转换矩阵

| 源状态 | 目标状态 | 触发条件 |
|--------|----------|----------|
| `CLOSED` | `OPEN` | 失败率 >= threshold 或 429 率 >= threshold 或 超时率 >= threshold |
| `OPEN` | `HALF_OPEN` | 经过 `open_duration_seconds` 后 |
| `HALF_OPEN` | `CLOSED` | 连续 `recovery_success_count` 次成功 |
| `HALF_OPEN` | `OPEN` | 任意一次失败 |

#### 状态流转图

```
                         失败率/429率/超时率 >= threshold
                    ┌─────────────────────────────────────────┐
                    │                                         │
                    ▼                                         │
             ┌─────────────┐      open_duration_seconds  ┌─────────────┐
             │    OPEN     │─────────────────────────────▶│  HALF_OPEN  │
             │  (熔断中)    │◀────────────────────────────│  (探测中)    │
             └─────────────┘      任意失败                └──────┬──────┘
                                                                │
                                                                │ 连续成功
                                                                │ >= recovery_success_count
                                                                ▼
                                                         ┌─────────────┐
                                                         │   CLOSED    │
                                                         │  (正常)     │
                                                         └─────────────┘
```

#### 小样本保护与平滑策略

```python
# 小样本保护：样本数 < min_samples 时不触发熔断
if total_runs < config.min_samples:
    return (False, None)  # 跳过熔断检查

# EMA 平滑：减少抖动
smoothed_value = alpha * current_value + (1 - alpha) * previous_smoothed
```

---

## 不变量清单

> **测试覆盖**: `tests/logbook/test_scm_sync_state_machine_invariants.py`

| 不变量 | 说明 | 漂移风险 |
|--------|------|----------|
| **INV-1** | 同一 `(repo_id, job_type)` 不会重复入队 | `queued_pairs` 检查失效 |
| **INV-2** | 预算超限时不入队新任务 | 预算检查逻辑 bug |
| **INV-3** | Bucket 暂停状态正确应用 priority penalty | penalty 计算错误 |
| **INV-4** | Bucket 暂停时 `skip_on_pause=True` 跳过任务 | 跳过逻辑失效 |
| **INV-5** | 熔断器状态正确持久化和恢复 | 状态丢失 |
| **INV-5a** | skipped 结果不影响熔断状态 | 探测计数错误 |
| **INV-6** | 并发限制按 instance/tenant 正确应用 | 限制逻辑错误 |
| **INV-7** | 优先级排序稳定（相同优先级按创建时间） | 排序不稳定 |
| **INV-8** | Tenant 公平调度策略正确轮询 | 大 tenant 饥饿其他 tenant |

---

## 配置开关与环境变量

### Scheduler 配置

> **模块**: `scm_sync_policy.SchedulerConfig`  
> **环境变量前缀**: `SCM_SCHEDULER_`

| 环境变量 | 配置项 | 类型 | 默认值 | 说明 |
|----------|--------|------|--------|------|
| `SCM_SCHEDULER_MAX_RUNNING` | `max_running` | int | `5` | 全局最大运行任务数 |
| `SCM_SCHEDULER_GLOBAL_CONCURRENCY` | `max_queue_depth` | int | `10` | 全局最大队列深度 |
| `SCM_SCHEDULER_PER_INSTANCE_CONCURRENCY` | `per_instance_concurrency` | int | `3` | 每 GitLab 实例并发限制 |
| `SCM_SCHEDULER_PER_TENANT_CONCURRENCY` | `per_tenant_concurrency` | int | `5` | 每租户并发限制 |
| `SCM_SCHEDULER_SCAN_INTERVAL_SECONDS` | `scan_interval_seconds` | int | `60` | 扫描间隔（秒） |
| `SCM_SCHEDULER_MAX_ENQUEUE_PER_SCAN` | `max_enqueue_per_scan` | int | `100` | 单次扫描最大入队数 |
| `SCM_SCHEDULER_ERROR_BUDGET_THRESHOLD` | `error_budget_threshold` | float | `0.3` | 错误预算阈值（30%） |
| `SCM_SCHEDULER_PAUSE_DURATION_SECONDS` | `pause_duration_seconds` | int | `300` | 暂停持续时间 |
| `SCM_SCHEDULER_CURSOR_AGE_THRESHOLD_SECONDS` | `cursor_age_threshold_seconds` | int | `3600` | 游标年龄阈值 |
| `SCM_SCHEDULER_BACKFILL_REPAIR_WINDOW_HOURS` | `backfill_repair_window_hours` | int | `24` | 回填修复窗口 |
| `SCM_SCHEDULER_MAX_BACKFILL_WINDOW_HOURS` | `max_backfill_window_hours` | int | `168` | 最大回填窗口（7天） |
| `SCM_SCHEDULER_ENABLE_TENANT_FAIRNESS` | `enable_tenant_fairness` | bool | `false` | 启用 Tenant 公平调度 |
| `SCM_SCHEDULER_TENANT_FAIRNESS_MAX_PER_ROUND` | `tenant_fairness_max_per_round` | int | `1` | 每轮每 tenant 最大入队数 |
| `SCM_SCHEDULER_MVP_MODE_ENABLED` | `mvp_mode_enabled` | bool | `false` | 启用 MVP 模式 |
| `SCM_SCHEDULER_MVP_JOB_TYPE_ALLOWLIST` | `mvp_job_type_allowlist` | list | `["commits"]` | MVP 允许的任务类型 |

### Worker 配置

| 环境变量 | 类型 | 默认值 | 说明 |
|----------|------|--------|------|
| `SCM_WORKER_LEASE_SECONDS` | int | `300` | 任务租约时长 |
| `SCM_WORKER_RENEW_INTERVAL_SECONDS` | int | `60` | 租约续期间隔 |
| `SCM_WORKER_MAX_RENEW_FAILURES` | int | `3` | 最大续期失败次数 |
| `SCM_WORKER_POLL_INTERVAL` | int | `10` | 轮询间隔 |
| `SCM_WORKER_PARALLELISM` | int | `1` | 内部并行度 |
| `SCM_WORKER_BATCH_SIZE` | int | `50` | 批处理大小 |
| `SCM_WORKER_LOCK_TIMEOUT` | int | `300` | 分布式锁超时 |

### 熔断器配置

> **模块**: `scm_sync_policy.CircuitBreakerConfig`  
> **环境变量前缀**: `SCM_CB_`

| 环境变量 | 配置项 | 类型 | 默认值 | 说明 |
|----------|--------|------|--------|------|
| `SCM_CB_FAILURE_RATE_THRESHOLD` | `failure_rate_threshold` | float | `0.3` | 失败率阈值（30%） |
| `SCM_CB_RATE_LIMIT_THRESHOLD` | `rate_limit_threshold` | float | `0.2` | 429 命中率阈值（20%） |
| `SCM_CB_TIMEOUT_RATE_THRESHOLD` | `timeout_rate_threshold` | float | `0.2` | 超时率阈值（20%） |
| `SCM_CB_MIN_SAMPLES` | `min_samples` | int | `5` | 最小样本数（小样本保护） |
| `SCM_CB_ENABLE_SMOOTHING` | `enable_smoothing` | bool | `true` | 启用 EMA 平滑 |
| `SCM_CB_SMOOTHING_ALPHA` | `smoothing_alpha` | float | `0.5` | EMA 平滑系数 |
| `SCM_CB_WINDOW_COUNT` | `window_count` | int | `20` | 统计窗口（运行次数） |
| `SCM_CB_WINDOW_MINUTES` | `window_minutes` | int | `30` | 统计窗口（时间） |
| `SCM_CB_OPEN_DURATION_SECONDS` | `open_duration_seconds` | int | `300` | 熔断持续时间 |
| `SCM_CB_HALF_OPEN_MAX_REQUESTS` | `half_open_max_requests` | int | `3` | 半开状态最大探测数 |
| `SCM_CB_RECOVERY_SUCCESS_COUNT` | `recovery_success_count` | int | `2` | 恢复所需连续成功数 |
| `SCM_CB_DEGRADED_BATCH_SIZE` | `degraded_batch_size` | int | `10` | 熔断时的 batch_size |
| `SCM_CB_BACKFILL_ONLY_MODE` | `backfill_only_mode` | bool | `true` | 熔断时仅执行 backfill |
| `SCM_CB_BACKFILL_INTERVAL_SECONDS` | `backfill_interval_seconds` | int | `600` | backfill 间隔 |
| `SCM_CB_PROBE_BUDGET_PER_INTERVAL` | `probe_budget_per_interval` | int | `2` | 探测预算 |
| `SCM_CB_PROBE_JOB_TYPES_ALLOWLIST` | `probe_job_types_allowlist` | list | `["commits"]` | 探测允许的任务类型 |

### 降级策略配置

> **模块**: `scm_sync_policy.DegradationConfig`

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `min_batch_size` | int | `10` | 最小 batch_size |
| `max_batch_size` | int | `500` | 最大 batch_size |
| `default_batch_size` | int | `100` | 默认 batch_size |
| `batch_shrink_factor` | float | `0.5` | batch 缩小因子 |
| `batch_grow_factor` | float | `1.2` | batch 增长因子 |
| `min_forward_window_seconds` | int | `300` | 最小前向窗口（5分钟） |
| `max_forward_window_seconds` | int | `86400` | 最大前向窗口（24小时） |
| `rate_limit_threshold` | int | `3` | 连续 429 触发 diff_mode=none |
| `timeout_threshold` | int | `3` | 连续超时触发暂停 |
| `base_sleep_seconds` | float | `1.0` | 基础退避时间 |
| `max_sleep_seconds` | float | `300.0` | 最大退避时间（5分钟） |
| `recovery_success_count` | int | `5` | 恢复所需连续成功数 |

### Reaper 配置

| 环境变量 | 类型 | 默认值 | 说明 |
|----------|------|--------|------|
| `SCM_REAPER_INTERVAL_SECONDS` | int | `60` | 清理间隔 |
| `SCM_REAPER_JOB_GRACE_SECONDS` | int | `60` | 任务宽限期 |
| `SCM_REAPER_RUN_MAX_SECONDS` | int | `3600` | 运行最大时长 |
| `SCM_REAPER_LOCK_GRACE_SECONDS` | int | `120` | 锁宽限期 |

---

## Payload 契约

> **模块**: `scm_sync_payload.py`

### 关键字段

| 字段 | 说明 | claim 过滤 |
|------|------|------------|
| `gitlab_instance` | GitLab 实例标识 | ✅ |
| `tenant_id` | 租户 ID | ✅ |
| `batch_size` | 批量大小 | |
| `suggested_batch_size` | 熔断降级建议 | |
| `suggested_diff_mode` | 熔断降级建议 | |
| `is_backfill_only` | 是否仅 backfill | |
| `circuit_state` | 熔断状态 | |
| `since` / `until` | backfill 时间窗口 | |
| `start_rev` / `end_rev` | SVN revision 窗口 | |

### 未知字段透传

```python
# Worker 访问未知字段
payload = job["payload"]  # SyncJobPayload
custom_value = payload.extra.get("custom_field", default_value)

# 或使用 dict-like API
custom_value = payload.get("custom_field", default_value)
```

---

## Run Finish 契约

> **模块**: `scm_sync_run_contract.py`

### 运行状态

```python
class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    NO_DATA = "no_data"
```

### 构建函数

| 函数 | 场景 |
|------|------|
| `build_payload_for_success()` | 成功完成 |
| `build_payload_for_no_data()` | 无数据 |
| `build_payload_for_exception()` | 异常退出 |
| `build_payload_for_lease_lost()` | 租约丢失 |
| `build_payload_for_mark_dead()` | 标记死信 |

### 示例

```python
from engram.logbook.scm_sync_run_contract import (
    build_run_finish_payload,
    RunStatus,
)

# 成功场景
payload = build_run_finish_payload(
    status=RunStatus.COMPLETED.value,
    counts={"synced_count": 100, "diff_count": 50},
    cursor_after={"last_commit_ts": "2024-01-15T12:00:00Z"},
)

# 失败场景
payload = build_run_finish_payload(
    status=RunStatus.FAILED.value,
    error_summary={
        "error_category": "timeout",
        "error_message": "Request timed out",
        "backoff_seconds": 60,
    },
)
```

---

## 监控指标

> **模块**: `scm_sync_status.py`

### Prometheus 指标

| 指标 | 说明 |
|------|------|
| `scm_jobs_by_status{status="..."}` | 各状态任务数 |
| `scm_expired_locks` | 过期锁数量 |
| `scm_circuit_breaker_state{key="..."}` | 熔断器状态 |
| `scm_rate_limit_bucket_paused{instance_key="..."}` | Rate Limit 暂停状态 |
| `scm_pauses_by_reason{reason_code="..."}` | 暂停原因分布 |
| `scm_retry_backoff_seconds{...}` | 重试退避秒数 |

---

## 参考文档

- [架构与数据流](./01_architecture.md) - 整体架构
- [SCM Sync 运维指南](./07_scm_sync_ops_guide.md) - 部署与运维操作
- [环境变量参考](../reference/environment_variables.md) - 完整环境变量列表
- [测试不变量](../../tests/logbook/test_scm_sync_state_machine_invariants.py) - 状态机测试

---

## CLI 快速参考

| 命令 | 说明 |
|------|------|
| `engram-scm-sync scheduler --once` | 执行一次调度 |
| `engram-scm-sync scheduler --loop` | 循环模式运行调度器 |
| `engram-scm-sync scheduler --loop --interval-seconds 30` | 循环模式，自定义间隔 |
| `engram-scm-sync scheduler --loop --json` | 循环模式，JSON 输出（便于日志采集） |
| `engram-scm-sync scheduler --dry-run` | 干运行调度 |
| `engram-scm-sync worker --worker-id W1` | 启动 Worker |
| `engram-scm-sync worker --worker-id W1 --once` | 处理单个任务 |
| `engram-scm-sync reaper` | 执行一次清理 |
| `engram-scm-sync reaper --once` | 执行一次清理（显式指定） |
| `engram-scm-sync reaper --loop` | 循环模式运行清理器 |
| `engram-scm-sync reaper --loop --interval-seconds 60 --json` | 循环模式，JSON 输出 |
| `engram-scm-sync reaper --dry-run` | 干运行清理 |
| `engram-scm-sync status --json` | 查看状态（JSON） |
| `engram-scm-sync status --prometheus` | 查看状态（Prometheus） |

### 循环模式说明

Scheduler 和 Reaper 支持 `--loop` 循环模式，适用于作为常驻进程部署：

```bash
# Scheduler 循环模式（默认间隔 60 秒）
engram-scm-sync scheduler --loop

# 自定义间隔
engram-scm-sync scheduler --loop --interval-seconds 30

# 配合 JSON 输出便于日志采集
engram-scm-sync scheduler --loop --json
```

- `--once`：执行一次后退出（默认行为，与 `--loop` 互斥）
- `--loop`：持续循环运行
- `--interval-seconds`：循环间隔秒数（默认 60）
- `--json`：在 `--loop` 模式下每轮输出单行 JSON，便于日志采集系统解析
