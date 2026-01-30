# -*- coding: utf-8 -*-
"""
test_scm_sync_integration.py - SCM Sync 组件集成测试

覆盖 Scheduler、Worker、Reaper 三大组件的集成测试：
1. Scheduler: 扫描仓库状态并入队同步任务
2. Worker: claim/execute/ack 任务执行流程
3. Reaper: 清理过期/卡住的任务

================================================================================
测试策略
================================================================================

- 使用 stub runner 或 monkeypatch 避免外部 SCM 依赖（GitLab/SVN API）
- 使用测试数据库 fixture 确保隔离
- 测试核心队列语义：claim/ack/fail/requeue
- 测试状态机不变量

================================================================================
运行方式
================================================================================

# 使用 conftest.py 中的 migrated_db fixture（自动创建测试数据库）
pytest apps/logbook_postgres/scripts/tests/test_scm_sync_integration.py -v

# 仅运行快速测试（跳过需要外部服务的测试）
pytest apps/logbook_postgres/scripts/tests/test_scm_sync_integration.py -v -m "not slow"

================================================================================
"""

import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest

# 添加 scripts 目录到 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ============================================================================
# Stub Sync Functions（避免外部 SCM 依赖）
# ============================================================================


def stub_sync_success(repo_id: int, mode: str, payload: dict, worker_id: str) -> dict:
    """
    Stub 同步函数：总是返回成功
    
    用于测试队列语义，不实际调用 GitLab/SVN API。
    """
    return {
        "success": True,
        "synced_count": 10,
        "counts": {
            "synced_count": 10,
            "diff_count": 5,
        },
        "run_id": str(uuid.uuid4()),
    }


def stub_sync_failure(repo_id: int, mode: str, payload: dict, worker_id: str) -> dict:
    """
    Stub 同步函数：总是返回失败（临时性错误，可重试）
    """
    return {
        "success": False,
        "error": "Stub transient error for testing",
        "error_category": "network",
    }


def stub_sync_permanent_failure(repo_id: int, mode: str, payload: dict, worker_id: str) -> dict:
    """
    Stub 同步函数：返回永久性错误（不可重试）
    """
    return {
        "success": False,
        "error": "Stub permanent error: authentication failed",
        "error_category": "auth_error",
    }


def stub_sync_rate_limited(repo_id: int, mode: str, payload: dict, worker_id: str) -> dict:
    """
    Stub 同步函数：返回 429 限流错误
    """
    return {
        "success": False,
        "error": "429 Too Many Requests",
        "error_category": "rate_limit",
        "retry_after": 60,
    }


def stub_sync_slow(repo_id: int, mode: str, payload: dict, worker_id: str) -> dict:
    """
    Stub 同步函数：模拟慢任务
    """
    time.sleep(0.5)  # 模拟耗时操作
    return {
        "success": True,
        "synced_count": 5,
    }


# ============================================================================
# Test Fixtures
# ============================================================================


@pytest.fixture
def test_repo(db_conn):
    """
    创建测试仓库并在测试后清理
    
    Returns:
        dict: 包含 repo_id, url, project_key 等信息
    """
    repo_id = None
    try:
        with db_conn.cursor() as cur:
            # 插入测试仓库
            cur.execute("""
                INSERT INTO scm.repos (url, repo_type, project_key, default_branch)
                VALUES ('https://gitlab.example.com/test/project', 'git', 'test', 'main')
                RETURNING repo_id
            """)
            repo_id = cur.fetchone()[0]
            db_conn.commit()
        
        yield {
            "repo_id": repo_id,
            "url": "https://gitlab.example.com/test/project",
            "repo_type": "git",
            "project_key": "test",
        }
    finally:
        # 清理（由于使用 db_conn 的回滚机制，这里的清理是可选的）
        pass


