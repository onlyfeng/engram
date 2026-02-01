"""
Legacy MCP 协议适配器

提供 legacy 格式与 canonical JSON-RPC 2.0 格式之间的双向转换。

================================================================================
                          协议格式对比
================================================================================

Legacy 格式请求:
    {
        "tool": "memory_query",
        "arguments": {"query": "test"}
    }

Canonical JSON-RPC 2.0 请求:
    {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": "memory_query", "arguments": {"query": "test"}},
        "id": 1
    }

Legacy 格式响应:
    {
        "ok": true,
        "result": {...},
        "correlation_id": "corr-abc123"
    }

Canonical JSON-RPC 2.0 响应:
    {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [{"type": "text", "text": "..."}]
        }
    }

================================================================================
                          设计原则
================================================================================

1. 适配器模式：legacy 请求先转换为 canonical 格式，统一通过 JsonRpcRouter 处理
2. 降级器模式：canonical 响应转换为 legacy 格式返回给旧客户端
3. correlation_id 保持：转换过程中 correlation_id 保持一致
4. 错误格式适配：JSON-RPC error 转换为 legacy {ok: false, error: "..."}

================================================================================
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Union

from .api_models import LegacyMCPResponse, LegacyMCPToolCall
from .mcp_rpc import JsonRpcRequest, JsonRpcResponse

logger = logging.getLogger("gateway.legacy_mcp_adapter")


# ===================== 类型定义 =====================


class LegacyAdapterError(Exception):
    """Legacy 适配器转换错误"""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


# ===================== Legacy → Canonical 适配器 =====================


def is_legacy_request(body: Dict[str, Any]) -> bool:
    """
    判断请求是否为 legacy 格式

    Legacy 格式特征:
    - 包含 "tool" 字段
    - 不包含 "jsonrpc" 字段，或 jsonrpc != "2.0"

    Args:
        body: 请求体 dict

    Returns:
        True 如果是 legacy 格式，False 否则
    """
    # 如果是 JSON-RPC 2.0 格式，不是 legacy
    if body.get("jsonrpc") == "2.0" and "method" in body:
        return False

    # 如果包含 tool 字段，是 legacy 格式
    return "tool" in body


def legacy_to_canonical_request(
    legacy_body: Dict[str, Any],
    request_id: Optional[Union[str, int]] = None,
) -> JsonRpcRequest:
    """
    将 legacy 请求转换为 canonical JSON-RPC 2.0 请求

    Args:
        legacy_body: Legacy 格式请求体 {"tool": "xxx", "arguments": {...}}
        request_id: 可选的请求 ID（legacy 格式没有 id，默认为 None）

    Returns:
        JsonRpcRequest 实例

    Raises:
        LegacyAdapterError: 转换失败（如缺少必需字段）

    Example:
        >>> legacy_to_canonical_request({"tool": "memory_query", "arguments": {"query": "test"}})
        JsonRpcRequest(
            jsonrpc="2.0",
            method="tools/call",
            params={"name": "memory_query", "arguments": {"query": "test"}},
            id=None
        )
    """
    # 验证必需字段
    tool = legacy_body.get("tool")
    if not tool:
        raise LegacyAdapterError(
            "Legacy 请求缺少 'tool' 字段",
            details={"received_keys": list(legacy_body.keys())},
        )

    if not isinstance(tool, str):
        raise LegacyAdapterError(
            f"'tool' 字段应为字符串，实际为 {type(tool).__name__}",
            details={"tool_type": type(tool).__name__},
        )

    # 获取 arguments（默认为空字典）
    arguments = legacy_body.get("arguments", {})
    if arguments is None:
        # arguments=null 在 legacy 格式中不允许
        raise LegacyAdapterError(
            "'arguments' 字段不能为 null",
            details={"arguments": None},
        )

    if not isinstance(arguments, dict):
        raise LegacyAdapterError(
            f"'arguments' 字段应为对象，实际为 {type(arguments).__name__}",
            details={"arguments_type": type(arguments).__name__},
        )

    # 构造 canonical 请求
    # legacy 的 tool 映射到 tools/call 的 params.name
    # legacy 的 arguments 映射到 tools/call 的 params.arguments
    return JsonRpcRequest(
        jsonrpc="2.0",
        method="tools/call",
        params={
            "name": tool,
            "arguments": arguments,
        },
        id=request_id,
    )


def legacy_to_canonical_list_request(
    request_id: Optional[Union[str, int]] = None,
) -> JsonRpcRequest:
    """
    构造 canonical tools/list 请求

    Legacy 格式没有对应的 tools/list 请求，此函数用于内部调用。

    Args:
        request_id: 可选的请求 ID

    Returns:
        JsonRpcRequest 实例（method="tools/list"）
    """
    return JsonRpcRequest(
        jsonrpc="2.0",
        method="tools/list",
        params={},
        id=request_id,
    )


# ===================== Canonical → Legacy 降级器 =====================


def canonical_to_legacy_response(
    canonical_response: JsonRpcResponse,
    correlation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    将 canonical JSON-RPC 响应转换为 legacy 格式响应

    转换规则:
    1. 成功响应：
       - canonical.result.content[0].text (JSON string) → 解析为 legacy.result
       - ok = True
    2. 错误响应：
       - canonical.error.message → legacy.error
       - ok = False
    3. correlation_id 始终包含在响应中

    Args:
        canonical_response: JSON-RPC 2.0 响应
        correlation_id: 关联 ID（从请求上下文传递）

    Returns:
        Legacy 格式响应 dict

    Example:
        # 成功响应
        >>> canonical = JsonRpcResponse(
        ...     jsonrpc="2.0",
        ...     id=1,
        ...     result={"content": [{"type": "text", "text": '{"ok": true, "results": []}'}]}
        ... )
        >>> canonical_to_legacy_response(canonical, "corr-abc123")
        {"ok": True, "result": {"ok": true, "results": [], "correlation_id": "corr-abc123"}}

        # 错误响应
        >>> canonical = JsonRpcResponse(
        ...     jsonrpc="2.0",
        ...     id=1,
        ...     error=JsonRpcError(code=-32001, message="连接失败")
        ... )
        >>> canonical_to_legacy_response(canonical, "corr-abc123")
        {"ok": False, "error": "连接失败", "correlation_id": "corr-abc123"}
    """
    # 处理错误响应
    if canonical_response.error:
        error_msg = canonical_response.error.message

        # 构建 legacy 错误响应
        legacy_response: Dict[str, Any] = {
            "ok": False,
            "error": error_msg,
        }

        # 添加 correlation_id
        if correlation_id:
            legacy_response["correlation_id"] = correlation_id

        return legacy_response

    # 处理成功响应
    result = canonical_response.result

    # 尝试从 content[0].text 解析 JSON
    inner_result = _extract_result_from_content(result)

    # 构建 legacy 成功响应
    legacy_response = {
        "ok": True,
        "result": inner_result,
    }

    # 将 correlation_id 注入到 result 中（legacy 约定）
    if correlation_id and isinstance(legacy_response["result"], dict):
        legacy_response["result"]["correlation_id"] = correlation_id

    return legacy_response


