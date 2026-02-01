"""
correlation_id 代理层测试

验证代理层（logbook_db、mcp_rpc、handlers）不引入新的 correlation_id 分裂。

核心契约:
1. correlation_id 只在 HTTP 入口层生成一次
2. 所有下游组件必须使用传入的 correlation_id
3. 错误响应中的 correlation_id 必须与请求保持一致
4. correlation_id 函数从 correlation_id.py 模块统一导出（单一来源原则）

测试场景:
- correlation_id 模块函数正确性和单一来源契约
- logbook_db 代理层透传 correlation_id
- mcp_rpc dispatch 正确设置和传递 correlation_id
- handlers 使用传入的 correlation_id 而非自行生成
- 错误响应保持 correlation_id 一致性
"""

import pytest

# 优先从 correlation_id 模块导入（单一来源原则）
from engram.gateway.correlation_id import (
    CORRELATION_ID_PATTERN,
    generate_correlation_id,
    is_valid_correlation_id,
    normalize_correlation_id,
)
from engram.gateway.mcp_rpc import (
    ErrorCategory,
    ErrorData,
    ErrorReason,
    JsonRpcRequest,
    JsonRpcRouter,
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


# ===================== 错误输入不污染系统测试 =====================


class TestCorrelationIdErrorInputSanitization:
    """
    验证错误输入不污染系统

    契约要点：
    1. 不合规的 correlation_id 被归一化，不会泄漏到系统中
    2. 恶意输入（注入尝试、特殊字符）被安全处理
    3. 错误响应始终包含合规格式的 correlation_id
    """

    def test_malformed_correlation_id_not_leaked_to_error_data(self):
        """不合规 correlation_id 不应泄漏到 ErrorData 中"""
        malformed_ids = [
            "test-abc123",  # 错误前缀
            "corr-test",  # 太短
            "corr-ghijklmnopqrstuv",  # 非十六进制
            "<script>alert(1)</script>",  # XSS 尝试
            "'; DROP TABLE users; --",  # SQL 注入尝试
            "",  # 空字符串
        ]

        for malformed_id in malformed_ids:
            error_data = ErrorData(
                category=ErrorCategory.VALIDATION,
                reason=ErrorReason.INVALID_PARAM_VALUE,
                correlation_id=malformed_id,
            )

            d = error_data.to_dict()
            result_id = d["correlation_id"]

            # 结果应该是合规格式，而非原始恶意输入
            assert is_valid_correlation_id(result_id), (
                f"ErrorData.to_dict() 应返回合规 correlation_id，"
                f"输入: {malformed_id!r}，输出: {result_id!r}"
            )
            # 不应该是原始恶意输入（除非恰好合规，但上面的输入都不合规）
            assert result_id != malformed_id, f"不合规输入不应原样泄漏：{malformed_id!r}"

    def test_normalize_sanitizes_malicious_input(self):
        """normalize_correlation_id 应安全处理恶意输入"""
        malicious_inputs = [
            None,
            "",
            "' OR '1'='1",
            "corr-<img src=x onerror=alert(1)>",
            "corr-" + "a" * 100,  # 超长
            "\x00\x01\x02",  # 控制字符
        ]

        for malicious in malicious_inputs:
            result = normalize_correlation_id(malicious)

            # 结果必须是合规格式
            assert is_valid_correlation_id(result), (
                f"normalize 应返回合规格式，输入: {malicious!r}，输出: {result!r}"
            )
            # 长度应为 21
            assert len(result) == 21, f"长度应为 21: {result}"

    @pytest.mark.asyncio
    async def test_dispatch_sanitizes_invalid_correlation_id(self):
        """dispatch 应归一化不合规的 correlation_id"""
        router = JsonRpcRouter()
        captured_corr_id = None

        @router.method("test/sanitize")
        async def test_handler(params):
            nonlocal captured_corr_id
            captured_corr_id = get_current_correlation_id()
            return {"ok": True}

        request = JsonRpcRequest(method="test/sanitize", params={})

        # 传入不合规的 correlation_id
        await router.dispatch(request, correlation_id="invalid-corr-id")

        # handler 应收到归一化后的合规 correlation_id
        assert is_valid_correlation_id(captured_corr_id), (
            f"dispatch 应归一化 correlation_id，结果: {captured_corr_id}"
        )

    @pytest.mark.asyncio
    async def test_error_response_always_has_valid_correlation_id(self):
        """错误响应始终包含合规格式的 correlation_id"""
        router = JsonRpcRouter()

        @router.method("test/error_sanitize")
        async def test_handler(params):
            raise ValueError("test error")

        request = JsonRpcRequest(method="test/error_sanitize", params={})

        # 传入不合规的 correlation_id
        response = await router.dispatch(request, correlation_id="bad-id")

        # 错误响应应包含合规 correlation_id
        assert response.error is not None
        result_corr_id = response.error.data.get("correlation_id")
        assert is_valid_correlation_id(result_corr_id), (
            f"错误响应应包含合规 correlation_id: {result_corr_id}"
        )

    def test_to_jsonrpc_error_sanitizes_correlation_id(self):
        """to_jsonrpc_error 应归一化不合规的 correlation_id"""
        invalid_ids = [None, "", "invalid", "corr-short"]

        for invalid_id in invalid_ids:
            response = to_jsonrpc_error(
                error=ValueError("test"),
                req_id=1,
                correlation_id=invalid_id,
            )

            result_corr_id = response.error.data["correlation_id"]
            assert is_valid_correlation_id(result_corr_id), (
                f"to_jsonrpc_error 应归一化，输入: {invalid_id!r}，输出: {result_corr_id!r}"
            )


# ===================== correlation_id 模块单一来源契约测试 =====================


class TestCorrelationIdModuleSingleSource:
    """
    验证 correlation_id 模块是所有 correlation_id 函数的单一来源

    契约要点：
    1. correlation_id.py 模块提供 generate/is_valid/normalize 三个核心函数
    2. 其他模块（mcp_rpc/di/dependencies/middleware）从 correlation_id.py 导入
    3. 向后兼容：从 mcp_rpc 导入仍然有效
    """

    def test_correlation_id_module_exports_all_functions(self):
        """correlation_id 模块应导出所有核心函数"""
        from engram.gateway import correlation_id

        # 核心函数
        assert hasattr(correlation_id, "generate_correlation_id")
        assert hasattr(correlation_id, "is_valid_correlation_id")
        assert hasattr(correlation_id, "normalize_correlation_id")
        # 正则常量
        assert hasattr(correlation_id, "CORRELATION_ID_PATTERN")

    def test_generate_correlation_id_format(self):
        """generate_correlation_id 应生成符合 schema 的格式"""
        corr_id = generate_correlation_id()

        # 格式检查
        assert corr_id.startswith("corr-"), f"应以 'corr-' 开头: {corr_id}"
        assert len(corr_id) == 21, f"长度应为 21: {corr_id}"

        # 后缀应为 16 位十六进制
        suffix = corr_id[5:]
        int(suffix, 16)  # 如果不是十六进制会抛出 ValueError

    def test_is_valid_correlation_id_accepts_valid_format(self):
        """is_valid_correlation_id 应接受合规格式"""
        # 合规格式
        assert is_valid_correlation_id("corr-a1b2c3d4e5f67890") is True
        assert is_valid_correlation_id("corr-0000000000000000") is True
        assert is_valid_correlation_id("corr-ABCDEF1234567890") is True

    def test_is_valid_correlation_id_rejects_invalid_format(self):
        """is_valid_correlation_id 应拒绝不合规格式"""
        # 不合规格式
        assert is_valid_correlation_id(None) is False
        assert is_valid_correlation_id("") is False
        assert is_valid_correlation_id("corr-test") is False  # 太短
        assert is_valid_correlation_id("corr-a1b2c3d4e5f6789") is False  # 15 位
        assert is_valid_correlation_id("corr-a1b2c3d4e5f678901") is False  # 17 位
        assert is_valid_correlation_id("test-a1b2c3d4e5f67890") is False  # 前缀错误
        assert is_valid_correlation_id("corr-ghijklmnopqrstuv") is False  # 非十六进制

    def test_normalize_correlation_id_preserves_valid(self):
        """normalize_correlation_id 应保留合规的 correlation_id"""
        valid_id = "corr-a1b2c3d4e5f67890"
        result = normalize_correlation_id(valid_id)
        assert result == valid_id

    def test_normalize_correlation_id_generates_new_for_invalid(self):
        """normalize_correlation_id 应为不合规的 correlation_id 生成新值"""
        # 不合规输入
        result = normalize_correlation_id("invalid")
        assert result.startswith("corr-")
        assert len(result) == 21
        assert result != "invalid"

        # None 输入
        result = normalize_correlation_id(None)
        assert result.startswith("corr-")
        assert len(result) == 21

    def test_pattern_matches_valid_format(self):
        """CORRELATION_ID_PATTERN 应匹配合规格式"""
        import re

        # 合规格式
        assert re.match(CORRELATION_ID_PATTERN, "corr-a1b2c3d4e5f67890")
        assert re.match(CORRELATION_ID_PATTERN, "corr-0000000000000000")
        assert re.match(CORRELATION_ID_PATTERN, "corr-ABCDEF1234567890")

        # 不合规格式
        assert not re.match(CORRELATION_ID_PATTERN, "corr-test")
        assert not re.match(CORRELATION_ID_PATTERN, "test-a1b2c3d4e5f67890")

    def test_backward_compatibility_mcp_rpc_import(self):
        """向后兼容：从 mcp_rpc 导入应仍然有效"""
        # 这些导入应该成功（虽然内部是从 correlation_id 重新导出）
        from engram.gateway.mcp_rpc import (
            CORRELATION_ID_PATTERN as MCP_PATTERN,
        )
        from engram.gateway.mcp_rpc import (
            generate_correlation_id as mcp_generate,
        )
        from engram.gateway.mcp_rpc import (
            is_valid_correlation_id as mcp_is_valid,
        )
        from engram.gateway.mcp_rpc import (
            normalize_correlation_id as mcp_normalize,
        )

        # 验证功能一致
        corr_id = mcp_generate()
        assert corr_id.startswith("corr-")
        assert mcp_is_valid(corr_id) is True
        assert mcp_normalize(corr_id) == corr_id
        assert MCP_PATTERN.match(corr_id)

    def test_di_module_uses_correlation_id_module(self):
        """di 模块应从 correlation_id 模块导入"""
        from engram.gateway.di import generate_correlation_id as di_generate

        # 验证功能一致
        corr_id = di_generate()
        assert corr_id.startswith("corr-")
        assert is_valid_correlation_id(corr_id) is True

    def test_all_modules_generate_same_format(self):
        """所有模块生成的 correlation_id 格式应一致"""
        from engram.gateway.correlation_id import (
            generate_correlation_id as corr_generate,
        )
        from engram.gateway.di import generate_correlation_id as di_generate
        from engram.gateway.mcp_rpc import generate_correlation_id as mcp_generate

        # 各模块生成的 correlation_id 格式一致
        for generate_fn in [corr_generate, di_generate, mcp_generate]:
            corr_id = generate_fn()
            assert corr_id.startswith("corr-"), f"格式错误: {corr_id}"
            assert len(corr_id) == 21, f"长度错误: {corr_id}"
            assert is_valid_correlation_id(corr_id), f"不合规: {corr_id}"
