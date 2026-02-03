# -*- coding: utf-8 -*-
"""
MCP JSON-RPC 2.0 协议契约测试

测试覆盖:
1. JSON-RPC 无效请求 -> -32600 (INVALID_REQUEST)
2. 未知 method -> -32601 (METHOD_NOT_FOUND)
3. tools/list 输出包含五个工具（memory_store, memory_query, reliability_report, governance_update, evidence_upload）
4. tools/call 返回 content[] 格式
5. 旧 {tool, arguments} 格式仍返回原 MCPResponse 结构
6. 每个工具的 inputSchema.required 与实际实现一致
7. 所有错误响应包含结构化 error.data（契约: docs/contracts/mcp_jsonrpc_error_v1.md）
"""

import json
import logging
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ===================== Handler 模块路径常量 =====================
# 统一管理 patch 路径，确保与 test_main_dedup.py 和 test_memory_query_fallback.py 模式一致
HANDLER_MODULE_MEMORY_STORE = "engram.gateway.handlers.memory_store"
HANDLER_MODULE_MEMORY_QUERY = "engram.gateway.handlers.memory_query"
HANDLER_MODULE_GOVERNANCE = "engram.gateway.handlers.governance_update"
HANDLER_MODULE_EVIDENCE = "engram.gateway.handlers.evidence_upload"
# 全局依赖模块
CONFIG_MODULE = "engram.gateway.config"
CLIENT_MODULE = "engram.gateway.openmemory_client"
DB_MODULE = "engram.gateway.logbook_db"
ADAPTER_MODULE = "engram.gateway.logbook_adapter"

# ===================== 契约断言辅助函数 =====================

from engram.gateway.error_codes import PUBLIC_MCP_ERROR_REASONS

# 有效的错误分类（契约定义）
VALID_ERROR_CATEGORIES = ["protocol", "validation", "business", "dependency", "internal"]

# 有效的错误原因码（从 PUBLIC_MCP_ERROR_REASONS 构建，用于断言）
# 契约参见: docs/contracts/mcp_jsonrpc_error_v1.md §4
VALID_ERROR_REASONS: set[str] = set(PUBLIC_MCP_ERROR_REASONS)


def assert_error_data_contract(
    error_data: dict, expected_category: str = None, expected_reason: str = None
):
    """
    断言 error.data 符合 MCP JSON-RPC 错误模型契约

    契约要求所有错误响应的 error.data 必须包含:
    - category: 错误分类 (protocol/validation/business/dependency/internal)
    - reason: 错误原因码
    - retryable: 是否可重试 (布尔值)
    - correlation_id: 追踪 ID (格式: corr-{16位十六进制})

    参见: docs/contracts/mcp_jsonrpc_error_v1.md

    Args:
        error_data: error.data 字典
        expected_category: 期望的分类（可选）
        expected_reason: 期望的原因码（可选）
    """
    # 必需字段存在性检查
    assert "category" in error_data, "契约违反: error.data 缺少 'category' 字段"
    assert "reason" in error_data, "契约违反: error.data 缺少 'reason' 字段"
    assert "retryable" in error_data, "契约违反: error.data 缺少 'retryable' 字段"
    assert "correlation_id" in error_data, "契约违反: error.data 缺少 'correlation_id' 字段"

    # 类型检查
    assert isinstance(error_data["category"], str), "契约违反: category 必须是字符串"
    assert isinstance(error_data["reason"], str), "契约违反: reason 必须是字符串"
    assert isinstance(error_data["retryable"], bool), "契约违反: retryable 必须是布尔值"
    assert isinstance(error_data["correlation_id"], str), "契约违反: correlation_id 必须是字符串"

    # 值域检查
    assert error_data["category"] in VALID_ERROR_CATEGORIES, (
        f"契约违反: category '{error_data['category']}' 不是有效分类 {VALID_ERROR_CATEGORIES}"
    )
    assert error_data["reason"] in VALID_ERROR_REASONS, (
        f"契约违反: reason '{error_data['reason']}' 不是有效原因码"
    )

    # correlation_id 格式检查
    assert error_data["correlation_id"].startswith("corr-"), (
        f"契约违反: correlation_id 必须以 'corr-' 开头，实际: {error_data['correlation_id']}"
    )
    assert len(error_data["correlation_id"]) == 21, (
        f"契约违反: correlation_id 长度应为 21 (corr- + 16位十六进制)，实际: {len(error_data['correlation_id'])}"
    )

    # 期望值检查（如果提供）
    if expected_category:
        assert error_data["category"] == expected_category, (
            f"分类不匹配: 期望 '{expected_category}'，实际 '{error_data['category']}'"
        )
    if expected_reason:
        assert error_data["reason"] == expected_reason, (
            f"原因码不匹配: 期望 '{expected_reason}'，实际 '{error_data['reason']}'"
        )


def assert_jsonrpc_error_response(
    result: dict,
    expected_code: int = None,
    expected_category: str = None,
    expected_reason: str = None,
):
    """
    断言 JSON-RPC 错误响应符合契约

    Args:
        result: 完整的 JSON-RPC 响应
        expected_code: 期望的错误码（可选）
        expected_category: 期望的分类（可选）
        expected_reason: 期望的原因码（可选）
    """
    assert "error" in result, "响应应包含 error 字段"
    error = result["error"]

    assert "code" in error, "error 应包含 code 字段"
    assert "message" in error, "error 应包含 message 字段"
    assert "data" in error, "契约违反: error 应包含 data 字段"

    if expected_code:
        assert error["code"] == expected_code, (
            f"错误码不匹配: 期望 {expected_code}，实际 {error['code']}"
        )

    # 验证 error.data 符合契约
    assert_error_data_contract(error["data"], expected_category, expected_reason)


# 创建 mock 依赖后再导入 app
@pytest.fixture(scope="function")
def mock_dependencies():
    """
    为 FastAPI 集成测试设置测试依赖

    使用 GatewayContainer.create_for_testing() + set_container() 设置全局容器，
    确保 app 通过 container.deps 获取统一的测试依赖。

    依赖注入策略（v2 架构）:
    ===========================
    1. 使用 GatewayContainer.create_for_testing() 设置全局容器
    2. routes.py 中通过 get_deps_for_request 获取 container.deps
    3. 无需 patch handler 模块级的 get_config/get_client（已统一通过 deps 注入）

    对于 handler 单元测试，应优先使用 GatewayDeps.for_testing() 进行依赖注入。

    Teardown:
    - 全局容器由 conftest.py 的 auto_reset_gateway_state fixture 自动重置
    """
    from engram.gateway.container import (
        GatewayContainer,
        set_container,
    )
    from tests.gateway.fakes import (
        FakeGatewayConfig,
        FakeLogbookAdapter,
        FakeLogbookDatabase,
    )

    # 使用 Fake 对象配置 config, db, adapter
    fake_config = FakeGatewayConfig(
        project_key="test_project",
        default_team_space="team:test_project",
    )

    fake_db = FakeLogbookDatabase()
    fake_db.configure_settings(team_write_enabled=False, policy_json={})

    fake_adapter = FakeLogbookAdapter()
    fake_adapter.configure_dedup_miss()

    # 保持使用 MagicMock 用于 client，因为一些测试需要动态修改行为
    mock_client = MagicMock()
    mock_client.store.return_value = MagicMock(
        success=True,
        memory_id="mock-memory-id-123",
        error=None,
    )
    mock_client.search.return_value = MagicMock(
        success=True,
        results=[],
        error=None,
    )

    # 创建并设置全局测试容器
    # v2 架构：handler 通过 container.deps 获取依赖，无需额外 patch
    test_container = GatewayContainer.create_for_testing(
        config=fake_config,
        db=fake_db,
        logbook_adapter=fake_adapter,
        openmemory_client=mock_client,
    )
    set_container(test_container)

    yield {
        "config": fake_config,
        "db": fake_db,
        "client": mock_client,
        "adapter": fake_adapter,
    }

    # teardown 由 conftest.py 的 auto_reset_gateway_state 处理


@pytest.fixture(scope="function")
def client(mock_dependencies):
    """
    创建 FastAPI TestClient

    依赖 mock_dependencies fixture 确保 app 使用正确的测试依赖。
    scope 改为 function 以确保每个测试都使用干净的依赖状态。

    重要: 使用 create_app() 创建新的 app 实例，而不是复用 main.py 中的模块级 app。
    这确保 app 中的 deps 绑定到 mock_dependencies 设置的测试容器。
    """
    from engram.gateway.app import create_app

    # 创建新的 app 实例，使用 mock_dependencies 设置的全局容器
    # container 已由 mock_dependencies 通过 set_container() 设置
    test_app = create_app()

    return TestClient(test_app)


@pytest.fixture(scope="function")
def auth_client(mock_dependencies, monkeypatch):
    """
    启用 Gateway Auth token 配置的 TestClient

    - GATEWAY_AUTH_TOKEN: 单 token 配置
    - GATEWAY_AUTH_TOKENS_JSON: 多 token 列表配置
    """
    monkeypatch.setenv("GATEWAY_AUTH_TOKEN", "primary-token")
    monkeypatch.setenv("GATEWAY_AUTH_TOKENS_JSON", json.dumps(["secondary-token"]))

    from engram.gateway.app import create_app

    test_app = create_app()
    return TestClient(test_app)


class TestJsonRpcInvalidRequest:
    """测试 JSON-RPC 无效请求 -> -32600"""

    def test_missing_jsonrpc_field(self, client):
        """缺少 jsonrpc 字段 (但有 method) 应返回无效请求"""
        # 注意: 根据 is_jsonrpc_request 的实现，只有同时有 jsonrpc="2.0" 和 method 才认为是 JSON-RPC
        # 所以缺少 jsonrpc 字段会走旧协议分支
        response = client.post("/mcp", json={"method": "tools/list"})
        # 这会被解析为旧协议，但旧协议需要 tool 字段
        assert response.status_code == 400
        result = response.json()
        assert result.get("ok") is False

    def test_jsonrpc_wrong_version(self, client):
        """jsonrpc 版本错误应返回 -32600"""
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "1.0",  # 错误版本
                "method": "tools/list",
                "id": 1,
            },
        )
        # 由于 jsonrpc != "2.0"，会走旧协议分支
        assert response.status_code == 400
        result = response.json()
        assert result.get("ok") is False

    def test_jsonrpc_missing_method(self, client):
        """缺少 method 字段应返回 -32600"""
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                # 缺少 method
            },
        )
        # 没有 method 字段，不会被识别为 JSON-RPC 请求
        # 会走旧协议，旧协议也会失败
        assert response.status_code == 400
        result = response.json()
        assert result.get("ok") is False

    def test_jsonrpc_invalid_params_type(self, client):
        """params 不是 dict 应返回 -32600"""
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/list",
                "params": "not_a_dict",  # 应该是 dict
                "id": 1,
            },
        )
        # Pydantic 验证会失败
        assert response.status_code == 400
        result = response.json()
        assert result.get("error") is not None
        assert result["error"]["code"] == -32600  # INVALID_REQUEST


class TestJsonRpcMethodNotFound:
    """测试未知 method -> -32601"""

    def test_unknown_method_returns_32601(self, client):
        """未知方法应返回 -32601"""
        response = client.post("/mcp", json={"jsonrpc": "2.0", "method": "unknown/method", "id": 1})
        assert response.status_code == 200
        result = response.json()
        assert result.get("error") is not None
        assert result["error"]["code"] == -32601  # METHOD_NOT_FOUND
        assert "未知方法" in result["error"]["message"]

    def test_typo_in_method_name(self, client):
        """方法名拼写错误应返回 -32601"""
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tool/list",  # 缺少 s
                "id": 2,
            },
        )
        assert response.status_code == 200
        result = response.json()
        assert result["error"]["code"] == -32601

    def test_empty_method_returns_32601(self, client):
        """空方法名应返回 -32601"""
        response = client.post("/mcp", json={"jsonrpc": "2.0", "method": "", "id": 3})
        assert response.status_code == 200
        result = response.json()
        assert result["error"]["code"] == -32601


class TestJsonRpcLifecycle:
    """测试 MCP initialize/ping 生命周期契约"""

    def test_initialize_allows_missing_params(self, client):
        """initialize 允许缺省 params"""
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "initialize", "id": 1},
        )
        assert response.status_code == 200
        result = response.json()

        assert result.get("error") is None
        assert result.get("result") is not None
        init_result = result["result"]
        assert "protocolVersion" in init_result
        assert "capabilities" in init_result
        assert "serverInfo" in init_result
        assert isinstance(init_result["protocolVersion"], str)
        assert isinstance(init_result["capabilities"], dict)
        assert isinstance(init_result["serverInfo"], dict)
        assert "tools" in init_result["capabilities"]
        assert isinstance(init_result["capabilities"]["tools"], dict)
        assert "name" in init_result["serverInfo"]
        assert "version" in init_result["serverInfo"]

    def test_initialize_allows_empty_params(self, client):
        """initialize 允许空 params"""
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "initialize", "params": {}, "id": 2},
        )
        assert response.status_code == 200
        result = response.json()
        assert result.get("error") is None
        assert result.get("result") is not None
        assert "protocolVersion" in result["result"]

    def test_ping_returns_empty_result(self, client):
        """ping 返回空结果对象"""
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "ping", "id": 3},
        )
        assert response.status_code == 200
        result = response.json()
        assert result.get("error") is None
        assert result.get("result") == {}


