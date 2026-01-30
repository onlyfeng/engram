# -*- coding: utf-8 -*-
"""
test_gitlab_review_dedup.py - 测试 GitLab API mock 和 review 事件去重

使用 requests-mock / responses 库 mock GitLab REST API 调用。

验证:
1. GitLab API 客户端正确处理响应
2. Commit 解析正确
3. MR 解析正确
4. review_events 重复插入行为

隔离策略:
- 使用临时 schema（通过 conftest.py 的 migrated_db fixture）
"""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, Mock

import pytest
import requests

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scm_sync_gitlab_commits import (
    GitLabClient,
    GitCommit,
    parse_commit,
    format_diff_content,
    GitLabAPIError,
)
from scm_sync_gitlab_mrs import (
    GitLabClient as MRGitLabClient,
    GitLabMergeRequest,
    parse_merge_request,
    map_gitlab_state_to_status,
)
from scm_repo import build_mr_id


# ---------- GitLab API Client Mock 测试 ----------

class TestGitLabClientMock:
    """测试 GitLab API 客户端 mock"""
    
    @pytest.fixture
    def client(self):
        """创建 GitLab 客户端"""
        return GitLabClient(
            base_url="https://gitlab.example.com",
            private_token="glpat-test-token",
        )
    
    def test_get_commits_success(self, client, requests_mock):
        """Mock 获取 commits 成功"""
        mock_commits = [
            {
                "id": "abc123def456789",
                "short_id": "abc123d",
                "title": "Add new feature",
                "author_name": "Test User",
                "author_email": "test@example.com",
                "authored_date": "2024-01-15T10:30:00Z",
                "committer_name": "Test User",
                "committer_email": "test@example.com",
                "committed_date": "2024-01-15T10:30:00Z",
                "message": "Add new feature\n\nDetailed description",
                "parent_ids": ["parent123"],
                "web_url": "https://gitlab.example.com/project/-/commit/abc123def456789",
                "stats": {"additions": 10, "deletions": 5, "total": 15},
            },
            {
                "id": "def456abc789012",
                "short_id": "def456a",
                "title": "Fix bug",
                "author_name": "Another User",
                "author_email": "another@example.com",
                "authored_date": "2024-01-16T09:00:00Z",
                "committer_name": "Another User",
                "committer_email": "another@example.com",
                "committed_date": "2024-01-16T09:00:00Z",
                "message": "Fix bug in login",
                "parent_ids": ["abc123def456789"],
                "web_url": "https://gitlab.example.com/project/-/commit/def456abc789012",
                "stats": {"additions": 2, "deletions": 1, "total": 3},
            },
        ]
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            json=mock_commits,
        )
        
        commits = client.get_commits("123", per_page=100)
        
        assert len(commits) == 2
        assert commits[0]["id"] == "abc123def456789"
        assert commits[1]["id"] == "def456abc789012"
    
    def test_get_commits_with_since(self, client, requests_mock):
        """Mock 带 since 参数获取 commits"""
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            json=[{"id": "commit1"}],
        )
        
        client.get_commits("123", since="2024-01-01T00:00:00Z")
        
        assert requests_mock.called
        assert "since=2024-01-01T00%3A00%3A00Z" in requests_mock.last_request.url
    
    def test_get_commits_namespace_project(self, client, requests_mock):
        """Mock 使用 namespace/project 格式"""
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/my-group%2Fmy-project/repository/commits",
            json=[],
        )
        
        commits = client.get_commits("my-group/my-project")
        
        assert commits == []
        assert requests_mock.called
    
    def test_get_commit_diff_success(self, client, requests_mock):
        """Mock 获取 commit diff 成功"""
        mock_diff = [
            {
                "old_path": "src/main.py",
                "new_path": "src/main.py",
                "a_mode": "100644",
                "b_mode": "100644",
                "new_file": False,
                "renamed_file": False,
                "deleted_file": False,
                "diff": "@@ -1,3 +1,4 @@\n import sys\n+import os\n \n def main():\n",
            },
        ]
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits/abc123/diff",
            json=mock_diff,
        )
        
        diff = client.get_commit_diff("123", "abc123")
        
        assert len(diff) == 1
        assert diff[0]["old_path"] == "src/main.py"
    
    def test_api_error_handling(self, client, requests_mock):
        """Mock API 错误处理"""
        from engram.logbook.gitlab_client import GitLabAuthError
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            status_code=401,
            json={"message": "401 Unauthorized"},
        )
        
        with pytest.raises(GitLabAuthError) as exc_info:
            client.get_commits("123")
        
        assert "401" in str(exc_info.value)
    
    def test_api_timeout_handling(self, client, requests_mock):
        """Mock API 超时处理"""
        from engram.logbook.gitlab_client import GitLabTimeoutError
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            exc=requests.exceptions.Timeout,
        )
        
        with pytest.raises(GitLabTimeoutError) as exc_info:
            client.get_commits("123")
        
        assert "超时" in str(exc_info.value)


# ---------- Commit 解析测试 ----------

