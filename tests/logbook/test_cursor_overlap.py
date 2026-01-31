# -*- coding: utf-8 -*-
"""
test_cursor_overlap.py - 测试游标重叠策略和 SVN 子进程 mock

验证:
1. SVN 同步的 overlap 策略计算正确
2. 游标更新后正确应用 overlap
3. SVN 子进程调用的 mock 测试
"""

import os
import subprocess
import sys
from datetime import datetime
from unittest.mock import MagicMock, Mock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scm_sync_svn import (
    FetchDiffResult,
    PatchFetchCommandError,
    PatchFetchContentTooLargeError,
    PatchFetchError,
    PatchFetchTimeoutError,
    SvnCommandError,
    SvnTimeoutError,
    SyncConfig,
    fetch_svn_diff,
    fetch_svn_log_xml,
    generate_diffstat,
    generate_ministat_from_changed_paths,
    get_svn_head_revision,
    parse_svn_log_xml,
)

# ---------- SVN XML 解析测试 ----------


class TestParseSvnLogXml:
    """测试 SVN log XML 解析"""

    def test_parse_single_revision(self):
        """解析单个 revision"""
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
<log>
<logentry revision="123">
<author>testuser</author>
<date>2024-01-15T10:30:45.123456Z</date>
<msg>Test commit message</msg>
<paths>
<path action="M" kind="file">/trunk/src/main.py</path>
<path action="A" kind="file">/trunk/src/new.py</path>
</paths>
</logentry>
</log>"""

        revisions = parse_svn_log_xml(xml_content)

        assert len(revisions) == 1
        rev = revisions[0]
        assert rev.revision == 123
        assert rev.author == "testuser"
        assert rev.message == "Test commit message"
        assert len(rev.changed_paths) == 2
        assert rev.changed_paths[0]["path"] == "/trunk/src/main.py"
        assert rev.changed_paths[0]["action"] == "M"

    def test_parse_multiple_revisions(self):
        """解析多个 revision"""
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
<log>
<logentry revision="100">
<author>user1</author>
<date>2024-01-10T08:00:00Z</date>
<msg>First commit</msg>
</logentry>
<logentry revision="101">
<author>user2</author>
<date>2024-01-11T09:00:00Z</date>
<msg>Second commit</msg>
</logentry>
<logentry revision="102">
<author>user1</author>
<date>2024-01-12T10:00:00Z</date>
<msg>Third commit</msg>
</logentry>
</log>"""

        revisions = parse_svn_log_xml(xml_content)

        assert len(revisions) == 3
        assert [r.revision for r in revisions] == [100, 101, 102]
        assert [r.author for r in revisions] == ["user1", "user2", "user1"]

    def test_parse_empty_log(self):
        """解析空日志"""
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
<log>
</log>"""

        revisions = parse_svn_log_xml(xml_content)
        assert len(revisions) == 0

    def test_parse_copyfrom_info(self):
        """解析包含 copyfrom 信息的路径"""
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
<log>
<logentry revision="200">
<author>user</author>
<date>2024-01-20T12:00:00Z</date>
<msg>Branch creation</msg>
<paths>
<path action="A" kind="dir" copyfrom-path="/trunk" copyfrom-rev="199">/branches/feature</path>
</paths>
</logentry>
</log>"""

        revisions = parse_svn_log_xml(xml_content)

        assert len(revisions) == 1
        path_info = revisions[0].changed_paths[0]
        assert path_info["copyfrom_path"] == "/trunk"
        assert path_info["copyfrom_rev"] == 199


# ---------- SVN 命令 Mock 测试 ----------


class TestSvnCommandMock:
    """测试 SVN 命令的 mock"""

    def test_get_svn_head_revision_success(self, mock_subprocess):
        """Mock svn info 成功获取 HEAD revision"""
        mock_subprocess.return_value = Mock(
            stdout="""<?xml version="1.0" encoding="UTF-8"?>
<info>
<entry revision="500" path="." kind="dir">
<url>svn://example.com/repo/trunk</url>
<repository>
<root>svn://example.com/repo</root>
</repository>
</entry>
</info>""",
            returncode=0,
        )

        head_rev = get_svn_head_revision("svn://example.com/repo/trunk")

        assert head_rev == 500
        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args
        assert "svn" in call_args[0][0]
        assert "info" in call_args[0][0]

    def test_get_svn_head_revision_failure(self, mock_subprocess):
        """Mock svn info 命令失败"""
        mock_subprocess.side_effect = subprocess.CalledProcessError(
            returncode=1,
            cmd=["svn", "info"],
            stderr="svn: E170013: Unable to connect to a repository",
        )

        with pytest.raises(SvnCommandError) as exc_info:
            get_svn_head_revision("svn://invalid.example.com/repo")

        assert "svn info 命令执行失败" in str(exc_info.value)

    def test_get_svn_head_revision_timeout(self, mock_subprocess):
        """Mock svn info 命令超时"""
        mock_subprocess.side_effect = subprocess.TimeoutExpired(
            cmd=["svn", "info"],
            timeout=60,
        )

        with pytest.raises(SvnTimeoutError) as exc_info:
            get_svn_head_revision("svn://slow.example.com/repo")

        assert "超时" in str(exc_info.value)

    def test_fetch_svn_log_xml_success(self, mock_subprocess):
        """Mock svn log 成功获取日志"""
        expected_xml = """<?xml version="1.0" encoding="UTF-8"?>
<log>
<logentry revision="100">
<author>testuser</author>
<date>2024-01-15T10:00:00Z</date>
<msg>Test commit</msg>
</logentry>
</log>"""

        mock_subprocess.return_value = Mock(
            stdout=expected_xml,
            returncode=0,
        )

        xml_content = fetch_svn_log_xml(
            "svn://example.com/repo",
            start_rev=100,
            end_rev=100,
        )

        assert xml_content == expected_xml

        # 验证命令参数
        call_args = mock_subprocess.call_args[0][0]
        assert "svn" in call_args
        assert "log" in call_args
        assert "--xml" in call_args
        assert "-r" in call_args
        assert "100:100" in call_args

    def test_fetch_svn_log_with_verbose(self, mock_subprocess):
        """Mock svn log 带 -v 参数"""
        mock_subprocess.return_value = Mock(
            stdout="<log></log>",
            returncode=0,
        )

        fetch_svn_log_xml(
            "svn://example.com/repo",
            start_rev=1,
            end_rev=10,
            verbose=True,
        )

        call_args = mock_subprocess.call_args[0][0]
        assert "-v" in call_args


# ---------- Overlap 策略测试 ----------


class TestOverlapStrategy:
    """测试 overlap 策略计算"""

    def test_overlap_calculation_basic(self):
        """基本 overlap 计算"""
        last_synced = 100
        overlap = 5

        # 计算起始 revision
        start_rev = max(1, last_synced + 1 - overlap)

        assert start_rev == 96, "应从 r96 开始（回退 5 个）"

    def test_overlap_calculation_first_sync(self):
        """首次同步时 overlap 不影响起始位置"""
        last_synced = 0
        overlap = 10

        start_rev = max(1, last_synced + 1 - overlap)

        assert start_rev == 1, "首次同步应从 r1 开始"

    def test_overlap_zero(self):
        """overlap 为 0 时正常递增"""
        last_synced = 100
        overlap = 0

        start_rev = max(1, last_synced + 1 - overlap)

        assert start_rev == 101, "无 overlap 时应从下一个 revision 开始"

    def test_batch_with_overlap(self):
        """批量同步配合 overlap 测试"""
        last_synced = 200
        overlap = 10
        batch_size = 50
        head_rev = 500

        start_rev = max(1, last_synced + 1 - overlap)
        end_rev = min(head_rev, start_rev + batch_size - 1)

        assert start_rev == 191
        assert end_rev == 240

        # 本批次覆盖 [191, 240]，共 50 个 revision
        assert end_rev - start_rev + 1 == batch_size


class TestSyncConfigOverlap:
    """测试 SyncConfig overlap 配置"""

    def test_sync_config_default_overlap(self):
        """默认 overlap 为 0"""
        config = SyncConfig(
            svn_url="svn://example.com/repo",
            batch_size=100,
        )

        assert config.overlap == 0

    def test_sync_config_custom_overlap(self):
        """自定义 overlap"""
        config = SyncConfig(
            svn_url="svn://example.com/repo",
            batch_size=100,
            overlap=20,
        )

        assert config.overlap == 20


# ---------- SVN Diff Mock 测试 ----------


class TestSvnDiffMock:
    """测试 SVN diff 命令的 mock"""

    def test_fetch_svn_diff_success(self, mock_subprocess):
        """Mock svn diff 成功获取 diff"""
        expected_diff = """Index: trunk/src/main.py
===================================================================
--- trunk/src/main.py   (revision 99)
+++ trunk/src/main.py   (revision 100)
@@ -1,3 +1,4 @@
 import sys
+import os

 def main():
"""

        mock_subprocess.return_value = Mock(
            stdout=expected_diff,
            returncode=0,
        )

        result = fetch_svn_diff("svn://example.com/repo", revision=100)

        # 新 API 返回 FetchDiffResult
        assert result.success is True
        assert result.content == expected_diff

        call_args = mock_subprocess.call_args[0][0]
        assert "svn" in call_args
        assert "diff" in call_args
        assert "-c" in call_args
        assert "100" in call_args

    def test_fetch_svn_diff_failure_returns_result(self, mock_subprocess):
        """Mock svn diff 失败返回 FetchDiffResult（非 None）"""
        mock_subprocess.side_effect = subprocess.CalledProcessError(
            returncode=1,
            cmd=["svn", "diff"],
            stderr="svn: E160013: Path not found",
        )

        result = fetch_svn_diff("svn://example.com/repo", revision=999)

        # 新 API 返回 FetchDiffResult 而非 None
        assert result.success is False
        assert result.error_category == "command_error"


# ---------- Cursor 升级测试 ----------

# 添加 engram_logbook 到 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engram.logbook.cursor import (
    CURSOR_TYPE_GITLAB,
    CURSOR_TYPE_GITLAB_MR,
    CURSOR_TYPE_GITLAB_REVIEWS,
    CURSOR_TYPE_SVN,
    CURSOR_VERSION,
    Cursor,
    _detect_cursor_version,
    normalize_iso_ts_z,
    parse_iso_ts,
    should_advance_gitlab_commit_cursor,
    should_advance_mr_cursor,
    upgrade_cursor,
)


