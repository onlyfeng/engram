#!/usr/bin/env python3
"""
test_seek_query_packet_shape.py - seek_query 输出格式与 Filter DSL 端到端测试

测试内容:
1. EvidenceResult 字段完整性与映射
2. --output-format packet/full 语义
3. filters 的 $prefix/$gte/$lte 端到端过滤
4. active_collection 模块：namespace/key 生成、resolve 优先级

使用 Mock Backend 进行测试，无需真实数据库连接。
"""

import json
import pytest
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

# 导入被测模块
from step3_seekdb_rag_hybrid.seek_query import (
    EvidenceResult,
    QueryFilters,
    QueryResult,
    RetrievalContext,
    query_evidence,
    query_evidence_dual_read,
    run_query,
    set_index_backend,
    set_shadow_backend,
    set_dual_read_config,
    set_embedding_provider_instance,
    _compare_results,
    DualReadStats,
    DualReadGateThresholds,
    DualReadGateResult,
    DualReadGateViolation,
    GateProfile,
    check_dual_read_gate,
    compute_dual_read_stats,
)
from step3_seekdb_rag_hybrid.dual_read_compare import (
    THRESHOLD_SOURCE_CLI,
    THRESHOLD_SOURCE_DEFAULT,
    THRESHOLD_SOURCE_ENV,
)
from step3_seekdb_rag_hybrid.step3_backend_factory import DualReadConfig
from step3_seekdb_rag_hybrid.index_backend import (
    IndexBackend,
    QueryRequest,
    QueryHit,
    FilterDSL,
    validate_filter_dsl,
)
from step3_seekdb_rag_hybrid.active_collection import (
    make_kv_namespace,
    make_active_collection_key,
    get_active_collection,
    set_active_collection,
    get_default_collection_id,
    resolve_collection_id,
    KV_NAMESPACE_PREFIX,
    ACTIVE_COLLECTION_KEY,
)

# Step1 URI 解析函数，用于集成测试
from engram_step1.uri import parse_attachment_evidence_uri


# ============ Mock Backend ============


class MockIndexBackend(IndexBackend):
    """
    Mock 索引后端，用于端到端测试
    
    支持模拟 Filter DSL 过滤逻辑和 collection 选择测试。
    """
    
    def __init__(
        self,
        mock_data: Optional[List[Dict[str, Any]]] = None,
        collection_id: Optional[str] = None,
    ):
        self._mock_data = mock_data or []
        self._query_history: List[QueryRequest] = []
        self._collection_id = collection_id or "default:v1:mock-model"
    
    @property
    def backend_name(self) -> str:
        return "mock"
    
    @property
    def supports_vector_search(self) -> bool:
        return True
    
    @property
    def canonical_id(self) -> str:
        """获取 canonical collection_id（冒号格式）"""
        return self._collection_id
    
    @property
    def collection_id(self) -> str:
        """collection_id 别名（兼容）"""
        return self._collection_id
    
    def set_collection_id(self, collection_id: str) -> None:
        """设置 collection_id（用于测试）"""
        self._collection_id = collection_id
    
    def set_mock_data(self, data: List[Dict[str, Any]]) -> None:
        """设置 mock 数据"""
        self._mock_data = data
    
    def get_query_history(self) -> List[QueryRequest]:
        """获取查询历史"""
        return self._query_history
    
    def clear_history(self) -> None:
        """清空查询历史"""
        self._query_history = []
    
    def upsert(self, docs: List[Any]) -> int:
        return len(docs)
    
    def delete(self, chunk_ids: List[str]) -> int:
        return len(chunk_ids)
    
    def delete_by_filter(self, filters: FilterDSL) -> int:
        return 0
    
    def health_check(self) -> Dict[str, Any]:
        return {"status": "healthy", "backend": "mock"}
    
    def get_stats(self) -> Dict[str, Any]:
        return {"total_docs": len(self._mock_data)}
    
    def query(self, request: QueryRequest) -> List[QueryHit]:
        """执行查询，应用 Filter DSL 过滤"""
        self._query_history.append(request)
        
        results = []
        for doc in self._mock_data:
            # 应用过滤条件
            if not self._match_filters(doc, request.filters):
                continue
            
            hit = QueryHit(
                chunk_id=doc.get("chunk_id", ""),
                content=doc.get("content", ""),
                score=doc.get("score", 0.8),
                source_type=doc.get("source_type", ""),
                source_id=doc.get("source_id", ""),
                artifact_uri=doc.get("artifact_uri", ""),
                chunk_idx=doc.get("chunk_idx", 0),
                sha256=doc.get("sha256", ""),
                excerpt=doc.get("excerpt", ""),
                metadata=doc.get("metadata", {}),
            )
            results.append(hit)
        
        # 按 score 降序排序
        results.sort(key=lambda x: x.score, reverse=True)
        
        # 限制返回数量
        return results[:request.top_k]
    
    def _match_filters(self, doc: Dict[str, Any], filters: FilterDSL) -> bool:
        """检查文档是否匹配过滤条件"""
        for field_name, field_value in filters.items():
            doc_value = doc.get(field_name)
            
            if isinstance(field_value, dict):
                # 操作符格式
                for op, op_value in field_value.items():
                    if op == "$eq":
                        if doc_value != op_value:
                            return False
                    elif op == "$prefix":
                        if not doc_value or not str(doc_value).startswith(str(op_value)):
                            return False
                    elif op == "$gte":
                        if not doc_value or doc_value < op_value:
                            return False
                    elif op == "$lte":
                        if not doc_value or doc_value > op_value:
                            return False
                    elif op == "$gt":
                        if not doc_value or doc_value <= op_value:
                            return False
                    elif op == "$lt":
                        if not doc_value or doc_value >= op_value:
                            return False
                    elif op == "$in":
                        if doc_value not in op_value:
                            return False
            else:
                # 直接值（$eq）
                if doc_value != field_value:
                    return False
        
        return True


# ============ Test Fixtures ============


@pytest.fixture
def mock_backend():
    """创建 Mock 后端"""
    backend = MockIndexBackend()
    set_index_backend(backend)
    set_embedding_provider_instance(None)  # 禁用 embedding
    yield backend
    set_index_backend(None)


@pytest.fixture
def sample_docs():
    """样本文档数据"""
    return [
        {
            "chunk_id": "webapp:git:1:abc123:v1:0",
            "content": "修复了登录页面的 XSS 漏洞",
            "score": 0.95,
            "source_type": "git",
            "source_id": "1:abc123",
            "artifact_uri": "memory://patch_blobs/git/1:abc123/sha256_abc",
            "chunk_idx": 0,
            "sha256": "sha256_abc",
            "excerpt": "修复登录页面 XSS 漏洞，添加输入验证...",
            "project_key": "webapp",
            "module": "src/auth/login.py",
            "commit_ts": "2024-06-15T10:00:00Z",
            "metadata": {"author": "alice"},
        },
        {
            "chunk_id": "webapp:git:1:def456:v1:0",
            "content": "优化数据库连接池配置",
            "score": 0.88,
            "source_type": "git",
            "source_id": "1:def456",
            "artifact_uri": "memory://patch_blobs/git/1:def456/sha256_def",
            "chunk_idx": 0,
            "sha256": "sha256_def",
            "excerpt": "优化数据库连接池，提升性能...",
            "project_key": "webapp",
            "module": "src/db/connection.py",
            "commit_ts": "2024-07-20T14:30:00Z",
            "metadata": {"author": "bob"},
        },
        {
            "chunk_id": "api:svn:100:r500:v1:0",
            "content": "API 接口添加认证检查",
            "score": 0.82,
            "source_type": "svn",
            "source_id": "100:r500",
            "artifact_uri": "memory://patch_blobs/svn/100:r500/sha256_svn",
            "chunk_idx": 0,
            "sha256": "sha256_svn",
            "excerpt": "添加 API 认证检查...",
            "project_key": "api",
            "module": "lib/api/auth.py",
            "commit_ts": "2024-05-10T08:00:00Z",
            "metadata": {"author": "charlie"},
        },
        {
            "chunk_id": "webapp:logbook:12345:v1:0",
            "content": "发布 v2.0 版本记录",
            "score": 0.75,
            "source_type": "logbook",
            "source_id": "12345",
            "artifact_uri": "memory://attachments/12345/sha256_log",
            "chunk_idx": 0,
            "sha256": "sha256_log",
            "excerpt": "v2.0 发布版本记录...",
            "project_key": "webapp",
            "module": "docs/release_notes.md",
            "commit_ts": "2024-08-01T16:00:00Z",
            "metadata": {"author": "dave"},
        },
    ]


# ============ Test: EvidenceResult 字段映射 ============


class TestEvidenceResultMapping:
    """测试 EvidenceResult 字段映射"""
    
    def test_from_query_hit_all_fields(self):
        """测试 from_query_hit 映射所有字段"""
        hit = QueryHit(
            chunk_id="test:git:1:abc:v1:0",
            content="测试内容",
            score=0.92,
            source_type="git",
            source_id="1:abc",
            artifact_uri="memory://patch_blobs/git/1:abc/sha256",
            chunk_idx=0,
            sha256="sha256_value",
            excerpt="摘要文本",
            metadata={"project_key": "test", "author": "alice"},
        )
        
        result = EvidenceResult.from_query_hit(hit)
        
        assert result.chunk_id == "test:git:1:abc:v1:0"
        assert result.chunk_idx == 0
        assert result.content == "测试内容"
        assert result.artifact_uri == "memory://patch_blobs/git/1:abc/sha256"
        assert result.evidence_uri == result.artifact_uri  # 别名
        assert result.sha256 == "sha256_value"
        assert result.source_id == "1:abc"
        assert result.source_type == "git"
        assert result.excerpt == "摘要文本"
        assert result.relevance_score == 0.92
        assert result.metadata["project_key"] == "test"
    
    def test_to_evidence_dict_with_content(self):
        """测试 to_evidence_dict 包含 content"""
        result = EvidenceResult(
            chunk_id="test:git:1:abc:v1:0",
            chunk_idx=0,
            content="完整内容文本",
            artifact_uri="memory://patch_blobs/git/1:abc/sha256",
            sha256="sha256_value",
            source_id="1:abc",
            source_type="git",
            excerpt="摘要",
            relevance_score=0.9,
            metadata={"key": "value"},
        )
        
        evidence_dict = result.to_evidence_dict(include_content=True)
        
        assert evidence_dict["content"] == "完整内容文本"
        assert evidence_dict["artifact_uri"] == "memory://patch_blobs/git/1:abc/sha256"
        assert evidence_dict["evidence_uri"] == evidence_dict["artifact_uri"]
        assert evidence_dict["metadata"] == {"key": "value"}
    
    def test_to_evidence_dict_without_content(self):
        """测试 to_evidence_dict 不包含 content（packet 格式）"""
        result = EvidenceResult(
            chunk_id="test:git:1:abc:v1:0",
            chunk_idx=0,
            content="完整内容文本",
            artifact_uri="memory://patch_blobs/git/1:abc/sha256",
            sha256="sha256_value",
            source_id="1:abc",
            source_type="git",
            excerpt="摘要",
            relevance_score=0.9,
            metadata={},
        )
        
        evidence_dict = result.to_evidence_dict(include_content=False)
        
        assert "content" not in evidence_dict
        assert evidence_dict["excerpt"] == "摘要"
        assert evidence_dict["artifact_uri"] == "memory://patch_blobs/git/1:abc/sha256"


# ============ Test: output-format packet/full 语义 ============


class TestOutputFormat:
    """测试 --output-format packet/full 的语义"""
    
    def test_packet_format_no_content(self, mock_backend, sample_docs):
        """packet 格式不包含 content"""
        mock_backend.set_mock_data(sample_docs)
        
        result = run_query("XSS 漏洞", top_k=5)
        packet = result.to_evidence_packet()
        
        # 验证 packet 结构
        assert "query" in packet
        assert "evidences" in packet
        assert "chunking_version" in packet
        assert "generated_at" in packet
        assert "result_count" in packet
        
        # packet 不应包含 filters（当没有过滤时）
        assert "filters" not in packet or packet["filters"] is None or packet["filters"] == {}
        
        # 验证 evidence 条目不包含 content
        for ev in packet["evidences"]:
            assert "content" not in ev
            assert "excerpt" in ev
            assert "artifact_uri" in ev
            assert "evidence_uri" in ev
    
    def test_full_format_includes_content(self, mock_backend, sample_docs):
        """full 格式包含 content"""
        mock_backend.set_mock_data(sample_docs)
        
        result = run_query("XSS 漏洞", top_k=5)
        full_output = result.to_dict()
        
        # 验证 full 结构
        assert "success" in full_output
        assert "query" in full_output
        assert "filters" in full_output
        assert "top_k" in full_output
        assert "result_count" in full_output
        assert "evidences" in full_output
        assert "timing" in full_output
        
        # 验证 evidence 条目包含 content
        for ev in full_output["evidences"]:
            assert "content" in ev
            assert "artifact_uri" in ev
            assert "evidence_uri" in ev
            assert "metadata" in ev
    
    def test_packet_with_filters_included(self, mock_backend, sample_docs):
        """packet 格式包含有效过滤条件"""
        mock_backend.set_mock_data(sample_docs)
        
        filters = QueryFilters(project_key="webapp", source_type="git")
        result = run_query("XSS 漏洞", filters=filters, top_k=5)
        packet = result.to_evidence_packet()
        
        # 验证 filters 被包含
        assert "filters" in packet
        assert packet["filters"]["project_key"] == "webapp"
        assert packet["filters"]["source_type"] == "git"


# ============ Test: Filter DSL 端到端测试 ============


