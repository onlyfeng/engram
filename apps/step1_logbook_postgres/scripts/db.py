"""
db.py - SCM 数据库操作模块

提供数据库连接和 SCM 相关表的 upsert 操作。

多租户隔离:
- SQL 使用无前缀表名，依赖连接时设置的 search_path
- 确保调用 get_conn 时已正确配置 search_path

唯一键与冲突处理说明：
- repos: UNIQUE(repo_type, url) -> DO UPDATE
- svn_revisions: UNIQUE(repo_id, COALESCE(rev_num, rev_id)) -> DO UPDATE
- git_commits: UNIQUE(repo_id, COALESCE(commit_sha, commit_id)) -> DO UPDATE
- mrs: PRIMARY KEY(mr_id) -> DO UPDATE
- review_events: UNIQUE(mr_id, source_event_id) -> DO NOTHING (幂等插入)
- patch_blobs: UNIQUE(source_type, source_id, sha256) -> DO NOTHING (幂等插入)
  - uri: 可空，支持待物化场景（先插入记录，后续补充 uri）
  - meta_json: 存储额外元数据，包含物化状态
  - updated_at: 更新时间（触发器自动更新）
  - 使用 update_patch_blob_meta() 显式更新 meta_json/uri

patch_blobs.meta_json 字段规范:
{
    "materialize_status": "pending" | "done" | "failed",  # 物化状态
    "materialize_error": "...",                           # 物化失败时的错误信息
    "materialized_at": "2024-01-01T00:00:00Z",           # 物化完成时间
    "source_uri": "svn://...",                            # 原始来源 URI（可选）
    ...其他自定义元数据
}
"""

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, Union

import psycopg
from psycopg.rows import dict_row

from engram_step1.source_id import (
    build_git_source_id,
    build_mr_source_id,
    build_svn_source_id,
)


def get_conn(
    dsn: Optional[str] = None,
    autocommit: bool = False,
) -> psycopg.Connection:
    """
    获取数据库连接

    Args:
        dsn: 数据库连接字符串，为 None 时从环境变量 POSTGRES_DSN 读取
        autocommit: 是否启用自动提交模式

    Returns:
        psycopg.Connection 对象

    Raises:
        ConnectionError: 连接失败时抛出
    """
    if dsn is None:
        dsn = os.environ.get("POSTGRES_DSN")
        if not dsn:
            raise ValueError("POSTGRES_DSN 环境变量未设置，且未提供 dsn 参数")

    try:
        return psycopg.connect(dsn, autocommit=autocommit)
    except Exception as e:
        raise ConnectionError(f"数据库连接失败: {e}")


# ============ SCM.repos Upsert ============


def upsert_repo(
    conn: psycopg.Connection,
    repo_type: str,
    url: str,
    project_key: str,
    default_branch: Optional[str] = None,
) -> int:
    """
    插入或更新 scm.repos 记录

    唯一键: (repo_type, url)
    冲突处理: DO UPDATE - 更新 project_key 和 default_branch

    Args:
        conn: 数据库连接
        repo_type: 仓库类型 ('svn' | 'git')
        url: 仓库 URL
        project_key: 项目标识
        default_branch: 默认分支

    Returns:
        repo_id
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO repos (repo_type, url, project_key, default_branch)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (repo_type, url) DO UPDATE
            SET project_key = EXCLUDED.project_key,
                default_branch = EXCLUDED.default_branch
            RETURNING repo_id
            """,
            (repo_type, url, project_key, default_branch),
        )
        result = cur.fetchone()
        return result[0]


def get_or_create_repo(
    conn: psycopg.Connection,
    repo_type: str,
    url: str,
    project_key: str,
    default_branch: Optional[str] = None,
) -> int:
    """
    获取或创建 repo，返回 repo_id

    Args:
        conn: 数据库连接
        repo_type: 仓库类型
        url: 仓库 URL
        project_key: 项目标识
        default_branch: 默认分支

    Returns:
        repo_id
    """
    return upsert_repo(conn, repo_type, url, project_key, default_branch)


# ============ SCM.svn_revisions Upsert ============


def upsert_svn_revision(
    conn: psycopg.Connection,
    repo_id: int,
    rev_num: int,
    author_raw: str,
    ts: Optional[datetime] = None,
    message: Optional[str] = None,
    is_bulk: bool = False,
    bulk_reason: Optional[str] = None,
    meta_json: Optional[Dict] = None,
    source_id: Optional[str] = None,
) -> int:
    """
    插入或更新 scm.svn_revisions 记录

    唯一键: (repo_id, COALESCE(rev_num, rev_id)) - 通过唯一索引 idx_svn_revisions_repo_revnum
    冲突处理: DO UPDATE - 更新 author_raw, ts, message, is_bulk, bulk_reason, meta_json, source_id

    注意: 由于使用表达式唯一索引，需要通过先查询再决定 INSERT/UPDATE 的方式处理

    Args:
        conn: 数据库连接
        repo_id: 仓库 ID
        rev_num: SVN revision number
        author_raw: 原始作者信息
        ts: 提交时间
        message: 提交消息
        is_bulk: 是否为批量提交
        bulk_reason: 批量提交原因
        meta_json: 元数据
        source_id: 统一的 source_id，格式为 svn:<repo_id>:<rev_num>，若为 None 则自动生成

    Returns:
        svn_rev_id
    """
    # 自动生成 source_id（如果未提供）
    if source_id is None:
        source_id = build_svn_source_id(repo_id, rev_num)
    
    meta = json.dumps(meta_json or {})

    with conn.cursor() as cur:
        # 先查询是否存在（基于 repo_id + rev_num 唯一索引）
        cur.execute(
            """
            SELECT svn_rev_id FROM svn_revisions
            WHERE repo_id = %s AND COALESCE(rev_num, rev_id) = %s
            """,
            (repo_id, rev_num),
        )
        existing = cur.fetchone()

        if existing:
            # 更新现有记录
            cur.execute(
                """
                UPDATE svn_revisions
                SET author_raw = %s,
                    ts = %s,
                    message = %s,
                    is_bulk = %s,
                    bulk_reason = %s,
                    meta_json = %s,
                    source_id = %s
                WHERE svn_rev_id = %s
                RETURNING svn_rev_id
                """,
                (author_raw, ts, message, is_bulk, bulk_reason, meta, source_id, existing[0]),
            )
        else:
            # 插入新记录
            cur.execute(
                """
                INSERT INTO svn_revisions
                    (repo_id, rev_num, author_raw, ts, message, is_bulk, bulk_reason, meta_json, source_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING svn_rev_id
                """,
                (repo_id, rev_num, author_raw, ts, message, is_bulk, bulk_reason, meta, source_id),
            )

        result = cur.fetchone()
        return result[0]


def upsert_svn_revisions_batch(
    conn: psycopg.Connection,
    revisions: List[Dict[str, Any]],
) -> List[int]:
    """
    批量插入或更新 scm.svn_revisions 记录

    Args:
        conn: 数据库连接
        revisions: 修订记录列表，每条记录需包含:
            - repo_id: int
            - rev_num: int
            - author_raw: str
            - ts: Optional[datetime]
            - message: Optional[str]
            - is_bulk: bool (default: False)
            - bulk_reason: Optional[str]
            - meta_json: Optional[Dict]
            - source_id: Optional[str] - 统一的 source_id

    Returns:
        svn_rev_id 列表
    """
    ids = []
    for rev in revisions:
        svn_rev_id = upsert_svn_revision(
            conn,
            repo_id=rev["repo_id"],
            rev_num=rev["rev_num"],
            author_raw=rev["author_raw"],
            ts=rev.get("ts"),
            message=rev.get("message"),
            is_bulk=rev.get("is_bulk", False),
            bulk_reason=rev.get("bulk_reason"),
            meta_json=rev.get("meta_json"),
            source_id=rev.get("source_id"),
        )
        ids.append(svn_rev_id)
    return ids


# ============ SCM.git_commits Upsert ============


def upsert_git_commit(
    conn: psycopg.Connection,
    repo_id: int,
    commit_sha: str,
    author_raw: str,
    ts: Optional[datetime] = None,
    message: Optional[str] = None,
    is_merge: bool = False,
    is_bulk: bool = False,
    bulk_reason: Optional[str] = None,
    meta_json: Optional[Dict] = None,
    source_id: Optional[str] = None,
) -> int:
    """
    插入或更新 scm.git_commits 记录

    唯一键: (repo_id, COALESCE(commit_sha, commit_id)) - 通过唯一索引
    冲突处理: DO UPDATE - 更新 author_raw, ts, message, is_merge, is_bulk, bulk_reason, meta_json, source_id

    Args:
        conn: 数据库连接
        repo_id: 仓库 ID
        commit_sha: Git commit SHA
        author_raw: 原始作者信息
        ts: 提交时间
        message: 提交消息
        is_merge: 是否为合并提交
        is_bulk: 是否为批量提交
        bulk_reason: 批量提交原因
        meta_json: 元数据
        source_id: 统一的 source_id，格式为 git:<repo_id>:<commit_sha>，若为 None 则自动生成

    Returns:
        git_commit_id
    """
    # 自动生成 source_id（如果未提供）
    if source_id is None:
        source_id = build_git_source_id(repo_id, commit_sha)
    
    meta = json.dumps(meta_json or {})

    with conn.cursor() as cur:
        # 先查询是否存在
        cur.execute(
            """
            SELECT git_commit_id FROM git_commits
            WHERE repo_id = %s AND COALESCE(commit_sha, commit_id) = %s
            """,
            (repo_id, commit_sha),
        )
        existing = cur.fetchone()

        if existing:
            # 更新现有记录
            cur.execute(
                """
                UPDATE git_commits
                SET author_raw = %s,
                    ts = %s,
                    message = %s,
                    is_merge = %s,
                    is_bulk = %s,
                    bulk_reason = %s,
                    meta_json = %s,
                    source_id = %s
                WHERE git_commit_id = %s
                RETURNING git_commit_id
                """,
                (author_raw, ts, message, is_merge, is_bulk, bulk_reason, meta, source_id, existing[0]),
            )
        else:
            # 插入新记录
            cur.execute(
                """
                INSERT INTO git_commits
                    (repo_id, commit_sha, author_raw, ts, message, is_merge, is_bulk, bulk_reason, meta_json, source_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING git_commit_id
                """,
                (repo_id, commit_sha, author_raw, ts, message, is_merge, is_bulk, bulk_reason, meta, source_id),
            )

        result = cur.fetchone()
        return result[0]


def upsert_git_commits_batch(
    conn: psycopg.Connection,
    commits: List[Dict[str, Any]],
) -> List[int]:
    """
    批量插入或更新 scm.git_commits 记录

    Args:
        conn: 数据库连接
        commits: 提交记录列表，每条记录需包含:
            - repo_id: int
            - commit_sha: str
            - author_raw: str
            - ts: Optional[datetime]
            - message: Optional[str]
            - is_merge: bool (default: False)
            - is_bulk: bool (default: False)
            - bulk_reason: Optional[str]
            - meta_json: Optional[Dict]
            - source_id: Optional[str]

    Returns:
        git_commit_id 列表
    """
    ids = []
    for commit in commits:
        git_commit_id = upsert_git_commit(
            conn,
            repo_id=commit["repo_id"],
            commit_sha=commit["commit_sha"],
            author_raw=commit["author_raw"],
            ts=commit.get("ts"),
            message=commit.get("message"),
            is_merge=commit.get("is_merge", False),
            is_bulk=commit.get("is_bulk", False),
            bulk_reason=commit.get("bulk_reason"),
            meta_json=commit.get("meta_json"),
            source_id=commit.get("source_id"),
        )
        ids.append(git_commit_id)
    return ids


# ============ SCM.mrs Upsert ============


