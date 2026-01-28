#!/usr/bin/env python3
"""
数据库 Bootstrap 脚本

用于初始化数据库角色、权限和安全硬化配置。
此脚本应在 db_migrate.py 之前运行，用于准备数据库环境。

使用方法:
    # 使用环境变量配置（推荐）
    export ENGRAM_PG_ADMIN_DSN="postgresql://postgres:password@localhost:5432/postgres"
    export STEP1_MIGRATOR_PASSWORD="xxx"
    export STEP1_SVC_PASSWORD="xxx"
    python db_bootstrap.py

    # 显式指定 DSN
    python db_bootstrap.py --admin-dsn "postgresql://postgres:password@localhost:5432/postgres"

    # 仅预检，不执行实际操作
    python db_bootstrap.py --precheck-only

环境变量:
    ENGRAM_PG_ADMIN_DSN:          管理员 DSN（必填，需要 CREATEROLE 权限）
    POSTGRES_DB:                  目标数据库名称（可选，默认从 DSN 解析）
    OM_PG_SCHEMA:                 OpenMemory 目标 schema（可选，默认 'openmemory'）

    密码环境变量（可选，若角色不存在则必须提供）:
    STEP1_MIGRATOR_PASSWORD:      step1_migrator 角色密码
    STEP1_SVC_PASSWORD:           step1_svc 角色密码
    OPENMEMORY_MIGRATOR_PASSWORD: openmemory_migrator_login 角色密码
    OPENMEMORY_SVC_PASSWORD:      openmemory_svc 角色密码

安全策略:
    - 禁止 OM_PG_SCHEMA=public（强制隔离）
    - 检查管理员账号具备 CREATEROLE 权限
    - 密码不会出现在日志或输出中
    - 所有敏感操作使用参数化查询

执行步骤:
    1. 预检（权限验证、schema 配置检查）
    2. 幂等创建/更新 LOGIN 角色
    3. 应用 sql/04_roles_and_grants.sql（NOLOGIN 权限角色）
    4. 应用 sql/05_openmemory_roles_and_grants.sql（OpenMemory schema）
    5. 数据库级硬化配置
    6. 执行 sql/99_verify_permissions.sql 验收
"""

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

# 确保可以导入本地模块
sys.path.insert(0, str(Path(__file__).parent))

from engram_step1.errors import (
    ConfigError,
    DatabaseError,
    EngramError,
    ExitCode,
    make_error_result,
    make_success_result,
)
from engram_step1.io import (
    add_output_arguments,
    get_output_options,
    log_error,
    log_info,
    log_warning,
    output_json,
)

# 模块 logger
logger = logging.getLogger(__name__)

# ============================================================================
# 常量定义
# ============================================================================

# 环境变量名称
ENV_ADMIN_DSN = "ENGRAM_PG_ADMIN_DSN"
ENV_POSTGRES_DB = "POSTGRES_DB"
ENV_OM_SCHEMA = "OM_PG_SCHEMA"

# 密码环境变量
ENV_STEP1_MIGRATOR_PASSWORD = "STEP1_MIGRATOR_PASSWORD"
ENV_STEP1_SVC_PASSWORD = "STEP1_SVC_PASSWORD"
ENV_OPENMEMORY_MIGRATOR_PASSWORD = "OPENMEMORY_MIGRATOR_PASSWORD"
ENV_OPENMEMORY_SVC_PASSWORD = "OPENMEMORY_SVC_PASSWORD"

# LOGIN 角色定义：(角色名, 密码环境变量, 继承的 NOLOGIN 角色)
LOGIN_ROLES = [
    ("step1_migrator", ENV_STEP1_MIGRATOR_PASSWORD, "engram_migrator"),
    ("step1_svc", ENV_STEP1_SVC_PASSWORD, "engram_app_readwrite"),
    ("openmemory_migrator_login", ENV_OPENMEMORY_MIGRATOR_PASSWORD, "openmemory_migrator"),
    ("openmemory_svc", ENV_OPENMEMORY_SVC_PASSWORD, "openmemory_app"),
]

# SQL 文件名
SQL_ROLES_GRANTS = "04_roles_and_grants.sql"
SQL_OPENMEMORY_GRANTS = "05_openmemory_roles_and_grants.sql"
SQL_DATABASE_HARDENING = "08_database_hardening.sql"
SQL_VERIFY_PERMISSIONS = "99_verify_permissions.sql"

# 需要 CONNECT 权限的登录角色
DB_CONNECT_ROLES = [
    "step1_svc",
    "step1_migrator",
    "openmemory_svc",
    "openmemory_migrator_login",
]

