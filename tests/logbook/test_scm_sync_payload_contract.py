# -*- coding: utf-8 -*-
"""
test_scm_sync_payload_contract.py - SCM Sync Payload JSON Schema 契约测试

使用 JSON Schema 验证 payload 数据结构的正确性:
- 最小 payload（仅必填字段）
- 包含未知字段（向前兼容）
- 各种 window_type 场景
- 各种 mode/diff_mode 组合
"""

import json
import os
from pathlib import Path

import pytest

# 尝试导入 jsonschema，如果不可用则跳过测试
try:
    import jsonschema
    from jsonschema import Draft202012Validator, ValidationError

    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False
    Draft202012Validator = None
    ValidationError = Exception

from engram.logbook.scm_sync_payload import (
    DiffMode,
    JobPayloadVersion,
    SyncJobPayloadV2,
    SyncMode,
    WindowType,
    build_rev_window_payload,
    build_time_window_payload,
    parse_payload,
)

# Schema 文件路径（tests/logbook -> tests -> repo root）
REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = str(REPO_ROOT / "schemas" / "scm_sync_job_payload_v2.schema.json")


@pytest.fixture(scope="module")
def payload_schema():
    """加载 payload schema"""
    if not os.path.exists(SCHEMA_PATH):
        pytest.skip(f"Schema file not found: {SCHEMA_PATH}")

    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def validator(payload_schema):
    """创建 schema validator"""
    if not HAS_JSONSCHEMA:
        pytest.skip("jsonschema not installed")

    return Draft202012Validator(payload_schema)


def validate_against_schema(validator, data):
    """使用 schema 验证数据，返回 (is_valid, errors)"""
    errors = list(validator.iter_errors(data))
    return len(errors) == 0, errors


# ============ 最小 Payload 测试 ============


class TestMinimalPayload:
    """测试最小 payload 结构"""

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_empty_payload_valid(self, validator):
        """空 payload 应该有效（所有字段都有默认值）"""
        payload = {}
        is_valid, errors = validate_against_schema(validator, payload)
        assert is_valid, f"Empty payload should be valid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_minimal_incremental_payload(self, validator):
        """最小增量同步 payload"""
        payload = {
            "version": "v2",
            "mode": "incremental",
        }
        is_valid, errors = validate_against_schema(validator, payload)
        assert is_valid, f"Minimal incremental payload invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_minimal_probe_payload(self, validator):
        """最小 probe 模式 payload"""
        payload = {
            "version": "v2",
            "mode": "probe",
        }
        is_valid, errors = validate_against_schema(validator, payload)
        assert is_valid, f"Minimal probe payload invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_minimal_time_window_payload(self, validator):
        """最小时间窗口 payload"""
        payload = {
            "version": "v2",
            "window_type": "time",
            "since": "2024-01-01T00:00:00Z",
            "until": "2024-01-02T00:00:00Z",
            "mode": "backfill",
        }
        is_valid, errors = validate_against_schema(validator, payload)
        assert is_valid, f"Minimal time window payload invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_minimal_rev_window_payload(self, validator):
        """最小 revision 窗口 payload（SVN）"""
        payload = {
            "version": "v2",
            "window_type": "rev",
            "start_rev": 1000,
            "end_rev": 1100,
            "mode": "backfill",
        }
        is_valid, errors = validate_against_schema(validator, payload)
        assert is_valid, f"Minimal rev window payload invalid: {errors}"


# ============ 未知字段测试 ============


class TestUnknownFields:
    """测试未知字段透传（向前兼容）"""

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_unknown_fields_allowed(self, validator):
        """未知字段应该被允许（additionalProperties: true）"""
        payload = {
            "version": "v2",
            "gitlab_instance": "gitlab.example.com",
            # 未知字段
            "future_feature": True,
            "custom_config": {"nested": "value"},
            "experimental_flag": 123,
        }
        is_valid, errors = validate_against_schema(validator, payload)
        assert is_valid, f"Unknown fields should be allowed: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_unknown_fields_preserved_in_parse(self, validator):
        """未知字段在解析后应该保留在 extra 中"""
        payload_dict = {
            "version": "v2",
            "gitlab_instance": "gitlab.example.com",
            "custom_field_1": "value1",
            "custom_field_2": {"nested": "value"},
        }

        # 先验证 schema
        is_valid, errors = validate_against_schema(validator, payload_dict)
        assert is_valid, f"Payload with unknown fields invalid: {errors}"

        # 解析并检查 extra
        payload = parse_payload(payload_dict)
        assert payload.extra.get("custom_field_1") == "value1"
        assert payload.extra.get("custom_field_2") == {"nested": "value"}

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_roundtrip_preserves_unknown_fields(self, validator):
        """往返转换应该保留未知字段"""
        original = {
            "version": "v2",
            "gitlab_instance": "gitlab.example.com",
            "unknown_key": "unknown_value",
        }

        payload = parse_payload(original)
        result = payload.to_json_dict()

        assert result.get("unknown_key") == "unknown_value"

        # 结果也应该通过 schema 验证
        is_valid, errors = validate_against_schema(validator, result)
        assert is_valid, f"Roundtrip result invalid: {errors}"


# ============ 字段类型测试 ============