class TestCursorUpgrade:
    """测试游标版本升级 (v1 → v2)"""

    def test_detect_version_v1_no_version_field(self):
        """检测 v1 格式（无 version 字段）"""
        v1_data = {"last_rev": 100, "last_sync_at": "2024-01-15T10:00:00Z"}
        assert _detect_cursor_version(v1_data) == 1

    def test_detect_version_v2_with_version_field(self):
        """检测 v2 格式（有 version 字段）"""
        v2_data = {"version": 2, "watermark": {}, "stats": {}}
        assert _detect_cursor_version(v2_data) == 2

    def test_upgrade_svn_cursor_v1_to_v2(self):
        """升级 SVN 游标: v1 → v2"""
        v1_data = {
            "last_rev": 500,
            "last_sync_at": "2024-01-15T10:30:00Z",
            "last_sync_count": 50,
        }

        cursor = upgrade_cursor(v1_data, CURSOR_TYPE_SVN)

        assert cursor.version == CURSOR_VERSION
        assert cursor.watermark == {"last_rev": 500}
        assert cursor.stats == {
            "last_sync_at": "2024-01-15T10:30:00Z",
            "last_sync_count": 50,
        }
        # 便捷属性访问
        assert cursor.last_rev == 500
        assert cursor.last_sync_at == "2024-01-15T10:30:00Z"
        assert cursor.last_sync_count == 50

    def test_upgrade_gitlab_cursor_v1_to_v2(self):
        """升级 GitLab 游标: v1 → v2"""
        v1_data = {
            "last_commit_sha": "abc123def456",
            "last_commit_ts": "2024-01-15T12:00:00Z",
            "last_sync_at": "2024-01-15T12:05:00Z",
            "last_sync_count": 100,
        }

        cursor = upgrade_cursor(v1_data, CURSOR_TYPE_GITLAB)

        assert cursor.version == CURSOR_VERSION
        assert cursor.watermark == {
            "last_commit_sha": "abc123def456",
            "last_commit_ts": "2024-01-15T12:00:00Z",
        }
        assert cursor.stats == {
            "last_sync_at": "2024-01-15T12:05:00Z",
            "last_sync_count": 100,
        }
        # 便捷属性访问
        assert cursor.last_commit_sha == "abc123def456"
        assert cursor.last_commit_ts == "2024-01-15T12:00:00Z"

    def test_upgrade_v2_returns_as_is(self):
        """v2 格式不需要升级，直接返回"""
        v2_data = {
            "version": 2,
            "watermark": {"last_rev": 999},
            "stats": {"last_sync_at": "2024-01-20T08:00:00Z", "last_sync_count": 10},
        }

        cursor = upgrade_cursor(v2_data, CURSOR_TYPE_SVN)

        assert cursor.version == 2
        assert cursor.watermark == {"last_rev": 999}
        assert cursor.stats == {"last_sync_at": "2024-01-20T08:00:00Z", "last_sync_count": 10}

    def test_upgrade_empty_v1_svn(self):
        """升级空的 v1 SVN 游标"""
        v1_data = {}

        cursor = upgrade_cursor(v1_data, CURSOR_TYPE_SVN)

        assert cursor.version == CURSOR_VERSION
        assert cursor.watermark == {}
        assert cursor.stats == {}
        assert cursor.last_rev == 0  # 默认值

    def test_upgrade_empty_v1_gitlab(self):
        """升级空的 v1 GitLab 游标"""
        v1_data = {}

        cursor = upgrade_cursor(v1_data, CURSOR_TYPE_GITLAB)

        assert cursor.version == CURSOR_VERSION
        assert cursor.watermark == {}
        assert cursor.stats == {}
        assert cursor.last_commit_sha is None
        assert cursor.last_commit_ts is None

    def test_upgrade_partial_v1_svn(self):
        """升级部分字段的 v1 SVN 游标"""
        v1_data = {
            "last_rev": 100,
            # 缺少 last_sync_at 和 last_sync_count
        }

        cursor = upgrade_cursor(v1_data, CURSOR_TYPE_SVN)

        assert cursor.version == CURSOR_VERSION
        assert cursor.watermark == {"last_rev": 100}
        assert cursor.stats == {}
        assert cursor.last_rev == 100

    def test_upgrade_partial_v1_gitlab(self):
        """升级部分字段的 v1 GitLab 游标"""
        v1_data = {
            "last_commit_sha": "deadbeef",
            # 缺少 last_commit_ts
        }

        cursor = upgrade_cursor(v1_data, CURSOR_TYPE_GITLAB)

        assert cursor.version == CURSOR_VERSION
        assert cursor.watermark == {"last_commit_sha": "deadbeef"}
        assert cursor.stats == {}
        assert cursor.last_commit_sha == "deadbeef"
        assert cursor.last_commit_ts is None


class TestGitLabMRCursorUpgrade:
    """测试 GitLab MR 游标升级 (v1 → v2)"""

    def test_upgrade_gitlab_mr_cursor_v1_to_v2(self):
        """升级 GitLab MR 游标: v1 → v2"""
        v1_data = {
            "last_mr_updated_at": "2024-01-15T12:00:00Z",
            "last_mr_iid": 123,
            "last_sync_at": "2024-01-15T12:05:00Z",
            "last_sync_count": 50,
        }

        cursor = upgrade_cursor(v1_data, CURSOR_TYPE_GITLAB_MR)

        assert cursor.version == CURSOR_VERSION
        assert cursor.watermark == {
            "last_mr_updated_at": "2024-01-15T12:00:00Z",
            "last_mr_iid": 123,
        }
        assert cursor.stats == {
            "last_sync_at": "2024-01-15T12:05:00Z",
            "last_sync_count": 50,
        }
        # 便捷属性访问
        assert cursor.last_mr_updated_at == "2024-01-15T12:00:00Z"
        assert cursor.last_mr_iid == 123

    def test_upgrade_empty_v1_gitlab_mr(self):
        """升级空的 v1 GitLab MR 游标"""
        v1_data = {}

        cursor = upgrade_cursor(v1_data, CURSOR_TYPE_GITLAB_MR)

        assert cursor.version == CURSOR_VERSION
        assert cursor.watermark == {}
        assert cursor.stats == {}
        assert cursor.last_mr_updated_at is None
        assert cursor.last_mr_iid is None

    def test_upgrade_partial_v1_gitlab_mr(self):
        """升级部分字段的 v1 GitLab MR 游标"""
        v1_data = {
            "last_mr_updated_at": "2024-01-15T12:00:00Z",
            # 缺少 last_mr_iid
        }

        cursor = upgrade_cursor(v1_data, CURSOR_TYPE_GITLAB_MR)

        assert cursor.version == CURSOR_VERSION
        assert cursor.watermark == {"last_mr_updated_at": "2024-01-15T12:00:00Z"}
        assert cursor.last_mr_updated_at == "2024-01-15T12:00:00Z"
        assert cursor.last_mr_iid is None


class TestGitLabReviewsCursorUpgrade:
    """测试 GitLab Reviews 游标升级 (v1 → v2)"""

    def test_upgrade_gitlab_reviews_cursor_v1_to_v2(self):
        """升级 GitLab Reviews 游标: v1 → v2"""
        v1_data = {
            "last_updated_at": "2024-01-15T12:00:00Z",  # 旧键名
            "last_sync_at": "2024-01-15T12:05:00Z",
            "last_sync_mr_count": 10,
            "last_sync_event_count": 100,
        }

        cursor = upgrade_cursor(v1_data, CURSOR_TYPE_GITLAB_REVIEWS)

        assert cursor.version == CURSOR_VERSION
        assert cursor.watermark == {
            "last_mr_updated_at": "2024-01-15T12:00:00Z",
        }
        assert cursor.stats == {
            "last_sync_at": "2024-01-15T12:05:00Z",
            "last_sync_mr_count": 10,
            "last_sync_event_count": 100,
        }
        assert cursor.last_mr_updated_at == "2024-01-15T12:00:00Z"

    def test_upgrade_gitlab_reviews_with_event_ts(self):
        """升级包含事件级水位线的 GitLab Reviews 游标"""
        v1_data = {
            "last_mr_updated_at": "2024-01-15T12:00:00Z",
            "last_mr_iid": 456,
            "last_event_ts": "2024-01-15T12:30:00Z",
            "last_sync_at": "2024-01-15T12:35:00Z",
        }

        cursor = upgrade_cursor(v1_data, CURSOR_TYPE_GITLAB_REVIEWS)

        assert cursor.version == CURSOR_VERSION
        assert cursor.watermark == {
            "last_mr_updated_at": "2024-01-15T12:00:00Z",
            "last_mr_iid": 456,
            "last_event_ts": "2024-01-15T12:30:00Z",
        }
        assert cursor.last_mr_updated_at == "2024-01-15T12:00:00Z"
        assert cursor.last_mr_iid == 456
        assert cursor.last_event_ts == "2024-01-15T12:30:00Z"

    def test_upgrade_empty_v1_gitlab_reviews(self):
        """升级空的 v1 GitLab Reviews 游标"""
        v1_data = {}

        cursor = upgrade_cursor(v1_data, CURSOR_TYPE_GITLAB_REVIEWS)

        assert cursor.version == CURSOR_VERSION
        assert cursor.watermark == {}
        assert cursor.stats == {}
        assert cursor.last_mr_updated_at is None
        assert cursor.last_mr_iid is None
        assert cursor.last_event_ts is None


class TestShouldAdvanceMrCursor:
    """测试 MR 游标单调递增规则"""

    def test_first_sync_always_advances(self):
        """首次同步总是推进"""
        assert should_advance_mr_cursor("2024-01-15T12:00:00Z", 100, None, None) is True

    def test_newer_updated_at_advances(self):
        """更新时间更新时推进"""
        assert (
            should_advance_mr_cursor(
                "2024-01-15T13:00:00Z",
                100,
                "2024-01-15T12:00:00Z",
                100,
            )
            is True
        )

    def test_older_updated_at_does_not_advance(self):
        """更新时间更旧时不推进"""
        assert (
            should_advance_mr_cursor(
                "2024-01-15T11:00:00Z",
                100,
                "2024-01-15T12:00:00Z",
                100,
            )
            is False
        )

    def test_same_updated_at_higher_iid_advances(self):
        """更新时间相同、IID 更大时推进"""
        assert (
            should_advance_mr_cursor(
                "2024-01-15T12:00:00Z",
                101,
                "2024-01-15T12:00:00Z",
                100,
            )
            is True
        )

    def test_same_updated_at_lower_iid_does_not_advance(self):
        """更新时间相同、IID 更小时不推进"""
        assert (
            should_advance_mr_cursor(
                "2024-01-15T12:00:00Z",
                99,
                "2024-01-15T12:00:00Z",
                100,
            )
            is False
        )

    def test_same_updated_at_same_iid_does_not_advance(self):
        """更新时间和 IID 相同时不推进"""
        assert (
            should_advance_mr_cursor(
                "2024-01-15T12:00:00Z",
                100,
                "2024-01-15T12:00:00Z",
                100,
            )
            is False
        )

    def test_none_last_iid_always_advances_if_updated_at_same(self):
        """旧 IID 为 None 时，更新时间相同则推进"""
        assert (
            should_advance_mr_cursor(
                "2024-01-15T12:00:00Z",
                100,
                "2024-01-15T12:00:00Z",
                None,
            )
            is True
        )


