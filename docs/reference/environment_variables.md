# 环境变量参考

本文档按组件列出 Engram 各组件使用的环境变量、默认值及依赖关系。

**模板文件**:
- Gateway: [`apps/openmemory_gateway/templates/gateway.env.example`](../../apps/openmemory_gateway/templates/gateway.env.example)
- OpenMemory: [`libs/OpenMemory/.env.example`](../../libs/OpenMemory/.env.example)

---

## 目录

- [通用配置](#通用配置)
- [Logbook 组件](#logbook-组件)
- [OpenMemory 组件](#openmemory-组件)
- [Gateway 组件](#gateway-组件)
- [SeekDB 组件](#seekdb-组件)
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

事实账本组件变量。模块路径: `apps/logbook_postgres/`

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

语义记忆存储与检索组件。模块路径: `libs/OpenMemory/`

完整示例参考: [`libs/OpenMemory/.env.example`](../../libs/OpenMemory/.env.example)

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
| `OM_DB_PATH` | SQLite 数据库路径（当 backend=sqlite） | `./data/openmemory.sqlite` | |

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

MCP 网关组件。模块路径: `apps/openmemory_gateway/`

完整示例参考: [`apps/openmemory_gateway/templates/gateway.env.example`](../../apps/openmemory_gateway/templates/gateway.env.example)

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

---

## SeekDB 组件（可选层）

RAG 检索索引组件。模块路径: `apps/seekdb_rag_hybrid/`

> **命名规范**: `SEEKDB_*` 为 canonical 前缀，`SEEK_*` 为已废弃别名（计划于 2026-Q3 移除）。详见 [命名规范](../architecture/naming.md#33-seekdb-环境变量)。
>
> **阈值配置来源**: 所有阈值的 canonical 名称、默认值、废弃别名均定义于 [`apps/seekdb_rag_hybrid/gate_profiles.py::THRESHOLD_REGISTRY`](../../apps/seekdb_rag_hybrid/gate_profiles.py)。

### 可选层约束

SeekDB 是**可选增强层**，不阻塞核心事实流：

- **禁用时**: `SEEKDB_ENABLE=0` 跳过 SeekDB schema/角色迁移，核心验证正常通过
- **失败时**: SeekDB 索引失败不阻塞 Logbook 事件写入和 Gateway 记忆卡片存储
- **可重建**: 索引可从 Logbook/制品完全重建

**验证入口**:

| 入口 | 命令 | 说明 |
|------|------|------|
| 统一栈验证 | `make verify-unified` | 包含 SeekDB 状态检查 |
| SeekDB 单元测试 | `make test-seek-unit` | SeekDB 模块测试 |
| 完整验收 | `make acceptance-unified-full` | 包含 SeekDB 验证 |

---

### Backend/Schema 配置

基础后端与 Schema 配置。

| 变量 | 说明 | 默认值 | 已废弃别名 |
|------|------|--------|------------|
| `SEEKDB_ENABLE` | SeekDB 启用开关（`0`=禁用，`1`=启用） | `1` | `SEEK_ENABLE` |
| `SEEKDB_PG_SCHEMA` | PostgreSQL Schema | `seekdb` | `SEEK_PG_SCHEMA` |
| `SEEKDB_PG_TABLE` | 分块表名 | `chunks` | `SEEK_PG_TABLE` |
| `SEEKDB_PGVECTOR_DSN` | PGVector 连接字符串 | - | - |

### 服务账号密码

| 变量 | 说明 | 默认值 | 已废弃别名 |
|------|------|--------|------------|
| `SEEKDB_MIGRATOR_PASSWORD` | SeekDB 迁移账号密码（`seekdb_migrator_login`） | - | `SEEK_MIGRATOR_PASSWORD` |
| `SEEKDB_SVC_PASSWORD` | SeekDB 服务账号密码（`seekdb_svc`） | - | `SEEK_SVC_PASSWORD` |

---

### Consistency Check（一致性校验）

Shadow 后端就绪性校验阈值，用于 Dual-Read 切换前的一致性检查。

| 变量 | 说明 | 默认值 | 类型 | 已废弃别名 |
|------|------|--------|------|------------|
| `SEEKDB_SHADOW_DOC_COUNT_MIN` | Shadow 后端最小文档数阈值（`0` 表示不检查） | `0` | int | `SEEK_SHADOW_DOC_COUNT_MIN` |
| `SEEKDB_SHADOW_DOC_COUNT_RATIO` | Shadow 后端文档数比例阈值（相对于 primary，`0.0` 表示不检查） | `0.0` | float | `SEEK_SHADOW_DOC_COUNT_RATIO` |
| `SEEKDB_SHADOW_VALIDATE_BLOCK` | Shadow 就绪性校验是否阻断（`True` 时校验失败会抛异常） | `False` | bool | `SEEK_SHADOW_VALIDATE_BLOCK` |

---

### Dual-Read（双读对比）

Dual-Read 模式下新旧后端结果对比的阈值配置。

| 变量 | 说明 | 默认值 | 类型 | 已废弃别名 |
|------|------|--------|------|------------|
| `SEEKDB_DUAL_READ_OVERLAP_MIN_WARN` | 双读命中重叠率警告阈值 | `0.7` | float | `SEEK_DUAL_READ_OVERLAP_MIN_WARN`, `SEEK_DUAL_READ_HIT_OVERLAP_MIN_WARN` |
| `SEEKDB_DUAL_READ_OVERLAP_MIN_FAIL` | 双读命中重叠率失败阈值 | `0.5` | float | `SEEK_DUAL_READ_OVERLAP_MIN_FAIL`, `SEEK_DUAL_READ_HIT_OVERLAP_MIN_FAIL` |
| `SEEKDB_DUAL_READ_SCORE_DRIFT_P95_MAX` | 双读 P95 分数漂移上限 | `0.1` | float | `SEEK_DUAL_READ_SCORE_DRIFT_P95_MAX` |
| `SEEKDB_DUAL_READ_RBO_MIN_WARN` | 双读 RBO 警告阈值 | `0.8` | float | `SEEK_DUAL_READ_RBO_MIN_WARN` |
| `SEEKDB_DUAL_READ_RBO_MIN_FAIL` | 双读 RBO 失败阈值 | `0.6` | float | `SEEK_DUAL_READ_RBO_MIN_FAIL` |
| `SEEKDB_DUAL_READ_RANK_P95_MAX_WARN` | 双读 P95 排名漂移警告阈值 | `3` | int | `SEEK_DUAL_READ_RANK_P95_MAX_WARN` |
| `SEEKDB_DUAL_READ_RANK_P95_MAX_FAIL` | 双读 P95 排名漂移失败阈值 | `5` | int | `SEEK_DUAL_READ_RANK_P95_MAX_FAIL` |
| `SEEKDB_DUAL_READ_LATENCY_RATIO_MAX` | 双读延迟比率上限 | `2.0` | float | `SEEK_DUAL_READ_LATENCY_RATIO_MAX` |

---

### Nightly Rebuild（Nightly 重建门禁）

Nightly CI 重建流程的门禁阈值配置。

| 变量 | 说明 | 默认值 | 类型 | 已废弃别名 |
|------|------|--------|------|------------|
| `SEEKDB_GATE_PROFILE` | Gate Profile 选择（`nightly_default`, `pr_gate_default`, ...） | `nightly_default` | str | `SEEK_GATE_PROFILE` |
| `SEEKDB_NIGHTLY_MIN_OVERLAP` | Nightly 最小命中重叠率阈值（0.0-1.0） | `0.5` | float | `SEEK_NIGHTLY_MIN_OVERLAP` |
| `SEEKDB_NIGHTLY_TOP_K` | Nightly 检索结果数量 | `10` | int | `SEEK_NIGHTLY_TOP_K` |
| `SEEKDB_NIGHTLY_QUERY_SET` | Nightly 查询集名称 | `nightly_default` | str | `SEEK_NIGHTLY_QUERY_SET` |
| `SEEKDB_NIGHTLY_RBO_MIN` | Nightly RBO 最小值阈值（0.0-1.0） | `0.6` | float | `SEEK_NIGHTLY_RBO_MIN` |
| `SEEKDB_NIGHTLY_SCORE_DRIFT_P95_MAX` | Nightly P95 分数漂移上限 | `0.1` | float | `SEEK_NIGHTLY_SCORE_DRIFT_P95_MAX` |
| `SEEKDB_NIGHTLY_RANK_P95_MAX` | Nightly P95 排名漂移上限 | `5` | int | `SEEK_NIGHTLY_RANK_P95_MAX` |

---

### 禁用 SeekDB

```bash
SEEKDB_ENABLE=0 make deploy
```

禁用后：
- SeekDB schema/角色迁移自动跳过
- `verify-unified`、`acceptance-*` 等核心验证正常通过
- Logbook + Gateway 功能不受影响

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

### 凭证配置（敏感信息）

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `GITLAB_PRIVATE_TOKEN` | GitLab 访问令牌 | - |
| `GITLAB_TOKEN` | GitLab 令牌（别名） | - |
| `GITLAB_URL` | GitLab 服务地址 | - |
| `SVN_USERNAME` | SVN 用户名 | - |
| `SVN_PASSWORD` | SVN 密码 | - |

### Scheduler 配置

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `SCM_SCHEDULER_GLOBAL_CONCURRENCY` | 全局并发数 | `10` |
| `SCM_SCHEDULER_PER_INSTANCE_CONCURRENCY` | 单实例并发数 | `3` |
| `SCM_SCHEDULER_PER_TENANT_CONCURRENCY` | 单租户并发数 | `5` |
| `SCM_SCHEDULER_SCAN_INTERVAL_SECONDS` | 扫描间隔（秒） | `60` |
| `SCM_SCHEDULER_MAX_ENQUEUE_PER_SCAN` | 单次入队最大数 | `100` |
| `SCM_SCHEDULER_ERROR_BUDGET_THRESHOLD` | 错误预算阈值 | `0.3` |
| `SCM_SCHEDULER_PAUSE_DURATION_SECONDS` | 暂停持续时间（秒） | `300` |
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

### Reaper 配置

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `SCM_REAPER_INTERVAL_SECONDS` | 清理间隔（秒） | `60` |
| `SCM_REAPER_JOB_GRACE_SECONDS` | 任务宽限期（秒） | `60` |
| `SCM_REAPER_RUN_MAX_SECONDS` | 运行最大时长（秒） | `3600` |
| `SCM_REAPER_LOCK_GRACE_SECONDS` | 锁宽限期（秒） | `120` |
| `SCM_REAPER_LOG_LEVEL` | 日志级别 | `INFO` |

### 熔断器配置

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `SCM_CB_FAILURE_RATE_THRESHOLD` | 失败率阈值 | `0.3` |
| `SCM_CB_RATE_LIMIT_THRESHOLD` | 限流阈值 | `0.2` |
| `SCM_CB_TIMEOUT_RATE_THRESHOLD` | 超时率阈值 | `0.2` |
| `SCM_CB_OPEN_DURATION_SECONDS` | 断开持续时间（秒） | `300` |
| `SCM_CB_HALF_OPEN_MAX_REQUESTS` | 半开状态最大请求数 | `3` |
| `SCM_CB_RECOVERY_SUCCESS_COUNT` | 恢复所需成功数 | `2` |

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

## 废弃变量

以下变量已废弃，将于 2026-Q3 移除：

| 废弃变量 | 替代变量 | 说明 |
|----------|----------|------|
| `SEEK_ENABLE` | `SEEKDB_ENABLE` | SeekDB 启用开关 |
| `SEEK_PG_SCHEMA` | `SEEKDB_PG_SCHEMA` | SeekDB Schema |
| `SEEK_PG_TABLE` | `SEEKDB_PG_TABLE` | SeekDB 分块表 |
| `SEEK_MIGRATOR_PASSWORD` | `SEEKDB_MIGRATOR_PASSWORD` | SeekDB 迁移密码 |
| `SEEK_SVC_PASSWORD` | `SEEKDB_SVC_PASSWORD` | SeekDB 服务密码 |

详见 [命名规范](../architecture/naming.md#33-seekdb-环境变量)。

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

# 统一栈验证
make verify-unified
```