class TestFilterDSLEndToEnd:
    """Filter DSL ($prefix/$gte/$lte) 端到端测试"""
    
    def test_prefix_filter_module(self, mock_backend, sample_docs):
        """测试 $prefix 操作符过滤 module 字段"""
        mock_backend.set_mock_data(sample_docs)
        
        # 过滤 src/auth/ 前缀
        filters = QueryFilters(module="src/auth/")
        results = query_evidence("XSS", filters=filters, top_k=10)
        
        # 只有 src/auth/login.py 匹配
        assert len(results) == 1
        assert results[0].source_id == "1:abc123"
        assert "abc123" in results[0].artifact_uri  # 验证 artifact_uri 包含正确的 source_id
        
        # 验证 Filter DSL 格式正确
        query_req = mock_backend.get_query_history()[-1]
        assert query_req.filters["module"] == {"$prefix": "src/auth/"}
    
    def test_prefix_filter_src_all(self, mock_backend, sample_docs):
        """测试 $prefix 匹配多个结果"""
        mock_backend.set_mock_data(sample_docs)
        
        # 过滤 src/ 前缀（匹配 src/auth/ 和 src/db/）
        filters = QueryFilters(module="src/")
        results = query_evidence("优化", filters=filters, top_k=10)
        
        # 两条结果
        assert len(results) == 2
        for r in results:
            assert r.metadata.get("project_key") == "webapp" or "webapp" in r.chunk_id
    
    def test_gte_filter_commit_ts(self, mock_backend, sample_docs):
        """测试 $gte 操作符过滤 commit_ts"""
        mock_backend.set_mock_data(sample_docs)
        
        # 过滤 2024-07-01 之后的提交
        filters = QueryFilters(time_range_start="2024-07-01T00:00:00Z")
        results = query_evidence("数据库", filters=filters, top_k=10)
        
        # 只有 2024-07-20 和 2024-08-01 的提交
        assert len(results) == 2
        
        # 验证 Filter DSL 格式
        query_req = mock_backend.get_query_history()[-1]
        assert query_req.filters["commit_ts"]["$gte"] == "2024-07-01T00:00:00Z"
    
    def test_lte_filter_commit_ts(self, mock_backend, sample_docs):
        """测试 $lte 操作符过滤 commit_ts"""
        mock_backend.set_mock_data(sample_docs)
        
        # 过滤 2024-06-30 之前的提交
        filters = QueryFilters(time_range_end="2024-06-30T23:59:59Z")
        results = query_evidence("漏洞", filters=filters, top_k=10)
        
        # 只有 2024-06-15 和 2024-05-10 的提交
        assert len(results) == 2
        
        # 验证 Filter DSL 格式
        query_req = mock_backend.get_query_history()[-1]
        assert query_req.filters["commit_ts"]["$lte"] == "2024-06-30T23:59:59Z"
    
    def test_range_filter_commit_ts(self, mock_backend, sample_docs):
        """测试 $gte + $lte 组合范围过滤"""
        mock_backend.set_mock_data(sample_docs)
        
        # 过滤 2024-06-01 到 2024-07-31 之间的提交
        filters = QueryFilters(
            time_range_start="2024-06-01T00:00:00Z",
            time_range_end="2024-07-31T23:59:59Z",
        )
        results = query_evidence("修复", filters=filters, top_k=10)
        
        # 只有 2024-06-15 和 2024-07-20 的提交
        assert len(results) == 2
        
        # 验证 Filter DSL 格式
        query_req = mock_backend.get_query_history()[-1]
        assert query_req.filters["commit_ts"]["$gte"] == "2024-06-01T00:00:00Z"
        assert query_req.filters["commit_ts"]["$lte"] == "2024-07-31T23:59:59Z"
    
    def test_combined_filters(self, mock_backend, sample_docs):
        """测试组合过滤条件"""
        mock_backend.set_mock_data(sample_docs)
        
        # 组合: project_key + source_type + module 前缀
        filters = QueryFilters(
            project_key="webapp",
            source_type="git",
            module="src/",
        )
        results = query_evidence("优化", filters=filters, top_k=10)
        
        # 验证过滤条件生效
        assert len(results) == 2
        for r in results:
            assert r.source_type == "git"
        
        # 验证 Filter DSL
        query_req = mock_backend.get_query_history()[-1]
        assert query_req.filters["project_key"] == "webapp"
        assert query_req.filters["source_type"] == "git"
        assert query_req.filters["module"] == {"$prefix": "src/"}
    
    def test_source_type_filter(self, mock_backend, sample_docs):
        """测试 source_type 过滤"""
        mock_backend.set_mock_data(sample_docs)
        
        # 过滤 svn 类型
        filters = QueryFilters(source_type="svn")
        results = query_evidence("API", filters=filters, top_k=10)
        
        assert len(results) == 1
        assert results[0].source_type == "svn"
        assert results[0].source_id == "100:r500"
    
    def test_empty_results_with_strict_filter(self, mock_backend, sample_docs):
        """测试过滤条件过严导致无结果"""
        mock_backend.set_mock_data(sample_docs)
        
        # 不存在的项目
        filters = QueryFilters(project_key="nonexistent")
        results = query_evidence("XSS", filters=filters, top_k=10)
        
        assert len(results) == 0


# ============ Test: QueryFilters.to_filter_dict ============


class TestQueryFiltersToFilterDict:
    """测试 QueryFilters.to_filter_dict 生成的 DSL"""
    
    def test_simple_fields(self):
        """测试简单字段转换"""
        filters = QueryFilters(
            project_key="webapp",
            source_type="git",
            source_id="1:abc",
            owner_user_id="alice",
        )
        dsl = filters.to_filter_dict()
        
        assert dsl["project_key"] == "webapp"
        assert dsl["source_type"] == "git"
        assert dsl["source_id"] == "1:abc"
        assert dsl["owner_user_id"] == "alice"
    
    def test_module_prefix(self):
        """测试 module 字段使用 $prefix"""
        filters = QueryFilters(module="src/auth/")
        dsl = filters.to_filter_dict()
        
        assert dsl["module"] == {"$prefix": "src/auth/"}
    
    def test_time_range_gte_only(self):
        """测试仅有起始时间"""
        filters = QueryFilters(time_range_start="2024-01-01T00:00:00Z")
        dsl = filters.to_filter_dict()
        
        assert dsl["commit_ts"] == {"$gte": "2024-01-01T00:00:00Z"}
    
    def test_time_range_lte_only(self):
        """测试仅有结束时间"""
        filters = QueryFilters(time_range_end="2024-12-31T23:59:59Z")
        dsl = filters.to_filter_dict()
        
        assert dsl["commit_ts"] == {"$lte": "2024-12-31T23:59:59Z"}
    
    def test_time_range_both(self):
        """测试时间范围两端"""
        filters = QueryFilters(
            time_range_start="2024-01-01T00:00:00Z",
            time_range_end="2024-12-31T23:59:59Z",
        )
        dsl = filters.to_filter_dict()
        
        assert dsl["commit_ts"]["$gte"] == "2024-01-01T00:00:00Z"
        assert dsl["commit_ts"]["$lte"] == "2024-12-31T23:59:59Z"
    
    def test_empty_filters(self):
        """测试空过滤条件"""
        filters = QueryFilters()
        dsl = filters.to_filter_dict()
        
        assert dsl == {}
    
    def test_dsl_passes_validation(self):
        """测试生成的 DSL 通过校验"""
        filters = QueryFilters(
            project_key="webapp",
            module="src/",
            source_type="git",
            time_range_start="2024-01-01T00:00:00Z",
            time_range_end="2024-12-31T23:59:59Z",
        )
        dsl = filters.to_filter_dict()
        
        # 应该不抛出异常
        warnings = validate_filter_dsl(dsl)
        assert warnings == []  # 无警告


# ============ Test: Evidence URI 别名 ============


class TestEvidenceUriAlias:
    """测试 evidence_uri 作为 artifact_uri 别名"""
    
    def test_evidence_uri_property(self):
        """测试 evidence_uri 属性"""
        result = EvidenceResult(
            chunk_id="test:git:1:abc:v1:0",
            chunk_idx=0,
            content="内容",
            artifact_uri="memory://patch_blobs/git/1:abc/sha256",
            sha256="sha256_value",
            source_id="1:abc",
            source_type="git",
            excerpt="摘要",
        )
        
        assert result.evidence_uri == result.artifact_uri
        assert result.evidence_uri == "memory://patch_blobs/git/1:abc/sha256"
    
    def test_from_index_result_with_evidence_uri(self):
        """测试从 index_result 读取 evidence_uri"""
        index_result = {
            "chunk_id": "test:git:1:abc:v1:0",
            "content": "内容",
            "evidence_uri": "memory://patch_blobs/git/1:abc/sha256",  # 用 evidence_uri
            "sha256": "sha256_value",
            "source_id": "1:abc",
            "source_type": "git",
            "excerpt": "摘要",
            "relevance_score": 0.9,
        }
        
        result = EvidenceResult.from_index_result(index_result)
        
        # 应该正确识别为 artifact_uri
        assert result.artifact_uri == "memory://patch_blobs/git/1:abc/sha256"
        assert result.evidence_uri == result.artifact_uri


# ============ Test: Collection 选择路径 ============


class TestCollectionSelection:
    """测试 collection 选择路径"""
    
    def test_mock_backend_has_canonical_id(self):
        """测试 MockIndexBackend 具有 canonical_id 属性"""
        backend = MockIndexBackend(collection_id="webapp:v1:bge-m3")
        
        assert backend.canonical_id == "webapp:v1:bge-m3"
        assert backend.collection_id == "webapp:v1:bge-m3"
    
    def test_mock_backend_default_collection_id(self):
        """测试 MockIndexBackend 默认 collection_id"""
        backend = MockIndexBackend()
        
        assert backend.canonical_id == "default:v1:mock-model"
    
    def test_mock_backend_set_collection_id(self):
        """测试 MockIndexBackend 设置 collection_id"""
        backend = MockIndexBackend()
        backend.set_collection_id("new-project:v2:text-embedding")
        
        assert backend.canonical_id == "new-project:v2:text-embedding"
    
    def test_query_with_specific_collection_backend(self, sample_docs):
        """测试使用指定 collection 的 backend 执行查询"""
        # 创建带有特定 collection_id 的 backend
        backend = MockIndexBackend(
            mock_data=sample_docs,
            collection_id="webapp:v1:bge-m3",
        )
        set_index_backend(backend)
        set_embedding_provider_instance(None)
        
        try:
            # 执行查询
            results = query_evidence("XSS", top_k=5)
            
            # 验证 backend 的 collection_id 正确
            assert backend.canonical_id == "webapp:v1:bge-m3"
            
            # 验证查询被记录
            assert len(backend.get_query_history()) == 1
            
            # 验证结果正常返回
            assert len(results) > 0
        finally:
            set_index_backend(None)
    
    def test_backend_collection_id_in_query_context(self, sample_docs):
        """测试查询时 backend 的 collection 上下文"""
        # 创建多个不同 collection 的 backend，验证正确的被使用
        backend_proj1 = MockIndexBackend(
            mock_data=sample_docs[:2],  # 只有前两条
            collection_id="proj1:v1:model-a",
        )
        backend_proj2 = MockIndexBackend(
            mock_data=sample_docs[2:],  # 只有后两条
            collection_id="proj2:v1:model-b",
        )
        
        try:
            # 使用 proj1 backend
            set_index_backend(backend_proj1)
            set_embedding_provider_instance(None)
            
            results1 = query_evidence("XSS", top_k=10)
            
            # proj1 只有前两条数据
            assert len(results1) == 2
            assert backend_proj1.canonical_id == "proj1:v1:model-a"
            
            # 切换到 proj2 backend
            set_index_backend(backend_proj2)
            
            results2 = query_evidence("API", top_k=10)
            
            # proj2 只有后两条数据
            assert len(results2) == 2
            assert backend_proj2.canonical_id == "proj2:v1:model-b"
            
        finally:
            set_index_backend(None)
    
    def test_run_query_uses_correct_backend(self, sample_docs):
        """测试 run_query 使用正确的 backend"""
        backend = MockIndexBackend(
            mock_data=sample_docs,
            collection_id="test-project:v2:bge-m3:20260128",
        )
        set_index_backend(backend)
        set_embedding_provider_instance(None)
        
        try:
            # 通过 run_query 执行
            result = run_query("数据库优化", top_k=5)
            
            # 验证 backend 被正确使用
            assert backend.canonical_id == "test-project:v2:bge-m3:20260128"
            assert len(backend.get_query_history()) == 1
            
            # 验证结果
            assert result.error is None
            assert len(result.evidences) > 0
            
        finally:
            set_index_backend(None)
    
    def test_query_evidence_with_explicit_backend(self, sample_docs):
        """测试 query_evidence 使用显式传入的 backend"""
        # 设置一个全局 backend
        global_backend = MockIndexBackend(
            mock_data=[],
            collection_id="global:v1:model",
        )
        set_index_backend(global_backend)
        set_embedding_provider_instance(None)
        
        # 创建一个显式 backend
        explicit_backend = MockIndexBackend(
            mock_data=sample_docs,
            collection_id="explicit:v1:model",
        )
        
        try:
            # 使用显式 backend 执行查询
            results = query_evidence(
                "XSS",
                top_k=5,
                backend=explicit_backend,
            )
            
            # 验证使用的是显式 backend（有数据）
            assert len(results) > 0
            
            # 验证查询记录在显式 backend
            assert len(explicit_backend.get_query_history()) == 1
            assert len(global_backend.get_query_history()) == 0
            
            # 验证 collection_id
            assert explicit_backend.canonical_id == "explicit:v1:model"
            
        finally:
            set_index_backend(None)


