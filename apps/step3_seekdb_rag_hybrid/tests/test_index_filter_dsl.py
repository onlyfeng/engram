#!/usr/bin/env python3
"""
test_index_filter_dsl.py - 索引后端 Filter DSL 单元测试

测试 Filter DSL 的格式规范、校验逻辑和各后端实现的一致性。
"""

import pytest
from typing import Dict, Any

# 路径配置在 conftest.py 中完成
from index_backend.types import (
    ChunkDoc,
    QueryRequest,
    QueryHit,
    FilterDSL,
    FILTER_FIELDS,
    FILTER_OPERATORS,
)
from index_backend.base import (
    validate_filter_dsl,
    normalize_filter_dsl,
    build_filter_dsl,
    FilterValidationError,
)
from seek_query import QueryFilters


class TestFilterDSLValidation:
    """Filter DSL 校验测试"""

    def test_valid_simple_filters(self):
        """测试简单的有效过滤条件"""
        # 直接值格式
        filters = {
            "project_key": "webapp",
            "source_type": "git",
        }
        warnings = validate_filter_dsl(filters)
        assert warnings == []

    def test_valid_operator_filters(self):
        """测试操作符格式的过滤条件"""
        filters = {
            "project_key": {"$eq": "webapp"},
            "module": {"$prefix": "src/components/"},
            "commit_ts": {"$gte": "2024-01-01T00:00:00Z", "$lte": "2024-12-31T23:59:59Z"},
        }
        warnings = validate_filter_dsl(filters)
        assert warnings == []

    def test_valid_in_operator(self):
        """测试 $in 操作符"""
        filters = {
            "source_type": {"$in": ["git", "svn"]},
        }
        warnings = validate_filter_dsl(filters)
        assert warnings == []

    def test_invalid_field_strict_mode(self):
        """测试严格模式下的无效字段"""
        filters = {
            "unknown_field": "value",
        }
        with pytest.raises(FilterValidationError) as exc_info:
            validate_filter_dsl(filters, strict=True)
        assert "未知的过滤字段" in str(exc_info.value)
        assert exc_info.value.field == "unknown_field"

    def test_invalid_field_non_strict_mode(self):
        """测试非严格模式下的无效字段（警告而非异常）"""
        filters = {
            "project_key": "webapp",
            "unknown_field": "value",
        }
        warnings = validate_filter_dsl(filters, strict=False)
        assert len(warnings) == 1
        assert "unknown_field" in warnings[0]

    def test_invalid_operator(self):
        """测试无效操作符"""
        filters = {
            "project_key": {"$invalid_op": "value"},
        }
        with pytest.raises(FilterValidationError) as exc_info:
            validate_filter_dsl(filters)
        assert "未知的操作符" in str(exc_info.value)
        assert exc_info.value.operator == "$invalid_op"

    def test_prefix_only_for_module(self):
        """测试 $prefix 操作符仅支持 module 字段"""
        # 有效: module 使用 $prefix
        valid_filters = {"module": {"$prefix": "src/"}}
        warnings = validate_filter_dsl(valid_filters)
        assert warnings == []

        # 无效: project_key 使用 $prefix
        invalid_filters = {"project_key": {"$prefix": "web"}}
        with pytest.raises(FilterValidationError) as exc_info:
            validate_filter_dsl(invalid_filters)
        assert "$prefix 仅支持 module 字段" in str(exc_info.value)

    def test_range_operators_only_for_commit_ts(self):
        """测试范围操作符仅支持 commit_ts 字段"""
        # 有效: commit_ts 使用范围操作符
        valid_filters = {"commit_ts": {"$gte": "2024-01-01", "$lte": "2024-12-31"}}
        warnings = validate_filter_dsl(valid_filters)
        assert warnings == []

        # 无效: project_key 使用范围操作符
        for op in ["$gte", "$lte", "$gt", "$lt"]:
            invalid_filters = {"project_key": {op: "value"}}
            with pytest.raises(FilterValidationError) as exc_info:
                validate_filter_dsl(invalid_filters)
            assert f"范围操作符 {op} 仅支持 commit_ts 字段" in str(exc_info.value)

    def test_in_operator_requires_list(self):
        """测试 $in 操作符必须是列表值"""
        invalid_filters = {"source_type": {"$in": "git"}}
        with pytest.raises(FilterValidationError) as exc_info:
            validate_filter_dsl(invalid_filters)
        assert "$in 操作符的值必须是列表" in str(exc_info.value)

    def test_prefix_value_must_be_string(self):
        """测试 $prefix 值必须是字符串"""
        invalid_filters = {"module": {"$prefix": 123}}
        with pytest.raises(FilterValidationError) as exc_info:
            validate_filter_dsl(invalid_filters)
        assert "$prefix 操作符的值必须是字符串" in str(exc_info.value)

    def test_range_value_must_be_string(self):
        """测试范围值必须是字符串"""
        invalid_filters = {"commit_ts": {"$gte": 12345}}
        with pytest.raises(FilterValidationError) as exc_info:
            validate_filter_dsl(invalid_filters)
        assert "范围操作符的值必须是 ISO 时间字符串" in str(exc_info.value)


