"""
engram_logbook.migrate - 数据库迁移模块

提供数据库迁移、验证、预检等功能函数，供 CLI 和其他模块调用。

主要函数:
- run_all_checks: 运行所有数据库结构自检项
- run_migrate: 执行数据库迁移
- run_precheck: 运行配置预检

使用方法:
    from engram.logbook.migrate import run_all_checks, run_migrate
    
    # 执行检查
    with get_connection(dsn=dsn) as conn:
        result = run_all_checks(conn)
    
    # 执行迁移
    result = run_migrate(dsn=dsn)
"""

import os
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urlunparse

from psycopg import sql

from .config import get_config, Config
from .db import get_connection, execute_sql_file
from .schema_context import SchemaContext, SCHEMA_SUFFIXES
from .errors import (
    DatabaseError,
    EngramError,
    make_success_result,
    make_error_result,
)
from .io import log_info, log_error, log_warning

# 延迟导入 backfill 模块，避免循环依赖
_backfill_evidence_uri_module = None
_backfill_chunking_version_module = None


def _get_backfill_evidence_uri():
    """延迟加载 backfill_evidence_uri 模块"""
    global _backfill_evidence_uri_module
    if _backfill_evidence_uri_module is None:
        from .backfill_evidence_uri import backfill_evidence_uri
        _backfill_evidence_uri_module = backfill_evidence_uri
    return _backfill_evidence_uri_module


def _get_backfill_chunking_version():
    """延迟加载 backfill_chunking_version 模块"""
    global _backfill_chunking_version_module
    if _backfill_chunking_version_module is None:
        from .backfill_chunking_version import backfill_chunking_version
        _backfill_chunking_version_module = backfill_chunking_version
    return _backfill_chunking_version_module


# ============================================================================
# 常量定义
# ============================================================================

# 默认 schema 后缀列表（无前缀时的 schema 名）
DEFAULT_SCHEMA_SUFFIXES = ["identity", "logbook", "scm", "analysis", "governance"]

# SQL 文件前缀分类
# 默认执行：结构性 DDL 脚本
# 01: 基础 schema 定义
# 02: scm 迁移
# 03: patch_blobs/pgvector 扩展
# 06: scm_sync_runs
# 07: scm_sync_locks/security_events
# 08: database_hardening/scm_sync_jobs
# 09: sync_jobs_dimension_columns
# 10: evidence_uri_column
# 11: sync_jobs_dimension_columns
# 12: governance_artifact_ops_audit
# 13: governance_object_store_audit_events
DDL_SCRIPT_PREFIXES = {"01", "02", "03", "06", "07", "08", "09", "10", "11", "12", "13"}
# 可选执行：权限脚本（需要 admin/superuser）
PERMISSION_SCRIPT_PREFIXES = {"04", "05"}
# 验证脚本：仅通过 --verify 执行
VERIFY_SCRIPT_PREFIXES = {"99"}

# 数据库名称白名单正则：仅允许小写字母、数字、下划线，且以字母开头
DB_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


# ============================================================================
# 需要验证的数据库对象模板
# ============================================================================

# 需要验证的核心表模板（格式：schema_suffix, table_name）
REQUIRED_TABLE_TEMPLATES = [
    # identity schema
    ("identity", "users"),
    ("identity", "accounts"),
    ("identity", "role_profiles"),
    # logbook schema
    ("logbook", "items"),
    ("logbook", "events"),
    ("logbook", "attachments"),
    ("logbook", "kv"),
    ("logbook", "outbox_memory"),
    # scm schema
    ("scm", "repos"),
    ("scm", "svn_revisions"),
    ("scm", "git_commits"),
    ("scm", "patch_blobs"),
    ("scm", "mrs"),
    ("scm", "review_events"),
    ("scm", "sync_rate_limits"),
    ("scm", "sync_runs"),
    ("scm", "sync_jobs"),
    ("scm", "sync_locks"),
    # analysis schema
    ("analysis", "runs"),
    ("analysis", "knowledge_candidates"),
    # governance schema
    ("governance", "settings"),
    ("governance", "write_audit"),
    ("governance", "promotion_queue"),
    ("governance", "security_events"),
    ("governance", "artifact_ops_audit"),
    ("governance", "object_store_audit_events"),
]

# 需要验证的关键列模板（格式：schema_suffix.table.column）
REQUIRED_COLUMN_TEMPLATES = [
    ("scm", "review_events", "source_event_id"),
    ("scm", "patch_blobs", "meta_json"),
    ("scm", "patch_blobs", "updated_at"),
    ("governance", "write_audit", "created_at"),
]

# 需要验证的关键索引模板（格式：schema_suffix, index_name）
REQUIRED_INDEX_TEMPLATES = [
    ("scm", "idx_v_facts_source_id"),
    ("scm", "idx_v_facts_repo_id"),
    ("scm", "idx_v_facts_repo_ts"),
    ("logbook", "idx_logbook_events_item_time"),
    ("logbook", "idx_outbox_memory_pending"),
    # governance - security_events 索引
    ("governance", "idx_security_events_ts"),
    ("governance", "idx_security_events_action"),
    ("governance", "idx_security_events_object_type"),
    # governance - artifact_ops_audit 索引
    ("governance", "idx_artifact_ops_audit_ts"),
    ("governance", "idx_artifact_ops_audit_uri"),
    ("governance", "idx_artifact_ops_audit_bucket"),
    # governance - object_store_audit_events 索引
    ("governance", "idx_object_store_audit_bucket_key_ts"),
    ("governance", "idx_object_store_audit_request_id"),
]

# 需要验证的关键触发器模板（格式：schema_suffix, table_name, trigger_name）
REQUIRED_TRIGGER_TEMPLATES = [
    ("scm", "patch_blobs", "trg_patch_blobs_updated_at"),
]

# 需要验证的物化视图模板（格式：schema_suffix, view_name）
REQUIRED_MATVIEW_TEMPLATES = [
    ("scm", "v_facts"),
]


# ============================================================================
# 辅助函数 - 测试模式检测
# ============================================================================

def is_testing_mode() -> bool:
    """
    检查是否处于测试模式。
    
    测试模式通过环境变量 ENGRAM_TESTING=1 启用。
    仅在测试模式下允许使用 schema_prefix 参数。
    
    Returns:
        True 表示测试模式，False 表示生产模式
    """
    return os.environ.get("ENGRAM_TESTING", "").strip() == "1"


# ============================================================================
# 修复命令提示
# ============================================================================

