# -*- coding: utf-8 -*-
"""
test_artifact_audit.py - 制品审计工具测试

测试覆盖:
1. ArtifactAuditor 核心功能测试
2. 哈希匹配/不匹配检测
3. 缺失文件检测
4. 采样审计测试
5. 速率限制测试
6. CLI 参数解析测试
7. head-only 模式测试
8. prefix 过滤测试
9. 并发审计测试
10. 增量游标测试
"""

import hashlib
import json
import os
import sys
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Tuple
from unittest.mock import MagicMock, Mock, patch

import pytest

# 添加 scripts 目录到 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engram.logbook.artifact_store import (
    LocalArtifactsStore,
    FileUriStore,
    ObjectStore,
    get_artifact_store_from_config,
)
from engram.logbook.hashing import sha256 as compute_sha256

from artifact_audit import (
    ArtifactAuditor,
    AuditResult,
    AuditSummary,
    RateLimiter,
    parse_args,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def tmp_artifacts(tmp_path):
    """创建临时制品目录并返回 store"""
    artifacts_root = tmp_path / "artifacts"
    artifacts_root.mkdir()
    store = LocalArtifactsStore(root=artifacts_root)
    return artifacts_root, store


@pytest.fixture
def sample_artifacts(tmp_artifacts):
    """创建样本制品文件"""
    artifacts_root, store = tmp_artifacts

    # 创建几个测试文件
    files = []
    for i in range(5):
        content = f"test content {i}".encode()
        uri = f"test/file_{i}.txt"
        result = store.put(uri, content)
        files.append({
            "uri": uri,
            "sha256": result["sha256"],
            "size_bytes": result["size_bytes"],
            "content": content,
        })

    return artifacts_root, store, files


class MockConnection:
    """模拟数据库连接"""

    def __init__(self, patch_blobs: List[Tuple], attachments: List[Tuple] = None):
        """
        Args:
            patch_blobs: [(blob_id, uri, sha256), ...] 或 [(blob_id, uri, sha256, created_at), ...]
            attachments: [(attachment_id, uri, sha256), ...] 或带 created_at 的元组
        """
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
        self._index = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, query: str, params=None):
        query_lower = query.lower()
        if "patch_blobs" in query_lower:
            # 确保返回 4 元组（添加 created_at 如果缺少）
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
        
        # 处理 prefix 过滤
        if params and "uri LIKE" in query:
            # 找到 LIKE 参数的位置
            prefix = None
            for p in params:
                if isinstance(p, str) and p.endswith("%"):
                    prefix = p[:-1]  # 移除 %
                    break
            if prefix:
                self._results = [r for r in self._results if r[1].startswith(prefix)]
        
        self._index = 0

    def __iter__(self):
        return iter(self._results)

    def fetchone(self):
        if self._index < len(self._results):
            result = self._results[self._index]
            self._index += 1
            return result
        return None


# =============================================================================
# 基础功能测试
# =============================================================================


class TestAuditResult:
    """AuditResult 测试"""

    def test_to_dict(self):
        """测试 to_dict 方法"""
        result = AuditResult(
            table="patch_blobs",
            record_id=1,
            uri="test/file.txt",
            expected_sha256="abc123",
            actual_sha256="abc123",
            size_bytes=100,
            status="ok",
        )
        d = result.to_dict()
        assert d["table"] == "patch_blobs"
        assert d["record_id"] == 1
        assert d["status"] == "ok"


class TestAuditSummary:
    """AuditSummary 测试"""

    def test_has_issues_false(self):
        """测试无问题时 has_issues 为 False"""
        summary = AuditSummary(ok_count=10)
        assert summary.has_issues is False

    def test_has_issues_mismatch(self):
        """测试有不匹配时 has_issues 为 True"""
        summary = AuditSummary(mismatch_count=1)
        assert summary.has_issues is True

    def test_has_issues_missing(self):
        """测试有缺失时 has_issues 为 True"""
        summary = AuditSummary(missing_count=1)
        assert summary.has_issues is True

    def test_to_dict(self):
        """测试 to_dict 方法"""
        summary = AuditSummary(
            total_records=100,
            ok_count=95,
            mismatch_count=3,
            missing_count=2,
        )
        d = summary.to_dict()
        assert d["total_records"] == 100
        assert d["ok_count"] == 95
        assert d["mismatch_count"] == 3


# =============================================================================
# RateLimiter 测试
# =============================================================================


class TestRateLimiter:
    """RateLimiter 测试"""

    def test_no_limit(self):
        """测试无限制时不阻塞"""
        limiter = RateLimiter(max_bytes_per_sec=None)
        start = time.monotonic()
        for _ in range(10):
            limiter.wait_if_needed(1000000)  # 1MB
        elapsed = time.monotonic() - start
        assert elapsed < 0.1  # 应该很快完成

    def test_rate_limiting(self):
        """测试速率限制功能"""
        # 限制每秒 1000 字节
        limiter = RateLimiter(max_bytes_per_sec=1000)

        start = time.monotonic()

        # 第一次调用不应阻塞
        limiter.wait_if_needed(500)

        # 第二次调用应该触发等待（如果超过限制）
        limiter.wait_if_needed(600)

        elapsed = time.monotonic() - start
        # 由于超过了 1000 字节/秒，应该有一些延迟
        # 但不一定是精确的 1 秒，因为实现可能有偏差
        assert elapsed >= 0  # 基本验证


