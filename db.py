#!/usr/bin/env python3
"""
db - SCM/Logbook 数据库访问层兼容包装器

[DEPRECATED] 此模块已废弃，请使用 engram.logbook.scm_db 代替。

此模块保留用于向后兼容，所有功能已迁移至 engram.logbook.scm_db。
新代码应直接使用:

    from engram.logbook import scm_db as db_api

或者:

    from engram.logbook.scm_db import get_conn, upsert_repo, ...

此包装器将在未来版本中移除。
"""

from __future__ import annotations

import warnings

# 发出废弃警告
warnings.warn(
    "db.py 已废弃。请使用 'from engram.logbook import scm_db' 代替。"
    "此模块将在未来版本中移除。",
    DeprecationWarning,
    stacklevel=2,
)

# 从新模块重新导出所有 API
from engram.logbook.scm_db import (
    MATERIALIZE_STATUS_DONE,
    MATERIALIZE_STATUS_FAILED,
    # 常量
    MATERIALIZE_STATUS_PENDING,
    # 枚举
    PauseReasonCode,
    # 数据类
    RepoPauseRecord,
    build_circuit_breaker_key,
    delete_circuit_breaker_state,
    # Sync job 操作
    enqueue_sync_job,
    force_release_lock,
    get_active_job_pairs,
    get_budget_snapshot,
    # 连接函数
    get_conn,
    get_cursor_value,
    get_latest_sync_run,
    get_patch_blob,
    get_pause_snapshot,
    get_paused_job_pairs,
    get_rate_limit_bucket_status,
    get_repo_by_url,
    get_repo_job_pause,
    get_repo_sync_stats,
    get_sync_run,
    get_sync_runs_health_stats,
    # 状态/统计
    get_sync_status_summary,
    insert_review_event,
    insert_sync_run_finish,
    # Sync run 操作
    insert_sync_run_start,
    list_all_pauses,
    list_expired_locks,
    # 过期任务处理
    list_expired_running_jobs,
    list_expired_running_runs,
    # KV 操作
    list_kv_cursors,
    list_repos,
    list_repos_for_scheduling,
    list_sync_jobs,
    # Lock 操作
    list_sync_locks,
    list_sync_runs,
    load_circuit_breaker_state,
    mark_blob_done,
    mark_blob_failed,
    mark_job_as_dead_by_reaper,
    mark_job_as_failed_by_reaper,
    mark_run_as_failed_by_reaper,
    # Circuit breaker
    save_circuit_breaker_state,
    select_pending_blobs_for_materialize,
    # Pause 操作
    set_repo_job_pause,
    update_patch_blob_materialize_status,
    upsert_git_commit,
    # MR 操作
    upsert_mr,
    # Patch blob 操作
    upsert_patch_blob,
    # Repo 操作
    upsert_repo,
    # Commit/Revision 操作
    upsert_svn_revision,
)

__all__ = [
    # 常量
    "MATERIALIZE_STATUS_PENDING",
    "MATERIALIZE_STATUS_DONE",
    "MATERIALIZE_STATUS_FAILED",
    # 枚举
    "PauseReasonCode",
    # 数据类
    "RepoPauseRecord",
    # 连接函数
    "get_conn",
    # Repo 操作
    "upsert_repo",
    "get_repo_by_url",
    "list_repos",
    "list_repos_for_scheduling",
    # MR 操作
    "upsert_mr",
    "insert_review_event",
    # Commit/Revision 操作
    "upsert_svn_revision",
    "upsert_git_commit",
    # Patch blob 操作
    "upsert_patch_blob",
    "get_patch_blob",
    "update_patch_blob_materialize_status",
    "select_pending_blobs_for_materialize",
    "mark_blob_done",
    "mark_blob_failed",
    # Sync run 操作
    "insert_sync_run_start",
    "insert_sync_run_finish",
    "get_sync_run",
    "get_latest_sync_run",
    "list_sync_runs",
    # Sync job 操作
    "enqueue_sync_job",
    "list_sync_jobs",
    # Lock 操作
    "list_sync_locks",
    "list_expired_locks",
    "force_release_lock",
    # KV 操作
    "list_kv_cursors",
    "get_cursor_value",
    # 状态/统计
    "get_sync_status_summary",
    "get_repo_sync_stats",
    "get_active_job_pairs",
    "get_budget_snapshot",
    "get_rate_limit_bucket_status",
    "get_sync_runs_health_stats",
    # 过期任务处理
    "list_expired_running_jobs",
    "list_expired_running_runs",
    "mark_job_as_failed_by_reaper",
    "mark_job_as_dead_by_reaper",
    "mark_run_as_failed_by_reaper",
    # Circuit breaker
    "save_circuit_breaker_state",
    "load_circuit_breaker_state",
    "delete_circuit_breaker_state",
    "build_circuit_breaker_key",
    # Pause 操作
    "set_repo_job_pause",
    "get_repo_job_pause",
    "list_all_pauses",
    "get_paused_job_pairs",
    "get_pause_snapshot",
]
