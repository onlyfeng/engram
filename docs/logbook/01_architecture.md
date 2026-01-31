# 架构与数据流

> **术语说明**：Memory Gateway 简称 Gateway，SeekDB 简称 Seek。详见 [命名规范](../architecture/naming.md)。

## 单项目单库 + Schema 分层（推荐）
每个项目一个 Postgres 数据库（例如 `proj_a`），内部按 schema 分层：

- identity.*：身份与角色（user_id、svn/git 映射、别名、角色标签、Role Profile 镜像）
- logbook.*：事实账本（items/events/attachments/kv/outbox）
- scm.*：代码仓库事实（SVN revision、Git commit、patch 指针、MR/Review 事件）
- analysis.*：分析运行与候选知识（runs / candidates）
- governance.*：治理（团队可写开关、写入审计、提升级别队列等）
- openmemory.* 或 ${PROJECT_KEY}_openmemory.*：OpenMemory 自己的表（由其迁移管理，不手改）
  > ⚠️ **注意**：在启用 Logbook 的 public CREATE 限制时，OpenMemory 不能使用 public schema，必须使用专用 schema

## 核心写入流程（任何工作流通用）
1. 工具/Agent 执行动作（同步、合并、分析、生成等）
2. 先写 Logbook：
   - logbook.items：任务/对象（如“本次同步任务”）
   - logbook.events：追加事件（状态变化、命令执行、结果、错误）
   - logbook.attachments：证据链指针（patch/log/report 的 URI + sha256）
3. （可选）生成视图产物（manifest/index）作为只读输出
4. Memory Gateway / SeekDB 消费 Logbook（异步或同步），但 Logbook 始终是最终真相源

