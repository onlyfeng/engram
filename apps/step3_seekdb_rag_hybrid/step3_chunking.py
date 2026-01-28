# step3_chunking.py
# 共用分块模块，可被 indexer 和 query 共同使用
#
# 功能：
# - 统一的 chunking 版本管理
# - 稳定的 chunk_id 生成规则
# - 针对 diff/log/md 的分块策略
# - excerpt 生成策略

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Literal


# ============================================================
# 版本常量
# ============================================================

CHUNKING_VERSION = "v1-2026-01"
"""
分块版本号，用于：
- 标记索引时使用的分块策略
- 当分块逻辑变更时递增版本号触发重建索引
- 格式: v<major>-<year>-<month>
"""


def get_chunking_version(cli_value: str = None) -> str:
    """
    获取运行时的 chunking version
    
    优先级（从高到低）：
    1. CLI 显式参数（cli_value）
    2. 环境变量 STEP3_CHUNKING_VERSION（canonical）
    3. 环境变量 CHUNKING_VERSION（deprecated alias，触发警告）
    4. 模块常量 CHUNKING_VERSION
    
    Args:
        cli_value: CLI 传入的值（最高优先级）
    
    Returns:
        chunking version 字符串
    
    Example:
        >>> get_chunking_version()  # 从环境变量或常量获取
        'v1-2026-01'
        >>> get_chunking_version("v2-custom")  # CLI 覆盖
        'v2-custom'
    """
    import os
    import sys
    
    # 1. CLI 显式参数优先
    if cli_value is not None and cli_value.strip():
        return cli_value.strip()
    
    # 2. STEP3_CHUNKING_VERSION（canonical）
    canonical_value = os.environ.get("STEP3_CHUNKING_VERSION")
    if canonical_value is not None and canonical_value.strip():
        return canonical_value.strip()
    
    # 3. CHUNKING_VERSION（deprecated alias）
    deprecated_value = os.environ.get("CHUNKING_VERSION")
    if deprecated_value is not None and deprecated_value.strip():
        # 触发废弃警告
        print(
            f"Warning: [DEPRECATION] 环境变量 CHUNKING_VERSION 已废弃，"
            f"请改用 STEP3_CHUNKING_VERSION。此警告仅显示一次。",
            file=sys.stderr
        )
        return deprecated_value.strip()
    
    # 4. fallback 到模块常量
    return CHUNKING_VERSION


# ============================================================
# Chunk ID 规则
# ============================================================

CHUNK_ID_NAMESPACE = "engram"
"""默认命名空间，区分不同系统的 chunk"""


def generate_chunk_id(
    source_type: str,
    source_id: str,
    sha256: str,
    chunk_idx: int,
    namespace: str = CHUNK_ID_NAMESPACE,
    chunking_version: str = CHUNKING_VERSION,
) -> str:
    """
    生成稳定的 chunk_id

    格式: <namespace>:<source_type>:<source_id>:<sha256_prefix>:<chunking_version>:<chunk_idx>

    Args:
        source_type: 来源类型 (svn/git/logbook)
        source_id: 来源标识 (repo_id:rev 或 attachment:id)
        sha256: 内容哈希（取前12位）
        chunk_idx: 分块索引
        namespace: 命名空间（默认 engram）
        chunking_version: 分块版本

    Returns:
        稳定的 chunk_id 字符串

    Examples:
        >>> generate_chunk_id("svn", "1:12345", "abc123def456...", 0)
        'engram:svn:1:12345:abc123def456:v1-2026-01:0'
    """
    # 取 sha256 前12位作为简短标识
    sha256_prefix = sha256[:12] if len(sha256) >= 12 else sha256
    # source_id 中的冒号替换为点，避免解析歧义
    safe_source_id = source_id.replace(":", ".")
    return f"{namespace}:{source_type}:{safe_source_id}:{sha256_prefix}:{chunking_version}:{chunk_idx}"


def parse_chunk_id(chunk_id: str) -> Optional[Dict[str, Any]]:
    """
    解析 chunk_id 为各组件

    Args:
        chunk_id: 完整的 chunk_id 字符串

    Returns:
        包含各组件的字典，解析失败返回 None
    """
    parts = chunk_id.split(":")
    if len(parts) != 6:
        return None
    return {
        "namespace": parts[0],
        "source_type": parts[1],
        "source_id": parts[2].replace(".", ":"),  # 还原 source_id
        "sha256_prefix": parts[3],
        "chunking_version": parts[4],
        "chunk_idx": int(parts[5]),
    }


