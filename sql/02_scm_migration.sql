-- SCM Schema 迁移脚本（可重复执行）
-- 用途：升级已有数据库的 scm.* 表结构
-- 设计原则：向后兼容，只添加列和约束，不删除现有结构
--
-- 执行方式：psql -d <your_db> -f 02_scm_migration.sql
-- 或在 pgAdmin/DBeaver 中直接执行

BEGIN;

-- ============================================================
-- scm.repos 兼容字段迁移
-- 添加 vcs_type/remote_url 作为 repo_type/url 的别名（弃用字段）
-- ============================================================

-- 1. 添加兼容字段（如果不存在）
ALTER TABLE scm.repos ADD COLUMN IF NOT EXISTS vcs_type text;
ALTER TABLE scm.repos ADD COLUMN IF NOT EXISTS remote_url text;

-- 2. 回填兼容字段（从主字段同步）
-- SAFE: 仅填充 NULL 值，不覆盖已有数据，幂等迁移
UPDATE scm.repos SET vcs_type = repo_type WHERE vcs_type IS NULL AND repo_type IS NOT NULL;
-- SAFE: 仅填充 NULL 值，不覆盖已有数据，幂等迁移
UPDATE scm.repos SET remote_url = url WHERE remote_url IS NULL AND url IS NOT NULL;

-- 3. 创建/替换字段同步触发器
CREATE OR REPLACE FUNCTION scm.sync_repos_compat_fields() RETURNS trigger AS $$
BEGIN
  -- INSERT/UPDATE 时同步字段
  -- 优先级：如果新字段为空但旧字段有值，使用旧字段值；否则同步到旧字段
  
  -- repo_type <-> vcs_type 同步
  IF NEW.repo_type IS NULL AND NEW.vcs_type IS NOT NULL THEN
    NEW.repo_type := NEW.vcs_type;
  END IF;
  NEW.vcs_type := NEW.repo_type;
  
  -- url <-> remote_url 同步
  IF NEW.url IS NULL AND NEW.remote_url IS NOT NULL THEN
    NEW.url := NEW.remote_url;
  END IF;
  NEW.remote_url := NEW.url;
  
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_repos_compat_sync ON scm.repos;
CREATE TRIGGER trg_repos_compat_sync
  BEFORE INSERT OR UPDATE ON scm.repos
  FOR EACH ROW EXECUTE FUNCTION scm.sync_repos_compat_fields();

-- 4. 创建兼容唯一索引（用于 ON CONFLICT 语句）
CREATE UNIQUE INDEX IF NOT EXISTS idx_repos_vcs_type_remote_url
  ON scm.repos(vcs_type, remote_url)
  WHERE vcs_type IS NOT NULL AND remote_url IS NOT NULL;

-- 5. 移除 project_key 的 NOT NULL 约束（如果存在）
-- 兼容旧代码：允许不指定 project_key
DO $$
BEGIN
    -- 检查 project_key 列是否有 NOT NULL 约束
    IF EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_schema = 'scm' 
          AND table_name = 'repos' 
          AND column_name = 'project_key'
          AND is_nullable = 'NO'
    ) THEN
        ALTER TABLE scm.repos ALTER COLUMN project_key DROP NOT NULL;
        RAISE NOTICE 'Dropped NOT NULL constraint from scm.repos.project_key';
    END IF;
END $$;

-- ============================================================
-- scm.svn_revisions 迁移
-- 原结构：rev_id bigint PRIMARY KEY
-- 新结构：svn_rev_id bigserial PRIMARY KEY + rev_num + UNIQUE(repo_id, rev_num)
-- ============================================================

-- 1. 添加代理主键列（如果表使用旧结构）
DO $$
BEGIN
    -- 检查是否存在 svn_rev_id 列
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_schema = 'scm' 
          AND table_name = 'svn_revisions' 
          AND column_name = 'svn_rev_id'
    ) THEN
        -- 旧表：rev_id 是 PRIMARY KEY，需要添加代理主键
        -- 先添加 svn_rev_id 列
        ALTER TABLE scm.svn_revisions 
        ADD COLUMN svn_rev_id bigserial;
        
        RAISE NOTICE 'Added svn_rev_id column to scm.svn_revisions';
    END IF;
