"""
reconcile_outbox - Outbox 与 Audit 数据一致性对账模块

按时间窗口扫描 logbook.outbox_memory 与 governance.write_audit，
检测并修复数据不一致情况：

1. status=sent 且缺少 outbox_flush_success 审计 → 补写审计
2. status=dead 且缺少 outbox_flush_dead 审计 → 补写审计
3. status=pending 且 locked 已过期（stale）→ 写 outbox_stale 审计并可选重新调度

用法：
    python -m gateway.reconcile_outbox --once    # 执行一轮对账
    python -m gateway.reconcile_outbox --report  # 仅报告不修复
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg

from . import logbook_adapter
from .audit_event import build_reconcile_audit_event, build_evidence_refs_json

# 导入统一错误码
try:
    from engram.logbook.errors import ErrorCode
except ImportError:
    import sys
    print(
        "\n"
        "=" * 60 + "\n"
        "[ERROR] 缺少依赖: engram_logbook\n"
        "=" * 60 + "\n"
        "\n"
        "请先安装:\n"
        "  pip install -e apps/logbook_postgres/scripts\n"
        "\n"
        "=" * 60 + "\n"
    )
    sys.exit(1)

logger = logging.getLogger(__name__)


# ---------- 配置 ----------

@dataclass
class ReconcileConfig:
    """对账配置"""
    # 时间窗口：扫描最近 N 小时内更新的记录
    scan_window_hours: int = 24
    # 批量处理大小
    batch_size: int = 100
    # 租约过期阈值（秒）：locked_at 超过此时间视为 stale
    stale_threshold_seconds: int = 600  # 10 分钟
    # 是否自动修复缺失的审计记录
    auto_fix: bool = True
    # 是否对 stale 记录重新调度
    reschedule_stale: bool = True
    # 重新调度的延迟秒数
    reschedule_delay_seconds: int = 0


@dataclass
class ReconcileResult:
    """对账结果"""
    # 扫描统计
    total_scanned: int = 0
    sent_count: int = 0
    dead_count: int = 0
    stale_count: int = 0
    
    # 缺失审计统计
    sent_missing_audit: int = 0
    dead_missing_audit: int = 0
    stale_missing_audit: int = 0
    
    # 修复统计
    sent_audit_fixed: int = 0
    dead_audit_fixed: int = 0
    stale_audit_fixed: int = 0
    stale_rescheduled: int = 0
    
    # 详细记录
    details: List[Dict[str, Any]] = field(default_factory=list)
    
    def summary(self) -> str:
        """生成摘要报告"""
        lines = [
            "=== Outbox Reconcile Report ===",
            f"Total scanned: {self.total_scanned}",
            f"  - sent:  {self.sent_count} (missing audit: {self.sent_missing_audit}, fixed: {self.sent_audit_fixed})",
            f"  - dead:  {self.dead_count} (missing audit: {self.dead_missing_audit}, fixed: {self.dead_audit_fixed})",
            f"  - stale: {self.stale_count} (missing audit: {self.stale_missing_audit}, fixed: {self.stale_audit_fixed}, rescheduled: {self.stale_rescheduled})",
        ]
        return "\n".join(lines)


# ---------- 数据库查询函数 ----------

def get_outbox_by_time_window(
    conn: psycopg.Connection,
    window_hours: int,
    status_filter: Optional[str] = None,
    limit: int = 1000,
) -> List[Dict[str, Any]]:
    """
    按时间窗口获取 outbox_memory 记录
    
    Args:
        conn: 数据库连接
        window_hours: 时间窗口（小时）
        status_filter: 可选状态筛选 (sent/dead/pending)
        limit: 返回记录数量上限
        
    Returns:
        outbox 记录列表
    """
    with conn.cursor() as cur:
        # 构建查询条件
        conditions = ["updated_at >= now() - make_interval(hours := %s)"]
        params: List[Any] = [window_hours]
        
        if status_filter:
            conditions.append("status = %s")
            params.append(status_filter)
        
        where_clause = " AND ".join(conditions)
        params.append(limit)
        
        cur.execute(
            f"""
            SELECT outbox_id, item_id, target_space, payload_md, payload_sha,
                   status, retry_count, next_attempt_at, locked_at, locked_by,
                   last_error, created_at, updated_at
            FROM logbook.outbox_memory
            WHERE {where_clause}
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            params,
        )
        
        rows = cur.fetchall()
        results = []
        for row in rows:
            results.append({
                "outbox_id": row[0],
                "item_id": row[1],
                "target_space": row[2],
                "payload_md": row[3],
                "payload_sha": row[4],
                "status": row[5],
                "retry_count": row[6],
                "next_attempt_at": row[7],
                "locked_at": row[8],
                "locked_by": row[9],
                "last_error": row[10],
                "created_at": row[11],
                "updated_at": row[12],
            })
        return results