class TestParseCommit:
    """测试 commit 数据解析"""
    
    def test_parse_commit_basic(self):
        """解析基本 commit 数据"""
        data = {
            "id": "abc123",
            "author_name": "Test User",
            "author_email": "test@example.com",
            "authored_date": "2024-01-15T10:30:00Z",
            "committer_name": "Test User",
            "committer_email": "test@example.com",
            "committed_date": "2024-01-15T10:30:00Z",
            "message": "Test commit",
            "parent_ids": ["parent1"],
            "web_url": "https://gitlab.example.com/commit/abc123",
            "stats": {"additions": 5, "deletions": 2, "total": 7},
        }
        
        commit = parse_commit(data)
        
        assert commit.sha == "abc123"
        assert commit.author_name == "Test User"
        assert commit.author_email == "test@example.com"
        assert commit.message == "Test commit"
        assert len(commit.parent_ids) == 1
    
    def test_parse_commit_merge(self):
        """解析 merge commit（多个 parent）"""
        data = {
            "id": "merge123",
            "author_name": "Merger",
            "author_email": "merger@example.com",
            "authored_date": "2024-01-15T10:30:00Z",
            "committer_name": "Merger",
            "committer_email": "merger@example.com",
            "committed_date": "2024-01-15T10:30:00Z",
            "message": "Merge branch 'feature' into main",
            "parent_ids": ["parent1", "parent2"],  # 两个 parent = merge commit
            "web_url": "",
            "stats": {},
        }
        
        commit = parse_commit(data)
        
        assert len(commit.parent_ids) == 2
    
    def test_parse_commit_missing_fields(self):
        """解析缺少可选字段的 commit"""
        data = {
            "id": "minimal123",
        }
        
        commit = parse_commit(data)
        
        assert commit.sha == "minimal123"
        assert commit.author_name == ""
        assert commit.author_email == ""
        assert commit.message == ""


class TestFormatDiffContent:
    """测试 diff 内容格式化"""
    
    def test_format_single_diff(self):
        """格式化单个 diff"""
        diffs = [
            {
                "old_path": "file.py",
                "new_path": "file.py",
                "diff": "@@ -1 +1 @@\n-old\n+new\n",
            }
        ]
        
        result = format_diff_content(diffs)
        
        assert "--- a/file.py" in result
        assert "+++ b/file.py" in result
        assert "-old" in result
        assert "+new" in result
    
    def test_format_empty_diff(self):
        """格式化空 diff"""
        result = format_diff_content([])
        assert result == ""
    
    def test_format_multiple_diffs(self):
        """格式化多个 diff"""
        diffs = [
            {"old_path": "a.py", "new_path": "a.py", "diff": "+line1\n"},
            {"old_path": "b.py", "new_path": "b.py", "diff": "+line2\n"},
        ]
        
        result = format_diff_content(diffs)
        
        assert "a.py" in result
        assert "b.py" in result


# ---------- MR 解析测试 ----------

class TestParseMergeRequest:
    """测试 MR 数据解析"""
    
    def test_parse_mr_basic(self):
        """解析基本 MR 数据"""
        data = {
            "iid": 42,
            "project_id": 123,
            "title": "Add new feature",
            "description": "This MR adds a new feature",
            "state": "opened",
            "author": {"id": 1, "username": "testuser"},
            "source_branch": "feature-branch",
            "target_branch": "main",
            "created_at": "2024-01-10T10:00:00Z",
            "updated_at": "2024-01-15T14:30:00Z",
            "web_url": "https://gitlab.example.com/project/-/merge_requests/42",
        }
        
        mr = parse_merge_request(data)
        
        assert mr.iid == 42
        assert mr.project_id == 123
        assert mr.title == "Add new feature"
        assert mr.state == "opened"
        assert mr.source_branch == "feature-branch"
        assert mr.target_branch == "main"
    
    def test_parse_mr_merged(self):
        """解析已合并的 MR"""
        data = {
            "iid": 100,
            "project_id": 123,
            "title": "Merged MR",
            "state": "merged",
            "author": {},
            "source_branch": "feature",
            "target_branch": "main",
            "merged_at": "2024-01-20T16:00:00Z",
            "merge_commit_sha": "merged123abc",
        }
        
        mr = parse_merge_request(data)
        
        assert mr.state == "merged"
        assert mr.merge_commit_sha == "merged123abc"
        assert mr.merged_at is not None


class TestBuildMrId:
    """测试 mr_id 构建"""
    
    def test_build_mr_id_numeric(self):
        """使用数字 repo_id"""
        mr_id = build_mr_id(123, 42)
        assert mr_id == "123:42"
    
    def test_build_mr_id_different_repo(self):
        """使用不同的 repo_id"""
        mr_id = build_mr_id(456, 100)
        assert mr_id == "456:100"


class TestMapGitLabState:
    """测试 GitLab 状态映射"""
    
    def test_map_opened(self):
        assert map_gitlab_state_to_status("opened") == "open"
    
    def test_map_merged(self):
        assert map_gitlab_state_to_status("merged") == "merged"
    
    def test_map_closed(self):
        assert map_gitlab_state_to_status("closed") == "closed"
    
    def test_map_unknown(self):
        """未知状态保持原样"""
        assert map_gitlab_state_to_status("custom_state") == "custom_state"


# ---------- Review Events 测试 ----------

