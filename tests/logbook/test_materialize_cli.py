# -*- coding: utf-8 -*-
"""
test_materialize_cli.py - scm_materialize_patch_blob CLI 单测

测试覆盖:
1. CLI 参数解析验证（--attachment-id, --kind, --materialize-missing）
2. 输出结构快照验证
3. 参数组合验证
"""

import json
from unittest.mock import MagicMock, patch

from engram.logbook.materialize_patch_blob import (
    MaterializeResult,
    MaterializeStatus,
    PatchBlobRecord,
    get_blobs_by_attachment_id,
    get_blobs_by_attachment_kind,
    parse_args,
)


class TestCliArgumentParsing:
    """CLI 参数解析测试"""

    def test_parse_args_blob_id(self):
        """测试 --blob-id 参数解析"""
        with patch("sys.argv", ["prog", "--blob-id", "123"]):
            args = parse_args()
            assert args.blob_id == 123
            assert args.attachment_id is None
            assert args.kind is None

    def test_parse_args_attachment_id(self):
        """测试 --attachment-id 参数解析"""
        with patch("sys.argv", ["prog", "--attachment-id", "456"]):
            args = parse_args()
            assert args.attachment_id == 456
            assert args.blob_id is None

    def test_parse_args_kind_with_materialize_missing(self):
        """测试 --kind 和 --materialize-missing 参数组合"""
        with patch("sys.argv", ["prog", "--kind", "patch", "--materialize-missing"]):
            args = parse_args()
            assert args.kind == "patch"
            assert args.materialize_missing is True

    def test_parse_args_source_type(self):
        """测试 --source-type 参数解析"""
        with patch("sys.argv", ["prog", "--source-type", "git"]):
            args = parse_args()
            assert args.source_type == "git"

        with patch("sys.argv", ["prog", "--source-type", "svn"]):
            args = parse_args()
            assert args.source_type == "svn"

    def test_parse_args_retry_failed(self):
        """测试 --retry-failed 参数解析"""
        with patch("sys.argv", ["prog", "--retry-failed"]):
            args = parse_args()
            assert args.retry_failed is True

    def test_parse_args_batch_size(self):
        """测试 --batch-size 参数解析"""
        with patch("sys.argv", ["prog", "--batch-size", "100"]):
            args = parse_args()
            assert args.batch_size == 100

    def test_parse_args_verbose_and_quiet(self):
        """测试 --verbose 和 --quiet 参数"""
        with patch("sys.argv", ["prog", "--verbose"]):
            args = parse_args()
            assert args.verbose is True
            assert args.quiet is False

        with patch("sys.argv", ["prog", "--quiet"]):
            args = parse_args()
            assert args.quiet is True
            assert args.verbose is False

    def test_parse_args_json_output(self):
        """测试 --json 输出格式参数"""
        with patch("sys.argv", ["prog", "--json"]):
            args = parse_args()
            assert args.json is True

    def test_parse_args_combined(self):
        """测试多个参数组合"""
        with patch(
            "sys.argv",
            [
                "prog",
                "--kind",
                "patch",
                "--materialize-missing",
                "--batch-size",
                "50",
                "--json",
                "--verbose",
            ],
        ):
            args = parse_args()
            assert args.kind == "patch"
            assert args.materialize_missing is True
            assert args.batch_size == 50
            assert args.json is True
            assert args.verbose is True


class TestOutputStructure:
    """输出结构快照验证测试"""

    def test_materialize_result_structure(self):
        """测试 MaterializeResult 数据结构"""
        result = MaterializeResult(
            blob_id=123,
            status=MaterializeStatus.MATERIALIZED,
            uri="scm/1/git/commits/abc123.diff",
            sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            size_bytes=1024,
        )

        assert result.blob_id == 123
        assert result.status == MaterializeStatus.MATERIALIZED
        assert result.uri is not None
        assert result.sha256 is not None
        assert result.size_bytes == 1024
        assert result.error is None

    def test_materialize_result_failed_structure(self):
        """测试失败情况的 MaterializeResult 结构"""
        result = MaterializeResult(
            blob_id=456,
            status=MaterializeStatus.FAILED,
            error="连接超时",
        )

        assert result.blob_id == 456
        assert result.status == MaterializeStatus.FAILED
        assert result.uri is None
        assert result.error == "连接超时"

    def test_materialize_result_skipped_structure(self):
        """测试跳过情况的 MaterializeResult 结构"""
        result = MaterializeResult(
            blob_id=789,
            status=MaterializeStatus.SKIPPED,
            uri="scm/1/git/commits/existing.diff",
        )

        assert result.blob_id == 789
        assert result.status == MaterializeStatus.SKIPPED
        assert result.uri is not None

    def test_batch_result_structure(self):
        """测试批量处理结果结构快照"""
        # 模拟批量处理结果
        result = {
            "success": True,
            "total": 3,
            "materialized": 1,
            "skipped": 1,
            "failed": 1,
            "details": [
                {
                    "blob_id": 1,
                    "status": "materialized",
                    "uri": "scm/1/git/commits/a.diff",
                    "sha256": "abc123",
                    "size_bytes": 100,
                    "error": None,
                },
                {
                    "blob_id": 2,
                    "status": "skipped",
                    "uri": "scm/1/git/commits/b.diff",
                    "sha256": None,
                    "size_bytes": None,
                    "error": None,
                },
                {
                    "blob_id": 3,
                    "status": "failed",
                    "uri": None,
                    "sha256": None,
                    "size_bytes": None,
                    "error": "获取失败",
                },
            ],
        }

        # 验证顶层结构
        assert "success" in result
        assert "total" in result
        assert "materialized" in result
        assert "skipped" in result
        assert "failed" in result
        assert "details" in result

        # 验证数值一致性
        assert result["total"] == result["materialized"] + result["skipped"] + result["failed"]

        # 验证 details 结构
        for detail in result["details"]:
            assert "blob_id" in detail
            assert "status" in detail
            assert detail["status"] in [
                "materialized",
                "skipped",
                "failed",
                "pending",
                "unreachable",
            ]


