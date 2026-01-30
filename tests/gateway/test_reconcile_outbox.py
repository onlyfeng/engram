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
    scripts_dir = Path(__file__).parent.parent.parent.parent / "logbook_postgres" / "scripts"
    if scripts_dir.exists() and str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))


ensure_imports()


# ---------- Fixtures ----------

@pytest.fixture
def reconcile_env(migrated_db: dict, logbook_adapter_config):
    """
    设置对账测试环境
    
    提供已迁移的数据库和配置好的 logbook_adapter
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
    """测试 sent 状态记录的对账
    
    覆盖场景：
    1. 缺失审计→补写
    2. report 模式不补写
    3. 补写后可查询（验证 reason 前缀匹配）
    """
    
    def test_detect_sent_missing_audit(self, db_conn_for_reconcile, reconcile_env):
        """测试检测 sent 状态缺失审计的记录（缺失审计→补写）"""
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
        from engram.gateway.reconcile_outbox import ReconcileConfig, run_reconcile
        
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
    
    def test_sent_report_mode_no_fix(self, db_conn_for_reconcile, reconcile_env):
        """测试 sent 场景 report 模式不补写"""
        conn = db_conn_for_reconcile
        
        # 准备数据：创建一个 sent 记录，不创建对应的审计
        outbox_id = insert_outbox_record(
            conn,
            target_space="private:user_report_sent",
            status="sent",
        )
        conn.commit()
        
        # 验证没有对应的审计
        audit_count = count_audits_for_outbox(conn, outbox_id, "outbox_flush_success")
        assert audit_count == 0, "应该没有 outbox_flush_success 审计"
        
        # 执行对账（report 模式，不修复）
        from engram.gateway.reconcile_outbox import ReconcileConfig, run_reconcile
        
        config = ReconcileConfig(
            scan_window_hours=24,
            auto_fix=False,  # report 模式
        )
        
        result = run_reconcile(config)
        
        # 验证检测到缺失但未修复
        assert result.sent_missing_audit >= 1, "应检测到缺失审计"
        assert result.sent_audit_fixed == 0, "report 模式不应修复任何记录"
        
        # 验证数据库中仍没有审计
        new_audit_count = count_audits_for_outbox(conn, outbox_id, "outbox_flush_success")
        assert new_audit_count == 0, "report 模式不应创建审计记录"
    
    def test_sent_audit_queryable_after_fix(self, db_conn_for_reconcile, reconcile_env):
        """测试 sent 补写后可查询（验证 reason 前缀匹配）"""
        conn = db_conn_for_reconcile
        
        # 准备数据：创建一个 sent 记录
        payload_sha = f"sha256_{uuid.uuid4().hex[:32]}"
        outbox_id = insert_outbox_record(
            conn,
            target_space="private:user_query_sent",
            status="sent",
            payload_sha=payload_sha,
            last_error="memory_id=mem_query_test",
        )
        conn.commit()
        
        # 执行对账补写
        from engram.gateway.reconcile_outbox import ReconcileConfig, run_reconcile
        
        config = ReconcileConfig(
            scan_window_hours=24,
            auto_fix=True,
        )
        
        run_reconcile(config)
        
        # 验证可以通过 reason 前缀查询到补写的审计
        # 使用 LIKE 'outbox_flush_success%' 查询
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT audit_id, reason, evidence_refs_json
                FROM governance.write_audit
                WHERE reason LIKE 'outbox_flush_success%'
                  AND (evidence_refs_json->>'outbox_id')::int = %s
                """,
                (outbox_id,),
            )
            row = cur.fetchone()
            assert row is not None, "应该能通过 reason 前缀查询到补写的审计"
            
            audit_id, reason, evidence_refs_json = row
            # 验证 reason 以 outbox_flush_success 开头
            assert reason.startswith("outbox_flush_success"), \
                f"reason '{reason}' 应以 'outbox_flush_success' 开头"
            # 验证 evidence_refs_json 包含正确的 outbox_id
            assert evidence_refs_json.get("gateway_event", {}).get("outbox_id") == outbox_id or \
                   evidence_refs_json.get("outbox_id") == outbox_id, \
                "evidence_refs_json 应包含正确的 outbox_id"
    
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
        from engram.gateway.reconcile_outbox import ReconcileConfig, run_reconcile
        
        config = ReconcileConfig(
            scan_window_hours=24,
            auto_fix=True,
        )
        
        result = run_reconcile(config)
        
        # 验证审计数量没有增加（被跳过）
        new_audit_count = count_audits_for_outbox(conn, outbox_id, "outbox_flush_success")
        assert new_audit_count == 1, f"审计数量不应增加，实际: {new_audit_count}"


