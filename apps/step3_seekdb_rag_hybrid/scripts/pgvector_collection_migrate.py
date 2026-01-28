#!/usr/bin/env python3
"""
pgvector_collection_migrate.py - PGVector Collection 迁移脚本

支持三种迁移方案：
1. 单表方案 (shared-table): 
   - ALTER TABLE ADD COLUMN collection_id
   - 基于规则回填 collection_id
   - 创建 collection_id 索引

2. 按表方案 (table-per-collection):
   - 扫描旧表数据
   - 按 collection_id 分桶写入新表
   - 验证计数一致性

3. 合并到共享表 (consolidate-to-shared-table):
   - 扫描 schema 下 step3_chunks_% 表
   - 从表名反推 canonical collection_id
   - 批量 INSERT INTO shared_table SELECT ...
   - 幂等：基于 chunk_id 主键冲突处理
   - 迁移后校验：每表 rowcount 对齐、抽样校验

特性：
- dry-run 模式：只显示将执行的操作，不实际修改数据库
- 分批处理：避免长事务和内存压力
- 失败可重试：幂等操作设计，支持断点续传
- 进度报告：显示迁移进度和统计信息
- 交互确认：非 dry-run 时默认要求用户确认，可用 --yes 跳过
- 生产护栏：要求设置备份环境变量（ENGRAM_BACKUP_OK=1 或 BACKUP_TAG）
- Schema/Table 白名单校验：防止写入意外的 schema 或表

使用方法:
    # 单表方案 - dry-run（不需要确认和备份检查）
    python pgvector_collection_migrate.py shared-table --dry-run
    
    # 单表方案 - 实际执行（需要设置备份环境变量并确认）
    export ENGRAM_BACKUP_OK=1
    python pgvector_collection_migrate.py shared-table
    
    # 单表方案 - CI/自动化模式（跳过确认）
    ENGRAM_BACKUP_OK=1 python pgvector_collection_migrate.py shared-table --yes
    
    # 单表方案 - 测试环境（跳过备份检查和确认）
    python pgvector_collection_migrate.py shared-table --no-require-backup-env --yes
    
    # 按表方案 - dry-run
    python pgvector_collection_migrate.py table-per-collection --dry-run
    
    # 按表方案 - 指定批次大小
    python pgvector_collection_migrate.py table-per-collection --batch-size 500 --yes
    
    # 合并到共享表 - dry-run
    python pgvector_collection_migrate.py consolidate-to-shared-table --dry-run
    
    # 合并到共享表 - 指定目标表和冲突策略
    python pgvector_collection_migrate.py consolidate-to-shared-table \\
        --target-table chunks_shared \\
        --conflict-strategy upsert \\
        --batch-size 500 \\
        --yes

环境变量（优先级：STEP3_PG_* canonical > 通用变量/deprecated alias > 默认值）:
    STEP3_PG_HOST / POSTGRES_HOST: PostgreSQL 主机 (默认 localhost)
    STEP3_PG_PORT / POSTGRES_PORT: PostgreSQL 端口 (默认 5432)
    STEP3_PG_DB / POSTGRES_DB: 数据库名 (默认 engram)
    STEP3_PG_USER / POSTGRES_USER: 用户名 (默认 postgres)
    STEP3_PG_PASSWORD / POSTGRES_PASSWORD: 密码 (必需)
    STEP3_PG_SCHEMA: 目标 schema (默认 step3)
    STEP3_SCHEMA: 目标 schema 的别名（已废弃，请改用 STEP3_PG_SCHEMA）
    STEP3_PG_TABLE: 基础表名 (默认 chunks)
    STEP3_TABLE: 基础表名的别名（已废弃，请改用 STEP3_PG_TABLE）
    CHUNKING_VERSION: 分块版本号 (默认 v1)
    STEP3_EMBEDDING_MODEL: Embedding 模型 ID (默认 nomodel)
    ENGRAM_BACKUP_OK: 设置为 "1" 表示已完成备份（生产环境安全检查）
    BACKUP_TAG: 备份标签（生产环境安全检查，与 ENGRAM_BACKUP_OK 二选一）

回填规则:
    collection_id = {project_key}:{chunking_version}:{embedding_model_id}
    
    例如: proj1:v2:bge-m3
    
    - 如果 project_key 存在，使用上述格式
    - 如果 project_key 为空，使用 --default-collection-id 指定的值
"""

import argparse
import hashlib
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple, Pattern

# ============ 安全配置常量 ============

# 允许的 schema 白名单（正则模式）
# 注意：默认不包含 public schema，防止意外写入公共 schema
# 如需允许 public，使用 --allow-public-schema 参数
ALLOWED_SCHEMA_PATTERNS: List[str] = [
    r"^step3(_\w+)?$",      # step3, step3_test, step3_dev 等
    r"^engram(_\w+)?$",     # engram, engram_test 等
    # public schema 已移除：生产环境应使用独立 schema，public 仅用于 pgvector 扩展
]

# 允许的表名模式（正则模式）
# 注意：默认不包含无前缀的通用表名，防止意外覆盖
ALLOWED_TABLE_PATTERNS: List[str] = [
    r"^chunks(_\w+)?$",           # chunks, chunks_backup 等
    r"^step3_chunks_[\w]+$",      # step3_chunks_* 表
]

# public schema 的额外模式（仅当显式允许时使用）
PUBLIC_SCHEMA_PATTERN: str = r"^public$"

# 备份环境变量名
BACKUP_ENV_VAR = "ENGRAM_BACKUP_OK"
BACKUP_TAG_ENV_VAR = "BACKUP_TAG"

# 添加父目录到路径以便导入
_script_dir = Path(__file__).resolve().parent
_parent_dir = _script_dir.parent
if str(_parent_dir) not in sys.path:
    sys.path.insert(0, str(_parent_dir))

# 导入环境变量兼容层
try:
    from env_compat import get_str, get_int
except ImportError:
    # 回退：简单实现（不带废弃警告）
    def get_str(name, *, deprecated_aliases=None, default=None, **kwargs):
        val = os.environ.get(name)
        if val is not None:
            return val
        for alias in (deprecated_aliases or []):
            val = os.environ.get(alias)
            if val is not None:
                return val
        return default
    
    def get_int(name, *, deprecated_aliases=None, default=None, **kwargs):
        val = get_str(name, deprecated_aliases=deprecated_aliases, default=None)
        if val is not None:
            return int(val)
        return default

# 尝试导入 collection_naming 模块
_collection_naming_imported = False
try:
    from collection_naming import (
        from_pgvector_table_name,
        to_pgvector_table_name,
        PGVECTOR_TABLE_PREFIX,
        make_collection_id,
    )
    _collection_naming_imported = True
except ImportError:
    # 回退实现
    PGVECTOR_TABLE_PREFIX = "step3_chunks"
    
    def from_pgvector_table_name(table_name: str) -> str:
        """从 PGVector 表名反向解析为 canonical collection_id（回退实现）"""
        prefix = f"{PGVECTOR_TABLE_PREFIX}_"
        if not table_name.startswith(prefix):
            raise ValueError(f"无效的 PGVector 表名: {table_name}")
        name_part = table_name[len(prefix):]
        # 简单实现：将下划线转换为冒号
        parts = name_part.split("_")
        if len(parts) >= 3:
            return ":".join(parts[:3])
        return name_part
    
    def to_pgvector_table_name(collection_id: str) -> str:
        """将 canonical collection_id 转换为 PGVector 表名（回退实现）"""
        sanitized = collection_id.replace("-", "_").replace(":", "_")
        sanitized = re.sub(r'[^a-zA-Z0-9_]', '', sanitized).lower()
        if sanitized and sanitized[0].isdigit():
            sanitized = "_" + sanitized
        full_name = f"{PGVECTOR_TABLE_PREFIX}_{sanitized}"
        max_len = 63
        if len(full_name) <= max_len:
            return full_name
        hash_suffix = hashlib.sha256(collection_id.encode()).hexdigest()[:8]
        prefix = f"{PGVECTOR_TABLE_PREFIX}_"
        max_body = max_len - len(prefix) - 9
        truncated = sanitized[:max_body].rstrip('_')
        return f"{prefix}{truncated}_{hash_suffix}"
    
    def make_collection_id(
        project_key: Optional[str] = None,
        chunking_version: Optional[str] = None,
        embedding_model_id: Optional[str] = None,
        version_tag: Optional[str] = None,
    ) -> str:
        """生成规范化的 collection_id（回退实现）"""
        parts = [
            project_key or "default",
            chunking_version or "v1",
            embedding_model_id or "nomodel",
        ]
        if version_tag:
            parts.append(version_tag)
        return ":".join(parts)

# ============ 日志配置 ============

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ============ 配置数据类 ============