class TestFieldTypes:
    """测试字段类型验证"""

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_invalid_version_rejected(self, validator):
        """无效版本应该被拒绝"""
        payload = {
            "version": "v999",  # 无效版本
        }
        is_valid, errors = validate_against_schema(validator, payload)
        assert not is_valid, "Invalid version should be rejected"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_invalid_mode_rejected(self, validator):
        """无效 mode 应该被拒绝"""
        payload = {
            "mode": "invalid_mode",
        }
        is_valid, errors = validate_against_schema(validator, payload)
        assert not is_valid, "Invalid mode should be rejected"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_invalid_diff_mode_rejected(self, validator):
        """无效 diff_mode 应该被拒绝"""
        payload = {
            "diff_mode": "invalid_diff_mode",
        }
        is_valid, errors = validate_against_schema(validator, payload)
        assert not is_valid, "Invalid diff_mode should be rejected"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_minimal_diff_mode_accepted(self, validator):
        """diff_mode='minimal' 应该被接受"""
        payload = {
            "diff_mode": "minimal",
        }
        is_valid, errors = validate_against_schema(validator, payload)
        assert is_valid, f"diff_mode='minimal' should be valid: {errors}"

    def test_minimal_diff_mode_parsed_correctly(self):
        """diff_mode='minimal' 解析后应该保持原值"""
        payload = parse_payload({"diff_mode": "minimal"})
        assert payload.diff_mode == "minimal", "diff_mode='minimal' should be preserved"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_invalid_window_type_rejected(self, validator):
        """无效 window_type 应该被拒绝"""
        payload = {
            "window_type": "invalid",
        }
        is_valid, errors = validate_against_schema(validator, payload)
        assert not is_valid, "Invalid window_type should be rejected"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_batch_size_range_validated(self, validator):
        """batch_size 范围应该被验证"""
        # 有效范围
        payload_valid = {"batch_size": 100}
        is_valid, _ = validate_against_schema(validator, payload_valid)
        assert is_valid, "Valid batch_size should pass"

        # 超出范围 - 太小
        payload_too_small = {"batch_size": 0}
        is_valid, _ = validate_against_schema(validator, payload_too_small)
        assert not is_valid, "batch_size < 1 should be rejected"

        # 超出范围 - 太大
        payload_too_large = {"batch_size": 100000}
        is_valid, _ = validate_against_schema(validator, payload_too_large)
        assert not is_valid, "batch_size > 10000 should be rejected"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_revision_range_validated(self, validator):
        """revision 范围应该被验证"""
        # 有效 revision
        payload_valid = {"start_rev": 1000, "end_rev": 2000}
        is_valid, _ = validate_against_schema(validator, payload_valid)
        assert is_valid, "Valid revision should pass"

        # 负数 revision
        payload_negative = {"start_rev": -1}
        is_valid, _ = validate_against_schema(validator, payload_negative)
        assert not is_valid, "Negative revision should be rejected"


# ============ 构建函数与 Schema 一致性测试 ============


class TestBuilderSchemaConsistency:
    """测试构建函数输出与 Schema 一致"""

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_build_time_window_payload_valid(self, validator):
        """build_time_window_payload 输出应该通过 schema 验证"""
        payload = build_time_window_payload(
            since_ts=1704067200,
            until_ts=1704153600,
            gitlab_instance="gitlab.example.com",
            mode=SyncMode.BACKFILL.value,
        )

        payload_dict = payload.to_json_dict()
        is_valid, errors = validate_against_schema(validator, payload_dict)
        assert is_valid, f"build_time_window_payload output invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_build_rev_window_payload_valid(self, validator):
        """build_rev_window_payload 输出应该通过 schema 验证"""
        payload = build_rev_window_payload(
            start_rev=1000,
            end_rev=1100,
            mode=SyncMode.BACKFILL.value,
        )

        payload_dict = payload.to_json_dict()
        is_valid, errors = validate_against_schema(validator, payload_dict)
        assert is_valid, f"build_rev_window_payload output invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_default_payload_valid(self, validator):
        """默认 SyncJobPayloadV2 应该通过 schema 验证"""
        payload = SyncJobPayloadV2()
        payload_dict = payload.to_json_dict()

        is_valid, errors = validate_against_schema(validator, payload_dict)
        assert is_valid, f"Default payload invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_full_payload_valid(self, validator):
        """完整 payload（所有字段都有值）应该通过 schema 验证"""
        payload = SyncJobPayloadV2(
            version=JobPayloadVersion.V2.value,
            gitlab_instance="gitlab.example.com",
            tenant_id="tenant-1",
            project_key="group/project",
            window_type=WindowType.TIME.value,
            since_ts=1704067200,
            until_ts=1704153600,
            mode=SyncMode.BACKFILL.value,
            diff_mode=DiffMode.BEST_EFFORT.value,
            strict=True,
            update_watermark=True,
            batch_size=100,
            forward_window_seconds=3600,
            chunk_size=1000,
            total_chunks=5,
            current_chunk=0,
            extra={"custom": "value"},
        )

        payload_dict = payload.to_json_dict()
        is_valid, errors = validate_against_schema(validator, payload_dict)
        assert is_valid, f"Full payload invalid: {errors}"


# ============ 熔断/降级场景测试 ============


class TestCircuitBreakerPayload:
    """测试熔断/降级场景的 payload"""

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_degraded_payload_valid(self, validator):
        """降级模式 payload 应该有效"""
        payload = {
            "version": "v2",
            "gitlab_instance": "gitlab.example.com",
            "is_backfill_only": True,
            "circuit_state": "half_open",
            "suggested_batch_size": 50,
            "suggested_diff_mode": "none",
        }
        is_valid, errors = validate_against_schema(validator, payload)
        assert is_valid, f"Degraded payload invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_probe_mode_payload_valid(self, validator):
        """探测模式 payload 应该有效"""
        payload = {
            "version": "v2",
            "gitlab_instance": "gitlab.example.com",
            "is_probe_mode": True,
            "probe_budget": 10,
            "circuit_state": "half_open",
        }
        is_valid, errors = validate_against_schema(validator, payload)
        assert is_valid, f"Probe mode payload invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_invalid_circuit_state_rejected(self, validator):
        """无效的 circuit_state 应该被拒绝"""
        payload = {
            "circuit_state": "invalid_state",
        }
        is_valid, errors = validate_against_schema(validator, payload)
        assert not is_valid, "Invalid circuit_state should be rejected"