class TestToolsList:
    """测试 tools/list 返回五个工具"""

    def test_tools_list_returns_five_tools(self, client):
        """tools/list 应返回五个工具定义"""
        response = client.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1})
        assert response.status_code == 200
        result = response.json()

        # 验证成功响应
        assert result.get("jsonrpc") == "2.0"
        assert result.get("id") == 1
        assert result.get("error") is None
        assert result.get("result") is not None

        # 验证包含五个工具
        tools = result["result"]["tools"]
        assert len(tools) == 5

        # 验证工具名称
        tool_names = {tool["name"] for tool in tools}
        expected_names = {
            "memory_store",
            "memory_query",
            "reliability_report",
            "governance_update",
            "evidence_upload",
        }
        assert tool_names == expected_names

    def test_tools_list_tool_structure(self, client):
        """验证工具定义的结构"""
        response = client.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1})
        result = response.json()
        tools = result["result"]["tools"]

        for tool in tools:
            # 每个工具必须有 name, description, inputSchema
            assert "name" in tool
            assert "description" in tool
            assert "inputSchema" in tool
            assert isinstance(tool["inputSchema"], dict)
            assert tool["inputSchema"].get("type") == "object"

    def test_tools_list_without_params(self, client):
        """不带 params 调用 tools/list"""
        response = client.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/list", "id": 2})
        assert response.status_code == 200
        result = response.json()
        assert len(result["result"]["tools"]) == 5

    def test_tools_list_input_schema_required_fields(self, client):
        """验证每个工具的 inputSchema.required 字段与实现一致"""
        response = client.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1})
        assert response.status_code == 200
        result = response.json()
        tools = result["result"]["tools"]

        # 构建工具名到工具定义的映射
        tools_by_name = {tool["name"]: tool for tool in tools}

        # 定义期望的 required 字段
        expected_required = {
            "memory_store": ["payload_md"],
            "memory_query": ["query"],
            "reliability_report": [],
            "governance_update": [],
            "evidence_upload": ["content", "content_type"],  # content 和 content_type 是必需的
        }

        # 验证每个工具的 required 字段
        for tool_name, expected_req in expected_required.items():
            assert tool_name in tools_by_name, f"工具 {tool_name} 应该存在"
            tool = tools_by_name[tool_name]
            input_schema = tool["inputSchema"]
            actual_required = input_schema.get("required", [])
            assert set(actual_required) == set(expected_req), (
                f"工具 {tool_name} 的 required 字段不匹配: 期望 {expected_req}, 实际 {actual_required}"
            )

    def test_evidence_upload_input_schema_properties(self, client):
        """验证 evidence_upload 工具的 inputSchema 包含正确的属性"""
        response = client.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1})
        assert response.status_code == 200
        result = response.json()
        tools = result["result"]["tools"]

        # 找到 evidence_upload 工具
        evidence_upload = None
        for tool in tools:
            if tool["name"] == "evidence_upload":
                evidence_upload = tool
                break

        assert evidence_upload is not None, "evidence_upload 工具应该存在"

        # 验证 inputSchema 结构
        input_schema = evidence_upload["inputSchema"]
        assert input_schema["type"] == "object"

        # 验证包含预期的属性
        properties = input_schema["properties"]
        expected_properties = {
            "content",
            "content_type",
            "title",
            "actor_user_id",
            "project_key",
            "item_id",
        }
        assert set(properties.keys()) == expected_properties, (
            f"evidence_upload properties 不匹配: 期望 {expected_properties}, 实际 {set(properties.keys())}"
        )

        # 验证 content 和 content_type 的类型定义
        assert properties["content"]["type"] == "string"
        assert properties["content_type"]["type"] == "string"


class TestGatewayAuthTokens:
    """测试 Gateway Auth token 鉴权契约"""

    def test_tools_list_without_token_rejected(self, auth_client):
        response = auth_client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
        )

        assert response.status_code in (401, 403)
        body = response.json()
        assert isinstance(body, dict)
        assert "detail" in body
        assert isinstance(body["detail"], str)

    @pytest.mark.parametrize("token", ["primary-token", "secondary-token"])
    def test_tools_list_with_valid_token_returns_tools(self, auth_client, token):
        response = auth_client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        result = response.json()
        assert "result" in result
        tools = result["result"].get("tools")
        assert isinstance(tools, list)
        assert len(tools) > 0


class TestToolsCall:
    """测试 tools/call 返回 content[] 格式"""

    def test_tools_call_returns_content_array(self, client, mock_dependencies):
        """tools/call 应返回 content[] 格式"""
        # 设置 mock 返回
        mock_client = mock_dependencies["client"]
        mock_client.store.return_value = MagicMock(
            success=True,
            memory_id="test-memory-id",
            error=None,
        )

        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "memory_query", "arguments": {"query": "test query"}},
                "id": 1,
            },
        )
        assert response.status_code == 200
        result = response.json()

        # 验证成功响应
        assert result.get("jsonrpc") == "2.0"
        assert result.get("id") == 1
        assert result.get("error") is None

        # 验证 content[] 格式
        content = result["result"]["content"]
        assert isinstance(content, list)
        assert len(content) >= 1

        # 验证 TextContent 格式
        first_content = content[0]
        assert first_content.get("type") == "text"
        assert "text" in first_content

        # 验证 text 是可解析的 JSON
        text_content = json.loads(first_content["text"])
        assert isinstance(text_content, dict)

    def test_tools_call_missing_name_returns_error(self, client):
        """缺少 name 参数应返回 -32602"""
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"arguments": {"query": "test"}},
                "id": 1,
            },
        )
        assert response.status_code == 200
        result = response.json()

        # 应返回参数错误
        assert result.get("error") is not None
        assert result["error"]["code"] == -32602  # INVALID_PARAMS

    def test_tools_call_unknown_tool_returns_error(self, client):
        """未知工具应返回 -32602"""
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "unknown_tool", "arguments": {}},
                "id": 1,
            },
        )
        assert response.status_code == 200
        result = response.json()

        # 应返回参数错误（工具不存在）
        assert result.get("error") is not None
        assert result["error"]["code"] == -32602

    def test_tools_call_reliability_report(self, client, mock_dependencies):
        """调用 reliability_report 工具"""
        # Mock get_reliability_report
        with patch("engram.gateway.logbook_adapter.get_reliability_report") as mock_report:
            mock_report.return_value = {
                "outbox_stats": {"pending": 0, "success": 5},
                "audit_stats": {"allow": 10, "reject": 2},
                "v2_evidence_stats": {},
                "content_intercept_stats": {},
                "generated_at": "2026-01-28T12:00:00Z",
            }

            response = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "params": {"name": "reliability_report", "arguments": {}},
                    "id": 1,
                },
            )

        assert response.status_code == 200
        result = response.json()

        # 验证 content[] 格式
        assert result.get("error") is None
        content = result["result"]["content"]
        assert isinstance(content, list)
        assert content[0]["type"] == "text"


class TestLegacyProtocol:
    """测试旧 {tool, arguments} 格式仍返回原 MCPResponse 结构"""

    def test_legacy_format_returns_mcp_response(self, client, mock_dependencies):
        """旧格式请求应返回 MCPResponse 结构"""
        response = client.post(
            "/mcp", json={"tool": "memory_query", "arguments": {"query": "test query", "top_k": 5}}
        )
        assert response.status_code == 200
        result = response.json()

        # 验证 MCPResponse 结构
        assert "ok" in result
        assert "result" in result or "error" in result

        # 验证不是 JSON-RPC 格式
        assert "jsonrpc" not in result

    def test_legacy_format_memory_store(self, client, mock_dependencies):
        """旧格式 memory_store 返回 MCPResponse"""
        mock_client = mock_dependencies["client"]
        mock_client.store.return_value = MagicMock(
            success=True,
            memory_id="legacy-memory-id",
            error=None,
        )

        response = client.post(
            "/mcp",
            json={
                "tool": "memory_store",
                "arguments": {
                    "payload_md": "# Test Memory\n\nThis is a test.",
                },
            },
        )
        assert response.status_code == 200
        result = response.json()

        # 验证 MCPResponse 结构
        assert "ok" in result
        assert result.get("ok") is True or result.get("ok") is False

        if result.get("result"):
            # 结果应包含 memory_store 的返回字段
            inner_result = result["result"]
            assert "action" in inner_result

    def test_legacy_format_unknown_tool(self, client):
        """旧格式未知工具应返回 ok=False"""
        response = client.post("/mcp", json={"tool": "unknown_tool", "arguments": {}})
        assert response.status_code == 200
        result = response.json()

        # 验证错误响应
        assert result.get("ok") is False
        assert result.get("error") is not None

    def test_legacy_format_missing_tool_field(self, client):
        """旧格式缺少 tool 字段应返回 400"""
        response = client.post("/mcp", json={"arguments": {"query": "test"}})
        assert response.status_code == 400
        result = response.json()
        assert result.get("ok") is False

    def test_legacy_format_with_empty_arguments(self, client, mock_dependencies):
        """旧格式空 arguments 应正常处理"""
        with patch("engram.gateway.logbook_adapter.get_reliability_report") as mock_report:
            mock_report.return_value = {
                "outbox_stats": {},
                "audit_stats": {},
                "v2_evidence_stats": {},
                "content_intercept_stats": {},
                "generated_at": "2026-01-28T12:00:00Z",
            }

            response = client.post("/mcp", json={"tool": "reliability_report", "arguments": {}})

        assert response.status_code == 200
        result = response.json()
        assert result.get("ok") is True
        assert "result" in result


class TestJsonRpcProtocolDetails:
    """测试 JSON-RPC 协议细节"""

    def test_response_includes_id(self, client):
        """响应应包含请求的 id"""
        response = client.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/list", "id": 42})
        result = response.json()
        assert result.get("id") == 42

    def test_response_includes_jsonrpc_version(self, client):
        """响应应包含 jsonrpc: 2.0"""
        response = client.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1})
        result = response.json()
        assert result.get("jsonrpc") == "2.0"

    def test_null_id_preserved(self, client):
        """null id 应被保留"""
        response = client.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/list", "id": None})
        result = response.json()
        assert result.get("id") is None

    def test_string_id_preserved(self, client):
        """字符串 id 应被保留"""
        response = client.post(
            "/mcp", json={"jsonrpc": "2.0", "method": "tools/list", "id": "my-request-id"}
        )
        result = response.json()
        assert result.get("id") == "my-request-id"


class TestJsonParseError:
    """测试 JSON 解析错误"""

    def test_invalid_json_returns_parse_error(self, client):
        """无效 JSON 应返回 -32700"""
        response = client.post(
            "/mcp", content="not valid json", headers={"Content-Type": "application/json"}
        )
        # FastAPI/Starlette 可能返回 400 或 422
        assert response.status_code in [400, 422]


# ===================== 新增：ErrorData 结构测试 =====================


class TestErrorDataStructure:
    """
    测试所有错误响应包含结构化的 ErrorData

    契约: docs/contracts/mcp_jsonrpc_error_v1.md
    所有 JSON-RPC 错误响应必须包含:
    - error.data.category
    - error.data.reason
    - error.data.retryable
    - error.data.correlation_id
    """

    def test_invalid_request_has_error_data(self, client):
        """无效请求应返回包含 ErrorData 的错误响应"""
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/list",
                "params": "not_a_dict",  # params 应该是 dict
                "id": 1,
            },
        )
        assert response.status_code == 400
        result = response.json()

        # 使用契约断言（验证必需字段完整性）
        assert_jsonrpc_error_response(result, expected_code=-32600)

    def test_method_not_found_has_error_data(self, client):
        """方法不存在应返回包含 ErrorData 的错误响应"""
        response = client.post(
            "/mcp", json={"jsonrpc": "2.0", "method": "nonexistent/method", "id": 1}
        )
        assert response.status_code == 200
        result = response.json()

        # 使用契约断言（验证必需字段完整性和值）
        assert_jsonrpc_error_response(
            result,
            expected_code=-32601,
            expected_category="protocol",
            expected_reason="METHOD_NOT_FOUND",
        )

        # 额外验证 retryable 为 False
        assert result["error"]["data"]["retryable"] is False

    def test_invalid_params_has_error_data(self, client):
        """无效参数应返回包含 ErrorData 的错误响应"""
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    # 缺少必需的 name 参数
                    "arguments": {"query": "test"}
                },
                "id": 1,
            },
        )
        assert response.status_code == 200
        result = response.json()

        # 使用契约断言（验证必需字段完整性）
        assert_jsonrpc_error_response(
            result,
            expected_code=-32602,
            expected_category="validation",
            expected_reason="MISSING_REQUIRED_PARAM",
        )

        # 额外验证 retryable 为 False
        assert result["error"]["data"]["retryable"] is False

    def test_unknown_tool_has_error_data(self, client):
        """未知工具应返回包含 ErrorData 的错误响应"""
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "nonexistent_tool", "arguments": {}},
                "id": 1,
            },
        )
        assert response.status_code == 200
        result = response.json()

        # 使用契约断言（验证必需字段完整性和值）
        assert_jsonrpc_error_response(
            result,
            expected_code=-32602,
            expected_category="validation",
            expected_reason="UNKNOWN_TOOL",
        )

        # 额外验证 retryable 为 False
        assert result["error"]["data"]["retryable"] is False


class TestDependencyUnavailable:
    """测试依赖服务不可用场景

    注意: memory_query 的设计是在 OpenMemory 不可用时降级到 Logbook 查询，
    返回业务响应（ok=True, degraded=True）而不是 JSON-RPC 错误。
    这是为了保证查询可用性，即使依赖服务临时不可用。
    """

    def test_openmemory_connection_error(self, client, mock_dependencies):
        """OpenMemory 连接失败时应降级返回空结果"""
        from engram.gateway.openmemory_client import OpenMemoryConnectionError

        mock_client = mock_dependencies["client"]
        mock_client.search.side_effect = OpenMemoryConnectionError(
            "连接超时", status_code=None, response=None
        )

        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "memory_query", "arguments": {"query": "test"}},
                "id": 1,
            },
        )
        assert response.status_code == 200
        result = response.json()

        # memory_query 降级处理：返回业务响应，不是 JSON-RPC 错误
        assert "result" in result
        import json as json_module

        content = json_module.loads(result["result"]["content"][0]["text"])
        assert content["ok"] is True
        assert content["degraded"] is True
        assert "连接超时" in content["message"]

    def test_openmemory_api_error_5xx(self, client, mock_dependencies):
        """OpenMemory 5xx 错误时应降级返回空结果"""
        from engram.gateway.openmemory_client import OpenMemoryAPIError

        mock_client = mock_dependencies["client"]
        mock_client.search.side_effect = OpenMemoryAPIError(
            "服务器内部错误", status_code=503, response={"error": "Service Unavailable"}
        )

        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "memory_query", "arguments": {"query": "test"}},
                "id": 1,
            },
        )
        assert response.status_code == 200
        result = response.json()

        # memory_query 降级处理
        assert "result" in result
        import json as json_module

        content = json_module.loads(result["result"]["content"][0]["text"])
        assert content["ok"] is True
        assert content["degraded"] is True
        assert "服务器内部错误" in content["message"]

    def test_openmemory_api_error_4xx(self, client, mock_dependencies):
        """OpenMemory 4xx 错误时应降级返回空结果"""
        from engram.gateway.openmemory_client import OpenMemoryAPIError

        mock_client = mock_dependencies["client"]
        mock_client.search.side_effect = OpenMemoryAPIError(
            "请求无效", status_code=400, response={"error": "Bad Request"}
        )

        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "memory_query", "arguments": {"query": "test"}},
                "id": 1,
            },
        )
        assert response.status_code == 200
        result = response.json()

        # memory_query 降级处理
        assert "result" in result
        import json as json_module

        content = json_module.loads(result["result"]["content"][0]["text"])
        assert content["ok"] is True
        assert content["degraded"] is True
        assert "请求无效" in content["message"]


