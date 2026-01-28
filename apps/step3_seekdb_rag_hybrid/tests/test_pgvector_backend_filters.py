#!/usr/bin/env python3
"""
test_pgvector_backend_filters.py - PGVector 后端 Filter DSL 翻译测试

测试 Filter DSL 到 SQL WHERE 条件的安全翻译，包括：
- 各种操作符的正确翻译
- 参数化查询（禁止 SQL 拼接）
- SQL 注入防护
- 边界条件处理
"""

import pytest
from typing import Any, Dict, List

# 路径配置在 conftest.py 中完成
from index_backend.pgvector_backend import (
    FilterDSLTranslator,
    SQLCondition,
    SQLInjectionError,
)
from index_backend.base import FilterValidationError


class TestFilterDSLTranslator:
    """Filter DSL 翻译器测试"""

    @pytest.fixture
    def translator(self) -> FilterDSLTranslator:
        """创建翻译器实例"""
        return FilterDSLTranslator(strict=True)

    # ============ 基础翻译测试 ============

    def test_empty_filters(self, translator: FilterDSLTranslator):
        """测试空过滤条件"""
        result = translator.translate({})
        assert result.clause == "TRUE"
        assert result.params == []

    def test_simple_eq_filter(self, translator: FilterDSLTranslator):
        """测试简单等值过滤"""
        filters = {"project_key": "webapp"}
        result = translator.translate(filters)
        
        assert "project_key = %s" in result.clause
        assert "webapp" in result.params

    def test_explicit_eq_operator(self, translator: FilterDSLTranslator):
        """测试显式 $eq 操作符"""
        filters = {"project_key": {"$eq": "webapp"}}
        result = translator.translate(filters)
        
        assert "project_key = %s" in result.clause
        assert "webapp" in result.params

    def test_in_operator(self, translator: FilterDSLTranslator):
        """测试 $in 操作符"""
        filters = {"source_type": {"$in": ["git", "svn", "logbook"]}}
        result = translator.translate(filters)
        
        assert "source_type IN" in result.clause
        assert "%s, %s, %s" in result.clause
        assert "git" in result.params
        assert "svn" in result.params
        assert "logbook" in result.params

    def test_prefix_operator(self, translator: FilterDSLTranslator):
        """测试 $prefix 操作符"""
        filters = {"module": {"$prefix": "src/components/"}}
        result = translator.translate(filters)
        
        assert "module LIKE %s" in result.clause
        # 应该添加 % 后缀
        assert any("src/components/%" in str(p) for p in result.params)

    def test_range_operators(self, translator: FilterDSLTranslator):
        """测试范围操作符"""
        filters = {
            "commit_ts": {
                "$gte": "2024-01-01T00:00:00Z",
                "$lte": "2024-12-31T23:59:59Z",
            }
        }
        result = translator.translate(filters)
        
        assert "commit_ts >=" in result.clause
        assert "commit_ts <=" in result.clause
        assert "2024-01-01T00:00:00Z" in result.params
        assert "2024-12-31T23:59:59Z" in result.params

    def test_gt_lt_operators(self, translator: FilterDSLTranslator):
        """测试 $gt 和 $lt 操作符"""
        filters = {
            "commit_ts": {
                "$gt": "2024-01-01",
                "$lt": "2024-12-31",
            }
        }
        result = translator.translate(filters)
        
        assert "commit_ts >" in result.clause
        assert "commit_ts <" in result.clause

    def test_multiple_fields(self, translator: FilterDSLTranslator):
        """测试多字段组合"""
        filters = {
            "project_key": "webapp",
            "source_type": "git",
            "owner_user_id": "user123",
        }
        result = translator.translate(filters)
        
        # 所有条件应该用 AND 连接
        assert " AND " in result.clause
        assert "project_key = %s" in result.clause
        assert "source_type = %s" in result.clause
        assert "owner_user_id = %s" in result.clause
        assert len(result.params) == 3

    def test_complex_filter(self, translator: FilterDSLTranslator):
        """测试复杂过滤条件"""
        filters = {
            "project_key": "webapp",
            "module": {"$prefix": "src/api/"},
            "source_type": {"$in": ["git", "svn"]},
            "commit_ts": {"$gte": "2024-01-01", "$lte": "2024-06-30"},
        }
        result = translator.translate(filters)
        
        assert "project_key = %s" in result.clause
        assert "module LIKE %s" in result.clause
        assert "source_type IN" in result.clause
        assert "commit_ts >=" in result.clause
        assert "commit_ts <=" in result.clause
        # 5 个条件
        assert result.clause.count("AND") >= 4

    # ============ 参数化查询测试 ============

    def test_parameterized_query_no_concatenation(self, translator: FilterDSLTranslator):
        """验证参数化查询，禁止字符串拼接"""
        dangerous_value = "'; DROP TABLE chunks; --"
        filters = {"project_key": dangerous_value}
        result = translator.translate(filters)
        
        # 值应该在 params 中，而不是在 clause 中
        assert dangerous_value not in result.clause
        assert dangerous_value in result.params
        # clause 只包含占位符
        assert "%s" in result.clause

    def test_all_values_in_params(self, translator: FilterDSLTranslator):
        """验证所有值都在 params 列表中"""
        filters = {
            "project_key": "testvalue1",
            "source_type": {"$in": ["testvalue2", "testvalue3"]},
            "module": {"$prefix": "src/components/"},
        }
        result = translator.translate(filters)
        
        # 所有测试值不应出现在 clause 中
        assert "testvalue1" not in result.clause
        assert "testvalue2" not in result.clause
        assert "testvalue3" not in result.clause
        assert "src/components/" not in result.clause
        
        # 值应该在 params 中
        assert "testvalue1" in result.params
        assert "testvalue2" in result.params
        assert "testvalue3" in result.params
        # prefix 会添加 % 后缀
        assert any("src/components/%" in str(p) for p in result.params)

    # ============ SQL 注入防护测试 ============

    def test_sql_injection_in_value(self, translator: FilterDSLTranslator):
        """测试值中的 SQL 注入攻击"""
        injection_attempts = [
            "'; DROP TABLE chunks; --",
            "1; DELETE FROM chunks WHERE 1=1; --",
            "' OR '1'='1",
            "test' UNION SELECT * FROM users --",
            "test'; TRUNCATE chunks; --",
        ]
        
        for injection in injection_attempts:
            filters = {"project_key": injection}
            result = translator.translate(filters)
            
            # 注入内容不应出现在 clause 中
            assert injection not in result.clause
            # 应该作为参数传递
            assert injection in result.params

    def test_like_pattern_escape(self, translator: FilterDSLTranslator):
        """测试 LIKE 模式特殊字符转义"""
        # % 和 _ 在 LIKE 中有特殊含义，需要转义
        filters = {"module": {"$prefix": "src/100%_complete/"}}
        result = translator.translate(filters)
        
        # 检查参数中的值已转义
        prefix_param = [p for p in result.params if "100" in str(p)][0]
        # % 应该被转义为 \%
        assert "100\\%" in prefix_param
        # _ 应该被转义为 \_
        assert "\\_complete" in prefix_param

    def test_unknown_field_strict_mode(self, translator: FilterDSLTranslator):
        """测试严格模式下未知字段被拒绝"""
        filters = {"unknown_field": "value"}
        
        with pytest.raises(FilterValidationError) as exc_info:
            translator.translate(filters)
        
        assert "未知的过滤字段" in str(exc_info.value)

    def test_unknown_field_non_strict_mode(self):
        """测试非严格模式下未知字段被忽略"""
        translator = FilterDSLTranslator(strict=False)
        filters = {
            "project_key": "webapp",
            "unknown_field": "ignored",
        }
        result = translator.translate(filters)
        
        # 只处理已知字段
        assert "project_key = %s" in result.clause
        assert "unknown_field" not in result.clause
        assert "ignored" not in result.params

    def test_invalid_operator_rejected(self, translator: FilterDSLTranslator):
        """测试无效操作符被拒绝"""
        filters = {"project_key": {"$invalid": "value"}}
        
        with pytest.raises(FilterValidationError) as exc_info:
            translator.translate(filters)
        
        assert "未知的操作符" in str(exc_info.value)

    def test_in_operator_empty_list(self, translator: FilterDSLTranslator):
        """测试 $in 操作符空列表"""
        filters = {"source_type": {"$in": []}}
        
        with pytest.raises(FilterValidationError) as exc_info:
            translator.translate(filters)
        
        assert "$in 操作符需要非空列表" in str(exc_info.value)

    # ============ 边界条件测试 ============

    def test_single_item_in_list(self, translator: FilterDSLTranslator):
        """测试单元素 $in 列表"""
        filters = {"source_type": {"$in": ["git"]}}
        result = translator.translate(filters)
        
        assert "source_type IN (%s)" in result.clause
        assert result.params == ["git"]

    def test_list_value_normalized_to_in(self, translator: FilterDSLTranslator):
        """测试列表值自动规范化为 $in"""
        filters = {"source_type": ["git", "svn"]}
        result = translator.translate(filters)
        
        assert "source_type IN" in result.clause
        assert "git" in result.params
        assert "svn" in result.params

    def test_unicode_values(self, translator: FilterDSLTranslator):
        """测试 Unicode 值"""
        filters = {"project_key": "项目中文名"}
        result = translator.translate(filters)
        
        assert "项目中文名" in result.params
        assert "项目中文名" not in result.clause

    def test_special_characters_in_value(self, translator: FilterDSLTranslator):
        """测试值中的特殊字符"""
        filters = {"project_key": "test@#$%^&*(){}[]|\\"}
        result = translator.translate(filters)
        
        # 特殊字符应该安全地在 params 中
        assert result.params[0] == "test@#$%^&*(){}[]|\\"

    def test_newline_in_value(self, translator: FilterDSLTranslator):
        """测试值中的换行符"""
        filters = {"project_key": "test\nwith\nnewlines"}
        result = translator.translate(filters)
        
        assert result.params[0] == "test\nwith\nnewlines"


