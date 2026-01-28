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
    check_dual_read_gate,
    compute_dual_read_stats,
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
            "chunk_id": "webapp:logbook:evt123:v1:0",
            "content": "发布 v2.0 版本记录",
            "score": 0.75,
            "source_type": "logbook",
            "source_id": "evt123",
            "artifact_uri": "memory://attachments/evt123/sha256_log",
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
        assert result.violations[0].threshold == 0.9
        assert result.violations[0].actual == 0.75
    
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
        assert result.violations[0].threshold == 1.0
        assert result.violations[0].actual == 2.0
    
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
        assert result.violations[0].threshold == 0.0
        assert result.violations[0].actual == 1.0
    
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
        assert result.violations[0].threshold == 0.05
        assert result.violations[0].actual == 0.08
    
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
                    threshold=0.9,
                    actual=0.75,
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
                    threshold=0.9,
                    actual=0.75,
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
