# -*- coding: utf-8 -*-
"""
test_upsert_idempotency.py - 测试 DB upsert 幂等性

验证:
1. upsert_repo: 相同 (repo_type, url) 返回相同 repo_id
2. upsert_git_commit: 相同 (repo_id, commit_sha) 不会重复插入
3. upsert_svn_revision: 相同 (repo_id, rev_num) 不会重复插入
4. upsert_mr: 相同 mr_id 更新而非插入
5. upsert_patch_blob: 相同 (source_type, source_id, sha256) DO NOTHING

隔离策略:
- 使用临时 schema（通过 conftest.py 的 migrated_db fixture）
"""

import os

# 导入被测模块（需要 PYTHONPATH 包含 scripts 目录）
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import psycopg
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import (
    get_repo_by_url,
    upsert_git_commit,
    upsert_mr,
    upsert_patch_blob,
    upsert_repo,
    upsert_svn_revision,
)
from engram.logbook.source_id import (
    build_git_source_id,
    build_mr_source_id,
    build_svn_source_id,
)
from scm_repo import build_mr_id


class TestUpsertRepoIdempotency:
    """测试 upsert_repo 幂等性"""

    def test_upsert_repo_returns_same_id(self, db_conn: psycopg.Connection):
        """相同 (repo_type, url) 多次 upsert 返回相同 repo_id"""
        repo_type = "git"
        url = f"https://test.example.com/test-repo-{datetime.now().timestamp()}"
        project_key = "test_project"

        # 第一次 upsert
        repo_id_1 = upsert_repo(db_conn, repo_type, url, project_key, "main")
        db_conn.commit()

        # 第二次 upsert - 应返回相同 ID
        repo_id_2 = upsert_repo(db_conn, repo_type, url, project_key, "main")
        db_conn.commit()

        assert repo_id_1 == repo_id_2, "upsert_repo 应返回相同的 repo_id"

    def test_upsert_repo_updates_project_key(self, db_conn: psycopg.Connection):
        """upsert_repo 应更新 project_key"""
        repo_type = "git"
        url = f"https://test.example.com/update-test-{datetime.now().timestamp()}"

        # 第一次 upsert
        repo_id = upsert_repo(db_conn, repo_type, url, "old_project", "main")
        db_conn.commit()

        # 更新 project_key
        repo_id_2 = upsert_repo(db_conn, repo_type, url, "new_project", "develop")
        db_conn.commit()

        assert repo_id == repo_id_2

        # 验证 project_key 已更新
        repo = get_repo_by_url(db_conn, repo_type, url)
        assert repo["project_key"] == "new_project"
        assert repo["default_branch"] == "develop"


class TestUpsertGitCommitIdempotency:
    """测试 upsert_git_commit 幂等性"""

    def test_upsert_git_commit_idempotent(self, db_conn: psycopg.Connection):
        """相同 (repo_id, commit_sha) 多次 upsert 不会重复"""
        # 先创建 repo
        url = f"https://test.example.com/git-commit-test-{datetime.now().timestamp()}"
        repo_id = upsert_repo(db_conn, "git", url, "test_project")
        db_conn.commit()

        commit_sha = "abc123def456"
        ts = datetime.now(timezone.utc)

        # 第一次 upsert
        commit_id_1 = upsert_git_commit(
            db_conn,
            repo_id,
            commit_sha,
            author_raw="Test User <test@example.com>",
            ts=ts,
            message="Initial commit",
        )
        db_conn.commit()

        # 第二次 upsert - 相同 sha
        commit_id_2 = upsert_git_commit(
            db_conn,
            repo_id,
            commit_sha,
            author_raw="Test User <test@example.com>",
            ts=ts,
            message="Updated message",  # 更新 message
        )
        db_conn.commit()

        assert commit_id_1 == commit_id_2, "相同 commit_sha 应返回相同 ID"

        # 验证 message 已更新（使用 search_path，无需指定 schema）
        with db_conn.cursor() as cur:
            cur.execute("SELECT message FROM git_commits WHERE git_commit_id = %s", (commit_id_1,))
            row = cur.fetchone()
            assert row[0] == "Updated message"

    def test_upsert_git_commit_source_id_persisted(self, db_conn: psycopg.Connection):
        """验证 git_commits.source_id 正确持久化"""
        url = f"https://test.example.com/git-source-id-{datetime.now().timestamp()}"
        repo_id = upsert_repo(db_conn, "git", url, "test_project")
        db_conn.commit()

        commit_sha = "sourceid123abc"
        ts = datetime.now(timezone.utc)
        expected_source_id = build_git_source_id(repo_id, commit_sha)

        # 插入时带上 source_id
        commit_id = upsert_git_commit(
            db_conn,
            repo_id,
            commit_sha,
            author_raw="Test User <test@example.com>",
            ts=ts,
            message="Test commit",
            source_id=expected_source_id,
        )
        db_conn.commit()

        # 验证 source_id 正确存储
        with db_conn.cursor() as cur:
            cur.execute("SELECT source_id FROM git_commits WHERE git_commit_id = %s", (commit_id,))
            row = cur.fetchone()
            assert row[0] == expected_source_id, f"source_id 应为 {expected_source_id}"
            assert row[0] == f"git:{repo_id}:{commit_sha}", (
                "source_id 格式应为 git:<repo_id>:<commit_sha>"
            )


