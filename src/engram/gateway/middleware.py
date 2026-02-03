"""
Gateway 中间件模块

提供:
- install_middleware(app: FastAPI) -> None: 安装所有中间件和异常处理器
- CorrelationIdMiddleware: 统一处理 correlation_id 的生成与传递

设计原则:
================
1. Import-Safe: 模块导入时不触发 get_config()/get_container()
2. 单一来源: correlation_id 在中间件层统一生成，通过 contextvars 传递
3. 契约保证: X-Correlation-ID header 与 error.data.correlation_id 始终一致

correlation_id 契约:
================
1. 每个请求入口处由中间件生成一个 correlation_id
2. correlation_id 格式: ^corr-[a-fA-F0-9]{16}$
3. 所有响应（成功或错误）的 X-Correlation-ID header 必须包含此 correlation_id
4. 所有错误响应的 error.data.correlation_id 必须与 X-Correlation-ID 一致
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from starlette.types import ASGIApp

from .error_redaction import DEFAULT_PUBLIC_ERROR_MESSAGE, sanitize_error_message

logger = logging.getLogger("gateway.middleware")


# ===================== Correlation ID 上下文管理 =====================

# 请求级别的 correlation_id 存储
_request_correlation_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "_request_correlation_id", default=None
)


def get_request_correlation_id() -> Optional[str]:
    """
    获取当前请求的 correlation_id

    由 CorrelationIdMiddleware 在请求入口处设置。

    Returns:
        当前请求的 correlation_id，如果不在请求上下文中则返回 None
    """
    return _request_correlation_id.get()


def set_request_correlation_id(correlation_id: str) -> contextvars.Token[Optional[str]]:
    """
    设置当前请求的 correlation_id

    通常由 CorrelationIdMiddleware 调用。

    Args:
        correlation_id: 要设置的 correlation_id

    Returns:
        contextvars.Token 用于恢复之前的值
    """
    return _request_correlation_id.set(correlation_id)


def reset_request_correlation_id_for_testing() -> None:
    """
    重置 correlation_id 为默认值 (None)

    仅用于测试隔离或异常恢复场景。

    警告:
    - 此函数仅应在测试代码中调用，用于确保测试间的状态隔离
    - 在生产代码中，应使用 CorrelationIdMiddleware 的 token 机制进行状态恢复
    - 异常恢复场景：当 contextvars token 丢失且无法正常恢复时的兜底方案
    """
    _request_correlation_id.set(None)


# ===================== Correlation ID Middleware =====================


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """
    统一处理 correlation_id 的中间件

    职责:
    1. 在请求入口处生成 correlation_id
    2. 将 correlation_id 存储到 contextvars
    3. 在响应头中添加 X-Correlation-ID

    注意:
    - correlation_id 生成使用延迟导入的 generate_correlation_id()
    - 异常处理由 exception_handler 负责，此中间件仅处理 correlation_id 传递
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """
        处理请求，生成并传递 correlation_id
        """
        # 从 mcp_rpc 重新导出（保持单一来源且便于测试 patch）
        from .mcp_rpc import generate_correlation_id

        # 1. 生成 correlation_id
        correlation_id = generate_correlation_id()

        # 2. 存储到 contextvars（供后续代码使用）
        token = set_request_correlation_id(correlation_id)

        try:
            # 3. 执行请求处理
            response = await call_next(request)

            # 4. 确保响应头中有 X-Correlation-ID
            # 如果已经设置了（如 mcp_endpoint 中），则不覆盖
            if "X-Correlation-ID" not in response.headers:
                response.headers["X-Correlation-ID"] = correlation_id

            return response
        finally:
            # 恢复 contextvars
            _request_correlation_id.reset(token)


def _is_mcp_path(path: str) -> bool:
    """判断是否为 MCP 相关路径。"""
    return path == "/mcp" or path.startswith("/mcp/")


def _load_gateway_auth_tokens() -> set[str]:
    """
    从环境变量加载 Gateway Auth tokens。

    支持:
    - GATEWAY_AUTH_TOKEN: 单 token
    - GATEWAY_AUTH_TOKENS_JSON: JSON 列表
    """
    tokens: set[str] = set()
    single_token = os.environ.get("GATEWAY_AUTH_TOKEN", "").strip()
    if single_token:
        tokens.add(single_token)

    tokens_json = os.environ.get("GATEWAY_AUTH_TOKENS_JSON", "").strip()
    if tokens_json:
        try:
            parsed = json.loads(tokens_json)
        except json.JSONDecodeError as exc:
            logger.warning(f"GATEWAY_AUTH_TOKENS_JSON 解析失败: {exc}")
        else:
            if isinstance(parsed, list):
                for token in parsed:
                    if isinstance(token, str) and token.strip():
                        tokens.add(token.strip())
            else:
                logger.warning("GATEWAY_AUTH_TOKENS_JSON 必须是 JSON 列表")

    return tokens