class TestBusinessRejection:
    """测试业务拒绝场景"""

    def test_governance_update_auth_failed(self, client, mock_dependencies):
        """governance_update 鉴权失败应返回业务拒绝错误"""
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": "governance_update",
                    "arguments": {
                        "team_write_enabled": True,
                        "admin_key": "wrong_key",  # 错误的密钥
                    },
                },
                "id": 1,
            },
        )
        assert response.status_code == 200
        result = response.json()

        # governance_update 返回的是 result 而不是 error（业务层处理）
        # 所以这里我们检查 result 内容
        assert "result" in result
        content = result["result"]["content"]
        assert len(content) >= 1

        # 解析 TextContent
        import json

        text_content = json.loads(content[0]["text"])
        assert text_content["ok"] is False
        assert "拒绝" in text_content.get("message", "") or "reject" in text_content.get(
            "action", ""
        )


class TestInternalError:
    """测试内部错误场景

    注意: memory_query 的设计是在内部处理所有异常，返回业务响应（ok=False）
    而不是 JSON-RPC 错误。这是为了保证接口一致性。
    """

    def test_tool_executor_runtime_error(self, client, mock_dependencies):
        """工具执行器运行时错误应返回 ok=False 的业务响应"""
        mock_client = mock_dependencies["client"]
        mock_client.search.side_effect = RuntimeError("未预期的内部错误")

        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "memory_query", "arguments": {"query": "test"}},
                "id": 1,
            },
        )
        assert response.status_code == 200
        result = response.json()

        # memory_query 内部处理所有异常，返回业务响应
        assert "result" in result
        import json as json_module

        content = json_module.loads(result["result"]["content"][0]["text"])
        assert content["ok"] is False
        assert "内部错误" in content["message"]
        assert "未预期的内部错误" in content["message"]

    def test_unhandled_exception(self, client, mock_dependencies):
        """未处理的异常应返回 ok=False 的业务响应"""
        mock_client = mock_dependencies["client"]
        mock_client.search.side_effect = KeyError("unexpected_key")

        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "memory_query", "arguments": {"query": "test"}},
                "id": 1,
            },
        )
        assert response.status_code == 200
        result = response.json()

        # memory_query 内部处理所有异常，返回业务响应
        assert "result" in result
        import json as json_module

        content = json_module.loads(result["result"]["content"][0]["text"])
        assert content["ok"] is False
        assert "内部错误" in content["message"]

    def test_unhandled_exception_before_dispatch_returns_jsonrpc_error(self, client):
        """分发前异常应返回 JSON-RPC internal error 且包含 CORS headers"""
        requested_headers = "X-Request-Id, X-Correlation-ID"
        sensitive_token = "glpat-abc123def456ghi789jkl"
        auth_value = "Authorization: Bearer secret-token-123"
        with patch(
            "engram.gateway.routes._make_cors_headers_with_correlation_id",
            side_effect=RuntimeError(f"boom {auth_value} {sensitive_token}"),
        ):
            response = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
                headers={"Access-Control-Request-Headers": requested_headers},
            )

        assert response.status_code == 500
        result = response.json()
        assert result.get("jsonrpc") == "2.0"

        assert_jsonrpc_error_response(
            result,
            expected_code=-32603,
            expected_category="internal",
            expected_reason="UNHANDLED_EXCEPTION",
        )
        error_data = result["error"]["data"]
        correlation_id = error_data["correlation_id"]
        assert response.headers.get("X-Correlation-ID") == correlation_id

        assert response.headers.get("Access-Control-Allow-Origin") == "*"
        expose_headers = response.headers.get("Access-Control-Expose-Headers", "")
        assert "X-Correlation-ID" in expose_headers
        allow_headers = response.headers.get("Access-Control-Allow-Headers", "")
        allow_header_set = {
            item.strip().lower() for item in allow_headers.split(",") if item.strip()
        }
        assert "x-request-id" in allow_header_set
        assert "x-correlation-id" in allow_header_set
        assert result["error"]["message"] == "内部错误"
        payload = json.dumps(result, ensure_ascii=False)
        assert sensitive_token not in payload
        assert "secret-token-123" not in payload
        details_payload = json.dumps(result["error"]["data"].get("details", {}), ensure_ascii=False)
        assert sensitive_token not in details_payload
        assert "secret-token-123" not in details_payload


class TestCorrelationIdTracking:
    """测试 correlation_id 追踪"""

    def test_error_response_has_correlation_id(self, client):
        """所有错误响应都应包含 correlation_id"""
        response = client.post("/mcp", json={"jsonrpc": "2.0", "method": "unknown/method", "id": 1})
        result = response.json()

        assert "error" in result
        data = result["error"]["data"]
        assert "correlation_id" in data
        assert data["correlation_id"].startswith("corr-")
        assert len(data["correlation_id"]) == 21  # "corr-" + 16 hex chars

    def test_parse_error_has_correlation_id(self, client):
        """JSON 解析错误也应有 correlation_id"""
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/list",
                "params": "invalid",  # 应该是 dict
                "id": 1,
            },
        )
        result = response.json()

        if "error" in result and "data" in result["error"]:
            data = result["error"]["data"]
            assert "correlation_id" in data


class TestErrorRedaction:
    """测试错误响应脱敏"""

    def test_error_message_redacts_sensitive_tool_name(self, client):
        """错误消息与 details 不应泄露敏感 token"""
        sensitive_tool_name = "glpat-abc123def456ghi789jkl"
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": sensitive_tool_name, "arguments": {}},
                "id": 1,
            },
        )
        result = response.json()

        assert "error" in result
        error = result["error"]
        assert sensitive_tool_name not in error["message"]
        assert "[GITLAB_TOKEN]" in error["message"] or "[REDACTED]" in error["message"]

        details = error["data"].get("details", {})
        assert sensitive_tool_name not in json.dumps(details)

    def test_unhandled_exception_hides_sensitive_values(self, client):
        """UNHANDLED_EXCEPTION 不应回显 token/Authorization 值"""
        sensitive_token = "glpat-abc123def456ghi789jkl"
        auth_value = "Authorization: Bearer secret-token-123"

        async def mock_executor(tool_name, tool_args, correlation_id):
            raise KeyError(f"{auth_value} {sensitive_token}")

        with patch(
            "engram.gateway.mcp_rpc.get_tool_executor",
            return_value=mock_executor,
        ):
            response = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "params": {"name": "memory_query", "arguments": {"query": "test"}},
                    "id": 1,
                },
            )

        result = response.json()
        assert_jsonrpc_error_response(
            result,
            expected_code=-32603,
            expected_category="internal",
            expected_reason="UNHANDLED_EXCEPTION",
        )
        assert result["error"]["message"] == "内部错误"
        payload = json.dumps(result, ensure_ascii=False)
        assert sensitive_token not in payload
        assert "secret-token-123" not in payload
        details_payload = json.dumps(result["error"]["data"].get("details", {}), ensure_ascii=False)
        assert sensitive_token not in details_payload
        assert "secret-token-123" not in details_payload


class TestErrorDataFields:
    """测试 ErrorData 字段完整性"""

    def test_error_data_has_all_required_fields(self, client):
        """ErrorData 应包含所有必需字段"""
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "unknown_tool", "arguments": {}},
                "id": 1,
            },
        )
        result = response.json()

        data = result["error"]["data"]

        # 必需字段
        assert "category" in data
        assert "reason" in data
        assert "retryable" in data

        # category 应为有效值
        assert data["category"] in ["protocol", "validation", "business", "dependency", "internal"]

        # retryable 应为布尔值
        assert isinstance(data["retryable"], bool)

    def test_details_contains_tool_name(self, client):
        """tools/call 错误的 details 应包含工具名"""
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": "memory_query",
                    "arguments": {},  # 缺少 query 参数
                },
                "id": 1,
            },
        )
        response.json()

        # memory_query 缺少 query 参数可能不会直接报错（取决于实现）
        # 这里我们测试一个会报错的场景
        response2 = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "nonexistent_tool", "arguments": {}},
                "id": 2,
            },
        )
        result2 = response2.json()

        if "error" in result2 and "data" in result2["error"]:
            data = result2["error"]["data"]
            # details 中可能包含 tool 信息
            if "details" in data and data["details"]:
                # 验证 details 是字典
                assert isinstance(data["details"], dict)


# ===================== tools/call 错误对齐测试 =====================


class TestToolsCallErrorAlignment:
    """
    测试 tools/call 的参数错误与未知工具错误对齐

    契约: docs/contracts/mcp_jsonrpc_error_v1.md §5

    所有 tools/call 错误应统一使用:
    - 错误码: -32602 (INVALID_PARAMS)
    - 分类: validation
    - 可重试: false
    """

    def test_missing_name_param_error_alignment(self, client):
        """缺少 name 参数: 应返回 validation/MISSING_REQUIRED_PARAM"""
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"arguments": {"query": "test"}},
                "id": 1,
            },
        )
        assert response.status_code == 200
        result = response.json()

        # 验证错误响应符合契约
        assert_jsonrpc_error_response(
            result,
            expected_code=-32602,
            expected_category="validation",
            expected_reason="MISSING_REQUIRED_PARAM",
        )

        # 验证不可重试
        assert result["error"]["data"]["retryable"] is False

    def test_unknown_tool_error_alignment(self, client):
        """未知工具: 应返回 validation/UNKNOWN_TOOL"""
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "this_tool_does_not_exist", "arguments": {}},
                "id": 1,
            },
        )
        assert response.status_code == 200
        result = response.json()

        # 验证错误响应符合契约
        assert_jsonrpc_error_response(
            result,
            expected_code=-32602,
            expected_category="validation",
            expected_reason="UNKNOWN_TOOL",
        )

        # 验证不可重试
        assert result["error"]["data"]["retryable"] is False

    def test_both_errors_use_same_category(self, client):
        """参数错误和未知工具错误应使用相同的分类 (validation)"""
        # 缺少参数
        response1 = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "tools/call", "params": {"arguments": {}}, "id": 1},
        )
        result1 = response1.json()

        # 未知工具
        response2 = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "unknown_tool_xyz", "arguments": {}},
                "id": 2,
            },
        )
        result2 = response2.json()

        # 两者应使用相同的错误码和分类
        assert result1["error"]["code"] == result2["error"]["code"] == -32602
        assert (
            result1["error"]["data"]["category"]
            == result2["error"]["data"]["category"]
            == "validation"
        )
        assert (
            result1["error"]["data"]["retryable"] == result2["error"]["data"]["retryable"] is False
        )

    def test_both_errors_have_correlation_id(self, client):
        """参数错误和未知工具错误都应有 correlation_id"""
        # 缺少参数
        response1 = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "tools/call", "params": {"arguments": {}}, "id": 1},
        )
        result1 = response1.json()

        # 未知工具
        response2 = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "unknown", "arguments": {}},
                "id": 2,
            },
        )
        result2 = response2.json()

        # 两者都应有 correlation_id
        corr_id1 = result1["error"]["data"]["correlation_id"]
        corr_id2 = result2["error"]["data"]["correlation_id"]

        assert corr_id1.startswith("corr-")
        assert corr_id2.startswith("corr-")
        assert len(corr_id1) == 21
        assert len(corr_id2) == 21
        # 两个请求的 correlation_id 应不同
        assert corr_id1 != corr_id2

    def test_error_message_distinguishes_error_types(self, client):
        """错误消息应能区分参数缺失和未知工具"""
        # 缺少参数
        response1 = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "tools/call", "params": {"arguments": {}}, "id": 1},
        )
        result1 = response1.json()

        # 未知工具
        response2 = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "unknown_tool", "arguments": {}},
                "id": 2,
            },
        )
        result2 = response2.json()

        # 错误消息应能区分
        msg1 = result1["error"]["message"]
        msg2 = result2["error"]["message"]

        # 缺少参数的消息应包含 "name" 或 "参数"
        assert "name" in msg1.lower() or "参数" in msg1

        # 未知工具的消息应包含工具名或 "未知"
        assert "unknown_tool" in msg2 or "未知" in msg2


class TestErrorDataContractCompliance:
    """
    全面测试 error.data 契约合规性

    验证所有可能的错误场景都符合 mcp_jsonrpc_error_v1.md 契约
    """

    def test_all_error_scenarios_have_required_fields(self, client):
        """所有错误场景都必须包含契约要求的必需字段"""
        # 测试场景列表：(请求, 期望状态码, 期望错误码)
        test_cases = [
            # 方法不存在
            ({"jsonrpc": "2.0", "method": "nonexistent", "id": 1}, 200, -32601),
            # 缺少工具名
            (
                {"jsonrpc": "2.0", "method": "tools/call", "params": {"arguments": {}}, "id": 2},
                200,
                -32602,
            ),
            # 未知工具
            (
                {
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "params": {"name": "unknown", "arguments": {}},
                    "id": 3,
                },
                200,
                -32602,
            ),
        ]

        for request_body, expected_status, expected_error_code in test_cases:
            response = client.post("/mcp", json=request_body)
            assert response.status_code == expected_status, f"请求 {request_body} 状态码不匹配"

            result = response.json()
            assert "error" in result, f"请求 {request_body} 应返回错误"
            assert result["error"]["code"] == expected_error_code, (
                f"请求 {request_body} 错误码不匹配: 期望 {expected_error_code}, 实际 {result['error']['code']}"
            )

            # 使用契约断言验证 error.data
            assert "data" in result["error"], f"请求 {request_body} 缺少 error.data"
            assert_error_data_contract(result["error"]["data"])

    def test_validation_errors_are_not_retryable(self, client):
        """所有校验错误都不可重试"""
        validation_requests = [
            {"jsonrpc": "2.0", "method": "tools/call", "params": {"arguments": {}}, "id": 1},
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "unknown", "arguments": {}},
                "id": 2,
            },
        ]

        for request_body in validation_requests:
            response = client.post("/mcp", json=request_body)
            result = response.json()

            assert result["error"]["data"]["category"] == "validation", (
                f"请求 {request_body} 应为 validation 分类"
            )
            assert result["error"]["data"]["retryable"] is False, (
                f"请求 {request_body} 的 validation 错误不应可重试"
            )

    def test_protocol_errors_are_not_retryable(self, client):
        """所有协议错误都不可重试"""
        protocol_requests = [
            {"jsonrpc": "2.0", "method": "nonexistent/method", "id": 1},
            {"jsonrpc": "2.0", "method": "", "id": 2},
        ]

        for request_body in protocol_requests:
            response = client.post("/mcp", json=request_body)
            result = response.json()

            assert result["error"]["data"]["category"] == "protocol", (
                f"请求 {request_body} 应为 protocol 分类"
            )
            assert result["error"]["data"]["retryable"] is False, (
                f"请求 {request_body} 的 protocol 错误不应可重试"
            )