def upsert_mr(
    conn: psycopg.Connection,
    mr_id: str,
    repo_id: int,
    status: str,
    author_user_id: Optional[str] = None,
    url: Optional[str] = None,
    meta_json: Optional[Dict] = None,
    mr_iid: Optional[int] = None,
    source_id: Optional[str] = None,
) -> str:
    """
    插入或更新 scm.mrs 记录

    唯一键: mr_id (PRIMARY KEY)
    冲突处理: DO UPDATE - 更新 repo_id, author_user_id, status, url, meta_json, source_id, updated_at

    Args:
        conn: 数据库连接
        mr_id: MR 唯一标识 (如 "gitlab:project:123")
        repo_id: 仓库 ID
        status: MR 状态 (opened/merged/closed)
        author_user_id: 作者用户 ID
        url: MR URL
        meta_json: 元数据
        mr_iid: MR 在仓库内的 IID（用于生成 source_id），若未提供则尝试从 mr_id 解析
        source_id: 统一的 source_id，格式为 mr:<repo_id>:<iid>，若为 None 则自动生成

    Returns:
        mr_id
    """
    # 自动生成 source_id（如果未提供）
    if source_id is None and mr_iid is not None:
        source_id = build_mr_source_id(repo_id, mr_iid)
    elif source_id is None:
        # 尝试从 mr_id 解析 iid（假设格式为 "platform:project:iid"）
        try:
            parts = mr_id.split(":")
            if len(parts) >= 3:
                iid = int(parts[-1])
                source_id = build_mr_source_id(repo_id, iid)
        except (ValueError, IndexError):
            pass  # 无法解析，source_id 保持 None
    
    meta = json.dumps(meta_json or {})

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO mrs (mr_id, repo_id, author_user_id, status, url, meta_json, source_id, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (mr_id) DO UPDATE
            SET repo_id = EXCLUDED.repo_id,
                author_user_id = EXCLUDED.author_user_id,
                status = EXCLUDED.status,
                url = EXCLUDED.url,
                meta_json = EXCLUDED.meta_json,
                source_id = EXCLUDED.source_id,
                updated_at = now()
            RETURNING mr_id
            """,
            (mr_id, repo_id, author_user_id, status, url, meta, source_id),
        )
        result = cur.fetchone()
        return result[0]


def upsert_mrs_batch(
    conn: psycopg.Connection,
    mrs: List[Dict[str, Any]],
) -> List[str]:
    """
    批量插入或更新 scm.mrs 记录

    Args:
        conn: 数据库连接
        mrs: MR 记录列表，每条记录需包含:
            - mr_id: str
            - repo_id: int
            - status: str
            - author_user_id: Optional[str]
            - url: Optional[str]
            - meta_json: Optional[Dict]
            - mr_iid: Optional[int] - MR 在仓库内的 IID
            - source_id: Optional[str] - 统一的 source_id

    Returns:
        mr_id 列表
    """
    ids = []
    for mr in mrs:
        mr_id = upsert_mr(
            conn,
            mr_id=mr["mr_id"],
            repo_id=mr["repo_id"],
            status=mr["status"],
            author_user_id=mr.get("author_user_id"),
            url=mr.get("url"),
            meta_json=mr.get("meta_json"),
            mr_iid=mr.get("mr_iid"),
            source_id=mr.get("source_id"),
        )
        ids.append(mr_id)
    return ids


# ============ SCM.review_events Insert ============


def insert_review_event(
    conn: psycopg.Connection,
    mr_id: str,
    event_type: str,
    source_event_id: Optional[str] = None,
    reviewer_user_id: Optional[str] = None,
    payload_json: Optional[Dict] = None,
    ts: Optional[datetime] = None,
) -> Optional[int]:
    """
    插入 scm.review_events 记录

    当提供 source_event_id 时，使用 ON CONFLICT (mr_id, source_event_id) DO NOTHING 保证幂等
    冲突处理: 如果 (mr_id, source_event_id) 已存在则跳过

    Args:
        conn: 数据库连接
        mr_id: MR 唯一标识
        event_type: 事件类型 (comment/approve/request_changes/assign/etc)
        source_event_id: 源系统事件ID，用于幂等去重
        reviewer_user_id: 评审者用户 ID
        payload_json: 事件负载
        ts: 事件时间 (默认 now())

    Returns:
        review_event id，如果因冲突未插入则返回 None
    """
    payload = json.dumps(payload_json or {})

    with conn.cursor() as cur:
        if ts is None:
            cur.execute(
                """
                INSERT INTO review_events (mr_id, source_event_id, reviewer_user_id, event_type, payload_json)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (mr_id, source_event_id) DO NOTHING
                RETURNING id
                """,
                (mr_id, source_event_id, reviewer_user_id, event_type, payload),
            )
        else:
            cur.execute(
                """
                INSERT INTO review_events (mr_id, source_event_id, reviewer_user_id, event_type, payload_json, ts)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (mr_id, source_event_id) DO NOTHING
                RETURNING id
                """,
                (mr_id, source_event_id, reviewer_user_id, event_type, payload, ts),
            )

        result = cur.fetchone()
        return result[0] if result else None


def upsert_review_event(
    conn: psycopg.Connection,
    mr_id: str,
    event_type: str,
    source_event_id: Optional[str] = None,
    reviewer_user_id: Optional[str] = None,
    payload_json: Optional[Dict] = None,
    ts: Optional[datetime] = None,
) -> Optional[int]:
    """
    插入 review_event，相同 source_event_id 不重复插入

    使用 ON CONFLICT (mr_id, source_event_id) DO NOTHING 保证幂等

    Args:
        conn: 数据库连接
        mr_id: MR 唯一标识
        event_type: 事件类型
        source_event_id: 源系统事件ID，用于幂等去重
        reviewer_user_id: 评审者用户 ID
        payload_json: 事件负载
        ts: 事件时间

    Returns:
        review_event id，如果因冲突未插入则返回 None
    """
    return insert_review_event(conn, mr_id, event_type, source_event_id, reviewer_user_id, payload_json, ts)


def insert_review_events_batch(
    conn: psycopg.Connection,
    events: List[Dict[str, Any]],
) -> List[Optional[int]]:
    """
    批量插入 scm.review_events 记录

    Args:
        conn: 数据库连接
        events: 事件记录列表，每条记录需包含:
            - mr_id: str
            - event_type: str
            - source_event_id: Optional[str] - 源系统事件ID
            - reviewer_user_id: Optional[str]
            - payload_json: Optional[Dict]
            - ts: Optional[datetime]

    Returns:
        review_event id 列表，因冲突未插入的为 None
    """
    ids = []
    for event in events:
        event_id = insert_review_event(
            conn,
            mr_id=event["mr_id"],
            event_type=event["event_type"],
            source_event_id=event.get("source_event_id"),
            reviewer_user_id=event.get("reviewer_user_id"),
            payload_json=event.get("payload_json"),
            ts=event.get("ts"),
        )
        ids.append(event_id)
    return ids


# ============ SCM.patch_blobs Upsert ============


def upsert_patch_blob(
    conn: psycopg.Connection,
    source_type: str,
    source_id: str,
    sha256: str,
    uri: Optional[str] = None,
    size_bytes: Optional[int] = None,
    format: str = "diff",
    chunking_version: Optional[str] = None,
    meta_json: Optional[Dict] = None,
) -> Optional[int]:
    """
    插入 scm.patch_blobs 记录（如果不存在）

    唯一键: (source_type, source_id, sha256)
    冲突处理: DO NOTHING - 已存在则跳过，返回现有记录的 blob_id
    
    注意：此函数保持幂等行为，已存在的记录不会被更新。
    如需更新 meta_json/uri，请使用 update_patch_blob_meta() 函数。
    
    支持待物化场景：
    - uri 可为 None，表示待物化状态
    - 使用 meta_json.materialize_status 跟踪物化进度
    - 后续通过 update_patch_blob_meta() 补充 uri

    Args:
        conn: 数据库连接
        source_type: 来源类型 ('svn' | 'git')
        source_id: 来源 ID (svn:rev_num 或 git:commit_sha)
        sha256: SHA256 哈希值
        uri: blob 存储 URI（可选，None 表示待物化）
        size_bytes: 文件大小（字节）
        format: 格式 (default: 'diff')
        chunking_version: 分块版本
        meta_json: 元数据字典，推荐包含 materialize_status 字段

    Returns:
        blob_id，如果记录已存在返回现有的 blob_id
    """
    meta = json.dumps(meta_json or {})
    
    with conn.cursor() as cur:
        # 尝试插入，冲突时不做任何操作
        cur.execute(
            """
            INSERT INTO patch_blobs
                (source_type, source_id, uri, sha256, size_bytes, format, chunking_version, meta_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_type, source_id, sha256) DO NOTHING
            RETURNING blob_id
            """,
            (source_type, source_id, uri, sha256, size_bytes, format, chunking_version, meta),
        )
        result = cur.fetchone()

        if result:
            return result[0]

        # 如果没有返回值，说明记录已存在，查询现有记录
        cur.execute(
            """
            SELECT blob_id FROM patch_blobs
            WHERE source_type = %s AND source_id = %s AND sha256 = %s
            """,
            (source_type, source_id, sha256),
        )
        existing = cur.fetchone()
        return existing[0] if existing else None


def update_patch_blob_meta(
    conn: psycopg.Connection,
    source_type: str,
    source_id: str,
    sha256: str,
    meta_json: Optional[Dict] = None,
    uri: Optional[str] = None,
) -> Optional[int]:
    """
    更新已存在的 scm.patch_blobs 记录的 meta_json 和/或 uri
    
    用于显式补全 meta_json/uri 字段，仅更新提供的非 None 字段。
    如果记录不存在则不执行任何操作。

    Args:
        conn: 数据库连接
        source_type: 来源类型 ('svn' | 'git')
        source_id: 来源 ID (svn:rev_num 或 git:commit_sha)
        sha256: SHA256 哈希值
        meta_json: 元数据字典（可选，None 则不更新）
        uri: blob 存储 URI（可选，None 则不更新）

    Returns:
        blob_id，如果记录不存在返回 None
    """
    # 构建动态 SET 子句
    set_clauses = []
    params = []
    
    if meta_json is not None:
        set_clauses.append("meta_json = %s")
        params.append(json.dumps(meta_json))
    
    if uri is not None:
        set_clauses.append("uri = %s")
        params.append(uri)
    
    if not set_clauses:
        # 没有需要更新的字段，直接查询返回 blob_id
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT blob_id FROM patch_blobs
                WHERE source_type = %s AND source_id = %s AND sha256 = %s
                """,
                (source_type, source_id, sha256),
            )
            existing = cur.fetchone()
            return existing[0] if existing else None
    
    # 添加 WHERE 条件参数
    params.extend([source_type, source_id, sha256])
    
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE patch_blobs
            SET {', '.join(set_clauses)}
            WHERE source_type = %s AND source_id = %s AND sha256 = %s
            RETURNING blob_id
            """,
            params,
        )
        result = cur.fetchone()
        return result[0] if result else None


def get_patch_blob(
    conn: psycopg.Connection,
    source_type: str,
    source_id: str,
    sha256: str,
) -> Optional[Dict[str, Any]]:
    """
    获取 scm.patch_blobs 记录

    Args:
        conn: 数据库连接
        source_type: 来源类型 ('svn' | 'git')
        source_id: 来源 ID (svn:rev_num 或 git:commit_sha)
        sha256: SHA256 哈希值

    Returns:
        patch_blob 信息字典，不存在返回 None
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT blob_id, source_type, source_id, uri, sha256, size_bytes, 
                   format, chunking_version, meta_json, created_at, updated_at
            FROM patch_blobs
            WHERE source_type = %s AND source_id = %s AND sha256 = %s
            """,
            (source_type, source_id, sha256),
        )
        return cur.fetchone()


def upsert_patch_blobs_batch(
    conn: psycopg.Connection,
    blobs: List[Dict[str, Any]],
) -> List[Optional[int]]:
    """
    批量插入 scm.patch_blobs 记录

    Args:
        conn: 数据库连接
        blobs: blob 记录列表，每条记录需包含:
            - source_type: str
            - source_id: str
            - sha256: str
            - uri: Optional[str]（可选，None 表示待物化）
            - size_bytes: Optional[int]
            - format: str (default: 'diff')
            - chunking_version: Optional[str]
            - meta_json: Optional[Dict]（推荐包含 materialize_status）

    Returns:
        blob_id 列表
    """
    ids = []
    for blob in blobs:
        blob_id = upsert_patch_blob(
            conn,
            source_type=blob["source_type"],
            source_id=blob["source_id"],
            sha256=blob["sha256"],
            uri=blob.get("uri"),
            size_bytes=blob.get("size_bytes"),
            format=blob.get("format", "diff"),
            chunking_version=blob.get("chunking_version"),
            meta_json=blob.get("meta_json"),
        )
        ids.append(blob_id)
    return ids


# ============ Patch Blob 物化相关函数 ============


# 物化状态常量
MATERIALIZE_STATUS_PENDING = "pending"
MATERIALIZE_STATUS_DONE = "done"
MATERIALIZE_STATUS_FAILED = "failed"
MATERIALIZE_STATUS_IN_PROGRESS = "in_progress"


def select_pending_blobs_for_materialize(
    conn: psycopg.Connection,
    source_type: Optional[str] = None,
    batch_size: int = 50,
    retry_failed: bool = False,
    max_attempts: int = 3,
) -> List[Dict[str, Any]]:
    """
    选择待物化的 patch_blobs 记录（并发安全）

    使用 FOR UPDATE SKIP LOCKED 避免并发重复处理。

    选择条件:
    1. uri 为空或为 NULL
    2. 或 meta_json.materialize_status IN ('pending', 'failed'[如果 retry_failed])
    3. 且 meta_json.attempts < max_attempts（如果重试）

    Args:
        conn: 数据库连接
        source_type: 可选，筛选特定源类型 ('svn' | 'git')
        batch_size: 最大返回数量
        retry_failed: 是否包含之前失败的记录
        max_attempts: 最大重试次数

    Returns:
        patch_blob 记录列表，每条包含:
        - blob_id, source_type, source_id, uri, sha256, size_bytes, format, meta_json
    """
    # 构建状态条件
    status_conditions = [f"'{MATERIALIZE_STATUS_PENDING}'"]
    if retry_failed:
        status_conditions.append(f"'{MATERIALIZE_STATUS_FAILED}'")
    status_in_clause = ", ".join(status_conditions)

    # 构建 source_type 条件
    source_type_clause = ""
    params: List[Any] = []
    if source_type:
        source_type_clause = "AND source_type = %s"
        params.append(source_type)

    # 重试时检查 attempts 次数
    attempts_clause = ""
    if retry_failed:
        attempts_clause = f"AND COALESCE((meta_json->>'attempts')::int, 0) < {max_attempts}"

    query = f"""
        SELECT blob_id, source_type, source_id, uri, sha256, size_bytes, format,
               meta_json
        FROM patch_blobs
        WHERE (
            -- 条件1: URI 为空或 NULL
            (uri IS NULL OR uri = '')
            OR
            -- 条件2: 物化状态为 pending 或 failed
            (meta_json->>'materialize_status' IN ({status_in_clause}))
        )
        {source_type_clause}
        {attempts_clause}
        ORDER BY blob_id
        LIMIT %s
        FOR UPDATE SKIP LOCKED
    """
    params.append(batch_size)

    results = []
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        for row in cur.fetchall():
            # 解析 meta_json
            meta = row.get("meta_json")
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except json.JSONDecodeError:
                    meta = {}
            row["meta_json"] = meta or {}
            results.append(dict(row))

    return results


def update_patch_blob_materialize_status(
    conn: psycopg.Connection,
    blob_id: int,
    status: str,
    uri: Optional[str] = None,
    sha256: Optional[str] = None,
    size_bytes: Optional[int] = None,
    error: Optional[str] = None,
    expected_sha256: Optional[str] = None,
    evidence_uri: Optional[str] = None,
    endpoint: Optional[str] = None,
    status_code: Optional[int] = None,
    error_category: Optional[str] = None,
    extra_meta: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    更新 patch_blob 的物化状态（并发安全）

    在 meta_json 中写入:
    - materialize_status: 状态
    - materialized_at: 完成时间（仅成功时）
    - attempts: 尝试次数（+1）
    - last_error: 最后一次错误信息
    - evidence_uri: canonical evidence URI (memory://patch_blobs/...)
    - last_endpoint: 最后请求的 endpoint（可选）
    - last_status_code: 最后响应的 HTTP 状态码（可选）
    - error_category: 错误分类（timeout/http_error/auth_error/content_too_large，可选）
    - last_attempt_at: 最后一次尝试时间
    - mirror_uri: SHA 不匹配时写入的 mirror 制品 URI（可选）
    - actual_sha256: 实际计算的 SHA256（可选）
    - mirrored_at: mirror 写入时间（可选）

    如果提供了 expected_sha256，则使用乐观锁检查 sha256 匹配。

    Args:
        conn: 数据库连接
        blob_id: blob ID
        status: 物化状态 ('pending' | 'done' | 'failed' | 'in_progress')
        uri: 新的 URI（可选）
        sha256: 新的 sha256（可选）
        size_bytes: 新的大小（可选）
        error: 错误信息（可选）
        expected_sha256: 预期的 sha256（用于乐观锁校验，可选）
        evidence_uri: canonical evidence URI (memory://patch_blobs/<source_type>/<source_id>/<sha256>)
        endpoint: 请求的 API endpoint（可选）
        status_code: HTTP 响应状态码（可选）
        error_category: 错误分类 (timeout/http_error/auth_error/content_too_large)
        extra_meta: 额外的 meta_json 字段（可选，如 mirror_uri, actual_sha256）

    Returns:
        True 如果更新成功，False 如果因 sha256 不匹配或记录不存在而失败
    """
    # 构建 meta_json 更新
    meta_updates = {
        "materialize_status": status,
        "last_attempt_at": datetime.utcnow().isoformat() + "Z",
    }

    if status == MATERIALIZE_STATUS_DONE:
        meta_updates["materialized_at"] = datetime.utcnow().isoformat() + "Z"

    if error:
        meta_updates["last_error"] = error
    
    if evidence_uri:
        meta_updates["evidence_uri"] = evidence_uri
    
    if endpoint:
        meta_updates["last_endpoint"] = endpoint
    
    if status_code is not None:
        meta_updates["last_status_code"] = status_code
    
    if error_category:
        meta_updates["error_category"] = error_category
    
    # 合并额外的 meta 字段（如 mirror_uri, actual_sha256）
    if extra_meta:
        meta_updates.update(extra_meta)

    # 构建 SET 子句
    set_clauses = [
        # 更新 meta_json，合并现有值并增加 attempts
        """meta_json = COALESCE(meta_json, '{}'::jsonb) 
           || %s::jsonb 
           || jsonb_build_object('attempts', COALESCE((meta_json->>'attempts')::int, 0) + 1)"""
    ]
    params: List[Any] = [json.dumps(meta_updates)]

    if uri is not None:
        set_clauses.append("uri = %s")
        params.append(uri)

    if sha256 is not None:
        set_clauses.append("sha256 = %s")
        params.append(sha256)

    if size_bytes is not None:
        set_clauses.append("size_bytes = %s")
        params.append(size_bytes)

    # 构建 WHERE 子句
    where_clauses = ["blob_id = %s"]
    params.append(blob_id)

    if expected_sha256 is not None:
        where_clauses.append("sha256 = %s")
        params.append(expected_sha256)

    query = f"""
        UPDATE patch_blobs
        SET {', '.join(set_clauses)}
        WHERE {' AND '.join(where_clauses)}
        RETURNING blob_id
    """

    with conn.cursor() as cur:
        cur.execute(query, params)
        result = cur.fetchone()
        return result is not None