@dataclass
class MigrationConfig:
    """迁移配置"""
    # 数据库连接
    host: str = "localhost"
    port: int = 5432
    database: str = "engram"
    user: str = "postgres"
    password: str = ""
    
    # Schema/Table 配置
    schema: str = "step3"
    base_table: str = "chunks"
    
    # 迁移参数
    batch_size: int = 1000
    dry_run: bool = False
    verbose: bool = False
    
    # Collection 规则配置
    default_collection_id: str = "default:v1:nomodel"
    
    # Collection 命名参数（用于回填生成 collection_id）
    chunking_version: str = "v1"
    embedding_model_id: str = "nomodel"
    
    # table-per-collection 专用配置
    # collection_allowlist: 只处理指定的 collection_id 列表（为空则处理所有）
    collection_allowlist: Optional[List[str]] = None
    # 是否执行计数校验
    verify_counts: bool = True
    
    # 重试配置
    max_retries: int = 3
    retry_delay: float = 1.0
    
    @classmethod
    def from_env(cls) -> "MigrationConfig":
        """
        从环境变量加载配置
        
        环境变量优先级（STEP3_PG_* canonical，旧名称为 deprecated alias）:
        - STEP3_PG_HOST > POSTGRES_HOST（默认 localhost）
        - STEP3_PG_PORT > POSTGRES_PORT（默认 5432）
        - STEP3_PG_DB > POSTGRES_DB（默认 engram）
        - STEP3_PG_USER > POSTGRES_USER（默认 postgres）
        - STEP3_PG_PASSWORD > POSTGRES_PASSWORD（必需）
        - STEP3_PG_SCHEMA（canonical）> STEP3_SCHEMA（deprecated alias）（默认 step3）
        - STEP3_PG_TABLE（canonical）> STEP3_TABLE（deprecated alias）（默认 chunks）
        """
        def get_with_fallback(primary: str, fallback: str, default: str = "") -> str:
            """优先读取 primary，回退到 fallback，最后使用 default"""
            return os.environ.get(primary) or os.environ.get(fallback, default)
        
        chunking_version = os.environ.get("CHUNKING_VERSION", "v1")
        embedding_model_id = os.environ.get("STEP3_EMBEDDING_MODEL", "nomodel")
        
        # 通过兼容层读取 schema/table（canonical 优先，legacy 作为别名并触发废弃警告）
        # STEP3_PG_SCHEMA 为 canonical，STEP3_SCHEMA 为 deprecated alias
        schema = get_str(
            "STEP3_PG_SCHEMA",
            deprecated_aliases=["STEP3_SCHEMA"],
            default="step3",
        )
        
        # STEP3_PG_TABLE 为 canonical，STEP3_TABLE 为 deprecated alias
        base_table = get_str(
            "STEP3_PG_TABLE",
            deprecated_aliases=["STEP3_TABLE"],
            default="chunks",
        )
        
        return cls(
            host=get_with_fallback("STEP3_PG_HOST", "POSTGRES_HOST", "localhost"),
            port=int(get_with_fallback("STEP3_PG_PORT", "POSTGRES_PORT", "5432")),
            database=get_with_fallback("STEP3_PG_DB", "POSTGRES_DB", "engram"),
            user=get_with_fallback("STEP3_PG_USER", "POSTGRES_USER", "postgres"),
            password=get_with_fallback("STEP3_PG_PASSWORD", "POSTGRES_PASSWORD", ""),
            schema=schema,
            base_table=base_table,
            chunking_version=chunking_version,
            embedding_model_id=embedding_model_id,
        )
    
    def make_collection_id_for_project(self, project_key: Optional[str]) -> str:
        """
        根据 project_key 生成 collection_id
        
        使用 collection_naming.make_collection_id() 保持一致性。
        
        规则：
        - 如果 project_key 存在且非空，使用 {project_key}:{chunking_version}:{embedding_model_id}
        - 否则使用 default_collection_id
        """
        if project_key:
            return make_collection_id(
                project_key=project_key,
                chunking_version=self.chunking_version,
                embedding_model_id=self.embedding_model_id,
            )
        return self.default_collection_id
    
    @property
    def dsn(self) -> str:
        """返回 PostgreSQL DSN"""
        return (
            f"postgresql://{self.user}:{self.password}@"
            f"{self.host}:{self.port}/{self.database}"
        )
    
    @property
    def qualified_table(self) -> str:
        """返回完整表名"""
        return f'"{self.schema}"."{self.base_table}"'


