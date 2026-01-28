#!/usr/bin/env python3
"""
benchmark_pgvector_collection_strategies.py - PGVector Collection 策略基准测试

测试目标:
- 对比 per_table (DefaultCollectionStrategy) 和 single_table (SharedTableStrategy) 两种策略
- 生成多 collection、多 project_key、多版本的数据集
- 记录关键指标：时间、行数、索引大小、查询延迟

环境要求:
- 设置环境变量 TEST_PGVECTOR_DSN 指向可用的 PostgreSQL 实例
- PostgreSQL 实例需要已安装 pgvector 扩展
- 示例: TEST_PGVECTOR_DSN=postgresql://postgres:postgres@localhost:5432/engram

使用方法:
    # 运行所有基准测试
    pytest tests/benchmark_pgvector_collection_strategies.py -v -s
    
    # 只运行特定基准
    pytest tests/benchmark_pgvector_collection_strategies.py::TestCollectionStrategyBenchmark -v -s
    
    # 独立运行脚本（显示详细报告）
    python tests/benchmark_pgvector_collection_strategies.py

复用:
- 使用 DeterministicEmbeddingMock，无需外部 embedding 服务
- 所有数据在测试 schema 中生成，测试后自动清理
"""

import os
import sys
import time
import uuid
import json
import statistics
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any, Tuple
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path

# 路径配置（支持独立运行和 pytest 运行）
_script_dir = Path(__file__).parent
_step3_path = _script_dir.parent
if str(_step3_path) not in sys.path:
    sys.path.insert(0, str(_step3_path))

import pytest

from index_backend.types import ChunkDoc, QueryRequest, QueryHit
from index_backend.pgvector_backend import (
    PGVectorBackend,
    HybridSearchConfig,
)
from index_backend.pgvector_collection_strategy import (
    DefaultCollectionStrategy,
    SharedTableStrategy,
    StorageResolution,
)

# 引入双读对比模块
from dual_read_compare import (
    CompareThresholds,
    CompareMetrics,
    CompareReport,
    OverlapMetrics,
    RankingDriftMetrics,
    ScoreDriftMetrics,
    compute_overlap_metrics,
    compute_ranking_drift,
    compute_score_drift,
    evaluate_with_report,
)


# ============ 环境配置 ============

TEST_PGVECTOR_DSN = os.environ.get("TEST_PGVECTOR_DSN")
TEST_SCHEMA = "step3_benchmark"  # 使用专用 benchmark schema

# 跳过条件
skip_no_dsn = pytest.mark.skipif(
    not TEST_PGVECTOR_DSN,
    reason="TEST_PGVECTOR_DSN 环境变量未设置，跳过 PGVector 基准测试"
)


# ============ Deterministic Embedding Mock (复用现有实现) ============


class DeterministicEmbeddingMock:
    """
    确定性 Embedding Mock
    
    对于相同的文本，总是返回相同的向量。
    使用简单的 hash 算法生成确定性向量。
    """
    
    def __init__(self, dim: int = 128):
        """
        初始化 Mock
        
        Args:
            dim: 向量维度（测试用较小维度以加速）
        """
        self._dim = dim
        self._model_id = "deterministic-mock-benchmark"
    
    @property
    def model_id(self) -> str:
        return self._model_id
    
    @property
    def dim(self) -> int:
        return self._dim
    
    def embed_text(self, text: str) -> List[float]:
        """生成确定性向量"""
        return self._text_to_vector(text)
    
    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """批量生成确定性向量"""
        return [self._text_to_vector(t) for t in texts]
    
    def _text_to_vector(self, text: str) -> List[float]:
        """
        将文本转换为确定性向量
        
        使用文本的 hash 值作为种子生成向量。
        """
        hash_val = hash(text)
        vector = []
        for i in range(self._dim):
            # 生成 -1 到 1 之间的确定性值
            val = ((hash_val + i * 31) % 1000) / 500.0 - 1.0
            vector.append(val)
        
        # 归一化
        norm = sum(v * v for v in vector) ** 0.5
        if norm > 0:
            vector = [v / norm for v in vector]
        
        return vector


# ============ 数据集生成器 ============


@dataclass
class DatasetConfig:
    """数据集配置"""
    num_collections: int = 3
    num_project_keys: int = 2
    num_versions_per_project: int = 2
    docs_per_collection: int = 100
    embedding_dim: int = 128
    
    @property
    def total_docs(self) -> int:
        """总文档数量"""
        return self.num_collections * self.docs_per_collection


@dataclass
class BenchmarkMetrics:
    """基准测试指标"""
    strategy_name: str
    setup_time_ms: float = 0.0
    insert_time_ms: float = 0.0
    insert_docs_per_sec: float = 0.0
    total_rows: int = 0
    index_size_bytes: int = 0
    table_size_bytes: int = 0
    query_latencies_ms: List[float] = field(default_factory=list)
    
    @property
    def avg_query_latency_ms(self) -> float:
        if not self.query_latencies_ms:
            return 0.0
        return statistics.mean(self.query_latencies_ms)
    
    @property
    def p95_query_latency_ms(self) -> float:
        if not self.query_latencies_ms:
            return 0.0
        return statistics.quantiles(self.query_latencies_ms, n=20)[18]  # 95th percentile
    
    @property
    def min_query_latency_ms(self) -> float:
        if not self.query_latencies_ms:
            return 0.0
        return min(self.query_latencies_ms)
    
    @property
    def max_query_latency_ms(self) -> float:
        if not self.query_latencies_ms:
            return 0.0
        return max(self.query_latencies_ms)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy_name": self.strategy_name,
            "setup_time_ms": round(self.setup_time_ms, 2),
            "insert_time_ms": round(self.insert_time_ms, 2),
            "insert_docs_per_sec": round(self.insert_docs_per_sec, 2),
            "total_rows": self.total_rows,
            "index_size_bytes": self.index_size_bytes,
            "index_size_kb": round(self.index_size_bytes / 1024, 2),
            "table_size_bytes": self.table_size_bytes,
            "table_size_kb": round(self.table_size_bytes / 1024, 2),
            "avg_query_latency_ms": round(self.avg_query_latency_ms, 3),
            "p95_query_latency_ms": round(self.p95_query_latency_ms, 3),
            "min_query_latency_ms": round(self.min_query_latency_ms, 3),
            "max_query_latency_ms": round(self.max_query_latency_ms, 3),
            "num_queries": len(self.query_latencies_ms),
        }
    
    def __str__(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)


