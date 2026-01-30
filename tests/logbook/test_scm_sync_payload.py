# -*- coding: utf-8 -*-
"""
SyncJobPayload 类型化 payload 单元测试

测试:
- SyncJobPayload.from_dict: 解析、未知字段保留、旧字段映射、新字段缺省
- SyncJobPayload.to_dict: 序列化、None 值省略
- parse_payload: 安全解析、解析失败返回错误
- claim 返回 typed payload、解析失败标记为 dead
"""

import json
import pytest
from unittest.mock import patch
import psycopg

from engram.logbook.scm_sync_payload import (
    SyncJobPayload,
    PayloadParseError,
    parse_payload_runtime as parse_payload,
)
from engram.logbook.scm_sync_errors import ErrorCategory


class TestSyncJobPayloadFromDict:
    """SyncJobPayload.from_dict 解析测试"""

    def test_from_dict_none(self):
        """None 解析为空 payload"""
        payload = SyncJobPayload.from_dict(None)
        assert payload.gitlab_instance is None
        assert payload.tenant_id is None
        assert payload.batch_size is None
        assert payload.is_backfill_only is False
        assert payload.extra == {}

    def test_from_dict_empty(self):
        """空 dict 解析为空 payload"""
        payload = SyncJobPayload.from_dict({})
        assert payload.gitlab_instance is None
        assert payload.verbose is False
        assert payload.extra == {}

    def test_from_dict_known_fields(self):
        """已知字段正确解析"""
        data = {
            "gitlab_instance": "gitlab.example.com",
            "tenant_id": "tenant-1",
            "batch_size": 100,
            "is_backfill_only": True,
            "verbose": True,
            "since": "2024-01-01T00:00:00Z",
            "until": 1704067200,  # 数字时间戳
        }
        payload = SyncJobPayload.from_dict(data)
        
        assert payload.gitlab_instance == "gitlab.example.com"
        assert payload.tenant_id == "tenant-1"
        assert payload.batch_size == 100
        assert payload.is_backfill_only is True
        assert payload.verbose is True
        assert payload.since == "2024-01-01T00:00:00Z"
        assert payload.until == 1704067200

    def test_from_dict_unknown_fields_preserved(self):
        """未知字段保留在 extra 中（向前兼容）"""
        data = {
            "gitlab_instance": "gitlab.example.com",
            "unknown_field_1": "value1",
            "unknown_field_2": {"nested": "value"},
            "future_feature": True,
        }
        payload = SyncJobPayload.from_dict(data)
        
        assert payload.gitlab_instance == "gitlab.example.com"
        assert payload.extra == {
            "unknown_field_1": "value1",
            "unknown_field_2": {"nested": "value"},
            "future_feature": True,
        }

    def test_from_dict_missing_fields_use_defaults(self):
        """缺失字段使用默认值（向后兼容）"""
        # 模拟旧任务只有部分字段
        data = {
            "page": 1,  # 旧字段
        }
        payload = SyncJobPayload.from_dict(data)
        
        assert payload.page == 1
        # 新字段使用默认值
        assert payload.suggested_batch_size is None
        assert payload.suggested_diff_mode is None
        assert payload.is_backfill_only is False
        assert payload.circuit_state is None

    def test_from_dict_type_coercion_int(self):
        """整数字段类型强制转换"""
        data = {
            "batch_size": "100",  # 字符串 -> 整数
            "start_rev": 123.0,   # 浮点 -> 整数
        }
        payload = SyncJobPayload.from_dict(data)
        
        assert payload.batch_size == 100
        assert payload.start_rev == 123

    def test_from_dict_type_coercion_bool(self):
        """布尔字段类型强制转换"""
        data = {
            "verbose": 1,  # 整数 -> 布尔
            "dry_run": "",  # 空字符串 -> False
        }
        payload = SyncJobPayload.from_dict(data)
        
        assert payload.verbose is True
        assert payload.dry_run is False

    def test_from_dict_invalid_int_raises_error(self):
        """无效整数类型抛出错误"""
        data = {
            "batch_size": "not_a_number",
        }
        with pytest.raises(PayloadParseError) as exc_info:
            SyncJobPayload.from_dict(data)
        
        assert "batch_size" in str(exc_info.value)
        assert "must be int" in str(exc_info.value)

    def test_from_dict_invalid_type_raises_error(self):
        """非 dict 类型抛出错误"""
        with pytest.raises(PayloadParseError) as exc_info:
            SyncJobPayload.from_dict("not a dict")
        
        assert "must be a dict" in str(exc_info.value)
        assert "str" in str(exc_info.value)

    def test_from_dict_list_raises_error(self):
        """列表类型抛出错误"""
        with pytest.raises(PayloadParseError) as exc_info:
            SyncJobPayload.from_dict([1, 2, 3])
        
        assert "must be a dict" in str(exc_info.value)