class TestReconcileDeadRecords:
    """测试 dead 状态记录的对账
    
    覆盖场景：
    1. 缺失审计→补写
    2. report 模式不补写
    3. 补写后可查询（验证 reason 前缀匹配）
    """
    
    def test_detect_dead_missing_audit(self, db_conn_for_reconcile, reconcile_env):
        """测试检测 dead 状态缺失审计的记录（缺失审计→补写）"""
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
        from engram.gateway.reconcile_outbox import ReconcileConfig, run_reconcile
        
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
    
    def test_dead_report_mode_no_fix(self, db_conn_for_reconcile, reconcile_env):
        """测试 dead 场景 report 模式不补写"""
        conn = db_conn_for_reconcile
        
        # 准备数据：创建一个 dead 记录，不创建对应的审计
        outbox_id = insert_outbox_record(
            conn,
            target_space="private:user_report_dead",
            status="dead",
            last_error="api_error_500",
            retry_count=5,
        )
        conn.commit()
        
        # 验证没有对应的审计
        audit_count = count_audits_for_outbox(conn, outbox_id, "outbox_flush_dead")
        assert audit_count == 0, "应该没有 outbox_flush_dead 审计"
        
        # 执行对账（report 模式，不修复）
        from engram.gateway.reconcile_outbox import ReconcileConfig, run_reconcile
        
        config = ReconcileConfig(
            scan_window_hours=24,
            auto_fix=False,  # report 模式
        )
        
        result = run_reconcile(config)
        
        # 验证检测到缺失但未修复
        assert result.dead_missing_audit >= 1, "应检测到缺失审计"
        assert result.dead_audit_fixed == 0, "report 模式不应修复任何记录"
        
        # 验证数据库中仍没有审计
        new_audit_count = count_audits_for_outbox(conn, outbox_id, "outbox_flush_dead")
        assert new_audit_count == 0, "report 模式不应创建审计记录"
    
    def test_dead_audit_queryable_after_fix(self, db_conn_for_reconcile, reconcile_env):
        """测试 dead 补写后可查询（验证 reason 前缀匹配）"""
        conn = db_conn_for_reconcile
        
        # 准备数据：创建一个 dead 记录
        payload_sha = f"sha256_{uuid.uuid4().hex[:32]}"
        outbox_id = insert_outbox_record(
            conn,
            target_space="private:user_query_dead",
            status="dead",
            payload_sha=payload_sha,
            last_error="connection_timeout",
            retry_count=5,
        )
        conn.commit()
        
        # 执行对账补写
        from engram.gateway.reconcile_outbox import ReconcileConfig, run_reconcile
        
        config = ReconcileConfig(
            scan_window_hours=24,
            auto_fix=True,
        )
        
        run_reconcile(config)
        
        # 验证可以通过 reason 前缀查询到补写的审计
        # 使用 LIKE 'outbox_flush_dead%' 查询
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT audit_id, reason, evidence_refs_json
                FROM governance.write_audit
                WHERE reason LIKE 'outbox_flush_dead%'
                  AND (evidence_refs_json->>'outbox_id')::int = %s
                """,
                (outbox_id,),
            )
            row = cur.fetchone()
            assert row is not None, "应该能通过 reason 前缀查询到补写的审计"
            
            audit_id, reason, evidence_refs_json = row
            # 验证 reason 以 outbox_flush_dead 开头
            assert reason.startswith("outbox_flush_dead"), \
                f"reason '{reason}' 应以 'outbox_flush_dead' 开头"
            # 验证 evidence_refs_json 包含正确的 outbox_id
            assert evidence_refs_json.get("gateway_event", {}).get("outbox_id") == outbox_id or \
                   evidence_refs_json.get("outbox_id") == outbox_id, \
                "evidence_refs_json 应包含正确的 outbox_id"
    
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
        from engram.gateway.reconcile_outbox import ReconcileConfig, run_reconcile
        
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
    """测试 pending+stale 状态记录的对账
    
    覆盖场景：
    1. 缺失审计→补写
    2. report 模式不补写
    3. 补写后可查询（验证 reason 前缀匹配）
    """
    
    def test_detect_stale_locked_record(self, db_conn_for_reconcile, reconcile_env):
        """测试检测 stale（locked 超时）的 pending 记录（缺失审计→补写）"""
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
        from engram.gateway.reconcile_outbox import ReconcileConfig, run_reconcile
        
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
    
    def test_stale_report_mode_no_fix(self, db_conn_for_reconcile, reconcile_env):
        """测试 stale 场景 report 模式不补写"""
        conn = db_conn_for_reconcile
        
        # 准备数据：创建一个 stale 的 pending 记录
        stale_time = datetime.now(timezone.utc) - timedelta(minutes=20)
        outbox_id = insert_outbox_record(
            conn,
            target_space="private:user_report_stale",
            status="pending",
            locked_at=stale_time,
            locked_by="worker-report-stale",
        )
        conn.commit()
        
        # 验证没有 stale 审计
        audit_count = count_audits_for_outbox(conn, outbox_id, "outbox_stale")
        assert audit_count == 0, "应该没有 outbox_stale 审计"
        
        # 执行对账（report 模式，不修复）
        from engram.gateway.reconcile_outbox import ReconcileConfig, run_reconcile
        
        config = ReconcileConfig(
            scan_window_hours=24,
            stale_threshold_seconds=600,
            auto_fix=False,  # report 模式
            reschedule_stale=False,
        )
        
        result = run_reconcile(config)
        
        # 验证检测到缺失但未修复
        assert result.stale_count >= 1, "应检测到 stale 记录"
        assert result.stale_missing_audit >= 1, "应检测到缺失审计"
        assert result.stale_audit_fixed == 0, "report 模式不应修复任何记录"
        
        # 验证数据库中仍没有审计
        new_audit_count = count_audits_for_outbox(conn, outbox_id, "outbox_stale")
        assert new_audit_count == 0, "report 模式不应创建审计记录"
    
    def test_stale_audit_queryable_after_fix(self, db_conn_for_reconcile, reconcile_env):
        """测试 stale 补写后可查询（验证 reason 前缀匹配）"""
        conn = db_conn_for_reconcile
        
        # 准备数据：创建一个 stale 的 pending 记录
        stale_time = datetime.now(timezone.utc) - timedelta(minutes=20)
        payload_sha = f"sha256_{uuid.uuid4().hex[:32]}"
        outbox_id = insert_outbox_record(
            conn,
            target_space="private:user_query_stale",
            status="pending",
            payload_sha=payload_sha,
            locked_at=stale_time,
            locked_by="worker-query-stale",
        )
        conn.commit()
        
        # 执行对账补写
        from engram.gateway.reconcile_outbox import ReconcileConfig, run_reconcile
        
        config = ReconcileConfig(
            scan_window_hours=24,
            stale_threshold_seconds=600,
            auto_fix=True,
            reschedule_stale=False,
        )
        
        run_reconcile(config)
        
        # 验证可以通过 reason 前缀查询到补写的审计
        # 使用 LIKE 'outbox_stale%' 查询
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT audit_id, reason, evidence_refs_json
                FROM governance.write_audit
                WHERE reason LIKE 'outbox_stale%'
                  AND (evidence_refs_json->>'outbox_id')::int = %s
                """,
                (outbox_id,),
            )
            row = cur.fetchone()
            assert row is not None, "应该能通过 reason 前缀查询到补写的审计"
            
            audit_id, reason, evidence_refs_json = row
            # 验证 reason 以 outbox_stale 开头
            assert reason.startswith("outbox_stale"), \
                f"reason '{reason}' 应以 'outbox_stale' 开头"
            # 验证 evidence_refs_json 包含正确的 outbox_id
            assert evidence_refs_json.get("gateway_event", {}).get("outbox_id") == outbox_id or \
                   evidence_refs_json.get("outbox_id") == outbox_id, \
                "evidence_refs_json 应包含正确的 outbox_id"
            # 验证包含 stale 相关的 extra 信息
            extra = evidence_refs_json.get("gateway_event", {}).get("extra", {})
            assert extra.get("original_locked_by") == "worker-query-stale", \
                "应包含 original_locked_by"
    
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
        from engram.gateway.reconcile_outbox import ReconcileConfig, run_reconcile
        
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
        from engram.gateway.reconcile_outbox import ReconcileConfig, run_reconcile
        
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
        from engram.gateway.reconcile_outbox import ReconcileConfig, run_reconcile
        
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
        from engram.gateway.reconcile_outbox import ReconcileConfig, run_reconcile
        
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


