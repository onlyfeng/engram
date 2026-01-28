"""
seekdb_backend.py - SeekDB 索引后端实现

基于 SeekDB 向量数据库的索引后端，支持:
- 向量相似度检索
- Metadata Filter（翻译自 QueryFilters DSL）
- 版本化的 collection 命名（chunking_version + embedding_model_id）
- 支持并行重建与回滚

Collection 命名规则:
    {namespace}_{chunking_version}_{embedding_model_id}
    例如: engram_v1_bge_m3

这种命名方式支持:
- 并行重建：新版本使用新 collection，不影响旧版本服务
- 回滚：直接切换到旧版本 collection
- 多模型：同一数据可用不同 embedding 模型建索引
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union
import time

# 导入顺序：优先尝试相对导入
_base_imported = False
_types_imported = False
_embedding_imported = False

# 尝试相对导入（测试环境）
try:
    from .base import (
        IndexBackend,
        FilterValidationError,
        validate_filter_dsl,
        normalize_filter_dsl,
    )
    _base_imported = True
except ImportError:
    pass

try:
    from .types import (
        ChunkDoc,
        QueryRequest,
        QueryHit,
        FilterDSL,
        FILTER_FIELDS,
        FILTER_OPERATORS,
    )
    _types_imported = True
except ImportError:
    pass

# 如果相对导入失败，尝试 index_backend.xxx 形式
if not _base_imported:
    try:
        from index_backend.base import (
            IndexBackend,
            FilterValidationError,
            validate_filter_dsl,
            normalize_filter_dsl,
        )
        _base_imported = True
    except ImportError:
        pass

if not _types_imported:
    try:
        from index_backend.types import (
            ChunkDoc,
            QueryRequest,
            QueryHit,
            FilterDSL,
            FILTER_FIELDS,
            FILTER_OPERATORS,
        )
        _types_imported = True
    except ImportError:
        pass

# 如果仍然失败，尝试完整路径
if not _base_imported:
    from step3_seekdb_rag_hybrid.index_backend.base import (
        IndexBackend,
        FilterValidationError,
        validate_filter_dsl,
        normalize_filter_dsl,
    )

if not _types_imported:
    from step3_seekdb_rag_hybrid.index_backend.types import (
        ChunkDoc,
        QueryRequest,
        QueryHit,
        FilterDSL,
        FILTER_FIELDS,
        FILTER_OPERATORS,
    )

# embedding_provider 导入
try:
    from embedding_provider import (
        EmbeddingProvider,
        get_embedding_provider,
    )
    _embedding_imported = True
except ImportError:
    pass

if not _embedding_imported:
    try:
        from step3_seekdb_rag_hybrid.embedding_provider import (
            EmbeddingProvider,
            get_embedding_provider,
        )
        _embedding_imported = True
    except ImportError:
        pass

if not _embedding_imported:
    # 在测试环境中可能不需要实际的 embedding_provider
    from typing import Protocol, runtime_checkable
    
    @runtime_checkable
    class EmbeddingProvider(Protocol):
        """Embedding Provider 协议"""
        @property
        def model_id(self) -> str: ...
        @property
        def dim(self) -> int: ...
        def embed_text(self, text: str) -> List[float]: ...
        def embed_texts(self, texts: List[str]) -> List[List[float]]: ...
    
    def get_embedding_provider():
        raise ImportError("embedding_provider module not available")

# collection_naming 导入
_naming_imported = False
try:
    from collection_naming import (
        make_collection_id,
        parse_collection_id,
        to_seekdb_collection_name,
        CollectionParts,
    )
    _naming_imported = True
except ImportError:
    pass

if not _naming_imported:
    try:
        from step3_seekdb_rag_hybrid.collection_naming import (
            make_collection_id,
            parse_collection_id,
            to_seekdb_collection_name,
            CollectionParts,
        )
        _naming_imported = True
    except ImportError:
        pass

logger = logging.getLogger(__name__)


# ============ 异常类 ============


class SeekDBError(Exception):
    """SeekDB 后端错误"""
    
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        self.message = message
        self.details = details or {}
        super().__init__(message)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "error": "SeekDBError",
            "message": self.message,
            "details": self.details,
        }


class SeekDBConnectionError(SeekDBError):
    """SeekDB 连接错误"""
    pass


class SeekDBCollectionError(SeekDBError):
    """SeekDB Collection 操作错误"""
    pass


# ============ 版本化 Collection 命名 ============
#
# 统一使用 collection_naming 模块进行命名管理:
# - Canonical collection_id 格式: {project_key}:{chunking_version}:{embedding_model_id}[:{version_tag}]
# - SeekDB 名称通过 to_seekdb_collection_name() 转换
#
# CollectionVersion 类保留用于兼容和内部使用


@dataclass
class CollectionVersion:
    """
    Collection 版本信息
    
    用于生成版本化的 collection 名称，支持并行重建和回滚。
    
    内部使用 collection_naming 模块进行命名转换:
    - canonical_id: {namespace}:{chunking_version}:{embedding_model_id}[:{version_tag}]
    - seekdb_name: 通过 to_seekdb_collection_name() 转换
    """
    namespace: str                   # 命名空间（如项目名，对应 project_key）
    chunking_version: str            # 分块版本（如 v1, v2）
    embedding_model_id: str          # embedding 模型标识（如 bge-m3）
    version_tag: Optional[str] = None  # 版本标签（如 20260128T120000）
    
    @staticmethod
    def _sanitize_name(name: str) -> str:
        """清理名称，只保留字母数字和下划线（用于 SeekDB 兼容）"""
        import re
        # 替换连字符为下划线
        name = name.replace("-", "_")
        # 移除其他特殊字符
        return re.sub(r'[^a-zA-Z0-9_]', '', name)
    
    @property
    def canonical_id(self) -> str:
        """
        获取 canonical collection_id（冒号格式）
        
        用于 logbook.kv 存储和跨后端统一标识。
        """
        if _naming_imported:
            return make_collection_id(
                project_key=self.namespace,
                chunking_version=self.chunking_version,
                embedding_model_id=self.embedding_model_id,
                version_tag=self.version_tag,
            )
        # 回退实现
        parts = [self.namespace, self.chunking_version, self.embedding_model_id]
        if self.version_tag:
            parts.append(self.version_tag)
        return ":".join(parts)
    
    @property
    def collection_name(self) -> str:
        """
        生成 SeekDB collection 名称（下划线格式）
        
        通过 collection_naming.to_seekdb_collection_name() 转换。
        """
        if _naming_imported:
            return to_seekdb_collection_name(self.canonical_id)
        # 回退实现（兼容旧逻辑）
        sanitized_ns = self._sanitize_name(self.namespace)
        sanitized_ver = self._sanitize_name(self.chunking_version)
        sanitized_model = self._sanitize_name(self.embedding_model_id)
        parts = [sanitized_ns, sanitized_ver, sanitized_model]
        if self.version_tag:
            parts.append(self._sanitize_name(self.version_tag))
        return "_".join(parts)
    
    @classmethod
    def from_canonical_id(cls, collection_id: str) -> "CollectionVersion":
        """
        从 canonical collection_id 创建 CollectionVersion
        
        Args:
            collection_id: 冒号格式的 collection_id
        
        Returns:
            CollectionVersion 实例
        """
        if _naming_imported:
            parts = parse_collection_id(collection_id)
            return cls(
                namespace=parts.project_key,
                chunking_version=parts.chunking_version,
                embedding_model_id=parts.embedding_model_id,
                version_tag=parts.version_tag,
            )
        # 回退实现
        parts = collection_id.split(":")
        if len(parts) < 3:
            raise ValueError(f"无效的 collection_id 格式: {collection_id}")
        return cls(
            namespace=parts[0],
            chunking_version=parts[1],
            embedding_model_id=parts[2],
            version_tag=parts[3] if len(parts) > 3 else None,
        )
    
    @classmethod
    def parse(cls, collection_name: str) -> "CollectionVersion":
        """
        从 SeekDB collection 名称解析版本信息
        
        Args:
            collection_name: SeekDB collection 名称（下划线格式）
        
        Returns:
            CollectionVersion 实例
        
        Raises:
            ValueError: 名称格式无效
        """
        parts = collection_name.rsplit("_", 2)
        if len(parts) < 3:
            raise ValueError(f"无效的 collection 名称格式: {collection_name}")
        
        # 处理 namespace 可能包含下划线的情况
        embedding_model_id = parts[-1]
        chunking_version = parts[-2]
        namespace = "_".join(parts[:-2]) if len(parts) > 3 else parts[0]
        
        return cls(
            namespace=namespace,
            chunking_version=chunking_version,
            embedding_model_id=embedding_model_id,
        )


def build_collection_name(
    namespace: str,
    chunking_version: str,
    embedding_model_id: str,
    version_tag: Optional[str] = None,
) -> str:
    """
    构建 SeekDB collection 名称
    
    推荐使用 collection_naming.to_seekdb_collection_name() 代替。
    
    Args:
        namespace: 命名空间（对应 project_key）
        chunking_version: 分块版本
        embedding_model_id: embedding 模型标识
        version_tag: 版本标签（可选）
    
    Returns:
        SeekDB collection 名称
    """
    version = CollectionVersion(
        namespace=namespace,
        chunking_version=chunking_version,
        embedding_model_id=embedding_model_id,
        version_tag=version_tag,
    )
    return version.collection_name


def build_collection_name_from_id(collection_id: str) -> str:
    """
    从 canonical collection_id 构建 SeekDB collection 名称
    
    Args:
        collection_id: 冒号格式的 collection_id
    
    Returns:
        SeekDB collection 名称
    """
    if _naming_imported:
        return to_seekdb_collection_name(collection_id)
    # 回退实现
    version = CollectionVersion.from_canonical_id(collection_id)
    return version.collection_name


# ============ Filter DSL 到 SeekDB Filter 翻译器 ============


@dataclass
class SeekDBFilter:
    """SeekDB 过滤条件"""
    filter_expr: Dict[str, Any]  # SeekDB 原生 filter 表达式


class FilterDSLToSeekDBTranslator:
    """
    Filter DSL 到 SeekDB Filter 的翻译器
    
    将统一的 Filter DSL 转换为 SeekDB 的原生 filter 语法。
    
    SeekDB Filter 语法示例:
        - 精确匹配: {"field": "value"} 或 {"field": {"$eq": "value"}}
        - 范围查询: {"field": {"$gte": 10, "$lte": 20}}
        - 列表包含: {"field": {"$in": ["a", "b"]}}
        - 前缀匹配: {"field": {"$prefix": "abc"}}
        - 布尔组合: {"$and": [...]} 或 {"$or": [...]}
    
    注意：不同版本的 SeekDB 可能有语法差异，此实现基于通用 API 设计。
    """
    
    # 允许的字段名映射（DSL 字段 -> SeekDB 字段）
    FIELD_MAPPING = {
        "project_key": "project_key",
        "module": "module",
        "source_type": "source_type",
        "source_id": "source_id",
        "owner_user_id": "owner_user_id",
        "commit_ts": "commit_ts",
    }
    
    # 操作符映射（DSL 操作符 -> SeekDB 操作符）
    OPERATOR_MAPPING = {
        "$eq": "$eq",
        "$gte": "$gte",
        "$lte": "$lte",
        "$gt": "$gt",
        "$lt": "$lt",
        "$in": "$in",
        "$prefix": "$prefix",  # SeekDB 可能使用不同语法，需按实际调整
    }
    
    def __init__(self, strict: bool = True):
        """
        初始化翻译器
        
        Args:
            strict: 严格模式，遇到未知字段时抛出异常
        """
        self.strict = strict
    
    def translate(self, filters: FilterDSL) -> SeekDBFilter:
        """
        翻译 Filter DSL 到 SeekDB Filter
        
        Args:
            filters: Filter DSL 字典
        
        Returns:
            SeekDBFilter 包含 SeekDB 原生 filter 表达式
        
        Raises:
            FilterValidationError: 过滤条件校验失败
        """
        if not filters:
            return SeekDBFilter(filter_expr={})
        
        # 先校验 DSL 格式
        validate_filter_dsl(filters, strict=self.strict)
        
        # 规范化 DSL
        normalized = normalize_filter_dsl(filters)
        
        # 翻译各字段条件
        conditions: List[Dict[str, Any]] = []
        
        for field_name, field_ops in normalized.items():
            # 校验字段名
            if field_name not in self.FIELD_MAPPING:
                if self.strict:
                    raise FilterValidationError(
                        f"不允许的过滤字段: {field_name}",
                        field=field_name,
                    )
                continue
            
            seekdb_field = self.FIELD_MAPPING[field_name]
            
            # 处理操作符字典
            if isinstance(field_ops, dict):
                field_condition = self._translate_field_ops(
                    seekdb_field, field_ops
                )
                conditions.append(field_condition)
            else:
                # 直接值应该已被 normalize_filter_dsl 转换
                raise FilterValidationError(
                    f"内部错误: 字段 {field_name} 未规范化",
                    field=field_name,
                )
        
        # 组合所有条件
        if not conditions:
            return SeekDBFilter(filter_expr={})
        
        if len(conditions) == 1:
            return SeekDBFilter(filter_expr=conditions[0])
        
        # 多个条件使用 $and 组合
        return SeekDBFilter(filter_expr={"$and": conditions})
    
    def _translate_field_ops(
        self, field_name: str, ops: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        翻译单个字段的操作符表达式
        
        Args:
            field_name: SeekDB 字段名
            ops: 操作符字典 {$op: value, ...}
        
        Returns:
            SeekDB 条件表达式
        """
        # 如果只有一个 $eq 操作符，简化为直接值
        if len(ops) == 1 and "$eq" in ops:
            return {field_name: ops["$eq"]}
        
        # 翻译操作符
        translated_ops = {}
        for op, value in ops.items():
            if op not in self.OPERATOR_MAPPING:
                raise FilterValidationError(
                    f"不支持的操作符: {op}",
                    operator=op,
                )
            
            seekdb_op = self.OPERATOR_MAPPING[op]
            
            # 特殊处理 $prefix：SeekDB 可能需要转换为正则或 LIKE 语法
            if op == "$prefix":
                # 方案1: 使用 $regex (如果 SeekDB 支持)
                # translated_ops["$regex"] = f"^{re.escape(str(value))}"
                # 方案2: 使用原生 $prefix (如果 SeekDB 支持)
                translated_ops[seekdb_op] = value
            else:
                translated_ops[seekdb_op] = value
        
        return {field_name: translated_ops}
    
    def translate_to_dict(self, filters: FilterDSL) -> Dict[str, Any]:
        """
        翻译并返回原生字典（便于序列化）
        
        Args:
            filters: Filter DSL 字典
        
        Returns:
            SeekDB 原生 filter 字典
        """
        result = self.translate(filters)
        return result.filter_expr


