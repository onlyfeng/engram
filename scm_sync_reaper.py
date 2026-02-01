#!/usr/bin/env python3
"""
scm_sync_reaper - SCM 同步任务回收器 CLI 入口

⚠️ DEPRECATION NOTICE:
此脚本已弃用，将在未来版本中移除。
请使用以下方式替代:
    - python -m engram.logbook.cli.scm_sync reaper [args]
    - engram-scm-sync reaper [args]
    - engram-scm-reaper [args]

核心实现位于: src/engram/logbook/scm_sync_reaper_core.py

用法:
    python scm_sync_reaper.py
    python scm_sync_reaper.py --dry-run
    python scm_sync_reaper.py --grace-seconds 120 --policy to_pending
"""

from __future__ import annotations

import sys
import warnings

# 导出核心模块的所有公共 API（向后兼容）
from engram.logbook.scm_sync_reaper_core import (
    # 常量
    DEFAULT_GRACE_SECONDS,
    DEFAULT_MAX_DURATION_SECONDS,
    DEFAULT_MAX_REAPER_BACKOFF_SECONDS,
    DEFAULT_RETRY_DELAY_SECONDS,
    # 枚举
    JobRecoveryPolicy,
    compute_backoff_seconds,
    # 函数
    format_error,
    mark_job_pending,
    process_expired_jobs,
    process_expired_locks,
    process_expired_runs,
    run_reaper,
)

# 兼容旧的别名
_format_error = format_error
_mark_job_pending = mark_job_pending
_compute_backoff_seconds = compute_backoff_seconds
DEFAULT_BACKOFF_BASE = 60  # 兼容旧代码引用


__all__ = [
    # 常量
    "DEFAULT_GRACE_SECONDS",
    "DEFAULT_MAX_DURATION_SECONDS",
    "DEFAULT_RETRY_DELAY_SECONDS",
    "DEFAULT_MAX_REAPER_BACKOFF_SECONDS",
    "DEFAULT_BACKOFF_BASE",
    # 枚举
    "JobRecoveryPolicy",
    # 函数
    "format_error",
    "_format_error",
    "mark_job_pending",
    "_mark_job_pending",
    "compute_backoff_seconds",
    "_compute_backoff_seconds",
    "process_expired_jobs",
    "process_expired_runs",
    "process_expired_locks",
    "run_reaper",
    # CLI
    "main",
]


# ============ 旧接口兼容 ============


def scan_expired_jobs(conn, *, grace_seconds: int = DEFAULT_GRACE_SECONDS):
    """
    扫描过期的 running jobs（兼容旧接口）

    新代码应直接使用 db.list_expired_running_jobs()
    """
    from engram.logbook import scm_db as db_api
    return db_api.list_expired_running_jobs(conn, grace_seconds=grace_seconds)


# ============ CLI 入口（转发到新模块） ============


def main() -> int:
    """CLI 入口函数 - 转发到 engram.logbook.cli.scm_sync.reaper_main"""
    warnings.warn(
        "scm_sync_reaper.py 已弃用，请使用 'python -m engram.logbook.cli.scm_sync reaper' "
        "或 'engram-scm-reaper' 代替",
        DeprecationWarning,
        stacklevel=2,
    )
    # 转发到新的 CLI 入口
    from engram.logbook.cli.scm_sync import reaper_main
    return reaper_main()


if __name__ == "__main__":
    sys.exit(main())