@dataclass
class MigrationResult:
    """迁移结果"""
    success: bool
    message: str
    rows_processed: int = 0
    rows_migrated: int = 0
    errors: List[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    dry_run: bool = False
    plan: Optional[Dict[str, Any]] = None  # 详细计划信息
    
    def to_dict(self, include_plan: bool = False) -> Dict[str, Any]:
        result = {
            "success": self.success,
            "message": self.message,
            "rows_processed": self.rows_processed,
            "rows_migrated": self.rows_migrated,
            "errors": self.errors,
            "duration_seconds": self.duration_seconds,
            "dry_run": self.dry_run,
        }
        if include_plan and self.plan:
            result["plan"] = self.plan
        return result


@dataclass
class ConsolidateConfig:
    """合并到共享表的配置"""
    # 数据库连接（继承自 MigrationConfig）
    host: str = "localhost"
    port: int = 5432
    database: str = "engram"
    user: str = "postgres"
    password: str = ""
    
    # Schema 配置
    schema: str = "step3"
    
    # 目标表配置
    target_table: str = "chunks"
    
    # 源表过滤
    table_pattern: str = "step3_chunks_%"  # LIKE 模式
    table_allowlist: Optional[List[str]] = None  # 显式白名单
    table_regex: Optional[str] = None  # 正则表达式
    exclude_tables: List[str] = field(default_factory=list)  # 排除列表
    
    # 显式 collection_id 映射（表名 -> collection_id）
    collection_mapping: Optional[Dict[str, str]] = None
    collection_mapping_file: Optional[str] = None  # JSON 映射文件路径
    
    # 迁移参数
    batch_size: int = 1000
    dry_run: bool = False
    verbose: bool = False
    
    # 冲突处理策略: 'skip' (DO NOTHING) 或 'upsert' (DO UPDATE)
    conflict_strategy: str = "skip"
    
    # 校验参数
    verify_counts: bool = True  # 是否校验行数
    sample_verify: bool = True  # 是否抽样校验
    sample_size: int = 100  # 每表抽样数量
    
    # 重试配置
    max_retries: int = 3
    retry_delay: float = 1.0
    
    @classmethod
    def from_env(cls) -> "ConsolidateConfig":
        """
        从环境变量加载配置
        
        环境变量优先级（STEP3_PG_* canonical，旧名称为 deprecated alias）:
        - STEP3_PG_HOST > POSTGRES_HOST（默认 localhost）
        - STEP3_PG_PORT > POSTGRES_PORT（默认 5432）
        - STEP3_PG_DB > POSTGRES_DB（默认 engram）
        - STEP3_PG_USER > POSTGRES_USER（默认 postgres）
        - STEP3_PG_PASSWORD > POSTGRES_PASSWORD（必需）
        - STEP3_PG_SCHEMA（canonical）> STEP3_SCHEMA（deprecated alias）（默认 step3）
        - STEP3_PG_TABLE（canonical）> STEP3_TABLE（deprecated alias）（默认 chunks）
        """
        def get_with_fallback(primary: str, fallback: str, default: str = "") -> str:
            """优先读取 primary，回退到 fallback，最后使用 default"""
            return os.environ.get(primary) or os.environ.get(fallback, default)
        
        # 通过兼容层读取 schema/table（canonical 优先，legacy 作为别名并触发废弃警告）
        # STEP3_PG_SCHEMA 为 canonical，STEP3_SCHEMA 为 deprecated alias
        schema = get_str(
            "STEP3_PG_SCHEMA",
            deprecated_aliases=["STEP3_SCHEMA"],
            default="step3",
        )
        
        # STEP3_PG_TABLE 为 canonical，STEP3_TABLE 为 deprecated alias
        target_table = get_str(
            "STEP3_PG_TABLE",
            deprecated_aliases=["STEP3_TABLE"],
            default="chunks",
        )
        
        return cls(
            host=get_with_fallback("STEP3_PG_HOST", "POSTGRES_HOST", "localhost"),
            port=int(get_with_fallback("STEP3_PG_PORT", "POSTGRES_PORT", "5432")),
            database=get_with_fallback("STEP3_PG_DB", "POSTGRES_DB", "engram"),
            user=get_with_fallback("STEP3_PG_USER", "POSTGRES_USER", "postgres"),
            password=get_with_fallback("STEP3_PG_PASSWORD", "POSTGRES_PASSWORD", ""),
            schema=schema,
            target_table=target_table,
        )
    
    @property
    def dsn(self) -> str:
        """返回 PostgreSQL DSN"""
        return (
            f"postgresql://{self.user}:{self.password}@"
            f"{self.host}:{self.port}/{self.database}"
        )
    
    @property
    def qualified_target_table(self) -> str:
        """返回完整目标表名"""
        return f'"{self.schema}"."{self.target_table}"'
    
    def load_collection_mapping(self) -> Dict[str, str]:
        """加载 collection_id 映射"""
        mapping = {}
        
        # 从参数加载
        if self.collection_mapping:
            mapping.update(self.collection_mapping)
        
        # 从文件加载
        if self.collection_mapping_file and os.path.exists(self.collection_mapping_file):
            with open(self.collection_mapping_file, 'r', encoding='utf-8') as f:
                file_mapping = json.load(f)
                mapping.update(file_mapping)
        
        return mapping


# ============ 辅助函数 ============


def validate_schema_name(
    schema: str,
    allowed_patterns: Optional[List[str]] = None,
    allow_public: bool = False,
) -> bool:
    """
    验证 schema 名称是否在允许的白名单中
    
    Args:
        schema: schema 名称
        allowed_patterns: 允许的正则模式列表（默认使用 ALLOWED_SCHEMA_PATTERNS）
        allow_public: 是否允许 public schema（默认 False，生产安全护栏）
    
    Returns:
        True 如果 schema 名称合法
    
    Raises:
        ValueError: 如果 schema 名称不在白名单中
    """
    patterns = list(allowed_patterns or ALLOWED_SCHEMA_PATTERNS)
    
    # 如果显式允许 public，添加 public 模式
    if allow_public:
        patterns.append(PUBLIC_SCHEMA_PATTERN)
    
    for pattern in patterns:
        if re.match(pattern, schema):
            # 如果匹配到 public 但未显式允许，给出警告
            if schema == "public" and allow_public:
                logger.warning(
                    "⚠️  使用 public schema 作为迁移目标。"
                    "生产环境应使用独立 schema（如 step3）以实现隔离。"
                )
            return True
    
    # 特殊处理 public schema 的错误消息
    if schema == "public":
        raise ValueError(
            f"Schema 'public' 默认被禁止作为迁移目标。\n"
            f"原因: public schema 用于 pgvector 扩展，业务数据应存放在独立 schema。\n"
            f"如确需使用 public（仅限开发/测试），请添加 --allow-public-schema 参数。"
        )
    
    raise ValueError(
        f"Schema '{schema}' 不在允许的白名单中。"
        f"允许的模式: {patterns}。"
        f"如需添加新 schema，请更新 ALLOWED_SCHEMA_PATTERNS。"
    )


def validate_table_name(table: str, allowed_patterns: Optional[List[str]] = None) -> bool:
    """
    验证表名是否在允许的白名单中
    
    Args:
        table: 表名
        allowed_patterns: 允许的正则模式列表（默认使用 ALLOWED_TABLE_PATTERNS）
    
    Returns:
        True 如果表名合法
    
    Raises:
        ValueError: 如果表名不在白名单中
    """
    patterns = allowed_patterns or ALLOWED_TABLE_PATTERNS
    
    for pattern in patterns:
        if re.match(pattern, table):
            return True
    
    raise ValueError(
        f"表名 '{table}' 不在允许的白名单中。"
        f"允许的模式: {patterns}。"
        f"如需添加新表名模式，请更新 ALLOWED_TABLE_PATTERNS。"
    )


def check_backup_env(require_backup: bool = True) -> bool:
    """
    检查备份环境变量是否设置
    
    生产环境护栏：要求设置 ENGRAM_BACKUP_OK=1 或 BACKUP_TAG 环境变量
    
    Args:
        require_backup: 是否要求备份环境变量
    
    Returns:
        True 如果检查通过
    
    Raises:
        RuntimeError: 如果要求备份但环境变量未设置
    """
    if not require_backup:
        return True
    
    backup_ok = os.environ.get(BACKUP_ENV_VAR, "").strip()
    backup_tag = os.environ.get(BACKUP_TAG_ENV_VAR, "").strip()
    
    if backup_ok == "1" or backup_tag:
        logger.info(f"备份检查通过: {BACKUP_ENV_VAR}={backup_ok}, {BACKUP_TAG_ENV_VAR}={backup_tag or '(not set)'}")
        return True
    
    raise RuntimeError(
        f"生产安全检查失败: 需要设置环境变量 {BACKUP_ENV_VAR}=1 或 {BACKUP_TAG_ENV_VAR}。\n"
        f"这是为了确保在执行迁移前已完成数据备份。\n"
        f"如果已完成备份，请设置: export {BACKUP_ENV_VAR}=1\n"
        f"或者使用 --no-require-backup-env 跳过此检查（仅限测试/演练环境）"
    )


def print_plan_summary(plan: Dict[str, Any], command: str) -> str:
    """
    打印迁移计划摘要
    
    Args:
        plan: 迁移计划详情
        command: 迁移命令类型
    
    Returns:
        摘要文本
    """
    lines = []
    lines.append("=" * 60)
    lines.append("迁移计划摘要")
    lines.append("=" * 60)
    
    if command == "shared-table":
        lines.append(f"  操作类型: 单表方案 - 添加 collection_id 列并回填")
        lines.append(f"  collection_id 列已存在: {plan.get('column_exists', 'N/A')}")
        lines.append(f"  索引已存在: {plan.get('index_exists', 'N/A')}")
        lines.append(f"  预计回填行数: {plan.get('planned_backfill_rows', 0):,}")
        lines.append(f"  总行数: {plan.get('total_rows', 0):,} {'(精确)' if plan.get('total_rows_exact') else '(估算)'}")
        lines.append(f"  计划操作:")
        for action in plan.get("actions", []):
            lines.append(f"    - {action}")
    
    elif command == "table-per-collection":
        lines.append(f"  操作类型: 按表方案 - 按 collection 分表存储")
        lines.append(f"  源表: {plan.get('source_table', 'N/A')}")
        lines.append(f"  collection_id 列存在: {plan.get('collection_id_column_exists', 'N/A')}")
        lines.append(f"  总 collection 数: {plan.get('total_collections', 0)}")
        lines.append(f"  预计处理行数: {plan.get('total_planned_rows', 0):,}")
        if plan.get("skipped_collections"):
            lines.append(f"  跳过的 collection: {len(plan.get('skipped_collections', []))}")
        lines.append(f"  目标表列表:")
        for tbl in plan.get("target_tables", [])[:10]:  # 最多显示 10 个
            status = "已存在" if tbl.get("table_exists") else "将创建"
            lines.append(f"    - {tbl.get('collection_id')}: {tbl.get('target_table')} ({status}, {tbl.get('source_row_count', 0):,} 行)")
        if len(plan.get("target_tables", [])) > 10:
            lines.append(f"    ... 还有 {len(plan.get('target_tables', [])) - 10} 个表")
    
    elif command == "consolidate-to-shared-table":
        lines.append(f"  操作类型: 合并到共享表")
        lines.append(f"  目标表: {plan.get('target_table', 'N/A')}")
        lines.append(f"  源表数量: {plan.get('source_table_count', 0)}")
        lines.append(f"  跳过表数量: {plan.get('skipped_table_count', 0)}")
        lines.append(f"  预计迁移行数: {plan.get('total_estimated_rows', 0):,} {'(精确)' if plan.get('total_exact') else '(估算)'}")
        lines.append(f"  冲突策略: {plan.get('conflict_strategy', 'N/A')}")
        lines.append(f"  源表列表:")
        for src in plan.get("source_tables", [])[:10]:  # 最多显示 10 个
            lines.append(f"    - {src.get('table_name')}: {src.get('estimated_rows', 0):,} 行 -> collection_id={src.get('collection_id')}")
        if len(plan.get("source_tables", [])) > 10:
            lines.append(f"    ... 还有 {len(plan.get('source_tables', [])) - 10} 个表")
    
    lines.append("=" * 60)
    
    summary = "\n".join(lines)
    print(summary)
    return summary


def prompt_confirmation(message: str = "确认执行迁移?", auto_yes: bool = False) -> bool:
    """
    提示用户确认
    
    Args:
        message: 确认提示消息
        auto_yes: 是否自动确认（用于 CI/自动化）
    
    Returns:
        True 如果用户确认，False 否则
    """
    if auto_yes:
        logger.info("自动确认模式 (--yes)")
        return True
    
    try:
        response = input(f"\n{message} [y/N]: ").strip().lower()
        return response in ("y", "yes")
    except EOFError:
        # 非交互式环境
        logger.warning("非交互式环境，无法获取用户输入")
        return False


def get_connection(config: MigrationConfig):
    """获取数据库连接"""
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        raise RuntimeError(
            "psycopg (v3) 未安装，请运行: pip install psycopg[binary]"
        )
    
    return psycopg.connect(
        config.dsn,
        row_factory=dict_row,
    )


def sanitize_identifier(name: str) -> str:
    """清理 SQL 标识符"""
    result = name.replace("-", "_").replace(":", "_")
    result = re.sub(r'[^a-zA-Z0-9_]', '', result)
    if result and result[0].isdigit():
        result = "_" + result
    return result.lower()


# ============ 单表方案迁移器 ============


class SharedTableMigrator:
    """
    单表方案迁移器
    
    执行步骤：
    1. 检查 collection_id 列是否存在
    2. 如果不存在，添加列
    3. 基于规则回填 collection_id
    4. 创建索引
    """
    
    def __init__(self, config: MigrationConfig):
        self.config = config
        self._conn = None
    
    def _get_conn(self):
        if self._conn is None:
            self._conn = get_connection(self.config)
        return self._conn
    
    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
    
    def column_exists(self) -> bool:
        """检查 collection_id 列是否存在"""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = %s 
                    AND table_name = %s 
                    AND column_name = 'collection_id'
                )
            """, (self.config.schema, self.config.base_table))
            row = cur.fetchone()
            return row and row.get("exists", False)
    
    def index_exists(self, index_name: str) -> bool:
        """检查索引是否存在"""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM pg_indexes
                    WHERE schemaname = %s AND indexname = %s
                )
            """, (self.config.schema, index_name))
            row = cur.fetchone()
            return row and row.get("exists", False)
    
    def count_null_collection_ids(self) -> int:
        """统计 collection_id 为 NULL 的行数"""
        conn = self._get_conn()
        table = self.config.qualified_table
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) as cnt FROM {table} WHERE collection_id IS NULL")
            row = cur.fetchone()
            return row.get("cnt", 0) if row else 0
    
    def count_total_rows(self) -> int:
        """统计总行数"""
        conn = self._get_conn()
        table = self.config.qualified_table
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) as cnt FROM {table}")
            row = cur.fetchone()
            return row.get("cnt", 0) if row else 0
    
    def estimate_total_rows(self) -> Tuple[int, bool]:
        """
        估算总行数（优先使用 pg_stat 快速估算）
        
        Returns:
            (row_count, is_exact): 行数和是否为精确值
        """
        conn = self._get_conn()
        with conn.cursor() as cur:
            # 尝试从 pg_stat_user_tables 获取估算值
            cur.execute("""
                SELECT n_live_tup 
                FROM pg_stat_user_tables 
                WHERE schemaname = %s AND relname = %s
            """, (self.config.schema, self.config.base_table))
            row = cur.fetchone()
            if row and row.get("n_live_tup", 0) > 0:
                return row["n_live_tup"], False  # 估算值
        
        # 回退到精确计数
        return self.count_total_rows(), True
    
    def get_plan(self) -> Dict[str, Any]:
        """
        获取迁移计划详情
        
        返回：
        - column_exists: collection_id 列是否已存在
        - index_exists: 索引是否已存在
        - index_name: 将创建的索引名
        - index_concurrent: 是否使用 CONCURRENTLY
        - null_collection_id_count: NULL collection_id 的行数
        - planned_backfill_rows: 预期回填的行数
        - total_rows: 总行数（估算或精确）
        - total_rows_exact: total_rows 是否为精确值
        - backfill_rule: 回填规则描述
        """
        column_exists = self.column_exists()
        index_name = f"{self.config.schema}_{self.config.base_table}_collection_id_idx"
        idx_exists = self.index_exists(index_name)
        
        # 获取 NULL collection_id 行数（仅在列存在时）
        null_count = 0
        if column_exists:
            null_count = self.count_null_collection_ids()
        
        # 估算总行数
        total_rows, total_exact = self.estimate_total_rows()
        
        # 计算预期回填行数
        if column_exists:
            planned_backfill = null_count
        else:
            # 如果列不存在，需要回填所有行
            planned_backfill = total_rows
        
        return {
            "column_exists": column_exists,
            "index_exists": idx_exists,
            "index_name": index_name,
            "index_concurrent": True,  # 始终使用 CONCURRENTLY
            "null_collection_id_count": null_count,
            "planned_backfill_rows": planned_backfill,
            "total_rows": total_rows,
            "total_rows_exact": total_exact,
            "backfill_rule": {
                "format": "{project_key}:{chunking_version}:{embedding_model_id}",
                "chunking_version": self.config.chunking_version,
                "embedding_model_id": self.config.embedding_model_id,
                "default_collection_id": self.config.default_collection_id,
            },
            "actions": self._get_planned_actions(column_exists, idx_exists, planned_backfill),
        }
    
    def _get_planned_actions(
        self, 
        column_exists: bool, 
        index_exists: bool, 
        planned_backfill: int
    ) -> List[str]:
        """生成计划执行的操作列表"""
        actions = []
        if not column_exists:
            actions.append("ALTER TABLE ADD COLUMN collection_id TEXT")
        if planned_backfill > 0:
            actions.append(f"UPDATE {planned_backfill} rows SET collection_id")
        if not index_exists:
            index_name = f"{self.config.schema}_{self.config.base_table}_collection_id_idx"
            actions.append(f"CREATE INDEX CONCURRENTLY {index_name}")
        if not actions:
            actions.append("No changes needed")
        return actions
    
    def add_column(self) -> bool:
        """添加 collection_id 列"""
        if self.config.dry_run:
            logger.info("[DRY-RUN] 将执行: ALTER TABLE ADD COLUMN collection_id TEXT")
            return True
        
        conn = self._get_conn()
        table = self.config.qualified_table
        with conn.cursor() as cur:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS collection_id TEXT")
            conn.commit()
        logger.info("已添加 collection_id 列")
        return True
    
    def backfill_collection_ids(self) -> int:
        """
        回填 collection_id
        
        分批处理，每批更新 batch_size 条记录
        
        回填规则使用 collection_naming.make_collection_id() 格式：
        - 有 project_key: {project_key}:{chunking_version}:{embedding_model_id}
        - 无 project_key: 使用 default_collection_id
        """
        if self.config.dry_run:
            null_count = self.count_null_collection_ids()
            logger.info(f"[DRY-RUN] 将回填 {null_count} 条记录的 collection_id")
            logger.info(
                f"[DRY-RUN] 回填规则: project_key:{self.config.chunking_version}:"
                f"{self.config.embedding_model_id}"
            )
            logger.info(f"[DRY-RUN] 空 project_key 默认值: {self.config.default_collection_id}")
            return null_count
        
        conn = self._get_conn()
        table = self.config.qualified_table
        batch_size = self.config.batch_size
        total_updated = 0
        retries = 0
        
        # 构建 collection_id 后缀: :{chunking_version}:{embedding_model_id}
        collection_id_suffix = f":{self.config.chunking_version}:{self.config.embedding_model_id}"
        
        while True:
            try:
                with conn.cursor() as cur:
                    # 批量更新：选取 batch_size 条 NULL 记录
                    # 使用 project_key 推断 collection_id
                    # 格式: {project_key}:{chunking_version}:{embedding_model_id}
                    cur.execute(f"""
                        WITH to_update AS (
                            SELECT chunk_id, project_key
                            FROM {table}
                            WHERE collection_id IS NULL
                            LIMIT %s
                        )
                        UPDATE {table} t
                        SET collection_id = CASE
                            WHEN tu.project_key IS NOT NULL AND tu.project_key != ''
                            THEN tu.project_key || %s
                            ELSE %s
                        END
                        FROM to_update tu
                        WHERE t.chunk_id = tu.chunk_id
                    """, (batch_size, collection_id_suffix, self.config.default_collection_id))
                    
                    updated = cur.rowcount
                    conn.commit()
                    
                    if updated == 0:
                        break
                    
                    total_updated += updated
                    retries = 0  # 重置重试计数
                    
                    if self.config.verbose:
                        logger.info(f"已回填 {total_updated} 条记录...")
                    
            except Exception as e:
                conn.rollback()
                retries += 1
                if retries >= self.config.max_retries:
                    logger.error(f"回填失败，已重试 {retries} 次: {e}")
                    raise
                logger.warning(f"回填出错，第 {retries} 次重试: {e}")
                time.sleep(self.config.retry_delay)
        
        logger.info(f"回填完成，共更新 {total_updated} 条记录")
        return total_updated
    
    def create_index(self) -> bool:
        """创建 collection_id 索引"""
        index_name = f"{self.config.schema}_{self.config.base_table}_collection_id_idx"
        
        if self.index_exists(index_name):
            logger.info(f"索引 {index_name} 已存在，跳过创建")
            return True
        
        if self.config.dry_run:
            logger.info(f"[DRY-RUN] 将创建索引: {index_name}")
            return True
        
        conn = self._get_conn()
        table = self.config.qualified_table
        with conn.cursor() as cur:
            # 使用 CONCURRENTLY 避免锁表（需要在事务外执行）
            conn.autocommit = True
            try:
                cur.execute(f"""
                    CREATE INDEX CONCURRENTLY IF NOT EXISTS "{index_name}"
                    ON {table} (collection_id)
                """)
            finally:
                conn.autocommit = False
        
        logger.info(f"已创建索引: {index_name}")
        return True
    
    def migrate(self, include_plan: bool = False) -> MigrationResult:
        """执行迁移"""
        start_time = time.time()
        errors = []
        rows_migrated = 0
        plan = None
        
        try:
            # 获取计划信息（在执行前）
            if include_plan or self.config.dry_run:
                plan = self.get_plan()
            
            # Step 1: 检查/添加列
            if not self.column_exists():
                logger.info("Step 1/3: 添加 collection_id 列...")
                self.add_column()
            else:
                logger.info("Step 1/3: collection_id 列已存在，跳过")
            
            # Step 2: 回填数据
            logger.info("Step 2/3: 回填 collection_id...")
            rows_migrated = self.backfill_collection_ids()
            
            # Step 3: 创建索引
            logger.info("Step 3/3: 创建索引...")
            self.create_index()
            
            duration = time.time() - start_time
            
            # dry-run 时使用计划中的 total_rows
            rows_processed = 0
            if not self.config.dry_run:
                rows_processed = self.count_total_rows()
            elif plan:
                rows_processed = plan.get("total_rows", 0)
            
            return MigrationResult(
                success=True,
                message="单表方案迁移完成",
                rows_processed=rows_processed,
                rows_migrated=rows_migrated,
                duration_seconds=duration,
                dry_run=self.config.dry_run,
                plan=plan,
            )
            
        except Exception as e:
            duration = time.time() - start_time
            errors.append(str(e))
            logger.error(f"迁移失败: {e}")
            return MigrationResult(
                success=False,
                message=f"迁移失败: {e}",
                rows_migrated=rows_migrated,
                errors=errors,
                duration_seconds=duration,
                dry_run=self.config.dry_run,
                plan=plan,
            )
        finally:
            self.close()


