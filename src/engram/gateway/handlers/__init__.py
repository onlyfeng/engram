"""
Gateway Handlers 模块

提供核心业务逻辑处理器实现。

模块结构:
- memory_store: memory_store 工具实现
- memory_query: memory_query 工具实现
- governance_update: governance_update 工具实现
- evidence_upload: evidence_upload 工具实现

依赖注入:
- handlers 内部通过模块级函数（get_config, get_db 等）获取依赖
- 支持通过 GatewayContainer 进行显式依赖注入（用于测试）
- 提供 get_dependencies() 辅助函数获取当前依赖

使用方式:
    # 方式 1: 直接调用（使用全局依赖）
    result = await memory_store_impl(payload_md="...", ...)

    # 方式 2: 在 FastAPI 路由中使用（通过 Depends）
    from engram.gateway.container import get_gateway_container

    @app.post("/memory/store")
    async def store(
        request: MemoryStoreRequest,
        container: GatewayContainer = Depends(get_gateway_container)
    ):
        # container.config, container.db 等可用于显式传参
        ...
"""

from .evidence_upload import execute_evidence_upload
from .governance_update import GovernanceSettingsUpdateResponse, governance_update_impl
from .memory_query import MemoryQueryResponse, memory_query_impl
from .memory_store import MemoryStoreResponse, memory_store_impl

__all__ = [
    # 核心 handler 实现
    "memory_store_impl",
    "MemoryStoreResponse",
    "memory_query_impl",
    "MemoryQueryResponse",
    "governance_update_impl",
    "GovernanceSettingsUpdateResponse",
    "execute_evidence_upload",
    # 依赖注入辅助
    "get_dependencies",
]


def get_dependencies():
    """
    获取 handlers 所需的依赖对象

    返回当前全局容器中的依赖对象，供需要显式访问的场景使用。

    Returns:
        dict: 包含 config, db, logbook_adapter, openmemory_client

    Usage:
        deps = get_dependencies()
        config = deps["config"]
        db = deps["db"]
    """
    from ..container import get_container

    container = get_container()
    return {
        "config": container.config,
        "db": container.db,
        "logbook_adapter": container.logbook_adapter,
        "openmemory_client": container.openmemory_client,
    }