# ============ Test: active_collection 模块 ============


class TestMakeKvNamespace:
    """测试 make_kv_namespace 函数"""
    
    def test_namespace_no_args(self):
        """无参数时返回基础前缀"""
        ns = make_kv_namespace()
        assert ns == KV_NAMESPACE_PREFIX
        assert ns == "seekdb.sync"
    
    def test_namespace_with_backend_only(self):
        """仅指定 backend_name"""
        ns = make_kv_namespace("seekdb")
        assert ns == "seekdb.sync:seekdb"
        
        ns = make_kv_namespace("pgvector")
        assert ns == "seekdb.sync:pgvector"
    
    def test_namespace_with_collection_only(self):
        """仅指定 collection_id（不推荐但应支持）"""
        ns = make_kv_namespace(collection_id="proj1:v1:bge-m3")
        assert ns == "seekdb.sync:proj1:v1:bge-m3"
    
    def test_namespace_full(self):
        """backend + collection"""
        ns = make_kv_namespace("seekdb", "proj1:v1:bge-m3")
        assert ns == "seekdb.sync:seekdb:proj1:v1:bge-m3"
        
        ns = make_kv_namespace("pgvector", "webapp:v2:openai")
        assert ns == "seekdb.sync:pgvector:webapp:v2:openai"
    
    def test_namespace_with_version_tag(self):
        """collection 含 version_tag"""
        ns = make_kv_namespace("seekdb", "proj1:v1:bge-m3:20260128T100000")
        assert ns == "seekdb.sync:seekdb:proj1:v1:bge-m3:20260128T100000"


class TestMakeActiveCollectionKey:
    """测试 make_active_collection_key 函数"""
    
    def test_key_no_project(self):
        """无 project_key 时使用 default"""
        key = make_active_collection_key()
        assert key == "active_collection:default"
    
    def test_key_with_project(self):
        """指定 project_key"""
        key = make_active_collection_key("webapp")
        assert key == "active_collection:webapp"
        
        key = make_active_collection_key("api")
        assert key == "active_collection:api"
    
    def test_key_none_project(self):
        """None project_key 等同于无参数"""
        key = make_active_collection_key(None)
        assert key == "active_collection:default"


class TestGetDefaultCollectionId:
    """测试 get_default_collection_id 函数"""
    
    def test_default_no_args(self):
        """无参数时的默认值"""
        from step3_seekdb_rag_hybrid.step3_chunking import CHUNKING_VERSION
        collection_id = get_default_collection_id()
        # 默认 project_key="default", embedding_model_id="nomodel"
        assert collection_id == f"default:{CHUNKING_VERSION}:nomodel"
    
    def test_default_with_project(self):
        """指定 project_key"""
        collection_id = get_default_collection_id(project_key="webapp")
        assert collection_id.startswith("webapp:")
    
    def test_default_with_embedding(self):
        """指定 embedding_model_id"""
        collection_id = get_default_collection_id(embedding_model_id="bge-m3")
        assert "bge-m3" in collection_id
    
    def test_default_full(self):
        """完整参数，显式指定 chunking_version"""
        collection_id = get_default_collection_id(
            project_key="webapp",
            embedding_model_id="bge-m3",
            chunking_version="v2"
        )
        assert collection_id == "webapp:v2:bge-m3"


class TestResolveCollectionIdPriority:
    """测试 resolve_collection_id 优先级：explicit > active > default"""
    
    def test_explicit_highest_priority(self):
        """显式指定 collection 优先级最高"""
        # 即使提供了 conn 和其他参数，explicit 也应该优先
        result = resolve_collection_id(
            conn=None,
            backend_name="seekdb",
            project_key="webapp",
            embedding_model_id="bge-m3",
            explicit_collection_id="custom:v1:override",
        )
        assert result == "custom:v1:override"
    
    def test_explicit_overrides_active(self):
        """explicit 覆盖 active_collection"""
        # 创建 mock conn，模拟返回 active_collection
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = {
            "value_json": {"collection": "active:v1:bge-m3"}
        }
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cursor
        
        result = resolve_collection_id(
            conn=mock_conn,
            backend_name="seekdb",
            project_key="webapp",
            explicit_collection_id="explicit:v1:override",
        )
        # explicit 优先，不应查询 active_collection
        assert result == "explicit:v1:override"
    
    def test_active_second_priority(self):
        """active_collection 优先级次于 explicit"""
        # 创建 mock conn
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = {
            "value_json": {"collection": "active:v1:bge-m3"}
        }
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cursor
        
        result = resolve_collection_id(
            conn=mock_conn,
            backend_name="seekdb",
            project_key="webapp",
            embedding_model_id="bge-m3",
            # 不提供 explicit_collection_id
        )
        assert result == "active:v1:bge-m3"
    
    def test_default_lowest_priority(self):
        """default 优先级最低（无 explicit，无 active）"""
        from step3_seekdb_rag_hybrid.step3_chunking import CHUNKING_VERSION
        
        # 创建 mock conn，模拟 active_collection 不存在
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None  # 无 active_collection
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cursor
        
        result = resolve_collection_id(
            conn=mock_conn,
            backend_name="seekdb",
            project_key="webapp",
            embedding_model_id="bge-m3",
        )
        # 应该回退到默认命名
        assert result == f"webapp:{CHUNKING_VERSION}:bge-m3"
    
    def test_default_when_no_conn(self):
        """无 conn 时直接使用 default"""
        from step3_seekdb_rag_hybrid.step3_chunking import CHUNKING_VERSION
        
        result = resolve_collection_id(
            conn=None,
            backend_name="seekdb",
            project_key="api",
            embedding_model_id="openai",
        )
        assert result == f"api:{CHUNKING_VERSION}:openai"
    
    def test_default_when_no_backend(self):
        """无 backend_name 时不尝试读取 active"""
        from step3_seekdb_rag_hybrid.step3_chunking import CHUNKING_VERSION
        
        mock_conn = MagicMock()
        
        result = resolve_collection_id(
            conn=mock_conn,
            backend_name=None,  # 无 backend
            project_key="webapp",
            embedding_model_id="bge-m3",
        )
        # 应该直接使用 default，不尝试查询
        assert result == f"webapp:{CHUNKING_VERSION}:bge-m3"
        # 验证没有调用 cursor
        mock_conn.cursor.assert_not_called()


class TestActiveCollectionReadWrite:
    """测试 get_active_collection 和 set_active_collection"""
    
    def test_get_active_collection_exists(self):
        """读取存在的 active_collection"""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = {
            "value_json": {"collection": "webapp:v1:bge-m3", "activated_at": "2026-01-28T10:00:00Z"}
        }
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cursor
        
        result = get_active_collection(mock_conn, "seekdb", "webapp")
        assert result == "webapp:v1:bge-m3"
    
    def test_get_active_collection_not_exists(self):
        """读取不存在的 active_collection"""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cursor
        
        result = get_active_collection(mock_conn, "seekdb", "webapp")
        assert result is None
    
    def test_get_active_collection_empty_value(self):
        """读取空值的 active_collection"""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = {"value_json": None}
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cursor
        
        result = get_active_collection(mock_conn, "seekdb", "webapp")
        assert result is None
    
    def test_set_active_collection_calls_execute(self):
        """设置 active_collection 调用 execute"""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cursor
        
        set_active_collection(mock_conn, "seekdb", "webapp:v1:bge-m3", "webapp")
        
        # 验证 execute 被调用
        mock_cursor.execute.assert_called_once()
        
        # 验证参数
        call_args = mock_cursor.execute.call_args
        params = call_args[0][1]  # 第二个位置参数是 params dict
        assert params["namespace"] == "seekdb.sync:seekdb"
        assert params["key"] == "active_collection:webapp"
        
        # 验证 value_json 内容
        value_json = json.loads(params["value_json"])
        assert value_json["collection"] == "webapp:v1:bge-m3"
        assert "activated_at" in value_json


# ============ Test: DualRead compare 功能测试 ============


class TestDualReadCompareDisabled:
    """测试 compare 关闭时的行为"""
    
    def test_compare_disabled_no_shadow_query(self, mock_backend, sample_docs):
        """compare 关闭时不执行 shadow 查询"""
        mock_backend.set_mock_data(sample_docs)
        
        # 创建 shadow backend
        shadow_backend = MockIndexBackend(
            mock_data=sample_docs,
            collection_id="shadow:v1:model",
        )
        
        # 配置 compare 关闭
        config = DualReadConfig(enabled=False)
        set_dual_read_config(config)
        set_shadow_backend(shadow_backend)
        
        try:
            # 执行查询
            results = query_evidence_dual_read(
                query_text="XSS 漏洞",
                top_k=5,
                primary_backend=mock_backend,
                shadow_backend=shadow_backend,
                dual_read_config=config,
            )
            
            # 验证返回结果正常
            assert len(results) > 0
            
            # 验证 primary 被调用
            assert len(mock_backend.get_query_history()) == 1
            
            # 验证 shadow 未被调用（compare 关闭）
            assert len(shadow_backend.get_query_history()) == 0
            
        finally:
            set_dual_read_config(None)
            set_shadow_backend(None)
    
    def test_compare_disabled_output_no_compare_info(self, mock_backend, sample_docs):
        """compare 关闭时输出不含 compare 相关信息"""
        mock_backend.set_mock_data(sample_docs)
        
        # 配置 compare 关闭
        config = DualReadConfig(enabled=False)
        set_dual_read_config(config)
        
        try:
            # 执行查询
            result = run_query(
                query_text="XSS 漏洞",
                top_k=5,
            )
            
            # 获取输出格式
            packet = result.to_evidence_packet()
            full_output = result.to_dict()
            
            # 验证正常输出结构
            assert "query" in packet
            assert "evidences" in packet
            assert "result_count" in packet
            
            assert "success" in full_output
            assert "evidences" in full_output
            
            # 验证不包含 compare/dual_read 相关字段
            assert "compare" not in packet
            assert "shadow" not in packet
            assert "dual_read" not in packet
            
            assert "compare" not in full_output
            assert "shadow" not in full_output
            assert "dual_read" not in full_output
            
        finally:
            set_dual_read_config(None)
    
    def test_compare_disabled_no_config(self, mock_backend, sample_docs):
        """无配置时（None）等同于 compare 关闭"""
        mock_backend.set_mock_data(sample_docs)
        
        # 创建 shadow backend
        shadow_backend = MockIndexBackend(
            mock_data=sample_docs,
            collection_id="shadow:v1:model",
        )
        
        # 不设置任何配置
        set_dual_read_config(None)
        set_shadow_backend(shadow_backend)
        
        try:
            # 执行查询
            results = query_evidence_dual_read(
                query_text="数据库优化",
                top_k=5,
                primary_backend=mock_backend,
                shadow_backend=shadow_backend,
                dual_read_config=None,  # 显式传 None
            )
            
            # 验证返回结果正常
            assert len(results) > 0
            
            # 验证 primary 被调用
            assert len(mock_backend.get_query_history()) == 1
            
            # 验证 shadow 未被调用
            assert len(shadow_backend.get_query_history()) == 0
            
        finally:
            set_dual_read_config(None)
            set_shadow_backend(None)