class DatasetGenerator:
    """数据集生成器"""
    
    def __init__(self, config: DatasetConfig, unique_id: str):
        self.config = config
        self.unique_id = unique_id
        self._embedding_mock = DeterministicEmbeddingMock(dim=config.embedding_dim)
        
        # 生成 collection 列表
        self._collections = self._generate_collections()
    
    def _generate_collections(self) -> List[Dict[str, str]]:
        """生成 collection 信息列表"""
        collections = []
        idx = 0
        for proj_idx in range(self.config.num_project_keys):
            project_key = f"proj_{proj_idx}"
            for ver_idx in range(self.config.num_versions_per_project):
                version = f"v{ver_idx + 1}"
                # 确保不超过 num_collections
                if idx >= self.config.num_collections:
                    break
                collections.append({
                    "project_key": project_key,
                    "version": version,
                    "collection_id": f"{project_key}:{version}:mock-embed:{self.unique_id}",
                })
                idx += 1
            if idx >= self.config.num_collections:
                break
        return collections
    
    @property
    def collections(self) -> List[Dict[str, str]]:
        """返回 collection 列表"""
        return self._collections
    
    @property
    def embedding_mock(self) -> DeterministicEmbeddingMock:
        """返回 embedding mock"""
        return self._embedding_mock
    
    def generate_docs_for_collection(self, collection: Dict[str, str]) -> List[ChunkDoc]:
        """为单个 collection 生成文档"""
        docs = []
        project_key = collection["project_key"]
        version = collection["version"]
        collection_id = collection["collection_id"]
        
        # 预定义的内容模板（用于混合检索测试）
        content_templates = [
            "修复了用户登录时的 {type} 漏洞，使用 escape 函数进行处理",
            "添加了输入验证单元测试，覆盖了各种边界情况",
            "优化了数据库查询性能，添加了索引",
            "重构了用户认证模块，支持 OAuth2.0",
            "Bug修复: 修复了内存泄漏问题",
            "新增功能: 支持批量导入导出",
            "文档更新: 添加了 API 使用说明",
            "性能优化: 减少了网络请求次数",
            "安全更新: 升级了依赖库版本",
            "代码重构: 抽取了公共组件",
        ]
        
        source_types = ["git", "logbook", "doc", "api"]
        modules = ["src/auth/", "src/db/", "src/api/", "tests/", "docs/"]
        
        for i in range(self.config.docs_per_collection):
            template = content_templates[i % len(content_templates)]
            content = template.format(type=f"类型{i}")
            
            doc = ChunkDoc(
                chunk_id=f"{self.unique_id}:{project_key}:{version}:chunk:{i}",
                content=content,
                project_key=project_key,
                module=modules[i % len(modules)],
                source_type=source_types[i % len(source_types)],
                source_id=f"source-{i % 10}",
                owner_user_id=f"user-{i % 5}",
                commit_ts=f"2024-06-{15 + (i % 15):02d}T10:30:00Z",
                artifact_uri=f"memory://benchmark/{self.unique_id}/{project_key}/{version}/chunk{i}",
                sha256=f"sha256_{i:08x}",
                chunk_idx=i,
                excerpt=f"摘要 {i}",
                metadata={"tag": f"tag{i % 5}", "priority": i % 3},
            )
            docs.append(doc)
        
        return docs
    
    def generate_all_docs(self) -> Dict[str, List[ChunkDoc]]:
        """为所有 collection 生成文档"""
        all_docs = {}
        for collection in self._collections:
            collection_id = collection["collection_id"]
            all_docs[collection_id] = self.generate_docs_for_collection(collection)
        return all_docs
    
    def get_query_samples(self) -> List[str]:
        """获取查询样本"""
        return [
            "用户登录 漏洞修复",
            "数据库 性能优化",
            "单元测试 边界",
            "OAuth2.0 认证",
            "内存泄漏 Bug",
            "批量导入 功能",
            "API 文档",
            "网络请求 优化",
            "安全更新 依赖",
            "代码重构 组件",
        ]


# ============ 基准测试执行器 ============


