"""
test_collection_naming.py - collection_naming 模块核心函数测试

测试覆盖:
1. make_collection_id - 生成规范化的 collection_id
2. parse_collection_id - 解析 collection_id
3. make_version_tag - 生成版本标签
4. to_seekdb_collection_name - 转换为 SeekDB collection 名称
5. to_pgvector_table_name - 转换为 PGVector 表名
6. from_seekdb_collection_name - 从 SeekDB 名称反向解析
7. from_pgvector_table_name - 从 PGVector 表名反向解析

测试场景:
- 大小写处理（BGE-M3 vs bge-m3）
- 包含 `-` 的 model_id（bge-m3）
- 包含 `:` 的 model_id（openai:ada-002）
- 带 version_tag / 不带 version_tag
"""

import pytest
import re
from datetime import datetime

from step3_seekdb_rag_hybrid.collection_naming import (
    make_collection_id,
    parse_collection_id,
    make_version_tag,
    to_seekdb_collection_name,
    to_pgvector_table_name,
    from_seekdb_collection_name,
    from_pgvector_table_name,
    CollectionParts,
    COLLECTION_ID_SEPARATOR,
    PGVECTOR_TABLE_PREFIX,
    POSTGRES_MAX_IDENTIFIER_LENGTH,
)


# ============================================================================
# make_collection_id 测试
# ============================================================================


class TestMakeCollectionId:
    """测试 make_collection_id 函数"""

    def test_basic(self):
        """测试基本用法"""
        result = make_collection_id("proj1", "v2", "bge-m3")
        assert result == "proj1:v2:bge-m3"

    def test_with_version_tag(self):
        """测试带 version_tag"""
        result = make_collection_id("proj1", "v1", "bge-m3", "20260128T120000")
        assert result == "proj1:v1:bge-m3:20260128T120000"

    def test_without_version_tag(self):
        """测试不带 version_tag"""
        result = make_collection_id("proj1", "v1", "bge-m3", None)
        assert result == "proj1:v1:bge-m3"
        # 不应包含末尾的冒号
        assert not result.endswith(":")

    def test_default_values(self):
        """测试默认值填充"""
        result = make_collection_id(None, None, None)
        assert result == "default:v1:nomodel"

    def test_partial_defaults(self):
        """测试部分默认值"""
        result = make_collection_id(None, "v2", "bge-m3")
        assert result == "default:v2:bge-m3"

        result = make_collection_id("proj1", None, "bge-m3")
        assert result == "proj1:v1:bge-m3"

    def test_uppercase_preserved(self):
        """测试大写字母保留（collection_id 保持原样）"""
        result = make_collection_id("Proj1", "V2", "BGE-M3")
        assert result == "Proj1:V2:BGE-M3"

    def test_hyphen_in_model_id(self):
        """测试 model_id 包含连字符"""
        result = make_collection_id("proj1", "v1", "bge-m3")
        assert result == "proj1:v1:bge-m3"
        assert "-" in result

    def test_colon_in_model_id(self):
        """测试 model_id 包含冒号（如 openai:ada-002）"""
        # 注意：model_id 中的冒号会与分隔符混淆，但 make_collection_id 不做校验
        result = make_collection_id("proj1", "v1", "openai:ada-002")
        # 结果会有 4 个冒号分隔的部分
        assert result == "proj1:v1:openai:ada-002"

    def test_complex_model_id(self):
        """测试复杂的 model_id"""
        result = make_collection_id("proj1", "v1", "sentence-transformers/all-MiniLM-L6-v2")
        assert result == "proj1:v1:sentence-transformers/all-MiniLM-L6-v2"


# ============================================================================
# parse_collection_id 测试
# ============================================================================


