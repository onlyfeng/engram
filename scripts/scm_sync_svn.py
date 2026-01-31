#!/usr/bin/env python3
"""
scm_sync_svn - SVN 同步脚本薄包装器

[DEPRECATED] 此脚本已废弃，核心逻辑已迁移至包内模块。

新代码应使用:
    from engram.logbook.scm_sync_tasks import svn

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
    "scripts/scm_sync_svn.py 已废弃。"
    "请使用 'from engram.logbook.scm_sync_tasks import svn' 代替。"
    "此脚本将在未来版本中移除。",
    DeprecationWarning,
    stacklevel=2,
)

# 从包内模块重新导出所有 API
from engram.logbook.scm_sync_tasks.svn import (
    # 异常类
    SvnCommandError,
    SvnTimeoutError,
    SvnParseError,
    PatchFetchError,
    PatchFetchTimeoutError,
    PatchFetchContentTooLargeError,
    PatchFetchCommandError,
    # 数据类
    FetchDiffResult,
    SvnRevision,
    SyncConfig,
    # 解析函数
    parse_svn_log_xml,
    # SVN 命令函数
    get_svn_head_revision,
    fetch_svn_log_xml,
    run_svn_cmd,
    fetch_svn_diff,
    # 辅助函数
    generate_ministat_from_changed_paths,
    generate_diffstat,
    # 数据库操作
    ensure_repo,
    insert_svn_revisions,
    sync_patches_for_revisions,
    # 同步主函数
    sync_svn_revisions,
    backfill_svn_revisions,
)


def parse_args(argv: Optional[List[str]] = None):
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description="scm_sync_svn (deprecated wrapper)")
    parser.add_argument("--repo", dest="repo", default=None)
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--start-rev", type=int)
    parser.add_argument("--end-rev", type=int)
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--update-watermark", action="store_true")
    return parser.parse_args(argv)


if __name__ == "__main__":
    print("警告: scripts/scm_sync_svn.py 已废弃，请使用包内模块。", file=sys.stderr)
    args = parse_args()
    # 仅作为 wrapper，不执行实际逻辑
    sys.exit(0)
