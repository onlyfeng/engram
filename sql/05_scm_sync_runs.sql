-- SCM Sync Runs 表（可重复执行）
-- 用途：记录每次同步运行的元数据，用于观测性、审计和故障排查
-- 设计原则：幂等执行，只添加不删除
--
-- 执行方式：psql -d <your_db> -f 06_scm_sync_runs.sql
-- 或在 pgAdmin/DBeaver 中直接执行

BEGIN;

-- ============================================================
-- scm.sync_runs 表
-- 记录每次同步运行的开始/结束状态、游标变化、计数和错误信息
-- ============================================================

CREATE TABLE IF NOT EXISTS scm.sync_runs (
    -- 主键
    run_id          uuid PRIMARY KEY,
    
    -- 关联信息
    -- job_type 使用 physical_job_type（与具体 SCM 实现绑定）
    -- 记录实际执行的同步任务类型，用于观测性和审计
    repo_id         integer NOT NULL REFERENCES scm.repos(repo_id) ON DELETE CASCADE,
    job_type        text NOT NULL,  -- physical_job_type: 'gitlab_commits' | 'gitlab_mrs' | 'gitlab_reviews' | 'svn'
                                    -- gitlab_commits: GitLab 提交记录同步
                                    -- gitlab_mrs: GitLab Merge Requests 同步
                                    -- gitlab_reviews: GitLab Review 事件同步
                                    -- svn: SVN 提交记录同步
    mode            text NOT NULL DEFAULT 'incremental',  -- 'incremental' | 'backfill' | 'full'
    
    -- 时间戳
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    
    -- 游标信息（同步前后的水位线）
    cursor_before   jsonb,  -- 同步前的游标快照 {last_commit_sha, last_commit_ts, ...}
    cursor_after    jsonb,  -- 同步后的游标快照
    
    -- 计数统计
    counts          jsonb DEFAULT '{}',  -- {synced_count, diff_count, bulk_count, degraded_count, ...}
    
    -- 错误信息
    error_summary_json   jsonb,  -- 错误摘要 {error_type, message, ...}
    
    -- 降级信息
    degradation_json     jsonb,  -- 降级详情 {degraded_reasons: {timeout: N, ...}, ...}
    
    -- 关联的 logbook item
    logbook_item_id      bigint,  -- 关联的 logbook.items.id
    
    -- 运行状态
    status          text NOT NULL DEFAULT 'running',  -- 'running' | 'completed' | 'failed' | 'no_data'
    
    -- 元数据
    meta_json       jsonb DEFAULT '{}',  -- 额外元数据（配置快照、版本信息等）
    
    -- 索引优化列（从 counts 中冗余以支持高效查询）
    synced_count    integer GENERATED ALWAYS AS ((counts->>'synced_count')::integer) STORED,
    
    -- 创建时间（与 started_at 相同，用于分区/归档策略）
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- 添加表注释
COMMENT ON TABLE scm.sync_runs IS '同步运行记录表，记录每次同步的元数据用于观测性和审计';
COMMENT ON COLUMN scm.sync_runs.run_id IS '运行唯一标识（UUID）';
COMMENT ON COLUMN scm.sync_runs.repo_id IS '关联的仓库 ID';
COMMENT ON COLUMN scm.sync_runs.job_type IS 'physical_job_type（物理任务类型）: gitlab_commits（GitLab提交）, gitlab_mrs（GitLab MR）, gitlab_reviews（GitLab Review）, svn（SVN提交）';
COMMENT ON COLUMN scm.sync_runs.mode IS '同步模式: incremental（增量）, backfill（回填）, full（全量）';
COMMENT ON COLUMN scm.sync_runs.started_at IS '同步开始时间';
COMMENT ON COLUMN scm.sync_runs.finished_at IS '同步结束时间（运行中为 NULL）';
COMMENT ON COLUMN scm.sync_runs.cursor_before IS '同步前的游标快照（JSON 格式）';
COMMENT ON COLUMN scm.sync_runs.cursor_after IS '同步后的游标快照（JSON 格式）';
COMMENT ON COLUMN scm.sync_runs.counts IS '同步计数统计（synced_count, diff_count 等）';
COMMENT ON COLUMN scm.sync_runs.error_summary_json IS '错误摘要信息';
COMMENT ON COLUMN scm.sync_runs.degradation_json IS '降级详情（降级原因分布等）';
COMMENT ON COLUMN scm.sync_runs.logbook_item_id IS '关联的 logbook item ID';
COMMENT ON COLUMN scm.sync_runs.status IS '运行状态: running, completed, failed, no_data';
COMMENT ON COLUMN scm.sync_runs.meta_json IS '额外元数据（配置快照、版本信息等）';
COMMENT ON COLUMN scm.sync_runs.synced_count IS '同步记录数（从 counts 提取，用于索引优化）';

-- ============================================================
-- 索引
-- ============================================================

-- 按仓库和任务类型查询（常用于查看特定仓库的同步历史）
CREATE INDEX IF NOT EXISTS idx_sync_runs_repo_job
    ON scm.sync_runs(repo_id, job_type, started_at DESC);

-- 按状态查询（用于监控运行中的任务或失败任务）
CREATE INDEX IF NOT EXISTS idx_sync_runs_status
    ON scm.sync_runs(status, started_at DESC);

-- 按时间范围查询（用于历史分析和清理）
CREATE INDEX IF NOT EXISTS idx_sync_runs_started_at
    ON scm.sync_runs(started_at DESC);

-- 按仓库查询最新运行（用于获取当前同步状态）
CREATE INDEX IF NOT EXISTS idx_sync_runs_repo_latest
    ON scm.sync_runs(repo_id, started_at DESC);

-- 失败任务快速查询（部分索引）
CREATE INDEX IF NOT EXISTS idx_sync_runs_failed
    ON scm.sync_runs(repo_id, started_at DESC)
    WHERE status = 'failed';

-- 运行中任务查询（部分索引，用于检测卡住的任务）
CREATE INDEX IF NOT EXISTS idx_sync_runs_running
    ON scm.sync_runs(repo_id, started_at)
    WHERE status = 'running';

-- logbook item 关联查询
CREATE INDEX IF NOT EXISTS idx_sync_runs_logbook_item
    ON scm.sync_runs(logbook_item_id)
    WHERE logbook_item_id IS NOT NULL;

COMMIT;

-- ============================================================
-- 验证（可选，取消注释执行）
-- ============================================================
/*
-- 验证表结构
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns 
WHERE table_schema = 'scm' AND table_name = 'sync_runs'
ORDER BY ordinal_position;

-- 验证索引
SELECT indexname, indexdef 
FROM pg_indexes 
WHERE schemaname = 'scm' AND tablename = 'sync_runs';
*/