# ============================================================
# Chunk 数据结构
# ============================================================

@dataclass
class ChunkResult:
    """
    分块结果，包含内容和完整元数据

    用于 indexer 写入索引和 query 返回结果

    artifact_uri 与 Step1 evidence_uri 对应关系:
    - 遵循 Step1 uri.py 定义的 Canonical Evidence URI 格式
    - patch_blobs: memory://patch_blobs/<source_type>/<source_id>/<sha256>
    - attachments: memory://attachments/<attachment_id>/<sha256>
    - 此字段可直接用于 evidence_refs_json 中的 artifact_uri
    """
    # 核心字段
    chunk_id: str
    chunk_idx: int
    content: str

    # 必须的可验证字段（遵循 Step1 Evidence URI 规范）
    artifact_uri: str              # Canonical Evidence URI（memory://patch_blobs/... 或 memory://attachments/...）
    sha256: str                    # 原始内容的完整 SHA256 哈希（64 位十六进制）
    source_id: str                 # 来源标识（格式: <repo_id>:<rev/sha> 或 attachment_id）
    source_type: str               # svn/git/logbook

    # 摘要字段（可在 index 阶段预生成或 query 阶段截取）
    excerpt: str = ""              # 内容摘要（前 200 字符或关键行）

    # 扩展元数据
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        return {
            "chunk_id": self.chunk_id,
            "chunk_idx": self.chunk_idx,
            "content": self.content,
            "artifact_uri": self.artifact_uri,
            "sha256": self.sha256,
            "source_id": self.source_id,
            "source_type": self.source_type,
            "excerpt": self.excerpt,
            "metadata": self.metadata,
        }


# ============================================================
# Excerpt 生成策略
# ============================================================

EXCERPT_MAX_LENGTH = 200
"""excerpt 最大长度"""


def generate_excerpt(content: str, content_type: str = "text") -> str:
    """
    生成内容摘要

    策略：
    - index 阶段预生成，避免 query 时计算
    - diff: 提取文件名和首个 hunk 的上下文
    - log: 提取错误行或首行
    - md: 提取标题和首段

    Args:
        content: 原始内容
        content_type: 内容类型 (diff/log/md/text)

    Returns:
        摘要字符串（最多 EXCERPT_MAX_LENGTH 字符）
    """
    if not content:
        return ""

    excerpt = ""

    if content_type == "diff":
        # 提取 diff 文件名和关键行
        lines = content.split("\n")
        key_lines = []
        for line in lines[:20]:  # 只看前20行
            if line.startswith("---") or line.startswith("+++") or line.startswith("@@"):
                key_lines.append(line)
            elif line.startswith("+") or line.startswith("-"):
                if len(key_lines) < 5:
                    key_lines.append(line)
        excerpt = "\n".join(key_lines)

    elif content_type == "log":
        # 提取错误行或首行
        lines = content.split("\n")
        error_lines = [l for l in lines if re.search(r"(error|exception|fail)", l, re.I)]
        if error_lines:
            excerpt = "\n".join(error_lines[:3])
        else:
            excerpt = "\n".join(lines[:3])

    elif content_type == "md":
        # 提取标题和首段
        lines = content.split("\n")
        key_lines = []
        for line in lines:
            if line.startswith("#") or (line.strip() and not key_lines):
                key_lines.append(line)
            if len(key_lines) >= 3:
                break
        excerpt = "\n".join(key_lines)

    else:
        # 默认：取前几行
        excerpt = content[:EXCERPT_MAX_LENGTH]

    # 截断到最大长度
    if len(excerpt) > EXCERPT_MAX_LENGTH:
        excerpt = excerpt[:EXCERPT_MAX_LENGTH - 3] + "..."

    return excerpt


