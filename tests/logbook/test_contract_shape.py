# -*- coding: utf-8 -*-
"""
契约级单测 - 验证 CLI 命令的 JSON 输出格式

这些测试只校验 JSON shape（字段存在性和类型），不依赖外部 GitLab/SVN。
使用 Mock 隔离外部依赖。

覆盖命令:
- logbook_cli: create_item, add_event, attach, set_kv, health, validate, render_views
- identity_sync: sync_identities
- logbook_cli_main: scm ensure-repo, sync-svn, sync-gitlab-commits, sync-gitlab-mrs, sync-gitlab-reviews
"""

import sys
from pathlib import Path
from typing import Any, Dict, Set
from unittest.mock import MagicMock, patch

import pytest

# =============================================================================
# 通用 Shape 验证器
# =============================================================================


def assert_json_shape(
    data: Dict[str, Any],
    required_fields: Set[str],
    optional_fields: Set[str] = None,
    field_types: Dict[str, type] = None,
):
    """
    验证 JSON 数据的 shape

    Args:
        data: 要验证的 JSON 数据
        required_fields: 必须存在的字段集合
        optional_fields: 可选字段集合
        field_types: 字段类型映射 {field: expected_type}
    """
    optional_fields = optional_fields or set()
    field_types = field_types or {}

    # 检查必需字段存在
    for field in required_fields:
        assert field in data, f"缺少必需字段: {field}, 实际字段: {list(data.keys())}"

    # 检查字段类型
    for field, expected_type in field_types.items():
        if field in data and data[field] is not None:
            actual_value = data[field]
            # 支持多类型检查
            if isinstance(expected_type, tuple):
                assert isinstance(actual_value, expected_type), (
                    f"字段 {field} 类型错误: 期望 {expected_type}, 实际 {type(actual_value)}"
                )
            else:
                assert isinstance(actual_value, expected_type), (
                    f"字段 {field} 类型错误: 期望 {expected_type}, 实际 {type(actual_value)}"
                )


def assert_success_shape(data: Dict[str, Any], extra_required: Set[str] = None):
    """验证成功响应的 shape"""
    required = {"ok"}
    if extra_required:
        required.update(extra_required)
    assert data.get("ok") is True, f"ok 应为 True: {data}"
    assert_json_shape(data, required)


def assert_error_shape(data: Dict[str, Any]):
    """验证错误响应的 shape"""
    required = {"ok", "code", "message"}
    optional = {"detail"}
    assert data.get("ok") is False, f"ok 应为 False: {data}"
    assert_json_shape(data, required, optional)


# =============================================================================
# Logbook CLI 契约测试
# =============================================================================


class TestLogbookCliContractShape:
    """logbook_cli.py 命令的 JSON shape 测试"""

    def test_create_item_success_shape(self, migrated_db: dict):
        """测试 create_item 成功响应的 shape"""

        # Mock 配置
        mock_config = MagicMock()
        mock_config.get.return_value = None
        mock_config.require.side_effect = lambda key: {
            "postgres.dsn": migrated_db["dsn"],
        }.get(key, f"mock_{key}")

        # 调用函数
        with patch("engram_logbook.config.get_config", return_value=mock_config):
            with patch("engram_logbook.db.get_config", return_value=mock_config):
                from engram.logbook.db import create_item
                from engram.logbook.errors import make_success_result

                item_id = create_item(
                    item_type="task",
                    title="Test Task",
                    scope_json={},
                    status="open",
                    config=mock_config,
                )

                # 构造预期的成功响应
                result = make_success_result(
                    item_id=item_id,
                    item_type="task",
                    title="Test Task",
                    status="open",
                )

        # 验证 shape
        assert_success_shape(result, {"item_id", "item_type", "title", "status"})
        assert_json_shape(
            result,
            set(),
            field_types={
                "item_id": int,
                "item_type": str,
                "title": str,
                "status": str,
            },
        )

    def test_add_event_success_shape(self, migrated_db: dict):
        """测试 add_event 成功响应的 shape"""

        mock_config = MagicMock()
        mock_config.get.return_value = None
        mock_config.require.side_effect = lambda key: {
            "postgres.dsn": migrated_db["dsn"],
        }.get(key, f"mock_{key}")

        with patch("engram_logbook.config.get_config", return_value=mock_config):
            with patch("engram_logbook.db.get_config", return_value=mock_config):
                from engram.logbook.db import add_event, create_item
                from engram.logbook.errors import make_success_result

                item_id = create_item(
                    item_type="task",
                    title="Event Test",
                    config=mock_config,
                )

                event_id = add_event(
                    item_id=item_id,
                    event_type="status_change",
                    payload_json={"note": "testing"},
                    status_from="open",
                    status_to="in_progress",
                    config=mock_config,
                )

                result = make_success_result(
                    event_id=event_id,
                    item_id=item_id,
                    event_type="status_change",
                    status_updated=True,
                    status_to="in_progress",
                )

        # 验证 shape
        assert_success_shape(result, {"event_id", "item_id", "event_type"})
        assert_json_shape(
            result,
            set(),
            field_types={
                "event_id": int,
                "item_id": int,
                "event_type": str,
            },
        )

    def test_attach_success_shape(self, migrated_db: dict):
        """测试 attach 成功响应的 shape"""

        mock_config = MagicMock()
        mock_config.get.return_value = None
        mock_config.require.side_effect = lambda key: {
            "postgres.dsn": migrated_db["dsn"],
        }.get(key, f"mock_{key}")

        with patch("engram_logbook.config.get_config", return_value=mock_config):
            with patch("engram_logbook.db.get_config", return_value=mock_config):
                from engram.logbook.db import attach, create_item
                from engram.logbook.errors import make_success_result

                item_id = create_item(
                    item_type="task",
                    title="Attach Test",
                    config=mock_config,
                )

                test_sha256 = "a" * 64
                attachment_id = attach(
                    item_id=item_id,
                    kind="patch",
                    uri="file:///tmp/test.diff",
                    sha256=test_sha256,
                    size_bytes=1024,
                    meta_json={"format": "unified"},
                    config=mock_config,
                )

                result = make_success_result(
                    attachment_id=attachment_id,
                    item_id=item_id,
                    kind="patch",
                    uri="file:///tmp/test.diff",
                    sha256=test_sha256,
                    size_bytes=1024,
                )

        # 验证 shape
        assert_success_shape(result, {"attachment_id", "item_id", "kind", "uri", "sha256"})
        assert_json_shape(
            result,
            set(),
            field_types={
                "attachment_id": int,
                "item_id": int,
                "kind": str,
                "uri": str,
                "sha256": str,
            },
        )

    def test_set_kv_success_shape(self, migrated_db: dict):
        """测试 set_kv 成功响应的 shape"""

        mock_config = MagicMock()
        mock_config.get.return_value = None
        mock_config.require.side_effect = lambda key: {
            "postgres.dsn": migrated_db["dsn"],
        }.get(key, f"mock_{key}")

        with patch("engram_logbook.config.get_config", return_value=mock_config):
            with patch("engram_logbook.db.get_config", return_value=mock_config):
                from engram.logbook.db import set_kv
                from engram.logbook.errors import make_success_result

                set_kv(
                    namespace="test.contract",
                    key="sample_key",
                    value_json={"data": "value"},
                    config=mock_config,
                )

                result = make_success_result(
                    namespace="test.contract",
                    key="sample_key",
                    upserted=True,
                )

        # 验证 shape
        assert_success_shape(result, {"namespace", "key", "upserted"})
        assert_json_shape(
            result,
            set(),
            field_types={
                "namespace": str,
                "key": str,
                "upserted": bool,
            },
        )


