"""
pgvector_backend.py - PGVector 索引后端实现

基于 PostgreSQL + pgvector 扩展的索引后端，支持:
- 向量相似度检索
- 全文检索
- Hybrid 混合检索（向量 + 全文分数加权）
- 安全的 Filter DSL 翻译（参数化查询，禁止拼接）

数据库表结构 (与 09_step3_seek_index.sql 保持一致):
    -- 默认 schema: step3, 表名: chunks
    -- 索引命名规则: {schema}_{table}_{suffix}
    
    CREATE TABLE IF NOT EXISTS step3.chunks (
        chunk_id TEXT PRIMARY KEY,
        content TEXT NOT NULL,
        vector vector(1536),  -- 维度根据模型配置
        project_key TEXT,
        module TEXT,
        source_type TEXT,
        source_id TEXT,
        owner_user_id TEXT,
        commit_ts TIMESTAMP WITH TIME ZONE,
        artifact_uri TEXT,
        sha256 TEXT,
        chunk_idx INTEGER,
        excerpt TEXT,
        metadata JSONB,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
    );
    
    -- 向量索引 (IVFFlat)
    CREATE INDEX IF NOT EXISTS step3_chunks_vector_idx 
        ON step3.chunks USING ivfflat (vector vector_cosine_ops) 
        WITH (lists = 100);
    
    -- 全文搜索索引 (GIN)
    CREATE INDEX IF NOT EXISTS step3_chunks_content_fts_idx 
        ON step3.chunks USING gin (to_tsvector('simple', content));
    
    -- 过滤字段索引
    CREATE INDEX IF NOT EXISTS step3_chunks_project_key_idx ON step3.chunks (project_key);
    CREATE INDEX IF NOT EXISTS step3_chunks_module_idx ON step3.chunks (module);
    CREATE INDEX IF NOT EXISTS step3_chunks_source_type_idx ON step3.chunks (source_type);
    CREATE INDEX IF NOT EXISTS step3_chunks_commit_ts_idx ON step3.chunks (commit_ts);
    CREATE INDEX IF NOT EXISTS step3_chunks_source_id_idx ON step3.chunks (source_id);
"""

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

# 导入顺序：优先尝试相对导入，以确保测试时模块路径一致
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

# 策略模块导入
_strategy_imported = False
try:
    from .pgvector_collection_strategy import (
        StorageResolution,
        BaseCollectionStrategy,
        DefaultCollectionStrategy,
        SharedTableStrategy,
        VectorDimensionMismatchError,
        get_default_strategy,
        resolve_storage,
        get_vector_column_dimension,
        preflight_check_vector_dimension,
    )
    _strategy_imported = True
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

if not _strategy_imported:
    try:
        from index_backend.pgvector_collection_strategy import (
            StorageResolution,
            BaseCollectionStrategy,
            DefaultCollectionStrategy,
            SharedTableStrategy,
            VectorDimensionMismatchError,
            get_default_strategy,
            resolve_storage,
            get_vector_column_dimension,
            preflight_check_vector_dimension,
        )
        _strategy_imported = True
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

if not _strategy_imported:
    try:
        from step3_seekdb_rag_hybrid.index_backend.pgvector_collection_strategy import (
            StorageResolution,
            BaseCollectionStrategy,
            DefaultCollectionStrategy,
            SharedTableStrategy,
            VectorDimensionMismatchError,
            get_default_strategy,
            resolve_storage,
            get_vector_column_dimension,
            preflight_check_vector_dimension,
        )
        _strategy_imported = True
    except ImportError:
        # 回退实现：提供基本的策略类型
        from dataclasses import dataclass, field
        from typing import Protocol, runtime_checkable
        
        class VectorDimensionMismatchError(Exception):
            """向量维度不匹配错误（回退实现）"""
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
                    message = (
                        f"向量维度不匹配 (preflight 校验失败):\n"
                        f"  配置维度: {requested_dim}, 表实际维度: {expected_dim}\n"
                        f"解决方案: 设置 STEP3_PGVECTOR_COLLECTION_STRATEGY=per_table"
                    )
                else:
                    message = (
                        f"向量维度不匹配: collection '{collection_id}' 请求维度 {requested_dim}，"
                        f"但共享表 '{table_name}' 要求维度 {expected_dim}。\n"
                        f"解决方案: 设置 STEP3_PGVECTOR_COLLECTION_STRATEGY=per_table"
                    )
                super().__init__(message)
        
        @dataclass
        class StorageResolution:
            """存储解析结果（回退实现）"""
            schema: str
            table: str
            where_clause_extra: str = ""
            params_extra: List[Any] = field(default_factory=list)
            
            @property
            def qualified_table(self) -> str:
                return f'"{self.schema}"."{self.table}"'
            
            @property
            def has_extra_filter(self) -> bool:
                return bool(self.where_clause_extra)
        
        class BaseCollectionStrategy:
            """策略基类（回退实现）"""
            @property
            def strategy_name(self) -> str:
                return "default"
            
            def resolve_storage(
                self,
                collection_id: Optional[str],
                schema: str,
                base_table: str,
            ) -> StorageResolution:
                return StorageResolution(schema=schema, table=base_table)
        
        class DefaultCollectionStrategy(BaseCollectionStrategy):
            """默认策略（回退实现）"""
            pass
        
        class SharedTableStrategy(BaseCollectionStrategy):
            """单表多租户策略（回退实现）"""
            def __init__(
                self,
                collection_id_column: str = "collection_id",
                expected_vector_dim: Optional[int] = None,
            ):
                self._collection_id_column = collection_id_column
                self._expected_vector_dim = expected_vector_dim
            
            @property
            def strategy_name(self) -> str:
                return "shared_table"
            
            @property
            def expected_vector_dim(self) -> Optional[int]:
                return self._expected_vector_dim
            
            def validate_vector_dim(
                self,
                collection_id: str,
                requested_dim: int,
                table_name: str,
            ) -> None:
                if self._expected_vector_dim and requested_dim != self._expected_vector_dim:
                    raise VectorDimensionMismatchError(
                        collection_id=collection_id,
                        requested_dim=requested_dim,
                        expected_dim=self._expected_vector_dim,
                        table_name=table_name,
                    )
            
            def resolve_storage(
                self,
                collection_id: Optional[str],
                schema: str,
                base_table: str,
            ) -> StorageResolution:
                if collection_id is None:
                    raise ValueError("SharedTableStrategy 要求提供 collection_id")
                return StorageResolution(
                    schema=schema,
                    table=base_table,
                    where_clause_extra=f"{self._collection_id_column} = %s",
                    params_extra=[collection_id],
                )
        
        def get_default_strategy() -> DefaultCollectionStrategy:
            return DefaultCollectionStrategy()
        
        def resolve_storage(
            collection_id: Optional[str],
            schema: str,
            base_table: str,
            strategy: Optional[BaseCollectionStrategy] = None,
        ) -> StorageResolution:
            if strategy is None:
                strategy = get_default_strategy()
            return strategy.resolve_storage(collection_id, schema, base_table)
        
        def get_vector_column_dimension(connection, schema: str, table: str, vector_column: str = "vector") -> Optional[int]:
            """回退实现：返回 None（跳过校验）"""
            return None
        
        def preflight_check_vector_dimension(connection, schema: str, table: str, expected_dim: int, collection_id: Optional[str] = None, vector_column: str = "vector") -> None:
            """回退实现：跳过校验"""
            pass

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
    # 定义一个协议类型用于类型提示
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

logger = logging.getLogger(__name__)


# ============ 异常类 ============


class PGVectorError(Exception):
    """PGVector 后端错误"""
    
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        self.message = message
        self.details = details or {}
        super().__init__(message)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "error": "PGVectorError",
            "message": self.message,
            "details": self.details,
        }


class SQLInjectionError(PGVectorError):
    """SQL 注入攻击检测错误"""
    pass


class PGVectorExtensionError(PGVectorError):
    """pgvector 扩展未安装错误"""
    pass


# ============ 标识符校验与 SQL 安全构造 ============


# 合法标识符正则：只允许字母、数字、下划线，以字母或下划线开头
_IDENTIFIER_PATTERN = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')

# PostgreSQL 标识符最大长度
_MAX_IDENTIFIER_LENGTH = 63

# 允许的 schema 名称白名单（第二道防线）
# 即使正则校验通过，也要在白名单中才能使用
ALLOWED_SCHEMAS = frozenset({
    "public", "step3", "step3_dev", "step3_test", "step3_staging", "step3_prod",
})

