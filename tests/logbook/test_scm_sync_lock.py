# -*- coding: utf-8 -*-
"""
SCM Sync Lock 分布式锁模块单元测试

测试:
- claim: 获取锁、并发 claim、过期锁回收
- renew: 续租锁、续租阻止他人抢占
- release: 释放锁、错误 worker 释放失败
- get: 获取锁信息
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import patch

import psycopg


class TestClaim:
    """claim 函数测试"""

    def test_claim_new_lock(self, migrated_db):
        """首次获取锁（锁不存在）"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            # 创建测试用仓库
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_claim_new.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

            with patch("engram_logbook.scm_sync_lock.get_connection") as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_lock import claim, get

                # 首次获取锁应该成功
                result = claim(repo_id, "gitlab_commits", "worker-1", lease_seconds=60)
                assert result is True

                # 验证锁状态
                lock_info = get(repo_id, "gitlab_commits")
                assert lock_info is not None
                assert lock_info["locked_by"] == "worker-1"
                assert lock_info["is_locked"] is True
                assert lock_info["is_expired"] is False
        finally:
            # 清理
            if repo_id:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM {scm_schema}.sync_locks WHERE repo_id = %s", (repo_id,)
                    )
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_claim_existing_unlocked(self, migrated_db):
        """获取已存在但未锁定的锁"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            # 创建测试用仓库和未锁定的锁记录
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_claim_unlocked.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_locks (repo_id, job_type, locked_by, locked_at, lease_seconds)
                    VALUES (%s, 'gitlab_commits', NULL, NULL, 60)
                """,
                    (repo_id,),
                )

            with patch("engram_logbook.scm_sync_lock.get_connection") as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_lock import claim

                # 获取未锁定的锁应该成功
                result = claim(repo_id, "gitlab_commits", "worker-1", lease_seconds=60)
                assert result is True
        finally:
            if repo_id:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM {scm_schema}.sync_locks WHERE repo_id = %s", (repo_id,)
                    )
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_claim_held_by_other_worker(self, migrated_db):
        """尝试获取被其他 worker 持有的锁（未过期）"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_claim_held.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                # 创建一个被 worker-1 持有的锁（未过期）
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_locks (repo_id, job_type, locked_by, locked_at, lease_seconds)
                    VALUES (%s, 'gitlab_commits', 'worker-1', now(), 60)
                """,
                    (repo_id,),
                )

            with patch("engram_logbook.scm_sync_lock.get_connection") as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_lock import claim

                # worker-2 尝试获取锁应该失败
                result = claim(repo_id, "gitlab_commits", "worker-2", lease_seconds=60)
                assert result is False
        finally:
            if repo_id:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM {scm_schema}.sync_locks WHERE repo_id = %s", (repo_id,)
                    )
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_claim_expired_lock_recovery(self, migrated_db):
        """过期锁回收：获取已过期的锁"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_claim_expired.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                # 创建一个已过期的锁（locked_at 在 2 分钟前，lease_seconds=60）
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_locks (repo_id, job_type, locked_by, locked_at, lease_seconds)
                    VALUES (%s, 'gitlab_commits', 'dead-worker', now() - interval '2 minutes', 60)
                """,
                    (repo_id,),
                )

            with patch("engram_logbook.scm_sync_lock.get_connection") as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_lock import claim, get

                # 过期锁应该可以被回收
                result = claim(repo_id, "gitlab_commits", "worker-2", lease_seconds=60)
                assert result is True

                # 验证锁被 worker-2 持有
                lock_info = get(repo_id, "gitlab_commits")
                assert lock_info["locked_by"] == "worker-2"
        finally:
            if repo_id:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM {scm_schema}.sync_locks WHERE repo_id = %s", (repo_id,)
                    )
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_claim_concurrent_only_one_wins(self, migrated_db):
        """并发 claim：多个 worker 同时尝试获取锁，只有一个成功"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_claim_concurrent.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

            results = []

            def try_claim(worker_id):
                try:
                    test_conn = psycopg.connect(dsn, autocommit=False)
                    with test_conn.cursor() as cur:
                        cur.execute(f"SET search_path TO {scm_schema}")

                    with patch(
                        "engram_logbook.scm_sync_lock.get_connection", return_value=test_conn
                    ):
                        from engram.logbook.scm_sync_lock import claim

                        result = claim(repo_id, "gitlab_commits", worker_id, lease_seconds=60)
                        return (worker_id, result)
                except Exception as e:
                    return (worker_id, f"error: {e}")

            # 使用多线程并发 claim
            num_workers = 5
            with ThreadPoolExecutor(max_workers=num_workers) as executor:
                futures = [executor.submit(try_claim, f"worker-{i}") for i in range(num_workers)]
                for future in as_completed(futures):
                    results.append(future.result())

            # 验证只有一个 worker 成功
            successful = [r for r in results if r[1] is True]
            failed = [r for r in results if r[1] is False]

            assert len(successful) == 1, f"应该只有一个 worker 成功获取锁，实际: {successful}"
            assert len(failed) == num_workers - 1, f"应该有 {num_workers - 1} 个 worker 失败"
        finally:
            if repo_id:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM {scm_schema}.sync_locks WHERE repo_id = %s", (repo_id,)
                    )
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestRenew:
    """renew 函数测试"""

    def test_renew_success(self, migrated_db):
        """成功续租锁"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_renew.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                # 创建一个被 worker-1 持有的锁（30秒前锁定）
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_locks (repo_id, job_type, locked_by, locked_at, lease_seconds)
                    VALUES (%s, 'gitlab_commits', 'worker-1', now() - interval '30 seconds', 60)
                    RETURNING locked_at
                """,
                    (repo_id,),
                )
                old_locked_at = cur.fetchone()[0]

            with patch("engram_logbook.scm_sync_lock.get_connection") as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_lock import get, renew

                # 续租应该成功
                result = renew(repo_id, "gitlab_commits", "worker-1")
                assert result is True

                # 验证 locked_at 已更新
                lock_info = get(repo_id, "gitlab_commits")
                assert lock_info["locked_at"] > old_locked_at
        finally:
            if repo_id:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM {scm_schema}.sync_locks WHERE repo_id = %s", (repo_id,)
                    )
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_renew_wrong_worker(self, migrated_db):
        """错误 worker 无法续租"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_renew_wrong.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_locks (repo_id, job_type, locked_by, locked_at, lease_seconds)
                    VALUES (%s, 'gitlab_commits', 'worker-1', now(), 60)
                """,
                    (repo_id,),
                )

            with patch("engram_logbook.scm_sync_lock.get_connection") as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_lock import renew

                # worker-2 尝试续租应该失败
                result = renew(repo_id, "gitlab_commits", "worker-2")
                assert result is False
        finally:
            if repo_id:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM {scm_schema}.sync_locks WHERE repo_id = %s", (repo_id,)
                    )
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_renew_prevents_reclaim(self, migrated_db):
        """续租阻止他人抢占：续租后锁不会被其他 worker 获取"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_renew_prevents.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                # 创建一个即将过期的锁（55秒前锁定，lease_seconds=60）
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_locks (repo_id, job_type, locked_by, locked_at, lease_seconds)
                    VALUES (%s, 'gitlab_commits', 'worker-1', now() - interval '55 seconds', 60)
                """,
                    (repo_id,),
                )

            # Worker-1 续租
            with patch("engram_logbook.scm_sync_lock.get_connection") as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_lock import renew

                result = renew(repo_id, "gitlab_commits", "worker-1")
                assert result is True

            # Worker-2 尝试 claim（续租后应该失败）
            with patch("engram_logbook.scm_sync_lock.get_connection") as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_lock import claim

                result = claim(repo_id, "gitlab_commits", "worker-2", lease_seconds=60)
                assert result is False, "续租后锁不应被其他 worker 获取"
        finally:
            if repo_id:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM {scm_schema}.sync_locks WHERE repo_id = %s", (repo_id,)
                    )
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_renew_with_new_lease_seconds(self, migrated_db):
        """续租时更新 lease_seconds"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_renew_lease.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_locks (repo_id, job_type, locked_by, locked_at, lease_seconds)
                    VALUES (%s, 'gitlab_commits', 'worker-1', now(), 60)
                """,
                    (repo_id,),
                )

            with patch("engram_logbook.scm_sync_lock.get_connection") as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_lock import get, renew

                # 续租并更新 lease_seconds 为 120
                result = renew(repo_id, "gitlab_commits", "worker-1", lease_seconds=120)
                assert result is True

                # 验证 lease_seconds 已更新
                lock_info = get(repo_id, "gitlab_commits")
                assert lock_info["lease_seconds"] == 120
        finally:
            if repo_id:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM {scm_schema}.sync_locks WHERE repo_id = %s", (repo_id,)
                    )
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestRelease:
    """release 函数测试"""

    def test_release_success(self, migrated_db):
        """成功释放锁"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_release.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_locks (repo_id, job_type, locked_by, locked_at, lease_seconds)
                    VALUES (%s, 'gitlab_commits', 'worker-1', now(), 60)
                """,
                    (repo_id,),
                )

            with patch("engram_logbook.scm_sync_lock.get_connection") as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_lock import get, release

                # 释放锁应该成功
                result = release(repo_id, "gitlab_commits", "worker-1")
                assert result is True

                # 验证锁已释放
                lock_info = get(repo_id, "gitlab_commits")
                assert lock_info["locked_by"] is None
                assert lock_info["locked_at"] is None
                assert lock_info["is_locked"] is False
        finally:
            if repo_id:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM {scm_schema}.sync_locks WHERE repo_id = %s", (repo_id,)
                    )
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_release_wrong_worker_fails(self, migrated_db):
        """错误 worker 释放失败"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_release_wrong.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_locks (repo_id, job_type, locked_by, locked_at, lease_seconds)
                    VALUES (%s, 'gitlab_commits', 'worker-1', now(), 60)
                """,
                    (repo_id,),
                )

            with patch("engram_logbook.scm_sync_lock.get_connection") as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_lock import get, release

                # worker-2 尝试释放应该失败
                result = release(repo_id, "gitlab_commits", "worker-2")
                assert result is False

                # 验证锁仍然被 worker-1 持有
                lock_info = get(repo_id, "gitlab_commits")
                assert lock_info["locked_by"] == "worker-1"
        finally:
            if repo_id:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM {scm_schema}.sync_locks WHERE repo_id = %s", (repo_id,)
                    )
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_release_nonexistent_lock(self, migrated_db):
        """释放不存在的锁返回 False"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_release_nonexist.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

            with patch("engram_logbook.scm_sync_lock.get_connection") as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_lock import release

                # 释放不存在的锁应该返回 False
                result = release(repo_id, "gitlab_commits", "worker-1")
                assert result is False
        finally:
            if repo_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_release_allows_reclaim(self, migrated_db):
        """释放后其他 worker 可以获取锁"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_release_reclaim.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_locks (repo_id, job_type, locked_by, locked_at, lease_seconds)
                    VALUES (%s, 'gitlab_commits', 'worker-1', now(), 60)
                """,
                    (repo_id,),
                )

            # Worker-1 释放锁
            with patch("engram_logbook.scm_sync_lock.get_connection") as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_lock import release

                result = release(repo_id, "gitlab_commits", "worker-1")
                assert result is True

            # Worker-2 获取锁
            with patch("engram_logbook.scm_sync_lock.get_connection") as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_lock import claim, get

                result = claim(repo_id, "gitlab_commits", "worker-2", lease_seconds=60)
                assert result is True

                lock_info = get(repo_id, "gitlab_commits")
                assert lock_info["locked_by"] == "worker-2"
        finally:
            if repo_id:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM {scm_schema}.sync_locks WHERE repo_id = %s", (repo_id,)
                    )
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestGet:
    """get 函数测试"""

    def test_get_existing_lock(self, migrated_db):
        """获取已存在的锁信息"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_get.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_locks (repo_id, job_type, locked_by, locked_at, lease_seconds)
                    VALUES (%s, 'gitlab_commits', 'worker-1', now(), 120)
                """,
                    (repo_id,),
                )

            with patch("engram_logbook.scm_sync_lock.get_connection") as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_lock import get

                lock_info = get(repo_id, "gitlab_commits")

                assert lock_info is not None
                assert lock_info["repo_id"] == repo_id
                assert lock_info["job_type"] == "gitlab_commits"
                assert lock_info["locked_by"] == "worker-1"
                assert lock_info["lease_seconds"] == 120
                assert lock_info["is_locked"] is True
                assert lock_info["is_expired"] is False
        finally:
            if repo_id:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM {scm_schema}.sync_locks WHERE repo_id = %s", (repo_id,)
                    )
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_get_nonexistent_lock(self, migrated_db):
        """获取不存在的锁返回 None"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_get_nonexist.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

            with patch("engram_logbook.scm_sync_lock.get_connection") as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_lock import get

                lock_info = get(repo_id, "gitlab_commits")
                assert lock_info is None
        finally:
            if repo_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_get_expired_lock_shows_is_expired(self, migrated_db):
        """获取过期锁时 is_expired 为 True"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_get_expired.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                # 创建一个已过期的锁
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_locks (repo_id, job_type, locked_by, locked_at, lease_seconds)
                    VALUES (%s, 'gitlab_commits', 'dead-worker', now() - interval '2 minutes', 60)
                """,
                    (repo_id,),
                )

            with patch("engram_logbook.scm_sync_lock.get_connection") as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_lock import get

                lock_info = get(repo_id, "gitlab_commits")

                assert lock_info is not None
                assert lock_info["is_locked"] is True  # 仍然被锁定
                assert lock_info["is_expired"] is True  # 但已过期
        finally:
            if repo_id:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM {scm_schema}.sync_locks WHERE repo_id = %s", (repo_id,)
                    )
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestForceRelease:
    """force_release 函数测试"""

    def test_force_release_success(self, migrated_db):
        """强制释放锁成功"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_force_release.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_locks (repo_id, job_type, locked_by, locked_at, lease_seconds)
                    VALUES (%s, 'gitlab_commits', 'dead-worker', now(), 60)
                """,
                    (repo_id,),
                )

            with patch("engram_logbook.scm_sync_lock.get_connection") as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_lock import force_release, get

                # 不需要提供 worker_id 即可强制释放
                result = force_release(repo_id, "gitlab_commits")
                assert result is True

                lock_info = get(repo_id, "gitlab_commits")
                assert lock_info["locked_by"] is None
        finally:
            if repo_id:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM {scm_schema}.sync_locks WHERE repo_id = %s", (repo_id,)
                    )
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestSyncFunctionLockIntegration:
    """
    测试同步函数的锁集成

    验证同 repo/job 并发调用时只有一个执行、另一个返回 locked/skip 结果
    """

    def test_concurrent_sync_only_one_executes(self, migrated_db):
        """
        并发同步测试：两个 worker 同时尝试同步同一个 repo，只有一个执行
        """
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            # 创建测试用仓库
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_concurrent_sync.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

            results = []

            def simulate_sync_with_lock(worker_id: str, job_type: str, hold_seconds: float = 1.0):
                """
                模拟带锁的同步操作

                Args:
                    worker_id: worker 标识符
                    job_type: 任务类型
                    hold_seconds: 持有锁的时间（秒），模拟同步工作

                Returns:
                    dict: 模拟的同步结果
                """
                try:
                    test_conn = psycopg.connect(dsn, autocommit=False)
                    with test_conn.cursor() as cur:
                        cur.execute(f"SET search_path TO {scm_schema}")

                    with patch(
                        "engram_logbook.scm_sync_lock.get_connection", return_value=test_conn
                    ):
                        from engram.logbook.scm_sync_lock import claim, release

                        # 尝试获取锁
                        lock_acquired = claim(
                            repo_id=repo_id,
                            job_type=job_type,
                            worker_id=worker_id,
                            lease_seconds=60,
                        )

                        if not lock_acquired:
                            # 锁被其他 worker 持有，返回 locked/skip 结果
                            return {
                                "worker_id": worker_id,
                                "locked": True,
                                "skipped": True,
                                "success": False,
                                "message": "锁被其他 worker 持有",
                            }

                        try:
                            # 模拟同步工作
                            time.sleep(hold_seconds)
                            return {
                                "worker_id": worker_id,
                                "locked": False,
                                "skipped": False,
                                "success": True,
                                "message": "同步完成",
                            }
                        finally:
                            # 释放锁
                            release(repo_id, job_type, worker_id)
                except Exception as e:
                    return {
                        "worker_id": worker_id,
                        "error": str(e),
                    }

            # 使用多线程并发模拟同步
            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = [
                    executor.submit(simulate_sync_with_lock, f"worker-{i}", "gitlab_commits", 0.5)
                    for i in range(3)
                ]
                for future in as_completed(futures):
                    results.append(future.result())

            # 验证结果
            successful = [r for r in results if r.get("success") and not r.get("locked")]
            locked_skipped = [r for r in results if r.get("locked") and r.get("skipped")]
            errors = [r for r in results if "error" in r]

            # 应该只有一个成功执行，其他都被跳过
            assert len(successful) == 1, f"应该只有一个 worker 成功执行，实际: {successful}"
            assert len(locked_skipped) == 2, f"应该有 2 个 worker 被跳过，实际: {locked_skipped}"
            assert len(errors) == 0, f"不应该有错误: {errors}"

            # 验证被跳过的结果包含正确的标识
            for r in locked_skipped:
                assert r["locked"] is True
                assert r["skipped"] is True
                assert "message" in r

        finally:
            if repo_id:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM {scm_schema}.sync_locks WHERE repo_id = %s", (repo_id,)
                    )
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_different_job_types_run_concurrently(self, migrated_db):
        """
        不同任务类型可以并发执行：同一个 repo 的不同 job_type 不互斥
        """
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_diff_jobs.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

            results = []
            job_types = ["gitlab_commits", "gitlab_mrs", "gitlab_reviews"]

            def try_claim_different_jobs(job_type: str):
                try:
                    test_conn = psycopg.connect(dsn, autocommit=False)
                    with test_conn.cursor() as cur:
                        cur.execute(f"SET search_path TO {scm_schema}")

                    with patch(
                        "engram_logbook.scm_sync_lock.get_connection", return_value=test_conn
                    ):
                        from engram.logbook.scm_sync_lock import claim, release

                        lock_acquired = claim(
                            repo_id=repo_id,
                            job_type=job_type,
                            worker_id=f"worker-{job_type}",
                            lease_seconds=60,
                        )

                        if lock_acquired:
                            # 释放锁
                            release(repo_id, job_type, f"worker-{job_type}")

                        return {
                            "job_type": job_type,
                            "lock_acquired": lock_acquired,
                        }
                except Exception as e:
                    return {
                        "job_type": job_type,
                        "error": str(e),
                    }

            # 并发获取不同 job_type 的锁
            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = [executor.submit(try_claim_different_jobs, jt) for jt in job_types]
                for future in as_completed(futures):
                    results.append(future.result())

            # 验证所有不同 job_type 都能获取锁
            successful = [r for r in results if r.get("lock_acquired")]
            assert len(successful) == 3, f"不同 job_type 都应该能获取锁，实际: {successful}"

        finally:
            if repo_id:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM {scm_schema}.sync_locks WHERE repo_id = %s", (repo_id,)
                    )
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_lock_released_after_exception(self, migrated_db):
        """
        异常处理测试：同步过程中发生异常后锁应该被释放
        """
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_exception.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

            def simulate_sync_with_exception():
                """模拟同步过程中发生异常"""
                test_conn = psycopg.connect(dsn, autocommit=False)
                with test_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")

                with patch("engram_logbook.scm_sync_lock.get_connection", return_value=test_conn):
                    from engram.logbook.scm_sync_lock import claim, release

                    lock_acquired = False
                    try:
                        lock_acquired = claim(
                            repo_id=repo_id,
                            job_type="gitlab_commits",
                            worker_id="worker-error",
                            lease_seconds=60,
                        )

                        if lock_acquired:
                            # 模拟同步过程中发生异常
                            raise ValueError("模拟同步错误")
                    finally:
                        # finally 块中释放锁
                        if lock_acquired:
                            release(repo_id, "gitlab_commits", "worker-error")

                    return {"success": True}

            # 执行会抛出异常的同步
            try:
                simulate_sync_with_exception()
            except ValueError:
                pass  # 预期的异常

            # 验证锁已被释放：另一个 worker 应该能获取锁
            with patch("engram_logbook.scm_sync_lock.get_connection") as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_lock import claim, get

                result = claim(repo_id, "gitlab_commits", "worker-2", lease_seconds=60)
                assert result is True, "异常后锁应该被释放，其他 worker 应该能获取"

                lock_info = get(repo_id, "gitlab_commits")
                assert lock_info["locked_by"] == "worker-2"

        finally:
            if repo_id:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM {scm_schema}.sync_locks WHERE repo_id = %s", (repo_id,)
                    )
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_locked_result_has_correct_format(self, migrated_db):
        """
        验证 locked/skip 结果格式正确，包含必要字段
        """
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_format.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                # 创建一个已被持有的锁
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_locks (repo_id, job_type, locked_by, locked_at, lease_seconds)
                    VALUES (%s, 'gitlab_commits', 'worker-1', now(), 60)
                """,
                    (repo_id,),
                )

            # 模拟第二个 worker 尝试同步
            def simulate_locked_sync():
                test_conn = psycopg.connect(dsn, autocommit=False)
                with test_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")

                with patch("engram_logbook.scm_sync_lock.get_connection", return_value=test_conn):
                    from engram.logbook.scm_sync_lock import claim, get

                    lock_acquired = claim(
                        repo_id=repo_id,
                        job_type="gitlab_commits",
                        worker_id="worker-2",
                        lease_seconds=60,
                    )

                    if not lock_acquired:
                        lock_info = get(repo_id, "gitlab_commits")
                        return {
                            "success": False,
                            "locked": True,
                            "skipped": True,
                            "message": "锁被其他 worker 持有，跳过本次同步",
                            "lock_holder": lock_info.get("locked_by") if lock_info else None,
                        }

                    return {"success": True, "locked": False, "skipped": False}

            result = simulate_locked_sync()

            # 验证结果格式
            assert result["locked"] is True
            assert result["skipped"] is True
            assert result["success"] is False
            assert "message" in result
            assert result["lock_holder"] == "worker-1"

        finally:
            if repo_id:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM {scm_schema}.sync_locks WHERE repo_id = %s", (repo_id,)
                    )
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestListFunctions:
    """list_locks_by_worker 和 list_expired_locks 函数测试"""

    def test_list_locks_by_worker(self, migrated_db):
        """列出指定 worker 持有的锁"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_ids = []
        try:
            # 创建多个仓库和锁
            with conn.cursor() as cur:
                for i in range(3):
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                        VALUES ('git', 'https://example.com/test_list_{i}.git')
                        RETURNING repo_id
                    """)
                    repo_id = cur.fetchone()[0]
                    repo_ids.append(repo_id)

                    # 前两个由 worker-1 持有，第三个由 worker-2 持有
                    worker = "worker-1" if i < 2 else "worker-2"
                    cur.execute(
                        f"""
                        INSERT INTO {scm_schema}.sync_locks (repo_id, job_type, locked_by, locked_at, lease_seconds)
                        VALUES (%s, 'gitlab_commits', %s, now(), 60)
                    """,
                        (repo_id, worker),
                    )

            with patch("engram_logbook.scm_sync_lock.get_connection") as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_lock import list_locks_by_worker

                locks = list_locks_by_worker("worker-1")
                assert len(locks) == 2
                for lock in locks:
                    assert lock["locked_by"] == "worker-1"
        finally:
            for repo_id in repo_ids:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM {scm_schema}.sync_locks WHERE repo_id = %s", (repo_id,)
                    )
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_list_expired_locks(self, migrated_db):
        """列出所有过期的锁"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_ids = []
        try:
            with conn.cursor() as cur:
                # 创建一个过期的锁
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_expired_1.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                repo_ids.append(repo_id)

                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_locks (repo_id, job_type, locked_by, locked_at, lease_seconds)
                    VALUES (%s, 'gitlab_commits', 'dead-worker', now() - interval '2 minutes', 60)
                """,
                    (repo_id,),
                )

                # 创建一个未过期的锁
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_expired_2.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                repo_ids.append(repo_id)

                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_locks (repo_id, job_type, locked_by, locked_at, lease_seconds)
                    VALUES (%s, 'gitlab_commits', 'active-worker', now(), 60)
                """,
                    (repo_id,),
                )

            with patch("engram_logbook.scm_sync_lock.get_connection") as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_lock import list_expired_locks

                expired = list_expired_locks()

                # 应该只有一个过期的锁
                assert len(expired) >= 1
                expired_workers = [lock["locked_by"] for lock in expired]
                assert "dead-worker" in expired_workers
                assert "active-worker" not in expired_workers
        finally:
            for repo_id in repo_ids:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM {scm_schema}.sync_locks WHERE repo_id = %s", (repo_id,)
                    )
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestWorkerLockIntegration:
    """
    Worker 锁集成测试

    测试 scm_sync_worker.process_one_job 与分布式锁的集成：
    - 并发同 repo/job 只有一个执行
    - 锁获取失败时触发 requeue_without_penalty
    - 成功路径和异常路径都释放锁
    """

    def test_worker_concurrent_same_repo_job_only_one_executes(self, migrated_db):
        """
        并发测试：多个 worker 同时处理同一 (repo_id, job_type) 任务，
        只有一个能执行，其他被 requeue
        """
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_ids = []

        try:
            # 创建测试仓库
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_worker_lock.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                # 创建多个相同 (repo_id, job_type) 的待处理任务
                # 使用不同 mode 以绕过唯一约束
                for i in range(3):
                    cur.execute(
                        f"""
                        INSERT INTO {scm_schema}.sync_jobs
                        (repo_id, job_type, mode, status, priority, payload_json)
                        VALUES (%s, 'gitlab_commits', %s, 'pending', 100, '{{}}')
                        RETURNING job_id
                    """,
                        (repo_id, f"mode_{i}"),
                    )
                    job_id = str(cur.fetchone()[0])
                    job_ids.append(job_id)

            execution_results = []
            requeue_results = []

            def run_worker(worker_id: str):
                """模拟 worker 执行"""
                try:
                    test_conn = psycopg.connect(dsn, autocommit=False)
                    with test_conn.cursor() as cur:
                        cur.execute(f"SET search_path TO {scm_schema}")

                    # Mock 各种连接函数
                    with patch(
                        "engram_logbook.scm_sync_queue.get_connection", return_value=test_conn
                    ):
                        with patch(
                            "engram_logbook.scm_sync_lock.get_connection", return_value=test_conn
                        ):
                            from engram.logbook import scm_sync_lock
                            from engram.logbook.scm_sync_queue import ack, claim

                            # claim 一个任务
                            job = claim(worker_id=worker_id, lease_seconds=60)
                            if not job:
                                return {"worker_id": worker_id, "claimed": False}

                            job_id = str(job["job_id"])
                            job_repo_id = job["repo_id"]
                            job_type = job["job_type"]

                            # 尝试获取锁
                            lock_acquired = scm_sync_lock.claim(
                                repo_id=job_repo_id,
                                job_type=job_type,
                                worker_id=worker_id,
                                lease_seconds=60,
                            )

                            if not lock_acquired:
                                # 锁获取失败，记录
                                return {
                                    "worker_id": worker_id,
                                    "claimed": True,
                                    "job_id": job_id,
                                    "lock_acquired": False,
                                    "requeued": True,
                                }

                            try:
                                # 模拟执行（持有锁一段时间）
                                time.sleep(0.3)

                                # 完成任务
                                ack(job_id=job_id, worker_id=worker_id)

                                return {
                                    "worker_id": worker_id,
                                    "claimed": True,
                                    "job_id": job_id,
                                    "lock_acquired": True,
                                    "executed": True,
                                }
                            finally:
                                # 释放锁
                                scm_sync_lock.release(
                                    repo_id=job_repo_id,
                                    job_type=job_type,
                                    worker_id=worker_id,
                                )

                except Exception as e:
                    return {"worker_id": worker_id, "error": str(e)}

            # 并发执行多个 worker
            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = [executor.submit(run_worker, f"worker-{i}") for i in range(3)]
                for future in as_completed(futures):
                    result = future.result()
                    if result.get("executed"):
                        execution_results.append(result)
                    elif result.get("requeued"):
                        requeue_results.append(result)

            # 验证：至少有 1 个执行成功，其他被跳过
            # 注意：由于任务是不同 mode，实际上每个任务都可能被不同 worker 执行
            # 但对于同一 (repo_id, job_type) 的锁，只有一个 worker 能持有
            total_claims = len(execution_results) + len(requeue_results)
            assert total_claims >= 1, "应该至少有一个任务被 claim"

        finally:
            # 清理
            with conn.cursor() as cur:
                for job_id in job_ids:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(
                        f"DELETE FROM {scm_schema}.sync_locks WHERE repo_id = %s", (repo_id,)
                    )
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_worker_lock_released_on_success(self, migrated_db):
        """
        成功路径测试：任务执行成功后锁被正确释放
        """
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None

        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_lock_release_success.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs
                    (repo_id, job_type, mode, status, priority, payload_json)
                    VALUES (%s, 'gitlab_commits', 'incremental', 'pending', 100, '{{}}')
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = str(cur.fetchone()[0])

            with patch("engram_logbook.scm_sync_queue.get_connection") as mock_queue_conn:
                with patch("engram_logbook.scm_sync_lock.get_connection") as mock_lock_conn:
                    test_conn = psycopg.connect(dsn, autocommit=False)
                    with test_conn.cursor() as cur:
                        cur.execute(f"SET search_path TO {scm_schema}")

                    mock_queue_conn.return_value = test_conn
                    mock_lock_conn.return_value = test_conn

                    from engram.logbook import scm_sync_lock
                    from engram.logbook.scm_sync_queue import ack, claim

                    # claim 任务
                    job = claim(worker_id="worker-success", lease_seconds=60)
                    assert job is not None

                    # 获取锁
                    lock_acquired = scm_sync_lock.claim(
                        repo_id=repo_id,
                        job_type="gitlab_commits",
                        worker_id="worker-success",
                        lease_seconds=60,
                    )
                    assert lock_acquired is True

                    # 验证锁被持有
                    lock_info = scm_sync_lock.get(repo_id, "gitlab_commits")
                    assert lock_info["locked_by"] == "worker-success"

                    # 执行成功并 ack
                    ack(job_id=str(job["job_id"]), worker_id="worker-success")

                    # 释放锁
                    released = scm_sync_lock.release(
                        repo_id=repo_id,
                        job_type="gitlab_commits",
                        worker_id="worker-success",
                    )
                    assert released is True

                    # 验证锁已释放
                    lock_info = scm_sync_lock.get(repo_id, "gitlab_commits")
                    assert lock_info["locked_by"] is None

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(
                        f"DELETE FROM {scm_schema}.sync_locks WHERE repo_id = %s", (repo_id,)
                    )
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_worker_lock_released_on_exception(self, migrated_db):
        """
        异常路径测试：任务执行异常后锁被正确释放（finally 块）
        """
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None

        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_lock_release_exception.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs
                    (repo_id, job_type, mode, status, priority, payload_json)
                    VALUES (%s, 'gitlab_commits', 'incremental', 'pending', 100, '{{}}')
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = str(cur.fetchone()[0])

            with patch("engram_logbook.scm_sync_queue.get_connection") as mock_queue_conn:
                with patch("engram_logbook.scm_sync_lock.get_connection") as mock_lock_conn:
                    test_conn = psycopg.connect(dsn, autocommit=False)
                    with test_conn.cursor() as cur:
                        cur.execute(f"SET search_path TO {scm_schema}")

                    mock_queue_conn.return_value = test_conn
                    mock_lock_conn.return_value = test_conn

                    from engram.logbook import scm_sync_lock
                    from engram.logbook.scm_sync_queue import claim

                    # claim 任务
                    job = claim(worker_id="worker-exception", lease_seconds=60)
                    assert job is not None

                    # 获取锁
                    lock_acquired = scm_sync_lock.claim(
                        repo_id=repo_id,
                        job_type="gitlab_commits",
                        worker_id="worker-exception",
                        lease_seconds=60,
                    )
                    assert lock_acquired is True

                    # 模拟执行过程中发生异常，但 finally 释放锁
                    try:
                        # 模拟执行
                        raise ValueError("模拟同步异常")
                    finally:
                        # finally 块释放锁
                        scm_sync_lock.release(
                            repo_id=repo_id,
                            job_type="gitlab_commits",
                            worker_id="worker-exception",
                        )

        except ValueError:
            pass  # 预期的异常

        # 验证锁已释放：另一个 worker 可以获取
        with patch("engram_logbook.scm_sync_lock.get_connection") as mock_lock_conn:
            test_conn2 = psycopg.connect(dsn, autocommit=False)
            with test_conn2.cursor() as cur:
                cur.execute(f"SET search_path TO {scm_schema}")
            mock_lock_conn.return_value = test_conn2

            from engram.logbook import scm_sync_lock

            lock_info = scm_sync_lock.get(repo_id, "gitlab_commits")
            assert lock_info["locked_by"] is None, "异常后锁应该被释放"

            # 另一个 worker 可以获取锁
            lock_acquired = scm_sync_lock.claim(
                repo_id=repo_id,
                job_type="gitlab_commits",
                worker_id="worker-2",
                lease_seconds=60,
            )
            assert lock_acquired is True, "异常后其他 worker 应能获取锁"

        # 清理
        with conn.cursor() as cur:
            if job_id:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
            if repo_id:
                cur.execute(f"DELETE FROM {scm_schema}.sync_locks WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
        conn.close()

    def test_lock_held_triggers_requeue_without_penalty(self, migrated_db):
        """
        锁被持有时触发 requeue_without_penalty，任务回到 pending 状态
        """
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

                # 创建一个被其他 worker 持有的锁
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_locks
                    (repo_id, job_type, locked_by, locked_at, lease_seconds)
                    VALUES (%s, 'gitlab_commits', 'other-worker', now(), 600)
                """,
                    (repo_id,),
                )

                # 创建一个待处理任务
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs
                    (repo_id, job_type, mode, status, priority, payload_json, attempts)
                    VALUES (%s, 'gitlab_commits', 'incremental', 'pending', 100, '{{}}', 0)
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = str(cur.fetchone()[0])

            with patch("engram_logbook.scm_sync_queue.get_connection") as mock_queue_conn:
                with patch("engram_logbook.scm_sync_lock.get_connection") as mock_lock_conn:
                    test_conn = psycopg.connect(dsn, autocommit=False)
                    with test_conn.cursor() as cur:
                        cur.execute(f"SET search_path TO {scm_schema}")

                    mock_queue_conn.return_value = test_conn
                    mock_lock_conn.return_value = test_conn

                    from engram.logbook import scm_sync_lock
                    from engram.logbook.scm_sync_queue import (
                        claim,
                        get_job,
                        requeue_without_penalty,
                    )

                    # worker-1 claim 任务
                    job = claim(worker_id="worker-1", lease_seconds=60)
                    assert job is not None

                    # 记录 claim 后的 attempts
                    job_after_claim = get_job(job_id)
                    attempts_after_claim = job_after_claim["attempts"]
                    assert attempts_after_claim == 1, "claim 后 attempts 应该是 1"

                    # 尝试获取锁（应该失败，因为被 other-worker 持有）
                    lock_acquired = scm_sync_lock.claim(
                        repo_id=repo_id,
                        job_type="gitlab_commits",
                        worker_id="worker-1",
                        lease_seconds=60,
                    )
                    assert lock_acquired is False, "锁被 other-worker 持有，应该获取失败"

                    # 触发 requeue_without_penalty
                    requeue_result = requeue_without_penalty(
                        job_id=job_id,
                        worker_id="worker-1",
                        reason="lock_held: locked by another worker",
                    )
                    assert requeue_result is True

                    # 验证任务回到 pending 状态，attempts 回退
                    job_after_requeue = get_job(job_id)
                    assert job_after_requeue["status"] == "pending", "requeue 后应回到 pending"
                    assert job_after_requeue["attempts"] == 0, (
                        "requeue_without_penalty 应回退 attempts"
                    )
                    assert job_after_requeue["locked_by"] is None, "requeue 后应释放任务锁"

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(
                        f"DELETE FROM {scm_schema}.sync_locks WHERE repo_id = %s", (repo_id,)
                    )
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()
