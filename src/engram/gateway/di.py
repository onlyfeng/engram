"""
Gateway 依赖注入模块 (Dependency Injection)

提供请求上下文和依赖容器的纯 Python 实现，不依赖 FastAPI。

核心类型:
- RequestContext: 请求上下文 dataclass，封装单次请求的追踪信息
- GatewayDeps: 依赖容器 Protocol/dataclass，定义 handler 所需的依赖接口

设计原则:
1. 纯 Python 类型，无框架依赖
2. 支持测试时手动注入 mock
3. 与 container.py 的 GatewayContainer 互补而非替代

依赖获取优先级:
- 容器绑定模式（推荐）: deps.xxx 委托给 container.xxx
- 独立模式: deps.config > _config 参数 > get_config()
- db: deps.db（LogbookDatabase 封装，向后兼容）
- logbook_adapter: deps.logbook_adapter（推荐新代码使用）
- openmemory_client: deps.openmemory_client > _openmemory_client 参数 > get_client()

生产环境推荐用法:
- 使用 container.get_gateway_deps() 获取绑定到全局容器的 deps
- 或使用 GatewayDeps.create() 自动从全局容器派生

迁移说明:
- LogbookDatabase (logbook_db.py) 已弃用，内部实际使用 LogbookAdapter
- 新代码应通过 deps.logbook_adapter 获取数据库操作
- 现有代码可继续使用 deps.db，在迁移完成后统一切换

线程安全与可测试性说明 (ADR: Gateway DI 与入口边界统一):
============================================================

1. 线程安全性 (Thread Safety):
   - RequestContext: 不可变 dataclass，天然线程安全
   - GatewayDeps: 延迟初始化使用模块级单例，依赖底层模块的线程安全实现
   - 推荐每个请求创建独立的 RequestContext 实例，避免跨请求共享状态

2. 可重入性 (Reentrancy):
   - RequestContext.create() / for_testing(): 无副作用，可安全重入
   - GatewayDeps 的 property 访问器: 幂等操作，多次调用返回同一实例
   - 延迟初始化只在首次访问时执行，后续访问直接返回缓存实例

3. 可测试替换 (Test Override):
   - 方式一: 使用 GatewayDeps.for_testing() 直接注入 mock 对象
   - 方式二: 使用 container.py 的 set_container() 替换全局容器
   - 方式三: 直接在测试中构造 RequestContext 和 GatewayDeps 实例

构造参数来源说明:
- GatewayConfig: config.get_config() -> 从环境变量加载
- LogbookAdapter: logbook_adapter.get_adapter(dsn) -> dsn 来自 config.postgres_dsn
- OpenMemoryClient: openmemory_client.get_client(config) -> base_url/api_key 来自 config
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, Optional, Protocol

if TYPE_CHECKING:
    from .config import GatewayConfig
    from .container import GatewayContainer
    from .logbook_adapter import LogbookAdapter
    from .logbook_db import LogbookDatabase
    from .openmemory_client import OpenMemoryClient


def _utc_now() -> datetime:
    """获取当前 UTC 时间"""
    return datetime.now(timezone.utc)


def generate_correlation_id() -> str:
    """
    生成关联 ID

    格式: corr-{16位十六进制}
    与 schemas/audit_event_v1.schema.json 中定义的格式一致。
    此函数在本模块内联实现，避免依赖 mcp_rpc 模块，确保 import-safe。

    注意：mcp_rpc.py 中有同样的实现，用于 JSON-RPC 层。
    两处实现保持格式一致：corr-{uuid.hex[:16]}

    Returns:
        格式为 corr-{16位十六进制} 的关联 ID
    """
    return f"corr-{uuid.uuid4().hex[:16]}"


# ======================== RequestContext ========================


@dataclass
class RequestContext:
    """
    请求上下文

    封装单次请求的追踪信息和元数据，在整个请求处理流程中传递。

    Attributes:
        correlation_id: 请求追踪 ID，用于日志关联和审计
        actor_user_id: 操作者用户 ID（可选）
        target_space: 目标空间（可选）
        request_time: 请求时间（UTC）
        extra: 额外的上下文数据（用于扩展）

    Usage:
        # 创建请求上下文
        ctx = RequestContext.create(actor_user_id="user123")

        # 从现有 correlation_id 创建（用于继承追踪）
        ctx = RequestContext.create(
            correlation_id="corr-abc123def456789a",
            actor_user_id="user123",
        )

        # 测试时直接构造
        ctx = RequestContext(
            correlation_id="corr-0000000000000000",
            actor_user_id="test-user",
        )
    """

    correlation_id: str = field(default_factory=generate_correlation_id)
    actor_user_id: Optional[str] = None
    target_space: Optional[str] = None
    request_time: datetime = field(default_factory=_utc_now)
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        correlation_id: Optional[str] = None,
        actor_user_id: Optional[str] = None,
        target_space: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> "RequestContext":
        """
        创建请求上下文

        Args:
            correlation_id: 请求追踪 ID。若不提供则自动生成。
            actor_user_id: 操作者用户 ID
            target_space: 目标空间
            extra: 额外的上下文数据

        Returns:
            RequestContext 实例
        """
        return cls(
            correlation_id=correlation_id or generate_correlation_id(),
            actor_user_id=actor_user_id,
            target_space=target_space,
            request_time=_utc_now(),
            extra=extra or {},
        )

    @classmethod
    def for_testing(
        cls,
        correlation_id: str = "corr-0000000000000000",
        actor_user_id: Optional[str] = "test-user",
        target_space: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> "RequestContext":
        """
        创建用于测试的请求上下文

        提供稳定的默认值，方便测试断言。

        Args:
            correlation_id: 请求追踪 ID（默认 "corr-0000000000000000"，
                            符合 schema 定义: corr-{16位十六进制}）
            actor_user_id: 操作者用户 ID（默认 "test-user"）
            target_space: 目标空间
            extra: 额外的上下文数据

        Returns:
            RequestContext 实例
        """
        return cls(
            correlation_id=correlation_id,
            actor_user_id=actor_user_id,
            target_space=target_space,
            request_time=datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            extra=extra or {},
        )

    def with_target_space(self, target_space: str) -> "RequestContext":
        """
        创建带有新 target_space 的副本

        用于策略决策后更新目标空间（如 redirect）。

        Args:
            target_space: 新的目标空间

        Returns:
            新的 RequestContext 实例
        """
        return RequestContext(
            correlation_id=self.correlation_id,
            actor_user_id=self.actor_user_id,
            target_space=target_space,
            request_time=self.request_time,
            extra=self.extra.copy(),
        )

    def to_dict(self) -> Dict[str, Any]:
        """
        转换为字典

        用于日志记录和审计事件构建。

        Returns:
            上下文字典
        """
        return {
            "correlation_id": self.correlation_id,
            "actor_user_id": self.actor_user_id,
            "target_space": self.target_space,
            "request_time": self.request_time.isoformat(),
            "extra": self.extra,
        }


# ======================== GatewayDeps Protocol ========================


class GatewayDepsProtocol(Protocol):
    """
    Gateway 依赖协议

    定义 handler 所需依赖的接口契约。
    使用 Protocol 允许在测试中使用任意实现了该接口的对象。
    """

    @property
    def config(self) -> "GatewayConfig":
        """Gateway 配置"""
        ...

    @property
    def db(self) -> "LogbookDatabase":
        """Logbook 数据库实例"""
        ...

    @property
    def logbook_adapter(self) -> "LogbookAdapter":
        """Logbook 适配器实例"""
        ...

    @property
    def openmemory_client(self) -> "OpenMemoryClient":
        """OpenMemory 客户端实例"""
        ...


@dataclass
class GatewayDeps:
    """
    Gateway 依赖容器

    具体实现 GatewayDepsProtocol，用于在 handler 中统一获取依赖。
    支持延迟初始化、容器绑定和显式注入（用于测试）。

    构造模式（按优先级）:
    ====================
    1. from_container(container): 绑定到已有容器，共享依赖实例（推荐生产路径）
    2. create(): 生产路径下优先从全局容器派生，否则延迟初始化
    3. for_testing(...): 测试路径，显式注入 mock 对象

    预热与生命周期:
    ==============
    生产环境推荐通过容器绑定模式（from_container）使用，在 lifespan 中预热：

        async def lifespan(app: FastAPI):
            container = get_container()
            # 预热容器依赖
            _ = container.db
            _ = container.logbook_adapter
            # deps 自动绑定到已预热的容器
            yield

    这样避免请求处理时的初始化延迟，且所有 handlers 共享同一组依赖实例。

    线程安全性:
        - 延迟初始化不使用锁，依赖 Python GIL 保证基本安全
        - 在高并发场景下可能出现多次初始化，但结果一致（幂等）
        - 容器绑定模式下委托给容器的线程安全实现

    可重入性:
        - 所有 property 访问器是幂等的
        - 延迟初始化后缓存实例，后续访问直接返回
        - 容器绑定模式下直接委托，无本地状态

    可测试替换:
        - for_testing() 工厂方法接受 mock 对象
        - 测试时传入 mock 可完全隔离外部依赖
        - 未提供的依赖在访问时会触发延迟初始化（可能导致测试意外失败）

    Attributes:
        _container: 绑定的 GatewayContainer 实例（优先委托）
        _config: GatewayConfig 配置对象（可选，延迟获取）
                 来源: config.get_config() -> 从 PROJECT_KEY/POSTGRES_DSN/OPENMEMORY_BASE_URL 环境变量加载
        _db: LogbookDatabase 数据库实例（可选，延迟获取）
             来源: logbook_db.get_db(dsn=config.postgres_dsn)
        _logbook_adapter: LogbookAdapter 适配器实例（可选，延迟获取）
                          来源: LogbookAdapter(dsn=config.postgres_dsn) 直接构造
        _openmemory_client: OpenMemoryClient 客户端实例（可选，延迟获取）
                            来源: OpenMemoryClient(base_url=config.openmemory_base_url, api_key=config.openmemory_api_key)

    Usage:
        # 生产环境（推荐）：从容器获取 deps
        from .container import get_gateway_deps
        deps = get_gateway_deps()  # 绑定到全局容器

        # 生产环境（备选）：create() 自动从容器派生
        deps = GatewayDeps.create()

        # 测试环境：显式注入 mock
        deps = GatewayDeps.for_testing(
            config=mock_config,
            db=mock_db,
        )
    """

    _container: Optional["GatewayContainer"] = field(default=None, repr=False)
    _config: Optional["GatewayConfig"] = field(default=None, repr=False)
    _db: Optional["LogbookDatabase"] = field(default=None, repr=False)
    _logbook_adapter: Optional["LogbookAdapter"] = field(default=None, repr=False)
    _openmemory_client: Optional["OpenMemoryClient"] = field(default=None, repr=False)

    @classmethod
    def from_container(cls, container: "GatewayContainer") -> "GatewayDeps":
        """
        从 GatewayContainer 创建绑定的 GatewayDeps 实例

        返回的实例委托给容器的同名属性，共享依赖对象。
        这是生产环境推荐的构造方式。

        线程安全:
            是 - 委托给容器的线程安全实现
        幂等性:
            是 - 属性访问委托给容器，容器保证幂等

        Args:
            container: GatewayContainer 实例

        Returns:
            绑定到容器的 GatewayDeps 实例

        Usage:
            container = get_container()
            deps = GatewayDeps.from_container(container)
            # deps.logbook_adapter 实际访问 container.logbook_adapter
        """
        return cls(_container=container)

    @classmethod
    def create(
        cls,
        config: Optional["GatewayConfig"] = None,
        db: Optional["LogbookDatabase"] = None,
        logbook_adapter: Optional["LogbookAdapter"] = None,
        openmemory_client: Optional["OpenMemoryClient"] = None,
        use_container: bool = True,
    ) -> "GatewayDeps":
        """
        创建 GatewayDeps 实例

        生产路径下（use_container=True）优先从全局容器派生，
        共享容器中已构造的依赖，避免在请求内重复构造。

        如果显式传入依赖（adapter/client/db），则使用传入的对象，
        而非从容器获取。

        预热说明:
            当 use_container=True 时，返回的 deps 绑定到全局容器。
            建议在 lifespan 中预热容器依赖，确保 deps 访问时无初始化延迟。

        Args:
            config: 可选的配置对象。如果不提供，首次访问时从全局获取。
            db: 可选的数据库实例。
            logbook_adapter: 可选的 LogbookAdapter 实例。
            openmemory_client: 可选的 OpenMemoryClient 实例。
            use_container: 是否优先从全局容器派生（默认 True）。
                           设为 False 可强制独立初始化（主要用于测试）。

        Returns:
            GatewayDeps 实例
        """
        # 如果提供了任何显式依赖，则不使用容器绑定模式
        has_explicit_deps = any([config, db, logbook_adapter, openmemory_client])

        if use_container and not has_explicit_deps:
            # 生产路径：从全局容器派生，共享依赖
            from .container import get_container

            return cls.from_container(get_container())

        # 显式依赖模式：使用传入的对象或延迟初始化
        return cls(
            _config=config,
            _db=db,
            _logbook_adapter=logbook_adapter,
            _openmemory_client=openmemory_client,
        )

    @classmethod
    def for_testing(
        cls,
        config: Optional["GatewayConfig"] = None,
        db: Optional["LogbookDatabase"] = None,
        logbook_adapter: Optional["LogbookAdapter"] = None,
        openmemory_client: Optional["OpenMemoryClient"] = None,
    ) -> "GatewayDeps":
        """
        创建用于测试的 GatewayDeps 实例

        允许注入 mock 依赖。所有参数都是可选的，未提供的依赖
        在首次访问时会抛出 RuntimeError（避免测试意外使用真实依赖）。

        Args:
            config: 配置对象
            db: 数据库实例
            logbook_adapter: Logbook 适配器实例
            openmemory_client: OpenMemory 客户端实例

        Returns:
            GatewayDeps 实例
        """
        return cls(
            _config=config,
            _db=db,
            _logbook_adapter=logbook_adapter,
            _openmemory_client=openmemory_client,
        )

    @property
    def config(self) -> "GatewayConfig":
        """
        获取 GatewayConfig 实例

        线程安全: 是（依赖 config.get_config() 的单例模式或容器委托）
        可重入: 是（幂等操作）

        构造参数来源:
            - 容器绑定模式: 委托给 container.config
            - 独立模式: config.get_config() -> config.load_config() -> 环境变量:
              - PROJECT_KEY: 项目标识（必填）
              - POSTGRES_DSN: PostgreSQL 连接字符串（必填）
              - OPENMEMORY_BASE_URL: OpenMemory 服务地址（默认 http://localhost:8080）
              - OPENMEMORY_API_KEY / OM_API_KEY: API 密钥（可选）
              - GATEWAY_PORT: 服务端口（默认 8787）
              - 更多配置项参见 config.py 的 load_config() 函数

        Returns:
            GatewayConfig 实例

        Raises:
            ConfigError: 缺少必填环境变量
        """
        # 容器绑定模式：委托给容器
        if self._container is not None:
            return self._container.config

        # 独立模式：延迟初始化
        if self._config is None:
            from .config import get_config

            self._config = get_config()
        return self._config

    @property
    def db(self) -> "LogbookDatabase":
        """
        获取 LogbookDatabase 实例

        [已弃用] 新代码应使用 self.logbook_adapter

        线程安全: 是（依赖 logbook_db.get_db() 的单例模式或容器委托）
        可重入: 是（幂等操作）

        构造参数来源:
            - 容器绑定模式: 委托给 container.db
            - 独立模式: logbook_db.get_db() -> LogbookDatabase(dsn=默认 DSN)
              - 默认 DSN 来自 logbook_db.set_default_dsn(config.postgres_dsn)
              - 或直接传入 dsn 参数

        注意：LogbookDatabase 是已弃用的类，内部实际使用 LogbookAdapter。
        新代码推荐直接使用 self.logbook_adapter 获取 LogbookAdapter 实例。
        此属性保留是为了向后兼容现有代码。

        测试替换:
            deps = GatewayDeps.for_testing(db=mock_db)

        Returns:
            LogbookDatabase 实例（内部封装 LogbookAdapter）
        """
        # 容器绑定模式：委托给容器
        if self._container is not None:
            return self._container.db

        # 独立模式：延迟初始化
        if self._db is None:
            # NOTE: get_db 返回的 LogbookDatabase 实际上是 LogbookAdapter 的封装
            # 在迁移完成后，此处应改为直接使用 LogbookAdapter
            from .logbook_db import get_db

            self._db = get_db()
        return self._db

    @property
    def logbook_adapter(self) -> "LogbookAdapter":
        """
        获取 LogbookAdapter 实例

        线程安全: 是（LogbookAdapter 内部使用连接池，容器委托同样线程安全）
        可重入: 是（幂等操作，每次返回同一实例）

        构造参数来源:
            - 容器绑定模式: 委托给 container.logbook_adapter
            - 独立模式: LogbookAdapter(dsn=self.config.postgres_dsn)
              - dsn: 来自 config.postgres_dsn，即 POSTGRES_DSN 环境变量
              - LogbookAdapter 初始化时会设置 os.environ["POSTGRES_DSN"] 确保内部模块一致

        预热说明:
            容器绑定模式下，如果容器已在 lifespan 中预热，访问此属性无初始化延迟。
            独立模式下首次访问会触发 LogbookAdapter 构造（含数据库连接池初始化）。

        测试替换:
            deps = GatewayDeps.for_testing(logbook_adapter=mock_adapter)
            或使用 logbook_adapter.reset_adapter() 重置全局单例后重新初始化

        Returns:
            LogbookAdapter 实例
        """
        # 容器绑定模式：委托给容器
        if self._container is not None:
            return self._container.logbook_adapter

        # 独立模式：延迟初始化
        if self._logbook_adapter is None:
            from .logbook_adapter import LogbookAdapter

            self._logbook_adapter = LogbookAdapter(dsn=self.config.postgres_dsn)
        return self._logbook_adapter

    @property
    def openmemory_client(self) -> "OpenMemoryClient":
        """
        获取 OpenMemoryClient 实例

        线程安全: 是（httpx.Client 是线程安全的，容器委托同样线程安全）
        可重入: 是（幂等操作，每次返回同一实例）

        构造参数来源:
            - 容器绑定模式: 委托给 container.openmemory_client
            - 独立模式: OpenMemoryClient(
                  base_url=self.config.openmemory_base_url,  # OPENMEMORY_BASE_URL 环境变量
                  api_key=self.config.openmemory_api_key,    # OPENMEMORY_API_KEY 或 OM_API_KEY 环境变量
              )

        预热说明:
            容器绑定模式下，如果容器已在 lifespan 中预热，访问此属性无初始化延迟。
            独立模式下首次访问会触发 OpenMemoryClient 构造。

        测试替换:
            deps = GatewayDeps.for_testing(openmemory_client=mock_client)
            或使用 openmemory_client.reset_client() 重置全局单例后重新初始化

        Returns:
            OpenMemoryClient 实例
        """
        # 容器绑定模式：委托给容器
        if self._container is not None:
            return self._container.openmemory_client

        # 独立模式：延迟初始化
        if self._openmemory_client is None:
            from .openmemory_client import OpenMemoryClient

            self._openmemory_client = OpenMemoryClient(
                base_url=self.config.openmemory_base_url,
                api_key=self.config.openmemory_api_key,
            )
        return self._openmemory_client


# ======================== 便捷构造函数 ========================


def create_request_context(
    correlation_id: Optional[str] = None,
    actor_user_id: Optional[str] = None,
    target_space: Optional[str] = None,
    **extra: Any,
) -> RequestContext:
    """
    创建请求上下文的便捷函数

    Args:
        correlation_id: 请求追踪 ID
        actor_user_id: 操作者用户 ID
        target_space: 目标空间
        **extra: 额外的上下文数据

    Returns:
        RequestContext 实例
    """
    return RequestContext.create(
        correlation_id=correlation_id,
        actor_user_id=actor_user_id,
        target_space=target_space,
        extra=extra if extra else None,
    )


def create_gateway_deps(
    config: Optional["GatewayConfig"] = None,
    db: Optional["LogbookDatabase"] = None,
    logbook_adapter: Optional["LogbookAdapter"] = None,
    openmemory_client: Optional["OpenMemoryClient"] = None,
) -> GatewayDeps:
    """
    创建 Gateway 依赖容器的便捷函数

    如果所有参数都为 None，则创建延迟初始化的容器。
    如果提供了部分参数，则仅这些参数被固定，其余延迟初始化。

    Args:
        config: 配置对象
        db: 数据库实例
        logbook_adapter: Logbook 适配器实例
        openmemory_client: OpenMemory 客户端实例

    Returns:
        GatewayDeps 实例
    """
    return GatewayDeps(
        _config=config,
        _db=db,
        _logbook_adapter=logbook_adapter,
        _openmemory_client=openmemory_client,
    )


# ======================== FastAPI Depends 函数 ========================


def get_request_context(
    correlation_id: Optional[str] = None,
    actor_user_id: Optional[str] = None,
    target_space: Optional[str] = None,
) -> RequestContext:
    """
    获取请求上下文（FastAPI Depends 兼容）

    此函数可用于：
    1. FastAPI Depends 注入（作为依赖函数）
    2. 显式调用（在入口层创建上下文）

    设计原则（ADR：入口层统一 + 参数透传）：
    - correlation_id 只在入口层生成一次
    - 如果未传入 correlation_id，自动生成（用于入口层调用）
    - handlers 应从入口层接收 ctx，而非自行生成

    Args:
        correlation_id: 请求追踪 ID。若不提供则自动生成。
        actor_user_id: 操作者用户 ID
        target_space: 目标空间

    Returns:
        RequestContext 实例

    Usage:
        # FastAPI Depends 注入
        @app.post("/example")
        async def example(ctx: RequestContext = Depends(get_request_context)):
            ...

        # 显式调用（推荐在入口层使用）
        from engram.gateway.di import generate_correlation_id
        ctx = get_request_context(correlation_id=generate_correlation_id())
        # correlation_id 格式: corr-{16位十六进制}
    """
    return RequestContext.create(
        correlation_id=correlation_id,
        actor_user_id=actor_user_id,
        target_space=target_space,
    )


# ======================== 导出 ========================

__all__ = [
    # 核心类型
    "RequestContext",
    "GatewayDeps",
    "GatewayDepsProtocol",
    # 便捷函数
    "create_request_context",
    "create_gateway_deps",
    "generate_correlation_id",
    # FastAPI Depends 函数
    "get_request_context",
]