@pytest.fixture
def multiple_test_repos(db_conn):
    """
    创建多个测试仓库用于调度测试
    
    Returns:
        List[dict]: 仓库信息列表
    """
    repos = []
    try:
        with db_conn.cursor() as cur:
            # 创建 3 个 Git 仓库
            for i in range(3):
                cur.execute("""
                    INSERT INTO scm.repos (url, repo_type, project_key, default_branch)
                    VALUES (%s, 'git', %s, 'main')
                    RETURNING repo_id
                """, (
                    f"https://gitlab.example.com/test/project-{i}",
                    f"test-{i}",
                ))
                repo_id = cur.fetchone()[0]
                repos.append({
                    "repo_id": repo_id,
                    "url": f"https://gitlab.example.com/test/project-{i}",
                    "repo_type": "git",
                    "project_key": f"test-{i}",
                })
            
            # 创建 1 个 SVN 仓库
            cur.execute("""
                INSERT INTO scm.repos (url, repo_type, project_key)
                VALUES ('svn://svn.example.com/test/repo', 'svn', 'svn-test')
                RETURNING repo_id
            """)
            repo_id = cur.fetchone()[0]
            repos.append({
                "repo_id": repo_id,
                "url": "svn://svn.example.com/test/repo",
                "repo_type": "svn",
                "project_key": "svn-test",
            })
            
            db_conn.commit()
        
        yield repos
    finally:
        pass


@pytest.fixture
def worker_id():
    """生成唯一的 worker ID"""
    return f"test-worker-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def stub_sync_registry():
    """
    返回 stub 同步函数注册表
    
    用于 monkeypatch scm_sync_worker.get_sync_function_registry
    """
    return {
        "gitlab_commits": lambda repo_id, mode, payload, worker_id: stub_sync_success(repo_id, mode, payload, worker_id),
        "gitlab_mrs": lambda repo_id, mode, payload, worker_id: stub_sync_success(repo_id, mode, payload, worker_id),
        "gitlab_reviews": lambda repo_id, mode, payload, worker_id: stub_sync_success(repo_id, mode, payload, worker_id),
        "svn": lambda repo_id, mode, payload, worker_id: stub_sync_success(repo_id, mode, payload, worker_id),
        "commits": lambda repo_id, mode, payload, worker_id: stub_sync_success(repo_id, mode, payload, worker_id),
        "mrs": lambda repo_id, mode, payload, worker_id: stub_sync_success(repo_id, mode, payload, worker_id),
        "reviews": lambda repo_id, mode, payload, worker_id: stub_sync_success(repo_id, mode, payload, worker_id),
    }


# ============================================================================
# 辅助函数
# ============================================================================


def create_test_job(
    conn,
    repo_id: int,
    job_type: str = "gitlab_commits",
    mode: str = "incremental",
    priority: int = 100,
    status: str = "pending",
    payload: Optional[dict] = None,
) -> str:
    """
    创建测试任务
    
    Returns:
        job_id (UUID string)
    """
    job_id = str(uuid.uuid4())
    payload_json = json.dumps(payload or {})
    
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO scm.sync_jobs (
                job_id, repo_id, job_type, mode, priority, status, payload_json
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (job_id, repo_id, job_type, mode, priority, status, payload_json))
        conn.commit()
    
    return job_id


def get_job_status(conn, job_id: str) -> Optional[dict]:
    """获取任务状态"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT job_id, repo_id, job_type, status, attempts, locked_by, last_error
            FROM scm.sync_jobs
            WHERE job_id = %s
        """, (job_id,))
        row = cur.fetchone()
        if row:
            return {
                "job_id": str(row[0]),
                "repo_id": row[1],
                "job_type": row[2],
                "status": row[3],
                "attempts": row[4],
                "locked_by": row[5],
                "last_error": row[6],
            }
    return None


def get_pending_jobs_count(conn) -> int:
    """获取待处理任务数量"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM scm.sync_jobs
            WHERE status IN ('pending', 'failed')
            AND not_before <= now()
        """)
        return cur.fetchone()[0]


def clear_all_jobs(conn):
    """清理所有任务（用于测试准备）"""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM scm.sync_jobs")
        conn.commit()


# ============================================================================
# 测试类: 队列基础操作
# ============================================================================


