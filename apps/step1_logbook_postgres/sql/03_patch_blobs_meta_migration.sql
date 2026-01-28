-- SCM patch_blobs 表扩展迁移脚本（可重复执行）
-- 用途：为 scm.patch_blobs 添加 meta_json、updated_at 列，并将 uri 改为可空
-- 设计原则：向后兼容，只添加列/修改约束，不删除现有结构
--
-- 执行方式：psql -d <your_db> -f 03_patch_blobs_meta_migration.sql
-- 或在 pgAdmin/DBeaver 中直接执行
--
-- meta_json 字段规范：
-- {
--   "materialize_status": "pending" | "done" | "failed",  -- 物化状态
--   "materialize_error": "...",                           -- 物化失败时的错误信息
--   "materialized_at": "2024-01-01T00:00:00Z",           -- 物化完成时间
--   "source_uri": "svn://...",                            -- 原始来源 URI（可选）
--   ...其他自定义元数据
-- }

BEGIN;

-- ============================================================
-- scm.patch_blobs 迁移
-- 1. 新增 meta_json 列（存储额外元数据，包含物化状态等）
-- 2. 新增 updated_at 列（记录更新时间）
-- 3. 将 uri 改为可空（支持待物化场景）
-- ============================================================

-- 1. 添加 meta_json 列
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_schema = 'scm' 
          AND table_name = 'patch_blobs' 
          AND column_name = 'meta_json'
    ) THEN
        ALTER TABLE scm.patch_blobs 
        ADD COLUMN meta_json jsonb NOT NULL DEFAULT '{}'::jsonb;
        
        RAISE NOTICE 'Added meta_json column to scm.patch_blobs';
    END IF;
END $$;

-- 2. 添加 updated_at 列
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_schema = 'scm' 
          AND table_name = 'patch_blobs' 
          AND column_name = 'updated_at'
    ) THEN
        ALTER TABLE scm.patch_blobs 
        ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();
        
        RAISE NOTICE 'Added updated_at column to scm.patch_blobs';
    END IF;
END $$;

-- 3. 创建 updated_at 更新触发器函数（如果不存在）
CREATE OR REPLACE FUNCTION scm.update_patch_blobs_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 4. 创建触发器（如果不存在）
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
        
        RAISE NOTICE 'Created trigger trg_patch_blobs_updated_at on scm.patch_blobs';
    END IF;
END $$;

-- 5. 将 uri 列改为可空（支持待物化场景）
DO $$
BEGIN
    -- 检查 uri 列是否为 NOT NULL
    IF EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_schema = 'scm' 
          AND table_name = 'patch_blobs' 
          AND column_name = 'uri'
          AND is_nullable = 'NO'
    ) THEN
        ALTER TABLE scm.patch_blobs 
        ALTER COLUMN uri DROP NOT NULL;
        
        RAISE NOTICE 'Changed uri column to nullable in scm.patch_blobs';
    END IF;
END $$;

-- 6. 为已有记录初始化 meta_json 的 materialize_status（如果 uri 有值则为 done）
UPDATE scm.patch_blobs
SET meta_json = meta_json || '{"materialize_status": "done"}'::jsonb
WHERE uri IS NOT NULL 
  AND meta_json->>'materialize_status' IS NULL;

COMMIT;

-- ============================================================
-- 迁移验证（可选，取消注释执行）
-- ============================================================
/*
-- 验证 patch_blobs 结构
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns 
WHERE table_schema = 'scm' AND table_name = 'patch_blobs'
ORDER BY ordinal_position;

-- 验证触发器
SELECT tgname, tgtype, tgenabled
FROM pg_trigger
WHERE tgrelid = 'scm.patch_blobs'::regclass;
*/
