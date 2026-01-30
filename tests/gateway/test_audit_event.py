"""
test_audit_event - 审计事件构建模块的单元测试

验证 audit_event 模块的核心功能：
1. build_audit_event 必须包含 schema_version 和 correlation_id
2. 各种便捷函数的正确性
3. 兼容旧字段的保留
4. JSON Schema 校验（audit_event_v1.schema.json）
"""

import json
import os
import re
from pathlib import Path

import pytest
import jsonschema
from jsonschema import validate, ValidationError

from engram.gateway.audit_event import (
    AUDIT_EVENT_SCHEMA_VERSION,
    build_audit_event,
    build_evidence_refs_json,
    build_gateway_audit_event,
    build_outbox_worker_audit_event,
    build_reconcile_audit_event,
    classify_evidence_uri,
    compute_evidence_summary,
    generate_correlation_id,
    is_valid_sha256,
    parse_attachment_evidence_uri,
)


# ============================================================================
# Schema 加载与校验辅助
# ============================================================================

def _get_schema_path() -> Path:
    """获取 audit_event_v1.schema.json 的路径"""
    # 从测试文件向上找到项目根目录
    current = Path(__file__).resolve()
    # test_audit_event.py -> tests/ -> gateway/ -> openmemory_gateway/ -> apps/ -> engram/
    project_root = current.parent.parent.parent.parent.parent
    schema_path = project_root / "schemas" / "audit_event_v1.schema.json"
    return schema_path


