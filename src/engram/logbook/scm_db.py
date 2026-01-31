# -*- coding: utf-8 -*-
"""
scm_db - SCM/Logbook 数据库访问层

本模块提供 SCM 同步子系统所需的数据库操作函数。

设计说明:
- 迁移自根目录 db.py，供包内模块使用
- 所有 SCM sync 相关的 DB 操作应使用此模块
- 根目录 db.py 将成为薄包装器，输出 deprecation 提示
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import psycopg
from psycopg.rows import dict_row

from engram.logbook.scm_sync_policy import build_circuit_breaker_key as _build_cb_key

MATERIALIZE_STATUS_PENDING = "pending"
MATERIALIZE_STATUS_DONE = "done"
MATERIALIZE_STATUS_FAILED = "failed"


class PauseReasonCode(str, Enum):
    ERROR_BUDGET = "error_budget"
    RATE_LIMIT_BUCKET = "rate_limit_bucket"
    CIRCUIT_OPEN = "circuit_open"
    MANUAL = "manual"


@dataclass
class RepoPauseRecord:
    repo_id: int
    job_type: str
    paused_until: float
    reason: str
    paused_at: float
    failure_rate: float = 0.0
    reason_code: Optional[str] = None

    def is_expired(self, *, now: float) -> bool:
        return now >= self.paused_until

    def remaining_seconds(self, *, now: float) -> float:
        return max(0.0, self.paused_until - now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "repo_id": self.repo_id,
            "job_type": self.job_type,
            "paused_until": self.paused_until,
            "reason": self.reason,
            "paused_at": self.paused_at,
            "failure_rate": self.failure_rate,
            "reason_code": self.reason_code,
        }

    @classmethod
    def from_dict(
        cls, *, repo_id: int, job_type: str, data: Optional[Dict[str, Any]] = None
    ) -> "RepoPauseRecord":
        data = data or {}
        return cls(
            repo_id=repo_id,
            job_type=job_type,
            paused_until=float(data.get("paused_until", 0.0)),
            reason=str(data.get("reason", "")),
            paused_at=float(data.get("paused_at", 0.0)),
            failure_rate=float(data.get("failure_rate", 0.0)),
            reason_code=data.get("reason_code"),
        )


def _dict_cursor(conn):
    return conn.cursor(row_factory=dict_row)


def _build_pause_key(repo_id: int, job_type: str) -> str:
    return f"repo:{repo_id}:{job_type}"


def _parse_pause_key(key: str) -> Optional[Tuple[int, str]]:
    if not key or not key.startswith("repo:"):
        return None
    parts = key.split(":")
    if len(parts) != 3:
        return None
    try:
        repo_id = int(parts[1])
    except ValueError:
        return None
    return repo_id, parts[2]


def get_conn(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, autocommit=False)


def upsert_repo(
    conn,
    repo_type: str,
    url: str,
    project_key: Optional[str] = None,
    default_branch: Optional[str] = None,
) -> int:
    """
    插入或更新仓库记录

    Args:
        conn: 数据库连接
        repo_type: 仓库类型 ('svn' 或 'git')
        url: 仓库 URL
        project_key: 项目标识（可选）
        default_branch: 默认分支（可选）

    Returns:
        repo_id: 仓库 ID

    注意:
        此函数使用 repo_type/url 作为主字段。
        数据库触发器会自动同步到弃用字段 vcs_type/remote_url。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO scm.repos (repo_type, url, project_key, default_branch)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (repo_type, url)
            DO UPDATE SET
                project_key = COALESCE(EXCLUDED.project_key, scm.repos.project_key),
                default_branch = COALESCE(EXCLUDED.default_branch, scm.repos.default_branch)
            RETURNING repo_id
            """,
            (repo_type, url, project_key, default_branch),
        )
        return cur.fetchone()[0]


def get_repo_by_url(conn, repo_type: str, url: str) -> Optional[Dict[str, Any]]:
    with _dict_cursor(conn) as cur:
        cur.execute(
            "SELECT repo_id, repo_type, url, project_key, default_branch, created_at FROM scm.repos WHERE repo_type=%s AND url=%s",
            (repo_type, url),
        )
        return cur.fetchone()


def get_repo_by_id(conn, repo_id: int) -> Optional[Dict[str, Any]]:
    """
    通过 repo_id 查询仓库

    Args:
        conn: 数据库连接
        repo_id: 仓库 ID

    Returns:
        仓库信息字典，不存在则返回 None
    """
    with _dict_cursor(conn) as cur:
        cur.execute(
            "SELECT repo_id, repo_type, url, project_key, default_branch, created_at FROM scm.repos WHERE repo_id=%s",
            (repo_id,),
        )
        return cur.fetchone()


def upsert_mr(
    conn,
    mr_id: str,
    repo_id: int,
    *,
    status: str,
    url: Optional[str] = None,
    author_user_id: Optional[str] = None,
    meta_json: Optional[Dict[str, Any]] = None,
    source_id: Optional[str] = None,
) -> str:
    meta_json = meta_json or {}
    if source_id is None:
        parts = str(mr_id).split(":")
        if len(parts) == 2:
            source_id = f"mr:{parts[0]}:{parts[1]}"
        else:
            source_id = f"mr:{repo_id}:{mr_id}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO scm.mrs (mr_id, repo_id, author_user_id, status, url, meta_json, source_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (mr_id) DO UPDATE
            SET status = EXCLUDED.status,
                url = COALESCE(EXCLUDED.url, scm.mrs.url),
                author_user_id = COALESCE(EXCLUDED.author_user_id, scm.mrs.author_user_id),
                meta_json = EXCLUDED.meta_json,
                source_id = COALESCE(EXCLUDED.source_id, scm.mrs.source_id),
                updated_at = now()
            """,
            (mr_id, repo_id, author_user_id, status, url, json.dumps(meta_json), source_id),
        )
    return mr_id