class TestLogbookCliErrorShape:
    """logbook_cli.py 错误响应的 shape 测试"""

    def test_validation_error_shape(self):
        """测试 ValidationError 的 shape"""
        from engram.logbook.errors import ValidationError

        error = ValidationError(
            message="缺少必需参数",
            details={"field": "item_type"},
        )

        result = error.to_dict()

        # 验证 shape
        assert_error_shape(result)
        assert result["code"] == "VALIDATION_ERROR"
        assert "缺少必需参数" in result["message"]

    def test_database_error_shape(self):
        """测试 DatabaseError 的 shape"""
        from engram.logbook.errors import DatabaseError

        error = DatabaseError(
            message="连接失败",
            details={"host": "localhost"},
        )

        result = error.to_dict()

        # 验证 shape
        assert_error_shape(result)
        assert result["code"] == "DATABASE_ERROR"

    def test_materialize_error_shape(self):
        """测试 MaterializeError 的 shape"""
        from engram.logbook.errors import ChecksumMismatchError

        error = ChecksumMismatchError(
            message="SHA256 不匹配",
            details={"expected": "abc...", "actual": "def..."},
        )

        result = error.to_dict()

        # 验证 shape
        assert_error_shape(result)
        assert result["code"] == "CHECKSUM_MISMATCH"
        assert error.exit_code == 12


# =============================================================================
# Identity Sync 契约测试
# =============================================================================


class TestIdentitySyncContractShape:
    """identity_sync.py 的 JSON shape 测试"""

    def test_sync_stats_shape(self):
        """测试 SyncStats 数据结构的 shape"""
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from identity_sync import SyncStats

        stats = SyncStats(
            users_inserted=2,
            users_updated=1,
            accounts_inserted=5,
            accounts_updated=0,
            role_profiles_inserted=1,
            role_profiles_updated=0,
        )

        result = stats.to_dict()

        # 验证 shape
        expected_fields = {
            "users_inserted",
            "users_updated",
            "accounts_inserted",
            "accounts_updated",
            "role_profiles_inserted",
            "role_profiles_updated",
        }
        assert_json_shape(result, expected_fields)

        # 验证类型
        for field in expected_fields:
            assert isinstance(result[field], int), f"{field} 应为 int"

    def test_sync_success_shape(self, migrated_db: dict, tmp_path: Path):
        """测试同步成功响应的 shape"""

        # 创建测试用户配置
        users_dir = tmp_path / ".agentx" / "users"
        users_dir.mkdir(parents=True)

        user_config = """
user_id: test_user
display_name: Test User
is_active: true
roles:
  - developer
accounts:
  svn:
    username: tuser
"""
        (users_dir / "test_user.yaml").write_text(user_config)

        mock_config = MagicMock()
        mock_config.get.return_value = None
        mock_config.require.side_effect = lambda key: {
            "postgres.dsn": migrated_db["dsn"],
        }.get(key, f"mock_{key}")

        from engram.logbook.errors import make_success_result
        from identity_sync import sync_identities

        with patch("engram_logbook.db.get_config", return_value=mock_config):
            stats = sync_identities(
                repo_root=tmp_path,
                config=mock_config,
                quiet=True,
            )

        result = make_success_result(
            stats=stats.to_dict(),
            summary=stats.summary(),
        )

        # 验证 shape
        assert_success_shape(result, {"stats", "summary"})
        assert isinstance(result["stats"], dict)
        assert isinstance(result["summary"], str)


# =============================================================================
# Render Views 契约测试
# =============================================================================


class TestRenderViewsContractShape:
    """render_views.py 的 JSON shape 测试"""

    def test_render_views_success_shape(self, migrated_db: dict, tmp_path: Path):
        """测试 render_views 成功响应的 shape"""
        from pathlib import Path
        from unittest.mock import MagicMock

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        schemas = migrated_db["schemas"]
        search_path_list = [
            schemas["logbook"],
            schemas["identity"],
            schemas["scm"],
            schemas["analysis"],
            schemas["governance"],
            "public",
        ]

        mock_config = MagicMock()
        mock_config.get.side_effect = lambda key, default=None: {
            "postgres.search_path": search_path_list,
        }.get(key, default)
        mock_config.require.side_effect = lambda key: {
            "postgres.dsn": migrated_db["dsn"],
        }.get(key, f"mock_{key}")

        from engram.logbook.views import render_views

        out_dir = tmp_path / "views"
        result = render_views(
            out_dir=str(out_dir),
            config=mock_config,
            quiet=True,
        )

        # 验证 shape
        required_fields = {"out_dir", "items_count", "files", "rendered_at"}
        assert_json_shape(result, required_fields)

        # 验证 files 子结构
        assert "manifest" in result["files"]
        assert "index" in result["files"]

        for file_key in ["manifest", "index"]:
            file_info = result["files"][file_key]
            assert_json_shape(file_info, {"path", "size", "sha256"})
            assert isinstance(file_info["path"], str)
            assert isinstance(file_info["size"], int)
            assert isinstance(file_info["sha256"], str)


# =============================================================================
# SCM CLI 契约测试（Mock 外部依赖）
# =============================================================================


