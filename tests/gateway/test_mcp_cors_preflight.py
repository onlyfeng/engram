from typing import Optional

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from engram.gateway.api_models import MCP_CORS_HEADERS, build_mcp_allow_headers
from engram.gateway.correlation_id import is_valid_correlation_id
from engram.gateway.middleware import GatewayAuthMiddleware, install_middleware


@pytest.fixture(autouse=True)
def _clear_gateway_auth_env(monkeypatch) -> None:
    monkeypatch.delenv("GATEWAY_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("GATEWAY_AUTH_TOKENS_JSON", raising=False)


def _make_app(path: str, status_code: int) -> FastAPI:
    app = FastAPI()
    app.add_middleware(GatewayAuthMiddleware)

    async def handler() -> JSONResponse:
        return JSONResponse(content={"detail": "auth failed"}, status_code=status_code)

    app.add_api_route(path, handler, methods=["POST"])
    return app


def _assert_mcp_cors_headers(response, requested_headers: Optional[str]) -> None:
    expected_allow_headers = build_mcp_allow_headers(requested_headers)
    for key, value in MCP_CORS_HEADERS.items():
        if key == "Access-Control-Allow-Headers":
            assert response.headers.get(key) == expected_allow_headers
        else:
            assert response.headers.get(key) == value


@pytest.mark.parametrize("path", ["/mcp", "/mcp/sessions"])
@pytest.mark.parametrize("status_code", [401, 403])
def test_mcp_auth_reject_injects_cors_headers(path: str, status_code: int) -> None:
    app = _make_app(path, status_code)
    requested_headers = "Authorization, X-Extra-Header"

    with TestClient(app) as client:
        response = client.post(
            path,
            headers={
                "Access-Control-Request-Headers": requested_headers,
            },
        )

    assert response.status_code == status_code
    assert response.json() == {"detail": "auth failed"}

    _assert_mcp_cors_headers(response, requested_headers)


def test_non_mcp_auth_reject_does_not_inject_cors_headers() -> None:
    app = _make_app("/health", 401)

    with TestClient(app) as client:
        response = client.post(
            "/health",
            headers={
                "Access-Control-Request-Headers": "Authorization",
            },
        )

    assert response.status_code == 401
    for key in MCP_CORS_HEADERS:
        assert key not in response.headers


def test_mcp_options_returns_204_with_cors_headers(monkeypatch) -> None:
    monkeypatch.setenv("GATEWAY_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("GATEWAY_AUTH_TOKENS_JSON", '["secondary-token"]')

    from engram.gateway.app import create_app

    app = create_app()

    with TestClient(app) as client:
        response = client.options(
            "/mcp",
            headers={
                "Origin": "http://example.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Authorization, Mcp-Session-Id",
            },
        )

    assert response.status_code == 204
    _assert_mcp_cors_headers(response, "Authorization, Mcp-Session-Id")


@pytest.mark.parametrize("status_code", [401, 403])
def test_mcp_auth_reject_includes_correlation_id_when_installed(status_code: int) -> None:
    app = FastAPI()
    app.add_middleware(GatewayAuthMiddleware)
    install_middleware(app)

    async def handler() -> JSONResponse:
        return JSONResponse(content={"detail": "auth failed"}, status_code=status_code)

    app.add_api_route("/mcp", handler, methods=["POST"])
    requested_headers = "Authorization, X-Extra-Header"

    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers={
                "Access-Control-Request-Headers": requested_headers,
            },
        )

    assert response.status_code == status_code
    assert response.json() == {"detail": "auth failed"}
    _assert_mcp_cors_headers(response, requested_headers)

    correlation_id = response.headers.get("X-Correlation-ID")
    assert correlation_id is not None
    assert is_valid_correlation_id(correlation_id)


def test_mcp_gateway_auth_unauthorized_includes_cors_and_correlation(monkeypatch) -> None:
    monkeypatch.setenv("GATEWAY_AUTH_TOKEN", "test-token")

    app = FastAPI()
    app.add_middleware(GatewayAuthMiddleware)
    install_middleware(app)

    async def handler() -> JSONResponse:
        return JSONResponse(content={"detail": "ok"}, status_code=200)

    app.add_api_route("/mcp", handler, methods=["POST"])
    requested_headers = "Authorization"

    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers={
                "Access-Control-Request-Headers": requested_headers,
            },
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}
    _assert_mcp_cors_headers(response, requested_headers)

    correlation_id = response.headers.get("X-Correlation-ID")
    assert correlation_id is not None
    assert is_valid_correlation_id(correlation_id)


def test_mcp_options_mixed_case_headers_allowed(monkeypatch) -> None:
    monkeypatch.setenv("GATEWAY_AUTH_TOKEN", "test-token")

    from engram.gateway.app import create_app

    app = create_app()
    requested_headers = "authorization, X-Extra-Header, x-extra-header, MCP-SESSION-ID"

    with TestClient(app) as client:
        response = client.options(
            "/mcp",
            headers={
                "Origin": "http://example.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": requested_headers,
            },
        )

    assert response.status_code == 204
    allow_headers = response.headers.get("Access-Control-Allow-Headers", "")
    allow_set = {item.strip().lower() for item in allow_headers.split(",") if item.strip()}
    requested_set = {item.strip().lower() for item in requested_headers.split(",") if item.strip()}
    assert requested_set.issubset(allow_set)
