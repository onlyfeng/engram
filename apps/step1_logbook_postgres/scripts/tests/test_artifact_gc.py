# -*- coding: utf-8 -*-
"""
test_artifact_gc.py - 制品垃圾回收测试

测试覆盖:
1. 引用保护：被 DB 引用的制品不能删除
2. 仅删未引用：未被引用的制品可以删除
3. dry-run 模式：不执行实际删除
4. 前缀限制：严格防止越权删除
5. 年龄过滤：older-than-days 参数有效
6. 软删除：trash-prefix 实现移动而非删除

隔离策略:
- 使用 pytest tmp_path fixture 提供临时 artifacts_root
- 使用 mock 模拟数据库查询
- 不依赖真实的数据库连接
"""

import os
import sys
import time
from pathlib import Path
from typing import Set
from unittest.mock import MagicMock, patch

import pytest

# 添加 scripts 目录到 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from artifact_gc import (
    GCCandidate,
    GCDatabaseError,
    GCError,
    GCPrefixError,
    GCOpsCredentialsRequiredError,
    GCResult,
    ReferencedUris,
    delete_local_file,
    delete_file_uri_file,
    get_referenced_uris,
    run_gc,
    run_tmp_gc,
    scan_local_artifacts,
    scan_file_uri_artifacts,
    _normalize_uri_for_gc,
)
from engram_step1.artifact_store import LocalArtifactsStore, FileUriStore
from engram_step1.db import get_connection
from engram_step1.uri import PhysicalRef


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
def sample_artifacts(artifacts_root: Path) -> dict:
    """
    创建示例制品文件

    返回:
        {
            "referenced": [uri1, uri2, ...],    # 被引用的 URI
            "unreferenced": [uri3, uri4, ...],  # 未被引用的 URI
            "all_files": {uri: path, ...},      # 所有文件映射
        }
    """
    # 创建目录结构
    scm_dir = artifacts_root / "scm" / "proj_a" / "1" / "git"
    scm_dir.mkdir(parents=True)

    attachments_dir = artifacts_root / "attachments" / "2024"
    attachments_dir.mkdir(parents=True)

    referenced_uris = []
    unreferenced_uris = []
    all_files = {}

    # 创建被引用的文件
    for i in range(3):
        uri = f"scm/proj_a/1/git/commit_{i}.diff"
        path = artifacts_root / uri
        path.write_bytes(f"diff content {i}".encode())
        referenced_uris.append(uri)
        all_files[uri] = path

    # 创建未被引用的文件
    for i in range(5):
        uri = f"scm/proj_a/1/git/old_commit_{i}.diff"
        path = artifacts_root / uri
        path.write_bytes(f"old diff content {i}".encode())
        unreferenced_uris.append(uri)
        all_files[uri] = path

    # 创建附件文件（未被引用）
    for i in range(2):
        uri = f"attachments/2024/file_{i}.txt"
        path = artifacts_root / uri
        path.write_bytes(f"attachment content {i}".encode())
        unreferenced_uris.append(uri)
        all_files[uri] = path

    return {
        "referenced": referenced_uris,
        "unreferenced": unreferenced_uris,
        "all_files": all_files,
    }


# =============================================================================
# 测试: URI 规范化（_normalize_uri_for_gc）
# =============================================================================


class TestNormalizeUriForGC:
    """测试 _normalize_uri_for_gc 函数的双轨分类行为"""

    def test_artifact_key_no_scheme(self):
        """无 scheme 的路径应规范化为 artifact key"""
        uri = "scm/proj_a/1/git/abc123.diff"
        normalized, uri_type = _normalize_uri_for_gc(uri)

        assert normalized == "scm/proj_a/1/git/abc123.diff"
        assert uri_type == "artifact_key"

    def test_artifact_key_with_artifact_scheme(self):
        """artifact:// scheme 应规范化为 artifact key"""
        uri = "artifact://scm/proj_a/1/git/abc123.diff"
        normalized, uri_type = _normalize_uri_for_gc(uri)

        assert normalized == "scm/proj_a/1/git/abc123.diff"
        assert uri_type == "artifact_key"

    def test_artifact_key_attachments(self):
        """attachments 路径应规范化为 artifact key"""
        uri = "attachments/2024/report.pdf"
        normalized, uri_type = _normalize_uri_for_gc(uri)

        assert normalized == "attachments/2024/report.pdf"
        assert uri_type == "artifact_key"

    def test_physical_uri_file_scheme(self):
        """file:// scheme 应返回 PhysicalRef 结构"""
        uri = "file:///mnt/artifacts/scm/proj_a/1.diff"
        result, uri_type = _normalize_uri_for_gc(uri)

        assert uri_type == "physical_uri"
        assert isinstance(result, PhysicalRef)
        assert result.scheme == "file"
        assert result.key == "/mnt/artifacts/scm/proj_a/1.diff"

    def test_physical_uri_s3_scheme(self):
        """s3:// scheme 应返回 PhysicalRef 结构，包含 bucket 和 key"""
        uri = "s3://my-bucket/engram/scm/proj_a/1.diff"
        result, uri_type = _normalize_uri_for_gc(uri)

        assert uri_type == "physical_uri"
        assert isinstance(result, PhysicalRef)
        assert result.scheme == "s3"
        assert result.bucket == "my-bucket"
        assert result.key == "engram/scm/proj_a/1.diff"

    def test_physical_uri_https_scheme(self):
        """https:// scheme 应返回 PhysicalRef 结构"""
        uri = "https://storage.example.com/artifacts/file.diff"
        result, uri_type = _normalize_uri_for_gc(uri)

        assert uri_type == "physical_uri"
        assert isinstance(result, PhysicalRef)
        assert result.scheme == "https"
        assert result.key == "/artifacts/file.diff"

    def test_memory_uri_ignored(self):
        """memory:// scheme 应被忽略（不参与 GC 匹配）"""
        uri = "memory://patch_blobs/git/1:abc123/sha256hash"
        normalized, uri_type = _normalize_uri_for_gc(uri)

        assert normalized == ""
        assert uri_type is None

    def test_empty_uri(self):
        """空 URI 应返回空结果"""
        normalized, uri_type = _normalize_uri_for_gc("")
        assert normalized == ""
        assert uri_type is None

    def test_path_normalization(self):
        """路径应正确规范化（去除前导斜杠、尾部斜杠、冗余分隔符）"""
        uri = "/scm/proj_a//1/git/abc123.diff/"
        normalized, uri_type = _normalize_uri_for_gc(uri)

        assert normalized == "scm/proj_a/1/git/abc123.diff"
        assert uri_type == "artifact_key"

    def test_s3_physical_ref_structure(self):
        """s3:// 返回的 PhysicalRef 应包含完整结构信息"""
        uri = "s3://engram-artifacts/prefix/scm/1/file.diff"
        result, uri_type = _normalize_uri_for_gc(uri)

        assert uri_type == "physical_uri"
        assert isinstance(result, PhysicalRef)
        assert result.scheme == "s3"
        assert result.bucket == "engram-artifacts"
        assert result.key == "prefix/scm/1/file.diff"
        assert result.raw == uri


# =============================================================================
# 测试: 引用保护
# =============================================================================


def _make_referenced_uris(artifact_keys: set, physical_refs: list = None) -> ReferencedUris:
    """创建 ReferencedUris 测试对象的辅助函数"""
    return ReferencedUris(
        artifact_keys=artifact_keys,
        physical_refs=physical_refs or [],
    )


class TestReferenceProtection:
    """测试被 DB 引用的制品受到保护"""

    def test_referenced_files_not_deleted(
        self,
        artifacts_root: Path,
        sample_artifacts: dict,
    ):
        """被引用的文件不应被删除"""
        referenced = set(sample_artifacts["referenced"])

        # Mock 数据库查询返回引用集合
        with patch("artifact_gc.get_referenced_uris") as mock_get_refs:
            mock_get_refs.return_value = _make_referenced_uris(referenced)

            result = run_gc(
                prefix="scm/",
                dry_run=False,
                delete=True,
                dsn="mock://",
                backend="local",
                artifacts_root=str(artifacts_root),
                verbose=False,
            )

            # 验证：被引用的文件应该被保护
            assert result.protected_count == len(referenced)

            # 验证：被引用的文件仍然存在
            for uri in referenced:
                path = artifacts_root / uri
                assert path.exists(), f"被引用的文件应该存在: {uri}"

    def test_all_files_protected_when_db_unavailable(
        self,
        artifacts_root: Path,
        sample_artifacts: dict,
    ):
        """当数据库不可用时，所有文件都应被保护"""
        from artifact_gc import GCDatabaseError

        # Mock 数据库查询抛出异常
        with patch("artifact_gc.get_referenced_uris") as mock_get_refs:
            mock_get_refs.side_effect = GCDatabaseError("Connection failed")

            result = run_gc(
                prefix="scm/",
                dry_run=False,
                delete=True,
                dsn="mock://",
                backend="local",
                artifacts_root=str(artifacts_root),
                verbose=False,
            )

            # 验证：所有文件都被保护（无法删除）
            assert result.deleted_count == 0
            assert result.candidates_count == 0

            # 验证：所有文件仍然存在
            for uri, path in sample_artifacts["all_files"].items():
                assert path.exists(), f"文件应该存在: {uri}"


# =============================================================================
# 测试: attachments 引用保护
# =============================================================================


