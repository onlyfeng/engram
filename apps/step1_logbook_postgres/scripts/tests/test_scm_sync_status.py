# -*- coding: utf-8 -*-
"""
test_scm_sync_status.py - SCM 同步状态查询功能测试

测试内容:
- db.py 中的只读查询函数
- scm_sync_status.py CLI 输出结构验证

测试策略:
- 使用 migrated_db fixture 确保表结构存在
- 插入少量测试数据
- 验证查询结果的结构（字段名、类型）
"""

import json
import uuid
from datetime import datetime, timezone

import pytest
import psycopg

from db import (
    get_conn,
    upsert_repo,
    list_repos,
    list_sync_runs,
    list_sync_jobs,
    list_sync_locks,
    list_kv_cursors,
    get_sync_status_summary,
    insert_sync_run_start,
    insert_sync_run_finish,
    enqueue_sync_job,
    save_circuit_breaker_state,
    build_circuit_breaker_key,
)

from kv import kv_set_json


# ============ 基础结构测试 ============


class TestListRepos:
    """测试 list_repos 函数"""

    def test_list_repos_empty(self, db_conn):
        """空表时返回空列表"""
        result = list_repos(db_conn)
        assert isinstance(result, list)

    def test_list_repos_structure(self, db_conn):
        """验证返回结果的结构"""
        # 插入测试数据
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url="https://example.com/test-repo.git",
            project_key="test_project",
            default_branch="main",
        )
        
        result = list_repos(db_conn)
        assert len(result) >= 1
        
        # 验证字段结构
        repo = next(r for r in result if r["repo_id"] == repo_id)
        assert "repo_id" in repo
        assert "repo_type" in repo
        assert "url" in repo
        assert "project_key" in repo
        assert "default_branch" in repo
        assert "created_at" in repo
        
        # 验证类型
        assert isinstance(repo["repo_id"], int)
        assert repo["repo_type"] in ("git", "svn")
        assert isinstance(repo["url"], str)

    def test_list_repos_filter_by_type(self, db_conn):
        """测试按类型过滤"""
        # 插入 git 仓库
        upsert_repo(
            db_conn,
            repo_type="git",
            url="https://example.com/git-repo.git",
            project_key="test",
        )
        
        # 按类型过滤
        git_repos = list_repos(db_conn, repo_type="git")
        svn_repos = list_repos(db_conn, repo_type="svn")
        
        for repo in git_repos:
            assert repo["repo_type"] == "git"
        
        for repo in svn_repos:
            assert repo["repo_type"] == "svn"


class TestListSyncRuns:
    """测试 list_sync_runs 函数"""

    def test_list_sync_runs_empty(self, db_conn):
        """空表时返回空列表"""
        result = list_sync_runs(db_conn)
        assert isinstance(result, list)

    def test_list_sync_runs_structure(self, db_conn):
        """验证返回结果的结构"""
        # 先创建 repo
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url="https://example.com/sync-test.git",
            project_key="test",
        )
        
        # 创建 sync_run
        run_id = str(uuid.uuid4())
        insert_sync_run_start(
            db_conn,
            run_id=run_id,
            repo_id=repo_id,
            job_type="gitlab_commits",
            mode="incremental",
            cursor_before={"last_sha": "abc123"},
            meta_json={"test": True},
        )
        
        result = list_sync_runs(db_conn, repo_id=repo_id)
        assert len(result) >= 1
        
        # 验证字段结构
        run = next(r for r in result if str(r["run_id"]) == run_id)
        expected_fields = [
            "run_id", "repo_id", "job_type", "mode", "status",
            "started_at", "finished_at",
            "cursor_before", "cursor_after",
            "counts", "error_summary_json", "degradation_json",
            "logbook_item_id", "meta_json", "synced_count",
        ]
        for field in expected_fields:
            assert field in run, f"缺少字段: {field}"
        
        # 验证类型
        assert run["status"] == "running"
        assert run["job_type"] == "gitlab_commits"

    def test_list_sync_runs_filter(self, db_conn):
        """测试过滤功能"""
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url="https://example.com/filter-test.git",
            project_key="test",
        )
        
        # 创建两个不同状态的 run
        run1 = str(uuid.uuid4())
        insert_sync_run_start(db_conn, run1, repo_id, "gitlab_commits")
        
        run2 = str(uuid.uuid4())
        insert_sync_run_start(db_conn, run2, repo_id, "gitlab_mrs")
        insert_sync_run_finish(db_conn, run2, status="completed")
        
        # 按状态过滤
        running = list_sync_runs(db_conn, repo_id=repo_id, status="running")
        completed = list_sync_runs(db_conn, repo_id=repo_id, status="completed")
        
        running_ids = [str(r["run_id"]) for r in running]
        completed_ids = [str(r["run_id"]) for r in completed]
        
        assert run1 in running_ids
        assert run2 in completed_ids


class TestListSyncJobs:
    """测试 list_sync_jobs 函数"""

    def test_list_sync_jobs_empty(self, db_conn):
        """空表时返回空列表"""
        result = list_sync_jobs(db_conn)
        assert isinstance(result, list)

    def test_list_sync_jobs_structure(self, db_conn):
        """验证返回结果的结构"""
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url="https://example.com/job-test.git",
            project_key="test",
        )
        
        # 入队任务
        job_id = enqueue_sync_job(
            db_conn,
            repo_id=repo_id,
            job_type="gitlab_commits",
            priority=50,
            payload_json={"source": "test"},
        )
        
        result = list_sync_jobs(db_conn, repo_id=repo_id)
        assert len(result) >= 1
        
        # 验证字段结构
        job = next(j for j in result if str(j["job_id"]) == job_id)
        expected_fields = [
            "job_id", "repo_id", "job_type", "mode", "status",
            "priority", "attempts", "max_attempts",
            "not_before", "locked_by", "locked_at", "lease_seconds",
            "last_error", "last_run_id",
            "payload_json", "created_at", "updated_at",
        ]
        for field in expected_fields:
            assert field in job, f"缺少字段: {field}"
        
        # 验证值
        assert job["status"] == "pending"
        assert job["priority"] == 50


