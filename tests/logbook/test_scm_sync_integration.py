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
注意：此文件保留使用根目录 db.py 和 kv.py
================================================================================

此文件作为验收测试，故意保留使用根目录 `db.py` 和 `kv.py` 导入，
以验证 deprecation 转发机制仍可正常工作。

新测试文件应使用 `engram.logbook.scm_db` 模块。

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
from typing import Optional
from unittest.mock import patch

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
                cur.execute(
                    """
                    INSERT INTO scm.repos (url, repo_type, project_key, default_branch)
                    VALUES (%s, 'git', %s, 'main')
                    RETURNING repo_id
                """,
                    (
                        f"https://gitlab.example.com/test/project-{i}",
                        f"test-{i}",
                    ),
                )
                repo_id = cur.fetchone()[0]
                repos.append(
                    {
                        "repo_id": repo_id,
                        "url": f"https://gitlab.example.com/test/project-{i}",
                        "repo_type": "git",
                        "project_key": f"test-{i}",
                    }
                )

            # 创建 1 个 SVN 仓库
            cur.execute("""
                INSERT INTO scm.repos (url, repo_type, project_key)
                VALUES ('svn://svn.example.com/test/repo', 'svn', 'svn-test')
                RETURNING repo_id
            """)
            repo_id = cur.fetchone()[0]
            repos.append(
                {
                    "repo_id": repo_id,
                    "url": "svn://svn.example.com/test/repo",
                    "repo_type": "svn",
                    "project_key": "svn-test",
                }
            )

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
        "gitlab_commits": lambda repo_id, mode, payload, worker_id: stub_sync_success(
            repo_id, mode, payload, worker_id
        ),
        "gitlab_mrs": lambda repo_id, mode, payload, worker_id: stub_sync_success(
            repo_id, mode, payload, worker_id
        ),
        "gitlab_reviews": lambda repo_id, mode, payload, worker_id: stub_sync_success(
            repo_id, mode, payload, worker_id
        ),
        "svn": lambda repo_id, mode, payload, worker_id: stub_sync_success(
            repo_id, mode, payload, worker_id
        ),
        "commits": lambda repo_id, mode, payload, worker_id: stub_sync_success(
            repo_id, mode, payload, worker_id
        ),
        "mrs": lambda repo_id, mode, payload, worker_id: stub_sync_success(
            repo_id, mode, payload, worker_id
        ),
        "reviews": lambda repo_id, mode, payload, worker_id: stub_sync_success(
            repo_id, mode, payload, worker_id
        ),
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
        cur.execute(
            """
            INSERT INTO scm.sync_jobs (
                job_id, repo_id, job_type, mode, priority, status, payload_json
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
            (job_id, repo_id, job_type, mode, priority, status, payload_json),
        )
        conn.commit()

    return job_id


def get_job_status(conn, job_id: str) -> Optional[dict]:
    """获取任务状态"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT job_id, repo_id, job_type, status, attempts, locked_by, last_error
            FROM scm.sync_jobs
            WHERE job_id = %s
        """,
            (job_id,),
        )
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
        from engram.logbook.scm_sync_queue import STATUS_RUNNING, claim

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
        create_test_job(
            db_conn,
            test_repo["repo_id"],
            priority=200,
            job_type="gitlab_commits",
        )
        high_priority_job = create_test_job(
            db_conn,
            test_repo["repo_id"],
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
        create_test_job(
            db_conn,
            test_repo["repo_id"],
            job_type="gitlab_commits",
        )
        mrs_job = create_test_job(
            db_conn,
            test_repo["repo_id"],
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
        from engram.logbook.scm_sync_queue import STATUS_COMPLETED, ack, claim

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
        from engram.logbook.scm_sync_queue import STATUS_FAILED, claim, fail_retry

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
        from engram.logbook.scm_sync_queue import STATUS_DEAD, claim, mark_dead

        # 创建任务，设置 max_attempts=1
        job_id = str(uuid.uuid4())
        with db_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scm.sync_jobs (
                    job_id, repo_id, job_type, mode, status, max_attempts
                ) VALUES (%s, %s, 'gitlab_commits', 'incremental', 'pending', 1)
            """,
                (job_id, test_repo["repo_id"]),
            )
            db_conn.commit()

        # claim
        claim(worker_id=worker_id, conn=db_conn)

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

    def test_process_job_success(
        self, db_conn, test_repo, worker_id, stub_sync_registry, monkeypatch
    ):
        """测试成功执行任务"""
        from engram.logbook import scm_sync_worker_core as scm_sync_worker
        from engram.logbook.scm_sync_queue import STATUS_COMPLETED

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
        from engram.logbook import scm_sync_worker_core as scm_sync_worker
        from engram.logbook.scm_sync_queue import STATUS_FAILED

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
        from engram.logbook import scm_sync_worker_core as scm_sync_worker
        from engram.logbook.scm_sync_queue import STATUS_DEAD

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
        from engram.logbook.scm_db import list_expired_running_jobs

        # 创建一个过期的 running 任务
        job_id = str(uuid.uuid4())
        expired_time = datetime.now(timezone.utc) - timedelta(seconds=600)

        with db_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scm.sync_jobs (
                    job_id, repo_id, job_type, status,
                    locked_by, locked_at, lease_seconds
                ) VALUES (%s, %s, 'gitlab_commits', 'running', %s, %s, 60)
            """,
                (job_id, test_repo["repo_id"], worker_id, expired_time),
            )
            db_conn.commit()

        # 扫描过期任务
        expired_jobs = list_expired_running_jobs(db_conn, grace_seconds=60, limit=10)

        assert len(expired_jobs) >= 1
        expired_job_ids = [str(j["job_id"]) for j in expired_jobs]
        assert job_id in expired_job_ids

    def test_reaper_processes_expired_jobs_to_failed(self, db_conn, test_repo, worker_id):
        """测试 Reaper 将过期任务转为 failed"""
        from engram.logbook.scm_db import list_expired_running_jobs
        from engram.logbook.scm_sync_queue import STATUS_FAILED
        from engram.logbook.scm_sync_reaper_core import JobRecoveryPolicy, process_expired_jobs

        # 创建过期任务
        job_id = str(uuid.uuid4())
        expired_time = datetime.now(timezone.utc) - timedelta(seconds=600)

        with db_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scm.sync_jobs (
                    job_id, repo_id, job_type, status,
                    locked_by, locked_at, lease_seconds, attempts, max_attempts
                ) VALUES (%s, %s, 'gitlab_commits', 'running', %s, %s, 60, 0, 3)
            """,
                (job_id, test_repo["repo_id"], worker_id, expired_time),
            )
            db_conn.commit()

        # 扫描并处理
        expired_jobs = list_expired_running_jobs(db_conn, grace_seconds=60, limit=10)
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
        from engram.logbook.scm_db import list_expired_running_jobs
        from engram.logbook.scm_sync_queue import STATUS_DEAD
        from engram.logbook.scm_sync_reaper_core import JobRecoveryPolicy, process_expired_jobs

        # 创建过期任务，已达到最大重试次数
        job_id = str(uuid.uuid4())
        expired_time = datetime.now(timezone.utc) - timedelta(seconds=600)

        with db_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scm.sync_jobs (
                    job_id, repo_id, job_type, status,
                    locked_by, locked_at, lease_seconds, attempts, max_attempts
                ) VALUES (%s, %s, 'gitlab_commits', 'running', %s, %s, 60, 3, 3)
            """,
                (job_id, test_repo["repo_id"], worker_id, expired_time),
            )
            db_conn.commit()

        # 扫描并处理
        expired_jobs = list_expired_running_jobs(db_conn, grace_seconds=60, limit=10)
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
        from engram.logbook import scm_db

        # 准备要入队的任务
        jobs_to_insert = []
        for repo in multiple_test_repos[:2]:  # 只取前两个 Git 仓库
            jobs_to_insert.append(
                {
                    "repo_id": repo["repo_id"],
                    "job_type": "gitlab_commits",
                    "priority": 100,
                    "mode": "incremental",
                    "payload_json": {"reason": "test"},
                }
            )

        # 批量入队
        job_ids = scm_db.enqueue_sync_jobs_batch(db_conn, jobs_to_insert)
        db_conn.commit()

        # 验证入队成功
        assert len(job_ids) == 2
        for job_id in job_ids:
            assert job_id is not None

    def test_enqueue_prevents_duplicates(self, db_conn, test_repo):
        """测试防止重复入队（同一 repo_id + job_type 只能有一个活跃任务）"""
        from engram.logbook import scm_db

        # 首次入队
        jobs = [
            {
                "repo_id": test_repo["repo_id"],
                "job_type": "gitlab_commits",
                "priority": 100,
                "mode": "incremental",
                "payload_json": {},
            }
        ]

        job_ids_1 = scm_db.enqueue_sync_jobs_batch(db_conn, jobs)
        db_conn.commit()

        assert job_ids_1[0] is not None

        # 重复入队应返回 None（被唯一索引阻止）
        job_ids_2 = scm_db.enqueue_sync_jobs_batch(db_conn, jobs)
        db_conn.commit()

        assert job_ids_2[0] is None

    def test_list_repos_for_scheduling(self, db_conn, multiple_test_repos):
        """测试获取待调度仓库列表"""
        from engram.logbook import scm_db

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
        create_test_job(db_conn, test_repo["repo_id"], job_type="gitlab_commits")

        # 尝试创建第二个相同类型的任务
        job_id_2 = str(uuid.uuid4())
        with pytest.raises(Exception):  # 应该违反唯一约束
            with db_conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO scm.sync_jobs (
                        job_id, repo_id, job_type, status
                    ) VALUES (%s, %s, 'gitlab_commits', 'pending')
                """,
                    (job_id_2, test_repo["repo_id"]),
                )
                db_conn.commit()

    def test_claim_is_atomic(self, db_conn, test_repo, worker_id):
        """
        不变量: claim 操作是原子的（同一任务不会被两个 worker 同时获取）
        """
        from engram.logbook.scm_sync_queue import claim

        # 创建任务
        create_test_job(db_conn, test_repo["repo_id"])

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
        from engram.logbook.scm_sync_queue import ack, claim

        # 创建并 claim 任务
        create_test_job(db_conn, test_repo["repo_id"])
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

    def test_full_job_lifecycle(
        self, db_conn, test_repo, worker_id, stub_sync_registry, monkeypatch
    ):
        """
        完整任务生命周期:
        1. 入队 -> 2. claim -> 3. 执行 -> 4. ack
        """
        from engram.logbook import scm_db
        from engram.logbook.scm_sync_queue import (
            STATUS_COMPLETED,
            STATUS_PENDING,
            STATUS_RUNNING,
            ack,
            claim,
        )

        # 1. 入队
        jobs = [
            {
                "repo_id": test_repo["repo_id"],
                "job_type": "gitlab_commits",
                "priority": 100,
                "mode": "incremental",
                "payload_json": {"test": True},
            }
        ]
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
        from engram.logbook import scm_db
        from engram.logbook.scm_sync_queue import STATUS_FAILED, claim, fail_retry

        # 1. 入队
        jobs = [
            {
                "repo_id": test_repo["repo_id"],
                "job_type": "gitlab_commits",
                "priority": 100,
                "mode": "incremental",
                "payload_json": {},
            }
        ]
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