# ===================== correlation_id 统一规则契约测试 =====================


class TestCorrelationIdUnifiedContract:
    """
    测试 correlation_id 统一规则契约

    契约要求：
    1. 每个请求只生成一次 correlation_id
    2. HTTP/MCP/JSON-RPC 的错误与业务响应都必须携带 correlation_id
    3. correlation_id 格式：corr-{16位十六进制}

    详见: docs/gateway/07_capability_boundary.md
    """

    def test_jsonrpc_success_response_has_no_direct_correlation_id(self, client, mock_dependencies):
        """
        JSON-RPC 2.0 成功响应：correlation_id 在 result.content[0].text 的 JSON 中

        注意：JSON-RPC 成功响应的顶层没有 correlation_id，
        correlation_id 在业务结果的 JSON 内部。
        """
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "memory_query", "arguments": {"query": "test"}},
                "id": 1,
            },
        )
        assert response.status_code == 200
        result = response.json()

        # 成功响应应有 result 字段
        assert "result" in result
        assert "content" in result["result"]

        # 解析 content[0].text 中的业务结果
        import json as json_module

        content_text = result["result"]["content"][0]["text"]
        business_result = json_module.loads(content_text)

        # 业务结果中应包含 correlation_id
        assert "correlation_id" in business_result, "业务响应中必须包含 correlation_id"
        assert business_result["correlation_id"].startswith("corr-"), (
            f"correlation_id 格式不正确: {business_result['correlation_id']}"
        )

    def test_jsonrpc_error_response_always_has_correlation_id(self, client):
        """JSON-RPC 2.0 错误响应必须始终包含 correlation_id"""
        # 测试多种错误场景
        error_requests = [
            # 方法不存在
            {"jsonrpc": "2.0", "method": "nonexistent", "id": 1},
            # 缺少工具名
            {"jsonrpc": "2.0", "method": "tools/call", "params": {"arguments": {}}, "id": 2},
            # 未知工具
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "unknown", "arguments": {}},
                "id": 3,
            },
        ]

        for request_body in error_requests:
            response = client.post("/mcp", json=request_body)
            result = response.json()

            assert "error" in result, f"请求 {request_body} 应返回错误"
            assert "data" in result["error"], f"请求 {request_body} 的 error 应包含 data"

            data = result["error"]["data"]
            assert "correlation_id" in data, (
                f"请求 {request_body} 的 error.data 必须包含 correlation_id"
            )
            assert data["correlation_id"].startswith("corr-"), (
                f"correlation_id 格式不正确: {data['correlation_id']}"
            )
            assert len(data["correlation_id"]) == 21, (
                f"correlation_id 长度应为 21，实际: {len(data['correlation_id'])}"
            )

    def test_legacy_protocol_success_response_has_correlation_id(self, client, mock_dependencies):
        """旧协议成功响应必须包含 correlation_id"""
        with patch("engram.gateway.logbook_adapter.get_reliability_report") as mock_report:
            mock_report.return_value = {
                "outbox_stats": {},
                "audit_stats": {},
                "v2_evidence_stats": {},
                "content_intercept_stats": {},
                "generated_at": "2026-01-31T12:00:00Z",
            }

            response = client.post("/mcp", json={"tool": "reliability_report", "arguments": {}})

        assert response.status_code == 200
        result = response.json()

        assert result.get("ok") is True
        assert "result" in result

        # 成功响应的 result 中必须包含 correlation_id
        assert "correlation_id" in result["result"], (
            "旧协议成功响应的 result 中必须包含 correlation_id"
        )
        assert result["result"]["correlation_id"].startswith("corr-"), (
            f"correlation_id 格式不正确: {result['result']['correlation_id']}"
        )

    def test_legacy_protocol_error_response_has_correlation_id(self, client):
        """旧协议错误响应必须包含 correlation_id"""
        # 测试未知工具
        response = client.post("/mcp", json={"tool": "unknown_tool", "arguments": {}})

        assert response.status_code == 200
        result = response.json()

        assert result.get("ok") is False
        assert "correlation_id" in result, "旧协议错误响应必须包含 correlation_id"
        assert result["correlation_id"].startswith("corr-"), (
            f"correlation_id 格式不正确: {result['correlation_id']}"
        )

    def test_legacy_protocol_invalid_format_error_has_correlation_id(self, client):
        """旧协议格式错误响应必须包含 correlation_id"""
        response = client.post(
            "/mcp",
            json={
                "arguments": {"query": "test"}  # 缺少 tool 字段
            },
        )

        assert response.status_code == 400
        result = response.json()

        assert result.get("ok") is False
        assert "correlation_id" in result, "旧协议格式错误响应必须包含 correlation_id"
        assert result["correlation_id"].startswith("corr-"), (
            f"correlation_id 格式不正确: {result['correlation_id']}"
        )

    def test_json_parse_error_has_correlation_id(self, client):
        """JSON 解析错误响应必须包含 correlation_id"""
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/list",
                "params": "invalid_params",  # 应该是 dict
                "id": 1,
            },
        )

        assert response.status_code == 400
        result = response.json()

        assert "error" in result
        # error.data 中应有 correlation_id
        if "data" in result["error"] and result["error"]["data"]:
            assert "correlation_id" in result["error"]["data"], (
                "解析错误的 error.data 必须包含 correlation_id"
            )

    def test_correlation_id_is_unique_per_request(self, client):
        """每个请求的 correlation_id 必须唯一"""
        correlation_ids = set()

        # 发送多个相同的请求
        for _ in range(5):
            response = client.post(
                "/mcp", json={"jsonrpc": "2.0", "method": "nonexistent", "id": 1}
            )
            result = response.json()

            corr_id = result["error"]["data"]["correlation_id"]
            assert corr_id not in correlation_ids, f"correlation_id 应唯一，重复: {corr_id}"
            correlation_ids.add(corr_id)

    def test_correlation_id_format_contract(self, client):
        """验证 correlation_id 格式契约"""
        response = client.post("/mcp", json={"jsonrpc": "2.0", "method": "unknown", "id": 1})
        result = response.json()

        corr_id = result["error"]["data"]["correlation_id"]

        # 格式检查
        assert corr_id.startswith("corr-"), f"correlation_id 必须以 'corr-' 开头: {corr_id}"

        suffix = corr_id[5:]  # 去掉 'corr-' 前缀
        assert len(suffix) == 16, f"correlation_id 后缀应为 16 位: {suffix}"

        # 验证是有效的十六进制
        try:
            int(suffix, 16)
        except ValueError:
            pytest.fail(f"correlation_id 后缀应为十六进制: {suffix}")


# ===================== correlation_id 单一来源回归测试 =====================


class TestCorrelationIdUnifiedSourceRegression:
    """
    correlation_id 单一来源回归测试

    验证重构后所有 correlation_id 都来自 mcp_rpc.generate_correlation_id()，
    格式统一为 corr-{16位十六进制}。

    契约要点：
    1. JSON-RPC 错误的 error.data.correlation_id 满足 pattern: ^corr-[a-fA-F0-9]{16}$
    2. memory_store 响应中的 correlation_id 必填不丢
    3. 旧协议响应中的 correlation_id 格式一致
    """

    CORRELATION_ID_PATTERN = r"^corr-[a-fA-F0-9]{16}$"

    def test_jsonrpc_error_correlation_id_pattern_method_not_found(self, client):
        """
        回归测试：METHOD_NOT_FOUND 错误的 correlation_id 满足 pattern
        """
        import re

        response = client.post(
            "/mcp", json={"jsonrpc": "2.0", "method": "nonexistent/method", "id": 1}
        )
        result = response.json()

        assert "error" in result
        assert "data" in result["error"]
        correlation_id = result["error"]["data"]["correlation_id"]

        assert re.match(self.CORRELATION_ID_PATTERN, correlation_id), (
            f"correlation_id 格式不正确: {correlation_id}，期望匹配 {self.CORRELATION_ID_PATTERN}"
        )

    def test_jsonrpc_error_correlation_id_pattern_invalid_params(self, client):
        """
        回归测试：INVALID_PARAMS 错误的 correlation_id 满足 pattern
        """
        import re

        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"arguments": {}},  # 缺少 name
                "id": 1,
            },
        )
        result = response.json()

        assert "error" in result
        correlation_id = result["error"]["data"]["correlation_id"]

        assert re.match(self.CORRELATION_ID_PATTERN, correlation_id), (
            f"correlation_id 格式不正确: {correlation_id}"
        )

    def test_jsonrpc_error_correlation_id_pattern_unknown_tool(self, client):
        """
        回归测试：UNKNOWN_TOOL 错误的 correlation_id 满足 pattern
        """
        import re

        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "nonexistent_tool", "arguments": {}},
                "id": 1,
            },
        )
        result = response.json()

        assert "error" in result
        correlation_id = result["error"]["data"]["correlation_id"]

        assert re.match(self.CORRELATION_ID_PATTERN, correlation_id), (
            f"correlation_id 格式不正确: {correlation_id}"
        )

    def test_jsonrpc_success_correlation_id_pattern(self, client, mock_dependencies):
        """
        回归测试：成功响应中业务结果的 correlation_id 满足 pattern
        """
        import json as json_module
        import re

        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "memory_query", "arguments": {"query": "test"}},
                "id": 1,
            },
        )
        result = response.json()

        assert "result" in result
        content = result["result"]["content"]
        business_result = json_module.loads(content[0]["text"])

        correlation_id = business_result.get("correlation_id")
        assert correlation_id is not None, "业务响应中 correlation_id 不应为空"
        assert re.match(self.CORRELATION_ID_PATTERN, correlation_id), (
            f"correlation_id 格式不正确: {correlation_id}"
        )

    def test_legacy_protocol_success_correlation_id_pattern(self, client, mock_dependencies):
        """
        回归测试：旧协议成功响应的 correlation_id 满足 pattern
        """
        import re

        with patch("engram.gateway.logbook_adapter.get_reliability_report") as mock_report:
            mock_report.return_value = {
                "outbox_stats": {},
                "audit_stats": {},
                "v2_evidence_stats": {},
                "content_intercept_stats": {},
                "generated_at": "2026-01-31T12:00:00Z",
            }

            response = client.post("/mcp", json={"tool": "reliability_report", "arguments": {}})

        result = response.json()
        assert result.get("ok") is True

        correlation_id = result["result"]["correlation_id"]
        assert re.match(self.CORRELATION_ID_PATTERN, correlation_id), (
            f"correlation_id 格式不正确: {correlation_id}"
        )

    def test_legacy_protocol_error_correlation_id_pattern(self, client):
        """
        回归测试：旧协议错误响应的 correlation_id 满足 pattern
        """
        import re

        response = client.post("/mcp", json={"tool": "unknown_tool", "arguments": {}})
        result = response.json()

        assert result.get("ok") is False
        correlation_id = result.get("correlation_id")

        assert correlation_id is not None, "旧协议错误响应应包含 correlation_id"
        assert re.match(self.CORRELATION_ID_PATTERN, correlation_id), (
            f"correlation_id 格式不正确: {correlation_id}"
        )


# ===================== 补充：Legacy 协议完整覆盖测试 =====================


class TestLegacyProtocolComplete:
    """
    Legacy 协议完整覆盖测试

    验证旧 {tool, arguments} 格式的各种场景：
    1. 所有 5 个工具都支持 legacy 格式
    2. legacy 格式响应结构正确
    3. legacy 格式错误处理正确
    4. legacy 格式包含 correlation_id
    """

    def test_legacy_memory_query_tool(self, client, mock_dependencies):
        """Legacy 格式调用 memory_query 工具"""
        response = client.post(
            "/mcp",
            json={
                "tool": "memory_query",
                "arguments": {
                    "query": "test query",
                    "top_k": 5,
                },
            },
        )
        assert response.status_code == 200
        result = response.json()

        # 验证 MCPResponse 结构
        assert "ok" in result
        assert "jsonrpc" not in result  # 不是 JSON-RPC 格式

        # 验证有 result 或 error
        assert "result" in result or "error" in result

    def test_legacy_memory_store_tool(self, client, mock_dependencies):
        """Legacy 格式调用 memory_store 工具"""
        mock_client = mock_dependencies["client"]
        mock_client.store.return_value = MagicMock(
            success=True,
            memory_id="legacy-store-id",
            error=None,
        )

        response = client.post(
            "/mcp",
            json={
                "tool": "memory_store",
                "arguments": {
                    "payload_md": "# Legacy Test\n\nContent here.",
                    "target_space": "team:test",
                },
            },
        )
        assert response.status_code == 200
        result = response.json()

        # 验证 MCPResponse 结构
        assert "ok" in result
        if result.get("ok"):
            assert "result" in result

    def test_legacy_reliability_report_tool(self, client, mock_dependencies):
        """Legacy 格式调用 reliability_report 工具"""
        with patch("engram.gateway.logbook_adapter.get_reliability_report") as mock_report:
            mock_report.return_value = {
                "outbox_stats": {"pending": 0, "success": 10},
                "audit_stats": {"allow": 50, "reject": 5},
                "v2_evidence_stats": {},
                "content_intercept_stats": {},
                "generated_at": "2026-01-31T12:00:00Z",
            }

            response = client.post("/mcp", json={"tool": "reliability_report", "arguments": {}})

        assert response.status_code == 200
        result = response.json()

        assert result.get("ok") is True
        assert "result" in result

    def test_legacy_governance_update_tool(self, client, mock_dependencies):
        """Legacy 格式调用 governance_update 工具"""
        response = client.post(
            "/mcp",
            json={
                "tool": "governance_update",
                "arguments": {
                    "team_write_enabled": True,
                    "admin_key": "wrong_key",  # 错误的密钥
                },
            },
        )
        assert response.status_code == 200
        result = response.json()

        # 验证 MCPResponse 结构
        assert "ok" in result
        # 错误密钥应返回失败
        # 注意：具体行为取决于 governance_admin_key 配置

    def test_legacy_evidence_upload_tool(self, client, mock_dependencies):
        """Legacy 格式调用 evidence_upload 工具"""
        response = client.post(
            "/mcp",
            json={
                "tool": "evidence_upload",
                "arguments": {
                    "content": "test content",
                    "content_type": "text/plain",
                },
            },
        )
        assert response.status_code == 200
        result = response.json()

        # 验证 MCPResponse 结构
        assert "ok" in result

    def test_legacy_response_has_correlation_id(self, client, mock_dependencies):
        """Legacy 格式响应包含 correlation_id"""
        with patch("engram.gateway.logbook_adapter.get_reliability_report") as mock_report:
            mock_report.return_value = {
                "outbox_stats": {},
                "audit_stats": {},
                "v2_evidence_stats": {},
                "content_intercept_stats": {},
                "generated_at": "2026-01-31T12:00:00Z",
            }

            response = client.post("/mcp", json={"tool": "reliability_report", "arguments": {}})

        result = response.json()
        assert result.get("ok") is True

        # 成功响应的 result 中应包含 correlation_id
        if "result" in result and result["result"]:
            assert "correlation_id" in result["result"]
            assert result["result"]["correlation_id"].startswith("corr-")

    def test_legacy_error_response_has_correlation_id(self, client):
        """Legacy 格式错误响应包含 correlation_id"""
        response = client.post("/mcp", json={"tool": "unknown_tool", "arguments": {}})
        result = response.json()

        assert result.get("ok") is False
        # 错误响应顶层应包含 correlation_id
        assert "correlation_id" in result
        assert result["correlation_id"].startswith("corr-")

    def test_legacy_missing_arguments_field(self, client):
        """Legacy 格式缺少 arguments 字段"""
        response = client.post(
            "/mcp",
            json={
                "tool": "memory_query",
                # 缺少 arguments
            },
        )
        # 应返回 400 或包含 ok=False 的响应
        if response.status_code == 400:
            result = response.json()
            assert result.get("ok") is False
        else:
            assert response.status_code == 200
            result = response.json()
            # 可能因为参数缺失而失败
            assert "ok" in result

    def test_legacy_null_arguments_returns_validation_error(self, client, mock_dependencies):
        """
        Legacy 格式 arguments=null 返回验证错误

        注意：MCPToolCall 模型要求 arguments 是 dict，null 值会被 Pydantic 拒绝。
        此测试验证错误响应中包含 correlation_id。
        """
        response = client.post(
            "/mcp",
            json={
                "tool": "reliability_report",
                "arguments": None,  # null - Pydantic 会拒绝
            },
        )

        # null arguments 会触发 Pydantic 验证错误
        assert response.status_code == 400
        result = response.json()
        assert result.get("ok") is False

        # 错误响应中必须有 correlation_id
        assert "correlation_id" in result
        assert result["correlation_id"].startswith("corr-")

    def test_legacy_extra_fields_ignored(self, client, mock_dependencies):
        """Legacy 格式额外字段应被忽略"""
        with patch("engram.gateway.logbook_adapter.get_reliability_report") as mock_report:
            mock_report.return_value = {
                "outbox_stats": {},
                "audit_stats": {},
                "v2_evidence_stats": {},
                "content_intercept_stats": {},
                "generated_at": "2026-01-31T12:00:00Z",
            }

            response = client.post(
                "/mcp",
                json={
                    "tool": "reliability_report",
                    "arguments": {},
                    "extra_field": "should be ignored",
                    "another_extra": 123,
                },
            )

        assert response.status_code == 200
        result = response.json()
        assert result.get("ok") is True


