# -*- coding: utf-8 -*-
"""
Audit Event Schema 契约测试

测试覆盖:
1. audit_event 返回结构符合 JSON Schema
2. evidence_refs_json 返回结构符合 JSON Schema
3. 字段级校验：必需字段、格式校验、枚举值
4. schema 中的 examples 有效性校验
5. object_store_audit_event_v1 归一化结构校验
"""

import json
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
from jsonschema import ValidationError, validate


# Schema 文件路径计算
def _find_schema_path(schema_name: str = "audit_event_v1.schema.json") -> Path:
    """查找 schema 文件路径，支持多种执行上下文"""
    current = Path(__file__).resolve().parent

    for _ in range(10):
        candidate = current / "schemas" / schema_name
        if candidate.exists():
            return candidate
        current = current.parent

    fallback = Path(__file__).resolve().parent.parent.parent.parent.parent / "schemas" / schema_name
    return fallback


SCHEMA_PATH = _find_schema_path()
OBJECT_STORE_SCHEMA_PATH = _find_schema_path("object_store_audit_event_v1.schema.json")


def load_schema() -> Dict[str, Any]:
    """加载 audit_event_v1 schema"""
    if not SCHEMA_PATH.exists():
        pytest.skip(f"Schema 文件不存在: {SCHEMA_PATH}")

    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def create_mock_audit_event() -> Dict[str, Any]:
    """创建一个符合规范的 mock audit_event"""
    return {
        "schema_version": "1.1",
        "source": "gateway",
        "operation": "memory_store",
        "correlation_id": "corr-a1b2c3d4e5f67890",
        "actor_user_id": "user1",
        "requested_space": "team:project",
        "final_space": "team:project",
        "decision": {"action": "allow", "reason": "policy_passed"},
        "payload_sha": "abc123def456789012345678901234567890123456789012345678901234",
        "payload_len": 1024,
        "evidence_summary": {
            "count": 2,
            "has_strong": True,
            "uris": ["memory://patch_blobs/git/1:abc/sha256hash"],
        },
        "trim": {"was_trimmed": False, "why": None, "original_len": None},
        "refs": ["memory://patch_blobs/git/1:abc/sha256hash"],
        "event_ts": "2024-01-15T10:30:00.000000+00:00",
    }


def create_mock_evidence_refs_json() -> Dict[str, Any]:
    """创建一个符合规范的 mock evidence_refs_json"""
    return {
        "gateway_event": create_mock_audit_event(),
        "patches": [
            {
                "artifact_uri": "memory://patch_blobs/git/1:abc123/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "source_type": "git",
                "source_id": "abc123",
            }
        ],
        "attachments": [],
        "external": [{"uri": "https://example.com/doc"}],
        "evidence_summary": {
            "count": 2,
            "has_strong": True,
            "uris": [
                "memory://patch_blobs/git/1:abc123/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "https://example.com/doc",
            ],
        },
    }


class TestAuditEventSchema:
    """测试 audit_event schema 校验"""

    @pytest.fixture(scope="class")
    def schema(self):
        """加载 schema"""
        return load_schema()

    def test_valid_audit_event_passes_schema(self, schema):
        """完整的有效 audit_event 应通过 schema 校验"""
        event = create_mock_audit_event()
        validate(instance=event, schema=schema)

    def test_required_fields_present(self, schema):
        """验证所有必需字段存在"""
        event = create_mock_audit_event()

        required_fields = [
            "schema_version",
            "source",
            "operation",
            "correlation_id",
            "decision",
            "evidence_summary",
            "trim",
            "refs",
            "event_ts",
        ]

        for field in required_fields:
            assert field in event, f"缺少必需字段: {field}"

    def test_missing_schema_version_fails(self, schema):
        """缺少 schema_version 应失败"""
        event = create_mock_audit_event()
        del event["schema_version"]

        with pytest.raises(ValidationError) as exc_info:
            validate(instance=event, schema=schema)

        assert "schema_version" in str(exc_info.value)

    def test_missing_correlation_id_fails(self, schema):
        """缺少 correlation_id 应失败"""
        event = create_mock_audit_event()
        del event["correlation_id"]

        with pytest.raises(ValidationError) as exc_info:
            validate(instance=event, schema=schema)

        assert "correlation_id" in str(exc_info.value)

    def test_invalid_correlation_id_format_fails(self, schema):
        """无效的 correlation_id 格式应失败"""
        event = create_mock_audit_event()
        event["correlation_id"] = "invalid-format"

        with pytest.raises(ValidationError):
            validate(instance=event, schema=schema)

    def test_valid_correlation_id_formats(self, schema):
        """有效的 correlation_id 格式应通过"""
        event = create_mock_audit_event()

        valid_ids = [
            "corr-a1b2c3d4e5f67890",
            "corr-ABCDEF1234567890",
            "corr-0000000000000000",
        ]

        for corr_id in valid_ids:
            event["correlation_id"] = corr_id
            validate(instance=event, schema=schema)


class TestDecisionSubstructure:
    """测试 decision 子结构 schema"""

    @pytest.fixture(scope="class")
    def schema(self):
        return load_schema()

    def test_valid_decision_actions(self, schema):
        """有效的 decision action 值应通过"""
        event = create_mock_audit_event()

        for action in ["allow", "redirect", "reject", None]:
            event["decision"]["action"] = action
            validate(instance=event, schema=schema)

    def test_invalid_decision_action_fails(self, schema):
        """无效的 decision action 值应失败"""
        event = create_mock_audit_event()
        event["decision"]["action"] = "invalid_action"

        with pytest.raises(ValidationError):
            validate(instance=event, schema=schema)


class TestEvidenceSummarySubstructure:
    """测试 evidence_summary 子结构 schema"""

    @pytest.fixture(scope="class")
    def schema(self):
        return load_schema()

    def test_evidence_summary_required_fields(self, schema):
        """evidence_summary 必须包含所有必需字段"""
        event = create_mock_audit_event()
        evidence_summary = event["evidence_summary"]

        required = ["count", "has_strong", "uris"]
        for field in required:
            assert field in evidence_summary, f"evidence_summary 缺少字段: {field}"

    def test_evidence_summary_count_non_negative(self, schema):
        """evidence_summary count 必须非负"""
        event = create_mock_audit_event()
        event["evidence_summary"]["count"] = -1

        with pytest.raises(ValidationError):
            validate(instance=event, schema=schema)

    def test_evidence_summary_uris_max_5(self, schema):
        """evidence_summary uris 最多 5 个"""
        event = create_mock_audit_event()
        event["evidence_summary"]["uris"] = ["uri1", "uri2", "uri3", "uri4", "uri5"]

        # 5 个应该通过
        validate(instance=event, schema=schema)

        # 6 个应该失败
        event["evidence_summary"]["uris"].append("uri6")
        with pytest.raises(ValidationError):
            validate(instance=event, schema=schema)


class TestSourceEnum:
    """测试 source 枚举值"""

    @pytest.fixture(scope="class")
    def schema(self):
        return load_schema()

    def test_valid_source_values(self, schema):
        """有效的 source 值应通过"""
        event = create_mock_audit_event()

        for source in ["gateway", "outbox_worker", "reconcile_outbox"]:
            event["source"] = source
            validate(instance=event, schema=schema)

    def test_invalid_source_fails(self, schema):
        """无效的 source 值应失败"""
        event = create_mock_audit_event()
        event["source"] = "invalid_source"

        with pytest.raises(ValidationError):
            validate(instance=event, schema=schema)


class TestEventTsFormat:
    """测试 event_ts 日期时间格式"""

    @pytest.fixture(scope="class")
    def schema(self):
        return load_schema()

    def test_valid_iso8601_formats(self, schema):
        """有效的 ISO8601 格式应通过"""
        event = create_mock_audit_event()

        valid_formats = [
            "2024-01-15T10:30:00+00:00",
            "2024-01-15T10:30:00.000000+00:00",
            "2024-01-15T10:30:00Z",
            "2024-01-15T10:30:00.123456Z",
            "2024-01-15T18:30:00+08:00",
        ]

        for fmt in valid_formats:
            event["event_ts"] = fmt
            validate(instance=event, schema=schema)

    def test_invalid_datetime_format_fails(self, schema):
        """无效的日期时间格式应失败"""
        event = create_mock_audit_event()

        invalid_formats = [
            "2024-01-15",
            "10:30:00",
            "invalid",
            "2024/01/15T10:30:00Z",
        ]

        for fmt in invalid_formats:
            event["event_ts"] = fmt
            with pytest.raises(ValidationError):
                validate(instance=event, schema=schema)


