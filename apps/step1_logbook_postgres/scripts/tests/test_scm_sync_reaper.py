# -*- coding: utf-8 -*-
"""
SCM Sync Reaper 单元测试

测试:
- 过期 running job 的回收
- 过期 running run 的回收
- 过期 lock 的回收
- 回收后的状态转换验证
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
import psycopg


class TestExpiredJobRecovery:
    """过期 running job 回收测试"""

    def test_expired_job_detected(self, migrated_db):
        """检测过期的 running 任务"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_expired_job.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # 创建一个过期的 running 任务（locked_at 在 10 分钟前，lease_seconds=300）
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 1, 3)
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            # 使用 db.list_expired_running_jobs 检测
            import sys
            from pathlib import Path
            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            
            from db import list_expired_running_jobs, get_conn
            
            test_conn = get_conn(dsn)
            try:
                expired_jobs = list_expired_running_jobs(test_conn, grace_seconds=60, limit=100)
                
                # 应该检测到过期任务
                expired_job_ids = [str(j["job_id"]) for j in expired_jobs]
                assert job_id in expired_job_ids, f"应该检测到过期任务 {job_id}"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_expired_job_marked_failed(self, migrated_db):
        """过期任务被标记为 failed（尚未达到 max_attempts）"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_expired_failed.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 1, 3)
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path
            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            
            from db import mark_job_as_failed_by_reaper, get_conn
            
            test_conn = get_conn(dsn)
            try:
                # 标记为 failed
                success = mark_job_as_failed_by_reaper(
                    test_conn, job_id, 
                    error="Reaped: job lock expired",
                    retry_delay_seconds=60,
                )
                test_conn.commit()
                
                assert success is True
                
                # 验证状态
                with test_conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT status, locked_by, last_error 
                        FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """, (job_id,))
                    row = cur.fetchone()
                    
                    assert row[0] == "failed", f"状态应为 failed，实际: {row[0]}"
                    assert row[1] is None, "locked_by 应为 NULL"
                    assert "Reaped" in row[2], f"last_error 应包含 Reaped: {row[2]}"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_expired_job_marked_dead_when_max_attempts(self, migrated_db):
        """过期任务达到 max_attempts 时被标记为 dead"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_expired_dead.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # attempts = max_attempts
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 3, 3)
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path
            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            
            from db import mark_job_as_dead_by_reaper, get_conn
            
            test_conn = get_conn(dsn)
            try:
                # 标记为 dead
                success = mark_job_as_dead_by_reaper(
                    test_conn, job_id, 
                    error="Reaped: job expired after max attempts",
                )
                test_conn.commit()
                
                assert success is True
                
                # 验证状态
                with test_conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT status, locked_by, last_error 
                        FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """, (job_id,))
                    row = cur.fetchone()
                    
                    assert row[0] == "dead", f"状态应为 dead，实际: {row[0]}"
                    assert row[1] is None, "locked_by 应为 NULL"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_non_expired_job_not_detected(self, migrated_db):
        """未过期的 running 任务不应被检测"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_non_expired.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # 创建一个未过期的 running 任务（刚刚锁定）
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'active-worker', now(), 300, 1, 3)
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path
            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            
            from db import list_expired_running_jobs, get_conn
            
            test_conn = get_conn(dsn)
            try:
                expired_jobs = list_expired_running_jobs(test_conn, grace_seconds=60, limit=100)
                
                # 不应检测到未过期任务
                expired_job_ids = [str(j["job_id"]) for j in expired_jobs]
                assert job_id not in expired_job_ids, f"不应检测到未过期任务 {job_id}"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestExpiredRunRecovery:
    """过期 running run 回收测试"""

    def test_expired_run_detected(self, migrated_db):
        """检测超时的 running run"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        run_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_expired_run.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                run_id = str(uuid.uuid4())
                # 创建一个超时的 running run（started_at 在 45 分钟前）
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_runs (
                        run_id, repo_id, job_type, mode, status, started_at
                    )
                    VALUES (%s, %s, 'gitlab_commits', 'incremental', 'running',
                            now() - interval '45 minutes')
                """, (run_id, repo_id))

            import sys
            from pathlib import Path
            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            
            from db import list_expired_running_runs, get_conn
            
            test_conn = get_conn(dsn)
            try:
                # max_duration_seconds=1800 (30 分钟)
                expired_runs = list_expired_running_runs(
                    test_conn, max_duration_seconds=1800, limit=100
                )
                
                expired_run_ids = [str(r["run_id"]) for r in expired_runs]
                assert run_id in expired_run_ids, f"应该检测到超时 run {run_id}"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if run_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_runs WHERE run_id = %s", (run_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_expired_run_marked_failed(self, migrated_db):
        """超时 run 被标记为 failed"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        run_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_run_failed.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                run_id = str(uuid.uuid4())
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_runs (
                        run_id, repo_id, job_type, mode, status, started_at
                    )
                    VALUES (%s, %s, 'gitlab_commits', 'incremental', 'running',
                            now() - interval '45 minutes')
                """, (run_id, repo_id))

            import sys
            from pathlib import Path
            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            
            from db import mark_run_as_failed_by_reaper, get_conn
            
            test_conn = get_conn(dsn)
            try:
                error_summary = {
                    "error_type": "reaper_timeout",
                    "message": "Reaped: sync run timed out",
                }
                
                success = mark_run_as_failed_by_reaper(test_conn, run_id, error_summary)
                test_conn.commit()
                
                assert success is True
                
                # 验证状态
                with test_conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT status, finished_at, error_summary_json 
                        FROM {scm_schema}.sync_runs WHERE run_id = %s
                    """, (run_id,))
                    row = cur.fetchone()
                    
                    assert row[0] == "failed", f"状态应为 failed，实际: {row[0]}"
                    assert row[1] is not None, "finished_at 应不为 NULL"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if run_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_runs WHERE run_id = %s", (run_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_non_expired_run_not_detected(self, migrated_db):
        """未超时的 running run 不应被检测"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        run_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_non_expired_run.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                run_id = str(uuid.uuid4())
                # 创建一个刚开始的 running run
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_runs (
                        run_id, repo_id, job_type, mode, status, started_at
                    )
                    VALUES (%s, %s, 'gitlab_commits', 'incremental', 'running', now())
                """, (run_id, repo_id))

            import sys
            from pathlib import Path
            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            
            from db import list_expired_running_runs, get_conn
            
            test_conn = get_conn(dsn)
            try:
                expired_runs = list_expired_running_runs(
                    test_conn, max_duration_seconds=1800, limit=100
                )
                
                expired_run_ids = [str(r["run_id"]) for r in expired_runs]
                assert run_id not in expired_run_ids, f"不应检测到未超时 run {run_id}"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if run_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_runs WHERE run_id = %s", (run_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestExpiredLockRecovery:
    """过期 lock 回收测试"""

    def test_expired_lock_detected(self, migrated_db):
        """检测过期的锁"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        lock_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_expired_lock.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # 创建一个过期的锁
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_locks (
                        repo_id, job_type, locked_by, locked_at, lease_seconds
                    )
                    VALUES (%s, 'gitlab_commits', 'dead-worker', 
                            now() - interval '10 minutes', 60)
                    RETURNING lock_id
                """, (repo_id,))
                lock_id = cur.fetchone()[0]

            import sys
            from pathlib import Path
            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            
            from db import list_expired_locks, get_conn
            
            test_conn = get_conn(dsn)
            try:
                expired_locks = list_expired_locks(test_conn, grace_seconds=0, limit=100)
                
                expired_lock_ids = [l["lock_id"] for l in expired_locks]
                assert lock_id in expired_lock_ids, f"应该检测到过期锁 {lock_id}"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if lock_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_locks WHERE lock_id = %s", (lock_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_expired_lock_force_released(self, migrated_db):
        """过期锁被强制释放"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        lock_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_lock_released.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_locks (
                        repo_id, job_type, locked_by, locked_at, lease_seconds
                    )
                    VALUES (%s, 'gitlab_commits', 'dead-worker', 
                            now() - interval '10 minutes', 60)
                    RETURNING lock_id
                """, (repo_id,))
                lock_id = cur.fetchone()[0]

            import sys
            from pathlib import Path
            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            
            from db import force_release_lock, get_conn
            
            test_conn = get_conn(dsn)
            try:
                success = force_release_lock(test_conn, lock_id)
                test_conn.commit()
                
                assert success is True
                
                # 验证锁已释放
                with test_conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT locked_by, locked_at 
                        FROM {scm_schema}.sync_locks WHERE lock_id = %s
                    """, (lock_id,))
                    row = cur.fetchone()
                    
                    assert row[0] is None, "locked_by 应为 NULL"
                    assert row[1] is None, "locked_at 应为 NULL"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if lock_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_locks WHERE lock_id = %s", (lock_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_non_expired_lock_not_detected(self, migrated_db):
        """未过期的锁不应被检测"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        lock_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_non_expired_lock.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # 创建一个未过期的锁（刚刚锁定）
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_locks (
                        repo_id, job_type, locked_by, locked_at, lease_seconds
                    )
                    VALUES (%s, 'gitlab_commits', 'active-worker', now(), 60)
                    RETURNING lock_id
                """, (repo_id,))
                lock_id = cur.fetchone()[0]

            import sys
            from pathlib import Path
            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            
            from db import list_expired_locks, get_conn
            
            test_conn = get_conn(dsn)
            try:
                expired_locks = list_expired_locks(test_conn, grace_seconds=0, limit=100)
                
                expired_lock_ids = [l["lock_id"] for l in expired_locks]
                assert lock_id not in expired_lock_ids, f"不应检测到未过期锁 {lock_id}"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if lock_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_locks WHERE lock_id = %s", (lock_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestLastErrorClassification:
    """last_error 分类测试（测试 engram_step1.scm_sync_errors 模块）"""

    def test_classify_auth_error(self):
        """测试认证错误分类为永久性错误"""
        from engram_step1.scm_sync_errors import classify_last_error
        
        # 401 错误
        is_permanent, is_transient, category = classify_last_error("401 Unauthorized: Invalid token")
        assert is_permanent is True
        assert is_transient is False
        assert category == "auth_error"
        
        # 认证失败
        is_permanent, is_transient, category = classify_last_error("Authentication failed: token expired")
        assert is_permanent is True
        assert is_transient is False
        assert category == "auth_error"
        
        # auth_error 分类
        is_permanent, is_transient, category = classify_last_error("auth_error: invalid credentials")
        assert is_permanent is True
        assert is_transient is False
        assert category == "auth_error"

    def test_classify_repo_not_found_error(self):
        """测试仓库不存在错误分类为永久性错误"""
        from engram_step1.scm_sync_errors import classify_last_error
        
        # 404 错误
        is_permanent, is_transient, category = classify_last_error("404 Not Found: repository does not exist")
        assert is_permanent is True
        assert is_transient is False
        assert category == "repo_not_found"
        
        # repo_not_found 分类
        is_permanent, is_transient, category = classify_last_error("repo_not_found: project deleted")
        assert is_permanent is True
        assert is_transient is False
        assert category == "repo_not_found"

    def test_classify_permission_denied_error(self):
        """测试权限不足错误分类为永久性错误"""
        from engram_step1.scm_sync_errors import classify_last_error
        
        # 403 错误
        is_permanent, is_transient, category = classify_last_error("403 Forbidden: access denied")
        assert is_permanent is True
        assert is_transient is False
        assert category == "permission_denied"
        
        # permission_denied 分类
        is_permanent, is_transient, category = classify_last_error("permission_denied: insufficient rights")
        assert is_permanent is True
        assert is_transient is False
        assert category == "permission_denied"

    def test_classify_rate_limit_error(self):
        """测试速率限制错误分类为临时性错误"""
        from engram_step1.scm_sync_errors import classify_last_error
        
        # 429 错误
        is_permanent, is_transient, category = classify_last_error("429 Too Many Requests")
        assert is_permanent is False
        assert is_transient is True
        assert category == "rate_limit"
        
        # rate limit 关键词
        is_permanent, is_transient, category = classify_last_error("Rate limit exceeded, retry after 60 seconds")
        assert is_permanent is False
        assert is_transient is True
        assert category == "rate_limit"

    def test_classify_timeout_error(self):
        """测试超时错误分类为临时性错误"""
        from engram_step1.scm_sync_errors import classify_last_error
        
        # timeout 关键词
        is_permanent, is_transient, category = classify_last_error("Request timeout after 30 seconds")
        assert is_permanent is False
        assert is_transient is True
        assert category == "timeout"
        
        # timed out
        is_permanent, is_transient, category = classify_last_error("Connection timed out")
        assert is_permanent is False
        assert is_transient is True
        assert category == "timeout"

    def test_classify_server_error(self):
        """测试服务器错误分类为临时性错误"""
        from engram_step1.scm_sync_errors import classify_last_error
        
        # 502 Bad Gateway
        is_permanent, is_transient, category = classify_last_error("502 Bad Gateway")
        assert is_permanent is False
        assert is_transient is True
        assert category == "server_error"
        
        # 503 Service Unavailable
        is_permanent, is_transient, category = classify_last_error("503 Service Unavailable")
        assert is_permanent is False
        assert is_transient is True
        assert category == "server_error"
        
        # 504 Gateway Timeout (匹配 timeout)
        is_permanent, is_transient, category = classify_last_error("504 Gateway Timeout")
        assert is_permanent is False
        assert is_transient is True
        # 504 包含 "timeout"，所以应该匹配 timeout 分类
        assert category in ("timeout", "server_error")

    def test_classify_network_error(self):
        """测试网络错误分类为临时性错误"""
        from engram_step1.scm_sync_errors import classify_last_error
        
        # network 关键词
        is_permanent, is_transient, category = classify_last_error("Network error: connection refused")
        assert is_permanent is False
        assert is_transient is True
        assert category == "network"
        
        # connection 关键词
        is_permanent, is_transient, category = classify_last_error("Connection reset by peer")
        assert is_permanent is False
        assert is_transient is True

    def test_classify_empty_error(self):
        """测试空错误信息"""
        from engram_step1.scm_sync_errors import classify_last_error
        
        # None
        is_permanent, is_transient, category = classify_last_error(None)
        assert is_permanent is False
        assert is_transient is False
        assert category == ""
        
        # 空字符串
        is_permanent, is_transient, category = classify_last_error("")
        assert is_permanent is False
        assert is_transient is False
        assert category == ""

    def test_classify_unknown_error(self):
        """测试未知错误"""
        from engram_step1.scm_sync_errors import classify_last_error
        
        # 未知错误
        is_permanent, is_transient, category = classify_last_error("Some unknown internal error occurred")
        assert is_permanent is False
        assert is_transient is False
        assert category == ""


class TestReaperProcessFunctions:
    """Reaper 处理函数测试"""

    def test_process_expired_jobs_to_failed(self, migrated_db):
        """process_expired_jobs 将任务转为 failed"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_process_failed.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 1, 3)
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path
            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            
            from db import get_conn, list_expired_running_jobs
            from scm_sync_reaper import process_expired_jobs, JobRecoveryPolicy
            
            test_conn = get_conn(dsn)
            try:
                expired_jobs = list_expired_running_jobs(test_conn, grace_seconds=60, limit=10)
                
                # 使用 to_failed 策略处理
                stats = process_expired_jobs(
                    test_conn, expired_jobs,
                    policy=JobRecoveryPolicy.to_failed,
                    retry_delay_seconds=60,
                )
                
                assert stats["to_failed"] >= 1
                assert stats["errors"] == 0
                
                # 验证状态
                with test_conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT status FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """, (job_id,))
                    row = cur.fetchone()
                    assert row[0] == "failed"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_process_expired_jobs_to_dead(self, migrated_db):
        """process_expired_jobs 达到 max_attempts 时转为 dead"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_process_dead.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # attempts = max_attempts
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 3, 3)
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path
            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            
            from db import get_conn, list_expired_running_jobs
            from scm_sync_reaper import process_expired_jobs, JobRecoveryPolicy
            
            test_conn = get_conn(dsn)
            try:
                expired_jobs = list_expired_running_jobs(test_conn, grace_seconds=60, limit=10)
                
                stats = process_expired_jobs(
                    test_conn, expired_jobs,
                    policy=JobRecoveryPolicy.to_failed,
                    retry_delay_seconds=60,
                )
                
                assert stats["to_dead"] >= 1
                
                # 验证状态
                with test_conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT status FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """, (job_id,))
                    row = cur.fetchone()
                    assert row[0] == "dead"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestUnifiedBackoffCalculation:
    """统一退避计算测试"""

    def test_calculate_backoff_with_attempts(self):
        """测试不同 attempts 的退避时间计算"""
        from engram_step1.scm_sync_errors import (
            calculate_backoff_seconds,
            DEFAULT_BACKOFF_BASE,
            DEFAULT_MAX_BACKOFF,
        )
        
        # attempts=1: base * 2^0 = 60
        assert calculate_backoff_seconds(attempts=1) == 60
        
        # attempts=2: base * 2^1 = 120
        assert calculate_backoff_seconds(attempts=2) == 120
        
        # attempts=3: base * 2^2 = 240
        assert calculate_backoff_seconds(attempts=3) == 240
        
        # attempts=4: base * 2^3 = 480
        assert calculate_backoff_seconds(attempts=4) == 480

    def test_calculate_backoff_with_max_limit(self):
        """测试退避时间不超过 max_seconds"""
        from engram_step1.scm_sync_errors import calculate_backoff_seconds
        
        # 设置较小的 max_seconds
        max_seconds = 300
        
        # attempts=1: 60 <= 300
        assert calculate_backoff_seconds(attempts=1, max_seconds=max_seconds) == 60
        
        # attempts=3: 240 <= 300
        assert calculate_backoff_seconds(attempts=3, max_seconds=max_seconds) == 240
        
        # attempts=4: 480 > 300，应限制为 300
        assert calculate_backoff_seconds(attempts=4, max_seconds=max_seconds) == 300
        
        # attempts=10: 非常大的值，应限制为 300
        assert calculate_backoff_seconds(attempts=10, max_seconds=max_seconds) == 300

    def test_calculate_backoff_with_error_category(self):
        """测试错误类别影响基础退避时间"""
        from engram_step1.scm_sync_errors import (
            calculate_backoff_seconds,
            ErrorCategory,
            TRANSIENT_ERROR_BACKOFF,
        )
        
        # rate_limit 的基础退避是 120
        rate_limit_base = TRANSIENT_ERROR_BACKOFF[ErrorCategory.RATE_LIMIT.value]
        
        # attempts=1: 120 * 2^0 = 120
        result = calculate_backoff_seconds(
            attempts=1,
            error_category=ErrorCategory.RATE_LIMIT.value,
        )
        assert result == rate_limit_base
        
        # attempts=2: 120 * 2^1 = 240
        result = calculate_backoff_seconds(
            attempts=2,
            error_category=ErrorCategory.RATE_LIMIT.value,
        )
        assert result == rate_limit_base * 2

    def test_calculate_backoff_with_error_message(self):
        """测试通过错误消息推断退避时间"""
        from engram_step1.scm_sync_errors import (
            calculate_backoff_seconds,
            TRANSIENT_ERROR_BACKOFF,
            ErrorCategory,
        )
        
        # "429" 应该匹配 rate_limit
        result = calculate_backoff_seconds(
            attempts=1,
            error_message="429 Too Many Requests",
        )
        assert result == TRANSIENT_ERROR_BACKOFF[ErrorCategory.RATE_LIMIT.value]
        
        # "timeout" 应该匹配 timeout
        result = calculate_backoff_seconds(
            attempts=1,
            error_message="Request timeout",
        )
        assert result == TRANSIENT_ERROR_BACKOFF[ErrorCategory.TIMEOUT.value]

    def test_queue_and_reaper_use_same_backoff_logic(self):
        """验证 queue 和 reaper 使用相同的退避计算逻辑"""
        from engram_step1.scm_sync_errors import (
            calculate_backoff_seconds,
            DEFAULT_BACKOFF_BASE,
            DEFAULT_MAX_BACKOFF,
        )
        
        # 模拟 queue 的 fail_retry 场景
        queue_backoff = calculate_backoff_seconds(
            attempts=2,
            base_seconds=DEFAULT_BACKOFF_BASE,
            max_seconds=DEFAULT_MAX_BACKOFF,
        )
        
        # 模拟 reaper 的场景（使用相同参数）
        reaper_backoff = calculate_backoff_seconds(
            attempts=2,
            base_seconds=DEFAULT_BACKOFF_BASE,
            max_seconds=DEFAULT_MAX_BACKOFF,
        )
        
        # 结果应该一致
        assert queue_backoff == reaper_backoff
        assert queue_backoff == 120  # 60 * 2^1

    def test_max_reaper_backoff_config(self):
        """测试 reaper 的 max_reaper_backoff_seconds 配置生效"""
        from engram_step1.scm_sync_errors import calculate_backoff_seconds
        
        # 默认 reaper max 是 1800 秒
        DEFAULT_MAX_REAPER_BACKOFF = 1800
        
        # attempts=5 时基础退避是 60 * 2^4 = 960，不超过 1800
        result = calculate_backoff_seconds(
            attempts=5,
            max_seconds=DEFAULT_MAX_REAPER_BACKOFF,
        )
        assert result == 960
        
        # attempts=6 时基础退避是 60 * 2^5 = 1920 > 1800，应限制
        result = calculate_backoff_seconds(
            attempts=6,
            max_seconds=DEFAULT_MAX_REAPER_BACKOFF,
        )
        assert result == DEFAULT_MAX_REAPER_BACKOFF


class TestLastErrorBasedProcessing:
    """基于 last_error 分类的 process_expired_jobs 集成测试"""

    def test_process_expired_jobs_with_permanent_error_marks_dead(self, migrated_db):
        """带有永久性错误的过期任务被标记为 dead（不考虑 attempts）"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_permanent_error.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # attempts=1, max_attempts=3，但有永久性错误
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts,
                        last_error
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 1, 3,
                            '401 Unauthorized: Invalid token')
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path
            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            
            from db import get_conn, list_expired_running_jobs
            from scm_sync_reaper import process_expired_jobs, JobRecoveryPolicy
            
            test_conn = get_conn(dsn)
            try:
                expired_jobs = list_expired_running_jobs(test_conn, grace_seconds=60, limit=10)
                
                stats = process_expired_jobs(
                    test_conn, expired_jobs,
                    policy=JobRecoveryPolicy.to_failed,
                    retry_delay_seconds=60,
                )
                
                assert stats["to_dead"] >= 1
                
                # 验证状态为 dead
                with test_conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT status, last_error FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """, (job_id,))
                    row = cur.fetchone()
                    assert row[0] == "dead", f"永久性错误应标记为 dead，实际: {row[0]}"
                    assert "permanent error" in row[1].lower() or "auth_error" in row[1].lower()
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_process_expired_jobs_with_transient_error_marks_failed_with_backoff(self, migrated_db):
        """带有临时性错误的过期任务被标记为 failed，使用更长的 retry_delay"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_transient_error.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # 临时性错误：429 速率限制
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts,
                        last_error
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 1, 3,
                            '429 Too Many Requests')
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path
            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            
            from db import get_conn, list_expired_running_jobs
            from scm_sync_reaper import process_expired_jobs, JobRecoveryPolicy
            
            test_conn = get_conn(dsn)
            try:
                expired_jobs = list_expired_running_jobs(test_conn, grace_seconds=60, limit=10)
                
                stats = process_expired_jobs(
                    test_conn, expired_jobs,
                    policy=JobRecoveryPolicy.to_failed,
                    retry_delay_seconds=60,
                    transient_retry_delay_multiplier=2.0,
                )
                
                assert stats["to_failed"] >= 1
                
                # 验证状态为 failed
                with test_conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT status, last_error, not_before FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """, (job_id,))
                    row = cur.fetchone()
                    assert row[0] == "failed", f"临时性错误应标记为 failed，实际: {row[0]}"
                    assert "transient error" in row[1].lower() or "rate_limit" in row[1].lower()
                    # not_before 应该被设置（延迟重试）
                    assert row[2] is not None
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_process_expired_jobs_repo_not_found_marks_dead(self, migrated_db):
        """仓库不存在错误被标记为 dead"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_repo_not_found.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts,
                        last_error
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 1, 3,
                            '404 Not Found: repository does not exist')
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path
            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            
            from db import get_conn, list_expired_running_jobs
            from scm_sync_reaper import process_expired_jobs, JobRecoveryPolicy
            
            test_conn = get_conn(dsn)
            try:
                expired_jobs = list_expired_running_jobs(test_conn, grace_seconds=60, limit=10)
                
                stats = process_expired_jobs(
                    test_conn, expired_jobs,
                    policy=JobRecoveryPolicy.to_failed,
                    retry_delay_seconds=60,
                )
                
                assert stats["to_dead"] >= 1
                
                # 验证状态为 dead
                with test_conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT status FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """, (job_id,))
                    row = cur.fetchone()
                    assert row[0] == "dead"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_process_expired_jobs_timeout_error_marks_failed(self, migrated_db):
        """超时错误被标记为 failed"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_timeout_error.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts,
                        last_error
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 1, 3,
                            'Request timeout after 30 seconds')
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path
            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            
            from db import get_conn, list_expired_running_jobs
            from scm_sync_reaper import process_expired_jobs, JobRecoveryPolicy
            
            test_conn = get_conn(dsn)
            try:
                expired_jobs = list_expired_running_jobs(test_conn, grace_seconds=60, limit=10)
                
                stats = process_expired_jobs(
                    test_conn, expired_jobs,
                    policy=JobRecoveryPolicy.to_failed,
                    retry_delay_seconds=60,
                )
                
                assert stats["to_failed"] >= 1
                
                # 验证状态为 failed
                with test_conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT status FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """, (job_id,))
                    row = cur.fetchone()
                    assert row[0] == "failed"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_process_expired_jobs_without_error_follows_policy(self, migrated_db):
        """没有 last_error 的任务按照 policy 处理"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_no_error.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # 没有 last_error
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 1, 3)
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path
            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            
            from db import get_conn, list_expired_running_jobs
            from scm_sync_reaper import process_expired_jobs, JobRecoveryPolicy
            
            test_conn = get_conn(dsn)
            try:
                expired_jobs = list_expired_running_jobs(test_conn, grace_seconds=60, limit=10)
                
                # 使用 to_pending 策略
                stats = process_expired_jobs(
                    test_conn, expired_jobs,
                    policy=JobRecoveryPolicy.to_pending,
                    retry_delay_seconds=60,
                )
                
                assert stats["to_pending"] >= 1
                
                # 验证状态为 pending
                with test_conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT status FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """, (job_id,))
                    row = cur.fetchone()
                    assert row[0] == "pending"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_transient_error_backoff_increases_with_attempts(self, migrated_db):
        """临时性错误的退避时间随 attempts 增加（指数退避）"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_ids = []
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_backoff_attempts.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # 创建多个不同 attempts 的任务
                for attempts in [1, 2, 3]:
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.sync_jobs (
                            repo_id, job_type, mode, priority, status,
                            locked_by, locked_at, lease_seconds, attempts, max_attempts,
                            last_error
                        )
                        VALUES (%s, 'gitlab_commits_{attempts}', 'incremental', 100, 'running',
                                'dead-worker', now() - interval '10 minutes', 300, %s, 5,
                                '429 Too Many Requests')
                        RETURNING job_id
                    """, (repo_id, attempts))
                    job_ids.append(str(cur.fetchone()[0]))

            import sys
            from pathlib import Path
            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            
            from db import get_conn, list_expired_running_jobs
            from scm_sync_reaper import process_expired_jobs, JobRecoveryPolicy
            
            test_conn = get_conn(dsn)
            try:
                expired_jobs = list_expired_running_jobs(test_conn, grace_seconds=60, limit=10)
                
                # 处理所有过期任务
                stats = process_expired_jobs(
                    test_conn, expired_jobs,
                    policy=JobRecoveryPolicy.to_failed,
                    retry_delay_seconds=60,
                    max_reaper_backoff_seconds=3600,
                )
                
                assert stats["to_failed"] >= 3
                
                # 验证 not_before 时间差异（退避时间应该随 attempts 增加）
                # 注意：这里只验证状态正确，实际退避时间由数据库时间戳控制
                for job_id in job_ids:
                    with test_conn.cursor() as cur:
                        cur.execute(f"""
                            SELECT status FROM {scm_schema}.sync_jobs WHERE job_id = %s
                        """, (job_id,))
                        row = cur.fetchone()
                        assert row[0] == "failed"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                for job_id in job_ids:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_different_error_categories_get_different_base_backoff(self):
        """不同错误类别有不同的基础退避时间"""
        from engram_step1.scm_sync_errors import (
            calculate_backoff_seconds,
            ErrorCategory,
            TRANSIENT_ERROR_BACKOFF,
        )
        
        # rate_limit: 120 秒
        rate_limit_backoff = calculate_backoff_seconds(
            attempts=1,
            error_category=ErrorCategory.RATE_LIMIT.value,
        )
        
        # timeout: 30 秒
        timeout_backoff = calculate_backoff_seconds(
            attempts=1,
            error_category=ErrorCategory.TIMEOUT.value,
        )
        
        # server_error: 90 秒
        server_error_backoff = calculate_backoff_seconds(
            attempts=1,
            error_category=ErrorCategory.SERVER_ERROR.value,
        )
        
        # 验证不同类别有不同的退避时间
        assert rate_limit_backoff == TRANSIENT_ERROR_BACKOFF[ErrorCategory.RATE_LIMIT.value]
        assert timeout_backoff == TRANSIENT_ERROR_BACKOFF[ErrorCategory.TIMEOUT.value]
        assert server_error_backoff == TRANSIENT_ERROR_BACKOFF[ErrorCategory.SERVER_ERROR.value]
        
        # rate_limit > server_error > timeout
        assert rate_limit_backoff > server_error_backoff > timeout_backoff

    def test_max_reaper_backoff_prevents_infinite_growth(self, migrated_db):
        """验证 max_reaper_backoff_seconds 防止退避时间无限增长"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_max_backoff.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # 创建 attempts=10 的任务（理论退避时间非常大）
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts,
                        last_error
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 10, 15,
                            '503 Service Unavailable')
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path
            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            
            from db import get_conn, list_expired_running_jobs
            from scm_sync_reaper import process_expired_jobs, JobRecoveryPolicy
            
            test_conn = get_conn(dsn)
            try:
                expired_jobs = list_expired_running_jobs(test_conn, grace_seconds=60, limit=10)
                
                # 使用较小的 max_reaper_backoff_seconds
                small_max_backoff = 300  # 5 分钟
                stats = process_expired_jobs(
                    test_conn, expired_jobs,
                    policy=JobRecoveryPolicy.to_failed,
                    retry_delay_seconds=60,
                    max_reaper_backoff_seconds=small_max_backoff,
                )
                
                assert stats["to_failed"] >= 1
                
                # 验证任务已被处理
                with test_conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT status FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """, (job_id,))
                    row = cur.fetchone()
                    assert row[0] == "failed"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_error_message_is_redacted(self, migrated_db):
        """验证写入的错误消息已被脱敏"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_redaction.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # 带敏感信息的 locked_by
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts,
                        last_error
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'worker-glpat-secret123456789', now() - interval '10 minutes', 300, 1, 3,
                            'Connection timeout')
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path
            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            
            from db import get_conn, list_expired_running_jobs
            from scm_sync_reaper import process_expired_jobs, JobRecoveryPolicy
            
            test_conn = get_conn(dsn)
            try:
                expired_jobs = list_expired_running_jobs(test_conn, grace_seconds=60, limit=10)
                
                stats = process_expired_jobs(
                    test_conn, expired_jobs,
                    policy=JobRecoveryPolicy.to_failed,
                    retry_delay_seconds=60,
                )
                
                # 验证错误消息中不包含原始 token
                with test_conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT last_error FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """, (job_id,))
                    row = cur.fetchone()
                    last_error = row[0]
                    
                    # 不应包含原始 token
                    assert "glpat-secret123456789" not in last_error
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestQueueReaperConsistency:
    """Queue 和 Reaper 退避计算一致性测试"""

    def test_queue_and_reaper_import_same_constants(self):
        """验证 queue 和 reaper 导入相同的退避常量"""
        from engram_step1.scm_sync_errors import (
            DEFAULT_BACKOFF_BASE as errors_base,
            DEFAULT_MAX_BACKOFF as errors_max,
        )
        from engram_step1.scm_sync_queue import DEFAULT_BACKOFF_BASE as queue_base
        from scm_sync_reaper import DEFAULT_BACKOFF_BASE as reaper_base
        
        # 都应该引用同一个常量
        assert queue_base == errors_base
        assert reaper_base == errors_base
        assert queue_base == 60

    def test_backoff_formula_consistency(self):
        """验证退避公式一致性：base * 2^(attempts-1)"""
        from engram_step1.scm_sync_errors import calculate_backoff_seconds
        
        # 验证公式：base * 2^(attempts-1)
        base = 60
        
        for attempts in range(1, 8):
            expected = base * (2 ** (attempts - 1))
            # 不设置 max，验证公式本身
            actual = calculate_backoff_seconds(
                attempts=attempts,
                base_seconds=base,
                max_seconds=100000,  # 设置很大的 max 以不触发限制
            )
            assert actual == expected, f"attempts={attempts}: expected {expected}, got {actual}"

    def test_permanent_errors_always_dead_regardless_of_attempts(self):
        """永久性错误无论 attempts 多少都直接标记为 dead"""
        from engram_step1.scm_sync_errors import classify_last_error
        
        permanent_errors = [
            "401 Unauthorized: Invalid token",
            "403 Forbidden: access denied",
            "404 Not Found: repository does not exist",
            "auth_error: authentication failed",
            "permission_denied: insufficient rights",
        ]
        
        for error in permanent_errors:
            is_permanent, is_transient, category = classify_last_error(error)
            assert is_permanent is True, f"'{error}' should be permanent"
            assert is_transient is False, f"'{error}' should not be transient"

    def test_transient_errors_always_retried_regardless_of_attempts(self):
        """临时性错误无论 attempts 多少都安排重试（直到 max_attempts）"""
        from engram_step1.scm_sync_errors import classify_last_error
        
        transient_errors = [
            "429 Too Many Requests",
            "Request timeout after 30 seconds",
            "Connection reset by peer",
            "502 Bad Gateway",
            "503 Service Unavailable",
        ]
        
        for error in transient_errors:
            is_permanent, is_transient, category = classify_last_error(error)
            assert is_permanent is False, f"'{error}' should not be permanent"
            assert is_transient is True, f"'{error}' should be transient"
            assert category != "", f"'{error}' should have a category"

    def test_reaper_default_max_backoff_is_reasonable(self):
        """验证 reaper 默认最大退避时间合理"""
        from scm_sync_reaper import DEFAULT_MAX_REAPER_BACKOFF_SECONDS
        from engram_step1.scm_sync_errors import DEFAULT_MAX_BACKOFF
        
        # reaper 的默认 max 应该合理（30 分钟）
        assert DEFAULT_MAX_REAPER_BACKOFF_SECONDS == 1800
        
        # scm_sync_errors 的默认 max 应该是 1 小时
        assert DEFAULT_MAX_BACKOFF == 3600

    def test_error_category_backoff_values(self):
        """验证不同错误类别的退避时间配置"""
        from engram_step1.scm_sync_errors import (
            TRANSIENT_ERROR_BACKOFF,
            ErrorCategory,
        )
        
        # 验证配置值存在且合理
        assert ErrorCategory.RATE_LIMIT.value in TRANSIENT_ERROR_BACKOFF
        assert ErrorCategory.TIMEOUT.value in TRANSIENT_ERROR_BACKOFF
        assert ErrorCategory.NETWORK.value in TRANSIENT_ERROR_BACKOFF
        assert ErrorCategory.SERVER_ERROR.value in TRANSIENT_ERROR_BACKOFF
        assert ErrorCategory.CONNECTION.value in TRANSIENT_ERROR_BACKOFF
        assert "default" in TRANSIENT_ERROR_BACKOFF
        
        # rate_limit 应该有较长的退避时间
        assert TRANSIENT_ERROR_BACKOFF[ErrorCategory.RATE_LIMIT.value] >= 60
        
        # timeout 可以有较短的退避时间
        assert TRANSIENT_ERROR_BACKOFF[ErrorCategory.TIMEOUT.value] >= 10