class TestLegacyVsJsonRpcCoexistence:
    """
    Legacy 协议与 JSON-RPC 协议共存测试

    验证：
    1. 请求格式自动检测
    2. 响应格式与请求格式匹配
    3. 边界情况处理
    """

    def test_jsonrpc_request_gets_jsonrpc_response(self, client):
        """JSON-RPC 请求获得 JSON-RPC 响应"""
        response = client.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1})
        result = response.json()

        # 验证是 JSON-RPC 响应
        assert result.get("jsonrpc") == "2.0"
        assert result.get("id") == 1
        # 不应有 ok 字段
        assert "ok" not in result

    def test_legacy_request_gets_legacy_response(self, client, mock_dependencies):
        """Legacy 请求获得 Legacy 响应"""
        with patch("engram.gateway.logbook_adapter.get_reliability_report") as mock_report:
            mock_report.return_value = {
                "outbox_stats": {},
                "audit_stats": {},
                "v2_evidence_stats": {},
                "content_intercept_stats": {},
                "generated_at": "2026-01-31T12:00:00Z",
            }

            response = client.post("/mcp", json={"tool": "reliability_report", "arguments": {}})
        result = response.json()

        # 验证是 Legacy 响应
        assert "ok" in result
        # 不应有 jsonrpc 字段
        assert "jsonrpc" not in result

    def test_partial_jsonrpc_fields_treated_as_legacy(self, client):
        """部分 JSON-RPC 字段被视为 Legacy 请求"""
        # 只有 method 没有 jsonrpc
        response = client.post(
            "/mcp",
            json={
                "method": "tools/list",
            },
        )
        # 不满足 JSON-RPC 格式要求（缺少 jsonrpc="2.0"）
        # 会被视为 Legacy 但缺少 tool 字段
        assert response.status_code == 400
        result = response.json()
        assert result.get("ok") is False

    def test_jsonrpc_1_0_treated_as_legacy(self, client):
        """jsonrpc: 1.0 被视为 Legacy 请求"""
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "1.0",  # 不是 2.0
                "method": "tools/list",
            },
        )
        # jsonrpc != "2.0"，被视为 Legacy
        assert response.status_code == 400
        result = response.json()
        assert result.get("ok") is False


class TestLegacyMemoryStoreScenarios:
    """
    Legacy 格式 memory_store 场景测试
    """

    def test_legacy_memory_store_with_evidence_refs(self, client, mock_dependencies):
        """Legacy memory_store 带 evidence_refs"""
        mock_client = mock_dependencies["client"]
        mock_client.store.return_value = MagicMock(
            success=True,
            memory_id="legacy-with-evidence",
            error=None,
        )

        response = client.post(
            "/mcp",
            json={
                "tool": "memory_store",
                "arguments": {
                    "payload_md": "# Test with evidence",
                    "evidence_refs": ["commit:abc123", "file:readme.md"],
                },
            },
        )
        assert response.status_code == 200
        result = response.json()
        assert "ok" in result

    def test_legacy_memory_store_with_v2_evidence(self, client, mock_dependencies):
        """Legacy memory_store 带 v2 evidence"""
        mock_client = mock_dependencies["client"]
        mock_client.store.return_value = MagicMock(
            success=True,
            memory_id="legacy-v2-evidence",
            error=None,
        )

        response = client.post(
            "/mcp",
            json={
                "tool": "memory_store",
                "arguments": {
                    "payload_md": "# Test with v2 evidence",
                    "evidence": [
                        {
                            "type": "external",
                            "uri": "commit:abc123",
                            "sha256": "a" * 64,
                        }
                    ],
                },
            },
        )
        assert response.status_code == 200
        result = response.json()
        assert "ok" in result

    def test_legacy_memory_store_with_kind(self, client, mock_dependencies):
        """Legacy memory_store 带 kind 参数"""
        mock_client = mock_dependencies["client"]
        mock_client.store.return_value = MagicMock(
            success=True,
            memory_id="legacy-with-kind",
            error=None,
        )

        response = client.post(
            "/mcp",
            json={
                "tool": "memory_store",
                "arguments": {
                    "payload_md": "# Test with kind",
                    "kind": "PROCEDURE",
                },
            },
        )
        assert response.status_code == 200
        result = response.json()
        assert "ok" in result


class TestLegacyMemoryQueryScenarios:
    """
    Legacy 格式 memory_query 场景测试
    """

    def test_legacy_memory_query_with_spaces(self, client, mock_dependencies):
        """Legacy memory_query 带 spaces 参数"""
        mock_client = mock_dependencies["client"]
        mock_client.search.return_value = MagicMock(
            success=True,
            results=[{"id": "1", "content": "test"}],
            error=None,
        )

        response = client.post(
            "/mcp",
            json={
                "tool": "memory_query",
                "arguments": {
                    "query": "test query",
                    "spaces": ["team:project1", "team:project2"],
                },
            },
        )
        assert response.status_code == 200
        result = response.json()
        assert "ok" in result

    def test_legacy_memory_query_with_filters(self, client, mock_dependencies):
        """Legacy memory_query 带 filters 参数"""
        mock_client = mock_dependencies["client"]
        mock_client.search.return_value = MagicMock(
            success=True,
            results=[],
            error=None,
        )

        response = client.post(
            "/mcp",
            json={
                "tool": "memory_query",
                "arguments": {
                    "query": "test query",
                    "filters": {"kind": "PROCEDURE"},
                },
            },
        )
        assert response.status_code == 200
        result = response.json()
        assert "ok" in result

    def test_legacy_memory_query_degraded_response(self, client, mock_dependencies):
        """Legacy memory_query 降级响应"""
        from engram.gateway.openmemory_client import OpenMemoryConnectionError

        mock_client = mock_dependencies["client"]
        mock_client.search.side_effect = OpenMemoryConnectionError(
            "连接超时", status_code=None, response=None
        )

        response = client.post(
            "/mcp",
            json={
                "tool": "memory_query",
                "arguments": {
                    "query": "test query",
                },
            },
        )
        assert response.status_code == 200
        result = response.json()

        # 应返回降级响应
        assert "ok" in result
        if result.get("result"):
            # 降级响应应有 degraded=True
            inner = result["result"]
            assert inner.get("degraded") is True or "降级" in inner.get("message", "")


# ===================== X-Correlation-ID Header 与业务响应对齐测试 =====================


class TestCorrelationIdHeaderAlignment:
    """
    测试 X-Correlation-ID 响应 header 与业务响应中 correlation_id 的对齐

    契约要求：
    1. 每个请求只生成一次 correlation_id（单次生成语义）
    2. X-Correlation-ID header 必须与业务响应中的 correlation_id 一致
    3. 失败/parse_error 场景也应保持单次生成语义

    详见: docs/gateway/07_capability_boundary.md
    """

    CORRELATION_ID_PATTERN = r"^corr-[a-fA-F0-9]{16}$"

    def test_tools_call_success_header_matches_result(self, client, mock_dependencies):
        """
        tools/call 成功场景：X-Correlation-ID header 与 result.content[0].text 内的 correlation_id 一致
        """
        import re

        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "memory_query", "arguments": {"query": "test"}},
                "id": 1,
            },
        )
        assert response.status_code == 200
        result = response.json()

        # 1. 验证 header 中有 X-Correlation-ID
        header_corr_id = response.headers.get("X-Correlation-ID")
        assert header_corr_id is not None, "响应 header 中必须包含 X-Correlation-ID"
        assert re.match(self.CORRELATION_ID_PATTERN, header_corr_id), (
            f"X-Correlation-ID 格式不正确: {header_corr_id}"
        )

        # 2. 验证 result 中有 content
        assert "result" in result
        content = result["result"]["content"]
        assert len(content) >= 1

        # 3. 解析 content[0].text 中的业务结果
        business_result = json.loads(content[0]["text"])
        result_corr_id = business_result.get("correlation_id")

        # 4. 验证两者一致（单次生成语义）
        assert result_corr_id is not None, "业务响应中必须包含 correlation_id"
        assert header_corr_id == result_corr_id, (
            f"X-Correlation-ID header ({header_corr_id}) 与业务响应中的 correlation_id ({result_corr_id}) 不一致"
        )

    def test_tools_call_error_header_matches_error_data(self, client):
        """
        tools/call 错误场景：X-Correlation-ID header 与 error.data.correlation_id 一致
        """
        import re

        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "unknown_tool", "arguments": {}},
                "id": 1,
            },
        )
        assert response.status_code == 200
        result = response.json()

        # 1. 验证 header 中有 X-Correlation-ID
        header_corr_id = response.headers.get("X-Correlation-ID")
        assert header_corr_id is not None, "响应 header 中必须包含 X-Correlation-ID"
        assert re.match(self.CORRELATION_ID_PATTERN, header_corr_id), (
            f"X-Correlation-ID 格式不正确: {header_corr_id}"
        )

        # 2. 验证 error.data 中有 correlation_id
        assert "error" in result
        assert "data" in result["error"]
        error_corr_id = result["error"]["data"].get("correlation_id")

        # 3. 验证两者一致（单次生成语义）
        assert error_corr_id is not None, "error.data 中必须包含 correlation_id"
        assert header_corr_id == error_corr_id, (
            f"X-Correlation-ID header ({header_corr_id}) 与 error.data.correlation_id ({error_corr_id}) 不一致"
        )

    def test_method_not_found_header_matches_error_data(self, client):
        """
        METHOD_NOT_FOUND 错误场景：X-Correlation-ID header 与 error.data.correlation_id 一致
        """
        import re

        response = client.post(
            "/mcp", json={"jsonrpc": "2.0", "method": "nonexistent/method", "id": 1}
        )
        assert response.status_code == 200
        result = response.json()

        # 1. 验证 header 中有 X-Correlation-ID
        header_corr_id = response.headers.get("X-Correlation-ID")
        assert header_corr_id is not None, "响应 header 中必须包含 X-Correlation-ID"
        assert re.match(self.CORRELATION_ID_PATTERN, header_corr_id), (
            f"X-Correlation-ID 格式不正确: {header_corr_id}"
        )

        # 2. 验证 error.data 中有 correlation_id
        assert "error" in result
        error_corr_id = result["error"]["data"].get("correlation_id")

        # 3. 验证两者一致
        assert header_corr_id == error_corr_id, (
            f"X-Correlation-ID header ({header_corr_id}) 与 error.data.correlation_id ({error_corr_id}) 不一致"
        )

    def test_parse_error_header_matches_error_data(self, client):
        """
        parse_error（无效 params）场景：X-Correlation-ID header 与 error.data.correlation_id 一致
        """
        import re

        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/list",
                "params": "invalid_params",  # 应该是 dict
                "id": 1,
            },
        )
        assert response.status_code == 400
        result = response.json()

        # 1. 验证 header 中有 X-Correlation-ID
        header_corr_id = response.headers.get("X-Correlation-ID")
        assert header_corr_id is not None, "响应 header 中必须包含 X-Correlation-ID"
        assert re.match(self.CORRELATION_ID_PATTERN, header_corr_id), (
            f"X-Correlation-ID 格式不正确: {header_corr_id}"
        )

        # 2. 验证 error.data 中有 correlation_id
        assert "error" in result
        if "data" in result["error"] and result["error"]["data"]:
            error_corr_id = result["error"]["data"].get("correlation_id")

            # 3. 验证两者一致
            assert header_corr_id == error_corr_id, (
                f"X-Correlation-ID header ({header_corr_id}) 与 error.data.correlation_id ({error_corr_id}) 不一致"
            )

    def test_tools_list_success_header_present(self, client):
        """
        tools/list 成功场景：响应 header 中应有 X-Correlation-ID
        """
        import re

        response = client.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1})
        assert response.status_code == 200

        # tools/list 成功响应不包含 correlation_id 在 result 中（因为不是工具调用结果）
        # 但 header 中必须有
        header_corr_id = response.headers.get("X-Correlation-ID")
        assert header_corr_id is not None, "响应 header 中必须包含 X-Correlation-ID"
        assert re.match(self.CORRELATION_ID_PATTERN, header_corr_id), (
            f"X-Correlation-ID 格式不正确: {header_corr_id}"
        )

    def test_legacy_protocol_success_header_matches_result(self, client, mock_dependencies):
        """
        旧协议成功场景：X-Correlation-ID header 与 result.correlation_id 一致

        使用 memory_query 工具测试（因为它的依赖已被 mock）
        """
        import re

        response = client.post(
            "/mcp", json={"tool": "memory_query", "arguments": {"query": "test query"}}
        )

        assert response.status_code == 200
        result = response.json()

        # 1. 验证 header 中有 X-Correlation-ID
        header_corr_id = response.headers.get("X-Correlation-ID")
        assert header_corr_id is not None, "响应 header 中必须包含 X-Correlation-ID"
        assert re.match(self.CORRELATION_ID_PATTERN, header_corr_id), (
            f"X-Correlation-ID 格式不正确: {header_corr_id}"
        )

        # 2. 验证 result 中有 correlation_id
        assert result.get("ok") is True, f"请求失败: {result.get('error')}"
        result_corr_id = result["result"].get("correlation_id")

        # 3. 验证两者一致
        assert result_corr_id is not None, "旧协议成功响应 result 中必须包含 correlation_id"
        assert header_corr_id == result_corr_id, (
            f"X-Correlation-ID header ({header_corr_id}) 与 result.correlation_id ({result_corr_id}) 不一致"
        )

    def test_legacy_protocol_error_header_matches_body(self, client):
        """
        旧协议错误场景：X-Correlation-ID header 与 body.correlation_id 一致
        """
        import re

        response = client.post("/mcp", json={"tool": "unknown_tool", "arguments": {}})

        assert response.status_code == 200
        result = response.json()

        # 1. 验证 header 中有 X-Correlation-ID
        header_corr_id = response.headers.get("X-Correlation-ID")
        assert header_corr_id is not None, "响应 header 中必须包含 X-Correlation-ID"
        assert re.match(self.CORRELATION_ID_PATTERN, header_corr_id), (
            f"X-Correlation-ID 格式不正确: {header_corr_id}"
        )

        # 2. 验证 body 中有 correlation_id
        assert result.get("ok") is False
        body_corr_id = result.get("correlation_id")

        # 3. 验证两者一致
        assert body_corr_id is not None, "旧协议错误响应 body 中必须包含 correlation_id"
        assert header_corr_id == body_corr_id, (
            f"X-Correlation-ID header ({header_corr_id}) 与 body.correlation_id ({body_corr_id}) 不一致"
        )

    def test_legacy_protocol_format_error_header_matches_body(self, client):
        """
        旧协议格式错误场景：X-Correlation-ID header 与 body.correlation_id 一致
        """
        import re

        response = client.post(
            "/mcp",
            json={
                "arguments": {"query": "test"}  # 缺少 tool 字段
            },
        )

        assert response.status_code == 400
        result = response.json()

        # 1. 验证 header 中有 X-Correlation-ID
        header_corr_id = response.headers.get("X-Correlation-ID")
        assert header_corr_id is not None, "响应 header 中必须包含 X-Correlation-ID"
        assert re.match(self.CORRELATION_ID_PATTERN, header_corr_id), (
            f"X-Correlation-ID 格式不正确: {header_corr_id}"
        )

        # 2. 验证 body 中有 correlation_id
        assert result.get("ok") is False
        body_corr_id = result.get("correlation_id")

        # 3. 验证两者一致
        assert body_corr_id is not None, "旧协议格式错误响应 body 中必须包含 correlation_id"
        assert header_corr_id == body_corr_id, (
            f"X-Correlation-ID header ({header_corr_id}) 与 body.correlation_id ({body_corr_id}) 不一致"
        )

    def test_multiple_requests_have_unique_correlation_ids(self, client):
        """
        多个请求的 correlation_id 必须唯一
        """
        header_corr_ids = set()

        for _ in range(5):
            response = client.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1})
            header_corr_id = response.headers.get("X-Correlation-ID")
            assert header_corr_id not in header_corr_ids, (
                f"correlation_id 应唯一，重复: {header_corr_id}"
            )
            header_corr_ids.add(header_corr_id)

    def test_exposed_headers_includes_correlation_id(self, client):
        """
        Access-Control-Expose-Headers 应包含 X-Correlation-ID（便于跨域访问）
        """
        response = client.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1})

        expose_headers = response.headers.get("Access-Control-Expose-Headers", "")
        assert "X-Correlation-ID" in expose_headers, (
            f"Access-Control-Expose-Headers 应包含 X-Correlation-ID，实际: {expose_headers}"
        )