class TestListSyncLocks:
    """测试 list_sync_locks 函数"""

    def test_list_sync_locks_empty(self, db_conn):
        """空表时返回空列表"""
        result = list_sync_locks(db_conn)
        assert isinstance(result, list)

    def test_list_sync_locks_structure(self, db_conn):
        """验证返回结果的结构（需要先创建锁记录）"""
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url="https://example.com/lock-test.git",
            project_key="test",
        )
        
        # 手动插入锁记录
        with db_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO sync_locks (repo_id, job_type, locked_by, locked_at, lease_seconds)
                VALUES (%s, %s, %s, now(), %s)
                ON CONFLICT (repo_id, job_type) DO UPDATE
                SET locked_by = EXCLUDED.locked_by, locked_at = now()
            """, (repo_id, "gitlab_commits", "worker-test-001", 60))
        
        result = list_sync_locks(db_conn, repo_id=repo_id)
        assert len(result) >= 1
        
        # 验证字段结构
        lock = result[0]
        expected_fields = [
            "lock_id", "repo_id", "job_type",
            "locked_by", "locked_at", "lease_seconds",
            "updated_at", "created_at",
            "is_locked", "is_expired",
        ]
        for field in expected_fields:
            assert field in lock, f"缺少字段: {field}"
        
        # 验证计算字段
        assert lock["is_locked"] is True
        assert lock["is_expired"] is False

    def test_list_sync_locks_filter_expired(self, db_conn):
        """测试过期锁过滤"""
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url="https://example.com/expired-lock.git",
            project_key="test",
        )
        
        # 插入一个"过期"的锁（locked_at 很早）
        with db_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO sync_locks (repo_id, job_type, locked_by, locked_at, lease_seconds)
                VALUES (%s, %s, %s, now() - interval '2 hours', %s)
                ON CONFLICT (repo_id, job_type) DO UPDATE
                SET locked_by = EXCLUDED.locked_by, 
                    locked_at = now() - interval '2 hours',
                    lease_seconds = EXCLUDED.lease_seconds
            """, (repo_id, "gitlab_commits", "old-worker", 60))
        
        # 查询过期锁
        expired = list_sync_locks(db_conn, repo_id=repo_id, expired_only=True)
        assert len(expired) >= 1
        assert all(lock["is_expired"] for lock in expired)


class TestListKvCursors:
    """测试 list_kv_cursors 函数"""

    def test_list_kv_cursors_empty(self, db_conn):
        """空结果时返回空列表"""
        result = list_kv_cursors(db_conn, namespace="scm.sync.nonexistent")
        assert isinstance(result, list)
        assert len(result) == 0

    def test_list_kv_cursors_structure(self, db_conn):
        """验证返回结果的结构"""
        # 插入游标数据
        kv_set_json(
            db_conn,
            namespace="scm.sync",
            key="gitlab_cursor:999",
            value={"last_sha": "abc123", "last_ts": "2024-01-01T00:00:00Z"},
        )
        
        result = list_kv_cursors(db_conn, namespace="scm.sync")
        assert len(result) >= 1
        
        # 验证字段结构
        cursor = next(c for c in result if c["key"] == "gitlab_cursor:999")
        assert "namespace" in cursor
        assert "key" in cursor
        assert "value_json" in cursor
        assert "updated_at" in cursor
        
        # 验证值类型
        assert cursor["namespace"] == "scm.sync"
        assert isinstance(cursor["value_json"], dict)

    def test_list_kv_cursors_filter_prefix(self, db_conn):
        """测试前缀过滤"""
        # 插入不同类型的游标
        kv_set_json(db_conn, "scm.sync", "gitlab_cursor:1", {"type": "gitlab"})
        kv_set_json(db_conn, "scm.sync", "svn_cursor:2", {"type": "svn"})
        
        # 按前缀过滤
        gitlab = list_kv_cursors(db_conn, namespace="scm.sync", key_prefix="gitlab")
        svn = list_kv_cursors(db_conn, namespace="scm.sync", key_prefix="svn")
        
        for cursor in gitlab:
            assert cursor["key"].startswith("gitlab")
        
        for cursor in svn:
            assert cursor["key"].startswith("svn")


class TestGetSyncStatusSummary:
    """测试 get_sync_status_summary 函数"""

    def test_summary_structure(self, db_conn):
        """验证摘要结果的结构"""
        result = get_sync_status_summary(db_conn)
        
        # 验证必需字段
        assert "repos_count" in result
        assert "repos_by_type" in result
        assert "runs_24h_by_status" in result
        assert "jobs_by_status" in result
        assert "locks" in result
        assert "cursors_count" in result
        
        # 验证类型
        assert isinstance(result["repos_count"], int)
        assert isinstance(result["repos_by_type"], dict)
        assert isinstance(result["locks"], dict)
        assert "active" in result["locks"]
        assert "expired" in result["locks"]

    def test_summary_counts(self, db_conn):
        """验证摘要计数正确"""
        # 插入一些测试数据
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url="https://example.com/summary-test.git",
            project_key="test",
        )
        
        # 插入游标
        kv_set_json(db_conn, "scm.sync", "test_cursor:summary", {"test": True})
        
        result = get_sync_status_summary(db_conn)
        
        # 验证至少有一个仓库
        assert result["repos_count"] >= 1
        assert result["cursors_count"] >= 1


# ============ 快照测试（输出结构） ============


