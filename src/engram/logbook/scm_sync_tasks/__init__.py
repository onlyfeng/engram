# -*- coding: utf-8 -*-
"""
scm_sync_tasks - SCM 同步任务核心实现

本包提供 SCM 同步任务的核心实现，供 SyncExecutor 和 worker 调用。

模块：
- gitlab_commits: GitLab commits 同步逻辑
- gitlab_mrs: GitLab MRs 同步逻辑
- svn: SVN revisions 同步逻辑

设计原则：
- 纯业务逻辑，不依赖根目录模块（如 scm_repo）
- 使用 engram.logbook.scm_db 进行数据库操作
- 提供统一的同步接口供 executor 调用
"""

from engram.logbook.scm_sync_tasks.gitlab_commits import (
    backfill_gitlab_commits,
    GitCommit,
    SyncConfig as GitLabCommitsSyncConfig,
    DiffMode,
    parse_commit,
    insert_git_commits,
    format_diff_content,
)
from engram.logbook.scm_sync_tasks.gitlab_mrs import (
    GitLabMergeRequest,
    parse_merge_request,
    map_gitlab_state_to_status,
)
from engram.logbook.scm_sync_tasks.svn import (
    SvnRevision,
    SyncConfig as SvnSyncConfig,
    backfill_svn_revisions,
    sync_svn_revisions,
    parse_svn_log_xml,
    insert_svn_revisions,
)

__all__ = [
    # GitLab commits
    "backfill_gitlab_commits",
    "GitCommit",
    "GitLabCommitsSyncConfig",
    "DiffMode",
    "parse_commit",
    "insert_git_commits",
    "format_diff_content",
    # GitLab MRs
    "GitLabMergeRequest",
    "parse_merge_request",
    "map_gitlab_state_to_status",
    # SVN
    "SvnRevision",
    "SvnSyncConfig",
    "backfill_svn_revisions",
    "sync_svn_revisions",
    "parse_svn_log_xml",
    "insert_svn_revisions",
]
