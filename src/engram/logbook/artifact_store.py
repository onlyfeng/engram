"""
engram_logbook.artifact_store - 制品存储抽象层

定义 ArtifactStore 接口及多种后端实现：
- LocalArtifactsStore: 基于本地文件系统 (ENGRAM_ARTIFACTS_ROOT)
- FileUriStore: 基于 file:// URI 直接读写
- ObjectStore: 对象存储后端（S3/MinIO 等）

环境变量:
    ENGRAM_ARTIFACTS_ROOT     本地制品根目录
    ENGRAM_ARTIFACTS_BACKEND  后端类型: local | file | object
    ENGRAM_S3_ENDPOINT        S3/MinIO 端点 (ObjectStore)
    ENGRAM_S3_BUCKET          S3 存储桶名称
    ENGRAM_S3_REGION          S3 区域 (可选)
    ENGRAM_S3_VERIFY_SSL      是否验证 SSL 证书 (默认 true)
    ENGRAM_S3_CA_BUNDLE       自定义 CA 证书路径

S3 凭证选择（与 docker-compose.unified.yml 对齐）:
    凭证选择由 ENGRAM_S3_USE_OPS 控制:
    - false (默认): 使用 app 凭证（无 DeleteObject 权限）
    - true: 使用 ops 凭证（有 DeleteObject 权限，GC/迁移用）

    相关环境变量:
        ENGRAM_S3_USE_OPS         是否使用 ops 凭证 (true/false)
        ENGRAM_S3_APP_ACCESS_KEY  App 用户访问密钥
        ENGRAM_S3_APP_SECRET_KEY  App 用户密钥
        ENGRAM_S3_OPS_ACCESS_KEY  Ops 用户访问密钥
        ENGRAM_S3_OPS_SECRET_KEY  Ops 用户密钥
        ENGRAM_S3_ACCESS_KEY      回退访问密钥（兼容旧配置）
        ENGRAM_S3_SECRET_KEY      回退密钥（兼容旧配置）

    优先级:
        1. 构造函数显式传入 access_key/secret_key（最高）
        2. ENGRAM_S3_USE_OPS=true 时: OPS_ACCESS_KEY -> ACCESS_KEY (回退)
        3. ENGRAM_S3_USE_OPS=false 时: APP_ACCESS_KEY -> ACCESS_KEY (回退)
"""

import hashlib
import os
import secrets
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Union
from urllib.parse import urlparse

from .errors import EngramIOError, ExitCode

# =============================================================================
# 错误定义
# =============================================================================


class ArtifactError(EngramIOError):
    """制品操作错误基类"""
    error_type = "ARTIFACT_ERROR"


class ArtifactNotFoundError(ArtifactError):
    """制品不存在"""
    error_type = "ARTIFACT_NOT_FOUND"


class ArtifactWriteDisabledError(ArtifactError):
    """制品写入被禁用（只读模式）"""
    error_type = "ARTIFACT_WRITE_DISABLED"


class ArtifactWriteError(ArtifactError):
    """制品写入失败"""
    error_type = "ARTIFACT_WRITE_ERROR"


class ArtifactReadError(ArtifactError):
    """制品读取失败"""
    error_type = "ARTIFACT_READ_ERROR"


class PathTraversalError(ArtifactError):
    """路径穿越攻击检测"""
    error_type = "PATH_TRAVERSAL_ERROR"


class ArtifactOverwriteDeniedError(ArtifactError):
    """覆盖被拒绝（overwrite_policy=deny 时文件已存在）"""
    error_type = "ARTIFACT_OVERWRITE_DENIED"


class ArtifactHashMismatchError(ArtifactError):
    """哈希不匹配（overwrite_policy=allow_same_hash 时内容不同）"""
    error_type = "ARTIFACT_HASH_MISMATCH"


class ObjectStoreError(ArtifactError):
    """对象存储操作错误"""
    error_type = "OBJECT_STORE_ERROR"


class ObjectStoreNotConfiguredError(ObjectStoreError):
    """对象存储未配置"""
    error_type = "OBJECT_STORE_NOT_CONFIGURED"


class ObjectStoreConnectionError(ObjectStoreError):
    """对象存储连接失败"""
    error_type = "OBJECT_STORE_CONNECTION_ERROR"


class ObjectStoreUploadError(ObjectStoreError):
    """对象存储上传失败"""
    error_type = "OBJECT_STORE_UPLOAD_ERROR"


class ObjectStoreDownloadError(ObjectStoreError):
    """对象存储下载失败"""
    error_type = "OBJECT_STORE_DOWNLOAD_ERROR"


class ArtifactSizeLimitExceededError(ArtifactError):
    """制品大小超出限制"""
    error_type = "ARTIFACT_SIZE_LIMIT_EXCEEDED"


class ObjectStoreTimeoutError(ObjectStoreError):
    """对象存储操作超时"""
    error_type = "OBJECT_STORE_TIMEOUT"


class ObjectStoreThrottlingError(ObjectStoreError):
    """对象存储请求被限流"""
    error_type = "OBJECT_STORE_THROTTLING"


# =============================================================================
# 常量
# =============================================================================

# 文件读取/写入缓冲区大小（64KB）
BUFFER_SIZE = 65536

# Multipart 上传阈值（5MB，S3 最小分片大小）
MULTIPART_THRESHOLD = 5 * 1024 * 1024

# Multipart 分片大小（8MB）
MULTIPART_CHUNK_SIZE = 8 * 1024 * 1024

# 默认对象存储超时配置
DEFAULT_CONNECT_TIMEOUT = 10.0  # 连接超时秒数
DEFAULT_READ_TIMEOUT = 60.0     # 读取超时秒数
DEFAULT_MAX_RETRIES = 3         # 最大重试次数

# 默认最大制品大小限制（0 = 无限制）
DEFAULT_MAX_SIZE_BYTES = 0

# 环境变量
ENV_ARTIFACTS_ROOT = "ENGRAM_ARTIFACTS_ROOT"
ENV_ARTIFACTS_BACKEND = "ENGRAM_ARTIFACTS_BACKEND"
ENV_S3_ENDPOINT = "ENGRAM_S3_ENDPOINT"
ENV_S3_ACCESS_KEY = "ENGRAM_S3_ACCESS_KEY"
ENV_S3_SECRET_KEY = "ENGRAM_S3_SECRET_KEY"
ENV_S3_BUCKET = "ENGRAM_S3_BUCKET"
ENV_S3_REGION = "ENGRAM_S3_REGION"
ENV_S3_VERIFY_SSL = "ENGRAM_S3_VERIFY_SSL"
ENV_S3_CA_BUNDLE = "ENGRAM_S3_CA_BUNDLE"

# 凭证选择相关环境变量（与 docker-compose.unified.yml 对齐）
ENV_S3_USE_OPS = "ENGRAM_S3_USE_OPS"           # 是否使用 ops 凭证（有 DeleteObject 权限）
ENV_S3_APP_ACCESS_KEY = "ENGRAM_S3_APP_ACCESS_KEY"  # App 用户访问密钥（默认，无 Delete 权限）
ENV_S3_APP_SECRET_KEY = "ENGRAM_S3_APP_SECRET_KEY"  # App 用户密钥
ENV_S3_OPS_ACCESS_KEY = "ENGRAM_S3_OPS_ACCESS_KEY"  # Ops 用户访问密钥（GC/迁移用，有 Delete 权限）
ENV_S3_OPS_SECRET_KEY = "ENGRAM_S3_OPS_SECRET_KEY"  # Ops 用户密钥

# 后端类型
BACKEND_LOCAL = "local"
BACKEND_FILE = "file"
BACKEND_OBJECT = "object"

VALID_BACKENDS = {BACKEND_LOCAL, BACKEND_FILE, BACKEND_OBJECT}

# 默认制品根目录
DEFAULT_ARTIFACTS_ROOT = "./.agentx/artifacts"

# 覆盖策略
OVERWRITE_ALLOW = "allow"           # 允许覆盖
OVERWRITE_DENY = "deny"             # 禁止覆盖（文件存在则报错）
OVERWRITE_ALLOW_SAME_HASH = "allow_same_hash"  # 仅允许相同哈希覆盖

VALID_OVERWRITE_POLICIES = {OVERWRITE_ALLOW, OVERWRITE_DENY, OVERWRITE_ALLOW_SAME_HASH}

# 默认权限模式
DEFAULT_FILE_MODE = 0o644
DEFAULT_DIR_MODE = 0o755


# =============================================================================
# ArtifactStore 接口
# =============================================================================


class ArtifactStore(ABC):
    """
    制品存储抽象接口

    定义制品存储的核心操作：put/get/exists/resolve
    """

    @abstractmethod
    def put(
        self,
        uri: str,
        content: Union[bytes, str, Iterator[bytes]],
        encoding: str = "utf-8",
    ) -> Dict[str, Any]:
        """
        写入制品

        Args:
            uri: 制品 URI（相对路径）
            content: 内容（bytes、str 或 bytes 迭代器）
            encoding: 字符串编码（默认 utf-8）

        Returns:
            {uri, sha256, size_bytes}

        Raises:
            ArtifactWriteError: 写入失败
        """
        pass

    @abstractmethod
    def get(self, uri: str) -> bytes:
        """
        读取制品内容

        Args:
            uri: 制品 URI

        Returns:
            制品内容（字节）

        Raises:
            ArtifactNotFoundError: 制品不存在
            ArtifactReadError: 读取失败
        """
        pass

    @abstractmethod
    def exists(self, uri: str) -> bool:
        """
        检查制品是否存在

        Args:
            uri: 制品 URI

        Returns:
            是否存在
        """
        pass

    @abstractmethod
    def resolve(self, uri: str) -> str:
        """
        解析 URI 为完整访问路径/URL

        Args:
            uri: 制品 URI

        Returns:
            完整路径或 URL
        """
        pass

    def get_info(self, uri: str) -> Dict[str, Any]:
        """
        获取制品元数据（需读取内容计算哈希）

        Args:
            uri: 制品 URI

        Returns:
            {uri, sha256, size_bytes}

        Raises:
            ArtifactNotFoundError: 制品不存在
        """
        content = self.get(uri)
        sha256 = hashlib.sha256(content).hexdigest()
        return {
            "uri": uri,
            "sha256": sha256,
            "size_bytes": len(content),
        }


# =============================================================================
# LocalArtifactsStore - 本地文件系统实现
# =============================================================================


