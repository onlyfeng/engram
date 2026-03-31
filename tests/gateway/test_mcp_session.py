"""
MCP 会话管理模块测试

覆盖：
- 会话创建、查询、删除
- mark_initialized
- TTL 过期清理
- 全局单例访问器
"""

from __future__ import annotations

import pytest

from engram.gateway.mcp_session import (
    McpSessionStore,
    get_session_store,
    reset_session_store_for_testing,
)


class TestMcpSessionStore:
    @pytest.mark.asyncio
    async def test_create_and_get(self):
        store = McpSessionStore()
        session = await store.create_session()
        assert session.session_id
        assert session.initialized is False

        retrieved = await store.get_session(session.session_id)
        assert retrieved is not None
        assert retrieved.session_id == session.session_id

    @pytest.mark.asyncio
    async def test_get_nonexistent(self):
        store = McpSessionStore()
        assert await store.get_session("nonexistent") is None

    @pytest.mark.asyncio
    async def test_delete(self):
        store = McpSessionStore()
        session = await store.create_session()
        assert await store.delete_session(session.session_id) is True
        assert await store.get_session(session.session_id) is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self):
        store = McpSessionStore()
        assert await store.delete_session("nonexistent") is False

    @pytest.mark.asyncio
    async def test_mark_initialized(self):
        store = McpSessionStore()
        session = await store.create_session()
        assert session.initialized is False
        await store.mark_initialized(session.session_id)
        retrieved = await store.get_session(session.session_id)
        assert retrieved.initialized is True

    @pytest.mark.asyncio
    async def test_mark_initialized_nonexistent(self):
        store = McpSessionStore()
        # Should not raise
        await store.mark_initialized("nonexistent")

    @pytest.mark.asyncio
    async def test_ttl_expiry(self):
        store = McpSessionStore(ttl=0.0)
        session = await store.create_session()
        # Session should be immediately expired
        assert await store.get_session(session.session_id) is None

    @pytest.mark.asyncio
    async def test_lazy_cleanup(self):
        store = McpSessionStore(ttl=0.0)
        s1 = await store.create_session()
        s2 = await store.create_session()
        # Both expired; creating a new session triggers lazy cleanup
        s3 = await store.create_session()
        assert await store.get_session(s1.session_id) is None
        assert await store.get_session(s2.session_id) is None
        # s3 is also expired (ttl=0.0) but was just created
        # With ttl=0.0, even s3 is expired
        assert await store.get_session(s3.session_id) is None

    @pytest.mark.asyncio
    async def test_session_id_is_hex(self):
        store = McpSessionStore()
        session = await store.create_session()
        # uuid4().hex is 32 hex chars
        assert len(session.session_id) == 32
        int(session.session_id, 16)  # Should not raise


class TestGetSessionStore:
    def test_singleton(self):
        reset_session_store_for_testing()
        store1 = get_session_store()
        store2 = get_session_store()
        assert store1 is store2

    def test_reset(self):
        store1 = get_session_store()
        reset_session_store_for_testing()
        store2 = get_session_store()
        assert store1 is not store2
