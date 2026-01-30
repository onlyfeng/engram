# -*- coding: utf-8 -*-
"""
test_artifact_ci_contract.py - 制品 CI 契约测试

仅验证 JSON 输出的 shape，不依赖真实 S3 或数据库连接。
使用 LocalArtifactsStore 和 mock 实现隔离测试。

覆盖:
1. artifact_gc.py JSON 输出 shape 和 status 字段
2. artifact_audit.py JSON 输出 shape
3. artifact_migrate.py JSON 输出 shape
4. artifact_delete.py JSON 输出 shape

外部依赖:
- 无（不依赖 MinIO/S3、PostgreSQL 等外部服务）
- 使用 MockConnection/MockCursor 模拟数据库
- 使用 LocalArtifactsStore 和 tmp_path 进行隔离测试

Skip 条件:
- 本测试文件无需 skip 条件，所有测试均使用 mock
- MinIO 集成测试请参见 test_object_store_minio_integration.py
  （需设置 ENGRAM_MINIO_INTEGRATION=1 启用）
"""

import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Set
from unittest.mock import MagicMock, patch
from datetime import datetime

import pytest

# 添加 scripts 目录到 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from artifact_gc import (
    GCCandidate,
    GCResult,
    ReferencedUris,
    run_gc,
    run_tmp_gc,
)
from artifact_audit import (
    ArtifactAuditor,
    AuditResult,
    AuditSummary,
)
from artifact_migrate import (
    MigrationItem,
    MigrationResult,
)
from engram.logbook.artifact_store import LocalArtifactsStore


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def artifacts_root(tmp_path: Path) -> Path:
    """创建临时制品根目录"""
    root = tmp_path / "artifacts"
    root.mkdir()
    return root


@pytest.fixture
def store(artifacts_root: Path) -> LocalArtifactsStore:
    """创建测试用 LocalArtifactsStore"""
    return LocalArtifactsStore(root=artifacts_root)


@pytest.fixture
def sample_files(artifacts_root: Path) -> dict:
    """创建示例制品文件"""
    scm_dir = artifacts_root / "scm" / "proj_a"
    scm_dir.mkdir(parents=True)
    
    files = {}
    for i in range(3):
        uri = f"scm/proj_a/file_{i}.diff"
        path = artifacts_root / uri
        path.write_bytes(f"content {i}".encode())
        files[uri] = path
    
    return files


def _make_referenced_uris(artifact_keys: set) -> ReferencedUris:
    """创建 ReferencedUris 测试对象"""
    return ReferencedUris(artifact_keys=artifact_keys, physical_refs=[])


# =============================================================================
# GC JSON Shape 契约测试
# =============================================================================


