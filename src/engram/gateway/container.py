"""
Gateway 依赖容器 (Dependency Container)

提供 GatewayContainer 类，集中管理 Gateway 的所有依赖：
- config: GatewayConfig 配置对象
- db: LogbookDatabase 数据库实例
- logbook_adapter: LogbookAdapter 适配器实例
- openmemory_client: OpenMemoryClient 客户端实例

使用方式：
1. 应用启动时创建 GatewayContainer 实例
2. 通过 FastAPI Depends 注入到 handlers 中
3. handlers 从容器获取所需依赖

预热与生命周期管理:
====================
GatewayContainer 采用延迟初始化策略，所有依赖在首次访问时构造。
生产环境推荐在 FastAPI lifespan 中预热依赖，避免请求时初始化延迟：

    async def lifespan(app: FastAPI):
        container = get_container()
        # 预热：触发延迟初始化
        _ = container.db
        _ = container.logbook_adapter
        _ = container.openmemory_client
        yield
        # 清理（可选）
        reset_container()

桥接 di.py:
===========
通过 as_deps() 或 deps 属性获取 GatewayDepsProtocol 实现，
该实现绑定到同一 container 实例，共享已初始化的依赖。

线程安全性:
==========
- 延迟初始化不使用锁，依赖 Python GIL 保证基本安全
- 在高并发场景下可能出现多次初始化，但结果幂等
- 全局容器单例 (_container) 在进程内共享，适合 ASGI 单进程多协程模型
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from .config import GatewayConfig, get_config, reset_config

if TYPE_CHECKING:
    from .di import GatewayDepsProtocol
    from .logbook_adapter import LogbookAdapter
    from .logbook_db import LogbookDatabase
    from .openmemory_client import OpenMemoryClient


@dataclass
class GatewayContainer:
    """
    Gateway 依赖容器

    集中管理 Gateway 的所有核心依赖。支持延迟初始化和依赖注入。

    Attributes:
        config: GatewayConfig 配置对象
        _db: LogbookDatabase 数据库实例（延迟初始化）
        _logbook_adapter: LogbookAdapter 适配器实例（延迟初始化）
        _openmemory_client: OpenMemoryClient 客户端实例（延迟初始化）
    """

    config: GatewayConfig = field(default_factory=get_config)
    _db: Optional["LogbookDatabase"] = field(default=None, repr=False)
    _logbook_adapter: Optional["LogbookAdapter"] = field(default=None, repr=False)
    _openmemory_client: Optional["OpenMemoryClient"] = field(default=None, repr=False)
    _deps_cache: Optional["GatewayDepsProtocol"] = field(default=None, repr=False)

    @classmethod
    def create(cls, config: Optional[GatewayConfig] = None) -> "GatewayContainer":
        """
        创建 GatewayContainer 实例

        Args:
            config: 可选的配置对象。如果不提供，从环境变量加载。

        Returns:
            GatewayContainer 实例
        """
        if config is None:
            config = get_config()
        return cls(config=config)

    @classmethod
    def create_for_testing(
        cls,
        config: Optional[GatewayConfig] = None,
        db: Optional["LogbookDatabase"] = None,
        logbook_adapter: Optional["LogbookAdapter"] = None,
        openmemory_client: Optional["OpenMemoryClient"] = None,
    ) -> "GatewayContainer":
        """
        创建用于测试的 GatewayContainer 实例

        允许注入 mock 依赖。

        Args:
            config: 配置对象
            db: 数据库实例
            logbook_adapter: Logbook 适配器实例
            openmemory_client: OpenMemory 客户端实例

        Returns:
            GatewayContainer 实例
        """
        container = cls(config=config or get_config())
        container._db = db
        container._logbook_adapter = logbook_adapter
        container._openmemory_client = openmemory_client
        return container

    @property
    def db(self) -> "LogbookDatabase":
        """
        获取 LogbookDatabase 实例（延迟初始化）

        Returns:
            LogbookDatabase 实例
        """
        if self._db is None:
            from .logbook_db import get_db, set_default_dsn

            set_default_dsn(self.config.postgres_dsn)
            self._db = get_db(dsn=self.config.postgres_dsn)
        return self._db

    @property
    def logbook_adapter(self) -> "LogbookAdapter":
        """
        获取 LogbookAdapter 实例（延迟初始化）

        Returns:
            LogbookAdapter 实例
        """
        if self._logbook_adapter is None:
            from .logbook_adapter import LogbookAdapter

            self._logbook_adapter = LogbookAdapter(dsn=self.config.postgres_dsn)
        return self._logbook_adapter

    @property
    def openmemory_client(self) -> "OpenMemoryClient":
        """
        获取 OpenMemoryClient 实例（延迟初始化）

        Returns:
            OpenMemoryClient 实例
        """
        if self._openmemory_client is None:
            from .openmemory_client import OpenMemoryClient

            self._openmemory_client = OpenMemoryClient(
                base_url=self.config.openmemory_base_url,
                api_key=self.config.openmemory_api_key,
            )
        return self._openmemory_client

    def reset(self) -> None:
        """
        重置所有依赖实例

        用于测试或重新初始化场景。
        """
        self._db = None
        self._logbook_adapter = None
        self._openmemory_client = None
        # 同时清除缓存的 deps 实例
        if hasattr(self, "_deps_cache"):
            self._deps_cache = None

    def as_deps(self) -> "GatewayDepsProtocol":
        """
        获取绑定到此容器的 GatewayDepsProtocol 实现

        返回的 GatewayDeps 实例与本容器共享同一组依赖对象，
        访问 deps.logbook_adapter 等属性时，实际委托给容器的同名属性。

        线程安全:
            是 - 返回的 GatewayDeps 绑定到容器实例，依赖访问委托给容器
        幂等性:
            是 - 多次调用返回同一实例（缓存在 _deps_cache）

        生命周期说明:
            - 返回的 GatewayDeps 与容器绑定，生命周期与容器一致
            - 调用 container.reset() 会同时清除缓存的 deps 实例
            - 适合在 lifespan 中预热后供整个应用生命周期使用

        Returns:
            GatewayDepsProtocol 实现（GatewayDeps 实例）

        Usage:
            container = get_container()
            deps = container.as_deps()
            adapter = deps.logbook_adapter  # 实际调用 container.logbook_adapter
        """
        # 使用缓存确保幂等
        if not hasattr(self, "_deps_cache") or self._deps_cache is None:
            from .di import GatewayDeps

            # 创建绑定到容器的 GatewayDeps 实例
            # 直接传入已初始化（或将延迟初始化）的依赖引用
            self._deps_cache = GatewayDeps.from_container(self)
        return self._deps_cache

    @property
    def deps(self) -> "GatewayDepsProtocol":
        """
        获取绑定到此容器的 GatewayDepsProtocol 实现（属性访问器）

        等同于 as_deps()，提供属性风格的访问方式。

        Returns:
            GatewayDepsProtocol 实现

        Usage:
            container = get_container()
            deps = container.deps
            adapter = deps.logbook_adapter
        """
        return self.as_deps()


# ======================== 全局容器实例管理 ========================

_container: Optional[GatewayContainer] = None


def get_container() -> GatewayContainer:
    """
    获取全局 GatewayContainer 实例（单例模式）

    首次调用时创建实例，后续调用返回缓存的实例。

    **使用场景**:
    - 入口层（app.py, startup.py）：用于初始化和预热依赖
    - FastAPI 依赖注入函数（本模块内）：如 get_gateway_deps() 等

    **禁止场景**:
    - handlers/ 模块内禁止直接调用此函数
    - handlers 应通过 `deps: GatewayDepsProtocol` 参数获取依赖

    Returns:
        GatewayContainer 实例

    See Also:
        - get_gateway_deps(): handlers 推荐使用的依赖获取方式
        - GatewayDeps.for_testing(): 测试时的依赖注入方式
    """
    global _container
    if _container is None:
        _container = GatewayContainer.create()
    return _container


def set_container(container: GatewayContainer) -> None:
    """
    设置全局 GatewayContainer 实例

    用于测试或自定义初始化场景。

    Args:
        container: GatewayContainer 实例
    """
    global _container
    _container = container


def reset_container() -> None:
    """
    重置全局 GatewayContainer 实例

    用于测试清理。
    """
    global _container
    if _container is not None:
        _container.reset()
    _container = None
    reset_config()


def reset_all_singletons() -> None:
    """
    重置所有 Gateway 模块的全局单例

    用于测试 teardown，确保所有单例被正确清理，避免测试间状态污染。
    此函数同时重置：
    - GatewayContainer 全局容器
    - GatewayConfig 全局配置
    - LogbookAdapter 全局适配器
    - OpenMemoryClient 全局客户端

    使用场景：
    - pytest fixture 的 teardown
    - 集成测试的 setup/teardown
    - 需要完全隔离的测试场景

    Usage:
        @pytest.fixture(autouse=True)
        def reset_singletons():
            yield
            reset_all_singletons()

    线程安全: 否（建议在单线程环境下调用，如测试 teardown）
    """
    global _container

    # 1. 重置全局容器（内部会调用 reset_config）
    if _container is not None:
        _container.reset()
    _container = None

    # 2. 重置 config 单例
    reset_config()

    # 3. 重置 logbook_adapter 单例
    from .logbook_adapter import reset_adapter

    reset_adapter()

    # 4. 重置 openmemory_client 单例
    from .openmemory_client import reset_client

    reset_client()


# ======================== FastAPI 依赖注入函数 ========================


def get_gateway_container() -> GatewayContainer:
    """
    FastAPI 依赖注入函数：获取 GatewayContainer

    用法:
        @app.get("/example")
        async def example(container: GatewayContainer = Depends(get_gateway_container)):
            config = container.config
            db = container.db
            ...
    """
    return get_container()


def get_gateway_config() -> GatewayConfig:
    """
    FastAPI 依赖注入函数：获取 GatewayConfig

    用法:
        @app.get("/example")
        async def example(config: GatewayConfig = Depends(get_gateway_config)):
            ...
    """
    return get_container().config


def get_logbook_db():
    """
    FastAPI 依赖注入函数：获取 LogbookDatabase

    用法:
        @app.get("/example")
        async def example(db = Depends(get_logbook_db)):
            ...
    """
    return get_container().db


def get_logbook_adapter_dep():
    """
    FastAPI 依赖注入函数：获取 LogbookAdapter

    用法:
        @app.get("/example")
        async def example(adapter = Depends(get_logbook_adapter_dep)):
            ...
    """
    return get_container().logbook_adapter


def get_openmemory_client_dep():
    """
    FastAPI 依赖注入函数：获取 OpenMemoryClient

    用法:
        @app.get("/example")
        async def example(client = Depends(get_openmemory_client_dep)):
            ...
    """
    return get_container().openmemory_client


def get_gateway_deps() -> "GatewayDepsProtocol":
    """
    FastAPI 依赖注入函数：获取 GatewayDepsProtocol

    返回全局容器绑定的 GatewayDeps 实例。推荐在 handlers 中使用此函数
    获取依赖，而非直接访问容器或调用 GatewayDeps.create()。

    生命周期说明:
        - 返回的 deps 绑定到全局容器，与应用生命周期一致
        - 所有请求共享同一 deps 实例（及其底层依赖）
        - 适合无状态的请求处理，避免每次请求重新构造依赖

    线程安全:
        是 - 返回单例实例，依赖访问委托给线程安全的容器

    用法:
        @app.post("/memory/store")
        async def store_memory(
            deps: GatewayDepsProtocol = Depends(get_gateway_deps),
            ctx: RequestContext = Depends(get_request_context),
        ):
            adapter = deps.logbook_adapter
            ...

    Returns:
        GatewayDepsProtocol 实现
    """
    return get_container().deps