END $$;

-- 2. 添加 rev_num 列（新的标准列名）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_schema = 'scm' 
          AND table_name = 'svn_revisions' 
          AND column_name = 'rev_num'
    ) THEN
        ALTER TABLE scm.svn_revisions 
        ADD COLUMN rev_num bigint;
        
        -- 从旧的 rev_id 列迁移数据（如果 rev_id 存在且 rev_num 为空）
        UPDATE scm.svn_revisions 
        SET rev_num = rev_id 
        WHERE rev_num IS NULL AND rev_id IS NOT NULL;
        
        RAISE NOTICE 'Added rev_num column to scm.svn_revisions and migrated data from rev_id';
    END IF;
END $$;

-- 3. 添加 CHECK 约束（确保至少有一个 revision 标识）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.constraint_column_usage 
        WHERE table_schema = 'scm' 
          AND table_name = 'svn_revisions' 
          AND constraint_name = 'chk_rev_num'
    ) THEN
        -- 使用 NOT VALID 避免锁表太久，后续可用 VALIDATE CONSTRAINT 验证
        ALTER TABLE scm.svn_revisions 
        ADD CONSTRAINT chk_rev_num 
        CHECK (rev_num IS NOT NULL OR rev_id IS NOT NULL) NOT VALID;
        
        RAISE NOTICE 'Added chk_rev_num constraint to scm.svn_revisions';
    END IF;
EXCEPTION
    WHEN duplicate_object THEN
        RAISE NOTICE 'Constraint chk_rev_num already exists, skipping';
END $$;

-- 4. 创建复合唯一索引
CREATE UNIQUE INDEX IF NOT EXISTS idx_svn_revisions_repo_revnum
  ON scm.svn_revisions(repo_id, COALESCE(rev_num, rev_id));

-- 5. 添加 source_id 列（统一标识符）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_schema = 'scm' 
          AND table_name = 'svn_revisions' 
          AND column_name = 'source_id'
    ) THEN
        ALTER TABLE scm.svn_revisions 
        ADD COLUMN source_id text;
        
        RAISE NOTICE 'Added source_id column to scm.svn_revisions';
    END IF;
END $$;

-- 6. 回填 source_id（格式：'svn:<repo_id>:<rev_num>'）
UPDATE scm.svn_revisions 
SET source_id = 'svn:' || repo_id || ':' || COALESCE(rev_num, rev_id)::text
WHERE source_id IS NULL;

-- 7. 创建 source_id 唯一索引
CREATE UNIQUE INDEX IF NOT EXISTS idx_svn_revisions_source_id
  ON scm.svn_revisions(repo_id, source_id) WHERE source_id IS NOT NULL;


-- ============================================================
-- scm.git_commits 迁移
-- 原结构：commit_id text PRIMARY KEY
-- 新结构：git_commit_id bigserial PRIMARY KEY + commit_sha + UNIQUE(repo_id, commit_sha)
-- ============================================================

-- 1. 添加代理主键列（如果表使用旧结构）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_schema = 'scm' 
          AND table_name = 'git_commits' 
          AND column_name = 'git_commit_id'
    ) THEN
        ALTER TABLE scm.git_commits 
        ADD COLUMN git_commit_id bigserial;
        
        RAISE NOTICE 'Added git_commit_id column to scm.git_commits';
    END IF;
END $$;

-- 2. 添加 commit_sha 列（新的标准列名）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_schema = 'scm' 
          AND table_name = 'git_commits' 
          AND column_name = 'commit_sha'
    ) THEN
        ALTER TABLE scm.git_commits 
        ADD COLUMN commit_sha text;
        
        -- 从旧的 commit_id 列迁移数据
        UPDATE scm.git_commits 
        SET commit_sha = commit_id 
        WHERE commit_sha IS NULL AND commit_id IS NOT NULL;
        
        RAISE NOTICE 'Added commit_sha column to scm.git_commits and migrated data from commit_id';
    END IF;
