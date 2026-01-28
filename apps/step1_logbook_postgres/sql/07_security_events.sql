-- Security Events 表（可重复执行）
-- 用途：记录安全相关事件（角色创建、权限变更、敏感操作审计等）
-- 设计原则：幂等执行，只添加不删除；不存储敏感明文
--
-- 执行方式：psql -d <your_db> -f 07_security_events.sql
-- 或在 pgAdmin/DBeaver 中直接执行

BEGIN;

-- ============================================================
-- governance.security_events 表
-- 安全事件审计日志，记录所有安全相关操作
-- ============================================================

CREATE TABLE IF NOT EXISTS governance.security_events (
    -- 主键
    event_id        bigserial PRIMARY KEY,
    
    -- 事件时间戳
    ts              timestamptz NOT NULL DEFAULT now(),
    
    -- 操作者标识（用户名、角色名或服务账号，不含密码等敏感信息）
    actor           text NOT NULL,
    
    -- 操作类型
    -- 示例值：role_created, role_updated, role_deleted, password_changed,
    --         grant_applied, permission_verified, bootstrap_completed,
    --         credential_rotated, hardening_applied
    action          text NOT NULL,
    
    -- 操作对象类型
    -- 示例值：role, database, schema, table, credential, config
    object_type     text NOT NULL,
    
    -- 操作对象标识（如角色名、数据库名等）
    object_id       text,
    
    -- 详细信息（JSON 格式，不含敏感明文如密码）
    -- 示例：{"created": true, "inherited_role": "engram_migrator", "source": "db_bootstrap"}
    detail_json     jsonb DEFAULT '{}',
    
    -- 来源标识（记录事件来源脚本/模块）
    source          text,
    
    -- 创建时间（与 ts 相同，用于分区策略）
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- 添加表注释
COMMENT ON TABLE governance.security_events IS '安全事件审计日志表，记录角色、权限、凭据等安全相关操作';
COMMENT ON COLUMN governance.security_events.event_id IS '事件唯一标识';
COMMENT ON COLUMN governance.security_events.ts IS '事件时间戳';
COMMENT ON COLUMN governance.security_events.actor IS '操作者标识（用户名/角色名/服务账号）';
COMMENT ON COLUMN governance.security_events.action IS '操作类型（role_created, grant_applied, credential_rotated 等）';
COMMENT ON COLUMN governance.security_events.object_type IS '操作对象类型（role, database, schema, credential 等）';
COMMENT ON COLUMN governance.security_events.object_id IS '操作对象标识';
COMMENT ON COLUMN governance.security_events.detail_json IS '事件详情（JSON，不含敏感明文）';
COMMENT ON COLUMN governance.security_events.source IS '事件来源（脚本/模块标识）';
COMMENT ON COLUMN governance.security_events.created_at IS '记录创建时间';

-- ============================================================
-- 索引
-- ============================================================

-- 按时间戳查询（最常用，用于时间范围查询和清理归档）
CREATE INDEX IF NOT EXISTS idx_security_events_ts
    ON governance.security_events(ts DESC);

-- 按操作类型查询（用于审计特定类型的操作）
CREATE INDEX IF NOT EXISTS idx_security_events_action
    ON governance.security_events(action, ts DESC);

-- 按对象类型查询（用于查看特定对象类型的操作历史）
CREATE INDEX IF NOT EXISTS idx_security_events_object_type
    ON governance.security_events(object_type, ts DESC);

-- 按操作者查询（用于追踪特定用户的操作记录）
CREATE INDEX IF NOT EXISTS idx_security_events_actor
    ON governance.security_events(actor, ts DESC);

-- 按对象 ID 查询（用于查看特定对象的变更历史）
CREATE INDEX IF NOT EXISTS idx_security_events_object_id
    ON governance.security_events(object_id, ts DESC)
    WHERE object_id IS NOT NULL;

-- 复合索引：按来源和时间查询（用于分析特定脚本的执行历史）
CREATE INDEX IF NOT EXISTS idx_security_events_source_ts
    ON governance.security_events(source, ts DESC)
    WHERE source IS NOT NULL;

COMMIT;

-- ============================================================
-- 验证（可选，取消注释执行）
-- ============================================================
/*
-- 验证表结构
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns 
WHERE table_schema = 'governance' AND table_name = 'security_events'
ORDER BY ordinal_position;

-- 验证索引
SELECT indexname, indexdef 
FROM pg_indexes 
WHERE schemaname = 'governance' AND tablename = 'security_events';

-- 插入测试事件
INSERT INTO governance.security_events (actor, action, object_type, object_id, detail_json, source)
VALUES ('test_admin', 'role_created', 'role', 'test_role', '{"created": true}', 'test');

-- 查询验证
SELECT * FROM governance.security_events ORDER BY ts DESC LIMIT 5;
*/