class TestCursorDataClass:
    """测试 Cursor 数据类"""

    def test_cursor_to_dict(self):
        """测试转换为字典"""
        cursor = Cursor(
            version=2,
            watermark={"last_rev": 123},
            stats={"last_sync_at": "2024-01-01T00:00:00Z", "last_sync_count": 5},
        )

        data = cursor.to_dict()

        assert data == {
            "version": 2,
            "watermark": {"last_rev": 123},
            "stats": {"last_sync_at": "2024-01-01T00:00:00Z", "last_sync_count": 5},
        }

    def test_cursor_from_dict(self):
        """测试从字典创建"""
        data = {
            "version": 2,
            "watermark": {"last_commit_sha": "abc123"},
            "stats": {"last_sync_count": 10},
        }

        cursor = Cursor.from_dict(data)

        assert cursor.version == 2
        assert cursor.watermark == {"last_commit_sha": "abc123"}
        assert cursor.stats == {"last_sync_count": 10}

    def test_cursor_default_values(self):
        """测试默认值"""
        cursor = Cursor()

        assert cursor.version == CURSOR_VERSION
        assert cursor.watermark == {}
        assert cursor.stats == {}
        assert cursor.last_rev == 0
        assert cursor.last_commit_sha is None
        assert cursor.last_commit_ts is None
        assert cursor.last_sync_at is None
        assert cursor.last_sync_count is None


# ---------- SVN Patch 获取异常分类测试 ----------


class TestPatchFetchErrorClassification:
    """测试 Patch 获取异常分类"""

    def test_timeout_error_classification(self, mock_subprocess):
        """测试超时错误分类"""
        mock_subprocess.side_effect = subprocess.TimeoutExpired(
            cmd=["svn", "diff"],
            timeout=120,
        )

        result = fetch_svn_diff("svn://example.com/repo", revision=100)

        assert result.success is False
        assert result.error_category == "timeout"
        assert result.error is not None
        assert isinstance(result.error, PatchFetchTimeoutError)
        assert "timeout" in result.error_message.lower()

    def test_command_error_classification(self, mock_subprocess):
        """测试命令执行错误分类"""
        mock_subprocess.side_effect = subprocess.CalledProcessError(
            returncode=1,
            cmd=["svn", "diff"],
            stderr="svn: E160013: Path not found",
        )

        result = fetch_svn_diff("svn://example.com/repo", revision=999)

        assert result.success is False
        assert result.error_category == "command_error"
        assert result.error is not None
        assert isinstance(result.error, PatchFetchCommandError)

    def test_content_too_large_classification(self, mock_subprocess):
        """测试内容过大错误分类"""
        # 生成超过限制的大内容
        large_diff = "+" + "x" * (1024 * 1024 * 11)  # 11MB
        mock_subprocess.return_value = Mock(
            stdout=large_diff,
            returncode=0,
        )

        result = fetch_svn_diff(
            "svn://example.com/repo",
            revision=100,
            max_size_bytes=10 * 1024 * 1024,  # 10MB 限制
        )

        assert result.success is False
        assert result.error_category == "content_too_large"
        assert result.error is not None
        assert isinstance(result.error, PatchFetchContentTooLargeError)

    def test_success_result_structure(self, mock_subprocess):
        """测试成功结果的结构"""
        expected_diff = "Index: file.py\n+new line"
        mock_subprocess.return_value = Mock(
            stdout=expected_diff,
            returncode=0,
        )

        result = fetch_svn_diff("svn://example.com/repo", revision=100)

        assert result.success is True
        assert result.content == expected_diff
        assert result.error is None
        assert result.error_category is None
        assert result.endpoint is not None

    def test_fetch_diff_result_dataclass(self):
        """测试 FetchDiffResult 数据结构"""
        # 成功场景
        success_result = FetchDiffResult(
            success=True,
            content="diff content",
            endpoint="svn diff -c 100",
        )
        assert success_result.success is True
        assert success_result.content == "diff content"

        # 失败场景
        error = PatchFetchTimeoutError("timeout", {})
        fail_result = FetchDiffResult(
            success=False,
            error=error,
            error_category="timeout",
            error_message="timeout after 120s",
            endpoint="svn diff -c 100",
        )
        assert fail_result.success is False
        assert fail_result.error_category == "timeout"


# ---------- Ministat 生成测试 ----------


class TestMinistatGeneration:
    """测试 ministat 生成函数"""

    def test_generate_ministat_from_changed_paths_basic(self):
        """测试基本的 ministat 生成"""
        changed_paths = [
            {"path": "/trunk/src/main.py", "action": "M", "kind": "file"},
            {"path": "/trunk/src/new.py", "action": "A", "kind": "file"},
            {"path": "/trunk/src/old.py", "action": "D", "kind": "file"},
        ]

        result = generate_ministat_from_changed_paths(changed_paths, revision=100)

        assert "ministat" in result
        assert "r100" in result
        assert "degraded" in result
        assert "/trunk/src/main.py" in result
        assert "3 path(s) changed" in result
        assert "1 modified" in result
        assert "1 added" in result
        assert "1 deleted" in result

    def test_generate_ministat_empty_paths(self):
        """测试空路径列表"""
        result = generate_ministat_from_changed_paths([])
        assert result == ""

    def test_generate_ministat_with_directories(self):
        """测试包含目录的变更"""
        changed_paths = [
            {"path": "/trunk/new_dir", "action": "A", "kind": "dir"},
            {"path": "/trunk/new_dir/file.py", "action": "A", "kind": "file"},
        ]

        result = generate_ministat_from_changed_paths(changed_paths)

        assert "(dir)" in result
        assert "2 path(s) changed" in result

    def test_generate_ministat_long_path_truncation(self):
        """测试长路径截断"""
        long_path = "/trunk/" + "a" * 100 + "/file.py"
        changed_paths = [
            {"path": long_path, "action": "M", "kind": "file"},
        ]

        result = generate_ministat_from_changed_paths(changed_paths)

        # 应该包含截断标记
        assert "..." in result


class TestDiffstatGeneration:
    """测试 diffstat 生成函数"""

    def test_generate_diffstat_basic(self):
        """测试基本的 diffstat 生成"""
        diff_content = """Index: trunk/src/main.py
===================================================================
--- trunk/src/main.py   (revision 99)
+++ trunk/src/main.py   (revision 100)
@@ -1,3 +1,5 @@
 import sys
+import os
+import json

 def main():
-    pass
"""
        result = generate_diffstat(diff_content)

        assert "trunk/src/main.py" in result
        assert "insertion" in result
        assert "deletion" in result

    def test_generate_diffstat_empty(self):
        """测试空 diff"""
        result = generate_diffstat("")
        assert result == ""

        result = generate_diffstat("   \n\n   ")
        assert result == ""

    def test_generate_diffstat_multiple_files(self):
        """测试多文件 diffstat"""
        diff_content = """Index: file1.py
===================================================================
+line1
+line2
Index: file2.py
===================================================================
-old_line
+new_line
"""
        result = generate_diffstat(diff_content)

        assert "file1.py" in result
        assert "file2.py" in result
        assert "2 file(s) changed" in result


# ---------- 降级处理集成测试 ----------


class TestDegradedPatchHandling:
    """测试降级处理逻辑"""

    def test_svn_diff_timeout_triggers_degradation(self, mock_subprocess):
        """测试 SVN diff 超时触发降级"""
        mock_subprocess.side_effect = subprocess.TimeoutExpired(
            cmd=["svn", "diff"],
            timeout=120,
        )

        result = fetch_svn_diff("svn://example.com/repo", revision=100)

        # 验证返回降级结果而非抛出异常
        assert result.success is False
        assert result.error_category == "timeout"
        # 可用于生成 ministat 的降级
        assert result.error is not None

    def test_patch_fetch_error_hierarchy(self):
        """测试异常类层次结构"""
        # 所有具体异常应继承自 PatchFetchError
        assert issubclass(PatchFetchTimeoutError, PatchFetchError)
        assert issubclass(PatchFetchContentTooLargeError, PatchFetchError)
        assert issubclass(PatchFetchCommandError, PatchFetchError)

        # 验证 error_category 属性
        assert PatchFetchTimeoutError.error_category == "timeout"
        assert PatchFetchContentTooLargeError.error_category == "content_too_large"
        assert PatchFetchCommandError.error_category == "command_error"


# ---------- GitLab MR 游标 Tie-Break 测试 ----------


