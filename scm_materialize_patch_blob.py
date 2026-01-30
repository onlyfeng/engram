#!/usr/bin/env python3
"""
scm_materialize_patch_blob - Patch Blob 物化工具（简化实现）
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

from engram.logbook.hashing import sha256 as compute_sha256


class MaterializeStatus(str, Enum):
    PENDING = "pending"
    MATERIALIZED = "materialized"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNREACHABLE = "unreachable"


class ErrorCategory(str, Enum):
    VALIDATION_ERROR = "validation_error"
    UNKNOWN = "unknown"


class ShaMismatchPolicy(str, Enum):
    STRICT = "strict"
    MIRROR = "mirror"


@dataclass
class PatchBlobRecord:
    blob_id: int
    source_type: str
    source_id: str
    uri: Optional[str]
    sha256: str
    size_bytes: Optional[int] = None
    format: str = "diff"
    meta_json: Optional[Dict[str, Any]] = None


@dataclass
class MaterializeResult:
    blob_id: int
    status: MaterializeStatus
    uri: Optional[str] = None
    sha256: Optional[str] = None
    size_bytes: Optional[int] = None
    error: Optional[str] = None
    error_category: Optional[ErrorCategory] = None
    status_code: Optional[int] = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--blob-id", type=int)
    parser.add_argument("--attachment-id", type=int)
    parser.add_argument("--kind")
    parser.add_argument("--materialize-missing", action="store_true")
    parser.add_argument("--source-type", choices=["git", "svn"])
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args()


def get_repo_info(*_args, **_kwargs) -> dict:
    raise NotImplementedError()


def get_gitlab_config(*_args, **_kwargs) -> dict:
    raise NotImplementedError()


def create_gitlab_token_provider(*_args, **_kwargs):
    raise NotImplementedError()


def fetch_gitlab_commit_diff(*_args, **_kwargs) -> str:
    raise NotImplementedError()


def fetch_svn_diff(*_args, **_kwargs) -> str:
    raise NotImplementedError()


def get_git_commit_meta(*_args, **_kwargs) -> dict:
    return {}


def get_svn_revision_meta(*_args, **_kwargs) -> dict:
    return {}


def mark_blob_in_progress(*_args, **_kwargs) -> bool:
    return True


def mark_blob_done(*_args, **_kwargs) -> bool:
    return True


def mark_blob_failed(*_args, **_kwargs) -> bool:
    return True


def write_text_artifact(*_args, **_kwargs) -> dict:
    raise NotImplementedError()


def generate_artifact_uri(
    source_type: str,
    repo_id: str,
    rev_or_sha: str,
    sha256: str,
    patch_format: str = "diff",
    project_key: str = "default",
    ext: Optional[str] = None,
) -> str:
    ext = ext or patch_format
    if source_type == "svn":
        if rev_or_sha.startswith("r"):
            rev = rev_or_sha
        else:
            if not rev_or_sha.isdigit():
                raise ValueError("SVN rev_or_sha 格式错误，应为数字或以 r 开头")
            rev = f"r{rev_or_sha}"
        return f"scm/{project_key}/{repo_id}/svn/{rev}/{sha256}.{ext}"
    if source_type == "git":
        if len(rev_or_sha) < 7:
            raise ValueError("Git/GitLab rev_or_sha 格式错误：至少 7 位")
        if not all(c in "0123456789abcdefABCDEF" for c in rev_or_sha):
            raise ValueError("Git/GitLab rev_or_sha 格式错误：必须为十六进制")
        return f"scm/{project_key}/{repo_id}/git/{rev_or_sha}/{sha256}.{ext}"
    raise ValueError("source_type 仅支持 svn 或 git")


def _build_diffstat(diff_content: str) -> str:
    files = set()
    additions = 0
    deletions = 0
    for line in diff_content.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            parts = line.split()
            if len(parts) >= 2:
                files.add(parts[1])
        if line.startswith("+") and not line.startswith("+++"):
            additions += 1
        if line.startswith("-") and not line.startswith("---"):
            deletions += 1
    file_count = max(len(files), 1)
    return f"{file_count} file(s) changed, {additions} insertion(s)(+), {deletions} deletion(s)(-)"


def _extract_rev_or_sha(source_id: str) -> str:
    if not source_id:
        return ""
    return source_id.split(":", 1)[-1]


def _build_ministat_from_stats(stats: dict, source_id: Optional[str] = None) -> str:
    total = int(stats.get("total", 0) or 0)
    additions = int(stats.get("additions", 0) or 0)
    deletions = int(stats.get("deletions", 0) or 0)
    short_sha = (_extract_rev_or_sha(source_id) or "unknown")[:7]
    return (
        f"ministat [{short_sha}] degraded: "
        f"{total} file(s) changed, {additions} insertion(s)(+), {deletions} deletion(s)(-)"
    )


def _build_ministat_from_changed_paths(
    changed_paths: list, revision: Optional[str] = None
) -> str:
    paths = list(changed_paths)
    if not paths:
        return ""
    added = sum(1 for p in paths if p.get("action") == "A")
    modified = sum(1 for p in paths if p.get("action") == "M")
    deleted = sum(1 for p in paths if p.get("action") == "D")
    total = len(paths)
    header = "ministat (degraded)"
    if revision:
        header += f" r{revision}"
    lines = [
        header,
        f"{total} path(s) changed, {modified} modified, {added} added, {deleted} deleted",
    ]
    return "\n".join(lines)


def materialize_blob(
    conn,
    record: PatchBlobRecord,
    config=None,
    *,
    on_sha_mismatch: ShaMismatchPolicy = ShaMismatchPolicy.STRICT,
) -> MaterializeResult:
    mark_blob_in_progress(conn, record.blob_id)
    repo_info = get_repo_info(conn, record.source_id)

    if record.source_type == "git":
        diff_content = fetch_gitlab_commit_diff(repo_info, record.source_id)
    else:
        diff_content = fetch_svn_diff(repo_info, record.source_id)

    diff_content = diff_content or ""

    if record.format == "diffstat":
        payload = _build_diffstat(diff_content)
        ext = "diffstat"
    elif record.format == "ministat":
        if record.source_type == "git":
            meta = get_git_commit_meta(repo_info, record.source_id) or {}
            payload = _build_ministat_from_stats(meta.get("stats", {}), record.source_id)
        else:
            meta = get_svn_revision_meta(repo_info, record.source_id) or {}
            revision = _extract_rev_or_sha(record.source_id)
            payload = _build_ministat_from_changed_paths(
                meta.get("changed_paths", []), revision
            )
        ext = "ministat"
    else:
        payload = diff_content
        ext = "diff"

    actual_sha256 = compute_sha256(payload.encode("utf-8"))

    if record.sha256 and record.sha256 != actual_sha256:
        if on_sha_mismatch == ShaMismatchPolicy.STRICT:
            mark_blob_failed(
                conn,
                record.blob_id,
                error="SHA256 不匹配",
                actual_sha256=actual_sha256,
                mirror_uri=None,
            )
            return MaterializeResult(
                blob_id=record.blob_id,
                status=MaterializeStatus.FAILED,
                error="SHA256 不匹配",
                error_category=ErrorCategory.VALIDATION_ERROR,
            )

        write_result = write_text_artifact(repo_info, payload, ext)
        mark_blob_failed(
            conn,
            record.blob_id,
            error="SHA256 不匹配",
            actual_sha256=actual_sha256,
            mirror_uri=write_result.get("uri"),
        )
        return MaterializeResult(
            blob_id=record.blob_id,
            status=MaterializeStatus.FAILED,
            uri=write_result.get("uri"),
            sha256=actual_sha256,
            size_bytes=write_result.get("size_bytes"),
            error="SHA256 不匹配",
            error_category=ErrorCategory.VALIDATION_ERROR,
        )

    write_result = write_text_artifact(repo_info, payload, ext)
    mark_blob_done(conn, record.blob_id, uri=write_result.get("uri"))
    return MaterializeResult(
        blob_id=record.blob_id,
        status=MaterializeStatus.MATERIALIZED,
        uri=write_result.get("uri"),
        sha256=write_result.get("sha256"),
        size_bytes=write_result.get("size_bytes"),
    )


def get_blobs_by_attachment_id(conn, attachment_id: int):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM attachments WHERE attachment_id = %s", (attachment_id,))
        attachment = cur.fetchone()
        if not attachment:
            return []
        sha = attachment.get("sha256") if isinstance(attachment, dict) else None
        cur.execute("SELECT * FROM patch_blobs WHERE sha256 = %s", (sha,))
        return cur.fetchall() or []


def get_blobs_by_attachment_kind(conn, kind: str):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM patch_blobs WHERE format = %s", (kind,))
        return cur.fetchall() or []


def materialize_patch_blobs(*_args, **_kwargs):
    return []


def main() -> None:
    parse_args()


if __name__ == "__main__":
    main()
