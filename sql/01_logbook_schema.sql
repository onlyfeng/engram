-- Logbook：Postgres Schema / DDL（最小可用集）
-- 约定：每项目一个 DB（如 proj_a），本文件只创建我们自有 schema 与表；
-- OpenMemory 的表由其迁移管理（使用独立 openmemory schema，不再依赖 public）。
-- 详见 05_openmemory_roles_and_grants.sql 中的权限配置。

BEGIN;

CREATE SCHEMA IF NOT EXISTS identity;
CREATE SCHEMA IF NOT EXISTS logbook;
CREATE SCHEMA IF NOT EXISTS scm;
CREATE SCHEMA IF NOT EXISTS analysis;
CREATE SCHEMA IF NOT EXISTS governance;

-- ---------- identity ----------
CREATE TABLE IF NOT EXISTS identity.users (
  user_id            text PRIMARY KEY,
  display_name       text NOT NULL,
  is_active          boolean NOT NULL DEFAULT true,
  roles_json         jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS identity.accounts (
  account_id         bigserial PRIMARY KEY,
  user_id            text NOT NULL REFERENCES identity.users(user_id),
  account_type       text NOT NULL CHECK (account_type IN ('svn','gitlab','git','email')),
  account_name       text NOT NULL,
  email              text,
  aliases_json       jsonb NOT NULL DEFAULT '[]'::jsonb,
  verified           boolean NOT NULL DEFAULT false,
  updated_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE(account_type, account_name)
);

CREATE TABLE IF NOT EXISTS identity.role_profiles (
  user_id            text PRIMARY KEY REFERENCES identity.users(user_id),
  profile_sha        text NOT NULL,
  profile_md         text NOT NULL,
  scope_yaml         text,
  review_rules_md    text,
  source_path        text,
  updated_at         timestamptz NOT NULL DEFAULT now()
);

-- ---------- logbook ----------
CREATE TABLE IF NOT EXISTS logbook.items (
  item_id            bigserial PRIMARY KEY,
  item_type          text NOT NULL,
  title              text NOT NULL,
  scope_json         jsonb NOT NULL DEFAULT '{}'::jsonb,
  status             text NOT NULL DEFAULT 'open',
  owner_user_id      text REFERENCES identity.users(user_id),
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS logbook.events (
  event_id           bigserial PRIMARY KEY,
  item_id            bigint NOT NULL REFERENCES logbook.items(item_id) ON DELETE CASCADE,
  event_type         text NOT NULL,
  status_from        text,
  status_to          text,
  payload_json       jsonb NOT NULL DEFAULT '{}'::jsonb,
  actor_user_id      text REFERENCES identity.users(user_id),
  source             text NOT NULL DEFAULT 'tool',
  created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_logbook_events_item_time ON logbook.events(item_id, created_at);

CREATE TABLE IF NOT EXISTS logbook.attachments (
  attachment_id      bigserial PRIMARY KEY,
  item_id            bigint NOT NULL REFERENCES logbook.items(item_id) ON DELETE CASCADE,
  kind               text NOT NULL, -- patch/log/report/spec/etc
  uri                text NOT NULL,
  sha256             text NOT NULL,
  size_bytes         bigint,
  meta_json          jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE(item_id, kind, sha256)
);

CREATE TABLE IF NOT EXISTS logbook.kv (
  namespace          text NOT NULL,
  key               text NOT NULL,
  value_json         jsonb NOT NULL,
  updated_at         timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(namespace, key)
);

-- 用于 Step2 写入失败时的补偿队列（outbox）
CREATE TABLE IF NOT EXISTS logbook.outbox_memory (
  outbox_id          bigserial PRIMARY KEY,
  item_id            bigint,
  target_space       text NOT NULL, -- team:<project> / private:<user> / org:shared
  payload_md         text NOT NULL,
  payload_sha        text NOT NULL,
  status             text NOT NULL DEFAULT 'pending', -- pending/sent/dead
  retry_count        int NOT NULL DEFAULT 0,
  next_attempt_at    timestamptz NOT NULL DEFAULT now(), -- 下次重试时间
  locked_at          timestamptz,                        -- 行锁定时间（可选悲观锁）
  locked_by          text,                               -- 锁定者标识（可选）
  last_error         text,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);

-- 兼容老库：移除 item_id 外键约束
ALTER TABLE logbook.outbox_memory
  DROP CONSTRAINT IF EXISTS outbox_memory_item_id_fkey;

-- outbox_memory 索引：按状态+下次尝试时间查询优化（用于 claim_outbox）
CREATE INDEX IF NOT EXISTS idx_outbox_memory_pending
  ON logbook.outbox_memory(status, next_attempt_at)
  WHERE status = 'pending';

-- outbox_memory 索引：按 status + next_attempt_at + created_at 优化并发消费
CREATE INDEX IF NOT EXISTS idx_outbox_pending_next
  ON logbook.outbox_memory(status, next_attempt_at, created_at);

-- outbox_memory 索引：按 status + next_attempt_at + locked_at 优化 claim_outbox 的 lease 过期检查
-- 用于快速筛选 pending 状态且锁已过期或未锁定的记录
CREATE INDEX IF NOT EXISTS idx_outbox_pending_lease
  ON logbook.outbox_memory(status, next_attempt_at, locked_at)
  WHERE status = 'pending';

-- outbox_memory 唯一索引：用于幂等去重 (dedupe)
-- 相同 (target_space, payload_sha) 且 status='sent' 的记录表示已成功写入，无需重复写入
-- 注意：此索引仅针对 sent 状态，允许 pending/dead 状态的重复（用于重试场景）
DROP INDEX IF EXISTS logbook.idx_outbox_dedup_sent;
CREATE INDEX IF NOT EXISTS idx_outbox_dedup_sent
  ON logbook.outbox_memory(target_space, payload_sha)
  WHERE status = 'sent';

-- ---------- scm ----------
CREATE TABLE IF NOT EXISTS scm.repos (
  repo_id            bigserial PRIMARY KEY,
  repo_type          text NOT NULL CHECK (repo_type IN ('svn','git')),
  url                text NOT NULL,
  project_key        text NOT NULL,
  default_branch     text,
  created_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE(repo_type, url)
);

CREATE TABLE IF NOT EXISTS scm.svn_revisions (
  svn_rev_id         bigserial PRIMARY KEY,           -- 代理主键（向后兼容：新增）
  rev_id             bigint,                          -- 保留旧列名（已有数据兼容），后续用 rev_num
  rev_num            bigint,                          -- SVN revision number（新增，推荐使用）
  repo_id            bigint NOT NULL REFERENCES scm.repos(repo_id),
  author_raw         text NOT NULL,
  ts                 timestamptz,
  message            text,
  is_bulk            boolean NOT NULL DEFAULT false,
  bulk_reason        text,
  meta_json          jsonb NOT NULL DEFAULT '{}'::jsonb,
  source_id          text,                            -- 统一标识符：'svn:<repo_id>:<rev_num>'
  CONSTRAINT chk_rev_num CHECK (rev_num IS NOT NULL OR rev_id IS NOT NULL)
);

-- 复合唯一约束：确保同一仓库内 revision number 唯一
CREATE UNIQUE INDEX IF NOT EXISTS idx_svn_revisions_repo_revnum
  ON scm.svn_revisions(repo_id, COALESCE(rev_num, rev_id));

-- 唯一约束：确保 source_id 全局唯一
CREATE UNIQUE INDEX IF NOT EXISTS idx_svn_revisions_source_id
  ON scm.svn_revisions(repo_id, source_id) WHERE source_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS scm.git_commits (
  git_commit_id      bigserial PRIMARY KEY,           -- 代理主键（向后兼容：新增）
  commit_id          text,                            -- 保留旧列名（已有数据兼容），后续用 commit_sha
  commit_sha         text,                            -- Git commit SHA（新增，推荐使用）
  repo_id            bigint NOT NULL REFERENCES scm.repos(repo_id),
  author_raw         text NOT NULL,
  ts                 timestamptz,
  message            text,
  is_merge           boolean NOT NULL DEFAULT false,
  is_bulk            boolean NOT NULL DEFAULT false,
  bulk_reason        text,
  meta_json          jsonb NOT NULL DEFAULT '{}'::jsonb,
  source_id          text,                            -- 统一标识符：'git:<repo_id>:<commit_sha>'
  CONSTRAINT chk_commit_sha CHECK (commit_sha IS NOT NULL OR commit_id IS NOT NULL)
);

-- 复合唯一约束：确保同一仓库内 commit SHA 唯一
CREATE UNIQUE INDEX IF NOT EXISTS idx_git_commits_repo_sha
  ON scm.git_commits(repo_id, COALESCE(commit_sha, commit_id));

-- 唯一约束：确保 source_id 全局唯一
CREATE UNIQUE INDEX IF NOT EXISTS idx_git_commits_source_id
  ON scm.git_commits(repo_id, source_id) WHERE source_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS scm.patch_blobs (
  blob_id            bigserial PRIMARY KEY,
  source_type        text NOT NULL CHECK (source_type IN ('svn','git')),
  source_id          text NOT NULL, -- 格式: <repo_id>:<rev_or_sha>
  uri                text,          -- 物理存储位置（artifact 路径 / file:// / https:// 等）
  evidence_uri       text,          -- Canonical evidence URI: memory://patch_blobs/<source_type>/<source_id>/<sha256>
  sha256             text NOT NULL,
  size_bytes         bigint,
  format             text NOT NULL DEFAULT 'diff',
  chunking_version   text,
  meta_json          jsonb NOT NULL DEFAULT '{}'::jsonb,  -- 元数据，包含物化状态（evidence_uri 已提升为独立列）
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE(source_type, source_id, sha256)
);

-- patch_blobs: evidence_uri 索引（用于溯源查询）
CREATE INDEX IF NOT EXISTS idx_patch_blobs_evidence_uri
  ON scm.patch_blobs(evidence_uri)
  WHERE evidence_uri IS NOT NULL;

-- patch_blobs: updated_at 自动更新触发器函数
CREATE OR REPLACE FUNCTION scm.update_patch_blobs_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- patch_blobs: updated_at 自动更新触发器
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger 
        WHERE tgname = 'trg_patch_blobs_updated_at'
          AND tgrelid = 'scm.patch_blobs'::regclass
    ) THEN
        CREATE TRIGGER trg_patch_blobs_updated_at
            BEFORE UPDATE ON scm.patch_blobs
            FOR EACH ROW
            EXECUTE FUNCTION scm.update_patch_blobs_updated_at();
    END IF;
END $$;

-- ============ patch_blobs: URI 规范说明 ============
--
-- 本表涉及两种 URI 概念:
--
-- 1. uri 列（物理存储位置）:
--    - 指向实际文件内容的位置
--    - 格式: artifact 相对路径、file:// 或 https:// 等
--    - 示例: "scm/1/git/commits/abc123.diff"
--
-- 2. meta_json.evidence_uri（逻辑引用）:
--    - Canonical evidence URI，用于 analysis.* 和 governance.* 表的 evidence_refs_json
--    - 格式: memory://patch_blobs/<source_type>/<source_id>/<sha256>
--    - 示例: "memory://patch_blobs/git/1:abc123/e3b0c44298fc..."
--
-- evidence_uri 的用途:
--    - 在 analysis.knowledge_candidates.evidence_refs_json 中引用 patch 证据
--    - 在 governance.write_audit.evidence_refs_json 中引用审计证据
--    - 提供统一的逻辑标识符，与物理存储位置解耦
--
-- ============ patch_blobs: meta_json 字段规范 ============
-- {
--   "materialize_status": "pending" | "done" | "failed",  -- 物化状态
--   "materialize_error": "...",                           -- 物化失败时的错误信息
--   "materialized_at": "2024-01-01T00:00:00Z",           -- 物化完成时间
--   "evidence_uri": "memory://patch_blobs/...",          -- canonical evidence URI（逻辑引用）
--   "source_uri": "svn://...",                            -- 原始来源 URI（可选）
--   "degraded": true,                                     -- 是否为降级内容（可选）
--   "degrade_reason": "timeout|content_too_large|...",   -- 降级原因（可选）
--   ...其他自定义元数据
-- }

CREATE TABLE IF NOT EXISTS scm.mrs (
  mr_id              text PRIMARY KEY,
  repo_id            bigint NOT NULL REFERENCES scm.repos(repo_id),
  author_user_id     text REFERENCES identity.users(user_id),
  status             text NOT NULL,
  url                text,
  meta_json          jsonb NOT NULL DEFAULT '{}'::jsonb,
  source_id          text,                            -- 统一标识符：'mr:<repo_id>:<iid>'
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);

-- 唯一约束：确保 source_id 全局唯一
CREATE UNIQUE INDEX IF NOT EXISTS idx_mrs_source_id
  ON scm.mrs(repo_id, source_id) WHERE source_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS scm.review_events (
  id                 bigserial PRIMARY KEY,
  mr_id              text NOT NULL REFERENCES scm.mrs(mr_id) ON DELETE CASCADE,
  source_event_id    text NOT NULL, -- 源系统事件ID，用于幂等去重
  reviewer_user_id   text REFERENCES identity.users(user_id),
  event_type         text NOT NULL, -- comment/approve/request_changes/assign/etc
  payload_json       jsonb NOT NULL DEFAULT '{}'::jsonb,
  ts                 timestamptz NOT NULL DEFAULT now(),
  UNIQUE(mr_id, source_event_id)
);

-- scm.review_events 索引：按 MR+时间查询优化
CREATE INDEX IF NOT EXISTS idx_review_events_mr_ts
  ON scm.review_events(mr_id, ts DESC);

-- scm.review_events 索引：按 source_event_id 快速查找（唯一约束隐式创建）
CREATE INDEX IF NOT EXISTS idx_review_events_mr_source
  ON scm.review_events(mr_id, source_event_id);

-- ---------- analysis ----------
CREATE TABLE IF NOT EXISTS analysis.runs (
  run_id             bigserial PRIMARY KEY,
  source_type        text NOT NULL, -- svn/git/mr/workflow
  source_id          text NOT NULL,
  owner_user_id      text REFERENCES identity.users(user_id),
  pipeline_version   text NOT NULL,
  status             text NOT NULL DEFAULT 'running',
  cost_json          jsonb NOT NULL DEFAULT '{}'::jsonb,
  error              text,
  started_at         timestamptz NOT NULL DEFAULT now(),
  ended_at           timestamptz
);

CREATE TABLE IF NOT EXISTS analysis.knowledge_candidates (
  candidate_id       bigserial PRIMARY KEY,
  run_id             bigint NOT NULL REFERENCES analysis.runs(run_id) ON DELETE CASCADE,
  kind               text NOT NULL, -- FACT/PROCEDURE/PITFALL/DECISION/REVIEW_GUIDE
  title              text NOT NULL,
  content_md         text NOT NULL,
  confidence         text NOT NULL DEFAULT 'mid',
  evidence_refs_json jsonb NOT NULL DEFAULT '{}'::jsonb,  -- 证据引用，使用 canonical evidence URI
  promote_suggested  boolean NOT NULL DEFAULT false,
  created_at         timestamptz NOT NULL DEFAULT now()
);

-- ============ knowledge_candidates: evidence_refs_json 结构规范 ============
-- {
--   "patches": [
--     {
--       "artifact_uri": "memory://patch_blobs/<source_type>/<source_id>/<sha256>",
--       "sha256": "<content_sha256>",
--       "source_id": "<repo_id>:<rev/sha>",
--       "source_type": "<svn|git>",
--       "kind": "patch"
--     },
--     ...
--   ],
--   "attachments": [...],  -- 其他附件引用（可选）
--   ...                    -- 可扩展其他证据类型
-- }
--
-- artifact_uri 使用 canonical evidence URI 格式: memory://patch_blobs/...

-- ---------- governance ----------
CREATE TABLE IF NOT EXISTS governance.settings (
  project_key        text PRIMARY KEY,
  team_write_enabled boolean NOT NULL DEFAULT false,
  policy_json        jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_by         text REFERENCES identity.users(user_id),
  updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS governance.write_audit (
  audit_id           bigserial PRIMARY KEY,
  created_at         timestamptz NOT NULL DEFAULT now(),
  ts                 timestamptz NOT NULL DEFAULT now(),
  actor_user_id      text,
  target_space       text NOT NULL,
  action             text NOT NULL, -- allow/redirect/reject
  reason             text,
  payload_sha        text,
  evidence_refs_json jsonb NOT NULL DEFAULT '{}'::jsonb  -- 证据引用，使用 canonical evidence URI
);

-- 兼容老库：补充 created_at 字段
ALTER TABLE governance.write_audit
  ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();

-- 兼容老库：移除 actor_user_id 外键约束
ALTER TABLE governance.write_audit
  DROP CONSTRAINT IF EXISTS write_audit_actor_user_id_fkey;

-- ============ write_audit: evidence_refs_json 结构规范 ============
-- 与 analysis.knowledge_candidates.evidence_refs_json 结构相同:
-- {
--   "patches": [
--     {
--       "artifact_uri": "memory://patch_blobs/<source_type>/<source_id>/<sha256>",
--       "sha256": "<content_sha256>",
--       "source_id": "<repo_id>:<rev/sha>",
--       "source_type": "<svn|git>",
--       "kind": "patch"
--     },
--     ...
--   ],
--   ...
-- }
--
-- artifact_uri 使用 canonical evidence URI 格式: memory://patch_blobs/...

CREATE TABLE IF NOT EXISTS governance.promotion_queue (
  promo_id           bigserial PRIMARY KEY,
  candidate_id       bigint REFERENCES analysis.knowledge_candidates(candidate_id),
  from_space         text NOT NULL,
  to_space           text NOT NULL,
  requested_by       text REFERENCES identity.users(user_id),
  status             text NOT NULL DEFAULT 'pending', -- pending/approved/rejected
  reviewer_user_id   text REFERENCES identity.users(user_id),
  review_note        text,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);

-- ---------- scm.v_facts：统一事实视图 ----------
-- 将 svn_revisions、git_commits、mrs 统一为一个视图，便于跨来源查询
-- 使用 MATERIALIZED VIEW 以支持索引和提升查询性能
-- 注意：需要定期执行 REFRESH MATERIALIZED VIEW CONCURRENTLY scm.v_facts;

DROP MATERIALIZED VIEW IF EXISTS scm.v_facts;

CREATE MATERIALIZED VIEW scm.v_facts AS
SELECT 
  'svn'::text AS source_type,
  source_id,
  repo_id,
  ts,
  author_raw,
  NULL::text AS author_user_id,
  is_bulk,
  bulk_reason,
  meta_json
FROM scm.svn_revisions
WHERE source_id IS NOT NULL

UNION ALL

SELECT 
  'git'::text AS source_type,
  source_id,
  repo_id,
  ts,
  author_raw,
  NULL::text AS author_user_id,
  is_bulk,
  bulk_reason,
  meta_json
FROM scm.git_commits
WHERE source_id IS NOT NULL

UNION ALL

SELECT 
  'mr'::text AS source_type,
  source_id,
  repo_id,
  created_at AS ts,
  NULL::text AS author_raw,
  author_user_id,
  false AS is_bulk,
  NULL::text AS bulk_reason,
  meta_json
FROM scm.mrs
WHERE source_id IS NOT NULL;

-- v_facts 索引：按 repo_id 查询优化
CREATE UNIQUE INDEX IF NOT EXISTS idx_v_facts_source_id 
  ON scm.v_facts(source_id);

CREATE INDEX IF NOT EXISTS idx_v_facts_repo_id 
  ON scm.v_facts(repo_id);

CREATE INDEX IF NOT EXISTS idx_v_facts_repo_ts 
  ON scm.v_facts(repo_id, ts DESC);

CREATE INDEX IF NOT EXISTS idx_v_facts_ts 
  ON scm.v_facts(ts DESC);

CREATE INDEX IF NOT EXISTS idx_v_facts_source_type 
  ON scm.v_facts(source_type, repo_id);

-- ---------- scm.sync_rate_limits: 分布式 Token Bucket 限流表 ----------
-- 用于控制对外部 API（如 GitLab）的请求速率
-- 每个实例（如 gitlab.example.com）独立维护一个 token bucket

CREATE TABLE IF NOT EXISTS scm.sync_rate_limits (
  instance_key       text PRIMARY KEY,  -- 限流标识（如 GitLab 实例域名）
  tokens             float NOT NULL,     -- 当前可用令牌数
  updated_at         timestamptz NOT NULL DEFAULT now(),  -- 最后更新时间
  rate               float NOT NULL DEFAULT 10.0,  -- 令牌补充速率（tokens/sec）
  burst              int NOT NULL DEFAULT 20,      -- 最大令牌容量（burst size）
  paused_until       timestamptz,                  -- 暂停直到该时间（用于 429 Retry-After）
  meta_json          jsonb NOT NULL DEFAULT '{}'::jsonb  -- 额外元数据
);

-- ============ sync_rate_limits 使用说明 ============
--
-- Token Bucket 算法：
--   1. tokens 表示当前可用令牌数，上限为 burst
--   2. rate 表示每秒补充的令牌数
--   3. 每次 consume 前先 refill（基于时间差补充令牌）
--   4. 如果 tokens >= 1，扣减 1 并返回成功
--   5. 如果 tokens < 1，返回需要等待的时间
--
-- 429 Retry-After 处理：
--   - 收到 429 时，设置 paused_until = now() + retry_after
--   - consume 时检查 paused_until，如果当前时间 < paused_until 则拒绝
--
-- 推荐配置：
--   - GitLab SaaS: rate=10.0, burst=20 (10 RPS, 20 突发)
--   - GitLab 私有实例: rate=30.0, burst=50 (30 RPS, 50 突发)
--
-- meta_json 字段示例：
-- {
--   "total_consumed": 12345,           -- 累计消费令牌数
--   "total_rejected": 100,             -- 累计拒绝次数
--   "last_429_at": "2024-01-01T...",   -- 最后一次 429 时间
--   "consecutive_429_count": 3,        -- 连续 429 次数
--   "config_source": "env"             -- 配置来源
-- }

COMMIT;
