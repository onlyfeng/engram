#!/usr/bin/env python3
"""
db_migrate - 弃用兼容入口（薄包装器）

[DEPRECATED] 此脚本已弃用。
计划移除: v2.0.0 或 2026-Q2

替代方案:
    engram-migrate [args]
    python -m engram.logbook.cli.db_migrate [args]

兼容导出:
    此包装器直接导出 engram.logbook.migrate 中的常用符号，以支持测试导入。
"""

from __future__ import annotations

import sys

_DEPRECATION_MSG = """\
[DEPRECATED] logbook_postgres/scripts/db_migrate.py 已弃用。
计划移除: v2.0.0 或 2026-Q2

替代方案:
  engram-migrate [args]
  python -m engram.logbook.cli.db_migrate [args]"""

# ---------------------------------------------------------------------------
# 兼容导出（直接导入，供测试使用）
# ---------------------------------------------------------------------------


def main() -> None:
    """入口函数，打印弃用警告并转发到权威入口"""
    print(_DEPRECATION_MSG, file=sys.stderr)
    from engram.logbook.cli.db_migrate import main as cli_main
    cli_main()


if __name__ == "__main__":
    main()
