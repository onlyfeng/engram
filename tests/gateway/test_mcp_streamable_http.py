"""
MCP Streamable HTTP 传输测试

覆盖：
1. initialize 返回 Mcp-Session-Id header
2. notifications/initialized → 202 空 body
3. 有效/无效 session 请求的正确响应码
4. DELETE /mcp 会话终止
5. GET /mcp → 405
6. 批量请求处理
7. 向后兼容：无 session ID 的请求仍正常工作
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="function")
def mock_dependencies():
    """设置测试依赖"""
    from engram.gateway.container import GatewayContainer, set_container
    from tests.gateway.fakes import (
        FakeGatewayConfig,
        FakeLogbookAdapter,
        FakeLogbookDatabase,
    )

    fake_config = FakeGatewayConfig(
        project_key="test_project",
        default_team_space="team:test_project",
    )
    fake_db = FakeLogbookDatabase()
    fake_db.configure_settings(team_write_enabled=False, policy_json={})
    fake_adapter = FakeLogbookAdapter()
    fake_adapter.configure_dedup_miss()
    mock_client = MagicMock()
    mock_client.store.return_value = MagicMock(success=True, memory_id="mock-id", error=None)
    mock_client.search.return_value = MagicMock(success=True, results=[], error=None)
    test_container = GatewayContainer.create_for_testing(
        config=fake_config,
        db=fake_db,
        logbook_adapter=fake_adapter,
        openmemory_client=mock_client,
    )
    set_container(test_container)
    yield


@pytest.fixture(scope="function")
def client(mock_dependencies):
    from engram.gateway.app import create_app

    return TestClient(create_app())


def _initialize_request() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"},
        },
    }


def _do_initialize(client) -> str:
    """Helper: perform initialize and return session_id"""
    resp = client.post("/mcp", json=_initialize_request())
    assert resp.status_code == 200
    session_id = resp.headers.get("Mcp-Session-Id")
    assert session_id
    return session_id


class TestInitializeSession:
    def test_initialize_returns_session_id_header(self, client):
        resp = client.post("/mcp", json=_initialize_request())
        assert resp.status_code == 200
        session_id = resp.headers.get("Mcp-Session-Id")
        assert session_id is not None
        assert len(session_id) == 32  # uuid4().hex

    def test_initialize_response_body(self, client):
        resp = client.post("/mcp", json=_initialize_request())
        result = resp.json()
        assert result["result"]["protocolVersion"] == "2025-03-26"
        assert "capabilities" in result["result"]
        assert "serverInfo" in result["result"]


class TestNotifications:
    def test_initialized_notification_returns_202(self, client):
        session_id = _do_initialize(client)
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={"Mcp-Session-Id": session_id},
        )
        assert resp.status_code == 202
        assert resp.content == b""

    def test_unknown_notification_returns_202(self, client):
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/unknown"},
        )
        assert resp.status_code == 202

    def test_notification_has_no_id(self, client):
        """Notification = JSON-RPC request without 'id' field"""
        # With id → treated as normal request, not notification
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "notifications/initialized"},
        )
        # With id it goes through dispatch → method not found
        assert resp.status_code != 202


class TestSessionValidation:
    def test_valid_session_passes(self, client):
        session_id = _do_initialize(client)
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "ping"},
            headers={"Mcp-Session-Id": session_id},
        )
        assert resp.status_code == 200

    def test_invalid_session_returns_404(self, client):
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "ping"},
            headers={"Mcp-Session-Id": "nonexistent_session_id_12345678"},
        )
        assert resp.status_code == 404

    def test_no_session_id_still_works(self, client):
        """Backward compatibility: requests without session ID work"""
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
        assert resp.status_code == 200
        result = resp.json()
        assert result.get("result") == {}


class TestDeleteMcp:
    def test_delete_valid_session(self, client):
        session_id = _do_initialize(client)
        resp = client.delete("/mcp", headers={"Mcp-Session-Id": session_id})
        assert resp.status_code == 204

        # Session should no longer exist
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "ping"},
            headers={"Mcp-Session-Id": session_id},
        )
        assert resp.status_code == 404

    def test_delete_invalid_session(self, client):
        resp = client.delete("/mcp", headers={"Mcp-Session-Id": "nonexistent"})
        assert resp.status_code == 404

    def test_delete_missing_session_header(self, client):
        resp = client.delete("/mcp")
        assert resp.status_code == 400


class TestGetMcp:
    def test_get_returns_405(self, client):
        resp = client.get("/mcp")
        assert resp.status_code == 405


class TestKnownProbeAccessLogFilter:
    def test_known_probe_classifier_matches_expected_paths(self):
        from engram.gateway.routes import _is_known_mcp_probe_access

        assert _is_known_mcp_probe_access("GET", "/mcp", 405) is True
        assert _is_known_mcp_probe_access("GET", "/mcp?transport=sse", 405) is True
        assert (
            _is_known_mcp_probe_access("GET", "/.well-known/oauth-protected-resource", 404) is True
        )
        assert (
            _is_known_mcp_probe_access("GET", "/.well-known/oauth-protected-resource/mcp", 404)
            is True
        )

        assert _is_known_mcp_probe_access("POST", "/mcp", 200) is False
        assert _is_known_mcp_probe_access("GET", "/mcp", 200) is False
        assert _is_known_mcp_probe_access("GET", "/health", 404) is False

    def test_known_probe_filter_suppresses_only_expected_access_logs(self):
        from engram.gateway.routes import _KnownMcpProbeAccessFilter

        access_filter = _KnownMcpProbeAccessFilter()

        known_probe = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=0,
            msg='%s - "%s %s HTTP/%s" %s',
            args=("127.0.0.1:51215", "GET", "/mcp", "1.1", 405),
            exc_info=None,
        )
        normal_request = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=0,
            msg='%s - "%s %s HTTP/%s" %s',
            args=("127.0.0.1:51215", "POST", "/mcp", "1.1", 200),
            exc_info=None,
        )

        assert access_filter.filter(known_probe) is False
        assert access_filter.filter(normal_request) is True

    def test_create_app_installs_uvicorn_access_filter_once(self):
        from engram.gateway.app import create_app

        access_logger = logging.getLogger("uvicorn.access")
        original_filters = list(access_logger.filters)
        access_logger.filters.clear()
        try:
            create_app()
            first_count = sum(
                flt.__class__.__name__ == "_KnownMcpProbeAccessFilter"
                for flt in access_logger.filters
            )
            create_app()
            second_count = sum(
                flt.__class__.__name__ == "_KnownMcpProbeAccessFilter"
                for flt in access_logger.filters
            )
            assert first_count == 1
            assert second_count == 1
        finally:
            access_logger.filters[:] = original_filters


class TestBatchRequests:
    def test_batch_requests(self, client):
        batch = [
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
        resp = client.post("/mcp", json=batch)
        assert resp.status_code == 200
        results = resp.json()
        assert isinstance(results, list)
        assert len(results) == 2

    def test_batch_with_notification(self, client):
        """Notifications in batch don't produce response items"""
        batch = [
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        ]
        resp = client.post("/mcp", json=batch)
        assert resp.status_code == 200
        results = resp.json()
        assert isinstance(results, list)
        assert len(results) == 1  # Only the ping response

    def test_batch_all_notifications(self, client):
        """Batch of only notifications → 202"""
        batch = [
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "method": "notifications/cancelled"},
        ]
        resp = client.post("/mcp", json=batch)
        assert resp.status_code == 202


class TestCorsHeaders:
    def test_options_includes_new_methods(self, client):
        resp = client.options("/mcp")
        allow_methods = resp.headers.get("Access-Control-Allow-Methods", "")
        assert "GET" in allow_methods
        assert "DELETE" in allow_methods
        assert "POST" in allow_methods

    def test_delete_has_cors_headers(self, client):
        session_id = _do_initialize(client)
        resp = client.delete("/mcp", headers={"Mcp-Session-Id": session_id})
        assert "Access-Control-Allow-Origin" in resp.headers