class TestEvidenceRefsJsonSchema:
    """测试 evidence_refs_json 结构 schema"""

    @pytest.fixture(scope="class")
    def schema(self):
        return load_schema()

    def test_valid_evidence_refs_json_passes(self, schema):
        """完整的有效 evidence_refs_json 应通过"""
        refs_json = create_mock_evidence_refs_json()
        validate(instance=refs_json, schema=schema)

    def test_evidence_refs_json_required_gateway_event(self, schema):
        """evidence_refs_json 必须包含 gateway_event"""
        refs_json = create_mock_evidence_refs_json()
        del refs_json["gateway_event"]

        with pytest.raises(ValidationError):
            validate(instance=refs_json, schema=schema)

    def test_patch_item_artifact_uri_pattern(self, schema):
        """patch_item artifact_uri 格式校验"""
        refs_json = create_mock_evidence_refs_json()

        # 有效格式
        refs_json["patches"][0]["artifact_uri"] = (
            "memory://patch_blobs/git/repo123/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        )
        validate(instance=refs_json, schema=schema)

    def test_patch_item_sha256_pattern(self, schema):
        """patch_item sha256 格式校验"""
        refs_json = create_mock_evidence_refs_json()

        # 有效 SHA256 (64 位十六进制)
        refs_json["patches"][0]["sha256"] = "a" * 64
        validate(instance=refs_json, schema=schema)


class TestPolicySubstructure:
    """测试 policy 子结构 schema"""

    @pytest.fixture(scope="class")
    def schema(self):
        return load_schema()

    def test_valid_policy_modes(self, schema):
        """有效的 policy mode 值应通过"""
        event = create_mock_audit_event()
        event["policy"] = {
            "mode": "strict",
            "mode_reason": "explicit_header",
            "policy_version": "v2",
            "is_pointerized": False,
            "policy_source": "settings",
        }

        validate(instance=event, schema=schema)

        # compat mode
        event["policy"]["mode"] = "compat"
        validate(instance=event, schema=schema)

        # null mode
        event["policy"]["mode"] = None
        validate(instance=event, schema=schema)

    def test_valid_policy_versions(self, schema):
        """有效的 policy_version 值应通过"""
        event = create_mock_audit_event()
        event["policy"] = {"mode": "strict", "policy_version": "v1", "is_pointerized": False}

        for version in ["v1", "v2", None]:
            event["policy"]["policy_version"] = version
            validate(instance=event, schema=schema)


class TestSchemaExamplesValid:
    """测试 schema 中的 examples 是否有效"""

    @pytest.fixture(scope="class")
    def schema(self):
        return load_schema()

    def test_schema_examples_pass_validation(self, schema):
        """schema 中的 examples 应通过校验"""
        examples = schema.get("examples", [])

        for i, example in enumerate(examples):
            try:
                validate(instance=example, schema=schema)
            except ValidationError as e:
                pytest.fail(f"Example {i} failed validation: {e.message}")


class TestTrimSubstructure:
    """测试 trim 子结构 schema"""

    @pytest.fixture(scope="class")
    def schema(self):
        return load_schema()

    def test_trim_was_trimmed_boolean(self, schema):
        """trim.was_trimmed 必须是布尔值"""
        event = create_mock_audit_event()

        event["trim"]["was_trimmed"] = True
        validate(instance=event, schema=schema)

        event["trim"]["was_trimmed"] = False
        validate(instance=event, schema=schema)

    def test_trim_with_original_len(self, schema):
        """trim 包含 original_len 的情况"""
        event = create_mock_audit_event()
        event["trim"] = {"was_trimmed": True, "why": "content_too_long", "original_len": 10000}

        validate(instance=event, schema=schema)


class TestPointerSubstructure:
    """测试 pointer 子结构 schema"""

    @pytest.fixture(scope="class")
    def schema(self):
        return load_schema()

    def test_pointer_required_fields(self, schema):
        """pointer 必须包含 from_space 和 to_space"""
        event = create_mock_audit_event()
        event["pointer"] = {
            "from_space": "personal",
            "to_space": "team:project",
            "reason": "redirect_policy",
            "preserved": True,
        }

        validate(instance=event, schema=schema)

    def test_pointer_minimal(self, schema):
        """pointer 最小结构（仅必需字段）"""
        event = create_mock_audit_event()
        event["pointer"] = {"from_space": "personal", "to_space": "team:project"}

        validate(instance=event, schema=schema)


# ============================================================================
# Object Store Audit Event Schema 测试
# ============================================================================


def load_object_store_schema() -> Dict[str, Any]:
    """加载 object_store_audit_event_v1 schema"""
    if not OBJECT_STORE_SCHEMA_PATH.exists():
        pytest.skip(f"Schema 文件不存在: {OBJECT_STORE_SCHEMA_PATH}")

    with open(OBJECT_STORE_SCHEMA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def create_mock_object_store_audit_event() -> Dict[str, Any]:
    """创建一个符合规范的 mock object_store_audit_event"""
    return {
        "schema_version": "1.0",
        "provider": "minio",
        "event_ts": "2024-01-15T10:00:00.000Z",
        "bucket": "engram-artifacts",
        "object_key": "scm/1/git/commits/abc123.diff",
        "operation": "s3:PutObject",
        "status_code": 200,
        "success": True,
        "request_id": "REQ-123-456",
        "principal": "AKIAIOSFODNN7EXAMPLE",
        "remote_ip": "192.168.1.100",
        "user_agent": "MinIO Console",
        "bytes_sent": 0,
        "bytes_received": 1024,
        "duration_ms": 15,
        "raw": {
            "version": "1",
            "deploymentid": "test-deployment",
            "time": "2024-01-15T10:00:00.000Z",
            "api": {
                "name": "PutObject",
                "bucket": "engram-artifacts",
                "object": "scm/1/git/commits/abc123.diff",
                "statusCode": 200,
            },
        },
    }


class TestObjectStoreAuditEventSchema:
    """测试 object_store_audit_event_v1 schema 校验"""

    @pytest.fixture(scope="class")
    def schema(self):
        """加载 schema"""
        return load_object_store_schema()

    def test_valid_event_passes_schema(self, schema):
        """完整的有效事件应通过 schema 校验"""
        event = create_mock_object_store_audit_event()
        validate(instance=event, schema=schema)

    def test_required_fields_present(self, schema):
        """验证所有必需字段存在"""
        event = create_mock_object_store_audit_event()

        required_fields = ["schema_version", "provider", "event_ts", "bucket", "operation", "raw"]

        for field in required_fields:
            assert field in event, f"缺少必需字段: {field}"

    def test_missing_schema_version_fails(self, schema):
        """缺少 schema_version 应失败"""
        event = create_mock_object_store_audit_event()
        del event["schema_version"]

        with pytest.raises(ValidationError) as exc_info:
            validate(instance=event, schema=schema)

        assert "schema_version" in str(exc_info.value)

    def test_missing_provider_fails(self, schema):
        """缺少 provider 应失败"""
        event = create_mock_object_store_audit_event()
        del event["provider"]

        with pytest.raises(ValidationError) as exc_info:
            validate(instance=event, schema=schema)

        assert "provider" in str(exc_info.value)

    def test_missing_raw_fails(self, schema):
        """缺少 raw 应失败"""
        event = create_mock_object_store_audit_event()
        del event["raw"]

        with pytest.raises(ValidationError) as exc_info:
            validate(instance=event, schema=schema)

        assert "raw" in str(exc_info.value)

    def test_valid_provider_values(self, schema):
        """有效的 provider 值应通过"""
        event = create_mock_object_store_audit_event()

        for provider in ["minio", "aws", "gcs", "azure_blob", "other"]:
            event["provider"] = provider
            validate(instance=event, schema=schema)

    def test_invalid_provider_fails(self, schema):
        """无效的 provider 值应失败"""
        event = create_mock_object_store_audit_event()
        event["provider"] = "invalid_provider"

        with pytest.raises(ValidationError):
            validate(instance=event, schema=schema)

    def test_valid_operation_formats(self, schema):
        """有效的 operation 格式应通过"""
        event = create_mock_object_store_audit_event()

        valid_operations = [
            "s3:GetObject",
            "s3:PutObject",
            "s3:DeleteObject",
            "s3:ListBucket",
            "s3:HeadObject",
            "unknown",
        ]

        for op in valid_operations:
            event["operation"] = op
            validate(instance=event, schema=schema)

    def test_invalid_operation_fails(self, schema):
        """无效的 operation 格式应失败"""
        event = create_mock_object_store_audit_event()

        invalid_operations = [
            "GetObject",  # 缺少 s3: 前缀
            "s3:",  # 缺少操作名
            "aws:GetObject",  # 错误前缀
        ]

        for op in invalid_operations:
            event["operation"] = op
            with pytest.raises(ValidationError):
                validate(instance=event, schema=schema)

    def test_status_code_range(self, schema):
        """status_code 必须在有效范围内"""
        event = create_mock_object_store_audit_event()

        # 有效状态码
        for code in [100, 200, 301, 404, 500, 599]:
            event["status_code"] = code
            validate(instance=event, schema=schema)

        # 无效状态码
        event["status_code"] = 99
        with pytest.raises(ValidationError):
            validate(instance=event, schema=schema)

        event["status_code"] = 600
        with pytest.raises(ValidationError):
            validate(instance=event, schema=schema)

    def test_nullable_fields(self, schema):
        """可空字段设置为 null 应通过"""
        event = create_mock_object_store_audit_event()

        nullable_fields = [
            "object_key",
            "status_code",
            "request_id",
            "principal",
            "remote_ip",
            "user_agent",
            "bytes_sent",
            "bytes_received",
            "duration_ms",
        ]

        for field in nullable_fields:
            event[field] = None
            validate(instance=event, schema=schema)
            # 恢复原值
            event = create_mock_object_store_audit_event()

    def test_minimal_event(self, schema):
        """最小化事件（仅必需字段）应通过"""
        minimal_event = {
            "schema_version": "1.0",
            "provider": "minio",
            "event_ts": "2024-01-15T10:00:00.000Z",
            "bucket": "test-bucket",
            "operation": "s3:GetObject",
            "raw": {},
        }

        validate(instance=minimal_event, schema=schema)

    def test_schema_examples_pass_validation(self, schema):
        """schema 中的 examples 应通过校验"""
        examples = schema.get("examples", [])

        for i, example in enumerate(examples):
            try:
                validate(instance=example, schema=schema)
            except ValidationError as e:
                pytest.fail(f"Example {i} failed validation: {e.message}")


class TestObjectStoreAuditEventTimestamp:
    """测试 object_store_audit_event_v1 时间戳格式"""

    @pytest.fixture(scope="class")
    def schema(self):
        return load_object_store_schema()

    def test_valid_iso8601_formats(self, schema):
        """有效的 ISO8601 格式应通过"""
        event = create_mock_object_store_audit_event()

        valid_formats = [
            "2024-01-15T10:30:00+00:00",
            "2024-01-15T10:30:00.000Z",
            "2024-01-15T10:30:00Z",
            "2024-01-15T10:30:00.123456Z",
            "2024-01-15T18:30:00+08:00",
        ]

        for fmt in valid_formats:
            event["event_ts"] = fmt
            validate(instance=event, schema=schema)

    def test_invalid_datetime_format_fails(self, schema):
        """无效的日期时间格式应失败"""
        event = create_mock_object_store_audit_event()

        invalid_formats = [
            "2024-01-15",
            "10:30:00",
            "invalid",
            "2024/01/15T10:30:00Z",
        ]

        for fmt in invalid_formats:
            event["event_ts"] = fmt
            with pytest.raises(ValidationError):
                validate(instance=event, schema=schema)


class TestObjectStoreAuditEventIpAddress:
    """测试 object_store_audit_event_v1 IP 地址格式"""

    @pytest.fixture(scope="class")
    def schema(self):
        return load_object_store_schema()

    def test_valid_ipv4_addresses(self, schema):
        """有效的 IPv4 地址应通过"""
        event = create_mock_object_store_audit_event()

        valid_ips = [
            "192.168.1.100",
            "10.0.0.1",
            "172.16.0.1",
            "8.8.8.8",
        ]

        for ip in valid_ips:
            event["remote_ip"] = ip
            validate(instance=event, schema=schema)

    def test_null_ip_address(self, schema):
        """null IP 地址应通过"""
        event = create_mock_object_store_audit_event()
        event["remote_ip"] = None
        validate(instance=event, schema=schema)


# ============================================================================
# Logbook 查询契约测试 (evidence_refs_json 顶层字段)
# ============================================================================


class TestEvidenceRefsJsonLogbookQueryContract:
    """
    测试 evidence_refs_json 顶层字段契约

    确保 reconcile_outbox.py 使用的 SQL 查询：
        (evidence_refs_json->>'outbox_id')::int
    能够正确找到 outbox_id 字段。

    契约要求：
    - outbox_id 必须在顶层（不仅仅在 gateway_event 子对象中）
    - memory_id 必须在顶层
    - source 必须在顶层
    """

    def test_outbox_id_at_top_level(self):
        """
        契约测试：outbox_id 必须在 evidence_refs_json 顶层

        reconcile_outbox.py 使用以下查询：
            evidence_refs_json->>'outbox_id'

        因此 outbox_id 必须在顶层存在，而不仅仅在 gateway_event 中。
        """
        from engram.gateway.audit_event import (
            build_evidence_refs_json,
            build_outbox_worker_audit_event,
        )

        # 构建 outbox worker 审计事件
        gateway_event = build_outbox_worker_audit_event(
            operation="outbox_flush",
            correlation_id="corr-test123456789",
            actor_user_id="test_user",
            target_space="private:test_user",
            action="allow",
            reason="outbox_flush_success",
            payload_sha="sha256" * 8,
            outbox_id=12345,  # 关键字段
            memory_id="mem_abc123",
            worker_id="worker-test",
            attempt_id="attempt-test",
        )

        # 构建 evidence_refs_json
        evidence_refs_json = build_evidence_refs_json(
            evidence=None,
            gateway_event=gateway_event,
        )

        # 契约断言：outbox_id 必须在顶层
        assert "outbox_id" in evidence_refs_json, (
            "outbox_id 必须在 evidence_refs_json 顶层（用于 SQL 查询兼容性）"
        )
        assert evidence_refs_json["outbox_id"] == 12345, (
            f"顶层 outbox_id 值不正确，期望 12345，实际 {evidence_refs_json.get('outbox_id')}"
        )

        # 同时验证 gateway_event 中也有（保持完整元数据）
        assert evidence_refs_json["gateway_event"]["outbox_id"] == 12345, (
            "gateway_event 中也应保留 outbox_id"
        )

    def test_memory_id_at_top_level(self):
        """
        契约测试：memory_id 必须在 evidence_refs_json 顶层
        """
        from engram.gateway.audit_event import (
            build_evidence_refs_json,
            build_outbox_worker_audit_event,
        )

        gateway_event = build_outbox_worker_audit_event(
            operation="outbox_flush",
            correlation_id="corr-test123456789",
            actor_user_id="test_user",
            target_space="private:test_user",
            action="allow",
            reason="outbox_flush_success",
            payload_sha="sha256" * 8,
            outbox_id=12345,
            memory_id="mem_xyz789",  # 关键字段
        )

        evidence_refs_json = build_evidence_refs_json(
            evidence=None,
            gateway_event=gateway_event,
        )

        # 契约断言：memory_id 必须在顶层
        assert "memory_id" in evidence_refs_json, "memory_id 必须在 evidence_refs_json 顶层"
        assert evidence_refs_json["memory_id"] == "mem_xyz789", (
            f"顶层 memory_id 值不正确，期望 mem_xyz789，实际 {evidence_refs_json.get('memory_id')}"
        )

    def test_source_at_top_level(self):
        """
        契约测试：source 必须在 evidence_refs_json 顶层
        """
        from engram.gateway.audit_event import (
            build_evidence_refs_json,
            build_outbox_worker_audit_event,
        )

        gateway_event = build_outbox_worker_audit_event(
            operation="outbox_flush",
            correlation_id="corr-test123456789",
            actor_user_id="test_user",
            target_space="private:test_user",
            action="allow",
            reason="outbox_flush_success",
            payload_sha="sha256" * 8,
            outbox_id=12345,
        )

        evidence_refs_json = build_evidence_refs_json(
            evidence=None,
            gateway_event=gateway_event,
        )

        # 契约断言：source 必须在顶层
        assert "source" in evidence_refs_json, "source 必须在 evidence_refs_json 顶层"
        assert evidence_refs_json["source"] == "outbox_worker", (
            f"顶层 source 值不正确，期望 outbox_worker，实际 {evidence_refs_json.get('source')}"
        )

    def test_reconcile_audit_outbox_id_at_top_level(self):
        """
        契约测试：reconcile_outbox 审计的 outbox_id 必须在顶层
        """
        from engram.gateway.audit_event import (
            build_evidence_refs_json,
            build_reconcile_audit_event,
        )

        gateway_event = build_reconcile_audit_event(
            operation="outbox_reconcile",
            correlation_id="corr-reconcile12345",
            actor_user_id=None,
            target_space="team:test_project",
            action="allow",
            reason="outbox_flush_success",
            payload_sha="sha256" * 8,
            outbox_id=67890,  # 关键字段
            memory_id="mem_reconcile",
            original_locked_by="worker-old",
        )

        evidence_refs_json = build_evidence_refs_json(
            evidence=None,
            gateway_event=gateway_event,
        )

        # 契约断言
        assert "outbox_id" in evidence_refs_json, "reconcile 审计的 outbox_id 必须在顶层"
        assert evidence_refs_json["outbox_id"] == 67890
        assert evidence_refs_json["source"] == "reconcile_outbox"

    def test_no_outbox_id_when_not_provided(self):
        """
        契约测试：当 outbox_id 未提供时，顶层不应有该字段
        """
        from engram.gateway.audit_event import (
            build_evidence_refs_json,
            build_gateway_audit_event,
        )

        # Gateway 审计事件通常不包含 outbox_id
        gateway_event = build_gateway_audit_event(
            operation="memory_store",
            correlation_id="corr-gateway1234567",
            actor_user_id="test_user",
            requested_space="team:project",
            final_space="team:project",
            action="allow",
            reason="policy_passed",
            payload_sha="sha256" * 8,
            # 不设置 outbox_id
        )

        evidence_refs_json = build_evidence_refs_json(
            evidence=None,
            gateway_event=gateway_event,
        )

        # 契约断言：当 gateway_event 中没有 outbox_id 时，顶层也不应有
        assert "outbox_id" not in evidence_refs_json, (
            "当 gateway_event 中没有 outbox_id 时，顶层不应有该字段"
        )


# ============================================================================
# AuditWriteError 测试
# ============================================================================

# ============================================================================
# OpenMemory 失败路径 Audit Event Schema 契约测试
# ============================================================================


class TestOpenMemoryFailureAuditEventSchema:
    """
    测试 OpenMemory 失败路径生成的 audit_event 能通过 schema 校验

    契约要求：
    - decision.action="redirect" 表示写入路径被重定向到 outbox 补偿
    - gateway_event.extra.intended_action="deferred" 记录原意
    - 对外 MemoryStoreResponse.action 仍保持 "deferred"
    - evidence_refs_json 顶层包含 outbox_id/correlation_id/source/payload_sha
    """

    @pytest.fixture(scope="class")
    def schema(self):
        """加载 schema"""
        return load_schema()

    def test_openmemory_failure_audit_event_passes_schema(self, schema):
        """
        契约测试：OpenMemory 失败路径生成的 audit_event 应通过 schema 校验

        验证 build_gateway_audit_event 在 OpenMemory 失败场景下生成的结构
        符合 audit_event_v1.schema.json 定义。
        """
        from engram.gateway.audit_event import (
            build_gateway_audit_event,
        )

        # 模拟 OpenMemory 失败场景的审计事件
        # correlation_id 格式：corr-{16位十六进制}
        gateway_event = build_gateway_audit_event(
            operation="memory_store",
            correlation_id="corr-a1b2c3d4e5f67890",  # 16 位十六进制
            actor_user_id="test_user",
            requested_space="team:test_project",
            final_space="team:test_project",
            action="redirect",  # 审计内部使用 redirect 表示重定向到 outbox
            reason="openmemory_write_failed:connection_error",
            payload_sha="a" * 64,
            payload_len=1024,
            evidence=None,
            outbox_id=12345,
            extra={
                "last_error": "Connection refused",
                "error_code": "connection_error",
                "evidence_source": "none",
            },
            intended_action="deferred",  # 记录原意为 deferred
        )

        # 验证 gateway_event 通过 schema 校验
        validate(instance=gateway_event, schema=schema)

        # 验证关键字段
        assert gateway_event["decision"]["action"] == "redirect"
        assert gateway_event["extra"]["intended_action"] == "deferred"
        assert gateway_event["outbox_id"] == 12345

    def test_openmemory_failure_evidence_refs_json_passes_schema(self, schema):
        """
        契约测试：OpenMemory 失败路径生成的 evidence_refs_json 应通过 schema 校验

        验证 build_evidence_refs_json 在 OpenMemory 失败场景下生成的结构
        符合 audit_event_v1.schema.json 定义。
        """
        from engram.gateway.audit_event import (
            build_evidence_refs_json,
            build_gateway_audit_event,
        )

        gateway_event = build_gateway_audit_event(
            operation="memory_store",
            correlation_id="corr-87654321abcdef01",  # 16 位十六进制
            actor_user_id="test_user",
            requested_space="team:test_project",
            final_space="team:test_project",
            action="redirect",
            reason="openmemory_write_failed:api_error_500",
            payload_sha="b" * 64,
            payload_len=2048,
            evidence=None,
            outbox_id=67890,
            extra={
                "last_error": "Internal Server Error",
                "error_code": "api_error_500",
            },
            intended_action="deferred",
        )

        evidence_refs_json = build_evidence_refs_json(
            evidence=None,
            gateway_event=gateway_event,
        )

        # 验证 evidence_refs_json 通过 schema 校验
        validate(instance=evidence_refs_json, schema=schema)

        # 验证顶层字段（Logbook 查询契约）
        assert "outbox_id" in evidence_refs_json
        assert evidence_refs_json["outbox_id"] == 67890
        assert "correlation_id" in evidence_refs_json
        assert evidence_refs_json["correlation_id"] == "corr-87654321abcdef01"
        assert "source" in evidence_refs_json
        assert evidence_refs_json["source"] == "gateway"
        assert "payload_sha" in evidence_refs_json
        assert evidence_refs_json["payload_sha"] == "b" * 64

        # 验证 intended_action 被提升到顶层
        assert "intended_action" in evidence_refs_json
        assert evidence_refs_json["intended_action"] == "deferred"

    def test_redirect_action_for_outbox_is_distinct_from_space_redirect(self, schema):
        """
        契约测试：outbox 重定向（OpenMemory 失败）与空间重定向（策略）的区分

        两种场景都使用 action="redirect"，但通过以下字段区分：
        - outbox 重定向：extra.intended_action="deferred", outbox_id 非空
        - 空间重定向：final_space != requested_space, outbox_id 为空
        """
        from engram.gateway.audit_event import build_gateway_audit_event

        # 场景 1: OpenMemory 失败 -> outbox 重定向
        outbox_redirect = build_gateway_audit_event(
            operation="memory_store",
            correlation_id="corr-1111222233334444",  # 16 位十六进制
            actor_user_id="user1",
            requested_space="team:project",
            final_space="team:project",  # 空间未变
            action="redirect",
            reason="openmemory_write_failed:connection_error",
            payload_sha="c" * 64,
            outbox_id=100,
            extra={"last_error": "Connection refused"},
            intended_action="deferred",  # 标记原意
        )

        # 场景 2: 策略空间重定向
        space_redirect = build_gateway_audit_event(
            operation="memory_store",
            correlation_id="corr-5555666677778888",  # 16 位十六进制
            actor_user_id="user2",
            requested_space="team:restricted",
            final_space="private:user2",  # 空间改变
            action="redirect",
            reason="policy:team_write_disabled",
            payload_sha="d" * 64,
            # 无 outbox_id
            # 无 intended_action
        )

        # 两个事件都应通过 schema 校验
        validate(instance=outbox_redirect, schema=schema)
        validate(instance=space_redirect, schema=schema)

        # 验证区分条件
        # outbox 重定向：有 intended_action 和 outbox_id
        assert outbox_redirect["extra"]["intended_action"] == "deferred"
        assert outbox_redirect["outbox_id"] == 100

        # 空间重定向：无 intended_action，无 outbox_id
        assert "intended_action" not in space_redirect.get("extra", {})
        assert "outbox_id" not in space_redirect


class TestV2EvidenceWithNullEvidenceRefs:
    """
    测试 v2 evidence + evidence_refs=None 场景的策略决策

    契约要求：
    - 当 evidence (v2 格式) 非空但 evidence_refs (v1 格式) 为 None 时
    - 策略检查不应触发 missing_evidence
    - 因为规范化后存在有效 evidence
    """

    def test_v2_evidence_only_should_not_trigger_missing_evidence(self):
        """
        契约测试：仅使用 v2 evidence（evidence_refs=None）时，策略应通过

        场景：
        - evidence: [{"type": "patch", "sha256": "..."}]（v2 格式非空）
        - evidence_refs: None（v1 格式为空）

        预期：策略决策为 allow，不触发 missing_evidence
        """
        from engram.gateway.policy import PolicyAction, PolicyEngine

        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={
                "require_evidence": True,
                "allowed_kinds": [],
            },
        )

        # 模拟 memory_store_impl 的行为：
        # 1. 调用 normalize_evidence 后得到非空列表
        # 2. 计算 evidence_present = len(normalized) > 0 = True
        # 3. 传递 evidence_present=True 给 policy.decide()

        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id="alice",
            payload_md="content with v2 evidence only",
            evidence_refs=None,  # v1 格式为空
            evidence_present=True,  # v2 规范化后有 evidence
        )

        # 核心断言：不应触发 missing_evidence
        assert decision.action == PolicyAction.ALLOW, (
            f"v2 evidence 存在时不应触发 missing_evidence，实际 action={decision.action.value}, reason={decision.reason}"
        )
        assert decision.reason == "policy_passed", (
            f"预期 reason=policy_passed，实际 reason={decision.reason}"
        )

    def test_both_empty_should_trigger_missing_evidence(self):
        """
        契约测试：v1 和 v2 都为空时，应触发 missing_evidence
        """
        from engram.gateway.policy import PolicyAction, PolicyEngine

        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={
                "require_evidence": True,
                "allowed_kinds": [],
            },
        )

        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id="alice",
            payload_md="content without any evidence",
            evidence_refs=None,  # v1 为空
            evidence_present=False,  # v2 规范化后也为空
        )

        assert decision.action == PolicyAction.REDIRECT
        assert decision.reason == "missing_evidence"


