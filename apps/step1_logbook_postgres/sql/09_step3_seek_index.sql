-- ============================================================
-- Step3: Seek Index Schema / DDL
-- ============================================================
--
-- 本脚本创建 Step3 检索索引相关的 schema 和表结构。
--
-- 设计原则：
--   1. 使用独立 schema（step3）隔离检索索引数据
--   2. public schema 仅用于 pgvector 扩展（不在 public 中创建业务表）
--   3. 支持混合检索：向量相似度 + 全文搜索 + 字段过滤
--
-- 依赖：
--   - pgvector 扩展（已在 public schema 中安装）
--
-- 使用场景：
--   - 从 patch_blobs、knowledge_candidates 等源构建检索索引
--   - 支持 RAG 系统的 chunk 检索
--   - 证据溯源（通过 artifact_uri 关联原始数据）
--
-- 与 pgvector_backend.py 保持一致
--
-- ============================================================

BEGIN;

-- ============================================================
-- 1. 创建 Step3 Schema
-- ============================================================

CREATE SCHEMA IF NOT EXISTS step3;

DO $$ BEGIN RAISE NOTICE 'Step3 schema created'; END $$;

-- ============================================================
-- 2. 创建 chunks 表（检索索引核心表）
-- ============================================================
-- 
-- 表设计说明（与 pgvector_backend.py 一致）：
--   - chunk_id: 主键，TEXT 类型（由应用层生成，格式含 project:source_type:source_id:sha256:version:chunk_idx）
--   - content: 完整 chunk 内容（用于检索后返回）
--   - vector: 向量表示（维度由 embedding_model 决定，默认 1536）
--   - project_key: 项目标识（用于过滤）
--   - module: 模块标识（用于过滤）
--   - source_type: 来源类型（patch_blob, knowledge_candidate 等）
--   - source_id: 来源标识
--   - owner_user_id: 所有者用户 ID
--   - commit_ts: 提交时间戳
--   - artifact_uri: 原始数据 URI（用于溯源）
--   - sha256: 内容哈希（用于去重和版本判断）
--   - chunk_idx: chunk 在文档中的索引
--   - excerpt: 摘要/片段（可选，用于预览）
--   - metadata: 扩展元数据（JSONB）
--

CREATE TABLE IF NOT EXISTS step3.chunks (
    -- 主键
    chunk_id            TEXT PRIMARY KEY,
    
    -- 内容
    content             TEXT NOT NULL,
    
    -- 向量表示
    -- 维度说明：
    --   - OpenAI text-embedding-3-small: 1536
    --   - OpenAI text-embedding-3-large: 3072
    --   - BGE-M3: 1024
    --   - 本地模型 (如 all-MiniLM-L6-v2): 384
    -- 使用 1536 作为默认维度（兼容 OpenAI 最常用模型）
    vector              vector(1536),
    
    -- 过滤字段
    project_key         TEXT,
    module              TEXT,
    source_type         TEXT,
    source_id           TEXT,
    owner_user_id       TEXT,
    commit_ts           TIMESTAMP WITH TIME ZONE,
    
    -- 溯源与去重
    artifact_uri        TEXT,
    sha256              TEXT,
    chunk_idx           INTEGER,
    
    -- 摘要
    excerpt             TEXT,
    
    -- 扩展元数据
    metadata            JSONB,
    
    -- 集合标识（用于分组管理和批量操作）
    collection_id       TEXT,
    
    -- 时间戳
    created_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- ============================================================
-- 2.1 兼容性：为已存在的表添加 collection_id 字段
-- ============================================================

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_schema = 'step3' 
          AND table_name = 'chunks' 
          AND column_name = 'collection_id'
    ) THEN
        ALTER TABLE step3.chunks ADD COLUMN collection_id TEXT;
        RAISE NOTICE 'Added collection_id column to step3.chunks';
    END IF;
END $$;

-- ============================================================
-- 3. 创建索引（与 pgvector_backend.py initialize() 一致）
-- ============================================================

-- 3.1 向量索引（IVFFlat）
-- IVFFlat 参数说明：
--   - lists: 聚类数量（默认 100，数据量大时可增加）
-- 选择 cosine 距离（适用于归一化向量，如 OpenAI embeddings）
-- 注意：IVFFlat 需要先有数据才能创建有效索引
CREATE INDEX IF NOT EXISTS step3_chunks_vector_idx 
    ON step3.chunks 
    USING ivfflat (vector vector_cosine_ops)
    WITH (lists = 100);

-- 3.2 全文搜索索引（GIN）
-- 使用 simple 配置以支持多语言（中英混合）
CREATE INDEX IF NOT EXISTS step3_chunks_content_fts_idx 
    ON step3.chunks 
    USING gin (to_tsvector('simple', content));