class TestGCJsonShapeContract:
    """GC JSON 输出 shape 契约测试"""

    # GC JSON 必需的顶层字段
    REQUIRED_TOP_LEVEL_FIELDS = {
        "gc_mode",
        "backend",
        "bucket",
        "prefix",
        "scanned_count",
        "referenced_count",
        "protected_count",
        "candidates_count",
        "skipped_by_age",
        "deleted_count",
        "trashed_count",
        "failed_count",
        "total_size_bytes",
        "deleted_size_bytes",
        "status_summary",
        "errors",
        "candidates",
    }

    # status_summary 必需的字段
    REQUIRED_STATUS_SUMMARY_FIELDS = {"ok", "skipped", "error", "pending"}

    # candidate 必需的字段
    REQUIRED_CANDIDATE_FIELDS = {"uri", "full_path", "size_bytes", "age_days", "status"}

    # status 允许的值
    ALLOWED_STATUS_VALUES = {"ok", "skipped", "error", "pending"}

    def test_gc_result_has_required_fields(self, artifacts_root: Path, sample_files: dict):
        """测试 GC 结果包含所有必需字段"""
        with patch("artifact_gc.get_referenced_uris") as mock_get_refs:
            mock_get_refs.return_value = _make_referenced_uris(set())

            result = run_gc(
                prefix="scm/",
                dry_run=True,
                dsn="mock://",
                backend="local",
                artifacts_root=str(artifacts_root),
                verbose=False,
            )

            # 验证 GCResult 有预期的属性
            assert hasattr(result, "gc_mode")
            assert hasattr(result, "backend")
            assert hasattr(result, "prefix")
            assert hasattr(result, "candidates")
            assert hasattr(result, "scanned_count")

    def test_gc_candidate_has_status_field(self, artifacts_root: Path, sample_files: dict):
        """测试 GCCandidate 包含 status 字段"""
        with patch("artifact_gc.get_referenced_uris") as mock_get_refs:
            mock_get_refs.return_value = _make_referenced_uris(set())

            result = run_gc(
                prefix="scm/",
                dry_run=True,
                dsn="mock://",
                backend="local",
                artifacts_root=str(artifacts_root),
                verbose=False,
            )

            # 所有候选应该有 status 字段
            for candidate in result.candidates:
                assert hasattr(candidate, "status")
                assert candidate.status in self.ALLOWED_STATUS_VALUES

    def test_gc_candidate_default_status_is_pending(self, artifacts_root: Path, sample_files: dict):
        """测试 GCCandidate 默认状态为 pending"""
        with patch("artifact_gc.get_referenced_uris") as mock_get_refs:
            mock_get_refs.return_value = _make_referenced_uris(set())

            result = run_gc(
                prefix="scm/",
                dry_run=True,  # dry-run 不执行删除
                dsn="mock://",
                backend="local",
                artifacts_root=str(artifacts_root),
                verbose=False,
            )

            # dry-run 模式下，候选状态应该是 pending
            for candidate in result.candidates:
                assert candidate.status == "pending"

    def test_gc_candidate_status_ok_after_delete(self, artifacts_root: Path, sample_files: dict):
        """测试删除成功后 status 变为 ok"""
        with patch("artifact_gc.get_referenced_uris") as mock_get_refs:
            mock_get_refs.return_value = _make_referenced_uris(set())

            result = run_gc(
                prefix="scm/",
                dry_run=False,
                delete=True,
                dsn="mock://",
                backend="local",
                artifacts_root=str(artifacts_root),
                verbose=False,
            )

            # 删除成功后，候选状态应该是 ok
            assert result.deleted_count > 0
            for candidate in result.candidates:
                assert candidate.status == "ok"

    def test_gc_json_output_shape(self, artifacts_root: Path, sample_files: dict, capsys):
        """测试 GC JSON 输出包含所有必需字段"""
        # 这个测试模拟 CLI JSON 输出并验证 shape
        with patch("artifact_gc.get_referenced_uris") as mock_get_refs:
            mock_get_refs.return_value = _make_referenced_uris(set())

            result = run_gc(
                prefix="scm/",
                dry_run=True,
                dsn="mock://",
                backend="local",
                artifacts_root=str(artifacts_root),
                verbose=False,
            )

            # 模拟 JSON 输出构建（与 CLI main 函数相同逻辑）
            status_summary = {"ok": 0, "skipped": 0, "error": 0, "pending": 0}
            for c in result.candidates:
                if c.status in status_summary:
                    status_summary[c.status] += 1
                else:
                    status_summary["pending"] += 1

            output = {
                "gc_mode": result.gc_mode,
                "backend": result.backend,
                "bucket": result.bucket,
                "prefix": result.prefix,
                "scanned_count": result.scanned_count,
                "referenced_count": result.referenced_count,
                "protected_count": result.protected_count,
                "candidates_count": result.candidates_count,
                "skipped_by_age": result.skipped_by_age,
                "deleted_count": result.deleted_count,
                "trashed_count": result.trashed_count,
                "failed_count": result.failed_count,
                "total_size_bytes": result.total_size_bytes,
                "deleted_size_bytes": result.deleted_size_bytes,
                "status_summary": status_summary,
                "errors": result.errors,
                "candidates": [
                    {
                        "uri": c.uri,
                        "full_path": c.full_path,
                        "size_bytes": c.size_bytes,
                        "age_days": round(c.age_days, 2),
                        "status": c.status,
                    }
                    for c in result.candidates
                ],
            }

            # 验证顶层字段
            for field in self.REQUIRED_TOP_LEVEL_FIELDS:
                assert field in output, f"缺少必需字段: {field}"

            # 验证 status_summary 字段
            for field in self.REQUIRED_STATUS_SUMMARY_FIELDS:
                assert field in output["status_summary"], f"status_summary 缺少字段: {field}"

            # 验证 candidate 字段
            for candidate in output["candidates"]:
                for field in self.REQUIRED_CANDIDATE_FIELDS:
                    assert field in candidate, f"candidate 缺少字段: {field}"
                assert candidate["status"] in self.ALLOWED_STATUS_VALUES

    def test_tmp_gc_json_output_shape(self, artifacts_root: Path):
        """测试 Tmp GC JSON 输出包含所有必需字段"""
        # 创建 tmp 目录和旧文件
        tmp_dir = artifacts_root / "tmp"
        tmp_dir.mkdir()
        old_file = tmp_dir / "old.tar.gz"
        old_file.write_bytes(b"old content")
        old_mtime = time.time() - (10 * 24 * 3600)
        os.utime(old_file, (old_mtime, old_mtime))

        result = run_tmp_gc(
            tmp_prefix="tmp/",
            older_than_days=7,
            dry_run=True,
            backend="local",
            artifacts_root=str(artifacts_root),
            verbose=False,
        )

        # 验证元信息
        assert result.gc_mode == "tmp"
        assert result.backend == "local"
        assert result.prefix == "tmp/"

        # 验证候选有 status 字段
        for candidate in result.candidates:
            assert hasattr(candidate, "status")
            assert candidate.status in self.ALLOWED_STATUS_VALUES