# ============================================================================
# Strict 模式 Evidence 校验阻断审计契约测试
# ============================================================================


class TestStrictModeEvidenceValidationAuditContract:
    """
    strict 模式 evidence 校验阻断时的审计契约测试

    契约来源: docs/contracts/gateway_policy_v1.md

    验证:
    1. 阻断时审计事件必须包含完整的 validation 子结构
    2. evidence_validation 子结构必须包含 is_valid, error_codes, compat_warnings
    3. 审计可用于回归测试和问题诊断
    """

    @pytest.fixture(scope="class")
    def schema(self):
        """加载 schema"""
        return load_schema()

    def test_strict_mode_rejection_audit_passes_schema(self, schema):
        """
        契约测试: strict 模式阻断时的审计事件必须通过 schema 校验
        """
        from engram.gateway.audit_event import (
            build_gateway_audit_event,
            validate_evidence_for_strict_mode,
        )

        # 模拟缺少 sha256 的 evidence
        invalid_evidence = [
            {
                "uri": "memory://attachments/123/abc",
                # sha256 缺失
            }
        ]
        evidence_validation = validate_evidence_for_strict_mode(invalid_evidence)

        # 构建审计事件（使用符合格式的 correlation_id: 16 位十六进制）
        gateway_event = build_gateway_audit_event(
            operation="memory_store",
            correlation_id="corr-a1b2c3d4e5f67890",  # 16 位十六进制
            actor_user_id="test_user",
            requested_space="team:project",
            final_space=None,
            action="reject",
            reason="EVIDENCE_VALIDATION_FAILED:EVIDENCE_MISSING_SHA256",
            payload_sha="a" * 64,
            payload_len=100,
            evidence=invalid_evidence,
            policy_mode="strict",
            validate_refs_effective=True,
            validate_refs_reason="strict_enforced",
            evidence_validation=evidence_validation.to_dict(),
        )

        # 契约断言：通过 schema 校验
        validate(instance=gateway_event, schema=schema)

    def test_strict_mode_rejection_has_complete_validation_structure(self, schema):
        """
        契约测试: strict 模式阻断审计必须包含完整的 validation 子结构

        必需字段:
        - validate_refs_effective: bool
        - validate_refs_reason: str
        - evidence_validation: {is_valid, error_codes, compat_warnings}
        """
        from engram.gateway.audit_event import (
            build_gateway_audit_event,
            validate_evidence_for_strict_mode,
        )

        invalid_evidence = [
            {"uri": "memory://test/123"}  # 缺少 sha256
        ]
        ev_validation = validate_evidence_for_strict_mode(invalid_evidence)

        gateway_event = build_gateway_audit_event(
            operation="memory_store",
            correlation_id="corr-validstructure1",
            action="reject",
            reason="EVIDENCE_VALIDATION_FAILED:EVIDENCE_MISSING_SHA256",
            payload_sha="b" * 64,
            policy_mode="strict",
            validate_refs_effective=True,
            validate_refs_reason="strict_enforced",
            evidence_validation=ev_validation.to_dict(),
        )

        # 契约断言：validation 子结构存在且完整
        assert "validation" in gateway_event
        validation = gateway_event["validation"]

        # 必需字段
        assert "validate_refs_effective" in validation
        assert validation["validate_refs_effective"] is True

        assert "validate_refs_reason" in validation
        assert validation["validate_refs_reason"] == "strict_enforced"

        assert "evidence_validation" in validation
        ev_val = validation["evidence_validation"]

        # evidence_validation 内部结构
        assert "is_valid" in ev_val
        assert ev_val["is_valid"] is False

        assert "error_codes" in ev_val
        assert isinstance(ev_val["error_codes"], list)
        assert any("EVIDENCE_MISSING_SHA256" in code for code in ev_val["error_codes"])

        assert "compat_warnings" in ev_val
        assert isinstance(ev_val["compat_warnings"], list)

    def test_strict_mode_rejection_error_codes_are_traceable(self, schema):
        """
        契约测试: error_codes 必须可追溯（包含具体位置信息）

        格式: EVIDENCE_<TYPE>:evidence[<index>]:<uri_or_value>
        """
        from engram.gateway.audit_event import validate_evidence_for_strict_mode

        # 多个 evidence，第二个缺少 sha256
        evidence = [
            {
                "uri": "memory://attachments/1/e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            },
            {
                "uri": "memory://attachments/2/invalid",
                # sha256 缺失
            },
        ]

        result = validate_evidence_for_strict_mode(evidence)

        assert result.is_valid is False

        # error_codes 应包含索引信息（evidence[1]）
        error_code = result.error_codes[0]
        assert "evidence[1]" in error_code, (
            f"error_code 必须包含位置信息 'evidence[1]'，实际: {error_code}"
        )

    def test_strict_mode_rejection_audit_reason_format(self, schema):
        """
        契约测试: 阻断 reason 必须使用稳定格式

        格式: EVIDENCE_VALIDATION_FAILED:<PRIMARY_ERROR_CODE>
        """
        from engram.gateway.audit_event import (
            build_gateway_audit_event,
            validate_evidence_for_strict_mode,
        )

        invalid_evidence = [{"uri": "memory://test/123", "sha256": "invalid_format"}]
        ev_validation = validate_evidence_for_strict_mode(invalid_evidence)

        # 模拟 memory_store_impl 中的 reason 构建逻辑
        primary_error = ev_validation.error_codes[0] if ev_validation.error_codes else "UNKNOWN"
        reason = f"EVIDENCE_VALIDATION_FAILED:{primary_error.split(':')[0]}"

        gateway_event = build_gateway_audit_event(
            operation="memory_store",
            correlation_id="corr-reasonformat123",
            action="reject",
            reason=reason,
            payload_sha="c" * 64,
            policy_mode="strict",
            evidence_validation=ev_validation.to_dict(),
        )

        # 契约断言：reason 格式正确
        decision_reason = gateway_event["decision"]["reason"]
        assert decision_reason.startswith("EVIDENCE_VALIDATION_FAILED:"), (
            f"reason 必须以 EVIDENCE_VALIDATION_FAILED: 开头，实际: {decision_reason}"
        )

        # 必须包含具体的错误类型
        assert "EVIDENCE_" in decision_reason.split(":", 1)[1], (
            f"reason 必须包含具体错误类型（如 EVIDENCE_INVALID_SHA256），实际: {decision_reason}"
        )


# ============================================================================
# correlation_id 单一来源回归测试
# ============================================================================


class TestCorrelationIdUnifiedSourceRegression:
    """
    correlation_id 单一来源回归测试

    验证重构后所有 correlation_id 都来自统一的 generate_correlation_id() 函数，
    格式统一为 corr-{16位十六进制}。

    关键路径验证：
    1. evidence_refs_json 顶层 correlation_id 在 outbox_worker 路径存在
    2. evidence_refs_json 顶层 correlation_id 在 reconcile_outbox 路径存在
    3. evidence_refs_json 顶层 correlation_id 在失败审计路径存在
    """

    CORRELATION_ID_PATTERN = r"^corr-[a-fA-F0-9]{16}$"

    def test_outbox_worker_evidence_refs_json_has_top_level_correlation_id(self):
        """
        回归测试：outbox_worker 路径的 evidence_refs_json 顶层 correlation_id 存在
        """
        import re

        from engram.gateway.audit_event import (
            build_evidence_refs_json,
            build_outbox_worker_audit_event,
        )

        gateway_event = build_outbox_worker_audit_event(
            operation="outbox_flush",
            correlation_id="corr-abcd1234efab5678",  # 显式提供（16位十六进制）
            actor_user_id="test_user",
            target_space="private:test_user",
            action="allow",
            reason="outbox_flush_success",
            payload_sha="sha256" * 8,
            outbox_id=12345,
            memory_id="mem_abc123",
            worker_id="worker-test",
            attempt_id="attempt-test",
        )

        evidence_refs_json = build_evidence_refs_json(
            evidence=None,
            gateway_event=gateway_event,
        )

        # 契约断言：顶层必须有 correlation_id
        assert "correlation_id" in evidence_refs_json, (
            "outbox_worker 路径的 evidence_refs_json 顶层必须有 correlation_id"
        )

        # 格式验证
        correlation_id = evidence_refs_json["correlation_id"]
        assert re.match(self.CORRELATION_ID_PATTERN, correlation_id), (
            f"correlation_id 格式不正确: {correlation_id}"
        )

    def test_reconcile_outbox_evidence_refs_json_has_top_level_correlation_id(self):
        """
        回归测试：reconcile_outbox 路径的 evidence_refs_json 顶层 correlation_id 存在
        """
        import re

        from engram.gateway.audit_event import (
            build_evidence_refs_json,
            build_reconcile_audit_event,
        )

        gateway_event = build_reconcile_audit_event(
            operation="outbox_reconcile",
            correlation_id="corr-1234567890abcdef",  # 显式提供
            actor_user_id=None,
            target_space="team:test_project",
            action="allow",
            reason="outbox_flush_success",
            payload_sha="sha256" * 8,
            outbox_id=67890,
            memory_id="mem_reconcile",
            original_locked_by="worker-old",
        )

        evidence_refs_json = build_evidence_refs_json(
            evidence=None,
            gateway_event=gateway_event,
        )

        # 契约断言：顶层必须有 correlation_id
        assert "correlation_id" in evidence_refs_json, (
            "reconcile_outbox 路径的 evidence_refs_json 顶层必须有 correlation_id"
        )

        correlation_id = evidence_refs_json["correlation_id"]
        assert re.match(self.CORRELATION_ID_PATTERN, correlation_id), (
            f"correlation_id 格式不正确: {correlation_id}"
        )

    def test_failure_audit_evidence_refs_json_has_top_level_correlation_id(self):
        """
        回归测试：失败审计路径的 evidence_refs_json 顶层 correlation_id 存在
        """
        import re

        from engram.gateway.audit_event import (
            build_evidence_refs_json,
            build_gateway_audit_event,
        )

        gateway_event = build_gateway_audit_event(
            operation="memory_store",
            correlation_id="corr-fedcba0987654321",  # 显式提供
            actor_user_id="test_user",
            requested_space="team:test_project",
            final_space="team:test_project",
            action="redirect",  # 失败降级到 outbox
            reason="openmemory_write_failed:connection_error",
            payload_sha="a" * 64,
            payload_len=1024,
            outbox_id=11111,
            extra={
                "last_error": "Connection refused",
                "error_code": "connection_error",
            },
            intended_action="deferred",
        )

        evidence_refs_json = build_evidence_refs_json(
            evidence=None,
            gateway_event=gateway_event,
        )

        # 契约断言：顶层必须有 correlation_id
        assert "correlation_id" in evidence_refs_json, (
            "失败审计路径的 evidence_refs_json 顶层必须有 correlation_id"
        )

        correlation_id = evidence_refs_json["correlation_id"]
        assert re.match(self.CORRELATION_ID_PATTERN, correlation_id), (
            f"correlation_id 格式不正确: {correlation_id}"
        )

        # 验证与 gateway_event 一致
        assert evidence_refs_json["correlation_id"] == gateway_event["correlation_id"], (
            "顶层 correlation_id 应与 gateway_event 一致"
        )

    def test_auto_generated_correlation_id_has_correct_format(self):
        """
        回归测试：自动生成的 correlation_id 格式正确
        """
        import re

        from engram.gateway.audit_event import (
            build_evidence_refs_json,
            build_gateway_audit_event,
        )

        # 不提供 correlation_id，让系统自动生成
        gateway_event = build_gateway_audit_event(
            operation="memory_store",
            # correlation_id 未提供
            actor_user_id="test_user",
            requested_space="team:test_project",
            final_space="team:test_project",
            action="allow",
            reason="policy_passed",
            payload_sha="b" * 64,
            payload_len=512,
        )

        evidence_refs_json = build_evidence_refs_json(
            evidence=None,
            gateway_event=gateway_event,
        )

        # 验证自动生成的 correlation_id 格式
        correlation_id = gateway_event["correlation_id"]
        assert correlation_id is not None, "correlation_id 应自动生成"
        assert re.match(self.CORRELATION_ID_PATTERN, correlation_id), (
            f"自动生成的 correlation_id 格式不正确: {correlation_id}"
        )

        # 顶层也应一致
        assert evidence_refs_json.get("correlation_id") == correlation_id, (
            "顶层 correlation_id 应与自动生成的一致"
        )


class TestAuditWriteError:
    """
    测试 AuditWriteError 异常类

    验证审计写入失败时的异常行为符合 ADR "审计不可丢" 语义。
    """

    def test_audit_write_error_basic(self):
        """AuditWriteError 基本用法"""
        from engram.gateway.audit_event import AuditWriteError

        error = AuditWriteError(message="测试错误")
        assert error.message == "测试错误"
        assert error.original_error is None
        assert error.audit_data is None
        assert str(error) == "测试错误"

    def test_audit_write_error_with_original_error(self):
        """AuditWriteError 包含原始错误"""
        from engram.gateway.audit_event import AuditWriteError

        original = ValueError("数据库连接失败")
        error = AuditWriteError(
            message="审计写入失败",
            original_error=original,
        )

        assert error.message == "审计写入失败"
        assert error.original_error is original
        assert "数据库连接失败" in str(error)

    def test_audit_write_error_with_audit_data(self):
        """AuditWriteError 包含审计数据（用于诊断）"""
        from engram.gateway.audit_event import AuditWriteError

        audit_data = {
            "actor_user_id": "test_user",
            "target_space": "team:project",
            "action": "allow",
            "correlation_id": "corr-12345678901234",
        }

        error = AuditWriteError(
            message="审计写入失败",
            audit_data=audit_data,
        )

        assert error.audit_data == audit_data
        assert error.audit_data["correlation_id"] == "corr-12345678901234"


# ============================================================================
# Audit-First 语义契约测试
# ============================================================================


class TestAuditFirstSemantics:
    """
    测试 Audit-First 语义：审计写入失败时必须阻断主操作

    根据 ADR "审计不可丢" 要求：
    - Audit 写入失败：Gateway 应阻止主操作继续，避免不可审计的写入
    - OpenMemory 失败时 audit 与 outbox 的顺序与字段一致
    """

    # Mock 路径：handlers 模块使用的依赖
    HANDLER_MODULE = "engram.gateway.handlers.memory_store"

    @pytest.mark.asyncio
    async def test_audit_failure_blocks_openmemory_write(self):
        """
        契约测试：审计写入失败时必须阻断 OpenMemory 写入

        场景：pre-audit 写入失败
        预期：
        1. 返回错误响应
        2. OpenMemory 不被调用
        3. 错误消息明确指出审计写入失败
        """
        from engram.gateway.handlers.memory_store import memory_store_impl

        payload_md = "# Test content for audit failure"
        target_space = "team:test_project"

        with (
            patch(f"{self.HANDLER_MODULE}.get_config") as mock_config,
            patch(f"{self.HANDLER_MODULE}.logbook_adapter") as mock_adapter,
            patch(f"{self.HANDLER_MODULE}.get_db") as mock_get_db,
            patch(f"{self.HANDLER_MODULE}.get_client") as mock_get_client,
            patch(f"{self.HANDLER_MODULE}.create_engine_from_settings") as mock_engine,
        ):
            # 配置 mock
            mock_config.return_value.default_team_space = "team:default"
            mock_config.return_value.project_key = "test_project"
            mock_config.return_value.validate_evidence_refs = False
            mock_config.return_value.strict_mode_enforce_validate_refs = False

            # 模拟 check_dedup 返回 None（无重复）
            mock_adapter.check_dedup.return_value = None

            mock_db = MagicMock()
            mock_db.get_or_create_settings.return_value = {
                "team_write_enabled": True,
                "policy_json": {},
            }
            # 模拟审计写入失败
            mock_db.insert_audit.side_effect = Exception("数据库连接失败")
            mock_get_db.return_value = mock_db

            # 模拟策略引擎
            from engram.gateway.policy import PolicyAction

            mock_decision = MagicMock()
            mock_decision.action = PolicyAction.ALLOW
            mock_decision.reason = "policy_passed"
            mock_decision.final_space = target_space
            mock_engine.return_value.decide.return_value = mock_decision

            # 模拟 OpenMemory 成功（审计失败发生在后置阶段）
            mock_client = MagicMock()
            mock_store_result = MagicMock()
            mock_store_result.success = True
            mock_store_result.memory_id = "mem_audit_failed"
            mock_client.store.return_value = mock_store_result
            mock_get_client.return_value = mock_client

            # 执行
            result = await memory_store_impl(
                payload_md=payload_md,
                target_space=target_space,
            )

            # 验证：操作失败
            assert result.ok is False
            assert result.action == "error"

            # 验证：错误消息包含审计写入失败
            assert "审计" in result.message or "audit" in result.message.lower()

            # 验证：OpenMemory 已被调用
            mock_get_client.assert_called_once()

    @pytest.mark.asyncio
    async def test_policy_reject_audit_failure_blocks_response(self):
        """
        契约测试：策略拒绝时审计写入失败也应阻断

        场景：策略判定为 REJECT，但审计写入失败
        预期：返回审计错误，而非策略拒绝响应
        """
        from engram.gateway.handlers.memory_store import memory_store_impl

        payload_md = "# Content for policy reject with audit failure"
        target_space = "team:restricted"

        with (
            patch(f"{self.HANDLER_MODULE}.get_config") as mock_config,
            patch(f"{self.HANDLER_MODULE}.logbook_adapter") as mock_adapter,
            patch(f"{self.HANDLER_MODULE}.get_db") as mock_get_db,
            patch(f"{self.HANDLER_MODULE}.create_engine_from_settings") as mock_engine,
        ):
            mock_config.return_value.default_team_space = "team:default"
            mock_config.return_value.project_key = "test_project"
            mock_config.return_value.validate_evidence_refs = False
            mock_config.return_value.strict_mode_enforce_validate_refs = False

            mock_adapter.check_dedup.return_value = None

            mock_db = MagicMock()
            mock_db.get_or_create_settings.return_value = {
                "team_write_enabled": False,  # 禁用团队写入
                "policy_json": {},
            }
            # 模拟审计写入失败
            mock_db.insert_audit.side_effect = Exception("审计表锁定超时")
            mock_get_db.return_value = mock_db

            # 模拟策略引擎返回 REJECT
            from engram.gateway.policy import PolicyAction

            mock_decision = MagicMock()
            mock_decision.action = PolicyAction.REJECT
            mock_decision.reason = "team_write_disabled"
            mock_decision.final_space = None
            mock_engine.return_value.decide.return_value = mock_decision

            # 执行
            result = await memory_store_impl(
                payload_md=payload_md,
                target_space=target_space,
            )

            # 验证：返回审计错误（非策略拒绝）
            assert result.ok is False
            assert result.action == "error"
            assert "审计" in result.message or "audit" in result.message.lower()

    @pytest.mark.asyncio
    async def test_openmemory_failure_outbox_then_audit(self):
        """
        契约测试：OpenMemory 失败时，先写 outbox 再写 audit

        场景：OpenMemory 写入失败
        预期：
        1. 先写入 outbox（获取 outbox_id）
        2. 再写入审计（包含 outbox_id）
        3. 审计的 evidence_refs_json 顶层包含 outbox_id
        """
        from engram.gateway.handlers.memory_store import memory_store_impl
        from engram.gateway.openmemory_client import OpenMemoryConnectionError

        payload_md = "# Content for OpenMemory failure"
        target_space = "team:test_project"

        with (
            patch(f"{self.HANDLER_MODULE}.get_config") as mock_config,
            patch(f"{self.HANDLER_MODULE}.logbook_adapter") as mock_adapter,
            patch(f"{self.HANDLER_MODULE}.get_db") as mock_get_db,
            patch(f"{self.HANDLER_MODULE}.get_client") as mock_get_client,
            patch(f"{self.HANDLER_MODULE}.create_engine_from_settings") as mock_engine,
        ):
            mock_config.return_value.default_team_space = "team:default"
            mock_config.return_value.project_key = "test_project"
            mock_config.return_value.validate_evidence_refs = False
            mock_config.return_value.strict_mode_enforce_validate_refs = False

            mock_adapter.check_dedup.return_value = None

            mock_db = MagicMock()
            mock_db.get_or_create_settings.return_value = {
                "team_write_enabled": True,
                "policy_json": {},
            }
            # 模拟 outbox 入队返回 outbox_id
            mock_db.enqueue_outbox.return_value = 12345
            # 记录 insert_audit 调用参数
            audit_calls = []

            def record_audit_call(**kwargs):
                audit_calls.append(kwargs)
                return len(audit_calls)

            mock_db.insert_audit.side_effect = record_audit_call
            mock_get_db.return_value = mock_db

            # 模拟策略引擎
            from engram.gateway.policy import PolicyAction

            mock_decision = MagicMock()
            mock_decision.action = PolicyAction.ALLOW
            mock_decision.reason = "policy_passed"
            mock_decision.final_space = target_space
            mock_engine.return_value.decide.return_value = mock_decision

            # 模拟 OpenMemory 连接失败
            mock_client = MagicMock()
            mock_client.store.side_effect = OpenMemoryConnectionError("连接超时")
            mock_get_client.return_value = mock_client

            # 执行
            result = await memory_store_impl(
                payload_md=payload_md,
                target_space=target_space,
            )

            # 验证：操作返回错误但有 outbox
            assert result.ok is False
            assert "outbox_id=12345" in result.message

            # 验证：enqueue_outbox 被调用
            mock_db.enqueue_outbox.assert_called_once()

            # 验证：insert_audit 被调用（仅失败审计）
            assert len(audit_calls) == 1, "应只有 1 次失败审计调用"

            # 验证：失败审计包含 outbox_id
            failure_audit = audit_calls[0]
            evidence_refs_json = failure_audit.get("evidence_refs_json", {})

            # 契约断言：outbox_id 必须在顶层
            assert "outbox_id" in evidence_refs_json, (
                "失败审计的 evidence_refs_json 必须包含顶层 outbox_id"
            )
            assert evidence_refs_json["outbox_id"] == 12345, (
                f"outbox_id 应为 12345，实际为 {evidence_refs_json.get('outbox_id')}"
            )

            # 契约断言：intended_action 必须在顶层（用于追踪原意）
            assert "intended_action" in evidence_refs_json, (
                "失败审计的 evidence_refs_json 必须包含顶层 intended_action"
            )
            assert evidence_refs_json["intended_action"] == "deferred", (
                f"intended_action 应为 'deferred'，实际为 {evidence_refs_json.get('intended_action')}"
            )


# ============================================================================
# memory_store_impl 关键分支契约测试（捕获 insert_audit payload 并校验 schema）
# ============================================================================


class TestGatewayEventPolicyValidationConsistency:
    """
    测试 gateway_event 的 policy 和 validation 子结构一致性

    契约要求（v1.1+）：
    - 所有返回分支的审计事件必须包含 policy 和 validation 子结构
    - 即使值为 None，字段也应该存在以保持一致性
    - 这确保了审计数据的结构稳定，便于下游查询和分析

    分支覆盖：
    - dedup_hit: policy/validation 字段为 None（未进入策略评估阶段）
    - policy_reject: policy/validation 字段有值
    - success: policy/validation 字段有值
    - openmemory_failure: policy/validation 字段有值
    - evidence_validation_failure: policy/validation 字段有值（strict 模式）
    - actor_unknown_reject/degrade/auto_create: policy/validation 字段为 None
    """

    @pytest.fixture(scope="class")
    def schema(self):
        """加载 schema"""
        return load_schema()

    def _assert_policy_substructure_exists(
        self, gateway_event: Dict[str, Any], allow_none_values: bool = False
    ):
        """
        断言 gateway_event 包含 policy 子结构

        Args:
            gateway_event: 审计事件
            allow_none_values: 是否允许所有字段值为 None（早期分支场景）
        """
        assert "policy" in gateway_event, "gateway_event 必须包含 policy 子结构"
        policy = gateway_event["policy"]

        # 必须包含的字段
        expected_fields = [
            "mode",
            "mode_reason",
            "policy_version",
            "is_pointerized",
            "policy_source",
        ]
        for field in expected_fields:
            assert field in policy, f"policy 子结构必须包含 {field} 字段"

        if not allow_none_values:
            # 非早期分支：至少 mode_reason 应该有值
            assert policy.get("mode_reason") is not None, "policy.mode_reason 不应为 None"

    def _assert_validation_substructure_exists(
        self, gateway_event: Dict[str, Any], allow_none_values: bool = False
    ):
        """
        断言 gateway_event 包含 validation 子结构

        Args:
            gateway_event: 审计事件
            allow_none_values: 是否允许所有字段值为 None（早期分支场景）
        """
        assert "validation" in gateway_event, "gateway_event 必须包含 validation 子结构"
        validation = gateway_event["validation"]

        # 必须包含的字段
        expected_fields = ["validate_refs_effective", "validate_refs_reason", "evidence_validation"]
        for field in expected_fields:
            assert field in validation, f"validation 子结构必须包含 {field} 字段"

        if not allow_none_values:
            # 非早期分支：至少 validate_refs_reason 应该有值
            assert validation.get("validate_refs_reason") is not None, (
                "validation.validate_refs_reason 不应为 None"
            )

    def test_dedup_hit_has_policy_validation_substructures(self, schema):
        """
        契约测试：dedup_hit 分支必须包含 policy/validation 子结构

        dedup_hit 发生在策略评估之前，字段值可以为 None
        """
        from engram.gateway.audit_event import build_gateway_audit_event

        gateway_event = build_gateway_audit_event(
            operation="memory_store",
            correlation_id="corr-ded0a1b2c3d4e5f6",  # 16 位十六进制
            actor_user_id="test_user",
            requested_space="team:project",
            final_space="team:project",
            action="allow",
            reason="dedup_hit",
            payload_sha="a" * 64,
            payload_len=100,
            # v1.1: 早期分支的 policy/validation 字段
            policy_mode=None,
            policy_mode_reason="dedup_hit_before_policy_evaluation",
            policy_version=None,
            policy_is_pointerized=False,
            policy_source=None,
            validate_refs_effective=None,
            validate_refs_reason="dedup_hit_before_validation",
            evidence_validation=None,
        )

        # 验证通过 schema
        validate(instance=gateway_event, schema=schema)

        # 验证 policy 子结构存在
        self._assert_policy_substructure_exists(gateway_event, allow_none_values=True)

        # 验证 validation 子结构存在
        self._assert_validation_substructure_exists(gateway_event, allow_none_values=True)

        # 验证 mode_reason 说明了原因
        assert gateway_event["policy"]["mode_reason"] == "dedup_hit_before_policy_evaluation"
        assert gateway_event["validation"]["validate_refs_reason"] == "dedup_hit_before_validation"

    def test_policy_reject_has_policy_validation_substructures(self, schema):
        """
        契约测试：policy_reject 分支必须包含 policy/validation 子结构
        """
        from engram.gateway.audit_event import build_gateway_audit_event

        gateway_event = build_gateway_audit_event(
            operation="memory_store",
            correlation_id="corr-a1b2c3d4e5f60001",  # 16 位十六进制
            actor_user_id="test_user",
            requested_space="team:project",
            final_space=None,
            action="reject",
            reason="policy:team_write_disabled",
            payload_sha="b" * 64,
            payload_len=200,
            # v1.1: policy 子结构
            policy_mode="compat",
            policy_mode_reason="from_settings",
            policy_version="v1",
            policy_is_pointerized=False,
            policy_source="settings",
            # v1.1: validation 子结构
            validate_refs_effective=False,
            validate_refs_reason="compat_mode_default",
            evidence_validation=None,
        )

        validate(instance=gateway_event, schema=schema)
        self._assert_policy_substructure_exists(gateway_event, allow_none_values=False)
        self._assert_validation_substructure_exists(gateway_event, allow_none_values=False)

        assert gateway_event["policy"]["mode"] == "compat"
        assert gateway_event["validation"]["validate_refs_effective"] is False

    def test_success_has_policy_validation_substructures(self, schema):
        """
        契约测试：success 分支必须包含 policy/validation 子结构
        """
        from engram.gateway.audit_event import build_gateway_audit_event

        gateway_event = build_gateway_audit_event(
            operation="memory_store",
            correlation_id="corr-a1b2c3d4e5f60002",  # 16 位十六进制
            actor_user_id="test_user",
            requested_space="team:project",
            final_space="team:project",
            action="allow",
            reason="policy:policy_passed",
            payload_sha="c" * 64,
            payload_len=300,
            memory_id="mem_success_001",
            # v1.1: policy 子结构
            policy_mode="strict",
            policy_mode_reason="from_settings",
            policy_version="v1",
            policy_is_pointerized=False,
            policy_source="settings",
            # v1.1: validation 子结构
            validate_refs_effective=True,
            validate_refs_reason="strict_enforced",
            evidence_validation={"is_valid": True, "error_codes": [], "compat_warnings": []},
        )

        validate(instance=gateway_event, schema=schema)
        self._assert_policy_substructure_exists(gateway_event, allow_none_values=False)
        self._assert_validation_substructure_exists(gateway_event, allow_none_values=False)

        assert gateway_event["policy"]["mode"] == "strict"
        assert gateway_event["validation"]["validate_refs_effective"] is True
        assert gateway_event["validation"]["evidence_validation"]["is_valid"] is True

    def test_openmemory_failure_has_policy_validation_substructures(self, schema):
        """
        契约测试：openmemory_failure 分支必须包含 policy/validation 子结构
        """
        from engram.gateway.audit_event import build_gateway_audit_event

        gateway_event = build_gateway_audit_event(
            operation="memory_store",
            correlation_id="corr-a1b2c3d4e5f60003",  # 16 位十六进制
            actor_user_id="test_user",
            requested_space="team:project",
            final_space="team:project",
            action="redirect",
            reason="openmemory_write_failed:connection_error",
            payload_sha="d" * 64,
            payload_len=400,
            outbox_id=12345,
            extra={"last_error": "Connection refused"},
            intended_action="deferred",
            # v1.1: policy 子结构
            policy_mode="compat",
            policy_mode_reason="from_settings",
            policy_version="v1",
            policy_is_pointerized=False,
            policy_source="settings",
            # v1.1: validation 子结构
            validate_refs_effective=False,
            validate_refs_reason="compat_mode_default",
            evidence_validation=None,
        )

        validate(instance=gateway_event, schema=schema)
        self._assert_policy_substructure_exists(gateway_event, allow_none_values=False)
        self._assert_validation_substructure_exists(gateway_event, allow_none_values=False)

        assert gateway_event["extra"]["intended_action"] == "deferred"

    def test_actor_unknown_reject_has_policy_validation_substructures(self, schema):
        """
        契约测试：actor_unknown_reject 分支必须包含 policy/validation 子结构

        actor 校验发生在策略评估之前，字段值可以为 None
        """
        from engram.gateway.audit_event import build_gateway_audit_event

        gateway_event = build_gateway_audit_event(
            operation="memory_store",
            correlation_id="corr-a1b2c3d4e5f60004",  # 16 位十六进制
            actor_user_id="unknown_user",
            requested_space="team:project",
            final_space=None,
            action="reject",
            reason="actor_unknown:reject",
            payload_sha="e" * 64,
            extra={"actor_policy": "reject"},
            # v1.1: 早期分支的 policy/validation 字段
            policy_mode=None,
            policy_mode_reason="actor_validation_before_policy_evaluation",
            policy_version=None,
            policy_is_pointerized=False,
            policy_source=None,
            validate_refs_effective=None,
            validate_refs_reason="actor_validation_before_validation",
            evidence_validation=None,
        )

        validate(instance=gateway_event, schema=schema)
        self._assert_policy_substructure_exists(gateway_event, allow_none_values=True)
        self._assert_validation_substructure_exists(gateway_event, allow_none_values=True)

        # 验证 mode_reason 说明了原因
        assert gateway_event["policy"]["mode_reason"] == "actor_validation_before_policy_evaluation"

    def test_actor_unknown_degrade_has_policy_validation_substructures(self, schema):
        """
        契约测试：actor_unknown_degrade 分支必须包含 policy/validation 子结构
        """
        from engram.gateway.audit_event import build_gateway_audit_event

        gateway_event = build_gateway_audit_event(
            operation="memory_store",
            correlation_id="corr-a1b2c3d4e5f60005",  # 16 位十六进制
            actor_user_id="unknown_user",
            requested_space="team:project",
            final_space="private:unknown",
            action="redirect",
            reason="actor_unknown:degrade",
            payload_sha="f" * 64,
            extra={"actor_policy": "degrade"},
            # v1.1: 早期分支的 policy/validation 字段
            policy_mode=None,
            policy_mode_reason="actor_validation_before_policy_evaluation",
            policy_version=None,
            policy_is_pointerized=False,
            policy_source=None,
            validate_refs_effective=None,
            validate_refs_reason="actor_validation_before_validation",
            evidence_validation=None,
        )

        validate(instance=gateway_event, schema=schema)
        self._assert_policy_substructure_exists(gateway_event, allow_none_values=True)
        self._assert_validation_substructure_exists(gateway_event, allow_none_values=True)

    def test_actor_autocreated_has_policy_validation_substructures(self, schema):
        """
        契约测试：actor_autocreated 分支必须包含 policy/validation 子结构
        """
        from engram.gateway.audit_event import build_gateway_audit_event

        gateway_event = build_gateway_audit_event(
            operation="memory_store",
            correlation_id="corr-a1b2c3d4e5f60006",  # 16 位十六进制
            actor_user_id="new_user",
            requested_space="team:project",
            final_space="team:project",
            action="allow",
            reason="actor_autocreated",
            payload_sha="0" * 64,
            extra={"actor_policy": "auto_create"},
            # v1.1: 早期分支的 policy/validation 字段
            policy_mode=None,
            policy_mode_reason="actor_validation_before_policy_evaluation",
            policy_version=None,
            policy_is_pointerized=False,
            policy_source=None,
            validate_refs_effective=None,
            validate_refs_reason="actor_validation_before_validation",
            evidence_validation=None,
        )

        validate(instance=gateway_event, schema=schema)
        self._assert_policy_substructure_exists(gateway_event, allow_none_values=True)
        self._assert_validation_substructure_exists(gateway_event, allow_none_values=True)


class TestMemoryStoreImplAuditPayloadContract:
    """
    memory_store_impl 关键分支契约测试

    测试策略：
    1. Mock 依赖（get_config, logbook_adapter, get_db, get_client, create_engine_from_settings）
    2. 捕获传给 insert_audit(evidence_refs_json=...) 的 payload
    3. 使用 schemas/audit_event_v1.schema.json 对 gateway_event 子对象进行校验
    4. 断言 evidence_refs_json 顶层字段满足 reconcile/outbox 查询契约

    契约要点：
    - evidence_refs_json.gateway_event 必须通过 audit_event_v1.schema.json 校验
    - evidence_refs_json 顶层必须包含 source, correlation_id, payload_sha（用于 SQL 查询）
    - OpenMemory 失败场景：顶层必须包含 outbox_id, intended_action
    """

    # Mock 路径：handlers 模块使用的依赖
    HANDLER_MODULE = "engram.gateway.handlers.memory_store"
    ACTOR_VALIDATION_MODULE = "engram.gateway.services.actor_validation"

    @pytest.fixture(scope="class")
    def schema(self):
        """加载 audit_event_v1 schema"""
        return load_schema()

    def _validate_gateway_event(self, evidence_refs_json: Dict[str, Any], schema: Dict[str, Any]):
        """
        校验 evidence_refs_json.gateway_event 是否符合 schema

        使用 audit_event_v1.schema.json 中的 audit_event 定义校验
        """
        gateway_event = evidence_refs_json.get("gateway_event")
        assert gateway_event is not None, "evidence_refs_json 必须包含 gateway_event"

        # 使用 schema 校验 gateway_event
        validate(instance=gateway_event, schema=schema)

    def _assert_reconcile_outbox_contract(
        self,
        evidence_refs_json: Dict[str, Any],
        require_outbox_id: bool = False,
        require_memory_id: bool = False,
        require_intended_action: bool = False,
    ):
        """
        断言 evidence_refs_json 满足 reconcile/outbox 查询契约

        契约要点（基于 reconcile_outbox.py SQL 查询）：
        - source: 必须在顶层
        - correlation_id: 必须在顶层
        - payload_sha: 必须在顶层
        - outbox_id: OpenMemory 失败场景必须在顶层
        - memory_id: 成功场景可选在顶层
        - intended_action: OpenMemory 失败场景必须在顶层
        """
        # 必需字段
        assert "source" in evidence_refs_json, (
            "evidence_refs_json 必须包含顶层 source（用于 SQL 查询）"
        )
        assert "correlation_id" in evidence_refs_json, (
            "evidence_refs_json 必须包含顶层 correlation_id（用于追踪）"
        )
        assert "payload_sha" in evidence_refs_json, (
            "evidence_refs_json 必须包含顶层 payload_sha（用于去重）"
        )

        # 条件必需字段
        if require_outbox_id:
            assert "outbox_id" in evidence_refs_json, (
                "OpenMemory 失败场景：evidence_refs_json 必须包含顶层 outbox_id"
            )

        if require_memory_id:
            assert "memory_id" in evidence_refs_json, (
                "成功场景：evidence_refs_json 必须包含顶层 memory_id"
            )

        if require_intended_action:
            assert "intended_action" in evidence_refs_json, (
                "OpenMemory 失败场景：evidence_refs_json 必须包含顶层 intended_action"
            )

    @pytest.mark.asyncio
    async def test_success_branch_audit_payload_schema(self, schema):
        """
        契约测试：成功写入（allow）分支的 audit payload 校验

        场景：策略允许且 OpenMemory 写入成功
        验证：
        1. gateway_event 通过 schema 校验
        2. 顶层字段满足 reconcile/outbox 查询契约
        """
        from engram.gateway.handlers.memory_store import memory_store_impl

        payload_md = "# Test content for success branch"
        target_space = "team:test_project"

        with (
            patch(f"{self.HANDLER_MODULE}.get_config") as mock_config,
            patch(f"{self.HANDLER_MODULE}.logbook_adapter") as mock_adapter,
            patch(f"{self.HANDLER_MODULE}.get_db") as mock_get_db,
            patch(f"{self.HANDLER_MODULE}.get_client") as mock_get_client,
            patch(f"{self.HANDLER_MODULE}.create_engine_from_settings") as mock_engine,
            patch(f"{self.ACTOR_VALIDATION_MODULE}.check_user_exists") as mock_check_user,
        ):
            mock_config.return_value.default_team_space = "team:default"
            mock_config.return_value.project_key = "test_project"
            mock_config.return_value.validate_evidence_refs = False
            mock_config.return_value.strict_mode_enforce_validate_refs = False

            # 用户存在
            mock_check_user.return_value = True

            mock_adapter.check_dedup.return_value = None

            mock_db = MagicMock()
            mock_db.get_or_create_settings.return_value = {
                "team_write_enabled": True,
                "policy_json": {},
            }

            # 捕获 insert_audit 调用参数
            audit_calls = []

            def capture_audit(**kwargs):
                audit_calls.append(kwargs)
                return len(audit_calls)

            mock_db.insert_audit.side_effect = capture_audit
            mock_get_db.return_value = mock_db

            # 模拟策略引擎
            from engram.gateway.policy import PolicyAction

            mock_decision = MagicMock()
            mock_decision.action = PolicyAction.ALLOW
            mock_decision.reason = "policy_passed"
            mock_decision.final_space = target_space
            mock_engine.return_value.decide.return_value = mock_decision

            # 模拟 OpenMemory 成功
            mock_client = MagicMock()
            mock_store_result = MagicMock()
            mock_store_result.success = True
            mock_store_result.memory_id = "mem_success_001"
            mock_client.store.return_value = mock_store_result
            mock_get_client.return_value = mock_client

            # 执行
            result = await memory_store_impl(
                payload_md=payload_md,
                target_space=target_space,
                actor_user_id="test_user",
            )

            # 验证操作成功
            assert result.ok is True
            assert result.action == "allow"
            assert result.memory_id == "mem_success_001"

            # 捕获并校验 audit payload
            assert len(audit_calls) == 1, "成功分支应有 1 次 insert_audit 调用"
            evidence_refs_json = audit_calls[0].get("evidence_refs_json", {})

            # 校验 gateway_event 符合 schema
            self._validate_gateway_event(evidence_refs_json, schema)

            # 校验顶层字段满足 reconcile/outbox 查询契约
            self._assert_reconcile_outbox_contract(
                evidence_refs_json,
                require_outbox_id=False,
                require_memory_id=True,
            )

            # 校验 gateway_event 关键字段
            gateway_event = evidence_refs_json["gateway_event"]
            assert gateway_event["source"] == "gateway"
            assert gateway_event["operation"] == "memory_store"
            assert gateway_event["decision"]["action"] == "allow"

    @pytest.mark.asyncio
    async def test_policy_reject_branch_audit_payload_schema(self, schema):
        """
        契约测试：策略拒绝（reject）分支的 audit payload 校验

        场景：策略判定为 REJECT
        验证：
        1. gateway_event 通过 schema 校验
        2. decision.action == "reject"
        3. 顶层字段满足契约
        """
        from engram.gateway.handlers.memory_store import memory_store_impl

        payload_md = "# Test content for reject branch"
        target_space = "team:restricted"

        with (
            patch(f"{self.HANDLER_MODULE}.get_config") as mock_config,
            patch(f"{self.HANDLER_MODULE}.logbook_adapter") as mock_adapter,
            patch(f"{self.HANDLER_MODULE}.get_db") as mock_get_db,
            patch(f"{self.HANDLER_MODULE}.create_engine_from_settings") as mock_engine,
            patch(f"{self.ACTOR_VALIDATION_MODULE}.check_user_exists") as mock_check_user,
        ):
            mock_config.return_value.default_team_space = "team:default"
            mock_config.return_value.project_key = "test_project"
            mock_config.return_value.validate_evidence_refs = False
            mock_config.return_value.strict_mode_enforce_validate_refs = False

            # 用户存在
            mock_check_user.return_value = True

            mock_adapter.check_dedup.return_value = None

            mock_db = MagicMock()
            mock_db.get_or_create_settings.return_value = {
                "team_write_enabled": False,
                "policy_json": {},
            }

            # 捕获 insert_audit 调用参数
            audit_calls = []

            def capture_audit(**kwargs):
                audit_calls.append(kwargs)
                return len(audit_calls)

            mock_db.insert_audit.side_effect = capture_audit
            mock_get_db.return_value = mock_db

            # 模拟策略拒绝
            from engram.gateway.policy import PolicyAction

            mock_decision = MagicMock()
            mock_decision.action = PolicyAction.REJECT
            mock_decision.reason = "team_write_disabled"
            mock_decision.final_space = None
            mock_engine.return_value.decide.return_value = mock_decision

            # 执行
            result = await memory_store_impl(
                payload_md=payload_md,
                target_space=target_space,
                actor_user_id="test_user",
            )

            # 验证操作被拒绝
            assert result.ok is False
            assert result.action == "reject"

            # 捕获并校验 audit payload
            assert len(audit_calls) == 1, "策略拒绝分支应有 1 次 insert_audit 调用"
            evidence_refs_json = audit_calls[0].get("evidence_refs_json", {})

            # 校验 gateway_event 符合 schema
            self._validate_gateway_event(evidence_refs_json, schema)

            # 校验顶层字段满足契约
            self._assert_reconcile_outbox_contract(evidence_refs_json)

            # 校验 gateway_event 关键字段
            gateway_event = evidence_refs_json["gateway_event"]
            assert gateway_event["decision"]["action"] == "reject"
            assert "team_write_disabled" in gateway_event["decision"]["reason"]

    @pytest.mark.asyncio
    async def test_openmemory_failure_branch_audit_payload_schema(self, schema):
        """
        契约测试：OpenMemory 失败（deferred）分支的 audit payload 校验

        场景：策略允许但 OpenMemory 写入失败
        验证：
        1. gateway_event 通过 schema 校验
        2. 顶层包含 outbox_id 和 intended_action
        3. decision.action == "redirect"（内部）
        4. extra.intended_action == "deferred"
        """
        from engram.gateway.handlers.memory_store import memory_store_impl
        from engram.gateway.openmemory_client import OpenMemoryConnectionError

        payload_md = "# Test content for OpenMemory failure"
        target_space = "team:test_project"

        with (
            patch(f"{self.HANDLER_MODULE}.get_config") as mock_config,
            patch(f"{self.HANDLER_MODULE}.logbook_adapter") as mock_adapter,
            patch(f"{self.HANDLER_MODULE}.get_db") as mock_get_db,
            patch(f"{self.HANDLER_MODULE}.get_client") as mock_get_client,
            patch(f"{self.HANDLER_MODULE}.create_engine_from_settings") as mock_engine,
            patch(f"{self.ACTOR_VALIDATION_MODULE}.check_user_exists") as mock_check_user,
        ):
            mock_config.return_value.default_team_space = "team:default"
            mock_config.return_value.project_key = "test_project"
            mock_config.return_value.validate_evidence_refs = False
            mock_config.return_value.strict_mode_enforce_validate_refs = False

            # 用户存在
            mock_check_user.return_value = True

            mock_adapter.check_dedup.return_value = None

            mock_db = MagicMock()
            mock_db.get_or_create_settings.return_value = {
                "team_write_enabled": True,
                "policy_json": {},
            }
            mock_db.enqueue_outbox.return_value = 99999  # outbox_id

            # 捕获 insert_audit 调用参数
            audit_calls = []

            def capture_audit(**kwargs):
                audit_calls.append(kwargs)
                return len(audit_calls)

            mock_db.insert_audit.side_effect = capture_audit
            mock_get_db.return_value = mock_db

            # 模拟策略引擎
            from engram.gateway.policy import PolicyAction

            mock_decision = MagicMock()
            mock_decision.action = PolicyAction.ALLOW
            mock_decision.reason = "policy_passed"
            mock_decision.final_space = target_space
            mock_engine.return_value.decide.return_value = mock_decision

            # 模拟 OpenMemory 连接失败
            mock_client = MagicMock()
            mock_client.store.side_effect = OpenMemoryConnectionError("Connection refused")
            mock_get_client.return_value = mock_client

            # 执行
            result = await memory_store_impl(
                payload_md=payload_md,
                target_space=target_space,
                actor_user_id="test_user",
            )

            # 验证操作降级到 outbox
            assert result.ok is False
            assert result.action == "deferred"
            assert result.outbox_id == 99999

            # 捕获并校验 audit payload
            assert len(audit_calls) == 1, "OpenMemory 失败分支应有 1 次失败审计调用"
            evidence_refs_json = audit_calls[0].get("evidence_refs_json", {})

            # 校验 gateway_event 符合 schema
            self._validate_gateway_event(evidence_refs_json, schema)

            # 校验顶层字段满足 reconcile/outbox 查询契约（关键）
            self._assert_reconcile_outbox_contract(
                evidence_refs_json,
                require_outbox_id=True,
                require_intended_action=True,
            )

            # 校验 gateway_event 关键字段
            gateway_event = evidence_refs_json["gateway_event"]
            assert gateway_event["decision"]["action"] == "redirect"
            assert gateway_event["extra"]["intended_action"] == "deferred"
            assert gateway_event["outbox_id"] == 99999

            # 校验顶层字段（SQL 查询使用）
            assert evidence_refs_json["outbox_id"] == 99999
            assert evidence_refs_json["intended_action"] == "deferred"

    @pytest.mark.asyncio
    async def test_dedup_hit_branch_audit_payload_schema(self, schema):
        """
        契约测试：Dedup 命中分支的 audit payload 校验

        场景：存在相同内容的成功写入记录
        验证：
        1. gateway_event 通过 schema 校验
        2. decision.action == "allow"
        3. 包含 original_outbox_id 信息
        """
        from engram.gateway.handlers.memory_store import memory_store_impl
        from engram.gateway.services.hash_utils import compute_payload_sha

        payload_md = "# Test content for dedup hit"
        target_space = "team:test_project"
        payload_sha = compute_payload_sha(payload_md)

        with (
            patch(f"{self.HANDLER_MODULE}.get_config") as mock_config,
            patch(f"{self.HANDLER_MODULE}.logbook_adapter") as mock_adapter,
            patch(f"{self.HANDLER_MODULE}.get_db") as mock_get_db,
            patch(f"{self.ACTOR_VALIDATION_MODULE}.check_user_exists") as mock_check_user,
        ):
            mock_config.return_value.default_team_space = "team:default"
            mock_config.return_value.project_key = "test_project"

            # 用户存在
            mock_check_user.return_value = True

            # 模拟 dedup hit
            mock_adapter.check_dedup.return_value = {
                "outbox_id": 888,
                "target_space": target_space,
                "payload_sha": payload_sha,
                "status": "sent",
                "last_error": "memory_id=mem_existing_888",
            }

            mock_db = MagicMock()

            # 捕获 insert_audit 调用参数
            audit_calls = []

            def capture_audit(**kwargs):
                audit_calls.append(kwargs)
                return len(audit_calls)

            mock_db.insert_audit.side_effect = capture_audit
            mock_get_db.return_value = mock_db

            # 执行
            result = await memory_store_impl(
                payload_md=payload_md,
                target_space=target_space,
                actor_user_id="test_user",
            )

            # 验证 dedup hit 响应
            assert result.ok is True
            assert result.action == "allow"
            assert result.memory_id == "mem_existing_888"
            assert "dedup_hit" in result.message

            # 捕获并校验 audit payload
            assert len(audit_calls) == 1, "Dedup hit 分支应有 1 次 insert_audit 调用"
            evidence_refs_json = audit_calls[0].get("evidence_refs_json", {})

            # 校验 gateway_event 符合 schema
            self._validate_gateway_event(evidence_refs_json, schema)

            # 校验顶层字段满足契约
            self._assert_reconcile_outbox_contract(evidence_refs_json)

            # 校验 gateway_event 关键字段
            gateway_event = evidence_refs_json["gateway_event"]
            assert gateway_event["decision"]["action"] == "allow"
            assert gateway_event["decision"]["reason"] == "dedup_hit"

            # 校验包含 original_outbox_id
            assert evidence_refs_json.get("original_outbox_id") == 888

    @pytest.mark.asyncio
    async def test_redirect_branch_audit_payload_schema(self, schema):
        """
        契约测试：策略重定向（redirect）分支的 audit payload 校验

        场景：策略判定为 REDIRECT（空间重定向）
        验证：
        1. gateway_event 通过 schema 校验
        2. final_space != requested_space
        3. decision.action == "redirect"
        """
        from engram.gateway.handlers.memory_store import memory_store_impl

        payload_md = "# Test content for redirect branch"
        target_space = "team:restricted"
        redirect_space = "private:test_user"

        with (
            patch(f"{self.HANDLER_MODULE}.get_config") as mock_config,
            patch(f"{self.HANDLER_MODULE}.logbook_adapter") as mock_adapter,
            patch(f"{self.HANDLER_MODULE}.get_db") as mock_get_db,
            patch(f"{self.HANDLER_MODULE}.get_client") as mock_get_client,
            patch(f"{self.HANDLER_MODULE}.create_engine_from_settings") as mock_engine,
            patch(f"{self.ACTOR_VALIDATION_MODULE}.check_user_exists") as mock_check_user,
        ):
            mock_config.return_value.default_team_space = "team:default"
            mock_config.return_value.project_key = "test_project"
            mock_config.return_value.validate_evidence_refs = False
            mock_config.return_value.strict_mode_enforce_validate_refs = False

            # 用户存在
            mock_check_user.return_value = True

            mock_adapter.check_dedup.return_value = None

            mock_db = MagicMock()
            mock_db.get_or_create_settings.return_value = {
                "team_write_enabled": True,
                "policy_json": {},
            }

            # 捕获 insert_audit 调用参数
            audit_calls = []

            def capture_audit(**kwargs):
                audit_calls.append(kwargs)
                return len(audit_calls)

            mock_db.insert_audit.side_effect = capture_audit
            mock_get_db.return_value = mock_db

            # 模拟策略重定向
            from engram.gateway.policy import PolicyAction

            mock_decision = MagicMock()
            mock_decision.action = PolicyAction.REDIRECT
            mock_decision.reason = "redirect_to_private"
            mock_decision.final_space = redirect_space
            mock_engine.return_value.decide.return_value = mock_decision

            # 模拟 OpenMemory 成功
            mock_client = MagicMock()
            mock_store_result = MagicMock()
            mock_store_result.success = True
            mock_store_result.memory_id = "mem_redirect_001"
            mock_client.store.return_value = mock_store_result
            mock_get_client.return_value = mock_client

            # 执行
            result = await memory_store_impl(
                payload_md=payload_md,
                target_space=target_space,
                actor_user_id="test_user",
            )

            # 验证重定向成功
            assert result.ok is True
            assert result.action == "redirect"
            assert result.space_written == redirect_space

            # 捕获并校验 audit payload
            assert len(audit_calls) == 1, "重定向分支应有 1 次 insert_audit 调用"
            evidence_refs_json = audit_calls[0].get("evidence_refs_json", {})

            # 校验 gateway_event 符合 schema
            self._validate_gateway_event(evidence_refs_json, schema)

            # 校验顶层字段满足契约
            self._assert_reconcile_outbox_contract(evidence_refs_json)

            # 校验 gateway_event 关键字段
            gateway_event = evidence_refs_json["gateway_event"]
            assert gateway_event["decision"]["action"] == "redirect"
            assert gateway_event["requested_space"] == target_space
            assert gateway_event["final_space"] == redirect_space

    @pytest.mark.asyncio
    async def test_with_evidence_v2_audit_payload_schema(self, schema):
        """
        契约测试：带 evidence(v2) 的 audit payload 校验

        场景：请求包含结构化 evidence(v2)
        验证：
        1. gateway_event.evidence_summary 正确计算
        2. evidence_refs_json 包含 patches/external 分类
        """
        from engram.gateway.handlers.memory_store import memory_store_impl

        payload_md = "# Test content with evidence v2"
        target_space = "team:test_project"
        evidence_v2 = [
            {
                "uri": "memory://patch_blobs/git/1:abc123/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "source_type": "git",
                "source_id": "1:abc123",
            },
            {
                "uri": "https://example.com/doc.md",
            },
        ]

        with (
            patch(f"{self.HANDLER_MODULE}.get_config") as mock_config,
            patch(f"{self.HANDLER_MODULE}.logbook_adapter") as mock_adapter,
            patch(f"{self.HANDLER_MODULE}.get_db") as mock_get_db,
            patch(f"{self.HANDLER_MODULE}.get_client") as mock_get_client,
            patch(f"{self.HANDLER_MODULE}.create_engine_from_settings") as mock_engine,
            patch(f"{self.ACTOR_VALIDATION_MODULE}.check_user_exists") as mock_check_user,
        ):
            mock_config.return_value.default_team_space = "team:default"
            mock_config.return_value.project_key = "test_project"
            mock_config.return_value.validate_evidence_refs = False
            mock_config.return_value.strict_mode_enforce_validate_refs = False

            # 用户存在
            mock_check_user.return_value = True

            mock_adapter.check_dedup.return_value = None

            mock_db = MagicMock()
            mock_db.get_or_create_settings.return_value = {
                "team_write_enabled": True,
                "policy_json": {"evidence_mode": "compat"},
            }

            # 捕获 insert_audit 调用参数
            audit_calls = []

            def capture_audit(**kwargs):
                audit_calls.append(kwargs)
                return len(audit_calls)

            mock_db.insert_audit.side_effect = capture_audit
            mock_get_db.return_value = mock_db

            # 模拟策略引擎
            from engram.gateway.policy import PolicyAction

            mock_decision = MagicMock()
            mock_decision.action = PolicyAction.ALLOW
            mock_decision.reason = "policy_passed"
            mock_decision.final_space = target_space
            mock_engine.return_value.decide.return_value = mock_decision

            # 模拟 OpenMemory 成功
            mock_client = MagicMock()
            mock_store_result = MagicMock()
            mock_store_result.success = True
            mock_store_result.memory_id = "mem_evidence_001"
            mock_client.store.return_value = mock_store_result
            mock_get_client.return_value = mock_client

            # 执行
            result = await memory_store_impl(
                payload_md=payload_md,
                target_space=target_space,
                actor_user_id="test_user",
                evidence=evidence_v2,
            )

            # 验证成功
            assert result.ok is True

            # 捕获并校验 audit payload
            assert len(audit_calls) == 1
            evidence_refs_json = audit_calls[0].get("evidence_refs_json", {})

            # 校验 gateway_event 符合 schema
            self._validate_gateway_event(evidence_refs_json, schema)

            # 校验 evidence_summary
            gateway_event = evidence_refs_json["gateway_event"]
            evidence_summary = gateway_event["evidence_summary"]
            assert evidence_summary["count"] == 2
            assert evidence_summary["has_strong"] is True
            assert len(evidence_summary["uris"]) == 2

            # 校验分类结果
            assert "patches" in evidence_refs_json or "external" in evidence_refs_json

    @pytest.mark.asyncio
    async def test_correlation_id_consistency(self, schema):
        """
        契约测试：correlation_id 一致性

        验证：
        1. gateway_event.correlation_id 格式正确
        2. 顶层 correlation_id 与 gateway_event 一致
        """
        from engram.gateway.handlers.memory_store import memory_store_impl

        payload_md = "# Test correlation_id consistency"
        target_space = "team:test_project"

        with (
            patch(f"{self.HANDLER_MODULE}.get_config") as mock_config,
            patch(f"{self.HANDLER_MODULE}.logbook_adapter") as mock_adapter,
            patch(f"{self.HANDLER_MODULE}.get_db") as mock_get_db,
            patch(f"{self.HANDLER_MODULE}.get_client") as mock_get_client,
            patch(f"{self.HANDLER_MODULE}.create_engine_from_settings") as mock_engine,
        ):
            mock_config.return_value.default_team_space = "team:default"
            mock_config.return_value.project_key = "test_project"

            mock_adapter.check_dedup.return_value = None

            mock_db = MagicMock()
            mock_db.get_or_create_settings.return_value = {
                "team_write_enabled": True,
                "policy_json": {},
            }

            # 捕获 insert_audit 调用参数
            audit_calls = []

            def capture_audit(**kwargs):
                audit_calls.append(kwargs)
                return len(audit_calls)

            mock_db.insert_audit.side_effect = capture_audit
            mock_get_db.return_value = mock_db

            # 模拟策略引擎
            from engram.gateway.policy import PolicyAction

            mock_decision = MagicMock()
            mock_decision.action = PolicyAction.ALLOW
            mock_decision.reason = "policy_passed"
            mock_decision.final_space = target_space
            mock_engine.return_value.decide.return_value = mock_decision

            # 模拟 OpenMemory 成功
            mock_client = MagicMock()
            mock_store_result = MagicMock()
            mock_store_result.success = True
            mock_store_result.memory_id = "mem_corr_001"
            mock_client.store.return_value = mock_store_result
            mock_get_client.return_value = mock_client

            # 执行
            result = await memory_store_impl(
                payload_md=payload_md,
                target_space=target_space,
            )

            assert result.ok is True

            # 捕获并校验 correlation_id
            evidence_refs_json = audit_calls[0].get("evidence_refs_json", {})
            gateway_event = evidence_refs_json["gateway_event"]

            # correlation_id 格式校验
            correlation_id = gateway_event["correlation_id"]
            assert correlation_id.startswith("corr-"), (
                f"correlation_id 应以 'corr-' 开头，实际: {correlation_id}"
            )
            assert len(correlation_id) == 21, (
                f"correlation_id 长度应为 21（corr- + 16位hex），实际: {len(correlation_id)}"
            )

            # 顶层与 gateway_event 一致
            assert evidence_refs_json.get("correlation_id") == correlation_id