-- 3.3 过滤字段索引
CREATE INDEX IF NOT EXISTS step3_chunks_project_key_idx 
    ON step3.chunks (project_key);

CREATE INDEX IF NOT EXISTS step3_chunks_module_idx 
    ON step3.chunks (module);

CREATE INDEX IF NOT EXISTS step3_chunks_source_type_idx 
    ON step3.chunks (source_type);

CREATE INDEX IF NOT EXISTS step3_chunks_commit_ts_idx 
    ON step3.chunks (commit_ts);

CREATE INDEX IF NOT EXISTS step3_chunks_source_id_idx 
    ON step3.chunks (source_id);

CREATE INDEX IF NOT EXISTS step3_chunks_collection_id_idx 
    ON step3.chunks (collection_id);

-- ============================================================
-- 4. 创建触发器（updated_at 自动更新）
-- ============================================================

CREATE OR REPLACE FUNCTION step3.update_chunks_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger 
        WHERE tgname = 'trg_chunks_updated_at'
          AND tgrelid = 'step3.chunks'::regclass
    ) THEN
        CREATE TRIGGER trg_chunks_updated_at
            BEFORE UPDATE ON step3.chunks
            FOR EACH ROW
            EXECUTE FUNCTION step3.update_chunks_updated_at();
    END IF;
END $$;

-- ============================================================
-- 5. 常用查询示例（文档用途，不执行）
-- ============================================================
/*

-- 5.1 向量相似度搜索（Top-K）
SELECT chunk_id, artifact_uri, excerpt, 
       1 - (vector <=> $1::vector) AS similarity
FROM step3.chunks
WHERE vector IS NOT NULL
ORDER BY vector <=> $1::vector
LIMIT 10;

-- 5.2 全文搜索
SELECT chunk_id, artifact_uri, excerpt,
       ts_rank(to_tsvector('simple', content), query) AS rank
FROM step3.chunks, to_tsquery('simple', 'keyword1 & keyword2') query
WHERE to_tsvector('simple', content) @@ query
ORDER BY rank DESC
LIMIT 10;

-- 5.3 混合检索（向量 + 全文 + 字段过滤）
SELECT chunk_id, artifact_uri, excerpt,
       1 - (vector <=> $1::vector) AS similarity,
       ts_rank(to_tsvector('simple', content), $2) AS text_rank
FROM step3.chunks
WHERE project_key = 'my_project'
  AND source_type = 'patch_blob'
  AND to_tsvector('simple', content) @@ $2
  AND vector IS NOT NULL
ORDER BY (1 - (vector <=> $1::vector)) * 0.7 + ts_rank(to_tsvector('simple', content), $2) * 0.3 DESC
LIMIT 10;

-- 5.4 按 artifact_uri 查找（溯源）
SELECT * FROM step3.chunks
WHERE artifact_uri = 'memory://patch_blobs/git/1:abc123/sha256';

-- 5.5 Upsert 示例（幂等插入/更新）
INSERT INTO step3.chunks (
    chunk_id, content, vector, project_key, module,
    source_type, source_id, owner_user_id, commit_ts,
    artifact_uri, sha256, chunk_idx, excerpt, metadata, collection_id
) VALUES (
    $1, $2, $3, $4, $5,
    $6, $7, $8, $9,
    $10, $11, $12, $13, $14, $15
)
ON CONFLICT (chunk_id) DO UPDATE SET
    content = EXCLUDED.content,
    vector = EXCLUDED.vector,
    project_key = EXCLUDED.project_key,
    module = EXCLUDED.module,
    source_type = EXCLUDED.source_type,
    source_id = EXCLUDED.source_id,
    owner_user_id = EXCLUDED.owner_user_id,
    commit_ts = EXCLUDED.commit_ts,
    artifact_uri = EXCLUDED.artifact_uri,
    sha256 = EXCLUDED.sha256,
    chunk_idx = EXCLUDED.chunk_idx,
    excerpt = EXCLUDED.excerpt,
    metadata = EXCLUDED.metadata,
    collection_id = EXCLUDED.collection_id,
    updated_at = NOW();

*/

-- ============================================================
-- 6. 权限说明（由 bootstrap_roles 统一管理）
-- ============================================================
--
-- Step3 schema 权限遵循与其他 schema 相同的模式：
--   - step3_migrator: DDL 权限（CREATE TABLE, ALTER TABLE 等）
--   - step3_app: DML 权限（SELECT, INSERT, UPDATE, DELETE）
--
-- 如需配置专用角色，可参考 05_openmemory_roles_and_grants.sql
-- 或在 04_roles_and_grants.sql 中添加相应配置
--

DO $$ BEGIN RAISE NOTICE 'Step3 seek index schema and chunks table created'; END $$;

COMMIT;