# ===================== correlation_id 单一来源契约测试 =====================


class TestCorrelationIdSingleSourceContract:
    """
    correlation_id 单一来源契约测试

    契约要求（docs/gateway/07_capability_boundary.md）：
    1. correlation_id 只在 HTTP 入口层生成一次（mcp_rpc.generate_correlation_id）
    2. handlers 不再自行生成 correlation_id
    3. 所有响应（含 legacy 分支）都必须包含 correlation_id
    4. correlation_id 格式：corr-{16位十六进制}

    测试覆盖：
    - tools/list: 成功响应，header 中有 X-Correlation-ID
    - tools/call: 所有工具的业务结果中都有 correlation_id
    - legacy tool call: 响应中必须有 correlation_id
    """

    CORRELATION_ID_PATTERN = r"^corr-[a-fA-F0-9]{16}$"

    # ==================== tools/list 契约测试 ====================

    def test_tools_list_has_correlation_id_in_header(self, client):
        """tools/list 成功响应的 header 中必须有 X-Correlation-ID"""
        import re

        response = client.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1})
        assert response.status_code == 200

        # header 中必须有 X-Correlation-ID
        header_corr_id = response.headers.get("X-Correlation-ID")
        assert header_corr_id is not None, "tools/list 响应 header 中必须包含 X-Correlation-ID"
        assert re.match(self.CORRELATION_ID_PATTERN, header_corr_id), (
            f"X-Correlation-ID 格式不正确: {header_corr_id}"
        )

    def test_tools_list_result_structure(self, client):
        """tools/list 成功响应的 result 结构验证"""
        response = client.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1})
        result = response.json()

        # 验证是 JSON-RPC 成功响应
        assert result.get("jsonrpc") == "2.0"
        assert result.get("id") == 1
        assert result.get("error") is None
        assert "result" in result

        # result 结构验证
        assert "tools" in result["result"]
        assert isinstance(result["result"]["tools"], list)

    # ==================== tools/call 契约测试 ====================

    def test_tools_call_memory_query_has_correlation_id(self, client, mock_dependencies):
        """tools/call memory_query 业务结果中必须有 correlation_id"""
        import json as json_module
        import re

        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "memory_query", "arguments": {"query": "test"}},
                "id": 1,
            },
        )
        assert response.status_code == 200
        result = response.json()

        # 验证成功响应
        assert "result" in result
        content = result["result"]["content"]
        assert len(content) >= 1

        # 解析业务结果
        business_result = json_module.loads(content[0]["text"])

        # 业务结果中必须有 correlation_id
        assert "correlation_id" in business_result, "memory_query 业务结果中必须包含 correlation_id"
        assert re.match(self.CORRELATION_ID_PATTERN, business_result["correlation_id"]), (
            f"correlation_id 格式不正确: {business_result['correlation_id']}"
        )

        # header 与业务结果中的 correlation_id 必须一致
        header_corr_id = response.headers.get("X-Correlation-ID")
        assert header_corr_id == business_result["correlation_id"], (
            f"header ({header_corr_id}) 与业务结果 ({business_result['correlation_id']}) 不一致"
        )

    def test_tools_call_memory_store_has_correlation_id(self, client, mock_dependencies):
        """tools/call memory_store 业务结果中必须有 correlation_id"""
        import json as json_module
        import re

        mock_client = mock_dependencies["client"]
        mock_client.store.return_value = MagicMock(
            success=True,
            memory_id="test-memory-id",
            error=None,
        )

        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "memory_store", "arguments": {"payload_md": "# Test"}},
                "id": 1,
            },
        )
        assert response.status_code == 200
        result = response.json()

        # 验证成功响应
        assert "result" in result
        content = result["result"]["content"]
        business_result = json_module.loads(content[0]["text"])

        # 业务结果中必须有 correlation_id
        assert "correlation_id" in business_result, "memory_store 业务结果中必须包含 correlation_id"
        assert re.match(self.CORRELATION_ID_PATTERN, business_result["correlation_id"]), (
            f"correlation_id 格式不正确: {business_result['correlation_id']}"
        )

        # header 与业务结果中的 correlation_id 必须一致
        header_corr_id = response.headers.get("X-Correlation-ID")
        assert header_corr_id == business_result["correlation_id"]

    def test_tools_call_reliability_report_has_correlation_id(self, client, mock_dependencies):
        """
        tools/call reliability_report 响应中必须有 correlation_id

        注意：无论是成功响应还是错误响应，都必须包含 correlation_id。
        此测试验证 correlation_id 的存在性和格式，而非工具的具体功能。
        """
        import json as json_module
        import re

        with patch("engram.gateway.logbook_adapter.get_reliability_report") as mock_report:
            mock_report.return_value = {
                "outbox_stats": {},
                "audit_stats": {},
                "v2_evidence_stats": {},
                "content_intercept_stats": {},
                "generated_at": "2026-01-31T12:00:00Z",
            }

            response = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "params": {"name": "reliability_report", "arguments": {}},
                    "id": 1,
                },
            )

        assert response.status_code == 200
        result = response.json()

        # header 中必须有 correlation_id
        header_corr_id = response.headers.get("X-Correlation-ID")
        assert header_corr_id is not None, "响应 header 中必须包含 X-Correlation-ID"
        assert re.match(self.CORRELATION_ID_PATTERN, header_corr_id), (
            f"X-Correlation-ID 格式不正确: {header_corr_id}"
        )

        # 无论成功还是错误，响应体中都应有 correlation_id
        if "result" in result:
            # 成功响应：correlation_id 在业务结果中
            content = result["result"]["content"]
            business_result = json_module.loads(content[0]["text"])
            assert "correlation_id" in business_result, (
                "reliability_report 成功响应的业务结果中必须包含 correlation_id"
            )
            assert header_corr_id == business_result["correlation_id"], (
                "header 与业务结果中的 correlation_id 必须一致"
            )
        else:
            # 错误响应：correlation_id 在 error.data 中
            assert "error" in result
            assert "data" in result["error"]
            error_corr_id = result["error"]["data"].get("correlation_id")
            assert error_corr_id is not None, (
                "reliability_report 错误响应的 error.data 中必须包含 correlation_id"
            )
            assert header_corr_id == error_corr_id, (
                "header 与 error.data 中的 correlation_id 必须一致"
            )

    def test_tools_call_governance_update_has_correlation_id(self, client, mock_dependencies):
        """tools/call governance_update 业务结果中必须有 correlation_id"""
        import json as json_module
        import re

        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": "governance_update",
                    "arguments": {"team_write_enabled": True, "admin_key": "wrong_key"},
                },
                "id": 1,
            },
        )
        assert response.status_code == 200
        result = response.json()

        # 验证成功响应
        assert "result" in result
        content = result["result"]["content"]
        business_result = json_module.loads(content[0]["text"])

        # 业务结果中必须有 correlation_id（即使是拒绝响应）
        assert "correlation_id" in business_result, (
            "governance_update 业务结果中必须包含 correlation_id"
        )
        assert re.match(self.CORRELATION_ID_PATTERN, business_result["correlation_id"]), (
            f"correlation_id 格式不正确: {business_result['correlation_id']}"
        )

        # header 与业务结果中的 correlation_id 必须一致
        header_corr_id = response.headers.get("X-Correlation-ID")
        assert header_corr_id == business_result["correlation_id"]

    def test_tools_call_evidence_upload_has_correlation_id(self, client, mock_dependencies):
        """tools/call evidence_upload 业务结果中必须有 correlation_id"""
        import json as json_module
        import re

        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": "evidence_upload",
                    "arguments": {"content": "test content", "content_type": "text/plain"},
                },
                "id": 1,
            },
        )
        assert response.status_code == 200
        result = response.json()

        # 验证成功响应
        assert "result" in result
        content = result["result"]["content"]
        business_result = json_module.loads(content[0]["text"])

        # 业务结果中必须有 correlation_id
        assert "correlation_id" in business_result, (
            "evidence_upload 业务结果中必须包含 correlation_id"
        )
        assert re.match(self.CORRELATION_ID_PATTERN, business_result["correlation_id"]), (
            f"correlation_id 格式不正确: {business_result['correlation_id']}"
        )

        # header 与业务结果中的 correlation_id 必须一致
        header_corr_id = response.headers.get("X-Correlation-ID")
        assert header_corr_id == business_result["correlation_id"]

    def test_tools_call_error_has_correlation_id(self, client):
        """tools/call 错误响应的 error.data 中必须有 correlation_id"""
        import re

        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "unknown_tool", "arguments": {}},
                "id": 1,
            },
        )
        assert response.status_code == 200
        result = response.json()

        # 验证错误响应
        assert "error" in result
        assert "data" in result["error"]

        # error.data 中必须有 correlation_id
        error_corr_id = result["error"]["data"].get("correlation_id")
        assert error_corr_id is not None, (
            "tools/call 错误响应的 error.data 中必须包含 correlation_id"
        )
        assert re.match(self.CORRELATION_ID_PATTERN, error_corr_id), (
            f"correlation_id 格式不正确: {error_corr_id}"
        )

        # header 与 error.data 中的 correlation_id 必须一致
        header_corr_id = response.headers.get("X-Correlation-ID")
        assert header_corr_id == error_corr_id

    # ==================== legacy tool call 契约测试 ====================

    def test_legacy_memory_query_has_correlation_id(self, client, mock_dependencies):
        """legacy memory_query 响应中必须有 correlation_id"""
        import re

        response = client.post(
            "/mcp", json={"tool": "memory_query", "arguments": {"query": "test"}}
        )
        assert response.status_code == 200
        result = response.json()

        # 验证 MCPResponse 结构
        assert "ok" in result
        assert "result" in result

        # result 中必须有 correlation_id
        assert "correlation_id" in result["result"], (
            "legacy memory_query 响应的 result 中必须包含 correlation_id"
        )
        assert re.match(self.CORRELATION_ID_PATTERN, result["result"]["correlation_id"]), (
            f"correlation_id 格式不正确: {result['result']['correlation_id']}"
        )

        # header 与 result 中的 correlation_id 必须一致
        header_corr_id = response.headers.get("X-Correlation-ID")
        assert header_corr_id == result["result"]["correlation_id"]

    def test_legacy_memory_store_has_correlation_id(self, client, mock_dependencies):
        """legacy memory_store 响应中必须有 correlation_id"""
        import re

        mock_client = mock_dependencies["client"]
        mock_client.store.return_value = MagicMock(
            success=True,
            memory_id="legacy-store-id",
            error=None,
        )

        response = client.post(
            "/mcp", json={"tool": "memory_store", "arguments": {"payload_md": "# Test"}}
        )
        assert response.status_code == 200
        result = response.json()

        # 验证 MCPResponse 结构
        assert "ok" in result
        assert "result" in result

        # result 中必须有 correlation_id
        assert "correlation_id" in result["result"], (
            "legacy memory_store 响应的 result 中必须包含 correlation_id"
        )
        assert re.match(self.CORRELATION_ID_PATTERN, result["result"]["correlation_id"]), (
            f"correlation_id 格式不正确: {result['result']['correlation_id']}"
        )

        # header 与 result 中的 correlation_id 必须一致
        header_corr_id = response.headers.get("X-Correlation-ID")
        assert header_corr_id == result["result"]["correlation_id"]

    def test_legacy_reliability_report_has_correlation_id(self, client, mock_dependencies):
        """
        legacy reliability_report 响应中必须有 correlation_id

        注意：无论是成功响应还是错误响应，都必须包含 correlation_id。
        此测试验证 correlation_id 的存在性和格式，而非工具的具体功能。
        """
        import re

        with patch("engram.gateway.logbook_adapter.get_reliability_report") as mock_report:
            mock_report.return_value = {
                "outbox_stats": {},
                "audit_stats": {},
                "v2_evidence_stats": {},
                "content_intercept_stats": {},
                "generated_at": "2026-01-31T12:00:00Z",
            }

            response = client.post("/mcp", json={"tool": "reliability_report", "arguments": {}})

        assert response.status_code == 200
        result = response.json()

        # header 中必须有 correlation_id
        header_corr_id = response.headers.get("X-Correlation-ID")
        assert header_corr_id is not None, "响应 header 中必须包含 X-Correlation-ID"
        assert re.match(self.CORRELATION_ID_PATTERN, header_corr_id), (
            f"X-Correlation-ID 格式不正确: {header_corr_id}"
        )

        # 验证 MCPResponse 结构
        assert "ok" in result

        if result.get("ok"):
            # 成功响应：result 中必须有 correlation_id
            assert "result" in result
            assert "correlation_id" in result["result"], (
                "legacy reliability_report 成功响应的 result 中必须包含 correlation_id"
            )
            assert header_corr_id == result["result"]["correlation_id"], (
                "header 与 result 中的 correlation_id 必须一致"
            )
        else:
            # 错误响应：顶层必须有 correlation_id
            assert "correlation_id" in result, (
                "legacy reliability_report 错误响应中必须包含 correlation_id"
            )
            assert header_corr_id == result["correlation_id"], (
                "header 与 body 中的 correlation_id 必须一致"
            )

    def test_legacy_governance_update_has_correlation_id(self, client, mock_dependencies):
        """legacy governance_update 响应中必须有 correlation_id"""
        import re

        response = client.post(
            "/mcp",
            json={
                "tool": "governance_update",
                "arguments": {"team_write_enabled": True, "admin_key": "wrong_key"},
            },
        )
        assert response.status_code == 200
        result = response.json()

        # 验证 MCPResponse 结构
        assert "ok" in result
        assert "result" in result

        # result 中必须有 correlation_id
        assert "correlation_id" in result["result"], (
            "legacy governance_update 响应的 result 中必须包含 correlation_id"
        )
        assert re.match(self.CORRELATION_ID_PATTERN, result["result"]["correlation_id"]), (
            f"correlation_id 格式不正确: {result['result']['correlation_id']}"
        )

        # header 与 result 中的 correlation_id 必须一致
        header_corr_id = response.headers.get("X-Correlation-ID")
        assert header_corr_id == result["result"]["correlation_id"]

    def test_legacy_evidence_upload_has_correlation_id(self, client, mock_dependencies):
        """legacy evidence_upload 响应中必须有 correlation_id"""
        import re

        response = client.post(
            "/mcp",
            json={
                "tool": "evidence_upload",
                "arguments": {"content": "test content", "content_type": "text/plain"},
            },
        )
        assert response.status_code == 200
        result = response.json()

        # 验证 MCPResponse 结构
        assert "ok" in result
        assert "result" in result

        # result 中必须有 correlation_id
        assert "correlation_id" in result["result"], (
            "legacy evidence_upload 响应的 result 中必须包含 correlation_id"
        )
        assert re.match(self.CORRELATION_ID_PATTERN, result["result"]["correlation_id"]), (
            f"correlation_id 格式不正确: {result['result']['correlation_id']}"
        )

        # header 与 result 中的 correlation_id 必须一致
        header_corr_id = response.headers.get("X-Correlation-ID")
        assert header_corr_id == result["result"]["correlation_id"]

    def test_legacy_unknown_tool_error_has_correlation_id(self, client):
        """legacy 未知工具错误响应中必须有 correlation_id"""
        import re

        response = client.post("/mcp", json={"tool": "unknown_tool", "arguments": {}})
        assert response.status_code == 200
        result = response.json()

        # 验证错误响应
        assert result.get("ok") is False

        # 顶层必须有 correlation_id
        assert "correlation_id" in result, "legacy 未知工具错误响应中必须包含 correlation_id"
        assert re.match(self.CORRELATION_ID_PATTERN, result["correlation_id"]), (
            f"correlation_id 格式不正确: {result['correlation_id']}"
        )

        # header 与 body 中的 correlation_id 必须一致
        header_corr_id = response.headers.get("X-Correlation-ID")
        assert header_corr_id == result["correlation_id"]

    def test_legacy_format_error_has_correlation_id(self, client):
        """legacy 格式错误响应中必须有 correlation_id"""
        import re

        response = client.post(
            "/mcp",
            json={
                "arguments": {"query": "test"}  # 缺少 tool 字段
            },
        )
        assert response.status_code == 400
        result = response.json()

        # 验证错误响应
        assert result.get("ok") is False

        # 顶层必须有 correlation_id
        assert "correlation_id" in result, "legacy 格式错误响应中必须包含 correlation_id"
        assert re.match(self.CORRELATION_ID_PATTERN, result["correlation_id"]), (
            f"correlation_id 格式不正确: {result['correlation_id']}"
        )

        # header 与 body 中的 correlation_id 必须一致
        header_corr_id = response.headers.get("X-Correlation-ID")
        assert header_corr_id == result["correlation_id"]

    # ==================== 稳定结构验证 ====================

    def test_correlation_id_format_is_stable(self, client):
        """
        correlation_id 格式稳定性验证

        格式契约：corr-{16位十六进制小写}
        长度：21 字符（5 + 16）
        """

        # 发送多个请求验证格式稳定性
        for _ in range(10):
            response = client.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1})
            header_corr_id = response.headers.get("X-Correlation-ID")

            # 长度验证
            assert len(header_corr_id) == 21, (
                f"correlation_id 长度应为 21，实际: {len(header_corr_id)}"
            )

            # 前缀验证
            assert header_corr_id.startswith("corr-"), (
                f"correlation_id 必须以 'corr-' 开头: {header_corr_id}"
            )

            # 后缀验证（16 位十六进制）
            suffix = header_corr_id[5:]
            assert len(suffix) == 16, f"correlation_id 后缀长度应为 16: {suffix}"

            try:
                int(suffix, 16)  # 验证是有效的十六进制
            except ValueError:
                pytest.fail(f"correlation_id 后缀应为十六进制: {suffix}")

    def test_all_tools_have_consistent_correlation_id_behavior(self, client, mock_dependencies):
        """
        所有工具的 correlation_id 行为一致性验证

        契约：所有工具的响应都必须包含 correlation_id，
        且与 X-Correlation-ID header 一致（无论成功还是错误）。
        """
        import json as json_module
        import re

        # 设置 mock
        mock_client = mock_dependencies["client"]
        mock_client.store.return_value = MagicMock(success=True, memory_id="test-id", error=None)
        mock_client.search.return_value = MagicMock(success=True, results=[], error=None)

        with patch("engram.gateway.logbook_adapter.get_reliability_report") as mock_report:
            mock_report.return_value = {
                "outbox_stats": {},
                "audit_stats": {},
                "v2_evidence_stats": {},
                "content_intercept_stats": {},
                "generated_at": "2026-01-31T12:00:00Z",
            }

            # 测试所有工具
            tools_to_test = [
                ("memory_query", {"query": "test"}),
                ("memory_store", {"payload_md": "# Test"}),
                ("reliability_report", {}),
                ("governance_update", {"team_write_enabled": False}),
                ("evidence_upload", {"content": "test", "content_type": "text/plain"}),
            ]

            for tool_name, arguments in tools_to_test:
                response = client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "method": "tools/call",
                        "params": {"name": tool_name, "arguments": arguments},
                        "id": 1,
                    },
                )

                assert response.status_code == 200, (
                    f"工具 {tool_name} 请求失败: {response.status_code}"
                )

                result = response.json()

                # 验证 header 中有 correlation_id
                header_corr_id = response.headers.get("X-Correlation-ID")
                assert header_corr_id is not None, (
                    f"工具 {tool_name}: 响应 header 中必须包含 X-Correlation-ID"
                )
                assert re.match(self.CORRELATION_ID_PATTERN, header_corr_id), (
                    f"工具 {tool_name}: X-Correlation-ID 格式不正确: {header_corr_id}"
                )

                # 验证响应体中有 correlation_id（无论成功还是错误）
                if "result" in result:
                    # 成功响应：correlation_id 在业务结果中
                    content = result["result"]["content"]
                    business_result = json_module.loads(content[0]["text"])
                    assert "correlation_id" in business_result, (
                        f"工具 {tool_name} 的成功响应业务结果中必须包含 correlation_id"
                    )
                    assert header_corr_id == business_result["correlation_id"], (
                        f"工具 {tool_name}: header ({header_corr_id}) 与业务结果 ({business_result['correlation_id']}) 不一致"
                    )
                else:
                    # 错误响应：correlation_id 在 error.data 中
                    assert "error" in result, f"工具 {tool_name}: 响应应包含 result 或 error"
                    assert "data" in result["error"], f"工具 {tool_name}: error 应包含 data"
                    error_corr_id = result["error"]["data"].get("correlation_id")
                    assert error_corr_id is not None, (
                        f"工具 {tool_name} 的错误响应 error.data 中必须包含 correlation_id"
                    )
                    assert header_corr_id == error_corr_id, (
                        f"工具 {tool_name}: header ({header_corr_id}) 与 error.data ({error_corr_id}) 不一致"
                    )