class TestDualReadCompareEnabled:
    """测试 compare 开启时的行为"""
    
    def test_compare_enabled_both_backends_queried(self, mock_backend, sample_docs):
        """compare 开启时同时查询 primary 和 shadow"""
        mock_backend.set_mock_data(sample_docs)
        
        # 创建 shadow backend，返回相同数据
        shadow_backend = MockIndexBackend(
            mock_data=sample_docs,
            collection_id="shadow:v1:model",
        )
        
        # 配置 compare 开启
        config = DualReadConfig(
            enabled=True,
            strategy=DualReadConfig.STRATEGY_COMPARE,
            log_diff=True,
        )
        set_dual_read_config(config)
        set_shadow_backend(shadow_backend)
        
        try:
            # 执行查询
            results = query_evidence_dual_read(
                query_text="XSS 漏洞",
                top_k=5,
                primary_backend=mock_backend,
                shadow_backend=shadow_backend,
                dual_read_config=config,
            )
            
            # 验证返回结果正常
            assert len(results) > 0
            
            # 验证 primary 被调用
            assert len(mock_backend.get_query_history()) == 1
            
            # 验证 shadow 也被调用（compare 开启）
            assert len(shadow_backend.get_query_history()) == 1
            
        finally:
            set_dual_read_config(None)
            set_shadow_backend(None)
    
    def test_compare_enabled_returns_primary_results(self, mock_backend, sample_docs):
        """compare 开启时返回 primary 结果"""
        # primary 有特定数据
        primary_data = sample_docs[:2]
        mock_backend.set_mock_data(primary_data)
        
        # shadow 有不同数据
        shadow_data = sample_docs[2:]
        shadow_backend = MockIndexBackend(
            mock_data=shadow_data,
            collection_id="shadow:v1:model",
        )
        
        # 配置 compare 开启
        config = DualReadConfig(
            enabled=True,
            strategy=DualReadConfig.STRATEGY_COMPARE,
        )
        
        try:
            # 执行查询
            results = query_evidence_dual_read(
                query_text="XSS 漏洞",
                top_k=5,
                primary_backend=mock_backend,
                shadow_backend=shadow_backend,
                dual_read_config=config,
            )
            
            # 验证返回的是 primary 结果
            assert len(results) == len(primary_data)
            
            # 验证 chunk_id 来自 primary 数据
            result_chunk_ids = {r.chunk_id for r in results}
            primary_chunk_ids = {d["chunk_id"] for d in primary_data}
            assert result_chunk_ids == primary_chunk_ids
            
        finally:
            set_dual_read_config(None)
    
    def test_compare_enabled_existing_assertions_not_affected(self, mock_backend, sample_docs):
        """compare 开启时不影响现有断言（packet/full 格式）"""
        mock_backend.set_mock_data(sample_docs)
        
        # 创建 shadow backend
        shadow_backend = MockIndexBackend(
            mock_data=sample_docs,
            collection_id="shadow:v1:model",
        )
        
        # 配置 compare 开启
        config = DualReadConfig(
            enabled=True,
            strategy=DualReadConfig.STRATEGY_COMPARE,
            log_diff=True,
        )
        set_dual_read_config(config)
        set_shadow_backend(shadow_backend)
        
        try:
            # 执行查询
            result = run_query(
                query_text="XSS 漏洞",
                top_k=5,
                shadow_backend=shadow_backend,
                dual_read_config=config,
            )
            
            # ========== 验证 packet 格式（现有断言不受影响）==========
            packet = result.to_evidence_packet()
            
            # 验证 packet 基本结构
            assert "query" in packet
            assert "evidences" in packet
            assert "chunking_version" in packet
            assert "generated_at" in packet
            assert "result_count" in packet
            
            # 验证 evidence 条目不包含 content
            for ev in packet["evidences"]:
                assert "content" not in ev
                assert "excerpt" in ev
                assert "artifact_uri" in ev
                assert "evidence_uri" in ev
            
            # ========== 验证 full 格式（现有断言不受影响）==========
            full_output = result.to_dict()
            
            assert "success" in full_output
            assert "query" in full_output
            assert "filters" in full_output
            assert "top_k" in full_output
            assert "result_count" in full_output
            assert "evidences" in full_output
            assert "timing" in full_output
            
            # 验证 evidence 条目包含 content
            for ev in full_output["evidences"]:
                assert "content" in ev
                assert "artifact_uri" in ev
                assert "evidence_uri" in ev
                assert "metadata" in ev
            
        finally:
            set_dual_read_config(None)
            set_shadow_backend(None)
    
    def test_compare_results_function_match(self):
        """测试 _compare_results 函数：结果匹配"""
        # 创建相同的结果
        primary = [
            EvidenceResult(
                chunk_id="test:1", chunk_idx=0, content="c1",
                artifact_uri="uri1", sha256="sha1", source_id="s1",
                source_type="git", excerpt="e1", relevance_score=0.9,
            ),
            EvidenceResult(
                chunk_id="test:2", chunk_idx=0, content="c2",
                artifact_uri="uri2", sha256="sha2", source_id="s2",
                source_type="git", excerpt="e2", relevance_score=0.8,
            ),
        ]
        shadow = [
            EvidenceResult(
                chunk_id="test:1", chunk_idx=0, content="c1",
                artifact_uri="uri1", sha256="sha1", source_id="s1",
                source_type="git", excerpt="e1", relevance_score=0.9,
            ),
            EvidenceResult(
                chunk_id="test:2", chunk_idx=0, content="c2",
                artifact_uri="uri2", sha256="sha2", source_id="s2",
                source_type="git", excerpt="e2", relevance_score=0.8,
            ),
        ]
        
        diff = _compare_results(primary, shadow)
        
        assert diff["match"] is True
        assert diff["primary_count"] == 2
        assert diff["shadow_count"] == 2
        assert diff["common_count"] == 2
        assert len(diff["only_primary"]) == 0
        assert len(diff["only_shadow"]) == 0
        assert len(diff["score_diffs"]) == 0
    
    def test_compare_results_function_diff(self):
        """测试 _compare_results 函数：结果不匹配"""
        # 创建不同的结果
        primary = [
            EvidenceResult(
                chunk_id="test:1", chunk_idx=0, content="c1",
                artifact_uri="uri1", sha256="sha1", source_id="s1",
                source_type="git", excerpt="e1", relevance_score=0.9,
            ),
            EvidenceResult(
                chunk_id="test:2", chunk_idx=0, content="c2",
                artifact_uri="uri2", sha256="sha2", source_id="s2",
                source_type="git", excerpt="e2", relevance_score=0.8,
            ),
        ]
        shadow = [
            EvidenceResult(
                chunk_id="test:1", chunk_idx=0, content="c1",
                artifact_uri="uri1", sha256="sha1", source_id="s1",
                source_type="git", excerpt="e1", relevance_score=0.7,  # 分数不同
            ),
            EvidenceResult(
                chunk_id="test:3", chunk_idx=0, content="c3",  # 不同的 chunk_id
                artifact_uri="uri3", sha256="sha3", source_id="s3",
                source_type="svn", excerpt="e3", relevance_score=0.6,
            ),
        ]
        
        diff = _compare_results(primary, shadow, diff_threshold=0.1)
        
        assert diff["match"] is False
        assert diff["primary_count"] == 2
        assert diff["shadow_count"] == 2
        assert diff["common_count"] == 1
        assert "test:2" in diff["only_primary"]
        assert "test:3" in diff["only_shadow"]
        # test:1 分数差异 0.2 > 0.1
        assert len(diff["score_diffs"]) == 1
        assert diff["score_diffs"][0]["chunk_id"] == "test:1"
    
    def test_compare_enabled_with_filters(self, mock_backend, sample_docs):
        """compare 开启时过滤条件正常应用到两个后端"""
        mock_backend.set_mock_data(sample_docs)
        
        # 创建 shadow backend
        shadow_backend = MockIndexBackend(
            mock_data=sample_docs,
            collection_id="shadow:v1:model",
        )
        
        # 配置 compare 开启
        config = DualReadConfig(
            enabled=True,
            strategy=DualReadConfig.STRATEGY_COMPARE,
        )
        
        # 使用过滤条件
        filters = QueryFilters(project_key="webapp", source_type="git")
        
        try:
            results = query_evidence_dual_read(
                query_text="XSS",
                filters=filters,
                top_k=5,
                primary_backend=mock_backend,
                shadow_backend=shadow_backend,
                dual_read_config=config,
            )
            
            # 验证 primary 查询包含正确的过滤条件
            primary_req = mock_backend.get_query_history()[-1]
            assert primary_req.filters["project_key"] == "webapp"
            assert primary_req.filters["source_type"] == "git"
            
            # 验证 shadow 查询也包含相同的过滤条件
            shadow_req = shadow_backend.get_query_history()[-1]
            assert shadow_req.filters["project_key"] == "webapp"
            assert shadow_req.filters["source_type"] == "git"
            
            # 验证结果符合过滤条件
            for r in results:
                assert r.source_type == "git"
                
        finally:
            set_dual_read_config(None)


# ============ Test: 双读门禁逻辑 ============


class TestDualReadGateThresholds:
    """测试 DualReadGateThresholds 数据结构"""
    
    def test_has_thresholds_empty(self):
        """无阈值时返回 False"""
        thresholds = DualReadGateThresholds()
        assert thresholds.has_thresholds() is False
    
    def test_has_thresholds_with_min_overlap(self):
        """配置 min_overlap 时返回 True"""
        thresholds = DualReadGateThresholds(min_overlap=0.8)
        assert thresholds.has_thresholds() is True
    
    def test_has_thresholds_with_max_only_primary(self):
        """配置 max_only_primary 时返回 True"""
        thresholds = DualReadGateThresholds(max_only_primary=2)
        assert thresholds.has_thresholds() is True
    
    def test_has_thresholds_with_max_only_shadow(self):
        """配置 max_only_shadow 时返回 True"""
        thresholds = DualReadGateThresholds(max_only_shadow=3)
        assert thresholds.has_thresholds() is True
    
    def test_has_thresholds_with_max_score_drift(self):
        """配置 max_score_drift 时返回 True"""
        thresholds = DualReadGateThresholds(max_score_drift=0.1)
        assert thresholds.has_thresholds() is True
    
    def test_has_thresholds_combined(self):
        """配置多个阈值时返回 True"""
        thresholds = DualReadGateThresholds(
            min_overlap=0.7,
            max_only_primary=1,
            max_score_drift=0.05,
        )
        assert thresholds.has_thresholds() is True


class TestCheckDualReadGate:
    """测试 check_dual_read_gate 函数"""
    
    @pytest.fixture
    def sample_stats(self):
        """创建样本 DualReadStats"""
        return DualReadStats(
            overlap_ratio=0.75,
            primary_count=10,
            shadow_count=10,
            common_count=8,
            only_primary=["chunk_1", "chunk_2"],
            only_shadow=["chunk_3"],
            score_diff_mean=0.03,
            score_diff_max=0.08,
        )
    
    def test_no_thresholds_pass(self, sample_stats):
        """无阈值配置时直接通过"""
        thresholds = DualReadGateThresholds()
        result = check_dual_read_gate(sample_stats, thresholds)
        
        assert result.passed is True
        assert len(result.violations) == 0
    
    def test_min_overlap_pass(self, sample_stats):
        """overlap_ratio >= 阈值时通过"""
        thresholds = DualReadGateThresholds(min_overlap=0.7)
        result = check_dual_read_gate(sample_stats, thresholds)
        
        assert result.passed is True
        assert len(result.violations) == 0
    
    def test_min_overlap_fail(self, sample_stats):
        """overlap_ratio < 阈值时失败"""
        thresholds = DualReadGateThresholds(min_overlap=0.9)
        result = check_dual_read_gate(sample_stats, thresholds)
        
        assert result.passed is False
        assert len(result.violations) == 1
        assert result.violations[0].check_name == "min_overlap"
        assert result.violations[0].threshold_value == 0.9
        assert result.violations[0].actual_value == 0.75
    
    def test_max_only_primary_pass(self, sample_stats):
        """only_primary 数量 <= 阈值时通过"""
        thresholds = DualReadGateThresholds(max_only_primary=5)
        result = check_dual_read_gate(sample_stats, thresholds)
        
        assert result.passed is True
        assert len(result.violations) == 0
    
    def test_max_only_primary_fail(self, sample_stats):
        """only_primary 数量 > 阈值时失败"""
        thresholds = DualReadGateThresholds(max_only_primary=1)
        result = check_dual_read_gate(sample_stats, thresholds)
        
        assert result.passed is False
        assert len(result.violations) == 1
        assert result.violations[0].check_name == "max_only_primary"
        assert result.violations[0].threshold_value == 1.0
        assert result.violations[0].actual_value == 2.0
    
    def test_max_only_shadow_pass(self, sample_stats):
        """only_shadow 数量 <= 阈值时通过"""
        thresholds = DualReadGateThresholds(max_only_shadow=3)
        result = check_dual_read_gate(sample_stats, thresholds)
        
        assert result.passed is True
        assert len(result.violations) == 0
    
    def test_max_only_shadow_fail(self, sample_stats):
        """only_shadow 数量 > 阈值时失败"""
        thresholds = DualReadGateThresholds(max_only_shadow=0)
        result = check_dual_read_gate(sample_stats, thresholds)
        
        assert result.passed is False
        assert len(result.violations) == 1
        assert result.violations[0].check_name == "max_only_shadow"
        assert result.violations[0].threshold_value == 0.0
        assert result.violations[0].actual_value == 1.0
    
    def test_max_score_drift_pass(self, sample_stats):
        """score_diff_max <= 阈值时通过"""
        thresholds = DualReadGateThresholds(max_score_drift=0.1)
        result = check_dual_read_gate(sample_stats, thresholds)
        
        assert result.passed is True
        assert len(result.violations) == 0
    
    def test_max_score_drift_fail(self, sample_stats):
        """score_diff_max > 阈值时失败"""
        thresholds = DualReadGateThresholds(max_score_drift=0.05)
        result = check_dual_read_gate(sample_stats, thresholds)
        
        assert result.passed is False
        assert len(result.violations) == 1
        assert result.violations[0].check_name == "max_score_drift"
        assert result.violations[0].threshold_value == 0.05
        assert result.violations[0].actual_value == 0.08
    
    def test_multiple_thresholds_all_pass(self, sample_stats):
        """多个阈值都满足时通过"""
        thresholds = DualReadGateThresholds(
            min_overlap=0.7,
            max_only_primary=5,
            max_only_shadow=5,
            max_score_drift=0.1,
        )
        result = check_dual_read_gate(sample_stats, thresholds)
        
        assert result.passed is True
        assert len(result.violations) == 0
    
    def test_multiple_thresholds_some_fail(self, sample_stats):
        """部分阈值不满足时失败"""
        thresholds = DualReadGateThresholds(
            min_overlap=0.9,     # 失败
            max_only_primary=1,  # 失败
            max_only_shadow=5,   # 通过
            max_score_drift=0.1, # 通过
        )
        result = check_dual_read_gate(sample_stats, thresholds)
        
        assert result.passed is False
        assert len(result.violations) == 2
        violation_names = [v.check_name for v in result.violations]
        assert "min_overlap" in violation_names
        assert "max_only_primary" in violation_names
    
    def test_thresholds_applied_in_result(self, sample_stats):
        """验证阈值配置包含在结果中"""
        thresholds = DualReadGateThresholds(
            min_overlap=0.8,
            max_score_drift=0.05,
        )
        result = check_dual_read_gate(sample_stats, thresholds)
        
        assert result.thresholds_applied is not None
        assert result.thresholds_applied.min_overlap == 0.8
        assert result.thresholds_applied.max_score_drift == 0.05


