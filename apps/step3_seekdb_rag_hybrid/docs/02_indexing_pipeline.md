# 索引管道：从 Step1 产物 -> Step3 索引

## 输入来源（按优先级）
1) Step1：logbook.attachments（patch/log/report/spec 的 URI + sha256）
2) Step1：scm.patch_blobs（svn/git 的 patch 指针）
3) 仓库内规范文档（/docs、/spec、/.agentx 等）

## Step1 数据表字段清单

### scm.patch_blobs
| 字段 | 类型 | 说明 |
|------|------|------|
| blob_id | bigserial | 主键，用作游标 |
| source_type | text | 'svn' 或 'git' |
| source_id | text | 格式: `<repo_id>:<rev>` 或 `<repo_id>:<sha>` |
| uri | text | artifacts 存储路径 |
| sha256 | text | 内容哈希 |
| size_bytes | bigint | 文件大小 |
| format | text | 'diff'（完整）或 'diffstat'（bulk 摘要） |
| chunking_version | text | 可选，标记已索引版本 |
| created_at | timestamptz | 创建时间 |

### logbook.attachments
| 字段 | 类型 | 说明 |
|------|------|------|
| attachment_id | bigserial | 主键，用作游标 |
| item_id | bigint | 关联 logbook.items |
| kind | text | 'patch'/'log'/'report'/'spec' 等 |
| uri | text | artifacts 存储路径 |
| sha256 | text | 内容哈希 |
| size_bytes | bigint | 文件大小 |
| meta_json | jsonb | 可含 project_key, module, owner_user_id 等 |
| created_at | timestamptz | 创建时间 |

## patch_blobs -> Evidence Packet 映射

`scm.patch_blobs` 每行可直接映射为 Evidence Packet 的一条 Evidence：

```
patch_blobs 字段            ->  Evidence Packet 字段
---------------------------------------------------------
source_type                 ->  source_type
source_id                   ->  source_id
uri                         ->  uri
sha256                      ->  sha256
format                      ->  format
repos.project_key           ->  project_key
repos.url                   ->  repo_url
svn_revisions/git_commits
  .author_raw               ->  author
  .meta_json.resolved_user_id -> author_user_id
  .ts                       ->  commit_ts
  .message                  ->  commit_message
  .is_bulk                  ->  is_bulk
(indexer 生成)              ->  chunk_idx, chunk_content
(RAG 返回)                  ->  relevance_score
```

## 分块（chunking）策略建议
- patch/diff：按文件 + hunk 分块
- log：按 error 段落/时间窗口分块
- md/spec：按标题层级分块（h2/h3）

每个 chunk 的 metadata 至少包含：
- project_key, repo_id
- source_type (svn/git/log/spec)
- source_id (rev/commit/event_id)
- artifact_uri, sha256
- owner_user_id（若可推断）
- module/path_prefix

## 后端配置

### Unified Stack 推荐默认值

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| STEP3_INDEX_BACKEND | pgvector | 索引后端类型（pgvector/seekdb） |
| STEP3_PGVECTOR_DSN | - | PGVector 连接字符串（如 postgresql://user:pass@host:5432/dbname） |
| STEP3_PG_SCHEMA | step3 | PGVector 数据库 schema（legacy 别名：`STEP3_SCHEMA`） |
| STEP3_PG_TABLE | chunks | PGVector 表名，不应包含 schema 前缀（legacy 别名：`STEP3_TABLE`） |
| STEP3_PG_VECTOR_DIM | 1536 | 向量维度 |
| STEP3_VECTOR_WEIGHT | 0.7 | 混合检索：向量分数权重 |
| STEP3_TEXT_WEIGHT | 0.3 | 混合检索：全文分数权重 |
| STEP3_PGVECTOR_COLLECTION_STRATEGY | single_table | Collection 策略：per_table/single_table/routing |
| STEP3_PGVECTOR_AUTO_INIT | 1 | 是否自动初始化 pgvector 后端（兼容别名：`STEP3_AUTO_INIT`） |

**STEP3_PGVECTOR_AUTO_INIT 说明**：
- 推荐名称：`STEP3_PGVECTOR_AUTO_INIT`
- 兼容别名：`STEP3_AUTO_INIT`（已废弃，计划于 **2026-Q3** 移除）
- 布尔解析规则：支持 `1/0/true/false/yes/no`（不区分大小写）
- 当 canonical 与别名同时设置且值冲突时，默认报错；可设置 `STEP3_ENV_ALLOW_CONFLICT=1` 改为仅警告
- 默认值：`1`（启用自动初始化）

**STEP3_PGVECTOR_COLLECTION_STRATEGY 策略说明**：
- `per_table`：每个 collection 独立表，适合数据隔离要求严格的场景
- `single_table`（默认）：所有 collection 共享一张表，通过 `collection_id` 列区分，简化运维
- `routing`：路由策略，根据规则选择 shared_table 或 per_table

