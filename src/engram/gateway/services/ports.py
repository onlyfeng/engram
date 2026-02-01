"""
ports - 服务端口协议定义模块

定义 Gateway 服务层所需的最小依赖接口（Protocol）。
使用 Protocol 实现结构化子类型（structural subtyping），
允许在测试中使用任意实现了相应方法的 mock 对象。

设计原则:
1. 最小接口原则：每个 Protocol 仅定义实际使用的方法
2. 按职责拆分：审计/用户管理/配置分别定义独立 Protocol
3. 支持 mypy strict 模式：完整的类型注解

Protocol 列表:
- WriteAuditPort: 审计写入接口（insert_audit/update_write_audit）
- UserDirectoryPort: 用户目录接口（check_user_exists/ensure_user）
- ActorPolicyConfigPort: Actor 策略配置接口（unknown_actor_policy/private_space_prefix）
- OpenMemoryPort: OpenMemory 客户端接口（store/search）
"""

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Protocol  # noqa: I001

# ======================== OpenMemory 结果类型 ========================


@dataclass
class StoreResult:
    """
    存储操作结果

    Attributes:
        success: 操作是否成功
        memory_id: 成功时返回的 memory ID
        data: 原始响应数据（可选）
        error: 失败时的错误信息
    """

    success: bool
    memory_id: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


@dataclass
class SearchResult:
    """
    搜索操作结果

    Attributes:
        success: 操作是否成功
        results: 搜索结果列表
        error: 失败时的错误信息
    """

    success: bool
    results: Optional[List[Dict[str, Any]]] = None
    error: Optional[str] = None

    def __post_init__(self) -> None:
        if self.results is None:
            self.results = []


# ======================== OpenMemory Port ========================