def mark_blob_in_progress(
    conn: psycopg.Connection,
    blob_id: int,
    endpoint: Optional[str] = None,
) -> bool:
    """
    标记 blob 为处理中状态

    Args:
        conn: 数据库连接
        blob_id: blob ID
        endpoint: 即将请求的 API endpoint（可选）

    Returns:
        True 如果更新成功
    """
    return update_patch_blob_materialize_status(
        conn, blob_id, MATERIALIZE_STATUS_IN_PROGRESS,
        endpoint=endpoint,
    )


def mark_blob_done(
    conn: psycopg.Connection,
    blob_id: int,
    uri: str,
    sha256: str,
    size_bytes: int,
    expected_sha256: Optional[str] = None,
    evidence_uri: Optional[str] = None,
) -> bool:
    """
    标记 blob 物化完成

    Args:
        conn: 数据库连接
        blob_id: blob ID
        uri: 物化后的 URI
        sha256: 内容 sha256
        size_bytes: 内容大小
        expected_sha256: 预期的 sha256（用于乐观锁校验）
        evidence_uri: canonical evidence URI (memory://patch_blobs/<source_type>/<source_id>/<sha256>)

    Returns:
        True 如果更新成功
    """
    return update_patch_blob_materialize_status(
        conn,
        blob_id,
        MATERIALIZE_STATUS_DONE,
        uri=uri,
        sha256=sha256,
        size_bytes=size_bytes,
        expected_sha256=expected_sha256,
        evidence_uri=evidence_uri,
    )


def mark_blob_failed(
    conn: psycopg.Connection,
    blob_id: int,
    error: str,
    endpoint: Optional[str] = None,
    status_code: Optional[int] = None,
    error_category: Optional[str] = None,
    mirror_uri: Optional[str] = None,
    actual_sha256: Optional[str] = None,
) -> bool:
    """
    标记 blob 物化失败

    Args:
        conn: 数据库连接
        blob_id: blob ID
        error: 错误信息
        endpoint: 请求的 API endpoint（可选）
        status_code: HTTP 响应状态码（可选）
        error_category: 错误分类 (timeout/http_error/auth_error/content_too_large/validation_error)
        mirror_uri: SHA 不匹配时，实际写入的 mirror 制品 URI（可选，mirror 模式使用）
        actual_sha256: 实际计算得到的 SHA256（可选，用于 SHA 不匹配场景追踪）

    Returns:
        True 如果更新成功
    """
    # 构建额外的 meta 更新（mirror 信息）
    extra_meta = {}
    if mirror_uri:
        extra_meta["mirror_uri"] = mirror_uri
        extra_meta["mirrored_at"] = datetime.utcnow().isoformat() + "Z"
    if actual_sha256:
        extra_meta["actual_sha256"] = actual_sha256
    
    return update_patch_blob_materialize_status(
        conn, blob_id, MATERIALIZE_STATUS_FAILED, 
        error=error,
        endpoint=endpoint,
        status_code=status_code,
        error_category=error_category,
        extra_meta=extra_meta if extra_meta else None,
    )


# ============ 查询辅助函数 ============


def get_repo_by_url(
    conn: psycopg.Connection,
    repo_type: str,
    url: str,
) -> Optional[Dict[str, Any]]:
    """
    根据 repo_type 和 url 获取 repo 信息

    Args:
        conn: 数据库连接
        repo_type: 仓库类型
        url: 仓库 URL

    Returns:
        repo 信息字典，不存在返回 None
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT repo_id, repo_type, url, project_key, default_branch, created_at
            FROM repos
            WHERE repo_type = %s AND url = %s
            """,
            (repo_type, url),
        )
        return cur.fetchone()


def get_latest_svn_revision(
    conn: psycopg.Connection,
    repo_id: int,
) -> Optional[int]:
    """
    获取仓库的最新 SVN revision number

    Args:
        conn: 数据库连接
        repo_id: 仓库 ID

    Returns:
        最新的 rev_num，无记录返回 None
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(rev_num, rev_id) as rev
            FROM svn_revisions
            WHERE repo_id = %s
            ORDER BY COALESCE(rev_num, rev_id) DESC
            LIMIT 1
            """,
            (repo_id,),
        )
        result = cur.fetchone()
        return result[0] if result else None


def get_latest_git_commit_ts(
    conn: psycopg.Connection,
    repo_id: int,
) -> Optional[datetime]:
    """
    获取仓库的最新 Git commit 时间

    Args:
        conn: 数据库连接
        repo_id: 仓库 ID

    Returns:
        最新的提交时间，无记录返回 None
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts
            FROM git_commits
            WHERE repo_id = %s
            ORDER BY ts DESC NULLS LAST
            LIMIT 1
            """,
            (repo_id,),
        )
        result = cur.fetchone()
        return result[0] if result else None


# ============ Source ID Backfill 函数 ============


def backfill_svn_source_ids(
    conn: psycopg.Connection,
    repo_id: Optional[int] = None,
    batch_size: int = 1000,
) -> int:
    """
    回填 scm.svn_revisions 表中缺失的 source_id

    Args:
        conn: 数据库连接
        repo_id: 指定仓库 ID，为 None 时处理所有仓库
        batch_size: 每批处理的记录数

    Returns:
        更新的记录数
    """
    total_updated = 0
    
    with conn.cursor() as cur:
        while True:
            # 查询缺失 source_id 的记录
            if repo_id is not None:
                cur.execute(
                    """
                    SELECT svn_rev_id, repo_id, COALESCE(rev_num, rev_id) as rev_num
                    FROM svn_revisions
                    WHERE source_id IS NULL AND repo_id = %s
                    LIMIT %s
                    """,
                    (repo_id, batch_size),
                )
            else:
                cur.execute(
                    """
                    SELECT svn_rev_id, repo_id, COALESCE(rev_num, rev_id) as rev_num
                    FROM svn_revisions
                    WHERE source_id IS NULL
                    LIMIT %s
                    """,
                    (batch_size,),
                )
            
            rows = cur.fetchall()
            if not rows:
                break
            
            # 批量更新
            for svn_rev_id, rid, rev_num in rows:
                new_source_id = build_svn_source_id(rid, rev_num)
                cur.execute(
                    """
                    UPDATE svn_revisions
                    SET source_id = %s
                    WHERE svn_rev_id = %s
                    """,
                    (new_source_id, svn_rev_id),
                )
            
            total_updated += len(rows)
            conn.commit()
    
    return total_updated


def backfill_git_source_ids(
    conn: psycopg.Connection,
    repo_id: Optional[int] = None,
    batch_size: int = 1000,
) -> int:
    """
    回填 scm.git_commits 表中缺失的 source_id

    Args:
        conn: 数据库连接
        repo_id: 指定仓库 ID，为 None 时处理所有仓库
        batch_size: 每批处理的记录数

    Returns:
        更新的记录数
    """
    total_updated = 0
    
    with conn.cursor() as cur:
        while True:
            # 查询缺失 source_id 的记录
            if repo_id is not None:
                cur.execute(
                    """
                    SELECT git_commit_id, repo_id, COALESCE(commit_sha, commit_id::text) as commit_sha
                    FROM git_commits
                    WHERE source_id IS NULL AND repo_id = %s
                    LIMIT %s
                    """,
                    (repo_id, batch_size),
                )
            else:
                cur.execute(
                    """
                    SELECT git_commit_id, repo_id, COALESCE(commit_sha, commit_id::text) as commit_sha
                    FROM git_commits
                    WHERE source_id IS NULL
                    LIMIT %s
                    """,
                    (batch_size,),
                )
            
            rows = cur.fetchall()
            if not rows:
                break
            
            # 批量更新
            for git_commit_id, rid, commit_sha in rows:
                new_source_id = build_git_source_id(rid, commit_sha)
                cur.execute(
                    """
                    UPDATE git_commits
                    SET source_id = %s
                    WHERE git_commit_id = %s
                    """,
                    (new_source_id, git_commit_id),
                )
            
            total_updated += len(rows)
            conn.commit()
    
    return total_updated


# ============ SCM.sync_runs 函数 ============


def insert_sync_run_start(
    conn: psycopg.Connection,
    run_id: str,
    repo_id: int,
    job_type: str,
    mode: str = "incremental",
    cursor_before: Optional[Dict] = None,
    meta_json: Optional[Dict] = None,
) -> str:
    """
    记录同步运行开始
    
    在同步脚本主流程开始时调用，创建一条 status='running' 的记录。
    
    Args:
        conn: 数据库连接
        run_id: 运行唯一标识（UUID 字符串）
        repo_id: 仓库 ID
        job_type: 任务类型 ('gitlab_commits' | 'gitlab_mrs' | 'gitlab_reviews' | 'svn')
        mode: 同步模式 ('incremental' | 'backfill' | 'full')
        cursor_before: 同步前的游标快照
        meta_json: 额外元数据
    
    Returns:
        run_id
    """
    cursor_json = json.dumps(cursor_before) if cursor_before else None
    meta = json.dumps(meta_json or {})
    
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sync_runs 
                (run_id, repo_id, job_type, mode, cursor_before, meta_json, status)
            VALUES (%s, %s, %s, %s, %s, %s, 'running')
            RETURNING run_id
            """,
            (run_id, repo_id, job_type, mode, cursor_json, meta),
        )
        result = cur.fetchone()
        return str(result[0])


def insert_sync_run_finish(
    conn: psycopg.Connection,
    run_id: str,
    status: str = "completed",
    cursor_after: Optional[Dict] = None,
    counts: Optional[Dict] = None,
    error_summary_json: Optional[Dict] = None,
    degradation_json: Optional[Dict] = None,
    logbook_item_id: Optional[int] = None,
) -> bool:
    """
    更新同步运行结束状态
    
    在同步脚本主流程结束时调用，更新 finished_at、status 和统计信息。
    即使"无新数据"也应调用此函数（status='no_data'）。
    
    Args:
        conn: 数据库连接
        run_id: 运行唯一标识（UUID 字符串）
        status: 结束状态 ('completed' | 'failed' | 'no_data')
        cursor_after: 同步后的游标快照
        counts: 计数统计 {synced_count, diff_count, bulk_count, degraded_count, ...}
        error_summary_json: 错误摘要 {error_type, message, ...}
        degradation_json: 降级详情 {degraded_reasons: {timeout: N, ...}, ...}
        logbook_item_id: 关联的 logbook item ID
    
    Returns:
        True 如果更新成功，False 如果记录不存在
    """
    cursor_json = json.dumps(cursor_after) if cursor_after else None
    counts_json = json.dumps(counts) if counts else '{}'
    error_json = json.dumps(error_summary_json) if error_summary_json else None
    degrade_json = json.dumps(degradation_json) if degradation_json else None
    
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE sync_runs
            SET finished_at = now(),
                status = %s,
                cursor_after = %s,
                counts = %s,
                error_summary_json = %s,
                degradation_json = %s,
                logbook_item_id = %s
            WHERE run_id = %s
            RETURNING run_id
            """,
            (status, cursor_json, counts_json, error_json, degrade_json, logbook_item_id, run_id),
        )
        result = cur.fetchone()
        return result is not None


def get_sync_run(
    conn: psycopg.Connection,
    run_id: str,
) -> Optional[Dict[str, Any]]:
    """
    获取同步运行记录
    
    Args:
        conn: 数据库连接
        run_id: 运行唯一标识
    
    Returns:
        sync_run 记录字典，不存在返回 None
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT run_id, repo_id, job_type, mode, started_at, finished_at,
                   cursor_before, cursor_after, counts, error_summary_json,
                   degradation_json, logbook_item_id, status, meta_json, synced_count
            FROM sync_runs
            WHERE run_id = %s
            """,
            (run_id,),
        )
        return cur.fetchone()


def get_latest_sync_run(
    conn: psycopg.Connection,
    repo_id: int,
    job_type: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    获取仓库的最近一次同步运行记录
    
    Args:
        conn: 数据库连接
        repo_id: 仓库 ID
        job_type: 可选的任务类型过滤
    
    Returns:
        最近的 sync_run 记录字典，不存在返回 None
    """
    with conn.cursor(row_factory=dict_row) as cur:
        if job_type:
            cur.execute(
                """
                SELECT run_id, repo_id, job_type, mode, started_at, finished_at,
                       cursor_before, cursor_after, counts, error_summary_json,
                       degradation_json, logbook_item_id, status, meta_json, synced_count
                FROM sync_runs
                WHERE repo_id = %s AND job_type = %s
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (repo_id, job_type),
            )
        else:
            cur.execute(
                """
                SELECT run_id, repo_id, job_type, mode, started_at, finished_at,
                       cursor_before, cursor_after, counts, error_summary_json,
                       degradation_json, logbook_item_id, status, meta_json, synced_count
                FROM sync_runs
                WHERE repo_id = %s
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (repo_id,),
            )
        return cur.fetchone()


# ============ SCM.sync_jobs 队列函数 ============


def enqueue_sync_job(
    conn: psycopg.Connection,
    repo_id: int,
    job_type: str,
    mode: str = "incremental",
    priority: int = 0,
    payload_json: Optional[Dict] = None,
) -> Optional[str]:
    """
    向 scm.sync_jobs 队列添加同步任务
    
    使用 ON CONFLICT (repo_id, job_type) WHERE status IN ('pending','running') DO NOTHING
    确保同一仓库同一类型的活跃任务不会重复入队。
    
    Args:
        conn: 数据库连接
        repo_id: 仓库 ID
        job_type: 任务类型 ('commits' | 'mrs' | 'reviews')
        mode: 同步模式 ('incremental' | 'backfill')
        priority: 优先级（越小越优先）
        payload_json: 任务参数/元数据（会写入 payload_json）
    
    Returns:
        job_id，如果因冲突未插入则返回 None
    """
    payload = json.dumps(payload_json or {}, ensure_ascii=False)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sync_jobs (repo_id, job_type, mode, priority, payload_json, status)
            VALUES (%s, %s, %s, %s, %s, 'pending')
            ON CONFLICT (repo_id, job_type) WHERE status IN ('pending', 'running')
            DO NOTHING
            RETURNING job_id
            """,
            (repo_id, job_type, mode, priority, payload),
        )
        row = cur.fetchone()
        # 注意：job_id 是 uuid 类型，这里统一转为字符串返回
        return str(row[0]) if row else None


