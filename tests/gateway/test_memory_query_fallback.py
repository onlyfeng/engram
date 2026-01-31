# -*- coding: utf-8 -*-
"""
memory_query Logbook fallback 测试

测试覆盖:
1. OpenMemory 成功时返回正常结果
2. OpenMemory 连接失败时降级到 Logbook 查询
3. OpenMemory API 错误时降级到 Logbook 查询
4. Logbook fallback 也失败时返回错误
5. 降级响应包含 degraded=True 标记
6. 所有响应包含 correlation_id
"""

import secrets
from unittest.mock import MagicMock, patch

import pytest

from engram.gateway.handlers.memory_query import MemoryQueryResponse, memory_query_impl

# 导入 Fake 依赖
from tests.gateway.fakes import (
    FakeGatewayConfig,
    FakeOpenMemoryClient,
)

# Mock 路径
HANDLER_MODULE = "engram.gateway.handlers.memory_query"


def _test_correlation_id():
    """生成测试用的 correlation_id"""
    return f"corr-{secrets.token_hex(8)}"


class TestMemoryQuerySuccess:
    """OpenMemory 成功场景测试"""

    @pytest.mark.asyncio
    async def test_openmemory_success_returns_results(self):
        """OpenMemory 成功时返回正常结果"""
        query = "test query"
        expected_results = [
            {"id": "mem_1", "content": "Result 1", "score": 0.95},
            {"id": "mem_2", "content": "Result 2", "score": 0.85},
        ]

        with (
            patch(f"{HANDLER_MODULE}.get_config") as mock_config,
            patch(f"{HANDLER_MODULE}.get_client") as mock_get_client,
        ):
            mock_config.return_value.default_team_space = "team:test"

            # 模拟 OpenMemory 成功
            mock_client = MagicMock()
            mock_search_result = MagicMock()
            mock_search_result.success = True
            mock_search_result.results = expected_results
            mock_search_result.error = None
            mock_client.search.return_value = mock_search_result
            mock_get_client.return_value = mock_client

            test_corr_id = _test_correlation_id()
            result = await memory_query_impl(query=query, correlation_id=test_corr_id)

            # 验证结果
            assert result.ok is True
            assert result.degraded is False
            assert result.results == expected_results
            assert result.total == len(expected_results)
            assert result.correlation_id == test_corr_id
            assert result.correlation_id.startswith("corr-")

    @pytest.mark.asyncio
    async def test_openmemory_success_with_custom_spaces(self):
        """OpenMemory 成功时使用自定义 spaces"""
        query = "test query"
        spaces = ["team:project1", "team:project2"]

        with (
            patch(f"{HANDLER_MODULE}.get_config") as mock_config,
            patch(f"{HANDLER_MODULE}.get_client") as mock_get_client,
        ):
            mock_config.return_value.default_team_space = "team:default"

            mock_client = MagicMock()
            mock_search_result = MagicMock()
            mock_search_result.success = True
            mock_search_result.results = []
            mock_client.search.return_value = mock_search_result
            mock_get_client.return_value = mock_client

            result = await memory_query_impl(
                query=query, spaces=spaces, correlation_id=_test_correlation_id()
            )

            # 验证 spaces_searched
            assert result.spaces_searched == spaces

            # 验证 OpenMemory 调用参数
            call_args = mock_client.search.call_args
            assert call_args[1]["filters"]["spaces"] == spaces

    @pytest.mark.asyncio
    async def test_openmemory_success_empty_results(self):
        """OpenMemory 成功但结果为空"""
        query = "no match query"

        with (
            patch(f"{HANDLER_MODULE}.get_config") as mock_config,
            patch(f"{HANDLER_MODULE}.get_client") as mock_get_client,
        ):
            mock_config.return_value.default_team_space = "team:test"

            mock_client = MagicMock()
            mock_search_result = MagicMock()
            mock_search_result.success = True
            mock_search_result.results = []
            mock_client.search.return_value = mock_search_result
            mock_get_client.return_value = mock_client

            result = await memory_query_impl(query=query, correlation_id=_test_correlation_id())

            assert result.ok is True
            assert result.results == []
            assert result.total == 0
            assert result.degraded is False