class TestOutputStructureSnapshot:
    """
    验证输出结构的快照测试
    
    确保关键查询返回的字段不会意外变化
    """

    def test_repos_fields_snapshot(self, db_conn):
        """repos 查询的字段快照"""
        upsert_repo(db_conn, "git", "https://snap.test/repo", "snap")
        result = list_repos(db_conn, limit=1)
        
        if result:
            expected_fields = {
                "repo_id", "repo_type", "url", 
                "project_key", "default_branch", "created_at"
            }
            actual_fields = set(result[0].keys())
            assert expected_fields == actual_fields, (
                f"字段不匹配: 期望 {expected_fields}, 实际 {actual_fields}"
            )

    def test_sync_runs_fields_snapshot(self, db_conn):
        """sync_runs 查询的字段快照"""
        repo_id = upsert_repo(db_conn, "git", "https://snap.test/runs", "snap")
        run_id = str(uuid.uuid4())
        insert_sync_run_start(db_conn, run_id, repo_id, "gitlab_commits")
        
        result = list_sync_runs(db_conn, repo_id=repo_id, limit=1)
        
        if result:
            expected_fields = {
                "run_id", "repo_id", "job_type", "mode", "status",
                "started_at", "finished_at",
                "cursor_before", "cursor_after",
                "counts", "error_summary_json", "degradation_json",
                "logbook_item_id", "meta_json", "synced_count",
            }
            actual_fields = set(result[0].keys())
            assert expected_fields == actual_fields, (
                f"字段不匹配: 期望 {expected_fields}, 实际 {actual_fields}"
            )

    def test_sync_jobs_fields_snapshot(self, db_conn):
        """sync_jobs 查询的字段快照"""
        repo_id = upsert_repo(db_conn, "git", "https://snap.test/jobs", "snap")
        enqueue_sync_job(db_conn, repo_id, "gitlab_commits")
        
        result = list_sync_jobs(db_conn, repo_id=repo_id, limit=1)
        
        if result:
            expected_fields = {
                "job_id", "repo_id", "job_type", "mode", "status",
                "priority", "attempts", "max_attempts",
                "not_before", "locked_by", "locked_at", "lease_seconds",
                "last_error", "last_run_id",
                "payload_json", "created_at", "updated_at",
            }
            actual_fields = set(result[0].keys())
            assert expected_fields == actual_fields, (
                f"字段不匹配: 期望 {expected_fields}, 实际 {actual_fields}"
            )

    def test_sync_locks_fields_snapshot(self, db_conn):
        """sync_locks 查询的字段快照"""
        repo_id = upsert_repo(db_conn, "git", "https://snap.test/locks", "snap")
        
        with db_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO sync_locks (repo_id, job_type, lease_seconds)
                VALUES (%s, %s, %s)
                ON CONFLICT (repo_id, job_type) DO NOTHING
            """, (repo_id, "gitlab_commits", 60))
        
        result = list_sync_locks(db_conn, repo_id=repo_id)
        
        if result:
            expected_fields = {
                "lock_id", "repo_id", "job_type",
                "locked_by", "locked_at", "lease_seconds",
                "updated_at", "created_at",
                "is_locked", "is_expired",
            }
            actual_fields = set(result[0].keys())
            assert expected_fields == actual_fields, (
                f"字段不匹配: 期望 {expected_fields}, 实际 {actual_fields}"
            )

    def test_kv_cursors_fields_snapshot(self, db_conn):
        """kv_cursors 查询的字段快照"""
        kv_set_json(db_conn, "scm.sync", "snapshot_test", {"test": True})
        
        result = list_kv_cursors(db_conn, namespace="scm.sync", key_prefix="snapshot")
        
        if result:
            expected_fields = {"namespace", "key", "value_json", "updated_at"}
            actual_fields = set(result[0].keys())
            assert expected_fields == actual_fields, (
                f"字段不匹配: 期望 {expected_fields}, 实际 {actual_fields}"
            )

    def test_summary_fields_snapshot(self, db_conn):
        """summary 查询的字段快照（db.py 的 get_sync_status_summary）"""
        result = get_sync_status_summary(db_conn)
        
        expected_fields = {
            "repos_count", "repos_by_type",
            "runs_24h_by_status", "jobs_by_status",
            "locks", "cursors_count",
        }
        actual_fields = set(result.keys())
        assert expected_fields == actual_fields, (
            f"字段不匹配: 期望 {expected_fields}, 实际 {actual_fields}"
        )
        
        # 验证 locks 子结构
        assert set(result["locks"].keys()) == {"active", "expired"}

    def test_scm_sync_status_summary_fields_snapshot(self, db_conn):
        """scm_sync_status.py get_sync_summary 的固定 schema 字段快照"""
        import sys
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from scm_sync_status import get_sync_summary
        
        result = get_sync_summary(db_conn)
        
        # 验证固定 schema 必需字段
        required_fields = {
            # 新固定 schema 字段
            "circuit_breakers",
            "rate_limit_buckets", 
            "error_budget",
            "pauses_by_reason",
            # 原有稳定字段
            "repos_count", "repos_by_type",
            "jobs", "jobs_by_status",
            "expired_locks", "locks",
            "cursors_count",
            "runs_24h_by_status",
            "window_stats",
            "by_instance", "by_tenant",
            "top_lag_repos",
            # 兼容旧字段
            "circuit_breaker_states",
            "token_bucket_states",
            "paused_by_reason",
            "paused_repos_count",
            "paused_details",
        }
        
        actual_fields = set(result.keys())
        missing = required_fields - actual_fields
        assert not missing, f"缺少必需字段: {missing}"
        
        # 验证 circuit_breakers 子结构
        cb = result["circuit_breakers"]
        assert "by_scope" in cb
        assert "total_count" in cb
        assert "total_open" in cb
        
        # 验证 error_budget 子结构
        eb = result["error_budget"]
        assert "window_minutes" in eb
        assert "samples" in eb
        assert "failure" in eb
        assert "rate_limit_429" in eb
        assert "timeout" in eb


# ============ 熔断器和令牌桶状态测试 ============


class TestCircuitBreakerAndTokenBucketStatus:
    """测试熔断器和令牌桶状态在 summary 中的输出"""

    def test_circuit_breaker_state_in_summary(self, db_conn):
        """验证熔断器状态在摘要中的结构"""
        # 插入熔断器状态
        cb_key = build_circuit_breaker_key("test_project", "global")
        cb_state = {
            "state": "open",
            "failure_count": 5,
            "success_count": 10,
            "last_failure_time": 1700000000.0,
        }
        save_circuit_breaker_state(db_conn, cb_key, cb_state)
        
        # 导入 scm_sync_status 中的函数
        import sys
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from scm_sync_status import get_sync_summary
        
        result = get_sync_summary(db_conn)
        
        # 验证新 schema: circuit_breakers 存在
        assert "circuit_breakers" in result
        circuit_breakers = result["circuit_breakers"]
        assert "by_scope" in circuit_breakers
        assert "total_count" in circuit_breakers
        assert "total_open" in circuit_breakers
        
        # 验证按 scope 聚合
        by_scope = circuit_breakers["by_scope"]
        assert isinstance(by_scope, dict)
        # 应该有 global scope
        if "global" in by_scope:
            global_scope = by_scope["global"]
            assert "count" in global_scope
            assert "open_count" in global_scope
            assert "half_open_count" in global_scope
            assert "closed_count" in global_scope
            assert "total_failures" in global_scope
            assert "entries" in global_scope
        
        # 验证兼容旧字段 circuit_breaker_states
        assert "circuit_breaker_states" in result
        cb_states = result["circuit_breaker_states"]
        assert isinstance(cb_states, list)
        
        # 找到刚插入的熔断器状态
        found = [s for s in cb_states if s["key"] == cb_key]
        assert len(found) == 1
        
        cb = found[0]
        assert "state" in cb
        assert cb["state"]["state"] == "open"
        assert cb["state"]["failure_count"] == 5
        assert cb["state"]["success_count"] == 10

    def test_token_bucket_state_in_summary(self, db_conn):
        """验证令牌桶状态在摘要中的结构"""
        # 插入令牌桶状态
        instance_key = "test-gitlab-instance.com"
        with db_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO scm.sync_rate_limits 
                    (instance_key, tokens, rate, burst, updated_at, meta_json)
                VALUES (%s, %s, %s, %s, now(), %s)
                ON CONFLICT (instance_key) DO UPDATE
                SET tokens = EXCLUDED.tokens,
                    rate = EXCLUDED.rate,
                    burst = EXCLUDED.burst,
                    updated_at = EXCLUDED.updated_at
            """, (instance_key, 15.0, 10.0, 20, '{"test": true}'))
        
        # 导入 scm_sync_status 中的函数
        import sys
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from scm_sync_status import get_sync_summary
        
        result = get_sync_summary(db_conn)
        
        # 验证新 schema: rate_limit_buckets 存在
        assert "rate_limit_buckets" in result
        rate_limit_buckets = result["rate_limit_buckets"]
        assert isinstance(rate_limit_buckets, list)
        
        # 找到刚插入的限流桶状态
        found_bucket = [b for b in rate_limit_buckets if b["instance_key"] == instance_key]
        assert len(found_bucket) == 1
        
        bucket = found_bucket[0]
        # 验证新 schema 字段
        assert "tokens_remaining" in bucket
        assert "pause_until" in bucket  # 注意：新字段名是 pause_until，不是 paused_until
        assert "source" in bucket
        assert "is_paused" in bucket
        assert "pause_remaining_seconds" in bucket
        assert "wait_seconds" in bucket
        assert "rate" in bucket
        assert "burst" in bucket
        
        # 验证兼容旧字段 token_bucket_states
        assert "token_bucket_states" in result
        tb_states = result["token_bucket_states"]
        assert isinstance(tb_states, list)
        
        # 找到刚插入的令牌桶状态
        found = [s for s in tb_states if s["instance_key"] == instance_key]
        assert len(found) == 1
        
        tb = found[0]
        assert "tokens_remaining" in tb
        assert "paused_until" in tb
        assert "wait_seconds" in tb
        assert "is_paused" in tb
        assert "pause_remaining_seconds" in tb
        assert "rate" in tb
        assert "burst" in tb
        
        # 验证值
        assert tb["rate"] == 10.0
        assert tb["burst"] == 20
        assert tb["is_paused"] is False

    def test_token_bucket_paused_state(self, db_conn):
        """验证暂停状态的令牌桶"""
        instance_key = "paused-instance.com"
        # 插入一个被暂停的令牌桶（paused_until 在未来）
        with db_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO scm.sync_rate_limits 
                    (instance_key, tokens, rate, burst, paused_until, updated_at, meta_json)
                VALUES (%s, %s, %s, %s, now() + interval '1 hour', now(), %s)
                ON CONFLICT (instance_key) DO UPDATE
                SET tokens = EXCLUDED.tokens,
                    paused_until = EXCLUDED.paused_until,
                    updated_at = EXCLUDED.updated_at
            """, (instance_key, 0, 10.0, 20, '{}'))
        
        import sys
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from scm_sync_status import get_sync_summary
        
        result = get_sync_summary(db_conn)
        
        found = [s for s in result["token_bucket_states"] if s["instance_key"] == instance_key]
        assert len(found) == 1
        
        tb = found[0]
        assert tb["is_paused"] is True
        assert tb["pause_remaining_seconds"] > 0
        assert tb["paused_until"] is not None


class TestPrometheusOutputFormat:
    """测试 Prometheus 输出格式"""

    def test_prometheus_error_budget_metrics(self, db_conn):
        """验证 Prometheus 输出包含 error_budget 指标"""
        import sys
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from scm_sync_status import get_sync_summary, format_prometheus_metrics
        
        summary = get_sync_summary(db_conn)
        prom_output = format_prometheus_metrics(summary)
        
        # 验证包含 error_budget 指标
        assert "scm_error_budget_samples" in prom_output
        assert "scm_error_budget_failure_count" in prom_output
        assert "scm_error_budget_failure_rate" in prom_output
        assert "scm_error_budget_429_count" in prom_output
        assert "scm_error_budget_429_rate" in prom_output
        assert "scm_error_budget_timeout_count" in prom_output
        assert "scm_error_budget_timeout_rate" in prom_output

    def test_prometheus_jobs_by_status_metric(self, db_conn):
        """验证 Prometheus 输出包含 jobs_by_status 指标（带 instance_key/tenant_id/job_type 标签）"""
        # 创建测试仓库和任务
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url="https://gitlab.example.com/tenant_a/project.git",
            project_key="tenant_a/project",
        )
        enqueue_sync_job(db_conn, repo_id, "gitlab_commits", priority=50)
        
        import sys
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from scm_sync_status import get_sync_summary, format_prometheus_metrics
        
        summary = get_sync_summary(db_conn)
        prom_output = format_prometheus_metrics(summary)
        
        # 验证指标名存在
        assert "scm_jobs_by_status" in prom_output
        # 验证标签存在
        assert 'instance_key=' in prom_output
        assert 'tenant_id=' in prom_output
        assert 'job_type=' in prom_output
        assert 'status=' in prom_output

    def test_prometheus_breaker_state_metric(self, db_conn):
        """验证 Prometheus 输出包含 breaker_state 指标（使用 instance_key 标签，无敏感信息）"""
        # 插入熔断器状态
        cb_key = build_circuit_breaker_key("test_instance.com", "global")
        cb_state = {
            "state": "open",
            "failure_count": 5,
            "success_count": 10,
        }
        save_circuit_breaker_state(db_conn, cb_key, cb_state)
        
        import sys
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from scm_sync_status import get_sync_summary, format_prometheus_metrics
        
        summary = get_sync_summary(db_conn)
        prom_output = format_prometheus_metrics(summary)
        
        # 验证新指标名存在
        assert "scm_breaker_state" in prom_output
        assert "scm_breaker_failure_count" in prom_output
        assert "scm_breaker_success_count" in prom_output
        # 验证使用 instance_key 标签
        assert 'scm_breaker_state{instance_key=' in prom_output

    def test_prometheus_rate_limit_pause_until_metric(self, db_conn):
        """验证 Prometheus 输出包含 rate_limit_pause_until 指标"""
        instance_key = "rate-limit-pause-test.com"
        with db_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO scm.sync_rate_limits 
                    (instance_key, tokens, rate, burst, paused_until, updated_at, meta_json)
                VALUES (%s, %s, %s, %s, now() + interval '1 hour', now(), %s)
                ON CONFLICT (instance_key) DO UPDATE
                SET tokens = EXCLUDED.tokens, 
                    paused_until = EXCLUDED.paused_until,
                    updated_at = EXCLUDED.updated_at
            """, (instance_key, 0, 10.0, 20, '{}'))
        
        import sys
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from scm_sync_status import get_sync_summary, format_prometheus_metrics
        
        summary = get_sync_summary(db_conn)
        prom_output = format_prometheus_metrics(summary)
        
        # 验证指标名存在
        assert "scm_rate_limit_pause_until" in prom_output
        # 验证有实际的时间戳值（大于 0）
        import re
        match = re.search(r'scm_rate_limit_pause_until\{instance_key="[^"]+"\}\s+(\d+)', prom_output)
        assert match is not None
        pause_until_ts = int(match.group(1))
        assert pause_until_ts > 0, "pause_until should be a positive timestamp"

    def test_prometheus_retry_backoff_seconds_metric(self, db_conn):
        """验证 Prometheus 输出包含 retry_backoff_seconds 指标"""
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url="https://gitlab.example.com/retry_test/project.git",
            project_key="retry_test/project",
        )
        
        # 创建一个 not_before 在未来的任务
        with db_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO scm.sync_jobs (repo_id, job_type, status, priority, not_before)
                VALUES (%s, %s, %s, %s, now() + interval '5 minutes')
            """, (repo_id, "gitlab_commits", "pending", 50))
        
        import sys
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from scm_sync_status import get_sync_summary, format_prometheus_metrics
        
        summary = get_sync_summary(db_conn)
        prom_output = format_prometheus_metrics(summary)
        
        # 验证指标名存在
        assert "scm_retry_backoff_seconds" in prom_output
        # 验证标签存在
        assert 'scm_retry_backoff_seconds{instance_key=' in prom_output

    def test_prometheus_circuit_breaker_metrics(self, db_conn):
        """验证 Prometheus 输出包含熔断器指标（新/旧格式）"""
        # 插入熔断器状态
        cb_key = build_circuit_breaker_key("prom_test", "global")
        cb_state = {
            "state": "half_open",
            "failure_count": 3,
            "success_count": 7,
        }
        save_circuit_breaker_state(db_conn, cb_key, cb_state)
        
        import sys
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from scm_sync_status import get_sync_summary, format_prometheus_metrics
        
        summary = get_sync_summary(db_conn)
        prom_output = format_prometheus_metrics(summary)
        
        # 验证包含新指标：按 scope 聚合
        assert "scm_circuit_breakers_by_scope" in prom_output
        assert "scm_circuit_breakers_total_failures" in prom_output
        
        # 验证包含兼容旧指标
        assert "scm_circuit_breaker_state" in prom_output
        assert "scm_circuit_breaker_failure_count" in prom_output
        assert "scm_circuit_breaker_success_count" in prom_output
        assert f'key="{cb_key}"' in prom_output

    def test_prometheus_rate_limit_bucket_metrics(self, db_conn):
        """验证 Prometheus 输出包含 rate_limit_buckets 指标"""
        instance_key = "prom-bucket-test.com"
        with db_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO scm.sync_rate_limits 
                    (instance_key, tokens, rate, burst, paused_until, updated_at, meta_json)
                VALUES (%s, %s, %s, %s, now() + interval '10 minutes', now(), %s)
                ON CONFLICT (instance_key) DO UPDATE
                SET tokens = EXCLUDED.tokens, 
                    paused_until = EXCLUDED.paused_until,
                    updated_at = EXCLUDED.updated_at
            """, (instance_key, 5.0, 5.0, 15, '{"pause_source": "test"}'))
        
        import sys
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from scm_sync_status import get_sync_summary, format_prometheus_metrics
        
        summary = get_sync_summary(db_conn)
        prom_output = format_prometheus_metrics(summary)
        
        # 验证包含新指标：rate_limit_buckets
        assert "scm_rate_limit_bucket_tokens" in prom_output
        assert "scm_rate_limit_bucket_paused" in prom_output
        assert "scm_rate_limit_bucket_pause_seconds" in prom_output
        assert f'instance_key="{instance_key}"' in prom_output

    def test_prometheus_token_bucket_metrics(self, db_conn):
        """验证 Prometheus 输出包含令牌桶指标（兼容旧格式）"""
        instance_key = "prom-test-instance.com"
        with db_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO scm.sync_rate_limits 
                    (instance_key, tokens, rate, burst, updated_at, meta_json)
                VALUES (%s, %s, %s, %s, now(), %s)
                ON CONFLICT (instance_key) DO UPDATE
                SET tokens = EXCLUDED.tokens, updated_at = EXCLUDED.updated_at
            """, (instance_key, 10.0, 5.0, 15, '{}'))
        
        import sys
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from scm_sync_status import get_sync_summary, format_prometheus_metrics
        
        summary = get_sync_summary(db_conn)
        prom_output = format_prometheus_metrics(summary)
        
        # 验证包含兼容旧指标
        assert "scm_token_bucket_tokens_remaining" in prom_output
        assert "scm_token_bucket_wait_seconds" in prom_output
        assert "scm_token_bucket_is_paused" in prom_output
        assert "scm_token_bucket_pause_remaining_seconds" in prom_output
        assert f'instance_key="{instance_key}"' in prom_output

    def test_prometheus_pauses_by_reason_metrics(self, db_conn):
        """验证 Prometheus 输出包含 pauses_by_reason 指标（新/旧格式）"""
        from db import set_repo_job_pause, PauseReasonCode
        
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url="https://example.com/prom-pauses-test.git",
            project_key="prom_pauses",
        )
        
        set_repo_job_pause(
            db_conn,
            repo_id=repo_id,
            job_type="commits",
            pause_duration_seconds=300,
            reason="test pause",
            reason_code=PauseReasonCode.ERROR_BUDGET,
        )
        
        import sys
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from scm_sync_status import get_sync_summary, format_prometheus_metrics
        
        summary = get_sync_summary(db_conn)
        prom_output = format_prometheus_metrics(summary)
        
        # 验证包含新指标名
        assert "scm_pauses_by_reason" in prom_output
        
        # 验证包含兼容旧指标名
        assert "scm_paused_by_reason" in prom_output
        assert f'reason_code="{PauseReasonCode.ERROR_BUDGET}"' in prom_output


# ============ 暂停统计和敏感信息测试 ============


class TestFixedSchemaSummary:
    """测试固定 schema 的 summary 输出"""

    def test_error_budget_schema(self, db_conn):
        """验证 error_budget 固定 schema 结构"""
        import sys
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from scm_sync_status import get_sync_summary
        
        summary = get_sync_summary(db_conn)
        
        # 验证 error_budget 字段存在
        assert "error_budget" in summary
        error_budget = summary["error_budget"]
        
        # 验证固定 schema 字段
        assert "window_minutes" in error_budget
        assert "samples" in error_budget
        assert "total_requests" in error_budget
        assert "completed_runs" in error_budget
        
        # 验证 failure 子结构
        assert "failure" in error_budget
        failure = error_budget["failure"]
        assert "count" in failure
        assert "rate" in failure
        
        # 验证 rate_limit_429 子结构
        assert "rate_limit_429" in error_budget
        rate_429 = error_budget["rate_limit_429"]
        assert "count" in rate_429
        assert "rate" in rate_429
        
        # 验证 timeout 子结构
        assert "timeout" in error_budget
        timeout = error_budget["timeout"]
        assert "count" in timeout
        assert "rate" in timeout

    def test_circuit_breakers_schema(self, db_conn):
        """验证 circuit_breakers 固定 schema（按 scope 聚合）"""
        # 插入熔断器状态
        cb_key = build_circuit_breaker_key("schema_test", "global")
        save_circuit_breaker_state(db_conn, cb_key, {
            "state": "open",
            "failure_count": 3,
            "success_count": 5,
        })
        
        import sys
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from scm_sync_status import get_sync_summary
        
        summary = get_sync_summary(db_conn)
        
        # 验证 circuit_breakers 固定 schema
        assert "circuit_breakers" in summary
        circuit_breakers = summary["circuit_breakers"]
        
        assert "by_scope" in circuit_breakers
        assert "total_count" in circuit_breakers
        assert "total_open" in circuit_breakers
        
        # 验证 by_scope 内每个 scope 的结构
        by_scope = circuit_breakers["by_scope"]
        if by_scope:
            for scope, data in by_scope.items():
                assert "count" in data
                assert "open_count" in data
                assert "half_open_count" in data
                assert "closed_count" in data
                assert "total_failures" in data
                assert "entries" in data

    def test_rate_limit_buckets_schema(self, db_conn):
        """验证 rate_limit_buckets 固定 schema（含 pause_until/source）"""
        instance_key = "schema-test-instance.com"
        with db_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO scm.sync_rate_limits 
                    (instance_key, tokens, rate, burst, paused_until, updated_at, meta_json)
                VALUES (%s, %s, %s, %s, now() + interval '30 minutes', now(), %s)
                ON CONFLICT (instance_key) DO UPDATE
                SET tokens = EXCLUDED.tokens,
                    paused_until = EXCLUDED.paused_until,
                    meta_json = EXCLUDED.meta_json,
                    updated_at = EXCLUDED.updated_at
            """, (instance_key, 5.0, 10.0, 20, '{"pause_source": "rate_limit_429"}'))
        
        import sys
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from scm_sync_status import get_sync_summary
        
        summary = get_sync_summary(db_conn)
        
        # 验证 rate_limit_buckets 固定 schema
        assert "rate_limit_buckets" in summary
        buckets = summary["rate_limit_buckets"]
        assert isinstance(buckets, list)
        
        found = [b for b in buckets if b["instance_key"] == instance_key]
        assert len(found) == 1
        
        bucket = found[0]
        # 验证固定 schema 字段
        assert "instance_key" in bucket
        assert "tokens_remaining" in bucket
        assert "pause_until" in bucket  # 新字段名
        assert "source" in bucket       # 新字段：暂停原因来源
        assert "is_paused" in bucket
        assert "pause_remaining_seconds" in bucket
        assert "wait_seconds" in bucket
        assert "rate" in bucket
        assert "burst" in bucket
        
        # 验证 source 字段能正确提取
        assert bucket["source"] == "rate_limit_429"

    def test_pauses_by_reason_schema(self, db_conn):
        """验证 pauses_by_reason 固定 schema"""
        from db import set_repo_job_pause, PauseReasonCode
        
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url="https://example.com/pauses-schema-test.git",
            project_key="pauses_schema_test",
        )
        
        set_repo_job_pause(
            db_conn,
            repo_id=repo_id,
            job_type="commits",
            pause_duration_seconds=300,
            reason="test",
            reason_code=PauseReasonCode.ERROR_BUDGET,
        )
        
        import sys
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from scm_sync_status import get_sync_summary
        
        summary = get_sync_summary(db_conn)
        
        # 验证新字段名 pauses_by_reason 存在
        assert "pauses_by_reason" in summary
        pauses_by_reason = summary["pauses_by_reason"]
        assert isinstance(pauses_by_reason, dict)
        
        # 验证兼容旧字段名 paused_by_reason
        assert "paused_by_reason" in summary
        assert summary["paused_by_reason"] == pauses_by_reason