def enqueue_sync_jobs_batch(
    conn: psycopg.Connection,
    jobs: List[Dict[str, Any]],
) -> List[Optional[str]]:
    """
    批量向 scm.sync_jobs 队列添加同步任务
    
    Args:
        conn: 数据库连接
        jobs: 任务列表，每项包含:
            - repo_id: int
            - job_type: str
            - mode: str (optional, default 'incremental')
            - priority: int (optional, default 0)
            - payload_json: Dict (optional)
    
    Returns:
        job_id 列表，重复任务返回 None
    """
    ids = []
    for job in jobs:
        job_id = enqueue_sync_job(
            conn,
            repo_id=job["repo_id"],
            job_type=job["job_type"],
            mode=job.get("mode", "incremental"),
            priority=job.get("priority", 0),
            payload_json=job.get("payload_json"),
        )
        ids.append(job_id)
    return ids


def get_pending_sync_jobs_count(
    conn: psycopg.Connection,
) -> int:
    """
    获取当前待处理的同步任务数量
    
    Args:
        conn: 数据库连接
    
    Returns:
        待处理任务数量
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) FROM sync_jobs
            WHERE status = 'pending'
            """
        )
        result = cur.fetchone()
        return result[0] if result else 0


def get_queued_repo_job_pairs(
    conn: psycopg.Connection,
) -> List[tuple]:
    """
    获取当前队列中的 (repo_id, job_type) 对列表
    
    Args:
        conn: 数据库连接
    
    Returns:
        (repo_id, job_type) 元组列表
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT repo_id, job_type FROM sync_jobs
            WHERE status IN ('pending', 'running')
            """
        )
        return [(row[0], row[1]) for row in cur.fetchall()]


@dataclass
class BudgetSnapshot:
    """
    预算占用快照
    
    记录调度器扫描时刻的活跃任务计数，用于策略层决策。
    
    Attributes:
        global_running: 全局正在运行的任务数
        global_pending: 全局待处理的任务数
        global_active: 全局活跃任务数（running + pending）
        by_instance: 按 gitlab_instance 分组的活跃任务计数
        by_tenant: 按 tenant_id 分组的活跃任务计数
    """
    global_running: int = 0
    global_pending: int = 0
    global_active: int = 0
    by_instance: Dict[str, int] = field(default_factory=dict)
    by_tenant: Dict[str, int] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "global_running": self.global_running,
            "global_pending": self.global_pending,
            "global_active": self.global_active,
            "by_instance": dict(self.by_instance),
            "by_tenant": dict(self.by_tenant),
        }


def get_active_jobs_budget_snapshot(
    conn: psycopg.Connection,
    include_pending: bool = True,
) -> BudgetSnapshot:
    """
    获取活跃任务的预算占用快照
    
    从 scm.sync_jobs 统计当前活跃（pending + running 或仅 running）任务
    按 gitlab_instance、tenant_id 分组的计数。
    
    注意：需要关联 repos 表来获取 gitlab_instance 和 tenant_id 信息。
    - gitlab_instance: 从 repo URL 解析的主机名
    - tenant_id: 从 project_key 解析的前缀（第一个 / 之前的部分）
    
    Args:
        conn: 数据库连接
        include_pending: 是否包含 pending 状态，False 则仅统计 running
    
    Returns:
        BudgetSnapshot 对象，包含全局和分组计数
    """
    status_filter = "('pending', 'running')" if include_pending else "('running',)"
    
    with conn.cursor(row_factory=dict_row) as cur:
        # 查询活跃任务，关联 repos 表获取 URL 和 project_key
        cur.execute(
            f"""
            SELECT 
                j.status,
                r.url,
                r.project_key,
                r.repo_type
            FROM sync_jobs j
            JOIN repos r ON j.repo_id = r.repo_id
            WHERE j.status IN {status_filter}
            """
        )
        rows = cur.fetchall()
    
    # 统计计数
    global_running = 0
    global_pending = 0
    by_instance: Dict[str, int] = {}
    by_tenant: Dict[str, int] = {}
    
    for row in rows:
        status = row["status"]
        url = row.get("url", "")
        project_key = row.get("project_key", "")
        repo_type = row.get("repo_type", "")
        
        # 统计全局计数
        if status == "running":
            global_running += 1
        else:
            global_pending += 1
        
        # 解析 gitlab_instance（仅 git 类型）
        gitlab_instance = None
        if repo_type == "git" and url and "://" in url:
            try:
                from urllib.parse import urlparse
                parsed = urlparse(url)
                gitlab_instance = parsed.netloc
            except Exception:
                pass
        
        # 解析 tenant_id
        tenant_id = None
        if project_key and "/" in project_key:
            tenant_id = project_key.split("/")[0]
        
        # 按实例计数
        if gitlab_instance:
            by_instance[gitlab_instance] = by_instance.get(gitlab_instance, 0) + 1
        
        # 按租户计数
        if tenant_id:
            by_tenant[tenant_id] = by_tenant.get(tenant_id, 0) + 1
    
    return BudgetSnapshot(
        global_running=global_running,
        global_pending=global_pending,
        global_active=global_running + global_pending,
        by_instance=by_instance,
        by_tenant=by_tenant,
    )


# ============ SCM.repos 查询函数（调度器用）============


def list_repos_for_scheduling(
    conn: psycopg.Connection,
    repo_type: Optional[str] = None,
    limit: int = 1000,
) -> List[Dict[str, Any]]:
    """
    获取需要调度的仓库列表
    
    Args:
        conn: 数据库连接
        repo_type: 可选的仓库类型过滤 ('git' | 'svn')
        limit: 最大返回数量
    
    Returns:
        仓库信息列表
    """
    params: List[Any] = []
    where_clause = "WHERE 1=1"
    
    if repo_type:
        where_clause += " AND repo_type = %s"
        params.append(repo_type)
    
    params.append(limit)
    
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT repo_id, repo_type, url, project_key, default_branch, created_at
            FROM repos
            {where_clause}
            ORDER BY repo_id
            LIMIT %s
            """,
            params,
        )
        return [dict(row) for row in cur.fetchall()]


def get_recent_sync_runs_stats(
    conn: psycopg.Connection,
    repo_id: int,
    job_type: Optional[str] = None,
    window_size: int = 10,
) -> Dict[str, Any]:
    """
    获取仓库最近同步运行的统计信息
    
    Args:
        conn: 数据库连接
        repo_id: 仓库 ID
        job_type: 可选的任务类型过滤
        window_size: 统计窗口大小（最近多少次运行）
    
    Returns:
        统计信息字典:
        {
            "total_runs": int,
            "completed_runs": int,
            "failed_runs": int,
            "no_data_runs": int,
            "total_429_hits": int,
            "total_requests": int,
            "last_run_at": float or None,
            "last_run_status": str or None,
        }
    """
    params: List[Any] = [repo_id]
    job_type_clause = ""
    
    if job_type:
        job_type_clause = "AND job_type = %s"
        params.append(job_type)
    
    params.append(window_size)
    
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            WITH recent_runs AS (
                SELECT 
                    run_id,
                    status,
                    started_at,
                    finished_at,
                    counts,
                    error_summary_json
                FROM sync_runs
                WHERE repo_id = %s {job_type_clause}
                ORDER BY started_at DESC
                LIMIT %s
            )
            SELECT
                COUNT(*) as total_runs,
                COUNT(*) FILTER (WHERE status = 'completed') as completed_runs,
                COUNT(*) FILTER (WHERE status = 'failed') as failed_runs,
                COUNT(*) FILTER (WHERE status = 'no_data') as no_data_runs,
                COALESCE(SUM((counts->>'total_429_hits')::int), 0) as total_429_hits,
                COALESCE(SUM((counts->>'total_requests')::int), 0) as total_requests,
                MAX(started_at) as last_run_at,
                (SELECT status FROM recent_runs ORDER BY started_at DESC LIMIT 1) as last_run_status
            FROM recent_runs
            """,
            params,
        )
        row = cur.fetchone()
        
        if row is None:
            return {
                "total_runs": 0,
                "completed_runs": 0,
                "failed_runs": 0,
                "no_data_runs": 0,
                "total_429_hits": 0,
                "total_requests": 0,
                "last_run_at": None,
                "last_run_status": None,
            }
        
        result = dict(row)
        # 转换 datetime 为 timestamp
        if result.get("last_run_at"):
            result["last_run_at"] = result["last_run_at"].timestamp()
        
        return result


def get_kv_cursors_for_repos(
    conn: psycopg.Connection,
    repo_ids: List[int],
    cursor_type: str,
) -> Dict[int, Dict[str, Any]]:
    """
    批量获取仓库的游标信息
    
    Args:
        conn: 数据库连接
        repo_ids: 仓库 ID 列表
        cursor_type: 游标类型 ('svn' | 'gitlab' | 'gitlab_mr' | 'gitlab_reviews')
    
    Returns:
        repo_id -> 游标数据的映射
    """
    if not repo_ids:
        return {}
    
    namespace = "scm.sync"
    keys = [f"{cursor_type}_cursor:{repo_id}" for repo_id in repo_ids]
    
    # 使用 ANY 进行批量查询
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT key, value_json, updated_at
            FROM kv
            WHERE namespace = %s AND key = ANY(%s)
            """,
            (namespace, keys),
        )
        
        result = {}
        for row in cur.fetchall():
            # 解析 key 获取 repo_id
            key = row["key"]
            try:
                repo_id = int(key.split(":")[-1])
                result[repo_id] = row["value_json"]
            except (ValueError, IndexError):
                continue
        
        return result


def get_kv_cursors_updated_at_for_repos(
    conn: psycopg.Connection,
    repo_ids: List[int],
    cursor_type: str,
) -> Dict[int, Optional[float]]:
    """
    批量获取仓库游标的 updated_at 时间戳
    
    专为调度器优化，仅返回 updated_at 时间戳，减少数据传输量。
    
    Args:
        conn: 数据库连接
        repo_ids: 仓库 ID 列表
        cursor_type: 游标类型 ('svn' | 'gitlab' | 'gitlab_mr' | 'gitlab_reviews')
    
    Returns:
        repo_id -> updated_at timestamp (float) 的映射，未找到的 repo 不在结果中
    """
    if not repo_ids:
        return {}
    
    namespace = "scm.sync"
    keys = [f"{cursor_type}_cursor:{repo_id}" for repo_id in repo_ids]
    
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT key, updated_at
            FROM kv
            WHERE namespace = %s AND key = ANY(%s)
            """,
            (namespace, keys),
        )
        
        result = {}
        for row in cur.fetchall():
            key = row["key"]
            try:
                repo_id = int(key.split(":")[-1])
                updated_at = row["updated_at"]
                if updated_at:
                    # 转换为 timestamp
                    result[repo_id] = updated_at.timestamp()
            except (ValueError, IndexError, AttributeError):
                continue
        
        return result


def get_recent_sync_runs_stats_batch(
    conn: psycopg.Connection,
    repo_ids: List[int],
    job_type: Optional[str] = None,
    window_size: int = 10,
) -> Dict[int, Dict[str, Any]]:
    """
    批量获取仓库最近同步运行的统计信息
    
    一次查询返回多个仓库的统计，替代逐个调用 get_recent_sync_runs_stats()。
    
    Args:
        conn: 数据库连接
        repo_ids: 仓库 ID 列表
        job_type: 可选的任务类型过滤
        window_size: 统计窗口大小（每个仓库最近多少次运行）
    
    Returns:
        repo_id -> 统计信息字典的映射，每个字典包含:
        {
            "total_runs": int,
            "completed_runs": int,
            "failed_runs": int,
            "no_data_runs": int,
            "total_429_hits": int,
            "total_requests": int,
            "last_run_at": float or None,
            "last_run_status": str or None,
        }
    """
    if not repo_ids:
        return {}
    
    # 构建 job_type 条件
    job_type_clause = ""
    params: List[Any] = [repo_ids, window_size]
    
    if job_type:
        job_type_clause = "AND job_type = %s"
        params.insert(1, job_type)
    
    # 使用窗口函数实现批量统计
    # 先按 repo_id 分组获取最近 N 条记录，再聚合统计
    query = f"""
        WITH ranked_runs AS (
            SELECT 
                repo_id,
                run_id,
                status,
                started_at,
                finished_at,
                counts,
                error_summary_json,
                ROW_NUMBER() OVER (PARTITION BY repo_id ORDER BY started_at DESC) as rn
            FROM sync_runs
            WHERE repo_id = ANY(%s) {job_type_clause}
        ),
        recent_runs AS (
            SELECT * FROM ranked_runs WHERE rn <= %s
        )
        SELECT
            repo_id,
            COUNT(*) as total_runs,
            COUNT(*) FILTER (WHERE status = 'completed') as completed_runs,
            COUNT(*) FILTER (WHERE status = 'failed') as failed_runs,
            COUNT(*) FILTER (WHERE status = 'no_data') as no_data_runs,
            COALESCE(SUM((counts->>'total_429_hits')::int), 0) as total_429_hits,
            COALESCE(SUM((counts->>'total_requests')::int), 0) as total_requests,
            MAX(started_at) as last_run_at,
            -- 获取最近一条记录的状态
            (SELECT status FROM recent_runs r2 WHERE r2.repo_id = recent_runs.repo_id ORDER BY started_at DESC LIMIT 1) as last_run_status
        FROM recent_runs
        GROUP BY repo_id
    """
    
    result: Dict[int, Dict[str, Any]] = {}
    
    # 初始化所有请求的 repo_id 为空统计
    empty_stats = {
        "total_runs": 0,
        "completed_runs": 0,
        "failed_runs": 0,
        "no_data_runs": 0,
        "total_429_hits": 0,
        "total_requests": 0,
        "last_run_at": None,
        "last_run_status": None,
    }
    for repo_id in repo_ids:
        result[repo_id] = empty_stats.copy()
    
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        
        for row in cur.fetchall():
            repo_id = row["repo_id"]
            stats = dict(row)
            
            # 转换 datetime 为 timestamp
            if stats.get("last_run_at"):
                stats["last_run_at"] = stats["last_run_at"].timestamp()
            
            # 移除 repo_id 字段（不需要在结果值中重复）
            stats.pop("repo_id", None)
            
            result[repo_id] = stats
    
    return result


def backfill_mr_source_ids(
    conn: psycopg.Connection,
    repo_id: Optional[int] = None,
    batch_size: int = 1000,
) -> int:
    """
    回填 scm.mrs 表中缺失的 source_id

    通过解析 mr_id（格式: platform:project:iid）提取 iid 来生成 source_id

    Args:
        conn: 数据库连接
        repo_id: 指定仓库 ID，为 None 时处理所有仓库
        batch_size: 每批处理的记录数

    Returns:
        更新的记录数
    """
    total_updated = 0
    
    with conn.cursor() as cur:
        while True:
            # 查询缺失 source_id 的记录
            if repo_id is not None:
                cur.execute(
                    """
                    SELECT mr_id, repo_id
                    FROM mrs
                    WHERE source_id IS NULL AND repo_id = %s
                    LIMIT %s
                    """,
                    (repo_id, batch_size),
                )
            else:
                cur.execute(
                    """
                    SELECT mr_id, repo_id
                    FROM mrs
                    WHERE source_id IS NULL
                    LIMIT %s
                    """,
                    (batch_size,),
                )
            
            rows = cur.fetchall()
            if not rows:
                break
            
            # 批量更新
            for mr_id_val, rid in rows:
                # 尝试从 mr_id 解析 iid（格式: platform:project:iid）
                try:
                    parts = mr_id_val.split(":")
                    if len(parts) >= 3:
                        iid = int(parts[-1])
                        new_source_id = build_mr_source_id(rid, iid)
                        cur.execute(
                            """
                            UPDATE mrs
                            SET source_id = %s
                            WHERE mr_id = %s
                            """,
                            (new_source_id, mr_id_val),
                        )
                except (ValueError, IndexError):
                    # 无法解析 iid，跳过
                    pass
            
            total_updated += len(rows)
            conn.commit()
    
    return total_updated


