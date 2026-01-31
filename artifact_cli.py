#!/usr/bin/env python3
"""
artifact_cli - 弃用兼容入口（薄包装器）

[DEPRECATED] 此脚本已弃用。
计划移除: v2.0.0 或 2026-Q2

替代方案:
    engram-artifacts [command] [options]
    engram-logbook artifacts [command] [options]
    python -m engram.logbook.cli.artifacts [command] [options]

兼容导出:
    此包装器支持 `from artifact_cli import app, artifacts_app` 兼容导入。
"""

from __future__ import annotations

import sys

_DEPRECATION_MSG = """\
[DEPRECATED] artifact_cli.py 已弃用。
计划移除: v2.0.0 或 2026-Q2

替代方案:
  engram-artifacts [command] [options]
  engram-logbook artifacts [command] [options]
  python -m engram.logbook.cli.artifacts [command] [options]"""

# ---------------------------------------------------------------------------
# 兼容导出（直接导入，无需 sys.path 操作）
# ---------------------------------------------------------------------------
from engram.logbook.cli.artifacts import app, main as artifacts_main

# 兼容别名
artifacts_app = app


def main() -> None:
    """入口函数，打印弃用警告并转发到权威入口"""
    print(_DEPRECATION_MSG, file=sys.stderr)
    artifacts_main()


if __name__ == "__main__":
    main()
