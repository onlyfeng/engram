-- Artifact Operations Audit 表（可重复执行）
-- 用途：记录 artifact 相关操作审计日志（artifact_gc、artifact_delete 等）
-- 设计原则：幂等执行，只添加不删除；支持多后端存储审计
--
-- 执行方式：psql -d <your_db> -f 12_governance_artifact_ops_audit.sql
-- 或在 pgAdmin/DBeaver 中直接执行

BEGIN;

-- ============================================================
-- governance.artifact_ops_audit 表
-- Artifact 操作审计日志，记录 GC、删除、迁移等操作
-- ============================================================

CREATE TABLE IF NOT EXISTS governance.artifact_ops_audit (
    -- 主键
    event_id            bigserial PRIMARY KEY,
    
    -- 事件时间戳
    event_ts            timestamptz NOT NULL DEFAULT now(),
    
    -- 操作工具标识
    -- 示例值：artifact_gc, artifact_delete, artifact_migrate, artifact_restore
    tool                text NOT NULL,
    
    -- 操作类型
    -- 示例值：delete, move_to_trash, restore, permanent_delete, scan, cleanup
    operation           text NOT NULL,
    
    -- 存储后端类型
    -- 示例值：minio, s3, gcs, azure_blob, filesystem
    backend             text,
    
    -- Artifact 标识（artifact key 或 physical URI）
    -- 示例：scm/1/git/commits/abc123.diff 或 s3://bucket/path/to/file
    uri                 text,
    
    -- 存储桶名称（对象存储场景）
    bucket              text,
    
    -- 对象键（对象存储场景）
    object_key          text,
    
    -- 垃圾桶前缀（软删除场景）
    -- 示例：.trash/20240129_120000/
    trash_prefix        text,
    
    -- 是否使用运维凭据（区分普通用户操作与运维操作）
    using_ops_credentials boolean,
    
    -- 操作是否成功
    success             boolean NOT NULL,
    
    -- 错误信息（失败时）
    error               text,
    
    -- 详细信息（JSON 格式）
    -- 示例：{"size_bytes": 1024, "content_sha256": "abc...", "dry_run": false, "reason": "expired"}
    details             jsonb DEFAULT '{}'
);

-- 添加表注释
COMMENT ON TABLE governance.artifact_ops_audit IS 'Artifact 操作审计日志表，记录 GC、删除、迁移等操作';
COMMENT ON COLUMN governance.artifact_ops_audit.event_id IS '事件唯一标识';
COMMENT ON COLUMN governance.artifact_ops_audit.event_ts IS '事件时间戳';
COMMENT ON COLUMN governance.artifact_ops_audit.tool IS '操作工具（artifact_gc, artifact_delete 等）';
COMMENT ON COLUMN governance.artifact_ops_audit.operation IS '操作类型（delete, move_to_trash, restore 等）';
COMMENT ON COLUMN governance.artifact_ops_audit.backend IS '存储后端类型（minio, s3, gcs 等）';
COMMENT ON COLUMN governance.artifact_ops_audit.uri IS 'Artifact 标识（artifact key 或 physical URI）';
COMMENT ON COLUMN governance.artifact_ops_audit.bucket IS '存储桶名称（对象存储场景）';
COMMENT ON COLUMN governance.artifact_ops_audit.object_key IS '对象键（对象存储场景）';
COMMENT ON COLUMN governance.artifact_ops_audit.trash_prefix IS '垃圾桶前缀（软删除场景）';
COMMENT ON COLUMN governance.artifact_ops_audit.using_ops_credentials IS '是否使用运维凭据';
COMMENT ON COLUMN governance.artifact_ops_audit.success IS '操作是否成功';
COMMENT ON COLUMN governance.artifact_ops_audit.error IS '错误信息（失败时）';
COMMENT ON COLUMN governance.artifact_ops_audit.details IS '详细信息（JSON 格式）';

-- ============================================================
-- 索引
-- ============================================================

-- 按时间戳查询（最常用，用于时间范围查询和清理归档）
CREATE INDEX IF NOT EXISTS idx_artifact_ops_audit_ts
    ON governance.artifact_ops_audit(event_ts DESC);

-- 按 URI 查询（用于追踪特定 artifact 的操作历史）
CREATE INDEX IF NOT EXISTS idx_artifact_ops_audit_uri
    ON governance.artifact_ops_audit(uri, event_ts DESC)
    WHERE uri IS NOT NULL;

-- 按 bucket 查询（用于按存储桶统计操作）
CREATE INDEX IF NOT EXISTS idx_artifact_ops_audit_bucket
    ON governance.artifact_ops_audit(bucket, event_ts DESC)
    WHERE bucket IS NOT NULL;

-- 按工具和时间查询（用于分析特定工具的执行历史）
CREATE INDEX IF NOT EXISTS idx_artifact_ops_audit_tool_ts
    ON governance.artifact_ops_audit(tool, event_ts DESC);

-- 按操作类型查询（用于审计特定类型的操作）
CREATE INDEX IF NOT EXISTS idx_artifact_ops_audit_operation
    ON governance.artifact_ops_audit(operation, event_ts DESC);

-- 按成功/失败状态查询（用于故障分析和监控）
CREATE INDEX IF NOT EXISTS idx_artifact_ops_audit_success
    ON governance.artifact_ops_audit(success, event_ts DESC)
    WHERE success = false;

COMMIT;

-- ============================================================
-- 验证（可选，取消注释执行）
-- ============================================================
/*
-- 验证表结构
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns 
WHERE table_schema = 'governance' AND table_name = 'artifact_ops_audit'
ORDER BY ordinal_position;

-- 验证索引
SELECT indexname, indexdef 
FROM pg_indexes 
WHERE schemaname = 'governance' AND tablename = 'artifact_ops_audit';

-- 插入测试事件
INSERT INTO governance.artifact_ops_audit 
    (tool, operation, backend, uri, bucket, object_key, success, details)
VALUES 
    ('artifact_gc', 'move_to_trash', 'minio', 'scm/1/git/commits/abc123.diff', 
     'engram-artifacts', 'scm/1/git/commits/abc123.diff', true, 
     '{"size_bytes": 1024, "reason": "expired"}');

-- 查询验证
SELECT * FROM governance.artifact_ops_audit ORDER BY event_ts DESC LIMIT 5;
*/
