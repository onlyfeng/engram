"""
Gateway API 模型定义

此模块集中定义所有 HTTP API 的请求/响应模型和错误码常量。

包含：
1. MCP JSON-RPC 2.0 标准模型（与 MCP 规范对齐）：
   - JSONRPCRequest: JSON-RPC 2.0 请求
   - JSONRPCResponse: JSON-RPC 2.0 响应
   - JSONRPCError: JSON-RPC 2.0 错误对象
   - ToolsListParams/ToolsListResult: tools/list 方法的参数和结果
   - ToolsCallParams/ToolsCallResult: tools/call 方法的参数和结果
   - ToolDefinition: 工具定义
   - TextContent: MCP 标准文本内容

2. Gateway 业务模型：
   - ReliabilityReportErrorCode: 可靠性报告相关错误码
   - MemoryStoreRequest: memory_store 请求模型
   - MemoryQueryRequest: memory_query 请求模型
   - ReliabilityReportResponse: 可靠性报告响应模型
   - GovernanceSettingsUpdateRequest: 治理设置更新请求模型

3. Legacy 兼容层（不与新模型混用）：
   - LegacyMCPToolCall: 旧格式工具调用请求
   - LegacyMCPResponse: 旧格式响应
   - MCPToolCall/MCPResponse: 旧格式别名（已废弃，保持兼容）

4. 常量：
   - MCP_CORS_HEADERS: CORS 配置常量

使用方式:
    # 新代码推荐
    from .api_models import JSONRPCRequest, JSONRPCResponse, ToolsCallParams

    # Legacy 兼容（不推荐新代码使用）
    from .api_models import MCPToolCall, MCPResponse

设计原则：
- 序列化字段名严格遵循 MCP 规范（驼峰命名）
- Legacy 模型独立命名空间，不混入新模型
- 新模型可通过 model_dump() 直接序列化为 MCP 兼容格式
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field

# ===================== MCP JSON-RPC 2.0 核心模型 =====================


class JSONRPCError(BaseModel):
    """
    JSON-RPC 2.0 错误对象

    MCP 规范要求的错误结构，序列化字段名与规范对齐。

    字段说明：
    - code: 错误码（标准码或自定义码）
    - message: 人类可读的错误描述
    - data: 可选的附加错误数据（用于 ErrorData 结构）

    标准错误码参见 mcp_rpc.JsonRpcErrorCode
    """

    code: int = Field(..., description="错误码")
    message: str = Field(..., description="错误消息")
    data: Optional[Any] = Field(None, description="附加数据（ErrorData 结构）")


class JSONRPCRequest(BaseModel):
    """
    JSON-RPC 2.0 请求

    MCP 规范要求的请求结构，序列化字段名与规范对齐。

    字段说明：
    - jsonrpc: 固定为 "2.0"
    - id: 请求标识（用于关联响应，notification 时为 None）
    - method: 方法名（如 "tools/list", "tools/call"）
    - params: 方法参数（可选）
    """

    jsonrpc: Literal["2.0"] = Field("2.0", description="JSON-RPC 版本")
    id: Optional[Union[str, int]] = Field(None, description="请求 ID")
    method: str = Field(..., description="方法名")
    params: Optional[Dict[str, Any]] = Field(None, description="方法参数")


class JSONRPCResponse(BaseModel):
    """
    JSON-RPC 2.0 响应

    MCP 规范要求的响应结构，序列化字段名与规范对齐。

    注意：result 和 error 互斥，成功时 error 为 None，失败时 result 为 None。
    """

    jsonrpc: Literal["2.0"] = Field("2.0", description="JSON-RPC 版本")
    id: Optional[Union[str, int]] = Field(None, description="请求 ID")
    result: Optional[Any] = Field(None, description="成功结果")
    error: Optional[JSONRPCError] = Field(None, description="错误对象")


# ===================== MCP tools/list 模型 =====================


class ToolDefinition(BaseModel):
    """
    MCP 工具定义

    描述一个可调用工具的元数据，序列化字段名与 MCP 规范对齐。

    注意：inputSchema 使用驼峰命名（MCP 规范要求）
    """

    name: str = Field(..., description="工具名称")
    description: str = Field(..., description="工具描述")
    inputSchema: Dict[str, Any] = Field(..., description="输入参数 JSON Schema")


class ToolsListParams(BaseModel):
    """
    tools/list 方法的请求参数

    当前版本无参数，预留扩展字段。

    可选扩展：
    - cursor: 分页游标（MCP 规范预留）
    """

    cursor: Optional[str] = Field(None, description="分页游标（预留）")


class ToolsListResult(BaseModel):
    """
    tools/list 方法的响应结果

    返回可用工具的列表。

    可选扩展：
    - nextCursor: 下一页游标（MCP 规范预留）
    """

    tools: List[ToolDefinition] = Field(..., description="工具定义列表")
    nextCursor: Optional[str] = Field(None, description="下一页游标（预留）")


# ===================== MCP tools/call 模型 =====================


class TextContent(BaseModel):
    """
    MCP 标准文本内容

    用于 tools/call 响应中的 content 数组元素。
    """

    type: Literal["text"] = Field("text", description="内容类型")
    text: str = Field(..., description="文本内容")


class ImageContent(BaseModel):
    """
    MCP 标准图片内容

    用于 tools/call 响应中的 content 数组元素（二进制内容）。
    """

    type: Literal["image"] = Field("image", description="内容类型")
    data: str = Field(..., description="Base64 编码的图片数据")
    mimeType: str = Field(..., description="MIME 类型")


class EmbeddedResource(BaseModel):
    """
    MCP 嵌入资源

    用于 tools/call 响应中的 content 数组元素（资源引用）。
    """

    type: Literal["resource"] = Field("resource", description="内容类型")
    resource: Dict[str, Any] = Field(..., description="资源内容")


# Content 联合类型
ContentItem = Union[TextContent, ImageContent, EmbeddedResource]


class ToolsCallParams(BaseModel):
    """
    tools/call 方法的请求参数

    调用指定工具并传入参数。

    字段说明：
    - name: 工具名称（必需）
    - arguments: 工具参数（可选，默认空字典）
    """

    name: str = Field(..., description="工具名称")
    arguments: Optional[Dict[str, Any]] = Field(default_factory=dict, description="工具参数")


class ToolsCallResult(BaseModel):
    """
    tools/call 方法的响应结果

    返回工具执行的输出内容。

    字段说明：
    - content: 输出内容数组（TextContent/ImageContent/EmbeddedResource）
    - isError: 是否为错误输出（可选，默认 False）

    注意：isError 使用驼峰命名（MCP 规范要求）
    """

    content: List[ContentItem] = Field(..., description="输出内容数组")
    isError: Optional[bool] = Field(None, description="是否为错误输出")


# ===================== MCP 模型构造辅助函数 =====================


def make_text_content(text: str) -> TextContent:
    """
    构造 TextContent 对象

    Args:
        text: 文本内容

    Returns:
        TextContent 实例
    """
    return TextContent(type="text", text=text)


def make_tools_call_result(
    content_text: str,
    is_error: bool = False,
) -> ToolsCallResult:
    """
    构造 ToolsCallResult 对象

    简化的构造函数，用于单文本内容的响应。

    Args:
        content_text: 文本内容（会被包装为 TextContent）
        is_error: 是否为错误输出

    Returns:
        ToolsCallResult 实例
    """
    return ToolsCallResult(
        content=[make_text_content(content_text)],
        isError=is_error if is_error else None,
    )


def make_jsonrpc_success(
    id: Optional[Union[str, int]],
    result: Any,
) -> JSONRPCResponse:
    """
    构造 JSON-RPC 成功响应

    Args:
        id: 请求 ID
        result: 成功结果

    Returns:
        JSONRPCResponse 实例
    """
    return JSONRPCResponse(jsonrpc="2.0", id=id, result=result, error=None)


def make_jsonrpc_error_response(
    id: Optional[Union[str, int]],
    code: int,
    message: str,
    data: Optional[Any] = None,
) -> JSONRPCResponse:
    """
    构造 JSON-RPC 错误响应

    Args:
        id: 请求 ID
        code: 错误码
        message: 错误消息
        data: 附加数据

    Returns:
        JSONRPCResponse 实例
    """
    return JSONRPCResponse(
        jsonrpc="2.0",
        id=id,
        result=None,
        error=JSONRPCError(code=code, message=message, data=data),
    )


# ===================== Legacy 兼容层（独立命名空间）=====================
#
# 以下模型保留用于向后兼容，新代码不应使用。
# Legacy 模型使用独立命名空间，不与新 MCP 模型混用。
#


class LegacyMCPToolCall(BaseModel):
    """
    [Legacy] MCP 工具调用请求（旧格式）

    此模型用于兼容旧版本 API，新代码应使用 ToolsCallParams。

    与新模型的差异：
    - 使用 tool 字段而非 name
    - 无严格类型检查
    """

    tool: str = Field(..., description="工具名称")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="工具参数")

    def to_tools_call_params(self) -> ToolsCallParams:
        """转换为新格式的 ToolsCallParams"""
        return ToolsCallParams(name=self.tool, arguments=self.arguments)


class LegacyMCPResponse(BaseModel):
    """
    [Legacy] MCP 响应（旧格式）

    此模型用于兼容旧版本 API，新代码应使用 JSONRPCResponse。

    与新模型的差异：
    - 使用 ok/result/error 扁平结构
    - 不符合 JSON-RPC 2.0 规范
    """

    ok: bool
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

    @classmethod
    def from_jsonrpc_response(cls, response: JSONRPCResponse) -> "LegacyMCPResponse":
        """从 JSONRPCResponse 转换为 Legacy 格式"""
        if response.error:
            return cls(ok=False, result=None, error=response.error.message)
        return cls(ok=True, result=response.result, error=None)


# Legacy 别名（已废弃，仅保持兼容）
# 新代码应使用 LegacyMCPToolCall / LegacyMCPResponse 或新版模型
MCPToolCall = LegacyMCPToolCall
MCPResponse = LegacyMCPResponse

# ===================== 错误码常量 =====================


class ReliabilityReportErrorCode:
    """reliability_report 相关错误码"""

    IMPORT_FAILED = "RELIABILITY_REPORT_IMPORT_FAILED"
    EXECUTION_FAILED = "RELIABILITY_REPORT_EXECUTION_FAILED"
    DEPENDENCY_UNAVAILABLE = "RELIABILITY_REPORT_DEPENDENCY_UNAVAILABLE"


# ===================== 请求/响应模型 =====================


class MemoryStoreRequest(BaseModel):
    """memory_store 请求模型"""

    payload_md: str = Field(..., description="记忆内容（Markdown 格式）")
    target_space: Optional[str] = Field(None, description="目标空间，默认为 team:<project>")
    meta_json: Optional[Dict[str, Any]] = Field(None, description="元数据")
    # 策略相关字段
    kind: Optional[str] = Field(
        None, description="知识类型: FACT/PROCEDURE/PITFALL/DECISION/REVIEW_GUIDE"
    )
    evidence_refs: Optional[List[str]] = Field(None, description="证据链引用（v1 legacy 格式）")
    evidence: Optional[List[Dict[str, Any]]] = Field(None, description="结构化证据列表（v2 格式）")
    is_bulk: bool = Field(False, description="是否为批量提交")
    # 关联字段
    item_id: Optional[int] = Field(None, description="关联的 logbook.items.item_id")
    # 审计字段
    actor_user_id: Optional[str] = Field(None, description="执行操作的用户标识")


class MemoryQueryRequest(BaseModel):
    """memory_query 请求模型"""

    query: str = Field(..., description="查询文本")
    spaces: Optional[List[str]] = Field(None, description="搜索空间列表")
    filters: Optional[Dict[str, Any]] = Field(None, description="过滤条件")
    top_k: int = Field(10, description="返回结果数量")


class ReliabilityReportResponse(BaseModel):
    """
    可靠性报告响应模型

    结构与 schemas/reliability_report_v1.schema.json 保持一致。

    降级语义（依赖缺失时的行为）：
    =================================
    当 logbook_adapter 或其依赖不可用时，reliability_report 端点会返回降级响应：
    - ok=false：表示报告生成失败
    - message：包含具体错误描述
    - error_code：标准化错误码，便于客户端处理
    - 各统计字段返回空字典 {}

    错误码说明：
    - RELIABILITY_REPORT_IMPORT_FAILED：logbook_adapter 导入失败（依赖缺失）
    - RELIABILITY_REPORT_EXECUTION_FAILED：报告生成执行失败（DB 连接等问题）
    - RELIABILITY_REPORT_DEPENDENCY_UNAVAILABLE：依赖服务不可用
    """

    ok: bool
    outbox_stats: Dict[str, Any] = Field(default_factory=dict, description="outbox_memory 表统计")
    audit_stats: Dict[str, Any] = Field(default_factory=dict, description="write_audit 表统计")
    v2_evidence_stats: Dict[str, Any] = Field(
        default_factory=dict, description="v2 evidence 覆盖率统计"
    )
    content_intercept_stats: Dict[str, Any] = Field(
        default_factory=dict, description="内容拦截统计"
    )
    generated_at: str = Field(default="", description="报告生成时间 (ISO 8601)")
    message: Optional[str] = None
    error_code: Optional[str] = Field(None, description="错误码（仅在 ok=false 时返回）")


class GovernanceSettingsUpdateRequest(BaseModel):
    """governance_update 请求模型"""

    team_write_enabled: Optional[bool] = Field(None, description="是否启用团队写入")
    policy_json: Optional[Dict[str, Any]] = Field(None, description="策略 JSON")
    # 鉴权字段
    admin_key: Optional[str] = Field(None, description="管理密钥（与 GOVERNANCE_ADMIN_KEY 匹配）")
    actor_user_id: Optional[str] = Field(
        None, description="执行操作的用户标识（可选，用于 allowlist 鉴权）"
    )


# CORS 配置常量
MCP_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, Mcp-Session-Id",
    "Access-Control-Expose-Headers": "Mcp-Session-Id, X-Correlation-ID",
    "Access-Control-Max-Age": "86400",
}


def build_mcp_allow_headers(requested_headers: Optional[str]) -> str:
    """
    构建 MCP 端点的 Access-Control-Allow-Headers 值。

    如果请求包含 Access-Control-Request-Headers，则在默认允许列表基础上补全，
    并进行大小写不敏感去重，确保 MCP 规范必需头仍然被允许。
    """

    def _split_headers(raw_headers: Optional[str]) -> List[str]:
        if not raw_headers:
            return []
        return [item.strip() for item in raw_headers.split(",") if item.strip()]

    default_headers = _split_headers(MCP_CORS_HEADERS.get("Access-Control-Allow-Headers"))
    requested = _split_headers(requested_headers)

    merged: List[str] = []
    seen = set()
    for header in [*default_headers, *requested]:
        normalized = header.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        merged.append(header)

    return ", ".join(merged) if merged else ""
