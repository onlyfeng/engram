# -*- coding: utf-8 -*-
"""
test_gitlab_commit_cursor_tie_break.py - GitLab Commit 游标 tie-break 测试

验证:
1. _deduplicate_commits 按 (ts, sha) 排序和过滤
2. should_advance_gitlab_commit_cursor 复合水位线逻辑
3. 同秒内多个 commit 的稳定处理顺序
4. 游标推进条件的边界情况
"""

from datetime import datetime, timezone, timedelta
from typing import Optional

import pytest

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scm_sync_gitlab_commits import (
    GitCommit,
    _deduplicate_commits,
    _get_commit_timestamp,
    _get_commit_sort_key,
)
from engram.logbook.cursor import should_advance_gitlab_commit_cursor


class TestGetCommitSortKey:
    """测试 _get_commit_sort_key 函数"""

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
            stats={},
        )

    def test_sort_key_includes_ts_and_sha(self):
        """排序键应包含 (ts, sha)"""
        ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        commit = self._make_commit("abc123", committed_date=ts)
        
        key = _get_commit_sort_key(commit)
        
        assert isinstance(key, tuple)
        assert len(key) == 2
        assert key[0] == ts
        assert key[1] == "abc123"

    def test_sort_key_ordering_by_ts_first(self):
        """排序键优先按 ts 排序"""
        ts1 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        ts2 = datetime(2024, 1, 1, 13, 0, 0, tzinfo=timezone.utc)
        
        commit1 = self._make_commit("zzz999", committed_date=ts1)  # 早时间，大 sha
        commit2 = self._make_commit("aaa111", committed_date=ts2)  # 晚时间，小 sha
        
        key1 = _get_commit_sort_key(commit1)
        key2 = _get_commit_sort_key(commit2)
        
        # ts1 < ts2，所以 key1 < key2（忽略 sha 大小）
        assert key1 < key2

    def test_sort_key_ordering_by_sha_when_ts_equal(self):
        """ts 相等时按 sha 排序"""
        ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        
        commit_a = self._make_commit("aaa111", committed_date=ts)
        commit_b = self._make_commit("bbb222", committed_date=ts)
        commit_c = self._make_commit("ccc333", committed_date=ts)
        
        key_a = _get_commit_sort_key(commit_a)
        key_b = _get_commit_sort_key(commit_b)
        key_c = _get_commit_sort_key(commit_c)
        
        assert key_a < key_b < key_c


