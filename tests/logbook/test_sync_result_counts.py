# -*- coding: utf-8 -*-
"""
test_sync_result_counts.py - SyncResult 计数行为测试

测试不同 diff_mode 下 counts 的期望差异:
1. diff_mode=always: diff 获取成功时 diff_count++，失败时不写入任何内容
2. diff_mode=best_effort: diff 获取失败时降级为 ministat，degraded_count++ 且 diff_count++
3. diff_mode=none: 完全跳过 diff，diff_none_count++ 且 diff_count 不变

计数语义:
- synced_count: 成功写入 DB 的记录数
- skipped_count: 去重/水位过滤跳过的记录数
- diff_count: 成功写入 patch_blobs 的数量（包含完整 diff 或降级后的 ministat）
- degraded_count: diff 获取失败但仍写入 ministat/diffstat 的数量
- bulk_count: 被标记为 bulk 的 commit 数
- diff_none_count: diff_mode=none 时完全跳过 diff fetch 的数量
"""

import pytest
from dataclasses import asdict

from engram.logbook.sync_result import SyncResult, DiffStatus


class TestSyncResultBasic:
    """SyncResult 基础功能测试"""
    
    def test_default_values(self):
        """默认值应全为零/空"""
        result = SyncResult()
        assert result.success is True
        assert result.synced_count == 0
        assert result.diff_count == 0
        assert result.degraded_count == 0
        assert result.skipped_count == 0
        assert result.bulk_count == 0
        assert result.diff_none_count == 0
    
    def test_to_dict(self):
        """to_dict() 应返回兼容的字典格式"""
        result = SyncResult(
            success=True,
            synced_count=10,
            diff_count=8,
            degraded_count=2,
            skipped_count=3,
        )
        d = result.to_dict()
        assert d["success"] is True
        assert d["synced_count"] == 10
        assert d["diff_count"] == 8
        assert d["degraded_count"] == 2
        assert d["skipped_count"] == 3
    
    def test_from_dict(self):
        """from_dict() 应正确恢复 SyncResult"""
        input_dict = {
            "success": True,
            "synced_count": 5,
            "diff_count": 4,
            "degraded_count": 1,
            "degraded_reasons": {"timeout": 1},
        }
        result = SyncResult.from_dict(input_dict)
        assert result.success is True
        assert result.synced_count == 5
        assert result.diff_count == 4
        assert result.degraded_count == 1
        assert result.degraded_reasons == {"timeout": 1}
    
    def test_add_operator(self):
        """+ 操作符应正确合并两个 SyncResult"""
        r1 = SyncResult(synced_count=5, diff_count=3, degraded_count=1)
        r2 = SyncResult(synced_count=3, diff_count=2, degraded_count=0)
        combined = r1 + r2
        assert combined.synced_count == 8
        assert combined.diff_count == 5
        assert combined.degraded_count == 1
    
    def test_for_no_data(self):
        """for_no_data() 应创建无数据结果"""
        result = SyncResult.for_no_data(cursor_after={"sha": "abc123"})
        assert result.success is True
        assert result.synced_count == 0
        assert result.cursor_after == {"sha": "abc123"}
    
    def test_for_error(self):
        """for_error() 应创建错误结果"""
        result = SyncResult.for_error("Connection timeout", "timeout")
        assert result.success is False
        assert result.error == "Connection timeout"
        assert result.error_category == "timeout"


class TestDiffModeCountsAlways:
    """
    测试 diff_mode=always 下的计数行为
    
    always 模式：diff 获取失败时视为错误，不写入任何内容
    """
    
    def test_diff_success(self):
        """diff 成功获取时，diff_count++"""
        result = SyncResult()
        result.synced_count = 1
        result.record_diff_success()
        
        assert result.diff_count == 1
        assert result.degraded_count == 0
        assert result.diff_none_count == 0
    
    def test_diff_success_multiple(self):
        """多个 diff 成功获取"""
        result = SyncResult()
        result.synced_count = 5
        for _ in range(5):
            result.record_diff_success()
        
        assert result.diff_count == 5
        assert result.degraded_count == 0


