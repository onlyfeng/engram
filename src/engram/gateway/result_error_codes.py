"""
Gateway 工具执行结果错误码定义

本模块定义用于工具执行结果（result.error_code）的错误码常量。
与 MCP/JSON-RPC 层错误码（error_codes.py 中的 McpErrorReason）不同，
这些错误码用于业务层的结构化错误响应。

用法：
    from engram.gateway.result_error_codes import ToolResultErrorCode

    return {
        "ok": False,
        "error_code": ToolResultErrorCode.DEPENDENCY_MISSING,
        ...
    }

设计原则：
- 工具执行结果错误码用于 ok=False 的业务层响应
- 与 JSON-RPC error.data.reason 区分，避免混淆
- 便于调用方根据 error_code 进行错误处理

================================================================================
错误码命名空间边界声明 (Boundary Declaration)
================================================================================

本模块定义的 ToolResultErrorCode 错误码**仅用于**工具执行结果 result.error_code 字段。

禁止行为：
- 禁止将 ToolResultErrorCode.* 用于 JSON-RPC error.data.reason 字段
- 禁止将 McpErrorReason.* (error_codes.py) 用于 result.error_code

边界检查由 CI 门禁强制执行：
- 测试: tests/gateway/test_mcp_jsonrpc_contract.py::TestErrorCodeBoundaryMisuse
- 文档: docs/contracts/mcp_jsonrpc_error_v2.md §3.0

参见: error_codes.py 的对应边界声明
================================================================================
"""


class ToolResultErrorCode:
    """
    工具执行结果错误码常量

    用于工具执行结果中的 error_code 字段（ok=False 时）。

    与 McpErrorReason 的区别：
    - McpErrorReason: 用于 JSON-RPC error.data.reason，表示协议层/框架层错误
    - ToolResultErrorCode: 用于工具执行结果 result.error_code，表示业务层错误

    错误码命名约定：
    - 使用大写 SNAKE_CASE
    - 描述错误的具体原因
    """

    # -------------------------------------------------------------------------
    # 依赖相关错误码
    # -------------------------------------------------------------------------

    # 依赖模块缺失（如 evidence_upload 延迟导入失败）
    DEPENDENCY_MISSING = "DEPENDENCY_MISSING"

    # -------------------------------------------------------------------------
    # 参数校验相关错误码
    # -------------------------------------------------------------------------

    # 缺少必需参数（canonical）
    MISSING_REQUIRED_PARAMETER = "MISSING_REQUIRED_PARAMETER"
    # 旧名 alias（保持向后兼容）
    MISSING_REQUIRED_PARAM = MISSING_REQUIRED_PARAMETER


__all__ = [
    "ToolResultErrorCode",
]