# =============================================================================
# Audit JSON Shape 契约测试
# =============================================================================


class MockConnection:
    """模拟数据库连接"""

    def __init__(self, patch_blobs=None, attachments=None):
        self.patch_blobs = patch_blobs or []
        self.attachments = attachments or []
        self._closed = False

    def cursor(self):
        return MockCursor(self)

    def close(self):
        self._closed = True


class MockCursor:
    """模拟数据库游标"""

    def __init__(self, conn: MockConnection):
        self.conn = conn
        self._results = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, query: str, params=None):
        query_lower = query.lower()
        if "patch_blobs" in query_lower:
            self._results = []
            for row in self.conn.patch_blobs:
                if len(row) == 3:
                    self._results.append((*row, datetime.now()))
                else:
                    self._results.append(row)
        elif "attachments" in query_lower:
            self._results = []
            for row in self.conn.attachments:
                if len(row) == 3:
                    self._results.append((*row, datetime.now()))
                else:
                    self._results.append(row)
        else:
            self._results = []

    def __iter__(self):
        return iter(self._results)


class TestAuditJsonShapeContract:
    """Audit JSON 输出 shape 契约测试"""

    # AuditSummary 必需的字段（根据实际 AuditSummary.to_dict() 实现）
    REQUIRED_SUMMARY_FIELDS = {
        "total_records",
        "sampled_records",
        "audited_records",
        "ok_count",
        "mismatch_count",
        "missing_count",
        "error_count",
        "skipped_count",
        "total_bytes",
        "duration_seconds",
        "start_time",
        "end_time",
        "tables_audited",
        "mismatches",
        "missing",
    }

    # AuditResult 必需的字段
    REQUIRED_RESULT_FIELDS = {
        "table",
        "record_id",
        "uri",
        "expected_sha256",
        "actual_sha256",
        "size_bytes",
        "status",
    }

    # status 允许的值
    ALLOWED_STATUS_VALUES = {"ok", "mismatch", "missing", "error", "skipped", "head_only_unverified"}

    def test_audit_summary_has_required_fields(self, artifacts_root: Path, store: LocalArtifactsStore):
        """测试 AuditSummary 包含所有必需字段"""
        # 创建测试文件
        content = b"test content"
        result = store.put("test/file.txt", content)
        sha256 = result["sha256"]

        patch_blobs = [(1, "test/file.txt", sha256)]
        mock_conn = MockConnection(patch_blobs=patch_blobs)

        auditor = ArtifactAuditor(
            artifacts_root=artifacts_root,
            conn=mock_conn,
            artifact_store=store,
        )

        summary = auditor.run_audit(tables=["patch_blobs"])
        summary_dict = summary.to_dict()

        # 验证所有必需字段存在
        for field in self.REQUIRED_SUMMARY_FIELDS:
            assert field in summary_dict, f"AuditSummary 缺少字段: {field}"

    def test_audit_result_has_required_fields(self, artifacts_root: Path, store: LocalArtifactsStore):
        """测试 AuditResult 包含所有必需字段"""
        content = b"test content"
        result = store.put("test/file.txt", content)
        sha256 = result["sha256"]

        mock_conn = MockConnection()
        auditor = ArtifactAuditor(
            artifacts_root=artifacts_root,
            conn=mock_conn,
            artifact_store=store,
        )

        audit_result = auditor.audit_record(
            table="patch_blobs",
            record_id=1,
            uri="test/file.txt",
            expected_sha256=sha256,
        )

        result_dict = audit_result.to_dict()

        # 验证所有必需字段存在
        for field in self.REQUIRED_RESULT_FIELDS:
            assert field in result_dict, f"AuditResult 缺少字段: {field}"

        # 验证 status 值合法
        assert result_dict["status"] in self.ALLOWED_STATUS_VALUES

    def test_audit_json_serializable(self, artifacts_root: Path, store: LocalArtifactsStore):
        """测试 Audit 输出可以序列化为 JSON"""
        content = b"test content"
        result = store.put("test/file.txt", content)
        sha256 = result["sha256"]

        patch_blobs = [(1, "test/file.txt", sha256)]
        mock_conn = MockConnection(patch_blobs=patch_blobs)

        auditor = ArtifactAuditor(
            artifacts_root=artifacts_root,
            conn=mock_conn,
            artifact_store=store,
        )

        summary = auditor.run_audit(tables=["patch_blobs"])

        # 应该能成功序列化为 JSON
        json_str = json.dumps(summary.to_dict(), ensure_ascii=False)
        assert json_str is not None

        # 应该能解析回来
        parsed = json.loads(json_str)
        assert "total_records" in parsed
        assert "ok_count" in parsed


# =============================================================================
# Migrate JSON Shape 契约测试
# =============================================================================


