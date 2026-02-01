#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scm_sync_worker - SCM 同步 Worker CLI 入口

⚠️ DEPRECATION NOTICE:
此脚本已弃用，将在未来版本中移除。
请使用以下方式替代:
    - python -m engram.logbook.cli.scm_sync worker [args]
    - engram-scm-sync worker [args]
    - engram-scm-worker [args]

核心实现位于: src/engram/logbook/scm_sync_worker_core.py

用法:
    python scripts/scm_sync_worker.py --worker-id worker-1
    python scripts/scm_sync_worker.py --worker-id worker-1 --once
    python scripts/scm_sync_worker.py --worker-id worker-1 --job-types commits,mrs
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
from engram.logbook.scm_sync_worker_core import (
    # 数据类
    HeartbeatManager,
    # 类型
    SyncExecutorType,
    default_sync_handler,
    execute_sync_job,
    fail_retry,
    generate_run_id,
    # 函数
    get_db_connection,
    get_executor,
    get_worker_config_from_module,
    insert_sync_run_finish,
    insert_sync_run_start,
    mark_dead,
    process_one_job,
    read_cursor_before,
    set_executor,
)
from engram.logbook.scm_sync_worker_core import (
    get_transient_error_backoff_wrapper as _get_transient_error_backoff,
)

# 额外导出（兼容旧代码）
_get_db_connection = get_db_connection
_generate_run_id = generate_run_id
_read_cursor_before = read_cursor_before
_insert_sync_run_start = insert_sync_run_start
_insert_sync_run_finish = insert_sync_run_finish


__all__ = [
    # 类型
    "SyncExecutorType",
    # 数据类
    "HeartbeatManager",
    # 函数
    "get_db_connection",
    "_get_db_connection",
    "generate_run_id",
    "_generate_run_id",
    "read_cursor_before",
    "_read_cursor_before",
    "insert_sync_run_start",
    "_insert_sync_run_start",
    "insert_sync_run_finish",
    "_insert_sync_run_finish",
    "mark_dead",
    "fail_retry",
    "get_worker_config_from_module",
    "_get_transient_error_backoff",
    "set_executor",
    "get_executor",
    "default_sync_handler",
    "execute_sync_job",
    "process_one_job",
    # CLI
    "main",
]


# ============ CLI 入口（转发到新模块） ============


def main():
    """CLI 入口函数 - 转发到 engram.logbook.cli.scm_sync.worker_main"""
    warnings.warn(
        "scripts/scm_sync_worker.py 已弃用，请使用 'python -m engram.logbook.cli.scm_sync worker' "
        "或 'engram-scm-worker' 代替",
        DeprecationWarning,
        stacklevel=2,
    )
    # 转发到新的 CLI 入口
    from engram.logbook.cli.scm_sync import worker_main
    return worker_main()


if __name__ == "__main__":
    sys.exit(main())
