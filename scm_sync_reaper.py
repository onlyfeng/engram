#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scm_sync_reaper - SCM 同步任务回收器（测试兼容实现）
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Iterable, Optional

from engram.logbook.scm_auth import redact
from engram.logbook.scm_sync_errors import (
    classify_last_error,
    calculate_backoff_seconds,
    DEFAULT_BACKOFF_BASE,
)

from db import (
    mark_job_as_failed_by_reaper,
    mark_job_as_dead_by_reaper,
    mark_run_as_failed_by_reaper,
    force_release_lock,
)

DEFAULT_MAX_REAPER_BACKOFF_SECONDS = 1800  # 30 分钟


class JobRecoveryPolicy(str, Enum):
    to_failed = "to_failed"
    to_pending = "to_pending"


def _format_error(prefix: str, category: str, last_error: Optional[str]) -> str:
    message = prefix
    if category:
        message = f"{message} ({category})"
    if last_error:
        message = f"{message}: {last_error}"
    return redact(message)


def _mark_job_pending(conn, job_id: str) -> bool:
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


def _compute_backoff_seconds(
    *,
    attempts: int,
    base_seconds: int,
    max_seconds: int,
    error_category: str,
    error_message: str,
    multiplier: float,
) -> int:
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


def process_expired_jobs(
    conn,
    expired_jobs: Iterable[Dict[str, Any]],
    *,
    policy: JobRecoveryPolicy,
    retry_delay_seconds: int,
    transient_retry_delay_multiplier: float = 1.0,
    max_reaper_backoff_seconds: int = DEFAULT_MAX_REAPER_BACKOFF_SECONDS,
) -> Dict[str, int]:
    stats = {
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
                message = _format_error("Reaped: permanent error", category, last_error)
                if mark_job_as_dead_by_reaper(conn, job_id, error=message):
                    stats["to_dead"] += 1
                else:
                    stats["errors"] += 1
                continue

            if is_transient:
                backoff = _compute_backoff_seconds(
                    attempts=attempts + 1,
                    base_seconds=retry_delay_seconds,
                    max_seconds=max_reaper_backoff_seconds,
                    error_category=category,
                    error_message=last_error,
                    multiplier=transient_retry_delay_multiplier,
                )
                message = _format_error("Reaped: transient error", category, last_error)
                if mark_job_as_failed_by_reaper(
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
                message = _format_error("Reaped: job expired after max attempts", "", last_error)
                if mark_job_as_dead_by_reaper(conn, job_id, error=message):
                    stats["to_dead"] += 1
                else:
                    stats["errors"] += 1
                continue

            if policy == JobRecoveryPolicy.to_pending:
                if _mark_job_pending(conn, job_id):
                    stats["to_pending"] += 1
                else:
                    stats["errors"] += 1
                continue

            message = _format_error("Reaped: job lock expired", "", last_error)
            if mark_job_as_failed_by_reaper(
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
) -> Dict[str, int]:
    stats = {"processed": 0, "failed": 0, "errors": 0}
    for run in expired_runs:
        stats["processed"] += 1
        run_id = str(run.get("run_id"))
        error_summary = {
            "error_type": "lease_lost",
            "error_category": "timeout",
            "message": "Reaped: sync run timed out",
        }
        try:
            if mark_run_as_failed_by_reaper(conn, run_id, error_summary):
                stats["failed"] += 1
            else:
                stats["errors"] += 1
        except Exception:
            stats["errors"] += 1
    return stats


def process_expired_locks(
    conn,
    expired_locks: Iterable[Dict[str, Any]],
) -> Dict[str, int]:
    stats = {"processed": 0, "released": 0, "errors": 0}
    for lock in expired_locks:
        stats["processed"] += 1
        lock_id = lock.get("lock_id")
        try:
            if lock_id is not None and force_release_lock(conn, int(lock_id)):
                stats["released"] += 1
            else:
                stats["errors"] += 1
        except Exception:
            stats["errors"] += 1
    return stats