## 身份与配置来源（双轨但有权威）
- Git（仓库内）：/.agentx/users/<user_id>.yaml、/.agentx/roles/<user_id>/*
- DB（权威映射与镜像）：identity.users / identity.accounts / identity.role_profiles
- 本地覆盖（可选）：~/.agentx/user.config.yaml（仅用于个人临时覆盖，不作为权威）

## SCM 同步映射与证据规范

### 字段来源映射

#### scm.repos（仓库注册表）
| DB 字段 | SVN 来源 | Git/GitLab 来源 | 说明 |
|---------|----------|-----------------|------|
| repo_type | 硬编码 `'svn'` | 硬编码 `'git'` | 仓库类型标识 |
| url | 配置 `svn.url` | `gitlab.url` + `project_id` 拼接 | 仓库完整 URL |
| project_key | 配置 `project.project_key` | 配置 `project.project_key` | 项目标识 |
| default_branch | — | 配置 `gitlab.ref_name` | 默认分支（Git 专用） |

#### scm.svn_revisions（SVN 修订记录）
| DB 字段 | SVN log XML 来源 | 说明 |
|---------|------------------|------|
| rev_id / rev_num | `<logentry revision="N">` | SVN revision number |
| author_raw | `<author>` | 提交作者用户名 |
| ts | `<date>` | ISO 8601 时间戳 |
| message | `<msg>` | 提交说明 |
| is_bulk | 规则推断 | changed_paths > 100 则为 true |
| bulk_reason | 规则推断 | 如 `large_changeset:150` |
| meta_json.changed_paths | `<paths><path action="M" kind="file">...</path>` | 变更路径列表，含 action/kind/copyfrom |

#### scm.git_commits（Git 提交记录）
| DB 字段 | GitLab REST API 字段 | 说明 |
|---------|---------------------|------|
| commit_sha | `id` | 40 位 SHA 哈希 |
| author_raw | `author_name` + ` <author_email>` | 拼接格式：`Name <email>` |
| ts | `committed_date` (优先) / `authored_date` | ISO 8601 时间戳 |
| message | `message` | 提交说明 |
| is_merge | `len(parent_ids) > 1` | 多父节点即为 merge commit |
| is_bulk | `stats.additions + deletions > 1000` | 变更行数超阈值 |
| bulk_reason | 规则推断 | 如 `large_changeset:1500` |
| meta_json.author_email | `author_email` | 作者邮箱 |
| meta_json.committer_name | `committer_name` | 提交者名称 |
| meta_json.committer_email | `committer_email` | 提交者邮箱 |
| meta_json.parent_ids | `parent_ids` | 父 commit SHA 列表 |
| meta_json.web_url | `web_url` | GitLab 页面链接 |
| meta_json.stats | `stats` | `{additions, deletions, total}` |

#### scm.patch_blobs（Patch 证据存储）
| DB 字段 | 来源 | 说明 |
|---------|------|------|
| source_type | 硬编码 `'svn'` / `'git'` | 来源类型 |
| source_id | 拼接 `<repo_id>:<rev/sha>` | 唯一定位标识 |
| uri | artifact 相对路径或外部 URL | 物理存储位置（如 `scm/1/git/commits/abc123.diff`） |
| sha256 | `hashlib.sha256(diff_content)` | diff 内容哈希 |
| size_bytes | `len(diff_content.encode('utf-8'))` | diff 字节大小 |
| format | 硬编码 `'diff'` | 存储格式 |
| meta_json.evidence_uri | 构建 `memory://patch_blobs/<source_type>/<source_id>/<sha256>` | 逻辑引用 URI（用于 evidence_refs_json） |

**URI 双轨规范：**

Logbook 区分两类 URI，用于不同场景：

1. **Artifact Key（逻辑键）** - **DB 默认格式（强制）**
   - 格式：无 scheme 的相对路径
   - 示例：`scm/proj_a/1/svn/r100/abc123.diff`
   - 用于 `patch_blobs.uri`、`attachments.uri` 等 DB 字段
   - 与物理存储解耦，后端切换（local → S3）无需修改 DB
   - **约束**：所有写入 DB 的 uri 字段必须使用此格式

2. **Physical URI（物理地址）** - **特例输入（需谨慎）**
   - 格式：`file://`、`s3://`、`https://` 等
   - 示例：`s3://bucket/engram/proj_a/scm/1/r100.diff`
   - **风险说明**：
     - 绑定特定后端，后端切换时需批量迁移 DB 记录
     - 硬编码路径可能因环境差异导致读取失败
     - 跨环境部署（开发 → 生产）需额外处理
   - **允许场景**：外部系统集成、历史数据兼容
   - **处理方式**：
     - 入库前转换为 artifact key（推荐）
     - 迁移工具：`artifact_migrate.py --update-db`
     - 检查残留：`SELECT uri FROM scm.patch_blobs WHERE uri LIKE 's3://%';`

3. **Evidence URI（证据引用）**
   - 格式：`memory://patch_blobs/<source_type>/<source_id>/<sha256>`
   - 存储于 `meta_json.evidence_uri`
   - 用于 analysis.* 和 governance.* 表的 evidence_refs_json 引用

---

### URI 规范索引（唯一规范入口）

> **重要**：Logbook 是 URI Grammar 的唯一规范 owner。以下为规范文档的完整索引。

| 主题 | 规范文档 | 说明 |
|------|----------|------|
| **URI 双轨规范** | 本节（上文） | Artifact Key vs Physical URI vs Evidence URI 的分类与使用场景 |
| **URI 语法与解析** | [`src/engram/logbook/uri.py`](../../src/engram/logbook/uri.py) | 唯一权威的 URI 解析实现，含模块级文档 |
| **Evidence Packet 结构** | [docs/contracts/evidence_packet.md](../contracts/evidence_packet.md) | evidence_refs_json 的结构规范、回溯流程 |
| **Evidence URI 构建** | `engram_logbook.uri.build_evidence_uri()` | patch_blobs 的 canonical URI 构建 |
| **Attachment URI 构建** | `engram_logbook.uri.build_attachment_evidence_uri()` | attachments 的 canonical URI 构建 |
| **Evidence Resolver** | [`src/engram/logbook/evidence_resolver.py`](../../src/engram/logbook/evidence_resolver.py) | URI → 物理内容的解析与回溯实现 |
| **Gateway ↔ Logbook 边界** | [docs/contracts/gateway_logbook_boundary.md](../contracts/gateway_logbook_boundary.md#uri-grammar-归属声明) | URI 归属声明、接口契约 |

**读者指引**：
- 如需了解 URI 格式定义 → 阅读 `src/engram/logbook/uri.py` 模块文档
- 如需了解 evidence 结构 → 阅读 `docs/contracts/evidence_packet.md`
- 如需了解 URI 回溯实现 → 阅读 `src/engram/logbook/evidence_resolver.py`

#### scm.mrs（Merge Request 记录）
| DB 字段 | GitLab REST API 字段 | 说明 |
|---------|---------------------|------|
| mr_id | 构建 `<repo_id>:<iid>` | MR 唯一标识（格式固定为 `<repo_id>:<iid>`） |
| repo_id | 关联 scm.repos | 所属仓库 |
| author_user_id | 映射 `author.username` → identity.users | 作者 |
| status | `state` | opened/merged/closed |
| url | `web_url` | MR 页面链接 |
| meta_json | 其余字段 | title, description, labels 等 |

#### scm.review_events（代码评审事件）
| DB 字段 | GitLab REST API 字段 | 说明 |
|---------|---------------------|------|
| mr_id | 关联 scm.mrs | 所属 MR（格式: `<repo_id>:<iid>`） |
| source_event_id | GitLab 原始事件 ID | 来源：notes 用 `note.id`，approvals 用 `approval.id` 等 |
| reviewer_user_id | 映射 `author.username` → identity.users | 评审者 |
| event_type | 推断自 API endpoint / `noteable_type` | comment/approve/request_changes |
| ts | `created_at` | 事件时间 |
| payload_json | 完整 API 响应 | body, position, resolved 等 |

**幂等键**: `(mr_id, source_event_id)` — GitLab 保证同一 MR 内事件 ID 唯一

---

## 数据隔离方案对比分析

### 路线A：多库方案（Multi-Database）

**架构描述：**
- 每个项目/租户一个独立 PostgreSQL 数据库（如 `proj_a`、`proj_b`）
- 每个数据库内 schema 名固定为：`identity`、`logbook`、`scm`、`analysis`、`governance`、`openmemory`
- OpenMemory 表使用专用 `openmemory` schema（推荐）或 `${PROJECT_KEY}_openmemory`
  > ⚠️ **重要**：在启用 Logbook 的 public CREATE 限制时，OpenMemory 不能使用 public schema

**目录结构示例：**
```
PostgreSQL Cluster
├── proj_a (database)
│   ├── identity.*    # 身份与角色
│   ├── logbook.*     # 事实账本
│   ├── scm.*         # 代码仓库事实
│   ├── analysis.*    # 分析运行
│   ├── governance.*  # 治理
│   └── openmemory.*  # OpenMemory 表（专用 schema，不使用 public）
├── proj_b (database)
│   └── ... (相同结构)
└── proj_test_xxx (CI 临时库)
```

**优点：**
1. **完全隔离**：数据库级别隔离，权限管理最严格
2. **备份/恢复简单**：`pg_dump proj_a` 即可独立备份单个项目
3. **清理简便**：`DROP DATABASE proj_test_xxx` 一条命令
4. **资源可控**：可为不同项目配置独立连接池
5. **无命名冲突**：不同项目完全独立，schema 名固定

**缺点：**
1. **连接开销**：每个数据库需独立连接，连接池管理复杂
2. **跨库查询困难**：需要 dblink/FDW，性能差
3. **CI 并发成本高**：每个测试需创建独立数据库，初始化慢

---

### 路线B：多 Schema 方案（Multi-Schema / 租户前缀）

**架构描述：**
- 单个 PostgreSQL 数据库（如 `engram`）
- 每个项目/租户使用 schema 前缀：`<tenant>_identity`、`<tenant>_logbook` 等
- 运行时通过 `SET search_path TO <tenant>_scm, <tenant>_identity, public;` 绑定租户

**目录结构示例：**
```
PostgreSQL Database: engram
├── proj_a_identity.*
├── proj_a_logbook.*
├── proj_a_scm.*
├── proj_a_analysis.*
├── proj_a_governance.*
├── proj_a_openmemory.*   # 或 public.openmemory_proj_a_*
├── proj_b_identity.*
├── proj_b_logbook.*
├── ... (同样结构)
└── public.*              # 共享工具函数/扩展
```

**优点：**
1. **连接复用**：单库多 schema，连接池高效
2. **跨租户查询**：同库内可直接 JOIN，支持全局分析
3. **CI 并发友好**：`CREATE SCHEMA test_xxx_scm` 快速创建，清理也快

**缺点：**
1. **隔离性较弱**：需依赖 `search_path` + 角色权限控制
2. **备份/恢复复杂**：需要 `pg_dump --schema=proj_a_*`，或自定义脚本
3. **命名冲突风险**：schema 命名需规范，避免前缀冲突
4. **OpenMemory 兼容性问题**：当前 `OM_PG_SCHEMA` 只支持单一 schema 名

---

### 对比矩阵

| 维度 | 路线A（多库） | 路线B（多 Schema） |
|------|---------------|-------------------|
| **迁移复杂度** | 低（每库独立迁移） | 中（需遍历所有租户 schema） |
| **权限隔离** | 强（数据库级） | 中（schema 级 + GRANT） |
| **并发/CI 成本** | 高（创建库 1-2s） | 低（创建 schema 毫秒级） |
| **跨租户分析** | 困难（需 FDW） | 简单（直接 JOIN） |
| **备份/恢复** | 简单（单库 dump） | 复杂（需按 schema 筛选） |
| **清理** | 简单（DROP DATABASE） | 简单（DROP SCHEMA CASCADE） |
| **连接资源** | 高（每库独立池） | 低（复用连接） |
| **OpenMemory 兼容** | ✅ 完全兼容 | ⚠️ 需扩展配置 |

---

### 与 OpenMemory 的兼容性分析

**当前 OpenMemory 配置项（docker-compose.yml）：**
```yaml
- OM_PG_SCHEMA=${OM_PG_SCHEMA:-openmemory}  # 推荐使用 openmemory 或 ${PROJECT_KEY}_openmemory
- OM_PG_TABLE=${OM_PG_TABLE:-openmemory_memories}
- OM_VECTOR_TABLE=${OM_VECTOR_TABLE:-openmemory_vectors}
```
> ⚠️ **注意**：在启用 Logbook 的 public CREATE 限制时，OpenMemory 不能使用 public schema，必须配置专用 schema

**migrate.ts 中的 schema 处理：**
```typescript
const sc = process.env.OM_PG_SCHEMA || "openmemory";  // 推荐默认使用 openmemory schema
const mt = process.env.OM_PG_TABLE || "openmemory_memories";
// SQL 使用: `"${sc}"."${mt}"`
```

**路线A 兼容性**：✅ 完全兼容（需使用专用 schema）
- 每个项目库使用 `OM_PG_SCHEMA=openmemory` 或 `OM_PG_SCHEMA=${PROJECT_KEY}_openmemory`
- ⚠️ 在启用 Logbook 的 public CREATE 限制时，OpenMemory 不能使用 public schema

**路线B 兼容性**：⚠️ 需要扩展
- 需将 `OM_PG_SCHEMA` 改为租户动态 schema（如 `proj_a_openmemory`）
- 或在表名中加入租户前缀：`OM_PG_TABLE=proj_a_memories`
- 需要修改 migrate.ts 支持动态 schema

---

### 最终选择：路线A（多库方案）

**选择原因：**
1. **完全隔离**：满足多项目/多租户的严格数据隔离需求
2. **运维友好**：备份、恢复、清理操作简单直观
3. **OpenMemory 兼容**：配置 `OM_PG_SCHEMA=openmemory` 即可使用专用 schema
4. **权限管理清晰**：数据库级别的 GRANT 最为可靠

**不选路线B 的原因：**
1. **OpenMemory 需改造**：当前不支持动态 schema，需修改迁移逻辑
2. **隔离性风险**：`search_path` 配置错误可能导致跨租户数据泄露
3. **备份恢复复杂**：多 schema 备份需自定义脚本，易出错

---

### 落地约束与配置项

#### 1. 数据库命名规范
```
<project_key>            # 生产库，如 proj_a
<project_key>_test_<id>  # 测试库，如 proj_a_test_12345
```

#### 2. 必需配置项（logbook 配置文件）

| 配置项 | 说明 | 示例 |
|--------|------|------|
| `db.project_key` | 项目标识，用于库名 | `proj_a` |
| `db.host` | PostgreSQL 主机 | `localhost` |
| `db.port` | PostgreSQL 端口 | `5432` |
| `db.user` | 数据库用户 | `engram` |
| `db.password` | 数据库密码 | `***` |
| `db.admin_user` | 管理员用户（创建库用） | `postgres` |
| `db.admin_password` | 管理员密码 | `***` |

#### 3. 派生配置项（自动计算）

| 配置项 | 计算规则 | 示例 |
|--------|----------|------|
| `db.database` | `= project_key` | `proj_a` |
| `db.dsn` | `postgresql://{user}:{password}@{host}:{port}/{database}` | - |

#### 4. OpenMemory 配置映射

当使用多库方案时，OpenMemory 配置如下：
```yaml
OM_PG_HOST: ${db.host}
OM_PG_PORT: ${db.port}
OM_PG_DB: ${db.database}      # 项目库名（应与 POSTGRES_DB 一致）
OM_PG_USER: ${db.user}
OM_PG_PASSWORD: ${db.password}
OM_PG_SCHEMA: openmemory      # 推荐使用 openmemory 或 ${PROJECT_KEY}_openmemory
OM_PG_TABLE: openmemory_memories
OM_VECTOR_TABLE: openmemory_vectors
```

#### 4.1 每项目一库部署配置

**关键配置项对齐规则**：
```
PROJECT_KEY = POSTGRES_DB = 项目标识（如 proj_a）
```

| 环境变量 | 说明 | 示例值 |
|----------|------|--------|
| `PROJECT_KEY` | Logbook 表前缀标识 | `proj_a` |
| `POSTGRES_DB` | PostgreSQL 数据库名（应与 PROJECT_KEY 一致） | `proj_a` |
| `OM_PG_SCHEMA` | OpenMemory schema 名 | `openmemory` |

**部署示例**：
```bash
# 项目 A 部署
PROJECT_KEY=proj_a POSTGRES_DB=proj_a docker compose -f docker-compose.unified.yml up -d

# 项目 B 部署（独立数据库）
PROJECT_KEY=proj_b POSTGRES_DB=proj_b docker compose -f docker-compose.unified.yml up -d
```

> ⚠️ **注意**：`docker-compose.unified.yml` 中的 `POSTGRES_DB` 默认值为 `engram`，仅适用于单项目/开发环境。
> 多项目生产部署时，**必须**显式设置 `POSTGRES_DB` 与 `PROJECT_KEY` 保持一致，以实现数据隔离。

> ⚠️ **重要说明**：在启用 Logbook 的 public CREATE 限制时，OpenMemory 不能使用 public schema。
> 必须配置专用 schema（如 `openmemory` 或 `${PROJECT_KEY}_openmemory`），否则 OpenMemory 的迁移脚本将无法在 public schema 中创建表。

#### 5. CI/测试隔离约束

- 每个测试运行创建临时数据库：`CREATE DATABASE proj_a_test_{uuid};`
- 测试结束后清理：`DROP DATABASE proj_a_test_{uuid};`
- 使用连接池隔离：每个测试使用独立连接字符串

#### 6. 迁移脚本约束

```python
# db_migrate.py 需支持以下操作：
# 1. 检测目标库是否存在，不存在则创建
# 2. 迁移所有固定 schema（identity, logbook, scm, analysis, governance）
# 3. 不操作 openmemory schema（由 OpenMemory 迁移脚本管理）
# 4. public schema 已限制 CREATE 权限，仅用于共享扩展/函数
```

#### 7. Schema Prefix 限制规则（路线A 强制约束）

**生产模式不允许使用 schema_prefix**：
- `--schema-prefix` 参数仅限测试环境使用
- 通过环境变量 `ENGRAM_TESTING=1` 显式启用测试模式才允许使用 prefix
- 生产环境下调用 `db_migrate.py --schema-prefix xxx` 会直接报错拒绝
- 这确保生产环境严格遵循多库隔离方案，schema 名始终为固定值

```python
# 生产模式 schema 名固定：
PRODUCTION_SCHEMAS = ["identity", "logbook", "scm", "analysis", "governance"]

# 测试模式（仅当 ENGRAM_TESTING=1）允许使用前缀：
# <prefix>_identity, <prefix>_logbook, ...
```

---

### source_id 命名规范

`source_id` 用于唯一标识 SCM 事实记录，格式如下：

| 类型 | 格式 | 示例 |
|------|------|------|
| SVN Revision | `svn:<repo_id>:<rev>` | `svn:1:12345` |
| Git Commit | `git:<repo_id>:<sha>` | `git:2:abc123def456789...` |
| Merge Request | `mr:<repo_id>:<iid>` | `mr:2:42` |
| Patch Blob (内部) | `<repo_id>:<rev/sha>` | `1:12345` 或 `2:abc123def...` |

**说明：**
- `repo_id`：scm.repos 表中的代理主键
- `rev`：SVN revision number
- `sha`：Git commit SHA（完整 40 位或短 SHA）
- `iid`：GitLab 项目内 MR 编号
- `id`：GitLab 全局 MR ID

### 证据链完整性

所有 SCM 事实记录必须满足：
1. **可追溯**：通过 `source_id` 可定位到原始 SCM 系统中的记录
2. **可验证**：patch_blobs 存储 `sha256` 哈希，可验证内容未被篡改
3. **可重建**：同步脚本支持幂等执行，使用 `ON CONFLICT DO UPDATE` 策略

---

## ArtifactStore 落地规范

ArtifactStore 是 Logbook 事实层的制品存储组件，用于存储 patch/diff、日志、报告等证据文件。支持三种后端类型，各自适用于不同的部署场景。

### 后端类型概述

| 后端类型 | 适用场景 | 存储位置 | 主要特点 |
|----------|----------|----------|----------|
| `local` | 单机开发、CI 测试 | 本地文件系统 | 零配置、快速、隔离性强 |
| `file` | 团队共享、NFS/SMB | 网络文件共享 | 多节点访问、权限继承 |
| `object` | 生产环境、云部署 | S3/MinIO | 高可用、跨区域、无限扩展 |

---

### 后端一：local（本地文件系统）

#### 路径规范

```
<artifacts_root>/
├── scm/<repo_id>/
│   ├── svn/r<rev>.diff           # SVN revision diff
│   └── git/commits/<sha>.diff    # Git commit diff
├── attachments/<item_id>/
│   └── <attachment_id>.<ext>     # logbook 附件
├── exports/                      # 导出产物
└── tmp/                          # 临时文件（定期清理）
```

| 配置项 | 说明 | 示例 |
|--------|------|------|
| `artifacts.backend` | 固定为 `local` | `"local"` |
| `artifacts.root` | 制品根目录 | `./.agentx/artifacts` |
| `artifacts.allowed_prefixes` | 可选，限制访问目录 | `["scm/", "attachments/"]` |

#### 权限要求

| 权限类型 | 要求 | 说明 |
|----------|------|------|
| 读权限 | 运行用户可读 | 用于读取已存储制品 |
| 写权限 | 运行用户可写 | 用于写入新制品 |
| 目录创建 | 自动创建中间目录 | 使用 `mkdir -p` 语义 |

```bash
# Linux 权限设置示例
chmod -R 750 ./.agentx/artifacts
chown -R engram:engram ./.agentx/artifacts

# Windows 权限（NTFS ACL）
icacls ".\.agentx\artifacts" /grant:r "engram:(OI)(CI)M"
```

#### 安全策略

- **路径穿越防护**：自动检测并拒绝 `../` 等穿越路径
- **路径前缀限制**（可选）：通过 `allowed_prefixes` 限制可访问目录
- **软链接策略**：不跟随符号链接（防止逃逸攻击）

#### 清理策略

```bash
# 清理超过 30 天的临时文件
find ./.agentx/artifacts/tmp -type f -mtime +30 -delete

# 清理孤立制品（需配合数据库查询）
python artifact_gc.py --dry-run --orphan-days 90
```

#### 校验规范

| 校验项 | 时机 | 方法 |
|--------|------|------|
| 写入校验 | 写入后 | 计算 SHA256 与预期对比 |
| 读取校验 | 按需 | 可选的完整性校验 |
| 定期校验 | 维护任务 | 批量扫描对比数据库记录 |

---

### 后端二：file（NFS/SMB 网络共享）

#### 路径规范

使用 `file://` URI scheme，支持 UNC 路径和挂载点。

| 平台 | 路径格式 | 示例 |
|------|----------|------|
| Linux (NFS) | `file:///mnt/nfs/artifacts/<project>/` | `file:///mnt/nfs/artifacts/proj_a/` |
| Linux (SMB) | `file:///mnt/smb/artifacts/<project>/` | `file:///mnt/smb/artifacts/proj_a/` |
| Windows (UNC) | `file://fileserver/share/artifacts/<project>/` | `file://fileserver/artifacts/proj_a/` |

```
file://<server>/<share>/artifacts/<project_key>/
├── scm/<repo_id>/
│   ├── svn/r<rev>.diff
│   └── git/commits/<sha>.diff
├── attachments/<item_id>/
└── exports/
```

#### 权限要求

| 层级 | Linux (NFS) | Linux (SMB) | Windows (SMB) |
|------|-------------|-------------|---------------|
| 服务端共享 | exports 配置 rw | smb.conf 配置 | 共享权限设置 |
| POSIX/ACL | uid/gid 映射 | 挂载选项指定 | NTFS ACL |
| 应用层 | 运行用户 rw | 挂载用户 rw | 运行服务账号 |

**NFS 挂载示例**
```bash
# /etc/fstab
fileserver:/exports/artifacts /mnt/nfs/artifacts nfs defaults,rw,soft,intr 0 0

# NFS 服务端 exports（/etc/exports）
/exports/artifacts 10.0.0.0/24(rw,sync,no_subtree_check,all_squash,anonuid=1000,anongid=1000)
```

**SMB 挂载示例**
```bash
# Linux SMB 挂载（/etc/fstab）
//fileserver/artifacts /mnt/smb/artifacts cifs credentials=/etc/samba/creds,uid=engram,gid=engram,file_mode=0640,dir_mode=0750 0 0

# Windows 映射网络驱动器
net use Z: \\fileserver\artifacts /persistent:yes
```

#### 安全策略

- **传输加密**：
  - NFS v4 + Kerberos（推荐）或 NFS over TLS
  - SMB 3.0+ 启用加密（`seal` 选项）
- **访问控制**：依赖服务端共享权限 + 文件系统 ACL
- **无客户端凭证存储**：使用 Kerberos 票据或机器账户认证

#### 清理策略

```bash
# NFS 挂载点清理（推荐在服务端执行）
find /exports/artifacts/*/tmp -type f -mtime +30 -delete

