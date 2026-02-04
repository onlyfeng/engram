"""
engram_logbook.cli - CLI 入口模块

包含以下命令行入口:
    - logbook: Logbook 操作主入口（argparse CLI）
    - artifacts: Artifacts 管理入口（typer CLI）
    - engram-scm: SCM 相关操作入口
    - migrate: 数据库迁移入口
    - bootstrap: 数据库 bootstrap（服务账号创建与预检）
    - scm_sync: SCM Sync 子系统统一入口

Artifacts 子命令:
    - engram-logbook artifacts write/read/exists/delete/gc/migrate
    - engram-artifacts（独立入口，等价于 engram-logbook artifacts）

SCM Sync 子系统入口:
    - engram-scm-sync: 统一入口（子命令: scheduler, worker, reaper, status）
    - engram-scm-scheduler: 调度器快捷入口
    - engram-scm-worker: Worker 快捷入口
    - engram-scm-reaper: 清理器快捷入口
    - engram-scm-status: 状态查询快捷入口
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "artifacts_main",
    "bootstrap_main",
    "migrate_main",
    "logbook_main",
    "scm_main",
    "scm_sync_main",
    "scheduler_main",
    "worker_main",
    "reaper_main",
    "status_main",
]


def __getattr__(name: str) -> Any:
    """
    Lazy import to avoid import-time side effects.

    Why:
    - `python -m engram.logbook.cli.db_bootstrap` 会先导入父包 `engram.logbook.cli`。
      如果这里 eager-import 子模块，会触发 runpy 的 warning（模块已在 sys.modules 中）。
    """
    if name == "artifacts_main":
        from .artifacts import main as _artifacts_main

        return _artifacts_main
    if name == "bootstrap_main":
        from .db_bootstrap import main as _bootstrap_main

        return _bootstrap_main
    if name == "migrate_main":
        from .db_migrate import main as _migrate_main

        return _migrate_main
    if name == "logbook_main":
        from .logbook import main as _logbook_main

        return _logbook_main
    if name == "scm_main":
        from .scm import main as _scm_main

        return _scm_main
    if name == "scm_sync_main":
        from .scm_sync import main as _scm_sync_main

        return _scm_sync_main
    if name == "scheduler_main":
        from .scm_sync import scheduler_main as _scheduler_main

        return _scheduler_main
    if name == "worker_main":
        from .scm_sync import worker_main as _worker_main

        return _worker_main
    if name == "reaper_main":
        from .scm_sync import reaper_main as _reaper_main

        return _reaper_main
    if name == "status_main":
        from .scm_sync import status_main as _status_main

        return _status_main
    raise AttributeError(name)
