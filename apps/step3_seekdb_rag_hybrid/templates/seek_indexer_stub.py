# seekdb 索引同步脚本（模板）
#
# 实现要点：
# - 读取 Step1（Postgres）logbook.attachments / scm.patch_blobs
# - 拉取原文（根据 uri）
# - chunking（按类型策略）—— 使用 step3_chunking 模块
# - upsert 到索引后端（seekdb/pgvector/其它）
# - 记录游标（logbook.kv）
#
# 注意：本文件是模板，具体 seekdb/pgvector 客户端按你们选型填充。

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# 引入共用 chunking 模块
from step3_seekdb_rag_hybrid.step3_chunking import (
    CHUNKING_VERSION,
    ChunkResult,
    chunk_content,
    chunk_diff,
    chunk_log,
    chunk_markdown,
    generate_chunk_id,
    generate_artifact_uri,
    generate_excerpt,
    compute_sha256,
)


# ============================================================
# Step1 字段清单
# ============================================================

# scm.patch_blobs 完整字段：
#   blob_id        - bigserial PRIMARY KEY
#   source_type    - text ('svn'/'git')
#   source_id      - text (格式: "<repo_id>:<rev>" 或 "<repo_id>:<sha>")
#   uri            - text (artifacts 存储路径)
#   sha256         - text (内容哈希)
#   size_bytes     - bigint
#   format         - text (默认 'diff'，或 'diffstat' for bulk)
#   chunking_version - text (可选，标记已索引版本)
#   created_at     - timestamptz
#
# logbook.attachments 完整字段：
#   attachment_id  - bigserial PRIMARY KEY
#   item_id        - bigint (关联 logbook.items)
#   kind           - text ('patch'/'log'/'report'/'spec' 等)
#   uri            - text
#   sha256         - text
#   size_bytes     - bigint
#   meta_json      - jsonb (可含 project_key, module, owner_user_id 等)
#   created_at     - timestamptz


# ============================================================
# SQL 示例：增量读取 patch_blobs（游标方式）
# ============================================================

SQL_FETCH_PATCH_BLOBS = """
-- 增量读取 scm.patch_blobs，根据游标（last_blob_id）
-- 参数:
--   :last_blob_id - 上次同步的最后 blob_id，首次为 0
--   :batch_size   - 每批获取的最大记录数
SELECT
    pb.blob_id,
    pb.source_type,
    pb.source_id,
    pb.uri,
    pb.sha256,
    pb.size_bytes,
    pb.format,
    pb.chunking_version,
    pb.created_at,
    -- 关联仓库信息以获取 project_key
    r.repo_id,
    r.project_key,
    r.repo_type,
    r.url AS repo_url,
    -- 根据 source_type 关联获取 author/ts/message
    CASE
        WHEN pb.source_type = 'svn' THEN sv.author_raw
        WHEN pb.source_type = 'git' THEN gc.author_raw
    END AS author_raw,
    CASE
        WHEN pb.source_type = 'svn' THEN sv.ts
        WHEN pb.source_type = 'git' THEN gc.ts
    END AS commit_ts,
    CASE
        WHEN pb.source_type = 'svn' THEN sv.message
        WHEN pb.source_type = 'git' THEN gc.message
    END AS commit_message,
    CASE
        WHEN pb.source_type = 'svn' THEN sv.is_bulk
        WHEN pb.source_type = 'git' THEN gc.is_bulk
    END AS is_bulk,
    -- meta_json 包含解析后的 user_id 等信息
    CASE
        WHEN pb.source_type = 'svn' THEN sv.meta_json
        WHEN pb.source_type = 'git' THEN gc.meta_json
    END AS commit_meta_json
FROM scm.patch_blobs pb
JOIN scm.repos r ON r.repo_id = CAST(split_part(pb.source_id, ':', 1) AS bigint)
LEFT JOIN scm.svn_revisions sv
    ON pb.source_type = 'svn'
    AND sv.repo_id = r.repo_id
    AND COALESCE(sv.rev_num, sv.rev_id) = CAST(split_part(pb.source_id, ':', 2) AS bigint)
LEFT JOIN scm.git_commits gc
    ON pb.source_type = 'git'
    AND gc.repo_id = r.repo_id
    AND COALESCE(gc.commit_sha, gc.commit_id) = split_part(pb.source_id, ':', 2)
WHERE pb.blob_id > :last_blob_id
ORDER BY pb.blob_id
LIMIT :batch_size;
"""


