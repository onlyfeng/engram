# -*- coding: utf-8 -*-
"""
SCM Sync Queue 任务队列模块单元测试

测试:
- enqueue: 任务入队、防重复
- claim: 获取任务、并发 claim、过期锁回收
- ack: 确认完成
- fail_retry: 失败重试、指数退避
- mark_dead: 标记死信
- renew_lease: 续租
"""

import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
import psycopg


class TestEnqueue:
    """enqueue 函数测试"""

    def test_enqueue_new_job(self, migrated_db):
        """成功入队新任务"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            # 创建测试仓库
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_enqueue.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

            with patch('engram_step1.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram_step1.scm_sync_queue import enqueue, get_job

                # 入队任务
                job_id = enqueue(
                    repo_id=repo_id,
                    job_type="gitlab_commits",
                    mode="incremental",
                    priority=50,
                    payload={"page": 1},
                )
                
                assert job_id is not None
                
                # 验证任务状态
                job = get_job(job_id)
                assert job is not None
                assert job["repo_id"] == repo_id
                assert job["job_type"] == "gitlab_commits"
                assert job["mode"] == "incremental"
                assert job["priority"] == 50
                assert job["payload"] == {"page": 1}
                assert job["status"] == "pending"
                assert job["attempts"] == 0
                
        finally:
            if job_id or repo_id:
                with conn.cursor() as cur:
                    if job_id:
                        cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                    if repo_id:
                        cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_enqueue_duplicate_returns_none(self, migrated_db):
        """重复入队返回 None"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_dup.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

            with patch('engram_step1.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram_step1.scm_sync_queue import enqueue

                # 第一次入队
                job_id = enqueue(repo_id=repo_id, job_type="gitlab_commits")
                assert job_id is not None

                # 第二次入队同一任务类型
                dup_job_id = enqueue(repo_id=repo_id, job_type="gitlab_commits")
                assert dup_job_id is None, "重复入队应该返回 None"

        finally:
            if job_id or repo_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_enqueue_different_job_types(self, migrated_db):
        """不同任务类型可以同时入队"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_ids = []
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_diff_types.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

            with patch('engram_step1.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram_step1.scm_sync_queue import enqueue

                for job_type in ["gitlab_commits", "gitlab_mrs", "gitlab_reviews"]:
                    job_id = enqueue(repo_id=repo_id, job_type=job_type)
                    assert job_id is not None
                    job_ids.append(job_id)

                assert len(job_ids) == 3

        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestClaim:
    """claim 函数测试"""

    def test_claim_pending_job(self, migrated_db):
        """获取 pending 状态的任务"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_claim.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'pending')
                """, (repo_id,))

            with patch('engram_step1.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram_step1.scm_sync_queue import claim, get_job

                # claim 任务
                job = claim(worker_id="worker-1")
                
                assert job is not None
                assert job["repo_id"] == repo_id
                assert job["job_type"] == "gitlab_commits"
                assert job["attempts"] == 1  # 已增加

                # 验证状态已更新
                job_info = get_job(job["job_id"])
                assert job_info["status"] == "running"
                assert job_info["locked_by"] == "worker-1"

        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_claim_respects_priority(self, migrated_db):
        """按优先级获取任务"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_ids = []
        try:
            # 创建多个仓库和任务，优先级不同
            priorities = [100, 50, 200]  # 50 应该最先被获取
            for i, priority in enumerate(priorities):
                with conn.cursor() as cur:
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                        VALUES ('git', 'https://example.com/test_priority_{i}.git')
                        RETURNING repo_id
                    """)
                    repo_id = cur.fetchone()[0]
                    repo_ids.append(repo_id)
                    
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.sync_jobs (
                            repo_id, job_type, mode, priority, status
                        )
                        VALUES (%s, 'gitlab_commits', 'incremental', %s, 'pending')
                    """, (repo_id, priority))

            with patch('engram_step1.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram_step1.scm_sync_queue import claim

                # claim 应该获取优先级最高（数值最小）的任务
                job = claim(worker_id="worker-1")
                
                assert job is not None
                assert job["priority"] == 50

        finally:
            for repo_id in repo_ids:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_claim_respects_not_before(self, migrated_db):
        """不获取 not_before 未到的任务"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_not_before.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # 创建一个 not_before 在未来的任务
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status, not_before
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'pending', 
                            now() + interval '1 hour')
                """, (repo_id,))

            with patch('engram_step1.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram_step1.scm_sync_queue import claim

                # 应该没有可用任务
                job = claim(worker_id="worker-1")
                assert job is None

        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_claim_expired_running_job(self, migrated_db):
        """回收锁过期的 running 任务"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_expired.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # 创建一个锁已过期的 running 任务
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 1)
                """, (repo_id,))

            with patch('engram_step1.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram_step1.scm_sync_queue import claim, get_job

                # 应该能获取这个过期任务
                job = claim(worker_id="worker-2")
                
                assert job is not None
                assert job["attempts"] == 2  # 从 1 增加到 2

                # 验证锁持有者已更新
                job_info = get_job(job["job_id"])
                assert job_info["locked_by"] == "worker-2"

        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_claim_concurrent_only_one_wins(self, migrated_db):
        """并发 claim：只有一个 worker 成功"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_concurrent.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'pending')
                """, (repo_id,))

            results = []

            def try_claim(worker_id):
                try:
                    test_conn = psycopg.connect(dsn, autocommit=False)
                    with test_conn.cursor() as cur:
                        cur.execute(f"SET search_path TO {scm_schema}")
                    
                    with patch('engram_step1.scm_sync_queue.get_connection', return_value=test_conn):
                        from engram_step1.scm_sync_queue import claim
                        job = claim(worker_id=worker_id)
                        return (worker_id, job is not None)
                except Exception as e:
                    return (worker_id, f"error: {e}")

            # 并发 claim
            num_workers = 5
            with ThreadPoolExecutor(max_workers=num_workers) as executor:
                futures = [executor.submit(try_claim, f"worker-{i}") for i in range(num_workers)]
                for future in as_completed(futures):
                    results.append(future.result())

            # 只有一个成功
            successful = [r for r in results if r[1] is True]
            failed = [r for r in results if r[1] is False]
            
            assert len(successful) == 1
            assert len(failed) == num_workers - 1

        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_claim_with_job_type_filter(self, migrated_db):
        """按任务类型过滤 claim"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_ids = []
        try:
            # 创建不同类型的任务
            for job_type in ["gitlab_commits", "gitlab_mrs", "svn"]:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                        VALUES ('git', 'https://example.com/test_filter_{job_type}.git')
                        RETURNING repo_id
                    """)
                    repo_id = cur.fetchone()[0]
                    repo_ids.append(repo_id)
                    
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.sync_jobs (
                            repo_id, job_type, mode, priority, status
                        )
                        VALUES (%s, %s, 'incremental', 100, 'pending')
                    """, (repo_id, job_type))

            with patch('engram_step1.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram_step1.scm_sync_queue import claim

                # 只 claim gitlab 相关任务
                job = claim(worker_id="worker-1", job_types=["gitlab_commits", "gitlab_mrs"])
                
                assert job is not None
                assert job["job_type"] in ["gitlab_commits", "gitlab_mrs"]

        finally:
            for repo_id in repo_ids:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestAck:
    """ack 函数测试"""

    def test_ack_success(self, migrated_db):
        """成功确认任务完成"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_ack.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, status, locked_by, locked_at
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 'running', 
                            'worker-1', now())
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            with patch('engram_step1.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram_step1.scm_sync_queue import ack, get_job

                result = ack(job_id, "worker-1", run_id="run-123")
                assert result is True

                job = get_job(job_id)
                assert job["status"] == "completed"
                assert job["locked_by"] is None
                assert job["last_run_id"] == "run-123"

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_ack_wrong_worker_fails(self, migrated_db):
        """错误 worker 无法 ack"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_ack_wrong.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, status, locked_by, locked_at
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 'running', 
                            'worker-1', now())
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            with patch('engram_step1.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram_step1.scm_sync_queue import ack

                # worker-2 尝试 ack worker-1 的任务
                result = ack(job_id, "worker-2")
                assert result is False

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestFailRetry:
    """fail_retry 函数测试"""

    def test_fail_retry_sets_failed_status(self, migrated_db):
        """失败后设置 failed 状态"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_fail.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, status, locked_by, locked_at,
                        attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 'running', 
                            'worker-1', now(), 1, 3)
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            with patch('engram_step1.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram_step1.scm_sync_queue import fail_retry, get_job

                result = fail_retry(job_id, "worker-1", "Connection timeout")
                assert result is True

                job = get_job(job_id)
                assert job["status"] == "failed"
                assert job["locked_by"] is None
                assert job["last_error"] == "Connection timeout"

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_fail_retry_marks_dead_when_max_attempts(self, migrated_db):
        """达到最大尝试次数时标记为 dead"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_dead.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # 已尝试 3 次，max_attempts = 3
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, status, locked_by, locked_at,
                        attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 'running', 
                            'worker-1', now(), 3, 3)
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            with patch('engram_step1.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram_step1.scm_sync_queue import fail_retry, get_job

                result = fail_retry(job_id, "worker-1", "Final failure")
                assert result is True

                job = get_job(job_id)
                assert job["status"] == "dead"
                assert job["last_error"] == "Final failure"

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestMarkDead:
    """mark_dead 函数测试"""

    def test_mark_dead_success(self, migrated_db):
        """成功标记为死信"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_mark_dead.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, status, locked_by, locked_at
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 'running', 
                            'worker-1', now())
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            with patch('engram_step1.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram_step1.scm_sync_queue import mark_dead, get_job

                result = mark_dead(job_id, "worker-1", "Unrecoverable error")
                assert result is True

                job = get_job(job_id)
                assert job["status"] == "dead"
                assert job["last_error"] == "Unrecoverable error"

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestRenewLease:
    """renew_lease 函数测试"""

    def test_renew_lease_success(self, migrated_db):
        """成功续租"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_renew.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, status, locked_by, locked_at,
                        lease_seconds
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 'running', 
                            'worker-1', now() - interval '2 minutes', 300)
                    RETURNING job_id, locked_at
                """, (repo_id,))
                row = cur.fetchone()
                job_id = str(row[0])
                old_locked_at = row[1]

            with patch('engram_step1.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram_step1.scm_sync_queue import renew_lease, get_job

                result = renew_lease(job_id, "worker-1")
                assert result is True

                job = get_job(job_id)
                assert job["locked_at"] > old_locked_at

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_renew_lease_wrong_worker_fails(self, migrated_db):
        """错误 worker 无法续租"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_renew_wrong.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, status, locked_by, locked_at
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 'running', 
                            'worker-1', now())
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            with patch('engram_step1.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram_step1.scm_sync_queue import renew_lease

                result = renew_lease(job_id, "worker-2")
                assert result is False

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestListAndCount:
    """列表和统计函数测试"""

    def test_list_jobs_by_status(self, migrated_db):
        """按状态列出任务"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_ids = []
        try:
            # 创建不同状态的任务
            for status in ["pending", "pending", "running", "completed"]:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                        VALUES ('git', 'https://example.com/test_list_{status}_{len(repo_ids)}.git')
                        RETURNING repo_id
                    """)
                    repo_id = cur.fetchone()[0]
                    repo_ids.append(repo_id)
                    
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.sync_jobs (
                            repo_id, job_type, mode, status
                        )
                        VALUES (%s, 'gitlab_commits', 'incremental', %s)
                    """, (repo_id, status))

            with patch('engram_step1.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram_step1.scm_sync_queue import list_jobs_by_status

                pending_jobs = list_jobs_by_status("pending")
                assert len(pending_jobs) >= 2
                
                for job in pending_jobs:
                    assert job["status"] == "pending"

        finally:
            for repo_id in repo_ids:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_count_jobs_by_status(self, migrated_db):
        """统计各状态任务数量"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_ids = []
        try:
            for status in ["pending", "pending", "running"]:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                        VALUES ('git', 'https://example.com/test_count_{len(repo_ids)}.git')
                        RETURNING repo_id
                    """)
                    repo_id = cur.fetchone()[0]
                    repo_ids.append(repo_id)
                    
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.sync_jobs (
                            repo_id, job_type, mode, status
                        )
                        VALUES (%s, 'gitlab_commits', 'incremental', %s)
                    """, (repo_id, status))

            with patch('engram_step1.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram_step1.scm_sync_queue import count_jobs_by_status

                counts = count_jobs_by_status()
                assert counts.get("pending", 0) >= 2
                assert counts.get("running", 0) >= 1

        finally:
            for repo_id in repo_ids:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestCleanupAndReset:
    """清理和重置函数测试"""

    def test_cleanup_completed_jobs(self, migrated_db):
        """清理已完成的旧任务"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_cleanup.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # 创建一个旧的已完成任务
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, status, updated_at
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 'completed', 
                            now() - interval '10 days')
                """, (repo_id,))

            with patch('engram_step1.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram_step1.scm_sync_queue import cleanup_completed_jobs

                deleted = cleanup_completed_jobs(older_than_days=7)
                assert deleted >= 1

        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_reset_dead_jobs(self, migrated_db):
        """重置死信任务"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_reset.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, status, attempts, last_error
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 'dead', 3, 'Some error')
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            with patch('engram_step1.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram_step1.scm_sync_queue import reset_dead_jobs, get_job

                reset = reset_dead_jobs(repo_id=repo_id)
                assert reset >= 1

                job = get_job(job_id)
                assert job["status"] == "pending"
                assert job["attempts"] == 0
                assert job["last_error"] is None

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestRequeueWithoutPenalty:
    """requeue_without_penalty 函数测试"""

    def test_requeue_without_penalty_success(self, migrated_db):
        """成功重新入队，attempts 不增加"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_requeue.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # 创建一个 running 状态的任务，attempts = 1
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, status, locked_by, locked_at,
                        attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 'running', 
                            'worker-1', now(), 1, 3)
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            with patch('engram_step1.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram_step1.scm_sync_queue import requeue_without_penalty, get_job

                result = requeue_without_penalty(
                    job_id, "worker-1", 
                    reason="resource_locked",
                    jitter_seconds=1
                )
                assert result is True

                job = get_job(job_id)
                assert job["status"] == "pending"
                assert job["locked_by"] is None
                # attempts 应该回退到 0（从 1 减 1）
                assert job["attempts"] == 0
                assert "resource_locked" in (job["last_error"] or "")

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_requeue_wrong_worker_fails(self, migrated_db):
        """错误 worker 无法 requeue"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_requeue_wrong.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, status, locked_by, locked_at,
                        attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 'running', 
                            'worker-1', now(), 1, 3)
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            with patch('engram_step1.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram_step1.scm_sync_queue import requeue_without_penalty

                # worker-2 尝试 requeue worker-1 的任务
                result = requeue_without_penalty(job_id, "worker-2", reason="test")
                assert result is False

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_locked_skipped_not_push_to_dead(self, migrated_db):
        """locked/skipped 场景多次 requeue 不会推向 dead"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_locked_skipped.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

            with patch('engram_step1.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram_step1.scm_sync_queue import (
                    enqueue, claim, requeue_without_penalty, get_job,
                    STATUS_PENDING, STATUS_DEAD,
                )

                # 入队，max_attempts = 2（很小的值，容易触发 dead）
                job_id = enqueue(
                    repo_id=repo_id,
                    job_type="gitlab_commits",
                    max_attempts=2,
                )
                assert job_id is not None

                # 模拟多次 locked/skipped 场景
                for i in range(5):  # 超过 max_attempts 的次数
                    # claim 任务
                    job = claim(worker_id=f"worker-{i}")
                    if job is None:
                        # 需要等待 not_before，直接更新
                        with mock_conn.cursor() as cur:
                            cur.execute(f"""
                                UPDATE {scm_schema}.sync_jobs 
                                SET not_before = now() - interval '1 second'
                                WHERE job_id = %s
                            """, (job_id,))
                            mock_conn.commit()
                        job = claim(worker_id=f"worker-{i}")
                    
                    assert job is not None, f"第 {i+1} 次 claim 失败"
                    
                    # 使用 requeue_without_penalty（模拟 locked/skipped）
                    result = requeue_without_penalty(
                        job_id, f"worker-{i}",
                        reason=f"locked_skipped_{i}",
                        jitter_seconds=0  # 立即可调度
                    )
                    assert result is True, f"第 {i+1} 次 requeue 失败"

                    # 验证没有变成 dead
                    job_info = get_job(job_id)
                    assert job_info["status"] != STATUS_DEAD, f"第 {i+1} 次后任务不应该是 dead 状态"
                    assert job_info["status"] == STATUS_PENDING, f"第 {i+1} 次后任务应该是 pending 状态"

                # 最终状态验证
                final_job = get_job(job_id)
                assert final_job["status"] == STATUS_PENDING
                # attempts 应该保持为 0（每次 +1 后又 -1）
                assert final_job["attempts"] == 0

        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestRequeueAttemptsSemantics:
    """requeue/attempts 语义测试
    
    验证:
    - claim 增加 attempts
    - requeue_without_penalty 回退 attempts
    - fail_retry 不改变 attempts（在 fail_retry 前已经被 claim 增加）
    - ack 不改变 attempts
    """

    def test_claim_increments_attempts(self, migrated_db):
        """claim 时 attempts 增加 1"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_claim_inc.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status, attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'pending', 0)
                """, (repo_id,))

            with patch('engram_step1.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram_step1.scm_sync_queue import claim, get_job

                # claim 前 attempts = 0
                job = claim(worker_id="worker-1")
                assert job is not None
                
                # claim 后 attempts = 1
                assert job["attempts"] == 1, "claim 应该增加 attempts"

        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_requeue_decrements_attempts(self, migrated_db):
        """requeue_without_penalty 时 attempts 减少 1（补偿 claim 的增加）"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_requeue_dec.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # 创建一个 running 任务，attempts = 2
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status, 
                        locked_by, locked_at, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running', 
                            'worker-1', now(), 2, 5)
                """, (repo_id,))

            with patch('engram_step1.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram_step1.scm_sync_queue import requeue_without_penalty, get_job

                # 获取 job_id
                with mock_conn.cursor() as cur:
                    cur.execute(f"SELECT job_id FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                    job_id = str(cur.fetchone()[0])

                # requeue 前 attempts = 2
                result = requeue_without_penalty(job_id, "worker-1", reason="test", jitter_seconds=0)
                assert result is True

                # requeue 后 attempts = 1
                job = get_job(job_id)
                assert job["attempts"] == 1, "requeue_without_penalty 应该减少 attempts"
                assert job["status"] == "pending"

        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_requeue_attempts_floor_at_zero(self, migrated_db):
        """requeue_without_penalty 时 attempts 最小为 0（不会变负）"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_requeue_floor.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # 创建一个 running 任务，attempts = 0（边界情况）
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status, 
                        locked_by, locked_at, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running', 
                            'worker-1', now(), 0, 5)
                """, (repo_id,))

            with patch('engram_step1.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram_step1.scm_sync_queue import requeue_without_penalty, get_job

                with mock_conn.cursor() as cur:
                    cur.execute(f"SELECT job_id FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                    job_id = str(cur.fetchone()[0])

                result = requeue_without_penalty(job_id, "worker-1", reason="test", jitter_seconds=0)
                assert result is True

                job = get_job(job_id)
                assert job["attempts"] == 0, "attempts 应该最小为 0，不应变为负数"

        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_claim_requeue_cycle_maintains_attempts(self, migrated_db):
        """claim + requeue 循环应保持 attempts 不变"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_claim_requeue_cycle.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'pending', 0, 5)
                """, (repo_id,))

            with patch('engram_step1.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram_step1.scm_sync_queue import claim, requeue_without_penalty, get_job

                # 执行 3 次 claim + requeue 循环
                for i in range(3):
                    job = claim(worker_id=f"worker-{i}")
                    assert job is not None, f"第 {i+1} 次 claim 失败"
                    job_id = job["job_id"]
                    
                    # claim 后 attempts 应为 1
                    assert job["attempts"] == 1, f"第 {i+1} 次 claim 后 attempts 应为 1"
                    
                    # requeue
                    result = requeue_without_penalty(job_id, f"worker-{i}", reason=f"test_{i}", jitter_seconds=0)
                    assert result is True, f"第 {i+1} 次 requeue 失败"
                    
                    # requeue 后 attempts 应为 0
                    job_info = get_job(job_id)
                    assert job_info["attempts"] == 0, f"第 {i+1} 次 requeue 后 attempts 应为 0"

        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_ack_does_not_change_attempts(self, migrated_db):
        """ack 不改变 attempts"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_ack_attempts.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # 创建一个 running 任务，attempts = 3
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status, 
                        locked_by, locked_at, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running', 
                            'worker-1', now(), 3, 5)
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            with patch('engram_step1.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram_step1.scm_sync_queue import ack, get_job

                result = ack(job_id, "worker-1", run_id="run-123")
                assert result is True

                job = get_job(job_id)
                assert job["attempts"] == 3, "ack 不应改变 attempts"
                assert job["status"] == "completed"

        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_fail_retry_does_not_change_attempts(self, migrated_db):
        """fail_retry 不改变 attempts（claim 时已增加）"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_fail_attempts.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # 创建一个 running 任务，attempts = 2
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status, 
                        locked_by, locked_at, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running', 
                            'worker-1', now(), 2, 5)
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            with patch('engram_step1.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram_step1.scm_sync_queue import fail_retry, get_job

                result = fail_retry(job_id, "worker-1", "Test error", backoff_seconds=0)
                assert result is True

                job = get_job(job_id)
                assert job["attempts"] == 2, "fail_retry 不应改变 attempts"
                assert job["status"] == "failed"

        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestIntegration:
    """集成测试：完整的 claim -> execute -> ack/fail 流程"""

    def test_full_job_lifecycle(self, migrated_db):
        """完整的任务生命周期"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_lifecycle.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

            with patch('engram_step1.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram_step1.scm_sync_queue import (
                    enqueue, claim, ack, get_job,
                    STATUS_PENDING, STATUS_RUNNING, STATUS_COMPLETED,
                )

                # 1. 入队
                job_id = enqueue(
                    repo_id=repo_id,
                    job_type="gitlab_commits",
                    payload={"since": "2024-01-01"},
                )
                assert job_id is not None

                job = get_job(job_id)
                assert job["status"] == STATUS_PENDING

                # 2. Claim
                claimed = claim(worker_id="worker-1")
                assert claimed is not None
                assert claimed["job_id"] == job_id

                job = get_job(job_id)
                assert job["status"] == STATUS_RUNNING
                assert job["locked_by"] == "worker-1"

                # 3. Ack
                result = ack(job_id, "worker-1", run_id="run-abc")
                assert result is True

                job = get_job(job_id)
                assert job["status"] == STATUS_COMPLETED
                assert job["locked_by"] is None
                assert job["last_run_id"] == "run-abc"

        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_retry_then_dead_lifecycle(self, migrated_db):
        """重试直到死信的生命周期"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_retry_dead.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

            with patch('engram_step1.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram_step1.scm_sync_queue import (
                    enqueue, claim, fail_retry, get_job,
                    STATUS_FAILED, STATUS_DEAD,
                )

                # 入队，max_attempts = 2
                job_id = enqueue(
                    repo_id=repo_id,
                    job_type="gitlab_commits",
                    max_attempts=2,
                )

                # 第一次尝试失败
                job = claim(worker_id="worker-1")
                fail_retry(job_id, "worker-1", "Error 1", backoff_seconds=0)

                job = get_job(job_id)
                assert job["status"] == STATUS_FAILED
                assert job["attempts"] == 1

                # 第二次尝试失败 -> 应该变成 dead
                job = claim(worker_id="worker-1")
                fail_retry(job_id, "worker-1", "Error 2", backoff_seconds=0)

                job = get_job(job_id)
                assert job["status"] == STATUS_DEAD
                assert job["attempts"] == 2

        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()
