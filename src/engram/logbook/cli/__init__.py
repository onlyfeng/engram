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

from engram.logbook.cli.artifacts import main as artifacts_main
from engram.logbook.cli.db_bootstrap import main as bootstrap_main
from engram.logbook.cli.db_migrate import main as migrate_main
from engram.logbook.cli.logbook import main as logbook_main
from engram.logbook.cli.scm import main as scm_main
from engram.logbook.cli.scm_sync import (
    main as scm_sync_main,
)
from engram.logbook.cli.scm_sync import (
    reaper_main,
    scheduler_main,
    status_main,
    worker_main,
)

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