class TestDeduplicateCommitsTieBreak:
    """测试 _deduplicate_commits 的 (ts, sha) tie-break 逻辑"""

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
            stats={},
        )

    def test_sorting_by_ts_sha_ascending(self):
        """测试按 (ts, sha) 升序排序"""
        ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        
        # 同一秒内的 3 个 commit，故意乱序输入
        commits = [
            self._make_commit("ccc333", committed_date=ts),
            self._make_commit("aaa111", committed_date=ts),
            self._make_commit("bbb222", committed_date=ts),
        ]
        
        result = _deduplicate_commits(commits)
        
        assert len(result) == 3
        # 应按 sha 字典序排列
        assert result[0].sha == "aaa111"
        assert result[1].sha == "bbb222"
        assert result[2].sha == "ccc333"

    def test_sorting_mixed_ts_and_sha(self):
        """测试混合 ts 和 sha 的排序"""
        ts1 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        ts2 = datetime(2024, 1, 1, 13, 0, 0, tzinfo=timezone.utc)
        
        commits = [
            self._make_commit("zzz999", committed_date=ts1),  # 早时间，大 sha
            self._make_commit("aaa111", committed_date=ts2),  # 晚时间，小 sha
            self._make_commit("bbb222", committed_date=ts1),  # 早时间，中 sha
        ]
        
        result = _deduplicate_commits(commits)
        
        assert len(result) == 3
        # 先按 ts 排序，ts 相同时按 sha
        assert result[0].sha == "bbb222"  # ts1, bbb
        assert result[1].sha == "zzz999"  # ts1, zzz
        assert result[2].sha == "aaa111"  # ts2

    def test_filter_by_cursor_ts_sha_boundary(self):
        """测试按 (ts, sha) 复合水位线过滤"""
        ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        
        commits = [
            self._make_commit("aaa111", committed_date=ts),
            self._make_commit("bbb222", committed_date=ts),
            self._make_commit("ccc333", committed_date=ts),
        ]
        
        # cursor_sha = "bbb222" 表示已处理到 (ts, bbb222)
        # 应跳过 sha <= "bbb222" 的记录
        result = _deduplicate_commits(commits, cursor_sha="bbb222", cursor_ts=ts)
        
        assert len(result) == 1
        assert result[0].sha == "ccc333"

    def test_filter_cursor_sha_equal_is_skipped(self):
        """cursor_sha 完全匹配时应跳过"""
        ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        
        commits = [
            self._make_commit("exact_match", committed_date=ts),
            self._make_commit("new_commit", committed_date=ts + timedelta(hours=1)),
        ]
        
        result = _deduplicate_commits(commits, cursor_sha="exact_match", cursor_ts=ts)
        
        assert len(result) == 1
        assert result[0].sha == "new_commit"

    def test_filter_sha_less_than_cursor_is_skipped(self):
        """sha < cursor_sha 且 ts == cursor_ts 时应跳过"""
        ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        
        commits = [
            self._make_commit("aaa111", committed_date=ts),  # sha < cursor_sha
            self._make_commit("bbb222", committed_date=ts),  # sha = cursor_sha
            self._make_commit("ccc333", committed_date=ts),  # sha > cursor_sha
        ]
        
        result = _deduplicate_commits(commits, cursor_sha="bbb222", cursor_ts=ts)
        
        # 只保留 sha > cursor_sha 的
        assert len(result) == 1
        assert result[0].sha == "ccc333"

    def test_filter_ts_less_than_cursor_all_skipped(self):
        """ts < cursor_ts 的全部跳过，不论 sha"""
        cursor_ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        old_ts = datetime(2024, 1, 1, 11, 0, 0, tzinfo=timezone.utc)
        
        commits = [
            self._make_commit("zzz999", committed_date=old_ts),  # 大 sha 但旧 ts
            self._make_commit("aaa111", committed_date=old_ts),  # 小 sha 旧 ts
        ]
        
        result = _deduplicate_commits(commits, cursor_sha="bbb222", cursor_ts=cursor_ts)
        
        # 全部跳过
        assert len(result) == 0

    def test_filter_ts_greater_than_cursor_all_kept(self):
        """ts > cursor_ts 的全部保留，不论 sha"""
        cursor_ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        new_ts = datetime(2024, 1, 1, 13, 0, 0, tzinfo=timezone.utc)
        
        commits = [
            self._make_commit("aaa111", committed_date=new_ts),  # 小 sha 但新 ts
            self._make_commit("zzz999", committed_date=new_ts),  # 大 sha 新 ts
        ]
        
        result = _deduplicate_commits(commits, cursor_sha="mmm555", cursor_ts=cursor_ts)
        
        # 全部保留
        assert len(result) == 2
        # 按 sha 排序
        assert result[0].sha == "aaa111"
        assert result[1].sha == "zzz999"

    def test_no_cursor_ts_only_sha_match(self):
        """仅有 cursor_sha 无 cursor_ts 时，只跳过完全匹配的 sha"""
        ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        
        commits = [
            self._make_commit("aaa111", committed_date=ts),
            self._make_commit("bbb222", committed_date=ts),  # 完全匹配
            self._make_commit("ccc333", committed_date=ts),
        ]
        
        # 仅 cursor_sha，无 cursor_ts
        result = _deduplicate_commits(commits, cursor_sha="bbb222", cursor_ts=None)
        
        # 只跳过完全匹配的 bbb222
        assert len(result) == 2
        assert result[0].sha == "aaa111"
        assert result[1].sha == "ccc333"

    def test_stable_order_for_batch_processing(self):
        """验证批量处理时的稳定顺序"""
        ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        
        # 模拟多次运行，每次输入顺序不同
        commits_run1 = [
            self._make_commit("sha_c", committed_date=ts),
            self._make_commit("sha_a", committed_date=ts),
            self._make_commit("sha_b", committed_date=ts),
        ]
        commits_run2 = [
            self._make_commit("sha_b", committed_date=ts),
            self._make_commit("sha_c", committed_date=ts),
            self._make_commit("sha_a", committed_date=ts),
        ]
        
        result1 = _deduplicate_commits(commits_run1)
        result2 = _deduplicate_commits(commits_run2)
        
        # 输出顺序应完全一致
        assert [c.sha for c in result1] == [c.sha for c in result2]
        assert [c.sha for c in result1] == ["sha_a", "sha_b", "sha_c"]


