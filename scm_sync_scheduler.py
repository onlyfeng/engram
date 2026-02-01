#!/usr/bin/env python3
"""
scm_sync_scheduler - SCM 同步调度器 CLI 入口

⚠️ DEPRECATION NOTICE:
此脚本已弃用，将在未来版本中移除。
请使用以下方式替代:
    - python -m engram.logbook.cli.scm_sync scheduler [args]
    - engram-scm-sync scheduler [args]
    - engram-scm-scheduler [args]

核心实现位于: src/engram/logbook/scm_sync_scheduler_core.py

用法:
    python scm_sync_scheduler.py --once           # 执行一次调度
    python scm_sync_scheduler.py --dry-run        # 干运行，不入队
    python scm_sync_scheduler.py --json           # JSON 输出摘要
"""

from __future__ import annotations

import sys
import warnings

# 导出核心模块的所有公共 API（向后兼容）
from engram.logbook.scm_sync_scheduler_core import (
    BuildJobsResult,
    BuildJobsSnapshot,
    SchedulerTickResult,
    # 数据类
    SkippedJob,
    # 函数
    build_jobs_to_insert,
    run_scheduler_tick,
)

__all__ = [
    # 数据类
    "SkippedJob",
    "BuildJobsSnapshot",
    "BuildJobsResult",
    "SchedulerTickResult",
    # 函数
    "build_jobs_to_insert",
    "run_scheduler_tick",
    # CLI
    "main",
]


# ============ CLI 入口（转发到新模块） ============


def main() -> int:
    """CLI 入口函数 - 转发到 engram.logbook.cli.scm_sync.scheduler_main"""
    warnings.warn(
        "scm_sync_scheduler.py 已弃用，请使用 'python -m engram.logbook.cli.scm_sync scheduler' "
        "或 'engram-scm-scheduler' 代替",
        DeprecationWarning,
        stacklevel=2,
    )
    # 转发到新的 CLI 入口
    from engram.logbook.cli.scm_sync import scheduler_main
    return scheduler_main()


if __name__ == "__main__":
    sys.exit(main())