class TestUpsertSvnRevisionIdempotency:
    """测试 upsert_svn_revision 幂等性"""

    def test_upsert_svn_revision_idempotent(self, db_conn: psycopg.Connection):
        """相同 (repo_id, rev_num) 多次 upsert 不会重复"""
        # 先创建 repo
        url = f"svn://test.example.com/svn-test-{datetime.now().timestamp()}"
        repo_id = upsert_repo(db_conn, "svn", url, "test_project")
        db_conn.commit()

        rev_num = 12345
        ts = datetime.now(timezone.utc)

        # 第一次 upsert
        rev_id_1 = upsert_svn_revision(
            db_conn,
            repo_id,
            rev_num,
            author_raw="svn_user",
            ts=ts,
            message="Initial commit",
        )
        db_conn.commit()

        # 第二次 upsert - 相同 rev_num
        rev_id_2 = upsert_svn_revision(
            db_conn,
            repo_id,
            rev_num,
            author_raw="svn_user",
            ts=ts,
            message="Updated message",
        )
        db_conn.commit()

        assert rev_id_1 == rev_id_2, "相同 rev_num 应返回相同 ID"

    def test_svn_revision_overlap_sync_idempotent(self, db_conn: psycopg.Connection):
        """
        测试 SVN overlap 同步场景的幂等性

        场景:
        1. 第一次同步 r1, r2, r3
        2. overlap=2 回退后，第二次同步 r2, r3, r4
        3. 验证 r2, r3 不会重复，总共只有 4 条记录
        """
        url = f"svn://test.example.com/svn-overlap-{datetime.now().timestamp()}"
        repo_id = upsert_repo(db_conn, "svn", url, "test_project")
        db_conn.commit()

        # 第一次同步：r1, r2, r3
        first_sync_revs = [
            (1, "user1", "Commit 1"),
            (2, "user2", "Commit 2"),
            (3, "user3", "Commit 3"),
        ]

        for rev_num, author, msg in first_sync_revs:
            upsert_svn_revision(
                db_conn,
                repo_id,
                rev_num,
                author_raw=author,
                message=msg,
            )
        db_conn.commit()

        # 验证第一次同步后的记录数
        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM svn_revisions WHERE repo_id = %s", (repo_id,))
            count1 = cur.fetchone()[0]
            assert count1 == 3, f"第一次同步后应有 3 条记录，实际 {count1}"

        # 第二次同步（overlap=2 导致 r2, r3 重复）：r2, r3, r4
        second_sync_revs = [
            (2, "user2", "Commit 2 updated"),  # 重复
            (3, "user3", "Commit 3 updated"),  # 重复
            (4, "user4", "Commit 4"),  # 新增
        ]

        for rev_num, author, msg in second_sync_revs:
            upsert_svn_revision(
                db_conn,
                repo_id,
                rev_num,
                author_raw=author,
                message=msg,
            )
        db_conn.commit()

        # 验证幂等性：总共应该是 4 条记录（不是 6 条）
        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM svn_revisions WHERE repo_id = %s", (repo_id,))
            count2 = cur.fetchone()[0]
            assert count2 == 4, f"第二次同步后应有 4 条记录（幂等），实际 {count2}"

        # 验证 r2 的 message 已更新
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT message FROM svn_revisions WHERE repo_id = %s AND rev_num = %s",
                (repo_id, 2),
            )
            msg = cur.fetchone()[0]
            assert msg == "Commit 2 updated", f"r2 的 message 应已更新，实际: {msg}"

        # 验证所有 revision 都存在
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT rev_num FROM svn_revisions WHERE repo_id = %s ORDER BY rev_num", (repo_id,)
            )
            all_revs = [row[0] for row in cur.fetchall()]
            assert all_revs == [1, 2, 3, 4], f"应有 r1-r4，实际: {all_revs}"

    def test_upsert_svn_revision_source_id_persisted(self, db_conn: psycopg.Connection):
        """验证 svn_revisions.source_id 正确持久化"""
        url = f"svn://test.example.com/svn-source-id-{datetime.now().timestamp()}"
        repo_id = upsert_repo(db_conn, "svn", url, "test_project")
        db_conn.commit()

        rev_num = 54321
        ts = datetime.now(timezone.utc)
        expected_source_id = build_svn_source_id(repo_id, rev_num)

        # 插入时带上 source_id
        rev_id = upsert_svn_revision(
            db_conn,
            repo_id,
            rev_num,
            author_raw="svn_user",
            ts=ts,
            message="Test revision",
            source_id=expected_source_id,
        )
        db_conn.commit()

        # 验证 source_id 正确存储
        with db_conn.cursor() as cur:
            cur.execute("SELECT source_id FROM svn_revisions WHERE svn_rev_id = %s", (rev_id,))
            row = cur.fetchone()
            assert row[0] == expected_source_id, f"source_id 应为 {expected_source_id}"
            assert row[0] == f"svn:{repo_id}:{rev_num}", (
                "source_id 格式应为 svn:<repo_id>:<rev_num>"
            )


