# 工具契约（Step1）

目的：让 Cursor Agent/脚本统一以“工具调用”写入事实层，而不是编辑文件。

## 建议的最小 CLI/SDK 接口
> 具体实现建议用 Python（psycopg 或 SQLAlchemy 2.x），此处先定义契约。

### 1) 身份同步
- identity.sync
  - 输入：user.config（仓库标准 + 本地覆盖）
  - 输出：写 identity.users / identity.accounts / identity.role_profiles
  - 约束：user_id 必须稳定；svn/git 用户名允许多 alias

**CLI 命令**
```bash
python identity_sync.py [--config PATH] [--repo-root PATH] [--verbose] [--quiet]
```

**返回统计**
```json
{
  "ok": true,
  "stats": {
    "users_inserted": 2,
    "users_updated": 1,
    "accounts_inserted": 5,
    "accounts_updated": 0,
    "role_profiles_inserted": 1,
    "role_profiles_updated": 0
  },
  "summary": "用户: +2 ~1, 账户: +5 ~0, 角色配置: +1 ~0"
}
```

**幂等键**: `user_id` (users), `(account_type, account_name)` (accounts)

### 2) 事实账本写入

#### logbook.create_item
创建新的 logbook item。

**CLI 命令**
```bash
python logbook_cli.py create_item --item-type <type> --title <title> [--status <status>] [--owner <user_id>]
```

**必填字段**
| 字段 | 类型 | 说明 |
|------|------|------|
| item_type | text | 条目类型（task/bug/feature 等） |
| title | text | 条目标题 |

**可选字段**
| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| status | text | "open" | 条目状态 |
| owner_user_id | text | null | 所有者用户 ID |
| scope_json | jsonb | {} | 范围元数据 |

**返回统计**
```json
{
  "ok": true,
  "item_id": 123,
  "item_type": "task",
  "title": "新功能开发",
  "status": "open"
}
```

**幂等键**: 无（每次调用创建新记录）

---

#### logbook.add_event
为 item 添加事件。

**CLI 命令**
```bash
python logbook_cli.py add_event --item-id <id> --event-type <type> [--status-from <s>] [--status-to <s>]
```

**必填字段**
| 字段 | 类型 | 说明 |
|------|------|------|
| item_id | int | 条目 ID |
| event_type | text | 事件类型 |

**可选字段**
| 字段 | 类型 | 说明 |
|------|------|------|
| payload_json | jsonb | 事件负载 |
| status_from | text | 变更前状态 |
| status_to | text | 变更后状态（会更新 item） |
| actor_user_id | text | 操作者用户 ID |
| source | text | 事件来源（默认 "tool"） |

**返回统计**
```json
{
  "ok": true,
  "event_id": 456,
  "item_id": 123,
  "event_type": "status_change",
  "status_updated": true,
  "status_to": "in_progress"
}
```

**幂等键**: 无（追加写入）

---

#### logbook.attach
为 item 添加附件。

**CLI 命令**
```bash
python logbook_cli.py attach --item-id <id> --kind <kind> --uri <uri> [--sha256 <hash>]
```

**必填字段**
| 字段 | 类型 | 说明 |
|------|------|------|
| item_id | int | 条目 ID |
| kind | text | 附件类型（patch/log/report/spec） |
| uri | text | 附件 URI |
| sha256 | text | SHA256 哈希（本地文件可自动计算） |

**可选字段**
| 字段 | 类型 | 说明 |
|------|------|------|
| size_bytes | int | 文件大小（本地文件可自动计算） |
| meta_json | jsonb | 附件元数据 |

**返回统计**
```json
{
  "ok": true,
  "attachment_id": 789,
  "item_id": 123,
  "kind": "patch",
  "uri": "scm/1/svn/r100.diff",
  "sha256": "abc123...",
  "size_bytes": 1024,
  "local_file": true
}
```

**幂等键**: 无（允许重复附件）

---

#### logbook.set_kv
设置键值对（upsert）。

**CLI 命令**
```bash
python logbook_cli.py set_kv --namespace <ns> --key <key> --value <json>
```

**必填字段**
| 字段 | 类型 | 说明 |
|------|------|------|
| namespace | text | 命名空间 |
| key | text | 键名 |
| value_json | jsonb | 值 |

**返回统计**
```json
{
  "ok": true,
  "namespace": "scm.sync",
  "key": "svn_cursor:123",
  "upserted": true
}
```

**幂等键**: `(namespace, key)`

### 3) 可再生视图

#### logbook.render_views
生成 manifest.csv 和 index.md 视图文件。

**CLI 命令**
```bash
python logbook_cli.py render_views [--out-dir <path>] [--limit <n>] [--item-type <type>] [--status <s>]
python render_views.py [--out-dir <path>] [--limit <n>] [--log-event] [--item-id <id>]
```

**可选字段**
| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| out_dir | path | ./.agentx/logbook/views | 输出目录 |
| limit | int | 50 | 最近条目数量上限 |
| item_type | text | null | 按 item_type 筛选 |
| status | text | null | 按状态筛选 |
| log_event | bool | false | 写入 render_views 事件记录 |
| item_id | int | null | 用于记录事件的 item_id |

**产物**
- `manifest.csv` — 所有 items 的完整数据导出
- `index.md` — 最近 N 条 items 的 Markdown 导航

**返回统计**
```json
{
  "ok": true,
  "out_dir": "/path/to/views",
  "items_count": 42,
  "files": {
    "manifest": {
      "path": "/path/to/views/manifest.csv",
      "size": 12345,
      "sha256": "abc123..."
    },
    "index": {
      "path": "/path/to/views/index.md",
      "size": 2345,
      "sha256": "def456..."
    }
  },
  "rendered_at": "2024-01-15T12:30:00Z"
}
```