def get_audit_by_outbox_id(
    conn: psycopg.Connection,
    outbox_id: int,
    reason_prefix: str,
) -> List[Dict[str, Any]]:
    """
    查询指定 outbox_id 对应的审计记录
    
    Args:
        conn: 数据库连接
        outbox_id: Outbox 记录 ID
        reason_prefix: reason 前缀（如 outbox_flush_success, outbox_flush_dead）
        
    Returns:
        匹配的审计记录列表
    """
    with conn.cursor() as cur:
        # 使用 JSON 路径查询 evidence_refs_json -> outbox_id
        cur.execute(
            """
            SELECT audit_id, actor_user_id, target_space, action, reason,
                   payload_sha, evidence_refs_json, created_at
            FROM governance.write_audit
            WHERE reason LIKE %s
              AND (evidence_refs_json->>'outbox_id')::int = %s
            ORDER BY created_at DESC
            """,
            (f"{reason_prefix}%", outbox_id),
        )
        
        rows = cur.fetchall()
        return [
            {
                "audit_id": row[0],
                "actor_user_id": row[1],
                "target_space": row[2],
                "action": row[3],
                "reason": row[4],
                "payload_sha": row[5],
                "evidence_refs_json": row[6],
                "created_at": row[7],
            }
            for row in rows
        ]


def check_audit_exists(
    conn: psycopg.Connection,
    outbox_id: int,
    reason_prefix: str,
) -> bool:
    """
    检查指定 outbox_id 是否存在对应的审计记录
    
    Args:
        conn: 数据库连接
        outbox_id: Outbox 记录 ID
        reason_prefix: reason 前缀
        
    Returns:
        True 如果存在审计记录
    """
    audits = get_audit_by_outbox_id(conn, outbox_id, reason_prefix)
    return len(audits) > 0


def update_outbox_next_attempt(
    conn: psycopg.Connection,
    outbox_id: int,
    next_attempt_at: datetime,
) -> bool:
    """
    更新 outbox 记录的 next_attempt_at（重新调度）
    
    同时清除 locked_at 和 locked_by 以释放锁
    
    Args:
        conn: 数据库连接
        outbox_id: Outbox 记录 ID
        next_attempt_at: 下次尝试时间
        
    Returns:
        True 如果更新成功
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE logbook.outbox_memory
            SET next_attempt_at = %s,
                locked_at = NULL,
                locked_by = NULL,
                updated_at = now()
            WHERE outbox_id = %s
              AND status = 'pending'
            RETURNING outbox_id
            """,
            (next_attempt_at, outbox_id),
        )
        result = cur.fetchone()
        return result is not None


# ---------- 补写审计函数 ----------