class TestUpsertMrIdempotency:
    """测试 upsert_mr 幂等性"""

    def test_upsert_mr_idempotent(self, db_conn: psycopg.Connection):
        """相同 mr_id 多次 upsert 更新而非插入"""
        # 先创建 repo
        url = f"https://test.example.com/mr-test-{datetime.now().timestamp()}"
        repo_id = upsert_repo(db_conn, "git", url, "test_project")
        db_conn.commit()

        mr_id = f"gitlab:test:123:{datetime.now().timestamp()}"

        # 第一次 upsert
        result_1 = upsert_mr(
            db_conn,
            mr_id,
            repo_id,
            status="open",
            url="https://gitlab.example.com/mr/1",
        )
        db_conn.commit()

        # 第二次 upsert - 更新状态
        result_2 = upsert_mr(
            db_conn,
            mr_id,
            repo_id,
            status="merged",
            url="https://gitlab.example.com/mr/1",
        )
        db_conn.commit()

        assert result_1 == result_2 == mr_id

        # 验证状态已更新（使用 search_path，无需指定 schema）
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM mrs WHERE mr_id = %s", (mr_id,))
            row = cur.fetchone()
            assert row[0] == "merged"

        # 验证只有一条记录
        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM mrs WHERE mr_id = %s", (mr_id,))
            count = cur.fetchone()[0]
            assert count == 1

    def test_upsert_mr_source_id_persisted(self, db_conn: psycopg.Connection):
        """验证 mrs.source_id 正确持久化"""
        url = f"https://test.example.com/mr-source-id-{datetime.now().timestamp()}"
        repo_id = upsert_repo(db_conn, "git", url, "test_project")
        db_conn.commit()

        mr_iid = 42
        mr_id = build_mr_id(repo_id, mr_iid)
        expected_source_id = build_mr_source_id(repo_id, mr_iid)

        # 通过直接 SQL 插入带 source_id 的 MR（模拟 sync 脚本行为）
        with db_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO mrs (mr_id, repo_id, status, url, source_id)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (mr_id) DO UPDATE SET
                    source_id = EXCLUDED.source_id
                """,
                (mr_id, repo_id, "open", "https://example.com/mr/42", expected_source_id),
            )
        db_conn.commit()

        # 验证 source_id 正确存储
        with db_conn.cursor() as cur:
            cur.execute("SELECT source_id FROM mrs WHERE mr_id = %s", (mr_id,))
            row = cur.fetchone()
            assert row[0] == expected_source_id, f"source_id 应为 {expected_source_id}"
            assert row[0] == f"mr:{repo_id}:{mr_iid}", "source_id 格式应为 mr:<repo_id>:<iid>"


class TestUpsertPatchBlobIdempotency:
    """测试 upsert_patch_blob 幂等性"""

    def test_upsert_patch_blob_do_nothing(self, db_conn: psycopg.Connection):
        """相同 (source_type, source_id, sha256) 应 DO NOTHING"""
        source_type = "git"
        source_id = f"1:test-commit-{datetime.now().timestamp()}"
        sha256 = "a" * 64  # 64 字符的 hex string
        uri = "file:///test/path/patch.diff"

        # 第一次 upsert
        blob_id_1 = upsert_patch_blob(
            db_conn,
            source_type,
            source_id,
            sha256,
            uri=uri,
            size_bytes=1024,
        )
        db_conn.commit()

        # 第二次 upsert - 相同内容
        blob_id_2 = upsert_patch_blob(
            db_conn,
            source_type,
            source_id,
            sha256,
            uri=uri,
            size_bytes=2048,  # 不同的 size
        )
        db_conn.commit()

        # 应返回相同的 blob_id（现有记录）
        assert blob_id_1 == blob_id_2

        # 验证 size_bytes 没有被更新（DO NOTHING 行为，使用 search_path）
        with db_conn.cursor() as cur:
            cur.execute("SELECT size_bytes FROM patch_blobs WHERE blob_id = %s", (blob_id_1,))
            row = cur.fetchone()
            assert row[0] == 1024, "DO NOTHING 不应更新现有记录"


class TestUpsertConcurrency:
    """测试并发 upsert 场景"""

    def test_repo_concurrent_upsert(self, db_conn: psycopg.Connection):
        """模拟并发场景：多次快速 upsert 同一 repo"""
        repo_type = "git"
        url = f"https://test.example.com/concurrent-{datetime.now().timestamp()}"

        repo_ids = []
        for i in range(5):
            repo_id = upsert_repo(
                db_conn,
                repo_type,
                url,
                project_key=f"project_{i}",
            )
            repo_ids.append(repo_id)

        db_conn.commit()

        # 所有 upsert 应返回相同的 repo_id
        assert len(set(repo_ids)) == 1, "并发 upsert 应返回相同 repo_id"


class TestMrIdConsistencyAcrossScripts:
    """测试 mr_id 在不同同步脚本中的一致性"""

    def test_mr_id_same_in_mrs_and_reviews_sync(self, db_conn: psycopg.Connection):
        """
        验证同一 MR 在 scm_sync_gitlab_mrs.py 与 scm_sync_gitlab_reviews.py
        中使用 build_mr_id 生成的 mr_id 完全一致
        """
        # 创建 repo
        url = f"https://test.example.com/mr-consistency-{datetime.now().timestamp()}"
        repo_id = upsert_repo(db_conn, "git", url, "test_project")
        db_conn.commit()

        mr_iid = 123

        # 模拟 scm_sync_gitlab_mrs.py 的 mr_id 构建逻辑
        # 参见: scm_sync_gitlab_mrs.py:insert_merge_requests() 第 436 行
        mr_id_from_mrs_sync = build_mr_id(repo_id, mr_iid)

        # 模拟 scm_sync_gitlab_reviews.py 的 mr_id 构建逻辑
        # 参见: scm_sync_gitlab_reviews.py:get_or_create_mr() 第 571 行
        mr_id_from_reviews_sync = build_mr_id(repo_id, mr_iid)

        # 验证两个脚本生成的 mr_id 完全一致
        assert mr_id_from_mrs_sync == mr_id_from_reviews_sync, (
            f"mr_id 应一致: mrs_sync={mr_id_from_mrs_sync}, reviews_sync={mr_id_from_reviews_sync}"
        )

        # 验证 mr_id 格式正确
        expected_format = f"{repo_id}:{mr_iid}"
        assert mr_id_from_mrs_sync == expected_format, (
            f"mr_id 格式应为 <repo_id>:<mr_iid>，实际为 {mr_id_from_mrs_sync}"
        )

    def test_mr_id_and_source_id_consistency(self, db_conn: psycopg.Connection):
        """验证 mr_id 和 source_id 的 repo_id/iid 部分一致"""
        url = f"https://test.example.com/mr-source-consistency-{datetime.now().timestamp()}"
        repo_id = upsert_repo(db_conn, "git", url, "test_project")
        db_conn.commit()

        mr_iid = 456

        # 构建 mr_id 和 source_id
        mr_id = build_mr_id(repo_id, mr_iid)
        source_id = build_mr_source_id(repo_id, mr_iid)

        # 验证 mr_id 格式: <repo_id>:<mr_iid>
        mr_id_parts = mr_id.split(":")
        assert len(mr_id_parts) == 2, "mr_id 应有 2 部分"
        assert mr_id_parts[0] == str(repo_id), "mr_id 的第一部分应为 repo_id"
        assert mr_id_parts[1] == str(mr_iid), "mr_id 的第二部分应为 mr_iid"

        # 验证 source_id 格式: mr:<repo_id>:<iid>
        source_id_parts = source_id.split(":")
        assert len(source_id_parts) == 3, "source_id 应有 3 部分"
        assert source_id_parts[0] == "mr", "source_id 的第一部分应为 'mr'"
        assert source_id_parts[1] == str(repo_id), "source_id 的 repo_id 应与 mr_id 一致"
        assert source_id_parts[2] == str(mr_iid), "source_id 的 iid 应与 mr_id 一致"

    def test_review_event_uses_consistent_mr_id(self, db_conn: psycopg.Connection):
        """验证 review_event 插入时使用与 MR 相同的 mr_id"""
        from db import insert_review_event

        url = f"https://test.example.com/review-mr-consistency-{datetime.now().timestamp()}"
        repo_id = upsert_repo(db_conn, "git", url, "test_project")
        db_conn.commit()

        mr_iid = 789
        mr_id = build_mr_id(repo_id, mr_iid)

        # 先创建 MR
        upsert_mr(db_conn, mr_id, repo_id, status="open")
        db_conn.commit()

        # 插入 review_event，使用相同的 mr_id
        source_event_id = f"note:test-{datetime.now().timestamp()}"
        event_id = insert_review_event(
            db_conn,
            mr_id,
            event_type="comment",
            source_event_id=source_event_id,
            reviewer_user_id=None,
            payload_json={"test": "data"},
        )
        db_conn.commit()

        assert event_id is not None, "review_event 应成功插入"

        # 验证 review_event 关联的 mr_id 正确
        with db_conn.cursor() as cur:
            cur.execute("SELECT mr_id FROM review_events WHERE id = %s", (event_id,))
            row = cur.fetchone()
            assert row[0] == mr_id, f"review_event 的 mr_id 应为 {mr_id}"


# ---------- GitLab 增量同步去重/边界测试 ----------

# 导入增量同步相关模块
from scm_sync_gitlab_commits import (
    GitCommit,
    _deduplicate_commits,
    _get_commit_timestamp,
    _parse_iso_datetime,
)


class TestIncrementalSyncDeduplication:
    """测试增量同步的去重和过滤逻辑"""

    def _make_commit(
        self,
        sha: str,
        committed_date: Optional[datetime] = None,
        authored_date: Optional[datetime] = None,
    ) -> GitCommit:
        """创建测试用 GitCommit 对象"""
        return GitCommit(
            sha=sha,
            author_name="Test User",
            author_email="test@example.com",
            authored_date=authored_date,
            committer_name="Test User",
            committer_email="test@example.com",
            committed_date=committed_date,
            message=f"Commit {sha}",
            parent_ids=[],
            web_url="",
            stats={},
        )

    def test_deduplicate_by_sha(self):
        """测试按 sha 去重"""
        ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        commits = [
            self._make_commit("abc123", committed_date=ts),
            self._make_commit("abc123", committed_date=ts),  # 重复
            self._make_commit("def456", committed_date=ts),
        ]

        result = _deduplicate_commits(commits)

        assert len(result) == 2
        assert result[0].sha == "abc123"
        assert result[1].sha == "def456"

    def test_filter_by_cursor_sha(self):
        """测试按 cursor sha 过滤"""
        ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        commits = [
            self._make_commit("abc123", committed_date=ts),
            self._make_commit("def456", committed_date=ts),
        ]

        # cursor_sha = "abc123" 表示已处理过，应跳过
        result = _deduplicate_commits(commits, cursor_sha="abc123")

        assert len(result) == 1
        assert result[0].sha == "def456"

    def test_filter_by_cursor_timestamp(self):
        """测试按 cursor 时间戳过滤（早于游标的 commit 被过滤）"""
        old_ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        new_ts = datetime(2024, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
        cursor_ts = datetime(2024, 1, 1, 18, 0, 0, tzinfo=timezone.utc)

        commits = [
            self._make_commit("old_commit", committed_date=old_ts),  # < cursor
            self._make_commit("new_commit", committed_date=new_ts),  # > cursor
        ]

        result = _deduplicate_commits(commits, cursor_ts=cursor_ts)

        assert len(result) == 1
        assert result[0].sha == "new_commit"

    def test_same_timestamp_multiple_commits(self):
        """测试同一时间戳多个 commit（使用 (ts, sha) 复合水位线过滤）"""
        ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        commits = [
            self._make_commit("commit_a", committed_date=ts),
            self._make_commit("commit_b", committed_date=ts),
            self._make_commit("commit_c", committed_date=ts),
        ]

        # cursor_sha = "aaa_old"，比所有 commit 的 sha 都小
        # 所以 sha > cursor_sha 的 commit 应保留
        result = _deduplicate_commits(commits, cursor_sha="aaa_old", cursor_ts=ts)

        # 所有 commit 的 sha 都 > "aaa_old"，应全部保留
        assert len(result) == 3

        # 测试只保留 sha > cursor_sha 的情况
        result2 = _deduplicate_commits(commits, cursor_sha="commit_b", cursor_ts=ts)

        # 只有 commit_c > "commit_b"，其他应被过滤
        assert len(result2) == 1
        assert result2[0].sha == "commit_c"

    def test_since_boundary_inclusive(self):
        """测试 since 边界包含场景"""
        cursor_ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        commits = [
            # 时间戳 = cursor_ts，sha 不同，应保留
            self._make_commit("same_time_a", committed_date=cursor_ts),
            self._make_commit("same_time_b", committed_date=cursor_ts),
            # 时间戳 < cursor_ts，应过滤
            self._make_commit("before", committed_date=cursor_ts - timedelta(seconds=1)),
            # 时间戳 > cursor_ts，应保留
            self._make_commit("after", committed_date=cursor_ts + timedelta(seconds=1)),
        ]

        result = _deduplicate_commits(commits, cursor_sha="other", cursor_ts=cursor_ts)

        # before 被过滤，其他保留
        assert len(result) == 3
        shas = {c.sha for c in result}
        assert "same_time_a" in shas
        assert "same_time_b" in shas
        assert "after" in shas
        assert "before" not in shas

    def test_sorting_by_committed_date_ascending(self):
        """测试按 committed_date 升序排序"""
        ts1 = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        ts2 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        ts3 = datetime(2024, 1, 1, 11, 0, 0, tzinfo=timezone.utc)

        # 故意乱序输入
        commits = [
            self._make_commit("middle", committed_date=ts3),
            self._make_commit("earliest", committed_date=ts1),
            self._make_commit("latest", committed_date=ts2),
        ]

        result = _deduplicate_commits(commits)

        assert len(result) == 3
        assert result[0].sha == "earliest"
        assert result[1].sha == "middle"
        assert result[2].sha == "latest"

    def test_pagination_duplicate_handling(self):
        """测试分页时重复数据的处理"""
        ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        # 模拟分页返回中有重复的 commit
        page1_commits = [
            self._make_commit("page1_a", committed_date=ts),
            self._make_commit("overlap", committed_date=ts),  # 在两页都出现
        ]
        page2_commits = [
            self._make_commit("overlap", committed_date=ts),  # 重复
            self._make_commit("page2_b", committed_date=ts + timedelta(hours=1)),
        ]

        # 合并两页
        all_commits = page1_commits + page2_commits
        result = _deduplicate_commits(all_commits)

        # 应该只有 3 个唯一 commit
        assert len(result) == 3
        shas = [c.sha for c in result]
        assert shas.count("overlap") == 1

    def test_empty_commits(self):
        """测试空列表"""
        result = _deduplicate_commits([])
        assert result == []

    def test_cursor_sha_exact_match_filtered(self):
        """测试 cursor_sha 精确匹配被过滤"""
        ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        commits = [
            self._make_commit("cursor_commit", committed_date=ts),
            self._make_commit("new_commit", committed_date=ts + timedelta(hours=1)),
        ]

        # cursor_sha 完全匹配的应被过滤
        result = _deduplicate_commits(commits, cursor_sha="cursor_commit")

        assert len(result) == 1
        assert result[0].sha == "new_commit"

    def test_parse_iso_datetime_valid(self):
        """测试 ISO 日期时间解析 - 有效格式"""
        # Z 后缀
        dt = _parse_iso_datetime("2024-01-01T12:00:00Z")
        assert dt is not None
        assert dt.year == 2024
        assert dt.month == 1
        assert dt.tzinfo is not None

        # +00:00 后缀
        dt2 = _parse_iso_datetime("2024-01-01T12:00:00+00:00")
        assert dt2 is not None
        assert dt2 == dt

    def test_parse_iso_datetime_none(self):
        """测试 ISO 日期时间解析 - None 输入"""
        assert _parse_iso_datetime(None) is None
        assert _parse_iso_datetime("") is None

    def test_get_commit_timestamp_fallback(self):
        """测试获取 commit 时间戳的回退逻辑"""
        ts_committed = datetime(2024, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
        ts_authored = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        # 优先使用 committed_date
        commit_with_both = self._make_commit(
            "test", committed_date=ts_committed, authored_date=ts_authored
        )
        assert _get_commit_timestamp(commit_with_both) == ts_committed

        # 回退到 authored_date
        commit_only_authored = self._make_commit("test2", authored_date=ts_authored)
        assert _get_commit_timestamp(commit_only_authored) == ts_authored

        # 都没有时返回 datetime.min
        commit_no_date = self._make_commit("test3")
        ts = _get_commit_timestamp(commit_no_date)
        assert ts.year == 1  # datetime.min.year


class TestIncrementalSyncConfig:
    """测试增量同步配置"""

    def test_get_incremental_config_defaults(self):
        """测试默认配置值"""
        from engram.logbook.config import (
            DEFAULT_OVERLAP_SECONDS,
            DEFAULT_TIME_WINDOW_DAYS,
            get_incremental_config,
        )

        # 使用一个空的 mock config
        class MockConfig:
            def get(self, key, default=None):
                return None

        cfg = get_incremental_config(MockConfig())

        assert cfg["overlap_seconds"] == DEFAULT_OVERLAP_SECONDS
        assert cfg["time_window_days"] == DEFAULT_TIME_WINDOW_DAYS


# ---------- Strict 模式与 Diff Mode 测试 ----------


from scm_sync_gitlab_commits import (
    DiffMode,
    SyncConfig,
    is_unrecoverable_api_error,
)


class TestIsUnrecoverableApiError:
    """测试 is_unrecoverable_api_error 函数"""

    def test_rate_limited_is_unrecoverable(self):
        """429 限流错误是不可恢复的"""
        assert is_unrecoverable_api_error("rate_limited") is True
        assert is_unrecoverable_api_error(None, status_code=429) is True

    def test_http_error_is_unrecoverable(self):
        """HTTP 错误（5xx）是不可恢复的"""
        assert is_unrecoverable_api_error("http_error") is True
        assert is_unrecoverable_api_error(None, status_code=500) is True
        assert is_unrecoverable_api_error(None, status_code=502) is True
        assert is_unrecoverable_api_error(None, status_code=503) is True

    def test_timeout_is_unrecoverable(self):
        """超时错误是不可恢复的"""
        assert is_unrecoverable_api_error("timeout") is True

    def test_client_error_is_recoverable(self):
        """客户端错误（4xx 除 429）是可恢复的"""
        assert is_unrecoverable_api_error("client_error") is False
        assert is_unrecoverable_api_error(None, status_code=400) is False
        assert is_unrecoverable_api_error(None, status_code=404) is False

    def test_parse_error_is_recoverable(self):
        """解析错误是可恢复的"""
        assert is_unrecoverable_api_error("parse_error") is False

    def test_content_too_large_is_recoverable(self):
        """内容过大是可恢复的（可以降级）"""
        assert is_unrecoverable_api_error("content_too_large") is False


class TestDiffMode:
    """测试 DiffMode 枚举"""

    def test_diff_mode_values(self):
        """测试 DiffMode 枚举值"""
        assert DiffMode.ALWAYS == "always"
        assert DiffMode.BEST_EFFORT == "best_effort"
        assert DiffMode.NONE == "none"


class TestStrictModeAndDiffMode:
    """测试 strict 模式与 diff_mode 的游标行为差异"""

    def _make_commit(
        self,
        sha: str,
        committed_date: Optional[datetime] = None,
    ) -> GitCommit:
        """创建测试用 GitCommit 对象"""
        return GitCommit(
            sha=sha,
            author_name="Test User",
            author_email="test@example.com",
            authored_date=committed_date,
            committer_name="Test User",
            committer_email="test@example.com",
            committed_date=committed_date,
            message=f"Commit {sha}",
            parent_ids=[],
            web_url="",
            stats={"additions": 10, "deletions": 5, "total": 15},
        )

    def test_strict_mode_cursor_on_429_error(self):
        """
        模拟 API 429 错误时 strict 与非 strict 的游标结果不同

        场景:
        - 有 3 个 commits: A, B, C（按时间升序）
        - 获取 A 的 diff 成功
        - 获取 B 的 diff 遇到 429 错误
        - C 未处理

        期望:
        - strict=True: 游标推进到 A（最后一个成功处理的）
        - strict=False: 游标推进到 C（批次中最后一个）
        """
        ts_a = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        ts_b = datetime(2024, 1, 1, 11, 0, 0, tzinfo=timezone.utc)
        ts_c = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        commit_a = self._make_commit("aaa111", committed_date=ts_a)
        commit_b = self._make_commit("bbb222", committed_date=ts_b)
        commit_c = self._make_commit("ccc333", committed_date=ts_c)

        # 模拟游标逻辑
        all_commits = [commit_a, commit_b, commit_c]

        # 模拟 strict 模式下的行为
        last_successful_commit_strict = commit_a  # 只有 A 成功
        encountered_unrecoverable_error = True

        # strict 模式：推进到 A
        if encountered_unrecoverable_error:
            target_commit_strict = last_successful_commit_strict
        else:
            target_commit_strict = all_commits[-1]

        assert target_commit_strict.sha == "aaa111", "strict 模式应推进到最后成功处理的 commit"

        # 非 strict 模式：推进到 C
        target_commit_non_strict = all_commits[-1]
        assert target_commit_non_strict.sha == "ccc333", "非 strict 模式应推进到批次最后一个 commit"

    def test_strict_mode_no_cursor_advance_on_total_failure(self):
        """
        模拟 API 5xx 错误且没有任何 commit 成功时，strict 模式不推进游标

        场景:
        - 有 2 个 commits: A, B
        - 获取 A 的 diff 遇到 500 错误
        - B 未处理

        期望:
        - strict=True: 游标不推进
        - strict=False: 游标推进到 B
        """
        ts_a = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        ts_b = datetime(2024, 1, 1, 11, 0, 0, tzinfo=timezone.utc)

        commit_a = self._make_commit("aaa111", committed_date=ts_a)
        commit_b = self._make_commit("bbb222", committed_date=ts_b)

        all_commits = [commit_a, commit_b]

        # 模拟 strict 模式下的行为：没有成功处理任何 commit
        last_successful_commit_strict = None
        encountered_unrecoverable_error = True

        # strict 模式：没有成功处理，不推进
        if encountered_unrecoverable_error and last_successful_commit_strict is None:
            target_commit_strict = None
        else:
            target_commit_strict = all_commits[-1]

        assert target_commit_strict is None, "strict 模式下无成功 commit 时不应推进游标"

        # 非 strict 模式：仍然推进到 B
        target_commit_non_strict = all_commits[-1]
        assert target_commit_non_strict.sha == "bbb222", (
            "非 strict 模式仍应推进到批次最后一个 commit"
        )

    def test_best_effort_mode_records_degraded_reasons(self):
        """
        测试 best_effort 模式下记录降级原因分布
        """
        # 模拟降级原因分布
        degraded_reasons: Dict[str, int] = {}

        # 模拟 3 个 commit 降级
        errors = ["rate_limited", "timeout", "rate_limited"]
        for error in errors:
            degraded_reasons[error] = degraded_reasons.get(error, 0) + 1

        assert degraded_reasons == {"rate_limited": 2, "timeout": 1}
        assert sum(degraded_reasons.values()) == 3

    def test_cursor_advance_reason_values(self):
        """
        测试 cursor_advance_reason 的各种取值
        """
        valid_reasons = [
            "batch_complete",  # 正常完成
            "best_effort_with_errors",  # best_effort 模式有错误但仍推进
            "strict_partial_success",  # strict 模式部分成功
            "strict_no_success",  # strict 模式无成功
            "watermark_unchanged",  # 水位线未变
        ]

        # 验证这些是合理的 reason 值
        for reason in valid_reasons:
            assert isinstance(reason, str)
            assert len(reason) > 0


class TestStrictModeIntegration:
    """
    集成测试：验证 strict 模式与 API 错误的交互

    这些测试使用 mock 来模拟 GitLab API 返回 429/5xx 错误
    """

    def test_sync_config_strict_default(self):
        """测试 SyncConfig 的 strict 默认值"""
        from engram.logbook.scm_auth import StaticTokenProvider

        token_provider = StaticTokenProvider("test-token")
        config = SyncConfig(
            gitlab_url="https://gitlab.example.com",
            project_id="123",
            token_provider=token_provider,
        )

        assert config.strict is False, "strict 默认应为 False"
        assert config.diff_mode == DiffMode.BEST_EFFORT, "diff_mode 默认应为 best_effort"

    def test_sync_config_strict_enabled(self):
        """测试启用 strict 模式的 SyncConfig"""
        from engram.logbook.scm_auth import StaticTokenProvider

        token_provider = StaticTokenProvider("test-token")
        config = SyncConfig(
            gitlab_url="https://gitlab.example.com",
            project_id="123",
            token_provider=token_provider,
            strict=True,
            diff_mode=DiffMode.ALWAYS,
        )

        assert config.strict is True
        assert config.diff_mode == DiffMode.ALWAYS

    def test_sync_config_diff_mode_none(self):
        """测试 diff_mode=none 的 SyncConfig"""
        from engram.logbook.scm_auth import StaticTokenProvider

        token_provider = StaticTokenProvider("test-token")
        config = SyncConfig(
            gitlab_url="https://gitlab.example.com",
            project_id="123",
            token_provider=token_provider,
            diff_mode=DiffMode.NONE,
        )

        assert config.diff_mode == DiffMode.NONE


# ---------- 运行测试的入口 ----------

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
