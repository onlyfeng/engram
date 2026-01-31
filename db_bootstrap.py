#!/usr/bin/env python3
"""
db_bootstrap - 弃用兼容入口（薄包装器）

[DEPRECATED] 此脚本已弃用。
计划移除: v2.0.0 或 2026-Q2

替代方案:
    engram-bootstrap-roles [args]
    python -m engram.logbook.cli.db_bootstrap [args]

兼容导出:
    此包装器通过 __getattr__ 支持延迟导入 engram.logbook.cli.db_bootstrap 中的符号。
"""

from __future__ import annotations

import sys

_DEPRECATION_MSG = """\
[DEPRECATED] db_bootstrap.py 已弃用。
计划移除: v2.0.0 或 2026-Q2

替代方案:
  engram-bootstrap-roles [args]
  python -m engram.logbook.cli.db_bootstrap [args]"""

# ---------------------------------------------------------------------------
# 兼容导出（延迟加载）
# ---------------------------------------------------------------------------


def __getattr__(name: str):
    """延迟加载 engram.logbook.cli.db_bootstrap 中的属性"""
    from engram.logbook.cli import db_bootstrap as impl
    return getattr(impl, name)


def main() -> None:
    """入口函数，打印弃用警告并转发到权威入口"""
    print(_DEPRECATION_MSG, file=sys.stderr)
    from engram.logbook.cli.db_bootstrap import main as cli_main
    cli_main()


if __name__ == "__main__":
    main()