# ============================================================
# artifact_uri 生成策略（遵循 Step1 Canonical Evidence URI 规范）
# ============================================================
#
# Step3 chunk 输出的 artifact_uri 遵循 Step1 uri.py 定义的 canonical 格式：
# - patch_blobs: memory://patch_blobs/<source_type>/<source_id>/<sha256>
# - attachments: memory://attachments/<attachment_id>/<sha256>
#
# 优先级：
# 1. 调用方传入 evidence_uri（已符合 canonical 格式）
# 2. 使用 Step1 build_evidence_uri / build_attachment_evidence_uri 构建
# 3. 后备：根据参数自动构建 canonical URI

def generate_artifact_uri(
    original_uri: str,
    source_type: str,
    source_id: str,
    sha256: str,
    evidence_uri: str = None,
) -> str:
    """
    生成规范化的 artifact_uri（遵循 Step1 Canonical Evidence URI 格式）

    格式规范:
    - patch_blobs: memory://patch_blobs/<source_type>/<source_id>/<sha256>
    - attachments: memory://attachments/<attachment_id>/<sha256>

    优先级:
    1. 如果提供了 evidence_uri，直接使用（假定已符合 canonical 格式）
    2. 如果 original_uri 已是 memory:// 格式且符合 canonical 规范，保持不变
    3. 如果 source_type='logbook' 且 source_id 形如 'attachment:<int>'，构建 attachments URI
    4. 根据参数构建 canonical patch_blobs URI

    Args:
        original_uri: 原始 URI（可能是 file://、artifact:// 或其他）
        source_type: 来源类型 (svn/git/logbook)
        source_id: 来源标识（格式: <repo_id>:<rev/sha> 或 attachment:<id>）
        sha256: 内容哈希（完整的 64 位 SHA256）
        evidence_uri: 可选，调用方预先构建的 evidence URI（优先使用）

    Returns:
        规范化的 Evidence URI（memory://patch_blobs/... 或 memory://attachments/...）

    Examples:
        >>> generate_artifact_uri("file:///data/patch.diff", "svn", "1:100", "abc123...def")
        'memory://patch_blobs/svn/1:100/abc123...def'

        >>> generate_artifact_uri("", "git", "2:abc123", "e3b0c44...", evidence_uri="memory://patch_blobs/git/2:abc123/e3b0c44...")
        'memory://patch_blobs/git/2:abc123/e3b0c44...'

        >>> generate_artifact_uri("", "logbook", "attachment:12345", "e3b0c44...")
        'memory://attachments/12345/e3b0c44...'
    """
    # 优先级 1：调用方传入的 evidence_uri
    if evidence_uri:
        return evidence_uri

    # 优先级 2：已经是 canonical memory:// 格式，保持不变
    if original_uri.startswith("memory://patch_blobs/") or original_uri.startswith("memory://attachments/"):
        return original_uri

    # 优先级 3：检测 attachment 格式的 source_id
    # 如果 source_type='logbook' 且 source_id 形如 'attachment:<int>'，构建 attachments URI
    source_id_norm = source_id.strip()
    sha256_norm = sha256.strip().lower()

    attachment_match = re.match(r"^attachment:(\d+)$", source_id_norm)
    if source_type.strip().lower() == "logbook" and attachment_match:
        attachment_id = attachment_match.group(1)
        return f"memory://attachments/{attachment_id}/{sha256_norm}"

    # 优先级 4：构建 canonical patch_blobs URI
    # 格式: memory://patch_blobs/<source_type>/<source_id>/<sha256>
    # source_id 保持原格式（不再替换冒号）
    source_type_norm = source_type.strip().lower()

    return f"memory://patch_blobs/{source_type_norm}/{source_id_norm}/{sha256_norm}"


def generate_attachment_artifact_uri(
    attachment_id: int,
    sha256: str,
    evidence_uri: str = None,
) -> str:
    """
    生成附件类型的 artifact_uri（遵循 Step1 Canonical Attachment Evidence URI 格式）

    格式: memory://attachments/<attachment_id>/<sha256>

    Args:
        attachment_id: 附件 ID（logbook.attachments.attachment_id）
        sha256: 内容哈希（完整的 64 位 SHA256）
        evidence_uri: 可选，调用方预先构建的 evidence URI（优先使用）

    Returns:
        规范化的 Attachment Evidence URI

    Examples:
        >>> generate_attachment_artifact_uri(12345, "e3b0c44...")
        'memory://attachments/12345/e3b0c44...'
    """
    if evidence_uri:
        return evidence_uri

    sha256_norm = sha256.strip().lower()
    return f"memory://attachments/{attachment_id}/{sha256_norm}"


