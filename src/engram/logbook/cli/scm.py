#!/usr/bin/env python3
"""
engram_logbook.cli.scm - SCM CLI 入口

这是 `engram-scm` 命令行工具的入口点，专注于 SCM 相关操作。

使用方式:
    engram-scm ensure-repo --repo-type git --repo-url https://gitlab.com/ns/proj --project-key my_project
    engram-scm sync-svn --repo-url svn://example.com/repo
    engram-scm sync-gitlab-commits --project-id 123
    engram-scm sync-gitlab-mrs --project-id 123
    engram-scm sync-gitlab-reviews --project-id 123
    engram-scm refresh-vfacts

安装后可直接使用:
    pip install -e .
    engram-scm --help
"""

import sys

import typer

# 延迟导入避免循环依赖
_scm_app = None


def get_scm_app() -> typer.Typer:
    """获取 SCM Typer 应用实例"""
    global _scm_app
    if _scm_app is None:
        from logbook_cli_main import scm_app
        _scm_app = scm_app
    return _scm_app


def main() -> None:
    """
    SCM CLI 主入口点
    
    直接运行 SCM 子应用，无需通过父应用。
    """
    app = get_scm_app()
    app()


if __name__ == "__main__":
    main()