def write_reconcile_audit(
    outbox: Dict[str, Any],
    reason: str,
    action: str,
    extra_evidence: Optional[Dict] = None,
) -> int:
    """
    写入对账补救的审计记录
    
    使用 build_reconcile_audit_event() 构建统一的审计事件结构，
    使用 build_evidence_refs_json() 生成 Logbook 兼容的 evidence_refs_json。
    
    Args:
        outbox: Outbox 记录字典
        reason: 审计原因，必须使用 ErrorCode 枚举值:
                - ErrorCode.OUTBOX_FLUSH_SUCCESS
                - ErrorCode.OUTBOX_FLUSH_DEAD
                - ErrorCode.OUTBOX_STALE
        action: 审计动作（allow/reject/redirect）
        extra_evidence: 额外的证据信息
        
    Returns:
        创建的 audit_id
    """
    # 解析 user_id
    user_id = None
    target_space = outbox["target_space"]
    if target_space.startswith("private:"):
        user_id = target_space[8:]
    
    # 从 last_error 中提取 memory_id（如果有）
    memory_id = None
    last_error = outbox.get("last_error")
    if last_error and last_error.startswith("memory_id="):
        memory_id = last_error.split("=", 1)[1]
    
    # 构建 extra 信息（包含 original_locked_at/locked_by/reconcile_time 等）
    reconcile_time = datetime.now(timezone.utc).isoformat()
    extra: Dict[str, Any] = {
        "reconcile_time": reconcile_time,
    }
    
    if outbox.get("locked_by"):
        extra["original_locked_by"] = outbox["locked_by"]
    
    if outbox.get("locked_at"):
        extra["original_locked_at"] = (
            outbox["locked_at"].isoformat()
            if isinstance(outbox["locked_at"], datetime)
            else str(outbox["locked_at"])
        )
    
    if extra_evidence:
        extra.update(extra_evidence)
    
    # 使用 build_reconcile_audit_event 构建统一审计事件
    gateway_event = build_reconcile_audit_event(
        operation="outbox_reconcile",
        actor_user_id=user_id,
        target_space=target_space,
        action=action,
        reason=reason,
        payload_sha=outbox["payload_sha"],
        outbox_id=outbox["outbox_id"],
        memory_id=memory_id,
        retry_count=outbox.get("retry_count"),
        original_locked_by=outbox.get("locked_by"),
        original_locked_at=(
            outbox["locked_at"].isoformat()
            if isinstance(outbox.get("locked_at"), datetime)
            else str(outbox.get("locked_at")) if outbox.get("locked_at") else None
        ),
        extra=extra,
    )
    
    # 使用 build_evidence_refs_json 生成 Logbook 兼容的结构
    # reconcile 场景通常没有 evidence 列表，传入 None
    evidence_refs_json = build_evidence_refs_json(
        evidence=None,
        gateway_event=gateway_event,
    )
    
    return logbook_adapter.insert_write_audit(
        actor_user_id=user_id,
        target_space=target_space,
        action=action,
        reason=reason,
        payload_sha=outbox["payload_sha"],
        evidence_refs_json=evidence_refs_json,
    )


# ---------- 核心对账逻辑 ----------

def reconcile_sent_records(
    conn: psycopg.Connection,
    config: ReconcileConfig,
    result: ReconcileResult,
) -> None:
    """
    对账 status=sent 的记录
    
    检查是否缺少 outbox_flush_success 审计，如缺失则补写
    """
    records = get_outbox_by_time_window(
        conn,
        window_hours=config.scan_window_hours,
        status_filter="sent",
        limit=config.batch_size,
    )
    
    result.sent_count = len(records)
    
    for record in records:
        outbox_id = record["outbox_id"]
        
        # 检查是否存在 outbox_flush_success 或 outbox_flush_dedup_hit 审计
        # 使用 ErrorCode 枚举确保与补写 reason 一致
        has_success = check_audit_exists(conn, outbox_id, ErrorCode.OUTBOX_FLUSH_SUCCESS)
        has_dedup = check_audit_exists(conn, outbox_id, ErrorCode.OUTBOX_FLUSH_DEDUP_HIT)
        
        if not has_success and not has_dedup:
            result.sent_missing_audit += 1
            result.details.append({
                "outbox_id": outbox_id,
                "status": "sent",
                "issue": "missing_audit",
                "expected_reason": "outbox_flush_success",
            })
            
            if config.auto_fix:
                try:
                    audit_id = write_reconcile_audit(
                        outbox=record,
                        reason=ErrorCode.OUTBOX_FLUSH_SUCCESS,
                        action="allow",
                        extra_evidence={"reconciled": True},
                    )
                    result.sent_audit_fixed += 1
                    result.details[-1]["fixed"] = True
                    result.details[-1]["audit_id"] = audit_id
                    logger.info(f"[reconcile] 补写 sent 审计: outbox_id={outbox_id}, audit_id={audit_id}")
                except Exception as e:
                    logger.error(f"[reconcile] 补写 sent 审计失败: outbox_id={outbox_id}, error={e}")
                    result.details[-1]["fix_error"] = str(e)