class TestShouldAdvanceGitlabCommitCursor:
    """测试 should_advance_gitlab_commit_cursor 函数"""

    def test_first_sync_always_advances(self):
        """首次同步总是推进"""
        assert should_advance_gitlab_commit_cursor(
            "2024-01-15T12:00:00Z", "abc123", None, None
        ) is True

    def test_newer_ts_advances(self):
        """时间戳更新时推进"""
        assert should_advance_gitlab_commit_cursor(
            "2024-01-15T13:00:00Z", "abc123",
            "2024-01-15T12:00:00Z", "xyz789",
        ) is True

    def test_older_ts_does_not_advance(self):
        """时间戳更旧时不推进"""
        assert should_advance_gitlab_commit_cursor(
            "2024-01-15T11:00:00Z", "abc123",
            "2024-01-15T12:00:00Z", "xyz789",
        ) is False

    def test_same_ts_higher_sha_advances(self):
        """时间戳相同、sha 更大时推进"""
        assert should_advance_gitlab_commit_cursor(
            "2024-01-15T12:00:00Z", "bbb222",
            "2024-01-15T12:00:00Z", "aaa111",
        ) is True

    def test_same_ts_lower_sha_does_not_advance(self):
        """时间戳相同、sha 更小时不推进"""
        assert should_advance_gitlab_commit_cursor(
            "2024-01-15T12:00:00Z", "aaa111",
            "2024-01-15T12:00:00Z", "bbb222",
        ) is False

    def test_same_ts_same_sha_does_not_advance(self):
        """时间戳和 sha 都相同时不推进"""
        assert should_advance_gitlab_commit_cursor(
            "2024-01-15T12:00:00Z", "abc123",
            "2024-01-15T12:00:00Z", "abc123",
        ) is False

    def test_none_last_sha_always_advances_if_ts_same(self):
        """旧 sha 为 None 时，时间戳相同则推进"""
        assert should_advance_gitlab_commit_cursor(
            "2024-01-15T12:00:00Z", "abc123",
            "2024-01-15T12:00:00Z", None,
        ) is True