# 需要 CREATE/TEMP 权限的迁移角色（NOLOGIN，由 LOGIN 角色继承）
DB_DDL_ROLES = [
    "engram_migrator",
    "openmemory_migrator",
]

# 默认 OpenMemory schema
DEFAULT_OM_SCHEMA = "openmemory"

# 安全事件来源标识
SECURITY_EVENT_SOURCE = "db_bootstrap"


# ============================================================================
# 安全事件记录
# ============================================================================


def log_security_event(
    conn,
    actor: str,
    action: str,
    object_type: str,
    object_id: Optional[str] = None,
    detail: Optional[dict] = None,
    source: str = SECURITY_EVENT_SOURCE,
) -> bool:
    """
    记录安全事件到 governance.security_events 表。
    
    事件记录不含敏感明文（如密码），仅记录操作元数据用于审计。
    
    Args:
        conn: 数据库连接
        actor: 操作者标识（用户名/角色名）
        action: 操作类型（如 role_created, grant_applied）
        object_type: 操作对象类型（如 role, database, schema）
        object_id: 操作对象标识（可选）
        detail: 详细信息字典（可选，不含敏感信息）
        source: 事件来源标识
    
    Returns:
        True 表示记录成功，False 表示记录失败（不阻塞主流程）
    """
    try:
        # 检查表是否存在（迁移可能尚未执行）
        with conn.cursor() as cur:
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'governance' AND table_name = 'security_events'
                )
            """)
            if not cur.fetchone()[0]:
                # 表不存在，跳过记录（首次 bootstrap 时可能发生）
                return False
            
            # 插入安全事件
            cur.execute("""
                INSERT INTO governance.security_events
                    (actor, action, object_type, object_id, detail_json, source)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (
                actor,
                action,
                object_type,
                object_id,
                json.dumps(detail) if detail else None,
                source,
            ))
        return True
    except Exception:
        # 安全事件记录失败不应阻塞主流程
        return False


# ============================================================================
# Bootstrap 错误类
# ============================================================================


class BootstrapError(EngramError):
    """Bootstrap 相关错误"""

    exit_code = ExitCode.DATABASE_ERROR
    error_type = "BOOTSTRAP_ERROR"


class PrecheckError(BootstrapError):
    """预检失败错误"""

    error_type = "PRECHECK_ERROR"


class PermissionError(BootstrapError):
    """权限不足错误"""

    error_type = "PERMISSION_ERROR"


class RoleCreationError(BootstrapError):
    """角色创建错误"""

    error_type = "ROLE_CREATION_ERROR"


class VerificationError(BootstrapError):
    """验收失败错误"""

    error_type = "VERIFICATION_ERROR"


# ============================================================================
# DSN 解析工具
# ============================================================================


