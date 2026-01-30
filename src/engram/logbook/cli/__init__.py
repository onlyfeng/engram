"""
engram_logbook.cli - CLI 入口模块

包含以下命令行入口:
    - logbook: Logbook 操作主入口（argparse CLI）
    - engram-scm: SCM 相关操作入口（Typer CLI）
    - migrate: 数据库迁移入口
"""

from engram.logbook.cli.db_migrate import main as migrate_main
from engram.logbook.cli.logbook import main as logbook_main
from engram.logbook.cli.scm import main as scm_main

__all__ = [
    "migrate_main",
    "logbook_main",
    "scm_main",
]