class TestCommitCursorTieBreakIntegration:
    """集成测试：验证 tie-break 在完整同步流程中的行为"""

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
            stats={},
        )

    def test_same_second_multiple_commits_cursor_advances(self):
        """同一秒内多个 commit，游标应推进到最大 sha"""
        ts = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        ts_str = "2024-01-15T12:00:00+00:00"
        
        commits = [
            self._make_commit("sha_a", committed_date=ts),
            self._make_commit("sha_b", committed_date=ts),
            self._make_commit("sha_c", committed_date=ts),
        ]
        
        # 去重排序后
        sorted_commits = _deduplicate_commits(commits)
        
        # 最后一个是 sha_c
        last = sorted_commits[-1]
        assert last.sha == "sha_c"
        
        # 模拟游标推进
        last_ts = None
        last_sha = None
        
        for commit in sorted_commits:
            commit_ts_str = _get_commit_timestamp(commit).isoformat()
            if should_advance_gitlab_commit_cursor(
                commit_ts_str, commit.sha, last_ts, last_sha
            ):
                last_ts = commit_ts_str
                last_sha = commit.sha
        
        # 最终游标应该是 sha_c
        assert last_sha == "sha_c"

    def test_batch_processing_with_overlap(self):
        """模拟 overlap 回退后批量处理"""
        ts = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        
        # 第一次同步：处理 sha_a, sha_b, sha_c（同秒）
        first_batch = [
            self._make_commit("sha_a", committed_date=ts),
            self._make_commit("sha_b", committed_date=ts),
            self._make_commit("sha_c", committed_date=ts),
        ]
        
        result1 = _deduplicate_commits(first_batch)
        assert len(result1) == 3
        
        # 第一次同步后游标: (ts, sha_c)
        cursor_ts = ts
        cursor_sha = "sha_c"
        
        # 第二次同步（overlap 导致 sha_b, sha_c 重复出现，加上新的 sha_d, sha_e）
        second_batch = [
            self._make_commit("sha_b", committed_date=ts),  # 重复
            self._make_commit("sha_c", committed_date=ts),  # 重复
            self._make_commit("sha_d", committed_date=ts),  # 新
            self._make_commit("sha_e", committed_date=ts + timedelta(seconds=1)),  # 新，不同秒
        ]
        
        result2 = _deduplicate_commits(second_batch, cursor_sha=cursor_sha, cursor_ts=cursor_ts)
        
        # 只应保留新的
        assert len(result2) == 2
        assert result2[0].sha == "sha_d"  # 同秒但 sha > cursor_sha
        assert result2[1].sha == "sha_e"  # 新秒

    def test_repeat_execution_no_duplicate_processing(self):
        """重复执行时不会重复处理"""
        ts = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        
        commits = [
            self._make_commit("sha_a", committed_date=ts),
            self._make_commit("sha_b", committed_date=ts),
            self._make_commit("sha_c", committed_date=ts),
        ]
        
        # 模拟第一次执行
        processed_first = set()
        last_ts = None
        last_sha = None
        
        for commit in _deduplicate_commits(commits):
            processed_first.add(commit.sha)
            commit_ts_str = _get_commit_timestamp(commit).isoformat()
            if should_advance_gitlab_commit_cursor(
                commit_ts_str, commit.sha, last_ts, last_sha
            ):
                last_ts = commit_ts_str
                last_sha = commit.sha
        
        assert processed_first == {"sha_a", "sha_b", "sha_c"}
        assert last_sha == "sha_c"
        
        # 模拟第二次执行（相同数据）
        cursor_ts = datetime.fromisoformat(last_ts.replace("Z", "+00:00")) if last_ts else None
        result2 = _deduplicate_commits(commits, cursor_sha=last_sha, cursor_ts=cursor_ts)
        
        # 不应有新的 commit
        assert len(result2) == 0


class TestEdgeCases:
    """边界情况测试"""

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
            stats={},
        )

    def test_sha_lexicographic_order(self):
        """SHA 应按字典序比较"""
        # 数字和字母混合的 SHA
        assert "1a2b3c" < "abc123"  # 数字在字母前
        assert "ABC123" < "abc123"  # 大写在小写前
        
        # 验证游标函数遵循同样规则
        assert should_advance_gitlab_commit_cursor(
            "2024-01-15T12:00:00Z", "abc123",
            "2024-01-15T12:00:00Z", "1a2b3c",
        ) is True

    def test_empty_commits_list(self):
        """空列表处理"""
        result = _deduplicate_commits([])
        assert result == []

    def test_single_commit(self):
        """单个 commit"""
        ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        commits = [self._make_commit("only_one", committed_date=ts)]
        
        result = _deduplicate_commits(commits)
        
        assert len(result) == 1
        assert result[0].sha == "only_one"

    def test_single_commit_filtered(self):
        """单个 commit 被过滤"""
        ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        commits = [self._make_commit("old_commit", committed_date=ts)]
        
        result = _deduplicate_commits(commits, cursor_sha="old_commit", cursor_ts=ts)
        
        assert len(result) == 0

    def test_timezone_aware_comparison(self):
        """时区感知的比较"""
        # 不同时区表示的同一时刻
        ts_utc = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        
        commits = [self._make_commit("commit1", committed_date=ts_utc)]
        
        # cursor_ts 没有时区信息（应自动假定 UTC）
        cursor_ts_naive = datetime(2024, 1, 1, 12, 0, 0)
        
        result = _deduplicate_commits(commits, cursor_sha="commit1", cursor_ts=cursor_ts_naive)
        
        # 应被过滤（等价时间戳 + 相同 sha）
        assert len(result) == 0


