#!/usr/bin/env python3
"""
identity_sync.py - 身份信息同步脚本

功能:
- 扫描 ./.agentx/users/*.yaml 用户配置文件
- 合并 ~/.agentx/user.config.yaml（若存在）
- 将用户信息写入 identity.users 表
- 将账户信息写入 identity.accounts 表
- 读取角色配置文件并写入 identity.role_profiles 表
- 使用 UPSERT 保证幂等

使用:
    python identity_sync.py [--config PATH] [--repo-root PATH] [--verbose] [--quiet]
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import psycopg
import yaml

from engram_step1.config import Config, add_config_argument, get_config
from engram_step1.db import get_connection
from engram_step1.errors import (
    DatabaseError,
    EngramError,
    ExitCode,
    ValidationError,
    make_success_result,
    make_error_result,
)
from engram_step1.hashing import hash_string
from engram_step1.io import (
    add_output_arguments,
    get_output_options,
    log_info,
    log_warning,
    log_debug,
    log_error,
    output_json,
)

# 支持的账户类型
ACCOUNT_TYPES = ("svn", "gitlab", "git", "email")


@dataclass
class UserConfig:
    """用户配置数据结构"""

    user_id: str
    display_name: str
    is_active: bool = True
    roles: List[str] = field(default_factory=list)
    aliases: List[str] = field(default_factory=list)
    accounts: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    visibility_default: str = "team"
    source_path: Optional[str] = None


@dataclass
class RoleProfile:
    """角色配置数据结构"""

    user_id: str
    profile_md: str
    profile_sha: str
    source_path: str


@dataclass
class SyncStats:
    """同步统计"""

    users_inserted: int = 0
    users_updated: int = 0
    accounts_inserted: int = 0
    accounts_updated: int = 0
    role_profiles_inserted: int = 0
    role_profiles_updated: int = 0

    def to_dict(self) -> Dict[str, int]:
        return {
            "users_inserted": self.users_inserted,
            "users_updated": self.users_updated,
            "accounts_inserted": self.accounts_inserted,
            "accounts_updated": self.accounts_updated,
            "role_profiles_inserted": self.role_profiles_inserted,
            "role_profiles_updated": self.role_profiles_updated,
        }

    def summary(self) -> str:
        return (
            f"用户: +{self.users_inserted} ~{self.users_updated}, "
            f"账户: +{self.accounts_inserted} ~{self.accounts_updated}, "
            f"角色配置: +{self.role_profiles_inserted} ~{self.role_profiles_updated}"
        )


class IdentitySyncError(EngramError):
    """身份同步错误"""

    exit_code = ExitCode.IDENTITY_SYNC_ERROR
    error_type = "IDENTITY_SYNC_ERROR"


def load_yaml_file(path: Path, quiet: bool = False) -> Dict[str, Any]:
    """
    加载 YAML 文件

    Args:
        path: 文件路径
        quiet: 静默模式

    Returns:
        解析后的字典
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = yaml.safe_load(f)
            return content if content else {}
    except yaml.YAMLError as e:
        raise IdentitySyncError(
            f"YAML 解析失败: {path}",
            {"path": str(path), "error": str(e)},
        )
    except IOError as e:
        raise IdentitySyncError(
            f"读取文件失败: {path}",
            {"path": str(path), "error": str(e)},
        )


def parse_user_config(data: Dict[str, Any], source_path: Optional[str] = None) -> UserConfig:
    """
    解析用户配置数据

    Args:
        data: YAML 解析后的字典
        source_path: 来源文件路径

    Returns:
        UserConfig 实例
    """
    user_id = data.get("user_id")
    if not user_id:
        raise ValidationError(
            "用户配置缺少 user_id",
            {"data": data, "source": source_path},
        )

    display_name = data.get("display_name", user_id)

    # 解析 accounts
    accounts_raw = data.get("accounts", {})
    accounts = {}
    for account_type, account_info in accounts_raw.items():
        if account_type in ACCOUNT_TYPES:
            if isinstance(account_info, dict):
                accounts[account_type] = account_info
            elif isinstance(account_info, str):
                # 支持简写形式: svn: username
                accounts[account_type] = {"username": account_info}

    return UserConfig(
        user_id=user_id,
        display_name=display_name,
        is_active=data.get("is_active", True),
        roles=data.get("roles", []),
        aliases=data.get("aliases", []),
        accounts=accounts,
        visibility_default=data.get("visibility_default", "team"),
        source_path=source_path,
    )


