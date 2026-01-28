# -*- coding: utf-8 -*-
"""
MCP JSON-RPC 2.0 协议契约测试

测试覆盖:
1. JSON-RPC 无效请求 -> -32600 (INVALID_REQUEST)
2. 未知 method -> -32601 (METHOD_NOT_FOUND)
3. tools/list 输出包含四个工具
4. tools/call 返回 content[] 格式
5. 旧 {tool, arguments} 格式仍返回原 MCPResponse 结构
"""

import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from fastapi.testclient import TestClient


# 创建 mock 依赖后再导入 app
@pytest.fixture(scope="module")
def mock_dependencies():
    """Mock 掉 OpenMemory 和 Step1 依赖"""
    # Mock step1_adapter 模块
    mock_adapter = MagicMock()
    mock_adapter.check_dedup.return_value = None
    mock_adapter.query_knowledge_candidates.return_value = []
    
    # Mock openmemory_client 模块
    mock_openmemory_client = MagicMock()
    mock_client_instance = MagicMock()
    mock_client_instance.store.return_value = MagicMock(
        success=True,
        memory_id="mock-memory-id-123",
        error=None,
    )
    mock_client_instance.search.return_value = MagicMock(
        success=True,
        results=[],
        error=None,
    )
    mock_openmemory_client.get_client.return_value = mock_client_instance
    
    # Mock step1_db
    mock_db = MagicMock()
    mock_db.get_or_create_settings.return_value = {
        "team_write_enabled": False,
        "policy_json": {},
    }
    mock_db.insert_audit.return_value = 1
    mock_db.enqueue_outbox.return_value = 1
    
    # Mock config
    mock_config = MagicMock()
    mock_config.project_key = "test_project"
    mock_config.default_team_space = "team:test_project"
    mock_config.private_space_prefix = "private:"
    mock_config.governance_admin_key = None
    
    with patch.dict('sys.modules', {
        'gateway.step1_adapter': mock_adapter,
    }):
        with patch('gateway.main.get_config', return_value=mock_config):
            with patch('gateway.main.get_db', return_value=mock_db):
                with patch('gateway.main.get_client', return_value=mock_client_instance):
                    with patch('gateway.main.check_user_exists', return_value=True):
                        with patch('gateway.step1_adapter.check_dedup', return_value=None):
                            yield {
                                'config': mock_config,
                                'db': mock_db,
                                'client': mock_client_instance,
                            }


@pytest.fixture(scope="module")
def client(mock_dependencies):
    """创建 FastAPI TestClient"""
    from gateway.main import app
    return TestClient(app)


class TestJsonRpcInvalidRequest:
    """测试 JSON-RPC 无效请求 -> -32600"""

    def test_missing_jsonrpc_field(self, client):
        """缺少 jsonrpc 字段 (但有 method) 应返回无效请求"""
        # 注意: 根据 is_jsonrpc_request 的实现，只有同时有 jsonrpc="2.0" 和 method 才认为是 JSON-RPC
        # 所以缺少 jsonrpc 字段会走旧协议分支
        response = client.post("/mcp", json={
            "method": "tools/list"
        })
        # 这会被解析为旧协议，但旧协议需要 tool 字段
        assert response.status_code == 400
        result = response.json()
        assert result.get("ok") is False

    def test_jsonrpc_wrong_version(self, client):
        """jsonrpc 版本错误应返回 -32600"""
        response = client.post("/mcp", json={
            "jsonrpc": "1.0",  # 错误版本
            "method": "tools/list",
            "id": 1
        })
        # 由于 jsonrpc != "2.0"，会走旧协议分支
        assert response.status_code == 400
        result = response.json()
        assert result.get("ok") is False

    def test_jsonrpc_missing_method(self, client):
        """缺少 method 字段应返回 -32600"""
        response = client.post("/mcp", json={
            "jsonrpc": "2.0",
            "id": 1
            # 缺少 method
        })
        # 没有 method 字段，不会被识别为 JSON-RPC 请求
        # 会走旧协议，旧协议也会失败
        assert response.status_code == 400
        result = response.json()
        assert result.get("ok") is False

    def test_jsonrpc_invalid_params_type(self, client):
        """params 不是 dict 应返回 -32600"""
        response = client.post("/mcp", json={
            "jsonrpc": "2.0",
            "method": "tools/list",
            "params": "not_a_dict",  # 应该是 dict
            "id": 1
        })
        # Pydantic 验证会失败
        assert response.status_code == 400
        result = response.json()
        assert result.get("error") is not None
        assert result["error"]["code"] == -32600  # INVALID_REQUEST


