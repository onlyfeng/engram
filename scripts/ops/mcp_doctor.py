#!/usr/bin/env python3
"""
mcp_doctor - Gateway MCP 健康诊断

检查项:
- GET /health
- OPTIONS /mcp
- POST /mcp (initialize)
- POST /mcp (ping)
- POST /mcp (tools/list)
- POST /mcp (correlation_id uniqueness)
- POST /mcp (unknown method)
- 无 MCP 配置时安全跳过
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib import error, request

DEFAULT_GATEWAY_HOST = "127.0.0.1"
DEFAULT_GATEWAY_PORT = "8787"
DEFAULT_TIMEOUT_SECONDS = 5.0
RESPONSE_PREVIEW_LIMIT = 200
DEFAULT_MCP_SERVER = "engram"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MCP_CONFIG_ENV = "MCP_DOCTOR_CONFIG"
# 合约必需 header（占位值用于对齐 preflight 与 POST 声明）
CONTRACT_REQUIRED_HEADERS = {
    "Mcp-Session-Id": "mcp-doctor-session",
    "Authorization": "Bearer mcp-doctor",
}
VALID_ERROR_CATEGORIES = {"protocol", "validation", "business", "dependency", "internal"}
CORRELATION_ID_PATTERN = re.compile(r"^corr-[a-fA-F0-9]{16}$")
EXPECTED_TOOL_NAMES = {
    "artifacts_exists",
    "artifacts_get",
    "artifacts_put",
    "evidence_read",
    "memory_store",
    "memory_query",
    "reliability_report",
    "governance_update",
    "evidence_upload",
    "logbook_add_event",
    "logbook_attach",
    "logbook_create_item",
    "logbook_get_kv",
    "logbook_list_attachments",
    "logbook_query_events",
    "logbook_query_items",
    "logbook_set_kv",
    "scm_materialize_patch_blob",
    "scm_patch_blob_resolve",
}
EXPECTED_REQUIRED_FIELDS = {
    "artifacts_exists": [],
    "artifacts_get": [],
    "artifacts_put": [],
    "memory_store": ["payload_md"],
    "memory_query": ["query"],
    "reliability_report": [],
    "governance_update": [],
    "evidence_upload": ["content", "content_type"],
    "evidence_read": ["uri"],
    "logbook_create_item": ["item_type", "title"],
    "logbook_add_event": ["item_id", "event_type"],
    "logbook_attach": ["item_id", "kind", "uri", "sha256"],
    "logbook_set_kv": ["namespace", "key", "value_json"],
    "logbook_get_kv": ["namespace", "key"],
    "logbook_query_items": [],
    "logbook_query_events": [],
    "logbook_list_attachments": [],
    "scm_patch_blob_resolve": [],
    "scm_materialize_patch_blob": [],
}
EXPECTED_EVIDENCE_UPLOAD_PROPERTIES = {
    "content",
    "content_type",
    "title",
    "actor_user_id",
    "project_key",
    "item_id",
}


@dataclass
class CheckResult:
    name: str
    passed: bool
    message: str
    details: Optional[str] = None
    status_code: Optional[int] = None
    missing_headers: list[str] = field(default_factory=list)
    missing_expose_headers: list[str] = field(default_factory=list)
    error: Optional[str] = None
    response_preview: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "message": self.message,
            "status_code": self.status_code,
            "missing_headers": self.missing_headers,
            "missing_expose_headers": self.missing_expose_headers,
            "error": self.error,
            "response_preview": self.response_preview,
        }


@dataclass
class McpTarget:
    server_name: str
    mcp_url: str
    headers: Dict[str, str]
    config_path: Path


def _get_gateway_url() -> Optional[str]:
    gateway_url = os.environ.get("GATEWAY_URL")
    if gateway_url:
        return gateway_url.rstrip("/")
    host = os.environ.get("GATEWAY_HOST")
    port = os.environ.get("GATEWAY_PORT")
    if host or port:
        host = host or DEFAULT_GATEWAY_HOST
        port = port or DEFAULT_GATEWAY_PORT
        return f"http://{host}:{port}"
    return None


def _get_timeout() -> float:
    raw = os.environ.get("MCP_DOCTOR_TIMEOUT", "")
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS


def _get_default_config_paths() -> list[Path]:
    return [
        PROJECT_ROOT / ".cursor" / "mcp.json",
        Path.home() / ".cursor" / "mcp.json",
    ]


def _resolve_config_path(explicit_path: Optional[str]) -> Tuple[Optional[Path], Optional[str]]:
    candidates = []
    if explicit_path:
        candidates.append(Path(explicit_path).expanduser())
    env_path = os.environ.get(MCP_CONFIG_ENV, "").strip()
    if env_path:
        candidates.append(Path(env_path).expanduser())
    if not candidates:
        for path in _get_default_config_paths():
            if path.is_file():
                return path, None
        return None, None
    for path in candidates:
        if not path.exists():
            return None, f"配置文件不存在: {path}"
        if not path.is_file():
            return None, f"配置路径不是文件: {path}"
        return path, None
    return None, None


def _load_mcp_config(config_path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        with config_path.open(encoding="utf-8") as handle:
            return json.load(handle), None
    except json.JSONDecodeError as exc:
        return None, f"配置 JSON 解析失败: {exc}"
    except OSError as exc:
        return None, f"配置文件读取失败: {exc}"


def _resolve_mcp_target(
    config: Dict[str, Any],
    *,
    config_path: Path,
    server_name: Optional[str],
) -> Tuple[Optional[McpTarget], Optional[str], Optional[str]]:
    if "mcpServers" not in config:
        return None, "配置缺少 mcpServers 字段", None
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        return None, "mcpServers 必须是对象", None
    if not servers:
        return None, None, "mcpServers 为空，未配置可用 MCP server"

    selected = server_name
    if selected:
        if selected not in servers:
            return (
                None,
                f"配置中未找到 server: {selected}",
                None,
            )
    else:
        if DEFAULT_MCP_SERVER in servers:
            selected = DEFAULT_MCP_SERVER
        elif len(servers) == 1:
            selected = next(iter(servers.keys()))
        else:
            return (
                None,
                "存在多个 MCP server，请使用 --server 指定",
                None,
            )

    server = servers.get(selected)
    if not isinstance(server, dict):
        return None, f"server 配置非法: {selected}", None

    server_type = server.get("type")
    url = server.get("url")
    if not server_type or not url:
        return None, f"server '{selected}' 缺少 type 或 url", None
    if server_type != "http":
        return None, f"server '{selected}' type 非 http: {server_type}", None
    if not isinstance(url, str) or not url.strip():
        return None, f"server '{selected}' url 非法", None
    url = url.rstrip("/")
    if not url.endswith("/mcp"):
        return None, f"server '{selected}' url 必须以 /mcp 结尾", None

    headers = server.get("headers") or {}
    if not isinstance(headers, dict):
        return None, f"server '{selected}' headers 必须为对象", None
    for key, value in headers.items():
        if not isinstance(key, str) or not isinstance(value, str):
            return None, f"server '{selected}' headers 需为字符串键值", None

    return McpTarget(selected, url, headers, config_path), None, None


def _parse_header_args(raw_headers: list[str]) -> Dict[str, str]:
    parsed: Dict[str, str] = {}
    for raw in raw_headers:
        if ":" not in raw:
            raise ValueError(f"Header 格式非法: {raw}（需为 'Key: Value'）")
        key, value = raw.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"Header key 不能为空: {raw}")
        parsed[key] = value
    return parsed


def _build_request_headers(
    custom_headers: Dict[str, str],
    *,
    config_headers: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if config_headers:
        headers.update(config_headers)
    authorization = os.environ.get("MCP_DOCTOR_AUTHORIZATION")
    if authorization:
        headers["Authorization"] = authorization
    # CLI 传入的 header 优先级更高
    headers.update(custom_headers)
    return headers


def _has_header(headers: Dict[str, str], name: str) -> bool:
    target = name.lower()
    return any(key.lower() == target for key in headers)


def _ensure_required_request_headers(request_headers: Dict[str, str]) -> Dict[str, str]:
    headers = dict(request_headers)
    for key, value in CONTRACT_REQUIRED_HEADERS.items():
        if not _has_header(headers, key):
            headers[key] = value
    return headers


def _prepare_request_headers(
    custom_headers: Dict[str, str],
    *,
    config_headers: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    return _ensure_required_request_headers(
        _build_request_headers(custom_headers, config_headers=config_headers)
    )


def _build_required_headers(preflight_headers: Dict[str, str]) -> set[str]:
    return _split_header_values(preflight_headers.get("Access-Control-Request-Headers"))


def _build_preflight_headers(request_headers: Dict[str, str]) -> Dict[str, str]:
    header_names = []
    seen = set()
    for key in request_headers:
        name = key.strip()
        if not name:
            continue
        normalized = name.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        header_names.append(name)
    header_names = sorted(header_names, key=str.lower)
    access_request_headers = ", ".join(header_names)
    return {
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": access_request_headers,
    }


def _build_mcp_url(base_or_mcp_url: str) -> str:
    normalized = base_or_mcp_url.rstrip("/")
    if normalized.endswith("/mcp"):
        return normalized
    return f"{normalized}/mcp"


def _derive_gateway_url(mcp_url: str) -> Optional[str]:
    normalized = mcp_url.rstrip("/")
    if normalized.endswith("/mcp"):
        return normalized[: -len("/mcp")]
    return None


def _request(
    method: str,
    url: str,
    *,
    data: Optional[bytes] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Tuple[Optional[int], Optional[bytes], Dict[str, str], Optional[str]]:
    req = request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), resp.read(), dict(resp.headers), None
    except error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers), None
    except error.URLError as exc:
        reason = exc.reason if hasattr(exc, "reason") else exc
        return None, None, {}, str(reason)
    except Exception as exc:  # pragma: no cover - 防御性兜底
        return None, None, {}, str(exc)


def _parse_json(body: Optional[bytes]) -> Tuple[Optional[Any], Optional[str]]:
    if body is None:
        return None, "响应体为空"
    try:
        return json.loads(body.decode("utf-8")), None
    except Exception as exc:
        return None, f"JSON 解析失败: {exc}"


def _get_header(headers: Dict[str, str], name: str) -> Optional[str]:
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def _split_header_values(value: Optional[str]) -> set[str]:
    if not value:
        return set()
    return {item.strip().lower() for item in value.split(",") if item.strip()}


def _validate_correlation_id(value: Optional[str]) -> Optional[str]:
    if not value:
        return "缺少 X-Correlation-ID 头"
    if not CORRELATION_ID_PATTERN.match(value):
        return f"X-Correlation-ID 格式错误: {value}"
    return None


def _validate_error_data(error_data: Any) -> Optional[str]:
    if not isinstance(error_data, dict):
        return "error.data 必须为对象"
    required_fields = ("category", "reason", "retryable", "correlation_id")
    missing = [field for field in required_fields if field not in error_data]
    if missing:
        return f"error.data 缺少字段: {', '.join(missing)}"
    category = error_data.get("category")
    reason = error_data.get("reason")
    retryable = error_data.get("retryable")
    correlation_id = error_data.get("correlation_id")
    if not isinstance(category, str) or category not in VALID_ERROR_CATEGORIES:
        return f"error.data.category 非法: {category}"
    if not isinstance(reason, str) or not reason:
        return "error.data.reason 必须为非空字符串"
    if not isinstance(retryable, bool):
        return "error.data.retryable 必须为布尔值"
    if not isinstance(correlation_id, str) or not CORRELATION_ID_PATTERN.match(correlation_id):
        return f"error.data.correlation_id 格式错误: {correlation_id}"
    return None


def _post_jsonrpc(
    mcp_url: str,
    payload: Dict[str, Any],
    *,
    headers: Dict[str, str],
    timeout: float,
) -> Tuple[Optional[int], Optional[Dict[str, Any]], Dict[str, str], Optional[str], Optional[str]]:
    data = json.dumps(payload).encode("utf-8")
    status, body, response_headers, err = _request(
        "POST", mcp_url, data=data, headers=headers, timeout=timeout
    )
    preview = _preview_body(body)
    if err:
        return status, None, response_headers, err, preview
    parsed, parse_err = _parse_json(body)
    if parse_err:
        return status, None, response_headers, parse_err, preview
    if not isinstance(parsed, dict):
        return status, None, response_headers, "响应格式非法", preview
    return status, parsed, response_headers, None, preview


def _preview_body(body: Optional[bytes]) -> Optional[str]:
    if not body:
        return None
    preview = body.decode("utf-8", errors="replace")
    if len(preview) > RESPONSE_PREVIEW_LIMIT:
        preview = preview[:RESPONSE_PREVIEW_LIMIT] + "...(truncated)"
    return preview


def _check_health(gateway_url: str, timeout: float) -> CheckResult:
    status, body, _, err = _request("GET", f"{gateway_url}/health", timeout=timeout)
    preview = _preview_body(body)
    if err:
        return CheckResult(
            "GET /health",
            False,
            "请求失败",
            err,
            status_code=status,
            error=err,
            response_preview=preview,
        )
    if status != 200:
        return CheckResult(
            "GET /health",
            False,
            f"状态码异常: {status}",
            status_code=status,
            response_preview=preview,
        )
    return CheckResult("GET /health", True, "OK", status_code=status)


def _check_options(
    gateway_url: str,
    timeout: float,
    *,
    request_headers: Dict[str, str],
) -> CheckResult:
    preflight_headers = _build_preflight_headers(request_headers)
    required_headers = _build_required_headers(preflight_headers)
    mcp_url = _build_mcp_url(gateway_url)
    status, body, headers, err = _request(
        "OPTIONS",
        mcp_url,
        headers=preflight_headers,
        timeout=timeout,
    )
    preview = _preview_body(body)
    if err:
        return CheckResult(
            "OPTIONS /mcp",
            False,
            "请求失败",
            err,
            status_code=status,
            error=err,
            response_preview=preview,
        )
    if status not in (200, 204):
        return CheckResult(
            "OPTIONS /mcp",
            False,
            f"状态码异常: {status}",
            status_code=status,
            response_preview=preview,
        )

    allow_origin = _get_header(headers, "Access-Control-Allow-Origin")
    allow_methods_raw = _get_header(headers, "Access-Control-Allow-Methods")
    allow_headers_raw = _get_header(headers, "Access-Control-Allow-Headers")
    expose_headers_raw = _get_header(headers, "Access-Control-Expose-Headers")

    allow_methods = _split_header_values(allow_methods_raw)
    allow_headers = _split_header_values(allow_headers_raw)
    expose_headers = _split_header_values(expose_headers_raw)

    missing = []
    if not allow_origin:
        missing.append("Access-Control-Allow-Origin")
    if not {"post", "options"}.issubset(allow_methods):
        missing.append("Access-Control-Allow-Methods")
    missing_header_values = sorted(required_headers - allow_headers)
    if not required_headers.issubset(allow_headers):
        missing.append("Access-Control-Allow-Headers")
    missing_headers_field = sorted(set(missing + missing_header_values))

    required_expose_headers = {"mcp-session-id", "x-correlation-id"}
    missing_expose_values = sorted(required_expose_headers - expose_headers)

    if missing:
        return CheckResult(
            "OPTIONS /mcp",
            False,
            "CORS 头缺失或不完整",
            ", ".join(missing),
            status_code=status,
            missing_headers=missing_headers_field,
            missing_expose_headers=missing_expose_values,
            response_preview=preview,
        )

    return CheckResult(
        "OPTIONS /mcp",
        True,
        "OK",
        status_code=status,
        missing_expose_headers=missing_expose_values,
    )


def _check_tools_list(
    gateway_url: str, timeout: float, *, request_headers: Dict[str, str]
) -> CheckResult:
    headers = dict(request_headers)
    mcp_url = _build_mcp_url(gateway_url)
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}

    status, parsed, response_headers, err, preview = _post_jsonrpc(
        mcp_url, payload, headers=headers, timeout=timeout
    )
    if err:
        return CheckResult(
            "POST /mcp (tools/list)",
            False,
            "请求失败",
            err,
            status_code=status,
            error=err,
            response_preview=preview,
        )
    if status != 200:
        detail = "鉴权失败" if status in (401, 403) else None
        return CheckResult(
            "POST /mcp (tools/list)",
            False,
            f"状态码异常: {status}",
            details=detail,
            status_code=status,
            response_preview=preview,
        )

    if parsed.get("error") is not None:
        return CheckResult(
            "POST /mcp (tools/list)",
            False,
            "JSON-RPC 返回错误",
            status_code=status,
            response_preview=preview,
        )
    if parsed.get("jsonrpc") != "2.0" or parsed.get("id") != 1:
        return CheckResult(
            "POST /mcp (tools/list)",
            False,
            "JSON-RPC 响应字段异常",
            status_code=status,
            response_preview=preview,
        )

    correlation_error = _validate_correlation_id(
        _get_header(response_headers, "X-Correlation-ID")
    )
    if correlation_error:
        return CheckResult(
            "POST /mcp (tools/list)",
            False,
            "响应头缺失或格式异常",
            details=correlation_error,
            status_code=status,
            response_preview=preview,
        )
    expose_raw = _get_header(response_headers, "Access-Control-Expose-Headers")
    expose_headers = _split_header_values(expose_raw)
    if "x-correlation-id" not in expose_headers:
        return CheckResult(
            "POST /mcp (tools/list)",
            False,
            "Expose-Headers 缺少 X-Correlation-ID",
            status_code=status,
            response_preview=preview,
        )

    result = parsed.get("result")
    if not isinstance(result, dict):
        return CheckResult(
            "POST /mcp (tools/list)",
            False,
            "result 字段缺失或格式错误",
            status_code=status,
            response_preview=preview,
        )

    tools = result.get("tools")
    if not isinstance(tools, list):
        return CheckResult(
            "POST /mcp (tools/list)",
            False,
            "tools 非数组",
            status_code=status,
            response_preview=preview,
        )
    if len(tools) != len(EXPECTED_TOOL_NAMES):
        return CheckResult(
            "POST /mcp (tools/list)",
            False,
            f"tools 数量不匹配: {len(tools)}",
            status_code=status,
            response_preview=preview,
        )

    tools_by_name: Dict[str, Dict[str, Any]] = {}
    for tool in tools:
        if not isinstance(tool, dict):
            return CheckResult(
                "POST /mcp (tools/list)",
                False,
                "工具定义格式错误",
                status_code=status,
                response_preview=preview,
            )
        name = tool.get("name")
        if not name or not tool.get("description") or "inputSchema" not in tool:
            return CheckResult(
                "POST /mcp (tools/list)",
                False,
                "工具定义字段缺失",
                status_code=status,
                response_preview=preview,
            )
        input_schema = tool.get("inputSchema")
        if not isinstance(input_schema, dict) or input_schema.get("type") != "object":
            return CheckResult(
                "POST /mcp (tools/list)",
                False,
                f"工具 {name} inputSchema 非 object",
                status_code=status,
                response_preview=preview,
            )
        tools_by_name[name] = tool

    tool_names = set(tools_by_name.keys())
    if tool_names != EXPECTED_TOOL_NAMES:
        return CheckResult(
            "POST /mcp (tools/list)",
            False,
            f"工具名称不匹配: {sorted(tool_names)}",
            status_code=status,
            response_preview=preview,
        )

    for tool_name, expected_required in EXPECTED_REQUIRED_FIELDS.items():
        tool = tools_by_name.get(tool_name)
        if tool is None:
            return CheckResult(
                "POST /mcp (tools/list)",
                False,
                f"缺少工具: {tool_name}",
                status_code=status,
                response_preview=preview,
            )
        input_schema = tool["inputSchema"]
        actual_required = input_schema.get("required") or []
        if not isinstance(actual_required, list):
            return CheckResult(
                "POST /mcp (tools/list)",
                False,
                f"工具 {tool_name} required 非数组",
                status_code=status,
                response_preview=preview,
            )
        if set(actual_required) != set(expected_required):
            return CheckResult(
                "POST /mcp (tools/list)",
                False,
                f"工具 {tool_name} required 不匹配",
                details=f"期望 {expected_required}，实际 {actual_required}",
                status_code=status,
                response_preview=preview,
            )

    evidence_upload = tools_by_name.get("evidence_upload")
    if evidence_upload:
        input_schema = evidence_upload["inputSchema"]
        properties = input_schema.get("properties")
        if not isinstance(properties, dict):
            return CheckResult(
                "POST /mcp (tools/list)",
                False,
                "evidence_upload properties 缺失或格式错误",
                status_code=status,
                response_preview=preview,
            )
        if set(properties.keys()) != EXPECTED_EVIDENCE_UPLOAD_PROPERTIES:
            return CheckResult(
                "POST /mcp (tools/list)",
                False,
                "evidence_upload properties 不匹配",
                details=f"期望 {sorted(EXPECTED_EVIDENCE_UPLOAD_PROPERTIES)}",
                status_code=status,
                response_preview=preview,
            )
        content_entry = properties.get("content")
        content_type_entry = properties.get("content_type")
        if not isinstance(content_entry, dict) or not isinstance(content_type_entry, dict):
            return CheckResult(
                "POST /mcp (tools/list)",
                False,
                "evidence_upload content 定义格式错误",
                status_code=status,
                response_preview=preview,
            )
        content_type = content_type_entry.get("type")
        content_type_value = content_entry.get("type")
        if content_type_value != "string" or content_type != "string":
            return CheckResult(
                "POST /mcp (tools/list)",
                False,
                "evidence_upload content 类型错误",
                details=f"content={content_type_value}, content_type={content_type}",
                status_code=status,
                response_preview=preview,
            )

    return CheckResult(
        "POST /mcp (tools/list)", True, f"OK ({len(tools)} tools)", status_code=status
    )


def _check_initialize(
    gateway_url: str, timeout: float, *, request_headers: Dict[str, str]
) -> CheckResult:
    headers = dict(request_headers)
    mcp_url = _build_mcp_url(gateway_url)
    payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize"}

    status, parsed, _, err, preview = _post_jsonrpc(
        mcp_url, payload, headers=headers, timeout=timeout
    )
    if err:
        return CheckResult(
            "POST /mcp (initialize)",
            False,
            "请求失败",
            err,
            status_code=status,
            error=err,
            response_preview=preview,
        )
    if status != 200:
        detail = "鉴权失败" if status in (401, 403) else None
        return CheckResult(
            "POST /mcp (initialize)",
            False,
            f"状态码异常: {status}",
            details=detail,
            status_code=status,
            response_preview=preview,
        )
    if parsed.get("error") is not None:
        return CheckResult(
            "POST /mcp (initialize)",
            False,
            "JSON-RPC 返回错误",
            status_code=status,
            response_preview=preview,
        )
    result = parsed.get("result")
    if not isinstance(result, dict):
        return CheckResult(
            "POST /mcp (initialize)",
            False,
            "result 字段缺失或格式错误",
            status_code=status,
            response_preview=preview,
        )
    if not isinstance(result.get("protocolVersion"), str):
        return CheckResult(
            "POST /mcp (initialize)",
            False,
            "protocolVersion 缺失或格式错误",
            status_code=status,
            response_preview=preview,
        )
    capabilities = result.get("capabilities")
    if not isinstance(capabilities, dict) or not isinstance(capabilities.get("tools"), dict):
        return CheckResult(
            "POST /mcp (initialize)",
            False,
            "capabilities.tools 缺失或格式错误",
            status_code=status,
            response_preview=preview,
        )
    server_info = result.get("serverInfo")
    if not isinstance(server_info, dict) or not server_info.get("name") or not server_info.get(
        "version"
    ):
        return CheckResult(
            "POST /mcp (initialize)",
            False,
            "serverInfo 缺失或格式错误",
            status_code=status,
            response_preview=preview,
        )

    return CheckResult("POST /mcp (initialize)", True, "OK", status_code=status)


def _check_ping(
    gateway_url: str, timeout: float, *, request_headers: Dict[str, str]
) -> CheckResult:
    headers = dict(request_headers)
    mcp_url = _build_mcp_url(gateway_url)
    payload = {"jsonrpc": "2.0", "id": 2, "method": "ping"}

    status, parsed, _, err, preview = _post_jsonrpc(
        mcp_url, payload, headers=headers, timeout=timeout
    )
    if err:
        return CheckResult(
            "POST /mcp (ping)",
            False,
            "请求失败",
            err,
            status_code=status,
            error=err,
            response_preview=preview,
        )
    if status != 200:
        detail = "鉴权失败" if status in (401, 403) else None
        return CheckResult(
            "POST /mcp (ping)",
            False,
            f"状态码异常: {status}",
            details=detail,
            status_code=status,
            response_preview=preview,
        )
    if parsed.get("error") is not None:
        return CheckResult(
            "POST /mcp (ping)",
            False,
            "JSON-RPC 返回错误",
            status_code=status,
            response_preview=preview,
        )
    if parsed.get("result") != {}:
        return CheckResult(
            "POST /mcp (ping)",
            False,
            "result 应为空对象",
            status_code=status,
            response_preview=preview,
        )
    return CheckResult("POST /mcp (ping)", True, "OK", status_code=status)


def _check_unknown_method_error(
    gateway_url: str, timeout: float, *, request_headers: Dict[str, str]
) -> CheckResult:
    headers = dict(request_headers)
    mcp_url = _build_mcp_url(gateway_url)
    payload = {"jsonrpc": "2.0", "id": 3, "method": "unknown/method"}

    status, parsed, response_headers, err, preview = _post_jsonrpc(
        mcp_url, payload, headers=headers, timeout=timeout
    )
    if err:
        return CheckResult(
            "POST /mcp (unknown method)",
            False,
            "请求失败",
            err,
            status_code=status,
            error=err,
            response_preview=preview,
        )
    if status != 200:
        detail = "鉴权失败" if status in (401, 403) else None
        return CheckResult(
            "POST /mcp (unknown method)",
            False,
            f"状态码异常: {status}",
            details=detail,
            status_code=status,
            response_preview=preview,
        )
    error = parsed.get("error")
    if not isinstance(error, dict):
        return CheckResult(
            "POST /mcp (unknown method)",
            False,
            "error 字段缺失或格式错误",
            status_code=status,
            response_preview=preview,
        )
    if error.get("code") != -32601:
        return CheckResult(
            "POST /mcp (unknown method)",
            False,
            "错误码不匹配（期望 -32601）",
            status_code=status,
            response_preview=preview,
        )
    error_data = error.get("data")
    error_data_issue = _validate_error_data(error_data)
    if error_data_issue:
        return CheckResult(
            "POST /mcp (unknown method)",
            False,
            "error.data 不符合契约",
            details=error_data_issue,
            status_code=status,
            response_preview=preview,
        )
    correlation_error = _validate_correlation_id(
        _get_header(response_headers, "X-Correlation-ID")
    )
    if correlation_error:
        return CheckResult(
            "POST /mcp (unknown method)",
            False,
            "响应头缺失或格式异常",
            details=correlation_error,
            status_code=status,
            response_preview=preview,
        )
    return CheckResult("POST /mcp (unknown method)", True, "OK", status_code=status)


def _check_correlation_id_uniqueness(
    gateway_url: str, timeout: float, *, request_headers: Dict[str, str], attempts: int = 3
) -> CheckResult:
    headers = dict(request_headers)
    mcp_url = _build_mcp_url(gateway_url)
    seen: set[str] = set()

    for index in range(attempts):
        payload = {"jsonrpc": "2.0", "id": 100 + index, "method": "tools/list"}
        status, parsed, response_headers, err, preview = _post_jsonrpc(
            mcp_url, payload, headers=headers, timeout=timeout
        )
        if err:
            return CheckResult(
                "POST /mcp (correlation_id uniqueness)",
                False,
                "请求失败",
                err,
                status_code=status,
                error=err,
                response_preview=preview,
            )
        if status != 200 or parsed.get("error") is not None:
            return CheckResult(
                "POST /mcp (correlation_id uniqueness)",
                False,
                "tools/list 返回异常",
                status_code=status,
                response_preview=preview,
            )
        correlation_id = _get_header(response_headers, "X-Correlation-ID")
        correlation_error = _validate_correlation_id(correlation_id)
        if correlation_error:
            return CheckResult(
                "POST /mcp (correlation_id uniqueness)",
                False,
                "响应头缺失或格式异常",
                details=correlation_error,
                status_code=status,
                response_preview=preview,
            )
        if correlation_id in seen:
            return CheckResult(
                "POST /mcp (correlation_id uniqueness)",
                False,
                "correlation_id 不唯一",
                details=correlation_id,
                status_code=status,
                response_preview=preview,
            )
        seen.add(correlation_id)

    return CheckResult(
        "POST /mcp (correlation_id uniqueness)",
        True,
        f"OK ({attempts} unique)",
    )

def _print_result(result: CheckResult) -> None:
    if result.passed:
        print(f"[OK] {result.name}")
        return
    line = f"[FAIL] {result.name} - {result.message}"
    if result.details:
        line += f" ({result.details})"
    print(line)


def _print_json(
    gateway_url: Optional[str],
    mcp_url: Optional[str],
    timeout: float,
    checks: list[CheckResult],
    *,
    pretty: bool = False,
    config_path: Optional[Path] = None,
    server_name: Optional[str] = None,
    skipped: bool = False,
    skip_reason: Optional[str] = None,
    next_steps: Optional[list[str]] = None,
    error: Optional[str] = None,
) -> None:
    payload = {
        "gateway_url": gateway_url,
        "mcp_url": mcp_url,
        "timeout_seconds": timeout,
        "config_path": str(config_path) if config_path else None,
        "server_name": server_name,
        "skipped": skipped,
        "skip_reason": skip_reason,
        "next_steps": next_steps or [],
        "error": error,
        "checks": [check.to_dict() for check in checks],
    }
    if pretty:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False))


def _print_skip(reason: str, next_steps: list[str], *, config_path: Optional[Path] = None) -> None:
    print("[SKIP] MCP Doctor")
    print(f"原因: {reason}")
    if config_path:
        print(f"配置路径: {config_path}")
    if next_steps:
        print("下一步建议:")
        for step in next_steps:
            print(f"- {step}")


def _print_error(reason: str, next_steps: list[str], *, config_path: Optional[Path] = None) -> None:
    print(f"[ERROR] {reason}", file=sys.stderr)
    if config_path:
        print(f"[ERROR] 配置路径: {config_path}", file=sys.stderr)
    if next_steps:
        print("[ERROR] 下一步建议:", file=sys.stderr)
        for step in next_steps:
            print(f"[ERROR] - {step}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Gateway MCP 健康诊断")
    parser.add_argument(
        "--json", action="store_true", help="输出 JSON 格式诊断结果（适合自动化解析）"
    )
    parser.add_argument("--pretty", action="store_true", help="JSON 输出使用缩进格式")
    parser.add_argument(
        "--gateway-url",
        help="直接指定 Gateway URL（优先于 MCP 配置）",
    )
    parser.add_argument(
        "--config",
        help="指定 MCP 配置文件路径（覆盖默认搜索路径）",
    )
    parser.add_argument(
        "--server",
        help="指定 MCP server 名称（默认 engram 或唯一项）",
    )
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        help="额外请求头（可重复），格式: 'Key: Value'",
    )
    args = parser.parse_args()

    explicit_gateway = args.gateway_url or _get_gateway_url()
    explicit_mcp_url = None
    if explicit_gateway and explicit_gateway.rstrip("/").endswith("/mcp"):
        explicit_mcp_url = explicit_gateway.rstrip("/")
        gateway_url = _derive_gateway_url(explicit_mcp_url)
    else:
        gateway_url = explicit_gateway
    timeout = _get_timeout()

    try:
        custom_headers = _parse_header_args(args.header)
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    config_path = None
    server_name = None
    mcp_url = None
    config_headers: Dict[str, str] = {}

    if gateway_url is None:
        config_path, config_error = _resolve_config_path(args.config)
        if config_error:
            next_steps = [
                "检查 MCP 配置路径是否存在或权限是否可读",
                "参考 configs/mcp/.mcp.json.example 与 docs/gateway/02_mcp_integration_cursor.md",
            ]
            if args.json:
                _print_json(
                    None,
                    None,
                    timeout,
                    [],
                    pretty=args.pretty,
                    error=config_error,
                    next_steps=next_steps,
                )
            else:
                _print_error(config_error, next_steps)
            return 2

        if config_path is None:
            reason = "未发现 MCP 配置文件（.cursor/mcp.json 或 ~/.cursor/mcp.json）"
            next_steps = [
                "复制 configs/mcp/.mcp.json.example 到 .cursor/mcp.json 或 ~/.cursor/mcp.json",
                "或使用 --gateway-url / GATEWAY_URL 直接指定 Gateway 地址",
                "参考 docs/gateway/02_mcp_integration_cursor.md",
            ]
            if args.json:
                _print_json(
                    None,
                    None,
                    timeout,
                    [],
                    pretty=args.pretty,
                    skipped=True,
                    skip_reason=reason,
                    next_steps=next_steps,
                )
            else:
                _print_skip(reason, next_steps)
            return 0

        config, config_parse_error = _load_mcp_config(config_path)
        if config_parse_error:
            next_steps = [
                "确认配置文件为有效 JSON",
                "参考 schemas/cursor_mcp_config_template_v2.schema.json",
            ]
            if args.json:
                _print_json(
                    None,
                    None,
                    timeout,
                    [],
                    pretty=args.pretty,
                    config_path=config_path,
                    error=config_parse_error,
                    next_steps=next_steps,
                )
            else:
                _print_error(config_parse_error, next_steps, config_path=config_path)
            return 2

        target, target_error, skip_reason = _resolve_mcp_target(
            config,
            config_path=config_path,
            server_name=args.server,
        )
        if skip_reason:
            next_steps = [
                "在 mcpServers 中添加 server 配置（默认 server: engram）",
                "参考 configs/mcp/.mcp.json.example",
            ]
            if args.json:
                _print_json(
                    None,
                    None,
                    timeout,
                    [],
                    pretty=args.pretty,
                    config_path=config_path,
                    skipped=True,
                    skip_reason=skip_reason,
                    next_steps=next_steps,
                )
            else:
                _print_skip(skip_reason, next_steps, config_path=config_path)
            return 0
        if target_error:
            if "多个 MCP server" in target_error:
                next_steps = [
                    "使用 --server 指定 mcpServers 中的 server 名称",
                    "或仅保留一个 server 配置",
                ]
            elif "未找到 server" in target_error:
                next_steps = [
                    "确认 mcpServers 中包含该 server 名称",
                    "使用 --server 指定正确的 server",
                ]
            else:
                next_steps = [
                    "检查 mcpServers 配置是否包含 type 与 url",
                    "参考 schemas/cursor_mcp_config_template_v2.schema.json",
                ]
            if args.json:
                _print_json(
                    None,
                    None,
                    timeout,
                    [],
                    pretty=args.pretty,
                    config_path=config_path,
                    error=target_error,
                    next_steps=next_steps,
                )
            else:
                _print_error(target_error, next_steps, config_path=config_path)
            return 2

        server_name = target.server_name
        mcp_url = target.mcp_url
        config_headers = target.headers
        gateway_url = _derive_gateway_url(mcp_url)

    if gateway_url is not None:
        mcp_url = explicit_mcp_url or _build_mcp_url(gateway_url)

    request_headers = _prepare_request_headers(
        custom_headers, config_headers=config_headers
    )

    tools_url = mcp_url or gateway_url
    if tools_url is None:
        reason = "未解析到可用的 MCP URL"
        next_steps = [
            "检查 --gateway-url / GATEWAY_URL 是否配置",
            "或确保 MCP 配置包含有效的 mcpServers.url",
        ]
        if args.json:
            _print_json(
                gateway_url,
                mcp_url,
                timeout,
                [],
                pretty=args.pretty,
                config_path=config_path,
                server_name=server_name,
                error=reason,
                next_steps=next_steps,
            )
        else:
            _print_error(reason, next_steps, config_path=config_path)
        return 2

    checks = [
        *([_check_health(gateway_url, timeout)] if gateway_url else []),
        *(
            [
                _check_options(
                    gateway_url,
                    timeout,
                    request_headers=request_headers,
                )
            ]
            if gateway_url
            else []
        ),
        _check_initialize(tools_url, timeout, request_headers=request_headers),
        _check_ping(tools_url, timeout, request_headers=request_headers),
        _check_tools_list(tools_url, timeout, request_headers=request_headers),
        _check_correlation_id_uniqueness(
            tools_url, timeout, request_headers=request_headers
        ),
        _check_unknown_method_error(
            tools_url, timeout, request_headers=request_headers
        ),
    ]

    if args.json:
        _print_json(
            gateway_url,
            mcp_url,
            timeout,
            checks,
            pretty=args.pretty,
            config_path=config_path,
            server_name=server_name,
        )
    else:
        if config_path:
            print(f"MCP 配置: {config_path}")
        if server_name:
            print(f"MCP Server: {server_name}")
        if mcp_url:
            print(f"MCP URL: {mcp_url}")
        if gateway_url:
            print(f"Gateway URL: {gateway_url}")
        print(f"Timeout: {timeout}s")
        for check in checks:
            _print_result(check)

    failed = [c for c in checks if not c.passed]
    if failed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