def merge_user_configs(base: UserConfig, overlay: UserConfig) -> UserConfig:
    """
    合并用户配置（overlay 覆盖 base）

    Args:
        base: 基础配置
        overlay: 覆盖配置

    Returns:
        合并后的 UserConfig
    """
    # 合并 roles（去重）
    merged_roles = list(set(base.roles + overlay.roles))

    # 合并 aliases（去重）
    merged_aliases = list(set(base.aliases + overlay.aliases))

    # 合并 accounts（overlay 优先）
    merged_accounts = {**base.accounts}
    for account_type, account_info in overlay.accounts.items():
        if account_type in merged_accounts:
            merged_accounts[account_type] = {
                **merged_accounts[account_type],
                **account_info,
            }
        else:
            merged_accounts[account_type] = account_info

    return UserConfig(
        user_id=base.user_id,
        display_name=overlay.display_name or base.display_name,
        is_active=overlay.is_active if hasattr(overlay, "is_active") else base.is_active,
        roles=merged_roles,
        aliases=merged_aliases,
        accounts=merged_accounts,
        visibility_default=overlay.visibility_default or base.visibility_default,
        source_path=base.source_path,  # 保留原始来源
    )


class AgentXDirectoryNotFoundError(IdentitySyncError):
    """AgentX 目录不存在错误"""

    exit_code = ExitCode.VALIDATION_ERROR
    error_type = "AGENTX_DIR_NOT_FOUND"


def scan_user_configs(
    repo_root: Path,
    quiet: bool = False,
    verbose: bool = False,
    strict: bool = False,
) -> Dict[str, UserConfig]:
    """
    扫描仓库中的用户配置文件

    Args:
        repo_root: 仓库根目录
        quiet: 静默模式
        verbose: 详细模式
        strict: 严格模式，目录不存在时抛出错误

    Returns:
        user_id -> UserConfig 的映射
    """
    users_dir = repo_root / ".agentx" / "users"
    configs: Dict[str, UserConfig] = {}

    if not users_dir.exists():
        hint_msg = (
            f"用户配置目录不存在: {users_dir}\n"
            f"请按以下步骤创建:\n"
            f"  1. mkdir -p {users_dir}\n"
            f"  2. 创建用户配置文件，例如:\n"
            f"     cat > {users_dir}/example_user.yaml << 'EOF'\n"
            f"     user_id: example_user\n"
            f"     display_name: Example User\n"
            f"     accounts:\n"
            f"       svn:\n"
            f"         username: example_svn\n"
            f"       gitlab:\n"
            f"         username: example_gitlab\n"
            f"         email: example@company.com\n"
            f"     aliases: [\"别名1\"]\n"
            f"     roles: [\"dev\"]\n"
            f"     EOF\n"
            f"  3. 重新运行 identity sync"
        )
        if strict:
            raise AgentXDirectoryNotFoundError(
                hint_msg,
                {"path": str(users_dir), "repo_root": str(repo_root)},
            )
        log_warning(hint_msg, quiet=quiet)
        return configs

    for yaml_file in users_dir.glob("*.yaml"):
        try:
            data = load_yaml_file(yaml_file, quiet=quiet)
            user_config = parse_user_config(data, str(yaml_file))
            configs[user_config.user_id] = user_config
            log_debug(f"加载用户配置: {user_config.user_id} <- {yaml_file}", verbose=verbose)
        except EngramError as e:
            log_warning(f"跳过无效配置文件 {yaml_file}: {e.message}", quiet=quiet)

    # 同时支持 .yml 扩展名
    for yaml_file in users_dir.glob("*.yml"):
        try:
            data = load_yaml_file(yaml_file, quiet=quiet)
            user_config = parse_user_config(data, str(yaml_file))
            if user_config.user_id not in configs:
                configs[user_config.user_id] = user_config
                log_debug(f"加载用户配置: {user_config.user_id} <- {yaml_file}", verbose=verbose)
        except EngramError as e:
            log_warning(f"跳过无效配置文件 {yaml_file}: {e.message}", quiet=quiet)

    log_info(f"从 {users_dir} 扫描到 {len(configs)} 个用户配置", quiet=quiet)
    return configs