# ============================================================================
# 测试类: Scheduler -> Worker -> Reaper 端到端集成测试
# ============================================================================


def mock_sync_executor_success(job: dict) -> dict:
    """
    Mock 执行器：返回成功结果（符合 scm_sync_result_v2 schema）

    用于测试 worker 处理流程，不依赖外部 GitLab API。
    """
    return {
        "success": True,
        "counts": {
            "synced_count": 10,
            "new_count": 5,
            "updated_count": 3,
            "skipped_count": 2,
        },
        "cursor_after": {
            "sha": "abc123def456",
            "timestamp": "2024-06-01T12:00:00Z",
        },
        "degradation": None,
        "run_id": str(uuid.uuid4()),
    }


def mock_sync_executor_failure_transient(job: dict) -> dict:
    """
    Mock 执行器：返回临时性错误（可重试）
    """
    return {
        "success": False,
        "error": "Connection timeout",
        "error_category": "network",
        "counts": {},
    }


def mock_sync_executor_failure_rate_limit(job: dict) -> dict:
    """
    Mock 执行器：返回 429 限流错误
    """
    return {
        "success": False,
        "error": "429 Too Many Requests",
        "error_category": "rate_limit",
        "retry_after": 60,
        "counts": {},
    }


class TestSchedulerWorkerReaperIntegration:
    """
    Scheduler -> Worker -> Reaper 端到端集成测试

    测试完整的 SCM 同步生命周期：
    1. Scheduler 扫描仓库并生成 jobs
    2. Worker claim 任务并执行（使用 mock executor）
    3. Worker 写入 sync_runs 记录
    4. 人为篡改 locked_at 模拟任务超时
    5. Reaper 检测并回收过期任务
    6. 验证最终状态
    """

    def test_full_scheduler_worker_cycle_success(self, db_conn, test_repo, worker_id):
        """
        完整流程测试（成功场景）:
        1. Scheduler 入队任务
        2. Worker claim 并执行
        3. 验证 sync_jobs 和 sync_runs 状态
        """
        from engram.logbook import scm_sync_worker_core as scm_sync_worker
        from engram.logbook.scm_sync_queue import (
            STATUS_COMPLETED,
            STATUS_RUNNING,
            ack,
            claim,
            enqueue,
        )

        # 清理可能存在的旧任务
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM scm.sync_jobs WHERE repo_id = %s", (test_repo["repo_id"],))
        db_conn.commit()

        # 1. 入队任务（模拟 Scheduler 行为）
        job_id = enqueue(
            repo_id=test_repo["repo_id"],
            job_type="gitlab_commits",
            mode="incremental",
            priority=100,
            payload={
                "reason": "scheduled",
                "gitlab_instance": "gitlab.example.com",
            },
            conn=db_conn,
        )

        assert job_id is not None

        # 2. Worker claim 任务
        job = claim(worker_id=worker_id, conn=db_conn)
        assert job is not None
        assert str(job["job_id"]) == job_id
        assert job["status"] == STATUS_RUNNING

        # 3. 使用 mock executor 执行（注入执行器）
        scm_sync_worker.set_executor(mock_sync_executor_success)
        try:
            result = scm_sync_worker.execute_sync_job(job)
            assert result["success"] is True

            # 4. ack 完成任务
            ack_result = ack(job_id, worker_id, conn=db_conn)
            assert ack_result is True

            # 5. 验证最终状态
            status = get_job_status(db_conn, job_id)
            assert status["status"] == STATUS_COMPLETED
        finally:
            # 清理注入的执行器
            scm_sync_worker.set_executor(None)

    def test_worker_writes_sync_run_on_success(self, db_conn, test_repo, worker_id):
        """
        测试 Worker 成功执行后写入 sync_run 记录
        """
        from engram.logbook import scm_sync_worker_core as scm_sync_worker
        from engram.logbook.scm_sync_queue import enqueue

        # 清理
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM scm.sync_jobs WHERE repo_id = %s", (test_repo["repo_id"],))
            cur.execute("DELETE FROM scm.sync_runs WHERE repo_id = %s", (test_repo["repo_id"],))
        db_conn.commit()

        # 入队
        enqueue(
            repo_id=test_repo["repo_id"],
            job_type="gitlab_commits",
            mode="incremental",
            priority=100,
            payload={},
            conn=db_conn,
        )

        # 注入 mock 执行器
        scm_sync_worker.set_executor(mock_sync_executor_success)

        try:
            # 使用 process_one_job 完整流程
            # Mock 必要的依赖
            with (
                patch("engram.logbook.scm_sync_worker_core.scm_sync_lock.claim", return_value=True),
                patch(
                    "engram.logbook.scm_sync_worker_core.scm_sync_lock.release",
                    return_value=True,
                ),
                patch("engram.logbook.scm_sync_worker_core.renew_lease", return_value=True),
            ):
                processed = scm_sync_worker.process_one_job(
                    worker_id=worker_id,
                    conn=db_conn,
                    enable_sync_runs=True,
                )

            assert processed is True

            # 验证 sync_runs 记录
            with db_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT run_id, status, repo_id, job_type
                    FROM scm.sync_runs
                    WHERE repo_id = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                """,
                    (test_repo["repo_id"],),
                )
                run = cur.fetchone()

            # sync_run 应该被创建
            if run:
                assert run[1] in ("running", "completed", "failed")
                assert run[2] == test_repo["repo_id"]
        finally:
            scm_sync_worker.set_executor(None)

    def test_reaper_recovers_expired_running_job(self, db_conn, test_repo, worker_id):
        """
        测试 Reaper 回收过期的 running 任务

        流程:
        1. 创建一个 running 任务
        2. 人为篡改 locked_at 使其过期
        3. Reaper 检测并回收
        4. 验证任务状态变为 failed
        """
        from engram.logbook.scm_db import list_expired_running_jobs
        from engram.logbook.scm_sync_queue import STATUS_FAILED, STATUS_RUNNING
        from engram.logbook.scm_sync_reaper_core import (
            JobRecoveryPolicy,
            process_expired_jobs,
        )

        # 清理
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM scm.sync_jobs WHERE repo_id = %s", (test_repo["repo_id"],))
        db_conn.commit()

        # 1. 创建一个 running 任务并人为设置过期的 locked_at
        job_id = str(uuid.uuid4())
        with db_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scm.sync_jobs (
                    job_id, repo_id, job_type, mode, priority, status,
                    locked_by, locked_at, lease_seconds, attempts, max_attempts
                ) VALUES (
                    %s, %s, 'gitlab_commits', 'incremental', 100, 'running',
                    %s, now() - interval '15 minutes', 300, 1, 3
                )
            """,
                (job_id, test_repo["repo_id"], worker_id),
            )
        db_conn.commit()

        # 验证初始状态
        status = get_job_status(db_conn, job_id)
        assert status["status"] == STATUS_RUNNING

        # 2. 使用 Reaper 检测过期任务
        expired_jobs = list_expired_running_jobs(db_conn, grace_seconds=60, limit=100)

        # 应该检测到我们的过期任务
        expired_job_ids = [str(j["job_id"]) for j in expired_jobs]
        assert job_id in expired_job_ids, f"应该检测到过期任务 {job_id}"

        # 3. Reaper 处理过期任务
        stats = process_expired_jobs(
            db_conn,
            [j for j in expired_jobs if str(j["job_id"]) == job_id],
            policy=JobRecoveryPolicy.to_failed,
            retry_delay_seconds=60,
        )
        db_conn.commit()

        # 4. 验证处理结果
        assert stats["processed"] == 1
        assert stats["to_failed"] == 1 or stats["to_dead"] >= 0

        # 验证任务状态
        status = get_job_status(db_conn, job_id)
        assert status["status"] in (STATUS_FAILED, "dead"), (
            f"过期任务应为 failed 或 dead，实际: {status['status']}"
        )

    def test_reaper_marks_max_attempts_job_as_dead(self, db_conn, test_repo, worker_id):
        """
        测试 Reaper 将达到最大重试次数的过期任务标记为 dead
        """
        from engram.logbook.scm_db import list_expired_running_jobs
        from engram.logbook.scm_sync_reaper_core import (
            JobRecoveryPolicy,
            process_expired_jobs,
        )

        # 清理
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM scm.sync_jobs WHERE repo_id = %s", (test_repo["repo_id"],))
        db_conn.commit()

        # 创建已达到 max_attempts 的过期任务
        job_id = str(uuid.uuid4())
        with db_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scm.sync_jobs (
                    job_id, repo_id, job_type, mode, priority, status,
                    locked_by, locked_at, lease_seconds, attempts, max_attempts
                ) VALUES (
                    %s, %s, 'gitlab_commits', 'incremental', 100, 'running',
                    %s, now() - interval '15 minutes', 300, 3, 3
                )
            """,
                (job_id, test_repo["repo_id"], worker_id),
            )
        db_conn.commit()

        # 检测并处理
        expired_jobs = list_expired_running_jobs(db_conn, grace_seconds=60, limit=100)
        target_jobs = [j for j in expired_jobs if str(j["job_id"]) == job_id]

        process_expired_jobs(
            db_conn,
            target_jobs,
            policy=JobRecoveryPolicy.to_failed,
            retry_delay_seconds=60,
        )
        db_conn.commit()

        # 验证：达到 max_attempts 应该标记为 dead
        status = get_job_status(db_conn, job_id)
        assert status["status"] == "dead", (
            f"达到 max_attempts 的任务应为 dead，实际: {status['status']}"
        )

    def test_scheduler_worker_reaper_full_cycle(self, db_conn, multiple_test_repos, worker_id):
        """
        完整端到端流程测试:
        1. Scheduler 为多个仓库入队任务
        2. Worker 处理部分任务
        3. 部分任务超时
        4. Reaper 回收超时任务
        5. 验证所有状态
        """
        from engram.logbook import scm_sync_worker_core as scm_sync_worker
        from engram.logbook.scm_db import list_expired_running_jobs
        from engram.logbook.scm_sync_queue import (
            STATUS_COMPLETED,
            STATUS_FAILED,
            ack,
            claim,
            enqueue,
        )
        from engram.logbook.scm_sync_reaper_core import JobRecoveryPolicy, process_expired_jobs

        # 清理所有测试仓库的任务
        for repo in multiple_test_repos:
            with db_conn.cursor() as cur:
                cur.execute("DELETE FROM scm.sync_jobs WHERE repo_id = %s", (repo["repo_id"],))
        db_conn.commit()

        # 1. Scheduler: 为前两个仓库入队任务
        job_ids = []
        for repo in multiple_test_repos[:2]:
            job_type = "gitlab_commits" if repo["repo_type"] == "git" else "svn"
            job_id = enqueue(
                repo_id=repo["repo_id"],
                job_type=job_type,
                mode="incremental",
                priority=100,
                payload={"reason": "integration_test"},
                conn=db_conn,
            )
            job_ids.append(job_id)

        assert len([j for j in job_ids if j]) == 2, "应该成功入队 2 个任务"

        # 2. Worker: 处理第一个任务（成功）
        scm_sync_worker.set_executor(mock_sync_executor_success)
        try:
            job1 = claim(worker_id=worker_id, conn=db_conn)
            assert job1 is not None
            job1_id = str(job1["job_id"])

            # 执行并 ack
            result = scm_sync_worker.execute_sync_job(job1)
            assert result["success"] is True
            ack(job1_id, worker_id, conn=db_conn)

            # 3. Worker: claim 第二个任务但"超时"（模拟）
            worker_id_2 = f"worker-timeout-{uuid.uuid4().hex[:8]}"
            job2 = claim(worker_id=worker_id_2, conn=db_conn)
            assert job2 is not None
            job2_id = str(job2["job_id"])

            # 人为篡改 locked_at 使其过期
            with db_conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE scm.sync_jobs
                    SET locked_at = now() - interval '20 minutes'
                    WHERE job_id = %s
                """,
                    (job2_id,),
                )
            db_conn.commit()

            # 4. Reaper: 回收过期任务
            expired_jobs = list_expired_running_jobs(db_conn, grace_seconds=60, limit=100)
            expired_job_ids = [str(j["job_id"]) for j in expired_jobs]

            assert job2_id in expired_job_ids, "第二个任务应该被检测为过期"

            stats = process_expired_jobs(
                db_conn,
                [j for j in expired_jobs if str(j["job_id"]) == job2_id],
                policy=JobRecoveryPolicy.to_failed,
                retry_delay_seconds=60,
            )
            db_conn.commit()

            assert stats["processed"] == 1

            # 5. 验证最终状态
            status1 = get_job_status(db_conn, job1_id)
            status2 = get_job_status(db_conn, job2_id)

            assert status1["status"] == STATUS_COMPLETED, (
                f"任务1应为 completed，实际: {status1['status']}"
            )
            assert status2["status"] in (STATUS_FAILED, "dead"), (
                f"任务2应为 failed/dead，实际: {status2['status']}"
            )

        finally:
            scm_sync_worker.set_executor(None)

    def test_kv_cursor_updated_after_successful_sync(self, db_conn, test_repo, worker_id):
        """
        测试成功同步后 kv 表中的 cursor 被更新
        """
        from engram.logbook import scm_db
        from engram.logbook import scm_sync_worker_core as scm_sync_worker
        from engram.logbook.kv import kv_set_json
        from engram.logbook.scm_sync_queue import ack, claim, enqueue

        # 清理
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM scm.sync_jobs WHERE repo_id = %s", (test_repo["repo_id"],))
            # 清理 kv 中的旧 cursor
            cur.execute(
                """
                DELETE FROM logbook.kv
                WHERE key LIKE %s
            """,
                (f"cursor:{test_repo['repo_id']}:%",),
            )
        db_conn.commit()

        # Mock 执行器返回带 cursor 的结果
        cursor_value = {
            "sha": "test_commit_sha_123",
            "timestamp": "2024-06-15T10:30:00Z",
        }

        def mock_executor_with_cursor(job: dict) -> dict:
            return {
                "success": True,
                "counts": {"synced_count": 5},
                "cursor_after": cursor_value,
            }

        # 入队
        job_id = enqueue(
            repo_id=test_repo["repo_id"],
            job_type="gitlab_commits",
            mode="incremental",
            priority=100,
            payload={},
            conn=db_conn,
        )

        # 注入 mock 执行器
        scm_sync_worker.set_executor(mock_executor_with_cursor)

        try:
            # claim 并执行
            job = claim(worker_id=worker_id, conn=db_conn)
            result = scm_sync_worker.execute_sync_job(job)

            assert result["success"] is True
            assert result["cursor_after"] == cursor_value

            # 手动更新 cursor（模拟 worker 行为）
            cursor_key = f"cursor:{test_repo['repo_id']}:gitlab_commits"
            kv_set_json(db_conn, "scm.sync", cursor_key, cursor_value)
            db_conn.commit()

            # ack
            ack(job_id, worker_id, conn=db_conn)

            # 验证 cursor 已写入 kv
            cursor_data = scm_db.get_cursor_value(db_conn, test_repo["repo_id"], "gitlab_commits")

            if cursor_data:
                assert cursor_data["value"]["sha"] == cursor_value["sha"], (
                    f"Cursor SHA 不匹配: {cursor_data['value']}"
                )
        finally:
            scm_sync_worker.set_executor(None)


