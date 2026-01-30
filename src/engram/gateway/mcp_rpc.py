"""
MCP JSON-RPC 2.0 协议层模块

提供：
1. JSON-RPC 请求校验与解析
2. method -> handler 映射
3. 统一错误返回
4. 结构化错误数据定义
"""

import json
import logging
import uuid
from typing import Any, Callable, Awaitable, Dict, List, Optional, Union

from pydantic import BaseModel, Field

logger = logging.getLogger("gateway.mcp_rpc")

# ===================== 可选依赖导入（避免循环导入）=====================

# OpenMemory 异常类（用于 to_jsonrpc_error 类型检查）
try:
    from engram.gateway.openmemory_client import (
        OpenMemoryError,
        OpenMemoryConnectionError, 
        OpenMemoryAPIError,
    )
except ImportError:
    OpenMemoryError = None
    OpenMemoryConnectionError = None
    OpenMemoryAPIError = None

# Logbook DB 检查异常
try:
    from engram.gateway.logbook_adapter import LogbookDBCheckError
    # 验证它确实是一个类型（防止被 mock 替换）
    if not isinstance(LogbookDBCheckError, type):
        LogbookDBCheckError = None
except ImportError:
    LogbookDBCheckError = None


def _is_exception_type(obj: Any, type_name: str) -> bool:
    """
    安全检查异常类型（防止 mock 导致的 isinstance 错误）
    
    通过类名检查，避免在 mock 环境下出错。
    """
    if obj is None:
        return False
    return type(obj).__name__ == type_name


# ===================== JSON-RPC 2.0 错误码 =====================


class JsonRpcErrorCode:
    """JSON-RPC 2.0 标准错误码"""
    PARSE_ERROR = -32700       # 解析错误
    INVALID_REQUEST = -32600   # 无效请求
    METHOD_NOT_FOUND = -32601  # 方法不存在
    INVALID_PARAMS = -32602    # 无效参数
    INTERNAL_ERROR = -32603    # 内部错误
    # 自定义服务器错误 (-32000 to -32099)
    TOOL_EXECUTION_ERROR = -32000    # 工具执行错误
    DEPENDENCY_UNAVAILABLE = -32001  # 依赖服务不可用
    BUSINESS_REJECTION = -32002      # 业务拒绝


# ===================== 错误分类常量 =====================


class ErrorCategory:
    """错误分类常量（用于 ErrorData.category）"""
    PROTOCOL = "protocol"           # 协议层错误（JSON-RPC 格式、方法不存在）
    VALIDATION = "validation"       # 参数校验错误
    BUSINESS = "business"           # 业务逻辑拒绝（策略拒绝、鉴权失败）
    DEPENDENCY = "dependency"       # 依赖服务错误（OpenMemory/Logbook 不可用）
    INTERNAL = "internal"           # 内部错误（未处理的异常）


# ===================== 稳定的错误 data 结构 =====================


