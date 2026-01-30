"""
uri.py - URI 解析与规范化模块

功能:
- 解析 URI 结构（scheme, path 等）
- 规范化 URI 路径
- 分类 URI 类型（本地、远程、artifacts 等）
- 解析 URI 到本地文件路径
- SCM 制品路径解析与旧版回退

===============================================================================
URI 双轨分类规范
===============================================================================

Logbook 事实层区分两类 URI，用于不同场景：

1. **Artifact Key（逻辑键）**
   - 格式：无 scheme 或 `artifact://` scheme
   - 用途：数据库存储、跨后端引用
   - 示例：
     - `scm/proj_a/1/svn/r100/abc123.diff`（无 scheme，推荐）
     - `artifact://scm/proj_a/1/svn/r100/abc123.diff`（显式 scheme）
   - 特点：
     - 与物理存储位置解耦
     - 后端切换（local → S3）无需修改 DB
     - Logbook 生产环境 **默认使用此格式**

2. **Physical URI（物理地址）**
   - 格式：`file://`、`s3://`、`gs://`、`https://` 等
   - 用途：直接指向物理存储位置
   - 示例：
     - `file:///mnt/nfs/artifacts/proj_a/scm/1/r100.diff`
     - `s3://bucket/engram/proj_a/scm/1/r100.diff`
     - `https://storage.example.com/artifacts/abc123.diff`
   - 特点：
     - 绑定特定存储后端
     - 适用于外部引用、遗留系统集成
     - Logbook **允许作为特例输入**，但不推荐作为默认存储

3. **Evidence URI（逻辑引用）**
   - 格式：`memory://` scheme
   - 用途：evidence_refs_json 中的证据引用
   - 示例：`memory://patch_blobs/git/1:abc123/sha256hash`
   - 特点：纯逻辑标识，不直接对应物理存储

-------------------------------------------------------------------------------
Logbook 生产约定
-------------------------------------------------------------------------------
- **默认**：patch_blobs.uri、attachments.uri 使用 artifact key（无 scheme）
- **特例**：允许 physical uri 输入（如外部 diff URL），工具负责解析
- **迁移**：后端切换时，artifact key 无需修改；physical uri 需通过 migrate 工具更新

-------------------------------------------------------------------------------
需跟随调整的模块
-------------------------------------------------------------------------------
以下模块需要遵循 artifact key 优先的原则：
- **audit**：审计日志中记录 artifact key，便于跨环境追溯
- **gc**：垃圾回收时，将 artifact key 解析为实际后端路径
- **migrate**：迁移工具负责 physical uri → artifact key 的转换
- **cli**：CLI 输入支持两种格式，内部统一为 artifact key 存储

===============================================================================

URI 分类:
    - file://  -> 本地文件（绝对路径）- Physical URI
    - http(s):// -> 远程 HTTP - Physical URI
    - s3://, gs:// -> 云存储 - Physical URI
    - memory:// -> 逻辑引用（evidence URI）
    - artifact:// -> 制品相对路径（显式 scheme）- Artifact Key
    - 无 scheme -> 默认视为 artifacts 相对路径 - Artifact Key（推荐）

SCM 路径规范:
    新版格式: scm/<project_key>/<repo_id>/<source_type>/<rev_or_sha>/<sha256>.<ext>
    旧版格式（只读兼容）:
        SVN: scm/<repo_id>/svn/r<rev>.<ext>
        Git: scm/<repo_id>/git/commits/<sha>.<ext>
    
    ext 支持: diff, diffstat, ministat
"""

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Tuple, Union
from urllib.parse import urlparse


class UriType(Enum):
    """URI 类型枚举"""
    FILE = "file"           # file:// 本地文件（绝对路径）
    HTTP = "http"           # http:// 或 https://
    S3 = "s3"               # s3:// AWS S3
    GS = "gs"               # gs:// Google Cloud Storage
    FTP = "ftp"             # ftp://
    MEMORY = "memory"       # memory:// 内存引用（patch_blobs/attachments 等）
    ARTIFACT = "artifact"   # 无 scheme，视为 artifacts 相对路径
    UNKNOWN = "unknown"     # 无法识别的 scheme


# 已知的远程 scheme
REMOTE_SCHEMES = {"http", "https", "s3", "gs", "ftp"}

# 本地 scheme（可本地解析）
LOCAL_SCHEMES = {"file", "memory", "artifact"}

# ============ URI 双轨分类常量 ============
#
# Artifact Key: 逻辑键，用于 DB 存储，与物理后端解耦
# - 无 scheme（推荐）
# - artifact:// scheme（显式）
ARTIFACT_KEY_SCHEMES = {None, "artifact"}

# Physical URI: 物理地址，直接指向存储位置
# - file://, s3://, gs://, https:// 等
PHYSICAL_URI_SCHEMES = {"file", "s3", "gs", "http", "https", "ftp"}


@dataclass
class PhysicalRef:
    """
    解析后的物理 URI 引用结构
    
    用于 GC 等场景精确匹配物理地址，区分不同存储后端。
    
    字段说明:
    - scheme: URI scheme（s3/gs/file/http/https 等）
    - bucket: 对象存储的 bucket（仅 s3/gs 有值）
    - key: 对象键或文件路径（完整的 path 部分）
    - root: 本地文件系统根路径（仅 file:// 有值）
    - raw: 原始 URI 字符串
    """
    scheme: str                     # URI scheme (s3, gs, file, http, https)
    bucket: Optional[str] = None    # 对象存储 bucket (s3/gs)
    key: str = ""                   # 对象键或路径
    root: Optional[str] = None      # 本地根路径 (file://)
    raw: str = ""                   # 原始 URI
    
    def __repr__(self) -> str:
        if self.bucket:
            return f"PhysicalRef(scheme={self.scheme!r}, bucket={self.bucket!r}, key={self.key!r})"
        elif self.root:
            return f"PhysicalRef(scheme={self.scheme!r}, root={self.root!r}, key={self.key!r})"
        else:
            return f"PhysicalRef(scheme={self.scheme!r}, key={self.key!r})"


def parse_physical_uri(uri: str) -> Optional[PhysicalRef]:
    """
    解析 Physical URI 为结构化的 PhysicalRef
    
    支持的 URI 格式:
    - s3://bucket/key -> PhysicalRef(scheme='s3', bucket='bucket', key='key')
    - gs://bucket/key -> PhysicalRef(scheme='gs', bucket='bucket', key='key')
    - file:///path/to/file -> PhysicalRef(scheme='file', key='/path/to/file')
    - https://host/path -> PhysicalRef(scheme='https', key='/path')
    
    Args:
        uri: Physical URI 字符串
    
    Returns:
        PhysicalRef 结构，如果不是 physical uri 返回 None
    
    示例:
        parse_physical_uri("s3://my-bucket/engram/scm/1.diff")
        # => PhysicalRef(scheme='s3', bucket='my-bucket', key='engram/scm/1.diff')
        
        parse_physical_uri("file:///mnt/artifacts/scm/1.diff")
        # => PhysicalRef(scheme='file', key='/mnt/artifacts/scm/1.diff')
    """
    parsed = parse_uri(uri)
    
    if parsed.scheme not in PHYSICAL_URI_SCHEMES:
        return None
    
    if parsed.scheme in ("s3", "gs"):
        # 对象存储: s3://bucket/key 或 gs://bucket/key
        # parsed.path 已经是 "bucket/key" 格式（由 parse_uri 处理）
        path_parts = parsed.path.split("/", 1)
        bucket = path_parts[0] if path_parts else ""
        key = path_parts[1] if len(path_parts) > 1 else ""
        return PhysicalRef(
            scheme=parsed.scheme,
            bucket=bucket,
            key=key,
            raw=uri,
        )
    
    elif parsed.scheme == "file":
        # 本地文件: file:///path/to/file
        return PhysicalRef(
            scheme=parsed.scheme,
            key=parsed.path,  # 绝对路径
            raw=uri,
        )
    
    else:
        # HTTP/HTTPS/FTP 等
        return PhysicalRef(
            scheme=parsed.scheme,
            key=parsed.path,
            raw=uri,
        )


