-- ============================================================================
-- 14_write_audit_status.sql - write_audit 表扩展：关联追踪与状态管理
-- ============================================================================
--
-- 本迁移为 governance.write_audit 表添加以下能力：
--   1. correlation_id: 用于关联多个审计记录（如请求追踪）
--   2. status: 记录审计条目的最终状态（pending/success/failed）
--   3. updated_at: 记录状态更新时间
--
-- 使用场景：
--   - 异步审计流程中追踪从 pending -> success/failed 的状态变化
--   - 通过 correlation_id 关联同一请求的多个审计条目
--   - 查询特定状态的审计记录
--
-- ============================================================================

-- 添加 correlation_id 列：用于关联多个审计记录
ALTER TABLE governance.write_audit
  ADD COLUMN IF NOT EXISTS correlation_id text;

-- 添加 status 列：记录审计条目状态
-- 默认 'success' 保持向后兼容（历史记录视为成功）
ALTER TABLE governance.write_audit
  ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'success';

-- 添加 updated_at 列：记录状态更新时间
ALTER TABLE governance.write_audit
  ADD COLUMN IF NOT EXISTS updated_at timestamptz;

-- 添加 CHECK 约束确保 status 值有效
-- 状态说明：
--   - pending: 操作进行中，尚未完成
--   - success: OpenMemory 写入成功
--   - failed: 操作失败（不可恢复）
--   - redirected: OpenMemory 失败，已入队 outbox 等待重试
DO $$
BEGIN
  -- 先删除旧约束（如存在），以便更新允许的状态值
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'write_audit_status_check'
      AND conrelid = 'governance.write_audit'::regclass
  ) THEN
    ALTER TABLE governance.write_audit
      DROP CONSTRAINT write_audit_status_check;
  END IF;
  
  -- 添加新约束（包含 redirected 状态）
  ALTER TABLE governance.write_audit
    ADD CONSTRAINT write_audit_status_check
    CHECK (status IN ('pending', 'success', 'failed', 'redirected'));
END $$;

-- ============================================================================
-- 索引
-- ============================================================================

-- correlation_id 索引：支持按关联 ID 快速查询
CREATE INDEX IF NOT EXISTS idx_write_audit_correlation_id
  ON governance.write_audit (correlation_id)
  WHERE correlation_id IS NOT NULL;

-- status 索引：支持按状态筛选（如查询所有 pending 记录）
CREATE INDEX IF NOT EXISTS idx_write_audit_status
  ON governance.write_audit (status);

-- 复合索引：支持按状态和时间范围查询
CREATE INDEX IF NOT EXISTS idx_write_audit_status_created
  ON governance.write_audit (status, created_at DESC);

-- ============================================================================
-- 回填历史记录
-- ============================================================================

-- 历史记录默认已通过 DEFAULT 'success' 处理
-- 此处显式更新确保一致性（幂等操作）
UPDATE governance.write_audit
SET status = 'success'
WHERE status IS NULL;

-- 为已有记录设置 updated_at = created_at（若未设置）
UPDATE governance.write_audit
SET updated_at = created_at
WHERE updated_at IS NULL;

-- ============================================================================
-- 注释
-- ============================================================================

COMMENT ON COLUMN governance.write_audit.correlation_id IS
  '关联 ID，用于追踪同一请求产生的多个审计记录';

COMMENT ON COLUMN governance.write_audit.status IS
  '审计条目状态：pending（处理中）、success（成功）、failed（失败）、redirected（已入队 outbox）';

COMMENT ON COLUMN governance.write_audit.updated_at IS
  '状态最后更新时间';