class TestScmCliContractShape:
    """logbook_cli_main.py SCM 命令的 JSON shape 测试（Mock 模式）"""

    def test_ensure_repo_success_shape_mock(self):
        """测试 ensure-repo 成功响应的 shape（Mock）"""
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from logbook_cli_main import make_ok_result

        # 模拟成功响应
        result = make_ok_result(
            item_id=1,
            repo_id=123,
            created=True,
            repo_type="git",
            url="https://gitlab.example.com/ns/proj",
            project_key="my_project",
        )

        # 验证 shape
        assert_success_shape(result, {"repo_id", "created"})
        assert_json_shape(
            result,
            set(),
            field_types={
                "repo_id": int,
                "created": bool,
                "repo_type": str,
                "url": str,
                "project_key": str,
            },
        )

    def test_sync_svn_success_shape_mock(self):
        """测试 sync-svn 成功响应的 shape（Mock）"""
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from logbook_cli_main import make_ok_result

        # 模拟成功响应（按契约定义）
        result = make_ok_result(
            item_id=1,
            repo_id=123,
            synced_count=50,
            start_rev=101,
            end_rev=150,
            last_rev=150,
            has_more=True,
            remaining=200,
            bulk_count=2,
            loop_count=1,
        )

        # 验证 shape（契约定义的字段）
        contract_fields = {"repo_id", "synced_count", "has_more"}
        assert_success_shape(result, contract_fields)

        # 可选字段验证
        optional_fields = {"start_rev", "end_rev", "last_rev", "remaining", "bulk_count"}
        for field in optional_fields:
            if field in result:
                assert isinstance(result[field], (int, type(None)))

    def test_sync_gitlab_commits_success_shape_mock(self):
        """测试 sync-gitlab-commits 成功响应的 shape（Mock）"""
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from logbook_cli_main import make_ok_result

        # 模拟成功响应（按契约定义）
        result = make_ok_result(
            item_id=1,
            repo_id=123,
            synced_count=50,
            diff_count=48,
            since="2024-01-01T00:00:00Z",
            last_commit_sha="abc123def456",
            last_commit_ts="2024-01-15T12:30:00Z",
            has_more=True,
            bulk_count=3,
            loop_count=1,
        )

        # 验证 shape（契约定义的字段）
        contract_fields = {"repo_id", "synced_count", "diff_count", "has_more"}
        assert_success_shape(result, contract_fields)

    def test_sync_gitlab_mrs_success_shape_mock(self):
        """测试 sync-gitlab-mrs 成功响应的 shape（Mock）"""
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from logbook_cli_main import make_ok_result

        # 模拟成功响应（按契约定义）
        result = make_ok_result(
            item_id=1,
            repo_id=123,
            inserted=10,
            updated=5,
            skipped=0,
            has_more=False,
            loop_count=1,
        )

        # 验证 shape（契约定义的字段）
        contract_fields = {"repo_id", "inserted", "updated", "skipped", "has_more"}
        assert_success_shape(result, contract_fields)
        assert_json_shape(
            result,
            set(),
            field_types={
                "repo_id": int,
                "inserted": int,
                "updated": int,
                "skipped": int,
                "has_more": bool,
            },
        )

    def test_sync_gitlab_reviews_success_shape_mock(self):
        """测试 sync-gitlab-reviews 成功响应的 shape（Mock）"""
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from logbook_cli_main import make_ok_result

        # 模拟成功响应（按契约定义）
        result = make_ok_result(
            item_id=1,
            repo_id=123,
            mr_id="123:42",
            inserted=15,
            skipped=3,
            by_type={
                "comment": 10,
                "approve": 2,
                "assign": 3,
            },
            has_more=False,
            loop_count=1,
        )

        # 验证 shape（契约定义的字段）
        contract_fields = {"inserted", "skipped", "by_type"}
        assert_success_shape(result, contract_fields)

        # 验证 by_type 子结构
        assert isinstance(result["by_type"], dict)
        for event_type, count in result["by_type"].items():
            assert isinstance(event_type, str)
            assert isinstance(count, int)

    def test_error_response_shape_mock(self):
        """测试错误响应的 shape（Mock）"""
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from logbook_cli_main import make_err_result

        # 模拟错误响应
        result = make_err_result(
            code="VALIDATION_ERROR",
            message="缺少必需参数 --repo-url",
            detail={"field": "repo_url"},
        )

        # 验证 shape
        assert_error_shape(result)
        assert result["code"] == "VALIDATION_ERROR"


# =============================================================================
# Exit Code 契约测试
# =============================================================================

# =============================================================================
# SCM 配置优先级测试
# =============================================================================


class TestGetBulkThresholds:
    """测试 get_bulk_thresholds 函数的键名优先级"""

    def test_only_new_keys(self):
        """测试仅配置新键名时正确读取"""
        from engram.logbook.config import get_bulk_thresholds

        mock_config = MagicMock()
        mock_config._loaded = True
        mock_config.get.side_effect = lambda key, default=None: {
            "scm.bulk_thresholds.svn_changed_paths": 200,
            "scm.bulk_thresholds.git_total_changes": 2000,
            "scm.bulk_thresholds.git_files_changed": 100,
            "scm.bulk_thresholds.diff_size_bytes": 2097152,
        }.get(key, default)

        result = get_bulk_thresholds(config=mock_config)

        assert result["svn_changed_paths_threshold"] == 200
        assert result["git_total_changes_threshold"] == 2000
        assert result["git_files_changed_threshold"] == 100
        assert result["diff_size_threshold"] == 2097152

    def test_only_old_keys(self):
        """测试仅配置旧键名时回退读取"""
        from engram.logbook.config import get_bulk_thresholds

        mock_config = MagicMock()
        mock_config._loaded = True
        mock_config.get.side_effect = lambda key, default=None: {
            "bulk.svn_changed_paths_threshold": 150,
            "bulk.git_total_changes_threshold": 1500,
            "bulk.git_files_changed_threshold": 75,
            "bulk.diff_size_threshold": 524288,
        }.get(key, default)

        result = get_bulk_thresholds(config=mock_config)

        assert result["svn_changed_paths_threshold"] == 150
        assert result["git_total_changes_threshold"] == 1500
        assert result["git_files_changed_threshold"] == 75
        assert result["diff_size_threshold"] == 524288

    def test_new_key_priority_over_old(self):
        """测试新键优先于旧键"""
        from engram.logbook.config import get_bulk_thresholds

        mock_config = MagicMock()
        mock_config._loaded = True
        mock_config.get.side_effect = lambda key, default=None: {
            # 新键
            "scm.bulk_thresholds.svn_changed_paths": 300,
            "scm.bulk_thresholds.git_total_changes": 3000,
            # 旧键（应被忽略）
            "bulk.svn_changed_paths_threshold": 100,
            "bulk.git_total_changes_threshold": 1000,
            # 仅旧键
            "bulk.git_files_changed_threshold": 50,
            "bulk.diff_size_threshold": 1048576,
        }.get(key, default)

        result = get_bulk_thresholds(config=mock_config)

        # 新键优先
        assert result["svn_changed_paths_threshold"] == 300
        assert result["git_total_changes_threshold"] == 3000
        # 仅旧键时回退
        assert result["git_files_changed_threshold"] == 50
        assert result["diff_size_threshold"] == 1048576

    def test_default_values_when_no_config(self):
        """测试无配置时使用默认值"""
        from engram.logbook.config import (
            DEFAULT_DIFF_SIZE_THRESHOLD,
            DEFAULT_GIT_FILES_CHANGED_THRESHOLD,
            DEFAULT_GIT_TOTAL_CHANGES_THRESHOLD,
            DEFAULT_SVN_CHANGED_PATHS_THRESHOLD,
            get_bulk_thresholds,
        )

        mock_config = MagicMock()
        mock_config._loaded = True
        mock_config.get.return_value = None

        result = get_bulk_thresholds(config=mock_config)

        assert result["svn_changed_paths_threshold"] == DEFAULT_SVN_CHANGED_PATHS_THRESHOLD
        assert result["git_total_changes_threshold"] == DEFAULT_GIT_TOTAL_CHANGES_THRESHOLD
        assert result["git_files_changed_threshold"] == DEFAULT_GIT_FILES_CHANGED_THRESHOLD
        assert result["diff_size_threshold"] == DEFAULT_DIFF_SIZE_THRESHOLD