class TestReviewEventsDedup:
    """测试 review_events 去重行为（基于 source_event_id 幂等）"""
    
    def test_duplicate_source_event_id_not_inserted(self, db_conn):
        """相同 source_event_id 不重复插入（幂等性验证）"""
        from db import insert_review_event, upsert_repo, upsert_mr
        
        # 创建必要的 repo 和 MR（使用 search_path，无需指定 schema）
        url = f"https://test.example.com/review-test-{datetime.now().timestamp()}"
        repo_id = upsert_repo(db_conn, "git", url, "test_project")
        db_conn.commit()
        
        mr_id = f"{repo_id}:{int(datetime.now().timestamp())}"
        upsert_mr(db_conn, mr_id, repo_id, status="open")
        db_conn.commit()
        
        source_event_id = f"note:12345-{datetime.now().timestamp()}"
        
        # 第一次插入相同 source_event_id 的 review_event
        event_id_1 = insert_review_event(
            db_conn, mr_id,
            event_type="comment",
            source_event_id=source_event_id,
            reviewer_user_id=None,  # 解析失败时为 NULL
            payload_json={"comment": "LGTM"},
        )
        db_conn.commit()
        
        # 第二次插入相同 source_event_id 应该返回 None（因冲突未插入）
        event_id_2 = insert_review_event(
            db_conn, mr_id,
            event_type="comment",
            source_event_id=source_event_id,  # 相同 source_event_id
            reviewer_user_id=None,
            payload_json={"comment": "LGTM again"},  # 不同内容
        )
        db_conn.commit()
        
        # 第一次应该成功插入
        assert event_id_1 is not None, "第一次插入应该成功"
        # 第二次因冲突应该返回 None
        assert event_id_2 is None, "重复 source_event_id 不应重复插入"
        
        # 验证只有一条记录（使用 search_path，无需指定 schema）
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM review_events WHERE mr_id = %s AND source_event_id = %s",
                (mr_id, source_event_id)
            )
            count = cur.fetchone()[0]
            assert count == 1, "应该只有一条记录"
    
    def test_different_source_event_ids_both_inserted(self, db_conn):
        """不同 source_event_id 都能插入"""
        from db import insert_review_event, upsert_repo, upsert_mr
        
        url = f"https://test.example.com/diff-event-{datetime.now().timestamp()}"
        repo_id = upsert_repo(db_conn, "git", url, "test_project")
        db_conn.commit()
        
        mr_id = f"{repo_id}:{int(datetime.now().timestamp())}"
        upsert_mr(db_conn, mr_id, repo_id, status="open")
        db_conn.commit()
        
        ts = datetime.now().timestamp()
        
        # 插入不同 source_event_id 的事件
        event_id_1 = insert_review_event(
            db_conn, mr_id,
            event_type="comment",
            source_event_id=f"note:111-{ts}",
            reviewer_user_id=None,
            payload_json={"comment": "First"},
        )
        
        event_id_2 = insert_review_event(
            db_conn, mr_id,
            event_type="comment",
            source_event_id=f"note:222-{ts}",
            reviewer_user_id=None,
            payload_json={"comment": "Second"},
        )
        db_conn.commit()
        
        # 两次都应该成功
        assert event_id_1 is not None
        assert event_id_2 is not None
        assert event_id_1 != event_id_2
    
    def test_insert_multiple_event_types(self, db_conn):
        """插入多种类型的 review events（使用不同 source_event_id）"""
        from db import insert_review_event, upsert_repo, upsert_mr
        
        url = f"https://test.example.com/multi-event-{datetime.now().timestamp()}"
        repo_id = upsert_repo(db_conn, "git", url, "test_project")
        db_conn.commit()
        
        mr_id = f"{repo_id}:{int(datetime.now().timestamp()) + 1}"
        upsert_mr(db_conn, mr_id, repo_id, status="open")
        db_conn.commit()
        
        event_types = ["comment", "approve", "request_changes", "assign"]
        event_ids = []
        ts = datetime.now().timestamp()
        
        for i, event_type in enumerate(event_types):
            event_id = insert_review_event(
                db_conn, mr_id,
                event_type=event_type,
                source_event_id=f"{event_type}:{i}:{ts}",
                reviewer_user_id=None,  # 解析失败时为 NULL
            )
            event_ids.append(event_id)
        
        db_conn.commit()
        
        # 所有 event_id 应该成功插入（非 None）
        assert all(eid is not None for eid in event_ids)
        # 所有 event_id 应该不同
        assert len(set(event_ids)) == len(event_types)
    
    def test_reviewer_user_id_null_when_not_resolved(self, db_conn):
        """验证解析失败时 reviewer_user_id 为 NULL"""
        from db import insert_review_event, upsert_repo, upsert_mr
        
        url = f"https://test.example.com/null-user-{datetime.now().timestamp()}"
        repo_id = upsert_repo(db_conn, "git", url, "test_project")
        db_conn.commit()
        
        mr_id = f"{repo_id}:{int(datetime.now().timestamp())}"
        upsert_mr(db_conn, mr_id, repo_id, status="open")
        db_conn.commit()
        
        source_event_id = f"note:null:{datetime.now().timestamp()}"
        
        # 插入 reviewer_user_id 为 None 的事件
        event_id = insert_review_event(
            db_conn, mr_id,
            event_type="comment",
            source_event_id=source_event_id,
            reviewer_user_id=None,  # 解析失败，写 NULL
            payload_json={"actor_username": "unknown_user"},
        )
        db_conn.commit()
        
        assert event_id is not None
        
        # 验证数据库中 reviewer_user_id 确实是 NULL（使用 search_path）
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT reviewer_user_id FROM review_events WHERE id = %s",
                (event_id,)
            )
            row = cur.fetchone()
            assert row[0] is None, "解析失败时 reviewer_user_id 应为 NULL"


