#!/usr/bin/env python3
"""
stack_doctor - 原生全栈诊断（Gateway + OpenMemory）

动机：
- `make mcp-doctor` 只验证 Gateway MCP 端点与协议契约，不依赖 OpenMemory
- 当 OpenMemory 未启动时，Gateway 仍可 /health 与 tools/list 正常，但 memory_store 可能降级为 deferred（写入 outbox）

检查项：
- OpenMemory: GET /health
- Gateway: GET /health
- Gateway MCP: tools/call(memory_store) 写入验证（要求返回 memory_id，避免 deferred 误报）

环境变量：
- GATEWAY_URL: Gateway 基础 URL（默认 http://127.0.0.1:8787）
- OPENMEMORY_URL: OpenMemory 基础 URL（可选；默认从 OPENMEMORY_BASE_URL 或 http://127.0.0.1:8080）
- OPENMEMORY_BASE_URL: 备用 OpenMemory URL（与 Gateway 一致）
- STACK_DOCTOR_TIMEOUT: 超时秒数（默认 5）
- MCP_DOCTOR_AUTHORIZATION: 可选 Authorization header（例如 "Bearer xxx"）
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from urllib import error, request

DEFAULT_GATEWAY_URL = "http://127.0.0.1:8787"
DEFAULT_OPENMEMORY_URL = "http://127.0.0.1:8080"
DEFAULT_TIMEOUT_SECONDS = 5.0
RESPONSE_PREVIEW_LIMIT = 300
DEFAULT_FULL_MODE = ("1", "true", "yes", "on")

# tools/list 期望的工具集合（用于 full 模式）
EXPECTED_TOOL_NAMES = {
    "memory_store",
    "memory_query",
    "reliability_report",
    "governance_update",
    "evidence_upload",
    "evidence_read",
    "artifacts_put",
    "artifacts_get",
    "artifacts_exists",
    "logbook_create_item",
    "logbook_add_event",
    "logbook_attach",
    "logbook_set_kv",
    "logbook_get_kv",
    "logbook_query_items",
    "logbook_query_events",
    "logbook_list_attachments",
    "scm_patch_blob_resolve",
    "scm_materialize_patch_blob",
}


@dataclass
class CheckResult:
    name: str
    passed: bool
    message: str
    status_code: Optional[int] = None
    details: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "message": self.message,
            "status_code": self.status_code,
            "details": self.details,
        }


def _get_timeout() -> float:
    raw = os.environ.get("STACK_DOCTOR_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS


def _preview_body(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return "<non-utf8>"
    if len(text) > RESPONSE_PREVIEW_LIMIT:
        return text[:RESPONSE_PREVIEW_LIMIT] + "...(truncated)"
    return text


def _request(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    timeout: float,
) -> Tuple[Optional[int], bytes, Dict[str, str], Optional[str]]:
    req = request.Request(url=url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            resp_body = resp.read() or b""
            resp_headers = {k: v for k, v in resp.headers.items()}
            return status, resp_body, resp_headers, None
    except error.HTTPError as exc:
        try:
            resp_body = exc.read() or b""
        except Exception:
            resp_body = b""
        resp_headers = {k: v for k, v in getattr(exc, "headers", {}).items()} if exc.headers else {}
        return exc.code, resp_body, resp_headers, f"http_error: {exc}"
    except Exception as exc:
        return None, b"", {}, f"request_error: {exc}"


def _get_gateway_url(explicit: Optional[str]) -> str:
    url = (explicit or os.environ.get("GATEWAY_URL") or DEFAULT_GATEWAY_URL).strip()
    return url.rstrip("/")


def _get_openmemory_url() -> str:
    url = os.environ.get("OPENMEMORY_URL") or os.environ.get("OPENMEMORY_BASE_URL") or DEFAULT_OPENMEMORY_URL
    return url.strip().rstrip("/")


def _build_mcp_url(gateway_url: str) -> str:
    return f"{gateway_url}/mcp"


def _build_mcp_headers() -> Dict[str, str]:
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    # 合约必需 header（占位值；如你有鉴权可用 MCP_DOCTOR_AUTHORIZATION 覆盖）
    headers.setdefault("Mcp-Session-Id", "stack-doctor-session")
    auth = os.environ.get("MCP_DOCTOR_AUTHORIZATION", "").strip()
    headers.setdefault("Authorization", auth or "Bearer stack-doctor")
    return headers


def _jsonrpc_request(
    *,
    mcp_url: str,
    method: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: float,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params or {},
        }
    ).encode("utf-8")
    status, raw, _, err = _request(
        "POST",
        mcp_url,
        headers=_build_mcp_headers(),
        body=body,
        timeout=timeout,
    )
    preview = _preview_body(raw)
    if err:
        return None, err
    if status != 200:
        return None, f"状态码异常: {status}, resp={preview}"
    try:
        parsed = json.loads(raw.decode("utf-8", errors="replace") or "{}")
    except Exception:
        return None, f"响应 JSON 解析失败: {preview}"
    if parsed.get("error") is not None:
        return None, f"JSON-RPC 返回 error: {parsed.get('error')}"
    return parsed, None


def _parse_tool_result(parsed: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    content = (parsed.get("result") or {}).get("content") or []
    if not isinstance(content, list) or not content:
        return None, "result.content 缺失或为空"
    first = content[0]
    text = first.get("text") if isinstance(first, dict) else None
    if not isinstance(text, str) or not text.strip():
        return None, "content[0].text 缺失或为空"
    try:
        return json.loads(text), None
    except Exception:
        return None, f"工具输出不是 JSON 对象: {text[:RESPONSE_PREVIEW_LIMIT]}"


def _check_openmemory_health(openmemory_url: str, timeout: float) -> CheckResult:
    status, body, _, err = _request("GET", f"{openmemory_url}/health", timeout=timeout)
    preview = _preview_body(body)
    if err:
        return CheckResult("OpenMemory GET /health", False, "请求失败", status_code=status, details=err)
    if status != 200:
        return CheckResult(
            "OpenMemory GET /health",
            False,
            f"状态码异常: {status}",
            status_code=status,
            details=preview,
        )
    try:
        parsed = json.loads(body.decode("utf-8", errors="replace") or "{}")
    except Exception:
        parsed = {}
    # OpenMemory 约定返回 {"status": "ok"}（兼容 ok/healthy 字段）
    status_val = str(parsed.get("status", "")).lower()
    ok = status_val == "ok" or bool(parsed.get("ok")) or bool(parsed.get("healthy"))
    if not ok:
        return CheckResult(
            "OpenMemory GET /health",
            False,
            "响应不符合预期（缺少 status=ok）",
            status_code=status,
            details=preview,
        )
    return CheckResult("OpenMemory GET /health", True, "OK", status_code=status)


def _check_gateway_health(gateway_url: str, timeout: float) -> CheckResult:
    status, body, _, err = _request("GET", f"{gateway_url}/health", timeout=timeout)
    preview = _preview_body(body)
    if err:
        return CheckResult("Gateway GET /health", False, "请求失败", status_code=status, details=err)
    if status != 200:
        return CheckResult(
            "Gateway GET /health",
            False,
            f"状态码异常: {status}",
            status_code=status,
            details=preview,
        )
    return CheckResult("Gateway GET /health", True, "OK", status_code=status)


def _check_mcp_memory_store(gateway_url: str, timeout: float) -> CheckResult:
    mcp_url = _build_mcp_url(gateway_url)
    headers = _build_mcp_headers()

    token = f"{int(time.time())}-{random.randrange(1_000_000, 9_999_999)}"
    payload_md = f"[stack-doctor] ping {token}"
    params = {
        "name": "memory_store",
        "arguments": {
            "payload_md": payload_md,
            "target_space": "private:stack-doctor",
            "meta_json": {"source": "stack-doctor", "token": token},
        },
    }
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}).encode(
        "utf-8"
    )
    status, raw, resp_headers, err = _request(
        "POST",
        mcp_url,
        headers=headers,
        body=body,
        timeout=timeout,
    )
    preview = _preview_body(raw)
    if err:
        return CheckResult("MCP tools/call(memory_store)", False, "请求失败", status_code=status, details=err)
    if status != 200:
        hint = "（鉴权失败）" if status in (401, 403) else ""
        return CheckResult(
            "MCP tools/call(memory_store)",
            False,
            f"状态码异常: {status}{hint}",
            status_code=status,
            details=preview,
        )
    try:
        parsed = json.loads(raw.decode("utf-8", errors="replace") or "{}")
    except Exception:
        return CheckResult(
            "MCP tools/call(memory_store)",
            False,
            "响应 JSON 解析失败",
            status_code=status,
            details=preview,
        )

    if parsed.get("error") is not None:
        return CheckResult(
            "MCP tools/call(memory_store)",
            False,
            "JSON-RPC 返回 error",
            status_code=status,
            details=_preview_body(json.dumps(parsed.get("error"), ensure_ascii=False).encode("utf-8")),
        )

    result = parsed.get("result") or {}
    content = result.get("content") or []
    if not isinstance(content, list) or not content:
        return CheckResult(
            "MCP tools/call(memory_store)",
            False,
            "result.content 缺失或为空",
            status_code=status,
            details=preview,
        )
    first = content[0]
    text = first.get("text") if isinstance(first, dict) else None
    if not isinstance(text, str) or not text.strip():
        return CheckResult(
            "MCP tools/call(memory_store)",
            False,
            "content[0].text 缺失或为空",
            status_code=status,
            details=preview,
        )
    try:
        tool_result = json.loads(text)
    except Exception:
        tool_result = None

    if not isinstance(tool_result, dict):
        return CheckResult(
            "MCP tools/call(memory_store)",
            False,
            "工具输出不是 JSON 对象",
            status_code=status,
            details=text[:RESPONSE_PREVIEW_LIMIT],
        )

    ok = bool(tool_result.get("ok"))
    action = str(tool_result.get("action") or "")
    memory_id = tool_result.get("memory_id")
    correlation_id = tool_result.get("correlation_id") or resp_headers.get("X-Correlation-ID")

    if not ok:
        return CheckResult(
            "MCP tools/call(memory_store)",
            False,
            "memory_store 返回失败",
            status_code=status,
            details=f"action={action}, correlation_id={correlation_id}, resp={tool_result}",
        )

    # 当 OpenMemory 不可用时，Gateway 可能降级为 deferred（写入 outbox），此时 memory_id 为空
    if action == "deferred" or not memory_id:
        outbox_id = tool_result.get("outbox_id")
        msg = "未获得 memory_id（可能 OpenMemory 不可用，已降级写入 outbox）"
        return CheckResult(
            "MCP tools/call(memory_store)",
            False,
            msg,
            status_code=status,
            details=f"action={action}, outbox_id={outbox_id}, correlation_id={correlation_id}",
        )

    return CheckResult(
        "MCP tools/call(memory_store)",
        True,
        f"OK (action={action}, memory_id={memory_id}, correlation_id={correlation_id})",
        status_code=status,
    )


def _check_mcp_tools_list(gateway_url: str, timeout: float) -> CheckResult:
    mcp_url = _build_mcp_url(gateway_url)
    parsed, err = _jsonrpc_request(mcp_url=mcp_url, method="tools/list", timeout=timeout)
    if err:
        return CheckResult("MCP tools/list", False, "请求失败", details=err)
    tools = (parsed.get("result") or {}).get("tools") or []
    if not isinstance(tools, list):
        return CheckResult("MCP tools/list", False, "tools 结构非法", details=_preview_body(json.dumps(parsed).encode()))
    tool_names = {tool.get("name") for tool in tools if isinstance(tool, dict)}
    missing = sorted(name for name in EXPECTED_TOOL_NAMES if name not in tool_names)
    if missing:
        return CheckResult(
            "MCP tools/list",
            False,
            "工具列表缺失",
            details=f"missing={missing}",
        )
    return CheckResult("MCP tools/list", True, "OK")


def _call_tool(
    gateway_url: str,
    *,
    timeout: float,
    name: str,
    arguments: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    mcp_url = _build_mcp_url(gateway_url)
    parsed, err = _jsonrpc_request(
        mcp_url=mcp_url,
        method="tools/call",
        params={"name": name, "arguments": arguments},
        timeout=timeout,
    )
    if err or not parsed:
        return None, err or "请求失败"
    return _parse_tool_result(parsed)


def _check_mcp_evidence_flow(gateway_url: str, timeout: float) -> CheckResult:
    token = f"stack-doctor-{int(time.time())}-{random.randrange(1000, 9999)}"
    result, err = _call_tool(
        gateway_url,
        timeout=timeout,
        name="evidence_upload",
        arguments={
            "content": f"[{token}] evidence payload",
            "content_type": "text/plain",
            "title": "stack-doctor",
        },
    )
    if err or not result or not result.get("ok"):
        return CheckResult("MCP evidence_upload/read", False, "evidence_upload 失败", details=err or str(result))
    evidence = result.get("evidence") or {}
    uri = evidence.get("uri")
    read_result, read_err = _call_tool(
        gateway_url,
        timeout=timeout,
        name="evidence_read",
        arguments={"uri": uri, "encoding": "utf-8"},
    )
    if read_err or not read_result or not read_result.get("ok"):
        return CheckResult("MCP evidence_upload/read", False, "evidence_read 失败", details=read_err or str(read_result))
    return CheckResult("MCP evidence_upload/read", True, "OK")


def _check_mcp_artifacts_flow(gateway_url: str, timeout: float) -> CheckResult:
    token = f"stack-doctor-{int(time.time())}-{random.randrange(1000, 9999)}"
    content = f"artifact:{token}"
    put_result, put_err = _call_tool(
        gateway_url,
        timeout=timeout,
        name="artifacts_put",
        arguments={"uri": f"diagnostics/stack-doctor/{token}.txt", "content": content, "encoding": "utf-8"},
    )
    if put_err or not put_result or not put_result.get("ok"):
        return CheckResult("MCP artifacts_put/get/exists", False, "artifacts_put 失败", details=put_err or str(put_result))
    uri = put_result.get("uri")
    exists_result, exists_err = _call_tool(
        gateway_url,
        timeout=timeout,
        name="artifacts_exists",
        arguments={"uri": uri},
    )
    if exists_err or not exists_result or not exists_result.get("ok") or not exists_result.get("exists"):
        return CheckResult(
            "MCP artifacts_put/get/exists",
            False,
            "artifacts_exists 失败",
            details=exists_err or str(exists_result),
        )
    get_result, get_err = _call_tool(
        gateway_url,
        timeout=timeout,
        name="artifacts_get",
        arguments={"uri": uri, "encoding": "utf-8"},
    )
    if get_err or not get_result or not get_result.get("ok"):
        return CheckResult("MCP artifacts_put/get/exists", False, "artifacts_get 失败", details=get_err or str(get_result))
    if get_result.get("content_text") != content:
        return CheckResult("MCP artifacts_put/get/exists", False, "制品内容不一致", details=str(get_result))
    return CheckResult("MCP artifacts_put/get/exists", True, "OK")


def _check_mcp_logbook_flow(gateway_url: str, timeout: float) -> CheckResult:
    token = f"stack-doctor-{int(time.time())}-{random.randrange(1000, 9999)}"
    item_result, item_err = _call_tool(
        gateway_url,
        timeout=timeout,
        name="logbook_create_item",
        arguments={"item_type": "task", "title": f"stack-doctor {token}"},
    )
    if item_err or not item_result or not item_result.get("ok"):
        return CheckResult("MCP logbook_*", False, "logbook_create_item 失败", details=item_err or str(item_result))
    item_id = item_result.get("item_id")

    event_result, event_err = _call_tool(
        gateway_url,
        timeout=timeout,
        name="logbook_add_event",
        arguments={"item_id": item_id, "event_type": "status", "status_to": "done"},
    )
    if event_err or not event_result or not event_result.get("ok"):
        return CheckResult("MCP logbook_*", False, "logbook_add_event 失败", details=event_err or str(event_result))

    attach_content = f"logbook-attach:{token}"
    artifact_result, artifact_err = _call_tool(
        gateway_url,
        timeout=timeout,
        name="artifacts_put",
        arguments={
            "uri": f"diagnostics/stack-doctor/{token}-attach.txt",
            "content": attach_content,
            "encoding": "utf-8",
        },
    )
    if artifact_err or not artifact_result or not artifact_result.get("ok"):
        return CheckResult("MCP logbook_*", False, "artifacts_put 失败", details=artifact_err or str(artifact_result))

    attach_result, attach_err = _call_tool(
        gateway_url,
        timeout=timeout,
        name="logbook_attach",
        arguments={
            "item_id": item_id,
            "kind": "artifact",
            "uri": artifact_result.get("uri"),
            "sha256": artifact_result.get("sha256"),
            "size_bytes": artifact_result.get("size_bytes"),
        },
    )
    if attach_err or not attach_result or not attach_result.get("ok"):
        return CheckResult("MCP logbook_*", False, "logbook_attach 失败", details=attach_err or str(attach_result))

    kv_set, kv_err = _call_tool(
        gateway_url,
        timeout=timeout,
        name="logbook_set_kv",
        arguments={"namespace": "stack-doctor", "key": token, "value_json": {"item_id": item_id}},
    )
    if kv_err or not kv_set or not kv_set.get("ok"):
        return CheckResult("MCP logbook_*", False, "logbook_set_kv 失败", details=kv_err or str(kv_set))

    kv_get, kv_get_err = _call_tool(
        gateway_url,
        timeout=timeout,
        name="logbook_get_kv",
        arguments={"namespace": "stack-doctor", "key": token},
    )
    if kv_get_err or not kv_get or not kv_get.get("ok") or not kv_get.get("found"):
        return CheckResult("MCP logbook_*", False, "logbook_get_kv 失败", details=kv_get_err or str(kv_get))

    query_items, query_items_err = _call_tool(
        gateway_url,
        timeout=timeout,
        name="logbook_query_items",
        arguments={"item_type": "task", "limit": 5},
    )
    if query_items_err or not query_items or not query_items.get("ok"):
        return CheckResult("MCP logbook_*", False, "logbook_query_items 失败", details=query_items_err or str(query_items))

    query_events, query_events_err = _call_tool(
        gateway_url,
        timeout=timeout,
        name="logbook_query_events",
        arguments={"item_id": item_id, "limit": 5},
    )
    if query_events_err or not query_events or not query_events.get("ok"):
        return CheckResult("MCP logbook_*", False, "logbook_query_events 失败", details=query_events_err or str(query_events))

    list_attachments, list_err = _call_tool(
        gateway_url,
        timeout=timeout,
        name="logbook_list_attachments",
        arguments={"item_id": item_id, "limit": 5},
    )
    if list_err or not list_attachments or not list_attachments.get("ok"):
        return CheckResult(
            "MCP logbook_*",
            False,
            "logbook_list_attachments 失败",
            details=list_err or str(list_attachments),
        )

    return CheckResult("MCP logbook_*", True, "OK")


def _print_human(results: list[CheckResult]) -> None:
    print("========== 全栈诊断 (stack-doctor) ==========")
    for r in results:
        prefix = "✓" if r.passed else "✗"
        print(f"{prefix} {r.name}: {r.message}")
        if not r.passed and r.details:
            print(f"    details: {r.details}")
    passed = all(r.passed for r in results)
    print("")
    print("结论:", "通过" if passed else "失败")


def main() -> int:
    parser = argparse.ArgumentParser(description="Engram 原生全栈诊断（Gateway + OpenMemory）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 结果（适合自动化）")
    parser.add_argument("--pretty", action="store_true", help="JSON 输出缩进格式")
    parser.add_argument("--gateway-url", help="覆盖 GATEWAY_URL（例如 http://127.0.0.1:8787）")
    parser.add_argument("--full", action="store_true", help="执行 MCP 全功能诊断（会写入少量测试数据）")
    args = parser.parse_args()

    timeout = _get_timeout()
    gateway_url = _get_gateway_url(args.gateway_url)
    openmemory_url = _get_openmemory_url()

    results = [
        _check_openmemory_health(openmemory_url, timeout),
        _check_gateway_health(gateway_url, timeout),
        _check_mcp_memory_store(gateway_url, timeout),
    ]

    full_mode = args.full or os.environ.get("STACK_DOCTOR_FULL", "").lower() in DEFAULT_FULL_MODE
    if full_mode:
        results.extend(
            [
                _check_mcp_tools_list(gateway_url, timeout),
                _check_mcp_evidence_flow(gateway_url, timeout),
                _check_mcp_artifacts_flow(gateway_url, timeout),
                _check_mcp_logbook_flow(gateway_url, timeout),
            ]
        )

    if args.json:
        out = {
            "gateway_url": gateway_url,
            "openmemory_url": openmemory_url,
            "timeout": timeout,
            "full_mode": full_mode,
            "ok": all(r.passed for r in results),
            "checks": [r.to_dict() for r in results],
        }
        if args.pretty:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(out, ensure_ascii=False))
    else:
        _print_human(results)

    return 0 if all(r.passed for r in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())