def _load_schema() -> dict:
    """加载 audit_event_v1.schema.json"""
    schema_path = _get_schema_path()
    if not schema_path.exists():
        pytest.skip(f"Schema file not found: {schema_path}")
    with open(schema_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _validate_audit_event(event: dict, schema: dict) -> None:
    """校验审计事件是否符合 audit_event 定义"""
    # 使用 definitions/audit_event 校验单独的审计事件
    audit_event_schema = {
        "$schema": schema.get("$schema"),
        **schema.get("definitions", {}).get("audit_event", {})
    }
    # 需要合并 definitions 以支持 $ref
    audit_event_schema["definitions"] = schema.get("definitions", {})
    validate(instance=event, schema=audit_event_schema)


def _validate_evidence_refs_json(refs_json: dict, schema: dict) -> None:
    """校验 evidence_refs_json 是否符合 evidence_refs_json 定义"""
    # 使用 definitions/evidence_refs_json 校验
    refs_schema = {
        "$schema": schema.get("$schema"),
        **schema.get("definitions", {}).get("evidence_refs_json", {})
    }
    # 需要合并 definitions 以支持 $ref
    refs_schema["definitions"] = schema.get("definitions", {})
    validate(instance=refs_json, schema=refs_schema)


class TestBuildAuditEvent:
    """build_audit_event 函数测试"""

    def test_must_contain_schema_version(self):
        """验证审计事件必须包含 schema_version"""
        event = build_audit_event(
            source="test",
            operation="test_op",
        )
        
        assert "schema_version" in event
        assert event["schema_version"] == AUDIT_EVENT_SCHEMA_VERSION
        # v1.1: 新增 gateway_event.policy 和 gateway_event.validation 稳定子结构
        assert event["schema_version"] == "1.1"

    def test_must_contain_correlation_id(self):
        """验证审计事件必须包含 correlation_id"""
        event = build_audit_event(
            source="test",
            operation="test_op",
        )
        
        assert "correlation_id" in event
        assert event["correlation_id"] is not None
        assert event["correlation_id"].startswith("corr-")

    def test_uses_provided_correlation_id(self):
        """验证使用提供的 correlation_id"""
        custom_corr_id = "corr-custom12345678"
        event = build_audit_event(
            source="test",
            operation="test_op",
            correlation_id=custom_corr_id,
        )
        
        assert event["correlation_id"] == custom_corr_id

    def test_contains_required_fields(self):
        """验证审计事件包含所有必需字段"""
        event = build_audit_event(
            source="gateway",
            operation="memory_store",
            actor_user_id="user1",
            requested_space="team:project",
            final_space="team:project",
            action="allow",
            reason="policy:ok",
            payload_sha="abc123",
            payload_len=100,
        )
        
        # 必需元数据字段
        assert event["schema_version"] == AUDIT_EVENT_SCHEMA_VERSION
        assert event["source"] == "gateway"
        assert event["operation"] == "memory_store"
        assert event["correlation_id"] is not None
        
        # 参与者信息
        assert event["actor_user_id"] == "user1"
        
        # 空间信息
        assert event["requested_space"] == "team:project"
        assert event["final_space"] == "team:project"
        
        # 决策信息
        assert event["decision"]["action"] == "allow"
        assert event["decision"]["reason"] == "policy:ok"
        
        # Payload 信息
        assert event["payload_sha"] == "abc123"
        assert event["payload_len"] == 100
        
        # 证据摘要
        assert "evidence_summary" in event
        
        # 裁剪信息
        assert "trim" in event
        assert event["trim"]["was_trimmed"] is False
        
        # 兼容旧字段
        assert "refs" in event
        
        # 时间戳
        assert "event_ts" in event

    def test_preserves_legacy_outbox_id(self):
        """验证保留旧的 outbox_id 字段"""
        event = build_audit_event(
            source="outbox_worker",
            operation="outbox_flush",
            outbox_id=12345,
        )
        
        assert "outbox_id" in event
        assert event["outbox_id"] == 12345

    def test_preserves_legacy_refs(self):
        """验证保留旧的 refs 字段"""
        event = build_audit_event(
            source="gateway",
            operation="memory_store",
            evidence_refs=["ref1", "ref2", "ref3"],
        )
        
        assert "refs" in event
        assert event["refs"] == ["ref1", "ref2", "ref3"]

    def test_preserves_legacy_memory_id(self):
        """验证保留旧的 memory_id 字段"""
        event = build_audit_event(
            source="gateway",
            operation="memory_store",
            memory_id="mem-123",
        )
        
        assert "memory_id" in event
        assert event["memory_id"] == "mem-123"

    def test_extra_fields(self):
        """验证 extra 字段正确传递"""
        extra = {"custom_field": "custom_value", "number": 42}
        event = build_audit_event(
            source="test",
            operation="test_op",
            extra=extra,
        )
        
        assert "extra" in event
        assert event["extra"]["custom_field"] == "custom_value"
        assert event["extra"]["number"] == 42

    def test_trim_information(self):
        """验证裁剪信息正确记录"""
        event = build_audit_event(
            source="gateway",
            operation="memory_store",
            trim_was_trimmed=True,
            trim_why="payload_too_large",
            trim_original_len=10000,
        )
        
        assert event["trim"]["was_trimmed"] is True
        assert event["trim"]["why"] == "payload_too_large"
        assert event["trim"]["original_len"] == 10000


class TestComputeEvidenceSummary:
    """compute_evidence_summary 函数测试"""

    def test_empty_evidence(self):
        """验证空证据列表处理"""
        summary = compute_evidence_summary(None)
        assert summary["count"] == 0
        assert summary["has_strong"] is False
        assert summary["uris"] == []
        
        summary = compute_evidence_summary([])
        assert summary["count"] == 0

    def test_weak_evidence(self):
        """验证弱证据（无 sha256）处理"""
        evidence = [
            {"uri": "memory://refs/ref1", "sha256": ""},
            {"uri": "memory://refs/ref2"},
        ]
        summary = compute_evidence_summary(evidence)
        
        assert summary["count"] == 2
        assert summary["has_strong"] is False
        assert len(summary["uris"]) == 2

    def test_strong_evidence(self):
        """验证强证据（有 sha256）处理"""
        evidence = [
            {"uri": "svn://repo/path", "sha256": "abc123def456"},
        ]
        summary = compute_evidence_summary(evidence)
        
        assert summary["count"] == 1
        assert summary["has_strong"] is True

    def test_uri_limit(self):
        """验证 URI 列表最多取 5 个"""
        evidence = [
            {"uri": f"memory://refs/ref{i}"} for i in range(10)
        ]
        summary = compute_evidence_summary(evidence)
        
        assert summary["count"] == 10
        assert len(summary["uris"]) == 5  # 最多 5 个


class TestBuildGatewayAuditEvent:
    """build_gateway_audit_event 函数测试"""

    def test_source_is_gateway(self):
        """验证 source 自动设置为 gateway"""
        event = build_gateway_audit_event(
            operation="memory_store",
        )
        
        assert event["source"] == "gateway"
        assert event["schema_version"] == AUDIT_EVENT_SCHEMA_VERSION
        assert event["correlation_id"] is not None


class TestBuildOutboxWorkerAuditEvent:
    """build_outbox_worker_audit_event 函数测试"""

    def test_source_is_outbox_worker(self):
        """验证 source 自动设置为 outbox_worker"""
        event = build_outbox_worker_audit_event(
            operation="outbox_flush",
        )
        
        assert event["source"] == "outbox_worker"
        assert event["schema_version"] == AUDIT_EVENT_SCHEMA_VERSION
        assert event["correlation_id"] is not None

    def test_worker_and_attempt_id_in_extra(self):
        """验证 worker_id 和 attempt_id 放入 extra"""
        event = build_outbox_worker_audit_event(
            operation="outbox_flush",
            worker_id="worker-001",
            attempt_id="attempt-abc",
        )
        
        assert event["extra"]["worker_id"] == "worker-001"
        assert event["extra"]["attempt_id"] == "attempt-abc"


class TestBuildReconcileAuditEvent:
    """build_reconcile_audit_event 函数测试"""

    def test_source_is_reconcile_outbox(self):
        """验证 source 自动设置为 reconcile_outbox"""
        event = build_reconcile_audit_event(
            operation="outbox_reconcile",
        )
        
        assert event["source"] == "reconcile_outbox"
        assert event["schema_version"] == AUDIT_EVENT_SCHEMA_VERSION
        assert event["correlation_id"] is not None

    def test_reconciled_flag_in_extra(self):
        """验证 reconciled 标记自动添加"""
        event = build_reconcile_audit_event(
            operation="outbox_reconcile",
        )
        
        assert event["extra"]["reconciled"] is True

    def test_original_lock_info_in_extra(self):
        """验证原始锁定信息放入 extra"""
        event = build_reconcile_audit_event(
            operation="outbox_reconcile",
            original_locked_by="worker-old",
            original_locked_at="2024-01-01T00:00:00Z",
        )
        
        assert event["extra"]["original_locked_by"] == "worker-old"
        assert event["extra"]["original_locked_at"] == "2024-01-01T00:00:00Z"


class TestGenerateCorrelationId:
    """generate_correlation_id 函数测试"""

    def test_format(self):
        """验证 correlation_id 格式"""
        corr_id = generate_correlation_id()
        assert corr_id.startswith("corr-")
        assert len(corr_id) == 21  # "corr-" + 16 hex chars

    def test_uniqueness(self):
        """验证 correlation_id 唯一性"""
        ids = [generate_correlation_id() for _ in range(100)]
        assert len(set(ids)) == 100  # 全部唯一


class TestIsValidSha256:
    """is_valid_sha256 函数测试"""

    def test_valid_sha256(self):
        """验证合法的 SHA256"""
        valid_sha = "a" * 64
        assert is_valid_sha256(valid_sha) is True
        
        mixed_case = "AbCdEf0123456789" * 4
        assert is_valid_sha256(mixed_case) is True

    def test_invalid_sha256(self):
        """验证非法的 SHA256"""
        # 空值
        assert is_valid_sha256(None) is False
        assert is_valid_sha256("") is False
        
        # 长度不对
        assert is_valid_sha256("a" * 63) is False
        assert is_valid_sha256("a" * 65) is False
        
        # 包含非十六进制字符
        assert is_valid_sha256("g" * 64) is False
        assert is_valid_sha256("!" * 64) is False


class TestClassifyEvidenceUri:
    """classify_evidence_uri 函数测试"""

    def test_patch_blob_uri(self):
        """验证 patch_blobs URI 分类"""
        sha = "a" * 64
        uri = f"memory://patch_blobs/project/abc123/{sha}"
        category, extracted = classify_evidence_uri(uri)
        
        assert category == "patches"
        assert extracted == sha

    def test_patch_blob_uri_with_colon_in_source_id(self):
        """验证包含冒号的 source_id 能正确分类为 patches
        
        Canonical 格式: memory://patch_blobs/<source_type>/<source_id>/<sha256>
        source_id 格式: <repo_id>:<revision/sha>（如 1:abc123）
        """
        sha = "b" * 64
        # source_id 包含冒号（如 1:abc123def）
        uri = f"memory://patch_blobs/git/1:abc123def/{sha}"
        category, extracted = classify_evidence_uri(uri)
        
        assert category == "patches"
        assert extracted == sha

    def test_patch_blob_uri_with_complex_source_id(self):
        """验证复杂 source_id 格式（含冒号、点号）能正确分类"""
        sha = "c" * 64
        # SVN source_id 格式: repo_id:revision
        uri = f"memory://patch_blobs/svn/2:12345/{sha}"
        category, extracted = classify_evidence_uri(uri)
        assert category == "patches"
        assert extracted == sha
        
        # Git source_id 格式: repo_id:commit_sha
        uri = f"memory://patch_blobs/git/1:abc123def456789/{sha}"
        category, extracted = classify_evidence_uri(uri)
        assert category == "patches"
        assert extracted == sha

    def test_attachment_uri(self):
        """验证 attachments URI 分类（符合 Logbook 规范）
        
        与 Logbook parse_attachment_evidence_uri() 对齐：
        - 第二段必须为 int attachment_id
        - 第三段必须为 64hex sha256
        """
        sha = "b" * 64
        # 严格格式: memory://attachments/<int>/<64hex>
        uri = f"memory://attachments/12345/{sha}"
        category, extracted = classify_evidence_uri(uri)
        
        assert category == "attachments"
        assert extracted == sha

    def test_attachment_uri_non_numeric_id_classified_as_external(self):
        """验证非数字 attachment_id 的 URI 降级分类为 external
        
        与 Logbook parse_attachment_evidence_uri() 对齐：attachment_id 必须为整数
        """
        sha = "c" * 64
        # 非数字 attachment_id
        uri = f"memory://attachments/project/{sha}"
        category, extracted = classify_evidence_uri(uri)
        
        assert category == "external"
        assert extracted is None

    def test_attachment_uri_invalid_sha256_classified_as_external(self):
        """验证 sha256 非 64hex 的 URI 降级分类为 external"""
        # sha256 长度不足
        uri = "memory://attachments/12345/short_sha"
        category, extracted = classify_evidence_uri(uri)
        assert category == "external"
        assert extracted is None
        
        # sha256 包含非十六进制字符
        uri = "memory://attachments/12345/" + "g" * 64
        category, extracted = classify_evidence_uri(uri)
        assert category == "external"
        assert extracted is None

    def test_attachment_uri_multi_segment_path_classified_as_external(self):
        """验证多段路径的 attachment URI 降级分类为 external
        
        Logbook 规范仅支持 memory://attachments/<int>/<64hex>，
        多段路径如 memory://attachments/namespace/id/sha256 不符合规范
        """
        sha = "d" * 64
        # 多段路径
        uri = f"memory://attachments/namespace/subfolder/{sha}"
        category, extracted = classify_evidence_uri(uri)
        
        assert category == "external"
        assert extracted is None
        
        # 另一种多段路径
        uri = f"memory://attachments/ns/id/extra/{sha}"
        category, extracted = classify_evidence_uri(uri)
        
        assert category == "external"
        assert extracted is None

    def test_external_uri_git(self):
        """验证 git:// URI 分类为 external"""
        uri = "git://github.com/repo/commit/abc123"
        category, extracted = classify_evidence_uri(uri)
        
        assert category == "external"
        assert extracted is None

    def test_external_uri_https(self):
        """验证 https:// URI 分类为 external"""
        uri = "https://example.com/resource"
        category, extracted = classify_evidence_uri(uri)
        
        assert category == "external"
        assert extracted is None

    def test_external_uri_svn(self):
        """验证 svn:// URI 分类为 external"""
        uri = "svn://repo/trunk/path"
        category, extracted = classify_evidence_uri(uri)
        
        assert category == "external"
        assert extracted is None

    def test_memory_refs_uri(self):
        """验证 memory://refs/ URI 分类为 external"""
        uri = "memory://refs/some_ref"
        category, extracted = classify_evidence_uri(uri)
        
        assert category == "external"
        assert extracted is None

    def test_invalid_sha_in_patch_uri(self):
        """验证 patch_blobs URI 中 sha256 非法时分类为 external"""
        # SHA256 长度不对
        uri = "memory://patch_blobs/project/abc123/short_sha"
        category, extracted = classify_evidence_uri(uri)
        
        assert category == "external"
        assert extracted is None

    def test_empty_uri(self):
        """验证空 URI 处理"""
        category, extracted = classify_evidence_uri("")
        assert category == "external"
        assert extracted is None
        
        category, extracted = classify_evidence_uri(None)
        assert category == "external"
        assert extracted is None


class TestParseAttachmentEvidenceUri:
    """parse_attachment_evidence_uri 函数测试
    
    与 Logbook engram_logbook.uri.parse_attachment_evidence_uri() 对齐：
    - 第二段必须为 int attachment_id
    - 第三段必须为 64hex sha256
    """

    def test_valid_uri(self):
        """验证合法的 attachment URI 解析"""
        sha = "a" * 64
        uri = f"memory://attachments/12345/{sha}"
        result = parse_attachment_evidence_uri(uri)
        
        assert result is not None
        assert result["attachment_id"] == 12345
        assert result["sha256"] == sha

    def test_valid_uri_large_attachment_id(self):
        """验证大数值 attachment_id 解析"""
        sha = "b" * 64
        uri = f"memory://attachments/999999999/{sha}"
        result = parse_attachment_evidence_uri(uri)
        
        assert result is not None
        assert result["attachment_id"] == 999999999
        assert result["sha256"] == sha

    def test_valid_uri_mixed_case_sha256(self):
        """验证大小写混合的 sha256 能正确解析"""
        sha = "AbCdEf0123456789" * 4  # 64 字符
        uri = f"memory://attachments/100/{sha}"
        result = parse_attachment_evidence_uri(uri)
        
        assert result is not None
        assert result["attachment_id"] == 100
        assert result["sha256"] == sha

    def test_non_numeric_attachment_id_returns_none(self):
        """验证非数字 attachment_id 返回 None"""
        sha = "c" * 64
        
        # 字母
        result = parse_attachment_evidence_uri(f"memory://attachments/project/{sha}")
        assert result is None
        
        # 混合数字字母
        result = parse_attachment_evidence_uri(f"memory://attachments/ns123/{sha}")
        assert result is None
        
        # 特殊字符
        result = parse_attachment_evidence_uri(f"memory://attachments/my-ns/{sha}")
        assert result is None

    def test_invalid_sha256_returns_none(self):
        """验证非 64hex sha256 返回 None"""
        # sha256 长度不足
        result = parse_attachment_evidence_uri("memory://attachments/12345/short")
        assert result is None
        
        # sha256 长度超过
        result = parse_attachment_evidence_uri("memory://attachments/12345/" + "a" * 65)
        assert result is None
        
        # sha256 包含非十六进制字符
        result = parse_attachment_evidence_uri("memory://attachments/12345/" + "g" * 64)
        assert result is None
        
        # sha256 包含特殊字符
        result = parse_attachment_evidence_uri("memory://attachments/12345/" + "!" * 64)
        assert result is None

    def test_multi_segment_path_returns_none(self):
        """验证多段路径返回 None
        
        Logbook 规范仅支持 memory://attachments/<int>/<64hex>
        """
        sha = "d" * 64
        
        # 多一段
        result = parse_attachment_evidence_uri(f"memory://attachments/ns/12345/{sha}")
        assert result is None
        
        # 更多段
        result = parse_attachment_evidence_uri(f"memory://attachments/a/b/c/{sha}")
        assert result is None

    def test_non_attachment_scheme_returns_none(self):
        """验证非 attachment scheme 返回 None"""
        sha = "e" * 64
        
        # patch_blobs
        result = parse_attachment_evidence_uri(f"memory://patch_blobs/git/1:abc/{sha}")
        assert result is None
        
        # refs
        result = parse_attachment_evidence_uri(f"memory://refs/some_ref")
        assert result is None
        
        # 非 memory
        result = parse_attachment_evidence_uri(f"https://example.com/{sha}")
        assert result is None

    def test_empty_uri_returns_none(self):
        """验证空 URI 返回 None"""
        result = parse_attachment_evidence_uri("")
        assert result is None

    def test_missing_sha256_segment_returns_none(self):
        """验证缺少 sha256 段返回 None"""
        result = parse_attachment_evidence_uri("memory://attachments/12345")
        assert result is None

    def test_only_attachments_prefix_returns_none(self):
        """验证仅有前缀返回 None"""
        result = parse_attachment_evidence_uri("memory://attachments")
        assert result is None
        result = parse_attachment_evidence_uri("memory://attachments/")
        assert result is None


class TestBuildEvidenceRefsJson:
    """build_evidence_refs_json 函数测试"""

    def test_gateway_event_always_present(self):
        """验证 gateway_event 字段始终存在"""
        gateway_event = build_gateway_audit_event(operation="test_op")
        result = build_evidence_refs_json(
            evidence=None,
            gateway_event=gateway_event,
        )
        
        assert "gateway_event" in result
        assert result["gateway_event"]["source"] == "gateway"
        assert result["gateway_event"]["operation"] == "test_op"

    def test_patches_extraction(self):
        """验证 patches 字段正确提取"""
        sha = "a" * 64
        evidence = [
            {
                "uri": f"memory://patch_blobs/project/item123/{sha}",
                "sha256": sha,
                "svn_rev": 12345,
            }
        ]
        gateway_event = build_gateway_audit_event(operation="memory_store")
        
        result = build_evidence_refs_json(
            evidence=evidence,
            gateway_event=gateway_event,
        )
        
        assert "patches" in result
        assert len(result["patches"]) == 1
        patch = result["patches"][0]
        assert patch["artifact_uri"] == f"memory://patch_blobs/project/item123/{sha}"
        assert patch["sha256"] == sha
        assert patch["source_type"] == "svn"
        assert patch["source_id"] == "12345"

    def test_attachments_extraction(self):
        """验证 attachments 字段正确提取（符合 Logbook 规范）"""
        sha = "b" * 64
        # 严格格式: memory://attachments/<int>/<64hex>
        evidence = [
            {
                "uri": f"memory://attachments/12345/{sha}",
                "sha256": sha,
            }
        ]
        gateway_event = build_gateway_audit_event(operation="memory_store")
        
        result = build_evidence_refs_json(
            evidence=evidence,
            gateway_event=gateway_event,
        )
        
        assert "attachments" in result
        assert len(result["attachments"]) == 1
        attachment = result["attachments"][0]
        assert attachment["artifact_uri"] == f"memory://attachments/12345/{sha}"
        assert attachment["sha256"] == sha

    def test_external_extraction(self):
        """验证 external 字段正确提取"""
        evidence = [
            {
                "uri": "git://github.com/repo/commit/abc",
                "git_commit": "abc123",
            },
            {
                "uri": "https://jira.example.com/browse/ISSUE-123",
            },
        ]
        gateway_event = build_gateway_audit_event(operation="memory_store")
        
        result = build_evidence_refs_json(
            evidence=evidence,
            gateway_event=gateway_event,
        )
        
        assert "external" in result
        assert len(result["external"]) == 2
        assert result["external"][0]["uri"] == "git://github.com/repo/commit/abc"
        assert result["external"][0]["git_commit"] == "abc123"
        assert result["external"][1]["uri"] == "https://jira.example.com/browse/ISSUE-123"

    def test_mixed_evidence_types(self):
        """验证混合类型证据正确分类"""
        patch_sha = "a" * 64
        attach_sha = "b" * 64
        evidence = [
            {"uri": f"memory://patch_blobs/proj/{patch_sha}", "sha256": patch_sha},
            # 严格格式: memory://attachments/<int>/<64hex>
            {"uri": f"memory://attachments/12345/{attach_sha}", "sha256": attach_sha},
            {"uri": "https://example.com/doc"},
            {"uri": "memory://refs/weak_ref"},
        ]
        gateway_event = build_gateway_audit_event(operation="memory_store")
        
        result = build_evidence_refs_json(
            evidence=evidence,
            gateway_event=gateway_event,
        )
        
        assert "patches" in result
        assert "attachments" in result
        assert "external" in result
        assert "gateway_event" in result
        
        assert len(result["patches"]) == 1
        assert len(result["attachments"]) == 1
        assert len(result["external"]) == 2

    def test_empty_evidence(self):
        """验证空证据处理"""
        gateway_event = build_gateway_audit_event(operation="memory_store")
        
        result = build_evidence_refs_json(
            evidence=None,
            gateway_event=gateway_event,
        )
        
        # 空列表不应出现在结果中
        assert "patches" not in result
        assert "attachments" not in result
        assert "external" not in result
        assert "gateway_event" in result

    def test_patches_source_type_detection(self):
        """验证 patches 的 source_type 自动检测"""
        sha = "c" * 64
        
        # SVN 来源
        ev_svn = [{"uri": f"memory://patch_blobs/p/{sha}", "svn_rev": 100}]
        result = build_evidence_refs_json(ev_svn, build_gateway_audit_event(operation="test"))
        assert result["patches"][0]["source_type"] == "svn"
        assert result["patches"][0]["source_id"] == "100"
        
        # Git 来源
        ev_git = [{"uri": f"memory://patch_blobs/p/{sha}", "git_commit": "abc123def"}]
        result = build_evidence_refs_json(ev_git, build_gateway_audit_event(operation="test"))
        assert result["patches"][0]["source_type"] == "git"
        assert result["patches"][0]["source_id"] == "abc123def"
        
        # MR 来源
        ev_mr = [{"uri": f"memory://patch_blobs/p/{sha}", "mr": 456}]
        result = build_evidence_refs_json(ev_mr, build_gateway_audit_event(operation="test"))
        assert result["patches"][0]["source_type"] == "mr"
        assert result["patches"][0]["source_id"] == "456"

    def test_no_pollution_between_fields(self):
        """验证 patches/attachments/external 与 gateway_event 相互独立不污染"""
        sha = "d" * 64
        evidence = [{"uri": f"memory://patch_blobs/p/{sha}", "sha256": sha}]
        gateway_event = build_gateway_audit_event(
            operation="memory_store",
            correlation_id="corr-test123",
            actor_user_id="user1",
        )
        
        result = build_evidence_refs_json(evidence, gateway_event)
        
        # patches 不应包含 gateway_event 的字段
        patch = result["patches"][0]
        assert "correlation_id" not in patch
        assert "actor_user_id" not in patch
        assert "source" not in patch
        
        # gateway_event 不应包含 patches 的字段
        gw = result["gateway_event"]
        assert "artifact_uri" not in gw
        # gateway_event 应保留其原有字段
        assert gw["correlation_id"] == "corr-test123"
        assert gw["actor_user_id"] == "user1"

    def test_toplevel_evidence_summary_from_gateway_event(self):
        """验证顶层 evidence_summary 从 gateway_event 复制（保持各路径审计字段一致）"""
        sha = "e" * 64
        evidence = [
            {"uri": f"git://github.com/repo/commit/abc", "sha256": sha},
            {"uri": "https://jira.example.com/ISSUE-1"},
        ]
        gateway_event = build_gateway_audit_event(
            operation="memory_store",
            evidence=evidence,
        )
        
        result = build_evidence_refs_json(evidence, gateway_event)
        
        # 顶层应有 evidence_summary
        assert "evidence_summary" in result
        summary = result["evidence_summary"]
        
        # 应与 gateway_event.evidence_summary 一致
        assert summary == gateway_event["evidence_summary"]
        
        # 验证 summary 结构正确
        assert summary["count"] == 2
        assert summary["has_strong"] is True  # 有 sha256 的算强证据
        assert len(summary["uris"]) == 2

    def test_toplevel_evidence_summary_empty_evidence(self):
        """验证无证据时顶层 evidence_summary 也存在"""
        gateway_event = build_gateway_audit_event(
            operation="memory_store",
            evidence=None,
        )
        
        result = build_evidence_refs_json(None, gateway_event)
        
        # 顶层应有 evidence_summary
        assert "evidence_summary" in result
        summary = result["evidence_summary"]
        
        # 空证据时的默认值
        assert summary["count"] == 0
        assert summary["has_strong"] is False
        assert summary["uris"] == []


class TestGatewayEventPolicySubstructure:
    """v1.1 新增: gateway_event.policy 子结构测试"""

    def test_policy_substructure_created_when_fields_provided(self):
        """验证提供 policy 字段时创建 policy 子结构"""
        event = build_gateway_audit_event(
            operation="memory_store",
            policy_mode="strict",
            policy_mode_reason="explicit_header",
            policy_version="v2",
            policy_is_pointerized=True,
            policy_source="settings",
        )
        
        assert "policy" in event
        policy = event["policy"]
        assert policy["mode"] == "strict"
        assert policy["mode_reason"] == "explicit_header"
        assert policy["policy_version"] == "v2"
        assert policy["is_pointerized"] is True
        assert policy["policy_source"] == "settings"

    def test_policy_substructure_not_created_when_no_fields(self):
        """验证不提供 policy 字段时不创建 policy 子结构"""
        event = build_gateway_audit_event(
            operation="memory_store",
        )
        
        # 当没有提供任何 policy 字段时，不创建 policy 子结构
        assert "policy" not in event

    def test_policy_substructure_partial_fields(self):
        """验证部分提供 policy 字段时也创建 policy 子结构"""
        event = build_gateway_audit_event(
            operation="memory_store",
            policy_mode="compat",
            policy_mode_reason="default",
        )
        
        assert "policy" in event
        policy = event["policy"]
        assert policy["mode"] == "compat"
        assert policy["mode_reason"] == "default"
        # 其他字段使用默认值
        assert policy["policy_version"] is None
        assert policy["is_pointerized"] is False
        assert policy["policy_source"] is None

    def test_policy_mode_values(self):
        """验证 policy.mode 支持 strict 和 compat 值"""
        for mode in ["strict", "compat"]:
            event = build_gateway_audit_event(
                operation="memory_store",
                policy_mode=mode,
            )
            assert event["policy"]["mode"] == mode

    def test_policy_source_values(self):
        """验证 policy.policy_source 支持各种来源值"""
        for source in ["settings", "default", "override", "dedup"]:
            event = build_gateway_audit_event(
                operation="memory_store",
                policy_source=source,
            )
            assert event["policy"]["policy_source"] == source


class TestGatewayEventValidationSubstructure:
    """v1.1 新增: gateway_event.validation 子结构测试"""

    def test_validation_substructure_created_when_fields_provided(self):
        """验证提供 validation 字段时创建 validation 子结构"""
        evidence_validation = {
            "is_valid": True,
            "error_code": None,
            "has_evidence": True,
            "evidence_count": 2,
        }
        event = build_gateway_audit_event(
            operation="memory_store",
            validate_refs_effective=True,
            validate_refs_reason="strict_mode_default",
            evidence_validation=evidence_validation,
        )
        
        assert "validation" in event
        validation = event["validation"]
        assert validation["validate_refs_effective"] is True
        assert validation["validate_refs_reason"] == "strict_mode_default"
        assert validation["evidence_validation"] == evidence_validation

    def test_validation_substructure_not_created_when_no_fields(self):
        """验证不提供 validation 字段时不创建 validation 子结构"""
        event = build_gateway_audit_event(
            operation="memory_store",
        )
        
        # 当没有提供任何 validation 字段时，不创建 validation 子结构
        assert "validation" not in event

    def test_validation_with_only_validate_refs_effective(self):
        """验证仅提供 validate_refs_effective 时也创建 validation 子结构"""
        event = build_gateway_audit_event(
            operation="memory_store",
            validate_refs_effective=False,
            validate_refs_reason="config_default",
        )
        
        assert "validation" in event
        validation = event["validation"]
        assert validation["validate_refs_effective"] is False
        assert validation["validate_refs_reason"] == "config_default"
        assert validation["evidence_validation"] is None

    def test_validation_with_evidence_validation_details(self):
        """验证 evidence_validation 详情正确记录"""
        evidence_validation = {
            "mode": "strict",
            "error_code": "EVIDENCE_MISSING_SHA256",
            "has_evidence": True,
            "evidence_count": 1,
            "failed_field": "sha256",
            "failed_uris": ["memory://patch_blobs/p/abc"],
        }
        event = build_gateway_audit_event(
            operation="memory_store",
            validate_refs_effective=True,
            validate_refs_reason="strict_mode",
            evidence_validation=evidence_validation,
        )
        
        validation = event["validation"]
        assert validation["evidence_validation"]["error_code"] == "EVIDENCE_MISSING_SHA256"
        assert validation["evidence_validation"]["failed_field"] == "sha256"


class TestSchemaVersionBackwardCompatibility:
    """schema_version 向后兼容性测试"""

    def test_schema_version_is_1_1(self):
        """验证当前 schema_version 为 1.1"""
        assert AUDIT_EVENT_SCHEMA_VERSION == "1.1"

    def test_schema_version_minor_change_only(self):
        """验证 schema_version 只做次版本扩展（1.0 -> 1.1）"""
        # 主版本号保持为 1
        assert AUDIT_EVENT_SCHEMA_VERSION.startswith("1.")
        # 次版本号 >= 1（表示有新增字段）
        minor_version = int(AUDIT_EVENT_SCHEMA_VERSION.split(".")[1])
        assert minor_version >= 1

    def test_old_fields_still_present(self):
        """验证旧字段仍然存在（向后兼容）"""
        event = build_gateway_audit_event(
            operation="memory_store",
            actor_user_id="user1",
            requested_space="team:project",
            final_space="team:project",
            action="allow",
            reason="policy:ok",
            payload_sha="abc123",
            evidence_refs=["ref1", "ref2"],
        )
        
        # v1.0 核心字段仍存在
        assert "schema_version" in event
        assert "source" in event
        assert "operation" in event
        assert "correlation_id" in event
        assert "actor_user_id" in event
        assert "requested_space" in event
        assert "final_space" in event
        assert "decision" in event
        assert "payload_sha" in event
        assert "evidence_summary" in event
        assert "trim" in event
        assert "refs" in event  # 兼容旧字段
        assert "event_ts" in event

    def test_new_fields_optional(self):
        """验证新增字段为可选（不提供时不创建子结构）"""
        # 不提供 v1.1 新增字段
        event = build_gateway_audit_event(
            operation="memory_store",
            actor_user_id="user1",
        )
        
        # 新字段为可选，不提供时子结构不存在
        # policy 和 validation 只有在至少提供一个字段时才创建
        assert "policy" not in event
        assert "validation" not in event
        
        # 但顶层兼容字段 policy_mode 可以单独存在（如果提供了）
        event_with_mode = build_gateway_audit_event(
            operation="memory_store",
            policy_mode="strict",
        )
        assert "policy_mode" in event_with_mode
        assert event_with_mode["policy_mode"] == "strict"


class TestEvidenceRefsJsonWithPolicyValidation:
    """evidence_refs_json 中 gateway_event.policy 和 gateway_event.validation 测试"""

    def test_evidence_refs_json_contains_policy_substructure(self):
        """验证 evidence_refs_json 中的 gateway_event 包含 policy 子结构"""
        gateway_event = build_gateway_audit_event(
            operation="memory_store",
            policy_mode="strict",
            policy_mode_reason="explicit",
            policy_version="v2",
            policy_is_pointerized=False,
            policy_source="settings",
            validate_refs_effective=True,
            validate_refs_reason="strict_mode",
        )
        
        result = build_evidence_refs_json(
            evidence=None,
            gateway_event=gateway_event,
        )
        
        # gateway_event 中包含 policy 子结构
        assert "gateway_event" in result
        gw = result["gateway_event"]
        assert "policy" in gw
        assert gw["policy"]["mode"] == "strict"
        assert gw["policy"]["mode_reason"] == "explicit"
        assert gw["policy"]["policy_version"] == "v2"
        assert gw["policy"]["is_pointerized"] is False
        assert gw["policy"]["policy_source"] == "settings"

    def test_evidence_refs_json_contains_validation_substructure(self):
        """验证 evidence_refs_json 中的 gateway_event 包含 validation 子结构"""
        evidence_validation = {
            "is_valid": True,
            "error_code": None,
        }
        gateway_event = build_gateway_audit_event(
            operation="memory_store",
            validate_refs_effective=True,
            validate_refs_reason="strict_mode",
            evidence_validation=evidence_validation,
        )
        
        result = build_evidence_refs_json(
            evidence=None,
            gateway_event=gateway_event,
        )
        
        # gateway_event 中包含 validation 子结构
        assert "gateway_event" in result
        gw = result["gateway_event"]
        assert "validation" in gw
        assert gw["validation"]["validate_refs_effective"] is True
        assert gw["validation"]["validate_refs_reason"] == "strict_mode"
        assert gw["validation"]["evidence_validation"] == evidence_validation

    def test_evidence_refs_json_required_fields_for_audit(self):
        """
        验证写入审计时 evidence_refs_json 必须包含完整结构
        
        当使用 v1.1 功能时，gateway_event 应包含：
        - schema_version: "1.1"
        - policy 子结构（如果提供了相关字段）
        - validation 子结构（如果提供了相关字段）
        """
        sha = "a" * 64
        evidence = [
            {"uri": f"memory://patch_blobs/project/{sha}", "sha256": sha}
        ]
        evidence_validation = {
            "is_valid": True,
            "mode": "strict",
            "error_code": None,
        }
        
        gateway_event = build_gateway_audit_event(
            operation="memory_store",
            actor_user_id="user1",
            requested_space="team:project",
            final_space="team:project",
            action="allow",
            reason="policy_passed",
            payload_sha="abc123",
            evidence=evidence,
            # v1.1 policy 子结构
            policy_mode="strict",
            policy_mode_reason="explicit_header",
            policy_version="v2",
            policy_is_pointerized=False,
            policy_source="settings",
            # v1.1 validation 子结构
            validate_refs_effective=True,
            validate_refs_reason="strict_mode",
            evidence_validation=evidence_validation,
        )
        
        result = build_evidence_refs_json(
            evidence=evidence,
            gateway_event=gateway_event,
        )
        
        # 验证完整结构
        assert "gateway_event" in result
        gw = result["gateway_event"]
        
        # schema_version
        assert gw["schema_version"] == "1.1"
        
        # policy 子结构
        assert "policy" in gw
        assert gw["policy"]["mode"] == "strict"
        assert gw["policy"]["mode_reason"] == "explicit_header"
        assert gw["policy"]["policy_version"] == "v2"
        assert gw["policy"]["is_pointerized"] is False
        assert gw["policy"]["policy_source"] == "settings"
        
        # validation 子结构
        assert "validation" in gw
        assert gw["validation"]["validate_refs_effective"] is True
        assert gw["validation"]["validate_refs_reason"] == "strict_mode"
        assert gw["validation"]["evidence_validation"]["is_valid"] is True
        
        # patches 也应正确提取
        assert "patches" in result
        assert len(result["patches"]) == 1


# ============================================================================
# Schema 校验测试：验证生成的审计事件符合 audit_event_v1.schema.json
# ============================================================================

class TestAuditEventSchemaValidation:
    """审计事件 JSON Schema 校验测试
    
    重点校验：
    - schema_version 格式（X.Y）
    - correlation_id 格式（corr-{16hex}）
    - decision 结构（action/reason）
    - payload_sha/payload_len
    - evidence_summary 结构
    - event_ts ISO8601 格式
    """

    @pytest.fixture(scope="class")
    def schema(self):
        """加载 schema 文件"""
        return _load_schema()

    def test_schema_file_exists(self, schema):
        """验证 schema 文件存在且可解析"""
        assert "$schema" in schema
        assert "definitions" in schema
        assert "audit_event" in schema["definitions"]
        assert "evidence_refs_json" in schema["definitions"]

    # ------------------------------------------------------------------------
    # 决策场景：allow / redirect / reject
    # ------------------------------------------------------------------------

    def test_allow_decision_with_evidence(self, schema):
        """场景：allow 决策 + 包含 evidence"""
        sha = "a" * 64
        evidence = [
            {"uri": f"memory://patch_blobs/git/1:abc/{sha}", "sha256": sha, "git_commit": "abc123"},
        ]
        event = build_gateway_audit_event(
            operation="memory_store",
            correlation_id="corr-1234567890abcdef",
            actor_user_id="user1",
            requested_space="team:project",
            final_space="team:project",
            action="allow",
            reason="policy_passed",
            payload_sha="contentsha256hash",
            payload_len=1024,
            evidence=evidence,
            policy_mode="strict",
            policy_mode_reason="explicit_header",
            policy_version="v2",
            policy_is_pointerized=False,
            policy_source="settings",
            validate_refs_effective=True,
            validate_refs_reason="strict_mode",
            evidence_validation={"is_valid": True, "error_code": None},
        )
        
        # 校验审计事件
        _validate_audit_event(event, schema)
        
        # 校验关键字段
        assert event["schema_version"] == "1.1"
        assert re.match(r"^corr-[a-fA-F0-9]{16}$", event["correlation_id"])
        assert event["decision"]["action"] == "allow"
        assert event["decision"]["reason"] == "policy_passed"
        assert event["payload_len"] == 1024
        assert event["evidence_summary"]["count"] == 1
        assert event["evidence_summary"]["has_strong"] is True
        # event_ts ISO8601 格式
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", event["event_ts"])

    def test_allow_decision_without_evidence(self, schema):
        """场景：allow 决策 + 无 evidence"""
        event = build_gateway_audit_event(
            operation="memory_store",
            correlation_id="corr-abcdef1234567890",
            actor_user_id="user2",
            requested_space="personal",
            final_space="personal",
            action="allow",
            reason="policy_passed",
            payload_sha=None,
            payload_len=512,
            evidence=None,
            policy_mode="compat",
            policy_mode_reason="default",
        )
        
        _validate_audit_event(event, schema)
        
        assert event["decision"]["action"] == "allow"
        assert event["evidence_summary"]["count"] == 0
        assert event["evidence_summary"]["has_strong"] is False
        assert event["evidence_summary"]["uris"] == []

    def test_redirect_decision_with_evidence(self, schema):
        """场景：redirect 决策 + 包含 evidence"""
        sha = "b" * 64
        evidence = [
            # 严格格式: memory://attachments/<int>/<64hex>
            {"uri": f"memory://attachments/67890/{sha}", "sha256": sha},
            {"uri": "https://jira.example.com/ISSUE-123"},
        ]
        event = build_gateway_audit_event(
            operation="memory_store",
            correlation_id="corr-fedcba9876543210",
            actor_user_id="user3",
            requested_space="team:restricted",
            final_space="team:fallback",
            action="redirect",
            reason="team_write_disabled",
            payload_sha="xyz789",
            payload_len=256,
            evidence=evidence,
            policy_mode="compat",
            policy_mode_reason="legacy_client",
            policy_version="v1",
            policy_is_pointerized=False,
            policy_source="default",
        )
        
        _validate_audit_event(event, schema)
        
        assert event["decision"]["action"] == "redirect"
        assert event["decision"]["reason"] == "team_write_disabled"
        assert event["requested_space"] == "team:restricted"
        assert event["final_space"] == "team:fallback"
        assert event["evidence_summary"]["count"] == 2
        assert event["evidence_summary"]["has_strong"] is True

    def test_redirect_decision_without_evidence(self, schema):
        """场景：redirect 决策 + 无 evidence"""
        event = build_gateway_audit_event(
            operation="memory_store",
            correlation_id="corr-0000111122223333",
            actor_user_id="user4",
            requested_space="personal",
            final_space="team:default",
            action="redirect",
            reason="personal_space_disabled",
            payload_sha=None,
            payload_len=100,
            evidence=None,
        )
        
        _validate_audit_event(event, schema)
        
        assert event["decision"]["action"] == "redirect"
        assert event["evidence_summary"]["count"] == 0

    def test_reject_decision_with_evidence(self, schema):
        """场景：reject 决策 + 包含 evidence（证据校验失败）"""
        evidence = [
            {"uri": "memory://refs/weak_ref"},  # 无 sha256 的弱证据
        ]
        event = build_gateway_audit_event(
            operation="memory_store",
            correlation_id="corr-4444555566667777",
            actor_user_id="user5",
            requested_space="team:strict",
            final_space=None,
            action="reject",
            reason="EVIDENCE_MISSING_SHA256",
            payload_sha="abc123",
            payload_len=200,
            evidence=evidence,
            policy_mode="strict",
            policy_mode_reason="team_strict_mode",
            policy_version="v2",
            policy_is_pointerized=False,
            policy_source="settings",
            validate_refs_effective=True,
            validate_refs_reason="strict_mode",
            evidence_validation={
                "is_valid": False,
                "error_code": "EVIDENCE_MISSING_SHA256",
                "has_evidence": True,
                "evidence_count": 1,
                "failed_field": "sha256",
                "failed_uris": ["memory://refs/weak_ref"],
            },
        )
        
        _validate_audit_event(event, schema)
        
        assert event["decision"]["action"] == "reject"
        assert event["decision"]["reason"] == "EVIDENCE_MISSING_SHA256"
        assert event["final_space"] is None
        assert event["evidence_summary"]["has_strong"] is False
        assert event["validation"]["evidence_validation"]["is_valid"] is False

    def test_reject_decision_without_evidence(self, schema):
        """场景：reject 决策 + 无 evidence（缺少必要证据）"""
        event = build_gateway_audit_event(
            operation="memory_store",
            correlation_id="corr-8888999900001111",
            actor_user_id="user6",
            requested_space="team:strict",
            final_space=None,
            action="reject",
            reason="missing_evidence",
            payload_sha="def456",
            payload_len=150,
            evidence=None,
            policy_mode="strict",
            policy_mode_reason="team_strict_mode",
            validate_refs_effective=True,
            validate_refs_reason="strict_mode",
            evidence_validation={
                "is_valid": False,
                "error_code": "EVIDENCE_EMPTY",
                "has_evidence": False,
                "evidence_count": 0,
            },
        )
        
        _validate_audit_event(event, schema)
        
        assert event["decision"]["action"] == "reject"
        assert event["decision"]["reason"] == "missing_evidence"
        assert event["evidence_summary"]["count"] == 0

    def test_reject_user_not_in_allowlist(self, schema):
        """场景：reject 决策 - 用户不在白名单"""
        event = build_gateway_audit_event(
            operation="memory_store",
            correlation_id="corr-aaaabbbbccccdddd",
            actor_user_id="unauthorized_user",
            requested_space="team:restricted",
            final_space=None,
            action="reject",
            reason="user_not_in_allowlist",
            payload_sha="ghi789",
            payload_len=300,
            evidence=None,
        )
        
        _validate_audit_event(event, schema)
        
        assert event["decision"]["action"] == "reject"
        assert event["decision"]["reason"] == "user_not_in_allowlist"

    # ------------------------------------------------------------------------
    # correlation_id 格式校验
    # ------------------------------------------------------------------------

    def test_correlation_id_format_valid(self, schema):
        """验证 correlation_id 格式正确（corr-{16hex}）"""
        event = build_gateway_audit_event(
            operation="test_op",
            correlation_id="corr-0123456789abcdef",
        )
        
        _validate_audit_event(event, schema)
        assert re.match(r"^corr-[a-fA-F0-9]{16}$", event["correlation_id"])

    def test_correlation_id_auto_generated(self, schema):
        """验证自动生成的 correlation_id 格式正确"""
        event = build_gateway_audit_event(
            operation="test_op",
            # 不提供 correlation_id，让系统自动生成
        )
        
        _validate_audit_event(event, schema)
        assert re.match(r"^corr-[a-fA-F0-9]{16}$", event["correlation_id"])

    # ------------------------------------------------------------------------
    # schema_version 格式校验
    # ------------------------------------------------------------------------

    def test_schema_version_format(self, schema):
        """验证 schema_version 格式正确（X.Y）"""
        event = build_gateway_audit_event(operation="test_op")
        
        _validate_audit_event(event, schema)
        assert re.match(r"^\d+\.\d+$", event["schema_version"])
        assert event["schema_version"] == AUDIT_EVENT_SCHEMA_VERSION

    # ------------------------------------------------------------------------
    # event_ts ISO8601 格式校验
    # ------------------------------------------------------------------------

    def test_event_ts_iso8601_format(self, schema):
        """验证 event_ts 是 ISO8601 格式"""
        event = build_gateway_audit_event(operation="test_op")
        
        _validate_audit_event(event, schema)
        # ISO8601 格式: YYYY-MM-DDTHH:MM:SS.ffffff+HH:MM 或 Z
        assert re.match(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$",
            event["event_ts"]
        )

    # ------------------------------------------------------------------------
    # evidence_summary 结构校验
    # ------------------------------------------------------------------------

    def test_evidence_summary_structure(self, schema):
        """验证 evidence_summary 包含必要字段"""
        sha = "c" * 64
        evidence = [
            {"uri": f"memory://patch_blobs/svn/1:123/{sha}", "sha256": sha},
        ]
        event = build_gateway_audit_event(
            operation="memory_store",
            evidence=evidence,
        )
        
        _validate_audit_event(event, schema)
        
        summary = event["evidence_summary"]
        assert "count" in summary
        assert "has_strong" in summary
        assert "uris" in summary
        assert isinstance(summary["count"], int)
        assert isinstance(summary["has_strong"], bool)
        assert isinstance(summary["uris"], list)

    def test_evidence_summary_uris_max_5(self, schema):
        """验证 evidence_summary.uris 最多 5 个"""
        sha = "d" * 64
        evidence = [
            {"uri": f"memory://refs/ref{i}", "sha256": sha if i == 0 else ""} 
            for i in range(10)
        ]
        event = build_gateway_audit_event(
            operation="memory_store",
            evidence=evidence,
        )
        
        _validate_audit_event(event, schema)
        
        assert event["evidence_summary"]["count"] == 10
        assert len(event["evidence_summary"]["uris"]) <= 5

    # ------------------------------------------------------------------------
    # payload_sha / payload_len 校验
    # ------------------------------------------------------------------------

    def test_payload_sha_and_len(self, schema):
        """验证 payload_sha 和 payload_len 字段"""
        event = build_gateway_audit_event(
            operation="memory_store",
            payload_sha="a" * 64,
            payload_len=2048,
        )
        
        _validate_audit_event(event, schema)
        
        assert event["payload_sha"] == "a" * 64
        assert event["payload_len"] == 2048

    def test_payload_sha_null(self, schema):
        """验证 payload_sha 可以为 null"""
        event = build_gateway_audit_event(
            operation="memory_store",
            payload_sha=None,
            payload_len=100,
        )
        
        _validate_audit_event(event, schema)
        
        assert event["payload_sha"] is None
        assert event["payload_len"] == 100

    # ------------------------------------------------------------------------
    # trim 结构校验
    # ------------------------------------------------------------------------

    def test_trim_structure(self, schema):
        """验证 trim 结构正确"""
        event = build_gateway_audit_event(
            operation="memory_store",
            trim_was_trimmed=True,
            trim_why="payload_too_large",
            trim_original_len=50000,
        )
        
        _validate_audit_event(event, schema)
        
        assert event["trim"]["was_trimmed"] is True
        assert event["trim"]["why"] == "payload_too_large"
        assert event["trim"]["original_len"] == 50000

    def test_trim_not_trimmed(self, schema):
        """验证未裁剪时的 trim 结构"""
        event = build_gateway_audit_event(operation="memory_store")
        
        _validate_audit_event(event, schema)
        
        assert event["trim"]["was_trimmed"] is False
        assert event["trim"]["why"] is None
        assert event["trim"]["original_len"] is None


class TestEvidenceRefsJsonSchemaValidation:
    """evidence_refs_json 结构的 JSON Schema 校验测试"""

    @pytest.fixture(scope="class")
    def schema(self):
        """加载 schema 文件"""
        return _load_schema()

    def test_evidence_refs_json_minimal(self, schema):
        """最小 evidence_refs_json 结构（仅 gateway_event）"""
        gateway_event = build_gateway_audit_event(
            operation="memory_store",
            correlation_id="corr-1111222233334444",
        )
        result = build_evidence_refs_json(
            evidence=None,
            gateway_event=gateway_event,
        )
        
        _validate_evidence_refs_json(result, schema)
        
        assert "gateway_event" in result
        assert "patches" not in result
        assert "attachments" not in result
        assert "external" not in result

    def test_evidence_refs_json_with_patches(self, schema):
        """evidence_refs_json 包含 patches"""
        sha = "e" * 64
        evidence = [
            {
                "uri": f"memory://patch_blobs/git/1:commit123/{sha}",
                "sha256": sha,
                "git_commit": "commit123",
            }
        ]
        gateway_event = build_gateway_audit_event(
            operation="memory_store",
            correlation_id="corr-5555666677778888",
            evidence=evidence,
        )
        result = build_evidence_refs_json(
            evidence=evidence,
            gateway_event=gateway_event,
        )
        
        _validate_evidence_refs_json(result, schema)
        
        assert "patches" in result
        assert len(result["patches"]) == 1
        patch = result["patches"][0]
        assert patch["artifact_uri"] == f"memory://patch_blobs/git/1:commit123/{sha}"
        assert patch["sha256"] == sha

    def test_evidence_refs_json_with_attachments(self, schema):
        """evidence_refs_json 包含 attachments（符合 Logbook 规范）"""
        sha = "f" * 64
        # 严格格式: memory://attachments/<int>/<64hex>
        evidence = [
            {
                "uri": f"memory://attachments/99999/{sha}",
                "sha256": sha,
            }
        ]
        gateway_event = build_gateway_audit_event(
            operation="memory_store",
            correlation_id="corr-9999000011112222",
            evidence=evidence,
        )
        result = build_evidence_refs_json(
            evidence=evidence,
            gateway_event=gateway_event,
        )
        
        _validate_evidence_refs_json(result, schema)
        
        assert "attachments" in result
        assert len(result["attachments"]) == 1
        attachment = result["attachments"][0]
        assert attachment["artifact_uri"] == f"memory://attachments/99999/{sha}"
        assert attachment["sha256"] == sha

    def test_evidence_refs_json_with_external(self, schema):
        """evidence_refs_json 包含 external"""
        evidence = [
            {"uri": "git://github.com/repo/commit/abc123", "git_commit": "abc123"},
            {"uri": "https://jira.example.com/ISSUE-456"},
            {"uri": "svn://repo/trunk@12345", "svn_rev": 12345},
        ]
        gateway_event = build_gateway_audit_event(
            operation="memory_store",
            correlation_id="corr-3333444455556666",
            evidence=evidence,
        )
        result = build_evidence_refs_json(
            evidence=evidence,
            gateway_event=gateway_event,
        )
        
        _validate_evidence_refs_json(result, schema)
        
        assert "external" in result
        assert len(result["external"]) == 3

    def test_evidence_refs_json_mixed_types(self, schema):
        """evidence_refs_json 包含混合类型证据"""
        patch_sha = "a" * 64
        attach_sha = "b" * 64
        evidence = [
            {"uri": f"memory://patch_blobs/svn/2:999/{patch_sha}", "sha256": patch_sha, "svn_rev": 999},
            # 严格格式: memory://attachments/<int>/<64hex>
            {"uri": f"memory://attachments/88888/{attach_sha}", "sha256": attach_sha},
            {"uri": "https://example.com/doc"},
            {"uri": "memory://refs/weak_ref"},
        ]
        gateway_event = build_gateway_audit_event(
            operation="memory_store",
            correlation_id="corr-7777888899990000",
            actor_user_id="user_mixed",
            evidence=evidence,
            policy_mode="strict",
            policy_mode_reason="test",
            policy_version="v2",
            policy_is_pointerized=False,
            policy_source="settings",
            validate_refs_effective=True,
            validate_refs_reason="strict_mode",
            evidence_validation={"is_valid": True, "error_code": None},
        )
        result = build_evidence_refs_json(
            evidence=evidence,
            gateway_event=gateway_event,
        )
        
        _validate_evidence_refs_json(result, schema)
        
        assert "patches" in result
        assert "attachments" in result
        assert "external" in result
        assert len(result["patches"]) == 1
        assert len(result["attachments"]) == 1
        assert len(result["external"]) == 2
        
        # 验证 evidence_summary 从 gateway_event 复制
        assert "evidence_summary" in result
        assert result["evidence_summary"]["count"] == 4

    def test_evidence_refs_json_gateway_event_policy_validation(self, schema):
        """验证 gateway_event 中的 policy 和 validation 子结构"""
        sha = "c" * 64
        evidence = [
            {"uri": f"memory://patch_blobs/git/1:xyz/{sha}", "sha256": sha}
        ]
        gateway_event = build_gateway_audit_event(
            operation="memory_store",
            correlation_id="corr-aaaabbbbccccdddd",
            action="allow",
            reason="policy_passed",
            evidence=evidence,
            # policy 子结构
            policy_mode="strict",
            policy_mode_reason="explicit_header",
            policy_version="v2",
            policy_is_pointerized=False,
            policy_source="settings",
            # validation 子结构
            validate_refs_effective=True,
            validate_refs_reason="strict_mode",
            evidence_validation={
                "is_valid": True,
                "mode": "strict",
                "error_code": None,
                "has_evidence": True,
                "evidence_count": 1,
            },
        )
        result = build_evidence_refs_json(
            evidence=evidence,
            gateway_event=gateway_event,
        )
        
        _validate_evidence_refs_json(result, schema)
        
        gw = result["gateway_event"]
        
        # policy 子结构
        assert "policy" in gw
        assert gw["policy"]["mode"] == "strict"
        assert gw["policy"]["mode_reason"] == "explicit_header"
        assert gw["policy"]["policy_version"] == "v2"
        assert gw["policy"]["is_pointerized"] is False
        assert gw["policy"]["policy_source"] == "settings"
        
        # validation 子结构
        assert "validation" in gw
        assert gw["validation"]["validate_refs_effective"] is True
        assert gw["validation"]["validate_refs_reason"] == "strict_mode"
        assert gw["validation"]["evidence_validation"]["is_valid"] is True


class TestSchemaValidationEdgeCases:
    """Schema 校验边界情况测试"""

    @pytest.fixture(scope="class")
    def schema(self):
        """加载 schema 文件"""
        return _load_schema()

    def test_outbox_worker_event(self, schema):
        """outbox_worker 来源的审计事件"""
        event = build_outbox_worker_audit_event(
            operation="outbox_flush",
            correlation_id="corr-1234567890abcdef",
            actor_user_id="outbox_worker",
            target_space="team:project",
            action="allow",
            reason="flush_success",
            outbox_id=12345,
            memory_id="mem-abc123",
            retry_count=0,
            worker_id="worker-001",
            attempt_id="attempt-xyz",
        )
        
        _validate_audit_event(event, schema)
        
        assert event["source"] == "outbox_worker"
        assert event["outbox_id"] == 12345
        assert event["memory_id"] == "mem-abc123"
        assert event["extra"]["worker_id"] == "worker-001"

    def test_outbox_worker_evidence_refs_json_success(self, schema):
        """outbox_worker success 场景的 evidence_refs_json 通过 oneOf 校验"""
        gateway_event = build_outbox_worker_audit_event(
            operation="outbox_flush",
            correlation_id="corr-1234567890abcdef",
            actor_user_id="user1",
            target_space="private:user1",
            action="allow",
            reason="outbox_flush_success",
            payload_sha="a" * 64,
            outbox_id=12345,
            memory_id="mem-abc123",
            worker_id="worker-001",
            attempt_id="attempt-xyz",
        )
        evidence_refs_json = build_evidence_refs_json(
            evidence=None,
            gateway_event=gateway_event,
        )
        
        # 验证符合 evidence_refs_json 定义
        _validate_evidence_refs_json(evidence_refs_json, schema)
        
        # 验证 gateway_event 结构正确
        gw = evidence_refs_json["gateway_event"]
        assert gw["source"] == "outbox_worker"
        assert gw["operation"] == "outbox_flush"
        assert gw["outbox_id"] == 12345
        assert gw["memory_id"] == "mem-abc123"
        assert gw["extra"]["worker_id"] == "worker-001"
        assert gw["extra"]["attempt_id"] == "attempt-xyz"

    def test_outbox_worker_evidence_refs_json_retry(self, schema):
        """outbox_worker retry 场景的 evidence_refs_json 通过 oneOf 校验"""
        gateway_event = build_outbox_worker_audit_event(
            operation="outbox_flush",
            correlation_id="corr-fedcba9876543210",
            actor_user_id="user2",
            target_space="team:project",
            action="redirect",
            reason="outbox_flush_retry",
            payload_sha="b" * 64,
            outbox_id=67890,
            retry_count=3,
            next_attempt_at="2024-01-15T12:00:00+00:00",
            worker_id="worker-002",
            attempt_id="attempt-abc",
            extra={
                "last_error": "connection_error: timeout",
            },
        )
        evidence_refs_json = build_evidence_refs_json(
            evidence=None,
            gateway_event=gateway_event,
        )
        
        # 验证符合 evidence_refs_json 定义
        _validate_evidence_refs_json(evidence_refs_json, schema)
        
        # 验证 gateway_event 结构正确
        gw = evidence_refs_json["gateway_event"]
        assert gw["source"] == "outbox_worker"
        assert gw["decision"]["action"] == "redirect"
        assert gw["decision"]["reason"] == "outbox_flush_retry"
        assert gw["outbox_id"] == 67890
        assert gw["retry_count"] == 3
        assert gw["next_attempt_at"] == "2024-01-15T12:00:00+00:00"
        assert gw["extra"]["last_error"] == "connection_error: timeout"

    def test_outbox_worker_evidence_refs_json_dead(self, schema):
        """outbox_worker dead 场景的 evidence_refs_json 通过 oneOf 校验"""
        gateway_event = build_outbox_worker_audit_event(
            operation="outbox_flush",
            correlation_id="corr-0000111122223333",
            actor_user_id="user3",
            target_space="team:restricted",
            action="reject",
            reason="outbox_flush_dead",
            payload_sha="c" * 64,
            outbox_id=11111,
            retry_count=5,
            worker_id="worker-003",
            attempt_id="attempt-def",
            extra={
                "last_error": "api_error: 500 - Internal Server Error",
            },
        )
        evidence_refs_json = build_evidence_refs_json(
            evidence=None,
            gateway_event=gateway_event,
        )
        
        # 验证符合 evidence_refs_json 定义
        _validate_evidence_refs_json(evidence_refs_json, schema)
        
        # 验证 gateway_event 结构正确
        gw = evidence_refs_json["gateway_event"]
        assert gw["source"] == "outbox_worker"
        assert gw["decision"]["action"] == "reject"
        assert gw["decision"]["reason"] == "outbox_flush_dead"
        assert gw["retry_count"] == 5
        assert gw["extra"]["last_error"] == "api_error: 500 - Internal Server Error"

    def test_outbox_worker_evidence_refs_json_conflict(self, schema):
        """outbox_worker conflict 场景的 evidence_refs_json 通过 oneOf 校验"""
        gateway_event = build_outbox_worker_audit_event(
            operation="outbox_flush",
            correlation_id="corr-4444555566667777",
            actor_user_id="user4",
            target_space="private:user4",
            action="redirect",
            reason="outbox_flush_conflict",
            payload_sha="d" * 64,
            outbox_id=22222,
            worker_id="worker-004",
            attempt_id="attempt-ghi",
            extra={
                "intended_action": "success",
                "observed_status": "sent",
                "observed_locked_by": "worker-other",
                "observed_last_error": None,
            },
        )
        evidence_refs_json = build_evidence_refs_json(
            evidence=None,
            gateway_event=gateway_event,
        )
        
        # 验证符合 evidence_refs_json 定义
        _validate_evidence_refs_json(evidence_refs_json, schema)
        
        # 验证 gateway_event 结构正确
        gw = evidence_refs_json["gateway_event"]
        assert gw["source"] == "outbox_worker"
        assert gw["decision"]["action"] == "redirect"
        assert gw["decision"]["reason"] == "outbox_flush_conflict"
        assert gw["extra"]["intended_action"] == "success"
        assert gw["extra"]["observed_status"] == "sent"
        assert gw["extra"]["observed_locked_by"] == "worker-other"

    def test_outbox_worker_evidence_refs_json_db_error(self, schema):
        """outbox_worker db_error 场景的 evidence_refs_json 通过 oneOf 校验"""
        gateway_event = build_outbox_worker_audit_event(
            operation="outbox_flush",
            correlation_id="corr-8888999900001111",
            actor_user_id="user5",
            target_space="team:project",
            action="redirect",
            reason="OUTBOX_FLUSH_DB_TIMEOUT",
            payload_sha="e" * 64,
            outbox_id=33333,
            worker_id="worker-005",
            attempt_id="attempt-jkl",
            extra={
                "error_type": "db_timeout",
                "error_message": "statement timeout",
            },
        )
        evidence_refs_json = build_evidence_refs_json(
            evidence=None,
            gateway_event=gateway_event,
        )
        
        # 验证符合 evidence_refs_json 定义
        _validate_evidence_refs_json(evidence_refs_json, schema)
        
        # 验证 gateway_event 结构正确
        gw = evidence_refs_json["gateway_event"]
        assert gw["source"] == "outbox_worker"
        assert gw["decision"]["reason"] == "OUTBOX_FLUSH_DB_TIMEOUT"
        assert gw["extra"]["error_type"] == "db_timeout"
        assert gw["extra"]["error_message"] == "statement timeout"

    def test_outbox_worker_evidence_refs_json_dedup_hit(self, schema):
        """outbox_worker dedup_hit 场景的 evidence_refs_json 通过 oneOf 校验"""
        gateway_event = build_outbox_worker_audit_event(
            operation="outbox_flush",
            correlation_id="corr-aaaabbbbccccdddd",
            actor_user_id="user6",
            target_space="private:user6",
            action="allow",
            reason="outbox_flush_dedup_hit",
            payload_sha="f" * 64,
            outbox_id=44444,
            memory_id="mem-original",
            worker_id="worker-006",
            attempt_id="attempt-mno",
            extra={
                "original_outbox_id": 44443,
            },
        )
        evidence_refs_json = build_evidence_refs_json(
            evidence=None,
            gateway_event=gateway_event,
        )
        
        # 验证符合 evidence_refs_json 定义
        _validate_evidence_refs_json(evidence_refs_json, schema)
        
        # 验证 gateway_event 结构正确
        gw = evidence_refs_json["gateway_event"]
        assert gw["source"] == "outbox_worker"
        assert gw["decision"]["action"] == "allow"
        assert gw["decision"]["reason"] == "outbox_flush_dedup_hit"
        assert gw["memory_id"] == "mem-original"
        assert gw["extra"]["original_outbox_id"] == 44443

    def test_reconcile_event(self, schema):
        """reconcile_outbox 来源的审计事件"""
        event = build_reconcile_audit_event(
            operation="outbox_reconcile",
            correlation_id="corr-fedcba0987654321",
            actor_user_id="reconcile_worker",
            target_space="team:project",
            action="allow",
            reason="reconcile_success",
            outbox_id=67890,
            retry_count=3,
            original_locked_by="worker-old",
            original_locked_at="2024-01-01T00:00:00Z",
        )
        
        _validate_audit_event(event, schema)
        
        assert event["source"] == "reconcile_outbox"
        assert event["outbox_id"] == 67890
        assert event["extra"]["reconciled"] is True
        assert event["extra"]["original_locked_by"] == "worker-old"

    def test_generic_audit_event(self, schema):
        """通用 build_audit_event 函数"""
        event = build_audit_event(
            source="gateway",
            operation="custom_operation",
            correlation_id="corr-0000000000000000",
            actor_user_id="test_user",
            action="allow",
            reason="custom_reason",
            extra={"custom_field": "custom_value"},
        )
        
        _validate_audit_event(event, schema)
        
        assert event["source"] == "gateway"
        assert event["operation"] == "custom_operation"
        assert event["extra"]["custom_field"] == "custom_value"

    def test_pointer_substructure(self, schema):
        """v1.3 pointer 子结构测试"""
        event = build_gateway_audit_event(
            operation="memory_store",
            correlation_id="corr-eeeeffffaaaabbbb",
            requested_space="personal",
            final_space="team:redirect",
            action="redirect",
            reason="personal_to_team_redirect",
            policy_mode="compat",
            policy_is_pointerized=True,
            pointer_from_space="personal",
            pointer_to_space="team:redirect",
            pointer_reason="personal_space_disabled",
            pointer_preserved=True,
        )
        
        _validate_audit_event(event, schema)
        
        assert "pointer" in event
        assert event["pointer"]["from_space"] == "personal"
        assert event["pointer"]["to_space"] == "team:redirect"
        assert event["pointer"]["reason"] == "personal_space_disabled"
        assert event["pointer"]["preserved"] is True


class TestReconcileAuditEventSchemaValidation:
    """reconcile_outbox 对账审计事件的 schema 校验测试
    
    验证 build_reconcile_audit_event 和 build_evidence_refs_json 输出
    满足 audit_event_v1.schema.json 的要求。
    
    覆盖三种对账场景：
    - sent: 补写 outbox_flush_success 审计
    - dead: 补写 outbox_flush_dead 审计
    - stale: 补写 outbox_stale 审计
    """

    @pytest.fixture(scope="class")
    def schema(self):
        """加载 schema 文件"""
        return _load_schema()

    def test_reconcile_sent_audit_event(self, schema):
        """sent 场景：补写 outbox_flush_success 审计事件符合 schema"""
        event = build_reconcile_audit_event(
            operation="outbox_reconcile",
            correlation_id="corr-1234567890abcdef",
            actor_user_id="user1",
            target_space="private:user1",
            action="allow",
            reason="outbox_flush_success",  # ErrorCode.OUTBOX_FLUSH_SUCCESS
            payload_sha="a" * 64,
            outbox_id=12345,
            memory_id="mem-abc123",
            retry_count=0,
            original_locked_by="worker-001",
            original_locked_at="2024-01-15T10:00:00+00:00",
        )
        
        _validate_audit_event(event, schema)
        
        # 验证 reconcile 特有字段
        assert event["source"] == "reconcile_outbox"
        assert event["operation"] == "outbox_reconcile"
        assert event["decision"]["action"] == "allow"
        assert event["decision"]["reason"] == "outbox_flush_success"
        assert event["outbox_id"] == 12345
        assert event["extra"]["reconciled"] is True
        assert event["extra"]["original_locked_by"] == "worker-001"

    def test_reconcile_dead_audit_event(self, schema):
        """dead 场景：补写 outbox_flush_dead 审计事件符合 schema"""
        event = build_reconcile_audit_event(
            operation="outbox_reconcile",
            correlation_id="corr-fedcba9876543210",
            actor_user_id="user2",
            target_space="team:project",
            action="reject",
            reason="outbox_flush_dead",  # ErrorCode.OUTBOX_FLUSH_DEAD
            payload_sha="b" * 64,
            outbox_id=67890,
            retry_count=5,
            original_locked_by="worker-002",
            original_locked_at="2024-01-15T09:00:00+00:00",
            extra={
                "last_error": "OpenMemory connection timeout after 5 retries",
            },
        )
        
        _validate_audit_event(event, schema)
        
        # 验证 reconcile 特有字段
        assert event["source"] == "reconcile_outbox"
        assert event["decision"]["action"] == "reject"
        assert event["decision"]["reason"] == "outbox_flush_dead"
        assert event["retry_count"] == 5
        assert event["extra"]["reconciled"] is True
        assert event["extra"]["last_error"] == "OpenMemory connection timeout after 5 retries"

    def test_reconcile_stale_audit_event(self, schema):
        """stale 场景：补写 outbox_stale 审计事件符合 schema"""
        event = build_reconcile_audit_event(
            operation="outbox_reconcile",
            correlation_id="corr-0000111122223333",
            actor_user_id="user3",
            target_space="private:user3",
            action="redirect",
            reason="outbox_stale",  # ErrorCode.OUTBOX_STALE
            payload_sha="c" * 64,
            outbox_id=11111,
            retry_count=2,
            original_locked_by="worker-stale",
            original_locked_at="2024-01-15T08:00:00+00:00",
            extra={
                "stale_threshold_seconds": 600,
                "will_reschedule": True,
            },
        )
        
        _validate_audit_event(event, schema)
        
        # 验证 reconcile 特有字段
        assert event["source"] == "reconcile_outbox"
        assert event["decision"]["action"] == "redirect"
        assert event["decision"]["reason"] == "outbox_stale"
        assert event["extra"]["reconciled"] is True
        assert event["extra"]["stale_threshold_seconds"] == 600
        assert event["extra"]["will_reschedule"] is True

    def test_reconcile_sent_evidence_refs_json(self, schema):
        """sent 场景：evidence_refs_json 结构符合 schema"""
        gateway_event = build_reconcile_audit_event(
            operation="outbox_reconcile",
            correlation_id="corr-aaaabbbbccccdddd",
            actor_user_id="user1",
            target_space="private:user1",
            action="allow",
            reason="outbox_flush_success",
            payload_sha="d" * 64,
            outbox_id=22222,
            memory_id="mem-def456",
            retry_count=0,
        )
        
        evidence_refs_json = build_evidence_refs_json(
            evidence=None,
            gateway_event=gateway_event,
        )
        
        _validate_evidence_refs_json(evidence_refs_json, schema)
        
        # 验证结构
        assert "gateway_event" in evidence_refs_json
        gw = evidence_refs_json["gateway_event"]
        assert gw["source"] == "reconcile_outbox"
        assert gw["decision"]["reason"] == "outbox_flush_success"
        assert gw["outbox_id"] == 22222

    def test_reconcile_dead_evidence_refs_json(self, schema):
        """dead 场景：evidence_refs_json 结构符合 schema"""
        gateway_event = build_reconcile_audit_event(
            operation="outbox_reconcile",
            correlation_id="corr-4444555566667777",
            actor_user_id="user2",
            target_space="team:project",
            action="reject",
            reason="outbox_flush_dead",
            payload_sha="e" * 64,
            outbox_id=33333,
            retry_count=5,
            extra={
                "last_error": "max_retries_exceeded",
            },
        )
        
        evidence_refs_json = build_evidence_refs_json(
            evidence=None,
            gateway_event=gateway_event,
        )
        
        _validate_evidence_refs_json(evidence_refs_json, schema)
        
        # 验证结构
        assert "gateway_event" in evidence_refs_json
        gw = evidence_refs_json["gateway_event"]
        assert gw["source"] == "reconcile_outbox"
        assert gw["decision"]["reason"] == "outbox_flush_dead"
        assert gw["extra"]["last_error"] == "max_retries_exceeded"

    def test_reconcile_stale_evidence_refs_json(self, schema):
        """stale 场景：evidence_refs_json 结构符合 schema"""
        gateway_event = build_reconcile_audit_event(
            operation="outbox_reconcile",
            correlation_id="corr-8888999900001111",
            actor_user_id="user3",
            target_space="private:user3",
            action="redirect",
            reason="outbox_stale",
            payload_sha="f" * 64,
            outbox_id=44444,
            retry_count=1,
            original_locked_by="worker-stale",
            original_locked_at="2024-01-15T07:00:00+00:00",
            extra={
                "stale_threshold_seconds": 600,
                "will_reschedule": True,
            },
        )
        
        evidence_refs_json = build_evidence_refs_json(
            evidence=None,
            gateway_event=gateway_event,
        )
        
        _validate_evidence_refs_json(evidence_refs_json, schema)
        
        # 验证结构
        assert "gateway_event" in evidence_refs_json
        gw = evidence_refs_json["gateway_event"]
        assert gw["source"] == "reconcile_outbox"
        assert gw["decision"]["reason"] == "outbox_stale"
        assert gw["extra"]["original_locked_by"] == "worker-stale"

    def test_reconcile_reason_prefix_consistency(self, schema):
        """验证 reason 前缀与 DB 查询前缀一致性
        
        对账补写使用的 reason 值必须与 DB 查询使用的前缀匹配：
        - outbox_flush_success -> outbox_flush_success%
        - outbox_flush_dead -> outbox_flush_dead%
        - outbox_stale -> outbox_stale%
        """
        # 定义 reason 值与预期前缀的对应关系
        reason_prefix_map = {
            "outbox_flush_success": "outbox_flush_success",  # ErrorCode.OUTBOX_FLUSH_SUCCESS
            "outbox_flush_dead": "outbox_flush_dead",        # ErrorCode.OUTBOX_FLUSH_DEAD
            "outbox_stale": "outbox_stale",                  # ErrorCode.OUTBOX_STALE
        }
        
        for reason, expected_prefix in reason_prefix_map.items():
            event = build_reconcile_audit_event(
                operation="outbox_reconcile",
                correlation_id="corr-1234567890abcdef",  # 16 位十六进制
                action="allow",
                reason=reason,
            )
            
            _validate_audit_event(event, schema)
            
            # 验证 reason 以预期前缀开头（支持 LIKE 'prefix%' 查询）
            actual_reason = event["decision"]["reason"]
            assert actual_reason.startswith(expected_prefix), \
                f"reason '{actual_reason}' should start with '{expected_prefix}'"