# ============ SeekDB 索引后端实现 ============


@dataclass
class SeekDBConfig:
    """SeekDB 连接配置"""
    host: str = "localhost"
    port: int = 19530          # SeekDB 默认端口
    api_key: Optional[str] = None
    timeout: int = 30          # 请求超时（秒）
    max_retries: int = 3       # 最大重试次数
    
    @property
    def endpoint(self) -> str:
        """获取 API endpoint"""
        return f"http://{self.host}:{self.port}"


class SeekDBBackend(IndexBackend):
    """
    SeekDB 索引后端
    
    基于 SeekDB 向量数据库实现向量检索。
    
    特性:
    - 版本化 Collection 命名（支持并行重建与回滚）
    - 安全的 Filter DSL 翻译
    - 支持 upsert 幂等操作
    - 健康检查与统计
    
    Collection 命名:
        - Canonical ID (冒号格式): {project_key}:{chunking_version}:{embedding_model_id}[:{version_tag}]
        - SeekDB 名称 (下划线格式): 通过 to_seekdb_collection_name() 转换
    """
    
    def __init__(
        self,
        config: SeekDBConfig,
        namespace: str,
        chunking_version: str,
        embedding_model_id: str,
        embedding_provider: Optional[EmbeddingProvider] = None,
        vector_dim: int = 1536,
        auto_create_collection: bool = True,
        collection_id: Optional[str] = None,
        version_tag: Optional[str] = None,
    ):
        """
        初始化 SeekDB 后端
        
        支持两种初始化方式:
        1. 指定各部分参数: namespace, chunking_version, embedding_model_id
        2. 直接指定 collection_id (canonical 冒号格式)
        
        Args:
            config: SeekDB 连接配置
            namespace: 命名空间（对应 project_key），当 collection_id 为 None 时使用
            chunking_version: 分块版本（如 v1），当 collection_id 为 None 时使用
            embedding_model_id: embedding 模型标识，当 collection_id 为 None 时使用
            embedding_provider: Embedding 服务
            vector_dim: 向量维度
            auto_create_collection: 是否自动创建 collection
            collection_id: canonical collection_id (冒号格式)，优先使用
            version_tag: 版本标签（用于全量重建）
        """
        self._config = config
        self._vector_dim = vector_dim
        self._embedding_provider = embedding_provider
        self._auto_create_collection = auto_create_collection
        self._filter_translator = FilterDSLToSeekDBTranslator(strict=True)
        
        # 版本化 collection 命名
        if collection_id is not None:
            # 从 canonical collection_id 初始化
            self._collection_version = CollectionVersion.from_canonical_id(collection_id)
        else:
            # 从各部分参数初始化
            self._collection_version = CollectionVersion(
                namespace=namespace,
                chunking_version=chunking_version,
                embedding_model_id=embedding_model_id,
                version_tag=version_tag,
            )
        
        # HTTP 客户端（懒加载）
        self._session = None
        self._initialized = False
    
    @property
    def backend_name(self) -> str:
        return "seekdb"
    
    @property
    def supports_vector_search(self) -> bool:
        return True
    
    @property
    def canonical_id(self) -> str:
        """
        获取 canonical collection_id（冒号格式）
        
        用于 logbook.kv 存储和跨后端统一标识。
        """
        return self._collection_version.canonical_id
    
    @property
    def collection_name(self) -> str:
        """
        获取 SeekDB collection 名称（下划线格式）
        """
        return self._collection_version.collection_name
    
    @property
    def collection_version(self) -> CollectionVersion:
        """获取 collection 版本信息"""
        return self._collection_version
    
    # ============ HTTP 客户端 ============
    
    def _get_session(self):
        """获取 HTTP session"""
        if self._session is None:
            try:
                import requests
            except ImportError:
                raise SeekDBError(
                    "requests 未安装，请运行: pip install requests"
                )
            self._session = requests.Session()
            if self._config.api_key:
                self._session.headers["Authorization"] = f"Bearer {self._config.api_key}"
            self._session.headers["Content-Type"] = "application/json"
        return self._session
    
    def _request(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        发送 HTTP 请求到 SeekDB
        
        Args:
            method: HTTP 方法 (GET/POST/PUT/DELETE)
            endpoint: API 端点路径
            data: 请求体数据
            params: URL 参数
        
        Returns:
            响应 JSON 数据
        
        Raises:
            SeekDBError: 请求失败时抛出
        """
        import json
        
        session = self._get_session()
        url = f"{self._config.endpoint}{endpoint}"
        
        last_error = None
        for attempt in range(self._config.max_retries):
            try:
                response = session.request(
                    method=method,
                    url=url,
                    json=data,
                    params=params,
                    timeout=self._config.timeout,
                )
                
                if response.status_code >= 400:
                    error_msg = response.text
                    try:
                        error_data = response.json()
                        error_msg = error_data.get("message", error_msg)
                    except json.JSONDecodeError:
                        pass
                    raise SeekDBError(
                        f"SeekDB 请求失败: {response.status_code} - {error_msg}",
                        details={"url": url, "status": response.status_code},
                    )
                
                return response.json()
            
            except Exception as e:
                last_error = e
                if attempt < self._config.max_retries - 1:
                    time.sleep(0.5 * (attempt + 1))  # 指数退避
                    continue
                break
        
        raise SeekDBConnectionError(
            f"SeekDB 连接失败: {last_error}",
            details={"url": url, "attempts": self._config.max_retries},
        )
    
    def _get_embedding_provider(self) -> EmbeddingProvider:
        """获取 Embedding Provider"""
        if self._embedding_provider is None:
            self._embedding_provider = get_embedding_provider()
        return self._embedding_provider
    
    # ============ 生命周期 ============
    
    def initialize(self) -> None:
        """
        初始化后端
        
        - 检查 SeekDB 连接
        - 创建 collection（如果不存在且配置允许）
        """
        if self._initialized:
            return
        
        # 检查连接
        health = self.health_check()
        if health["status"] == "unhealthy":
            raise SeekDBConnectionError(
                f"SeekDB 连接失败: {health.get('error', 'unknown')}"
            )
        
        # 创建 collection
        if self._auto_create_collection:
            self._ensure_collection_exists()
        
        self._initialized = True
        logger.info(
            f"SeekDB 后端初始化完成: collection={self.collection_name}"
        )
    
    def _ensure_collection_exists(self) -> None:
        """确保 collection 存在，不存在则创建"""
        try:
            # 检查 collection 是否存在
            collections = self._list_collections()
            if self.collection_name in collections:
                logger.debug(f"Collection 已存在: {self.collection_name}")
                return
            
            # 创建 collection
            self._create_collection()
            logger.info(f"Collection 创建成功: {self.collection_name}")
        
        except SeekDBError as e:
            raise SeekDBCollectionError(
                f"确保 collection 存在失败: {e.message}",
                details=e.details,
            )
    
    def _list_collections(self) -> List[str]:
        """列出所有 collections"""
        try:
            result = self._request("GET", "/api/v1/collections")
            return result.get("collections", [])
        except SeekDBError:
            # 如果 API 不支持列表，返回空
            return []
    
    def _create_collection(self) -> None:
        """创建 collection"""
        schema = {
            "name": self.collection_name,
            "dimension": self._vector_dim,
            "metric_type": "COSINE",  # 余弦相似度
            "fields": [
                {"name": "chunk_id", "type": "string", "primary": True},
                {"name": "content", "type": "string"},
                {"name": "project_key", "type": "string", "index": True},
                {"name": "module", "type": "string", "index": True},
                {"name": "source_type", "type": "string", "index": True},
                {"name": "source_id", "type": "string"},
                {"name": "owner_user_id", "type": "string"},
                {"name": "commit_ts", "type": "string"},
                {"name": "artifact_uri", "type": "string"},
                {"name": "sha256", "type": "string"},
                {"name": "chunk_idx", "type": "int"},
                {"name": "excerpt", "type": "string"},
                {"name": "metadata", "type": "json"},
            ],
        }
        
        self._request("POST", "/api/v1/collections", data=schema)
    
    def close(self) -> None:
        """关闭连接"""
        if self._session is not None:
            self._session.close()
            self._session = None
        self._initialized = False
    
    # ============ 文档操作 ============
    
    def upsert(self, docs: List[ChunkDoc]) -> int:
        """
        批量插入或更新文档（幂等操作）
        
        Args:
            docs: 文档列表
        
        Returns:
            成功处理的文档数量
        """
        if not docs:
            return 0
        
        if not self._initialized:
            self.initialize()
        
        # 为没有向量的文档生成向量
        texts_to_embed = []
        docs_need_vector = []
        for doc in docs:
            if doc.vector is None:
                texts_to_embed.append(doc.content)
                docs_need_vector.append(doc)
        
        if texts_to_embed:
            provider = self._get_embedding_provider()
            vectors = provider.embed_texts(texts_to_embed)
            for doc, vec in zip(docs_need_vector, vectors):
                doc.vector = vec
        
        # 构建 upsert 数据
        records = []
        for doc in docs:
            record = {
                "id": doc.chunk_id,  # 主键
                "vector": doc.vector,
                "chunk_id": doc.chunk_id,
                "content": doc.content,
                "project_key": doc.project_key,
                "module": doc.module,
                "source_type": doc.source_type,
                "source_id": doc.source_id,
                "owner_user_id": doc.owner_user_id,
                "commit_ts": doc.commit_ts or "",
                "artifact_uri": doc.artifact_uri,
                "sha256": doc.sha256,
                "chunk_idx": doc.chunk_idx,
                "excerpt": doc.excerpt,
                "metadata": doc.metadata,
            }
            records.append(record)
        
        # 执行 upsert
        try:
            result = self._request(
                "POST",
                f"/api/v1/collections/{self.collection_name}/upsert",
                data={"records": records},
            )
            processed = result.get("upserted", len(records))
            logger.info(f"upsert 完成: {processed}/{len(docs)} 文档")
            return processed
        
        except SeekDBError as e:
            logger.error(f"upsert 失败: {e.message}")
            raise
    
    def delete(self, chunk_ids: List[str]) -> int:
        """批量删除文档"""
        if not chunk_ids:
            return 0
        
        if not self._initialized:
            self.initialize()
        
        try:
            result = self._request(
                "POST",
                f"/api/v1/collections/{self.collection_name}/delete",
                data={"ids": chunk_ids},
            )
            deleted = result.get("deleted", 0)
            logger.info(f"删除完成: {deleted} 文档")
            return deleted
        
        except SeekDBError as e:
            logger.error(f"删除失败: {e.message}")
            raise
    
    def delete_by_filter(self, filters: FilterDSL) -> int:
        """根据过滤条件删除文档"""
        if not self._initialized:
            self.initialize()
        
        # 翻译 filter
        seekdb_filter = self._filter_translator.translate_to_dict(filters)
        
        try:
            result = self._request(
                "POST",
                f"/api/v1/collections/{self.collection_name}/delete",
                data={"filter": seekdb_filter},
            )
            deleted = result.get("deleted", 0)
            logger.info(f"按条件删除完成: {deleted} 文档")
            return deleted
        
        except SeekDBError as e:
            logger.error(f"按条件删除失败: {e.message}")
            raise
    
    # ============ 检索操作 ============
    
    def query(self, request: QueryRequest) -> List[QueryHit]:
        """
        执行向量检索
        
        Args:
            request: 查询请求
        
        Returns:
            命中结果列表，按相似度降序排列
        """
        if not request.validate():
            raise SeekDBError("无效的查询请求")
        
        if not self._initialized:
            self.initialize()
        
        # 获取查询向量
        if request.query_vector is not None:
            query_vector = request.query_vector
        else:
            provider = self._get_embedding_provider()
            query_vector = provider.embed_text(request.query_text)
        
        # 翻译过滤条件
        seekdb_filter = self._filter_translator.translate_to_dict(
            request.filters or {}
        )
        
        # 构建查询请求
        search_request = {
            "vector": query_vector,
            "top_k": request.top_k,
            "filter": seekdb_filter,
            "include_metadata": True,
        }
        
        if request.min_score > 0:
            search_request["min_score"] = request.min_score
        
        # 执行检索
        try:
            result = self._request(
                "POST",
                f"/api/v1/collections/{self.collection_name}/search",
                data=search_request,
            )
        except SeekDBError as e:
            logger.error(f"检索失败: {e.message}")
            raise
        
        # 转换结果
        hits = []
        for item in result.get("results", []):
            hit = QueryHit(
                chunk_id=item.get("chunk_id", item.get("id", "")),
                content=item.get("content", ""),
                score=float(item.get("score", 0.0)),
                source_type=item.get("source_type", ""),
                source_id=item.get("source_id", ""),
                artifact_uri=item.get("artifact_uri", ""),
                chunk_idx=item.get("chunk_idx", 0),
                sha256=item.get("sha256", ""),
                excerpt=item.get("excerpt", ""),
                metadata={
                    "project_key": item.get("project_key", ""),
                    "module": item.get("module", ""),
                    "owner_user_id": item.get("owner_user_id", ""),
                    "commit_ts": item.get("commit_ts"),
                    **(item.get("metadata") or {}),
                },
            )
            hits.append(hit)
        
        query_preview = (request.query_text or "")[:30]
        logger.info(f"检索完成: query='{query_preview}...', hits={len(hits)}")
        return hits
    
    # ============ 状态查询 ============
    
    def health_check(self) -> Dict[str, Any]:
        """
        健康检查
        
        检查 SeekDB 服务是否可用。
        """
        try:
            result = self._request("GET", "/api/v1/health")
            
            return {
                "status": "healthy",
                "backend": self.backend_name,
                "details": {
                    "endpoint": self._config.endpoint,
                    "collection": self.collection_name,
                    "version": result.get("version", "unknown"),
                },
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "backend": self.backend_name,
                "error": str(e),
                "details": {
                    "endpoint": self._config.endpoint,
                },
            }
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        if not self._initialized:
            self.initialize()
        
        try:
            result = self._request(
                "GET",
                f"/api/v1/collections/{self.collection_name}/stats",
            )
            
            return {
                "total_docs": result.get("count", 0),
                "index_size_bytes": result.get("size_bytes", 0),
                "collection": self.collection_name,
                "collection_version": {
                    "namespace": self._collection_version.namespace,
                    "chunking_version": self._collection_version.chunking_version,
                    "embedding_model_id": self._collection_version.embedding_model_id,
                },
                "vector_dim": self._vector_dim,
            }
        except SeekDBError:
            return {
                "total_docs": -1,
                "collection": self.collection_name,
                "error": "无法获取统计信息",
            }
    
    # ============ Collection 管理 ============
    
    def drop_collection(self) -> bool:
        """
        删除当前 collection
        
        用于版本回滚时清理旧版本。
        
        Returns:
            是否删除成功
        """
        try:
            self._request(
                "DELETE",
                f"/api/v1/collections/{self.collection_name}",
            )
            logger.info(f"Collection 删除成功: {self.collection_name}")
            return True
        except SeekDBError as e:
            logger.error(f"Collection 删除失败: {e.message}")
            return False
    
    def list_version_collections(self, namespace: str) -> List[CollectionVersion]:
        """
        列出指定命名空间下所有版本的 collections
        
        用于查看可用版本和回滚选择。
        
        Args:
            namespace: 命名空间
        
        Returns:
            CollectionVersion 列表
        """
        try:
            all_collections = self._list_collections()
            prefix = f"{CollectionVersion._sanitize_name(namespace)}_"
            
            versions = []
            for name in all_collections:
                if name.startswith(prefix):
                    try:
                        version = CollectionVersion.parse(name)
                        versions.append(version)
                    except ValueError:
                        continue
            
            return versions
        except SeekDBError:
            return []


# ============ 工厂函数 ============


def create_seekdb_backend(
    host: str = "localhost",
    port: int = 19530,
    api_key: Optional[str] = None,
    namespace: str = "default",
    chunking_version: str = "v1",
    embedding_model_id: str = "default",
    vector_dim: int = 1536,
    embedding_provider: Optional[EmbeddingProvider] = None,
    auto_create_collection: bool = True,
    collection_id: Optional[str] = None,
    version_tag: Optional[str] = None,
) -> SeekDBBackend:
    """
    创建 SeekDB 后端实例
    
    支持两种方式指定 collection:
    1. 指定 collection_id (canonical 冒号格式，优先)
    2. 指定各部分参数: namespace, chunking_version, embedding_model_id
    
    Args:
        host: SeekDB 服务器地址
        port: SeekDB 服务器端口
        api_key: API 密钥（可选）
        namespace: 命名空间（对应 project_key）
        chunking_version: 分块版本
        embedding_model_id: embedding 模型标识
        vector_dim: 向量维度
        embedding_provider: Embedding 服务
        auto_create_collection: 是否自动创建 collection
        collection_id: canonical collection_id (冒号格式)，优先使用
        version_tag: 版本标签（用于全量重建）
    
    Returns:
        SeekDBBackend 实例
    
    Example:
        # 方式1: 使用 canonical collection_id
        backend = create_seekdb_backend(
            collection_id="engram:v1:bge-m3",
        )
        
        # 方式2: 使用各部分参数
        backend = create_seekdb_backend(
            namespace="engram",
            chunking_version="v1",
            embedding_model_id="bge_m3",
        )
        
        # 带版本标签的全量重建
        backend_new = create_seekdb_backend(
            collection_id="engram:v2:bge-m3:20260128T120000",
        )
    """
    config = SeekDBConfig(
        host=host,
        port=port,
        api_key=api_key,
    )
    
    return SeekDBBackend(
        config=config,
        namespace=namespace,
        chunking_version=chunking_version,
        embedding_model_id=embedding_model_id,
        embedding_provider=embedding_provider,
        vector_dim=vector_dim,
        auto_create_collection=auto_create_collection,
        collection_id=collection_id,
        version_tag=version_tag,
    )


def create_seekdb_backend_from_env(
    namespace: str = "default",
    chunking_version: str = "v1",
    embedding_model_id: str = "default",
    collection_id: Optional[str] = None,
) -> SeekDBBackend:
    """
    从环境变量创建 SeekDB 后端
    
    环境变量:
        SEEKDB_HOST: 服务器地址（默认 localhost）
        SEEKDB_PORT: 服务器端口（默认 19530）
        SEEKDB_API_KEY: API 密钥（可选）
        SEEKDB_VECTOR_DIM: 向量维度（默认 1536）
        SEEKDB_COLLECTION_ID: canonical collection_id（可选，优先使用）
    
    Args:
        namespace: 命名空间（对应 project_key）
        chunking_version: 分块版本
        embedding_model_id: embedding 模型标识
        collection_id: canonical collection_id，优先使用
    
    Returns:
        SeekDBBackend 实例
    """
    import os
    
    # 优先使用参数传入的 collection_id，其次使用环境变量
    actual_collection_id = collection_id or os.getenv("SEEKDB_COLLECTION_ID")
    
    return create_seekdb_backend(
        host=os.getenv("SEEKDB_HOST", "localhost"),
        port=int(os.getenv("SEEKDB_PORT", "19530")),
        api_key=os.getenv("SEEKDB_API_KEY"),
        namespace=namespace,
        chunking_version=chunking_version,
        embedding_model_id=embedding_model_id,
        vector_dim=int(os.getenv("SEEKDB_VECTOR_DIM", "1536")),
        collection_id=actual_collection_id,
    )