**Legacy 值映射**（兼容旧配置，计划于 **2026-Q3** 移除）：
| Legacy 值 | 映射到 | 说明 |
|-----------|--------|------|
| `single` | `single_table` | 旧版简写 |
| `shared` | `single_table` | 旧版别名 |
| `per_project` | `per_table` | 旧版命名 |
| `per-collection` | `per_table` | 旧版命名 |

**Routing 策略配置**（仅当 `STEP3_PGVECTOR_COLLECTION_STRATEGY=routing` 时生效）：

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| STEP3_PGVECTOR_ROUTING_SHARED_TABLE | chunks_shared | 路由命中时使用的共享表名 |
| STEP3_PGVECTOR_COLLECTION_ROUTING_ALLOWLIST | - | 精确匹配列表（逗号分隔） |
| STEP3_PGVECTOR_COLLECTION_ROUTING_PREFIX | - | project_key 前缀列表（逗号分隔） |
| STEP3_PGVECTOR_COLLECTION_ROUTING_REGEX | - | 正则表达式列表（逗号分隔） |

### SeekDB 后端配置

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| SEEKDB_HOST | localhost | SeekDB 服务器地址 |
| SEEKDB_PORT | 19530 | SeekDB 服务器端口 |
| SEEKDB_API_KEY | - | API Key（可选） |
| SEEKDB_NAMESPACE | engram | 默认命名空间 |
| SEEKDB_VECTOR_DIM | 1536 | 向量维度 |
| SEEKDB_TIMEOUT | 30 | 连接超时（秒） |

### PGVector 前置条件

使用 PGVector 后端需满足以下条件：

1. **PostgreSQL 实例**：版本 ≥ 14，推荐使用 `pgvector/pgvector:pg16` 镜像
2. **pgvector 扩展**：需在目标数据库中创建扩展
   ```sql
   CREATE EXTENSION IF NOT EXISTS vector;
   ```
3. **角色权限**：连接用户需具备 schema 和表的创建/写入权限
   ```sql
   -- 确保用户有 schema 创建权限（或使用已存在的 schema）
   GRANT CREATE ON DATABASE engram TO <user>;
   -- 或使用现有 schema
   GRANT USAGE, CREATE ON SCHEMA step3 TO <user>;
   ```

完整连接配置示例：
```bash
export STEP3_PGVECTOR_DSN="postgresql://user:pass@localhost:5432/engram"
export STEP3_PG_SCHEMA="step3"
export STEP3_PGVECTOR_COLLECTION_STRATEGY="single_table"
```

## 增量/全量/回滚

### Collection 命名规范

Canonical Collection ID 格式：`{project_key}:{chunking_version}:{embedding_model_id}[:{version_tag}]`

| 组成部分 | 说明 | 示例 |
|----------|------|------|
| project_key | 项目标识，默认 "default" | proj1, default |
| chunking_version | 分块版本 | v1, v1-2026-01 |
| embedding_model_id | Embedding 模型 ID | bge-m3, nomodel |
| version_tag | 可选的版本标签（时间戳） | 20260128T120000 |

完整示例：`proj1:v1:bge-m3:20260128T120000`

各后端映射（参见 `collection_naming.py`）：
- **SeekDB**: 下划线分隔 → `proj1_v1_bge_m3`
- **PGVector**: 带前缀表名 → `step3_chunks_proj1_v1_bge_m3`

### 游标存储

游标存储位置：`logbook.kv`
- namespace: `seekdb.sync:{backend}:{collection}` （按 backend + collection 隔离）
- key: `cursor:patch_blobs` 或 `cursor:attachments`
- active collection: `active_collection:{project_key}` 存储在 `seekdb.sync:{backend}` 下

### 增量同步

增量同步使用游标记录同步位置，只处理新增数据：

```bash
# 增量同步（默认模式）
python seek_indexer.py --mode incremental

# 指定项目和数据源
python seek_indexer.py --mode incremental --project-key proj1 --source patch_blobs
```

同步流程：
1. 根据 backend + collection 生成 namespace
2. 读取游标 `cursor:patch_blobs` 获取 last_id
3. 执行增量 SQL: `WHERE blob_id > :last_blob_id ORDER BY blob_id LIMIT :batch_size`
4. 读取原文 + 分块 + upsert 索引
5. 更新游标（包含 embedding 模型信息用于一致性检查）

关键原则：
- 只同步新增/变更的附件与 patch
- 幂等 upsert（chunk_id 稳定，格式见下）
- 不同 backend/collection 的游标完全隔离

**chunk_id 生成规则**（参见 `step3_chunking.py::generate_chunk_id`）：

```
格式: <namespace>:<source_type>:<source_id>:<sha256_prefix>:<chunking_version>:<chunk_idx>

示例: engram:svn:1.12345:abc123def456:v1-2026-01:0
```

