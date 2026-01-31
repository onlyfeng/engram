# -*- coding: utf-8 -*-
"""
test_sync_run_counts_schema.py - sync_runs.counts 字段契约测试

测试内容:
- sync_run_counts 模块的契约定义
- build_counts 函数的行为
- validate_counts_schema 验证函数
- 各同步脚本返回的 counts 结构符合契约

测试策略:
- 单元测试: 测试契约模块本身
- Schema 快照: 验证 build_counts 返回的字段集合
"""

from engram.logbook.sync_run_counts import (
    COUNTS_LIMITER_FIELDS,
    COUNTS_OPTIONAL_FIELDS,
    COUNTS_REQUIRED_FIELDS,
    SyncRunCounts,
    build_counts,
    build_counts_from_result,
    validate_counts_schema,
)

# ============ 契约定义测试 ============


class TestCountsFieldDefinition:
    """测试 counts 字段定义"""

    def test_required_fields_not_empty(self):
        """必需字段集合不为空"""
        assert len(COUNTS_REQUIRED_FIELDS) > 0
        assert "synced_count" in COUNTS_REQUIRED_FIELDS

    def test_optional_fields_defined(self):
        """可选字段集合已定义"""
        assert isinstance(COUNTS_OPTIONAL_FIELDS, set)
        # 至少包含一些已知字段
        assert "diff_count" in COUNTS_OPTIONAL_FIELDS
        assert "bulk_count" in COUNTS_OPTIONAL_FIELDS
        assert "diff_none_count" in COUNTS_OPTIONAL_FIELDS
        assert "skipped_count" in COUNTS_OPTIONAL_FIELDS

    def test_limiter_fields_defined(self):
        """Limiter 统计字段已定义"""
        assert "total_requests" in COUNTS_LIMITER_FIELDS
        assert "total_429_hits" in COUNTS_LIMITER_FIELDS
        assert "timeout_count" in COUNTS_LIMITER_FIELDS
        assert "avg_wait_time_ms" in COUNTS_LIMITER_FIELDS

    def test_no_field_overlap(self):
        """字段集合之间无重叠"""
        all_sets = [COUNTS_REQUIRED_FIELDS, COUNTS_OPTIONAL_FIELDS, COUNTS_LIMITER_FIELDS]
        for i, s1 in enumerate(all_sets):
            for j, s2 in enumerate(all_sets):
                if i != j:
                    overlap = s1 & s2
                    assert len(overlap) == 0, f"字段集合 {i} 和 {j} 有重叠: {overlap}"


# ============ SyncRunCounts 数据类测试 ============


class TestSyncRunCountsDataclass:
    """测试 SyncRunCounts 数据类"""

    def test_default_values_are_zero(self):
        """所有字段默认值为 0"""
        counts = SyncRunCounts()
        assert counts.synced_count == 0
        assert counts.diff_count == 0
        assert counts.bulk_count == 0
        assert counts.degraded_count == 0
        assert counts.diff_none_count == 0
        assert counts.skipped_count == 0
        assert counts.total_requests == 0
        assert counts.total_429_hits == 0

    def test_to_dict_includes_all_fields(self):
        """to_dict 包含所有字段"""
        counts = SyncRunCounts()
        d = counts.to_dict()

        # 验证必需字段
        for field in COUNTS_REQUIRED_FIELDS:
            assert field in d, f"缺少必需字段: {field}"

        # 验证可选字段
        for field in COUNTS_OPTIONAL_FIELDS:
            assert field in d, f"缺少可选字段: {field}"

        # 验证 limiter 字段
        for field in COUNTS_LIMITER_FIELDS:
            assert field in d, f"缺少 limiter 字段: {field}"

    def test_to_dict_exclude_zero(self):
        """to_dict(include_zero=False) 排除值为 0 的字段"""
        counts = SyncRunCounts(synced_count=10, diff_count=5)
        d = counts.to_dict(include_zero=False)

        assert "synced_count" in d
        assert "diff_count" in d
        assert d["synced_count"] == 10
        assert d["diff_count"] == 5

        # 值为 0 的字段不应出现
        assert "bulk_count" not in d


# ============ build_counts 函数测试 ============