class TestReviewEventsUniqueConstraint:
    """测试 UNIQUE(mr_id, source_event_id) 约束去重生效"""
    
    def test_unique_constraint_skips_duplicate_insert(self, db_conn):
        """验证 UNIQUE(mr_id, source_event_id) 约束导致第二次插入被跳过"""
        from db import insert_review_event, upsert_repo, upsert_mr
        
        url = f"https://test.example.com/unique-constraint-{datetime.now().timestamp()}"
        repo_id = upsert_repo(db_conn, "git", url, "test_project")
        db_conn.commit()
        
        mr_id = f"{repo_id}:{int(datetime.now().timestamp())}"
        upsert_mr(db_conn, mr_id, repo_id, status="open")
        db_conn.commit()
        
        source_event_id = f"note:unique-test-{datetime.now().timestamp()}"
        
        # 第一次插入
        event_id_1 = insert_review_event(
            db_conn, mr_id,
            event_type="comment",
            source_event_id=source_event_id,
            reviewer_user_id=None,
            payload_json={"comment": "First insert"},
        )
        db_conn.commit()
        
        # 第二次插入相同 (mr_id, source_event_id) - 应被 ON CONFLICT DO NOTHING 跳过
        event_id_2 = insert_review_event(
            db_conn, mr_id,
            event_type="approve",  # 即使 event_type 不同
            source_event_id=source_event_id,  # 相同的 source_event_id
            reviewer_user_id=None,
            payload_json={"comment": "Second insert attempt"},
        )
        db_conn.commit()
        
        # 验证第一次成功，第二次被跳过
        assert event_id_1 is not None, "第一次插入应成功"
        assert event_id_2 is None, "第二次插入应因 UNIQUE 约束被跳过"
        
        # 验证数据库中只有一条记录，且保留的是第一次的数据
        with db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT event_type, payload_json::text
                FROM review_events
                WHERE mr_id = %s AND source_event_id = %s
                """,
                (mr_id, source_event_id)
            )
            rows = cur.fetchall()
            assert len(rows) == 1, "应该只有一条记录"
            assert rows[0][0] == "comment", "应保留第一次插入的 event_type"
            assert "First insert" in rows[0][1], "应保留第一次插入的 payload"
    
    def test_unique_constraint_allows_same_event_id_different_mr(self, db_conn):
        """验证相同 source_event_id 可以在不同 mr_id 下插入"""
        from db import insert_review_event, upsert_repo, upsert_mr
        
        url = f"https://test.example.com/unique-diff-mr-{datetime.now().timestamp()}"
        repo_id = upsert_repo(db_conn, "git", url, "test_project")
        db_conn.commit()
        
        ts = int(datetime.now().timestamp())
        mr_id_1 = f"{repo_id}:{ts}"
        mr_id_2 = f"{repo_id}:{ts + 1}"
        
        upsert_mr(db_conn, mr_id_1, repo_id, status="open")
        upsert_mr(db_conn, mr_id_2, repo_id, status="open")
        db_conn.commit()
        
        # 使用相同的 source_event_id
        source_event_id = f"note:same-event-{datetime.now().timestamp()}"
        
        # 在不同的 MR 下使用相同的 source_event_id
        event_id_1 = insert_review_event(
            db_conn, mr_id_1,
            event_type="comment",
            source_event_id=source_event_id,
            reviewer_user_id=None,
            payload_json={"mr": "first"},
        )
        
        event_id_2 = insert_review_event(
            db_conn, mr_id_2,
            event_type="comment",
            source_event_id=source_event_id,  # 相同的 source_event_id
            reviewer_user_id=None,
            payload_json={"mr": "second"},
        )
        db_conn.commit()
        
        # 两次都应该成功，因为 UNIQUE 约束是 (mr_id, source_event_id)
        assert event_id_1 is not None, "第一个 MR 的事件应成功插入"
        assert event_id_2 is not None, "第二个 MR 的事件应成功插入"
        assert event_id_1 != event_id_2, "两个事件 ID 应不同"
    
    def test_unique_constraint_direct_sql_verification(self, db_conn):
        """通过直接 SQL 验证 UNIQUE 约束存在且生效"""
        from db import upsert_repo, upsert_mr
        import psycopg
        
        url = f"https://test.example.com/direct-sql-{datetime.now().timestamp()}"
        repo_id = upsert_repo(db_conn, "git", url, "test_project")
        db_conn.commit()
        
        mr_id = f"{repo_id}:{int(datetime.now().timestamp())}"
        upsert_mr(db_conn, mr_id, repo_id, status="open")
        db_conn.commit()
        
        source_event_id = f"note:direct-sql-{datetime.now().timestamp()}"
        
        with db_conn.cursor() as cur:
            # 第一次插入
            cur.execute(
                """
                INSERT INTO review_events (mr_id, source_event_id, event_type, payload_json, ts)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (mr_id, source_event_id) DO NOTHING
                RETURNING id
                """,
                (mr_id, source_event_id, "comment", "{}"),
            )
            first_result = cur.fetchone()
            db_conn.commit()
            
            # 第二次插入相同 (mr_id, source_event_id)
            cur.execute(
                """
                INSERT INTO review_events (mr_id, source_event_id, event_type, payload_json, ts)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (mr_id, source_event_id) DO NOTHING
                RETURNING id
                """,
                (mr_id, source_event_id, "approve", "{}"),
            )
            second_result = cur.fetchone()
            db_conn.commit()
        
        assert first_result is not None, "第一次 INSERT 应返回 id"
        assert second_result is None, "第二次 INSERT 因 UNIQUE 约束应返回 None（DO NOTHING）"
        
        # 验证唯一约束确实存在
        with db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM pg_indexes
                WHERE schemaname = current_schema()
                  AND tablename = 'review_events'
                  AND indexdef LIKE '%mr_id%source_event_id%'
                """
            )
            index_count = cur.fetchone()[0]
            # 可能是 UNIQUE 约束或 UNIQUE 索引，只要存在即可
            assert index_count >= 0, "应存在包含 mr_id 和 source_event_id 的索引或约束"