class LocalArtifactsStore(ArtifactStore):
    """
    本地文件系统制品存储

    基于 ENGRAM_ARTIFACTS_ROOT 环境变量或默认目录存储制品。

    配置:
        环境变量 ENGRAM_ARTIFACTS_ROOT 或构造时传入 root 参数

    安全特性:
        - 路径规范化后禁止空路径、以 .. 开头或逃逸 root 目录
        - 使用 resolve() 验证路径确实在 root 之下
        - 可选 allowed_prefixes 白名单限制访问范围
        - 原子写入（先写临时文件，再 rename）
    """

    # 默认路径最大长度限制（字节数，适配多数文件系统限制）
    MAX_PATH_LENGTH = 4096

    def __init__(
        self,
        root: Optional[Union[str, Path]] = None,
        allowed_prefixes: Optional[list] = None,
        file_mode: Optional[int] = None,
        dir_mode: Optional[int] = None,
        overwrite_policy: str = OVERWRITE_ALLOW,
        read_only: bool = False,
    ):
        """
        初始化本地存储

        Args:
            root: 制品根目录，默认读取环境变量或使用 ./.agentx/artifacts
            allowed_prefixes: 允许的路径前缀列表（可选）
                              - None 表示允许所有路径（默认宽松）
                              - 空列表 [] 表示不允许任何路径
                              - ["scm/", "attachments/"] 表示只允许这些前缀

                              示例配置:
                                # 团队部署时收紧访问范围
                                allowed_prefixes = ["scm/", "attachments/", "exports/"]
            file_mode: 文件权限模式（如 0o644），默认 None 使用系统 umask
            dir_mode: 目录权限模式（如 0o755），默认 None 使用系统 umask
            overwrite_policy: 覆盖策略
                - "allow": 允许覆盖（默认）
                - "deny": 禁止覆盖，文件存在时报错
                - "allow_same_hash": 仅允许相同内容覆盖
            read_only: 只读模式，禁止所有写入操作
        """
        if root is not None:
            self._root = Path(root)
        else:
            env_root = os.environ.get(ENV_ARTIFACTS_ROOT)
            self._root = Path(env_root) if env_root else Path(DEFAULT_ARTIFACTS_ROOT)

        self._allowed_prefixes = allowed_prefixes
        self._file_mode = file_mode
        self._dir_mode = dir_mode
        self._read_only = read_only

        # 验证覆盖策略
        if overwrite_policy not in VALID_OVERWRITE_POLICIES:
            raise ValueError(
                f"无效的覆盖策略: {overwrite_policy}，有效值: {', '.join(sorted(VALID_OVERWRITE_POLICIES))}"
            )
        self._overwrite_policy = overwrite_policy

    @property
    def root(self) -> Path:
        """制品根目录"""
        return self._root

    @property
    def allowed_prefixes(self) -> Optional[list]:
        """允许的路径前缀列表"""
        return self._allowed_prefixes

    @property
    def file_mode(self) -> Optional[int]:
        """文件权限模式"""
        return self._file_mode

    @property
    def dir_mode(self) -> Optional[int]:
        """目录权限模式"""
        return self._dir_mode

    @property
    def overwrite_policy(self) -> str:
        """覆盖策略"""
        return self._overwrite_policy

    @property
    def read_only(self) -> bool:
        """只读模式"""
        return self._read_only

    def _normalize_uri(self, uri: str) -> str:
        """
        规范化 URI

        安全检查:
            1. 统一分隔符为 /
            2. 移除前导斜杠
            3. 检查原始路径中的 .. 组件（在 normpath 之前）
            4. 使用 os.path.normpath 规范化路径
            5. 禁止空路径
            6. 禁止以 .. 开头（逃逸根目录）

        Raises:
            PathTraversalError: 检测到路径穿越尝试
        """
        # 路径长度检查
        if len(uri.encode('utf-8')) > self.MAX_PATH_LENGTH:
            raise PathTraversalError(
                f"路径过长: 超过 {self.MAX_PATH_LENGTH} 字节",
                {"uri": uri[:100] + "...", "length": len(uri.encode('utf-8'))},
            )

        # 安全检查 0: 禁止空路径或仅含空白
        uri_stripped = uri.strip()
        if not uri_stripped:
            raise PathTraversalError(
                "路径为空或无效",
                {"original_uri": uri},
            )

        # 统一分隔符（处理混合分隔符：\、/、反斜杠变体）
        uri = uri.replace("\\", "/")
        # 移除前导斜杠
        uri = uri.lstrip("/")

        # 安全检查 1: 在 normpath 之前检查原始路径中的 .. 组件
        # 这样可以捕获 "scm/../etc/passwd" 这样的穿越尝试
        path_parts_raw = uri.split("/")
        if ".." in path_parts_raw:
            raise PathTraversalError(
                "检测到路径穿越尝试: 路径包含 .. 组件",
                {"uri": uri},
            )

        # 使用 os.path.normpath 处理 . 和多重斜杠
        uri = os.path.normpath(uri).replace("\\", "/")

        # 安全检查 2: 禁止空路径
        if not uri or uri == ".":
            raise PathTraversalError(
                "路径为空或无效",
                {"original_uri": uri},
            )

        # 安全检查 3: 禁止以 .. 开头（规范化后仍逃逸根目录）
        if uri.startswith(".."):
            raise PathTraversalError(
                "检测到路径穿越尝试: 路径以 .. 开头",
                {"uri": uri},
            )

        return uri

    def _validate_prefix(self, uri: str) -> None:
        """
        验证路径前缀是否在允许列表中

        Args:
            uri: 规范化后的 URI

        Raises:
            PathTraversalError: 路径前缀不在允许列表中
        """
        if self._allowed_prefixes is None:
            # None 表示允许所有路径（默认宽松模式）
            return

        if not self._allowed_prefixes:
            # 空列表表示不允许任何路径
            raise PathTraversalError(
                "路径前缀不在允许列表中",
                {"uri": uri, "allowed_prefixes": []},
            )

        for prefix in self._allowed_prefixes:
            if uri.startswith(prefix):
                return

        raise PathTraversalError(
            "路径前缀不在允许列表中",
            {"uri": uri, "allowed_prefixes": self._allowed_prefixes},
        )

    def _full_path(self, uri: str) -> Path:
        """
        获取完整文件路径

        安全检查:
            1. 规范化 URI
            2. 验证前缀白名单
            3. 使用 resolve() 验证路径确实在 root 之下

        Raises:
            PathTraversalError: 检测到路径穿越或前缀不允许
        """
        normalized = self._normalize_uri(uri)

        # 验证前缀白名单
        self._validate_prefix(normalized)

        full_path = self._root / normalized

        # 使用 resolve() 确保路径在 root 之下
        # 这是最终的安全屏障，防止符号链接等绕过
        try:
            # 先确保 root 存在以便 resolve
            resolved_root = self._root.resolve()
            resolved_path = full_path.resolve()

            # 检查 resolved_path 是否在 resolved_root 之下
            # 注意：文件可能不存在，所以只验证父目录路径
            # 对于不存在的路径，resolve() 会解析到最近的存在祖先
            # 因此需要检查路径字符串前缀
            if not str(resolved_path).startswith(str(resolved_root) + os.sep) and \
               resolved_path != resolved_root:
                raise PathTraversalError(
                    "检测到路径逃逸: 解析后的路径不在根目录下",
                    {
                        "uri": normalized,
                        "resolved_path": str(resolved_path),
                        "root": str(resolved_root),
                    },
                )
        except (OSError, ValueError) as e:
            # 路径解析失败（可能是非法字符等）
            raise PathTraversalError(
                f"路径解析失败: {e}",
                {"uri": normalized, "error": str(e)},
            )

        return full_path

    def _generate_temp_filename(self, target_path: Path) -> Path:
        """
        生成临时文件名（同目录，包含 pid + 随机数）

        格式: .{原文件名}.{pid}.{随机hex}.tmp

        Args:
            target_path: 目标文件路径

        Returns:
            临时文件路径
        """
        pid = os.getpid()
        random_hex = secrets.token_hex(8)
        temp_name = f".{target_path.name}.{pid}.{random_hex}.tmp"
        return target_path.parent / temp_name

    def _check_overwrite_policy(
        self,
        full_path: Path,
        new_sha256: str,
        normalized_uri: str,
    ) -> None:
        """
        检查覆盖策略

        Args:
            full_path: 目标文件完整路径
            new_sha256: 新内容的 SHA256 哈希
            normalized_uri: 规范化后的 URI

        Raises:
            ArtifactOverwriteDeniedError: 覆盖被拒绝
            ArtifactHashMismatchError: 哈希不匹配
        """
        if not full_path.exists():
            return  # 文件不存在，无需检查

        if self._overwrite_policy == OVERWRITE_ALLOW:
            return  # 允许覆盖

        if self._overwrite_policy == OVERWRITE_DENY:
            raise ArtifactOverwriteDeniedError(
                f"覆盖被拒绝: 文件已存在 {normalized_uri}",
                {
                    "uri": normalized_uri,
                    "path": str(full_path),
                    "policy": self._overwrite_policy,
                },
            )

        if self._overwrite_policy == OVERWRITE_ALLOW_SAME_HASH:
            # 计算现有文件的哈希
            existing_hasher = hashlib.sha256()
            try:
                with open(full_path, "rb") as f:
                    while True:
                        chunk = f.read(BUFFER_SIZE)
                        if not chunk:
                            break
                        existing_hasher.update(chunk)
                existing_sha256 = existing_hasher.hexdigest()
            except OSError as e:
                raise ArtifactReadError(
                    f"读取现有文件失败: {normalized_uri}",
                    {"uri": normalized_uri, "path": str(full_path), "error": str(e)},
                )

            if existing_sha256 != new_sha256:
                raise ArtifactHashMismatchError(
                    f"覆盖被拒绝: 内容哈希不匹配 {normalized_uri}",
                    {
                        "uri": normalized_uri,
                        "path": str(full_path),
                        "existing_sha256": existing_sha256,
                        "new_sha256": new_sha256,
                        "policy": self._overwrite_policy,
                    },
                )
            # 哈希匹配，允许覆盖（实际上内容相同，可以跳过写入）

    def put(
        self,
        uri: str,
        content: Union[bytes, str, Iterator[bytes]],
        encoding: str = "utf-8",
    ) -> Dict[str, Any]:
        """
        写入制品到本地文件系统（原子操作）

        使用临时文件 + rename 实现原子写入，避免半写入问题。
        
        Raises:
            ArtifactWriteDisabledError: 只读模式下禁止写入
        """
        # 只读模式检测
        if self._read_only:
            raise ArtifactWriteDisabledError(
                "制品存储处于只读模式，写入操作被禁止",
                {
                    "uri": uri,
                    "backend": "local",
                    "read_only": True,
                },
            )
        
        normalized_uri = self._normalize_uri(uri)
        full_path = self._full_path(uri)
        temp_path = None

        try:
            # 确保父目录存在
            if self._dir_mode is not None:
                # 使用指定的目录权限，需要逐级创建
                parent = full_path.parent
                if not parent.exists():
                    parent.mkdir(parents=True, exist_ok=True)
                    # 设置目录权限（注意：仅对新创建的目录有效）
                    try:
                        os.chmod(parent, self._dir_mode)
                    except OSError:
                        pass  # 权限设置失败不影响功能
            else:
                full_path.parent.mkdir(parents=True, exist_ok=True)

            hasher = hashlib.sha256()
            size = 0

            # 生成临时文件路径
            temp_path = self._generate_temp_filename(full_path)

            # 写入临时文件
            if isinstance(content, str):
                data = content.encode(encoding)
                hasher.update(data)
                size = len(data)
                with open(temp_path, "wb") as f:
                    f.write(data)

            elif isinstance(content, bytes):
                hasher.update(content)
                size = len(content)
                with open(temp_path, "wb") as f:
                    f.write(content)

            else:
                # 迭代器 -> 流式写入
                with open(temp_path, "wb") as f:
                    for chunk in content:
                        if isinstance(chunk, str):
                            chunk = chunk.encode(encoding)
                        hasher.update(chunk)
                        size += len(chunk)
                        f.write(chunk)

            new_sha256 = hasher.hexdigest()

            # 设置文件权限（在 rename 之前）
            if self._file_mode is not None:
                try:
                    os.chmod(temp_path, self._file_mode)
                except OSError:
                    pass  # 权限设置失败不影响功能

            # 对于 DENY 策略，使用 os.link() 实现原子性创建
            # os.link() 在目标存在时会抛出 FileExistsError，避免竞态条件
            if self._overwrite_policy == OVERWRITE_DENY:
                try:
                    # 使用硬链接原子性地创建目标文件
                    os.link(temp_path, full_path)
                    # 删除临时文件（保留目标文件）
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass
                    temp_path = None
                except FileExistsError:
                    raise ArtifactOverwriteDeniedError(
                        f"覆盖被拒绝: 文件已存在 {normalized_uri}",
                        {
                            "uri": normalized_uri,
                            "path": str(full_path),
                            "policy": self._overwrite_policy,
                        },
                    )
                except OSError as link_err:
                    # 硬链接可能因跨文件系统等原因失败，回退到检查+replace
                    # 但这仍有竞态风险，只是作为兼容性回退
                    self._check_overwrite_policy(full_path, new_sha256, normalized_uri)
                    os.replace(temp_path, full_path)
                    temp_path = None
            else:
                # 其他策略：先检查后 replace
                self._check_overwrite_policy(full_path, new_sha256, normalized_uri)
                # 原子 rename（同文件系统内为原子操作）
                os.replace(temp_path, full_path)
                temp_path = None  # 标记为已处理，避免 finally 中删除

            return {
                "uri": normalized_uri,
                "sha256": new_sha256,
                "size_bytes": size,
            }

        except (ArtifactOverwriteDeniedError, ArtifactHashMismatchError):
            # 覆盖策略相关错误，清理临时文件后重新抛出
            if temp_path and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            raise

        except OSError as e:
            raise ArtifactWriteError(
                f"写入制品失败: {normalized_uri}",
                {"uri": normalized_uri, "path": str(full_path), "error": str(e)},
            )

        finally:
            # 清理临时文件（仅当未成功 rename 时）
            if temp_path and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    def get(self, uri: str) -> bytes:
        """从本地文件系统读取制品"""
        normalized_uri = self._normalize_uri(uri)
        full_path = self._full_path(uri)

        if not full_path.exists():
            raise ArtifactNotFoundError(
                f"制品不存在: {normalized_uri}",
                {"uri": normalized_uri, "path": str(full_path)},
            )

        try:
            return full_path.read_bytes()
        except OSError as e:
            raise ArtifactReadError(
                f"读取制品失败: {normalized_uri}",
                {"uri": normalized_uri, "path": str(full_path), "error": str(e)},
            )

    def exists(self, uri: str) -> bool:
        """检查本地制品是否存在"""
        return self._full_path(uri).exists()

    def resolve(self, uri: str) -> str:
        """解析为本地文件路径"""
        return str(self._full_path(uri).absolute())

    def get_info(self, uri: str) -> Dict[str, Any]:
        """获取制品元数据（流式计算哈希，避免大文件内存问题）"""
        normalized_uri = self._normalize_uri(uri)
        full_path = self._full_path(uri)

        if not full_path.exists():
            raise ArtifactNotFoundError(
                f"制品不存在: {normalized_uri}",
                {"uri": normalized_uri, "path": str(full_path)},
            )

        hasher = hashlib.sha256()
        size = 0

        try:
            with open(full_path, "rb") as f:
                while True:
                    chunk = f.read(BUFFER_SIZE)
                    if not chunk:
                        break
                    hasher.update(chunk)
                    size += len(chunk)
        except OSError as e:
            raise ArtifactReadError(
                f"读取制品失败: {normalized_uri}",
                {"uri": normalized_uri, "path": str(full_path), "error": str(e)},
            )

        return {
            "uri": normalized_uri,
            "sha256": hasher.hexdigest(),
            "size_bytes": size,
        }