# ============ 时间戳场景测试 ============


class TestTimestampPayload:
    """测试时间戳相关的 payload"""

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_numeric_timestamp_valid(self, validator):
        """数字时间戳应该有效"""
        payload = {
            "since_ts": 1704067200.0,
            "until_ts": 1704153600.0,
        }
        is_valid, errors = validate_against_schema(validator, payload)
        assert is_valid, f"Numeric timestamp invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_string_since_until_valid(self, validator):
        """字符串 since/until 应该有效"""
        payload = {
            "since": "2024-01-01T00:00:00Z",
            "until": 1704153600,  # 混合格式
        }
        is_valid, errors = validate_against_schema(validator, payload)
        assert is_valid, f"String since/until invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_timestamp_out_of_range_rejected(self, validator):
        """超出范围的时间戳应该被拒绝"""
        # 超出最大值
        payload = {
            "since_ts": 5000000000,  # 大于 4102444800 (2100-01-01)
        }
        is_valid, errors = validate_against_schema(validator, payload)
        assert not is_valid, "Timestamp out of range should be rejected"


# ============ 分块场景测试 ============


class TestChunkPayload:
    """测试分块相关的 payload"""

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_chunk_payload_valid(self, validator):
        """分块 payload 应该有效"""
        payload = {
            "version": "v2",
            "mode": "backfill",
            "chunk_size": 86400,
            "total_chunks": 10,
            "current_chunk": 0,
        }
        is_valid, errors = validate_against_schema(validator, payload)
        assert is_valid, f"Chunk payload invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_chunk_index_range(self, validator):
        """分块索引范围应该被验证"""
        # current_chunk 超出 total_chunks（schema 不检查这个，由应用层检查）
        # 但 current_chunk 本身的范围应该被检查
        payload_negative = {
            "current_chunk": -1,
        }
        is_valid, _ = validate_against_schema(validator, payload_negative)
        assert not is_valid, "Negative current_chunk should be rejected"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_chunk_index_alias_valid(self, validator):
        """chunk_index/chunk_total 别名字段应该有效"""
        payload = {
            "version": "v2",
            "mode": "backfill",
            "window_type": "time",
            "window_since": "2024-01-01T00:00:00+00:00",
            "window_until": "2024-01-02T00:00:00+00:00",
            "chunk_index": 0,
            "chunk_total": 3,
        }
        is_valid, errors = validate_against_schema(validator, payload)
        assert is_valid, f"Chunk alias payload invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_revision_window_chunk_valid(self, validator):
        """SVN revision 窗口分块 payload 应该有效"""
        payload = {
            "version": "v2",
            "mode": "backfill",
            "window_type": "revision",
            "window_start_rev": 1000,
            "window_end_rev": 1100,
            "chunk_index": 1,
            "chunk_total": 5,
        }
        is_valid, errors = validate_against_schema(validator, payload)
        assert is_valid, f"Revision window chunk payload invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_window_type_revision_valid(self, validator):
        """window_type='revision' 应该有效（与 'rev' 等价）"""
        payload = {
            "window_type": "revision",
        }
        is_valid, errors = validate_against_schema(validator, payload)
        assert is_valid, f"window_type='revision' should be valid: {errors}"

    def test_window_type_revision_normalized_to_rev(self):
        """window_type='revision' 解析后应该被规范化为 'rev'"""
        payload = parse_payload({"window_type": "revision"})
        assert payload.window_type == "rev", "window_type='revision' should be normalized to 'rev'"

    def test_window_type_revision_case_insensitive(self):
        """window_type='REVISION' 大小写不敏感，应该被规范化为 'rev'"""
        for variant in ["REVISION", "Revision", "ReVision"]:
            payload = parse_payload({"window_type": variant})
            assert payload.window_type == "rev", (
                f"window_type='{variant}' should be normalized to 'rev'"
            )

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_negative_chunk_index_rejected(self, validator):
        """负数 chunk_index 应该被拒绝"""
        payload_negative = {
            "chunk_index": -1,
        }
        is_valid, _ = validate_against_schema(validator, payload_negative)
        assert not is_valid, "Negative chunk_index should be rejected"


# ============ 调度元数据测试 ============


class TestSchedulerMetadata:
    """测试调度器注入的元数据"""

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_scheduler_metadata_valid(self, validator):
        """调度器元数据应该有效"""
        payload = {
            "version": "v2",
            "gitlab_instance": "gitlab.example.com",
            "reason": "incremental_due",
            "scheduled_at": "2024-01-15T10:00:00+00:00",
            "logical_job_type": "commits",
            "physical_job_type": "gitlab_commits",
        }
        is_valid, errors = validate_against_schema(validator, payload)
        assert is_valid, f"Scheduler metadata invalid: {errors}"


# ============ Schema 文件测试 ============


class TestSchemaFile:
    """测试 Schema 文件本身"""

    def test_schema_file_exists(self):
        """Schema 文件应该存在"""
        assert os.path.exists(SCHEMA_PATH), f"Schema file not found: {SCHEMA_PATH}"

    def test_schema_is_valid_json(self):
        """Schema 文件应该是有效的 JSON"""
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            try:
                schema = json.load(f)
            except json.JSONDecodeError as e:
                pytest.fail(f"Schema is not valid JSON: {e}")

        assert schema is not None

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_schema_is_valid_json_schema(self, payload_schema):
        """Schema 应该是有效的 JSON Schema"""
        try:
            Draft202012Validator.check_schema(payload_schema)
        except jsonschema.SchemaError as e:
            pytest.fail(f"Schema is not valid JSON Schema: {e}")

    def test_schema_has_required_fields(self, payload_schema):
        """Schema 应该包含必要的元数据"""
        assert "$schema" in payload_schema
        assert "$id" in payload_schema
        assert "title" in payload_schema
        assert "type" in payload_schema
        assert payload_schema["type"] == "object"

    def test_schema_allows_additional_properties(self, payload_schema):
        """Schema 应该允许额外属性（向前兼容）"""
        assert payload_schema.get("additionalProperties") is True