class ErrorData(BaseModel):
    """
    JSON-RPC error.data 的稳定结构
    
    用于向调用方提供结构化的错误上下文，便于自动化处理和调试。
    
    字段说明:
    - category: 错误分类 (protocol/validation/business/dependency/internal)
    - reason: 错误原因码（如 OPENMEMORY_UNAVAILABLE, POLICY_REJECT）
    - retryable: 是否可重试
    - correlation_id: 请求追踪 ID
    - details: 附加详情（可选）
    
    Example:
        {
            "category": "dependency",
            "reason": "OPENMEMORY_CONNECTION_FAILED",
            "retryable": true,
            "correlation_id": "corr-abc123",
            "details": {"service": "openmemory", "status_code": 503}
        }
    """
    category: str = Field(..., description="错误分类: protocol/validation/business/dependency/internal")
    reason: str = Field(..., description="错误原因码")
    retryable: bool = Field(False, description="是否可重试")
    correlation_id: Optional[str] = Field(None, description="请求追踪 ID")
    details: Optional[Dict[str, Any]] = Field(None, description="附加详情")
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为 dict（排除 None 值）"""
        d = {
            "category": self.category,
            "reason": self.reason,
            "retryable": self.retryable,
        }
        if self.correlation_id:
            d["correlation_id"] = self.correlation_id
        if self.details:
            d["details"] = self.details
        return d


# ===================== 错误原因码 =====================


class ErrorReason:
    """错误原因码常量"""
    # 协议层
    PARSE_ERROR = "PARSE_ERROR"
    INVALID_REQUEST = "INVALID_REQUEST"
    METHOD_NOT_FOUND = "METHOD_NOT_FOUND"
    
    # 参数校验
    MISSING_REQUIRED_PARAM = "MISSING_REQUIRED_PARAM"
    INVALID_PARAM_TYPE = "INVALID_PARAM_TYPE"
    INVALID_PARAM_VALUE = "INVALID_PARAM_VALUE"
    UNKNOWN_TOOL = "UNKNOWN_TOOL"
    
    # 业务拒绝
    POLICY_REJECT = "POLICY_REJECT"
    AUTH_FAILED = "AUTH_FAILED"
    ACTOR_UNKNOWN = "ACTOR_UNKNOWN"
    GOVERNANCE_UPDATE_DENIED = "GOVERNANCE_UPDATE_DENIED"
    
    # 依赖不可用
    OPENMEMORY_UNAVAILABLE = "OPENMEMORY_UNAVAILABLE"
    OPENMEMORY_CONNECTION_FAILED = "OPENMEMORY_CONNECTION_FAILED"
    OPENMEMORY_API_ERROR = "OPENMEMORY_API_ERROR"
    LOGBOOK_DB_UNAVAILABLE = "LOGBOOK_DB_UNAVAILABLE"
    LOGBOOK_DB_CHECK_FAILED = "LOGBOOK_DB_CHECK_FAILED"
    
    # 内部错误
    INTERNAL_ERROR = "INTERNAL_ERROR"
    TOOL_EXECUTOR_NOT_REGISTERED = "TOOL_EXECUTOR_NOT_REGISTERED"
    UNHANDLED_EXCEPTION = "UNHANDLED_EXCEPTION"


def generate_correlation_id() -> str:
    """生成关联 ID"""
    return f"corr-{uuid.uuid4().hex[:16]}"


# ===================== JSON-RPC 2.0 数据模型 =====================


class JsonRpcError(BaseModel):
    """JSON-RPC 2.0 错误对象"""
    code: int = Field(..., description="错误码")
    message: str = Field(..., description="错误消息")
    data: Optional[Any] = Field(None, description="附加数据")


class JsonRpcRequest(BaseModel):
    """JSON-RPC 2.0 请求"""
    jsonrpc: str = Field("2.0", description="JSON-RPC 版本")
    id: Optional[Any] = Field(None, description="请求 ID")
    method: str = Field(..., description="方法名")
    params: Optional[Dict[str, Any]] = Field(None, description="方法参数")


class JsonRpcResponse(BaseModel):
    """JSON-RPC 2.0 响应"""
    jsonrpc: str = Field("2.0", description="JSON-RPC 版本")
    id: Optional[Any] = Field(None, description="请求 ID")
    result: Optional[Any] = Field(None, description="成功结果")
    error: Optional[JsonRpcError] = Field(None, description="错误对象")


class ToolDefinition(BaseModel):
    """工具定义"""
    name: str = Field(..., description="工具名称")
    description: str = Field(..., description="工具描述")
    inputSchema: Dict[str, Any] = Field(..., description="输入参数 JSON Schema")


# ===================== 响应构造辅助函数 =====================


def make_jsonrpc_error(
    id: Optional[Any],
    code: int,
    message: str,
    data: Optional[Any] = None,
) -> JsonRpcResponse:
    """构造 JSON-RPC 错误响应"""
    return JsonRpcResponse(
        id=id,
        error=JsonRpcError(code=code, message=message, data=data),
    )


def make_jsonrpc_result(id: Optional[Any], result: Any) -> JsonRpcResponse:
    """构造 JSON-RPC 成功响应"""
    return JsonRpcResponse(id=id, result=result)


# ===================== 请求格式检测 =====================


def is_jsonrpc_request(body: Dict[str, Any]) -> bool:
    """判断请求是否为 JSON-RPC 2.0 格式"""
    return body.get("jsonrpc") == "2.0" and "method" in body


def parse_jsonrpc_request(body: Dict[str, Any]) -> tuple[Optional[JsonRpcRequest], Optional[JsonRpcResponse]]:
    """
    解析 JSON-RPC 请求
    
    Args:
        body: 请求体 dict
        
    Returns:
        (request, error_response)
        - 成功时返回 (JsonRpcRequest, None)
        - 失败时返回 (None, JsonRpcResponse 错误响应)
    """
    try:
        request = JsonRpcRequest(**body)
        return request, None
    except Exception as e:
        return None, make_jsonrpc_error(
            body.get("id"),
            JsonRpcErrorCode.INVALID_REQUEST,
            f"无效的 JSON-RPC 请求: {str(e)}",
        )


# ===================== Handler 类型定义 =====================


# Handler 类型：接收 params dict，返回 result dict
MethodHandler = Callable[[Dict[str, Any]], Awaitable[Any]]


# ===================== JSON-RPC 路由器 =====================


class JsonRpcRouter:
    """
    JSON-RPC 方法路由器
    
    用于注册和分发 JSON-RPC 方法调用。
    
    使用示例:
        router = JsonRpcRouter()
        
        @router.method("tools/list")
        async def list_tools(params: dict) -> dict:
            return {"tools": [...]}
        
        response = await router.dispatch(request)
    """
    
    def __init__(self):
        self._handlers: Dict[str, MethodHandler] = {}
    
    def method(self, name: str):
        """
        方法装饰器，用于注册 JSON-RPC 方法处理器
        
        Args:
            name: 方法名（如 "tools/list", "tools/call"）
        """
        def decorator(func: MethodHandler) -> MethodHandler:
            self._handlers[name] = func
            return func
        return decorator
    
    def register(self, name: str, handler: MethodHandler):
        """
        手动注册方法处理器
        
        Args:
            name: 方法名
            handler: 异步处理函数
        """
        self._handlers[name] = handler
    
    def has_method(self, name: str) -> bool:
        """检查是否有指定方法的处理器"""
        return name in self._handlers
    
    def list_methods(self) -> List[str]:
        """列出所有已注册的方法"""
        return list(self._handlers.keys())
    
    async def dispatch(
        self,
        request: JsonRpcRequest,
        correlation_id: Optional[str] = None,
    ) -> JsonRpcResponse:
        """
        分发 JSON-RPC 请求到对应的处理器
        
        所有错误都通过 to_jsonrpc_error() 转换，确保返回结构化的 ErrorData。
        
        Args:
            request: 已解析的 JSON-RPC 请求
            correlation_id: 可选的关联 ID，用于追踪
            
        Returns:
            JSON-RPC 响应（成功或错误）
        """
        method = request.method
        params = request.params or {}
        req_id = request.id
        
        # 生成或使用提供的 correlation_id
        corr_id = correlation_id or generate_correlation_id()
        
        # 提取工具名（如果是 tools/call）
        tool_name = params.get("name") if method == "tools/call" else None
        
        # 检查方法是否存在
        if method not in self._handlers:
            error_data = ErrorData(
                category=ErrorCategory.PROTOCOL,
                reason=ErrorReason.METHOD_NOT_FOUND,
                retryable=False,
                correlation_id=corr_id,
                details={"method": method, "available_methods": list(self._handlers.keys())},
            )
            return make_jsonrpc_error(
                req_id,
                JsonRpcErrorCode.METHOD_NOT_FOUND,
                f"未知方法: {method}",
                data=error_data.to_dict(),
            )
        
        # 执行处理器
        try:
            handler = self._handlers[method]
            result = await handler(params)
            return make_jsonrpc_result(req_id, result)
        except Exception as e:
            # 使用 to_jsonrpc_error 统一处理所有异常
            logger.exception(f"JSON-RPC 方法执行失败: {method}")
            return to_jsonrpc_error(
                error=e,
                req_id=req_id,
                tool_name=tool_name,
                correlation_id=corr_id,
            )


# ===================== MCP 工具定义 =====================


# 可用工具定义（MCP 标准）
AVAILABLE_TOOLS: List[ToolDefinition] = [
    ToolDefinition(
        name="memory_store",
        description="存储记忆到 OpenMemory，含策略校验、审计、失败降级到 outbox",
        inputSchema={
            "type": "object",
            "properties": {
                "payload_md": {"type": "string", "description": "记忆内容（Markdown 格式）"},
                "target_space": {"type": "string", "description": "目标空间，默认为 team:<project>"},
                "meta_json": {"type": "object", "description": "元数据"},
                "kind": {"type": "string", "description": "知识类型: FACT/PROCEDURE/PITFALL/DECISION/REVIEW_GUIDE"},
                "evidence_refs": {"type": "array", "items": {"type": "string"}, "description": "证据链引用"},
                "is_bulk": {"type": "boolean", "description": "是否为批量提交", "default": False},
                "item_id": {"type": "integer", "description": "关联的 logbook.items.item_id"},
                "actor_user_id": {"type": "string", "description": "执行操作的用户标识"},
            },
            "required": ["payload_md"],
        },
    ),
    ToolDefinition(
        name="memory_query",
        description="查询记忆，支持多空间搜索和过滤",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "查询文本"},
                "spaces": {"type": "array", "items": {"type": "string"}, "description": "搜索空间列表"},
                "filters": {"type": "object", "description": "过滤条件"},
                "top_k": {"type": "integer", "description": "返回结果数量", "default": 10},
            },
            "required": ["query"],
        },
    ),
    ToolDefinition(
        name="reliability_report",
        description="获取可靠性统计报告（只读），包含 outbox 和 audit 统计",
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    ToolDefinition(
        name="governance_update",
        description="更新治理设置（需鉴权），支持 admin_key 或 allowlist_users 鉴权",
        inputSchema={
            "type": "object",
            "properties": {
                "team_write_enabled": {"type": "boolean", "description": "是否启用团队写入"},
                "policy_json": {"type": "object", "description": "策略 JSON"},
                "admin_key": {"type": "string", "description": "管理密钥（与 GOVERNANCE_ADMIN_KEY 匹配）"},
                "actor_user_id": {"type": "string", "description": "执行操作的用户标识（可选，用于 allowlist 鉴权）"},
            },
            "required": [],
        },
    ),
    ToolDefinition(
        name="evidence_upload",
        description=(
            "上传证据文件到存储后端，返回证据引用用于关联记忆。\n"
            "输出: ok/evidence/attachment_id/sha256/item_id\n\n"
            "编码策略:\n"
            "- 纯文本: 直接传递 UTF-8 文本内容\n"
            "- 二进制: 暂不支持 base64 编码（仅支持文本类内容）\n\n"
            "限制:\n"
            "- 最大内容大小: 1MB (1048576 bytes)\n"
            "- 允许的 content_type: text/plain, text/markdown, text/x-diff, text/x-patch, "
            "application/json, application/xml, text/xml, text/html, text/csv, text/yaml, application/x-yaml\n\n"
            "详见: docs/gateway/07_capability_boundary.md"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": (
                        "证据内容（UTF-8 纯文本）。最大 1MB。"
                        "暂不支持 base64 编码的二进制内容。"
                    ),
                },
                "content_type": {
                    "type": "string",
                    "description": (
                        "内容 MIME 类型。允许值: text/plain, text/markdown, text/x-diff, "
                        "text/x-patch, application/json, application/xml, text/xml, "
                        "text/html, text/csv, text/yaml, application/x-yaml"
                    ),
                },
                "title": {"type": "string", "description": "证据标题/文件名"},
                "actor_user_id": {"type": "string", "description": "执行操作的用户标识"},
                "project_key": {"type": "string", "description": "项目标识"},
                "item_id": {"type": "integer", "description": "关联的 logbook.items.item_id（若不提供则自动创建）"},
            },
            "required": ["content", "content_type"],
        },
    ),
]


def get_tool_definitions() -> List[Dict[str, Any]]:
    """获取工具定义列表（dict 格式）"""
    return [tool.model_dump() for tool in AVAILABLE_TOOLS]


# ===================== 工具调用辅助函数 =====================


def format_tool_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """
    格式化工具执行结果为 MCP 标准格式
    
    Args:
        result: 工具返回的 dict
        
    Returns:
        MCP 格式的 content 数组
    """
    return {
        "content": [
            {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
        ]
    }


def make_tool_error(
    req_id: Optional[Any],
    tool_name: str,
    error_msg: str,
    is_not_found: bool = False,
) -> JsonRpcResponse:
    """
    构造工具执行错误响应
    
    Args:
        req_id: 请求 ID
        tool_name: 工具名
        error_msg: 错误消息
        is_not_found: 是否为工具不存在错误
    """
    if is_not_found:
        return make_jsonrpc_error(
            req_id,
            JsonRpcErrorCode.METHOD_NOT_FOUND,
            error_msg,
        )
    return make_jsonrpc_error(
        req_id,
        JsonRpcErrorCode.TOOL_EXECUTION_ERROR,
        f"工具执行失败: {error_msg}",
        data={"tool": tool_name},
    )


# ===================== 错误分类与转换 =====================


class GatewayErrorCategory:
    """网关错误分类（兼容旧代码）"""
    PROTOCOL = ErrorCategory.PROTOCOL
    BUSINESS = ErrorCategory.BUSINESS
    DEPENDENCY = ErrorCategory.DEPENDENCY


class GatewayError(Exception):
    """
    网关统一错误类型
    
    用于区分协议错误和业务/依赖错误，支持结构化错误信息。
    
    Attributes:
        message: 错误消息
        category: 错误分类 (protocol/business/dependency/validation/internal)
        reason: 错误原因码（使用 ErrorReason 常量）
        retryable: 是否可重试
        correlation_id: 请求追踪 ID
        status_code: HTTP 状态码（依赖服务返回的）
        details: 附加详情
        
        # 兼容旧版本
        gateway_error_code: 已废弃，使用 reason
        extra_data: 已废弃，使用 details
    """
    def __init__(
        self,
        message: str,
        category: str = ErrorCategory.BUSINESS,
        reason: Optional[str] = None,
        retryable: bool = False,
        correlation_id: Optional[str] = None,
        status_code: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None,
        # 兼容旧版本参数
        gateway_error_code: Optional[str] = None,
        extra_data: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.message = message
        self.category = category
        self.reason = reason or gateway_error_code or ErrorReason.INTERNAL_ERROR
        self.retryable = retryable
        self.correlation_id = correlation_id
        self.status_code = status_code
        self.details = details or extra_data or {}
        
        # 兼容旧版本属性
        self.gateway_error_code = self.reason
        self.extra_data = self.details
    
    def to_error_data(self, override_correlation_id: Optional[str] = None) -> ErrorData:
        """转换为 ErrorData 结构"""
        return ErrorData(
            category=self.category,
            reason=self.reason,
            retryable=self.retryable,
            correlation_id=override_correlation_id or self.correlation_id,
            details=self.details if self.details else None,
        )


def to_jsonrpc_error(
    error: Exception,
    req_id: Optional[Any] = None,
    tool_name: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> JsonRpcResponse:
    """
    将异常转换为 JSON-RPC 响应
    
    统一的错误转换函数，支持：
    - GatewayError: 网关自定义错误
    - OpenMemoryError/OpenMemoryConnectionError/OpenMemoryAPIError: OpenMemory 依赖错误
    - LogbookDBCheckError: Logbook 数据库错误
    - ValueError: 参数校验错误
    - RuntimeError: 内部运行时错误
    - 其他异常: 作为内部错误处理
    
    错误处理策略:
    1. 协议层错误（解析失败、方法不存在、参数无效）→ 返回 JSON-RPC error response
    2. 业务层错误（策略拒绝、鉴权失败）→ 返回 JSON-RPC error response (business_rejection)
    3. 依赖服务错误（OpenMemory/Logbook 不可用）→ 返回 JSON-RPC error response (dependency_unavailable)
    
    所有错误都返回结构化的 error.data（ErrorData 格式），包含:
    - category: 错误分类
    - reason: 错误原因码
    - retryable: 是否可重试
    - correlation_id: 追踪 ID
    - details: 附加详情
    
    Args:
        error: 异常对象
        req_id: JSON-RPC 请求 ID
        tool_name: 工具名称（可选，用于上下文）
        correlation_id: 关联 ID（可选，用于追踪）
        
    Returns:
        JsonRpcResponse - 包含结构化 error.data 的错误响应
        
    Example:
        # 参数错误
        >>> to_jsonrpc_error(ValueError("缺少参数 name"), req_id=1)
        JsonRpcResponse(error={"code": -32602, "message": "...", "data": {"category": "validation", ...}})
        
        # 依赖不可用
        >>> to_jsonrpc_error(OpenMemoryConnectionError("连接超时"), req_id=1)
        JsonRpcResponse(error={"code": -32001, "message": "...", "data": {"category": "dependency", "retryable": true, ...}})
    """
    # 确保有 correlation_id
    corr_id = correlation_id or generate_correlation_id()
    
    # 构建 details 基础信息
    base_details: Dict[str, Any] = {}
    if tool_name:
        base_details["tool"] = tool_name
    
    # 使用类型名称检查，避免导入失败或 mock 导致的问题
    error_type_name = type(error).__name__
    error_module = type(error).__module__ if hasattr(type(error), '__module__') else ""
    
    # ===== 1. 处理 GatewayError（网关自定义错误）=====
    if isinstance(error, GatewayError):
        # 合并 details
        details = {**base_details, **error.details} if error.details else base_details
        if error.status_code:
            details["status_code"] = error.status_code
        
        error_data = ErrorData(
            category=error.category,
            reason=error.reason,
            retryable=error.retryable,
            correlation_id=corr_id,
            details=details if details else None,
        )
        
        # 根据分类选择错误码
        if error.category == ErrorCategory.PROTOCOL:
            code = JsonRpcErrorCode.INVALID_REQUEST
        elif error.category == ErrorCategory.VALIDATION:
            code = JsonRpcErrorCode.INVALID_PARAMS
        elif error.category == ErrorCategory.BUSINESS:
            code = JsonRpcErrorCode.BUSINESS_REJECTION
        elif error.category == ErrorCategory.DEPENDENCY:
            code = JsonRpcErrorCode.DEPENDENCY_UNAVAILABLE
        else:
            code = JsonRpcErrorCode.INTERNAL_ERROR
        
        return make_jsonrpc_error(
            req_id,
            code,
            error.message,
            data=error_data.to_dict(),
        )
    
    # ===== 2. 处理 OpenMemory 异常 =====
    if OpenMemoryConnectionError is not None and isinstance(error, OpenMemoryConnectionError):
        details = {**base_details, "service": "openmemory"}
        if hasattr(error, 'status_code') and error.status_code:
            details["status_code"] = error.status_code
        
        error_data = ErrorData(
            category=ErrorCategory.DEPENDENCY,
            reason=ErrorReason.OPENMEMORY_CONNECTION_FAILED,
            retryable=True,  # 连接错误通常可重试
            correlation_id=corr_id,
            details=details,
        )
        
        return make_jsonrpc_error(
            req_id,
            JsonRpcErrorCode.DEPENDENCY_UNAVAILABLE,
            f"OpenMemory 连接失败: {error.message if hasattr(error, 'message') else str(error)}",
            data=error_data.to_dict(),
        )
    
    if OpenMemoryAPIError is not None and isinstance(error, OpenMemoryAPIError):
        details = {**base_details, "service": "openmemory"}
        if hasattr(error, 'status_code') and error.status_code:
            details["status_code"] = error.status_code
        if hasattr(error, 'response') and error.response:
            details["api_response"] = error.response
        
        # 5xx 错误可重试，4xx 不可重试
        retryable = hasattr(error, 'status_code') and error.status_code and error.status_code >= 500
        
        error_data = ErrorData(
            category=ErrorCategory.DEPENDENCY,
            reason=ErrorReason.OPENMEMORY_API_ERROR,
            retryable=retryable,
            correlation_id=corr_id,
            details=details,
        )
        
        return make_jsonrpc_error(
            req_id,
            JsonRpcErrorCode.DEPENDENCY_UNAVAILABLE,
            f"OpenMemory API 错误: {error.message if hasattr(error, 'message') else str(error)}",
            data=error_data.to_dict(),
        )
    
    if OpenMemoryError is not None and isinstance(error, OpenMemoryError):
        details = {**base_details, "service": "openmemory"}
        if hasattr(error, 'status_code') and error.status_code:
            details["status_code"] = error.status_code
        
        error_data = ErrorData(
            category=ErrorCategory.DEPENDENCY,
            reason=ErrorReason.OPENMEMORY_UNAVAILABLE,
            retryable=True,
            correlation_id=corr_id,
            details=details,
        )
        
        return make_jsonrpc_error(
            req_id,
            JsonRpcErrorCode.DEPENDENCY_UNAVAILABLE,
            f"OpenMemory 错误: {error.message if hasattr(error, 'message') else str(error)}",
            data=error_data.to_dict(),
        )
    
    # ===== 3. 处理 Logbook DB 异常 =====
    if LogbookDBCheckError is not None and isinstance(error, LogbookDBCheckError):
        details = {**base_details, "service": "logbook_db"}
        if hasattr(error, 'missing_items') and error.missing_items:
            details["missing_items"] = error.missing_items
        
        error_data = ErrorData(
            category=ErrorCategory.DEPENDENCY,
            reason=ErrorReason.LOGBOOK_DB_CHECK_FAILED,
            retryable=False,  # DB 结构问题通常不可自动重试
            correlation_id=corr_id,
            details=details,
        )
        
        return make_jsonrpc_error(
            req_id,
            JsonRpcErrorCode.DEPENDENCY_UNAVAILABLE,
            f"Logbook DB 检查失败: {error.message if hasattr(error, 'message') else str(error)}",
            data=error_data.to_dict(),
        )
    
    # ===== 4. 处理 ValueError → 参数校验错误 =====
    if isinstance(error, ValueError):
        error_msg = str(error)
        
        # 根据错误消息推断更具体的原因码
        if "缺少" in error_msg or "missing" in error_msg.lower() or "required" in error_msg.lower():
            reason = ErrorReason.MISSING_REQUIRED_PARAM
        elif "未知工具" in error_msg or "unknown tool" in error_msg.lower():
            reason = ErrorReason.UNKNOWN_TOOL
        elif "类型" in error_msg or "type" in error_msg.lower():
            reason = ErrorReason.INVALID_PARAM_TYPE
        else:
            reason = ErrorReason.INVALID_PARAM_VALUE
        
        error_data = ErrorData(
            category=ErrorCategory.VALIDATION,
            reason=reason,
            retryable=False,
            correlation_id=corr_id,
            details=base_details if base_details else None,
        )
        
        return make_jsonrpc_error(
            req_id,
            JsonRpcErrorCode.INVALID_PARAMS,
            error_msg,
            data=error_data.to_dict(),
        )
    
    # ===== 5. 处理 RuntimeError → 内部错误 =====
    if isinstance(error, RuntimeError):
        error_msg = str(error)
        
        # 判断是否是执行器未注册
        if "执行器未注册" in error_msg or "not registered" in error_msg.lower():
            reason = ErrorReason.TOOL_EXECUTOR_NOT_REGISTERED
        else:
            reason = ErrorReason.INTERNAL_ERROR
        
        error_data = ErrorData(
            category=ErrorCategory.INTERNAL,
            reason=reason,
            retryable=False,
            correlation_id=corr_id,
            details=base_details if base_details else None,
        )
        
        return make_jsonrpc_error(
            req_id,
            JsonRpcErrorCode.INTERNAL_ERROR,
            error_msg,
            data=error_data.to_dict(),
        )
    
    # ===== 6. 处理 psycopg2/数据库异常 =====
    error_type_name = type(error).__name__
    if "psycopg2" in error_type_name.lower() or "database" in error_type_name.lower() or "operational" in error_type_name.lower():
        error_data = ErrorData(
            category=ErrorCategory.DEPENDENCY,
            reason=ErrorReason.LOGBOOK_DB_UNAVAILABLE,
            retryable=True,  # 数据库连接问题通常可重试
            correlation_id=corr_id,
            details={**base_details, "service": "logbook_db", "exception_type": error_type_name},
        )
        
        return make_jsonrpc_error(
            req_id,
            JsonRpcErrorCode.DEPENDENCY_UNAVAILABLE,
            f"数据库错误: {str(error)}",
            data=error_data.to_dict(),
        )
    
    # ===== 7. 其他未知异常 → 内部错误 =====
    logger.exception(f"未分类的异常: {error_type_name}: {error}")
    
    error_data = ErrorData(
        category=ErrorCategory.INTERNAL,
        reason=ErrorReason.UNHANDLED_EXCEPTION,
        retryable=False,
        correlation_id=corr_id,
        details={**base_details, "exception_type": error_type_name} if base_details else {"exception_type": error_type_name},
    )
    
    return make_jsonrpc_error(
        req_id,
        JsonRpcErrorCode.INTERNAL_ERROR,
        f"内部错误: {str(error)}",
        data=error_data.to_dict(),
    )


def make_business_error_response(
    req_id: Optional[Any],
    error_msg: str,
    reason: str = ErrorReason.POLICY_REJECT,
    correlation_id: Optional[str] = None,
    retryable: bool = False,
    details: Optional[Dict[str, Any]] = None,
) -> JsonRpcResponse:
    """
    构造业务层错误响应（JSON-RPC error response）
    
    用于业务逻辑拒绝（如策略拒绝、鉴权失败）。
    
    Args:
        req_id: JSON-RPC 请求 ID
        error_msg: 错误消息
        reason: 错误原因码（使用 ErrorReason 常量）
        correlation_id: 关联 ID
        retryable: 是否可重试
        details: 附加详情
        
    Returns:
        JsonRpcResponse with error (code: -32002 BUSINESS_REJECTION)
    """
    corr_id = correlation_id or generate_correlation_id()
    
    error_data = ErrorData(
        category=ErrorCategory.BUSINESS,
        reason=reason,
        retryable=retryable,
        correlation_id=corr_id,
        details=details,
    )
    
    return make_jsonrpc_error(
        req_id,
        JsonRpcErrorCode.BUSINESS_REJECTION,
        error_msg,
        data=error_data.to_dict(),
    )


def make_dependency_error_response(
    req_id: Optional[Any],
    error_msg: str,
    reason: str = ErrorReason.OPENMEMORY_UNAVAILABLE,
    correlation_id: Optional[str] = None,
    retryable: bool = True,
    service_name: Optional[str] = None,
    status_code: Optional[int] = None,
    details: Optional[Dict[str, Any]] = None,
) -> JsonRpcResponse:
    """
    构造依赖服务错误响应（JSON-RPC error response）
    
    用于依赖服务（如 OpenMemory、Logbook DB）不可用时返回错误。
    
    Args:
        req_id: JSON-RPC 请求 ID
        error_msg: 错误消息
        reason: 错误原因码（使用 ErrorReason 常量）
        correlation_id: 关联 ID
        retryable: 是否可重试（默认 True，依赖服务通常可重试）
        service_name: 依赖服务名称
        status_code: 依赖服务返回的 HTTP 状态码
        details: 附加详情
        
    Returns:
        JsonRpcResponse with error (code: -32001 DEPENDENCY_UNAVAILABLE)
    """
    corr_id = correlation_id or generate_correlation_id()
    
    # 构建 details
    full_details: Dict[str, Any] = {}
    if service_name:
        full_details["service"] = service_name
    if status_code:
        full_details["status_code"] = status_code
    if details:
        full_details.update(details)
    
    error_data = ErrorData(
        category=ErrorCategory.DEPENDENCY,
        reason=reason,
        retryable=retryable,
        correlation_id=corr_id,
        details=full_details if full_details else None,
    )
    
    return make_jsonrpc_error(
        req_id,
        JsonRpcErrorCode.DEPENDENCY_UNAVAILABLE,
        error_msg,
        data=error_data.to_dict(),
    )


# ==================== 兼容旧版本的别名 ====================

def make_business_error_result(
    req_id: Optional[Any],
    error_msg: str,
    gateway_error_code: Optional[str] = None,
    correlation_id: Optional[str] = None,
    status_code: Optional[int] = None,
    extra_data: Optional[Dict[str, Any]] = None,
) -> JsonRpcResponse:
    """
    [已废弃] 请使用 make_business_error_response()
    
    保留用于向后兼容。
    """
    details = extra_data.copy() if extra_data else {}
    if status_code:
        details["status_code"] = status_code
    
    return make_business_error_response(
        req_id=req_id,
        error_msg=error_msg,
        reason=gateway_error_code or ErrorReason.POLICY_REJECT,
        correlation_id=correlation_id,
        retryable=False,
        details=details if details else None,
    )


def make_dependency_error_result(
    req_id: Optional[Any],
    error_msg: str,
    gateway_error_code: Optional[str] = None,
    correlation_id: Optional[str] = None,
    status_code: Optional[int] = None,
    service_name: Optional[str] = None,
    extra_data: Optional[Dict[str, Any]] = None,
) -> JsonRpcResponse:
    """
    [已废弃] 请使用 make_dependency_error_response()
    
    保留用于向后兼容。
    """
    return make_dependency_error_response(
        req_id=req_id,
        error_msg=error_msg,
        reason=gateway_error_code or ErrorReason.OPENMEMORY_UNAVAILABLE,
        correlation_id=correlation_id,
        retryable=True,
        service_name=service_name,
        status_code=status_code,
        details=extra_data,
    )


# ===================== 工具执行器注册表 =====================


# 工具执行器类型：接收工具名和参数，返回结果 dict
ToolExecutor = Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]]

# 全局工具执行器（由 main.py 注册）
_tool_executor: Optional[ToolExecutor] = None


def register_tool_executor(executor: ToolExecutor):
    """
    注册工具执行器
    
    工具执行器负责实际调用业务逻辑（memory_store_impl 等）。
    由 main.py 在启动时注册。
    
    Args:
        executor: 异步函数，签名为 (tool_name, arguments) -> result_dict
    """
    global _tool_executor
    _tool_executor = executor
    logger.info("工具执行器已注册")


def get_tool_executor() -> Optional[ToolExecutor]:
    """获取已注册的工具执行器"""
    return _tool_executor


# ===================== tools/call Handler =====================


async def handle_tools_call(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    处理 tools/call JSON-RPC 请求
    
    调用已注册的工具执行器执行业务逻辑，将结果序列化为 JSON 字符串
    放入 TextContent.text（符合 MCP 规范）。
    
    错误处理约定：
    - JSON-RPC 层错误（协议/参数/内部异常）：抛出异常，由路由器转换为 error response
    - 业务 reject/redirect：作为正常 result 返回（ok=False, action="reject"/"redirect"）
    
    Args:
        params: JSON-RPC 请求参数，应包含 {name: str, arguments: dict}
        
    Returns:
        MCP 格式结果 {content: [{type: "text", text: "..."}]}
        
    Raises:
        ValueError: 缺少必需参数或工具未找到
        RuntimeError: 工具执行器未注册
    """
    # 1. 参数校验
    tool_name = params.get("name")
    tool_args = params.get("arguments", {})
    
    if not tool_name:
        raise ValueError("缺少必需参数: name")
    
    # 2. 检查工具是否存在
    available_tool_names = [t.name for t in AVAILABLE_TOOLS]
    if tool_name not in available_tool_names:
        raise ValueError(f"未知工具: {tool_name}")
    
    # 3. 检查执行器是否已注册
    executor = get_tool_executor()
    if executor is None:
        raise RuntimeError("工具执行器未注册")
    
    # 4. 执行工具调用
    # 业务层的 reject/redirect 会作为正常结果返回（ok=False），不抛异常
    result = await executor(tool_name, tool_args)
    
    # 5. 序列化结果为 JSON 字符串，放入 TextContent.text
    return format_tool_result(result)


async def handle_tools_list(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    处理 tools/list JSON-RPC 请求
    
    返回所有可用工具的定义列表。
    
    Args:
        params: JSON-RPC 请求参数（本方法不使用）
        
    Returns:
        {tools: [ToolDefinition...]}
    """
    return {"tools": get_tool_definitions()}


# ===================== MCP JSON-RPC 路由器初始化 =====================


def create_mcp_router() -> JsonRpcRouter:
    """
    创建并初始化 MCP JSON-RPC 路由器
    
    注册 MCP 标准方法：
    - tools/list: 返回可用工具清单
    - tools/call: 调用工具
    
    Returns:
        配置好的 JsonRpcRouter 实例
    """
    router = JsonRpcRouter()
    router.register("tools/list", handle_tools_list)
    router.register("tools/call", handle_tools_call)
    return router


# 默认 MCP 路由器实例
mcp_router = create_mcp_router()
