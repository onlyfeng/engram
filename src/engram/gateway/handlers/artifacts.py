"""
artifacts handlers - artifacts_* 工具实现

提供：
- execute_artifacts_put
- execute_artifacts_get
- execute_artifacts_exists

依赖注入：
- deps 参数为必传，调用方需显式传入 GatewayDeps 实例
- 通过 engram.logbook.artifact_store 读写制品
"""

from __future__ import annotations

import base64
import hashlib
import logging
from typing import Any, Dict, Optional

from ..di import GatewayDepsProtocol
from ..result_error_codes import ToolResultErrorCode

logger = logging.getLogger("gateway.handlers.artifacts")


def _resolve_artifact_uri(uri: Optional[str], path: Optional[str]) -> Optional[str]:
    if uri and path:
        return None
    return uri or path


def _decode_content(
    content: Optional[str], content_base64: Optional[str], encoding: str
) -> Optional[bytes]:
    if content is not None and content_base64 is not None:
        return None
    if content_base64 is not None:
        return base64.b64decode(content_base64.encode("ascii"))
    if content is not None:
        return content.encode(encoding)
    return None


async def execute_artifacts_put(
    uri: Optional[str],
    path: Optional[str] = None,
    content: Optional[str] = None,
    content_base64: Optional[str] = None,
    encoding: str = "utf-8",
    expected_sha256: Optional[str] = None,
    *,
    deps: GatewayDepsProtocol,
) -> Dict[str, Any]:
    _ = deps

    target_uri = _resolve_artifact_uri(uri, path)
    if not target_uri:
        return {
            "ok": False,
            "error_code": ToolResultErrorCode.MISSING_REQUIRED_PARAMETER,
            "retryable": False,
            "message": "缺少必需参数: uri/path",
        }

    content_bytes = _decode_content(content, content_base64, encoding)
    if content_bytes is None:
        return {
            "ok": False,
            "error_code": ToolResultErrorCode.MISSING_REQUIRED_PARAMETER,
            "retryable": False,
            "message": "缺少必需参数: content 或 content_base64",
        }

    if expected_sha256:
        actual_sha256 = hashlib.sha256(content_bytes).hexdigest()
        if actual_sha256.lower() != expected_sha256.lower():
            return {
                "ok": False,
                "error_code": "CHECKSUM_MISMATCH",
                "retryable": False,
                "message": "内容 SHA256 与 expected_sha256 不匹配",
                "expected_sha256": expected_sha256,
                "actual_sha256": actual_sha256,
            }

    try:
        from engram.logbook.artifact_store import (
            ArtifactError,
            ArtifactNotFoundError,
            ArtifactOverwriteDeniedError,
            ArtifactReadError,
            ArtifactSizeLimitExceededError,
            ArtifactWriteDisabledError,
            ArtifactWriteError,
            ObjectStoreConnectionError,
            ObjectStoreError,
            ObjectStoreNotConfiguredError,
            ObjectStoreThrottlingError,
            ObjectStoreTimeoutError,
            PathTraversalError,
            get_artifact_store,
        )
    except ImportError as import_err:
        logger.warning(f"artifact_store 导入失败: {import_err}")
        return {
            "ok": False,
            "error_code": "DEPENDENCY_MISSING",
            "retryable": False,
            "message": "artifacts_put 依赖 engram_logbook 模块",
            "details": {"import_error": str(import_err)},
        }

    try:
        store = get_artifact_store()
        put_result = store.put(uri=target_uri, content=content_bytes)
        result_sha = put_result.get("sha256")
        if expected_sha256 and result_sha and result_sha.lower() != expected_sha256.lower():
            return {
                "ok": False,
                "error_code": "CHECKSUM_MISMATCH",
                "retryable": False,
                "message": "写入后 SHA256 与 expected_sha256 不匹配",
                "expected_sha256": expected_sha256,
                "actual_sha256": result_sha,
            }
        return {
            "ok": True,
            "uri": put_result.get("uri", target_uri),
            "sha256": result_sha,
            "size_bytes": put_result.get("size_bytes"),
        }
    except PathTraversalError as e:
        return {
            "ok": False,
            "error_code": "PATH_TRAVERSAL_ERROR",
            "retryable": False,
            "message": str(e),
        }
    except ArtifactWriteDisabledError as e:
        return {
            "ok": False,
            "error_code": "ARTIFACT_WRITE_DISABLED",
            "retryable": False,
            "message": str(e),
        }
    except ArtifactSizeLimitExceededError as e:
        return {
            "ok": False,
            "error_code": "ARTIFACT_SIZE_LIMIT_EXCEEDED",
            "retryable": False,
            "message": str(e),
        }
    except ArtifactOverwriteDeniedError as e:
        return {
            "ok": False,
            "error_code": "ARTIFACT_OVERWRITE_DENIED",
            "retryable": False,
            "message": str(e),
        }
    except ObjectStoreNotConfiguredError as e:
        return {
            "ok": False,
            "error_code": "OBJECT_STORE_NOT_CONFIGURED",
            "retryable": False,
            "message": str(e),
        }
    except (ObjectStoreConnectionError, ObjectStoreTimeoutError, ObjectStoreThrottlingError) as e:
        return {
            "ok": False,
            "error_code": "OBJECT_STORE_ERROR",
            "retryable": True,
            "message": str(e),
        }
    except (
        ArtifactWriteError,
        ArtifactReadError,
        ArtifactNotFoundError,
        ObjectStoreError,
        ArtifactError,
    ) as e:
        return {
            "ok": False,
            "error_code": "ARTIFACT_WRITE_ERROR",
            "retryable": True,
            "message": str(e),
        }
    except Exception as e:
        logger.exception(f"artifacts_put 未预期错误: {e}")
        return {
            "ok": False,
            "error_code": "INTERNAL_ERROR",
            "retryable": True,
            "message": str(e),
        }