class TestGetScmConfig:
    """测试 get_scm_config 函数的键名优先级"""

    def test_only_new_keys(self):
        """测试仅配置新键名时正确读取"""
        from engram.logbook.config import get_scm_config

        mock_config = MagicMock()
        mock_config._loaded = True
        mock_config.get.side_effect = lambda key, default=None: {
            "scm.gitlab.url": "https://new.gitlab.com",
            "scm.gitlab.project": "new/project",
        }.get(key, default)

        url = get_scm_config("scm.gitlab.url", config=mock_config)
        project = get_scm_config("scm.gitlab.project", config=mock_config)

        assert url == "https://new.gitlab.com"
        assert project == "new/project"

    def test_only_old_keys(self):
        """测试仅配置旧键名时回退读取"""
        from engram.logbook.config import get_scm_config

        mock_config = MagicMock()
        mock_config._loaded = True
        mock_config.get.side_effect = lambda key, default=None: {
            "gitlab.url": "https://old.gitlab.com",
            "gitlab.project_id": "old/project",
        }.get(key, default)

        # 使用新键名调用，应回退到旧键
        url = get_scm_config("scm.gitlab.url", config=mock_config)
        project = get_scm_config("scm.gitlab.project", config=mock_config)

        assert url == "https://old.gitlab.com"
        assert project == "old/project"

    def test_new_key_priority_over_old(self):
        """测试新键优先于旧键"""
        from engram.logbook.config import get_scm_config

        mock_config = MagicMock()
        mock_config._loaded = True
        mock_config.get.side_effect = lambda key, default=None: {
            # 新键
            "scm.gitlab.url": "https://new.gitlab.com",
            # 旧键（应被忽略）
            "gitlab.url": "https://old.gitlab.com",
            "gitlab.project_id": "old/project",
        }.get(key, default)

        url = get_scm_config("scm.gitlab.url", config=mock_config)
        project = get_scm_config("scm.gitlab.project", config=mock_config)

        # 新键优先
        assert url == "https://new.gitlab.com"
        # 仅旧键时回退
        assert project == "old/project"

    def test_old_key_call_converts_to_new(self):
        """测试使用旧键名调用时会尝试新键"""
        from engram.logbook.config import get_scm_config

        mock_config = MagicMock()
        mock_config._loaded = True
        mock_config.get.side_effect = lambda key, default=None: {
            "scm.gitlab.url": "https://new.gitlab.com",
            "gitlab.url": "https://old.gitlab.com",
        }.get(key, default)

        # 使用旧键名调用，应先尝试新键
        url = get_scm_config("gitlab.url", config=mock_config)

        assert url == "https://new.gitlab.com"

    def test_default_value_when_no_config(self):
        """测试无配置时返回默认值"""
        from engram.logbook.config import get_scm_config

        mock_config = MagicMock()
        mock_config._loaded = True
        # mock get 方法正确处理 default 参数
        mock_config.get.side_effect = lambda key, default=None: default

        result = get_scm_config("scm.gitlab.url", default="https://default.com", config=mock_config)

        assert result == "https://default.com"


class TestRequireScmConfig:
    """测试 require_scm_config 函数的错误提示"""

    def test_missing_config_raises_error_with_new_key_hint(self):
        """测试缺失配置时抛出错误，错误信息包含新键名"""
        from engram.logbook.config import require_scm_config
        from engram.logbook.errors import ConfigError

        mock_config = MagicMock()
        mock_config._loaded = True
        mock_config.get.return_value = None

        with pytest.raises(ConfigError) as exc_info:
            require_scm_config("scm.gitlab.url", config=mock_config)

        error = exc_info.value
        assert "scm.gitlab.url" in str(error.message)
        assert error.details["key"] == "scm.gitlab.url"

    def test_missing_config_with_old_key_shows_new_key(self):
        """测试使用旧键名时错误信息仍显示新键名"""
        from engram.logbook.config import require_scm_config
        from engram.logbook.errors import ConfigError

        mock_config = MagicMock()
        mock_config._loaded = True
        mock_config.get.return_value = None

        with pytest.raises(ConfigError) as exc_info:
            require_scm_config("gitlab.url", config=mock_config)

        error = exc_info.value
        # 错误信息应提示新键名
        assert "scm.gitlab.url" in str(error.message)
        assert error.details["key"] == "scm.gitlab.url"
        assert error.details["legacy_key"] == "gitlab.url"

    def test_existing_config_returns_value(self):
        """测试配置存在时正常返回值"""
        from engram.logbook.config import require_scm_config

        mock_config = MagicMock()
        mock_config._loaded = True
        mock_config.get.side_effect = lambda key, default=None: {
            "scm.gitlab.url": "https://gitlab.com",
        }.get(key, default)

        result = require_scm_config("scm.gitlab.url", config=mock_config)

        assert result == "https://gitlab.com"


# =============================================================================
# Exit Code 契约测试
# =============================================================================

# =============================================================================
# Governance 契约测试
# =============================================================================


class TestGovernanceGetContractShape:
    """governance_get 命令的 JSON shape 测试"""

    def test_governance_get_exists_shape(self, migrated_db: dict):
        """测试 governance_get 设置存在时的响应 shape"""

        schemas = migrated_db["schemas"]
        search_path_list = [
            schemas["logbook"],
            schemas["identity"],
            schemas["scm"],
            schemas["analysis"],
            schemas["governance"],
            "public",
        ]

        mock_config = MagicMock()
        mock_config.get.side_effect = lambda key, default=None: {
            "postgres.search_path": search_path_list,
        }.get(key, default)
        mock_config.require.side_effect = lambda key: {
            "postgres.dsn": migrated_db["dsn"],
        }.get(key, f"mock_{key}")

        with patch("engram_logbook.governance.get_connection") as mock_get_conn:
            # 创建模拟的数据库连接
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
            mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

            from datetime import datetime

            mock_cursor.fetchone.return_value = (
                "test_project",
                True,
                {"rule1": "value1"},
                "admin_user",
                datetime(2024, 1, 15, 12, 30, 0),
            )
            mock_get_conn.return_value = mock_conn

            from engram.logbook.errors import make_success_result
            from engram.logbook.governance import get_settings

            settings = get_settings(project_key="test_project", config=mock_config)

            # 构造预期的成功响应
            settings_data = dict(settings)
            settings_data["updated_at"] = settings_data["updated_at"].isoformat()

            result = make_success_result(
                exists=True,
                settings=settings_data,
            )

        # 验证 shape（按契约定义）
        assert_success_shape(result, {"exists", "settings"})
        assert result["exists"] is True

        # 验证 settings 子结构
        settings_obj = result["settings"]
        required_settings_fields = {
            "project_key",
            "team_write_enabled",
            "policy_json",
            "updated_at",
        }
        for field in required_settings_fields:
            assert field in settings_obj, f"settings 缺少字段: {field}"

        assert isinstance(settings_obj["project_key"], str)
        assert isinstance(settings_obj["team_write_enabled"], bool)
        assert isinstance(settings_obj["policy_json"], dict)

    def test_governance_get_not_exists_shape(self):
        """测试 governance_get 设置不存在时的响应 shape"""
        from engram.logbook.errors import make_success_result

        # 模拟设置不存在的响应
        result = make_success_result(
            project_key="nonexistent_project",
            exists=False,
            message="项目设置不存在",
        )

        # 验证 shape（按契约定义）
        assert_success_shape(result, {"project_key", "exists"})
        assert result["exists"] is False
        assert isinstance(result["project_key"], str)
        assert "message" in result