class TestPausedReposAggregation:
    """测试暂停记录按原因聚合统计"""

    def test_paused_by_reason_in_summary(self, db_conn):
        """验证 summary 输出包含按 reason_code 聚合的暂停统计"""
        from db import set_repo_job_pause, PauseReasonCode
        
        # 创建测试仓库
        repo_id1 = upsert_repo(
            db_conn,
            repo_type="git",
            url="https://example.com/pause-test-1.git",
            project_key="pause_test_1",
        )
        repo_id2 = upsert_repo(
            db_conn,
            repo_type="git",
            url="https://example.com/pause-test-2.git",
            project_key="pause_test_2",
        )
        repo_id3 = upsert_repo(
            db_conn,
            repo_type="svn",
            url="https://svn.example.com/pause-test",
            project_key="pause_test_3",
        )
        
        # 设置不同原因的暂停记录
        set_repo_job_pause(
            db_conn,
            repo_id=repo_id1,
            job_type="commits",
            pause_duration_seconds=300,
            reason="failure_rate=0.35",
            reason_code=PauseReasonCode.ERROR_BUDGET,
            failure_rate=0.35,
        )
        set_repo_job_pause(
            db_conn,
            repo_id=repo_id1,
            job_type="mrs",
            pause_duration_seconds=300,
            reason="failure_rate=0.40",
            reason_code=PauseReasonCode.ERROR_BUDGET,
            failure_rate=0.40,
        )
        set_repo_job_pause(
            db_conn,
            repo_id=repo_id2,
            job_type="commits",
            pause_duration_seconds=600,
            reason="bucket_paused,remaining=120.0s",
            reason_code=PauseReasonCode.RATE_LIMIT_BUCKET,
        )
        set_repo_job_pause(
            db_conn,
            repo_id=repo_id3,
            job_type="commits",
            pause_duration_seconds=900,
            reason="circuit_breaker_open",
            reason_code=PauseReasonCode.CIRCUIT_OPEN,
        )
        
        import sys
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from scm_sync_status import get_sync_summary
        
        summary = get_sync_summary(db_conn)
        
        # 验证新字段 pauses_by_reason 存在
        assert "pauses_by_reason" in summary
        
        # 验证聚合字段存在
        assert "paused_repos_count" in summary
        assert "paused_by_reason" in summary
        assert "paused_details" in summary
        
        # 验证总数
        assert summary["paused_repos_count"] >= 4
        
        # 验证按 reason_code 聚合（使用新字段名）
        pauses_by_reason = summary["pauses_by_reason"]
        assert isinstance(pauses_by_reason, dict)
        assert PauseReasonCode.ERROR_BUDGET in pauses_by_reason
        assert pauses_by_reason[PauseReasonCode.ERROR_BUDGET] >= 2
        assert PauseReasonCode.RATE_LIMIT_BUCKET in pauses_by_reason
        assert pauses_by_reason[PauseReasonCode.RATE_LIMIT_BUCKET] >= 1
        assert PauseReasonCode.CIRCUIT_OPEN in pauses_by_reason
        assert pauses_by_reason[PauseReasonCode.CIRCUIT_OPEN] >= 1

    def test_prometheus_paused_metrics(self, db_conn):
        """验证 Prometheus 输出包含暂停统计指标"""
        from db import set_repo_job_pause, PauseReasonCode
        
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url="https://example.com/prom-pause-test.git",
            project_key="prom_pause",
        )
        
        set_repo_job_pause(
            db_conn,
            repo_id=repo_id,
            job_type="commits",
            pause_duration_seconds=300,
            reason="test pause",
            reason_code=PauseReasonCode.ERROR_BUDGET,
        )
        
        import sys
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from scm_sync_status import get_sync_summary, format_prometheus_metrics
        
        summary = get_sync_summary(db_conn)
        prom_output = format_prometheus_metrics(summary)
        
        # 验证包含暂停指标
        assert "scm_paused_repos_total" in prom_output
        assert "scm_paused_by_reason" in prom_output
        assert f'reason_code="{PauseReasonCode.ERROR_BUDGET}"' in prom_output


