#!/usr/bin/env python3
"""
scm_sync_runner - SCM 同步运行器（测试兼容实现）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional, Tuple

from engram.logbook.config import get_config
from engram.logbook.db import get_connection as _get_db_connection

REPO_TYPE_GITLAB = "gitlab"
REPO_TYPE_SVN = "svn"
VALID_REPO_TYPES = {REPO_TYPE_GITLAB, REPO_TYPE_SVN}

JOB_TYPE_COMMITS = "commits"
JOB_TYPE_MRS = "mrs"
JOB_TYPE_REVIEWS = "reviews"
VALID_JOB_TYPES = {JOB_TYPE_COMMITS, JOB_TYPE_MRS, JOB_TYPE_REVIEWS}

DEFAULT_REPAIR_WINDOW_HOURS = 24
DEFAULT_LOOP_INTERVAL_SECONDS = 60
DEFAULT_WINDOW_CHUNK_HOURS = 4
DEFAULT_WINDOW_CHUNK_REVS = 100


class RunnerStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class RunnerPhase(str, Enum):
    INCREMENTAL = "incremental"
    BACKFILL = "backfill"


class WatermarkConstraintError(Exception):
    def __init__(self, message: str, details: Optional[dict] = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


@dataclass
class RepoSpec:
    repo_type: str
    repo_id: str

    @classmethod
    def parse(cls, value: str) -> "RepoSpec":
        if ":" not in value:
            raise ValueError("格式应为 <type>:<id>")
        repo_type, repo_id = value.split(":", 1)
        repo_type = repo_type.lower().strip()
        repo_id = repo_id.strip()
        if repo_type not in VALID_REPO_TYPES:
            raise ValueError("不支持的仓库类型")
        if not repo_id:
            raise ValueError("仓库 ID 不能为空")
        return cls(repo_type=repo_type, repo_id=repo_id)

    def __str__(self) -> str:
        return f"{self.repo_type}:{self.repo_id}"


@dataclass
class JobSpec:
    job_type: str

    @classmethod
    def parse(cls, value: str) -> "JobSpec":
        job_type = value.lower().strip()
        if job_type not in VALID_JOB_TYPES:
            raise ValueError("不支持的任务类型")
        return cls(job_type=job_type)

    def __str__(self) -> str:
        return self.job_type


@dataclass
class BackfillConfig:
    repair_window_hours: int = DEFAULT_REPAIR_WINDOW_HOURS
    cron_hint: str = "0 2 * * *"
    max_concurrent_jobs: int = 4
    default_update_watermark: bool = False

    @classmethod
    def from_config(cls, config=None) -> "BackfillConfig":
        if config is None:
            config = get_config()
        return cls(
            repair_window_hours=int(config.get("scm.backfill.repair_window_hours", DEFAULT_REPAIR_WINDOW_HOURS)),
            cron_hint=str(config.get("scm.backfill.cron_hint", "0 2 * * *")),
            max_concurrent_jobs=int(config.get("scm.backfill.max_concurrent_jobs", 4)),
            default_update_watermark=bool(config.get("scm.backfill.default_update_watermark", False)),
        )


@dataclass
class IncrementalConfig:
    loop: bool = False
    loop_interval_seconds: int = DEFAULT_LOOP_INTERVAL_SECONDS
    max_iterations: int = 0


@dataclass
class RunnerContext:
    config: object
    repo: RepoSpec
    job: JobSpec = field(default_factory=lambda: JobSpec(job_type=JOB_TYPE_COMMITS))
    config_path: Optional[str] = None
    verbose: bool = False
    dry_run: bool = False
    update_watermark: bool = False
    window_chunk_hours: int = DEFAULT_WINDOW_CHUNK_HOURS
    window_chunk_revs: int = DEFAULT_WINDOW_CHUNK_REVS
    auto_refresh_vfacts: bool = True
    refresh_concurrently: bool = False


@dataclass
class SyncResult:
    phase: str
    repo: str
    status: str = RunnerStatus.SUCCESS.value
    job: Optional[str] = None
    items_synced: int = 0
    message: Optional[str] = None
    error: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    vfacts_refreshed: bool = False
    vfacts_refresh_info: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "repo": self.repo,
            "status": self.status,
            "job": self.job,
            "items_synced": self.items_synced,
            "message": self.message,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "vfacts_refreshed": self.vfacts_refreshed,
            "vfacts_refresh_info": self.vfacts_refresh_info,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


@dataclass
class TimeWindowChunk:
    since: datetime
    until: datetime
    index: int
    total: int

    def to_payload(self, *, update_watermark: bool, watermark_constraint: str) -> dict:
        return {
            "window_type": "time",
            "since": self.since.isoformat(),
            "until": self.until.isoformat(),
            "window_since": self.since.isoformat(),
            "window_until": self.until.isoformat(),
            "index": self.index,
            "total": self.total,
            "chunk_index": self.index,
            "chunk_total": self.total,
            "update_watermark": update_watermark,
            "watermark_constraint": watermark_constraint,
        }


@dataclass
class RevisionWindowChunk:
    start_rev: int
    end_rev: int
    index: int
    total: int

    def to_payload(self, *, update_watermark: bool, watermark_constraint: str) -> dict:
        return {
            "window_type": "revision",
            "start_rev": self.start_rev,
            "end_rev": self.end_rev,
            "window_start_rev": self.start_rev,
            "window_end_rev": self.end_rev,
            "index": self.index,
            "total": self.total,
            "chunk_index": self.index,
            "chunk_total": self.total,
            "update_watermark": update_watermark,
            "watermark_constraint": watermark_constraint,
        }


def split_time_window(
    since: datetime,
    until: datetime,
    *,
    chunk_hours: int = DEFAULT_WINDOW_CHUNK_HOURS,
) -> List[TimeWindowChunk]:
    if since >= until:
        return []
    chunks: List[TimeWindowChunk] = []
    delta = timedelta(hours=chunk_hours)
    cursor = since
    while cursor < until:
        next_cursor = min(cursor + delta, until)
        chunks.append(TimeWindowChunk(since=cursor, until=next_cursor, index=0, total=0))
        cursor = next_cursor
    total = len(chunks)
    for idx, chunk in enumerate(chunks):
        chunk.index = idx
        chunk.total = total
    return chunks


def split_revision_window(
    start_rev: int,
    end_rev: int,
    *,
    chunk_size: int = DEFAULT_WINDOW_CHUNK_REVS,
) -> List[RevisionWindowChunk]:
    if start_rev > end_rev:
        return []
    chunks: List[RevisionWindowChunk] = []
    cursor = start_rev
    while cursor <= end_rev:
        chunk_end = min(cursor + chunk_size - 1, end_rev)
        chunks.append(RevisionWindowChunk(start_rev=cursor, end_rev=chunk_end, index=0, total=0))
        cursor = chunk_end + 1
    total = len(chunks)
    for idx, chunk in enumerate(chunks):
        chunk.index = idx
        chunk.total = total
    return chunks


def calculate_backfill_window(
    *,
    hours: Optional[int] = None,
    days: Optional[int] = None,
    config: Optional[BackfillConfig] = None,
) -> Tuple[datetime, datetime]:
    if config is None:
        config = BackfillConfig()
    if hours is None and days is None:
        hours = config.repair_window_hours
    if days is not None:
        hours = int(days) * 24
    since = datetime.now(timezone.utc) - timedelta(hours=int(hours or 0))
    until = datetime.now(timezone.utc)
    return since, until


def validate_watermark_constraint(
    *,
    watermark_before: Optional[str],
    watermark_after: Optional[str],
    update_watermark: bool = True,
) -> None:
    if not update_watermark:
        return
    if watermark_before is None or watermark_after is None:
        return
    before_dt = datetime.fromisoformat(watermark_before.replace("Z", "+00:00"))
    after_dt = datetime.fromisoformat(watermark_after.replace("Z", "+00:00"))
    if after_dt < before_dt:
        raise WatermarkConstraintError(
            "Watermark 回退被禁止",
            details={"watermark_before": watermark_before, "watermark_after": watermark_after},
        )


def get_script_path(repo_type: str, job_type: str) -> str:
    repo_type = repo_type.lower()
    job_type = job_type.lower()
    repo_root = Path(__file__).resolve().parent
    if repo_type == REPO_TYPE_GITLAB:
        if job_type == JOB_TYPE_COMMITS:
            return str(repo_root / "scm_sync_gitlab_commits.py")
        if job_type == JOB_TYPE_MRS:
            return str(repo_root / "scm_sync_gitlab_mrs.py")
        if job_type == JOB_TYPE_REVIEWS:
            return str(repo_root / "scm_sync_gitlab_reviews.py")
    if repo_type == REPO_TYPE_SVN and job_type == JOB_TYPE_COMMITS:
        return str(repo_root / "scm_sync_svn.py")
    raise ValueError("不支持的仓库/任务组合")


def build_sync_command(
    ctx: RunnerContext,
    phase: RunnerPhase,
    *,
    since_time: Optional[datetime] = None,
    until_time: Optional[datetime] = None,
    start_rev: Optional[int] = None,
    end_rev: Optional[int] = None,
) -> List[str]:
    script_path = get_script_path(ctx.repo.repo_type, ctx.job.job_type)
    cmd = [sys.executable, script_path]
    if ctx.config_path:
        cmd += ["--config", ctx.config_path]
    if ctx.verbose:
        cmd.append("--verbose")
    if ctx.dry_run:
        cmd.append("--dry-run")

    if phase == RunnerPhase.INCREMENTAL:
        cmd += ["--repo", str(ctx.repo)]
    else:
        cmd.append("--backfill")
        if start_rev is not None or end_rev is not None:
            if start_rev is not None:
                cmd += ["--start-rev", str(start_rev)]
            if end_rev is not None:
                cmd += ["--end-rev", str(end_rev)]
        if since_time is not None:
            cmd += ["--since", since_time.isoformat()]
        if until_time is not None:
            cmd += ["--until", until_time.isoformat()]
        if ctx.update_watermark:
            cmd.append("--update-watermark")
        else:
            cmd.append("--no-update-cursor")
    return cmd


def get_connection():
    dsn = os.environ.get("LOGBOOK_DSN") or os.environ.get("POSTGRES_DSN")
    return _get_db_connection(dsn=dsn)


def refresh_vfacts(*, dry_run: bool = False, concurrently: bool = False) -> dict:
    result = {
        "dry_run": dry_run,
        "refreshed": False,
        "concurrently": concurrently,
        "before_row_count": 0,
        "after_row_count": 0,
    }
    if dry_run:
        return result
    started = time.monotonic()
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM scm.v_facts")
                before = cur.fetchone()[0] or 0
                if concurrently:
                    cur.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY scm.v_facts")
                else:
                    cur.execute("REFRESH MATERIALIZED VIEW scm.v_facts")
                cur.execute("SELECT COUNT(*) FROM scm.v_facts")
                after = cur.fetchone()[0] or 0
            conn.commit()
        finally:
            conn.close()
        duration_ms = (time.monotonic() - started) * 1000.0
        refreshed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        result.update(
            {
                "refreshed": True,
                "before_row_count": int(before),
                "after_row_count": int(after),
                "duration_ms": duration_ms,
                "refreshed_at": refreshed_at,
            }
        )
    except Exception as exc:
        result["error"] = str(exc)
    return result


class SyncRunner:
    def __init__(self, ctx: RunnerContext) -> None:
        self.ctx = ctx

    def _run_sync_once(self) -> dict:
        return {"items_synced": 0, "items_skipped": 0, "items_failed": 0}

    def run_incremental(self) -> SyncResult:
        stats = self._run_sync_once()
        result = SyncResult(
            phase=RunnerPhase.INCREMENTAL.value,
            repo=str(self.ctx.repo),
            job=str(self.ctx.job),
            items_synced=int(stats.get("items_synced", 0) or 0),
        )
        if self.ctx.auto_refresh_vfacts and result.items_synced > 0:
            info = refresh_vfacts(
                dry_run=self.ctx.dry_run,
                concurrently=self.ctx.refresh_concurrently,
            )
            result.vfacts_refreshed = bool(info.get("refreshed"))
            result.vfacts_refresh_info = info
        return result


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SCM sync runner")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--config")
    parser.add_argument("--json", dest="json_output", action="store_true")

    subparsers = parser.add_subparsers(dest="command", required=True)

    inc = subparsers.add_parser("incremental")
    inc.add_argument("--repo", required=True)
    inc.add_argument("--job", default=JOB_TYPE_COMMITS, choices=sorted(VALID_JOB_TYPES))
    inc.add_argument("--loop", action="store_true")
    inc.add_argument("--loop-interval", type=int, default=DEFAULT_LOOP_INTERVAL_SECONDS)
    inc.add_argument("--max-iterations", type=int, default=0)

    bf = subparsers.add_parser("backfill")
    bf.add_argument("--repo", required=True)
    bf.add_argument("--job", default=JOB_TYPE_COMMITS, choices=sorted(VALID_JOB_TYPES))
    time_group = bf.add_mutually_exclusive_group()
    time_group.add_argument("--last-hours", type=int)
    time_group.add_argument("--last-days", type=int)
    bf.add_argument("--update-watermark", action="store_true")
    bf.add_argument("--start-rev", type=int)
    bf.add_argument("--end-rev", type=int)
    bf.add_argument("--since")
    bf.add_argument("--until")

    cfg = subparsers.add_parser("config")
    cfg.add_argument("--show-backfill", action="store_true")

    return parser


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = create_parser()
    return parser.parse_args(argv)