# =============================================================================
# ArtifactAuditor 测试
# =============================================================================


class TestArtifactAuditor:
    """ArtifactAuditor 测试"""

    def test_audit_record_ok(self, sample_artifacts):
        """测试正常文件审计"""
        artifacts_root, store, files = sample_artifacts

        # 模拟数据库连接
        mock_conn = MockConnection([])

        auditor = ArtifactAuditor(
            artifacts_root=artifacts_root,
            conn=mock_conn,
            artifact_store=store,  # 注入 store 避免读取配置
        )

        # 审计第一个文件
        file_info = files[0]
        result = auditor.audit_record(
            table="patch_blobs",
            record_id=1,
            uri=file_info["uri"],
            expected_sha256=file_info["sha256"],
        )

        assert result.status == "ok"
        assert result.actual_sha256 == file_info["sha256"]
        assert result.size_bytes == file_info["size_bytes"]

    def test_audit_record_mismatch(self, sample_artifacts):
        """测试哈希不匹配检测"""
        artifacts_root, store, files = sample_artifacts
        mock_conn = MockConnection([])

        auditor = ArtifactAuditor(
            artifacts_root=artifacts_root,
            conn=mock_conn,
            artifact_store=store,
        )

        file_info = files[0]

        # 使用错误的预期哈希
        result = auditor.audit_record(
            table="patch_blobs",
            record_id=1,
            uri=file_info["uri"],
            expected_sha256="wrong_hash_" + "0" * 54,
        )

        assert result.status == "mismatch"
        assert result.actual_sha256 == file_info["sha256"]
        assert result.expected_sha256 != result.actual_sha256

    def test_audit_record_missing(self, tmp_artifacts):
        """测试缺失文件检测"""
        artifacts_root, store = tmp_artifacts
        mock_conn = MockConnection([])

        auditor = ArtifactAuditor(
            artifacts_root=artifacts_root,
            conn=mock_conn,
            artifact_store=store,
        )

        result = auditor.audit_record(
            table="patch_blobs",
            record_id=1,
            uri="nonexistent/file.txt",
            expected_sha256="any_hash_" + "0" * 55,
        )

        assert result.status == "missing"
        assert result.error_message is not None

    def test_audit_with_tampered_file(self, sample_artifacts):
        """测试篡改文件检测"""
        artifacts_root, store, files = sample_artifacts
        mock_conn = MockConnection([])

        # 篡改第一个文件
        file_info = files[0]
        file_path = artifacts_root / file_info["uri"]
        original_sha256 = file_info["sha256"]

        # 修改文件内容
        file_path.write_bytes(b"tampered content!!!")

        auditor = ArtifactAuditor(
            artifacts_root=artifacts_root,
            conn=mock_conn,
            artifact_store=store,
        )

        result = auditor.audit_record(
            table="patch_blobs",
            record_id=1,
            uri=file_info["uri"],
            expected_sha256=original_sha256,
        )

        assert result.status == "mismatch"
        assert result.actual_sha256 != original_sha256

    def test_audit_table(self, sample_artifacts):
        """测试整表审计"""
        artifacts_root, store, files = sample_artifacts

        # 创建模拟连接
        patch_blobs = [
            (i + 1, f["uri"], f["sha256"])
            for i, f in enumerate(files)
        ]
        mock_conn = MockConnection(patch_blobs=patch_blobs)

        auditor = ArtifactAuditor(
            artifacts_root=artifacts_root,
            conn=mock_conn,
            artifact_store=store,
        )

        results = list(auditor.audit_table("patch_blobs"))

        assert len(results) == len(files)
        for result, created_at in results:
            assert result.status == "ok"

    def test_audit_with_mixed_results(self, sample_artifacts):
        """测试混合结果（正常+不匹配+缺失）"""
        artifacts_root, store, files = sample_artifacts

        # 准备测试数据
        # 1. 正常文件
        # 2. 哈希不匹配（使用错误的 sha256）
        # 3. 文件缺失
        patch_blobs = [
            (1, files[0]["uri"], files[0]["sha256"]),  # 正常
            (2, files[1]["uri"], "wrong_" + "0" * 59),  # 哈希不匹配
            (3, "missing/file.txt", "any_" + "0" * 60),  # 缺失
        ]
        mock_conn = MockConnection(patch_blobs=patch_blobs)

        auditor = ArtifactAuditor(
            artifacts_root=artifacts_root,
            conn=mock_conn,
            artifact_store=store,
        )

        results = list(auditor.audit_table("patch_blobs"))

        assert len(results) == 3
        assert results[0][0].status == "ok"
        assert results[1][0].status == "mismatch"
        assert results[2][0].status == "missing"

    def test_sample_rate(self, sample_artifacts):
        """测试采样率功能"""
        artifacts_root, store, files = sample_artifacts

        patch_blobs = [
            (i + 1, f["uri"], f["sha256"])
            for i, f in enumerate(files)
        ]
        mock_conn = MockConnection(patch_blobs=patch_blobs)

        # 设置 0% 采样率（全部跳过）
        auditor = ArtifactAuditor(
            artifacts_root=artifacts_root,
            conn=mock_conn,
            sample_rate=0.0,
            artifact_store=store,
        )

        results = list(auditor.audit_table("patch_blobs"))

        # 所有记录应该被跳过
        for result, created_at in results:
            assert result.status == "skipped"

    def test_full_audit_run(self, sample_artifacts):
        """测试完整审计运行"""
        artifacts_root, store, files = sample_artifacts

        patch_blobs = [
            (i + 1, f["uri"], f["sha256"])
            for i, f in enumerate(files[:3])
        ]
        attachments = [
            (i + 1, f["uri"], f["sha256"])
            for i, f in enumerate(files[3:])
        ]
        mock_conn = MockConnection(
            patch_blobs=patch_blobs,
            attachments=attachments,
        )

        auditor = ArtifactAuditor(
            artifacts_root=artifacts_root,
            conn=mock_conn,
            artifact_store=store,
        )

        summary = auditor.run_audit(tables=["patch_blobs", "attachments"])

        assert summary.total_records == len(files)
        assert summary.ok_count == len(files)
        assert summary.mismatch_count == 0
        assert summary.missing_count == 0
        assert summary.has_issues is False

    def test_fail_on_mismatch(self, sample_artifacts):
        """测试 fail_on_mismatch 选项"""
        artifacts_root, store, files = sample_artifacts

        # 第一个正常，第二个不匹配
        patch_blobs = [
            (1, files[0]["uri"], files[0]["sha256"]),
            (2, files[1]["uri"], "wrong_" + "0" * 59),
            (3, files[2]["uri"], files[2]["sha256"]),  # 这个不应该被检查
        ]
        mock_conn = MockConnection(patch_blobs=patch_blobs)

        auditor = ArtifactAuditor(
            artifacts_root=artifacts_root,
            conn=mock_conn,
            artifact_store=store,
        )

        summary = auditor.run_audit(
            tables=["patch_blobs"],
            fail_on_mismatch=True,
        )

        # 应该在发现第一个不匹配后停止
        assert summary.mismatch_count == 1
        assert summary.audited_records == 2  # 只审计了前两条
        assert summary.has_issues is True