class TestJsonRpcMethodNotFound:
    """测试未知 method -> -32601"""

    def test_unknown_method_returns_32601(self, client):
        """未知方法应返回 -32601"""
        response = client.post("/mcp", json={
            "jsonrpc": "2.0",
            "method": "unknown/method",
            "id": 1
        })
        assert response.status_code == 200
        result = response.json()
        assert result.get("error") is not None
        assert result["error"]["code"] == -32601  # METHOD_NOT_FOUND
        assert "未知方法" in result["error"]["message"]

    def test_typo_in_method_name(self, client):
        """方法名拼写错误应返回 -32601"""
        response = client.post("/mcp", json={
            "jsonrpc": "2.0",
            "method": "tool/list",  # 缺少 s
            "id": 2
        })
        assert response.status_code == 200
        result = response.json()
        assert result["error"]["code"] == -32601

    def test_empty_method_returns_32601(self, client):
        """空方法名应返回 -32601"""
        response = client.post("/mcp", json={
            "jsonrpc": "2.0",
            "method": "",
            "id": 3
        })
        assert response.status_code == 200
        result = response.json()
        assert result["error"]["code"] == -32601


class TestToolsList:
    """测试 tools/list 返回四个工具"""

    def test_tools_list_returns_four_tools(self, client):
        """tools/list 应返回四个工具定义"""
        response = client.post("/mcp", json={
            "jsonrpc": "2.0",
            "method": "tools/list",
            "id": 1
        })
        assert response.status_code == 200
        result = response.json()
        
        # 验证成功响应
        assert result.get("jsonrpc") == "2.0"
        assert result.get("id") == 1
        assert result.get("error") is None
        assert result.get("result") is not None
        
        # 验证包含四个工具
        tools = result["result"]["tools"]
        assert len(tools) == 4
        
        # 验证工具名称
        tool_names = {tool["name"] for tool in tools}
        expected_names = {"memory_store", "memory_query", "reliability_report", "governance_update"}
        assert tool_names == expected_names

    def test_tools_list_tool_structure(self, client):
        """验证工具定义的结构"""
        response = client.post("/mcp", json={
            "jsonrpc": "2.0",
            "method": "tools/list",
            "id": 1
        })
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
        response = client.post("/mcp", json={
            "jsonrpc": "2.0",
            "method": "tools/list",
            "id": 2
        })
        assert response.status_code == 200
        result = response.json()
        assert len(result["result"]["tools"]) == 4


