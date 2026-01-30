#!/usr/bin/env python3
"""
scm_sync_gitlab_mrs - GitLab MR 解析与客户端兼容层
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from engram.logbook.gitlab_client import GitLabClient as _GitLabClient


GitLabClient = _GitLabClient


@dataclass
class GitLabMergeRequest:
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


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def parse_merge_request(data: Dict[str, Any]) -> GitLabMergeRequest:
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
    if state == "opened":
        return "open"
    return state