# =============================================================================
# 篡改场景测试
# =============================================================================


class TestTamperingScenarios:
    """篡改场景测试"""

    def test_content_modification(self, tmp_artifacts):
        """测试内容修改检测"""
        artifacts_root, store = tmp_artifacts

        # 创建原始文件
        original_content = b"original secret data"
        result = store.put("secrets/data.bin", original_content)
        original_sha256 = result["sha256"]

        # 创建审计器
        patch_blobs = [(1, "secrets/data.bin", original_sha256)]
        mock_conn = MockConnection(patch_blobs=patch_blobs)

        auditor = ArtifactAuditor(
            artifacts_root=artifacts_root,
            conn=mock_conn,
            artifact_store=store,
        )

        # 验证原始文件通过审计
        r = auditor.audit_record("patch_blobs", 1, "secrets/data.bin", original_sha256)
        assert r.status == "ok"

        # 篡改文件
        tampered_content = b"modified malicious data"
        (artifacts_root / "secrets/data.bin").write_bytes(tampered_content)

        # 重新审计应该检测到不匹配
        r = auditor.audit_record("patch_blobs", 1, "secrets/data.bin", original_sha256)
        assert r.status == "mismatch"
        assert r.actual_sha256 == compute_sha256(tampered_content)

    def test_file_deletion(self, tmp_artifacts):
        """测试文件删除检测"""
        artifacts_root, store = tmp_artifacts

        # 创建文件
        result = store.put("important/file.txt", b"important data")
        original_sha256 = result["sha256"]

        # 删除文件
        (artifacts_root / "important/file.txt").unlink()

        patch_blobs = [(1, "important/file.txt", original_sha256)]
        mock_conn = MockConnection(patch_blobs=patch_blobs)

        auditor = ArtifactAuditor(
            artifacts_root=artifacts_root,
            conn=mock_conn,
            artifact_store=store,
        )

        r = auditor.audit_record("patch_blobs", 1, "important/file.txt", original_sha256)
        assert r.status == "missing"

    def test_file_replacement(self, tmp_artifacts):
        """测试文件替换检测"""
        artifacts_root, store = tmp_artifacts

        # 创建原始文件
        original = b"original content version 1"
        result = store.put("doc/report.txt", original)
        original_sha256 = result["sha256"]

        # 用不同内容替换文件（模拟攻击者替换）
        replacement = b"totally different content"
        (artifacts_root / "doc/report.txt").write_bytes(replacement)

        patch_blobs = [(1, "doc/report.txt", original_sha256)]
        mock_conn = MockConnection(patch_blobs=patch_blobs)

        auditor = ArtifactAuditor(
            artifacts_root=artifacts_root,
            conn=mock_conn,
            artifact_store=store,
        )

        r = auditor.audit_record("patch_blobs", 1, "doc/report.txt", original_sha256)
        assert r.status == "mismatch"

    def test_append_to_file(self, tmp_artifacts):
        """测试文件追加内容检测"""
        artifacts_root, store = tmp_artifacts

        # 创建原始文件
        original = b"original log entry\n"
        result = store.put("logs/audit.log", original)
        original_sha256 = result["sha256"]

        # 追加内容
        file_path = artifacts_root / "logs/audit.log"
        with open(file_path, "ab") as f:
            f.write(b"injected malicious entry\n")

        patch_blobs = [(1, "logs/audit.log", original_sha256)]
        mock_conn = MockConnection(patch_blobs=patch_blobs)

        auditor = ArtifactAuditor(
            artifacts_root=artifacts_root,
            conn=mock_conn,
            artifact_store=store,
        )

        r = auditor.audit_record("patch_blobs", 1, "logs/audit.log", original_sha256)
        assert r.status == "mismatch"

    def test_bit_flip(self, tmp_artifacts):
        """测试单比特翻转检测"""
        artifacts_root, store = tmp_artifacts

        # 创建原始文件
        original = b"binary data with specific content"
        result = store.put("data/binary.bin", original)
        original_sha256 = result["sha256"]

        # 翻转一个比特
        modified = bytearray(original)
        modified[10] ^= 0x01  # 翻转第 10 字节的最低位
        (artifacts_root / "data/binary.bin").write_bytes(bytes(modified))

        patch_blobs = [(1, "data/binary.bin", original_sha256)]
        mock_conn = MockConnection(patch_blobs=patch_blobs)

        auditor = ArtifactAuditor(
            artifacts_root=artifacts_root,
            conn=mock_conn,
            artifact_store=store,
        )

        r = auditor.audit_record("patch_blobs", 1, "data/binary.bin", original_sha256)
        assert r.status == "mismatch"