**约束**
- 所有字段来自 DB；禁止人工编辑（否则视为脏数据）
- 产物可随时重建

### 4) SCM 同步工具

#### scm.ensure_repo
确保仓库记录存在，若不存在则创建。

**必填字段**
| 字段 | 类型 | 说明 |
|------|------|------|
| repo_type | text | 仓库类型：`svn` \| `git` |
| url | text | 仓库 URL（作为唯一标识） |
| project_key | text | 项目标识 |

**可选字段**
| 字段 | 类型 | 说明 |
|------|------|------|
| default_branch | text | 默认分支（仅 git） |

**幂等键**: `(repo_type, url)` — 相同 type+url 不会重复创建

**写入表**: `scm.repos`

**返回统计**
```json
{
  "ok": true,
  "repo_id": 123,
  "created": true | false  // true=新建, false=已存在
}
```

---

#### scm.sync_svn_revisions
增量同步 SVN 日志，支持批量和 overlap 策略。

**必填字段**
| 字段 | 类型 | 说明 |
|------|------|------|
| svn_url | text | SVN 仓库 URL |
| project_key | text | 项目标识 |

**可选字段**
| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| batch_size | int | 100 | 每次同步的最大 revision 数 |
| overlap | int | 0 | 重叠 revision 数（重新同步已同步的部分） |

**幂等键**: `(repo_id, COALESCE(rev_num, rev_id))` — revision 号唯一

**写入表**
- `scm.repos` — 通过 ensure_repo 自动创建
- `scm.svn_revisions` — SVN revision 记录
- `logbook.kv` — 同步游标 (`scm.sync`, `svn_cursor:<repo_id>`)

**返回统计**
```json
{
  "ok": true,
  "repo_id": 123,
  "synced_count": 50,      // 新增+更新的记录数（upsert）
  "start_rev": 101,
  "end_rev": 150,
  "last_rev": 150,
  "has_more": true,        // 是否还有更多待同步
  "remaining": 200,        // 剩余待同步数（可选）
  "bulk_count": 2          // 被标记为 bulk 的 revision 数
}
```

**Bulk 检测规则**
- `changed_paths > 100` → `bulk_reason: "large_changeset:<count>"`

---

#### scm.sync_gitlab_commits
增量同步 GitLab commits，支持批量获取和 diff 存储。

**必填字段**
| 字段 | 类型 | 说明 |
|------|------|------|
| gitlab_url | text | GitLab 实例 URL |
| project_id | text/int | 项目 ID 或路径 (如 `namespace/project`) |
| private_token | text | GitLab Private Token |
| project_key | text | 项目标识 |

**可选字段**
| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| batch_size | int | 100 | 每次同步的最大 commit 数 |
| ref_name | text | null | 分支/tag 名称 |
| fetch_diffs | bool | true | 是否获取 diff 内容 |
| request_timeout | int | 60 | API 请求超时(秒) |

**幂等键**: `(repo_id, COALESCE(commit_sha, commit_id))` — commit SHA 唯一

**写入表**
- `scm.repos` — 通过 ensure_repo 自动创建
- `scm.git_commits` — Git commit 记录
- `scm.patch_blobs` — Diff 内容指针（当 fetch_diffs=true）
- `logbook.kv` — 同步游标 (`scm.sync`, `gitlab_cursor:<repo_id>`)

**返回统计**
```json
{
  "ok": true,
  "repo_id": 123,
  "synced_count": 50,      // 新增+更新的 commit 数
  "diff_count": 48,        // 成功存储的 diff 数
  "since": "2024-01-01T00:00:00Z",
  "last_commit_sha": "abc123...",
  "last_commit_ts": "2024-01-15T12:30:00Z",
  "has_more": true,        // 是否还有更多待同步
  "bulk_count": 3          // 被标记为 bulk 的 commit 数
}
```

**Bulk 检测规则**
- `stats.additions + stats.deletions > 1000` → `bulk_reason: "large_changeset:<total>"`

---

#### scm.sync_gitlab_mrs
同步 GitLab Merge Requests。

**必填字段**
| 字段 | 类型 | 说明 |
|------|------|------|
| gitlab_url | text | GitLab 实例 URL |
| project_id | text/int | 项目 ID 或路径 |
| private_token | text | GitLab Private Token |

**可选字段**
| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| state | text | "all" | MR 状态过滤: `opened`/`merged`/`closed`/`all` |
| updated_after | text | null | ISO 时间，增量过滤 |
| batch_size | int | 100 | 每次同步的最大 MR 数 |

**幂等键**: `mr_id` (格式: `<repo_id>:<iid>`)

**写入表**
- `scm.repos` — 通过 ensure_repo 自动创建
- `scm.mrs` — Merge Request 记录
- `logbook.kv` — 同步游标

**返回统计**
```json
{
  "ok": true,
  "repo_id": 123,
  "inserted": 10,          // 新增 MR 数
  "updated": 5,            // 更新 MR 数
  "skipped": 0,            // 跳过（无变化）
  "has_more": false
}
```

---

#### scm.sync_gitlab_review_events
同步 GitLab MR 的 Review 事件（评论、审批、变更请求等）。

**必填字段**
| 字段 | 类型 | 说明 |
|------|------|------|
| gitlab_url | text | GitLab 实例 URL |
| project_id | text/int | 项目 ID 或路径 |
| private_token | text | GitLab Private Token |
| mr_iid | int | MR 的项目内编号 |