class TestSyncJobPayloadToDict:
    """SyncJobPayload.to_dict 序列化测试"""

    def test_to_dict_empty(self):
        """空 payload 序列化"""
        payload = SyncJobPayload()
        d = payload.to_dict()
        
        # None 值不应出现
        assert "gitlab_instance" not in d
        assert "tenant_id" not in d
        # False 布尔值应保留（与 None 区分）
        assert d.get("is_backfill_only") is False
        assert d.get("verbose") is False

    def test_to_dict_with_values(self):
        """有值 payload 序列化"""
        payload = SyncJobPayload(
            gitlab_instance="gitlab.example.com",
            batch_size=100,
            is_backfill_only=True,
        )
        d = payload.to_dict()
        
        assert d["gitlab_instance"] == "gitlab.example.com"
        assert d["batch_size"] == 100
        assert d["is_backfill_only"] is True

    def test_to_dict_extra_merged(self):
        """extra 字段合并到顶层"""
        payload = SyncJobPayload(
            gitlab_instance="gitlab.example.com",
            extra={"custom_field": "custom_value"},
        )
        d = payload.to_dict()
        
        assert d["gitlab_instance"] == "gitlab.example.com"
        assert d["custom_field"] == "custom_value"
        # extra 本身不应出现
        assert "extra" not in d

    def test_to_dict_roundtrip(self):
        """from_dict -> to_dict 往返测试"""
        original = {
            "gitlab_instance": "gitlab.example.com",
            "tenant_id": "tenant-1",
            "batch_size": 100,
            "is_backfill_only": True,
            "unknown_field": "preserved",
        }
        
        payload = SyncJobPayload.from_dict(original)
        result = payload.to_dict()
        
        # 已知字段保留
        assert result["gitlab_instance"] == "gitlab.example.com"
        assert result["batch_size"] == 100
        # 未知字段也保留
        assert result["unknown_field"] == "preserved"


class TestSyncJobPayloadDictLikeAccess:
    """SyncJobPayload dict-like 访问测试（向后兼容）"""

    def test_get_known_field(self):
        """get() 访问已知字段"""
        payload = SyncJobPayload(batch_size=100)
        
        assert payload.get("batch_size") == 100
        assert payload.get("batch_size", 50) == 100

    def test_get_unknown_field_from_extra(self):
        """get() 访问 extra 中的字段"""
        payload = SyncJobPayload(extra={"custom": "value"})
        
        assert payload.get("custom") == "value"
        assert payload.get("custom", "default") == "value"

    def test_get_missing_field_default(self):
        """get() 缺失字段返回默认值"""
        payload = SyncJobPayload()
        
        assert payload.get("batch_size") is None
        assert payload.get("batch_size", 50) == 50
        assert payload.get("nonexistent", "default") == "default"

    def test_getitem_known_field(self):
        """__getitem__ 访问已知字段"""
        payload = SyncJobPayload(batch_size=100)
        
        assert payload["batch_size"] == 100

    def test_getitem_extra_field(self):
        """__getitem__ 访问 extra 字段"""
        payload = SyncJobPayload(extra={"custom": "value"})
        
        assert payload["custom"] == "value"

    def test_getitem_missing_raises_keyerror(self):
        """__getitem__ 缺失字段抛出 KeyError"""
        payload = SyncJobPayload()
        
        with pytest.raises(KeyError):
            _ = payload["nonexistent"]

    def test_contains_known_field(self):
        """__contains__ 检查已知字段"""
        payload = SyncJobPayload(batch_size=100)
        
        assert "batch_size" in payload
        assert "gitlab_instance" not in payload  # None 值

    def test_contains_extra_field(self):
        """__contains__ 检查 extra 字段"""
        payload = SyncJobPayload(extra={"custom": "value"})
        
        assert "custom" in payload
        assert "nonexistent" not in payload