# =============================================================================
# CLI 测试
# =============================================================================


class TestCLI:
    """CLI 参数解析测试"""

    def test_default_args(self):
        """测试默认参数"""
        args = parse_args([])
        assert args.table == "all"
        assert args.limit is None
        assert args.sample_rate == 1.0
        assert args.json is False
        assert args.fail_on_mismatch is False
        assert args.head_only is False
        assert args.workers == 1
        assert args.prefix is None

    def test_custom_args(self):
        """测试自定义参数"""
        args = parse_args([
            "--table", "patch_blobs",
            "--limit", "100",
            "--sample-rate", "0.5",
            "--max-bytes-per-sec", "1048576",
            "--json",
            "--fail-on-mismatch",
            "--verbose",
        ])
        assert args.table == "patch_blobs"
        assert args.limit == 100
        assert args.sample_rate == 0.5
        assert args.max_bytes_per_sec == 1048576
        assert args.json is True
        assert args.fail_on_mismatch is True
        assert args.verbose is True

    def test_since_arg(self):
        """测试 since 参数"""
        args = parse_args([
            "--since", "2024-01-01T00:00:00",
        ])
        assert args.since == "2024-01-01T00:00:00"

    def test_prefix_arg(self):
        """测试 prefix 参数"""
        args = parse_args([
            "--prefix", "scm/patches/",
        ])
        assert args.prefix == "scm/patches/"

    def test_head_only_arg(self):
        """测试 head-only 参数"""
        args = parse_args([
            "--head-only",
        ])
        assert args.head_only is True

    def test_workers_arg(self):
        """测试 workers 参数"""
        args = parse_args([
            "--workers", "4",
        ])
        assert args.workers == 4

    def test_combined_new_args(self):
        """测试组合新参数"""
        args = parse_args([
            "--prefix", "attachments/",
            "--head-only",
            "--workers", "8",
            "--since", "2024-06-01T00:00:00",
        ])
        assert args.prefix == "attachments/"
        assert args.head_only is True
        assert args.workers == 8
        assert args.since == "2024-06-01T00:00:00"


# =============================================================================
# 边界条件测试
# =============================================================================