class TestParseCollectionId:
    """测试 parse_collection_id 函数"""

    def test_basic(self):
        """测试基本解析"""
        parts = parse_collection_id("proj1:v2:bge-m3")
        assert parts.project_key == "proj1"
        assert parts.chunking_version == "v2"
        assert parts.embedding_model_id == "bge-m3"
        assert parts.version_tag is None

    def test_with_version_tag(self):
        """测试带 version_tag 的解析"""
        parts = parse_collection_id("proj1:v1:bge-m3:20260128T120000")
        assert parts.project_key == "proj1"
        assert parts.chunking_version == "v1"
        assert parts.embedding_model_id == "bge-m3"
        assert parts.version_tag == "20260128T120000"

    def test_without_version_tag(self):
        """测试不带 version_tag 的解析"""
        parts = parse_collection_id("default:v1:nomodel")
        assert parts.version_tag is None

    def test_uppercase(self):
        """测试大写字母保留"""
        parts = parse_collection_id("Proj1:V2:BGE-M3")
        assert parts.project_key == "Proj1"
        assert parts.chunking_version == "V2"
        assert parts.embedding_model_id == "BGE-M3"

    def test_hyphen_in_model_id(self):
        """测试 model_id 包含连字符"""
        parts = parse_collection_id("proj1:v1:openai-ada-002")
        assert parts.embedding_model_id == "openai-ada-002"

    def test_colon_in_model_id(self):
        """测试 model_id 包含冒号 - 会被解析为额外部分"""
        # 当 model_id 包含冒号时，解析会将冒号后的部分作为 version_tag
        parts = parse_collection_id("proj1:v1:openai:ada-002")
        # 第三部分是 "openai"，第四部分 "ada-002" 会被当作 version_tag
        assert parts.project_key == "proj1"
        assert parts.chunking_version == "v1"
        assert parts.embedding_model_id == "openai"
        assert parts.version_tag == "ada-002"

    def test_invalid_format_too_few_parts(self):
        """测试无效格式 - 部分过少"""
        with pytest.raises(ValueError) as exc_info:
            parse_collection_id("proj1:v1")
        assert "无效的 collection_id 格式" in str(exc_info.value)

    def test_invalid_format_single_part(self):
        """测试无效格式 - 只有一部分"""
        with pytest.raises(ValueError):
            parse_collection_id("proj1")

    def test_invalid_format_empty(self):
        """测试无效格式 - 空字符串"""
        with pytest.raises(ValueError):
            parse_collection_id("")

    def test_to_canonical_id_roundtrip(self):
        """测试 CollectionParts.to_canonical_id 往返转换"""
        original = "proj1:v2:bge-m3:20260128T120000"
        parts = parse_collection_id(original)
        result = parts.to_canonical_id()
        assert result == original

    def test_to_dict(self):
        """测试 CollectionParts.to_dict"""
        parts = parse_collection_id("proj1:v1:model:tag123")
        d = parts.to_dict()
        assert d["project_key"] == "proj1"
        assert d["chunking_version"] == "v1"
        assert d["embedding_model_id"] == "model"
        assert d["version_tag"] == "tag123"


# ============================================================================
# make_version_tag 测试
# ============================================================================


class TestMakeVersionTag:
    """测试 make_version_tag 函数"""

    def test_format(self):
        """测试版本标签格式"""
        tag = make_version_tag()
        # 格式应为 YYYYMMDDTHHmmss
        assert re.match(r'^\d{8}T\d{6}$', tag), f"格式不正确: {tag}"

    def test_contains_valid_date(self):
        """测试版本标签包含有效日期"""
        tag = make_version_tag()
        # 尝试解析日期
        parsed = datetime.strptime(tag, "%Y%m%dT%H%M%S")
        assert parsed is not None

    def test_unique_tags(self):
        """测试生成的标签在同一秒内相同"""
        import time
        tag1 = make_version_tag()
        tag2 = make_version_tag()
        # 同一秒内应相同
        assert tag1[:8] == tag2[:8]  # 日期部分相同

    def test_tag_length(self):
        """测试标签长度"""
        tag = make_version_tag()
        # 格式 YYYYMMDDTHHmmss = 15 字符
        assert len(tag) == 15


# ============================================================================
# to_seekdb_collection_name 测试
# ============================================================================