class TestNoSensitiveInfoLeakage:
    """测试 summary 输出不泄漏敏感信息（验证 redact 脱敏生效）"""

    def test_paused_details_no_url(self, db_conn):
        """验证 paused_details 不包含 URL"""
        from db import set_repo_job_pause, PauseReasonCode
        
        sensitive_url = "https://secret-gitlab.internal.corp/sensitive/repo.git"
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url=sensitive_url,
            project_key="secret_team/sensitive_project",
        )
        
        set_repo_job_pause(
            db_conn,
            repo_id=repo_id,
            job_type="commits",
            pause_duration_seconds=300,
            reason="test",
            reason_code=PauseReasonCode.ERROR_BUDGET,
        )
        
        import sys
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from scm_sync_status import get_sync_summary
        
        summary = get_sync_summary(db_conn)
        
        # 验证 paused_details 存在
        assert "paused_details" in summary
        paused_details = summary["paused_details"]
        assert len(paused_details) >= 1
        
        # 验证 paused_details 中的每条记录都不包含敏感字段
        for detail in paused_details:
            assert "url" not in detail, "paused_details should not contain url"
            assert "project_key" not in detail, "paused_details should not contain project_key"
            # 确保只包含非敏感字段
            allowed_fields = {"repo_id", "job_type", "reason_code", "remaining_seconds"}
            for field in detail.keys():
                assert field in allowed_fields, f"Unexpected field in paused_details: {field}"
        
        # 验证 URL 字符串不出现在 paused_details JSON 中
        import json
        paused_json = json.dumps(paused_details)
        assert sensitive_url not in paused_json
        assert "secret-gitlab" not in paused_json
        assert "sensitive_project" not in paused_json

    def test_top_lag_repos_url_redacted(self, db_conn):
        """验证 top_lag_repos 中的 URL 被 redact 脱敏"""
        from db import insert_sync_run_start, insert_sync_run_finish
        
        # 创建包含敏感信息的 URL
        sensitive_url = "https://user:glpat-secret123456789@gitlab.private.corp/api/v4"
        sensitive_project = "internal_team/secret_project"
        
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url=sensitive_url,
            project_key=sensitive_project,
        )
        
        # 创建 sync_run 以便生成 lag 数据
        run_id = str(uuid.uuid4())
        insert_sync_run_start(db_conn, run_id, repo_id, "gitlab_commits")
        insert_sync_run_finish(db_conn, run_id, status="completed")
        
        import sys
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from scm_sync_status import get_sync_summary
        
        summary = get_sync_summary(db_conn)
        
        # 验证 top_lag_repos 存在
        assert "top_lag_repos" in summary
        top_lag_repos = summary["top_lag_repos"]
        
        # 找到我们的测试仓库
        found = [r for r in top_lag_repos if r["repo_id"] == repo_id]
        
        if found:
            repo = found[0]
            # 验证 URL 已被脱敏（不包含原始敏感内容）
            url = repo.get("url", "")
            project_key = repo.get("project_key", "")
            
            # GitLab token 应被替换
            assert "glpat-secret123456789" not in (url or ""), "GitLab token should be redacted in URL"
            # 用户凭证应被替换
            assert ":glpat-" not in (url or ""), "Credentials should be redacted in URL"
            
            # 确保脱敏后的标记存在（如果 URL 被脱敏了）
            if url and "://" in url and "@" in url:
                assert "[REDACTED]" in url or "[GITLAB_TOKEN]" in url, \
                    "URL with credentials should have redaction markers"

    def test_token_bucket_meta_redacted(self, db_conn):
        """验证 token_bucket_states 中的 meta_json 被脱敏"""
        instance_key = "redact-test-instance.com"
        # meta_json 中包含敏感 token
        sensitive_meta = {
            "authorization": "Bearer glpat-sensitive-token-123",
            "private-token": "glpat-another-token-456",
            "normal_field": "safe_value",
        }
        
        with db_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO scm.sync_rate_limits 
                    (instance_key, tokens, rate, burst, updated_at, meta_json)
                VALUES (%s, %s, %s, %s, now(), %s)
                ON CONFLICT (instance_key) DO UPDATE
                SET meta_json = EXCLUDED.meta_json,
                    updated_at = EXCLUDED.updated_at
            """, (instance_key, 10.0, 5.0, 15, json.dumps(sensitive_meta)))
        
        import sys
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from scm_sync_status import get_sync_summary
        
        summary = get_sync_summary(db_conn)
        
        # 找到 token_bucket_states 中的测试条目
        tb_states = summary.get("token_bucket_states", [])
        found = [s for s in tb_states if s["instance_key"] == instance_key]
        
        assert len(found) == 1
        meta = found[0].get("meta_json")
        
        if meta:
            # 敏感字段应被脱敏
            assert meta.get("authorization") == "[REDACTED]", \
                "authorization header should be redacted"
            assert meta.get("private-token") == "[REDACTED]", \
                "private-token header should be redacted"
            # 非敏感字段应保留
            assert meta.get("normal_field") == "safe_value"

    def test_paused_by_reason_no_sensitive_info(self, db_conn):
        """验证 paused_by_reason 聚合只包含 reason_code 和计数"""
        from db import set_repo_job_pause, PauseReasonCode
        
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url="https://private.gitlab.com/team/project.git",
            project_key="team/project",
        )
        
        set_repo_job_pause(
            db_conn,
            repo_id=repo_id,
            job_type="commits",
            pause_duration_seconds=300,
            reason="internal error details",
            reason_code=PauseReasonCode.ERROR_BUDGET,
        )
        
        import sys
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from scm_sync_status import get_sync_summary
        
        summary = get_sync_summary(db_conn)
        
        paused_by_reason = summary.get("paused_by_reason", {})
        
        # paused_by_reason 应该只是 {reason_code: count} 格式
        import json
        reason_json = json.dumps(paused_by_reason)
        
        # 验证不包含敏感信息
        assert "private.gitlab.com" not in reason_json
        assert "team/project" not in reason_json
        assert "internal error details" not in reason_json
        
        # 验证格式正确（只有 reason_code 作为 key，int 作为 value）
        for key, value in paused_by_reason.items():
            assert isinstance(key, str), "reason_code should be string"
            assert isinstance(value, int), "count should be int"

    def test_prometheus_output_no_sensitive_info(self, db_conn):
        """验证 Prometheus 输出不泄漏敏感信息"""
        from db import set_repo_job_pause, PauseReasonCode
        
        sensitive_url = "https://enterprise.gitlab.corp/internal/secret-api.git"
        sensitive_project = "internal/secret-api"
        
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url=sensitive_url,
            project_key=sensitive_project,
        )
        
        set_repo_job_pause(
            db_conn,
            repo_id=repo_id,
            job_type="commits",
            pause_duration_seconds=300,
            reason="credentials exposed in error",
            reason_code=PauseReasonCode.ERROR_BUDGET,
        )
        
        import sys
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from scm_sync_status import get_sync_summary, format_prometheus_metrics
        
        summary = get_sync_summary(db_conn)
        prom_output = format_prometheus_metrics(summary)
        
        # 验证 Prometheus 输出不包含敏感信息
        assert "enterprise.gitlab.corp" not in prom_output
        assert "secret-api" not in prom_output
        assert "credentials exposed" not in prom_output
        
        # 验证只包含标准化的 reason_code 标签
        assert f'reason_code="{PauseReasonCode.ERROR_BUDGET}"' in prom_output