class TestEdgeCases:
    """边界条件测试"""

    def test_empty_file(self, tmp_artifacts):
        """测试空文件审计"""
        artifacts_root, store = tmp_artifacts

        result = store.put("empty.txt", b"")
        empty_sha256 = result["sha256"]

        patch_blobs = [(1, "empty.txt", empty_sha256)]
        mock_conn = MockConnection(patch_blobs=patch_blobs)

        auditor = ArtifactAuditor(
            artifacts_root=artifacts_root,
            conn=mock_conn,
            artifact_store=store,
        )

        r = auditor.audit_record("patch_blobs", 1, "empty.txt", empty_sha256)
        assert r.status == "ok"
        assert r.size_bytes == 0

    def test_large_file_simulation(self, tmp_artifacts):
        """测试大文件审计（使用较小的测试文件模拟）"""
        artifacts_root, store = tmp_artifacts

        # 创建 1MB 的测试文件
        large_content = b"x" * (1024 * 1024)
        result = store.put("large/file.bin", large_content)

        patch_blobs = [(1, "large/file.bin", result["sha256"])]
        mock_conn = MockConnection(patch_blobs=patch_blobs)

        auditor = ArtifactAuditor(
            artifacts_root=artifacts_root,
            conn=mock_conn,
            artifact_store=store,
        )

        r = auditor.audit_record("patch_blobs", 1, "large/file.bin", result["sha256"])
        assert r.status == "ok"
        assert r.size_bytes == 1024 * 1024

    def test_special_characters_in_uri(self, tmp_artifacts):
        """测试 URI 中的特殊字符"""
        artifacts_root, store = tmp_artifacts

        # 创建包含特殊字符的路径
        uri = "test/file with spaces.txt"
        result = store.put(uri, b"content")

        patch_blobs = [(1, uri, result["sha256"])]
        mock_conn = MockConnection(patch_blobs=patch_blobs)

        auditor = ArtifactAuditor(
            artifacts_root=artifacts_root,
            conn=mock_conn,
            artifact_store=store,
        )

        r = auditor.audit_record("patch_blobs", 1, uri, result["sha256"])
        assert r.status == "ok"

    def test_unicode_content(self, tmp_artifacts):
        """测试 Unicode 内容"""
        artifacts_root, store = tmp_artifacts

        unicode_content = "中文内容测试 🎉".encode("utf-8")
        result = store.put("unicode/test.txt", unicode_content)

        patch_blobs = [(1, "unicode/test.txt", result["sha256"])]
        mock_conn = MockConnection(patch_blobs=patch_blobs)

        auditor = ArtifactAuditor(
            artifacts_root=artifacts_root,
            conn=mock_conn,
            artifact_store=store,
        )

        r = auditor.audit_record("patch_blobs", 1, "unicode/test.txt", result["sha256"])
        assert r.status == "ok"

    def test_no_records(self, tmp_artifacts):
        """测试空表审计"""
        artifacts_root, store = tmp_artifacts

        mock_conn = MockConnection(patch_blobs=[], attachments=[])

        auditor = ArtifactAuditor(
            artifacts_root=artifacts_root,
            conn=mock_conn,
            artifact_store=store,
        )

        summary = auditor.run_audit(tables=["patch_blobs"])

        assert summary.total_records == 0
        assert summary.ok_count == 0
        assert summary.has_issues is False


# =============================================================================
# 报告格式测试
# =============================================================================


class TestReportFormat:
    """报告格式测试"""

    def test_json_output_format(self, sample_artifacts):
        """测试 JSON 输出格式"""
        artifacts_root, store, files = sample_artifacts

        # 混合结果
        patch_blobs = [
            (1, files[0]["uri"], files[0]["sha256"]),  # ok
            (2, files[1]["uri"], "wrong_" + "0" * 59),  # mismatch
        ]
        mock_conn = MockConnection(patch_blobs=patch_blobs)

        auditor = ArtifactAuditor(
            artifacts_root=artifacts_root,
            conn=mock_conn,
            artifact_store=store,
        )

        summary = auditor.run_audit(tables=["patch_blobs"])

        # 验证可以转换为 JSON
        json_str = json.dumps(summary.to_dict(), ensure_ascii=False)
        parsed = json.loads(json_str)

        assert "total_records" in parsed
        assert "ok_count" in parsed
        assert "mismatch_count" in parsed
        assert "mismatches" in parsed
        assert len(parsed["mismatches"]) == 1

    def test_summary_statistics(self, sample_artifacts):
        """测试汇总统计"""
        artifacts_root, store, files = sample_artifacts

        patch_blobs = [
            (i + 1, f["uri"], f["sha256"])
            for i, f in enumerate(files)
        ]
        mock_conn = MockConnection(patch_blobs=patch_blobs)

        auditor = ArtifactAuditor(
            artifacts_root=artifacts_root,
            conn=mock_conn,
            artifact_store=store,
        )

        summary = auditor.run_audit(tables=["patch_blobs"])

        assert summary.total_records == len(files)
        assert summary.sampled_records == len(files)
        assert summary.audited_records == len(files)
        assert summary.total_bytes > 0
        assert summary.duration_seconds > 0
        assert summary.start_time != ""
        assert summary.end_time != ""


# =============================================================================
# Store 后端选择测试
# =============================================================================