def insert_review_event(
    conn,
    mr_id: str,
    *,
    event_type: str,
    source_event_id: str,
    reviewer_user_id: Optional[str] = None,
    payload_json: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    payload_json = payload_json or {}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO scm.review_events (mr_id, source_event_id, reviewer_user_id, event_type, payload_json)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (mr_id, source_event_id) DO NOTHING
            RETURNING id
            """,
            (mr_id, source_event_id, reviewer_user_id, event_type, json.dumps(payload_json)),
        )
        row = cur.fetchone()
        return row[0] if row else None


def upsert_svn_revision(
    conn,
    repo_id: int,
    rev_num: int,
    *,
    author_raw: str,
    ts,
    message: Optional[str] = None,
    is_bulk: bool = False,
    bulk_reason: Optional[str] = None,
    meta_json: Optional[Dict[str, Any]] = None,
    source_id: Optional[str] = None,
) -> int:
    meta_json = meta_json or {}
    source_id = source_id or f"svn:{repo_id}:{rev_num}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO scm.svn_revisions (rev_num, repo_id, author_raw, ts, message, is_bulk, bulk_reason, meta_json, source_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (repo_id, rev_num) DO UPDATE
            SET author_raw = EXCLUDED.author_raw,
                ts = EXCLUDED.ts,
                message = EXCLUDED.message,
                is_bulk = EXCLUDED.is_bulk,
                bulk_reason = EXCLUDED.bulk_reason,
                meta_json = EXCLUDED.meta_json,
                source_id = EXCLUDED.source_id
            RETURNING svn_rev_id
            """,
            (
                rev_num,
                repo_id,
                author_raw,
                ts,
                message,
                is_bulk,
                bulk_reason,
                json.dumps(meta_json),
                source_id,
            ),
        )
        return cur.fetchone()[0]


def upsert_git_commit(
    conn,
    repo_id: int,
    commit_sha: str,
    *,
    author_raw: str,
    ts,
    message: Optional[str] = None,
    is_merge: bool = False,
    is_bulk: bool = False,
    bulk_reason: Optional[str] = None,
    meta_json: Optional[Dict[str, Any]] = None,
    source_id: Optional[str] = None,
) -> int:
    meta_json = meta_json or {}
    source_id = source_id or f"git:{repo_id}:{commit_sha}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO scm.git_commits (commit_sha, repo_id, author_raw, ts, message, is_merge, is_bulk, bulk_reason, meta_json, source_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (repo_id, commit_sha) DO UPDATE
            SET author_raw = EXCLUDED.author_raw,
                ts = EXCLUDED.ts,
                message = EXCLUDED.message,
                is_merge = EXCLUDED.is_merge,
                is_bulk = EXCLUDED.is_bulk,
                bulk_reason = EXCLUDED.bulk_reason,
                meta_json = EXCLUDED.meta_json,
                source_id = EXCLUDED.source_id
            RETURNING git_commit_id
            """,
            (
                commit_sha,
                repo_id,
                author_raw,
                ts,
                message,
                is_merge,
                is_bulk,
                bulk_reason,
                json.dumps(meta_json),
                source_id,
            ),
        )
        return cur.fetchone()[0]


def upsert_patch_blob(
    conn,
    source_type: str,
    source_id: str,
    sha256: str,
    *,
    uri: Optional[str],
    size_bytes: Optional[int] = None,
    format: str = "diff",
    meta_json: Optional[Dict[str, Any]] = None,
    evidence_uri: Optional[str] = None,
    chunking_version: Optional[str] = None,
) -> int:
    meta_json = meta_json or {}
    if "materialize_status" not in meta_json:
        meta_json["materialize_status"] = (
            MATERIALIZE_STATUS_DONE if uri else MATERIALIZE_STATUS_PENDING
        )
    meta_json.setdefault("attempts", 0)
    evidence_uri = evidence_uri or f"memory://patch_blobs/{source_type}/{source_id}/{sha256}"
    meta_json.setdefault("evidence_uri", evidence_uri)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO scm.patch_blobs
                (source_type, source_id, uri, evidence_uri, sha256, size_bytes, format, chunking_version, meta_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_type, source_id, sha256) DO NOTHING
            RETURNING blob_id
            """,
            (
                source_type,
                source_id,
                uri,
                evidence_uri,
                sha256,
                size_bytes,
                format,
                chunking_version,
                json.dumps(meta_json),
            ),
        )
        row = cur.fetchone()
        if row:
            return row[0]
        cur.execute(
            """
            SELECT blob_id FROM scm.patch_blobs
            WHERE source_type=%s AND source_id=%s AND sha256=%s
            """,
            (source_type, source_id, sha256),
        )
        return cur.fetchone()[0]