class TestQueueBasicOperations:
    """测试队列基础操作：claim/ack/fail"""
    
    def test_claim_pending_job(self, db_conn, test_repo, worker_id):
        """测试 claim 获取待处理任务"""
        from engram.logbook.scm_sync_queue import claim, STATUS_RUNNING
        
        # 创建一个待处理任务
        job_id = create_test_job(db_conn, test_repo["repo_id"])
        
        # claim 获取任务
        job = claim(worker_id=worker_id, conn=db_conn)
        
        assert job is not None
        assert str(job["job_id"]) == job_id
        assert job["status"] == STATUS_RUNNING
        assert job["locked_by"] == worker_id
    
    def test_claim_respects_priority(self, db_conn, test_repo, worker_id):
        """测试 claim 按优先级获取任务（数值越小优先级越高）"""
        from engram.logbook.scm_sync_queue import claim
        
        # 创建两个不同优先级的任务
        low_priority_job = create_test_job(
            db_conn, test_repo["repo_id"],
            priority=200,
            job_type="gitlab_commits",
        )
        high_priority_job = create_test_job(
            db_conn, test_repo["repo_id"],
            priority=50,
            job_type="gitlab_mrs",
        )
        
        # claim 应该获取高优先级任务
        job = claim(worker_id=worker_id, conn=db_conn)
        
        assert job is not None
        assert str(job["job_id"]) == high_priority_job
        assert job["priority"] == 50
    
    def test_claim_filters_by_job_type(self, db_conn, test_repo, worker_id):
        """测试 claim 按 job_type 过滤"""
        from engram.logbook.scm_sync_queue import claim
        
        # 创建不同类型的任务
        commits_job = create_test_job(
            db_conn, test_repo["repo_id"],
            job_type="gitlab_commits",
        )
        mrs_job = create_test_job(
            db_conn, test_repo["repo_id"],
            job_type="gitlab_mrs",
        )
        
        # 只获取 gitlab_mrs 类型的任务
        job = claim(
            worker_id=worker_id,
            job_types=["gitlab_mrs"],
            conn=db_conn,
        )
        
        assert job is not None
        assert str(job["job_id"]) == mrs_job
        assert job["job_type"] == "gitlab_mrs"
    
    def test_claim_returns_none_when_empty(self, db_conn, worker_id):
        """测试队列为空时 claim 返回 None"""
        from engram.logbook.scm_sync_queue import claim
        
        # 清空队列
        clear_all_jobs(db_conn)
        
        job = claim(worker_id=worker_id, conn=db_conn)
        
        assert job is None
    
    def test_ack_completes_job(self, db_conn, test_repo, worker_id):
        """测试 ack 完成任务"""
        from engram.logbook.scm_sync_queue import claim, ack, STATUS_COMPLETED
        
        # 创建并 claim 任务
        job_id = create_test_job(db_conn, test_repo["repo_id"])
        job = claim(worker_id=worker_id, conn=db_conn)
        
        # ack 完成任务
        result = ack(str(job["job_id"]), worker_id, conn=db_conn)
        
        assert result is True
        
        # 验证状态
        status = get_job_status(db_conn, job_id)
        assert status["status"] == STATUS_COMPLETED
    
    def test_fail_retry_increments_attempts(self, db_conn, test_repo, worker_id):
        """测试 fail_retry 增加重试次数"""
        from engram.logbook.scm_sync_queue import claim, fail_retry, STATUS_FAILED
        
        # 创建并 claim 任务
        job_id = create_test_job(db_conn, test_repo["repo_id"])
        job = claim(worker_id=worker_id, conn=db_conn)
        
        # fail_retry
        result = fail_retry(
            str(job["job_id"]),
            worker_id,
            error="Test error",
            backoff_seconds=10,
            conn=db_conn,
        )
        
        assert result is True
        
        # 验证状态
        status = get_job_status(db_conn, job_id)
        assert status["status"] == STATUS_FAILED
        assert status["attempts"] == 1
        assert "Test error" in status["last_error"]
    
    def test_mark_dead_after_max_attempts(self, db_conn, test_repo, worker_id):
        """测试达到最大重试次数后标记为 dead"""
        from engram.logbook.scm_sync_queue import claim, fail_retry, mark_dead, STATUS_DEAD
        
        # 创建任务，设置 max_attempts=1
        job_id = str(uuid.uuid4())
        with db_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO scm.sync_jobs (
                    job_id, repo_id, job_type, mode, status, max_attempts
                ) VALUES (%s, %s, 'gitlab_commits', 'incremental', 'pending', 1)
            """, (job_id, test_repo["repo_id"]))
            db_conn.commit()
        
        # claim
        job = claim(worker_id=worker_id, conn=db_conn)
        
        # 标记为 dead（模拟达到最大重试次数）
        result = mark_dead(job_id, worker_id, error="Max attempts reached", conn=db_conn)
        
        assert result is True
        
        # 验证状态
        status = get_job_status(db_conn, job_id)
        assert status["status"] == STATUS_DEAD


# ============================================================================
# 测试类: Worker 执行流程
# ============================================================================


class TestWorkerExecution:
    """测试 Worker 任务执行流程"""
    
    def test_process_job_success(self, db_conn, test_repo, worker_id, stub_sync_registry, monkeypatch):
        """测试成功执行任务"""
        from engram.logbook.scm_sync_queue import STATUS_COMPLETED
        import scm_sync_worker
        
        # Monkeypatch 同步函数注册表
        monkeypatch.setattr(
            scm_sync_worker,
            "get_sync_function_registry",
            lambda: stub_sync_registry,
        )
        
        # 创建任务
        job_id = create_test_job(db_conn, test_repo["repo_id"])
        
        # Mock get_connection 返回测试连接
        def mock_get_connection(config=None):
            return db_conn
        
        # 处理任务
        with patch.object(scm_sync_worker, "get_connection", mock_get_connection):
            with patch.object(scm_sync_worker, "_get_repo_info", return_value=test_repo):
                processed = scm_sync_worker.process_one_job(
                    worker_id=worker_id,
                    circuit_breaker=None,
                )
        
        assert processed is True
        
        # 验证状态
        status = get_job_status(db_conn, job_id)
        assert status["status"] == STATUS_COMPLETED
    
    def test_process_job_transient_failure(self, db_conn, test_repo, worker_id, monkeypatch):
        """测试临时性失败自动重试"""
        from engram.logbook.scm_sync_queue import STATUS_FAILED
        import scm_sync_worker
        
        # 创建失败的同步函数
        failure_registry = {
            "gitlab_commits": stub_sync_failure,
            "commits": stub_sync_failure,
        }
        
        monkeypatch.setattr(
            scm_sync_worker,
            "get_sync_function_registry",
            lambda: failure_registry,
        )
        
        # 创建任务
        job_id = create_test_job(db_conn, test_repo["repo_id"])
        
        def mock_get_connection(config=None):
            return db_conn
        
        with patch.object(scm_sync_worker, "get_connection", mock_get_connection):
            with patch.object(scm_sync_worker, "_get_repo_info", return_value=test_repo):
                processed = scm_sync_worker.process_one_job(
                    worker_id=worker_id,
                    circuit_breaker=None,
                )
        
        assert processed is True
        
        # 验证状态：应为 failed 而非 dead
        status = get_job_status(db_conn, job_id)
        assert status["status"] == STATUS_FAILED
        assert status["attempts"] == 1
    
    def test_process_job_permanent_failure(self, db_conn, test_repo, worker_id, monkeypatch):
        """测试永久性失败直接标记为 dead"""
        from engram.logbook.scm_sync_queue import STATUS_DEAD
        import scm_sync_worker
        
        # 创建永久失败的同步函数
        permanent_failure_registry = {
            "gitlab_commits": stub_sync_permanent_failure,
            "commits": stub_sync_permanent_failure,
        }
        
        monkeypatch.setattr(
            scm_sync_worker,
            "get_sync_function_registry",
            lambda: permanent_failure_registry,
        )
        
        # 创建任务
        job_id = create_test_job(db_conn, test_repo["repo_id"])
        
        def mock_get_connection(config=None):
            return db_conn
        
        with patch.object(scm_sync_worker, "get_connection", mock_get_connection):
            with patch.object(scm_sync_worker, "_get_repo_info", return_value=test_repo):
                processed = scm_sync_worker.process_one_job(
                    worker_id=worker_id,
                    circuit_breaker=None,
                )
        
        assert processed is True
        
        # 验证状态：永久性错误应直接标记为 dead
        status = get_job_status(db_conn, job_id)
        assert status["status"] == STATUS_DEAD


# ============================================================================
# 测试类: Reaper 清理逻辑
# ============================================================================


class TestReaperCleanup:
    """测试 Reaper 清理过期任务"""
    
    def test_reaper_scans_expired_jobs(self, db_conn, test_repo, worker_id):
        """测试 Reaper 扫描过期的 running 任务"""
        from scm_sync_reaper import scan_expired_jobs
        
        # 创建一个过期的 running 任务
        job_id = str(uuid.uuid4())
        expired_time = datetime.now(timezone.utc) - timedelta(seconds=600)
        
        with db_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO scm.sync_jobs (
                    job_id, repo_id, job_type, status, 
                    locked_by, locked_at, lease_seconds
                ) VALUES (%s, %s, 'gitlab_commits', 'running', %s, %s, 60)
            """, (job_id, test_repo["repo_id"], worker_id, expired_time))
            db_conn.commit()
        
        # 扫描过期任务
        expired_jobs = scan_expired_jobs(db_conn, grace_seconds=60, limit=10)
        
        assert len(expired_jobs) >= 1
        expired_job_ids = [str(j["job_id"]) for j in expired_jobs]
        assert job_id in expired_job_ids
    
    def test_reaper_processes_expired_jobs_to_failed(self, db_conn, test_repo, worker_id):
        """测试 Reaper 将过期任务转为 failed"""
        from scm_sync_reaper import scan_expired_jobs, process_expired_jobs, JobRecoveryPolicy
        from engram.logbook.scm_sync_queue import STATUS_FAILED
        
        # 创建过期任务
        job_id = str(uuid.uuid4())
        expired_time = datetime.now(timezone.utc) - timedelta(seconds=600)
        
        with db_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO scm.sync_jobs (
                    job_id, repo_id, job_type, status, 
                    locked_by, locked_at, lease_seconds, attempts, max_attempts
                ) VALUES (%s, %s, 'gitlab_commits', 'running', %s, %s, 60, 0, 3)
            """, (job_id, test_repo["repo_id"], worker_id, expired_time))
            db_conn.commit()
        
        # 扫描并处理
        expired_jobs = scan_expired_jobs(db_conn, grace_seconds=60, limit=10)
        stats = process_expired_jobs(
            db_conn,
            expired_jobs,
            policy=JobRecoveryPolicy.to_failed,
        )
        
        assert stats["to_failed"] >= 1
        
        # 验证状态
        status = get_job_status(db_conn, job_id)
        assert status["status"] == STATUS_FAILED
    
    def test_reaper_marks_max_attempts_as_dead(self, db_conn, test_repo, worker_id):
        """测试 Reaper 将达到最大重试次数的任务标记为 dead"""
        from scm_sync_reaper import scan_expired_jobs, process_expired_jobs, JobRecoveryPolicy
        from engram.logbook.scm_sync_queue import STATUS_DEAD
        
        # 创建过期任务，已达到最大重试次数
        job_id = str(uuid.uuid4())
        expired_time = datetime.now(timezone.utc) - timedelta(seconds=600)
        
        with db_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO scm.sync_jobs (
                    job_id, repo_id, job_type, status, 
                    locked_by, locked_at, lease_seconds, attempts, max_attempts
                ) VALUES (%s, %s, 'gitlab_commits', 'running', %s, %s, 60, 3, 3)
            """, (job_id, test_repo["repo_id"], worker_id, expired_time))
            db_conn.commit()
        
        # 扫描并处理
        expired_jobs = scan_expired_jobs(db_conn, grace_seconds=60, limit=10)
        stats = process_expired_jobs(
            db_conn,
            expired_jobs,
            policy=JobRecoveryPolicy.to_failed,
        )
        
        assert stats["to_dead"] >= 1
        
        # 验证状态
        status = get_job_status(db_conn, job_id)
        assert status["status"] == STATUS_DEAD