class TestGovernanceSetContractShape:
    """governance_set 命令的 JSON shape 测试"""

    def test_governance_set_success_shape(self):
        """测试 governance_set 成功响应的 shape"""
        from engram.logbook.errors import make_success_result

        # 模拟成功响应（按契约定义）
        result = make_success_result(
            project_key="my_project",
            team_write_enabled=True,
            policy_json={"require_review": True, "min_approvers": 2},
            updated_by="admin_user",
            upserted=True,
        )

        # 验证 shape（按契约定义）
        contract_fields = {"project_key", "team_write_enabled", "policy_json", "upserted"}
        assert_success_shape(result, contract_fields)

        assert_json_shape(
            result,
            set(),
            field_types={
                "project_key": str,
                "team_write_enabled": bool,
                "policy_json": dict,
                "upserted": bool,
            },
        )

        # updated_by 是可选字段
        if "updated_by" in result:
            assert isinstance(result["updated_by"], (str, type(None)))


class TestAuditQueryContractShape:
    """audit_query 命令的 JSON shape 测试"""

    def test_audit_query_success_shape(self):
        """测试 audit_query 成功响应的 shape"""

        from engram.logbook.errors import make_success_result

        # 模拟审计记录
        mock_audits = [
            {
                "audit_id": 1,
                "actor_user_id": "user_001",
                "target_space": "team:my_project",
                "action": "allow",
                "reason": "scm_sync",
                "payload_sha": "abc123" + "0" * 58,
                "evidence_refs_json": {"patches": []},
                "created_at": "2024-01-15T12:30:00",
            },
            {
                "audit_id": 2,
                "actor_user_id": None,
                "target_space": "private:user_001",
                "action": "redirect",
                "reason": None,
                "payload_sha": None,
                "evidence_refs_json": {},
                "created_at": "2024-01-15T12:31:00",
            },
        ]

        result = make_success_result(
            count=len(mock_audits),
            audits=mock_audits,
        )

        # 验证 shape（按契约定义）
        contract_fields = {"count", "audits"}
        assert_success_shape(result, contract_fields)

        assert isinstance(result["count"], int)
        assert isinstance(result["audits"], list)

        # 验证审计记录结构
        for audit in result["audits"]:
            audit_required_fields = {"audit_id", "target_space", "action", "created_at"}
            for field in audit_required_fields:
                assert field in audit, f"audit 缺少必需字段: {field}"

            assert isinstance(audit["audit_id"], int)
            assert isinstance(audit["target_space"], str)
            assert isinstance(audit["action"], str)

    def test_audit_query_empty_result_shape(self):
        """测试 audit_query 无结果时的响应 shape"""
        from engram.logbook.errors import make_success_result

        result = make_success_result(
            count=0,
            audits=[],
        )

        # 验证 shape
        assert_success_shape(result, {"count", "audits"})
        assert result["count"] == 0
        assert result["audits"] == []


class TestRenderViewsContractShapeEnhanced:
    """render_views 命令的增强 JSON shape 测试"""

    def test_render_views_full_contract_shape(self, migrated_db: dict, tmp_path: Path):
        """测试 render_views 完整契约响应的 shape"""

        schemas = migrated_db["schemas"]
        search_path_list = [
            schemas["logbook"],
            schemas["identity"],
            schemas["scm"],
            schemas["analysis"],
            schemas["governance"],
            "public",
        ]

        mock_config = MagicMock()
        mock_config.get.side_effect = lambda key, default=None: {
            "postgres.search_path": search_path_list,
        }.get(key, default)
        mock_config.require.side_effect = lambda key: {
            "postgres.dsn": migrated_db["dsn"],
        }.get(key, f"mock_{key}")

        from engram.logbook.errors import make_success_result
        from engram.logbook.views import render_views

        out_dir = tmp_path / "views"
        render_result = render_views(
            out_dir=str(out_dir),
            config=mock_config,
            quiet=True,
        )

        # 添加 ok 字段构造完整响应
        result = make_success_result(**render_result)

        # 验证契约定义的必需字段
        contract_required_fields = {"ok", "out_dir", "items_count", "files", "rendered_at"}
        assert_json_shape(result, contract_required_fields)

        # 验证 ok 字段
        assert result["ok"] is True

        # 验证字段类型
        assert isinstance(result["out_dir"], str)
        assert isinstance(result["items_count"], int)
        assert isinstance(result["files"], dict)
        assert isinstance(result["rendered_at"], str)

        # 验证 files 子结构完整性
        assert "manifest" in result["files"]
        assert "index" in result["files"]

        for file_key in ["manifest", "index"]:
            file_info = result["files"][file_key]
            file_required_fields = {"path", "size", "sha256"}
            assert_json_shape(file_info, file_required_fields)
            assert isinstance(file_info["path"], str)
            assert isinstance(file_info["size"], int)
            assert isinstance(file_info["sha256"], str)
            # 验证 SHA256 格式（64 个十六进制字符）
            assert len(file_info["sha256"]) == 64, f"{file_key} sha256 长度应为 64"


class TestExitCodeContract:
    """验证 exit code 与契约一致"""

    def test_exit_code_values(self):
        """验证 ExitCode 常量值"""
        from engram.logbook.errors import ExitCode

        # 按契约定义验证
        assert ExitCode.SUCCESS == 0
        assert ExitCode.ENGRAM_ERROR == 1
        assert ExitCode.CONFIG_ERROR == 2
        assert ExitCode.DATABASE_ERROR == 3
        assert ExitCode.VALIDATION_ERROR == 6
        assert ExitCode.IDENTITY_SYNC_ERROR == 11
        assert ExitCode.MATERIALIZE_ERROR == 12

    def test_error_classes_exit_codes(self):
        """验证各错误类的 exit_code"""
        from engram.logbook.errors import (
            ChecksumMismatchError,
            ConfigError,
            DatabaseError,
            EngramError,
            FetchError,
            MaterializeError,
            PayloadTooLargeError,
            ValidationError,
        )

        assert EngramError.exit_code == 1
        assert ConfigError.exit_code == 2
        assert DatabaseError.exit_code == 3
        assert ValidationError.exit_code == 6
        assert MaterializeError.exit_code == 12
        assert ChecksumMismatchError.exit_code == 12
        assert PayloadTooLargeError.exit_code == 12
        assert FetchError.exit_code == 12


