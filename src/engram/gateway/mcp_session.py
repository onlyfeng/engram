"""
MCP Streamable HTTP 会话管理模块

提供 MCP 传输规范（2025-03-26）要求的会话管理功能：
- 会话创建（initialize 时生成 Mcp-Session-Id）
- 会话验证（后续请求验证 session_id 有效性）
- 会话销毁（DELETE /mcp 终止会话）
- 惰性 TTL 过期清理

内存 dict 存储，单进程足够。
使用 asyncio.Lock 保护共享状态，防止协程并发导致的竞态条件。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, Optional
from uuid import uuid4

# 默认会话 TTL：1 小时
DEFAULT_SESSION_TTL_SECONDS = 3600.0


@dataclass
class McpSession:
    """单个 MCP 会话"""

    session_id: str
    created_at: float = field(default_factory=time.monotonic)
    initialized: bool = False


class McpSessionStore:
    """
    MCP 会话存储

    内存 dict 存储，惰性 TTL 过期清理。
    使用 asyncio.Lock 保护所有 _sessions 操作，
    防止并发协程间的 check-then-act 竞态。
    """

    def __init__(self, ttl: float = DEFAULT_SESSION_TTL_SECONDS) -> None:
        self._sessions: Dict[str, McpSession] = {}
        self._ttl = ttl
        self._lock = asyncio.Lock()

    async def create_session(self) -> McpSession:
        """创建新会话，返回 McpSession 实例"""
        async with self._lock:
            self._lazy_cleanup()
            session = McpSession(session_id=uuid4().hex)
            self._sessions[session.session_id] = session
            return session

    async def get_session(self, session_id: str) -> Optional[McpSession]:
        """获取会话，不存在或已过期返回 None"""
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if self._is_expired(session):
                self._sessions.pop(session_id, None)
                return None
            return session

    async def delete_session(self, session_id: str) -> bool:
        """删除会话，返回是否成功删除"""
        async with self._lock:
            return self._sessions.pop(session_id, None) is not None

    async def mark_initialized(self, session_id: str) -> None:
        """标记会话已完成 initialized 通知"""
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is not None and not self._is_expired(session):
                session.initialized = True

    def _is_expired(self, session: McpSession) -> bool:
        return (time.monotonic() - session.created_at) > self._ttl

    def _lazy_cleanup(self) -> None:
        """惰性清理过期会话（必须在持有 _lock 时调用）"""
        now = time.monotonic()
        expired = [sid for sid, s in self._sessions.items() if (now - s.created_at) > self._ttl]
        for sid in expired:
            self._sessions.pop(sid, None)


# 模块级单例
_session_store: Optional[McpSessionStore] = None


def get_session_store() -> McpSessionStore:
    """获取全局 McpSessionStore 单例"""
    global _session_store
    if _session_store is None:
        _session_store = McpSessionStore()
    return _session_store


def reset_session_store_for_testing() -> None:
    """重置全局 session store（仅测试用）"""
    global _session_store
    _session_store = None