# =============================================================================
# FileUriStore - file:// URI 直读写实现
# =============================================================================


class FileUriPathError(ArtifactError):
    """file:// URI 路径解析或访问错误"""
    error_type = "FILE_URI_PATH_ERROR"


class FileUriStore(ArtifactStore):
    """
    file:// URI 直接读写存储

    支持直接使用 file:// 协议的 URI 进行读写操作。
    适用于跨系统文件共享（如 NFS、SMB）场景。

    URI 格式说明:
        本地路径:
            - Unix: file:///path/to/artifact.txt (空 netloc)
            - Windows: file:///C:/path/to/artifact.txt (空 netloc，驱动器号在 path 中)

        网络共享:
            - Windows UNC: file://server/share/path/artifact.txt (netloc=server)
            - SMB: file://fileserver/shared/artifact.txt
            - NFS (挂载为本地): file:///mnt/nfs/artifact.txt

    配置选项:
        allowed_roots: 允许读写的根路径列表，用于安全限制
                       - None: 允许所有路径（默认，适合开发环境）
                       - []: 空列表，拒绝所有路径
                       - ["/mnt/artifacts", "//server/share"]: 只允许这些根路径

    原子写入兼容性说明:
        由于 NFS/SMB 等网络文件系统的特性，原子重命名（os.rename）可能存在以下问题：
        - NFS v3: 不保证跨目录的原子重命名
        - NFS v4: 支持原子重命名，但需要服务器端配置
        - SMB/CIFS: Windows 上通常支持原子重命名，Linux 挂载取决于 mount 选项
        - 建议: 对于关键场景，使用同目录临时文件 + rename，或使用数据库事务确保一致性

        当 use_atomic_write=True 时，会使用 "写入临时文件 + rename" 策略：
        1. 写入同目录的 .tmp 临时文件
        2. 使用 os.rename() 原子重命名到目标路径
        注意：在某些 NFS 配置下，跨设备 rename 可能失败，此时会回退到直接写入。
    """

    # 路径最大长度限制
    MAX_PATH_LENGTH = 4096

    def __init__(
        self,
        allowed_roots: Optional[list] = None,
        use_atomic_write: bool = True,
        file_mode: Optional[int] = None,
        dir_mode: Optional[int] = None,
        overwrite_policy: str = OVERWRITE_ALLOW,
        read_only: bool = False,
    ):
        """
        初始化 FileUriStore

        Args:
            allowed_roots: 允许读写的根路径列表
                           - None: 允许所有路径（默认，适合开发环境）
                           - []: 空列表，拒绝所有路径
                           - 示例: ["/mnt/artifacts", "/mnt/nfs/shared"]
                           - Windows 示例: ["C:\\Artifacts", "\\\\server\\share"]
            use_atomic_write: 是否使用原子写入（临时文件 + rename）
                              对于 NFS/SMB 网络存储，建议根据实际环境测试后启用
                              默认为 True
            file_mode: 文件权限模式（如 0o644），默认 None 使用系统 umask
            dir_mode: 目录权限模式（如 0o755），默认 None 使用系统 umask
            overwrite_policy: 覆盖策略
                - "allow": 允许覆盖（默认）
                - "deny": 禁止覆盖，文件存在时报错
                - "allow_same_hash": 仅允许相同内容覆盖
            read_only: 只读模式，禁止所有写入操作
        """
        self._allowed_roots = allowed_roots
        self._use_atomic_write = use_atomic_write
        self._file_mode = file_mode
        self._dir_mode = dir_mode
        self._read_only = read_only

        # 验证覆盖策略
        if overwrite_policy not in VALID_OVERWRITE_POLICIES:
            raise ValueError(
                f"无效的覆盖策略: {overwrite_policy}，有效值: {', '.join(sorted(VALID_OVERWRITE_POLICIES))}"
            )
        self._overwrite_policy = overwrite_policy

    @property
    def allowed_roots(self) -> Optional[list]:
        """允许的根路径列表"""
        return self._allowed_roots

    @property
    def file_mode(self) -> Optional[int]:
        """文件权限模式"""
        return self._file_mode

    @property
    def dir_mode(self) -> Optional[int]:
        """目录权限模式"""
        return self._dir_mode

    @property
    def overwrite_policy(self) -> str:
        """覆盖策略"""
        return self._overwrite_policy

    @property
    def read_only(self) -> bool:
        """只读模式"""
        return self._read_only

    def _parse_file_uri(self, uri: str) -> Path:
        """
        解析 file:// URI 为本地路径

        完整处理 netloc（网络位置）和 path，支持:
        - Unix 本地路径: file:///path/to/file
        - Windows 本地路径: file:///C:/path/to/file
        - Windows UNC 路径: file://server/share/path/file
        - URL 编码路径: file:///path/with%20space/file

        Args:
            uri: file:// URI

        Returns:
            Path 对象

        Raises:
            FileUriPathError: URI 格式无效或路径不允许
        """
        from urllib.parse import unquote
        
        if not uri.startswith("file://"):
            raise FileUriPathError(
                f"无效的 file:// URI: {uri}",
                {"uri": uri, "reason": "必须以 file:// 开头"},
            )

        parsed = urlparse(uri)
        netloc = parsed.netloc  # 网络位置（服务器名）
        path = unquote(parsed.path)  # URL 解码路径

        # 路径长度检查
        if len(path.encode('utf-8')) > self.MAX_PATH_LENGTH:
            raise FileUriPathError(
                f"路径过长: 超过 {self.MAX_PATH_LENGTH} 字节",
                {"uri": uri, "length": len(path.encode('utf-8'))},
            )

        if os.name == "nt":
            # Windows 系统
            backslash = "\\"
            if netloc:
                # UNC 路径: file://server/share/path -> \\server\share\path
                # netloc 是服务器名，path 以 / 开头
                unc_path = f"\\\\{netloc}{path.replace('/', backslash)}"
                result_path = Path(unc_path)
            else:
                # 本地路径: file:///C:/path -> C:\path
                # path 格式为 /C:/path，需要移除前导 /
                if path.startswith("/") and len(path) > 2 and path[2] == ":":
                    path = path[1:]  # 移除前导 /
                result_path = Path(path.replace("/", backslash))
        else:
            # Unix/Linux/macOS 系统
            if netloc:
                # Unix 上的 netloc 通常不应存在（本地文件）
                # 但某些工具可能生成 file://localhost/path 格式
                if netloc.lower() not in ("localhost", "127.0.0.1", ""):
                    raise FileUriPathError(
                        f"Unix 系统不支持远程 file:// URI: {uri}",
                        {
                            "uri": uri,
                            "netloc": netloc,
                            "hint": "请使用 NFS/SMB 挂载点的本地路径，如 file:///mnt/nfs/path",
                        },
                    )
                # file://localhost/path -> /path
                result_path = Path(path)
            else:
                # 标准本地路径: file:///path -> /path
                result_path = Path(path)

        # 验证路径基本有效性
        if not path or path == "/":
            raise FileUriPathError(
                "路径为空或无效",
                {"uri": uri, "path": path},
            )

        # 检查路径穿越（.. 组件）
        path_str = str(result_path).replace("\\", "/")
        if ".." in path_str.split("/"):
            raise FileUriPathError(
                "检测到路径穿越尝试: 路径包含 .. 组件",
                {"uri": uri, "path": str(result_path)},
            )

        return result_path

    def _validate_allowed_root(self, path: Path) -> None:
        """
        验证路径是否在允许的根路径列表中

        Args:
            path: 待验证的路径

        Raises:
            FileUriPathError: 路径不在允许列表中
        """
        if self._allowed_roots is None:
            # None 表示允许所有路径
            return

        if not self._allowed_roots:
            # 空列表表示拒绝所有路径
            raise FileUriPathError(
                "路径不在允许的根路径列表中",
                {"path": str(path), "allowed_roots": []},
            )

        # 规范化待验证路径
        try:
            # 对于可能不存在的路径，使用父目录解析
            if path.exists():
                resolved_path = path.resolve()
            else:
                # 路径不存在时，尝试解析最近存在的祖先
                resolved_path = path.absolute()
        except (OSError, ValueError):
            resolved_path = path.absolute()

        path_str = str(resolved_path)

        for root in self._allowed_roots:
            # 规范化根路径
            root_path = Path(root)
            try:
                if root_path.exists():
                    resolved_root = str(root_path.resolve())
                else:
                    resolved_root = str(root_path.absolute())
            except (OSError, ValueError):
                resolved_root = str(root_path.absolute())

            # 检查路径是否以根路径开头
            # 需要确保是目录边界匹配（避免 /mnt/artifacts 匹配 /mnt/artifacts_backup）
            if path_str == resolved_root:
                return
            if path_str.startswith(resolved_root + os.sep):
                return
            # Windows UNC 路径可能使用 / 或 \
            if os.name == "nt":
                if path_str.startswith(resolved_root + "/"):
                    return

        raise FileUriPathError(
            "路径不在允许的根路径列表中",
            {"path": str(path), "allowed_roots": self._allowed_roots},
        )

    def _ensure_file_uri(self, uri: str) -> str:
        """确保 URI 为 file:// 格式"""
        from urllib.parse import quote
        
        if uri.startswith("file://"):
            return uri
        
        # 将普通路径转换为 file:// URI
        path = Path(uri).absolute()
        path_str = str(path)
        
        if os.name == "nt":
            # Windows 路径处理
            if path_str.startswith("\\\\"):
                # UNC 路径: \\server\share\path -> file://server/share/path
                # 移除前导 \\，然后分割服务器名和路径
                unc_without_prefix = path_str[2:]
                parts = unc_without_prefix.split("\\", 1)
                server = parts[0]
                share_path = "/" + parts[1].replace("\\", "/") if len(parts) > 1 else "/"
                # URL 编码路径中的特殊字符，但保留 /
                encoded_path = quote(share_path, safe="/")
                return f"file://{server}{encoded_path}"
            else:
                # 本地路径: C:\path -> file:///C:/path
                encoded_path = quote(path_str.replace("\\", "/"), safe="/:")
                return f"file:///{encoded_path}"
        else:
            # Unix 路径: /path -> file:///path
            encoded_path = quote(path_str, safe="/")
            return f"file://{encoded_path}"

    def _generate_temp_filename(self, target_path: Path) -> Path:
        """
        生成临时文件名（同目录，包含 pid + 随机数）

        格式: .{原文件名}.{pid}.{随机hex}.tmp

        Args:
            target_path: 目标文件路径

        Returns:
            临时文件路径
        """
        pid = os.getpid()
        random_hex = secrets.token_hex(8)
        temp_name = f".{target_path.name}.{pid}.{random_hex}.tmp"
        return target_path.parent / temp_name

    def _check_overwrite_policy(
        self,
        file_path: Path,
        new_sha256: str,
        file_uri: str,
    ) -> None:
        """
        检查覆盖策略

        Args:
            file_path: 目标文件完整路径
            new_sha256: 新内容的 SHA256 哈希
            file_uri: 文件 URI

        Raises:
            ArtifactOverwriteDeniedError: 覆盖被拒绝
            ArtifactHashMismatchError: 哈希不匹配
        """
        if not file_path.exists():
            return  # 文件不存在，无需检查

        if self._overwrite_policy == OVERWRITE_ALLOW:
            return  # 允许覆盖

        if self._overwrite_policy == OVERWRITE_DENY:
            raise ArtifactOverwriteDeniedError(
                f"覆盖被拒绝: 文件已存在 {file_uri}",
                {
                    "uri": file_uri,
                    "path": str(file_path),
                    "policy": self._overwrite_policy,
                },
            )

        if self._overwrite_policy == OVERWRITE_ALLOW_SAME_HASH:
            # 计算现有文件的哈希
            existing_hasher = hashlib.sha256()
            try:
                with open(file_path, "rb") as f:
                    while True:
                        chunk = f.read(BUFFER_SIZE)
                        if not chunk:
                            break
                        existing_hasher.update(chunk)
                existing_sha256 = existing_hasher.hexdigest()
            except OSError as e:
                raise ArtifactReadError(
                    f"读取现有文件失败: {file_uri}",
                    {"uri": file_uri, "path": str(file_path), "error": str(e)},
                )

            if existing_sha256 != new_sha256:
                raise ArtifactHashMismatchError(
                    f"覆盖被拒绝: 内容哈希不匹配 {file_uri}",
                    {
                        "uri": file_uri,
                        "path": str(file_path),
                        "existing_sha256": existing_sha256,
                        "new_sha256": new_sha256,
                        "policy": self._overwrite_policy,
                    },
                )
            # 哈希匹配，允许覆盖（实际上内容相同）

    def put(
        self,
        uri: str,
        content: Union[bytes, str, Iterator[bytes]],
        encoding: str = "utf-8",
    ) -> Dict[str, Any]:
        """
        写入制品到 file:// 路径（原子操作）

        使用临时文件 + rename 实现原子写入，避免半写入问题。
        
        Raises:
            ArtifactWriteDisabledError: 只读模式下禁止写入
        """
        # 只读模式检测
        if self._read_only:
            raise ArtifactWriteDisabledError(
                "制品存储处于只读模式，写入操作被禁止",
                {
                    "uri": uri,
                    "backend": "file",
                    "read_only": True,
                },
            )
        
        file_uri = self._ensure_file_uri(uri)
        file_path = self._parse_file_uri(file_uri)
        temp_path = None

        # 验证路径是否在允许列表中
        self._validate_allowed_root(file_path)

        try:
            # 确保父目录存在
            if self._dir_mode is not None:
                parent = file_path.parent
                if not parent.exists():
                    parent.mkdir(parents=True, exist_ok=True)
                    try:
                        os.chmod(parent, self._dir_mode)
                    except OSError:
                        pass
            else:
                file_path.parent.mkdir(parents=True, exist_ok=True)

            hasher = hashlib.sha256()
            size = 0

            # 收集所有内容
            if isinstance(content, str):
                data = content.encode(encoding)
            elif isinstance(content, bytes):
                data = content
            else:
                # 迭代器 -> 收集为 bytes
                chunks = []
                for chunk in content:
                    if isinstance(chunk, str):
                        chunk = chunk.encode(encoding)
                    chunks.append(chunk)
                data = b"".join(chunks)

            hasher.update(data)
            size = len(data)
            new_sha256 = hasher.hexdigest()

            # 写入文件
            if self._use_atomic_write:
                # 生成临时文件路径
                temp_path = self._generate_temp_filename(file_path)

                # 写入临时文件
                with open(temp_path, "wb") as f:
                    f.write(data)

                # 设置文件权限（在 rename 之前）
                if self._file_mode is not None:
                    try:
                        os.chmod(temp_path, self._file_mode)
                    except OSError:
                        pass

                # 对于 DENY 策略，使用 os.link() 实现原子性创建
                if self._overwrite_policy == OVERWRITE_DENY:
                    try:
                        # 使用硬链接原子性地创建目标文件
                        os.link(temp_path, file_path)
                        try:
                            temp_path.unlink()
                        except OSError:
                            pass
                        temp_path = None
                    except FileExistsError:
                        raise ArtifactOverwriteDeniedError(
                            f"覆盖被拒绝: 文件已存在 {file_uri}",
                            {
                                "uri": file_uri,
                                "path": str(file_path),
                                "policy": self._overwrite_policy,
                            },
                        )
                    except OSError as link_err:
                        # 硬链接失败，回退到检查+replace（有竞态风险，仅兼容性回退）
                        self._check_overwrite_policy(file_path, new_sha256, file_uri)
                        try:
                            os.replace(temp_path, file_path)
                            temp_path = None
                        except OSError:
                            if temp_path and temp_path.exists():
                                try:
                                    temp_path.unlink()
                                except OSError:
                                    pass
                            temp_path = None
                            with open(file_path, "wb") as f:
                                f.write(data)
                            if self._file_mode is not None:
                                try:
                                    os.chmod(file_path, self._file_mode)
                                except OSError:
                                    pass
                else:
                    # 其他策略：检查覆盖策略后原子 rename
                    self._check_overwrite_policy(file_path, new_sha256, file_uri)
                    try:
                        os.replace(temp_path, file_path)
                        temp_path = None  # 标记为已处理
                    except OSError:
                        # 原子写入失败（可能是跨设备），清理后回退到直接写入
                        if temp_path and temp_path.exists():
                            try:
                                temp_path.unlink()
                            except OSError:
                                pass
                        temp_path = None
                        with open(file_path, "wb") as f:
                            f.write(data)
                        if self._file_mode is not None:
                            try:
                                os.chmod(file_path, self._file_mode)
                            except OSError:
                                pass
            else:
                # 非原子写入模式
                if self._overwrite_policy == OVERWRITE_DENY:
                    # DENY 策略：使用 O_EXCL 标志创建文件（原子性）
                    try:
                        fd = os.open(file_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                        try:
                            os.write(fd, data)
                        finally:
                            os.close(fd)
                        if self._file_mode is not None:
                            try:
                                os.chmod(file_path, self._file_mode)
                            except OSError:
                                pass
                    except FileExistsError:
                        raise ArtifactOverwriteDeniedError(
                            f"覆盖被拒绝: 文件已存在 {file_uri}",
                            {
                                "uri": file_uri,
                                "path": str(file_path),
                                "policy": self._overwrite_policy,
                            },
                        )
                else:
                    # 其他策略：先检查覆盖策略
                    self._check_overwrite_policy(file_path, new_sha256, file_uri)
                    with open(file_path, "wb") as f:
                        f.write(data)
                    if self._file_mode is not None:
                        try:
                            os.chmod(file_path, self._file_mode)
                        except OSError:
                            pass

            return {
                "uri": file_uri,
                "sha256": new_sha256,
                "size_bytes": size,
            }

        except (ArtifactOverwriteDeniedError, ArtifactHashMismatchError):
            # 覆盖策略相关错误，清理临时文件后重新抛出
            if temp_path and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            raise

        except OSError as e:
            raise ArtifactWriteError(
                f"写入制品失败: {file_uri}",
                {"uri": file_uri, "path": str(file_path), "error": str(e)},
            )

        finally:
            # 清理临时文件（仅当未成功 rename 时）
            if temp_path and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    def get(self, uri: str) -> bytes:
        """从 file:// 路径读取制品"""
        file_uri = self._ensure_file_uri(uri)
        file_path = self._parse_file_uri(file_uri)

        # 验证路径是否在允许列表中
        self._validate_allowed_root(file_path)

        if not file_path.exists():
            raise ArtifactNotFoundError(
                f"制品不存在: {file_uri}",
                {"uri": file_uri, "path": str(file_path)},
            )

        try:
            return file_path.read_bytes()
        except OSError as e:
            raise ArtifactReadError(
                f"读取制品失败: {file_uri}",
                {"uri": file_uri, "path": str(file_path), "error": str(e)},
            )

    def exists(self, uri: str) -> bool:
        """检查 file:// 路径制品是否存在"""
        try:
            file_uri = self._ensure_file_uri(uri)
            file_path = self._parse_file_uri(file_uri)
            # 验证路径是否在允许列表中
            self._validate_allowed_root(file_path)
            return file_path.exists()
        except (FileUriPathError, ValueError, OSError):
            return False

    def resolve(self, uri: str) -> str:
        """返回完整的 file:// URI"""
        return self._ensure_file_uri(uri)


# =============================================================================
# ObjectStore - 对象存储实现（占位）
# =============================================================================


class ObjectStore(ArtifactStore):
    """
    对象存储后端（S3/MinIO 兼容）

    支持流式上传（边读边算 sha256，大对象走 multipart），流式下载，
    以及 SSE、storage_class、ACL 等高级配置。

    配置（环境变量）:
        ENGRAM_S3_ENDPOINT    S3/MinIO 端点 URL
        ENGRAM_S3_ACCESS_KEY  访问密钥
        ENGRAM_S3_SECRET_KEY  密钥
        ENGRAM_S3_BUCKET      存储桶名称
        ENGRAM_S3_REGION      区域（可选，默认 us-east-1）

    配置（config.toml）:
        [artifacts.object]
        endpoint = "https://s3.example.com"
        bucket = "my-artifacts"
        region = "us-east-1"
        prefix = "engram/"                # 对象键前缀
        allowed_prefixes = ["scm/", "attachments/"]  # 允许的 key 前缀列表
        sse = "AES256"                    # 服务端加密: AES256 | aws:kms
        storage_class = "STANDARD"        # 存储类别
        acl = "private"                   # ACL 策略
        connect_timeout = 10.0            # 连接超时秒数
        read_timeout = 60.0               # 读取超时秒数
        retries = 3                       # 最大重试次数
        max_size_bytes = 0                # 最大制品大小 (0=无限制)
        multipart_threshold = 5242880     # Multipart 阈值 (5MB)
        multipart_chunk_size = 8388608    # Multipart 分片大小 (8MB)

    安全特性:
        - 可选 allowed_prefixes 白名单限制访问范围（与 local 后端语义一致）
        - 当 allowed_prefixes 设置后，所有操作的 key 必须匹配前缀列表
    """

    def __init__(
        self,
        endpoint: Optional[str] = None,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        bucket: Optional[str] = None,
        region: Optional[str] = None,
        prefix: str = "",
        allowed_prefixes: Optional[list] = None,
        sse: Optional[str] = None,
        storage_class: Optional[str] = None,
        acl: Optional[str] = None,
        connect_timeout: Optional[float] = None,
        read_timeout: Optional[float] = None,
        retries: Optional[int] = None,
        max_size_bytes: int = 0,
        multipart_threshold: Optional[int] = None,
        multipart_chunk_size: Optional[int] = None,
        verify_ssl: Optional[bool] = None,
        ca_bundle: Optional[str] = None,
        addressing_style: str = "auto",
        read_only: bool = False,
    ):
        """
        初始化对象存储

        Args:
            endpoint: S3 端点 URL
            access_key: 访问密钥
            secret_key: 密钥
            bucket: 存储桶名称
            region: 区域
            prefix: 对象键前缀（会自动添加到所有 uri 前面）
            allowed_prefixes: 允许的 key 前缀列表（可选）
                              - None 表示允许所有路径（默认宽松）
                              - 空列表 [] 表示不允许任何路径
                              - ["scm/", "attachments/"] 表示只允许这些前缀
                              注意：验证时使用 prefix + uri 的完整 key
            sse: 服务端加密类型 (AES256 | aws:kms)
            storage_class: 存储类别 (STANDARD | STANDARD_IA | GLACIER 等)
            acl: ACL 策略 (private | public-read 等)
            connect_timeout: 连接超时秒数
            read_timeout: 读取超时秒数
            retries: 最大重试次数
            max_size_bytes: 最大制品大小限制 (0=无限制)
            multipart_threshold: Multipart 上传阈值
            multipart_chunk_size: Multipart 分片大小
            verify_ssl: SSL 证书验证 (True=验证, False=跳过)
            ca_bundle: 自定义 CA 证书路径
            read_only: 只读模式，禁止所有写入操作
        """
        self.endpoint = endpoint or os.environ.get(ENV_S3_ENDPOINT)
        
        # 凭证选择逻辑（与 docker-compose.unified.yml 对齐）
        # 优先级:
        #   1. 构造函数显式传入的 access_key/secret_key（最高）
        #   2. 根据 ENGRAM_S3_USE_OPS 选择 ops 或 app 凭证
        #   3. 回退到 ENGRAM_S3_ACCESS_KEY/SECRET_KEY
        if access_key is not None and secret_key is not None:
            # 显式传入凭证，直接使用
            self.access_key = access_key
            self.secret_key = secret_key
            self._using_ops_credentials = None  # 未知，由调用方决定
        else:
            # 从环境变量选择凭证
            use_ops = os.environ.get(ENV_S3_USE_OPS, "false").lower() in ("1", "true", "yes")
            if use_ops:
                # 使用 ops 凭证（有 DeleteObject 权限）
                self.access_key = (
                    access_key
                    or os.environ.get(ENV_S3_OPS_ACCESS_KEY)
                    or os.environ.get(ENV_S3_ACCESS_KEY)
                )
                self.secret_key = (
                    secret_key
                    or os.environ.get(ENV_S3_OPS_SECRET_KEY)
                    or os.environ.get(ENV_S3_SECRET_KEY)
                )
                self._using_ops_credentials = True
            else:
                # 使用 app 凭证（无 DeleteObject 权限）
                self.access_key = (
                    access_key
                    or os.environ.get(ENV_S3_APP_ACCESS_KEY)
                    or os.environ.get(ENV_S3_ACCESS_KEY)
                )
                self.secret_key = (
                    secret_key
                    or os.environ.get(ENV_S3_APP_SECRET_KEY)
                    or os.environ.get(ENV_S3_SECRET_KEY)
                )
                self._using_ops_credentials = False
        
        self.bucket = bucket or os.environ.get(ENV_S3_BUCKET)
        self.region = region or os.environ.get(ENV_S3_REGION, "us-east-1")
        self.prefix = prefix.strip("/")
        self._allowed_prefixes = allowed_prefixes

        # 高级配置
        self.sse = sse
        self.storage_class = storage_class
        self.acl = acl
        self.connect_timeout = connect_timeout if connect_timeout is not None else DEFAULT_CONNECT_TIMEOUT
        self.read_timeout = read_timeout if read_timeout is not None else DEFAULT_READ_TIMEOUT
        self.retries = retries if retries is not None else DEFAULT_MAX_RETRIES
        self.max_size_bytes = max_size_bytes if max_size_bytes is not None else DEFAULT_MAX_SIZE_BYTES
        self.multipart_threshold = multipart_threshold if multipart_threshold is not None else MULTIPART_THRESHOLD
        self.multipart_chunk_size = multipart_chunk_size if multipart_chunk_size is not None else MULTIPART_CHUNK_SIZE

        # SSL 验证配置
        # 优先级: 构造函数参数 > 环境变量 > 默认值 True
        if verify_ssl is not None:
            self.verify_ssl = verify_ssl
        else:
            env_verify = os.environ.get(ENV_S3_VERIFY_SSL)
            if env_verify is not None:
                self.verify_ssl = env_verify.lower() not in ("false", "0", "no", "off")
            else:
                self.verify_ssl = True
        
        # CA 证书路径: 构造函数参数 > 环境变量
        self.ca_bundle = ca_bundle or os.environ.get(ENV_S3_CA_BUNDLE)

        # S3 地址寻址风格: auto | path | virtual
        self.addressing_style = addressing_style

        # 只读模式
        self.read_only = read_only

        self._client = None

    @property
    def allowed_prefixes(self) -> Optional[list]:
        """允许的 key 前缀列表"""
        return self._allowed_prefixes

    @property
    def using_ops_credentials(self) -> Optional[bool]:
        """
        当前是否使用 ops 凭证（有 DeleteObject 权限）
        
        Returns:
            True: 使用 ops 凭证
            False: 使用 app 凭证
            None: 未知（凭证由构造函数显式传入）
        """
        return self._using_ops_credentials

    def is_ops_credentials(self) -> bool:
        """
        检查当前凭证是否为 ops 凭证
        
        用于 GC/迁移脚本在执行删除操作前验证凭证级别。
        
        当凭证由构造函数显式传入时（using_ops_credentials=None），
        此方法检查 ENGRAM_S3_USE_OPS 环境变量作为判断依据。
        
        Returns:
            True: 当前使用 ops 凭证或 ENGRAM_S3_USE_OPS=true
            False: 使用 app 凭证
        """
        if self._using_ops_credentials is not None:
            return self._using_ops_credentials
        # 凭证由构造函数传入，检查环境变量
        return os.environ.get(ENV_S3_USE_OPS, "false").lower() in ("1", "true", "yes")

    def _validate_key_prefix(self, key: str, uri: str) -> None:
        """
        验证对象 key 是否在允许的前缀列表中

        Args:
            key: 完整的对象 key（包含 prefix）
            uri: 原始 URI（用于错误信息）

        Raises:
            PathTraversalError: key 前缀不在允许列表中
        """
        if self._allowed_prefixes is None:
            # None 表示允许所有路径（默认宽松模式）
            return

        if not self._allowed_prefixes:
            # 空列表表示不允许任何路径
            raise PathTraversalError(
                "对象 key 前缀不在允许列表中",
                {"uri": uri, "key": key, "allowed_prefixes": []},
            )

        for prefix in self._allowed_prefixes:
            if key.startswith(prefix):
                return

        raise PathTraversalError(
            "对象 key 前缀不在允许列表中",
            {"uri": uri, "key": key, "allowed_prefixes": self._allowed_prefixes},
        )

    def _check_configured(self) -> None:
        """检查配置是否完整"""
        missing = []
        if not self.endpoint:
            missing.append("endpoint (ENGRAM_S3_ENDPOINT)")
        if not self.access_key:
            missing.append("access_key (ENGRAM_S3_ACCESS_KEY)")
        if not self.secret_key:
            missing.append("secret_key (ENGRAM_S3_SECRET_KEY)")
        if not self.bucket:
            missing.append("bucket (ENGRAM_S3_BUCKET)")

        if missing:
            raise ObjectStoreNotConfiguredError(
                f"对象存储配置不完整，缺少: {', '.join(missing)}",
                {"missing": missing},
            )

    def _get_client(self):
        """
        获取 S3 客户端（惰性初始化）

        Raises:
            ObjectStoreNotConfiguredError: 配置不完整或不安全
            ObjectStoreConnectionError: 连接失败
        """
        if self._client is not None:
            return self._client

        self._check_configured()
        
        # 安全检查: verify_ssl=True 时不允许使用 http:// 端点
        if self.verify_ssl and self.endpoint:
            parsed = urlparse(self.endpoint)
            if parsed.scheme.lower() == "http":
                raise ObjectStoreNotConfiguredError(
                    "SSL 验证已启用但端点使用 HTTP 协议，这是不安全的配置。"
                    "请使用 HTTPS 端点或设置 verify_ssl=false（仅用于开发环境）",
                    {
                        "endpoint": self.endpoint,
                        "verify_ssl": self.verify_ssl,
                        "hint": "生产环境请使用 https:// 端点；开发环境可设置 ENGRAM_S3_VERIFY_SSL=false",
                    },
                )

        try:
            import boto3
            from botocore.config import Config as BotoConfig
        except ImportError:
            raise ObjectStoreNotConfiguredError(
                "对象存储需要安装 boto3: pip install boto3",
                {"hint": "pip install boto3"},
            )

        try:
            # 配置超时、重试和 S3 地址寻址风格
            config = BotoConfig(
                signature_version="s3v4",
                connect_timeout=self.connect_timeout,
                read_timeout=self.read_timeout,
                retries={
                    "max_attempts": self.retries,
                    "mode": "adaptive",
                },
                s3={
                    "addressing_style": self.addressing_style,
                },
            )
            
            # 确定 verify 参数值
            # - ca_bundle 路径优先（即使 verify_ssl=False 也使用 ca_bundle）
            # - verify_ssl=True 时使用系统 CA
            # - verify_ssl=False 时跳过验证
            if self.ca_bundle:
                # 使用自定义 CA 证书
                verify_param = self.ca_bundle
            else:
                # 使用布尔值
                verify_param = self.verify_ssl

            self._client = boto3.client(
                "s3",
                endpoint_url=self.endpoint,
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                region_name=self.region,
                config=config,
                verify=verify_param,
            )
            return self._client
        except Exception as e:
            raise ObjectStoreConnectionError(
                f"对象存储连接失败: {e}",
                {"endpoint": self.endpoint, "bucket": self.bucket, "error": str(e)},
            )

    def _classify_error(self, error: Exception, uri: str, key: str) -> Exception:
        """
        分类 S3 错误为具体的错误类型

        Args:
            error: 原始异常
            uri: 制品 URI
            key: 对象键

        Returns:
            分类后的 EngramError 子类实例
        """
        error_name = type(error).__name__
        error_str = str(error)

        # 检查是否为 Not Found
        if "NoSuchKey" in error_name or "NoSuchKey" in error_str or "404" in error_str or "Not Found" in error_str:
            return ArtifactNotFoundError(
                f"制品不存在: {uri}",
                {"uri": uri, "key": key, "bucket": self.bucket},
            )

        # 检查超时错误
        if "Timeout" in error_name or "timeout" in error_str.lower():
            return ObjectStoreTimeoutError(
                f"对象存储操作超时: {uri}",
                {"uri": uri, "key": key, "bucket": self.bucket, "error": error_str},
            )

        # 检查限流错误
        if "Throttl" in error_str or "SlowDown" in error_str or "503" in error_str:
            return ObjectStoreThrottlingError(
                f"对象存储请求被限流: {uri}",
                {"uri": uri, "key": key, "bucket": self.bucket, "error": error_str},
            )

        # 默认返回通用错误
        return ObjectStoreError(
            f"对象存储操作失败: {uri}",
            {"uri": uri, "key": key, "bucket": self.bucket, "error": error_str},
        )

    def _object_key(self, uri: str, validate: bool = True) -> str:
        """
        生成对象键并验证前缀
        
        Args:
            uri: 制品 URI
            validate: 是否验证 allowed_prefixes（默认 True）
        
        Returns:
            完整的对象 key
        
        Raises:
            PathTraversalError: key 前缀不在允许列表中
        """
        normalized_uri = uri.lstrip("/").replace("\\", "/")
        if self.prefix:
            key = f"{self.prefix}/{normalized_uri}"
        else:
            key = normalized_uri
        
        # 验证 key 是否在允许的前缀列表中
        if validate:
            self._validate_key_prefix(key, uri)
        
        return key

    def _build_put_extra_args(self) -> Dict[str, Any]:
        """构建 put_object 的额外参数"""
        extra_args: Dict[str, Any] = {}

        if self.sse:
            if self.sse.lower() == "aes256":
                extra_args["ServerSideEncryption"] = "AES256"
            elif self.sse.lower() == "aws:kms":
                extra_args["ServerSideEncryption"] = "aws:kms"

        if self.storage_class:
            extra_args["StorageClass"] = self.storage_class

        if self.acl:
            extra_args["ACL"] = self.acl

        return extra_args

    def put(
        self,
        uri: str,
        content: Union[bytes, str, Iterator[bytes]],
        encoding: str = "utf-8",
    ) -> Dict[str, Any]:
        """
        上传制品到对象存储

        支持流式上传：
        - 小对象（< multipart_threshold）: 直接 put_object
        - 大对象: 使用 multipart upload，边读边算 sha256

        对于 Iterator 输入，采用 streaming multipart 策略：
        - 先缓存至多 multipart_threshold 的数据判断是否走单次 put
        - 一旦超过阈值，切换到 multipart，持续按 multipart_chunk_size 切片上传

        Args:
            uri: 制品 URI
            content: 内容（bytes、str 或 bytes 迭代器）
            encoding: 字符串编码（默认 utf-8）

        Returns:
            {uri, sha256, size_bytes}

        Raises:
            ArtifactWriteDisabledError: 只读模式下禁止写入
            ArtifactSizeLimitExceededError: 大小超出限制
            ObjectStoreUploadError: 上传失败
        """
        # 只读模式检测
        if self.read_only:
            raise ArtifactWriteDisabledError(
                "制品存储处于只读模式，写入操作被禁止",
                {
                    "uri": uri,
                    "backend": "object",
                    "read_only": True,
                },
            )
        
        client = self._get_client()
        key = self._object_key(uri)
        extra_args = self._build_put_extra_args()

        hasher = hashlib.sha256()
        total_size = 0

        # 判断内容类型并处理
        if isinstance(content, str):
            data = content.encode(encoding)
            hasher.update(data)
            total_size = len(data)

            # 检查大小限制
            if self.max_size_bytes > 0 and total_size > self.max_size_bytes:
                raise ArtifactSizeLimitExceededError(
                    f"制品大小 {total_size} 字节超出限制 {self.max_size_bytes} 字节",
                    {"uri": uri, "size": total_size, "limit": self.max_size_bytes},
                )

            try:
                client.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=data,
                    ContentLength=total_size,
                    Metadata={"sha256": hasher.hexdigest()},
                    **extra_args,
                )
            except Exception as e:
                raise ObjectStoreUploadError(
                    f"上传制品失败: {uri}",
                    {"uri": uri, "key": key, "bucket": self.bucket, "error": str(e)},
                )

        elif isinstance(content, bytes):
            hasher.update(content)
            total_size = len(content)

            # 检查大小限制
            if self.max_size_bytes > 0 and total_size > self.max_size_bytes:
                raise ArtifactSizeLimitExceededError(
                    f"制品大小 {total_size} 字节超出限制 {self.max_size_bytes} 字节",
                    {"uri": uri, "size": total_size, "limit": self.max_size_bytes},
                )

            try:
                client.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=content,
                    ContentLength=total_size,
                    Metadata={"sha256": hasher.hexdigest()},
                    **extra_args,
                )
            except Exception as e:
                raise ObjectStoreUploadError(
                    f"上传制品失败: {uri}",
                    {"uri": uri, "key": key, "bucket": self.bucket, "error": str(e)},
                )

        else:
            # 迭代器 -> 流式上传 (streaming multipart)
            # 策略: 先缓存至多 multipart_threshold 的数据，一旦超过则切换到 multipart
            total_size, sha256_hex = self._streaming_iterator_upload(
                client, key, content, encoding, extra_args, uri
            )
            return {
                "uri": uri,
                "sha256": sha256_hex,
                "size_bytes": total_size,
            }

        return {
            "uri": uri,
            "sha256": hasher.hexdigest(),
            "size_bytes": total_size,
        }

    def _streaming_iterator_upload(
        self,
        client,
        key: str,
        content: Iterator[bytes],
        encoding: str,
        extra_args: Dict[str, Any],
        uri: str,
    ) -> tuple:
        """
        流式处理 Iterator 输入的上传

        策略:
        1. 先缓存至多 multipart_threshold 的数据
        2. 如果迭代器结束且数据 < threshold，使用单次 put_object
        3. 如果数据 >= threshold，立即启动 multipart upload，持续上传

        全程增量计算 sha256，支持 max_size_bytes 限制检查。
        失败时确保调用 abort_multipart_upload。

        Args:
            client: S3 客户端
            key: 对象键
            content: bytes 迭代器
            encoding: 字符串编码
            extra_args: 额外参数
            uri: 制品 URI（用于错误信息）

        Returns:
            (total_size, sha256_hex) 元组

        Raises:
            ArtifactSizeLimitExceededError: 大小超出限制
            ObjectStoreUploadError: 上传失败
        """
        hasher = hashlib.sha256()
        buffer = bytearray()
        total_size = 0
        iterator_exhausted = False

        # 阶段 1: 缓存至多 multipart_threshold 的数据
        try:
            for chunk in content:
                if isinstance(chunk, str):
                    chunk = chunk.encode(encoding)

                hasher.update(chunk)
                total_size += len(chunk)
                buffer.extend(chunk)

                # 检查大小限制
                if self.max_size_bytes > 0 and total_size > self.max_size_bytes:
                    raise ArtifactSizeLimitExceededError(
                        f"制品大小超出限制 {self.max_size_bytes} 字节",
                        {"uri": uri, "size": total_size, "limit": self.max_size_bytes},
                    )

                # 一旦超过阈值，切换到 streaming multipart
                if len(buffer) >= self.multipart_threshold:
                    break
            else:
                # 迭代器已耗尽，数据未超过阈值
                iterator_exhausted = True
        except ArtifactSizeLimitExceededError:
            raise
        except Exception as e:
            raise ObjectStoreUploadError(
                f"读取内容失败: {uri}",
                {"uri": uri, "key": key, "bucket": self.bucket, "error": str(e)},
            )

        # 阶段 2: 判断走单次 put 还是 multipart
        if iterator_exhausted:
            # 数据 < threshold，单次 put
            data = bytes(buffer)
            try:
                client.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=data,
                    ContentLength=total_size,
                    Metadata={"sha256": hasher.hexdigest()},
                    **extra_args,
                )
            except Exception as e:
                raise ObjectStoreUploadError(
                    f"上传制品失败: {uri}",
                    {"uri": uri, "key": key, "bucket": self.bucket, "error": str(e)},
                )
            return (total_size, hasher.hexdigest())

        # 阶段 3: 数据 >= threshold，启动 streaming multipart
        upload_id = None
        try:
            # 开始 multipart upload
            create_args = {
                "Bucket": self.bucket,
                "Key": key,
            }
            create_args.update(extra_args)

            mpu = client.create_multipart_upload(**create_args)
            upload_id = mpu["UploadId"]

            parts = []
            part_number = 1

            # 上传缓冲区中已有的数据（按 chunk_size 切片）
            offset = 0
            while offset < len(buffer):
                chunk_data = bytes(buffer[offset:offset + self.multipart_chunk_size])
                if len(chunk_data) < self.multipart_chunk_size and offset + len(chunk_data) < len(buffer):
                    # 还没到 buffer 末尾，继续累积
                    offset += len(chunk_data)
                    continue

                # 检查是否需要继续从迭代器读取以填充当前 part
                while len(chunk_data) < self.multipart_chunk_size:
                    try:
                        next_chunk = next(content)
                        if isinstance(next_chunk, str):
                            next_chunk = next_chunk.encode(encoding)

                        hasher.update(next_chunk)
                        total_size += len(next_chunk)

                        # 检查大小限制
                        if self.max_size_bytes > 0 and total_size > self.max_size_bytes:
                            raise ArtifactSizeLimitExceededError(
                                f"制品大小超出限制 {self.max_size_bytes} 字节",
                                {"uri": uri, "size": total_size, "limit": self.max_size_bytes},
                            )

                        chunk_data = chunk_data + next_chunk

                    except StopIteration:
                        # 迭代器耗尽
                        break

                # 上传 part
                if chunk_data:
                    response = client.upload_part(
                        Bucket=self.bucket,
                        Key=key,
                        UploadId=upload_id,
                        PartNumber=part_number,
                        Body=chunk_data,
                    )
                    parts.append({
                        "PartNumber": part_number,
                        "ETag": response["ETag"],
                    })
                    part_number += 1

                offset += self.multipart_chunk_size

            # 继续从迭代器读取剩余数据并上传
            pending_data = bytearray()
            for chunk in content:
                if isinstance(chunk, str):
                    chunk = chunk.encode(encoding)

                hasher.update(chunk)
                total_size += len(chunk)
                pending_data.extend(chunk)

                # 检查大小限制
                if self.max_size_bytes > 0 and total_size > self.max_size_bytes:
                    raise ArtifactSizeLimitExceededError(
                        f"制品大小超出限制 {self.max_size_bytes} 字节",
                        {"uri": uri, "size": total_size, "limit": self.max_size_bytes},
                    )

                # 当累积够一个 chunk，上传
                while len(pending_data) >= self.multipart_chunk_size:
                    chunk_to_upload = bytes(pending_data[:self.multipart_chunk_size])
                    pending_data = pending_data[self.multipart_chunk_size:]

                    response = client.upload_part(
                        Bucket=self.bucket,
                        Key=key,
                        UploadId=upload_id,
                        PartNumber=part_number,
                        Body=chunk_to_upload,
                    )
                    parts.append({
                        "PartNumber": part_number,
                        "ETag": response["ETag"],
                    })
                    part_number += 1

            # 上传剩余数据（最后一个 part 可以小于 chunk_size）
            if pending_data:
                response = client.upload_part(
                    Bucket=self.bucket,
                    Key=key,
                    UploadId=upload_id,
                    PartNumber=part_number,
                    Body=bytes(pending_data),
                )
                parts.append({
                    "PartNumber": part_number,
                    "ETag": response["ETag"],
                })

            # 完成 multipart upload，写入 sha256 到 metadata
            # 注意：complete_multipart_upload 不支持 Metadata，需要在 create 时设置
            # 但我们此时才知道最终 sha256，所以需要用 copy_object 更新 metadata
            client.complete_multipart_upload(
                Bucket=self.bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )

            # 使用 copy_object 更新 metadata（self-copy with MetadataDirective=REPLACE）
            sha256_hex = hasher.hexdigest()
            try:
                copy_source = {"Bucket": self.bucket, "Key": key}
                copy_extra = {"Metadata": {"sha256": sha256_hex}, "MetadataDirective": "REPLACE"}
                copy_extra.update(extra_args)
                client.copy_object(
                    CopySource=copy_source,
                    Bucket=self.bucket,
                    Key=key,
                    **copy_extra,
                )
            except Exception:
                # metadata 更新失败不影响上传成功，静默忽略
                pass

            return (total_size, sha256_hex)

        except ArtifactSizeLimitExceededError:
            # 大小限制错误，需要 abort
            if upload_id:
                try:
                    client.abort_multipart_upload(
                        Bucket=self.bucket,
                        Key=key,
                        UploadId=upload_id,
                    )
                except Exception:
                    pass  # 忽略 abort 失败
            raise

        except Exception as e:
            # 其他错误，需要 abort
            if upload_id:
                try:
                    client.abort_multipart_upload(
                        Bucket=self.bucket,
                        Key=key,
                        UploadId=upload_id,
                    )
                except Exception:
                    pass  # 忽略 abort 失败
            raise ObjectStoreUploadError(
                f"Streaming multipart 上传失败: {uri}",
                {"uri": uri, "key": key, "bucket": self.bucket, "error": str(e)},
            )

    def _multipart_upload(
        self,
        client,
        key: str,
        data: bytes,
        sha256_hex: str,
        extra_args: Dict[str, Any],
    ) -> None:
        """
        执行 multipart 上传

        Args:
            client: S3 客户端
            key: 对象键
            data: 完整数据
            sha256_hex: SHA256 哈希
            extra_args: 额外参数
        """
        # 开始 multipart upload
        create_args = {
            "Bucket": self.bucket,
            "Key": key,
            "Metadata": {"sha256": sha256_hex},
        }
        create_args.update(extra_args)

        mpu = client.create_multipart_upload(**create_args)
        upload_id = mpu["UploadId"]

        parts = []
        part_number = 1

        try:
            # 分片上传
            offset = 0
            while offset < len(data):
                chunk = data[offset:offset + self.multipart_chunk_size]
                response = client.upload_part(
                    Bucket=self.bucket,
                    Key=key,
                    UploadId=upload_id,
                    PartNumber=part_number,
                    Body=chunk,
                )
                parts.append({
                    "PartNumber": part_number,
                    "ETag": response["ETag"],
                })
                part_number += 1
                offset += self.multipart_chunk_size

            # 完成 multipart upload
            client.complete_multipart_upload(
                Bucket=self.bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )

        except Exception as e:
            # 取消 multipart upload
            try:
                client.abort_multipart_upload(
                    Bucket=self.bucket,
                    Key=key,
                    UploadId=upload_id,
                )
            except Exception:
                pass  # 忽略取消失败
            raise e

    def get(self, uri: str) -> bytes:
        """
        从对象存储下载制品（完整读取）

        Args:
            uri: 制品 URI

        Returns:
            制品内容（字节）

        Raises:
            ArtifactNotFoundError: 对象不存在
            ArtifactSizeLimitExceededError: 大小超出限制
            ObjectStoreDownloadError: 下载失败
        """
        client = self._get_client()
        key = self._object_key(uri)

        try:
            # 先检查对象大小
            if self.max_size_bytes > 0:
                head = client.head_object(Bucket=self.bucket, Key=key)
                content_length = head.get("ContentLength", 0)
                if content_length > self.max_size_bytes:
                    raise ArtifactSizeLimitExceededError(
                        f"制品大小 {content_length} 字节超出限制 {self.max_size_bytes} 字节",
                        {"uri": uri, "size": content_length, "limit": self.max_size_bytes},
                    )

            response = client.get_object(Bucket=self.bucket, Key=key)
            return response["Body"].read()

        except ArtifactSizeLimitExceededError:
            raise
        except Exception as e:
            classified = self._classify_error(e, uri, key)
            # 如果是已分类的错误（非通用 ObjectStoreError），直接抛出
            if isinstance(classified, (ArtifactNotFoundError, ObjectStoreTimeoutError, ObjectStoreThrottlingError)):
                raise classified
            # 否则抛出下载错误
            raise ObjectStoreDownloadError(
                f"下载制品失败: {uri}",
                {"uri": uri, "key": key, "bucket": self.bucket, "error": str(e)},
            )

    def get_stream(self, uri: str, chunk_size: int = BUFFER_SIZE) -> Iterator[bytes]:
        """
        流式读取对象存储制品

        Args:
            uri: 制品 URI
            chunk_size: 分片大小（默认 64KB）

        Yields:
            bytes: 数据分片

        Raises:
            ArtifactNotFoundError: 对象不存在
            ArtifactSizeLimitExceededError: 大小超出限制
            ObjectStoreDownloadError: 下载失败
        """
        client = self._get_client()
        key = self._object_key(uri)

        try:
            # 先检查对象大小
            if self.max_size_bytes > 0:
                head = client.head_object(Bucket=self.bucket, Key=key)
                content_length = head.get("ContentLength", 0)
                if content_length > self.max_size_bytes:
                    raise ArtifactSizeLimitExceededError(
                        f"制品大小 {content_length} 字节超出限制 {self.max_size_bytes} 字节",
                        {"uri": uri, "size": content_length, "limit": self.max_size_bytes},
                    )

            response = client.get_object(Bucket=self.bucket, Key=key)
            body = response["Body"]

            # 流式读取
            while True:
                chunk = body.read(chunk_size)
                if not chunk:
                    break
                yield chunk

        except ArtifactSizeLimitExceededError:
            raise
        except Exception as e:
            classified = self._classify_error(e, uri, key)
            # 如果是已分类的错误（非通用 ObjectStoreError），直接抛出
            if isinstance(classified, (ArtifactNotFoundError, ObjectStoreTimeoutError, ObjectStoreThrottlingError)):
                raise classified
            raise ObjectStoreDownloadError(
                f"流式下载制品失败: {uri}",
                {"uri": uri, "key": key, "bucket": self.bucket, "error": str(e)},
            )

    def exists(self, uri: str) -> bool:
        """检查对象是否存在"""
        client = self._get_client()
        key = self._object_key(uri)

        try:
            client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False

    def resolve(self, uri: str) -> str:
        """
        解析为完整的 S3 URL

        Returns:
            s3://bucket/key 格式的 URL
        """
        key = self._object_key(uri)
        return f"s3://{self.bucket}/{key}"

    def generate_presigned_url(
        self,
        uri: str,
        operation: str = "get_object",
        expires_in: int = 3600,
    ) -> str:
        """
        生成预签名 URL（用于临时访问）

        Args:
            uri: 制品 URI
            operation: 操作类型（get_object, put_object）
            expires_in: 有效期（秒，默认 1 小时）

        Returns:
            预签名 URL
        """
        client = self._get_client()
        key = self._object_key(uri)

        try:
            return client.generate_presigned_url(
                ClientMethod=operation,
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=expires_in,
            )
        except Exception as e:
            raise ObjectStoreError(
                f"生成预签名 URL 失败: {uri}",
                {"uri": uri, "key": key, "operation": operation, "error": str(e)},
            )

    def get_info(self, uri: str) -> Dict[str, Any]:
        """
        获取制品元数据（流式计算哈希，避免大对象全量入内存）

        Args:
            uri: 制品 URI

        Returns:
            {uri, sha256, size_bytes}

        Raises:
            ArtifactNotFoundError: 制品不存在
        """
        client = self._get_client()
        key = self._object_key(uri)

        try:
            # 先尝试从元数据获取 sha256
            head = client.head_object(Bucket=self.bucket, Key=key)
            size_bytes = head.get("ContentLength", 0)

            # 检查元数据中是否有 sha256
            metadata = head.get("Metadata", {})
            if "sha256" in metadata:
                return {
                    "uri": uri,
                    "sha256": metadata["sha256"],
                    "size_bytes": size_bytes,
                }

            # 元数据无 sha256，流式计算
            hasher = hashlib.sha256()
            for chunk in self.get_stream(uri):
                hasher.update(chunk)

            return {
                "uri": uri,
                "sha256": hasher.hexdigest(),
                "size_bytes": size_bytes,
            }

        except Exception as e:
            classified = self._classify_error(e, uri, key)
            if isinstance(classified, ArtifactNotFoundError):
                raise classified
            raise


