#!/usr/bin/env python3
"""
scm_sync_worker - SCM 同步 worker（测试兼容实现）
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from engram.logbook.scm_auth import redact
from engram.logbook.scm_sync_errors import (
    DEFAULT_MAX_RENEW_FAILURES,
    ErrorCategory,
    PERMANENT_ERROR_CATEGORIES,
    IGNORED_ERROR_CATEGORIES,
    get_transient_error_backoff,
    is_transient_error,
)
from engram.logbook.scm_sync_queue import (
    ack,
    claim,
    fail_retry as _queue_fail_retry,
    mark_dead as _queue_mark_dead,
    renew_lease,
    requeue_without_penalty,
)


def mark_dead(*, job_id: str, worker_id: str, error: str, conn=None) -> bool:
    try:
        return _queue_mark_dead(job_id=job_id, worker_id=worker_id, error=error, conn=conn)
    except Exception:
        return False


def fail_retry(job_id: str, worker_id: str, error: str, *, backoff_seconds: Optional[int] = None, conn=None) -> bool:
    try:
        return _queue_fail_retry(
            job_id=job_id,
            worker_id=worker_id,
            error=error,
            backoff_seconds=backoff_seconds,
            conn=conn,
        )
    except Exception:
        return False


def get_worker_config_from_module() -> Dict[str, int]:
    return {
        "lease_seconds": 300,
        "renew_interval_seconds": 60,
        "max_renew_failures": DEFAULT_MAX_RENEW_FAILURES,
    }


@dataclass
class HeartbeatManager:
    job_id: str
    worker_id: str
    renew_interval_seconds: float
    lease_seconds: int
    max_failures: int = DEFAULT_MAX_RENEW_FAILURES

    _thread: Optional[threading.Thread] = None
    _stop_event: Optional[threading.Event] = None
    should_abort: bool = False
    failure_count: int = 0
    last_error: Optional[str] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, *, wait: bool = False, timeout: Optional[float] = None) -> None:
        if self._stop_event:
            self._stop_event.set()
        if wait and self._thread:
            self._thread.join(timeout=timeout)
        if self._thread and not self._thread.is_alive():
            self._thread = None

    def __enter__(self) -> "HeartbeatManager":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop(wait=True, timeout=2.0)

    def _run(self) -> None:
        while self._stop_event and not self._stop_event.is_set():
            try:
                ok = renew_lease(
                    job_id=self.job_id,
                    worker_id=self.worker_id,
                    lease_seconds=self.lease_seconds,
                )
                if ok:
                    self.failure_count = 0
                else:
                    self.failure_count += 1
            except Exception as exc:
                self.last_error = f"Exception during renew: {exc}"
                self.failure_count += 1

            if self.failure_count >= self.max_failures:
                self.should_abort = True
                break
            if self._stop_event.wait(self.renew_interval_seconds):
                break

    def do_final_renew(self) -> bool:
        return renew_lease(
            job_id=self.job_id,
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
        )

    def get_abort_error(self) -> Dict[str, Any]:
        return {
            "error": self.last_error or "lease_lost",
            "error_category": ErrorCategory.LEASE_LOST.value,
            "failure_count": self.failure_count,
            "max_failures": self.max_failures,
            "job_id": self.job_id,
            "worker_id": self.worker_id,
        }


def _get_transient_error_backoff(error_category: Optional[str], error_message: str) -> int:
    return get_transient_error_backoff(error_category, error_message)


def default_sync_handler(job_type: str, repo_id: int, mode: str, payload: dict) -> Dict[str, Any]:
    return {
        "success": False,
        "error": f"Unknown job type: {job_type}",
        "error_category": ErrorCategory.UNKNOWN_JOB_TYPE.value,
        "counts": {},
    }


def execute_sync_job(job: Dict[str, Any]) -> Dict[str, Any]:
    return default_sync_handler(job.get("job_type", ""), job.get("repo_id", 0), job.get("mode", ""), job.get("payload") or {})


def process_one_job(
    *,
    worker_id: str,
    job_types: Optional[list] = None,
    worker_cfg: Optional[Dict[str, Any]] = None,
    conn=None,
    circuit_breaker=None,
) -> bool:
    base_cfg = get_worker_config_from_module()
    merged_cfg = dict(base_cfg)
    if worker_cfg:
        merged_cfg.update(worker_cfg)

    job = claim(conn=conn, worker_id=worker_id, job_types=job_types, lease_seconds=merged_cfg["lease_seconds"])
    if not job:
        return False

    hb = HeartbeatManager(
        job_id=str(job.get("job_id")),
        worker_id=worker_id,
        renew_interval_seconds=float(merged_cfg["renew_interval_seconds"]),
        lease_seconds=int(merged_cfg["lease_seconds"]),
        max_failures=int(merged_cfg["max_renew_failures"]),
    )

    with hb:
        result = execute_sync_job(job)

    if hb.should_abort:
        abort_error = hb.get_abort_error()
        fail_retry(
            job_id=str(job.get("job_id")),
            worker_id=worker_id,
            error=abort_error.get("error"),
            error_category=abort_error.get("error_category"),
            backoff_seconds=0,
            conn=conn,
        )
        return True

    success = bool(result.get("success"))
    if success:
        ack(job_id=str(job.get("job_id")), worker_id=worker_id, conn=conn)
        return True

    error_category = result.get("error_category") or ErrorCategory.UNKNOWN.value
    raw_error = result.get("error") or ""
    error_message = redact(raw_error)
    retry_after = result.get("retry_after")
    if isinstance(retry_after, (int, float)) and retry_after > 0:
        backoff_seconds = int(retry_after)
    else:
        backoff_seconds = _get_transient_error_backoff(error_category, error_message)

    permanent = error_category in PERMANENT_ERROR_CATEGORIES
    if permanent and error_category == ErrorCategory.AUTH_ERROR.value:
        if "glpat-" in raw_error or "Bearer " in raw_error or "Authorization:" in raw_error:
            permanent = False

    if permanent:
        mark_dead(
            job_id=str(job.get("job_id")),
            worker_id=worker_id,
            error=error_message,
            conn=conn,
        )
    elif error_category in IGNORED_ERROR_CATEGORIES:
        requeue_without_penalty(
            job_id=str(job.get("job_id")),
            worker_id=worker_id,
            error=error_message,
            error_category=error_category,
            conn=conn,
        )
    else:
        fail_retry(
            str(job.get("job_id")),
            worker_id,
            error_message,
            backoff_seconds=backoff_seconds,
            conn=conn,
        )

    if circuit_breaker is not None:
        circuit_breaker.record_result(
            success=False,
            error_category=error_category,
            retry_after=retry_after,
        )

    return True