@dataclass
class ParsedUri:
    """解析后的 URI 结构"""
    raw: str                    # 原始 URI
    scheme: Optional[str]       # scheme (file, http, s3, 等)
    path: str                   # 路径部分
    uri_type: UriType           # URI 类型
    is_remote: bool             # 是否为远程 URI
    is_local: bool              # 是否为本地 URI（file:// 或无 scheme）

    def __repr__(self) -> str:
        return f"ParsedUri(type={self.uri_type.value}, scheme={self.scheme}, path={self.path!r})"


def parse_uri(uri: str) -> ParsedUri:
    """
    解析 URI 结构

    Args:
        uri: URI 字符串

    Returns:
        ParsedUri 结构

    示例:
        parse_uri("file:///path/to/file")
        # => ParsedUri(type=file, scheme=file, path='/path/to/file')

        parse_uri("https://example.com/file")
        # => ParsedUri(type=http, scheme=https, path='/file')

        parse_uri("scm/repo-001/git/file.txt")
        # => ParsedUri(type=artifact, scheme=None, path='scm/repo-001/git/file.txt')
    """
    uri = uri.strip()

    # 尝试解析 URL
    parsed = urlparse(uri)

    # 如果有 scheme
    if parsed.scheme:
        scheme = parsed.scheme.lower()

        if scheme == "file":
            # file:// URI
            # parsed.path 会包含路径
            path = parsed.path
            # Windows 路径处理：file:///C:/path -> C:/path
            if len(path) > 2 and path[0] == "/" and path[2] == ":":
                path = path[1:]
            return ParsedUri(
                raw=uri,
                scheme=scheme,
                path=path,
                uri_type=UriType.FILE,
                is_remote=False,
                is_local=True,
            )

        elif scheme in ("http", "https"):
            return ParsedUri(
                raw=uri,
                scheme=scheme,
                path=parsed.path,
                uri_type=UriType.HTTP,
                is_remote=True,
                is_local=False,
            )

        elif scheme == "s3":
            # s3://bucket/key
            # parsed.path 可能有前导斜杠，需要去除以避免双斜杠
            key_path = parsed.path.lstrip("/")
            path = f"{parsed.netloc}/{key_path}" if key_path else parsed.netloc
            return ParsedUri(
                raw=uri,
                scheme=scheme,
                path=path,
                uri_type=UriType.S3,
                is_remote=True,
                is_local=False,
            )

        elif scheme == "gs":
            # gs://bucket/key
            # parsed.path 可能有前导斜杠，需要去除以避免双斜杠
            key_path = parsed.path.lstrip("/")
            path = f"{parsed.netloc}/{key_path}" if key_path else parsed.netloc
            return ParsedUri(
                raw=uri,
                scheme=scheme,
                path=path,
                uri_type=UriType.GS,
                is_remote=True,
                is_local=False,
            )

        elif scheme == "ftp":
            return ParsedUri(
                raw=uri,
                scheme=scheme,
                path=parsed.path,
                uri_type=UriType.FTP,
                is_remote=True,
                is_local=False,
            )

        elif scheme == "memory":
            # memory:// URI 格式: memory://{resource_type}/{resource_id}[/{extra}]
            # 例如: memory://patch_blobs/git/repo-1:abc123/sha256
            #       memory://attachments/12345/sha256
            # netloc 是 // 后面的第一部分，path 是 / 后面的部分
            if parsed.netloc:
                # memory://netloc/path -> netloc/path
                path = f"{parsed.netloc}{parsed.path}" if parsed.path else parsed.netloc
            else:
                path = parsed.path.lstrip("/")
            return ParsedUri(
                raw=uri,
                scheme=scheme,
                path=path,
                uri_type=UriType.MEMORY,
                is_remote=False,
                is_local=True,
            )

        elif scheme == "artifact":
            # artifact:// URI 格式: artifact://{relative_path}
            # 例如: artifact://scm/repo/test.diff
            #       artifact://attachments/12345.txt
            # 视为本地 artifact 相对路径，去掉 scheme 后按 artifacts_root 解析
            if parsed.netloc:
                # artifact://netloc/path -> netloc/path
                path = f"{parsed.netloc}{parsed.path}" if parsed.path else parsed.netloc
            else:
                path = parsed.path.lstrip("/")
            return ParsedUri(
                raw=uri,
                scheme=scheme,
                path=path,
                uri_type=UriType.ARTIFACT,
                is_remote=False,
                is_local=True,
            )

        else:
            # 未知 scheme
            return ParsedUri(
                raw=uri,
                scheme=scheme,
                path=parsed.path or uri,
                uri_type=UriType.UNKNOWN,
                is_remote=True,  # 保守处理，未知 scheme 视为远程
                is_local=False,
            )

    # 无 scheme：视为 artifact 相对路径
    return ParsedUri(
        raw=uri,
        scheme=None,
        path=uri,
        uri_type=UriType.ARTIFACT,
        is_remote=False,
        is_local=True,
    )


def normalize_uri(path: Union[str, Path]) -> str:
    """
    规范化 URI 路径

    规范化规则:
    - 使用正斜杠 (/) 作为分隔符
    - 移除前导斜杠（制品 URI 为相对路径）
    - 移除冗余的 ./ 和 ../ 路径组件
    - 移除尾部斜杠

    Args:
        path: 文件路径（字符串或 Path 对象）

    Returns:
        规范化的 URI 字符串

    示例:
        normalize_uri("scm\\repo-001\\git\\file.txt")
        # => "scm/repo-001/git/file.txt"

        normalize_uri("./scm/repo-001/../repo-002/file.txt")
        # => "scm/repo-002/file.txt"

        normalize_uri("/absolute/path/file.txt")
        # => "absolute/path/file.txt"
    """
    # 转换为字符串
    path_str = str(path)

    # 统一分隔符为正斜杠
    path_str = path_str.replace("\\", "/")

    # 使用 os.path.normpath 来处理 . 和 ..
    path_str = os.path.normpath(path_str)

    # 再次统一分隔符（Windows 上 normpath 可能返回反斜杠）
    path_str = path_str.replace("\\", "/")

    # 移除前导斜杠（制品 URI 为相对路径）
    path_str = path_str.lstrip("/")

    # 移除尾部斜杠
    path_str = path_str.rstrip("/")

    # 处理空路径
    if not path_str or path_str == ".":
        return ""

    return path_str


def classify_uri(uri: str) -> UriType:
    """
    分类 URI 类型

    Args:
        uri: URI 字符串

    Returns:
        UriType 枚举值

    示例:
        classify_uri("file:///path/to/file")  # => UriType.FILE
        classify_uri("https://example.com")   # => UriType.HTTP
        classify_uri("scm/repo/file.txt")     # => UriType.ARTIFACT
    """
    parsed = parse_uri(uri)
    return parsed.uri_type


