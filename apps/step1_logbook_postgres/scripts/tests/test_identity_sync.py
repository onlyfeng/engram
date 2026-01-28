# -*- coding: utf-8 -*-
"""
test_identity_sync.py - 身份同步功能测试

测试覆盖:
- 用户配置扫描
- 本地覆盖合并
- 数据库写入
- 幂等性保证
- 目录不存在时的错误处理
"""

import os
import sys
import tempfile
from pathlib import Path
from typing import Generator

import pytest
import yaml

# 确保可以导入 scripts 目录下的模块
scripts_dir = Path(__file__).parent.parent
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))

from identity_sync import (
    UserConfig,
    RoleProfile,
    SyncStats,
    scan_user_configs,
    scan_role_profiles,
    load_home_user_config,
    merge_user_configs,
    parse_user_config,
    sync_identities,
    AgentXDirectoryNotFoundError,
)


# ---------- Fixtures ----------


@pytest.fixture
def temp_repo() -> Generator[Path, None, None]:
    """创建临时仓库目录结构"""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        yield repo_root


@pytest.fixture
def temp_repo_with_users(temp_repo: Path) -> Path:
    """创建带有用户配置的临时仓库"""
    users_dir = temp_repo / ".agentx" / "users"
    users_dir.mkdir(parents=True)

    # 创建测试用户配置
    user1_config = {
        "user_id": "test_user_1",
        "display_name": "Test User 1",
        "accounts": {
            "svn": {"username": "test_svn_1"},
            "gitlab": {"username": "test_gitlab_1", "email": "test1@example.com"},
        },
        "aliases": ["alias1", "别名1"],
        "roles": ["dev", "reviewer"],
        "visibility_default": "team",
    }

    user2_config = {
        "user_id": "test_user_2",
        "display_name": "Test User 2",
        "accounts": {
            "svn": {"username": "test_svn_2"},
        },
        "aliases": [],
        "roles": ["dev"],
    }

    with open(users_dir / "user1.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(user1_config, f, allow_unicode=True)

    with open(users_dir / "user2.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(user2_config, f, allow_unicode=True)

    return temp_repo


@pytest.fixture
def temp_repo_with_roles(temp_repo_with_users: Path) -> Path:
    """创建带有角色配置的临时仓库"""
    roles_dir = temp_repo_with_users / ".agentx" / "roles"
    roles_dir.mkdir(parents=True)

    # 创建角色配置（单文件形式）
    role_content = """# Test User 1 角色配置

## 职责
- 代码开发
- Code Review

## 擅长领域
- Python
- PostgreSQL
"""
    with open(roles_dir / "test_user_1.md", "w", encoding="utf-8") as f:
        f.write(role_content)

    return temp_repo_with_users


# ---------- 单元测试：配置解析 ----------


class TestParseUserConfig:
    """用户配置解析测试"""

    def test_parse_basic_config(self):
        """测试基本配置解析"""
        data = {
            "user_id": "test_user",
            "display_name": "Test User",
            "accounts": {
                "svn": {"username": "test_svn"},
            },
        }
        config = parse_user_config(data, "test.yaml")

        assert config.user_id == "test_user"
        assert config.display_name == "Test User"
        assert "svn" in config.accounts
        assert config.accounts["svn"]["username"] == "test_svn"
        assert config.is_active is True
        assert config.visibility_default == "team"

    def test_parse_shorthand_account(self):
        """测试简写形式的账户配置"""
        data = {
            "user_id": "test_user",
            "display_name": "Test",
            "accounts": {
                "svn": "test_svn_username",  # 简写形式
            },
        }
        config = parse_user_config(data)

        assert config.accounts["svn"]["username"] == "test_svn_username"

    def test_parse_missing_user_id(self):
        """测试缺少 user_id 时抛出错误"""
        data = {
            "display_name": "Test User",
        }
        from engram_step1.errors import ValidationError

        with pytest.raises(ValidationError):
            parse_user_config(data)

    def test_parse_display_name_defaults_to_user_id(self):
        """测试 display_name 默认为 user_id"""
        data = {
            "user_id": "test_user",
        }
        config = parse_user_config(data)
        assert config.display_name == "test_user"


# ---------- 单元测试：配置合并 ----------


class TestMergeUserConfigs:
    """用户配置合并测试"""

    def test_merge_display_name(self):
        """测试 display_name 覆盖"""
        base = UserConfig(
            user_id="test",
            display_name="Original Name",
        )
        overlay = UserConfig(
            user_id="test",
            display_name="New Name",
        )
        merged = merge_user_configs(base, overlay)

        assert merged.display_name == "New Name"
        assert merged.user_id == "test"

    def test_merge_accounts(self):
        """测试 accounts 合并"""
        base = UserConfig(
            user_id="test",
            display_name="Test",
            accounts={
                "svn": {"username": "base_svn"},
                "gitlab": {"username": "base_gitlab"},
            },
        )
        overlay = UserConfig(
            user_id="test",
            display_name="Test",
            accounts={
                "gitlab": {"username": "overlay_gitlab", "email": "new@example.com"},
            },
        )
        merged = merge_user_configs(base, overlay)

        # SVN 保持不变
        assert merged.accounts["svn"]["username"] == "base_svn"
        # GitLab 被覆盖
        assert merged.accounts["gitlab"]["username"] == "overlay_gitlab"
        assert merged.accounts["gitlab"]["email"] == "new@example.com"

    def test_merge_roles_dedup(self):
        """测试 roles 合并去重"""
        base = UserConfig(
            user_id="test",
            display_name="Test",
            roles=["dev", "reviewer"],
        )
        overlay = UserConfig(
            user_id="test",
            display_name="Test",
            roles=["reviewer", "admin"],
        )
        merged = merge_user_configs(base, overlay)

        # 合并且去重
        assert set(merged.roles) == {"dev", "reviewer", "admin"}

    def test_merge_aliases_dedup(self):
        """测试 aliases 合并去重"""
        base = UserConfig(
            user_id="test",
            display_name="Test",
            aliases=["alias1", "别名"],
        )
        overlay = UserConfig(
            user_id="test",
            display_name="Test",
            aliases=["别名", "alias2"],
        )
        merged = merge_user_configs(base, overlay)

        # 合并且去重
        assert set(merged.aliases) == {"alias1", "alias2", "别名"}


# ---------- 单元测试：目录扫描 ----------


class TestScanUserConfigs:
    """用户配置扫描测试"""

    def test_scan_existing_users(self, temp_repo_with_users: Path):
        """测试扫描已有用户配置"""
        configs = scan_user_configs(temp_repo_with_users, quiet=True)

        assert len(configs) == 2
        assert "test_user_1" in configs
        assert "test_user_2" in configs
        assert configs["test_user_1"].display_name == "Test User 1"

    def test_scan_empty_dir(self, temp_repo: Path):
        """测试目录不存在时返回空"""
        configs = scan_user_configs(temp_repo, quiet=True, strict=False)
        assert len(configs) == 0

    def test_scan_strict_mode_raises(self, temp_repo: Path):
        """测试 strict 模式下目录不存在时抛出错误"""
        with pytest.raises(AgentXDirectoryNotFoundError) as exc_info:
            scan_user_configs(temp_repo, quiet=True, strict=True)

        assert "用户配置目录不存在" in exc_info.value.message
        assert "mkdir -p" in exc_info.value.message  # 包含操作提示


class TestScanRoleProfiles:
    """角色配置扫描测试"""

    def test_scan_existing_roles(self, temp_repo_with_roles: Path):
        """测试扫描已有角色配置"""
        profiles = scan_role_profiles(temp_repo_with_roles, quiet=True)

        assert len(profiles) == 1
        assert "test_user_1" in profiles
        assert "代码开发" in profiles["test_user_1"].profile_md

    def test_scan_role_dir_not_exists(self, temp_repo_with_users: Path):
        """测试角色目录不存在时返回空"""
        profiles = scan_role_profiles(temp_repo_with_users, quiet=True)
        assert len(profiles) == 0


# ---------- 集成测试：数据库写入 ----------


class TestIdentitySyncIntegration:
    """身份同步集成测试（需要数据库）"""

    @pytest.mark.usefixtures("migrated_db")
    def test_sync_users_and_accounts(
        self, temp_repo_with_users: Path, migrated_db: dict
    ):
        """测试用户和账户写入数据库"""
        import psycopg
        from unittest.mock import MagicMock

        dsn = migrated_db["dsn"]

        # 创建 mock config
        config = MagicMock()
        config.require.return_value = dsn
        config.get.return_value = None

        # 执行同步
        stats = sync_identities(
            temp_repo_with_users,
            config=config,
            quiet=True,
            verbose=False,
        )

        # 验证统计
        assert stats.users_inserted == 2
        assert stats.users_updated == 0
        # user1 有 2 个账户，user2 有 1 个账户
        assert stats.accounts_inserted == 3

        # 验证数据库内容
        conn = psycopg.connect(dsn, autocommit=False)
        try:
            with conn.cursor() as cur:
                # 检查 users 表
                cur.execute(
                    "SELECT user_id, display_name FROM identity.users ORDER BY user_id"
                )
                users = cur.fetchall()
                assert len(users) == 2
                assert users[0][0] == "test_user_1"
                assert users[0][1] == "Test User 1"

                # 检查 accounts 表
                cur.execute(
                    "SELECT user_id, account_type, account_name FROM identity.accounts ORDER BY user_id, account_type"
                )
                accounts = cur.fetchall()
                assert len(accounts) == 3
        finally:
            conn.rollback()
            conn.close()

    @pytest.mark.usefixtures("migrated_db")
    def test_sync_idempotency(self, temp_repo_with_users: Path, migrated_db: dict):
        """测试同步幂等性：多次运行结果相同"""
        import psycopg
        from unittest.mock import MagicMock

        dsn = migrated_db["dsn"]

        config = MagicMock()
        config.require.return_value = dsn
        config.get.return_value = None

        # 第一次同步
        stats1 = sync_identities(
            temp_repo_with_users,
            config=config,
            quiet=True,
        )
        assert stats1.users_inserted == 2
        assert stats1.accounts_inserted == 3

        # 第二次同步（应该只更新，不插入）
        stats2 = sync_identities(
            temp_repo_with_users,
            config=config,
            quiet=True,
        )
        assert stats2.users_inserted == 0
        assert stats2.users_updated == 2
        assert stats2.accounts_inserted == 0
        assert stats2.accounts_updated == 3

        # 验证数据库行数不变
        conn = psycopg.connect(dsn, autocommit=False)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM identity.users")
                user_count = cur.fetchone()[0]
                assert user_count == 2

                cur.execute("SELECT COUNT(*) FROM identity.accounts")
                account_count = cur.fetchone()[0]
                assert account_count == 3
        finally:
            conn.rollback()
            conn.close()

    @pytest.mark.usefixtures("migrated_db")
    def test_sync_with_role_profiles(
        self, temp_repo_with_roles: Path, migrated_db: dict
    ):
        """测试包含角色配置的同步"""
        import psycopg
        from unittest.mock import MagicMock

        dsn = migrated_db["dsn"]

        config = MagicMock()
        config.require.return_value = dsn
        config.get.return_value = None

        # 执行同步
        stats = sync_identities(
            temp_repo_with_roles,
            config=config,
            quiet=True,
        )

        # 验证角色配置写入
        assert stats.role_profiles_inserted == 1
        assert stats.role_profiles_updated == 0

        # 验证数据库内容
        conn = psycopg.connect(dsn, autocommit=False)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT user_id, profile_md FROM identity.role_profiles"
                )
                profiles = cur.fetchall()
                assert len(profiles) == 1
                assert profiles[0][0] == "test_user_1"
                assert "代码开发" in profiles[0][1]
        finally:
            conn.rollback()
            conn.close()


# ---------- 测试：SyncStats ----------


class TestSyncStats:
    """SyncStats 数据类测试"""

    def test_to_dict(self):
        """测试转换为字典"""
        stats = SyncStats(
            users_inserted=2,
            users_updated=1,
            accounts_inserted=3,
            accounts_updated=0,
            role_profiles_inserted=1,
            role_profiles_updated=0,
        )
        d = stats.to_dict()

        assert d["users_inserted"] == 2
        assert d["accounts_inserted"] == 3
        assert d["role_profiles_inserted"] == 1

    def test_summary(self):
        """测试 summary 格式"""
        stats = SyncStats(
            users_inserted=2,
            users_updated=1,
            accounts_inserted=3,
            accounts_updated=2,
            role_profiles_inserted=1,
            role_profiles_updated=0,
        )
        summary = stats.summary()

        assert "用户: +2 ~1" in summary
        assert "账户: +3 ~2" in summary
        assert "角色配置: +1 ~0" in summary