# Windows 共享清理（PowerShell）
Get-ChildItem "\\fileserver\artifacts\*\tmp" -Recurse | 
  Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } | 
  Remove-Item -Force
```

#### 校验规范

| 校验项 | 网络共享特殊处理 |
|--------|------------------|
| 网络中断 | 写入使用临时文件 + 原子重命名 |
| 部分写入 | 校验文件大小与 SHA256 |
| 权限错误 | 明确报告，不静默忽略 |

**原子写入示例**
```python
# 先写入临时文件，再原子重命名
tmp_path = f"{target_path}.tmp.{uuid4()}"
with open(tmp_path, 'wb') as f:
    f.write(content)
os.rename(tmp_path, target_path)  # POSIX 原子操作
```

---

### 后端三：object（S3/MinIO 对象存储）

#### 对象键规范

```
<prefix>/<project_key>/
├── scm/<repo_id>/svn/r<rev>.diff
├── scm/<repo_id>/git/commits/<sha>.diff
├── attachments/<item_id>/<attachment_id>.<ext>
└── exports/<export_id>/
```

| 配置项 | 来源 | 示例 |
|--------|------|------|
| `endpoint` | 环境变量 `ENGRAM_S3_ENDPOINT` | `https://minio.example.com` |
| `bucket` | 环境变量 `ENGRAM_S3_BUCKET` | `artifacts` |
| `access_key` | 环境变量 `ENGRAM_S3_ACCESS_KEY` | `minioadmin` |
| `secret_key` | 环境变量 `ENGRAM_S3_SECRET_KEY` | `***` |
| `region` | 配置文件或环境变量 | `us-east-1` |
| `prefix` | 配置文件 | `engram/` |