class TestDualReadGateResultToDict:
    """测试 DualReadGateResult.to_dict 方法"""
    
    def test_passed_to_dict(self):
        """通过时的 to_dict"""
        result = DualReadGateResult(
            passed=True,
            thresholds_applied=DualReadGateThresholds(min_overlap=0.7),
        )
        output = result.to_dict()
        
        assert output["passed"] is True
        assert "thresholds" in output
        assert output["thresholds"]["min_overlap"] == 0.7
        assert "violations" not in output
    
    def test_failed_to_dict(self):
        """失败时的 to_dict"""
        result = DualReadGateResult(
            passed=False,
            violations=[
                DualReadGateViolation(
                    check_name="min_overlap",
                    threshold_value=0.9,
                    actual_value=0.75,
                    message="重叠率 0.7500 低于阈值 0.9",
                ),
            ],
            thresholds_applied=DualReadGateThresholds(min_overlap=0.9),
        )
        output = result.to_dict()
        
        assert output["passed"] is False
        assert "thresholds" in output
        assert output["thresholds"]["min_overlap"] == 0.9
        assert "violations" in output
        assert len(output["violations"]) == 1
        assert output["violations"][0]["check"] == "min_overlap"
        assert output["violations"][0]["threshold"] == 0.9
        assert output["violations"][0]["actual"] == 0.75
    
    def test_to_dict_only_set_thresholds(self):
        """to_dict 只输出设置的阈值"""
        result = DualReadGateResult(
            passed=True,
            thresholds_applied=DualReadGateThresholds(
                min_overlap=0.7,
                max_only_primary=None,  # 未设置
                max_score_drift=0.1,
            ),
        )
        output = result.to_dict()
        
        assert "min_overlap" in output["thresholds"]
        assert "max_score_drift" in output["thresholds"]
        assert "max_only_primary" not in output["thresholds"]
        assert "max_only_shadow" not in output["thresholds"]


class TestDualReadStatsWithGate:
    """测试 DualReadStats 的 gate 字段集成"""
    
    def test_stats_to_dict_without_gate(self):
        """无门禁时 to_dict 不包含 gate"""
        stats = DualReadStats(
            overlap_ratio=0.8,
            primary_count=10,
            shadow_count=10,
        )
        output = stats.to_dict()
        
        assert "gate" not in output
    
    def test_stats_to_dict_with_gate_passed(self):
        """门禁通过时 to_dict 包含 gate"""
        gate_result = DualReadGateResult(
            passed=True,
            thresholds_applied=DualReadGateThresholds(min_overlap=0.7),
        )
        stats = DualReadStats(
            overlap_ratio=0.8,
            primary_count=10,
            shadow_count=10,
            gate=gate_result,
        )
        output = stats.to_dict()
        
        assert "gate" in output
        assert output["gate"]["passed"] is True
    
    def test_stats_to_dict_with_gate_failed(self):
        """门禁失败时 to_dict 包含 gate 和 violations"""
        gate_result = DualReadGateResult(
            passed=False,
            violations=[
                DualReadGateViolation(
                    check_name="min_overlap",
                    threshold_value=0.9,
                    actual_value=0.75,
                    message="重叠率低于阈值",
                ),
            ],
            thresholds_applied=DualReadGateThresholds(min_overlap=0.9),
        )
        stats = DualReadStats(
            overlap_ratio=0.75,
            primary_count=10,
            shadow_count=10,
            gate=gate_result,
        )
        output = stats.to_dict()
        
        assert "gate" in output
        assert output["gate"]["passed"] is False
        assert len(output["gate"]["violations"]) == 1


class TestDualReadGateIntegration:
    """测试门禁逻辑与 run_query 的集成（使用 Mock Backend）"""
    
    def test_run_query_with_gate_thresholds_pass(self, mock_backend, sample_docs):
        """门禁通过时 dual_read 输出包含 gate.passed=True"""
        mock_backend.set_mock_data(sample_docs)
        
        # 创建 shadow backend，返回相同数据
        shadow_backend = MockIndexBackend(
            mock_data=sample_docs,
            collection_id="shadow:v1:model",
        )
        set_shadow_backend(shadow_backend)
        
        # 配置双读
        config = DualReadConfig(enabled=True, strategy=DualReadConfig.STRATEGY_COMPARE)
        set_dual_read_config(config)
        
        # 配置宽松的阈值（应该通过）
        gate_thresholds = DualReadGateThresholds(
            min_overlap=0.5,
            max_only_primary=10,
            max_only_shadow=10,
            max_score_drift=0.5,
        )
        
        try:
            result = run_query(
                query_text="XSS 漏洞",
                top_k=5,
                backend=mock_backend,
                shadow_backend=shadow_backend,
                enable_dual_read=True,
                dual_read_gate_thresholds=gate_thresholds,
            )
            
            # 验证结果
            assert result.dual_read_stats is not None
            assert result.dual_read_stats.gate is not None
            assert result.dual_read_stats.gate.passed is True
            
            # 验证 to_dict 输出
            full_output = result.to_dict(include_dual_read=True)
            assert "dual_read" in full_output
            assert "gate" in full_output["dual_read"]
            assert full_output["dual_read"]["gate"]["passed"] is True
            
        finally:
            set_dual_read_config(None)
            set_shadow_backend(None)
    
    def test_run_query_with_gate_thresholds_fail(self, mock_backend, sample_docs):
        """门禁失败时 dual_read 输出包含 gate.passed=False"""
        # primary 有特定数据
        primary_data = sample_docs[:2]
        mock_backend.set_mock_data(primary_data)
        
        # shadow 有不同数据（造成差异）
        shadow_data = sample_docs[2:]
        shadow_backend = MockIndexBackend(
            mock_data=shadow_data,
            collection_id="shadow:v1:model",
        )
        set_shadow_backend(shadow_backend)
        
        # 配置双读
        config = DualReadConfig(enabled=True, strategy=DualReadConfig.STRATEGY_COMPARE)
        set_dual_read_config(config)
        
        # 配置严格的阈值（应该失败）
        gate_thresholds = DualReadGateThresholds(
            min_overlap=0.99,  # 非常高的重叠率要求
        )
        
        try:
            result = run_query(
                query_text="XSS 漏洞",
                top_k=5,
                backend=mock_backend,
                shadow_backend=shadow_backend,
                enable_dual_read=True,
                dual_read_gate_thresholds=gate_thresholds,
            )
            
            # 验证结果
            assert result.dual_read_stats is not None
            assert result.dual_read_stats.gate is not None
            assert result.dual_read_stats.gate.passed is False
            
            # 验证有违规记录
            violations = result.dual_read_stats.gate.violations
            assert len(violations) >= 1
            violation_names = [v.check_name for v in violations]
            assert "min_overlap" in violation_names
            
            # 验证 to_dict 输出
            full_output = result.to_dict(include_dual_read=True)
            assert full_output["dual_read"]["gate"]["passed"] is False
            assert "violations" in full_output["dual_read"]["gate"]
            
        finally:
            set_dual_read_config(None)
            set_shadow_backend(None)
    
    def test_run_query_without_gate_thresholds(self, mock_backend, sample_docs):
        """不配置门禁阈值时，不执行门禁检查"""
        mock_backend.set_mock_data(sample_docs)
        
        shadow_backend = MockIndexBackend(
            mock_data=sample_docs,
            collection_id="shadow:v1:model",
        )
        set_shadow_backend(shadow_backend)
        
        config = DualReadConfig(enabled=True, strategy=DualReadConfig.STRATEGY_COMPARE)
        set_dual_read_config(config)
        
        try:
            result = run_query(
                query_text="XSS 漏洞",
                top_k=5,
                backend=mock_backend,
                shadow_backend=shadow_backend,
                enable_dual_read=True,
                dual_read_gate_thresholds=None,  # 不配置阈值
            )
            
            # 验证结果
            assert result.dual_read_stats is not None
            assert result.dual_read_stats.gate is None  # 无门禁检查
            
            # 验证 to_dict 输出不包含 gate
            full_output = result.to_dict(include_dual_read=True)
            assert "dual_read" in full_output
            assert "gate" not in full_output["dual_read"]
            
        finally:
            set_dual_read_config(None)
            set_shadow_backend(None)
    
    def test_gate_with_constructed_query_hits(self):
        """使用构造的 QueryHit 列表测试门禁逻辑（不依赖后端）"""
        # 构造 primary 结果
        primary_results = [
            EvidenceResult(
                chunk_id="chunk_a", chunk_idx=0, content="content_a",
                artifact_uri="uri_a", sha256="sha_a", source_id="s_a",
                source_type="git", excerpt="excerpt_a", relevance_score=0.95,
            ),
            EvidenceResult(
                chunk_id="chunk_b", chunk_idx=0, content="content_b",
                artifact_uri="uri_b", sha256="sha_b", source_id="s_b",
                source_type="git", excerpt="excerpt_b", relevance_score=0.85,
            ),
            EvidenceResult(
                chunk_id="chunk_c", chunk_idx=0, content="content_c",
                artifact_uri="uri_c", sha256="sha_c", source_id="s_c",
                source_type="git", excerpt="excerpt_c", relevance_score=0.75,
            ),
        ]
        
        # 构造 shadow 结果（与 primary 有部分差异）
        shadow_results = [
            EvidenceResult(
                chunk_id="chunk_a", chunk_idx=0, content="content_a",
                artifact_uri="uri_a", sha256="sha_a", source_id="s_a",
                source_type="git", excerpt="excerpt_a", relevance_score=0.93,  # 略有差异
            ),
            EvidenceResult(
                chunk_id="chunk_d", chunk_idx=0, content="content_d",  # 不同 chunk
                artifact_uri="uri_d", sha256="sha_d", source_id="s_d",
                source_type="git", excerpt="excerpt_d", relevance_score=0.88,
            ),
        ]
        
        # 计算双读统计
        stats = compute_dual_read_stats(
            primary_results=primary_results,
            shadow_results=shadow_results,
        )
        
        # 验证统计数据
        assert stats.primary_count == 3
        assert stats.shadow_count == 2
        assert stats.common_count == 1  # 只有 chunk_a 共同
        assert len(stats.only_primary) == 2  # chunk_b, chunk_c
        assert len(stats.only_shadow) == 1  # chunk_d
        
        # 测试门禁检查 - 场景1：宽松阈值，应通过
        thresholds_loose = DualReadGateThresholds(
            min_overlap=0.1,
            max_only_primary=5,
            max_only_shadow=5,
        )
        gate_result = check_dual_read_gate(stats, thresholds_loose)
        assert gate_result.passed is True
        
        # 测试门禁检查 - 场景2：严格阈值，应失败
        thresholds_strict = DualReadGateThresholds(
            min_overlap=0.8,  # overlap_ratio 实际约 0.25，会失败
            max_only_primary=1,  # 实际 2，会失败
        )
        gate_result = check_dual_read_gate(stats, thresholds_strict)
        assert gate_result.passed is False
        assert len(gate_result.violations) == 2


# ============ Test: compare_report/dual_read 字段可选性与默认行为 ============


class TestCompareReportDualReadOptional:
    """测试 packet/full 输出中 compare_report/dual_read 字段的可选性与默认行为"""
    
    def test_packet_no_compare_report_by_default(self, mock_backend, sample_docs):
        """packet 默认不包含 compare_report（无双读时）"""
        mock_backend.set_mock_data(sample_docs)
        
        result = run_query("XSS 漏洞", top_k=5)
        packet = result.to_evidence_packet()
        
        # 无双读配置时，默认不应有 compare_report
        assert "compare_report" not in packet
    
    def test_packet_no_dual_read_by_default(self, mock_backend, sample_docs):
        """packet 默认不包含 dual_read（无双读时）"""
        mock_backend.set_mock_data(sample_docs)
        
        result = run_query("数据库优化", top_k=5)
        packet = result.to_evidence_packet()
        
        # 无双读配置时，默认不应有 dual_read
        assert "dual_read" not in packet
    
    def test_full_no_compare_report_by_default(self, mock_backend, sample_docs):
        """full 默认不包含 compare_report（无双读时）"""
        mock_backend.set_mock_data(sample_docs)
        
        result = run_query("API 接口", top_k=5)
        full_output = result.to_dict()
        
        # 无双读配置时，默认不应有 compare_report
        assert "compare_report" not in full_output
    
    def test_full_no_dual_read_by_default(self, mock_backend, sample_docs):
        """full 默认不包含 dual_read（无双读时）"""
        mock_backend.set_mock_data(sample_docs)
        
        result = run_query("版本记录", top_k=5)
        full_output = result.to_dict()
        
        # 无双读配置时，默认不应有 dual_read
        assert "dual_read" not in full_output
    
    def test_packet_include_compare_false(self, mock_backend, sample_docs):
        """packet include_compare=False 时不输出 compare_report"""
        mock_backend.set_mock_data(sample_docs)
        
        # 创建 shadow 并启用双读
        shadow_backend = MockIndexBackend(
            mock_data=sample_docs,
            collection_id="shadow:v1:model",
        )
        config = DualReadConfig(enabled=True, strategy=DualReadConfig.STRATEGY_COMPARE)
        set_dual_read_config(config)
        set_shadow_backend(shadow_backend)
        
        try:
            result = run_query(
                query_text="XSS",
                top_k=5,
                shadow_backend=shadow_backend,
                dual_read_config=config,
            )
            
            # 显式设置 include_compare=False
            packet = result.to_evidence_packet(include_compare=False)
            assert "compare_report" not in packet
            
            # 但 dual_read 仍可能存在（如果有 stats）
            # 显式设置 include_dual_read=False 时也不输出
            packet_no_dual = result.to_evidence_packet(include_compare=False, include_dual_read=False)
            assert "compare_report" not in packet_no_dual
            assert "dual_read" not in packet_no_dual
        finally:
            set_dual_read_config(None)
            set_shadow_backend(None)
    
    def test_full_include_compare_false(self, mock_backend, sample_docs):
        """full include_compare=False 时不输出 compare_report"""
        mock_backend.set_mock_data(sample_docs)
        
        shadow_backend = MockIndexBackend(
            mock_data=sample_docs,
            collection_id="shadow:v1:model",
        )
        config = DualReadConfig(enabled=True, strategy=DualReadConfig.STRATEGY_COMPARE)
        set_dual_read_config(config)
        set_shadow_backend(shadow_backend)
        
        try:
            result = run_query(
                query_text="优化",
                top_k=5,
                shadow_backend=shadow_backend,
                dual_read_config=config,
            )
            
            # 显式设置 include_compare=False
            full_output = result.to_dict(include_compare=False)
            assert "compare_report" not in full_output
            
            # 显式设置 include_dual_read=False
            full_no_dual = result.to_dict(include_compare=False, include_dual_read=False)
            assert "compare_report" not in full_no_dual
            assert "dual_read" not in full_no_dual
        finally:
            set_dual_read_config(None)
            set_shadow_backend(None)
    
    def test_dual_read_stats_present_when_enabled(self, mock_backend, sample_docs):
        """启用双读时 dual_read_stats 存在且可输出"""
        mock_backend.set_mock_data(sample_docs)
        
        shadow_backend = MockIndexBackend(
            mock_data=sample_docs,
            collection_id="shadow:v1:model",
        )
        config = DualReadConfig(enabled=True, strategy=DualReadConfig.STRATEGY_COMPARE)
        set_dual_read_config(config)
        set_shadow_backend(shadow_backend)
        
        try:
            result = run_query(
                query_text="漏洞",
                top_k=5,
                shadow_backend=shadow_backend,
                enable_dual_read=True,
            )
            
            # 验证 dual_read_stats 被填充
            assert result.dual_read_stats is not None
            
            # 验证 to_dict 包含 dual_read
            full_output = result.to_dict(include_dual_read=True)
            assert "dual_read" in full_output
            assert "health" in full_output["dual_read"]
            
            # 验证 to_evidence_packet 包含 dual_read
            packet = result.to_evidence_packet(include_dual_read=True)
            assert "dual_read" in packet
        finally:
            set_dual_read_config(None)
            set_shadow_backend(None)


