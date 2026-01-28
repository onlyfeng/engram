"""
step3_backend_factory.py - Step3 索引后端工厂模块

从环境变量或 CLI 参数选择并初始化索引后端（pgvector/seekdb）。

================================================================================
Collection/Backend 契约结论（2026-01-28 整理）
================================================================================

一、Canonical Collection ID 格式
    {project_key}:{chunking_version}:{embedding_model_id}[:{version_tag}]
    
    例如:
    - default:v1:bge-m3              # 增量同步使用
    - proj1:v2:bge-m3:20260128T120000  # 全量重建带版本标签
    
    生成函数: collection_naming.make_collection_id()
    解析函数: collection_naming.parse_collection_id()

二、各后端的 Collection 映射
    1. PGVector:
       - 表名 = to_pgvector_table_name(collection_id)
       - 格式: step3_chunks_{sanitized_id}
       - 例如: step3_chunks_proj1_v2_bge_m3
       - 索引名: {schema}_{table_name}_{suffix}
       
    2. SeekDB:
       - collection 名 = to_seekdb_collection_name(collection_id)
       - 格式: {sanitized_id}
       - 例如: proj1_v2_bge_m3

三、后端工厂的 collection_id 传递契约
    1. 调用者（seek_indexer/seek_query）通过 resolve_collection_id() 解析目标 collection
       解析优先级: explicit_collection_id > active_collection > default
       
    2. 调用 create_backend_from_env(collection_id=...) 或 create_backend_from_args(args, collection_id=...)
       传入 canonical collection_id（冒号格式）
       
    3. 工厂函数将 collection_id 透传给后端构造函数
       后端内部负责调用 to_*_table_name() 转换为具体的表名/collection名称
       
    4. 后端实例暴露 canonical_id 属性，供调用者检查一致性

四、调用者的责任
    1. seek_indexer/seek_query 需要检查 backend.canonical_id 是否与目标 collection 一致
    2. 如不一致，需要调用 create_backend_from_env(collection_id=...) 重建后端实例
    3. 全量重建（full mode）需要使用 make_version_tag() 生成新版本标签

五、必须改动的函数签名清单（当前均已支持 collection_id 参数）
    - create_backend_from_env(collection_id: Optional[str] = None)
    - create_backend_from_args(args, collection_id: Optional[str] = None)
    - create_pgvector_backend(collection_id: Optional[str] = None)
    - create_seekdb_backend(collection_id: Optional[str] = None)
    - PGVectorBackend.__init__(collection_id: Optional[str] = None)
    - SeekDBBackend.__init__(collection_id: Optional[str] = None)

六、潜在改进点（待评估）
    1. chunking_version/embedding_model_id 参数冗余：
       当 collection_id 已指定时，这些参数可从 collection_id 解析得到，
       建议后续版本中简化为仅接收 collection_id 或拆分参数二选一
       
    2. 表名前缀白名单扩展：
       pgvector_backend.ALLOWED_TABLE_PREFIXES 需确保包含动态生成的表名前缀

================================================================================

环境变量配置:
    # 后端选择
    STEP3_INDEX_BACKEND=pgvector|seekdb   # 默认 pgvector

    # PGVector 后端配置
    # DSN 解析优先级（从高到低）:
    #   1. STEP3_PGVECTOR_DSN（推荐，显式配置）
    #   2. 标准 PG 变量组合: PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE
    #   3. POSTGRES_DSN（仅当 STEP3_ALLOW_POSTGRES_DSN=1 时读取）
    #   4. POSTGRES_* 变量组合: POSTGRES_HOST/PORT/USER/PASSWORD/DB（fallback，会打印提示）
    STEP3_PGVECTOR_DSN=postgresql://user:pass@host:5432/dbname
    STEP3_ALLOW_POSTGRES_DSN=0|1          # 是否允许读取 POSTGRES_DSN（默认 0，避免误用非 Step3 权限账号）
    STEP3_PG_SCHEMA=step3                 # 默认 step3
    STEP3_PG_TABLE=chunks                 # 默认 chunks
    STEP3_PG_VECTOR_DIM=1536              # 默认 1536
    STEP3_VECTOR_WEIGHT=0.7               # 默认 0.7
    STEP3_TEXT_WEIGHT=0.3                 # 默认 0.3
    STEP3_PGVECTOR_COLLECTION_STRATEGY=per_table|single_table|routing  # 默认 single_table
        # per_table: 每个 collection 独立表（方案 A）
        # single_table: 所有 collection 共享一张表（方案 B，默认，通过 collection_id 列隔离）
        # routing: 路由策略，根据规则选择 shared_table 或 per_table
        # 废弃的值别名（会触发警告）:
        #   single -> single_table, shared -> single_table
        #   per_project -> per_table, per-collection -> per_table
    
    # Routing 策略配置（仅当 STEP3_PGVECTOR_COLLECTION_STRATEGY=routing 时生效）
    STEP3_PGVECTOR_ROUTING_SHARED_TABLE=chunks_shared  # 路由命中时使用的共享表名
    STEP3_PGVECTOR_COLLECTION_ROUTING_ALLOWLIST=proj1:v1:model,proj2:v1:model  # 精确匹配列表（逗号分隔）
    STEP3_PGVECTOR_COLLECTION_ROUTING_PREFIX=hot_,temp_,cache_  # project_key 前缀列表（逗号分隔）
    STEP3_PGVECTOR_COLLECTION_ROUTING_REGEX=^test_.*,.*_staging$  # 正则表达式列表（逗号分隔）

    # SeekDB 后端配置
    SEEKDB_HOST=localhost                 # 默认 localhost
    SEEKDB_PORT=19530                     # 默认 19530
    SEEKDB_API_KEY=                       # 可选
    SEEKDB_NAMESPACE=engram               # 默认 engram
    SEEKDB_VECTOR_DIM=1536                # 默认 1536

使用:
    from step3_backend_factory import create_backend_from_env, BackendType

    # 自动从环境变量创建后端
    backend = create_backend_from_env()

    # 指定后端类型
    backend = create_backend_from_env(backend_type=BackendType.PGVECTOR)

    # CLI 参数添加
    add_backend_arguments(parser)
    args = parser.parse_args()
    backend = create_backend_from_args(args)
"""

import argparse
import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, TYPE_CHECKING

# 导入环境变量兼容层
try:
    from step3_seekdb_rag_hybrid.env_compat import get_str, get_choice
except ImportError:
    from env_compat import get_str, get_choice

if TYPE_CHECKING:
    from step3_seekdb_rag_hybrid.index_backend.base import IndexBackend
    from step3_seekdb_rag_hybrid.embedding_provider import EmbeddingProvider

logger = logging.getLogger(__name__)


# ============ 后端类型枚举 ============


class BackendType(str, Enum):
    """索引后端类型"""
    PGVECTOR = "pgvector"
    SEEKDB = "seekdb"
    
    @classmethod
    def from_string(cls, value: str) -> "BackendType":
        """从字符串解析后端类型"""
        value = value.lower().strip()
        if value in ("pgvector", "pg", "postgres", "postgresql"):
            return cls.PGVECTOR
        elif value in ("seekdb", "seek"):
            return cls.SEEKDB
        else:
            raise ValueError(f"未知的后端类型: {value}，支持: pgvector, seekdb")


# ============ 配置数据类 ============


# Collection 策略类型常量
COLLECTION_STRATEGY_PER_TABLE = "per_table"     # 每个 collection 独立表
COLLECTION_STRATEGY_SINGLE_TABLE = "single_table"  # 共享表，通过 collection_id 列隔离
COLLECTION_STRATEGY_ROUTING = "routing"         # 路由策略：根据规则选择 shared_table 或 per_table


# ============ 双写配置 ============