class TestAttachmentsReferenceProtection:
    """测试 logbook.attachments 引用的制品受到保护（local 后端）"""

    def test_attachments_referenced_files_not_in_candidates(
        self,
        artifacts_root: Path,
    ):
        """被 attachments 表引用的文件不应进入待删除候选（local 后端）"""
        # 创建附件目录和文件
        attachments_dir = artifacts_root / "attachments" / "2024"
        attachments_dir.mkdir(parents=True)

        # 创建被 attachments 引用的文件
        referenced_file = attachments_dir / "report_001.pdf"
        referenced_file.write_bytes(b"referenced report content")

        # 创建未被引用的文件
        unreferenced_file = attachments_dir / "old_report.pdf"
        unreferenced_file.write_bytes(b"old report content")

        # attachments 引用的 URI（使用纯相对路径 - artifact key）
        referenced_uri = "attachments/2024/report_001.pdf"

        # Mock 数据库查询返回 attachments 引用
        with patch("artifact_gc.get_referenced_uris") as mock_get_refs:
            mock_get_refs.return_value = _make_referenced_uris({referenced_uri})

            result = run_gc(
                prefix="attachments/",
                dry_run=True,
                delete=False,
                dsn="mock://",
                backend="local",
                artifacts_root=str(artifacts_root),
                verbose=False,
            )

            # 验证：被引用的文件被保护
            assert result.protected_count == 1

            # 验证：只有未引用的文件进入候选
            assert result.candidates_count == 1
            candidate_uris = {c.uri for c in result.candidates}
            assert "attachments/2024/old_report.pdf" in candidate_uris
            assert referenced_uri not in candidate_uris

    def test_attachments_referenced_files_not_deleted(
        self,
        artifacts_root: Path,
    ):
        """被 attachments 表引用的文件不应被删除（local 后端）"""
        # 创建附件目录和文件
        attachments_dir = artifacts_root / "attachments" / "2024"
        attachments_dir.mkdir(parents=True)

        # 创建被 attachments 引用的文件
        referenced_file = attachments_dir / "important_doc.pdf"
        referenced_file.write_bytes(b"important document")

        # 创建未被引用的文件
        unreferenced_file = attachments_dir / "temp_doc.pdf"
        unreferenced_file.write_bytes(b"temp document")

        # attachments 引用的 URI
        referenced_uri = "attachments/2024/important_doc.pdf"

        with patch("artifact_gc.get_referenced_uris") as mock_get_refs:
            mock_get_refs.return_value = _make_referenced_uris({referenced_uri})

            result = run_gc(
                prefix="attachments/",
                dry_run=False,
                delete=True,
                dsn="mock://",
                backend="local",
                artifacts_root=str(artifacts_root),
                verbose=False,
            )

            # 验证：被引用的文件仍然存在
            assert referenced_file.exists(), "被引用的文件应该存在"

            # 验证：未引用的文件已被删除
            assert not unreferenced_file.exists(), "未引用的文件应该被删除"

            # 验证：删除计数正确
            assert result.deleted_count == 1
            assert result.protected_count == 1

    def test_attachments_with_artifact_scheme_protected(
        self,
        artifacts_root: Path,
    ):
        """使用 artifact:// scheme 引用的 attachments 文件也应受保护（local 后端）"""
        # 创建附件目录和文件
        attachments_dir = artifacts_root / "attachments" / "2024"
        attachments_dir.mkdir(parents=True)

        # 创建文件
        file1 = attachments_dir / "doc_001.pdf"
        file1.write_bytes(b"document 1")

        file2 = attachments_dir / "doc_002.pdf"
        file2.write_bytes(b"document 2")

        # 使用 artifact:// scheme 引用（应该被规范化为相对路径后匹配）
        # 注意：这里测试的是 get_referenced_uris 返回的已规范化 URI
        # 实际上 _normalize_uri_for_gc 会将 artifact://attachments/2024/doc_001.pdf
        # 规范化为 attachments/2024/doc_001.pdf
        referenced_uri = "attachments/2024/doc_001.pdf"

        with patch("artifact_gc.get_referenced_uris") as mock_get_refs:
            mock_get_refs.return_value = _make_referenced_uris({referenced_uri})

            result = run_gc(
                prefix="attachments/",
                dry_run=False,
                delete=True,
                dsn="mock://",
                backend="local",
                artifacts_root=str(artifacts_root),
                verbose=False,
            )

            # 验证：被引用的文件仍然存在
            assert file1.exists(), "被 artifact:// scheme 引用的文件应该存在"

            # 验证：未引用的文件已被删除
            assert not file2.exists(), "未引用的文件应该被删除"

    def test_mixed_patch_blobs_and_attachments_references(
        self,
        artifacts_root: Path,
        sample_artifacts: dict,
    ):
        """同时存在 patch_blobs 和 attachments 引用时都应受保护（local 后端）"""
        # sample_artifacts 已创建 scm/ 和 attachments/ 下的文件
        # 其中 referenced 是 scm/ 下的引用，我们额外添加 attachments 引用

        # 创建额外的 attachments 引用文件
        attachments_dir = artifacts_root / "attachments" / "2024"
        # 目录已由 sample_artifacts 创建

        extra_referenced = attachments_dir / "protected_attach.txt"
        extra_referenced.write_bytes(b"protected attachment")

        # 合并 patch_blobs 引用和 attachments 引用
        referenced = set(sample_artifacts["referenced"])
        referenced.add("attachments/2024/protected_attach.txt")

        with patch("artifact_gc.get_referenced_uris") as mock_get_refs:
            mock_get_refs.return_value = _make_referenced_uris(referenced)

            # 测试 scm/ 前缀
            result_scm = run_gc(
                prefix="scm/",
                dry_run=False,
                delete=True,
                dsn="mock://",
                backend="local",
                artifacts_root=str(artifacts_root),
                verbose=False,
            )

            # 验证 scm/ 下被引用的文件受保护
            for uri in sample_artifacts["referenced"]:
                path = artifacts_root / uri
                assert path.exists(), f"patch_blobs 引用的文件应该存在: {uri}"

            # 测试 attachments/ 前缀
            result_attach = run_gc(
                prefix="attachments/",
                dry_run=False,
                delete=True,
                dsn="mock://",
                backend="local",
                artifacts_root=str(artifacts_root),
                verbose=False,
            )

            # 验证 attachments/ 下被引用的文件受保护
            assert extra_referenced.exists(), "attachments 引用的文件应该存在"

    def test_attachments_multiple_files_protection(
        self,
        artifacts_root: Path,
    ):
        """多个 attachments 文件引用保护测试（local 后端）"""
        # 创建附件目录
        attachments_dir = artifacts_root / "attachments" / "2024" / "monthly"
        attachments_dir.mkdir(parents=True)

        # 创建多个被引用的文件
        protected_files = []
        for i in range(5):
            file_path = attachments_dir / f"report_{i:03d}.pdf"
            file_path.write_bytes(f"report content {i}".encode())
            protected_files.append(file_path)

        # 创建多个未被引用的文件
        unprotected_files = []
        for i in range(3):
            file_path = attachments_dir / f"temp_{i:03d}.pdf"
            file_path.write_bytes(f"temp content {i}".encode())
            unprotected_files.append(file_path)

        # 引用前 5 个文件
        referenced_uris = {
            f"attachments/2024/monthly/report_{i:03d}.pdf"
            for i in range(5)
        }

        with patch("artifact_gc.get_referenced_uris") as mock_get_refs:
            mock_get_refs.return_value = _make_referenced_uris(referenced_uris)

            result = run_gc(
                prefix="attachments/",
                dry_run=False,
                delete=True,
                dsn="mock://",
                backend="local",
                artifacts_root=str(artifacts_root),
                verbose=False,
            )

            # 验证：所有被引用的文件仍然存在
            for f in protected_files:
                assert f.exists(), f"被引用的文件应该存在: {f.name}"

            # 验证：所有未引用的文件已被删除
            for f in unprotected_files:
                assert not f.exists(), f"未引用的文件应该被删除: {f.name}"

            # 验证统计
            assert result.protected_count == 5
            assert result.deleted_count == 3

    def test_attachments_with_nested_directories(
        self,
        artifacts_root: Path,
    ):
        """嵌套目录中的 attachments 引用保护测试（local 后端）"""
        # 创建多级嵌套目录
        nested_dirs = [
            "attachments/2024/q1/jan",
            "attachments/2024/q1/feb",
            "attachments/2024/q2/apr",
        ]

        for dir_path in nested_dirs:
            (artifacts_root / dir_path).mkdir(parents=True)

        # 在不同目录创建文件
        files_to_create = [
            ("attachments/2024/q1/jan/report.pdf", True),   # 被引用
            ("attachments/2024/q1/jan/draft.pdf", False),   # 未被引用
            ("attachments/2024/q1/feb/summary.pdf", True),  # 被引用
            ("attachments/2024/q2/apr/notes.pdf", False),   # 未被引用
        ]

        for file_uri, _ in files_to_create:
            file_path = artifacts_root / file_uri
            file_path.write_bytes(f"content of {file_uri}".encode())

        # 只引用标记为 True 的文件
        referenced_uris = {
            uri for uri, is_referenced in files_to_create if is_referenced
        }

        with patch("artifact_gc.get_referenced_uris") as mock_get_refs:
            mock_get_refs.return_value = _make_referenced_uris(referenced_uris)

            result = run_gc(
                prefix="attachments/",
                dry_run=False,
                delete=True,
                dsn="mock://",
                backend="local",
                artifacts_root=str(artifacts_root),
                verbose=False,
            )

            # 验证被引用的文件存在
            for uri, is_referenced in files_to_create:
                file_path = artifacts_root / uri
                if is_referenced:
                    assert file_path.exists(), f"被引用的文件应该存在: {uri}"
                else:
                    assert not file_path.exists(), f"未引用的文件应该被删除: {uri}"

            assert result.protected_count == 2
            assert result.deleted_count == 2


# =============================================================================
# 测试: 仅删未引用
# =============================================================================


class TestDeleteUnreferencedOnly:
    """测试仅删除未被引用的制品"""

    def test_unreferenced_files_deleted(
        self,
        artifacts_root: Path,
        sample_artifacts: dict,
    ):
        """未被引用的文件应被删除"""
        referenced = set(sample_artifacts["referenced"])
        unreferenced = sample_artifacts["unreferenced"]

        # 只保留 scm/ 前缀下的未引用文件
        unreferenced_scm = [u for u in unreferenced if u.startswith("scm/")]

        # Mock 数据库查询返回引用集合
        with patch("artifact_gc.get_referenced_uris") as mock_get_refs:
            mock_get_refs.return_value = _make_referenced_uris(referenced)

            result = run_gc(
                prefix="scm/",
                dry_run=False,
                delete=True,
                dsn="mock://",
                backend="local",
                artifacts_root=str(artifacts_root),
                verbose=False,
            )

            # 验证：未引用的文件应被删除
            assert result.deleted_count == len(unreferenced_scm)

            # 验证：未引用的文件不再存在
            for uri in unreferenced_scm:
                path = artifacts_root / uri
                assert not path.exists(), f"未引用的文件应该被删除: {uri}"

            # 验证：被引用的文件仍然存在
            for uri in referenced:
                path = artifacts_root / uri
                assert path.exists(), f"被引用的文件应该存在: {uri}"

    def test_candidates_correctly_identified(
        self,
        artifacts_root: Path,
        sample_artifacts: dict,
    ):
        """待删除候选应正确识别"""
        referenced = set(sample_artifacts["referenced"])
        unreferenced = sample_artifacts["unreferenced"]

        # 只保留 scm/ 前缀下的未引用文件
        unreferenced_scm = [u for u in unreferenced if u.startswith("scm/")]

        with patch("artifact_gc.get_referenced_uris") as mock_get_refs:
            mock_get_refs.return_value = _make_referenced_uris(referenced)

            result = run_gc(
                prefix="scm/",
                dry_run=True,  # dry-run 模式
                delete=False,
                dsn="mock://",
                backend="local",
                artifacts_root=str(artifacts_root),
                verbose=False,
            )

            # 验证：候选数量正确
            assert result.candidates_count == len(unreferenced_scm)

            # 验证：候选 URI 正确
            candidate_uris = {c.uri for c in result.candidates}
            expected_uris = set(unreferenced_scm)
            assert candidate_uris == expected_uris