# ============ Test: summary/detailed 模式下 thresholds/metadata 存在性 ============


class TestCompareModeThresholdsMetadata:
    """测试 summary/detailed 模式下 thresholds/metadata 的存在性"""
    
    def test_summary_mode_no_thresholds(self, mock_backend, sample_docs):
        """summary 模式下 compare_report 不包含 thresholds"""
        from step3_seekdb_rag_hybrid.seek_query import generate_compare_report
        from step3_seekdb_rag_hybrid.dual_read_compare import CompareThresholds
        
        mock_backend.set_mock_data(sample_docs)
        
        # 构建测试数据
        primary_results = [
            EvidenceResult(
                chunk_id="chunk_a", chunk_idx=0, content="content_a",
                artifact_uri="uri_a", sha256="sha_a", source_id="s_a",
                source_type="git", excerpt="excerpt_a", relevance_score=0.95,
            ),
        ]
        shadow_results = [
            EvidenceResult(
                chunk_id="chunk_a", chunk_idx=0, content="content_a",
                artifact_uri="uri_a", sha256="sha_a", source_id="s_a",
                source_type="git", excerpt="excerpt_a", relevance_score=0.93,
            ),
        ]
        
        # 使用 summary 模式生成报告
        report = generate_compare_report(
            primary_results=primary_results,
            shadow_results=shadow_results,
            thresholds=CompareThresholds(),
            compare_mode="summary",
        )
        
        # summary 模式下 thresholds 应为 None
        assert report.thresholds is None
        
        # 转为 dict 后验证
        report_dict = report.to_dict()
        assert "thresholds" not in report_dict
    
    def test_detailed_mode_has_thresholds(self, mock_backend, sample_docs):
        """detailed 模式下 compare_report 包含 thresholds"""
        from step3_seekdb_rag_hybrid.seek_query import generate_compare_report
        from step3_seekdb_rag_hybrid.dual_read_compare import CompareThresholds
        
        mock_backend.set_mock_data(sample_docs)
        
        # 构建测试数据
        primary_results = [
            EvidenceResult(
                chunk_id="chunk_a", chunk_idx=0, content="content_a",
                artifact_uri="uri_a", sha256="sha_a", source_id="s_a",
                source_type="git", excerpt="excerpt_a", relevance_score=0.95,
            ),
        ]
        shadow_results = [
            EvidenceResult(
                chunk_id="chunk_a", chunk_idx=0, content="content_a",
                artifact_uri="uri_a", sha256="sha_a", source_id="s_a",
                source_type="git", excerpt="excerpt_a", relevance_score=0.93,
            ),
        ]
        
        thresholds = CompareThresholds(
            hit_overlap_min=0.7,
            rbo_min_warn=0.8,
        )
        
        # 使用 detailed 模式生成报告
        report = generate_compare_report(
            primary_results=primary_results,
            shadow_results=shadow_results,
            thresholds=thresholds,
            compare_mode="detailed",
        )
        
        # detailed 模式下 thresholds 应存在
        assert report.thresholds is not None
        
        # 转为 dict 后验证
        report_dict = report.to_dict()
        assert "thresholds" in report_dict
        assert report_dict["thresholds"]["hit_overlap_min"] == 0.7
    
    def test_detailed_mode_has_metadata(self, mock_backend, sample_docs):
        """detailed 模式下 compare_report 包含 metadata"""
        from step3_seekdb_rag_hybrid.seek_query import generate_compare_report
        from step3_seekdb_rag_hybrid.dual_read_compare import CompareThresholds
        
        mock_backend.set_mock_data(sample_docs)
        
        # 构建测试数据
        primary_results = [
            EvidenceResult(
                chunk_id="chunk_a", chunk_idx=0, content="content_a",
                artifact_uri="uri_a", sha256="sha_a", source_id="s_a",
                source_type="git", excerpt="excerpt_a", relevance_score=0.95,
            ),
            EvidenceResult(
                chunk_id="chunk_b", chunk_idx=0, content="content_b",
                artifact_uri="uri_b", sha256="sha_b", source_id="s_b",
                source_type="git", excerpt="excerpt_b", relevance_score=0.85,
            ),
        ]
        shadow_results = [
            EvidenceResult(
                chunk_id="chunk_a", chunk_idx=0, content="content_a",
                artifact_uri="uri_a", sha256="sha_a", source_id="s_a",
                source_type="git", excerpt="excerpt_a", relevance_score=0.93,
            ),
        ]
        
        # 使用 detailed 模式生成报告
        report = generate_compare_report(
            primary_results=primary_results,
            shadow_results=shadow_results,
            thresholds=CompareThresholds(),
            compare_mode="detailed",
        )
        
        # detailed 模式下 metadata 应存在且非空
        assert report.metadata is not None
        assert len(report.metadata) > 0
        
        # metadata 应包含 ranking_drift、score_drift、compare_mode
        assert "ranking_drift" in report.metadata
        assert "score_drift" in report.metadata
        assert "compare_mode" in report.metadata
        assert report.metadata["compare_mode"] == "detailed"
        
        # 转为 dict 后验证
        report_dict = report.to_dict()
        assert "metadata" in report_dict
        assert len(report_dict["metadata"]) > 0
    
    def test_summary_mode_no_detailed_metadata(self, mock_backend, sample_docs):
        """summary 模式下 compare_report 不包含详细 metadata"""
        from step3_seekdb_rag_hybrid.seek_query import generate_compare_report
        from step3_seekdb_rag_hybrid.dual_read_compare import CompareThresholds
        
        mock_backend.set_mock_data(sample_docs)
        
        # 构建测试数据
        primary_results = [
            EvidenceResult(
                chunk_id="chunk_a", chunk_idx=0, content="content_a",
                artifact_uri="uri_a", sha256="sha_a", source_id="s_a",
                source_type="git", excerpt="excerpt_a", relevance_score=0.95,
            ),
        ]
        shadow_results = [
            EvidenceResult(
                chunk_id="chunk_a", chunk_idx=0, content="content_a",
                artifact_uri="uri_a", sha256="sha_a", source_id="s_a",
                source_type="git", excerpt="excerpt_a", relevance_score=0.93,
            ),
        ]
        
        # 使用 summary 模式生成报告
        report = generate_compare_report(
            primary_results=primary_results,
            shadow_results=shadow_results,
            thresholds=CompareThresholds(),
            compare_mode="summary",
        )
        
        # summary 模式下 metadata 应为空
        assert report.metadata == {} or report.metadata is None or len(report.metadata) == 0
        
        # 转为 dict 后验证
        report_dict = report.to_dict()
        # metadata 为空时可能不在输出中
        if "metadata" in report_dict:
            assert len(report_dict["metadata"]) == 0


# ============ Test: 字段仅追加、不覆盖既有字段 ============


class TestFieldsAppendOnly:
    """测试字段仅追加、不覆盖既有字段（artifact_uri/evidence_uri 别名行为等）"""
    
    def test_artifact_uri_evidence_uri_both_present(self, mock_backend, sample_docs):
        """evidence_uri 作为别名与 artifact_uri 同时存在，不覆盖"""
        mock_backend.set_mock_data(sample_docs)
        
        result = run_query("XSS", top_k=5)
        packet = result.to_evidence_packet()
        
        for ev in packet["evidences"]:
            # 两者都应存在
            assert "artifact_uri" in ev
            assert "evidence_uri" in ev
            # 两者值相同
            assert ev["artifact_uri"] == ev["evidence_uri"]
            # artifact_uri 不应被 evidence_uri 覆盖（都保留）
            assert len(ev["artifact_uri"]) > 0
    
    def test_full_output_artifact_uri_evidence_uri_coexist(self, mock_backend, sample_docs):
        """full 输出中 artifact_uri 和 evidence_uri 同时存在"""
        mock_backend.set_mock_data(sample_docs)
        
        result = run_query("优化", top_k=5)
        full_output = result.to_dict()
        
        for ev in full_output["evidences"]:
            # 两者都应存在
            assert "artifact_uri" in ev
            assert "evidence_uri" in ev
            # 值相同
            assert ev["artifact_uri"] == ev["evidence_uri"]
    
    def test_compare_report_appends_to_packet(self, mock_backend, sample_docs):
        """compare_report 追加到 packet，不覆盖既有字段"""
        mock_backend.set_mock_data(sample_docs)
        
        shadow_backend = MockIndexBackend(
            mock_data=sample_docs,
            collection_id="shadow:v1:model",
        )
        config = DualReadConfig(enabled=True, strategy=DualReadConfig.STRATEGY_COMPARE)
        set_dual_read_config(config)
        set_shadow_backend(shadow_backend)
        
        try:
            result = run_query(
                query_text="XSS",
                top_k=5,
                shadow_backend=shadow_backend,
                dual_read_config=config,
            )
            
            packet = result.to_evidence_packet()
            
            # 验证核心字段仍存在
            assert "query" in packet
            assert "evidences" in packet
            assert "chunking_version" in packet
            assert "generated_at" in packet
            assert "result_count" in packet
            
            # 如果有 compare_report，它应该是追加的
            if "compare_report" in packet:
                # compare_report 不应覆盖任何核心字段
                assert packet["query"] == "XSS"
                assert len(packet["evidences"]) > 0
                # compare_report 内部结构正确
                assert "decision" in packet["compare_report"] or "metrics" in packet["compare_report"]
        finally:
            set_dual_read_config(None)
            set_shadow_backend(None)
    
    def test_dual_read_appends_to_full(self, mock_backend, sample_docs):
        """dual_read 追加到 full 输出，不覆盖既有字段"""
        mock_backend.set_mock_data(sample_docs)
        
        shadow_backend = MockIndexBackend(
            mock_data=sample_docs,
            collection_id="shadow:v1:model",
        )
        config = DualReadConfig(enabled=True, strategy=DualReadConfig.STRATEGY_COMPARE)
        set_dual_read_config(config)
        set_shadow_backend(shadow_backend)
        
        try:
            result = run_query(
                query_text="数据库",
                top_k=5,
                shadow_backend=shadow_backend,
                enable_dual_read=True,
            )
            
            full_output = result.to_dict(include_dual_read=True)
            
            # 验证核心字段仍存在且未被覆盖
            assert "success" in full_output
            assert "query" in full_output
            assert "filters" in full_output
            assert "top_k" in full_output
            assert "result_count" in full_output
            assert "evidences" in full_output
            assert "timing" in full_output
            
            # 如果有 dual_read，它应该是追加的
            if "dual_read" in full_output:
                # dual_read 不应覆盖任何核心字段
                assert full_output["query"] == "数据库"
                assert full_output["success"] is True
                # dual_read 内部结构正确
                assert "health" in full_output["dual_read"]
        finally:
            set_dual_read_config(None)
            set_shadow_backend(None)
    
    def test_evidence_fields_preserved_with_dual_read(self, mock_backend, sample_docs):
        """启用双读时 evidence 字段保持完整，不被覆盖"""
        mock_backend.set_mock_data(sample_docs)
        
        shadow_backend = MockIndexBackend(
            mock_data=sample_docs,
            collection_id="shadow:v1:model",
        )
        config = DualReadConfig(enabled=True, strategy=DualReadConfig.STRATEGY_COMPARE)
        set_dual_read_config(config)
        set_shadow_backend(shadow_backend)
        
        try:
            result = run_query(
                query_text="XSS",
                top_k=5,
                shadow_backend=shadow_backend,
                enable_dual_read=True,
            )
            
            # 验证 evidence 字段完整性
            for ev in result.evidences:
                # 核心字段
                assert ev.chunk_id is not None
                assert ev.artifact_uri is not None
                assert ev.evidence_uri is not None  # 别名
                assert ev.artifact_uri == ev.evidence_uri
                assert ev.source_type is not None
                assert ev.source_id is not None
                
            # 验证输出格式中字段完整
            packet = result.to_evidence_packet()
            for ev_dict in packet["evidences"]:
                assert "chunk_id" in ev_dict
                assert "artifact_uri" in ev_dict
                assert "evidence_uri" in ev_dict
                assert "source_type" in ev_dict
                assert "relevance_score" in ev_dict
        finally:
            set_dual_read_config(None)
            set_shadow_backend(None)
    
    def test_metadata_preserved_in_evidence(self, mock_backend, sample_docs):
        """evidence 的 metadata 字段保持完整"""
        mock_backend.set_mock_data(sample_docs)
        
        result = run_query("XSS", top_k=5)
        full_output = result.to_dict()
        
        # 验证有 metadata 的 evidence 保持 metadata
        for ev in full_output["evidences"]:
            assert "metadata" in ev
            # metadata 可以是空字典但不能缺失
            assert isinstance(ev["metadata"], dict)