class TestParsePayload:
    """parse_payload 安全解析测试"""

    def test_parse_none(self):
        """解析 None"""
        payload, error = parse_payload(None)
        
        assert error is None
        assert isinstance(payload, SyncJobPayload)

    def test_parse_dict(self):
        """解析 dict"""
        payload, error = parse_payload({"batch_size": 100})
        
        assert error is None
        assert payload.batch_size == 100

    def test_parse_json_string(self):
        """解析 JSON 字符串"""
        payload, error = parse_payload('{"batch_size": 100}')
        
        assert error is None
        assert payload.batch_size == 100

    def test_parse_invalid_json_returns_error(self):
        """无效 JSON 返回错误"""
        payload, error = parse_payload("not valid json")
        
        assert payload is None
        assert error is not None
        assert "invalid JSON" in error

    def test_parse_invalid_type_returns_error(self):
        """无效类型返回错误"""
        payload, error = parse_payload({"batch_size": "not_a_number"})
        
        assert payload is None
        assert error is not None
        assert "contract mismatch" in error

    def test_parse_already_typed_payload(self):
        """解析已经是 SyncJobPayload 的对象"""
        original = SyncJobPayload(batch_size=100)
        payload, error = parse_payload(original)
        
        assert error is None
        assert payload is original


class TestClaimWithTypedPayload:
    """claim 返回 typed payload 测试（需要数据库）"""

    def test_claim_returns_typed_payload(self, migrated_db):
        """claim 返回 typed payload"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_typed_payload.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # 创建带 payload 的任务
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        payload_json
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'pending',
                            %s::jsonb)
                """, (repo_id, json.dumps({
                    "gitlab_instance": "gitlab.example.com",
                    "batch_size": 100,
                    "unknown_field": "preserved",
                })))

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import claim, ack

                job = claim(worker_id="worker-1")
                
                assert job is not None
                # payload 应该是 SyncJobPayload
                payload = job["payload"]
                assert isinstance(payload, SyncJobPayload)
                # 已知字段正确解析
                assert payload.gitlab_instance == "gitlab.example.com"
                assert payload.batch_size == 100
                # 未知字段保留
                assert payload.extra.get("unknown_field") == "preserved"
                # 原始 dict 也保留
                assert "payload_raw" in job
                
                ack(job["job_id"], "worker-1")

        finally:
            if repo_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_claim_invalid_payload_marks_dead(self, migrated_db):
        """claim 解析失败时标记为 dead"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_invalid_payload.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # 创建带无效 payload 的任务（batch_size 是无效类型）
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        payload_json
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'pending',
                            %s::jsonb)
                    RETURNING job_id
                """, (repo_id, json.dumps({
                    "batch_size": "not_a_number",  # 无效类型
                })))
                job_id = str(cur.fetchone()[0])

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import claim, get_job

                # claim 应该返回 None（任务被标记为 dead）
                job = claim(worker_id="worker-1")
                assert job is None
                
                mock_conn.close()
            
            # 验证任务被标记为 dead
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT status, last_error
                    FROM {scm_schema}.sync_jobs
                    WHERE job_id = %s
                """, (job_id,))
                row = cur.fetchone()
                
                assert row is not None
                assert row[0] == "dead"
                assert ErrorCategory.PAYLOAD_CONTRACT_MISMATCH.value in row[1]

        finally:
            if repo_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestEnqueueWithTypedPayload:
    """enqueue 支持 typed payload 测试（需要数据库）"""

    def test_enqueue_with_typed_payload(self, migrated_db):
        """enqueue 接受 SyncJobPayload"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_enqueue_typed.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import enqueue, get_job

                # 使用 typed payload 入队
                typed_payload = SyncJobPayload(
                    gitlab_instance="gitlab.example.com",
                    batch_size=100,
                    is_backfill_only=True,
                    extra={"custom_field": "custom_value"},
                )
                
                job_id = enqueue(
                    repo_id=repo_id,
                    job_type="gitlab_commits",
                    payload=typed_payload,
                )
                
                assert job_id is not None
                
                # 验证 payload 正确存储
                job = get_job(job_id)
                assert job is not None
                # get_job 默认返回 dict
                payload = job["payload"]
                assert payload["gitlab_instance"] == "gitlab.example.com"
                assert payload["batch_size"] == 100
                assert payload["is_backfill_only"] is True
                assert payload["custom_field"] == "custom_value"

        finally:
            if job_id or repo_id:
                with conn.cursor() as cur:
                    if job_id:
                        cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                    if repo_id:
                        cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_enqueue_with_dict_payload(self, migrated_db):
        """enqueue 仍然接受 dict payload（向后兼容）"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_enqueue_dict.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import enqueue, get_job

                # 使用 dict payload 入队（向后兼容）
                job_id = enqueue(
                    repo_id=repo_id,
                    job_type="gitlab_commits",
                    payload={"batch_size": 100},
                )
                
                assert job_id is not None
                
                job = get_job(job_id)
                assert job["payload"]["batch_size"] == 100

        finally:
            if job_id or repo_id:
                with conn.cursor() as cur:
                    if job_id:
                        cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                    if repo_id:
                        cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestSchedulerPayloadFields:
    """Scheduler 注入的调度信息字段测试"""

    def test_payload_contains_reason_field(self):
        """payload 包含 reason 字段"""
        # 模拟 scheduler 构建的 payload
        payload_dict = {
            "reason": "cursor_age_exceeded",
            "cursor_age_seconds": 3600,
            "failure_rate": 0.1,
            "rate_limit_rate": 0.05,
            "scheduled_at": "2024-01-15T10:00:00+00:00",
        }
        
        payload = SyncJobPayload.from_dict(payload_dict)
        result = payload.to_dict()
        
        # reason 应该保留在 extra 中并在 to_dict 时合并
        assert result.get("reason") == "cursor_age_exceeded"
        assert result.get("cursor_age_seconds") == 3600
        assert result.get("failure_rate") == 0.1
        assert result.get("scheduled_at") == "2024-01-15T10:00:00+00:00"

    def test_payload_contains_budget_snapshot(self):
        """payload 包含 budget_snapshot 字段"""
        budget_snapshot = {
            "global_running": 5,
            "global_pending": 10,
            "global_active": 15,
            "by_instance": {"gitlab.example.com": 3},
            "by_tenant": {"tenant-a": 2},
        }
        
        payload_dict = {
            "reason": "incremental_due",
            "budget_snapshot": budget_snapshot,
        }
        
        payload = SyncJobPayload.from_dict(payload_dict)
        result = payload.to_dict()
        
        # budget_snapshot 应该保留
        assert "budget_snapshot" in result
        assert result["budget_snapshot"]["global_running"] == 5
        assert result["budget_snapshot"]["global_pending"] == 10
        assert result["budget_snapshot"]["global_active"] == 15
        assert result["budget_snapshot"]["by_instance"]["gitlab.example.com"] == 3
        assert result["budget_snapshot"]["by_tenant"]["tenant-a"] == 2

    def test_payload_contains_circuit_breaker_decision(self):
        """payload 包含 circuit_breaker_decision 字段"""
        circuit_decision = {
            "current_state": "closed",
            "allow_sync": True,
            "trigger_reason": None,
            "is_backfill_only": False,
            "is_probe_mode": False,
            "probe_budget": 5,
            "wait_seconds": 0,
        }
        
        payload_dict = {
            "reason": "incremental_due",
            "circuit_breaker_decision": circuit_decision,
        }
        
        payload = SyncJobPayload.from_dict(payload_dict)
        result = payload.to_dict()
        
        # circuit_breaker_decision 应该保留
        assert "circuit_breaker_decision" in result
        assert result["circuit_breaker_decision"]["current_state"] == "closed"
        assert result["circuit_breaker_decision"]["allow_sync"] is True
        assert result["circuit_breaker_decision"]["is_backfill_only"] is False

    def test_payload_full_scheduler_injection(self):
        """完整的 scheduler 注入 payload 测试"""
        # 模拟 scheduler 完整构建的 payload
        payload_dict = {
            "reason": "cursor_age_exceeded",
            "cursor_age_seconds": 7200,
            "failure_rate": 0.05,
            "rate_limit_rate": 0.02,
            "scheduled_at": "2024-01-15T10:00:00+00:00",
            "logical_job_type": "commits",
            "physical_job_type": "gitlab_commits",
            "gitlab_instance": "gitlab.example.com",
            "tenant_id": "tenant-acme",
            "suggested_batch_size": 100,
            "suggested_forward_window_seconds": 3600,
            "suggested_diff_mode": "best_effort",
            "budget_snapshot": {
                "global_running": 3,
                "global_pending": 7,
                "global_active": 10,
                "by_instance": {"gitlab.example.com": 2},
                "by_tenant": {"tenant-acme": 1},
            },
            "circuit_breaker_decision": {
                "current_state": "closed",
                "allow_sync": True,
                "trigger_reason": None,
                "is_backfill_only": False,
                "is_probe_mode": False,
                "probe_budget": 5,
                "wait_seconds": 0,
            },
        }
        
        payload = SyncJobPayload.from_dict(payload_dict)
        
        # 已知字段应该直接访问
        assert payload.gitlab_instance == "gitlab.example.com"
        assert payload.tenant_id == "tenant-acme"
        assert payload.suggested_batch_size == 100
        assert payload.suggested_diff_mode == "best_effort"
        
        # extra 字段应该包含调度信息
        assert payload.extra.get("reason") == "cursor_age_exceeded"
        assert payload.extra.get("cursor_age_seconds") == 7200
        assert "budget_snapshot" in payload.extra
        assert "circuit_breaker_decision" in payload.extra
        
        # 验证往返不丢失数据
        result = payload.to_dict()
        assert result.get("reason") == "cursor_age_exceeded"
        assert result.get("budget_snapshot") is not None
        assert result.get("circuit_breaker_decision") is not None

    def test_payload_schema_validation_budget_snapshot(self):
        """budget_snapshot 字段 schema 验证"""
        # 验证必需的子字段
        valid_budget = {
            "global_running": 0,
            "global_pending": 0,
            "global_active": 0,
            "by_instance": {},
            "by_tenant": {},
        }
        
        payload_dict = {"budget_snapshot": valid_budget}
        payload = SyncJobPayload.from_dict(payload_dict)
        result = payload.to_dict()
        
        budget = result.get("budget_snapshot")
        assert budget is not None
        assert "global_running" in budget
        assert "global_pending" in budget
        assert "global_active" in budget
        assert "by_instance" in budget
        assert "by_tenant" in budget

    def test_payload_schema_validation_circuit_breaker_decision(self):
        """circuit_breaker_decision 字段 schema 验证"""
        # 验证必需的子字段
        valid_decision = {
            "current_state": "half_open",
            "allow_sync": True,
            "trigger_reason": "failure_rate_exceeded",
            "is_backfill_only": True,
            "is_probe_mode": True,
            "probe_budget": 3,
            "wait_seconds": 60,
        }
        
        payload_dict = {"circuit_breaker_decision": valid_decision}
        payload = SyncJobPayload.from_dict(payload_dict)
        result = payload.to_dict()
        
        decision = result.get("circuit_breaker_decision")
        assert decision is not None
        assert "current_state" in decision
        assert "allow_sync" in decision
        assert "is_backfill_only" in decision
        assert "is_probe_mode" in decision
        assert decision["current_state"] == "half_open"
        assert decision["is_probe_mode"] is True


class TestChunkFieldMapping:
    """测试 chunk 字段名映射（chunk_index <-> current_chunk）"""

    def test_chunk_index_maps_to_current_chunk(self):
        """chunk_index 映射到 current_chunk"""
        payload = SyncJobPayload.from_dict({
            "chunk_index": 2,
            "chunk_total": 5,
        })
        
        assert payload.chunk_index == 2
        assert payload.current_chunk == 2  # 自动映射
        assert payload.chunk_total == 5
        assert payload.total_chunks == 5  # 自动映射

    def test_current_chunk_maps_to_chunk_index(self):
        """current_chunk 映射到 chunk_index"""
        payload = SyncJobPayload.from_dict({
            "current_chunk": 3,
            "total_chunks": 10,
        })
        
        assert payload.current_chunk == 3
        assert payload.chunk_index == 3  # 自动映射
        assert payload.total_chunks == 10
        assert payload.chunk_total == 10  # 自动映射

    def test_both_formats_present_prefer_explicit(self):
        """同时存在两种格式时保留各自的值"""
        payload = SyncJobPayload.from_dict({
            "chunk_index": 1,
            "current_chunk": 2,  # 不同的值
            "chunk_total": 5,
            "total_chunks": 6,
        })
        
        # 两种格式都保留
        assert payload.chunk_index == 1
        assert payload.current_chunk == 2
        assert payload.chunk_total == 5
        assert payload.total_chunks == 6

    def test_window_chunk_fields(self):
        """测试分块窗口字段解析"""
        payload = SyncJobPayload.from_dict({
            "window_type": "time",
            "window_since": "2024-01-01T00:00:00+00:00",
            "window_until": "2024-01-02T00:00:00+00:00",
            "chunk_index": 0,
            "chunk_total": 3,
        })
        
        assert payload.window_type == "time"
        assert payload.window_since == "2024-01-01T00:00:00+00:00"
        assert payload.window_until == "2024-01-02T00:00:00+00:00"
        assert payload.chunk_index == 0
        assert payload.chunk_total == 3

    def test_revision_window_chunk_fields(self):
        """测试 SVN revision 分块窗口字段解析"""
        payload = SyncJobPayload.from_dict({
            "window_type": "revision",
            "window_start_rev": 1000,
            "window_end_rev": 1100,
            "chunk_index": 1,
            "chunk_total": 5,
        })
        
        assert payload.window_type == "revision"
        assert payload.window_start_rev == 1000
        assert payload.window_end_rev == 1100
        assert payload.chunk_index == 1
        assert payload.chunk_total == 5

    def test_roundtrip_preserves_chunk_fields(self):
        """往返转换保留分块字段"""
        original = {
            "window_type": "time",
            "window_since": "2024-01-01T00:00:00+00:00",
            "window_until": "2024-01-02T00:00:00+00:00",
            "chunk_index": 0,
            "chunk_total": 3,
            "update_watermark": True,
        }
        
        payload = SyncJobPayload.from_dict(original)
        result = payload.to_dict()
        
        assert result["window_type"] == "time"
        assert result["window_since"] == "2024-01-01T00:00:00+00:00"
        assert result["chunk_index"] == 0
        assert result["chunk_total"] == 3

    def test_suggested_forward_window_seconds(self):
        """测试 suggested_forward_window_seconds 字段"""
        payload = SyncJobPayload.from_dict({
            "suggested_forward_window_seconds": 3600,
            "suggested_batch_size": 50,
        })
        
        assert payload.suggested_forward_window_seconds == 3600
        assert payload.suggested_batch_size == 50


class TestSchedulerSvnJobTypeFiltering:
    """
    Scheduler SVN 任务类型过滤测试
    
    验证 scheduler 不会为 SVN 仓库产生 mrs/reviews 任务。
    SVN 仓库仅支持 commits（映射为 physical_job_type='svn'）。
    """

    def test_logical_to_physical_svn_commits(self):
        """SVN commits 正确映射为 svn physical type"""
        from engram.logbook.scm_sync_job_types import logical_to_physical
        
        result = logical_to_physical("commits", "svn")
        assert result == "svn"

    def test_logical_to_physical_svn_mrs_raises_error(self):
        """SVN mrs 映射应该抛出 ValueError"""
        from engram.logbook.scm_sync_job_types import logical_to_physical
        
        with pytest.raises(ValueError) as exc_info:
            logical_to_physical("mrs", "svn")
        
        assert "SVN" in str(exc_info.value)
        assert "commits" in str(exc_info.value)

    def test_logical_to_physical_svn_reviews_raises_error(self):
        """SVN reviews 映射应该抛出 ValueError"""
        from engram.logbook.scm_sync_job_types import logical_to_physical
        
        with pytest.raises(ValueError) as exc_info:
            logical_to_physical("reviews", "svn")
        
        assert "SVN" in str(exc_info.value)
        assert "commits" in str(exc_info.value)

    def test_get_logical_job_types_for_svn(self):
        """SVN 仓库仅返回 commits logical job type"""
        from engram.logbook.scm_sync_job_types import get_logical_job_types_for_repo
        
        job_types = get_logical_job_types_for_repo("svn")
        
        assert job_types == ["commits"]
        assert "mrs" not in job_types
        assert "reviews" not in job_types

    def test_get_physical_job_types_for_svn(self):
        """SVN 仓库仅返回 svn physical job type"""
        from engram.logbook.scm_sync_job_types import get_physical_job_types_for_repo
        
        job_types = get_physical_job_types_for_repo("svn")
        
        assert job_types == ["svn"]
        assert "gitlab_commits" not in job_types
        assert "gitlab_mrs" not in job_types
        assert "gitlab_reviews" not in job_types

    def test_get_logical_job_types_for_git(self):
        """Git 仓库返回 commits/mrs/reviews logical job types"""
        from engram.logbook.scm_sync_job_types import get_logical_job_types_for_repo
        
        job_types = get_logical_job_types_for_repo("git")
        
        assert "commits" in job_types
        assert "mrs" in job_types
        assert "reviews" in job_types

    def test_get_physical_job_types_for_git(self):
        """Git 仓库返回 gitlab_* physical job types"""
        from engram.logbook.scm_sync_job_types import get_physical_job_types_for_repo
        
        job_types = get_physical_job_types_for_repo("git")
        
        assert "gitlab_commits" in job_types
        assert "gitlab_mrs" in job_types
        assert "gitlab_reviews" in job_types
        assert "svn" not in job_types

    def test_scheduler_does_not_produce_svn_mrs_reviews(self):
        """
        验证 scheduler 逻辑不会为 SVN 仓库产生 mrs/reviews 任务
        
        模拟 scheduler 的 job 类型选择逻辑：
        1. 根据 repo_type 获取支持的 logical job types
        2. 转换为 physical job types
        3. 验证 SVN 仓库不会产生 mrs/reviews 相关的任务
        """
        from engram.logbook.scm_sync_job_types import (
            get_logical_job_types_for_repo,
            logical_to_physical,
        )
        
        # 模拟 SVN 仓库的 scheduler 逻辑
        svn_logical_types = get_logical_job_types_for_repo("svn")
        svn_physical_types = []
        
        for logical_type in svn_logical_types:
            try:
                physical_type = logical_to_physical(logical_type, "svn")
                svn_physical_types.append(physical_type)
            except ValueError:
                # 非法组合被过滤，不应该发生因为 get_logical_job_types_for_repo 已过滤
                pass
        
        # SVN 仓库只应该产生 'svn' 任务
        assert svn_physical_types == ["svn"]
        
        # 验证不会产生 mrs/reviews
        assert "gitlab_mrs" not in svn_physical_types
        assert "gitlab_reviews" not in svn_physical_types
        
        # 模拟 Git 仓库的 scheduler 逻辑
        git_logical_types = get_logical_job_types_for_repo("git")
        git_physical_types = []
        
        for logical_type in git_logical_types:
            try:
                physical_type = logical_to_physical(logical_type, "git")
                git_physical_types.append(physical_type)
            except ValueError:
                pass
        
        # Git 仓库应该产生 gitlab_commits, gitlab_mrs, gitlab_reviews
        assert "gitlab_commits" in git_physical_types
        assert "gitlab_mrs" in git_physical_types
        assert "gitlab_reviews" in git_physical_types

    def test_scheduler_invalid_combination_filtered(self):
        """
        验证非法组合（如 svn+mrs）在 scheduler 中被过滤
        
        模拟如果由于某种原因（比如配置错误）尝试为 SVN 仓库创建 mrs 任务，
        logical_to_physical 会抛出异常，scheduler 应捕获并跳过。
        """
        from engram.logbook.scm_sync_job_types import logical_to_physical
        
        # 模拟 scheduler 的异常处理逻辑
        invalid_combinations = [
            ("mrs", "svn"),
            ("reviews", "svn"),
        ]
        
        skipped_jobs = []
        for logical_type, repo_type in invalid_combinations:
            try:
                logical_to_physical(logical_type, repo_type)
            except ValueError as e:
                # scheduler 应捕获此异常并跳过
                skipped_jobs.append((logical_type, repo_type, str(e)))
        
        # 所有非法组合都应该被跳过
        assert len(skipped_jobs) == 2
        
        # 验证错误信息包含有用的提示
        for logical_type, repo_type, error_msg in skipped_jobs:
            assert "SVN" in error_msg
            assert "commits" in error_msg