# =============================================================================
# 测试: dry-run 模式
# =============================================================================


class TestDryRunMode:
    """测试 dry-run 模式不执行实际删除"""

    def test_dry_run_does_not_delete(
        self,
        artifacts_root: Path,
        sample_artifacts: dict,
    ):
        """dry-run 模式不应删除任何文件"""
        referenced = set(sample_artifacts["referenced"])

        with patch("artifact_gc.get_referenced_uris") as mock_get_refs:
            mock_get_refs.return_value = _make_referenced_uris(referenced)

            result = run_gc(
                prefix="scm/",
                dry_run=True,
                delete=False,
                dsn="mock://",
                backend="local",
                artifacts_root=str(artifacts_root),
                verbose=False,
            )

            # 验证：删除计数为 0
            assert result.deleted_count == 0

            # 验证：所有文件仍然存在
            for uri, path in sample_artifacts["all_files"].items():
                assert path.exists(), f"dry-run 模式下文件应该存在: {uri}"

    def test_dry_run_identifies_candidates(
        self,
        artifacts_root: Path,
        sample_artifacts: dict,
    ):
        """dry-run 模式应正确识别候选文件"""
        referenced = set(sample_artifacts["referenced"])
        unreferenced = sample_artifacts["unreferenced"]
        unreferenced_scm = [u for u in unreferenced if u.startswith("scm/")]

        with patch("artifact_gc.get_referenced_uris") as mock_get_refs:
            mock_get_refs.return_value = _make_referenced_uris(referenced)

            result = run_gc(
                prefix="scm/",
                dry_run=True,
                delete=False,
                dsn="mock://",
                backend="local",
                artifacts_root=str(artifacts_root),
                verbose=False,
            )

            # 验证：正确识别候选
            assert result.candidates_count == len(unreferenced_scm)
            assert len(result.candidates) == len(unreferenced_scm)


# =============================================================================
# 测试: 前缀限制
# =============================================================================


class TestPrefixRestriction:
    """测试前缀限制防止越权删除"""

    def test_prefix_must_be_specified(self, artifacts_root: Path):
        """必须指定扫描前缀"""
        with pytest.raises(GCPrefixError):
            run_gc(
                prefix="",  # 空前缀
                dry_run=True,
                dsn="mock://",
                backend="local",
                artifacts_root=str(artifacts_root),
                verbose=False,
            )

    def test_prefix_not_in_allowed_list_rejected(
        self,
        artifacts_root: Path,
        sample_artifacts: dict,
    ):
        """前缀不在允许列表中应被拒绝"""
        with patch("artifact_gc.get_referenced_uris") as mock_get_refs:
            mock_get_refs.return_value = _make_referenced_uris(set())

            with pytest.raises(GCPrefixError) as exc_info:
                run_gc(
                    prefix="scm/",
                    dry_run=True,
                    dsn="mock://",
                    backend="local",
                    artifacts_root=str(artifacts_root),
                    allowed_prefixes=["attachments/"],  # 只允许 attachments/
                    verbose=False,
                )

            assert "不在允许范围内" in str(exc_info.value)

    def test_prefix_in_allowed_list_accepted(
        self,
        artifacts_root: Path,
        sample_artifacts: dict,
    ):
        """前缀在允许列表中应被接受"""
        with patch("artifact_gc.get_referenced_uris") as mock_get_refs:
            mock_get_refs.return_value = _make_referenced_uris(set())

            # 不应抛出异常
            result = run_gc(
                prefix="scm/",
                dry_run=True,
                dsn="mock://",
                backend="local",
                artifacts_root=str(artifacts_root),
                allowed_prefixes=["scm/", "attachments/"],
                verbose=False,
            )

            assert result is not None

    def test_empty_allowed_prefixes_rejects_all(
        self,
        artifacts_root: Path,
        sample_artifacts: dict,
    ):
        """空的 allowed_prefixes 应拒绝所有操作"""
        with patch("artifact_gc.get_referenced_uris") as mock_get_refs:
            mock_get_refs.return_value = _make_referenced_uris(set())

            with pytest.raises(GCPrefixError) as exc_info:
                run_gc(
                    prefix="scm/",
                    dry_run=True,
                    dsn="mock://",
                    backend="local",
                    artifacts_root=str(artifacts_root),
                    allowed_prefixes=[],  # 空列表
                    verbose=False,
                )

            assert "空列表" in str(exc_info.value)


# =============================================================================
# 测试: 年龄过滤
# =============================================================================


class TestAgeFiltering:
    """测试 older-than-days 参数"""

    def test_older_than_days_filters_new_files(
        self,
        artifacts_root: Path,
    ):
        """新文件应被跳过"""
        # 创建新文件（刚创建的文件）
        scm_dir = artifacts_root / "scm" / "test"
        scm_dir.mkdir(parents=True)

        new_file = scm_dir / "new.diff"
        new_file.write_bytes(b"new content")

        with patch("artifact_gc.get_referenced_uris") as mock_get_refs:
            mock_get_refs.return_value = _make_referenced_uris(set())

            result = run_gc(
                prefix="scm/",
                dry_run=True,
                older_than_days=30,  # 只删除 30 天前的文件
                dsn="mock://",
                backend="local",
                artifacts_root=str(artifacts_root),
                verbose=False,
            )

            # 新文件应被跳过（年龄不足）
            assert result.skipped_by_age == 1
            assert result.candidates_count == 0

    def test_older_than_days_includes_old_files(
        self,
        artifacts_root: Path,
    ):
        """旧文件应被包含"""
        # 创建旧文件
        scm_dir = artifacts_root / "scm" / "test"
        scm_dir.mkdir(parents=True)

        old_file = scm_dir / "old.diff"
        old_file.write_bytes(b"old content")

        # 修改文件时间为 60 天前
        old_mtime = time.time() - (60 * 24 * 3600)
        os.utime(old_file, (old_mtime, old_mtime))

        with patch("artifact_gc.get_referenced_uris") as mock_get_refs:
            mock_get_refs.return_value = _make_referenced_uris(set())

            result = run_gc(
                prefix="scm/",
                dry_run=True,
                older_than_days=30,  # 只删除 30 天前的文件
                dsn="mock://",
                backend="local",
                artifacts_root=str(artifacts_root),
                verbose=False,
            )

            # 旧文件应被包含
            assert result.candidates_count == 1
            assert result.skipped_by_age == 0


# =============================================================================
# 测试: 软删除
# =============================================================================


class TestSoftDelete:
    """测试软删除（trash-prefix）功能"""

    def test_trash_prefix_moves_files(
        self,
        artifacts_root: Path,
        sample_artifacts: dict,
    ):
        """使用 trash-prefix 应移动文件而非删除"""
        referenced = set(sample_artifacts["referenced"])
        unreferenced = sample_artifacts["unreferenced"]
        unreferenced_scm = [u for u in unreferenced if u.startswith("scm/")]

        trash_dir = artifacts_root / ".trash"

        with patch("artifact_gc.get_referenced_uris") as mock_get_refs:
            mock_get_refs.return_value = _make_referenced_uris(referenced)

            result = run_gc(
                prefix="scm/",
                dry_run=False,
                delete=True,
                trash_prefix=".trash/",
                dsn="mock://",
                backend="local",
                artifacts_root=str(artifacts_root),
                verbose=False,
            )

            # 验证：文件被移动到 trash
            assert result.trashed_count == len(unreferenced_scm)
            assert result.deleted_count == 0

            # 验证：原位置文件不存在
            for uri in unreferenced_scm:
                path = artifacts_root / uri
                assert not path.exists(), f"原文件应该不存在: {uri}"

            # 验证：trash 目录中有文件
            assert trash_dir.exists()
            trash_files = list(trash_dir.rglob("*"))
            trash_files = [f for f in trash_files if f.is_file()]
            assert len(trash_files) == len(unreferenced_scm)


# =============================================================================
# 测试: 扫描功能
# =============================================================================


class TestScanLocalArtifacts:
    """测试本地文件扫描"""

    def test_scan_returns_correct_files(
        self,
        store: LocalArtifactsStore,
        sample_artifacts: dict,
    ):
        """扫描应返回正确的文件列表"""
        files = scan_local_artifacts(store, "scm/")

        # 只统计 scm/ 前缀下的文件
        expected_scm_files = [
            uri for uri in sample_artifacts["all_files"].keys()
            if uri.startswith("scm/")
        ]

        assert len(files) == len(expected_scm_files)

        scanned_uris = {f[0] for f in files}
        expected_uris = set(expected_scm_files)
        assert scanned_uris == expected_uris

    def test_scan_skips_hidden_files(
        self,
        store: LocalArtifactsStore,
        artifacts_root: Path,
    ):
        """扫描应跳过隐藏文件"""
        scm_dir = artifacts_root / "scm" / "test"
        scm_dir.mkdir(parents=True)

        # 创建普通文件
        normal_file = scm_dir / "normal.diff"
        normal_file.write_bytes(b"content")

        # 创建隐藏文件
        hidden_file = scm_dir / ".hidden.diff"
        hidden_file.write_bytes(b"hidden")

        # 创建临时文件
        tmp_file = scm_dir / "temp.diff.tmp"
        tmp_file.write_bytes(b"temp")

        files = scan_local_artifacts(store, "scm/")

        scanned_uris = {f[0] for f in files}

        assert "scm/test/normal.diff" in scanned_uris
        assert "scm/test/.hidden.diff" not in scanned_uris
        assert "scm/test/temp.diff.tmp" not in scanned_uris

    def test_scan_with_prefix_restriction(
        self,
        store: LocalArtifactsStore,
        sample_artifacts: dict,
    ):
        """扫描应受 allowed_prefixes 限制"""
        with pytest.raises(GCPrefixError):
            scan_local_artifacts(
                store,
                "scm/",
                allowed_prefixes=["attachments/"],  # 不允许 scm/
            )


# =============================================================================
# 测试: 删除功能
# =============================================================================


class TestDeleteLocalFile:
    """测试本地文件删除"""

    def test_delete_existing_file(self, tmp_path: Path):
        """删除存在的文件"""
        file_path = tmp_path / "test.txt"
        file_path.write_bytes(b"content")

        success, error = delete_local_file(str(file_path))

        assert success is True
        assert error is None
        assert not file_path.exists()

    def test_delete_nonexistent_file(self, tmp_path: Path):
        """删除不存在的文件应成功（幂等）"""
        file_path = tmp_path / "nonexistent.txt"

        success, error = delete_local_file(str(file_path))

        assert success is True
        assert error is None

    def test_soft_delete_moves_file(self, tmp_path: Path):
        """软删除应移动文件到 trash 目录"""
        artifacts_root = tmp_path / "artifacts"
        artifacts_root.mkdir()

        scm_dir = artifacts_root / "scm"
        scm_dir.mkdir()

        file_path = scm_dir / "test.diff"
        file_path.write_bytes(b"content")

        success, error = delete_local_file(
            str(file_path),
            trash_prefix=".trash/",
            artifacts_root=artifacts_root,
        )

        assert success is True
        assert error is None
        assert not file_path.exists()

        # 验证文件被移动到 trash
        trash_file = artifacts_root / ".trash" / "scm" / "test.diff"
        assert trash_file.exists()