def load_home_user_config(quiet: bool = False, verbose: bool = False) -> Optional[UserConfig]:
    """
    加载用户主目录下的配置文件

    Args:
        quiet: 静默模式
        verbose: 详细模式

    Returns:
        UserConfig 或 None（如果不存在）
    """
    home_config = Path.home() / ".agentx" / "user.config.yaml"
    if not home_config.exists():
        # 尝试 .yml 扩展名
        home_config = Path.home() / ".agentx" / "user.config.yml"
        if not home_config.exists():
            log_debug("用户主目录配置不存在: ~/.agentx/user.config.yaml", verbose=verbose)
            return None

    try:
        data = load_yaml_file(home_config, quiet=quiet)
        user_config = parse_user_config(data, str(home_config))
        log_info(f"加载用户主目录配置: {user_config.user_id}", quiet=quiet)
        return user_config
    except EngramError as e:
        log_warning(f"解析用户主目录配置失败: {e.message}", quiet=quiet)
        return None


def scan_role_profiles(
    repo_root: Path,
    quiet: bool = False,
    verbose: bool = False,
    strict: bool = False,
) -> Dict[str, RoleProfile]:
    """
    扫描角色配置文件

    支持两种结构:
    1. .agentx/roles/<user_id>.md (单文件)
    2. .agentx/roles/<user_id>/profile.md (目录)
    3. .agentx/roles/<user_id>/**/*.md (目录下所有 md 合并)

    Args:
        repo_root: 仓库根目录
        quiet: 静默模式
        verbose: 详细模式
        strict: 严格模式，目录不存在时抛出错误

    Returns:
        user_id -> RoleProfile 的映射
    """
    roles_dir = repo_root / ".agentx" / "roles"
    profiles: Dict[str, RoleProfile] = {}

    if not roles_dir.exists():
        hint_msg = (
            f"角色配置目录不存在: {roles_dir}\n"
            f"角色配置是可选的。如需创建，请:\n"
            f"  1. mkdir -p {roles_dir}\n"
            f"  2. 为用户创建角色配置（两种方式任选）:\n"
            f"     方式A - 单文件: {roles_dir}/<user_id>.md\n"
            f"     方式B - 目录:   {roles_dir}/<user_id>/profile.md\n"
            f"  3. 在 markdown 文件中描述用户的角色职责和上下文"
        )
        if strict:
            raise AgentXDirectoryNotFoundError(
                hint_msg,
                {"path": str(roles_dir), "repo_root": str(repo_root)},
            )
        log_debug(hint_msg, verbose=verbose)
        return profiles

    # 扫描单文件形式: <user_id>.md
    for md_file in roles_dir.glob("*.md"):
        user_id = md_file.stem
        try:
            content = md_file.read_text(encoding="utf-8")
            sha = hash_string(content)
            profiles[user_id] = RoleProfile(
                user_id=user_id,
                profile_md=content,
                profile_sha=sha,
                source_path=str(md_file),
            )
            log_debug(f"加载角色配置: {user_id} <- {md_file}", verbose=verbose)
        except IOError as e:
            log_warning(f"读取角色配置失败 {md_file}: {e}", quiet=quiet)

    # 扫描目录形式: <user_id>/
    for user_dir in roles_dir.iterdir():
        if not user_dir.is_dir():
            continue

        user_id = user_dir.name
        if user_id in profiles:
            # 单文件优先，跳过目录
            continue

        # 查找目录下的 markdown 文件
        md_files = sorted(user_dir.glob("**/*.md"))
        if not md_files:
            continue

        # 合并所有 markdown 内容
        contents = []
        source_paths = []
        for md_file in md_files:
            try:
                content = md_file.read_text(encoding="utf-8")
                contents.append(f"<!-- source: {md_file.name} -->\n{content}")
                source_paths.append(str(md_file))
            except IOError as e:
                log_warning(f"读取角色配置失败 {md_file}: {e}", quiet=quiet)

        if contents:
            merged_content = "\n\n---\n\n".join(contents)
            sha = hash_string(merged_content)
            profiles[user_id] = RoleProfile(
                user_id=user_id,
                profile_md=merged_content,
                profile_sha=sha,
                source_path=str(user_dir),
            )
            log_debug(f"加载角色配置: {user_id} <- {user_dir} ({len(md_files)} 文件)", verbose=verbose)

    log_info(f"从 {roles_dir} 扫描到 {len(profiles)} 个角色配置", quiet=quiet)
    return profiles


def upsert_user(
    conn: psycopg.Connection,
    user: UserConfig,
) -> tuple[bool, bool]:
    """
    UPSERT 用户记录

    Args:
        conn: 数据库连接
        user: 用户配置

    Returns:
        (is_inserted, is_updated) 元组
    """
    roles_json = json.dumps(user.roles)

    with conn.cursor() as cur:
        # 先检查是否存在
        cur.execute(
            "SELECT 1 FROM identity.users WHERE user_id = %s",
            (user.user_id,),
        )
        exists = cur.fetchone() is not None

        cur.execute(
            """
            INSERT INTO identity.users (user_id, display_name, is_active, roles_json, updated_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (user_id) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                is_active = EXCLUDED.is_active,
                roles_json = EXCLUDED.roles_json,
                updated_at = now()
            """,
            (user.user_id, user.display_name, user.is_active, roles_json),
        )

        return (not exists, exists)