**可选字段**
| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| since | text | null | ISO 时间，增量过滤 |

**幂等键**: `(mr_id, source_event_id)` — GitLab 原始事件 ID 保证唯一性

**source_event_id 来源说明**
| event_type | source_event_id 来源 | API 字段 |
|------------|---------------------|----------|
| comment | MR Note ID | `GET /merge_requests/:iid/notes` → `note.id` |
| approve | Approval ID | `GET /merge_requests/:iid/approvals` → `approved_by[].id` |
| request_changes | Note ID (带 resolved 标记) | `note.id` |
| assign/unassign | Award Emoji / 系统事件 ID | 生成 `assign:<user_id>:<ts>` |
| merge/close/reopen | MR 状态变更时间戳 | 生成 `<event_type>:<updated_at>` |

**写入表**
- `scm.mrs` — 必须先存在对应的 MR
- `scm.review_events` — Review 事件记录

**event_type 枚举**
- `comment` — 评论
- `approve` — 批准
- `request_changes` — 请求变更
- `assign` — 分配审阅者
- `unassign` — 取消分配
- `merge` — 合并
- `close` — 关闭
- `reopen` — 重新打开

**返回统计**
```json
{
  "ok": true,
  "mr_id": "123:42",
  "inserted": 15,          // 新增事件数
  "skipped": 3,            // 跳过（已存在）
  "by_type": {             // 按类型统计
    "comment": 10,
    "approve": 2,
    "assign": 3
  }
}
```

---

#### scm.materialize_patch_blob
物化 patch blob：读取数据库中 URI 不可解析的 patch_blobs 记录，按需从 SVN/GitLab 拉取 diff 内容，写入 ArtifactStore，并安全更新数据库。

**CLI 命令**
```bash
python scm_materialize_patch_blob.py [OPTIONS]

# 示例
python scm_materialize_patch_blob.py --source-type svn --batch-size 50
python scm_materialize_patch_blob.py --blob-id 123
python scm_materialize_patch_blob.py --retry-failed --json
```

**CLI 参数**
| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| --blob-id | int | null | 处理指定的 blob ID |
| --source-type | text | null | 筛选特定源类型: `svn` \| `git` |
| --batch-size | int | 50 | 每次处理的最大 blob 数 |
| --retry-failed | flag | false | 重试之前失败的记录 |
| --config | path | null | 配置文件路径 |
| --json | flag | false | 以 JSON 格式输出结果 |
| -v, --verbose | flag | false | 显示详细输出 |

**输入（读取表）**
- `scm.patch_blobs` — 待物化的 blob 记录
  - 筛选条件: `uri IS NULL OR uri = ''`（或 `--retry-failed` 时包含所有）
- `scm.repos` — 获取仓库 URL 用于拉取 diff

**输出（写入）**
- `ArtifactStore` — 写入 diff 文件到制品存储
  - 新版路径格式: `scm/<project_key>/<repo_id>/<source_type>/<rev_or_sha>/<sha256>.<ext>`
  - 旧版路径格式（只读兼容）: `scm/<repo_id>/svn/r<rev>.diff` 或 `scm/<repo_id>/git/commits/<sha>.diff`
- `scm.patch_blobs` — 更新 uri, size_bytes（仅当 sha256 匹配时）

**幂等键**: `(source_type, source_id, sha256)` — 相同内容不重复存储

**处理流程**
1. 读取 `scm.patch_blobs` 中 `uri` 为空或不可解析的记录
2. 解析 `source_id` 格式 `<repo_id>:<revision|sha>` 获取仓库和版本信息
3. 根据 `source_type` 调用对应拉取方法:
   - `svn`: 执行 `svn diff -c <rev> <repo_url>`
   - `git`: 调用 GitLab API `GET /projects/:id/repository/commits/:sha/diff`
4. 将 diff 内容写入 ArtifactStore，计算 sha256
5. **安全更新策略**: 仅当计算的 `sha256` 与数据库记录匹配时，才更新 `uri` 字段
6. 若 sha256 不匹配，返回 `checksum_mismatch` 错误，不更新数据库

**返回统计**
```json
{
  "ok": true,
  "total": 10,              // 待处理总数
  "materialized": 8,        // 成功物化数
  "skipped": 1,             // 跳过数（URI 已可解析）
  "failed": 1,              // 失败数
  "details": [              // --verbose 时包含
    {
      "blob_id": 123,
      "status": "materialized",
      "uri": "scm/1/svn/r100.diff",
      "sha256": "abc123...",
      "size_bytes": 1234
    }
  ]
}
```

**状态枚举**
| 状态 | 说明 |
|------|------|
| `materialized` | 成功物化并更新数据库 |
| `skipped` | URI 已可解析，无需处理 |
| `failed` | 物化失败 |
| `unreachable` | 外部 URI 不可达 |

**错误码**
| 错误类型 | exit_code | 说明 |
|----------|-----------|------|
| `MATERIALIZE_ERROR` | 12 | 物化错误基类 |
| `URI_NOT_RESOLVABLE` | 12 | URI 不可解析 |
| `CHECKSUM_MISMATCH` | 12 | SHA256 不匹配（源内容可能已变更） |
| `PAYLOAD_TOO_LARGE` | 12 | Diff 内容超过 10MB 限制 |
| `FETCH_ERROR` | 12 | 从 SVN/GitLab 拉取失败 |
| `VALIDATION_ERROR` | 6 | 输入验证失败（如 source_id 格式无效） |