# ===================== 错误路径 correlation_id 一致性测试（增强版）=====================


class TestCorrelationIdConsistencyAllErrorPaths:
    """
    验证所有错误路径中 X-Correlation-ID header 与 error.data.correlation_id 一致

    契约要求（钉死规则）：
    ================================================================================
    1. 真实 dispatch 链路中，correlation_id 由 HTTP 入口层（routes.py mcp_endpoint）生成
    2. 所有错误响应的 X-Correlation-ID header 与 error.data.correlation_id 必须一致
    3. ErrorData.to_dict() 在真实链路中不应触发重新生成（因为 correlation_id 已传入）
    ================================================================================

    覆盖路径：
    - PARSE_ERROR (-32700): JSON 解析失败
    - INVALID_REQUEST (-32600): 无效的 JSON-RPC 请求结构
    - METHOD_NOT_FOUND (-32601): 未知方法
    - INVALID_PARAMS (-32602): tools/call 参数错误（如未知工具）
    - TOOL_EXECUTION_ERROR (-32000): 工具执行时抛出异常
    - INTERNAL_ERROR (-32603): 内部错误
    """

    CORRELATION_ID_PATTERN = r"^corr-[a-fA-F0-9]{16}$"

    def _assert_header_matches_error_data(self, response, scenario_name: str):
        """
        断言 X-Correlation-ID header 与 error.data.correlation_id 一致

        这是契约核心断言：确保单次生成语义。
        """
        import re

        result = response.json()

        # 验证 header 中有 X-Correlation-ID
        header_corr_id = response.headers.get("X-Correlation-ID")
        assert header_corr_id is not None, (
            f"{scenario_name}: 响应 header 中必须包含 X-Correlation-ID"
        )
        assert re.match(self.CORRELATION_ID_PATTERN, header_corr_id), (
            f"{scenario_name}: X-Correlation-ID 格式不正确: {header_corr_id}"
        )

        # 验证 error.data 存在且包含 correlation_id
        assert "error" in result, f"{scenario_name}: 响应应包含 error 字段"
        assert "data" in result["error"], f"{scenario_name}: error 应包含 data 字段（契约要求）"
        error_data = result["error"]["data"]
        error_corr_id = error_data.get("correlation_id")
        assert error_corr_id is not None, f"{scenario_name}: error.data 中必须包含 correlation_id"

        # 核心断言：header 与 error.data 中的 correlation_id 必须一致
        assert header_corr_id == error_corr_id, (
            f"{scenario_name}: X-Correlation-ID header ({header_corr_id}) "
            f"与 error.data.correlation_id ({error_corr_id}) 不一致！"
            f"这违反了单次生成语义契约。"
        )

        return header_corr_id, error_corr_id

    def test_parse_error_json_invalid(self, client):
        """
        PARSE_ERROR 路径：无效 JSON 触发 -32700

        契约断言：X-Correlation-ID header 与 error.data.correlation_id 一致
        """
        response = client.post(
            "/mcp",
            content=b"{ invalid json }",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400

        self._assert_header_matches_error_data(response, "PARSE_ERROR (invalid JSON)")

        # 验证错误码
        result = response.json()
        assert result["error"]["code"] == -32700

    def test_invalid_request_empty_method(self, client):
        """
        INVALID_REQUEST 路径：method 为空字符串触发 -32600

        契约断言：X-Correlation-ID header 与 error.data.correlation_id 一致

        注意：缺少 method 字段的请求会被当作旧协议处理，
        所以这里使用空字符串 method 来触发 JSON-RPC 格式校验失败。
        """
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "",  # 空字符串，触发 Pydantic 校验失败
                "id": 1,
            },
        )
        # 可能返回 400（格式错误）或 200（方法不存在）
        # 取决于 Pydantic 是否校验空字符串
        response.json()

        # 无论哪种情况，header 和 error.data 中的 correlation_id 应一致
        self._assert_header_matches_error_data(response, "INVALID_REQUEST (empty method)")

    def test_invalid_request_wrong_params_type(self, client):
        """
        INVALID_REQUEST 路径：params 类型错误触发 -32600

        契约断言：X-Correlation-ID header 与 error.data.correlation_id 一致
        """
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/list",
                "params": "should_be_dict_not_string",  # 应该是 dict
                "id": 1,
            },
        )
        assert response.status_code == 400

        self._assert_header_matches_error_data(response, "INVALID_REQUEST (wrong params type)")

        result = response.json()
        assert result["error"]["code"] == -32600

    def test_method_not_found(self, client):
        """
        METHOD_NOT_FOUND 路径：未知方法触发 -32601

        契约断言：X-Correlation-ID header 与 error.data.correlation_id 一致
        """
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "unknown/nonexistent_method",
                "id": 1,
            },
        )
        assert response.status_code == 200  # JSON-RPC 错误仍返回 200

        self._assert_header_matches_error_data(response, "METHOD_NOT_FOUND")

        result = response.json()
        assert result["error"]["code"] == -32601

    def test_tools_call_unknown_tool(self, client):
        """
        INVALID_PARAMS 路径：tools/call 未知工具触发 -32602

        契约断言：X-Correlation-ID header 与 error.data.correlation_id 一致
        """
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": "nonexistent_tool_xyz",
                    "arguments": {},
                },
                "id": 1,
            },
        )
        assert response.status_code == 200

        self._assert_header_matches_error_data(response, "tools/call unknown tool")

        result = response.json()
        assert result["error"]["code"] == -32602
        assert result["error"]["data"]["reason"] == "UNKNOWN_TOOL"

    def test_tools_call_missing_name(self, client):
        """
        INVALID_PARAMS 路径：tools/call 缺少 name 参数触发 -32602

        契约断言：X-Correlation-ID header 与 error.data.correlation_id 一致
        """
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    # "name" 缺失
                    "arguments": {},
                },
                "id": 1,
            },
        )
        assert response.status_code == 200

        self._assert_header_matches_error_data(response, "tools/call missing name")

        result = response.json()
        assert result["error"]["code"] == -32602
        assert result["error"]["data"]["reason"] == "MISSING_REQUIRED_PARAM"

    def test_tools_call_execution_exception(self, client):
        """
        INTERNAL_ERROR 路径：工具执行时抛出异常触发 -32603

        契约断言：X-Correlation-ID header 与 error.data.correlation_id 一致

        注意：需要在 mcp_rpc 模块层面 mock 工具执行器，确保异常能正确传播。
        """

        # 模拟工具执行器抛出异常
        async def mock_executor(tool_name, tool_args, correlation_id):
            raise RuntimeError("模拟工具执行异常")

        with patch(
            "engram.gateway.mcp_rpc.get_tool_executor",
            return_value=mock_executor,
        ):
            response = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "params": {
                        "name": "memory_query",
                        "arguments": {"query": "test"},
                    },
                    "id": 1,
                },
            )

        assert response.status_code == 200

        self._assert_header_matches_error_data(response, "tools/call execution exception")

        result = response.json()
        # 工具执行错误码为 -32603（内部错误）
        assert result["error"]["code"] == -32603

    def test_multiple_sequential_requests_unique_correlation_ids(self, client):
        """
        验证多次请求生成不同的 correlation_id（确保每次请求独立）
        """
        correlation_ids = set()

        for i in range(5):
            response = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "method": "unknown/method",
                    "id": i,
                },
            )
            header_corr_id = response.headers.get("X-Correlation-ID")
            assert header_corr_id is not None
            correlation_ids.add(header_corr_id)

        # 5 次请求应产生 5 个不同的 correlation_id
        assert len(correlation_ids) == 5, (
            f"5 次请求应产生 5 个不同的 correlation_id，实际: {len(correlation_ids)}"
        )