def upsert_account(
    conn: psycopg.Connection,
    user_id: str,
    account_type: str,
    account_info: Dict[str, Any],
    aliases: List[str],
    quiet: bool = False,
) -> tuple[bool, bool]:
    """
    UPSERT 账户记录

    Args:
        conn: 数据库连接
        user_id: 用户 ID
        account_type: 账户类型
        account_info: 账户信息
        aliases: 别名列表
        quiet: 静默模式

    Returns:
        (is_inserted, is_updated) 元组
    """
    account_name = account_info.get("username", "")
    email = account_info.get("email")
    verified = account_info.get("verified", False)
    aliases_json = json.dumps(aliases)

    if not account_name:
        log_warning(f"跳过无效账户: user_id={user_id}, type={account_type} (缺少 username)", quiet=quiet)
        return (False, False)

    with conn.cursor() as cur:
        # 先检查是否存在
        cur.execute(
            "SELECT 1 FROM identity.accounts WHERE account_type = %s AND account_name = %s",
            (account_type, account_name),
        )
        exists = cur.fetchone() is not None

        cur.execute(
            """
            INSERT INTO identity.accounts
                (user_id, account_type, account_name, email, aliases_json, verified, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (account_type, account_name) DO UPDATE SET
                user_id = EXCLUDED.user_id,
                email = EXCLUDED.email,
                aliases_json = EXCLUDED.aliases_json,
                verified = EXCLUDED.verified,
                updated_at = now()
            """,
            (user_id, account_type, account_name, email, aliases_json, verified),
        )

        return (not exists, exists)


def upsert_role_profile(
    conn: psycopg.Connection,
    profile: RoleProfile,
    verbose: bool = False,
) -> tuple[bool, bool]:
    """
    UPSERT 角色配置记录

    Args:
        conn: 数据库连接
        profile: 角色配置
        verbose: 详细模式

    Returns:
        (is_inserted, is_updated) 元组
    """
    with conn.cursor() as cur:
        # 先检查是否存在
        cur.execute(
            "SELECT profile_sha FROM identity.role_profiles WHERE user_id = %s",
            (profile.user_id,),
        )
        row = cur.fetchone()
        exists = row is not None

        # 如果 SHA 相同，跳过更新
        if exists and row[0] == profile.profile_sha:
            log_debug(f"角色配置未变更，跳过: {profile.user_id}", verbose=verbose)
            return (False, False)

        cur.execute(
            """
            INSERT INTO identity.role_profiles
                (user_id, profile_sha, profile_md, source_path, updated_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (user_id) DO UPDATE SET
                profile_sha = EXCLUDED.profile_sha,
                profile_md = EXCLUDED.profile_md,
                source_path = EXCLUDED.source_path,
                updated_at = now()
            """,
            (profile.user_id, profile.profile_sha, profile.profile_md, profile.source_path),
        )

        return (not exists, exists)


