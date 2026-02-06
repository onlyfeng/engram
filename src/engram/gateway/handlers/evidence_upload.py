"""
evidence_upload handler - evidence_upload 工具核心实现

提供 execute_evidence_upload 函数，处理：
1. 参数校验
2. 自动创建 item（如果 item_id 缺失）
3. 调用 upload_evidence
4. 返回结果

依赖注入：
- deps 参数为必传，调用方需显式传入 GatewayDeps 实例
- 统一通过 deps.logbook_adapter 获取依赖，确保依赖来源单一可控

导入策略：
- evidence_store 依赖 engram_logbook 模块，采用函数内延迟导入
- 导入失败时返回结构化错误 DEPENDENCY_MISSING，不抛出到 app 工厂层
"""

import logging
from typing import Any, Dict, List, Optional

from ..di import GatewayDepsProtocol

logger = logging.getLogger("gateway.handlers.evidence_upload")

# 默认允许的内容类型列表（当 evidence_store 导入失败时使用）
_DEFAULT_ALLOWED_CONTENT_TYPES: List[str] = [
    "text/plain",
    "text/markdown",
    "text/x-diff",
    "text/x-patch",
    "application/json",
    "application/xml",
    "text/xml",
    "text/html",
    "text/csv",
    "text/yaml",
    "application/x-yaml",
]


async def execute_evidence_upload(
    content: Optional[str],
    content_type: Optional[str],
    title: Optional[str] = None,
    actor_user_id: Optional[str] = None,
    project_key: Optional[str] = None,
    item_id: Optional[int] = None,
    *,
    deps: GatewayDepsProtocol,
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
        deps: GatewayDeps 依赖容器（必传）

    Returns:
        执行结果 dict

    Raises:
        TypeError: 如果未提供 deps 参数
    """
    # ===== 延迟导入 evidence_store（依赖 engram_logbook）=====
    # 导入失败时返回结构化错误，不抛出到 app 工厂层
    try:
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
    except ImportError as import_err:
        logger.warning(f"evidence_store 导入失败: {import_err}")
        return {
            "ok": False,
            "error_code": "DEPENDENCY_MISSING",
            "retryable": False,
            "message": "evidence_upload 功能依赖 engram_logbook 模块，当前未安装或配置不正确",
            "suggestion": (
                "请确保 engram_logbook 模块已正确安装：\n"
                '  pip install -e ".[full]"\n'
                "或检查 POSTGRES_DSN 环境变量是否正确配置"
            ),
            "details": {
                "missing_module": "engram_logbook",
                "import_error": str(import_err),
            },
        }

    # 获取允许的内容类型列表（导入成功后使用实际值）
    allowed_types = list(ALLOWED_CONTENT_TYPES)

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
            "allowed_types": allowed_types,
        }

    try:
        # 获取 logbook_adapter（统一通过 deps 获取，确保使用同一实例）
        adapter = deps.logbook_adapter

        # 确保 actor_user_id 对应的用户存在（避免 items.owner_user_id 外键约束违反）
        if actor_user_id:
            adapter.ensure_user(user_id=actor_user_id, display_name=actor_user_id)

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
