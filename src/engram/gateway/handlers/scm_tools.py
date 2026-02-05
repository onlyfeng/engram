"""
scm_tools - scm_* 工具实现

提供：
- scm_patch_blob_resolve
- scm_materialize_patch_blob
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from ..di import GatewayDepsProtocol
from ..result_error_codes import ToolResultErrorCode

logger = logging.getLogger("gateway.handlers.scm_tools")


def _parse_resource_id(resource_id: str) -> Tuple[Optional[str], Optional[str]]:
    if ":" not in resource_id:
        return None, None
    source_type, source_id = resource_id.split(":", 1)
    return source_type, source_id


async def execute_scm_patch_blob_resolve(
    evidence_uri: Optional[str] = None,
    source_type: Optional[str] = None,
    source_id: Optional[str] = None,
    sha256: Optional[str] = None,
    blob_id: Optional[int] = None,
    *,
    deps: GatewayDepsProtocol,
) -> Dict[str, Any]:
    _ = deps

    if (
        not evidence_uri
        and not blob_id
        and not (source_type and source_id and sha256)
        and not sha256
    ):
        return {
            "ok": False,
            "error_code": ToolResultErrorCode.MISSING_REQUIRED_PARAMETER,
            "retryable": False,
            "message": "缺少必需参数: evidence_uri/blob_id/sha256 或 source_type+source_id+sha256",
        }

    try:
        from engram.logbook.db import get_connection
        from engram.logbook.evidence_resolver import get_evidence_info
        from engram.logbook.scm_db import get_patch_blob
        from engram.logbook.uri import build_evidence_uri
    except ImportError as import_err:
        logger.warning(f"scm_tools 依赖导入失败: {import_err}")
        return {
            "ok": False,
            "error_code": "DEPENDENCY_MISSING",
            "retryable": False,
            "message": "scm_patch_blob_resolve 依赖 engram_logbook 模块",
            "details": {"import_error": str(import_err)},
        }

    # 优先 evidence_uri
    if evidence_uri:
        info = get_evidence_info(evidence_uri)
        if info is None:
            return {
                "ok": False,
                "error_code": "NOT_FOUND",
                "retryable": False,
                "message": "未找到对应的 patch_blob",
            }
        source_type_from_id, source_id_from_id = _parse_resource_id(info.get("resource_id", ""))
        sha = info.get("sha256")
        result: Dict[str, Any] = {
            "ok": True,
            "evidence_uri": evidence_uri,
            "artifact_uri": info.get("artifact_uri"),
            "sha256": sha,
            "size_bytes": info.get("size_bytes"),
        }
        if source_type_from_id and source_id_from_id:
            result["source_type"] = source_type_from_id
            result["source_id"] = source_id_from_id

            # 尝试补齐 patch_blob 表字段
            if isinstance(sha, str):
                conn = get_connection()
                try:
                    row = get_patch_blob(
                        conn,
                        source_type_from_id,
                        source_id_from_id,
                        sha,
                    )
                    if row:
                        result.update(
                            {
                                "blob_id": row.get("blob_id"),
                                "format": row.get("format"),
                                "chunking_version": row.get("chunking_version"),
                                "meta_json": row.get("meta_json"),
                                "created_at": row.get("created_at"),
                                "updated_at": row.get("updated_at"),
                                "evidence_uri": row.get("evidence_uri") or evidence_uri,
                            }
                        )
                finally:
                    conn.close()

        return result

    # blob_id 查询
    if blob_id is not None:
        evidence_uri = f"memory://patch_blobs/blob_id/{blob_id}"
        info = get_evidence_info(evidence_uri)
        if info is None:
            return {
                "ok": False,
                "error_code": "NOT_FOUND",
                "retryable": False,
                "message": "未找到对应的 patch_blob",
            }
        source_type_from_id, source_id_from_id = _parse_resource_id(info.get("resource_id", ""))
        sha = info.get("sha256")
        result = {
            "ok": True,
            "blob_id": blob_id,
            "artifact_uri": info.get("artifact_uri"),
            "sha256": sha,
            "size_bytes": info.get("size_bytes"),
        }
        if source_type_from_id and source_id_from_id:
            result["source_type"] = source_type_from_id
            result["source_id"] = source_id_from_id
            if isinstance(sha, str):
                result["evidence_uri"] = build_evidence_uri(
                    source_type_from_id,
                    source_id_from_id,
                    sha,
                )
        return result

    # source_type/source_id/sha256 或仅 sha256 查询
    if source_type and source_id and sha256:
        evidence_uri = build_evidence_uri(source_type, source_id, sha256)
        info = get_evidence_info(evidence_uri)
        if info is None:
            return {
                "ok": False,
                "error_code": "NOT_FOUND",
                "retryable": False,
                "message": "未找到对应的 patch_blob",
            }
        return {
            "ok": True,
            "source_type": source_type,
            "source_id": source_id,
            "sha256": sha256,
            "artifact_uri": info.get("artifact_uri"),
            "size_bytes": info.get("size_bytes"),
            "evidence_uri": evidence_uri,
        }

    if sha256:
        evidence_uri = f"memory://patch_blobs/sha256/{sha256}"
        info = get_evidence_info(evidence_uri)
        if info is None:
            return {
                "ok": False,
                "error_code": "NOT_FOUND",
                "retryable": False,
                "message": "未找到对应的 patch_blob",
            }
        source_type_from_id, source_id_from_id = _parse_resource_id(info.get("resource_id", ""))
        return {
            "ok": True,
            "source_type": source_type_from_id,
            "source_id": source_id_from_id,
            "sha256": sha256,
            "artifact_uri": info.get("artifact_uri"),
            "size_bytes": info.get("size_bytes"),
            "evidence_uri": evidence_uri,
        }

    return {
        "ok": False,
        "error_code": "NOT_FOUND",
        "retryable": False,
        "message": "未找到对应的 patch_blob",
    }


async def execute_scm_materialize_patch_blob(
    blob_id: Optional[int] = None,
    evidence_uri: Optional[str] = None,
    source_type: Optional[str] = None,
    source_id: Optional[str] = None,
    sha256: Optional[str] = None,
    *,
    deps: GatewayDepsProtocol,
) -> Dict[str, Any]:
    _ = deps

    if blob_id is None and not evidence_uri and not (source_type and source_id and sha256):
        return {
            "ok": False,
            "error_code": ToolResultErrorCode.MISSING_REQUIRED_PARAMETER,
            "retryable": False,
            "message": "缺少必需参数: blob_id/evidence_uri 或 source_type+source_id+sha256",
        }

    try:
        from engram.logbook.db import get_connection
        from engram.logbook.evidence_resolver import get_evidence_info
        from engram.logbook.materialize_patch_blob import (
            MaterializeStatus,
            PatchBlobRecord,
            materialize_blob,
        )
        from engram.logbook.scm_db import get_patch_blob
    except ImportError as import_err:
        logger.warning(f"materialize_patch_blob 依赖导入失败: {import_err}")
        return {
            "ok": False,
            "error_code": "DEPENDENCY_MISSING",
            "retryable": False,
            "message": "scm_materialize_patch_blob 依赖 engram_logbook 模块",
            "details": {"import_error": str(import_err)},
        }

    conn = get_connection()
    try:
        row: Optional[Dict[str, Any]] = None

        if blob_id is not None:
            # 通过 evidence_resolver 获取元信息，再反查 patch_blobs
            info = get_evidence_info(f"memory://patch_blobs/blob_id/{blob_id}")
            if info:
                st, sid = _parse_resource_id(info.get("resource_id", ""))
                sha = info.get("sha256")
                if st and sid and isinstance(sha, str):
                    row = get_patch_blob(conn, st, sid, sha)
        elif evidence_uri:
            info = get_evidence_info(evidence_uri)
            if info:
                st, sid = _parse_resource_id(info.get("resource_id", ""))
                sha = info.get("sha256")
                if st and sid and isinstance(sha, str):
                    row = get_patch_blob(conn, st, sid, sha)
        else:
            if not (source_type and source_id and sha256):
                return {
                    "ok": False,
                    "error_code": ToolResultErrorCode.MISSING_REQUIRED_PARAMETER,
                    "retryable": False,
                    "message": "缺少必需参数: source_type/source_id/sha256",
                }
            row = get_patch_blob(conn, source_type, source_id, sha256)

        if not row:
            return {
                "ok": False,
                "error_code": "NOT_FOUND",
                "retryable": False,
                "message": "未找到对应的 patch_blob",
            }

        record = PatchBlobRecord(
            blob_id=row["blob_id"],
            source_type=row["source_type"],
            source_id=row["source_id"],
            uri=row.get("uri"),
            sha256=row["sha256"],
            size_bytes=row.get("size_bytes"),
            format=row.get("format") or "diff",
            meta_json=row.get("meta_json"),
        )

        try:
            result = materialize_blob(conn, record, config=None)
        except NotImplementedError as e:
            return {
                "ok": False,
                "error_code": "NOT_IMPLEMENTED",
                "retryable": False,
                "message": str(e) or "materialize_blob 未实现",
            }
        except Exception as e:
            logger.exception(f"materialize_blob 执行失败: {e}")
            return {
                "ok": False,
                "error_code": "EXECUTION_FAILED",
                "retryable": True,
                "message": str(e),
            }

        status = result.status.value if hasattr(result.status, "value") else str(result.status)
        ok = status in (MaterializeStatus.MATERIALIZED.value, MaterializeStatus.SKIPPED.value)
        return {
            "ok": ok,
            "blob_id": result.blob_id,
            "status": status,
            "uri": result.uri,
            "sha256": result.sha256,
            "size_bytes": result.size_bytes,
            "error": result.error,
            "error_category": result.error_category.value if result.error_category else None,
            "status_code": result.status_code,
        }
    finally:
        conn.close()