# ============ 类型转换边界测试 ============


class TestTypeCoercion:
    """测试 parse_payload 的类型转换行为"""

    def test_bool_string_true_variants(self):
        """布尔字段：字符串 'true'/'1'/'yes' 转换为 True"""
        for true_val in ["true", "True", "TRUE", "1", "yes", "Yes"]:
            payload = parse_payload({"strict": true_val})
            assert payload.strict is True, f"'{true_val}' should be True"

    def test_bool_string_false_variants(self):
        """布尔字段：字符串 'false'/'0'/'no' 转换为 False"""
        for false_val in ["false", "False", "FALSE", "0", "no", "No", ""]:
            payload = parse_payload({"strict": false_val})
            assert payload.strict is False, f"'{false_val}' should be False"

    def test_bool_int_coercion(self):
        """布尔字段：整数转换为布尔"""
        # 非零整数 -> True
        payload = parse_payload({"strict": 1})
        assert payload.strict is True

        # 零 -> False
        payload = parse_payload({"strict": 0})
        assert payload.strict is False

    def test_int_string_coercion(self):
        """整数字段：字符串转换为整数"""
        payload = parse_payload({"batch_size": "100"})
        assert payload.batch_size == 100
        assert isinstance(payload.batch_size, int)

    def test_int_float_coercion(self):
        """整数字段：浮点数转换为整数"""
        payload = parse_payload({"batch_size": 100.5})
        assert payload.batch_size == 100
        assert isinstance(payload.batch_size, int)

    def test_int_invalid_string_becomes_none(self):
        """整数字段：无效字符串转换为 None（非 strict 模式）"""
        payload = parse_payload({"batch_size": "not_a_number"})
        assert payload.batch_size is None

    def test_timestamp_string_iso8601(self):
        """时间戳字段：ISO8601 字符串解析"""
        payload = parse_payload({"since": "2024-01-01T00:00:00Z"})
        assert payload.since_ts is not None
        assert isinstance(payload.since_ts, float)
        # 2024-01-01T00:00:00Z = 1704067200
        assert payload.since_ts == 1704067200.0

    def test_timestamp_string_date_only(self):
        """时间戳字段：仅日期字符串解析"""
        payload = parse_payload({"since": "2024-01-01"})
        assert payload.since_ts is not None
        assert isinstance(payload.since_ts, float)

    def test_timestamp_numeric_string(self):
        """时间戳字段：数字字符串解析"""
        payload = parse_payload({"since": "1704067200"})
        assert payload.since_ts == 1704067200.0

    def test_timestamp_int_passthrough(self):
        """时间戳字段：整数直接使用"""
        payload = parse_payload({"since": 1704067200})
        assert payload.since_ts == 1704067200.0

    def test_timestamp_float_passthrough(self):
        """时间戳字段：浮点数直接使用"""
        payload = parse_payload({"since": 1704067200.5})
        assert payload.since_ts == 1704067200.5


# ============ Extra 字段保留测试 ============


class TestExtraFieldPreservation:
    """测试未知字段的保留和往返"""

    def test_extra_simple_types_preserved(self):
        """简单类型的 extra 字段保留"""
        payload = parse_payload(
            {
                "custom_string": "value",
                "custom_int": 42,
                "custom_float": 3.14,
                "custom_bool": True,
            }
        )

        assert payload.extra.get("custom_string") == "value"
        assert payload.extra.get("custom_int") == 42
        assert payload.extra.get("custom_float") == 3.14
        assert payload.extra.get("custom_bool") is True

    def test_extra_nested_object_preserved(self):
        """嵌套对象的 extra 字段保留"""
        nested = {"level1": {"level2": {"level3": "deep"}}}
        payload = parse_payload({"nested_config": nested})

        assert payload.extra.get("nested_config") == nested
        assert payload.extra["nested_config"]["level1"]["level2"]["level3"] == "deep"

    def test_extra_list_preserved(self):
        """列表类型的 extra 字段保留"""
        payload = parse_payload(
            {
                "custom_list": [1, 2, 3],
                "mixed_list": [1, "two", {"three": 3}],
            }
        )

        assert payload.extra.get("custom_list") == [1, 2, 3]
        assert payload.extra.get("mixed_list") == [1, "two", {"three": 3}]

    def test_extra_null_preserved(self):
        """null 值的 extra 字段保留"""
        payload = parse_payload({"custom_null": None})

        assert "custom_null" in payload.extra
        assert payload.extra["custom_null"] is None

    def test_roundtrip_with_extra_fields(self):
        """往返转换保留所有 extra 字段"""
        original = {
            "version": "v2",
            "gitlab_instance": "gitlab.example.com",
            "batch_size": 100,
            "custom_field_1": "preserved",
            "custom_field_2": {"nested": True},
            "custom_field_3": [1, 2, 3],
        }

        payload = parse_payload(original)
        result = payload.to_json_dict()

        # 已知字段保留
        assert result["version"] == "v2"
        assert result["gitlab_instance"] == "gitlab.example.com"
        assert result["batch_size"] == 100

        # extra 字段保留
        assert result["custom_field_1"] == "preserved"
        assert result["custom_field_2"] == {"nested": True}
        assert result["custom_field_3"] == [1, 2, 3]

    def test_extra_access_via_get(self):
        """通过 extra.get() 访问未知字段"""
        payload = parse_payload({"scheduler_metadata": {"reason": "test"}})

        # 直接访问 extra
        meta = payload.extra.get("scheduler_metadata")
        assert meta is not None
        assert meta["reason"] == "test"

        # 缺失字段返回默认值
        assert payload.extra.get("nonexistent", "default") == "default"


