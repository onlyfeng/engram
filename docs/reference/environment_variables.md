# 环境变量参考

本文档按组件列出 Engram 各组件使用的环境变量、默认值及依赖关系。

**模板文件**:
- 统一栈 `.env` 示例：[`/.env.example`](../../.env.example)

---

## 目录

- [通用配置](#通用配置)
- [Logbook 组件](#logbook-组件)
- [OpenMemory 组件](#openmemory-组件)
- [Gateway 组件](#gateway-组件)
- [统一栈 (Unified Stack)](#统一栈-unified-stack)
- [MinIO 对象存储](#minio-对象存储)
- [SCM 同步服务](#scm-同步服务)
- [环境变量优先级](#环境变量优先级)
- [废弃变量](#废弃变量)

---

## 通用配置

所有组件共享的核心变量。

| 变量 | 说明 | 默认值 | 必填 |
|------|------|--------|------|
| `PROJECT_KEY` | 项目标识（用于 Logbook 表前缀、多项目隔离） | `default` | 推荐 |
| `POSTGRES_DB` | 数据库名（建议与 `PROJECT_KEY` 一致，实现"每项目一库"隔离） | `engram` | 推荐 |
| `POSTGRES_USER` | PostgreSQL 用户名 | `postgres` | |
| `POSTGRES_PASSWORD` | PostgreSQL 超级用户密码 | `postgres` | 生产必填 |
| `POSTGRES_PORT` | PostgreSQL 端口 | `5432` | |

### 多项目部署示例

```bash
# 项目 A
PROJECT_KEY=proj_a POSTGRES_DB=proj_a make deploy

# 项目 B
PROJECT_KEY=proj_b POSTGRES_DB=proj_b make deploy
```

---

## Logbook 组件

事实账本组件变量。模块路径: `src/engram/logbook/`（脚本入口在 `logbook_postgres/`）

### 数据库连接

| 变量 | 说明 | 默认值 | 必填 |
|------|------|--------|------|
| `POSTGRES_DSN` | PostgreSQL 完整连接字符串 | - | 二选一 |
| `ENGRAM_LOGBOOK_CONFIG` | 配置文件路径（TOML 格式） | `.agentx/config.toml` | 二选一 |

> **注意**: `POSTGRES_DSN` 优先级高于配置文件。

### 服务账号密码

统一栈强制要求设置这些密码，避免使用 postgres 超级用户。

| 变量 | 说明 | 默认值 | 必填 |
|------|------|--------|------|
| `LOGBOOK_MIGRATOR_PASSWORD` | Logbook 迁移账号密码（`logbook_migrator`） | - | 统一栈必填 |
| `LOGBOOK_SVC_PASSWORD` | Logbook 服务账号密码（`logbook_svc`） | - | 统一栈必填 |

### 配置文件格式

配置文件搜索路径（优先级从高到低）：
1. `--config` / `-c` 命令行参数
2. 环境变量 `ENGRAM_LOGBOOK_CONFIG`
3. `./.agentx/config.toml`（工作目录）
4. `~/.agentx/config.toml`（用户目录）

```toml
[postgres]
dsn = "postgresql://logbook_svc:<pwd>@localhost:5432/engram"
pool_min_size = 1
pool_max_size = 10
connect_timeout = 10.0

[project]
project_key = "my_project"
description = "项目描述"

[logging]
level = "INFO"
```

---

## OpenMemory 组件

语义记忆存储与检索组件。默认使用上游镜像 `OPENMEMORY_IMAGE`（无需 vendoring）。

### 服务配置

| 变量 | 说明 | 默认值 | 必填 |
|------|------|--------|------|
| `OM_PORT` | OpenMemory 服务端口 | `8080` | |
| `OM_MODE` | 服务模式（`standard` / `langgraph`） | `standard` | |
| `OM_TIER` | 性能层级（`hybrid` / `fast` / `smart` / `deep`） | `hybrid` | |
| `OM_API_KEY` | API 认证密钥（生产环境强烈建议设置） | - | 生产推荐 |

### 元数据后端

| 变量 | 说明 | 默认值 | 必填 |
|------|------|--------|------|
| `OM_METADATA_BACKEND` | 元数据后端类型（`sqlite` / `postgres`） | `sqlite` | |
| `OM_DB_PATH` | SQLite 数据库路径（当 backend=sqlite） | `/path/to/openmemory.sqlite` | |

### PostgreSQL 连接（当 OM_METADATA_BACKEND=postgres）

| 变量 | 说明 | 默认值 | 必填 |
|------|------|--------|------|
| `OM_PG_HOST` | PostgreSQL 主机 | `localhost` | |
| `OM_PG_PORT` | PostgreSQL 端口 | `5432` | |
| `OM_PG_DB` | 数据库名 | `openmemory` | |
| `OM_PG_USER` | 连接用户 | `postgres` | |
| `OM_PG_PASSWORD` | 连接密码 | `postgres` | |
| `OM_PG_SCHEMA` | Schema 名（⚠️ 禁止设为 `public`） | `openmemory` | |
| `OM_PG_TABLE` | 记忆表名 | `openmemory_memories` | |
| `OM_PG_SSL` | SSL 模式（`disable` / `require`） | `disable` | |
| `OM_PG_AUTO_CREATE_DB` | 是否自动创建数据库 | `false` | |
| `OM_PG_AUTO_DDL` | 是否自动执行 DDL | `false` | |
| `OM_PG_SET_ROLE` | 会话角色切换（迁移用 `openmemory_migrator`，运行时用 `openmemory_app`） | - | |

### 服务账号密码

| 变量 | 说明 | 默认值 | 必填 |
|------|------|--------|------|
| `OPENMEMORY_MIGRATOR_PASSWORD` | OpenMemory 迁移账号密码（`openmemory_migrator_login`） | - | 统一栈必填 |
| `OPENMEMORY_SVC_PASSWORD` | OpenMemory 服务账号密码（`openmemory_svc`） | - | 统一栈必填 |

### 向量与嵌入配置

| 变量 | 说明 | 默认值 | 必填 |
|------|------|--------|------|
| `OM_VECTOR_BACKEND` | 向量存储后端（跟随 `OM_METADATA_BACKEND` 或 `valkey`） | - | |
| `OM_VECTOR_TABLE` | 向量表名 | `vectors` | |
| `OM_VEC_DIM` | 向量维度（按 TIER 自动调整） | 按 tier | |
| `OM_EMBEDDINGS` | 嵌入提供者（`openai` / `gemini` / `ollama` / `synthetic`） | `openai` | |
| `OM_EMBEDDING_FALLBACK` | 嵌入失败回退链（逗号分隔） | `synthetic` | |
| `OM_EMBED_MODE` | 嵌入模式（`simple` / `advanced`） | `simple` | |

### API 密钥（按嵌入提供者）

| 变量 | 说明 | 默认值 | 必填 |
|------|------|--------|------|
| `OPENAI_API_KEY` | OpenAI API 密钥 | - | 使用 OpenAI 时 |
| `OM_OPENAI_BASE_URL` | OpenAI 兼容 API 基础 URL | `https://api.openai.com/v1` | |
| `OM_OPENAI_MODEL` | OpenAI 模型覆盖 | - | |
| `GEMINI_API_KEY` | Google Gemini API 密钥 | - | 使用 Gemini 时 |
| `OLLAMA_URL` | Ollama 服务地址 | `http://localhost:11434` | |

### 记忆系统配置

| 变量 | 说明 | 默认值 | 必填 |
|------|------|--------|------|
| `OM_MIN_SCORE` | 搜索最小相似度分数 | `0.3` | |
| `OM_MAX_PAYLOAD_SIZE` | 最大请求体大小（字节） | `1000000` | |
| `OM_USE_SUMMARY_ONLY` | 仅存储摘要 | `true` | |
| `OM_SUMMARY_MAX_LENGTH` | 摘要最大长度 | `300` | |
| `OM_SEG_SIZE` | 每段记忆数 | `10000` | |

### 衰减系统

| 变量 | 说明 | 默认值 | 必填 |
|------|------|--------|------|
| `OM_DECAY_INTERVAL_MINUTES` | 衰减周期间隔（分钟） | `120` | |
| `OM_DECAY_THREADS` | 衰减工作线程数 | `3` | |
| `OM_DECAY_COLD_THRESHOLD` | 冷记忆阈值 | `0.25` | |
| `OM_DECAY_REINFORCE_ON_QUERY` | 查询时强化记忆 | `true` | |
| `OM_REGENERATION_ENABLED` | 启用冷记忆再生 | `true` | |

### 自动反思

| 变量 | 说明 | 默认值 | 必填 |
|------|------|--------|------|
| `OM_AUTO_REFLECT` | 启用自动反思 | `false` | |
| `OM_REFLECT_INTERVAL` | 反思间隔（分钟） | `10` | |
| `OM_REFLECT_MIN_MEMORIES` | 触发反思的最小记忆数 | `20` | |

### 速率限制

| 变量 | 说明 | 默认值 | 必填 |
|------|------|--------|------|
| `OM_RATE_LIMIT_ENABLED` | 启用速率限制 | `true` | |
| `OM_RATE_LIMIT_WINDOW_MS` | 时间窗口（毫秒） | `60000` | |
| `OM_RATE_LIMIT_MAX_REQUESTS` | 窗口内最大请求数 | `100` | |

---

## Gateway 组件

MCP 网关组件。模块路径: `src/engram/gateway/`

完整示例参考: [`.env.example`](../../.env.example)

### 服务配置

| 变量 | 说明 | 默认值 | 必填 |
|------|------|--------|------|
| `GATEWAY_PORT` | Gateway 服务端口 | `8787` | |
| `PROJECT_KEY` | 项目标识 | `default` | 推荐 |

### 连接配置

| 变量 | 说明 | 默认值 | 必填 |
|------|------|--------|------|
| `POSTGRES_DSN` | PostgreSQL 连接字符串 | - | **必填** |
| `OPENMEMORY_BASE_URL` | OpenMemory 服务地址 | - | **必填** |
| `OPENMEMORY_API_KEY` | OpenMemory API 密钥（兼容旧配置） | - | |
| `OM_API_KEY` | OpenMemory API 密钥（推荐） | - | |

> **注意**: `OPENMEMORY_API_KEY` 优先级高于 `OM_API_KEY`。

### Space 配置

| 变量 | 说明 | 默认值 | 必填 |
|------|------|--------|------|
| `DEFAULT_TEAM_SPACE` | 默认团队 Space | - | |
| `PRIVATE_SPACE_PREFIX` | 私有 Space 前缀 | `private:` | |

### 启动与迁移配置

| 变量 | 说明 | 默认值 | 必填 |
|------|------|--------|------|
| `AUTO_MIGRATE_ON_STARTUP` | 启动时如检测到 DB 缺失是否自动执行迁移 | `false` | |
| `LOGBOOK_CHECK_ON_STARTUP` | 启动时是否检查 Logbook DB 结构 | `true` | |

### 治理管理配置

| 变量 | 说明 | 默认值 | 必填 |
|------|------|--------|------|
| `GOVERNANCE_ADMIN_KEY` | 治理管理密钥（用于更新 settings） | - | |
| `UNKNOWN_ACTOR_POLICY` | 未知用户处理策略（`reject`/`degrade`/`auto_create`） | `degrade` | |

### Evidence Refs 校验配置

| 变量 | 说明 | 默认值 | 必填 |
|------|------|--------|------|
| `VALIDATE_EVIDENCE_REFS` | 是否校验 evidence refs 结构 | `false` | |
| `STRICT_MODE_ENFORCE_VALIDATE_REFS` | strict 模式下是否强制启用校验 | `true` | |

### Worker 配置

Outbox Worker 从 Logbook 消费事件并推送到 OpenMemory。

| 变量 | 说明 | 默认值 | 必填 |
|------|------|--------|------|
| `WORKER_POLL_INTERVAL` | 轮询间隔（秒） | `5` | |
| `WORKER_BATCH_SIZE` | 批处理大小 | `50` | |
| `WORKER_LEASE_TIMEOUT` | 租约超时（秒） | `300` | |

### MinIO Audit 配置

| 变量 | 说明 | 默认值 | 必填 |
|------|------|--------|------|
| `MINIO_AUDIT_WEBHOOK_AUTH_TOKEN` | MinIO Audit Webhook 认证 Token（与 MinIO 侧一致） | - | 启用时必填 |
| `MINIO_AUDIT_MAX_PAYLOAD_SIZE` | MinIO Audit Webhook 最大 payload 大小（字节） | `1048576` | |

---

## 统一栈 (Unified Stack)

完整部署（Logbook + OpenMemory + Gateway）的变量汇总。

### 最小必需变量

```bash
# .env 文件
PROJECT_KEY=myproject
POSTGRES_DB=myproject

# 服务账号密码（必填）
LOGBOOK_MIGRATOR_PASSWORD=changeme1
LOGBOOK_SVC_PASSWORD=changeme2
OPENMEMORY_MIGRATOR_PASSWORD=changeme3
OPENMEMORY_SVC_PASSWORD=changeme4

# 可选
POSTGRES_PASSWORD=postgres
```

### 端口配置

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `POSTGRES_PORT` | PostgreSQL 端口 | `5432` |
| `OM_PORT` | OpenMemory 端口 | `8080` |
| `GATEWAY_PORT` | Gateway 端口 | `8787` |
| `DASHBOARD_PORT` | Dashboard 端口（可选组件） | `3000` |

### 镜像配置（Docker）

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `OPENMEMORY_IMAGE` | OpenMemory 上游镜像 | `ghcr.io/caviraoss/openmemory:latest` |
| `POSTGRES_IMAGE` | PostgreSQL（含 pgvector）镜像 | `pgvector/pgvector:pg16` |

### Dashboard 配置（可选）

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `DASHBOARD_PORT` | Dashboard 服务端口 | `3000` |
| `NEXT_PUBLIC_API_URL` | 前端 API 地址 | `http://localhost:${OM_PORT}` |
| `NEXT_PUBLIC_API_KEY` | 前端 API 密钥 | `${OM_API_KEY}` |

---

## MinIO 对象存储

对象存储服务（可选，启用 minio profile）。

### 核心配置

| 变量 | 说明 | 默认值 | 必填 |
|------|------|--------|------|
| `MINIO_ROOT_USER` | MinIO 管理员用户名 | - | minio profile 必填 |
| `MINIO_ROOT_PASSWORD` | MinIO 管理员密码 | - | minio profile 必填 |
| `MINIO_BUCKET` | 存储桶名称 | `engram` | |
| `MINIO_API_PORT` | API 端口 | `9000` | |
| `MINIO_CONSOLE_PORT` | 控制台端口 | `9001` | |

### 应用用户配置（最小权限）

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `MINIO_APP_USER` | 应用用户名（无 Delete 权限） | - |
| `MINIO_APP_PASSWORD` | 应用用户密码 | - |
| `MINIO_ALLOWED_PREFIXES` | 允许访问的前缀（逗号分隔） | `scm/,attachments/,exports/,tmp/` |

### Ops 用户配置（GC/迁移用）

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `MINIO_CREATE_OPS_USER` | 是否创建 ops 用户 | `false` |
| `MINIO_OPS_USER` | Ops 用户名（有 Delete/List 权限） | - |
| `MINIO_OPS_PASSWORD` | Ops 用户密码 | - |

### HTTPS/TLS 配置

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `MINIO_FORCE_HTTPS` | 强制 HTTPS（生产必须） | `false` |
| `MINIO_CERTS_DIR` | TLS 证书路径 | `/certs` |

### Audit Webhook 配置

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `MINIO_AUDIT_WEBHOOK_ENDPOINT` | Audit Webhook 端点 | `http://gateway:8787/minio/audit` |
| `MINIO_AUDIT_WEBHOOK_AUTH_TOKEN` | 认证 Token（生产必填） | - |
| `MINIO_AUDIT_CLIENT_CERT` | mTLS 客户端证书 | - |
| `MINIO_AUDIT_CLIENT_KEY` | mTLS 客户端密钥 | - |
| `MINIO_AUDIT_QUEUE_DIR` | 队列持久化目录 | `/data/.audit_queue` |
| `MINIO_AUDIT_QUEUE_SIZE` | 队列大小 | `10000` |

### Versioning 配置

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `MINIO_ENABLE_VERSIONING` | 启用 Bucket Versioning | `false` |

### Logbook Tools S3 配置

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `ENGRAM_S3_ENDPOINT` | S3 端点 | `http://minio:9000` |
| `ENGRAM_S3_BUCKET` | 存储桶名 | `${MINIO_BUCKET}` |
| `ENGRAM_S3_REGION` | 区域 | `us-east-1` |
| `ENGRAM_S3_USE_OPS` | 使用 ops 凭证（GC 操作时） | `false` |
| `ENGRAM_S3_VERIFY_SSL` | SSL 验证 | `true` |

---

## SCM 同步服务

SCM 增量同步服务（可选，启用 scm_sync profile）。

### 启用开关

| 变量 | 说明 | 默认值 | 必填 |
|------|------|--------|------|
| `ENGRAM_SCM_SYNC_ENABLED` | 启用 SCM 同步服务（`true`/`false`） | `false` | |

> **注意**: 设置为 `true` 并配合 `--profile scm_sync` 启动时，会启用 Scheduler、Worker、Reaper 等 SCM 同步组件。

### 数据库连接（SCM CLI）

SCM CLI 工具（`engram-scm`）使用以下优先级获取数据库连接：

1. `--dsn` 命令行参数（最高优先级）
2. `--config` 指定的配置文件中的 `[postgres].dsn`
3. `POSTGRES_DSN` 环境变量
4. `ENGRAM_LOGBOOK_CONFIG` 指定的配置文件

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `POSTGRES_DSN` | PostgreSQL 连接字符串 | - |
| `ENGRAM_LOGBOOK_CONFIG` | 配置文件路径（TOML 格式） | `.agentx/config.toml` |

> **注意**: 如果未提供任何 DSN，CLI 将返回退出码 3 并输出清晰的错误提示。

### 凭证配置（敏感信息）

> **安全警告**: 以下变量包含敏感凭证，**不提供默认值**。必须通过 `.env` 文件或环境变量显式设置。切勿将凭证提交到版本控制系统。

| 变量 | 说明 | 默认值 | 必填 |
|------|------|--------|------|
| `GITLAB_URL` | GitLab 服务地址 | - | 使用 GitLab 时必填 |
| `GITLAB_TOKEN` | GitLab 访问令牌 | - | 使用 GitLab 时必填 |
| `GITLAB_PRIVATE_TOKEN` | GitLab 私有令牌（别名） | - | |
| `SVN_USERNAME` | SVN 用户名 | - | 使用 SVN 时必填 |
| `SVN_PASSWORD` | SVN 密码 | - | 使用 SVN 时必填 |

### Scheduler 配置

> **详细说明**: 参见 [SCM Sync 子系统文档](../logbook/06_scm_sync_subsystem.md#scheduler-配置)

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `SCM_SCHEDULER_MAX_RUNNING` | 全局最大运行任务数 | `5` |
| `SCM_SCHEDULER_GLOBAL_CONCURRENCY` | 全局最大队列深度 | `10` |
| `SCM_SCHEDULER_PER_INSTANCE_CONCURRENCY` | 单实例并发数 | `3` |
| `SCM_SCHEDULER_PER_TENANT_CONCURRENCY` | 单租户并发数 | `5` |
| `SCM_SCHEDULER_SCAN_INTERVAL_SECONDS` | 扫描间隔（秒） | `60` |
| `SCM_SCHEDULER_MAX_ENQUEUE_PER_SCAN` | 单次入队最大数 | `100` |
| `SCM_SCHEDULER_ERROR_BUDGET_THRESHOLD` | 错误预算阈值 | `0.3` |
| `SCM_SCHEDULER_PAUSE_DURATION_SECONDS` | 暂停持续时间（秒） | `300` |
| `SCM_SCHEDULER_CURSOR_AGE_THRESHOLD_SECONDS` | 游标年龄阈值（秒） | `3600` |
| `SCM_SCHEDULER_BACKFILL_REPAIR_WINDOW_HOURS` | 回填修复窗口（小时） | `24` |
| `SCM_SCHEDULER_MAX_BACKFILL_WINDOW_HOURS` | 最大回填窗口（小时） | `168` |
| `SCM_SCHEDULER_ENABLE_TENANT_FAIRNESS` | 启用 Tenant 公平调度 | `false` |
| `SCM_SCHEDULER_TENANT_FAIRNESS_MAX_PER_ROUND` | 每轮每 tenant 最大入队数 | `1` |
| `SCM_SCHEDULER_MVP_MODE_ENABLED` | 启用 MVP 模式 | `false` |
| `SCM_SCHEDULER_MVP_JOB_TYPE_ALLOWLIST` | MVP 允许的任务类型（逗号分隔） | `commits` |
| `SCM_SCHEDULER_LOG_LEVEL` | 日志级别 | `INFO` |

### Worker 配置

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `SCM_WORKER_LEASE_SECONDS` | 任务租约时长（秒） | `300` |
| `SCM_WORKER_RENEW_INTERVAL_SECONDS` | 租约续期间隔（秒） | `60` |
| `SCM_WORKER_MAX_RENEW_FAILURES` | 最大续期失败次数 | `3` |
| `SCM_WORKER_POLL_INTERVAL` | 轮询间隔（秒） | `10` |
| `SCM_WORKER_PARALLELISM` | 内部并行度 | `1` |
| `SCM_WORKER_BATCH_SIZE` | 批处理大小 | `50` |
| `SCM_WORKER_LOCK_TIMEOUT` | 分布式锁超时（秒） | `300` |
| `SCM_WORKER_LOG_LEVEL` | 日志级别 | `INFO` |

### Claim 配置（租户公平调度）

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `SCM_CLAIM_ENABLE_TENANT_FAIR_CLAIM` | 启用租户公平调度 | `false` |
| `SCM_CLAIM_MAX_CONSECUTIVE_SAME_TENANT` | 单租户最大连续 claim 次数 | `3` |
| `SCM_CLAIM_MAX_TENANTS_PER_ROUND` | 每轮选取的最大租户数 | `5` |

### Reaper 配置

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `SCM_REAPER_INTERVAL_SECONDS` | 清理间隔（秒） | `60` |
| `SCM_REAPER_JOB_GRACE_SECONDS` | 任务宽限期（秒） | `60` |
| `SCM_REAPER_RUN_MAX_SECONDS` | 运行最大时长（秒） | `3600` |
| `SCM_REAPER_LOCK_GRACE_SECONDS` | 锁宽限期（秒） | `120` |
| `SCM_REAPER_LOG_LEVEL` | 日志级别 | `INFO` |

### 熔断器配置

> **详细说明**: 参见 [SCM Sync 子系统文档](../logbook/06_scm_sync_subsystem.md#熔断器配置)

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `SCM_CB_FAILURE_RATE_THRESHOLD` | 失败率阈值 | `0.3` |
| `SCM_CB_RATE_LIMIT_THRESHOLD` | 429 命中率阈值 | `0.2` |
| `SCM_CB_TIMEOUT_RATE_THRESHOLD` | 超时率阈值 | `0.2` |
| `SCM_CB_MIN_SAMPLES` | 最小样本数（小样本保护） | `5` |
| `SCM_CB_ENABLE_SMOOTHING` | 启用 EMA 平滑 | `true` |
| `SCM_CB_SMOOTHING_ALPHA` | EMA 平滑系数 | `0.5` |
| `SCM_CB_WINDOW_COUNT` | 统计窗口（运行次数） | `20` |
| `SCM_CB_WINDOW_MINUTES` | 统计窗口（分钟） | `30` |
| `SCM_CB_OPEN_DURATION_SECONDS` | 熔断持续时间（秒） | `300` |
| `SCM_CB_HALF_OPEN_MAX_REQUESTS` | 半开状态最大探测数 | `3` |
| `SCM_CB_RECOVERY_SUCCESS_COUNT` | 恢复所需连续成功数 | `2` |
| `SCM_CB_DEGRADED_BATCH_SIZE` | 熔断时的 batch_size | `10` |
| `SCM_CB_DEGRADED_FORWARD_WINDOW_SECONDS` | 熔断时的前向窗口 | `300` |
| `SCM_CB_BACKFILL_ONLY_MODE` | 熔断时仅执行 backfill | `true` |
| `SCM_CB_BACKFILL_INTERVAL_SECONDS` | backfill 间隔（秒） | `600` |
| `SCM_CB_PROBE_BUDGET_PER_INTERVAL` | 探测预算 | `2` |
| `SCM_CB_PROBE_JOB_TYPES_ALLOWLIST` | 探测允许的任务类型（逗号分隔） | `commits` |

### SCM CLI 工具运行方式

SCM 同步子系统提供以下 CLI 工具，核心实现位于 `src/engram/logbook/`：

| 工具 | 推荐命令 | 说明 |
|------|---------|------|
| **调度器** | `engram-scm-scheduler` | 扫描仓库并入队同步任务 |
| **Worker** | `engram-scm-worker` | 从队列处理同步任务 |
| **Reaper** | `engram-scm-reaper` | 回收过期的任务/runs/locks |
| **状态查看** | `engram-scm-status` | 查看同步健康状态与指标 |
| **运行器** | `engram-scm run` | 手动执行增量/回填同步 |

> **弃用说明**: 根目录的 `python scm_sync_*.py` 脚本已移除。请使用 `engram-scm-*` 命令。

#### 调度器使用示例

```bash
# 执行一次调度
engram-scm-scheduler --once

# 干运行（不实际入队）
engram-scm-scheduler --once --dry-run --json

# 指定配置文件
engram-scm-scheduler --once --config /path/to/config.toml
```

#### Worker 使用示例

```bash
# 启动 worker
engram-scm-worker --worker-id worker-1

# 只处理一个任务
engram-scm-worker --worker-id worker-1 --once

# 限制任务类型
engram-scm-worker --worker-id worker-1 --job-types commits,mrs

# 自定义参数
engram-scm-worker --worker-id worker-1 \
    --lease-seconds 600 \
    --poll-interval 5
```

#### Reaper 使用示例

```bash
# 执行回收
engram-scm-reaper

# 模拟运行
engram-scm-reaper --dry-run --verbose

# 自定义参数
engram-scm-reaper \
    --grace-seconds 120 \
    --max-duration-seconds 3600 \
    --policy to_pending
```

#### 状态查看示例

```bash
# JSON 输出
engram-scm-status --json

# Prometheus 指标格式
engram-scm-status --prometheus
```

#### 运行器使用示例

```bash
# 增量同步
engram-scm run incremental --repo gitlab:123

# 回填最近 24 小时
engram-scm run backfill --repo gitlab:123 --last-hours 24

# 查看配置
engram-scm run config --show-backfill
```

---

## 环境变量优先级

### DSN vs 配置文件

1. 命令行参数 `--dsn` 最高优先
2. 环境变量 `POSTGRES_DSN`
3. 配置文件中的 `[postgres].dsn`

### API Key 优先级

Gateway 组件中：
1. `OPENMEMORY_API_KEY`（兼容旧配置）
2. `OM_API_KEY`（推荐）

### 配置文件搜索顺序

1. `--config` / `-c` 参数指定
2. `ENGRAM_LOGBOOK_CONFIG` 环境变量
3. `./.agentx/config.toml`（当前目录）
4. `~/.agentx/config.toml`（用户目录）

---

## OpenMemory Vendoring 相关

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `OPENMEMORY_PATCH_FILES_REQUIRED` | 是否强制 patch 文件存在 | `0` |
| `SCHEMA_STRICT` | 严格模式校验 | `0` |
| `UPSTREAM_REF` | 上游版本引用 | 从 lock 文件读取 |
| `DRY_RUN` | 预览模式（默认 1，设为 0 执行） | `1` |

---

## 快速命令

```bash
# 查看所有 Makefile 目标
make help

# 验证环境变量配置
make precheck

# 一键初始化数据库（bootstrap -> migrate -> verify）
make setup-db

# 统一栈验证
make verify-unified
```

---

## 脚本与工具选择指南

### 数据库初始化推荐流程

```
bootstrap_roles → migrate → verify
```

| 步骤 | 推荐命令 | Docker Compose 服务 | 说明 |
|------|---------|-------------------|------|
| 1. bootstrap_roles | `engram-bootstrap-roles` | `bootstrap_roles` | 创建服务账号 |
| 2. migrate | `engram-migrate --apply-roles --apply-openmemory-grants` | `logbook_migrate` | 执行迁移 |
| 3. verify | `engram-migrate --verify` | `permissions_verify` | 验证权限 |

> **弃用说明**: `python scripts/db_bootstrap.py` 已弃用，并在 v2.0 移除。请使用 `engram-bootstrap-roles`。

### 工具选择

| 场景 | 推荐工具 | 说明 |
|------|---------|------|
| 本地开发 | `make setup-db` | 一键完成，无需记忆参数 |
| CI/CD | `engram-migrate` | pip 安装后可用的 CLI |
| Docker 部署 | docker-compose | 自动按依赖顺序执行 |
| 仅迁移 | `engram-migrate --dsn ...` | 适用于已有服务账号 |

### 废弃脚本

以下脚本是**兼容入口，已弃用**，已在 v2.0 版本移除：

| 废弃脚本 | 替代方案 |
|---------|---------|
| `logbook_postgres/scripts/db_bootstrap.py` | `engram-bootstrap-roles` |
| `logbook_postgres/scripts/db_migrate.py` | `engram-migrate` |
| `python scripts/db_bootstrap.py` | `engram-bootstrap-roles` |
| `python scm_sync_scheduler.py` | `engram-scm-scheduler` |
| `python scm_sync_worker.py` | `engram-scm-worker` |
| `python scm_sync_reaper.py` | `engram-scm-reaper` |
| `python scm_sync_status.py` | `engram-scm-status` |
| `python scm_sync_runner.py` | `engram-scm run` |
| `python artifact_cli.py` | `engram-artifacts` |

> **迁移窗口**: 旧命令已在 v2.0 移除，请使用新入口。