class TestDiffModeCountsBestEffort:
    """
    测试 diff_mode=best_effort 下的计数行为
    
    best_effort 模式：diff 获取失败时降级为 ministat/diffstat
    """
    
    def test_diff_degraded_timeout(self):
        """diff 获取超时，降级写入 ministat，diff_count++ 且 degraded_count++"""
        result = SyncResult()
        result.synced_count = 1
        result.record_diff_degraded("timeout")
        
        assert result.diff_count == 1  # ministat 也算写入了 patch
        assert result.degraded_count == 1
        assert result.degraded_reasons == {"timeout": 1}
    
    def test_diff_degraded_content_too_large(self):
        """diff 内容过大，降级写入 ministat"""
        result = SyncResult()
        result.synced_count = 1
        result.record_diff_degraded("content_too_large")
        
        assert result.diff_count == 1
        assert result.degraded_count == 1
        assert result.degraded_reasons == {"content_too_large": 1}
    
    def test_mixed_success_and_degraded(self):
        """混合：部分成功，部分降级"""
        result = SyncResult()
        result.synced_count = 5
        
        # 3 个成功，2 个降级
        for _ in range(3):
            result.record_diff_success()
        result.record_diff_degraded("timeout")
        result.record_diff_degraded("http_error")
        
        assert result.diff_count == 5  # 所有 5 个都写入了 patch
        assert result.degraded_count == 2
        assert result.degraded_reasons == {"timeout": 1, "http_error": 1}


class TestDiffModeCountsNone:
    """
    测试 diff_mode=none 下的计数行为
    
    none 模式：完全跳过 diff 获取，diff_none_count++ 且 diff_count 不变
    """
    
    def test_diff_none_single(self):
        """diff_mode=none 时，diff_none_count++，diff_count 不变"""
        result = SyncResult()
        result.synced_count = 1
        result.record_diff_none()
        
        assert result.diff_count == 0  # 没有写入任何 patch
        assert result.diff_none_count == 1
        assert result.degraded_count == 0
    
    def test_diff_none_multiple(self):
        """多个 commit 跳过 diff"""
        result = SyncResult()
        result.synced_count = 10
        for _ in range(10):
            result.record_diff_none()
        
        assert result.diff_count == 0
        assert result.diff_none_count == 10


class TestDeduplicationCounts:
    """测试去重计数行为"""
    
    def test_dedup_single(self):
        """去重跳过的记录数应增加到 skipped_count"""
        result = SyncResult()
        result.record_dedup(5)
        
        assert result.skipped_count == 5
    
    def test_dedup_multiple_calls(self):
        """多次去重调用应累加"""
        result = SyncResult()
        result.record_dedup(3)
        result.record_dedup(2)
        
        assert result.skipped_count == 5


class TestBulkCounts:
    """测试 bulk commit 计数"""
    
    def test_bulk_single(self):
        """bulk commit 应增加 bulk_count"""
        result = SyncResult()
        result.record_bulk()
        
        assert result.bulk_count == 1
    
    def test_bulk_multiple(self):
        """多个 bulk commit"""
        result = SyncResult()
        for _ in range(3):
            result.record_bulk()
        
        assert result.bulk_count == 3