class BenchmarkRunner:
    """基准测试执行器"""
    
    def __init__(
        self,
        dsn: str,
        schema: str,
        embedding_mock: DeterministicEmbeddingMock,
        unique_id: str,
    ):
        self.dsn = dsn
        self.schema = schema
        self.embedding_mock = embedding_mock
        self.unique_id = unique_id
        self._created_tables: List[str] = []
    
    @contextmanager
    def _timer(self) -> Tuple[float, None]:
        """计时上下文管理器"""
        start = time.perf_counter()
        yield lambda: (time.perf_counter() - start) * 1000  # 返回毫秒
    
    def _ensure_schema(self, conn) -> None:
        """确保 schema 存在"""
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
            conn.commit()
    
    def _get_table_stats(self, conn, schema: str, table: str) -> Dict[str, int]:
        """获取表统计信息"""
        stats = {"rows": 0, "table_size": 0, "index_size": 0}
        try:
            with conn.cursor() as cur:
                # 行数
                cur.execute(f'SELECT COUNT(*) as cnt FROM "{schema}"."{table}"')
                row = cur.fetchone()
                stats["rows"] = row["cnt"] if row else 0
                
                # 表大小
                cur.execute("""
                    SELECT pg_table_size(%s::regclass) as table_size,
                           pg_indexes_size(%s::regclass) as index_size
                """, (f'{schema}.{table}', f'{schema}.{table}'))
                row = cur.fetchone()
                if row:
                    stats["table_size"] = row.get("table_size", 0) or 0
                    stats["index_size"] = row.get("index_size", 0) or 0
        except Exception as e:
            print(f"获取表统计失败 ({schema}.{table}): {e}")
        return stats
    
    def _cleanup_table(self, conn, schema: str, table: str) -> None:
        """清理测试表"""
        try:
            with conn.cursor() as cur:
                cur.execute(f'DROP TABLE IF EXISTS "{schema}"."{table}" CASCADE')
                conn.commit()
        except Exception as e:
            print(f"清理表失败 ({schema}.{table}): {e}")
    
    def run_per_table_benchmark(
        self,
        dataset: DatasetGenerator,
        query_count: int = 50,
    ) -> BenchmarkMetrics:
        """
        运行 per_table 策略基准测试
        
        每个 collection 使用独立的表。
        """
        metrics = BenchmarkMetrics(strategy_name="per_table (DefaultCollectionStrategy)")
        all_docs = dataset.generate_all_docs()
        backends: Dict[str, PGVectorBackend] = {}
        
        try:
            # 创建各 collection 的后端
            with self._timer() as get_elapsed:
                for collection in dataset.collections:
                    collection_id = collection["collection_id"]
                    backend = PGVectorBackend(
                        connection_string=self.dsn,
                        schema=self.schema,
                        collection_id=collection_id,
                        embedding_provider=self.embedding_mock,
                        vector_dim=self.embedding_mock.dim,
                        hybrid_config=HybridSearchConfig(vector_weight=0.7, text_weight=0.3),
                    )
                    backend.initialize()
                    backends[collection_id] = backend
                    self._created_tables.append(backend.table_name)
                metrics.setup_time_ms = get_elapsed()
            
            # 插入数据
            with self._timer() as get_elapsed:
                total_inserted = 0
                for collection_id, docs in all_docs.items():
                    backend = backends[collection_id]
                    result = backend.upsert(docs)
                    total_inserted += result
                metrics.insert_time_ms = get_elapsed()
            
            # 计算插入速度
            if metrics.insert_time_ms > 0:
                metrics.insert_docs_per_sec = (total_inserted / metrics.insert_time_ms) * 1000
            
            # 统计信息（汇总所有表）
            total_rows = 0
            total_table_size = 0
            total_index_size = 0
            
            for collection_id, backend in backends.items():
                conn = backend._get_connection()
                stats = self._get_table_stats(conn, self.schema, backend.table_name)
                total_rows += stats["rows"]
                total_table_size += stats["table_size"]
                total_index_size += stats["index_size"]
            
            metrics.total_rows = total_rows
            metrics.table_size_bytes = total_table_size
            metrics.index_size_bytes = total_index_size
            
            # 查询延迟测试
            queries = dataset.get_query_samples()
            for _ in range(query_count):
                query_text = queries[_ % len(queries)]
                # 随机选择一个 collection 进行查询
                collection_id = dataset.collections[_ % len(dataset.collections)]["collection_id"]
                backend = backends[collection_id]
                
                request = QueryRequest(
                    query_text=query_text,
                    top_k=10,
                    min_score=0.0,
                )
                
                with self._timer() as get_elapsed:
                    results = backend.query(request)
                metrics.query_latencies_ms.append(get_elapsed())
            
        finally:
            # 关闭连接
            for backend in backends.values():
                try:
                    backend.close()
                except Exception:
                    pass
        
        return metrics
    
    def run_single_table_benchmark(
        self,
        dataset: DatasetGenerator,
        query_count: int = 50,
    ) -> BenchmarkMetrics:
        """
        运行 single_table 策略基准测试
        
        所有 collection 共享同一张表，通过 collection_id 列隔离。
        """
        metrics = BenchmarkMetrics(strategy_name="single_table (SharedTableStrategy)")
        all_docs = dataset.generate_all_docs()
        
        # 使用共享表名
        shared_table_name = f"benchmark_shared_{self.unique_id[:8]}"
        strategy = SharedTableStrategy(
            collection_id_column="collection_id",
            expected_vector_dim=self.embedding_mock.dim,
        )
        
        backends: Dict[str, PGVectorBackend] = {}
        
        try:
            # 创建各 collection 的后端（共享同一张表）
            with self._timer() as get_elapsed:
                for collection in dataset.collections:
                    collection_id = collection["collection_id"]
                    backend = PGVectorBackend(
                        connection_string=self.dsn,
                        schema=self.schema,
                        table_name=shared_table_name,
                        collection_id=collection_id,
                        embedding_provider=self.embedding_mock,
                        vector_dim=self.embedding_mock.dim,
                        hybrid_config=HybridSearchConfig(vector_weight=0.7, text_weight=0.3),
                        collection_strategy=strategy,
                    )
                    backend.initialize()
                    backends[collection_id] = backend
                
                self._created_tables.append(shared_table_name)
                metrics.setup_time_ms = get_elapsed()
            
            # 插入数据
            with self._timer() as get_elapsed:
                total_inserted = 0
                for collection_id, docs in all_docs.items():
                    backend = backends[collection_id]
                    result = backend.upsert(docs)
                    total_inserted += result
                metrics.insert_time_ms = get_elapsed()
            
            # 计算插入速度
            if metrics.insert_time_ms > 0:
                metrics.insert_docs_per_sec = (total_inserted / metrics.insert_time_ms) * 1000
            
            # 统计信息（单表）
            first_backend = list(backends.values())[0]
            conn = first_backend._get_connection()
            stats = self._get_table_stats(conn, self.schema, shared_table_name)
            
            metrics.total_rows = stats["rows"]
            metrics.table_size_bytes = stats["table_size"]
            metrics.index_size_bytes = stats["index_size"]
            
            # 查询延迟测试
            queries = dataset.get_query_samples()
            for i in range(query_count):
                query_text = queries[i % len(queries)]
                # 随机选择一个 collection 进行查询
                collection_id = dataset.collections[i % len(dataset.collections)]["collection_id"]
                backend = backends[collection_id]
                
                request = QueryRequest(
                    query_text=query_text,
                    top_k=10,
                    min_score=0.0,
                )
                
                with self._timer() as get_elapsed:
                    results = backend.query(request)
                metrics.query_latencies_ms.append(get_elapsed())
            
        finally:
            # 关闭连接
            for backend in backends.values():
                try:
                    backend.close()
                except Exception:
                    pass
        
        return metrics
    
    def cleanup_all_tables(self) -> None:
        """清理所有创建的测试表"""
        import psycopg
        
        try:
            with psycopg.connect(self.dsn, row_factory=psycopg.rows.dict_row) as conn:
                for table_name in self._created_tables:
                    self._cleanup_table(conn, self.schema, table_name)
                self._created_tables.clear()
        except Exception as e:
            print(f"清理测试表失败: {e}")


# ============ pytest 基准测试 ============


@pytest.fixture(scope="module")
def benchmark_unique_id() -> str:
    """生成唯一的基准测试 ID"""
    return f"bench-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def dataset_config() -> DatasetConfig:
    """默认数据集配置"""
    return DatasetConfig(
        num_collections=3,
        num_project_keys=2,
        num_versions_per_project=2,
        docs_per_collection=50,  # pytest 中使用较小数据集
        embedding_dim=128,
    )


@pytest.fixture(scope="module")
def dataset_generator(
    dataset_config: DatasetConfig,
    benchmark_unique_id: str,
) -> DatasetGenerator:
    """创建数据集生成器"""
    return DatasetGenerator(dataset_config, benchmark_unique_id)


@pytest.fixture(scope="module")
def benchmark_runner(
    benchmark_unique_id: str,
    dataset_generator: DatasetGenerator,
):
    """创建基准测试执行器"""
    if not TEST_PGVECTOR_DSN:
        pytest.skip("TEST_PGVECTOR_DSN 环境变量未设置")
    
    runner = BenchmarkRunner(
        dsn=TEST_PGVECTOR_DSN,
        schema=TEST_SCHEMA,
        embedding_mock=dataset_generator.embedding_mock,
        unique_id=benchmark_unique_id,
    )
    
    # 确保 schema 存在
    import psycopg
    with psycopg.connect(TEST_PGVECTOR_DSN, row_factory=psycopg.rows.dict_row) as conn:
        runner._ensure_schema(conn)
    
    yield runner
    
    # 清理
    runner.cleanup_all_tables()