def _extract_bearer_token(auth_header: Optional[str]) -> Optional[str]:
    """从 Authorization header 中提取 Bearer token。"""
    if not auth_header:
        return None
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header[len("Bearer ") :].strip()
    return token or None


def _build_mcp_cors_headers(request: Request) -> dict:
    """基于请求构造 MCP CORS headers（可动态扩展 Allow-Headers）。"""
    from .api_models import MCP_CORS_HEADERS, build_mcp_allow_headers

    cors_headers = dict(MCP_CORS_HEADERS)
    requested_headers = request.headers.get("Access-Control-Request-Headers")
    if requested_headers:
        cors_headers["Access-Control-Allow-Headers"] = build_mcp_allow_headers(requested_headers)
    return cors_headers


class GatewayAuthMiddleware(BaseHTTPMiddleware):
    """
    Gateway 鉴权中间件

    负责在鉴权失败（401/403）时，为 MCP 相关路径补齐 CORS 头，
    以避免浏览器端因缺少跨域头而阻断响应读取。
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self._auth_tokens = _load_gateway_auth_tokens()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if self._auth_tokens and _is_mcp_path(request.url.path):
            if request.method != "OPTIONS":
                token = _extract_bearer_token(request.headers.get("Authorization"))
                if token is None or token not in self._auth_tokens:
                    unauthorized_response = JSONResponse(
                        content={"detail": "Unauthorized"},
                        status_code=401,
                    )
                    unauthorized_response.headers.update(_build_mcp_cors_headers(request))
                    return unauthorized_response

        response = await call_next(request)

        if response.status_code not in (401, 403):
            return response
        if not _is_mcp_path(request.url.path):
            return response

        response.headers.update(_build_mcp_cors_headers(request))
        return response


# ===================== Exception Handlers =====================


def _create_unhandled_exception_handler():
    """
    创建未处理异常的处理器

    返回一个闭包函数，以支持延迟导入。

    契约保证:
    - 所有未处理异常返回的 error.data.correlation_id 与 X-Correlation-ID 一致
    """

    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """
        全局未处理异常处理器

        确保所有未被路由捕获的异常都返回:
        1. 正确的 JSON 格式
        2. correlation_id 在 header 和 body 中一致
        """
        # correlation_id 从统一模块导入（单一来源原则）
        from .mcp_rpc import (
            ErrorCategory,
            ErrorData,
            ErrorReason,
            JsonRpcErrorCode,
            generate_correlation_id,
            make_jsonrpc_error,
        )

        # 获取 correlation_id（优先从 contextvars，否则生成新的）
        correlation_id = get_request_correlation_id()
        strict_correlation_id = correlation_id is not None
        if correlation_id is None:
            correlation_id = generate_correlation_id()

        # 记录异常
        logger.exception(
            f"未处理的异常: correlation_id={correlation_id}, "
            f"path={request.url.path}, exception={exc}"
        )

        # 构建错误响应
        error_data = ErrorData(
            category=ErrorCategory.INTERNAL,
            reason=ErrorReason.UNHANDLED_EXCEPTION,
            retryable=False,
            correlation_id=correlation_id,
            details={"exception_type": type(exc).__name__},
        )

        error_data_dict = error_data.to_dict(strict=strict_correlation_id)
        public_message = sanitize_error_message(DEFAULT_PUBLIC_ERROR_MESSAGE)
        error_response = make_jsonrpc_error(
            None,
            JsonRpcErrorCode.INTERNAL_ERROR,
            public_message,
            data=error_data_dict,
        )

        response_headers: dict
        if _is_mcp_path(request.url.path):
            response_headers = _build_mcp_cors_headers(request)
        else:
            response_headers = {}
        response_headers["X-Correlation-ID"] = error_data_dict["correlation_id"]

        return JSONResponse(
            content=error_response.model_dump(exclude_none=True),
            status_code=500,
            headers=response_headers,
        )

    return unhandled_exception_handler


# ===================== Install Function =====================


def install_middleware(app: FastAPI) -> None:
    """
    安装所有中间件和异常处理器

    调用时机: 在 create_app() 中，register_routes() 之前调用

    安装内容:
    1. CorrelationIdMiddleware: 统一生成和传递 correlation_id
    2. 全局异常处理器: 捕获未处理的异常，确保返回正确格式

    Args:
        app: FastAPI 应用实例

    设计原则:
    - Import-Safe: 此函数内部使用延迟导入
    - 不引入新的 import-time 外部依赖
    """
    # 1. 注册全局异常处理器
    app.add_exception_handler(Exception, _create_unhandled_exception_handler())

    # 2. 添加 correlation_id 中间件
    # 注意: FastAPI 中间件按添加顺序的逆序执行（LIFO）
    # 所以 CorrelationIdMiddleware 应该最后添加，这样它最先执行
    app.add_middleware(CorrelationIdMiddleware)

    logger.debug("Gateway 中间件已安装")