class TestDiffModeComparison:
    """
    比较同一输入在不同 diff_mode 下的 counts 差异
    
    场景：10 个 commits，其中 2 个 diff 获取失败
    """
    
    def test_same_input_different_modes(self):
        """同一输入样例在不同 diff_mode 下的 counts 期望差异"""
        
        # 场景：10 个 commits
        # - 8 个 diff 获取成功
        # - 2 个 diff 获取失败（超时）
        
        # ======== diff_mode=always ========
        # diff 获取失败时视为错误，不写入 patch
        result_always = SyncResult()
        result_always.synced_count = 10  # 所有 commits 都写入 git_commits
        for _ in range(8):
            result_always.record_diff_success()
        # 2 个失败的不调用 record_diff_*
        
        assert result_always.synced_count == 10
        assert result_always.diff_count == 8  # 只有成功的 8 个写入 patch
        assert result_always.degraded_count == 0
        assert result_always.diff_none_count == 0
        
        # ======== diff_mode=best_effort ========
        # diff 获取失败时降级写入 ministat
        result_best_effort = SyncResult()
        result_best_effort.synced_count = 10
        for _ in range(8):
            result_best_effort.record_diff_success()
        result_best_effort.record_diff_degraded("timeout")
        result_best_effort.record_diff_degraded("timeout")
        
        assert result_best_effort.synced_count == 10
        assert result_best_effort.diff_count == 10  # 全部 10 个都写入 patch（含 ministat）
        assert result_best_effort.degraded_count == 2
        assert result_best_effort.degraded_reasons == {"timeout": 2}
        
        # ======== diff_mode=none ========
        # 完全跳过 diff 获取
        result_none = SyncResult()
        result_none.synced_count = 10
        for _ in range(10):
            result_none.record_diff_none()
        
        assert result_none.synced_count == 10
        assert result_none.diff_count == 0  # 没有写入任何 patch
        assert result_none.diff_none_count == 10
        assert result_none.degraded_count == 0
        
        # ======== 验证差异 ========
        # diff_count 差异
        assert result_always.diff_count == 8
        assert result_best_effort.diff_count == 10
        assert result_none.diff_count == 0
        
        # degraded_count 差异
        assert result_always.degraded_count == 0
        assert result_best_effort.degraded_count == 2
        assert result_none.degraded_count == 0
        
        # diff_none_count 差异
        assert result_always.diff_none_count == 0
        assert result_best_effort.diff_none_count == 0
        assert result_none.diff_none_count == 10


class TestSyncResultLockHeld:
    """测试 SyncResult 的 locked/skipped 字段（用于 lock_held 场景）"""
    
    def test_locked_skipped_fields_default(self):
        """locked 和 skipped 字段默认为 False"""
        result = SyncResult()
        assert result.locked is False
        assert result.skipped is False
    
    def test_locked_skipped_to_dict(self):
        """locked=True 和 skipped=True 时应包含在 to_dict() 中"""
        result = SyncResult(
            success=True,
            locked=True,
            skipped=True,
        )
        d = result.to_dict()
        assert d["locked"] is True
        assert d["skipped"] is True
    
    def test_locked_false_not_in_dict(self):
        """locked=False 时不应包含在 to_dict() 中（节省空间）"""
        result = SyncResult(success=True)
        d = result.to_dict()
        assert "locked" not in d
        assert "skipped" not in d
    
    def test_from_dict_with_locked(self):
        """from_dict() 应正确恢复 locked/skipped 字段"""
        input_dict = {
            "success": True,
            "locked": True,
            "skipped": True,
        }
        result = SyncResult.from_dict(input_dict)
        assert result.locked is True
        assert result.skipped is True
    
    def test_add_operator_with_locked(self):
        """+ 操作符应正确合并 locked/skipped 字段"""
        r1 = SyncResult(locked=False, skipped=False)
        r2 = SyncResult(locked=True, skipped=True)
        combined = r1 + r2
        assert combined.locked is True
        assert combined.skipped is True
    
    def test_lock_held_scenario(self):
        """模拟 lock_held 场景下的 SyncResult"""
        # 当外部资源锁被其他进程持有时
        result = SyncResult(
            success=True,  # 不是失败，只是无法执行
            locked=True,
            skipped=True,
            error_category="lock_held",
            message="Watermark lock held by another worker",
        )
        
        d = result.to_dict()
        assert d["success"] is True
        assert d["locked"] is True
        assert d["skipped"] is True
        assert d["error_category"] == "lock_held"