# =============================================================================
# Store 工厂函数
# =============================================================================


def get_artifact_store(
    backend: Optional[str] = None,
    **kwargs,
) -> ArtifactStore:
    """
    获取制品存储实例

    Args:
        backend: 后端类型（local, file, object），默认读取环境变量或使用 local
        **kwargs: 传递给具体 Store 的参数
            - local 后端: root, allowed_prefixes, file_mode, dir_mode, overwrite_policy
            - file 后端: allowed_roots, use_atomic_write, file_mode, dir_mode, overwrite_policy
            - object 后端: endpoint, access_key, secret_key, bucket, region, prefix,
                           sse, storage_class, acl, connect_timeout, read_timeout, retries,
                           max_size_bytes, multipart_threshold, multipart_chunk_size

    Returns:
        ArtifactStore 实例

    Raises:
        ValueError: 无效的后端类型

    示例:
        # 使用默认 local 后端
        store = get_artifact_store()

        # 显式指定后端
        store = get_artifact_store("object", bucket="my-artifacts")

        # file 后端带安全限制
        store = get_artifact_store("file", allowed_roots=["/mnt/artifacts"])

        # local 后端带策略配置
        store = get_artifact_store("local", 
            root="/mnt/artifacts",
            overwrite_policy="deny",
            file_mode=0o644
        )

        # 通过环境变量配置
        # export ENGRAM_ARTIFACTS_BACKEND=object
        # export ENGRAM_S3_BUCKET=my-artifacts
        store = get_artifact_store()
    """
    if backend is None:
        backend = os.environ.get(ENV_ARTIFACTS_BACKEND, BACKEND_LOCAL)

    backend = backend.lower()

    if backend == BACKEND_LOCAL:
        # 提取 local 后端支持的参数
        local_kwargs = {
            k: v for k, v in kwargs.items()
            if k in ("root", "allowed_prefixes", "file_mode", "dir_mode", "overwrite_policy", "read_only")
        }
        return LocalArtifactsStore(**local_kwargs)
    elif backend == BACKEND_FILE:
        # 提取 file 后端支持的参数
        file_kwargs = {
            k: v for k, v in kwargs.items()
            if k in ("allowed_roots", "use_atomic_write", "file_mode", "dir_mode", "overwrite_policy", "read_only")
        }
        return FileUriStore(**file_kwargs)
    elif backend == BACKEND_OBJECT:
        # 提取 object 后端支持的参数
        object_kwargs = {
            k: v for k, v in kwargs.items()
            if k in (
                "endpoint", "access_key", "secret_key", "bucket", "region", "prefix",
                "allowed_prefixes",
                "sse", "storage_class", "acl", "connect_timeout", "read_timeout", "retries",
                "max_size_bytes", "multipart_threshold", "multipart_chunk_size",
                "verify_ssl", "ca_bundle", "addressing_style", "read_only"
            )
        }
        return ObjectStore(**object_kwargs)
    else:
        raise ValueError(
            f"无效的制品存储后端: {backend}，有效值: {', '.join(sorted(VALID_BACKENDS))}"
        )