def get_patch_blob(conn, source_type: str, source_id: str, sha256: str) -> Optional[Dict[str, Any]]:
    with _dict_cursor(conn) as cur:
        cur.execute(
            """
            SELECT blob_id, source_type, source_id, uri, evidence_uri, sha256, size_bytes,
                   format, chunking_version, meta_json, created_at, updated_at
            FROM scm.patch_blobs
            WHERE source_type=%s AND source_id=%s AND sha256=%s
            """,
            (source_type, source_id, sha256),
        )
        return cur.fetchone()


def update_patch_blob_materialize_status(
    conn,
    *,
    blob_id: int,
    status: str,
    attempts: Optional[int] = None,
    last_error: Optional[str] = None,
) -> bool:
    with _dict_cursor(conn) as cur:
        cur.execute("SELECT meta_json FROM scm.patch_blobs WHERE blob_id=%s", (blob_id,))
        row = cur.fetchone()
        if not row:
            return False
        meta = row["meta_json"] or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        meta["materialize_status"] = status
        if attempts is not None:
            meta["attempts"] = attempts
        if last_error:
            meta["last_error"] = last_error
        cur.execute(
            "UPDATE scm.patch_blobs SET meta_json=%s WHERE blob_id=%s RETURNING blob_id",
            (json.dumps(meta), blob_id),
        )
        return cur.fetchone() is not None


def select_pending_blobs_for_materialize(
    conn,
    *,
    batch_size: int = 100,
    retry_failed: bool = False,
    max_attempts: int = 3,
) -> List[Dict[str, Any]]:
    status_list = [MATERIALIZE_STATUS_PENDING]
    if retry_failed:
        status_list.append(MATERIALIZE_STATUS_FAILED)
    with _dict_cursor(conn) as cur:
        cur.execute(
            """
            SELECT blob_id, source_type, source_id, uri, sha256, size_bytes, format, meta_json
            FROM scm.patch_blobs
            WHERE (uri IS NULL OR uri = '')
              AND COALESCE(meta_json->>'materialize_status', %s) = ANY(%s)
              AND COALESCE((meta_json->>'attempts')::int, 0) < %s
            ORDER BY blob_id
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            (MATERIALIZE_STATUS_PENDING, status_list, max_attempts, batch_size),
        )
        return cur.fetchall()


def mark_blob_done(
    conn,
    blob_id: int,
    *,
    uri: str,
    sha256: str,
    size_bytes: int,
    expected_sha256: Optional[str] = None,
) -> bool:
    with _dict_cursor(conn) as cur:
        cur.execute("SELECT sha256, meta_json FROM scm.patch_blobs WHERE blob_id=%s", (blob_id,))
        row = cur.fetchone()
        if not row:
            return False
        if expected_sha256 and row["sha256"] != expected_sha256:
            return False
        meta = row["meta_json"] or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        meta["materialize_status"] = MATERIALIZE_STATUS_DONE
        meta["attempts"] = int(meta.get("attempts", 0)) + 1
        meta["materialized_at"] = time.time()
        meta.pop("last_error", None)
        cur.execute(
            """
            UPDATE scm.patch_blobs
            SET uri=%s, sha256=%s, size_bytes=%s, meta_json=%s
            WHERE blob_id=%s
            RETURNING blob_id
            """,
            (uri, sha256, size_bytes, json.dumps(meta), blob_id),
        )
        return cur.fetchone() is not None


def mark_blob_failed(
    conn,
    *,
    blob_id: int,
    error: str,
    error_category: Optional[str] = None,
    mirror_uri: Optional[str] = None,
    actual_sha256: Optional[str] = None,
) -> bool:
    meta = {
        "materialize_status": MATERIALIZE_STATUS_FAILED,
        "attempts": 1,
        "last_error": error,
    }
    if error_category:
        meta["error_category"] = error_category
    if mirror_uri:
        meta["mirror_uri"] = mirror_uri
        meta["mirrored_at"] = time.time()
    if actual_sha256:
        meta["actual_sha256"] = actual_sha256

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE scm.patch_blobs
            SET meta_json=%s
            WHERE blob_id=%s
            RETURNING blob_id
            """,
            (json.dumps(meta), blob_id),
        )
        return cur.fetchone() is not None


