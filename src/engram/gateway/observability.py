"""
Gateway 可观测性组件（指标 + 基础 tracing）。

功能:
1. Prometheus 指标输出（/metrics）
2. 请求级、MCP 工具级、OpenMemory 调用级指标
3. 基于 OpenTelemetry 的基础 span（缺依赖时自动降级为日志 span）
"""

from __future__ import annotations

import importlib
import logging
import os
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Any, Iterator, Mapping

logger = logging.getLogger("gateway.observability")

try:
    _prom_module = importlib.import_module("prometheus_client")
    CONTENT_TYPE_LATEST = _prom_module.CONTENT_TYPE_LATEST
    CollectorRegistry = _prom_module.CollectorRegistry
    Counter = _prom_module.Counter
    Histogram = _prom_module.Histogram
    generate_latest = _prom_module.generate_latest

    _PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover - 可选依赖
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"
    CollectorRegistry = None
    Counter = None
    Histogram = None
    generate_latest = None
    _PROMETHEUS_AVAILABLE = False

trace: Any = None
Status: Any = None
StatusCode: Any = None
Resource: Any = None
TracerProvider: Any = None
BatchSpanProcessor: Any = None
ConsoleSpanExporter: Any = None

try:
    trace = importlib.import_module("opentelemetry.trace")
    _otel_resources = importlib.import_module("opentelemetry.sdk.resources")
    _otel_sdk_trace = importlib.import_module("opentelemetry.sdk.trace")
    _otel_sdk_export = importlib.import_module("opentelemetry.sdk.trace.export")

    Resource = _otel_resources.Resource
    TracerProvider = _otel_sdk_trace.TracerProvider
    BatchSpanProcessor = _otel_sdk_export.BatchSpanProcessor
    ConsoleSpanExporter = _otel_sdk_export.ConsoleSpanExporter
    Status = trace.Status
    StatusCode = trace.StatusCode

    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - 可选依赖
    trace = None
    Status = None
    StatusCode = None
    Resource = None
    TracerProvider = None
    BatchSpanProcessor = None
    ConsoleSpanExporter = None
    _OTEL_AVAILABLE = False