def backfill_scm_source_ids(
    conn: psycopg.Connection,
    repo_id: Optional[int] = None,
    batch_size: int = 1000,
) -> Dict[str, int]:
    """
    回填所有 SCM 表（svn_revisions, git_commits, mrs）中缺失的 source_id

    Args:
        conn: 数据库连接
        repo_id: 指定仓库 ID，为 None 时处理所有仓库
        batch_size: 每批处理的记录数

    Returns:
        各表更新记录数的字典:
        {
            "svn_revisions": int,
            "git_commits": int,
            "mrs": int,
        }
    """
    return {
        "svn_revisions": backfill_svn_source_ids(conn, repo_id, batch_size),
        "git_commits": backfill_git_source_ids(conn, repo_id, batch_size),
        "mrs": backfill_mr_source_ids(conn, repo_id, batch_size),
    }


# ============ 熔断健康统计函数 ============


# ---- 熔断 Key 规范 ----
# Key 格式: <project_key>:<scope>
# 示例:
#   - default:global              全局熔断状态
#   - default:pool:gitlab-prod    特定 pool 的熔断状态
#   - myproject:pool:svn-only     特定项目的特定 pool
#
# 旧 key 格式（已废弃，需兼容读取）:
#   - global                      旧全局 key
#   - worker:<worker_id>          旧 worker 级别 key（随机，不再使用）


# === 从 scm_sync_policy 导入熔断 key 构建函数（统一入口）===
# 为保持向后兼容性，这里重新导出这些函数
# 新代码应从 engram_step1.scm_sync_policy 导入
try:
    from engram_step1.scm_sync_policy import (
        build_circuit_breaker_key,
        get_legacy_key_fallbacks as _get_legacy_key_fallbacks,
    )
except ImportError:
    # 回退实现：当 scm_sync_policy 不可用时使用本地实现
    def build_circuit_breaker_key(
        project_key: str = "default",
        scope: str = "global",
        pool_name: Optional[str] = None,
        *,
        instance_key: Optional[str] = None,
        tenant_id: Optional[str] = None,
        worker_pool: Optional[str] = None,
    ) -> str:
        """
        构建熔断状态的规范化 key（本地回退实现）
        
        注意：此为回退实现，新代码应使用 engram_step1.scm_sync_policy.build_circuit_breaker_key
        
        向后兼容：前三个位置参数保持旧签名 (project_key, scope, pool_name)
        """
        project_key = project_key or "default"
        effective_pool = worker_pool or pool_name
        
        if scope and scope != "global":
            final_scope = scope
        elif effective_pool:
            final_scope = f"pool:{effective_pool}"
        elif instance_key:
            # 简单规范化
            if "://" in instance_key:
                from urllib.parse import urlparse
                try:
                    parsed = urlparse(instance_key)
                    normalized = parsed.netloc.lower() if parsed.netloc else instance_key
                except Exception:
                    normalized = instance_key
            else:
                normalized = instance_key.lower()
            final_scope = f"instance:{normalized}"
        elif tenant_id:
            final_scope = f"tenant:{tenant_id}"
        else:
            final_scope = "global"
        
        return f"{project_key}:{final_scope}"

    def _get_legacy_key_fallbacks(key: str) -> List[str]:
        """获取旧 key 格式的回退列表（本地回退实现）"""
        fallbacks = []
        parts = key.split(":", 1) if key else []
        
        if len(parts) == 2:
            project_key, scope = parts
            
            if scope == "global":
                fallbacks.append("global")
            elif scope.startswith("pool:"):
                pool_name = scope[5:]
                fallbacks.append(f"pool:{pool_name}")
                fallbacks.append(pool_name)
            elif scope.startswith("instance:"):
                instance_name = scope[9:]
                fallbacks.append(f"instance:{instance_name}")
                fallbacks.append(instance_name)
            elif scope.startswith("tenant:"):
                tenant_name = scope[7:]
                fallbacks.append(f"tenant:{tenant_name}")
                fallbacks.append(tenant_name)
        
        if key and ":" in key:
            if not key.startswith("worker:"):
                fallbacks.append(key)
        
        return fallbacks


def get_sync_runs_health_stats(
    conn: psycopg.Connection,
    repo_id: Optional[int] = None,
    job_type: Optional[str] = None,
    window_count: Optional[int] = None,
    window_minutes: Optional[int] = None,
) -> Dict[str, Any]:
    """
    获取同步运行的健康统计信息（用于熔断决策）
    
    支持两种窗口模式：
    - window_count: 最近 N 次运行
    - window_minutes: 最近 T 分钟内的运行
    
    如果两者都指定，同时应用两个条件。
    如果都未指定，默认使用最近 20 次。
    
    Args:
        conn: 数据库连接
        repo_id: 可选，按仓库 ID 过滤
        job_type: 可选，按任务类型过滤
        window_count: 最近 N 次运行
        window_minutes: 最近 T 分钟内的运行
    
    Returns:
        健康统计字典:
        {
            "total_runs": int,              # 总运行次数
            "completed_runs": int,          # 成功次数
            "failed_runs": int,             # 失败次数
            "no_data_runs": int,            # 无数据次数
            "running_runs": int,            # 正在运行次数
            "failed_rate": float,           # 失败率 (0.0~1.0)
            "total_429_hits": int,          # 总 429 命中次数
            "total_timeout_count": int,     # 总超时次数
            "total_requests": int,          # 总请求次数
            "rate_limit_rate": float,       # 429 命中率 (0.0~1.0)
            "avg_duration_seconds": float,  # 平均耗时秒数
            "max_duration_seconds": float,  # 最大耗时秒数
            "min_duration_seconds": float,  # 最小耗时秒数
            "first_run_at": float or None,  # 窗口内最早运行时间
            "last_run_at": float or None,   # 窗口内最后运行时间
        }
    """
    # 默认使用最近 20 次
    if window_count is None and window_minutes is None:
        window_count = 20
    
    # 构建查询条件
    conditions = []
    params: List[Any] = []
    
    if repo_id is not None:
        conditions.append("repo_id = %s")
        params.append(repo_id)
    
    if job_type is not None:
        conditions.append("job_type = %s")
        params.append(job_type)
    
    if window_minutes is not None:
        conditions.append("started_at >= now() - interval '%s minutes'")
        params.append(window_minutes)
    
    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)
    
    # 构建子查询限制行数
    limit_clause = ""
    if window_count is not None:
        limit_clause = f"LIMIT {int(window_count)}"
    
    query = f"""
        WITH recent_runs AS (
            SELECT 
                run_id,
                status,
                started_at,
                finished_at,
                counts,
                error_summary_json,
                EXTRACT(EPOCH FROM (COALESCE(finished_at, now()) - started_at)) as duration_seconds
            FROM sync_runs
            {where_clause}
            ORDER BY started_at DESC
            {limit_clause}
        )
        SELECT
            COUNT(*) as total_runs,
            COUNT(*) FILTER (WHERE status = 'completed') as completed_runs,
            COUNT(*) FILTER (WHERE status = 'failed') as failed_runs,
            COUNT(*) FILTER (WHERE status = 'no_data') as no_data_runs,
            COUNT(*) FILTER (WHERE status = 'running') as running_runs,
            COALESCE(SUM((counts->>'total_429_hits')::int), 0) as total_429_hits,
            COALESCE(SUM((counts->>'timeout_count')::int), 0) as total_timeout_count,
            COALESCE(SUM((counts->>'total_requests')::int), 0) as total_requests,
            AVG(duration_seconds) as avg_duration_seconds,
            MAX(duration_seconds) as max_duration_seconds,
            MIN(duration_seconds) as min_duration_seconds,
            MIN(started_at) as first_run_at,
            MAX(started_at) as last_run_at
        FROM recent_runs
    """
    
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        row = cur.fetchone()
        
        if row is None or row.get("total_runs", 0) == 0:
            return {
                "total_runs": 0,
                "completed_runs": 0,
                "failed_runs": 0,
                "no_data_runs": 0,
                "running_runs": 0,
                "failed_rate": 0.0,
                "total_429_hits": 0,
                "total_timeout_count": 0,
                "total_requests": 0,
                "rate_limit_rate": 0.0,
                "avg_duration_seconds": 0.0,
                "max_duration_seconds": 0.0,
                "min_duration_seconds": 0.0,
                "first_run_at": None,
                "last_run_at": None,
            }
        
        result = dict(row)
        
        # 计算失败率
        total_finished = result["completed_runs"] + result["failed_runs"] + result["no_data_runs"]
        result["failed_rate"] = (
            result["failed_runs"] / total_finished if total_finished > 0 else 0.0
        )
        
        # 计算 429 命中率
        result["rate_limit_rate"] = (
            result["total_429_hits"] / result["total_requests"]
            if result["total_requests"] > 0 else 0.0
        )
        
        # 转换 datetime 为 timestamp
        if result.get("first_run_at"):
            result["first_run_at"] = result["first_run_at"].timestamp()
        if result.get("last_run_at"):
            result["last_run_at"] = result["last_run_at"].timestamp()
        
        # 确保数值类型
        result["avg_duration_seconds"] = float(result.get("avg_duration_seconds") or 0.0)
        result["max_duration_seconds"] = float(result.get("max_duration_seconds") or 0.0)
        result["min_duration_seconds"] = float(result.get("min_duration_seconds") or 0.0)
        
        return result


def get_sync_runs_health_stats_by_dimension(
    conn: psycopg.Connection,
    dimension: str = "instance",
    window_count: Optional[int] = None,
    window_minutes: Optional[int] = None,
) -> Dict[str, Dict[str, Any]]:
    """
    按维度（instance/tenant）聚合同步运行的健康统计信息
    
    从 scm.repos 解析 instance/tenant，并与 scm.sync_runs 关联统计。
    
    instance 解析规则: 从 repos.url 提取 host 部分
    tenant 解析规则: 从 repos.project_key 提取第一段（假设格式为 'tenant/project'）
    
    Args:
        conn: 数据库连接
        dimension: 聚合维度，'instance' 或 'tenant'
        window_count: 最近 N 次运行
        window_minutes: 最近 T 分钟内的运行
    
    Returns:
        按维度聚合的健康统计字典:
        {
            "<instance_or_tenant_id>": {
                "total_runs": int,
                "completed_runs": int,
                "failed_runs": int,
                "no_data_runs": int,
                "running_runs": int,
                "failed_rate": float,
                "total_429_hits": int,
                "total_timeout_count": int,
                "total_requests": int,
                "rate_limit_rate": float,
                "avg_duration_seconds": float,
                "repo_count": int,           # 该维度下的仓库数量
            },
            ...
        }
    """
    # 默认使用最近 20 次（per dimension）
    if window_count is None and window_minutes is None:
        window_count = 20
    
    # 构建维度字段表达式
    if dimension == "instance":
        # 从 url 提取 host: 'https://gitlab.example.com/path' -> 'gitlab.example.com'
        # 使用 PostgreSQL 正则或 substring 提取
        dimension_expr = """
            CASE 
                WHEN r.url ~ '^[a-z]+://' 
                THEN regexp_replace(r.url, '^[a-z]+://([^/]+).*$', '\\1')
                ELSE r.url
            END
        """
    elif dimension == "tenant":
        # 从 project_key 提取第一段: 'tenant/project' -> 'tenant'
        dimension_expr = "split_part(r.project_key, '/', 1)"
    else:
        raise ValueError(f"Unknown dimension: {dimension}, expected 'instance' or 'tenant'")
    
    # 时间窗口条件
    time_condition = ""
    params: List[Any] = []
    
    if window_minutes is not None:
        time_condition = "AND sr.started_at >= now() - interval '%s minutes'"
        params.append(window_minutes)
    
    # 行数限制子查询（per dimension per repo）
    limit_clause = ""
    if window_count is not None:
        limit_clause = f"LIMIT {int(window_count)}"
    
    # 使用 window function 实现 per-dimension 行数限制
    query = f"""
        WITH dimension_repos AS (
            SELECT 
                r.repo_id,
                r.repo_type,
                ({dimension_expr}) as dimension_key
            FROM repos r
            WHERE ({dimension_expr}) IS NOT NULL AND ({dimension_expr}) != ''
        ),
        ranked_runs AS (
            SELECT 
                dr.dimension_key,
                sr.run_id,
                sr.repo_id,
                sr.status,
                sr.started_at,
                sr.finished_at,
                sr.counts,
                EXTRACT(EPOCH FROM (COALESCE(sr.finished_at, now()) - sr.started_at)) as duration_seconds,
                ROW_NUMBER() OVER (
                    PARTITION BY dr.dimension_key 
                    ORDER BY sr.started_at DESC
                ) as rn
            FROM sync_runs sr
            JOIN dimension_repos dr ON sr.repo_id = dr.repo_id
            WHERE 1=1 {time_condition}
        ),
        filtered_runs AS (
            SELECT * FROM ranked_runs
            WHERE rn <= {window_count or 1000}
        )
        SELECT
            fr.dimension_key,
            COUNT(*) as total_runs,
            COUNT(*) FILTER (WHERE fr.status = 'completed') as completed_runs,
            COUNT(*) FILTER (WHERE fr.status = 'failed') as failed_runs,
            COUNT(*) FILTER (WHERE fr.status = 'no_data') as no_data_runs,
            COUNT(*) FILTER (WHERE fr.status = 'running') as running_runs,
            COALESCE(SUM((fr.counts->>'total_429_hits')::int), 0) as total_429_hits,
            COALESCE(SUM((fr.counts->>'timeout_count')::int), 0) as total_timeout_count,
            COALESCE(SUM((fr.counts->>'total_requests')::int), 0) as total_requests,
            AVG(fr.duration_seconds) as avg_duration_seconds,
            COUNT(DISTINCT fr.repo_id) as repo_count
        FROM filtered_runs fr
        GROUP BY fr.dimension_key
        ORDER BY fr.dimension_key
    """
    
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
        
        result: Dict[str, Dict[str, Any]] = {}
        
        for row in rows:
            dimension_key = row["dimension_key"]
            if not dimension_key:
                continue
            
            stats = dict(row)
            del stats["dimension_key"]  # 移除维度键，它作为字典 key
            
            # 计算失败率
            total_finished = stats["completed_runs"] + stats["failed_runs"] + stats["no_data_runs"]
            stats["failed_rate"] = (
                stats["failed_runs"] / total_finished if total_finished > 0 else 0.0
            )
            
            # 计算 429 命中率
            stats["rate_limit_rate"] = (
                stats["total_429_hits"] / stats["total_requests"]
                if stats["total_requests"] > 0 else 0.0
            )
            
            # 确保数值类型
            stats["avg_duration_seconds"] = float(stats.get("avg_duration_seconds") or 0.0)
            
            result[dimension_key] = stats
        
        return result