@dataclass
class DualWriteConfig:
    """
    双写配置
    
    当启用双写时，indexer 会同时写入 primary 后端和 shadow 后端。
    primary 使用当前配置的策略，shadow 使用另一种策略（per_table 或 single_table）。
    
    环境变量:
        STEP3_PGVECTOR_DUAL_WRITE=1               # 启用双写
        STEP3_PGVECTOR_SHADOW_STRATEGY=single_table  # shadow 后端策略（默认与 primary 相反）
        STEP3_PGVECTOR_DUAL_WRITE_DRY_RUN=1       # 双写 dry-run 模式（shadow 不实际写入）
    """
    enabled: bool = False                          # 是否启用双写
    shadow_strategy: str = ""                      # shadow 后端策略（空表示自动选择相反策略）
    dry_run: bool = False                          # 双写 dry-run 模式
    shadow_table: str = "chunks_shadow"            # shadow 表名（当使用 per_table 策略时）
    
    @classmethod
    def from_env(cls, primary_strategy: str = COLLECTION_STRATEGY_PER_TABLE) -> "DualWriteConfig":
        """
        从环境变量加载双写配置
        
        Args:
            primary_strategy: primary 后端当前使用的策略，用于自动选择相反的 shadow 策略
        
        Returns:
            DualWriteConfig 实例
        """
        enabled_str = os.getenv("STEP3_PGVECTOR_DUAL_WRITE", "0").lower()
        enabled = enabled_str in ("1", "true", "yes")
        
        dry_run_str = os.getenv("STEP3_PGVECTOR_DUAL_WRITE_DRY_RUN", "0").lower()
        dry_run = dry_run_str in ("1", "true", "yes")
        
        # 获取 shadow 策略，默认选择与 primary 相反的策略
        shadow_strategy = os.getenv("STEP3_PGVECTOR_SHADOW_STRATEGY", "").lower().strip()
        if not shadow_strategy:
            # 自动选择相反策略
            if primary_strategy == COLLECTION_STRATEGY_SINGLE_TABLE:
                shadow_strategy = COLLECTION_STRATEGY_PER_TABLE
            else:
                shadow_strategy = COLLECTION_STRATEGY_SINGLE_TABLE
        
        # 验证策略值
        valid_strategies = (COLLECTION_STRATEGY_PER_TABLE, COLLECTION_STRATEGY_SINGLE_TABLE)
        if shadow_strategy not in valid_strategies:
            logger.warning(
                f"无效的 STEP3_PGVECTOR_SHADOW_STRATEGY 值 '{shadow_strategy}'，"
                f"使用默认值 '{COLLECTION_STRATEGY_SINGLE_TABLE}'"
            )
            shadow_strategy = COLLECTION_STRATEGY_SINGLE_TABLE
        
        # shadow_table 推导逻辑:
        # 1. 若 STEP3_PGVECTOR_SHADOW_TABLE 显式设置，优先使用
        # 2. 若未设置且 shadow_strategy==single_table:
        #    - 若 STEP3_PGVECTOR_ROUTING_SHARED_TABLE 存在，使用它
        #    - 否则回退到 "chunks_shadow"
        # 3. 其他情况回退到 "chunks_shadow"
        shadow_table_explicit = os.getenv("STEP3_PGVECTOR_SHADOW_TABLE")
        if shadow_table_explicit:
            # 显式设置，直接使用
            shadow_table = shadow_table_explicit
        elif shadow_strategy == COLLECTION_STRATEGY_SINGLE_TABLE:
            # 未显式设置且使用 single_table 策略，尝试复用 routing_shared_table
            routing_shared_table = os.getenv("STEP3_PGVECTOR_ROUTING_SHARED_TABLE")
            if routing_shared_table:
                shadow_table = routing_shared_table
                logger.debug(
                    f"STEP3_PGVECTOR_SHADOW_TABLE 未设置，shadow_strategy=single_table，"
                    f"复用 STEP3_PGVECTOR_ROUTING_SHARED_TABLE={routing_shared_table}"
                )
            else:
                shadow_table = "chunks_shadow"
        else:
            # 其他情况使用默认值
            shadow_table = "chunks_shadow"
        
        return cls(
            enabled=enabled,
            shadow_strategy=shadow_strategy,
            dry_run=dry_run,
            shadow_table=shadow_table,
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "enabled": self.enabled,
            "shadow_strategy": self.shadow_strategy,
            "dry_run": self.dry_run,
            "shadow_table": self.shadow_table,
        }


# ============ 双读配置 ============


# 双读策略常量
DUAL_READ_STRATEGY_COMPARE = "compare"       # 同时查询两个后端并比较结果，返回 primary 结果
DUAL_READ_STRATEGY_FALLBACK = "fallback"     # 优先使用 primary，失败或无结果时 fallback 到 shadow
DUAL_READ_STRATEGY_SHADOW_ONLY = "shadow_only"  # 仅使用 shadow 后端


@dataclass
class DualReadConfig:
    """
    双读配置
    
    当启用双读时，query 会同时从 primary 后端和 shadow 后端读取数据。
    主要用于灰度验证新后端策略的正确性，通过对比两个后端的查询结果。
    
    环境变量:
        STEP3_PGVECTOR_DUAL_READ=1                   # 启用双读
        STEP3_PGVECTOR_DUAL_READ_STRATEGY=compare    # 双读策略: compare/fallback/shadow_only
        STEP3_PGVECTOR_DUAL_READ_SHADOW_STRATEGY=single_table  # shadow 后端策略
        STEP3_PGVECTOR_DUAL_READ_LOG_DIFF=1          # 是否记录差异日志
        STEP3_PGVECTOR_DUAL_READ_DIFF_THRESHOLD=0.1  # 分数差异阈值
        STEP3_PGVECTOR_DUAL_READ_SHADOW_TIMEOUT_MS=5000  # shadow 查询超时（毫秒）
        STEP3_PGVECTOR_DUAL_READ_FAIL_OPEN=1         # shadow 失败时是否继续（fail-open 模式）
    
    策略说明:
        - compare: 同时查询两个后端并比较结果，返回 primary 结果，记录差异
        - fallback: 优先使用 primary，失败或无结果时 fallback 到 shadow
        - shadow_only: 仅使用 shadow 后端（用于验证 shadow 数据）
    
    双读关闭时行为:
        - 仅使用 primary 后端，完全保持现有行为零变化
        - 返回的 shadow_backend 为 None
    
    双读开启但 shadow 创建失败时行为:
        - 记录可操作的 warning 日志，包含具体错误信息
        - 自动退化到 primary_only 模式
        - fail_open=True（默认）时不影响主流程
    """
    enabled: bool = False                          # 是否启用双读
    strategy: str = DUAL_READ_STRATEGY_COMPARE     # 双读策略
    shadow_strategy: str = ""                      # shadow 后端策略（空表示自动选择相反策略）
    shadow_table: str = "chunks_shadow"            # shadow 表名
    log_diff: bool = True                          # 是否记录差异日志
    diff_threshold: float = 0.1                    # 分数差异阈值
    shadow_timeout_ms: int = 5000                  # shadow 查询超时（毫秒）
    fail_open: bool = True                         # shadow 失败时是否继续返回 primary 结果
    
    # 策略常量（类属性，方便引用）
    STRATEGY_COMPARE = DUAL_READ_STRATEGY_COMPARE
    STRATEGY_FALLBACK = DUAL_READ_STRATEGY_FALLBACK
    STRATEGY_SHADOW_ONLY = DUAL_READ_STRATEGY_SHADOW_ONLY
    
    @classmethod
    def from_env(cls, primary_strategy: str = COLLECTION_STRATEGY_PER_TABLE) -> "DualReadConfig":
        """
        从环境变量加载双读配置
        
        Args:
            primary_strategy: primary 后端当前使用的策略，用于自动选择相反的 shadow 策略
        
        Returns:
            DualReadConfig 实例
        """
        enabled_str = os.getenv("STEP3_PGVECTOR_DUAL_READ", "0").lower()
        enabled = enabled_str in ("1", "true", "yes")
        
        # 获取双读策略
        strategy = os.getenv("STEP3_PGVECTOR_DUAL_READ_STRATEGY", DUAL_READ_STRATEGY_COMPARE).lower().strip()
        valid_read_strategies = (DUAL_READ_STRATEGY_COMPARE, DUAL_READ_STRATEGY_FALLBACK, DUAL_READ_STRATEGY_SHADOW_ONLY)
        if strategy not in valid_read_strategies:
            logger.warning(
                f"无效的 STEP3_PGVECTOR_DUAL_READ_STRATEGY 值 '{strategy}'，"
                f"使用默认值 '{DUAL_READ_STRATEGY_COMPARE}'"
            )
            strategy = DUAL_READ_STRATEGY_COMPARE
        
        # 获取 shadow 策略，默认选择与 primary 相反的策略
        shadow_strategy = os.getenv("STEP3_PGVECTOR_DUAL_READ_SHADOW_STRATEGY", "").lower().strip()
        if not shadow_strategy:
            # 自动选择相反策略
            if primary_strategy == COLLECTION_STRATEGY_SINGLE_TABLE:
                shadow_strategy = COLLECTION_STRATEGY_PER_TABLE
            else:
                shadow_strategy = COLLECTION_STRATEGY_SINGLE_TABLE
        
        # 验证策略值
        valid_strategies = (COLLECTION_STRATEGY_PER_TABLE, COLLECTION_STRATEGY_SINGLE_TABLE)
        if shadow_strategy not in valid_strategies:
            logger.warning(
                f"无效的 STEP3_PGVECTOR_DUAL_READ_SHADOW_STRATEGY 值 '{shadow_strategy}'，"
                f"使用默认值 '{COLLECTION_STRATEGY_SINGLE_TABLE}'"
            )
            shadow_strategy = COLLECTION_STRATEGY_SINGLE_TABLE
        
        shadow_table = os.getenv("STEP3_PGVECTOR_DUAL_READ_SHADOW_TABLE", "chunks_shadow")
        
        log_diff_str = os.getenv("STEP3_PGVECTOR_DUAL_READ_LOG_DIFF", "1").lower()
        log_diff = log_diff_str in ("1", "true", "yes")
        
        diff_threshold = float(os.getenv("STEP3_PGVECTOR_DUAL_READ_DIFF_THRESHOLD", "0.1"))
        
        shadow_timeout_ms = int(os.getenv("STEP3_PGVECTOR_DUAL_READ_SHADOW_TIMEOUT_MS", "5000"))
        
        fail_open_str = os.getenv("STEP3_PGVECTOR_DUAL_READ_FAIL_OPEN", "1").lower()
        fail_open = fail_open_str in ("1", "true", "yes")
        
        return cls(
            enabled=enabled,
            strategy=strategy,
            shadow_strategy=shadow_strategy,
            shadow_table=shadow_table,
            log_diff=log_diff,
            diff_threshold=diff_threshold,
            shadow_timeout_ms=shadow_timeout_ms,
            fail_open=fail_open,
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "enabled": self.enabled,
            "strategy": self.strategy,
            "shadow_strategy": self.shadow_strategy,
            "shadow_table": self.shadow_table,
            "log_diff": self.log_diff,
            "diff_threshold": self.diff_threshold,
            "shadow_timeout_ms": self.shadow_timeout_ms,
            "fail_open": self.fail_open,
        }