class TestSyncResultCompatibility:
    """测试 SyncResult 与 run-contract 的兼容性"""
    
    def test_to_dict_contains_required_fields(self):
        """to_dict() 应包含 run-contract 所需的字段"""
        result = SyncResult(
            success=True,
            synced_count=10,
            diff_count=8,
            degraded_count=2,
            degraded_reasons={"timeout": 2},
            request_stats={"total_requests": 20},
        )
        d = result.to_dict()
        
        # 检查 run-contract 所需的顶层字段
        assert "success" in d
        assert "synced_count" in d
        assert "diff_count" in d
        assert "degraded_count" in d
        assert "skipped_count" in d
        
        # 检查 request_stats
        assert "request_stats" in d
        assert d["request_stats"]["total_requests"] == 20
        
        # 检查 degraded_reasons
        assert "degraded_reasons" in d
        assert d["degraded_reasons"] == {"timeout": 2}
    
    def test_round_trip(self):
        """SyncResult -> dict -> SyncResult 往返应保持数据一致"""
        original = SyncResult(
            success=True,
            synced_count=15,
            diff_count=12,
            degraded_count=3,
            skipped_count=5,
            bulk_count=2,
            diff_none_count=0,
            degraded_reasons={"timeout": 2, "content_too_large": 1},
            cursor_after={"sha": "abc123", "ts": "2024-01-01T00:00:00Z"},
        )
        
        d = original.to_dict()
        restored = SyncResult.from_dict(d)
        
        assert restored.success == original.success
        assert restored.synced_count == original.synced_count
        assert restored.diff_count == original.diff_count
        assert restored.degraded_count == original.degraded_count
        assert restored.skipped_count == original.skipped_count
        assert restored.bulk_count == original.bulk_count
        assert restored.diff_none_count == original.diff_none_count
        assert restored.degraded_reasons == original.degraded_reasons
        assert restored.cursor_after == original.cursor_after


# ============ Legacy 字段映射一致性测试 ============


class TestLegacyFieldMapping:
    """测试旧字段到新字段的映射一致性（ok→success, count→synced_count）"""
    
    def test_legacy_ok_maps_to_success(self):
        """旧字段 ok 应该映射到 success"""
        legacy_dict = {"ok": True, "count": 10}
        result = SyncResult.from_dict(legacy_dict)
        
        assert result.success is True
        # 验证 from_dict 正确回退到 ok 字段
        assert result.success == legacy_dict["ok"]
    
    def test_legacy_ok_false_maps_to_success_false(self):
        """旧字段 ok=False 应该映射到 success=False"""
        legacy_dict = {"ok": False, "error": "Some error"}
        result = SyncResult.from_dict(legacy_dict)
        
        assert result.success is False
    
    def test_legacy_count_maps_to_synced_count(self):
        """旧字段 count 应该映射到 synced_count"""
        legacy_dict = {"ok": True, "count": 50}
        result = SyncResult.from_dict(legacy_dict)
        
        assert result.synced_count == 50
        # 验证 from_dict 正确回退到 count 字段
        assert result.synced_count == legacy_dict["count"]
    
    def test_new_fields_take_priority_over_legacy(self):
        """新字段优先于旧字段"""
        # 同时有 success 和 ok 时，success 优先
        mixed_dict = {"success": True, "ok": False, "synced_count": 100, "count": 50}
        result = SyncResult.from_dict(mixed_dict)
        
        assert result.success is True  # success 优先于 ok
        assert result.synced_count == 100  # synced_count 优先于 count
    
    def test_legacy_field_mapping_constant_consistency(self):
        """验证 LEGACY_FIELD_MAPPING 常量与 from_dict 实现一致"""
        from engram.logbook.sync_result import LEGACY_FIELD_MAPPING
        
        # 验证映射定义
        assert LEGACY_FIELD_MAPPING["ok"] == "success"
        assert LEGACY_FIELD_MAPPING["count"] == "synced_count"
        
        # 验证 from_dict 实现与映射一致
        legacy_dict = {"ok": True, "count": 25}
        result = SyncResult.from_dict(legacy_dict)
        
        # from_dict 应该按照 LEGACY_FIELD_MAPPING 的定义进行映射
        assert result.success is True  # ok -> success
        assert result.synced_count == 25  # count -> synced_count