# Schema 前缀白名单（第一道防线：前缀匹配）
ALLOWED_SCHEMA_PREFIXES = frozenset({
    "step3_",  # 允许所有 step3_ 开头的 schema
    "public",  # public 完全匹配
})

# 允许的表名白名单（静态表名，可选配置项）
# 优先级高于前缀检查
ALLOWED_TABLE_NAMES: frozenset = frozenset({
    "chunks", "chunks_dev", "chunks_test", "chunks_archive",
    "document_chunks", "embeddings", "vectors",
})

# 表名前缀白名单（动态表名支持）
# 格式: step3_chunks_{collection_id 转换后}
ALLOWED_TABLE_PREFIXES = frozenset({
    "step3_chunks_",  # 由 collection_naming.to_pgvector_table_name 生成
    "step3_",         # 通用 step3_ 前缀
    "chunks_",        # chunks_ 前缀（用于分环境表）
})


def _get_sql_module():
    """
    懒加载获取 psycopg.sql 模块
    
    Returns:
        psycopg.sql 模块
    
    Raises:
        PGVectorError: psycopg 未安装
    """
    try:
        from psycopg import sql
        return sql
    except ImportError:
        raise PGVectorError(
            "psycopg (v3) 未安装，请运行: pip install psycopg[binary]"
        )


def validate_identifier(name: str, kind: str = "identifier") -> str:
    """
    校验并规范化 SQL 标识符（schema, table, index 等）
    
    安全规则:
    1. 非空检查
    2. 长度限制（≤63 字符）
    3. 正则校验（只允许字母、数字、下划线，以字母或下划线开头）
    
    Args:
        name: 标识符名称
        kind: 标识符类型描述（用于错误信息）
    
    Returns:
        规范化后的标识符（小写）
    
    Raises:
        SQLInjectionError: 标识符不符合安全规范
    """
    if not name:
        raise SQLInjectionError(f"{kind} 不能为空")
    
    # 转小写并去除空白
    normalized = name.strip().lower()
    
    # 长度限制（PostgreSQL 标识符最长 63 字符）
    if len(normalized) > _MAX_IDENTIFIER_LENGTH:
        raise SQLInjectionError(
            f"{kind} 长度超过 {_MAX_IDENTIFIER_LENGTH} 字符: {name}"
        )
    
    # 正则校验
    if not _IDENTIFIER_PATTERN.match(normalized):
        raise SQLInjectionError(
            f"{kind} 包含非法字符: {name}。"
            f"只允许字母、数字、下划线，且以字母或下划线开头"
        )
    
    return normalized


def validate_schema_name(
    schema: str,
    allowed_list: Optional[frozenset] = None,
    allowed_prefixes: Optional[frozenset] = None,
) -> str:
    """
    校验 schema 名称（前缀白名单 + 正则校验 + 允许列表双重防线）
    
    校验流程:
    1. 基础正则校验（validate_identifier）
    2. 前缀白名单检查（第一道防线）
    3. 完全匹配白名单检查（第二道防线）
    
    Args:
        schema: schema 名称
        allowed_list: 可选，自定义允许列表（默认使用 ALLOWED_SCHEMAS）
        allowed_prefixes: 可选，自定义前缀列表（默认使用 ALLOWED_SCHEMA_PREFIXES）
    
    Returns:
        规范化后的 schema 名称
    
    Raises:
        SQLInjectionError: schema 不合法
    """
    # 1. 基础校验
    normalized = validate_identifier(schema, "schema")
    
    # 使用默认或自定义配置
    allowed = allowed_list if allowed_list is not None else ALLOWED_SCHEMAS
    prefixes = allowed_prefixes if allowed_prefixes is not None else ALLOWED_SCHEMA_PREFIXES
    
    # 2. 完全匹配白名单（优先级最高）
    if normalized in allowed:
        return normalized
    
    # 3. 前缀白名单检查
    for prefix in prefixes:
        if normalized.startswith(prefix) or normalized == prefix.rstrip("_"):
            logger.debug(f"schema '{normalized}' 匹配前缀 '{prefix}'")
            return normalized
    
    # 不在允许范围内
    raise SQLInjectionError(
        f"schema '{schema}' 不合法。"
        f"允许的 schema: {sorted(allowed)} 或以 {sorted(prefixes)} 开头"
    )


def validate_table_name(
    table: str,
    allow_dynamic: bool = True,
    allowed_list: Optional[frozenset] = None,
    allowed_prefixes: Optional[frozenset] = None,
) -> str:
    """
    校验表名（前缀白名单 + 正则校验 + 长度限制 + 可选允许列表）
    
    校验流程:
    1. 基础正则校验 + 长度限制（validate_identifier）
    2. 完全匹配白名单检查（如果提供）
    3. 前缀白名单检查（如果 allow_dynamic=True）
    
    支持的表名模式：
    - 静态白名单表名（chunks, chunks_dev 等）
    - 动态生成的表名（step3_chunks_* 前缀，由 collection_naming 生成）
    
    Args:
        table: 表名
        allow_dynamic: 是否允许动态表名（前缀匹配）
        allowed_list: 可选，自定义静态允许列表（默认使用 ALLOWED_TABLE_NAMES）
        allowed_prefixes: 可选，自定义前缀列表（默认使用 ALLOWED_TABLE_PREFIXES）
    
    Returns:
        规范化后的表名
    
    Raises:
        SQLInjectionError: 表名不合法
    """
    # 1. 基础校验（正则 + 长度）
    normalized = validate_identifier(table, "table_name")
    
    # 使用默认或自定义配置
    allowed = allowed_list if allowed_list is not None else ALLOWED_TABLE_NAMES
    prefixes = allowed_prefixes if allowed_prefixes is not None else ALLOWED_TABLE_PREFIXES
    
    # 2. 完全匹配白名单检查（优先级最高）
    if allowed and normalized in allowed:
        return normalized
    
    # 3. 前缀白名单检查（动态表名）
    if allow_dynamic:
        for prefix in prefixes:
            if normalized.startswith(prefix):
                logger.debug(f"表名 '{normalized}' 匹配前缀 '{prefix}'")
                return normalized
    
    # 不在允许范围内
    error_msg = f"表名 '{table}' 不合法。"
    if allowed:
        error_msg += f"允许的静态表名: {sorted(allowed)}"
    if allow_dynamic and prefixes:
        error_msg += f"，或使用以下前缀的动态表名: {sorted(prefixes)}"
    
    raise SQLInjectionError(error_msg)


def make_qualified_table_name(schema: str, table: str) -> str:
    """
    生成带 schema 的完整表名（字符串格式，用于日志等）
    
    注意：此函数返回的字符串仅用于日志和显示。
    执行 SQL 时应使用 make_qualified_identifier() 生成安全的 Composed 对象。
    
    Args:
        schema: schema 名称（已校验）
        table: 表名（已校验）
    
    Returns:
        "schema"."table" 格式的完整表名字符串
    """
    # 使用双引号包裹，防止关键字冲突
    return f'"{schema}"."{table}"'


def make_qualified_identifier(schema: str, table: str):
    """
    使用 psycopg.sql 安全构造带 schema 的表标识符
    
    返回 sql.Composed 对象，可直接用于 cur.execute()。
    
    Args:
        schema: schema 名称（已校验）
        table: 表名（已校验）
    
    Returns:
        sql.Composed 对象：Identifier(schema).Identifier(table)
    """
    sql = _get_sql_module()
    return sql.SQL("{}.{}").format(
        sql.Identifier(schema),
        sql.Identifier(table)
    )


def make_index_identifier(index_name: str):
    """
    使用 psycopg.sql 安全构造索引标识符
    
    Args:
        index_name: 索引名称（已校验）
    
    Returns:
        sql.Identifier 对象
    """
    sql = _get_sql_module()
    return sql.Identifier(index_name)


def make_schema_identifier(schema: str):
    """
    使用 psycopg.sql 安全构造 schema 标识符
    
    Args:
        schema: schema 名称（已校验）
    
    Returns:
        sql.Identifier 对象
    """
    sql = _get_sql_module()
    return sql.Identifier(schema)


# ============ Filter DSL 到 SQL 翻译器 ============


@dataclass
class SQLCondition:
    """SQL 条件表达式"""
    clause: str          # WHERE 子句片段（使用 %s 占位符）
    params: List[Any]    # 参数值列表


