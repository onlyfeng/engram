# -*- coding: utf-8 -*-
"""
scm_sync_worker_core - SCM 同步 Worker 核心实现

功能:
- 从队列 claim 任务
- 调用 executor 执行同步
- 处理心跳续租
- 错误分类和重试逻辑
- sync_runs 生命周期管理（run_start -> execute -> run_finish）

设计原则:
- 纯业务逻辑，不包含 argparse/打印
- CLI 入口在根目录 scm_sync_worker.py 和 scripts/scm_sync_worker.py

sync_runs 生命周期:
1. claim job 后，生成 run_id
2. 读取 cursor_before（从 kv 或 payload）
3. 调用 insert_sync_run_start 写入 status=running
4. 执行 job 得到 result
5. 调用 build_run_finish_payload_from_result + validate_run_finish_payload
6. 调用 insert_sync_run_finish
7. ack 时将 run_id 写回 job.last_run_id

使用示例:
    from engram.logbook.scm_sync_worker_core import (
        HeartbeatManager,
        process_one_job,
        set_executor,
        get_executor,
    )

    # 处理单个任务
    processed = process_one_job(worker_id="worker-1", conn=conn)
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from engram.logbook import scm_sync_lock
from engram.logbook.scm_auth import redact
from engram.logbook.scm_sync_errors import (
    DEFAULT_MAX_RENEW_FAILURES,
    IGNORED_ERROR_CATEGORIES,
    PERMANENT_ERROR_CATEGORIES,
    ErrorCategory,
    get_transient_error_backoff,
)
from engram.logbook.scm_sync_queue import (
    ack,
    claim,
    renew_lease,
    requeue_without_penalty,
)
from engram.logbook.scm_sync_queue import (
    fail_retry as _queue_fail_retry,
)
from engram.logbook.scm_sync_queue import (
    mark_dead as _queue_mark_dead,
)
from engram.logbook.scm_sync_run_contract import (
    RunStatus,
    build_payload_for_exception,
    build_payload_for_lease_lost,
    build_run_finish_payload_from_result,
    validate_run_finish_payload,
)

__all__ = [
    # 类型
    "SyncExecutorType",
    # 数据类
    "HeartbeatManager",
    # 函数
    "get_db_connection",
    "generate_run_id",
    "read_cursor_before",
    "insert_sync_run_start",
    "insert_sync_run_finish",
    "mark_dead",
    "fail_retry",
    "get_worker_config_from_module",
    "get_transient_error_backoff_wrapper",
    "set_executor",
    "get_executor",
    "default_sync_handler",
    "execute_sync_job",
    "process_one_job",
]


# 执行器类型定义
SyncExecutorType = Callable[[Dict[str, Any]], Dict[str, Any]]


# ============ sync_runs 数据库操作 ============


def get_db_connection(conn=None):
    """
    获取数据库连接

    Args:
        conn: 可选的现有连接

    Returns:
        (connection, should_close) 元组
    """
    if conn is not None:
        return conn, False  # (connection, should_close)
    try:
        from engram.logbook.db import get_connection

        return get_connection(), True
    except Exception:
        return None, False


def generate_run_id() -> str:
    """生成 sync_run ID"""
    return str(uuid.uuid4())


def read_cursor_before(
    repo_id: int,
    job_type: str,
    payload: Optional[Dict[str, Any]] = None,
    conn=None,
) -> Optional[Dict[str, Any]]:
    """
    读取 cursor_before（同步开始前的游标位置）

    优先从 kv 表读取，如果没有则返回 None。
    cursor_before 用于审计和断点续传。

    Args:
        repo_id: 仓库 ID
        job_type: 任务类型
        payload: 任务 payload（可能包含覆盖的 cursor）
        conn: 数据库连接

    Returns:
        游标字典或 None
    """
    # 如果 payload 中显式指定了 cursor，使用它
    if payload and payload.get("cursor_before"):
        cursor_val = payload["cursor_before"]
        if isinstance(cursor_val, dict):
            return cursor_val
        return None

    # 从 kv 表读取
    db_conn, should_close = get_db_connection(conn)
    if db_conn is None:
        return None

    try:
        from engram.logbook import scm_db as db_module

        cursor_data = db_module.get_cursor_value(db_conn, repo_id, job_type)
        if cursor_data and cursor_data.get("value"):
            value = cursor_data["value"]
            if isinstance(value, dict):
                return value
        return None
    except Exception:
        return None
    finally:
        if should_close and db_conn:
            try:
                db_conn.close()
            except Exception:
                pass


def insert_sync_run_start(
    run_id: str,
    repo_id: int,
    job_type: str,
    mode: str,
    cursor_before: Optional[Dict[str, Any]] = None,
    meta_json: Optional[Dict[str, Any]] = None,
    conn=None,
) -> bool:
    """
    写入 sync_run 开始记录（status=running）

    Args:
        run_id: 运行 ID
        repo_id: 仓库 ID
        job_type: 任务类型
        mode: 同步模式
        cursor_before: 同步前的游标
        meta_json: 元数据
        conn: 数据库连接

    Returns:
        是否成功
    """
    db_conn, should_close = get_db_connection(conn)
    if db_conn is None:
        return False

    try:
        from engram.logbook import scm_db as db_module

        db_module.insert_sync_run_start(
            db_conn,
            run_id=run_id,
            repo_id=repo_id,
            job_type=job_type,
            mode=mode,
            cursor_before=cursor_before,
            meta_json=meta_json,
        )
        db_conn.commit()
        return True
    except Exception:
        try:
            db_conn.rollback()
        except Exception:
            pass
        return False
    finally:
        if should_close and db_conn:
            try:
                db_conn.close()
            except Exception:
                pass


def insert_sync_run_finish(
    run_id: str,
    status: str,
    cursor_after: Optional[Dict[str, Any]] = None,
    counts: Optional[Dict[str, Any]] = None,
    error_summary_json: Optional[Dict[str, Any]] = None,
    degradation_json: Optional[Dict[str, Any]] = None,
    logbook_item_id: Optional[int] = None,
    meta_json: Optional[Dict[str, Any]] = None,
    conn=None,
) -> bool:
    """
    写入 sync_run 完成记录

    Args:
        run_id: 运行 ID
        status: 最终状态 (completed/failed/no_data)
        cursor_after: 同步后的游标
        counts: 统计计数
        error_summary_json: 错误摘要（failed 状态必须提供）
        degradation_json: 降级信息
        logbook_item_id: 关联的 logbook item ID
        meta_json: 元数据
        conn: 数据库连接

    Returns:
        是否成功
    """
    db_conn, should_close = get_db_connection(conn)
    if db_conn is None:
        return False

    try:
        from engram.logbook import scm_db as db_module

        db_module.insert_sync_run_finish(
            db_conn,
            run_id=run_id,
            status=status,
            cursor_after=cursor_after,
            counts=counts,
            error_summary_json=error_summary_json,
            degradation_json=degradation_json,
            logbook_item_id=logbook_item_id,
            meta_json=meta_json,
        )
        db_conn.commit()
        return True
    except Exception:
        try:
            db_conn.rollback()
        except Exception:
            pass
        return False
    finally:
        if should_close and db_conn:
            try:
                db_conn.close()
            except Exception:
                pass


def mark_dead(*, job_id: str, worker_id: str, error: str, conn=None) -> bool:
    """标记任务为 dead"""
    try:
        return _queue_mark_dead(job_id=job_id, worker_id=worker_id, error=error, conn=conn)
    except Exception:
        return False


def fail_retry(
    job_id: str,
    worker_id: str,
    error: str,
    *,
    backoff_seconds: Optional[int] = None,
    error_category: Optional[str] = None,
    conn=None,
) -> bool:
    """
    任务失败重试包装器

    Args:
        job_id: 任务 ID
        worker_id: worker 标识符
        error: 错误信息
        backoff_seconds: 退避时间（秒）
        error_category: 错误分类（用于日志，不传递给底层函数）
        conn: 数据库连接

    Returns:
        True 表示成功，False 表示失败
    """
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
    """获取 worker 配置"""
    return {
        "lease_seconds": 300,
        "renew_interval_seconds": 60,
        "max_renew_failures": DEFAULT_MAX_RENEW_FAILURES,
    }


# ============ HeartbeatManager ============


@dataclass
class HeartbeatManager:
    """心跳管理器 - 处理任务租约续租"""

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
        """启动心跳线程"""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, *, wait: bool = False, timeout: Optional[float] = None) -> None:
        """停止心跳线程"""
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
        """心跳循环"""
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
        """执行最终续租"""
        return renew_lease(
            job_id=self.job_id,
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
        )

    def get_abort_error(self) -> Dict[str, Any]:
        """获取中止错误信息"""
        return {
            "error": self.last_error or "lease_lost",
            "error_category": ErrorCategory.LEASE_LOST.value,
            "failure_count": self.failure_count,
            "max_failures": self.max_failures,
            "job_id": self.job_id,
            "worker_id": self.worker_id,
        }


def get_transient_error_backoff_wrapper(error_category: Optional[str], error_message: str) -> int:
    """获取瞬态错误退避时间包装器"""
    return get_transient_error_backoff(error_category, error_message)


# ============ 执行器集成 ============


# 默认执行器（可被注入替换）
_injected_executor: Optional[SyncExecutorType] = None


def set_executor(executor: Optional[SyncExecutorType]) -> None:
    """
    注入自定义执行器（用于测试或自定义实现）

    Args:
        executor: 执行器函数，接收 job dict，返回 result dict
                  如果为 None，则使用默认执行器
    """
    global _injected_executor
    _injected_executor = executor


def get_executor() -> SyncExecutorType:
    """
    获取当前执行器

    Returns:
        当前生效的执行器函数
    """
    if _injected_executor is not None:
        return _injected_executor
    # 延迟导入默认执行器，避免循环依赖
    from engram.logbook.scm_sync_executor import execute_sync_job as default_execute

    return default_execute


def default_sync_handler(job_type: str, repo_id: int, mode: str, payload: dict) -> Dict[str, Any]:
    """
    默认同步处理器（兼容旧接口）

    注意：此函数保留用于向后兼容，新代码应使用 execute_sync_job。
    """
    return {
        "success": False,
        "error": f"Unknown job type: {job_type}",
        "error_category": ErrorCategory.UNKNOWN_JOB_TYPE.value,
        "counts": {},
    }


def execute_sync_job(job: Dict[str, Any]) -> Dict[str, Any]:
    """
    执行同步任务

    使用注入的执行器或默认执行器执行任务。

    Args:
        job: 任务字典，包含 job_type, repo_id, mode, payload 等字段

    Returns:
        结果字典，符合 scm_sync_result_v1.schema.json
    """
    executor = get_executor()
    return executor(job)


def process_one_job(
    *,
    worker_id: str,
    job_types: Optional[list] = None,
    worker_cfg: Optional[Dict[str, Any]] = None,
    conn=None,
    circuit_breaker=None,
    enable_sync_runs: bool = True,
) -> bool:
    """
    处理单个同步任务

    流程:
    1. 从队列 claim 一个任务
    2. 获取 (repo_id, job_type) 分布式锁
       - 锁获取失败则 requeue_without_penalty 并返回（error_category=lock_held）
    3. 生成 run_id，读取 cursor_before
    4. 调用 insert_sync_run_start 写入 status=running
    5. 启动心跳续租线程
    6. 执行同步任务
    7. 调用 build_run_finish_payload_from_result + validate_run_finish_payload
    8. 调用 insert_sync_run_finish
    9. 根据结果 ack/fail_retry/mark_dead/requeue（ack 时传入 run_id）
    10. finally 中确保释放锁

    Args:
        worker_id: worker 标识符
        job_types: 限制处理的任务类型列表
        worker_cfg: worker 配置覆盖
        conn: 数据库连接
        circuit_breaker: 熔断器实例
        enable_sync_runs: 是否启用 sync_runs 写入（默认 True）

    Returns:
        True 表示处理了一个任务（无论成功失败），False 表示队列为空
    """
    base_cfg = get_worker_config_from_module()
    merged_cfg = dict(base_cfg)
    if worker_cfg:
        merged_cfg.update(worker_cfg)

    job = claim(
        conn=conn,
        worker_id=worker_id,
        job_types=job_types,
        lease_seconds=merged_cfg["lease_seconds"],
    )
    if not job:
        return False

    job_id = str(job.get("job_id"))
    repo_id_raw = job.get("repo_id")
    job_type_raw = job.get("job_type")
    mode = str(job.get("mode", "incremental") or "incremental")
    payload = job.get("payload") or {}

    # 类型守卫：确保 repo_id 和 job_type 是有效值
    if not isinstance(repo_id_raw, int) or not isinstance(job_type_raw, str):
        return False
    repo_id: int = repo_id_raw
    job_type: str = job_type_raw

    # 尝试获取 (repo_id, job_type) 分布式锁
    # 锁确保同一 repo 的同一类型任务只有一个 worker 在执行
    lock_lease_seconds = int(merged_cfg["lease_seconds"])
    lock_acquired = False

    try:
        lock_acquired = scm_sync_lock.claim(
            repo_id=repo_id,
            job_type=job_type,
            worker_id=worker_id,
            lease_seconds=lock_lease_seconds,
            conn=conn,
        )
    except Exception:
        # 锁获取异常视为锁获取失败
        lock_acquired = False

    if not lock_acquired:
        # 锁被其他 worker 持有，无惩罚重入队
        requeue_without_penalty(
            job_id=job_id,
            worker_id=worker_id,
            reason=f"lock_held: (repo_id={repo_id}, job_type={job_type}) locked by another worker",
            conn=conn,
        )
        return True  # 返回 True 表示处理了任务（跳过）

    # ============ sync_runs 生命周期开始 ============
    # 1. 生成 run_id
    run_id = generate_run_id()

    # 2. 读取 cursor_before（同步前的游标位置）
    cursor_before = read_cursor_before(repo_id, job_type, payload, conn)

    # 3. 写入 sync_run_start（status=running）
    if enable_sync_runs:
        insert_sync_run_start(
            run_id=run_id,
            repo_id=repo_id,
            job_type=job_type,
            mode=mode,
            cursor_before=cursor_before,
            meta_json={"job_id": job_id, "worker_id": worker_id},
            conn=conn,
        )

    # 锁获取成功，执行任务（finally 中释放锁）
    result = None
    run_payload = None

    try:
        hb = HeartbeatManager(
            job_id=job_id,
            worker_id=worker_id,
            renew_interval_seconds=float(merged_cfg["renew_interval_seconds"]),
            lease_seconds=int(merged_cfg["lease_seconds"]),
            max_failures=int(merged_cfg["max_renew_failures"]),
        )

        with hb:
            result = execute_sync_job(job)
            # 将 cursor_before 注入到 result 中，用于 payload 构建
            if cursor_before:
                result["cursor_before"] = cursor_before

        # ============ 处理心跳中止 ============
        if hb.should_abort:
            abort_error = hb.get_abort_error()

            # 构建租约丢失的 payload
            run_payload = build_payload_for_lease_lost(
                job_id=job_id,
                worker_id=worker_id,
                failure_count=hb.failure_count,
                max_failures=hb.max_failures,
                last_error=hb.last_error,
                cursor_before=cursor_before,
            )

            # 验证 payload
            is_valid, errors, warnings = validate_run_finish_payload(run_payload)
            if not is_valid:
                # 验证失败时，确保 error_summary_json 存在
                from engram.logbook.scm_sync_run_contract import ErrorSummary

                if run_payload.error_summary is None:
                    run_payload.error_summary = ErrorSummary(
                        error_category="validation_error",
                        error_message=f"Payload validation failed: {'; '.join(errors)}",
                    )

            # 写入 sync_run_finish
            if enable_sync_runs:
                payload_dict = run_payload.to_dict()
                insert_sync_run_finish(
                    run_id=run_id,
                    status=payload_dict["status"],
                    counts=payload_dict.get("counts"),
                    error_summary_json=payload_dict.get("error_summary_json"),
                    conn=conn,
                )

            fail_retry(
                job_id=job_id,
                worker_id=worker_id,
                error=str(abort_error.get("error") or ""),
                error_category=str(abort_error.get("error_category") or "") or None,
                backoff_seconds=0,
                conn=conn,
            )
            return True

        # ============ 构建并验证 run finish payload ============
        run_payload = build_run_finish_payload_from_result(result)

        # 验证 payload（确保 failed 状态包含 error_summary_json）
        is_valid, errors, warnings = validate_run_finish_payload(run_payload)
        if not is_valid:
            # 验证失败，确保有 error_summary
            from engram.logbook.scm_sync_run_contract import ErrorSummary

            if run_payload.status == RunStatus.FAILED.value and run_payload.error_summary is None:
                run_payload.error_summary = ErrorSummary(
                    error_category="validation_error",
                    error_message=f"Payload validation failed: {'; '.join(errors)}",
                )

        # ============ 写入 sync_run_finish ============
        if enable_sync_runs:
            payload_dict = run_payload.to_dict()
            insert_sync_run_finish(
                run_id=run_id,
                status=payload_dict["status"],
                cursor_after=payload_dict.get("cursor_after"),
                counts=payload_dict.get("counts"),
                error_summary_json=payload_dict.get("error_summary_json"),
                degradation_json=payload_dict.get("degradation_json"),
                logbook_item_id=payload_dict.get("logbook_item_id"),
                conn=conn,
            )

        # ============ 根据结果处理任务状态 ============
        success = bool(result.get("success"))
        if success:
            # ack 时传入 run_id，写回 job.last_run_id
            ack(job_id=job_id, worker_id=worker_id, run_id=run_id, conn=conn)
            return True

        error_category = result.get("error_category") or ErrorCategory.UNKNOWN.value
        raw_error = result.get("error") or ""
        error_message = redact(raw_error)
        retry_after = result.get("retry_after")

        # 计算 backoff（优先使用 retry_after）
        if isinstance(retry_after, (int, float)) and retry_after > 0:
            backoff_seconds = int(retry_after)
        else:
            backoff_seconds = get_transient_error_backoff_wrapper(error_category, error_message)

        permanent = error_category in PERMANENT_ERROR_CATEGORIES
        if permanent and error_category == ErrorCategory.AUTH_ERROR.value:
            if "glpat-" in raw_error or "Bearer " in raw_error or "Authorization:" in raw_error:
                permanent = False

        if permanent:
            mark_dead(
                job_id=job_id,
                worker_id=worker_id,
                error=error_message,
                conn=conn,
            )
        elif error_category in IGNORED_ERROR_CATEGORIES:
            requeue_without_penalty(
                job_id=job_id,
                worker_id=worker_id,
                reason=error_message,
                conn=conn,
            )
        else:
            fail_retry(
                job_id,
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

    except Exception as exc:
        # 异常路径：构建异常 payload 并写入 sync_run_finish
        if enable_sync_runs:
            exc_payload = build_payload_for_exception(
                exc=exc,
                cursor_before=cursor_before,
            )
            payload_dict = exc_payload.to_dict()
            insert_sync_run_finish(
                run_id=run_id,
                status=payload_dict["status"],
                counts=payload_dict.get("counts"),
                error_summary_json=payload_dict.get("error_summary_json"),
                conn=conn,
            )

        # 重新抛出异常让上层处理
        raise

    finally:
        # 确保释放锁（无论成功、失败还是异常）
        if lock_acquired:
            try:
                scm_sync_lock.release(
                    repo_id=repo_id,
                    job_type=job_type,
                    worker_id=worker_id,
                    conn=conn,
                )
            except Exception:
                # 释放锁失败不应影响主流程
                pass