**完整对象键格式**
```
s3://<bucket>/<prefix><project_key>/scm/<repo_id>/<type>/<id>.diff
# 示例
s3://artifacts/engram/proj_a/scm/1/svn/r100.diff
```

#### 权限要求

**MinIO Policy 示例**
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::artifacts",
        "arn:aws:s3:::artifacts/engram/*"
      ]
    }
  ]
}
```

**AWS S3 IAM Policy 示例**
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject"
      ],
      "Resource": "arn:aws:s3:::artifacts/engram/${aws:PrincipalTag/project_key}/*"
    },
    {
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::artifacts",
      "Condition": {
        "StringLike": {
          "s3:prefix": ["engram/${aws:PrincipalTag/project_key}/*"]
        }
      }
    }
  ]
}
```

| 权限 | 用途 | 必需 |
|------|------|------|
| `s3:GetObject` | 读取制品 | ✅ |
| `s3:PutObject` | 写入制品 | ✅ |
| `s3:DeleteObject` | 清理/GC | 按需 |
| `s3:ListBucket` | 遍历制品 | 按需 |

#### 安全策略

| 安全项 | 配置要求 |
|--------|----------|
| **凭证管理** | 禁止配置文件明文，必须使用环境变量 |
| **传输加密** | 强制 HTTPS（endpoint 必须 `https://`） |
| **静态加密** | 启用 SSE-S3 或 SSE-KMS |
| **访问日志** | 启用 S3 Server Access Logging |
| **版本控制** | 建议启用，便于恢复误删除 |

**MinIO TLS 配置验证**
```bash
# 验证端点支持 TLS
curl -I https://minio.example.com/minio/health/live

# 配置证书（MinIO 客户端）
mc alias set engram https://minio.example.com $ACCESS_KEY $SECRET_KEY
```

#### 清理策略

**S3 生命周期规则（推荐）**
```json
{
  "Rules": [
    {
      "ID": "cleanup-tmp",
      "Prefix": "engram/*/tmp/",
      "Status": "Enabled",
      "Expiration": { "Days": 7 }
    },
    {
      "ID": "cleanup-old-exports",
      "Prefix": "engram/*/exports/",
      "Status": "Enabled",
      "Expiration": { "Days": 90 }
    }
  ]
}
```

