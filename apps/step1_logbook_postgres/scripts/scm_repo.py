#!/usr/bin/env python3
"""
scm_repo.py - SCM 仓库管理模块

提供仓库的 upsert 操作，确保仓库记录存在并返回 repo_id。

使用:
    # 作为模块导入
    from scm_repo import ensure_repo
    repo_id = ensure_repo("git", "https://gitlab.example.com/ns/proj", "my_project")

    # 作为 CLI 使用
    python scm_repo.py ensure --repo-type git --url "https://gitlab.example.com/ns/proj" --project-key my_project
"""

import argparse
import json
import logging
import sys
from typing import Optional

import psycopg

from engram_step1.config import Config, add_config_argument, get_config
from engram_step1.db import get_connection
from engram_step1.errors import DatabaseError, EngramError, ValidationError

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# 有效的仓库类型
VALID_REPO_TYPES = ("svn", "git")


# ============ ID 构建工具函数 ============


def build_mr_id(repo_id: int, mr_iid: int) -> str:
    """
    构建全局唯一的 mr_id

    格式: <repo_id>:<mr_iid>

    Args:
        repo_id: 数据库中的仓库 ID
        mr_iid: MR 在仓库内的 IID（GitLab 项目内唯一 ID）

    Returns:
        全局唯一的 mr_id 字符串
    """
    return f"{repo_id}:{mr_iid}"


class ScmRepoError(EngramError):
    """SCM 仓库操作错误"""
    exit_code = 12
    error_type = "SCM_REPO_ERROR"


def ensure_repo(
    repo_type: str,
    url: str,
    project_key: str,
    default_branch: Optional[str] = None,
    config: Optional[Config] = None,
) -> int:
    """
    确保仓库记录存在，对 (repo_type, url) 做 upsert 并返回 repo_id。

    如果仓库已存在，会更新 project_key 和 default_branch（如果提供）。
    如果仓库不存在，会创建新记录。

    Args:
        repo_type: 仓库类型，必须为 'svn' 或 'git'
        url: 仓库 URL（唯一标识）
        project_key: 项目标识
        default_branch: 默认分支（可选）
        config: 配置实例

    Returns:
        repo_id (int)

    Raises:
        ValidationError: repo_type 无效时
        DatabaseError: 数据库操作失败时
    """
    # 验证 repo_type
    if repo_type not in VALID_REPO_TYPES:
        raise ValidationError(
            f"无效的仓库类型: {repo_type}，必须为 {VALID_REPO_TYPES}",
            {"repo_type": repo_type, "valid_types": VALID_REPO_TYPES},
        )

    # 验证 url
    if not url or not url.strip():
        raise ValidationError(
            "仓库 URL 不能为空",
            {"url": url},
        )

    # 验证 project_key
    if not project_key or not project_key.strip():
        raise ValidationError(
            "project_key 不能为空",
            {"project_key": project_key},
        )

    # 规范化 URL：去除空白和尾随斜杠
    url = url.strip().rstrip("/")
    project_key = project_key.strip()

    conn = get_connection(config=config)
    try:
        with conn.cursor() as cur:
            # 使用 PostgreSQL 的 INSERT ... ON CONFLICT ... DO UPDATE 实现 upsert
            # 如果 (repo_type, url) 已存在，更新 project_key 和 default_branch
            # 如果不存在，创建新记录
            cur.execute(
                """
                INSERT INTO scm.repos (repo_type, url, project_key, default_branch)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (repo_type, url) DO UPDATE SET
                    project_key = EXCLUDED.project_key,
                    default_branch = COALESCE(EXCLUDED.default_branch, scm.repos.default_branch)
                RETURNING repo_id
                """,
                (repo_type, url, project_key, default_branch),
            )
            result = cur.fetchone()
            conn.commit()

            repo_id = result[0]
            logger.debug(f"ensure_repo: repo_id={repo_id}, repo_type={repo_type}, url={url}")
            return repo_id

    except psycopg.Error as e:
        conn.rollback()
        raise DatabaseError(
            f"仓库 upsert 失败: {e}",
            {"repo_type": repo_type, "url": url, "error": str(e)},
        )
    finally:
        conn.close()