# ============ 错误提示测试 ============


class TestErrorMessages:
    """测试验证错误的提示信息"""

    def test_validation_error_includes_field_name(self):
        """验证错误应包含字段名"""
        from engram.logbook.scm_sync_payload import PayloadValidationError

        # 非法版本
        try:
            parse_payload({"version": "invalid"}, strict=True)
            pytest.fail("Should raise PayloadValidationError")
        except PayloadValidationError as e:
            assert "version" in str(e)

    def test_validation_error_includes_invalid_value(self):
        """验证错误应包含无效值"""
        from engram.logbook.scm_sync_payload import PayloadValidationError

        # 非法 mode
        try:
            parse_payload({"mode": "invalid_mode"}, strict=True)
            pytest.fail("Should raise PayloadValidationError")
        except PayloadValidationError as e:
            assert "invalid_mode" in str(e) or "mode" in str(e)

    def test_validation_error_for_range_violation(self):
        """范围验证错误应有清晰提示"""
        from engram.logbook.scm_sync_payload import PayloadValidationError

        # batch_size 超出范围
        try:
            parse_payload({"batch_size": 0}, strict=True)
            pytest.fail("Should raise PayloadValidationError")
        except PayloadValidationError as e:
            assert "batch_size" in str(e)

    def test_validation_error_for_window_order(self):
        """窗口顺序错误应有清晰提示"""
        from engram.logbook.scm_sync_payload import PayloadValidationError

        # since > until
        try:
            parse_payload(
                {
                    "window_type": "time",
                    "since": 2000000000,
                    "until": 1000000000,
                },
                strict=True,
            )
            pytest.fail("Should raise PayloadValidationError")
        except PayloadValidationError as e:
            assert "since" in str(e) or "until" in str(e) or "窗口" in str(e)

    def test_non_strict_mode_returns_partial_result(self):
        """非 strict 模式返回部分有效结果"""
        # 无效的 batch_size，但不抛出异常
        payload = parse_payload({"batch_size": "invalid"}, strict=False)

        # batch_size 无法解析时变为 None
        assert payload.batch_size is None
        # 其他字段使用默认值
        assert payload.version == "v2"
        assert payload.mode == "incremental"

    def test_none_input_returns_default_payload(self):
        """None 输入返回默认 payload"""
        payload = parse_payload(None)

        assert payload is not None
        assert payload.version == "v2"
        assert payload.mode == "incremental"
        assert payload.diff_mode == "best_effort"

    def test_empty_dict_returns_default_payload(self):
        """空 dict 返回默认 payload"""
        payload = parse_payload({})

        assert payload is not None
        assert payload.version == "v2"
        assert payload.mode == "incremental"


# ============ Schema 与实现一致性测试 ============


class TestSchemaImplementationConsistency:
    """测试 Schema 定义与 Python 实现的一致性"""

    def test_all_schema_enums_match_implementation(self, payload_schema):
        """Schema 中的枚举值与实现一致"""
        props = payload_schema.get("properties", {})

        # version enum
        version_enum = props.get("version", {}).get("enum", [])
        assert "v2" in version_enum
        assert JobPayloadVersion.V2.value in version_enum

        # mode enum
        mode_enum = props.get("mode", {}).get("enum", [])
        assert "incremental" in mode_enum
        assert "backfill" in mode_enum
        assert "probe" in mode_enum
        assert SyncMode.INCREMENTAL.value in mode_enum
        assert SyncMode.BACKFILL.value in mode_enum
        assert SyncMode.PROBE.value in mode_enum

        # diff_mode enum
        diff_mode_enum = props.get("diff_mode", {}).get("enum", [])
        assert "always" in diff_mode_enum
        assert "best_effort" in diff_mode_enum
        assert "minimal" in diff_mode_enum
        assert "none" in diff_mode_enum
        assert DiffMode.ALWAYS.value in diff_mode_enum
        assert DiffMode.BEST_EFFORT.value in diff_mode_enum
        assert DiffMode.MINIMAL.value in diff_mode_enum
        assert DiffMode.NONE.value in diff_mode_enum

        # window_type enum
        window_type_enum = props.get("window_type", {}).get("enum", [])
        assert "time" in window_type_enum
        assert "rev" in window_type_enum
        # 新增：支持 "revision" 别名（scm_sync_runner 使用）
        assert "revision" in window_type_enum
        assert WindowType.TIME.value in window_type_enum
        assert WindowType.REV.value in window_type_enum

    def test_schema_defaults_match_implementation(self, payload_schema):
        """Schema 中的默认值与实现一致"""
        props = payload_schema.get("properties", {})

        # 创建默认 payload
        default_payload = SyncJobPayloadV2()

        # version default
        assert default_payload.version == props.get("version", {}).get("default", "v2")

        # mode default
        assert default_payload.mode == props.get("mode", {}).get("default", "incremental")

        # diff_mode default
        assert default_payload.diff_mode == props.get("diff_mode", {}).get("default", "best_effort")

        # window_type default
        assert default_payload.window_type == props.get("window_type", {}).get("default", "time")

        # strict default
        assert default_payload.strict == props.get("strict", {}).get("default", False)

        # update_watermark default
        assert default_payload.update_watermark == props.get("update_watermark", {}).get(
            "default", True
        )

    def test_schema_range_constraints_enforced(self):
        """Schema 中的范围约束应在实现中强制执行"""
        # batch_size 范围 1-10000
        payload = SyncJobPayloadV2(batch_size=5000)
        errors = payload.validate()
        assert len(errors) == 0, "Valid batch_size should pass"

        payload = SyncJobPayloadV2(batch_size=0)
        errors = payload.validate()
        assert len(errors) > 0, "batch_size=0 should fail validation"

        payload = SyncJobPayloadV2(batch_size=100000)
        errors = payload.validate()
        assert len(errors) > 0, "batch_size=100000 should fail validation"

    def test_revision_range_constraints_enforced(self):
        """Revision 范围约束应在实现中强制执行"""
        # 负数 revision 无效
        payload = SyncJobPayloadV2(
            window_type=WindowType.REV.value,
            start_rev=-1,
        )
        errors = payload.validate()
        assert len(errors) > 0, "Negative revision should fail validation"


