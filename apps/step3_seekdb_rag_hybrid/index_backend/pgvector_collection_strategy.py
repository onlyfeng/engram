"""
pgvector_collection_strategy.py - PGVector Collection 存储策略模块

定义 Collection 到存储位置的映射策略接口，支持多种隔离方案：
- 方案 A: 按 collection 建表（每个 collection 独立表）
- 方案 B: 单表多租户（所有 collection 共享一张表，通过 WHERE 过滤）

策略接口:
    resolve_storage(collection_id, schema, base_table) 
    -> StorageResolution{schema, table, where_clause_extra, params_extra}

Collection 命名规则:
    - Canonical ID (冒号格式): {project_key}:{chunking_version}:{embedding_model_id}[:{version_tag}]
    - PGVector 表名: step3_chunks_{sanitized_collection_id}
    - 表名长度限制: ≤63 字符（PostgreSQL 限制），超长时截断并添加 hash 后缀

默认策略（DefaultCollectionStrategy）保持向后兼容，不启用隔离。
"""

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable

logger = logging.getLogger(__name__)


# ============ 异常类 ============


class CollectionStrategyError(Exception):
    """Collection 策略错误基类"""
    pass


class VectorDimensionMismatchError(CollectionStrategyError):
    """
    向量维度不匹配错误
    
    当使用 single_table 策略时，所有 collection 必须使用相同的向量维度。
    如果检测到维度不匹配，会抛出此错误并指引用户使用 per_table 策略。
    """
    
    def __init__(
        self,
        collection_id: str,
        requested_dim: int,
        expected_dim: int,
        table_name: str,
        is_preflight: bool = False,
    ):
        self.collection_id = collection_id
        self.requested_dim = requested_dim
        self.expected_dim = expected_dim
        self.table_name = table_name
        self.is_preflight = is_preflight
        
        if is_preflight:
            # Preflight 校验时的错误信息（从数据库读取的实际维度）
            message = (
                f"向量维度不匹配 (preflight 校验失败):\n"
                f"  配置维度 (STEP3_PG_VECTOR_DIM): {requested_dim}\n"
                f"  共享表 '{table_name}' 实际维度: {expected_dim}\n"
                f"\n"
                f"解决方案:\n"
                f"  1. 修改 STEP3_PG_VECTOR_DIM 环境变量为 {expected_dim}（与现有表一致）\n"
                f"  2. 或者切换到 per_table 策略（每个 collection 独立表）:\n"
                f"     设置环境变量: STEP3_PGVECTOR_COLLECTION_STRATEGY=per_table\n"
                f"  3. 或者创建新的共享表（使用不同表名）:\n"
                f"     设置环境变量: STEP3_PG_TABLE=chunks_new_{requested_dim}d\n"
                f"\n"
                f"注意: single_table/routing 策略要求所有 collection 共享同一张表，"
                f"因此必须使用相同维度的向量。"
            )
        else:
            message = (
                f"向量维度不匹配: collection '{collection_id}' 请求维度 {requested_dim}，"
                f"但共享表 '{table_name}' 要求维度 {expected_dim}。\n"
                f"\n"
                f"解决方案:\n"
                f"  1. 确保所有 collection 使用相同的 embedding 模型（相同维度）\n"
                f"  2. 或者切换到 per_table 策略（每个 collection 独立表）:\n"
                f"     设置环境变量: STEP3_PGVECTOR_COLLECTION_STRATEGY=per_table\n"
                f"\n"
                f"注意: single_table 策略要求所有 collection 共享同一张表，"
                f"因此必须使用相同维度的向量。"
            )
        super().__init__(message)

# PostgreSQL 标识符最大长度
_MAX_IDENTIFIER_LENGTH = 63

# 合法标识符正则：只允许字母、数字、下划线，以字母或下划线开头
_IDENTIFIER_PATTERN = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')


# ============ 存储解析结果 ============