def parse_db_from_dsn(dsn: str) -> Optional[str]:
    """
    从 DSN 中解析数据库名称。
    
    Args:
        dsn: PostgreSQL DSN
    
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


def mask_password_in_dsn(dsn: str) -> str:
    """
    隐藏 DSN 中的密码（用于日志输出）。
    
    Args:
        dsn: 原始 DSN
    
    Returns:
        密码被 *** 替换的 DSN
    """
    try:
        parsed = urlparse(dsn)
        if parsed.password:
            # 替换密码为 ***
            masked = dsn.replace(f":{parsed.password}@", ":***@")
            return masked
        return dsn
    except Exception:
        # 回退到正则替换
        return re.sub(r"(://[^:]+:)[^@]+(@)", r"\1***\2", dsn)


# ============================================================================
# 预检函数
# ============================================================================


def check_om_schema_not_public(om_schema: str) -> tuple[bool, str]:
    """
    检查 OpenMemory schema 配置是否安全（禁止 public）。
    
    Args:
        om_schema: OpenMemory 目标 schema 名称
    
    Returns:
        (ok, message)
    """
    if om_schema.lower() == "public":
        return False, (
            "OM_PG_SCHEMA=public 是禁止的配置！"
            "OpenMemory 必须使用独立 schema 以确保隔离性和可维护性。"
            f"请设置 OM_PG_SCHEMA 为非 public 值（如 '{DEFAULT_OM_SCHEMA}'）"
        )
    return True, ""


def check_admin_privileges(conn) -> tuple[bool, str, dict]:
    """
    检查管理员连接是否具备必要权限。
    
    检查项：
    1. CREATEROLE 权限（创建登录角色）
    2. CREATE SCHEMA 权限（创建 OpenMemory schema）
    
    Args:
        conn: 数据库连接
    
    Returns:
        (ok, message, details)
    """
    details = {
        "can_create_role": False,
        "can_create_schema": False,
        "is_superuser": False,
        "current_user": None,
    }
    
    try:
        with conn.cursor() as cur:
            # 获取当前用户信息
            cur.execute("""
                SELECT 
                    current_user,
                    rolcreaterole,
                    rolsuper
                FROM pg_roles
                WHERE rolname = current_user
            """)
            row = cur.fetchone()
            
            if row is None:
                return False, "无法获取当前用户信息", details
            
            current_user, can_create_role, is_superuser = row
            details["current_user"] = current_user
            details["can_create_role"] = can_create_role or is_superuser
            details["is_superuser"] = is_superuser
            
            # superuser 拥有所有权限
            if is_superuser:
                details["can_create_schema"] = True
                return True, "", details
            
            # 检查 CREATEROLE 权限
            if not can_create_role:
                return False, (
                    f"用户 '{current_user}' 缺少 CREATEROLE 权限，"
                    "无法创建登录角色。请使用具有 CREATEROLE 权限的账号连接。"
                ), details
            
            # 检查 CREATE SCHEMA 权限（在当前数据库）
            cur.execute("""
                SELECT has_database_privilege(current_user, current_database(), 'CREATE')
            """)
            can_create = cur.fetchone()[0]
            details["can_create_schema"] = can_create
            
            if not can_create:
                return False, (
                    f"用户 '{current_user}' 在当前数据库缺少 CREATE 权限，"
                    "无法创建 schema。请授予相应权限或使用 superuser 连接。"
                ), details
            
            return True, "", details
            
    except Exception as e:
        return False, f"权限检查失败: {e}", details


def run_precheck(
    admin_dsn: str,
    om_schema: str,
    quiet: bool = False,
) -> dict:
    """
    执行所有预检项。
    
    Args:
        admin_dsn: 管理员 DSN
        om_schema: OpenMemory 目标 schema
        quiet: 静默模式
    
    Returns:
        {ok: bool, checks: {...}, message: str}
    """
    import psycopg
    
    checks = {}
    all_ok = True
    messages = []
    
    # 1. 检查 OM schema 配置
    log_info("预检: 检查 OpenMemory schema 配置...", quiet=quiet)
    om_ok, om_msg = check_om_schema_not_public(om_schema)
    checks["om_schema_not_public"] = {"ok": om_ok, "message": om_msg, "value": om_schema}
    if not om_ok:
        all_ok = False
        messages.append(om_msg)
        if not quiet:
            log_error(om_msg)
    else:
        log_info(f"预检: OpenMemory schema = '{om_schema}' (合法)", quiet=quiet)
    
    # 2. 检查管理员权限
    log_info("预检: 检查管理员权限...", quiet=quiet)
    try:
        conn = psycopg.connect(admin_dsn, autocommit=True)
        try:
            priv_ok, priv_msg, priv_details = check_admin_privileges(conn)
            checks["admin_privileges"] = {
                "ok": priv_ok,
                "message": priv_msg,
                "details": priv_details,
            }
            if not priv_ok:
                all_ok = False
                messages.append(priv_msg)
                if not quiet:
                    log_error(priv_msg)
            else:
                log_info(
                    f"预检: 管理员权限正常 (user={priv_details['current_user']}, "
                    f"superuser={priv_details['is_superuser']})",
                    quiet=quiet,
                )
        finally:
            conn.close()
    except Exception as e:
        error_msg = f"无法连接到管理员数据库: {e}"
        checks["admin_privileges"] = {"ok": False, "message": error_msg, "details": {}}
        all_ok = False
        messages.append(error_msg)
        if not quiet:
            log_error(error_msg)
    
    return {
        "ok": all_ok,
        "checks": checks,
        "message": "; ".join(messages) if messages else "",
    }


# ============================================================================
# 角色管理函数
# ============================================================================


def check_role_exists(conn, role_name: str) -> bool:
    """
    检查角色是否存在。
    
    Args:
        conn: 数据库连接
        role_name: 角色名称
    
    Returns:
        True 表示角色存在
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s",
            (role_name,)
        )
        return cur.fetchone() is not None