# ============ Dry-Run 默认行为契约测试 ============


class TestDryRunDefaultBehavior:
    """测试 dry_run 默认行为契约

    验证 payload 中 dry_run 字段的默认行为符合预期：
    - SyncJobPayloadV2 中 dry_run 作为 extra 字段透传
    - JSON Schema 允许 dry_run 字段
    - 往返序列化保留原始值

    注意：dry_run 不是 SyncJobPayloadV2 的直接属性（它是运行时 SyncJobPayload 的属性）
    在 SyncJobPayloadV2 中，dry_run 会被存储在 extra 字段中进行透传。
    """

    def test_dry_run_preserved_in_extra(self):
        """dry_run 被保留在 extra 中"""
        # parse_payload 会将 dry_run 放入 extra（因为不是 V1 的直接字段）
        payload = parse_payload({"dry_run": True})
        assert payload.extra.get("dry_run") is True

        payload = parse_payload({"dry_run": False})
        assert payload.extra.get("dry_run") is False

    def test_dry_run_roundtrip_via_extra(self):
        """dry_run 往返序列化保留原始值"""
        # True roundtrip
        payload = parse_payload({"dry_run": True})
        result = payload.to_json_dict()
        assert result.get("dry_run") is True

        # False roundtrip
        payload = parse_payload({"dry_run": False})
        result = payload.to_json_dict()
        assert result.get("dry_run") is False

    def test_dry_run_preserved_with_other_fields(self):
        """dry_run 与其他字段一起保留"""
        original = {
            "version": "v2",
            "gitlab_instance": "gitlab.example.com",
            "batch_size": 100,
            "dry_run": True,
            "mode": "backfill",
        }

        payload = parse_payload(original)
        assert payload.extra.get("dry_run") is True
        assert payload.gitlab_instance == "gitlab.example.com"
        assert payload.batch_size == 100

        result = payload.to_json_dict()
        assert result.get("dry_run") is True
        assert result.get("gitlab_instance") == "gitlab.example.com"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_dry_run_schema_valid(self, validator):
        """dry_run 字段在 schema 中有效"""
        # dry_run=true
        payload_true = {"dry_run": True}
        is_valid, errors = validate_against_schema(validator, payload_true)
        assert is_valid, f"dry_run=True should be valid: {errors}"

        # dry_run=false
        payload_false = {"dry_run": False}
        is_valid, errors = validate_against_schema(validator, payload_false)
        assert is_valid, f"dry_run=False should be valid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_dry_run_in_schema_properties(self, payload_schema):
        """Schema 应该定义 dry_run 字段"""
        props = payload_schema.get("properties", {})

        # Schema 应该有 dry_run 字段定义（或者允许 additionalProperties）
        if "dry_run" in props:
            dry_run_spec = props["dry_run"]
            # 如果定义了，应该是 boolean 类型
            assert dry_run_spec.get("type") == "boolean"
        else:
            # 如果没有显式定义，additionalProperties 应该为 True
            assert payload_schema.get("additionalProperties") is True


# ============ 非法字段值分类测试 ============


class TestInvalidFieldCategories:
    """测试各类非法字段值的拒绝行为"""

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_invalid_suggested_diff_mode_rejected(self, validator):
        """无效 suggested_diff_mode 应该被拒绝"""
        payload = {
            "suggested_diff_mode": "invalid_mode",
        }
        is_valid, errors = validate_against_schema(validator, payload)
        assert not is_valid, "Invalid suggested_diff_mode should be rejected"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_valid_suggested_diff_mode_accepted(self, validator):
        """有效 suggested_diff_mode 应该被接受"""
        for valid_mode in ["always", "best_effort", "minimal", "none", None]:
            payload = {"suggested_diff_mode": valid_mode}
            is_valid, errors = validate_against_schema(validator, payload)
            assert is_valid, f"suggested_diff_mode='{valid_mode}' should be valid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_valid_circuit_state_accepted(self, validator):
        """有效 circuit_state 应该被接受"""
        for valid_state in ["closed", "half_open", "open", "degraded", None]:
            payload = {"circuit_state": valid_state}
            is_valid, errors = validate_against_schema(validator, payload)
            assert is_valid, f"circuit_state='{valid_state}' should be valid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_forward_window_seconds_too_small_rejected(self, validator):
        """forward_window_seconds 低于最小值应该被拒绝"""
        payload = {"forward_window_seconds": 30}  # min 60
        is_valid, errors = validate_against_schema(validator, payload)
        assert not is_valid, "forward_window_seconds < 60 should be rejected"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_forward_window_seconds_too_large_rejected(self, validator):
        """forward_window_seconds 超过最大值应该被拒绝"""
        payload = {"forward_window_seconds": 3000000}  # max 2592000
        is_valid, errors = validate_against_schema(validator, payload)
        assert not is_valid, "forward_window_seconds > 2592000 should be rejected"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_chunk_size_too_large_rejected(self, validator):
        """chunk_size 超过最大值应该被拒绝"""
        payload = {"chunk_size": 200000}  # max 100000
        is_valid, errors = validate_against_schema(validator, payload)
        assert not is_valid, "chunk_size > 100000 should be rejected"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_total_chunks_zero_rejected(self, validator):
        """total_chunks=0 应该被拒绝"""
        payload = {"total_chunks": 0}  # min 1
        is_valid, errors = validate_against_schema(validator, payload)
        assert not is_valid, "total_chunks=0 should be rejected"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_total_chunks_too_large_rejected(self, validator):
        """total_chunks 超过最大值应该被拒绝"""
        payload = {"total_chunks": 20000}  # max 10000
        is_valid, errors = validate_against_schema(validator, payload)
        assert not is_valid, "total_chunks > 10000 should be rejected"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_current_chunk_too_large_rejected(self, validator):
        """current_chunk 超过最大值应该被拒绝"""
        payload = {"current_chunk": 10000}  # max 9999
        is_valid, errors = validate_against_schema(validator, payload)
        assert not is_valid, "current_chunk > 9999 should be rejected"