SQL_FETCH_ATTACHMENTS = """
-- 增量读取 logbook.attachments，根据游标（last_attachment_id）
-- 参数:
--   :last_attachment_id - 上次同步的最后 attachment_id，首次为 0
--   :batch_size         - 每批获取的最大记录数
SELECT
    a.attachment_id,
    a.item_id,
    a.kind,
    a.uri,
    a.sha256,
    a.size_bytes,
    a.meta_json,
    a.created_at,
    -- 关联 logbook.items 获取上下文
    i.item_type,
    i.title,
    i.scope_json,
    i.owner_user_id,
    i.status
FROM logbook.attachments a
JOIN logbook.items i ON i.item_id = a.item_id
WHERE a.attachment_id > :last_attachment_id
ORDER BY a.attachment_id
LIMIT :batch_size;
"""


# ============================================================
# 数据结构：映射到 Evidence Packet
# ============================================================
# patch_blobs 行可直接映射到 Evidence Packet 的 Evidence 条目：
#
# Evidence Packet 结构（用于 RAG 检索结果）:
# {
#     "query": "...",
#     "evidences": [
#         {
#             "source_type": "svn" | "git",           # <- patch_blobs.source_type
#             "source_id": "1:12345",                 # <- patch_blobs.source_id
#             "uri": "file://artifacts/...",         # <- patch_blobs.uri
#             "sha256": "abc123...",                 # <- patch_blobs.sha256
#             "format": "diff" | "diffstat",         # <- patch_blobs.format
#             "project_key": "my_project",           # <- repos.project_key
#             "repo_url": "svn://...",               # <- repos.url
#             "author": "john.doe",                  # <- svn_revisions/git_commits.author_raw
#             "author_user_id": "user_123",          # <- commit_meta_json.resolved_user_id
#             "commit_ts": "2024-01-15T10:30:00Z",   # <- svn_revisions/git_commits.ts
#             "commit_message": "Fix bug #123",      # <- svn_revisions/git_commits.message
#             "chunk_idx": 0,                        # 分块索引（indexer 生成）
#             "chunk_content": "...",                # 分块内容（indexer 生成）
#             "relevance_score": 0.85                # 检索相似度（RAG 返回）
#         }
#     ],
#     "generated_at": "2024-01-15T12:00:00Z"
# }


@dataclass
class PatchBlobRecord:
    """patch_blobs 记录，包含关联的 commit 信息"""
    blob_id: int
    source_type: str          # 'svn' / 'git'
    source_id: str            # '<repo_id>:<rev>' 或 '<repo_id>:<sha>'
    uri: str
    sha256: str
    size_bytes: Optional[int]
    format: str               # 'diff' / 'diffstat'
    chunking_version: Optional[str]
    created_at: str
    # 关联信息
    repo_id: int
    project_key: str
    repo_type: str
    repo_url: str
    author_raw: Optional[str]
    commit_ts: Optional[str]
    commit_message: Optional[str]
    is_bulk: bool
    commit_meta_json: Optional[Dict[str, Any]]

    def to_evidence(self, chunk_idx: int = 0, chunk_content: str = "", relevance_score: float = 0.0) -> Dict[str, Any]:
        """
        转换为 Evidence Packet 的 Evidence 条目

        Args:
            chunk_idx: 分块索引
            chunk_content: 分块内容
            relevance_score: 检索相似度

        Returns:
            Evidence 字典
        """
        # 从 meta_json 提取 resolved_user_id
        author_user_id = None
        if self.commit_meta_json:
            author_user_id = self.commit_meta_json.get("resolved_user_id")

        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "uri": self.uri,
            "sha256": self.sha256,
            "format": self.format,
            "project_key": self.project_key,
            "repo_url": self.repo_url,
            "author": self.author_raw,
            "author_user_id": author_user_id,
            "commit_ts": self.commit_ts,
            "commit_message": self.commit_message,
            "is_bulk": self.is_bulk,
            "chunk_idx": chunk_idx,
            "chunk_content": chunk_content,
            "relevance_score": relevance_score,
        }