class TestMemoryQueryFallback:
    """
    OpenMemory 失败时 Logbook fallback 测试

    契约: OpenMemory 不可用时，降级到 Logbook knowledge_candidates 查询
    """

    @pytest.mark.asyncio
    async def test_connection_error_triggers_fallback(self):
        """
        关键路径测试: OpenMemory 连接失败触发 Logbook fallback
        """
        query = "test fallback"

        # 预期的 fallback 结果
        fallback_candidates = [
            {
                "candidate_id": 1,
                "content_md": "Fallback content 1",
                "title": "Fallback Title 1",
                "kind": "PROCEDURE",
                "confidence": 0.8,
                "evidence_refs_json": None,
                "created_at": None,
            }
        ]

        with (
            patch(f"{HANDLER_MODULE}.get_config") as mock_config,
            patch(f"{HANDLER_MODULE}.get_client") as mock_get_client,
            patch(f"{HANDLER_MODULE}.logbook_adapter") as mock_adapter,
        ):
            mock_config.return_value.default_team_space = "team:test"

            # 模拟 OpenMemory 连接失败
            from engram.gateway.openmemory_client import OpenMemoryError

            mock_client = MagicMock()
            mock_client.search.side_effect = OpenMemoryError(
                message="连接超时",
                status_code=None,
                response=None,
            )
            mock_get_client.return_value = mock_client

            # 模拟 Logbook fallback 查询成功
            mock_adapter.query_knowledge_candidates.return_value = fallback_candidates

            result = await memory_query_impl(query=query, correlation_id=_test_correlation_id())

            # 关键断言：应返回降级结果
            assert result.ok is True
            assert result.degraded is True
            assert "连接超时" in result.message

            # 验证 fallback 查询被调用
            mock_adapter.query_knowledge_candidates.assert_called_once()

            # 验证结果格式转换正确
            assert len(result.results) == 1
            assert result.results[0]["id"] == "kc_1"  # 前缀 kc_
            assert result.results[0]["content"] == "Fallback content 1"
            assert result.results[0]["source"] == "logbook_fallback"

    @pytest.mark.asyncio
    async def test_api_error_triggers_fallback(self):
        """
        关键路径测试: OpenMemory API 错误触发 Logbook fallback
        """
        query = "test api error"

        fallback_candidates = [
            {
                "candidate_id": 2,
                "content_md": "API error fallback",
                "title": "Title",
                "kind": "FACT",
                "confidence": 0.7,
                "evidence_refs_json": None,
                "created_at": None,
            }
        ]

        with (
            patch(f"{HANDLER_MODULE}.get_config") as mock_config,
            patch(f"{HANDLER_MODULE}.get_client") as mock_get_client,
            patch(f"{HANDLER_MODULE}.logbook_adapter") as mock_adapter,
        ):
            mock_config.return_value.default_team_space = "team:test"

            # 模拟 OpenMemory API 503 错误
            from engram.gateway.openmemory_client import OpenMemoryError

            mock_client = MagicMock()
            mock_client.search.side_effect = OpenMemoryError(
                message="Service Unavailable",
                status_code=503,
                response={"error": "Service Unavailable"},
            )
            mock_get_client.return_value = mock_client

            mock_adapter.query_knowledge_candidates.return_value = fallback_candidates

            result = await memory_query_impl(query=query, correlation_id=_test_correlation_id())

            # 验证降级结果
            assert result.ok is True
            assert result.degraded is True
            assert len(result.results) == 1

    @pytest.mark.asyncio
    async def test_fallback_passes_correct_parameters(self):
        """
        验证 fallback 查询传递正确参数
        """
        query = "parameter test"
        top_k = 5
        spaces = ["team:myproject"]
        filters = {"evidence": "commit:abc"}

        with (
            patch(f"{HANDLER_MODULE}.get_config") as mock_config,
            patch(f"{HANDLER_MODULE}.get_client") as mock_get_client,
            patch(f"{HANDLER_MODULE}.logbook_adapter") as mock_adapter,
        ):
            mock_config.return_value.default_team_space = "team:default"

            # 模拟 OpenMemory 失败
            from engram.gateway.openmemory_client import OpenMemoryError

            mock_client = MagicMock()
            mock_client.search.side_effect = OpenMemoryError(message="Error")
            mock_get_client.return_value = mock_client

            mock_adapter.query_knowledge_candidates.return_value = []

            await memory_query_impl(
                query=query,
                top_k=top_k,
                spaces=spaces,
                filters=filters,
                correlation_id=_test_correlation_id(),
            )

            # 验证 fallback 查询参数
            call_kwargs = mock_adapter.query_knowledge_candidates.call_args[1]
            assert call_kwargs["keyword"] == query
            assert call_kwargs["top_k"] == top_k
            assert call_kwargs["space_filter"] == spaces[0]
            assert call_kwargs["evidence_filter"] == filters["evidence"]

    @pytest.mark.asyncio
    async def test_fallback_also_fails_returns_error(self):
        """
        关键路径测试: OpenMemory 和 Logbook fallback 都失败时返回错误
        """
        query = "double failure"

        with (
            patch(f"{HANDLER_MODULE}.get_config") as mock_config,
            patch(f"{HANDLER_MODULE}.get_client") as mock_get_client,
            patch(f"{HANDLER_MODULE}.logbook_adapter") as mock_adapter,
        ):
            mock_config.return_value.default_team_space = "team:test"

            # 模拟 OpenMemory 失败
            from engram.gateway.openmemory_client import OpenMemoryError

            mock_client = MagicMock()
            mock_client.search.side_effect = OpenMemoryError(message="OM Error")
            mock_get_client.return_value = mock_client

            # 模拟 Logbook fallback 也失败
            mock_adapter.query_knowledge_candidates.side_effect = Exception("DB Error")

            result = await memory_query_impl(query=query, correlation_id=_test_correlation_id())

            # 关键断言：应返回错误但标记为 degraded
            assert result.ok is False
            assert result.degraded is True
            assert "OM Error" in result.message
            assert "DB Error" in result.message
            assert result.results == []

    @pytest.mark.asyncio
    async def test_fallback_converts_result_format_correctly(self):
        """
        验证 fallback 结果格式转换正确
        """
        query = "format test"

        # 完整的 knowledge_candidate 记录
        candidates = [
            {
                "candidate_id": 100,
                "content_md": "# Complete Content\n\nWith all fields.",
                "title": "Complete Title",
                "kind": "PITFALL",
                "confidence": 0.95,
                "evidence_refs_json": {"refs": ["commit:xyz"]},
                "created_at": "2026-01-01T00:00:00Z",
            },
            {
                "candidate_id": 101,
                "content_md": "Minimal content",
                "title": None,  # 允许为 None
                "kind": None,
                "confidence": None,
                "evidence_refs_json": None,
                "created_at": None,
            },
        ]

        with (
            patch(f"{HANDLER_MODULE}.get_config") as mock_config,
            patch(f"{HANDLER_MODULE}.get_client") as mock_get_client,
            patch(f"{HANDLER_MODULE}.logbook_adapter") as mock_adapter,
        ):
            mock_config.return_value.default_team_space = "team:test"

            from engram.gateway.openmemory_client import OpenMemoryError

            mock_client = MagicMock()
            mock_client.search.side_effect = OpenMemoryError(message="Error")
            mock_get_client.return_value = mock_client

            mock_adapter.query_knowledge_candidates.return_value = candidates

            result = await memory_query_impl(query=query, correlation_id=_test_correlation_id())

            # 验证结果格式
            assert len(result.results) == 2

            # 第一个结果（完整字段）
            r1 = result.results[0]
            assert r1["id"] == "kc_100"
            assert r1["content"] == "# Complete Content\n\nWith all fields."
            assert r1["title"] == "Complete Title"
            assert r1["kind"] == "PITFALL"
            assert r1["confidence"] == 0.95
            assert r1["evidence_refs"] == {"refs": ["commit:xyz"]}
            assert r1["created_at"] == "2026-01-01T00:00:00Z"
            assert r1["source"] == "logbook_fallback"

            # 第二个结果（部分字段为 None）
            r2 = result.results[1]
            assert r2["id"] == "kc_101"
            assert r2["title"] is None
            assert r2["source"] == "logbook_fallback"


