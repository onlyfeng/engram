#!/usr/bin/env python3
"""
scm_sync_gitlab_mrs - GitLab MRs 同步脚本薄包装器

[DEPRECATED] 此脚本已废弃，核心逻辑已迁移至包内模块。

新代码应使用:
    from engram.logbook.scm_sync_tasks import gitlab_mrs

或通过 CLI:
    python -m engram.logbook.cli.scm_sync ...

此脚本保留用于向后兼容。
"""

from __future__ import annotations

import warnings

# 发出废弃警告
warnings.warn(
    "scripts/scm_sync_gitlab_mrs.py 已废弃。"
    "请使用 'from engram.logbook.scm_sync_tasks import gitlab_mrs' 代替。"
    "此脚本将在未来版本中移除。",
    DeprecationWarning,
    stacklevel=2,
)

# 从包内模块重新导出所有 API
from engram.logbook.scm_sync_tasks.gitlab_mrs import (
    # 数据类
    GitLabMergeRequest,
    # 解析函数
    parse_merge_request,
    # 辅助函数
    map_gitlab_state_to_status,
    build_mr_id,
    ensure_repo,
    # 数据库操作
    insert_merge_requests,
    # 同步主函数
    backfill_gitlab_mrs,
    sync_gitlab_mrs_incremental,
)
from engram.logbook.gitlab_client import GitLabClient


__all__ = [
    "GitLabClient",
    "GitLabMergeRequest",
    "parse_merge_request",
    "map_gitlab_state_to_status",
    "build_mr_id",
    "ensure_repo",
    "insert_merge_requests",
    "backfill_gitlab_mrs",
    "sync_gitlab_mrs_incremental",
]
