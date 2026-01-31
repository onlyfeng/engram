"""
correlation_id 代理层测试

验证代理层（logbook_db、mcp_rpc、handlers）不引入新的 correlation_id 分裂。

核心契约:
1. correlation_id 只在 HTTP 入口层生成一次
2. 所有下游组件必须使用传入的 correlation_id
3. 错误响应中的 correlation_id 必须与请求保持一致

测试场景:
- logbook_db 代理层透传 correlation_id
- mcp_rpc dispatch 正确设置和传递 correlation_id
- handlers 使用传入的 correlation_id 而非自行生成
- 错误响应保持 correlation_id 一致性
"""

import pytest

from engram.gateway.mcp_rpc import (
    ErrorCategory,
    ErrorData,
    ErrorReason,
    JsonRpcRequest,
    JsonRpcRouter,
    generate_correlation_id,
    get_current_correlation_id,
    set_current_correlation_id,
    to_jsonrpc_error,
)


class TestCorrelationIdSingleSource:
    """验证 correlation_id 单一来源原则"""

    def test_generate_correlation_id_format(self):
        """correlation_id 应有稳定的格式前缀"""
        corr_id = generate_correlation_id()
        assert corr_id.startswith("corr-")
        assert len(corr_id) == 21  # "corr-" + 16 hex chars

    def test_contextvars_propagation(self):
        """correlation_id 应通过 contextvars 正确传递"""
        # 使用符合 schema 格式的 correlation_id（corr-{16位十六进制}）
        test_corr_id = "corr-a1b2c3d4e5f67001"

        # 设置 correlation_id
        token = set_current_correlation_id(test_corr_id)

        try:
            # 验证可以获取到设置的值
            assert get_current_correlation_id() == test_corr_id
        finally:
            # 恢复原值
            from engram.gateway.mcp_rpc import _current_correlation_id

            _current_correlation_id.reset(token)

    def test_contextvars_isolation(self):
        """不同请求的 correlation_id 应该隔离"""
        # 默认应为 None
        assert get_current_correlation_id() is None

        # 设置后应可获取
        token = set_current_correlation_id("corr-a1b2c3d4e5f67002")
        assert get_current_correlation_id() == "corr-a1b2c3d4e5f67002"

        # 重置后应回到 None
        from engram.gateway.mcp_rpc import _current_correlation_id

        _current_correlation_id.reset(token)
        assert get_current_correlation_id() is None


class TestMcpRpcCorrelationIdPropagation:
    """验证 mcp_rpc 模块的 correlation_id 传递"""

    @pytest.mark.asyncio
    async def test_dispatch_sets_correlation_id(self):
        """dispatch 应正确设置 correlation_id 供 handler 使用"""
        router = JsonRpcRouter()
        captured_corr_id = None

        @router.method("test/method")
        async def test_handler(params):
            nonlocal captured_corr_id
            captured_corr_id = get_current_correlation_id()
            return {"ok": True}

        request = JsonRpcRequest(method="test/method", params={})
        # 使用符合 schema 格式的 correlation_id（corr-{16位十六进制}）
        test_corr_id = "corr-a1b2c3d4e5f67890"

        await router.dispatch(request, correlation_id=test_corr_id)

        # 合规的 correlation_id 应被保留
        assert captured_corr_id == test_corr_id

    @pytest.mark.asyncio
    async def test_dispatch_generates_correlation_id_if_not_provided(self):
        """若未提供 correlation_id，dispatch 应自动生成"""
        router = JsonRpcRouter()
        captured_corr_id = None

        @router.method("test/generate")
        async def test_handler(params):
            nonlocal captured_corr_id
            captured_corr_id = get_current_correlation_id()
            return {"ok": True}

        request = JsonRpcRequest(method="test/generate", params={})

        await router.dispatch(request)  # 不传 correlation_id

        # 应自动生成
        assert captured_corr_id is not None
        assert captured_corr_id.startswith("corr-")

    @pytest.mark.asyncio
    async def test_error_response_preserves_correlation_id(self):
        """错误响应应保持原始 correlation_id"""
        router = JsonRpcRouter()

        @router.method("test/error")
        async def test_handler(params):
            raise ValueError("test error")

        request = JsonRpcRequest(method="test/error", params={})
        # 使用符合 schema 格式的 correlation_id
        test_corr_id = "corr-e1f2a3b4c5d67890"

        response = await router.dispatch(request, correlation_id=test_corr_id)

        # 错误响应应包含原始 correlation_id（合规格式被保留）
        assert response.error is not None
        assert response.error.data is not None
        assert response.error.data.get("correlation_id") == test_corr_id