class TestMRCursorTieBreak:
    """
    测试多个 MR 具有相同 updated_at 时的游标推进逻辑

    验证:
    1. 游标推进使用 (updated_at, iid) 作为复合水位线
    2. 相同 updated_at 时按 iid 升序排列，取最大 iid 作为新游标
    3. 重复执行时不遗漏、不重复
    """

    def test_same_updated_at_different_iids_cursor_advances(self):
        """相同 updated_at、不同 iid 时，游标应推进到最大 iid"""
        # 模拟 3 个 MR 具有相同的 updated_at
        same_ts = "2024-01-15T12:00:00Z"
        mrs = [
            {"iid": 100, "updated_at": same_ts},
            {"iid": 101, "updated_at": same_ts},
            {"iid": 102, "updated_at": same_ts},
        ]

        # 按 (updated_at, iid) 排序后取最大
        sorted_mrs = sorted(
            mrs,
            key=lambda m: (m["updated_at"], m["iid"]),
            reverse=True,
        )
        latest = sorted_mrs[0]

        # 验证最大 iid 被选中
        assert latest["iid"] == 102
        assert latest["updated_at"] == same_ts

        # 验证游标推进逻辑
        # 初始状态：无游标
        assert should_advance_mr_cursor(same_ts, 102, None, None) is True

        # 游标已设置为 (same_ts, 100)，处理到 102 时应推进
        assert (
            should_advance_mr_cursor(
                same_ts,
                102,
                same_ts,
                100,
            )
            is True
        )

        # 游标已设置为 (same_ts, 102)，处理到 101 时不应推进
        assert (
            should_advance_mr_cursor(
                same_ts,
                101,
                same_ts,
                102,
            )
            is False
        )

    def test_batch_processing_with_same_updated_at(self):
        """批量处理时，正确追踪最大 (updated_at, iid)"""
        # 模拟一批 MR，部分具有相同 updated_at
        mrs = [
            {"iid": 50, "updated_at": "2024-01-14T10:00:00Z"},
            {"iid": 51, "updated_at": "2024-01-15T12:00:00Z"},  # 相同时间戳
            {"iid": 52, "updated_at": "2024-01-15T12:00:00Z"},  # 相同时间戳
            {"iid": 53, "updated_at": "2024-01-15T12:00:00Z"},  # 相同时间戳
            {"iid": 54, "updated_at": "2024-01-15T11:00:00Z"},  # 较早时间
        ]

        # 模拟游标推进逻辑
        last_updated_at = None
        last_iid = None

        for mr in mrs:
            if should_advance_mr_cursor(
                mr["updated_at"],
                mr["iid"],
                last_updated_at,
                last_iid,
            ):
                last_updated_at = mr["updated_at"]
                last_iid = mr["iid"]

        # 最终游标应该是 (2024-01-15T12:00:00Z, 53)
        assert last_updated_at == "2024-01-15T12:00:00Z"
        assert last_iid == 53

    def test_repeat_execution_no_missing_no_duplicate(self):
        """重复执行时不遗漏、不重复"""
        same_ts = "2024-01-15T12:00:00Z"

        # 第一次执行：处理 iid 100, 101, 102
        first_batch = [100, 101, 102]
        processed_first = set()

        last_updated_at = None
        last_iid = None

        for iid in first_batch:
            processed_first.add(iid)
            if should_advance_mr_cursor(same_ts, iid, last_updated_at, last_iid):
                last_updated_at = same_ts
                last_iid = iid

        # 验证第一次执行后游标
        assert last_updated_at == same_ts
        assert last_iid == 102
        assert processed_first == {100, 101, 102}

        # 第二次执行：应用 overlap 回退后，API 返回 iid 101, 102, 103, 104
        # （overlap 回退导致 101, 102 重复出现）
        second_batch = [101, 102, 103, 104]
        processed_second = set()

        for iid in second_batch:
            # 只处理 iid > last_iid 或 updated_at > last_updated_at 的 MR
            # 这里模拟：如果 iid <= last_iid 且 updated_at == last_updated_at，跳过
            if iid > last_iid or same_ts > last_updated_at:
                processed_second.add(iid)
                if should_advance_mr_cursor(same_ts, iid, last_updated_at, last_iid):
                    last_updated_at = same_ts
                    last_iid = iid

        # 验证第二次执行只处理了新的 MR
        assert processed_second == {103, 104}
        assert last_iid == 104

    def test_cursor_comparison_with_timezone_variations(self):
        """测试时间戳带不同时区格式的比较（Z 与 +00:00 应等价）"""
        # ISO 8601 格式比较
        ts_z = "2024-01-15T12:00:00Z"
        ts_offset = "2024-01-15T12:00:00+00:00"  # 等价于 Z
        ts_later = "2024-01-15T12:00:01Z"  # 晚 1 秒

        # 首次同步
        assert should_advance_mr_cursor(ts_z, 100, None, None) is True

        # 晚 1 秒应推进
        assert should_advance_mr_cursor(ts_later, 100, ts_z, 100) is True

        # 同时间戳但 iid 更大应推进
        assert should_advance_mr_cursor(ts_z, 101, ts_z, 100) is True

        # Z 与 +00:00 应等价：相同时间戳、相同 iid 不推进
        assert should_advance_mr_cursor(ts_z, 100, ts_offset, 100) is False
        assert should_advance_mr_cursor(ts_offset, 100, ts_z, 100) is False

        # Z 与 +00:00 等价：相同时间戳、不同 iid 按 iid 比较
        assert should_advance_mr_cursor(ts_z, 101, ts_offset, 100) is True
        assert should_advance_mr_cursor(ts_offset, 99, ts_z, 100) is False


class TestMRPaginationDedup:
    """
    测试分页导致重复 MR（跨页重叠）时的本地去重策略

    验证:
    1. GitLab API 分页可能导致跨页重复
    2. 本地应使用 set/dict 去重
    3. 数据库 upsert 保证最终一致性
    """

    def test_pagination_overlap_local_dedup(self):
        """分页重叠时本地去重正确"""
        # 模拟 GitLab API 分页响应
        # 第一页: iid 1, 2, 3
        page1 = [
            {"iid": 1, "updated_at": "2024-01-10T10:00:00Z"},
            {"iid": 2, "updated_at": "2024-01-11T10:00:00Z"},
            {"iid": 3, "updated_at": "2024-01-12T10:00:00Z"},
        ]

        # 第二页: iid 3, 4, 5 (iid 3 重复)
        page2 = [
            {"iid": 3, "updated_at": "2024-01-12T10:00:00Z"},  # 重复
            {"iid": 4, "updated_at": "2024-01-13T10:00:00Z"},
            {"iid": 5, "updated_at": "2024-01-14T10:00:00Z"},
        ]

        # 本地去重策略：使用 dict 按 iid 去重
        seen_iids = {}

        for mr in page1 + page2:
            iid = mr["iid"]
            if iid not in seen_iids:
                seen_iids[iid] = mr
            # 如果已存在，可以选择更新或跳过
            # 这里模拟保留第一次出现的

        # 验证去重结果
        assert len(seen_iids) == 5
        assert set(seen_iids.keys()) == {1, 2, 3, 4, 5}

    def test_pagination_overlap_with_updated_data(self):
        """分页重叠时，后续页面的数据可能更新"""
        # 第一页: iid 3 的旧数据
        page1 = [
            {"iid": 3, "updated_at": "2024-01-12T10:00:00Z", "title": "Old Title"},
        ]

        # 第二页: iid 3 的新数据（在分页期间被更新）
        page2 = [
            {"iid": 3, "updated_at": "2024-01-12T10:30:00Z", "title": "New Title"},
        ]

        # 策略：保留 updated_at 更新的版本
        seen = {}

        for mr in page1 + page2:
            iid = mr["iid"]
            if iid not in seen:
                seen[iid] = mr
            else:
                # 比较 updated_at，保留更新的
                if mr["updated_at"] > seen[iid]["updated_at"]:
                    seen[iid] = mr

        # 验证保留了新版本
        assert seen[3]["title"] == "New Title"
        assert seen[3]["updated_at"] == "2024-01-12T10:30:00Z"

    def test_pagination_dedup_with_tie_break(self):
        """分页去重结合 tie-break 策略"""
        # 模拟 3 页数据，存在跨页重叠
        pages = [
            # Page 1
            [
                {"iid": 10, "updated_at": "2024-01-15T12:00:00Z"},
                {"iid": 11, "updated_at": "2024-01-15T12:00:00Z"},
            ],
            # Page 2 (与 Page 1 有重叠)
            [
                {"iid": 11, "updated_at": "2024-01-15T12:00:00Z"},  # 重复
                {"iid": 12, "updated_at": "2024-01-15T12:00:00Z"},
            ],
            # Page 3
            [
                {"iid": 13, "updated_at": "2024-01-15T12:00:00Z"},
            ],
        ]

        # 去重并收集所有 MR
        all_mrs = {}
        for page in pages:
            for mr in page:
                iid = mr["iid"]
                if iid not in all_mrs:
                    all_mrs[iid] = mr

        # 验证去重
        assert len(all_mrs) == 4
        assert set(all_mrs.keys()) == {10, 11, 12, 13}

        # 计算最终游标（最大 updated_at + iid）
        sorted_mrs = sorted(
            all_mrs.values(),
            key=lambda m: (m["updated_at"], m["iid"]),
            reverse=True,
        )
        final_cursor = sorted_mrs[0]

        assert final_cursor["iid"] == 13
        assert final_cursor["updated_at"] == "2024-01-15T12:00:00Z"