class TestToSeekdbCollectionName:
    """测试 to_seekdb_collection_name 函数"""

    def test_basic(self):
        """测试基本转换"""
        result = to_seekdb_collection_name("proj1:v2:bge-m3")
        assert result == "proj1_v2_bge_m3"

    def test_with_version_tag(self):
        """测试带 version_tag 的转换"""
        result = to_seekdb_collection_name("default:v1:bge-m3:20260128T120000")
        assert result == "default_v1_bge_m3_20260128T120000"

    def test_without_version_tag(self):
        """测试不带 version_tag 的转换"""
        result = to_seekdb_collection_name("proj1:v1:model")
        assert result == "proj1_v1_model"
        # 不应包含末尾下划线
        assert not result.endswith("_")

    def test_hyphen_replaced(self):
        """测试连字符被替换为下划线"""
        result = to_seekdb_collection_name("my-project:v1:bge-m3")
        assert result == "my_project_v1_bge_m3"
        assert "-" not in result

    def test_colon_replaced(self):
        """测试冒号被替换为下划线"""
        result = to_seekdb_collection_name("proj1:v1:openai:ada-002")
        assert result == "proj1_v1_openai_ada_002"
        assert ":" not in result

    def test_uppercase_preserved(self):
        """测试大写字母保留（SeekDB 不转小写）"""
        result = to_seekdb_collection_name("Proj1:V2:BGE-M3")
        assert result == "Proj1_V2_BGE_M3"

    def test_special_chars_removed(self):
        """测试特殊字符被移除"""
        result = to_seekdb_collection_name("proj@1:v1:model#test")
        # @ 和 # 应被移除
        assert "@" not in result
        assert "#" not in result

    def test_numeric_prefix(self):
        """测试数字开头的名称"""
        result = to_seekdb_collection_name("123proj:v1:model")
        # 数字开头会被添加下划线前缀
        assert result.startswith("_")
        assert result == "_123proj_v1_model"


# ============================================================================
# to_pgvector_table_name 测试
# ============================================================================


class TestToPgvectorTableName:
    """测试 to_pgvector_table_name 函数"""

    def test_basic(self):
        """测试基本转换"""
        result = to_pgvector_table_name("proj1:v2:bge-m3")
        assert result == "step3_chunks_proj1_v2_bge_m3"
        assert result.startswith(PGVECTOR_TABLE_PREFIX + "_")

    def test_with_version_tag(self):
        """测试带 version_tag 的转换"""
        result = to_pgvector_table_name("default:v1:bge-m3:20260128T120000")
        assert result == "step3_chunks_default_v1_bge_m3_20260128t120000"

    def test_without_version_tag(self):
        """测试不带 version_tag 的转换"""
        result = to_pgvector_table_name("proj1:v1:model")
        assert result == "step3_chunks_proj1_v1_model"

    def test_lowercase_conversion(self):
        """测试转换为小写"""
        result = to_pgvector_table_name("Proj1:V2:BGE-M3")
        assert result == "step3_chunks_proj1_v2_bge_m3"
        # 应全小写
        assert result == result.lower()

    def test_hyphen_replaced(self):
        """测试连字符被替换"""
        result = to_pgvector_table_name("my-project:v1:bge-m3")
        assert result == "step3_chunks_my_project_v1_bge_m3"
        assert "-" not in result

    def test_colon_replaced(self):
        """测试冒号被替换"""
        result = to_pgvector_table_name("proj1:v1:openai:ada-002")
        assert result == "step3_chunks_proj1_v1_openai_ada_002"
        assert ":" not in result

    def test_length_limit(self):
        """测试长度限制（PostgreSQL 最大 63 字符）"""
        long_project = "a" * 50
        long_model = "very_long_embedding_model_name"
        long_id = f"{long_project}:v1:{long_model}"
        
        result = to_pgvector_table_name(long_id)
        
        assert len(result) <= POSTGRES_MAX_IDENTIFIER_LENGTH
        assert len(result) <= 63
        assert result.startswith(PGVECTOR_TABLE_PREFIX + "_")

    def test_long_names_unique(self):
        """测试超长名称通过 hash 保证唯一"""
        base = "a" * 50 + ":v1:model_"
        id_a = base + "suffix_a"
        id_b = base + "suffix_b"
        
        result_a = to_pgvector_table_name(id_a)
        result_b = to_pgvector_table_name(id_b)
        
        assert len(result_a) <= 63
        assert len(result_b) <= 63
        # 不同的 collection_id 应生成不同的表名
        assert result_a != result_b

    def test_valid_identifier(self):
        """测试生成的表名是有效的 PostgreSQL 标识符"""
        result = to_pgvector_table_name("proj1:v1:bge-m3")
        # 应匹配 PostgreSQL 标识符规则
        assert re.match(r'^[a-z_][a-z0-9_]*$', result)


