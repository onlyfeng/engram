-- SCM patch_blobs 表扩展迁移脚本：添加 evidence_uri 列
-- 用途：为 scm.patch_blobs 添加独立的 evidence_uri 列，并从 meta_json 回填
-- 设计原则：向后兼容，支持 COALESCE(evidence_uri, meta_json->>'evidence_uri') 查询模式
--
-- 执行方式：psql -d <your_db> -f 10_evidence_uri_column.sql
-- 或在 pgAdmin/DBeaver 中直接执行
--
-- evidence_uri 格式（Canonical Evidence URI）：
--   memory://patch_blobs/<source_type>/<source_id>/<sha256>
-- 示例：
--   memory://patch_blobs/git/1:abc123/e3b0c44298fc...
--
-- 迁移策略：
--   1. 添加 evidence_uri 列（允许 NULL，便于增量迁移）
--   2. 从 meta_json->>'evidence_uri' 回填现有数据
--   3. 为空记录生成 canonical URI
--   4. 创建索引优化查询

BEGIN;

-- ============================================================
-- 1. 添加 evidence_uri 列
-- ============================================================

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_schema = 'scm' 
          AND table_name = 'patch_blobs' 
          AND column_name = 'evidence_uri'
    ) THEN
        ALTER TABLE scm.patch_blobs 
        ADD COLUMN evidence_uri text;
        
        RAISE NOTICE 'Added evidence_uri column to scm.patch_blobs';
    END IF;
END $$;

-- ============================================================
-- 2. 从 meta_json->>'evidence_uri' 回填现有数据
-- ============================================================

-- 回填已有 meta_json.evidence_uri 的记录
UPDATE scm.patch_blobs
SET evidence_uri = meta_json->>'evidence_uri'
WHERE evidence_uri IS NULL 
  AND meta_json->>'evidence_uri' IS NOT NULL
  AND meta_json->>'evidence_uri' != '';

DO $$ BEGIN RAISE NOTICE 'Backfilled evidence_uri from meta_json'; END $$;

-- ============================================================
-- 3. 为剩余记录生成 canonical evidence_uri
-- ============================================================

-- 格式: memory://patch_blobs/<source_type>/<source_id>/<sha256>
UPDATE scm.patch_blobs
SET evidence_uri = 'memory://patch_blobs/' || source_type || '/' || source_id || '/' || sha256
WHERE evidence_uri IS NULL
  AND source_type IS NOT NULL
  AND source_id IS NOT NULL
  AND sha256 IS NOT NULL;

DO $$ BEGIN RAISE NOTICE 'Generated canonical evidence_uri for remaining records'; END $$;

-- ============================================================
-- 4. 同步更新 meta_json（保持双写一致性）
-- ============================================================

-- 将新生成的 evidence_uri 同步到 meta_json
UPDATE scm.patch_blobs
SET meta_json = COALESCE(meta_json, '{}'::jsonb) || jsonb_build_object('evidence_uri', evidence_uri)
WHERE evidence_uri IS NOT NULL
  AND (meta_json->>'evidence_uri' IS NULL OR meta_json->>'evidence_uri' = '');

DO $$ BEGIN RAISE NOTICE 'Synced evidence_uri to meta_json'; END $$;

-- ============================================================
-- 5. 创建索引优化查询
-- ============================================================

-- evidence_uri 索引（用于溯源查询）
CREATE INDEX IF NOT EXISTS idx_patch_blobs_evidence_uri
  ON scm.patch_blobs(evidence_uri)
  WHERE evidence_uri IS NOT NULL;

DO $$ BEGIN RAISE NOTICE 'Created index on evidence_uri'; END $$;

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

-- 验证 evidence_uri 填充情况
SELECT 
    COUNT(*) AS total,
    COUNT(evidence_uri) AS with_uri,
    COUNT(*) - COUNT(evidence_uri) AS without_uri
FROM scm.patch_blobs;

-- 验证双写一致性
SELECT COUNT(*)
FROM scm.patch_blobs
WHERE evidence_uri IS NOT NULL 
  AND (meta_json->>'evidence_uri' IS NULL OR evidence_uri != meta_json->>'evidence_uri');

-- 验证索引
SELECT indexname, indexdef
FROM pg_indexes
WHERE tablename = 'patch_blobs' AND indexname LIKE '%evidence_uri%';
*/