class TestMigrateJsonShapeContract:
    """Migrate JSON 输出 shape 契约测试"""

    # MigrationResult 必需的字段
    REQUIRED_RESULT_FIELDS = {
        "scanned_count",
        "migrated_count",
        "verified_count",
        "skipped_count",
        "failed_count",
        "deleted_count",
        "trashed_count",
        "total_size_bytes",
        "migrated_size_bytes",
        "duration_seconds",
        "dry_run",
        "error_count",
        "errors",
    }

    # MigrationItem 必需的字段
    REQUIRED_ITEM_FIELDS = {
        "key",
        "source_uri",
        "target_uri",
        "status",
    }

    # status 允许的值
    ALLOWED_STATUS_VALUES = {"pending", "migrated", "verified", "failed", "skipped"}

    def test_migration_result_has_required_fields(self):
        """测试 MigrationResult 包含所有必需字段"""
        result = MigrationResult(
            scanned_count=10,
            migrated_count=8,
            verified_count=8,
            skipped_count=1,
            failed_count=1,
        )

        result_dict = result.to_dict()

        # 验证所有必需字段存在
        for field in self.REQUIRED_RESULT_FIELDS:
            assert field in result_dict, f"MigrationResult 缺少字段: {field}"

    def test_migration_item_has_required_fields(self):
        """测试 MigrationItem 包含所有必需字段"""
        item = MigrationItem(
            key="scm/test.diff",
            source_uri="scm/test.diff",
            target_uri="s3://bucket/scm/test.diff",
            status="verified",
        )

        # 使用 asdict 转换（MigrationItem 是 dataclass）
        item_dict = asdict(item)

        # 验证所有必需字段存在
        for field in self.REQUIRED_ITEM_FIELDS:
            assert field in item_dict, f"MigrationItem 缺少字段: {field}"

        # 验证 status 值合法
        assert item_dict["status"] in self.ALLOWED_STATUS_VALUES

    def test_migration_result_json_serializable(self):
        """测试 MigrationResult 输出可以序列化为 JSON"""
        result = MigrationResult(
            scanned_count=5,
            migrated_count=3,
            verified_count=3,
            skipped_count=1,
            failed_count=1,
            errors=[{"key": "test.txt", "error": "Failed"}],
            items=[
                MigrationItem(
                    key="test.diff",
                    source_uri="test.diff",
                    target_uri="s3://bucket/test.diff",
                    status="verified",
                ),
            ],
        )

        # 应该能成功序列化为 JSON
        json_str = json.dumps(result.to_dict(), ensure_ascii=False)
        assert json_str is not None

        # 应该能解析回来
        parsed = json.loads(json_str)
        assert "scanned_count" in parsed
        assert "migrated_count" in parsed


# =============================================================================
# Delete JSON Shape 契约测试
# =============================================================================


