# -*- coding: utf-8 -*-
"""
Gateway 可观测性基础测试。
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_metrics_endpoint_exposes_prometheus_text(gateway_test_container, monkeypatch) -> None:
    """/metrics 应返回 Prometheus 文本并包含核心指标名。"""
    _ = gateway_test_container
    monkeypatch.setenv("GATEWAY_METRICS_ENABLED", "1")

    from engram.gateway.app import create_app

    app = create_app()
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200

        # 触发一次 tools/call 路径，生成 MCP 工具指标
        rpc_body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "memory_query", "arguments": {"query": "hello"}},
        }
        rpc_resp = client.post("/mcp", json=rpc_body)
        assert rpc_resp.status_code == 200

        metrics = client.get("/metrics")
        assert metrics.status_code == 200
        assert "gateway_http_requests_total" in metrics.text
        assert "gateway_mcp_tool_calls_total" in metrics.text
        assert "gateway_openmemory_calls_total" in metrics.text


def test_metrics_endpoint_can_be_disabled(gateway_test_container, monkeypatch) -> None:
    """/metrics 在禁用时应返回提示文本。"""
    _ = gateway_test_container
    monkeypatch.setenv("GATEWAY_METRICS_ENABLED", "0")

    from engram.gateway.app import create_app

    app = create_app()
    with TestClient(app) as client:
        metrics = client.get("/metrics")
        assert metrics.status_code == 200
        assert "metrics disabled" in metrics.text


def test_start_span_falls_back_when_otel_missing(monkeypatch) -> None:
    """启用 OTel 开关但缺依赖时，start_span 应降级且不抛错。"""
    monkeypatch.setenv("GATEWAY_OTEL_ENABLED", "1")

    from engram.gateway import observability

    # 重置初始化状态，确保读取新的环境变量
    observability._trace_backend = None  # type: ignore[attr-defined]

    with observability.start_span(
        "test.trace.fallback",
        correlation_id="corr-0123456789abcdef",
        attributes={"test.key": "value"},
    ):
        pass