# ============ 完整 Payload 场景测试 ============


class TestCompletePayloadScenarios:
    """测试完整 payload 的各种典型场景"""

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_full_gitlab_incremental_payload(self, validator):
        """完整的 GitLab 增量同步 payload"""
        payload = {
            "version": "v2",
            "gitlab_instance": "gitlab.example.com",
            "tenant_id": "tenant-acme",
            "project_key": "group/project",
            "mode": "incremental",
            "diff_mode": "best_effort",
            "strict": False,
            "update_watermark": True,
            "batch_size": 100,
            "verbose": False,
            "reason": "incremental_due",
            "scheduled_at": "2024-01-15T10:00:00+00:00",
            "logical_job_type": "commits",
            "physical_job_type": "gitlab_commits",
        }
        is_valid, errors = validate_against_schema(validator, payload)
        assert is_valid, f"Full GitLab incremental payload invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_full_svn_backfill_payload(self, validator):
        """完整的 SVN backfill payload"""
        payload = {
            "version": "v2",
            "tenant_id": "tenant-acme",
            "window_type": "rev",
            "start_rev": 1000,
            "end_rev": 2000,
            "mode": "backfill",
            "diff_mode": "always",
            "strict": True,
            "update_watermark": True,
            "chunk_size": 100,
            "total_chunks": 10,
            "current_chunk": 0,
        }
        is_valid, errors = validate_against_schema(validator, payload)
        assert is_valid, f"Full SVN backfill payload invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_full_chunked_time_window_payload(self, validator):
        """完整的时间窗口分块 payload"""
        payload = {
            "version": "v2",
            "gitlab_instance": "gitlab.example.com",
            "window_type": "time",
            "window_since": "2024-01-01T00:00:00+00:00",
            "window_until": "2024-01-02T00:00:00+00:00",
            "mode": "backfill",
            "chunk_index": 2,
            "chunk_total": 5,
            "update_watermark": False,
        }
        is_valid, errors = validate_against_schema(validator, payload)
        assert is_valid, f"Full chunked time window payload invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_full_degraded_mode_payload(self, validator):
        """完整的熔断降级 payload"""
        payload = {
            "version": "v2",
            "gitlab_instance": "gitlab.example.com",
            "mode": "incremental",
            "is_backfill_only": True,
            "circuit_state": "degraded",
            "is_probe_mode": False,
            "suggested_batch_size": 25,
            "suggested_diff_mode": "none",
            "suggested_forward_window_seconds": 1800,
        }
        is_valid, errors = validate_against_schema(validator, payload)
        assert is_valid, f"Full degraded mode payload invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_full_probe_mode_payload(self, validator):
        """完整的探测模式 payload"""
        payload = {
            "version": "v2",
            "gitlab_instance": "gitlab.example.com",
            "mode": "probe",  # probe 模式
            "circuit_state": "half_open",
            "is_probe_mode": True,
            "probe_budget": 5,
            "batch_size": 10,
        }
        is_valid, errors = validate_against_schema(validator, payload)
        assert is_valid, f"Full probe mode payload invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_probe_mode_with_degraded_params(self, validator):
        """probe 模式配合降级参数 payload"""
        payload = {
            "version": "v2",
            "gitlab_instance": "gitlab.example.com",
            "mode": "probe",
            "circuit_state": "half_open",
            "is_probe_mode": True,
            "probe_budget": 3,
            "suggested_batch_size": 20,
            "suggested_forward_window_seconds": 1800,
            "suggested_diff_mode": "minimal",
        }
        is_valid, errors = validate_against_schema(validator, payload)
        assert is_valid, f"Probe mode with degraded params invalid: {errors}"


# ============ 未知字段深度透传测试 ============


class TestUnknownFieldsDeepPassthrough:
    """测试未知字段的深度透传场景"""

    def test_deeply_nested_unknown_fields_preserved(self):
        """深层嵌套的未知字段应该保留"""
        nested = {"level1": {"level2": {"level3": {"deep_value": "preserved"}}}}
        payload = parse_payload({"deep_nested": nested})
        assert payload.extra.get("deep_nested") == nested

    def test_array_with_objects_preserved(self):
        """包含对象的数组应该保留"""
        array_data = [
            {"name": "item1", "value": 1},
            {"name": "item2", "value": 2},
        ]
        payload = parse_payload({"custom_array": array_data})
        assert payload.extra.get("custom_array") == array_data

    def test_mixed_known_unknown_fields_roundtrip(self):
        """已知字段和未知字段混合的往返测试"""
        original = {
            "version": "v2",
            "gitlab_instance": "gitlab.example.com",
            "batch_size": 100,
            "mode": "backfill",
            # 未知字段
            "custom_metadata": {"key": "value"},
            "custom_tags": ["tag1", "tag2"],
            "custom_number": 42,
            "custom_bool": True,
            "custom_null": None,
        }

        payload = parse_payload(original)
        result = payload.to_json_dict()

        # 已知字段保留
        assert result["version"] == "v2"
        assert result["gitlab_instance"] == "gitlab.example.com"
        assert result["batch_size"] == 100

        # 未知字段保留
        assert result.get("custom_metadata") == {"key": "value"}
        assert result.get("custom_tags") == ["tag1", "tag2"]
        assert result.get("custom_number") == 42
        assert result.get("custom_bool") is True
        assert result.get("custom_null") is None

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_roundtrip_result_passes_schema(self, validator):
        """往返结果应该通过 schema 验证"""
        original = {
            "version": "v2",
            "gitlab_instance": "gitlab.example.com",
            "mode": "backfill",
            "future_field_1": "value1",
            "future_field_2": {"nested": True},
        }

        payload = parse_payload(original)
        result = payload.to_json_dict()

        is_valid, errors = validate_against_schema(validator, result)
        assert is_valid, f"Roundtrip result should pass schema: {errors}"