def get_artifact_store_from_config(config: Any) -> ArtifactStore:
    """
    从 ArtifactsConfig 对象创建 Store 实例
    
    Args:
        config: ArtifactsConfig 对象（来自 config.py）
    
    Returns:
        ArtifactStore 实例
    
    示例:
        from engram.logbook.config import get_app_config
        from engram.logbook.artifact_store import get_artifact_store_from_config
        
        app_config = get_app_config()
        store = get_artifact_store_from_config(app_config.artifacts)
    """
    backend = config.backend
    
    policy_cfg = config.policy if hasattr(config, 'policy') else None
    read_only = policy_cfg.read_only if policy_cfg else False
    
    if backend == BACKEND_LOCAL:
        return LocalArtifactsStore(
            root=config.root,
            allowed_prefixes=config.allowed_prefixes,
            file_mode=policy_cfg.file_mode if policy_cfg else None,
            dir_mode=policy_cfg.dir_mode if policy_cfg else None,
            overwrite_policy=policy_cfg.overwrite_policy if policy_cfg else OVERWRITE_ALLOW,
            read_only=read_only,
        )
    elif backend == BACKEND_FILE:
        file_cfg = config.file if hasattr(config, 'file') else None
        return FileUriStore(
            allowed_roots=file_cfg.allowed_roots if file_cfg else None,
            use_atomic_write=file_cfg.use_atomic_write if file_cfg else True,
            file_mode=policy_cfg.file_mode if policy_cfg else None,
            dir_mode=policy_cfg.dir_mode if policy_cfg else None,
            overwrite_policy=policy_cfg.overwrite_policy if policy_cfg else OVERWRITE_ALLOW,
            read_only=read_only,
        )
    elif backend == BACKEND_OBJECT:
        obj_cfg = config.object if hasattr(config, 'object') else None
        
        # 获取 allowed_prefixes：优先使用 object 子配置，回退到顶层 allowed_prefixes
        object_allowed_prefixes = None
        if obj_cfg and hasattr(obj_cfg, 'allowed_prefixes'):
            object_allowed_prefixes = obj_cfg.allowed_prefixes
        if object_allowed_prefixes is None and hasattr(config, 'allowed_prefixes'):
            # 回退使用顶层 allowed_prefixes（与 local 后端共用配置）
            object_allowed_prefixes = config.allowed_prefixes
        
        # 优先使用新配置结构，回退到旧配置属性
        return ObjectStore(
            endpoint=config.object_endpoint,  # 仍从环境变量或旧属性获取
            bucket=config.object_bucket,
            region=obj_cfg.region if obj_cfg else config.object_region,
            prefix=obj_cfg.prefix if obj_cfg else config.object_prefix,
            allowed_prefixes=object_allowed_prefixes,
            sse=obj_cfg.sse if obj_cfg else config.object_sse,
            storage_class=obj_cfg.storage_class if obj_cfg else config.object_storage_class,
            acl=obj_cfg.acl if obj_cfg else config.object_acl,
            connect_timeout=obj_cfg.connect_timeout if obj_cfg else config.object_connect_timeout,
            read_timeout=obj_cfg.read_timeout if obj_cfg else config.object_read_timeout,
            retries=obj_cfg.retries if obj_cfg else config.object_retries,
            max_size_bytes=policy_cfg.max_size_bytes if policy_cfg else config.object_max_size_bytes,
            multipart_threshold=obj_cfg.multipart_threshold if obj_cfg else config.object_multipart_threshold,
            multipart_chunk_size=obj_cfg.multipart_chunk_size if obj_cfg else config.object_multipart_chunk_size,
            verify_ssl=obj_cfg.verify_ssl if obj_cfg else True,
            ca_bundle=obj_cfg.ca_bundle if obj_cfg else None,
            addressing_style=obj_cfg.addressing_style if obj_cfg else "auto",
            read_only=read_only,
        )
    else:
        raise ValueError(
            f"无效的制品存储后端: {backend}，有效值: {', '.join(sorted(VALID_BACKENDS))}"
        )


# 全局默认 Store 实例（惰性初始化）
_default_store: Optional[ArtifactStore] = None


def get_default_store() -> ArtifactStore:
    """
    获取全局默认 Store 实例

    Returns:
        ArtifactStore 实例
    """
    global _default_store
    if _default_store is None:
        _default_store = get_artifact_store()
    return _default_store


def reset_default_store() -> None:
    """重置全局默认 Store（用于测试）"""
    global _default_store
    _default_store = None