def is_remote_uri(uri: str) -> bool:
    """
    检查 URI 是否为远程 URI

    Args:
        uri: URI 字符串

    Returns:
        True 如果是远程 URI
    """
    parsed = parse_uri(uri)
    return parsed.is_remote


def is_local_uri(uri: str) -> bool:
    """
    检查 URI 是否为本地 URI

    本地 URI 包括:
    - file:// scheme
    - artifact:// scheme（显式制品相对路径）
    - 无 scheme（artifact 相对路径）

    Args:
        uri: URI 字符串

    Returns:
        True 如果是本地 URI
    """
    parsed = parse_uri(uri)
    return parsed.is_local


# ============ Artifact Key vs Physical URI 分类函数 ============


def is_artifact_key(uri: str) -> bool:
    """
    检查 URI 是否为 Artifact Key（逻辑键）
    
    Artifact Key 是与物理存储解耦的逻辑标识，用于 DB 存储。
    Logbook 生产环境默认使用此格式。
    
    Artifact Key 包括:
    - 无 scheme（推荐）: `scm/proj_a/1/svn/r100.diff`
    - artifact:// scheme: `artifact://scm/proj_a/1/svn/r100.diff`
    
    Args:
        uri: URI 字符串
    
    Returns:
        True 如果是 artifact key
    
    示例:
        is_artifact_key("scm/1/svn/r100.diff")  # => True
        is_artifact_key("artifact://scm/1/r100.diff")  # => True
        is_artifact_key("s3://bucket/key")  # => False
        is_artifact_key("file:///path/to/file")  # => False
    """
    parsed = parse_uri(uri)
    return parsed.scheme in ARTIFACT_KEY_SCHEMES


def is_physical_uri(uri: str) -> bool:
    """
    检查 URI 是否为 Physical URI（物理地址）
    
    Physical URI 直接指向物理存储位置，绑定特定后端。
    Logbook 允许作为特例输入，但不推荐作为默认存储格式。
    
    Physical URI 包括:
    - file://: 本地文件系统
    - s3://: AWS S3 / MinIO
    - gs://: Google Cloud Storage
    - https://: HTTP(S) 资源
    
    Args:
        uri: URI 字符串
    
    Returns:
        True 如果是 physical uri
    
    示例:
        is_physical_uri("s3://bucket/key")  # => True
        is_physical_uri("file:///path/to/file")  # => True
        is_physical_uri("https://storage.example.com/file")  # => True
        is_physical_uri("scm/1/svn/r100.diff")  # => False
    """
    parsed = parse_uri(uri)
    return parsed.scheme in PHYSICAL_URI_SCHEMES


def normalize_to_artifact_key(uri: str) -> str:
    """
    将 URI 规范化为 artifact key 格式
    
    规范化规则:
    - artifact:// scheme -> 移除 scheme，保留路径
    - 无 scheme -> 规范化路径
    - physical uri -> 抛出 ValueError（不可自动转换）
    
    Args:
        uri: URI 字符串
    
    Returns:
        规范化的 artifact key（无 scheme）
    
    Raises:
        ValueError: 如果 URI 是 physical uri，无法自动转换
    
    示例:
        normalize_to_artifact_key("artifact://scm/1/r100.diff")
        # => "scm/1/r100.diff"
        
        normalize_to_artifact_key("scm/1/r100.diff")
        # => "scm/1/r100.diff"
        
        normalize_to_artifact_key("s3://bucket/key")
        # => ValueError
    """
    parsed = parse_uri(uri)
    
    if parsed.scheme in PHYSICAL_URI_SCHEMES:
        raise ValueError(
            f"无法将 physical uri 自动转换为 artifact key: {uri!r}。"
            f"Physical URI 绑定特定后端，请使用 migrate 工具进行转换。"
        )
    
    # artifact:// 或无 scheme -> 返回规范化路径
    return normalize_uri(parsed.path)


def classify_uri_type(uri: str) -> str:
    """
    分类 URI 为 artifact_key、physical_uri 或 evidence_uri
    
    Args:
        uri: URI 字符串
    
    Returns:
        分类字符串: "artifact_key" | "physical_uri" | "evidence_uri"
    
    示例:
        classify_uri_type("scm/1/r100.diff")  # => "artifact_key"
        classify_uri_type("s3://bucket/key")  # => "physical_uri"
        classify_uri_type("memory://patch_blobs/...")  # => "evidence_uri"
    """
    parsed = parse_uri(uri)
    
    if parsed.scheme == "memory":
        return "evidence_uri"
    elif parsed.scheme in PHYSICAL_URI_SCHEMES:
        return "physical_uri"
    else:
        return "artifact_key"


@dataclass
class UriConversionResult:
    """URI 转换结果"""
    success: bool                    # 是否成功转换
    original_uri: str                # 原始 URI
    converted_uri: Optional[str]     # 转换后的 URI（成功时有值）
    error: Optional[str] = None      # 错误信息（失败时有值）
    uri_type: str = ""               # 原始 URI 类型
    
    def __repr__(self) -> str:
        if self.success:
            return f"UriConversionResult(success=True, {self.original_uri!r} -> {self.converted_uri!r})"
        return f"UriConversionResult(success=False, {self.original_uri!r}, error={self.error!r})"