def create_or_update_login_role(
    conn,
    role_name: str,
    password: Optional[str],
    inherit_role: str,
    quiet: bool = False,
) -> dict:
    """
    幂等创建或更新 LOGIN 角色。
    
    如果角色不存在，创建新角色并设置密码。
    如果角色已存在，更新密码（若提供）并确保角色继承正确。
    
    Args:
        conn: 数据库连接
        role_name: 角色名称
        password: 角色密码（None 表示不设置/不更新密码）
        inherit_role: 要继承的 NOLOGIN 角色名称
        quiet: 静默模式
    
    Returns:
        {ok: bool, created: bool, updated: bool, message: str}
    """
    from psycopg import sql
    
    result = {
        "ok": True,
        "role": role_name,
        "created": False,
        "updated": False,
        "message": "",
        "inherit_role": inherit_role,
    }
    
    try:
        exists = check_role_exists(conn, role_name)
        
        if not exists:
            # 创建新角色
            if password is None:
                result["ok"] = False
                result["message"] = f"角色 '{role_name}' 不存在，但未提供密码（请设置对应环境变量）"
                return result
            
            log_info(f"创建登录角色: {role_name}...", quiet=quiet)
            with conn.cursor() as cur:
                # 使用 sql.SQL 和 sql.Identifier 安全构建 SQL
                # 密码通过 sql.Literal 传入（psycopg3 会正确引用）
                create_sql = sql.SQL(
                    "CREATE ROLE {} LOGIN PASSWORD {}"
                ).format(
                    sql.Identifier(role_name),
                    sql.Literal(password)
                )
                cur.execute(create_sql)
            result["created"] = True
            log_info(f"角色 {role_name} 创建成功", quiet=quiet)
        else:
            # 角色已存在，更新密码（如果提供）
            if password is not None:
                log_info(f"更新角色密码: {role_name}...", quiet=quiet)
                with conn.cursor() as cur:
                    update_sql = sql.SQL(
                        "ALTER ROLE {} PASSWORD {}"
                    ).format(
                        sql.Identifier(role_name),
                        sql.Literal(password)
                    )
                    cur.execute(update_sql)
                result["updated"] = True
                log_info(f"角色 {role_name} 密码已更新", quiet=quiet)
            else:
                log_info(f"角色 {role_name} 已存在（密码未变更）", quiet=quiet)
        
        # 确保角色继承正确的 NOLOGIN 角色
        # 先检查 inherit_role 是否存在
        if check_role_exists(conn, inherit_role):
            with conn.cursor() as cur:
                # 使用 GRANT 授予成员身份（幂等）
                grant_sql = sql.SQL("GRANT {} TO {}").format(
                    sql.Identifier(inherit_role),
                    sql.Identifier(role_name)
                )
                cur.execute(grant_sql)
            log_info(f"授权 {role_name} <- {inherit_role}", quiet=quiet)
        else:
            # inherit_role 不存在，这是正常的（将在 04_roles_and_grants.sql 中创建）
            log_info(
                f"跳过授权 {role_name} <- {inherit_role}（角色 {inherit_role} 尚未创建）",
                quiet=quiet,
            )
        
        # 记录安全事件（不含密码等敏感信息）
        if result["created"] or result["updated"]:
            log_security_event(
                conn,
                actor="bootstrap_admin",
                action="role_created" if result["created"] else "role_updated",
                object_type="role",
                object_id=role_name,
                detail={
                    "created": result["created"],
                    "updated": result["updated"],
                    "inherited_role": inherit_role,
                    "password_changed": password is not None,
                },
            )
        
        return result
        
    except Exception as e:
        result["ok"] = False
        result["message"] = f"角色操作失败: {e}"
        return result


def create_all_login_roles(
    conn,
    passwords: dict[str, Optional[str]],
    quiet: bool = False,
) -> dict:
    """
    创建所有登录角色。
    
    Args:
        conn: 数据库连接
        passwords: {环境变量名: 密码值} 字典
        quiet: 静默模式
    
    Returns:
        {ok: bool, roles: [...], message: str}
    """
    results = []
    all_ok = True
    messages = []
    
    for role_name, env_var, inherit_role in LOGIN_ROLES:
        password = passwords.get(env_var)
        result = create_or_update_login_role(
            conn,
            role_name,
            password,
            inherit_role,
            quiet=quiet,
        )
        results.append(result)
        if not result["ok"]:
            all_ok = False
            messages.append(result["message"])
    
    return {
        "ok": all_ok,
        "roles": results,
        "message": "; ".join(messages) if messages else "",
    }


# ============================================================================
# SQL 脚本执行
# ============================================================================


