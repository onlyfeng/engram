#!/usr/bin/env python3
"""
mcp_doctor.py 单元测试

覆盖场景：
1. OPTIONS /mcp CORS 头完整
2. 缺少 Access-Control-Expose-Headers 仍通过
3. 自定义 header 仍通过
4. 缺少已请求 header 时失败
5. expose headers 缺失/存在判定
6. 无配置时安全退出
7. 配置缺失字段报错
8. 不可连接时失败
9. 可连接时成功（包含 initialize/ping/tools/list/unknown method）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# 将 scripts/ops 目录添加到 path
sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))

import mcp_doctor


def _default_request_headers() -> dict[str, str]:
    return mcp_doctor._prepare_request_headers({})


def _valid_tools_list_payload() -> dict:
    tools = [
        {
            "name": "memory_store",
            "description": "d",
            "inputSchema": {
                "type": "object",
                "required": ["payload_md"],
                "properties": {"payload_md": {"type": "string"}},
            },
        },
        {
            "name": "memory_query",
            "description": "d",
            "inputSchema": {
                "type": "object",
                "required": ["query"],
                "properties": {"query": {"type": "string"}},
            },
        },
        {
            "name": "reliability_report",
            "description": "d",
            "inputSchema": {"type": "object", "required": [], "properties": {}},
        },
        {
            "name": "governance_update",
            "description": "d",
            "inputSchema": {"type": "object", "required": [], "properties": {}},
        },
        {
            "name": "evidence_upload",
            "description": "d",
            "inputSchema": {
                "type": "object",
                "required": ["content", "content_type"],
                "properties": {
                    "content": {"type": "string"},
                    "content_type": {"type": "string"},
                    "title": {"type": "string"},
                    "actor_user_id": {"type": "string"},
                    "project_key": {"type": "string"},
                    "item_id": {"type": "string"},
                },
            },
        },
    ]
    return {"jsonrpc": "2.0", "id": 1, "result": {"tools": tools}}


def _patch_request(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: int,
    response_headers: dict[str, str],
    err: str | None = None,
) -> None:
    def _request(
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = mcp_doctor.DEFAULT_TIMEOUT_SECONDS,
    ) -> tuple[int | None, bytes | None, dict[str, str], str | None]:
        return status, None, response_headers, err

    monkeypatch.setattr(mcp_doctor, "_request", _request)


def _run_main(monkeypatch: pytest.MonkeyPatch, args: list[str] | None = None) -> int:
    argv = ["mcp_doctor.py"] + (args or [])
    monkeypatch.setattr(sys, "argv", argv)
    return mcp_doctor.main()


def _clear_gateway_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GATEWAY_URL", raising=False)
    monkeypatch.delenv("GATEWAY_HOST", raising=False)
    monkeypatch.delenv("GATEWAY_PORT", raising=False)
    monkeypatch.delenv("MCP_DOCTOR_CONFIG", raising=False)


def _write_config(tmp_path: Path, data: dict) -> Path:
    config_path = tmp_path / "mcp.json"
    config_path.write_text(json.dumps(data), encoding="utf-8")
    return config_path


def test_check_options_cors_success(monkeypatch: pytest.MonkeyPatch) -> None:
    response_headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "OPTIONS, POST",
        "Access-Control-Allow-Headers": "Content-Type, Authorization, Mcp-Session-Id",
        "Access-Control-Expose-Headers": "Mcp-Session-Id, X-Correlation-ID",
    }
    _patch_request(monkeypatch, status=204, response_headers=response_headers)

    request_headers = _default_request_headers()
    result = mcp_doctor._check_options(
        "http://example.com",
        1.0,
        request_headers=request_headers,
    )

    assert result.passed is True
    assert result.message == "OK"
    assert result.missing_expose_headers == []


def test_check_options_missing_expose_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    response_headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type, Mcp-Session-Id",
    }
    _patch_request(monkeypatch, status=200, response_headers=response_headers)

    request_headers = _default_request_headers()
    result = mcp_doctor._check_options(
        "http://example.com",
        1.0,
        request_headers=request_headers,
    )

    assert result.passed is True
    assert set(result.missing_expose_headers) == {"mcp-session-id", "x-correlation-id"}


def test_check_options_allows_custom_header(monkeypatch: pytest.MonkeyPatch) -> None:
    response_headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "OPTIONS, POST",
        "Access-Control-Allow-Headers": (
            "Content-Type, Authorization, Mcp-Session-Id, X-Custom-Header"
        ),
    }
    _patch_request(monkeypatch, status=204, response_headers=response_headers)

    request_headers = mcp_doctor._prepare_request_headers({"X-Custom-Header": "value"})
    result = mcp_doctor._check_options(
        "http://example.com",
        1.0,
        request_headers=request_headers,
    )

    assert result.passed is True


def test_check_options_missing_requested_header(monkeypatch: pytest.MonkeyPatch) -> None:
    response_headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "OPTIONS, POST",
        "Access-Control-Allow-Headers": "Content-Type, Authorization, Mcp-Session-Id",
    }
    _patch_request(monkeypatch, status=204, response_headers=response_headers)

    request_headers = mcp_doctor._prepare_request_headers({"X-Trace-Id": "trace-1"})
    result = mcp_doctor._check_options(
        "http://example.com",
        1.0,
        request_headers=request_headers,
    )

    assert result.passed is False
    assert result.details == "Access-Control-Allow-Headers"
    assert "x-trace-id" in result.missing_headers


def test_check_options_includes_custom_request_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    response_headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "OPTIONS, POST",
        "Access-Control-Allow-Headers": (
            "Content-Type, Authorization, Mcp-Session-Id, X-Trace-Id"
        ),
    }
    captured: dict[str, dict[str, str]] = {}

    def _request(
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = mcp_doctor.DEFAULT_TIMEOUT_SECONDS,
    ) -> tuple[int | None, bytes | None, dict[str, str], str | None]:
        captured["headers"] = headers or {}
        return 204, None, response_headers, None

    monkeypatch.setattr(mcp_doctor, "_request", _request)

    request_headers = mcp_doctor._prepare_request_headers({"X-Trace-Id": "trace-123"})
    result = mcp_doctor._check_options(
        "http://example.com",
        1.0,
        request_headers=request_headers,
    )

    assert result.passed is True
    assert "X-Trace-Id" in captured["headers"]["Access-Control-Request-Headers"]


def test_check_tools_list_sends_custom_header(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, dict[str, str]] = {}

    def _request(
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = mcp_doctor.DEFAULT_TIMEOUT_SECONDS,
    ) -> tuple[int | None, bytes | None, dict[str, str], str | None]:
        captured["headers"] = headers or {}
        body = json.dumps(_valid_tools_list_payload()).encode("utf-8")
        response_headers = {
            "X-Correlation-ID": "corr-0000000000000001",
            "Access-Control-Expose-Headers": "X-Correlation-ID",
        }
        return 200, body, response_headers, None

    monkeypatch.setattr(mcp_doctor, "_request", _request)

    request_headers = mcp_doctor._prepare_request_headers({"X-Project-Key": "demo"})
    preflight = mcp_doctor._build_preflight_headers(request_headers)
    result = mcp_doctor._check_tools_list(
        "http://example.com",
        1.0,
        request_headers=request_headers,
    )

    assert result.passed is True
    assert captured["headers"]["X-Project-Key"] == "demo"
    declared = mcp_doctor._split_header_values(
        preflight["Access-Control-Request-Headers"]
    )
    sent = {key.lower() for key in captured["headers"]}
    assert declared.issubset(sent)


def test_main_skips_when_no_config(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _clear_gateway_env(monkeypatch)
    monkeypatch.setattr(mcp_doctor, "_get_default_config_paths", lambda: [])

    exit_code = _run_main(monkeypatch)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "[SKIP]" in captured.out
    assert "未发现 MCP 配置文件" in captured.out


def test_main_config_missing_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _clear_gateway_env(monkeypatch)
    config_path = _write_config(tmp_path, {"mcpServers": {"engram": {"type": "http"}}})
    monkeypatch.setenv("MCP_DOCTOR_CONFIG", str(config_path))

    exit_code = _run_main(monkeypatch)
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "缺少 type 或 url" in captured.err


def test_main_unreachable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _clear_gateway_env(monkeypatch)
    config_path = _write_config(
        tmp_path,
        {"mcpServers": {"engram": {"type": "http", "url": "http://example.com/mcp"}}},
    )
    monkeypatch.setenv("MCP_DOCTOR_CONFIG", str(config_path))

    def _request(
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = mcp_doctor.DEFAULT_TIMEOUT_SECONDS,
    ) -> tuple[int | None, bytes | None, dict[str, str], str | None]:
        return None, None, {}, "Connection refused"

    monkeypatch.setattr(mcp_doctor, "_request", _request)

    exit_code = _run_main(monkeypatch)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "[FAIL]" in captured.out


def test_main_connectable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _clear_gateway_env(monkeypatch)
    config_path = _write_config(
        tmp_path,
        {"mcpServers": {"engram": {"type": "http", "url": "http://example.com/mcp"}}},
    )
    monkeypatch.setenv("MCP_DOCTOR_CONFIG", str(config_path))

    counter = {"value": 0}

    def _next_corr_id() -> str:
        counter["value"] += 1
        return f"corr-{counter['value']:016x}"

    def _request(
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = mcp_doctor.DEFAULT_TIMEOUT_SECONDS,
    ) -> tuple[int | None, bytes | None, dict[str, str], str | None]:
        if method == "GET" and url.endswith("/health"):
            return 200, b'{"status":"ok"}', {}, None
        if method == "OPTIONS":
            allow_headers = (headers or {}).get("Access-Control-Request-Headers", "")
            response_headers = {
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "OPTIONS, POST",
                "Access-Control-Allow-Headers": allow_headers,
                "Access-Control-Expose-Headers": "Mcp-Session-Id, X-Correlation-ID",
            }
            return 204, None, response_headers, None
        if method == "POST" and url.endswith("/mcp"):
            payload = json.loads((data or b"{}").decode("utf-8"))
            method_name = payload.get("method")
            corr_id = _next_corr_id()
            response_headers = {
                "X-Correlation-ID": corr_id,
                "Access-Control-Expose-Headers": "X-Correlation-ID",
            }
            if method_name == "initialize":
                body = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": payload.get("id"),
                        "result": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "engram", "version": "0.0.0"},
                        },
                    }
                ).encode("utf-8")
                return 200, body, response_headers, None
            if method_name == "ping":
                body = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": payload.get("id"),
                        "result": {},
                    }
                ).encode("utf-8")
                return 200, body, response_headers, None
            if method_name == "tools/list":
                tools_payload = _valid_tools_list_payload()
                tools_payload["id"] = payload.get("id")
                body = json.dumps(tools_payload).encode("utf-8")
                return 200, body, response_headers, None
            if method_name == "unknown/method":
                body = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": payload.get("id"),
                        "error": {
                            "code": -32601,
                            "message": "未知方法",
                            "data": {
                                "category": "protocol",
                                "reason": "METHOD_NOT_FOUND",
                                "retryable": False,
                                "correlation_id": corr_id,
                            },
                        },
                    }
                ).encode("utf-8")
                return 200, body, response_headers, None
            return 500, b"", response_headers, None
        return 500, b"", {}, None

    monkeypatch.setattr(mcp_doctor, "_request", _request)

    exit_code = _run_main(monkeypatch)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "[OK]" in captured.out


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