def reconcile_dead_records(
    conn: psycopg.Connection,
    config: ReconcileConfig,
    result: ReconcileResult,
) -> None:
    """
    对账 status=dead 的记录
    
    检查是否缺少 outbox_flush_dead 审计，如缺失则补写
    """
    records = get_outbox_by_time_window(
        conn,
        window_hours=config.scan_window_hours,
        status_filter="dead",
        limit=config.batch_size,
    )
    
    result.dead_count = len(records)
    
    for record in records:
        outbox_id = record["outbox_id"]
        
        # 检查是否存在 outbox_flush_dead 审计
        # 使用 ErrorCode 枚举确保与补写 reason 一致
        has_dead_audit = check_audit_exists(conn, outbox_id, ErrorCode.OUTBOX_FLUSH_DEAD)
        
        if not has_dead_audit:
            result.dead_missing_audit += 1
            result.details.append({
                "outbox_id": outbox_id,
                "status": "dead",
                "issue": "missing_audit",
                "expected_reason": "outbox_flush_dead",
            })
            
            if config.auto_fix:
                try:
                    audit_id = write_reconcile_audit(
                        outbox=record,
                        reason=ErrorCode.OUTBOX_FLUSH_DEAD,
                        action="reject",
                        extra_evidence={
                            "reconciled": True,
                            "last_error": record.get("last_error"),
                        },
                    )
                    result.dead_audit_fixed += 1
                    result.details[-1]["fixed"] = True
                    result.details[-1]["audit_id"] = audit_id
                    logger.info(f"[reconcile] 补写 dead 审计: outbox_id={outbox_id}, audit_id={audit_id}")
                except Exception as e:
                    logger.error(f"[reconcile] 补写 dead 审计失败: outbox_id={outbox_id}, error={e}")
                    result.details[-1]["fix_error"] = str(e)


def reconcile_stale_records(
    conn: psycopg.Connection,
    config: ReconcileConfig,
    result: ReconcileResult,
) -> None:
    """
    对账 status=pending 且 locked 已过期的记录（stale）
    
    写入 outbox_stale 审计，可选重新调度
    """
    records = get_outbox_by_time_window(
        conn,
        window_hours=config.scan_window_hours,
        status_filter="pending",
        limit=config.batch_size,
    )
    
    now = datetime.now(timezone.utc)
    stale_threshold = timedelta(seconds=config.stale_threshold_seconds)
    
    stale_records = []
    for record in records:
        locked_at = record.get("locked_at")
        if locked_at is not None:
            # 确保 locked_at 是 timezone-aware
            if locked_at.tzinfo is None:
                locked_at = locked_at.replace(tzinfo=timezone.utc)
            
            if now - locked_at > stale_threshold:
                stale_records.append(record)
    
    result.stale_count = len(stale_records)
    
    for record in stale_records:
        outbox_id = record["outbox_id"]
        
        # 检查是否存在 outbox_stale 审计（最近24小时内）
        # 使用 ErrorCode 枚举确保与补写 reason 一致
        has_stale_audit = check_audit_exists(conn, outbox_id, ErrorCode.OUTBOX_STALE)
        
        if not has_stale_audit:
            result.stale_missing_audit += 1
            result.details.append({
                "outbox_id": outbox_id,
                "status": "pending",
                "issue": "stale_lock",
                "locked_by": record.get("locked_by"),
                "locked_at": str(record.get("locked_at")),
            })
            
            if config.auto_fix:
                try:
                    # 写入 stale 审计
                    audit_id = write_reconcile_audit(
                        outbox=record,
                        reason=ErrorCode.OUTBOX_STALE,
                        action="redirect",
                        extra_evidence={
                            "stale_threshold_seconds": config.stale_threshold_seconds,
                            "will_reschedule": config.reschedule_stale,
                        },
                    )
                    result.stale_audit_fixed += 1
                    result.details[-1]["fixed"] = True
                    result.details[-1]["audit_id"] = audit_id
                    logger.info(f"[reconcile] 补写 stale 审计: outbox_id={outbox_id}, audit_id={audit_id}")
                    
                    # 可选重新调度
                    if config.reschedule_stale:
                        next_attempt = now + timedelta(seconds=config.reschedule_delay_seconds)
                        if update_outbox_next_attempt(conn, outbox_id, next_attempt):
                            result.stale_rescheduled += 1
                            result.details[-1]["rescheduled"] = True
                            result.details[-1]["next_attempt_at"] = next_attempt.isoformat()
                            logger.info(f"[reconcile] 重新调度 stale 记录: outbox_id={outbox_id}, next_attempt={next_attempt.isoformat()}")
                        else:
                            logger.warning(f"[reconcile] 重新调度失败（状态可能已变更）: outbox_id={outbox_id}")
                    
                except Exception as e:
                    logger.error(f"[reconcile] 处理 stale 记录失败: outbox_id={outbox_id}, error={e}")
                    result.details[-1]["fix_error"] = str(e)