def try_convert_to_artifact_key(
    uri: str,
    prefix_mappings: Optional[dict] = None,
) -> UriConversionResult:
    """
    尝试将 URI 转换为 artifact key
    
    转换规则:
    1. artifact:// scheme -> 移除 scheme，保留路径（规范化）
    2. 无 scheme（已是 artifact key）-> 直接规范化
    3. file:// 或 s3:// -> 需要通过 prefix_mappings 确定映射关系
       - 如果能匹配到映射，转换为 artifact key
       - 如果无法匹配，返回失败
    
    Args:
        uri: 原始 URI
        prefix_mappings: 物理路径前缀到 artifact key 前缀的映射
            格式: {
                "file://": {"/mnt/artifacts/": ""},  # file:///mnt/artifacts/scm/1.diff -> scm/1.diff
                "s3://": {"bucket/engram/": ""},     # s3://bucket/engram/scm/1.diff -> scm/1.diff
            }
    
    Returns:
        UriConversionResult 结构
    
    示例:
        # artifact:// 转换
        try_convert_to_artifact_key("artifact://scm/1/r100.diff")
        # => UriConversionResult(success=True, "scm/1/r100.diff")
        
        # 已是 artifact key
        try_convert_to_artifact_key("scm/1/r100.diff")
        # => UriConversionResult(success=True, "scm/1/r100.diff")
        
        # file:// 需要映射
        try_convert_to_artifact_key(
            "file:///mnt/artifacts/scm/1/r100.diff",
            prefix_mappings={"file://": {"/mnt/artifacts/": ""}}
        )
        # => UriConversionResult(success=True, "scm/1/r100.diff")
        
        # 无法确定映射
        try_convert_to_artifact_key("s3://unknown-bucket/key")
        # => UriConversionResult(success=False, error="...")
    """
    parsed = parse_uri(uri)
    uri_type = classify_uri_type(uri)
    
    # 1. artifact:// scheme 或无 scheme -> 直接规范化
    if parsed.scheme in ARTIFACT_KEY_SCHEMES:
        normalized = normalize_uri(parsed.path)
        return UriConversionResult(
            success=True,
            original_uri=uri,
            converted_uri=normalized,
            uri_type=uri_type,
        )
    
    # 2. memory:// (evidence uri) -> 不支持转换
    if parsed.scheme == "memory":
        return UriConversionResult(
            success=False,
            original_uri=uri,
            converted_uri=None,
            error="Evidence URI (memory://) 无法转换为 artifact key",
            uri_type=uri_type,
        )
    
    # 3. Physical URI (file://, s3://, gs://, etc.) -> 需要映射
    if parsed.scheme in PHYSICAL_URI_SCHEMES:
        if not prefix_mappings:
            return UriConversionResult(
                success=False,
                original_uri=uri,
                converted_uri=None,
                error=f"Physical URI ({parsed.scheme}://) 需要提供 prefix_mappings 才能转换",
                uri_type=uri_type,
            )
        
        scheme_key = f"{parsed.scheme}://"
        scheme_mappings = prefix_mappings.get(scheme_key, {})
        
        if not scheme_mappings:
            return UriConversionResult(
                success=False,
                original_uri=uri,
                converted_uri=None,
                error=f"未配置 {scheme_key} 的前缀映射",
                uri_type=uri_type,
            )
        
        # 尝试匹配前缀
        for physical_prefix, artifact_prefix in scheme_mappings.items():
            if parsed.path.startswith(physical_prefix):
                # 移除物理前缀，添加 artifact 前缀
                relative_path = parsed.path[len(physical_prefix):]
                artifact_key = artifact_prefix + relative_path
                normalized = normalize_uri(artifact_key)
                return UriConversionResult(
                    success=True,
                    original_uri=uri,
                    converted_uri=normalized,
                    uri_type=uri_type,
                )
        
        # 无法匹配任何前缀
        return UriConversionResult(
            success=False,
            original_uri=uri,
            converted_uri=None,
            error=f"无法匹配 {scheme_key} 的任何前缀映射: {parsed.path!r}",
            uri_type=uri_type,
        )
    
    # 4. 未知 scheme
    return UriConversionResult(
        success=False,
        original_uri=uri,
        converted_uri=None,
        error=f"不支持的 URI scheme: {parsed.scheme}",
        uri_type=uri_type,
    )


def strip_artifact_scheme(uri: str) -> str:
    """
    移除 artifact:// scheme，返回规范化的 artifact key
    
    如果不是 artifact:// scheme 或无 scheme，原样返回规范化后的路径部分。
    此函数不验证是否为 physical uri，仅做 scheme 移除。
    
    Args:
        uri: URI 字符串
    
    Returns:
        规范化的路径（无 scheme）
    
    示例:
        strip_artifact_scheme("artifact://scm/1/r100.diff")
        # => "scm/1/r100.diff"
        
        strip_artifact_scheme("scm/1/r100.diff")
        # => "scm/1/r100.diff"
    """
    parsed = parse_uri(uri)
    return normalize_uri(parsed.path)


def resolve_to_local_path(
    uri: str,
    artifacts_root: Optional[Union[str, Path]] = None,
) -> Optional[str]:
    """
    解析 URI 到本地文件路径

    解析规则:
    - file://path -> 直接使用路径
    - 无 scheme (artifact) -> artifacts_root / normalized_path
    - 远程 URI -> None

    Args:
        uri: URI 字符串
        artifacts_root: 制品根目录（用于解析无 scheme 的 artifact URI）

    Returns:
        本地文件绝对路径，如果不是本地 URI 或文件不存在返回 None

    示例:
        # file:// URI
        resolve_to_local_path("file:///home/user/file.txt")
        # => "/home/user/file.txt" (如果文件存在)

        # artifact URI
        resolve_to_local_path("scm/repo/file.txt", artifacts_root="/data/artifacts")
        # => "/data/artifacts/scm/repo/file.txt" (如果文件存在)

        # 远程 URI
        resolve_to_local_path("https://example.com/file")
        # => None
    """
    parsed = parse_uri(uri)

    if parsed.is_remote:
        return None

    if parsed.uri_type == UriType.FILE:
        # file:// URI
        path = Path(parsed.path)
        if path.exists():
            return str(path.resolve())
        return None

    if parsed.uri_type == UriType.ARTIFACT:
        # artifact:// scheme 或无 scheme：按 artifacts_root 解析
        if not artifacts_root:
            # 如果未提供 artifacts_root，使用统一的配置获取入口
            from . import config
            try:
                artifacts_root = config.get_effective_artifacts_root()
            except Exception:
                artifacts_root = "./.agentx/artifacts"

        root = Path(artifacts_root)
        normalized_path = normalize_uri(parsed.path)
        full_path = root / normalized_path

        if full_path.exists():
            return str(full_path.resolve())
        return None

    return None


def get_uri_path(uri: str) -> str:
    """
    获取 URI 的路径部分

    Args:
        uri: URI 字符串

    Returns:
        路径部分

    示例:
        get_uri_path("file:///path/to/file")  # => "/path/to/file"
        get_uri_path("s3://bucket/key")       # => "bucket/key"
        get_uri_path("scm/repo/file.txt")     # => "scm/repo/file.txt"
    """
    parsed = parse_uri(uri)
    return parsed.path


def build_artifact_uri(*parts: str) -> str:
    """
    构建规范化的 artifact URI

    Args:
        *parts: 路径组件

    Returns:
        规范化的 URI 字符串

    示例:
        build_artifact_uri("scm", "repo-001", "git", "file.txt")
        # => "scm/repo-001/git/file.txt"
    """
    return normalize_uri("/".join(parts))


# ============ Evidence URI 规范 ============
# 
# Canonical Evidence URI 格式:
#   memory://patch_blobs/<source_type>/<source_id>/<sha256>
#
# 用途:
# - analysis.knowledge_candidates.evidence_refs_json 中引用 patch 证据
# - governance.write_audit.evidence_refs_json 中引用审计证据
#
# 与 scm.patch_blobs.uri 的区别:
# - evidence_uri (memory://...) 是逻辑引用，用于 evidence 追溯
# - patch_blobs.uri 是物理存储位置（artifact 路径 / file:// / https:// 等）
#
# 示例:
#   evidence_uri: memory://patch_blobs/git/1:abc123def/e3b0c44298fc...
#   patch_blobs.uri: scm/1/git/commits/abc123def.diff


def build_evidence_uri(source_type: str, source_id: str, sha256: str) -> str:
    """
    构建 canonical evidence URI
    
    格式: memory://patch_blobs/<source_type>/<source_id>/<sha256>
    
    Args:
        source_type: 源类型 ('svn' 或 'git')
        source_id: 源标识符（如 '1:abc123'，即 repo_id:revision/sha）
        sha256: 内容 SHA256 哈希
    
    Returns:
        规范化的 evidence URI
    
    示例:
        build_evidence_uri("git", "1:abc123def", "e3b0c44...")
        # => "memory://patch_blobs/git/1:abc123def/e3b0c44..."
        
        build_evidence_uri("svn", "2:1234", "a1b2c3d4...")
        # => "memory://patch_blobs/svn/2:1234/a1b2c3d4..."
    """
    # 规范化参数
    source_type = source_type.strip().lower()
    source_id = source_id.strip()
    sha256 = sha256.strip().lower()
    
    return f"memory://patch_blobs/{source_type}/{source_id}/{sha256}"


