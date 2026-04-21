-- 15_write_audit_dedup_lookup.sql - 优化 write_audit 去重回退查询
--
-- 目的：
-- 为 memory_store 直写成功路径新增的 dedup fallback 查询提供索引，
-- 避免在 governance.write_audit 上做大范围扫描。

CREATE INDEX IF NOT EXISTS idx_write_audit_dedup_lookup
  ON governance.write_audit (target_space, payload_sha, created_at DESC)
  WHERE action = 'allow'
    AND status = 'success'
    AND payload_sha IS NOT NULL
    AND COALESCE(evidence_refs_json->>'memory_id', '') <> ''
    AND COALESCE(reason, '') <> 'dedup_hit';

COMMENT ON INDEX governance.idx_write_audit_dedup_lookup IS
  'Supports memory_store dedup fallback lookups for successful direct writes.';