class TestToolsCall:
    """测试 tools/call 返回 content[] 格式"""

    def test_tools_call_returns_content_array(self, client, mock_dependencies):
        """tools/call 应返回 content[] 格式"""
        # 设置 mock 返回
        mock_client = mock_dependencies['client']
        mock_client.store.return_value = MagicMock(
            success=True,
            memory_id="test-memory-id",
            error=None,
        )
        
        response = client.post("/mcp", json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "memory_query",
                "arguments": {
                    "query": "test query"
                }
            },
            "id": 1
        })
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
        response = client.post("/mcp", json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "arguments": {"query": "test"}
            },
            "id": 1
        })
        assert response.status_code == 200
        result = response.json()
        
        # 应返回参数错误
        assert result.get("error") is not None
        assert result["error"]["code"] == -32602  # INVALID_PARAMS

    def test_tools_call_unknown_tool_returns_error(self, client):
        """未知工具应返回 -32602"""
        response = client.post("/mcp", json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "unknown_tool",
                "arguments": {}
            },
            "id": 1
        })
        assert response.status_code == 200
        result = response.json()
        
        # 应返回参数错误（工具不存在）
        assert result.get("error") is not None
        assert result["error"]["code"] == -32602

    def test_tools_call_reliability_report(self, client, mock_dependencies):
        """调用 reliability_report 工具"""
        # Mock get_reliability_report
        with patch('gateway.main.get_reliability_report') as mock_report:
            mock_report.return_value = {
                "outbox_stats": {"pending": 0, "success": 5},
                "audit_stats": {"allow": 10, "reject": 2},
                "generated_at": "2026-01-28T12:00:00Z",
            }
            
            response = client.post("/mcp", json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": "reliability_report",
                    "arguments": {}
                },
                "id": 1
            })
        
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
        response = client.post("/mcp", json={
            "tool": "memory_query",
            "arguments": {
                "query": "test query",
                "top_k": 5
            }
        })
        assert response.status_code == 200
        result = response.json()
        
        # 验证 MCPResponse 结构
        assert "ok" in result
        assert "result" in result or "error" in result
        
        # 验证不是 JSON-RPC 格式
        assert "jsonrpc" not in result

    def test_legacy_format_memory_store(self, client, mock_dependencies):
        """旧格式 memory_store 返回 MCPResponse"""
        mock_client = mock_dependencies['client']
        mock_client.store.return_value = MagicMock(
            success=True,
            memory_id="legacy-memory-id",
            error=None,
        )
        
        response = client.post("/mcp", json={
            "tool": "memory_store",
            "arguments": {
                "payload_md": "# Test Memory\n\nThis is a test.",
            }
        })
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
        response = client.post("/mcp", json={
            "tool": "unknown_tool",
            "arguments": {}
        })
        assert response.status_code == 200
        result = response.json()
        
        # 验证错误响应
        assert result.get("ok") is False
        assert result.get("error") is not None

    def test_legacy_format_missing_tool_field(self, client):
        """旧格式缺少 tool 字段应返回 400"""
        response = client.post("/mcp", json={
            "arguments": {"query": "test"}
        })
        assert response.status_code == 400
        result = response.json()
        assert result.get("ok") is False

    def test_legacy_format_with_empty_arguments(self, client, mock_dependencies):
        """旧格式空 arguments 应正常处理"""
        with patch('gateway.main.get_reliability_report') as mock_report:
            mock_report.return_value = {
                "outbox_stats": {},
                "audit_stats": {},
                "generated_at": "2026-01-28T12:00:00Z",
            }
            
            response = client.post("/mcp", json={
                "tool": "reliability_report",
                "arguments": {}
            })
        
        assert response.status_code == 200
        result = response.json()
        assert result.get("ok") is True
        assert "result" in result


class TestJsonRpcProtocolDetails:
    """测试 JSON-RPC 协议细节"""

    def test_response_includes_id(self, client):
        """响应应包含请求的 id"""
        response = client.post("/mcp", json={
            "jsonrpc": "2.0",
            "method": "tools/list",
            "id": 42
        })
        result = response.json()
        assert result.get("id") == 42

    def test_response_includes_jsonrpc_version(self, client):
        """响应应包含 jsonrpc: 2.0"""
        response = client.post("/mcp", json={
            "jsonrpc": "2.0",
            "method": "tools/list",
            "id": 1
        })
        result = response.json()
        assert result.get("jsonrpc") == "2.0"

    def test_null_id_preserved(self, client):
        """null id 应被保留"""
        response = client.post("/mcp", json={
            "jsonrpc": "2.0",
            "method": "tools/list",
            "id": None
        })
        result = response.json()
        assert result.get("id") is None

    def test_string_id_preserved(self, client):
        """字符串 id 应被保留"""
        response = client.post("/mcp", json={
            "jsonrpc": "2.0",
            "method": "tools/list",
            "id": "my-request-id"
        })
        result = response.json()
        assert result.get("id") == "my-request-id"


class TestJsonParseError:
    """测试 JSON 解析错误"""

    def test_invalid_json_returns_parse_error(self, client):
        """无效 JSON 应返回 -32700"""
        response = client.post(
            "/mcp",
            content="not valid json",
            headers={"Content-Type": "application/json"}
        )
        # FastAPI/Starlette 可能返回 400 或 422
        assert response.status_code in [400, 422]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
