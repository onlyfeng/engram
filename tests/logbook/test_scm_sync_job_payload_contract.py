# -*- coding: utf-8 -*-
"""
SCM Sync Job Payload Schema 契约测试

测试覆盖:
1. sync_jobs.payload_json 返回结构符合 JSON Schema
2. 字段级校验：窗口类型、模式、批量配置
3. schema 中的 examples 有效性校验
"""

import json
from pathlib import Path
from typing import Any, Dict

import pytest

try:
    import jsonschema  # noqa: F401
    from jsonschema import Draft202012Validator, ValidationError, validate  # noqa: F401

    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False


# Schema 文件路径计算
def _find_schema_path() -> Path:
    """查找 schema 文件路径，支持多种执行上下文"""
    current = Path(__file__).resolve().parent

    for _ in range(10):
        candidate = current / "schemas" / "scm_sync_job_payload_v2.schema.json"
        if candidate.exists():
            return candidate
        current = current.parent

    # 从 apps/logbook_postgres/scripts/tests/ 回溯到项目根
    fallback = (
        Path(__file__).resolve().parent.parent.parent.parent.parent
        / "schemas"
        / "scm_sync_job_payload_v2.schema.json"
    )
    return fallback


SCHEMA_PATH = _find_schema_path()


def load_schema() -> Dict[str, Any]:
    """加载 scm_sync_job_payload_v2 schema"""
    if not SCHEMA_PATH.exists():
        pytest.skip(f"Schema 文件不存在: {SCHEMA_PATH}")

    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def create_mock_job_payload_incremental() -> Dict[str, Any]:
    """创建一个符合规范的 mock incremental 模式 job payload"""
    return {
        "version": "v2",
        "gitlab_instance": "gitlab.example.com",
        "mode": "incremental",
        "diff_mode": "best_effort",
        "strict": False,
        "update_watermark": True,
    }


def create_mock_job_payload_time_window() -> Dict[str, Any]:
    """创建一个符合规范的 mock 时间窗口 backfill payload"""
    return {
        "version": "v2",
        "window_type": "time",
        "since": "2024-01-01T00:00:00Z",
        "until": "2024-01-02T00:00:00Z",
        "mode": "backfill",
        "update_watermark": True,
    }


def create_mock_job_payload_revision_window() -> Dict[str, Any]:
    """创建一个符合规范的 mock revision 窗口 backfill payload"""
    return {
        "version": "v2",
        "window_type": "rev",
        "start_rev": 1000,
        "end_rev": 1100,
        "mode": "backfill",
    }


def create_mock_job_payload_with_degradation() -> Dict[str, Any]:
    """创建一个包含熔断降级信息的 mock payload"""
    return {
        "version": "v2",
        "gitlab_instance": "gitlab.example.com",
        "mode": "incremental",
        "diff_mode": "none",
        "is_backfill_only": True,
        "circuit_state": "half_open",
        "suggested_batch_size": 50,
        "suggested_forward_window_seconds": 3600,
        "suggested_diff_mode": "none",
    }


@pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
class TestJobPayloadSchema:
    """测试 job payload schema 校验"""

    @pytest.fixture(scope="class")
    def schema(self):
        """加载 schema"""
        return load_schema()

    def test_valid_incremental_payload_passes(self, schema):
        """完整的有效 incremental payload 应通过 schema 校验"""
        payload = create_mock_job_payload_incremental()
        validate(instance=payload, schema=schema)

    def test_valid_time_window_payload_passes(self, schema):
        """完整的有效时间窗口 payload 应通过 schema 校验"""
        payload = create_mock_job_payload_time_window()
        validate(instance=payload, schema=schema)

    def test_valid_revision_window_payload_passes(self, schema):
        """完整的有效 revision 窗口 payload 应通过 schema 校验"""
        payload = create_mock_job_payload_revision_window()
        validate(instance=payload, schema=schema)

    def test_valid_degradation_payload_passes(self, schema):
        """包含熔断降级信息的 payload 应通过 schema 校验"""
        payload = create_mock_job_payload_with_degradation()
        validate(instance=payload, schema=schema)

    def test_minimal_payload_passes(self, schema):
        """最小 payload（仅版本）应通过 schema 校验"""
        payload = {"version": "v2"}
        validate(instance=payload, schema=schema)

    def test_empty_payload_passes(self, schema):
        """空 payload 应通过 schema 校验（additionalProperties: true）"""
        payload = {}
        validate(instance=payload, schema=schema)


@pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
class TestVersionField:
    """测试 version 字段"""

    @pytest.fixture(scope="class")
    def schema(self):
        return load_schema()

    def test_valid_version_v2(self, schema):
        """有效的 version v2 应通过"""
        payload = {"version": "v2"}
        validate(instance=payload, schema=schema)

    def test_invalid_version_fails(self, schema):
        """无效的 version 值应失败"""
        payload = {"version": "v2"}

        with pytest.raises(ValidationError):
            validate(instance=payload, schema=schema)


@pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
class TestWindowTypeEnum:
    """测试 window_type 枚举值"""

    @pytest.fixture(scope="class")
    def schema(self):
        return load_schema()

    def test_valid_window_types(self, schema):
        """有效的 window_type 值应通过"""
        for window_type in ["time", "rev", "revision", None]:
            payload = {"window_type": window_type}
            validate(instance=payload, schema=schema)

    def test_invalid_window_type_fails(self, schema):
        """无效的 window_type 值应失败"""
        payload = {"window_type": "invalid_type"}

        with pytest.raises(ValidationError):
            validate(instance=payload, schema=schema)


@pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
class TestModeEnum:
    """测试 mode 枚举值"""

    @pytest.fixture(scope="class")
    def schema(self):
        return load_schema()

    def test_valid_modes(self, schema):
        """有效的 mode 值应通过"""
        for mode in ["incremental", "backfill"]:
            payload = {"mode": mode}
            validate(instance=payload, schema=schema)

    def test_invalid_mode_fails(self, schema):
        """无效的 mode 值应失败"""
        payload = {"mode": "invalid_mode"}

        with pytest.raises(ValidationError):
            validate(instance=payload, schema=schema)


@pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
class TestDiffModeEnum:
    """测试 diff_mode 枚举值"""

    @pytest.fixture(scope="class")
    def schema(self):
        return load_schema()

    def test_valid_diff_modes(self, schema):
        """有效的 diff_mode 值应通过"""
        for diff_mode in ["always", "best_effort", "minimal", "none"]:
            payload = {"diff_mode": diff_mode}
            validate(instance=payload, schema=schema)

    def test_invalid_diff_mode_fails(self, schema):
        """无效的 diff_mode 值应失败"""
        payload = {"diff_mode": "invalid_mode"}

        with pytest.raises(ValidationError):
            validate(instance=payload, schema=schema)

    def test_minimal_diff_mode_valid(self, schema):
        """diff_mode='minimal' 应该通过验证"""
        payload = {"diff_mode": "minimal"}
        validate(instance=payload, schema=schema)

    def test_suggested_diff_mode_minimal_valid(self, schema):
        """suggested_diff_mode='minimal' 应该通过验证"""
        payload = {"suggested_diff_mode": "minimal"}
        validate(instance=payload, schema=schema)


@pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
class TestTimeWindowFields:
    """测试时间窗口字段"""

    @pytest.fixture(scope="class")
    def schema(self):
        return load_schema()

    def test_since_ts_valid_range(self, schema):
        """since_ts 必须在有效范围内"""
        # 有效值
        payload = {"since_ts": 0}
        validate(instance=payload, schema=schema)

        payload = {"since_ts": 1704067200}  # 2024-01-01
        validate(instance=payload, schema=schema)

    def test_since_ts_negative_fails(self, schema):
        """since_ts 负值应失败"""
        payload = {"since_ts": -1}

        with pytest.raises(ValidationError):
            validate(instance=payload, schema=schema)

    def test_since_ts_exceeds_max_fails(self, schema):
        """since_ts 超过最大值应失败"""
        payload = {"since_ts": 4102444801}  # 超过最大值

        with pytest.raises(ValidationError):
            validate(instance=payload, schema=schema)

    def test_until_ts_valid_range(self, schema):
        """until_ts 必须在有效范围内"""
        payload = {"until_ts": 1704153600}  # 2024-01-02
        validate(instance=payload, schema=schema)


@pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
class TestRevisionWindowFields:
    """测试 revision 窗口字段"""

    @pytest.fixture(scope="class")
    def schema(self):
        return load_schema()

    def test_start_rev_valid_range(self, schema):
        """start_rev 必须在有效范围内"""
        payload = {"start_rev": 0}
        validate(instance=payload, schema=schema)

        payload = {"start_rev": 1000}
        validate(instance=payload, schema=schema)

    def test_start_rev_negative_fails(self, schema):
        """start_rev 负值应失败"""
        payload = {"start_rev": -1}

        with pytest.raises(ValidationError):
            validate(instance=payload, schema=schema)

    def test_end_rev_valid_range(self, schema):
        """end_rev 必须在有效范围内"""
        payload = {"end_rev": 1100}
        validate(instance=payload, schema=schema)


@pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
class TestBatchConfigFields:
    """测试批量配置字段"""

    @pytest.fixture(scope="class")
    def schema(self):
        return load_schema()

    def test_batch_size_valid_range(self, schema):
        """batch_size 必须在有效范围内"""
        payload = {"batch_size": 1}
        validate(instance=payload, schema=schema)

        payload = {"batch_size": 10000}
        validate(instance=payload, schema=schema)

    def test_batch_size_too_small_fails(self, schema):
        """batch_size 小于 1 应失败"""
        payload = {"batch_size": 0}

        with pytest.raises(ValidationError):
            validate(instance=payload, schema=schema)

    def test_batch_size_too_large_fails(self, schema):
        """batch_size 超过最大值应失败"""
        payload = {"batch_size": 10001}

        with pytest.raises(ValidationError):
            validate(instance=payload, schema=schema)

    def test_forward_window_seconds_valid_range(self, schema):
        """forward_window_seconds 必须在有效范围内"""
        payload = {"forward_window_seconds": 60}
        validate(instance=payload, schema=schema)

        payload = {"forward_window_seconds": 2592000}
        validate(instance=payload, schema=schema)

    def test_forward_window_seconds_too_small_fails(self, schema):
        """forward_window_seconds 小于最小值应失败"""
        payload = {"forward_window_seconds": 59}

        with pytest.raises(ValidationError):
            validate(instance=payload, schema=schema)


@pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
class TestChunkingFields:
    """测试分块字段"""

    @pytest.fixture(scope="class")
    def schema(self):
        return load_schema()

    def test_chunk_size_valid_range(self, schema):
        """chunk_size 必须在有效范围内"""
        payload = {"chunk_size": 1}
        validate(instance=payload, schema=schema)

        payload = {"chunk_size": 100000}
        validate(instance=payload, schema=schema)

    def test_total_chunks_valid_range(self, schema):
        """total_chunks 必须在有效范围内"""
        payload = {"total_chunks": 1}
        validate(instance=payload, schema=schema)

        payload = {"total_chunks": 10000}
        validate(instance=payload, schema=schema)

    def test_current_chunk_valid_range(self, schema):
        """current_chunk 必须在有效范围内"""
        payload = {"current_chunk": 0}
        validate(instance=payload, schema=schema)

        payload = {"current_chunk": 9999}
        validate(instance=payload, schema=schema)


@pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
class TestCircuitStateEnum:
    """测试 circuit_state 枚举值"""

    @pytest.fixture(scope="class")
    def schema(self):
        return load_schema()

    def test_valid_circuit_states(self, schema):
        """有效的 circuit_state 值应通过"""
        for state in ["closed", "half_open", "open", "degraded", None]:
            payload = {"circuit_state": state}
            validate(instance=payload, schema=schema)

    def test_invalid_circuit_state_fails(self, schema):
        """无效的 circuit_state 值应失败"""
        payload = {"circuit_state": "invalid_state"}

        with pytest.raises(ValidationError):
            validate(instance=payload, schema=schema)


@pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
class TestBooleanFields:
    """测试布尔字段"""

    @pytest.fixture(scope="class")
    def schema(self):
        return load_schema()

    def test_boolean_fields_valid(self, schema):
        """布尔字段应接受 true/false"""
        payload = {
            "strict": True,
            "update_watermark": False,
            "is_backfill_only": True,
            "is_probe_mode": False,
            "verbose": True,
            "dry_run": False,
        }
        validate(instance=payload, schema=schema)


@pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
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


@pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
class TestAdditionalProperties:
    """测试 additionalProperties 允许扩展字段"""

    @pytest.fixture(scope="class")
    def schema(self):
        return load_schema()

    def test_additional_properties_allowed(self, schema):
        """额外的属性应被允许（additionalProperties: true）"""
        payload = {
            "version": "v2",
            "custom_field": "custom_value",
            "another_custom": 123,
        }

        validate(instance=payload, schema=schema)

    def test_complex_additional_properties(self, schema):
        """复杂的额外属性应被允许"""
        payload = {
            "version": "v2",
            "custom_object": {"nested": "value"},
            "custom_array": [1, 2, 3],
        }

        validate(instance=payload, schema=schema)


# ============================================================
# 集成测试：入队 → claim 按 instance/tenant 过滤
# ============================================================

from unittest.mock import MagicMock


