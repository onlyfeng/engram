#!/usr/bin/env python3
"""
artifact_cli - 弃用兼容入口（薄包装器）

[DEPRECATED] 此脚本已弃用，将在后续版本移除。

权威入口:
    engram-artifacts [command] [options]
    engram-logbook artifacts [command] [options]
    python -m engram.logbook.cli.artifacts [command] [options]

入口策略说明:
    - pyproject.toml [project.scripts] 定义的 engram-artifacts 为权威入口
    - engram-logbook artifacts 子命令也是权威入口
    - scripts/ 仅保留运维/CI 辅助脚本
    - 根目录与 logbook_postgres/scripts/ 仅保留 import 转发的薄包装器
"""

from __future__ import annotations

import sys
from pathlib import Path

_DEPRECATION_MSG = (
    "[DEPRECATED] scripts/artifact_cli.py 已弃用，将在后续版本移除。\n"
    "权威入口:\n"
    "  engram-artifacts [command] [options]\n"
    "  engram-logbook artifacts [command] [options]\n"
    "  python -m engram.logbook.cli.artifacts [command] [options]"
)

# 确保 src 目录在 sys.path 中
_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

# 直接导入以支持 `from artifact_cli import app, artifacts_app` 兼容导入
from engram.logbook.cli.artifacts import app
from engram.logbook.cli.artifacts import main as artifacts_main

# 兼容别名
artifacts_app = app


def main() -> None:
    """入口函数，打印弃用警告并转发到权威入口"""
    print(_DEPRECATION_MSG, file=sys.stderr)
    artifacts_main()


if __name__ == "__main__":
    main()