@skip_no_dsn
class TestCollectionStrategyBenchmark:
    """Collection 策略基准测试"""
    
    def test_per_table_strategy_benchmark(
        self,
        benchmark_runner: BenchmarkRunner,
        dataset_generator: DatasetGenerator,
    ):
        """测试 per_table 策略性能"""
        metrics = benchmark_runner.run_per_table_benchmark(
            dataset_generator,
            query_count=20,
        )
        
        print("\n" + "=" * 60)
        print("per_table 策略基准测试结果")
        print("=" * 60)
        print(metrics)
        
        # 基本断言
        assert metrics.total_rows > 0, "应该有数据被插入"
        assert metrics.insert_time_ms > 0, "插入应该花费时间"
        assert len(metrics.query_latencies_ms) > 0, "应该有查询延迟数据"
    
    def test_single_table_strategy_benchmark(
        self,
        benchmark_runner: BenchmarkRunner,
        dataset_generator: DatasetGenerator,
    ):
        """测试 single_table 策略性能"""
        metrics = benchmark_runner.run_single_table_benchmark(
            dataset_generator,
            query_count=20,
        )
        
        print("\n" + "=" * 60)
        print("single_table 策略基准测试结果")
        print("=" * 60)
        print(metrics)
        
        # 基本断言
        assert metrics.total_rows > 0, "应该有数据被插入"
        assert metrics.insert_time_ms > 0, "插入应该花费时间"
        assert len(metrics.query_latencies_ms) > 0, "应该有查询延迟数据"


@skip_no_dsn  
class TestDataIsolationBetweenStrategies:
    """策略间数据隔离测试"""
    
    def test_per_table_collection_isolation(
        self,
        benchmark_runner: BenchmarkRunner,
        dataset_generator: DatasetGenerator,
    ):
        """验证 per_table 策略的 collection 隔离"""
        # 获取两个不同的 collection
        collections = dataset_generator.collections
        if len(collections) < 2:
            pytest.skip("需要至少 2 个 collection 进行隔离测试")
        
        collection_a = collections[0]
        collection_b = collections[1]
        
        # 创建两个后端
        backend_a = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            collection_id=collection_a["collection_id"],
            embedding_provider=dataset_generator.embedding_mock,
            vector_dim=dataset_generator.config.embedding_dim,
        )
        
        backend_b = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            collection_id=collection_b["collection_id"],
            embedding_provider=dataset_generator.embedding_mock,
            vector_dim=dataset_generator.config.embedding_dim,
        )
        
        try:
            backend_a.initialize()
            backend_b.initialize()
            
            # 验证使用不同的表
            assert backend_a.table_name != backend_b.table_name
            
            benchmark_runner._created_tables.append(backend_a.table_name)
            benchmark_runner._created_tables.append(backend_b.table_name)
            
            # 写入数据
            doc_a = ChunkDoc(
                chunk_id=f"{benchmark_runner.unique_id}:isolation:a:0",
                content="Collection A 独有内容",
                project_key=collection_a["project_key"],
                source_type="git",
            )
            backend_a.upsert([doc_a])
            
            doc_b = ChunkDoc(
                chunk_id=f"{benchmark_runner.unique_id}:isolation:b:0",
                content="Collection B 独有内容",
                project_key=collection_b["project_key"],
                source_type="git",
            )
            backend_b.upsert([doc_b])
            
            # 验证隔离
            exists_a = backend_a.exists([doc_a.chunk_id, doc_b.chunk_id])
            assert exists_a[doc_a.chunk_id] is True
            assert exists_a[doc_b.chunk_id] is False
            
            exists_b = backend_b.exists([doc_a.chunk_id, doc_b.chunk_id])
            assert exists_b[doc_a.chunk_id] is False
            assert exists_b[doc_b.chunk_id] is True
            
        finally:
            backend_a.close()
            backend_b.close()
    
    def test_single_table_collection_isolation(
        self,
        benchmark_runner: BenchmarkRunner,
        dataset_generator: DatasetGenerator,
    ):
        """验证 single_table 策略的 collection 隔离"""
        collections = dataset_generator.collections
        if len(collections) < 2:
            pytest.skip("需要至少 2 个 collection 进行隔离测试")
        
        collection_a = collections[0]
        collection_b = collections[1]
        
        shared_table = f"isolation_shared_{benchmark_runner.unique_id[:8]}"
        strategy = SharedTableStrategy(
            collection_id_column="collection_id",
            expected_vector_dim=dataset_generator.config.embedding_dim,
        )
        
        backend_a = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name=shared_table,
            collection_id=collection_a["collection_id"],
            embedding_provider=dataset_generator.embedding_mock,
            vector_dim=dataset_generator.config.embedding_dim,
            collection_strategy=strategy,
        )
        
        backend_b = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name=shared_table,
            collection_id=collection_b["collection_id"],
            embedding_provider=dataset_generator.embedding_mock,
            vector_dim=dataset_generator.config.embedding_dim,
            collection_strategy=strategy,
        )
        
        try:
            backend_a.initialize()
            backend_b.initialize()
            
            # 验证使用相同的表
            assert backend_a.table_name == backend_b.table_name == shared_table
            
            benchmark_runner._created_tables.append(shared_table)
            
            # 写入数据
            doc_a = ChunkDoc(
                chunk_id=f"{benchmark_runner.unique_id}:shared-iso:a:0",
                content="Shared table Collection A 内容",
                project_key=collection_a["project_key"],
                source_type="git",
            )
            backend_a.upsert([doc_a])
            
            doc_b = ChunkDoc(
                chunk_id=f"{benchmark_runner.unique_id}:shared-iso:b:0",
                content="Shared table Collection B 内容",
                project_key=collection_b["project_key"],
                source_type="git",
            )
            backend_b.upsert([doc_b])
            
            # 验证隔离
            exists_a = backend_a.exists([doc_a.chunk_id, doc_b.chunk_id])
            assert exists_a[doc_a.chunk_id] is True
            assert exists_a[doc_b.chunk_id] is False
            
            exists_b = backend_b.exists([doc_a.chunk_id, doc_b.chunk_id])
            assert exists_b[doc_a.chunk_id] is False
            assert exists_b[doc_b.chunk_id] is True
            
        finally:
            backend_a.close()
            backend_b.close()


# ============ 跨策略查询结果对比 ============


