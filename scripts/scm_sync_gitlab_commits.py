#!/usr/bin/env python3
"""
scm_sync_gitlab_commits - GitLab commits 同步脚本薄包装器

[DEPRECATED] 此脚本已废弃，核心逻辑已迁移至包内模块。

新代码应使用:
    from engram.logbook.scm_sync_tasks import gitlab_commits

或通过 CLI:
    python -m engram.logbook.cli.scm_sync ...

此脚本保留用于向后兼容。
"""

from __future__ import annotations

import argparse
import sys
import warnings
from typing import List, Optional

# 发出废弃警告
warnings.warn(
    "scripts/scm_sync_gitlab_commits.py 已废弃。"
    "请使用 'from engram.logbook.scm_sync_tasks import gitlab_commits' 代替。"
    "此脚本将在未来版本中移除。",
    DeprecationWarning,
    stacklevel=2,
)

# 从包内模块重新导出所有 API
from engram.logbook.scm_sync_tasks.gitlab_commits import (
    # 异常类
    PatchFetchError,
    PatchFetchTimeoutError,
    PatchFetchHttpError,
    PatchFetchContentTooLargeError,
    PatchFetchParseError,
    # 数据类
    FetchDiffResult,
    GitCommit,
    DiffMode,
    SyncConfig,
    FetchWindow,
    AdaptiveWindowState,
    # 解析函数
    parse_commit,
    # 辅助函数
    format_diff_content,
    generate_ministat_from_stats,
    generate_diffstat,
    compute_commit_fetch_window,
    select_next_batch,
    compute_batch_cursor_target,
    is_unrecoverable_api_error,
    # 数据库操作
    ensure_repo,
    insert_git_commits,
    update_sync_cursor,
    # 同步主函数
    backfill_gitlab_commits,
    sync_gitlab_commits_incremental,
    build_mr_id,
    # 内部辅助函数（测试兼容）
    _deduplicate_commits,
    _get_commit_sort_key,
    _get_commit_timestamp,
    _parse_iso_datetime,
)
from engram.logbook.gitlab_client import GitLabClient


def parse_args(argv: Optional[List[str]] = None):
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description="scm_sync_gitlab_commits (deprecated wrapper)")
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--update-watermark", action="store_true")
    return parser.parse_args(argv)


if __name__ == "__main__":
    print("警告: scripts/scm_sync_gitlab_commits.py 已废弃，请使用包内模块。", file=sys.stderr)
    args = parse_args()
    # 仅作为 wrapper，不执行实际逻辑
    sys.exit(0)