def execute_sql_file_raw(conn, sql_path: Path, quiet: bool = False) -> dict:
    """
    执行 SQL 文件（原始执行，不做 schema 替换）。
    
    Args:
        conn: 数据库连接
        sql_path: SQL 文件路径
        quiet: 静默模式
    
    Returns:
        {ok: bool, message: str}
    """
    if not sql_path.exists():
        return {"ok": False, "message": f"SQL 文件不存在: {sql_path}"}
    
    try:
        log_info(f"执行 SQL 脚本: {sql_path.name}...", quiet=quiet)
        sql_content = sql_path.read_text(encoding="utf-8")
        
        with conn.cursor() as cur:
            cur.execute(sql_content)
        
        log_info(f"SQL 脚本执行成功: {sql_path.name}", quiet=quiet)
        return {"ok": True, "message": ""}
        
    except Exception as e:
        return {"ok": False, "message": f"SQL 执行失败: {e}"}


def apply_roles_and_grants(
    conn,
    sql_dir: Path,
    om_schema: str,
    quiet: bool = False,
) -> dict:
    """
    应用角色权限 SQL 脚本。
    
    执行顺序：
    1. 04_roles_and_grants.sql（创建 NOLOGIN 权限角色）
    2. 05_openmemory_roles_and_grants.sql（OpenMemory schema 权限）
    
    Args:
        conn: 数据库连接
        sql_dir: SQL 文件目录
        om_schema: OpenMemory 目标 schema
        quiet: 静默模式
    
    Returns:
        {ok: bool, executed: [...], message: str}
    """
    executed = []
    messages = []
    
    # 1. 执行 04_roles_and_grants.sql
    roles_sql = sql_dir / SQL_ROLES_GRANTS
    if roles_sql.exists():
        result = execute_sql_file_raw(conn, roles_sql, quiet=quiet)
        executed.append({"file": str(roles_sql), "ok": result["ok"]})
        if not result["ok"]:
            messages.append(result["message"])
            return {
                "ok": False,
                "executed": executed,
                "message": "; ".join(messages),
            }
    else:
        log_warning(f"跳过 {SQL_ROLES_GRANTS}（文件不存在）", quiet=quiet)
    
    # 2. 设置 om.target_schema 并执行 05_openmemory_roles_and_grants.sql
    om_sql = sql_dir / SQL_OPENMEMORY_GRANTS
    if om_sql.exists():
        log_info(f"设置 om.target_schema = '{om_schema}'", quiet=quiet)
        with conn.cursor() as cur:
            cur.execute("SET om.target_schema = %s", (om_schema,))
        
        result = execute_sql_file_raw(conn, om_sql, quiet=quiet)
        executed.append({"file": str(om_sql), "ok": result["ok"]})
        if not result["ok"]:
            messages.append(result["message"])
            return {
                "ok": False,
                "executed": executed,
                "message": "; ".join(messages),
            }
    else:
        log_warning(f"跳过 {SQL_OPENMEMORY_GRANTS}（文件不存在）", quiet=quiet)
    
    return {
        "ok": True,
        "executed": executed,
        "message": "",
    }


# ============================================================================
# 数据库硬化
# ============================================================================