@dataclass
class QueryResultComparison:
    """查询结果对比（使用 dual_read_compare 数据结构）"""
    query_text: str
    collection_id: str
    top_k: int
    
    # per_table 结果
    per_table_chunk_ids: List[str] = field(default_factory=list)
    per_table_scores: List[float] = field(default_factory=list)
    per_table_source_types: List[str] = field(default_factory=list)
    per_table_project_keys: List[str] = field(default_factory=list)
    
    # single_table 结果
    single_table_chunk_ids: List[str] = field(default_factory=list)
    single_table_scores: List[float] = field(default_factory=list)
    single_table_source_types: List[str] = field(default_factory=list)
    single_table_project_keys: List[str] = field(default_factory=list)
    
    # dual_read_compare 计算结果（延迟计算）
    _overlap_metrics: Optional[OverlapMetrics] = field(default=None, repr=False)
    _ranking_drift: Optional[RankingDriftMetrics] = field(default=None, repr=False)
    _score_drift: Optional[ScoreDriftMetrics] = field(default=None, repr=False)
    _compare_report: Optional[CompareReport] = field(default=None, repr=False)
    
    def compute_metrics(self, thresholds: Optional[CompareThresholds] = None) -> None:
        """计算所有对比指标"""
        # 计算 overlap 指标
        self._overlap_metrics = compute_overlap_metrics(
            primary_ids=self.per_table_chunk_ids,
            shadow_ids=self.single_table_chunk_ids,
            top_k=self.top_k,
        )
        
        # 计算 ranking drift 指标（使用 id-score 元组）
        per_table_hits = list(zip(self.per_table_chunk_ids, self.per_table_scores))
        single_table_hits = list(zip(self.single_table_chunk_ids, self.single_table_scores))
        self._ranking_drift = compute_ranking_drift(
            primary_ids_ranked=per_table_hits,
            shadow_ids_ranked=single_table_hits,
            stabilize=True,
        )
        
        # 计算 score drift 指标
        self._score_drift = compute_score_drift(
            primary_hits=per_table_hits,
            shadow_hits=single_table_hits,
        )
        
        # 生成完整对比报告
        compare_metrics = CompareMetrics(
            hit_overlap_ratio=self._overlap_metrics.overlap_ratio,
            common_hit_count=self._overlap_metrics.overlap_count,
            primary_hit_count=len(self.per_table_chunk_ids),
            secondary_hit_count=len(self.single_table_chunk_ids),
            avg_score_diff=self._score_drift.avg_abs_score_diff,
            max_score_diff=self._score_drift.max_abs_score_diff,
            p95_score_diff=self._score_drift.p95_abs_score_diff,
            avg_rank_drift=self._ranking_drift.avg_abs_rank_diff,
            max_rank_drift=int(self._ranking_drift.p95_abs_rank_diff),
        )
        
        self._compare_report = evaluate_with_report(
            metrics=compare_metrics,
            thresholds=thresholds or CompareThresholds.from_env(),
            ranking_metrics=self._ranking_drift,
            score_drift_metrics=self._score_drift,
            request_id=f"{self.collection_id}:{self.query_text[:20]}",
            primary_backend="per_table",
            secondary_backend="single_table",
            metadata={"query_text": self.query_text, "top_k": self.top_k},
        )
    
    @property
    def overlap_metrics(self) -> Optional[OverlapMetrics]:
        """获取 overlap 指标"""
        return self._overlap_metrics
    
    @property
    def ranking_drift(self) -> Optional[RankingDriftMetrics]:
        """获取 ranking drift 指标"""
        return self._ranking_drift
    
    @property
    def score_drift(self) -> Optional[ScoreDriftMetrics]:
        """获取 score drift 指标"""
        return self._score_drift
    
    @property
    def compare_report(self) -> Optional[CompareReport]:
        """获取完整对比报告"""
        return self._compare_report
    
    @property
    def overlap_count(self) -> int:
        """重叠的 chunk_id 数量"""
        if self._overlap_metrics:
            return self._overlap_metrics.overlap_count
        set_a = set(self.per_table_chunk_ids)
        set_b = set(self.single_table_chunk_ids)
        return len(set_a & set_b)
    
    @property
    def overlap_ratio(self) -> float:
        """TopK 重叠度 (0.0 ~ 1.0)"""
        if self._overlap_metrics:
            return self._overlap_metrics.overlap_ratio
        total = max(len(self.per_table_chunk_ids), len(self.single_table_chunk_ids))
        if total == 0:
            return 1.0
        return self.overlap_count / total
    
    @property
    def rbo(self) -> float:
        """RBO (Rank-Biased Overlap)"""
        if self._ranking_drift:
            return self._ranking_drift.rbo
        return 0.0
    
    @property
    def source_types_match(self) -> bool:
        """过滤字段（source_type）是否一致"""
        return set(self.per_table_source_types) == set(self.single_table_source_types)
    
    @property
    def project_keys_match(self) -> bool:
        """过滤字段（project_key）是否一致"""
        return set(self.per_table_project_keys) == set(self.single_table_project_keys)


@dataclass 
class CrossCollectionVisibilityTest:
    """跨 collection 可见性测试结果"""
    source_collection: str
    target_collection: str
    query_text: str
    found_source_chunk_ids: List[str] = field(default_factory=list)
    is_isolated: bool = True  # True 表示正确隔离（在 target 中查不到 source 的数据）