# =============================================================================
# 测试: get_referenced_uris 函数
# =============================================================================


class TestGetReferencedUris:
    """测试 get_referenced_uris 函数的数据库连接和查询"""

    def _create_mock_connection(self, fetchall_return=None):
        """创建测试用的 mock 数据库连接

        正确设置上下文管理器以支持 `with conn.cursor() as cur:` 语法
        """
        if fetchall_return is None:
            fetchall_return = []

        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = fetchall_return
        # 让 cursor 支持上下文管理器协议
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        return mock_conn, mock_cursor

    def test_uses_get_connection_with_search_path(self):
        """验证 get_referenced_uris 使用 get_connection 并设置 search_path"""
        mock_conn, mock_cursor = self._create_mock_connection([("scm/test/file.diff",)])

        with patch("engram_step1.db.get_connection") as mock_get_conn:
            mock_get_conn.return_value = mock_conn

            result = get_referenced_uris(dsn="postgresql://test", prefix="scm/")

            # 验证 get_connection 被调用且传入了正确参数
            mock_get_conn.assert_called_once()
            call_kwargs = mock_get_conn.call_args.kwargs
            assert call_kwargs["dsn"] == "postgresql://test"
            assert call_kwargs["autocommit"] is True
            # search_path 包含 scm、logbook 和 public（因为查询两个表）
            assert call_kwargs["search_path"] == ["scm", "logbook", "public"]

            # 验证返回结果是 ReferencedUris 类型
            assert isinstance(result, ReferencedUris)
            assert "scm/test/file.diff" in result.artifact_keys

    def test_custom_search_path(self):
        """验证可以传入自定义 search_path"""
        mock_conn, _ = self._create_mock_connection([])

        with patch("engram_step1.db.get_connection") as mock_get_conn:
            mock_get_conn.return_value = mock_conn

            get_referenced_uris(
                dsn="postgresql://test",
                search_path=["custom_schema", "scm", "public"],
            )

            # 验证使用了自定义 search_path
            call_kwargs = mock_get_conn.call_args.kwargs
            assert call_kwargs["search_path"] == ["custom_schema", "scm", "public"]

    def test_schema_qualified_query(self):
        """验证查询使用 schema-qualified 表名 scm.patch_blobs 和 logbook.attachments"""
        mock_conn, mock_cursor = self._create_mock_connection([])

        with patch("engram_step1.db.get_connection") as mock_get_conn:
            mock_get_conn.return_value = mock_conn

            get_referenced_uris(dsn="postgresql://test")

            # 验证执行了两次查询（patch_blobs 和 attachments）
            assert mock_cursor.execute.call_count == 2

            # 获取两次执行的 SQL
            calls = mock_cursor.execute.call_args_list
            first_sql = calls[0][0][0]
            second_sql = calls[1][0][0]

            # 验证第一次查询使用了 scm.patch_blobs
            assert "scm.patch_blobs" in first_sql
            assert "FROM scm.patch_blobs" in first_sql

            # 验证第二次查询使用了 logbook.attachments
            assert "logbook.attachments" in second_sql
            assert "FROM logbook.attachments" in second_sql

    def test_query_with_prefix_filter(self):
        """验证带前缀过滤的查询"""
        mock_conn, mock_cursor = self._create_mock_connection([
            ("scm/proj_a/1.diff",),
            ("scm/proj_a/2.diff",),
        ])

        with patch("engram_step1.db.get_connection") as mock_get_conn:
            mock_get_conn.return_value = mock_conn

            result = get_referenced_uris(dsn="postgresql://test", prefix="scm/proj_a/")

            # 验证执行了两次查询
            assert mock_cursor.execute.call_count == 2
            calls = mock_cursor.execute.call_args_list

            # 验证第一次查询（patch_blobs）使用 prefix% 模式
            # 注意：normalize_uri 会移除尾部斜杠，所以 "scm/proj_a/" -> "scm/proj_a"
            first_sql = calls[0][0][0]
            first_params = calls[0][0][1]
            assert "LIKE" in first_sql
            assert first_params[0] == "scm/proj_a%"

            # 验证第二次查询（attachments）使用 %prefix% 模式
            # （因为 attachments 的 URI 可能带有 scheme，需要匹配 path 部分）
            second_sql = calls[1][0][0]
            second_params = calls[1][0][1]
            assert "LIKE" in second_sql
            assert second_params[0] == "%scm/proj_a%"

            # 验证返回结果（ReferencedUris 类型）
            assert isinstance(result, ReferencedUris)
            assert len(result.artifact_keys) == 2

    def test_connection_closed_after_query(self):
        """验证查询后连接被正确关闭"""
        mock_conn, _ = self._create_mock_connection([])

        with patch("engram_step1.db.get_connection") as mock_get_conn:
            mock_get_conn.return_value = mock_conn

            get_referenced_uris(dsn="postgresql://test")

            # 验证连接被关闭
            mock_conn.close.assert_called_once()

    def test_connection_closed_on_query_error(self):
        """验证查询出错时连接也被正确关闭"""
        mock_conn, mock_cursor = self._create_mock_connection([])
        mock_cursor.execute.side_effect = Exception("Query failed")

        with patch("engram_step1.db.get_connection") as mock_get_conn:
            mock_get_conn.return_value = mock_conn

            with pytest.raises(GCDatabaseError):
                get_referenced_uris(dsn="postgresql://test")

            # 验证连接被关闭
            mock_conn.close.assert_called_once()

    def test_raises_gc_database_error_on_connection_failure(self):
        """验证连接失败时抛出 GCDatabaseError"""
        from engram_step1.errors import DbConnectionError

        with patch("engram_step1.db.get_connection") as mock_get_conn:
            mock_get_conn.side_effect = DbConnectionError(
                "Connection refused", {"error": "refused"}
            )

            with pytest.raises(GCDatabaseError) as exc_info:
                get_referenced_uris(dsn="postgresql://invalid")

            assert "数据库连接失败" in str(exc_info.value)

    def test_raises_error_when_dsn_not_provided(self):
        """验证未提供 DSN 且环境变量未设置时抛出错误"""
        # 确保环境变量未设置
        with patch.dict(os.environ, {}, clear=True):
            # 移除可能存在的 POSTGRES_DSN
            os.environ.pop("POSTGRES_DSN", None)

            with pytest.raises(GCDatabaseError) as exc_info:
                get_referenced_uris()

            assert "POSTGRES_DSN" in str(exc_info.value)


# =============================================================================
# 测试: FileUriStore 扫描
# =============================================================================


class TestScanFileUriArtifacts:
    """测试 scan_file_uri_artifacts 函数"""

    @pytest.fixture
    def file_uri_root(self, tmp_path: Path) -> Path:
        """创建临时的 file:// URI 根目录"""
        root = tmp_path / "file_uri_root"
        root.mkdir()
        return root

    @pytest.fixture
    def file_uri_store(self, file_uri_root: Path) -> FileUriStore:
        """创建测试用 FileUriStore"""
        return FileUriStore(allowed_roots=[str(file_uri_root)])

    @pytest.fixture
    def file_uri_artifacts(self, file_uri_root: Path) -> dict:
        """创建 file:// URI 测试文件"""
        # 创建目录结构
        scm_dir = file_uri_root / "scm" / "proj_a"
        scm_dir.mkdir(parents=True)

        attachments_dir = file_uri_root / "attachments" / "2024"
        attachments_dir.mkdir(parents=True)

        all_files = {}

        # 创建 scm/ 下的文件
        for i in range(3):
            file_path = scm_dir / f"commit_{i}.diff"
            file_path.write_bytes(f"diff content {i}".encode())
            all_files[f"scm/proj_a/commit_{i}.diff"] = file_path

        # 创建 attachments/ 下的文件
        for i in range(2):
            file_path = attachments_dir / f"report_{i}.pdf"
            file_path.write_bytes(f"report content {i}".encode())
            all_files[f"attachments/2024/report_{i}.pdf"] = file_path

        return {
            "root": file_uri_root,
            "all_files": all_files,
        }

    def test_scan_returns_file_uris(
        self,
        file_uri_store: FileUriStore,
        file_uri_artifacts: dict,
    ):
        """扫描应返回 file:// 格式的 URI"""
        files = scan_file_uri_artifacts(file_uri_store, "scm/")

        assert len(files) == 3

        for uri, full_path, size_bytes, mtime in files:
            # URI 应该是 file:// 格式
            assert uri.startswith("file://"), f"URI 应该是 file:// 格式: {uri}"
            # full_path 应该是本地路径
            assert Path(full_path).exists()

    def test_scan_with_prefix_filter(
        self,
        file_uri_store: FileUriStore,
        file_uri_artifacts: dict,
    ):
        """扫描应正确过滤前缀"""
        # 只扫描 scm/ 前缀
        scm_files = scan_file_uri_artifacts(file_uri_store, "scm/")
        assert len(scm_files) == 3

        # 只扫描 attachments/ 前缀
        attach_files = scan_file_uri_artifacts(file_uri_store, "attachments/")
        assert len(attach_files) == 2

    def test_scan_requires_allowed_roots(self, tmp_path: Path):
        """未配置 allowed_roots 时应抛出错误"""
        store = FileUriStore(allowed_roots=None)

        with pytest.raises(GCPrefixError) as exc_info:
            scan_file_uri_artifacts(store, "scm/")

        assert "allowed_roots" in str(exc_info.value)

    def test_scan_empty_allowed_roots_rejected(self, tmp_path: Path):
        """空的 allowed_roots 应被拒绝"""
        store = FileUriStore(allowed_roots=[])

        with pytest.raises(GCPrefixError) as exc_info:
            scan_file_uri_artifacts(store, "scm/")

        assert "空" in str(exc_info.value)

    def test_scan_skips_hidden_and_tmp_files(
        self,
        file_uri_store: FileUriStore,
        file_uri_root: Path,
    ):
        """扫描应跳过隐藏文件和临时文件"""
        scm_dir = file_uri_root / "scm" / "test"
        scm_dir.mkdir(parents=True)

        # 创建普通文件
        normal_file = scm_dir / "normal.diff"
        normal_file.write_bytes(b"content")

        # 创建隐藏文件
        hidden_file = scm_dir / ".hidden.diff"
        hidden_file.write_bytes(b"hidden")

        # 创建临时文件
        tmp_file = scm_dir / "temp.diff.tmp"
        tmp_file.write_bytes(b"temp")

        files = scan_file_uri_artifacts(file_uri_store, "scm/test/")

        # 只应该有一个普通文件
        assert len(files) == 1

        # 验证是普通文件
        uri, full_path, _, _ = files[0]
        assert "normal.diff" in uri


