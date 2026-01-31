#!/usr/bin/env python3
"""
兼容 CLI 入口（logbook_cli_main.py）

[DEPRECATED] 此脚本已弃用，将在后续版本移除。

提供 Typer app 以及 artifacts、scm 子命令，满足旧测试与工具调用。

权威入口:
    engram-logbook [command] [options]
    engram-artifacts [command] [options]
    python -m engram.logbook.cli.logbook [command] [options]
    python -m engram.logbook.cli.artifacts [command] [options]

注意: 此文件已弃用，artifacts 逻辑已迁移到 src/engram/logbook/cli/artifacts.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# 确保 src 目录在 sys.path 中，以支持导入包内模块
_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

# 确保根目录在 sys.path 中，以支持导入根目录模块
_ROOT_DIR = Path(__file__).resolve().parent.parent
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

import typer

# 从包内模块导入 artifacts app
from engram.logbook.cli.artifacts import app as artifacts_app

# 向后兼容：导出 make_ok_result, make_err_result 等辅助函数
from engram.logbook.cli.artifacts import (
    make_ok_result,
    make_err_result,
    _emit_json,
    _resolve_uri,
)

# 创建主 app
app = typer.Typer(add_completion=False)
scm_app = typer.Typer(add_completion=False, help="SCM 同步命令")


@scm_app.command("refresh-vfacts")
def scm_refresh_vfacts(
    dry_run: bool = typer.Option(False, "--dry-run"),
    concurrently: bool = typer.Option(False, "--concurrently"),
):
    """刷新 vfacts 视图"""
    import json
    import scm_sync_runner

    result = scm_sync_runner.refresh_vfacts(dry_run=dry_run, concurrently=concurrently)
    ok = result.get("refreshed", False) or dry_run
    output = make_ok_result(**result, ok=ok)
    typer.echo(json.dumps(output, ensure_ascii=False))
    raise typer.Exit(code=0 if ok else 1)


# 注册子命令
app.add_typer(artifacts_app, name="artifacts")
app.add_typer(scm_app, name="scm")


def main() -> None:
    """主入口函数"""
    _DEPRECATION_MSG = (
        "[DEPRECATED] scripts/logbook_cli_main.py 已弃用，将在后续版本移除。\n"
        "权威入口:\n"
        "  engram-logbook [command] [options]\n"
        "  engram-artifacts [command] [options]"
    )
    print(_DEPRECATION_MSG, file=sys.stderr)
    app()


if __name__ == "__main__":
    main()