@dataclass
class AttachmentRecord:
    """logbook.attachments 记录，包含关联的 item 信息"""
    attachment_id: int
    item_id: int
    kind: str                 # 'patch'/'log'/'report'/'spec' 等
    uri: str
    sha256: str
    size_bytes: Optional[int]
    meta_json: Optional[Dict[str, Any]]
    created_at: str
    # 关联信息
    item_type: str
    title: str
    scope_json: Optional[Dict[str, Any]]
    owner_user_id: Optional[str]
    status: str

    def to_evidence(self, chunk_idx: int = 0, chunk_content: str = "", relevance_score: float = 0.0) -> Dict[str, Any]:
        """
        转换为 Evidence Packet 的 Evidence 条目

        Returns:
            Evidence 字典
        """
        # 从 scope_json 提取 project_key
        project_key = None
        if self.scope_json:
            project_key = self.scope_json.get("project_key")

        return {
            "source_type": "logbook",
            "source_id": f"attachment:{self.attachment_id}",
            "uri": self.uri,
            "sha256": self.sha256,
            "kind": self.kind,
            "project_key": project_key,
            "item_id": self.item_id,
            "item_type": self.item_type,
            "title": self.title,
            "owner_user_id": self.owner_user_id,
            "chunk_idx": chunk_idx,
            "chunk_content": chunk_content,
            "relevance_score": relevance_score,
        }


# ============================================================
# 游标管理
# ============================================================

KV_NAMESPACE = "seekdb.sync"
KV_KEY_PATCH_BLOBS = "last_blob_id"
KV_KEY_ATTACHMENTS = "last_attachment_id"


def get_sync_cursor(conn, cursor_key: str) -> int:
    """
    从 logbook.kv 获取同步游标

    Args:
        conn: 数据库连接
        cursor_key: 游标键名

    Returns:
        上次同步的 ID，首次返回 0
    """
    # 注意：这是伪代码示例，实际实现需要使用 engram_step1.db.get_kv
    # cursor_data = get_kv(KV_NAMESPACE, cursor_key)
    # return cursor_data.get("last_id", 0) if cursor_data else 0
    raise NotImplementedError("Implement using engram_step1.db.get_kv")


def update_sync_cursor(conn, cursor_key: str, last_id: int) -> None:
    """
    更新 logbook.kv 同步游标

    Args:
        conn: 数据库连接
        cursor_key: 游标键名
        last_id: 最后同步的 ID
    """
    # 注意：这是伪代码示例，实际实现需要使用 engram_step1.db.set_kv
    # from datetime import datetime
    # cursor_data = {
    #     "last_id": last_id,
    #     "updated_at": datetime.utcnow().isoformat() + "Z"
    # }
    # set_kv(KV_NAMESPACE, cursor_key, cursor_data)
    raise NotImplementedError("Implement using engram_step1.db.set_kv")


# ============================================================
# 索引同步主流程（模板）
# ============================================================

def fetch_patch_blobs(conn, last_blob_id: int, batch_size: int = 100) -> List[PatchBlobRecord]:
    """
    从 Step1 读取 patch_blobs 增量数据

    Args:
        conn: 数据库连接
        last_blob_id: 上次同步的最后 blob_id
        batch_size: 每批最大记录数

    Returns:
        PatchBlobRecord 列表
    """
    # 注意：这是模板，实际实现需要执行 SQL_FETCH_PATCH_BLOBS
    # cursor = conn.cursor()
    # cursor.execute(SQL_FETCH_PATCH_BLOBS, {"last_blob_id": last_blob_id, "batch_size": batch_size})
    # return [PatchBlobRecord(**row) for row in cursor.fetchall()]
    raise NotImplementedError("Fill in database query logic")


def fetch_attachments(conn, last_attachment_id: int, batch_size: int = 100) -> List[AttachmentRecord]:
    """
    从 Step1 读取 attachments 增量数据

    Args:
        conn: 数据库连接
        last_attachment_id: 上次同步的最后 attachment_id
        batch_size: 每批最大记录数

    Returns:
        AttachmentRecord 列表
    """
    # 注意：这是模板，实际实现需要执行 SQL_FETCH_ATTACHMENTS
    raise NotImplementedError("Fill in database query logic")


def read_artifact_content(uri: str) -> str:
    """
    根据 URI 读取 artifact 原文内容

    Args:
        uri: artifacts 存储路径 (file:// 或其他协议)

    Returns:
        文件内容
    """
    # 注意：这是模板，实际实现需要根据 uri 协议读取文件
    # if uri.startswith("file://"):
    #     with open(uri[7:], "r", encoding="utf-8") as f:
    #         return f.read()
    raise NotImplementedError("Fill in artifact reading logic")