**配置示例**
```toml
[materialize]
batch_size = 50           # 每次处理最大 blob 数

[gitlab]
url = "https://gitlab.example.com"
private_token = "glpat-xxx"
```

**安全策略**
- **SHA256 校验**: 更新数据库前必须验证计算的 sha256 与记录中的预期值匹配
- **原子更新**: 使用 `WHERE sha256 = expected_sha256` 条件，防止并发更新冲突
- **内容变更处理**: 若 sha256 不匹配，返回错误而非覆盖，由调用方决定后续处理

**URI 双轨规范**

本工具涉及两类 URI，Step1 生产环境默认使用 Artifact Key：

| URI 类型 | 字段位置 | 格式 | 用途 |
|----------|----------|------|------|
| **Artifact Key（推荐）** | `patch_blobs.uri` | 无 scheme 或 `artifact://` | 逻辑键，与物理后端解耦 |
| **Physical URI（特例）** | `patch_blobs.uri` | `file://` / `s3://` / `https://` | 物理地址，绑定特定后端 |
| **Evidence URI** | `patch_blobs.meta_json.evidence_uri` | `memory://patch_blobs/<source_type>/<source_id>/<sha256>` | 证据引用，全局唯一标识 |

**Canonical Evidence URI 规范**

Evidence URI 用于在 logbook 事件和审计场景中引用 patch blob 内容，格式为：

```
memory://patch_blobs/<source_type>/<source_id>/<sha256>
```

| 组件 | 说明 | 示例 |
|------|------|------|
| `memory://patch_blobs/` | 固定前缀 | — |
| `<source_type>` | SCM 类型 | `svn` \| `git` |
| `<source_id>` | 源标识（`<repo_id>:<rev_or_sha>`） | `1:r100` 或 `2:abc123def` |
| `<sha256>` | 内容哈希 | `e3b0c44298fc...` |

**示例**
```
memory://patch_blobs/svn/1:r100/e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
memory://patch_blobs/git/2:abc123def/a1b2c3d4e5f6789012345678901234567890abcdef1234567890123456789012
```

**Resolver 兼容性**

Evidence URI resolver 支持新旧格式自动识别：

| 格式 | 示例 | 说明 |
|------|------|------|
| 新格式（v2） | `memory://patch_blobs/svn/1:r100/<sha256>` | 推荐格式，包含完整 source_id |
| 旧格式 | `memory://patch_blobs/1/r100/<sha256>` | 兼容旧版，仅 repo_id + rev |

resolver 解析逻辑：
1. 检测 `<source_type>/` 前缀，若为 `svn/` 或 `git/` 则按新格式解析
2. 否则回退到旧格式解析（纯数字前缀视为 repo_id）
3. 查询 `scm.patch_blobs` 时使用 `(source_type, source_id, sha256)` 组合索引

**Artifact Key（生产默认）**
| 格式 | 说明 | 示例 |
|------|------|------|
| (无 scheme) | **推荐**，纯相对路径 | `scm/1/svn/r100.diff` |
| `artifact://` | 显式 scheme，等价于无 scheme | `artifact://scm/1/svn/r100.diff` |

**Physical URI（特例输入）**
| Scheme | 说明 | 示例 |
|--------|------|------|
| `file://` | 本地文件系统路径 | `file:///var/cache/patches/r100.diff` |
| `s3://` | S3/MinIO 对象存储 | `s3://bucket/engram/proj_a/r100.diff` |
| `https://` | HTTP(S) 可访问资源 | `https://gitlab.example.com/api/v4/...` |

> **生产约定**：DB 中默认存储 Artifact Key，后端切换无需修改数据。
> Physical URI 允许作为特例输入（如外部 diff URL），迁移时需使用工具转换。

**Materialize 约束**
- 相同 `(source_type, source_id, sha256)` 的 blob 仅存储一次
- `diff_content` 最大 10MB；超限时返回错误 `payload_too_large`
- 若 `sha256` 与实际计算不符，返回错误 `checksum_mismatch`
- 外部 URI 暂不触发内容拉取，仅验证可达性

**Artifact Key 优先约定**
- 物化成功后，`patch_blobs.uri` 存储为 Artifact Key（无 scheme）
- Physical URI 输入（如 `https://` diff URL）仅作为特例保留
- 后续 gc/migrate 操作基于 Artifact Key 解析实际后端路径

---

### Watermark 同步规范

本节定义 SCM 同步工具的增量同步机制：Watermark 定义、Overlap 策略、同步语义和回填模式。

#### 各 source_type 的 Watermark 定义

| source_type | watermark 类型 | 存储位置 | 格式 | 说明 |
|-------------|----------------|----------|------|------|
| svn | revision number | `logbook.kv` | `int` | SVN revision 是单调递增整数，天然适合作为 watermark |
| git | commit timestamp | `logbook.kv` | `ISO 8601 datetime` | Git commit 无全局序，使用 `committed_date` 作为时间戳 watermark |
| gitlab_mr | updated_at | `logbook.kv` | `ISO 8601 datetime` | MR 可能被修改，使用 `updated_at` 作为增量过滤依据 |
| gitlab_review | created_at | `logbook.kv` | `ISO 8601 datetime` | Review 事件按创建时间增量拉取 |

**Watermark KV 键命名规范**
```
namespace: scm.sync
key: <source_type>_cursor:<repo_id>

# 示例（cursor 值包含 watermark 和 run_id）
scm.sync / svn_cursor:1        → {"last_rev": 12345, "run_id": "sync-20240115-001"}
scm.sync / gitlab_cursor:2     → {"last_commit_ts": "2024-01-15T12:30:00Z", "run_id": "sync-20240115-002"}
scm.sync / gitlab_mr_cursor:2  → {"last_updated_at": "2024-01-15T12:30:00Z", "run_id": "sync-20240115-003"}
```