# ============================================================
# 分块函数
# ============================================================

# --- Diff 分块 ---

def chunk_diff(
    content: str,
    source_type: str,
    source_id: str,
    sha256: str,
    artifact_uri: str,
    max_chunk_size: int = 2000,
    metadata: Optional[Dict[str, Any]] = None,
    evidence_uri: str = None,
) -> List[ChunkResult]:
    """
    对 diff 内容进行分块（按文件 + hunk）

    策略：
    - 按 'diff --git' 或 'Index:' 分割为文件级块
    - 每个文件再按 '@@' 分割为 hunk 级块
    - 保留文件头信息（--- / +++）在每个 hunk 块中

    Args:
        content: diff 原始内容
        source_type: svn 或 git
        source_id: 来源标识
        sha256: 内容哈希
        artifact_uri: 原始 artifact URI（用于后备构建）
        max_chunk_size: 单个 chunk 最大字符数
        metadata: 附加元数据（project_key, repo_id 等）
        evidence_uri: 可选，调用方预先构建的 Canonical Evidence URI（优先使用）

    Returns:
        ChunkResult 列表
    """
    if not content.strip():
        return []

    results: List[ChunkResult] = []
    metadata = metadata or {}
    normalized_uri = generate_artifact_uri(artifact_uri, source_type, source_id, sha256, evidence_uri=evidence_uri)

    # 识别 diff 分隔符
    if "diff --git" in content:
        # Git 格式
        file_pattern = r"(?=^diff --git)"
    elif "Index:" in content:
        # SVN 格式
        file_pattern = r"(?=^Index:)"
    else:
        # 单文件 diff
        file_pattern = None

    if file_pattern:
        file_diffs = re.split(file_pattern, content, flags=re.MULTILINE)
        file_diffs = [f for f in file_diffs if f.strip()]
    else:
        file_diffs = [content]

    chunk_idx = 0
    for file_diff in file_diffs:
        # 提取文件头
        lines = file_diff.split("\n")
        header_lines = []
        body_start = 0
        for i, line in enumerate(lines):
            if line.startswith("@@"):
                body_start = i
                break
            header_lines.append(line)

        file_header = "\n".join(header_lines)

        # 按 hunk 分割
        hunk_pattern = r"(?=^@@)"
        hunks = re.split(hunk_pattern, "\n".join(lines[body_start:]), flags=re.MULTILINE)
        hunks = [h for h in hunks if h.strip()]

        if not hunks:
            # 无 hunk（可能是新增/删除整个文件）
            chunk_content = file_diff
            if len(chunk_content) <= max_chunk_size:
                results.append(ChunkResult(
                    chunk_id=generate_chunk_id(source_type, source_id, sha256, chunk_idx),
                    chunk_idx=chunk_idx,
                    content=chunk_content,
                    artifact_uri=normalized_uri,
                    sha256=sha256,
                    source_id=source_id,
                    source_type=source_type,
                    excerpt=generate_excerpt(chunk_content, "diff"),
                    metadata=metadata,
                ))
                chunk_idx += 1
        else:
            # 每个 hunk 加上文件头作为一个 chunk
            for hunk in hunks:
                chunk_content = file_header + "\n" + hunk if file_header else hunk
                # 如果超长，按行拆分
                if len(chunk_content) > max_chunk_size:
                    sub_chunks = _split_by_size(chunk_content, max_chunk_size)
                    for sub in sub_chunks:
                        results.append(ChunkResult(
                            chunk_id=generate_chunk_id(source_type, source_id, sha256, chunk_idx),
                            chunk_idx=chunk_idx,
                            content=sub,
                            artifact_uri=normalized_uri,
                            sha256=sha256,
                            source_id=source_id,
                            source_type=source_type,
                            excerpt=generate_excerpt(sub, "diff"),
                            metadata=metadata,
                        ))
                        chunk_idx += 1
                else:
                    results.append(ChunkResult(
                        chunk_id=generate_chunk_id(source_type, source_id, sha256, chunk_idx),
                        chunk_idx=chunk_idx,
                        content=chunk_content,
                        artifact_uri=normalized_uri,
                        sha256=sha256,
                        source_id=source_id,
                        source_type=source_type,
                        excerpt=generate_excerpt(chunk_content, "diff"),
                        metadata=metadata,
                    ))
                    chunk_idx += 1

    return results


