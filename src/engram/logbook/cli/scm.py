#!/usr/bin/env python3
"""
engram_logbook.cli.scm - SCM CLI 入口

这是 `engram-scm` 命令行工具的入口点，专注于 SCM 相关操作。

使用方式:
    engram-scm ensure-repo --repo-type git --repo-url https://gitlab.com/ns/proj --project-key my_project
    engram-scm list-repos [--repo-type git]
    engram-scm get-repo --repo-type git --repo-url https://gitlab.com/ns/proj
    engram-scm get-repo --repo-id 123

SCM 同步操作请使用 engram-scm-sync 命令:
    engram-scm-sync scheduler --once
    engram-scm-sync worker --worker-id worker-1
    engram-scm-sync runner incremental --repo gitlab:123

安装后可直接使用:
    pip install -e .
    engram-scm --help

环境变量:
    POSTGRES_DSN: PostgreSQL 连接字符串
    ENGRAM_LOGBOOK_CONFIG: 配置文件路径
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Dict

from engram.logbook.errors import (
    EngramConfigError,
    EngramDatabaseError,
    EngramError,
    make_error_result,
    make_success_result,
)
from engram.logbook.io import add_output_arguments, get_output_options, output_json

# Exit codes
EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_INVALID_ARGS = 2
EXIT_NO_DSN = 3


def _get_dsn(args: argparse.Namespace) -> str:
    """
    获取数据库连接字符串

    优先级: --dsn 参数 > --config 文件 > POSTGRES_DSN 环境变量

    Raises:
        EngramConfigError: 无法获取 DSN
    """
    import os

    # 1. 命令行参数优先
    if hasattr(args, "dsn") and args.dsn:
        return str(args.dsn)

    # 2. 配置文件
    if hasattr(args, "config") and args.config:
        try:
            import tomllib  # type: ignore[import-not-found]
        except ImportError:
            import tomli as tomllib

        with open(args.config, "rb") as f:
            config = tomllib.load(f)
            dsn = config.get("postgres", {}).get("dsn")
            if dsn:
                return str(dsn)

    # 3. 环境变量
    dsn = os.environ.get("POSTGRES_DSN")
    if dsn:
        return dsn

    # EngramConfigError 默认 exit_code 是 2，我们需要 EXIT_NO_DSN = 3
    # 但由于 EngramConfigError 继承的 exit_code 是类属性，我们直接使用它
    error = EngramConfigError(
        "无法获取数据库连接字符串",
        {
            "hint": "请通过 --dsn 参数、--config 配置文件或 POSTGRES_DSN 环境变量指定",
            "example": "engram-scm ensure-repo --dsn 'postgresql://user:pass@localhost/db' ...",
        },
    )
    # 覆盖 exit_code 为 EXIT_NO_DSN
    error.exit_code = EXIT_NO_DSN
    raise error


def _handle_ensure_repo(args: argparse.Namespace, opts: Dict[str, Any]) -> int:
    """处理 ensure-repo 子命令"""
    from engram.logbook.scm_db import get_conn, upsert_repo

    try:
        dsn = _get_dsn(args)
    except EngramError as e:
        output_json(e.to_dict(), pretty=opts["pretty"], quiet=opts["quiet"])
        return e.exit_code

    try:
        with get_conn(dsn) as conn:
            repo_id = upsert_repo(
                conn,
                repo_type=args.repo_type,
                url=args.repo_url,
                project_key=args.project_key,
                default_branch=args.default_branch,
            )
            conn.commit()

        result = make_success_result(
            repo_id=repo_id,
            repo_type=args.repo_type,
            url=args.repo_url,
            project_key=args.project_key,
            default_branch=args.default_branch,
            message="仓库已确保存在",
        )
        output_json(result, pretty=opts["pretty"], quiet=opts["quiet"])
        return EXIT_SUCCESS

    except Exception as e:
        error = EngramDatabaseError(f"数据库操作失败: {e}", {"exception": str(e)})
        output_json(error.to_dict(), pretty=opts["pretty"], quiet=opts["quiet"])
        return EXIT_ERROR


def _handle_list_repos(args: argparse.Namespace, opts: Dict[str, Any]) -> int:
    """处理 list-repos 子命令"""
    from engram.logbook.scm_db import get_conn, list_repos

    try:
        dsn = _get_dsn(args)
    except EngramError as e:
        output_json(e.to_dict(), pretty=opts["pretty"], quiet=opts["quiet"])
        return e.exit_code

    try:
        with get_conn(dsn) as conn:
            repos = list_repos(
                conn,
                repo_type=args.repo_type if hasattr(args, "repo_type") else None,
                limit=args.limit if hasattr(args, "limit") else 100,
            )

        # 转换 datetime 对象为字符串
        for repo in repos:
            if repo.get("created_at"):
                repo["created_at"] = repo["created_at"].isoformat()

        result = make_success_result(
            repos=repos,
            count=len(repos),
        )
        output_json(result, pretty=opts["pretty"], quiet=opts["quiet"])
        return EXIT_SUCCESS

    except Exception as e:
        error = EngramDatabaseError(f"数据库操作失败: {e}", {"exception": str(e)})
        output_json(error.to_dict(), pretty=opts["pretty"], quiet=opts["quiet"])
        return EXIT_ERROR


def _handle_get_repo(args: argparse.Namespace, opts: Dict[str, Any]) -> int:
    """处理 get-repo 子命令"""
    from engram.logbook.scm_db import get_conn, get_repo_by_id, get_repo_by_url

    # 验证参数：要么提供 repo_id，要么提供 repo_type + repo_url
    has_id = hasattr(args, "repo_id") and args.repo_id is not None
    has_url = (
        hasattr(args, "repo_type")
        and args.repo_type
        and hasattr(args, "repo_url")
        and args.repo_url
    )

    if not has_id and not has_url:
        error = make_error_result(
            code="INVALID_ARGS",
            message="必须提供 --repo-id 或同时提供 --repo-type 和 --repo-url",
        )
        output_json(error, pretty=opts["pretty"], quiet=opts["quiet"])
        return EXIT_INVALID_ARGS

    try:
        dsn = _get_dsn(args)
    except EngramError as e:
        output_json(e.to_dict(), pretty=opts["pretty"], quiet=opts["quiet"])
        return e.exit_code

    try:
        with get_conn(dsn) as conn:
            if has_id:
                repo = get_repo_by_id(conn, args.repo_id)
            else:
                repo = get_repo_by_url(conn, args.repo_type, args.repo_url)

        if repo is None:
            error = make_error_result(
                code="REPO_NOT_FOUND",
                message="仓库不存在",
                detail={
                    "repo_id": args.repo_id if has_id else None,
                    "repo_type": args.repo_type if has_url else None,
                    "repo_url": args.repo_url if has_url else None,
                },
            )
            output_json(error, pretty=opts["pretty"], quiet=opts["quiet"])
            return EXIT_ERROR

        # 转换 datetime 对象为字符串
        if repo.get("created_at"):
            repo["created_at"] = repo["created_at"].isoformat()

        result = make_success_result(repo=repo)
        output_json(result, pretty=opts["pretty"], quiet=opts["quiet"])
        return EXIT_SUCCESS

    except Exception as e:
        db_error = EngramDatabaseError(f"数据库操作失败: {e}", {"exception": str(e)})
        output_json(db_error.to_dict(), pretty=opts["pretty"], quiet=opts["quiet"])
        return EXIT_ERROR


def _add_dsn_config_args(parser: argparse.ArgumentParser) -> None:
    """为 parser 添加 --dsn 和 --config 参数"""
    parser.add_argument(
        "--dsn",
        metavar="DSN",
        help="PostgreSQL 连接字符串（优先级高于配置文件和环境变量）",
    )
    parser.add_argument(
        "--config",
        "-c",
        metavar="PATH",
        help="配置文件路径（TOML 格式，读取 [postgres].dsn）",
    )


def main() -> int:
    """
    SCM CLI 主入口点
    """
    parser = argparse.ArgumentParser(
        prog="engram-scm",
        description="Engram SCM 同步命令行工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
环境变量:
  POSTGRES_DSN              PostgreSQL 连接字符串
  ENGRAM_LOGBOOK_CONFIG     配置文件路径

示例:
  # 确保仓库存在
  engram-scm ensure-repo --repo-type git --repo-url https://gitlab.com/ns/proj --project-key my_project

  # 列出所有仓库
  engram-scm list-repos --dsn "postgresql://user:pass@localhost/db"

  # 按 URL 查询仓库
  engram-scm get-repo --repo-type git --repo-url https://gitlab.com/ns/proj

  # 按 ID 查询仓库
  engram-scm get-repo --repo-id 123
""",
    )
    parser.add_argument("--version", action="version", version="engram-scm 0.1.0")
    add_output_arguments(parser)

    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # ensure-repo 子命令
    ensure_parser = subparsers.add_parser(
        "ensure-repo",
        help="确保仓库存在（幂等操作）",
        description="如果仓库不存在则创建，存在则更新可选字段。使用 repo_type + url 作为唯一标识。",
    )
    ensure_parser.add_argument(
        "--repo-type",
        required=True,
        choices=["git", "svn"],
        help="仓库类型 (git/svn)",
    )
    ensure_parser.add_argument(
        "--repo-url",
        required=True,
        help="仓库 URL",
    )
    ensure_parser.add_argument(
        "--project-key",
        help="项目标识（可选）",
    )
    ensure_parser.add_argument(
        "--default-branch",
        help="默认分支（可选，git 仓库推荐设置）",
    )
    _add_dsn_config_args(ensure_parser)
    add_output_arguments(ensure_parser)

    # list-repos 子命令
    list_parser = subparsers.add_parser(
        "list-repos",
        help="列出仓库",
        description="列出已注册的仓库，可按类型过滤。",
    )
    list_parser.add_argument(
        "--repo-type",
        choices=["git", "svn"],
        help="按仓库类型过滤",
    )
    list_parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="返回数量限制（默认 100）",
    )
    _add_dsn_config_args(list_parser)
    add_output_arguments(list_parser)

    # get-repo 子命令
    get_parser = subparsers.add_parser(
        "get-repo",
        help="查询单个仓库",
        description="通过 ID 或 URL 查询仓库详情。",
    )
    get_parser.add_argument(
        "--repo-id",
        type=int,
        help="仓库 ID",
    )
    get_parser.add_argument(
        "--repo-type",
        choices=["git", "svn"],
        help="仓库类型（与 --repo-url 配合使用）",
    )
    get_parser.add_argument(
        "--repo-url",
        help="仓库 URL（与 --repo-type 配合使用）",
    )
    _add_dsn_config_args(get_parser)
    add_output_arguments(get_parser)

    args = parser.parse_args()
    opts = get_output_options(args)

    if args.command == "ensure-repo":
        return _handle_ensure_repo(args, opts)
    elif args.command == "list-repos":
        return _handle_list_repos(args, opts)
    elif args.command == "get-repo":
        return _handle_get_repo(args, opts)
    elif args.command is None:
        parser.print_help()
        return EXIT_SUCCESS
    else:
        output_json(
            make_error_result(code="UNKNOWN_COMMAND", message=f"未知命令: {args.command}"),
            pretty=opts["pretty"],
            quiet=opts["quiet"],
        )
        return EXIT_INVALID_ARGS


if __name__ == "__main__":
    sys.exit(main())
