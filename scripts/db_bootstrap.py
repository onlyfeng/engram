#!/usr/bin/env python3
"""
db_bootstrap - 数据库 bootstrap 预检与角色创建（薄包装器）

[DEPRECATED] 此脚本为运维辅助入口，核心逻辑已迁移至包内模块。

权威入口:
    engram-bootstrap-roles
    python -m engram.logbook.cli.db_bootstrap
    
迁移指引:
    旧命令: python scripts/db_bootstrap.py [args]
    新命令: engram-bootstrap-roles [args]
            python -m engram.logbook.cli.db_bootstrap [args]
    
    参数与错误码保持完全一致，无需修改调用方式。
"""

from __future__ import annotations


def main() -> None:
    """调用包内 CLI 入口"""
    from engram.logbook.cli.db_bootstrap import main as cli_main
    cli_main()


# 兼容导出：允许 from scripts.db_bootstrap import BootstrapErrorCode 等
def __getattr__(name):
    """延迟兼容导出（按需加载）"""
    from engram.logbook.cli import db_bootstrap as impl
    return getattr(impl, name)


if __name__ == "__main__":
    main()
