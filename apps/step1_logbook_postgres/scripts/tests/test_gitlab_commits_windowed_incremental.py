# -*- coding: utf-8 -*-
"""
test_gitlab_commits_windowed_incremental.py - GitLab Commits 窗口化增量同步测试

验证:
1. compute_commit_fetch_window 函数的时间窗口计算
2. select_next_batch 函数的批次选择逻辑
3. AdaptiveWindowState 自适应窗口策略
4. 多轮同步覆盖全部 commits 且无重复
5. 使用 mock GitLabClient 模拟"最新优先分页"的 commits
"""

from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from unittest.mock import Mock, MagicMock, patch

import pytest

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scm_sync_gitlab_commits import (
    GitCommit,
    FetchWindow,
    AdaptiveWindowState,
    compute_commit_fetch_window,
    select_next_batch,
    compute_batch_cursor_target,
    _deduplicate_commits,
    _get_commit_timestamp,
    _get_commit_sort_key,
)
from engram_step1.config import (
    DEFAULT_FORWARD_WINDOW_SECONDS,
    DEFAULT_FORWARD_WINDOW_MIN_SECONDS,
    DEFAULT_ADAPTIVE_SHRINK_FACTOR,
    DEFAULT_ADAPTIVE_GROW_FACTOR,
    DEFAULT_ADAPTIVE_COMMIT_THRESHOLD,
)