# ============ 按表方案迁移器 ============


class TablePerCollectionMigrator:
    """
    按表方案迁移器（增强版）
    
    将共享表中的数据按 collection_id 拆分到独立表中。
    
    关键设计：
    1. 优先使用共享表的 collection_id 列做分桶（禁止用 project_key 推断）
    2. 目标表名使用 collection_naming.to_pgvector_table_name(collection_id)
    3. 支持 collection_allowlist 只处理指定的 collection
    4. 幂等插入：使用 ON CONFLICT DO NOTHING
    5. 计数校验：迁移后验证源表与目标表计数一致
    
    执行步骤：
    1. 检查源表是否有 collection_id 列（必须存在）
    2. 扫描源表，统计 collection_id 分布
    3. 根据 allowlist 过滤要处理的 collection
    4. 为每个 collection 创建目标表
    5. 分批幂等复制数据到目标表
    6. 验证计数一致性
    """
    
    def __init__(self, config: MigrationConfig):
        self.config = config
        self._conn = None
    
    def _get_conn(self):
        if self._conn is None:
            self._conn = get_connection(self.config)
        return self._conn
    
    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
    
    def check_collection_id_column_exists(self) -> bool:
        """
        检查源表是否有 collection_id 列
        
        table-per-collection 迁移要求源表必须有 collection_id 列。
        如果没有，应先执行 shared-table 迁移来添加该列。
        
        Returns:
            True 如果 collection_id 列存在
        
        Raises:
            RuntimeError: 如果 collection_id 列不存在
        """
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = %s 
                    AND table_name = %s 
                    AND column_name = 'collection_id'
                )
            """, (self.config.schema, self.config.base_table))
            row = cur.fetchone()
            exists = row and row.get("exists", False)
        
        if not exists:
            raise RuntimeError(
                f"源表 {self.config.qualified_table} 没有 collection_id 列。"
                f"请先执行 'shared-table' 迁移来添加 collection_id 列并回填数据。"
            )
        
        return True
    
    def get_collection_distribution(self) -> Dict[str, int]:
        """
        获取 collection 分布（基于 collection_id 列）
        
        注意：此方法要求源表必须有 collection_id 列。
        不再支持基于 project_key 推断。
        
        Returns:
            字典 {collection_id: row_count}
        """
        conn = self._get_conn()
        table = self.config.qualified_table
        
        with conn.cursor() as cur:
            # 使用 collection_id 列统计，NULL 值使用默认 collection_id
            cur.execute(f"""
                SELECT 
                    COALESCE(collection_id, %s) as cid,
                    COUNT(*) as cnt
                FROM {table}
                GROUP BY COALESCE(collection_id, %s)
                ORDER BY cnt DESC
            """, (self.config.default_collection_id, self.config.default_collection_id))
            
            return {row["cid"]: row["cnt"] for row in cur.fetchall()}
    
    def filter_by_allowlist(self, distribution: Dict[str, int]) -> Dict[str, int]:
        """
        根据 allowlist 过滤要处理的 collection
        
        Args:
            distribution: 完整的 collection 分布
        
        Returns:
            过滤后的分布（如果没有 allowlist 则返回原分布）
        """
        if not self.config.collection_allowlist:
            return distribution
        
        allowlist_set = set(self.config.collection_allowlist)
        filtered = {
            cid: cnt 
            for cid, cnt in distribution.items() 
            if cid in allowlist_set
        }
        
        # 报告被过滤的 collection
        skipped = set(distribution.keys()) - set(filtered.keys())
        if skipped:
            logger.info(f"根据 allowlist 跳过 {len(skipped)} 个 collection: {', '.join(sorted(skipped)[:5])}...")
        
        return filtered
    
    def get_plan(self) -> Dict[str, Any]:
        """
        获取迁移计划详情
        
        返回：
        - source_table: 源表名
        - collection_id_column_exists: collection_id 列是否存在
        - full_distribution: 完整的 collection 分布
        - filtered_distribution: 过滤后的分布
        - skipped_collections: 被跳过的 collection 列表
        - target_tables: 每个 collection 的目标表信息
        - total_planned_rows: 预计复制的总行数
        - verify_counts_enabled: 是否启用计数校验
        """
        # 检查 collection_id 列
        try:
            column_exists = True
            self.check_collection_id_column_exists()
        except RuntimeError:
            column_exists = False
            return {
                "source_table": self.config.qualified_table,
                "collection_id_column_exists": False,
                "error": "源表没有 collection_id 列，请先执行 shared-table 迁移",
                "full_distribution": {},
                "filtered_distribution": {},
                "skipped_collections": [],
                "target_tables": [],
                "total_planned_rows": 0,
                "verify_counts_enabled": self.config.verify_counts,
            }
        
        # 获取 collection 分布
        full_distribution = self.get_collection_distribution()
        filtered_distribution = self.filter_by_allowlist(full_distribution)
        
        # 计算被跳过的 collections
        skipped_collections = list(set(full_distribution.keys()) - set(filtered_distribution.keys()))
        
        # 构建目标表信息
        target_tables = []
        for collection_id, row_count in sorted(filtered_distribution.items()):
            table_name = to_pgvector_table_name(collection_id)
            table_exists = self.table_exists(table_name)
            existing_count = 0
            if table_exists:
                existing_count = self.get_target_table_count(table_name)
            
            target_tables.append({
                "collection_id": collection_id,
                "target_table": table_name,
                "table_exists": table_exists,
                "source_row_count": row_count,
                "existing_target_count": existing_count,
                "planned_copy_rows": row_count,  # 幂等复制，可能小于此数
            })
        
        total_planned_rows = sum(filtered_distribution.values())
        
        return {
            "source_table": self.config.qualified_table,
            "collection_id_column_exists": column_exists,
            "full_distribution": full_distribution,
            "filtered_distribution": filtered_distribution,
            "skipped_collections": skipped_collections,
            "allowlist": self.config.collection_allowlist,
            "target_tables": target_tables,
            "total_planned_rows": total_planned_rows,
            "total_collections": len(filtered_distribution),
            "verify_counts_enabled": self.config.verify_counts,
            "batch_size": self.config.batch_size,
        }
    
    def table_exists(self, table_name: str) -> bool:
        """检查表是否存在"""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = %s AND table_name = %s
                )
            """, (self.config.schema, table_name))
            row = cur.fetchone()
            return row and row.get("exists", False)
    
    def create_collection_table(self, table_name: str) -> bool:
        """
        创建 collection 专用表
        
        表结构与源表相同（使用 LIKE ... INCLUDING ALL）
        """
        if self.table_exists(table_name):
            logger.info(f"表 {table_name} 已存在，跳过创建")
            return True
        
        if self.config.dry_run:
            logger.info(f"[DRY-RUN] 将创建表: {self.config.schema}.{table_name}")
            return True
        
        conn = self._get_conn()
        source_table = self.config.qualified_table
        
        with conn.cursor() as cur:
            # 使用 CREATE TABLE ... LIKE 复制表结构（包括约束和索引）
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS "{self.config.schema}"."{table_name}" 
                (LIKE {source_table} INCLUDING ALL)
            """)
            conn.commit()
        
        logger.info(f"已创建表: {self.config.schema}.{table_name}")
        return True
    
    def get_target_table_count(self, table_name: str) -> int:
        """获取目标表当前行数"""
        conn = self._get_conn()
        target_qualified = f'"{self.config.schema}"."{table_name}"'
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) as cnt FROM {target_qualified}")
            row = cur.fetchone()
            return row.get("cnt", 0) if row else 0
    
    def copy_data_for_collection(
        self,
        collection_id: str,
        target_table: str,
        expected_count: int,
    ) -> int:
        """
        幂等复制指定 collection 的数据到目标表
        
        使用 INSERT ... ON CONFLICT DO NOTHING 实现幂等性。
        分批处理，支持断点续传。
        
        Args:
            collection_id: 要复制的 collection_id
            target_table: 目标表名
            expected_count: 预期复制的记录数
        
        Returns:
            实际复制的记录数
        """
        if self.config.dry_run:
            logger.info(
                f"[DRY-RUN] 将复制 {expected_count} 条记录到 "
                f"{self.config.schema}.{target_table}"
            )
            return expected_count
        
        conn = self._get_conn()
        source_table = self.config.qualified_table
        batch_size = self.config.batch_size
        total_copied = 0
        retries = 0
        
        target_qualified = f'"{self.config.schema}"."{target_table}"'
        
        # 检查已有数据量
        already_exists = self.get_target_table_count(target_table)
        if already_exists > 0:
            logger.info(f"目标表 {target_table} 已有 {already_exists} 条记录")
        
        # 获取源表的列名列表（用于 INSERT）
        with conn.cursor() as cur:
            cur.execute("""
                SELECT column_name 
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY ordinal_position
            """, (self.config.schema, self.config.base_table))
            columns = [row["column_name"] for row in cur.fetchall()]
        
        columns_str = ", ".join(f'"{c}"' for c in columns)
        
        while True:
            try:
                with conn.cursor() as cur:
                    # 幂等插入：使用 ON CONFLICT DO NOTHING
                    # 基于 collection_id 列过滤（NULL 值视为默认 collection）
                    cur.execute(f"""
                        INSERT INTO {target_qualified} ({columns_str})
                        SELECT {columns_str} FROM {source_table} s
                        WHERE COALESCE(s.collection_id, %s) = %s
                        AND NOT EXISTS (
                            SELECT 1 FROM {target_qualified} t
                            WHERE t.chunk_id = s.chunk_id
                        )
                        LIMIT %s
                        ON CONFLICT (chunk_id) DO NOTHING
                    """, (self.config.default_collection_id, collection_id, batch_size))
                    
                    inserted = cur.rowcount
                    conn.commit()
                    
                    if inserted == 0:
                        break
                    
                    total_copied += inserted
                    retries = 0
                    
                    if self.config.verbose:
                        logger.info(f"已复制 {total_copied}/{expected_count} 条记录到 {target_table}...")
                    
            except Exception as e:
                conn.rollback()
                retries += 1
                if retries >= self.config.max_retries:
                    logger.error(f"复制失败，已重试 {retries} 次: {e}")
                    raise
                logger.warning(f"复制出错，第 {retries} 次重试: {e}")
                time.sleep(self.config.retry_delay)
        
        logger.info(f"复制完成: {total_copied} 条新记录到 {target_table}")
        return total_copied
    
    def verify_counts(self, collection_counts: Dict[str, int]) -> Tuple[bool, List[str]]:
        """
        验证各目标表计数与源表一致
        
        Args:
            collection_counts: {collection_id: expected_count}
        
        Returns:
            (all_ok, error_messages)
        """
        if self.config.dry_run:
            logger.info("[DRY-RUN] 将验证计数一致性")
            return True, []
        
        if not self.config.verify_counts:
            logger.info("跳过计数校验（verify_counts=False）")
            return True, []
        
        conn = self._get_conn()
        errors = []
        
        for collection_id, expected_count in collection_counts.items():
            # 使用 to_pgvector_table_name 获取目标表名
            table_name = to_pgvector_table_name(collection_id)
            target_qualified = f'"{self.config.schema}"."{table_name}"'
            
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT COUNT(*) as cnt FROM {target_qualified}")
                    actual_count = cur.fetchone().get("cnt", 0)
                
                if actual_count != expected_count:
                    errors.append(
                        f"{collection_id}: 期望 {expected_count}, 实际 {actual_count} (差异 {actual_count - expected_count})"
                    )
                else:
                    if self.config.verbose:
                        logger.info(f"验证通过: {collection_id} = {actual_count}")
                        
            except Exception as e:
                errors.append(f"{collection_id}: 验证失败 - {e}")
        
        return len(errors) == 0, errors
    
    def migrate(self, include_plan: bool = False) -> MigrationResult:
        """执行迁移"""
        start_time = time.time()
        errors = []
        rows_migrated = 0
        plan = None
        
        try:
            # 获取计划信息（在执行前）
            if include_plan or self.config.dry_run:
                plan = self.get_plan()
                # 如果 collection_id 列不存在，直接返回错误
                if not plan.get("collection_id_column_exists", True):
                    return MigrationResult(
                        success=False,
                        message=plan.get("error", "源表没有 collection_id 列"),
                        errors=[plan.get("error", "源表没有 collection_id 列")],
                        dry_run=self.config.dry_run,
                        plan=plan,
                    )
            
            # Step 0: 检查 collection_id 列是否存在
            logger.info("Step 0/5: 检查源表结构...")
            self.check_collection_id_column_exists()
            logger.info(f"源表 {self.config.qualified_table} 有 collection_id 列，可以继续")
            
            # Step 1: 获取 collection 分布
            logger.info("Step 1/5: 分析 collection 分布...")
            full_distribution = self.get_collection_distribution()
            
            if not full_distribution:
                return MigrationResult(
                    success=True,
                    message="源表为空，无需迁移",
                    dry_run=self.config.dry_run,
                    plan=plan,
                )
            
            logger.info(f"发现 {len(full_distribution)} 个 collection，共 {sum(full_distribution.values())} 条记录")
            
            # Step 2: 应用 allowlist 过滤
            logger.info("Step 2/5: 应用 collection 过滤...")
            distribution = self.filter_by_allowlist(full_distribution)
            
            if not distribution:
                return MigrationResult(
                    success=True,
                    message="allowlist 过滤后没有需要处理的 collection",
                    dry_run=self.config.dry_run,
                    plan=plan,
                )
            
            total_rows = sum(distribution.values())
            logger.info(f"将处理 {len(distribution)} 个 collection，共 {total_rows} 条记录:")
            for cid, cnt in sorted(distribution.items()):
                table_name = to_pgvector_table_name(cid)
                logger.info(f"  - {cid}: {cnt} 条 -> {table_name}")
            
            # Step 3: 创建目标表
            logger.info("Step 3/5: 创建目标表...")
            for collection_id in distribution.keys():
                table_name = to_pgvector_table_name(collection_id)
                self.create_collection_table(table_name)
            
            # Step 4: 复制数据
            logger.info("Step 4/5: 复制数据...")
            for collection_id, expected_count in distribution.items():
                table_name = to_pgvector_table_name(collection_id)
                copied = self.copy_data_for_collection(
                    collection_id=collection_id,
                    target_table=table_name,
                    expected_count=expected_count,
                )
                rows_migrated += copied
            
            # Step 5: 验证一致性
            logger.info("Step 5/5: 验证计数一致性...")
            all_ok, verify_errors = self.verify_counts(distribution)
            
            if not all_ok:
                errors.extend(verify_errors)
                logger.warning(f"验证发现 {len(verify_errors)} 个问题:")
                for err in verify_errors:
                    logger.warning(f"  - {err}")
            else:
                logger.info("验证通过，所有计数一致")
            
            duration = time.time() - start_time
            return MigrationResult(
                success=all_ok,
                message="按表方案迁移完成" if all_ok else "迁移完成但存在计数不一致",
                rows_processed=total_rows,
                rows_migrated=rows_migrated,
                errors=errors,
                duration_seconds=duration,
                dry_run=self.config.dry_run,
                plan=plan,
            )
            
        except Exception as e:
            duration = time.time() - start_time
            errors.append(str(e))
            logger.error(f"迁移失败: {e}")
            import traceback
            traceback.print_exc()
            return MigrationResult(
                success=False,
                message=f"迁移失败: {e}",
                rows_migrated=rows_migrated,
                errors=errors,
                duration_seconds=duration,
                dry_run=self.config.dry_run,
                plan=plan,
            )
        finally:
            self.close()


# ============ 合并到共享表迁移器 ============


class ConsolidateToSharedTableMigrator:
    """
    合并到共享表迁移器
    
    将多个 step3_chunks_* 表合并到一个共享表中。
    
    执行步骤：
    1. 扫描 schema 下匹配的源表
    2. 为每个源表反推 canonical collection_id
    3. 分批复制数据到目标表，强制写入 collection_id
    4. 校验：每表 rowcount 对齐、抽样校验
    
    幂等性：
    - conflict_strategy='skip': 使用 ON CONFLICT DO NOTHING
    - conflict_strategy='upsert': 使用 ON CONFLICT DO UPDATE
    """
    
    def __init__(self, config: ConsolidateConfig):
        self.config = config
        self._conn = None
        self._collection_mapping = None
    
    def _get_conn(self):
        if self._conn is None:
            self._conn = get_connection_from_dsn(self.config.dsn)
        return self._conn
    
    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
    
    def _get_collection_mapping(self) -> Dict[str, str]:
        """获取表名到 collection_id 的映射（懒加载）"""
        if self._collection_mapping is None:
            self._collection_mapping = self.config.load_collection_mapping()
        return self._collection_mapping
    
    def scan_source_tables(self) -> List[str]:
        """
        扫描 schema 下匹配的源表
        
        过滤逻辑：
        1. 匹配 table_pattern (LIKE)
        2. 如果有 table_allowlist，只保留白名单中的表
        3. 如果有 table_regex，只保留匹配正则的表
        4. 排除 exclude_tables 中的表
        5. 排除目标表本身
        
        Returns:
            匹配的源表名列表
        """
        conn = self._get_conn()
        
        with conn.cursor() as cur:
            # 使用 LIKE 模式查询
            cur.execute("""
                SELECT table_name 
                FROM information_schema.tables
                WHERE table_schema = %s 
                AND table_name LIKE %s
                AND table_type = 'BASE TABLE'
                ORDER BY table_name
            """, (self.config.schema, self.config.table_pattern))
            
            tables = [row["table_name"] for row in cur.fetchall()]
        
        # 应用 allowlist 过滤
        if self.config.table_allowlist:
            allowlist_set = set(self.config.table_allowlist)
            tables = [t for t in tables if t in allowlist_set]
        
        # 应用正则过滤
        if self.config.table_regex:
            pattern = re.compile(self.config.table_regex)
            tables = [t for t in tables if pattern.match(t)]
        
        # 排除指定表
        exclude_set = set(self.config.exclude_tables)
        exclude_set.add(self.config.target_table)  # 排除目标表
        tables = [t for t in tables if t not in exclude_set]
        
        return tables
    
    def resolve_collection_id(self, table_name: str) -> Optional[str]:
        """
        从表名反推 canonical collection_id
        
        优先级：
        1. 显式映射（collection_mapping）
        2. 使用 from_pgvector_table_name() 自动解析
        
        Args:
            table_name: 源表名
        
        Returns:
            collection_id，解析失败返回 None
        """
        mapping = self._get_collection_mapping()
        
        # 优先使用显式映射
        if table_name in mapping:
            return mapping[table_name]
        
        # 尝试自动解析
        try:
            return from_pgvector_table_name(table_name)
        except (ValueError, Exception) as e:
            logger.warning(f"无法从表名 '{table_name}' 解析 collection_id: {e}")
            return None
    
    def get_table_row_count(self, table_name: str) -> int:
        """获取表的行数"""
        conn = self._get_conn()
        qualified_table = f'"{self.config.schema}"."{table_name}"'
        
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) as cnt FROM {qualified_table}")
            row = cur.fetchone()
            return row.get("cnt", 0) if row else 0
    
    def estimate_table_row_count(self, table_name: str) -> Tuple[int, bool]:
        """
        估算表的行数（优先使用 pg_stat 快速估算）
        
        Returns:
            (row_count, is_exact): 行数和是否为精确值
        """
        conn = self._get_conn()
        with conn.cursor() as cur:
            # 尝试从 pg_stat_user_tables 获取估算值
            cur.execute("""
                SELECT n_live_tup 
                FROM pg_stat_user_tables 
                WHERE schemaname = %s AND relname = %s
            """, (self.config.schema, table_name))
            row = cur.fetchone()
            if row and row.get("n_live_tup", 0) > 0:
                return row["n_live_tup"], False  # 估算值
        
        # 回退到精确计数
        return self.get_table_row_count(table_name), True
    
    def get_plan(self) -> Dict[str, Any]:
        """
        获取迁移计划详情
        
        返回：
        - source_tables: 扫描到的源表列表及详情
        - skipped_tables: 被跳过的表及原因
        - target_table: 目标共享表名
        - conflict_strategy: 冲突处理策略
        - verify_counts: 是否启用行数校验
        - sample_verify: 是否启用抽样校验
        - sample_size: 抽样数量
        - total_estimated_rows: 预计迁移的总行数
        - total_exact: 行数是否为精确值
        """
        # 扫描源表
        source_tables_list = self.scan_source_tables()
        
        source_tables = []
        skipped_tables = []
        total_estimated = 0
        all_exact = True
        
        for table in source_tables_list:
            # 解析 collection_id
            collection_id = self.resolve_collection_id(table)
            
            if collection_id is None:
                skipped_tables.append({
                    "table_name": table,
                    "reason": "无法解析 collection_id",
                })
                continue
            
            # 估算行数
            row_count, is_exact = self.estimate_table_row_count(table)
            if not is_exact:
                all_exact = False
            
            total_estimated += row_count
            
            source_tables.append({
                "table_name": table,
                "collection_id": collection_id,
                "estimated_rows": row_count,
                "row_count_exact": is_exact,
            })
        
        return {
            "source_tables": source_tables,
            "skipped_tables": skipped_tables,
            "target_table": self.config.qualified_target_table,
            "conflict_strategy": self.config.conflict_strategy,
            "verify_counts": self.config.verify_counts,
            "sample_verify": self.config.sample_verify,
            "sample_size": self.config.sample_size,
            "total_estimated_rows": total_estimated,
            "total_exact": all_exact,
            "batch_size": self.config.batch_size,
            "table_pattern": self.config.table_pattern,
            "table_allowlist": self.config.table_allowlist,
            "table_regex": self.config.table_regex,
            "exclude_tables": self.config.exclude_tables,
            "source_table_count": len(source_tables),
            "skipped_table_count": len(skipped_tables),
        }
    
    def get_target_collection_count(self, collection_id: str) -> int:
        """获取目标表中指定 collection_id 的行数"""
        conn = self._get_conn()
        target_table = self.config.qualified_target_table
        
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT COUNT(*) as cnt 
                FROM {target_table} 
                WHERE collection_id = %s
            """, (collection_id,))
            row = cur.fetchone()
            return row.get("cnt", 0) if row else 0
    
    def ensure_target_table_has_collection_id(self) -> bool:
        """确保目标表有 collection_id 列"""
        conn = self._get_conn()
        
        with conn.cursor() as cur:
            # 检查列是否存在
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = %s 
                    AND table_name = %s 
                    AND column_name = 'collection_id'
                )
            """, (self.config.schema, self.config.target_table))
            row = cur.fetchone()
            
            if row and row.get("exists", False):
                return True
            
            if self.config.dry_run:
                logger.info(f"[DRY-RUN] 将在目标表添加 collection_id 列")
                return True
            
            # 添加列
            target_table = self.config.qualified_target_table
            cur.execute(f"ALTER TABLE {target_table} ADD COLUMN IF NOT EXISTS collection_id TEXT")
            conn.commit()
            logger.info("已在目标表添加 collection_id 列")
        
        return True
    
    def copy_table_to_shared(
        self,
        source_table: str,
        collection_id: str,
    ) -> Tuple[int, List[str]]:
        """
        将源表数据复制到共享表
        
        分批处理，支持断点续传。
        
        Args:
            source_table: 源表名
            collection_id: 要写入的 collection_id
        
        Returns:
            (copied_count, errors) 复制的行数和错误列表
        """
        errors = []
        total_copied = 0
        retries = 0
        
        source_qualified = f'"{self.config.schema}"."{source_table}"'
        target_table = self.config.qualified_target_table
        batch_size = self.config.batch_size
        
        if self.config.dry_run:
            count = self.get_table_row_count(source_table)
            logger.info(
                f"[DRY-RUN] 将复制 {count} 条记录从 {source_table} 到目标表, "
                f"collection_id={collection_id}"
            )
            return count, errors
        
        conn = self._get_conn()
        
        # 构建 INSERT SQL
        # 根据冲突策略选择 ON CONFLICT 行为
        if self.config.conflict_strategy == "upsert":
            conflict_clause = """
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
            """
        else:  # 'skip'
            conflict_clause = "ON CONFLICT (chunk_id) DO NOTHING"
        
        # 获取源表的列（排除 collection_id，因为我们要覆盖它）
        with conn.cursor() as cur:
            cur.execute("""
                SELECT column_name 
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                AND column_name != 'collection_id'
                ORDER BY ordinal_position
            """, (self.config.schema, source_table))
            source_columns = [row["column_name"] for row in cur.fetchall()]
        
        # 构建列列表（排除 collection_id，我们会强制写入）
        columns_list = ", ".join(source_columns)
        
        # 分批复制
        offset = 0
        while True:
            try:
                with conn.cursor() as cur:
                    # 批量 INSERT ... SELECT
                    # 强制写入指定的 collection_id（覆盖源表的值）
                    insert_sql = f"""
                        INSERT INTO {target_table} ({columns_list}, collection_id)
                        SELECT {columns_list}, %s as collection_id
                        FROM {source_qualified}
                        WHERE chunk_id NOT IN (
                            SELECT chunk_id FROM {target_table}
                            WHERE collection_id = %s
                        )
                        LIMIT %s
                        {conflict_clause}
                    """
                    
                    cur.execute(insert_sql, (collection_id, collection_id, batch_size))
                    inserted = cur.rowcount
                    conn.commit()
                    
                    if inserted == 0:
                        break
                    
                    total_copied += inserted
                    retries = 0
                    offset += batch_size
                    
                    if self.config.verbose:
                        logger.info(f"已复制 {total_copied} 条记录从 {source_table}...")
                    
            except Exception as e:
                conn.rollback()
                retries += 1
                if retries >= self.config.max_retries:
                    error_msg = f"复制 {source_table} 失败，已重试 {retries} 次: {e}"
                    logger.error(error_msg)
                    errors.append(error_msg)
                    break
                logger.warning(f"复制 {source_table} 出错，第 {retries} 次重试: {e}")
                time.sleep(self.config.retry_delay)
        
        logger.info(f"复制完成: {source_table} -> {total_copied} 条记录")
        return total_copied, errors
    
    def verify_table_migration(
        self,
        source_table: str,
        collection_id: str,
    ) -> Tuple[bool, List[str]]:
        """
        校验单表迁移结果
        
        校验项：
        1. 行数对齐
        2. 抽样校验 chunk_id 存在性
        
        Args:
            source_table: 源表名
            collection_id: collection_id
        
        Returns:
            (success, errors)
        """
        errors = []
        
        if self.config.dry_run:
            logger.info(f"[DRY-RUN] 将校验 {source_table} 迁移结果")
            return True, errors
        
        conn = self._get_conn()
        source_qualified = f'"{self.config.schema}"."{source_table}"'
        target_table = self.config.qualified_target_table
        
        # 1. 行数校验
        if self.config.verify_counts:
            source_count = self.get_table_row_count(source_table)
            target_count = self.get_target_collection_count(collection_id)
            
            if source_count != target_count:
                errors.append(
                    f"{source_table}: 行数不一致，源={source_count}, 目标={target_count}"
                )
            elif self.config.verbose:
                logger.info(f"行数校验通过: {source_table} = {source_count}")
        
        # 2. 抽样校验
        if self.config.sample_verify:
            with conn.cursor() as cur:
                # 从源表随机抽样
                cur.execute(f"""
                    SELECT chunk_id FROM {source_qualified}
                    ORDER BY RANDOM()
                    LIMIT %s
                """, (self.config.sample_size,))
                sample_ids = [row["chunk_id"] for row in cur.fetchall()]
                
                if sample_ids:
                    # 检查这些 chunk_id 是否存在于目标表
                    placeholders = ", ".join(["%s"] * len(sample_ids))
                    cur.execute(f"""
                        SELECT chunk_id FROM {target_table}
                        WHERE collection_id = %s
                        AND chunk_id IN ({placeholders})
                    """, [collection_id] + sample_ids)
                    
                    found_ids = {row["chunk_id"] for row in cur.fetchall()}
                    missing_ids = set(sample_ids) - found_ids
                    
                    if missing_ids:
                        errors.append(
                            f"{source_table}: 抽样校验失败，{len(missing_ids)}/{len(sample_ids)} "
                            f"chunk_id 未找到"
                        )
                    elif self.config.verbose:
                        logger.info(f"抽样校验通过: {source_table} ({len(sample_ids)} samples)")
        
        return len(errors) == 0, errors
    
    def migrate(self, include_plan: bool = False) -> MigrationResult:
        """执行合并迁移"""
        start_time = time.time()
        errors = []
        rows_migrated = 0
        rows_processed = 0
        table_results = {}
        plan = None
        
        try:
            # 获取计划信息（在执行前）
            if include_plan or self.config.dry_run:
                plan = self.get_plan()
            
            # Step 1: 扫描源表
            logger.info("Step 1/5: 扫描源表...")
            source_tables = self.scan_source_tables()
            
            if not source_tables:
                return MigrationResult(
                    success=True,
                    message="未找到匹配的源表",
                    dry_run=self.config.dry_run,
                    plan=plan,
                )
            
            logger.info(f"发现 {len(source_tables)} 个源表: {', '.join(source_tables)}")
            
            # Step 2: 解析 collection_id
            logger.info("Step 2/5: 解析 collection_id...")
            table_collection_map = {}
            skipped_tables = []
            
            for table in source_tables:
                cid = self.resolve_collection_id(table)
                if cid:
                    table_collection_map[table] = cid
                    logger.info(f"  {table} -> {cid}")
                else:
                    skipped_tables.append(table)
                    logger.warning(f"  {table} -> 无法解析，跳过")
            
            if skipped_tables:
                errors.append(f"跳过 {len(skipped_tables)} 个无法解析的表: {', '.join(skipped_tables)}")
            
            if not table_collection_map:
                return MigrationResult(
                    success=False,
                    message="所有表都无法解析 collection_id",
                    errors=errors,
                    dry_run=self.config.dry_run,
                    plan=plan,
                )
            
            # Step 3: 确保目标表有 collection_id 列
            logger.info("Step 3/5: 确保目标表结构...")
            self.ensure_target_table_has_collection_id()
            
            # Step 4: 复制数据
            logger.info("Step 4/5: 复制数据...")
            for table, collection_id in table_collection_map.items():
                source_count = self.get_table_row_count(table)
                rows_processed += source_count
                
                logger.info(f"处理 {table} ({source_count} 条记录)...")
                copied, copy_errors = self.copy_table_to_shared(table, collection_id)
                
                rows_migrated += copied
                errors.extend(copy_errors)
                table_results[table] = {
                    "collection_id": collection_id,
                    "source_count": source_count,
                    "copied": copied,
                    "errors": copy_errors,
                }
            
            # Step 5: 校验
            logger.info("Step 5/5: 校验迁移结果...")
            verify_errors = []
            for table, collection_id in table_collection_map.items():
                ok, table_errors = self.verify_table_migration(table, collection_id)
                verify_errors.extend(table_errors)
                table_results[table]["verified"] = ok
            
            errors.extend(verify_errors)
            
            # 汇总结果
            all_ok = len(errors) == 0
            duration = time.time() - start_time
            
            # 输出详细结果
            logger.info("=" * 50)
            logger.info("迁移结果汇总:")
            for table, result in table_results.items():
                status = "✓" if result.get("verified", True) else "✗"
                logger.info(
                    f"  {status} {table}: {result['source_count']} -> {result['copied']} "
                    f"(collection_id={result['collection_id']})"
                )
            
            # dry-run 时使用计划中的估算值
            if self.config.dry_run and plan:
                rows_processed = plan.get("total_estimated_rows", 0)
                rows_migrated = rows_processed  # dry-run 假设全部迁移
            
            return MigrationResult(
                success=all_ok,
                message="合并到共享表完成" if all_ok else "迁移完成但存在问题",
                rows_processed=rows_processed,
                rows_migrated=rows_migrated,
                errors=errors,
                duration_seconds=duration,
                dry_run=self.config.dry_run,
                plan=plan,
            )
            
        except Exception as e:
            duration = time.time() - start_time
            errors.append(str(e))
            logger.error(f"迁移失败: {e}")
            import traceback
            traceback.print_exc()
            return MigrationResult(
                success=False,
                message=f"迁移失败: {e}",
                rows_migrated=rows_migrated,
                errors=errors,
                duration_seconds=duration,
                dry_run=self.config.dry_run,
                plan=plan,
            )
        finally:
            self.close()


def get_connection_from_dsn(dsn: str):
    """从 DSN 获取数据库连接"""
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        raise RuntimeError(
            "psycopg (v3) 未安装，请运行: pip install psycopg[binary]"
        )
    
    return psycopg.connect(dsn, row_factory=dict_row)


# ============ CLI 入口 ============


def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="PGVector Collection 迁移脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # 单表方案 - dry-run
    python pgvector_collection_migrate.py shared-table --dry-run
    
    # 单表方案 - 实际执行
    python pgvector_collection_migrate.py shared-table
    
    # 按表方案 - dry-run
    python pgvector_collection_migrate.py table-per-collection --dry-run
    
    # 按表方案 - 指定批次大小
    python pgvector_collection_migrate.py table-per-collection --batch-size 500
    
    # 合并到共享表 - dry-run
    python pgvector_collection_migrate.py consolidate-to-shared-table --dry-run
    
    # 合并到共享表 - 指定参数
    python pgvector_collection_migrate.py consolidate-to-shared-table \\
        --target-table chunks \\
        --conflict-strategy upsert \\
        --table-regex "step3_chunks_proj.*"
""",
    )
    
    # 子命令
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # shared-table 子命令
    shared_parser = subparsers.add_parser(
        "shared-table",
        help="单表方案: 添加 collection_id 列并回填",
    )
    shared_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只显示将执行的操作，不实际修改数据库",
    )
    shared_parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="每批处理的记录数 (默认 1000)",
    )
    shared_parser.add_argument(
        "--default-collection-id",
        default=None,
        help="默认 collection_id，用于覆盖空 project_key (默认基于 chunking-version 和 embedding-model-id 生成)",
    )
    shared_parser.add_argument(
        "--chunking-version",
        default=None,
        help="分块版本号，用于生成 collection_id (默认从环境变量 CHUNKING_VERSION 读取，或使用 'v1')",
    )
    shared_parser.add_argument(
        "--embedding-model-id",
        default=None,
        help="Embedding 模型 ID，用于生成 collection_id (默认从环境变量 STEP3_EMBEDDING_MODEL 读取，或使用 'nomodel')",
    )
    shared_parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="显示详细进度",
    )
    shared_parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出结果",
    )
    shared_parser.add_argument(
        "--plan-json",
        action="store_true",
        help="输出详细计划信息（JSON 格式），包含列/索引存在状态、预期回填行数等",
    )
    # 确认机制参数
    shared_parser.add_argument(
        "--yes", "-y",
        action="store_true",
        dest="auto_yes",
        help="跳过交互确认，直接执行（用于 CI/自动化）",
    )
    shared_parser.add_argument(
        "--no-require-confirm",
        action="store_true",
        help="不要求确认（等同于 --yes）",
    )
    # 备份环境检查参数
    shared_parser.add_argument(
        "--require-backup-env",
        action="store_true",
        default=True,
        help="要求设置备份环境变量 (ENGRAM_BACKUP_OK=1 或 BACKUP_TAG)，默认启用",
    )
    shared_parser.add_argument(
        "--no-require-backup-env",
        action="store_true",
        help="跳过备份环境变量检查（仅限测试/演练环境）",
    )
    # Schema 安全参数
    shared_parser.add_argument(
        "--allow-public-schema",
        action="store_true",
        help="允许 public schema 作为迁移目标（默认禁止，仅限开发/测试环境）",
    )
    
    # table-per-collection 子命令
    table_parser = subparsers.add_parser(
        "table-per-collection",
        help="按表方案: 按 collection 分表存储",
    )
    table_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只显示将执行的操作，不实际修改数据库",
    )
    table_parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="每批处理的记录数 (默认 1000)",
    )
    table_parser.add_argument(
        "--default-collection-id",
        default=None,
        help="默认 collection_id，用于 NULL collection_id 的记录 (默认基于 chunking-version 和 embedding-model-id 生成)",
    )
    table_parser.add_argument(
        "--chunking-version",
        default=None,
        help="分块版本号，用于生成默认 collection_id (默认从环境变量 CHUNKING_VERSION 读取，或使用 'v1')",
    )
    table_parser.add_argument(
        "--embedding-model-id",
        default=None,
        help="Embedding 模型 ID，用于生成默认 collection_id (默认从环境变量 STEP3_EMBEDDING_MODEL 读取，或使用 'nomodel')",
    )
    table_parser.add_argument(
        "--collection-allowlist",
        nargs="+",
        help="只处理指定的 collection_id 列表（空格分隔）。不指定则处理所有 collection。",
    )
    table_parser.add_argument(
        "--no-verify-counts",
        action="store_true",
        help="跳过迁移后的计数校验",
    )
    table_parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="显示详细进度",
    )
    table_parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出结果",
    )
    table_parser.add_argument(
        "--plan-json",
        action="store_true",
        help="输出详细计划信息（JSON 格式），包含 collection 分布、目标表名、预期行数等",
    )
    # 确认机制参数
    table_parser.add_argument(
        "--yes", "-y",
        action="store_true",
        dest="auto_yes",
        help="跳过交互确认，直接执行（用于 CI/自动化）",
    )
    table_parser.add_argument(
        "--no-require-confirm",
        action="store_true",
        help="不要求确认（等同于 --yes）",
    )
    # 备份环境检查参数
    table_parser.add_argument(
        "--require-backup-env",
        action="store_true",
        default=True,
        help="要求设置备份环境变量 (ENGRAM_BACKUP_OK=1 或 BACKUP_TAG)，默认启用",
    )
    table_parser.add_argument(
        "--no-require-backup-env",
        action="store_true",
        help="跳过备份环境变量检查（仅限测试/演练环境）",
    )
    # Schema 安全参数
    table_parser.add_argument(
        "--allow-public-schema",
        action="store_true",
        help="允许 public schema 作为迁移目标（默认禁止，仅限开发/测试环境）",
    )
    
    # consolidate-to-shared-table 子命令
    consolidate_parser = subparsers.add_parser(
        "consolidate-to-shared-table",
        help="合并到共享表: 将多个 step3_chunks_* 表合并到一个共享表",
    )
    consolidate_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只显示将执行的操作，不实际修改数据库",
    )
    consolidate_parser.add_argument(
        "--target-table",
        default="chunks",
        help="目标共享表名 (默认 chunks)",
    )
    consolidate_parser.add_argument(
        "--table-pattern",
        default="step3_chunks_%",
        help="源表 LIKE 模式 (默认 step3_chunks_%%)",
    )
    consolidate_parser.add_argument(
        "--table-allowlist",
        nargs="+",
        help="源表白名单（空格分隔）",
    )
    consolidate_parser.add_argument(
        "--table-regex",
        help="源表正则表达式过滤",
    )
    consolidate_parser.add_argument(
        "--exclude-tables",
        nargs="+",
        default=[],
        help="要排除的表（空格分隔）",
    )
    consolidate_parser.add_argument(
        "--collection-mapping-file",
        help="表名到 collection_id 的映射文件 (JSON 格式)",
    )
    consolidate_parser.add_argument(
        "--conflict-strategy",
        choices=["skip", "upsert"],
        default="skip",
        help="冲突处理策略: skip (DO NOTHING) 或 upsert (DO UPDATE) (默认 skip)",
    )
    consolidate_parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="每批处理的记录数 (默认 1000)",
    )
    consolidate_parser.add_argument(
        "--no-verify-counts",
        action="store_true",
        help="跳过行数校验",
    )
    consolidate_parser.add_argument(
        "--no-sample-verify",
        action="store_true",
        help="跳过抽样校验",
    )
    consolidate_parser.add_argument(
        "--sample-size",
        type=int,
        default=100,
        help="每表抽样校验数量 (默认 100)",
    )
    consolidate_parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="显示详细进度",
    )
    consolidate_parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出结果",
    )
    consolidate_parser.add_argument(
        "--plan-json",
        action="store_true",
        help="输出详细计划信息（JSON 格式），包含源表列表、collection_id 映射、行数估算等",
    )
    # 确认机制参数
    consolidate_parser.add_argument(
        "--yes", "-y",
        action="store_true",
        dest="auto_yes",
        help="跳过交互确认，直接执行（用于 CI/自动化）",
    )
    consolidate_parser.add_argument(
        "--no-require-confirm",
        action="store_true",
        help="不要求确认（等同于 --yes）",
    )
    # 备份环境检查参数
    consolidate_parser.add_argument(
        "--require-backup-env",
        action="store_true",
        default=True,
        help="要求设置备份环境变量 (ENGRAM_BACKUP_OK=1 或 BACKUP_TAG)，默认启用",
    )
    consolidate_parser.add_argument(
        "--no-require-backup-env",
        action="store_true",
        help="跳过备份环境变量检查（仅限测试/演练环境）",
    )
    # Schema 安全参数
    consolidate_parser.add_argument(
        "--allow-public-schema",
        action="store_true",
        help="允许 public schema 作为迁移目标（默认禁止，仅限开发/测试环境）",
    )
    
    return parser.parse_args()


