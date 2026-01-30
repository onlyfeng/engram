#!/usr/bin/env python3
"""
artifact_audit - 制品审计工具（兼容测试所需功能）
"""

from __future__ import annotations

import argparse
import os
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple
from urllib.parse import urlparse

from engram.logbook.artifact_store import (
    ArtifactNotFoundError,
    ArtifactReadError,
    FileUriStore,
    LocalArtifactsStore,
    ObjectStore,
    get_artifact_store_from_config,
)
from engram.logbook.config import get_app_config
from engram.logbook.hashing import sha256 as compute_sha256


@dataclass
class AuditResult:
    table: str
    record_id: int
    uri: str
    expected_sha256: Optional[str]
    actual_sha256: Optional[str]
    size_bytes: Optional[int]
    status: str
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "table": self.table,
            "record_id": self.record_id,
            "uri": self.uri,
            "expected_sha256": self.expected_sha256,
            "actual_sha256": self.actual_sha256,
            "size_bytes": self.size_bytes,
            "status": self.status,
            "error_message": self.error_message,
        }


@dataclass
class AuditSummary:
    total_records: int = 0
    sampled_records: int = 0
    audited_records: int = 0
    ok_count: int = 0
    mismatch_count: int = 0
    missing_count: int = 0
    error_count: int = 0
    skipped_count: int = 0
    total_bytes: int = 0
    duration_seconds: float = 0.0
    start_time: str = ""
    end_time: str = ""
    tables_audited: List[str] = field(default_factory=list)
    mismatches: List[Dict[str, Any]] = field(default_factory=list)
    missing: List[Dict[str, Any]] = field(default_factory=list)
    next_cursor: Optional[str] = None

    @property
    def has_issues(self) -> bool:
        return (self.mismatch_count + self.missing_count + self.error_count) > 0

    def to_dict(self) -> Dict[str, Any]:
        tables_audited = self.tables_audited or []
        mismatches = self.mismatches or []
        missing = self.missing or []
        result = {
            "total_records": self.total_records,
            "sampled_records": self.sampled_records,
            "audited_records": self.audited_records,
            "ok_count": self.ok_count,
            "mismatch_count": self.mismatch_count,
            "missing_count": self.missing_count,
            "error_count": self.error_count,
            "skipped_count": self.skipped_count,
            "total_bytes": self.total_bytes,
            "duration_seconds": self.duration_seconds,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "tables_audited": tables_audited,
            "mismatches": mismatches,
            "missing": missing,
            "has_issues": self.has_issues,
        }
        if self.next_cursor:
            result["next_cursor"] = self.next_cursor
        return result


class RateLimiter:
    """简单的速率限制器（线程安全）"""

    def __init__(self, max_bytes_per_sec: Optional[int]) -> None:
        self.max_bytes_per_sec = max_bytes_per_sec
        self._lock = threading.Lock()
        self._next_allowed_time = time.monotonic()

    def wait_if_needed(self, size_bytes: int) -> None:
        if not self.max_bytes_per_sec:
            return
        if size_bytes <= 0:
            return
        with self._lock:
            now = time.monotonic()
            if now < self._next_allowed_time:
                time.sleep(self._next_allowed_time - now)
                now = time.monotonic()
            duration = size_bytes / float(self.max_bytes_per_sec)
            self._next_allowed_time = max(now, self._next_allowed_time) + duration