class FilterDSLTranslator:
    """
    Filter DSL 到 SQL WHERE 的安全翻译器
    
    核心原则：
    1. 所有值使用参数化查询（%s 占位符）
    2. 字段名白名单校验
    3. 禁止任何字符串拼接
    """
    
    # 允许的数据库列名（白名单）
    ALLOWED_COLUMNS = {
        "project_key": "project_key",
        "module": "module",
        "source_type": "source_type",
        "source_id": "source_id",
        "owner_user_id": "owner_user_id",
        "commit_ts": "commit_ts",
        "collection_id": "collection_id",
    }
    
    # 操作符到 SQL 的映射
    OPERATOR_MAP = {
        "$eq": "=",
        "$gte": ">=",
        "$lte": "<=",
        "$gt": ">",
        "$lt": "<",
        "$prefix": "LIKE",
        "$in": "IN",
    }
    
    def __init__(self, strict: bool = True):
        """
        初始化翻译器
        
        Args:
            strict: 严格模式，遇到未知字段时抛出异常
        """
        self.strict = strict
    
    def translate(self, filters: FilterDSL) -> SQLCondition:
        """
        翻译 Filter DSL 到 SQL WHERE 条件
        
        Args:
            filters: Filter DSL 字典
        
        Returns:
            SQLCondition 包含 WHERE 子句和参数列表
        
        Raises:
            FilterValidationError: 过滤条件校验失败
            SQLInjectionError: 检测到潜在的 SQL 注入
        """
        if not filters:
            return SQLCondition(clause="TRUE", params=[])
        
        # 先校验 DSL 格式
        validate_filter_dsl(filters, strict=self.strict)
        
        # 规范化 DSL
        normalized = normalize_filter_dsl(filters)
        
        # 翻译各字段条件
        clauses: List[str] = []
        params: List[Any] = []
        
        for field_name, field_ops in normalized.items():
            # 校验字段名（白名单）
            if field_name not in self.ALLOWED_COLUMNS:
                if self.strict:
                    raise FilterValidationError(
                        f"不允许的过滤字段: {field_name}",
                        field=field_name,
                    )
                continue
            
            col_name = self.ALLOWED_COLUMNS[field_name]
            
            # 处理操作符字典
            if isinstance(field_ops, dict):
                for op, value in field_ops.items():
                    clause, op_params = self._translate_operator(col_name, op, value)
                    clauses.append(clause)
                    params.extend(op_params)
            else:
                # 直接值应该已被 normalize_filter_dsl 转换
                raise FilterValidationError(
                    f"内部错误: 字段 {field_name} 未规范化",
                    field=field_name,
                )
        
        # 组合所有条件
        if not clauses:
            return SQLCondition(clause="TRUE", params=[])
        
        combined = " AND ".join(f"({c})" for c in clauses)
        return SQLCondition(clause=combined, params=params)
    
    def _translate_operator(
        self, col_name: str, operator: str, value: Any
    ) -> Tuple[str, List[Any]]:
        """
        翻译单个操作符
        
        Args:
            col_name: 列名（已校验）
            operator: 操作符
            value: 值
        
        Returns:
            (SQL 子句, 参数列表)
        """
        if operator not in self.OPERATOR_MAP:
            raise FilterValidationError(
                f"不支持的操作符: {operator}",
                operator=operator,
            )
        
        if operator == "$in":
            # IN 操作符
            if not isinstance(value, list) or not value:
                raise FilterValidationError(
                    f"$in 操作符需要非空列表",
                    operator=operator,
                )
            placeholders = ", ".join(["%s"] * len(value))
            clause = f"{col_name} IN ({placeholders})"
            return clause, list(value)
        
        elif operator == "$prefix":
            # 前缀匹配：需要转义特殊字符并添加 %
            safe_value = self._escape_like_pattern(str(value))
            clause = f"{col_name} LIKE %s"
            return clause, [safe_value + "%"]
        
        else:
            # 比较操作符
            sql_op = self.OPERATOR_MAP[operator]
            clause = f"{col_name} {sql_op} %s"
            return clause, [value]
    
    def _escape_like_pattern(self, value: str) -> str:
        """
        转义 LIKE 模式中的特殊字符
        
        防止 % 和 _ 被误解释为通配符
        """
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ============ Hybrid 检索分数计算 ============


@dataclass
class HybridSearchConfig:
    """Hybrid 检索配置"""
    vector_weight: float = 0.7      # 向量分数权重
    text_weight: float = 0.3        # 全文分数权重
    normalize_scores: bool = True   # 是否对分数进行归一化
    min_score: float = 0.0          # 最小分数阈值
    
    def __post_init__(self):
        # 确保权重归一化
        total = self.vector_weight + self.text_weight
        if abs(total - 1.0) > 0.001:
            self.vector_weight = self.vector_weight / total
            self.text_weight = self.text_weight / total


# ============ PGVector 索引后端实现 ============