class TestFilterDSLNormalization:
    """Filter DSL 规范化测试"""

    def test_normalize_scalar_to_eq(self):
        """测试标量值规范化为 $eq"""
        filters = {"project_key": "webapp"}
        normalized = normalize_filter_dsl(filters)
        assert normalized == {"project_key": {"$eq": "webapp"}}

    def test_normalize_list_to_in(self):
        """测试列表值规范化为 $in"""
        filters = {"source_type": ["git", "svn"]}
        normalized = normalize_filter_dsl(filters)
        assert normalized == {"source_type": {"$in": ["git", "svn"]}}

    def test_normalize_preserves_operators(self):
        """测试已有操作符格式保持不变"""
        filters = {
            "project_key": {"$eq": "webapp"},
            "module": {"$prefix": "src/"},
            "commit_ts": {"$gte": "2024-01-01"},
        }
        normalized = normalize_filter_dsl(filters)
        assert normalized == filters

    def test_normalize_mixed(self):
        """测试混合格式规范化"""
        filters = {
            "project_key": "webapp",  # 标量
            "source_type": ["git", "svn"],  # 列表
            "module": {"$prefix": "src/"},  # 已规范化
        }
        normalized = normalize_filter_dsl(filters)
        expected = {
            "project_key": {"$eq": "webapp"},
            "source_type": {"$in": ["git", "svn"]},
            "module": {"$prefix": "src/"},
        }
        assert normalized == expected


class TestBuildFilterDSL:
    """build_filter_dsl 便捷函数测试"""

    def test_build_empty(self):
        """测试空参数"""
        filters = build_filter_dsl()
        assert filters == {}

    def test_build_simple_fields(self):
        """测试简单字段"""
        filters = build_filter_dsl(
            project_key="webapp",
            source_type="git",
            owner_user_id="user123",
        )
        assert filters == {
            "project_key": "webapp",
            "source_type": "git",
            "owner_user_id": "user123",
        }

    def test_build_module_prefix(self):
        """测试 module 字段自动使用 $prefix"""
        filters = build_filter_dsl(module="src/components/")
        assert filters == {"module": {"$prefix": "src/components/"}}

    def test_build_commit_ts_range(self):
        """测试 commit_ts 范围"""
        filters = build_filter_dsl(
            commit_ts_gte="2024-01-01T00:00:00Z",
            commit_ts_lte="2024-12-31T23:59:59Z",
        )
        assert filters == {
            "commit_ts": {
                "$gte": "2024-01-01T00:00:00Z",
                "$lte": "2024-12-31T23:59:59Z",
            }
        }

    def test_build_commit_ts_gte_only(self):
        """测试仅设置 commit_ts 起始"""
        filters = build_filter_dsl(commit_ts_gte="2024-01-01")
        assert filters == {"commit_ts": {"$gte": "2024-01-01"}}

    def test_build_commit_ts_lte_only(self):
        """测试仅设置 commit_ts 结束"""
        filters = build_filter_dsl(commit_ts_lte="2024-12-31")
        assert filters == {"commit_ts": {"$lte": "2024-12-31"}}

    def test_build_complete(self):
        """测试完整参数"""
        filters = build_filter_dsl(
            project_key="webapp",
            module="src/api/",
            source_type="git",
            source_id="repo123",
            owner_user_id="user456",
            commit_ts_gte="2024-01-01",
            commit_ts_lte="2024-06-30",
        )
        expected = {
            "project_key": "webapp",
            "module": {"$prefix": "src/api/"},
            "source_type": "git",
            "source_id": "repo123",
            "owner_user_id": "user456",
            "commit_ts": {"$gte": "2024-01-01", "$lte": "2024-06-30"},
        }
        assert filters == expected


