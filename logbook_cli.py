#!/usr/bin/env python3
"""
logbook_cli - 弃用兼容入口（薄包装器，纯 CLI 转发）

[DEPRECATED] 此脚本已弃用。
计划移除: v2.0.0 或 2026-Q2

替代方案:
    engram-logbook [command] [options]
    python -m engram.logbook.cli.logbook [command] [options]

注意:
    此包装器仅支持 CLI 执行，不提供兼容导出。
"""

from __future__ import annotations

import sys

_DEPRECATION_MSG = """\
[DEPRECATED] logbook_cli.py 已弃用。
计划移除: v2.0.0 或 2026-Q2

替代方案:
  engram-logbook [command] [options]
  python -m engram.logbook.cli.logbook [command] [options]"""


def main() -> None:
    """入口函数，打印弃用警告并转发到权威入口"""
    print(_DEPRECATION_MSG, file=sys.stderr)
    from engram.logbook.cli.logbook import main as cli_main
    raise SystemExit(cli_main())


if __name__ == "__main__":
    main()