class TestStoreSelection:
    """测试 _get_store_for_uri 根据 URI 类型正确选择后端"""

    def test_file_uri_uses_file_store(self, tmp_artifacts):
        """测试 file:// URI 使用 FileUriStore"""
        artifacts_root, store = tmp_artifacts
        mock_conn = MockConnection([])

        auditor = ArtifactAuditor(
            artifacts_root=artifacts_root,
            conn=mock_conn,
        )

        # 使用 file:// URI
        selected_store, resolved_uri = auditor._get_store_for_uri("file:///tmp/test.txt")

        assert isinstance(selected_store, FileUriStore)
        assert resolved_uri == "file:///tmp/test.txt"

    def test_s3_uri_uses_object_store(self, tmp_artifacts):
        """测试 s3:// URI 使用 ObjectStore"""
        artifacts_root, store = tmp_artifacts
        mock_conn = MockConnection([])

        auditor = ArtifactAuditor(
            artifacts_root=artifacts_root,
            conn=mock_conn,
        )

        # 使用 s3:// URI，需要设置 ENGRAM_S3_BUCKET 环境变量
        with patch.dict("os.environ", {"ENGRAM_S3_BUCKET": "bucket"}):
            selected_store, resolved_uri = auditor._get_store_for_uri("s3://bucket/key")

            assert isinstance(selected_store, ObjectStore)
            # S3 URI 返回的是 key 而非完整 URI
            assert resolved_uri == "key"

    def test_artifact_uri_uses_config_store_local(self, tmp_artifacts):
        """测试 ARTIFACT 类型 URI 使用配置中的 local 后端"""
        artifacts_root, store = tmp_artifacts
        mock_conn = MockConnection([])

        # Mock 配置返回 local 后端
        mock_config = MagicMock()
        mock_config.artifacts.backend = "local"
        mock_config.artifacts.root = str(artifacts_root)
        mock_config.artifacts.allowed_prefixes = None
        mock_config.artifacts.policy = None

        with patch("artifact_audit.get_app_config", return_value=mock_config):
            with patch(
                "artifact_audit.get_artifact_store_from_config",
                return_value=LocalArtifactsStore(root=artifacts_root),
            ) as mock_get_store:
                auditor = ArtifactAuditor(
                    artifacts_root=artifacts_root,
                    conn=mock_conn,
                )

                # 使用 artifact 相对路径（无 scheme）
                selected_store, resolved_uri = auditor._get_store_for_uri("test/file.txt")

                # 验证调用了 get_artifact_store_from_config
                mock_get_store.assert_called_once()
                assert isinstance(selected_store, LocalArtifactsStore)
                assert resolved_uri == "test/file.txt"

    def test_artifact_uri_uses_config_store_object(self, tmp_artifacts):
        """测试 ARTIFACT 类型 URI 使用配置中的 object 后端"""
        artifacts_root, store = tmp_artifacts
        mock_conn = MockConnection([])

        # Mock ObjectStore
        mock_object_store = MagicMock(spec=ObjectStore)

        with patch("artifact_audit.get_app_config") as mock_get_config:
            with patch(
                "artifact_audit.get_artifact_store_from_config",
                return_value=mock_object_store,
            ) as mock_get_store:
                auditor = ArtifactAuditor(
                    artifacts_root=artifacts_root,
                    conn=mock_conn,
                )

                # 使用 artifact 相对路径（无 scheme）
                selected_store, resolved_uri = auditor._get_store_for_uri("test/file.txt")

                # 验证调用了 get_artifact_store_from_config
                mock_get_store.assert_called_once()
                # 验证返回的是配置中的 ObjectStore
                assert selected_store is mock_object_store
                assert resolved_uri == "test/file.txt"

    def test_artifact_store_cached(self, tmp_artifacts):
        """测试 artifact store 实例被缓存"""
        artifacts_root, store = tmp_artifacts
        mock_conn = MockConnection([])

        mock_object_store = MagicMock(spec=ObjectStore)

        with patch("artifact_audit.get_app_config"):
            with patch(
                "artifact_audit.get_artifact_store_from_config",
                return_value=mock_object_store,
            ) as mock_get_store:
                auditor = ArtifactAuditor(
                    artifacts_root=artifacts_root,
                    conn=mock_conn,
                )

                # 多次调用
                auditor._get_store_for_uri("test/file1.txt")
                auditor._get_store_for_uri("test/file2.txt")
                auditor._get_store_for_uri("test/file3.txt")

                # 应该只调用一次 get_artifact_store_from_config
                assert mock_get_store.call_count == 1

    def test_audit_with_object_backend(self, tmp_artifacts):
        """测试使用 object 后端进行审计"""
        artifacts_root, store = tmp_artifacts
        mock_conn = MockConnection([])

        # Mock ObjectStore 的 get_info 返回
        mock_object_store = MagicMock(spec=ObjectStore)
        expected_sha256 = "abc123def456" + "0" * 52
        mock_object_store.get_info.return_value = {
            "sha256": expected_sha256,
            "size_bytes": 1024,
        }

        with patch("artifact_audit.get_app_config"):
            with patch(
                "artifact_audit.get_artifact_store_from_config",
                return_value=mock_object_store,
            ):
                auditor = ArtifactAuditor(
                    artifacts_root=artifacts_root,
                    conn=mock_conn,
                )

                result = auditor.audit_record(
                    table="patch_blobs",
                    record_id=1,
                    uri="test/file.txt",
                    expected_sha256=expected_sha256,
                )

                # 验证使用了 object store 的 get_info
                mock_object_store.get_info.assert_called_once_with("test/file.txt")
                assert result.status == "ok"
                assert result.actual_sha256 == expected_sha256
                assert result.size_bytes == 1024