**MinIO 生命周期配置**
```bash
mc ilm add engram/artifacts --prefix "engram/*/tmp/" --expiry-days 7
mc ilm add engram/artifacts --prefix "engram/*/exports/" --expiry-days 90
```

**手动 GC（配合数据库查询）**
```bash
# 列出孤立对象（不在数据库记录中）
python artifact_gc.py --backend object --list-orphans

# 清理孤立对象（超过 90 天）
python artifact_gc.py --backend object --delete-orphans --orphan-days 90
```

#### S3 安全加固（生产环境必做）

**1. 启用 Bucket Versioning**

Versioning 可防止误删除，支持恢复历史版本：

```bash
# MinIO
mc version enable myminio/artifacts

# AWS S3
aws s3api put-bucket-versioning --bucket artifacts \
    --versioning-configuration Status=Enabled
```

**2. 可选：启用 Object Lock**

Object Lock 可防止在保留期内删除对象（需创建 Bucket 时启用）：

```bash
# MinIO（创建时启用）
mc mb --with-lock myminio/artifacts-locked

# 配置默认保留期（GOVERNANCE 模式，30 天）
mc retention set --default GOVERNANCE 30d myminio/artifacts-locked
```

**3. artifact_gc.py 生产安全参数**

| 参数 | 说明 |
|------|------|
| `--trash-prefix .trash/` | 软删除，移动到回收站而非永久删除 |
| `--require-trash` | 强制要求 `--trash-prefix`，禁止硬删除 |
| `--force-hard-delete` | 显式确认硬删除（无 `--trash-prefix` 时必须） |

```bash
# 推荐：始终使用软删除
python artifact_gc.py --prefix scm/ --trash-prefix .trash/ --delete

# 生产环境：强制软删除策略
python artifact_gc.py --prefix scm/ --require-trash --trash-prefix .trash/ --delete

# 硬删除需显式确认（不推荐）
python artifact_gc.py --prefix scm/ --delete --force-hard-delete
```

**4. 配置脚本**

详细加固步骤参考：`scripts/ops/s3_hardening.sh`

#### 校验规范

| 校验项 | 方法 | 说明 |
|--------|------|------|
| 上传校验 | `Content-MD5` Header | S3 自动验证 |
| 完整性校验 | ETag (MD5/分片哈希) | 读取时对比 |
| SHA256 校验 | `x-amz-checksum-sha256` | 推荐使用 |
| 定期校验 | 批量 HeadObject | 检查存在性和大小 |

**上传时校验示例**
```python
import hashlib
import base64

content = b"diff content..."
md5_hash = base64.b64encode(hashlib.md5(content).digest()).decode()
sha256_hash = hashlib.sha256(content).hexdigest()

s3_client.put_object(
    Bucket=bucket,
    Key=key,
    Body=content,
    ContentMD5=md5_hash,
    ChecksumSHA256=base64.b64encode(hashlib.sha256(content).digest()).decode()
)
```

---

### 后端切换与迁移

#### 配置切换

```toml
# 开发环境 → 生产环境切换
[artifacts]
backend = "object"  # 从 "local" 改为 "object"
```

```bash
# 环境变量覆盖（无需改配置文件）
export ENGRAM_ARTIFACTS_BACKEND=object
export ENGRAM_S3_ENDPOINT=https://minio.example.com
export ENGRAM_S3_ACCESS_KEY=***
export ENGRAM_S3_SECRET_KEY=***
export ENGRAM_S3_BUCKET=artifacts
```

#### 数据迁移

```bash
# local → object 迁移
python artifact_migrate.py \
  --source-backend local --source-root ./.agentx/artifacts \
  --target-backend object \
  --verify --dry-run

# file → object 迁移
python artifact_migrate.py \
  --source-backend file --source-uri file:///mnt/nfs/artifacts/proj_a \
  --target-backend object \
  --update-db  # 更新 patch_blobs.uri 引用
```

---

### 通用约束与最佳实践

#### URI 格式规范

**生产默认：使用 Artifact Key（无 scheme 相对路径）**

| 后端 | DB uri 字段格式（Artifact Key） | 运行时解析为 |
|------|--------------------------------|-------------|
| local | `scm/1/svn/r100.diff` | `{artifacts_root}/scm/1/svn/r100.diff` |
| file | `scm/1/svn/r100.diff` | `file://{file_root}/scm/1/svn/r100.diff` |
| object | `scm/1/svn/r100.diff` | `s3://{bucket}/{prefix}/scm/1/svn/r100.diff` |

**特例输入：允许 Physical URI（绑定后端）**

| Physical URI 格式 | 适用场景 |
|-------------------|----------|
| `file:///mnt/nfs/artifacts/proj_a/scm/1/r100.diff` | NFS 共享直接引用 |
| `s3://bucket/engram/proj_a/scm/1/r100.diff` | 外部 S3 对象 |
| `https://gitlab.example.com/api/v4/.../diff` | 外部 API 引用 |

> **注意**：Physical URI 绑定特定后端，后端切换时需使用 `artifact_migrate.py` 更新。

#### Physical URI 风险与处理

**风险矩阵**

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| 后端切换困难 | 需批量更新所有 DB 记录 | 始终使用 artifact key |
| 环境差异 | 路径在不同环境无效 | 入库前统一转换格式 |
| 迁移遗漏 | 部分记录读取失败 | 迁移后执行一致性校验 |
| 外部依赖 | https:// 链接失效 | 定期检查外部引用可用性 |

**检测与迁移流程**

```sql
-- 1. 检测 DB 中的 physical uri 残留
SELECT COUNT(*) FROM scm.patch_blobs 
WHERE uri LIKE 's3://%' OR uri LIKE 'file://%' OR uri LIKE 'http%';

SELECT COUNT(*) FROM logbook.attachments 
WHERE uri LIKE 's3://%' OR uri LIKE 'file://%' OR uri LIKE 'http%';
```

```bash
# 2. 迁移 physical uri 到 artifact key
python artifact_migrate.py --update-db --dry-run   # 预览变更数量
python artifact_migrate.py --update-db --execute   # 执行迁移

# 3. 验证迁移结果
python artifact_migrate.py --verify                # 校验记录一致性
```

**最佳实践**

1. **入库前转换**：所有外部输入的 physical uri 在写入 DB 前转换为 artifact key
2. **定期检查**：CI 中加入 physical uri 残留检测，发现即告警
3. **后端切换 Checklist**：
   - [ ] 确认所有 uri 字段无 scheme 前缀
   - [ ] 备份数据库
   - [ ] 迁移制品文件
   - [ ] 更新配置
   - [ ] 验证读取功能

#### 文件命名规范

| 类型 | 命名格式 | 示例 |
|------|----------|------|
| SVN diff | `r<rev>.diff` | `r12345.diff` |
| Git diff | `<sha>.diff` | `abc123def.diff` |
| 附件 | `<attachment_id>.<ext>` | `789.log` |
| 临时文件 | `<name>.tmp.<uuid>` | `r100.diff.tmp.a1b2c3` |

#### 大小限制

| 限制项 | 默认值 | 配置项 |
|--------|--------|--------|
| 单文件最大 | 10 MB | `artifacts.max_file_size` |
| Bulk diff 阈值 | 1 MB | `bulk.diff_size_threshold` |
| 总存储配额 | 无限制 | 后端自身配置 |

#### 错误处理

| 错误类型 | 处理策略 |
|----------|----------|
| 写入失败 | 返回错误，不更新数据库 |
| 读取失败 | 返回错误，日志记录 |
| 校验失败 | 返回 `CHECKSUM_MISMATCH`，不覆盖 |
| 权限不足 | 明确报告，包含路径信息 |
| 配额超限 | 返回 `QUOTA_EXCEEDED` |

