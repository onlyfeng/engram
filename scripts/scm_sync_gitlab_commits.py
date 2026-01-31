#!/usr/bin/env python3
"""
scm_sync_gitlab_commits - 兼容 GitLab diff 获取与降级工具

注意: 此文件已移动到 scripts/ 目录。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

# 确保根目录在 sys.path 中，以支持导入根目录模块
_ROOT_DIR = Path(__file__).resolve().parent.parent
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

import requests

from engram.logbook.gitlab_client import (
    GitLabClient as _GitLabClient,
    GitLabErrorCategory,
    GitLabAPIError,
)
from engram.logbook.cursor import save_gitlab_cursor
from db import get_conn as get_connection
from db import upsert_git_commit
import scm_repo
from engram.logbook.hashing import sha256 as compute_sha256
from engram.logbook.config import DEFAULT_FORWARD_WINDOW_SECONDS


@dataclass
class FetchDiffResult:
    success: bool
    diffs: Optional[List[Dict[str, Any]]] = None
    error: Optional[Exception] = None
    error_category: Optional[str] = None
    error_message: Optional[str] = None
    endpoint: Optional[str] = None
    status_code: Optional[int] = None


class PatchFetchError(Exception):
    error_category = "unknown"

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.details = details or {}


class PatchFetchTimeoutError(PatchFetchError):
    error_category = "timeout"


class PatchFetchHttpError(PatchFetchError):
    error_category = "http_error"


class PatchFetchContentTooLargeError(PatchFetchError):
    error_category = "content_too_large"


class PatchFetchParseError(PatchFetchError):
    error_category = "parse_error"


GitLabClient = _GitLabClient


@dataclass
class GitCommit:
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


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    return _parse_dt(value)


class DiffMode(str, Enum):
    ALWAYS = "always"
    BEST_EFFORT = "best_effort"
    NONE = "none"


@dataclass
class SyncConfig:
    gitlab_url: str
    project_id: str
    token_provider: Any
    batch_size: int = 100
    forward_window_seconds: int = DEFAULT_FORWARD_WINDOW_SECONDS
    diff_mode: DiffMode = DiffMode.BEST_EFFORT
    strict: bool = False
    timeout: int = 120


def parse_commit(data: Dict[str, Any]) -> GitCommit:
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


def format_diff_content(diffs: List[Dict[str, Any]]) -> str:
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
    ts = commit.committed_date or commit.authored_date
    if ts is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def _get_commit_sort_key(commit: GitCommit) -> Tuple[datetime, str]:
    return (_get_commit_timestamp(commit), commit.sha)


def _deduplicate_commits(
    commits: List[GitCommit],
    cursor_sha: Optional[str] = None,
    cursor_ts: Optional[datetime] = None,
) -> List[GitCommit]:
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


@dataclass
class FetchWindow:
    since: datetime
    until: datetime


@dataclass
class AdaptiveWindowState:
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


def compute_commit_fetch_window(
    *,
    cursor_ts: Optional[datetime],
    overlap_seconds: int,
    forward_window_seconds: int,
    now: Optional[datetime] = None,
) -> FetchWindow:
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
    deduped = _deduplicate_commits(commits, cursor_sha=cursor_sha, cursor_ts=cursor_ts)
    return deduped[:batch_size]


def compute_batch_cursor_target(
    commits: List[GitCommit],
) -> Optional[Tuple[datetime, str]]:
    if not commits:
        return None
    last = max(commits, key=_get_commit_sort_key)
    return _get_commit_timestamp(last), last.sha


def generate_ministat_from_stats(stats: Dict[str, Any], commit_sha: Optional[str] = None) -> str:
    total = int(stats.get("total", 0) or 0)
    additions = int(stats.get("additions", 0) or 0)
    deletions = int(stats.get("deletions", 0) or 0)
    short_sha = (commit_sha or "unknown")[:8]
    return (
        f"ministat [{short_sha}] degraded: "
        f"{total} file(s) changed, {additions} insertion(s)(+), {deletions} deletion(s)(-)"
    )


def generate_diffstat(diffs: List[Dict[str, Any]]) -> str:
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


def write_text_artifact(*_args, **_kwargs) -> dict:
    raise NotImplementedError()


scm_db = SimpleNamespace(upsert_patch_blob=lambda *args, **kwargs: 0)


def insert_patch_blob(
    *,
    conn,
    repo_id: int,
    commit_sha: str,
    content: str,
    patch_format: str,
    is_degraded: bool,
    degrade_reason: Optional[str] = None,
    source_fetch_error: Optional[str] = None,
    original_endpoint: Optional[str] = None,
) -> int:
    write_result = write_text_artifact(
        repo_id=repo_id,
        commit_sha=commit_sha,
        content=content,
        patch_format=patch_format,
    )

    meta_json: Dict[str, Any] = {"materialize_status": "done"}
    if is_degraded:
        meta_json.update(
            {
                "degraded": True,
                "degrade_reason": degrade_reason,
                "source_fetch_error": source_fetch_error,
                "original_endpoint": original_endpoint,
            }
        )

    blob_id = scm_db.upsert_patch_blob(
        conn=conn,
        repo_id=repo_id,
        commit_sha=commit_sha,
        uri=write_result.get("uri"),
        sha256=write_result.get("sha256") or compute_sha256(content.encode("utf-8")),
        size_bytes=write_result.get("size_bytes") or len(content.encode("utf-8")),
        format=patch_format,
        meta_json=meta_json,
    )
    return blob_id


def _result_from_exception(exc: Exception, endpoint: str) -> FetchDiffResult:
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
    if error_category == "rate_limited" or status_code == 429:
        return True
    if error_category == "timeout":
        return True
    if error_category == "http_error":
        return True
    if status_code is not None and 500 <= status_code <= 599:
        return True
    return False


def insert_git_commits(conn, repo_id: int, commits: List[GitCommit]) -> int:
    count = 0
    for commit in commits:
        upsert_git_commit(
            conn,
            repo_id,
            commit.sha,
            author_name=commit.author_name,
            author_email=commit.author_email,
            commit_message=commit.message,
            committed_at=commit.committed_date.isoformat() if commit.committed_date else None,
            stats_json=commit.stats,
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
    return save_gitlab_cursor(repo_id, last_commit_sha, last_commit_ts, synced_count, config=None)


def backfill_gitlab_commits(
    sync_config: SyncConfig,
    *,
    project_key: str,
    since: Optional[str],
    until: Optional[str],
    update_watermark: bool = False,
    dry_run: bool = False,
    fetch_diffs: bool = False,
) -> Dict[str, Any]:
    client = GitLabClient(sync_config.gitlab_url, token=sync_config.token_provider.get_token())
    raw_commits = client.get_commits(
        sync_config.project_id,
        since=since,
        until=until,
        per_page=sync_config.batch_size,
    )
    commits = [parse_commit(item) for item in raw_commits]
    repo_id = scm_repo.ensure_repo(
        None,
        repo_type="gitlab",
        url=f"{sync_config.gitlab_url}/{sync_config.project_id}",
        project_key=project_key,
    )

    result: Dict[str, Any] = {
        "success": True,
        "synced_count": 0,
        "watermark_updated": False,
        "dry_run": dry_run,
    }
    if dry_run:
        return result

    conn = get_connection("")
    try:
        synced = insert_git_commits(conn, repo_id, commits)
        conn.commit()
        result["synced_count"] = synced
    finally:
        try:
            conn.close()
        except Exception:
            pass

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


def parse_args(argv: Optional[List[str]] = None):
    parser = argparse.ArgumentParser(description="scm_sync_gitlab_commits")
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--update-watermark", action="store_true")
    return parser.parse_args(argv)