END $$;

-- 3. 添加 CHECK 约束（确保至少有一个 commit 标识）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.constraint_column_usage 
        WHERE table_schema = 'scm' 
          AND table_name = 'git_commits' 
          AND constraint_name = 'chk_commit_sha'
    ) THEN
        ALTER TABLE scm.git_commits 
        ADD CONSTRAINT chk_commit_sha 
        CHECK (commit_sha IS NOT NULL OR commit_id IS NOT NULL) NOT VALID;
        
        RAISE NOTICE 'Added chk_commit_sha constraint to scm.git_commits';
    END IF;
EXCEPTION
    WHEN duplicate_object THEN
        RAISE NOTICE 'Constraint chk_commit_sha already exists, skipping';
END $$;

-- 4. 创建复合唯一索引
CREATE UNIQUE INDEX IF NOT EXISTS idx_git_commits_repo_sha
  ON scm.git_commits(repo_id, COALESCE(commit_sha, commit_id));

-- 5. 添加 source_id 列（统一标识符）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_schema = 'scm' 
          AND table_name = 'git_commits' 
          AND column_name = 'source_id'
    ) THEN
        ALTER TABLE scm.git_commits 
        ADD COLUMN source_id text;
        
        RAISE NOTICE 'Added source_id column to scm.git_commits';
    END IF;
END $$;

-- 6. 回填 source_id（格式：'git:<repo_id>:<commit_sha>'）
UPDATE scm.git_commits 
SET source_id = 'git:' || repo_id || ':' || COALESCE(commit_sha, commit_id)
WHERE source_id IS NULL;

-- 7. 创建 source_id 唯一索引
CREATE UNIQUE INDEX IF NOT EXISTS idx_git_commits_source_id
  ON scm.git_commits(repo_id, source_id) WHERE source_id IS NOT NULL;


-- ============================================================
-- scm.mrs 迁移
-- 新增 source_id 列及 UNIQUE 约束
-- ============================================================

-- 1. 添加 source_id 列（统一标识符）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_schema = 'scm' 
          AND table_name = 'mrs' 
          AND column_name = 'source_id'
    ) THEN
        ALTER TABLE scm.mrs 
        ADD COLUMN source_id text;
        
        RAISE NOTICE 'Added source_id column to scm.mrs';
    END IF;
END $$;

-- 2. 回填 source_id（格式：'mr:<repo_id>:<iid>'）
-- 优先从 meta_json->>'iid' 获取，否则从 mr_id 解析（假设格式为 <project>!<iid> 或 <project>/<iid>）
UPDATE scm.mrs 
SET source_id = 'mr:' || repo_id || ':' || COALESCE(
    meta_json->>'iid',
    -- 尝试从 mr_id 解析 iid（支持 ! 或 / 分隔符）
    CASE 
        WHEN mr_id LIKE '%!%' THEN split_part(mr_id, '!', 2)
        WHEN mr_id LIKE '%/%' THEN split_part(mr_id, '/', 2)
        ELSE mr_id
    END
)
WHERE source_id IS NULL;

-- 3. 创建 source_id 唯一索引
CREATE UNIQUE INDEX IF NOT EXISTS idx_mrs_source_id
  ON scm.mrs(repo_id, source_id) WHERE source_id IS NOT NULL;


-- ============================================================
-- 额外索引优化（可选但推荐）
-- ============================================================

-- scm.svn_revisions 按时间查询优化
CREATE INDEX IF NOT EXISTS idx_svn_revisions_repo_ts 
  ON scm.svn_revisions(repo_id, ts DESC);

-- scm.git_commits 按时间查询优化
CREATE INDEX IF NOT EXISTS idx_git_commits_repo_ts 
  ON scm.git_commits(repo_id, ts DESC);

-- scm.patch_blobs 按来源查询优化
CREATE INDEX IF NOT EXISTS idx_patch_blobs_source 
  ON scm.patch_blobs(source_type, source_id);