---

## SCM Sync at Scale

本节描述 SCM 同步在大规模场景（多仓库、高频同步、分布式部署）下的架构设计与最佳实践。

### 核心概念

#### Watermark（水位标记）

Watermark 是增量同步的核心机制，用于记录上次同步的位置，避免重复拉取历史数据。

| source_type | watermark 类型 | 格式 | 说明 |
|-------------|----------------|------|------|
| svn | revision number | `int` | SVN revision 是单调递增整数 |
| git | commit timestamp | `ISO 8601 datetime` | 使用 `committed_date` 时间戳 |
| gitlab_mr | updated_at | `ISO 8601 datetime` | MR 更新时间 |
| gitlab_review | created_at | `ISO 8601 datetime` | Review 事件创建时间 |

#### Cursor（游标）

Cursor 是 watermark 的存储形式，存储于 `logbook.kv` 表中：

```
namespace: scm.sync
key: <source_type>_cursor:<repo_id>

# 示例
scm.sync / svn_cursor:1        → {"last_rev": 12345, "run_id": "sync-20240115-001"}
scm.sync / gitlab_cursor:2     → {"last_commit_ts": "2024-01-15T12:30:00Z", "run_id": "sync-20240115-002"}
```

#### Run ID（运行标识）

`run_id` 用于标识每次同步运行，便于追溯和审计：

| 字段 | 格式 | 说明 | 示例 |
|------|------|------|------|
| run_id | `<prefix>-<date>-<seq>` | 唯一运行标识 | `sync-20240115-001` |
| 存储位置 | `logbook.kv` cursor 值中 | 嵌入 cursor JSON | `{"last_rev": 100, "run_id": "..."}` |
| 日志关联 | `analysis.runs` | 完整运行记录 | 包含开始/结束时间、统计、错误 |

### 大规模同步架构

```
┌─────────────────────────────────────────────────────────────────┐
│                     SCM Sync Controller                         │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐           │
│  │  Scheduler   │  │ Rate Limiter │  │  Dispatcher  │           │
│  └──────────────┘  └──────────────┘  └──────────────┘           │
└─────────────────────────────────────────────────────────────────┘
                              │
         ┌────────────────────┼────────────────────┐
         ▼                    ▼                    ▼
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│  Worker Pool 1  │  │  Worker Pool 2  │  │  Worker Pool N  │
│  ┌───────────┐  │  │  ┌───────────┐  │  │  ┌───────────┐  │
│  │ SVN Sync  │  │  │  │ Git Sync  │  │  │  │ MR Sync   │  │
│  └───────────┘  │  │  └───────────┘  │  │  └───────────┘  │
└─────────────────┘  └─────────────────┘  └─────────────────┘
         │                    │                    │
         └────────────────────┼────────────────────┘
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                        PostgreSQL                                │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐           │
│  │  scm.repos   │  │ scm.commits  │  │ logbook.kv   │           │
│  │              │  │ scm.mrs      │  │ (cursors)    │           │
│  └──────────────┘  └──────────────┘  └──────────────┘           │
└─────────────────────────────────────────────────────────────────┘
```

### 并发同步策略

#### 仓库级并行

同一时间可并行同步多个不同仓库，每个仓库独立维护 cursor：

| 策略 | 适用场景 | 配置 |
|------|----------|------|
| 串行 | 单仓库、调试 | `scm.sync.parallelism = 1` |
| 并行 | 多仓库生产部署 | `scm.sync.parallelism = 4-8` |
| 按类型分组 | SVN/Git 混合环境 | 按 source_type 分 worker pool |

#### 单仓库内串行

同一仓库的同步任务必须串行执行，避免 cursor 冲突：

```python
# 使用分布式锁确保单仓库串行
with acquire_repo_lock(repo_id, timeout=300):
    cursor = load_cursor(repo_id)
    new_data = fetch_since(cursor.watermark)
    upsert_data(new_data)
    save_cursor(repo_id, new_watermark, run_id)
```

### 容错与恢复

#### Overlap 策略

Overlap 用于补偿边界条件（如网络中断导致的部分写入）：

| source_type | 默认 overlap | 推荐范围 | 说明 |
|-------------|--------------|----------|------|
| svn | 0 | 0-10 | SVN revision 严格有序 |
| git | 0 | 0-50 | commit 时间戳可能有边界问题 |
| gitlab_mr | 0 | 0-20 | MR 更新时间可能有时钟偏移 |

#### 故障恢复

| 故障类型 | 恢复策略 |
|----------|----------|
| 网络中断 | 从 cursor 位置重试，增加 overlap |
| 部分写入 | upsert 幂等，cursor 仅在批次完成后更新 |
| 数据损坏 | 使用 backfill 模式重新同步指定范围 |
| cursor 丢失 | 从 `analysis.runs` 恢复最后成功的 watermark |

### 监控指标

| 指标 | 说明 | 告警阈值 |
|------|------|----------|
| `scm_sync_lag_seconds` | 同步延迟（HEAD - cursor 时间差） | > 3600s |
| `scm_sync_batch_duration` | 单批次同步耗时 | > 300s |
| `scm_sync_error_rate` | 同步错误率 | > 5% |
| `scm_sync_pending_repos` | 待同步仓库数 | > 10 |
| `scm_cursor_age_seconds` | cursor 未更新时长 | > 86400s |

### 表结构

#### scm.sync_tasks（同步任务注册）

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| task_id | UUID | PK | 任务唯一标识 |
| repo_id | BIGINT | FK → scm.repos | 关联仓库 |
| source_type | VARCHAR(20) | NOT NULL | `svn` / `git` / `gitlab_mr` / `gitlab_review` |
| status | VARCHAR(20) | NOT NULL | 任务状态（见状态机） |
| priority | INT | DEFAULT 0 | 调度优先级，数值越大越优先 |
| cron_expr | VARCHAR(50) | NULLABLE | cron 表达式，NULL 表示手动触发 |
| config_json | JSONB | DEFAULT '{}' | 任务级配置覆盖 |
| created_at | TIMESTAMPTZ | NOT NULL | 创建时间 |
| updated_at | TIMESTAMPTZ | NOT NULL | 更新时间 |

**索引**：
- `idx_sync_tasks_repo_type` ON (repo_id, source_type) UNIQUE
- `idx_sync_tasks_status` ON (status) WHERE status IN ('pending', 'running')

#### scm.sync_runs（同步运行记录）

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| run_id | VARCHAR(50) | PK | 运行标识（`sync-<date>-<seq>`） |
| task_id | UUID | FK → scm.sync_tasks | 关联任务 |
| status | VARCHAR(20) | NOT NULL | 运行状态 |
| started_at | TIMESTAMPTZ | NOT NULL | 开始时间 |
| finished_at | TIMESTAMPTZ | NULLABLE | 结束时间 |
| watermark_start | JSONB | NOT NULL | 起始水位 |
| watermark_end | JSONB | NULLABLE | 结束水位（成功时填充） |
| stats_json | JSONB | DEFAULT '{}' | 统计信息 |
| error_json | JSONB | NULLABLE | 错误详情 |
| worker_id | VARCHAR(100) | NULLABLE | 执行 worker 标识 |

**索引**：
- `idx_sync_runs_task_started` ON (task_id, started_at DESC)
- `idx_sync_runs_status` ON (status) WHERE status = 'running'

**stats_json 结构**：
```json
{
  "fetched": 150,        // 拉取记录数
  "inserted": 120,       // 新增记录数
  "updated": 30,         // 更新记录数（upsert 命中）
  "skipped": 0,          // 跳过记录数（如 bulk）
  "duration_ms": 12345,  // 耗时毫秒
  "batches": 2           // 批次数
}
```