@dataclass
class PGVectorConfig:
    """PGVector 后端配置"""
    dsn: str                              # PostgreSQL 连接字符串
    schema: str = "step3"                 # 数据库 schema
    table: str = "chunks"                 # 表名
    vector_dim: int = 1536                # 向量维度
    vector_weight: float = 0.7            # 向量分数权重
    text_weight: float = 0.3              # 全文分数权重
    collection_strategy: str = COLLECTION_STRATEGY_SINGLE_TABLE  # 存储策略（默认 single_table）
    # Routing 策略配置
    routing_shared_table: str = "chunks_shared"  # 路由策略使用的共享表名
    routing_allowlist: Optional[List[str]] = None  # 精确匹配列表（逗号分隔）
    routing_prefix_list: Optional[List[str]] = None  # 前缀列表（逗号分隔）
    routing_regex_patterns: Optional[List[str]] = None  # 正则表达式列表（逗号分隔）
    
    @classmethod
    def from_env(cls) -> "PGVectorConfig":
        """
        从环境变量加载配置
        
        DSN 解析优先级（从高到低）:
            1. STEP3_PGVECTOR_DSN（推荐，显式配置）
            2. 标准 PG 环境变量组合: PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE
            3. POSTGRES_DSN（仅当 STEP3_ALLOW_POSTGRES_DSN=1 时读取，避免误用非 Step3 权限账号）
            4. POSTGRES_* 变量组合: POSTGRES_HOST/PORT/USER/PASSWORD/DB（Makefile/统一栈兼容）
               - 会打印 fallback 提示，建议显式设置 STEP3_PGVECTOR_DSN
        
        安全说明:
            - POSTGRES_DSN 默认不自动读取，因为它可能指向非 Step3 权限的账号
            - 如需启用 POSTGRES_DSN 读取，请设置 STEP3_ALLOW_POSTGRES_DSN=1
        """
        dsn = ""
        dsn_source = ""
        
        # 优先级 1: STEP3_PGVECTOR_DSN（显式配置，最高优先级）
        step3_dsn = os.getenv("STEP3_PGVECTOR_DSN", "")
        if step3_dsn:
            dsn = step3_dsn
            dsn_source = "STEP3_PGVECTOR_DSN"
        
        # 优先级 2: 标准 PG 环境变量组合
        if not dsn:
            pg_host = os.getenv("PGHOST")
            pg_port = os.getenv("PGPORT")
            pg_user = os.getenv("PGUSER")
            pg_password = os.getenv("PGPASSWORD")
            pg_database = os.getenv("PGDATABASE")
            
            # 至少有一个标准 PG 变量被设置才认为是有效配置
            has_pg_vars = any([pg_host, pg_port, pg_user, pg_password, pg_database])
            if has_pg_vars:
                # 使用默认值填充未设置的变量
                pg_host = pg_host or "localhost"
                pg_port = pg_port or "5432"
                pg_user = pg_user or "postgres"
                pg_database = pg_database or "engram"
                
                if pg_password:
                    dsn = f"postgresql://{pg_user}:{pg_password}@{pg_host}:{pg_port}/{pg_database}"
                else:
                    dsn = f"postgresql://{pg_user}@{pg_host}:{pg_port}/{pg_database}"
                dsn_source = "PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE"
        
        # 优先级 3: POSTGRES_DSN（仅当显式允许时读取）
        if not dsn:
            allow_postgres_dsn = os.getenv("STEP3_ALLOW_POSTGRES_DSN", "0").lower() in ("1", "true", "yes")
            postgres_dsn = os.getenv("POSTGRES_DSN", "")
            if postgres_dsn and allow_postgres_dsn:
                dsn = postgres_dsn
                dsn_source = "POSTGRES_DSN (STEP3_ALLOW_POSTGRES_DSN=1)"
                logger.info(
                    f"使用 POSTGRES_DSN 作为 Step3 DSN（STEP3_ALLOW_POSTGRES_DSN=1）。"
                    f"建议显式设置 STEP3_PGVECTOR_DSN 以避免权限混淆。"
                )
            elif postgres_dsn and not allow_postgres_dsn:
                logger.debug(
                    f"检测到 POSTGRES_DSN 但未启用 STEP3_ALLOW_POSTGRES_DSN，忽略。"
                    f"如需使用 POSTGRES_DSN，请设置 STEP3_ALLOW_POSTGRES_DSN=1。"
                )
        
        # 优先级 4: POSTGRES_* 变量组合（Makefile/统一栈兼容，fallback）
        if not dsn:
            postgres_host = os.getenv("POSTGRES_HOST")
            postgres_port = os.getenv("POSTGRES_PORT")
            postgres_user = os.getenv("POSTGRES_USER")
            postgres_password = os.getenv("POSTGRES_PASSWORD")
            postgres_db = os.getenv("POSTGRES_DB")
            
            has_postgres_vars = any([postgres_host, postgres_port, postgres_user, postgres_password, postgres_db])
            if has_postgres_vars:
                # 使用默认值填充未设置的变量
                postgres_host = postgres_host or "localhost"
                postgres_port = postgres_port or "5432"
                postgres_user = postgres_user or "postgres"
                postgres_db = postgres_db or "engram"
                
                if postgres_password:
                    dsn = f"postgresql://{postgres_user}:{postgres_password}@{postgres_host}:{postgres_port}/{postgres_db}"
                else:
                    dsn = f"postgresql://{postgres_user}@{postgres_host}:{postgres_port}/{postgres_db}"
                dsn_source = "POSTGRES_HOST/PORT/USER/PASSWORD/DB (fallback)"
                
                # 打印 fallback 提示
                logger.info(
                    f"[FALLBACK] 使用 POSTGRES_* 变量组合作为 Step3 DSN。"
                    f"建议显式设置 STEP3_PGVECTOR_DSN 环境变量。"
                )
        
        # 如果仍然没有 DSN，使用默认值（本地开发环境）
        if not dsn:
            dsn = "postgresql://postgres@localhost:5432/engram"
            dsn_source = "default (localhost)"
        
        if dsn_source:
            logger.debug(f"PGVector DSN 来源: {dsn_source}")
        
        # 通过兼容层读取 schema/table（canonical 优先，legacy 作为别名）
        # STEP3_PG_SCHEMA 为 canonical，STEP3_SCHEMA 为 legacy 别名
        schema = get_str(
            "STEP3_PG_SCHEMA",
            deprecated_aliases=["STEP3_SCHEMA"],
            default="step3",
        )
        
        # STEP3_PG_TABLE 为 canonical，STEP3_TABLE 为 legacy 别名
        table = get_str(
            "STEP3_PG_TABLE",
            deprecated_aliases=["STEP3_TABLE"],
            default="chunks",
        )
        
        # 校验表名
        if "." in table:
            raise ValueError(
                f"STEP3_PG_TABLE 环境变量值 '{table}' 不应包含点号（.）。\n"
                f"如需指定 schema，请使用 STEP3_PG_SCHEMA 环境变量。\n"
                f"示例配置:\n"
                f"  STEP3_PG_SCHEMA=step3\n"
                f"  STEP3_PG_TABLE=chunks"
            )
        
        # 获取 collection 策略（通过兼容层，支持 legacy 值别名）
        # 有效策略: per_table, single_table, routing
        # 废弃的值别名（会触发警告）:
        #   - single -> single_table
        #   - shared -> single_table
        #   - per_project -> per_table
        #   - per-collection -> per_table
        strategy_str = get_choice(
            "STEP3_PGVECTOR_COLLECTION_STRATEGY",
            choices=[
                COLLECTION_STRATEGY_PER_TABLE,
                COLLECTION_STRATEGY_SINGLE_TABLE,
                COLLECTION_STRATEGY_ROUTING,
            ],
            deprecated_value_aliases={
                "single": COLLECTION_STRATEGY_SINGLE_TABLE,
                "shared": COLLECTION_STRATEGY_SINGLE_TABLE,
                "per_project": COLLECTION_STRATEGY_PER_TABLE,
                "per-collection": COLLECTION_STRATEGY_PER_TABLE,
            },
            default=COLLECTION_STRATEGY_SINGLE_TABLE,
        )
        
        # 解析 Routing 策略配置
        routing_shared_table = os.getenv("STEP3_PGVECTOR_ROUTING_SHARED_TABLE", "chunks_shared")
        
        # 解析 allowlist（逗号分隔）
        allowlist_str = os.getenv("STEP3_PGVECTOR_COLLECTION_ROUTING_ALLOWLIST", "")
        routing_allowlist = None
        if allowlist_str.strip():
            routing_allowlist = [s.strip() for s in allowlist_str.split(",") if s.strip()]
        
        # 解析 prefix_list（逗号分隔）
        prefix_str = os.getenv("STEP3_PGVECTOR_COLLECTION_ROUTING_PREFIX", "")
        routing_prefix_list = None
        if prefix_str.strip():
            routing_prefix_list = [s.strip() for s in prefix_str.split(",") if s.strip()]
        
        # 解析 regex_patterns（逗号分隔）
        regex_str = os.getenv("STEP3_PGVECTOR_COLLECTION_ROUTING_REGEX", "")
        routing_regex_patterns = None
        if regex_str.strip():
            routing_regex_patterns = [s.strip() for s in regex_str.split(",") if s.strip()]
        
        return cls(
            dsn=dsn,
            schema=schema,
            table=table,
            vector_dim=int(os.getenv("STEP3_PG_VECTOR_DIM", "1536")),
            vector_weight=float(os.getenv("STEP3_VECTOR_WEIGHT", "0.7")),
            text_weight=float(os.getenv("STEP3_TEXT_WEIGHT", "0.3")),
            collection_strategy=strategy_str,
            routing_shared_table=routing_shared_table,
            routing_allowlist=routing_allowlist,
            routing_prefix_list=routing_prefix_list,
            routing_regex_patterns=routing_regex_patterns,
        )
    
    @property
    def full_table_name(self) -> str:
        """获取完整表名（带 schema）"""
        return f"{self.schema}.{self.table}" if self.schema else self.table
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        result = {
            "dsn": self.dsn[:50] + "..." if len(self.dsn) > 50 else self.dsn,  # 隐藏密码
            "schema": self.schema,
            "table": self.table,
            "vector_dim": self.vector_dim,
            "vector_weight": self.vector_weight,
            "text_weight": self.text_weight,
            "collection_strategy": self.collection_strategy,
        }
        
        # 如果使用 routing 策略，添加相关配置
        if self.collection_strategy == COLLECTION_STRATEGY_ROUTING:
            result["routing_shared_table"] = self.routing_shared_table
            result["routing_allowlist"] = self.routing_allowlist
            result["routing_prefix_list"] = self.routing_prefix_list
            result["routing_regex_patterns"] = self.routing_regex_patterns
        
        return result