# ============================================================================
# 测试类: 调度器入队逻辑
# ============================================================================


class TestSchedulerEnqueue:
    """测试 Scheduler 入队逻辑"""
    
    def test_enqueue_jobs_batch(self, db_conn, multiple_test_repos):
        """测试批量入队任务"""
        import db as scm_db
        
        # 准备要入队的任务
        jobs_to_insert = []
        for repo in multiple_test_repos[:2]:  # 只取前两个 Git 仓库
            jobs_to_insert.append({
                "repo_id": repo["repo_id"],
                "job_type": "gitlab_commits",
                "priority": 100,
                "mode": "incremental",
                "payload_json": {"reason": "test"},
            })
        
        # 批量入队
        job_ids = scm_db.enqueue_sync_jobs_batch(db_conn, jobs_to_insert)
        db_conn.commit()
        
        # 验证入队成功
        assert len(job_ids) == 2
        for job_id in job_ids:
            assert job_id is not None
    
    def test_enqueue_prevents_duplicates(self, db_conn, test_repo):
        """测试防止重复入队（同一 repo_id + job_type 只能有一个活跃任务）"""
        import db as scm_db
        
        # 首次入队
        jobs = [{
            "repo_id": test_repo["repo_id"],
            "job_type": "gitlab_commits",
            "priority": 100,
            "mode": "incremental",
            "payload_json": {},
        }]
        
        job_ids_1 = scm_db.enqueue_sync_jobs_batch(db_conn, jobs)
        db_conn.commit()
        
        assert job_ids_1[0] is not None
        
        # 重复入队应返回 None（被唯一索引阻止）
        job_ids_2 = scm_db.enqueue_sync_jobs_batch(db_conn, jobs)
        db_conn.commit()
        
        assert job_ids_2[0] is None
    
    def test_list_repos_for_scheduling(self, db_conn, multiple_test_repos):
        """测试获取待调度仓库列表"""
        import db as scm_db
        
        repos = scm_db.list_repos_for_scheduling(db_conn)
        
        # 应该至少包含我们创建的仓库
        repo_ids = [r["repo_id"] for r in repos]
        for test_repo in multiple_test_repos:
            assert test_repo["repo_id"] in repo_ids