class CrossStrategyComparison:
    """跨策略查询结果对比"""
    
    def __init__(
        self,
        dsn: str,
        schema: str,
        dataset: DatasetGenerator,
        unique_id: str,
    ):
        self.dsn = dsn
        self.schema = schema
        self.dataset = dataset
        self.unique_id = unique_id
        self.embedding_mock = dataset.embedding_mock
        
        self._per_table_backends: Dict[str, PGVectorBackend] = {}
        self._single_table_backends: Dict[str, PGVectorBackend] = {}
        self._created_tables: List[str] = []
        
        # 测试结果
        self.query_comparisons: List[QueryResultComparison] = []
        self.isolation_tests: List[CrossCollectionVisibilityTest] = []
    
    def setup(self) -> None:
        """初始化两种策略的后端并插入数据"""
        import psycopg
        
        # 确保 schema 存在
        with psycopg.connect(self.dsn, row_factory=psycopg.rows.dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
                conn.commit()
        
        # 生成数据
        all_docs = self.dataset.generate_all_docs()
        
        # 创建 per_table 后端
        for collection in self.dataset.collections:
            collection_id = collection["collection_id"]
            backend = PGVectorBackend(
                connection_string=self.dsn,
                schema=self.schema,
                collection_id=collection_id,
                embedding_provider=self.embedding_mock,
                vector_dim=self.embedding_mock.dim,
                hybrid_config=HybridSearchConfig(vector_weight=0.7, text_weight=0.3),
            )
            backend.initialize()
            backend.upsert(all_docs[collection_id])
            self._per_table_backends[collection_id] = backend
            self._created_tables.append(backend.table_name)
        
        # 创建 single_table 后端
        shared_table = f"cross_compare_{self.unique_id[:8]}"
        strategy = SharedTableStrategy(
            collection_id_column="collection_id",
            expected_vector_dim=self.embedding_mock.dim,
        )
        
        for collection in self.dataset.collections:
            collection_id = collection["collection_id"]
            backend = PGVectorBackend(
                connection_string=self.dsn,
                schema=self.schema,
                table_name=shared_table,
                collection_id=collection_id,
                embedding_provider=self.embedding_mock,
                vector_dim=self.embedding_mock.dim,
                hybrid_config=HybridSearchConfig(vector_weight=0.7, text_weight=0.3),
                collection_strategy=strategy,
            )
            backend.initialize()
            backend.upsert(all_docs[collection_id])
            self._single_table_backends[collection_id] = backend
        
        self._created_tables.append(shared_table)
    
    def run_query_comparison(
        self,
        top_k: int = 10,
        thresholds: Optional[CompareThresholds] = None,
    ) -> List[QueryResultComparison]:
        """
        运行查询对比测试
        
        Args:
            top_k: TopK 结果数量
            thresholds: 对比阈值配置，None 则使用环境变量或默认值
        
        Returns:
            QueryResultComparison 列表，每个包含完整的对比报告
        """
        self.query_comparisons.clear()
        queries = self.dataset.get_query_samples()
        
        if thresholds is None:
            thresholds = CompareThresholds.from_env()
        
        for collection in self.dataset.collections:
            collection_id = collection["collection_id"]
            per_table_backend = self._per_table_backends[collection_id]
            single_table_backend = self._single_table_backends[collection_id]
            
            for query_text in queries:
                request = QueryRequest(
                    query_text=query_text,
                    top_k=top_k,
                    min_score=0.0,
                )
                
                # per_table 查询
                per_table_results = per_table_backend.query(request)
                
                # single_table 查询
                single_table_results = single_table_backend.query(request)
                
                comparison = QueryResultComparison(
                    query_text=query_text,
                    collection_id=collection_id,
                    top_k=top_k,
                    per_table_chunk_ids=[h.chunk_id for h in per_table_results],
                    per_table_scores=[h.score for h in per_table_results],
                    per_table_source_types=[h.source_type or "" for h in per_table_results],
                    per_table_project_keys=[h.project_key or "" for h in per_table_results],
                    single_table_chunk_ids=[h.chunk_id for h in single_table_results],
                    single_table_scores=[h.score for h in single_table_results],
                    single_table_source_types=[h.source_type or "" for h in single_table_results],
                    single_table_project_keys=[h.project_key or "" for h in single_table_results],
                )
                # 计算对比指标并生成报告
                comparison.compute_metrics(thresholds=thresholds)
                self.query_comparisons.append(comparison)
        
        return self.query_comparisons
    
    def run_isolation_test(self) -> List[CrossCollectionVisibilityTest]:
        """运行跨 collection 隔离测试"""
        self.isolation_tests.clear()
        collections = self.dataset.collections
        
        if len(collections) < 2:
            return self.isolation_tests
        
        queries = self.dataset.get_query_samples()[:3]  # 使用少量查询
        
        # 测试：从 collection A 查询，不应该能看到 collection B 的数据
        for i, source_col in enumerate(collections):
            for j, target_col in enumerate(collections):
                if i == j:
                    continue
                
                source_collection_id = source_col["collection_id"]
                target_collection_id = target_col["collection_id"]
                
                # 获取 source collection 的一些 chunk_ids
                source_docs = self.dataset.generate_docs_for_collection(source_col)
                source_chunk_ids = set(d.chunk_id for d in source_docs[:20])
                
                # 在 target collection 的两种后端中查询
                for query_text in queries:
                    request = QueryRequest(
                        query_text=query_text,
                        top_k=50,  # 大一些以便更全面检查
                        min_score=0.0,
                    )
                    
                    # per_table 策略
                    per_table_target = self._per_table_backends[target_collection_id]
                    per_table_results = per_table_target.query(request)
                    per_table_found = [h.chunk_id for h in per_table_results if h.chunk_id in source_chunk_ids]
                    
                    test_per_table = CrossCollectionVisibilityTest(
                        source_collection=source_collection_id,
                        target_collection=target_collection_id,
                        query_text=f"[per_table] {query_text}",
                        found_source_chunk_ids=per_table_found,
                        is_isolated=len(per_table_found) == 0,
                    )
                    self.isolation_tests.append(test_per_table)
                    
                    # single_table 策略
                    single_table_target = self._single_table_backends[target_collection_id]
                    single_table_results = single_table_target.query(request)
                    single_table_found = [h.chunk_id for h in single_table_results if h.chunk_id in source_chunk_ids]
                    
                    test_single = CrossCollectionVisibilityTest(
                        source_collection=source_collection_id,
                        target_collection=target_collection_id,
                        query_text=f"[single_table] {query_text}",
                        found_source_chunk_ids=single_table_found,
                        is_isolated=len(single_table_found) == 0,
                    )
                    self.isolation_tests.append(test_single)
        
        return self.isolation_tests
    
    def print_diff_report(self) -> None:
        """
        打印差异报告到终端
        
        使用 dual_read_compare.CompareReport 的汇总字段格式化输出。
        """
        print("\n" + "=" * 80)
        print("跨策略查询结果对比报告 (dual_read_compare)")
        print("=" * 80)
        
        # 汇总统计
        total_comparisons = len(self.query_comparisons)
        if total_comparisons == 0:
            print("没有查询对比数据")
            return
        
        # 收集所有报告的指标
        overlap_ratios = []
        rbo_values = []
        p95_score_diffs = []
        p95_rank_diffs = []
        passed_count = 0
        warning_count = 0
        fail_count = 0
        
        for c in self.query_comparisons:
            if c.compare_report and c.compare_report.metrics:
                overlap_ratios.append(c.compare_report.metrics.hit_overlap_ratio)
            else:
                overlap_ratios.append(c.overlap_ratio)
            
            if c.ranking_drift:
                rbo_values.append(c.ranking_drift.rbo)
                p95_rank_diffs.append(c.ranking_drift.p95_abs_rank_diff)
            
            if c.score_drift:
                p95_score_diffs.append(c.score_drift.p95_abs_score_diff)
            
            if c.compare_report and c.compare_report.decision:
                if c.compare_report.decision.passed:
                    if c.compare_report.decision.has_warnings:
                        warning_count += 1
                    else:
                        passed_count += 1
                else:
                    fail_count += 1
        
        avg_overlap = sum(overlap_ratios) / len(overlap_ratios) if overlap_ratios else 0
        avg_rbo = sum(rbo_values) / len(rbo_values) if rbo_values else 0
        avg_p95_score_diff = sum(p95_score_diffs) / len(p95_score_diffs) if p95_score_diffs else 0
        avg_p95_rank_diff = sum(p95_rank_diffs) / len(p95_rank_diffs) if p95_rank_diffs else 0
        perfect_match = sum(1 for r in overlap_ratios if r >= 0.999)
        source_type_mismatches = sum(1 for c in self.query_comparisons if not c.source_types_match)
        project_key_mismatches = sum(1 for c in self.query_comparisons if not c.project_keys_match)
        
        # 打印 CompareReport 汇总字段
        print(f"\n┌{'─'*78}┐")
        print(f"│ {'CompareReport 汇总指标':<76} │")
        print(f"├{'─'*78}┤")
        print(f"│ {'总查询对比数':<33} │ {total_comparisons:<42} │")
        print(f"│ {'通过/警告/失败':<33} │ {passed_count}/{warning_count}/{fail_count:<35} │")
        print(f"├{'─'*78}┤")
        print(f"│ {'平均命中重叠率 (hit_overlap)':<29} │ {avg_overlap:.4f} ({avg_overlap:.2%}){' ':24} │")
        print(f"│ {'完全匹配 (100%)':<32} │ {perfect_match}/{total_comparisons} ({perfect_match/total_comparisons*100:.1f}%){' ':20} │")
        print(f"│ {'平均 RBO':<35} │ {avg_rbo:.4f}{' ':34} │")
        print(f"│ {'平均 P95 分数漂移':<31} │ {avg_p95_score_diff:.4f}{' ':34} │")
        print(f"│ {'平均 P95 排名漂移':<31} │ {avg_p95_rank_diff:.2f}{' ':36} │")
        print(f"├{'─'*78}┤")
        print(f"│ {'source_type 字段不一致':<30} │ {source_type_mismatches:<42} │")
        print(f"│ {'project_key 字段不一致':<30} │ {project_key_mismatches:<42} │")
        print(f"└{'─'*78}┘")
        
        # 显示有违规的查询
        violated_comparisons = [
            c for c in self.query_comparisons
            if c.compare_report and c.compare_report.decision and 
            (not c.compare_report.decision.passed or c.compare_report.decision.has_warnings)
        ]
        
        if violated_comparisons:
            print(f"\n有违规的查询 ({len(violated_comparisons)} 个):")
            print("-" * 80)
            for c in violated_comparisons[:5]:
                report = c.compare_report
                decision = report.decision
                print(f"  Collection: {c.collection_id[:30]}...")
                print(f"  Query: '{c.query_text}'")
                print(f"  Decision: {'PASS+WARN' if decision.passed and decision.has_warnings else 'FAIL'}")
                print(f"  Recommendation: {decision.recommendation}")
                if decision.violation_details:
                    for v in decision.violation_details[:3]:
                        print(f"    [{v.level.upper()}] {v.check_name}: {v.actual_value:.4f} (阈值: {v.threshold_value:.4f})")
                print()
        else:
            print("\n所有查询的对比检查均通过!")
        
        # 显示重叠度最低的几个查询
        sorted_by_overlap = sorted(self.query_comparisons, key=lambda x: x.overlap_ratio)
        low_overlap = [c for c in sorted_by_overlap if c.overlap_ratio < 1.0][:5]
        
        if low_overlap:
            print(f"\n重叠度最低的查询 (非完全匹配):")
            print("-" * 80)
            for c in low_overlap:
                print(f"  Collection: {c.collection_id[:30]}...")
                print(f"  Query: '{c.query_text}'")
                print(f"  重叠度: {c.overlap_ratio:.2%} (重叠 {c.overlap_count}/{c.top_k})")
                print(f"  RBO: {c.rbo:.4f}")
                if c.score_drift:
                    print(f"  P95 分数漂移: {c.score_drift.p95_abs_score_diff:.4f}")
                
                # 显示差异的 chunk_ids
                if c.overlap_metrics:
                    if c.overlap_metrics.primary_only_ids_sample:
                        print(f"  仅 per_table: {c.overlap_metrics.primary_only_ids_sample[:3]}")
                    if c.overlap_metrics.shadow_only_ids_sample:
                        print(f"  仅 single_table: {c.overlap_metrics.shadow_only_ids_sample[:3]}")
                print()
        
        # 隔离测试结果
        print(f"\n{'='*40} 跨 Collection 隔离测试 {'='*40}")
        total_isolation = len(self.isolation_tests)
        passed_isolation = sum(1 for t in self.isolation_tests if t.is_isolated)
        
        print(f"总隔离测试数: {total_isolation}")
        print(f"通过 (正确隔离): {passed_isolation}/{total_isolation}")
        
        failed_tests = [t for t in self.isolation_tests if not t.is_isolated]
        if failed_tests:
            print(f"\n隔离失败的测试 (数据泄漏):")
            print("-" * 80)
            for t in failed_tests[:10]:
                print(f"  Source: {t.source_collection[:30]}...")
                print(f"  Target: {t.target_collection[:30]}...")
                print(f"  Query: {t.query_text}")
                print(f"  泄漏的 chunk_ids: {t.found_source_chunk_ids[:5]}")
                print()
        else:
            print("\n所有跨 collection 隔离测试通过!")
        
        print("=" * 80)
    
    def cleanup(self) -> None:
        """清理资源"""
        import psycopg
        
        # 关闭连接
        for backend in self._per_table_backends.values():
            try:
                backend.close()
            except Exception:
                pass
        
        for backend in self._single_table_backends.values():
            try:
                backend.close()
            except Exception:
                pass
        
        # 清理表
        try:
            with psycopg.connect(self.dsn, row_factory=psycopg.rows.dict_row) as conn:
                for table_name in self._created_tables:
                    try:
                        with conn.cursor() as cur:
                            cur.execute(f'DROP TABLE IF EXISTS "{self.schema}"."{table_name}" CASCADE')
                        conn.commit()
                    except Exception:
                        pass
        except Exception:
            pass


@skip_no_dsn
class TestCrossStrategyQueryComparison:
    """跨策略查询结果对比测试（使用 dual_read_compare）"""
    
    def test_query_results_consistency(
        self,
        benchmark_runner: BenchmarkRunner,
        dataset_generator: DatasetGenerator,
    ):
        """
        测试同一 collection 下 per_table 和 single_table 查询结果一致性
        
        使用 dual_read_compare.CompareReport 生成报告并断言阈值:
        - hit_overlap_ratio >= hit_overlap_min_fail (默认 0.5)
        - rbo >= rbo_min_fail (默认 0.6)
        - p95_score_drift <= score_drift_p95_max (默认 0.1)
        """
        # 使用稍宽松的阈值用于测试（因为是不同策略的对比，不是同后端对比）
        thresholds = CompareThresholds(
            hit_overlap_min_warn=0.7,
            hit_overlap_min_fail=0.5,
            rbo_min_warn=0.7,
            rbo_min_fail=0.5,
            score_drift_p95_max=0.15,
            rank_p95_max_warn=5,
            rank_p95_max_fail=10,
        )
        
        comparison = CrossStrategyComparison(
            dsn=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            dataset=dataset_generator,
            unique_id=benchmark_runner.unique_id,
        )
        
        try:
            comparison.setup()
            comparison.run_query_comparison(top_k=10, thresholds=thresholds)
            comparison.run_isolation_test()
            comparison.print_diff_report()
            
            # ========== 使用 CompareReport 断言阈值 ==========
            
            # 1. 收集所有对比报告
            reports = [c.compare_report for c in comparison.query_comparisons if c.compare_report]
            assert len(reports) > 0, "应该有对比报告生成"
            
            # 2. 统计通过/失败
            failed_reports = [r for r in reports if not r.decision.passed]
            
            # 3. 计算汇总指标
            overlap_ratios = [r.metrics.hit_overlap_ratio for r in reports]
            avg_overlap = sum(overlap_ratios) / len(overlap_ratios)
            
            rbo_values = [
                c.ranking_drift.rbo for c in comparison.query_comparisons
                if c.ranking_drift is not None
            ]
            avg_rbo = sum(rbo_values) / len(rbo_values) if rbo_values else 0
            
            p95_score_diffs = [
                c.score_drift.p95_abs_score_diff for c in comparison.query_comparisons
                if c.score_drift is not None
            ]
            avg_p95_score_diff = sum(p95_score_diffs) / len(p95_score_diffs) if p95_score_diffs else 0
            
            # 4. 断言: 平均重叠度
            assert avg_overlap >= thresholds.hit_overlap_min_fail, \
                f"平均 hit_overlap {avg_overlap:.4f} < fail 阈值 {thresholds.hit_overlap_min_fail}"
            
            # 5. 断言: 平均 RBO
            assert avg_rbo >= thresholds.rbo_min_fail, \
                f"平均 RBO {avg_rbo:.4f} < fail 阈值 {thresholds.rbo_min_fail}"
            
            # 6. 断言: P95 分数漂移（使用平均值的 1.5 倍作为容差）
            # 注：单个查询可能有较大漂移，但平均应在合理范围内
            assert avg_p95_score_diff <= thresholds.score_drift_p95_max * 1.5, \
                f"平均 P95 分数漂移 {avg_p95_score_diff:.4f} > 阈值 {thresholds.score_drift_p95_max * 1.5}"
            
            # 7. 打印失败报告的详情（用于调试）
            if failed_reports:
                print(f"\n警告: 有 {len(failed_reports)}/{len(reports)} 个对比未通过 fail 阈值")
                for r in failed_reports[:3]:
                    print(f"  - {r.request_id}: {r.decision.reason}")
            
            # 8. 隔离测试应全部通过
            for test in comparison.isolation_tests:
                assert test.is_isolated, f"隔离失败: {test.source_collection} -> {test.target_collection}"
            
        finally:
            comparison.cleanup()


# ============ 独立运行入口 ============


def run_full_benchmark():
    """独立运行完整基准测试"""
    if not TEST_PGVECTOR_DSN:
        print("错误: TEST_PGVECTOR_DSN 环境变量未设置")
        print("示例: export TEST_PGVECTOR_DSN=postgresql://postgres:postgres@localhost:5432/engram")
        sys.exit(1)
    
    print("=" * 70)
    print("PGVector Collection 策略基准测试")
    print("=" * 70)
    print(f"数据库: {TEST_PGVECTOR_DSN.split('@')[-1] if '@' in TEST_PGVECTOR_DSN else '***'}")
    print(f"时间: {datetime.now().isoformat()}")
    print()
    
    # 配置（独立运行时使用更大的数据集）
    config = DatasetConfig(
        num_collections=5,
        num_project_keys=3,
        num_versions_per_project=2,
        docs_per_collection=200,
        embedding_dim=128,
    )
    
    unique_id = f"bench-{uuid.uuid4().hex[:8]}"
    dataset = DatasetGenerator(config, unique_id)
    
    print("数据集配置:")
    print(f"  - Collections: {config.num_collections}")
    print(f"  - Project Keys: {config.num_project_keys}")
    print(f"  - Versions per Project: {config.num_versions_per_project}")
    print(f"  - Docs per Collection: {config.docs_per_collection}")
    print(f"  - Total Docs: {config.total_docs}")
    print(f"  - Embedding Dim: {config.embedding_dim}")
    print()
    
    runner = BenchmarkRunner(
        dsn=TEST_PGVECTOR_DSN,
        schema=TEST_SCHEMA,
        embedding_mock=dataset.embedding_mock,
        unique_id=unique_id,
    )
    
    # 确保 schema 存在
    import psycopg
    with psycopg.connect(TEST_PGVECTOR_DSN, row_factory=psycopg.rows.dict_row) as conn:
        runner._ensure_schema(conn)
    
    try:
        # 运行 per_table 基准测试
        print("-" * 70)
        print("运行 per_table 策略基准测试...")
        print("-" * 70)
        per_table_metrics = runner.run_per_table_benchmark(dataset, query_count=100)
        print(per_table_metrics)
        print()
        
        # 运行 single_table 基准测试
        print("-" * 70)
        print("运行 single_table 策略基准测试...")
        print("-" * 70)
        single_table_metrics = runner.run_single_table_benchmark(dataset, query_count=100)
        print(single_table_metrics)
        print()
        
        # 对比报告
        print("=" * 70)
        print("策略对比报告")
        print("=" * 70)
        print(f"{'指标':<30} {'per_table':>18} {'single_table':>18}")
        print("-" * 70)
        
        comparisons = [
            ("设置时间 (ms)", per_table_metrics.setup_time_ms, single_table_metrics.setup_time_ms),
            ("插入时间 (ms)", per_table_metrics.insert_time_ms, single_table_metrics.insert_time_ms),
            ("插入速度 (docs/s)", per_table_metrics.insert_docs_per_sec, single_table_metrics.insert_docs_per_sec),
            ("总行数", per_table_metrics.total_rows, single_table_metrics.total_rows),
            ("表大小 (KB)", per_table_metrics.table_size_bytes / 1024, single_table_metrics.table_size_bytes / 1024),
            ("索引大小 (KB)", per_table_metrics.index_size_bytes / 1024, single_table_metrics.index_size_bytes / 1024),
            ("平均查询延迟 (ms)", per_table_metrics.avg_query_latency_ms, single_table_metrics.avg_query_latency_ms),
            ("P95 查询延迟 (ms)", per_table_metrics.p95_query_latency_ms, single_table_metrics.p95_query_latency_ms),
            ("最小查询延迟 (ms)", per_table_metrics.min_query_latency_ms, single_table_metrics.min_query_latency_ms),
            ("最大查询延迟 (ms)", per_table_metrics.max_query_latency_ms, single_table_metrics.max_query_latency_ms),
        ]
        
        for name, per_table_val, single_table_val in comparisons:
            if isinstance(per_table_val, float):
                print(f"{name:<30} {per_table_val:>18.2f} {single_table_val:>18.2f}")
            else:
                print(f"{name:<30} {per_table_val:>18} {single_table_val:>18}")
        
        print("=" * 70)
        
    finally:
        # 清理
        print("\n清理测试数据...")
        runner.cleanup_all_tables()
        print("完成!")


if __name__ == "__main__":
    run_full_benchmark()