@dataclass
class SeekDBConfig:
    """SeekDB 后端配置"""
    host: str = "localhost"
    port: int = 19530
    api_key: Optional[str] = None
    namespace: str = "engram"
    vector_dim: int = 1536
    timeout: int = 30
    
    @classmethod
    def from_env(cls) -> "SeekDBConfig":
        """从环境变量加载配置"""
        return cls(
            host=os.getenv("SEEKDB_HOST", "localhost"),
            port=int(os.getenv("SEEKDB_PORT", "19530")),
            api_key=os.getenv("SEEKDB_API_KEY") or None,
            namespace=os.getenv("SEEKDB_NAMESPACE", "engram"),
            vector_dim=int(os.getenv("SEEKDB_VECTOR_DIM", "1536")),
            timeout=int(os.getenv("SEEKDB_TIMEOUT", "30")),
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "host": self.host,
            "port": self.port,
            "api_key": "***" if self.api_key else None,
            "namespace": self.namespace,
            "vector_dim": self.vector_dim,
            "timeout": self.timeout,
        }


# ============ 后端工厂 ============


def get_backend_type_from_env() -> BackendType:
    """
    从环境变量获取后端类型
    
    Returns:
        BackendType 枚举值，默认返回 PGVECTOR
    """
    backend_str = os.getenv("STEP3_INDEX_BACKEND", "pgvector")
    try:
        return BackendType.from_string(backend_str)
    except ValueError:
        logger.warning(f"未知的后端类型 '{backend_str}'，使用默认值 pgvector")
        return BackendType.PGVECTOR