def make_commit(
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


class TestComputeCommitFetchWindow:
    """测试 compute_commit_fetch_window 函数"""

    def test_first_sync_starts_from_epoch(self):
        """首次同步从 1970-01-01 开始"""
        now = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        
        window = compute_commit_fetch_window(
            cursor_ts=None,
            overlap_seconds=300,
            forward_window_seconds=3600,
            now=now,
        )
        
        assert window.since == datetime(1970, 1, 1, tzinfo=timezone.utc)
        # until = 1970-01-01 + 3600s = 1970-01-01T01:00:00
        assert window.until == datetime(1970, 1, 1, 1, 0, 0, tzinfo=timezone.utc)

    def test_incremental_sync_with_overlap(self):
        """增量同步时向前回溯 overlap"""
        now = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        cursor_ts = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        
        window = compute_commit_fetch_window(
            cursor_ts=cursor_ts,
            overlap_seconds=300,  # 5 分钟
            forward_window_seconds=3600,  # 1 小时
            now=now,
        )
        
        # since = cursor_ts - 300s = 09:55:00
        expected_since = datetime(2024, 1, 15, 9, 55, 0, tzinfo=timezone.utc)
        assert window.since == expected_since
        
        # until = since + 3600s = 10:55:00
        expected_until = datetime(2024, 1, 15, 10, 55, 0, tzinfo=timezone.utc)
        assert window.until == expected_until

    def test_until_capped_at_now(self):
        """until 不超过当前时间"""
        now = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        cursor_ts = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        
        window = compute_commit_fetch_window(
            cursor_ts=cursor_ts,
            overlap_seconds=300,
            forward_window_seconds=7200,  # 2 小时，超过 now
            now=now,
        )
        
        # until 应被 cap 到 now
        assert window.until == now

    def test_cursor_ts_without_timezone(self):
        """无时区的 cursor_ts 自动假定 UTC"""
        now = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        cursor_ts = datetime(2024, 1, 15, 10, 0, 0)  # naive datetime
        
        window = compute_commit_fetch_window(
            cursor_ts=cursor_ts,
            overlap_seconds=300,
            forward_window_seconds=3600,
            now=now,
        )
        
        # 应该正常工作，since 有时区信息
        assert window.since.tzinfo is not None
        assert window.until.tzinfo is not None


class TestSelectNextBatch:
    """测试 select_next_batch 函数"""

    def test_empty_commits_returns_empty(self):
        """空列表返回空"""
        result = select_next_batch([], None, None, 100)
        assert result == []

    def test_filters_by_cursor_watermark(self):
        """按游标水位线过滤"""
        ts = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        commits = [
            make_commit("aaa", committed_date=ts),
            make_commit("bbb", committed_date=ts),
            make_commit("ccc", committed_date=ts),
        ]
        
        # cursor_sha = "bbb" 表示已处理到 (ts, bbb)
        result = select_next_batch(commits, cursor_sha="bbb", cursor_ts=ts, batch_size=100)
        
        # 只保留 sha > "bbb" 的
        assert len(result) == 1
        assert result[0].sha == "ccc"

    def test_respects_batch_size_limit(self):
        """遵守 batch_size 限制"""
        ts = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        commits = [make_commit(f"sha_{i:03d}", committed_date=ts) for i in range(100)]
        
        result = select_next_batch(commits, None, None, batch_size=10)
        
        assert len(result) == 10

    def test_maintains_sort_order(self):
        """保持 (ts, sha) 升序"""
        ts1 = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        ts2 = datetime(2024, 1, 15, 13, 0, 0, tzinfo=timezone.utc)
        
        # 乱序输入
        commits = [
            make_commit("ccc", committed_date=ts1),
            make_commit("aaa", committed_date=ts2),
            make_commit("bbb", committed_date=ts1),
        ]
        
        result = select_next_batch(commits, None, None, batch_size=100)
        
        # 按 (ts, sha) 升序
        assert result[0].sha == "bbb"  # ts1, bbb
        assert result[1].sha == "ccc"  # ts1, ccc
        assert result[2].sha == "aaa"  # ts2, aaa


class TestAdaptiveWindowState:
    """测试 AdaptiveWindowState 自适应窗口策略"""

    def test_initial_state(self):
        """初始状态"""
        state = AdaptiveWindowState(
            current_window_seconds=3600,
            min_window_seconds=60,
            max_window_seconds=7200,
            shrink_factor=0.5,
            grow_factor=1.5,
            commit_threshold=200,
        )
        
        assert state.current_window_seconds == 3600
        assert state.rate_limit_count == 0

    def test_shrink_reduces_window(self):
        """shrink 减小窗口"""
        state = AdaptiveWindowState(
            current_window_seconds=3600,
            min_window_seconds=60,
            max_window_seconds=7200,
            shrink_factor=0.5,
            grow_factor=1.5,
            commit_threshold=200,
        )
        
        result = state.shrink(reason="too_many_commits")
        
        assert result == 1800  # 3600 * 0.5
        assert state.current_window_seconds == 1800

    def test_shrink_respects_min(self):
        """shrink 不低于最小值"""
        state = AdaptiveWindowState(
            current_window_seconds=100,
            min_window_seconds=60,
            max_window_seconds=7200,
            shrink_factor=0.5,
            grow_factor=1.5,
            commit_threshold=200,
        )
        
        result = state.shrink()
        
        # 100 * 0.5 = 50，但最小是 60
        assert result == 60
        assert state.current_window_seconds == 60

    def test_grow_increases_window(self):
        """grow 增大窗口"""
        state = AdaptiveWindowState(
            current_window_seconds=1800,
            min_window_seconds=60,
            max_window_seconds=7200,
            shrink_factor=0.5,
            grow_factor=1.5,
            commit_threshold=200,
        )
        
        result = state.grow()
        
        assert result == 2700  # 1800 * 1.5
        assert state.current_window_seconds == 2700

    def test_grow_respects_max(self):
        """grow 不超过最大值"""
        state = AdaptiveWindowState(
            current_window_seconds=6000,
            min_window_seconds=60,
            max_window_seconds=7200,
            shrink_factor=0.5,
            grow_factor=1.5,
            commit_threshold=200,
        )
        
        result = state.grow()
        
        # 6000 * 1.5 = 9000，但最大是 7200
        assert result == 7200
        assert state.current_window_seconds == 7200

    def test_rate_limit_triggers_shrink(self):
        """429 错误触发窗口缩小"""
        state = AdaptiveWindowState(
            current_window_seconds=3600,
            min_window_seconds=60,
            max_window_seconds=7200,
            shrink_factor=0.5,
            grow_factor=1.5,
            commit_threshold=200,
        )
        
        state.record_rate_limit()
        
        assert state.rate_limit_count == 1
        assert state.current_window_seconds == 1800  # 自动缩小

    def test_reset_rate_limit_count(self):
        """重置限流计数"""
        state = AdaptiveWindowState(
            current_window_seconds=3600,
            min_window_seconds=60,
            max_window_seconds=7200,
            shrink_factor=0.5,
            grow_factor=1.5,
            commit_threshold=200,
        )
        
        state.rate_limit_count = 5
        state.reset_rate_limit_count()
        
        assert state.rate_limit_count == 0


class TestComputeBatchCursorTarget:
    """测试 compute_batch_cursor_target 函数"""

    def test_empty_list_returns_none(self):
        """空列表返回 None"""
        result = compute_batch_cursor_target([])
        assert result is None

    def test_returns_last_commit(self):
        """返回最后一个 commit 的 (ts, sha)"""
        ts1 = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        ts2 = datetime(2024, 1, 15, 13, 0, 0, tzinfo=timezone.utc)
        
        commits = [
            make_commit("aaa", committed_date=ts1),
            make_commit("bbb", committed_date=ts1),
            make_commit("ccc", committed_date=ts2),
        ]
        
        result = compute_batch_cursor_target(commits)
        
        assert result is not None
        target_ts, target_sha = result
        assert target_ts == ts2
        assert target_sha == "ccc"


class MockGitLabClient:
    """
    模拟 GitLabClient，返回"最新优先分页"的 commits
    
    GitLab API 默认按时间降序返回 commits（最新在前）
    """
    
    def __init__(self, all_commits: List[Dict[str, Any]]):
        """
        Args:
            all_commits: 按时间降序排列的所有 commits
        """
        self.all_commits = all_commits
        self.call_count = 0
    
    def get_commits(
        self,
        project_id: str,
        since: Optional[str] = None,
        until: Optional[str] = None,
        ref_name: Optional[str] = None,
        per_page: int = 100,
        page: int = 1,
    ) -> List[Dict[str, Any]]:
        """模拟 get_commits API"""
        self.call_count += 1
        
        # 解析时间范围
        since_dt = None
        until_dt = None
        if since:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        if until:
            until_dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
        
        # 过滤符合时间范围的 commits
        filtered = []
        for commit in self.all_commits:
            commit_ts = datetime.fromisoformat(
                commit["committed_date"].replace("Z", "+00:00")
            )
            
            if since_dt and commit_ts < since_dt:
                continue
            if until_dt and commit_ts > until_dt:
                continue
            filtered.append(commit)
        
        # 分页
        start_idx = (page - 1) * per_page
        end_idx = start_idx + per_page
        
        return filtered[start_idx:end_idx]


class TestMultiRoundSyncCoverage:
    """测试多轮同步覆盖全部 commits 且无重复"""

    def _make_commit_data(self, sha: str, ts: datetime) -> Dict[str, Any]:
        """创建 GitLab API 格式的 commit 数据"""
        return {
            "id": sha,
            "author_name": "Test User",
            "author_email": "test@example.com",
            "authored_date": ts.isoformat(),
            "committer_name": "Test User",
            "committer_email": "test@example.com",
            "committed_date": ts.isoformat(),
            "message": f"Commit {sha}",
            "parent_ids": [],
            "web_url": "",
            "stats": {"additions": 1, "deletions": 0, "total": 1},
        }

    def test_multi_round_covers_all_commits(self):
        """多轮同步覆盖所有 commits"""
        # 创建 50 个 commits，跨 10 个不同时间点
        base_ts = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        all_commits_data = []
        
        for hour in range(10):
            ts = base_ts + timedelta(hours=hour)
            for i in range(5):  # 每小时 5 个 commits
                sha = f"sha_{hour:02d}_{chr(ord('a') + i)}"
                all_commits_data.append(self._make_commit_data(sha, ts))
        
        # 按时间降序（模拟 GitLab API 返回顺序）
        all_commits_data.sort(
            key=lambda c: c["committed_date"], reverse=True
        )
        
        # 模拟多轮同步
        mock_client = MockGitLabClient(all_commits_data)
        
        processed_shas = set()
        # 首次同步时使用最早时间点作为起始游标（模拟 time_window_days 限制）
        cursor_ts = base_ts - timedelta(hours=1)  # 比数据起始早 1 小时
        cursor_sha = None
        round_count = 0
        max_rounds = 20
        
        forward_window_seconds = 3600  # 1 小时
        overlap_seconds = 300
        batch_size = 10
        
        now = base_ts + timedelta(hours=20)  # 当前时间
        
        while round_count < max_rounds:
            round_count += 1
            
            # 计算时间窗口
            window = compute_commit_fetch_window(
                cursor_ts=cursor_ts,
                overlap_seconds=overlap_seconds,
                forward_window_seconds=forward_window_seconds,
                now=now,
            )
            
            # 获取 commits
            commits_data = mock_client.get_commits(
                project_id="test",
                since=window.since.isoformat(),
                until=window.until.isoformat(),
                per_page=100,
            )
            
            if not commits_data:
                # 窗口内没有 commits，推进窗口
                cursor_ts = window.until
                cursor_sha = None  # 重置 sha，因为进入新时间段
                continue
            
            # 解析并选择批次
            commits = [
                make_commit(c["id"], datetime.fromisoformat(c["committed_date"].replace("Z", "+00:00")))
                for c in commits_data
            ]
            
            batch = select_next_batch(
                commits, cursor_sha, cursor_ts, batch_size
            )
            
            if not batch:
                # 没有新 commits，推进窗口
                cursor_ts = window.until
                cursor_sha = None
                continue
            
            # 处理批次
            for commit in batch:
                processed_shas.add(commit.sha)
            
            # 更新游标
            target = compute_batch_cursor_target(batch)
            if target:
                cursor_ts, cursor_sha = target
            
            # 检查是否完成
            if cursor_ts and cursor_ts >= now:
                break
        
        # 验证覆盖了所有 commits
        expected_shas = {c["id"] for c in all_commits_data}
        assert processed_shas == expected_shas, f"Missing: {expected_shas - processed_shas}"

    def test_no_duplicate_processing(self):
        """验证不会重复处理 commits"""
        base_ts = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        
        # 同一秒内的多个 commits
        all_commits_data = [
            self._make_commit_data("sha_a", base_ts),
            self._make_commit_data("sha_b", base_ts),
            self._make_commit_data("sha_c", base_ts),
            self._make_commit_data("sha_d", base_ts + timedelta(seconds=1)),
            self._make_commit_data("sha_e", base_ts + timedelta(seconds=1)),
        ]
        
        # 按时间降序
        all_commits_data.sort(key=lambda c: c["committed_date"], reverse=True)
        
        # 第一轮处理
        commits = [
            make_commit(c["id"], datetime.fromisoformat(c["committed_date"].replace("Z", "+00:00")))
            for c in all_commits_data
        ]
        
        batch1 = select_next_batch(commits, None, None, batch_size=3)
        assert len(batch1) == 3
        processed_round1 = {c.sha for c in batch1}
        
        # 更新游标
        target = compute_batch_cursor_target(batch1)
        cursor_ts, cursor_sha = target
        
        # 第二轮（模拟 overlap 导致部分重复）
        batch2 = select_next_batch(commits, cursor_sha, cursor_ts, batch_size=3)
        processed_round2 = {c.sha for c in batch2}
        
        # 验证没有重复
        assert len(processed_round1 & processed_round2) == 0
        
        # 合并后应该覆盖所有
        all_processed = processed_round1 | processed_round2
        expected = {c["id"] for c in all_commits_data}
        assert all_processed == expected

    def test_same_second_commits_stable_order(self):
        """同一秒内的 commits 有稳定的处理顺序"""
        ts = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        
        # 多次运行应该产生相同的处理顺序
        commits_data = [
            self._make_commit_data("sha_c", ts),
            self._make_commit_data("sha_a", ts),
            self._make_commit_data("sha_b", ts),
        ]
        
        results = []
        for _ in range(5):
            commits = [
                make_commit(c["id"], datetime.fromisoformat(c["committed_date"].replace("Z", "+00:00")))
                for c in commits_data
            ]
            
            batch = select_next_batch(commits, None, None, batch_size=10)
            order = [c.sha for c in batch]
            results.append(order)
        
        # 所有运行结果应该一致
        assert all(r == results[0] for r in results)
        # 顺序应该是按 sha 字典序
        assert results[0] == ["sha_a", "sha_b", "sha_c"]


class TestWindowedIncrementalIntegration:
    """窗口化增量同步的集成测试"""

    def test_adaptive_window_shrinks_on_many_commits(self):
        """commits 数量过多时自适应缩小窗口"""
        state = AdaptiveWindowState(
            current_window_seconds=3600,
            min_window_seconds=60,
            max_window_seconds=7200,
            shrink_factor=0.5,
            grow_factor=1.5,
            commit_threshold=200,
        )
        
        # 模拟获取到 250 个 commits（超过阈值）
        commit_count = 250
        
        if commit_count > state.commit_threshold:
            state.shrink(reason=f"commit_count={commit_count}>{state.commit_threshold}")
        
        assert state.current_window_seconds == 1800

    def test_adaptive_window_grows_on_few_commits(self):
        """commits 数量较少时自适应增长窗口"""
        state = AdaptiveWindowState(
            current_window_seconds=1800,
            min_window_seconds=60,
            max_window_seconds=7200,
            shrink_factor=0.5,
            grow_factor=1.5,
            commit_threshold=200,
        )
        
        # 模拟获取到 50 个 commits（远低于阈值）
        commit_count = 50
        
        if commit_count < state.commit_threshold // 2:
            state.grow()
        
        assert state.current_window_seconds == 2700


class TestHighActivityNoMissingCommits:
    """
    测试高活跃场景（>batch_size）不漏数据
    
    验证当仓库活跃度很高，单窗口内 commits 数超过 batch_size 时：
    1. 多轮迭代能覆盖全部 commits
    2. 游标基于 (ts, sha) 复合水位线保证不漏不重
    3. 分页最新优先的 GitLab API 模式下仍能正确处理
    """

    def _make_commit_data(self, sha: str, ts: datetime) -> Dict[str, Any]:
        """创建 GitLab API 格式的 commit 数据"""
        return {
            "id": sha,
            "author_name": "Test User",
            "author_email": "test@example.com",
            "authored_date": ts.isoformat(),
            "committer_name": "Test User",
            "committer_email": "test@example.com",
            "committed_date": ts.isoformat(),
            "message": f"Commit {sha}",
            "parent_ids": [],
            "web_url": "",
            "stats": {"additions": 1, "deletions": 0, "total": 1},
        }

    def test_high_activity_batch_split_no_missing(self):
        """
        高活跃仓库：单窗口内 commits 数 > batch_size，分多轮处理不漏
        
        场景：
        - 某个小时内有 150 个 commits
        - batch_size = 50
        - 预期需要 3+ 轮才能处理完毕
        - 验证 150 个 commits 全部被处理且无重复
        """
        base_ts = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        batch_size = 50
        
        # 创建 150 个 commits，都在同一小时内
        all_commits_data = []
        for i in range(150):
            # 每个 commit 相隔 20 秒，确保 ts 不同
            ts = base_ts + timedelta(seconds=i * 20)
            sha = f"sha_{i:04d}"
            all_commits_data.append(self._make_commit_data(sha, ts))
        
        # 使用 select_next_batch 的去重+排序逻辑直接模拟多轮处理
        # 不依赖 MockGitLabClient，因为它的时间过滤逻辑与测试目的不符
        
        processed_shas = set()
        cursor_ts: Optional[datetime] = None
        cursor_sha: Optional[str] = None
        round_count = 0
        max_rounds = 10
        
        # 所有 commits 转为 GitCommit 对象
        all_commits = [
            make_commit(c["id"], datetime.fromisoformat(c["committed_date"].replace("Z", "+00:00")))
            for c in all_commits_data
        ]
        
        while round_count < max_rounds:
            round_count += 1
            
            # 使用 select_next_batch 选择下一批
            batch = select_next_batch(all_commits, cursor_sha, cursor_ts, batch_size)
            
            if not batch:
                # 全部处理完毕
                break
            
            # 处理批次
            for commit in batch:
                processed_shas.add(commit.sha)
            
            # 更新游标
            target = compute_batch_cursor_target(batch)
            if target:
                cursor_ts, cursor_sha = target
        
        # 验证覆盖了所有 commits
        expected_shas = {c["id"] for c in all_commits_data}
        missing = expected_shas - processed_shas
        extra = processed_shas - expected_shas
        
        assert not missing, f"遗漏 {len(missing)} 个 commits: {list(missing)[:5]}"
        assert not extra, f"多出 {len(extra)} 个未预期的 commits"
        assert processed_shas == expected_shas
        assert round_count >= 3, f"预期至少 3 轮，实际 {round_count} 轮"

    def test_same_second_high_volume_no_missing(self):
        """
        同一秒内大量 commits：使用 (ts, sha) 复合水位线保证不漏
        
        场景：
        - 同一秒内有 100 个 commits
        - batch_size = 20
        - 验证通过 sha 字典序排序确保处理顺序稳定且不漏
        """
        ts = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        batch_size = 20
        
        # 创建 100 个同一秒的 commits
        all_commits_data = []
        for i in range(100):
            sha = f"commit_{i:04d}_hash"
            all_commits_data.append(self._make_commit_data(sha, ts))
        
        # 解析为 GitCommit 对象
        commits = [
            make_commit(c["id"], datetime.fromisoformat(c["committed_date"].replace("Z", "+00:00")))
            for c in all_commits_data
        ]
        
        # 模拟多轮处理
        processed_shas = set()
        cursor_sha: Optional[str] = None
        cursor_ts: Optional[datetime] = None
        round_count = 0
        
        while round_count < 10:
            round_count += 1
            
            batch = select_next_batch(commits, cursor_sha, cursor_ts, batch_size)
            
            if not batch:
                break
            
            # 处理批次
            for commit in batch:
                # 确保没有重复
                assert commit.sha not in processed_shas, f"重复处理 {commit.sha}"
                processed_shas.add(commit.sha)
            
            # 更新游标
            target = compute_batch_cursor_target(batch)
            if target:
                cursor_ts, cursor_sha = target
        
        # 验证覆盖了所有 commits
        expected_shas = {c["id"] for c in all_commits_data}
        assert processed_shas == expected_shas
        # 100 个 commits，batch_size=20，需要 5 轮处理数据 + 1 轮检测结束 = 5-6 轮
        assert 5 <= round_count <= 6, f"预期 5-6 轮，实际 {round_count} 轮"

    def test_mixed_timestamps_batch_boundary(self):
        """
        混合时间戳场景：批次边界跨越时间戳时不丢失
        
        场景：
        - 10:00:00 有 30 个 commits
        - 10:00:01 有 30 个 commits
        - batch_size = 40
        - 第一轮处理 40 个（跨越两个时间点），第二轮处理剩余 20 个
        """
        ts1 = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        ts2 = datetime(2024, 1, 15, 10, 0, 1, tzinfo=timezone.utc)
        batch_size = 40
        
        all_commits_data = []
        # ts1 时刻 30 个
        for i in range(30):
            sha = f"ts1_{i:03d}"
            all_commits_data.append(self._make_commit_data(sha, ts1))
        # ts2 时刻 30 个
        for i in range(30):
            sha = f"ts2_{i:03d}"
            all_commits_data.append(self._make_commit_data(sha, ts2))
        
        commits = [
            make_commit(c["id"], datetime.fromisoformat(c["committed_date"].replace("Z", "+00:00")))
            for c in all_commits_data
        ]
        
        # 第一轮
        batch1 = select_next_batch(commits, None, None, batch_size)
        assert len(batch1) == 40
        
        processed_shas = {c.sha for c in batch1}
        
        # 更新游标
        target = compute_batch_cursor_target(batch1)
        cursor_ts, cursor_sha = target
        
        # 第二轮
        batch2 = select_next_batch(commits, cursor_sha, cursor_ts, batch_size)
        assert len(batch2) == 20
        
        # 验证无重复
        for c in batch2:
            assert c.sha not in processed_shas
            processed_shas.add(c.sha)
        
        # 验证全部覆盖
        expected_shas = {c["id"] for c in all_commits_data}
        assert processed_shas == expected_shas


# ---------- 运行测试的入口 ----------

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