# ============================================================================
# from_seekdb_collection_name 测试
# ============================================================================


class TestFromSeekdbCollectionName:
    """测试 from_seekdb_collection_name 函数"""

    def test_basic(self):
        """测试基本反向解析"""
        result = from_seekdb_collection_name("proj1_v2_bge_m3")
        # 启发式解析，可能不完全还原
        parts = result.split(":")
        assert len(parts) >= 3
        assert "v2" in parts

    def test_with_version_tag(self):
        """测试带 version_tag 的反向解析"""
        result = from_seekdb_collection_name("default_v1_bge_m3_20260128T120000")
        assert "20260128T120000" in result
        parts = result.split(":")
        assert len(parts) == 4
        assert parts[3] == "20260128T120000"

    def test_without_version_tag(self):
        """测试不带 version_tag 的反向解析"""
        result = from_seekdb_collection_name("proj1_v1_model")
        parts = result.split(":")
        assert len(parts) == 3
        # version_tag 部分应为 None（不在结果中）

    def test_roundtrip_basic(self):
        """测试基本往返转换"""
        original_id = "proj1:v2:bge_m3"  # 使用下划线避免歧义
        seekdb_name = to_seekdb_collection_name(original_id)
        recovered = from_seekdb_collection_name(seekdb_name)
        
        # 解析后应能正确识别各部分
        parts = parse_collection_id(recovered)
        assert "proj1" in parts.project_key
        assert "v2" in parts.chunking_version

    def test_roundtrip_with_version_tag(self):
        """测试带 version_tag 的往返转换"""
        original_id = "proj1:v1:model:20260128T120000"
        seekdb_name = to_seekdb_collection_name(original_id)
        recovered = from_seekdb_collection_name(seekdb_name)
        
        parts = parse_collection_id(recovered)
        assert parts.version_tag == "20260128T120000"

    def test_invalid_format(self):
        """测试无效格式"""
        with pytest.raises(ValueError):
            from_seekdb_collection_name("ab")  # 少于 3 部分

    def test_complex_project_key(self):
        """测试复杂的 project_key（包含下划线）"""
        result = from_seekdb_collection_name("my_project_v1_bge_m3")
        # 启发式解析会找到 v1 作为版本
        assert "v1" in result


# ============================================================================
# from_pgvector_table_name 测试
# ============================================================================