def create_shadow_backend(
    primary_config: Optional[PGVectorConfig] = None,
    dual_write_config: Optional[DualWriteConfig] = None,
    chunking_version: Optional[str] = None,
    embedding_model_id: Optional[str] = None,
    embedding_provider: Optional["EmbeddingProvider"] = None,
    collection_id: Optional[str] = None,
) -> Optional["IndexBackend"]:
    """
    创建 Shadow 后端实例（用于双写）
    
    Shadow 后端与 primary 后端共享 DSN，但使用不同的 collection 策略和/或表名。
    
    Args:
        primary_config: primary 后端的 PGVector 配置
        dual_write_config: 双写配置，None 则从环境变量加载
        chunking_version: 分块版本
        embedding_model_id: Embedding 模型 ID
        embedding_provider: Embedding Provider 实例
        collection_id: canonical collection_id (冒号格式)
    
    Returns:
        Shadow PGVectorBackend 实例，如果双写未启用则返回 None
    """
    if primary_config is None:
        primary_config = PGVectorConfig.from_env()
    
    if dual_write_config is None:
        dual_write_config = DualWriteConfig.from_env(primary_config.collection_strategy)
    
    if not dual_write_config.enabled:
        return None
    
    if not primary_config.dsn:
        logger.warning("双写启用但 PGVector DSN 未配置，跳过 shadow 后端创建")
        return None
    
    # 导入 PGVectorBackend 和策略类
    try:
        from step3_seekdb_rag_hybrid.index_backend.pgvector_backend import (
            PGVectorBackend,
            HybridSearchConfig,
        )
        from step3_seekdb_rag_hybrid.index_backend.pgvector_collection_strategy import (
            DefaultCollectionStrategy,
            SharedTableStrategy,
        )
    except ImportError:
        from index_backend.pgvector_backend import (
            PGVectorBackend,
            HybridSearchConfig,
        )
        from index_backend.pgvector_collection_strategy import (
            DefaultCollectionStrategy,
            SharedTableStrategy,
        )
    
    hybrid_config = HybridSearchConfig(
        vector_weight=primary_config.vector_weight,
        text_weight=primary_config.text_weight,
    )
    
    # 根据 shadow 策略创建策略实例
    shadow_strategy = dual_write_config.shadow_strategy
    if shadow_strategy == COLLECTION_STRATEGY_SINGLE_TABLE:
        collection_strategy = SharedTableStrategy(
            collection_id_column="collection_id",
            expected_vector_dim=primary_config.vector_dim,
        )
        # single_table 策略使用固定表名
        table_name = dual_write_config.shadow_table
        strategy_name = "single_table"
    else:
        # per_table 策略
        collection_strategy = DefaultCollectionStrategy()
        # per_table 策略时表名由 collection_id 决定，这里设置基础表名
        table_name = primary_config.table
        strategy_name = "per_table"
    
    backend = PGVectorBackend(
        connection_string=primary_config.dsn,
        schema=primary_config.schema,
        table_name=table_name,
        embedding_provider=embedding_provider,
        hybrid_config=hybrid_config,
        vector_dim=primary_config.vector_dim,
        collection_id=collection_id,
        collection_strategy=collection_strategy,
    )
    
    log_msg = (
        f"创建 Shadow 后端: schema={primary_config.schema}, table={table_name}, "
        f"strategy={strategy_name}, dry_run={dual_write_config.dry_run}"
    )
    if collection_id:
        log_msg += f", collection_id={collection_id}"
    logger.info(log_msg)
    
    return backend


def create_pgvector_backend(
    config: Optional[PGVectorConfig] = None,
    chunking_version: Optional[str] = None,
    embedding_model_id: Optional[str] = None,
    embedding_provider: Optional["EmbeddingProvider"] = None,
    collection_id: Optional[str] = None,
    skip_preflight: bool = False,
) -> "IndexBackend":
    """
    创建 PGVector 后端实例
    
    Args:
        config: PGVector 配置，None 则从环境变量加载
        chunking_version: 分块版本（用于日志和 collection 命名）
        embedding_model_id: Embedding 模型 ID
        embedding_provider: Embedding Provider 实例
        collection_id: canonical collection_id (冒号格式)，用于动态表名
        skip_preflight: 是否跳过 preflight 校验（默认 False）
    
    Returns:
        PGVectorBackend 实例
        
    Raises:
        ValueError: 配置无效时抛出
        VectorDimensionMismatchError: 使用 single_table/routing 策略时向量维度不匹配
            （preflight 校验会在启动时检测数据库中表的实际维度与配置是否一致）
    """
    if config is None:
        config = PGVectorConfig.from_env()
    
    if not config.dsn:
        raise ValueError(
            "PGVector DSN 未配置，请设置 STEP3_PGVECTOR_DSN 环境变量，"
            "或配置 PGHOST/PGUSER/PGPASSWORD/PGDATABASE"
        )
    
    # 导入 PGVectorBackend 和策略类
    try:
        from step3_seekdb_rag_hybrid.index_backend.pgvector_backend import (
            PGVectorBackend,
            HybridSearchConfig,
        )
        from step3_seekdb_rag_hybrid.index_backend.pgvector_collection_strategy import (
            DefaultCollectionStrategy,
            SharedTableStrategy,
            RoutingCollectionStrategy,
            VectorDimensionMismatchError,
        )
    except ImportError:
        from index_backend.pgvector_backend import (
            PGVectorBackend,
            HybridSearchConfig,
        )
        from index_backend.pgvector_collection_strategy import (
            DefaultCollectionStrategy,
            SharedTableStrategy,
            RoutingCollectionStrategy,
            VectorDimensionMismatchError,
        )
    
    hybrid_config = HybridSearchConfig(
        vector_weight=config.vector_weight,
        text_weight=config.text_weight,
    )
    
    # 根据配置创建策略实例
    needs_preflight = False  # 是否需要 preflight 校验
    
    if config.collection_strategy == COLLECTION_STRATEGY_ROUTING:
        collection_strategy = RoutingCollectionStrategy(
            shared_table=config.routing_shared_table,
            base_table=config.table,
            allowlist=config.routing_allowlist,
            prefix_list=config.routing_prefix_list,
            regex_patterns=config.routing_regex_patterns,
            collection_id_column="collection_id",
            expected_vector_dim=config.vector_dim,
        )
        strategy_name = "routing"
        needs_preflight = True  # routing 策略可能使用共享表
    elif config.collection_strategy == COLLECTION_STRATEGY_SINGLE_TABLE:
        collection_strategy = SharedTableStrategy(
            collection_id_column="collection_id",
            expected_vector_dim=config.vector_dim,
        )
        strategy_name = "single_table"
        needs_preflight = True  # single_table 策略使用共享表
    else:
        # 默认使用 per_table 策略
        collection_strategy = DefaultCollectionStrategy()
        strategy_name = "per_table"
        needs_preflight = False  # per_table 策略每个 collection 独立表
    
    backend = PGVectorBackend(
        connection_string=config.dsn,
        schema=config.schema,
        table_name=config.table,
        embedding_provider=embedding_provider,
        hybrid_config=hybrid_config,
        vector_dim=config.vector_dim,
        collection_id=collection_id,
        collection_strategy=collection_strategy,
    )
    
    log_msg = (
        f"创建 PGVector 后端: schema={config.schema}, table={config.table}, "
        f"strategy={strategy_name}, vector_weight={config.vector_weight}, "
        f"text_weight={config.text_weight}"
    )
    if collection_id:
        log_msg += f", collection_id={collection_id}"
    logger.info(log_msg)
    
    # 执行 preflight 校验（仅对使用共享表的策略）
    if needs_preflight and not skip_preflight:
        try:
            logger.info(f"执行 preflight 校验: strategy={strategy_name}, table={config.table}")
            preflight_result = backend.preflight_check()
            logger.info(f"Preflight 校验完成: {preflight_result['status']}")
        except VectorDimensionMismatchError:
            # 重新抛出，让调用者处理
            raise
        except Exception as e:
            # 其他错误记录警告但不阻止启动
            # （例如表不存在时会在 initialize() 创建）
            logger.warning(f"Preflight 校验异常（非致命）: {e}")
    
    return backend


def create_seekdb_backend(
    config: Optional[SeekDBConfig] = None,
    chunking_version: str = "v1",
    embedding_model_id: str = "default",
    embedding_provider: Optional["EmbeddingProvider"] = None,
    collection_id: Optional[str] = None,
) -> "IndexBackend":
    """
    创建 SeekDB 后端实例
    
    Args:
        config: SeekDB 配置，None 则从环境变量加载
        chunking_version: 分块版本
        embedding_model_id: Embedding 模型 ID
        embedding_provider: Embedding Provider 实例
        collection_id: canonical collection_id (冒号格式)，优先使用
    
    Returns:
        SeekDBBackend 实例
    """
    if config is None:
        config = SeekDBConfig.from_env()
    
    # 导入 SeekDBBackend
    try:
        from step3_seekdb_rag_hybrid.index_backend.seekdb_backend import (
            SeekDBBackend,
            SeekDBConfig as SeekDBBackendConfig,
        )
    except ImportError:
        from index_backend.seekdb_backend import (
            SeekDBBackend,
            SeekDBConfig as SeekDBBackendConfig,
        )
    
    backend_config = SeekDBBackendConfig(
        host=config.host,
        port=config.port,
        api_key=config.api_key,
        timeout=config.timeout,
    )
    
    backend = SeekDBBackend(
        config=backend_config,
        namespace=config.namespace,
        chunking_version=chunking_version,
        embedding_model_id=embedding_model_id,
        embedding_provider=embedding_provider,
        vector_dim=config.vector_dim,
        auto_create_collection=True,
        collection_id=collection_id,
    )
    
    log_msg = (
        f"创建 SeekDB 后端: host={config.host}:{config.port}, "
        f"namespace={config.namespace}, version={chunking_version}"
    )
    if collection_id:
        log_msg += f", collection_id={collection_id}"
    logger.info(log_msg)
    
    return backend