def do_chunk_content(
    content: str,
    source_type: str,
    source_id: str,
    sha256: str,
    artifact_uri: str,
    format_type: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> List[ChunkResult]:
    """
    对内容进行分块（使用共用 chunking 模块）

    Args:
        content: 原文内容
        source_type: 来源类型 (svn/git/logbook)
        source_id: 来源标识
        sha256: 内容哈希
        artifact_uri: artifact 存储路径
        format_type: 格式类型 (diff/diffstat/log/spec/md)
        metadata: 附加元数据（project_key, repo_id 等）

    Returns:
        ChunkResult 列表

    Examples:
        >>> chunks = do_chunk_content(
        ...     content="diff --git a/foo.py ...",
        ...     source_type="git",
        ...     source_id="1:abc123",
        ...     sha256="deadbeef...",
        ...     artifact_uri="file:///data/patch.diff",
        ...     format_type="diff",
        ...     metadata={"project_key": "myproject"}
        ... )
        >>> print(chunks[0].chunk_id)
        'engram:git:1.abc123:deadbeef...:v1-2026-01:0'
    """
    # 映射 format_type 到 content_type
    content_type_map = {
        "diff": "diff",
        "diffstat": "diff",
        "log": "log",
        "spec": "md",
        "md": "md",
        "report": "text",
    }
    content_type = content_type_map.get(format_type, "text")

    # 调用共用 chunking 模块
    return chunk_content(
        content=content,
        content_type=content_type,
        source_type=source_type,
        source_id=source_id,
        sha256=sha256,
        artifact_uri=artifact_uri,
        metadata=metadata,
    )


def upsert_to_index(chunks: List[ChunkResult]) -> int:
    """
    将分块 upsert 到索引后端

    Args:
        chunks: ChunkResult 列表（由 do_chunk_content 生成）

    Returns:
        成功索引的数量

    Notes:
        每个 ChunkResult 包含：
        - chunk_id: 稳定唯一标识（用于幂等 upsert）
        - content: 分块内容（用于向量化）
        - artifact_uri: 规范化的 memory:// URI
        - sha256: 原始内容哈希（用于验证）
        - source_id: 来源标识
        - source_type: 来源类型
        - excerpt: 内容摘要（用于快速预览）
        - metadata: 扩展元数据（project_key, repo_id 等）
    """
    # 注意：这是模板，具体实现取决于选择的索引后端
    # - seekdb: 使用 seekdb client
    # - pgvector: 使用 pgvector 扩展
    #
    # 示例伪代码：
    # for chunk in chunks:
    #     vector = embedding_model.encode(chunk.content)
    #     index_client.upsert(
    #         id=chunk.chunk_id,
    #         vector=vector,
    #         metadata={
    #             "content": chunk.content,
    #             "artifact_uri": chunk.artifact_uri,
    #             "sha256": chunk.sha256,
    #             "source_id": chunk.source_id,
    #             "source_type": chunk.source_type,
    #             "excerpt": chunk.excerpt,
    #             "chunk_idx": chunk.chunk_idx,
    #             **chunk.metadata,
    #         }
    #     )
    raise NotImplementedError("Fill in seekdb/pgvector client and indexing logic")


def main():
    """
    索引同步主入口

    流程:
    1. 获取游标（last_blob_id, last_attachment_id）
    2. 增量读取 patch_blobs / attachments
    3. 读取原文 + 分块（使用 do_chunk_content）
    4. upsert 到索引后端（使用 upsert_to_index）
    5. 更新游标

    示例伪代码:
    ```
    conn = get_db_connection()
    last_blob_id = get_sync_cursor(conn, KV_KEY_PATCH_BLOBS)

    batch = fetch_patch_blobs(conn, last_blob_id, batch_size=100)
    for record in batch:
        content = read_artifact_content(record.uri)
        # 使用共用 chunking 模块
        chunks = do_chunk_content(
            content=content,
            source_type=record.source_type,
            source_id=record.source_id,
            sha256=record.sha256,
            artifact_uri=record.uri,
            format_type=record.format,
            metadata={
                "project_key": record.project_key,
                "repo_id": record.repo_id,
                "author": record.author_raw,
                "commit_ts": record.commit_ts,
            }
        )
        upsert_to_index(chunks)
        # 更新 patch_blobs.chunking_version
        update_chunking_version(conn, record.blob_id, CHUNKING_VERSION)

    update_sync_cursor(conn, KV_KEY_PATCH_BLOBS, max(r.blob_id for r in batch))
    ```
    """
    raise NotImplementedError(
        "Fill in seekdb/pgvector client and indexing logic.\n"
        "参考本文件中的 SQL 示例和数据结构定义。\n"
        f"当前 CHUNKING_VERSION: {CHUNKING_VERSION}"
    )


if __name__ == "__main__":
    main()
