#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scm_sync_reaper.py - SCM 同步任务清理器

用于清理过期/卡住的同步任务：
- scm.sync_jobs: running 且锁租约过期的任务，转为 failed/pending
- scm.sync_runs: running 且持续时间超过阈值的 run，标记 failed
- scm.sync_locks: 过期的锁，强制释放

所有动作输出结构化 JSON 日志，便于外部采集。

使用示例:
    # 扫描并输出过期任务（dry-run 模式）
    python scm_sync_reaper.py scan
    
    # 执行清理（实际处理）
    python scm_sync_reaper.py reap
    
    # 仅清理过期锁
    python scm_sync_reaper.py reap --locks-only
    
    # 自定义阈值
    python scm_sync_reaper.py reap --job-grace-seconds 120 --run-max-seconds 3600
    
    # 循环模式（每 60 秒执行一次）
    python scm_sync_reaper.py loop --interval 60

策略说明:
    - 过期 jobs: 
      - 如果 attempts < max_attempts: 转为 failed，延迟重试
      - 如果 attempts >= max_attempts: 转为 dead，不再重试
    - 超时 runs: 标记为 failed，写 error_summary
    - 过期 locks: 直接强制释放
"""

import json
import os
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

import typer

# db 模块通过 pip install -e 安装（参见 pyproject.toml 中的 py-modules 配置）
from db import (
    get_conn,
    list_expired_running_jobs,
    list_expired_running_runs,
    list_expired_locks,
    mark_job_as_failed_by_reaper,
    mark_job_as_pending_by_reaper,
    mark_job_as_dead_by_reaper,
    mark_run_as_failed_by_reaper,
    force_release_lock,
)
from engram_step1.scm_auth import redact, redact_dict

# 导入错误分类常量和函数（从统一的 scm_sync_errors 模块）
from engram_step1.scm_sync_errors import (
    ErrorCategory,
    TRANSIENT_ERROR_CATEGORIES,
    TRANSIENT_ERROR_KEYWORDS,
    TRANSIENT_ERROR_BACKOFF,
    PERMANENT_ERROR_CATEGORIES,
    DEFAULT_BACKOFF_BASE,
    DEFAULT_MAX_BACKOFF,
    is_transient_error as _is_transient_error,
    is_permanent_error as _is_permanent_error,
    get_transient_error_backoff as _get_transient_error_backoff,
    classify_last_error as _classify_last_error,
    calculate_backoff_seconds as _calculate_backoff_seconds,
)

# Reaper 默认最大退避时间（秒），可通过 CLI 参数覆盖
DEFAULT_MAX_REAPER_BACKOFF_SECONDS = 1800  # 30 分钟

# ============ CLI 应用定义 ============

app = typer.Typer(
    name="scm-sync-reaper",
    help="SCM 同步任务清理器（清理过期/卡住的任务）",
    no_args_is_help=True,
)


class JobRecoveryPolicy(str, Enum):
    """过期任务恢复策略"""
    to_failed = "to_failed"    # 转为 failed，允许重试
    to_pending = "to_pending"  # 转为 pending，不增加重试次数


# ============ 结构化日志输出 ============


def log_json(
    event: str,
    level: str = "info",
    **kwargs,
) -> None:
    """
    输出结构化 JSON 日志
    
    Args:
        event: 事件类型
        level: 日志级别 (info/warn/error)
        **kwargs: 额外字段
    """
    log_entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "level": level,
        **kwargs,
    }
    print(json.dumps(log_entry, ensure_ascii=False, default=str))


def get_connection():
    """获取数据库连接"""
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        log_json(
            "startup_error",
            level="error",
            error="POSTGRES_DSN 环境变量未设置",
        )
        raise typer.Exit(1)
    return get_conn(dsn)


# ============ 扫描函数 ============


def scan_expired_jobs(
    conn,
    grace_seconds: int = 60,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """扫描过期的 running 任务"""
    jobs = list_expired_running_jobs(conn, grace_seconds=grace_seconds, limit=limit)
    for job in jobs:
        log_json(
            "expired_job_found",
            job_id=str(job["job_id"]),
            repo_id=job["repo_id"],
            job_type=job["job_type"],
            locked_by=job["locked_by"],
            locked_at=job["locked_at"],
            lease_seconds=job["lease_seconds"],
            expired_seconds=job.get("expired_seconds"),
            attempts=job["attempts"],
            max_attempts=job["max_attempts"],
        )
    return jobs


def scan_expired_runs(
    conn,
    max_duration_seconds: int = 1800,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """扫描超时的 running 同步运行"""
    runs = list_expired_running_runs(conn, max_duration_seconds=max_duration_seconds, limit=limit)
    for run in runs:
        log_json(
            "expired_run_found",
            run_id=str(run["run_id"]),
            repo_id=run["repo_id"],
            job_type=run["job_type"],
            started_at=run["started_at"],
            running_seconds=run.get("running_seconds"),
        )
    return runs


def scan_expired_locks(
    conn,
    grace_seconds: int = 0,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """扫描过期的锁"""
    locks = list_expired_locks(conn, grace_seconds=grace_seconds, limit=limit)
    for lock in locks:
        log_json(
            "expired_lock_found",
            lock_id=lock["lock_id"],
            repo_id=lock["repo_id"],
            job_type=lock["job_type"],
            locked_by=lock["locked_by"],
            locked_at=lock["locked_at"],
            lease_seconds=lock["lease_seconds"],
            expired_seconds=lock.get("expired_seconds"),
        )
    return locks


# ============ 处理函数 ============

# 注意：_classify_last_error 函数已迁移到 engram_step1.scm_sync_errors 模块
# 通过上面的 import 语句导入


def process_expired_jobs(
    conn,
    jobs: List[Dict[str, Any]],
    policy: JobRecoveryPolicy = JobRecoveryPolicy.to_failed,
    retry_delay_seconds: int = 60,
    transient_retry_delay_multiplier: float = 2.0,
    max_reaper_backoff_seconds: int = DEFAULT_MAX_REAPER_BACKOFF_SECONDS,
) -> Dict[str, int]:
    """
    处理过期的 running 任务
    
    策略说明（基于 last_error 分类）：
    - 永久性错误（auth_error/repo_not_found/permission_denied）：直接转为 dead
    - 临时性错误（rate_limit/timeout/network/server_error）：转为 failed + 统一 backoff 计算
    - 未知错误 + 达到 max_attempts：转为 dead
    - 未知错误 + 未达到 max_attempts：按照 policy 参数处理
    
    退避计算策略（复用 scm_sync_errors.calculate_backoff_seconds）：
    - 根据错误类别获取基础退避时间
    - 应用指数退避：base * 2^(attempts-1)
    - 限制在 max_reaper_backoff_seconds 范围内
    
    Args:
        conn: 数据库连接
        jobs: 过期任务列表
        policy: 恢复策略（仅对未分类的错误生效）
        retry_delay_seconds: 基础重试延迟秒数（用于未分类错误）
        transient_retry_delay_multiplier: 临时性错误的延迟乘数（已废弃，保留兼容性）
        max_reaper_backoff_seconds: 最大退避时间（秒），默认 1800（30 分钟）
    
    Returns:
        处理统计 {processed, to_failed, to_pending, to_dead, errors}
    """
    stats = {
        "processed": 0,
        "to_failed": 0,
        "to_pending": 0,
        "to_dead": 0,
        "errors": 0,
    }
    
    for job in jobs:
        job_id = str(job["job_id"])
        attempts = job["attempts"]
        max_attempts = job["max_attempts"]
        last_error = job.get("last_error", "")
        
        try:
            # 对 last_error 进行分类
            is_permanent, is_transient, error_category = _classify_last_error(last_error)
            
            # 对 locked_by 进行脱敏（可能包含敏感信息）
            redacted_locked_by = redact(job.get('locked_by', ''))
            
            # 策略 1：永久性错误，直接转为 dead（不再重试）
            if is_permanent:
                error_msg = f"Reaped: permanent error ({error_category}), marking as dead"
                redacted_error_msg = redact(error_msg)
                success = mark_job_as_dead_by_reaper(conn, job_id, redacted_error_msg)
                if success:
                    stats["to_dead"] += 1
                    log_json(
                        "job_marked_dead",
                        job_id=job_id,
                        repo_id=job["repo_id"],
                        job_type=job["job_type"],
                        error_category=error_category,
                        reason="permanent_error",
                        attempts=attempts,
                        max_attempts=max_attempts,
                    )
                else:
                    stats["errors"] += 1
                    log_json(
                        "job_update_failed",
                        level="error",
                        job_id=job_id,
                        action="mark_dead",
                    )
            
            # 策略 2：临时性错误，转为 failed + 统一 backoff 计算
            elif is_transient:
                # 使用统一的 backoff 计算函数（复用 queue 的指数退避逻辑）
                actual_delay = _calculate_backoff_seconds(
                    attempts=attempts,
                    base_seconds=DEFAULT_BACKOFF_BASE,
                    max_seconds=max_reaper_backoff_seconds,
                    error_category=error_category,
                    error_message=last_error,
                )
                
                error_msg = f"Reaped: transient error ({error_category}), locked_by={redacted_locked_by}, expired_seconds={job.get('expired_seconds', 0):.0f}"
                redacted_error_msg = redact(error_msg)
                success = mark_job_as_failed_by_reaper(
                    conn, job_id, redacted_error_msg, retry_delay_seconds=actual_delay
                )
                if success:
                    stats["to_failed"] += 1
                    log_json(
                        "job_marked_failed",
                        job_id=job_id,
                        repo_id=job["repo_id"],
                        job_type=job["job_type"],
                        error_category=error_category,
                        reason="transient_error",
                        retry_delay_seconds=actual_delay,
                    )
                else:
                    stats["errors"] += 1
                    log_json(
                        "job_update_failed",
                        level="error",
                        job_id=job_id,
                        action="mark_failed",
                    )
            
            # 策略 3：未分类错误 + 达到 max_attempts，转为 dead
            elif attempts >= max_attempts:
                error_msg = f"Reaped: job expired after {attempts} attempts, marking as dead"
                redacted_error_msg = redact(error_msg)
                success = mark_job_as_dead_by_reaper(conn, job_id, redacted_error_msg)
                if success:
                    stats["to_dead"] += 1
                    log_json(
                        "job_marked_dead",
                        job_id=job_id,
                        repo_id=job["repo_id"],
                        job_type=job["job_type"],
                        reason="max_attempts_reached",
                        attempts=attempts,
                        max_attempts=max_attempts,
                    )
                else:
                    stats["errors"] += 1
                    log_json(
                        "job_update_failed",
                        level="error",
                        job_id=job_id,
                        action="mark_dead",
                    )
            
            # 策略 4：未分类错误 + 未达到 max_attempts，按 policy 处理
            elif policy == JobRecoveryPolicy.to_failed:
                error_msg = f"Reaped: job lock expired (locked_by={redacted_locked_by}, expired_seconds={job.get('expired_seconds', 0):.0f})"
                redacted_error_msg = redact(error_msg)
                success = mark_job_as_failed_by_reaper(
                    conn, job_id, redacted_error_msg, retry_delay_seconds=retry_delay_seconds
                )
                if success:
                    stats["to_failed"] += 1
                    log_json(
                        "job_marked_failed",
                        job_id=job_id,
                        repo_id=job["repo_id"],
                        job_type=job["job_type"],
                        reason="lock_expired",
                        retry_delay_seconds=retry_delay_seconds,
                    )
                else:
                    stats["errors"] += 1
                    log_json(
                        "job_update_failed",
                        level="error",
                        job_id=job_id,
                        action="mark_failed",
                    )
            else:
                # to_pending 策略
                error_msg = "Reaped: job lock expired, restoring to pending"
                redacted_error_msg = redact(error_msg)
                success = mark_job_as_pending_by_reaper(conn, job_id, redacted_error_msg)
                if success:
                    stats["to_pending"] += 1
                    log_json(
                        "job_marked_pending",
                        job_id=job_id,
                        repo_id=job["repo_id"],
                        job_type=job["job_type"],
                        reason="lock_expired_to_pending",
                    )
                else:
                    stats["errors"] += 1
                    log_json(
                        "job_update_failed",
                        level="error",
                        job_id=job_id,
                        action="mark_pending",
                    )
            
            stats["processed"] += 1
            conn.commit()
            
        except Exception as e:
            stats["errors"] += 1
            # 对错误信息进行脱敏
            log_json(
                "job_process_error",
                level="error",
                job_id=job_id,
                error=redact(str(e)),
            )
            conn.rollback()
    
    return stats


def process_expired_runs(
    conn,
    runs: List[Dict[str, Any]],
) -> Dict[str, int]:
    """
    处理超时的 running 同步运行
    
    Args:
        conn: 数据库连接
        runs: 超时运行列表
    
    Returns:
        处理统计 {processed, failed, errors}
    """
    stats = {
        "processed": 0,
        "failed": 0,
        "errors": 0,
    }
    
    for run in runs:
        run_id = str(run["run_id"])
        
        try:
            error_summary = {
                "error_type": "reaper_timeout",
                "message": f"Reaped: sync run timed out after {run.get('running_seconds', 0):.0f} seconds",
                "reaped_at": datetime.now(timezone.utc).isoformat(),
                "running_seconds": run.get("running_seconds"),
            }
            
            # 对 error_summary 进行脱敏，防止敏感信息泄露
            redacted_error_summary = redact_dict(error_summary)
            
            success = mark_run_as_failed_by_reaper(conn, run_id, redacted_error_summary)
            if success:
                stats["failed"] += 1
                log_json(
                    "run_marked_failed",
                    run_id=run_id,
                    repo_id=run["repo_id"],
                    job_type=run["job_type"],
                    running_seconds=run.get("running_seconds"),
                )
            else:
                stats["errors"] += 1
                log_json(
                    "run_update_failed",
                    level="error",
                    run_id=run_id,
                    action="mark_failed",
                )
            
            stats["processed"] += 1
            conn.commit()
            
        except Exception as e:
            stats["errors"] += 1
            # 对错误信息进行脱敏
            log_json(
                "run_process_error",
                level="error",
                run_id=run_id,
                error=redact(str(e)),
            )
            conn.rollback()
    
    return stats


def process_expired_locks(
    conn,
    locks: List[Dict[str, Any]],
) -> Dict[str, int]:
    """
    处理过期的锁（强制释放）
    
    Args:
        conn: 数据库连接
        locks: 过期锁列表
    
    Returns:
        处理统计 {processed, released, errors}
    """
    stats = {
        "processed": 0,
        "released": 0,
        "errors": 0,
    }
    
    for lock in locks:
        lock_id = lock["lock_id"]
        
        try:
            success = force_release_lock(conn, lock_id)
            if success:
                stats["released"] += 1
                log_json(
                    "lock_released",
                    lock_id=lock_id,
                    repo_id=lock["repo_id"],
                    job_type=lock["job_type"],
                    locked_by=lock["locked_by"],
                    expired_seconds=lock.get("expired_seconds"),
                )
            else:
                stats["errors"] += 1
                log_json(
                    "lock_release_failed",
                    level="error",
                    lock_id=lock_id,
                )
            
            stats["processed"] += 1
            conn.commit()
            
        except Exception as e:
            stats["errors"] += 1
            # 对错误信息进行脱敏
            log_json(
                "lock_process_error",
                level="error",
                lock_id=lock_id,
                error=redact(str(e)),
            )
            conn.rollback()
    
    return stats


# ============ CLI 命令 ============


@app.command("scan")
def cmd_scan(
    job_grace_seconds: int = typer.Option(
        60, "--job-grace-seconds",
        help="任务锁过期宽限期（秒）"
    ),
    run_max_seconds: int = typer.Option(
        1800, "--run-max-seconds",
        help="运行最大时长阈值（秒），默认 30 分钟"
    ),
    lock_grace_seconds: int = typer.Option(
        0, "--lock-grace-seconds",
        help="锁过期宽限期（秒）"
    ),
    limit: int = typer.Option(
        100, "--limit", "-l",
        help="每类最大扫描数量"
    ),
):
    """
    扫描过期任务（仅扫描，不处理）
    
    输出发现的过期 jobs、runs、locks 信息。
    """
    log_json(
        "scan_started",
        job_grace_seconds=job_grace_seconds,
        run_max_seconds=run_max_seconds,
        lock_grace_seconds=lock_grace_seconds,
        limit=limit,
    )
    
    try:
        conn = get_connection()
        try:
            # 扫描过期任务
            jobs = scan_expired_jobs(conn, grace_seconds=job_grace_seconds, limit=limit)
            
            # 扫描超时运行
            runs = scan_expired_runs(conn, max_duration_seconds=run_max_seconds, limit=limit)
            
            # 扫描过期锁
            locks = scan_expired_locks(conn, grace_seconds=lock_grace_seconds, limit=limit)
            
            log_json(
                "scan_completed",
                expired_jobs=len(jobs),
                expired_runs=len(runs),
                expired_locks=len(locks),
            )
            
        finally:
            conn.close()
            
    except Exception as e:
        log_json(
            "scan_error",
            level="error",
            error=str(e),
            error_type=type(e).__name__,
        )
        raise typer.Exit(1)


@app.command("reap")
def cmd_reap(
    job_grace_seconds: int = typer.Option(
        60, "--job-grace-seconds",
        help="任务锁过期宽限期（秒）"
    ),
    run_max_seconds: int = typer.Option(
        1800, "--run-max-seconds",
        help="运行最大时长阈值（秒），默认 30 分钟"
    ),
    lock_grace_seconds: int = typer.Option(
        0, "--lock-grace-seconds",
        help="锁过期宽限期（秒）"
    ),
    job_policy: JobRecoveryPolicy = typer.Option(
        JobRecoveryPolicy.to_failed, "--job-policy",
        help="过期任务恢复策略 (to_failed/to_pending)"
    ),
    retry_delay_seconds: int = typer.Option(
        60, "--retry-delay-seconds",
        help="基础重试延迟秒数（用于未分类错误）"
    ),
    max_reaper_backoff_seconds: int = typer.Option(
        DEFAULT_MAX_REAPER_BACKOFF_SECONDS, "--max-reaper-backoff-seconds",
        help="最大退避时间（秒），防止指数退避无限增长，默认 1800（30 分钟）"
    ),
    limit: int = typer.Option(
        100, "--limit", "-l",
        help="每类最大处理数量"
    ),
    jobs_only: bool = typer.Option(
        False, "--jobs-only",
        help="仅处理过期任务"
    ),
    runs_only: bool = typer.Option(
        False, "--runs-only",
        help="仅处理超时运行"
    ),
    locks_only: bool = typer.Option(
        False, "--locks-only",
        help="仅处理过期锁"
    ),
):
    """
    执行清理（扫描并处理过期任务）
    
    将过期的 jobs 转为 failed/pending，超时的 runs 标记为 failed，
    过期的 locks 强制释放。
    """
    # 确定要处理的类型
    process_jobs = not (runs_only or locks_only) or jobs_only
    process_runs = not (jobs_only or locks_only) or runs_only
    process_locks = not (jobs_only or runs_only) or locks_only
    
    log_json(
        "reap_started",
        job_grace_seconds=job_grace_seconds,
        run_max_seconds=run_max_seconds,
        lock_grace_seconds=lock_grace_seconds,
        job_policy=job_policy.value,
        retry_delay_seconds=retry_delay_seconds,
        max_reaper_backoff_seconds=max_reaper_backoff_seconds,
        limit=limit,
        process_jobs=process_jobs,
        process_runs=process_runs,
        process_locks=process_locks,
    )
    
    total_stats = {
        "jobs": {"processed": 0, "to_failed": 0, "to_pending": 0, "to_dead": 0, "errors": 0},
        "runs": {"processed": 0, "failed": 0, "errors": 0},
        "locks": {"processed": 0, "released": 0, "errors": 0},
    }
    
    try:
        conn = get_connection()
        try:
            # 处理过期任务
            if process_jobs:
                jobs = scan_expired_jobs(conn, grace_seconds=job_grace_seconds, limit=limit)
                if jobs:
                    job_stats = process_expired_jobs(
                        conn, jobs,
                        policy=job_policy,
                        retry_delay_seconds=retry_delay_seconds,
                        max_reaper_backoff_seconds=max_reaper_backoff_seconds,
                    )
                    total_stats["jobs"] = job_stats
            
            # 处理超时运行
            if process_runs:
                runs = scan_expired_runs(conn, max_duration_seconds=run_max_seconds, limit=limit)
                if runs:
                    run_stats = process_expired_runs(conn, runs)
                    total_stats["runs"] = run_stats
            
            # 处理过期锁
            if process_locks:
                locks = scan_expired_locks(conn, grace_seconds=lock_grace_seconds, limit=limit)
                if locks:
                    lock_stats = process_expired_locks(conn, locks)
                    total_stats["locks"] = lock_stats
            
            log_json(
                "reap_completed",
                stats=total_stats,
            )
            
        finally:
            conn.close()
            
    except Exception as e:
        log_json(
            "reap_error",
            level="error",
            error=str(e),
            error_type=type(e).__name__,
        )
        raise typer.Exit(1)


@app.command("loop")
def cmd_loop(
    interval: int = typer.Option(
        60, "--interval", "-i",
        help="循环间隔（秒）"
    ),
    job_grace_seconds: int = typer.Option(
        60, "--job-grace-seconds",
        help="任务锁过期宽限期（秒）"
    ),
    run_max_seconds: int = typer.Option(
        1800, "--run-max-seconds",
        help="运行最大时长阈值（秒），默认 30 分钟"
    ),
    lock_grace_seconds: int = typer.Option(
        0, "--lock-grace-seconds",
        help="锁过期宽限期（秒）"
    ),
    job_policy: JobRecoveryPolicy = typer.Option(
        JobRecoveryPolicy.to_failed, "--job-policy",
        help="过期任务恢复策略 (to_failed/to_pending)"
    ),
    retry_delay_seconds: int = typer.Option(
        60, "--retry-delay-seconds",
        help="基础重试延迟秒数（用于未分类错误）"
    ),
    max_reaper_backoff_seconds: int = typer.Option(
        DEFAULT_MAX_REAPER_BACKOFF_SECONDS, "--max-reaper-backoff-seconds",
        help="最大退避时间（秒），防止指数退避无限增长，默认 1800（30 分钟）"
    ),
    limit: int = typer.Option(
        100, "--limit", "-l",
        help="每类最大处理数量"
    ),
):
    """
    循环模式（定时执行清理）
    
    每隔 interval 秒执行一次清理操作。
    适合作为 cron job 或 systemd timer 运行。
    """
    log_json(
        "loop_started",
        interval=interval,
        job_grace_seconds=job_grace_seconds,
        run_max_seconds=run_max_seconds,
        lock_grace_seconds=lock_grace_seconds,
        job_policy=job_policy.value,
        retry_delay_seconds=retry_delay_seconds,
        max_reaper_backoff_seconds=max_reaper_backoff_seconds,
        limit=limit,
    )
    
    iteration = 0
    while True:
        iteration += 1
        log_json("loop_iteration_start", iteration=iteration)
        
        try:
            conn = get_connection()
            try:
                total_stats = {
                    "jobs": {"processed": 0, "to_failed": 0, "to_pending": 0, "to_dead": 0, "errors": 0},
                    "runs": {"processed": 0, "failed": 0, "errors": 0},
                    "locks": {"processed": 0, "released": 0, "errors": 0},
                }
                
                # 处理过期任务
                jobs = scan_expired_jobs(conn, grace_seconds=job_grace_seconds, limit=limit)
                if jobs:
                    job_stats = process_expired_jobs(
                        conn, jobs,
                        policy=job_policy,
                        retry_delay_seconds=retry_delay_seconds,
                        max_reaper_backoff_seconds=max_reaper_backoff_seconds,
                    )
                    total_stats["jobs"] = job_stats
                
                # 处理超时运行
                runs = scan_expired_runs(conn, max_duration_seconds=run_max_seconds, limit=limit)
                if runs:
                    run_stats = process_expired_runs(conn, runs)
                    total_stats["runs"] = run_stats
                
                # 处理过期锁
                locks = scan_expired_locks(conn, grace_seconds=lock_grace_seconds, limit=limit)
                if locks:
                    lock_stats = process_expired_locks(conn, locks)
                    total_stats["locks"] = lock_stats
                
                log_json(
                    "loop_iteration_completed",
                    iteration=iteration,
                    stats=total_stats,
                )
                
            finally:
                conn.close()
                
        except Exception as e:
            log_json(
                "loop_iteration_error",
                level="error",
                iteration=iteration,
                error=str(e),
                error_type=type(e).__name__,
            )
        
        # 等待下一次迭代
        log_json("loop_sleeping", interval=interval)
        time.sleep(interval)


# ============ 主入口 ============


def main():
    """主入口"""
    app()


if __name__ == "__main__":
    main()
