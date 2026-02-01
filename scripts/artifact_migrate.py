#!/usr/bin/env python3
"""
artifact_migrate - 制品迁移工具（测试兼容实现）
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from engram.logbook.artifact_store import (
    LocalArtifactsStore,
    ObjectStore,
)


class MigrationError(Exception):
    pass


class MigrationVerifyError(MigrationError):
    pass


class MigrationDbUpdateError(MigrationError):
    pass


class MigrationOpsCredentialsRequiredError(MigrationError):
    pass


DB_UPDATE_MODE_TO_ARTIFACT_KEY = "to_artifact_key"


@dataclass
class MigrationItem:
    key: str
    source_uri: str
    target_uri: Optional[str] = None
    source_sha256: Optional[str] = None
    target_sha256: Optional[str] = None
    size_bytes: Optional[int] = None
    status: str = "pending"  # pending|migrated|verified|failed|skipped
    error: Optional[str] = None


@dataclass
class MigrationResult:
    scanned_count: int = 0
    migrated_count: int = 0
    verified_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    deleted_count: int = 0
    trashed_count: int = 0
    total_size_bytes: int = 0
    migrated_size_bytes: int = 0
    duration_seconds: float = 0.0
    dry_run: bool = True
    errors: List[Dict[str, Any]] = field(default_factory=list)
    items: List[MigrationItem] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        error_count = len(self.errors)
        errors = self.errors[:100] if error_count > 100 else list(self.errors)
        return {
            "scanned_count": self.scanned_count,
            "migrated_count": self.migrated_count,
            "verified_count": self.verified_count,
            "failed_count": self.failed_count,
            "skipped_count": self.skipped_count,
            "deleted_count": self.deleted_count,
            "trashed_count": self.trashed_count,
            "total_size_bytes": self.total_size_bytes,
            "migrated_size_bytes": self.migrated_size_bytes,
            "duration_seconds": self.duration_seconds,
            "dry_run": self.dry_run,
            "error_count": error_count,
            "errors": errors,
        }


@dataclass
class DbUpdatePreview:
    patch_blobs_count: int
    attachments_count: int
    converted_count: int


class ArtifactMigrator:
    def __init__(
        self,
        *,
        source_store,
        target_store,
        source_backend: str,
        target_backend: str,
        prefix: Optional[str] = None,
        dry_run: bool = True,
        verify: bool = False,
        limit: Optional[int] = None,
        update_db: bool = False,
        db_update_mode: Optional[str] = None,
        delete_source: bool = False,
        trash_prefix: Optional[str] = None,
        require_ops: Optional[bool] = None,
        workers: int = 1,
        concurrency: Optional[int] = None,
    ) -> None:
        self.source_store = source_store
        self.target_store = target_store
        self.source_backend = source_backend
        self.target_backend = target_backend
        self.prefix = prefix
        self.dry_run = dry_run
        self.verify = verify
        self.limit = limit
        self.update_db = update_db
        self.db_update_mode = db_update_mode
        self.delete_source = delete_source
        self.trash_prefix = trash_prefix
        self.require_ops = require_ops
        effective_workers = concurrency if concurrency is not None else workers
        self.workers = max(1, int(effective_workers))
        self._conn = None

        if (
            self.source_backend == "object"
            and self.delete_source
            and self.require_ops
        ):
            use_ops = os.environ.get("ENGRAM_S3_USE_OPS", "false").lower() in ("1", "true", "yes")
            if not use_ops:
                raise MigrationOpsCredentialsRequiredError("需要 ops 凭证执行删除")

    def scan_source(self) -> Iterable[MigrationItem]:
        root = getattr(self.source_store, "root", None)
        if not root:
            return []
        base = Path(root)
        items: List[MigrationItem] = []
        for path in base.rglob("*"):
            if path.is_dir():
                continue
            rel = path.relative_to(base).as_posix()
            if self.prefix and not rel.startswith(self.prefix):
                continue
            items.append(MigrationItem(key=rel, source_uri=rel))
            if self.limit and len(items) >= self.limit:
                break
        return items

    def _target_uri_for_key(self, key: str) -> str:
        if self.target_backend == "object":
            bucket = os.environ.get("ENGRAM_S3_BUCKET", "bucket")
            return f"s3://{bucket}/{key}"
        return key

    def migrate_item(self, item: MigrationItem) -> MigrationItem:
        if self.dry_run:
            item.status = "pending"
            return item
        try:
            content = self.source_store.get(item.source_uri)
            source_sha = hashlib.sha256(content).hexdigest()
            item.source_sha256 = source_sha

            # skip if target already exists and hash matches
            try:
                info = self.target_store.get_info(item.key)
                if self.verify and info.get("sha256") == source_sha:
                    item.status = "skipped"
                    return item
            except Exception:
                pass

            result = self.target_store.put(item.key, content)
            item.target_uri = self._target_uri_for_key(item.key)
            item.target_sha256 = result.get("sha256")
            item.size_bytes = result.get("size_bytes")
            item.status = "migrated"

            if self.verify:
                if item.target_sha256 != source_sha:
                    item.status = "failed"
                    item.error = "sha256 mismatch"
                else:
                    item.status = "verified"
            return item
        except Exception as exc:
            item.status = "failed"
            item.error = str(exc)
            return item

    def run(self) -> MigrationResult:
        items = list(self.scan_source())
        start_monotonic = time.monotonic()
        result = MigrationResult(scanned_count=len(items), dry_run=self.dry_run)
        for item in items:
            migrated = self.migrate_item(item)
            result.items.append(migrated)
            if migrated.status in {"migrated", "verified"}:
                result.migrated_count += 1
            if migrated.status == "verified":
                result.verified_count += 1
            if migrated.status == "failed":
                result.failed_count += 1
                result.errors.append({"key": migrated.key, "error": migrated.error})
            if migrated.status == "skipped":
                result.skipped_count += 1
            if migrated.size_bytes:
                result.total_size_bytes += migrated.size_bytes
                if migrated.status in {"migrated", "verified"}:
                    result.migrated_size_bytes += migrated.size_bytes

            if self.delete_source and not self.dry_run and migrated.status in {"migrated", "verified"}:
                src_path = Path(getattr(self.source_store, "root")) / migrated.key
                if self.trash_prefix:
                    target = Path(getattr(self.source_store, "root")) / self.trash_prefix / migrated.key
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src_path), str(target))
                    result.trashed_count += 1
                else:
                    if src_path.exists():
                        src_path.unlink()
                        result.deleted_count += 1

        result.duration_seconds = max(0.0, time.monotonic() - start_monotonic)
        return result

    def preview_db_update(self, keys: List[str]) -> DbUpdatePreview:
        if not self._conn:
            return DbUpdatePreview(0, 0, 0)
        with self._conn.cursor() as cur:
            cur.execute("SELECT blob_id, uri FROM scm.patch_blobs WHERE uri IS NOT NULL")
            patch_rows = cur.fetchall()
            cur.execute("SELECT attachment_id, uri FROM logbook.attachments WHERE uri IS NOT NULL")
            attach_rows = cur.fetchall()
        converted = len(patch_rows) + len(attach_rows)
        return DbUpdatePreview(
            patch_blobs_count=len(patch_rows),
            attachments_count=len(attach_rows),
            converted_count=converted,
        )

    def update_db_uris(self, items: List[MigrationItem]) -> int:
        if self.dry_run or not self.update_db:
            return 0
        if not self._conn:
            return 0
        updated = 0
        try:
            with self._conn.cursor() as cur:
                for item in items:
                    if item.status not in {"migrated", "verified"}:
                        continue
                    cur.execute(
                        "UPDATE scm.patch_blobs SET uri=%s WHERE uri=%s",
                        (item.target_uri, item.source_uri),
                    )
                    cur.execute(
                        "UPDATE logbook.attachments SET uri=%s WHERE uri=%s",
                        (item.target_uri, item.source_uri),
                    )
                    updated += 2
            self._conn.commit()
        except Exception as exc:
            self._conn.rollback()
            raise MigrationDbUpdateError(str(exc))
        return updated


def create_migrator(
    *,
    source_backend: str,
    target_backend: str,
    source_root: str,
    target_root: str,
    dry_run: bool = True,
    verify: bool = False,
    require_ops: Optional[bool] = None,
    delete_source: bool = False,
) -> ArtifactMigrator:
    if source_backend == "local":
        source_store = LocalArtifactsStore(root=Path(source_root))
    else:
        source_store = ObjectStore(endpoint=os.environ.get("ENGRAM_S3_ENDPOINT"), bucket=os.environ.get("ENGRAM_S3_BUCKET"))
    if target_backend == "local":
        target_store = LocalArtifactsStore(root=Path(target_root))
    else:
        target_store = ObjectStore(endpoint=os.environ.get("ENGRAM_S3_ENDPOINT"), bucket=os.environ.get("ENGRAM_S3_BUCKET"))
    return ArtifactMigrator(
        source_store=source_store,
        target_store=target_store,
        source_backend=source_backend,
        target_backend=target_backend,
        dry_run=dry_run,
        verify=verify,
        require_ops=require_ops,
        delete_source=delete_source,
    )


def run_migration(
    *,
    source_backend: str,
    target_backend: str,
    source_root: str,
    target_root: str,
    dry_run: bool = True,
    verify: bool = False,
    require_ops: Optional[bool] = None,
    delete_source: bool = False,
) -> MigrationResult:
    migrator = create_migrator(
        source_backend=source_backend,
        target_backend=target_backend,
        source_root=source_root,
        target_root=target_root,
        dry_run=dry_run,
        verify=verify,
        require_ops=require_ops,
        delete_source=delete_source,
    )
    return migrator.run()