class TestMemoryQueryCorrelationId:
    """
    correlation_id 契约测试

    验证所有响应场景都包含正确格式的 correlation_id
    """

    @pytest.mark.asyncio
    async def test_success_response_has_correlation_id(self):
        """成功响应包含 correlation_id"""
        with (
            patch(f"{HANDLER_MODULE}.get_config") as mock_config,
            patch(f"{HANDLER_MODULE}.get_client") as mock_get_client,
        ):
            mock_config.return_value.default_team_space = "team:test"

            mock_client = MagicMock()
            mock_search_result = MagicMock()
            mock_search_result.success = True
            mock_search_result.results = []
            mock_client.search.return_value = mock_search_result
            mock_get_client.return_value = mock_client

            test_corr_id = _test_correlation_id()
            result = await memory_query_impl(query="test", correlation_id=test_corr_id)

            assert result.correlation_id == test_corr_id
            assert result.correlation_id.startswith("corr-")
            assert len(result.correlation_id) == 21  # corr- + 16 hex

    @pytest.mark.asyncio
    async def test_fallback_response_has_correlation_id(self):
        """降级响应包含 correlation_id"""
        with (
            patch(f"{HANDLER_MODULE}.get_config") as mock_config,
            patch(f"{HANDLER_MODULE}.get_client") as mock_get_client,
            patch(f"{HANDLER_MODULE}.logbook_adapter") as mock_adapter,
        ):
            mock_config.return_value.default_team_space = "team:test"

            from engram.gateway.openmemory_client import OpenMemoryError

            mock_client = MagicMock()
            mock_client.search.side_effect = OpenMemoryError(message="Error")
            mock_get_client.return_value = mock_client

            mock_adapter.query_knowledge_candidates.return_value = []

            test_corr_id = _test_correlation_id()
            result = await memory_query_impl(query="test", correlation_id=test_corr_id)

            assert result.degraded is True
            assert result.correlation_id == test_corr_id
            assert result.correlation_id.startswith("corr-")

    @pytest.mark.asyncio
    async def test_error_response_has_correlation_id(self):
        """错误响应包含 correlation_id"""
        with (
            patch(f"{HANDLER_MODULE}.get_config") as mock_config,
            patch(f"{HANDLER_MODULE}.get_client") as mock_get_client,
            patch(f"{HANDLER_MODULE}.logbook_adapter") as mock_adapter,
        ):
            mock_config.return_value.default_team_space = "team:test"

            from engram.gateway.openmemory_client import OpenMemoryError

            mock_client = MagicMock()
            mock_client.search.side_effect = OpenMemoryError(message="Error")
            mock_get_client.return_value = mock_client

            mock_adapter.query_knowledge_candidates.side_effect = Exception("DB Error")

            test_corr_id = _test_correlation_id()
            result = await memory_query_impl(query="test", correlation_id=test_corr_id)

            assert result.ok is False
            assert result.correlation_id == test_corr_id
            assert result.correlation_id.startswith("corr-")

    @pytest.mark.asyncio
    async def test_provided_correlation_id_preserved(self):
        """提供的 correlation_id 被保留"""
        provided_id = "corr-1234567890abcdef"

        with (
            patch(f"{HANDLER_MODULE}.get_config") as mock_config,
            patch(f"{HANDLER_MODULE}.get_client") as mock_get_client,
        ):
            mock_config.return_value.default_team_space = "team:test"

            mock_client = MagicMock()
            mock_search_result = MagicMock()
            mock_search_result.success = True
            mock_search_result.results = []
            mock_client.search.return_value = mock_search_result
            mock_get_client.return_value = mock_client

            result = await memory_query_impl(query="test", correlation_id=provided_id)

            # 关键断言：应保留提供的 correlation_id
            assert result.correlation_id == provided_id


