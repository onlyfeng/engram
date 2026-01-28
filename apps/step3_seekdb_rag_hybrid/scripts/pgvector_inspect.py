#!/usr/bin/env python3
"""
pgvector_inspect.py - PGVector 信息架构检查脚本

可独立运行，连接 PostgreSQL 并执行信息架构查询，输出 JSON 格式结果。
用于后续策略决策与迁移前置校验。

功能:
- 列出 schema=step3 及 step3_* 下匹配 step3_chunks_% 的表清单
- 每表: 行数、pg_total_relation_size、pg_indexes_size、vector 列 typmod（推导 dim）
- 检查 step3.chunks 是否存在、是否有 collection_id 列与索引

使用方法:
    # 使用环境变量
    export POSTGRES_HOST=localhost
    export POSTGRES_PORT=5432
    export POSTGRES_DB=engram
    export POSTGRES_USER=postgres
    export POSTGRES_PASSWORD=your_password
    python pgvector_inspect.py

    # 使用 DSN
    export PGVECTOR_DSN="postgresql://user:pass@host:5432/db"
    python pgvector_inspect.py

    # 指定 schema 模式
    python pgvector_inspect.py --schema-pattern "step3%"

    # 输出人类可读格式
    python pgvector_inspect.py --pretty

环境变量:
    PGVECTOR_DSN: 完整 DSN 连接字符串（优先）
    POSTGRES_HOST: 主机 (默认 localhost)
    POSTGRES_PORT: 端口 (默认 5432)
    POSTGRES_DB: 数据库 (默认 engram)
    POSTGRES_USER: 用户 (默认 postgres)
    POSTGRES_PASSWORD: 密码 (必需)
    STEP3_PG_SCHEMA: 目标 schema (默认 step3)
    STEP3_SCHEMA: 目标 schema 的别名（已废弃，请改用 STEP3_PG_SCHEMA）
"""

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 添加父目录到路径以便导入
_script_dir = Path(__file__).resolve().parent
_parent_dir = _script_dir.parent
if str(_parent_dir) not in sys.path:
    sys.path.insert(0, str(_parent_dir))

# 导入环境变量兼容层
try:
    from env_compat import get_str
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

# ============ 日志配置 ============

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ============ 数据类定义 ============


@dataclass
class TableInfo:
    """表信息"""
    schema_name: str
    table_name: str
    qualified_name: str
    row_count: int
    total_size_bytes: int
    total_size_human: str
    indexes_size_bytes: int
    indexes_size_human: str
    has_vector_column: bool
    vector_dim: Optional[int] = None
    vector_column_name: Optional[str] = None
    has_collection_id: bool = False
    collection_id_indexed: bool = False
    error: Optional[str] = None


@dataclass
class ChunksTableStatus:
    """chunks 主表状态"""
    exists: bool
    schema_name: str
    table_name: str = "chunks"
    qualified_name: str = ""
    has_collection_id: bool = False
    collection_id_indexed: bool = False
    collection_id_index_name: Optional[str] = None
    row_count: int = 0
    total_size_bytes: int = 0
    total_size_human: str = ""
    error: Optional[str] = None


