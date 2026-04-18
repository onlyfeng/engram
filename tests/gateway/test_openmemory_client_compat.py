import httpx

from engram.gateway.openmemory_client import OpenMemoryClient


def _response(method: str, url: str, status_code: int, payload: dict) -> httpx.Response:
    request = httpx.Request(method, url)
    return httpx.Response(status_code, json=payload, request=request)


def test_search_sends_k_compat_field_for_openmemory_1_3(monkeypatch):
    client = OpenMemoryClient(base_url="http://openmemory.test")
    calls: list[tuple[str, dict]] = []

    def fake_post(url, payload, retry_config=None):  # noqa: ARG001
        calls.append((url, payload))
        return _response("POST", url, 200, {"matches": [{"id": "m-1", "content": "hello"}]})

    monkeypatch.setattr(client, "_post_with_retry", fake_post)

    result = client.search("hello", user_id="u-1", limit=5, filters={"kind": "FACT"})

    assert result.success is True
    assert result.results == [{"id": "m-1", "content": "hello"}]
    assert calls[0][1]["k"] == 5
    assert calls[0][1]["limit"] == 5
    assert calls[0][1]["filters"]["user_id"] == "u-1"


def test_list_memories_parses_items_and_filters_space(monkeypatch):
    client = OpenMemoryClient(base_url="http://openmemory.test")
    calls: list[tuple[str, dict]] = []

    class FakeHttpxClient:
        def __init__(self, *args, **kwargs):  # noqa: D401, ANN002, ANN003
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN201
            return False

        def get(self, url, params=None, headers=None):  # noqa: ANN001
            calls.append((url, params or {}))
            return _response(
                "GET",
                url,
                200,
                {
                    "items": [
                        {
                            "id": "m-keep",
                            "content": "keep",
                            "metadata": {"target_space": "team:demo"},
                        },
                        {
                            "id": "m-drop",
                            "content": "drop",
                            "metadata": {"target_space": "team:other"},
                        },
                    ]
                },
            )

    monkeypatch.setattr("engram.gateway.openmemory_client.httpx.Client", FakeHttpxClient)

    result = client.list_memories(limit=7, offset=0, space="team:demo")

    assert result.success is True
    assert [memory["id"] for memory in result.memories or []] == ["m-keep"]
    assert result.total == 1
    assert calls[0][1]["limit"] == 100
    assert calls[0][1]["offset"] == 0
    assert calls[0][1]["l"] == 100
    assert calls[0][1]["u"] == 0


def test_list_memories_space_filter_keeps_pagination_semantics(monkeypatch):
    client = OpenMemoryClient(base_url="http://openmemory.test")
    calls: list[tuple[str, dict]] = []
    first_page = [
        {
            "id": "m-first",
            "content": "first",
            "metadata": {"target_space": "team:demo"},
        }
    ] + [
        {
            "id": f"m-other-{idx}",
            "content": "other",
            "metadata": {"target_space": "team:other"},
        }
        for idx in range(99)
    ]

    class FakeHttpxClient:
        def __init__(self, *args, **kwargs):  # noqa: D401, ANN002, ANN003
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN201
            return False

        def get(self, url, params=None, headers=None):  # noqa: ANN001
            query = params or {}
            calls.append((url, query))
            if query.get("u") == 0:
                payload = {"items": first_page}
            elif query.get("u") == 100:
                payload = {
                    "items": [
                        {
                            "id": "m-second",
                            "content": "second",
                            "metadata": {"target_space": "team:demo"},
                        }
                    ]
                }
            else:
                raise AssertionError(f"unexpected query params: {query}")
            return _response("GET", url, 200, payload)

    monkeypatch.setattr("engram.gateway.openmemory_client.httpx.Client", FakeHttpxClient)

    result = client.list_memories(limit=1, offset=1, space="team:demo")

    assert result.success is True
    assert [memory["id"] for memory in result.memories or []] == ["m-second"]
    assert result.total == 2
    assert [query["u"] for _, query in calls] == [0, 100]


def test_reinforce_falls_back_to_legacy_payload(monkeypatch):
    client = OpenMemoryClient(base_url="http://openmemory.test")
    payloads: list[dict] = []

    def fake_post(url, payload, retry_config=None):  # noqa: ARG001
        payloads.append(payload)
        if "memory_id" in payload:
            response = _response("POST", url, 422, {"error": "strict schema"})
            raise httpx.HTTPStatusError("422", request=response.request, response=response)
        return _response("POST", url, 200, {"ok": True})

    monkeypatch.setattr(client, "_post_with_retry", fake_post)

    result = client.reinforce("m-1", delta=2.5, reason="useful")

    assert result.success is True
    assert result.memory_id == "m-1"
    assert result.new_strength is None
    assert any(payload.get("id") == "m-1" and payload.get("boost") == 2.5 for payload in payloads)