def insert_sync_run_start(
    conn,
    run_id: str,
    repo_id: int,
    job_type: str,
    *,
    mode: str = "incremental",
    cursor_before: Optional[Dict[str, Any]] = None,
    meta_json: Optional[Dict[str, Any]] = None,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO scm.sync_runs (run_id, repo_id, job_type, mode, cursor_before, status, meta_json)
            VALUES (%s, %s, %s, %s, %s, 'running', %s)
            ON CONFLICT (run_id) DO NOTHING
            """,
            (
                run_id,
                repo_id,
                job_type,
                mode,
                json.dumps(cursor_before) if cursor_before else None,
                json.dumps(meta_json or {}),
            ),
        )
    return run_id


def insert_sync_run_finish(
    conn,
    run_id: str,
    *,
    status: str = "completed",
    cursor_after: Optional[Dict[str, Any]] = None,
    counts: Optional[Dict[str, Any]] = None,
    error_summary_json: Optional[Dict[str, Any]] = None,
    degradation_json: Optional[Dict[str, Any]] = None,
    logbook_item_id: Optional[int] = None,
    meta_json: Optional[Dict[str, Any]] = None,
) -> bool:
    counts = counts or {"synced_count": 0}
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE scm.sync_runs
            SET status=%s,
                finished_at=now(),
                cursor_after=%s,
                counts=%s,
                error_summary_json=%s,
                degradation_json=%s,
                logbook_item_id=%s,
                meta_json=%s
            WHERE run_id=%s
            RETURNING run_id
            """,
            (
                status,
                json.dumps(cursor_after) if cursor_after else None,
                json.dumps(counts),
                json.dumps(error_summary_json) if error_summary_json else None,
                json.dumps(degradation_json) if degradation_json else None,
                logbook_item_id,
                json.dumps(meta_json or {}),
                run_id,
            ),
        )
        return cur.fetchone() is not None


def get_sync_run(conn, run_id: str) -> Optional[Dict[str, Any]]:
    with _dict_cursor(conn) as cur:
        cur.execute(
            """
            SELECT run_id, repo_id, job_type, mode, status,
                   started_at, finished_at,
                   cursor_before, cursor_after,
                   counts, error_summary_json, degradation_json,
                   logbook_item_id, meta_json, synced_count
            FROM scm.sync_runs
            WHERE run_id=%s
            """,
            (run_id,),
        )
        return cur.fetchone()


