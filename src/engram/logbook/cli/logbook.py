#!/usr/bin/env python3
"""
engram_logbook.cli.logbook - Logbook CLI 主入口

这是 `logbook` 命令行工具的入口点。

使用方式:
    logbook create_item --item-type task --title "My Task"
    logbook add_event --item-id 1 --event-type comment
    logbook health
    logbook validate
    logbook render_views

安装后可直接使用:
    pip install -e .
    logbook --help
"""

import sys


def main() -> int:
    """
    Logbook CLI 主入口点
    
    委托给 logbook_cli 模块的 main 函数。
    """
    # 导入放在函数内部，避免循环导入
    from logbook_cli import main as logbook_main
    return logbook_main()


if __name__ == "__main__":
    sys.exit(main())