def main() -> int:
    """主入口"""
    args = parse_args()
    
    # 解析确认机制参数
    auto_yes = getattr(args, 'auto_yes', False) or getattr(args, 'no_require_confirm', False)
    require_backup = not getattr(args, 'no_require_backup_env', False)
    allow_public_schema = getattr(args, 'allow_public_schema', False)
    
    # 根据命令选择配置类型
    if args.command == "consolidate-to-shared-table":
        # 使用 ConsolidateConfig
        config = ConsolidateConfig.from_env()
        config.dry_run = args.dry_run
        config.batch_size = args.batch_size
        config.verbose = args.verbose
        config.target_table = args.target_table
        config.table_pattern = args.table_pattern
        config.table_allowlist = args.table_allowlist
        config.table_regex = args.table_regex
        config.exclude_tables = args.exclude_tables or []
        config.collection_mapping_file = args.collection_mapping_file
        config.conflict_strategy = args.conflict_strategy
        config.verify_counts = not args.no_verify_counts
        config.sample_verify = not args.no_sample_verify
        config.sample_size = args.sample_size
        
        # 检查密码
        if not config.password:
            logger.error("错误: POSTGRES_PASSWORD 环境变量未设置")
            return 1
        
        # Schema/Table 白名单校验（仅非 dry-run 时严格执行）
        if not config.dry_run:
            try:
                validate_schema_name(config.schema, allow_public=allow_public_schema)
                validate_table_name(config.target_table)
                logger.info(f"Schema/Table 校验通过: {config.schema}.{config.target_table}")
            except ValueError as e:
                logger.error(f"Schema/Table 校验失败: {e}")
                return 1
        
        migrator = ConsolidateToSharedTableMigrator(config)
        
        logger.info(f"开始迁移: {args.command}")
        logger.info(f"  数据库: {config.host}:{config.port}/{config.database}")
        logger.info(f"  目标表: {config.qualified_target_table}")
        logger.info(f"  源表模式: {config.table_pattern}")
        logger.info(f"  冲突策略: {config.conflict_strategy}")
        logger.info(f"  dry-run: {config.dry_run}")
        logger.info(f"  batch-size: {config.batch_size}")
        
    else:
        # 使用 MigrationConfig
        config = MigrationConfig.from_env()
        config.dry_run = args.dry_run
        config.batch_size = args.batch_size
        config.verbose = args.verbose
        
        # 处理 chunking_version 和 embedding_model_id 参数
        # 优先级: CLI 参数 > 环境变量 > 默认值（from_env 已处理后两者）
        if args.chunking_version:
            config.chunking_version = args.chunking_version
        if args.embedding_model_id:
            config.embedding_model_id = args.embedding_model_id
        
        # 处理 default_collection_id 参数
        # 如果显式指定了 --default-collection-id，使用该值
        # 否则基于 chunking_version 和 embedding_model_id 生成
        if args.default_collection_id:
            config.default_collection_id = args.default_collection_id
        else:
            # 使用 make_collection_id 生成默认值
            config.default_collection_id = make_collection_id(
                project_key="default",
                chunking_version=config.chunking_version,
                embedding_model_id=config.embedding_model_id,
            )
        
        # table-per-collection 专用参数
        if args.command == "table-per-collection":
            config.collection_allowlist = getattr(args, 'collection_allowlist', None)
            config.verify_counts = not getattr(args, 'no_verify_counts', False)
        
        # 检查密码
        if not config.password:
            logger.error("错误: POSTGRES_PASSWORD 环境变量未设置")
            return 1
        
        # Schema/Table 白名单校验（仅非 dry-run 时严格执行）
        if not config.dry_run:
            try:
                validate_schema_name(config.schema, allow_public=allow_public_schema)
                validate_table_name(config.base_table)
                logger.info(f"Schema/Table 校验通过: {config.schema}.{config.base_table}")
            except ValueError as e:
                logger.error(f"Schema/Table 校验失败: {e}")
                return 1
        
        # 执行迁移
        if args.command == "shared-table":
            migrator = SharedTableMigrator(config)
        elif args.command == "table-per-collection":
            migrator = TablePerCollectionMigrator(config)
        else:
            logger.error(f"未知命令: {args.command}")
            return 1
        
        logger.info(f"开始迁移: {args.command}")
        logger.info(f"  数据库: {config.host}:{config.port}/{config.database}")
        logger.info(f"  源表: {config.qualified_table}")
        logger.info(f"  chunking-version: {config.chunking_version}")
        logger.info(f"  embedding-model-id: {config.embedding_model_id}")
        logger.info(f"  default-collection-id: {config.default_collection_id}")
        if args.command == "table-per-collection":
            if config.collection_allowlist:
                logger.info(f"  collection-allowlist: {config.collection_allowlist}")
            logger.info(f"  verify-counts: {config.verify_counts}")
        logger.info(f"  dry-run: {config.dry_run}")
        logger.info(f"  batch-size: {config.batch_size}")
    
    # 确定是否需要计划信息
    include_plan = getattr(args, 'plan_json', False)
    output_json = getattr(args, 'json', False)
    
    # ============ 生产安全检查（非 dry-run 时执行）============
    if not args.dry_run:
        # 1. 备份环境变量检查
        try:
            check_backup_env(require_backup)
        except RuntimeError as e:
            logger.error(str(e))
            return 1
        
        # 2. 获取并显示迁移计划
        logger.info("获取迁移计划...")
        plan = migrator.get_plan()
        print_plan_summary(plan, args.command)
        
        # 3. 确认执行
        if not auto_yes:
            if not prompt_confirmation("确认执行迁移?"):
                logger.info("用户取消迁移")
                return 0
        else:
            logger.info("自动确认模式，跳过交互确认")
    
    # 执行迁移
    result = migrator.migrate(include_plan=include_plan or output_json)
    
    # 输出结果
    if include_plan:
        # --plan-json 模式：只输出计划信息
        output = {
            "command": args.command,
            "dry_run": args.dry_run,
            "plan": result.plan,
        }
        # 如果同时指定了 --json，也输出完整结果
        if output_json:
            output.update(result.to_dict(include_plan=True))
        print(json.dumps(output, indent=2, ensure_ascii=False))
    elif output_json:
        # --json 模式：输出完整结果（可选包含 plan）
        print(json.dumps(result.to_dict(include_plan=include_plan), indent=2, ensure_ascii=False))
    else:
        logger.info("=" * 50)
        logger.info(f"迁移结果: {'成功' if result.success else '失败'}")
        logger.info(f"  处理行数: {result.rows_processed}")
        logger.info(f"  迁移行数: {result.rows_migrated}")
        logger.info(f"  耗时: {result.duration_seconds:.2f} 秒")
        if result.errors:
            logger.warning(f"  错误数: {len(result.errors)}")
            for err in result.errors:
                logger.warning(f"    - {err}")
    
    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