def get_latest_sync_run(
    conn, repo_id: int, job_type: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    query = """
        SELECT run_id, repo_id, job_type, mode, status,
               started_at, finished_at,
               cursor_before, cursor_after,
               counts, error_summary_json, degradation_json,
               logbook_item_id, meta_json, synced_count
        FROM scm.sync_runs
        WHERE repo_id=%s
    """
    params = [repo_id]
    if job_type:
        query += " AND job_type=%s"
        params.append(job_type)
    query += " ORDER BY started_at DESC LIMIT 1"
    with _dict_cursor(conn) as cur:
        cur.execute(query, params)
        return cur.fetchone()


def list_sync_runs(
    conn, *, repo_id: Optional[int] = None, status: Optional[str] = None, limit: int = 100
) -> List[Dict[str, Any]]:
    query = """
        SELECT run_id, repo_id, job_type, mode, status,
               started_at, finished_at,
               cursor_before, cursor_after,
               counts, error_summary_json, degradation_json,
               logbook_item_id, meta_json, synced_count
        FROM scm.sync_runs
        WHERE 1=1
    """
    params: List[Any] = []
    if repo_id is not None:
        query += " AND repo_id=%s"
        params.append(repo_id)
    if status is not None:
        query += " AND status=%s"
        params.append(status)
    query += " ORDER BY started_at DESC LIMIT %s"
    params.append(limit)
    with _dict_cursor(conn) as cur:
        cur.execute(query, params)
        return cur.fetchall()


def enqueue_sync_job(
    conn,
    *,
    repo_id: int,
    job_type: str,
    mode: str = "incremental",
    priority: int = 100,
    payload_json: Optional[Dict[str, Any]] = None,
) -> str:
    payload_json = payload_json or {}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO scm.sync_jobs (repo_id, job_type, mode, priority, payload_json)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING job_id
            """,
            (repo_id, job_type, mode, priority, json.dumps(payload_json)),
        )
        job_id = cur.fetchone()[0]
    return str(job_id)


def list_sync_jobs(
    conn, *, repo_id: Optional[int] = None, limit: int = 100
) -> List[Dict[str, Any]]:
    query = """
        SELECT job_id, repo_id, job_type, mode, status,
               priority, attempts, max_attempts,
               not_before, locked_by, locked_at, lease_seconds,
               last_error, last_run_id,
               payload_json, created_at, updated_at
        FROM scm.sync_jobs
        WHERE 1=1
    """
    params: List[Any] = []
    if repo_id is not None:
        query += " AND repo_id=%s"
        params.append(repo_id)
    query += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)
    with _dict_cursor(conn) as cur:
        cur.execute(query, params)
        return cur.fetchall()


def list_sync_locks(
    conn, *, repo_id: Optional[int] = None, limit: int = 100
) -> List[Dict[str, Any]]:
    query = """
        SELECT lock_id, repo_id, job_type, locked_by, locked_at, lease_seconds, updated_at, created_at
        FROM scm.sync_locks
        WHERE 1=1
    """
    params: List[Any] = []
    if repo_id is not None:
        query += " AND repo_id=%s"
        params.append(repo_id)
    query += " ORDER BY lock_id DESC LIMIT %s"
    params.append(limit)
    with _dict_cursor(conn) as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    now_ts = time.time()
    for row in rows:
        row["is_locked"] = row["locked_by"] is not None
        if row["locked_at"] is None:
            row["is_expired"] = False
        else:
            elapsed = now_ts - row["locked_at"].timestamp()
            row["is_expired"] = elapsed > row["lease_seconds"]
    return rows


def list_kv_cursors(
    conn, *, namespace: str = "scm.sync", key_prefix: Optional[str] = None, limit: int = 200
) -> List[Dict[str, Any]]:
    query = """
        SELECT namespace, key, value_json, updated_at
        FROM logbook.kv
        WHERE namespace=%s
    """
    params: List[Any] = [namespace]
    if key_prefix:
        query += " AND key LIKE %s"
        params.append(f"{key_prefix}%")
    query += " ORDER BY updated_at DESC LIMIT %s"
    params.append(limit)
    with _dict_cursor(conn) as cur:
        cur.execute(query, params)
        return cur.fetchall()


def list_repos(conn, *, repo_type: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    query = """
        SELECT repo_id, repo_type, url, project_key, default_branch, created_at
        FROM scm.repos
        WHERE 1=1
    """
    params: List[Any] = []
    if repo_type:
        query += " AND repo_type=%s"
        params.append(repo_type)
    query += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)
    with _dict_cursor(conn) as cur:
        cur.execute(query, params)
        return cur.fetchall()


def get_sync_status_summary(conn) -> Dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM scm.repos")
        repos_count = cur.fetchone()[0]
        cur.execute("SELECT repo_type, COUNT(*) FROM scm.repos GROUP BY repo_type")
        repos_by_type = {row[0]: row[1] for row in cur.fetchall()}
        cur.execute(
            """
            SELECT status, COUNT(*)
            FROM scm.sync_runs
            WHERE started_at >= now() - interval '24 hours'
            GROUP BY status
            """
        )
        runs_24h_by_status = {row[0]: row[1] for row in cur.fetchall()}
        cur.execute("SELECT status, COUNT(*) FROM scm.sync_jobs GROUP BY status")
        jobs_by_status = {row[0]: row[1] for row in cur.fetchall()}
        cur.execute(
            """
            SELECT COUNT(*) FROM scm.sync_locks
            WHERE locked_by IS NOT NULL
            """
        )
        active_locks = cur.fetchone()[0]
        cur.execute(
            """
            SELECT COUNT(*) FROM scm.sync_locks
            WHERE locked_by IS NOT NULL AND locked_at + lease_seconds * interval '1 second' < now()
            """
        )
        expired_locks = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM logbook.kv WHERE namespace = %s", ("scm.sync",))
        cursors_count = cur.fetchone()[0]

    return {
        "repos_count": repos_count,
        "repos_by_type": repos_by_type,
        "runs_24h_by_status": runs_24h_by_status,
        "jobs_by_status": jobs_by_status,
        "locks": {"active": active_locks, "expired": expired_locks},
        "cursors_count": cursors_count,
    }


def list_expired_running_jobs(
    conn, *, grace_seconds: int = 60, limit: int = 100
) -> List[Dict[str, Any]]:
    with _dict_cursor(conn) as cur:
        cur.execute(
            """
            SELECT job_id, repo_id, job_type, status, attempts, max_attempts,
                   locked_at, lease_seconds, last_error
            FROM scm.sync_jobs
            WHERE status = 'running'
              AND locked_at IS NOT NULL
              AND locked_at + (lease_seconds + %s) * interval '1 second' < now()
            ORDER BY locked_at ASC
            LIMIT %s
            """,
            (grace_seconds, limit),
        )
        return cur.fetchall()


def list_expired_running_runs(
    conn, *, max_duration_seconds: int = 1800, limit: int = 100
) -> List[Dict[str, Any]]:
    with _dict_cursor(conn) as cur:
        cur.execute(
            """
            SELECT run_id, repo_id, job_type, status, started_at
            FROM scm.sync_runs
            WHERE status = 'running'
              AND started_at + %s * interval '1 second' < now()
            ORDER BY started_at ASC
            LIMIT %s
            """,
            (max_duration_seconds, limit),
        )
        return cur.fetchall()


def list_expired_locks(conn, *, grace_seconds: int = 0, limit: int = 100) -> List[Dict[str, Any]]:
    with _dict_cursor(conn) as cur:
        cur.execute(
            """
            SELECT lock_id, repo_id, job_type, locked_by, locked_at, lease_seconds
            FROM scm.sync_locks
            WHERE locked_by IS NOT NULL
              AND locked_at + (lease_seconds + %s) * interval '1 second' < now()
            ORDER BY locked_at ASC
            LIMIT %s
            """,
            (grace_seconds, limit),
        )
        return cur.fetchall()


def mark_job_as_failed_by_reaper(
    conn,
    job_id: str,
    error: Optional[str] = None,
    *,
    reason: Optional[str] = None,
    retry_delay_seconds: int = 0,
) -> bool:
    """
    Reaper 将过期任务标记为 failed。

    注意：不修改 attempts。根据 "attempts 只在 claim 时 +1" 语义，
    reaper 只是清理过期任务，不应该增加 attempts。
    """
    message = error or reason or "lease_expired"
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE scm.sync_jobs
            SET status = 'failed',
                last_error = %s,
                locked_by = NULL,
                locked_at = NULL,
                not_before = now() + %s * interval '1 second',
                updated_at = now()
            WHERE job_id = %s
            RETURNING job_id
            """,
            (message, retry_delay_seconds, job_id),
        )
        return cur.fetchone() is not None