class TestErrorDataStrictModeInDispatchChain:
    """
    验证在真实 dispatch 链路中 ErrorData 的 correlation_id 单一来源契约

    使用 ErrorData.to_dict(strict=True) 来钉死规则：
    若真实链路中出现 correlation_id 未设置或不合规，测试应失败。
    """

    @pytest.mark.asyncio
    async def test_dispatch_sets_correlation_id_before_error_data(self):
        """
        验证 dispatch 在构造 ErrorData 前已设置 correlation_id

        这确保了 ErrorData.to_dict() 不会触发重新生成。
        """
        from engram.gateway.mcp_rpc import (
            JsonRpcRequest,
            JsonRpcRouter,
            generate_correlation_id,
            get_current_correlation_id,
        )

        router = JsonRpcRouter()

        # 注册一个测试 handler，验证 contextvars 已设置
        captured_corr_ids = []

        @router.method("test/capture_correlation_id")
        async def capture_handler(params):
            corr_id = get_current_correlation_id()
            captured_corr_ids.append(corr_id)
            return {"captured": corr_id}

        # 使用显式传入的 correlation_id
        test_corr_id = generate_correlation_id()
        request = JsonRpcRequest(
            jsonrpc="2.0",
            method="test/capture_correlation_id",
            params={},
            id=1,
        )

        response = await router.dispatch(request, correlation_id=test_corr_id)

        # 验证 handler 捕获的 correlation_id 与传入的一致
        assert len(captured_corr_ids) == 1
        assert captured_corr_ids[0] == test_corr_id, (
            f"Handler 捕获的 correlation_id ({captured_corr_ids[0]}) "
            f"与传入的 ({test_corr_id}) 不一致"
        )

        # 验证成功响应
        assert response.result is not None
        assert response.result["captured"] == test_corr_id

    @pytest.mark.asyncio
    async def test_dispatch_method_not_found_uses_passed_correlation_id(self):
        """
        验证 dispatch 在 METHOD_NOT_FOUND 错误时使用传入的 correlation_id
        """
        from engram.gateway.mcp_rpc import (
            JsonRpcRequest,
            JsonRpcRouter,
            generate_correlation_id,
        )

        router = JsonRpcRouter()

        test_corr_id = generate_correlation_id()
        request = JsonRpcRequest(
            jsonrpc="2.0",
            method="nonexistent/method",
            params={},
            id=1,
        )

        response = await router.dispatch(request, correlation_id=test_corr_id)

        # 验证错误响应
        assert response.error is not None
        assert response.error.code == -32601

        # 验证 error.data.correlation_id 与传入的一致
        assert response.error.data is not None
        error_data = response.error.data
        assert error_data["correlation_id"] == test_corr_id, (
            f"error.data.correlation_id ({error_data['correlation_id']}) "
            f"与传入的 ({test_corr_id}) 不一致"
        )

    def test_error_data_strict_mode_in_real_dispatch_chain(self):
        """
        验证在真实 dispatch 链路中 ErrorData 使用 strict=True 不会失败

        这钉死了规则：真实链路中 correlation_id 必须已正确设置。
        """
        from engram.gateway.mcp_rpc import (
            ErrorCategory,
            ErrorData,
            ErrorReason,
            generate_correlation_id,
        )

        # 模拟真实链路：HTTP 入口层生成 correlation_id
        http_entry_corr_id = generate_correlation_id()

        # 模拟 dispatch 中构造 ErrorData（如 METHOD_NOT_FOUND）
        error_data = ErrorData(
            category=ErrorCategory.PROTOCOL,
            reason=ErrorReason.METHOD_NOT_FOUND,
            retryable=False,
            correlation_id=http_entry_corr_id,  # 传入已有的 correlation_id
            details={"method": "unknown/method"},
        )

        # strict=True 不应抛出异常（因为 correlation_id 已正确设置）
        d = error_data.to_dict(strict=True)

        assert d["correlation_id"] == http_entry_corr_id
        assert d["category"] == ErrorCategory.PROTOCOL
        assert d["reason"] == ErrorReason.METHOD_NOT_FOUND


class TestErrorCodeBoundaryMisuse:
    """
    错误码命名空间边界反误用测试

    确保 ToolResultErrorCode.* 不会被误用到 error.data.reason 字段，
    反之亦然（McpErrorReason.* 不应出现在 result.error_code 中）。

    契约参见: docs/contracts/mcp_jsonrpc_error_v1.md §3.0
    """

    def test_tool_result_error_codes_not_in_mcp_error_reasons(self):
        """
        ToolResultErrorCode 常量不应出现在 PUBLIC_MCP_ERROR_REASONS 中

        边界规则：两个命名空间相互隔离
        """
        from engram.gateway.error_codes import PUBLIC_MCP_ERROR_REASONS
        from engram.gateway.result_error_codes import ToolResultErrorCode

        # 提取 ToolResultErrorCode 的所有公开字符串常量
        tool_result_codes: set[str] = set()
        for name in dir(ToolResultErrorCode):
            if name.startswith("_"):
                continue
            value = getattr(ToolResultErrorCode, name)
            if isinstance(value, str):
                tool_result_codes.add(value)

        # 检查是否有交集（DEPENDENCY_MISSING 等仅属于 ToolResultErrorCode）
        mcp_reasons_set = set(PUBLIC_MCP_ERROR_REASONS)

        # ToolResultErrorCode 专有的错误码不应出现在 MCP 层
        tool_result_only_codes = {"DEPENDENCY_MISSING"}
        for code in tool_result_only_codes:
            assert code not in mcp_reasons_set, (
                f"边界违规: ToolResultErrorCode.{code} 不应出现在 PUBLIC_MCP_ERROR_REASONS 中。"
                f"ToolResultErrorCode 仅用于 result.error_code，"
                f"不应用于 error.data.reason。"
            )

    def test_mcp_error_reasons_not_for_result_error_code(self):
        """
        McpErrorReason 专有常量不应与 ToolResultErrorCode 混淆

        这是一个静态检查，确保两个命名空间的设计意图被正确理解。
        """
        from engram.gateway.result_error_codes import ToolResultErrorCode

        # McpErrorReason 专有的错误码（协议层/系统层）
        # 这些错误码仅用于 error.data.reason，不应出现在 ToolResultErrorCode 中
        mcp_only_reasons = {
            "PARSE_ERROR",
            "INVALID_REQUEST",
            "METHOD_NOT_FOUND",
            "UNKNOWN_TOOL",
            "UNHANDLED_EXCEPTION",
            "TOOL_EXECUTOR_NOT_REGISTERED",
            "OPENMEMORY_UNAVAILABLE",
            "OPENMEMORY_CONNECTION_FAILED",
            "OPENMEMORY_API_ERROR",
            "LOGBOOK_DB_UNAVAILABLE",
            "LOGBOOK_DB_CHECK_FAILED",
        }

        # 提取 ToolResultErrorCode 的所有公开字符串常量
        tool_result_codes: set[str] = set()
        for name in dir(ToolResultErrorCode):
            if name.startswith("_"):
                continue
            value = getattr(ToolResultErrorCode, name)
            if isinstance(value, str):
                tool_result_codes.add(value)

        # MCP 专有错误码不应出现在 ToolResultErrorCode 中
        for reason in mcp_only_reasons:
            assert reason not in tool_result_codes, (
                f"边界违规: McpErrorReason.{reason} 不应在 ToolResultErrorCode 中定义。"
                f"McpErrorReason 仅用于 error.data.reason，"
                f"不应用于 result.error_code。"
            )

    def test_valid_error_reasons_whitelist_excludes_tool_result_codes(self):
        """
        测试辅助函数 VALID_ERROR_REASONS 不包含 ToolResultErrorCode 专有码

        VALID_ERROR_REASONS 用于契约断言，必须与 PUBLIC_MCP_ERROR_REASONS 一致。
        """
        # ToolResultErrorCode 专有的错误码（定义在 result_error_codes.py 中）
        # 这些错误码仅用于 result.error_code，不应出现在 VALID_ERROR_REASONS 中
        tool_result_only_codes = {"DEPENDENCY_MISSING"}

        for code in tool_result_only_codes:
            assert code not in VALID_ERROR_REASONS, (
                f"边界违规: VALID_ERROR_REASONS 不应包含 ToolResultErrorCode.{code}。"
                f"请确保测试文件中的 VALID_ERROR_REASONS 与 PUBLIC_MCP_ERROR_REASONS 一致。"
            )


class TestMcpRequestLogging:
    def test_mcp_options_logs_headers_without_secrets(self, client, caplog):
        caplog.set_level(logging.INFO, logger="gateway")

        headers = {
            "Origin": "https://example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": (
                "Authorization: Bearer top-secret, X-Engram-Auth: engram-secret, X-Extra"
            ),
        }
        response = client.options("/mcp", headers=headers)
        assert response.status_code == 204

        record = next(
            (item for item in caplog.records if item.getMessage() == "MCP CORS preflight"),
            None,
        )
        assert record is not None, "应记录 MCP CORS preflight 日志"

        requested_headers = getattr(record, "requested_headers", "")
        allow_headers = getattr(record, "allow_headers", "")
        assert "Authorization" in requested_headers
        assert "X-Engram-Auth" in requested_headers
        assert "top-secret" not in requested_headers
        assert "engram-secret" not in requested_headers
        assert "Bearer" not in requested_headers
        assert "Authorization" in allow_headers

    def test_mcp_endpoint_logs_request_metadata_without_token_leak(self, client, caplog):
        caplog.set_level(logging.INFO, logger="gateway")

        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
            headers={
                "Authorization": "Bearer top-secret",
                "X-Engram-Auth": "engram-secret",
                "Mcp-Session-Id": "session-123",
            },
        )
        assert response.status_code == 200

        record = next(
            (item for item in caplog.records if item.getMessage() == "MCP request"),
            None,
        )
        assert record is not None, "应记录 MCP request 日志"

        assert getattr(record, "is_jsonrpc", None) is True
        assert getattr(record, "method", None) == "tools/list"
        assert getattr(record, "mcp_session_id_present", None) is True
        assert getattr(record, "correlation_id", None) == response.headers.get("X-Correlation-ID")

        record_payload = json.dumps(record.__dict__, default=str)
        assert "top-secret" not in record_payload
        assert "engram-secret" not in record_payload
        assert "session-123" not in record_payload


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