| 组件 | 说明 |
|------|------|
| namespace | 命名空间，默认 "engram" |
| source_type | svn/git/logbook |
| source_id | 来源标识（冒号替换为点，如 `1.12345`） |
| sha256_prefix | 内容哈希前 12 位 |
| chunking_version | 分块版本（如 v1-2026-01） |
| chunk_idx | 分块索引 |

### 全量重建

全量重建会创建新的 collection（带时间戳版本标签），保留旧 collection 便于回滚：

```bash
# 全量重建（创建新 collection，不激活）
python seek_indexer.py --mode full

# 全量重建并激活新 collection
python seek_indexer.py --mode full --activate

# 指定版本标签
python seek_indexer.py --mode full --version-tag v2.0.0 --activate

# 预览模式
python seek_indexer.py --mode full --dry-run --json
```

全量重建流程：
1. 生成新 collection 名称（含时间戳版本标签）
2. 新 collection 游标从 0 开始，相当于全量索引
3. 完成后可选择激活新 collection（设置 active_collection）
4. 旧 collection 保留，可随时回滚

### 回滚

回滚操作切换 active collection 到指定版本：

```bash
# 回滚到指定 collection
python seek_indexer.py --mode rollback --collection "proj1:v1:bge-m3:20260128T100000"

# 回滚指定项目
python seek_indexer.py --mode rollback --collection "proj1:v1:bge-m3:20260128T100000" --project-key proj1
```

回滚原理：
- 只修改 `active_collection` 标记，不删除任何数据
- 应用层查询时根据 active_collection 决定使用哪个 collection
- 支持快速切换，无需重建索引

### CLI 参数说明

| 参数 | 说明 | 适用模式 |
|------|------|----------|
| --mode | 同步模式：incremental/full/single/rollback | 全部 |
| --source | 数据源：patch_blobs/attachments/all | incremental/full |
| --project-key | 项目标识过滤 | incremental/full/rollback |
| --collection | 目标/回滚 collection | incremental/rollback |
| --version-tag | 版本标签 | full |
| --activate | 完成后激活新 collection | full |
| --batch-size | 批量大小（默认 100） | incremental/full |
| --dry-run | 预览模式，不实际写入 | 全部 |
| --json | JSON 格式输出 | 全部 |

## SQL 示例

详细的增量读取 SQL 参见：
`../templates/seek_indexer_stub.py` 中的 `SQL_FETCH_PATCH_BLOBS` 和 `SQL_FETCH_ATTACHMENTS`

## CI 验证策略

### Standard CI（每次 PR/Push）

| 验证项 | 命令示例 | 说明 |
|--------|----------|------|
| Step3 Unit Test | `make test-step3-unit` | 纯单元测试（≤5min），不依赖真实 Postgres |
| Step3 Smoke Test | `make step3-run-smoke` | 索引同步 + 检索验证（≤8min，STEP3_SKIP_CHECK=1） |
| PGVector Integration | `make test-step3-pgvector` | PGVector 集成测试（≤10min） |

Standard CI 特点：
- 执行时间 < 30 分钟（含统一栈启动）
- 使用 `STEP3_SKIP_CHECK=1` 跳过一致性检查，加速 CI 反馈
- 每次代码变更必跑

### Nightly CI（每日定时）

| 验证项 | 命令示例 | 说明 |
|--------|----------|------|
| PGVector 集成测试 | `make test-step3-pgvector` | 完整 PGVector 集成测试（≤10min） |
| Collection Migrate | `make step3-migrate-dry-run` | 迁移脚本 dry-run 验证（≤5min） |
| Smoke Test (Full) | `make step3-run-smoke` | 含一致性检查（STEP3_SKIP_CHECK=0，≤10min） |

Nightly CI 特点：
- 执行时间 30-60 分钟
- 使用真实 PGVector 容器（`pgvector/pgvector:pg16`）
- 执行完整一致性检查，确保数据完整性

### CI 环境变量配置示例

```bash
# Standard CI（PGVector 真实测试，跳过一致性检查）
export STEP3_INDEX_BACKEND=pgvector
export STEP3_PG_SCHEMA=step3_test
export STEP3_PGVECTOR_COLLECTION_STRATEGY=single_table
export STEP3_SKIP_CHECK="1"
export TEST_PGVECTOR_DSN="postgresql://postgres:postgres@localhost:5432/engram"

# Nightly CI（完整验证）
export STEP3_INDEX_BACKEND=pgvector
export STEP3_PGVECTOR_DSN="postgresql://postgres:postgres@localhost:5432/engram"
export STEP3_PGVECTOR_COLLECTION_STRATEGY=single_table
export STEP3_SKIP_CHECK="0"
export STEP3_SMOKE_INDEX_SAMPLE_SIZE="30"
export STEP3_SMOKE_LIMIT="50"
```