def mark_job_as_dead_by_reaper(
    conn,
    job_id: str,
    error: Optional[str] = None,
    *,
    reason: Optional[str] = None,
) -> bool:
    """
    Reaper 将过期任务标记为 dead（不可重试）。

    注意：不修改 attempts。根据 "attempts 只在 claim 时 +1" 语义，
    reaper 只是清理过期任务，不应该增加 attempts。
    """
    message = error or reason or "lease_expired"
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE scm.sync_jobs
            SET status = 'dead',
                last_error = %s,
                locked_by = NULL,
                locked_at = NULL,
                updated_at = now()
            WHERE job_id = %s
            RETURNING job_id
            """,
            (message, job_id),
        )
        return cur.fetchone() is not None


def mark_run_as_failed_by_reaper(
    conn,
    run_id: str,
    error_summary: Optional[Dict[str, Any]] = None,
    *,
    reason: Optional[str] = None,
) -> bool:
    if error_summary is None:
        error_summary = {"error_type": "REAPER_TIMEOUT", "message": reason or "run_timeout"}
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE scm.sync_runs
            SET status = 'failed',
                finished_at = now(),
                error_summary_json = %s
            WHERE run_id = %s
            RETURNING run_id
            """,
            (json.dumps(error_summary), run_id),
        )
        return cur.fetchone() is not None


def force_release_lock(conn, lock_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE scm.sync_locks
            SET locked_by = NULL,
                locked_at = NULL,
                updated_at = now()
            WHERE lock_id = %s
            RETURNING lock_id
            """,
            (lock_id,),
        )
        return cur.fetchone() is not None


def save_circuit_breaker_state(conn, key: str, state: Dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO logbook.kv (namespace, key, value_json)
            VALUES ('scm.sync_health', %s, %s)
            ON CONFLICT (namespace, key) DO UPDATE
            SET value_json = EXCLUDED.value_json,
                updated_at = now()
            """,
            (key, json.dumps(state)),
        )


def load_circuit_breaker_state(conn, key: str) -> Optional[Dict[str, Any]]:
    with _dict_cursor(conn) as cur:
        cur.execute(
            """
            SELECT value_json FROM logbook.kv
            WHERE namespace = 'scm.sync_health' AND key = %s
            """,
            (key,),
        )
        row = cur.fetchone()
        return row["value_json"] if row else None


def delete_circuit_breaker_state(conn, key: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM logbook.kv WHERE namespace = 'scm.sync_health' AND key = %s",
            (key,),
        )


def build_circuit_breaker_key(
    project_key: str = "default",
    scope: str = "global",
    **kwargs,
) -> str:
    return _build_cb_key(project_key=project_key, scope=scope, **kwargs)