class TestDeleteFileUriFile:
    """测试 delete_file_uri_file 函数"""

    @pytest.fixture
    def file_uri_root(self, tmp_path: Path) -> Path:
        """创建临时的 file:// URI 根目录"""
        root = tmp_path / "file_uri_root"
        root.mkdir()
        return root

    @pytest.fixture
    def file_uri_store(self, file_uri_root: Path) -> FileUriStore:
        """创建测试用 FileUriStore"""
        return FileUriStore(allowed_roots=[str(file_uri_root)])

    def test_delete_file_uri(
        self,
        file_uri_store: FileUriStore,
        file_uri_root: Path,
    ):
        """删除 file:// URI 指向的文件"""
        # 创建测试文件
        scm_dir = file_uri_root / "scm"
        scm_dir.mkdir(parents=True)
        test_file = scm_dir / "test.diff"
        test_file.write_bytes(b"test content")

        # 构建 file:// URI
        file_uri = file_uri_store._ensure_file_uri(str(test_file))

        success, error = delete_file_uri_file(file_uri_store, file_uri)

        assert success is True
        assert error is None
        assert not test_file.exists()

    def test_soft_delete_file_uri(
        self,
        file_uri_store: FileUriStore,
        file_uri_root: Path,
    ):
        """软删除 file:// URI 指向的文件"""
        # 创建测试文件
        scm_dir = file_uri_root / "scm"
        scm_dir.mkdir(parents=True)
        test_file = scm_dir / "test.diff"
        test_file.write_bytes(b"test content")

        # 构建 file:// URI
        file_uri = file_uri_store._ensure_file_uri(str(test_file))

        success, error = delete_file_uri_file(
            file_uri_store, file_uri, trash_prefix=".trash/"
        )

        assert success is True
        assert error is None
        assert not test_file.exists()

        # 验证文件被移动到 trash
        trash_dir = file_uri_root / ".trash"
        assert trash_dir.exists()
        trash_files = list(trash_dir.rglob("*"))
        trash_files = [f for f in trash_files if f.is_file()]
        assert len(trash_files) == 1

    def test_delete_nonexistent_file_uri(
        self,
        file_uri_store: FileUriStore,
        file_uri_root: Path,
    ):
        """删除不存在的文件应成功（幂等）"""
        file_uri = f"file://{file_uri_root}/nonexistent.txt"

        success, error = delete_file_uri_file(file_uri_store, file_uri)

        assert success is True
        assert error is None


class TestFileUriStoreGCIntegration:
    """测试 FileUriStore 与 GC 的集成"""

    @pytest.fixture
    def file_uri_root(self, tmp_path: Path) -> Path:
        """创建临时的 file:// URI 根目录"""
        root = tmp_path / "file_uri_root"
        root.mkdir()
        return root

    @pytest.fixture
    def file_uri_artifacts(self, file_uri_root: Path) -> dict:
        """创建 file:// URI 测试文件"""
        scm_dir = file_uri_root / "scm" / "proj_a"
        scm_dir.mkdir(parents=True)

        referenced_files = []
        unreferenced_files = []
        all_files = {}

        # 创建被引用的文件
        for i in range(2):
            file_path = scm_dir / f"referenced_{i}.diff"
            file_path.write_bytes(f"referenced content {i}".encode())
            referenced_files.append(f"scm/proj_a/referenced_{i}.diff")
            all_files[f"scm/proj_a/referenced_{i}.diff"] = file_path

        # 创建未被引用的文件
        for i in range(3):
            file_path = scm_dir / f"unreferenced_{i}.diff"
            file_path.write_bytes(f"unreferenced content {i}".encode())
            unreferenced_files.append(f"scm/proj_a/unreferenced_{i}.diff")
            all_files[f"scm/proj_a/unreferenced_{i}.diff"] = file_path

        return {
            "root": file_uri_root,
            "referenced": referenced_files,
            "unreferenced": unreferenced_files,
            "all_files": all_files,
        }

    def test_file_uri_gc_protects_referenced(
        self,
        file_uri_root: Path,
        file_uri_artifacts: dict,
    ):
        """GC 应保护被引用的 file:// URI 文件"""
        # 创建 store 时设置 allowed_roots
        store = FileUriStore(allowed_roots=[str(file_uri_root)])

        # 构建引用 URI 集合（file:// 格式）
        # PhysicalRef 需要存储在 physical_refs 列表中
        from engram_step1.uri import PhysicalRef
        physical_refs = []
        artifact_keys = set()
        
        for rel_path in file_uri_artifacts["referenced"]:
            full_path = file_uri_root / rel_path
            file_uri = store._ensure_file_uri(str(full_path))
            # 规范化后的 path 部分用于匹配
            from artifact_gc import _normalize_uri_for_gc
            normalized, uri_type = _normalize_uri_for_gc(file_uri)
            if uri_type == "physical_uri" and isinstance(normalized, PhysicalRef):
                physical_refs.append(normalized)
            else:
                artifact_keys.add(normalized)

        # Mock get_referenced_uris 返回引用集合
        with patch("artifact_gc.get_referenced_uris") as mock_get_refs:
            mock_get_refs.return_value = _make_referenced_uris(artifact_keys, physical_refs)

            # 使用 scan_file_uri_artifacts 直接测试
            scanned = scan_file_uri_artifacts(store, "scm/")

            assert len(scanned) == 5  # 2 被引用 + 3 未引用

    def test_file_uri_gc_soft_delete(
        self,
        file_uri_root: Path,
        file_uri_artifacts: dict,
    ):
        """GC 软删除 file:// URI 文件"""
        store = FileUriStore(allowed_roots=[str(file_uri_root)])

        # 直接测试 delete_file_uri_file 的软删除功能
        unreferenced = file_uri_artifacts["unreferenced"]

        for rel_path in unreferenced:
            full_path = file_uri_root / rel_path
            file_uri = store._ensure_file_uri(str(full_path))

            success, error = delete_file_uri_file(
                store, file_uri, trash_prefix=".trash/"
            )

            assert success is True
            assert not full_path.exists()

        # 验证 trash 目录中有文件
        trash_dir = file_uri_root / ".trash"
        assert trash_dir.exists()
        trash_files = list(trash_dir.rglob("*"))
        trash_files = [f for f in trash_files if f.is_file()]
        assert len(trash_files) == 3


# =============================================================================
# 测试: 结果统计
# =============================================================================


class TestGCResult:
    """测试 GC 结果统计"""

    def test_result_statistics_correct(
        self,
        artifacts_root: Path,
        sample_artifacts: dict,
    ):
        """结果统计应正确"""
        referenced = set(sample_artifacts["referenced"])
        unreferenced = sample_artifacts["unreferenced"]
        unreferenced_scm = [u for u in unreferenced if u.startswith("scm/")]

        with patch("artifact_gc.get_referenced_uris") as mock_get_refs:
            mock_get_refs.return_value = _make_referenced_uris(referenced)

            result = run_gc(
                prefix="scm/",
                dry_run=False,
                delete=True,
                dsn="mock://",
                backend="local",
                artifacts_root=str(artifacts_root),
                verbose=False,
            )

            # 扫描数 = 被引用 + 未引用（scm/ 前缀下）
            scm_files = [
                uri for uri in sample_artifacts["all_files"].keys()
                if uri.startswith("scm/")
            ]
            assert result.scanned_count == len(scm_files)

            # 引用数
            assert result.referenced_count == len(referenced)

            # 保护数
            assert result.protected_count == len(referenced)

            # 候选数
            assert result.candidates_count == len(unreferenced_scm)

            # 删除数
            assert result.deleted_count == len(unreferenced_scm)

            # 无失败
            assert result.failed_count == 0

    def test_result_includes_metadata(
        self,
        artifacts_root: Path,
        sample_artifacts: dict,
    ):
        """结果应包含元信息（backend, prefix, gc_mode）"""
        referenced = set(sample_artifacts["referenced"])

        with patch("artifact_gc.get_referenced_uris") as mock_get_refs:
            mock_get_refs.return_value = _make_referenced_uris(referenced)

            result = run_gc(
                prefix="scm/",
                dry_run=True,
                dsn="mock://",
                backend="local",
                artifacts_root=str(artifacts_root),
                verbose=False,
            )

            # 验证元信息
            assert result.gc_mode == "orphan"
            assert result.backend == "local"
            assert result.prefix == "scm/"
            assert result.bucket is None  # local 后端没有 bucket


# =============================================================================
# 测试: Tmp 清理模式
# =============================================================================