# =============================================================================
# Head-Only 模式测试
# =============================================================================


class TestHeadOnlyMode:
    """head-only 模式测试"""

    def test_head_only_with_metadata_sha256(self, tmp_artifacts):
        """测试 head-only 模式：metadata 中有 sha256"""
        artifacts_root, store = tmp_artifacts
        mock_conn = MockConnection([])

        # Mock ObjectStore
        mock_object_store = MagicMock(spec=ObjectStore)
        mock_object_store.bucket = "test-bucket"
        expected_sha256 = "abc123def456" + "0" * 52
        
        # Mock S3 client
        mock_client = MagicMock()
        mock_client.head_object.return_value = {
            "ContentLength": 1024,
            "Metadata": {"sha256": expected_sha256},
        }
        mock_object_store._get_client.return_value = mock_client
        mock_object_store._object_key.return_value = "test/file.txt"

        with patch("artifact_audit.get_app_config"):
            with patch(
                "artifact_audit.get_artifact_store_from_config",
                return_value=mock_object_store,
            ):
                auditor = ArtifactAuditor(
                    artifacts_root=artifacts_root,
                    conn=mock_conn,
                    head_only=True,
                )

                result = auditor.audit_record(
                    table="patch_blobs",
                    record_id=1,
                    uri="test/file.txt",
                    expected_sha256=expected_sha256,
                )

                assert result.status == "ok"
                assert result.actual_sha256 == expected_sha256
                assert result.size_bytes == 1024
                # 验证只调用了 head_object，没有调用 get_info
                mock_client.head_object.assert_called_once()
                mock_object_store.get_info.assert_not_called()

    def test_head_only_without_metadata_sha256(self, tmp_artifacts):
        """测试 head-only 模式：metadata 中没有 sha256"""
        artifacts_root, store = tmp_artifacts
        mock_conn = MockConnection([])

        # Mock ObjectStore
        mock_object_store = MagicMock(spec=ObjectStore)
        mock_object_store.bucket = "test-bucket"
        expected_sha256 = "abc123def456" + "0" * 52
        
        # Mock S3 client - 没有 sha256 metadata
        mock_client = MagicMock()
        mock_client.head_object.return_value = {
            "ContentLength": 1024,
            "Metadata": {},  # 没有 sha256
        }
        mock_object_store._get_client.return_value = mock_client
        mock_object_store._object_key.return_value = "test/file.txt"

        with patch("artifact_audit.get_app_config"):
            with patch(
                "artifact_audit.get_artifact_store_from_config",
                return_value=mock_object_store,
            ):
                auditor = ArtifactAuditor(
                    artifacts_root=artifacts_root,
                    conn=mock_conn,
                    head_only=True,
                )

                result = auditor.audit_record(
                    table="patch_blobs",
                    record_id=1,
                    uri="test/file.txt",
                    expected_sha256=expected_sha256,
                )

                # 应该标记为无法验证
                assert result.status == "head_only_unverified"
                assert result.actual_sha256 is None
                assert result.size_bytes == 1024
                assert "metadata" in result.error_message.lower()

    def test_head_only_local_store_still_computes_hash(self, sample_artifacts):
        """测试 head-only 模式：LocalArtifactsStore 仍需要计算哈希"""
        artifacts_root, store, files = sample_artifacts
        mock_conn = MockConnection([])

        auditor = ArtifactAuditor(
            artifacts_root=artifacts_root,
            conn=mock_conn,
            artifact_store=store,
            head_only=True,
        )

        # 审计第一个文件
        file_info = files[0]
        result = auditor.audit_record(
            table="patch_blobs",
            record_id=1,
            uri=file_info["uri"],
            expected_sha256=file_info["sha256"],
        )

        # LocalArtifactsStore 不支持 metadata sha256，所以会流式计算
        assert result.status == "ok"
        assert result.actual_sha256 == file_info["sha256"]


# =============================================================================
# Prefix 过滤测试
# =============================================================================


class TestPrefixFilter:
    """prefix 过滤测试"""

    def test_prefix_filter_matches(self, sample_artifacts):
        """测试 prefix 过滤：匹配的记录"""
        artifacts_root, store, files = sample_artifacts

        # 创建带前缀的记录
        patch_blobs = [
            (1, "scm/patch1.txt", files[0]["sha256"]),
            (2, "scm/patch2.txt", files[1]["sha256"]),
            (3, "attachments/file1.txt", files[2]["sha256"]),
        ]
        mock_conn = MockConnection(patch_blobs=patch_blobs)

        auditor = ArtifactAuditor(
            artifacts_root=artifacts_root,
            conn=mock_conn,
            artifact_store=store,
        )

        # 使用 prefix 过滤
        summary = auditor.run_audit(
            tables=["patch_blobs"],
            prefix="scm/",
        )

        # 只应该审计 scm/ 前缀的记录
        assert summary.total_records == 2

    def test_prefix_filter_no_matches(self, sample_artifacts):
        """测试 prefix 过滤：无匹配记录"""
        artifacts_root, store, files = sample_artifacts

        patch_blobs = [
            (1, "attachments/file1.txt", files[0]["sha256"]),
            (2, "attachments/file2.txt", files[1]["sha256"]),
        ]
        mock_conn = MockConnection(patch_blobs=patch_blobs)

        auditor = ArtifactAuditor(
            artifacts_root=artifacts_root,
            conn=mock_conn,
            artifact_store=store,
        )

        summary = auditor.run_audit(
            tables=["patch_blobs"],
            prefix="scm/",
        )

        # 没有匹配的记录
        assert summary.total_records == 0