def apply_database_hardening(
    conn,
    target_db: str,
    quiet: bool = False,
) -> dict:
    """
    应用数据库级安全硬化配置。
    
    硬化措施：
    1. 撤销 PUBLIC 在目标数据库的 CREATE, TEMP 权限
    2. 撤销 PUBLIC 在 public schema 的 CREATE 权限
    3. 授予登录角色 CONNECT 权限
    4. 授予迁移角色 CREATE, TEMP 权限（用于执行 DDL）
    
    参考文档：sql/08_database_hardening.sql
    
    Args:
        conn: 数据库连接
        target_db: 目标数据库名称
        quiet: 静默模式
    
    Returns:
        {ok: bool, applied: [...], message: str}
    """
    from psycopg import sql
    
    applied = []
    
    try:
        log_info(f"应用数据库硬化配置: {target_db}...", quiet=quiet)
        
        with conn.cursor() as cur:
            # ========================================
            # 1. 撤销 PUBLIC 的数据库级默认权限
            # ========================================
            # PostgreSQL 默认授予 PUBLIC: CONNECT, TEMP
            # 撤销后需要显式授权才能连接或创建临时对象
            revoke_db_sql = sql.SQL(
                "REVOKE CREATE, TEMP ON DATABASE {} FROM PUBLIC"
            ).format(sql.Identifier(target_db))
            cur.execute(revoke_db_sql)
            applied.append(f"REVOKE CREATE, TEMP ON DATABASE {target_db} FROM PUBLIC")
            log_info("已撤销 PUBLIC 在数据库的 CREATE, TEMP 权限", quiet=quiet)
            
            # ========================================
            # 2. 撤销 PUBLIC 在 public schema 的 CREATE 权限
            # ========================================
            # 这在 04_roles_and_grants.sql 中已执行，这里再次确保
            cur.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            applied.append("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            log_info("已撤销 PUBLIC 在 public schema 的 CREATE 权限", quiet=quiet)
            
            # ========================================
            # 3. 授予登录角色 CONNECT 权限
            # ========================================
            for role_name in DB_CONNECT_ROLES:
                if check_role_exists(conn, role_name):
                    grant_sql = sql.SQL(
                        "GRANT CONNECT ON DATABASE {} TO {}"
                    ).format(
                        sql.Identifier(target_db),
                        sql.Identifier(role_name)
                    )
                    cur.execute(grant_sql)
                    applied.append(f"GRANT CONNECT ON DATABASE {target_db} TO {role_name}")
                    log_info(f"已授予 {role_name} CONNECT 权限", quiet=quiet)
                else:
                    log_info(f"跳过 {role_name} CONNECT 授权（角色不存在）", quiet=quiet)
            
            # ========================================
            # 4. 授予迁移角色 CREATE, TEMP 权限
            # ========================================
            # 迁移角色需要执行 DDL，必须有 CREATE/TEMP 权限
            # 这些是 NOLOGIN 角色，LOGIN 角色通过继承获得权限
            for role_name in DB_DDL_ROLES:
                if check_role_exists(conn, role_name):
                    grant_sql = sql.SQL(
                        "GRANT CREATE, TEMP ON DATABASE {} TO {}"
                    ).format(
                        sql.Identifier(target_db),
                        sql.Identifier(role_name)
                    )
                    cur.execute(grant_sql)
                    applied.append(f"GRANT CREATE, TEMP ON DATABASE {target_db} TO {role_name}")
                    log_info(f"已授予 {role_name} CREATE, TEMP 权限", quiet=quiet)
                else:
                    log_info(f"跳过 {role_name} CREATE/TEMP 授权（角色不存在）", quiet=quiet)
        
        log_info("数据库硬化配置完成", quiet=quiet)
        
        # 记录安全事件
        log_security_event(
            conn,
            actor="bootstrap_admin",
            action="hardening_applied",
            object_type="database",
            object_id=target_db,
            detail={
                "applied_count": len(applied),
                "applied_operations": applied,
            },
        )
        
        return {
            "ok": True,
            "applied": applied,
            "message": "",
        }
        
    except Exception as e:
        return {
            "ok": False,
            "applied": applied,
            "message": f"硬化配置失败: {e}",
        }


# ============================================================================
# 权限验收
# ============================================================================


def run_verification(
    conn,
    sql_dir: Path,
    om_schema: str,
    quiet: bool = False,
) -> dict:
    """
    执行权限验收脚本。
    
    Args:
        conn: 数据库连接
        sql_dir: SQL 文件目录
        om_schema: OpenMemory 目标 schema
        quiet: 静默模式
    
    Returns:
        {ok: bool, warnings: [...], message: str}
    """
    verify_sql = sql_dir / SQL_VERIFY_PERMISSIONS
    
    if not verify_sql.exists():
        log_warning(f"验收脚本不存在: {verify_sql}", quiet=quiet)
        return {
            "ok": True,
            "warnings": ["验收脚本不存在，跳过验收"],
            "message": "",
        }
    
    try:
        log_info("执行权限验收...", quiet=quiet)
        
        # 设置 om.target_schema 参数
        with conn.cursor() as cur:
            cur.execute("SET om.target_schema = %s", (om_schema,))
        
        # 执行验收脚本
        sql_content = verify_sql.read_text(encoding="utf-8")
        
        # 捕获 NOTICE/WARNING 消息
        warnings = []
        
        with conn.cursor() as cur:
            cur.execute(sql_content)
            
            # 获取所有通知消息
            while conn.pgconn.notifies:
                notify = conn.pgconn.notifies.pop(0)
                warnings.append(notify.payload)
        
        log_info("权限验收完成", quiet=quiet)
        
        # 检查是否有 FAIL 或 WARNING 消息
        fail_count = sum(1 for w in warnings if "FAIL" in w.upper())
        if fail_count > 0:
            return {
                "ok": False,
                "warnings": warnings,
                "fail_count": fail_count,
                "message": f"验收发现 {fail_count} 个失败项",
            }
        
        return {
            "ok": True,
            "warnings": warnings,
            "message": "",
        }
        
    except Exception as e:
        return {
            "ok": False,
            "warnings": [],
            "message": f"验收脚本执行失败: {e}",
        }


# ============================================================================
# 主函数
# ============================================================================


def run_bootstrap(
    admin_dsn: Optional[str] = None,
    target_db: Optional[str] = None,
    om_schema: Optional[str] = None,
    precheck_only: bool = False,
    quiet: bool = False,
) -> dict:
    """
    执行数据库 Bootstrap。
    
    Args:
        admin_dsn: 管理员 DSN（默认从环境变量读取）
        target_db: 目标数据库名称（默认从 DSN 或环境变量读取）
        om_schema: OpenMemory schema 名称（默认从环境变量读取）
        precheck_only: 仅执行预检
        quiet: 静默模式
    
    Returns:
        {ok: True, ...} 或 {ok: False, code, message, detail}
    """
    import psycopg
    
    try:
        # ========================================
        # 1. 解析配置
        # ========================================
        
        # 管理员 DSN
        admin_dsn = admin_dsn or os.environ.get(ENV_ADMIN_DSN)
        if not admin_dsn:
            return make_error_result(
                code="CONFIG_ERROR",
                message=f"未配置管理员 DSN（{ENV_ADMIN_DSN}）",
                detail={
                    "hint": f"请设置环境变量 {ENV_ADMIN_DSN} 或通过 --admin-dsn 参数传入",
                },
            )
        
        # 目标数据库名称
        target_db = target_db or os.environ.get(ENV_POSTGRES_DB) or parse_db_from_dsn(admin_dsn)
        if not target_db:
            return make_error_result(
                code="CONFIG_ERROR",
                message="无法确定目标数据库名称",
                detail={
                    "hint": f"请设置环境变量 {ENV_POSTGRES_DB} 或在 DSN 中指定数据库名",
                },
            )
        
        # OpenMemory schema
        om_schema = om_schema or os.environ.get(ENV_OM_SCHEMA, DEFAULT_OM_SCHEMA)
        
        # 读取密码环境变量（不记录到日志）
        passwords = {
            ENV_STEP1_MIGRATOR_PASSWORD: os.environ.get(ENV_STEP1_MIGRATOR_PASSWORD),
            ENV_STEP1_SVC_PASSWORD: os.environ.get(ENV_STEP1_SVC_PASSWORD),
            ENV_OPENMEMORY_MIGRATOR_PASSWORD: os.environ.get(ENV_OPENMEMORY_MIGRATOR_PASSWORD),
            ENV_OPENMEMORY_SVC_PASSWORD: os.environ.get(ENV_OPENMEMORY_SVC_PASSWORD),
        }
        
        log_info("Bootstrap 配置:", quiet=quiet)
        log_info(f"  admin_dsn: {mask_password_in_dsn(admin_dsn)}", quiet=quiet)
        log_info(f"  target_db: {target_db}", quiet=quiet)
        log_info(f"  om_schema: {om_schema}", quiet=quiet)
        
        # ========================================
        # 2. 预检
        # ========================================
        
        log_info("开始预检...", quiet=quiet)
        precheck_result = run_precheck(admin_dsn, om_schema, quiet=quiet)
        
        if not precheck_result["ok"]:
            return make_error_result(
                code="PRECHECK_FAILED",
                message="预检失败，无法继续 Bootstrap",
                detail={
                    "checks": precheck_result["checks"],
                    "hint": "请检查配置和权限后重试",
                },
            )
        
        log_info("预检通过", quiet=quiet)
        
        if precheck_only:
            return make_success_result(
                precheck_only=True,
                checks=precheck_result["checks"],
                target_db=target_db,
                om_schema=om_schema,
            )
        
        # ========================================
        # 3. 连接数据库执行 Bootstrap
        # ========================================
        
        log_info("连接数据库...", quiet=quiet)
        conn = psycopg.connect(admin_dsn, autocommit=True)
        
        try:
            # SQL 文件目录
            sql_dir = Path(__file__).parent.parent / "sql"
            
            # ========================================
            # 3.1 创建登录角色
            # ========================================
            
            log_info("创建登录角色...", quiet=quiet)
            roles_result = create_all_login_roles(conn, passwords, quiet=quiet)
            
            if not roles_result["ok"]:
                return make_error_result(
                    code="ROLE_CREATION_ERROR",
                    message="登录角色创建失败",
                    detail={
                        "roles": roles_result["roles"],
                        "hint": "请检查密码环境变量是否设置正确",
                    },
                )
            
            # ========================================
            # 3.2 应用权限脚本
            # ========================================
            
            log_info("应用权限脚本...", quiet=quiet)
            grants_result = apply_roles_and_grants(conn, sql_dir, om_schema, quiet=quiet)
            
            if not grants_result["ok"]:
                return make_error_result(
                    code="GRANTS_ERROR",
                    message="权限脚本执行失败",
                    detail={
                        "executed": grants_result["executed"],
                        "error": grants_result["message"],
                    },
                )
            
            # ========================================
            # 3.3 数据库硬化
            # ========================================
            
            log_info("应用数据库硬化...", quiet=quiet)
            hardening_result = apply_database_hardening(conn, target_db, quiet=quiet)
            
            if not hardening_result["ok"]:
                return make_error_result(
                    code="HARDENING_ERROR",
                    message="数据库硬化失败",
                    detail={
                        "applied": hardening_result["applied"],
                        "error": hardening_result["message"],
                    },
                )
            
            # ========================================
            # 3.4 权限验收
            # ========================================
            
            log_info("执行权限验收...", quiet=quiet)
            verify_result = run_verification(conn, sql_dir, om_schema, quiet=quiet)
            
            if not verify_result["ok"]:
                return make_error_result(
                    code="VERIFICATION_FAILED",
                    message="权限验收失败",
                    detail={
                        "warnings": verify_result.get("warnings", []),
                        "fail_count": verify_result.get("fail_count", 0),
                        "error": verify_result["message"],
                    },
                )
            
            # ========================================
            # 3.5 记录 Bootstrap 完成事件
            # ========================================
            
            log_security_event(
                conn,
                actor="bootstrap_admin",
                action="bootstrap_completed",
                object_type="database",
                object_id=target_db,
                detail={
                    "om_schema": om_schema,
                    "roles_created": [r["role"] for r in roles_result["roles"] if r.get("created")],
                    "roles_updated": [r["role"] for r in roles_result["roles"] if r.get("updated")],
                    "sql_executed": [e["file"] for e in grants_result["executed"]],
                },
            )
            
        finally:
            conn.close()
        
        # ========================================
        # 4. 返回成功结果
        # ========================================
        
        log_info("Bootstrap 完成", quiet=quiet)
        
        return make_success_result(
            target_db=target_db,
            om_schema=om_schema,
            roles_created=[r["role"] for r in roles_result["roles"] if r.get("created")],
            roles_updated=[r["role"] for r in roles_result["roles"] if r.get("updated")],
            roles_unchanged=[
                r["role"] for r in roles_result["roles"]
                if not r.get("created") and not r.get("updated") and r.get("ok")
            ],
            sql_executed=[e["file"] for e in grants_result["executed"]],
            hardening_applied=hardening_result["applied"],
            verification=verify_result,
        )
        
    except EngramError as e:
        return e.to_dict()
    except Exception as e:
        return make_error_result(
            code="BOOTSTRAP_ERROR",
            message=f"{type(e).__name__}: {e}",
            detail={"error_type": type(e).__name__},
        )


def main():
    parser = argparse.ArgumentParser(
        description="数据库 Bootstrap：初始化角色、权限和安全配置",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    
    parser.add_argument(
        "--admin-dsn",
        type=str,
        default=None,
        help=f"管理员 DSN（优先于环境变量 {ENV_ADMIN_DSN}）",
    )
    
    parser.add_argument(
        "--target-db",
        type=str,
        default=None,
        help=f"目标数据库名称（优先于环境变量 {ENV_POSTGRES_DB}）",
    )
    
    parser.add_argument(
        "--om-schema",
        type=str,
        default=None,
        help=f"OpenMemory schema 名称（优先于环境变量 {ENV_OM_SCHEMA}，默认 '{DEFAULT_OM_SCHEMA}'）",
    )
    
    parser.add_argument(
        "--precheck-only",
        action="store_true",
        default=False,
        help="仅执行预检，不执行实际操作",
    )
    
    add_output_arguments(parser)
    
    args = parser.parse_args()
    
    opts = get_output_options(args)
    result = run_bootstrap(
        admin_dsn=args.admin_dsn,
        target_db=args.target_db,
        om_schema=args.om_schema,
        precheck_only=args.precheck_only,
        quiet=opts["quiet"],
    )
    
    output_json(result, pretty=opts["pretty"])
    
    # 根据 ok 字段决定退出码
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