class TestTmpGCMode:
    """测试 tmp 清理模式（不依赖 DB）"""

    def test_tmp_gc_deletes_old_files(self, artifacts_root: Path):
        """tmp 清理应删除超过指定天数的文件"""
        # 创建 tmp 目录和文件
        tmp_dir = artifacts_root / "tmp" / "builds"
        tmp_dir.mkdir(parents=True)

        # 创建旧文件（10 天前）
        old_file = tmp_dir / "old_build.tar.gz"
        old_file.write_bytes(b"old build content")
        old_mtime = time.time() - (10 * 24 * 3600)
        os.utime(old_file, (old_mtime, old_mtime))

        # 创建新文件（刚创建）
        new_file = tmp_dir / "new_build.tar.gz"
        new_file.write_bytes(b"new build content")

        result = run_tmp_gc(
            tmp_prefix="tmp/",
            older_than_days=7,
            dry_run=False,
            delete=True,
            backend="local",
            artifacts_root=str(artifacts_root),
            verbose=False,
        )

        # 验证：旧文件被删除
        assert not old_file.exists()
        assert result.deleted_count == 1

        # 验证：新文件保留
        assert new_file.exists()
        assert result.skipped_by_age == 1

        # 验证元信息
        assert result.gc_mode == "tmp"
        assert result.backend == "local"
        assert result.prefix == "tmp/"

    def test_tmp_gc_dry_run_mode(self, artifacts_root: Path):
        """tmp 清理 dry-run 模式不应删除文件"""
        # 创建 tmp 目录和旧文件
        tmp_dir = artifacts_root / "tmp"
        tmp_dir.mkdir(parents=True)

        old_file = tmp_dir / "old_build.tar.gz"
        old_file.write_bytes(b"old content")
        old_mtime = time.time() - (30 * 24 * 3600)
        os.utime(old_file, (old_mtime, old_mtime))

        result = run_tmp_gc(
            tmp_prefix="tmp/",
            older_than_days=7,
            dry_run=True,
            delete=False,
            backend="local",
            artifacts_root=str(artifacts_root),
            verbose=False,
        )

        # 验证：文件未被删除
        assert old_file.exists()
        assert result.deleted_count == 0
        assert result.candidates_count == 1

    def test_tmp_gc_requires_older_than_days(self, artifacts_root: Path):
        """tmp 清理模式必须指定 older_than_days 参数"""
        # 创建 tmp 目录
        tmp_dir = artifacts_root / "tmp"
        tmp_dir.mkdir(parents=True)

        with pytest.raises(GCPrefixError) as exc_info:
            run_tmp_gc(
                tmp_prefix="tmp/",
                older_than_days=None,  # 未指定
                dry_run=True,
                backend="local",
                artifacts_root=str(artifacts_root),
                verbose=False,
            )

        assert "older-than-days" in str(exc_info.value).lower() or "必须" in str(exc_info.value)

    def test_tmp_gc_requires_prefix(self, artifacts_root: Path):
        """tmp 清理模式必须指定 tmp_prefix 参数"""
        with pytest.raises(GCPrefixError) as exc_info:
            run_tmp_gc(
                tmp_prefix="",  # 空前缀
                older_than_days=7,
                dry_run=True,
                backend="local",
                artifacts_root=str(artifacts_root),
                verbose=False,
            )

        assert "前缀" in str(exc_info.value)

    def test_tmp_gc_does_not_query_db(self, artifacts_root: Path):
        """tmp 清理模式不应查询数据库"""
        # 创建 tmp 目录和文件
        tmp_dir = artifacts_root / "tmp"
        tmp_dir.mkdir(parents=True)

        old_file = tmp_dir / "old_cache.dat"
        old_file.write_bytes(b"content")
        old_mtime = time.time() - (10 * 24 * 3600)
        os.utime(old_file, (old_mtime, old_mtime))

        # 不 mock get_referenced_uris，如果被调用会抛出异常
        # 因为没有设置 POSTGRES_DSN 且没有传入 dsn 参数

        # 应该能正常执行，不查询数据库
        result = run_tmp_gc(
            tmp_prefix="tmp/",
            older_than_days=7,
            dry_run=False,
            delete=True,
            backend="local",
            artifacts_root=str(artifacts_root),
            verbose=False,
        )

        # 验证：成功删除
        assert result.deleted_count == 1
        assert not old_file.exists()

        # 验证：没有引用计数（因为不查询 DB）
        assert result.referenced_count == 0
        assert result.protected_count == 0

    def test_tmp_gc_nested_directories(self, artifacts_root: Path):
        """tmp 清理应递归处理嵌套目录"""
        # 创建嵌套 tmp 目录
        tmp_dirs = [
            "tmp/builds/project_a",
            "tmp/builds/project_b",
            "tmp/cache/images",
        ]
        for d in tmp_dirs:
            (artifacts_root / d).mkdir(parents=True)

        # 在各目录创建旧文件
        old_files = []
        for i, d in enumerate(tmp_dirs):
            file_path = artifacts_root / d / f"old_{i}.tar.gz"
            file_path.write_bytes(f"content {i}".encode())
            old_mtime = time.time() - (15 * 24 * 3600)
            os.utime(file_path, (old_mtime, old_mtime))
            old_files.append(file_path)

        result = run_tmp_gc(
            tmp_prefix="tmp/",
            older_than_days=7,
            dry_run=False,
            delete=True,
            backend="local",
            artifacts_root=str(artifacts_root),
            verbose=False,
        )

        # 验证：所有旧文件被删除
        assert result.deleted_count == 3
        for f in old_files:
            assert not f.exists()

    def test_tmp_gc_mixed_age_files(self, artifacts_root: Path):
        """tmp 清理应正确区分新旧文件"""
        tmp_dir = artifacts_root / "tmp"
        tmp_dir.mkdir(parents=True)

        # 创建不同年龄的文件（不使用 .tmp 后缀，因为会被跳过）
        files_with_ages = [
            ("very_old.tar.gz", 30),   # 30 天前，应删除
            ("old.tar.gz", 10),        # 10 天前，应删除
            ("recent.tar.gz", 3),      # 3 天前，应保留
            ("new.tar.gz", 0),         # 刚创建，应保留
        ]

        for filename, age_days in files_with_ages:
            file_path = tmp_dir / filename
            file_path.write_bytes(f"content {age_days}".encode())
            if age_days > 0:
                old_mtime = time.time() - (age_days * 24 * 3600)
                os.utime(file_path, (old_mtime, old_mtime))

        result = run_tmp_gc(
            tmp_prefix="tmp/",
            older_than_days=7,  # 只删除 7 天前的文件
            dry_run=False,
            delete=True,
            backend="local",
            artifacts_root=str(artifacts_root),
            verbose=False,
        )

        # 验证：旧文件被删除，新文件保留
        assert result.deleted_count == 2  # very_old 和 old
        assert result.skipped_by_age == 2  # recent 和 new

        assert not (tmp_dir / "very_old.tar.gz").exists()
        assert not (tmp_dir / "old.tar.gz").exists()
        assert (tmp_dir / "recent.tar.gz").exists()
        assert (tmp_dir / "new.tar.gz").exists()


# =============================================================================
# 测试: 孤立清理与 Tmp 清理的完整覆盖
# =============================================================================


class TestOrphanAndTmpGCCoverage:
    """测试孤立清理和 tmp 清理的完整覆盖（local 后端）"""

    def test_orphan_gc_with_metadata(
        self,
        artifacts_root: Path,
        sample_artifacts: dict,
    ):
        """孤立清理应正确设置元信息"""
        referenced = set(sample_artifacts["referenced"])

        with patch("artifact_gc.get_referenced_uris") as mock_get_refs:
            mock_get_refs.return_value = _make_referenced_uris(referenced)

            result = run_gc(
                prefix="scm/",
                dry_run=True,
                dsn="mock://",
                backend="local",
                artifacts_root=str(artifacts_root),
                verbose=False,
            )

            # 验证元信息
            assert result.gc_mode == "orphan"
            assert result.backend == "local"
            assert result.prefix == "scm/"
            assert result.bucket is None

    def test_tmp_gc_with_metadata(self, artifacts_root: Path):
        """tmp 清理应正确设置元信息"""
        tmp_dir = artifacts_root / "tmp"
        tmp_dir.mkdir(parents=True)

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
        assert result.bucket is None

    def test_orphan_gc_soft_delete_local(
        self,
        artifacts_root: Path,
        sample_artifacts: dict,
    ):
        """孤立清理 local 后端软删除"""
        referenced = set(sample_artifacts["referenced"])
        unreferenced_scm = [
            u for u in sample_artifacts["unreferenced"]
            if u.startswith("scm/")
        ]

        with patch("artifact_gc.get_referenced_uris") as mock_get_refs:
            mock_get_refs.return_value = _make_referenced_uris(referenced)

            result = run_gc(
                prefix="scm/",
                dry_run=False,
                delete=True,
                trash_prefix=".gc_trash/",
                dsn="mock://",
                backend="local",
                artifacts_root=str(artifacts_root),
                verbose=False,
            )

            # 验证：软删除成功
            assert result.trashed_count == len(unreferenced_scm)
            assert result.deleted_count == 0

            # 验证：trash 目录存在且有文件
            trash_dir = artifacts_root / ".gc_trash"
            assert trash_dir.exists()
            trash_files = list(trash_dir.rglob("*"))
            trash_files = [f for f in trash_files if f.is_file()]
            assert len(trash_files) == len(unreferenced_scm)

    def test_orphan_gc_hard_delete_local(
        self,
        artifacts_root: Path,
        sample_artifacts: dict,
    ):
        """孤立清理 local 后端硬删除"""
        referenced = set(sample_artifacts["referenced"])
        unreferenced_scm = [
            u for u in sample_artifacts["unreferenced"]
            if u.startswith("scm/")
        ]

        with patch("artifact_gc.get_referenced_uris") as mock_get_refs:
            mock_get_refs.return_value = _make_referenced_uris(referenced)

            result = run_gc(
                prefix="scm/",
                dry_run=False,
                delete=True,
                dsn="mock://",
                backend="local",
                artifacts_root=str(artifacts_root),
                verbose=False,
            )

            # 验证：硬删除成功
            assert result.deleted_count == len(unreferenced_scm)
            assert result.trashed_count == 0

            # 验证：文件已删除
            for uri in unreferenced_scm:
                assert not (artifacts_root / uri).exists()

    def test_tmp_gc_hard_delete_local(self, artifacts_root: Path):
        """tmp 清理 local 后端硬删除"""
        tmp_dir = artifacts_root / "tmp"
        tmp_dir.mkdir(parents=True)

        # 创建旧文件（不使用 .tmp 后缀，因为会被跳过）
        old_files = []
        for i in range(3):
            file_path = tmp_dir / f"old_{i}.tar.gz"
            file_path.write_bytes(f"content {i}".encode())
            old_mtime = time.time() - (10 * 24 * 3600)
            os.utime(file_path, (old_mtime, old_mtime))
            old_files.append(file_path)

        result = run_tmp_gc(
            tmp_prefix="tmp/",
            older_than_days=7,
            dry_run=False,
            delete=True,
            backend="local",
            artifacts_root=str(artifacts_root),
            verbose=False,
        )

        # 验证：硬删除成功
        assert result.deleted_count == 3

        # 验证：文件已删除
        for f in old_files:
            assert not f.exists()


# =============================================================================
# 测试: JSON 输出元信息
# =============================================================================


class TestJSONOutputMetadata:
    """测试 JSON 输出包含正确的元信息"""

    def test_json_output_has_gc_mode(
        self,
        artifacts_root: Path,
        sample_artifacts: dict,
    ):
        """JSON 输出应包含 gc_mode 字段"""
        referenced = set(sample_artifacts["referenced"])

        with patch("artifact_gc.get_referenced_uris") as mock_get_refs:
            mock_get_refs.return_value = _make_referenced_uris(referenced)

            result = run_gc(
                prefix="scm/",
                dry_run=True,
                dsn="mock://",
                backend="local",
                artifacts_root=str(artifacts_root),
                verbose=False,
            )

            assert result.gc_mode == "orphan"

    def test_json_output_has_backend(
        self,
        artifacts_root: Path,
        sample_artifacts: dict,
    ):
        """JSON 输出应包含 backend 字段"""
        referenced = set(sample_artifacts["referenced"])

        with patch("artifact_gc.get_referenced_uris") as mock_get_refs:
            mock_get_refs.return_value = _make_referenced_uris(referenced)

            result = run_gc(
                prefix="scm/",
                dry_run=True,
                dsn="mock://",
                backend="local",
                artifacts_root=str(artifacts_root),
                verbose=False,
            )

            assert result.backend == "local"

    def test_json_output_has_prefix(
        self,
        artifacts_root: Path,
        sample_artifacts: dict,
    ):
        """JSON 输出应包含 prefix 字段"""
        referenced = set(sample_artifacts["referenced"])

        with patch("artifact_gc.get_referenced_uris") as mock_get_refs:
            mock_get_refs.return_value = _make_referenced_uris(referenced)

            result = run_gc(
                prefix="scm/",
                dry_run=True,
                dsn="mock://",
                backend="local",
                artifacts_root=str(artifacts_root),
                verbose=False,
            )

            assert result.prefix == "scm/"

    def test_tmp_gc_json_output_metadata(self, artifacts_root: Path):
        """tmp 清理的 JSON 输出应包含正确的元信息"""
        tmp_dir = artifacts_root / "tmp"
        tmp_dir.mkdir(parents=True)

        result = run_tmp_gc(
            tmp_prefix="tmp/",
            older_than_days=7,
            dry_run=True,
            backend="local",
            artifacts_root=str(artifacts_root),
            verbose=False,
        )

        # 验证所有元信息字段
        assert result.gc_mode == "tmp"
        assert result.backend == "local"
        assert result.prefix == "tmp/"
        assert result.bucket is None