class TestBuildCounts:
    """测试 build_counts 函数"""

    def test_returns_dict(self):
        """返回字典类型"""
        result = build_counts()
        assert isinstance(result, dict)

    def test_default_values_are_zero(self):
        """未指定的字段默认为 0"""
        result = build_counts()
        assert result["synced_count"] == 0
        assert result["diff_count"] == 0
        assert result["total_requests"] == 0

    def test_specified_values_preserved(self):
        """指定的值被正确保留"""
        result = build_counts(
            synced_count=100,
            diff_count=50,
            total_requests=200,
        )
        assert result["synced_count"] == 100
        assert result["diff_count"] == 50
        assert result["total_requests"] == 200

    def test_all_values_are_int(self):
        """所有值都是 int 类型"""
        result = build_counts(
            synced_count=10.5,  # 会被转为 int
            diff_count="20",  # 字符串也会被转为 int
        )
        for key, value in result.items():
            assert isinstance(value, int), f"字段 {key} 类型不是 int: {type(value)}"

    def test_extra_fields_allowed(self):
        """额外字段被接受（向前兼容）"""
        result = build_counts(
            synced_count=10,
            custom_field=5,
        )
        assert result["synced_count"] == 10
        assert result["custom_field"] == 5

    def test_schema_snapshot_gitlab_commits(self):
        """GitLab Commits 脚本的 counts 字段快照"""
        # 模拟 scm_sync_gitlab_commits.py 的调用方式
        result = build_counts(
            synced_count=100,
            diff_count=95,
            bulk_count=2,
            degraded_count=1,
            skipped_count=5,
            total_requests=200,
            total_429_hits=1,
            timeout_count=0,
            avg_wait_time_ms=150,
        )

        # 验证关键字段存在
        expected_fields = {
            "synced_count",
            "diff_count",
            "bulk_count",
            "degraded_count",
            "skipped_count",
            "total_requests",
            "total_429_hits",
            "timeout_count",
            "avg_wait_time_ms",
        }
        for field in expected_fields:
            assert field in result, f"GitLab Commits counts 缺少字段: {field}"

    def test_schema_snapshot_gitlab_mrs(self):
        """GitLab MRs 脚本的 counts 字段快照"""
        result = build_counts(
            synced_count=50,
            scanned_count=100,
            inserted_count=30,
            skipped_count=20,
            total_requests=150,
            total_429_hits=0,
            timeout_count=0,
            avg_wait_time_ms=100,
        )

        expected_fields = {
            "synced_count",
            "scanned_count",
            "inserted_count",
            "skipped_count",
            "total_requests",
            "total_429_hits",
            "timeout_count",
            "avg_wait_time_ms",
        }
        for field in expected_fields:
            assert field in result, f"GitLab MRs counts 缺少字段: {field}"

    def test_schema_snapshot_gitlab_reviews(self):
        """GitLab Reviews 脚本的 counts 字段快照"""
        result = build_counts(
            synced_mr_count=10,
            synced_event_count=50,
            skipped_event_count=5,
            total_requests=100,
            total_429_hits=0,
            timeout_count=0,
            avg_wait_time_ms=80,
        )

        expected_fields = {
            "synced_mr_count",
            "synced_event_count",
            "skipped_event_count",
            "total_requests",
            "total_429_hits",
            "timeout_count",
            "avg_wait_time_ms",
        }
        for field in expected_fields:
            assert field in result, f"GitLab Reviews counts 缺少字段: {field}"

    def test_schema_snapshot_svn(self):
        """SVN 脚本的 counts 字段快照"""
        result = build_counts(
            synced_count=200,
            diff_count=180,
            bulk_count=5,
            degraded_count=3,
            patch_success=175,
            patch_failed=5,
            skipped_by_controller=10,
        )

        expected_fields = {
            "synced_count",
            "diff_count",
            "bulk_count",
            "degraded_count",
            "patch_success",
            "patch_failed",
            "skipped_by_controller",
        }
        for field in expected_fields:
            assert field in result, f"SVN counts 缺少字段: {field}"


# ============ build_counts_from_result 测试 ============


class TestBuildCountsFromResult:
    """测试 build_counts_from_result 函数"""

    def test_extracts_from_result_dict(self):
        """从 result 字典提取字段"""
        result = {
            "synced_count": 100,
            "diff_count": 50,
            "request_stats": {
                "total_requests": 200,
                "total_429_hits": 1,
            },
        }

        counts = build_counts_from_result(result)
        assert counts["synced_count"] == 100
        assert counts["diff_count"] == 50
        assert counts["total_requests"] == 200
        assert counts["total_429_hits"] == 1

    def test_handles_missing_fields(self):
        """缺失字段默认为 0"""
        result = {"synced_count": 10}
        counts = build_counts_from_result(result)

        assert counts["synced_count"] == 10
        assert counts["diff_count"] == 0
        assert counts["total_requests"] == 0

    def test_extracts_patch_stats(self):
        """从 patch_stats 提取字段"""
        result = {
            "synced_count": 100,
            "patch_stats": {
                "success": 90,
                "failed": 5,
                "skipped_by_controller": 5,
            },
        }

        counts = build_counts_from_result(result)
        assert counts["patch_success"] == 90
        assert counts["patch_failed"] == 5
        assert counts["skipped_by_controller"] == 5

    def test_extracts_diff_none_count(self):
        """从 result 提取 diff_none_count 字段"""
        result = {
            "synced_count": 10,
            "diff_none_count": 10,
        }

        counts = build_counts_from_result(result)
        assert counts["diff_none_count"] == 10