def parse_evidence_uri(evidence_uri: str) -> Optional[dict]:
    """
    解析 evidence URI，提取其中的 source_type、source_id、sha256
    
    Args:
        evidence_uri: evidence URI 字符串
    
    Returns:
        解析结果字典，包含 source_type、source_id、sha256；
        如果不是有效的 evidence URI，返回 None
    
    示例:
        parse_evidence_uri("memory://patch_blobs/git/1:abc123/sha256hash")
        # => {"source_type": "git", "source_id": "1:abc123", "sha256": "sha256hash"}
    """
    parsed = parse_uri(evidence_uri)
    
    if parsed.uri_type != UriType.MEMORY:
        return None
    
    # 路径格式: patch_blobs/<source_type>/<source_id>/<sha256>
    parts = parsed.path.split("/")
    
    if len(parts) < 4 or parts[0] != "patch_blobs":
        return None
    
    return {
        "source_type": parts[1],
        "source_id": parts[2],
        "sha256": parts[3],
    }


def build_evidence_uri_from_patch_blob(
    source_type: str,
    repo_id: int,
    rev_or_sha: str,
    sha256: str,
) -> str:
    """
    从 patch_blob 参数构建 evidence URI（便捷方法）
    
    自动构建 source_id 格式: <repo_id>:<rev_or_sha>
    
    Args:
        source_type: 源类型 ('svn' 或 'git')
        repo_id: 仓库 ID
        rev_or_sha: revision 号（SVN）或 commit SHA（Git）
        sha256: 内容 SHA256 哈希
    
    Returns:
        规范化的 evidence URI
    
    示例:
        build_evidence_uri_from_patch_blob("git", 1, "abc123def", "e3b0c44...")
        # => "memory://patch_blobs/git/1:abc123def/e3b0c44..."
    """
    source_id = f"{repo_id}:{rev_or_sha}"
    return build_evidence_uri(source_type, source_id, sha256)


# ============ Evidence Reference 构建函数 ============
#
# 统一的 evidence_refs_json 结构规范（用于 governance/analysis 模块）:
# {
#     "artifact_uri": "memory://patch_blobs/<source_type>/<source_id>/<sha256>",
#     "sha256": "<content_sha256>",
#     "source_id": "<source_id>",
#     "source_type": "<svn|git>",
#     "kind": "patch",  # 可选，附件类型
#     "size_bytes": <int>  # 可选，内容大小
# }
#
# 此结构用于:
# - analysis.knowledge_candidates.evidence_refs_json
# - governance.write_audit.evidence_refs_json
# - logbook.attachments (kind='patch')


def build_evidence_ref_for_patch_blob(
    source_type: str,
    source_id: str,
    sha256: Optional[str] = None,
    content_sha256: Optional[str] = None,
    size_bytes: Optional[int] = None,
    kind: str = "patch",
    extra: Optional[dict] = None,
) -> dict:
    """
    构建统一的 evidence reference 结构，用于 evidence_refs_json
    
    此函数生成的字典结构可以被同步脚本与治理/分析模块复用，
    确保 evidence_refs_json 结构的一致性。
    
    Args:
        source_type: 源类型 ('svn' 或 'git')
        source_id: 源标识符（格式: <repo_id>:<revision/sha>）
        sha256: 内容 SHA256 哈希
        size_bytes: 可选，内容大小（字节）
        kind: 附件类型，默认 'patch'
        extra: 可选，额外的元数据字段（会合并到结果中）
    
    Returns:
        统一的 evidence reference 字典，包含:
        {
            "artifact_uri": "memory://patch_blobs/<source_type>/<source_id>/<sha256>",
            "sha256": "<sha256>",
            "source_id": "<source_id>",
            "source_type": "<source_type>",
            "kind": "<kind>"
        }
    
    示例:
        build_evidence_ref_for_patch_blob("git", "1:abc123", "e3b0c44...")
        # => {
        #     "artifact_uri": "memory://patch_blobs/git/1:abc123/e3b0c44...",
        #     "sha256": "e3b0c44...",
        #     "source_id": "1:abc123",
        #     "source_type": "git",
        #     "kind": "patch"
        # }
    
    使用场景:
        # 在同步脚本中构建 attachment 元数据
        ref = build_evidence_ref_for_patch_blob("git", source_id, sha256)
        db.attach(item_id=item_id, kind="patch", uri=ref["artifact_uri"], sha256=sha256)
        
        # 在 governance.write_audit 中构建 evidence_refs_json
        ref = build_evidence_ref_for_patch_blob("svn", source_id, sha256)
        insert_write_audit(..., evidence_refs_json={"patches": [ref]})
    """
    # 向后兼容: content_sha256 别名
    if not sha256 and content_sha256:
        sha256 = content_sha256
    if not sha256:
        raise ValueError("sha256 不能为空")

    # 规范化参数
    source_type = source_type.strip().lower()
    source_id = source_id.strip()
    sha256 = str(sha256).strip().lower()
    
    # 构建 canonical artifact_uri
    artifact_uri = build_evidence_uri(source_type, source_id, sha256)
    
    # 构建基础结构
    ref = {
        "artifact_uri": artifact_uri,
        "sha256": sha256,
        "source_id": source_id,
        "source_type": source_type,
        "kind": kind,
    }
    
    # 可选字段
    if size_bytes is not None:
        ref["size_bytes"] = size_bytes
    
    # 合并额外字段
    if extra:
        ref.update(extra)
    
    return ref


def build_evidence_refs_json(
    patches: Optional[list] = None,
    attachments: Optional[list] = None,
    extra: Optional[dict] = None,
) -> dict:
    """
    构建完整的 evidence_refs_json 结构
    
    这是 evidence_refs_json 的标准构建函数，用于 governance/analysis 模块。
    
    Args:
        patches: patch evidence 引用列表（每项由 build_evidence_ref_for_patch_blob 生成）
        attachments: 其他附件引用列表
        extra: 额外的元数据字段
    
    Returns:
        完整的 evidence_refs_json 字典
    
    示例:
        refs = build_evidence_refs_json(
            patches=[
                build_evidence_ref_for_patch_blob("git", "1:abc", "sha1"),
                build_evidence_ref_for_patch_blob("git", "1:def", "sha2"),
            ]
        )
        # => {
        #     "patches": [
        #         {"artifact_uri": "...", "sha256": "sha1", ...},
        #         {"artifact_uri": "...", "sha256": "sha2", ...}
        #     ]
        # }
    """
    result = {}
    
    if patches:
        result["patches"] = patches
    
    if attachments:
        result["attachments"] = attachments
    
    if extra:
        result.update(extra)
    
    return result


# ============ SCM 路径解析与回退 ============
#
# 新版路径格式: scm/<project_key>/<repo_id>/<source_type>/<rev_or_sha>/<sha256>.<ext>
# 旧版路径格式:
#   SVN: scm/<repo_id>/svn/r<rev>.<ext>
#   Git: scm/<repo_id>/git/commits/<sha>.<ext>
#
# 读取时支持自动回退到旧版路径


@dataclass
class ScmArtifactPath:
    """解析后的 SCM 制品路径结构"""
    raw: str                         # 原始路径
    project_key: Optional[str]       # 项目标识（旧版路径为 None）
    repo_id: str                     # 仓库 ID
    source_type: str                 # 源类型 (svn/git/gitlab)
    rev_or_sha: str                  # revision 或 commit SHA
    sha256: Optional[str]            # SHA256（旧版路径为 None）
    ext: str                         # 扩展名 (diff/diffstat/ministat)
    is_legacy: bool                  # 是否为旧版路径格式
    
    def __repr__(self) -> str:
        return (
            f"ScmArtifactPath(project_key={self.project_key!r}, repo_id={self.repo_id!r}, "
            f"source_type={self.source_type!r}, rev_or_sha={self.rev_or_sha!r}, "
            f"is_legacy={self.is_legacy})"
        )