# --- Log 分块 ---

def chunk_log(
    content: str,
    source_id: str,
    sha256: str,
    artifact_uri: str,
    max_chunk_size: int = 2000,
    time_window_minutes: int = 5,
    metadata: Optional[Dict[str, Any]] = None,
    evidence_uri: str = None,
) -> List[ChunkResult]:
    """
    对日志内容进行分块（按错误段落/时间窗口）

    策略：
    - 优先按 error/exception/traceback 段落分块
    - 无错误时按时间戳窗口分块
    - 无时间戳时按固定行数分块

    Args:
        content: 日志原始内容
        source_id: 来源标识
        sha256: 内容哈希
        artifact_uri: 原始 artifact URI（用于后备构建）
        max_chunk_size: 单个 chunk 最大字符数
        time_window_minutes: 时间窗口（分钟）
        metadata: 附加元数据
        evidence_uri: 可选，调用方预先构建的 Canonical Evidence URI（优先使用）

    Returns:
        ChunkResult 列表
    """
    if not content.strip():
        return []

    results: List[ChunkResult] = []
    metadata = metadata or {}
    source_type = "logbook"
    normalized_uri = generate_artifact_uri(artifact_uri, source_type, source_id, sha256, evidence_uri=evidence_uri)

    # 尝试按错误段落分块
    error_pattern = r"(?i)(error|exception|traceback|fatal|critical)"
    lines = content.split("\n")

    # 查找错误行及上下文
    error_chunks = []
    in_error_block = False
    current_block: List[str] = []
    context_before: List[str] = []

    for i, line in enumerate(lines):
        if re.search(error_pattern, line):
            if not in_error_block:
                in_error_block = True
                # 添加前 3 行上下文
                current_block = context_before[-3:]
            current_block.append(line)
        elif in_error_block:
            # 后 3 行上下文
            current_block.append(line)
            if len(current_block) - len([l for l in current_block if re.search(error_pattern, l)]) >= 3:
                error_chunks.append("\n".join(current_block))
                in_error_block = False
                current_block = []
        else:
            context_before.append(line)
            if len(context_before) > 10:
                context_before.pop(0)

    if current_block:
        error_chunks.append("\n".join(current_block))

    chunk_idx = 0
    if error_chunks:
        # 使用错误段落分块
        for chunk_content in error_chunks:
            if len(chunk_content) > max_chunk_size:
                sub_chunks = _split_by_size(chunk_content, max_chunk_size)
                for sub in sub_chunks:
                    results.append(ChunkResult(
                        chunk_id=generate_chunk_id(source_type, source_id, sha256, chunk_idx),
                        chunk_idx=chunk_idx,
                        content=sub,
                        artifact_uri=normalized_uri,
                        sha256=sha256,
                        source_id=source_id,
                        source_type=source_type,
                        excerpt=generate_excerpt(sub, "log"),
                        metadata=metadata,
                    ))
                    chunk_idx += 1
            else:
                results.append(ChunkResult(
                    chunk_id=generate_chunk_id(source_type, source_id, sha256, chunk_idx),
                    chunk_idx=chunk_idx,
                    content=chunk_content,
                    artifact_uri=normalized_uri,
                    sha256=sha256,
                    source_id=source_id,
                    source_type=source_type,
                    excerpt=generate_excerpt(chunk_content, "log"),
                    metadata=metadata,
                ))
                chunk_idx += 1
    else:
        # 无错误段落，按固定大小分块
        chunks = _split_by_size(content, max_chunk_size)
        for chunk_content in chunks:
            results.append(ChunkResult(
                chunk_id=generate_chunk_id(source_type, source_id, sha256, chunk_idx),
                chunk_idx=chunk_idx,
                content=chunk_content,
                artifact_uri=normalized_uri,
                sha256=sha256,
                source_id=source_id,
                source_type=source_type,
                excerpt=generate_excerpt(chunk_content, "log"),
                metadata=metadata,
            ))
            chunk_idx += 1

    return results


