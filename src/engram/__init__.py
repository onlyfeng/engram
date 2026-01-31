"""
Engram - AI 友好的事实账本与记忆管理模块

提供：
- logbook: 事实账本核心模块（PostgreSQL 存储）
- gateway: MCP 网关模块（连接 OpenMemory）
"""

__version__ = "0.1.0"

from engram.logbook import config, db, errors

__all__ = ["db", "config", "errors", "__version__"]