class TestEnqueueClaimDimensionFiltering:
    """
    集成测试：验证 enqueue 写入 dimension 列，claim 按 allowlist 过滤

    测试场景：
    1. 入队带 gitlab_instance 的任务，claim(instance_allowlist) 只取匹配的
    2. 入队带 tenant_id 的任务，claim(tenant_allowlist) 只取匹配的
    3. 入队不带 dimension 的任务，claim 带 allowlist 仍可取到（允许 NULL）
    """

    def _create_mock_conn(self):
        """创建 mock 数据库连接"""
        mock_cursor = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return mock_conn, mock_cursor

    def test_enqueue_writes_gitlab_instance_to_column(self):
        """enqueue 应将 gitlab_instance 从 payload 写入到 DB 列"""
        conn, cursor = self._create_mock_conn()
        cursor.fetchone.return_value = ("test-job-id-123",)

        from engram.logbook.scm_sync_queue import enqueue

        payload = {
            "gitlab_instance": "gitlab.example.com",
            "tenant_id": "tenant-abc",
            "mode": "incremental",
        }

        job_id = enqueue(
            repo_id=1,
            job_type="gitlab_commits",
            payload=payload,
            conn=conn,
        )

        assert job_id == "test-job-id-123"

        # 验证 SQL 调用包含 gitlab_instance 和 tenant_id 列
        call_args = cursor.execute.call_args
        sql = call_args[0][0]
        params = call_args[0][1]

        # SQL 应包含这些列
        assert "gitlab_instance" in sql
        assert "tenant_id" in sql

        # 参数应包含这些值
        assert "gitlab.example.com" in params
        assert "tenant-abc" in params

    def test_enqueue_handles_missing_dimensions(self):
        """enqueue 不带 dimension 字段时，应写入 NULL"""
        conn, cursor = self._create_mock_conn()
        cursor.fetchone.return_value = ("test-job-id-456",)

        from engram.logbook.scm_sync_queue import enqueue

        # 不带 gitlab_instance 和 tenant_id
        payload = {"mode": "incremental"}

        job_id = enqueue(
            repo_id=2,
            job_type="svn",
            payload=payload,
            conn=conn,
        )

        assert job_id == "test-job-id-456"

        # 参数应包含 None（NULL）
        call_args = cursor.execute.call_args
        params = call_args[0][1]

        # 最后两个参数应为 None（gitlab_instance, tenant_id）
        assert params[-2] is None  # gitlab_instance
        assert params[-1] is None  # tenant_id

    def test_claim_with_instance_allowlist_generates_correct_sql(self):
        """claim(instance_allowlist) 应生成包含列过滤的 SQL"""
        conn, cursor = self._create_mock_conn()
        cursor.fetchone.return_value = None  # 无可用任务

        from engram.logbook.scm_sync_queue import claim

        # 直接传入参数避免调用 get_claim_config
        result = claim(
            worker_id="test-worker",
            instance_allowlist=["gitlab.example.com", "gitlab2.example.com"],
            enable_tenant_fair_claim=False,
            max_consecutive_same_tenant=3,
            conn=conn,
        )

        assert result is None  # 无任务

        # 验证 SQL 包含 gitlab_instance 列过滤
        call_args = cursor.execute.call_args
        sql = call_args[0][0]
        params = call_args[0][1]

        # SQL 应包含列过滤条件
        assert "gitlab_instance IS NULL" in sql or "gitlab_instance IN" in sql

        # 参数应包含规范化的实例名
        assert "gitlab.example.com" in params
        assert "gitlab2.example.com" in params

    def test_claim_with_tenant_allowlist_generates_correct_sql(self):
        """claim(tenant_allowlist) 应生成包含列过滤的 SQL"""
        conn, cursor = self._create_mock_conn()
        cursor.fetchone.return_value = None

        from engram.logbook.scm_sync_queue import claim

        # 直接传入参数避免调用 get_claim_config
        result = claim(
            worker_id="test-worker",
            tenant_allowlist=["tenant-a", "tenant-b"],
            enable_tenant_fair_claim=False,
            max_consecutive_same_tenant=3,
            conn=conn,
        )

        assert result is None

        # 验证 SQL 包含 tenant_id 列过滤
        call_args = cursor.execute.call_args
        sql = call_args[0][0]
        params = call_args[0][1]

        # SQL 应包含列过滤条件
        assert "tenant_id IS NULL" in sql or "tenant_id IN" in sql

        # 参数应包含租户 ID
        assert "tenant-a" in params
        assert "tenant-b" in params

    def test_claim_combined_allowlists(self):
        """claim 同时使用 instance_allowlist 和 tenant_allowlist"""
        conn, cursor = self._create_mock_conn()
        cursor.fetchone.return_value = None

        from engram.logbook.scm_sync_queue import claim

        # 直接传入参数避免调用 get_claim_config
        result = claim(
            worker_id="test-worker",
            instance_allowlist=["gitlab.example.com"],
            tenant_allowlist=["tenant-x"],
            enable_tenant_fair_claim=False,
            max_consecutive_same_tenant=3,
            conn=conn,
        )

        assert result is None

        call_args = cursor.execute.call_args
        sql = call_args[0][0]
        params = call_args[0][1]

        # SQL 应同时包含两种过滤
        assert "gitlab_instance" in sql
        assert "tenant_id" in sql

        # 参数应包含两种过滤值
        assert "gitlab.example.com" in params
        assert "tenant-x" in params


class TestDimensionColumnPayloadConsistency:
    """测试 dimension 列与 payload_json 的一致性"""

    def test_payload_gitlab_instance_format(self):
        """gitlab_instance 在 payload 中应为规范化格式"""
        from engram.logbook.scm_sync_keys import normalize_instance_key

        # 验证规范化函数行为
        assert normalize_instance_key("https://gitlab.example.com/") == "gitlab.example.com"
        assert normalize_instance_key("GITLAB.EXAMPLE.COM") == "gitlab.example.com"
        assert normalize_instance_key("gitlab.example.com:443") == "gitlab.example.com"
        assert normalize_instance_key("gitlab.example.com:8080") == "gitlab.example.com:8080"

    def test_payload_dimension_fields_present(self):
        """mock payload 应包含 dimension 字段"""
        payload = create_mock_job_payload_incremental()

        # 验证 gitlab_instance 存在
        assert "gitlab_instance" in payload
        assert payload["gitlab_instance"] == "gitlab.example.com"

    def test_degradation_payload_includes_dimensions(self):
        """降级 payload 应包含 dimension 字段"""
        payload = create_mock_job_payload_with_degradation()

        assert "gitlab_instance" in payload
        assert payload["gitlab_instance"] == "gitlab.example.com"