-- scm.review_events 按 MR 和时间查询优化
CREATE INDEX IF NOT EXISTS idx_review_events_mr_ts 
  ON scm.review_events(mr_id, ts DESC);


-- ============================================================
-- scm.review_events 迁移
-- 新增 source_event_id 列及 UNIQUE 约束，用于幂等去重
-- ============================================================

-- 1. 添加 source_event_id 列（源系统事件ID）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_schema = 'scm' 
          AND table_name = 'review_events' 
          AND column_name = 'source_event_id'
    ) THEN
        -- 先添加允许 NULL 的列
        ALTER TABLE scm.review_events 
        ADD COLUMN source_event_id text;
        
        -- 为已有数据生成默认值（使用 id 作为临时值）
        UPDATE scm.review_events 
        SET source_event_id = 'legacy_' || id::text 
        WHERE source_event_id IS NULL;
        
        -- 设置为 NOT NULL
        ALTER TABLE scm.review_events 
        ALTER COLUMN source_event_id SET NOT NULL;
        
        RAISE NOTICE 'Added source_event_id column to scm.review_events';
    END IF;
END $$;

-- 2. 添加 UNIQUE 约束（mr_id, source_event_id）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint 
        WHERE conname = 'review_events_mr_id_source_event_id_key'
          AND conrelid = 'scm.review_events'::regclass
    ) THEN
        ALTER TABLE scm.review_events 
        ADD CONSTRAINT review_events_mr_id_source_event_id_key 
        UNIQUE (mr_id, source_event_id);
        
        RAISE NOTICE 'Added UNIQUE(mr_id, source_event_id) constraint to scm.review_events';
    END IF;
EXCEPTION
    WHEN duplicate_object THEN
        RAISE NOTICE 'Constraint review_events_mr_id_source_event_id_key already exists, skipping';
END $$;

-- 3. 创建 (mr_id, source_event_id) 索引（可能被唯一约束隐式创建，这里确保存在）
CREATE INDEX IF NOT EXISTS idx_review_events_mr_source 
  ON scm.review_events(mr_id, source_event_id);


-- ============================================================
-- scm.v_facts 统一事实视图（Materialized View）
-- 将 svn_revisions、git_commits、mrs 统一为一个视图
-- ============================================================

-- 删除旧视图（如存在），重新创建以确保结构更新
-- SAFE: 幂等重建视图，无数据丢失风险（视图不存储数据）
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

-- v_facts 索引：source_id 唯一索引（支持 REFRESH CONCURRENTLY）
CREATE UNIQUE INDEX IF NOT EXISTS idx_v_facts_source_id 
  ON scm.v_facts(source_id);

-- v_facts 索引：按 repo_id 查询
CREATE INDEX IF NOT EXISTS idx_v_facts_repo_id 
  ON scm.v_facts(repo_id);

-- v_facts 索引：按 repo_id + ts 查询（常用场景）
CREATE INDEX IF NOT EXISTS idx_v_facts_repo_ts 
  ON scm.v_facts(repo_id, ts DESC);

-- v_facts 索引：按 ts 全局时间线查询
CREATE INDEX IF NOT EXISTS idx_v_facts_ts 
  ON scm.v_facts(ts DESC);

-- v_facts 索引：按 source_type + repo_id 查询
CREATE INDEX IF NOT EXISTS idx_v_facts_source_type 
  ON scm.v_facts(source_type, repo_id);


COMMIT;

-- ============================================================
-- 迁移验证（可选，取消注释执行）
-- ============================================================
/*
-- 验证 svn_revisions 结构
SELECT column_name, data_type, is_nullable 
FROM information_schema.columns 
WHERE table_schema = 'scm' AND table_name = 'svn_revisions'
ORDER BY ordinal_position;

-- 验证 git_commits 结构
SELECT column_name, data_type, is_nullable 
FROM information_schema.columns 
WHERE table_schema = 'scm' AND table_name = 'git_commits'
ORDER BY ordinal_position;

-- 验证索引
SELECT indexname, indexdef 
FROM pg_indexes 
WHERE schemaname = 'scm';
*/
