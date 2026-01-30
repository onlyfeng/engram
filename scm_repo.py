#!/usr/bin/env python3
"""
scm_repo - SCM 仓库辅助函数（兼容测试）
"""

from __future__ import annotations

from typing import Optional


def build_mr_id(repo_id: int | str, mr_iid: int | str) -> str:
    return f"{repo_id}:{mr_iid}"


def ensure_repo(
    conn,
    repo_type: str,
    url: str,
    *,
    project_key: Optional[str] = None,
    default_branch: Optional[str] = None,
) -> int:
    from db import upsert_repo

    return upsert_repo(
        conn,
        repo_type=repo_type,
        url=url,
        project_key=project_key or "default",
        default_branch=default_branch,
    )
