"""
logbook_tools - logbook_* 工具实现

提供：
- logbook_create_item
- logbook_add_event
- logbook_attach
- logbook_set_kv / logbook_get_kv
- logbook_query_items / logbook_query_events / logbook_list_attachments
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from ..di import GatewayDepsProtocol
from ..result_error_codes import ToolResultErrorCode


def _iso(dt: Any) -> Any:
    if isinstance(dt, datetime):
        return dt.isoformat()
    return dt


def _normalize_rows(rows: List[Dict[str, Any]], time_fields: List[str]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        for field in time_fields:
            if field in item:
                item[field] = _iso(item[field])
        normalized.append(item)
    return normalized


async def execute_logbook_create_item(
    item_type: Optional[str],
    title: Optional[str],
    status: Optional[str] = None,
    owner_user_id: Optional[str] = None,
    scope_json: Optional[Dict[str, Any]] = None,
    project_key: Optional[str] = None,
    *,
    deps: GatewayDepsProtocol,
) -> Dict[str, Any]:
    if not item_type:
        return {
            "ok": False,
            "error_code": ToolResultErrorCode.MISSING_REQUIRED_PARAMETER,
            "retryable": False,
            "message": "缺少必需参数: item_type",
        }
    if not title:
        return {
            "ok": False,
            "error_code": ToolResultErrorCode.MISSING_REQUIRED_PARAMETER,
            "retryable": False,
            "message": "缺少必需参数: title",
        }

    # 确保 owner_user_id 对应的用户存在（避免 items.owner_user_id 外键约束违反）
    if owner_user_id:
        deps.logbook_adapter.ensure_user(user_id=owner_user_id, display_name=owner_user_id)

    item_id = deps.logbook_adapter.create_item(
        item_type=item_type,
        title=title,
        status=status or "open",
        owner_user_id=owner_user_id,
        scope_json=scope_json,
        project_key=project_key,
    )
    return {
        "ok": True,
        "item_id": item_id,
        "item_type": item_type,
        "title": title,
        "status": status or "open",
    }


async def execute_logbook_add_event(
    item_id: Optional[int],
    event_type: Optional[str],
    payload_json: Optional[Dict[str, Any]] = None,
    status_from: Optional[str] = None,
    status_to: Optional[str] = None,
    actor_user_id: Optional[str] = None,
    source: Optional[str] = None,
    *,
    deps: GatewayDepsProtocol,
) -> Dict[str, Any]:
    if item_id is None:
        return {
            "ok": False,
            "error_code": ToolResultErrorCode.MISSING_REQUIRED_PARAMETER,
            "retryable": False,
            "message": "缺少必需参数: item_id",
        }
    if not event_type:
        return {
            "ok": False,
            "error_code": ToolResultErrorCode.MISSING_REQUIRED_PARAMETER,
            "retryable": False,
            "message": "缺少必需参数: event_type",
        }

    # 确保 actor_user_id 对应的用户存在（避免 events.actor_user_id 外键约束违反）
    if actor_user_id:
        deps.logbook_adapter.ensure_user(user_id=actor_user_id, display_name=actor_user_id)

    event_id = deps.logbook_adapter.add_event(
        item_id=item_id,
        event_type=event_type,
        payload_json=payload_json,
        status_from=status_from,
        status_to=status_to,
        actor_user_id=actor_user_id,
        source=source or "tool",
    )
    return {
        "ok": True,
        "event_id": event_id,
        "item_id": item_id,
        "event_type": event_type,
        "status_to": status_to,
    }


async def execute_logbook_attach(
    item_id: Optional[int],
    kind: Optional[str],
    uri: Optional[str],
    sha256: Optional[str],
    size_bytes: Optional[int] = None,
    meta_json: Optional[Dict[str, Any]] = None,
    *,
    deps: GatewayDepsProtocol,
) -> Dict[str, Any]:
    if item_id is None:
        return {
            "ok": False,
            "error_code": ToolResultErrorCode.MISSING_REQUIRED_PARAMETER,
            "retryable": False,
            "message": "缺少必需参数: item_id",
        }
    if not kind:
        return {
            "ok": False,
            "error_code": ToolResultErrorCode.MISSING_REQUIRED_PARAMETER,
            "retryable": False,
            "message": "缺少必需参数: kind",
        }
    if not uri:
        return {
            "ok": False,
            "error_code": ToolResultErrorCode.MISSING_REQUIRED_PARAMETER,
            "retryable": False,
            "message": "缺少必需参数: uri",
        }
    if not sha256:
        return {
            "ok": False,
            "error_code": ToolResultErrorCode.MISSING_REQUIRED_PARAMETER,
            "retryable": False,
            "message": "缺少必需参数: sha256",
        }

    attachment_id = deps.logbook_adapter.attach(
        item_id=item_id,
        kind=kind,
        uri=uri,
        sha256=sha256,
        size_bytes=size_bytes,
        meta_json=meta_json,
    )
    return {
        "ok": True,
        "attachment_id": attachment_id,
        "item_id": item_id,
        "kind": kind,
        "uri": uri,
        "sha256": sha256,
    }


async def execute_logbook_set_kv(
    namespace: Optional[str],
    key: Optional[str],
    value_json: Any = None,
    *,
    deps: GatewayDepsProtocol,
) -> Dict[str, Any]:
    if not namespace:
        return {
            "ok": False,
            "error_code": ToolResultErrorCode.MISSING_REQUIRED_PARAMETER,
            "retryable": False,
            "message": "缺少必需参数: namespace",
        }
    if not key:
        return {
            "ok": False,
            "error_code": ToolResultErrorCode.MISSING_REQUIRED_PARAMETER,
            "retryable": False,
            "message": "缺少必需参数: key",
        }

    deps.logbook_adapter.set_kv(namespace=namespace, key=key, value_json=value_json)
    return {
        "ok": True,
        "namespace": namespace,
        "key": key,
    }


async def execute_logbook_get_kv(
    namespace: Optional[str],
    key: Optional[str],
    *,
    deps: GatewayDepsProtocol,
) -> Dict[str, Any]:
    if not namespace:
        return {
            "ok": False,
            "error_code": ToolResultErrorCode.MISSING_REQUIRED_PARAMETER,
            "retryable": False,
            "message": "缺少必需参数: namespace",
        }
    if not key:
        return {
            "ok": False,
            "error_code": ToolResultErrorCode.MISSING_REQUIRED_PARAMETER,
            "retryable": False,
            "message": "缺少必需参数: key",
        }

    value = deps.logbook_adapter.get_kv(namespace=namespace, key=key)
    return {
        "ok": True,
        "namespace": namespace,
        "key": key,
        "value_json": value,
        "found": value is not None,
    }


async def execute_logbook_query_items(
    limit: int = 50,
    item_type: Optional[str] = None,
    status: Optional[str] = None,
    owner_user_id: Optional[str] = None,
    *,
    deps: GatewayDepsProtocol,
) -> Dict[str, Any]:
    items = deps.logbook_adapter.query_items(
        limit=limit,
        item_type=item_type,
        status=status,
        owner_user_id=owner_user_id,
    )
    normalized = _normalize_rows(
        items,
        ["created_at", "updated_at", "latest_event_ts"],
    )
    return {
        "ok": True,
        "items": normalized,
        "count": len(normalized),
    }


async def execute_logbook_query_events(
    limit: int = 100,
    item_id: Optional[int] = None,
    event_type: Optional[str] = None,
    actor_user_id: Optional[str] = None,
    since: Optional[str] = None,
    *,
    deps: GatewayDepsProtocol,
) -> Dict[str, Any]:
    events = deps.logbook_adapter.query_events(
        limit=limit,
        item_id=item_id,
        event_type=event_type,
        actor_user_id=actor_user_id,
        since=since,
    )
    normalized = _normalize_rows(events, ["created_at"])
    return {
        "ok": True,
        "events": normalized,
        "count": len(normalized),
    }


async def execute_logbook_list_attachments(
    limit: int = 100,
    item_id: Optional[int] = None,
    kind: Optional[str] = None,
    *,
    deps: GatewayDepsProtocol,
) -> Dict[str, Any]:
    attachments = deps.logbook_adapter.list_attachments(
        limit=limit,
        item_id=item_id,
        kind=kind,
    )
    normalized = _normalize_rows(attachments, ["created_at"])
    return {
        "ok": True,
        "attachments": normalized,
        "count": len(normalized),
    }
