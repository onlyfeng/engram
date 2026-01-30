-- SCM Sync Jobs 表（可重复执行）
-- 用途：同步任务队列表，支持任务调度、重试、优先级控制
-- 设计原则：幂等执行，只添加不删除
--
-- 执行方式：psql -d <your_db> -f 08_scm_sync_jobs.sql
-- 或在 pgAdmin/DBeaver 中直接执行

BEGIN;

-- ============================================================
-- scm.sync_jobs 表
-- 同步任务队列表，使用 claim/ack/fail 模式实现可靠队列
-- ============================================================

CREATE TABLE IF NOT EXISTS scm.sync_jobs (
    -- 主键
    job_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    
    -- 关联信息
    -- job_type 使用 physical_job_type（与具体 SCM 实现绑定）
    -- scheduler 入队时使用 physical_job_type，确保同一语义任务在队列里有唯一键
    repo_id         integer NOT NULL REFERENCES scm.repos(repo_id) ON DELETE CASCADE,
    job_type        text NOT NULL,  -- physical_job_type: 'gitlab_commits' | 'gitlab_mrs' | 'gitlab_reviews' | 'svn'
                                    -- gitlab_commits: GitLab 提交记录同步
                                    -- gitlab_mrs: GitLab Merge Requests 同步
                                    -- gitlab_reviews: GitLab Review 事件同步
                                    -- svn: SVN 提交记录同步
                                    -- 注意：logical_job_type (commits/mrs/reviews) 在 scheduler 层转换为 physical
    mode            text NOT NULL DEFAULT 'incremental',  -- 'incremental' | 'backfill'
    
    -- 优先级（数值越小优先级越高，默认 100）
    priority        integer NOT NULL DEFAULT 100,
    
    -- 任务参数（JSON 格式，如回填窗口参数）
    -- 例: {"since": "2024-01-01", "until": "2024-06-01"} 或 {"page": 1, "per_page": 100}
    payload_json    jsonb DEFAULT '{}',
    
    -- 任务状态
    status          text NOT NULL DEFAULT 'pending',
    -- 状态值: 'pending' | 'running' | 'completed' | 'failed' | 'dead'
    -- pending: 等待执行
    -- running: 正在执行（被 worker 锁定）
    -- completed: 执行成功
    -- failed: 执行失败（可重试）
    -- dead: 达到最大重试次数，不再重试
    
    -- 重试控制
    attempts        integer NOT NULL DEFAULT 0,       -- 已尝试次数
    max_attempts    integer NOT NULL DEFAULT 3,       -- 最大尝试次数
    
    -- 延迟执行（任务在此时间之前不会被 claim）
    not_before      timestamptz NOT NULL DEFAULT now(),
    
    -- 锁持有者信息
    locked_by       text,                             -- 当前执行的 worker 标识符
    locked_at       timestamptz,                      -- 锁定时间
    
    -- 租约配置（秒）
    lease_seconds   integer NOT NULL DEFAULT 300,     -- 默认 5 分钟
    
    -- 错误信息
    last_error      text,                             -- 最后一次错误信息
    
    -- 关联的 sync_run
    last_run_id     uuid,                             -- 最后一次运行的 run_id
    
    -- 时间戳
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- 添加表注释
COMMENT ON TABLE scm.sync_jobs IS '同步任务队列表，支持 claim/ack/fail 模式的可靠任务队列';
COMMENT ON COLUMN scm.sync_jobs.job_id IS '任务唯一标识（UUID）';
COMMENT ON COLUMN scm.sync_jobs.repo_id IS '关联的仓库 ID';
COMMENT ON COLUMN scm.sync_jobs.job_type IS 'physical_job_type（物理任务类型）: gitlab_commits（GitLab提交）, gitlab_mrs（GitLab MR）, gitlab_reviews（GitLab Review）, svn（SVN提交）。scheduler 入队时将 logical_job_type 转换为 physical_job_type';
COMMENT ON COLUMN scm.sync_jobs.mode IS '同步模式: incremental（增量）, backfill（回填）';
COMMENT ON COLUMN scm.sync_jobs.priority IS '优先级（数值越小优先级越高），默认 100';
COMMENT ON COLUMN scm.sync_jobs.payload_json IS '任务参数（JSON 格式），如回填时间窗口';
COMMENT ON COLUMN scm.sync_jobs.status IS '任务状态: pending, running, completed, failed, dead';
COMMENT ON COLUMN scm.sync_jobs.attempts IS '已尝试执行次数';
COMMENT ON COLUMN scm.sync_jobs.max_attempts IS '最大尝试次数，超过后标记为 dead';
COMMENT ON COLUMN scm.sync_jobs.not_before IS '延迟执行时间，任务在此时间前不会被 claim';
COMMENT ON COLUMN scm.sync_jobs.locked_by IS '当前持有锁的 worker 标识符';
COMMENT ON COLUMN scm.sync_jobs.locked_at IS '锁定时间戳';
COMMENT ON COLUMN scm.sync_jobs.lease_seconds IS '租约时长（秒），超过后锁可被其他 worker 抢占';
COMMENT ON COLUMN scm.sync_jobs.last_error IS '最后一次执行的错误信息';
COMMENT ON COLUMN scm.sync_jobs.last_run_id IS '关联的最后一次 sync_run ID';

-- ============================================================
-- 索引
-- ============================================================

-- claim 查询索引：按优先级和创建时间获取待执行任务
-- 条件：status = 'pending' AND not_before <= now()
-- 或 status = 'running' 且锁已过期
CREATE INDEX IF NOT EXISTS idx_sync_jobs_claim
    ON scm.sync_jobs(priority ASC, created_at ASC)
    WHERE status IN ('pending', 'failed');

-- 按状态查询（用于监控和管理）
CREATE INDEX IF NOT EXISTS idx_sync_jobs_status
    ON scm.sync_jobs(status, created_at DESC);

-- 按仓库查询（查看仓库的任务队列）
CREATE INDEX IF NOT EXISTS idx_sync_jobs_repo
    ON scm.sync_jobs(repo_id, status, created_at DESC);

-- 按仓库和任务类型查询（防止重复任务）
CREATE INDEX IF NOT EXISTS idx_sync_jobs_repo_job_type
    ON scm.sync_jobs(repo_id, job_type, status)
    WHERE status IN ('pending', 'running');

-- 防止重复活跃任务（同一 repo + job_type + mode 只能有一个 pending/running）
CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_jobs_unique_active
    ON scm.sync_jobs(repo_id, job_type, mode)
    WHERE status IN ('pending', 'running');

-- 按 worker 查询其持有的任务
CREATE INDEX IF NOT EXISTS idx_sync_jobs_locked_by
    ON scm.sync_jobs(locked_by)
    WHERE locked_by IS NOT NULL;

-- 运行中任务的锁过期检查
CREATE INDEX IF NOT EXISTS idx_sync_jobs_running_lease
    ON scm.sync_jobs(locked_at, lease_seconds)
    WHERE status = 'running';

-- 按 repo + job_type 查询最新任务
CREATE INDEX IF NOT EXISTS idx_sync_jobs_repo_job_latest
    ON scm.sync_jobs(repo_id, job_type, created_at DESC);

COMMIT;

-- ============================================================
-- 验证（可选，取消注释执行）
-- ============================================================
/*
-- 验证表结构
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'scm' AND table_name = 'sync_jobs'
ORDER BY ordinal_position;

-- 验证索引
SELECT indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'scm' AND tablename = 'sync_jobs';
*/