def parse_scm_artifact_path(uri: str) -> Optional[ScmArtifactPath]:
    """
    解析 SCM 制品路径，支持新旧两种格式
    
    新版格式: scm/<project_key>/<repo_id>/<source_type>/<rev_or_sha>/<sha256>.<ext>
    
    rev_or_sha 格式规范:
        - SVN: r<rev> 格式（如 r100、r12345）
        - Git/GitLab: 完整 40 位 SHA 或短 SHA（最少 7 位）
    
    旧版格式（只读兼容）:
        SVN: scm/<repo_id>/svn/r<rev>.<ext>
        Git: scm/<repo_id>/git/commits/<sha>.<ext>
    
    Args:
        uri: SCM 制品 URI 或路径
    
    Returns:
        ScmArtifactPath 结构，无法解析返回 None
        - 新版路径: rev_or_sha 保留原格式（SVN 含 r 前缀）
        - 旧版路径: rev_or_sha 为纯数字（SVN）或纯 SHA（Git）
    
    示例:
        # 新版格式 - SVN（rev_or_sha 含 r 前缀）
        parse_scm_artifact_path("scm/proj_a/1/svn/r100/abc123.diff")
        # => ScmArtifactPath(project_key='proj_a', repo_id='1', rev_or_sha='r100', ...)
        
        # 新版格式 - Git（rev_or_sha 为完整 SHA）
        parse_scm_artifact_path("scm/proj_a/2/git/abc123def.../sha256.diff")
        # => ScmArtifactPath(project_key='proj_a', repo_id='2', rev_or_sha='abc123def...', ...)
        
        # 旧版 SVN 格式（rev_or_sha 为纯数字）
        parse_scm_artifact_path("scm/1/svn/r100.diff")
        # => ScmArtifactPath(project_key=None, repo_id='1', rev_or_sha='100', is_legacy=True, ...)
        
        # 旧版 Git 格式
        parse_scm_artifact_path("scm/1/git/commits/abc123.diff")
        # => ScmArtifactPath(project_key=None, repo_id='1', rev_or_sha='abc123', is_legacy=True, ...)
    """
    # 规范化路径
    path = normalize_uri(uri)
    
    if not path.startswith("scm/"):
        return None
    
    parts = path.split("/")
    
    # 最少需要 scm/<repo_id>/<source_type>/... (4 个部分)
    if len(parts) < 4:
        return None
    
    # 尝试解析新版格式 (6 个部分: scm/project_key/repo_id/source_type/rev_or_sha/filename)
    if len(parts) == 6:
        _, project_key, repo_id, source_type, rev_or_sha, filename = parts
        
        # 解析文件名 (sha256.ext)
        if "." in filename:
            sha256, ext = filename.rsplit(".", 1)
        else:
            sha256, ext = filename, "diff"
        
        if source_type in ("svn", "git", "gitlab"):
            return ScmArtifactPath(
                raw=uri,
                project_key=project_key,
                repo_id=repo_id,
                source_type=source_type,
                rev_or_sha=rev_or_sha,
                sha256=sha256,
                ext=ext,
                is_legacy=False,
            )
    
    # 尝试解析旧版 SVN 格式: scm/<repo_id>/svn/r<rev>.<ext>
    if len(parts) == 4 and parts[2] == "svn":
        _, repo_id, _, filename = parts
        
        # 解析文件名 (r<rev>.<ext>)
        if filename.startswith("r") and "." in filename:
            rev_and_ext = filename[1:]  # 移除 'r' 前缀
            rev, ext = rev_and_ext.rsplit(".", 1)
            
            return ScmArtifactPath(
                raw=uri,
                project_key=None,
                repo_id=repo_id,
                source_type="svn",
                rev_or_sha=rev,
                sha256=None,
                ext=ext,
                is_legacy=True,
            )
    
    # 尝试解析旧版 Git 格式: scm/<repo_id>/git/commits/<sha>.<ext>
    if len(parts) == 5 and parts[2] == "git" and parts[3] == "commits":
        _, repo_id, _, _, filename = parts
        
        # 解析文件名 (<sha>.<ext>)
        if "." in filename:
            sha, ext = filename.rsplit(".", 1)
            
            return ScmArtifactPath(
                raw=uri,
                project_key=None,
                repo_id=repo_id,
                source_type="git",
                rev_or_sha=sha,
                sha256=None,
                ext=ext,
                is_legacy=True,
            )
    
    return None


def resolve_scm_artifact_path(
    project_key: str,
    repo_id: str,
    source_type: str,
    rev_or_sha: str,
    sha256: str,
    ext: str = "diff",
    artifacts_root: Optional[Union[str, Path]] = None,
) -> Optional[str]:
    """
    解析 SCM 制品到本地文件路径，支持新旧路径格式回退
    
    查找顺序:
    1. 新版路径: scm/<project_key>/<repo_id>/<source_type>/<rev_or_sha>/<sha256>.<ext>
    2. 旧版路径（回退）:
       - SVN: scm/<repo_id>/svn/r<rev>.<ext>
       - Git: scm/<repo_id>/git/commits/<sha>.<ext>
    
    Args:
        project_key: 项目标识
        repo_id: 仓库 ID
        source_type: 源类型 (svn/git/gitlab)
        rev_or_sha: 版本标识（格式规范）:
            - SVN: r<rev> 格式（如 "r100"），也支持纯数字（自动补 r 前缀回退查找）
            - Git/GitLab: 完整 40 位 SHA 或短 SHA（最少 7 位）
        sha256: 内容 SHA256 哈希
        ext: 扩展名 (diff/diffstat/ministat)
        artifacts_root: 制品根目录（可选）
    
    Returns:
        本地文件绝对路径，不存在返回 None
    
    示例:
        # SVN: 使用 r<rev> 格式
        path = resolve_scm_artifact_path("proj_a", "1", "svn", "r100", "abc123...", "diff")
        # 优先尝试新版路径 scm/proj_a/1/svn/r100/abc123....diff
        # 若不存在，回退到旧版路径 scm/1/svn/r100.diff
        
        # Git: 使用完整 SHA
        path = resolve_scm_artifact_path("proj_a", "2", "git", "abc123def...", "e3b0c4...", "diff")
        # 优先尝试新版路径 scm/proj_a/2/git/abc123def.../e3b0c4....diff
        # 若不存在，回退到旧版路径 scm/2/git/commits/abc123def....diff
    """
    if not artifacts_root:
        try:
            from . import config
            artifacts_root = config.get_effective_artifacts_root()
        except Exception:
            artifacts_root = "./.agentx/artifacts"
    
    root = Path(artifacts_root)
    
    # 1. 尝试新版路径（rev_or_sha 保持原格式：SVN 含 r 前缀，Git 为纯 SHA）
    new_path = f"scm/{project_key}/{repo_id}/{source_type}/{rev_or_sha}/{sha256}.{ext}"
    new_full_path = root / normalize_uri(new_path)
    if new_full_path.exists():
        return str(new_full_path.resolve())
    
    # 2. 回退到旧版路径
    if source_type == "svn":
        # 旧版 SVN 格式: scm/<repo_id>/svn/r<rev>.<ext>
        # 如果 rev_or_sha 已含 r 前缀（如 "r100"），直接使用
        # 如果是纯数字（如 "100"），补上 r 前缀
        if rev_or_sha.startswith("r"):
            legacy_rev = rev_or_sha  # 已含 r 前缀
        else:
            legacy_rev = f"r{rev_or_sha}"  # 补 r 前缀
        legacy_path = f"scm/{repo_id}/svn/{legacy_rev}.{ext}"
    elif source_type in ("git", "gitlab"):
        # 旧版 Git 格式: scm/<repo_id>/git/commits/<sha>.<ext>
        legacy_path = f"scm/{repo_id}/git/commits/{rev_or_sha}.{ext}"
    else:
        return None
    
    legacy_full_path = root / normalize_uri(legacy_path)
    if legacy_full_path.exists():
        return str(legacy_full_path.resolve())
    
    return None