def sync_identities(
    repo_root: Path,
    config: Optional[Config] = None,
    quiet: bool = False,
    verbose: bool = False,
    strict: bool = False,
) -> SyncStats:
    """
    执行身份同步

    Args:
        repo_root: 仓库根目录
        config: Config 实例
        quiet: 静默模式
        verbose: 详细模式
        strict: 严格模式，缺少必要目录时抛出错误

    Returns:
        同步统计
    """
    stats = SyncStats()

    # 1. 扫描用户配置
    user_configs = scan_user_configs(repo_root, quiet=quiet, verbose=verbose, strict=strict)

    # 2. 加载用户主目录配置并合并
    home_config = load_home_user_config(quiet=quiet, verbose=verbose)
    if home_config:
        if home_config.user_id in user_configs:
            # 合并配置
            base = user_configs[home_config.user_id]
            user_configs[home_config.user_id] = merge_user_configs(base, home_config)
            log_info(f"合并用户主目录配置: {home_config.user_id}", quiet=quiet)
        else:
            # 直接添加
            user_configs[home_config.user_id] = home_config

    # 3. 扫描角色配置（角色配置是可选的，不使用 strict 模式）
    role_profiles = scan_role_profiles(repo_root, quiet=quiet, verbose=verbose, strict=False)

    if not user_configs and not role_profiles:
        log_info("未找到任何用户配置或角色配置，跳过同步", quiet=quiet)
        return stats

    # 4. 连接数据库并写入
    conn = get_connection(config=config)
    try:
        # 写入用户
        for user_id, user_config in user_configs.items():
            try:
                inserted, updated = upsert_user(conn, user_config)
                if inserted:
                    stats.users_inserted += 1
                elif updated:
                    stats.users_updated += 1

                # 写入账户
                for account_type, account_info in user_config.accounts.items():
                    try:
                        acc_inserted, acc_updated = upsert_account(
                            conn,
                            user_id,
                            account_type,
                            account_info,
                            user_config.aliases,
                            quiet=quiet,
                        )
                        if acc_inserted:
                            stats.accounts_inserted += 1
                        elif acc_updated:
                            stats.accounts_updated += 1
                    except psycopg.Error as e:
                        log_error(f"写入账户失败: {user_id}/{account_type}: {e}", quiet=quiet)

            except psycopg.Error as e:
                log_error(f"写入用户失败: {user_id}: {e}", quiet=quiet)
                raise DatabaseError(
                    f"写入用户失败: {e}",
                    {"user_id": user_id, "error": str(e)},
                )

        # 写入角色配置（需要先确保用户存在）
        for user_id, profile in role_profiles.items():
            # 检查用户是否存在
            if user_id not in user_configs:
                # 用户不在配置中，检查数据库
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM identity.users WHERE user_id = %s",
                        (user_id,),
                    )
                    if not cur.fetchone():
                        log_warning(f"跳过角色配置，用户不存在: {user_id}", quiet=quiet)
                        continue

            try:
                inserted, updated = upsert_role_profile(conn, profile, verbose=verbose)
                if inserted:
                    stats.role_profiles_inserted += 1
                elif updated:
                    stats.role_profiles_updated += 1
            except psycopg.Error as e:
                log_error(f"写入角色配置失败: {user_id}: {e}", quiet=quiet)

        conn.commit()
        log_info(f"同步完成: {stats.summary()}", quiet=quiet)

    except EngramError:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise IdentitySyncError(
            f"身份同步失败: {e}",
            {"error": str(e)},
        )
    finally:
        conn.close()

    return stats


def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="身份信息同步脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # 使用配置文件同步
    python identity_sync.py --config config.toml

    # 指定仓库根目录
    python identity_sync.py --repo-root /path/to/repo

    # 详细输出
    python identity_sync.py -v

    # 严格模式（目录不存在时报错）
    python identity_sync.py --strict
        """,
    )

    add_config_argument(parser)
    add_output_arguments(parser)

    parser.add_argument(
        "--repo-root",
        type=str,
        default=".",
        help="仓库根目录路径 (默认: 当前目录)",
    )

    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="显示详细输出",
    )

    parser.add_argument(
        "--strict",
        action="store_true",
        help="严格模式: .agentx/users 目录不存在时报错而非警告",
    )

    return parser.parse_args()


def main() -> int:
    """主入口"""
    args = parse_args()

    opts = get_output_options(args)
    quiet = opts["quiet"]
    pretty = opts["pretty"]
    verbose = getattr(args, "verbose", False)

    try:
        # 加载配置
        config = get_config(args.config_path)
        config.load()

        # 解析仓库根目录
        repo_root = Path(args.repo_root).resolve()
        if not repo_root.exists():
            result = make_error_result(
                code="PATH_NOT_FOUND",
                message=f"仓库根目录不存在: {repo_root}",
                detail={"path": str(repo_root)},
            )
            output_json(result, pretty=pretty)
            return ExitCode.VALIDATION_ERROR

        log_info(f"仓库根目录: {repo_root}", quiet=quiet)

        # 获取 strict 模式
        strict = getattr(args, "strict", False)

        # 执行同步
        stats = sync_identities(repo_root, config, quiet=quiet, verbose=verbose, strict=strict)

        result = make_success_result(
            stats=stats.to_dict(),
            summary=stats.summary(),
        )
        output_json(result, pretty=pretty)
        return 0

    except EngramError as e:
        output_json(e.to_dict(), pretty=pretty)
        if not quiet:
            log_error(f"{e.error_type}: {e.message}")
        return e.exit_code

    except Exception as e:
        result = make_error_result(
            code="UNEXPECTED_ERROR",
            message=str(e),
            detail={"error_type": type(e).__name__},
        )
        output_json(result, pretty=pretty)
        if not quiet:
            log_error(f"未预期的错误: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
