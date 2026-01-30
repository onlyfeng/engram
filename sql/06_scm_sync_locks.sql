-- SCM Sync Locks 表（可重复执行）
-- 用途：分布式锁表，确保同一 (repo_id, job_type) 只有一个 worker 在执行同步任务
-- 设计原则：幂等执行，只添加不删除
--
-- 执行方式：psql -d <your_db> -f 07_scm_sync_locks.sql
-- 或在 pgAdmin/DBeaver 中直接执行

BEGIN;

-- ============================================================
-- scm.sync_locks 表
-- 分布式锁表，使用 lease 机制防止死锁
-- ============================================================

CREATE TABLE IF NOT EXISTS scm.sync_locks (
    -- 主键
    lock_id         bigserial PRIMARY KEY,
    
    -- 唯一键：(repo_id, job_type)
    -- job_type 使用 physical_job_type（与具体 SCM 实现绑定）
    -- 确保同一仓库的同一任务类型只有一个 worker 在执行
    repo_id         integer NOT NULL REFERENCES scm.repos(repo_id) ON DELETE CASCADE,
    job_type        text NOT NULL,  -- physical_job_type: 'gitlab_commits' | 'gitlab_mrs' | 'gitlab_reviews' | 'svn'
                                    -- gitlab_commits: GitLab 提交记录同步
                                    -- gitlab_mrs: GitLab Merge Requests 同步
                                    -- gitlab_reviews: GitLab Review 事件同步
                                    -- svn: SVN 提交记录同步
    
    -- 锁持有者信息
    locked_by       text,           -- worker 标识符，NULL 表示未锁定
    locked_at       timestamptz,    -- 锁定时间，NULL 表示未锁定
    
    -- 租约配置
    lease_seconds   integer NOT NULL DEFAULT 60,  -- 租约时长（秒），超过后可被其他 worker 抢占
    
    -- 更新时间
    updated_at      timestamptz NOT NULL DEFAULT now(),
    
    -- 创建时间
    created_at      timestamptz NOT NULL DEFAULT now(),
    
    -- 唯一约束：每个 (repo_id, job_type) 只能有一条记录
    CONSTRAINT uq_sync_locks_repo_job UNIQUE (repo_id, job_type)
);

-- 添加表注释
COMMENT ON TABLE scm.sync_locks IS '同步任务分布式锁表，确保同一仓库的同一任务类型只有一个 worker 在执行';
COMMENT ON COLUMN scm.sync_locks.lock_id IS '锁记录唯一标识';
COMMENT ON COLUMN scm.sync_locks.repo_id IS '关联的仓库 ID';
COMMENT ON COLUMN scm.sync_locks.job_type IS 'physical_job_type（物理任务类型）: gitlab_commits（GitLab提交）, gitlab_mrs（GitLab MR）, gitlab_reviews（GitLab Review）, svn（SVN提交）';
COMMENT ON COLUMN scm.sync_locks.locked_by IS '当前持有锁的 worker 标识符，NULL 表示未锁定';
COMMENT ON COLUMN scm.sync_locks.locked_at IS '锁定时间戳，NULL 表示未锁定';
COMMENT ON COLUMN scm.sync_locks.lease_seconds IS '租约时长（秒），锁超过此时间可被其他 worker 抢占';
COMMENT ON COLUMN scm.sync_locks.updated_at IS '最后更新时间（锁状态变更时更新）';
COMMENT ON COLUMN scm.sync_locks.created_at IS '记录创建时间';

-- ============================================================
-- 索引
-- ============================================================

-- 按仓库查询锁状态
CREATE INDEX IF NOT EXISTS idx_sync_locks_repo
    ON scm.sync_locks(repo_id);

-- 按 worker 查询其持有的锁（用于 worker 故障恢复）
CREATE INDEX IF NOT EXISTS idx_sync_locks_locked_by
    ON scm.sync_locks(locked_by)
    WHERE locked_by IS NOT NULL;

-- 查询已锁定的记录（用于监控和清理过期锁）
CREATE INDEX IF NOT EXISTS idx_sync_locks_locked_at
    ON scm.sync_locks(locked_at)
    WHERE locked_at IS NOT NULL;

-- 按 job_type 查询（用于批量操作）
CREATE INDEX IF NOT EXISTS idx_sync_locks_job_type
    ON scm.sync_locks(job_type);

-- ============================================================
-- governance.security_events 表（安全事件审计）
-- ============================================================

CREATE TABLE IF NOT EXISTS governance.security_events (
    event_id        bigserial PRIMARY KEY,
    event_ts        timestamptz NOT NULL DEFAULT now(),
    action          text NOT NULL,
    object_type     text,
    object_id       text,
    actor_user_id   text,
    details         jsonb DEFAULT '{}'
);

-- 索引：按时间、动作、对象类型查询
CREATE INDEX IF NOT EXISTS idx_security_events_ts
    ON governance.security_events(event_ts DESC);

CREATE INDEX IF NOT EXISTS idx_security_events_action
    ON governance.security_events(action, event_ts DESC);

CREATE INDEX IF NOT EXISTS idx_security_events_object_type
    ON governance.security_events(object_type, event_ts DESC)
    WHERE object_type IS NOT NULL;

COMMIT;

-- ============================================================
-- 验证（可选，取消注释执行）
-- ============================================================
/*
-- 验证表结构
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns 
WHERE table_schema = 'scm' AND table_name = 'sync_locks'
ORDER BY ordinal_position;

-- 验证索引
SELECT indexname, indexdef 
FROM pg_indexes 
WHERE schemaname = 'scm' AND tablename = 'sync_locks';

-- 验证约束
SELECT conname, contype, pg_get_constraintdef(oid) 
FROM pg_constraint 
WHERE conrelid = 'scm.sync_locks'::regclass;
*/
