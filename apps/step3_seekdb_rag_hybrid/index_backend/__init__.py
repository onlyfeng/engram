"""
index_backend - 索引后端抽象层

提供统一的索引后端接口和数据结构，支持多种后端实现（seekdb/pgvector等）。
"""

try:
    from step3_seekdb_rag_hybrid.index_backend.types import (
        ChunkDoc,
        QueryRequest,
        QueryHit,
        FilterDSL,
    )
    from step3_seekdb_rag_hybrid.index_backend.base import (
        IndexBackend,
        validate_filter_dsl,
        normalize_filter_dsl,
        FilterValidationError,
    )
    from step3_seekdb_rag_hybrid.index_backend.pgvector_backend import (
        PGVectorBackend,
        PGVectorError,
        FilterDSLTranslator,
        HybridSearchConfig,
        create_pgvector_backend,
    )
    from step3_seekdb_rag_hybrid.index_backend.pgvector_collection_strategy import (
        StorageResolution,
        CollectionStrategy,
        BaseCollectionStrategy,
        DefaultCollectionStrategy,
        SharedTableStrategy,
        register_strategy,
        get_strategy,
        get_default_strategy,
        resolve_storage,
    )
    from step3_seekdb_rag_hybrid.index_backend.seekdb_backend import (
        SeekDBBackend,
        SeekDBError,
        SeekDBConfig,
        SeekDBConnectionError,
        SeekDBCollectionError,
        CollectionVersion,
        FilterDSLToSeekDBTranslator,
        build_collection_name,
        create_seekdb_backend,
        create_seekdb_backend_from_env,
    )
except ImportError:
    from .types import (
        ChunkDoc,
        QueryRequest,
        QueryHit,
        FilterDSL,
    )
    from .base import (
        IndexBackend,
        validate_filter_dsl,
        normalize_filter_dsl,
        FilterValidationError,
    )
    from .pgvector_backend import (
        PGVectorBackend,
        PGVectorError,
        FilterDSLTranslator,
        HybridSearchConfig,
        create_pgvector_backend,
    )
    from .pgvector_collection_strategy import (
        StorageResolution,
        CollectionStrategy,
        BaseCollectionStrategy,
        DefaultCollectionStrategy,
        SharedTableStrategy,
        register_strategy,
        get_strategy,
        get_default_strategy,
        resolve_storage,
    )
    from .seekdb_backend import (
        SeekDBBackend,
        SeekDBError,
        SeekDBConfig,
        SeekDBConnectionError,
        SeekDBCollectionError,
        CollectionVersion,
        FilterDSLToSeekDBTranslator,
        build_collection_name,
        create_seekdb_backend,
        create_seekdb_backend_from_env,
    )

# 尝试导入后端工厂
try:
    from step3_seekdb_rag_hybrid.step3_backend_factory import (
        BackendType,
        PGVectorConfig,
        SeekDBConfig as SeekDBFactoryConfig,
        create_backend_from_env,
        create_backend_from_args,
        add_backend_arguments,
        get_backend_info,
        validate_backend_config,
    )
    _factory_imported = True
except ImportError:
    _factory_imported = False

__all__ = [
    # Types
    "ChunkDoc",
    "QueryRequest",
    "QueryHit",
    "FilterDSL",
    # Base
    "IndexBackend",
    "validate_filter_dsl",
    "normalize_filter_dsl",
    "FilterValidationError",
    # PGVector
    "PGVectorBackend",
    "PGVectorError",
    "FilterDSLTranslator",
    "HybridSearchConfig",
    "create_pgvector_backend",
    # PGVector Collection Strategy
    "StorageResolution",
    "CollectionStrategy",
    "BaseCollectionStrategy",
    "DefaultCollectionStrategy",
    "SharedTableStrategy",
    "register_strategy",
    "get_strategy",
    "get_default_strategy",
    "resolve_storage",
    # SeekDB
    "SeekDBBackend",
    "SeekDBError",
    "SeekDBConfig",
    "SeekDBConnectionError",
    "SeekDBCollectionError",
    "CollectionVersion",
    "FilterDSLToSeekDBTranslator",
    "build_collection_name",
    "create_seekdb_backend",
    "create_seekdb_backend_from_env",
]

# 添加工厂导出（如果可用）
if _factory_imported:
    __all__.extend([
        "BackendType",
        "PGVectorConfig",
        "SeekDBFactoryConfig",
        "create_backend_from_env",
        "create_backend_from_args",
        "add_backend_arguments",
        "get_backend_info",
        "validate_backend_config",
    ])