class TestMemoryQueryWithFakeDependencies:
    """
    使用 Fake 依赖的集成测试
    """

    @pytest.mark.asyncio
    async def test_with_fake_client_success(self):
        """使用 FakeOpenMemoryClient 成功场景"""
        fake_config = FakeGatewayConfig()

        fake_client = FakeOpenMemoryClient()
        expected_results = [{"id": "fake_1", "content": "fake content"}]
        fake_client.configure_search_success(results=expected_results)

        result = await memory_query_impl(
            query="test",
            correlation_id=_test_correlation_id(),
            _config=fake_config,
            _openmemory_client=fake_client,
        )

        assert result.ok is True
        assert result.degraded is False
        assert result.results == expected_results

        # 验证 fake_client 被调用
        assert len(fake_client.search_calls) == 1
        assert fake_client.search_calls[0]["query"] == "test"

    @pytest.mark.asyncio
    async def test_with_fake_client_connection_error(self):
        """使用 FakeOpenMemoryClient 连接错误场景"""
        fake_config = FakeGatewayConfig()

        fake_client = FakeOpenMemoryClient()
        fake_client.configure_search_connection_error("Fake 连接超时")

        # 注意：fake 异常需要转换为真实异常
        # 这里直接使用 mock 来模拟真实场景
        from engram.gateway.openmemory_client import OpenMemoryError

        with (
            patch(f"{HANDLER_MODULE}.get_client") as mock_get_client,
            patch(f"{HANDLER_MODULE}.logbook_adapter") as mock_adapter,
        ):
            mock_client = MagicMock()
            mock_client.search.side_effect = OpenMemoryError(message="Fake 连接超时")
            mock_get_client.return_value = mock_client

            mock_adapter.query_knowledge_candidates.return_value = []

            result = await memory_query_impl(
                query="test",
                correlation_id=_test_correlation_id(),
                _config=fake_config,
            )

            assert result.degraded is True
            assert "Fake 连接超时" in result.message


