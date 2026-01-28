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
        """summary 查询的字段快照"""
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
