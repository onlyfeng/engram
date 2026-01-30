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
import os
import pytest
from pathlib import Path
from typing import Any, Dict
from datetime import datetime, timezone

import jsonschema
from jsonschema import validate, ValidationError, Draft202012Validator


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
        "decision": {
            "action": "allow",
            "reason": "policy_passed"
        },
        "payload_sha": "abc123def456789012345678901234567890123456789012345678901234",
        "payload_len": 1024,
        "evidence_summary": {
            "count": 2,
            "has_strong": True,
            "uris": ["memory://patch_blobs/git/1:abc/sha256hash"]
        },
        "trim": {
            "was_trimmed": False,
            "why": None,
            "original_len": None
        },
        "refs": ["memory://patch_blobs/git/1:abc/sha256hash"],
        "event_ts": "2024-01-15T10:30:00.000000+00:00"
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
                "source_id": "abc123"
            }
        ],
        "attachments": [],
        "external": [
            {
                "uri": "https://example.com/doc"
            }
        ],
        "evidence_summary": {
            "count": 2,
            "has_strong": True,
            "uris": [
                "memory://patch_blobs/git/1:abc123/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "https://example.com/doc"
            ]
        }
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
            "schema_version", "source", "operation", "correlation_id",
            "decision", "evidence_summary", "trim", "refs", "event_ts"
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
        refs_json["patches"][0]["artifact_uri"] = "memory://patch_blobs/git/repo123/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
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
            "policy_source": "settings"
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
        event["policy"] = {
            "mode": "strict",
            "policy_version": "v1",
            "is_pointerized": False
        }
        
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
        event["trim"] = {
            "was_trimmed": True,
            "why": "content_too_long",
            "original_len": 10000
        }
        
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
            "preserved": True
        }
        
        validate(instance=event, schema=schema)

    def test_pointer_minimal(self, schema):
        """pointer 最小结构（仅必需字段）"""
        event = create_mock_audit_event()
        event["pointer"] = {
            "from_space": "personal",
            "to_space": "team:project"
        }
        
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
                "statusCode": 200
            }
        }
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
        
        required_fields = [
            "schema_version", "provider", "event_ts", "bucket", "operation", "raw"
        ]
        
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
            "object_key", "status_code", "request_id", "principal",
            "remote_ip", "user_agent", "bytes_sent", "bytes_received", "duration_ms"
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
            "raw": {}
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
            build_outbox_worker_audit_event,
            build_evidence_refs_json,
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
        assert "outbox_id" in evidence_refs_json, \
            "outbox_id 必须在 evidence_refs_json 顶层（用于 SQL 查询兼容性）"
        assert evidence_refs_json["outbox_id"] == 12345, \
            f"顶层 outbox_id 值不正确，期望 12345，实际 {evidence_refs_json.get('outbox_id')}"
        
        # 同时验证 gateway_event 中也有（保持完整元数据）
        assert evidence_refs_json["gateway_event"]["outbox_id"] == 12345, \
            "gateway_event 中也应保留 outbox_id"
    
    def test_memory_id_at_top_level(self):
        """
        契约测试：memory_id 必须在 evidence_refs_json 顶层
        """
        from engram.gateway.audit_event import (
            build_outbox_worker_audit_event,
            build_evidence_refs_json,
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
        assert "memory_id" in evidence_refs_json, \
            "memory_id 必须在 evidence_refs_json 顶层"
        assert evidence_refs_json["memory_id"] == "mem_xyz789", \
            f"顶层 memory_id 值不正确，期望 mem_xyz789，实际 {evidence_refs_json.get('memory_id')}"
    
    def test_source_at_top_level(self):
        """
        契约测试：source 必须在 evidence_refs_json 顶层
        """
        from engram.gateway.audit_event import (
            build_outbox_worker_audit_event,
            build_evidence_refs_json,
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
        assert "source" in evidence_refs_json, \
            "source 必须在 evidence_refs_json 顶层"
        assert evidence_refs_json["source"] == "outbox_worker", \
            f"顶层 source 值不正确，期望 outbox_worker，实际 {evidence_refs_json.get('source')}"
    
    def test_reconcile_audit_outbox_id_at_top_level(self):
        """
        契约测试：reconcile_outbox 审计的 outbox_id 必须在顶层
        """
        from engram.gateway.audit_event import (
            build_reconcile_audit_event,
            build_evidence_refs_json,
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
        assert "outbox_id" in evidence_refs_json, \
            "reconcile 审计的 outbox_id 必须在顶层"
        assert evidence_refs_json["outbox_id"] == 67890
        assert evidence_refs_json["source"] == "reconcile_outbox"
    
    def test_no_outbox_id_when_not_provided(self):
        """
        契约测试：当 outbox_id 未提供时，顶层不应有该字段
        """
        from engram.gateway.audit_event import (
            build_gateway_audit_event,
            build_evidence_refs_json,
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
        assert "outbox_id" not in evidence_refs_json, \
            "当 gateway_event 中没有 outbox_id 时，顶层不应有该字段"


# ============================================================================
# AuditWriteError 测试
# ============================================================================

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
        from unittest.mock import MagicMock, patch
        from engram.gateway.main import memory_store_impl
        from engram.gateway.audit_event import AuditWriteError
        
        payload_md = "# Test content for audit failure"
        target_space = "team:test_project"
        
        with patch("engram.gateway.main.get_config") as mock_config, \
             patch("engram.gateway.main.logbook_adapter") as mock_adapter, \
             patch("engram.gateway.main.get_db") as mock_get_db, \
             patch("engram.gateway.main.get_client") as mock_get_client, \
             patch("engram.gateway.main.create_engine_from_settings") as mock_engine:
            
            # 配置 mock
            mock_config.return_value.default_team_space = "team:default"
            mock_config.return_value.project_key = "test_project"
            
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
        from unittest.mock import MagicMock, patch
        from engram.gateway.main import memory_store_impl
        
        payload_md = "# Content for policy reject with audit failure"
        target_space = "team:restricted"
        
        with patch("engram.gateway.main.get_config") as mock_config, \
             patch("engram.gateway.main.logbook_adapter") as mock_adapter, \
             patch("engram.gateway.main.get_db") as mock_get_db, \
             patch("engram.gateway.main.create_engine_from_settings") as mock_engine:
            
            mock_config.return_value.default_team_space = "team:default"
            mock_config.return_value.project_key = "test_project"
            
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
        from unittest.mock import MagicMock, patch, call
        from engram.gateway.main import memory_store_impl
        from engram.gateway.openmemory_client import OpenMemoryConnectionError
        
        payload_md = "# Content for OpenMemory failure"
        target_space = "team:test_project"
        
        with patch("engram.gateway.main.get_config") as mock_config, \
             patch("engram.gateway.main.logbook_adapter") as mock_adapter, \
             patch("engram.gateway.main.get_db") as mock_get_db, \
             patch("engram.gateway.main.get_client") as mock_get_client, \
             patch("engram.gateway.main.create_engine_from_settings") as mock_engine:
            
            mock_config.return_value.default_team_space = "team:default"
            mock_config.return_value.project_key = "test_project"
            
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
            assert "outbox_id" in evidence_refs_json, \
                "失败审计的 evidence_refs_json 必须包含顶层 outbox_id"
            assert evidence_refs_json["outbox_id"] == 12345, \
                f"outbox_id 应为 12345，实际为 {evidence_refs_json.get('outbox_id')}"
