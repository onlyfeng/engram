#!/usr/bin/env python3
"""
artifact_gc - 制品垃圾回收（测试兼容实现）

注意: 此文件已移动到 scripts/ 目录。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

# 确保根目录在 sys.path 中，以支持导入根目录模块
_ROOT_DIR = Path(__file__).resolve().parent.parent
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

from engram.logbook.artifact_ops_audit import (
    write_gc_delete_audit_event,
    write_gc_summary_audit_event,
)
from engram.logbook.artifact_store import FileUriStore, LocalArtifactsStore
from engram.logbook.uri import PhysicalRef, parse_physical_uri
from engram.logbook.config import get_gc_require_ops_default, get_gc_require_trash_default


class GCError(Exception):
    pass


class GCDatabaseError(GCError):
    pass


class GCPrefixError(GCError):
    pass


class GCOpsCredentialsRequiredError(GCError):
    pass


@dataclass
class GCCandidate:
    uri: str
    full_path: str
    size_bytes: int
    age_days: float
    status: str = "pending"
    error: Optional[str] = None


@dataclass
class ReferencedUris:
    artifact_keys: Set[str]
    physical_refs: List[PhysicalRef]

    def has_physical_ref_for_key(
        self,
        *,
        artifact_key: str,
        store_bucket: str,
        store_prefix: str,
    ) -> bool:
        normalized_prefix = store_prefix or ""
        for ref in self.physical_refs:
            if ref.scheme not in {"s3", "gs"}:
                continue
            if ref.bucket != store_bucket:
                continue
            if normalized_prefix:
                if not ref.key.startswith(normalized_prefix):
                    continue
                key_without_prefix = ref.key[len(normalized_prefix):]
                if key_without_prefix == artifact_key:
                    return True
            else:
                if ref.key == artifact_key:
                    return True
        return False


@dataclass
class GCResult:
    gc_mode: str = "orphan"
    backend: str = "local"
    bucket: Optional[str] = None
    prefix: str = ""
    scanned_count: int = 0
    referenced_count: int = 0
    protected_count: int = 0
    candidates_count: int = 0
    skipped_by_age: int = 0
    deleted_count: int = 0
    trashed_count: int = 0
    failed_count: int = 0
    total_size_bytes: int = 0
    deleted_size_bytes: int = 0
    status_summary: Dict[str, int] = field(
        default_factory=lambda: {"ok": 0, "skipped": 0, "error": 0, "pending": 0}
    )
    errors: List[str] = field(default_factory=list)
    candidates: List[GCCandidate] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        status_summary = self.status_summary or {"ok": 0, "skipped": 0, "error": 0, "pending": 0}
        errors = self.errors or []
        candidates = self.candidates or []
        return {
            "gc_mode": self.gc_mode,
            "backend": self.backend,
            "bucket": self.bucket,
            "prefix": self.prefix,
            "scanned_count": self.scanned_count,
            "referenced_count": self.referenced_count,
            "protected_count": self.protected_count,
            "candidates_count": self.candidates_count,
            "skipped_by_age": self.skipped_by_age,
            "deleted_count": self.deleted_count,
            "trashed_count": self.trashed_count,
            "failed_count": self.failed_count,
            "total_size_bytes": self.total_size_bytes,
            "deleted_size_bytes": self.deleted_size_bytes,
            "status_summary": status_summary,
            "errors": errors,
            "candidates": [c.__dict__ for c in candidates],
        }


def _normalize_path(path: str) -> str:
    normalized = path.strip().strip("/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def _normalize_uri_for_gc(uri: str) -> Tuple[Any, Optional[str]]:
    if not uri:
        return "", None
    if uri.startswith("memory://"):
        return "", None
    parsed = parse_physical_uri(uri)
    if parsed:
        return parsed, "physical_uri"
    if uri.startswith("artifact://"):
        uri = uri[len("artifact://"):]
    return _normalize_path(uri), "artifact_key"


def _validate_prefix(prefix: str, allowed_prefixes: Optional[List[str]] = None) -> None:
    if not prefix:
        raise GCPrefixError("前缀不能为空")
    if allowed_prefixes is not None:
        if not allowed_prefixes:
            raise GCPrefixError("allowed_prefixes 空列表")
        if prefix not in allowed_prefixes:
            raise GCPrefixError("prefix 不在允许范围内")


def scan_local_artifacts(
    store: LocalArtifactsStore,
    prefix: str,
    *,
    allowed_prefixes: Optional[List[str]] = None,
) -> List[Tuple[str, str, int, float]]:
    _validate_prefix(prefix, allowed_prefixes)
    root = Path(store.root)
    base = root / prefix
    files: List[Tuple[str, str, int, float]] = []
    if not base.exists():
        return files
    for path in base.rglob("*"):
        if path.is_dir():
            continue
        name = path.name
        if name.startswith(".") or name.endswith(".tmp"):
            continue
        rel = path.relative_to(root).as_posix()
        stat = path.stat()
        files.append((rel, str(path), stat.st_size, stat.st_mtime))
    return files


def scan_file_uri_artifacts(
    store: FileUriStore,
    prefix: str,
    *,
    allowed_prefixes: Optional[List[str]] = None,
) -> List[Tuple[str, str, int, float]]:
    _validate_prefix(prefix, allowed_prefixes)
    roots = getattr(store, "_allowed_roots", None)
    if roots is None:
        raise GCPrefixError("allowed_roots 未配置")
    if not roots:
        raise GCPrefixError("allowed_roots 为空")

    results: List[Tuple[str, str, int, float]] = []
    for root in roots:
        base = Path(root) / prefix
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.is_dir():
                continue
            name = path.name
            if name.startswith(".") or name.endswith(".tmp"):
                continue
            stat = path.stat()
            file_uri = store._ensure_file_uri(str(path))
            results.append((file_uri, str(path), stat.st_size, stat.st_mtime))
    return results


def delete_local_file(
    file_path: str,
    *,
    trash_prefix: Optional[str] = None,
    artifacts_root: Optional[Path] = None,
) -> Tuple[bool, Optional[str]]:
    path = Path(file_path)
    if not path.exists():
        return True, None
    try:
        if trash_prefix:
            if artifacts_root is None:
                raise ValueError("artifacts_root is required for trash delete")
            rel = path.relative_to(artifacts_root)
            target = artifacts_root / trash_prefix / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(target))
            return True, None
        path.unlink()
        return True, None
    except Exception as exc:
        return False, str(exc)


def delete_file_uri_file(
    store: FileUriStore,
    file_uri: str,
    *,
    trash_prefix: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    file_path = store._parse_file_uri(file_uri)
    artifacts_root: Optional[Path] = None
    roots = getattr(store, "_allowed_roots", None) or []
    for root in roots:
        root_path = Path(root)
        try:
            file_path.relative_to(root_path)
            artifacts_root = root_path
            break
        except ValueError:
            continue
    return delete_local_file(
        str(file_path),
        trash_prefix=trash_prefix,
        artifacts_root=artifacts_root,
    )


def get_referenced_uris(
    *,
    dsn: Optional[str] = None,
    prefix: Optional[str] = None,
    search_path: Optional[List[str]] = None,
) -> ReferencedUris:
    if not dsn:
        dsn = os.environ.get("POSTGRES_DSN") or os.environ.get("TEST_PG_DSN")
    if not dsn:
        raise GCDatabaseError("未配置 POSTGRES_DSN")
    from engram_logbook.db import get_connection
    from engram.logbook.errors import DbConnectionError

    search_path = search_path or ["scm", "logbook", "public"]
    try:
        conn = get_connection(dsn=dsn, autocommit=True, search_path=search_path)
    except DbConnectionError as exc:
        raise GCDatabaseError(f"数据库连接失败: {exc}") from exc
    try:
        artifact_keys: Set[str] = set()
        physical_refs: List[PhysicalRef] = []
        normalized_prefix = _normalize_path(prefix) if prefix else None
        try:
            with conn.cursor() as cur:
                sql = "SELECT uri FROM scm.patch_blobs WHERE uri IS NOT NULL"
                params = None
                if normalized_prefix:
                    sql += " AND uri LIKE %s"
                    params = (f"{normalized_prefix}%",)
                cur.execute(sql, params)
                for (uri,) in cur.fetchall():
                    normalized, uri_type = _normalize_uri_for_gc(uri)
                    if uri_type == "artifact_key":
                        artifact_keys.add(normalized)
                    elif uri_type == "physical_uri":
                        physical_refs.append(normalized)

                sql = "SELECT uri FROM logbook.attachments WHERE uri IS NOT NULL"
                params = None
                if normalized_prefix:
                    sql += " AND uri LIKE %s"
                    params = (f"%{normalized_prefix}%",)
                cur.execute(sql, params)
                for (uri,) in cur.fetchall():
                    normalized, uri_type = _normalize_uri_for_gc(uri)
                    if uri_type == "artifact_key":
                        artifact_keys.add(normalized)
                    elif uri_type == "physical_uri":
                        physical_refs.append(normalized)
        except Exception as exc:
            raise GCDatabaseError(f"查询失败: {exc}") from exc
        return ReferencedUris(artifact_keys=artifact_keys, physical_refs=physical_refs)
    finally:
        conn.close()


def _check_ops_required(
    *,
    backend: str,
    require_ops: Optional[bool],
    dry_run: bool,
    delete: bool,
) -> None:
    if backend != "object":
        return
    if not delete or dry_run:
        return
    if require_ops is None:
        require_ops = get_gc_require_ops_default()
    if require_ops:
        use_ops = os.environ.get("ENGRAM_S3_USE_OPS", "false").lower() in ("1", "true", "yes")
        if not use_ops:
            raise GCOpsCredentialsRequiredError("需要 ops 凭证执行删除")


def _check_trash_required(
    *,
    delete: bool,
    dry_run: bool,
    trash_prefix: Optional[str],
    require_trash: Optional[bool],
) -> None:
    if not delete or dry_run:
        return
    if trash_prefix:
        return
    if require_trash is None:
        require_trash = get_gc_require_trash_default()
    if require_trash:
        raise GCPrefixError("require_trash=true 时必须使用 trash_prefix")


def _build_status_summary(candidates: List[GCCandidate]) -> Dict[str, int]:
    summary = {"ok": 0, "skipped": 0, "error": 0, "pending": 0}
    for c in candidates:
        if c.status in summary:
            summary[c.status] += 1
    return summary


def run_gc(
    *,
    prefix: str,
    dry_run: bool = True,
    delete: bool = False,
    dsn: Optional[str] = None,
    backend: str = "local",
    artifacts_root: Optional[str] = None,
    allowed_prefixes: Optional[List[str]] = None,
    older_than_days: Optional[int] = None,
    trash_prefix: Optional[str] = None,
    limit: Optional[int] = None,
    require_ops: Optional[bool] = None,
    require_trash: Optional[bool] = None,
    audit: bool = False,
    verbose: bool = False,
) -> GCResult:
    _validate_prefix(prefix, allowed_prefixes)
    _check_ops_required(backend=backend, require_ops=require_ops, dry_run=dry_run, delete=delete)
    _check_trash_required(
        delete=delete,
        dry_run=dry_run,
        trash_prefix=trash_prefix,
        require_trash=require_trash,
    )

    bucket = os.environ.get("ENGRAM_S3_BUCKET") if backend == "object" else None

    if backend == "local":
        if not artifacts_root:
            raise GCPrefixError("artifacts_root 不能为空")
        store = LocalArtifactsStore(root=Path(artifacts_root))
        scanned = scan_local_artifacts(store, prefix, allowed_prefixes=allowed_prefixes)
    elif backend == "file":
        if not artifacts_root:
            raise GCPrefixError("artifacts_root 不能为空")
        store = FileUriStore(allowed_roots=[artifacts_root])
        scanned = scan_file_uri_artifacts(store, prefix, allowed_prefixes=allowed_prefixes)
    else:
        if not artifacts_root:
            artifacts_root = os.getcwd()
        store = LocalArtifactsStore(root=Path(artifacts_root))
        scanned = scan_local_artifacts(store, prefix, allowed_prefixes=allowed_prefixes)

    if limit:
        scanned = scanned[:limit]

    total_size = sum(item[2] for item in scanned)
    referenced_count = 0
    protected_count = 0
    skipped_by_age = 0
    candidates: List[GCCandidate] = []
    deleted_count = 0
    trashed_count = 0
    failed_count = 0
    deleted_size_bytes = 0
    errors: List[str] = []

    try:
        referenced = get_referenced_uris(dsn=dsn, prefix=prefix) if dsn else ReferencedUris(set(), [])
        referenced_count = len(referenced.artifact_keys)
    except GCDatabaseError:
        referenced = None
        errors.append("db_unavailable")

    for uri, full_path, size_bytes, mtime in scanned:
        age_days = (time.time() - mtime) / 86400.0
        if older_than_days is not None and age_days < older_than_days:
            skipped_by_age += 1
            continue
        if referenced is None:
            protected_count += 1
            continue
        normalized, uri_type = _normalize_uri_for_gc(uri)
        is_referenced = False
        if uri_type == "artifact_key":
            if normalized in referenced.artifact_keys:
                is_referenced = True
        if backend == "object" and uri_type == "artifact_key" and bucket:
            if referenced.has_physical_ref_for_key(
                artifact_key=normalized,
                store_bucket=bucket,
                store_prefix="",
            ):
                is_referenced = True
        if is_referenced:
            protected_count += 1
            continue

        candidates.append(
            GCCandidate(
                uri=uri,
                full_path=full_path,
                size_bytes=size_bytes,
                age_days=age_days,
                status="pending",
            )
        )

    if delete and not dry_run:
        for candidate in candidates:
            if backend in {"local", "object"}:
                success, error = delete_local_file(
                    candidate.full_path,
                    trash_prefix=trash_prefix,
                    artifacts_root=Path(artifacts_root) if artifacts_root else None,
                )
            else:
                file_uri = f"file://{candidate.full_path}"
                store = FileUriStore(allowed_roots=[artifacts_root] if artifacts_root else None)
                success, error = delete_file_uri_file(store, file_uri, trash_prefix=trash_prefix)

            if success:
                candidate.status = "ok"
                if trash_prefix:
                    trashed_count += 1
                else:
                    deleted_count += 1
                    deleted_size_bytes += candidate.size_bytes
            else:
                candidate.status = "error"
                candidate.error = error
                failed_count += 1
                errors.append(error or "delete_failed")

            if audit:
                write_gc_delete_audit_event(
                    uri=candidate.uri,
                    backend=backend,
                    success=success,
                    error=error,
                    size_bytes=candidate.size_bytes,
                    age_days=candidate.age_days,
                    require_ops=None if backend == "local" else require_ops,
                    dry_run=dry_run,
                )
    else:
        for candidate in candidates:
            candidate.status = "pending"

    status_summary = _build_status_summary(candidates)
    result = GCResult(
        gc_mode="orphan",
        backend=backend,
        bucket=bucket,
        prefix=prefix,
        scanned_count=len(scanned),
        referenced_count=referenced_count,
        protected_count=protected_count,
        candidates_count=len(candidates),
        skipped_by_age=skipped_by_age,
        deleted_count=deleted_count,
        trashed_count=trashed_count,
        failed_count=failed_count,
        total_size_bytes=total_size,
        deleted_size_bytes=deleted_size_bytes,
        status_summary=status_summary,
        errors=errors,
        candidates=candidates,
    )

    if audit and delete and not dry_run:
        write_gc_summary_audit_event(
            gc_mode="orphan",
            backend=backend,
            prefix=prefix,
            bucket=bucket,
            scanned=len(scanned),
            candidates=len(candidates),
            deleted=deleted_count,
            failed=failed_count,
            trashed=trashed_count,
            skipped_by_age=skipped_by_age,
            require_ops=None if backend == "local" else require_ops,
            dry_run=dry_run,
        )

    return result


def run_tmp_gc(
    *,
    tmp_prefix: str,
    older_than_days: Optional[int],
    dry_run: bool = True,
    delete: bool = False,
    backend: str = "local",
    artifacts_root: Optional[str] = None,
    trash_prefix: Optional[str] = None,
    require_ops: Optional[bool] = None,
    audit: bool = False,
    verbose: bool = False,
) -> GCResult:
    if not tmp_prefix:
        raise GCPrefixError("tmp_prefix 前缀不能为空")
    if older_than_days is None:
        raise GCPrefixError("older-than-days 不能为空")
    _check_ops_required(backend=backend, require_ops=require_ops, dry_run=dry_run, delete=delete)

    if backend == "object" and not artifacts_root:
        artifacts_root = os.getcwd()

    if backend == "local":
        if not artifacts_root:
            raise GCPrefixError("artifacts_root 不能为空")
        store = LocalArtifactsStore(root=Path(artifacts_root))
        scanned = scan_local_artifacts(store, tmp_prefix)
    else:
        if not artifacts_root:
            raise GCPrefixError("artifacts_root 不能为空")
        store = LocalArtifactsStore(root=Path(artifacts_root))
        scanned = scan_local_artifacts(store, tmp_prefix)

    total_size = sum(item[2] for item in scanned)
    candidates: List[GCCandidate] = []
    deleted_count = 0
    trashed_count = 0
    failed_count = 0
    deleted_size_bytes = 0
    skipped_by_age = 0
    errors: List[str] = []

    for uri, full_path, size_bytes, mtime in scanned:
        age_days = (time.time() - mtime) / 86400.0
        if age_days < older_than_days:
            skipped_by_age += 1
            continue
        candidates.append(
            GCCandidate(
                uri=uri,
                full_path=full_path,
                size_bytes=size_bytes,
                age_days=age_days,
                status="pending",
            )
        )

    if delete and not dry_run:
        for candidate in candidates:
            success, error = delete_local_file(
                candidate.full_path,
                trash_prefix=trash_prefix,
                artifacts_root=Path(artifacts_root) if artifacts_root else None,
            )
            if success:
                candidate.status = "ok"
                if trash_prefix:
                    trashed_count += 1
                else:
                    deleted_count += 1
                    deleted_size_bytes += candidate.size_bytes
            else:
                candidate.status = "error"
                candidate.error = error
                failed_count += 1
                errors.append(error or "delete_failed")

            if audit:
                write_gc_delete_audit_event(
                    uri=candidate.uri,
                    backend=backend,
                    success=success,
                    error=error,
                    size_bytes=candidate.size_bytes,
                    age_days=candidate.age_days,
                    require_ops=None if backend == "local" else require_ops,
                    dry_run=dry_run,
                )
    else:
        for candidate in candidates:
            candidate.status = "pending"

    status_summary = _build_status_summary(candidates)
    result = GCResult(
        gc_mode="tmp",
        backend=backend,
        bucket=os.environ.get("ENGRAM_S3_BUCKET") if backend == "object" else None,
        prefix=tmp_prefix,
        scanned_count=len(scanned),
        referenced_count=0,
        protected_count=0,
        candidates_count=len(candidates),
        skipped_by_age=skipped_by_age,
        deleted_count=deleted_count,
        trashed_count=trashed_count,
        failed_count=failed_count,
        total_size_bytes=total_size,
        deleted_size_bytes=deleted_size_bytes,
        status_summary=status_summary,
        errors=errors,
        candidates=candidates,
    )

    if audit and delete and not dry_run:
        write_gc_summary_audit_event(
            gc_mode="tmp",
            backend=backend,
            prefix=tmp_prefix,
            bucket=result.bucket,
            scanned=len(scanned),
            candidates=len(candidates),
            deleted=deleted_count,
            failed=failed_count,
            trashed=trashed_count,
            skipped_by_age=skipped_by_age,
            require_ops=None if backend == "local" else require_ops,
            dry_run=dry_run,
        )

    return result
