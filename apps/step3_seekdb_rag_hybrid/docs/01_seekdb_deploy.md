# seekdb 部署（建议：Server 模式）

说明：你们偏好 Windows 内网、非 Docker。seekdb 若强依赖容器时，可选择：
- 内网 Linux VM/物理机部署 seekdb（推荐）
- 或先用"本地 Python 向量库 + Postgres pgvector"替代（过渡）

本 Step3 文档先按 seekdb Server 模式设计，落地时可把"索引后端"替换为 pgvector/其它引擎，接口保持不变。

## 部署输出要求

- 提供 collection/namespace 概念
- 支持 metadata filter
- 支持批量 upsert 与 query

## Collection 版本化命名规则

为支持**并行重建**与**回滚**，collection 采用版本化命名：

```
{namespace}_{chunking_version}_{embedding_model_id}
```

### 示例

| 命名空间 | 分块版本 | Embedding 模型 | Collection 名称 |
|---------|---------|---------------|-----------------|
| engram | v1 | bge_m3 | engram_v1_bge_m3 |
| engram | v2 | bge_m3 | engram_v2_bge_m3 |
| engram | v1 | openai_3s | engram_v1_openai_3s |

### 并行重建流程

```
1. 当前生产使用: engram_v1_bge_m3
2. 后台构建新版本: engram_v2_bge_m3 (不影响生产)
3. 新版本验证通过后，切换生产指向 v2
4. 保留 v1 一段时间用于回滚
5. 确认无问题后删除旧版本
```

### 回滚流程

```
1. 当前生产: engram_v2_bge_m3
2. 发现问题，切换回: engram_v1_bge_m3
3. 修复问题后构建 v3，再切换
```

## Filter DSL 支持

SeekDB 后端支持统一的 Filter DSL 语法：

```python
# 精确匹配
{"project_key": "webapp"}

# 操作符语法
{"project_key": {"$eq": "webapp"}}

# 前缀匹配（module 字段）
{"module": {"$prefix": "src/components/"}}

# 范围查询（commit_ts 字段）
{"commit_ts": {"$gte": "2024-01-01", "$lte": "2024-12-31"}}

# 列表包含
{"source_type": {"$in": ["git", "svn"]}}

# 组合条件
{
    "project_key": "webapp",
    "source_type": "git",
    "module": {"$prefix": "src/"}
}
```

## 使用示例

```python
from index_backend import create_seekdb_backend

# 创建 v1 版本后端
backend = create_seekdb_backend(
    host="localhost",
    port=19530,
    namespace="engram",
    chunking_version="v1",
    embedding_model_id="bge_m3",
)

# 初始化并使用
with backend:
    # 健康检查
    health = backend.health_check()
    
    # 索引文档
    backend.upsert(docs)
    
    # 检索
    from index_backend import QueryRequest
    hits = backend.query(QueryRequest(
        query_text="如何配置数据库连接",
        filters={"project_key": "webapp"},
        top_k=10,
    ))
```

## 环境变量配置

```bash
SEEKDB_HOST=localhost
SEEKDB_PORT=19530
SEEKDB_API_KEY=your_api_key  # 可选
SEEKDB_VECTOR_DIM=1536
```

```python
from index_backend import create_seekdb_backend_from_env

backend = create_seekdb_backend_from_env(
    namespace="engram",
    chunking_version="v1",
    embedding_model_id="bge_m3",
)
```
