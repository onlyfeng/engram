# -*- coding: utf-8 -*-
"""
gitlab_mrs - GitLab MRs 同步核心实现

本模块提供 GitLab Merge Requests 同步的核心逻辑。

功能:
- GitLab MR 获取与解析
- MR 状态映射
- 数据库写入

设计原则:
- 纯业务逻辑，不依赖根目录模块
- 使用 engram.logbook.scm_db 进行数据库操作
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from engram.logbook.gitlab_client import GitLabClient
from engram.logbook.scm_db import (
    get_conn as get_connection,
    upsert_mr,
    upsert_repo,
)


# ============ 数据类定义 ============


@dataclass
class GitLabMergeRequest:
    """GitLab Merge Request 数据类"""
    iid: int
    project_id: int
    title: str
    description: str
    state: str
    author_id: Optional[int] = None
    author_username: str = ""
    source_branch: str = ""
    target_branch: str = ""
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    merged_at: Optional[datetime] = None
    merge_commit_sha: Optional[str] = None
    web_url: str = ""


# ============ 解析函数 ============


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    """解析 ISO 日期时间字符串"""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def parse_merge_request(data: Dict[str, Any]) -> GitLabMergeRequest:
    """解析 GitLab API 返回的 MR 数据"""
    author = data.get("author") or {}
    return GitLabMergeRequest(
        iid=int(data.get("iid") or 0),
        project_id=int(data.get("project_id") or 0),
        title=data.get("title", "") or "",
        description=data.get("description", "") or "",
        state=data.get("state", "") or "",
        author_id=author.get("id"),
        author_username=author.get("username", "") or "",
        source_branch=data.get("source_branch", "") or "",
        target_branch=data.get("target_branch", "") or "",
        created_at=_parse_dt(data.get("created_at")),
        updated_at=_parse_dt(data.get("updated_at")),
        merged_at=_parse_dt(data.get("merged_at")),
        merge_commit_sha=data.get("merge_commit_sha"),
        web_url=data.get("web_url", "") or "",
    )


def map_gitlab_state_to_status(state: str) -> str:
    """映射 GitLab MR 状态到内部状态"""
    if state == "opened":
        return "open"
    return state


# ============ 辅助函数 ============


def build_mr_id(repo_id: int | str, mr_iid: int | str) -> str:
    """构建 MR ID"""
    return f"{repo_id}:{mr_iid}"


def ensure_repo(
    conn,
    repo_type: str,
    url: str,
    *,
    project_key: Optional[str] = None,
    default_branch: Optional[str] = None,
) -> int:
    """
    确保仓库存在（使用包内 API）
    """
    return upsert_repo(
        conn,
        repo_type=repo_type,
        url=url,
        project_key=project_key or "default",
        default_branch=default_branch,
    )


# ============ 数据库操作 ============


def insert_merge_requests(
    conn,
    repo_id: int,
    mrs: List[GitLabMergeRequest],
) -> int:
    """插入 MRs 到数据库"""
    count = 0
    for mr in mrs:
        mr_id = build_mr_id(repo_id, mr.iid)
        status = map_gitlab_state_to_status(mr.state)
        meta_json = {
            "source_branch": mr.source_branch,
            "target_branch": mr.target_branch,
            "merge_commit_sha": mr.merge_commit_sha,
            "created_at": mr.created_at.isoformat() if mr.created_at else None,
            "updated_at": mr.updated_at.isoformat() if mr.updated_at else None,
            "merged_at": mr.merged_at.isoformat() if mr.merged_at else None,
        }
        upsert_mr(
            conn,
            mr_id=mr_id,
            repo_id=repo_id,
            status=status,
            url=mr.web_url,
            author_user_id=mr.author_username or str(mr.author_id) if mr.author_id else None,
            meta_json=meta_json,
            source_id=f"gitlab_mr:{repo_id}:{mr.iid}",
        )
        count += 1
    return count


# ============ 同步主函数 ============


def backfill_gitlab_mrs(
    *,
    gitlab_url: str,
    project_id: str,
    token: str,
    project_key: str,
    since: Optional[str] = None,
    until: Optional[str] = None,
    update_watermark: bool = False,
    dry_run: bool = False,
    dsn: Optional[str] = None,
    batch_size: int = 100,
) -> Dict[str, Any]:
    """
    回填 GitLab MRs

    Args:
        gitlab_url: GitLab 实例 URL
        project_id: 项目 ID
        token: API token
        project_key: 项目标识
        since: 开始时间（ISO 格式）
        until: 结束时间（ISO 格式）
        update_watermark: 是否更新游标
        dry_run: 是否模拟运行
        dsn: 数据库连接字符串（可选）
        batch_size: 每批获取数量

    Returns:
        同步结果字典
    """
    import os
    
    client = GitLabClient(gitlab_url, token=token)

    # 获取 MRs
    raw_mrs = client.get_merge_requests(
        project_id,
        state="all",
        updated_after=since,
        per_page=batch_size,
    )
    mrs = [parse_merge_request(item) for item in raw_mrs]

    result: Dict[str, Any] = {
        "success": True,
        "synced_count": 0,
        "scanned_count": len(mrs),
        "inserted_count": 0,
        "watermark_updated": False,
        "dry_run": dry_run,
    }

    if dry_run:
        return result

    # 获取或创建数据库连接
    dsn = dsn or os.environ.get("LOGBOOK_DSN") or os.environ.get("POSTGRES_DSN") or ""
    conn = get_connection(dsn)

    try:
        # 确保仓库存在
        repo_id = ensure_repo(
            conn,
            repo_type="gitlab",
            url=f"{gitlab_url}/{project_id}",
            project_key=project_key,
        )
        conn.commit()

        synced = insert_merge_requests(conn, repo_id, mrs)
        conn.commit()

        result["synced_count"] = synced
        result["inserted_count"] = synced

        return result
    finally:
        try:
            conn.close()
        except Exception:
            pass


def sync_gitlab_mrs_incremental(
    *,
    gitlab_url: str,
    project_id: str,
    token: str,
    project_key: str,
    dsn: Optional[str] = None,
) -> Dict[str, Any]:
    """
    增量同步 GitLab MRs

    Args:
        gitlab_url: GitLab 实例 URL
        project_id: 项目 ID
        token: API token
        project_key: 项目标识
        dsn: 数据库连接字符串（可选）

    Returns:
        同步结果字典
    """
    # 增量同步暂时返回基本实现
    return {
        "success": True,
        "synced_count": 0,
        "scanned_count": 0,
        "inserted_count": 0,
        "message": "gitlab_mrs incremental sync stub",
    }