def set_repo_job_pause(
    conn,
    *,
    repo_id: int,
    job_type: str,
    pause_duration_seconds: float,
    reason: str,
    reason_code: PauseReasonCode = PauseReasonCode.ERROR_BUDGET,
    failure_rate: float = 0.0,
) -> RepoPauseRecord:
    paused_at = time.time()
    paused_until = paused_at + pause_duration_seconds
    record = RepoPauseRecord(
        repo_id=repo_id,
        job_type=job_type,
        paused_until=paused_until,
        reason=reason,
        paused_at=paused_at,
        failure_rate=failure_rate,
        reason_code=str(reason_code),
    )
    key = _build_pause_key(repo_id, job_type)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO logbook.kv (namespace, key, value_json)
            VALUES ('scm.sync_pauses', %s, %s)
            ON CONFLICT (namespace, key) DO UPDATE
            SET value_json = EXCLUDED.value_json,
                updated_at = now()
            """,
            (key, json.dumps(record.to_dict())),
        )
    return record


def get_repo_job_pause(
    conn,
    *,
    repo_id: int,
    job_type: str,
) -> Optional[RepoPauseRecord]:
    """获取仓库任务的暂停记录"""
    key = _build_pause_key(repo_id, job_type)
    with _dict_cursor(conn) as cur:
        cur.execute(
            """
            SELECT value_json FROM logbook.kv
            WHERE namespace = 'scm.sync_pauses' AND key = %s
            """,
            (key,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return RepoPauseRecord.from_dict(repo_id=repo_id, job_type=job_type, data=row["value_json"])


def list_all_pauses(conn, *, include_expired: bool = False) -> List[RepoPauseRecord]:
    """列出所有暂停记录"""
    now_ts = time.time()
    with _dict_cursor(conn) as cur:
        cur.execute(
            """
            SELECT key, value_json FROM logbook.kv
            WHERE namespace = 'scm.sync_pauses'
            """
        )
        rows = cur.fetchall()

    results = []
    for row in rows:
        parsed = _parse_pause_key(row["key"])
        if parsed is None:
            continue
        repo_id, job_type = parsed
        record = RepoPauseRecord.from_dict(
            repo_id=repo_id, job_type=job_type, data=row["value_json"]
        )
        if include_expired or not record.is_expired(now=now_ts):
            results.append(record)
    return results


def get_paused_job_pairs(conn) -> set:
    """
    获取当前暂停的 (repo_id, job_type) 对集合

    从 logbook.kv (namespace='scm.sync_pauses') 读取有效的暂停记录，
    返回未过期的 (repo_id, job_type) 集合。

    设计说明:
    - 此函数仅返回 kv 暂停记录，不包括 DB 中的 active jobs
    - 与 get_active_job_pairs() 分离，调用方在 scheduler 层合并
    - 返回的是 set[tuple[int, str]] 格式，与 queued_pairs 格式一致

    Returns:
        set[tuple[int, str]]: 暂停的 (repo_id, job_type) 集合

    Usage:
        paused_pairs = get_paused_job_pairs(conn)
        queued_pairs = get_active_job_pairs(conn)
        # scheduler 层合并后传入 select_jobs_to_enqueue
    """
    pauses = list_all_pauses(conn, include_expired=False)
    return {(p.repo_id, p.job_type) for p in pauses}


def get_pause_snapshot(conn) -> Dict[str, Any]:
    """
    获取暂停状态快照

    返回当前暂停状态的完整快照，包含：
    - paused_pairs: (repo_id, job_type) 集合
    - pause_count: 暂停记录总数
    - by_reason_code: 按 reason_code 分组的暂停计数
    - snapshot_at: 快照时间戳

    Returns:
        Dict[str, Any]: 暂停快照字典，可用于构造 PauseSnapshot 数据类
    """
    now_ts = time.time()
    pauses = list_all_pauses(conn, include_expired=False)

    paused_pairs = set()
    by_reason_code: Dict[str, int] = {}

    for p in pauses:
        paused_pairs.add((p.repo_id, p.job_type))
        reason_code = p.reason_code or "unknown"
        by_reason_code[reason_code] = by_reason_code.get(reason_code, 0) + 1

    return {
        "paused_pairs": paused_pairs,
        "pause_count": len(paused_pairs),
        "by_reason_code": by_reason_code,
        "snapshot_at": now_ts,
    }


def list_repos_for_scheduling(
    conn,
    *,
    repo_type: Optional[str] = None,
    limit: int = 1000,
) -> List[Dict[str, Any]]:
    """
    获取待调度的仓库列表，用于 scheduler 扫描

    返回仓库基础信息和最近同步统计。
    """
    query = """
        SELECT
            r.repo_id,
            r.repo_type,
            r.url,
            r.project_key,
            r.default_branch,
            r.created_at
        FROM scm.repos r
        WHERE 1=1
    """
    params: List[Any] = []
    if repo_type:
        query += " AND r.repo_type = %s"
        params.append(repo_type)
    query += " ORDER BY r.repo_id LIMIT %s"
    params.append(limit)

    with _dict_cursor(conn) as cur:
        cur.execute(query, params)
        return cur.fetchall()


def get_repo_sync_stats(
    conn,
    repo_id: int,
    *,
    window_count: int = 10,
) -> Dict[str, Any]:
    """
    获取仓库的同步统计（用于 scheduler 决策）

    统计最近 N 次运行的成功/失败率、429 命中率等。
    """
    with _dict_cursor(conn) as cur:
        # 获取最近 N 次运行
        cur.execute(
            """
            SELECT
                run_id, status, started_at, finished_at,
                counts, error_summary_json
            FROM scm.sync_runs
            WHERE repo_id = %s
            ORDER BY started_at DESC
            LIMIT %s
            """,
            (repo_id, window_count),
        )
        runs = cur.fetchall()

        if not runs:
            return {
                "repo_id": repo_id,
                "total_runs": 0,
                "failed_count": 0,
                "completed_count": 0,
                "last_run_status": None,
                "last_run_at": None,
                "total_429_hits": 0,
                "total_requests": 0,
            }

        failed_count = sum(1 for r in runs if r["status"] == "failed")
        completed_count = sum(1 for r in runs if r["status"] == "completed")
        total_429_hits = 0
        total_requests = 0

        for run in runs:
            counts = run.get("counts") or {}
            if isinstance(counts, str):
                counts = json.loads(counts)
            total_429_hits += counts.get("total_429_hits", 0)
            total_requests += counts.get("total_requests", 0)

        last_run = runs[0]
        return {
            "repo_id": repo_id,
            "total_runs": len(runs),
            "failed_count": failed_count,
            "completed_count": completed_count,
            "last_run_status": last_run["status"],
            "last_run_at": last_run["started_at"].timestamp() if last_run["started_at"] else None,
            "total_429_hits": total_429_hits,
            "total_requests": total_requests,
        }


def get_cursor_value(
    conn,
    repo_id: int,
    job_type: str,
    *,
    namespace: str = "scm.sync",
) -> Optional[Dict[str, Any]]:
    """获取仓库同步游标"""
    key = f"cursor:{repo_id}:{job_type}"
    with _dict_cursor(conn) as cur:
        cur.execute(
            """
            SELECT value_json, updated_at FROM logbook.kv
            WHERE namespace = %s AND key = %s
            """,
            (namespace, key),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "value": row["value_json"],
            "updated_at": row["updated_at"].timestamp() if row["updated_at"] else None,
        }


def get_active_job_pairs(conn) -> List[Tuple[int, str]]:
    """
    获取当前活跃的 (repo_id, job_type) 对

    用于 scheduler 跳过已在队列中的任务（per job_type 去重）。
    活跃状态包括 pending 和 running。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT repo_id, job_type
            FROM scm.sync_jobs
            WHERE status IN ('pending', 'running')
            """
        )
        return [(row[0], row[1]) for row in cur.fetchall()]


def get_budget_snapshot(conn) -> Dict[str, Any]:
    """
    获取当前预算快照

    统计当前活跃任务数，按 instance 和 tenant 分组。
    """
    with _dict_cursor(conn) as cur:
        # 统计全局活跃任务数
        cur.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE status = 'running') as global_running,
                COUNT(*) FILTER (WHERE status = 'pending') as global_pending,
                COUNT(*) as global_active
            FROM scm.sync_jobs
            WHERE status IN ('pending', 'running')
            """
        )
        global_counts = cur.fetchone()

        # 按 instance 分组统计（从 payload_json 或 gitlab_instance 列）
        cur.execute(
            """
            SELECT
                COALESCE(gitlab_instance, payload_json->>'gitlab_instance') as instance_key,
                COUNT(*) as active_count
            FROM scm.sync_jobs
            WHERE status IN ('pending', 'running')
              AND COALESCE(gitlab_instance, payload_json->>'gitlab_instance') IS NOT NULL
            GROUP BY COALESCE(gitlab_instance, payload_json->>'gitlab_instance')
            """
        )
        by_instance = {row["instance_key"]: row["active_count"] for row in cur.fetchall()}

        # 按 tenant 分组统计
        cur.execute(
            """
            SELECT
                COALESCE(tenant_id, payload_json->>'tenant_id') as tenant_key,
                COUNT(*) as active_count
            FROM scm.sync_jobs
            WHERE status IN ('pending', 'running')
              AND COALESCE(tenant_id, payload_json->>'tenant_id') IS NOT NULL
            GROUP BY COALESCE(tenant_id, payload_json->>'tenant_id')
            """
        )
        by_tenant = {row["tenant_key"]: row["active_count"] for row in cur.fetchall()}

    return {
        "global_running": global_counts["global_running"] or 0,
        "global_pending": global_counts["global_pending"] or 0,
        "global_active": global_counts["global_active"] or 0,
        "by_instance": by_instance,
        "by_tenant": by_tenant,
    }