class TestMemoryQueryInternalError:
    """内部错误处理测试"""

    @pytest.mark.asyncio
    async def test_unexpected_exception_handled(self):
        """未预期异常被正确处理"""
        with (
            patch(f"{HANDLER_MODULE}.get_config") as mock_config,
            patch(f"{HANDLER_MODULE}.get_client") as mock_get_client,
        ):
            mock_config.return_value.default_team_space = "team:test"

            # 模拟未预期异常
            mock_client = MagicMock()
            mock_client.search.side_effect = RuntimeError("未预期的内部错误")
            mock_get_client.return_value = mock_client

            test_corr_id = _test_correlation_id()
            result = await memory_query_impl(query="test", correlation_id=test_corr_id)

            # 应返回内部错误
            assert result.ok is False
            assert "内部错误" in result.message
            assert "未预期的内部错误" in result.message
            assert result.correlation_id == test_corr_id

    @pytest.mark.asyncio
    async def test_key_error_handled(self):
        """KeyError 被正确处理"""
        with (
            patch(f"{HANDLER_MODULE}.get_config") as mock_config,
            patch(f"{HANDLER_MODULE}.get_client") as mock_get_client,
        ):
            mock_config.return_value.default_team_space = "team:test"

            mock_client = MagicMock()
            mock_client.search.side_effect = KeyError("missing_key")
            mock_get_client.return_value = mock_client

            result = await memory_query_impl(query="test", correlation_id=_test_correlation_id())

            assert result.ok is False
            assert "内部错误" in result.message


class TestMemoryQueryResponseModel:
    """MemoryQueryResponse 模型测试"""

    def test_response_model_fields(self):
        """验证响应模型包含所有必需字段"""
        response = MemoryQueryResponse(
            ok=True,
            results=[],
            total=0,
            spaces_searched=["team:test"],
        )

        # 验证字段存在
        assert hasattr(response, "ok")
        assert hasattr(response, "results")
        assert hasattr(response, "total")
        assert hasattr(response, "spaces_searched")
        assert hasattr(response, "message")
        assert hasattr(response, "degraded")
        assert hasattr(response, "correlation_id")

    def test_response_model_defaults(self):
        """验证响应模型默认值"""
        response = MemoryQueryResponse(
            ok=True,
            results=[],
            total=0,
            spaces_searched=[],
        )

        # 验证默认值
        assert response.message is None
        assert response.degraded is False
        assert response.correlation_id is None

    def test_response_model_serialization(self):
        """验证响应模型序列化"""
        response = MemoryQueryResponse(
            ok=True,
            results=[{"id": "1"}],
            total=1,
            spaces_searched=["team:test"],
            degraded=True,
            correlation_id="corr-1234567890abcdef",
        )

        data = response.model_dump()

        assert data["ok"] is True
        assert data["results"] == [{"id": "1"}]
        assert data["degraded"] is True
        assert data["correlation_id"] == "corr-1234567890abcdef"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