# ============ Test: Attachment artifact_uri 与 Step1 互通性集成测试 ============


class TestAttachmentArtifactUriStep1Integration:
    """测试 attachment artifact_uri 与 Step1 parse_attachment_evidence_uri() 的互通性"""
    
    def test_evidence_result_attachment_uri_parsed_by_step1(self):
        """构造 EvidenceResult -> to_evidence_dict()，验证 artifact_uri 可被 Step1 解析"""
        attachment_id = 12345
        sha256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        artifact_uri = f"memory://attachments/{attachment_id}/{sha256}"
        
        # 构造 EvidenceResult
        result = EvidenceResult(
            chunk_id=f"webapp:logbook:{attachment_id}:v1:0",
            chunk_idx=0,
            content="附件测试内容",
            artifact_uri=artifact_uri,
            sha256=sha256,
            source_id=str(attachment_id),
            source_type="logbook",
            excerpt="附件摘要...",
            relevance_score=0.85,
            metadata={"kind": "screenshot"},
        )
        
        # 转换为 evidence dict
        evidence_dict = result.to_evidence_dict(include_content=False)
        
        # 验证 artifact_uri 和 evidence_uri 存在
        assert "artifact_uri" in evidence_dict
        assert "evidence_uri" in evidence_dict
        assert evidence_dict["artifact_uri"] == artifact_uri
        assert evidence_dict["evidence_uri"] == artifact_uri
        
        # 使用 Step1 的 parse_attachment_evidence_uri 解析
        parsed = parse_attachment_evidence_uri(evidence_dict["artifact_uri"])
        
        # 断言解析成功
        assert parsed is not None, "Step1 parse_attachment_evidence_uri 应能解析 attachment URI"
        assert parsed["attachment_id"] == attachment_id
        assert parsed["sha256"] == sha256
    
    def test_evidence_packet_attachment_uri_parsed_by_step1(self, mock_backend, sample_docs):
        """通过 to_evidence_packet() 输出的 attachment artifact_uri 应可被 Step1 解析"""
        mock_backend.set_mock_data(sample_docs)
        
        # 执行查询
        result = run_query("发布版本", top_k=5)
        packet = result.to_evidence_packet()
        
        # 找到 logbook 类型的 evidence（使用 memory://attachments/ URI）
        logbook_evidences = [
            ev for ev in packet["evidences"]
            if ev.get("source_type") == "logbook"
        ]
        
        assert len(logbook_evidences) > 0, "应有 logbook 类型的 evidence"
        
        for ev in logbook_evidences:
            artifact_uri = ev["artifact_uri"]
            
            # 验证 URI scheme 正确
            assert artifact_uri.startswith("memory://attachments/"), \
                f"logbook evidence 应使用 memory://attachments/ scheme: {artifact_uri}"
            
            # 使用 Step1 解析
            parsed = parse_attachment_evidence_uri(artifact_uri)
            
            # 断言解析成功且 attachment_id 为整数
            assert parsed is not None, f"Step1 应能解析 artifact_uri: {artifact_uri}"
            assert isinstance(parsed["attachment_id"], int), \
                f"attachment_id 应为整数: {parsed['attachment_id']}"
            assert parsed["sha256"] == ev["sha256"]
    
    def test_attachment_uri_integer_id_format(self):
        """验证 attachment URI 使用整数 ID 格式"""
        attachment_id = 99999
        sha256 = "a" * 64
        artifact_uri = f"memory://attachments/{attachment_id}/{sha256}"
        
        # 构造 EvidenceResult
        result = EvidenceResult(
            chunk_id=f"test:logbook:{attachment_id}:v1:0",
            chunk_idx=0,
            content="内容",
            artifact_uri=artifact_uri,
            sha256=sha256,
            source_id=str(attachment_id),
            source_type="logbook",
            excerpt="摘要",
            relevance_score=0.9,
            metadata={},
        )
        
        # 验证 Step1 可以正确解析
        parsed = parse_attachment_evidence_uri(result.artifact_uri)
        assert parsed is not None
        assert parsed["attachment_id"] == attachment_id
        
        # 验证 evidence_uri 别名也可解析
        parsed_alias = parse_attachment_evidence_uri(result.evidence_uri)
        assert parsed_alias is not None
        assert parsed_alias["attachment_id"] == attachment_id
    
    def test_attachment_uri_scheme_consistency(self):
        """验证 attachment URI scheme 与 Step1 规范一致"""
        # Step1 规范: memory://attachments/<attachment_id>/<sha256>
        test_cases = [
            (12345, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),
            (1, "a" * 64),
            (999999999, "b" * 64),
        ]
        
        for attachment_id, sha256 in test_cases:
            artifact_uri = f"memory://attachments/{attachment_id}/{sha256}"
            
            result = EvidenceResult(
                chunk_id=f"test:logbook:{attachment_id}:v1:0",
                chunk_idx=0,
                content="",
                artifact_uri=artifact_uri,
                sha256=sha256,
                source_id=str(attachment_id),
                source_type="logbook",
                excerpt="",
                relevance_score=0.5,
                metadata={},
            )
            
            # 转换为 packet 格式
            evidence_dict = result.to_evidence_dict(include_content=False)
            
            # 验证 scheme 一致性
            assert evidence_dict["artifact_uri"].startswith("memory://attachments/")
            
            # Step1 解析验证
            parsed = parse_attachment_evidence_uri(evidence_dict["artifact_uri"])
            assert parsed is not None, f"attachment_id={attachment_id} 应可解析"
            assert parsed["attachment_id"] == attachment_id
            assert parsed["sha256"] == sha256


# ============ Test: retrieval_context 字段测试 ============


class TestRetrievalContextDataClass:
    """测试 RetrievalContext 数据类"""
    
    def test_to_dict_empty(self):
        """空 RetrievalContext 返回空字典"""
        ctx = RetrievalContext()
        result = ctx.to_dict()
        assert result == {}
    
    def test_to_dict_backend_info(self):
        """测试后端信息输出"""
        ctx = RetrievalContext(
            backend_name="pgvector",
            backend_config={"type": "pgvector", "schema": "step3", "table": "chunks"},
            collection_id="webapp:v1:bge-m3",
        )
        result = ctx.to_dict()
        
        assert result["backend_name"] == "pgvector"
        assert result["backend_config"]["type"] == "pgvector"
        assert result["backend_config"]["schema"] == "step3"
        assert result["collection_id"] == "webapp:v1:bge-m3"
    
    def test_to_dict_embedding_info(self):
        """测试 embedding 信息输出"""
        ctx = RetrievalContext(
            embedding_model_id="bge-m3",
            embedding_dim=1024,
            embedding_normalize=True,
        )
        result = ctx.to_dict()
        
        assert "embedding" in result
        assert result["embedding"]["model_id"] == "bge-m3"
        assert result["embedding"]["dim"] == 1024
        assert result["embedding"]["normalize"] is True
    
    def test_to_dict_hybrid_config(self):
        """测试 hybrid 配置输出"""
        ctx = RetrievalContext(
            hybrid_config={
                "vector_weight": 0.7,
                "text_weight": 0.3,
                "normalize_scores": True,
            },
        )
        result = ctx.to_dict()
        
        assert "hybrid_config" in result
        assert result["hybrid_config"]["vector_weight"] == 0.7
        assert result["hybrid_config"]["text_weight"] == 0.3
        assert result["hybrid_config"]["normalize_scores"] is True
    
    def test_to_dict_query_request(self):
        """测试查询请求参数输出"""
        ctx = RetrievalContext(
            query_request={
                "top_k": 10,
                "min_score": 0.5,
                "filters": {"project_key": "webapp"},
            },
        )
        result = ctx.to_dict()
        
        assert "query_request" in result
        assert result["query_request"]["top_k"] == 10
        assert result["query_request"]["min_score"] == 0.5
        assert result["query_request"]["filters"]["project_key"] == "webapp"
    
    def test_to_dict_full(self):
        """测试完整 RetrievalContext 输出"""
        ctx = RetrievalContext(
            backend_name="pgvector",
            backend_config={"type": "pgvector", "schema": "step3", "strategy": "single_table"},
            collection_id="webapp:v1:bge-m3:20260128",
            embedding_model_id="bge-m3",
            embedding_dim=1024,
            embedding_normalize=True,
            hybrid_config={"vector_weight": 0.7, "text_weight": 0.3},
            query_request={"top_k": 10, "filters": {"source_type": "git"}},
        )
        result = ctx.to_dict()
        
        # 验证所有顶级字段存在
        assert "backend_name" in result
        assert "backend_config" in result
        assert "collection_id" in result
        assert "embedding" in result
        assert "hybrid_config" in result
        assert "query_request" in result
        
        # 验证 embedding 子字段
        assert result["embedding"]["model_id"] == "bge-m3"
        assert result["embedding"]["dim"] == 1024
        assert result["embedding"]["normalize"] is True


class TestRetrievalContextInPacket:
    """测试 retrieval_context 在 packet 输出中的集成"""
    
    def test_packet_includes_retrieval_context(self, mock_backend, sample_docs):
        """packet 格式包含 retrieval_context"""
        mock_backend.set_mock_data(sample_docs)
        
        result = run_query("XSS 漏洞", top_k=5)
        packet = result.to_evidence_packet()
        
        # 验证 retrieval_context 存在
        assert "retrieval_context" in packet
        ctx = packet["retrieval_context"]
        
        # 验证必要字段存在
        assert "backend_name" in ctx
        assert ctx["backend_name"] == "mock"
        
        # 验证 query_request 存在并包含 top_k
        assert "query_request" in ctx
        assert ctx["query_request"]["top_k"] == 5
    
    def test_full_output_includes_retrieval_context(self, mock_backend, sample_docs):
        """full 格式包含 retrieval_context"""
        mock_backend.set_mock_data(sample_docs)
        
        result = run_query("数据库优化", top_k=10)
        full_output = result.to_dict()
        
        # 验证 retrieval_context 存在
        assert "retrieval_context" in full_output
        ctx = full_output["retrieval_context"]
        
        # 验证后端信息
        assert "backend_name" in ctx
        
        # 验证 query_request
        assert "query_request" in ctx
        assert ctx["query_request"]["top_k"] == 10
    
    def test_packet_retrieval_context_with_filters(self, mock_backend, sample_docs):
        """带过滤条件时 retrieval_context 包含 filters DSL"""
        mock_backend.set_mock_data(sample_docs)
        
        filters = QueryFilters(
            project_key="webapp",
            source_type="git",
            module="src/",
        )
        result = run_query("修复漏洞", filters=filters, top_k=5)
        packet = result.to_evidence_packet()
        
        # 验证 retrieval_context 包含 filters
        assert "retrieval_context" in packet
        ctx = packet["retrieval_context"]
        
        assert "query_request" in ctx
        assert "filters" in ctx["query_request"]
        
        # 验证 filter DSL 格式
        filters_dsl = ctx["query_request"]["filters"]
        assert filters_dsl["project_key"] == "webapp"
        assert filters_dsl["source_type"] == "git"
        assert filters_dsl["module"] == {"$prefix": "src/"}
    
    def test_retrieval_context_collection_id(self, sample_docs):
        """测试 retrieval_context 包含正确的 collection_id"""
        # 创建带有特定 collection_id 的 backend
        backend = MockIndexBackend(
            mock_data=sample_docs,
            collection_id="test-project:v2:bge-m3:20260128",
        )
        set_index_backend(backend)
        set_embedding_provider_instance(None)
        
        try:
            result = run_query("测试查询", top_k=3)
            packet = result.to_evidence_packet()
            
            # 验证 collection_id 存在
            assert "retrieval_context" in packet
            ctx = packet["retrieval_context"]
            
            assert "collection_id" in ctx
            assert ctx["collection_id"] == "test-project:v2:bge-m3:20260128"
        finally:
            set_index_backend(None)