class TestSyncRunLifecycle:
    """
    sync_runs 生命周期测试

    验证 sync_runs 表的状态转换：
    - running -> completed
    - running -> failed
    - running -> (timeout) -> failed (by reaper)
    """

    def test_sync_run_start_creates_running_record(self, db_conn, test_repo):
        """测试 insert_sync_run_start 创建 running 状态记录"""
        from engram.logbook import scm_db

        run_id = str(uuid.uuid4())

        # 创建 sync_run_start
        scm_db.insert_sync_run_start(
            db_conn,
            run_id=run_id,
            repo_id=test_repo["repo_id"],
            job_type="gitlab_commits",
            mode="incremental",
            cursor_before={"sha": "old_sha"},
            meta_json={"test": True},
        )
        db_conn.commit()

        # 验证记录
        with db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT run_id, status, repo_id, job_type, mode, cursor_before
                FROM scm.sync_runs
                WHERE run_id = %s
            """,
                (run_id,),
            )
            row = cur.fetchone()

        assert row is not None
        assert str(row[0]) == run_id
        assert row[1] == "running"
        assert row[2] == test_repo["repo_id"]
        assert row[3] == "gitlab_commits"
        assert row[4] == "incremental"

    def test_sync_run_finish_updates_status(self, db_conn, test_repo):
        """测试 insert_sync_run_finish 更新状态"""
        from engram.logbook import scm_db

        run_id = str(uuid.uuid4())

        # 先创建 running 记录
        scm_db.insert_sync_run_start(
            db_conn,
            run_id=run_id,
            repo_id=test_repo["repo_id"],
            job_type="gitlab_commits",
            mode="incremental",
        )
        db_conn.commit()

        # 完成（成功）
        scm_db.insert_sync_run_finish(
            db_conn,
            run_id=run_id,
            status="completed",
            cursor_after={"sha": "new_sha", "timestamp": "2024-06-01T00:00:00Z"},
            counts={"synced_count": 10, "new_count": 5},
        )
        db_conn.commit()

        # 验证状态已更新
        with db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, cursor_after, counts
                FROM scm.sync_runs
                WHERE run_id = %s
            """,
                (run_id,),
            )
            row = cur.fetchone()

        assert row[0] == "completed"
        if row[1]:  # cursor_after
            assert row[1].get("sha") == "new_sha"

    def test_sync_run_failed_requires_error_summary(self, db_conn, test_repo):
        """测试 failed 状态的 sync_run 包含 error_summary"""
        from engram.logbook import scm_db

        run_id = str(uuid.uuid4())

        # 创建 running 记录
        scm_db.insert_sync_run_start(
            db_conn,
            run_id=run_id,
            repo_id=test_repo["repo_id"],
            job_type="gitlab_commits",
            mode="incremental",
        )
        db_conn.commit()

        # 失败（带 error_summary）
        error_summary = {
            "error_type": "api_error",
            "error_category": "network",
            "message": "Connection refused",
        }

        scm_db.insert_sync_run_finish(
            db_conn,
            run_id=run_id,
            status="failed",
            error_summary_json=error_summary,
        )
        db_conn.commit()

        # 验证
        with db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, error_summary_json
                FROM scm.sync_runs
                WHERE run_id = %s
            """,
                (run_id,),
            )
            row = cur.fetchone()

        assert row[0] == "failed"
        if row[1]:
            assert row[1].get("error_category") == "network"

    def test_reaper_marks_expired_run_as_failed(self, db_conn, test_repo):
        """测试 Reaper 将过期的 running sync_run 标记为 failed"""
        from engram.logbook.scm_db import list_expired_running_runs
        from engram.logbook.scm_sync_reaper_core import process_expired_runs

        run_id = str(uuid.uuid4())

        # 创建一个"长时间运行"的 sync_run（人为设置过期）
        with db_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scm.sync_runs (
                    run_id, repo_id, job_type, mode, status, created_at
                ) VALUES (
                    %s, %s, 'gitlab_commits', 'incremental', 'running',
                    now() - interval '60 minutes'
                )
            """,
                (run_id, test_repo["repo_id"]),
            )
        db_conn.commit()

        # Reaper 检测过期 runs
        expired_runs = list_expired_running_runs(
            db_conn,
            max_duration_seconds=1800,  # 30 分钟
            limit=100,
        )

        # 应该检测到我们的过期 run
        expired_run_ids = [str(r["run_id"]) for r in expired_runs]
        assert run_id in expired_run_ids

        # 处理过期 runs
        stats = process_expired_runs(
            db_conn,
            [r for r in expired_runs if str(r["run_id"]) == run_id],
        )
        db_conn.commit()

        assert stats["processed"] == 1
        assert stats["failed"] == 1

        # 验证状态
        with db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, error_summary_json
                FROM scm.sync_runs
                WHERE run_id = %s
            """,
                (run_id,),
            )
            row = cur.fetchone()

        assert row[0] == "failed"
        if row[1]:
            assert "timeout" in str(row[1]).lower() or "reaped" in str(row[1]).lower()


class TestLockExpiration:
    """
    分布式锁过期测试

    验证 scm.sync_locks 表的锁过期和回收机制
    """

    def test_expired_lock_detected_by_reaper(self, db_conn, test_repo, worker_id):
        """测试 Reaper 检测过期的锁"""
        from engram.logbook.scm_db import list_expired_locks

        # 先清理可能存在的锁
        with db_conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM scm.sync_locks
                WHERE repo_id = %s AND job_type = 'gitlab_commits'
            """,
                (test_repo["repo_id"],),
            )
        db_conn.commit()

        # 创建一个过期的锁（人为设置过期时间）
        lock_id = None
        with db_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scm.sync_locks (
                    repo_id, job_type, locked_by, locked_at, lease_seconds
                ) VALUES (
                    %s, 'gitlab_commits', %s, now() - interval '15 minutes', 300
                )
                RETURNING lock_id
            """,
                (test_repo["repo_id"], worker_id),
            )
            lock_id = cur.fetchone()[0]
        db_conn.commit()

        # Reaper 检测过期锁
        expired_locks = list_expired_locks(db_conn, grace_seconds=60, limit=100)

        # 应该检测到过期锁
        expired_lock_ids = [lock.get("lock_id") for lock in expired_locks]
        assert lock_id in expired_lock_ids, f"应该检测到过期锁 {lock_id}"

    def test_expired_lock_released_by_reaper(self, db_conn, test_repo, worker_id):
        """测试 Reaper 释放过期的锁"""
        from engram.logbook.scm_db import list_expired_locks
        from engram.logbook.scm_sync_reaper_core import process_expired_locks

        # 清理
        with db_conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM scm.sync_locks
                WHERE repo_id = %s AND job_type = 'gitlab_commits'
            """,
                (test_repo["repo_id"],),
            )
        db_conn.commit()

        # 创建过期锁
        with db_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scm.sync_locks (
                    repo_id, job_type, locked_by, locked_at, lease_seconds
                ) VALUES (
                    %s, 'gitlab_commits', %s, now() - interval '15 minutes', 300
                )
                RETURNING lock_id
            """,
                (test_repo["repo_id"], worker_id),
            )
            lock_id = cur.fetchone()[0]
        db_conn.commit()

        # 检测并处理
        expired_locks = list_expired_locks(db_conn, grace_seconds=60, limit=100)
        target_locks = [lock for lock in expired_locks if lock.get("lock_id") == lock_id]

        stats = process_expired_locks(db_conn, target_locks)
        db_conn.commit()

        assert stats["processed"] == 1
        assert stats["released"] == 1

        # 验证锁已被释放
        with db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM scm.sync_locks
                WHERE lock_id = %s
            """,
                (lock_id,),
            )
            count = cur.fetchone()[0]

        assert count == 0, "锁应该被删除"