def create_backend_from_env(
    backend_type: Optional[BackendType] = None,
    chunking_version: Optional[str] = None,
    embedding_model_id: Optional[str] = None,
    embedding_provider: Optional["EmbeddingProvider"] = None,
    collection_id: Optional[str] = None,
) -> "IndexBackend":
    """
    从环境变量创建索引后端
    
    这是主要的工厂入口，会根据 STEP3_INDEX_BACKEND 环境变量选择后端类型，
    并从相应的环境变量加载配置。
    
    Args:
        backend_type: 后端类型，None 则从环境变量 STEP3_INDEX_BACKEND 读取
        chunking_version: 分块版本
        embedding_model_id: Embedding 模型 ID
        embedding_provider: Embedding Provider 实例
        collection_id: canonical collection_id (冒号格式)，优先使用
    
    Returns:
        IndexBackend 实例
    
    Example:
        # 使用环境变量默认配置
        backend = create_backend_from_env()
        
        # 指定后端类型
        backend = create_backend_from_env(backend_type=BackendType.SEEKDB)
        
        # 完整配置
        backend = create_backend_from_env(
            backend_type=BackendType.PGVECTOR,
            chunking_version="v1-2026-01",
            embedding_model_id="bge-m3",
        )
        
        # 指定 collection_id
        backend = create_backend_from_env(
            collection_id="proj1:v1:bge-m3:20260128T120000",
        )
    """
    # 获取分块版本
    if chunking_version is None:
        try:
            from step3_seekdb_rag_hybrid.step3_chunking import CHUNKING_VERSION
        except ImportError:
            from step3_chunking import CHUNKING_VERSION
        chunking_version = CHUNKING_VERSION
    
    # 获取 embedding 模型 ID
    if embedding_model_id is None and embedding_provider is not None:
        embedding_model_id = embedding_provider.model_id
    elif embedding_model_id is None:
        embedding_model_id = os.getenv("STEP3_EMBEDDING_MODEL", "default")
    
    # 确定后端类型
    if backend_type is None:
        backend_type = get_backend_type_from_env()
    
    logger.info(f"初始化索引后端: type={backend_type.value}")
    
    if backend_type == BackendType.PGVECTOR:
        return create_pgvector_backend(
            chunking_version=chunking_version,
            embedding_model_id=embedding_model_id,
            embedding_provider=embedding_provider,
            collection_id=collection_id,
        )
    elif backend_type == BackendType.SEEKDB:
        return create_seekdb_backend(
            chunking_version=chunking_version,
            embedding_model_id=embedding_model_id,
            embedding_provider=embedding_provider,
            collection_id=collection_id,
        )
    else:
        raise ValueError(f"不支持的后端类型: {backend_type}")


# ============ CLI 参数支持 ============


def add_backend_arguments(parser: argparse.ArgumentParser) -> None:
    """
    向 ArgumentParser 添加后端相关参数
    
    添加的参数:
        --backend: 后端类型 (pgvector/seekdb)
        --pgvector-dsn: PGVector 连接字符串
        --seekdb-host: SeekDB 服务器地址
        --seekdb-port: SeekDB 服务器端口
    
    Args:
        parser: argparse.ArgumentParser 实例
    """
    backend_group = parser.add_argument_group("索引后端选项")
    
    backend_group.add_argument(
        "--backend",
        type=str,
        choices=["pgvector", "seekdb"],
        default=None,
        help="索引后端类型 (默认从 STEP3_INDEX_BACKEND 环境变量读取，未设置则为 pgvector)",
    )
    
    # PGVector 参数
    backend_group.add_argument(
        "--pgvector-dsn",
        type=str,
        default=None,
        help="PGVector 连接字符串 (默认从 STEP3_PGVECTOR_DSN 环境变量读取)",
    )
    
    # SeekDB 参数
    backend_group.add_argument(
        "--seekdb-host",
        type=str,
        default=None,
        help="SeekDB 服务器地址 (默认从 SEEKDB_HOST 环境变量读取)",
    )
    backend_group.add_argument(
        "--seekdb-port",
        type=int,
        default=None,
        help="SeekDB 服务器端口 (默认从 SEEKDB_PORT 环境变量读取)",
    )


def create_backend_from_args(
    args: argparse.Namespace,
    chunking_version: Optional[str] = None,
    embedding_model_id: Optional[str] = None,
    embedding_provider: Optional["EmbeddingProvider"] = None,
    collection_id: Optional[str] = None,
) -> "IndexBackend":
    """
    从 CLI 参数创建索引后端
    
    CLI 参数优先级高于环境变量。
    
    Args:
        args: 解析后的命令行参数
        chunking_version: 分块版本
        embedding_model_id: Embedding 模型 ID
        embedding_provider: Embedding Provider 实例
        collection_id: canonical collection_id (冒号格式)，优先使用
    
    Returns:
        IndexBackend 实例
    """
    # 确定后端类型
    backend_type = None
    if hasattr(args, 'backend') and args.backend:
        backend_type = BackendType.from_string(args.backend)
    
    # 处理 CLI 参数覆盖环境变量
    if hasattr(args, 'pgvector_dsn') and args.pgvector_dsn:
        os.environ["STEP3_PGVECTOR_DSN"] = args.pgvector_dsn
    
    if hasattr(args, 'seekdb_host') and args.seekdb_host:
        os.environ["SEEKDB_HOST"] = args.seekdb_host
    
    if hasattr(args, 'seekdb_port') and args.seekdb_port:
        os.environ["SEEKDB_PORT"] = str(args.seekdb_port)
    
    # 优先使用显式传入的 collection_id，其次从 args 获取
    actual_collection_id = collection_id
    if actual_collection_id is None and hasattr(args, 'collection') and args.collection:
        actual_collection_id = args.collection
    
    return create_backend_from_env(
        backend_type=backend_type,
        chunking_version=chunking_version,
        embedding_model_id=embedding_model_id,
        embedding_provider=embedding_provider,
        collection_id=actual_collection_id,
    )


# ============ 辅助函数 ============


def get_backend_info(backend_type: Optional[BackendType] = None) -> Dict[str, Any]:
    """
    获取后端配置信息（用于诊断）
    
    Args:
        backend_type: 后端类型，None 则从环境变量读取
    
    Returns:
        配置信息字典
    """
    if backend_type is None:
        backend_type = get_backend_type_from_env()
    
    info = {
        "backend_type": backend_type.value,
        "env_var": os.getenv("STEP3_INDEX_BACKEND", "(not set, default: pgvector)"),
    }
    
    if backend_type == BackendType.PGVECTOR:
        config = PGVectorConfig.from_env()
        info["config"] = config.to_dict()
    elif backend_type == BackendType.SEEKDB:
        config = SeekDBConfig.from_env()
        info["config"] = config.to_dict()
    
    return info


