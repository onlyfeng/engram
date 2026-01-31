#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
engram_logbook.scm_integrity_check - Patch blob 完整性检查
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .artifact_store import get_default_store
from .uri import parse_evidence_uri


def artifact_exists(uri: str) -> bool:
    """检查制品是否存在（可被测试 mock）"""
    store = get_default_store()
    return store.exists(uri)


def get_artifact_info(uri: str) -> Dict[str, Any]:
    """获取制品元数据（可被测试 mock）"""
    store = get_default_store()
    return store.get_info(uri)


@dataclass
class PatchBlobIssue:
    blob_id: int
    source_type: str
    source_id: str
    uri: Optional[str]
    evidence_uri: Optional[str]
    sha256: Optional[str]
    issue_type: str
    details: Optional[str] = None


@dataclass
class IntegrityCheckResult:
    patch_blob_issues: List[PatchBlobIssue] = field(default_factory=list)

    @property
    def has_issues(self) -> bool:
        return len(self.patch_blob_issues) > 0

    @property
    def issue_count(self) -> int:
        return len(self.patch_blob_issues)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "patch_blob_issues": [
                {
                    "blob_id": issue.blob_id,
                    "source_type": issue.source_type,
                    "source_id": issue.source_id,
                    "uri": issue.uri,
                    "evidence_uri": issue.evidence_uri,
                    "sha256": issue.sha256,
                    "issue_type": issue.issue_type,
                    "details": issue.details,
                }
                for issue in self.patch_blob_issues
            ]
        }


def _normalize_row(row: Any) -> Dict[str, Any]:
    if isinstance(row, dict):
        return row
    return {
        "blob_id": row[0],
        "source_type": row[1],
        "source_id": row[2],
        "uri": row[3],
        "evidence_uri": row[4],
        "sha256": row[5],
    }


def check_patch_blobs(
    conn,
    *,
    check_artifacts: bool = False,
    verify_sha256: bool = False,
    verify_limit: Optional[int] = None,
) -> List[PatchBlobIssue]:
    """
    检查 patch_blobs 记录的 evidence_uri 与制品一致性。
    """
    query = """
        SELECT blob_id, source_type, source_id, uri, evidence_uri, sha256
        FROM scm.patch_blobs
        ORDER BY blob_id
    """.strip()
    if verify_limit is not None:
        query += f" LIMIT {int(verify_limit)}"

    with conn.cursor() as cur:
        cur.execute(query)
        rows = cur.fetchall()

    issues: List[PatchBlobIssue] = []
    for row in rows:
        item = _normalize_row(row)
        blob_id = item.get("blob_id")
        source_type = item.get("source_type")
        source_id = item.get("source_id")
        uri = item.get("uri")
        evidence_uri = item.get("evidence_uri")
        sha256 = item.get("sha256")

        if not evidence_uri:
            issues.append(
                PatchBlobIssue(
                    blob_id=blob_id,
                    source_type=source_type,
                    source_id=source_id,
                    uri=uri,
                    evidence_uri=evidence_uri,
                    sha256=sha256,
                    issue_type="missing_evidence_uri",
                )
            )
            continue

        parsed = parse_evidence_uri(evidence_uri)
        if not parsed:
            issues.append(
                PatchBlobIssue(
                    blob_id=blob_id,
                    source_type=source_type,
                    source_id=source_id,
                    uri=uri,
                    evidence_uri=evidence_uri,
                    sha256=sha256,
                    issue_type="invalid_evidence_uri",
                )
            )
            continue

        if not check_artifacts:
            continue

        if not uri:
            issues.append(
                PatchBlobIssue(
                    blob_id=blob_id,
                    source_type=source_type,
                    source_id=source_id,
                    uri=uri,
                    evidence_uri=evidence_uri,
                    sha256=sha256,
                    issue_type="uri_not_resolvable",
                )
            )
            continue

        if not artifact_exists(uri):
            issues.append(
                PatchBlobIssue(
                    blob_id=blob_id,
                    source_type=source_type,
                    source_id=source_id,
                    uri=uri,
                    evidence_uri=evidence_uri,
                    sha256=sha256,
                    issue_type="artifact_not_found",
                )
            )
            continue

        if verify_sha256:
            info = get_artifact_info(uri)
            actual_sha256 = info.get("sha256")
            if sha256 and actual_sha256 and sha256 != actual_sha256:
                issues.append(
                    PatchBlobIssue(
                        blob_id=blob_id,
                        source_type=source_type,
                        source_id=source_id,
                        uri=uri,
                        evidence_uri=evidence_uri,
                        sha256=sha256,
                        issue_type="sha256_mismatch",
                        details=f"DB sha256={sha256}, artifact sha256={actual_sha256}",
                    )
                )

    return issues


__all__ = [
    "PatchBlobIssue",
    "IntegrityCheckResult",
    "check_patch_blobs",
    "artifact_exists",
    "get_artifact_info",
]
