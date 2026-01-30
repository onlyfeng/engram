#!/usr/bin/env python3
"""
db_bootstrap - 数据库 bootstrap 预检与角色创建（简化版）
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional
from urllib.parse import urlparse, urlunparse


class BootstrapErrorCode:
    CONFIG_MISSING_DSN = "BOOTSTRAP_CONFIG_MISSING_DSN"
    CONFIG_INVALID_SCHEMA = "BOOTSTRAP_CONFIG_INVALID_SCHEMA"
    CONFIG_MISSING_PASSWORD = "BOOTSTRAP_CONFIG_MISSING_PASSWORD"
    PRECHECK_SCHEMA_PUBLIC = "BOOTSTRAP_PRECHECK_SCHEMA_PUBLIC"
    PRECHECK_NO_CREATEROLE = "BOOTSTRAP_PRECHECK_NO_CREATEROLE"
    ROLE_CREATION_MISSING_PASSWORD = "BOOTSTRAP_ROLE_CREATION_MISSING_PASSWORD"
    ROLE_CREATION_FAILED = "BOOTSTRAP_ROLE_CREATION_FAILED"


DEFAULT_OM_SCHEMA = "openmemory"

ENV_LOGBOOK_MIGRATOR_PASSWORD = "LOGBOOK_MIGRATOR_PASSWORD"
ENV_LOGBOOK_SVC_PASSWORD = "LOGBOOK_SVC_PASSWORD"
ENV_OPENMEMORY_MIGRATOR_PASSWORD = "OPENMEMORY_MIGRATOR_PASSWORD"
ENV_OPENMEMORY_SVC_PASSWORD = "OPENMEMORY_SVC_PASSWORD"

LOGIN_ROLES = [
    ("logbook_migrator", ENV_LOGBOOK_MIGRATOR_PASSWORD, "engram_migrator"),
    ("logbook_svc", ENV_LOGBOOK_SVC_PASSWORD, "engram_app_readwrite"),
    ("openmemory_migrator_login", ENV_OPENMEMORY_MIGRATOR_PASSWORD, "openmemory_migrator"),
    ("openmemory_svc", ENV_OPENMEMORY_SVC_PASSWORD, "openmemory_app"),
]

# 错误码 -> 修复命令映射（用于提示）
REMEDIATION_COMMANDS = {
    BootstrapErrorCode.CONFIG_MISSING_DSN: "请设置 POSTGRES_DSN 或 TEST_PG_DSN",
    BootstrapErrorCode.CONFIG_MISSING_PASSWORD: "请设置 LOGBOOK/OPENMEMORY 相关角色密码环境变量",
    BootstrapErrorCode.PRECHECK_SCHEMA_PUBLIC: "请设置 OM_PG_SCHEMA 为非 public 的 schema",
    BootstrapErrorCode.PRECHECK_NO_CREATEROLE: "请使用具备 CREATEROL 或 SUPERUSER 权限的账号",
    BootstrapErrorCode.ROLE_CREATION_MISSING_PASSWORD: "请提供登录角色密码（环境变量或参数）",
}


def check_om_schema_not_public(om_schema: str) -> Dict[str, object]:
    if om_schema is None:
        om_schema = ""
    if om_schema.lower() == "public":
        return {
            "ok": False,
            "code": BootstrapErrorCode.PRECHECK_SCHEMA_PUBLIC,
            "value": om_schema,
            "message": "om_schema 不允许为 public",
            "remediation": "请设置 OM_PG_SCHEMA 为非 public 的 schema",
        }
    return {"ok": True, "code": "", "value": om_schema, "message": "ok"}


def check_admin_privileges(conn) -> Dict[str, object]:
    with conn.cursor() as cur:
        cur.execute("SELECT usesuper, rolcreaterole FROM pg_roles WHERE rolname = current_user")
        row = cur.fetchone()
    is_superuser = bool(row[0]) if row else False
    can_create_role = bool(row[1]) if row else False
    ok = is_superuser or can_create_role
    return {
        "ok": ok,
        "code": "" if ok else BootstrapErrorCode.PRECHECK_NO_CREATEROLE,
        "message": "ok" if ok else "缺少 createrole 权限",
        "details": {"is_superuser": is_superuser, "can_create_role": can_create_role},
    }


def run_precheck(
    *,
    admin_dsn: Optional[str],
    om_schema: str,
    quiet: bool = False,
    skip_db_check: bool = False,
) -> Dict[str, object]:
    checks: Dict[str, Dict[str, object]] = {}
    failed_codes: List[str] = []

    schema_check = check_om_schema_not_public(om_schema)
    checks["om_schema_not_public"] = schema_check
    if not schema_check["ok"]:
        failed_codes.append(schema_check["code"])

    if skip_db_check or not admin_dsn:
        checks["admin_privileges"] = {"ok": True, "skipped": True}
    else:
        import psycopg
        try:
            conn = psycopg.connect(admin_dsn, autocommit=True)
            try:
                admin_check = check_admin_privileges(conn)
            finally:
                conn.close()
        except Exception:
            admin_check = {
                "ok": False,
                "code": BootstrapErrorCode.PRECHECK_NO_CREATEROLE,
                "message": "无法连接数据库或权限不足",
            }
        checks["admin_privileges"] = admin_check
        if not admin_check.get("ok"):
            failed_codes.append(admin_check.get("code", BootstrapErrorCode.PRECHECK_NO_CREATEROLE))

    return {
        "ok": len(failed_codes) == 0,
        "checks": checks,
        "failed_codes": failed_codes,
    }


def create_or_update_login_role(
    conn,
    *,
    role_name: str,
    password: Optional[str],
    inherit_role: Optional[str] = None,
    quiet: bool = False,
) -> Dict[str, object]:
    if not password:
        return {
            "ok": False,
            "code": BootstrapErrorCode.ROLE_CREATION_MISSING_PASSWORD,
            "message": "缺少密码",
            "remediation": f"请设置 {role_name} 的密码",
        }
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,))
            exists = cur.fetchone() is not None
            if not exists:
                cur.execute(f"CREATE ROLE {role_name} LOGIN PASSWORD %s", (password,))
                created = True
                updated = False
            else:
                cur.execute(f"ALTER ROLE {role_name} PASSWORD %s", (password,))
                created = False
                updated = True
            if inherit_role:
                try:
                    cur.execute(f"GRANT {inherit_role} TO {role_name}")
                except Exception:
                    pass
        return {
            "ok": True,
            "created": created,
            "updated": updated,
        }
    except Exception as exc:
        return {
            "ok": False,
            "code": BootstrapErrorCode.ROLE_CREATION_FAILED,
            "message": str(exc),
            "remediation": "请检查数据库权限与角色状态",
        }


def create_all_login_roles(
    conn,
    *,
    passwords: Dict[str, str],
    quiet: bool = False,
) -> Dict[str, object]:
    failed_codes: List[str] = []
    for role_name, env_key, inherit_role in LOGIN_ROLES:
        password = passwords.get(role_name) or os.environ.get(env_key)
        result = create_or_update_login_role(
            conn,
            role_name=role_name,
            password=password,
            inherit_role=inherit_role,
            quiet=quiet,
        )
        if not result.get("ok"):
            failed_codes.append(result.get("code"))
    return {
        "ok": len(failed_codes) == 0,
        "failed_codes": [c for c in failed_codes if c],
        "remediation": "请提供缺失的角色密码" if failed_codes else "",
    }


def parse_db_from_dsn(dsn: str) -> Optional[str]:
    if not dsn:
        return None
    parsed = urlparse(dsn)
    path = parsed.path.lstrip("/")
    return path or None


def mask_password_in_dsn(dsn: str) -> str:
    if not dsn:
        return dsn
    parsed = urlparse(dsn)
    if parsed.password:
        netloc = parsed.netloc.replace(parsed.password, "******")
        return urlunparse(parsed._replace(netloc=netloc))
    return dsn
