# -*- coding: utf-8 -*-
"""
test_reconcile_outbox - Outbox 对账模块测试

测试用例覆盖：
1. sent 状态缺失 outbox_flush_success 审计的检测与补写
2. dead 状态缺失 outbox_flush_dead 审计的检测与补写
3. pending 状态 stale 锁的检测、审计补写与重新调度
4. 对账配置参数验证
"""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import psycopg


# ---------- 确保可导入 ----------

def ensure_imports():
    """确保必要的导入路径"""
    from pathlib import Path
    scripts_dir = Path(__file__).parent.parent.parent.parent / "step1_logbook_postgres" / "scripts"
    if scripts_dir.exists() and str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))


ensure_imports()


# ---------- Fixtures ----------

@pytest.fixture
def reconcile_env(migrated_db: dict, step1_adapter_config):
    """
    设置对账测试环境
    
    提供已迁移的数据库和配置好的 step1_adapter
    """
    return {
        "dsn": migrated_db["dsn"],
    }


@pytest.fixture
def db_conn_for_reconcile(migrated_db: dict):
    """
    提供用于对账测试的数据库连接（手动提交）
    """
    dsn = migrated_db["dsn"]
    conn = psycopg.connect(dsn, autocommit=False)
    
    with conn.cursor() as cur:
        cur.execute("SET search_path TO logbook, governance, scm, identity, analysis, public")
    
    yield conn
    
    # 清理：回滚未提交的事务
    conn.rollback()
    conn.close()


# ---------- 辅助函数 ----------

def insert_outbox_record(
    conn: psycopg.Connection,
    target_space: str,
    status: str,
    payload_md: str = "test content",
    payload_sha: str = None,
    locked_at: datetime = None,
    locked_by: str = None,
    last_error: str = None,
    retry_count: int = 0,
) -> int:
    """
    直接插入 outbox_memory 测试记录
    
    Returns:
        outbox_id
    """
    if payload_sha is None:
        payload_sha = f"sha256_{uuid.uuid4().hex[:32]}"
    
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO logbook.outbox_memory
                (target_space, payload_md, payload_sha, status, locked_at, locked_by, last_error, retry_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING outbox_id
            """,
            (target_space, payload_md, payload_sha, status, locked_at, locked_by, last_error, retry_count),
        )
        outbox_id = cur.fetchone()[0]
    
    return outbox_id


def insert_audit_record(
    conn: psycopg.Connection,
    outbox_id: int,
    target_space: str,
    reason: str,
    action: str = "allow",
    payload_sha: str = None,
) -> int:
    """
    直接插入 write_audit 测试记录
    
    Returns:
        audit_id
    """
    import json
    
    if payload_sha is None:
        payload_sha = f"sha256_{uuid.uuid4().hex[:32]}"
    
    evidence = json.dumps({"outbox_id": outbox_id, "source": "test"})
    
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO governance.write_audit
                (target_space, action, reason, payload_sha, evidence_refs_json)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING audit_id
            """,
            (target_space, action, reason, payload_sha, evidence),
        )
        audit_id = cur.fetchone()[0]
    
    return audit_id


def count_audits_for_outbox(
    conn: psycopg.Connection,
    outbox_id: int,
    reason_prefix: str,
) -> int:
    """统计指定 outbox_id 的审计记录数"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM governance.write_audit
            WHERE reason LIKE %s
              AND (evidence_refs_json->>'outbox_id')::int = %s
            """,
            (f"{reason_prefix}%", outbox_id),
        )
        return cur.fetchone()[0]


def get_outbox_record(
    conn: psycopg.Connection,
    outbox_id: int,
) -> dict:
    """获取 outbox 记录"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT outbox_id, status, locked_at, locked_by, next_attempt_at
            FROM logbook.outbox_memory
            WHERE outbox_id = %s
            """,
            (outbox_id,),
        )
        row = cur.fetchone()
        if row:
            return {
                "outbox_id": row[0],
                "status": row[1],
                "locked_at": row[2],
                "locked_by": row[3],
                "next_attempt_at": row[4],
            }
        return None


# ---------- 测试用例 ----------