**Cursor 值字段说明**
| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `last_rev` / `last_commit_ts` / `last_updated_at` | int / datetime | ✅ | watermark 值，按 source_type 不同 |
| `run_id` | text | 可选 | 最后一次成功同步的运行标识，格式 `<prefix>-<date>-<seq>` |
| `synced_at` | datetime | 可选 | 最后一次成功同步的时间戳 |
| `synced_count` | int | 可选 | 最后一次同步的记录数 |

---

#### Overlap 策略

Overlap 用于处理边界条件和补偿潜在的数据遗漏（如网络中断导致的部分写入）。

| source_type | 默认 overlap | 推荐范围 | 说明 |
|-------------|--------------|----------|------|
| svn | 0 | 0-10 | SVN revision 严格有序，一般无需 overlap |
| git | 0 | 0-50 | Git commit 可能因时间戳精度或并发推送存在边界问题 |
| gitlab_mr | 0 | 0-20 | MR 更新时间可能存在时钟偏移 |
| gitlab_review | 0 | 0-10 | Review 事件较少，小 overlap 即可 |

**Overlap 选择原则**
- **overlap = 0**：仅拉取 watermark 之后的新数据，效率最高
- **overlap > 0**：重新拉取最近 N 条已同步的记录，用于补偿边界遗漏
- **overlap 过大**：增加 API 调用和数据库 upsert 开销

**使用场景建议**
| 场景 | 推荐 overlap | 理由 |
|------|--------------|------|
| 首次同步 / 回填 | 0 | 全量拉取，无需重叠 |
| 定时增量同步 | 0-10 | 小 overlap 补偿潜在遗漏 |
| 故障恢复后同步 | 10-50 | 较大 overlap 确保完整性 |
| 审计/校验场景 | 全量 | 使用回填模式重新同步所有 |

---

#### strict vs best_effort 同步语义

同步工具支持两种语义模式，用于控制遇到错误时的行为：

| 模式 | 行为 | 适用场景 |
|------|------|----------|
| **strict** | 遇到任何错误立即中止，不更新 watermark | 审计、合规场景，要求数据完整性 |
| **best_effort** | 记录错误并跳过问题记录，继续处理后续数据 | 日常增量同步，容忍少量数据缺失 |

**strict 模式行为**
```python
# 严格模式：任何错误都中止
for revision in fetch_revisions(since=watermark):
    try:
        upsert_revision(revision)
    except Exception as e:
        # 不更新 watermark，下次从当前位置重试
        raise SyncError(f"sync failed at rev {revision.rev}", cause=e)

# 仅当全部成功才更新 watermark
update_watermark(last_rev)
```

**best_effort 模式行为**
```python
# 尽力模式：记录错误，继续处理
errors = []
last_success_rev = watermark
for revision in fetch_revisions(since=watermark):
    try:
        upsert_revision(revision)
        last_success_rev = revision.rev
    except Exception as e:
        errors.append({"rev": revision.rev, "error": str(e)})
        continue  # 跳过错误记录，继续处理

# 更新 watermark 到最后成功位置
update_watermark(last_success_rev)

# 返回统计包含错误信息
return {"synced": n, "errors": errors, "mode": "best_effort"}
```

**CLI 参数**
```bash
python scm_sync_svn.py --mode strict    # 严格模式（默认）
python scm_sync_svn.py --mode best_effort --error-log /path/to/errors.json
```

**返回统计扩展**
```json
{
  "ok": true,
  "mode": "best_effort",
  "synced_count": 48,
  "error_count": 2,
  "errors": [
    {"rev": 1234, "error": "diff fetch timeout"},
    {"rev": 1250, "error": "author mapping failed"}
  ]
}
```

---

#### 回填模式（Backfill）

回填模式用于重新同步历史数据，忽略现有 watermark，从指定起点开始全量或范围同步。

**回填场景**
| 场景 | 触发条件 | 回填范围 |
|------|----------|----------|
| 首次接入 | 仓库首次注册 | 全量（从 rev 1 或最早 commit） |
| Schema 变更 | 新增字段需要回填 | 全量或受影响范围 |
| 数据修复 | 发现历史数据错误 | 指定范围 |
| 迁移验证 | 数据库迁移后校验 | 全量比对 |

**CLI 参数**
```bash
# 忽略 watermark，从指定位置开始
python scm_sync_svn.py --backfill --start-rev 1000 --end-rev 2000

# 全量回填（危险操作，需确认）
python scm_sync_svn.py --backfill --full --force

# Git 回填：指定时间范围
python scm_sync_gitlab.py --backfill --since 2023-01-01 --until 2024-01-01
```

**回填模式标志**
| 参数 | 类型 | 说明 |
|------|------|------|
| --backfill | flag | 启用回填模式，忽略现有 watermark |
| --start-rev / --since | int/datetime | 回填起点 |
| --end-rev / --until | int/datetime | 回填终点（可选，默认到最新） |
| --full | flag | 全量回填（从头开始） |
| --force | flag | 跳过确认提示（用于自动化） |
| --dry-run | flag | 仅分析不写入 |

**回填返回统计**
```json
{
  "ok": true,
  "mode": "backfill",
  "range": {
    "start": 1000,
    "end": 2000,
    "type": "revision"
  },
  "total_in_range": 1001,
  "synced_count": 1001,
  "upserted": 950,
  "unchanged": 51,
  "backfill_completed": true
}
```

