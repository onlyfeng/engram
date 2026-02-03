# -*- coding: utf-8 -*-
"""
scm_repo - SCM 仓库辅助函数

本模块集中放置 SCM 同步相关的仓库工具函数，供测试与同步任务复用。
"""

from __future__ import annotations

from typing import Optional


def build_mr_id(repo_id: int | str, mr_iid: int | str) -> str:
    """构建 MR 唯一标识：<repo_id>:<mr_iid>。"""
    return f"{repo_id}:{mr_iid}"


def ensure_repo(
    conn,
    repo_type: str,
    url: str,
    *,
    project_key: Optional[str] = None,
    default_branch: Optional[str] = None,
) -> int:
    """确保仓库存在（不存在则创建），返回 repo_id。"""
    from .scm_db import upsert_repo

    return upsert_repo(
        conn,
        repo_type=repo_type,
        url=url,
        project_key=project_key or "default",
        default_branch=default_branch,
    )


__all__ = [
    "build_mr_id",
    "ensure_repo",
]