class ArtifactAuditor:
    def __init__(
        self,
        *,
        artifacts_root: Optional[str] = None,
        conn=None,
        artifact_store=None,
        sample_rate: float = 1.0,
        max_bytes_per_sec: Optional[int] = None,
        head_only: bool = False,
        workers: int = 1,
    ) -> None:
        self.artifacts_root = str(artifacts_root) if artifacts_root else None
        self.conn = conn
        self.sample_rate = sample_rate
        self.head_only = head_only
        self.workers = max(1, workers)
        self._rate_limiter = RateLimiter(max_bytes_per_sec)
        self._artifact_store = artifact_store
        self._object_store = None
        self._file_store = None

    def _get_default_store(self):
        if self._artifact_store is None:
            config = get_app_config()
            self._artifact_store = get_artifact_store_from_config(config)
        return self._artifact_store

    def _get_store_for_uri(self, uri: str):
        parsed = urlparse(uri)
        if parsed.scheme == "file":
            if self._file_store is None:
                self._file_store = FileUriStore()
            return self._file_store, uri

        if parsed.scheme == "s3":
            bucket = parsed.netloc
            key = parsed.path.lstrip("/")

            configured_bucket = os.environ.get("ENGRAM_S3_BUCKET")
            if not configured_bucket:
                raise ArtifactReadError("未配置 bucket")
            if not bucket:
                raise ArtifactReadError("缺少 bucket")
            if not key:
                raise ArtifactReadError("缺少对象 key")
            if bucket != configured_bucket:
                raise ArtifactReadError(
                    f"拒绝跨 bucket 审计: {bucket} != {configured_bucket}"
                )

            if self._object_store is None:
                self._object_store = ObjectStore(
                    endpoint=os.environ.get("ENGRAM_S3_ENDPOINT"),
                    access_key=os.environ.get("ENGRAM_S3_ACCESS_KEY"),
                    secret_key=os.environ.get("ENGRAM_S3_SECRET_KEY"),
                    bucket=configured_bucket,
                )
            return self._object_store, key

        # 默认使用配置中的 artifact store（支持本地或对象存储）
        return self._get_default_store(), uri

    def audit_record(
        self,
        table: str,
        record_id: int,
        uri: str,
        expected_sha256: Optional[str],
    ) -> AuditResult:
        if self.sample_rate <= 0:
            return AuditResult(
                table=table,
                record_id=record_id,
                uri=uri,
                expected_sha256=expected_sha256,
                actual_sha256=None,
                size_bytes=None,
                status="skipped",
                error_message="sample_rate=0",
            )

        if self.sample_rate < 1.0 and random.random() > self.sample_rate:
            return AuditResult(
                table=table,
                record_id=record_id,
                uri=uri,
                expected_sha256=expected_sha256,
                actual_sha256=None,
                size_bytes=None,
                status="skipped",
                error_message="sampled_out",
            )

        try:
            store, resolved_uri = self._get_store_for_uri(uri)
            actual_sha256: Optional[str] = None
            size_bytes: Optional[int] = None

            if isinstance(store, ObjectStore) and self.head_only:
                client = store._get_client()
                object_key = store._object_key(resolved_uri)
                head = client.head_object(Bucket=store.bucket, Key=object_key)
                size_bytes = int(head.get("ContentLength", 0))
                metadata = head.get("Metadata", {}) or {}
                actual_sha256 = metadata.get("sha256")
                self._rate_limiter.wait_if_needed(size_bytes)
                if not actual_sha256:
                    return AuditResult(
                        table=table,
                        record_id=record_id,
                        uri=uri,
                        expected_sha256=expected_sha256,
                        actual_sha256=None,
                        size_bytes=size_bytes,
                        status="head_only_unverified",
                        error_message="metadata missing sha256",
                    )
            else:
                info = store.get_info(resolved_uri)
                actual_sha256 = info.get("sha256")
                size_bytes = int(info.get("size_bytes", 0))
                self._rate_limiter.wait_if_needed(size_bytes)

            if expected_sha256 and actual_sha256:
                if expected_sha256.lower() == actual_sha256.lower():
                    status = "ok"
                else:
                    status = "mismatch"
            else:
                status = "ok" if actual_sha256 else "error"

            return AuditResult(
                table=table,
                record_id=record_id,
                uri=uri,
                expected_sha256=expected_sha256,
                actual_sha256=actual_sha256,
                size_bytes=size_bytes,
                status=status,
                error_message=None if status == "ok" else "hash mismatch",
            )
        except ArtifactNotFoundError as exc:
            return AuditResult(
                table=table,
                record_id=record_id,
                uri=uri,
                expected_sha256=expected_sha256,
                actual_sha256=None,
                size_bytes=None,
                status="missing",
                error_message=str(exc),
            )
        except ArtifactReadError as exc:
            return AuditResult(
                table=table,
                record_id=record_id,
                uri=uri,
                expected_sha256=expected_sha256,
                actual_sha256=None,
                size_bytes=None,
                status="error",
                error_message=str(exc),
            )
        except Exception as exc:
            return AuditResult(
                table=table,
                record_id=record_id,
                uri=uri,
                expected_sha256=expected_sha256,
                actual_sha256=None,
                size_bytes=None,
                status="error",
                error_message=str(exc),
            )

    def _iter_rows(
        self,
        table: str,
        *,
        limit: Optional[int] = None,
        prefix: Optional[str] = None,
        since: Optional[str] = None,
    ) -> Iterator[Tuple[int, str, str, Optional[datetime]]]:
        if not self.conn:
            return iter([])
        if table == "patch_blobs":
            query = "SELECT blob_id, uri, sha256, created_at FROM scm.patch_blobs"
        else:
            query = "SELECT attachment_id, uri, sha256, created_at FROM logbook.attachments"

        params: List[Any] = []
        conditions: List[str] = []
        if prefix:
            conditions.append("uri LIKE %s")
            params.append(f"{prefix}%")
        if since:
            conditions.append("created_at >= %s")
            params.append(since)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        if limit:
            query += " LIMIT %s"
            params.append(limit)

        with self.conn.cursor() as cur:
            cur.execute(query, params if params else None)
            for row in cur:
                if len(row) >= 4:
                    yield row[0], row[1], row[2], row[3]
                else:
                    yield row[0], row[1], row[2], None

    def audit_table(
        self,
        table: str,
        *,
        limit: Optional[int] = None,
        prefix: Optional[str] = None,
        since: Optional[str] = None,
    ) -> Iterator[Tuple[AuditResult, Optional[datetime]]]:
        rows = list(self._iter_rows(table, limit=limit, prefix=prefix, since=since))
        if not rows:
            return iter(())

        if self.workers <= 1:
            for record_id, uri, expected_sha256, created_at in rows:
                result = self.audit_record(table, record_id, uri, expected_sha256)
                yield result, created_at
            return

        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            future_map = {
                executor.submit(self.audit_record, table, rid, uri, sha): (uri, created_at)
                for rid, uri, sha, created_at in rows
            }
            for future in as_completed(future_map):
                created_at = future_map[future][1]
                yield future.result(), created_at

    def run_audit(
        self,
        *,
        tables: Iterable[str],
        limit: Optional[int] = None,
        prefix: Optional[str] = None,
        since: Optional[str] = None,
        fail_on_mismatch: bool = False,
    ) -> AuditSummary:
        summary = AuditSummary()
        start_time = datetime.utcnow()
        start_monotonic = time.monotonic()
        summary.start_time = start_time.isoformat() + "Z"
        max_cursor: Optional[datetime] = None

        for table in tables:
            if table not in summary.tables_audited:
                summary.tables_audited.append(table)
            for result, created_at in self.audit_table(table, limit=limit, prefix=prefix, since=since):
                summary.total_records += 1
                if result.status != "skipped":
                    summary.sampled_records += 1
                    summary.audited_records += 1
                    if result.size_bytes:
                        summary.total_bytes += result.size_bytes
                if result.status == "ok":
                    summary.ok_count += 1
                elif result.status == "mismatch":
                    summary.mismatch_count += 1
                    summary.mismatches.append(result.to_dict())
                elif result.status == "missing":
                    summary.missing_count += 1
                    summary.missing.append(result.to_dict())
                elif result.status in {"error", "head_only_unverified"}:
                    summary.error_count += 1
                elif result.status == "skipped":
                    summary.skipped_count += 1

                if created_at and (max_cursor is None or created_at > max_cursor):
                    max_cursor = created_at

                if fail_on_mismatch and result.status == "mismatch":
                    summary.next_cursor = max_cursor.isoformat() if max_cursor else None
                    summary.end_time = datetime.utcnow().isoformat() + "Z"
                    summary.duration_seconds = max(0.000001, time.monotonic() - start_monotonic)
                    return summary

        summary.next_cursor = max_cursor.isoformat() if max_cursor else None
        summary.end_time = datetime.utcnow().isoformat() + "Z"
        summary.duration_seconds = max(0.000001, time.monotonic() - start_monotonic)
        return summary


def parse_args(argv: List[str]):
    parser = argparse.ArgumentParser(description="artifact audit")
    parser.add_argument("--table", default="all")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--sample-rate", type=float, default=1.0)
    parser.add_argument("--max-bytes-per-sec", type=int, default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--fail-on-mismatch", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--head-only", action="store_true")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--since", type=str, default=None)
    parser.add_argument("--prefix", type=str, default=None)
    return parser.parse_args(argv)