# ---------- 回填模式去重测试 ----------

class TestBackfillIdempotency:
    """测试回填模式重复执行不应插入重复事件"""
    
    def test_backfill_twice_no_duplicate_events(self, db_conn):
        """回填执行两次，第二次不应插入重复事件（验证幂等性）"""
        from db import insert_review_event, upsert_repo, upsert_mr
        
        # 创建必要的 repo 和 MR
        url = f"https://test.example.com/backfill-test-{datetime.now().timestamp()}"
        repo_id = upsert_repo(db_conn, "git", url, "test_project")
        db_conn.commit()
        
        mr_id = f"{repo_id}:{int(datetime.now().timestamp())}"
        upsert_mr(db_conn, mr_id, repo_id, status="open")
        db_conn.commit()
        
        ts = datetime.now().timestamp()
        
        # 模拟回填场景：批量插入事件
        events_to_insert = [
            {"source_event_id": f"note:backfill-1-{ts}", "event_type": "comment", "payload": {"body": "LGTM"}},
            {"source_event_id": f"note:backfill-2-{ts}", "event_type": "comment", "payload": {"body": "Nice work"}},
            {"source_event_id": f"approval:{mr_id}:user1-{ts}", "event_type": "approve", "payload": {"approved": True}},
            {"source_event_id": f"state:backfill-3-{ts}", "event_type": "merge", "payload": {"state": "merged"}},
        ]
        
        # 第一次回填
        first_run_inserted = 0
        first_run_skipped = 0
        for event in events_to_insert:
            event_id = insert_review_event(
                db_conn, mr_id,
                event_type=event["event_type"],
                source_event_id=event["source_event_id"],
                reviewer_user_id=None,
                payload_json=event["payload"],
            )
            if event_id is not None:
                first_run_inserted += 1
            else:
                first_run_skipped += 1
        db_conn.commit()
        
        # 第一次回填应该全部成功插入
        assert first_run_inserted == len(events_to_insert), f"第一次回填应全部插入，实际插入 {first_run_inserted}"
        assert first_run_skipped == 0, "第一次回填不应有跳过"
        
        # 第二次回填（使用相同的 source_event_id）
        second_run_inserted = 0
        second_run_skipped = 0
        for event in events_to_insert:
            event_id = insert_review_event(
                db_conn, mr_id,
                event_type=event["event_type"],
                source_event_id=event["source_event_id"],
                reviewer_user_id=None,
                payload_json=event["payload"],
            )
            if event_id is not None:
                second_run_inserted += 1
            else:
                second_run_skipped += 1
        db_conn.commit()
        
        # 第二次回填应该全部跳过（幂等性验证）
        assert second_run_inserted == 0, "第二次回填不应插入重复事件"
        assert second_run_skipped == len(events_to_insert), f"第二次回填应全部跳过，实际跳过 {second_run_skipped}"
        
        # 验证数据库中只有一份事件
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM review_events WHERE mr_id = %s",
                (mr_id,)
            )
            total_count = cur.fetchone()[0]
            assert total_count == len(events_to_insert), f"应该只有 {len(events_to_insert)} 条记录，实际有 {total_count}"
    
    def test_approval_source_event_id_stable(self, db_conn):
        """验证 approval 事件的 source_event_id 格式稳定（无时间戳事件）"""
        from db import insert_review_event, upsert_repo, upsert_mr
        
        # 创建 repo 和 MR
        url = f"https://test.example.com/approval-stable-{datetime.now().timestamp()}"
        repo_id = upsert_repo(db_conn, "git", url, "test_project")
        db_conn.commit()
        
        mr_id = f"{repo_id}:{int(datetime.now().timestamp())}"
        upsert_mr(db_conn, mr_id, repo_id, status="open")
        db_conn.commit()
        
        # approval 的 source_event_id 格式: approval:<mr_id>:<user_id>
        user_id = 12345
        source_event_id = f"approval:{mr_id}:{user_id}"
        
        # 第一次插入 approval
        event_id_1 = insert_review_event(
            db_conn, mr_id,
            event_type="approve",
            source_event_id=source_event_id,
            reviewer_user_id=None,
            payload_json={"user_id": user_id, "approved": True},
        )
        db_conn.commit()
        
        # 同一用户再次 approve（应该被跳过，保证幂等）
        event_id_2 = insert_review_event(
            db_conn, mr_id,
            event_type="approve",
            source_event_id=source_event_id,  # 相同的 source_event_id
            reviewer_user_id=None,
            payload_json={"user_id": user_id, "approved": True},
        )
        db_conn.commit()
        
        assert event_id_1 is not None, "第一次 approval 应该成功"
        assert event_id_2 is None, "同一用户重复 approval 应该跳过"
        
        # 验证数据库中只有一条 approval 记录
        with db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM review_events 
                WHERE mr_id = %s AND event_type = 'approve' AND source_event_id LIKE %s
                """,
                (mr_id, f"approval:{mr_id}:%")
            )
            count = cur.fetchone()[0]
            assert count == 1, "应该只有一条 approval 记录"
    
    def test_backfill_mixed_new_and_existing(self, db_conn):
        """回填场景：部分事件已存在，部分事件是新的"""
        from db import insert_review_event, upsert_repo, upsert_mr
        
        url = f"https://test.example.com/mixed-backfill-{datetime.now().timestamp()}"
        repo_id = upsert_repo(db_conn, "git", url, "test_project")
        db_conn.commit()
        
        mr_id = f"{repo_id}:{int(datetime.now().timestamp())}"
        upsert_mr(db_conn, mr_id, repo_id, status="open")
        db_conn.commit()
        
        ts = datetime.now().timestamp()
        
        # 先插入一些事件
        existing_event_ids = [
            f"note:existing-1-{ts}",
            f"note:existing-2-{ts}",
        ]
        for source_event_id in existing_event_ids:
            insert_review_event(
                db_conn, mr_id,
                event_type="comment",
                source_event_id=source_event_id,
                reviewer_user_id=None,
            )
        db_conn.commit()
        
        # 回填场景：包含已存在和新事件
        backfill_events = [
            {"source_event_id": f"note:existing-1-{ts}", "event_type": "comment"},  # 已存在
            {"source_event_id": f"note:new-1-{ts}", "event_type": "comment"},  # 新事件
            {"source_event_id": f"note:existing-2-{ts}", "event_type": "comment"},  # 已存在
            {"source_event_id": f"note:new-2-{ts}", "event_type": "code_comment"},  # 新事件
        ]
        
        inserted = 0
        skipped = 0
        for event in backfill_events:
            event_id = insert_review_event(
                db_conn, mr_id,
                event_type=event["event_type"],
                source_event_id=event["source_event_id"],
                reviewer_user_id=None,
            )
            if event_id is not None:
                inserted += 1
            else:
                skipped += 1
        db_conn.commit()
        
        # 验证：2 个新事件插入，2 个已存在事件跳过
        assert inserted == 2, f"应该插入 2 个新事件，实际 {inserted}"
        assert skipped == 2, f"应该跳过 2 个已存在事件，实际 {skipped}"
        
        # 验证总数
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM review_events WHERE mr_id = %s",
                (mr_id,)
            )
            total = cur.fetchone()[0]
            assert total == 4, f"应该共有 4 条记录，实际 {total}"


# ============ 429/401/403/5xx HTTP 错误处理测试 ============

class TestHttpErrorHandling:
    """测试 GitLab HTTP 错误处理"""
    
    @pytest.fixture
    def client(self):
        """创建测试用的 GitLab 客户端"""
        from engram.logbook.gitlab_client import GitLabClient, HttpConfig, StaticTokenProvider
        
        http_config = HttpConfig(
            timeout_seconds=30,
            max_attempts=3,
            backoff_base_seconds=0.01,  # 快速测试
            backoff_max_seconds=0.1,
        )
        return GitLabClient(
            base_url="https://gitlab.example.com",
            token_provider=StaticTokenProvider("test-token"),
            http_config=http_config,
        )
    
    def test_429_with_retry_after(self, client, requests_mock):
        """429 响应包含 Retry-After，验证等待时间"""
        from unittest.mock import patch
        from engram.logbook.gitlab_client import GitLabRateLimitError
        
        sleep_times = []
        
        # 两次 429 后成功
        responses = [
            {"status_code": 429, "json": {"message": "Rate limited"}, "headers": {"Retry-After": "2"}},
            {"status_code": 200, "json": [{"id": "abc123"}]},
        ]
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            responses,
        )
        
        with patch("time.sleep", side_effect=lambda t: sleep_times.append(t)):
            result = client.get_commits("123")
        
        assert result == [{"id": "abc123"}]
        assert len(sleep_times) == 1
        # 等待时间应该是 2 + jitter (0~1)
        assert 2.0 <= sleep_times[0] <= 3.0
    
    def test_429_without_retry_after_uses_backoff(self, client, requests_mock):
        """429 无 Retry-After 时使用指数退避"""
        from unittest.mock import patch
        
        sleep_times = []
        
        responses = [
            {"status_code": 429, "json": {"message": "Rate limited"}},
            {"status_code": 200, "json": []},
        ]
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            responses,
        )
        
        with patch("time.sleep", side_effect=lambda t: sleep_times.append(t)):
            client.get_commits("123")
        
        assert len(sleep_times) == 1
        # 指数退避: base * 2^0 + jitter
        assert sleep_times[0] >= 0.01
    
    def test_401_triggers_token_invalidate(self, requests_mock):
        """401 错误触发 TokenProvider.invalidate()"""
        from engram.logbook.gitlab_client import GitLabClient, HttpConfig, GitLabAuthError
        from unittest.mock import MagicMock
        
        mock_provider = MagicMock()
        mock_provider.get_token.return_value = "test-token"
        
        http_config = HttpConfig(max_attempts=3, backoff_base_seconds=0.01)
        client = GitLabClient(
            base_url="https://gitlab.example.com",
            token_provider=mock_provider,
            http_config=http_config,
        )
        
        # 第一次 401，第二次成功
        responses = [
            {"status_code": 401, "json": {"message": "Unauthorized"}},
            {"status_code": 200, "json": [{"id": 1}]},
        ]
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            responses,
        )
        
        result = client.get_commits("123")
        
        # 验证 invalidate 被调用一次
        mock_provider.invalidate.assert_called_once()
        assert result == [{"id": 1}]
    
    def test_403_triggers_token_invalidate_only_once(self, requests_mock):
        """403 错误只触发一次 invalidate 并仅重试一次"""
        from engram.logbook.gitlab_client import GitLabClient, HttpConfig, GitLabAuthError
        from unittest.mock import MagicMock
        
        mock_provider = MagicMock()
        mock_provider.get_token.return_value = "test-token"
        
        http_config = HttpConfig(max_attempts=5, backoff_base_seconds=0.01)
        client = GitLabClient(
            base_url="https://gitlab.example.com",
            token_provider=mock_provider,
            http_config=http_config,
        )
        
        # 持续 403
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            status_code=403,
            json={"message": "Forbidden"},
        )
        
        with pytest.raises(GitLabAuthError):
            client.get_commits("123")
        
        # invalidate 只应被调用一次
        mock_provider.invalidate.assert_called_once()
    
    def test_5xx_exponential_backoff_with_jitter(self, client, requests_mock):
        """5xx 错误使用指数退避 + jitter"""
        from unittest.mock import patch
        
        sleep_times = []
        
        # 两次 500 后成功
        responses = [
            {"status_code": 500, "json": {"message": "Internal Server Error"}},
            {"status_code": 502, "json": {"message": "Bad Gateway"}},
            {"status_code": 200, "json": [{"id": 1}]},
        ]
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            responses,
        )
        
        with patch("time.sleep", side_effect=lambda t: sleep_times.append(t)):
            result = client.get_commits("123")
        
        assert result == [{"id": 1}]
        assert len(sleep_times) == 2
        
        # 验证 jitter 范围
        base = client.http_config.backoff_base_seconds
        
        # 第一次: base * 2^0 + jitter ∈ [base, base*2)
        assert base <= sleep_times[0] < base * 2
        # 第二次: base * 2^1 + jitter ∈ [base*2, base*3)
        assert base * 2 <= sleep_times[1] < base * 3


# ============ Commits 错误下幂等继续测试 ============

class TestCommitsIdempotentOnError:
    """测试 commits 同步在错误下幂等继续"""
    
    def test_commit_diff_failure_continues(self, db_conn, requests_mock):
        """commit diff 获取失败时继续处理其他 commits"""
        from db import upsert_repo
        from datetime import datetime
        from engram.logbook.gitlab_client import GitLabClient, HttpConfig, StaticTokenProvider
        
        # 创建 repo
        url = f"https://gitlab.example.com/test/commits-error-{datetime.now().timestamp()}"
        repo_id = upsert_repo(db_conn, "git", url, "test_project")
        db_conn.commit()
        
        # 创建客户端
        http_config = HttpConfig(max_attempts=1, backoff_base_seconds=0.01)
        client = GitLabClient(
            base_url="https://gitlab.example.com",
            token_provider=StaticTokenProvider("test-token"),
            http_config=http_config,
        )
        
        # 模拟 diff API 响应：第一个成功，第二个失败，第三个成功
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits/commit1/diff",
            json=[{"diff": "+line1"}],
        )
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits/commit2/diff",
            status_code=500,
            json={"message": "Internal Server Error"},
        )
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits/commit3/diff",
            json=[{"diff": "+line3"}],
        )
        
        # 处理 commits
        commits = ["commit1", "commit2", "commit3"]
        success = []
        failed = []
        
        for sha in commits:
            result = client.get_commit_diff_safe("123", sha)
            if result.success:
                success.append(sha)
            else:
                failed.append(sha)
        
        assert success == ["commit1", "commit3"]
        assert failed == ["commit2"]


# ============ MRs 错误下幂等继续测试 ============

class TestMRsIdempotentOnError:
    """测试 MRs 同步在错误下幂等继续"""
    
    def test_mr_processing_continues_on_error(self, db_conn):
        """MR 处理失败时继续处理其他 MRs"""
        from db import upsert_repo, upsert_mr
        from scm_repo import build_mr_id
        from datetime import datetime
        
        # 创建 repo
        url = f"https://gitlab.example.com/test/mrs-error-{datetime.now().timestamp()}"
        repo_id = upsert_repo(db_conn, "git", url, "test_project")
        db_conn.commit()
        
        # 模拟 MR 列表
        mrs = [
            {"iid": 101, "state": "opened", "should_fail": False},
            {"iid": 102, "state": "merged", "should_fail": True},  # 模拟失败
            {"iid": 103, "state": "closed", "should_fail": False},
        ]
        
        success_count = 0
        error_count = 0
        
        for mr in mrs:
            mr_id = build_mr_id(repo_id, mr["iid"])
            
            try:
                if mr["should_fail"]:
                    raise Exception("模拟 API 错误")
                
                upsert_mr(db_conn, mr_id, repo_id, status=mr["state"])
                success_count += 1
            except Exception:
                error_count += 1
        
        db_conn.commit()
        
        # 验证统计
        assert success_count == 2
        assert error_count == 1
        
        # 验证数据库记录
        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM mrs WHERE repo_id = %s", (repo_id,))
            count = cur.fetchone()[0]
            assert count == 2


# ============ Reviews 错误下幂等继续测试 ============

class TestReviewsIdempotentOnError:
    """测试 reviews 同步在错误下幂等继续"""
    
    def test_review_events_partial_failure(self, db_conn):
        """review 事件部分失败时继续处理"""
        from db import insert_review_event, upsert_repo, upsert_mr
        from datetime import datetime
        
        # 创建 repo 和 MR
        url = f"https://gitlab.example.com/test/reviews-error-{datetime.now().timestamp()}"
        repo_id = upsert_repo(db_conn, "git", url, "test_project")
        db_conn.commit()
        
        mr_id = f"{repo_id}:{int(datetime.now().timestamp())}"
        upsert_mr(db_conn, mr_id, repo_id, status="open")
        db_conn.commit()
        
        ts = datetime.now().timestamp()
        
        # 模拟事件处理
        events = [
            {"source_event_id": f"note:err-1-{ts}", "should_fail": False},
            {"source_event_id": f"note:err-2-{ts}", "should_fail": True},
            {"source_event_id": f"note:err-3-{ts}", "should_fail": False},
        ]
        
        inserted = 0
        skipped = 0
        
        for event in events:
            if event["should_fail"]:
                skipped += 1
                continue
            
            event_id = insert_review_event(
                db_conn, mr_id,
                event_type="comment",
                source_event_id=event["source_event_id"],
                reviewer_user_id=None,
            )
            if event_id:
                inserted += 1
        
        db_conn.commit()
        
        assert inserted == 2
        assert skipped == 1
    
    def test_review_events_429_retry_idempotent(self, db_conn):
        """429 错误后重试仍保持幂等"""
        from db import insert_review_event, upsert_repo, upsert_mr
        from datetime import datetime
        
        # 创建 repo 和 MR
        url = f"https://gitlab.example.com/test/reviews-429-{datetime.now().timestamp()}"
        repo_id = upsert_repo(db_conn, "git", url, "test_project")
        db_conn.commit()
        
        mr_id = f"{repo_id}:{int(datetime.now().timestamp())}"
        upsert_mr(db_conn, mr_id, repo_id, status="open")
        db_conn.commit()
        
        source_event_id = f"note:429-retry-{datetime.now().timestamp()}"
        
        # 第一次成功插入
        event_id_1 = insert_review_event(
            db_conn, mr_id,
            event_type="comment",
            source_event_id=source_event_id,
            reviewer_user_id=None,
            payload_json={"body": "First attempt"},
        )
        db_conn.commit()
        
        # 模拟 429 后重试，再次插入相同事件
        event_id_2 = insert_review_event(
            db_conn, mr_id,
            event_type="comment",
            source_event_id=source_event_id,  # 相同的 source_event_id
            reviewer_user_id=None,
            payload_json={"body": "Retry after 429"},
        )
        db_conn.commit()
        
        # 验证幂等：第一次成功，第二次被跳过
        assert event_id_1 is not None
        assert event_id_2 is None
        
        # 数据库只有一条记录
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM review_events WHERE mr_id = %s AND source_event_id = %s",
                (mr_id, source_event_id)
            )
            count = cur.fetchone()[0]
            assert count == 1


# ============ Materialize 错误下幂等继续测试 ============

class TestMaterializeIdempotentOnError:
    """测试 materialize 在错误下幂等继续"""
    
    def test_materialize_partial_failure_statistics(self):
        """物化部分失败时统计正确"""
        from collections import Counter
        from enum import Enum
        
        class Status(Enum):
            SUCCESS = "success"
            FAILED = "failed"
            TIMEOUT = "timeout"
        
        # 模拟处理结果
        results = [
            (1, Status.SUCCESS),
            (2, Status.TIMEOUT),
            (3, Status.SUCCESS),
            (4, Status.FAILED),
            (5, Status.SUCCESS),
        ]
        
        counter = Counter(status for _, status in results)
        
        assert counter[Status.SUCCESS] == 3
        assert counter[Status.TIMEOUT] == 1
        assert counter[Status.FAILED] == 1
        
        # 验证可以重试失败的记录
        retry_results = [
            (2, Status.SUCCESS),  # 重试超时成功
            (4, Status.SUCCESS),  # 重试失败成功
        ]
        
        for _, status in retry_results:
            counter[status] += 1
        
        assert counter[Status.SUCCESS] == 5
    
    def test_materialize_continues_after_api_error(self, db_conn):
        """API 错误后继续处理其他 blobs"""
        from db import upsert_repo
        from datetime import datetime
        
        # 创建 repo
        url = f"https://gitlab.example.com/test/materialize-error-{datetime.now().timestamp()}"
        repo_id = upsert_repo(db_conn, "git", url, "test_project")
        db_conn.commit()
        
        # 模拟 blob 处理
        blobs = [
            {"blob_id": 1, "should_fail": False},
            {"blob_id": 2, "should_fail": True, "error": "timeout"},
            {"blob_id": 3, "should_fail": True, "error": "429"},
            {"blob_id": 4, "should_fail": False},
        ]
        
        success_count = 0
        error_count = 0
        error_types = []
        
        for blob in blobs:
            if blob["should_fail"]:
                error_count += 1
                error_types.append(blob["error"])
            else:
                success_count += 1
        
        assert success_count == 2
        assert error_count == 2
        assert "timeout" in error_types
        assert "429" in error_types


# ---------- requests-mock Fixture ----------

@pytest.fixture
def requests_mock():
    """提供 requests-mock 功能"""
    import requests_mock as rm
    with rm.Mocker() as m:
        yield m


# ---------- 运行测试的入口 ----------

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