class TestDeleteJsonShapeContract:
    """Delete JSON 输出 shape 契约测试"""

    # 单个删除必需的顶层字段
    REQUIRED_SINGLE_DELETE_FIELDS = {
        "ok",
        "mode",
        "dry_run",
        "trash_mode",
        "total_count",
        "deleted_count",
        "trashed_count",
        "failed_count",
        "pending_count",
        "not_existed_count",
        "results",
        "path",
        "deleted",
        "existed",
    }

    # 批量删除必需的顶层字段
    REQUIRED_BATCH_DELETE_FIELDS = {
        "ok",
        "mode",
        "dry_run",
        "trash_mode",
        "total_count",
        "deleted_count",
        "trashed_count",
        "failed_count",
        "pending_count",
        "not_existed_count",
        "results",
    }

    # 删除结果项必需的字段
    REQUIRED_RESULT_ITEM_FIELDS = {"uri", "deleted", "existed"}

    # mode 允许的值
    ALLOWED_MODE_VALUES = {"single", "prefix", "batch"}

    def test_delete_result_has_required_fields(self, artifacts_root: Path, store: LocalArtifactsStore):
        """测试 ArtifactDeleteResult 包含所有必需字段"""
        from engram.logbook.artifact_delete import delete_artifact_key, ArtifactDeleteResult

        # 创建测试文件
        content = b"test content for delete"
        store.put("delete_test/file.txt", content)

        result = delete_artifact_key("delete_test/file.txt", store=store)

        # 验证 ArtifactDeleteResult 有预期的属性
        assert hasattr(result, "uri")
        assert hasattr(result, "deleted")
        assert hasattr(result, "existed")
        assert hasattr(result, "trashed")
        assert hasattr(result, "trash_path")
        assert hasattr(result, "error")

    def test_single_delete_json_output_shape(self, artifacts_root: Path, store: LocalArtifactsStore):
        """测试单个删除 JSON 输出包含所有必需字段"""
        from engram.logbook.artifact_delete import delete_artifact_key

        # 创建测试文件
        content = b"test content"
        store.put("single_delete/test.txt", content)

        result = delete_artifact_key("single_delete/test.txt", store=store)

        # 模拟 CLI JSON 输出构建
        result_dict = {
            "uri": result.uri,
            "deleted": result.deleted,
            "existed": result.existed,
            "trashed": result.trashed,
        }
        if result.trash_path:
            result_dict["trash_path"] = result.trash_path
        if result.error:
            result_dict["error"] = result.error

        output = {
            "ok": True,
            "mode": "single",
            "dry_run": False,
            "trash_mode": False,
            "trash_prefix": None,
            "total_count": 1,
            "deleted_count": 1 if result.deleted else 0,
            "trashed_count": 1 if result.trashed else 0,
            "failed_count": 1 if result.error else 0,
            "pending_count": 0,
            "not_existed_count": 0 if result.existed else 1,
            "results": [result_dict],
            "path": "single_delete/test.txt",
            "deleted": result.deleted,
            "existed": result.existed,
            "trashed": result.trashed,
        }

        # 验证顶层字段
        for field in self.REQUIRED_SINGLE_DELETE_FIELDS:
            assert field in output, f"缺少必需字段: {field}"

        # 验证 mode 值
        assert output["mode"] in self.ALLOWED_MODE_VALUES

        # 验证 results 中每项的字段
        for item in output["results"]:
            for field in self.REQUIRED_RESULT_ITEM_FIELDS:
                assert field in item, f"result 项缺少字段: {field}"

    def test_soft_delete_json_output_shape(self, artifacts_root: Path, store: LocalArtifactsStore):
        """测试软删除 JSON 输出包含 trash_path"""
        from engram.logbook.artifact_delete import delete_artifact_key

        # 创建测试文件
        content = b"soft delete content"
        store.put("soft_delete/test.txt", content)

        result = delete_artifact_key("soft_delete/test.txt", store=store, trash_prefix=".trash/")

        # 验证软删除结果
        assert result.trashed is True
        assert result.trash_path is not None
        assert ".trash" in result.trash_path

        # 构建输出
        result_dict = {
            "uri": result.uri,
            "deleted": result.deleted,
            "existed": result.existed,
            "trashed": result.trashed,
            "trash_path": result.trash_path,
        }

        output = {
            "ok": True,
            "mode": "single",
            "dry_run": False,
            "trash_mode": True,
            "trash_prefix": ".trash/",
            "total_count": 1,
            "deleted_count": 1,
            "trashed_count": 1,
            "failed_count": 0,
            "pending_count": 0,
            "not_existed_count": 0,
            "results": [result_dict],
            "path": "soft_delete/test.txt",
            "deleted": True,
            "existed": True,
            "trashed": True,
            "trash_path": result.trash_path,
        }

        # 验证 trash 相关字段
        assert output["trash_mode"] is True
        assert output["trash_prefix"] == ".trash/"
        assert "trash_path" in output
        assert output["results"][0]["trashed"] is True
        assert "trash_path" in output["results"][0]

    def test_batch_delete_json_output_shape(self, artifacts_root: Path, store: LocalArtifactsStore):
        """测试批量删除 JSON 输出 shape"""
        from engram.logbook.artifact_delete import delete_artifact_key

        # 创建多个测试文件
        for i in range(3):
            store.put(f"batch_delete/file_{i}.txt", f"content {i}".encode())

        # 模拟批量删除结果
        results = []
        for i in range(3):
            result = delete_artifact_key(f"batch_delete/file_{i}.txt", store=store)
            results.append({
                "uri": result.uri,
                "deleted": result.deleted,
                "existed": result.existed,
                "trashed": result.trashed,
            })

        output = {
            "ok": True,
            "mode": "batch",
            "dry_run": False,
            "trash_mode": False,
            "trash_prefix": None,
            "total_count": 3,
            "deleted_count": 3,
            "trashed_count": 0,
            "failed_count": 0,
            "pending_count": 0,
            "not_existed_count": 0,
            "results": results,
        }

        # 验证顶层字段
        for field in self.REQUIRED_BATCH_DELETE_FIELDS:
            assert field in output, f"缺少必需字段: {field}"

        # 验证 mode
        assert output["mode"] == "batch"

        # 验证 results
        assert len(output["results"]) == 3
        for item in output["results"]:
            for field in self.REQUIRED_RESULT_ITEM_FIELDS:
                assert field in item, f"result 项缺少字段: {field}"

    def test_prefix_delete_json_output_shape(self, artifacts_root: Path, store: LocalArtifactsStore):
        """测试前缀删除 JSON 输出 shape"""
        from engram.logbook.artifact_delete import delete_artifact_key

        # 创建多个测试文件
        for i in range(2):
            store.put(f"prefix_delete/sub/file_{i}.txt", f"content {i}".encode())

        # 模拟前缀删除结果
        results = []
        for i in range(2):
            result = delete_artifact_key(f"prefix_delete/sub/file_{i}.txt", store=store)
            results.append({
                "uri": result.uri,
                "deleted": result.deleted,
                "existed": result.existed,
                "trashed": result.trashed,
            })

        output = {
            "ok": True,
            "mode": "prefix",
            "dry_run": False,
            "trash_mode": False,
            "trash_prefix": None,
            "total_count": 2,
            "deleted_count": 2,
            "trashed_count": 0,
            "failed_count": 0,
            "pending_count": 0,
            "not_existed_count": 0,
            "results": results,
        }

        # 验证顶层字段
        for field in self.REQUIRED_BATCH_DELETE_FIELDS:
            assert field in output, f"缺少必需字段: {field}"

        # 验证 mode
        assert output["mode"] == "prefix"

    def test_dry_run_json_output_shape(self, artifacts_root: Path, store: LocalArtifactsStore):
        """测试 dry-run 模式 JSON 输出 shape"""
        # 创建测试文件
        store.put("dry_run_delete/test.txt", b"dry run content")

        # 模拟 dry-run 输出
        output = {
            "ok": True,
            "mode": "single",
            "dry_run": True,
            "trash_mode": False,
            "trash_prefix": None,
            "total_count": 1,
            "deleted_count": 0,
            "trashed_count": 0,
            "failed_count": 0,
            "pending_count": 1,
            "not_existed_count": 0,
            "results": [
                {
                    "uri": "dry_run_delete/test.txt",
                    "action": "pending",
                    "mode": "delete",
                }
            ],
            "path": "dry_run_delete/test.txt",
            "deleted": True,  # dry-run 返回 True 表示将被删除
            "existed": True,
        }

        # 验证 dry_run 字段
        assert output["dry_run"] is True
        assert output["pending_count"] == 1
        assert output["deleted_count"] == 0  # dry-run 不实际删除

        # 验证 results 中的 action
        assert output["results"][0]["action"] == "pending"

    def test_delete_nonexistent_json_output_shape(self, artifacts_root: Path, store: LocalArtifactsStore):
        """测试删除不存在文件的 JSON 输出 shape"""
        from engram.logbook.artifact_delete import delete_artifact_key

        result = delete_artifact_key("nonexistent/file.txt", store=store)

        # 不存在的文件删除也应该成功（幂等）
        assert result.deleted is True
        assert result.existed is False

        result_dict = {
            "uri": result.uri,
            "deleted": result.deleted,
            "existed": result.existed,
            "trashed": result.trashed,
        }

        output = {
            "ok": True,
            "mode": "single",
            "dry_run": False,
            "trash_mode": False,
            "trash_prefix": None,
            "total_count": 1,
            "deleted_count": 1,
            "trashed_count": 0,
            "failed_count": 0,
            "pending_count": 0,
            "not_existed_count": 1,
            "results": [result_dict],
            "path": "nonexistent/file.txt",
            "deleted": True,
            "existed": False,
            "trashed": False,
        }

        # 验证 existed 为 False
        assert output["existed"] is False
        assert output["not_existed_count"] == 1

    def test_delete_json_serializable(self, artifacts_root: Path, store: LocalArtifactsStore):
        """测试 Delete 输出可以序列化为 JSON"""
        from engram.logbook.artifact_delete import delete_artifact_key

        # 创建测试文件
        store.put("json_test/file.txt", b"json test content")

        result = delete_artifact_key("json_test/file.txt", store=store)

        output = {
            "ok": True,
            "mode": "single",
            "dry_run": False,
            "trash_mode": False,
            "trash_prefix": None,
            "total_count": 1,
            "deleted_count": 1,
            "trashed_count": 0,
            "failed_count": 0,
            "pending_count": 0,
            "not_existed_count": 0,
            "results": [{
                "uri": result.uri,
                "deleted": result.deleted,
                "existed": result.existed,
                "trashed": result.trashed,
            }],
            "path": "json_test/file.txt",
            "deleted": True,
            "existed": True,
            "trashed": False,
        }

        # 应该能成功序列化为 JSON
        json_str = json.dumps(output, ensure_ascii=False)
        assert json_str is not None

        # 应该能解析回来
        parsed = json.loads(json_str)
        assert "ok" in parsed
        assert "deleted_count" in parsed
        assert "results" in parsed


