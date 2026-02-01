#!/usr/bin/env python3
"""iteration_evidence_naming helper 单元测试。

测试覆盖:
    - canonical_evidence_filename: 固定格式文件名生成
    - canonical_evidence_path: 完整路径生成
    - snapshot_evidence_filename: 带时间戳/SHA 的快照文件名生成
    - snapshot_evidence_path: 快照完整路径生成
    - parse_evidence_filename: 文件名解析
    - relative_evidence_path: 相对路径生成
    - 边界条件: 无效迭代编号、无效 SHA、无效文件名格式
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from scripts.iteration.iteration_evidence_naming import (
    EVIDENCE_DIR,
    canonical_evidence_filename,
    canonical_evidence_path,
    parse_evidence_filename,
    relative_evidence_path,
    snapshot_evidence_filename,
    snapshot_evidence_path,
)


class TestCanonicalEvidenceFilename:
    """canonical_evidence_filename 测试。"""

    def test_basic_iteration_number(self) -> None:
        """基本迭代编号生成正确文件名。"""
        assert canonical_evidence_filename(13) == "iteration_13_evidence.json"
        assert canonical_evidence_filename(1) == "iteration_1_evidence.json"
        assert canonical_evidence_filename(100) == "iteration_100_evidence.json"

    def test_invalid_iteration_number_zero(self) -> None:
        """迭代编号为 0 时抛出异常。"""
        with pytest.raises(ValueError, match="迭代编号必须为正整数"):
            canonical_evidence_filename(0)

    def test_invalid_iteration_number_negative(self) -> None:
        """迭代编号为负数时抛出异常。"""
        with pytest.raises(ValueError, match="迭代编号必须为正整数"):
            canonical_evidence_filename(-1)

    def test_invalid_iteration_number_type(self) -> None:
        """迭代编号类型无效时抛出异常。"""
        with pytest.raises(ValueError, match="迭代编号必须为正整数"):
            canonical_evidence_filename("13")  # type: ignore[arg-type]


class TestCanonicalEvidencePath:
    """canonical_evidence_path 测试。"""

    def test_returns_path_object(self) -> None:
        """返回 Path 对象。"""
        result = canonical_evidence_path(13)
        assert isinstance(result, Path)

    def test_path_ends_with_correct_filename(self) -> None:
        """路径以正确的文件名结尾。"""
        result = canonical_evidence_path(13)
        assert result.name == "iteration_13_evidence.json"

    def test_path_parent_is_evidence_dir(self) -> None:
        """路径父目录是 EVIDENCE_DIR。"""
        result = canonical_evidence_path(13)
        assert result.parent == EVIDENCE_DIR

    def test_path_string_format(self) -> None:
        """路径字符串格式正确。"""
        result = canonical_evidence_path(13)
        assert str(result).endswith("docs/acceptance/evidence/iteration_13_evidence.json")


class TestSnapshotEvidenceFilename:
    """snapshot_evidence_filename 测试。"""

    def test_with_timestamp_only(self) -> None:
        """仅带时间戳的文件名。"""
        ts = datetime(2026, 2, 1, 10, 30, 0)
        result = snapshot_evidence_filename(13, timestamp=ts)
        assert result == "iteration_13_20260201_103000.json"

    def test_with_timestamp_and_sha(self) -> None:
        """带时间戳和 SHA 的文件名。"""
        ts = datetime(2026, 2, 1, 10, 30, 0)
        result = snapshot_evidence_filename(13, timestamp=ts, commit_sha="abc1234def5678")
        assert result == "iteration_13_20260201_103000_abc1234.json"

    def test_sha_truncated_to_7_chars(self) -> None:
        """SHA 截取前 7 位。"""
        ts = datetime(2026, 2, 1, 10, 30, 0)
        # 40 位完整 SHA
        full_sha = "abc1234def5678901234567890abcdef12345678"
        result = snapshot_evidence_filename(13, timestamp=ts, commit_sha=full_sha)
        assert "_abc1234." in result
        assert "_abc1234def" not in result

    def test_sha_exactly_7_chars(self) -> None:
        """7 位短 SHA 直接使用。"""
        ts = datetime(2026, 2, 1, 10, 30, 0)
        result = snapshot_evidence_filename(13, timestamp=ts, commit_sha="abc1234")
        assert result == "iteration_13_20260201_103000_abc1234.json"

    def test_default_timestamp_is_now(self) -> None:
        """默认时间戳为当前时间。"""
        result = snapshot_evidence_filename(13)
        # 验证格式正确（不验证具体时间）
        assert result.startswith("iteration_13_")
        assert result.endswith(".json")
        # 时间戳部分为 15 字符: YYYYMMDD_HHMMSS
        parts = result.replace("iteration_13_", "").replace(".json", "")
        assert len(parts) == 15
        assert "_" in parts

    def test_invalid_sha_format(self) -> None:
        """无效 SHA 格式抛出异常。"""
        ts = datetime(2026, 2, 1, 10, 30, 0)

        # 包含非十六进制字符
        with pytest.raises(ValueError, match="commit SHA 必须是"):
            snapshot_evidence_filename(13, timestamp=ts, commit_sha="xyz1234")

        # 大写字母（schema 要求小写）
        with pytest.raises(ValueError, match="commit SHA 必须是"):
            snapshot_evidence_filename(13, timestamp=ts, commit_sha="ABC1234")

        # 太短（少于 7 位）
        with pytest.raises(ValueError, match="commit SHA 必须是"):
            snapshot_evidence_filename(13, timestamp=ts, commit_sha="abc123")

    def test_invalid_iteration_number(self) -> None:
        """无效迭代编号抛出异常。"""
        ts = datetime(2026, 2, 1, 10, 30, 0)
        with pytest.raises(ValueError, match="迭代编号必须为正整数"):
            snapshot_evidence_filename(0, timestamp=ts)


class TestSnapshotEvidencePath:
    """snapshot_evidence_path 测试。"""

    def test_returns_path_object(self) -> None:
        """返回 Path 对象。"""
        ts = datetime(2026, 2, 1, 10, 30, 0)
        result = snapshot_evidence_path(13, timestamp=ts)
        assert isinstance(result, Path)

    def test_path_parent_is_evidence_dir(self) -> None:
        """路径父目录是 EVIDENCE_DIR。"""
        ts = datetime(2026, 2, 1, 10, 30, 0)
        result = snapshot_evidence_path(13, timestamp=ts)
        assert result.parent == EVIDENCE_DIR


class TestParseEvidenceFilename:
    """parse_evidence_filename 测试。"""

    def test_parse_canonical_filename(self) -> None:
        """解析 canonical 文件名。"""
        result = parse_evidence_filename("iteration_13_evidence.json")
        assert result == {
            "iteration_number": 13,
            "is_canonical": True,
            "timestamp": None,
            "commit_sha": None,
        }

    def test_parse_snapshot_without_sha(self) -> None:
        """解析无 SHA 的 snapshot 文件名。"""
        result = parse_evidence_filename("iteration_13_20260201_103000.json")
        assert result == {
            "iteration_number": 13,
            "is_canonical": False,
            "timestamp": "20260201_103000",
            "commit_sha": None,
        }

    def test_parse_snapshot_with_sha(self) -> None:
        """解析带 SHA 的 snapshot 文件名。"""
        result = parse_evidence_filename("iteration_13_20260201_103000_abc1234.json")
        assert result == {
            "iteration_number": 13,
            "is_canonical": False,
            "timestamp": "20260201_103000",
            "commit_sha": "abc1234",
        }

    def test_parse_large_iteration_number(self) -> None:
        """解析大迭代编号。"""
        result = parse_evidence_filename("iteration_999_evidence.json")
        assert result["iteration_number"] == 999

    def test_invalid_filename_format(self) -> None:
        """无效文件名格式抛出异常。"""
        with pytest.raises(ValueError, match="无效的证据文件名格式"):
            parse_evidence_filename("evidence.json")

        with pytest.raises(ValueError, match="无效的证据文件名格式"):
            parse_evidence_filename("iteration_evidence.json")

        with pytest.raises(ValueError, match="无效的证据文件名格式"):
            parse_evidence_filename("iteration_13.json")

        with pytest.raises(ValueError, match="无效的证据文件名格式"):
            parse_evidence_filename("iteration_abc_evidence.json")


class TestRelativeEvidencePath:
    """relative_evidence_path 测试。"""

    def test_from_regression_doc(self) -> None:
        """从 regression 文档引用时包含 evidence/ 前缀。"""
        result = relative_evidence_path(13)
        assert result == "evidence/iteration_13_evidence.json"

    def test_not_from_regression_doc(self) -> None:
        """非 regression 文档引用时不包含前缀。"""
        result = relative_evidence_path(13, from_regression_doc=False)
        assert result == "iteration_13_evidence.json"


class TestRoundTrip:
    """文件名生成与解析往返测试。"""

    def test_canonical_roundtrip(self) -> None:
        """canonical 文件名往返一致。"""
        original = 13
        filename = canonical_evidence_filename(original)
        parsed = parse_evidence_filename(filename)
        assert parsed["iteration_number"] == original
        assert parsed["is_canonical"] is True

    def test_snapshot_roundtrip_without_sha(self) -> None:
        """snapshot 文件名（无 SHA）往返一致。"""
        original = 13
        ts = datetime(2026, 2, 1, 10, 30, 0)
        filename = snapshot_evidence_filename(original, timestamp=ts)
        parsed = parse_evidence_filename(filename)
        assert parsed["iteration_number"] == original
        assert parsed["is_canonical"] is False
        assert parsed["timestamp"] == "20260201_103000"

    def test_snapshot_roundtrip_with_sha(self) -> None:
        """snapshot 文件名（带 SHA）往返一致。"""
        original = 13
        ts = datetime(2026, 2, 1, 10, 30, 0)
        sha = "abc1234def5678"
        filename = snapshot_evidence_filename(original, timestamp=ts, commit_sha=sha)
        parsed = parse_evidence_filename(filename)
        assert parsed["iteration_number"] == original
        assert parsed["is_canonical"] is False
        assert parsed["timestamp"] == "20260201_103000"
        assert parsed["commit_sha"] == "abc1234"
