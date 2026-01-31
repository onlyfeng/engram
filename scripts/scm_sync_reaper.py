#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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
    python scripts/scm_sync_reaper.py
    python scripts/scm_sync_reaper.py --dry-run
    python scripts/scm_sync_reaper.py --grace-seconds 120 --policy to_pending
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

# 确保根目录在 sys.path 中，以支持导入根目录模块
_ROOT_DIR = Path(__file__).resolve().parent.parent
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

# 导出核心模块的所有公共 API（向后兼容）
from engram.logbook.scm_sync_reaper_core import (
    # 常量
    DEFAULT_GRACE_SECONDS,
    DEFAULT_MAX_DURATION_SECONDS,
    DEFAULT_RETRY_DELAY_SECONDS,
    DEFAULT_MAX_REAPER_BACKOFF_SECONDS,
    # 枚举
    JobRecoveryPolicy,
    # 函数
    format_error,
    mark_job_pending,
    compute_backoff_seconds,
    process_expired_jobs,
    process_expired_runs,
    process_expired_locks,
    run_reaper,
)

# 兼容旧的别名
_format_error = format_error
_mark_job_pending = mark_job_pending
_compute_backoff_seconds = compute_backoff_seconds


__all__ = [
    # 常量
    "DEFAULT_GRACE_SECONDS",
    "DEFAULT_MAX_DURATION_SECONDS",
    "DEFAULT_RETRY_DELAY_SECONDS",
    "DEFAULT_MAX_REAPER_BACKOFF_SECONDS",
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


# ============ CLI 入口（转发到新模块） ============


def main():
    """CLI 入口函数 - 转发到 engram.logbook.cli.scm_sync.reaper_main"""
    warnings.warn(
        "scripts/scm_sync_reaper.py 已弃用，请使用 'python -m engram.logbook.cli.scm_sync reaper' "
        "或 'engram-scm-reaper' 代替",
        DeprecationWarning,
        stacklevel=2,
    )
    # 转发到新的 CLI 入口
    from engram.logbook.cli.scm_sync import reaper_main
    return reaper_main()


if __name__ == "__main__":
    sys.exit(main())