def create_dual_write_backends(
    backend_type: Optional[BackendType] = None,
    chunking_version: Optional[str] = None,
    embedding_model_id: Optional[str] = None,
    embedding_provider: Optional["EmbeddingProvider"] = None,
    collection_id: Optional[str] = None,
) -> tuple[Optional["IndexBackend"], Optional["IndexBackend"], Optional[DualWriteConfig]]:
    """
    创建双写后端（primary + shadow）
    
    仅对 pgvector 后端支持双写。如果后端类型不是 pgvector 或双写未启用，
    则仅返回 primary 后端。
    
    Args:
        backend_type: 后端类型，None 则从环境变量读取
        chunking_version: 分块版本
        embedding_model_id: Embedding 模型 ID
        embedding_provider: Embedding Provider 实例
        collection_id: canonical collection_id (冒号格式)
    
    Returns:
        (primary_backend, shadow_backend, dual_write_config)
        - shadow_backend 为 None 表示双写未启用或后端类型不支持
        - dual_write_config 包含双写配置信息
    """
    # 获取分块版本
    if chunking_version is None:
        try:
            from step3_seekdb_rag_hybrid.step3_chunking import CHUNKING_VERSION
        except ImportError:
            from step3_chunking import CHUNKING_VERSION
        chunking_version = CHUNKING_VERSION
    
    # 获取 embedding 模型 ID
    if embedding_model_id is None and embedding_provider is not None:
        embedding_model_id = embedding_provider.model_id
    elif embedding_model_id is None:
        embedding_model_id = os.getenv("STEP3_EMBEDDING_MODEL", "default")
    
    # 确定后端类型
    if backend_type is None:
        backend_type = get_backend_type_from_env()
    
    # 仅 pgvector 后端支持双写
    if backend_type != BackendType.PGVECTOR:
        primary = create_backend_from_env(
            backend_type=backend_type,
            chunking_version=chunking_version,
            embedding_model_id=embedding_model_id,
            embedding_provider=embedding_provider,
            collection_id=collection_id,
        )
        return primary, None, None
    
    # 创建 pgvector primary 后端
    primary_config = PGVectorConfig.from_env()
    dual_write_config = DualWriteConfig.from_env(primary_config.collection_strategy)
    
    primary = create_pgvector_backend(
        config=primary_config,
        chunking_version=chunking_version,
        embedding_model_id=embedding_model_id,
        embedding_provider=embedding_provider,
        collection_id=collection_id,
    )
    
    # 如果双写启用，创建 shadow 后端
    shadow = None
    if dual_write_config.enabled:
        shadow = create_shadow_backend(
            primary_config=primary_config,
            dual_write_config=dual_write_config,
            chunking_version=chunking_version,
            embedding_model_id=embedding_model_id,
            embedding_provider=embedding_provider,
            collection_id=collection_id,
        )
        
        if shadow is None:
            logger.warning("双写配置已启用但 shadow 后端创建失败")
    
    return primary, shadow, dual_write_config


def create_dual_read_backends(
    backend_type: Optional[BackendType] = None,
    chunking_version: Optional[str] = None,
    embedding_model_id: Optional[str] = None,
    embedding_provider: Optional["EmbeddingProvider"] = None,
    collection_id: Optional[str] = None,
) -> tuple[Optional["IndexBackend"], Optional["IndexBackend"], Optional[DualReadConfig]]:
    """
    创建双读后端（primary + shadow）
    
    仅对 pgvector 后端支持双读。如果后端类型不是 pgvector 或双读未启用，
    则仅返回 primary 后端。
    
    双读关闭时:
    - 返回 (primary_backend, None, DualReadConfig(enabled=False))
    - 完全保持现有行为零变化
    
    双读开启但 shadow 创建失败时:
    - 记录可操作的 warning 日志
    - 自动退化到 primary_only 模式
    - 返回 (primary_backend, None, DualReadConfig(enabled=True, ...))
    
    Args:
        backend_type: 后端类型，None 则从环境变量读取
        chunking_version: 分块版本
        embedding_model_id: Embedding 模型 ID
        embedding_provider: Embedding Provider 实例
        collection_id: canonical collection_id (冒号格式)
    
    Returns:
        (primary_backend, shadow_backend, dual_read_config)
        - primary_backend: 主后端（始终非 None）
        - shadow_backend: 影子后端，为 None 表示双读未启用、后端类型不支持或创建失败
        - dual_read_config: 双读配置信息，包含启用状态和策略设置
    """
    # 获取分块版本
    if chunking_version is None:
        try:
            from step3_seekdb_rag_hybrid.step3_chunking import CHUNKING_VERSION
        except ImportError:
            from step3_chunking import CHUNKING_VERSION
        chunking_version = CHUNKING_VERSION
    
    # 获取 embedding 模型 ID
    if embedding_model_id is None and embedding_provider is not None:
        embedding_model_id = embedding_provider.model_id
    elif embedding_model_id is None:
        embedding_model_id = os.getenv("STEP3_EMBEDDING_MODEL", "default")
    
    # 确定后端类型
    if backend_type is None:
        backend_type = get_backend_type_from_env()
    
    # 仅 pgvector 后端支持双读
    if backend_type != BackendType.PGVECTOR:
        primary = create_backend_from_env(
            backend_type=backend_type,
            chunking_version=chunking_version,
            embedding_model_id=embedding_model_id,
            embedding_provider=embedding_provider,
            collection_id=collection_id,
        )
        # 非 pgvector 后端返回禁用的 DualReadConfig
        return primary, None, DualReadConfig(enabled=False)
    
    # 创建 pgvector primary 后端
    primary_config = PGVectorConfig.from_env()
    dual_read_config = DualReadConfig.from_env(primary_config.collection_strategy)
    
    primary = create_pgvector_backend(
        config=primary_config,
        chunking_version=chunking_version,
        embedding_model_id=embedding_model_id,
        embedding_provider=embedding_provider,
        collection_id=collection_id,
    )
    
    # 如果双读未启用，直接返回
    if not dual_read_config.enabled:
        logger.debug("双读未启用，仅使用 primary 后端")
        return primary, None, dual_read_config
    
    # 双读启用，尝试创建 shadow 后端
    shadow = None
    try:
        shadow = _create_dual_read_shadow_backend(
            primary_config=primary_config,
            dual_read_config=dual_read_config,
            chunking_version=chunking_version,
            embedding_model_id=embedding_model_id,
            embedding_provider=embedding_provider,
            collection_id=collection_id,
        )
    except Exception as e:
        # 记录可操作的 warning，包含具体错误信息便于排查
        logger.warning(
            f"双读 shadow 后端创建失败，自动退化到 primary_only 模式。"
            f"错误: {type(e).__name__}: {e}。"
            f"检查项: 1) shadow 表 '{dual_read_config.shadow_table}' 是否存在; "
            f"2) 数据库连接是否正常; 3) shadow 策略 '{dual_read_config.shadow_strategy}' 配置是否正确"
        )
    
    if shadow is None and dual_read_config.enabled:
        # shadow 创建失败但双读已启用
        if dual_read_config.fail_open:
            logger.warning(
                f"双读已启用但 shadow 后端不可用，fail_open=True 允许退化到 primary_only。"
                f"策略: {dual_read_config.shadow_strategy}, 表: {dual_read_config.shadow_table}"
            )
        else:
            logger.error(
                f"双读已启用但 shadow 后端不可用，fail_open=False 配置下这可能影响功能。"
                f"请检查 shadow 后端配置或设置 STEP3_PGVECTOR_DUAL_READ_FAIL_OPEN=1"
            )
    
    return primary, shadow, dual_read_config


