"""
Gateway 请求级依赖获取模块

提供统一的入口层依赖和 correlation_id 获取函数，
确保路由层与业务逻辑的关注点分离。

设计原则：
===========
1. 仅在入口/路由层调用 get_deps_for_request() 和 new_request_correlation_id()
2. handlers 通过参数接收 deps 和 correlation_id，不直接调用本模块
3. 对 mcp_rpc 的 correlation_id 函数做薄封装，保持格式一致

使用方式：
=========
    from .dependencies import get_deps_for_request, new_request_correlation_id

    # 在路由层获取依赖和 correlation_id
    deps = get_deps_for_request()
    correlation_id = new_request_correlation_id()

    # 传递给 handler
    result = await some_handler_impl(
        ...,
        correlation_id=correlation_id,
        deps=deps,
    )

与其他模块的关系：
================
- container.py: GatewayContainer 管理依赖生命周期
- di.py: GatewayDeps/GatewayDepsProtocol 定义依赖接口
- correlation_id.py: correlation_id 生成和校验的单一来源实现
- middleware.py: 中间件层的 correlation_id 上下文管理

本模块作为入口层的薄封装，屏蔽底层实现细节。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .di import GatewayDepsProtocol


def get_deps_for_request() -> "GatewayDepsProtocol":
    """
    获取当前请求所需的依赖（仅在入口/路由层调用）

    此函数是入口层获取依赖的统一接口，封装了对 container 的访问。
    handlers 不应直接调用此函数，而应通过参数接收 deps。

    设计原则：
    - 入口层统一获取依赖，handlers 以参数形式接收
    - 支持 import-time 不触发 get_config()
    - lifespan 中预热后，首次请求时依赖已初始化

    线程安全: 是 - 委托给 container 的线程安全实现
    可重入: 是 - 返回单例实例

    Returns:
        GatewayDepsProtocol 实现

    Raises:
        ConfigError: 配置加载失败（缺少必填环境变量等）

    Usage:
        # 在路由函数中
        @app.post("/memory/store")
        async def memory_store_endpoint(request: MemoryStoreRequest):
            deps = get_deps_for_request()
            correlation_id = new_request_correlation_id()
            return await memory_store_impl(..., deps=deps, correlation_id=correlation_id)
    """
    # 延迟导入，避免模块导入时触发 get_config()
    from .container import get_container

    return get_container().deps


def new_request_correlation_id(existing: Optional[str] = None) -> str:
    """
    获取或生成请求的 correlation_id（仅在入口/路由层调用）

    此函数是入口层获取 correlation_id 的统一接口，封装了对 correlation_id 模块的访问。
    handlers 不应直接调用此函数，而应通过参数接收 correlation_id。

    行为说明：
    - 如果提供了 existing 参数且格式合规，直接返回
    - 如果 existing 不合规或为空，生成新的 correlation_id
    - 格式保证符合 schema: corr-{16位十六进制}

    与 middleware 的配合：
    - 优先使用中间件上下文中的 correlation_id（单一来源原则）
    - 若不在中间件上下文中（如单元测试），则生成新的

    Args:
        existing: 已有的 correlation_id（可选）。
                  通常来自中间件 get_request_correlation_id()。

    Returns:
        合规的 correlation_id，格式: corr-{16位十六进制}

    Usage:
        # 在路由函数中（推荐方式：配合中间件）
        from .middleware import get_request_correlation_id

        @app.post("/memory/store")
        async def memory_store_endpoint(request: MemoryStoreRequest):
            correlation_id = new_request_correlation_id(get_request_correlation_id())
            ...

        # 独立使用（不在中间件上下文中）
        correlation_id = new_request_correlation_id()
    """
    # 从 mcp_rpc 重新导出（保持单一来源且便于测试 patch）
    from .mcp_rpc import generate_correlation_id, normalize_correlation_id

    if existing:
        # 归一化：如果 existing 合规则直接返回，否则生成新的
        return normalize_correlation_id(existing)

    # 无 existing，生成新的
    return generate_correlation_id()


def get_request_correlation_id_or_new() -> str:
    """
    从中间件上下文获取 correlation_id，若无则生成新的

    这是 new_request_correlation_id() 与 middleware.get_request_correlation_id() 的组合，
    提供一站式的 correlation_id 获取。

    典型用法：
        @app.post("/memory/store")
        async def memory_store_endpoint(request: MemoryStoreRequest):
            correlation_id = get_request_correlation_id_or_new()
            deps = get_deps_for_request()
            return await memory_store_impl(..., correlation_id=correlation_id, deps=deps)

    Returns:
        合规的 correlation_id
    """
    from .middleware import get_request_correlation_id

    return new_request_correlation_id(get_request_correlation_id())


__all__ = [
    "get_deps_for_request",
    "new_request_correlation_id",
    "get_request_correlation_id_or_new",
]