async def execute_artifacts_get(
    uri: Optional[str],
    path: Optional[str] = None,
    encoding: Optional[str] = None,
    max_bytes: Optional[int] = None,
    include_content: bool = True,
    *,
    deps: GatewayDepsProtocol,
) -> Dict[str, Any]:
    _ = deps

    target_uri = _resolve_artifact_uri(uri, path)
    if not target_uri:
        return {
            "ok": False,
            "error_code": ToolResultErrorCode.MISSING_REQUIRED_PARAMETER,
            "retryable": False,
            "message": "缺少必需参数: uri/path",
        }

    try:
        from engram.logbook.artifact_store import (
            ArtifactError,
            ArtifactNotFoundError,
            ArtifactReadError,
            ObjectStoreError,
            PathTraversalError,
            get_artifact_store,
        )
    except ImportError as import_err:
        logger.warning(f"artifact_store 导入失败: {import_err}")
        return {
            "ok": False,
            "error_code": "DEPENDENCY_MISSING",
            "retryable": False,
            "message": "artifacts_get 依赖 engram_logbook 模块",
            "details": {"import_error": str(import_err)},
        }

    try:
        store = get_artifact_store()
        if not include_content:
            info = store.get_info(target_uri)
            return {
                "ok": True,
                "uri": info.get("uri", target_uri),
                "sha256": info.get("sha256"),
                "size_bytes": info.get("size_bytes"),
            }

        content = store.get(target_uri)
        if max_bytes is not None and len(content) > max_bytes:
            return {
                "ok": False,
                "error_code": "PAYLOAD_TOO_LARGE",
                "retryable": False,
                "message": "制品内容超出最大返回大小",
                "size_bytes": len(content),
                "max_bytes": max_bytes,
                "uri": target_uri,
            }

        sha256 = hashlib.sha256(content).hexdigest()
        result: Dict[str, Any] = {
            "ok": True,
            "uri": target_uri,
            "sha256": sha256,
            "size_bytes": len(content),
        }

        if encoding:
            result["content_text"] = content.decode(encoding, errors="replace")
            result["encoding"] = encoding
        else:
            result["content_base64"] = base64.b64encode(content).decode("ascii")
        return result
    except ArtifactNotFoundError as e:
        return {
            "ok": False,
            "error_code": "ARTIFACT_NOT_FOUND",
            "retryable": False,
            "message": str(e),
        }
    except PathTraversalError as e:
        return {
            "ok": False,
            "error_code": "PATH_TRAVERSAL_ERROR",
            "retryable": False,
            "message": str(e),
        }
    except (ArtifactReadError, ObjectStoreError, ArtifactError) as e:
        return {
            "ok": False,
            "error_code": "ARTIFACT_READ_ERROR",
            "retryable": True,
            "message": str(e),
        }
    except Exception as e:
        logger.exception(f"artifacts_get 未预期错误: {e}")
        return {
            "ok": False,
            "error_code": "INTERNAL_ERROR",
            "retryable": True,
            "message": str(e),
        }


async def execute_artifacts_exists(
    uri: Optional[str],
    path: Optional[str] = None,
    *,
    deps: GatewayDepsProtocol,
) -> Dict[str, Any]:
    _ = deps

    target_uri = _resolve_artifact_uri(uri, path)
    if not target_uri:
        return {
            "ok": False,
            "error_code": ToolResultErrorCode.MISSING_REQUIRED_PARAMETER,
            "retryable": False,
            "message": "缺少必需参数: uri/path",
        }

    try:
        from engram.logbook.artifact_store import (
            ArtifactError,
            PathTraversalError,
            get_artifact_store,
        )
    except ImportError as import_err:
        logger.warning(f"artifact_store 导入失败: {import_err}")
        return {
            "ok": False,
            "error_code": "DEPENDENCY_MISSING",
            "retryable": False,
            "message": "artifacts_exists 依赖 engram_logbook 模块",
            "details": {"import_error": str(import_err)},
        }

    try:
        store = get_artifact_store()
        exists = store.exists(target_uri)
        info: Dict[str, Any] = {"ok": True, "uri": target_uri, "exists": exists}
        if exists:
            try:
                meta = store.get_info(target_uri)
                info.update(
                    {
                        "sha256": meta.get("sha256"),
                        "size_bytes": meta.get("size_bytes"),
                    }
                )
            except Exception:
                # info 查询失败不阻断 exists 结果
                pass
        return info
    except PathTraversalError as e:
        return {
            "ok": False,
            "error_code": "PATH_TRAVERSAL_ERROR",
            "retryable": False,
            "message": str(e),
        }
    except ArtifactError as e:
        return {
            "ok": False,
            "error_code": "ARTIFACT_READ_ERROR",
            "retryable": True,
            "message": str(e),
        }
    except Exception as e:
        logger.exception(f"artifacts_exists 未预期错误: {e}")
        return {
            "ok": False,
            "error_code": "INTERNAL_ERROR",
            "retryable": True,
            "message": str(e),
        }