# --- Markdown 分块 ---

def chunk_markdown(
    content: str,
    source_id: str,
    sha256: str,
    artifact_uri: str,
    max_chunk_size: int = 2000,
    heading_levels: List[int] = None,
    metadata: Optional[Dict[str, Any]] = None,
    evidence_uri: str = None,
) -> List[ChunkResult]:
    """
    对 Markdown 内容进行分块（按标题层级）

    策略：
    - 按 h2/h3 标题分割
    - 保留标题层级结构（父标题 + 当前标题）
    - 超长段落按固定大小二次分割

    Args:
        content: Markdown 原始内容
        source_id: 来源标识
        sha256: 内容哈希
        artifact_uri: 原始 artifact URI（用于后备构建）
        max_chunk_size: 单个 chunk 最大字符数
        heading_levels: 分割的标题层级（默认 [2, 3] 即 h2/h3）
        metadata: 附加元数据
        evidence_uri: 可选，调用方预先构建的 Canonical Evidence URI（优先使用）

    Returns:
        ChunkResult 列表
    """
    if not content.strip():
        return []

    if heading_levels is None:
        heading_levels = [2, 3]

    results: List[ChunkResult] = []
    metadata = metadata or {}
    source_type = "logbook"
    normalized_uri = generate_artifact_uri(artifact_uri, source_type, source_id, sha256, evidence_uri=evidence_uri)

    # 构建标题匹配模式
    heading_pattern = r"^(#{" + ",".join(str(l) for l in heading_levels) + r"})\s+(.+)$"

    lines = content.split("\n")
    current_chunk: List[str] = []
    current_heading_stack: List[str] = []  # 保留标题层级

    chunk_idx = 0

    def flush_chunk():
        nonlocal chunk_idx
        if not current_chunk:
            return
        chunk_content = "\n".join(current_chunk)
        if len(chunk_content) > max_chunk_size:
            sub_chunks = _split_by_size(chunk_content, max_chunk_size)
            for sub in sub_chunks:
                results.append(ChunkResult(
                    chunk_id=generate_chunk_id(source_type, source_id, sha256, chunk_idx),
                    chunk_idx=chunk_idx,
                    content=sub,
                    artifact_uri=normalized_uri,
                    sha256=sha256,
                    source_id=source_id,
                    source_type=source_type,
                    excerpt=generate_excerpt(sub, "md"),
                    metadata=metadata,
                ))
                chunk_idx += 1
        else:
            results.append(ChunkResult(
                chunk_id=generate_chunk_id(source_type, source_id, sha256, chunk_idx),
                chunk_idx=chunk_idx,
                content=chunk_content,
                artifact_uri=normalized_uri,
                sha256=sha256,
                source_id=source_id,
                source_type=source_type,
                excerpt=generate_excerpt(chunk_content, "md"),
                metadata=metadata,
            ))
            chunk_idx += 1

    for line in lines:
        heading_match = re.match(r"^(#+)\s+(.+)$", line)
        if heading_match:
            level = len(heading_match.group(1))
            if level in heading_levels:
                # 遇到目标层级标题，flush 当前块
                flush_chunk()
                current_chunk = [line]
                # 更新标题栈
                while current_heading_stack and len(current_heading_stack[-1]) >= level:
                    current_heading_stack.pop()
                current_heading_stack.append("#" * level + " " + heading_match.group(2))
            else:
                current_chunk.append(line)
        else:
            current_chunk.append(line)

    # 处理最后一块
    flush_chunk()

    return results


# ============================================================
# 通用分块入口
# ============================================================

ContentType = Literal["diff", "log", "md", "text"]