# ============ validate_counts_schema 测试 ============


class TestValidateCountsSchema:
    """测试 validate_counts_schema 函数"""

    def test_valid_counts_passes(self):
        """有效的 counts 通过验证"""
        counts = build_counts(synced_count=100)
        is_valid, errors, warnings = validate_counts_schema(counts)

        assert is_valid
        assert len(errors) == 0

    def test_missing_required_field_fails(self):
        """缺少必需字段验证失败"""
        counts = {"diff_count": 10}  # 缺少 synced_count
        is_valid, errors, warnings = validate_counts_schema(counts)

        assert not is_valid
        assert any("synced_count" in e for e in errors)

    def test_wrong_type_fails(self):
        """类型错误验证失败"""
        counts = {"synced_count": "not_a_number"}
        is_valid, errors, warnings = validate_counts_schema(counts)

        assert not is_valid
        assert any("类型错误" in e for e in errors)

    def test_unknown_field_warns(self):
        """未知字段产生警告"""
        counts = build_counts(synced_count=100)
        counts["unknown_field"] = 10

        is_valid, errors, warnings = validate_counts_schema(counts)

        assert is_valid  # 未知字段不影响有效性
        assert any("unknown_field" in w for w in warnings)


# ============ 校验行为一致性测试 ============


class TestValidateCountsSchemaConsistency:
    """测试 validate_counts_schema 行为一致性"""

    # ============ 缺失必需字段测试 ============

    def test_empty_counts_fails(self):
        """空字典缺少所有必需字段"""
        counts = {}
        is_valid, errors, warnings = validate_counts_schema(counts)

        assert not is_valid
        # 应该报告缺少 synced_count
        assert any("synced_count" in e for e in errors)

    def test_only_optional_fields_fails(self):
        """只有可选字段缺少必需字段"""
        counts = {
            "diff_count": 10,
            "bulk_count": 5,
            "total_requests": 100,
        }
        is_valid, errors, warnings = validate_counts_schema(counts)

        assert not is_valid
        assert any("synced_count" in e for e in errors)

    def test_all_required_fields_present_passes(self):
        """所有必需字段存在通过验证"""
        # COUNTS_REQUIRED_FIELDS 目前只有 synced_count
        counts = {"synced_count": 0}
        is_valid, errors, warnings = validate_counts_schema(counts)

        assert is_valid
        assert len(errors) == 0

    # ============ 类型错误测试 ============

    def test_string_value_fails(self):
        """字符串值类型错误"""
        counts = {"synced_count": "100"}
        is_valid, errors, warnings = validate_counts_schema(counts)

        assert not is_valid
        assert any("类型错误" in e for e in errors)

    def test_list_value_fails(self):
        """列表值类型错误"""
        counts = {"synced_count": [1, 2, 3]}
        is_valid, errors, warnings = validate_counts_schema(counts)

        assert not is_valid
        assert any("类型错误" in e for e in errors)

    def test_dict_value_fails(self):
        """字典值类型错误"""
        counts = {"synced_count": {"nested": 10}}
        is_valid, errors, warnings = validate_counts_schema(counts)

        assert not is_valid
        assert any("类型错误" in e for e in errors)

    def test_none_value_fails(self):
        """None 值类型错误"""
        counts = {"synced_count": None}
        is_valid, errors, warnings = validate_counts_schema(counts)

        assert not is_valid
        assert any("类型错误" in e for e in errors)

    def test_float_value_fails(self):
        """浮点数值验证失败（契约要求 integer 且 minimum=0）"""
        counts = {"synced_count": 100.0}
        is_valid, errors, warnings = validate_counts_schema(counts)

        assert not is_valid
        assert any("类型错误" in e for e in errors)

    def test_negative_int_fails(self):
        """负整数验证失败（契约要求 minimum=0）"""
        counts = {"synced_count": -1}
        is_valid, errors, warnings = validate_counts_schema(counts)

        assert not is_valid
        assert any("不能为负数" in e for e in errors)

    def test_multiple_type_errors_all_reported(self):
        """多个类型错误都被报告"""
        counts = {
            "synced_count": "100",
            "diff_count": "50",
            "bulk_count": [1, 2],
        }
        is_valid, errors, warnings = validate_counts_schema(counts)

        assert not is_valid
        # 应该报告所有三个类型错误
        assert len([e for e in errors if "类型错误" in e]) >= 3

    # ============ 未知字段警告测试 ============

    def test_single_unknown_field_warns(self):
        """单个未知字段产生警告"""
        counts = {"synced_count": 100, "custom_metric": 50}
        is_valid, errors, warnings = validate_counts_schema(counts)

        assert is_valid  # 不影响有效性
        assert len(warnings) == 1
        assert "custom_metric" in warnings[0]

    def test_multiple_unknown_fields_all_warned(self):
        """多个未知字段都产生警告"""
        counts = {
            "synced_count": 100,
            "custom_field_1": 10,
            "custom_field_2": 20,
            "future_metric": 30,
        }
        is_valid, errors, warnings = validate_counts_schema(counts)

        assert is_valid
        assert len(warnings) == 3
        warned_fields = " ".join(warnings)
        assert "custom_field_1" in warned_fields
        assert "custom_field_2" in warned_fields
        assert "future_metric" in warned_fields

    def test_known_optional_fields_no_warning(self):
        """已知可选字段不产生警告"""
        counts = {
            "synced_count": 100,
            "diff_count": 50,
            "bulk_count": 5,
            "scanned_count": 200,
            "total_requests": 300,
        }
        is_valid, errors, warnings = validate_counts_schema(counts)

        assert is_valid
        assert len(warnings) == 0

    def test_known_limiter_fields_no_warning(self):
        """已知 limiter 字段不产生警告"""
        counts = {
            "synced_count": 100,
            "total_requests": 500,
            "total_429_hits": 5,
            "timeout_count": 2,
            "avg_wait_time_ms": 150,
        }
        is_valid, errors, warnings = validate_counts_schema(counts)

        assert is_valid
        assert len(warnings) == 0

    # ============ 组合场景测试 ============

    def test_missing_field_and_type_error(self):
        """同时缺少必需字段和类型错误"""
        counts = {"diff_count": "not_a_number"}  # 缺少 synced_count，且 diff_count 类型错误
        is_valid, errors, warnings = validate_counts_schema(counts)

        assert not is_valid
        assert any("synced_count" in e for e in errors)
        assert any("类型错误" in e for e in errors)

    def test_missing_field_and_unknown_field(self):
        """同时缺少必需字段和有未知字段"""
        counts = {"unknown_field": 100}  # 缺少 synced_count，有未知字段
        is_valid, errors, warnings = validate_counts_schema(counts)

        assert not is_valid
        assert any("synced_count" in e for e in errors)
        assert any("unknown_field" in w for w in warnings)

    def test_type_error_and_unknown_field(self):
        """同时类型错误和有未知字段"""
        counts = {
            "synced_count": "not_a_number",
            "unknown_field": 100,
        }
        is_valid, errors, warnings = validate_counts_schema(counts)

        assert not is_valid
        assert any("类型错误" in e for e in errors)
        assert any("unknown_field" in w for w in warnings)

    def test_valid_with_unknown_fields(self):
        """有效 counts 加未知字段"""
        counts = build_counts(synced_count=100, diff_count=50)
        counts["custom_metric"] = 25

        is_valid, errors, warnings = validate_counts_schema(counts)

        assert is_valid
        assert len(errors) == 0
        assert len(warnings) == 1

    # ============ 边界值测试 ============

    def test_zero_value_passes(self):
        """零值通过验证"""
        counts = {"synced_count": 0}
        is_valid, errors, warnings = validate_counts_schema(counts)

        assert is_valid

    def test_large_value_passes(self):
        """大数值通过验证"""
        counts = {"synced_count": 10**12}  # 1 万亿
        is_valid, errors, warnings = validate_counts_schema(counts)

        assert is_valid

    def test_all_fields_zero_passes(self):
        """所有字段为零通过验证"""
        counts = build_counts()  # 所有字段默认为 0
        is_valid, errors, warnings = validate_counts_schema(counts)

        assert is_valid
        assert len(errors) == 0
        assert len(warnings) == 0


# ============ 集成测试：验证 build_counts 返回的完整 schema ============


class TestCountsSchemaIntegrity:
    """验证 counts schema 完整性"""

    def test_build_counts_returns_all_known_fields(self):
        """build_counts 返回所有已知字段"""
        counts = build_counts()

        all_known_fields = COUNTS_REQUIRED_FIELDS | COUNTS_OPTIONAL_FIELDS | COUNTS_LIMITER_FIELDS

        for field in all_known_fields:
            assert field in counts, f"build_counts 缺少字段: {field}"

    def test_build_counts_no_extra_unknown_fields(self):
        """build_counts 不返回未知字段"""
        counts = build_counts()

        all_known_fields = COUNTS_REQUIRED_FIELDS | COUNTS_OPTIONAL_FIELDS | COUNTS_LIMITER_FIELDS

        for field in counts:
            assert field in all_known_fields, f"build_counts 返回了未知字段: {field}"

    def test_counts_field_names_are_snake_case(self):
        """所有字段名使用 snake_case"""
        counts = build_counts()

        for field in counts:
            assert "_" in field or field.islower(), f"字段名不符合 snake_case: {field}"
            # 不应包含大写字母（camelCase）
            assert field == field.lower(), f"字段名包含大写字母: {field}"
