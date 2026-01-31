"""
test_artifact_migrate.py - 制品迁移模块测试

覆盖：
- dry-run 模式
- verify 校验
- 失败处理和重试
- DB 更新 SQL 正确性（使用 mock conn）
"""

import hashlib
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from artifact_migrate import (
    ArtifactMigrator,
    MigrationDbUpdateError,
    MigrationItem,
    MigrationOpsCredentialsRequiredError,
    MigrationResult,
    create_migrator,
    run_migration,
)
from engram.logbook.artifact_store import (
    LocalArtifactsStore,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def temp_source_dir():
    """创建临时源目录"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def temp_target_dir():
    """创建临时目标目录"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def source_store(temp_source_dir):
    """创建源存储"""
    return LocalArtifactsStore(root=temp_source_dir)


@pytest.fixture
def target_store(temp_target_dir):
    """创建目标存储"""
    return LocalArtifactsStore(root=temp_target_dir)


@pytest.fixture
def sample_files(temp_source_dir):
    """创建示例文件"""
    files = {}

    # 创建目录结构
    (temp_source_dir / "scm" / "proj1").mkdir(parents=True)
    (temp_source_dir / "attachments").mkdir(parents=True)

    # 创建测试文件
    content1 = b"Hello, World!"
    file1 = temp_source_dir / "scm" / "proj1" / "test.diff"
    file1.write_bytes(content1)
    files["scm/proj1/test.diff"] = {
        "content": content1,
        "sha256": hashlib.sha256(content1).hexdigest(),
        "size": len(content1),
    }

    content2 = b"This is a larger file content.\n" * 100
    file2 = temp_source_dir / "scm" / "proj1" / "large.diff"
    file2.write_bytes(content2)
    files["scm/proj1/large.diff"] = {
        "content": content2,
        "sha256": hashlib.sha256(content2).hexdigest(),
        "size": len(content2),
    }

    content3 = b"Attachment content"
    file3 = temp_source_dir / "attachments" / "doc.pdf"
    file3.write_bytes(content3)
    files["attachments/doc.pdf"] = {
        "content": content3,
        "sha256": hashlib.sha256(content3).hexdigest(),
        "size": len(content3),
    }

    return files


# =============================================================================
# Dry-run 模式测试
# =============================================================================


class TestDryRunMode:
    """Dry-run 模式测试"""

    def test_dry_run_does_not_copy_files(
        self, source_store, target_store, sample_files, temp_target_dir
    ):
        """测试 dry-run 模式不实际复制文件"""
        migrator = ArtifactMigrator(
            source_store=source_store,
            target_store=target_store,
            source_backend="local",
            target_backend="local",
            dry_run=True,
        )

        result = migrator.run()

        # 应该扫描到文件
        assert result.scanned_count == len(sample_files)
        assert result.dry_run is True

        # 但目标目录应该是空的
        target_files = list(temp_target_dir.rglob("*"))
        target_files = [f for f in target_files if f.is_file()]
        assert len(target_files) == 0

    def test_dry_run_reports_pending_status(self, source_store, target_store, sample_files):
        """测试 dry-run 模式下 item 状态为 pending"""
        migrator = ArtifactMigrator(
            source_store=source_store,
            target_store=target_store,
            source_backend="local",
            target_backend="local",
            dry_run=True,
        )

        result = migrator.run()

        # 所有 item 应该是 pending 状态
        for item in result.items:
            assert item.status == "pending"

    def test_dry_run_with_prefix(self, source_store, target_store, sample_files):
        """测试 dry-run 带前缀过滤"""
        migrator = ArtifactMigrator(
            source_store=source_store,
            target_store=target_store,
            source_backend="local",
            target_backend="local",
            prefix="scm/",
            dry_run=True,
        )

        result = migrator.run()

        # 只应该扫描 scm/ 下的文件
        assert result.scanned_count == 2
        for item in result.items:
            assert item.key.startswith("scm/")


# =============================================================================
# Verify 校验测试
# =============================================================================


class TestVerifyMode:
    """Verify 校验测试"""

    def test_verify_checks_sha256(self, source_store, target_store, sample_files):
        """测试校验模式检查 SHA256"""
        migrator = ArtifactMigrator(
            source_store=source_store,
            target_store=target_store,
            source_backend="local",
            target_backend="local",
            dry_run=False,
            verify=True,
        )

        result = migrator.run()

        # 所有成功迁移的文件应该是 verified 状态
        assert result.migrated_count == len(sample_files)
        assert result.verified_count == len(sample_files)

        for item in result.items:
            if item.status == "verified":
                assert item.source_sha256 is not None
                assert item.target_sha256 is not None
                assert item.source_sha256 == item.target_sha256

    def test_verify_detects_hash_mismatch(
        self, source_store, target_store, sample_files, temp_target_dir
    ):
        """测试校验模式检测哈希不匹配"""
        # 先迁移 scm/ 目录下的文件
        migrator = ArtifactMigrator(
            source_store=source_store,
            target_store=target_store,
            source_backend="local",
            target_backend="local",
            prefix="scm/",
            dry_run=False,
            verify=False,
        )
        result1 = migrator.run()
        assert result1.migrated_count >= 1

        # 修改目标文件内容
        target_file = temp_target_dir / "scm" / "proj1" / "test.diff"
        assert target_file.exists(), f"Target file should exist: {target_file}"
        target_file.write_bytes(b"Modified content!")

        # 再次运行迁移，应该检测到不匹配
        migrator2 = ArtifactMigrator(
            source_store=source_store,
            target_store=target_store,
            source_backend="local",
            target_backend="local",
            prefix="scm/",
            dry_run=False,
            verify=True,
        )
        result = migrator2.run()

        # 应该重新迁移并校验通过
        assert result.verified_count >= 1

    def test_skip_existing_with_matching_hash(self, source_store, target_store, sample_files):
        """测试跳过哈希匹配的已存在文件"""
        # 先迁移
        migrator1 = ArtifactMigrator(
            source_store=source_store,
            target_store=target_store,
            source_backend="local",
            target_backend="local",
            dry_run=False,
            verify=True,
        )
        result1 = migrator1.run()
        assert result1.migrated_count == len(sample_files)

        # 再次迁移，应该跳过
        migrator2 = ArtifactMigrator(
            source_store=source_store,
            target_store=target_store,
            source_backend="local",
            target_backend="local",
            dry_run=False,
            verify=True,
        )
        result2 = migrator2.run()

        # 所有文件应该被跳过
        assert result2.skipped_count == len(sample_files)
        assert result2.migrated_count == 0


# =============================================================================
# 失败处理测试
# =============================================================================


class TestFailureHandling:
    """失败处理测试"""

    def test_handles_missing_source_file(self, temp_source_dir, temp_target_dir):
        """测试处理源文件不存在的情况"""
        source_store = LocalArtifactsStore(root=temp_source_dir)
        target_store = LocalArtifactsStore(root=temp_target_dir)

        # 创建一个文件
        (temp_source_dir / "test.txt").write_bytes(b"test")

        migrator = ArtifactMigrator(
            source_store=source_store,
            target_store=target_store,
            source_backend="local",
            target_backend="local",
            dry_run=False,
        )

        # 扫描后删除源文件
        items = list(migrator.scan_source())
        assert len(items) == 1

        # 删除源文件
        (temp_source_dir / "test.txt").unlink()

        # 尝试迁移
        item = migrator.migrate_item(items[0])

        assert item.status == "failed"
        assert "不存在" in item.error or "not found" in item.error.lower()

    def test_records_errors_in_result(self, source_store, target_store, sample_files):
        """测试错误被记录到结果中"""
        # 使用一个会失败的 mock target store
        mock_target = MagicMock(spec=LocalArtifactsStore)
        mock_target.exists.return_value = False
        mock_target.put.side_effect = Exception("Write failed!")

        migrator = ArtifactMigrator(
            source_store=source_store,
            target_store=mock_target,
            source_backend="local",
            target_backend="local",
            dry_run=False,
        )

        result = migrator.run()

        assert result.failed_count == len(sample_files)
        assert len(result.errors) == len(sample_files)
        for error in result.errors:
            assert "Write failed!" in error.get("error", "")

    def test_continues_after_failure(self, source_store, temp_target_dir, sample_files):
        """测试单个文件失败后继续处理其他文件"""
        target_store = LocalArtifactsStore(root=temp_target_dir)

        # 创建一个只读目录，使某些文件无法写入
        call_count = [0]
        original_put = target_store.put

        def failing_put(uri, content, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("First write fails!")
            return original_put(uri, content, *args, **kwargs)

        target_store.put = failing_put

        migrator = ArtifactMigrator(
            source_store=source_store,
            target_store=target_store,
            source_backend="local",
            target_backend="local",
            dry_run=False,
        )

        result = migrator.run()

        # 应该有 1 个失败，其余成功
        assert result.failed_count == 1
        assert result.migrated_count == len(sample_files) - 1


# =============================================================================
# DB 更新测试（使用 mock conn）
# =============================================================================


class TestDbUpdate:
    """数据库更新测试"""

    def test_preview_db_update_sql(self, source_store, target_store, sample_files):
        """测试数据库更新预览生成正确的映射"""
        from artifact_migrate import DB_UPDATE_MODE_TO_ARTIFACT_KEY

        migrator = ArtifactMigrator(
            source_store=source_store,
            target_store=target_store,
            source_backend="local",
            target_backend="object",
            prefix="scm/",
            dry_run=True,
            db_update_mode=DB_UPDATE_MODE_TO_ARTIFACT_KEY,  # 需要设置 db_update_mode
        )

        # Mock 数据库连接，fetchall 返回 (id, uri) 行列表
        mock_cursor = MagicMock()
        # 第一次调用返回 patch_blobs 行，第二次返回 attachments 行
        mock_cursor.fetchall.side_effect = [
            [(1, "artifact://scm/proj1/a.diff"), (2, "artifact://scm/proj1/b.diff")],  # patch_blobs
            [(10, "artifact://attachments/doc.pdf")],  # attachments
        ]
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        migrator._conn = mock_conn

        preview = migrator.preview_db_update(["scm/proj1/test.diff"])

        # artifact:// 会被转换为无 scheme 的 artifact key
        assert preview.patch_blobs_count == 2
        assert preview.attachments_count == 1
        assert preview.converted_count == 3

    def test_update_db_uris_executes_correct_sql(self, source_store, target_store):
        """测试数据库更新执行正确的 SQL"""
        migrator = ArtifactMigrator(
            source_store=source_store,
            target_store=target_store,
            source_backend="local",
            target_backend="object",
            dry_run=False,
            update_db=True,
        )

        # 创建迁移结果
        items = [
            MigrationItem(
                key="scm/proj1/test.diff",
                source_uri="scm/proj1/test.diff",
                target_uri="s3://bucket/scm/proj1/test.diff",
                status="verified",
            ),
            MigrationItem(
                key="attachments/doc.pdf",
                source_uri="attachments/doc.pdf",
                target_uri="s3://bucket/attachments/doc.pdf",
                status="migrated",
            ),
        ]

        # Mock 数据库连接
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        migrator._conn = mock_conn

        updated_count = migrator.update_db_uris(items)

        # 验证 SQL 执行
        assert mock_cursor.execute.call_count == 4  # 2 items * 2 tables

        # 验证 commit 被调用
        mock_conn.commit.assert_called_once()

        # 验证返回的更新数
        assert updated_count == 4  # 每个 execute 返回 rowcount=1

    def test_update_db_skips_failed_items(self, source_store, target_store):
        """测试数据库更新跳过失败的项"""
        migrator = ArtifactMigrator(
            source_store=source_store,
            target_store=target_store,
            source_backend="local",
            target_backend="object",
            dry_run=False,
            update_db=True,
        )

        items = [
            MigrationItem(
                key="scm/proj1/test.diff",
                source_uri="scm/proj1/test.diff",
                target_uri="s3://bucket/scm/proj1/test.diff",
                status="failed",
                error="Some error",
            ),
            MigrationItem(
                key="attachments/doc.pdf",
                source_uri="attachments/doc.pdf",
                target_uri="s3://bucket/attachments/doc.pdf",
                status="verified",
            ),
        ]

        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        migrator._conn = mock_conn

        migrator.update_db_uris(items)

        # 只应该执行 2 次（1 个成功的 item * 2 tables）
        assert mock_cursor.execute.call_count == 2

    def test_update_db_rollback_on_error(self, source_store, target_store):
        """测试数据库更新失败时回滚"""
        migrator = ArtifactMigrator(
            source_store=source_store,
            target_store=target_store,
            source_backend="local",
            target_backend="object",
            dry_run=False,
            update_db=True,
        )

        items = [
            MigrationItem(
                key="scm/proj1/test.diff",
                source_uri="scm/proj1/test.diff",
                target_uri="s3://bucket/scm/proj1/test.diff",
                status="verified",
            ),
        ]

        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = Exception("DB error!")
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        migrator._conn = mock_conn

        with pytest.raises(MigrationDbUpdateError):
            migrator.update_db_uris(items)

        # 验证 rollback 被调用
        mock_conn.rollback.assert_called_once()

    def test_dry_run_skips_db_update(self, source_store, target_store):
        """测试 dry-run 模式跳过数据库更新"""
        migrator = ArtifactMigrator(
            source_store=source_store,
            target_store=target_store,
            source_backend="local",
            target_backend="object",
            dry_run=True,
            update_db=True,
        )

        items = [
            MigrationItem(
                key="scm/proj1/test.diff",
                source_uri="scm/proj1/test.diff",
                status="pending",
            ),
        ]

        # 不应该调用数据库
        updated_count = migrator.update_db_uris(items)
        assert updated_count == 0


# =============================================================================
# 并发测试
# =============================================================================


class TestConcurrency:
    """并发迁移测试"""

    def test_concurrent_migration(self, source_store, target_store, sample_files):
        """测试并发迁移"""
        migrator = ArtifactMigrator(
            source_store=source_store,
            target_store=target_store,
            source_backend="local",
            target_backend="local",
            dry_run=False,
            concurrency=3,
        )

        result = migrator.run()

        assert result.migrated_count == len(sample_files)
        assert result.failed_count == 0


# =============================================================================
# Limit 测试
# =============================================================================


class TestLimit:
    """限制数量测试"""

    def test_limit_restricts_file_count(self, source_store, target_store, sample_files):
        """测试 limit 参数限制文件数量"""
        migrator = ArtifactMigrator(
            source_store=source_store,
            target_store=target_store,
            source_backend="local",
            target_backend="local",
            dry_run=True,
            limit=1,
        )

        result = migrator.run()

        assert result.scanned_count == 1


# =============================================================================
# Delete Source 测试
# =============================================================================


class TestDeleteSource:
    """删除源文件测试"""

    def test_delete_source_after_migration(self, temp_source_dir, temp_target_dir, sample_files):
        """测试迁移后删除源文件"""
        source_store = LocalArtifactsStore(root=temp_source_dir)
        target_store = LocalArtifactsStore(root=temp_target_dir)

        migrator = ArtifactMigrator(
            source_store=source_store,
            target_store=target_store,
            source_backend="local",
            target_backend="local",
            dry_run=False,
            delete_source=True,
        )

        result = migrator.run()

        assert result.deleted_count == len(sample_files)

        # 源目录应该没有文件了
        source_files = list(temp_source_dir.rglob("*"))
        source_files = [f for f in source_files if f.is_file()]
        assert len(source_files) == 0

    def test_trash_prefix_moves_to_trash(self, temp_source_dir, temp_target_dir, sample_files):
        """测试 trash_prefix 移动文件而非删除"""
        source_store = LocalArtifactsStore(root=temp_source_dir)
        target_store = LocalArtifactsStore(root=temp_target_dir)

        migrator = ArtifactMigrator(
            source_store=source_store,
            target_store=target_store,
            source_backend="local",
            target_backend="local",
            dry_run=False,
            delete_source=True,
            trash_prefix=".trash",
        )

        result = migrator.run()

        assert result.trashed_count == len(sample_files)

        # 检查 trash 目录
        trash_dir = temp_source_dir / ".trash"
        assert trash_dir.exists()

        trash_files = list(trash_dir.rglob("*"))
        trash_files = [f for f in trash_files if f.is_file()]
        assert len(trash_files) == len(sample_files)


# =============================================================================
# 工厂函数测试
# =============================================================================


class TestFactoryFunctions:
    """工厂函数测试"""

    def test_create_migrator(self, temp_source_dir, temp_target_dir):
        """测试 create_migrator 函数"""
        migrator = create_migrator(
            source_backend="local",
            target_backend="local",
            source_root=str(temp_source_dir),
            target_root=str(temp_target_dir),
            dry_run=True,
        )

        assert isinstance(migrator, ArtifactMigrator)
        assert migrator.dry_run is True

    def test_run_migration_convenience(self, temp_source_dir, temp_target_dir, sample_files):
        """测试 run_migration 便捷函数"""
        # 创建测试文件
        (temp_source_dir / "test.txt").write_bytes(b"test content")

        result = run_migration(
            source_backend="local",
            target_backend="local",
            source_root=str(temp_source_dir),
            target_root=str(temp_target_dir),
            dry_run=False,
        )

        assert isinstance(result, MigrationResult)
        # sample_files 已经在 temp_source_dir 创建了文件
        # 加上我们创建的 test.txt
        assert result.scanned_count >= 1


# =============================================================================
# MigrationResult 测试
# =============================================================================


class TestMigrationResult:
    """MigrationResult 测试"""

    def test_to_dict(self):
        """测试 to_dict 方法"""
        result = MigrationResult(
            scanned_count=10,
            migrated_count=8,
            verified_count=8,
            skipped_count=1,
            failed_count=1,
            total_size_bytes=1024,
            migrated_size_bytes=900,
            duration_seconds=5.5,
            dry_run=False,
            errors=[{"key": "test.txt", "error": "Failed"}],
        )

        d = result.to_dict()

        assert d["scanned_count"] == 10
        assert d["migrated_count"] == 8
        assert d["verified_count"] == 8
        assert d["failed_count"] == 1
        assert d["dry_run"] is False
        assert d["error_count"] == 1

    def test_to_dict_truncates_errors(self):
        """测试 to_dict 截断大量错误"""
        errors = [{"key": f"file{i}.txt", "error": "Failed"} for i in range(150)]
        result = MigrationResult(errors=errors)

        d = result.to_dict()

        assert len(d["errors"]) == 100
        assert d["error_count"] == 150


# =============================================================================
# require_ops 安全开关测试
# =============================================================================


class TestRequireOpsFlag:
    """--require-ops 安全开关测试"""

    def test_require_ops_with_object_source_and_app_credentials_raises_error(self):
        """
        require_ops=True 且 delete_source=True 时，
        如果 object 源存储使用 app 凭证应抛出错误
        """
        from engram.logbook.artifact_store import ObjectStore

        with patch.dict(
            os.environ,
            {
                "ENGRAM_S3_USE_OPS": "false",
                "ENGRAM_S3_APP_ACCESS_KEY": "app-key",
                "ENGRAM_S3_APP_SECRET_KEY": "app-secret",
                "ENGRAM_S3_ENDPOINT": "http://localhost:9000",
                "ENGRAM_S3_BUCKET": "test-bucket",
            },
            clear=False,
        ):
            # 创建 mock 的 ObjectStore
            source_store = ObjectStore(
                endpoint="http://localhost:9000",
                bucket="source-bucket",
            )
            target_store = LocalArtifactsStore(root=tempfile.mkdtemp())

            with pytest.raises(MigrationOpsCredentialsRequiredError) as exc_info:
                ArtifactMigrator(
                    source_store=source_store,
                    target_store=target_store,
                    source_backend="object",
                    target_backend="local",
                    delete_source=True,
                    require_ops=True,
                )

            assert "ops 凭证" in str(exc_info.value)

    def test_require_ops_with_object_source_and_ops_credentials_succeeds(self):
        """
        require_ops=True 且 delete_source=True 时，
        如果 object 源存储使用 ops 凭证应正常创建 migrator
        """
        from engram.logbook.artifact_store import ObjectStore

        with patch.dict(
            os.environ,
            {
                "ENGRAM_S3_USE_OPS": "true",
                "ENGRAM_S3_OPS_ACCESS_KEY": "ops-key",
                "ENGRAM_S3_OPS_SECRET_KEY": "ops-secret",
                "ENGRAM_S3_ENDPOINT": "http://localhost:9000",
                "ENGRAM_S3_BUCKET": "test-bucket",
            },
            clear=False,
        ):
            source_store = ObjectStore(
                endpoint="http://localhost:9000",
                bucket="source-bucket",
            )
            target_store = LocalArtifactsStore(root=tempfile.mkdtemp())

            # 应正常创建 migrator
            migrator = ArtifactMigrator(
                source_store=source_store,
                target_store=target_store,
                source_backend="object",
                target_backend="local",
                delete_source=True,
                require_ops=True,
            )

            assert migrator.require_ops is True
            assert migrator.delete_source is True

    def test_require_ops_without_delete_source_does_not_check(self):
        """
        require_ops=True 但 delete_source=False 时不检查凭证
        """
        from engram.logbook.artifact_store import ObjectStore

        with patch.dict(
            os.environ,
            {
                "ENGRAM_S3_USE_OPS": "false",
                "ENGRAM_S3_APP_ACCESS_KEY": "app-key",
                "ENGRAM_S3_APP_SECRET_KEY": "app-secret",
                "ENGRAM_S3_ENDPOINT": "http://localhost:9000",
                "ENGRAM_S3_BUCKET": "test-bucket",
            },
            clear=False,
        ):
            source_store = ObjectStore(
                endpoint="http://localhost:9000",
                bucket="source-bucket",
            )
            target_store = LocalArtifactsStore(root=tempfile.mkdtemp())

            # delete_source=False，不检查凭证
            migrator = ArtifactMigrator(
                source_store=source_store,
                target_store=target_store,
                source_backend="object",
                target_backend="local",
                delete_source=False,
                require_ops=True,
            )

            assert migrator.require_ops is True
            assert migrator.delete_source is False

    def test_require_ops_with_local_source_does_not_check(self):
        """
        local 源存储不检查 ops 凭证
        """
        source_store = LocalArtifactsStore(root=tempfile.mkdtemp())
        target_store = LocalArtifactsStore(root=tempfile.mkdtemp())

        # local 后端不检查 ops 凭证
        migrator = ArtifactMigrator(
            source_store=source_store,
            target_store=target_store,
            source_backend="local",
            target_backend="local",
            delete_source=True,
            require_ops=True,
        )

        assert migrator.require_ops is True

    def test_run_migration_with_require_ops(self, temp_source_dir, temp_target_dir):
        """测试 run_migration 便捷函数的 require_ops 参数"""
        # 创建测试文件
        (temp_source_dir / "test.txt").write_bytes(b"test content")

        # local -> local 迁移，require_ops 不影响
        result = run_migration(
            source_backend="local",
            target_backend="local",
            source_root=str(temp_source_dir),
            target_root=str(temp_target_dir),
            dry_run=False,
            require_ops=True,  # local 后端不检查
        )

        assert result.scanned_count >= 1

    def test_create_migrator_with_require_ops(self, temp_source_dir, temp_target_dir):
        """测试 create_migrator 工厂函数的 require_ops 参数"""
        migrator = create_migrator(
            source_backend="local",
            target_backend="local",
            source_root=str(temp_source_dir),
            target_root=str(temp_target_dir),
            dry_run=True,
            require_ops=True,
        )

        assert migrator.require_ops is True