class TestNormalizeSyncResult:
    """测试 normalize_sync_result 函数的一致性"""
    
    def test_normalize_adds_new_fields_from_legacy(self):
        """normalize 应该从旧字段添加新字段"""
        from engram.logbook.sync_result import normalize_sync_result
        
        legacy_dict = {"ok": True, "count": 30}
        normalized = normalize_sync_result(legacy_dict)
        
        # 应该添加新字段
        assert normalized["success"] is True
        assert normalized["synced_count"] == 30
        
        # 旧字段保留
        assert normalized["ok"] is True
        assert normalized["count"] == 30
    
    def test_normalize_does_not_override_existing_new_fields(self):
        """normalize 不应覆盖已存在的新字段"""
        from engram.logbook.sync_result import normalize_sync_result
        
        mixed_dict = {"success": False, "ok": True, "synced_count": 100, "count": 50}
        normalized = normalize_sync_result(mixed_dict)
        
        # 新字段保持不变
        assert normalized["success"] is False
        assert normalized["synced_count"] == 100
    
    def test_normalize_does_not_modify_original(self):
        """normalize 不应修改原始字典"""
        from engram.logbook.sync_result import normalize_sync_result
        
        original = {"ok": True, "count": 20}
        original_copy = dict(original)
        
        normalize_sync_result(original)
        
        # 原始字典应该保持不变
        assert original == original_copy


class TestValidateSyncResultLegacyCompat:
    """测试 validate_sync_result 对旧字段的兼容验证"""
    
    def test_validate_accepts_legacy_ok_field(self):
        """validate 应该接受旧字段 ok"""
        from engram.logbook.sync_result import validate_sync_result
        
        legacy_dict = {"ok": True, "count": 10}
        is_valid, errors, warnings = validate_sync_result(legacy_dict)
        
        assert is_valid is True
        assert len(errors) == 0
        # 应该有使用旧字段的警告
        assert any("ok" in w for w in warnings)
    
    def test_validate_accepts_legacy_count_field(self):
        """validate 应该接受旧字段 count"""
        from engram.logbook.sync_result import validate_sync_result
        
        legacy_dict = {"ok": True, "count": 10}
        is_valid, errors, warnings = validate_sync_result(legacy_dict)
        
        assert is_valid is True
        # 应该有使用旧字段的警告
        assert any("count" in w for w in warnings)
    
    def test_validate_warns_about_legacy_fields(self):
        """validate 应该对旧字段发出警告"""
        from engram.logbook.sync_result import validate_sync_result
        
        # 只有旧字段
        legacy_dict = {"ok": True, "count": 10}
        is_valid, errors, warnings = validate_sync_result(legacy_dict)
        
        assert is_valid is True
        # 警告中应该提到迁移建议
        warning_text = " ".join(warnings)
        assert "success" in warning_text or "synced_count" in warning_text
    
    def test_validate_no_warning_for_new_fields(self):
        """validate 使用新字段时不应有旧字段警告"""
        from engram.logbook.sync_result import validate_sync_result
        
        new_dict = {"success": True, "synced_count": 10}
        is_valid, errors, warnings = validate_sync_result(new_dict)
        
        assert is_valid is True
        # 不应有旧字段相关的警告
        assert not any("ok" in w for w in warnings)
        assert not any("count" in w and "旧字段" in w for w in warnings)