class TestReconcileSentRecords:
    """测试 sent 状态记录的对账"""
    
    def test_detect_sent_missing_audit(self, db_conn_for_reconcile, reconcile_env):
        """测试检测 sent 状态缺失审计的记录"""
        conn = db_conn_for_reconcile
        
        # 准备数据：创建一个 sent 记录，不创建对应的审计
        outbox_id = insert_outbox_record(
            conn,
            target_space="team:test_project",
            status="sent",
            last_error="memory_id=mem_12345",
        )
        conn.commit()
        
        # 验证没有对应的审计
        audit_count = count_audits_for_outbox(conn, outbox_id, "outbox_flush_success")
        assert audit_count == 0, "应该没有 outbox_flush_success 审计"
        
        # 执行对账
        from gateway.reconcile_outbox import ReconcileConfig, run_reconcile
        
        config = ReconcileConfig(
            scan_window_hours=24,
            batch_size=100,
            auto_fix=True,
        )
        
        result = run_reconcile(config)
        
        # 验证检测到缺失
        assert result.sent_missing_audit >= 1, f"应检测到至少 1 条缺失审计，实际: {result.sent_missing_audit}"
        
        # 验证已补写审计
        assert result.sent_audit_fixed >= 1, f"应修复至少 1 条审计，实际: {result.sent_audit_fixed}"
        
        # 验证数据库中已有审计
        new_audit_count = count_audits_for_outbox(conn, outbox_id, "outbox_flush_success")
        assert new_audit_count >= 1, f"应该有 outbox_flush_success 审计，实际: {new_audit_count}"
    
    def test_skip_sent_with_audit(self, db_conn_for_reconcile, reconcile_env):
        """测试跳过已有审计的 sent 记录"""
        conn = db_conn_for_reconcile
        
        # 准备数据：创建一个 sent 记录和对应的审计
        payload_sha = f"sha256_{uuid.uuid4().hex[:32]}"
        outbox_id = insert_outbox_record(
            conn,
            target_space="team:test_project",
            status="sent",
            payload_sha=payload_sha,
        )
        insert_audit_record(
            conn,
            outbox_id=outbox_id,
            target_space="team:test_project",
            reason="outbox_flush_success",
            payload_sha=payload_sha,
        )
        conn.commit()
        
        # 验证已有审计
        audit_count = count_audits_for_outbox(conn, outbox_id, "outbox_flush_success")
        assert audit_count == 1, "应该有 1 条 outbox_flush_success 审计"
        
        # 执行对账
        from gateway.reconcile_outbox import ReconcileConfig, run_reconcile
        
        config = ReconcileConfig(
            scan_window_hours=24,
            auto_fix=True,
        )
        
        result = run_reconcile(config)
        
        # 验证审计数量没有增加（被跳过）
        new_audit_count = count_audits_for_outbox(conn, outbox_id, "outbox_flush_success")
        assert new_audit_count == 1, f"审计数量不应增加，实际: {new_audit_count}"