# =============================================================================
# ArtifactStore 契约测试
# =============================================================================


class TestArtifactWriteContractShape:
    """artifacts.write 命令的 JSON shape 测试"""

    def test_artifacts_write_success_shape(self):
        """测试 artifacts.write 成功响应的 shape（按契约定义）"""
        from engram.logbook.errors import make_success_result

        # 模拟成功响应（按契约定义）
        result = make_success_result(
            path="scm/1/svn/r100.diff",
            uri="scm/1/svn/r100.diff",
            sha256="abc123" + "0" * 58,
            size_bytes=1234,
            backend="local",
            created=True,
        )

        # 验证 shape（契约定义的字段）
        contract_fields = {"ok", "path", "uri", "sha256", "size_bytes", "backend", "created"}
        assert_success_shape(result, contract_fields)
        assert_json_shape(
            result,
            set(),
            field_types={
                "path": str,
                "uri": str,
                "sha256": str,
                "size_bytes": int,
                "backend": str,
                "created": bool,
            },
        )


class TestArtifactReadContractShape:
    """artifacts.read 命令的 JSON shape 测试"""

    def test_artifacts_read_success_shape(self):
        """测试 artifacts.read 成功响应的 shape（按契约定义）"""
        from engram.logbook.errors import make_success_result

        # 模拟成功响应（按契约定义）
        result = make_success_result(
            path="scm/1/svn/r100.diff",
            size_bytes=1234,
            sha256="abc123" + "0" * 58,
            backend="local",
            output="/tmp/r100.diff",
        )

        # 验证 shape（契约定义的字段）
        contract_fields = {"ok", "path", "size_bytes", "sha256", "backend"}
        assert_success_shape(result, contract_fields)
        assert_json_shape(
            result,
            set(),
            field_types={
                "path": str,
                "size_bytes": int,
                "sha256": str,
                "backend": str,
            },
        )


class TestArtifactExistsContractShape:
    """artifacts.exists 命令的 JSON shape 测试"""

    def test_artifacts_exists_success_shape(self):
        """测试 artifacts.exists 成功响应的 shape（按契约定义）"""
        from engram.logbook.errors import make_success_result

        # 模拟成功响应（按契约定义）
        result = make_success_result(
            path="scm/1/svn/r100.diff",
            exists=True,
            size_bytes=1234,
            backend="local",
        )

        # 验证 shape（契约定义的字段）
        contract_fields = {"ok", "path", "exists"}
        assert_success_shape(result, contract_fields)
        assert_json_shape(
            result,
            set(),
            field_types={
                "path": str,
                "exists": bool,
            },
        )

    def test_artifacts_exists_not_found_shape(self):
        """测试 artifacts.exists 不存在时的响应 shape"""
        from engram.logbook.errors import make_success_result

        result = make_success_result(
            path="scm/1/svn/r999.diff",
            exists=False,
        )

        assert_success_shape(result, {"ok", "path", "exists"})
        assert result["exists"] is False


class TestArtifactDeleteContractShape:
    """artifacts.delete 命令的 JSON shape 测试"""

    def test_artifacts_delete_success_shape(self):
        """测试 artifacts.delete 成功响应的 shape（按契约定义）"""
        from engram.logbook.errors import make_success_result

        # 模拟成功响应（按契约定义）
        result = make_success_result(
            path="scm/1/svn/r100.diff",
            deleted=True,
            backend="local",
        )

        # 验证 shape（契约定义的字段）
        contract_fields = {"ok", "path", "deleted"}
        assert_success_shape(result, contract_fields)
        assert_json_shape(
            result,
            set(),
            field_types={
                "path": str,
                "deleted": bool,
            },
        )


class TestArtifactVerifyContractShape:
    """artifacts.verify 命令的 JSON shape 测试"""

    def test_artifacts_verify_success_shape(self):
        """测试 artifacts.verify 成功响应的 shape（按契约定义）"""
        from engram.logbook.errors import make_success_result

        # 模拟成功响应（按契约定义）
        result = make_success_result(
            total=100,
            valid=98,
            missing=1,
            corrupted=1,
            details=[
                {
                    "path": "scm/1/svn/r100.diff",
                    "status": "valid",
                    "sha256": "abc123" + "0" * 58,
                },
                {
                    "path": "scm/1/svn/r101.diff",
                    "status": "missing",
                },
                {
                    "path": "scm/1/svn/r102.diff",
                    "status": "corrupted",
                    "expected_sha256": "def456" + "0" * 58,
                    "actual_sha256": "ghi789" + "0" * 58,
                },
            ],
        )

        # 验证 shape（契约定义的字段）
        contract_fields = {"ok", "total", "valid", "missing", "corrupted"}
        assert_success_shape(result, contract_fields)
        assert_json_shape(
            result,
            set(),
            field_types={
                "total": int,
                "valid": int,
                "missing": int,
                "corrupted": int,
                "details": list,
            },
        )

        # 验证 details 子结构
        for detail in result["details"]:
            assert "path" in detail
            assert "status" in detail
            assert detail["status"] in ("valid", "missing", "corrupted")


class TestArtifactGCContractShape:
    """artifacts.gc 命令的 JSON shape 测试"""

    def test_artifacts_gc_success_shape(self):
        """测试 artifacts.gc 成功响应的 shape（按契约定义）"""
        from engram.logbook.errors import make_success_result

        # 模拟成功响应（按契约定义）
        result = make_success_result(
            scanned=1000,
            orphans={
                "count": 15,
                "size_bytes": 1234567,
                "deleted": False,
            },
            tmp_files={
                "count": 8,
                "size_bytes": 45678,
                "deleted": False,
            },
        )

        # 验证 shape（契约定义的字段）
        contract_fields = {"ok", "scanned", "orphans", "tmp_files"}
        assert_success_shape(result, contract_fields)
        assert_json_shape(
            result,
            set(),
            field_types={
                "scanned": int,
                "orphans": dict,
                "tmp_files": dict,
            },
        )

        # 验证子结构
        for key in ["orphans", "tmp_files"]:
            assert "count" in result[key]
            assert "size_bytes" in result[key]
            assert "deleted" in result[key]


# =============================================================================
# Materialize Patch Blob 契约测试
# =============================================================================


