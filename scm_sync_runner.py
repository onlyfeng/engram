#!/usr/bin/env python3
"""
scm_sync_runner - SCM 同步运行器 CLI 入口

⚠️ DEPRECATION NOTICE:
此脚本已弃用，将在未来版本中移除。
请使用以下方式替代:
    - python -m engram.logbook.cli.scm_sync runner [args]
    - engram-scm-sync runner [args]
    - engram-scm-runner [args]

核心实现位于: src/engram/logbook/scm_sync_runner.py

用法:
    python scm_sync_runner.py incremental --repo gitlab:123
    python scm_sync_runner.py backfill --repo gitlab:123 --last-hours 24
    python scm_sync_runner.py config --show-backfill
"""

from __future__ import annotations

import sys
import warnings

# 导出核心模块的所有公共 API（向后兼容）
from engram.logbook.scm_sync_runner import (
    # 常量
    REPO_TYPE_GITLAB,
    REPO_TYPE_SVN,
    VALID_REPO_TYPES,
    JOB_TYPE_COMMITS,
    JOB_TYPE_MRS,
    JOB_TYPE_REVIEWS,
    VALID_JOB_TYPES,
    DEFAULT_REPAIR_WINDOW_HOURS,
    DEFAULT_LOOP_INTERVAL_SECONDS,
    DEFAULT_WINDOW_CHUNK_HOURS,
    DEFAULT_WINDOW_CHUNK_REVS,
    EXIT_SUCCESS,
    EXIT_PARTIAL,
    EXIT_FAILED,
    # 枚举
    RunnerStatus,
    RunnerPhase,
    # 异常
    WatermarkConstraintError,
    # 数据类
    RepoSpec,
    JobSpec,
    BackfillConfig,
    IncrementalConfig,
    RunnerContext,
    SyncResult,
    TimeWindowChunk,
    RevisionWindowChunk,
    AggregatedResult,
    # 函数
    split_time_window,
    split_revision_window,
    calculate_backfill_window,
    validate_watermark_constraint,
    get_script_path,
    build_sync_command,
    get_connection,
    refresh_vfacts,
    get_exit_code,
    parse_args,
    # 类
    SyncRunner,
)


__all__ = [
    # 常量
    "REPO_TYPE_GITLAB",
    "REPO_TYPE_SVN",
    "VALID_REPO_TYPES",
    "JOB_TYPE_COMMITS",
    "JOB_TYPE_MRS",
    "JOB_TYPE_REVIEWS",
    "VALID_JOB_TYPES",
    "DEFAULT_REPAIR_WINDOW_HOURS",
    "DEFAULT_LOOP_INTERVAL_SECONDS",
    "DEFAULT_WINDOW_CHUNK_HOURS",
    "DEFAULT_WINDOW_CHUNK_REVS",
    "EXIT_SUCCESS",
    "EXIT_PARTIAL",
    "EXIT_FAILED",
    # 枚举
    "RunnerStatus",
    "RunnerPhase",
    # 异常
    "WatermarkConstraintError",
    # 数据类
    "RepoSpec",
    "JobSpec",
    "BackfillConfig",
    "IncrementalConfig",
    "RunnerContext",
    "SyncResult",
    "TimeWindowChunk",
    "RevisionWindowChunk",
    "AggregatedResult",
    # 函数
    "split_time_window",
    "split_revision_window",
    "calculate_backfill_window",
    "validate_watermark_constraint",
    "get_script_path",
    "build_sync_command",
    "get_connection",
    "refresh_vfacts",
    "get_exit_code",
    # 类
    "SyncRunner",
    # CLI
    "main",
    "parse_args",
]


# ============ CLI 入口（转发到新模块） ============


def main() -> int:
    """CLI 入口函数 - 转发到 engram.logbook.cli.scm_sync.runner_main"""
    warnings.warn(
        "scm_sync_runner.py 已弃用，请使用 'python -m engram.logbook.cli.scm_sync runner' "
        "或 'engram-scm-runner' 代替",
        DeprecationWarning,
        stacklevel=2,
    )
    # 转发到新的 CLI 入口
    from engram.logbook.cli.scm_sync import runner_main
    return runner_main()


if __name__ == "__main__":
    sys.exit(main())