def get_repair_commands_hint(error_code: str = None, target_db: str = None) -> dict:
    """
    根据错误代码生成修复命令提示。
    
    Args:
        error_code: 错误代码
        target_db: 目标数据库名称
    
    Returns:
        包含修复命令的字典
    """
    db_suffix = f" (数据库: {target_db})" if target_db else ""
    
    base_commands = {
        "bootstrap": "python logbook_postgres/scripts/db_bootstrap.py",
        "migrate": "python logbook_postgres/scripts/db_migrate.py",
        "migrate_with_roles": "python logbook_postgres/scripts/db_migrate.py --apply-roles --apply-openmemory-grants",
        "verify": "python logbook_postgres/scripts/db_migrate.py --verify",
        "docker_bootstrap": "docker compose -f docker-compose.unified.yml up bootstrap_roles",
        "docker_migrate": "docker compose -f docker-compose.unified.yml up logbook_migrate openmemory_migrate",
    }
    
    # 根据错误代码推荐不同的修复方案
    if error_code in ("SCHEMA_MISSING", "TABLE_MISSING", "COLUMN_MISSING", "INDEX_MISSING", "TRIGGER_MISSING", "MATVIEW_MISSING"):
        return {
            "repair_hint": f"数据库结构缺失{db_suffix}",
            "recommended_commands": [
                "# 方案 1: 完整初始化（推荐）",
                base_commands["bootstrap"],
                base_commands["migrate_with_roles"],
                "",
                "# 方案 2: Docker 环境",
                base_commands["docker_bootstrap"],
                base_commands["docker_migrate"],
                "",
                "# 验证修复结果",
                base_commands["verify"],
            ],
        }
    elif error_code == "OPENMEMORY_SCHEMA_MISSING":
        return {
            "repair_hint": f"OpenMemory schema 未创建{db_suffix}",
            "recommended_commands": [
                "# 执行 OpenMemory 权限脚本",
                "python logbook_postgres/scripts/db_migrate.py --apply-openmemory-grants",
                "",
                "# 或完整初始化",
                base_commands["bootstrap"],
                "",
                "# Docker 环境",
                "docker compose -f docker-compose.unified.yml up bootstrap_roles openmemory_migrate",
            ],
        }
    elif error_code == "PRECHECK_FAILED":
        return {
            "repair_hint": "预检失败，请检查环境变量配置",
            "recommended_commands": [
                "# 检查 OM_PG_SCHEMA 配置",
                "export OM_PG_SCHEMA=openmemory  # 不能是 public",
                "",
                "# 重新运行预检",
                "python logbook_postgres/scripts/db_migrate.py --precheck-only",
            ],
        }
    elif error_code == "INSUFFICIENT_PRIVILEGE":
        return {
            "repair_hint": "权限不足，需要 superuser 或 CREATEROLE 权限",
            "recommended_commands": [
                "# 使用 bootstrap 脚本（需要 admin DSN）",
                "export ENGRAM_PG_ADMIN_DSN='postgresql://postgres:password@localhost:5432/postgres'",
                base_commands["bootstrap"],
                "",
                "# Docker 环境（自动使用 postgres 超级用户）",
                base_commands["docker_bootstrap"],
            ],
        }
    else:
        return {
            "repair_hint": f"数据库问题{db_suffix}",
            "recommended_commands": [
                "# 完整初始化",
                base_commands["bootstrap"],
                base_commands["migrate_with_roles"],
                "",
                "# Docker 环境",
                base_commands["docker_bootstrap"],
                base_commands["docker_migrate"],
            ],
        }


# ============================================================================
# 预检相关函数
# ============================================================================

def precheck_openmemory_schema() -> tuple[bool, str]:
    """
    预检 OpenMemory schema 配置是否安全。
    
    当 OM_METADATA_BACKEND=postgres 时，强制要求 OM_PG_SCHEMA 不能是 public。
    
    Returns:
        (ok, message) - ok 为 True 表示检查通过
    """
    backend = os.environ.get("OM_METADATA_BACKEND", "")
    schema = os.environ.get("OM_PG_SCHEMA", "public")
    
    # 仅当使用 postgres 后端时检查
    if backend != "postgres":
        return True, ""
    
    if schema == "public":
        message = """[FATAL] OM_PG_SCHEMA=public 是禁止的配置！

原因：
  1. public schema 是 PostgreSQL 默认 schema，可能包含其他应用的表
  2. 无法使用 pg_dump --schema 进行隔离备份
  3. DROP SCHEMA public CASCADE 会破坏整个数据库

解决方案：
  设置环境变量 OM_PG_SCHEMA 为非 public 值，例如：
  - OM_PG_SCHEMA=openmemory（统一栈默认值）
  - OM_PG_SCHEMA=${PROJECT_KEY}_openmemory（多租户隔离）

参考 docker-compose.unified.yml 中的统一默认配置：
  OM_PG_SCHEMA: ${OM_PG_SCHEMA:-openmemory}
"""
        return False, message
    
    return True, ""


def run_precheck(quiet: bool = False) -> dict:
    """
    运行所有预检项。
    
    Args:
        quiet: 静默模式
    
    Returns:
        {ok: bool, checks: {...}, message: str}
    """
    checks = {}
    all_ok = True
    messages = []
    
    # 检查 OpenMemory schema 配置
    om_ok, om_msg = precheck_openmemory_schema()
    checks["openmemory_schema"] = {"ok": om_ok, "message": om_msg}
    if not om_ok:
        all_ok = False
        messages.append(om_msg)
        if not quiet:
            log_error(om_msg)
    
    return {
        "ok": all_ok,
        "checks": checks,
        "message": "\n".join(messages) if messages else "",
    }


# ============================================================================
# SQL 文件扫描与分类
# ============================================================================

def scan_sql_files(sql_dir: Path) -> list[tuple[str, Path]]:
    """
    扫描 SQL 目录，返回按前缀数字排序的 SQL 文件列表。
    
    Args:
        sql_dir: SQL 文件目录
    
    Returns:
        [(prefix, path), ...] 按前缀排序的文件列表
    """
    pattern = re.compile(r"^(\d{2})_.*\.sql$")
    result = []
    
    for sql_file in sql_dir.glob("*.sql"):
        match = pattern.match(sql_file.name)
        if match:
            prefix = match.group(1)
            result.append((prefix, sql_file))
    
    # 按前缀数字排序
    result.sort(key=lambda x: int(x[0]))
    return result


