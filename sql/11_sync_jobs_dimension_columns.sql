-- SCM Sync Jobs 维度列迁移（可重复执行）
-- 用途：为 sync_jobs 表添加 gitlab_instance 和 tenant_id 列，优化 budget 查询
-- 设计原则：幂等执行，仅添加不删除
--
-- 执行方式：psql -d <your_db> -f 11_sync_jobs_dimension_columns.sql
-- 或在 pgAdmin/DBeaver 中直接执行

BEGIN;

-- ============================================================
-- 添加维度列（如果不存在）
-- ============================================================

-- 添加 gitlab_instance 列
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'scm'
          AND table_name = 'sync_jobs'
          AND column_name = 'gitlab_instance'
    ) THEN
        ALTER TABLE scm.sync_jobs ADD COLUMN gitlab_instance text;
        COMMENT ON COLUMN scm.sync_jobs.gitlab_instance IS 'GitLab 实例主机名（冗余存储，用于 budget 查询优化）';
        RAISE NOTICE 'Added column: gitlab_instance';
    END IF;
END $$;

-- 添加 tenant_id 列
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'scm'
          AND table_name = 'sync_jobs'
          AND column_name = 'tenant_id'
    ) THEN
        ALTER TABLE scm.sync_jobs ADD COLUMN tenant_id text;
        COMMENT ON COLUMN scm.sync_jobs.tenant_id IS '租户 ID（冗余存储，用于 budget 查询优化）';
        RAISE NOTICE 'Added column: tenant_id';
    END IF;
END $$;

-- ============================================================
-- 添加维度索引（如果不存在）
-- ============================================================

-- 按 gitlab_instance 聚合活跃任务索引
CREATE INDEX IF NOT EXISTS idx_sync_jobs_gitlab_instance_active
    ON scm.sync_jobs(gitlab_instance)
    WHERE status IN ('pending', 'running') AND gitlab_instance IS NOT NULL;

-- 按 tenant_id 聚合活跃任务索引
CREATE INDEX IF NOT EXISTS idx_sync_jobs_tenant_id_active
    ON scm.sync_jobs(tenant_id)
    WHERE status IN ('pending', 'running') AND tenant_id IS NOT NULL;

-- ============================================================
-- 回填现有数据（从 repos 表获取并更新）
-- 注意：仅更新活跃任务（pending/running），历史任务不回填
-- ============================================================

-- 回填 gitlab_instance（从 repos.url 解析 host）
-- 注意：如果 repos 记录不存在或 url 格式不匹配，gitlab_instance 保持 NULL
-- 这是预期行为，因为某些任务（如 SVN）不需要 gitlab_instance
UPDATE scm.sync_jobs j
SET gitlab_instance = (
    SELECT 
        CASE 
            WHEN r.repo_type = 'git' AND r.url IS NOT NULL AND r.url LIKE '%://%'
            THEN LOWER(REGEXP_REPLACE(r.url, '^[^:]+://([^/:]+).*$', '\1'))
            ELSE NULL
        END
    FROM scm.repos r
    WHERE r.repo_id = j.repo_id
)
WHERE j.status IN ('pending', 'running')
  AND j.gitlab_instance IS NULL
  -- 边界保护：仅当 repos 记录存在时回填
  AND EXISTS (SELECT 1 FROM scm.repos r WHERE r.repo_id = j.repo_id);

-- 回填 tenant_id（从 repos.project_key 解析）
-- 注意：如果 project_key 格式不符合 "group/project" 模式，从 payload_json 尝试读取
UPDATE scm.sync_jobs j
SET tenant_id = COALESCE(
    -- 优先从 repos.project_key 解析
    (
        SELECT 
            CASE 
                WHEN r.project_key IS NOT NULL AND r.project_key LIKE '%/%'
                THEN SPLIT_PART(r.project_key, '/', 1)
                ELSE NULL
            END
        FROM scm.repos r
        WHERE r.repo_id = j.repo_id
    ),
    -- 回退到 payload_json 中的 tenant_id
    j.payload_json ->> 'tenant_id'
)
WHERE j.status IN ('pending', 'running')
  AND j.tenant_id IS NULL;

-- ============================================================
-- 数据完整性检查（警告但不阻塞）
-- ============================================================

-- 检查并报告孤立的活跃任务（repo_id 不存在于 repos 表）
DO $$
DECLARE
    orphan_count int;
BEGIN
    SELECT COUNT(*) INTO orphan_count
    FROM scm.sync_jobs j
    WHERE j.status IN ('pending', 'running')
      AND NOT EXISTS (SELECT 1 FROM scm.repos r WHERE r.repo_id = j.repo_id);
    
    IF orphan_count > 0 THEN
        RAISE WARNING 'Found % orphaned active sync_jobs (repo_id not in repos table). These may need manual review.', orphan_count;
    END IF;
END $$;

COMMIT;

-- ============================================================
-- 验证（可选，取消注释执行）
-- ============================================================
/*
-- 验证列是否存在
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'scm'
  AND table_name = 'sync_jobs'
  AND column_name IN ('gitlab_instance', 'tenant_id');

-- 验证索引
SELECT indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'scm'
  AND tablename = 'sync_jobs'
  AND indexname LIKE 'idx_sync_jobs_%';
*/