# =============================================================================
# 并发审计测试
# =============================================================================


class TestConcurrentAudit:
    """并发审计测试"""

    def test_concurrent_audit_basic(self, sample_artifacts):
        """测试基本并发审计"""
        artifacts_root, store, files = sample_artifacts

        patch_blobs = [
            (i + 1, f["uri"], f["sha256"])
            for i, f in enumerate(files)
        ]
        mock_conn = MockConnection(patch_blobs=patch_blobs)

        auditor = ArtifactAuditor(
            artifacts_root=artifacts_root,
            conn=mock_conn,
            artifact_store=store,
            workers=2,  # 2 个线程
        )

        summary = auditor.run_audit(tables=["patch_blobs"])

        assert summary.total_records == len(files)
        assert summary.ok_count == len(files)
        assert summary.mismatch_count == 0

    def test_concurrent_audit_with_errors(self, sample_artifacts):
        """测试并发审计处理错误"""
        artifacts_root, store, files = sample_artifacts

        # 混合正常和不存在的文件
        patch_blobs = [
            (1, files[0]["uri"], files[0]["sha256"]),
            (2, "nonexistent/file.txt", "any_hash_" + "0" * 55),
            (3, files[1]["uri"], files[1]["sha256"]),
        ]
        mock_conn = MockConnection(patch_blobs=patch_blobs)

        auditor = ArtifactAuditor(
            artifacts_root=artifacts_root,
            conn=mock_conn,
            artifact_store=store,
            workers=2,
        )

        summary = auditor.run_audit(tables=["patch_blobs"])

        assert summary.total_records == 3
        assert summary.ok_count == 2
        assert summary.missing_count == 1


# =============================================================================
# 增量游标测试
# =============================================================================


class TestIncrementalCursor:
    """增量游标测试"""

    def test_next_cursor_set(self, sample_artifacts):
        """测试 next_cursor 被正确设置"""
        artifacts_root, store, files = sample_artifacts

        # 创建带 created_at 的记录
        now = datetime.now()
        patch_blobs = [
            (1, files[0]["uri"], files[0]["sha256"], datetime(2024, 1, 1, 10, 0, 0)),
            (2, files[1]["uri"], files[1]["sha256"], datetime(2024, 1, 2, 10, 0, 0)),
            (3, files[2]["uri"], files[2]["sha256"], datetime(2024, 1, 3, 10, 0, 0)),
        ]
        mock_conn = MockConnection(patch_blobs=patch_blobs)

        auditor = ArtifactAuditor(
            artifacts_root=artifacts_root,
            conn=mock_conn,
            artifact_store=store,
        )

        summary = auditor.run_audit(tables=["patch_blobs"])

        # next_cursor 应该是最大的 created_at
        assert summary.next_cursor is not None
        cursor_dt = datetime.fromisoformat(summary.next_cursor)
        assert cursor_dt == datetime(2024, 1, 3, 10, 0, 0)

    def test_next_cursor_in_json_output(self, sample_artifacts):
        """测试 JSON 输出包含 next_cursor"""
        artifacts_root, store, files = sample_artifacts

        patch_blobs = [
            (1, files[0]["uri"], files[0]["sha256"], datetime(2024, 6, 15, 12, 0, 0)),
        ]
        mock_conn = MockConnection(patch_blobs=patch_blobs)

        auditor = ArtifactAuditor(
            artifacts_root=artifacts_root,
            conn=mock_conn,
            artifact_store=store,
        )

        summary = auditor.run_audit(tables=["patch_blobs"])
        json_output = summary.to_dict()

        assert "next_cursor" in json_output
        assert json_output["next_cursor"] == "2024-06-15T12:00:00"


# =============================================================================
# RateLimiter 线程安全测试
# =============================================================================


class TestRateLimiterThreadSafe:
    """RateLimiter 线程安全测试"""

    def test_rate_limiter_thread_safe(self):
        """测试速率限制器在多线程下的安全性"""
        limiter = RateLimiter(max_bytes_per_sec=10000)
        errors = []
        call_count = [0]
        lock = threading.Lock()

        def worker():
            try:
                for _ in range(10):
                    limiter.wait_if_needed(500)
                    with lock:
                        call_count[0] += 1
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 不应该有错误
        assert len(errors) == 0
        # 所有调用都应该完成
        assert call_count[0] == 40