class TestTimezoneFormatEquivalence:
    """测试 Z 与 +00:00 格式的等价性"""

    def test_z_and_offset_formats_are_equal(self):
        """Z 与 +00:00 格式应视为相等"""
        ts_z = "2024-01-15T12:00:00Z"
        ts_offset = "2024-01-15T12:00:00+00:00"
        
        # 相同时间、相同 sha：不应推进
        assert should_advance_gitlab_commit_cursor(ts_z, "abc123", ts_offset, "abc123") is False
        assert should_advance_gitlab_commit_cursor(ts_offset, "abc123", ts_z, "abc123") is False

    def test_z_and_offset_with_different_sha(self):
        """相同时间（不同格式）、不同 sha 按 sha 比较"""
        ts_z = "2024-01-15T12:00:00Z"
        ts_offset = "2024-01-15T12:00:00+00:00"
        
        # sha 更大时应推进
        assert should_advance_gitlab_commit_cursor(ts_z, "bbb222", ts_offset, "aaa111") is True
        assert should_advance_gitlab_commit_cursor(ts_offset, "bbb222", ts_z, "aaa111") is True
        
        # sha 更小时不应推进
        assert should_advance_gitlab_commit_cursor(ts_z, "aaa111", ts_offset, "bbb222") is False
        assert should_advance_gitlab_commit_cursor(ts_offset, "aaa111", ts_z, "bbb222") is False

    def test_mixed_format_cursor_storage(self):
        """模拟游标存储为 Z 格式，新值为 +00:00 格式的场景"""
        # 游标存储的是 Z 格式
        cursor_ts = "2024-01-15T12:00:00Z"
        cursor_sha = "abc123"
        
        # 新值使用 +00:00 格式
        new_ts_same = "2024-01-15T12:00:00+00:00"  # 等价时间
        new_ts_later = "2024-01-15T12:00:01+00:00"  # 晚 1 秒
        
        # 等价时间 + 相同 sha：不推进
        assert should_advance_gitlab_commit_cursor(new_ts_same, cursor_sha, cursor_ts, cursor_sha) is False
        
        # 等价时间 + 不同 sha：按 sha 比较
        assert should_advance_gitlab_commit_cursor(new_ts_same, "def456", cursor_ts, cursor_sha) is True
        
        # 晚 1 秒：总是推进
        assert should_advance_gitlab_commit_cursor(new_ts_later, cursor_sha, cursor_ts, cursor_sha) is True

    def test_microseconds_preserved_in_comparison(self):
        """微秒精度在比较中正确处理"""
        ts1 = "2024-01-15T12:00:00.123456Z"
        ts2 = "2024-01-15T12:00:00.123456+00:00"  # 等价
        ts3 = "2024-01-15T12:00:00.123457Z"  # 晚 1 微秒
        
        # 等价时间
        assert should_advance_gitlab_commit_cursor(ts1, "abc", ts2, "abc") is False
        
        # 晚 1 微秒应推进
        assert should_advance_gitlab_commit_cursor(ts3, "abc", ts1, "abc") is True


# ---------- 运行测试的入口 ----------

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
