"""
engram_logbook.schema_context - Schema 上下文模块

提供 Schema 名称管理，支持测试环境隔离。

================================================================================
架构约束（路线A - 多库方案）:
--------------------------------------------------------------------------------
- **生产环境**: 使用固定 schema 名（identity, logbook, scm, analysis, governance）
- **测试环境**: 可使用 schema_prefix 进行隔离（需设置 ENGRAM_TESTING=1）
- schema_prefix 功能仅用于测试场景，生产环境不允许使用

生产环境 Schema 命名（固定）:
- identity      身份管理
- logbook       工作日志
- scm           源码管理
- analysis      分析结果
- governance    治理审计

测试环境 Schema 命名（带前缀，仅测试可用）:
- <prefix>_identity
- <prefix>_logbook
- <prefix>_scm
- <prefix>_analysis
- <prefix>_governance
================================================================================

示例:
    # 生产模式（无前缀）
    ctx = SchemaContext()
    ctx.logbook  # "logbook"
    ctx.search_path  # ["logbook", "scm", "identity", "analysis", "governance", "public"]

    # 测试模式隔离（仅测试环境，需 ENGRAM_TESTING=1）
    ctx = SchemaContext(schema_prefix="test_abc")
    ctx.logbook  # "test_abc_logbook"
    ctx.search_path  # ["test_abc_logbook", "test_abc_scm", ..., "public"]
"""

from dataclasses import dataclass
from typing import List, Optional

# 标准 Schema 后缀名（按 search_path 推荐顺序）
SCHEMA_SUFFIXES = ["logbook", "scm", "identity", "analysis", "governance"]


@dataclass
class SchemaContext:
    """
    Schema 上下文，用于多租户隔离

    Attributes:
        schema_prefix: Schema 前缀，为 None 或空字符串时使用默认 schema 名
        tenant: 租户标识（可选，用于日志/调试）
    """

    schema_prefix: Optional[str] = None
    tenant: Optional[str] = None

    def __post_init__(self):
        # 规范化空字符串为 None
        if self.schema_prefix == "":
            self.schema_prefix = None

    def _build_schema_name(self, suffix: str) -> str:
        """根据前缀构建 schema 名称"""
        if self.schema_prefix:
            return f"{self.schema_prefix}_{suffix}"
        return suffix

    @property
    def identity(self) -> str:
        """identity schema 名称"""
        return self._build_schema_name("identity")

    @property
    def logbook(self) -> str:
        """logbook schema 名称"""
        return self._build_schema_name("logbook")

    @property
    def scm(self) -> str:
        """scm schema 名称"""
        return self._build_schema_name("scm")

    @property
    def analysis(self) -> str:
        """analysis schema 名称"""
        return self._build_schema_name("analysis")

    @property
    def governance(self) -> str:
        """governance schema 名称"""
        return self._build_schema_name("governance")

    @property
    def all_schemas(self) -> dict:
        """
        返回所有 schema 名称字典

        Returns:
            {
                "identity": "<prefix>_identity",
                "logbook": "<prefix>_logbook",
                "scm": "<prefix>_scm",
                "analysis": "<prefix>_analysis",
                "governance": "<prefix>_governance",
            }
        """
        return {
            "identity": self.identity,
            "logbook": self.logbook,
            "scm": self.scm,
            "analysis": self.analysis,
            "governance": self.governance,
        }

    @property
    def search_path(self) -> List[str]:
        """
        返回 PostgreSQL search_path 列表

        顺序: logbook, scm, identity, analysis, governance, public
        public 作为兜底始终放在最后

        Returns:
            search_path 列表
        """
        schemas = [
            self.logbook,
            self.scm,
            self.identity,
            self.analysis,
            self.governance,
        ]
        # public 作为兜底
        schemas.append("public")
        return schemas

    @property
    def search_path_sql(self) -> str:
        """
        返回用于 SET search_path TO ... 的 SQL 值

        Returns:
            逗号分隔的 schema 列表字符串
        """
        return ", ".join(self.search_path)

    def table(self, schema_key: str, table_name: str) -> str:
        """
        生成完全限定的表名

        Args:
            schema_key: schema 键名 ("identity", "logbook", "scm", "analysis", "governance")
            table_name: 表名

        Returns:
            完全限定表名，如 "tenant_abc_logbook.items"

        Raises:
            ValueError: 无效的 schema_key
        """
        schema_name = self.all_schemas.get(schema_key)
        if schema_name is None:
            raise ValueError(
                f"无效的 schema_key: {schema_key}，有效值: {list(self.all_schemas.keys())}"
            )
        return f"{schema_name}.{table_name}"

    def __repr__(self) -> str:
        return f"SchemaContext(prefix={self.schema_prefix!r}, tenant={self.tenant!r})"


# ============ 全局 SchemaContext 管理 ============

_global_schema_context: Optional[SchemaContext] = None


def get_schema_context(
    schema_prefix: Optional[str] = None,
    tenant: Optional[str] = None,
    reload: bool = False,
) -> SchemaContext:
    """
    获取全局 SchemaContext 实例

    Args:
        schema_prefix: Schema 前缀
        tenant: 租户标识
        reload: 是否强制重新创建

    Returns:
        SchemaContext 实例
    """
    global _global_schema_context

    if _global_schema_context is None or reload:
        _global_schema_context = SchemaContext(
            schema_prefix=schema_prefix,
            tenant=tenant,
        )

    return _global_schema_context


def reset_schema_context() -> None:
    """重置全局 SchemaContext"""
    global _global_schema_context
    _global_schema_context = None


def set_schema_context(ctx: SchemaContext) -> None:
    """
    设置全局 SchemaContext

    Args:
        ctx: SchemaContext 实例
    """
    global _global_schema_context
    _global_schema_context = ctx


# ============ 便捷函数 ============


def build_search_path(
    schema_prefix: Optional[str] = None,
    include_public: bool = True,
) -> List[str]:
    """
    快速构建 search_path 列表

    Args:
        schema_prefix: Schema 前缀
        include_public: 是否包含 public schema

    Returns:
        search_path 列表
    """
    ctx = SchemaContext(schema_prefix=schema_prefix)
    path = ctx.search_path
    if not include_public and path and path[-1] == "public":
        path = path[:-1]
    return path


def build_schema_names(schema_prefix: Optional[str] = None) -> dict:
    """
    快速构建 schema 名称字典

    Args:
        schema_prefix: Schema 前缀

    Returns:
        schema 名称字典
    """
    return SchemaContext(schema_prefix=schema_prefix).all_schemas