def _is_true(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _metrics_enabled() -> bool:
    return _is_true(os.environ.get("GATEWAY_METRICS_ENABLED"), True)


def _otel_enabled() -> bool:
    return _is_true(os.environ.get("GATEWAY_OTEL_ENABLED"), False)


def _otel_service_name() -> str:
    name = os.environ.get("GATEWAY_OTEL_SERVICE_NAME", "").strip()
    return name or "engram-gateway"


def _otel_exporter() -> str:
    value = os.environ.get("GATEWAY_OTEL_EXPORTER", "console").strip().lower()
    if value not in {"console", "none"}:
        return "console"
    return value


class _FallbackMetricStore:
    """
    Prometheus 依赖缺失时的简易指标存储（文本格式兼容输出）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._http_total: dict[tuple[str, str, str], int] = defaultdict(int)
        self._http_latency_sum: dict[tuple[str, str, str], float] = defaultdict(float)
        self._http_latency_count: dict[tuple[str, str, str], int] = defaultdict(int)
        self._tool_total: dict[tuple[str, str], int] = defaultdict(int)
        self._tool_latency_sum: dict[tuple[str, str], float] = defaultdict(float)
        self._tool_latency_count: dict[tuple[str, str], int] = defaultdict(int)
        self._openmemory_total: dict[tuple[str, str], int] = defaultdict(int)
        self._openmemory_latency_sum: dict[tuple[str, str], float] = defaultdict(float)
        self._openmemory_latency_count: dict[tuple[str, str], int] = defaultdict(int)

    def observe_http(self, method: str, path: str, status: str, duration: float) -> None:
        with self._lock:
            key = (method, path, status)
            self._http_total[key] += 1
            self._http_latency_sum[key] += duration
            self._http_latency_count[key] += 1

    def observe_tool(self, tool: str, status: str, duration: float) -> None:
        with self._lock:
            key = (tool, status)
            self._tool_total[key] += 1
            self._tool_latency_sum[key] += duration
            self._tool_latency_count[key] += 1

    def observe_openmemory(self, operation: str, status: str, duration: float) -> None:
        with self._lock:
            key = (operation, status)
            self._openmemory_total[key] += 1
            self._openmemory_latency_sum[key] += duration
            self._openmemory_latency_count[key] += 1

    @staticmethod
    def _escape_label_value(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _labels(**kwargs: str) -> str:
        parts = [f'{k}="{_FallbackMetricStore._escape_label_value(v)}"' for k, v in kwargs.items()]
        return ",".join(parts)

    def render(self) -> str:
        lines: list[str] = []
        lines.append("# HELP gateway_http_requests_total Total Gateway HTTP requests.")
        lines.append("# TYPE gateway_http_requests_total counter")
        for (method, path, status), counter_value in sorted(self._http_total.items()):
            lines.append(
                "gateway_http_requests_total{%s} %d"
                % (
                    self._labels(method=method, path=path, status=status),
                    counter_value,
                )
            )
        lines.append("# HELP gateway_http_request_duration_seconds Gateway HTTP request duration.")
        lines.append("# TYPE gateway_http_request_duration_seconds summary")
        for (method, path, status), latency_sum in sorted(self._http_latency_sum.items()):
            labels = self._labels(method=method, path=path, status=status)
            count = self._http_latency_count[(method, path, status)]
            lines.append(f"gateway_http_request_duration_seconds_sum{{{labels}}} {latency_sum:.9f}")
            lines.append(f"gateway_http_request_duration_seconds_count{{{labels}}} {count}")

        lines.append("# HELP gateway_mcp_tool_calls_total Total MCP tool calls.")
        lines.append("# TYPE gateway_mcp_tool_calls_total counter")
        for (tool, status), counter_value in sorted(self._tool_total.items()):
            lines.append(
                "gateway_mcp_tool_calls_total{%s} %d"
                % (self._labels(tool=tool, status=status), counter_value)
            )
        lines.append("# HELP gateway_mcp_tool_duration_seconds MCP tool call duration.")
        lines.append("# TYPE gateway_mcp_tool_duration_seconds summary")
        for (tool, status), latency_sum in sorted(self._tool_latency_sum.items()):
            labels = self._labels(tool=tool, status=status)
            count = self._tool_latency_count[(tool, status)]
            lines.append(f"gateway_mcp_tool_duration_seconds_sum{{{labels}}} {latency_sum:.9f}")
            lines.append(f"gateway_mcp_tool_duration_seconds_count{{{labels}}} {count}")

        lines.append("# HELP gateway_openmemory_calls_total Total OpenMemory calls.")
        lines.append("# TYPE gateway_openmemory_calls_total counter")
        for (operation, status), counter_value in sorted(self._openmemory_total.items()):
            lines.append(
                "gateway_openmemory_calls_total{%s} %d"
                % (self._labels(operation=operation, status=status), counter_value)
            )
        lines.append("# HELP gateway_openmemory_call_duration_seconds OpenMemory call duration.")
        lines.append("# TYPE gateway_openmemory_call_duration_seconds summary")
        for (operation, status), latency_sum in sorted(self._openmemory_latency_sum.items()):
            labels = self._labels(operation=operation, status=status)
            count = self._openmemory_latency_count[(operation, status)]
            lines.append(
                f"gateway_openmemory_call_duration_seconds_sum{{{labels}}} {latency_sum:.9f}"
            )
            lines.append(f"gateway_openmemory_call_duration_seconds_count{{{labels}}} {count}")

        lines.append("")
        return "\n".join(lines)


_fallback_metrics = _FallbackMetricStore()
_prom_registry = None
_prom_http_total = None
_prom_http_duration = None
_prom_tool_total = None
_prom_tool_duration = None
_prom_om_total = None
_prom_om_duration = None


def _ensure_prometheus_metrics() -> bool:
    global _prom_registry
    global _prom_http_total
    global _prom_http_duration
    global _prom_tool_total
    global _prom_tool_duration
    global _prom_om_total
    global _prom_om_duration

    if not _PROMETHEUS_AVAILABLE:
        return False

    if _prom_registry is not None:
        return True

    registry = CollectorRegistry()
    _prom_http_total = Counter(
        "gateway_http_requests_total",
        "Total Gateway HTTP requests.",
        ["method", "path", "status"],
        registry=registry,
    )
    _prom_http_duration = Histogram(
        "gateway_http_request_duration_seconds",
        "Gateway HTTP request duration.",
        ["method", "path", "status"],
        registry=registry,
    )
    _prom_tool_total = Counter(
        "gateway_mcp_tool_calls_total",
        "Total MCP tool calls.",
        ["tool", "status"],
        registry=registry,
    )
    _prom_tool_duration = Histogram(
        "gateway_mcp_tool_duration_seconds",
        "MCP tool call duration.",
        ["tool", "status"],
        registry=registry,
    )
    _prom_om_total = Counter(
        "gateway_openmemory_calls_total",
        "Total OpenMemory calls.",
        ["operation", "status"],
        registry=registry,
    )
    _prom_om_duration = Histogram(
        "gateway_openmemory_call_duration_seconds",
        "OpenMemory call duration.",
        ["operation", "status"],
        registry=registry,
    )
    _prom_registry = registry
    return True


def observe_http_request(method: str, path: str, status_code: int, duration_seconds: float) -> None:
    """记录 HTTP 请求指标。"""
    if not _metrics_enabled():
        return

    status = str(status_code)
    if _ensure_prometheus_metrics():
        assert _prom_http_total is not None
        assert _prom_http_duration is not None
        _prom_http_total.labels(method=method, path=path, status=status).inc()
        _prom_http_duration.labels(method=method, path=path, status=status).observe(
            max(duration_seconds, 0.0)
        )
    else:
        _fallback_metrics.observe_http(method, path, status, max(duration_seconds, 0.0))


def observe_mcp_tool_call(tool: str, status: str, duration_seconds: float) -> None:
    """记录 MCP 工具调用指标。"""
    if not _metrics_enabled():
        return

    normalized_status = status or "unknown"
    if _ensure_prometheus_metrics():
        assert _prom_tool_total is not None
        assert _prom_tool_duration is not None
        _prom_tool_total.labels(tool=tool, status=normalized_status).inc()
        _prom_tool_duration.labels(tool=tool, status=normalized_status).observe(
            max(duration_seconds, 0.0)
        )
    else:
        _fallback_metrics.observe_tool(tool, normalized_status, max(duration_seconds, 0.0))


def observe_openmemory_call(operation: str, status: str, duration_seconds: float) -> None:
    """记录 OpenMemory 调用指标。"""
    if not _metrics_enabled():
        return

    normalized_status = status or "unknown"
    if _ensure_prometheus_metrics():
        assert _prom_om_total is not None
        assert _prom_om_duration is not None
        _prom_om_total.labels(operation=operation, status=normalized_status).inc()
        _prom_om_duration.labels(operation=operation, status=normalized_status).observe(
            max(duration_seconds, 0.0)
        )
    else:
        _fallback_metrics.observe_openmemory(
            operation, normalized_status, max(duration_seconds, 0.0)
        )


def render_metrics_payload() -> tuple[str, str]:
    """渲染 /metrics 响应体与 content-type。"""
    if not _metrics_enabled():
        return "# metrics disabled\n", CONTENT_TYPE_LATEST

    if _ensure_prometheus_metrics():
        assert _prom_registry is not None
        assert generate_latest is not None
        payload = generate_latest(_prom_registry).decode("utf-8")
        return payload, CONTENT_TYPE_LATEST

    return _fallback_metrics.render(), CONTENT_TYPE_LATEST


_trace_lock = threading.Lock()
_trace_backend: str | None = None
_trace_warning_emitted = False


def _safe_attr_value(value: Any) -> Any:
    if isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _setup_tracing_backend() -> str:
    """
    初始化 tracing 后端。

    返回值:
    - "disabled": tracing 关闭
    - "otel": 已启用 OTel
    - "log": 启用但降级为日志 span
    """
    global _trace_backend
    global _trace_warning_emitted

    if _trace_backend is not None:
        return _trace_backend

    with _trace_lock:
        if _trace_backend is not None:
            return _trace_backend

        if not _otel_enabled():
            _trace_backend = "disabled"
            return _trace_backend

        if not _OTEL_AVAILABLE:
            if not _trace_warning_emitted:
                logger.warning(
                    "GATEWAY_OTEL_ENABLED=1 但缺少 OpenTelemetry 依赖，降级为日志 span。"
                )
                _trace_warning_emitted = True
            _trace_backend = "log"
            return _trace_backend

        assert trace is not None
        assert TracerProvider is not None

        provider = trace.get_tracer_provider()
        if provider.__class__.__name__.lower().startswith("proxy"):
            assert Resource is not None
            resource = Resource.create({"service.name": _otel_service_name()})
            provider = TracerProvider(resource=resource)

            exporter = _otel_exporter()
            if exporter == "console":
                assert BatchSpanProcessor is not None
                assert ConsoleSpanExporter is not None
                provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

            trace.set_tracer_provider(provider)

        _trace_backend = "otel"
        return _trace_backend


@contextmanager
def start_span(
    name: str,
    *,
    correlation_id: str | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[None]:
    """
    开启一个基础 span。

    - OTel 可用且开启时：真实 span
    - OTel 不可用但开关开启时：日志 span
    - 开关关闭时：no-op
    """
    backend = _setup_tracing_backend()
    attrs = dict(attributes or {})
    if correlation_id:
        attrs["engram.correlation_id"] = correlation_id

    if backend == "disabled":
        yield
        return

    if backend == "otel":
        assert trace is not None
        tracer = trace.get_tracer("engram.gateway")
        with tracer.start_as_current_span(name) as span:
            for key, value in attrs.items():
                span.set_attribute(key, _safe_attr_value(value))
            try:
                yield
            except Exception as exc:
                span.record_exception(exc)
                if Status is not None and StatusCode is not None:
                    span.set_status(Status(StatusCode.ERROR))
                raise
        return

    started = time.perf_counter()
    logger.info("trace.start name=%s correlation_id=%s attrs=%s", name, correlation_id, attrs)
    try:
        yield
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        logger.warning(
            "trace.error name=%s correlation_id=%s duration_ms=%.2f error=%s",
            name,
            correlation_id,
            elapsed_ms,
            exc,
        )
        raise
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        logger.info(
            "trace.end name=%s correlation_id=%s duration_ms=%.2f",
            name,
            correlation_id,
            elapsed_ms,
        )
