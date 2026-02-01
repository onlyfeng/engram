# -*- coding: utf-8 -*-
"""
scm_sync_reaper_core - SCM 同步任务回收器核心实现

功能:
- 回收过期的 running 任务
- 回收过期的 sync_runs
- 回收过期的 locks

设计原则:
- 纯业务逻辑，不包含 argparse/打印
- CLI 入口在根目录 scm_sync_reaper.py 和 scripts/scm_sync_reaper.py

使用示例:
    from engram.logbook.scm_sync_reaper_core import (
        JobRecoveryPolicy,
        process_expired_jobs,
        process_expired_runs,
        process_expired_locks,
        run_reaper,
    )

    result = run_reaper(
        dsn=dsn,
        grace_seconds=60,
        policy=JobRecoveryPolicy.to_failed,
    )
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Iterable, Optional, TypedDict

from engram.logbook.scm_auth import redact
from engram.logbook.scm_sync_errors import (
    DEFAULT_BACKOFF_BASE,
    calculate_backoff_seconds,
    classify_last_error,
)

# ============ 类型定义 ============


class JobStats(TypedDict):
    """Job 处理统计"""

    processed: int
    to_failed: int
    to_dead: int
    to_pending: int
    errors: int


class RunStats(TypedDict):
    """Run 处理统计"""

    processed: int
    failed: int
    errors: int


class LockStats(TypedDict):
    """Lock 处理统计"""

    processed: int
    released: int
    errors: int


class ReaperResult(TypedDict):
    """Reaper 执行结果"""

    jobs: JobStats
    runs: RunStats
    locks: LockStats
    dry_run: bool


__all__ = [
    # 常量
    "DEFAULT_BACKOFF_BASE",
    "DEFAULT_GRACE_SECONDS",
    "DEFAULT_MAX_DURATION_SECONDS",
    "DEFAULT_RETRY_DELAY_SECONDS",
    "DEFAULT_MAX_REAPER_BACKOFF_SECONDS",
    # 类型
    "JobStats",
    "RunStats",
    "LockStats",
    "ReaperResult",
    # 枚举
    "JobRecoveryPolicy",
    # 函数
    "format_error",
    "mark_job_pending",
    "compute_backoff_seconds",
    "process_expired_jobs",
    "process_expired_runs",
    "process_expired_locks",
    "run_reaper",
]


# ============ 常量定义 ============

DEFAULT_GRACE_SECONDS = 60
DEFAULT_MAX_DURATION_SECONDS = 1800
DEFAULT_RETRY_DELAY_SECONDS = 60
DEFAULT_MAX_REAPER_BACKOFF_SECONDS = 1800  # 30 分钟


# ============ 枚举定义 ============


class JobRecoveryPolicy(str, Enum):
    """任务恢复策略"""

    to_failed = "to_failed"
    to_pending = "to_pending"


# ============ 辅助函数 ============


def format_error(prefix: str, category: str, last_error: Optional[str]) -> str:
    """
    格式化错误消息

    Args:
        prefix: 消息前缀
        category: 错误分类
        last_error: 上次错误信息

    Returns:
        格式化后的错误消息（已脱敏）
    """
    message = prefix
    if category:
        message = f"{message} ({category})"
    if last_error:
        message = f"{message}: {last_error}"
    return redact(message)


def mark_job_pending(conn, job_id: str) -> bool:
    """
    将任务标记为 pending 状态

    Args:
        conn: 数据库连接
        job_id: 任务 ID

    Returns:
        是否成功
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE scm.sync_jobs
            SET status = 'pending',
                locked_by = NULL,
                locked_at = NULL,
                updated_at = now()
            WHERE job_id = %s
            RETURNING job_id
            """,
            (job_id,),
        )
        return cur.fetchone() is not None


def compute_backoff_seconds(
    *,
    attempts: int,
    base_seconds: int,
    max_seconds: int,
    error_category: str,
    error_message: str,
    multiplier: float,
) -> int:
    """
    计算退避时间

    Args:
        attempts: 尝试次数
        base_seconds: 基础退避秒数
        max_seconds: 最大退避秒数
        error_category: 错误分类
        error_message: 错误消息
        multiplier: 乘数

    Returns:
        退避秒数
    """
    base = base_seconds or DEFAULT_BACKOFF_BASE
    backoff = calculate_backoff_seconds(
        attempts=max(1, attempts),
        base_seconds=base,
        max_seconds=max_seconds,
        error_category=error_category or None,
        error_message=error_message or None,
    )
    if multiplier and multiplier != 1.0:
        backoff = int(backoff * multiplier)
    return max(0, int(backoff))


# ============ 核心处理函数 ============


def process_expired_jobs(
    conn,
    expired_jobs: Iterable[Dict[str, Any]],
    *,
    policy: JobRecoveryPolicy,
    retry_delay_seconds: int,
    transient_retry_delay_multiplier: float = 1.0,
    max_reaper_backoff_seconds: int = DEFAULT_MAX_REAPER_BACKOFF_SECONDS,
    db_api=None,
) -> JobStats:
    """
    处理过期的任务

    Args:
        conn: 数据库连接
        expired_jobs: 过期任务迭代器
        policy: 恢复策略
        retry_delay_seconds: 重试延迟秒数
        transient_retry_delay_multiplier: 瞬态错误重试延迟乘数
        max_reaper_backoff_seconds: 最大退避秒数
        db_api: 数据库 API 模块（用于测试注入）

    Returns:
        处理统计字典
    """
    if db_api is None:
        from engram.logbook import scm_db as db_api

    stats: JobStats = {
        "processed": 0,
        "to_failed": 0,
        "to_dead": 0,
        "to_pending": 0,
        "errors": 0,
    }

    for job in expired_jobs:
        stats["processed"] += 1
        job_id = str(job.get("job_id"))
        attempts = int(job.get("attempts") or 0)
        max_attempts = int(job.get("max_attempts") or 0)
        last_error = job.get("last_error") or ""

        is_permanent, is_transient, category = classify_last_error(last_error)

        try:
            if is_permanent:
                message = format_error("Reaped: permanent error", category, last_error)
                if db_api.mark_job_as_dead_by_reaper(conn, job_id, error=message):
                    stats["to_dead"] += 1
                else:
                    stats["errors"] += 1
                continue

            if is_transient:
                backoff = compute_backoff_seconds(
                    attempts=attempts + 1,
                    base_seconds=retry_delay_seconds,
                    max_seconds=max_reaper_backoff_seconds,
                    error_category=category,
                    error_message=last_error,
                    multiplier=transient_retry_delay_multiplier,
                )
                message = format_error("Reaped: transient error", category, last_error)
                if db_api.mark_job_as_failed_by_reaper(
                    conn,
                    job_id,
                    error=message,
                    retry_delay_seconds=backoff,
                ):
                    stats["to_failed"] += 1
                else:
                    stats["errors"] += 1
                continue

            if max_attempts and attempts >= max_attempts:
                message = format_error("Reaped: job expired after max attempts", "", last_error)
                if db_api.mark_job_as_dead_by_reaper(conn, job_id, error=message):
                    stats["to_dead"] += 1
                else:
                    stats["errors"] += 1
                continue

            if policy == JobRecoveryPolicy.to_pending:
                if mark_job_pending(conn, job_id):
                    stats["to_pending"] += 1
                else:
                    stats["errors"] += 1
                continue

            message = format_error("Reaped: job lock expired", "", last_error)
            if db_api.mark_job_as_failed_by_reaper(
                conn,
                job_id,
                error=message,
                retry_delay_seconds=retry_delay_seconds,
            ):
                stats["to_failed"] += 1
            else:
                stats["errors"] += 1
        except Exception:
            stats["errors"] += 1

    return stats


def process_expired_runs(
    conn,
    expired_runs: Iterable[Dict[str, Any]],
    *,
    db_api=None,
) -> RunStats:
    """
    处理过期的 sync_runs

    Args:
        conn: 数据库连接
        expired_runs: 过期运行记录迭代器
        db_api: 数据库 API 模块（用于测试注入）

    Returns:
        处理统计字典
    """
    if db_api is None:
        from engram.logbook import scm_db as db_api

    stats: RunStats = {"processed": 0, "failed": 0, "errors": 0}
    for run in expired_runs:
        stats["processed"] += 1
        run_id = str(run.get("run_id"))
        error_summary = {
            "error_type": "lease_lost",
            "error_category": "timeout",
            "message": "Reaped: sync run timed out",
        }
        try:
            if db_api.mark_run_as_failed_by_reaper(conn, run_id, error_summary):
                stats["failed"] += 1
            else:
                stats["errors"] += 1
        except Exception:
            stats["errors"] += 1
    return stats


def process_expired_locks(
    conn,
    expired_locks: Iterable[Dict[str, Any]],
    *,
    db_api=None,
) -> LockStats:
    """
    处理过期的锁

    Args:
        conn: 数据库连接
        expired_locks: 过期锁迭代器
        db_api: 数据库 API 模块（用于测试注入）

    Returns:
        处理统计字典
    """
    if db_api is None:
        from engram.logbook import scm_db as db_api

    stats: LockStats = {"processed": 0, "released": 0, "errors": 0}
    for lock in expired_locks:
        stats["processed"] += 1
        lock_id = lock.get("lock_id")
        try:
            if lock_id is not None and db_api.force_release_lock(conn, int(lock_id)):
                stats["released"] += 1
            else:
                stats["errors"] += 1
        except Exception:
            stats["errors"] += 1
    return stats


# ============ 主流程函数 ============


def run_reaper(
    dsn: str,
    *,
    grace_seconds: int = DEFAULT_GRACE_SECONDS,
    max_duration_seconds: int = DEFAULT_MAX_DURATION_SECONDS,
    policy: JobRecoveryPolicy = JobRecoveryPolicy.to_failed,
    retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
    dry_run: bool = False,
    logger=None,
    db_api=None,
) -> ReaperResult:
    """
    执行 reaper 主流程

    Args:
        dsn: 数据库连接字符串
        grace_seconds: Job 过期宽限时间（秒）
        max_duration_seconds: Run 最大运行时间（秒）
        policy: Job 恢复策略
        retry_delay_seconds: Job 失败后重试延迟（秒）
        dry_run: 是否模拟运行
        logger: 日志记录器
        db_api: 数据库 API 模块（用于测试注入）

    Returns:
        包含统计信息的字典
    """
    if db_api is None:
        from engram.logbook import scm_db as db_api

    conn = db_api.get_conn(dsn)
    result: ReaperResult = {
        "jobs": {"processed": 0, "to_failed": 0, "to_dead": 0, "to_pending": 0, "errors": 0},
        "runs": {"processed": 0, "failed": 0, "errors": 0},
        "locks": {"processed": 0, "released": 0, "errors": 0},
        "dry_run": dry_run,
    }

    try:
        # 1. 获取过期的 running jobs
        expired_jobs = db_api.list_expired_running_jobs(conn, grace_seconds=grace_seconds)
        if logger:
            logger.info(f"发现 {len(expired_jobs)} 个过期的 running jobs")

        # 2. 获取过期的 running runs
        expired_runs = db_api.list_expired_running_runs(
            conn, max_duration_seconds=max_duration_seconds
        )
        if logger:
            logger.info(f"发现 {len(expired_runs)} 个过期的 running runs")

        # 3. 获取过期的 locks
        expired_locks = db_api.list_expired_locks(conn, grace_seconds=grace_seconds)
        if logger:
            logger.info(f"发现 {len(expired_locks)} 个过期的 locks")

        if dry_run:
            # 模拟运行，只返回发现的数量
            result["jobs"]["processed"] = len(expired_jobs)
            result["runs"]["processed"] = len(expired_runs)
            result["locks"]["processed"] = len(expired_locks)
            if logger:
                logger.info("Dry-run 模式：不实际修改数据库")
            return result

        # 4. 处理过期的 jobs
        if expired_jobs:
            job_stats = process_expired_jobs(
                conn,
                expired_jobs,
                policy=policy,
                retry_delay_seconds=retry_delay_seconds,
                db_api=db_api,
            )
            result["jobs"] = job_stats
            conn.commit()
            if logger:
                logger.info(f"Jobs 处理完成: {job_stats}")

        # 5. 处理过期的 runs
        if expired_runs:
            run_stats = process_expired_runs(conn, expired_runs, db_api=db_api)
            result["runs"] = run_stats
            conn.commit()
            if logger:
                logger.info(f"Runs 处理完成: {run_stats}")

        # 6. 处理过期的 locks
        if expired_locks:
            lock_stats = process_expired_locks(conn, expired_locks, db_api=db_api)
            result["locks"] = lock_stats
            conn.commit()
            if logger:
                logger.info(f"Locks 处理完成: {lock_stats}")

    except Exception as e:
        if logger:
            logger.error(f"Reaper 执行出错: {e}")
        conn.rollback()
        raise
    finally:
        conn.close()

    return result