**回填与 Watermark 交互**

> **核心原则**：回填模式**默认不推进 watermark**，必须显式使用 `--update-watermark` 才会更新。

| 行为 | 说明 |
|------|------|
| **默认行为** | 回填完成后 watermark 保持不变，不影响后续增量同步 |
| **显式更新** | 添加 `--update-watermark` 参数，回填完成后将 watermark 更新到回填范围的终点 |
| **设计理由** | 确保回填过程可随时中断和恢复，不会意外跳过未同步区间 |

```bash
# 默认行为：回填后不更新 watermark
python scm_sync_svn.py --backfill --start-rev 1000 --end-rev 2000
# watermark 仍停留在原位置

# 显式更新：回填后将 watermark 推进到 2000
python scm_sync_svn.py --backfill --start-rev 1000 --end-rev 2000 --update-watermark

# 或手动更新
python logbook_cli.py set_kv --namespace scm.sync --key svn_cursor:1 --value '{"last_rev": 2000}'
```

**注意事项**
- 若回填范围与现有 watermark 不连续，不使用 `--update-watermark` 可避免产生同步空洞
- 全量回填 (`--full`) 后通常应使用 `--update-watermark` 将 watermark 设置到最新位置
- `--dry-run` 模式下 `--update-watermark` 参数被忽略

---

### SCM 路径规范

#### 新版路径格式（v2）

```
scm/<project_key>/<repo_id>/<source_type>/<rev_or_sha>/<sha256>.<ext>
```

| 层级 | 说明 | 示例 |
|------|------|------|
| `scm/` | 固定前缀 | `scm/` |
| `<project_key>/` | 项目标识 | `proj_a/` |
| `<repo_id>/` | 仓库 ID | `1/` |
| `<source_type>/` | SCM 类型 | `svn/` 或 `git/` |
| `<rev_or_sha>/` | 版本标识 | SVN: `r<rev>/`，Git: `<sha>/` |
| `<sha256>.<ext>` | 文件名 | `e3b0c44...diff` |

**rev_or_sha 命名规则**

| source_type | 格式 | 说明 | 示例 |
|-------------|------|------|------|
| `svn` | `r<rev>` | `r` 前缀 + 纯数字 revision | `r100`, `r12345` |
| `git` | `<sha>` | 完整或短 commit SHA | `abc123def`, `abc123def456...` |

> **设计理由**：SVN revision 使用 `r` 前缀以避免与纯数字 repo_id 混淆，同时与 SVN 命令行惯例一致（如 `svn log -r100`）。

**扩展名说明**
| ext | 说明 |
|-----|------|
| `diff` | 完整 diff 内容 |
| `diffstat` | diffstat 统计信息（用于 bulk commit） |
| `ministat` | 精简统计信息 |

**示例**
```
scm/proj_a/1/svn/r100/e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855.diff
scm/proj_a/2/git/abc123def/a1b2c3d4e5f6789012345678901234567890abcdef1234567890123456789012.diffstat
```

#### 旧版路径格式（只读兼容）

新版本写入使用新格式，但读取时自动回退到旧版路径。

| 类型 | 旧版格式 | 示例 |
|------|----------|------|
| SVN | `scm/<repo_id>/svn/r<rev>.<ext>` | `scm/1/svn/r100.diff` |
| Git | `scm/<repo_id>/git/commits/<sha>.<ext>` | `scm/1/git/commits/abc123.diff` |

**回退读取逻辑**
```python
from engram_step1.uri import resolve_scm_artifact_path

# 优先查找新版路径，若不存在则回退到旧版路径
path = resolve_scm_artifact_path(
    project_key="proj_a",
    repo_id="1",
    source_type="svn",
    rev_or_sha="100",
    sha256="abc123...",
    ext="diff"
)
```

---

### 5) ArtifactStore 工具

ArtifactStore 提供统一的制品存储接口，支持 local/file/object 三种后端，自动处理路径规范、权限验证和校验。

---

#### artifacts.write
写入制品到 ArtifactStore。

**CLI 命令**
```bash
python artifact_cli.py write --path <relative_path> --file <local_file>
python artifact_cli.py write --path <relative_path> --stdin
python artifact_cli.py write --path <relative_path> --content <text>
```

**必填字段**
| 字段 | 类型 | 说明 |
|------|------|------|
| path | text | 制品相对路径（如 `scm/1/svn/r100.diff`） |

**输入来源（三选一）**
| 字段 | 类型 | 说明 |
|------|------|------|
| file | path | 本地源文件路径 |
| stdin | flag | 从标准输入读取 |
| content | bytes | 直接传入内容 |

**可选字段**
| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| expected_sha256 | text | null | 预期 SHA256（用于校验） |
| overwrite | bool | false | 是否覆盖已存在文件 |
| meta_json | jsonb | {} | 元数据（存储于对象存储时作为 headers） |

**返回统计**
```json
{
  "ok": true,
  "path": "scm/1/svn/r100.diff",
  "uri": "scm/1/svn/r100.diff",
  "sha256": "abc123...",
  "size_bytes": 1234,
  "backend": "local",
  "created": true
}
```

**错误码**
| 错误类型 | exit_code | 说明 |
|----------|-----------|------|
| `PATH_TRAVERSAL` | 2 | 路径包含 `../` 等穿越尝试 |
| `PREFIX_NOT_ALLOWED` | 2 | 路径不在允许的前缀范围 |
| `CHECKSUM_MISMATCH` | 12 | SHA256 与预期不符 |
| `FILE_EXISTS` | 12 | 文件已存在且 overwrite=false |
| `QUOTA_EXCEEDED` | 12 | 超出大小限制 |
| `PERMISSION_DENIED` | 13 | 写入权限不足 |