def classify_sql_files(
    sql_files: list[tuple[str, Path]],
    apply_roles: bool = False,
    apply_openmemory_grants: bool = False,
    verify: bool = False,
) -> dict[str, list[Path]]:
    """
    对 SQL 文件进行分类。
    
    Args:
        sql_files: 扫描到的 SQL 文件列表
        apply_roles: 是否包含角色权限脚本（04）
        apply_openmemory_grants: 是否包含 OpenMemory 权限脚本（05）
        verify: 是否包含验证脚本（99）
    
    Returns:
        {
            "ddl": [Path, ...],           # 默认执行的 DDL 脚本
            "permissions": [Path, ...],   # 权限脚本
            "verify": [Path, ...],        # 验证脚本
            "execute": [Path, ...],       # 本次需要执行的所有脚本（按顺序）
        }
    """
    ddl_files = []
    permission_files = []
    verify_files = []
    execute_files = []
    
    for prefix, path in sql_files:
        is_openmemory_script = "openmemory" in path.name.lower()
        
        if prefix in DDL_SCRIPT_PREFIXES:
            ddl_files.append(path)
            execute_files.append(path)
        elif prefix in PERMISSION_SCRIPT_PREFIXES:
            # 05 前缀需要区分：只有包含 "openmemory" 的才是 OpenMemory 脚本
            if prefix == "05" and not is_openmemory_script:
                # 非 OpenMemory 的 05 文件当作 DDL 处理
                ddl_files.append(path)
                execute_files.append(path)
            else:
                permission_files.append(path)
                # 根据开关决定是否执行
                if prefix == "04" and apply_roles:
                    execute_files.append(path)
                elif prefix == "05" and is_openmemory_script and apply_openmemory_grants:
                    execute_files.append(path)
        elif prefix in VERIFY_SCRIPT_PREFIXES:
            verify_files.append(path)
            # 仅当 verify=True 时执行
            if verify:
                execute_files.append(path)
    
    return {
        "ddl": ddl_files,
        "permissions": permission_files,
        "verify": verify_files,
        "execute": execute_files,
    }


def check_has_superuser_privilege(conn) -> bool:
    """
    检查当前连接用户是否具有 superuser 或 CREATEROLE 权限。
    
    Args:
        conn: 数据库连接
    
    Returns:
        True 表示有足够权限
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 
                rolsuper OR rolcreaterole AS can_create_role
            FROM pg_roles 
            WHERE rolname = current_user
        """)
        result = cur.fetchone()
        return result[0] if result else False


# ============================================================================
# 数据库名称校验与自动创建相关
# ============================================================================

def validate_db_name(db_name: str) -> tuple[bool, str]:
    """
    校验数据库名称是否符合安全命名规范。
    
    Args:
        db_name: 待校验的数据库名称
    
    Returns:
        (valid, error_message) - valid 为 True 表示合法
    """
    if not db_name:
        return False, "数据库名称不能为空"
    
    if len(db_name) > 63:
        return False, f"数据库名称过长（最大 63 字符）：{len(db_name)} 字符"
    
    if not DB_NAME_PATTERN.match(db_name):
        return False, (
            f"数据库名称 '{db_name}' 不符合命名规范："
            "仅允许小写字母、数字、下划线，且必须以小写字母开头"
        )
    
    return True, ""


def parse_db_name_from_dsn(dsn: str) -> str | None:
    """
    从 DSN 中解析数据库名称。
    
    Args:
        dsn: PostgreSQL 连接字符串
    
    Returns:
        数据库名称，解析失败返回 None
    """
    try:
        parsed = urlparse(dsn)
        if parsed.path and len(parsed.path) > 1:
            return parsed.path.lstrip("/")
        return None
    except Exception:
        return None


def replace_db_in_dsn(dsn: str, new_db_name: str) -> str:
    """
    替换 DSN 中的数据库名称。
    
    Args:
        dsn: 原 DSN
        new_db_name: 新数据库名称
    
    Returns:
        替换后的 DSN
    """
    try:
        parsed = urlparse(dsn)
        new_parsed = parsed._replace(path=f"/{new_db_name}")
        return urlunparse(new_parsed)
    except Exception:
        if "/" in dsn:
            base = dsn.rsplit("/", 1)[0]
            return f"{base}/{new_db_name}"
        return dsn


def check_database_exists(admin_dsn: str, db_name: str) -> bool:
    """
    检测数据库是否存在。
    
    Args:
        admin_dsn: 管理员 DSN
        db_name: 要检测的数据库名称
    
    Returns:
        True 表示数据库存在
    """
    import psycopg
    
    conn = psycopg.connect(admin_dsn, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s",
                (db_name,)
            )
            return cur.fetchone() is not None
    finally:
        conn.close()


