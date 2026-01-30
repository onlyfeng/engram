-- Object Store Audit Events 表（可重复执行）
-- 用途：记录对象存储（MinIO/S3）的审计事件，用于安全审计、合规追溯
-- 设计原则：幂等执行，只添加不删除；支持 MinIO 和 AWS S3 审计日志导入
--
-- 执行方式：psql -d <your_db> -f 13_governance_object_store_audit_events.sql
-- 或在 pgAdmin/DBeaver 中直接执行

BEGIN;

-- ============================================================
-- governance.object_store_audit_events 表
-- 对象存储审计日志，记录 MinIO/S3 等对象存储的 API 操作
-- ============================================================

CREATE TABLE IF NOT EXISTS governance.object_store_audit_events (
    -- 主键
    event_id            bigserial PRIMARY KEY,
    
    -- 对象存储提供者类型
    -- 示例值：minio, aws, gcs, azure_blob
    provider            text NOT NULL,
    
    -- 事件时间戳（原始事件发生时间）
    event_ts            timestamptz NOT NULL,
    
    -- 存储桶名称
    bucket              text NOT NULL,
    
    -- 对象键
    object_key          text,
    
    -- 操作类型
    -- 示例值：s3:GetObject, s3:PutObject, s3:DeleteObject, s3:ListBucket, s3:HeadObject
    operation           text NOT NULL,
    
    -- HTTP 状态码
    status_code         int,
    
    -- 请求 ID（用于追踪和去重）
    request_id          text,
    
    -- 操作者标识（IAM 用户/角色/访问密钥）
    -- 示例：arn:aws:iam::123456:user/admin, AKIAIOSFODNN7EXAMPLE
    principal           text,
    
    -- 客户端 IP 地址
    remote_ip           inet,
    
    -- 原始审计日志（JSON 格式，完整保留以供详细分析）
    -- MinIO 和 S3 的审计日志格式可能不同，使用 jsonb 灵活存储
    raw                 jsonb DEFAULT '{}',
    
    -- 数据入库时间
    ingested_at         timestamptz NOT NULL DEFAULT now()
);

-- 添加表注释
COMMENT ON TABLE governance.object_store_audit_events IS '对象存储审计日志表，记录 MinIO/S3 等对象存储的 API 操作';
COMMENT ON COLUMN governance.object_store_audit_events.event_id IS '事件唯一标识';
COMMENT ON COLUMN governance.object_store_audit_events.provider IS '对象存储提供者（minio, aws, gcs 等）';
COMMENT ON COLUMN governance.object_store_audit_events.event_ts IS '事件时间戳（原始事件发生时间）';
COMMENT ON COLUMN governance.object_store_audit_events.bucket IS '存储桶名称';
COMMENT ON COLUMN governance.object_store_audit_events.object_key IS '对象键';
COMMENT ON COLUMN governance.object_store_audit_events.operation IS '操作类型（s3:GetObject, s3:PutObject 等）';
COMMENT ON COLUMN governance.object_store_audit_events.status_code IS 'HTTP 状态码';
COMMENT ON COLUMN governance.object_store_audit_events.request_id IS '请求 ID（用于追踪和去重）';
COMMENT ON COLUMN governance.object_store_audit_events.principal IS '操作者标识（IAM 用户/角色/访问密钥）';
COMMENT ON COLUMN governance.object_store_audit_events.remote_ip IS '客户端 IP 地址';
COMMENT ON COLUMN governance.object_store_audit_events.raw IS '原始审计日志（JSON 格式）';
COMMENT ON COLUMN governance.object_store_audit_events.ingested_at IS '数据入库时间';

-- ============================================================
-- 索引
-- ============================================================

-- 复合索引：按 bucket + object_key + event_ts 查询（最常用，用于追踪特定对象的操作历史）
CREATE INDEX IF NOT EXISTS idx_object_store_audit_bucket_key_ts
    ON governance.object_store_audit_events(bucket, object_key, event_ts DESC)
    WHERE object_key IS NOT NULL;

-- 按操作类型查询（用于统计特定 API 操作）
CREATE INDEX IF NOT EXISTS idx_object_store_audit_operation
    ON governance.object_store_audit_events(operation, event_ts DESC);

-- 按 provider 查询（用于区分 MinIO / AWS 等）
CREATE INDEX IF NOT EXISTS idx_object_store_audit_provider
    ON governance.object_store_audit_events(provider, event_ts DESC);

-- 按 request_id 查询（用于追踪单次请求）
CREATE INDEX IF NOT EXISTS idx_object_store_audit_request_id
    ON governance.object_store_audit_events(request_id)
    WHERE request_id IS NOT NULL;

COMMIT;

-- ============================================================
-- 验证（可选，取消注释执行）
-- ============================================================
/*
-- 验证表结构
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns 
WHERE table_schema = 'governance' AND table_name = 'object_store_audit_events'
ORDER BY ordinal_position;

-- 验证索引
SELECT indexname, indexdef 
FROM pg_indexes 
WHERE schemaname = 'governance' AND tablename = 'object_store_audit_events';
*/
