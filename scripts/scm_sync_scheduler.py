#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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
    python scripts/scm_sync_scheduler.py --once
    python scripts/scm_sync_scheduler.py --dry-run
    python scripts/scm_sync_scheduler.py --json
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
    "BuildJobsSnapshot",
    "BuildJobsResult",
    "SkippedJob",
    "SchedulerTickResult",
    "build_jobs_to_insert",
    "run_scheduler_tick",
    "main",
]


# ============ CLI 入口（转发到新模块） ============


def main():
    """CLI 入口函数 - 转发到 engram.logbook.cli.scm_sync.scheduler_main"""
    warnings.warn(
        "scripts/scm_sync_scheduler.py 已弃用，请使用 'python -m engram.logbook.cli.scm_sync scheduler' "
        "或 'engram-scm-scheduler' 代替",
        DeprecationWarning,
        stacklevel=2,
    )
    # 转发到新的 CLI 入口
    from engram.logbook.cli.scm_sync import scheduler_main
    return scheduler_main()


if __name__ == "__main__":
    sys.exit(main())