def save_circuit_breaker_state(
    conn: psycopg.Connection,
    key: str,
    state: Dict[str, Any],
) -> bool:
    """
    保存熔断状态到 logbook.kv
    
    使用 namespace='scm.sync_health' 存储熔断状态
    
    Key 规范: <project_key>:<scope>
    示例:
        - 'default:global'           全局熔断状态
        - 'default:pool:gitlab-prod' 特定 pool 的熔断状态
    
    Args:
        conn: 数据库连接
        key: 状态键名（推荐使用 build_circuit_breaker_key() 构建）
        state: 熔断状态数据
    
    Returns:
        True 表示成功
    """
    namespace = "scm.sync_health"
    
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO kv (namespace, key, value_json, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (namespace, key) DO UPDATE
            SET value_json = EXCLUDED.value_json, updated_at = now()
            """,
            (namespace, key, json.dumps(state)),
        )
        return True


def load_circuit_breaker_state(
    conn: psycopg.Connection,
    key: str,
    fallback_to_legacy: bool = True,
) -> Optional[Dict[str, Any]]:
    """
    从 logbook.kv 加载熔断状态
    
    支持旧 key 格式的兼容读取：
    - 先尝试读取新 key（如 'default:global'）
    - 如果未找到且 fallback_to_legacy=True，尝试读取旧 key（如 'global'）
    
    Args:
        conn: 数据库连接
        key: 状态键名（推荐使用 build_circuit_breaker_key() 构建）
        fallback_to_legacy: 是否尝试回退读取旧 key 格式（默认 True）
    
    Returns:
        熔断状态数据，不存在返回 None
    """
    namespace = "scm.sync_health"
    
    # 构建要尝试的 key 列表（新 key 优先）
    keys_to_try = [key]
    if fallback_to_legacy:
        keys_to_try.extend(_get_legacy_key_fallbacks(key))
    
    with conn.cursor(row_factory=dict_row) as cur:
        for try_key in keys_to_try:
            cur.execute(
                """
                SELECT value_json, updated_at
                FROM kv
                WHERE namespace = %s AND key = %s
                """,
                (namespace, try_key),
            )
            row = cur.fetchone()
            
            if row is not None:
                value = row["value_json"]
                if isinstance(value, str):
                    return json.loads(value)
                return value
        
        return None


def migrate_circuit_breaker_key(
    conn: psycopg.Connection,
    old_key: str,
    new_key: str,
    delete_old: bool = False,
) -> bool:
    """
    迁移熔断状态 key 到新格式
    
    将旧 key 的状态复制到新 key，可选删除旧 key。
    
    Args:
        conn: 数据库连接
        old_key: 旧 key 格式
        new_key: 新 key 格式（推荐使用 build_circuit_breaker_key() 构建）
        delete_old: 是否删除旧 key（默认 False，保留以便回退）
    
    Returns:
        True 如果迁移成功，False 如果旧 key 不存在
    """
    # 加载旧状态（不回退，直接读取指定 key）
    state = load_circuit_breaker_state(conn, old_key, fallback_to_legacy=False)
    
    if state is None:
        return False
    
    # 保存到新 key
    save_circuit_breaker_state(conn, new_key, state)
    
    # 可选删除旧 key
    if delete_old:
        delete_circuit_breaker_state(conn, old_key)
    
    return True


def delete_circuit_breaker_state(
    conn: psycopg.Connection,
    key: str,
) -> bool:
    """
    删除熔断状态
    
    Args:
        conn: 数据库连接
        key: 状态键名
    
    Returns:
        True 如果删除成功（记录存在）
    """
    namespace = "scm.sync_health"
    
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM kv
            WHERE namespace = %s AND key = %s
            RETURNING key
            """,
            (namespace, key),
        )
        return cur.fetchone() is not None


# ============ SCM.sync_pause: Repo/Job_type 暂停记录 ============
#
# 使用 logbook.kv 存储，namespace='scm.sync_pause'
# key 格式: repo:<repo_id>:<job_type>
# value_json 包含: {"paused_until": <timestamp>, "reason": <string>, "reason_code": <string>, "paused_at": <timestamp>, "failure_rate": <float>}


class PauseReasonCode:
    """
    暂停原因代码常量
    
    标准化的暂停原因代码，用于统一存储和按原因聚合统计。
    
    使用示例:
        set_repo_job_pause(conn, repo_id, job_type, 300, 
                          reason="failure_rate=0.35", 
                          reason_code=PauseReasonCode.ERROR_BUDGET)
    """
    # 错误预算超限（failure_rate 过高）
    ERROR_BUDGET = "error_budget"
    
    # 令牌桶/Rate Limit 暂停（bucket 级别的限流触发）
    RATE_LIMIT_BUCKET = "rate_limit_bucket"
    
    # 熔断器打开（全局或实例级熔断）
    CIRCUIT_OPEN = "circuit_open"
    
    # 手动暂停（运维人员手动设置）
    MANUAL = "manual"
    
    # 所有有效的 reason code（用于验证）
    ALL_CODES = frozenset([ERROR_BUDGET, RATE_LIMIT_BUCKET, CIRCUIT_OPEN, MANUAL])
    
    @classmethod
    def is_valid(cls, code: str) -> bool:
        """验证 reason code 是否有效"""
        return code in cls.ALL_CODES


def _build_pause_key(repo_id: int, job_type: str) -> str:
    """构建 pause 记录的 key"""
    return f"repo:{repo_id}:{job_type}"


def _parse_pause_key(key: str) -> Optional[tuple]:
    """
    解析 pause key，返回 (repo_id, job_type)
    
    Args:
        key: 格式为 'repo:<repo_id>:<job_type>'
    
    Returns:
        (repo_id, job_type) 或 None（如果格式不对）
    """
    parts = key.split(":", 2)
    if len(parts) != 3 or parts[0] != "repo":
        return None
    try:
        repo_id = int(parts[1])
        job_type = parts[2]
        return (repo_id, job_type)
    except (ValueError, IndexError):
        return None


@dataclass
class RepoPauseRecord:
    """
    Repo/Job_type 暂停记录
    
    Attributes:
        repo_id: 仓库 ID
        job_type: 任务类型（logical: commits/mrs/reviews）
        paused_until: 暂停到期时间戳 (Unix timestamp)
        reason: 暂停原因描述（详细信息，如 failure_rate=0.35）
        paused_at: 暂停开始时间戳 (Unix timestamp)
        failure_rate: 当前失败率
        reason_code: 标准化的暂停原因代码（参见 PauseReasonCode）
    """
    repo_id: int
    job_type: str
    paused_until: float  # Unix timestamp
    reason: str
    paused_at: float     # Unix timestamp
    failure_rate: float = 0.0
    # 标准化原因代码（用于聚合统计），可选（向后兼容旧数据）
    reason_code: str = ""
    
    def is_expired(self, now: Optional[float] = None) -> bool:
        """检查暂停是否已过期"""
        if now is None:
            now = time.time()
        return now >= self.paused_until
    
    def remaining_seconds(self, now: Optional[float] = None) -> float:
        """返回剩余暂停秒数"""
        if now is None:
            now = time.time()
        return max(0.0, self.paused_until - now)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "repo_id": self.repo_id,
            "job_type": self.job_type,
            "paused_until": self.paused_until,
            "reason": self.reason,
            "paused_at": self.paused_at,
            "failure_rate": self.failure_rate,
            "reason_code": self.reason_code,
        }
    
    @classmethod
    def from_dict(cls, repo_id: int, job_type: str, data: Dict[str, Any]) -> "RepoPauseRecord":
        """从字典创建记录"""
        return cls(
            repo_id=repo_id,
            job_type=job_type,
            paused_until=data.get("paused_until", 0.0),
            reason=data.get("reason", ""),
            paused_at=data.get("paused_at", 0.0),
            failure_rate=data.get("failure_rate", 0.0),
            reason_code=data.get("reason_code", ""),
        )


def set_repo_job_pause(
    conn: psycopg.Connection,
    repo_id: int,
    job_type: str,
    pause_duration_seconds: float,
    reason: str,
    failure_rate: float = 0.0,
    reason_code: str = "",
    now: Optional[float] = None,
) -> RepoPauseRecord:
    """
    设置 repo/job_type 的暂停记录
    
    Args:
        conn: 数据库连接
        repo_id: 仓库 ID
        job_type: 任务类型（logical: commits/mrs/reviews）
        pause_duration_seconds: 暂停时长（秒）
        reason: 暂停原因描述（详细信息）
        failure_rate: 当前失败率
        reason_code: 标准化原因代码（参见 PauseReasonCode），用于聚合统计
        now: 当前时间戳（默认使用 time.time()）
    
    Returns:
        创建的暂停记录
    
    Example:
        set_repo_job_pause(
            conn, repo_id=1, job_type="commits",
            pause_duration_seconds=300,
            reason="failure_rate=0.35 exceeded threshold=0.30",
            reason_code=PauseReasonCode.ERROR_BUDGET,
            failure_rate=0.35,
        )
    """
    if now is None:
        now = time.time()
    
    namespace = "scm.sync_pause"
    key = _build_pause_key(repo_id, job_type)
    
    record = RepoPauseRecord(
        repo_id=repo_id,
        job_type=job_type,
        paused_until=now + pause_duration_seconds,
        reason=reason,
        paused_at=now,
        failure_rate=failure_rate,
        reason_code=reason_code,
    )
    
    value_json = json.dumps(record.to_dict())
    
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO kv (namespace, key, value_json, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (namespace, key) DO UPDATE SET
                value_json = EXCLUDED.value_json,
                updated_at = now()
            """,
            (namespace, key, value_json),
        )
    
    return record


def get_repo_job_pause(
    conn: psycopg.Connection,
    repo_id: int,
    job_type: str,
    include_expired: bool = False,
    now: Optional[float] = None,
) -> Optional[RepoPauseRecord]:
    """
    获取 repo/job_type 的暂停记录
    
    Args:
        conn: 数据库连接
        repo_id: 仓库 ID
        job_type: 任务类型
        include_expired: 是否包含已过期的记录（默认 False）
        now: 当前时间戳
    
    Returns:
        暂停记录，不存在或已过期返回 None
    """
    if now is None:
        now = time.time()
    
    namespace = "scm.sync_pause"
    key = _build_pause_key(repo_id, job_type)
    
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT value_json FROM kv
            WHERE namespace = %s AND key = %s
            """,
            (namespace, key),
        )
        row = cur.fetchone()
    
    if row is None:
        return None
    
    value_json = row["value_json"]
    if isinstance(value_json, str):
        data = json.loads(value_json)
    else:
        data = value_json
    
    record = RepoPauseRecord.from_dict(repo_id, job_type, data)
    
    # 检查是否过期
    if not include_expired and record.is_expired(now):
        return None
    
    return record


def list_paused_repo_jobs(
    conn: psycopg.Connection,
    repo_ids: Optional[List[int]] = None,
    include_expired: bool = False,
    now: Optional[float] = None,
) -> List[RepoPauseRecord]:
    """
    列出暂停的 repo/job_type 记录
    
    Args:
        conn: 数据库连接
        repo_ids: 可选的仓库 ID 列表过滤
        include_expired: 是否包含已过期的记录
        now: 当前时间戳
    
    Returns:
        暂停记录列表
    """
    if now is None:
        now = time.time()
    
    namespace = "scm.sync_pause"
    
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT key, value_json FROM kv
            WHERE namespace = %s
            """,
            (namespace,),
        )
        rows = cur.fetchall()
    
    records: List[RepoPauseRecord] = []
    
    for row in rows:
        key = row["key"]
        parsed = _parse_pause_key(key)
        if parsed is None:
            continue
        
        repo_id, job_type = parsed
        
        # 过滤 repo_ids
        if repo_ids is not None and repo_id not in repo_ids:
            continue
        
        value_json = row["value_json"]
        if isinstance(value_json, str):
            data = json.loads(value_json)
        else:
            data = value_json
        
        record = RepoPauseRecord.from_dict(repo_id, job_type, data)
        
        # 过滤过期
        if not include_expired and record.is_expired(now):
            continue
        
        records.append(record)
    
    return records


def get_paused_repo_job_pairs(
    conn: psycopg.Connection,
    repo_ids: Optional[List[int]] = None,
    now: Optional[float] = None,
) -> set:
    """
    获取当前暂停的 (repo_id, job_type) 集合
    
    用于在 select_jobs_to_enqueue 之前快速过滤。
    
    Args:
        conn: 数据库连接
        repo_ids: 可选的仓库 ID 列表过滤
        now: 当前时间戳
    
    Returns:
        暂停的 (repo_id, job_type) 集合
    """
    records = list_paused_repo_jobs(conn, repo_ids=repo_ids, include_expired=False, now=now)
    return {(r.repo_id, r.job_type) for r in records}


def clear_repo_job_pause(
    conn: psycopg.Connection,
    repo_id: int,
    job_type: str,
) -> bool:
    """
    清除 repo/job_type 的暂停记录
    
    Args:
        conn: 数据库连接
        repo_id: 仓库 ID
        job_type: 任务类型
    
    Returns:
        True 如果记录存在并被删除
    """
    namespace = "scm.sync_pause"
    key = _build_pause_key(repo_id, job_type)
    
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM kv
            WHERE namespace = %s AND key = %s
            RETURNING key
            """,
            (namespace, key),
        )
        return cur.fetchone() is not None


def clear_expired_pauses(
    conn: psycopg.Connection,
    now: Optional[float] = None,
) -> int:
    """
    清除所有已过期的暂停记录
    
    Args:
        conn: 数据库连接
        now: 当前时间戳
    
    Returns:
        删除的记录数
    """
    if now is None:
        now = time.time()
    
    # 获取所有记录（包括过期的）
    all_records = list_paused_repo_jobs(conn, include_expired=True, now=now)
    
    deleted_count = 0
    for record in all_records:
        if record.is_expired(now):
            if clear_repo_job_pause(conn, record.repo_id, record.job_type):
                deleted_count += 1
    
    return deleted_count


def check_and_auto_unpause(
    conn: psycopg.Connection,
    repo_id: int,
    job_type: str,
    failure_rate_threshold: float = 0.3,
    window_size: int = 10,
) -> Optional[RepoPauseRecord]:
    """
    检查健康指标并自动解除暂停
    
    如果 repo/job_type 有暂停记录，且当前 failed_rate 低于阈值，则自动解除暂停。
    
    Args:
        conn: 数据库连接
        repo_id: 仓库 ID
        job_type: 任务类型
        failure_rate_threshold: 失败率阈值（低于此值则解除暂停）
        window_size: 统计窗口大小
    
    Returns:
        如果暂停被解除，返回解除前的记录；否则返回 None
    """
    # 检查是否有暂停记录（包括过期的，因为过期也需要清理）
    pause_record = get_repo_job_pause(conn, repo_id, job_type, include_expired=True)
    
    if pause_record is None:
        return None
    
    # 如果已过期，直接清除
    if pause_record.is_expired():
        clear_repo_job_pause(conn, repo_id, job_type)
        return pause_record
    
    # 获取当前健康统计
    stats = get_recent_sync_runs_stats(conn, repo_id, job_type=job_type, window_size=window_size)
    
    if stats["total_runs"] == 0:
        # 没有运行记录，不自动解除（等待自然过期）
        return None
    
    # 计算失败率
    current_failure_rate = (
        stats["failed_runs"] / stats["total_runs"]
        if stats["total_runs"] > 0 else 0.0
    )
    
    # 如果失败率低于阈值，解除暂停
    if current_failure_rate < failure_rate_threshold:
        clear_repo_job_pause(conn, repo_id, job_type)
        return pause_record
    
    return None