class OpenMemoryPort(Protocol):
    """
    OpenMemory 客户端端口

    定义 OpenMemory 存储和搜索所需的最小接口。
    用于 memory_store 和 memory_query handler 中的 OpenMemory 操作。

    实现类:
    - OpenMemoryClient (openmemory_client.py)

    测试 Fake:
    - FakeOpenMemoryClient (tests/gateway/fakes.py)

    使用示例:
        def store_memory(
            client: OpenMemoryPort,
            content: str,
            space: str,
        ) -> StoreResult:
            return client.store(content=content, space=space)
    """

    def store(
        self,
        content: str,
        space: Optional[str] = None,
        user_id: Optional[str] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> StoreResult:
        """
        存储记忆到 OpenMemory

        Args:
            content: 记忆内容（markdown）
            space: 目标空间 (team:<project> / private:<user> / org:shared)
            user_id: 用户 ID（用于私有空间）
            tags: 标签列表
            metadata: 额外元数据
            meta: 额外元数据（兼容别名）

        Returns:
            StoreResult 存储结果

        Raises:
            OpenMemoryConnectionError: 连接超时或网络错误
            OpenMemoryAPIError: API 返回错误
        """
        ...

    def search(
        self,
        query: str,
        user_id: Optional[str] = None,
        limit: int = 10,
        filters: Optional[Dict[str, Any]] = None,
    ) -> SearchResult:
        """
        搜索 OpenMemory 记忆

        Args:
            query: 搜索查询
            user_id: 用户 ID（用于私有空间过滤）
            limit: 返回结果数量限制
            filters: 额外过滤条件

        Returns:
            SearchResult 搜索结果
        """
        ...


class WriteAuditPort(Protocol):
    """
    审计写入端口

    定义审计记录写入和更新所需的最小接口。
    用于 audit_service 和 actor_validation 模块中的审计操作。

    实现类:
    - LogbookAdapter (logbook_adapter.py)

    使用示例:
        def write_audit_or_raise(
            db: WriteAuditPort,
            ...
        ) -> int:
            return db.insert_audit(...)
    """

    def insert_audit(
        self,
        actor_user_id: Optional[str],
        target_space: str,
        action: str,
        reason: Optional[str] = None,
        payload_sha: Optional[str] = None,
        evidence_refs_json: Optional[Dict[str, Any]] = None,
        validate_refs: bool = False,
        correlation_id: Optional[str] = None,
        status: str = "success",
    ) -> int:
        """
        写入审计日志

        Args:
            actor_user_id: 操作者用户 ID
            target_space: 目标空间
            action: 操作类型 (allow/redirect/reject)
            reason: 原因说明
            payload_sha: 内容 SHA256 哈希
            evidence_refs_json: 证据链引用
            validate_refs: 是否校验 evidence_refs 结构
            correlation_id: 关联 ID
            status: 审计状态

        Returns:
            创建的 audit_id
        """
        ...

    def update_write_audit(
        self,
        correlation_id: str,
        status: str,
        reason_suffix: Optional[str] = None,
        evidence_refs_json_patch: Optional[Dict[str, Any]] = None,
    ) -> int:
        """
        更新审计记录的最终状态

        Args:
            correlation_id: 关联 ID
            status: 最终状态（success/failed/redirected）
            reason_suffix: 追加到原 reason 的后缀
            evidence_refs_json_patch: 需要合并到 evidence_refs_json 的字段

        Returns:
            更新的记录数
        """
        ...


class UserDirectoryPort(Protocol):
    """
    用户目录端口

    定义用户查询和创建所需的最小接口。
    用于 actor_validation 模块中的用户校验操作。

    实现类:
    - LogbookAdapter (logbook_adapter.py)

    使用示例:
        def validate_actor_user(
            logbook_adapter: UserDirectoryPort,
            ...
        ) -> ActorValidationResult:
            if not logbook_adapter.check_user_exists(actor_user_id):
                logbook_adapter.ensure_user(user_id=actor_user_id, ...)
    """

    def check_user_exists(self, user_id: str) -> bool:
        """
        检查用户是否存在

        Args:
            user_id: 用户标识

        Returns:
            True 如果用户存在
        """
        ...

    def ensure_user(
        self,
        user_id: str,
        display_name: Optional[str] = None,
        is_active: bool = True,
        roles_json: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        确保用户存在（幂等 upsert）

        Args:
            user_id: 用户标识
            display_name: 显示名称
            is_active: 是否激活
            roles_json: 角色信息

        Returns:
            用户信息字典
        """
        ...


class ActorPolicyConfigPort(Protocol):
    """
    Actor 策略配置端口

    定义 actor 校验所需的配置属性接口。
    用于 actor_validation 模块中的策略决策。

    实现类:
    - GatewayConfig (config.py)

    使用示例:
        def validate_actor_user(
            config: ActorPolicyConfigPort,
            ...
        ) -> ActorValidationResult:
            policy = config.unknown_actor_policy
            degrade_space = f"{config.private_space_prefix}unknown"
    """

    @property
    def unknown_actor_policy(self) -> str:
        """
        未知用户处理策略

        返回值:
        - "reject": 拒绝请求
        - "degrade": 降级到 private:unknown 空间
        - "auto_create": 自动创建用户
        """
        ...

    @property
    def private_space_prefix(self) -> str:
        """
        私有空间前缀（默认 "private:"）
        """
        ...


class UserDirectoryWithAuditPort(WriteAuditPort, UserDirectoryPort, Protocol):
    """
    组合端口：同时支持审计写入和用户目录操作

    用于 actor_validation 中 auto_create 策略场景，
    需要同时调用 ensure_user（创建用户）和 insert_audit（写入审计）。

    实现类:
    - LogbookAdapter (logbook_adapter.py)
    """

    pass


# ======================== Tool Definition ========================


@dataclass
class ToolDefinition:
    """
    工具定义数据类

    用于描述 MCP 工具的基本信息和输入规范。

    Attributes:
        name: 工具名称（唯一标识）
        description: 工具描述
        inputSchema: 输入参数 JSON Schema
    """

    name: str
    description: str
    inputSchema: Dict[str, Any]


# ======================== ToolExecutor Port ========================


class ToolCallContext:
    """
    工具调用上下文

    封装工具调用时的上下文信息，包括请求追踪、依赖获取等。

    Attributes:
        correlation_id: 请求追踪 ID，必须由 HTTP 入口层生成
        get_deps: 获取依赖的回调函数（延迟调用）
    """

    def __init__(
        self,
        correlation_id: str,
        get_deps: Callable[[], Any],
    ) -> None:
        self.correlation_id = correlation_id
        self.get_deps = get_deps


class ToolCallResult:
    """
    工具调用结果

    封装工具执行的结果或错误信息。

    Attributes:
        ok: 执行是否成功
        result: 执行结果（成功时）
        error_code: 错误码（失败时）
        error_message: 错误消息（失败时）
        retryable: 是否可重试（失败时）
    """

    def __init__(
        self,
        ok: bool,
        result: Optional[Dict[str, Any]] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        retryable: bool = False,
    ) -> None:
        self.ok = ok
        self.result = result
        self.error_code = error_code
        self.error_message = error_message
        self.retryable = retryable

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        if self.ok:
            return self.result or {}
        # 失败时，优先返回 result 中的完整错误信息（如果有）
        # 这允许 handler 返回额外的错误字段（如 allowed_types, suggestion 等）
        if self.result:
            return self.result
        return {
            "ok": False,
            "error_code": self.error_code,
            "message": self.error_message,
            "retryable": self.retryable,
        }


class ToolExecutorPort(Protocol):
    """
    工具执行器端口

    定义 MCP 工具执行所需的最小接口。
    包含工具列表查询和工具执行两个核心方法。

    实现类:
    - DefaultToolExecutor (entrypoints/tool_executor.py)

    测试 Fake:
    - FakeToolExecutor (tests/gateway/fakes.py)

    使用示例:
        def handle_tools_list(executor: ToolExecutorPort) -> dict:
            tools = executor.list_tools()
            return {"tools": tools}

        async def handle_tools_call(executor: ToolExecutorPort, params: dict):
            context = ToolCallContext(
                correlation_id="corr-xxx",
                get_deps=get_gateway_deps,
            )
            result = await executor.call_tool("memory_store", {"payload_md": "..."}, context)
            return result.to_dict()
    """

    def list_tools(self) -> List[Dict[str, Any]]:
        """
        列出所有可用工具

        Returns:
            工具定义列表（dict 格式），按 name 字母顺序排序

        契约要求：
        - 返回的每个工具必须包含 name, description, inputSchema 字段
        - 工具列表按 name 字母顺序排序（确保响应稳定性）
        """
        ...

    async def call_tool(
        self,
        name: str,
        arguments: Dict[str, Any],
        context: ToolCallContext,
    ) -> ToolCallResult:
        """
        执行工具调用

        参数解析 → 路由到对应 handler → 结果封装

        Args:
            name: 工具名称
            arguments: 工具参数字典
            context: 调用上下文，包含 correlation_id 和依赖获取回调

        Returns:
            ToolCallResult: 执行结果封装

        错误处理契约:
        - 未知工具 → error_code=UNKNOWN_TOOL, retryable=False
        - 非法参数 → error_code=INVALID_PARAM_*, retryable=False
        - 内部异常 → error_code=INTERNAL_ERROR, retryable 根据异常类型决定

        实现应通过统一错误模型处理所有错误场景。
        """
        ...


# ======================== ToolRouter Port ========================


class ToolRouterPort(Protocol):
    """
    工具路由器端口

    定义 MCP 工具路由所需的最小接口。
    与 ToolExecutorPort 的区别在于：
    - ToolRouterPort: 面向 HTTP 入口层的薄接口，签名更简单
    - ToolExecutorPort: 面向内部调用的完整接口，包含上下文对象

    实现类:
    - 默认实现通过 routes.py 中的闭包包装 ToolExecutorPort

    使用示例:
        async def handle_mcp_call(
            router: ToolRouterPort,
            tool: str,
            args: dict,
            correlation_id: str,
        ) -> dict:
            return await router.execute(tool, args, correlation_id)
    """

    async def execute(
        self,
        tool: str,
        args: Dict[str, Any],
        correlation_id: str,
    ) -> Dict[str, Any]:
        """
        执行工具调用

        Args:
            tool: 工具名称
            args: 工具参数字典
            correlation_id: 请求追踪 ID

        Returns:
            Dict[str, Any]: 工具执行结果，必须包含 correlation_id 字段
        """
        ...


__all__ = [
    # 审计与用户目录
    "WriteAuditPort",
    "UserDirectoryPort",
    "ActorPolicyConfigPort",
    "UserDirectoryWithAuditPort",
    # OpenMemory
    "OpenMemoryPort",
    "StoreResult",
    "SearchResult",
    # 工具执行器
    "ToolExecutorPort",
    "ToolRouterPort",
    "ToolDefinition",
    "ToolCallContext",
    "ToolCallResult",
]