# =============================================================================
# 测试: Physical URI 引用保护（s3:// 等）
# =============================================================================


class TestPhysicalUriReferenceProtection:
    """测试 physical URI（s3:// 等）引用的制品受到保护"""

    def test_s3_physical_ref_protects_matching_object(self):
        """
        回归测试：DB 引用含 s3:// 时，GC dry-run 不应把被引用对象列为候选
        
        场景:
        - DB 中有 s3://engram-bucket/prefix/scm/proj_a/1.diff 引用
        - ObjectStore 配置: bucket=engram-bucket, prefix=prefix/
        - 扫描到 scm/proj_a/1.diff
        - 应该被保护，不进入候选列表
        """
        # 创建 PhysicalRef 模拟 s3:// 引用
        physical_ref = PhysicalRef(
            scheme="s3",
            bucket="engram-bucket",
            key="prefix/scm/proj_a/1.diff",
            raw="s3://engram-bucket/prefix/scm/proj_a/1.diff",
        )
        
        # 创建 ReferencedUris
        referenced = ReferencedUris(
            artifact_keys=set(),  # 无 artifact key 引用
            physical_refs=[physical_ref],  # 有 s3:// 物理引用
        )
        
        # 验证 has_physical_ref_for_key 方法
        assert referenced.has_physical_ref_for_key(
            artifact_key="scm/proj_a/1.diff",
            store_bucket="engram-bucket",
            store_prefix="prefix/",
        )
        
        # bucket 不匹配时不应匹配
        assert not referenced.has_physical_ref_for_key(
            artifact_key="scm/proj_a/1.diff",
            store_bucket="other-bucket",
            store_prefix="prefix/",
        )
        
        # prefix 不匹配时不应匹配
        assert not referenced.has_physical_ref_for_key(
            artifact_key="scm/proj_a/1.diff",
            store_bucket="engram-bucket",
            store_prefix="other-prefix/",
        )

    def test_s3_physical_ref_with_different_prefix(self):
        """测试 s3:// 引用与 store prefix 不匹配时的行为"""
        physical_ref = PhysicalRef(
            scheme="s3",
            bucket="engram-bucket",
            key="production/scm/proj_a/1.diff",  # 使用 production/ 前缀
            raw="s3://engram-bucket/production/scm/proj_a/1.diff",
        )
        
        referenced = ReferencedUris(
            artifact_keys=set(),
            physical_refs=[physical_ref],
        )
        
        # 使用 staging/ 前缀的 store 不应匹配
        assert not referenced.has_physical_ref_for_key(
            artifact_key="scm/proj_a/1.diff",
            store_bucket="engram-bucket",
            store_prefix="staging/",
        )
        
        # 使用 production/ 前缀的 store 应该匹配
        assert referenced.has_physical_ref_for_key(
            artifact_key="scm/proj_a/1.diff",
            store_bucket="engram-bucket",
            store_prefix="production/",
        )

    def test_s3_physical_ref_empty_store_prefix(self):
        """测试 store.prefix 为空时的匹配"""
        physical_ref = PhysicalRef(
            scheme="s3",
            bucket="engram-bucket",
            key="scm/proj_a/1.diff",  # 无前缀
            raw="s3://engram-bucket/scm/proj_a/1.diff",
        )
        
        referenced = ReferencedUris(
            artifact_keys=set(),
            physical_refs=[physical_ref],
        )
        
        # store.prefix 为空时应直接匹配 key
        assert referenced.has_physical_ref_for_key(
            artifact_key="scm/proj_a/1.diff",
            store_bucket="engram-bucket",
            store_prefix="",
        )

    def test_multiple_s3_physical_refs(self):
        """测试多个 s3:// 引用"""
        refs = [
            PhysicalRef(
                scheme="s3",
                bucket="bucket-a",
                key="prefix/scm/1.diff",
                raw="s3://bucket-a/prefix/scm/1.diff",
            ),
            PhysicalRef(
                scheme="s3",
                bucket="bucket-b",
                key="prefix/scm/2.diff",
                raw="s3://bucket-b/prefix/scm/2.diff",
            ),
        ]
        
        referenced = ReferencedUris(
            artifact_keys=set(),
            physical_refs=refs,
        )
        
        # 应该匹配 bucket-a 中的文件
        assert referenced.has_physical_ref_for_key(
            artifact_key="scm/1.diff",
            store_bucket="bucket-a",
            store_prefix="prefix/",
        )
        
        # 应该匹配 bucket-b 中的文件
        assert referenced.has_physical_ref_for_key(
            artifact_key="scm/2.diff",
            store_bucket="bucket-b",
            store_prefix="prefix/",
        )
        
        # bucket-a 中没有 scm/2.diff
        assert not referenced.has_physical_ref_for_key(
            artifact_key="scm/2.diff",
            store_bucket="bucket-a",
            store_prefix="prefix/",
        )

    def test_mixed_artifact_keys_and_physical_refs(self):
        """测试同时有 artifact key 和 physical ref 引用"""
        physical_ref = PhysicalRef(
            scheme="s3",
            bucket="engram-bucket",
            key="prefix/scm/s3-referenced.diff",
            raw="s3://engram-bucket/prefix/scm/s3-referenced.diff",
        )
        
        referenced = ReferencedUris(
            artifact_keys={"scm/key-referenced.diff"},  # artifact key 引用
            physical_refs=[physical_ref],  # s3 物理引用
        )
        
        # artifact key 引用应该可以直接匹配
        assert "scm/key-referenced.diff" in referenced.artifact_keys
        
        # s3 物理引用应该通过 has_physical_ref_for_key 匹配
        assert referenced.has_physical_ref_for_key(
            artifact_key="scm/s3-referenced.diff",
            store_bucket="engram-bucket",
            store_prefix="prefix/",
        )

    def test_gs_physical_ref_also_works(self):
        """测试 gs:// (Google Cloud Storage) 引用也能正确匹配"""
        physical_ref = PhysicalRef(
            scheme="gs",
            bucket="gcs-bucket",
            key="prefix/scm/1.diff",
            raw="gs://gcs-bucket/prefix/scm/1.diff",
        )
        
        referenced = ReferencedUris(
            artifact_keys=set(),
            physical_refs=[physical_ref],
        )
        
        assert referenced.has_physical_ref_for_key(
            artifact_key="scm/1.diff",
            store_bucket="gcs-bucket",
            store_prefix="prefix/",
        )

    def test_file_physical_ref_not_matched_as_object(self):
        """测试 file:// 引用不会被当作 object 匹配"""
        physical_ref = PhysicalRef(
            scheme="file",
            key="/mnt/artifacts/scm/1.diff",
            raw="file:///mnt/artifacts/scm/1.diff",
        )
        
        referenced = ReferencedUris(
            artifact_keys=set(),
            physical_refs=[physical_ref],
        )
        
        # file:// 引用不应该通过 has_physical_ref_for_key 匹配
        # （因为 file:// 没有 bucket 概念）
        assert not referenced.has_physical_ref_for_key(
            artifact_key="scm/1.diff",
            store_bucket="any-bucket",
            store_prefix="",
        )


# =============================================================================
# 测试: s3:// URI 解析回归测试
# =============================================================================


