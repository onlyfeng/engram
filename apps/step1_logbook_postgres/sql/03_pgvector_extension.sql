-- pgvector 扩展初始化脚本（幂等）
-- 在 public schema 中创建 pgvector 扩展
--
-- 执行时机：
--   1. 首次初始化：由 docker-entrypoint-initdb.d 自动执行
--   2. 已有 volume：由 bootstrap_roles 服务显式执行
--
-- 设计原则：
--   - pgvector 扩展仅在 public schema 中创建（PostgreSQL 扩展的标准做法）
--   - 业务数据存放在独立 schema（openmemory, step3 等）
--   - 脚本幂等，可重复执行

-- ============================================================
-- 创建 pgvector 扩展（如果不存在）
-- ============================================================
CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================
-- 验证扩展已安装
-- ============================================================
DO $$
DECLARE
    ext_version TEXT;
BEGIN
    SELECT extversion INTO ext_version
    FROM pg_extension
    WHERE extname = 'vector';
    
    IF ext_version IS NULL THEN
        RAISE EXCEPTION 'pgvector extension not installed!';
    ELSE
        RAISE NOTICE 'pgvector extension version: %', ext_version;
    END IF;
END $$;