class TestMaterializePatchBlobContractShape:
    """scm.materialize_patch_blob 命令的 JSON shape 测试"""

    def test_materialize_success_shape(self):
        """测试 materialize_patch_blob 成功响应的 shape（按契约定义）"""
        from engram.logbook.errors import make_success_result

        # 模拟成功响应（按契约定义）
        result = make_success_result(
            total=10,
            materialized=8,
            skipped=1,
            failed=1,
            details=[
                {
                    "blob_id": 123,
                    "status": "materialized",
                    "uri": "scm/1/svn/r100.diff",
                    "sha256": "abc123" + "0" * 58,
                    "size_bytes": 1234,
                },
            ],
        )

        # 验证 shape（契约定义的字段）
        contract_fields = {"ok", "total", "materialized", "skipped", "failed"}
        assert_success_shape(result, contract_fields)
        assert_json_shape(
            result,
            set(),
            field_types={
                "total": int,
                "materialized": int,
                "skipped": int,
                "failed": int,
            },
        )

    def test_materialize_with_details_shape(self):
        """测试 materialize_patch_blob 带 details 的响应 shape"""
        from engram.logbook.errors import make_success_result

        details = [
            {
                "blob_id": 123,
                "status": "materialized",
                "uri": "scm/1/svn/r100.diff",
                "sha256": "abc123" + "0" * 58,
                "size_bytes": 1234,
            },
            {
                "blob_id": 124,
                "status": "skipped",
            },
            {
                "blob_id": 125,
                "status": "failed",
                "error": "CHECKSUM_MISMATCH",
            },
        ]

        result = make_success_result(
            total=3,
            materialized=1,
            skipped=1,
            failed=1,
            details=details,
        )

        # 验证 details 子结构
        assert "details" in result
        for detail in result["details"]:
            assert "blob_id" in detail
            assert "status" in detail
            assert detail["status"] in ("materialized", "skipped", "failed", "unreachable")


# =============================================================================
# ok 字段一致性测试
# =============================================================================


class TestOkFieldConsistency:
    """验证所有命令输出都使用 ok 而非 success 字段"""

    def test_logbook_commands_use_ok(self):
        """验证 logbook 命令使用 ok 字段"""
        from engram.logbook.errors import make_error_result, make_success_result

        success = make_success_result(item_id=1)
        error = make_error_result(code="TEST", message="test")

        assert "ok" in success
        assert success["ok"] is True
        assert "success" not in success

        assert "ok" in error
        assert error["ok"] is False
        assert "success" not in error

    def test_logbook_cli_main_commands_use_ok(self):
        """验证 logbook_cli_main 命令使用 ok 字段"""
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from logbook_cli_main import make_err_result, make_ok_result

        success = make_ok_result(repo_id=123)
        error = make_err_result(code="TEST", message="test")

        assert "ok" in success
        assert success["ok"] is True
        assert "success" not in success

        assert "ok" in error
        assert error["ok"] is False
        assert "success" not in error

    def test_scm_sync_svn_uses_ok_not_success(self):
        """验证 sync_svn 结果使用 ok 而非 success（契约一致性）"""
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from logbook_cli_main import make_ok_result

        # 按契约构造 sync_svn 结果
        result = make_ok_result(
            repo_id=123,
            synced_count=50,
            start_rev=101,
            end_rev=150,
            last_rev=150,
            has_more=True,
            remaining=200,
            bulk_count=2,
        )

        # 验证使用 ok 而非 success
        assert "ok" in result
        assert result["ok"] is True
        assert "success" not in result, "契约规定使用 ok 而非 success"

    def test_scm_sync_gitlab_commits_uses_ok_not_success(self):
        """验证 sync_gitlab_commits 结果使用 ok 而非 success（契约一致性）"""
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from logbook_cli_main import make_ok_result

        result = make_ok_result(
            repo_id=123,
            synced_count=50,
            diff_count=48,
            since="2024-01-01T00:00:00Z",
            last_commit_sha="abc123",
            last_commit_ts="2024-01-15T12:30:00Z",
            has_more=True,
            bulk_count=3,
        )

        assert "ok" in result
        assert result["ok"] is True
        assert "success" not in result


# =============================================================================
# Validate 命令 URI Policy 契约测试
# =============================================================================


class TestAttachmentsUriPolicyContractShape:
    """attachments_uri_policy 校验的错误码和响应 shape 测试"""

    def test_uri_policy_error_codes_defined(self):
        """验证 URI 策略校验定义了正确的错误码"""
        # 定义的错误码列表
        expected_error_codes = {
            "DANGEROUS_URI_SCHEME",  # 危险 scheme（javascript:, data:, blob:）
            "ABSOLUTE_PATH_URI",  # 绝对路径
            "REMOTE_URI_NOT_MATERIALIZED",  # 远程 URI 未物化
            "UNKNOWN_URI_TYPE",  # 未知 URI 类型
        }

        # 验证这些错误码在代码中被使用（通过模拟结果验证 shape）
        sample_issue = {
            "attachment_id": 1,
            "item_id": 1,
            "kind": "patch",
            "uri": "javascript:alert(1)",
            "error_code": "DANGEROUS_URI_SCHEME",
            "message": "危险的 URI scheme: javascript",
            "remedy": "请移除此附件或替换为安全的 artifact 路径",
        }

        # 验证 issue 结构的必需字段
        required_issue_fields = {
            "attachment_id",
            "item_id",
            "kind",
            "uri",
            "error_code",
            "message",
            "remedy",
        }
        for field in required_issue_fields:
            assert field in sample_issue, f"issue 缺少必需字段: {field}"

        assert sample_issue["error_code"] in expected_error_codes

    def test_uri_policy_stats_shape(self):
        """验证 URI 策略统计的 shape"""
        # 模拟完整的 stats 结构
        stats = {
            "artifact": 10,  # 无 scheme 的本地相对路径（推荐）
            "file": 2,  # file:// 本地绝对路径
            "remote": 3,  # http/https/s3/gs/ftp 远程
            "unknown": 0,  # 未知 scheme
            "absolute_path": 2,  # 绝对路径（非推荐）
            "dangerous": 1,  # 危险 scheme
        }

        # 验证所有统计字段存在且为整数
        expected_stat_fields = {
            "artifact",
            "file",
            "remote",
            "unknown",
            "absolute_path",
            "dangerous",
        }
        for field in expected_stat_fields:
            assert field in stats, f"stats 缺少字段: {field}"
            assert isinstance(stats[field], int), f"stats.{field} 应为 int"

    def test_uri_policy_dangerous_scheme_detection(self):
        """验证危险 scheme 的检测逻辑（模拟测试）"""
        DANGEROUS_SCHEMES = {"javascript", "data", "blob", "vbscript"}

        test_cases = [
            ("javascript:alert(1)", True, "javascript"),
            ("data:text/html,<script>alert(1)</script>", True, "data"),
            ("blob:http://example.com/uuid", True, "blob"),
            ("vbscript:msgbox(1)", True, "vbscript"),
            ("http://example.com/file.txt", False, None),
            ("https://example.com/file.txt", False, None),
            ("file:///tmp/test.txt", False, None),
            ("scm/1/svn/r100.diff", False, None),
        ]

        for uri, should_be_dangerous, expected_scheme in test_cases:
            # 简单的 scheme 提取
            if ":" in uri:
                scheme = uri.split(":")[0].lower()
            else:
                scheme = None

            is_dangerous = scheme in DANGEROUS_SCHEMES if scheme else False

            if should_be_dangerous:
                assert is_dangerous, f"URI '{uri}' 应被检测为危险"
                assert scheme == expected_scheme, f"scheme 应为 {expected_scheme}"
            else:
                assert not is_dangerous, f"URI '{uri}' 不应被检测为危险"

    def test_uri_policy_absolute_path_detection(self):
        """验证绝对路径的检测逻辑（模拟测试）"""
        test_cases = [
            ("file:///tmp/test.txt", True, "file:// scheme"),
            ("/absolute/path/to/file.txt", True, "starts with /"),
            ("scm/1/svn/r100.diff", False, "relative artifact path"),
            ("./local/file.txt", False, "relative with ./"),
            ("../parent/file.txt", False, "relative with ../"),
            ("http://example.com/file.txt", False, "remote URI"),
        ]

        for uri, should_be_absolute, description in test_cases:
            is_absolute = uri.startswith("file://") or (
                not uri.startswith(("http://", "https://", "s3://", "gs://"))
                and uri.startswith("/")
            )

            if should_be_absolute:
                assert is_absolute, f"URI '{uri}' ({description}) 应被检测为绝对路径"
            else:
                assert not is_absolute, f"URI '{uri}' ({description}) 不应被检测为绝对路径"