#### logbook.kv（Cursor 存储）

同步 cursor 存储于 `logbook.kv` 表，复用现有 KV 存储机制：

| namespace | key 格式 | value 结构 |
|-----------|----------|------------|
| `scm.sync` | `<source_type>_cursor:<repo_id>` | `{"watermark": ..., "run_id": ..., "updated_at": ...}` |

**Cursor value 示例**：
```json
// SVN cursor
{"watermark": 12345, "run_id": "sync-20240115-001", "updated_at": "2024-01-15T12:30:00Z"}

// Git cursor
{"watermark": "2024-01-15T12:30:00Z", "run_id": "sync-20240115-002", "updated_at": "2024-01-15T12:35:00Z"}

// GitLab MR cursor
{"watermark": "2024-01-15T10:00:00Z", "run_id": "sync-20240115-003", "updated_at": "2024-01-15T12:40:00Z"}
```

---

### 关键配置项详解

| 配置项 | 类型 | 默认值 | 取值范围 | 说明 |
|--------|------|--------|----------|------|
| `scm.sync.parallelism` | int | 4 | 1-16 | 并行同步仓库数，生产建议 4-8 |
| `scm.sync.batch_size` | int | 100 | 10-1000 | 单批次最大记录数，过大影响内存和事务 |
| `scm.sync.interval_seconds` | int | 300 | 0-86400 | 同步间隔，0 表示仅手动触发 |
| `scm.sync.retry_count` | int | 3 | 0-10 | 失败重试次数 |
| `scm.sync.retry_interval` | int | 5 | 1-60 | 重试间隔秒数，建议采用指数退避 |
| `scm.sync.default_overlap` | int | 0 | 0-100 | 默认 overlap 值，补偿边界条件 |
| `scm.sync.mode` | string | `strict` | `strict` / `best_effort` | strict 失败即停，best_effort 记录错误继续 |
| `scm.sync.lock_timeout` | int | 300 | 60-3600 | 分布式锁超时秒数 |
| `scm.sync.run_history_days` | int | 90 | 7-365 | 运行记录保留天数 |
| `scm.sync.max_lag_seconds` | int | 3600 | 300-86400 | 最大允许延迟，超出触发告警 |
| `scm.sync.bulk_threshold` | int | 100 | 50-500 | bulk 判定阈值（SVN 文件数 / Git 行数） |
| `scm.sync.diff_fetch_enabled` | bool | true | true/false | 是否拉取 diff 内容存储到 patch_blobs |
| `scm.sync.diff_size_limit_mb` | int | 10 | 1-50 | 单个 diff 大小限制 MB |

**重试策略配置**：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `scm.sync.retry_backoff` | string | `exponential` | `fixed` / `exponential` / `linear` |
| `scm.sync.retry_max_interval` | int | 60 | 最大重试间隔秒数（指数退避时） |
| `scm.sync.retry_jitter` | float | 0.1 | 重试抖动系数 0-1 |

**源类型特定配置**：

| 配置项 | 适用 source_type | 默认值 | 说明 |
|--------|------------------|--------|------|
| `scm.sync.svn.log_limit` | svn | 500 | 单次 `svn log` 拉取上限 |
| `scm.sync.git.fetch_stats` | git | true | 是否获取 commit stats |
| `scm.sync.gitlab.per_page` | git/gitlab_mr | 100 | API 分页大小 |
| `scm.sync.gitlab.rate_limit_rpm` | git/gitlab_mr | 600 | API 请求限速（每分钟） |

---

### 状态机定义

#### 任务状态（sync_tasks.status）

```
                    ┌──────────────────────────────────────────────┐
                    │                   启用/禁用切换               │
                    ▼                                              │
┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐      │
│ disabled │───▶│ pending  │───▶│ running  │───▶│ pending  │──────┘
└──────────┘    └──────────┘    └──────────┘    └──────────┘
     │               │               │               │
     │               │               ▼               │
     │               │         ┌──────────┐          │
     │               │         │  failed  │──────────┤
     │               │         └──────────┘          │
     │               │               │               │
     │               ▼               ▼               ▼
     └──────────────────────────────────────────────────
```

| 状态 | 说明 | 转换条件 |
|------|------|----------|
| `disabled` | 任务禁用，不参与调度 | 手动禁用或超过失败阈值 |
| `pending` | 等待调度 | 到达调度时间或手动触发 |
| `running` | 正在执行 | 被 worker 获取执行 |
| `failed` | 最近运行失败 | 执行出错（可重试或待人工介入） |

#### 运行状态（sync_runs.status）

```
┌───────────┐    ┌───────────┐    ┌───────────┐
│  pending  │───▶│  running  │───▶│ completed │
└───────────┘    └───────────┘    └───────────┘
                      │
                      ▼
                ┌───────────┐    ┌───────────┐
                │  failed   │───▶│  retrying │───┐
                └───────────┘    └───────────┘   │
                      ▲                          │
                      └──────────────────────────┘
                                (重试次数未耗尽)
                      │
                      ▼
                ┌───────────┐
                │ exhausted │ (重试耗尽，需人工介入)
                └───────────┘
```

| 状态 | 说明 | 终态 |
|------|------|------|
| `pending` | 运行已创建，等待执行 | 否 |
| `running` | 正在执行同步 | 否 |
| `completed` | 成功完成 | 是 |
| `failed` | 执行失败，等待重试判定 | 否 |
| `retrying` | 重试中 | 否 |
| `exhausted` | 重试次数耗尽 | 是 |
| `cancelled` | 手动取消 | 是 |

---

### 失败恢复流程

#### 流程图

```
                          同步任务开始
                               │
                               ▼
                    ┌──────────────────────┐
                    │ 1. 获取分布式锁       │
                    │    (repo_id + type)  │
                    └──────────────────────┘
                               │
                     ┌─────────┴─────────┐
                     │ 锁获取失败?        │
                     └─────────┬─────────┘
                          是   │   否
               ┌───────────────┘   │
               ▼                   ▼
        ┌──────────────┐  ┌──────────────────────┐
        │ 等待/跳过本轮 │  │ 2. 加载 cursor        │
        └──────────────┘  └──────────────────────┘
                                   │
                          ┌────────┴────────┐
                          │ cursor 存在?    │
                          └────────┬────────┘
                            否     │    是
                    ┌──────────────┘    │
                    ▼                   ▼
           ┌────────────────┐  ┌────────────────────────┐
           │ 使用默认起点   │  │ watermark - overlap     │
           │ (全量同步)     │  │ 作为实际起点            │
           └────────────────┘  └────────────────────────┘
                    │                   │
                    └─────────┬─────────┘
                              ▼
                    ┌──────────────────────┐
                    │ 3. 批量拉取数据       │
                    │    (fetch + 分页)    │
                    └──────────────────────┘
                              │
                     ┌────────┴────────┐
                     │ 拉取失败?        │
                     └────────┬────────┘
                         是   │   否
              ┌───────────────┘   │
              ▼                   ▼
     ┌────────────────┐  ┌──────────────────────┐
     │ 进入重试流程    │  │ 4. 事务写入数据       │
     │ (见重试伪代码)  │  │    (upsert 幂等)     │
     └────────────────┘  └──────────────────────┘
                                  │
                         ┌────────┴────────┐
                         │ 写入失败?        │
                         └────────┬────────┘
                            是    │    否
                 ┌────────────────┘    │
                 ▼                     ▼
        ┌────────────────┐   ┌──────────────────────┐
        │ 回滚事务        │   │ 5. 更新 cursor        │
        │ 进入重试流程    │   │    (同一事务内)       │
        └────────────────┘   └──────────────────────┘
                                       │
                                       ▼
                             ┌──────────────────────┐
                             │ 6. 提交事务 + 释放锁  │
                             └──────────────────────┘
                                       │
                                       ▼
                             ┌──────────────────────┐
                             │ 7. 更新运行记录       │
                             │    status=completed  │
                             └──────────────────────┘
```