**安全约束**
- 自动检测并拒绝路径穿越攻击（`../`）
- 检查 `allowed_prefixes` 配置（如已设置）
- 强制 SHA256 校验（如提供 `expected_sha256`）

---

#### artifacts.read
读取制品内容。

**CLI 命令**
```bash
python artifact_cli.py read --path <relative_path>
python artifact_cli.py read --uri <full_uri>
python artifact_cli.py read --path <path> --output <local_file>
```

**输入（二选一）**
| 字段 | 类型 | 说明 |
|------|------|------|
| path | text | 制品相对路径 |
| uri | text | 完整 URI（`file://`、`s3://` 等） |

**可选字段**
| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| output | path | null | 输出到本地文件（不指定则输出到 stdout） |
| verify_sha256 | text | null | 校验 SHA256 |

**返回统计**
```json
{
  "ok": true,
  "path": "scm/1/svn/r100.diff",
  "size_bytes": 1234,
  "sha256": "abc123...",
  "backend": "local",
  "output": "/tmp/r100.diff"
}
```

**错误码**
| 错误类型 | exit_code | 说明 |
|----------|-----------|------|
| `NOT_FOUND` | 11 | 制品不存在 |
| `CHECKSUM_MISMATCH` | 12 | 读取后 SHA256 校验失败 |
| `PERMISSION_DENIED` | 13 | 读取权限不足 |

---

#### artifacts.exists
检查制品是否存在。

**CLI 命令**
```bash
python artifact_cli.py exists --path <relative_path>
python artifact_cli.py exists --uri <full_uri>
```

**返回统计**
```json
{
  "ok": true,
  "path": "scm/1/svn/r100.diff",
  "exists": true,
  "size_bytes": 1234,
  "backend": "local"
}
```

---

#### artifacts.delete
删除制品。

**CLI 命令**
```bash
python artifact_cli.py delete --path <relative_path>
python artifact_cli.py delete --uri <full_uri> --force
```

**可选字段**
| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| force | bool | false | 忽略不存在错误 |

**返回统计**
```json
{
  "ok": true,
  "path": "scm/1/svn/r100.diff",
  "deleted": true,
  "backend": "local"
}
```

---

#### artifacts.verify
批量验证制品完整性。

**CLI 命令**
```bash
python artifact_cli.py verify --paths <path1,path2,...>
python artifact_cli.py verify --db-table scm.patch_blobs --limit 100
python artifact_cli.py verify --prefix scm/1/ --recursive
```

**输入（三选一）**
| 字段 | 类型 | 说明 |
|------|------|------|
| paths | list | 制品路径列表 |
| db_table | text | 从数据库表读取 uri 和 sha256 |
| prefix | text | 按前缀扫描 |

**可选字段**
| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| limit | int | 1000 | 最大验证数量 |
| recursive | bool | true | 递归扫描子目录 |
| fix_missing | bool | false | 尝试重新物化缺失制品 |

**返回统计**
```json
{
  "ok": true,
  "total": 100,
  "valid": 98,
  "missing": 1,
  "corrupted": 1,
  "details": [
    {
      "path": "scm/1/svn/r100.diff",
      "status": "valid",
      "sha256": "abc123..."
    },
    {
      "path": "scm/1/svn/r101.diff",
      "status": "missing"
    },
    {
      "path": "scm/1/svn/r102.diff",
      "status": "corrupted",
      "expected_sha256": "def456...",
      "actual_sha256": "ghi789..."
    }
  ]
}
```

---

#### artifacts.gc
垃圾回收：清理孤立制品和临时文件。

**CLI 命令**
```bash
python artifact_cli.py gc --dry-run
python artifact_cli.py gc --orphan-days 90 --delete
python artifact_cli.py gc --tmp-days 7 --delete
```

**可选字段**
| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| orphan_days | int | 90 | 孤立制品保留天数 |
| tmp_days | int | 7 | 临时文件保留天数 |
| dry_run | bool | true | 仅列出待删除，不实际删除 |
| delete | bool | false | 执行删除 |

**返回统计**
```json
{
  "ok": true,
  "scanned": 1000,
  "orphans": {
    "count": 15,
    "size_bytes": 1234567,
    "deleted": false
  },
  "tmp_files": {
    "count": 8,
    "size_bytes": 45678,
    "deleted": false
  }
}
```

**GC 判定规则**
- **孤立制品**：存储中存在但数据库无引用（`patch_blobs.uri`、`attachments.uri`）
- **临时文件**：路径匹配 `tmp/` 或文件名包含 `.tmp.`

**Artifact Key 处理**
- GC 扫描时，将 DB 中的 Artifact Key 解析为当前后端的实际路径
- 支持 Artifact Key（无 scheme）和 Physical URI（有 scheme）混合场景
- Physical URI 直接按其 scheme 解析（如 `s3://` 调用 S3 API 检查）

---

#### artifacts.migrate
跨后端迁移制品。

**CLI 命令**
```bash
python artifact_cli.py migrate \
  --source-backend local --source-root ./.agentx/artifacts \
  --target-backend object \
  --dry-run

python artifact_cli.py migrate \
  --source-backend file --source-uri file:///mnt/nfs/artifacts \
  --target-backend object \
  --update-db --verify
```

**必填字段**
| 字段 | 类型 | 说明 |
|------|------|------|
| source_backend | text | 源后端类型 |
| target_backend | text | 目标后端类型 |