class TestSQLCondition:
    """SQLCondition 数据结构测试"""

    def test_sql_condition_structure(self):
        """测试 SQLCondition 结构"""
        condition = SQLCondition(
            clause="project_key = %s AND source_type = %s",
            params=["webapp", "git"],
        )
        
        assert condition.clause == "project_key = %s AND source_type = %s"
        assert condition.params == ["webapp", "git"]

    def test_sql_condition_empty(self):
        """测试空 SQLCondition"""
        condition = SQLCondition(clause="TRUE", params=[])
        
        assert condition.clause == "TRUE"
        assert condition.params == []


class TestFilterDSLTranslatorColumnWhitelist:
    """列名白名单测试"""

    def test_all_allowed_columns(self):
        """测试所有允许的列"""
        translator = FilterDSLTranslator()
        
        expected_columns = {
            "project_key",
            "module",
            "source_type",
            "source_id",
            "owner_user_id",
            "commit_ts",
            "collection_id",
        }
        
        assert set(translator.ALLOWED_COLUMNS.keys()) == expected_columns

    def test_column_name_not_in_params(self):
        """验证列名不在参数中（防止列名注入）"""
        translator = FilterDSLTranslator()
        filters = {"project_key": "value"}
        result = translator.translate(filters)
        
        # 列名应该直接在 clause 中（硬编码）
        assert "project_key" in result.clause
        # 列名不应该在 params 中
        assert "project_key" not in result.params