def chunk_content(
    content: str,
    content_type: ContentType,
    source_type: str,
    source_id: str,
    sha256: str,
    artifact_uri: str,
    max_chunk_size: int = 2000,
    metadata: Optional[Dict[str, Any]] = None,
    evidence_uri: str = None,
) -> List[ChunkResult]:
    """
    通用分块入口函数

    根据 content_type 调用对应的分块策略

    Args:
        content: 原始内容
        content_type: 内容类型 (diff/log/md/text)
        source_type: 来源类型 (svn/git/logbook)
        source_id: 来源标识
        sha256: 内容哈希
        artifact_uri: 原始 artifact URI（用于后备构建）
        max_chunk_size: 单个 chunk 最大字符数
        metadata: 附加元数据
        evidence_uri: 可选，调用方预先构建的 Canonical Evidence URI（优先使用）

    Returns:
        ChunkResult 列表
    """
    if content_type == "diff":
        return chunk_diff(
            content=content,
            source_type=source_type,
            source_id=source_id,
            sha256=sha256,
            artifact_uri=artifact_uri,
            max_chunk_size=max_chunk_size,
            metadata=metadata,
            evidence_uri=evidence_uri,
        )
    elif content_type == "log":
        return chunk_log(
            content=content,
            source_id=source_id,
            sha256=sha256,
            artifact_uri=artifact_uri,
            max_chunk_size=max_chunk_size,
            metadata=metadata,
            evidence_uri=evidence_uri,
        )
    elif content_type == "md":
        return chunk_markdown(
            content=content,
            source_id=source_id,
            sha256=sha256,
            artifact_uri=artifact_uri,
            max_chunk_size=max_chunk_size,
            metadata=metadata,
            evidence_uri=evidence_uri,
        )
    else:
        # 默认：按固定大小分块
        return _chunk_text(
            content=content,
            source_type=source_type,
            source_id=source_id,
            sha256=sha256,
            artifact_uri=artifact_uri,
            max_chunk_size=max_chunk_size,
            metadata=metadata,
            evidence_uri=evidence_uri,
        )


def _chunk_text(
    content: str,
    source_type: str,
    source_id: str,
    sha256: str,
    artifact_uri: str,
    max_chunk_size: int = 2000,
    metadata: Optional[Dict[str, Any]] = None,
    evidence_uri: str = None,
) -> List[ChunkResult]:
    """纯文本按固定大小分块"""
    if not content.strip():
        return []

    results: List[ChunkResult] = []
    metadata = metadata or {}
    normalized_uri = generate_artifact_uri(artifact_uri, source_type, source_id, sha256, evidence_uri=evidence_uri)

    chunks = _split_by_size(content, max_chunk_size)
    for chunk_idx, chunk_content in enumerate(chunks):
        results.append(ChunkResult(
            chunk_id=generate_chunk_id(source_type, source_id, sha256, chunk_idx),
            chunk_idx=chunk_idx,
            content=chunk_content,
            artifact_uri=normalized_uri,
            sha256=sha256,
            source_id=source_id,
            source_type=source_type,
            excerpt=generate_excerpt(chunk_content, "text"),
            metadata=metadata,
        ))

    return results


# ============================================================
# 辅助函数
# ============================================================

def _split_by_size(content: str, max_size: int) -> List[str]:
    """
    按固定大小分割内容，尽量在行边界分割

    Args:
        content: 原始内容
        max_size: 最大块大小

    Returns:
        分块列表
    """
    if len(content) <= max_size:
        return [content]

    chunks = []
    lines = content.split("\n")
    current_chunk: List[str] = []
    current_size = 0

    for line in lines:
        line_size = len(line) + 1  # +1 for newline
        if current_size + line_size > max_size and current_chunk:
            chunks.append("\n".join(current_chunk))
            current_chunk = [line]
            current_size = line_size
        else:
            current_chunk.append(line)
            current_size += line_size

    if current_chunk:
        chunks.append("\n".join(current_chunk))

    return chunks


def compute_sha256(content: str) -> str:
    """
    计算内容的 SHA256 哈希

    Args:
        content: 原始内容

    Returns:
        SHA256 哈希字符串（小写十六进制）
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ============================================================
# 模块导出
# ============================================================

__all__ = [
    # 版本常量
    "CHUNKING_VERSION",
    "get_chunking_version",
    "CHUNK_ID_NAMESPACE",
    # chunk_id 相关
    "generate_chunk_id",
    "parse_chunk_id",
    # 数据结构
    "ChunkResult",
    # 分块函数
    "chunk_content",
    "chunk_diff",
    "chunk_log",
    "chunk_markdown",
    # 辅助函数
    "generate_excerpt",
    "generate_artifact_uri",
    "generate_attachment_artifact_uri",
    "compute_sha256",
]