def get_repo_by_id(
    repo_id: int,
    config: Optional[Config] = None,
) -> Optional[dict]:
    """
    根据 repo_id 获取仓库信息

    Args:
        repo_id: 仓库 ID
        config: 配置实例

    Returns:
        仓库信息字典，不存在返回 None
    """
    conn = get_connection(config=config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT repo_id, repo_type, url, project_key, default_branch, created_at
                FROM scm.repos
                WHERE repo_id = %s
                """,
                (repo_id,),
            )
            row = cur.fetchone()
            if row:
                return {
                    "repo_id": row[0],
                    "repo_type": row[1],
                    "url": row[2],
                    "project_key": row[3],
                    "default_branch": row[4],
                    "created_at": row[5],
                }
            return None
    except psycopg.Error as e:
        raise DatabaseError(
            f"查询仓库失败: {e}",
            {"repo_id": repo_id, "error": str(e)},
        )
    finally:
        conn.close()


def get_repo_by_url(
    repo_type: str,
    url: str,
    config: Optional[Config] = None,
) -> Optional[dict]:
    """
    根据 (repo_type, url) 获取仓库信息

    Args:
        repo_type: 仓库类型
        url: 仓库 URL
        config: 配置实例

    Returns:
        仓库信息字典，不存在返回 None
    """
    conn = get_connection(config=config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT repo_id, repo_type, url, project_key, default_branch, created_at
                FROM scm.repos
                WHERE repo_type = %s AND url = %s
                """,
                (repo_type, url),
            )
            row = cur.fetchone()
            if row:
                return {
                    "repo_id": row[0],
                    "repo_type": row[1],
                    "url": row[2],
                    "project_key": row[3],
                    "default_branch": row[4],
                    "created_at": row[5],
                }
            return None
    except psycopg.Error as e:
        raise DatabaseError(
            f"查询仓库失败: {e}",
            {"repo_type": repo_type, "url": url, "error": str(e)},
        )
    finally:
        conn.close()


def list_repos(
    repo_type: Optional[str] = None,
    project_key: Optional[str] = None,
    limit: int = 100,
    config: Optional[Config] = None,
) -> list:
    """
    列出仓库

    Args:
        repo_type: 按类型过滤（可选）
        project_key: 按项目标识过滤（可选）
        limit: 返回数量限制
        config: 配置实例

    Returns:
        仓库信息列表
    """
    conn = get_connection(config=config)
    try:
        with conn.cursor() as cur:
            query = """
                SELECT repo_id, repo_type, url, project_key, default_branch, created_at
                FROM scm.repos
                WHERE 1=1
            """
            params = []

            if repo_type:
                query += " AND repo_type = %s"
                params.append(repo_type)

            if project_key:
                query += " AND project_key = %s"
                params.append(project_key)

            query += " ORDER BY created_at DESC LIMIT %s"
            params.append(limit)

            cur.execute(query, params)
            rows = cur.fetchall()

            return [
                {
                    "repo_id": row[0],
                    "repo_type": row[1],
                    "url": row[2],
                    "project_key": row[3],
                    "default_branch": row[4],
                    "created_at": row[5],
                }
                for row in rows
            ]
    except psycopg.Error as e:
        raise DatabaseError(
            f"查询仓库列表失败: {e}",
            {"error": str(e)},
        )
    finally:
        conn.close()


# ============ CLI 部分 ============