#### 重试伪代码

```python
def sync_with_retry(task: SyncTask) -> SyncResult:
    """带重试的同步执行主流程"""
    
    config = load_sync_config()
    retry_count = 0
    last_error = None
    
    while retry_count <= config.retry_count:
        try:
            # 1. 获取分布式锁（防止同一仓库并发同步）
            lock_key = f"scm_sync:{task.repo_id}:{task.source_type}"
            with acquire_lock(lock_key, timeout=config.lock_timeout) as lock:
                if not lock.acquired:
                    raise LockAcquisitionError(f"无法获取锁: {lock_key}")
                
                # 2. 加载 cursor
                cursor = load_cursor(
                    namespace="scm.sync",
                    key=f"{task.source_type}_cursor:{task.repo_id}"
                )
                
                # 3. 计算实际起点（应用 overlap）
                start_watermark = apply_overlap(
                    cursor.watermark if cursor else get_default_start(task.source_type),
                    overlap=task.config.get("overlap", config.default_overlap)
                )
                
                # 4. 创建运行记录
                run = create_sync_run(
                    task_id=task.task_id,
                    run_id=generate_run_id(),
                    watermark_start=start_watermark
                )
                
                # 5. 批量拉取并写入
                result = fetch_and_upsert(
                    task=task,
                    start_watermark=start_watermark,
                    batch_size=config.batch_size,
                    run_id=run.run_id
                )
                
                # 6. 更新 cursor（仅在成功时）
                save_cursor(
                    namespace="scm.sync",
                    key=f"{task.source_type}_cursor:{task.repo_id}",
                    value={
                        "watermark": result.end_watermark,
                        "run_id": run.run_id,
                        "updated_at": utcnow()
                    }
                )
                
                # 7. 标记运行完成
                complete_sync_run(run.run_id, result)
                return result
                
        except RetryableError as e:
            # 可重试错误：网络超时、临时不可用等
            last_error = e
            retry_count += 1
            
            if retry_count <= config.retry_count:
                # 计算退避间隔
                interval = calculate_backoff(
                    retry_count=retry_count,
                    base_interval=config.retry_interval,
                    strategy=config.retry_backoff,
                    max_interval=config.retry_max_interval,
                    jitter=config.retry_jitter
                )
                
                log.warning(f"同步失败，{interval}s 后重试 ({retry_count}/{config.retry_count}): {e}")
                update_run_status(run.run_id, "retrying", error=str(e))
                sleep(interval)
            else:
                # 重试耗尽
                log.error(f"同步重试次数耗尽: {e}")
                update_run_status(run.run_id, "exhausted", error=str(e))
                update_task_status(task.task_id, "failed")
                raise SyncExhaustedError(f"重试耗尽: {last_error}")
                
        except NonRetryableError as e:
            # 不可重试错误：配置错误、权限问题等
            log.error(f"同步遇到不可重试错误: {e}")
            update_run_status(run.run_id, "failed", error=str(e))
            update_task_status(task.task_id, "failed")
            raise


def fetch_and_upsert(task: SyncTask, start_watermark, batch_size: int, run_id: str) -> FetchResult:
    """分批拉取并写入，cursor 仅在整批完成后更新"""
    
    stats = {"fetched": 0, "inserted": 0, "updated": 0, "skipped": 0, "batches": 0}
    end_watermark = start_watermark
    
    while True:
        # 拉取一批数据
        batch = fetch_batch(
            repo_id=task.repo_id,
            source_type=task.source_type,
            since=end_watermark,
            limit=batch_size
        )
        
        if not batch.records:
            break  # 无更多数据
        
        # 事务内批量 upsert
        with db.transaction():
            for record in batch.records:
                result = upsert_record(record)
                if result.was_inserted:
                    stats["inserted"] += 1
                elif result.was_updated:
                    stats["updated"] += 1
                else:
                    stats["skipped"] += 1
        
        stats["fetched"] += len(batch.records)
        stats["batches"] += 1
        end_watermark = batch.last_watermark
        
        # 短暂休眠，避免 API 过载
        if batch.has_more:
            sleep(0.1)
    
    return FetchResult(
        end_watermark=end_watermark,
        stats=stats
    )


def calculate_backoff(retry_count: int, base_interval: int, strategy: str,
                      max_interval: int, jitter: float) -> float:
    """计算重试退避间隔"""
    
    if strategy == "fixed":
        interval = base_interval
    elif strategy == "linear":
        interval = base_interval * retry_count
    elif strategy == "exponential":
        interval = base_interval * (2 ** (retry_count - 1))
    else:
        interval = base_interval
    
    # 应用上限
    interval = min(interval, max_interval)
    
    # 应用抖动（防止惊群效应）
    if jitter > 0:
        import random
        jitter_range = interval * jitter
        interval += random.uniform(-jitter_range, jitter_range)
    
    return max(interval, 1)  # 至少 1 秒


def recover_from_crash() -> None:
    """崩溃恢复：处理中断的同步任务"""
    
    # 1. 查找状态为 running 但 worker 已失联的运行
    stale_runs = db.query("""
        SELECT r.* FROM scm.sync_runs r
        WHERE r.status = 'running'
          AND r.started_at < NOW() - INTERVAL '1 hour'
          AND NOT EXISTS (
              SELECT 1 FROM worker_heartbeats w
              WHERE w.worker_id = r.worker_id
                AND w.last_seen > NOW() - INTERVAL '5 minutes'
          )
    """)
    
    for run in stale_runs:
        log.warning(f"发现中断的运行: {run.run_id}, 标记为 failed")
        
        # 2. 标记为 failed（cursor 未更新，下次从原位置重试）
        update_run_status(
            run.run_id, 
            status="failed", 
            error="Worker 失联，任务中断"
        )
        
        # 3. 重置任务状态为 pending，等待重新调度
        update_task_status(run.task_id, "pending")
    
    log.info(f"崩溃恢复完成，处理了 {len(stale_runs)} 个中断任务")
```

#### Cursor 一致性保证

| 场景 | 保证机制 | 说明 |
|------|----------|------|
| 写入中断 | 同事务更新 | cursor 与数据在同一事务提交 |
| 部分成功 | 仅批次完成后更新 | 未完成批次不更新 cursor |
| 重复写入 | upsert 幂等 | `ON CONFLICT DO UPDATE` |
| 并发冲突 | 分布式锁 | 单仓库同时只有一个 worker |

---

### 配置示例

```toml
[scm.sync]
# 并行同步仓库数
parallelism = 4

# 单批次最大记录数
batch_size = 100

# 同步间隔（秒），0 表示手动触发
interval_seconds = 300

# 失败重试次数
retry_count = 3

# 重试间隔（秒）
retry_interval = 5

# 默认 overlap 值
default_overlap = 0

# 同步模式: strict | best_effort
mode = "strict"

# 分布式锁超时（秒）
lock_timeout = 300

# 运行记录保留天数
run_history_days = 90

# 最大允许延迟（秒），超出触发告警
max_lag_seconds = 3600

# bulk 判定阈值
bulk_threshold = 100

# diff 拉取开关
diff_fetch_enabled = true

# 单 diff 大小限制 MB
diff_size_limit_mb = 10

# 重试策略
retry_backoff = "exponential"
retry_max_interval = 60
retry_jitter = 0.1

[scm.sync.svn]
# SVN 单次 log 拉取上限
log_limit = 500

[scm.sync.git]
# 是否获取 commit stats
fetch_stats = true

[scm.sync.gitlab]
# API 分页大小
per_page = 100

# API 请求限速（每分钟）
rate_limit_rpm = 600
```