class TestOverlapIdempotency:
    """
    测试 overlap_seconds 回退后重复扫描仍保持幂等

    验证:
    1. mrs upsert 幂等（ON CONFLICT DO UPDATE）
    2. review_events unique 约束幂等（ON CONFLICT DO NOTHING）
    3. 重复扫描不会产生重复数据
    """

    def test_mrs_upsert_idempotent_on_overlap(self, db_conn):
        """MRs upsert 在 overlap 重复扫描时保持幂等"""
        from db import upsert_mr, upsert_repo
        from scm_repo import build_mr_id

        # 创建 repo
        ts = datetime.now().timestamp()
        url = f"https://gitlab.example.com/test/overlap-idempotent-{ts}"
        repo_id = upsert_repo(db_conn, "git", url, "test_project")
        db_conn.commit()

        # 模拟第一次同步：3 个 MR
        mr_data_v1 = [
            {"iid": 100, "status": "open", "title": "MR 100 v1"},
            {"iid": 101, "status": "open", "title": "MR 101 v1"},
            {"iid": 102, "status": "open", "title": "MR 102 v1"},
        ]

        for mr in mr_data_v1:
            mr_id = build_mr_id(repo_id, mr["iid"])
            upsert_mr(db_conn, mr_id, repo_id, status=mr["status"])
        db_conn.commit()

        # 验证第一次插入
        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM mrs WHERE repo_id = %s", (repo_id,))
            count1 = cur.fetchone()[0]
            assert count1 == 3

        # 模拟第二次同步（overlap 回退导致 100, 101 重复）
        # 同时 101 状态更新为 merged
        mr_data_v2 = [
            {"iid": 100, "status": "open", "title": "MR 100 v2"},  # 重复，无变化
            {"iid": 101, "status": "merged", "title": "MR 101 v2"},  # 重复，状态更新
            {"iid": 103, "status": "open", "title": "MR 103 v2"},  # 新 MR
        ]

        for mr in mr_data_v2:
            mr_id = build_mr_id(repo_id, mr["iid"])
            upsert_mr(db_conn, mr_id, repo_id, status=mr["status"])
        db_conn.commit()

        # 验证幂等性：总数应该是 4 (100, 101, 102, 103)
        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM mrs WHERE repo_id = %s", (repo_id,))
            count2 = cur.fetchone()[0]
            assert count2 == 4, f"应该有 4 个 MR，实际 {count2}"

        # 验证 101 的状态被更新
        with db_conn.cursor() as cur:
            mr_id_101 = build_mr_id(repo_id, 101)
            cur.execute("SELECT status FROM mrs WHERE mr_id = %s", (mr_id_101,))
            status = cur.fetchone()[0]
            assert status == "merged", f"MR 101 状态应该是 merged，实际 {status}"

    def test_review_events_unique_on_overlap(self, db_conn):
        """review_events 在 overlap 重复扫描时保持幂等"""
        from db import insert_review_event, upsert_mr, upsert_repo

        # 创建 repo 和 MR
        ts = datetime.now().timestamp()
        url = f"https://gitlab.example.com/test/review-overlap-{ts}"
        repo_id = upsert_repo(db_conn, "git", url, "test_project")
        db_conn.commit()

        mr_id = f"{repo_id}:{int(ts)}"
        upsert_mr(db_conn, mr_id, repo_id, status="open")
        db_conn.commit()

        # 模拟第一次同步：3 个事件
        events_v1 = [
            {"source_event_id": f"note:overlap-1-{ts}", "event_type": "comment"},
            {"source_event_id": f"note:overlap-2-{ts}", "event_type": "comment"},
            {"source_event_id": f"note:overlap-3-{ts}", "event_type": "approve"},
        ]

        first_inserted = 0
        for event in events_v1:
            event_id = insert_review_event(
                db_conn,
                mr_id,
                event_type=event["event_type"],
                source_event_id=event["source_event_id"],
                reviewer_user_id=None,
            )
            if event_id:
                first_inserted += 1
        db_conn.commit()

        assert first_inserted == 3

        # 模拟第二次同步（overlap 回退导致事件 2, 3 重复）
        events_v2 = [
            {"source_event_id": f"note:overlap-2-{ts}", "event_type": "comment"},  # 重复
            {"source_event_id": f"note:overlap-3-{ts}", "event_type": "approve"},  # 重复
            {"source_event_id": f"note:overlap-4-{ts}", "event_type": "comment"},  # 新事件
        ]

        second_inserted = 0
        second_skipped = 0
        for event in events_v2:
            event_id = insert_review_event(
                db_conn,
                mr_id,
                event_type=event["event_type"],
                source_event_id=event["source_event_id"],
                reviewer_user_id=None,
            )
            if event_id:
                second_inserted += 1
            else:
                second_skipped += 1
        db_conn.commit()

        # 验证幂等性：只有 1 个新事件插入，2 个跳过
        assert second_inserted == 1, f"应该插入 1 个新事件，实际 {second_inserted}"
        assert second_skipped == 2, f"应该跳过 2 个重复事件，实际 {second_skipped}"

        # 验证总数为 4
        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM review_events WHERE mr_id = %s", (mr_id,))
            total = cur.fetchone()[0]
            assert total == 4, f"应该有 4 个事件，实际 {total}"

    def test_overlap_seconds_calculation(self):
        """测试 overlap_seconds 回退计算"""
        from datetime import timedelta

        # 游标时间
        cursor_time_str = "2024-01-15T12:00:00Z"
        cursor_time = datetime.fromisoformat(cursor_time_str.replace("Z", "+00:00"))

        # 不同的 overlap_seconds 值
        test_cases = [
            (0, "2024-01-15T12:00:00Z"),  # 无回退
            (60, "2024-01-15T11:59:00Z"),  # 回退 1 分钟
            (300, "2024-01-15T11:55:00Z"),  # 回退 5 分钟
            (3600, "2024-01-15T11:00:00Z"),  # 回退 1 小时
        ]

        for overlap_seconds, expected_start in test_cases:
            start_time = cursor_time - timedelta(seconds=overlap_seconds)
            start_str = start_time.isoformat().replace("+00:00", "Z")

            # 由于浮点精度问题，比较前缀
            assert start_str.startswith(expected_start[:19]), (
                f"overlap={overlap_seconds}s: 期望 {expected_start}, 实际 {start_str}"
            )

    def test_overlap_with_cursor_tie_break_integration(self):
        """测试 overlap 回退与 tie-break 集成"""
        # 场景：
        # 1. 第一次同步到游标 (2024-01-15T12:00:00Z, iid=102)
        # 2. overlap 回退 5 分钟后，API 返回 iid 100, 101, 102, 103
        # 3. 应该只处理 iid 103（因为 iid <= 102 且 updated_at 相同应跳过）

        cursor_updated_at = "2024-01-15T12:00:00Z"
        cursor_iid = 102

        # 模拟 API 返回的 MR 列表（overlap 导致的重复）
        api_response = [
            {"iid": 100, "updated_at": "2024-01-15T11:55:00Z"},  # 早于游标
            {"iid": 101, "updated_at": "2024-01-15T11:58:00Z"},  # 早于游标
            {"iid": 102, "updated_at": "2024-01-15T12:00:00Z"},  # 等于游标
            {"iid": 103, "updated_at": "2024-01-15T12:00:00Z"},  # 等于游标但 iid 更大
            {"iid": 104, "updated_at": "2024-01-15T12:05:00Z"},  # 晚于游标
        ]

        # 判断哪些 MR 需要处理
        to_process = []
        for mr in api_response:
            # 使用 should_advance_mr_cursor 的逻辑判断
            if should_advance_mr_cursor(
                mr["updated_at"],
                mr["iid"],
                cursor_updated_at,
                cursor_iid,
            ):
                to_process.append(mr["iid"])

        # 应该处理 iid 103 和 104
        assert to_process == [103, 104], f"应该处理 [103, 104]，实际 {to_process}"


class TestMRSyncCursorEdgeCases:
    """测试 MR 同步游标边界情况"""

    def test_empty_batch_no_cursor_update(self):
        """空批次不应更新游标"""
        last_updated_at = "2024-01-15T12:00:00Z"
        last_iid = 100

        # 空批次
        batch = []

        # 不应有任何游标更新
        new_updated_at = last_updated_at
        new_iid = last_iid

        for mr in batch:
            if should_advance_mr_cursor(
                mr.get("updated_at"),
                mr.get("iid"),
                new_updated_at,
                new_iid,
            ):
                new_updated_at = mr["updated_at"]
                new_iid = mr["iid"]

        # 游标应保持不变
        assert new_updated_at == last_updated_at
        assert new_iid == last_iid

    def test_all_old_mrs_no_cursor_update(self):
        """所有 MR 都旧于游标时不应更新"""
        last_updated_at = "2024-01-15T12:00:00Z"
        last_iid = 100

        # 所有 MR 都早于游标
        batch = [
            {"iid": 50, "updated_at": "2024-01-14T10:00:00Z"},
            {"iid": 60, "updated_at": "2024-01-14T11:00:00Z"},
            {"iid": 70, "updated_at": "2024-01-14T12:00:00Z"},
        ]

        new_updated_at = last_updated_at
        new_iid = last_iid

        for mr in batch:
            if should_advance_mr_cursor(
                mr["updated_at"],
                mr["iid"],
                new_updated_at,
                new_iid,
            ):
                new_updated_at = mr["updated_at"]
                new_iid = mr["iid"]

        # 游标应保持不变
        assert new_updated_at == last_updated_at
        assert new_iid == last_iid

    def test_single_mr_at_cursor_boundary(self):
        """单个 MR 恰好在游标边界时的处理"""
        last_updated_at = "2024-01-15T12:00:00Z"
        last_iid = 100

        # 恰好等于游标
        mr_equal = {"iid": 100, "updated_at": "2024-01-15T12:00:00Z"}
        assert (
            should_advance_mr_cursor(
                mr_equal["updated_at"],
                mr_equal["iid"],
                last_updated_at,
                last_iid,
            )
            is False
        )

        # 同时间但 iid 更大
        mr_higher_iid = {"iid": 101, "updated_at": "2024-01-15T12:00:00Z"}
        assert (
            should_advance_mr_cursor(
                mr_higher_iid["updated_at"],
                mr_higher_iid["iid"],
                last_updated_at,
                last_iid,
            )
            is True
        )

        # 同 iid 但时间更新
        mr_newer_time = {"iid": 100, "updated_at": "2024-01-15T12:00:01Z"}
        assert (
            should_advance_mr_cursor(
                mr_newer_time["updated_at"],
                mr_newer_time["iid"],
                last_updated_at,
                last_iid,
            )
            is True
        )


class TestReviewsOverlapIdempotency:
    """测试 Reviews 同步在 overlap 重复扫描时的幂等性"""

    def test_reviews_cursor_with_event_level_watermark(self):
        """测试事件级水位线（可选扩展）"""
        # 这是一个概念验证测试
        # 当前实现使用 MR 级别的水位线 (updated_at, iid)
        # 事件级水位线 last_event_ts 是可选的扩展

        cursor_data = {
            "version": 2,
            "watermark": {
                "last_mr_updated_at": "2024-01-15T12:00:00Z",
                "last_mr_iid": 100,
                "last_event_ts": "2024-01-15T12:30:00Z",  # 可选
            },
            "stats": {},
        }

        cursor = Cursor.from_dict(cursor_data)

        assert cursor.last_mr_updated_at == "2024-01-15T12:00:00Z"
        assert cursor.last_mr_iid == 100
        assert cursor.last_event_ts == "2024-01-15T12:30:00Z"

    def test_multi_mr_review_events_overlap_dedup(self, db_conn):
        """多个 MR 的 review_events 在 overlap 时正确去重"""
        from db import insert_review_event, upsert_mr, upsert_repo

        # 创建 repo 和多个 MR
        ts = datetime.now().timestamp()
        url = f"https://gitlab.example.com/test/multi-mr-overlap-{ts}"
        repo_id = upsert_repo(db_conn, "git", url, "test_project")
        db_conn.commit()

        mr_id_1 = f"{repo_id}:{int(ts)}"
        mr_id_2 = f"{repo_id}:{int(ts) + 1}"
        upsert_mr(db_conn, mr_id_1, repo_id, status="open")
        upsert_mr(db_conn, mr_id_2, repo_id, status="open")
        db_conn.commit()

        # 第一次同步：MR1 和 MR2 各有事件
        events_run1 = [
            (mr_id_1, f"note:mr1-1-{ts}"),
            (mr_id_1, f"note:mr1-2-{ts}"),
            (mr_id_2, f"note:mr2-1-{ts}"),
        ]

        for mr_id, source_event_id in events_run1:
            insert_review_event(
                db_conn,
                mr_id,
                event_type="comment",
                source_event_id=source_event_id,
                reviewer_user_id=None,
            )
        db_conn.commit()

        # 第二次同步（overlap 导致部分重复）
        events_run2 = [
            (mr_id_1, f"note:mr1-2-{ts}"),  # 重复
            (mr_id_1, f"note:mr1-3-{ts}"),  # 新
            (mr_id_2, f"note:mr2-1-{ts}"),  # 重复
            (mr_id_2, f"note:mr2-2-{ts}"),  # 新
        ]

        inserted = 0
        skipped = 0
        for mr_id, source_event_id in events_run2:
            event_id = insert_review_event(
                db_conn,
                mr_id,
                event_type="comment",
                source_event_id=source_event_id,
                reviewer_user_id=None,
            )
            if event_id:
                inserted += 1
            else:
                skipped += 1
        db_conn.commit()

        # 验证
        assert inserted == 2, f"应该插入 2 个新事件，实际 {inserted}"
        assert skipped == 2, f"应该跳过 2 个重复事件，实际 {skipped}"

        # 验证各 MR 的事件总数
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT mr_id, COUNT(*) FROM review_events WHERE mr_id IN (%s, %s) GROUP BY mr_id ORDER BY mr_id",
                (mr_id_1, mr_id_2),
            )
            results = dict(cur.fetchall())
            assert results.get(mr_id_1) == 3, "MR1 应该有 3 个事件"
            assert results.get(mr_id_2) == 2, "MR2 应该有 2 个事件"


