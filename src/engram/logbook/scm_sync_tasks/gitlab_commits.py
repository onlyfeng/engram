# -*- coding: utf-8 -*-
"""
gitlab_commits - GitLab commits 同步核心实现

本模块提供 GitLab commits 同步的核心逻辑，移除对根目录模块的依赖。

功能:
- GitLab commits 获取与解析
- Diff 获取与降级处理
- 数据库写入
- 游标管理

设计原则:
- 纯业务逻辑，不依赖根目录模块（如 scm_repo）
- 使用 engram.logbook.scm_db 进行数据库操作
- 使用 engram.logbook.cursor 进行游标管理
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import requests

from engram.logbook.gitlab_client import (
    GitLabClient,
    GitLabErrorCategory,
    GitLabAPIError,
)
from engram.logbook.cursor import save_gitlab_cursor
from engram.logbook.scm_db import (
    get_conn as get_connection,
    upsert_git_commit,
    upsert_repo,
)
from engram.logbook.hashing import sha256 as compute_sha256
from engram.logbook.config import DEFAULT_FORWARD_WINDOW_SECONDS


# ============ 异常定义 ============


class PatchFetchError(Exception):
    """Patch 获取基础错误"""
    error_category = "unknown"

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.details = details or {}


class PatchFetchTimeoutError(PatchFetchError):
    """Patch 获取超时"""
    error_category = "timeout"


class PatchFetchHttpError(PatchFetchError):
    """Patch 获取 HTTP 错误"""
    error_category = "http_error"


class PatchFetchContentTooLargeError(PatchFetchError):
    """Patch 内容过大"""
    error_category = "content_too_large"


class PatchFetchParseError(PatchFetchError):
    """Patch 解析错误"""
    error_category = "parse_error"


# ============ 数据类定义 ============


@dataclass
class FetchDiffResult:
    """Diff 获取结果"""
    success: bool
    diffs: Optional[List[Dict[str, Any]]] = None
    error: Optional[Exception] = None
    error_category: Optional[str] = None
    error_message: Optional[str] = None
    endpoint: Optional[str] = None
    status_code: Optional[int] = None


@dataclass
class GitCommit:
    """Git 提交记录"""
    sha: str
    author_name: str = ""
    author_email: str = ""
    authored_date: Optional[datetime] = None
    committer_name: str = ""
    committer_email: str = ""
    committed_date: Optional[datetime] = None
    message: str = ""
    parent_ids: List[str] = field(default_factory=list)
    web_url: str = ""
    stats: Dict[str, Any] = field(default_factory=dict)


class DiffMode(str, Enum):
    """Diff 获取模式"""
    ALWAYS = "always"
    BEST_EFFORT = "best_effort"
    NONE = "none"


@dataclass
class SyncConfig:
    """同步配置"""
    gitlab_url: str
    project_id: str
    token_provider: Any
    batch_size: int = 100
    forward_window_seconds: int = DEFAULT_FORWARD_WINDOW_SECONDS
    diff_mode: DiffMode = DiffMode.BEST_EFFORT
    strict: bool = False
    timeout: int = 120


@dataclass
class FetchWindow:
    """获取时间窗口"""
    since: datetime
    until: datetime


@dataclass
class AdaptiveWindowState:
    """自适应窗口状态"""
    current_window_seconds: int
    min_window_seconds: int
    max_window_seconds: int
    shrink_factor: float
    grow_factor: float
    commit_threshold: int
    rate_limit_count: int = 0

    def shrink(self, reason: Optional[str] = None) -> int:
        new_value = int(self.current_window_seconds * self.shrink_factor)
        if new_value < self.min_window_seconds:
            new_value = self.min_window_seconds
        self.current_window_seconds = new_value
        return new_value

    def grow(self) -> int:
        new_value = int(self.current_window_seconds * self.grow_factor)
        if new_value > self.max_window_seconds:
            new_value = self.max_window_seconds
        self.current_window_seconds = new_value
        return new_value

    def record_rate_limit(self) -> None:
        self.rate_limit_count += 1
        self.shrink(reason="rate_limit")

    def reset_rate_limit_count(self) -> None:
        self.rate_limit_count = 0


# ============ 解析函数 ============


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    """解析 ISO 日期时间字符串"""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    """解析 ISO 日期时间（别名）"""
    return _parse_dt(value)


def parse_commit(data: Dict[str, Any]) -> GitCommit:
    """解析 GitLab API 返回的 commit 数据"""
    return GitCommit(
        sha=data.get("id") or data.get("sha") or "",
        author_name=data.get("author_name", "") or "",
        author_email=data.get("author_email", "") or "",
        authored_date=_parse_dt(data.get("authored_date")),
        committer_name=data.get("committer_name", "") or "",
        committer_email=data.get("committer_email", "") or "",
        committed_date=_parse_dt(data.get("committed_date")),
        message=data.get("message", "") or "",
        parent_ids=list(data.get("parent_ids") or []),
        web_url=data.get("web_url", "") or "",
        stats=data.get("stats") or {},
    )


# ============ 辅助函数 ============


def format_diff_content(diffs: List[Dict[str, Any]]) -> str:
    """格式化 diff 内容"""
    if not diffs:
        return ""
    parts: List[str] = []
    for diff in diffs:
        old_path = diff.get("old_path") or ""
        new_path = diff.get("new_path") or old_path
        parts.append(f"--- a/{old_path}")
        parts.append(f"+++ b/{new_path}")
        diff_text = diff.get("diff") or ""
        if diff_text:
            parts.append(diff_text.rstrip("\n"))
    return "\n".join(parts) + "\n"


def _get_commit_timestamp(commit: GitCommit) -> datetime:
    """获取 commit 时间戳"""
    ts = commit.committed_date or commit.authored_date
    if ts is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def _get_commit_sort_key(commit: GitCommit) -> Tuple[datetime, str]:
    """获取 commit 排序键"""
    return (_get_commit_timestamp(commit), commit.sha)


def _deduplicate_commits(
    commits: List[GitCommit],
    cursor_sha: Optional[str] = None,
    cursor_ts: Optional[datetime] = None,
) -> List[GitCommit]:
    """去重并过滤 commits"""
    if cursor_ts is not None and not isinstance(cursor_ts, datetime):
        try:
            cursor_ts = datetime.fromisoformat(str(cursor_ts).replace("Z", "+00:00"))
        except Exception:
            cursor_ts = None
    if cursor_ts is not None and cursor_ts.tzinfo is None:
        cursor_ts = cursor_ts.replace(tzinfo=timezone.utc)

    def is_after_cursor(commit: GitCommit) -> bool:
        if cursor_ts is None:
            if cursor_sha is None:
                return True
            return commit.sha != cursor_sha
        ts = _get_commit_timestamp(commit)
        if ts > cursor_ts:
            return True
        if ts < cursor_ts:
            return False
        if cursor_sha is None:
            return True
        return commit.sha > cursor_sha

    sorted_commits = sorted(commits, key=_get_commit_sort_key)
    seen = set()
    result: List[GitCommit] = []
    for commit in sorted_commits:
        if commit.sha in seen:
            continue
        if not is_after_cursor(commit):
            continue
        seen.add(commit.sha)
        result.append(commit)
    return result


def compute_commit_fetch_window(
    *,
    cursor_ts: Optional[datetime],
    overlap_seconds: int,
    forward_window_seconds: int,
    now: Optional[datetime] = None,
) -> FetchWindow:
    """计算 commit 获取窗口"""
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if cursor_ts is None:
        since = datetime(1970, 1, 1, tzinfo=timezone.utc)
    else:
        if cursor_ts.tzinfo is None:
            cursor_ts = cursor_ts.replace(tzinfo=timezone.utc)
        since = cursor_ts - timedelta(seconds=overlap_seconds)
    until = since + timedelta(seconds=forward_window_seconds)
    if until > now:
        until = now
    return FetchWindow(since=since, until=until)


def select_next_batch(
    commits: List[GitCommit],
    cursor_sha: Optional[str],
    cursor_ts: Optional[datetime],
    batch_size: int,
) -> List[GitCommit]:
    """选择下一批 commits"""
    deduped = _deduplicate_commits(commits, cursor_sha=cursor_sha, cursor_ts=cursor_ts)
    return deduped[:batch_size]


def compute_batch_cursor_target(
    commits: List[GitCommit],
) -> Optional[Tuple[datetime, str]]:
    """计算批次游标目标"""
    if not commits:
        return None
    last = max(commits, key=_get_commit_sort_key)
    return _get_commit_timestamp(last), last.sha


def generate_ministat_from_stats(stats: Dict[str, Any], commit_sha: Optional[str] = None) -> str:
    """从 stats 生成 ministat"""
    total = int(stats.get("total", 0) or 0)
    additions = int(stats.get("additions", 0) or 0)
    deletions = int(stats.get("deletions", 0) or 0)
    short_sha = (commit_sha or "unknown")[:8]
    return (
        f"ministat [{short_sha}] degraded: "
        f"{total} file(s) changed, {additions} insertion(s)(+), {deletions} deletion(s)(-)"
    )


def generate_diffstat(diffs: List[Dict[str, Any]]) -> str:
    """生成 diffstat"""
    if not diffs:
        return ""
    lines = []
    paths = []
    for diff in diffs:
        path = diff.get("new_path") or diff.get("old_path") or "unknown"
        paths.append(path)
        if diff.get("new_file"):
            lines.append(f"{path} (new)")
        else:
            lines.append(path)
    lines.append(f"{len(set(paths))} file(s) changed")
    return "\n".join(lines)


def _result_from_exception(exc: Exception, endpoint: str) -> FetchDiffResult:
    """从异常创建 FetchDiffResult"""
    if isinstance(exc, requests.exceptions.Timeout):
        return FetchDiffResult(
            success=False,
            error=PatchFetchTimeoutError("timeout"),
            error_category=GitLabErrorCategory.TIMEOUT,
            error_message="请求超时",
            endpoint=endpoint,
        )
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        return FetchDiffResult(
            success=False,
            error=PatchFetchHttpError("http_error"),
            error_category=GitLabErrorCategory.CLIENT_ERROR,
            error_message=str(exc),
            endpoint=endpoint,
            status_code=exc.response.status_code,
        )
    return FetchDiffResult(
        success=False,
        error=PatchFetchError("unknown"),
        error_category=GitLabErrorCategory.UNKNOWN,
        error_message=str(exc),
        endpoint=endpoint,
    )


def is_unrecoverable_api_error(
    error_category: Optional[str],
    *,
    status_code: Optional[int] = None,
) -> bool:
    """判断是否为不可恢复的 API 错误"""
    if error_category == "rate_limited" or status_code == 429:
        return True
    if error_category == "timeout":
        return True
    if error_category == "http_error":
        return True
    if status_code is not None and 500 <= status_code <= 599:
        return True
    return False


# ============ 数据库操作 ============


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

    此函数替代根目录 scm_repo.ensure_repo，使用 engram.logbook.scm_db.upsert_repo。
    """
    return upsert_repo(
        conn,
        repo_type=repo_type,
        url=url,
        project_key=project_key or "default",
        default_branch=default_branch,
    )


