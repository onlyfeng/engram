"""
evidence_read handler - evidence_read 工具实现

提供 execute_evidence_read 函数，处理：
1. 参数校验
2. evidence 元数据读取（不取内容）
3. 可选读取内容并返回（text 或 base64）
4. 统一错误返回

依赖注入：
- deps 参数为必传，调用方需显式传入 GatewayDeps 实例
- 通过 engram.logbook.evidence_resolver 解析 memory:// URI
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Dict, Optional

from ..di import GatewayDepsProtocol
from ..result_error_codes import ToolResultErrorCode

logger = logging.getLogger("gateway.handlers.evidence_read")


async def execute_evidence_read(
    uri: Optional[str],
    encoding: Optional[str] = None,
    max_bytes: Optional[int] = None,
    include_content: bool = True,
    verify_sha256: bool = True,
    *,
    deps: GatewayDepsProtocol,
) -> Dict[str, Any]:
    """
    evidence_read 工具执行函数

    Args:
        uri: memory:// URI
        encoding: 解码编码（提供时返回 content_text，否则返回 content_base64）
        max_bytes: 最大允许返回内容大小（超过则返回错误）
        include_content: 是否返回内容（false 则仅返回元数据）
        verify_sha256: 是否校验 SHA256（默认 true）
        deps: GatewayDeps 依赖容器（必传）

    Returns:
        执行结果 dict
    """
    _ = deps  # 依赖保留用于未来扩展（如审计、策略等）

    if not uri:
        return {
            "ok": False,
            "error_code": ToolResultErrorCode.MISSING_REQUIRED_PARAMETER,
            "retryable": False,
            "message": "缺少必需参数: uri",
        }

    try:
        from engram.logbook.errors import (
            MemoryUriInvalidError,
            MemoryUriNotFoundError,
            Sha256MismatchError,
        )
        from engram.logbook.evidence_resolver import (
            get_evidence_info,
            resolve_memory_uri,
        )
    except ImportError as import_err:
        logger.warning(f"evidence_resolver 导入失败: {import_err}")
        return {
            "ok": False,
            "error_code": "DEPENDENCY_MISSING",
            "retryable": False,
            "message": "evidence_read 依赖 engram_logbook 模块",
            "details": {
                "missing_module": "engram_logbook",
                "import_error": str(import_err),
            },
        }

    try:
        info = get_evidence_info(uri)
        if info is None:
            return {
                "ok": False,
                "error_code": "NOT_FOUND",
                "retryable": False,
                "message": "证据不存在或 URI 无效",
            }

        if not include_content:
            return {
                "ok": True,
                **info,
            }

        if max_bytes is not None:
            size_bytes = info.get("size_bytes")
            if isinstance(size_bytes, int) and size_bytes > max_bytes:
                return {
                    "ok": False,
                    "error_code": "PAYLOAD_TOO_LARGE",
                    "retryable": False,
                    "message": "证据内容超出最大返回大小",
                    "size_bytes": size_bytes,
                    "max_bytes": max_bytes,
                    "artifact_uri": info.get("artifact_uri"),
                }

        evidence = resolve_memory_uri(uri, verify_sha256=verify_sha256)
        content_bytes = evidence.content
        if max_bytes is not None and len(content_bytes) > max_bytes:
            return {
                "ok": False,
                "error_code": "PAYLOAD_TOO_LARGE",
                "retryable": False,
                "message": "证据内容超出最大返回大小",
                "size_bytes": len(content_bytes),
                "max_bytes": max_bytes,
                "artifact_uri": evidence.artifact_uri,
            }

        result: Dict[str, Any] = {
            "ok": True,
            "resource_type": evidence.resource_type,
            "resource_id": evidence.resource_id,
            "sha256": evidence.sha256,
            "size_bytes": evidence.size_bytes,
            "artifact_uri": evidence.artifact_uri,
            "uri": evidence.uri,
        }

        if encoding:
            result["content_text"] = content_bytes.decode(encoding, errors="replace")
            result["encoding"] = encoding
        else:
            result["content_base64"] = base64.b64encode(content_bytes).decode("ascii")

        return result

    except MemoryUriInvalidError as e:
        return {
            "ok": False,
            "error_code": "INVALID_URI",
            "retryable": False,
            "message": str(e),
        }
    except MemoryUriNotFoundError as e:
        return {
            "ok": False,
            "error_code": "NOT_FOUND",
            "retryable": False,
            "message": str(e),
        }
    except Sha256MismatchError as e:
        return {
            "ok": False,
            "error_code": "CHECKSUM_MISMATCH",
            "retryable": False,
            "message": str(e),
        }
    except Exception as e:
        logger.exception(f"evidence_read 未预期错误: {e}")
        return {
            "ok": False,
            "error_code": "INTERNAL_ERROR",
            "retryable": True,
            "message": str(e),
        }
