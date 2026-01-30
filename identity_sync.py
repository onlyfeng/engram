#!/usr/bin/env python3
"""
identity_sync - 身份配置同步（测试兼容实现）
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml
import psycopg

from engram.logbook.errors import ValidationError


class AgentXDirectoryNotFoundError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class UserConfig:
    user_id: str
    display_name: str
    accounts: Dict[str, Dict[str, str]] = field(default_factory=dict)
    aliases: List[str] = field(default_factory=list)
    roles: List[str] = field(default_factory=list)
    is_active: bool = True
    visibility_default: str = "team"


@dataclass
class RoleProfile:
    user_id: str
    profile_md: str


@dataclass
class SyncStats:
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


def parse_user_config(data: Dict, source: Optional[str] = None) -> UserConfig:
    user_id = data.get("user_id")
    if not user_id:
        raise ValidationError("user_id 缺失", {"source": source})
    display_name = data.get("display_name") or user_id
    accounts_raw = data.get("accounts") or {}
    accounts: Dict[str, Dict[str, str]] = {}
    for k, v in accounts_raw.items():
        if isinstance(v, str):
            accounts[k] = {"username": v}
        else:
            accounts[k] = dict(v)
    return UserConfig(
        user_id=user_id,
        display_name=display_name,
        accounts=accounts,
        aliases=data.get("aliases") or [],
        roles=data.get("roles") or [],
        is_active=data.get("is_active", True),
        visibility_default=data.get("visibility_default") or "team",
    )


def merge_user_configs(base: UserConfig, overlay: UserConfig) -> UserConfig:
    accounts = dict(base.accounts)
    accounts.update(overlay.accounts)
    roles = list(dict.fromkeys(base.roles + overlay.roles))
    aliases = list(dict.fromkeys(base.aliases + overlay.aliases))
    display_name = overlay.display_name or base.display_name
    return UserConfig(
        user_id=base.user_id,
        display_name=display_name,
        accounts=accounts,
        aliases=aliases,
        roles=roles,
        is_active=overlay.is_active if overlay.is_active is not None else base.is_active,
        visibility_default=overlay.visibility_default or base.visibility_default,
    )


def scan_user_configs(
    repo_root: Path, *, quiet: bool = True, strict: bool = False
) -> Dict[str, UserConfig]:
    users_dir = repo_root / ".agentx" / "users"
    if not users_dir.exists():
        if strict:
            raise AgentXDirectoryNotFoundError(
                "用户配置目录不存在，请执行: mkdir -p .agentx/users"
            )
        return {}
    configs: Dict[str, UserConfig] = {}
    for path in users_dir.glob("*.yaml"):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        parsed = parse_user_config(data, str(path))
        if parsed.user_id in configs:
            configs[parsed.user_id] = merge_user_configs(configs[parsed.user_id], parsed)
        else:
            configs[parsed.user_id] = parsed
    return configs


def scan_role_profiles(repo_root: Path, *, quiet: bool = True) -> Dict[str, RoleProfile]:
    roles_dir = repo_root / ".agentx" / "roles"
    if not roles_dir.exists():
        return {}
    profiles: Dict[str, RoleProfile] = {}
    for path in roles_dir.glob("*.md"):
        profiles[path.stem] = RoleProfile(
            user_id=path.stem, profile_md=path.read_text(encoding="utf-8")
        )
    return profiles


def load_home_user_config() -> Optional[UserConfig]:
    home = Path.home()
    path = home / ".agentx" / "user.yaml"
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return parse_user_config(data, str(path))


def sync_identities(
    repo_root: Path,
    *,
    config,
    quiet: bool = True,
    verbose: bool = False,
) -> SyncStats:
    stats = SyncStats()
    dsn = None
    if hasattr(config, "get"):
        dsn = config.get("postgres.dsn")
    if not dsn and hasattr(config, "require"):
        try:
            dsn = config.require("postgres.dsn")
        except Exception:
            dsn = None
    if not dsn:
        dsn = os.environ.get("POSTGRES_DSN") or os.environ.get("TEST_PG_DSN")
    if not dsn:
        raise ValidationError(
            "缺少 POSTGRES_DSN",
            {"hint": "请设置配置项 postgres.dsn 或环境变量 POSTGRES_DSN"},
        )
    users = scan_user_configs(repo_root, quiet=quiet, strict=True)
    profiles = scan_role_profiles(repo_root, quiet=quiet)

    conn = psycopg.connect(dsn, autocommit=False)
    try:
        with conn.cursor() as cur:
            for user in users.values():
                cur.execute("SELECT 1 FROM identity.users WHERE user_id=%s", (user.user_id,))
                exists = cur.fetchone() is not None
                if not exists:
                    cur.execute(
                        """
                        INSERT INTO identity.users (user_id, display_name, is_active, roles_json)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (user.user_id, user.display_name, user.is_active, json.dumps(user.roles)),
                    )
                    stats.users_inserted += 1
                else:
                    cur.execute(
                        """
                        UPDATE identity.users
                        SET display_name=%s, is_active=%s, roles_json=%s, updated_at=now()
                        WHERE user_id=%s
                        """,
                        (user.display_name, user.is_active, json.dumps(user.roles), user.user_id),
                    )
                    stats.users_updated += 1

                for account_type, info in user.accounts.items():
                    account_name = info.get("username") or info.get("email") or ""
                    email = info.get("email")
                    cur.execute(
                        """
                        SELECT 1 FROM identity.accounts
                        WHERE account_type=%s AND account_name=%s
                        """,
                        (account_type, account_name),
                    )
                    acc_exists = cur.fetchone() is not None
                    if not acc_exists:
                        cur.execute(
                            """
                            INSERT INTO identity.accounts (user_id, account_type, account_name, email, aliases_json)
                            VALUES (%s, %s, %s, %s, %s)
                            """,
                            (user.user_id, account_type, account_name, email, json.dumps(user.aliases)),
                        )
                        stats.accounts_inserted += 1
                    else:
                        cur.execute(
                            """
                            UPDATE identity.accounts
                            SET user_id=%s, email=%s, aliases_json=%s, updated_at=now()
                            WHERE account_type=%s AND account_name=%s
                            """,
                            (user.user_id, email, json.dumps(user.aliases), account_type, account_name),
                        )
                        stats.accounts_updated += 1

            for profile in profiles.values():
                profile_sha = hashlib.sha256(profile.profile_md.encode("utf-8")).hexdigest()
                cur.execute(
                    "SELECT 1 FROM identity.role_profiles WHERE user_id=%s",
                    (profile.user_id,),
                )
                exists = cur.fetchone() is not None
                if not exists:
                    cur.execute(
                        """
                        INSERT INTO identity.role_profiles (user_id, profile_sha, profile_md)
                        VALUES (%s, %s, %s)
                        """,
                        (profile.user_id, profile_sha, profile.profile_md),
                    )
                    stats.role_profiles_inserted += 1
                else:
                    cur.execute(
                        """
                        UPDATE identity.role_profiles
                        SET profile_sha=%s, profile_md=%s, updated_at=now()
                        WHERE user_id=%s
                        """,
                        (profile_sha, profile.profile_md, profile.user_id),
                    )
                    stats.role_profiles_updated += 1
        conn.commit()
    finally:
        conn.close()
    return stats