class TestFilterOperatorCompatibility:
    """操作符与字段兼容性测试"""

    @pytest.fixture
    def translator(self) -> FilterDSLTranslator:
        return FilterDSLTranslator(strict=True)

    def test_prefix_only_for_module(self, translator: FilterDSLTranslator):
        """$prefix 操作符仅支持 module 字段"""
        # 有效
        valid_filters = {"module": {"$prefix": "src/"}}
        result = translator.translate(valid_filters)
        assert "module LIKE" in result.clause
        
        # 无效 - project_key 不支持 $prefix
        invalid_filters = {"project_key": {"$prefix": "web"}}
        with pytest.raises(FilterValidationError) as exc_info:
            translator.translate(invalid_filters)
        assert "$prefix 仅支持 module 字段" in str(exc_info.value)

    def test_range_only_for_commit_ts(self, translator: FilterDSLTranslator):
        """范围操作符仅支持 commit_ts 字段"""
        # 有效
        valid_filters = {"commit_ts": {"$gte": "2024-01-01"}}
        result = translator.translate(valid_filters)
        assert "commit_ts >=" in result.clause
        
        # 无效 - project_key 不支持范围操作符
        for op in ["$gte", "$lte", "$gt", "$lt"]:
            invalid_filters = {"project_key": {op: "value"}}
            with pytest.raises(FilterValidationError):
                translator.translate(invalid_filters)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