def get_rate_limit_bucket_status(
    conn,
    instance_key: str,
    *,
    namespace: str = "scm.rate_limit",
) -> Optional[Dict[str, Any]]:
    """获取实例级速率限制桶状态"""
    key = f"bucket:{instance_key}"
    with _dict_cursor(conn) as cur:
        cur.execute(
            """
            SELECT value_json, updated_at FROM logbook.kv
            WHERE namespace = %s AND key = %s
            """,
            (namespace, key),
        )
        row = cur.fetchone()
        if not row:
            return None
        return row["value_json"]


def get_sync_runs_health_stats(
    conn,
    *,
    window_minutes: int = 30,
    window_count: int = 20,
    instance_key: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    获取同步运行健康统计（用于熔断决策）

    统计最近一段时间的同步运行成功率、失败率、429 命中率等。
    """
    # 构建过滤条件

    # 注意：instance_key 和 tenant_id 需要关联到 repos 表或从 payload 读取
    # 简化实现：暂时只按时间窗口统计全局

    with _dict_cursor(conn) as cur:
        cur.execute(
            """
            SELECT
                COUNT(*) as total_runs,
                COUNT(*) FILTER (WHERE status = 'failed') as failed_count,
                COUNT(*) FILTER (WHERE status = 'completed') as completed_count,
                COUNT(*) FILTER (WHERE status = 'no_data') as no_data_count,
                SUM(COALESCE((counts->>'total_429_hits')::int, 0)) as total_429_hits,
                SUM(COALESCE((counts->>'total_requests')::int, 0)) as total_requests,
                SUM(COALESCE((counts->>'total_timeout_count')::int, 0)) as total_timeout_count
            FROM scm.sync_runs r
            WHERE r.started_at >= now() - %s * interval '1 minute'
            LIMIT %s
            """,
            (window_minutes, window_count),
        )
        row = cur.fetchone()

        total_runs = row["total_runs"] or 0
        failed_count = row["failed_count"] or 0
        total_requests = row["total_requests"] or 0
        total_429_hits = row["total_429_hits"] or 0

        return {
            "total_runs": total_runs,
            "failed_count": failed_count,
            "completed_count": row["completed_count"] or 0,
            "no_data_count": row["no_data_count"] or 0,
            "failed_rate": failed_count / total_runs if total_runs > 0 else 0.0,
            "rate_limit_rate": total_429_hits / total_requests if total_requests > 0 else 0.0,
            "total_429_hits": total_429_hits,
            "total_requests": total_requests,
            "total_timeout_count": row["total_timeout_count"] or 0,
        }
