"""
Gateway Handlers 模块

提供核心业务逻辑处理器实现。

模块结构:
- memory_store: memory_store 工具实现
- memory_query: memory_query 工具实现
- governance_update: governance_update 工具实现
- evidence_upload: evidence_upload 工具实现

依赖注入:
- handlers 通过 `deps: GatewayDepsProtocol` 参数接收依赖
- 生产环境使用 `get_gateway_deps()` 获取绑定到全局容器的 deps
- 测试环境使用 `GatewayDeps.for_testing(...)` 注入 mock 对象

重要约束:
- handlers 禁止直接 import `get_container()` — 应由入口层（app.py/startup.py）调用
- 依赖应通过 `deps: GatewayDepsProtocol` 参数传递，或通过 `get_gateway_deps()` 获取

使用方式:
    # 推荐方式: 通过 FastAPI Depends 注入 deps
    from engram.gateway.container import get_gateway_deps

    @app.post("/memory/store")
    async def store(
        request: MemoryStoreRequest,
        deps: GatewayDepsProtocol = Depends(get_gateway_deps),
    ):
        config = deps.config
        adapter = deps.logbook_adapter
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
]