# ---------- Backfill CLI 参数测试 ----------


class TestSvnSyncRepeatExecution:
    """
    测试 SVN 同步重复执行时的幂等性

    验证:
    1. 重复执行同步时，已同步的 revision 不会重复插入
    2. 游标正确推进
    3. overlap 策略下 upsert 保持幂等
    """

    def test_sync_twice_no_duplicate_revisions(self, mock_subprocess):
        """重复执行同步时，相同 revision 通过 upsert 保持幂等"""
        from scm_sync_svn import (
            SyncConfig,
            sync_svn_revisions,
        )

        # 模拟游标存储（内存）
        cursor_storage = {}

        def mock_load_cursor(repo_id, config=None):
            from engram.logbook.cursor import CURSOR_VERSION, Cursor

            if repo_id in cursor_storage:
                return Cursor.from_dict(cursor_storage[repo_id])
            return Cursor(version=CURSOR_VERSION, watermark={}, stats={})

        def mock_save_cursor(repo_id, last_rev, synced_count, config=None):
            from datetime import timezone

            cursor_storage[repo_id] = {
                "version": 2,
                "watermark": {"last_rev": last_rev},
                "stats": {
                    "last_sync_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "last_sync_count": synced_count,
                },
            }
            return True

        # 模拟 SVN 日志返回
        svn_log_xml = """<?xml version="1.0" encoding="UTF-8"?>
<log>
<logentry revision="1">
<author>user1</author>
<date>2024-01-01T10:00:00Z</date>
<msg>Initial commit</msg>
</logentry>
<logentry revision="2">
<author>user2</author>
<date>2024-01-02T10:00:00Z</date>
<msg>Second commit</msg>
</logentry>
</log>"""

        svn_info_xml = """<?xml version="1.0" encoding="UTF-8"?>
<info><entry revision="2"></entry></info>"""

        def mock_run(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if "info" in cmd:
                return MagicMock(stdout=svn_info_xml, returncode=0)
            elif "log" in cmd:
                return MagicMock(stdout=svn_log_xml, returncode=0)
            return MagicMock(stdout="", returncode=0)

        mock_subprocess.side_effect = mock_run

        # 跟踪 insert 调用
        insert_calls = []

        def mock_insert_revisions(conn, repo_id, revisions, config=None):
            for rev in revisions:
                insert_calls.append(rev.revision)
            return len(revisions)

        sync_config = SyncConfig(
            svn_url="svn://example.com/repo",
            batch_size=100,
            overlap=0,
        )

        with patch("scm_sync_svn.ensure_repo", return_value=1):
            with patch("scm_sync_svn.get_connection") as mock_conn:
                mock_conn.return_value = MagicMock()
                mock_conn.return_value.close = MagicMock()
                mock_conn.return_value.commit = MagicMock()

                with patch("scm_sync_svn.load_svn_cursor", mock_load_cursor):
                    with patch("scm_sync_svn.save_svn_cursor", mock_save_cursor):
                        with patch("scm_sync_svn.insert_svn_revisions", mock_insert_revisions):
                            # 第一次同步
                            result1 = sync_svn_revisions(
                                sync_config,
                                project_key="test",
                                verbose=False,
                            )

                            assert result1["success"] is True
                            assert result1["synced_count"] == 2
                            assert len(insert_calls) == 2
                            assert set(insert_calls) == {1, 2}

                            # 验证游标已更新
                            assert 1 in cursor_storage
                            assert cursor_storage[1]["watermark"]["last_rev"] == 2

                            # 第二次同步（HEAD 未变化）
                            insert_calls.clear()
                            result2 = sync_svn_revisions(
                                sync_config,
                                project_key="test",
                                verbose=False,
                            )

                            # 应该没有新的同步（已是最新）
                            assert result2["success"] is True
                            assert result2.get("synced_count", 0) == 0 or "无需同步" in result2.get(
                                "message", ""
                            )
                            assert len(insert_calls) == 0, "不应有重复插入"

    def test_sync_with_overlap_upsert_idempotent(self, mock_subprocess):
        """使用 overlap 策略时，重复的 revision 通过 upsert 保持幂等"""
        from scm_sync_svn import (
            parse_svn_log_xml,
        )

        # 测试 upsert 幂等性的核心逻辑
        # 同一个 revision 多次 upsert 应该返回相同的 ID

        # 模拟第一次同步：revision 1, 2, 3
        xml_v1 = """<?xml version="1.0" encoding="UTF-8"?>
<log>
<logentry revision="1"><author>user1</author><date>2024-01-01T10:00:00Z</date><msg>Commit 1</msg></logentry>
<logentry revision="2"><author>user2</author><date>2024-01-02T10:00:00Z</date><msg>Commit 2</msg></logentry>
<logentry revision="3"><author>user3</author><date>2024-01-03T10:00:00Z</date><msg>Commit 3</msg></logentry>
</log>"""

        revisions = parse_svn_log_xml(xml_v1)
        assert len(revisions) == 3

        # 模拟第二次同步（overlap=2 导致 revision 2, 3 重复）
        xml_v2 = """<?xml version="1.0" encoding="UTF-8"?>
<log>
<logentry revision="2"><author>user2</author><date>2024-01-02T10:00:00Z</date><msg>Commit 2</msg></logentry>
<logentry revision="3"><author>user3</author><date>2024-01-03T10:00:00Z</date><msg>Commit 3</msg></logentry>
<logentry revision="4"><author>user4</author><date>2024-01-04T10:00:00Z</date><msg>Commit 4</msg></logentry>
</log>"""

        revisions_v2 = parse_svn_log_xml(xml_v2)
        assert len(revisions_v2) == 3

        # 验证 revision 2 和 3 在两个批次中都存在（模拟 overlap 回退）
        rev_nums_v1 = {r.revision for r in revisions}
        rev_nums_v2 = {r.revision for r in revisions_v2}
        overlap = rev_nums_v1 & rev_nums_v2

        assert overlap == {2, 3}, f"应该有 revision 2, 3 重叠，实际: {overlap}"

        # 合并所有 revision（去重后应该是 1, 2, 3, 4）
        all_revs = {r.revision for r in revisions + revisions_v2}
        assert all_revs == {1, 2, 3, 4}, f"合并后应该有 4 个 revision，实际: {all_revs}"

    def test_cursor_advances_to_max_revision(self, mock_subprocess):
        """游标应该推进到批次中最大的 revision"""
        from scm_sync_svn import parse_svn_log_xml

        # 模拟乱序返回的 revision（虽然 SVN 通常是有序的）
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<log>
<logentry revision="100"><author>user</author><date>2024-01-01T10:00:00Z</date><msg>Commit 100</msg></logentry>
<logentry revision="98"><author>user</author><date>2024-01-01T09:00:00Z</date><msg>Commit 98</msg></logentry>
<logentry revision="102"><author>user</author><date>2024-01-01T11:00:00Z</date><msg>Commit 102</msg></logentry>
</log>"""

        revisions = parse_svn_log_xml(xml)

        # 找到最大 revision
        max_rev = max(r.revision for r in revisions)
        assert max_rev == 102, f"最大 revision 应该是 102，实际: {max_rev}"

        # 验证 sync_svn_revisions 中的游标更新逻辑
        # (在 scm_sync_svn.py 第 1366-1368 行)
        # if revisions:
        #     max_rev = max(r.revision for r in revisions)
        #     save_svn_cursor(repo_id, max_rev, synced_count, config)


class TestSvnBackfillCLI:
    """测试 SVN backfill CLI 参数组合"""

    def test_backfill_does_not_update_watermark_by_default(self, mock_subprocess):
        """Backfill 默认不更新游标"""
        from scm_sync_svn import SyncConfig, backfill_svn_revisions

        # Mock subprocess 返回有效的 SVN log
        mock_subprocess.return_value = MagicMock(
            stdout="""<?xml version="1.0" encoding="UTF-8"?>
<log>
<logentry revision="100">
<author>testuser</author>
<date>2024-01-15T10:00:00Z</date>
<msg>Test commit</msg>
</logentry>
</log>""",
            returncode=0,
        )

        sync_config = SyncConfig(
            svn_url="svn://example.com/repo",
            batch_size=100,
        )

        # Mock 必要的依赖
        with patch("scm_sync_svn.ensure_repo", return_value=1):
            with patch("scm_sync_svn.get_connection") as mock_conn:
                mock_conn.return_value = MagicMock()
                mock_conn.return_value.__enter__ = MagicMock(return_value=mock_conn.return_value)
                mock_conn.return_value.__exit__ = MagicMock(return_value=False)
                mock_conn.return_value.cursor = MagicMock()
                mock_conn.return_value.commit = MagicMock()
                mock_conn.return_value.close = MagicMock()

                with patch("scm_sync_svn.insert_svn_revisions", return_value=1):
                    with patch("scm_sync_svn.save_svn_cursor") as mock_save_cursor:
                        result = backfill_svn_revisions(
                            sync_config,
                            project_key="test",
                            start_rev=100,
                            end_rev=100,
                            update_watermark=False,  # 默认不更新
                            dry_run=False,
                        )

                        # 验证不调用 save_svn_cursor
                        mock_save_cursor.assert_not_called()
                        assert result["watermark_updated"] is False

    def test_backfill_updates_watermark_when_flag_set(self, mock_subprocess):
        """Backfill 在 update_watermark=True 时更新游标"""
        from scm_sync_svn import SyncConfig, backfill_svn_revisions

        # Mock subprocess
        mock_subprocess.return_value = MagicMock(
            stdout="""<?xml version="1.0" encoding="UTF-8"?>
<log>
<logentry revision="100">
<author>testuser</author>
<date>2024-01-15T10:00:00Z</date>
<msg>Test commit</msg>
</logentry>
<logentry revision="101">
<author>testuser</author>
<date>2024-01-15T11:00:00Z</date>
<msg>Test commit 2</msg>
</logentry>
</log>""",
            returncode=0,
        )

        sync_config = SyncConfig(
            svn_url="svn://example.com/repo",
            batch_size=100,
        )

        with patch("scm_sync_svn.ensure_repo", return_value=1):
            with patch("scm_sync_svn.get_connection") as mock_conn:
                mock_conn.return_value = MagicMock()
                mock_conn.return_value.__enter__ = MagicMock(return_value=mock_conn.return_value)
                mock_conn.return_value.__exit__ = MagicMock(return_value=False)
                mock_conn.return_value.close = MagicMock()

                with patch("scm_sync_svn.insert_svn_revisions", return_value=2):
                    with patch("scm_sync_svn.save_svn_cursor") as mock_save_cursor:
                        result = backfill_svn_revisions(
                            sync_config,
                            project_key="test",
                            start_rev=100,
                            end_rev=101,
                            update_watermark=True,  # 明确要求更新
                            dry_run=False,
                        )

                        # 验证调用 save_svn_cursor
                        mock_save_cursor.assert_called_once()
                        assert result["watermark_updated"] is True
                        assert result["last_rev"] == 101

    def test_backfill_dry_run_no_db_write(self, mock_subprocess):
        """Backfill dry-run 模式不写入 DB"""
        from scm_sync_svn import SyncConfig, backfill_svn_revisions

        # Mock HEAD revision
        mock_subprocess.return_value = MagicMock(
            stdout="""<?xml version="1.0" encoding="UTF-8"?>
<info>
<entry revision="500">
</entry>
</info>""",
            returncode=0,
        )

        sync_config = SyncConfig(
            svn_url="svn://example.com/repo",
            batch_size=100,
        )

        with patch("scm_sync_svn.ensure_repo", return_value=1):
            with patch("scm_sync_svn.get_connection"):
                with patch("scm_sync_svn.insert_svn_revisions") as mock_insert:
                    result = backfill_svn_revisions(
                        sync_config,
                        project_key="test",
                        start_rev=100,
                        end_rev=200,
                        dry_run=True,  # dry-run 模式
                    )

                    # 验证不调用 insert_svn_revisions
                    mock_insert.assert_not_called()
                    assert result["dry_run"] is True
                    assert result["success"] is True
                    assert "dry-run" in result.get("message", "").lower()

    def test_backfill_range_validation(self):
        """Backfill 范围验证：start_rev > end_rev 报错"""
        from engram.logbook.errors import ValidationError
        from scm_sync_svn import SyncConfig, backfill_svn_revisions

        sync_config = SyncConfig(
            svn_url="svn://example.com/repo",
            batch_size=100,
        )

        with patch("scm_sync_svn.ensure_repo", return_value=1):
            with pytest.raises(ValidationError) as exc_info:
                backfill_svn_revisions(
                    sync_config,
                    project_key="test",
                    start_rev=200,
                    end_rev=100,  # 无效范围
                )

            assert "起始 revision" in str(exc_info.value)


class TestGitLabBackfillCLI:
    """测试 GitLab commits backfill CLI 参数组合"""

    def test_backfill_does_not_update_watermark_by_default(self):
        """Backfill 默认不更新游标"""
        from engram.logbook.scm_auth import TokenProvider
        from scm_sync_gitlab_commits import (
            DiffMode,
            SyncConfig,
            backfill_gitlab_commits,
        )

        # 创建 mock token provider
        mock_token_provider = MagicMock(spec=TokenProvider)
        mock_token_provider.get_token.return_value = "test-token"

        sync_config = SyncConfig(
            gitlab_url="https://gitlab.example.com",
            project_id="123",
            token_provider=mock_token_provider,
            batch_size=100,
            diff_mode=DiffMode.NONE,
        )

        # Mock GitLabClient
        with patch("scm_sync_gitlab_commits.GitLabClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.get_commits.return_value = [
                {
                    "id": "abc123",
                    "author_name": "Test User",
                    "author_email": "test@example.com",
                    "committed_date": "2024-01-15T10:00:00Z",
                    "message": "Test commit",
                    "parent_ids": [],
                    "stats": {"additions": 10, "deletions": 5},
                }
            ]
            mock_client_class.return_value = mock_client

            with patch("scm_sync_gitlab_commits.scm_repo.ensure_repo", return_value=1):
                with patch("scm_sync_gitlab_commits.get_connection") as mock_conn:
                    mock_conn.return_value = MagicMock()
                    mock_conn.return_value.close = MagicMock()

                    with patch("scm_sync_gitlab_commits.insert_git_commits", return_value=1):
                        with patch(
                            "scm_sync_gitlab_commits.save_gitlab_cursor"
                        ) as mock_save_cursor:
                            result = backfill_gitlab_commits(
                                sync_config,
                                project_key="test",
                                since="2024-01-01",
                                until="2024-01-31",
                                update_watermark=False,  # 默认不更新
                                dry_run=False,
                                fetch_diffs=False,
                            )

                            # 验证不调用 save_gitlab_cursor
                            mock_save_cursor.assert_not_called()
                            assert result["watermark_updated"] is False

    def test_backfill_updates_watermark_when_flag_set(self):
        """Backfill 在 update_watermark=True 时更新游标"""
        from engram.logbook.scm_auth import TokenProvider
        from scm_sync_gitlab_commits import (
            DiffMode,
            SyncConfig,
            backfill_gitlab_commits,
        )

        mock_token_provider = MagicMock(spec=TokenProvider)
        mock_token_provider.get_token.return_value = "test-token"

        sync_config = SyncConfig(
            gitlab_url="https://gitlab.example.com",
            project_id="123",
            token_provider=mock_token_provider,
            batch_size=100,
            diff_mode=DiffMode.NONE,
        )

        with patch("scm_sync_gitlab_commits.GitLabClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.get_commits.return_value = [
                {
                    "id": "abc123",
                    "author_name": "Test User",
                    "author_email": "test@example.com",
                    "committed_date": "2024-01-15T10:00:00Z",
                    "message": "Test commit",
                    "parent_ids": [],
                    "stats": {"additions": 10, "deletions": 5},
                },
                {
                    "id": "def456",
                    "author_name": "Test User",
                    "author_email": "test@example.com",
                    "committed_date": "2024-01-16T10:00:00Z",
                    "message": "Test commit 2",
                    "parent_ids": ["abc123"],
                    "stats": {"additions": 20, "deletions": 10},
                },
            ]
            mock_client_class.return_value = mock_client

            with patch("scm_sync_gitlab_commits.scm_repo.ensure_repo", return_value=1):
                with patch("scm_sync_gitlab_commits.get_connection") as mock_conn:
                    mock_conn.return_value = MagicMock()
                    mock_conn.return_value.close = MagicMock()

                    with patch("scm_sync_gitlab_commits.insert_git_commits", return_value=2):
                        with patch(
                            "scm_sync_gitlab_commits.update_sync_cursor"
                        ) as mock_update_cursor:
                            result = backfill_gitlab_commits(
                                sync_config,
                                project_key="test",
                                since="2024-01-01",
                                until="2024-01-31",
                                update_watermark=True,  # 明确要求更新
                                dry_run=False,
                                fetch_diffs=False,
                            )

                            # 验证调用 update_sync_cursor
                            mock_update_cursor.assert_called_once()
                            assert result["watermark_updated"] is True
                            assert result["last_commit_sha"] == "def456"

    def test_backfill_dry_run_no_db_write(self):
        """Backfill dry-run 模式不写入 DB"""
        from engram.logbook.scm_auth import TokenProvider
        from scm_sync_gitlab_commits import (
            DiffMode,
            SyncConfig,
            backfill_gitlab_commits,
        )

        mock_token_provider = MagicMock(spec=TokenProvider)
        mock_token_provider.get_token.return_value = "test-token"

        sync_config = SyncConfig(
            gitlab_url="https://gitlab.example.com",
            project_id="123",
            token_provider=mock_token_provider,
            batch_size=100,
            diff_mode=DiffMode.NONE,
        )

        with patch("scm_sync_gitlab_commits.GitLabClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.get_commits.return_value = [{"id": "abc123"}]
            mock_client_class.return_value = mock_client

            with patch("scm_sync_gitlab_commits.scm_repo.ensure_repo", return_value=1):
                with patch("scm_sync_gitlab_commits.insert_git_commits") as mock_insert:
                    result = backfill_gitlab_commits(
                        sync_config,
                        project_key="test",
                        since="2024-01-01",
                        until="2024-01-31",
                        dry_run=True,  # dry-run 模式
                    )

                    # 验证不调用 insert_git_commits
                    mock_insert.assert_not_called()
                    assert result["dry_run"] is True
                    assert result["success"] is True


class TestBackfillCursorIntegration:
    """Backfill 与游标的集成测试"""

    def test_backfill_preserves_existing_cursor(self):
        """Backfill 不更新游标时应保留现有游标值"""
        from engram.logbook.cursor import (
            CURSOR_VERSION,
            Cursor,
        )

        # 模拟已存在的游标
        existing_cursor = Cursor(
            version=CURSOR_VERSION,
            watermark={"last_rev": 500},
            stats={"last_sync_at": "2024-01-10T10:00:00Z", "last_sync_count": 50},
        )

        # Backfill 范围 100-200（早于现有游标）
        # 不带 update_watermark，游标应保持不变

        # 这个测试验证游标数据结构
        assert existing_cursor.last_rev == 500
        assert existing_cursor.last_sync_at == "2024-01-10T10:00:00Z"

        # 模拟 backfill 后，如果不更新游标，值应保持不变
        # 实际测试需要在集成测试中完成

    def test_backfill_with_update_watermark_advances_cursor(self):
        """Backfill 使用 update_watermark 时应推进游标"""
        from engram.logbook.cursor import (
            should_advance_gitlab_commit_cursor,
        )

        # 现有游标
        existing_ts = "2024-01-10T10:00:00Z"
        existing_sha = "aaa111"

        # Backfill 的最后一个 commit
        new_ts = "2024-01-15T10:00:00Z"
        new_sha = "bbb222"

        # 验证应该推进
        assert (
            should_advance_gitlab_commit_cursor(new_ts, new_sha, existing_ts, existing_sha) is True
        )

    def test_backfill_older_range_no_cursor_regression(self):
        """Backfill 较旧范围时游标不应回退"""
        from engram.logbook.cursor import should_advance_gitlab_commit_cursor

        # 现有游标指向较新的位置
        existing_ts = "2024-01-15T10:00:00Z"
        existing_sha = "bbb222"

        # Backfill 的最后一个 commit 较旧
        new_ts = "2024-01-10T10:00:00Z"
        new_sha = "aaa111"

        # 验证不应推进（防止游标回退）
        assert (
            should_advance_gitlab_commit_cursor(new_ts, new_sha, existing_ts, existing_sha) is False
        )


class TestBackfillArgumentValidation:
    """Backfill 参数验证测试"""

    def test_svn_backfill_requires_start_rev(self):
        """SVN backfill 需要 --start-rev 参数"""
        import sys

        from scm_sync_svn import parse_args

        # 模拟命令行参数
        test_args = ["scm_sync_svn.py", "--backfill"]

        with patch.object(sys, "argv", test_args):
            args = parse_args()
            assert args.backfill is True
            assert args.start_rev is None  # 应该是 None，由 main() 验证

    def test_gitlab_backfill_requires_since(self):
        """GitLab backfill 需要 --since 参数"""
        import sys

        from scm_sync_gitlab_commits import parse_args

        test_args = ["scm_sync_gitlab_commits.py", "--backfill"]

        with patch.object(sys, "argv", test_args):
            args = parse_args()
            assert args.backfill is True
            assert args.since is None  # 应该是 None，由 main() 验证

    def test_dry_run_only_valid_in_backfill_mode(self):
        """--dry-run 仅在 --backfill 模式下有效"""
        import sys

        from scm_sync_svn import parse_args

        # dry-run 不带 backfill 应该被解析（验证由 main() 处理）
        test_args = ["scm_sync_svn.py", "--dry-run"]

        with patch.object(sys, "argv", test_args):
            args = parse_args()
            assert args.dry_run is True
            assert args.backfill is False

    def test_update_watermark_only_valid_in_backfill_mode(self):
        """--update-watermark 仅在 --backfill 模式下有效"""
        import sys

        from scm_sync_svn import parse_args

        test_args = ["scm_sync_svn.py", "--backfill", "--start-rev", "100", "--update-watermark"]

        with patch.object(sys, "argv", test_args):
            args = parse_args()
            assert args.backfill is True
            assert args.start_rev == 100
            assert args.update_watermark is True

    def test_svn_backfill_end_rev_defaults_to_head(self):
        """SVN backfill --end-rev 默认为 HEAD"""
        import sys

        from scm_sync_svn import parse_args

        test_args = ["scm_sync_svn.py", "--backfill", "--start-rev", "100"]

        with patch.object(sys, "argv", test_args):
            args = parse_args()
            assert args.start_rev == 100
            assert args.end_rev is None  # 由 backfill 函数处理为 HEAD

    def test_gitlab_backfill_until_defaults_to_now(self):
        """GitLab backfill --until 默认为当前时间"""
        import sys

        from scm_sync_gitlab_commits import parse_args

        test_args = ["scm_sync_gitlab_commits.py", "--backfill", "--since", "2024-01-01"]

        with patch.object(sys, "argv", test_args):
            args = parse_args()
            assert args.since == "2024-01-01"
            assert args.until is None  # 由 backfill 函数处理为当前时间


class TestBackfillWithPatches:
    """Backfill 与 patch 同步测试"""

    def test_svn_backfill_with_fetch_patches(self, mock_subprocess):
        """SVN backfill 可以同时获取 patches"""
        from scm_sync_svn import SyncConfig, backfill_svn_revisions

        mock_subprocess.return_value = MagicMock(
            stdout="""<?xml version="1.0" encoding="UTF-8"?>
<log>
<logentry revision="100">
<author>testuser</author>
<date>2024-01-15T10:00:00Z</date>
<msg>Test commit</msg>
<paths>
<path action="M" kind="file">/trunk/src/main.py</path>
</paths>
</logentry>
</log>""",
            returncode=0,
        )

        sync_config = SyncConfig(
            svn_url="svn://example.com/repo",
            batch_size=100,
        )

        with patch("scm_sync_svn.ensure_repo", return_value=1):
            with patch("scm_sync_svn.get_connection") as mock_conn:
                mock_conn.return_value = MagicMock()
                mock_conn.return_value.close = MagicMock()
                mock_conn.return_value.commit = MagicMock()

                with patch("scm_sync_svn.insert_svn_revisions", return_value=1):
                    with patch("scm_sync_svn.sync_patches_for_revisions") as mock_sync_patches:
                        mock_sync_patches.return_value = {
                            "total": 1,
                            "success": 1,
                            "failed": 0,
                            "skipped": 0,
                            "bulk_count": 0,
                            "patches": [],
                        }

                        result = backfill_svn_revisions(
                            sync_config,
                            project_key="test",
                            start_rev=100,
                            end_rev=100,
                            fetch_patches=True,  # 同时获取 patches
                            dry_run=False,
                        )

                        # 验证调用 sync_patches_for_revisions
                        mock_sync_patches.assert_called_once()
                        assert "patch_stats" in result


# ---------- 时间戳解析与标准化测试 ----------


class TestParseIsoTs:
    """测试 parse_iso_ts 函数"""

    def test_parse_z_format(self):
        """解析 Z 结尾的时间戳"""
        result = parse_iso_ts("2024-01-15T12:00:00Z")
        assert result is not None
        assert result.year == 2024
        assert result.month == 1
        assert result.day == 15
        assert result.hour == 12
        assert result.minute == 0
        assert result.second == 0
        assert result.tzinfo is not None

    def test_parse_offset_format(self):
        """解析 +00:00 结尾的时间戳"""
        result = parse_iso_ts("2024-01-15T12:00:00+00:00")
        assert result is not None
        assert result.year == 2024
        assert result.hour == 12
        assert result.tzinfo is not None

    def test_parse_with_microseconds(self):
        """解析带微秒的时间戳"""
        result = parse_iso_ts("2024-01-15T12:00:00.123456Z")
        assert result is not None
        assert result.microsecond == 123456

    def test_parse_none_returns_none(self):
        """None 输入返回 None"""
        assert parse_iso_ts(None) is None

    def test_parse_empty_returns_none(self):
        """空字符串返回 None"""
        assert parse_iso_ts("") is None

    def test_parse_invalid_returns_none(self):
        """无效格式返回 None"""
        assert parse_iso_ts("invalid-timestamp") is None

    def test_z_and_offset_are_equal(self):
        """Z 和 +00:00 应解析为相等的时间"""
        ts_z = parse_iso_ts("2024-01-15T12:00:00Z")
        ts_offset = parse_iso_ts("2024-01-15T12:00:00+00:00")
        assert ts_z == ts_offset


class TestNormalizeIsoTsZ:
    """测试 normalize_iso_ts_z 函数"""

    def test_normalize_z_unchanged(self):
        """Z 结尾的时间戳保持不变"""
        result = normalize_iso_ts_z("2024-01-15T12:00:00Z")
        assert result == "2024-01-15T12:00:00Z"

    def test_normalize_offset_to_z(self):
        """+00:00 结尾转换为 Z"""
        result = normalize_iso_ts_z("2024-01-15T12:00:00+00:00")
        assert result == "2024-01-15T12:00:00Z"

    def test_normalize_with_microseconds(self):
        """带微秒的时间戳正常标准化"""
        result = normalize_iso_ts_z("2024-01-15T12:00:00.123456+00:00")
        assert result == "2024-01-15T12:00:00.123456Z"

    def test_normalize_none_returns_none(self):
        """None 输入返回 None"""
        assert normalize_iso_ts_z(None) is None

    def test_normalize_empty_returns_none(self):
        """空字符串返回 None"""
        assert normalize_iso_ts_z("") is None

    def test_normalize_invalid_returns_original(self):
        """无效格式返回原值"""
        invalid = "invalid-timestamp"
        assert normalize_iso_ts_z(invalid) == invalid

    def test_idempotent_normalization(self):
        """标准化是幂等的"""
        original = "2024-01-15T12:00:00+00:00"
        first = normalize_iso_ts_z(original)
        second = normalize_iso_ts_z(first)
        assert first == second == "2024-01-15T12:00:00Z"


class TestTimezoneEquivalenceInCursorComparison:
    """测试游标比较中 Z 与 +00:00 的等价性"""

    def test_mr_cursor_z_vs_offset_equal(self):
        """MR 游标：Z 与 +00:00 格式应视为相等"""
        ts_z = "2024-01-15T12:00:00Z"
        ts_offset = "2024-01-15T12:00:00+00:00"

        # 相同时间、相同 iid：不应推进
        assert should_advance_mr_cursor(ts_z, 100, ts_offset, 100) is False
        assert should_advance_mr_cursor(ts_offset, 100, ts_z, 100) is False

        # 相同时间、不同 iid：按 iid 比较
        assert should_advance_mr_cursor(ts_z, 101, ts_offset, 100) is True
        assert should_advance_mr_cursor(ts_offset, 101, ts_z, 100) is True

    def test_commit_cursor_z_vs_offset_equal(self):
        """Commit 游标：Z 与 +00:00 格式应视为相等"""
        ts_z = "2024-01-15T12:00:00Z"
        ts_offset = "2024-01-15T12:00:00+00:00"

        # 相同时间、相同 sha：不应推进
        assert should_advance_gitlab_commit_cursor(ts_z, "abc123", ts_offset, "abc123") is False
        assert should_advance_gitlab_commit_cursor(ts_offset, "abc123", ts_z, "abc123") is False

        # 相同时间、不同 sha：按 sha 比较
        assert should_advance_gitlab_commit_cursor(ts_z, "bbb222", ts_offset, "aaa111") is True
        assert should_advance_gitlab_commit_cursor(ts_offset, "bbb222", ts_z, "aaa111") is True

    def test_mixed_format_comparison(self):
        """混合格式比较"""
        # 场景：游标存储的是 Z 格式，新值是 +00:00 格式
        cursor_ts = "2024-01-15T12:00:00Z"
        new_ts_same = "2024-01-15T12:00:00+00:00"  # 等价时间
        new_ts_later = "2024-01-15T12:00:01+00:00"  # 晚 1 秒

        # 等价时间应该正确比较
        assert should_advance_mr_cursor(new_ts_same, 100, cursor_ts, 100) is False
        assert should_advance_mr_cursor(new_ts_later, 100, cursor_ts, 100) is True


# ---------- 运行测试的入口 ----------

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