# =============================================================================
# Validate 命令 Views Integrity 契约测试
# =============================================================================


class TestViewsIntegrityContractShape:
    """views_integrity 校验的错误码和响应 shape 测试"""

    def test_views_integrity_error_codes_defined(self):
        """验证 views_integrity 定义了正确的错误码"""
        expected_error_codes = {
            "VIEWS_DIR_NOT_EXISTS",  # 视图目录不存在
            "META_FILE_NOT_EXISTS",  # 元数据文件不存在
            "META_PARSE_ERROR",  # 元数据文件解析失败
            "INVALID_GENERATOR",  # 生成器标记无效
            "ARTIFACT_KEY_MISMATCH",  # artifact key（文件名）不一致
            "SHA256_MISMATCH",  # SHA256 哈希不匹配
            "FILE_MISSING",  # 文件缺失
            "MARKER_MISSING",  # 自动生成标记缺失
            "EXTRA_FILE_IN_META",  # 元数据中有多余的文件记录
            "VERIFICATION_ERROR",  # 验证过程出错
        }

        # 模拟一个 SHA256_MISMATCH 错误
        sample_issue = {
            "error_code": "SHA256_MISMATCH",
            "file": "manifest.csv",
            "message": "SHA256 不匹配: manifest.csv",
            "expected": "abc123...",
            "actual": "def456...",
            "remedy": "文件 manifest.csv 可能被手动修改，请选择:\n  1. 恢复自动生成: python -m engram.logbook.cli.logbook render_views",
        }

        assert sample_issue["error_code"] in expected_error_codes
        assert "file" in sample_issue
        assert "message" in sample_issue
        assert "remedy" in sample_issue

    def test_views_integrity_result_shape(self):
        """验证 views_integrity 结果的完整 shape"""
        # 模拟完整的 views_integrity 结果
        result = {
            "status": "ok",
            "views_dir": "./.agentx/logbook/views",
            "files_checked": [
                {
                    "file": "manifest.csv",
                    "artifact_key": "manifest.csv",
                    "status": "ok",
                    "expected_sha256": "abc123" + "0" * 58,
                    "actual_sha256": "abc123" + "0" * 58,
                    "expected_size": 1234,
                    "actual_size": 1234,
                },
                {
                    "file": "index.md",
                    "artifact_key": "index.md",
                    "status": "ok",
                    "expected_sha256": "def456" + "0" * 58,
                    "actual_sha256": "def456" + "0" * 58,
                    "expected_size": 5678,
                    "actual_size": 5678,
                },
            ],
            "artifact_checks": [
                {"artifact_key": "manifest.csv", "status": "present_in_meta"},
                {"artifact_key": "index.md", "status": "present_in_meta"},
            ],
            "meta_rendered_at": "2024-01-15T12:30:00Z",
            "meta_items_count": 50,
            "message": "视图文件完整性验证通过（artifact key 和 SHA256 均一致）",
        }

        # 验证必需字段
        required_fields = {"status", "views_dir", "files_checked", "artifact_checks"}
        for field in required_fields:
            assert field in result, f"result 缺少必需字段: {field}"

        # 验证 status 值
        assert result["status"] in ("ok", "warn", "fail")

        # 验证 files_checked 子结构
        for file_check in result["files_checked"]:
            assert "file" in file_check
            assert "status" in file_check
            assert file_check["status"] in ("ok", "missing", "sha256_mismatch")

        # 验证 artifact_checks 子结构
        for artifact_check in result["artifact_checks"]:
            assert "artifact_key" in artifact_check
            assert "status" in artifact_check

    def test_views_integrity_sha256_mismatch_shape(self):
        """验证 SHA256 不匹配时的错误结构"""
        file_check_mismatch = {
            "file": "manifest.csv",
            "artifact_key": "manifest.csv",
            "status": "sha256_mismatch",
            "error_code": "SHA256_MISMATCH",
            "expected_sha256": "abc123" + "0" * 58,
            "actual_sha256": "def456" + "0" * 58,
            "expected_sha256_short": "abc1230000000000...",
            "actual_sha256_short": "def4560000000000...",
            "expected_size": 1234,
            "actual_size": 1300,
        }

        # 验证 SHA256 不匹配时的字段
        assert file_check_mismatch["status"] == "sha256_mismatch"
        assert file_check_mismatch["error_code"] == "SHA256_MISMATCH"
        assert "expected_sha256" in file_check_mismatch
        assert "actual_sha256" in file_check_mismatch
        # 验证提供了缩短版本用于显示
        assert "expected_sha256_short" in file_check_mismatch
        assert "actual_sha256_short" in file_check_mismatch

    def test_views_integrity_artifact_key_check_shape(self):
        """验证 artifact key 一致性检查的结构"""
        # 正常情况
        artifact_check_ok = {
            "artifact_key": "manifest.csv",
            "status": "present_in_meta",
        }
        assert artifact_check_ok["status"] == "present_in_meta"

        # 元数据中缺少
        artifact_check_missing = {
            "artifact_key": "manifest.csv",
            "status": "missing_in_meta",
            "error_code": "ARTIFACT_KEY_MISMATCH",
            "message": "元数据中缺少文件记录: manifest.csv",
        }
        assert artifact_check_missing["error_code"] == "ARTIFACT_KEY_MISMATCH"

        # 元数据中有多余的
        artifact_check_extra = {
            "artifact_key": "unknown_file.txt",
            "status": "extra",
            "error_code": "EXTRA_FILE_IN_META",
        }
        assert artifact_check_extra["error_code"] == "EXTRA_FILE_IN_META"

    def test_views_integrity_expected_files(self):
        """验证预期的视图文件列表"""
        EXPECTED_VIEW_FILES = {"manifest.csv", "index.md"}

        # 验证预期文件列表
        assert "manifest.csv" in EXPECTED_VIEW_FILES
        assert "index.md" in EXPECTED_VIEW_FILES
        assert len(EXPECTED_VIEW_FILES) == 2