def validate_evidence_ref(ref: dict) -> tuple:
    """
    验证 evidence reference 结构是否符合规范
    
    Args:
        ref: evidence reference 字典
    
    Returns:
        (is_valid: bool, error_message: Optional[str])
    
    示例:
        valid, error = validate_evidence_ref({"artifact_uri": "...", "sha256": "..."})
        if not valid:
            raise ValidationError(error)
    """
    # 必需字段
    required_fields = ["artifact_uri", "sha256", "source_id"]
    
    for field in required_fields:
        if field not in ref:
            return (False, f"缺少必需字段: {field}")
        if not ref[field]:
            return (False, f"字段不能为空: {field}")
    
    # 验证 sha256 格式 (64 位十六进制)
    sha256 = ref.get("sha256", "")
    if len(sha256) != 64:
        return (False, f"sha256 长度应为 64 位，实际 {len(sha256)} 位")
    
    import re
    if not re.match(r"^[a-f0-9]{64}$", sha256.lower()):
        return (False, "sha256 格式无效（应为 64 位十六进制）")
    
    # 验证 artifact_uri 格式
    artifact_uri = ref.get("artifact_uri", "")
    if not artifact_uri.startswith("memory://"):
        return (False, "artifact_uri 应以 memory:// 开头")
    
    # 验证 source_id 格式 (应包含 : 分隔符)
    source_id = ref.get("source_id", "")
    if ":" not in source_id:
        return (False, "source_id 格式无效（应为 <repo_id>:<rev/sha>）")
    
    return (True, None)


# ============ Attachment Evidence URI 规范 ============
#
# Canonical Attachment Evidence URI 格式:
#   memory://attachments/<attachment_id>/<sha256>
#
# 用途:
# - evidence_refs_json 中引用附件类型的证据
# - 与 patch_blobs 类似，但用于 logbook.attachments 表
#
# 与 logbook.attachments.uri 的区别:
# - evidence_uri (memory://attachments/...) 是逻辑引用，用于 evidence 追溯
# - attachments.uri 是物理存储位置（artifact 路径 / file:// / https:// 等）
#
# 示例:
#   evidence_uri: memory://attachments/12345/e3b0c44298fc...
#   attachments.uri: attachments/item_123/doc.pdf
#
# **重要**：旧格式（三段路径）已废弃，必须拒绝:
#   ~~memory://attachments/<namespace>/<id>/<sha256>~~
#
# ============ 错误码定义 ============
#
# 用于 AttachmentUriParseResult.error_code:
ATTACHMENT_URI_ERR_NOT_MEMORY = "E_NOT_MEMORY"        # 不是 memory:// scheme
ATTACHMENT_URI_ERR_NOT_ATTACHMENTS = "E_NOT_ATTACHMENTS"  # 路径不以 attachments/ 开头
ATTACHMENT_URI_ERR_LEGACY_FORMAT = "E_LEGACY_FORMAT"  # 旧格式（三段路径）已废弃
ATTACHMENT_URI_ERR_INVALID_ID = "E_INVALID_ID"        # attachment_id 非整数
ATTACHMENT_URI_ERR_INVALID_SHA256 = "E_INVALID_SHA256"  # sha256 格式无效
ATTACHMENT_URI_ERR_MALFORMED = "E_MALFORMED"          # 路径格式错误


@dataclass
class AttachmentUriParseResult:
    """
    Attachment Evidence URI 解析结果
    
    提供详细的解析状态和错误信息，便于调试和审计。
    """
    success: bool                           # 是否成功解析
    attachment_id: Optional[int] = None     # 附件 ID
    sha256: Optional[str] = None            # SHA256 哈希
    error_code: Optional[str] = None        # 错误码
    error_message: Optional[str] = None     # 详细错误信息
    raw_uri: str = ""                       # 原始 URI
    
    def to_dict(self) -> dict:
        """转换为字典（成功时）"""
        if not self.success:
            return {}
        return {
            "attachment_id": self.attachment_id,
            "sha256": self.sha256,
        }
    
    def __repr__(self) -> str:
        if self.success:
            return f"AttachmentUriParseResult(success=True, attachment_id={self.attachment_id}, sha256={self.sha256!r})"
        return f"AttachmentUriParseResult(success=False, error_code={self.error_code!r}, error={self.error_message!r})"


def build_attachment_evidence_uri(attachment_id: int, sha256: str) -> str:
    """
    构建 canonical attachment evidence URI
    
    格式: memory://attachments/<attachment_id>/<sha256>
    
    Args:
        attachment_id: 附件 ID（logbook.attachments.attachment_id，必须为整数）
        sha256: 内容 SHA256 哈希（必须为 64 位十六进制字符串）
    
    Returns:
        规范化的 attachment evidence URI
    
    Raises:
        ValueError: 如果 attachment_id 不是整数或 sha256 格式无效
    
    示例:
        build_attachment_evidence_uri(12345, "e3b0c44...")
        # => "memory://attachments/12345/e3b0c44..."
    """
    # 验证 attachment_id
    if not isinstance(attachment_id, int):
        raise ValueError(
            f"attachment_id 必须为整数，got {type(attachment_id).__name__}: {attachment_id!r}"
        )
    
    # 规范化并验证 sha256
    sha256 = sha256.strip().lower()
    import re
    if not re.match(r"^[a-f0-9]{64}$", sha256):
        raise ValueError(
            f"sha256 必须为 64 位十六进制字符串，got: {sha256!r}"
        )
    
    return f"memory://attachments/{attachment_id}/{sha256}"


def parse_attachment_evidence_uri(evidence_uri: str) -> Optional[dict]:
    """
    解析 attachment evidence URI，提取其中的 attachment_id、sha256
    
    Args:
        evidence_uri: attachment evidence URI 字符串
    
    Returns:
        解析结果字典，包含 attachment_id、sha256；
        如果不是有效的 attachment evidence URI，返回 None
    
    注意:
        此函数为向后兼容的简化接口。如需详细错误信息，请使用
        parse_attachment_evidence_uri_strict() 函数。
    
    示例:
        parse_attachment_evidence_uri("memory://attachments/12345/sha256hash")
        # => {"attachment_id": 12345, "sha256": "sha256hash"}
        
        parse_attachment_evidence_uri("memory://patch_blobs/...")
        # => None（不是 attachment URI）
    """
    result = parse_attachment_evidence_uri_strict(evidence_uri)
    return result.to_dict() if result.success else None