@dataclass
class InspectResult:
    """检查结果"""
    success: bool
    message: str
    # 连接信息
    database: str = ""
    host: str = ""
    port: int = 0
    # Schema 信息
    schema_pattern: str = ""
    schemas_found: List[str] = field(default_factory=list)
    # 表清单
    tables: List[TableInfo] = field(default_factory=list)
    total_tables: int = 0
    # chunks 主表状态
    chunks_status: Optional[ChunksTableStatus] = None
    # 统计信息
    summary: Dict[str, Any] = field(default_factory=dict)
    # 错误信息
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典（用于 JSON 序列化）"""
        result = {
            "success": self.success,
            "message": self.message,
            "connection": {
                "database": self.database,
                "host": self.host,
                "port": self.port,
            },
            "schema_pattern": self.schema_pattern,
            "schemas_found": self.schemas_found,
            "total_tables": self.total_tables,
            "tables": [asdict(t) for t in self.tables],
            "summary": self.summary,
            "errors": self.errors,
        }
        if self.chunks_status:
            result["chunks_status"] = asdict(self.chunks_status)
        return result


# ============ 配置 ============


@dataclass
class InspectConfig:
    """检查配置"""
    # 连接参数
    host: str = "localhost"
    port: int = 5432
    database: str = "engram"
    user: str = "postgres"
    password: str = ""
    dsn: Optional[str] = None

    # 检查参数
    schema_pattern: str = "step3%"  # LIKE 模式
    table_pattern: str = "step3_chunks_%"  # LIKE 模式
    base_schema: str = "step3"  # 主 schema
    base_table: str = "chunks"  # 主表名

    # 输出参数
    pretty: bool = False
    verbose: bool = False

    @classmethod
    def from_env(cls) -> "InspectConfig":
        """
        从环境变量加载配置
        
        环境变量优先级（STEP3_PG_* canonical，旧名称为 deprecated alias）:
        - STEP3_PG_SCHEMA（canonical）> STEP3_SCHEMA（deprecated alias）（默认 step3）
        """
        # 通过兼容层读取 schema（canonical 优先，legacy 作为别名并触发废弃警告）
        # STEP3_PG_SCHEMA 为 canonical，STEP3_SCHEMA 为 deprecated alias
        base_schema = get_str(
            "STEP3_PG_SCHEMA",
            deprecated_aliases=["STEP3_SCHEMA"],
            default="step3",
        )
        
        return cls(
            dsn=os.environ.get("PGVECTOR_DSN"),
            host=os.environ.get("POSTGRES_HOST", "localhost"),
            port=int(os.environ.get("POSTGRES_PORT", "5432")),
            database=os.environ.get("POSTGRES_DB", "engram"),
            user=os.environ.get("POSTGRES_USER", "postgres"),
            password=os.environ.get("POSTGRES_PASSWORD", ""),
            base_schema=base_schema,
        )

    def get_dsn(self) -> str:
        """获取 DSN 连接字符串"""
        if self.dsn:
            return self.dsn
        return (
            f"postgresql://{self.user}:{self.password}@"
            f"{self.host}:{self.port}/{self.database}"
        )


# ============ 检查器实现 ============


class PGVectorInspector:
    """PGVector 信息架构检查器"""

    def __init__(self, config: InspectConfig):
        self.config = config
        self._conn = None

    def _get_conn(self):
        """获取数据库连接"""
        if self._conn is None:
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError:
                raise RuntimeError(
                    "psycopg (v3) 未安装，请运行: pip install psycopg[binary]"
                )

            self._conn = psycopg.connect(
                self.config.get_dsn(),
                row_factory=dict_row,
            )
        return self._conn

    def close(self):
        """关闭连接"""
        if self._conn:
            self._conn.close()
            self._conn = None

    def _format_bytes(self, size_bytes: int) -> str:
        """格式化字节数为人类可读形式"""
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        elif size_bytes < 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.1f} MB"
        else:
            return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"

    def _get_matching_schemas(self) -> List[str]:
        """获取匹配的 schema 列表"""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT schema_name 
                FROM information_schema.schemata
                WHERE schema_name LIKE %s
                ORDER BY schema_name
            """, (self.config.schema_pattern,))
            return [row["schema_name"] for row in cur.fetchall()]

    def _get_matching_tables(self, schemas: List[str]) -> List[Tuple[str, str]]:
        """获取匹配的表列表 (schema, table)"""
        if not schemas:
            return []

        conn = self._get_conn()
        # 构建 IN 子句的占位符
        placeholders = ", ".join(["%s"] * len(schemas))

        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT table_schema, table_name
                FROM information_schema.tables
                WHERE table_schema IN ({placeholders})
                AND table_name LIKE %s
                AND table_type = 'BASE TABLE'
                ORDER BY table_schema, table_name
            """, schemas + [self.config.table_pattern])
            return [(row["table_schema"], row["table_name"]) for row in cur.fetchall()]

    def _get_table_info(self, schema: str, table: str) -> TableInfo:
        """获取单个表的详细信息"""
        conn = self._get_conn()
        qualified_name = f'"{schema}"."{table}"'

        info = TableInfo(
            schema_name=schema,
            table_name=table,
            qualified_name=qualified_name,
            row_count=0,
            total_size_bytes=0,
            total_size_human="",
            indexes_size_bytes=0,
            indexes_size_human="",
            has_vector_column=False,
        )

        try:
            with conn.cursor() as cur:
                # 1. 获取行数（使用 estimate 加速大表）
                cur.execute(f"""
                    SELECT reltuples::bigint AS estimate
                    FROM pg_class
                    WHERE oid = %s::regclass
                """, (qualified_name,))
                row = cur.fetchone()
                estimate = row["estimate"] if row else 0

                # 如果估算值较小或为 -1，使用精确计数
                if estimate < 10000 or estimate == -1:
                    cur.execute(f"SELECT COUNT(*) AS cnt FROM {qualified_name}")
                    info.row_count = cur.fetchone()["cnt"]
                else:
                    info.row_count = estimate

                # 2. 获取表大小
                cur.execute("""
                    SELECT 
                        pg_total_relation_size(%s::regclass) AS total_size,
                        pg_indexes_size(%s::regclass) AS indexes_size
                """, (qualified_name, qualified_name))
                size_row = cur.fetchone()
                if size_row:
                    info.total_size_bytes = size_row["total_size"]
                    info.total_size_human = self._format_bytes(size_row["total_size"])
                    info.indexes_size_bytes = size_row["indexes_size"]
                    info.indexes_size_human = self._format_bytes(size_row["indexes_size"])

                # 3. 检查 vector 列及其维度
                cur.execute("""
                    SELECT column_name, udt_name, 
                           (SELECT typmod FROM pg_attribute a
                            JOIN pg_class c ON a.attrelid = c.oid
                            JOIN pg_namespace n ON c.relnamespace = n.oid
                            WHERE n.nspname = %s AND c.relname = %s 
                            AND a.attname = column_name) AS typmod
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s
                    AND udt_name = 'vector'
                """, (schema, table, schema, table))
                vector_row = cur.fetchone()
                if vector_row:
                    info.has_vector_column = True
                    info.vector_column_name = vector_row["column_name"]
                    # typmod 存储维度信息（typmod - 4 = 实际维度，因为 pg 添加了 4 字节头）
                    # 但对于 pgvector，typmod 直接就是维度
                    typmod = vector_row.get("typmod")
                    if typmod and typmod > 0:
                        info.vector_dim = typmod

                # 如果 typmod 未能获取维度，尝试直接查询 vector 维度
                if info.has_vector_column and info.vector_dim is None:
                    try:
                        cur.execute(f"""
                            SELECT vector_dims({info.vector_column_name}) AS dim
                            FROM {qualified_name}
                            WHERE {info.vector_column_name} IS NOT NULL
                            LIMIT 1
                        """)
                        dim_row = cur.fetchone()
                        if dim_row and dim_row.get("dim"):
                            info.vector_dim = dim_row["dim"]
                    except Exception:
                        pass  # 表可能为空

                # 4. 检查 collection_id 列
                cur.execute("""
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = %s AND table_name = %s
                        AND column_name = 'collection_id'
                    ) AS has_col
                """, (schema, table))
                info.has_collection_id = cur.fetchone()["has_col"]

                # 5. 检查 collection_id 索引
                if info.has_collection_id:
                    cur.execute("""
                        SELECT EXISTS (
                            SELECT 1 FROM pg_indexes
                            WHERE schemaname = %s AND tablename = %s
                            AND indexdef LIKE '%%collection_id%%'
                        ) AS has_idx
                    """, (schema, table))
                    info.collection_id_indexed = cur.fetchone()["has_idx"]

        except Exception as e:
            info.error = str(e)
            logger.warning(f"获取表 {qualified_name} 信息失败: {e}")

        return info

    def _check_chunks_table(self) -> ChunksTableStatus:
        """检查 chunks 主表状态"""
        schema = self.config.base_schema
        table = self.config.base_table
        qualified_name = f'"{schema}"."{table}"'

        status = ChunksTableStatus(
            exists=False,
            schema_name=schema,
            table_name=table,
            qualified_name=qualified_name,
        )

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                # 1. 检查表是否存在
                cur.execute("""
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.tables
                        WHERE table_schema = %s AND table_name = %s
                    ) AS exists
                """, (schema, table))
                status.exists = cur.fetchone()["exists"]

                if not status.exists:
                    return status

                # 2. 检查 collection_id 列
                cur.execute("""
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = %s AND table_name = %s
                        AND column_name = 'collection_id'
                    ) AS has_col
                """, (schema, table))
                status.has_collection_id = cur.fetchone()["has_col"]

                # 3. 检查 collection_id 索引
                if status.has_collection_id:
                    cur.execute("""
                        SELECT indexname FROM pg_indexes
                        WHERE schemaname = %s AND tablename = %s
                        AND indexdef LIKE '%%collection_id%%'
                        LIMIT 1
                    """, (schema, table))
                    idx_row = cur.fetchone()
                    if idx_row:
                        status.collection_id_indexed = True
                        status.collection_id_index_name = idx_row["indexname"]

                # 4. 获取行数和大小
                cur.execute(f"SELECT COUNT(*) AS cnt FROM {qualified_name}")
                status.row_count = cur.fetchone()["cnt"]

                cur.execute("""
                    SELECT pg_total_relation_size(%s::regclass) AS total_size
                """, (qualified_name,))
                size_row = cur.fetchone()
                if size_row:
                    status.total_size_bytes = size_row["total_size"]
                    status.total_size_human = self._format_bytes(size_row["total_size"])

        except Exception as e:
            status.error = str(e)
            logger.warning(f"检查 chunks 表状态失败: {e}")

        return status

    def inspect(self) -> InspectResult:
        """执行检查"""
        result = InspectResult(
            success=False,
            message="",
            schema_pattern=self.config.schema_pattern,
        )

        try:
            # 解析 DSN 获取连接信息
            dsn = self.config.get_dsn()
            # 简单解析 DSN 获取 host/port/db
            import urllib.parse
            parsed = urllib.parse.urlparse(dsn)
            result.host = parsed.hostname or self.config.host
            result.port = parsed.port or self.config.port
            result.database = parsed.path.lstrip("/") or self.config.database

            # 1. 获取匹配的 schemas
            logger.info(f"查找匹配 schema: {self.config.schema_pattern}")
            schemas = self._get_matching_schemas()
            result.schemas_found = schemas
            logger.info(f"发现 {len(schemas)} 个匹配的 schema: {schemas}")

            if not schemas:
                result.success = True
                result.message = "未找到匹配的 schema"
                return result

            # 2. 获取匹配的表
            logger.info(f"查找匹配表: {self.config.table_pattern}")
            tables = self._get_matching_tables(schemas)
            result.total_tables = len(tables)
            logger.info(f"发现 {len(tables)} 个匹配的表")

            # 3. 获取每个表的详细信息
            for schema, table in tables:
                if self.config.verbose:
                    logger.info(f"检查表: {schema}.{table}")
                info = self._get_table_info(schema, table)
                result.tables.append(info)
                if info.error:
                    result.errors.append(f"{info.qualified_name}: {info.error}")

            # 4. 检查 chunks 主表状态
            logger.info(f"检查 chunks 主表: {self.config.base_schema}.{self.config.base_table}")
            result.chunks_status = self._check_chunks_table()

            # 5. 汇总统计
            total_rows = sum(t.row_count for t in result.tables)
            total_size = sum(t.total_size_bytes for t in result.tables)
            total_indexes = sum(t.indexes_size_bytes for t in result.tables)
            tables_with_vector = sum(1 for t in result.tables if t.has_vector_column)
            tables_with_collection_id = sum(1 for t in result.tables if t.has_collection_id)
            tables_with_collection_id_indexed = sum(
                1 for t in result.tables if t.collection_id_indexed
            )

            # 统计不同维度的数量
            dims_count: Dict[int, int] = {}
            for t in result.tables:
                if t.vector_dim:
                    dims_count[t.vector_dim] = dims_count.get(t.vector_dim, 0) + 1

            result.summary = {
                "total_tables": len(result.tables),
                "total_rows": total_rows,
                "total_size_bytes": total_size,
                "total_size_human": self._format_bytes(total_size),
                "total_indexes_size_bytes": total_indexes,
                "total_indexes_size_human": self._format_bytes(total_indexes),
                "tables_with_vector": tables_with_vector,
                "tables_with_collection_id": tables_with_collection_id,
                "tables_with_collection_id_indexed": tables_with_collection_id_indexed,
                "vector_dimensions": dims_count,
            }

            result.success = True
            result.message = f"检查完成: 发现 {len(result.tables)} 个表, 共 {total_rows} 行"

        except Exception as e:
            result.success = False
            result.message = f"检查失败: {e}"
            result.errors.append(str(e))
            logger.error(f"检查失败: {e}")
            import traceback
            traceback.print_exc()

        finally:
            self.close()

        return result


# ============ CLI 入口 ============


def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="PGVector 信息架构检查脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # 使用环境变量
    export POSTGRES_PASSWORD=your_password
    python pgvector_inspect.py

    # 使用 DSN
    export PGVECTOR_DSN="postgresql://user:pass@host:5432/db"
    python pgvector_inspect.py

    # 指定 schema 模式
    python pgvector_inspect.py --schema-pattern "step3%"

    # 输出人类可读格式
    python pgvector_inspect.py --pretty
""",
    )

    parser.add_argument(
        "--schema-pattern",
        default=None,
        help="Schema LIKE 模式 (默认 step3%%)",
    )
    parser.add_argument(
        "--table-pattern",
        default=None,
        help="表名 LIKE 模式 (默认 step3_chunks_%%)",
    )
    parser.add_argument(
        "--base-schema",
        default=None,
        help="主 schema (默认从 STEP3_SCHEMA 环境变量或 step3)",
    )
    parser.add_argument(
        "--base-table",
        default="chunks",
        help="主表名 (默认 chunks)",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="输出人类可读的格式化 JSON",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="显示详细日志",
    )

    return parser.parse_args()


def main() -> int:
    """主入口"""
    args = parse_args()

    # 从环境变量加载配置
    config = InspectConfig.from_env()

    # 命令行参数覆盖
    if args.schema_pattern:
        config.schema_pattern = args.schema_pattern
    if args.table_pattern:
        config.table_pattern = args.table_pattern
    if args.base_schema:
        config.base_schema = args.base_schema
    if args.base_table:
        config.base_table = args.base_table
    config.pretty = args.pretty
    config.verbose = args.verbose

    # 检查密码
    if not config.dsn and not config.password:
        logger.error("错误: POSTGRES_PASSWORD 或 PGVECTOR_DSN 环境变量未设置")
        return 1

    # 执行检查
    inspector = PGVectorInspector(config)
    result = inspector.inspect()

    # 输出结果
    if config.pretty:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(json.dumps(result.to_dict(), ensure_ascii=False))

    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