**可选字段**
| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| source_root | path | 配置值 | 源 local 后端根目录 |
| source_uri | text | null | 源 file 后端 URI |
| prefix | text | null | 仅迁移匹配前缀的制品 |
| dry_run | bool | true | 仅分析，不执行迁移 |
| verify | bool | true | 迁移后校验 SHA256 |
| update_db | bool | false | 更新数据库 uri 引用 |
| delete_source | bool | false | 迁移成功后删除源文件 |
| normalize_to_artifact_key | bool | true | 迁移后将 uri 规范化为 Artifact Key |

**Artifact Key 迁移约定**
- 默认行为：迁移后 DB 中 uri 字段存储为 Artifact Key（无 scheme）
- 若源 uri 为 Physical URI（如 `file://`），迁移后转换为 Artifact Key
- 设置 `normalize_to_artifact_key=false` 可保留 Physical URI 格式（不推荐）

**返回统计**
```json
{
  "ok": true,
  "total": 500,
  "migrated": 498,
  "skipped": 2,
  "failed": 0,
  "db_updated": 498,
  "source_deleted": 0,
  "size_bytes": 12345678
}
```

---

### ArtifactStore 后端配置参考

#### local 后端
```toml
[artifacts]
backend = "local"
root = "./.agentx/artifacts"
# allowed_prefixes = ["scm/", "attachments/"]  # 可选安全限制
```

#### file 后端
```toml
[artifacts]
backend = "file"
root = "file:///mnt/nfs/artifacts/proj_a"
```

#### object 后端
```toml
[artifacts]
backend = "object"
object_region = "us-east-1"
object_prefix = "engram/"

# 敏感凭证必须通过环境变量注入
# ENGRAM_S3_ENDPOINT=https://minio.example.com
# ENGRAM_S3_ACCESS_KEY=***
# ENGRAM_S3_SECRET_KEY=***
# ENGRAM_S3_BUCKET=artifacts
```

---

## 强制字段与一致性规则（建议）
- 所有表包含：created_at、created_by（actor_user_id）、source（tool/agent）
- events 追加写入，不做物理删除
- attachments 仅存指针 + hash，不存大文本
- SCM 同步使用 KV 游标实现增量，命名空间为 `scm.sync`
- ArtifactStore 写入后必须校验 SHA256，确保数据完整性

## URI 存储规范

**生产默认：Artifact Key 优先**
- `patch_blobs.uri`、`attachments.uri` 默认存储 Artifact Key（无 scheme）
- Artifact Key 与物理后端解耦，后端切换（local → S3）无需修改 DB

**特例输入：Physical URI**
- 允许输入 Physical URI（`file://`、`s3://`、`https://`）
- Physical URI 绑定特定后端，后端切换时需 migrate 工具更新

**需跟随调整的模块**
| 模块 | 调整要求 |
|------|----------|
| **audit** | 审计日志记录 Artifact Key，便于跨环境追溯 |
| **gc** | 将 Artifact Key 解析为实际后端路径进行扫描 |
| **migrate** | 支持 Physical URI → Artifact Key 转换 |
| **cli** | 输入支持两种格式，内部统一为 Artifact Key 存储 |

---

## 验收清单

本清单用于验证工具契约实现的完整性和正确性。

### 1. Evidence URI 规范

- [ ] Evidence URI 格式遵循 `memory://patch_blobs/<source_type>/<source_id>/<sha256>`
- [ ] resolver 能正确解析新格式（含 source_type 前缀）
- [ ] resolver 能兼容旧格式（纯数字 repo_id 前缀）
- [ ] `patch_blobs.meta_json.evidence_uri` 字段按规范写入

### 2. SCM 路径规范（v2）

- [ ] 新版路径格式：`scm/<project_key>/<repo_id>/<source_type>/<rev_or_sha>/<sha256>.<ext>`
- [ ] SVN 版本标识使用 `r<rev>` 格式（如 `r100/`）
- [ ] Git 版本标识使用完整或短 SHA（如 `abc123def/`）
- [ ] 读取时自动回退到旧版路径格式

### 3. Backfill 模式

- [ ] `--backfill` 模式默认**不**更新 watermark
- [ ] 仅当显式指定 `--update-watermark` 时才推进 watermark
- [ ] `--dry-run` 模式下 `--update-watermark` 被忽略
- [ ] 回填范围统计正确返回（`range.start`, `range.end`, `synced_count`）

### 4. Watermark 同步

- [ ] 各 source_type 的 watermark 按规范存储在 `logbook.kv`
- [ ] KV 键命名遵循 `<source_type>_cursor:<repo_id>` 格式
- [ ] overlap 参数按预期工作（重叠拉取指定数量记录）
- [ ] strict/best_effort 模式行为正确

### 5. Materialize 工具

- [ ] 物化成功后 URI 存储为 Artifact Key（无 scheme）
- [ ] SHA256 校验通过后才更新数据库
- [ ] 校验失败返回 `CHECKSUM_MISMATCH` 错误
- [ ] `--retry-failed` 参数可重试之前失败的记录

### 6. ArtifactStore

- [ ] 支持 local/file/object 三种后端
- [ ] 路径穿越攻击被正确拦截
- [ ] SHA256 校验功能正常
- [ ] GC 能正确识别孤立制品和临时文件
- [ ] migrate 工具支持 Physical URI → Artifact Key 转换

### 7. 通用约束

- [ ] 所有写入记录包含 `created_at`、`created_by`、`source` 字段
- [ ] 幂等键约束按规范生效
- [ ] 错误码与 exit_code 按规范返回
- [ ] JSON 输出格式符合契约定义