class TestQueryFiltersToFilterDict:
    """测试 QueryFilters.to_filter_dict 与 DSL 的一致性"""

    def test_empty_filters(self):
        """测试空过滤条件"""
        qf = QueryFilters()
        result = qf.to_filter_dict()
        assert result == {}
        # 应该能通过校验
        warnings = validate_filter_dsl(result)
        assert warnings == []

    def test_simple_equality_filters(self):
        """测试简单等值过滤"""
        qf = QueryFilters(
            project_key="webapp",
            source_type="git",
            source_id="repo123",
            owner_user_id="user456",
        )
        result = qf.to_filter_dict()
        
        # 验证格式
        assert result["project_key"] == "webapp"
        assert result["source_type"] == "git"
        assert result["source_id"] == "repo123"
        assert result["owner_user_id"] == "user456"
        
        # 应该能通过校验
        warnings = validate_filter_dsl(result)
        assert warnings == []

    def test_module_prefix_filter(self):
        """测试 module 使用 $prefix 格式"""
        qf = QueryFilters(module="src/components/")
        result = qf.to_filter_dict()
        
        # module 应该使用 $prefix 格式
        assert result["module"] == {"$prefix": "src/components/"}
        
        # 应该能通过校验
        warnings = validate_filter_dsl(result)
        assert warnings == []

    def test_commit_ts_range_filter(self):
        """测试 commit_ts 使用范围格式"""
        qf = QueryFilters(
            time_range_start="2024-01-01T00:00:00Z",
            time_range_end="2024-12-31T23:59:59Z",
        )
        result = qf.to_filter_dict()
        
        # commit_ts 应该使用 $gte/$lte 格式
        assert "commit_ts" in result
        assert result["commit_ts"]["$gte"] == "2024-01-01T00:00:00Z"
        assert result["commit_ts"]["$lte"] == "2024-12-31T23:59:59Z"
        
        # 应该能通过校验
        warnings = validate_filter_dsl(result)
        assert warnings == []

    def test_commit_ts_start_only(self):
        """测试仅设置时间范围起始"""
        qf = QueryFilters(time_range_start="2024-01-01")
        result = qf.to_filter_dict()
        
        assert result["commit_ts"] == {"$gte": "2024-01-01"}
        warnings = validate_filter_dsl(result)
        assert warnings == []

    def test_commit_ts_end_only(self):
        """测试仅设置时间范围结束"""
        qf = QueryFilters(time_range_end="2024-12-31")
        result = qf.to_filter_dict()
        
        assert result["commit_ts"] == {"$lte": "2024-12-31"}
        warnings = validate_filter_dsl(result)
        assert warnings == []

    def test_complete_filters(self):
        """测试完整的过滤条件"""
        qf = QueryFilters(
            project_key="webapp",
            module="src/api/users/",
            source_type="git",
            source_id="commit:abc123",
            owner_user_id="user789",
            time_range_start="2024-01-01",
            time_range_end="2024-06-30",
        )
        result = qf.to_filter_dict()
        
        expected = {
            "project_key": "webapp",
            "module": {"$prefix": "src/api/users/"},
            "source_type": "git",
            "source_id": "commit:abc123",
            "owner_user_id": "user789",
            "commit_ts": {"$gte": "2024-01-01", "$lte": "2024-06-30"},
        }
        assert result == expected
        
        # 应该能通过校验
        warnings = validate_filter_dsl(result)
        assert warnings == []

    def test_consistency_with_build_filter_dsl(self):
        """测试 QueryFilters 与 build_filter_dsl 的一致性"""
        # 使用 QueryFilters
        qf = QueryFilters(
            project_key="webapp",
            module="src/",
            source_type="git",
            owner_user_id="user1",
            time_range_start="2024-01-01",
            time_range_end="2024-12-31",
        )
        qf_result = qf.to_filter_dict()
        
        # 使用 build_filter_dsl
        build_result = build_filter_dsl(
            project_key="webapp",
            module="src/",
            source_type="git",
            owner_user_id="user1",
            commit_ts_gte="2024-01-01",
            commit_ts_lte="2024-12-31",
        )
        
        # 两者应该产生相同的 DSL
        assert qf_result == build_result


