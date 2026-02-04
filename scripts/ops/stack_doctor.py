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
    args = parser.parse_args()

    timeout = _get_timeout()
    gateway_url = _get_gateway_url(args.gateway_url)
    openmemory_url = _get_openmemory_url()

    results = [
        _check_openmemory_health(openmemory_url, timeout),
        _check_gateway_health(gateway_url, timeout),
        _check_mcp_memory_store(gateway_url, timeout),
    ]

    if args.json:
        out = {
            "gateway_url": gateway_url,
            "openmemory_url": openmemory_url,
            "timeout": timeout,
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