def run_reconcile(config: ReconcileConfig) -> ReconcileResult:
    """
    执行完整的对账流程
    
    Args:
        config: 对账配置
        
    Returns:
        ReconcileResult 对账结果
    """
    result = ReconcileResult()
    
    # 获取数据库连接
    from engram.logbook.db import get_connection
    conn = get_connection()
    
    try:
        # 设置 search_path
        with conn.cursor() as cur:
            cur.execute("SET search_path TO logbook, governance, public")
        
        # 执行各项对账
        logger.info(f"[reconcile] 开始对账，时间窗口: {config.scan_window_hours} 小时")
        
        reconcile_sent_records(conn, config, result)
        reconcile_dead_records(conn, config, result)
        reconcile_stale_records(conn, config, result)
        
        # 提交事务
        conn.commit()
        
        result.total_scanned = result.sent_count + result.dead_count + result.stale_count
        logger.info(f"[reconcile] 对账完成: {result.summary()}")
        
    except Exception as e:
        conn.rollback()
        logger.error(f"[reconcile] 对账失败: {e}")
        raise
    finally:
        conn.close()
    
    return result


# ---------- 命令行入口 ----------

def main():
    """命令行入口"""
    parser = argparse.ArgumentParser(
        description="Outbox 对账工具：检测并修复 outbox_memory 与 write_audit 的数据不一致"
    )
    
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--once",
        action="store_true",
        help="执行一轮对账"
    )
    mode_group.add_argument(
        "--report",
        action="store_true",
        help="仅报告不修复（等同于 --once --no-auto-fix）"
    )
    
    parser.add_argument(
        "--scan-window",
        type=int,
        default=24,
        help="扫描时间窗口（小时，默认: 24）"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="批量处理大小（默认: 100）"
    )
    parser.add_argument(
        "--stale-threshold",
        type=int,
        default=600,
        help="Stale 阈值（秒，默认: 600 即 10 分钟）"
    )
    parser.add_argument(
        "--no-auto-fix",
        action="store_true",
        help="不自动修复，仅检测"
    )
    parser.add_argument(
        "--no-reschedule",
        action="store_true",
        help="不重新调度 stale 记录"
    )
    parser.add_argument(
        "--reschedule-delay",
        type=int,
        default=0,
        help="重新调度延迟（秒，默认: 0 立即重试）"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="详细日志输出"
    )
    
    args = parser.parse_args()
    
    # 配置日志
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    
    # 构建配置
    auto_fix = not args.no_auto_fix
    if args.report:
        auto_fix = False
    
    config = ReconcileConfig(
        scan_window_hours=args.scan_window,
        batch_size=args.batch_size,
        stale_threshold_seconds=args.stale_threshold,
        auto_fix=auto_fix,
        reschedule_stale=not args.no_reschedule,
        reschedule_delay_seconds=args.reschedule_delay,
    )
    
    # 执行对账
    try:
        result = run_reconcile(config)
        print(result.summary())
        
        # 返回码：如果有未修复的缺失审计则返回 1
        total_missing = result.sent_missing_audit + result.dead_missing_audit + result.stale_missing_audit
        total_fixed = result.sent_audit_fixed + result.dead_audit_fixed + result.stale_audit_fixed
        if total_missing > total_fixed:
            sys.exit(1)
        else:
            sys.exit(0)
            
    except Exception as e:
        logger.exception(f"对账执行失败: {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
