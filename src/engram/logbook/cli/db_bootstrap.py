#!/usr/bin/env python3
"""
数据库 bootstrap 预检与角色创建 - CLI 入口

使用方法:
    python -m engram.logbook.cli.db_bootstrap
    python -m engram.logbook.cli.db_bootstrap --dsn "postgresql://..."
    python -m engram.logbook.cli.db_bootstrap --require-roles

    或使用 console script:
    engram-bootstrap-roles
    engram-bootstrap-roles --dsn "postgresql://..."

环境变量:
    ENGRAM_PG_ADMIN_DSN / POSTGRES_DSN: 管理员数据库连接字符串
    OM_PG_SCHEMA: OpenMemory schema 名（默认 'openmemory'，禁止为 public）

    服务账号密码（unified-stack 模式需全部设置，logbook-only 模式全部不设置）:
    - LOGBOOK_MIGRATOR_PASSWORD
    - LOGBOOK_SVC_PASSWORD
    - OPENMEMORY_MIGRATOR_PASSWORD
    - OPENMEMORY_SVC_PASSWORD

部署模式:
    - logbook-only: 无任何密码环境变量 → 跳过服务账号创建，使用 postgres 超级用户
    - unified-stack: 全部 4 个密码都设置 → 创建所有服务账号
    - 部分设置 → 配置错误（需要全部设置或全部不设置）

错误码:
    BOOTSTRAP_CONFIG_MISSING_DSN: 缺少管理员 DSN
    BOOTSTRAP_CONFIG_INVALID_SCHEMA: schema 配置无效
    BOOTSTRAP_CONFIG_MISSING_PASSWORD: 缺少角色密码
    BOOTSTRAP_CONFIG_PARTIAL_PASSWORD: 部分密码未设置（配置错误）
    BOOTSTRAP_PRECHECK_SCHEMA_PUBLIC: om_schema 不允许为 public
    BOOTSTRAP_PRECHECK_NO_CREATEROLE: 缺少 CREATEROLE 权限
    BOOTSTRAP_ROLE_CREATION_MISSING_PASSWORD: 角色创建缺少密码
    BOOTSTRAP_ROLE_CREATION_FAILED: 角色创建失败
    BOOTSTRAP_SKIP_MODE_ACTIVE: logbook-only 模式，跳过服务账号创建
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import TYPE_CHECKING, TypedDict
from urllib.parse import urlparse, urlunparse

if TYPE_CHECKING:
    from psycopg import Connection


class DeploymentModeResult(TypedDict):
    """detect_deployment_mode() 返回类型"""

    mode: str  # "logbook-only" | "unified-stack" | "invalid"
    skip_roles: bool
    set_passwords: list[str]
    missing_passwords: list[str]
    code: str
    message: str


class SchemaCheckResult(TypedDict, total=False):
    """check_om_schema_not_public() 返回类型"""

    ok: bool
    code: str
    value: str
    message: str
    remediation: str


class AdminCheckResult(TypedDict, total=False):
    """check_admin_privileges() 返回类型"""

    ok: bool
    code: str
    message: str
    details: dict[str, bool]
    skipped: bool


class PrecheckResult(TypedDict):
    """run_precheck() 返回类型"""

    ok: bool
    checks: dict[str, SchemaCheckResult | AdminCheckResult]
    failed_codes: list[str]


class RoleCreationResult(TypedDict, total=False):
    """create_or_update_login_role() 返回类型"""

    ok: bool
    code: str
    message: str
    remediation: str
    created: bool
    updated: bool


class AllRolesResult(TypedDict):
    """create_all_login_roles() 返回类型"""

    ok: bool
    failed_codes: list[str]
    remediation: str


class BootstrapErrorCode:
    """Bootstrap 错误码常量"""

    CONFIG_MISSING_DSN = "BOOTSTRAP_CONFIG_MISSING_DSN"
    CONFIG_INVALID_SCHEMA = "BOOTSTRAP_CONFIG_INVALID_SCHEMA"
    CONFIG_MISSING_PASSWORD = "BOOTSTRAP_CONFIG_MISSING_PASSWORD"
    CONFIG_PARTIAL_PASSWORD = "BOOTSTRAP_CONFIG_PARTIAL_PASSWORD"
    PRECHECK_SCHEMA_PUBLIC = "BOOTSTRAP_PRECHECK_SCHEMA_PUBLIC"
    PRECHECK_NO_CREATEROLE = "BOOTSTRAP_PRECHECK_NO_CREATEROLE"
    ROLE_CREATION_MISSING_PASSWORD = "BOOTSTRAP_ROLE_CREATION_MISSING_PASSWORD"
    ROLE_CREATION_FAILED = "BOOTSTRAP_ROLE_CREATION_FAILED"
    SKIP_MODE_ACTIVE = "BOOTSTRAP_SKIP_MODE_ACTIVE"


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
    BootstrapErrorCode.CONFIG_PARTIAL_PASSWORD: "unified-stack 模式要求设置全部 4 个密码环境变量，或全部不设置（logbook-only 模式）",
    BootstrapErrorCode.PRECHECK_SCHEMA_PUBLIC: "请设置 OM_PG_SCHEMA 为非 public 的 schema",
    BootstrapErrorCode.PRECHECK_NO_CREATEROLE: "请使用具备 CREATEROL 或 SUPERUSER 权限的账号",
    BootstrapErrorCode.ROLE_CREATION_MISSING_PASSWORD: "请提供登录角色密码（环境变量或参数）",
    BootstrapErrorCode.SKIP_MODE_ACTIVE: "logbook-only 模式：跳过服务账号创建，使用 postgres 超级用户",
}

# 服务账号密码环境变量列表
SERVICE_ACCOUNT_PASSWORD_ENVS = [
    ENV_LOGBOOK_MIGRATOR_PASSWORD,
    ENV_LOGBOOK_SVC_PASSWORD,
    ENV_OPENMEMORY_MIGRATOR_PASSWORD,
    ENV_OPENMEMORY_SVC_PASSWORD,
]


def detect_deployment_mode() -> DeploymentModeResult:
    """
    检测部署模式：logbook-only vs unified-stack

    策略：
    - 无任何密码环境变量 → logbook-only 模式（SKIP）
    - 全部 4 个密码都设置 → unified-stack 模式
    - 部分设置 → 配置错误（需要全部设置或全部不设置）

    返回:
        DeploymentModeResult TypedDict
    """
    set_passwords: list[str] = []
    missing_passwords: list[str] = []

    for env_var in SERVICE_ACCOUNT_PASSWORD_ENVS:
        value = os.environ.get(env_var, "").strip()
        if value:
            set_passwords.append(env_var)
        else:
            missing_passwords.append(env_var)

    total = len(SERVICE_ACCOUNT_PASSWORD_ENVS)
    set_count = len(set_passwords)

    if set_count == 0:
        # logbook-only 模式：不创建服务账号
        return DeploymentModeResult(
            mode="logbook-only",
            skip_roles=True,
            set_passwords=set_passwords,
            missing_passwords=missing_passwords,
            code=BootstrapErrorCode.SKIP_MODE_ACTIVE,
            message="logbook-only 模式：未设置任何服务账号密码，跳过 login role 创建",
        )
    elif set_count == total:
        # unified-stack 模式：创建所有服务账号
        return DeploymentModeResult(
            mode="unified-stack",
            skip_roles=False,
            set_passwords=set_passwords,
            missing_passwords=missing_passwords,
            code="",
            message=f"unified-stack 模式：已设置全部 {total} 个服务账号密码",
        )
    else:
        # 部分设置：配置错误
        return DeploymentModeResult(
            mode="invalid",
            skip_roles=False,
            set_passwords=set_passwords,
            missing_passwords=missing_passwords,
            code=BootstrapErrorCode.CONFIG_PARTIAL_PASSWORD,
            message=(
                f"配置错误：已设置 {set_count}/{total} 个密码。"
                f"unified-stack 模式要求全部设置，logbook-only 模式要求全部不设置。"
                f"\n缺失: {', '.join(missing_passwords)}"
            ),
        )


def check_om_schema_not_public(om_schema: str | None) -> SchemaCheckResult:
    """检查 OpenMemory schema 不为 public"""
    # 确保 om_schema 为 str
    if om_schema is None:
        om_schema = ""
    elif not isinstance(om_schema, str):
        om_schema = str(om_schema)

    if om_schema.lower() == "public":
        return SchemaCheckResult(
            ok=False,
            code=BootstrapErrorCode.PRECHECK_SCHEMA_PUBLIC,
            value=om_schema,
            message="om_schema 不允许为 public",
            remediation="请设置 OM_PG_SCHEMA 为非 public 的 schema",
        )
    return SchemaCheckResult(ok=True, code="", value=om_schema, message="ok")


def check_admin_privileges(conn: Connection[tuple[object, ...]]) -> AdminCheckResult:
    """检查当前用户是否有足够权限创建角色"""
    with conn.cursor() as cur:
        cur.execute("SELECT rolsuper, rolcreaterole FROM pg_roles WHERE rolname = current_user")
        row = cur.fetchone()
    is_superuser = bool(row[0]) if row else False
    can_create_role = bool(row[1]) if row else False
    ok = is_superuser or can_create_role
    return AdminCheckResult(
        ok=ok,
        code="" if ok else BootstrapErrorCode.PRECHECK_NO_CREATEROLE,
        message="ok" if ok else "缺少 createrole 权限",
        details={"is_superuser": is_superuser, "can_create_role": can_create_role},
    )


def run_precheck(
    *,
    admin_dsn: str | None,
    om_schema: str,
    quiet: bool = False,
    skip_db_check: bool = False,
) -> PrecheckResult:
    """执行预检"""
    checks: dict[str, SchemaCheckResult | AdminCheckResult] = {}
    failed_codes: list[str] = []

    schema_check = check_om_schema_not_public(om_schema)
    checks["om_schema_not_public"] = schema_check
    if not schema_check.get("ok"):
        code = schema_check.get("code", "")
        if isinstance(code, str) and code:
            failed_codes.append(code)

    if skip_db_check or not admin_dsn:
        checks["admin_privileges"] = AdminCheckResult(ok=True, skipped=True)
    else:
        import psycopg

        try:
            conn = psycopg.connect(admin_dsn, autocommit=True)
            try:
                admin_check = check_admin_privileges(conn)
            finally:
                conn.close()
        except Exception as exc:
            admin_check = AdminCheckResult(
                ok=False,
                code=BootstrapErrorCode.PRECHECK_NO_CREATEROLE,
                message=f"无法连接数据库或权限不足: {exc}",
            )
        checks["admin_privileges"] = admin_check
        if not admin_check.get("ok"):
            code = admin_check.get("code", BootstrapErrorCode.PRECHECK_NO_CREATEROLE)
            if isinstance(code, str) and code:
                failed_codes.append(code)

    return PrecheckResult(
        ok=len(failed_codes) == 0,
        checks=checks,
        failed_codes=failed_codes,
    )


def create_or_update_login_role(
    conn: Connection[tuple[object, ...]],
    *,
    role_name: str,
    password: str | None,
    inherit_role: str | None = None,
    quiet: bool = False,
) -> RoleCreationResult:
    """创建或更新登录角色"""
    from psycopg import sql

    if not password:
        return RoleCreationResult(
            ok=False,
            code=BootstrapErrorCode.ROLE_CREATION_MISSING_PASSWORD,
            message="缺少密码",
            remediation=f"请设置 {role_name} 的密码",
        )
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,))
            exists = cur.fetchone() is not None
            if not exists:
                cur.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(role_name), sql.Literal(password)
                    )
                )
                created = True
                updated = False
            else:
                cur.execute(
                    sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                        sql.Identifier(role_name), sql.Literal(password)
                    )
                )
                created = False
                updated = True
            if inherit_role:
                try:
                    cur.execute(
                        sql.SQL("GRANT {} TO {}").format(
                            sql.Identifier(inherit_role), sql.Identifier(role_name)
                        )
                    )
                except Exception:
                    pass
        return RoleCreationResult(
            ok=True,
            created=created,
            updated=updated,
        )
    except Exception as exc:
        return RoleCreationResult(
            ok=False,
            code=BootstrapErrorCode.ROLE_CREATION_FAILED,
            message=str(exc),
            remediation="请检查数据库权限与角色状态",
        )


def create_all_login_roles(
    conn: Connection[tuple[object, ...]],
    *,
    passwords: dict[str, str],
    quiet: bool = False,
) -> AllRolesResult:
    """创建所有登录角色"""
    failed_codes: list[str] = []
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
            code = result.get("code", "")
            if isinstance(code, str) and code:
                failed_codes.append(code)
    return AllRolesResult(
        ok=len(failed_codes) == 0,
        failed_codes=failed_codes,
        remediation="请提供缺失的角色密码" if failed_codes else "",
    )


def parse_db_from_dsn(dsn: str) -> str | None:
    """从 DSN 解析数据库名"""
    if not dsn:
        return None
    parsed = urlparse(dsn)
    path = parsed.path.lstrip("/")
    return path or None


def mask_password_in_dsn(dsn: str) -> str:
    """在 DSN 中隐藏密码"""
    if not dsn:
        return dsn
    parsed = urlparse(dsn)
    if parsed.password:
        netloc = parsed.netloc.replace(parsed.password, "******")
        return urlunparse(parsed._replace(netloc=netloc))
    return dsn


def main() -> None:
    """CLI 主入口"""
    parser = argparse.ArgumentParser(
        description="Engram DB bootstrap：创建服务账号并做预检",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--dsn",
        dest="admin_dsn",
        default=None,
        help="管理员 DSN（优先级高于环境变量 ENGRAM_PG_ADMIN_DSN/POSTGRES_DSN）",
    )
    parser.add_argument(
        "--om-schema",
        default=os.environ.get("OM_PG_SCHEMA", DEFAULT_OM_SCHEMA),
        help="OpenMemory schema 名（禁止为 public）",
    )
    parser.add_argument(
        "--skip-db-check",
        action="store_true",
        default=False,
        help="跳过数据库连通性/权限预检（仅做 schema 校验）",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        default=False,
        help="静默模式（仅输出关键结果）",
    )
    parser.add_argument(
        "--require-roles",
        action="store_true",
        default=False,
        help="强制要求创建服务账号（unified-stack 模式），若密码未设置则报错退出",
    )
    args = parser.parse_args()

    admin_dsn = (
        args.admin_dsn or os.environ.get("ENGRAM_PG_ADMIN_DSN") or os.environ.get("POSTGRES_DSN")
    )
    if not admin_dsn:
        print("[ERROR] 缺少管理员 DSN，请设置 --dsn 或 ENGRAM_PG_ADMIN_DSN", file=sys.stderr)
        sys.exit(1)

    # 检测部署模式
    mode_info = detect_deployment_mode()

    if not args.quiet:
        print(f"[INFO] 部署模式检测: {mode_info['mode']}")
        print(f"       {mode_info['message']}")

    # 检查配置有效性
    if mode_info["mode"] == "invalid":
        print(f"[ERROR] {mode_info['message']}", file=sys.stderr)
        print(
            f"[ERROR] 修复方法: {REMEDIATION_COMMANDS.get(mode_info['code'], '')}", file=sys.stderr
        )
        sys.exit(1)

    # unified-stack 模式检查（--require-roles 参数）
    if args.require_roles and mode_info["skip_roles"]:
        print("[ERROR] --require-roles 指定但未设置服务账号密码", file=sys.stderr)
        print(
            f"       请设置以下环境变量: {', '.join(mode_info['missing_passwords'])}",
            file=sys.stderr,
        )
        sys.exit(1)

    precheck = run_precheck(
        admin_dsn=admin_dsn,
        om_schema=args.om_schema,
        quiet=args.quiet,
        skip_db_check=args.skip_db_check,
    )
    if not precheck.get("ok"):
        print("[ERROR] 预检失败", file=sys.stderr)
        if not args.quiet:
            print(precheck, file=sys.stderr)
        sys.exit(1)

    # logbook-only 模式：跳过服务账号创建
    if mode_info["skip_roles"]:
        if not args.quiet:
            masked = mask_password_in_dsn(admin_dsn)
            print("[SKIP] 服务账号创建被跳过 (logbook-only 模式)")
            print("       将使用 postgres 超级用户进行后续操作")
            print(f"       DSN: {masked}")
        sys.exit(0)

    # unified-stack 模式：创建服务账号
    try:
        import psycopg
    except Exception as exc:
        print(f"[ERROR] 缺少 psycopg 依赖: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        conn = psycopg.connect(admin_dsn, autocommit=True)
        try:
            result = create_all_login_roles(conn, passwords={}, quiet=args.quiet)
        finally:
            conn.close()
    except Exception as exc:
        print(f"[ERROR] 连接数据库失败: {exc}", file=sys.stderr)
        sys.exit(1)

    if not result.get("ok"):
        print("[ERROR] 服务账号创建失败", file=sys.stderr)
        if not args.quiet:
            print(result, file=sys.stderr)
        sys.exit(1)

    if not args.quiet:
        masked = mask_password_in_dsn(admin_dsn)
        print("[OK] 服务账号已就绪 (unified-stack 模式)")
        print(f"     DSN: {masked}")


if __name__ == "__main__":
    main()