class TestDataStructures:
    """数据结构测试"""

    def test_chunk_doc_to_index_doc(self):
        """测试 ChunkDoc 转换为索引文档"""
        doc = ChunkDoc(
            chunk_id="proj:git:abc:sha256:v1:0",
            content="测试内容",
            project_key="proj",
            module="src/utils/",
            source_type="git",
            source_id="abc",
            commit_ts="2024-06-15T10:30:00Z",
        )
        index_doc = doc.to_index_doc()
        
        assert index_doc["chunk_id"] == "proj:git:abc:sha256:v1:0"
        assert index_doc["content"] == "测试内容"
        assert index_doc["project_key"] == "proj"
        assert index_doc["module"] == "src/utils/"
        assert index_doc["commit_ts"] == "2024-06-15T10:30:00Z"

    def test_query_request_validate(self):
        """测试 QueryRequest 验证"""
        # 有效请求
        valid_req = QueryRequest(query_text="test query")
        assert valid_req.validate() is True
        
        # 无效请求（无查询）
        invalid_req = QueryRequest()
        assert invalid_req.validate() is False
        
        # 无效请求（top_k <= 0）
        invalid_req2 = QueryRequest(query_text="test", top_k=0)
        assert invalid_req2.validate() is False

    def test_query_hit_from_dict(self):
        """测试 QueryHit 从字典构建"""
        data = {
            "chunk_id": "test:id:1",
            "content": "测试内容",
            "score": 0.95,
            "source_type": "git",
            "extra_field": "extra_value",
        }
        hit = QueryHit.from_dict(data)
        
        assert hit.chunk_id == "test:id:1"
        assert hit.content == "测试内容"
        assert hit.score == 0.95
        assert hit.source_type == "git"
        assert hit.metadata.get("extra_field") == "extra_value"


class TestFilterFieldsAndOperators:
    """测试过滤字段和操作符定义"""

    def test_required_fields_defined(self):
        """测试必需字段都已定义"""
        required_fields = [
            "project_key",
            "module",
            "source_type",
            "source_id",
            "owner_user_id",
            "commit_ts",
        ]
        for field in required_fields:
            assert field in FILTER_FIELDS, f"缺少必需字段: {field}"

    def test_required_operators_defined(self):
        """测试必需操作符都已定义"""
        required_operators = [
            "$eq",
            "$prefix",
            "$gte",
            "$lte",
            "$gt",
            "$lt",
            "$in",
        ]
        for op in required_operators:
            assert op in FILTER_OPERATORS, f"缺少必需操作符: {op}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