@dataclass
class StorageResolution:
    """
    存储解析结果
    
    由策略的 resolve_storage 方法返回，包含：
    - 实际使用的 schema 和 table
    - 额外的 WHERE 条件（用于多租户隔离）
    - 额外的参数值
    
    Attributes:
        schema: 目标 schema 名称
        table: 目标表名
        where_clause_extra: 额外的 WHERE 子句（如 "collection_id = %s"），空串表示无额外条件
        params_extra: WHERE 子句对应的参数列表
        qualified_table: 完整的 schema.table 名称（用于日志显示）
    """
    schema: str
    table: str
    where_clause_extra: str = ""
    params_extra: List[Any] = field(default_factory=list)
    
    @property
    def qualified_table(self) -> str:
        """返回带 schema 的完整表名（用于日志显示）"""
        return f'"{self.schema}"."{self.table}"'
    
    @property
    def has_extra_filter(self) -> bool:
        """是否有额外的过滤条件"""
        return bool(self.where_clause_extra)
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典（用于调试和日志）"""
        return {
            "schema": self.schema,
            "table": self.table,
            "qualified_table": self.qualified_table,
            "where_clause_extra": self.where_clause_extra,
            "params_extra": self.params_extra,
            "has_extra_filter": self.has_extra_filter,
        }


# ============ 策略协议/基类 ============


@runtime_checkable
class CollectionStrategy(Protocol):
    """
    Collection 存储策略协议
    
    实现此协议的类需要提供 resolve_storage 方法，
    用于将 collection_id 映射到实际的存储位置。
    """
    
    @property
    def strategy_name(self) -> str:
        """策略名称"""
        ...
    
    def resolve_storage(
        self,
        collection_id: Optional[str],
        schema: str,
        base_table: str,
    ) -> StorageResolution:
        """
        解析存储位置
        
        Args:
            collection_id: Collection ID（canonical 冒号格式），可为 None
            schema: 配置的 schema 名称
            base_table: 配置的基础表名
        
        Returns:
            StorageResolution 包含实际的 schema、table 及额外过滤条件
        """
        ...


class BaseCollectionStrategy(ABC):
    """
    Collection 存储策略基类
    
    提供通用功能和抽象接口。
    """
    
    @property
    @abstractmethod
    def strategy_name(self) -> str:
        """策略名称"""
        pass
    
    @abstractmethod
    def resolve_storage(
        self,
        collection_id: Optional[str],
        schema: str,
        base_table: str,
    ) -> StorageResolution:
        """解析存储位置"""
        pass
    
    def validate_table_name(self, table_name: str) -> Tuple[bool, str]:
        """
        验证表名是否合法
        
        Args:
            table_name: 表名
        
        Returns:
            (is_valid, error_message) 元组
        """
        if not table_name:
            return False, "表名不能为空"
        
        if len(table_name) > _MAX_IDENTIFIER_LENGTH:
            return False, f"表名长度超过 {_MAX_IDENTIFIER_LENGTH} 字符: {len(table_name)}"
        
        if not _IDENTIFIER_PATTERN.match(table_name):
            return False, f"表名包含非法字符或格式错误: {table_name}"
        
        return True, ""
    
    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} strategy={self.strategy_name}>"


# ============ 默认策略实现 ============


class DefaultCollectionStrategy(BaseCollectionStrategy):
    """
    默认存储策略（向后兼容，不启用隔离）
    
    行为：
    - 当 collection_id 存在时，使用 collection_naming 生成动态表名
    - 当 collection_id 为 None 时，使用传入的 base_table
    - 不添加任何额外的 WHERE 条件
    - 自动验证生成的表名合法性
    
    这保持了与原 PGVectorBackend 完全一致的行为，
    每个 collection 仍然映射到独立的表。
    
    Collection 命名规则:
        Canonical ID: {project_key}:{chunking_version}:{embedding_model_id}[:{version_tag}]
        PGVector 表名: step3_chunks_{sanitized_id}
        
        例如:
        - "proj1:v2:bge-m3" -> "step3_chunks_proj1_v2_bge_m3"
        - "default:v1:bge-m3:20260128T120000" -> "step3_chunks_default_v1_bge_m3_20260128t120000"
    """
    
    @property
    def strategy_name(self) -> str:
        return "default"
    
    def resolve_storage(
        self,
        collection_id: Optional[str],
        schema: str,
        base_table: str,
    ) -> StorageResolution:
        """
        默认策略：按 collection_id 生成动态表名，无额外过滤
        
        Args:
            collection_id: Collection ID，可为 None
            schema: 配置的 schema
            base_table: 默认表名（当 collection_id 为 None 时使用）
        
        Returns:
            StorageResolution，where_clause_extra 为空
        
        Raises:
            ValueError: 生成的表名不合法时抛出
        """
        if collection_id is not None:
            # 使用 collection_naming 生成动态表名
            table_name = self._get_table_name_for_collection(collection_id)
            
            # 验证表名合法性
            is_valid, error_msg = self.validate_table_name(table_name)
            if not is_valid:
                raise ValueError(
                    f"collection_id '{collection_id}' 生成的表名无效: {error_msg}"
                )
            
            logger.debug(
                f"DefaultStrategy: collection_id={collection_id} -> table={table_name}"
            )
        else:
            # 使用默认表名
            table_name = base_table
            logger.debug(f"DefaultStrategy: 无 collection_id，使用默认表={table_name}")
        
        return StorageResolution(
            schema=schema,
            table=table_name,
            where_clause_extra="",
            params_extra=[],
        )
    
    def _get_table_name_for_collection(self, collection_id: str) -> str:
        """
        根据 collection_id 生成表名
        
        使用 collection_naming 模块的 to_pgvector_table_name 函数。
        该函数会:
        1. 将 collection_id 中的特殊字符替换为下划线
        2. 转换为小写
        3. 添加 step3_chunks_ 前缀
        4. 超长时截断并添加 hash 后缀以保证唯一性
        
        Args:
            collection_id: canonical collection_id (冒号格式)
        
        Returns:
            合法的 PostgreSQL 表名
        """
        try:
            from collection_naming import to_pgvector_table_name
        except ImportError:
            try:
                from step3_seekdb_rag_hybrid.collection_naming import to_pgvector_table_name
            except ImportError:
                # 回退实现（包含长度限制处理）
                import hashlib
                
                def to_pgvector_table_name(cid: str) -> str:
                    # 清理标识符
                    sanitized = cid.replace(":", "_").replace("-", "_").lower()
                    # 移除其他特殊字符
                    import re
                    sanitized = re.sub(r'[^a-zA-Z0-9_]', '', sanitized)
                    # 确保不以数字开头
                    if sanitized and sanitized[0].isdigit():
                        sanitized = "_" + sanitized
                    
                    full_name = f"step3_chunks_{sanitized}"
                    
                    # 检查长度限制
                    if len(full_name) <= _MAX_IDENTIFIER_LENGTH:
                        return full_name
                    
                    # 超长时截断并添加 hash 后缀
                    hash_suffix = hashlib.sha256(cid.encode()).hexdigest()[:8]
                    prefix = "step3_chunks_"
                    max_body_len = _MAX_IDENTIFIER_LENGTH - len(prefix) - 9  # _hash8
                    truncated = sanitized[:max_body_len].rstrip('_')
                    
                    return f"{prefix}{truncated}_{hash_suffix}"
        
        return to_pgvector_table_name(collection_id)
    
    def get_table_name_for_collection(self, collection_id: str) -> str:
        """
        公开方法：获取 collection_id 对应的表名
        
        供外部调用以获取表名映射（如用于日志、调试）。
        
        Args:
            collection_id: canonical collection_id (冒号格式)
        
        Returns:
            表名
        """
        return self._get_table_name_for_collection(collection_id)


# ============ 单表多租户策略（预留实现） ============


class SharedTableStrategy(BaseCollectionStrategy):
    """
    单表多租户策略（方案 B）
    
    行为：
    - 所有 collection 共享同一张表
    - 通过 collection_id 列进行隔离
    - 自动添加 WHERE collection_id = %s 条件
    
    注意：
    - 此策略要求表中有 collection_id 列
    - 所有 collection 必须使用相同的向量维度（因为共享同一张表）
    - 如果需要不同维度的 collection，请使用 per_table 策略
    """
    
    def __init__(
        self,
        collection_id_column: str = "collection_id",
        expected_vector_dim: Optional[int] = None,
    ):
        """
        初始化单表多租户策略
        
        Args:
            collection_id_column: 用于隔离的列名，默认为 "collection_id"
            expected_vector_dim: 预期的向量维度，用于验证 collection 维度一致性
        """
        self._collection_id_column = collection_id_column
        self._expected_vector_dim = expected_vector_dim
    
    @property
    def strategy_name(self) -> str:
        return "shared_table"
    
    @property
    def collection_id_column(self) -> str:
        """返回用于隔离的列名"""
        return self._collection_id_column
    
    @property
    def expected_vector_dim(self) -> Optional[int]:
        """返回预期的向量维度"""
        return self._expected_vector_dim
    
    def validate_vector_dim(
        self,
        collection_id: str,
        requested_dim: int,
        table_name: str,
    ) -> None:
        """
        验证向量维度是否与预期一致
        
        使用 single_table 策略时，所有 collection 必须使用相同的向量维度，
        因为它们共享同一张表（表的 vector 列维度是固定的）。
        
        Args:
            collection_id: Collection ID
            requested_dim: 请求的向量维度
            table_name: 目标表名（用于错误信息）
        
        Raises:
            VectorDimensionMismatchError: 维度不匹配时抛出
        """
        if self._expected_vector_dim is None:
            # 未设置预期维度，跳过验证
            return
        
        if requested_dim != self._expected_vector_dim:
            raise VectorDimensionMismatchError(
                collection_id=collection_id,
                requested_dim=requested_dim,
                expected_dim=self._expected_vector_dim,
                table_name=table_name,
            )
        
        logger.debug(
            f"SharedTableStrategy: collection_id={collection_id} "
            f"维度验证通过 (dim={requested_dim})"
        )
    
    def resolve_storage(
        self,
        collection_id: Optional[str],
        schema: str,
        base_table: str,
    ) -> StorageResolution:
        """
        单表策略：使用固定表名，添加 collection_id 过滤条件
        
        Args:
            collection_id: Collection ID（必须提供以进行隔离）
            schema: 配置的 schema
            base_table: 共享表名
        
        Returns:
            StorageResolution，包含 collection_id 过滤条件
        
        Raises:
            ValueError: 当 collection_id 为 None 时抛出
        """
        if collection_id is None:
            raise ValueError(
                "SharedTableStrategy 要求提供 collection_id 以进行租户隔离"
            )
        
        # 生成额外的 WHERE 条件
        where_clause = f"{self._collection_id_column} = %s"
        
        logger.debug(
            f"SharedTableStrategy: collection_id={collection_id}, "
            f"table={base_table}, where_extra='{where_clause}'"
        )
        
        return StorageResolution(
            schema=schema,
            table=base_table,
            where_clause_extra=where_clause,
            params_extra=[collection_id],
        )


# ============ 路由策略（混合策略） ============


class RoutingCollectionStrategy(BaseCollectionStrategy):
    """
    路由策略 - 根据规则选择使用 SharedTableStrategy 或 DefaultCollectionStrategy
    
    行为：
    - 根据 collection_id 匹配路由规则
    - 命中规则 → SharedTableStrategy（使用 shared_table，通过 collection_id 列隔离）
    - 未命中 → DefaultCollectionStrategy（每个 collection 独立表）
    
    路由规则类型：
    - allowlist: 精确匹配的 collection_id 列表
    - prefix: project_key 前缀匹配
    - regex: 正则表达式匹配
    
    使用场景：
    - 高频小型 collection 使用共享表（减少表数量）
    - 大型或特殊 collection 使用独立表（更好的隔离性）
    
    示例：
        # 配置：allowlist=["hot_proj:v1:model", "common:v1:model"], prefix=["temp_", "cache_"]
        
        "hot_proj:v1:model"       -> SharedTableStrategy (allowlist 命中)
        "temp_abc:v1:model"       -> SharedTableStrategy (prefix 命中)
        "important_proj:v1:model" -> DefaultCollectionStrategy (未命中)
    """
    
    def __init__(
        self,
        shared_table: str,
        base_table: str = "chunks",
        allowlist: Optional[List[str]] = None,
        prefix_list: Optional[List[str]] = None,
        regex_patterns: Optional[List[str]] = None,
        collection_id_column: str = "collection_id",
        expected_vector_dim: Optional[int] = None,
    ):
        """
        初始化路由策略
        
        Args:
            shared_table: 命中规则时使用的共享表名
            base_table: 未命中规则时使用的默认表名（作为 DefaultCollectionStrategy 的参数）
            allowlist: 精确匹配的 collection_id 列表
            prefix_list: project_key 前缀列表（从 collection_id 提取 project_key 进行匹配）
            regex_patterns: 正则表达式模式列表（匹配整个 collection_id）
            collection_id_column: 共享表中用于隔离的列名，默认 "collection_id"
            expected_vector_dim: 预期的向量维度（用于 SharedTableStrategy 验证）
        """
        self._shared_table = shared_table
        self._base_table = base_table
        self._allowlist = set(allowlist) if allowlist else set()
        self._prefix_list = list(prefix_list) if prefix_list else []
        self._regex_patterns: List[re.Pattern] = []
        
        # 编译正则表达式
        if regex_patterns:
            for pattern in regex_patterns:
                try:
                    self._regex_patterns.append(re.compile(pattern))
                except re.error as e:
                    logger.warning(f"无效的正则表达式 '{pattern}': {e}，已跳过")
        
        self._collection_id_column = collection_id_column
        self._expected_vector_dim = expected_vector_dim
        
        # 内部策略实例
        self._shared_strategy = SharedTableStrategy(
            collection_id_column=collection_id_column,
            expected_vector_dim=expected_vector_dim,
        )
        self._default_strategy = DefaultCollectionStrategy()
        
        logger.info(
            f"RoutingCollectionStrategy 初始化: "
            f"shared_table={shared_table}, "
            f"allowlist={len(self._allowlist)} items, "
            f"prefixes={len(self._prefix_list)} items, "
            f"regex_patterns={len(self._regex_patterns)} items"
        )
    
    @property
    def strategy_name(self) -> str:
        return "routing"
    
    @property
    def shared_table(self) -> str:
        """返回共享表名"""
        return self._shared_table
    
    @property
    def base_table(self) -> str:
        """返回默认表名"""
        return self._base_table
    
    @property
    def allowlist(self) -> set:
        """返回 allowlist"""
        return self._allowlist.copy()
    
    @property
    def prefix_list(self) -> List[str]:
        """返回前缀列表"""
        return self._prefix_list.copy()
    
    @property
    def expected_vector_dim(self) -> Optional[int]:
        """返回预期的向量维度"""
        return self._expected_vector_dim
    
    def matches_routing_rule(self, collection_id: str) -> bool:
        """
        检查 collection_id 是否命中路由规则
        
        Args:
            collection_id: Collection ID（canonical 冒号格式）
        
        Returns:
            True 表示命中规则（使用 SharedTableStrategy），False 表示未命中（使用 DefaultCollectionStrategy）
        """
        if not collection_id:
            return False
        
        # 1. 精确匹配 allowlist
        if collection_id in self._allowlist:
            logger.debug(f"RoutingStrategy: '{collection_id}' 命中 allowlist")
            return True
        
        # 2. 前缀匹配（提取 project_key）
        project_key = self._extract_project_key(collection_id)
        if project_key:
            for prefix in self._prefix_list:
                if project_key.startswith(prefix):
                    logger.debug(
                        f"RoutingStrategy: '{collection_id}' 命中 prefix '{prefix}' "
                        f"(project_key='{project_key}')"
                    )
                    return True
        
        # 3. 正则表达式匹配
        for pattern in self._regex_patterns:
            if pattern.search(collection_id):
                logger.debug(
                    f"RoutingStrategy: '{collection_id}' 命中 regex '{pattern.pattern}'"
                )
                return True
        
        logger.debug(f"RoutingStrategy: '{collection_id}' 未命中任何规则")
        return False
    
    def _extract_project_key(self, collection_id: str) -> Optional[str]:
        """
        从 collection_id 提取 project_key
        
        Collection ID 格式: {project_key}:{chunking_version}:{embedding_model_id}[:{version_tag}]
        
        Args:
            collection_id: Collection ID
        
        Returns:
            project_key 或 None（格式不符时）
        """
        if not collection_id:
            return None
        
        parts = collection_id.split(":")
        if len(parts) >= 3:
            return parts[0]
        
        return None
    
    def resolve_storage(
        self,
        collection_id: Optional[str],
        schema: str,
        base_table: str,
    ) -> StorageResolution:
        """
        根据路由规则解析存储位置
        
        Args:
            collection_id: Collection ID，可为 None
            schema: 配置的 schema
            base_table: 配置的基础表名（注意：会被策略内部的配置覆盖）
        
        Returns:
            StorageResolution
        
        行为：
        - collection_id 为 None: 使用 DefaultCollectionStrategy（返回 base_table）
        - collection_id 命中规则: 使用 SharedTableStrategy（返回 shared_table + WHERE 条件）
        - collection_id 未命中规则: 使用 DefaultCollectionStrategy（返回动态表名）
        """
        if collection_id is None:
            # 无 collection_id 时使用默认策略
            logger.debug("RoutingStrategy: collection_id 为 None，使用默认策略")
            return self._default_strategy.resolve_storage(
                collection_id=None,
                schema=schema,
                base_table=self._base_table,  # 使用策略配置的 base_table
            )
        
        if self.matches_routing_rule(collection_id):
            # 命中规则 -> SharedTableStrategy
            logger.debug(
                f"RoutingStrategy: '{collection_id}' 命中规则，"
                f"使用 SharedTableStrategy (table={self._shared_table})"
            )
            return self._shared_strategy.resolve_storage(
                collection_id=collection_id,
                schema=schema,
                base_table=self._shared_table,  # 使用配置的共享表
            )
        else:
            # 未命中规则 -> DefaultCollectionStrategy
            logger.debug(
                f"RoutingStrategy: '{collection_id}' 未命中规则，"
                f"使用 DefaultCollectionStrategy"
            )
            return self._default_strategy.resolve_storage(
                collection_id=collection_id,
                schema=schema,
                base_table=self._base_table,
            )
    
    def validate_vector_dim(
        self,
        collection_id: str,
        requested_dim: int,
        table_name: str,
    ) -> None:
        """
        验证向量维度（仅在命中路由规则时验证）
        
        Args:
            collection_id: Collection ID
            requested_dim: 请求的向量维度
            table_name: 目标表名（用于错误信息）
        
        Raises:
            VectorDimensionMismatchError: 命中规则且维度不匹配时抛出
        """
        if self.matches_routing_rule(collection_id):
            # 只有使用共享表时才需要验证维度
            self._shared_strategy.validate_vector_dim(
                collection_id=collection_id,
                requested_dim=requested_dim,
                table_name=table_name,
            )
    
    def __repr__(self) -> str:
        return (
            f"<RoutingCollectionStrategy "
            f"shared_table={self._shared_table}, "
            f"allowlist={len(self._allowlist)}, "
            f"prefixes={len(self._prefix_list)}, "
            f"regex={len(self._regex_patterns)}>"
        )


# ============ 策略工厂 ============


# 全局策略注册表
_STRATEGY_REGISTRY: Dict[str, type] = {
    "default": DefaultCollectionStrategy,
    "shared_table": SharedTableStrategy,
    "routing": RoutingCollectionStrategy,
}


def register_strategy(name: str, strategy_class: type) -> None:
    """
    注册自定义策略
    
    Args:
        name: 策略名称
        strategy_class: 策略类（需实现 CollectionStrategy 协议）
    """
    _STRATEGY_REGISTRY[name] = strategy_class
    logger.info(f"注册 Collection 策略: {name} -> {strategy_class.__name__}")


def get_strategy(
    name: str = "default",
    **kwargs,
) -> BaseCollectionStrategy:
    """
    获取策略实例
    
    Args:
        name: 策略名称（"default" 或 "shared_table"）
        **kwargs: 传递给策略构造函数的参数
    
    Returns:
        策略实例
    
    Raises:
        ValueError: 未知的策略名称
    
    Examples:
        >>> strategy = get_strategy("default")
        >>> resolution = strategy.resolve_storage("proj:v1:bge-m3", "step3", "chunks")
        
        >>> strategy = get_strategy("shared_table", collection_id_column="tenant_id")
    """
    if name not in _STRATEGY_REGISTRY:
        raise ValueError(
            f"未知的策略名称: {name}，"
            f"可用策略: {list(_STRATEGY_REGISTRY.keys())}"
        )
    
    strategy_class = _STRATEGY_REGISTRY[name]
    return strategy_class(**kwargs)


def get_default_strategy() -> DefaultCollectionStrategy:
    """
    获取默认策略实例
    
    Returns:
        DefaultCollectionStrategy 实例
    """
    return DefaultCollectionStrategy()


# ============ 便捷函数 ============


def resolve_storage(
    collection_id: Optional[str],
    schema: str,
    base_table: str,
    strategy: Optional[BaseCollectionStrategy] = None,
) -> StorageResolution:
    """
    解析存储位置的便捷函数
    
    使用指定策略或默认策略解析 collection_id 到实际存储位置。
    
    Args:
        collection_id: Collection ID（canonical 冒号格式），可为 None
        schema: 配置的 schema 名称
        base_table: 配置的基础表名
        strategy: 可选的策略实例，默认使用 DefaultCollectionStrategy
    
    Returns:
        StorageResolution 包含实际的 schema、table 及额外过滤条件
    
    Examples:
        >>> res = resolve_storage("proj:v1:bge-m3", "step3", "chunks")
        >>> res.table
        'step3_chunks_proj_v1_bge_m3'
        >>> res.where_clause_extra
        ''
    """
    if strategy is None:
        strategy = get_default_strategy()
    
    return strategy.resolve_storage(collection_id, schema, base_table)


# ============ Preflight 校验辅助函数 ============


def get_vector_column_dimension(
    connection,
    schema: str,
    table: str,
    vector_column: str = "vector",
) -> Optional[int]:
    """
    从数据库查询 vector 列的实际维度
    
    通过查询 information_schema 获取 vector 列的类型定义，
    从中解析出维度信息。
    
    Args:
        connection: psycopg 数据库连接
        schema: 表所在的 schema
        table: 表名
        vector_column: vector 列名，默认 "vector"
    
    Returns:
        向量维度（整数），如果表或列不存在则返回 None
    
    Example:
        >>> conn = psycopg.connect(dsn)
        >>> dim = get_vector_column_dimension(conn, "step3", "chunks_shared")
        >>> print(dim)  # 1536
    """
    try:
        with connection.cursor() as cur:
            # 查询 vector 列的 udt_name 和 character_maximum_length
            # pgvector 的类型格式为 vector(N)，需要从 atttypmod 解析
            cur.execute("""
                SELECT 
                    a.atttypmod
                FROM pg_catalog.pg_attribute a
                JOIN pg_catalog.pg_class c ON a.attrelid = c.oid
                JOIN pg_catalog.pg_namespace n ON c.relnamespace = n.oid
                WHERE n.nspname = %s
                  AND c.relname = %s
                  AND a.attname = %s
                  AND a.attnum > 0
                  AND NOT a.attisdropped
            """, (schema, table, vector_column))
            
            row = cur.fetchone()
            if row is None:
                logger.debug(
                    f"表 {schema}.{table} 或列 {vector_column} 不存在"
                )
                return None
            
            # atttypmod 对于 vector 类型：维度 = atttypmod
            # pgvector 的 atttypmod 直接存储维度值
            atttypmod = row[0] if isinstance(row, (list, tuple)) else row.get("atttypmod")
            
            if atttypmod is None or atttypmod < 0:
                # 尝试备用方法：从 pg_type 查询
                cur.execute("""
                    SELECT format_type(a.atttypid, a.atttypmod) as type_def
                    FROM pg_catalog.pg_attribute a
                    JOIN pg_catalog.pg_class c ON a.attrelid = c.oid
                    JOIN pg_catalog.pg_namespace n ON c.relnamespace = n.oid
                    WHERE n.nspname = %s
                      AND c.relname = %s
                      AND a.attname = %s
                """, (schema, table, vector_column))
                
                type_row = cur.fetchone()
                if type_row:
                    type_def = type_row[0] if isinstance(type_row, (list, tuple)) else type_row.get("type_def")
                    # 解析 "vector(1536)" 格式
                    if type_def:
                        match = re.search(r'vector\((\d+)\)', str(type_def))
                        if match:
                            return int(match.group(1))
                
                logger.debug(
                    f"无法从 atttypmod 获取维度: {schema}.{table}.{vector_column}"
                )
                return None
            
            logger.debug(
                f"获取到 vector 列维度: {schema}.{table}.{vector_column} = {atttypmod}"
            )
            return atttypmod
            
    except Exception as e:
        logger.warning(f"查询 vector 列维度失败: {e}")
        return None


def preflight_check_vector_dimension(
    connection,
    schema: str,
    table: str,
    expected_dim: int,
    collection_id: Optional[str] = None,
    vector_column: str = "vector",
) -> None:
    """
    Preflight 校验：检查共享表的 vector 列维度是否与配置一致
    
    如果表存在且维度不匹配，抛出 VectorDimensionMismatchError，
    提供可操作的错误信息。
    
    如果表不存在，则跳过校验（将在 initialize() 时创建）。
    
    Args:
        connection: psycopg 数据库连接
        schema: 表所在的 schema
        table: 表名
        expected_dim: 预期的向量维度（来自 STEP3_PG_VECTOR_DIM 配置）
        collection_id: 可选的 collection_id（用于错误信息）
        vector_column: vector 列名，默认 "vector"
    
    Raises:
        VectorDimensionMismatchError: 维度不匹配时抛出
    
    Example:
        >>> conn = psycopg.connect(dsn)
        >>> preflight_check_vector_dimension(
        ...     conn, "step3", "chunks_shared", 
        ...     expected_dim=1536, 
        ...     collection_id="proj:v1:bge-m3"
        ... )
    """
    actual_dim = get_vector_column_dimension(
        connection, schema, table, vector_column
    )
    
    if actual_dim is None:
        # 表或列不存在，跳过校验（将在 initialize 时创建）
        logger.debug(
            f"Preflight 校验: 表 {schema}.{table} 不存在或无 vector 列，跳过维度校验"
        )
        return
    
    if actual_dim != expected_dim:
        qualified_table = f'"{schema}"."{table}"'
        raise VectorDimensionMismatchError(
            collection_id=collection_id or "(未指定)",
            requested_dim=expected_dim,
            expected_dim=actual_dim,
            table_name=qualified_table,
            is_preflight=True,
        )
    
    logger.info(
        f"Preflight 校验通过: 表 {schema}.{table} 的 vector 列维度 = {actual_dim}，"
        f"与配置 STEP3_PG_VECTOR_DIM={expected_dim} 一致"
    )


# ============ 导出 ============


__all__ = [
    # 异常类
    "CollectionStrategyError",
    "VectorDimensionMismatchError",
    # 数据类
    "StorageResolution",
    # 策略协议和基类
    "CollectionStrategy",
    "BaseCollectionStrategy",
    # 策略实现
    "DefaultCollectionStrategy",
    "SharedTableStrategy",
    "RoutingCollectionStrategy",
    # 工厂函数
    "register_strategy",
    "get_strategy",
    "get_default_strategy",
    # 便捷函数
    "resolve_storage",
    # Preflight 校验
    "get_vector_column_dimension",
    "preflight_check_vector_dimension",
]