def parse_attachment_evidence_uri_strict(evidence_uri: str) -> AttachmentUriParseResult:
    """
    严格解析 attachment evidence URI，提供详细错误信息
    
    规范格式: memory://attachments/<attachment_id>/<sha256>
    - attachment_id: 必须为整数（数据库主键）
    - sha256: 必须为 64 位十六进制字符串
    
    **旧格式已废弃，将被拒绝**:
    - ~~memory://attachments/<namespace>/<id>/<sha256>~~（三段路径格式）
    
    Args:
        evidence_uri: attachment evidence URI 字符串
    
    Returns:
        AttachmentUriParseResult 结构，包含:
        - success: 是否成功解析
        - attachment_id: 附件 ID（成功时有值）
        - sha256: SHA256 哈希（成功时有值）
        - error_code: 错误码（失败时有值）
        - error_message: 详细错误信息（失败时有值）
    
    错误码说明:
        E_NOT_MEMORY: 不是 memory:// scheme
        E_NOT_ATTACHMENTS: 路径不以 attachments/ 开头
        E_LEGACY_FORMAT: 旧格式（三段路径）已废弃
        E_INVALID_ID: attachment_id 非整数
        E_INVALID_SHA256: sha256 格式无效（非 64 位十六进制）
        E_MALFORMED: 路径格式错误
    
    示例:
        # 正确格式
        parse_attachment_evidence_uri_strict("memory://attachments/12345/e3b0c4...64字符")
        # => AttachmentUriParseResult(success=True, attachment_id=12345, sha256="e3b0c4...")
        
        # 旧格式（三段路径）-> 被拒绝
        parse_attachment_evidence_uri_strict("memory://attachments/ns/123/sha256")
        # => AttachmentUriParseResult(success=False, error_code="E_LEGACY_FORMAT", ...)
        
        # 非整数 ID -> 被拒绝
        parse_attachment_evidence_uri_strict("memory://attachments/abc/sha256")
        # => AttachmentUriParseResult(success=False, error_code="E_INVALID_ID", ...)
    """
    import re
    
    raw_uri = evidence_uri
    parsed = parse_uri(evidence_uri)
    
    # 检查 scheme
    if parsed.uri_type != UriType.MEMORY:
        return AttachmentUriParseResult(
            success=False,
            error_code=ATTACHMENT_URI_ERR_NOT_MEMORY,
            error_message=f"URI scheme 必须为 memory://，got: {parsed.scheme!r}",
            raw_uri=raw_uri,
        )
    
    # 解析路径
    path = parsed.path.strip("/")
    parts = path.split("/")
    
    # 检查路径前缀
    if not parts or parts[0] != "attachments":
        return AttachmentUriParseResult(
            success=False,
            error_code=ATTACHMENT_URI_ERR_NOT_ATTACHMENTS,
            error_message=f"路径必须以 attachments/ 开头，got: {path!r}",
            raw_uri=raw_uri,
        )
    
    # 检查路径段数: 必须恰好 3 段 (attachments, attachment_id, sha256)
    if len(parts) < 3:
        return AttachmentUriParseResult(
            success=False,
            error_code=ATTACHMENT_URI_ERR_MALFORMED,
            error_message=(
                f"路径格式错误，期望 attachments/<attachment_id>/<sha256>，"
                f"got: {path!r}"
            ),
            raw_uri=raw_uri,
        )
    
    if len(parts) > 3:
        # 三段以上路径 -> 旧格式（已废弃）
        return AttachmentUriParseResult(
            success=False,
            error_code=ATTACHMENT_URI_ERR_LEGACY_FORMAT,
            error_message=(
                f"旧格式（三段路径）已废弃: {path!r}。"
                f"正确格式为 attachments/<attachment_id>/<sha256>，"
                f"其中 attachment_id 必须为整数（数据库主键）"
            ),
            raw_uri=raw_uri,
        )
    
    # 解析 attachment_id（必须为整数）
    attachment_id_str = parts[1]
    try:
        attachment_id = int(attachment_id_str)
    except ValueError:
        return AttachmentUriParseResult(
            success=False,
            error_code=ATTACHMENT_URI_ERR_INVALID_ID,
            error_message=(
                f"attachment_id 必须为整数（数据库主键），got: {attachment_id_str!r}。"
                f"如果这是旧格式的 namespace，请使用新格式 memory://attachments/<id>/<sha256>"
            ),
            raw_uri=raw_uri,
        )
    
    # 验证 sha256 格式
    sha256 = parts[2].lower()
    if not re.match(r"^[a-f0-9]{64}$", sha256):
        return AttachmentUriParseResult(
            success=False,
            error_code=ATTACHMENT_URI_ERR_INVALID_SHA256,
            error_message=(
                f"sha256 必须为 64 位十六进制字符串，"
                f"got: {parts[2]!r} (长度 {len(parts[2])})"
            ),
            raw_uri=raw_uri,
        )
    
    # 解析成功
    return AttachmentUriParseResult(
        success=True,
        attachment_id=attachment_id,
        sha256=sha256,
        raw_uri=raw_uri,
    )


def build_attachment_evidence_ref(
    attachment_id: int,
    sha256: str,
    kind: str,
    item_id: Optional[int] = None,
    size_bytes: Optional[int] = None,
    extra: Optional[dict] = None,
) -> dict:
    """
    构建 attachment evidence reference 结构
    
    此函数生成的字典结构用于 evidence_refs_json 中的 attachments 数组。
    
    Args:
        attachment_id: 附件 ID
        sha256: 内容 SHA256 哈希
        kind: 附件类型（如 'screenshot', 'document', 'patch' 等）
        item_id: 可选，关联的 logbook item_id
        size_bytes: 可选，内容大小（字节）
        extra: 可选，额外的元数据字段
    
    Returns:
        统一的 attachment evidence reference 字典
    
    示例:
        build_attachment_evidence_ref(12345, "e3b0c44...", "screenshot", item_id=100)
        # => {
        #     "artifact_uri": "memory://attachments/12345/e3b0c44...",
        #     "sha256": "e3b0c44...",
        #     "attachment_id": 12345,
        #     "kind": "screenshot",
        #     "item_id": 100
        # }
    """
    sha256 = sha256.strip().lower()
    artifact_uri = build_attachment_evidence_uri(attachment_id, sha256)
    
    ref = {
        "artifact_uri": artifact_uri,
        "sha256": sha256,
        "attachment_id": attachment_id,
        "kind": kind,
    }
    
    if item_id is not None:
        ref["item_id"] = item_id
    
    if size_bytes is not None:
        ref["size_bytes"] = size_bytes
    
    if extra:
        ref.update(extra)
    
    return ref


def is_patch_blob_evidence_uri(uri: str) -> bool:
    """
    检查 URI 是否为 patch_blobs evidence URI
    
    Args:
        uri: URI 字符串
    
    Returns:
        True 如果是 memory://patch_blobs/... 格式
    """
    parsed = parse_uri(uri)
    if parsed.uri_type != UriType.MEMORY:
        return False
    return parsed.path.startswith("patch_blobs/")


def is_attachment_evidence_uri(uri: str) -> bool:
    """
    检查 URI 是否为 attachment evidence URI
    
    Args:
        uri: URI 字符串
    
    Returns:
        True 如果是 memory://attachments/... 格式
    """
    parsed = parse_uri(uri)
    if parsed.uri_type != UriType.MEMORY:
        return False
    return parsed.path.startswith("attachments/")
