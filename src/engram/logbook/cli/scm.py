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

import argparse


def main() -> None:
    """
    SCM CLI 主入口点
    
    TODO: 实现完整的 SCM CLI 功能
    """
    parser = argparse.ArgumentParser(
        prog="engram-scm",
        description="Engram SCM 同步命令行工具"
    )
    parser.add_argument("--version", action="version", version="engram-scm 0.1.0")
    
    subparsers = parser.add_subparsers(dest="command", help="可用命令")
    
    # ensure-repo 子命令
    ensure_parser = subparsers.add_parser("ensure-repo", help="确保仓库存在")
    ensure_parser.add_argument("--repo-type", required=True, help="仓库类型 (git/svn)")
    ensure_parser.add_argument("--repo-url", required=True, help="仓库 URL")
    ensure_parser.add_argument("--project-key", required=True, help="项目标识")
    
    # sync 子命令
    sync_parser = subparsers.add_parser("sync", help="同步仓库数据")
    sync_parser.add_argument("--repo-id", required=True, help="仓库 ID")
    
    args = parser.parse_args()
    
    if args.command == "ensure-repo":
        print("TODO: 实现 ensure-repo 命令")
    elif args.command == "sync":
        print("TODO: 实现 sync 命令")
    elif args.command is None:
        parser.print_help()


if __name__ == "__main__":
    main()