class TestToJsonrpcErrorCorrelationId:
    """验证 to_jsonrpc_error 的 correlation_id 处理"""

    def test_preserves_provided_correlation_id(self):
        """应使用提供的 correlation_id"""
        error = ValueError("test error")
        # 使用符合 schema 格式的 correlation_id（corr-{16位十六进制}）
        test_corr_id = "corr-a1b2c3d4e5f67003"

        response = to_jsonrpc_error(
            error=error,
            req_id=1,
            correlation_id=test_corr_id,
        )

        assert response.error.data["correlation_id"] == test_corr_id

    def test_generates_correlation_id_if_not_provided(self):
        """若未提供，应生成新的 correlation_id"""
        error = ValueError("test error")

        response = to_jsonrpc_error(
            error=error,
            req_id=1,
            correlation_id=None,
        )

        # 应自动生成
        corr_id = response.error.data["correlation_id"]
        assert corr_id is not None
        assert corr_id.startswith("corr-")

    def test_gateway_error_correlation_id_priority(self):
        """GatewayError 的 correlation_id 应被覆盖参数取代"""
        from engram.gateway.mcp_rpc import GatewayError

        # 使用符合 schema 格式的 correlation_id（corr-{16位十六进制}）
        error = GatewayError(
            message="test",
            correlation_id="corr-a1b2c3d4e5f67004",
        )
        override_corr_id = "corr-a1b2c3d4e5f67005"

        response = to_jsonrpc_error(
            error=error,
            req_id=1,
            correlation_id=override_corr_id,
        )

        # 覆盖参数优先
        assert response.error.data["correlation_id"] == override_corr_id


class TestErrorDataCorrelationId:
    """验证 ErrorData 的 correlation_id 处理"""

    def test_to_dict_preserves_correlation_id(self):
        """to_dict 应保留 correlation_id"""
        # 使用符合 schema 格式的 correlation_id（corr-{16位十六进制}）
        test_corr_id = "corr-a1b2c3d4e5f67006"
        error_data = ErrorData(
            category=ErrorCategory.VALIDATION,
            reason=ErrorReason.MISSING_REQUIRED_PARAM,
            correlation_id=test_corr_id,
        )

        d = error_data.to_dict()
        assert d["correlation_id"] == test_corr_id

    def test_to_dict_generates_if_missing(self):
        """to_dict 在 correlation_id 缺失时应生成"""
        error_data = ErrorData(
            category=ErrorCategory.VALIDATION,
            reason=ErrorReason.MISSING_REQUIRED_PARAM,
            correlation_id=None,
        )

        d = error_data.to_dict()
        assert d["correlation_id"] is not None
        assert d["correlation_id"].startswith("corr-")


class TestHandlerCorrelationIdRequirement:
    """验证 handler 对 correlation_id 的要求"""

    @pytest.mark.asyncio
    async def test_memory_store_requires_correlation_id(self):
        """memory_store_impl 必须接收 correlation_id"""
        from unittest.mock import MagicMock

        from engram.gateway.di import GatewayDeps
        from engram.gateway.handlers.memory_store import memory_store_impl

        # 创建 mock deps（用于触发 correlation_id 校验，在此之前会抛出 ValueError）
        mock_config = MagicMock()
        mock_deps = GatewayDeps.for_testing(config=mock_config)

        with pytest.raises(ValueError) as exc_info:
            await memory_store_impl(
                payload_md="test",
                correlation_id=None,  # 显式传 None
                deps=mock_deps,
            )

        assert "correlation_id 是必需参数" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_memory_query_requires_correlation_id(self):
        """memory_query_impl 必须接收 correlation_id"""
        from unittest.mock import MagicMock

        from engram.gateway.di import GatewayDeps
        from engram.gateway.handlers.memory_query import memory_query_impl

        # 创建 mock deps（用于触发 correlation_id 校验，在此之前会抛出 ValueError）
        mock_config = MagicMock()
        mock_deps = GatewayDeps.for_testing(config=mock_config)

        with pytest.raises(ValueError) as exc_info:
            await memory_query_impl(
                query="test",
                correlation_id=None,  # 显式传 None
                deps=mock_deps,
            )

        assert "correlation_id 是必需参数" in str(exc_info.value)


class TestProxyLayerNoNewCorrelationId:
    """验证代理层不引入新的 correlation_id"""

    def test_logbook_db_is_thin_proxy(self):
        """logbook_db 应只做薄代理，不生成 correlation_id"""
        # 验证模块 docstring 中有相关说明
        from engram.gateway import logbook_db

        docstring = logbook_db.__doc__
        assert "薄代理" in docstring or "thin proxy" in docstring.lower()
        assert "correlation_id" in docstring

    def test_mcp_rpc_documents_single_source(self):
        """mcp_rpc 模块应记录单一来源原则"""
        from engram.gateway import mcp_rpc

        docstring = mcp_rpc.__doc__
        assert "单一来源" in docstring or "single source" in docstring.lower()
        assert "correlation_id" in docstring