# ============ Python 实现验证测试 ============


class TestPythonValidation:
    """测试 Python 实现的验证逻辑"""

    def test_validate_invalid_batch_size_zero(self):
        """batch_size=0 应该验证失败"""
        payload = SyncJobPayloadV2(batch_size=0)
        errors = payload.validate()
        assert len(errors) > 0
        assert any("batch_size" in e for e in errors)

    def test_validate_invalid_batch_size_negative(self):
        """batch_size 负数应该验证失败"""
        payload = SyncJobPayloadV2(batch_size=-1)
        errors = payload.validate()
        assert len(errors) > 0

    def test_validate_invalid_forward_window_seconds(self):
        """forward_window_seconds 超出范围应该验证失败"""
        # 太小
        payload = SyncJobPayloadV2(forward_window_seconds=30)
        errors = payload.validate()
        assert len(errors) > 0

        # 太大
        payload = SyncJobPayloadV2(forward_window_seconds=3000000)
        errors = payload.validate()
        assert len(errors) > 0

    def test_validate_invalid_chunk_size(self):
        """chunk_size 超出范围应该验证失败"""
        payload = SyncJobPayloadV2(chunk_size=0)
        errors = payload.validate()
        assert len(errors) > 0

        payload = SyncJobPayloadV2(chunk_size=200000)
        errors = payload.validate()
        assert len(errors) > 0

    def test_validate_invalid_current_chunk_exceeds_total(self):
        """current_chunk >= total_chunks 应该验证失败"""
        payload = SyncJobPayloadV2(total_chunks=5, current_chunk=5)
        errors = payload.validate()
        assert len(errors) > 0
        assert any("current_chunk" in e for e in errors)

    def test_validate_invalid_time_window_order(self):
        """since_ts > until_ts 应该验证失败"""
        payload = SyncJobPayloadV2(
            window_type=WindowType.TIME.value,
            since_ts=2000000000,
            until_ts=1000000000,
        )
        errors = payload.validate()
        assert len(errors) > 0
        assert any("窗口" in e or "since" in e or "until" in e for e in errors)

    def test_validate_invalid_rev_window_order(self):
        """start_rev > end_rev 应该验证失败"""
        payload = SyncJobPayloadV2(
            window_type=WindowType.REV.value,
            start_rev=2000,
            end_rev=1000,
        )
        errors = payload.validate()
        assert len(errors) > 0

    def test_validate_negative_revision(self):
        """负数 revision 应该验证失败"""
        payload = SyncJobPayloadV2(
            window_type=WindowType.REV.value,
            start_rev=-1,
        )
        errors = payload.validate()
        assert len(errors) > 0

    def test_validate_timestamp_out_of_range(self):
        """时间戳超出范围应该验证失败"""
        payload = SyncJobPayloadV2(
            window_type=WindowType.TIME.value,
            since_ts=5000000000,  # > 4102444800
        )
        errors = payload.validate()
        assert len(errors) > 0


# ============ Probe 模式测试 ============


class TestProbeModeContract:
    """测试 probe 模式的契约一致性"""

    def test_probe_mode_enum_value(self):
        """SyncMode.PROBE 值正确"""
        assert SyncMode.PROBE.value == "probe"

    def test_parse_probe_mode_payload(self):
        """解析 probe 模式 payload"""
        payload = parse_payload({"mode": "probe"})
        assert payload.mode == "probe"

    def test_probe_mode_validation_passes(self):
        """probe 模式验证通过"""
        payload = SyncJobPayloadV2(mode=SyncMode.PROBE.value)
        errors = payload.validate()
        assert len(errors) == 0, f"Probe mode should be valid: {errors}"

    def test_probe_mode_with_probe_budget(self):
        """probe 模式配合 probe_budget"""
        payload = parse_payload(
            {
                "mode": "probe",
                "is_probe_mode": True,
                "probe_budget": 10,
                "circuit_state": "half_open",
            }
        )
        assert payload.mode == "probe"
        assert payload.extra.get("is_probe_mode") is True
        assert payload.extra.get("probe_budget") == 10
        assert payload.extra.get("circuit_state") == "half_open"

    def test_probe_mode_roundtrip(self):
        """probe 模式往返序列化"""
        original = {
            "version": "v2",
            "mode": "probe",
            "gitlab_instance": "gitlab.example.com",
            "is_probe_mode": True,
            "probe_budget": 5,
        }
        payload = parse_payload(original)
        result = payload.to_json_dict()

        assert result["mode"] == "probe"
        assert result.get("is_probe_mode") is True
        assert result.get("probe_budget") == 5

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_probe_mode_schema_valid(self, validator):
        """probe 模式通过 schema 验证"""
        payload = {"mode": "probe"}
        is_valid, errors = validate_against_schema(validator, payload)
        assert is_valid, f"mode='probe' should be valid: {errors}"

    def test_all_sync_modes_valid(self):
        """所有 SyncMode 枚举值都能通过验证"""
        for mode in SyncMode:
            payload = SyncJobPayloadV2(mode=mode.value)
            errors = payload.validate()
            assert len(errors) == 0, f"Mode '{mode.value}' should be valid: {errors}"