class PGVectorBackend(IndexBackend):
    """
    PGVector 索引后端
    
    基于 PostgreSQL + pgvector 扩展实现向量检索和全文检索。
    
    特性:
    - 支持 Hybrid 混合检索（向量 + 全文）
    - 安全的 Filter DSL 翻译（参数化查询）
    - 支持 upsert 幂等操作
    - 支持按版本删除
    - Schema 和表名白名单校验
    - 支持 collection_id 动态表名（方案 A: 按 collection 建表）
    
    Collection 命名:
        当指定 collection_id 时，表名通过 collection_naming.to_pgvector_table_name() 生成：
        - Canonical ID (冒号格式): {project_key}:{chunking_version}:{embedding_model_id}[:{version_tag}]
        - PGVector 表名: step3_chunks_{sanitized_collection_id}
    """
    
    def __init__(
        self,
        connection_string: str,
        schema: str = "step3",
        table_name: str = "chunks",
        embedding_provider: Optional[EmbeddingProvider] = None,
        hybrid_config: Optional[HybridSearchConfig] = None,
        vector_dim: int = 1536,
        collection_id: Optional[str] = None,
        collection_strategy: Optional[BaseCollectionStrategy] = None,
    ):
        """
        初始化 PGVector 后端
        
        支持两种初始化方式:
        1. 指定 table_name: 使用静态表名（需在白名单中）
        2. 指定 collection_id: 动态生成表名（step3_chunks_ 前缀）
        
        存储策略:
        - 通过 collection_strategy 参数指定存储策略
        - 默认使用 DefaultCollectionStrategy（保持向后兼容）
        - 策略决定实际的 schema、table 以及额外的过滤条件
        
        Args:
            connection_string: PostgreSQL 连接字符串
            schema: 数据库 schema（默认 step3，白名单校验）
            table_name: 表名（默认 chunks，当 collection_id 为 None 时使用）
            embedding_provider: Embedding 服务（可选，默认使用全局实例）
            hybrid_config: Hybrid 检索配置
            vector_dim: 向量维度
            collection_id: canonical collection_id (冒号格式)，优先使用
            collection_strategy: Collection 存储策略，默认使用 DefaultCollectionStrategy
        
        Raises:
            SQLInjectionError: schema 或 table_name 不合法
        """
        self._connection_string = connection_string
        self._collection_id = collection_id
        
        # 初始化存储策略（默认使用 DefaultCollectionStrategy 保持兼容）
        self._collection_strategy = collection_strategy or get_default_strategy()
        
        # 校验 schema（作为基础 schema 传入策略）
        validated_schema = validate_schema_name(schema)
        
        # 校验基础表名
        validated_base_table = validate_table_name(table_name, allow_dynamic=True)
        
        # 通过策略解析实际的存储位置
        self._storage_resolution = self._collection_strategy.resolve_storage(
            collection_id=collection_id,
            schema=validated_schema,
            base_table=validated_base_table,
        )
        
        # 从策略解析结果获取实际的 schema 和 table
        self._schema = self._storage_resolution.schema
        self._table_name = validate_table_name(
            self._storage_resolution.table, allow_dynamic=True
        )
        self._qualified_table = make_qualified_table_name(self._schema, self._table_name)
        
        # 当使用 SharedTableStrategy 时，验证向量维度
        if (
            isinstance(self._collection_strategy, SharedTableStrategy)
            and collection_id is not None
        ):
            self._collection_strategy.validate_vector_dim(
                collection_id=collection_id,
                requested_dim=vector_dim,
                table_name=self._qualified_table,
            )
        
        # 记录策略解析结果
        if collection_id is not None:
            logger.info(
                f"使用策略 '{self._collection_strategy.strategy_name}' 解析 "
                f"collection_id '{collection_id}' -> {self._qualified_table}"
            )
            if self._storage_resolution.has_extra_filter:
                logger.debug(
                    f"策略额外过滤: {self._storage_resolution.where_clause_extra}"
                )
        
        self._vector_dim = vector_dim
        self._embedding_provider = embedding_provider
        self._hybrid_config = hybrid_config or HybridSearchConfig()
        self._filter_translator = FilterDSLTranslator(strict=True)
        
        # 连接（懒加载）
        self._conn = None
        self._pgvector_registered = False
    
    @property
    def backend_name(self) -> str:
        return "pgvector"
    
    @property
    def supports_vector_search(self) -> bool:
        return True
    
    @property
    def schema(self) -> str:
        """返回当前使用的 schema"""
        return self._schema
    
    @property
    def table_name(self) -> str:
        """返回当前使用的表名"""
        return self._table_name
    
    @property
    def qualified_table(self) -> str:
        """返回带 schema 的完整表名"""
        return self._qualified_table
    
    @property
    def collection_id(self) -> Optional[str]:
        """
        返回 canonical collection_id（冒号格式）
        
        如果未指定 collection_id 则返回 None。
        """
        return self._collection_id
    
    @property
    def canonical_id(self) -> Optional[str]:
        """
        collection_id 的别名（与 SeekDBBackend 保持一致）
        """
        return self._collection_id
    
    @property
    def collection_strategy(self) -> BaseCollectionStrategy:
        """返回当前使用的存储策略"""
        return self._collection_strategy
    
    @property
    def storage_resolution(self) -> StorageResolution:
        """返回策略解析的存储位置信息"""
        return self._storage_resolution
    
    def _get_extra_where_clause(self) -> Tuple[str, List[Any]]:
        """
        获取策略提供的额外 WHERE 条件
        
        Returns:
            (where_clause, params) 元组
        """
        return (
            self._storage_resolution.where_clause_extra,
            list(self._storage_resolution.params_extra),
        )
    
    def _merge_where_conditions(
        self,
        base_clause: str,
        base_params: List[Any],
    ) -> Tuple[str, List[Any]]:
        """
        合并基础 WHERE 条件与策略额外条件
        
        Args:
            base_clause: 基础 WHERE 子句（如来自 FilterDSL）
            base_params: 基础参数列表
        
        Returns:
            (merged_clause, merged_params) 合并后的条件和参数
        """
        extra_clause, extra_params = self._get_extra_where_clause()
        
        if not extra_clause:
            return base_clause, base_params
        
        # 合并条件：(base) AND (extra)
        if base_clause and base_clause != "TRUE":
            merged_clause = f"({base_clause}) AND ({extra_clause})"
        else:
            merged_clause = extra_clause
        
        # 合并参数：base_params + extra_params
        merged_params = list(base_params) + list(extra_params)
        
        return merged_clause, merged_params
    
    # ============ 连接管理 ============
    
    def _get_connection(self):
        """获取数据库连接（使用 psycopg3 + pgvector 注册）"""
        if self._conn is None:
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError:
                raise PGVectorError(
                    "psycopg (v3) 未安装，请运行: pip install psycopg[binary] pgvector"
                )
            
            # 注册 pgvector 类型适配器
            if not self._pgvector_registered:
                try:
                    from pgvector.psycopg import register_vector
                except ImportError:
                    raise PGVectorError(
                        "pgvector Python 包未安装或版本不兼容，请运行: pip install pgvector>=0.2.0"
                    )
            
            self._conn = psycopg.connect(
                self._connection_string,
                row_factory=dict_row,
            )
            
            # 注册 pgvector 类型
            if not self._pgvector_registered:
                try:
                    from pgvector.psycopg import register_vector
                    register_vector(self._conn)
                    self._pgvector_registered = True
                    logger.debug("pgvector 类型适配器注册成功")
                except Exception as e:
                    self._conn.close()
                    self._conn = None
                    raise PGVectorError(f"注册 pgvector 类型失败: {e}")
        
        return self._conn
    
    def _get_embedding_provider(self) -> EmbeddingProvider:
        """获取 Embedding Provider"""
        if self._embedding_provider is None:
            self._embedding_provider = get_embedding_provider()
        return self._embedding_provider
    
    def _check_pgvector_extension(self, cur) -> None:
        """
        检查 pgvector 扩展是否已安装
        
        Raises:
            PGVectorExtensionError: 扩展未安装时抛出可操作的错误
        """
        cur.execute("""
            SELECT EXISTS (
                SELECT 1 FROM pg_extension WHERE extname = 'vector'
            ) AS installed
        """)
        row = cur.fetchone()
        
        if not row or not row.get("installed"):
            raise PGVectorExtensionError(
                "pgvector 扩展未安装。请联系数据库管理员执行:\n"
                "  CREATE EXTENSION vector;\n"
                "或使用超级用户权限安装扩展。\n"
                "参考: https://github.com/pgvector/pgvector#installation"
            )
    
    def preflight_check(self) -> Dict[str, Any]:
        """
        Preflight 校验：在实际使用前检查配置与数据库状态是否一致
        
        检查内容：
        1. 数据库连接是否正常
        2. pgvector 扩展是否已安装
        3. 如果使用 SharedTableStrategy/RoutingStrategy 且目标表已存在，
           检查 vector 列维度是否与配置 STEP3_PG_VECTOR_DIM 一致
        
        Returns:
            校验结果字典，包含:
            - status: "ok" 或 "error"
            - checks: 各项检查结果
            - error: 错误信息（如有）
        
        Raises:
            VectorDimensionMismatchError: 向量维度不匹配时抛出
            PGVectorExtensionError: pgvector 扩展未安装时抛出
            PGVectorError: 其他数据库错误
        
        Example:
            >>> backend = PGVectorBackend(...)
            >>> result = backend.preflight_check()
            >>> if result["status"] == "ok":
            ...     backend.initialize()
        """
        result = {
            "status": "ok",
            "checks": {},
            "error": None,
        }
        
        try:
            conn = self._get_connection()
            result["checks"]["connection"] = "ok"
            
            with conn.cursor() as cur:
                # 1. 检查 pgvector 扩展
                self._check_pgvector_extension(cur)
                result["checks"]["pgvector_extension"] = "ok"
            
            # 2. 如果使用共享表策略，检查向量维度
            uses_shared_table = isinstance(self._collection_strategy, SharedTableStrategy)
            
            # 检查是否是 RoutingStrategy（需要动态判断）
            is_routing_strategy = (
                hasattr(self._collection_strategy, 'strategy_name') and 
                self._collection_strategy.strategy_name == "routing"
            )
            
            if uses_shared_table or is_routing_strategy:
                # 执行维度校验
                preflight_check_vector_dimension(
                    connection=conn,
                    schema=self._schema,
                    table=self._table_name,
                    expected_dim=self._vector_dim,
                    collection_id=self._collection_id,
                    vector_column="vector",
                )
                result["checks"]["vector_dimension"] = "ok"
                result["checks"]["vector_dimension_details"] = {
                    "configured_dim": self._vector_dim,
                    "table": self._qualified_table,
                    "strategy": self._collection_strategy.strategy_name,
                }
            else:
                result["checks"]["vector_dimension"] = "skipped"
                result["checks"]["vector_dimension_details"] = {
                    "reason": "per_table 策略不需要 preflight 维度校验",
                    "strategy": self._collection_strategy.strategy_name,
                }
            
            logger.info(
                f"Preflight 校验通过: table={self._qualified_table}, "
                f"strategy={self._collection_strategy.strategy_name}"
            )
            
        except VectorDimensionMismatchError as e:
            result["status"] = "error"
            result["error"] = str(e)
            result["checks"]["vector_dimension"] = "failed"
            result["checks"]["vector_dimension_details"] = {
                "configured_dim": e.requested_dim,
                "actual_dim": e.expected_dim,
                "table": e.table_name,
            }
            logger.error(f"Preflight 校验失败: {e}")
            raise
            
        except PGVectorExtensionError as e:
            result["status"] = "error"
            result["error"] = str(e)
            result["checks"]["pgvector_extension"] = "failed"
            logger.error(f"Preflight 校验失败: {e}")
            raise
            
        except Exception as e:
            result["status"] = "error"
            result["error"] = str(e)
            logger.error(f"Preflight 校验失败: {e}")
            raise PGVectorError(f"Preflight 校验失败: {e}")
        
        return result
    
    def get_actual_vector_dimension(self) -> Optional[int]:
        """
        获取数据库中 vector 列的实际维度
        
        Returns:
            向量维度（整数），如果表或列不存在则返回 None
        """
        try:
            conn = self._get_connection()
            return get_vector_column_dimension(
                connection=conn,
                schema=self._schema,
                table=self._table_name,
                vector_column="vector",
            )
        except Exception as e:
            logger.warning(f"获取 vector 列维度失败: {e}")
            return None
    
    def _ensure_schema_exists(self, cur) -> None:
        """确保 schema 存在（使用安全的 SQL 构造）"""
        sql = _get_sql_module()
        # 使用 sql.Identifier 安全构造 schema 名称
        cur.execute(
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                make_schema_identifier(self._schema)
            )
        )
    
    def initialize(self) -> None:
        """初始化后端：检查扩展、创建 schema、表和索引"""
        sql = _get_sql_module()
        conn = self._get_connection()
        
        # 预先构造安全的表标识符
        table_id = make_qualified_identifier(self._schema, self._table_name)
        
        with conn.cursor() as cur:
            # 检查 pgvector 扩展是否存在（不尝试创建）
            self._check_pgvector_extension(cur)
            
            # 确保 schema 存在
            self._ensure_schema_exists(cur)
            
            # 创建表（使用 sql.SQL 安全构造）
            # 注意：vector 维度是整数，需要先验证后嵌入
            if not isinstance(self._vector_dim, int) or self._vector_dim <= 0:
                raise PGVectorError(f"无效的向量维度: {self._vector_dim}")
            
            create_table_sql = sql.SQL("""
                CREATE TABLE IF NOT EXISTS {} (
                    chunk_id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    vector vector({}),
                    project_key TEXT,
                    module TEXT,
                    source_type TEXT,
                    source_id TEXT,
                    owner_user_id TEXT,
                    commit_ts TIMESTAMP WITH TIME ZONE,
                    artifact_uri TEXT,
                    sha256 TEXT,
                    chunk_idx INTEGER,
                    excerpt TEXT,
                    metadata JSONB,
                    collection_id TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """).format(
                table_id,
                sql.Literal(self._vector_dim)
            )
            cur.execute(create_table_sql)
            
            # 如果表已存在，添加 collection_id 列（ALTER TABLE 兼容旧表）
            cur.execute(sql.SQL("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns 
                        WHERE table_schema = %s AND table_name = %s AND column_name = 'collection_id'
                    ) THEN
                        ALTER TABLE {} ADD COLUMN collection_id TEXT;
                    END IF;
                END $$;
            """).format(table_id), (self._schema, self._table_name))
            
            # 创建索引（索引名使用校验过的标识符组合）
            # 索引名格式: {schema}_{table}_{suffix}
            # 注意：索引名也要通过 validate_identifier 校验
            index_prefix = f"{self._schema}_{self._table_name}"
            
            # 各索引名称
            vector_idx = validate_identifier(f"{index_prefix}_vector_idx", "index_name")
            fts_idx = validate_identifier(f"{index_prefix}_content_fts_idx", "index_name")
            project_key_idx = validate_identifier(f"{index_prefix}_project_key_idx", "index_name")
            module_idx = validate_identifier(f"{index_prefix}_module_idx", "index_name")
            source_type_idx = validate_identifier(f"{index_prefix}_source_type_idx", "index_name")
            commit_ts_idx = validate_identifier(f"{index_prefix}_commit_ts_idx", "index_name")
            source_id_idx = validate_identifier(f"{index_prefix}_source_id_idx", "index_name")
            
            # 向量索引
            cur.execute(
                sql.SQL("""
                    CREATE INDEX IF NOT EXISTS {} 
                    ON {} USING ivfflat (vector vector_cosine_ops) 
                    WITH (lists = 100)
                """).format(
                    make_index_identifier(vector_idx),
                    table_id
                )
            )
            
            # 全文搜索索引
            cur.execute(
                sql.SQL("""
                    CREATE INDEX IF NOT EXISTS {} 
                    ON {} USING gin (to_tsvector('simple', content))
                """).format(
                    make_index_identifier(fts_idx),
                    table_id
                )
            )
            
            # project_key 索引
            cur.execute(
                sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (project_key)").format(
                    make_index_identifier(project_key_idx),
                    table_id
                )
            )
            
            # module 索引
            cur.execute(
                sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (module)").format(
                    make_index_identifier(module_idx),
                    table_id
                )
            )
            
            # source_type 索引
            cur.execute(
                sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (source_type)").format(
                    make_index_identifier(source_type_idx),
                    table_id
                )
            )
            
            # commit_ts 索引
            cur.execute(
                sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (commit_ts)").format(
                    make_index_identifier(commit_ts_idx),
                    table_id
                )
            )
            
            # source_id 索引（用于一致性检查）
            cur.execute(
                sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (source_id)").format(
                    make_index_identifier(source_id_idx),
                    table_id
                )
            )
            
            # collection_id 索引（用于 collection 隔离）
            collection_id_idx = validate_identifier(f"{index_prefix}_collection_id_idx", "index_name")
            cur.execute(
                sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (collection_id)").format(
                    make_index_identifier(collection_id_idx),
                    table_id
                )
            )
            
            conn.commit()
        
        logger.info(
            f"PGVector 后端初始化完成: schema={self._schema}, "
            f"table={self._table_name}, qualified={self._qualified_table}"
        )
    
    def close(self) -> None:
        """关闭连接"""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            self._pgvector_registered = False
    
    # ============ Collection 过滤辅助方法 ============
    
    # 注意：collection 隔离完全依赖 strategy 提供的 where_clause_extra
    # 
    # 管理态 backend（collection_id=None）的行为边界：
    # - upsert: 允许使用 doc.collection_id，但使用 SharedTableStrategy 时必须提供
    # - query/delete_by_filter: 需要显式传入 collection_id filter 才能限定范围
    # - delete/exists/get_by_ids: 无 collection 限制，可跨 collection 操作
    # - health_check/get_stats: 统计所有数据
    
    # ============ 文档操作 ============
    
    def upsert(self, docs: List[ChunkDoc]) -> int:
        """
        批量插入或更新文档（幂等操作）
        
        使用 PostgreSQL 的 ON CONFLICT DO UPDATE 实现幂等 upsert。
        如果 chunk_id 已存在，则更新所有字段。
        
        collection_id 解析规则:
        1. 优先使用 backend 级别的 self._collection_id
        2. 如果 backend.collection_id 为 None，允许使用 doc.collection_id（管理/诊断场景）
        3. 如果 doc.collection_id 与 backend.collection_id 不一致，抛出错误（防止误操作）
        4. 使用 SharedTableStrategy 时，collection_id 不能为 None（必须明确归属）
        
        Args:
            docs: 文档列表
        
        Returns:
            成功处理的文档数量
        
        Raises:
            PGVectorError: collection_id 校验失败或写入失败
        """
        if not docs:
            return 0
        
        sql_mod = _get_sql_module()
        conn = self._get_connection()
        processed = 0
        
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
        
        # 预构造安全的表标识符
        table_id = make_qualified_identifier(self._schema, self._table_name)
        
        # 构造安全的 upsert SQL（包含 collection_id 字段）
        upsert_sql = sql_mod.SQL("""
            INSERT INTO {} (
                chunk_id, content, vector, project_key, module,
                source_type, source_id, owner_user_id, commit_ts,
                artifact_uri, sha256, chunk_idx, excerpt, metadata,
                collection_id, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, NOW()
            )
            ON CONFLICT (chunk_id) DO UPDATE SET
                content = EXCLUDED.content,
                vector = EXCLUDED.vector,
                project_key = EXCLUDED.project_key,
                module = EXCLUDED.module,
                source_type = EXCLUDED.source_type,
                source_id = EXCLUDED.source_id,
                owner_user_id = EXCLUDED.owner_user_id,
                commit_ts = EXCLUDED.commit_ts,
                artifact_uri = EXCLUDED.artifact_uri,
                sha256 = EXCLUDED.sha256,
                chunk_idx = EXCLUDED.chunk_idx,
                excerpt = EXCLUDED.excerpt,
                metadata = EXCLUDED.metadata,
                collection_id = EXCLUDED.collection_id,
                updated_at = NOW()
        """).format(table_id)
        
        # 判断是否使用 SharedTableStrategy（单表多租户模式）
        is_shared_table_strategy = isinstance(self._collection_strategy, SharedTableStrategy)
        
        # 执行 upsert
        with conn.cursor() as cur:
            for doc in docs:
                try:
                    # 解析 collection_id：优先 backend 级别，允许 doc 级覆盖
                    effective_collection_id = self._resolve_collection_id_for_doc(
                        doc, is_shared_table_strategy
                    )
                    
                    # 准备元数据 JSON
                    import json
                    metadata_json = json.dumps(doc.metadata) if doc.metadata else None
                    
                    cur.execute(upsert_sql, (
                        doc.chunk_id,
                        doc.content,
                        doc.vector,
                        doc.project_key,
                        doc.module,
                        doc.source_type,
                        doc.source_id,
                        doc.owner_user_id,
                        doc.commit_ts,
                        doc.artifact_uri,
                        doc.sha256,
                        doc.chunk_idx,
                        doc.excerpt,
                        metadata_json,
                        effective_collection_id,  # 第15个参数：collection_id
                    ))
                    processed += 1
                except PGVectorError:
                    # 重新抛出我们自己的错误
                    conn.rollback()
                    raise
                except Exception as e:
                    logger.error(
                        f"upsert 失败: chunk_id={doc.chunk_id}, "
                        f"collection_id={self._collection_id}, error={e}"
                    )
                    conn.rollback()
                    raise PGVectorError(f"upsert 失败: {e}")
            
            conn.commit()
        
        logger.info(f"upsert 完成: {processed}/{len(docs)} 文档")
        return processed
    
    def _resolve_collection_id_for_doc(
        self, doc: ChunkDoc, is_shared_table_strategy: bool
    ) -> Optional[str]:
        """
        解析单个文档的 effective collection_id
        
        规则:
        1. 如果 backend.collection_id 已设置:
           - 使用 backend.collection_id（忽略 doc.collection_id）
           - 如果 doc.collection_id 已设置且不一致，抛出错误
        2. 如果 backend.collection_id 为 None:
           - 使用 doc.collection_id（允许 doc 级覆盖）
           - 如果使用 SharedTableStrategy 且 doc.collection_id 也为 None，抛出错误
        
        Args:
            doc: 文档对象
            is_shared_table_strategy: 是否使用 SharedTableStrategy
        
        Returns:
            effective collection_id
        
        Raises:
            PGVectorError: collection_id 校验失败
        """
        backend_collection_id = self._collection_id
        doc_collection_id = getattr(doc, 'collection_id', None)
        
        if backend_collection_id is not None:
            # Backend 已设置 collection_id，使用 backend 的
            if doc_collection_id is not None and doc_collection_id != backend_collection_id:
                raise PGVectorError(
                    f"collection_id 不一致: doc.collection_id='{doc_collection_id}' "
                    f"与 backend.collection_id='{backend_collection_id}' 不匹配。"
                    f"请确保 doc.collection_id 与 backend 配置一致，或留空让 backend 自动注入。",
                    details={
                        "chunk_id": doc.chunk_id,
                        "doc_collection_id": doc_collection_id,
                        "backend_collection_id": backend_collection_id,
                    }
                )
            return backend_collection_id
        else:
            # Backend 未设置 collection_id（管理/诊断场景）
            if doc_collection_id is not None:
                # 使用 doc 级 collection_id
                return doc_collection_id
            else:
                # 两者都为 None
                if is_shared_table_strategy:
                    # SharedTableStrategy 必须有 collection_id
                    raise PGVectorError(
                        f"SharedTableStrategy 要求 collection_id 不为 None。"
                        f"当 backend.collection_id 未设置时，必须在 doc.collection_id 中显式提供。",
                        details={
                            "chunk_id": doc.chunk_id,
                            "strategy": "SharedTableStrategy",
                        }
                    )
                # 非 SharedTableStrategy 允许 collection_id 为 None
                return None
    
    def delete(self, chunk_ids: List[str]) -> int:
        """
        批量删除文档
        
        策略支持：
        - 通过 _merge_where_conditions 合并策略提供的额外过滤条件
        - SharedTableStrategy: 自动添加 collection_id 过滤
        - DefaultCollectionStrategy: 无额外过滤（管理态可跨 collection）
        """
        if not chunk_ids:
            return 0
        
        sql_mod = _get_sql_module()
        conn = self._get_connection()
        table_id = make_qualified_identifier(self._schema, self._table_name)
        
        # 基础条件：chunk_id IN (...)
        placeholders_sql = ", ".join(["%s"] * len(chunk_ids))
        base_clause = f"chunk_id IN ({placeholders_sql})"
        base_params = list(chunk_ids)
        
        # 合并策略的额外过滤条件
        merged_clause, merged_params = self._merge_where_conditions(
            base_clause, base_params
        )
        
        with conn.cursor() as cur:
            delete_sql = sql_mod.SQL("DELETE FROM {} WHERE {}").format(
                table_id,
                sql_mod.SQL(merged_clause)
            )
            cur.execute(delete_sql, merged_params)
            deleted = cur.rowcount
            conn.commit()
        
        logger.info(f"删除完成: {deleted} 文档")
        return deleted
    
    def delete_by_filter(self, filters: FilterDSL) -> int:
        """
        根据过滤条件删除文档
        
        策略支持：
        - 合并策略提供的额外过滤条件（通过 _merge_where_conditions）
        - 支持单表多租户隔离（当使用 SharedTableStrategy 时）
        """
        sql_mod = _get_sql_module()
        condition = self._filter_translator.translate(filters)
        
        # 合并策略的额外过滤条件
        merged_clause, merged_params = self._merge_where_conditions(
            condition.clause,
            condition.params,
        )
        
        conn = self._get_connection()
        table_id = make_qualified_identifier(self._schema, self._table_name)
        
        with conn.cursor() as cur:
            # 注意：merged_clause 中的列名已经过白名单校验或来自策略
            delete_sql = sql_mod.SQL("DELETE FROM {} WHERE {}").format(
                table_id,
                sql_mod.SQL(merged_clause)
            )
            cur.execute(delete_sql, merged_params)
            deleted = cur.rowcount
            conn.commit()
        
        logger.info(f"按条件删除完成: {deleted} 文档")
        return deleted
    
    def delete_by_version(self, version: str) -> int:
        """
        根据版本删除文档
        
        版本信息编码在 chunk_id 中，格式: {project}:{source_type}:{source_id}:{sha256}:{version}:{chunk_idx}
        
        策略支持：
        - 通过 _merge_where_conditions 合并策略提供的额外过滤条件
        - SharedTableStrategy: 自动添加 collection_id 过滤
        - DefaultCollectionStrategy: 无额外过滤（管理态可跨 collection）
        
        Args:
            version: 版本标识（如 "v1-2026-01"）
        
        Returns:
            删除的文档数量
        """
        sql_mod = _get_sql_module()
        conn = self._get_connection()
        table_id = make_qualified_identifier(self._schema, self._table_name)
        
        # 基础条件：按版本 LIKE 匹配
        pattern = f"%:{version}:%"
        base_clause = "chunk_id LIKE %s"
        base_params = [pattern]
        
        # 合并策略的额外过滤条件
        merged_clause, merged_params = self._merge_where_conditions(
            base_clause, base_params
        )
        
        with conn.cursor() as cur:
            delete_sql = sql_mod.SQL("DELETE FROM {} WHERE {}").format(
                table_id,
                sql_mod.SQL(merged_clause)
            )
            cur.execute(delete_sql, merged_params)
            deleted = cur.rowcount
            conn.commit()
        
        logger.info(f"按版本删除完成: version={version}, deleted={deleted}")
        return deleted
    
    # ============ 检索操作 ============
    
    def query(self, request: QueryRequest) -> List[QueryHit]:
        """
        执行 Hybrid 检索
        
        使用向量相似度和全文检索分数的加权组合：
        hybrid_score = vector_weight * vector_score + text_weight * text_score
        
        向量分数和全文分数会先进行归一化（如果配置启用）。
        
        策略支持：
        - 通过 _merge_where_conditions 合并策略提供的额外过滤条件
        - SharedTableStrategy: 自动添加 collection_id 过滤（只查询本 collection）
        - DefaultCollectionStrategy: 无额外过滤（管理态需显式传 collection_id filter）
        
        注意：
        - 使用 SharedTableStrategy 时，collection 隔离由策略自动提供
        - 使用 DefaultCollectionStrategy 且 collection_id=None 时，需要显式传入
          filters={"collection_id": "xxx"} 才能限定范围
        """
        if not request.validate():
            raise PGVectorError("无效的查询请求")
        
        sql_mod = _get_sql_module()
        
        # 获取查询向量
        if request.query_vector is not None:
            query_vector = request.query_vector
        else:
            provider = self._get_embedding_provider()
            query_vector = provider.embed_text(request.query_text)
        
        # 翻译过滤条件（用户提供的 filters，不做额外注入）
        filter_condition = self._filter_translator.translate(request.filters or {})
        
        # 合并策略的额外过滤条件（collection 隔离由此统一处理）
        merged_clause, merged_params = self._merge_where_conditions(
            filter_condition.clause,
            filter_condition.params,
        )
        
        # 构建 Hybrid 检索 SQL
        config = self._hybrid_config
        table_id = make_qualified_identifier(self._schema, self._table_name)
        
        # 归一化子查询
        # 向量分数: 1 - cosine_distance (范围 0-2，需归一化到 0-1)
        # 全文分数: ts_rank (范围不固定，需归一化)
        
        # 注意：merged_clause 中的列名已经过白名单校验或来自策略
        query_sql = sql_mod.SQL("""
            WITH base_scores AS (
                SELECT 
                    chunk_id,
                    content,
                    project_key,
                    module,
                    source_type,
                    source_id,
                    owner_user_id,
                    commit_ts,
                    artifact_uri,
                    sha256,
                    chunk_idx,
                    excerpt,
                    metadata,
                    -- 向量距离 (cosine distance, 范围 0-2)
                    (vector <=> %s::vector) AS vector_distance,
                    -- 全文检索分数
                    COALESCE(
                        ts_rank(
                            to_tsvector('simple', content),
                            plainto_tsquery('simple', %s)
                        ),
                        0
                    ) AS text_score_raw
                FROM {}
                WHERE {}
            ),
            normalized_scores AS (
                SELECT 
                    *,
                    -- 向量分数归一化: 1 - distance/2 (distance 范围 0-2)
                    (1.0 - vector_distance / 2.0) AS vector_score,
                    -- 全文分数归一化: 使用窗口函数
                    CASE 
                        WHEN MAX(text_score_raw) OVER () > 0 THEN
                            text_score_raw / MAX(text_score_raw) OVER ()
                        ELSE 0
                    END AS text_score
                FROM base_scores
            )
            SELECT 
                *,
                -- 混合分数
                (%s * vector_score + %s * text_score) AS hybrid_score
            FROM normalized_scores
            WHERE (%s * vector_score + %s * text_score) >= %s
            ORDER BY hybrid_score DESC, chunk_id ASC
            LIMIT %s
        """).format(
            table_id,
            sql_mod.SQL(merged_clause)
        )
        
        # 参数列表
        params = [
            query_vector,                    # 向量参数
            request.query_text or "",        # 全文查询
            *merged_params,                  # 合并后的过滤条件参数（包含策略额外参数）
            config.vector_weight,            # 向量权重 (第一次)
            config.text_weight,              # 文本权重 (第一次)
            config.vector_weight,            # 向量权重 (第二次, WHERE)
            config.text_weight,              # 文本权重 (第二次, WHERE)
            request.min_score,               # 最小分数
            request.top_k,                   # 返回数量
        ]
        
        conn = self._get_connection()
        with conn.cursor() as cur:
            cur.execute(query_sql, params)
            rows = cur.fetchall()
        
        # 转换结果
        hits = []
        for row in rows:
            hit = QueryHit(
                chunk_id=row["chunk_id"],
                content=row["content"],
                score=float(row["hybrid_score"]),
                source_type=row["source_type"] or "",
                source_id=row["source_id"] or "",
                artifact_uri=row["artifact_uri"] or "",
                chunk_idx=row["chunk_idx"] or 0,
                sha256=row["sha256"] or "",
                excerpt=row["excerpt"] or "",
                metadata={
                    "project_key": row["project_key"],
                    "module": row["module"],
                    "owner_user_id": row["owner_user_id"],
                    "commit_ts": str(row["commit_ts"]) if row["commit_ts"] else None,
                    "vector_score": float(row["vector_score"]),
                    "text_score": float(row["text_score"]),
                    **(row["metadata"] or {}),
                },
            )
            hits.append(hit)
        
        logger.info(f"检索完成: query='{request.query_text[:30]}...', hits={len(hits)}")
        return hits
    
    # ============ 一致性检查支持 ============
    
    def get_by_ids(self, chunk_ids: List[str]) -> List[ChunkDoc]:
        """
        批量获取文档
        
        策略支持：
        - 通过 _merge_where_conditions 合并策略提供的额外过滤条件
        - SharedTableStrategy: 自动添加 collection_id 过滤
        - DefaultCollectionStrategy: 无额外过滤（管理态可跨 collection）
        
        Args:
            chunk_ids: chunk_id 列表
        
        Returns:
            存在的文档列表（ChunkDoc 对象）
        """
        if not chunk_ids:
            return []
        
        sql_mod = _get_sql_module()
        conn = self._get_connection()
        table_id = make_qualified_identifier(self._schema, self._table_name)
        
        # 基础条件：chunk_id IN (...)
        placeholders_sql = ", ".join(["%s"] * len(chunk_ids))
        base_clause = f"chunk_id IN ({placeholders_sql})"
        base_params = list(chunk_ids)
        
        # 合并策略的额外过滤条件
        merged_clause, merged_params = self._merge_where_conditions(
            base_clause, base_params
        )
        
        with conn.cursor() as cur:
            select_sql = sql_mod.SQL("""
                SELECT 
                    chunk_id, content, vector, project_key, module,
                    source_type, source_id, owner_user_id, commit_ts,
                    artifact_uri, sha256, chunk_idx, excerpt, metadata
                FROM {}
                WHERE {}
            """).format(table_id, sql_mod.SQL(merged_clause))
            cur.execute(select_sql, merged_params)
            rows = cur.fetchall()
        
        docs = []
        for row in rows:
            # 将 vector 转换为 list（如果存在）
            vector_data = row.get("vector")
            if vector_data is not None:
                # pgvector 返回的可能是 numpy array 或 list
                if hasattr(vector_data, 'tolist'):
                    vector_data = vector_data.tolist()
                elif not isinstance(vector_data, list):
                    vector_data = list(vector_data)
            
            doc = ChunkDoc(
                chunk_id=row["chunk_id"],
                content=row["content"],
                vector=vector_data,
                project_key=row.get("project_key"),
                module=row.get("module"),
                source_type=row.get("source_type"),
                source_id=row.get("source_id"),
                owner_user_id=row.get("owner_user_id"),
                commit_ts=str(row["commit_ts"]) if row.get("commit_ts") else None,
                artifact_uri=row.get("artifact_uri"),
                sha256=row.get("sha256"),
                chunk_idx=row.get("chunk_idx"),
                excerpt=row.get("excerpt"),
                metadata=row.get("metadata") or {},
            )
            docs.append(doc)
        
        return docs
    
    def exists(self, chunk_ids: List[str]) -> Dict[str, bool]:
        """
        批量检查文档是否存在
        
        策略支持：
        - 通过 _merge_where_conditions 合并策略提供的额外过滤条件
        - SharedTableStrategy: 自动添加 collection_id 过滤
        - DefaultCollectionStrategy: 无额外过滤（管理态可跨 collection）
        
        Args:
            chunk_ids: chunk_id 列表
        
        Returns:
            {chunk_id: exists} 的字典
        """
        if not chunk_ids:
            return {}
        
        sql_mod = _get_sql_module()
        conn = self._get_connection()
        table_id = make_qualified_identifier(self._schema, self._table_name)
        
        # 基础条件：chunk_id IN (...)
        placeholders_sql = ", ".join(["%s"] * len(chunk_ids))
        base_clause = f"chunk_id IN ({placeholders_sql})"
        base_params = list(chunk_ids)
        
        # 合并策略的额外过滤条件
        merged_clause, merged_params = self._merge_where_conditions(
            base_clause, base_params
        )
        
        with conn.cursor() as cur:
            select_sql = sql_mod.SQL("""
                SELECT chunk_id FROM {}
                WHERE {}
            """).format(table_id, sql_mod.SQL(merged_clause))
            cur.execute(select_sql, merged_params)
            rows = cur.fetchall()
        
        found_ids = {row["chunk_id"] for row in rows}
        return {cid: cid in found_ids for cid in chunk_ids}
    
    def get_chunk_metadata(self, chunk_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        批量获取文档元数据
        
        策略支持：
        - 通过 _merge_where_conditions 合并策略提供的额外过滤条件
        - SharedTableStrategy: 自动添加 collection_id 过滤
        - DefaultCollectionStrategy: 无额外过滤（管理态可跨 collection）
        
        Args:
            chunk_ids: chunk_id 列表
        
        Returns:
            {chunk_id: metadata_dict} 的字典
            metadata_dict 包含: sha256, source_id, artifact_uri, project_key 等
        """
        if not chunk_ids:
            return {}
        
        sql_mod = _get_sql_module()
        conn = self._get_connection()
        table_id = make_qualified_identifier(self._schema, self._table_name)
        
        # 基础条件：chunk_id IN (...)
        placeholders_sql = ", ".join(["%s"] * len(chunk_ids))
        base_clause = f"chunk_id IN ({placeholders_sql})"
        base_params = list(chunk_ids)
        
        # 合并策略的额外过滤条件
        merged_clause, merged_params = self._merge_where_conditions(
            base_clause, base_params
        )
        
        with conn.cursor() as cur:
            select_sql = sql_mod.SQL("""
                SELECT 
                    chunk_id, sha256, source_id, source_type,
                    artifact_uri, project_key, module, chunk_idx
                FROM {}
                WHERE {}
            """).format(table_id, sql_mod.SQL(merged_clause))
            cur.execute(select_sql, merged_params)
            rows = cur.fetchall()
        
        return {
            row["chunk_id"]: {
                "sha256": row.get("sha256"),
                "source_id": row.get("source_id"),
                "source_type": row.get("source_type"),
                "artifact_uri": row.get("artifact_uri"),
                "project_key": row.get("project_key"),
                "module": row.get("module"),
                "chunk_idx": row.get("chunk_idx"),
            }
            for row in rows
        }
    
    def count_by_source(self, source_type: str, source_id: str) -> int:
        """
        统计指定来源的文档数量
        
        策略支持：
        - 通过 _merge_where_conditions 合并策略提供的额外过滤条件
        - SharedTableStrategy: 自动添加 collection_id 过滤
        - DefaultCollectionStrategy: 无额外过滤（管理态可跨 collection）
        
        Args:
            source_type: 来源类型
            source_id: 来源标识
        
        Returns:
            文档数量
        """
        sql_mod = _get_sql_module()
        conn = self._get_connection()
        table_id = make_qualified_identifier(self._schema, self._table_name)
        
        # 基础条件：source_type 和 source_id
        base_clause = "source_type = %s AND source_id = %s"
        base_params = [source_type, source_id]
        
        # 合并策略的额外过滤条件
        merged_clause, merged_params = self._merge_where_conditions(
            base_clause, base_params
        )
        
        with conn.cursor() as cur:
            count_sql = sql_mod.SQL("""
                SELECT COUNT(*) as count 
                FROM {}
                WHERE {}
            """).format(table_id, sql_mod.SQL(merged_clause))
            cur.execute(count_sql, merged_params)
            row = cur.fetchone()
            return row["count"] if row else 0
    
    # ============ 状态查询 ============
    
    def health_check(self) -> Dict[str, Any]:
        """
        健康检查
        
        策略支持：
        - 通过 _merge_where_conditions 合并策略提供的额外过滤条件
        - SharedTableStrategy: 统计本 collection 文档数
        - DefaultCollectionStrategy: 统计所有文档数（管理态）
        """
        try:
            sql_mod = _get_sql_module()
            conn = self._get_connection()
            table_id = make_qualified_identifier(self._schema, self._table_name)
            
            # 合并策略的额外过滤条件
            merged_clause, merged_params = self._merge_where_conditions("TRUE", [])
            
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                count_sql = sql_mod.SQL("SELECT COUNT(*) as count FROM {} WHERE {}").format(
                    table_id,
                    sql_mod.SQL(merged_clause)
                )
                cur.execute(count_sql, merged_params)
                row = cur.fetchone()
                doc_count = row["count"] if row else 0
            
            return {
                "status": "healthy",
                "backend": self.backend_name,
                "details": {
                    "schema": self._schema,
                    "table": self._table_name,
                    "qualified_table": self._qualified_table,
                    "collection_id": self._collection_id,
                    "canonical_id": self._collection_id,  # 与 collection_id 相同，便于统一访问
                    "doc_count": doc_count,
                    "vector_dim": self._vector_dim,
                    "strategy": self._collection_strategy.strategy_name,
                },
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "backend": self.backend_name,
                "error": str(e),
            }
    
    def get_stats(self) -> Dict[str, Any]:
        """
        获取统计信息
        
        策略支持：
        - 通过 _merge_where_conditions 合并策略提供的额外过滤条件
        - SharedTableStrategy: 统计本 collection 数据
        - DefaultCollectionStrategy: 统计所有数据（管理态）
        """
        sql_mod = _get_sql_module()
        conn = self._get_connection()
        table_id = make_qualified_identifier(self._schema, self._table_name)
        
        # 合并策略的额外过滤条件
        merged_clause, merged_params = self._merge_where_conditions("TRUE", [])
        
        with conn.cursor() as cur:
            # 文档总数（按策略过滤）
            count_sql = sql_mod.SQL("SELECT COUNT(*) as count FROM {} WHERE {}").format(
                table_id,
                sql_mod.SQL(merged_clause)
            )
            cur.execute(count_sql, merged_params)
            total_docs = cur.fetchone()["count"]
            
            # 表大小（使用 regclass 转换完整表名，参数化传递）
            # 注意：表大小不按 collection 过滤，因为是整个表的大小
            cur.execute("""
                SELECT pg_total_relation_size(%s::regclass) as size
            """, (self._qualified_table,))
            size_bytes = cur.fetchone()["size"]
            
            # 按来源类型统计（按策略过滤）
            group_sql = sql_mod.SQL("""
                SELECT source_type, COUNT(*) as count 
                FROM {} 
                WHERE {}
                GROUP BY source_type
            """).format(table_id, sql_mod.SQL(merged_clause))
            cur.execute(group_sql, merged_params)
            by_source = {row["source_type"]: row["count"] for row in cur.fetchall()}
        
        return {
            "total_docs": total_docs,
            "index_size_bytes": size_bytes,
            "by_source_type": by_source,
            "schema": self._schema,
            "table": self._table_name,
            "collection_id": self._collection_id,
            "canonical_id": self._collection_id,  # 与 collection_id 相同，便于统一访问
            "vector_dim": self._vector_dim,
            "strategy": self._collection_strategy.strategy_name,
            "hybrid_config": {
                "vector_weight": self._hybrid_config.vector_weight,
                "text_weight": self._hybrid_config.text_weight,
            },
        }


# ============ 工厂函数 ============


def create_pgvector_backend(
    connection_string: str,
    schema: str = "step3",
    table_name: str = "chunks",
    vector_dim: int = 1536,
    vector_weight: float = 0.7,
    text_weight: float = 0.3,
    embedding_provider: Optional[EmbeddingProvider] = None,
    collection_id: Optional[str] = None,
    collection_strategy: Optional[BaseCollectionStrategy] = None,
) -> PGVectorBackend:
    """
    创建 PGVector 后端实例
    
    支持两种方式指定表：
    1. 指定 collection_id (canonical 冒号格式，优先)
    2. 指定 table_name（需在白名单中）
    
    存储策略：
    - collection_strategy 参数指定存储策略
    - 默认使用 DefaultCollectionStrategy（保持向后兼容）
    - 可使用 SharedTableStrategy 实现单表多租户
    
    Args:
        connection_string: PostgreSQL 连接字符串
        schema: 数据库 schema（默认 step3）
        table_name: 表名（默认 chunks，当 collection_id 为 None 时使用）
        vector_dim: 向量维度
        vector_weight: 向量分数权重
        text_weight: 全文分数权重
        embedding_provider: Embedding 服务
        collection_id: canonical collection_id (冒号格式)，优先使用
        collection_strategy: Collection 存储策略，默认使用 DefaultCollectionStrategy
    
    Returns:
        PGVectorBackend 实例
    
    Raises:
        SQLInjectionError: schema 或 table_name 不合法
    
    Example:
        # 方式1: 使用 canonical collection_id（默认策略，独立表）
        backend = create_pgvector_backend(
            connection_string="postgresql://...",
            collection_id="engram:v1:bge-m3",
        )
        # 生成表名: step3_chunks_engram_v1_bge_m3
        
        # 方式2: 使用静态表名
        backend = create_pgvector_backend(
            connection_string="postgresql://...",
            table_name="chunks",
        )
        
        # 方式3: 使用 SharedTableStrategy（单表多租户）
        from index_backend.pgvector_collection_strategy import SharedTableStrategy
        backend = create_pgvector_backend(
            connection_string="postgresql://...",
            collection_id="engram:v1:bge-m3",
            collection_strategy=SharedTableStrategy(),
        )
    """
    config = HybridSearchConfig(
        vector_weight=vector_weight,
        text_weight=text_weight,
    )
    
    return PGVectorBackend(
        connection_string=connection_string,
        schema=schema,
        table_name=table_name,
        embedding_provider=embedding_provider,
        hybrid_config=config,
        vector_dim=vector_dim,
        collection_id=collection_id,
        collection_strategy=collection_strategy,
    )