class TestReconcileReasonErrorCodeContract:
    """
    测试 reconcile_outbox.py 使用的 reason/errorcode 与 engram_logbook.errors 常量的一致性
    
    确保所有 reconcile 模块写入审计的 reason 都使用 ErrorCode 枚举常量，
    而非硬编码字符串，保证 Logbook 和 Gateway 的契约一致。
    """
    
    def test_reconcile_uses_errorcode_constants_for_sent(self, db_conn_for_reconcile, reconcile_env):
        """验证 sent 对账使用 ErrorCode.OUTBOX_FLUSH_SUCCESS 常量"""
        from engram.logbook.errors import ErrorCode
        from engram.gateway.reconcile_outbox import reconcile_sent_records, ReconcileConfig, ReconcileResult
        
        # 验证常量值与期望字符串一致
        assert ErrorCode.OUTBOX_FLUSH_SUCCESS == "outbox_flush_success", \
            f"ErrorCode.OUTBOX_FLUSH_SUCCESS 应为 'outbox_flush_success'，实际: {ErrorCode.OUTBOX_FLUSH_SUCCESS}"
        assert ErrorCode.OUTBOX_FLUSH_DEDUP_HIT == "outbox_flush_dedup_hit", \
            f"ErrorCode.OUTBOX_FLUSH_DEDUP_HIT 应为 'outbox_flush_dedup_hit'，实际: {ErrorCode.OUTBOX_FLUSH_DEDUP_HIT}"
    
    def test_reconcile_uses_errorcode_constants_for_dead(self, db_conn_for_reconcile, reconcile_env):
        """验证 dead 对账使用 ErrorCode.OUTBOX_FLUSH_DEAD 常量"""
        from engram.logbook.errors import ErrorCode
        
        # 验证常量值与期望字符串一致
        assert ErrorCode.OUTBOX_FLUSH_DEAD == "outbox_flush_dead", \
            f"ErrorCode.OUTBOX_FLUSH_DEAD 应为 'outbox_flush_dead'，实际: {ErrorCode.OUTBOX_FLUSH_DEAD}"
    
    def test_reconcile_uses_errorcode_constants_for_stale(self, db_conn_for_reconcile, reconcile_env):
        """验证 stale 对账使用 ErrorCode.OUTBOX_STALE 常量"""
        from engram.logbook.errors import ErrorCode
        
        # 验证常量值与期望字符串一致
        assert ErrorCode.OUTBOX_STALE == "outbox_stale", \
            f"ErrorCode.OUTBOX_STALE 应为 'outbox_stale'，实际: {ErrorCode.OUTBOX_STALE}"
    
    def test_reconcile_errorcode_import_consistency(self):
        """
        验证 reconcile_outbox 模块正确导入并使用 ErrorCode
        
        通过检查模块代码确保使用的是 ErrorCode 枚举而非硬编码字符串
        """
        import inspect
        from engram.gateway import reconcile_outbox
        from engram.logbook.errors import ErrorCode
        
        # 获取 reconcile_outbox 模块的源代码
        source = inspect.getsource(reconcile_outbox)
        
        # 验证使用 ErrorCode 常量而非硬编码字符串
        # 1. 检查 sent 相关
        assert "ErrorCode.OUTBOX_FLUSH_SUCCESS" in source, \
            "reconcile_outbox 应使用 ErrorCode.OUTBOX_FLUSH_SUCCESS 而非硬编码"
        assert "ErrorCode.OUTBOX_FLUSH_DEDUP_HIT" in source, \
            "reconcile_outbox 应使用 ErrorCode.OUTBOX_FLUSH_DEDUP_HIT 而非硬编码"
        
        # 2. 检查 dead 相关
        assert "ErrorCode.OUTBOX_FLUSH_DEAD" in source, \
            "reconcile_outbox 应使用 ErrorCode.OUTBOX_FLUSH_DEAD 而非硬编码"
        
        # 3. 检查 stale 相关
        assert "ErrorCode.OUTBOX_STALE" in source, \
            "reconcile_outbox 应使用 ErrorCode.OUTBOX_STALE 而非硬编码"
    
    def test_all_outbox_errorcodes_defined_in_logbook(self):
        """
        验证所有 outbox 相关的 ErrorCode 都在 engram_logbook.errors 中定义
        
        这是契约校验的核心：确保 Gateway 使用的错误码与 Logbook 定义一致
        """
        from engram.logbook.errors import ErrorCode
        
        # 定义所有期望存在的 outbox 相关 ErrorCode
        expected_outbox_errorcodes = [
            "OUTBOX_FLUSH_SUCCESS",
            "OUTBOX_FLUSH_RETRY",
            "OUTBOX_FLUSH_DEAD",
            "OUTBOX_FLUSH_CONFLICT",
            "OUTBOX_FLUSH_DEDUP_HIT",
            "OUTBOX_FLUSH_DB_TIMEOUT",
            "OUTBOX_FLUSH_DB_ERROR",
            "OUTBOX_STALE",
        ]
        
        for code_name in expected_outbox_errorcodes:
            assert hasattr(ErrorCode, code_name), \
                f"ErrorCode 应定义 {code_name}，但未找到"
            
            code_value = getattr(ErrorCode, code_name)
            assert isinstance(code_value, str), \
                f"ErrorCode.{code_name} 应为字符串，实际类型: {type(code_value)}"
            
            # 验证命名规范：以 outbox_ 开头
            assert code_value.startswith("outbox_"), \
                f"ErrorCode.{code_name} 的值 '{code_value}' 应以 'outbox_' 开头"
    
    def test_reconcile_audit_reason_matches_outbox_status(self, db_conn_for_reconcile, reconcile_env):
        """
        验证对账后审计 reason 与 outbox 状态的对应关系：
        - sent -> outbox_flush_success 或 outbox_flush_dedup_hit
        - dead -> outbox_flush_dead
        - pending(stale) -> outbox_stale
        """
        from engram.logbook.errors import ErrorCode
        
        # 定义状态到期望 reason 的映射
        status_to_reason_mapping = {
            "sent": [ErrorCode.OUTBOX_FLUSH_SUCCESS, ErrorCode.OUTBOX_FLUSH_DEDUP_HIT],
            "dead": [ErrorCode.OUTBOX_FLUSH_DEAD],
            "stale": [ErrorCode.OUTBOX_STALE],  # pending 且 locked 过期
        }
        
        # 验证映射中的值都是有效的 ErrorCode 常量
        for status, reasons in status_to_reason_mapping.items():
            for reason in reasons:
                assert isinstance(reason, str), \
                    f"状态 '{status}' 的 reason '{reason}' 应为字符串"
                assert reason.startswith("outbox_"), \
                    f"状态 '{status}' 的 reason '{reason}' 应以 'outbox_' 开头"


