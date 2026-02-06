# -*- coding: utf-8 -*-
"""
MCP 全功能端到端验收测试（仅通过 /mcp JSON-RPC）

覆盖：
- tools/list
- governance_update
- evidence_upload / evidence_read
- memory_store / memory_query
- artifacts_put / artifacts_get / artifacts_exists
- logbook_create_item / logbook_add_event / logbook_attach / logbook_set_kv / logbook_get_kv
- logbook_query_items / logbook_query_events / logbook_list_attachments
- scm_patch_blob_resolve / scm_materialize_patch_blob（通过 DB 夹具 + stub）
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict

import psycopg
import pytest
from fastapi.testclient import TestClient

from engram.gateway.config import GatewayConfig, UnknownActorPolicy, override_config, reset_config
from engram.gateway.container import GatewayContainer, reset_container, set_container
from engram.gateway.logbook_adapter import LogbookAdapter
from engram.gateway.logbook_db import LogbookDatabase
from tests.gateway.fakes import FakeOpenMemoryClient

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


def _mcp_tools_list(client: TestClient) -> list[Dict[str, Any]]:
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload.get("error") is None
    return payload.get("result", {}).get("tools", [])


def _mcp_call(
    client: TestClient, name: str, arguments: Dict[str, Any], request_id: int = 1
) -> Dict[str, Any]:
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
            "id": request_id,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload.get("error") is None, payload.get("error")
    content = payload.get("result", {}).get("content", [])
    assert isinstance(content, list) and content, payload
    text = content[0].get("text", "")
    assert isinstance(text, str) and text.strip(), payload
    return json.loads(text)


@pytest.fixture(scope="function")
def gateway_client(migrated_db, tmp_path, monkeypatch):
    project_key = "test_project"
    admin_key = "test-admin-key"
    artifacts_root = tmp_path / "artifacts"
    artifacts_root.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("ENGRAM_ARTIFACTS_ROOT", str(artifacts_root))
    monkeypatch.setenv("PROJECT_KEY", project_key)
    monkeypatch.setenv("POSTGRES_DSN", migrated_db["dsn"])
    monkeypatch.setenv("OPENMEMORY_BASE_URL", "http://fake-openmemory")
    monkeypatch.setenv("GOVERNANCE_ADMIN_KEY", admin_key)

    config = GatewayConfig(
        project_key=project_key,
        postgres_dsn=migrated_db["dsn"],
        openmemory_base_url="http://fake-openmemory",
        governance_admin_key=admin_key,
        unknown_actor_policy=UnknownActorPolicy.AUTO_CREATE,
    )
    override_config(config)

    db = LogbookDatabase(dsn=migrated_db["dsn"])
    adapter = LogbookAdapter(dsn=migrated_db["dsn"])
    fake_openmemory = FakeOpenMemoryClient()

    test_container = GatewayContainer.create_for_testing(
        config=config,
        db=db,
        logbook_adapter=adapter,
        openmemory_client=fake_openmemory,
    )
    set_container(test_container)

    from engram.gateway.main import app

    try:
        with TestClient(app) as client:
            yield {
                "client": client,
                "openmemory": fake_openmemory,
                "project_key": project_key,
                "admin_key": admin_key,
                "dsn": migrated_db["dsn"],
            }
    finally:
        reset_container()
        reset_config()


@pytest.fixture(scope="function")
def stub_materialize_blob(monkeypatch):
    from engram.logbook.materialize_patch_blob import MaterializeResult, MaterializeStatus

    def _fake_materialize_blob(conn, record, config=None, **_kwargs):
        _ = conn
        _ = config
        return MaterializeResult(
            blob_id=record.blob_id,
            status=MaterializeStatus.MATERIALIZED,
            uri=record.uri or f"scm/{record.source_id}/{record.sha256}.diff",
            sha256=record.sha256,
            size_bytes=record.size_bytes or 0,
        )

    monkeypatch.setattr(
        "engram.logbook.materialize_patch_blob.materialize_blob",
        _fake_materialize_blob,
    )


def _seed_patch_blob(dsn: str, project_key: str) -> Dict[str, Any]:
    from engram.logbook.hashing import sha256 as compute_sha256
    from engram.logbook.scm_db import upsert_patch_blob

    diff_content = "diff --git a/foo.txt b/foo.txt\n+hello\n"
    sha = compute_sha256(diff_content.encode("utf-8"))
    source_type = "svn"
    source_id = "1:r1"
    uri = f"scm/{project_key}/1/svn/r1/{sha}.diff"
    size_bytes = len(diff_content.encode("utf-8"))

    with psycopg.connect(dsn, autocommit=True) as conn:
        blob_id = upsert_patch_blob(
            conn,
            source_type=source_type,
            source_id=source_id,
            sha256=sha,
            uri=uri,
            size_bytes=size_bytes,
            format="diff",
        )

    return {
        "blob_id": blob_id,
        "source_type": source_type,
        "source_id": source_id,
        "sha256": sha,
        "uri": uri,
        "size_bytes": size_bytes,
        "evidence_uri": f"memory://patch_blobs/{source_type}/{source_id}/{sha}",
    }


def test_mcp_full_workflow(gateway_client, stub_materialize_blob):
    client = gateway_client["client"]
    openmemory = gateway_client["openmemory"]
    project_key = gateway_client["project_key"]
    admin_key = gateway_client["admin_key"]
    dsn = gateway_client["dsn"]

    tools = _mcp_tools_list(client)
    tool_names = {tool.get("name") for tool in tools}
    assert EXPECTED_TOOL_NAMES.issubset(tool_names)

    governance_result = _mcp_call(
        client,
        "governance_update",
        {
            "team_write_enabled": True,
            "admin_key": admin_key,
            "actor_user_id": "admin",
        },
        request_id=2,
    )
    assert governance_result.get("ok") is True

    evidence_result = _mcp_call(
        client,
        "evidence_upload",
        {
            "content": "mcp-e2e evidence",
            "content_type": "text/plain",
            "title": "mcp-e2e",
            "actor_user_id": "alice",
            "project_key": project_key,
        },
        request_id=3,
    )
    assert evidence_result.get("ok") is True
    evidence_obj = evidence_result.get("evidence")
    assert isinstance(evidence_obj, dict)

    evidence_read = _mcp_call(
        client,
        "evidence_read",
        {"uri": evidence_obj.get("uri"), "encoding": "utf-8"},
        request_id=4,
    )
    assert evidence_read.get("ok") is True
    assert "content_text" in evidence_read

    memory_id = f"mem-{uuid.uuid4().hex[:8]}"
    openmemory.configure_store_success(memory_id=memory_id)

    memory_team = _mcp_call(
        client,
        "memory_store",
        {
            "payload_md": "team memory via mcp",
            "target_space": f"team:{project_key}",
            "kind": "PROCEDURE",
            "actor_user_id": "alice",
            "evidence": [evidence_obj],
        },
        request_id=5,
    )
    assert memory_team.get("ok") is True
    assert memory_team.get("memory_id") == memory_id
    assert str(memory_team.get("space_written", "")).startswith("team:")

    memory_private = _mcp_call(
        client,
        "memory_store",
        {"payload_md": "private memory via mcp", "target_space": "private:alice"},
        request_id=6,
    )
    assert memory_private.get("ok") is True
    assert str(memory_private.get("space_written", "")).startswith("private:")

    openmemory.configure_search_success(
        results=[
            {"id": memory_id, "content": "team memory via mcp", "space": f"team:{project_key}"}
        ]
    )
    memory_query = _mcp_call(
        client,
        "memory_query",
        {"query": "team memory", "spaces": [f"team:{project_key}"]},
        request_id=7,
    )
    assert memory_query.get("ok") is True
    assert memory_query.get("results")

    artifact_content = "artifact-from-mcp"
    artifacts_put = _mcp_call(
        client,
        "artifacts_put",
        {
            "uri": "mcp-e2e/artifacts/demo.txt",
            "content": artifact_content,
            "encoding": "utf-8",
        },
        request_id=8,
    )
    assert artifacts_put.get("ok") is True
    artifact_uri = artifacts_put.get("uri")
    artifact_sha = artifacts_put.get("sha256")

    artifacts_exists = _mcp_call(
        client,
        "artifacts_exists",
        {"uri": artifact_uri},
        request_id=9,
    )
    assert artifacts_exists.get("ok") is True
    assert artifacts_exists.get("exists") is True

    artifacts_get = _mcp_call(
        client,
        "artifacts_get",
        {"uri": artifact_uri, "encoding": "utf-8"},
        request_id=10,
    )
    assert artifacts_get.get("ok") is True
    assert artifacts_get.get("content_text") == artifact_content

    item = _mcp_call(
        client,
        "logbook_create_item",
        {"item_type": "task", "title": "mcp e2e item", "owner_user_id": "alice"},
        request_id=11,
    )
    assert item.get("ok") is True
    item_id = item.get("item_id")

    event = _mcp_call(
        client,
        "logbook_add_event",
        {"item_id": item_id, "event_type": "status", "status_to": "done", "actor_user_id": "alice"},
        request_id=12,
    )
    assert event.get("ok") is True

    attachment = _mcp_call(
        client,
        "logbook_attach",
        {
            "item_id": item_id,
            "kind": "artifact",
            "uri": artifact_uri,
            "sha256": artifact_sha,
            "size_bytes": len(artifact_content.encode("utf-8")),
        },
        request_id=13,
    )
    assert attachment.get("ok") is True

    _mcp_call(
        client,
        "logbook_set_kv",
        {"namespace": "mcp.e2e", "key": "latest", "value_json": {"item_id": item_id}},
        request_id=14,
    )
    kv = _mcp_call(
        client,
        "logbook_get_kv",
        {"namespace": "mcp.e2e", "key": "latest"},
        request_id=15,
    )
    assert kv.get("ok") is True
    assert kv.get("found") is True

    items = _mcp_call(
        client,
        "logbook_query_items",
        {"item_type": "task", "owner_user_id": "alice", "limit": 10},
        request_id=16,
    )
    assert items.get("ok") is True
    assert items.get("count", 0) >= 1

    events = _mcp_call(
        client,
        "logbook_query_events",
        {"item_id": item_id, "limit": 10},
        request_id=17,
    )
    assert events.get("ok") is True
    assert events.get("count", 0) >= 1

    attachments = _mcp_call(
        client,
        "logbook_list_attachments",
        {"item_id": item_id, "limit": 10},
        request_id=18,
    )
    assert attachments.get("ok") is True
    assert attachments.get("count", 0) >= 1

    patch_blob = _seed_patch_blob(dsn, project_key)

    scm_resolve = _mcp_call(
        client,
        "scm_patch_blob_resolve",
        {"evidence_uri": patch_blob["evidence_uri"]},
        request_id=19,
    )
    assert scm_resolve.get("ok") is True
    assert scm_resolve.get("sha256") == patch_blob["sha256"]

    scm_materialize = _mcp_call(
        client,
        "scm_materialize_patch_blob",
        {"evidence_uri": patch_blob["evidence_uri"]},
        request_id=20,
    )
    assert scm_materialize.get("ok") is True
    assert scm_materialize.get("status") in ("materialized", "skipped")