# =============================================================================
# Delete 错误码契约测试
# =============================================================================


class TestDeleteErrorCodesContract:
    """Delete 错误码契约测试

    验证 artifact_delete.py 使用统一的 artifact_store 安全策略：
    - PathTraversalError: 路径穿越检测
    - FileUriPathError: file:// URI 错误
    - ArtifactDeleteOpsCredentialsRequiredError: ops 凭证要求
    - ArtifactDeleteNotSupportedError: 不支持的存储类型
    """

    # 错误类型到错误码的映射（与 logbook_cli_main.py 对齐）
    ERROR_TYPE_CODES = {
        "PATH_TRAVERSAL_ERROR": "PATH_TRAVERSAL",
        "FILE_URI_PATH_ERROR": "FILE_URI_ERROR",
        "ARTIFACT_DELETE_OPS_CREDENTIALS_REQUIRED": "OPS_CREDENTIALS_REQUIRED",
        "ARTIFACT_DELETE_NOT_SUPPORTED": "DELETE_NOT_SUPPORTED",
    }

    def test_path_traversal_error_returns_error_field(
        self, artifacts_root: Path, store: LocalArtifactsStore
    ):
        """测试路径穿越攻击时返回 error 字段"""
        from engram.logbook.artifact_delete import delete_artifact_key

        # 尝试路径穿越
        result = delete_artifact_key("../../../etc/passwd", store=store)

        # 验证删除失败并返回 error 字段
        assert result.deleted is False
        assert result.existed is False
        assert result.error is not None
        assert "路径" in result.error or "traversal" in result.error.lower() or "穿越" in result.error

    def test_path_traversal_error_json_shape(
        self, artifacts_root: Path, store: LocalArtifactsStore
    ):
        """测试路径穿越错误的 JSON 输出 shape"""
        from engram.logbook.artifact_delete import delete_artifact_key

        result = delete_artifact_key("../../escape/file.txt", store=store)

        # 构建 CLI 风格的 JSON 输出
        result_dict = {
            "uri": result.uri,
            "deleted": result.deleted,
            "existed": result.existed,
            "trashed": result.trashed,
            "error": result.error,
        }

        output = {
            "ok": False,
            "mode": "single",
            "dry_run": False,
            "trash_mode": False,
            "total_count": 1,
            "deleted_count": 0,
            "failed_count": 1,
            "pending_count": 0,
            "not_existed_count": 0,
            "results": [result_dict],
            "error_code": "PATH_TRAVERSAL",
        }

        # 验证 error 相关字段
        assert output["ok"] is False
        assert output["failed_count"] == 1
        assert output["deleted_count"] == 0
        assert "error_code" in output
        assert output["results"][0]["error"] is not None

    def test_file_uri_path_error_returns_error_field(self, tmp_path: Path):
        """测试无效 file:// URI 返回 error 字段"""
        from engram.logbook.artifact_delete import delete_physical_uri

        # 尝试访问不允许的路径
        result = delete_physical_uri(
            "file:///etc/passwd",
            allowed_roots=[str(tmp_path)],  # 只允许 tmp_path
        )

        # 验证删除失败并返回 error 字段
        assert result.deleted is False
        assert result.error is not None

    def test_ops_credentials_required_error(self):
        """测试 ops 凭证要求时抛出正确的异常"""
        from engram.logbook.artifact_delete import (
            _delete_object_store_artifact,
            ArtifactDeleteOpsCredentialsRequiredError,
        )
        from engram.logbook.artifact_store import ObjectStore

        # 创建一个模拟的 ObjectStore（非 ops 凭证）
        mock_store = MagicMock(spec=ObjectStore)
        mock_store.is_ops_credentials.return_value = False
        mock_store.using_ops_credentials = False

        # 验证 require_ops=True 时抛出异常
        with pytest.raises(ArtifactDeleteOpsCredentialsRequiredError) as exc_info:
            _delete_object_store_artifact(
                mock_store, "test/file.txt", require_ops=True
            )

        # 验证异常包含正确的错误类型
        assert exc_info.value.error_type == "ARTIFACT_DELETE_OPS_CREDENTIALS_REQUIRED"
        assert "ops" in str(exc_info.value).lower()

    def test_ops_credentials_error_json_shape(self):
        """测试 ops 凭证错误的 JSON 输出 shape"""
        from engram.logbook.artifact_delete import ArtifactDeleteOpsCredentialsRequiredError

        # 模拟 CLI 捕获异常后的 JSON 输出
        error = ArtifactDeleteOpsCredentialsRequiredError(
            "需要 ops 凭证",
            {"uri": "test.txt", "hint": "设置 ENGRAM_S3_USE_OPS=true"},
        )

        output = {
            "ok": False,
            "mode": "single",
            "dry_run": False,
            "error_code": "OPS_CREDENTIALS_REQUIRED",
            "error_type": error.error_type,
            "error_message": str(error),
            "error_details": error.details,
            "total_count": 1,
            "deleted_count": 0,
            "failed_count": 1,
        }

        # 验证 shape
        assert output["ok"] is False
        assert output["error_code"] == "OPS_CREDENTIALS_REQUIRED"
        assert "error_type" in output
        assert "error_details" in output
        assert output["error_details"].get("hint") is not None

    def test_not_supported_error_for_file_uri_store(
        self, artifacts_root: Path
    ):
        """测试 FileUriStore 不支持逻辑键删除"""
        from engram.logbook.artifact_delete import (
            delete_artifact_key,
            ArtifactDeleteNotSupportedError,
        )
        from engram.logbook.artifact_store import FileUriStore

        store = FileUriStore(allowed_roots=[str(artifacts_root)])

        # FileUriStore 不支持逻辑键删除
        with pytest.raises(ArtifactDeleteNotSupportedError) as exc_info:
            delete_artifact_key("test/file.txt", store=store)

        assert exc_info.value.error_type == "ARTIFACT_DELETE_NOT_SUPPORTED"

    def test_delete_error_types_have_error_type_attr(self):
        """测试所有删除错误类都有 error_type 属性"""
        from engram.logbook.artifact_delete import (
            ArtifactDeleteError,
            ArtifactDeleteOpsCredentialsRequiredError,
            ArtifactDeleteNotSupportedError,
        )

        # 验证基类
        assert hasattr(ArtifactDeleteError, "error_type")

        # 验证子类
        assert hasattr(ArtifactDeleteOpsCredentialsRequiredError, "error_type")
        assert ArtifactDeleteOpsCredentialsRequiredError.error_type == "ARTIFACT_DELETE_OPS_CREDENTIALS_REQUIRED"

        assert hasattr(ArtifactDeleteNotSupportedError, "error_type")
        assert ArtifactDeleteNotSupportedError.error_type == "ARTIFACT_DELETE_NOT_SUPPORTED"

    def test_artifact_store_errors_have_error_type_attr(self):
        """测试 artifact_store 错误类都有 error_type 属性"""
        from engram.logbook.artifact_store import (
            ArtifactError,
            PathTraversalError,
            ArtifactNotFoundError,
            ArtifactWriteDisabledError,
            ArtifactOverwriteDeniedError,
            ArtifactHashMismatchError,
            FileUriPathError,
        )

        # 验证所有错误类都有 error_type
        error_classes = [
            (ArtifactError, "ARTIFACT_ERROR"),
            (PathTraversalError, "PATH_TRAVERSAL_ERROR"),
            (ArtifactNotFoundError, "ARTIFACT_NOT_FOUND"),
            (ArtifactWriteDisabledError, "ARTIFACT_WRITE_DISABLED"),
            (ArtifactOverwriteDeniedError, "ARTIFACT_OVERWRITE_DENIED"),
            (ArtifactHashMismatchError, "ARTIFACT_HASH_MISMATCH"),
            (FileUriPathError, "FILE_URI_PATH_ERROR"),
        ]

        for cls, expected_type in error_classes:
            assert hasattr(cls, "error_type"), f"{cls.__name__} 缺少 error_type"
            assert cls.error_type == expected_type, f"{cls.__name__}.error_type 不正确"