# ============================================================================
# 测试类: 状态机不变量
# ============================================================================


class TestStateMachineInvariants:
    """测试队列状态机不变量"""
    
    def test_only_one_active_job_per_repo_job_type(self, db_conn, test_repo):
        """
        不变量: 同一 (repo_id, job_type) 只能有一个 pending/running 任务
        """
        # 创建第一个任务
        job_id_1 = create_test_job(db_conn, test_repo["repo_id"], job_type="gitlab_commits")
        
        # 尝试创建第二个相同类型的任务
        job_id_2 = str(uuid.uuid4())
        with pytest.raises(Exception):  # 应该违反唯一约束
            with db_conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO scm.sync_jobs (
                        job_id, repo_id, job_type, status
                    ) VALUES (%s, %s, 'gitlab_commits', 'pending')
                """, (job_id_2, test_repo["repo_id"]))
                db_conn.commit()
    
    def test_claim_is_atomic(self, db_conn, test_repo, worker_id):
        """
        不变量: claim 操作是原子的（同一任务不会被两个 worker 同时获取）
        """
        from engram.logbook.scm_sync_queue import claim, STATUS_RUNNING
        
        # 创建任务
        job_id = create_test_job(db_conn, test_repo["repo_id"])
        
        # 第一个 worker claim
        job = claim(worker_id=worker_id, conn=db_conn)
        assert job is not None
        
        # 第二个 worker 尝试 claim 同一任务
        worker_id_2 = f"worker-{uuid.uuid4().hex[:8]}"
        job_2 = claim(worker_id=worker_id_2, conn=db_conn)
        
        # 第二个 worker 应该获取不到任务（队列为空）
        assert job_2 is None
    
    def test_ack_requires_correct_worker(self, db_conn, test_repo, worker_id):
        """
        不变量: 只有持有锁的 worker 才能 ack
        """
        from engram.logbook.scm_sync_queue import claim, ack
        
        # 创建并 claim 任务
        job_id = create_test_job(db_conn, test_repo["repo_id"])
        job = claim(worker_id=worker_id, conn=db_conn)
        
        # 其他 worker 尝试 ack
        wrong_worker = f"wrong-worker-{uuid.uuid4().hex[:8]}"
        result = ack(str(job["job_id"]), wrong_worker, conn=db_conn)
        
        # 应该失败
        assert result is False
        
        # 原 worker 应该能成功 ack
        result = ack(str(job["job_id"]), worker_id, conn=db_conn)
        assert result is True


# ============================================================================
# 测试类: 端到端流程
# ============================================================================


class TestEndToEndFlow:
    """端到端流程测试"""
    
    def test_full_job_lifecycle(self, db_conn, test_repo, worker_id, stub_sync_registry, monkeypatch):
        """
        完整任务生命周期:
        1. 入队 -> 2. claim -> 3. 执行 -> 4. ack
        """
        import db as scm_db
        from engram.logbook.scm_sync_queue import claim, ack, STATUS_PENDING, STATUS_RUNNING, STATUS_COMPLETED
        
        # 1. 入队
        jobs = [{
            "repo_id": test_repo["repo_id"],
            "job_type": "gitlab_commits",
            "priority": 100,
            "mode": "incremental",
            "payload_json": {"test": True},
        }]
        job_ids = scm_db.enqueue_sync_jobs_batch(db_conn, jobs)
        db_conn.commit()
        
        job_id = str(job_ids[0])
        assert job_id is not None
        
        # 验证初始状态
        status = get_job_status(db_conn, job_id)
        assert status["status"] == STATUS_PENDING
        
        # 2. claim
        job = claim(worker_id=worker_id, conn=db_conn)
        assert job is not None
        assert str(job["job_id"]) == job_id
        
        # 验证 running 状态
        status = get_job_status(db_conn, job_id)
        assert status["status"] == STATUS_RUNNING
        assert status["locked_by"] == worker_id
        
        # 3. 执行（使用 stub）
        sync_func = stub_sync_registry["gitlab_commits"]
        result = sync_func(
            test_repo["repo_id"],
            "incremental",
            {"test": True},
            worker_id,
        )
        assert result["success"] is True
        
        # 4. ack
        ack_result = ack(job_id, worker_id, conn=db_conn)
        assert ack_result is True
        
        # 验证完成状态
        status = get_job_status(db_conn, job_id)
        assert status["status"] == STATUS_COMPLETED
    
    def test_job_retry_cycle(self, db_conn, test_repo, worker_id):
        """
        任务重试流程:
        1. 入队 -> 2. claim -> 3. 失败 -> 4. reaper 处理 -> 5. 再次可 claim
        """
        from engram.logbook.scm_sync_queue import claim, fail_retry, STATUS_FAILED
        from scm_sync_reaper import scan_expired_jobs, process_expired_jobs, JobRecoveryPolicy
        import db as scm_db
        
        # 1. 入队
        jobs = [{
            "repo_id": test_repo["repo_id"],
            "job_type": "gitlab_commits",
            "priority": 100,
            "mode": "incremental",
            "payload_json": {},
        }]
        job_ids = scm_db.enqueue_sync_jobs_batch(db_conn, jobs)
        db_conn.commit()
        
        job_id = str(job_ids[0])
        
        # 2. claim
        job = claim(worker_id=worker_id, conn=db_conn)
        assert job is not None
        
        # 3. 失败
        fail_retry(job_id, worker_id, "Test failure", backoff_seconds=0, conn=db_conn)
        
        # 验证 failed 状态
        status = get_job_status(db_conn, job_id)
        assert status["status"] == STATUS_FAILED
        assert status["attempts"] == 1
        
        # 4. 可以再次 claim（因为 not_before 设为 0 秒后）
        worker_id_2 = f"worker-{uuid.uuid4().hex[:8]}"
        job_2 = claim(worker_id=worker_id_2, conn=db_conn)
        
        # 应该能获取到同一任务
        assert job_2 is not None
        assert str(job_2["job_id"]) == job_id