class TestReconcileSmokeTest:
    """
    Reconcile 冒烟测试
    
    在 FULL profile 下执行命令行对账，验证：
    1. 命令正常退出（退出码 0 或 1）
    2. 摘要格式符合契约
    
    跳过条件：HTTP_ONLY_MODE=1 时跳过
    """
    
    @pytest.fixture
    def is_full_profile(self):
        """检查是否为 FULL profile"""
        http_only = os.environ.get("HTTP_ONLY_MODE", "0")
        return http_only != "1"
    
    def test_reconcile_cli_once_exit_code(self, reconcile_env, is_full_profile):
        """
        冒烟测试：执行 --once 并验证退出码
        
        退出码契约：
        - 0: 成功（所有缺失审计已修复）
        - 1: 部分失败（存在未修复的缺失审计）
        - 2: 执行错误（不应出现）
        """
        if not is_full_profile:
            pytest.skip("FULL profile required for smoke test")
        
        import subprocess
        
        # 执行 reconcile --once
        result = subprocess.run(
            [
                sys.executable, "-m", "engram.gateway.reconcile_outbox",
                "--once",
                "--scan-window", "1",  # 仅扫描最近 1 小时
                "--batch-size", "10",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=os.path.dirname(os.path.dirname(__file__)),  # gateway 目录
        )
        
        # 验证退出码（0 或 1 都是正常，2 是异常）
        assert result.returncode in (0, 1), \
            f"退出码应为 0 或 1，实际: {result.returncode}\nstderr: {result.stderr}"
    
    def test_reconcile_cli_summary_format(self, reconcile_env, is_full_profile):
        """
        冒烟测试：验证摘要格式符合契约
        
        摘要格式契约：
        - 包含 "Outbox Reconcile Report"
        - 包含 "Total scanned:"
        - 包含 "sent:", "dead:", "stale:" 统计行
        """
        if not is_full_profile:
            pytest.skip("FULL profile required for smoke test")
        
        import subprocess
        
        # 执行 reconcile --once
        result = subprocess.run(
            [
                sys.executable, "-m", "engram.gateway.reconcile_outbox",
                "--once",
                "--scan-window", "1",
                "--batch-size", "10",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=os.path.dirname(os.path.dirname(__file__)),
        )
        
        stdout = result.stdout
        
        # 验证摘要格式
        assert "Outbox Reconcile Report" in stdout, \
            f"摘要应包含 'Outbox Reconcile Report'\nstdout: {stdout}"
        
        assert "Total scanned:" in stdout, \
            f"摘要应包含 'Total scanned:'\nstdout: {stdout}"
        
        assert "sent:" in stdout, \
            f"摘要应包含 'sent:' 统计\nstdout: {stdout}"
        
        assert "dead:" in stdout, \
            f"摘要应包含 'dead:' 统计\nstdout: {stdout}"
        
        assert "stale:" in stdout, \
            f"摘要应包含 'stale:' 统计\nstdout: {stdout}"
    
    def test_reconcile_cli_report_mode_no_fix(self, reconcile_env, is_full_profile):
        """
        冒烟测试：--report 模式不修复
        
        验证 --report 等同于 --once --no-auto-fix
        """
        if not is_full_profile:
            pytest.skip("FULL profile required for smoke test")
        
        import subprocess
        
        # 执行 reconcile --report
        result = subprocess.run(
            [
                sys.executable, "-m", "engram.gateway.reconcile_outbox",
                "--report",
                "--scan-window", "1",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=os.path.dirname(os.path.dirname(__file__)),
        )
        
        # --report 模式退出码可能是 0（无缺失）或 1（有缺失但未修复）
        assert result.returncode in (0, 1), \
            f"--report 退出码应为 0 或 1，实际: {result.returncode}\nstderr: {result.stderr}"
        
        # 验证摘要存在
        assert "Outbox Reconcile Report" in result.stdout, \
            f"--report 应输出摘要\nstdout: {result.stdout}"


class TestReconcileEdgeCases:
    """测试边界情况"""
    
    def test_empty_database(self, db_conn_for_reconcile, reconcile_env):
        """测试空数据库的对账"""
        # 不插入任何数据
        
        from engram.gateway.reconcile_outbox import ReconcileConfig, run_reconcile
        
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
        from engram.gateway.reconcile_outbox import ReconcileConfig, run_reconcile
        
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


# ============================================================================
# 集成测试: redirect → outbox_enqueue → outbox_worker flush → reconcile 闭环
# ============================================================================

class TestAuditOutboxInvariants:
    """
    集成测试：审计/Outbox 不变量校验
    
    测试 redirect → outbox_enqueue → outbox_worker flush → reconcile 的最小闭环，
    验证以下不变量：
    
    1. 审计 reason/action 与 outbox 状态映射一致
       - sent → outbox_flush_success/outbox_flush_dedup_hit (action=allow)
       - dead → outbox_flush_dead (action=reject)
       - pending(stale) → outbox_stale (action=redirect)
    
    2. evidence_refs_json 顶层字段满足对账 SQL 查询需求
       - outbox_id 必须在顶层（用于 evidence_refs_json->>'outbox_id' 查询）
       - source 必须在顶层（用于来源追踪）
       - memory_id 必须在顶层（如果存在）
    
    引用文档: docs/gateway/06_gateway_design.md
    """
    
    def test_redirect_to_outbox_enqueue_audit_invariant(
        self, db_conn_for_reconcile, reconcile_env
    ):
        """
        不变量测试：redirect 决策后入队 outbox，审计 action 应为 redirect
        
        验证场景：
        1. 模拟 redirect 决策（team_write_disabled）
        2. 入队 outbox_memory（status=pending）
        3. 验证审计记录的 action=redirect
        """
        conn = db_conn_for_reconcile
        import json
        
        # 模拟 redirect 场景：team_write_disabled，写入 private 空间
        original_space = "team:test_project"
        final_space = "private:user123"
        payload_sha = f"sha256_{uuid.uuid4().hex[:32]}"
        
        # 1. 写入 redirect 审计（模拟 Gateway 的 redirect 决策）
        redirect_evidence = json.dumps({
            "gateway_event": {
                "source": "gateway",
                "operation": "memory_store",
                "decision": {"action": "redirect", "reason": "team_write_disabled"},
            },
            "source": "gateway",
        })
        
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO governance.write_audit
                    (target_space, action, reason, payload_sha, evidence_refs_json)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING audit_id
                """,
                (final_space, "redirect", "team_write_disabled", payload_sha, redirect_evidence),
            )
            redirect_audit_id = cur.fetchone()[0]
        
        # 2. 入队 outbox_memory（模拟 redirect 后的 OpenMemory 写入失败降级）
        outbox_id = insert_outbox_record(
            conn,
            target_space=final_space,
            status="pending",
            payload_sha=payload_sha,
        )
        conn.commit()
        
        # 3. 验证不变量：redirect 审计存在且 action=redirect
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT action, reason FROM governance.write_audit
                WHERE audit_id = %s
                """,
                (redirect_audit_id,),
            )
            row = cur.fetchone()
            assert row is not None, "redirect 审计记录应存在"
            assert row[0] == "redirect", f"redirect 决策的 action 应为 'redirect'，实际: {row[0]}"
            assert row[1] == "team_write_disabled", f"reason 应为 'team_write_disabled'，实际: {row[1]}"
        
        # 4. 验证 outbox 记录存在且 status=pending
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM logbook.outbox_memory WHERE outbox_id = %s",
                (outbox_id,),
            )
            row = cur.fetchone()
            assert row is not None, "outbox 记录应存在"
            assert row[0] == "pending", f"outbox status 应为 'pending'，实际: {row[0]}"
    
    def test_outbox_flush_success_audit_invariant(
        self, db_conn_for_reconcile, reconcile_env
    ):
        """
        不变量测试：outbox flush 成功后，审计 reason/action 与 outbox 状态映射
        
        验证场景：
        1. 创建 sent 状态的 outbox 记录
        2. 执行 reconcile 补写审计
        3. 验证审计 reason=outbox_flush_success, action=allow
        4. 验证 evidence_refs_json 顶层字段契约
        """
        conn = db_conn_for_reconcile
        from engram.logbook.errors import ErrorCode
        from engram.gateway.reconcile_outbox import ReconcileConfig, run_reconcile
        
        # 1. 创建 sent 状态的 outbox 记录（模拟 outbox_worker flush 成功）
        payload_sha = f"sha256_{uuid.uuid4().hex[:32]}"
        outbox_id = insert_outbox_record(
            conn,
            target_space="private:user_test_flush",
            status="sent",
            payload_sha=payload_sha,
            last_error="memory_id=mem_success_test",
        )
        conn.commit()
        
        # 2. 执行 reconcile 补写审计
        config = ReconcileConfig(scan_window_hours=24, auto_fix=True)
        run_reconcile(config)
        
        # 3. 查询补写的审计记录
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT action, reason, evidence_refs_json
                FROM governance.write_audit
                WHERE (evidence_refs_json->>'outbox_id')::int = %s
                  AND reason = %s
                """,
                (outbox_id, ErrorCode.OUTBOX_FLUSH_SUCCESS),
            )
            row = cur.fetchone()
        
        # 4. 验证审计 reason/action 不变量
        assert row is not None, "sent 状态应有 outbox_flush_success 审计"
        action, reason, evidence_refs_json = row
        
        assert action == "allow", \
            f"sent → 审计 action 应为 'allow'，实际: {action}"
        assert reason == ErrorCode.OUTBOX_FLUSH_SUCCESS, \
            f"sent → 审计 reason 应为 '{ErrorCode.OUTBOX_FLUSH_SUCCESS}'，实际: {reason}"
        
        # 5. 验证 evidence_refs_json 顶层字段契约（用于 SQL 查询）
        assert "outbox_id" in evidence_refs_json, \
            "evidence_refs_json 顶层必须包含 outbox_id（SQL 查询契约）"
        assert evidence_refs_json["outbox_id"] == outbox_id, \
            f"顶层 outbox_id 值应为 {outbox_id}，实际: {evidence_refs_json['outbox_id']}"
        
        assert "source" in evidence_refs_json, \
            "evidence_refs_json 顶层必须包含 source"
        assert evidence_refs_json["source"] == "reconcile_outbox", \
            f"source 应为 'reconcile_outbox'，实际: {evidence_refs_json['source']}"
        
        # 6. 验证 gateway_event 子结构完整性
        assert "gateway_event" in evidence_refs_json, \
            "evidence_refs_json 必须包含 gateway_event 子结构"
        gateway_event = evidence_refs_json["gateway_event"]
        assert gateway_event.get("outbox_id") == outbox_id, \
            "gateway_event.outbox_id 应与顶层一致"
    
    def test_outbox_flush_dead_audit_invariant(
        self, db_conn_for_reconcile, reconcile_env
    ):
        """
        不变量测试：outbox flush 失败（dead）后，审计 reason/action 与 outbox 状态映射
        
        验证场景：
        1. 创建 dead 状态的 outbox 记录
        2. 执行 reconcile 补写审计
        3. 验证审计 reason=outbox_flush_dead, action=reject
        4. 验证 evidence_refs_json 顶层字段契约
        """
        conn = db_conn_for_reconcile
        from engram.logbook.errors import ErrorCode
        from engram.gateway.reconcile_outbox import ReconcileConfig, run_reconcile
        
        # 1. 创建 dead 状态的 outbox 记录（模拟 outbox_worker 重试耗尽）
        payload_sha = f"sha256_{uuid.uuid4().hex[:32]}"
        outbox_id = insert_outbox_record(
            conn,
            target_space="private:user_test_dead",
            status="dead",
            payload_sha=payload_sha,
            last_error="max_retries_exceeded: connection_timeout",
            retry_count=5,
        )
        conn.commit()
        
        # 2. 执行 reconcile 补写审计
        config = ReconcileConfig(scan_window_hours=24, auto_fix=True)
        run_reconcile(config)
        
        # 3. 查询补写的审计记录
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT action, reason, evidence_refs_json
                FROM governance.write_audit
                WHERE (evidence_refs_json->>'outbox_id')::int = %s
                  AND reason = %s
                """,
                (outbox_id, ErrorCode.OUTBOX_FLUSH_DEAD),
            )
            row = cur.fetchone()
        
        # 4. 验证审计 reason/action 不变量
        assert row is not None, "dead 状态应有 outbox_flush_dead 审计"
        action, reason, evidence_refs_json = row
        
        assert action == "reject", \
            f"dead → 审计 action 应为 'reject'，实际: {action}"
        assert reason == ErrorCode.OUTBOX_FLUSH_DEAD, \
            f"dead → 审计 reason 应为 '{ErrorCode.OUTBOX_FLUSH_DEAD}'，实际: {reason}"
        
        # 5. 验证 evidence_refs_json 顶层字段契约
        assert "outbox_id" in evidence_refs_json, \
            "evidence_refs_json 顶层必须包含 outbox_id"
        assert evidence_refs_json["outbox_id"] == outbox_id
        
        assert "source" in evidence_refs_json
        assert evidence_refs_json["source"] == "reconcile_outbox"
        
        # 6. 验证 extra 包含 last_error（用于故障排查）
        assert "extra" in evidence_refs_json, \
            "evidence_refs_json 应包含 extra 字段"
        assert "last_error" in evidence_refs_json["extra"], \
            "extra 应包含 last_error 字段"
    
    def test_outbox_stale_audit_invariant(
        self, db_conn_for_reconcile, reconcile_env
    ):
        """
        不变量测试：pending 且 stale 的 outbox，审计 reason/action 映射
        
        验证场景：
        1. 创建 pending + locked_at 过期的 outbox 记录
        2. 执行 reconcile 检测 stale 并补写审计
        3. 验证审计 reason=outbox_stale, action=redirect
        4. 验证 evidence_refs_json 顶层字段契约
        """
        conn = db_conn_for_reconcile
        from engram.logbook.errors import ErrorCode
        from engram.gateway.reconcile_outbox import ReconcileConfig, run_reconcile
        
        # 1. 创建 stale 的 pending 记录
        stale_time = datetime.now(timezone.utc) - timedelta(minutes=20)
        payload_sha = f"sha256_{uuid.uuid4().hex[:32]}"
        outbox_id = insert_outbox_record(
            conn,
            target_space="private:user_test_stale",
            status="pending",
            payload_sha=payload_sha,
            locked_at=stale_time,
            locked_by="worker-stale-test",
        )
        conn.commit()
        
        # 2. 执行 reconcile（stale 阈值 10 分钟）
        config = ReconcileConfig(
            scan_window_hours=24,
            stale_threshold_seconds=600,
            auto_fix=True,
            reschedule_stale=False,  # 只测试审计，不重新调度
        )
        run_reconcile(config)
        
        # 3. 查询补写的审计记录
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT action, reason, evidence_refs_json
                FROM governance.write_audit
                WHERE (evidence_refs_json->>'outbox_id')::int = %s
                  AND reason = %s
                """,
                (outbox_id, ErrorCode.OUTBOX_STALE),
            )
            row = cur.fetchone()
        
        # 4. 验证审计 reason/action 不变量
        assert row is not None, "stale 状态应有 outbox_stale 审计"
        action, reason, evidence_refs_json = row
        
        assert action == "redirect", \
            f"stale → 审计 action 应为 'redirect'，实际: {action}"
        assert reason == ErrorCode.OUTBOX_STALE, \
            f"stale → 审计 reason 应为 '{ErrorCode.OUTBOX_STALE}'，实际: {reason}"
        
        # 5. 验证 evidence_refs_json 顶层字段契约
        assert "outbox_id" in evidence_refs_json, \
            "evidence_refs_json 顶层必须包含 outbox_id"
        assert evidence_refs_json["outbox_id"] == outbox_id
        
        assert "source" in evidence_refs_json
        assert evidence_refs_json["source"] == "reconcile_outbox"
        
        # 6. 验证 extra 包含 original_locked_by（用于 stale 诊断）
        assert "extra" in evidence_refs_json
        assert evidence_refs_json["extra"].get("original_locked_by") == "worker-stale-test", \
            "extra 应包含 original_locked_by 用于 stale 诊断"
    
    def test_full_redirect_outbox_reconcile_loop(
        self, db_conn_for_reconcile, reconcile_env
    ):
        """
        完整闭环测试：redirect → outbox_enqueue → status 变更 → reconcile 审计补写
        
        此测试验证从 redirect 决策到 reconcile 对账的完整数据流：
        
        1. redirect 决策 → 写入 redirect 审计
        2. OpenMemory 写入失败 → 入队 outbox (pending)
        3. outbox_worker flush 成功 → 状态变更为 sent
        4. reconcile 检测缺失 → 补写 outbox_flush_success 审计
        5. 验证所有审计记录的 reason/action 与状态映射一致
        
        不变量（文档引用: docs/gateway/06_gateway_design.md）:
        - audit.count(action=redirect) 包含 redirect 和 stale 重试
        - sent 状态 → outbox_flush_success 审计
        - evidence_refs_json->>'outbox_id' 必须可查询
        """
        conn = db_conn_for_reconcile
        import json
        from engram.logbook.errors import ErrorCode
        from engram.gateway.reconcile_outbox import ReconcileConfig, run_reconcile
        
        # ========== Phase 1: redirect 决策 ==========
        original_space = "team:production"
        final_space = "private:user_loop_test"
        payload_sha = f"sha256_{uuid.uuid4().hex[:32]}"
        
        # 写入 redirect 审计
        redirect_evidence = json.dumps({
            "gateway_event": {
                "source": "gateway",
                "operation": "memory_store",
                "requested_space": original_space,
                "final_space": final_space,
                "decision": {"action": "redirect", "reason": "team_write_disabled"},
            },
            "source": "gateway",
        })
        
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO governance.write_audit
                    (target_space, action, reason, payload_sha, evidence_refs_json)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING audit_id
                """,
                (final_space, "redirect", "team_write_disabled", payload_sha, redirect_evidence),
            )
            redirect_audit_id = cur.fetchone()[0]
        
        # ========== Phase 2: 入队 outbox (pending) ==========
        outbox_id = insert_outbox_record(
            conn,
            target_space=final_space,
            status="pending",
            payload_sha=payload_sha,
        )
        conn.commit()
        
        # ========== Phase 3: 模拟 flush 成功 → sent ==========
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE logbook.outbox_memory
                SET status = 'sent', last_error = 'memory_id=mem_loop_success'
                WHERE outbox_id = %s
                """,
                (outbox_id,),
            )
        conn.commit()
        
        # ========== Phase 4: reconcile 补写审计 ==========
        config = ReconcileConfig(scan_window_hours=24, auto_fix=True)
        result = run_reconcile(config)
        
        # 验证 reconcile 检测到并修复了缺失审计
        assert result.sent_missing_audit >= 1, \
            f"应检测到至少 1 条 sent 缺失审计，实际: {result.sent_missing_audit}"
        assert result.sent_audit_fixed >= 1, \
            f"应修复至少 1 条 sent 审计，实际: {result.sent_audit_fixed}"
        
        # ========== Phase 5: 验证审计链完整性 ==========
        
        # 5.1 验证 redirect 审计
        with conn.cursor() as cur:
            cur.execute(
                "SELECT action, reason FROM governance.write_audit WHERE audit_id = %s",
                (redirect_audit_id,),
            )
            row = cur.fetchone()
            assert row[0] == "redirect", "redirect 审计 action 应为 'redirect'"
            assert row[1] == "team_write_disabled", "redirect 审计 reason 应为 'team_write_disabled'"
        
        # 5.2 验证 outbox_flush_success 审计
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT action, reason, evidence_refs_json
                FROM governance.write_audit
                WHERE (evidence_refs_json->>'outbox_id')::int = %s
                  AND reason = %s
                """,
                (outbox_id, ErrorCode.OUTBOX_FLUSH_SUCCESS),
            )
            row = cur.fetchone()
            assert row is not None, "sent 状态应有 outbox_flush_success 审计"
            
            action, reason, evidence_refs_json = row
            
            # 验证 reason/action 映射不变量
            assert action == "allow", \
                f"sent → action 应为 'allow'，实际: {action}"
            assert reason == ErrorCode.OUTBOX_FLUSH_SUCCESS, \
                f"sent → reason 应为 '{ErrorCode.OUTBOX_FLUSH_SUCCESS}'，实际: {reason}"
            
            # 验证 evidence_refs_json 顶层字段契约
            assert evidence_refs_json.get("outbox_id") == outbox_id, \
                "顶层 outbox_id 应与 outbox 记录匹配"
            assert evidence_refs_json.get("source") == "reconcile_outbox", \
                "source 应为 reconcile_outbox"
    
    def test_evidence_refs_json_sql_query_contract(
        self, db_conn_for_reconcile, reconcile_env
    ):
        """
        SQL 查询契约测试：验证 reconcile 使用的 SQL 查询能正确找到审计记录
        
        此测试验证 evidence_refs_json->>'outbox_id' 查询的可靠性，
        确保 reconcile_outbox.py 中的 SQL 查询契约得到满足。
        
        查询契约（来源: reconcile_outbox.py:get_audit_by_outbox_id）:
            WHERE (evidence_refs_json->>'outbox_id')::int = %s
        
        这要求 outbox_id 必须在 evidence_refs_json 顶层。
        """
        conn = db_conn_for_reconcile
        from engram.logbook.errors import ErrorCode
        from engram.gateway.reconcile_outbox import ReconcileConfig, run_reconcile, check_audit_exists
        
        # 创建 sent 记录并触发 reconcile 补写
        payload_sha = f"sha256_{uuid.uuid4().hex[:32]}"
        outbox_id = insert_outbox_record(
            conn,
            target_space="private:user_sql_contract",
            status="sent",
            payload_sha=payload_sha,
        )
        conn.commit()
        
        # 执行 reconcile
        config = ReconcileConfig(scan_window_hours=24, auto_fix=True)
        run_reconcile(config)
        
        # 使用 reconcile 的 SQL 查询验证审计存在
        exists = check_audit_exists(conn, outbox_id, ErrorCode.OUTBOX_FLUSH_SUCCESS)
        assert exists, \
            f"check_audit_exists 应能通过 evidence_refs_json->>'outbox_id' 找到审计记录"
        
        # 直接执行 SQL 验证查询契约
        with conn.cursor() as cur:
            # 这是 reconcile_outbox.py 使用的确切查询模式
            cur.execute(
                """
                SELECT COUNT(*)
                FROM governance.write_audit
                WHERE reason LIKE %s
                  AND (evidence_refs_json->>'outbox_id')::int = %s
                """,
                (f"{ErrorCode.OUTBOX_FLUSH_SUCCESS}%", outbox_id),
            )
            count = cur.fetchone()[0]
            assert count >= 1, \
                f"SQL 查询契约: evidence_refs_json->>'outbox_id' 应能匹配审计记录"