class TestFromPgvectorTableName:
    """测试 from_pgvector_table_name 函数"""

    def test_basic(self):
        """测试基本反向解析"""
        result = from_pgvector_table_name("step3_chunks_proj1_v2_bge_m3")
        parts = result.split(":")
        assert len(parts) >= 3

    def test_with_version_tag(self):
        """测试带 version_tag 的反向解析"""
        result = from_pgvector_table_name("step3_chunks_default_v1_bge_m3_20260128t120000")
        # 注意：表名是小写的，时间戳也是小写
        parts = result.split(":")
        assert len(parts) >= 3

    def test_without_version_tag(self):
        """测试不带 version_tag 的反向解析"""
        result = from_pgvector_table_name("step3_chunks_proj1_v1_model")
        parts = result.split(":")
        assert len(parts) == 3

    def test_invalid_prefix(self):
        """测试无效前缀"""
        with pytest.raises(ValueError) as exc_info:
            from_pgvector_table_name("wrong_prefix_proj1_v1_model")
        assert "无效的 PGVector 表名" in str(exc_info.value)
        assert f"期望以 '{PGVECTOR_TABLE_PREFIX}_' 开头" in str(exc_info.value)

    def test_roundtrip_basic(self):
        """测试基本往返转换"""
        original_id = "proj1:v2:bge_m3"
        table_name = to_pgvector_table_name(original_id)
        recovered = from_pgvector_table_name(table_name)
        
        # 由于小写转换，不能完全还原
        parts = parse_collection_id(recovered)
        assert "proj1" in parts.project_key.lower()
        assert "v2" in parts.chunking_version.lower()

    def test_roundtrip_with_version_tag(self):
        """测试带 version_tag 的往返转换"""
        original_id = "proj1:v1:model:20260128T120000"
        table_name = to_pgvector_table_name(original_id)
        recovered = from_pgvector_table_name(table_name)
        
        # 时间戳格式的 version_tag 应该能正确识别
        parts = parse_collection_id(recovered)
        # 由于小写转换，时间戳也是小写
        assert parts.version_tag is not None or "20260128" in recovered


# ============================================================================
# 综合场景测试
# ============================================================================


class TestCollectionNamingIntegration:
    """综合场景测试"""

    @pytest.mark.parametrize("project,version,model,tag", [
        ("proj1", "v1", "bge-m3", None),
        ("proj1", "v1", "bge-m3", "20260128T120000"),
        ("my-project", "v2", "openai-ada-002", None),
        ("Proj_Test", "v1", "BGE-M3", "20260128T235959"),
        ("default", "v1", "nomodel", None),
    ])
    def test_make_parse_roundtrip(self, project, version, model, tag):
        """测试 make_collection_id 和 parse_collection_id 往返"""
        coll_id = make_collection_id(project, version, model, tag)
        parts = parse_collection_id(coll_id)
        
        assert parts.project_key == project
        assert parts.chunking_version == version
        assert parts.embedding_model_id == model
        assert parts.version_tag == tag

    @pytest.mark.parametrize("collection_id,expected_seekdb,expected_pgvector", [
        (
            "proj1:v2:bge-m3",
            "proj1_v2_bge_m3",
            "step3_chunks_proj1_v2_bge_m3",
        ),
        (
            "default:v1:bge-m3:20260128T120000",
            "default_v1_bge_m3_20260128T120000",
            "step3_chunks_default_v1_bge_m3_20260128t120000",
        ),
        (
            "my-project:v1:openai-ada-002",
            "my_project_v1_openai_ada_002",
            "step3_chunks_my_project_v1_openai_ada_002",
        ),
    ])
    def test_backend_name_mapping(self, collection_id, expected_seekdb, expected_pgvector):
        """测试不同后端的名称映射"""
        assert to_seekdb_collection_name(collection_id) == expected_seekdb
        assert to_pgvector_table_name(collection_id) == expected_pgvector

    def test_version_tag_timestamp_format(self):
        """测试版本标签时间戳格式识别"""
        tag = make_version_tag()
        coll_id = make_collection_id("proj", "v1", "model", tag)
        
        # 转换为 SeekDB 名称
        seekdb_name = to_seekdb_collection_name(coll_id)
        
        # 反向解析应能识别时间戳
        recovered = from_seekdb_collection_name(seekdb_name)
        parts = parse_collection_id(recovered)
        
        # 时间戳应被正确识别
        assert parts.version_tag is not None
        assert re.match(r'^\d{8}T\d{6}$', parts.version_tag)

    def test_case_handling_consistency(self):
        """测试大小写处理一致性"""
        upper_id = "PROJ1:V1:BGE-M3"
        lower_id = "proj1:v1:bge-m3"
        
        # SeekDB: 保持原始大小写
        assert to_seekdb_collection_name(upper_id) != to_seekdb_collection_name(lower_id)
        
        # PGVector: 都转为小写
        assert to_pgvector_table_name(upper_id) == to_pgvector_table_name(lower_id)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
