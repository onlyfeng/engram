"""
evidence_upload handler - evidence_upload 工具核心实现

提供 execute_evidence_upload 函数，处理：
1. 参数校验
2. 自动创建 item（如果 item_id 缺失）
3. 调用 upload_evidence
4. 返回结果

依赖注入支持：
- 函数签名包含可选的 deps 参数
- 如果不传入 deps，使用模块级函数获取（保持向后兼容）
- 如果传入 deps，使用传入的依赖（用于测试或显式注入）
"""

import logging
from typing import Any, Dict, Optional

from ..di import GatewayDeps, GatewayDepsProtocol
from ..evidence_store import (
    ALLOWED_CONTENT_TYPES,
    EvidenceContentTypeError,
    EvidenceItemRequiredError,
    EvidenceSizeLimitExceededError,
    EvidenceUploadError,
    EvidenceUploadResult,
    EvidenceWriteError,
    upload_evidence,
)

logger = logging.getLogger("gateway.handlers.evidence_upload")


async def execute_evidence_upload(
    content: Optional[str],
    content_type: Optional[str],
    title: Optional[str] = None,
    actor_user_id: Optional[str] = None,
    project_key: Optional[str] = None,
    item_id: Optional[int] = None,
    # 依赖注入参数（推荐方式）
    deps: Optional[GatewayDepsProtocol] = None,
) -> Dict[str, Any]:
    """
    evidence_upload 工具执行函数

    Args:
        content: 证据内容
        content_type: 内容类型
        title: 证据标题
        actor_user_id: 操作者用户 ID
        project_key: 项目标识
        item_id: 关联的 logbook.items.item_id
        deps: 可选的 GatewayDeps 依赖容器，优先使用其中的依赖

    Returns:
        执行结果 dict
    """
    # 参数校验
    if not content:
        return {
            "ok": False,
            "error_code": "MISSING_REQUIRED_PARAMETER",
            "retryable": False,
            "suggestion": "参数 'content' 为必填项，请提供证据内容",
        }

    if not content_type:
        return {
            "ok": False,
            "error_code": "MISSING_REQUIRED_PARAMETER",
            "retryable": False,
            "suggestion": "参数 'content_type' 为必填项，请提供内容类型",
            "allowed_types": list(ALLOWED_CONTENT_TYPES),
        }

    try:
        # 确保有可用的 deps 对象（统一通过 deps.logbook_adapter 获取 adapter）
        # DI-BOUNDARY-ALLOW: legacy fallback (v0.9 兼容期，v1.0 移除)
        if deps is None:
            # [LEGACY] 无 deps 的兼容分支：创建默认 deps 容器
            # 此路径仅为向后兼容保留，新代码应显式传入 deps=GatewayDeps.create()
            # TODO: 后续版本将移除此兼容分支，届时 deps 参数将变为必需
            # DI-BOUNDARY-ALLOW: legacy fallback (v0.9 兼容期，v1.0 移除)
            deps = GatewayDeps.create()
            logger.debug("evidence_upload: 未提供 deps 参数，使用默认 GatewayDeps（legacy path）")

        # 获取 logbook_adapter（统一通过 deps 获取，确保使用同一实例）
        adapter = deps.logbook_adapter

        # 若 item_id 缺失，自动创建 item
        if item_id is None:
            scope_json = {
                "source": "gateway",
            }
            if project_key:
                scope_json["project_key"] = project_key

            item_id = adapter.create_item(
                item_type="evidence",
                title=title or "evidence_upload",
                scope_json=scope_json,
                owner_user_id=actor_user_id,
            )
            logger.info(f"evidence_upload: 自动创建 item, item_id={item_id}")

        # 调用 upload_evidence
        result: EvidenceUploadResult = upload_evidence(
            content=content,
            content_type=content_type,
            actor_user_id=actor_user_id,
            project_key=project_key,
            item_id=item_id,
            title=title,
        )

        # 构建 v2 evidence 对象
        evidence_obj = result.to_evidence_object(title=title)

        return {
            "ok": True,
            "item_id": item_id,
            "attachment_id": result.attachment_id,
            "sha256": result.sha256,
            "evidence": evidence_obj,
            "artifact_uri": result.artifact_uri,
            "size_bytes": result.size_bytes,
            "content_type": result.content_type,
        }

    except EvidenceSizeLimitExceededError as e:
        return {
            "ok": False,
            "error_code": e.error_code,
            "retryable": e.retryable,
            "suggestion": e.details.get("suggestion"),
            "size_bytes": e.details.get("size_bytes"),
            "max_bytes": e.details.get("max_bytes"),
        }
    except EvidenceContentTypeError as e:
        return {
            "ok": False,
            "error_code": e.error_code,
            "retryable": e.retryable,
            "content_type": e.details.get("content_type"),
            "allowed_types": e.details.get("allowed_types"),
        }
    except EvidenceWriteError as e:
        return {
            "ok": False,
            "error_code": e.error_code,
            "retryable": e.retryable,
            "message": e.message,
            "original_error": e.details.get("original_error"),
        }
    except EvidenceItemRequiredError as e:
        return {
            "ok": False,
            "error_code": e.error_code,
            "retryable": e.retryable,
            "suggestion": e.details.get("suggestion"),
        }
    except EvidenceUploadError as e:
        return {
            "ok": False,
            "error_code": e.error_code,
            "retryable": e.retryable,
            "message": e.message,
            **e.details,
        }
    except Exception as e:
        logger.exception(f"evidence_upload 未预期错误: {e}")
        return {
            "ok": False,
            "error_code": "INTERNAL_ERROR",
            "retryable": True,
            "message": str(e),
        }