class TestRetrievalContextSchemaStability:
    """测试 retrieval_context schema 稳定性（只追加不破坏兼容）"""
    
    def test_required_top_level_fields(self):
        """验证必要的顶级字段可以被设置"""
        # 这些字段是契约的一部分，不应被移除
        ctx = RetrievalContext(
            backend_name="pgvector",
            backend_config={"type": "pgvector"},
            collection_id="default:v1:model",
            embedding_model_id="bge-m3",
            embedding_dim=1024,
            embedding_normalize=True,
            hybrid_config={"vector_weight": 0.7, "text_weight": 0.3},
            query_request={"top_k": 10},
        )
        
        # 验证所有字段都可访问
        assert ctx.backend_name == "pgvector"
        assert ctx.backend_config is not None
        assert ctx.collection_id == "default:v1:model"
        assert ctx.embedding_model_id == "bge-m3"
        assert ctx.embedding_dim == 1024
        assert ctx.embedding_normalize is True
        assert ctx.hybrid_config is not None
        assert ctx.query_request is not None
    
    def test_to_dict_output_structure(self):
        """验证 to_dict 输出结构"""
        ctx = RetrievalContext(
            backend_name="pgvector",
            backend_config={"type": "pgvector", "schema": "step3"},
            collection_id="webapp:v1:bge-m3",
            embedding_model_id="bge-m3",
            embedding_dim=1024,
            hybrid_config={"vector_weight": 0.7, "text_weight": 0.3},
            query_request={"top_k": 10, "filters": {"project_key": "webapp"}},
        )
        result = ctx.to_dict()
        
        # 验证结构符合契约
        # 1. backend_name 是字符串
        assert isinstance(result["backend_name"], str)
        
        # 2. backend_config 是字典
        assert isinstance(result["backend_config"], dict)
        assert "type" in result["backend_config"]
        
        # 3. collection_id 是字符串
        assert isinstance(result["collection_id"], str)
        
        # 4. embedding 是字典，包含 model_id 和 dim
        assert isinstance(result["embedding"], dict)
        assert "model_id" in result["embedding"]
        assert "dim" in result["embedding"]
        
        # 5. hybrid_config 是字典，包含权重
        assert isinstance(result["hybrid_config"], dict)
        assert "vector_weight" in result["hybrid_config"]
        assert "text_weight" in result["hybrid_config"]
        
        # 6. query_request 是字典，包含 top_k
        assert isinstance(result["query_request"], dict)
        assert "top_k" in result["query_request"]
    
    def test_backend_config_no_password(self):
        """验证 backend_config 不包含敏感信息（密码）"""
        ctx = RetrievalContext(
            backend_name="pgvector",
            backend_config={
                "type": "pgvector",
                "schema": "step3",
                "table": "chunks",
                "strategy": "single_table",
                # 注意：不应包含 dsn、password 等敏感字段
            },
        )
        result = ctx.to_dict()
        
        # 验证不包含敏感字段
        config = result["backend_config"]
        assert "password" not in config
        assert "dsn" not in config
        assert "connection_string" not in config
        assert "api_key" not in config
    
    def test_query_request_filters_dsl_format(self):
        """验证 query_request.filters 使用 DSL 格式"""
        filters = QueryFilters(
            project_key="webapp",
            module="src/auth/",
            time_range_start="2024-01-01T00:00:00Z",
            time_range_end="2024-12-31T23:59:59Z",
        )
        
        ctx = RetrievalContext(
            query_request={
                "top_k": 10,
                "filters": filters.to_filter_dict(),
            },
        )
        result = ctx.to_dict()
        
        filters_dsl = result["query_request"]["filters"]
        
        # 验证 DSL 格式
        assert filters_dsl["project_key"] == "webapp"
        assert filters_dsl["module"] == {"$prefix": "src/auth/"}
        assert "commit_ts" in filters_dsl
        assert filters_dsl["commit_ts"]["$gte"] == "2024-01-01T00:00:00Z"
        assert filters_dsl["commit_ts"]["$lte"] == "2024-12-31T23:59:59Z"


class TestRetrievalContextFieldsAppendOnly:
    """测试 retrieval_context 字段仅追加、不覆盖既有字段"""
    
    def test_retrieval_context_does_not_override_existing_fields(self, mock_backend, sample_docs):
        """retrieval_context 追加到 packet，不覆盖核心字段"""
        mock_backend.set_mock_data(sample_docs)
        
        result = run_query("XSS", top_k=5)
        packet = result.to_evidence_packet()
        
        # 验证核心字段仍存在且未被覆盖
        assert "query" in packet
        assert packet["query"] == "XSS"
        
        assert "evidences" in packet
        assert len(packet["evidences"]) > 0
        
        assert "chunking_version" in packet
        assert "generated_at" in packet
        assert "result_count" in packet
        
        # 验证 retrieval_context 是追加的，不影响其他字段
        assert "retrieval_context" in packet
    
    def test_retrieval_context_coexists_with_filters(self, mock_backend, sample_docs):
        """retrieval_context 与 filters 字段共存"""
        mock_backend.set_mock_data(sample_docs)
        
        filters = QueryFilters(project_key="webapp")
        result = run_query("修复", filters=filters, top_k=5)
        packet = result.to_evidence_packet()
        
        # 验证 filters 字段存在（在 packet 顶层）
        assert "filters" in packet
        assert packet["filters"]["project_key"] == "webapp"
        
        # 验证 retrieval_context 也存在
        assert "retrieval_context" in packet
        
        # 两者独立存在，互不干扰
        assert packet["filters"] != packet["retrieval_context"]
    
    def test_retrieval_context_coexists_with_dual_read(self, mock_backend, sample_docs):
        """retrieval_context 与 dual_read 字段共存"""
        mock_backend.set_mock_data(sample_docs)
        
        shadow_backend = MockIndexBackend(
            mock_data=sample_docs,
            collection_id="shadow:v1:model",
        )
        config = DualReadConfig(enabled=True, strategy=DualReadConfig.STRATEGY_COMPARE)
        set_dual_read_config(config)
        set_shadow_backend(shadow_backend)
        
        try:
            result = run_query(
                query_text="XSS",
                top_k=5,
                shadow_backend=shadow_backend,
                enable_dual_read=True,
            )
            
            packet = result.to_evidence_packet(include_dual_read=True)
            
            # 验证 retrieval_context 存在
            assert "retrieval_context" in packet
            
            # 验证 dual_read 也存在
            assert "dual_read" in packet
            
            # 两者独立存在
            assert "health" in packet["dual_read"]
            assert "backend_name" in packet["retrieval_context"]
        finally:
            set_dual_read_config(None)
            set_shadow_backend(None)


# =============================================================================
# GateProfile 与 thresholds_metadata 测试
# =============================================================================


class TestGateProfileToDict:
    """测试 GateProfile.to_dict() 输出格式稳定性"""
    
    def test_gate_profile_to_dict_contains_required_fields(self):
        """GateProfile.to_dict() 应包含 name/version/source 字段"""
        profile = GateProfile(
            name="dual_read_gate",
            version="1.0",
            source=THRESHOLD_SOURCE_CLI,
        )
        
        result = profile.to_dict()
        
        # 验证必须字段存在
        assert "name" in result
        assert "version" in result
        assert "source" in result
        
        # 验证字段值正确
        assert result["name"] == "dual_read_gate"
        assert result["version"] == "1.0"
        assert result["source"] == THRESHOLD_SOURCE_CLI
    
    def test_gate_profile_default_values(self):
        """GateProfile 默认值应正确序列化"""
        profile = GateProfile()
        
        result = profile.to_dict()
        
        assert result["name"] == "dual_read_gate"
        assert result["version"] == "1.0"
        assert result["source"] == THRESHOLD_SOURCE_DEFAULT
    
    def test_gate_profile_from_dict_roundtrip(self):
        """GateProfile 序列化/反序列化往返正确"""
        original = GateProfile(
            name="custom_gate",
            version="2.0",
            source=THRESHOLD_SOURCE_ENV,
        )
        
        # 序列化
        data = original.to_dict()
        
        # 反序列化
        restored = GateProfile.from_dict(data)
        
        assert restored.name == original.name
        assert restored.version == original.version
        assert restored.source == original.source


class TestDualReadGateThresholdsToDictWithProfile:
    """测试 DualReadGateThresholds.to_dict() 包含 profile 信息"""
    
    def test_to_dict_includes_profile(self):
        """to_dict() 应包含完整的 profile 字段"""
        thresholds = DualReadGateThresholds(
            min_overlap=0.7,
            max_only_primary=5,
            max_only_shadow=3,
            max_score_drift=0.1,
            profile=GateProfile(
                name="dual_read_gate",
                version="1.0",
                source=THRESHOLD_SOURCE_CLI,
            ),
        )
        
        result = thresholds.to_dict()
        
        # 验证 profile 字段存在
        assert "profile" in result
        
        # 验证 profile 子字段
        profile = result["profile"]
        assert "name" in profile
        assert "version" in profile
        assert "source" in profile
        
        # 验证 profile 值正确
        assert profile["name"] == "dual_read_gate"
        assert profile["version"] == "1.0"
        assert profile["source"] == THRESHOLD_SOURCE_CLI
    
    def test_to_dict_preserves_compat_fields(self):
        """to_dict() 应保留兼容字段（现有 consumers 期望的 keys）"""
        thresholds = DualReadGateThresholds(
            min_overlap=0.8,
            max_only_primary=10,
            max_only_shadow=5,
            max_score_drift=0.15,
            profile=GateProfile(source=THRESHOLD_SOURCE_CLI),
        )
        
        result = thresholds.to_dict()
        
        # 验证兼容字段存在
        assert "min_overlap" in result
        assert "max_only_primary" in result
        assert "max_only_shadow" in result
        assert "max_score_drift" in result
        assert "source" in result  # 顶层兼容字段
        
        # 验证兼容字段值正确
        assert result["min_overlap"] == 0.8
        assert result["max_only_primary"] == 10
        assert result["max_only_shadow"] == 5
        assert result["max_score_drift"] == 0.15
        assert result["source"] == THRESHOLD_SOURCE_CLI
    
    def test_to_dict_without_profile_provides_default(self):
        """没有 profile 时，to_dict() 应提供默认 profile"""
        thresholds = DualReadGateThresholds(
            min_overlap=0.6,
        )
        
        result = thresholds.to_dict()
        
        # 即使没有设置 profile，也应该有 profile 字段
        assert "profile" in result
        assert result["profile"]["name"] == "dual_read_gate"
        assert result["profile"]["version"] == "1.0"
        assert result["profile"]["source"] == THRESHOLD_SOURCE_DEFAULT
        
        # 顶层 source 也应该是 default
        assert result["source"] == THRESHOLD_SOURCE_DEFAULT
    
    def test_to_dict_omits_none_threshold_values(self):
        """to_dict() 应只包含非 None 的阈值字段"""
        thresholds = DualReadGateThresholds(
            min_overlap=0.7,
            # 其他字段为 None
            profile=GateProfile(source=THRESHOLD_SOURCE_CLI),
        )
        
        result = thresholds.to_dict()
        
        # min_overlap 存在
        assert "min_overlap" in result
        assert result["min_overlap"] == 0.7
        
        # 其他阈值字段不存在（因为为 None）
        assert "max_only_primary" not in result
        assert "max_only_shadow" not in result
        assert "max_score_drift" not in result
        
        # profile 和 source 始终存在
        assert "profile" in result
        assert "source" in result
    
    def test_to_dict_json_serializable(self):
        """to_dict() 输出应可直接 JSON 序列化"""
        thresholds = DualReadGateThresholds(
            min_overlap=0.75,
            max_only_primary=8,
            max_only_shadow=4,
            max_score_drift=0.12,
            profile=GateProfile(
                name="dual_read_gate",
                version="1.0",
                source=THRESHOLD_SOURCE_CLI,
            ),
        )
        
        result = thresholds.to_dict()
        
        # 验证可以 JSON 序列化
        json_str = json.dumps(result)
        
        # 验证可以反序列化
        parsed = json.loads(json_str)
        
        # 验证字段稳定（反序列化后与原始一致）
        assert parsed["min_overlap"] == 0.75
        assert parsed["max_only_primary"] == 8
        assert parsed["profile"]["name"] == "dual_read_gate"
        assert parsed["profile"]["version"] == "1.0"
        assert parsed["profile"]["source"] == THRESHOLD_SOURCE_CLI


class TestThresholdsMetadataProfileFields:
    """测试 thresholds_metadata.gate_thresholds.profile 字段稳定性"""
    
    def test_gate_thresholds_profile_fields_stable(self):
        """验证 gate_thresholds 输出包含稳定的 profile 字段"""
        # 模拟 CLI 参数创建的 thresholds
        thresholds = DualReadGateThresholds(
            min_overlap=0.7,
            max_only_primary=5,
            max_only_shadow=3,
            max_score_drift=0.1,
            profile=GateProfile(
                name="dual_read_gate",
                version="1.0",
                source=THRESHOLD_SOURCE_CLI,
            ),
        )
        
        # 模拟 thresholds_metadata 构建
        thresholds_metadata = {
            "gate_thresholds": thresholds.to_dict(),
        }
        
        # JSON 序列化
        json_output = json.dumps(thresholds_metadata, indent=2)
        parsed = json.loads(json_output)
        
        gate = parsed["gate_thresholds"]
        
        # 验证 profile 字段存在且稳定
        assert "profile" in gate, "thresholds_metadata.gate_thresholds.profile 必须存在"
        assert "name" in gate["profile"], "profile.name 必须存在"
        assert "version" in gate["profile"], "profile.version 必须存在"
        assert "source" in gate["profile"], "profile.source 必须存在"
        
        # 验证字段值符合预期
        assert gate["profile"]["name"] == "dual_read_gate"
        assert gate["profile"]["version"] == "1.0"
        assert gate["profile"]["source"] == "cli"
    
    def test_gate_thresholds_compat_and_profile_coexist(self):
        """验证兼容字段与 profile 字段共存"""
        thresholds = DualReadGateThresholds(
            min_overlap=0.8,
            max_only_primary=10,
            profile=GateProfile(
                name="dual_read_gate",
                version="1.0",
                source=THRESHOLD_SOURCE_CLI,
            ),
        )
        
        result = thresholds.to_dict()
        
        # 兼容字段存在
        assert result["min_overlap"] == 0.8
        assert result["max_only_primary"] == 10
        assert result["source"] == "cli"  # 顶层兼容字段
        
        # profile 字段也存在
        assert result["profile"]["name"] == "dual_read_gate"
        assert result["profile"]["source"] == "cli"
        
        # 两者 source 一致
        assert result["source"] == result["profile"]["source"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