def batch_check_and_auto_unpause(
    conn: psycopg.Connection,
    repo_ids: List[int],
    failure_rate_threshold: float = 0.3,
    window_size: int = 10,
) -> List[RepoPauseRecord]:
    """
    批量检查健康指标并自动解除暂停
    
    用于在调度扫描开始时批量检查所有暂停的 repo/job_type。
    
    Args:
        conn: 数据库连接
        repo_ids: 仓库 ID 列表
        failure_rate_threshold: 失败率阈值
        window_size: 统计窗口大小
    
    Returns:
        被解除暂停的记录列表
    """
    if not repo_ids:
        return []
    
    # 获取这些 repo 的所有暂停记录
    pause_records = list_paused_repo_jobs(conn, repo_ids=repo_ids, include_expired=True)
    
    if not pause_records:
        return []
    
    # 批量获取健康统计
    stats_map = get_recent_sync_runs_stats_batch(conn, repo_ids, window_size=window_size)
    
    unpaused: List[RepoPauseRecord] = []
    now = time.time()
    
    for record in pause_records:
        # 如果已过期，直接清除
        if record.is_expired(now):
            clear_repo_job_pause(conn, record.repo_id, record.job_type)
            unpaused.append(record)
            continue
        
        # 获取该 repo 的统计
        stats = stats_map.get(record.repo_id, {})
        total_runs = stats.get("total_runs", 0)
        
        if total_runs == 0:
            continue
        
        # 计算失败率
        failed_runs = stats.get("failed_runs", 0)
        current_failure_rate = failed_runs / total_runs
        
        # 如果失败率低于阈值，解除暂停
        if current_failure_rate < failure_rate_threshold:
            clear_repo_job_pause(conn, record.repo_id, record.job_type)
            unpaused.append(record)
    
    return unpaused


# ============ SCM.sync_rate_limits: Token Bucket 限流函数 ============


@dataclass
class RateLimitResult:
    """限流操作结果"""
    allowed: bool              # 是否允许请求
    tokens_remaining: float    # 剩余令牌数
    wait_seconds: float = 0.0  # 如果不允许，需要等待的秒数
    paused_until: Optional[datetime] = None  # 如果被暂停，暂停到什么时候
    instance_key: str = ""     # 实例标识
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "tokens_remaining": round(self.tokens_remaining, 3),
            "wait_seconds": round(self.wait_seconds, 3),
            "paused_until": self.paused_until.isoformat() if self.paused_until else None,
            "instance_key": self.instance_key,
        }


def ensure_rate_limit_bucket(
    conn: psycopg.Connection,
    instance_key: str,
    rate: float = 10.0,
    burst: int = 20,
) -> bool:
    """
    确保指定实例的限流桶存在（如不存在则创建）
    
    Args:
        conn: 数据库连接
        instance_key: 实例标识（如 GitLab 域名）
        rate: 令牌补充速率（tokens/sec）
        burst: 最大令牌容量
    
    Returns:
        True 表示成功（已存在或新创建）
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sync_rate_limits (instance_key, tokens, rate, burst, updated_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (instance_key) DO NOTHING
            """,
            (instance_key, float(burst), rate, burst),
        )
        return True


def update_rate_limit_config(
    conn: psycopg.Connection,
    instance_key: str,
    rate: Optional[float] = None,
    burst: Optional[int] = None,
) -> bool:
    """
    更新限流配置（rate/burst）
    
    Args:
        conn: 数据库连接
        instance_key: 实例标识
        rate: 新的令牌补充速率
        burst: 新的最大令牌容量
    
    Returns:
        True 如果更新成功（记录存在）
    """
    set_clauses = []
    params: List[Any] = []
    
    if rate is not None:
        set_clauses.append("rate = %s")
        params.append(rate)
    
    if burst is not None:
        set_clauses.append("burst = %s")
        params.append(burst)
    
    if not set_clauses:
        return False
    
    params.append(instance_key)
    
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE sync_rate_limits
            SET {', '.join(set_clauses)}, updated_at = now()
            WHERE instance_key = %s
            RETURNING instance_key
            """,
            params,
        )
        return cur.fetchone() is not None


def consume_rate_limit_token(
    conn: psycopg.Connection,
    instance_key: str,
    tokens_needed: float = 1.0,
    default_rate: float = 10.0,
    default_burst: int = 20,
) -> RateLimitResult:
    """
    原子操作：refill + consume（事务内 SELECT FOR UPDATE + UPDATE）
    
    此函数执行以下步骤：
    1. SELECT FOR UPDATE 锁定行
    2. 计算时间差，补充令牌（tokens = min(burst, tokens + elapsed * rate)）
    3. 检查 paused_until（如果当前时间 < paused_until，拒绝请求）
    4. 如果 tokens >= tokens_needed，扣减并返回成功
    5. 否则返回需要等待的时间
    
    注意：调用者需要在事务中调用此函数，或使用 autocommit=True 连接
    
    Args:
        conn: 数据库连接
        instance_key: 实例标识
        tokens_needed: 需要的令牌数（默认 1.0）
        default_rate: 如果桶不存在时的默认速率
        default_burst: 如果桶不存在时的默认容量
    
    Returns:
        RateLimitResult 对象
    """
    with conn.cursor(row_factory=dict_row) as cur:
        # 1. 尝试获取现有记录（FOR UPDATE 加行锁）
        cur.execute(
            """
            SELECT instance_key, tokens, updated_at, rate, burst, paused_until
            FROM sync_rate_limits
            WHERE instance_key = %s
            FOR UPDATE
            """,
            (instance_key,),
        )
        row = cur.fetchone()
        
        now = datetime.utcnow()
        
        if row is None:
            # 桶不存在，创建新桶并消费一个令牌
            new_tokens = float(default_burst) - tokens_needed
            cur.execute(
                """
                INSERT INTO sync_rate_limits 
                    (instance_key, tokens, rate, burst, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (instance_key) DO UPDATE
                SET tokens = sync_rate_limits.tokens,
                    updated_at = sync_rate_limits.updated_at
                RETURNING tokens
                """,
                (instance_key, new_tokens, default_rate, default_burst, now),
            )
            return RateLimitResult(
                allowed=True,
                tokens_remaining=new_tokens,
                wait_seconds=0.0,
                paused_until=None,
                instance_key=instance_key,
            )
        
        # 2. 检查 paused_until
        paused_until = row.get("paused_until")
        if paused_until:
            # 确保 paused_until 是 timezone-naive 以便比较
            if paused_until.tzinfo is not None:
                paused_until = paused_until.replace(tzinfo=None)
            
            if now < paused_until:
                wait_seconds = (paused_until - now).total_seconds()
                return RateLimitResult(
                    allowed=False,
                    tokens_remaining=row["tokens"],
                    wait_seconds=wait_seconds,
                    paused_until=paused_until,
                    instance_key=instance_key,
                )
        
        # 3. 计算时间差并补充令牌
        last_update = row["updated_at"]
        if last_update.tzinfo is not None:
            last_update = last_update.replace(tzinfo=None)
        
        elapsed = (now - last_update).total_seconds()
        rate = row["rate"]
        burst = row["burst"]
        
        # 补充令牌
        old_tokens = row["tokens"]
        refilled_tokens = min(float(burst), old_tokens + elapsed * rate)
        
        # 4. 检查是否有足够的令牌
        if refilled_tokens >= tokens_needed:
            # 扣减令牌
            new_tokens = refilled_tokens - tokens_needed
            cur.execute(
                """
                UPDATE sync_rate_limits
                SET tokens = %s, updated_at = %s, paused_until = NULL
                WHERE instance_key = %s
                """,
                (new_tokens, now, instance_key),
            )
            return RateLimitResult(
                allowed=True,
                tokens_remaining=new_tokens,
                wait_seconds=0.0,
                paused_until=None,
                instance_key=instance_key,
            )
        else:
            # 令牌不足，更新 tokens 并返回等待时间
            cur.execute(
                """
                UPDATE sync_rate_limits
                SET tokens = %s, updated_at = %s
                WHERE instance_key = %s
                """,
                (refilled_tokens, now, instance_key),
            )
            
            # 计算需要等待的时间
            tokens_deficit = tokens_needed - refilled_tokens
            wait_seconds = tokens_deficit / rate if rate > 0 else 60.0
            
            return RateLimitResult(
                allowed=False,
                tokens_remaining=refilled_tokens,
                wait_seconds=wait_seconds,
                paused_until=None,
                instance_key=instance_key,
            )


def pause_rate_limit_bucket(
    conn: psycopg.Connection,
    instance_key: str,
    retry_after_seconds: float,
    record_429: bool = True,
) -> bool:
    """
    暂停限流桶直到指定时间（用于 429 Retry-After 处理）
    
    Args:
        conn: 数据库连接
        instance_key: 实例标识
        retry_after_seconds: 暂停秒数
        record_429: 是否在 meta_json 中记录 429 信息
    
    Returns:
        True 如果更新成功
    """
    now = datetime.utcnow()
    pause_until = now + timedelta(seconds=retry_after_seconds)
    
    with conn.cursor() as cur:
        if record_429:
            # 更新 paused_until 并记录 429 信息到 meta_json
            cur.execute(
                """
                UPDATE sync_rate_limits
                SET paused_until = %s,
                    tokens = 0,
                    updated_at = %s,
                    meta_json = COALESCE(meta_json, '{}'::jsonb)
                        || jsonb_build_object(
                            'last_429_at', %s,
                            'last_retry_after', %s,
                            'consecutive_429_count', 
                            COALESCE((meta_json->>'consecutive_429_count')::int, 0) + 1
                        )
                WHERE instance_key = %s
                RETURNING instance_key
                """,
                (pause_until, now, now.isoformat() + "Z", retry_after_seconds, instance_key),
            )
        else:
            cur.execute(
                """
                UPDATE sync_rate_limits
                SET paused_until = %s, tokens = 0, updated_at = %s
                WHERE instance_key = %s
                RETURNING instance_key
                """,
                (pause_until, now, instance_key),
            )
        
        result = cur.fetchone()
        
        if result is None:
            # 桶不存在，先创建再暂停
            cur.execute(
                """
                INSERT INTO sync_rate_limits 
                    (instance_key, tokens, rate, burst, paused_until, updated_at, meta_json)
                VALUES (%s, 0, 10.0, 20, %s, %s, %s)
                """,
                (
                    instance_key,
                    pause_until,
                    now,
                    json.dumps({"last_429_at": now.isoformat() + "Z", "last_retry_after": retry_after_seconds}),
                ),
            )
        
        return True


def clear_rate_limit_pause(
    conn: psycopg.Connection,
    instance_key: str,
) -> bool:
    """
    清除限流桶的暂停状态并重置连续 429 计数
    
    Args:
        conn: 数据库连接
        instance_key: 实例标识
    
    Returns:
        True 如果更新成功
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE sync_rate_limits
            SET paused_until = NULL,
                meta_json = COALESCE(meta_json, '{}'::jsonb)
                    || jsonb_build_object('consecutive_429_count', 0)
            WHERE instance_key = %s
            RETURNING instance_key
            """,
            (instance_key,),
        )
        return cur.fetchone() is not None


def get_rate_limit_status(
    conn: psycopg.Connection,
    instance_key: str,
) -> Optional[Dict[str, Any]]:
    """
    获取限流桶的当前状态
    
    Args:
        conn: 数据库连接
        instance_key: 实例标识
    
    Returns:
        限流桶状态字典，不存在返回 None
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT instance_key, tokens, updated_at, rate, burst, 
                   paused_until, meta_json
            FROM sync_rate_limits
            WHERE instance_key = %s
            """,
            (instance_key,),
        )
        row = cur.fetchone()
        
        if row is None:
            return None
        
        result = dict(row)
        
        # 计算当前实际令牌数（加上 refill）
        now = datetime.utcnow()
        last_update = result["updated_at"]
        if last_update.tzinfo is not None:
            last_update = last_update.replace(tzinfo=None)
        
        elapsed = (now - last_update).total_seconds()
        current_tokens = min(result["burst"], result["tokens"] + elapsed * result["rate"])
        result["current_tokens"] = round(current_tokens, 3)
        
        # 检查是否被暂停
        paused_until = result.get("paused_until")
        if paused_until:
            if paused_until.tzinfo is not None:
                paused_until = paused_until.replace(tzinfo=None)
            result["is_paused"] = now < paused_until
            result["pause_remaining_seconds"] = max(0, (paused_until - now).total_seconds())
        else:
            result["is_paused"] = False
            result["pause_remaining_seconds"] = 0
        
        return result


def list_rate_limit_buckets(
    conn: psycopg.Connection,
) -> List[Dict[str, Any]]:
    """
    列出所有限流桶的状态
    
    Args:
        conn: 数据库连接
    
    Returns:
        限流桶状态列表
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT instance_key, tokens, updated_at, rate, burst,
                   paused_until, meta_json
            FROM sync_rate_limits
            ORDER BY instance_key
            """
        )
        rows = cur.fetchall()
    
    now = datetime.utcnow()
    results = []
    
    for row in rows:
        result = dict(row)
        
        # 计算当前实际令牌数
        last_update = result["updated_at"]
        if last_update.tzinfo is not None:
            last_update = last_update.replace(tzinfo=None)
        
        elapsed = (now - last_update).total_seconds()
        current_tokens = min(result["burst"], result["tokens"] + elapsed * result["rate"])
        result["current_tokens"] = round(current_tokens, 3)
        
        # 检查暂停状态
        paused_until = result.get("paused_until")
        if paused_until:
            if paused_until.tzinfo is not None:
                paused_until = paused_until.replace(tzinfo=None)
            result["is_paused"] = now < paused_until
        else:
            result["is_paused"] = False
        
        results.append(result)
    
    return results


# ============ 只读查询函数（运维排障用）============


def list_repos(
    conn: psycopg.Connection,
    repo_type: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """
    查询仓库列表（只读）
    
    Args:
        conn: 数据库连接
        repo_type: 可选的仓库类型过滤 ('git' | 'svn')
        limit: 最大返回数量
    
    Returns:
        仓库信息列表
    """
    params: List[Any] = []
    where_clause = "WHERE 1=1"
    
    if repo_type:
        where_clause += " AND repo_type = %s"
        params.append(repo_type)
    
    params.append(limit)
    
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT repo_id, repo_type, url, project_key, default_branch, created_at
            FROM repos
            {where_clause}
            ORDER BY repo_id
            LIMIT %s
            """,
            params,
        )
        return [dict(row) for row in cur.fetchall()]