def _create_dual_read_shadow_backend(
    primary_config: PGVectorConfig,
    dual_read_config: DualReadConfig,
    chunking_version: Optional[str] = None,
    embedding_model_id: Optional[str] = None,
    embedding_provider: Optional["EmbeddingProvider"] = None,
    collection_id: Optional[str] = None,
) -> Optional["IndexBackend"]:
    """
    创建双读 Shadow 后端实例（内部函数）
    
    Shadow 后端与 primary 后端共享 DSN，但使用不同的 collection 策略和/或表名。
    
    Args:
        primary_config: primary 后端的 PGVector 配置
        dual_read_config: 双读配置
        chunking_version: 分块版本
        embedding_model_id: Embedding 模型 ID
        embedding_provider: Embedding Provider 实例
        collection_id: canonical collection_id (冒号格式)
    
    Returns:
        Shadow PGVectorBackend 实例，如果创建失败则返回 None
    """
    if not primary_config.dsn:
        logger.warning("双读启用但 PGVector DSN 未配置，跳过 shadow 后端创建")
        return None
    
    # 导入 PGVectorBackend 和策略类
    try:
        from step3_seekdb_rag_hybrid.index_backend.pgvector_backend import (
            PGVectorBackend,
            HybridSearchConfig,
        )
        from step3_seekdb_rag_hybrid.index_backend.pgvector_collection_strategy import (
            DefaultCollectionStrategy,
            SharedTableStrategy,
        )
    except ImportError:
        from index_backend.pgvector_backend import (
            PGVectorBackend,
            HybridSearchConfig,
        )
        from index_backend.pgvector_collection_strategy import (
            DefaultCollectionStrategy,
            SharedTableStrategy,
        )
    
    hybrid_config = HybridSearchConfig(
        vector_weight=primary_config.vector_weight,
        text_weight=primary_config.text_weight,
    )
    
    # 根据 shadow 策略创建策略实例
    shadow_strategy = dual_read_config.shadow_strategy
    if shadow_strategy == COLLECTION_STRATEGY_SINGLE_TABLE:
        collection_strategy = SharedTableStrategy(
            collection_id_column="collection_id",
            expected_vector_dim=primary_config.vector_dim,
        )
        # single_table 策略使用固定表名
        table_name = dual_read_config.shadow_table
        strategy_name = "single_table"
    else:
        # per_table 策略
        collection_strategy = DefaultCollectionStrategy()
        # per_table 策略时表名由 collection_id 决定，这里设置基础表名
        table_name = primary_config.table
        strategy_name = "per_table"
    
    backend = PGVectorBackend(
        connection_string=primary_config.dsn,
        schema=primary_config.schema,
        table_name=table_name,
        embedding_provider=embedding_provider,
        hybrid_config=hybrid_config,
        vector_dim=primary_config.vector_dim,
        collection_id=collection_id,
        collection_strategy=collection_strategy,
    )
    
    log_msg = (
        f"创建双读 Shadow 后端: schema={primary_config.schema}, table={table_name}, "
        f"strategy={strategy_name}, timeout_ms={dual_read_config.shadow_timeout_ms}, "
        f"log_diff={dual_read_config.log_diff}"
    )
    if collection_id:
        log_msg += f", collection_id={collection_id}"
    logger.info(log_msg)
    
    return backend


def get_dual_write_config() -> DualWriteConfig:
    """
    获取当前的双写配置（用于诊断）
    
    Returns:
        DualWriteConfig 实例
    """
    primary_config = PGVectorConfig.from_env()
    return DualWriteConfig.from_env(primary_config.collection_strategy)


def get_dual_read_config() -> DualReadConfig:
    """
    获取当前的双读配置（用于诊断）
    
    Returns:
        DualReadConfig 实例
    """
    primary_config = PGVectorConfig.from_env()
    return DualReadConfig.from_env(primary_config.collection_strategy)


def create_shadow_backend_for_read(
    primary_config: Optional[PGVectorConfig] = None,
    dual_read_config: Optional[DualReadConfig] = None,
    chunking_version: Optional[str] = None,
    embedding_model_id: Optional[str] = None,
    embedding_provider: Optional["EmbeddingProvider"] = None,
    collection_id: Optional[str] = None,
) -> Optional["IndexBackend"]:
    """
    创建用于双读的 Shadow 后端实例
    
    与 create_shadow_backend 类似，但专门用于双读场景。
    
    Args:
        primary_config: primary 后端的 PGVector 配置
        dual_read_config: 双读配置，None 则从环境变量加载
        chunking_version: 分块版本
        embedding_model_id: Embedding 模型 ID
        embedding_provider: Embedding Provider 实例
        collection_id: canonical collection_id (冒号格式)
    
    Returns:
        Shadow PGVectorBackend 实例，如果双读未启用则返回 None
    """
    if primary_config is None:
        primary_config = PGVectorConfig.from_env()
    
    if dual_read_config is None:
        dual_read_config = DualReadConfig.from_env(primary_config.collection_strategy)
    
    if not dual_read_config.enabled:
        return None
    
    if not primary_config.dsn:
        logger.warning("双读启用但 PGVector DSN 未配置，跳过 shadow 后端创建")
        return None
    
    # 导入 PGVectorBackend 和策略类
    try:
        from step3_seekdb_rag_hybrid.index_backend.pgvector_backend import (
            PGVectorBackend,
            HybridSearchConfig,
        )
        from step3_seekdb_rag_hybrid.index_backend.pgvector_collection_strategy import (
            DefaultCollectionStrategy,
            SharedTableStrategy,
        )
    except ImportError:
        from index_backend.pgvector_backend import (
            PGVectorBackend,
            HybridSearchConfig,
        )
        from index_backend.pgvector_collection_strategy import (
            DefaultCollectionStrategy,
            SharedTableStrategy,
        )
    
    hybrid_config = HybridSearchConfig(
        vector_weight=primary_config.vector_weight,
        text_weight=primary_config.text_weight,
    )
    
    # 根据 shadow 策略创建策略实例
    shadow_strategy = dual_read_config.shadow_strategy
    if shadow_strategy == COLLECTION_STRATEGY_SINGLE_TABLE:
        collection_strategy = SharedTableStrategy(
            collection_id_column="collection_id",
            expected_vector_dim=primary_config.vector_dim,
        )
        # single_table 策略使用固定表名
        table_name = dual_read_config.shadow_table
        strategy_name = "single_table"
    else:
        # per_table 策略
        collection_strategy = DefaultCollectionStrategy()
        # per_table 策略时表名由 collection_id 决定，这里设置基础表名
        table_name = primary_config.table
        strategy_name = "per_table"
    
    backend = PGVectorBackend(
        connection_string=primary_config.dsn,
        schema=primary_config.schema,
        table_name=table_name,
        embedding_provider=embedding_provider,
        hybrid_config=hybrid_config,
        vector_dim=primary_config.vector_dim,
        collection_id=collection_id,
        collection_strategy=collection_strategy,
    )
    
    log_msg = (
        f"创建 Shadow 后端(双读): schema={primary_config.schema}, table={table_name}, "
        f"strategy={strategy_name}, read_strategy={dual_read_config.strategy}"
    )
    if collection_id:
        log_msg += f", collection_id={collection_id}"
    logger.info(log_msg)
    
    return backend


def validate_backend_config(backend_type: Optional[BackendType] = None) -> bool:
    """
    验证后端配置是否有效
    
    Args:
        backend_type: 后端类型，None 则从环境变量读取
    
    Returns:
        配置是否有效
    
    Raises:
        ValueError: 配置无效时抛出详细错误信息
    """
    if backend_type is None:
        backend_type = get_backend_type_from_env()
    
    if backend_type == BackendType.PGVECTOR:
        config = PGVectorConfig.from_env()
        if not config.dsn:
            raise ValueError(
                "PGVector DSN 未配置。请设置以下环境变量之一:\n"
                "  - STEP3_PGVECTOR_DSN=postgresql://user:pass@host:5432/dbname\n"
                "  - 或设置标准 PG 环境变量: PGHOST, PGUSER, PGPASSWORD, PGDATABASE"
            )
        return True
    
    elif backend_type == BackendType.SEEKDB:
        config = SeekDBConfig.from_env()
        # SeekDB 有默认值，基本配置总是有效
        return True
    
    return False


__all__ = [
    # 类型
    "BackendType",
    "PGVectorConfig",
    "SeekDBConfig",
    "DualWriteConfig",
    "DualReadConfig",
    # Collection 策略常量
    "COLLECTION_STRATEGY_PER_TABLE",
    "COLLECTION_STRATEGY_SINGLE_TABLE",
    "COLLECTION_STRATEGY_ROUTING",
    # 双读策略常量
    "DUAL_READ_STRATEGY_COMPARE",
    "DUAL_READ_STRATEGY_FALLBACK",
    "DUAL_READ_STRATEGY_SHADOW_ONLY",
    # 工厂函数
    "create_backend_from_env",
    "create_backend_from_args",
    "create_pgvector_backend",
    "create_seekdb_backend",
    "create_shadow_backend",
    "create_shadow_backend_for_read",
    "create_dual_write_backends",
    "create_dual_read_backends",
    # CLI 支持
    "add_backend_arguments",
    # 辅助函数
    "get_backend_type_from_env",
    "get_backend_info",
    "get_dual_write_config",
    "get_dual_read_config",
    "validate_backend_config",
]