def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="SCM 仓库管理工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # 确保仓库存在（upsert）
    python scm_repo.py ensure --repo-type git --url "https://gitlab.example.com/ns/proj" --project-key my_project

    # 带默认分支
    python scm_repo.py ensure --repo-type git --url "https://gitlab.example.com/ns/proj" --project-key my_project --default-branch main

    # 查询仓库
    python scm_repo.py get --repo-id 1

    # 列出所有仓库
    python scm_repo.py list

    # 列出指定类型的仓库
    python scm_repo.py list --repo-type git
        """,
    )

    add_config_argument(parser)

    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="显示详细输出",
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出结果",
    )

    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # ensure 子命令
    ensure_parser = subparsers.add_parser("ensure", help="确保仓库存在（upsert）")
    ensure_parser.add_argument(
        "--repo-type",
        type=str,
        required=True,
        choices=VALID_REPO_TYPES,
        help="仓库类型 (svn/git)",
    )
    ensure_parser.add_argument(
        "--url",
        type=str,
        required=True,
        help="仓库 URL",
    )
    ensure_parser.add_argument(
        "--project-key",
        type=str,
        required=True,
        help="项目标识",
    )
    ensure_parser.add_argument(
        "--default-branch",
        type=str,
        default=None,
        help="默认分支",
    )

    # get 子命令
    get_parser = subparsers.add_parser("get", help="获取仓库信息")
    get_group = get_parser.add_mutually_exclusive_group(required=True)
    get_group.add_argument(
        "--repo-id",
        type=int,
        help="仓库 ID",
    )
    get_group.add_argument(
        "--url",
        type=str,
        help="仓库 URL（需同时指定 --repo-type）",
    )
    get_parser.add_argument(
        "--repo-type",
        type=str,
        choices=VALID_REPO_TYPES,
        help="仓库类型（与 --url 一起使用）",
    )

    # list 子命令
    list_parser = subparsers.add_parser("list", help="列出仓库")
    list_parser.add_argument(
        "--repo-type",
        type=str,
        choices=VALID_REPO_TYPES,
        help="按类型过滤",
    )
    list_parser.add_argument(
        "--project-key",
        type=str,
        help="按项目标识过滤",
    )
    list_parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="返回数量限制 (默认: 100)",
    )

    return parser.parse_args()


def main() -> int:
    """主入口"""
    args = parse_args()

    # 设置日志级别
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if not args.command:
        print("错误: 请指定子命令 (ensure/get/list)")
        print("使用 --help 查看帮助")
        return 1

    try:
        # 加载配置
        config = get_config(args.config_path)
        config.load()

        if args.command == "ensure":
            repo_id = ensure_repo(
                repo_type=args.repo_type,
                url=args.url,
                project_key=args.project_key,
                default_branch=args.default_branch,
                config=config,
            )

            if args.json:
                print(json.dumps({
                    "success": True,
                    "repo_id": repo_id,
                    "repo_type": args.repo_type,
                    "url": args.url,
                    "project_key": args.project_key,
                    "default_branch": args.default_branch,
                }, ensure_ascii=False))
            else:
                logger.info(f"仓库已确保存在: repo_id={repo_id}")
                print(f"repo_id={repo_id}")

        elif args.command == "get":
            if args.repo_id:
                repo = get_repo_by_id(args.repo_id, config=config)
            else:
                if not args.repo_type:
                    print("错误: 使用 --url 时必须同时指定 --repo-type")
                    return 1
                repo = get_repo_by_url(args.repo_type, args.url, config=config)

            if repo:
                if args.json:
                    print(json.dumps(repo, default=str, ensure_ascii=False))
                else:
                    print(f"repo_id: {repo['repo_id']}")
                    print(f"repo_type: {repo['repo_type']}")
                    print(f"url: {repo['url']}")
                    print(f"project_key: {repo['project_key']}")
                    print(f"default_branch: {repo['default_branch']}")
                    print(f"created_at: {repo['created_at']}")
            else:
                if args.json:
                    print(json.dumps({"error": "仓库不存在"}, ensure_ascii=False))
                else:
                    print("仓库不存在")
                return 1

        elif args.command == "list":
            repos = list_repos(
                repo_type=args.repo_type,
                project_key=args.project_key,
                limit=args.limit,
                config=config,
            )

            if args.json:
                print(json.dumps(repos, default=str, ensure_ascii=False))
            else:
                if not repos:
                    print("无仓库记录")
                else:
                    for repo in repos:
                        print(f"[{repo['repo_id']}] {repo['repo_type']}:{repo['url']} ({repo['project_key']})")

        return 0

    except EngramError as e:
        if args.json:
            print(json.dumps(e.to_dict(), default=str, ensure_ascii=False))
        else:
            logger.error(f"{e.error_type}: {e.message}")
            if args.verbose and e.details:
                logger.error(f"详情: {e.details}")
        return e.exit_code

    except Exception as e:
        logger.exception(f"未预期的错误: {e}")
        if args.json:
            print(json.dumps({
                "error": True,
                "type": "UNEXPECTED_ERROR",
                "message": str(e),
            }, default=str, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