def list_sync_runs(
    conn: psycopg.Connection,
    repo_id: Optional[int] = None,
    job_type: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """
    查询同步运行记录（只读）
    
    Args:
        conn: 数据库连接
        repo_id: 可选的仓库 ID 过滤
        job_type: 可选的任务类型过滤
        status: 可选的状态过滤 ('running' | 'completed' | 'failed' | 'no_data')
        limit: 最大返回数量
    
    Returns:
        同步运行记录列表
    """
    params: List[Any] = []
    conditions = []
    
    if repo_id is not None:
        conditions.append("repo_id = %s")
        params.append(repo_id)
    
    if job_type:
        conditions.append("job_type = %s")
        params.append(job_type)
    
    if status:
        conditions.append("status = %s")
        params.append(status)
    
    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)
    
    params.append(limit)
    
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT 
                run_id, repo_id, job_type, mode, status,
                started_at, finished_at,
                cursor_before, cursor_after,
                counts, error_summary_json, degradation_json,
                logbook_item_id, meta_json, synced_count
            FROM sync_runs
            {where_clause}
            ORDER BY started_at DESC
            LIMIT %s
            """,
            params,
        )
        return [dict(row) for row in cur.fetchall()]


def list_sync_jobs(
    conn: psycopg.Connection,
    repo_id: Optional[int] = None,
    job_type: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """
    查询同步任务队列（只读）
    
    Args:
        conn: 数据库连接
        repo_id: 可选的仓库 ID 过滤
        job_type: 可选的任务类型过滤
        status: 可选的状态过滤 ('pending' | 'running' | 'completed' | 'failed' | 'dead')
        limit: 最大返回数量
    
    Returns:
        同步任务列表
    """
    params: List[Any] = []
    conditions = []
    
    if repo_id is not None:
        conditions.append("repo_id = %s")
        params.append(repo_id)
    
    if job_type:
        conditions.append("job_type = %s")
        params.append(job_type)
    
    if status:
        conditions.append("status = %s")
        params.append(status)
    
    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)
    
    params.append(limit)
    
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT 
                job_id, repo_id, job_type, mode, status,
                priority, attempts, max_attempts,
                not_before, locked_by, locked_at, lease_seconds,
                last_error, last_run_id,
                payload_json, created_at, updated_at
            FROM sync_jobs
            {where_clause}
            ORDER BY priority ASC, created_at ASC
            LIMIT %s
            """,
            params,
        )
        return [dict(row) for row in cur.fetchall()]


def list_sync_locks(
    conn: psycopg.Connection,
    repo_id: Optional[int] = None,
    job_type: Optional[str] = None,
    worker_id: Optional[str] = None,
    expired_only: bool = False,
    active_only: bool = False,
) -> List[Dict[str, Any]]:
    """
    查询同步锁状态（只读）
    
    Args:
        conn: 数据库连接
        repo_id: 可选的仓库 ID 过滤
        job_type: 可选的任务类型过滤
        worker_id: 可选的 worker 标识过滤
        expired_only: 仅返回过期的锁
        active_only: 仅返回活跃的锁
    
    Returns:
        锁状态列表
    """
    params: List[Any] = []
    conditions = []
    
    if repo_id is not None:
        conditions.append("repo_id = %s")
        params.append(repo_id)
    
    if job_type:
        conditions.append("job_type = %s")
        params.append(job_type)
    
    if worker_id:
        conditions.append("locked_by = %s")
        params.append(worker_id)
    
    if expired_only:
        conditions.append("""
            locked_by IS NOT NULL 
            AND locked_at + (lease_seconds || ' seconds')::interval < now()
        """)
    
    if active_only:
        conditions.append("locked_by IS NOT NULL")
    
    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)
    
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT 
                lock_id, repo_id, job_type,
                locked_by, locked_at, lease_seconds,
                updated_at, created_at,
                locked_by IS NOT NULL AS is_locked,
                CASE 
                    WHEN locked_at IS NULL THEN false
                    ELSE locked_at + (lease_seconds || ' seconds')::interval < now()
                END AS is_expired
            FROM sync_locks
            {where_clause}
            ORDER BY repo_id, job_type
            """,
            params,
        )
        return [dict(row) for row in cur.fetchall()]


def list_kv_cursors(
    conn: psycopg.Connection,
    namespace: str = "scm.sync",
    key_prefix: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    查询 KV 中的同步游标（只读）
    
    使用 logbook schema 的绝对路径，适用于 scm schema 的 search_path 环境。
    
    Args:
        conn: 数据库连接
        namespace: KV 命名空间，默认 'scm.sync'
        key_prefix: 可选的键名前缀过滤
    
    Returns:
        游标信息列表
    """
    params: List[Any] = [namespace]
    conditions = ["namespace = %s"]
    
    if key_prefix:
        conditions.append("key LIKE %s")
        params.append(f"{key_prefix}%")
    
    where_clause = "WHERE " + " AND ".join(conditions)
    
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT namespace, key, value_json, updated_at
            FROM logbook.kv
            {where_clause}
            ORDER BY key
            """,
            params,
        )
        return [dict(row) for row in cur.fetchall()]


def get_sync_status_summary(
    conn: psycopg.Connection,
) -> Dict[str, Any]:
    """
    获取同步状态摘要（只读）
    
    包含仓库、任务、锁的统计信息，用于运维监控。
    
    Args:
        conn: 数据库连接
    
    Returns:
        摘要信息字典
    """
    summary: Dict[str, Any] = {}
    
    with conn.cursor() as cur:
        # 仓库总数
        cur.execute("SELECT COUNT(*) FROM repos")
        summary["repos_count"] = cur.fetchone()[0]
        
        # 按类型统计仓库
        cur.execute("""
            SELECT repo_type, COUNT(*) 
            FROM repos 
            GROUP BY repo_type
        """)
        summary["repos_by_type"] = {row[0]: row[1] for row in cur.fetchall()}
        
        # 同步运行统计（最近 24 小时）
        cur.execute("""
            SELECT status, COUNT(*) 
            FROM sync_runs 
            WHERE started_at >= now() - interval '24 hours'
            GROUP BY status
        """)
        summary["runs_24h_by_status"] = {row[0]: row[1] for row in cur.fetchall()}
        
        # 同步任务队列统计
        cur.execute("""
            SELECT status, COUNT(*) 
            FROM sync_jobs 
            GROUP BY status
        """)
        summary["jobs_by_status"] = {row[0]: row[1] for row in cur.fetchall()}
        
        # 锁状态统计
        cur.execute("""
            SELECT 
                COUNT(*) FILTER (WHERE locked_by IS NOT NULL) AS active_locks,
                COUNT(*) FILTER (
                    WHERE locked_by IS NOT NULL 
                    AND locked_at + (lease_seconds || ' seconds')::interval < now()
                ) AS expired_locks
            FROM sync_locks
        """)
        row = cur.fetchone()
        summary["locks"] = {
            "active": row[0] or 0,
            "expired": row[1] or 0,
        }
        
        # 游标数量（使用 logbook schema）
        cur.execute("""
            SELECT COUNT(*) FROM logbook.kv 
            WHERE namespace = 'scm.sync'
        """)
        summary["cursors_count"] = cur.fetchone()[0]
    
    return summary


# ============ Reaper 辅助函数（清理过期任务/锁）============


def list_expired_running_jobs(
    conn: psycopg.Connection,
    grace_seconds: int = 60,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """
    查询过期的 running 任务（锁租约已过期）
    
    条件：status='running' 且 locked_at + lease_seconds + grace_seconds < now()
    
    Args:
        conn: 数据库连接
        grace_seconds: 宽限期秒数（额外等待时间，默认 60 秒）
        limit: 最大返回数量
    
    Returns:
        过期任务列表，包含完整的 job 信息
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT 
                job_id, repo_id, job_type, mode, status,
                priority, attempts, max_attempts,
                not_before, locked_by, locked_at, lease_seconds,
                last_error, last_run_id,
                payload_json, created_at, updated_at,
                EXTRACT(EPOCH FROM (now() - (locked_at + (lease_seconds || ' seconds')::interval))) 
                    AS expired_seconds
            FROM sync_jobs
            WHERE status = 'running'
              AND locked_at IS NOT NULL
              AND locked_at + ((lease_seconds + %s) || ' seconds')::interval < now()
            ORDER BY locked_at ASC
            LIMIT %s
            """,
            (grace_seconds, limit),
        )
        return [dict(row) for row in cur.fetchall()]


def list_expired_running_runs(
    conn: psycopg.Connection,
    max_duration_seconds: int = 1800,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """
    查询超时的 running 同步运行（持续时间过长）
    
    条件：status='running' 且 started_at + max_duration_seconds < now()
    
    Args:
        conn: 数据库连接
        max_duration_seconds: 最大允许运行时长（默认 30 分钟）
        limit: 最大返回数量
    
    Returns:
        超时运行列表，包含完整的 run 信息
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT 
                run_id, repo_id, job_type, mode, status,
                started_at, finished_at,
                cursor_before, cursor_after,
                counts, error_summary_json, degradation_json,
                logbook_item_id, meta_json, synced_count,
                EXTRACT(EPOCH FROM (now() - started_at)) AS running_seconds
            FROM sync_runs
            WHERE status = 'running'
              AND started_at + (%s || ' seconds')::interval < now()
            ORDER BY started_at ASC
            LIMIT %s
            """,
            (max_duration_seconds, limit),
        )
        return [dict(row) for row in cur.fetchall()]


def list_expired_locks(
    conn: psycopg.Connection,
    grace_seconds: int = 0,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """
    查询过期的锁
    
    条件：locked_by IS NOT NULL 且 locked_at + lease_seconds + grace_seconds < now()
    
    Args:
        conn: 数据库连接
        grace_seconds: 宽限期秒数
        limit: 最大返回数量
    
    Returns:
        过期锁列表
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT 
                lock_id, repo_id, job_type,
                locked_by, locked_at, lease_seconds,
                updated_at, created_at,
                EXTRACT(EPOCH FROM (now() - (locked_at + (lease_seconds || ' seconds')::interval))) 
                    AS expired_seconds
            FROM sync_locks
            WHERE locked_by IS NOT NULL
              AND locked_at + ((lease_seconds + %s) || ' seconds')::interval < now()
            ORDER BY locked_at ASC
            LIMIT %s
            """,
            (grace_seconds, limit),
        )
        return [dict(row) for row in cur.fetchall()]


def update_job_status(
    conn: psycopg.Connection,
    job_id: str,
    new_status: str,
    error_message: Optional[str] = None,
    reset_lock: bool = False,
    increment_attempts: bool = False,
    not_before_seconds: Optional[int] = None,
) -> bool:
    """
    更新任务状态（用于 reaper 状态转换）
    
    Args:
        conn: 数据库连接
        job_id: 任务 ID
        new_status: 新状态 ('pending' | 'failed' | 'dead')
        error_message: 错误信息（可选）
        reset_lock: 是否重置锁信息（locked_by, locked_at）
        increment_attempts: 是否增加尝试次数
        not_before_seconds: 延迟执行秒数（可选）
    
    Returns:
        True 如果更新成功
    """
    set_clauses = ["status = %s", "updated_at = now()"]
    params: List[Any] = [new_status]
    
    if error_message is not None:
        set_clauses.append("last_error = %s")
        params.append(error_message)
    
    if reset_lock:
        set_clauses.append("locked_by = NULL")
        set_clauses.append("locked_at = NULL")
    
    if increment_attempts:
        set_clauses.append("attempts = attempts + 1")
    
    if not_before_seconds is not None:
        set_clauses.append(f"not_before = now() + interval '{not_before_seconds} seconds'")
    
    params.append(job_id)
    
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE sync_jobs
            SET {', '.join(set_clauses)}
            WHERE job_id = %s
            RETURNING job_id
            """,
            params,
        )
        return cur.fetchone() is not None


def mark_job_as_failed_by_reaper(
    conn: psycopg.Connection,
    job_id: str,
    error_message: str,
    retry_delay_seconds: int = 60,
) -> bool:
    """
    将过期的 running 任务标记为 failed（可重试）
    
    重置锁信息，增加尝试次数，设置延迟执行时间。
    
    Args:
        conn: 数据库连接
        job_id: 任务 ID
        error_message: 错误信息
        retry_delay_seconds: 重试延迟秒数
    
    Returns:
        True 如果更新成功
    """
    return update_job_status(
        conn,
        job_id,
        new_status="failed",
        error_message=error_message,
        reset_lock=True,
        increment_attempts=True,
        not_before_seconds=retry_delay_seconds,
    )


def mark_job_as_pending_by_reaper(
    conn: psycopg.Connection,
    job_id: str,
    error_message: Optional[str] = None,
) -> bool:
    """
    将过期的 running 任务恢复为 pending（不增加尝试次数）
    
    仅重置锁信息，不增加尝试次数，适用于 worker 进程异常退出的场景。
    
    Args:
        conn: 数据库连接
        job_id: 任务 ID
        error_message: 错误信息（可选）
    
    Returns:
        True 如果更新成功
    """
    return update_job_status(
        conn,
        job_id,
        new_status="pending",
        error_message=error_message,
        reset_lock=True,
        increment_attempts=False,
    )


def mark_job_as_dead_by_reaper(
    conn: psycopg.Connection,
    job_id: str,
    error_message: str,
) -> bool:
    """
    将过期的任务标记为 dead（不再重试）
    
    Args:
        conn: 数据库连接
        job_id: 任务 ID
        error_message: 错误信息
    
    Returns:
        True 如果更新成功
    """
    return update_job_status(
        conn,
        job_id,
        new_status="dead",
        error_message=error_message,
        reset_lock=True,
        increment_attempts=False,
    )


def mark_run_as_failed_by_reaper(
    conn: psycopg.Connection,
    run_id: str,
    error_summary: Dict[str, Any],
) -> bool:
    """
    将超时的 running run 标记为 failed
    
    Args:
        conn: 数据库连接
        run_id: 运行 ID
        error_summary: 错误摘要 {error_type, message, ...}
    
    Returns:
        True 如果更新成功
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE sync_runs
            SET status = 'failed',
                finished_at = now(),
                error_summary_json = %s
            WHERE run_id = %s AND status = 'running'
            RETURNING run_id
            """,
            (json.dumps(error_summary), run_id),
        )
        return cur.fetchone() is not None


def force_release_lock(
    conn: psycopg.Connection,
    lock_id: int,
) -> bool:
    """
    强制释放锁
    
    Args:
        conn: 数据库连接
        lock_id: 锁 ID
    
    Returns:
        True 如果更新成功
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE sync_locks
            SET locked_by = NULL,
                locked_at = NULL,
                updated_at = now()
            WHERE lock_id = %s
            RETURNING lock_id
            """,
            (lock_id,),
        )
        return cur.fetchone() is not None


def force_release_locks_batch(
    conn: psycopg.Connection,
    lock_ids: List[int],
) -> int:
    """
    批量强制释放锁
    
    Args:
        conn: 数据库连接
        lock_ids: 锁 ID 列表
    
    Returns:
        释放的锁数量
    """
    if not lock_ids:
        return 0
    
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE sync_locks
            SET locked_by = NULL,
                locked_at = NULL,
                updated_at = now()
            WHERE lock_id = ANY(%s)
            """,
            (lock_ids,),
        )
        return cur.rowcount