def test_wipe_user_uses_delete_users_memories_route(monkeypatch):
    client = OpenMemoryClient(base_url="http://openmemory.test")
    delete_calls: list[str] = []

    class FakeHttpxClient:
        def __init__(self, *args, **kwargs):  # noqa: D401, ANN002, ANN003
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN201
            return False

        def delete(self, url, params=None, headers=None):  # noqa: ANN001
            delete_calls.append(url)
            return _response("DELETE", url, 200, {"ok": True, "deleted": 3})

    monkeypatch.setattr("engram.gateway.openmemory_client.httpx.Client", FakeHttpxClient)

    result = client.wipe(confirm=True, user_id="u-1")

    assert result.success is True
    assert result.deleted_count == 3
    assert any(url.endswith("/users/u-1/memories") for url in delete_calls)


def test_wipe_user_falls_back_to_iterative_delete_without_global_wipe(monkeypatch):
    client = OpenMemoryClient(base_url="http://openmemory.test")
    calls: list[tuple[str, str, dict | None]] = []
    deleted_ids: set[str] = set()

    class FakeHttpxClient:
        def __init__(self, *args, **kwargs):  # noqa: D401, ANN002, ANN003
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN201
            return False

        def delete(self, url, params=None, headers=None):  # noqa: ANN001
            query = params or {}
            calls.append(("DELETE", url, query))
            if url.endswith("/users/u-1/memories"):
                return _response("DELETE", url, 404, {"error": "not_found"})
            if url.endswith("/memory/m-1"):
                assert query == {"user_id": "u-1"}
                deleted_ids.add("m-1")
                return _response("DELETE", url, 200, {"ok": True})
            raise AssertionError(f"unexpected DELETE {url} params={query}")

        def get(self, url, params=None, headers=None):  # noqa: ANN001
            query = params or {}
            calls.append(("GET", url, query))
            if url.endswith("/memory/all"):
                assert query["user_id"] == "u-1"
                if query.get("u") == 0 and "m-1" not in deleted_ids:
                    payload = {"items": [{"id": "m-1", "content": "hello", "user_id": "u-1"}]}
                else:
                    payload = {"items": []}
                return _response(
                    "GET",
                    url,
                    200,
                    payload,
                )
            raise AssertionError(f"unexpected GET {url} params={query}")

        def post(self, url, json=None, headers=None, timeout=None):  # noqa: ANN001
            raise AssertionError(
                f"user-scoped wipe must not call global POST wipe endpoints: {url}"
            )

    monkeypatch.setattr("engram.gateway.openmemory_client.httpx.Client", FakeHttpxClient)

    result = client.wipe(confirm=True, user_id="u-1")

    assert result.success is True
    assert result.deleted_count == 1
    assert all("/admin/wipe" not in url and "/memory/wipe" not in url for _, url, _ in calls)


def test_health_check_accepts_ok_boolean_payload(monkeypatch):
    client = OpenMemoryClient(base_url="http://openmemory.test")

    class FakeHttpxClient:
        def __init__(self, *args, **kwargs):  # noqa: D401, ANN002, ANN003
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN201
            return False

        def get(self, url, params=None, headers=None):  # noqa: ANN001
            return _response("GET", url, 200, {"ok": True, "version": "2.0-hsg-tiered"})

    monkeypatch.setattr("engram.gateway.openmemory_client.httpx.Client", FakeHttpxClient)

    assert client.health_check() is True


def test_wipe_falls_back_to_iterative_delete_when_no_wipe_endpoint(monkeypatch):
    client = OpenMemoryClient(base_url="http://openmemory.test")
    calls: list[tuple[str, str, dict | None]] = []
    deleted_ids: set[str] = set()

    class FakeHttpxClient:
        def __init__(self, *args, **kwargs):  # noqa: D401, ANN002, ANN003
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN201
            return False

        def post(self, url, json=None, headers=None, timeout=None):  # noqa: ANN001
            calls.append(("POST", url, json))
            return _response("POST", url, 404, {"error": "not_found"})

        def delete(self, url, params=None, headers=None):  # noqa: ANN001
            calls.append(("DELETE", url, params))
            if url.endswith("/memory/all"):
                return _response("DELETE", url, 405, {"error": "method_not_allowed"})
            if url.endswith("/memory/m-1"):
                deleted_ids.add("m-1")
            if url.endswith("/memory/m-2"):
                deleted_ids.add("m-2")
            return _response("DELETE", url, 200, {"ok": True})

        def get(self, url, params=None, headers=None):  # noqa: ANN001
            calls.append(("GET", url, params))
            if url.endswith("/memory/all"):
                query = params or {}
                if query.get("u") == 0:
                    items = []
                    if "m-1" not in deleted_ids:
                        items.append({"id": "m-1", "content": "hello"})
                    if "m-2" not in deleted_ids:
                        items.append({"id": "m-2", "content": "world"})
                    payload = {"items": items}
                else:
                    payload = {"items": []}
                return _response(
                    "GET",
                    url,
                    200,
                    payload,
                )
            raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr("engram.gateway.openmemory_client.httpx.Client", FakeHttpxClient)

    result = client.wipe(confirm=True)

    assert result.success is True
    assert result.deleted_count == 2
    assert any(method == "DELETE" and url.endswith("/memory/m-1") for method, url, _ in calls)
    assert any(method == "DELETE" and url.endswith("/memory/m-2") for method, url, _ in calls)