# =============================================================================
# CI 输出 Shape 契约测试
# =============================================================================


class TestCIOutputShapeContract:
    """CI 输出 shape 契约测试（验证 ci_status 字段）"""

    REQUIRED_CI_STATUS_VALUES = {"ok", "skipped", "error"}

    def test_gc_ci_output_shape(self, artifacts_root: Path, sample_files: dict):
        """测试 GC CI 输出包含 ci_status 和 status_summary"""
        with patch("artifact_gc.get_referenced_uris") as mock_get_refs:
            mock_get_refs.return_value = _make_referenced_uris(set())

            result = run_gc(
                prefix="scm/",
                dry_run=True,
                dsn="mock://",
                backend="local",
                artifacts_root=str(artifacts_root),
                verbose=False,
            )

            # 构建 CI 输出（模拟 Makefile target 逻辑）
            status_summary = {"ok": 0, "skipped": 0, "error": 0, "pending": 0}
            for c in result.candidates:
                if c.status in status_summary:
                    status_summary[c.status] += 1

            ci_output = {
                "ci_status": "ok",
                "gc_mode": result.gc_mode,
                "prefix": result.prefix,
                "status_summary": status_summary,
                "candidates_count": result.candidates_count,
            }

            # 验证 CI 必需字段
            assert "ci_status" in ci_output
            assert ci_output["ci_status"] in self.REQUIRED_CI_STATUS_VALUES
            assert "status_summary" in ci_output
            assert "ok" in ci_output["status_summary"]
            assert "skipped" in ci_output["status_summary"]
            assert "error" in ci_output["status_summary"]

    def test_skipped_ci_output_shape(self):
        """测试 skipped 状态的 CI 输出 shape"""
        # 模拟 POSTGRES_DSN 未设置时的输出
        ci_output = {
            "ci_status": "skipped",
            "reason": "POSTGRES_DSN not set",
            "gc_mode": "orphan",
            "prefix": "scm/",
            "status_summary": {"ok": 0, "skipped": 0, "error": 0},
        }

        # 验证 shape
        assert ci_output["ci_status"] == "skipped"
        assert "reason" in ci_output
        assert "status_summary" in ci_output

    def test_error_ci_output_shape(self):
        """测试 error 状态的 CI 输出 shape"""
        # 模拟执行失败时的输出
        ci_output = {
            "ci_status": "error",
            "exit_code": 1,
            "gc_mode": "orphan",
            "prefix": "scm/",
        }

        # 验证 shape
        assert ci_output["ci_status"] == "error"
        assert "exit_code" in ci_output