class TestPatchBlobRecordStructure:
    """PatchBlobRecord 数据结构测试"""

    def test_patch_blob_record_fields(self):
        """测试 PatchBlobRecord 字段完整性"""
        record = PatchBlobRecord(
            blob_id=123,
            source_type="git",
            source_id="1:abc123def",
            uri="scm/1/git/commits/abc123def.diff",
            sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            size_bytes=1024,
            format="diff",
            meta_json={"materialize_status": "done"},
        )

        assert record.blob_id == 123
        assert record.source_type == "git"
        assert record.source_id == "1:abc123def"
        assert record.uri is not None
        assert record.sha256 is not None
        assert record.size_bytes == 1024
        assert record.format == "diff"
        assert record.meta_json is not None
        assert record.meta_json.get("materialize_status") == "done"

    def test_patch_blob_record_nullable_fields(self):
        """测试 PatchBlobRecord 可空字段"""
        record = PatchBlobRecord(
            blob_id=456,
            source_type="svn",
            source_id="2:1234",
            uri=None,  # 待物化
            sha256="abc123",
            size_bytes=None,
            format="diff",
        )

        assert record.uri is None
        assert record.size_bytes is None
        assert record.meta_json is None


class TestMaterializeStatusEnum:
    """MaterializeStatus 枚举测试"""

    def test_all_status_values(self):
        """测试所有状态枚举值"""
        expected_statuses = ["pending", "materialized", "failed", "skipped", "unreachable"]
        actual_statuses = [s.value for s in MaterializeStatus]

        for expected in expected_statuses:
            assert expected in actual_statuses, f"缺少状态: {expected}"

    def test_status_serialization(self):
        """测试状态值序列化"""
        assert MaterializeStatus.PENDING.value == "pending"
        assert MaterializeStatus.MATERIALIZED.value == "materialized"
        assert MaterializeStatus.FAILED.value == "failed"
        assert MaterializeStatus.SKIPPED.value == "skipped"
        assert MaterializeStatus.UNREACHABLE.value == "unreachable"


class TestGetBlobsFunctions:
    """get_blobs_by_* 函数测试（使用 mock）"""

    def test_get_blobs_by_attachment_id_not_found(self):
        """测试 attachment 不存在的情况"""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_cursor.fetchone.return_value = None  # attachment 不存在

        results = get_blobs_by_attachment_id(mock_conn, 999)

        assert results == []

    def test_get_blobs_by_attachment_id_sha256_match(self):
        """测试通过 sha256 匹配 blob"""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        # 第一次调用返回 attachment
        # 第二次调用返回关联的 blob
        mock_cursor.fetchone.side_effect = [
            {
                "attachment_id": 1,
                "item_id": 100,
                "kind": "patch",
                "uri": "scm/1/git/commits/abc.diff",
                "sha256": "abc123",
                "size_bytes": 1024,
                "meta_json": None,
            }
        ]
        mock_cursor.fetchall.return_value = [
            {
                "blob_id": 1,
                "source_type": "git",
                "source_id": "1:abc",
                "uri": "scm/1/git/commits/abc.diff",
                "sha256": "abc123",
                "size_bytes": 1024,
                "format": "diff",
                "meta_json": {},
            }
        ]

        results = get_blobs_by_attachment_id(mock_conn, 1)

        assert len(results) == 1
        assert results[0]["blob_id"] == 1
        assert results[0]["sha256"] == "abc123"

    def test_get_blobs_by_attachment_kind_empty(self):
        """测试没有匹配的 kind"""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_cursor.fetchall.return_value = []

        results = get_blobs_by_attachment_kind(mock_conn, "unknown_kind")

        assert results == []


class TestJsonOutputFormat:
    """JSON 输出格式验证"""

    def test_json_output_success(self):
        """测试成功情况的 JSON 输出格式"""
        output = {
            "success": True,
            "total": 2,
            "materialized": 2,
            "skipped": 0,
            "failed": 0,
        }

        # 验证可序列化
        json_str = json.dumps(output, ensure_ascii=False)
        parsed = json.loads(json_str)

        assert parsed["success"] is True
        assert parsed["total"] == 2

    def test_json_output_with_details(self):
        """测试包含 details 的 JSON 输出格式"""
        output = {
            "success": True,
            "total": 1,
            "materialized": 1,
            "skipped": 0,
            "failed": 0,
            "details": [
                {
                    "blob_id": 1,
                    "status": "materialized",
                    "uri": "scm/1/git/commits/abc.diff",
                    "sha256": "abc123def456",
                    "size_bytes": 1024,
                    "error": None,
                }
            ],
        }

        json_str = json.dumps(output, ensure_ascii=False)
        parsed = json.loads(json_str)

        assert len(parsed["details"]) == 1
        assert parsed["details"][0]["blob_id"] == 1

    def test_json_output_error(self):
        """测试错误情况的 JSON 输出格式"""
        output = {
            "error": True,
            "type": "MATERIALIZE_ERROR",
            "message": "物化失败: 连接超时",
        }

        json_str = json.dumps(output, ensure_ascii=False)
        parsed = json.loads(json_str)

        assert parsed["error"] is True
        assert "message" in parsed