class TestReconcileDeadRecords:
    """测试 dead 状态记录的对账"""
    
    def test_detect_dead_missing_audit(self, db_conn_for_reconcile, reconcile_env):
        """测试检测 dead 状态缺失审计的记录"""
        conn = db_conn_for_reconcile
        
        # 准备数据：创建一个 dead 记录，不创建对应的审计
        outbox_id = insert_outbox_record(
            conn,
            target_space="team:test_project",
            status="dead",
            last_error="max_retries_exceeded",
            retry_count=5,
        )
        conn.commit()
        
        # 验证没有对应的审计
        audit_count = count_audits_for_outbox(conn, outbox_id, "outbox_flush_dead")
        assert audit_count == 0, "应该没有 outbox_flush_dead 审计"
        
        # 执行对账
        from gateway.reconcile_outbox import ReconcileConfig, run_reconcile
        
        config = ReconcileConfig(
            scan_window_hours=24,
            auto_fix=True,
        )
        
        result = run_reconcile(config)
        
        # 验证检测到缺失
        assert result.dead_missing_audit >= 1, f"应检测到至少 1 条缺失审计，实际: {result.dead_missing_audit}"
        
        # 验证已补写审计
        assert result.dead_audit_fixed >= 1, f"应修复至少 1 条审计，实际: {result.dead_audit_fixed}"
        
        # 验证数据库中已有审计
        new_audit_count = count_audits_for_outbox(conn, outbox_id, "outbox_flush_dead")
        assert new_audit_count >= 1, f"应该有 outbox_flush_dead 审计，实际: {new_audit_count}"
    
    def test_dead_audit_contains_last_error(self, db_conn_for_reconcile, reconcile_env):
        """测试补写的 dead 审计包含 last_error 信息"""
        conn = db_conn_for_reconcile
        
        # 准备数据
        expected_error = "OpenMemory connection timeout after 5 retries"
        outbox_id = insert_outbox_record(
            conn,
            target_space="private:user123",
            status="dead",
            last_error=expected_error,
            retry_count=5,
        )
        conn.commit()
        
        # 执行对账
        from gateway.reconcile_outbox import ReconcileConfig, run_reconcile
        
        config = ReconcileConfig(
            scan_window_hours=24,
            auto_fix=True,
        )
        
        run_reconcile(config)
        
        # 查询补写的审计记录
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT evidence_refs_json
                FROM governance.write_audit
                WHERE reason = 'outbox_flush_dead'
                  AND (evidence_refs_json->>'outbox_id')::int = %s
                """,
                (outbox_id,),
            )
            row = cur.fetchone()
            assert row is not None, "应该有补写的审计记录"
            
            evidence = row[0]
            assert evidence.get("extra", {}).get("last_error") == expected_error, \
                f"审计应包含 last_error: {expected_error}"


class TestReconcileStaleRecords:
    """测试 pending+stale 状态记录的对账"""
    
    def test_detect_stale_locked_record(self, db_conn_for_reconcile, reconcile_env):
        """测试检测 stale（locked 超时）的 pending 记录"""
        conn = db_conn_for_reconcile
        
        # 准备数据：创建一个 pending 记录，locked_at 在 20 分钟前
        stale_time = datetime.now(timezone.utc) - timedelta(minutes=20)
        outbox_id = insert_outbox_record(
            conn,
            target_space="team:test_project",
            status="pending",
            locked_at=stale_time,
            locked_by="worker-abc123",
        )
        conn.commit()
        
        # 验证没有 stale 审计
        audit_count = count_audits_for_outbox(conn, outbox_id, "outbox_stale")
        assert audit_count == 0, "应该没有 outbox_stale 审计"
        
        # 执行对账（stale 阈值 10 分钟）
        from gateway.reconcile_outbox import ReconcileConfig, run_reconcile
        
        config = ReconcileConfig(
            scan_window_hours=24,
            stale_threshold_seconds=600,  # 10 分钟
            auto_fix=True,
            reschedule_stale=False,  # 先不测试重新调度
        )
        
        result = run_reconcile(config)
        
        # 验证检测到 stale
        assert result.stale_count >= 1, f"应检测到至少 1 条 stale 记录，实际: {result.stale_count}"
        assert result.stale_missing_audit >= 1, f"应检测到至少 1 条缺失审计，实际: {result.stale_missing_audit}"
        assert result.stale_audit_fixed >= 1, f"应修复至少 1 条审计，实际: {result.stale_audit_fixed}"
        
        # 验证数据库中已有审计
        new_audit_count = count_audits_for_outbox(conn, outbox_id, "outbox_stale")
        assert new_audit_count >= 1, f"应该有 outbox_stale 审计，实际: {new_audit_count}"
    
    def test_stale_reschedule(self, db_conn_for_reconcile, reconcile_env):
        """测试 stale 记录的重新调度"""
        conn = db_conn_for_reconcile
        
        # 准备数据：创建一个 stale 的 pending 记录
        stale_time = datetime.now(timezone.utc) - timedelta(minutes=20)
        old_next_attempt = datetime.now(timezone.utc) - timedelta(minutes=15)
        
        outbox_id = insert_outbox_record(
            conn,
            target_space="team:test_project",
            status="pending",
            locked_at=stale_time,
            locked_by="worker-stale",
        )
        
        # 设置 next_attempt_at
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE logbook.outbox_memory SET next_attempt_at = %s WHERE outbox_id = %s",
                (old_next_attempt, outbox_id),
            )
        conn.commit()
        
        # 执行对账（开启重新调度）
        from gateway.reconcile_outbox import ReconcileConfig, run_reconcile
        
        config = ReconcileConfig(
            scan_window_hours=24,
            stale_threshold_seconds=600,
            auto_fix=True,
            reschedule_stale=True,
            reschedule_delay_seconds=0,
        )
        
        result = run_reconcile(config)
        
        # 验证重新调度
        assert result.stale_rescheduled >= 1, f"应重新调度至少 1 条记录，实际: {result.stale_rescheduled}"
        
        # 验证数据库中的 locked_at 和 locked_by 已清除
        record = get_outbox_record(conn, outbox_id)
        assert record is not None, "记录应该存在"
        assert record["locked_at"] is None, "locked_at 应该被清除"
        assert record["locked_by"] is None, "locked_by 应该被清除"
    
    def test_skip_non_stale_pending(self, db_conn_for_reconcile, reconcile_env):
        """测试跳过未超时的 pending 记录"""
        conn = db_conn_for_reconcile
        
        # 准备数据：创建一个刚刚 locked 的 pending 记录（5分钟前）
        recent_time = datetime.now(timezone.utc) - timedelta(minutes=5)
        outbox_id = insert_outbox_record(
            conn,
            target_space="team:test_project",
            status="pending",
            locked_at=recent_time,
            locked_by="worker-active",
        )
        conn.commit()
        
        # 执行对账（stale 阈值 10 分钟）
        from gateway.reconcile_outbox import ReconcileConfig, run_reconcile
        
        config = ReconcileConfig(
            scan_window_hours=24,
            stale_threshold_seconds=600,  # 10 分钟
            auto_fix=True,
        )
        
        result = run_reconcile(config)
        
        # 验证未被识别为 stale
        # 检查 details 中是否有这个 outbox_id
        stale_ids = [d["outbox_id"] for d in result.details if d.get("issue") == "stale_lock"]
        assert outbox_id not in stale_ids, f"未超时的记录不应被识别为 stale: {stale_ids}"


class TestReconcileConfig:
    """测试对账配置"""
    
    def test_report_mode_no_fix(self, db_conn_for_reconcile, reconcile_env):
        """测试报告模式（auto_fix=False）不修复"""
        conn = db_conn_for_reconcile
        
        # 准备数据
        outbox_id = insert_outbox_record(
            conn,
            target_space="team:test_project",
            status="sent",
        )
        conn.commit()
        
        # 执行对账（不修复）
        from gateway.reconcile_outbox import ReconcileConfig, run_reconcile
        
        config = ReconcileConfig(
            scan_window_hours=24,
            auto_fix=False,  # 不自动修复
        )
        
        result = run_reconcile(config)
        
        # 验证检测到但未修复
        assert result.sent_missing_audit >= 1, "应检测到缺失审计"
        assert result.sent_audit_fixed == 0, "不应修复任何记录"
        
        # 验证数据库中没有新审计
        audit_count = count_audits_for_outbox(conn, outbox_id, "outbox_flush_success")
        assert audit_count == 0, "不应创建审计记录"
    
    def test_result_summary(self, db_conn_for_reconcile, reconcile_env):
        """测试结果摘要生成"""
        conn = db_conn_for_reconcile
        
        # 准备数据
        insert_outbox_record(conn, target_space="team:test", status="sent")
        insert_outbox_record(conn, target_space="team:test", status="dead")
        conn.commit()
        
        # 执行对账
        from gateway.reconcile_outbox import ReconcileConfig, run_reconcile
        
        config = ReconcileConfig(
            scan_window_hours=24,
            auto_fix=True,
        )
        
        result = run_reconcile(config)
        
        # 验证摘要
        summary = result.summary()
        assert "Outbox Reconcile Report" in summary
        assert "sent:" in summary
        assert "dead:" in summary
        assert "stale:" in summary


class TestReconcileEdgeCases:
    """测试边界情况"""
    
    def test_empty_database(self, db_conn_for_reconcile, reconcile_env):
        """测试空数据库的对账"""
        # 不插入任何数据
        
        from gateway.reconcile_outbox import ReconcileConfig, run_reconcile
        
        config = ReconcileConfig(
            scan_window_hours=24,
            auto_fix=True,
        )
        
        result = run_reconcile(config)
        
        # 验证正常完成
        assert result.total_scanned == 0
        assert result.sent_missing_audit == 0
        assert result.dead_missing_audit == 0
        assert result.stale_missing_audit == 0
    
    def test_dedup_hit_audit_also_valid(self, db_conn_for_reconcile, reconcile_env):
        """测试 outbox_flush_dedup_hit 审计也被视为有效"""
        conn = db_conn_for_reconcile
        
        # 准备数据：创建一个 sent 记录和 dedup_hit 审计
        payload_sha = f"sha256_{uuid.uuid4().hex[:32]}"
        outbox_id = insert_outbox_record(
            conn,
            target_space="team:test_project",
            status="sent",
            payload_sha=payload_sha,
        )
        insert_audit_record(
            conn,
            outbox_id=outbox_id,
            target_space="team:test_project",
            reason="outbox_flush_dedup_hit",  # 使用 dedup_hit 而非 success
            payload_sha=payload_sha,
        )
        conn.commit()
        
        # 执行对账
        from gateway.reconcile_outbox import ReconcileConfig, run_reconcile
        
        config = ReconcileConfig(
            scan_window_hours=24,
            auto_fix=True,
        )
        
        result = run_reconcile(config)
        
        # 验证没有检测为缺失（dedup_hit 也是有效的审计）
        # 检查 details 中是否有这个 outbox_id
        missing_ids = [
            d["outbox_id"] for d in result.details 
            if d.get("status") == "sent" and d.get("issue") == "missing_audit"
        ]
        assert outbox_id not in missing_ids, "有 dedup_hit 审计的记录不应被报告为缺失"
