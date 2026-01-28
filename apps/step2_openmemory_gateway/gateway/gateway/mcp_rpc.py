"""
MCP JSON-RPC 2.0 协议层模块

提供：
1. JSON-RPC 请求校验与解析
2. method -> handler 映射
3. 统一错误返回
"""

import json
import logging
from typing import Any, Callable, Awaitable, Dict, List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("gateway.mcp_rpc")


# ===================== JSON-RPC 2.0 错误码 =====================


class JsonRpcErrorCode:
    """JSON-RPC 2.0 标准错误码"""
    PARSE_ERROR = -32700       # 解析错误
    INVALID_REQUEST = -32600   # 无效请求
    METHOD_NOT_FOUND = -32601  # 方法不存在
    INVALID_PARAMS = -32602    # 无效参数
    INTERNAL_ERROR = -32603    # 内部错误
    # 自定义服务器错误 (-32000 to -32099)
    TOOL_EXECUTION_ERROR = -32000  # 工具执行错误


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
    
    async def dispatch(self, request: JsonRpcRequest) -> JsonRpcResponse:
        """
        分发 JSON-RPC 请求到对应的处理器
        
        Args:
            request: 已解析的 JSON-RPC 请求
            
        Returns:
            JSON-RPC 响应
        """
        method = request.method
        params = request.params or {}
        req_id = request.id
        
        # 检查方法是否存在
        if method not in self._handlers:
            return make_jsonrpc_error(
                req_id,
                JsonRpcErrorCode.METHOD_NOT_FOUND,
                f"未知方法: {method}",
            )
        
        # 执行处理器
        try:
            handler = self._handlers[method]
            result = await handler(params)
            return make_jsonrpc_result(req_id, result)
        except ValueError as e:
            # 参数错误
            return make_jsonrpc_error(
                req_id,
                JsonRpcErrorCode.INVALID_PARAMS,
                str(e),
            )
        except Exception as e:
            # 内部错误
            logger.exception(f"JSON-RPC 方法执行失败: {method}")
            return make_jsonrpc_error(
                req_id,
                JsonRpcErrorCode.INTERNAL_ERROR,
                f"内部错误: {str(e)}",
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
    """网关错误分类"""
    PROTOCOL = "protocol"      # 协议层错误（使用 JSON-RPC error）
    BUSINESS = "business"      # 业务层错误（作为 result.content 返回）
    DEPENDENCY = "dependency"  # 依赖服务错误（可降级，作为 result.content 返回）


class GatewayError(Exception):
    """
    网关统一错误类型
    
    用于区分协议错误和业务/依赖错误，支持结构化错误信息。
    
    Attributes:
        message: 错误消息
        category: 错误分类 (protocol/business/dependency)
        gateway_error_code: 网关内部错误码（如 OPENMEMORY_WRITE_FAILED）
        correlation_id: 请求追踪 ID
        status_code: HTTP 状态码（依赖服务返回的）
        extra_data: 附加数据
    """
    def __init__(
        self,
        message: str,
        category: str = GatewayErrorCategory.BUSINESS,
        gateway_error_code: Optional[str] = None,
        correlation_id: Optional[str] = None,
        status_code: Optional[int] = None,
        extra_data: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.message = message
        self.category = category
        self.gateway_error_code = gateway_error_code
        self.correlation_id = correlation_id
        self.status_code = status_code
        self.extra_data = extra_data or {}


def to_jsonrpc_error(
    error: Exception,
    req_id: Optional[Any] = None,
    tool_name: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> JsonRpcResponse:
    """
    将异常转换为 JSON-RPC 响应
    
    错误处理策略:
    1. 协议层错误（解析失败、方法不存在、参数无效）→ 返回 JSON-RPC error response
    2. 业务层错误（策略拒绝、鉴权失败）→ 作为 result.content 返回，isError=True
    3. 依赖服务错误（OpenMemory 不可用）→ 作为 result.content 返回，isError=True
    
    这种设计避免把可恢复的降级场景当成协议失败，让调用方能区分：
    - 协议失败：需要修复请求格式
    - 业务/依赖失败：请求格式正确，但执行过程中出现问题
    
    Args:
        error: 异常对象
        req_id: JSON-RPC 请求 ID
        tool_name: 工具名称（可选，用于上下文）
        correlation_id: 关联 ID（可选，用于追踪）
        
    Returns:
        JsonRpcResponse - 错误响应或包含错误的成功响应
        
    Example:
        # 协议错误 → JSON-RPC error
        >>> to_jsonrpc_error(ValueError("缺少参数"), req_id=1)
        JsonRpcResponse(error={"code": -32602, "message": "..."})
        
        # 业务错误 → result with isError
        >>> err = GatewayError("策略拒绝", category="business")
        >>> to_jsonrpc_error(err, req_id=1)
        JsonRpcResponse(result={"content": [...], "isError": True})
    """
    # 1. 处理 GatewayError（结构化错误）
    if isinstance(error, GatewayError):
        # 构建 error.data 附加信息
        error_data = {}
        if error.gateway_error_code:
            error_data["gateway_error_code"] = error.gateway_error_code
        if error.correlation_id or correlation_id:
            error_data["correlation_id"] = error.correlation_id or correlation_id
        if error.status_code:
            error_data["status_code"] = error.status_code
        if error.extra_data:
            error_data.update(error.extra_data)
        if tool_name:
            error_data["tool"] = tool_name
        
        # 协议层错误 → JSON-RPC error response
        if error.category == GatewayErrorCategory.PROTOCOL:
            return make_jsonrpc_error(
                req_id,
                JsonRpcErrorCode.INVALID_REQUEST,
                error.message,
                data=error_data if error_data else None,
            )
        
        # 业务/依赖错误 → 作为 result.content 返回（MCP 规范）
        # 这样调用方能通过 isError=True 识别错误，但不会认为协议失败
        error_content = {
            "ok": False,
            "error": error.message,
            "category": error.category,
        }
        if error.gateway_error_code:
            error_content["gateway_error_code"] = error.gateway_error_code
        if error.correlation_id or correlation_id:
            error_content["correlation_id"] = error.correlation_id or correlation_id
        if error.status_code:
            error_content["status_code"] = error.status_code
        if error.extra_data:
            error_content["extra"] = error.extra_data
        
        return make_jsonrpc_result(
            req_id,
            {
                "content": [
                    {"type": "text", "text": json.dumps(error_content, ensure_ascii=False)}
                ],
                "isError": True,
            },
        )
    
    # 2. 处理 ValueError → 参数错误（协议层）
    if isinstance(error, ValueError):
        error_data = {}
        if correlation_id:
            error_data["correlation_id"] = correlation_id
        if tool_name:
            error_data["tool"] = tool_name
        
        return make_jsonrpc_error(
            req_id,
            JsonRpcErrorCode.INVALID_PARAMS,
            str(error),
            data=error_data if error_data else None,
        )
    
    # 3. 处理 RuntimeError → 内部错误（协议层）
    if isinstance(error, RuntimeError):
        error_data = {"gateway_error_code": "INTERNAL_ERROR"}
        if correlation_id:
            error_data["correlation_id"] = correlation_id
        if tool_name:
            error_data["tool"] = tool_name
        
        return make_jsonrpc_error(
            req_id,
            JsonRpcErrorCode.INTERNAL_ERROR,
            str(error),
            data=error_data,
        )
    
    # 4. 其他未知异常 → 作为业务错误返回（避免暴露内部细节）
    logger.exception(f"未分类的异常: {type(error).__name__}: {error}")
    
    error_content = {
        "ok": False,
        "error": f"内部错误: {str(error)}",
        "category": GatewayErrorCategory.BUSINESS,
        "gateway_error_code": "UNHANDLED_EXCEPTION",
    }
    if correlation_id:
        error_content["correlation_id"] = correlation_id
    
    return make_jsonrpc_result(
        req_id,
        {
            "content": [
                {"type": "text", "text": json.dumps(error_content, ensure_ascii=False)}
            ],
            "isError": True,
        },
    )


def make_business_error_result(
    req_id: Optional[Any],
    error_msg: str,
    gateway_error_code: Optional[str] = None,
    correlation_id: Optional[str] = None,
    status_code: Optional[int] = None,
    extra_data: Optional[Dict[str, Any]] = None,
) -> JsonRpcResponse:
    """
    构造业务层错误响应（作为 result 返回，非 JSON-RPC error）
    
    用于可恢复的业务错误，调用方可以根据 isError=True 和 content 中的
    错误信息决定如何处理。
    
    Args:
        req_id: JSON-RPC 请求 ID
        error_msg: 错误消息
        gateway_error_code: 网关错误码
        correlation_id: 关联 ID
        status_code: HTTP 状态码
        extra_data: 附加数据
        
    Returns:
        JsonRpcResponse with result (not error)
    """
    error_content = {
        "ok": False,
        "error": error_msg,
        "category": GatewayErrorCategory.BUSINESS,
    }
    if gateway_error_code:
        error_content["gateway_error_code"] = gateway_error_code
    if correlation_id:
        error_content["correlation_id"] = correlation_id
    if status_code:
        error_content["status_code"] = status_code
    if extra_data:
        error_content["extra"] = extra_data
    
    return make_jsonrpc_result(
        req_id,
        {
            "content": [
                {"type": "text", "text": json.dumps(error_content, ensure_ascii=False)}
            ],
            "isError": True,
        },
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
    构造依赖服务错误响应（作为 result 返回，非 JSON-RPC error）
    
    用于依赖服务（如 OpenMemory）不可用时，返回可降级的错误。
    
    Args:
        req_id: JSON-RPC 请求 ID
        error_msg: 错误消息
        gateway_error_code: 网关错误码
        correlation_id: 关联 ID
        status_code: 依赖服务返回的 HTTP 状态码
        service_name: 依赖服务名称
        extra_data: 附加数据
        
    Returns:
        JsonRpcResponse with result (not error)
    """
    error_content = {
        "ok": False,
        "error": error_msg,
        "category": GatewayErrorCategory.DEPENDENCY,
    }
    if gateway_error_code:
        error_content["gateway_error_code"] = gateway_error_code
    if correlation_id:
        error_content["correlation_id"] = correlation_id
    if status_code:
        error_content["status_code"] = status_code
    if service_name:
        error_content["service"] = service_name
    if extra_data:
        error_content["extra"] = extra_data
    
    return make_jsonrpc_result(
        req_id,
        {
            "content": [
                {"type": "text", "text": json.dumps(error_content, ensure_ascii=False)}
            ],
            "isError": True,
        },
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