def _extract_result_from_content(result: Any) -> Any:
    """
    从 MCP content 数组中提取结果

    MCP tools/call 返回格式:
    {
        "content": [
            {"type": "text", "text": "{\"ok\": true, ...}"}
        ]
    }

    需要解析 content[0].text 中的 JSON。

    Args:
        result: canonical response.result

    Returns:
        解析后的结果 dict，或原始 result（如果无法解析）
    """
    if not isinstance(result, dict):
        return result

    content = result.get("content")
    if not isinstance(content, list) or len(content) == 0:
        return result

    first_content = content[0]
    if not isinstance(first_content, dict):
        return result

    # 处理 TextContent
    if first_content.get("type") == "text":
        text = first_content.get("text", "")
        try:
            # 尝试解析 JSON
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            # 无法解析，返回原始文本包装
            return {"text": text}

    # 其他内容类型（如 image, resource），直接返回
    return result


def canonical_error_to_legacy_response(
    error_msg: str,
    correlation_id: Optional[str] = None,
    error_code: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    构建 legacy 格式错误响应

    用于在适配层捕获到错误时直接构建 legacy 响应。

    Args:
        error_msg: 错误消息
        correlation_id: 关联 ID
        error_code: 错误码（可选）
        details: 附加详情（可选）

    Returns:
        Legacy 格式错误响应 dict
    """
    legacy_response: Dict[str, Any] = {
        "ok": False,
        "error": error_msg,
    }

    if correlation_id:
        legacy_response["correlation_id"] = correlation_id

    if error_code:
        legacy_response["error_code"] = error_code

    if details:
        legacy_response["details"] = details

    return legacy_response


# ===================== 适配器组合函数 =====================


def adapt_legacy_request_to_canonical(
    body: Dict[str, Any],
) -> tuple[Optional[JsonRpcRequest], Optional[Dict[str, Any]]]:
    """
    尝试将 legacy 请求适配为 canonical 格式

    如果请求是 legacy 格式，转换为 JsonRpcRequest。
    如果转换失败，返回错误响应（legacy 格式）。

    Args:
        body: 原始请求体

    Returns:
        (request, error_response)
        - 成功时返回 (JsonRpcRequest, None)
        - 失败时返回 (None, legacy_error_dict)
    """
    try:
        request = legacy_to_canonical_request(body)
        return request, None
    except LegacyAdapterError as e:
        logger.warning(f"Legacy 请求转换失败: {e.message}, details={e.details}")
        return None, canonical_error_to_legacy_response(
            error_msg=e.message,
            error_code="LEGACY_ADAPTER_ERROR",
            details=e.details,
        )


def downgrade_canonical_response_to_legacy(
    canonical_response: JsonRpcResponse,
    correlation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    将 canonical 响应降级为 legacy 格式

    便捷函数，封装 canonical_to_legacy_response。

    Args:
        canonical_response: JSON-RPC 响应
        correlation_id: 关联 ID

    Returns:
        Legacy 格式响应 dict
    """
    return canonical_to_legacy_response(canonical_response, correlation_id)


# ===================== 辅助函数（向后兼容）=====================


def parse_legacy_request(body: Dict[str, Any]) -> tuple[Optional[LegacyMCPToolCall], Optional[str]]:
    """
    解析 legacy 请求为 LegacyMCPToolCall 模型

    向后兼容函数，用于与现有代码集成。

    Args:
        body: 请求体 dict

    Returns:
        (tool_call, error_msg)
        - 成功时返回 (LegacyMCPToolCall, None)
        - 失败时返回 (None, error_msg)
    """
    try:
        tool_call = LegacyMCPToolCall(**body)
        return tool_call, None
    except Exception as e:
        return None, str(e)


def create_legacy_success_response(
    result: Dict[str, Any],
    correlation_id: Optional[str] = None,
) -> LegacyMCPResponse:
    """
    创建 legacy 成功响应模型

    Args:
        result: 结果 dict
        correlation_id: 关联 ID（会注入到 result 中）

    Returns:
        LegacyMCPResponse 实例
    """
    if correlation_id:
        result = {**result, "correlation_id": correlation_id}
    return LegacyMCPResponse(ok=True, result=result, error=None)


def create_legacy_error_response(
    error: str,
    correlation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    创建 legacy 错误响应 dict

    Args:
        error: 错误消息
        correlation_id: 关联 ID

    Returns:
        Legacy 格式错误响应 dict
    """
    response: Dict[str, Any] = {
        "ok": False,
        "error": error,
    }
    if correlation_id:
        response["correlation_id"] = correlation_id
    return response