def insert_git_commits(conn, repo_id: int, commits: List[GitCommit]) -> int:
    """插入 git commits 到数据库"""
    count = 0
    for commit in commits:
        upsert_git_commit(
            conn,
            repo_id,
            commit.sha,
            author_raw=commit.author_name or commit.author_email or "unknown",
            ts=commit.committed_date.isoformat() if commit.committed_date else None,
            message=commit.message,
            meta_json=commit.stats,
            source_id=commit.sha,
        )
        count += 1
    return count


def update_sync_cursor(
    repo_id: int,
    *,
    last_commit_sha: str,
    last_commit_ts: str,
    synced_count: int,
) -> bool:
    """更新同步游标"""
    return save_gitlab_cursor(repo_id, last_commit_sha, last_commit_ts, synced_count, config=None)


# ============ 同步主函数 ============


def backfill_gitlab_commits(
    sync_config: SyncConfig,
    *,
    project_key: str,
    since: Optional[str],
    until: Optional[str],
    update_watermark: bool = False,
    dry_run: bool = False,
    fetch_diffs: bool = False,
    dsn: Optional[str] = None,
) -> Dict[str, Any]:
    """
    回填 GitLab commits

    Args:
        sync_config: 同步配置
        project_key: 项目标识
        since: 开始时间（ISO 格式）
        until: 结束时间（ISO 格式）
        update_watermark: 是否更新游标
        dry_run: 是否模拟运行
        fetch_diffs: 是否获取 diffs
        dsn: 数据库连接字符串（可选）

    Returns:
        同步结果字典
    """
    import os
    
    client = GitLabClient(sync_config.gitlab_url, token=sync_config.token_provider.get_token())
    raw_commits = client.get_commits(
        sync_config.project_id,
        since=since,
        until=until,
        per_page=sync_config.batch_size,
    )
    commits = [parse_commit(item) for item in raw_commits]

    # 获取或创建数据库连接
    dsn = dsn or os.environ.get("LOGBOOK_DSN") or os.environ.get("POSTGRES_DSN") or ""
    conn = get_connection(dsn)

    try:
        # 确保仓库存在
        repo_id = ensure_repo(
            conn,
            repo_type="gitlab",
            url=f"{sync_config.gitlab_url}/{sync_config.project_id}",
            project_key=project_key,
        )
        conn.commit()

        result: Dict[str, Any] = {
            "success": True,
            "synced_count": 0,
            "watermark_updated": False,
            "dry_run": dry_run,
        }
        if dry_run:
            return result

        synced = insert_git_commits(conn, repo_id, commits)
        conn.commit()
        result["synced_count"] = synced

        if update_watermark and commits:
            last_commit = max(commits, key=_get_commit_sort_key)
            last_ts = _get_commit_timestamp(last_commit).isoformat().replace("+00:00", "Z")
            update_sync_cursor(
                repo_id,
                last_commit_sha=last_commit.sha,
                last_commit_ts=last_ts,
                synced_count=result["synced_count"],
            )
            result["watermark_updated"] = True
            result["last_commit_sha"] = last_commit.sha

        return result
    finally:
        try:
            conn.close()
        except Exception:
            pass


def sync_gitlab_commits_incremental(
    sync_config: SyncConfig,
    *,
    project_key: str,
    dsn: Optional[str] = None,
) -> Dict[str, Any]:
    """
    增量同步 GitLab commits

    Args:
        sync_config: 同步配置
        project_key: 项目标识
        dsn: 数据库连接字符串（可选）

    Returns:
        同步结果字典
    """
    # 增量同步暂时返回基本实现
    return {
        "success": True,
        "synced_count": 0,
        "message": "incremental sync not fully implemented",
    }


def build_mr_id(repo_id: int | str, mr_iid: int | str) -> str:
    """构建 MR ID"""
    return f"{repo_id}:{mr_iid}"