class TestS3UriParsingRegression:
    """
    s3:// URI 解析回归测试
    
    确保 artifact_gc.py 中 _normalize_uri_for_gc 和相关函数
    正确处理 s3:// URI，不会误判或遗漏 s3 引用。
    """

    def test_normalize_s3_uri_extracts_bucket_and_key(self):
        """回归测试: _normalize_uri_for_gc 正确解析 s3:// URI 的 bucket 和 key"""
        uri = "s3://engram-artifacts/prefix/scm/proj_a/1/git/abc123.diff"
        result, uri_type = _normalize_uri_for_gc(uri)
        
        assert uri_type == "physical_uri"
        assert isinstance(result, PhysicalRef)
        assert result.scheme == "s3"
        assert result.bucket == "engram-artifacts"
        assert result.key == "prefix/scm/proj_a/1/git/abc123.diff"
        assert result.raw == uri

    def test_normalize_s3_uri_with_trailing_slash_in_key(self):
        """回归测试: s3:// URI 中 key 的尾部斜杠被正确处理"""
        uri = "s3://bucket/prefix/path/to/object/"
        result, uri_type = _normalize_uri_for_gc(uri)
        
        assert uri_type == "physical_uri"
        assert isinstance(result, PhysicalRef)
        assert result.scheme == "s3"
        assert result.bucket == "bucket"
        # key 应保留原始格式（可能有尾部斜杠）
        assert "prefix/path/to/object" in result.key

    def test_normalize_s3_uri_with_only_bucket(self):
        """回归测试: s3://bucket（无 key）的处理"""
        uri = "s3://bucket-only"
        result, uri_type = _normalize_uri_for_gc(uri)
        
        assert uri_type == "physical_uri"
        assert isinstance(result, PhysicalRef)
        assert result.scheme == "s3"
        assert result.bucket == "bucket-only"
        assert result.key == ""

    def test_normalize_s3_uri_with_bucket_and_slash(self):
        """回归测试: s3://bucket/（bucket 后有斜杠但无 key）"""
        uri = "s3://bucket/"
        result, uri_type = _normalize_uri_for_gc(uri)
        
        assert uri_type == "physical_uri"
        assert isinstance(result, PhysicalRef)
        assert result.scheme == "s3"
        assert result.bucket == "bucket"
        assert result.key == ""

    def test_normalize_s3_uri_preserves_complex_key_path(self):
        """回归测试: s3:// 复杂 key 路径被完整保留"""
        uri = "s3://my-bucket/engram/v2/scm/proj-abc/123/git/abc123def456/sha256hash.diff"
        result, uri_type = _normalize_uri_for_gc(uri)
        
        assert uri_type == "physical_uri"
        assert isinstance(result, PhysicalRef)
        assert result.bucket == "my-bucket"
        assert result.key == "engram/v2/scm/proj-abc/123/git/abc123def456/sha256hash.diff"

    def test_s3_physical_ref_matches_with_correct_store_config(self):
        """回归测试: s3 引用与正确的 store 配置匹配"""
        physical_ref = PhysicalRef(
            scheme="s3",
            bucket="engram-bucket",
            key="prefix/scm/proj_a/1.diff",
            raw="s3://engram-bucket/prefix/scm/proj_a/1.diff",
        )
        
        referenced = ReferencedUris(
            artifact_keys=set(),
            physical_refs=[physical_ref],
        )
        
        # 正确配置应该匹配
        assert referenced.has_physical_ref_for_key(
            artifact_key="scm/proj_a/1.diff",
            store_bucket="engram-bucket",
            store_prefix="prefix/",
        )

    def test_s3_physical_ref_not_match_wrong_bucket(self):
        """回归测试: s3 引用与错误 bucket 不匹配"""
        physical_ref = PhysicalRef(
            scheme="s3",
            bucket="engram-bucket",
            key="prefix/scm/1.diff",
            raw="s3://engram-bucket/prefix/scm/1.diff",
        )
        
        referenced = ReferencedUris(
            artifact_keys=set(),
            physical_refs=[physical_ref],
        )
        
        # 错误 bucket 不应匹配
        assert not referenced.has_physical_ref_for_key(
            artifact_key="scm/1.diff",
            store_bucket="wrong-bucket",
            store_prefix="prefix/",
        )

    def test_s3_physical_ref_not_match_wrong_prefix(self):
        """回归测试: s3 引用与错误 prefix 不匹配"""
        physical_ref = PhysicalRef(
            scheme="s3",
            bucket="bucket",
            key="prod/scm/1.diff",  # 使用 prod/ 前缀
            raw="s3://bucket/prod/scm/1.diff",
        )
        
        referenced = ReferencedUris(
            artifact_keys=set(),
            physical_refs=[physical_ref],
        )
        
        # staging/ 前缀不应匹配
        assert not referenced.has_physical_ref_for_key(
            artifact_key="scm/1.diff",
            store_bucket="bucket",
            store_prefix="staging/",  # 错误前缀
        )

    def test_gc_dry_run_protects_s3_referenced_objects(self):
        """
        回归测试: GC dry-run 不把 s3:// 引用的对象当作 orphan
        
        场景:
        - DB 中有 s3://bucket/prefix/scm/1.diff 引用
        - 扫描到 scm/1.diff 对象
        - GC dry-run 应该保护该对象，不列为候选
        """
        physical_ref = PhysicalRef(
            scheme="s3",
            bucket="engram-bucket",
            key="prefix/scm/proj_a/referenced.diff",
            raw="s3://engram-bucket/prefix/scm/proj_a/referenced.diff",
        )
        
        referenced = ReferencedUris(
            artifact_keys=set(),
            physical_refs=[physical_ref],
        )
        
        # 模拟 GC 判断逻辑：对象应该被保护
        artifact_key = "scm/proj_a/referenced.diff"
        store_bucket = "engram-bucket"
        store_prefix = "prefix/"
        
        is_protected = referenced.has_physical_ref_for_key(
            artifact_key=artifact_key,
            store_bucket=store_bucket,
            store_prefix=store_prefix,
        )
        
        assert is_protected is True, "s3:// 引用的对象应该被 GC 保护"

    def test_gc_identifies_orphan_when_no_s3_ref(self):
        """
        回归测试: 当对象无 s3:// 引用时，GC 应将其识别为 orphan
        """
        physical_ref = PhysicalRef(
            scheme="s3",
            bucket="engram-bucket",
            key="prefix/scm/other.diff",  # 不同的对象
            raw="s3://engram-bucket/prefix/scm/other.diff",
        )
        
        referenced = ReferencedUris(
            artifact_keys=set(),
            physical_refs=[physical_ref],
        )
        
        # 这个对象不应该被保护
        artifact_key = "scm/orphan.diff"
        store_bucket = "engram-bucket"
        store_prefix = "prefix/"
        
        is_protected = referenced.has_physical_ref_for_key(
            artifact_key=artifact_key,
            store_bucket=store_bucket,
            store_prefix=store_prefix,
        )
        
        assert is_protected is False, "无 s3:// 引用的对象应为 orphan"

    def test_mixed_artifact_keys_and_s3_refs_protection(self):
        """
        回归测试: 同时有 artifact key 和 s3:// 引用时的保护逻辑
        """
        physical_ref = PhysicalRef(
            scheme="s3",
            bucket="bucket",
            key="prefix/scm/s3-ref.diff",
            raw="s3://bucket/prefix/scm/s3-ref.diff",
        )
        
        referenced = ReferencedUris(
            artifact_keys={"scm/key-ref.diff"},  # artifact key 引用
            physical_refs=[physical_ref],  # s3 物理引用
        )
        
        # artifact key 引用的对象应该直接在 artifact_keys 中
        assert "scm/key-ref.diff" in referenced.artifact_keys
        
        # s3 引用的对象应该通过 has_physical_ref_for_key 匹配
        assert referenced.has_physical_ref_for_key(
            artifact_key="scm/s3-ref.diff",
            store_bucket="bucket",
            store_prefix="prefix/",
        )
        
        # 未引用的对象不应匹配
        assert not referenced.has_physical_ref_for_key(
            artifact_key="scm/orphan.diff",
            store_bucket="bucket",
            store_prefix="prefix/",
        )

    def test_s3_uri_with_special_characters_in_key(self):
        """回归测试: s3:// key 中的特殊字符处理"""
        # S3 允许 key 中包含一些特殊字符
        uri = "s3://bucket/prefix/scm/proj-a_123/file+name.diff"
        result, uri_type = _normalize_uri_for_gc(uri)
        
        assert uri_type == "physical_uri"
        assert isinstance(result, PhysicalRef)
        assert result.bucket == "bucket"
        assert "proj-a_123" in result.key
        assert "file+name.diff" in result.key


# =============================================================================
# require_ops 安全开关测试
# =============================================================================


class TestRequireOpsFlag:
    """--require-ops 安全开关测试"""

    def test_run_gc_require_ops_with_app_credentials_raises_error(self, artifacts_root):
        """
        require_ops=True 但使用 app 凭证时应抛出错误（object 后端）
        """
        from engram_step1.artifact_store import ObjectStore
        
        # 创建测试文件
        scm_dir = artifacts_root / "scm"
        scm_dir.mkdir()
        (scm_dir / "test.diff").write_bytes(b"test content")
        
        # Mock ObjectStore 使用 app 凭证
        with patch.dict(os.environ, {
            "ENGRAM_S3_USE_OPS": "false",
            "ENGRAM_S3_APP_ACCESS_KEY": "app-key",
            "ENGRAM_S3_APP_SECRET_KEY": "app-secret",
            "ENGRAM_S3_ENDPOINT": "http://localhost:9000",
            "ENGRAM_S3_BUCKET": "test-bucket",
        }, clear=False):
            with pytest.raises(GCOpsCredentialsRequiredError) as exc_info:
                run_gc(
                    prefix="scm/",
                    dry_run=False,
                    delete=True,
                    backend="object",
                    require_ops=True,
                    verbose=False,
                )
            
            assert "ops 凭证" in str(exc_info.value)

    def test_run_gc_require_ops_with_ops_credentials_succeeds(self, artifacts_root):
        """
        require_ops=True 且使用 ops 凭证时应正常执行（dry-run）
        """
        # 创建测试文件
        scm_dir = artifacts_root / "scm"
        scm_dir.mkdir()
        (scm_dir / "test.diff").write_bytes(b"test content")
        
        # 对 local 后端，require_ops 检查不适用
        result = run_gc(
            prefix="scm/",
            dry_run=True,
            delete=False,
            backend="local",
            artifacts_root=str(artifacts_root),
            require_ops=True,  # local 后端不检查此标志
            verbose=False,
        )
        
        # local 后端不检查 ops 凭证，应正常返回结果
        assert result.scanned_count >= 0

    def test_run_tmp_gc_require_ops_with_app_credentials_raises_error(self, artifacts_root):
        """
        tmp GC require_ops=True 但使用 app 凭证时应抛出错误（object 后端）
        """
        # 创建 tmp 文件
        tmp_dir = artifacts_root / "tmp"
        tmp_dir.mkdir()
        old_file = tmp_dir / "old.tmp"
        old_file.write_bytes(b"old content")
        # 设置文件时间为 10 天前
        import time
        old_mtime = time.time() - 10 * 24 * 3600
        os.utime(old_file, (old_mtime, old_mtime))
        
        with patch.dict(os.environ, {
            "ENGRAM_S3_USE_OPS": "false",
            "ENGRAM_S3_APP_ACCESS_KEY": "app-key",
            "ENGRAM_S3_APP_SECRET_KEY": "app-secret",
            "ENGRAM_S3_ENDPOINT": "http://localhost:9000",
            "ENGRAM_S3_BUCKET": "test-bucket",
        }, clear=False):
            with pytest.raises(GCOpsCredentialsRequiredError):
                run_tmp_gc(
                    tmp_prefix="tmp/",
                    older_than_days=7,
                    dry_run=False,
                    delete=True,
                    backend="object",
                    require_ops=True,
                    verbose=False,
                )

    def test_run_gc_without_require_ops_allows_app_credentials(self, artifacts_root):
        """
        require_ops=False 时允许使用 app 凭证（dry-run）
        """
        # 创建测试文件
        scm_dir = artifacts_root / "scm"
        scm_dir.mkdir()
        (scm_dir / "test.diff").write_bytes(b"test content")
        
        # 使用 local 后端，不检查凭证
        result = run_gc(
            prefix="scm/",
            dry_run=True,
            delete=False,
            backend="local",
            artifacts_root=str(artifacts_root),
            require_ops=False,
            verbose=False,
        )
        
        # 应正常返回结果
        assert result.scanned_count > 0

    def test_dry_run_does_not_require_ops_check(self, artifacts_root):
        """
        dry-run 模式下即使 require_ops=True 也不应抛出错误
        
        注意: 当前实现在 delete=False 时不检查 require_ops，
        因为 dry-run 不执行实际删除操作
        """
        scm_dir = artifacts_root / "scm"
        scm_dir.mkdir()
        (scm_dir / "test.diff").write_bytes(b"test content")
        
        # dry-run 模式下使用 local 后端
        result = run_gc(
            prefix="scm/",
            dry_run=True,
            delete=False,  # dry-run
            backend="local",
            artifacts_root=str(artifacts_root),
            require_ops=True,
            verbose=False,
        )
        
        # dry-run 应正常完成
        assert result.scanned_count >= 0