def create_database(admin_dsn: str, db_name: str, quiet: bool = False) -> None:
    """
    创建数据库。
    
    Args:
        admin_dsn: 管理员 DSN
        db_name: 数据库名称（已校验合法）
        quiet: 静默模式
    
    Raises:
        DatabaseError: 创建失败时
    """
    import psycopg
    
    log_info(f"创建数据库: {db_name}...", quiet=quiet)
    
    conn = psycopg.connect(admin_dsn, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        conn.close()


def ensure_database_exists(
    target_dsn: str,
    admin_dsn: str | None,
    project_key: str | None = None,
    quiet: bool = False,
) -> dict:
    """
    确保目标数据库存在，不存在则自动创建。
    
    Args:
        target_dsn: 目标数据库 DSN
        admin_dsn: 管理员 DSN
        project_key: 项目标识（可选，用作数据库名）
        quiet: 静默模式
    
    Returns:
        {ok, db_name, created, message}
    """
    db_name = parse_db_name_from_dsn(target_dsn)
    
    if not db_name and project_key:
        db_name = project_key
    
    if not db_name:
        return {
            "ok": False,
            "db_name": None,
            "created": False,
            "message": "无法确定目标数据库名称：DSN 中未指定数据库名，且未提供 project_key",
        }
    
    valid, error_msg = validate_db_name(db_name)
    if not valid:
        return {
            "ok": False,
            "db_name": db_name,
            "created": False,
            "message": error_msg,
        }
    
    if not admin_dsn:
        log_info(f"未配置 admin_dsn，跳过数据库存在性检查", quiet=quiet)
        return {
            "ok": True,
            "db_name": db_name,
            "created": False,
            "message": "",
        }
    
    try:
        exists = check_database_exists(admin_dsn, db_name)
    except Exception as e:
        return {
            "ok": False,
            "db_name": db_name,
            "created": False,
            "message": f"检测数据库存在性失败: {e}",
        }
    
    if exists:
        log_info(f"数据库 {db_name} 已存在", quiet=quiet)
        return {
            "ok": True,
            "db_name": db_name,
            "created": False,
            "message": "",
        }
    
    try:
        create_database(admin_dsn, db_name, quiet=quiet)
        log_info(f"数据库 {db_name} 创建成功", quiet=quiet)
        return {
            "ok": True,
            "db_name": db_name,
            "created": True,
            "message": "",
        }
    except Exception as e:
        return {
            "ok": False,
            "db_name": db_name,
            "created": False,
            "message": f"创建数据库失败: {e}",
        }


# ============================================================================
# Schema 相关辅助函数
# ============================================================================

def get_required_schemas(schema_context: SchemaContext) -> list[str]:
    """获取需要验证的 schema 列表。"""
    return list(schema_context.all_schemas.values())


def get_required_tables(schema_context: SchemaContext) -> list[tuple[str, str]]:
    """获取需要验证的核心表列表。"""
    schema_map = schema_context.all_schemas
    result = []
    for suffix, table in REQUIRED_TABLE_TEMPLATES:
        actual_schema = schema_map.get(suffix, suffix)
        result.append((actual_schema, table))
    return result


def get_required_columns(schema_context: SchemaContext) -> list[tuple[str, str, str]]:
    """获取需要验证的关键列列表。"""
    schema_map = schema_context.all_schemas
    result = []
    for suffix, table, column in REQUIRED_COLUMN_TEMPLATES:
        actual_schema = schema_map.get(suffix, suffix)
        result.append((actual_schema, table, column))
    return result


def get_required_indexes(schema_context: SchemaContext) -> list[tuple[str, str]]:
    """获取需要验证的关键索引列表。"""
    schema_map = schema_context.all_schemas
    result = []
    for suffix, index_name in REQUIRED_INDEX_TEMPLATES:
        actual_schema = schema_map.get(suffix, suffix)
        result.append((actual_schema, index_name))
    return result


def get_required_triggers(schema_context: SchemaContext) -> list[tuple[str, str, str]]:
    """获取需要验证的关键触发器列表。"""
    schema_map = schema_context.all_schemas
    result = []
    for suffix, table, trigger in REQUIRED_TRIGGER_TEMPLATES:
        actual_schema = schema_map.get(suffix, suffix)
        result.append((actual_schema, table, trigger))
    return result


def get_required_matviews(schema_context: SchemaContext) -> list[tuple[str, str]]:
    """获取需要验证的物化视图列表。"""
    schema_map = schema_context.all_schemas
    result = []
    for suffix, view_name in REQUIRED_MATVIEW_TEMPLATES:
        actual_schema = schema_map.get(suffix, suffix)
        result.append((actual_schema, view_name))
    return result


def get_openmemory_schema() -> str:
    """获取 OpenMemory 目标 schema 名称。"""
    return os.environ.get("OM_PG_SCHEMA", "openmemory")


def should_auto_apply_openmemory() -> bool:
    """检查是否应自动启用 OpenMemory schema 权限脚本。"""
    backend = os.environ.get("OM_METADATA_BACKEND", "")
    return backend == "postgres"


# ============================================================================
# 数据库结构检查函数
# ============================================================================

def check_openmemory_schema_exists(conn, schema_name: str = None) -> tuple[bool, str]:
    """检查 openmemory schema 是否存在。"""
    if schema_name is None:
        schema_name = get_openmemory_schema()
    
    with conn.cursor() as cur:
        cur.execute("""
            SELECT schema_name
            FROM information_schema.schemata
            WHERE schema_name = %s
        """, (schema_name,))
        result = cur.fetchone()
    
    if result is None:
        return False, f"openmemory schema '{schema_name}' 不存在"
    return True, ""


def check_tables_exist(
    conn,
    tables: list[tuple[str, str]] = None,
    *,
    schema_map: dict[str, str] = None,
    schema_prefix: str = None,
) -> tuple[bool, list[str]]:
    """检查指定的表是否存在。"""
    if tables is None:
        if schema_map is not None:
            tables = []
            for suffix, table in REQUIRED_TABLE_TEMPLATES:
                actual_schema = schema_map.get(suffix, suffix)
                tables.append((actual_schema, table))
        elif schema_prefix is not None:
            tables = []
            for suffix, table in REQUIRED_TABLE_TEMPLATES:
                tables.append((f"{schema_prefix}_{suffix}", table))
        else:
            tables = list(REQUIRED_TABLE_TEMPLATES)

    missing = []
    with conn.cursor() as cur:
        for schema, table in tables:
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = %s AND table_name = %s
                )
            """, (schema, table))
            if not cur.fetchone()[0]:
                missing.append(f"{schema}.{table}")

    return len(missing) == 0, missing


def check_schemas_exist(
    conn,
    schemas: list[str] = None,
    *,
    schema_map: dict[str, str] = None,
    schema_prefix: str = None,
) -> tuple[bool, list[str]]:
    """检查指定的 schema 是否存在。"""
    if schemas is None:
        if schema_map is not None:
            schemas = list(schema_map.values())
        elif schema_prefix is not None:
            schemas = [f"{schema_prefix}_{s}" for s in DEFAULT_SCHEMA_SUFFIXES]
        else:
            schemas = DEFAULT_SCHEMA_SUFFIXES

    with conn.cursor() as cur:
        cur.execute("""
            SELECT schema_name
            FROM information_schema.schemata
            WHERE schema_name = ANY(%s)
        """, (schemas,))
        existing = {row[0] for row in cur.fetchall()}

    missing = [s for s in schemas if s not in existing]
    return len(missing) == 0, missing


def check_columns_exist(
    conn,
    columns: list[tuple[str, str, str]] = None,
    *,
    schema_map: dict[str, str] = None,
    schema_prefix: str = None,
) -> tuple[bool, list[str]]:
    """检查指定的列是否存在。"""
    if columns is None:
        if schema_map is not None:
            columns = []
            for suffix, table, column in REQUIRED_COLUMN_TEMPLATES:
                actual_schema = schema_map.get(suffix, suffix)
                columns.append((actual_schema, table, column))
        elif schema_prefix is not None:
            columns = []
            for suffix, table, column in REQUIRED_COLUMN_TEMPLATES:
                columns.append((f"{schema_prefix}_{suffix}", table, column))
        else:
            columns = list(REQUIRED_COLUMN_TEMPLATES)

    missing = []
    with conn.cursor() as cur:
        for schema, table, column in columns:
            cur.execute("""
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = %s
                  AND table_name = %s
                  AND column_name = %s
            """, (schema, table, column))
            if cur.fetchone() is None:
                missing.append(f"{schema}.{table}.{column}")

    return len(missing) == 0, missing


def check_indexes_exist(
    conn,
    indexes: list[tuple[str, str]] = None,
    *,
    schema_map: dict[str, str] = None,
    schema_prefix: str = None,
) -> tuple[bool, list[str]]:
    """检查指定的索引是否存在。"""
    if indexes is None:
        if schema_map is not None:
            indexes = []
            for suffix, index_name in REQUIRED_INDEX_TEMPLATES:
                actual_schema = schema_map.get(suffix, suffix)
                indexes.append((actual_schema, index_name))
        elif schema_prefix is not None:
            indexes = []
            for suffix, index_name in REQUIRED_INDEX_TEMPLATES:
                indexes.append((f"{schema_prefix}_{suffix}", index_name))
        else:
            indexes = list(REQUIRED_INDEX_TEMPLATES)

    missing = []
    with conn.cursor() as cur:
        for schema, index_name in indexes:
            cur.execute("""
                SELECT 1
                FROM pg_indexes
                WHERE schemaname = %s
                  AND indexname = %s
            """, (schema, index_name))
            if cur.fetchone() is None:
                missing.append(f"{schema}.{index_name}")

    return len(missing) == 0, missing


def check_triggers_exist(
    conn,
    triggers: list[tuple[str, str, str]] = None,
    *,
    schema_map: dict[str, str] = None,
    schema_prefix: str = None,
) -> tuple[bool, list[str]]:
    """检查指定的触发器是否存在。"""
    if triggers is None:
        if schema_map is not None:
            triggers = []
            for suffix, table, trigger in REQUIRED_TRIGGER_TEMPLATES:
                actual_schema = schema_map.get(suffix, suffix)
                triggers.append((actual_schema, table, trigger))
        elif schema_prefix is not None:
            triggers = []
            for suffix, table, trigger in REQUIRED_TRIGGER_TEMPLATES:
                triggers.append((f"{schema_prefix}_{suffix}", table, trigger))
        else:
            triggers = list(REQUIRED_TRIGGER_TEMPLATES)

    missing = []
    with conn.cursor() as cur:
        for schema, table, trigger_name in triggers:
            cur.execute("""
                SELECT 1
                FROM pg_trigger t
                JOIN pg_class c ON t.tgrelid = c.oid
                JOIN pg_namespace n ON c.relnamespace = n.oid
                WHERE n.nspname = %s
                  AND c.relname = %s
                  AND t.tgname = %s
            """, (schema, table, trigger_name))
            if cur.fetchone() is None:
                missing.append(f"{schema}.{table}.{trigger_name}")

    return len(missing) == 0, missing


def check_matviews_exist(
    conn,
    matviews: list[tuple[str, str]] = None,
    *,
    schema_map: dict[str, str] = None,
    schema_prefix: str = None,
) -> tuple[bool, list[str]]:
    """检查指定的物化视图是否存在。"""
    if matviews is None:
        if schema_map is not None:
            matviews = []
            for suffix, view_name in REQUIRED_MATVIEW_TEMPLATES:
                actual_schema = schema_map.get(suffix, suffix)
                matviews.append((actual_schema, view_name))
        elif schema_prefix is not None:
            matviews = []
            for suffix, view_name in REQUIRED_MATVIEW_TEMPLATES:
                matviews.append((f"{schema_prefix}_{suffix}", view_name))
        else:
            matviews = list(REQUIRED_MATVIEW_TEMPLATES)

    missing = []
    with conn.cursor() as cur:
        for schema, view_name in matviews:
            cur.execute("""
                SELECT 1
                FROM pg_matviews
                WHERE schemaname = %s
                  AND matviewname = %s
            """, (schema, view_name))
            if cur.fetchone() is None:
                missing.append(f"{schema}.{view_name}")

    return len(missing) == 0, missing


def check_search_path(
    conn,
    expected_schemas: list[str],
    *,
    require_order: bool = False,
    require_public_last: bool = True,
) -> tuple[bool, str]:
    """检查当前连接的 search_path 是否包含预期的 schema。"""
    with conn.cursor() as cur:
        cur.execute("SHOW search_path")
        actual_path = cur.fetchone()[0]

    actual_schemas = [s.strip().strip('"') for s in actual_path.split(",")]

    if require_public_last:
        if "public" in actual_schemas and actual_schemas[-1] != "public":
            return False, f"public 应在 search_path 最后，实际: {actual_path}"

    missing = [s for s in expected_schemas if s not in actual_schemas]
    if missing:
        return False, f"search_path 缺少 schema: {missing}，实际: {actual_path}"

    if require_order:
        actual_indices = []
        for s in expected_schemas:
            if s in actual_schemas:
                actual_indices.append(actual_schemas.index(s))
        
        for i in range(1, len(actual_indices)):
            if actual_indices[i] <= actual_indices[i - 1]:
                return False, f"search_path 顺序不正确，期望: {expected_schemas}，实际: {actual_path}"

    return True, ""


# ============================================================================
# 核心函数: run_all_checks
# ============================================================================

def run_all_checks(
    conn,
    schema_context: SchemaContext = None,
    *,
    schema_map: dict[str, str] = None,
    schema_prefix: str = None,
    check_search_path_schemas: list[str] = None,
    check_openmemory_schema: bool = False,
    openmemory_schema_name: str = None,
) -> dict:
    """
    运行所有自检项，返回统一结果。

    该函数用于测试复用，可直接调用进行完整验证。

    Args:
        conn: 数据库连接
        schema_context: SchemaContext 实例（优先使用）
        schema_map: schema 后缀到实际名称的映射
        schema_prefix: schema 前缀
        check_search_path_schemas: 若提供，检查 search_path 是否包含这些 schema
        check_openmemory_schema: 若为 True，检查 openmemory schema 是否存在
        openmemory_schema_name: OpenMemory 目标 schema 名称

    Returns:
        {ok: bool, checks: {...}}
    """
    # 确定实际的 schema_map
    if schema_context is not None:
        actual_schema_map = schema_context.all_schemas
    elif schema_map is not None:
        actual_schema_map = schema_map
    elif schema_prefix is not None:
        actual_schema_map = {s: f"{schema_prefix}_{s}" for s in DEFAULT_SCHEMA_SUFFIXES}
    else:
        actual_schema_map = {s: s for s in DEFAULT_SCHEMA_SUFFIXES}

    checks = {}
    all_ok = True

    # 检查 schemas
    schemas_ok, missing_schemas = check_schemas_exist(conn, schema_map=actual_schema_map)
    checks["schemas"] = {"ok": schemas_ok, "missing": missing_schemas}
    all_ok = all_ok and schemas_ok

    # 检查 tables
    tables_ok, missing_tables = check_tables_exist(conn, schema_map=actual_schema_map)
    checks["tables"] = {"ok": tables_ok, "missing": missing_tables}
    all_ok = all_ok and tables_ok

    # 检查 columns
    cols_ok, missing_cols = check_columns_exist(conn, schema_map=actual_schema_map)
    checks["columns"] = {"ok": cols_ok, "missing": missing_cols}
    all_ok = all_ok and cols_ok

    # 检查 indexes
    indexes_ok, missing_indexes = check_indexes_exist(conn, schema_map=actual_schema_map)
    checks["indexes"] = {"ok": indexes_ok, "missing": missing_indexes}
    all_ok = all_ok and indexes_ok

    # 检查 triggers
    triggers_ok, missing_triggers = check_triggers_exist(conn, schema_map=actual_schema_map)
    checks["triggers"] = {"ok": triggers_ok, "missing": missing_triggers}
    all_ok = all_ok and triggers_ok

    # 检查 matviews
    matviews_ok, missing_matviews = check_matviews_exist(conn, schema_map=actual_schema_map)
    checks["matviews"] = {"ok": matviews_ok, "missing": missing_matviews}
    all_ok = all_ok and matviews_ok

    # 检查 search_path（可选）
    if check_search_path_schemas is not None:
        sp_ok, sp_message = check_search_path(conn, check_search_path_schemas)
        checks["search_path"] = {"ok": sp_ok, "message": sp_message}
        all_ok = all_ok and sp_ok

    # 检查 openmemory schema（可选）
    if check_openmemory_schema:
        om_ok, om_message = check_openmemory_schema_exists(conn, openmemory_schema_name)
        checks["openmemory_schema"] = {"ok": om_ok, "message": om_message, "schema": openmemory_schema_name}
        all_ok = all_ok and om_ok

    return {"ok": all_ok, "checks": checks}


# ============================================================================
# 迁移锁相关函数
# ============================================================================

def _build_lock_key(schema_prefix: str = None) -> str:
    """构建迁移锁的唯一标识符。"""
    namespace = "engram_migrate"
    if schema_prefix:
        return f"{namespace}:{schema_prefix}"
    return f"{namespace}:default"


def _acquire_advisory_lock(conn, lock_key: str, quiet: bool = False) -> None:
    """获取 PostgreSQL 咨询锁（阻塞式）。"""
    log_info(f"获取迁移锁: {lock_key}...", quiet=quiet)
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(hashtext(%s))", (lock_key,))


def _release_advisory_lock(conn, lock_key: str, quiet: bool = False) -> None:
    """释放 PostgreSQL 咨询锁。"""
    log_info(f"释放迁移锁: {lock_key}...", quiet=quiet)
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (lock_key,))


# ============================================================================
# 核心函数: run_migrate
# ============================================================================

def run_migrate(
    config_path: str = None,
    quiet: bool = False,
    dsn: str = None,
    schema_prefix: str = None,
    apply_roles: bool = None,
    apply_openmemory_grants: bool = None,
    precheck_only: bool = False,
    verify: bool = False,
    post_backfill: bool = False,
    backfill_chunking_version: str = None,
    backfill_batch_size: int = 1000,
    backfill_dry_run: bool = False,
) -> dict:
    """
    执行数据库迁移。

    使用 PostgreSQL 咨询锁确保同一 schema_prefix 的迁移不会并发执行。
    
    Args:
        config_path: 配置文件路径（可选）
        quiet: 静默模式
        dsn: 直接指定数据库 DSN（可选，优先于 config_path）
        schema_prefix: Schema 前缀，仅限测试模式使用（可选）
        apply_roles: 是否执行角色权限脚本 04_roles_and_grants.sql
        apply_openmemory_grants: 是否执行 OpenMemory schema 权限脚本
        precheck_only: 仅执行预检
        verify: 是否执行权限验证脚本 99_verify_permissions.sql
        post_backfill: 是否在迁移后执行 backfill（evidence_uri 回填）
        backfill_chunking_version: 若指定，同时执行 chunking_version 回填
        backfill_batch_size: backfill 每批处理记录数（默认 1000）
        backfill_dry_run: backfill 是否为 dry-run 模式

    Returns:
        {ok: True, ...} 或 {ok: False, code, message, detail}
    """
    try:
        # ========================================
        # 预检：检查配置安全性
        # ========================================
        log_info("执行预检...", quiet=quiet)
        precheck_result = run_precheck(quiet=quiet)
        
        if not precheck_result["ok"]:
            repair_hint = get_repair_commands_hint("PRECHECK_FAILED")
            return make_error_result(
                code="PRECHECK_FAILED",
                message="预检失败，无法继续迁移",
                detail={
                    "checks": precheck_result["checks"],
                    "hint": "请检查环境变量配置是否正确",
                    **repair_hint,
                },
            )
        
        log_info("预检通过", quiet=quiet)
        
        # 如果仅执行预检，直接返回成功
        if precheck_only:
            return make_success_result(
                precheck_only=True,
                checks=precheck_result["checks"],
            )
        
        # [路线A 约束] 生产模式下禁止使用 schema_prefix
        if schema_prefix and not is_testing_mode():
            return make_error_result(
                code="SCHEMA_PREFIX_NOT_ALLOWED",
                message="生产模式下不允许使用 schema_prefix（路线A 多库方案约束）",
                detail={
                    "schema_prefix": schema_prefix,
                    "hint": "设置环境变量 ENGRAM_TESTING=1 启用测试模式以使用 schema_prefix",
                },
            )
        
        # 加载配置
        log_info("加载配置...", quiet=quiet)
        config = get_config(config_path, reload=True)
        config.load()

        # 获取 DSN 和 admin_dsn
        target_dsn = dsn or config.get("postgres.dsn")
        admin_dsn = (
            os.environ.get("ENGRAM_PG_ADMIN_DSN")
            or config.get("postgres.admin_dsn")
        )
        project_key = config.get("project.project_key")

        if not target_dsn:
            return make_error_result(
                code="CONFIG_ERROR",
                message="未配置数据库 DSN（postgres.dsn）",
                detail={"hint": "请在配置文件中设置 [postgres].dsn 或通过参数传入"},
            )

        # 确保数据库存在（自动创建）
        db_result = ensure_database_exists(
            target_dsn=target_dsn,
            admin_dsn=admin_dsn,
            project_key=project_key,
            quiet=quiet,
        )
        
        if not db_result["ok"]:
            return make_error_result(
                code="DATABASE_CREATE_ERROR",
                message=db_result["message"],
                detail={
                    "db_name": db_result.get("db_name"),
                    "admin_dsn_configured": bool(admin_dsn),
                },
            )
        
        db_created = db_result.get("created", False)

        # 创建 SchemaContext
        schema_context = SchemaContext(schema_prefix=schema_prefix)
        
        if schema_prefix:
            log_info(f"使用 schema 前缀: {schema_prefix}", quiet=quiet)

        # SQL 文件路径 - 使用相对于此模块的路径
        # migrate.py 在 src/engram/logbook/ 目录下，sql 在项目根目录 sql/
        # 路径: src/engram/logbook/migrate.py -> sql/
        sql_dir = Path(__file__).parent.parent.parent.parent / "sql"
        
        # 确定是否执行角色权限脚本
        should_apply_roles = apply_roles
        if should_apply_roles is None:
            should_apply_roles = config.get("postgres.apply_roles", False)
        
        # 确定是否执行 OpenMemory schema 权限脚本
        should_apply_openmemory = apply_openmemory_grants
        if should_apply_openmemory is None:
            should_apply_openmemory = config.get("postgres.apply_openmemory_grants", None)
        if should_apply_openmemory is None:
            should_apply_openmemory = should_auto_apply_openmemory()
            if should_apply_openmemory:
                log_info("检测到 OM_METADATA_BACKEND=postgres，自动启用 OpenMemory schema 权限脚本", quiet=quiet)
        
        # 扫描和分类 SQL 文件
        log_info("扫描 SQL 文件...", quiet=quiet)
        sql_files = scan_sql_files(sql_dir)
        
        if not sql_files:
            return make_error_result(
                code="NO_SQL_FILES",
                message=f"SQL 目录为空或不存在: {sql_dir}",
                detail={"path": str(sql_dir)},
            )
        
        classified = classify_sql_files(
            sql_files,
            apply_roles=should_apply_roles,
            apply_openmemory_grants=should_apply_openmemory,
            verify=verify,
        )
        
        execute_sql_files = classified["execute"]
        
        if not execute_sql_files:
            return make_error_result(
                code="NO_EXECUTABLE_FILES",
                message="没有需要执行的 SQL 文件",
                detail={
                    "ddl_files": [str(f) for f in classified["ddl"]],
                    "permission_files": [str(f) for f in classified["permissions"]],
                    "verify_files": [str(f) for f in classified["verify"]],
                },
            )
        
        # 验证必需的 01_logbook_schema.sql 存在
        schema_sql = sql_dir / "01_logbook_schema.sql"
        if not schema_sql.exists():
            return make_error_result(
                code="FILE_NOT_FOUND",
                message=f"核心 SQL 文件不存在: {schema_sql}",
                detail={"path": str(schema_sql)},
            )
        
        log_info(f"将执行 {len(execute_sql_files)} 个 SQL 文件: {[f.name for f in execute_sql_files]}", quiet=quiet)

        # 获取实际需要验证的 schema 和列
        required_schemas = get_required_schemas(schema_context)
        required_columns = get_required_columns(schema_context)

        # 构建锁标识符
        lock_key = _build_lock_key(schema_prefix)

        # 建立连接（autocommit=True 让 SQL 文件自己控制事务）
        log_info("连接数据库...", quiet=quiet)
        executed_files = []
        openmemory_script_applied = False
        openmemory_target_schema = None
        
        with get_connection(dsn=dsn, config=config, autocommit=True) as conn:
            # 获取咨询锁
            _acquire_advisory_lock(conn, lock_key, quiet=quiet)
            try:
                # 权限脚本需要检查用户权限
                if should_apply_roles or should_apply_openmemory:
                    has_permission = check_has_superuser_privilege(conn)
                    if not has_permission:
                        permission_scripts = []
                        if should_apply_roles:
                            permission_scripts.append("04_roles_and_grants.sql")
                        if should_apply_openmemory:
                            permission_scripts.append("05_openmemory_roles_and_grants.sql")
                        repair_hint = get_repair_commands_hint("INSUFFICIENT_PRIVILEGE")
                        return make_error_result(
                            code="INSUFFICIENT_PRIVILEGE",
                            message="执行权限脚本需要 superuser 或 CREATEROLE 权限",
                            detail={
                                "scripts": permission_scripts,
                                "hint": "请使用具有 superuser 或 CREATEROLE 权限的用户执行",
                                **repair_hint,
                            },
                        )
                
                # 按顺序执行 SQL 文件
                for sql_file in execute_sql_files:
                    prefix = sql_file.name[:2]
                    is_openmemory_script = "openmemory" in sql_file.name.lower()
                    
                    # 05_openmemory_* 权限脚本需要特殊处理
                    if prefix == "05" and is_openmemory_script and should_apply_openmemory:
                        openmemory_target_schema = get_openmemory_schema()
                        log_info(f"执行 OpenMemory 脚本: {sql_file.name}，目标 schema: {openmemory_target_schema}...", quiet=quiet)
                        
                        with conn.cursor() as cur:
                            cur.execute(
                                sql.SQL("SET om.target_schema = {}")
                                .format(sql.Literal(openmemory_target_schema))
                            )
                        
                        execute_sql_file(conn, sql_file, schema_context=schema_context)
                        executed_files.append(str(sql_file))
                        openmemory_script_applied = True
                    elif prefix == "99":
                        log_info(f"执行验证脚本: {sql_file.name}...", quiet=quiet)
                        if should_apply_openmemory:
                            openmemory_target_schema = get_openmemory_schema()
                            with conn.cursor() as cur:
                                cur.execute(
                                    sql.SQL("SET om.target_schema = {}")
                                    .format(sql.Literal(openmemory_target_schema))
                                )
                        execute_sql_file(conn, sql_file, schema_context=schema_context)
                        executed_files.append(str(sql_file))
                    elif prefix in PERMISSION_SCRIPT_PREFIXES:
                        log_info(f"执行权限脚本: {sql_file.name}...", quiet=quiet)
                        execute_sql_file(conn, sql_file, schema_context=schema_context)
                        executed_files.append(str(sql_file))
                    else:
                        log_info(f"执行迁移脚本: {sql_file.name}...", quiet=quiet)
                        execute_sql_file(conn, sql_file, schema_context=schema_context)
                        executed_files.append(str(sql_file))

                # 自检验证
                log_info("验证 schema...", quiet=quiet)
                all_exist, missing = check_schemas_exist(conn, required_schemas)

                if not all_exist:
                    repair_hint = get_repair_commands_hint("SCHEMA_MISSING", db_result.get("db_name"))
                    return make_error_result(
                        code="SCHEMA_MISSING",
                        message=f"以下 schema 未创建成功: {', '.join(missing)}",
                        detail={
                            "missing_schemas": missing,
                            "hint": "请检查 SQL 脚本执行是否有错误",
                            "sql_file": str(schema_sql),
                            **repair_hint,
                        },
                    )

                log_info("验证核心表...", quiet=quiet)
                required_tables = get_required_tables(schema_context)
                tables_exist, missing_tables = check_tables_exist(conn, required_tables)

                if not tables_exist:
                    repair_hint = get_repair_commands_hint("TABLE_MISSING", db_result.get("db_name"))
                    return make_error_result(
                        code="TABLE_MISSING",
                        message=f"以下核心表未创建成功: {', '.join(missing_tables)}",
                        detail={
                            "missing_tables": missing_tables,
                            "total_required": len(required_tables),
                            **repair_hint,
                        },
                    )

                log_info("验证关键列...", quiet=quiet)
                cols_exist, missing_cols = check_columns_exist(conn, required_columns)

                if not cols_exist:
                    repair_hint = get_repair_commands_hint("COLUMN_MISSING", db_result.get("db_name"))
                    return make_error_result(
                        code="COLUMN_MISSING",
                        message=f"以下关键列未创建成功: {', '.join(missing_cols)}",
                        detail={
                            "missing_columns": missing_cols,
                            **repair_hint,
                        },
                    )

                log_info("验证关键索引...", quiet=quiet)
                required_indexes = get_required_indexes(schema_context)
                indexes_exist, missing_indexes = check_indexes_exist(conn, required_indexes)

                if not indexes_exist:
                    repair_hint = get_repair_commands_hint("INDEX_MISSING", db_result.get("db_name"))
                    return make_error_result(
                        code="INDEX_MISSING",
                        message=f"以下关键索引未创建成功: {', '.join(missing_indexes)}",
                        detail={
                            "missing_indexes": missing_indexes,
                            **repair_hint,
                        },
                    )

                log_info("验证关键触发器...", quiet=quiet)
                required_triggers = get_required_triggers(schema_context)
                triggers_exist, missing_triggers = check_triggers_exist(conn, required_triggers)

                if not triggers_exist:
                    repair_hint = get_repair_commands_hint("TRIGGER_MISSING", db_result.get("db_name"))
                    return make_error_result(
                        code="TRIGGER_MISSING",
                        message=f"以下关键触发器未创建成功: {', '.join(missing_triggers)}",
                        detail={
                            "missing_triggers": missing_triggers,
                            **repair_hint,
                        },
                    )

                log_info("验证物化视图...", quiet=quiet)
                required_matviews = get_required_matviews(schema_context)
                matviews_exist, missing_matviews = check_matviews_exist(conn, required_matviews)

                if not matviews_exist:
                    repair_hint = get_repair_commands_hint("MATVIEW_MISSING", db_result.get("db_name"))
                    return make_error_result(
                        code="MATVIEW_MISSING",
                        message=f"以下物化视图未创建成功: {', '.join(missing_matviews)}",
                        detail={
                            "missing_matviews": missing_matviews,
                            **repair_hint,
                        },
                    )

                # 验证 OpenMemory schema
                if openmemory_script_applied:
                    log_info(f"验证 OpenMemory schema: {openmemory_target_schema}...", quiet=quiet)
                    om_exists, om_message = check_openmemory_schema_exists(conn, openmemory_target_schema)
                    if not om_exists:
                        repair_hint = get_repair_commands_hint("OPENMEMORY_SCHEMA_MISSING", db_result.get("db_name"))
                        return make_error_result(
                            code="OPENMEMORY_SCHEMA_MISSING",
                            message=f"OpenMemory schema 未创建成功: {om_message}",
                            detail={
                                "target_schema": openmemory_target_schema,
                                **repair_hint,
                            },
                        )
            finally:
                _release_advisory_lock(conn, lock_key, quiet=quiet)

        log_info("迁移完成", quiet=quiet)
        
        # ========================================
        # 迁移后回填（post-backfill）
        # ========================================
        backfill_results = {}
        
        if post_backfill:
            log_info("执行迁移后回填: evidence_uri...", quiet=quiet)
            try:
                backfill_fn = _get_backfill_evidence_uri()
                evidence_result = backfill_fn(
                    batch_size=backfill_batch_size,
                    dry_run=backfill_dry_run,
                    config=config,
                )
                backfill_results["evidence_uri"] = evidence_result
                if evidence_result.get("success"):
                    log_info(
                        f"evidence_uri 回填完成: 更新={evidence_result.get('total_updated', 0)}",
                        quiet=quiet
                    )
                else:
                    log_warning(
                        f"evidence_uri 回填出现问题: {evidence_result.get('error', '未知错误')}",
                        quiet=quiet
                    )
            except Exception as e:
                log_error(f"evidence_uri 回填失败: {e}", quiet=quiet)
                backfill_results["evidence_uri"] = {
                    "success": False,
                    "error": str(e),
                }
        
        if backfill_chunking_version:
            log_info(f"执行迁移后回填: chunking_version={backfill_chunking_version}...", quiet=quiet)
            try:
                backfill_cv_fn = _get_backfill_chunking_version()
                chunking_result = backfill_cv_fn(
                    target_version=backfill_chunking_version,
                    batch_size=backfill_batch_size,
                    dry_run=backfill_dry_run,
                    config=config,
                )
                backfill_results["chunking_version"] = chunking_result
                if chunking_result.get("success"):
                    summary = chunking_result.get("summary", {})
                    log_info(
                        f"chunking_version 回填完成: 更新={summary.get('total_updated', 0)}",
                        quiet=quiet
                    )
                else:
                    log_warning(
                        f"chunking_version 回填出现问题: {chunking_result.get('error', '未知错误')}",
                        quiet=quiet
                    )
            except Exception as e:
                log_error(f"chunking_version 回填失败: {e}", quiet=quiet)
                backfill_results["chunking_version"] = {
                    "success": False,
                    "error": str(e),
                }
        
        return make_success_result(
            schemas=required_schemas,
            schema_prefix=schema_prefix,
            sql_files=executed_files,
            sql_classification={
                "ddl": [str(f) for f in classified["ddl"]],
                "permissions": [str(f) for f in classified["permissions"]],
                "verify": [str(f) for f in classified["verify"]],
            },
            db_created=db_created,
            db_name=db_result.get("db_name"),
            roles_applied=should_apply_roles,
            openmemory_grants_applied=should_apply_openmemory,
            openmemory_schema_applied=openmemory_script_applied,
            openmemory_target_schema=openmemory_target_schema,
            verify_executed=verify,
            verified={
                "tables": [f"{s}.{t}" for s, t in required_tables],
                "columns": [f"{s}.{t}.{c}" for s, t, c in required_columns],
                "indexes": [f"{s}.{i}" for s, i in required_indexes],
                "triggers": [f"{s}.{t}.{tr}" for s, t, tr in required_triggers],
                "matviews": [f"{s}.{v}" for s, v in required_matviews],
                "openmemory_schema": openmemory_target_schema if openmemory_script_applied else None,
            },
            summary={
                "schemas_count": len(required_schemas),
                "tables_count": len(required_tables),
                "indexes_count": len(required_indexes),
                "triggers_count": len(required_triggers),
                "matviews_count": len(required_matviews),
            },
            backfill_executed=post_backfill or bool(backfill_chunking_version),
            backfill_results=backfill_results if backfill_results else None,
        )

    except FileNotFoundError as e:
        return make_error_result(
            code="FILE_NOT_FOUND",
            message=str(e),
            detail={"error_type": "FileNotFoundError"},
        )
    except EngramError as e:
        return e.to_dict()
    except Exception as e:
        return make_error_result(
            code="MIGRATION_ERROR",
            message=f"{type(e).__name__}: {e}",
            detail={"error_type": type(e).__name__},
        )


# ============================================================================
# 导出
# ============================================================================

__all__ = [
    # 核心函数
    "run_all_checks",
    "run_migrate",
    "run_precheck",
    # 辅助函数
    "is_testing_mode",
    "get_repair_commands_hint",
    # 检查函数
    "check_schemas_exist",
    "check_tables_exist",
    "check_columns_exist",
    "check_indexes_exist",
    "check_triggers_exist",
    "check_matviews_exist",
    "check_openmemory_schema_exists",
    "check_search_path",
    # 常量
    "DEFAULT_SCHEMA_SUFFIXES",
    "REQUIRED_TABLE_TEMPLATES",
    "REQUIRED_COLUMN_TEMPLATES",
    "REQUIRED_INDEX_TEMPLATES",
    "REQUIRED_TRIGGER_TEMPLATES",
    "REQUIRED_MATVIEW_TEMPLATES",
    # Backfill 相关（延迟加载函数）
    "_get_backfill_evidence_uri",
    "_get_backfill_chunking_version",
]
