"""
engram_step1.config - 配置管理模块

支持:
- CLI --config 参数覆盖
- 环境变量 ENGRAM_STEP1_CONFIG 指定配置文件路径
- TOML 格式配置文件

优先级: --config > ENGRAM_STEP1_CONFIG > ./.agentx/config.toml > ~/.agentx/config.toml
"""

import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

# 敏感信息日志 logger
_auth_logger = logging.getLogger(__name__ + ".auth")

from .errors import ConfigError, ConfigNotFoundError, ConfigParseError

# 环境变量名称
ENV_CONFIG_PATH = "ENGRAM_STEP1_CONFIG"

# 默认配置文件搜索路径（按优先级）
DEFAULT_CONFIG_PATHS = [
    Path("./.agentx/config.toml"),
    Path.home() / ".agentx" / "config.toml",
]


# === 规范化配置对象 ===


@dataclass
class PostgresConfig:
    """PostgreSQL 数据库配置"""

    dsn: str
    pool_min_size: int = 1
    pool_max_size: int = 10
    connect_timeout: float = 10.0
    # 管理员 DSN（可选，用于自动创建数据库）
    # 当目标数据库不存在时，使用此 DSN 连接服务器创建数据库
    admin_dsn: Optional[str] = None

    def __post_init__(self):
        if not self.dsn:
            raise ConfigError(
                "配置项 [postgres].dsn 不能为空",
                {"section": "postgres", "key": "dsn"},
            )


@dataclass
class ProjectConfig:
    """项目配置"""

    project_key: str
    description: str = ""
    tags: list = field(default_factory=list)

    def __post_init__(self):
        if not self.project_key:
            raise ConfigError(
                "配置项 [project].project_key 不能为空",
                {"section": "project", "key": "project_key"},
            )


@dataclass
class LoggingConfig:
    """日志配置"""

    level: str = "INFO"
    format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    file: Optional[str] = None


# === Artifacts 子配置类 ===

# 有效的覆盖策略
VALID_OVERWRITE_POLICIES = {"allow", "deny", "allow_same_hash"}

# 有效的 SSE 类型
VALID_SSE_TYPES = {"AES256", "aws:kms"}

# 有效的存储类别
VALID_STORAGE_CLASSES = {
    "STANDARD", "STANDARD_IA", "ONEZONE_IA", 
    "GLACIER", "DEEP_ARCHIVE", "INTELLIGENT_TIERING"
}

# 有效的 ACL 策略
VALID_ACL_POLICIES = {
    "private", "public-read", "public-read-write",
    "authenticated-read", "aws-exec-read", 
    "bucket-owner-read", "bucket-owner-full-control"
}


# 环境变量名称：只读模式
ENV_ARTIFACTS_READ_ONLY = "ENGRAM_ARTIFACTS_READ_ONLY"


@dataclass
class ArtifactsPolicyConfig:
    """
    制品存储策略配置
    
    控制路径限制、覆盖策略、大小限制等安全与行为策略
    
    环境变量覆盖:
        ENGRAM_ARTIFACTS_READ_ONLY   设为 true/1/yes 启用只读模式，禁止写入
    """
    
    # 文件权限模式（八进制，如 0o644）
    file_mode: Optional[int] = None
    
    # 目录权限模式（八进制，如 0o755）
    dir_mode: Optional[int] = None
    
    # 覆盖策略: allow | deny | allow_same_hash
    overwrite_policy: str = "allow"
    
    # 最大制品大小限制（字节），0 = 无限制
    max_size_bytes: int = 0
    
    # 路径最大长度限制（字节）
    max_path_length: int = 4096
    
    # 只读模式：禁止所有写入操作
    # 优先级: 环境变量 ENGRAM_ARTIFACTS_READ_ONLY > 配置文件
    read_only: bool = False
    
    def __post_init__(self):
        # 校验覆盖策略
        if self.overwrite_policy not in VALID_OVERWRITE_POLICIES:
            raise ConfigError(
                f"无效的覆盖策略: {self.overwrite_policy}，有效值: {', '.join(sorted(VALID_OVERWRITE_POLICIES))}",
                {"section": "artifacts.policy", "key": "overwrite_policy", "value": self.overwrite_policy},
            )
        
        # 校验大小限制
        if self.max_size_bytes < 0:
            raise ConfigError(
                f"最大制品大小不能为负数: {self.max_size_bytes}",
                {"section": "artifacts.policy", "key": "max_size_bytes", "value": self.max_size_bytes},
            )
        
        # 校验路径长度限制
        if self.max_path_length <= 0:
            raise ConfigError(
                f"路径最大长度必须为正数: {self.max_path_length}",
                {"section": "artifacts.policy", "key": "max_path_length", "value": self.max_path_length},
            )
        
        # 校验文件权限模式（如果指定）
        if self.file_mode is not None:
            if not isinstance(self.file_mode, int) or self.file_mode < 0 or self.file_mode > 0o777:
                raise ConfigError(
                    f"无效的文件权限模式: {oct(self.file_mode) if isinstance(self.file_mode, int) else self.file_mode}，有效范围: 0o000-0o777",
                    {"section": "artifacts.policy", "key": "file_mode", "value": self.file_mode},
                )
        
        # 校验目录权限模式（如果指定）
        if self.dir_mode is not None:
            if not isinstance(self.dir_mode, int) or self.dir_mode < 0 or self.dir_mode > 0o777:
                raise ConfigError(
                    f"无效的目录权限模式: {oct(self.dir_mode) if isinstance(self.dir_mode, int) else self.dir_mode}，有效范围: 0o000-0o777",
                    {"section": "artifacts.policy", "key": "dir_mode", "value": self.dir_mode},
                )


@dataclass
class ArtifactsFileConfig:
    """
    file 后端配置（file:// URI 直读写）
    """
    
    # 允许读写的根路径列表（安全策略）
    # - None: 允许所有路径（默认，适合开发环境）
    # - []: 拒绝所有路径
    # - ["path1", "path2"]: 只允许这些根路径
    allowed_roots: Optional[list] = None
    
    # 是否使用原子写入（临时文件 + rename）
    use_atomic_write: bool = False
    
    def __post_init__(self):
        # 校验 allowed_roots 类型
        if self.allowed_roots is not None:
            if not isinstance(self.allowed_roots, list):
                raise ConfigError(
                    f"allowed_roots 必须为列表类型，当前: {type(self.allowed_roots).__name__}",
                    {"section": "artifacts.file", "key": "allowed_roots", "value": self.allowed_roots},
                )
            for idx, root in enumerate(self.allowed_roots):
                if not isinstance(root, str):
                    raise ConfigError(
                        f"allowed_roots[{idx}] 必须为字符串，当前: {type(root).__name__}",
                        {"section": "artifacts.file", "key": f"allowed_roots[{idx}]", "value": root},
                    )


@dataclass
class ArtifactsObjectConfig:
    """
    对象存储配置（object 后端）
    
    【重要】敏感凭证必须通过环境变量注入，禁止明文写入配置文件！
    
    必须使用环境变量:
        ENGRAM_S3_ENDPOINT     S3/MinIO 端点 URL
        ENGRAM_S3_ACCESS_KEY   访问密钥（禁止写入配置文件）
        ENGRAM_S3_SECRET_KEY   密钥（禁止写入配置文件）
        ENGRAM_S3_BUCKET       存储桶名称
        ENGRAM_S3_VERIFY_SSL   SSL 证书验证 (true/false)
        ENGRAM_S3_CA_BUNDLE    自定义 CA 证书路径（可选）
    """
    
    # 对象键前缀（可选，非敏感）
    prefix: str = ""
    
    # 允许的 key 前缀列表（安全策略）
    # - None: 允许所有路径（默认，适合开发环境）
    # - []: 拒绝所有路径
    # - ["scm/", "attachments/"]: 只允许这些前缀
    # 注意：验证时使用 prefix + uri 的完整 key
    allowed_prefixes: Optional[list] = None
    
    # 存储区域（非敏感）
    region: str = "us-east-1"
    
    # 服务端加密类型: AES256 | aws:kms
    sse: Optional[str] = None
    
    # 存储类别: STANDARD | STANDARD_IA | GLACIER 等
    storage_class: Optional[str] = None
    
    # ACL 策略: private | public-read 等
    acl: Optional[str] = None
    
    # 连接超时秒数
    connect_timeout: float = 10.0
    
    # 读取超时秒数
    read_timeout: float = 60.0
    
    # 最大重试次数
    retries: int = 3
    
    # Multipart 上传阈值（字节），默认 5MB
    multipart_threshold: int = 5242880
    
    # Multipart 分片大小（字节），默认 8MB
    multipart_chunk_size: int = 8388608
    
    # SSL 证书验证
    # - True（默认）：验证服务器证书（生产环境必须）
    # - False：跳过证书验证（仅用于开发环境自签名证书）
    # - 字符串：自定义 CA 证书路径
    verify_ssl: bool = True
    
    # 自定义 CA 证书包路径（可选）
    # 当 verify_ssl=True 且需要使用自签名 CA 时使用
    # 优先级: ca_bundle > verify_ssl (当 ca_bundle 非空时)
    ca_bundle: Optional[str] = None
    
    # S3 地址寻址风格: auto | path | virtual
    # - auto: 自动选择（默认，boto3 根据 endpoint 和 bucket 决定）
    # - path: 路径风格 (http://endpoint/bucket/key)，适用于 MinIO 等兼容存储
    # - virtual: 虚拟主机风格 (http://bucket.endpoint/key)，AWS S3 默认
    addressing_style: str = "auto"
    
    def __post_init__(self):
        # 校验 allowed_prefixes 类型
        if self.allowed_prefixes is not None:
            if not isinstance(self.allowed_prefixes, list):
                raise ConfigError(
                    f"allowed_prefixes 必须为列表类型，当前: {type(self.allowed_prefixes).__name__}",
                    {"section": "artifacts.object", "key": "allowed_prefixes", "value": self.allowed_prefixes},
                )
            for idx, prefix in enumerate(self.allowed_prefixes):
                if not isinstance(prefix, str):
                    raise ConfigError(
                        f"allowed_prefixes[{idx}] 必须为字符串，当前: {type(prefix).__name__}",
                        {"section": "artifacts.object", "key": f"allowed_prefixes[{idx}]", "value": prefix},
                    )
        
        # 校验 SSE 类型
        if self.sse is not None and self.sse not in VALID_SSE_TYPES:
            raise ConfigError(
                f"无效的 SSE 类型: {self.sse}，有效值: {', '.join(sorted(VALID_SSE_TYPES))}",
                {"section": "artifacts.object", "key": "sse", "value": self.sse},
            )
        
        # 校验存储类别
        if self.storage_class is not None and self.storage_class not in VALID_STORAGE_CLASSES:
            raise ConfigError(
                f"无效的存储类别: {self.storage_class}，有效值: {', '.join(sorted(VALID_STORAGE_CLASSES))}",
                {"section": "artifacts.object", "key": "storage_class", "value": self.storage_class},
            )
        
        # 校验 ACL 策略
        if self.acl is not None and self.acl not in VALID_ACL_POLICIES:
            raise ConfigError(
                f"无效的 ACL 策略: {self.acl}，有效值: {', '.join(sorted(VALID_ACL_POLICIES))}",
                {"section": "artifacts.object", "key": "acl", "value": self.acl},
            )
        
        # 校验超时配置
        if self.connect_timeout <= 0:
            raise ConfigError(
                f"连接超时必须为正数: {self.connect_timeout}",
                {"section": "artifacts.object", "key": "connect_timeout", "value": self.connect_timeout},
            )
        
        if self.read_timeout <= 0:
            raise ConfigError(
                f"读取超时必须为正数: {self.read_timeout}",
                {"section": "artifacts.object", "key": "read_timeout", "value": self.read_timeout},
            )
        
        # 校验重试次数
        if self.retries < 0:
            raise ConfigError(
                f"重试次数不能为负数: {self.retries}",
                {"section": "artifacts.object", "key": "retries", "value": self.retries},
            )
        
        # 校验 Multipart 配置
        if self.multipart_threshold < 5242880:  # 5MB 是 S3 最小分片大小
            raise ConfigError(
                f"Multipart 阈值不能小于 5MB (5242880 字节): {self.multipart_threshold}",
                {"section": "artifacts.object", "key": "multipart_threshold", "value": self.multipart_threshold},
            )
        
        if self.multipart_chunk_size < 5242880:
            raise ConfigError(
                f"Multipart 分片大小不能小于 5MB (5242880 字节): {self.multipart_chunk_size}",
                {"section": "artifacts.object", "key": "multipart_chunk_size", "value": self.multipart_chunk_size},
            )
        
        # 校验 verify_ssl 类型
        if not isinstance(self.verify_ssl, bool):
            raise ConfigError(
                f"verify_ssl 必须为布尔值，当前: {type(self.verify_ssl).__name__}",
                {"section": "artifacts.object", "key": "verify_ssl", "value": self.verify_ssl},
            )
        
        # 校验 ca_bundle 路径（如果指定）
        if self.ca_bundle is not None:
            if not isinstance(self.ca_bundle, str):
                raise ConfigError(
                    f"ca_bundle 必须为字符串路径，当前: {type(self.ca_bundle).__name__}",
                    {"section": "artifacts.object", "key": "ca_bundle", "value": self.ca_bundle},
                )
            # 注意：不在配置解析阶段验证文件存在性，延迟到运行时检查
        
        # 校验 addressing_style
        valid_addressing_styles = {"auto", "path", "virtual"}
        if self.addressing_style not in valid_addressing_styles:
            raise ConfigError(
                f"无效的 addressing_style: {self.addressing_style}，有效值: {', '.join(sorted(valid_addressing_styles))}",
                {"section": "artifacts.object", "key": "addressing_style", "value": self.addressing_style},
            )


@dataclass
class ArtifactsConfig:
    """
    制品存储配置

    后端类型:
        - local: 本地文件系统（默认），使用 root 指定目录
        - file: file:// URI 直读写
        - object: 对象存储（S3/MinIO 兼容）

    环境变量优先级高于配置文件，用于凭证注入:
        ENGRAM_ARTIFACTS_BACKEND  覆盖 backend
        ENGRAM_ARTIFACTS_ROOT     覆盖 root (local 后端)
        ENGRAM_S3_ENDPOINT        对象存储端点
        ENGRAM_S3_ACCESS_KEY      对象存储访问密钥（禁止明文配置）
        ENGRAM_S3_SECRET_KEY      对象存储密钥（禁止明文配置）
        ENGRAM_S3_BUCKET          对象存储桶名称
        ENGRAM_S3_REGION          对象存储区域
    """

    backend: str = "local"  # local | file | object
    root: str = "./.agentx/artifacts"  # local 后端的根目录
    
    # local 后端: 允许的路径前缀（可选，安全策略）
    allowed_prefixes: Optional[list] = None

    # 子配置对象
    policy: ArtifactsPolicyConfig = field(default_factory=ArtifactsPolicyConfig)
    file: ArtifactsFileConfig = field(default_factory=ArtifactsFileConfig)
    object: ArtifactsObjectConfig = field(default_factory=ArtifactsObjectConfig)

    # === 旧版配置（向后兼容，建议迁移到子配置） ===
    # 对象存储配置（object 后端）
    # 敏感信息建议通过环境变量注入
    object_endpoint: Optional[str] = None
    object_bucket: Optional[str] = None
    object_region: str = "us-east-1"
    object_prefix: str = ""  # 对象键前缀

    # 对象存储高级配置
    object_sse: Optional[str] = None           # 服务端加密: AES256 | aws:kms
    object_storage_class: Optional[str] = None # 存储类别: STANDARD | STANDARD_IA | GLACIER 等
    object_acl: Optional[str] = None           # ACL 策略: private | public-read 等
    object_connect_timeout: float = 10.0       # 连接超时秒数
    object_read_timeout: float = 60.0          # 读取超时秒数
    object_retries: int = 3                    # 最大重试次数
    object_max_size_bytes: int = 0             # 最大制品大小 (0=无限制)
    object_multipart_threshold: int = 5242880  # Multipart 阈值 (5MB)
    object_multipart_chunk_size: int = 8388608 # Multipart 分片大小 (8MB)

    def __post_init__(self):
        valid_backends = {"local", "file", "object"}
        if self.backend not in valid_backends:
            raise ConfigError(
                f"无效的制品存储后端: {self.backend}，有效值: {', '.join(sorted(valid_backends))}",
                {"section": "artifacts", "key": "backend", "value": self.backend},
            )
        
        # 校验 allowed_prefixes 类型
        if self.allowed_prefixes is not None:
            if not isinstance(self.allowed_prefixes, list):
                raise ConfigError(
                    f"allowed_prefixes 必须为列表类型，当前: {type(self.allowed_prefixes).__name__}",
                    {"section": "artifacts", "key": "allowed_prefixes", "value": self.allowed_prefixes},
                )
            for idx, prefix in enumerate(self.allowed_prefixes):
                if not isinstance(prefix, str):
                    raise ConfigError(
                        f"allowed_prefixes[{idx}] 必须为字符串，当前: {type(prefix).__name__}",
                        {"section": "artifacts", "key": f"allowed_prefixes[{idx}]", "value": prefix},
                    )


@dataclass
class AppConfig:
    """应用程序完整配置（规范化对象）"""

    postgres: PostgresConfig
    project: ProjectConfig
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    artifacts: ArtifactsConfig = field(default_factory=ArtifactsConfig)

    # 配置文件来源路径（用于调试）
    _source_path: Optional[Path] = field(default=None, repr=False)

    @classmethod
    def from_dict(
        cls, data: dict, source_path: Optional[Path] = None
    ) -> "AppConfig":
        """从字典创建配置对象"""
        postgres_data = data.get("postgres", {})
        project_data = data.get("project", {})
        logging_data = data.get("logging", {})
        artifacts_data = data.get("artifacts", {})

        # 校验必需的配置节
        if not postgres_data:
            raise ConfigError(
                "缺少必需的配置节 [postgres]",
                {"section": "postgres"},
            )
        if not project_data:
            raise ConfigError(
                "缺少必需的配置节 [project]",
                {"section": "project"},
            )

        # admin_dsn 优先从环境变量读取
        admin_dsn = os.environ.get("ENGRAM_PG_ADMIN_DSN") or postgres_data.get("admin_dsn")
        
        # === 解析 artifacts 子配置 ===
        
        # 解析 [artifacts.policy] 配置
        policy_data = artifacts_data.get("policy", {})
        
        # 只读模式：优先环境变量覆盖
        env_read_only = os.environ.get(ENV_ARTIFACTS_READ_ONLY, "").lower()
        if env_read_only in ("true", "1", "yes", "on"):
            read_only = True
        elif env_read_only in ("false", "0", "no", "off"):
            read_only = False
        else:
            # 无有效环境变量，使用配置文件值
            read_only = policy_data.get("read_only", False)
        
        policy_config = ArtifactsPolicyConfig(
            file_mode=policy_data.get("file_mode"),
            dir_mode=policy_data.get("dir_mode"),
            overwrite_policy=policy_data.get("overwrite_policy", 
                artifacts_data.get("overwrite_policy", "allow")),  # 向后兼容
            max_size_bytes=policy_data.get("max_size_bytes",
                artifacts_data.get("object_max_size_bytes", 0)),  # 向后兼容
            max_path_length=policy_data.get("max_path_length", 4096),
            read_only=read_only,
        )
        
        # 解析 [artifacts.file] 配置
        file_data = artifacts_data.get("file", {})
        file_config = ArtifactsFileConfig(
            allowed_roots=file_data.get("allowed_roots"),
            use_atomic_write=file_data.get("use_atomic_write", False),
        )
        
        # 解析 [artifacts.object] 配置（优先新配置，回退旧配置）
        object_data = artifacts_data.get("object", {})
        object_config = ArtifactsObjectConfig(
            prefix=object_data.get("prefix", 
                artifacts_data.get("object_prefix", "")),
            allowed_prefixes=object_data.get("allowed_prefixes"),
            region=object_data.get("region", 
                artifacts_data.get("object_region", "us-east-1")),
            sse=object_data.get("sse", 
                artifacts_data.get("object_sse")),
            storage_class=object_data.get("storage_class", 
                artifacts_data.get("object_storage_class")),
            acl=object_data.get("acl", 
                artifacts_data.get("object_acl")),
            connect_timeout=object_data.get("connect_timeout", 
                artifacts_data.get("object_connect_timeout", 10.0)),
            read_timeout=object_data.get("read_timeout", 
                artifacts_data.get("object_read_timeout", 60.0)),
            retries=object_data.get("retries", 
                artifacts_data.get("object_retries", 3)),
            multipart_threshold=object_data.get("multipart_threshold", 
                artifacts_data.get("object_multipart_threshold", 5242880)),
            multipart_chunk_size=object_data.get("multipart_chunk_size", 
                artifacts_data.get("object_multipart_chunk_size", 8388608)),
            verify_ssl=object_data.get("verify_ssl", True),
            ca_bundle=object_data.get("ca_bundle"),
            addressing_style=object_data.get("addressing_style", "auto"),
        )
        
        return cls(
            postgres=PostgresConfig(
                dsn=postgres_data.get("dsn", ""),
                pool_min_size=postgres_data.get("pool_min_size", 1),
                pool_max_size=postgres_data.get("pool_max_size", 10),
                connect_timeout=postgres_data.get("connect_timeout", 10.0),
                admin_dsn=admin_dsn,
            ),
            project=ProjectConfig(
                project_key=project_data.get("project_key", ""),
                description=project_data.get("description", ""),
                tags=project_data.get("tags", []),
            ),
            logging=LoggingConfig(
                level=logging_data.get("level", "INFO"),
                format=logging_data.get(
                    "format", "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
                ),
                file=logging_data.get("file"),
            ),
            artifacts=ArtifactsConfig(
                backend=artifacts_data.get("backend", "local"),
                root=artifacts_data.get("root", "./.agentx/artifacts"),
                allowed_prefixes=artifacts_data.get("allowed_prefixes"),
                # 子配置对象
                policy=policy_config,
                file=file_config,
                object=object_config,
                # 旧版配置（向后兼容）
                object_endpoint=artifacts_data.get("object_endpoint"),
                object_bucket=artifacts_data.get("object_bucket"),
                object_region=artifacts_data.get("object_region", "us-east-1"),
                object_prefix=artifacts_data.get("object_prefix", ""),
                # 高级配置（向后兼容）
                object_sse=artifacts_data.get("object_sse"),
                object_storage_class=artifacts_data.get("object_storage_class"),
                object_acl=artifacts_data.get("object_acl"),
                object_connect_timeout=artifacts_data.get("object_connect_timeout", 10.0),
                object_read_timeout=artifacts_data.get("object_read_timeout", 60.0),
                object_retries=artifacts_data.get("object_retries", 3),
                object_max_size_bytes=artifacts_data.get("object_max_size_bytes", 0),
                object_multipart_threshold=artifacts_data.get("object_multipart_threshold", 5242880),
                object_multipart_chunk_size=artifacts_data.get("object_multipart_chunk_size", 8388608),
            ),
            _source_path=source_path,
        )


# === TOML 解析工具 ===


def _get_toml_parser():
    """获取 TOML 解析器（兼容 Python 3.11 以下版本）"""
    if sys.version_info >= (3, 11):
        import tomllib

        return tomllib
    else:
        try:
            import tomli as tomllib

            return tomllib
        except ImportError:
            raise ConfigError(
                "需要安装 tomli 包来解析 TOML 配置文件 (Python < 3.11)",
                {"hint": "pip install tomli"},
            )


def _parse_toml_file(path: Path) -> dict:
    """解析 TOML 文件"""
    tomllib = _get_toml_parser()
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception as e:
        raise ConfigParseError(
            f"配置文件解析失败: {e}",
            {"path": str(path), "error": str(e)},
        )


# === 配置管理类 ===


class Config:
    """配置管理类"""

    def __init__(self, config_path: Optional[str] = None):
        """
        初始化配置

        Args:
            config_path: 配置文件路径，优先级:
                1. 显式传入的 config_path（来自 --config 参数）
                2. 环境变量 ENGRAM_STEP1_CONFIG
                3. ./.agentx/config.toml
                4. ~/.agentx/config.toml
        """
        self._config_path: Optional[Path] = None
        self._data: dict = {}
        self._loaded = False
        self._app_config: Optional[AppConfig] = None

        # 确定配置文件路径
        self._resolve_config_path(config_path)

    def _resolve_config_path(self, explicit_path: Optional[str] = None) -> None:
        """解析配置文件路径"""
        # 1. 显式指定的路径（--config 参数）
        if explicit_path:
            path = Path(explicit_path)
            if not path.exists():
                raise ConfigNotFoundError(
                    f"指定的配置文件不存在: {explicit_path}",
                    {"path": str(path.absolute())},
                )
            self._config_path = path
            return

        # 2. 环境变量
        env_path = os.environ.get(ENV_CONFIG_PATH)
        if env_path:
            path = Path(env_path)
            if not path.exists():
                raise ConfigNotFoundError(
                    f"环境变量 {ENV_CONFIG_PATH} 指定的配置文件不存在: {env_path}",
                    {"path": str(path.absolute()), "env_var": ENV_CONFIG_PATH},
                )
            self._config_path = path
            return

        # 3. 默认搜索路径: ./.agentx/config.toml > ~/.agentx/config.toml
        for default_path in DEFAULT_CONFIG_PATHS:
            if default_path.exists():
                self._config_path = default_path
                return

        # 未找到配置文件，设为 None（延迟报错，允许仅使用环境变量配置）
        self._config_path = None

    def load(self) -> "Config":
        """加载配置文件"""
        if self._loaded:
            return self

        if self._config_path is None:
            # 无配置文件，使用空配置
            self._data = {}
            self._loaded = True
            return self

        self._data = _parse_toml_file(self._config_path)
        self._loaded = True
        return self

    def get(self, key: str, default: Any = None) -> Any:
        """
        获取配置值，支持点分隔的嵌套键

        Args:
            key: 配置键，如 "postgres.dsn" 或 "project.project_key"
            default: 默认值

        Returns:
            配置值或默认值
        """
        if not self._loaded:
            self.load()

        keys = key.split(".")
        value = self._data
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        return value

    def require(self, key: str) -> Any:
        """
        获取必需的配置值

        Args:
            key: 配置键

        Returns:
            配置值

        Raises:
            ConfigError: 如果配置项不存在或为空
        """
        value = self.get(key)
        if value is None or value == "":
            raise ConfigError(
                f"缺少必需的配置项: {key}",
                {"key": key, "config_path": str(self._config_path)},
            )
        return value

    def to_app_config(self) -> AppConfig:
        """
        将配置转换为规范化的 AppConfig 对象

        Returns:
            AppConfig 实例

        Raises:
            ConfigError: 如果配置校验失败
        """
        if self._app_config is not None:
            return self._app_config

        if not self._loaded:
            self.load()

        self._app_config = AppConfig.from_dict(self._data, self._config_path)
        return self._app_config

    def validate(self) -> bool:
        """
        校验配置完整性

        Returns:
            True 如果配置有效

        Raises:
            ConfigError: 如果配置无效
        """
        # 通过创建 AppConfig 来触发校验
        self.to_app_config()
        return True

    @property
    def config_path(self) -> Optional[Path]:
        """当前使用的配置文件路径"""
        return self._config_path

    @property
    def data(self) -> dict:
        """原始配置数据"""
        if not self._loaded:
            self.load()
        return self._data.copy()

    def __repr__(self) -> str:
        return f"Config(path={self._config_path}, loaded={self._loaded})"


# 全局配置实例（延迟初始化）
_global_config: Optional[Config] = None
_global_app_config: Optional[AppConfig] = None


def get_config(config_path: Optional[str] = None, reload: bool = False) -> Config:
    """
    获取全局配置实例

    Args:
        config_path: 配置文件路径（仅首次调用或 reload=True 时生效）
        reload: 是否强制重新加载

    Returns:
        Config 实例
    """
    global _global_config, _global_app_config
    if _global_config is None or reload:
        _global_config = Config(config_path)
        _global_app_config = None  # 重置 AppConfig 缓存
    return _global_config


def get_app_config(config_path: Optional[str] = None, reload: bool = False) -> AppConfig:
    """
    获取规范化的应用配置对象（CLI 复用入口）

    Args:
        config_path: 配置文件路径（仅首次调用或 reload=True 时生效）
        reload: 是否强制重新加载

    Returns:
        AppConfig 实例

    Raises:
        ConfigError: 如果配置无效
    """
    global _global_app_config
    if _global_app_config is None or reload:
        config = get_config(config_path, reload)
        _global_app_config = config.to_app_config()
    return _global_app_config


def add_config_argument(parser) -> None:
    """
    为 argparse.ArgumentParser 添加 --config 参数

    Args:
        parser: argparse.ArgumentParser 实例
    """
    parser.add_argument(
        "--config",
        "-c",
        metavar="PATH",
        help=f"配置文件路径（优先级: --config > {ENV_CONFIG_PATH} > ./.agentx/config.toml > ~/.agentx/config.toml）",
        dest="config_path",
    )


def init_config_from_args(args) -> AppConfig:
    """
    从 CLI 参数初始化配置（CLI 复用函数）

    Args:
        args: argparse 解析后的参数对象（需包含 config_path 属性）

    Returns:
        AppConfig 实例

    Raises:
        ConfigError: 如果配置无效
    """
    config_path = getattr(args, "config_path", None)
    return get_app_config(config_path, reload=True)


# === Artifacts 配置辅助方法 ===


def get_artifacts_config() -> Optional[ArtifactsConfig]:
    """
    获取已加载的 artifacts 配置

    如果 CLI 已加载配置，则返回 ArtifactsConfig 对象；
    否则返回 None（调用方应回退到环境变量）。

    Returns:
        ArtifactsConfig 实例或 None
    """
    global _global_app_config
    if _global_app_config is not None:
        return _global_app_config.artifacts
    return None


def get_artifacts_root_from_config() -> Optional[str]:
    """
    从配置中获取 artifacts root 路径

    优先级:
    1. [artifacts].root（新配置）
    2. [paths].artifacts_root（旧配置，向后兼容）

    如果 CLI 未加载配置，返回 None（调用方应回退到环境变量）。

    Returns:
        artifacts root 路径或 None
    """
    global _global_config, _global_app_config

    # 如果有规范化的 AppConfig，优先使用
    if _global_app_config is not None:
        return _global_app_config.artifacts.root

    # 如果有原始配置，尝试读取
    if _global_config is not None and _global_config._loaded:
        # 优先 [artifacts].root
        root = _global_config.get("artifacts.root")
        if root:
            return root
        # 回退 [paths].artifacts_root（向后兼容）
        return _global_config.get("paths.artifacts_root")

    return None


def get_artifacts_backend_from_config() -> Optional[str]:
    """
    从配置中获取 artifacts 后端类型

    如果 CLI 未加载配置，返回 None（调用方应回退到环境变量）。

    Returns:
        后端类型 (local/file/object) 或 None
    """
    global _global_app_config

    if _global_app_config is not None:
        return _global_app_config.artifacts.backend

    if _global_config is not None and _global_config._loaded:
        return _global_config.get("artifacts.backend")

    return None


# === Artifacts 有效配置获取（统一入口，供外部模块调用） ===

# 环境变量名称常量
ENV_ARTIFACTS_ROOT = "ENGRAM_ARTIFACTS_ROOT"
ENV_ARTIFACTS_BACKEND = "ENGRAM_ARTIFACTS_BACKEND"

# 默认值常量
DEFAULT_ARTIFACTS_ROOT = "./.agentx/artifacts"
DEFAULT_ARTIFACTS_BACKEND = "local"

# 弃用警告日志（使用模块级 logger）
_deprecation_logger = logging.getLogger(__name__ + ".deprecation")

# 已发出警告的标记（避免重复警告）
_deprecation_warned = {
    "artifacts_root": False,
    "paths.artifacts_root": False,
}


def _emit_deprecation_warning(legacy_key: str, new_key: str) -> None:
    """
    发出配置项弃用警告（每个 key 仅警告一次）
    
    Args:
        legacy_key: 旧配置键名
        new_key: 新配置键名
    """
    global _deprecation_warned
    if not _deprecation_warned.get(legacy_key, False):
        _deprecation_logger.warning(
            "配置项 '%s' 已弃用，请迁移到 '%s'",
            legacy_key, new_key,
        )
        _deprecation_warned[legacy_key] = True


def get_effective_artifacts_root() -> str:
    """
    获取有效的 artifacts root 路径（统一入口）

    优先级（从高到低）:
    1. 环境变量 ENGRAM_ARTIFACTS_ROOT
    2. 配置项 [artifacts].root（推荐）
    3. 配置项 [paths].artifacts_root（已弃用，向后兼容）
    4. 配置项 artifacts_root（顶层，已弃用，向后兼容）
    5. 默认值 ./.agentx/artifacts

    注意:
        使用 legacy 配置键时会发出弃用警告日志

    Returns:
        artifacts root 路径字符串
    """
    global _global_config, _global_app_config

    # 1. 环境变量优先
    env_root = os.environ.get(ENV_ARTIFACTS_ROOT)
    if env_root:
        return env_root

    # 2. 从规范化的 AppConfig 获取
    if _global_app_config is not None:
        return _global_app_config.artifacts.root

    # 3. 从原始配置读取（支持 legacy 回退）
    if _global_config is not None and _global_config._loaded:
        # 优先 [artifacts].root
        root = _global_config.get("artifacts.root")
        if root:
            return root

        # 回退 [paths].artifacts_root（已弃用）
        legacy_paths_root = _global_config.get("paths.artifacts_root")
        if legacy_paths_root:
            _emit_deprecation_warning("paths.artifacts_root", "artifacts.root")
            return legacy_paths_root

        # 回退顶层 artifacts_root（已弃用）
        legacy_root = _global_config.get("artifacts_root")
        if legacy_root:
            _emit_deprecation_warning("artifacts_root", "artifacts.root")
            return legacy_root

    # 4. 默认值
    return DEFAULT_ARTIFACTS_ROOT


def get_effective_artifacts_backend() -> str:
    """
    获取有效的 artifacts 后端类型（统一入口）

    优先级（从高到低）:
    1. 环境变量 ENGRAM_ARTIFACTS_BACKEND
    2. 配置项 [artifacts].backend（推荐）
    3. 默认值 local

    有效后端类型: local, file, object

    Returns:
        后端类型字符串
    """
    global _global_config, _global_app_config

    # 1. 环境变量优先
    env_backend = os.environ.get(ENV_ARTIFACTS_BACKEND)
    if env_backend:
        return env_backend

    # 2. 从规范化的 AppConfig 获取
    if _global_app_config is not None:
        return _global_app_config.artifacts.backend

    # 3. 从原始配置读取
    if _global_config is not None and _global_config._loaded:
        backend = _global_config.get("artifacts.backend")
        if backend:
            return backend

    # 4. 默认值
    return DEFAULT_ARTIFACTS_BACKEND


def is_config_loaded() -> bool:
    """
    检查全局配置是否已加载

    Returns:
        True 如果配置已加载
    """
    global _global_config
    return _global_config is not None and _global_config._loaded


# === SCM 配置兼容读取 ===

# SCM 配置键名映射表：新键 -> 旧键
SCM_KEY_FALLBACK_MAPPING = {
    # GitLab 映射
    "scm.gitlab.url": "gitlab.url",
    "scm.gitlab.project": "gitlab.project_id",
    "scm.gitlab.token": "gitlab.private_token",
    "scm.gitlab.default_branch": "gitlab.ref_name",
    "scm.gitlab.batch_size": "gitlab.batch_size",
    "scm.gitlab.request_timeout": "gitlab.request_timeout",
    "scm.gitlab.mr_state_filter": "gitlab.mr_state_filter",
    # SVN 映射
    "scm.svn.url": "svn.url",
    "scm.svn.username": "svn.username",
    "scm.svn.batch_size": "svn.batch_size",
    "scm.svn.overlap": "svn.overlap",
}

# 反向映射表：旧键 -> 新键
SCM_KEY_REVERSE_MAPPING = {v: k for k, v in SCM_KEY_FALLBACK_MAPPING.items()}

# SCM 配置弃用警告状态
_scm_deprecation_warned: dict = {}


def _emit_scm_deprecation_warning(legacy_key: str, new_key: str) -> None:
    """
    发出 SCM 配置项弃用警告（每个 key 仅警告一次）

    Args:
        legacy_key: 旧配置键名
        new_key: 新配置键名
    """
    global _scm_deprecation_warned
    if not _scm_deprecation_warned.get(legacy_key, False):
        _deprecation_logger.warning(
            "配置项 '%s' 已弃用，请迁移到 '%s'",
            legacy_key, new_key,
        )
        _scm_deprecation_warned[legacy_key] = True


def get_scm_config(key: str, default: Any = None, config: Optional["Config"] = None) -> Any:
    """
    获取 SCM 配置值，支持新旧配置键名的兼容读取

    优先级规则：
    1. 优先读取新的统一键名 (scm.gitlab.*, scm.svn.*)
    2. 如果新键不存在，回退到旧键名 (gitlab.*, svn.*)，并发出弃用警告

    键名映射关系：
    - scm.gitlab.url       <- 回退 gitlab.url
    - scm.gitlab.project   <- 回退 gitlab.project_id
    - scm.gitlab.token     <- 回退 gitlab.private_token
    - scm.gitlab.default_branch <- 回退 gitlab.ref_name
    - scm.gitlab.batch_size <- 回退 gitlab.batch_size
    - scm.gitlab.request_timeout <- 回退 gitlab.request_timeout
    - scm.svn.url          <- 回退 svn.url
    - scm.svn.batch_size   <- 回退 svn.batch_size
    - scm.svn.overlap      <- 回退 svn.overlap

    Args:
        key: 配置键，如 "scm.gitlab.url" 或简写 "gitlab.url"
        default: 默认值
        config: 可选的 Config 实例，默认使用全局配置

    Returns:
        配置值或默认值
    """
    if config is None:
        config = get_config()

    # 标准化键名：如果使用旧键名，先尝试新键
    if key in SCM_KEY_REVERSE_MAPPING:
        new_key = SCM_KEY_REVERSE_MAPPING[key]
        value = config.get(new_key)
        if value is not None:
            return value
        # 新键无值，使用原始旧键，并发出弃用警告
        legacy_value = config.get(key, default)
        if legacy_value is not None and legacy_value != default:
            _emit_scm_deprecation_warning(key, new_key)
        return legacy_value
    elif key in SCM_KEY_FALLBACK_MAPPING:
        # 使用新键名，优先读取新键
        value = config.get(key)
        if value is not None:
            return value
        # 新键无值，回退到旧键，并发出弃用警告
        fallback_key = SCM_KEY_FALLBACK_MAPPING[key]
        legacy_value = config.get(fallback_key, default)
        if legacy_value is not None and legacy_value != default:
            _emit_scm_deprecation_warning(fallback_key, key)
        return legacy_value
    else:
        # 普通键，直接读取
        return config.get(key, default)


def require_scm_config(key: str, config: Optional["Config"] = None) -> Any:
    """
    获取必需的 SCM 配置值，缺失时抛出明确错误

    Args:
        key: 配置键（推荐使用新键名如 "scm.gitlab.url"）
        config: 可选的 Config 实例

    Returns:
        配置值

    Raises:
        ConfigError: 配置项缺失，错误信息中明确提示新键名
    """
    value = get_scm_config(key, default=None, config=config)
    if value is None or value == "":
        # 确定要提示的新键名
        if key in SCM_KEY_REVERSE_MAPPING:
            new_key = SCM_KEY_REVERSE_MAPPING[key]
            legacy_key = key
        elif key in SCM_KEY_FALLBACK_MAPPING:
            new_key = key
            legacy_key = SCM_KEY_FALLBACK_MAPPING[key]
        else:
            new_key = key
            legacy_key = None

        hint = f"请在配置文件中设置 [{new_key.rsplit('.', 1)[0]}] 区块的 {new_key.rsplit('.', 1)[1]} 字段"
        if legacy_key:
            hint += f"（旧键名 {legacy_key} 已弃用）"

        raise ConfigError(
            f"缺少必需的 SCM 配置项: {new_key}",
            {"key": new_key, "legacy_key": legacy_key, "hint": hint},
        )
    return value


def get_gitlab_config(config: Optional["Config"] = None) -> dict:
    """
    获取 GitLab 配置（兼容新旧配置格式）

    返回统一格式的 GitLab 配置字典，包含：
    - url: GitLab 服务器 URL
    - project_id: 项目 ID 或路径
    - ref_name: 默认分支（可选）
    - batch_size: 批量大小（可选）
    - request_timeout: 请求超时（可选）
    - auth.mode: 认证模式（可选）
    - auth.token_env: token 环境变量名（可选）
    - auth.token_file: token 文件路径（可选）
    - auth.exec: token 获取命令（可选）

    配置优先级：
    1. scm.gitlab.* 配置
    2. gitlab.* 配置（向后兼容）

    注意：此函数不返回 token 明文，请使用 get_gitlab_auth() 获取认证凭证

    Args:
        config: 可选的 Config 实例

    Returns:
        GitLab 配置字典
    """
    if config is None:
        config = get_config()

    result = {
        "url": get_scm_config("scm.gitlab.url", config=config),
        "project_id": get_scm_config("scm.gitlab.project", config=config),
        "ref_name": get_scm_config("scm.gitlab.default_branch", config=config),
        "batch_size": get_scm_config("scm.gitlab.batch_size", config=config),
        "request_timeout": get_scm_config("scm.gitlab.request_timeout", config=config),
        "mr_state_filter": get_scm_config("scm.gitlab.mr_state_filter", config=config),
        # 认证配置（仅返回配置键名，不返回明文，使用 get_gitlab_auth() 获取凭证）
        "auth": {
            "mode": config.get("scm.gitlab.auth.mode"),
            "token_env": config.get("scm.gitlab.auth.token_env"),
            "token_file": config.get("scm.gitlab.auth.token_file"),
            "exec": config.get("scm.gitlab.auth.exec"),
        },
    }
    return result


def get_svn_config(config: Optional["Config"] = None) -> dict:
    """
    获取 SVN 配置（兼容新旧配置格式）

    返回统一格式的 SVN 配置字典，包含：
    - url: SVN 仓库 URL
    - username: SVN 用户名（可选）
    - batch_size: 批量大小（可选）
    - overlap: 重叠数量（可选）
    - non_interactive: 是否使用 --non-interactive 参数（默认 True）
    - trust_server_cert: 是否信任服务器证书 --trust-server-cert-failures=unknown-ca（默认 False）
    - command_timeout: SVN 命令超时秒数（默认 120）
    - password_env: 密码环境变量名（可选）
    - password_file: 密码文件路径（可选）

    配置优先级：
    1. 环境变量 SVN_USERNAME
    2. scm.svn.* 配置
    3. svn.* 配置（向后兼容）

    注意：此函数不返回密码明文，请使用 get_svn_auth() 获取认证凭证

    配置示例:
        [scm.svn]
        url = "svn://svn.example.com/project/trunk"
        username = "svn_user"
        password_env = "MY_SVN_PASSWORD"  # 或使用环境变量 SVN_PASSWORD
        password_file = "~/.secrets/svn.passwd"  # 或从文件读取
        non_interactive = true
        trust_server_cert = false
        command_timeout = 120

    Args:
        config: 可选的 Config 实例

    Returns:
        SVN 配置字典
    """
    if config is None:
        config = get_config()

    result = {
        "url": get_scm_config("scm.svn.url", config=config),
        "username": os.environ.get("SVN_USERNAME") or get_scm_config("scm.svn.username", config=config),
        "batch_size": get_scm_config("scm.svn.batch_size", config=config),
        "overlap": get_scm_config("scm.svn.overlap", config=config),
        # SVN 命令行安全选项
        "non_interactive": config.get("scm.svn.non_interactive", True),
        "trust_server_cert": config.get("scm.svn.trust_server_cert", False),
        "command_timeout": config.get("scm.svn.command_timeout", 120),
        # 密码配置（仅返回配置键名，不返回明文，使用 get_svn_auth() 获取凭证）
        "password_env": config.get("scm.svn.password_env"),
        "password_file": config.get("scm.svn.password_file"),
    }
    return result


# === SCM Incremental 增量同步配置 ===

# 默认值常量
DEFAULT_OVERLAP_SECONDS = 300  # 5 分钟
DEFAULT_TIME_WINDOW_DAYS = 365  # 首次同步默认拉取最近 1 年
DEFAULT_FORWARD_WINDOW_SECONDS = 3600  # 前向窗口默认 1 小时
DEFAULT_FORWARD_WINDOW_MIN_SECONDS = 60  # 前向窗口最小 1 分钟
DEFAULT_ADAPTIVE_SHRINK_FACTOR = 0.5  # 自适应缩小因子
DEFAULT_ADAPTIVE_GROW_FACTOR = 1.5  # 自适应增长因子
DEFAULT_ADAPTIVE_COMMIT_THRESHOLD = 200  # commit 数阈值，超过则缩小窗口


def get_incremental_config(config: Optional["Config"] = None) -> dict:
    """
    获取 SCM 增量同步配置

    配置键名优先级:
    1. scm.incremental.* (推荐)
    2. scm.gitlab.incremental.* (GitLab 特定)
    3. scm.gitlab.commits.* (GitLab commits 特定)

    配置项:
    - overlap_seconds: 向前重叠的秒数，用于防止边界丢失 (默认 300 秒)
    - time_window_days: 首次同步时拉取的天数范围 (默认 365 天)
    - forward_window_seconds: 前向时间窗口秒数 (默认 3600 秒)
    - forward_window_min_seconds: 前向时间窗口最小秒数 (默认 60 秒)
    - adaptive_shrink_factor: 自适应缩小因子 (默认 0.5)
    - adaptive_grow_factor: 自适应增长因子 (默认 1.5)
    - adaptive_commit_threshold: 触发窗口缩小的 commit 数阈值 (默认 200)

    配置示例:
        [scm.incremental]
        overlap_seconds = 300      # 5 分钟重叠窗口
        time_window_days = 365     # 首次同步拉取最近 1 年

        [scm.gitlab.commits]
        forward_window_seconds = 3600     # 前向窗口 1 小时
        forward_window_min_seconds = 60   # 最小窗口 1 分钟
        adaptive_shrink_factor = 0.5      # 缩小因子
        adaptive_grow_factor = 1.5        # 增长因子
        adaptive_commit_threshold = 200   # commit 数阈值

    Args:
        config: 可选的 Config 实例

    Returns:
        增量同步配置字典
    """
    if config is None:
        config = get_config()

    # 优先读取 scm.incremental.*，回退到 scm.gitlab.incremental.*
    overlap_seconds = (
        config.get("scm.incremental.overlap_seconds")
        or config.get("scm.gitlab.incremental.overlap_seconds")
        or DEFAULT_OVERLAP_SECONDS
    )
    time_window_days = (
        config.get("scm.incremental.time_window_days")
        or config.get("scm.gitlab.incremental.time_window_days")
        or DEFAULT_TIME_WINDOW_DAYS
    )
    
    # 前向窗口配置（优先 scm.gitlab.commits.*）
    forward_window_seconds = (
        config.get("scm.gitlab.commits.forward_window_seconds")
        or config.get("scm.incremental.forward_window_seconds")
        or DEFAULT_FORWARD_WINDOW_SECONDS
    )
    forward_window_min_seconds = (
        config.get("scm.gitlab.commits.forward_window_min_seconds")
        or config.get("scm.incremental.forward_window_min_seconds")
        or DEFAULT_FORWARD_WINDOW_MIN_SECONDS
    )
    
    # 自适应窗口配置
    adaptive_shrink_factor = (
        config.get("scm.gitlab.commits.adaptive_shrink_factor")
        or config.get("scm.incremental.adaptive_shrink_factor")
        or DEFAULT_ADAPTIVE_SHRINK_FACTOR
    )
    adaptive_grow_factor = (
        config.get("scm.gitlab.commits.adaptive_grow_factor")
        or config.get("scm.incremental.adaptive_grow_factor")
        or DEFAULT_ADAPTIVE_GROW_FACTOR
    )
    adaptive_commit_threshold = (
        config.get("scm.gitlab.commits.adaptive_commit_threshold")
        or config.get("scm.incremental.adaptive_commit_threshold")
        or DEFAULT_ADAPTIVE_COMMIT_THRESHOLD
    )

    return {
        "overlap_seconds": int(overlap_seconds),
        "time_window_days": int(time_window_days),
        "forward_window_seconds": int(forward_window_seconds),
        "forward_window_min_seconds": int(forward_window_min_seconds),
        "adaptive_shrink_factor": float(adaptive_shrink_factor),
        "adaptive_grow_factor": float(adaptive_grow_factor),
        "adaptive_commit_threshold": int(adaptive_commit_threshold),
    }


# === SCM Sync Mode 配置 ===

# 同步模式枚举值
SCM_SYNC_MODE_BEST_EFFORT = "best_effort"
SCM_SYNC_MODE_STRICT = "strict"
VALID_SCM_SYNC_MODES = {SCM_SYNC_MODE_BEST_EFFORT, SCM_SYNC_MODE_STRICT}

# 默认同步模式
DEFAULT_SCM_SYNC_MODE = SCM_SYNC_MODE_BEST_EFFORT


def get_scm_sync_mode(config: Optional["Config"] = None, cli_override: Optional[str] = None) -> str:
    """
    获取 SCM 同步模式

    优先级（从高到低）：
    1. CLI 参数覆盖 (--strict / --sync-mode)
    2. 配置项 scm.sync.mode
    3. 默认值 best_effort

    同步模式说明：
    - best_effort: 遇到不可恢复错误时允许推进游标，但记录降级与缺失类型
    - strict: 遇到不可恢复错误时，游标仅推进到"最后完全成功处理"的水位线

    不可恢复的错误类型：
    - 429 Rate Limited
    - 5xx Server Error
    - Timeout
    - 认证失败 (401/403)

    Args:
        config: 可选的 Config 实例
        cli_override: CLI 参数覆盖值（可以是 "strict"、"best_effort" 或布尔值字符串）

    Returns:
        同步模式字符串 ("strict" 或 "best_effort")
    """
    # 1. CLI 覆盖优先
    if cli_override is not None:
        # 支持 --strict (True) 或 --sync-mode=strict
        if isinstance(cli_override, bool):
            return SCM_SYNC_MODE_STRICT if cli_override else SCM_SYNC_MODE_BEST_EFFORT
        if cli_override.lower() in ("true", "1", "strict"):
            return SCM_SYNC_MODE_STRICT
        if cli_override.lower() in ("false", "0", "best_effort"):
            return SCM_SYNC_MODE_BEST_EFFORT
        # 验证值
        if cli_override.lower() not in VALID_SCM_SYNC_MODES:
            _deprecation_logger.warning(
                "无效的 sync_mode 值: %s，使用默认值 %s",
                cli_override, DEFAULT_SCM_SYNC_MODE,
            )
            return DEFAULT_SCM_SYNC_MODE
        return cli_override.lower()

    # 2. 从配置文件读取
    if config is None:
        config = get_config()

    mode = config.get("scm.sync.mode")
    if mode:
        mode_lower = mode.lower()
        if mode_lower in VALID_SCM_SYNC_MODES:
            return mode_lower
        _deprecation_logger.warning(
            "配置项 scm.sync.mode 值无效: %s，使用默认值 %s",
            mode, DEFAULT_SCM_SYNC_MODE,
        )

    # 3. 默认值
    return DEFAULT_SCM_SYNC_MODE


def is_strict_mode(config: Optional["Config"] = None, cli_override: Optional[str] = None) -> bool:
    """
    检查是否为严格模式

    Args:
        config: 可选的 Config 实例
        cli_override: CLI 参数覆盖值

    Returns:
        是否为严格模式
    """
    return get_scm_sync_mode(config, cli_override) == SCM_SYNC_MODE_STRICT


def get_scm_sync_config(config: Optional["Config"] = None) -> dict:
    """
    获取 SCM 同步配置

    配置键名：
    - scm.sync.mode: 同步模式 (strict/best_effort)
    - scm.sync.default_strict: 默认是否启用严格模式（可选，布尔值）
    - scm.sync.strict_on_auth_error: 认证错误时是否启用严格模式（可选，默认 True）
    - scm.sync.strict_on_rate_limit: 限流错误时是否启用严格模式（可选，默认 True）
    - scm.sync.strict_on_server_error: 服务器错误时是否启用严格模式（可选，默认 True）
    - scm.sync.strict_on_timeout: 超时错误时是否启用严格模式（可选，默认 True）

    配置示例:
        [scm.sync]
        mode = "strict"           # strict 或 best_effort
        # default_strict = true   # 可选，等价于 mode = "strict"

    Args:
        config: 可选的 Config 实例

    Returns:
        同步配置字典
    """
    if config is None:
        config = get_config()

    # 读取 mode（优先 scm.sync.mode，回退 default_strict）
    mode = config.get("scm.sync.mode")
    if not mode:
        # 如果 mode 未设置，检查 default_strict
        default_strict = config.get("scm.sync.default_strict")
        if default_strict is True or (isinstance(default_strict, str) and default_strict.lower() in ("true", "1")):
            mode = SCM_SYNC_MODE_STRICT
        else:
            mode = DEFAULT_SCM_SYNC_MODE
    else:
        mode = mode.lower() if mode.lower() in VALID_SCM_SYNC_MODES else DEFAULT_SCM_SYNC_MODE

    return {
        "mode": mode,
        "is_strict": mode == SCM_SYNC_MODE_STRICT,
        # 细粒度控制：哪些错误类型在 strict 模式下阻止游标推进
        "strict_on_auth_error": config.get("scm.sync.strict_on_auth_error", True),
        "strict_on_rate_limit": config.get("scm.sync.strict_on_rate_limit", True),
        "strict_on_server_error": config.get("scm.sync.strict_on_server_error", True),
        "strict_on_timeout": config.get("scm.sync.strict_on_timeout", True),
    }


# === SCM Backfill 配置 ===

# Backfill 默认值
DEFAULT_BACKFILL_REPAIR_WINDOW_HOURS = 24
DEFAULT_BACKFILL_CRON_HINT = "0 2 * * *"
DEFAULT_BACKFILL_MAX_CONCURRENT_JOBS = 4
DEFAULT_BACKFILL_UPDATE_WATERMARK = False

# Backfill 窗口限制默认值
# max_total_window_seconds: 回填窗口最大总秒数，防止单次回填范围过大
# 默认 30 天 = 30 * 24 * 3600 = 2592000 秒
DEFAULT_BACKFILL_MAX_TOTAL_WINDOW_SECONDS = 30 * 24 * 3600  # 30 天

# max_chunks_per_request: 每次回填请求最大 chunk 数，防止任务过多
# 默认 100 个 chunks
DEFAULT_BACKFILL_MAX_CHUNKS_PER_REQUEST = 100


class BackfillWindowExceededError(Exception):
    """回填窗口超限错误
    
    当回填请求的时间窗口或 chunk 数超过配置的限制时抛出。
    
    Attributes:
        error_type: 错误类型标识
        details: 结构化错误详情，用于 JSON 输出
    """
    
    def __init__(self, message: str, details: dict = None):
        super().__init__(message)
        self.error_type = "BACKFILL_WINDOW_EXCEEDED"
        self.details = details or {}
    
    def to_dict(self) -> dict:
        """转换为结构化字典，便于 JSON 输出"""
        return {
            "error_type": self.error_type,
            "message": str(self),
            "details": self.details,
        }


def get_backfill_config(config: Optional["Config"] = None) -> dict:
    """
    获取 SCM 回填配置

    配置键名:
    - scm.backfill.repair_window_hours: 修复窗口小时数 (默认 24)
    - scm.backfill.cron_hint: 建议的 cron 表达式 (默认 "0 2 * * *")
    - scm.backfill.max_concurrent_jobs: 最大并发任务数 (默认 4)
    - scm.backfill.default_update_watermark: 默认是否更新 watermark (默认 false)
    - scm.backfill.max_total_window_seconds: 回填窗口最大总秒数 (默认 2592000，即 30 天)
    - scm.backfill.max_chunks_per_request: 每次请求最大 chunk 数 (默认 100)

    配置示例:
        [scm.backfill]
        repair_window_hours = 24      # 默认修复窗口
        cron_hint = "0 2 * * *"       # 建议的 cron 表达式
        max_concurrent_jobs = 4       # 最大并发任务数
        default_update_watermark = false  # 回填默认不更新 watermark
        max_total_window_seconds = 2592000  # 回填窗口最大 30 天
        max_chunks_per_request = 100  # 每次请求最多 100 个 chunks

    Args:
        config: 可选的 Config 实例

    Returns:
        回填配置字典
    """
    if config is None:
        config = get_config()

    return {
        "repair_window_hours": config.get(
            "scm.backfill.repair_window_hours",
            DEFAULT_BACKFILL_REPAIR_WINDOW_HOURS,
        ),
        "cron_hint": config.get(
            "scm.backfill.cron_hint",
            DEFAULT_BACKFILL_CRON_HINT,
        ),
        "max_concurrent_jobs": config.get(
            "scm.backfill.max_concurrent_jobs",
            DEFAULT_BACKFILL_MAX_CONCURRENT_JOBS,
        ),
        "default_update_watermark": config.get(
            "scm.backfill.default_update_watermark",
            DEFAULT_BACKFILL_UPDATE_WATERMARK,
        ),
        "max_total_window_seconds": config.get(
            "scm.backfill.max_total_window_seconds",
            DEFAULT_BACKFILL_MAX_TOTAL_WINDOW_SECONDS,
        ),
        "max_chunks_per_request": config.get(
            "scm.backfill.max_chunks_per_request",
            DEFAULT_BACKFILL_MAX_CHUNKS_PER_REQUEST,
        ),
    }


def validate_backfill_window(
    total_window_seconds: int,
    chunk_count: int,
    config: Optional["Config"] = None,
) -> None:
    """
    校验回填窗口是否超限
    
    Args:
        total_window_seconds: 回填窗口总秒数
        chunk_count: chunk 数量
        config: 可选的 Config 实例
    
    Raises:
        BackfillWindowExceededError: 如果超过配置的限制
    """
    backfill_cfg = get_backfill_config(config)
    max_window_seconds = backfill_cfg["max_total_window_seconds"]
    max_chunks = backfill_cfg["max_chunks_per_request"]
    
    errors = []
    
    # 检查窗口时长限制
    if total_window_seconds > max_window_seconds:
        errors.append({
            "constraint": "max_total_window_seconds",
            "limit": max_window_seconds,
            "actual": total_window_seconds,
            "message": f"回填窗口 {total_window_seconds}s 超过限制 {max_window_seconds}s "
                       f"({max_window_seconds / 86400:.1f} 天)",
        })
    
    # 检查 chunk 数量限制
    if chunk_count > max_chunks:
        errors.append({
            "constraint": "max_chunks_per_request",
            "limit": max_chunks,
            "actual": chunk_count,
            "message": f"chunk 数量 {chunk_count} 超过限制 {max_chunks}",
        })
    
    if errors:
        # 构建详细错误信息
        messages = [e["message"] for e in errors]
        raise BackfillWindowExceededError(
            f"回填窗口超限: {'; '.join(messages)}",
            details={
                "errors": errors,
                "total_window_seconds": total_window_seconds,
                "chunk_count": chunk_count,
                "limits": {
                    "max_total_window_seconds": max_window_seconds,
                    "max_chunks_per_request": max_chunks,
                },
            },
        )


# === SCM Scheduler 配置 ===

# Scheduler 默认值
DEFAULT_SCHEDULER_GLOBAL_CONCURRENCY = 10
DEFAULT_SCHEDULER_PER_INSTANCE_CONCURRENCY = 3
DEFAULT_SCHEDULER_PER_TENANT_CONCURRENCY = 5
DEFAULT_SCHEDULER_SCAN_INTERVAL_SECONDS = 60
DEFAULT_SCHEDULER_MAX_ENQUEUE_PER_SCAN = 100
DEFAULT_SCHEDULER_ERROR_BUDGET_THRESHOLD = 0.3
DEFAULT_SCHEDULER_PAUSE_DURATION_SECONDS = 300

# Tenant 公平调度默认值
DEFAULT_SCHEDULER_ENABLE_TENANT_FAIRNESS = False
DEFAULT_SCHEDULER_TENANT_FAIRNESS_MAX_PER_ROUND = 1

# Claim 租户公平调度默认值
DEFAULT_CLAIM_ENABLE_TENANT_FAIR_CLAIM = False
DEFAULT_CLAIM_MAX_CONSECUTIVE_SAME_TENANT = 3
DEFAULT_CLAIM_MAX_TENANTS_PER_ROUND = 5

# === GitLab HTTP/Rate Limit 默认值 ===

# HTTP 请求速率限制（内存版令牌桶）
DEFAULT_GITLAB_RATE_LIMIT_ENABLED = False  # 默认关闭，保持向后兼容
DEFAULT_GITLAB_RATE_LIMIT_REQUESTS_PER_SECOND = 10.0
DEFAULT_GITLAB_RATE_LIMIT_BURST_SIZE = None  # None 表示等于 requests_per_second

# Postgres 分布式速率限制
DEFAULT_GITLAB_POSTGRES_RATE_LIMIT_ENABLED = False  # 默认关闭，保持向后兼容
DEFAULT_GITLAB_POSTGRES_RATE_LIMIT_RATE = 10.0
DEFAULT_GITLAB_POSTGRES_RATE_LIMIT_BURST = 20
DEFAULT_GITLAB_POSTGRES_RATE_LIMIT_MAX_WAIT = 60.0

# Postgres 分布式速率限制（tenant 维度）
# key 形如: gitlab:<host>:tenant:<id>
# 用于对每个 tenant 单独限流，避免某个 tenant 耗尽全局配额
DEFAULT_GITLAB_TENANT_RATE_LIMIT_ENABLED = False  # 默认关闭
DEFAULT_GITLAB_TENANT_RATE_LIMIT_RATE = 5.0  # tenant 级别更保守
DEFAULT_GITLAB_TENANT_RATE_LIMIT_BURST = 10
DEFAULT_GITLAB_TENANT_RATE_LIMIT_MAX_WAIT = 30.0


def get_scheduler_config(config: Optional["Config"] = None) -> dict:
    """
    获取 SCM Scheduler 配置
    
    配置键名:
    - scm.scheduler.global_concurrency: 全局最大队列深度，默认 10
    - scm.scheduler.per_instance_concurrency: 每 GitLab 实例并发限制，默认 3
    - scm.scheduler.per_tenant_concurrency: 每租户并发限制，默认 5
    - scm.scheduler.scan_interval_seconds: 扫描间隔秒数，默认 60
    - scm.scheduler.max_enqueue_per_scan: 每次扫描最大入队数，默认 100
    - scm.scheduler.error_budget_threshold: 错误预算阈值，默认 0.3
    - scm.scheduler.pause_duration_seconds: 暂停时长秒数，默认 300
    - scm.scheduler.enable_tenant_fairness: 启用按 tenant 分桶轮询策略，默认 False
    - scm.scheduler.tenant_fairness_max_per_round: 每轮每 tenant 最多入队数，默认 1
    
    环境变量覆盖:
    - SCM_SCHEDULER_GLOBAL_CONCURRENCY
    - SCM_SCHEDULER_PER_INSTANCE_CONCURRENCY
    - SCM_SCHEDULER_PER_TENANT_CONCURRENCY
    - SCM_SCHEDULER_SCAN_INTERVAL_SECONDS
    - SCM_SCHEDULER_MAX_ENQUEUE_PER_SCAN
    - SCM_SCHEDULER_ERROR_BUDGET_THRESHOLD
    - SCM_SCHEDULER_PAUSE_DURATION_SECONDS
    - SCM_SCHEDULER_ENABLE_TENANT_FAIRNESS
    - SCM_SCHEDULER_TENANT_FAIRNESS_MAX_PER_ROUND
    
    配置示例:
        [scm.scheduler]
        global_concurrency = 10
        per_instance_concurrency = 3
        per_tenant_concurrency = 5
        scan_interval_seconds = 60
        max_enqueue_per_scan = 100
        error_budget_threshold = 0.3
        pause_duration_seconds = 300
        # Tenant 公平调度配置
        enable_tenant_fairness = false
        tenant_fairness_max_per_round = 1
    
    Args:
        config: 可选的 Config 实例
    
    Returns:
        Scheduler 配置字典
    """
    if config is None:
        config = get_config()
    
    def _get_env_or_config(env_key: str, config_key: str, default, value_type=int):
        """优先环境变量，否则配置文件，最后默认值"""
        env_val = os.environ.get(env_key)
        if env_val:
            if value_type == float:
                return float(env_val)
            return int(env_val)
        return config.get(config_key, default)
    
    def _get_env_or_config_bool(env_key: str, config_key: str, default: bool) -> bool:
        """优先环境变量，否则配置文件，最后默认值（布尔类型）"""
        env_val = os.environ.get(env_key)
        if env_val:
            return env_val.lower() in ("true", "1", "yes", "on")
        cfg_val = config.get(config_key)
        if cfg_val is not None:
            if isinstance(cfg_val, bool):
                return cfg_val
            if isinstance(cfg_val, str):
                return cfg_val.lower() in ("true", "1", "yes", "on")
        return default
    
    return {
        "global_concurrency": _get_env_or_config(
            "SCM_SCHEDULER_GLOBAL_CONCURRENCY",
            "scm.scheduler.global_concurrency",
            DEFAULT_SCHEDULER_GLOBAL_CONCURRENCY,
        ),
        "per_instance_concurrency": _get_env_or_config(
            "SCM_SCHEDULER_PER_INSTANCE_CONCURRENCY",
            "scm.scheduler.per_instance_concurrency",
            DEFAULT_SCHEDULER_PER_INSTANCE_CONCURRENCY,
        ),
        "per_tenant_concurrency": _get_env_or_config(
            "SCM_SCHEDULER_PER_TENANT_CONCURRENCY",
            "scm.scheduler.per_tenant_concurrency",
            DEFAULT_SCHEDULER_PER_TENANT_CONCURRENCY,
        ),
        "scan_interval_seconds": _get_env_or_config(
            "SCM_SCHEDULER_SCAN_INTERVAL_SECONDS",
            "scm.scheduler.scan_interval_seconds",
            DEFAULT_SCHEDULER_SCAN_INTERVAL_SECONDS,
        ),
        "max_enqueue_per_scan": _get_env_or_config(
            "SCM_SCHEDULER_MAX_ENQUEUE_PER_SCAN",
            "scm.scheduler.max_enqueue_per_scan",
            DEFAULT_SCHEDULER_MAX_ENQUEUE_PER_SCAN,
        ),
        "error_budget_threshold": _get_env_or_config(
            "SCM_SCHEDULER_ERROR_BUDGET_THRESHOLD",
            "scm.scheduler.error_budget_threshold",
            DEFAULT_SCHEDULER_ERROR_BUDGET_THRESHOLD,
            value_type=float,
        ),
        "pause_duration_seconds": _get_env_or_config(
            "SCM_SCHEDULER_PAUSE_DURATION_SECONDS",
            "scm.scheduler.pause_duration_seconds",
            DEFAULT_SCHEDULER_PAUSE_DURATION_SECONDS,
        ),
        # Tenant 公平调度配置
        "enable_tenant_fairness": _get_env_or_config_bool(
            "SCM_SCHEDULER_ENABLE_TENANT_FAIRNESS",
            "scm.scheduler.enable_tenant_fairness",
            DEFAULT_SCHEDULER_ENABLE_TENANT_FAIRNESS,
        ),
        "tenant_fairness_max_per_round": _get_env_or_config(
            "SCM_SCHEDULER_TENANT_FAIRNESS_MAX_PER_ROUND",
            "scm.scheduler.tenant_fairness_max_per_round",
            DEFAULT_SCHEDULER_TENANT_FAIRNESS_MAX_PER_ROUND,
        ),
    }


# === SCM Claim 配置 ===


def get_claim_config(config: Optional["Config"] = None) -> dict:
    """
    获取 SCM Claim 配置（租户公平调度）
    
    配置键名:
    - scm.claim.enable_tenant_fair_claim: 启用租户公平调度，默认 False
    - scm.claim.max_consecutive_same_tenant: 单租户最大连续 claim 次数，默认 3
    - scm.claim.max_tenants_per_round: 每轮选取的最大租户数，默认 5
    
    环境变量覆盖:
    - SCM_CLAIM_ENABLE_TENANT_FAIR_CLAIM
    - SCM_CLAIM_MAX_CONSECUTIVE_SAME_TENANT
    - SCM_CLAIM_MAX_TENANTS_PER_ROUND
    
    配置示例:
        [scm.claim]
        enable_tenant_fair_claim = true       # 启用租户公平调度
        max_consecutive_same_tenant = 3       # 单租户最大连续 claim 3 次
        max_tenants_per_round = 5             # 每轮选取最多 5 个租户
    
    Args:
        config: 可选的 Config 实例
    
    Returns:
        Claim 配置字典
    """
    if config is None:
        config = get_config()
    
    def _get_env_or_config(env_key: str, config_key: str, default, value_type=int):
        """优先环境变量，否则配置文件，最后默认值"""
        env_val = os.environ.get(env_key)
        if env_val:
            if value_type == float:
                return float(env_val)
            return int(env_val)
        return config.get(config_key, default)
    
    def _get_env_or_config_bool(env_key: str, config_key: str, default: bool) -> bool:
        """优先环境变量，否则配置文件，最后默认值（布尔类型）"""
        env_val = os.environ.get(env_key)
        if env_val:
            return env_val.lower() in ("true", "1", "yes", "on")
        cfg_val = config.get(config_key)
        if cfg_val is not None:
            if isinstance(cfg_val, bool):
                return cfg_val
            if isinstance(cfg_val, str):
                return cfg_val.lower() in ("true", "1", "yes", "on")
        return default
    
    return {
        "enable_tenant_fair_claim": _get_env_or_config_bool(
            "SCM_CLAIM_ENABLE_TENANT_FAIR_CLAIM",
            "scm.claim.enable_tenant_fair_claim",
            DEFAULT_CLAIM_ENABLE_TENANT_FAIR_CLAIM,
        ),
        "max_consecutive_same_tenant": _get_env_or_config(
            "SCM_CLAIM_MAX_CONSECUTIVE_SAME_TENANT",
            "scm.claim.max_consecutive_same_tenant",
            DEFAULT_CLAIM_MAX_CONSECUTIVE_SAME_TENANT,
        ),
        "max_tenants_per_round": _get_env_or_config(
            "SCM_CLAIM_MAX_TENANTS_PER_ROUND",
            "scm.claim.max_tenants_per_round",
            DEFAULT_CLAIM_MAX_TENANTS_PER_ROUND,
        ),
    }


# === SCM Worker 配置 ===

# Worker 默认值
DEFAULT_WORKER_LEASE_SECONDS = 300  # 5 分钟
DEFAULT_WORKER_RENEW_INTERVAL_SECONDS = 60  # 1 分钟
DEFAULT_WORKER_MAX_RENEW_FAILURES = 3  # 最大续租失败次数


def get_worker_config(config: Optional["Config"] = None) -> dict:
    """
    获取 SCM Worker 配置
    
    配置键名:
    - scm.worker.lease_seconds: 任务租约时长（秒），默认 300
    - scm.worker.renew_interval_seconds: 续租间隔（秒），默认 60
    - scm.worker.max_renew_failures: 最大续租失败次数，超过则中止任务，默认 3
    
    环境变量覆盖:
    - SCM_WORKER_LEASE_SECONDS
    - SCM_WORKER_RENEW_INTERVAL_SECONDS
    - SCM_WORKER_MAX_RENEW_FAILURES
    
    配置示例:
        [scm.worker]
        lease_seconds = 300              # 任务租约时长 5 分钟
        renew_interval_seconds = 60      # 每 60 秒续租一次
        max_renew_failures = 3           # 连续 3 次续租失败则中止
    
    Args:
        config: 可选的 Config 实例
    
    Returns:
        Worker 配置字典
    """
    if config is None:
        config = get_config()
    
    # 优先环境变量
    lease_seconds = os.environ.get("SCM_WORKER_LEASE_SECONDS")
    if lease_seconds:
        lease_seconds = int(lease_seconds)
    else:
        lease_seconds = config.get("scm.worker.lease_seconds", DEFAULT_WORKER_LEASE_SECONDS)
    
    renew_interval_seconds = os.environ.get("SCM_WORKER_RENEW_INTERVAL_SECONDS")
    if renew_interval_seconds:
        renew_interval_seconds = int(renew_interval_seconds)
    else:
        renew_interval_seconds = config.get("scm.worker.renew_interval_seconds", DEFAULT_WORKER_RENEW_INTERVAL_SECONDS)
    
    max_renew_failures = os.environ.get("SCM_WORKER_MAX_RENEW_FAILURES")
    if max_renew_failures:
        max_renew_failures = int(max_renew_failures)
    else:
        max_renew_failures = config.get("scm.worker.max_renew_failures", DEFAULT_WORKER_MAX_RENEW_FAILURES)
    
    return {
        "lease_seconds": int(lease_seconds),
        "renew_interval_seconds": int(renew_interval_seconds),
        "max_renew_failures": int(max_renew_failures),
    }


# === SCM HTTP 配置 ===

# HTTP 请求默认值
DEFAULT_HTTP_TIMEOUT_SECONDS = 60.0
DEFAULT_HTTP_MAX_ATTEMPTS = 3
DEFAULT_HTTP_BACKOFF_BASE_SECONDS = 1.0
DEFAULT_HTTP_BACKOFF_MAX_SECONDS = 60.0


def get_http_config(config: Optional["Config"] = None) -> dict:
    """
    获取 SCM HTTP 请求配置

    配置键名:
    - scm.http.timeout_seconds: 请求超时秒数 (默认 60)
    - scm.http.max_attempts: 最大重试次数 (默认 3)
    - scm.http.backoff_base_seconds: 退避基础秒数 (默认 1.0)
    - scm.http.backoff_max_seconds: 退避最大秒数 (默认 60.0)
    - scm.gitlab.max_concurrency: 可选，GitLab 最大并发数

    配置示例:
        [scm.http]
        timeout_seconds = 60
        max_attempts = 3
        backoff_base_seconds = 1.0
        backoff_max_seconds = 60.0

        [scm.gitlab]
        max_concurrency = 5

    Args:
        config: 可选的 Config 实例

    Returns:
        HTTP 配置字典
    """
    if config is None:
        config = get_config()

    return {
        "timeout_seconds": config.get("scm.http.timeout_seconds", DEFAULT_HTTP_TIMEOUT_SECONDS),
        "max_attempts": config.get("scm.http.max_attempts", DEFAULT_HTTP_MAX_ATTEMPTS),
        "backoff_base_seconds": config.get("scm.http.backoff_base_seconds", DEFAULT_HTTP_BACKOFF_BASE_SECONDS),
        "backoff_max_seconds": config.get("scm.http.backoff_max_seconds", DEFAULT_HTTP_BACKOFF_MAX_SECONDS),
        "max_concurrency": config.get("scm.gitlab.max_concurrency"),
    }


def get_gitlab_rate_limit_config(config: Optional["Config"] = None) -> dict:
    """
    获取 GitLab 速率限制配置（统一入口）

    配置键名:
    - scm.gitlab.rate_limit_enabled: 启用内存版速率限制，默认 False
    - scm.gitlab.rate_limit_requests_per_second: 每秒请求数，默认 10.0
    - scm.gitlab.rate_limit_burst_size: 突发容量，默认 None（等于 requests_per_second）
    - scm.gitlab.postgres_rate_limit_enabled: 启用 Postgres 分布式速率限制，默认 False
    - scm.gitlab.postgres_rate_limit_rate: Postgres 令牌补充速率，默认 10.0
    - scm.gitlab.postgres_rate_limit_burst: Postgres 最大令牌容量，默认 20
    - scm.gitlab.postgres_rate_limit_max_wait: Postgres 最大等待秒数，默认 60.0

    配置示例:
        [scm.gitlab]
        # 内存版速率限制（单进程）
        rate_limit_enabled = false
        rate_limit_requests_per_second = 10.0
        rate_limit_burst_size = 10

        # Postgres 分布式速率限制（多 worker）
        postgres_rate_limit_enabled = false
        postgres_rate_limit_rate = 10.0
        postgres_rate_limit_burst = 20
        postgres_rate_limit_max_wait = 60.0

    注意:
        这两个速率限制开关默认都为 False，保持向后兼容。
        关闭时不改变原有逻辑（无速率限制）。

    Args:
        config: 可选的 Config 实例

    Returns:
        速率限制配置字典
    """
    if config is None:
        config = get_config()

    def _get_bool_config(key: str, default: bool) -> bool:
        """获取布尔配置值"""
        val = config.get(key)
        if val is None:
            return default
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in ("true", "1", "yes", "on")
        return default

    def _get_with_default(key: str, default):
        """获取配置值，None 时返回默认值"""
        val = config.get(key)
        return default if val is None else val

    return {
        # 内存版速率限制
        "rate_limit_enabled": _get_bool_config(
            "scm.gitlab.rate_limit_enabled",
            DEFAULT_GITLAB_RATE_LIMIT_ENABLED,
        ),
        "rate_limit_requests_per_second": _get_with_default(
            "scm.gitlab.rate_limit_requests_per_second",
            DEFAULT_GITLAB_RATE_LIMIT_REQUESTS_PER_SECOND,
        ),
        "rate_limit_burst_size": _get_with_default(
            "scm.gitlab.rate_limit_burst_size",
            DEFAULT_GITLAB_RATE_LIMIT_BURST_SIZE,
        ),
        # Postgres 分布式速率限制（实例维度）
        "postgres_rate_limit_enabled": _get_bool_config(
            "scm.gitlab.postgres_rate_limit_enabled",
            DEFAULT_GITLAB_POSTGRES_RATE_LIMIT_ENABLED,
        ),
        "postgres_rate_limit_rate": _get_with_default(
            "scm.gitlab.postgres_rate_limit_rate",
            DEFAULT_GITLAB_POSTGRES_RATE_LIMIT_RATE,
        ),
        "postgres_rate_limit_burst": _get_with_default(
            "scm.gitlab.postgres_rate_limit_burst",
            DEFAULT_GITLAB_POSTGRES_RATE_LIMIT_BURST,
        ),
        "postgres_rate_limit_max_wait": _get_with_default(
            "scm.gitlab.postgres_rate_limit_max_wait",
            DEFAULT_GITLAB_POSTGRES_RATE_LIMIT_MAX_WAIT,
        ),
        # Postgres 分布式速率限制（tenant 维度）
        # key 形如: gitlab:<host>:tenant:<id>
        "tenant_rate_limit_enabled": _get_bool_config(
            "scm.gitlab.tenant_rate_limit_enabled",
            DEFAULT_GITLAB_TENANT_RATE_LIMIT_ENABLED,
        ),
        "tenant_rate_limit_rate": _get_with_default(
            "scm.gitlab.tenant_rate_limit_rate",
            DEFAULT_GITLAB_TENANT_RATE_LIMIT_RATE,
        ),
        "tenant_rate_limit_burst": _get_with_default(
            "scm.gitlab.tenant_rate_limit_burst",
            DEFAULT_GITLAB_TENANT_RATE_LIMIT_BURST,
        ),
        "tenant_rate_limit_max_wait": _get_with_default(
            "scm.gitlab.tenant_rate_limit_max_wait",
            DEFAULT_GITLAB_TENANT_RATE_LIMIT_MAX_WAIT,
        ),
    }


# === SCM Bulk Thresholds 配置 ===

# Bulk 阈值默认值
DEFAULT_SVN_CHANGED_PATHS_THRESHOLD = 100
DEFAULT_GIT_TOTAL_CHANGES_THRESHOLD = 1000
DEFAULT_GIT_FILES_CHANGED_THRESHOLD = 50
DEFAULT_DIFF_SIZE_THRESHOLD = 1048576  # 1MB


# Bulk 阈值键名映射：新键 -> 旧键
BULK_THRESHOLDS_KEY_MAPPING = {
    "scm.bulk_thresholds.svn_changed_paths": "bulk.svn_changed_paths_threshold",
    "scm.bulk_thresholds.git_total_changes": "bulk.git_total_changes_threshold",
    "scm.bulk_thresholds.git_files_changed": "bulk.git_files_changed_threshold",
    "scm.bulk_thresholds.diff_size_bytes": "bulk.diff_size_threshold",
}

# Bulk 阈值弃用警告状态
_bulk_deprecation_warned: dict = {}


def get_bulk_thresholds(config: Optional["Config"] = None) -> dict:
    """
    获取 SCM Bulk Commit 阈值配置

    优先级规则：
    1. 优先读取新键名 scm.bulk_thresholds.*
    2. 若新键不存在，回退到旧键名 bulk.*，并发出弃用警告
    3. 若都不存在，使用默认值

    键名映射关系：
    - scm.bulk_thresholds.svn_changed_paths    <- 回退 bulk.svn_changed_paths_threshold
    - scm.bulk_thresholds.git_total_changes   <- 回退 bulk.git_total_changes_threshold
    - scm.bulk_thresholds.git_files_changed   <- 回退 bulk.git_files_changed_threshold
    - scm.bulk_thresholds.diff_size_bytes     <- 回退 bulk.diff_size_threshold

    配置示例:
        [scm.bulk_thresholds]
        svn_changed_paths = 100      # SVN changed_paths 数量阈值
        git_total_changes = 1000     # Git 变更行数阈值
        git_files_changed = 50       # Git 变更文件数阈值
        diff_size_bytes = 1048576    # diff 大小阈值 (1MB)

    Args:
        config: 可选的 Config 实例

    Returns:
        bulk 阈值配置字典，包含:
        - svn_changed_paths_threshold: SVN changed_paths 数量阈值
        - git_total_changes_threshold: Git 变更行数阈值
        - git_files_changed_threshold: Git 变更文件数阈值
        - diff_size_threshold: diff 大小阈值 (bytes)
    """
    global _bulk_deprecation_warned
    if config is None:
        config = get_config()

    def _get_threshold(new_key: str, old_key: str, default: int) -> int:
        """按优先级获取阈值：新键 > 旧键 > 默认值，使用旧键时发出弃用警告"""
        # 优先新键
        value = config.get(new_key)
        if value is not None:
            return int(value)
        # 回退旧键
        value = config.get(old_key)
        if value is not None:
            # 发出弃用警告（每个 key 仅警告一次）
            if not _bulk_deprecation_warned.get(old_key, False):
                _deprecation_logger.warning(
                    "配置项 '%s' 已弃用，请迁移到 '%s'",
                    old_key, new_key,
                )
                _bulk_deprecation_warned[old_key] = True
            return int(value)
        # 使用默认值
        return default

    return {
        "svn_changed_paths_threshold": _get_threshold(
            "scm.bulk_thresholds.svn_changed_paths",
            "bulk.svn_changed_paths_threshold",
            DEFAULT_SVN_CHANGED_PATHS_THRESHOLD,
        ),
        "git_total_changes_threshold": _get_threshold(
            "scm.bulk_thresholds.git_total_changes",
            "bulk.git_total_changes_threshold",
            DEFAULT_GIT_TOTAL_CHANGES_THRESHOLD,
        ),
        "git_files_changed_threshold": _get_threshold(
            "scm.bulk_thresholds.git_files_changed",
            "bulk.git_files_changed_threshold",
            DEFAULT_GIT_FILES_CHANGED_THRESHOLD,
        ),
        "diff_size_threshold": _get_threshold(
            "scm.bulk_thresholds.diff_size_bytes",
            "bulk.diff_size_threshold",
            DEFAULT_DIFF_SIZE_THRESHOLD,
        ),
    }


# === SCM 认证配置 ===


class GitLabAuthMode(str, Enum):
    """GitLab 认证模式枚举"""
    TOKEN = "token"       # 通用 token（默认，向后兼容）
    PAT = "pat"           # Personal Access Token
    OAUTH2 = "oauth2"     # OAuth2 Token
    JOB = "job"           # CI/CD Job Token


@dataclass
class GitLabAuth:
    """GitLab 认证凭证（已解析）"""
    mode: GitLabAuthMode
    token: str
    source: str  # 凭证来源描述（用于日志，不含敏感信息）

    def __repr__(self) -> str:
        # 防止敏感信息泄露到日志
        return f"GitLabAuth(mode={self.mode.value}, source='{self.source}', token=***)"


@dataclass
class SVNAuth:
    """SVN 认证凭证（已解析）"""
    username: Optional[str]
    password: Optional[str]
    source: str  # 凭证来源描述（用于日志，不含敏感信息）

    def __repr__(self) -> str:
        # 防止敏感信息泄露到日志
        has_password = "yes" if self.password else "no"
        return f"SVNAuth(username='{self.username}', has_password={has_password}, source='{self.source}')"


def _read_secret_from_file(file_path: str) -> Optional[str]:
    """
    从文件中读取敏感信息（如 token、password）

    Args:
        file_path: 文件路径

    Returns:
        文件内容（去除首尾空白），文件不存在或为空时返回 None
    """
    try:
        path = Path(file_path).expanduser()
        if path.exists() and path.is_file():
            content = path.read_text(encoding="utf-8").strip()
            if content:
                _auth_logger.debug("从文件读取凭证成功: %s", file_path)
                return content
            _auth_logger.debug("文件内容为空: %s", file_path)
        else:
            _auth_logger.debug("文件不存在: %s", file_path)
    except Exception as e:
        _auth_logger.warning("读取凭证文件失败: %s, 错误: %s", file_path, type(e).__name__)
    return None


def _read_secret_from_env(env_var: str) -> Optional[str]:
    """
    从环境变量中读取敏感信息

    Args:
        env_var: 环境变量名

    Returns:
        环境变量值，不存在或为空时返回 None
    """
    value = os.environ.get(env_var, "").strip()
    if value:
        _auth_logger.debug("从环境变量读取凭证成功: %s", env_var)
        return value
    _auth_logger.debug("环境变量不存在或为空: %s", env_var)
    return None


def _read_secret_from_exec(exec_cmd: str) -> Optional[str]:
    """
    通过执行命令获取敏感信息

    Args:
        exec_cmd: 要执行的命令

    Returns:
        命令标准输出（去除首尾空白），执行失败时返回 None
    """
    try:
        result = subprocess.run(
            exec_cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            content = result.stdout.strip()
            if content:
                _auth_logger.debug("通过命令获取凭证成功: %s", exec_cmd[:50] + "..." if len(exec_cmd) > 50 else exec_cmd)
                return content
            _auth_logger.debug("命令输出为空: %s", exec_cmd[:50] + "..." if len(exec_cmd) > 50 else exec_cmd)
        else:
            _auth_logger.warning("凭证命令执行失败: returncode=%d", result.returncode)
    except subprocess.TimeoutExpired:
        _auth_logger.warning("凭证命令执行超时: %s", exec_cmd[:50] + "..." if len(exec_cmd) > 50 else exec_cmd)
    except Exception as e:
        _auth_logger.warning("凭证命令执行异常: %s", type(e).__name__)
    return None


def get_gitlab_auth(config: Optional["Config"] = None) -> Optional[GitLabAuth]:
    """
    获取 GitLab 认证凭证（统一接口）

    认证模式 (scm.gitlab.auth.mode):
        - token: 通用 token（默认，向后兼容）
        - pat: Personal Access Token
        - oauth2: OAuth2 Token
        - job: CI/CD Job Token

    凭证读取优先级:
        1. scm.gitlab.auth.exec (执行命令获取)
        2. scm.gitlab.auth.token_env (环境变量名)
        3. scm.gitlab.auth.token_file (文件路径)
        4. 环境变量 GITLAB_TOKEN (向后兼容)
        5. scm.gitlab.token (配置文件，向后兼容)
        6. gitlab.private_token (旧配置，向后兼容)

    配置示例:
        [scm.gitlab.auth]
        mode = "pat"                           # token|oauth2|pat|job
        token_env = "MY_GITLAB_TOKEN"          # 从此环境变量读取 token
        token_file = "~/.secrets/gitlab.token" # 或从文件读取
        exec = "vault read -field=token secret/gitlab"  # 或执行命令获取

    Args:
        config: 可选的 Config 实例

    Returns:
        GitLabAuth 实例，如果无法获取凭证则返回 None

    Note:
        此函数不会将 token 明文写入日志，仅记录凭证来源类型
    """
    if config is None:
        config = get_config()

    # 读取认证模式
    mode_str = config.get("scm.gitlab.auth.mode", "token")
    try:
        auth_mode = GitLabAuthMode(mode_str.lower())
    except ValueError:
        _auth_logger.warning(
            "无效的 GitLab 认证模式: %s，使用默认值 'token'",
            mode_str,
        )
        auth_mode = GitLabAuthMode.TOKEN

    token: Optional[str] = None
    source: str = ""

    # 1. 尝试 exec 命令
    exec_cmd = config.get("scm.gitlab.auth.exec")
    if exec_cmd:
        token = _read_secret_from_exec(exec_cmd)
        if token:
            source = "exec"
            _auth_logger.info("GitLab 认证凭证来源: exec 命令")

    # 2. 尝试 token_env 环境变量
    if not token:
        token_env = config.get("scm.gitlab.auth.token_env")
        if token_env:
            token = _read_secret_from_env(token_env)
            if token:
                source = f"env:{token_env}"
                _auth_logger.info("GitLab 认证凭证来源: 环境变量 %s", token_env)

    # 3. 尝试 token_file 文件
    if not token:
        token_file = config.get("scm.gitlab.auth.token_file")
        if token_file:
            token = _read_secret_from_file(token_file)
            if token:
                source = "file"
                _auth_logger.info("GitLab 认证凭证来源: 文件")

    # 4. 回退: GITLAB_TOKEN 环境变量（向后兼容）
    if not token:
        token = _read_secret_from_env("GITLAB_TOKEN")
        if token:
            source = "env:GITLAB_TOKEN"
            _auth_logger.info("GitLab 认证凭证来源: 环境变量 GITLAB_TOKEN (向后兼容)")

    # 4.5 回退: GITLAB_PRIVATE_TOKEN 环境变量（向后兼容，优先级低于 GITLAB_TOKEN）
    if not token:
        token = _read_secret_from_env("GITLAB_PRIVATE_TOKEN")
        if token:
            source = "env:GITLAB_PRIVATE_TOKEN"
            _auth_logger.info("GitLab 认证凭证来源: 环境变量 GITLAB_PRIVATE_TOKEN (向后兼容)")

    # 5. 回退: scm.gitlab.token 配置（向后兼容）
    if not token:
        token = config.get("scm.gitlab.token")
        if token:
            source = "config:scm.gitlab.token"
            _auth_logger.info("GitLab 认证凭证来源: 配置项 scm.gitlab.token")

    # 6. 回退: gitlab.private_token 旧配置（向后兼容）
    if not token:
        token = config.get("gitlab.private_token")
        if token:
            source = "config:gitlab.private_token"
            _auth_logger.info("GitLab 认证凭证来源: 配置项 gitlab.private_token (向后兼容)")

    if not token:
        _auth_logger.warning("未找到有效的 GitLab 认证凭证")
        return None

    return GitLabAuth(mode=auth_mode, token=token, source=source)


def get_svn_auth(config: Optional["Config"] = None) -> Optional[SVNAuth]:
    """
    获取 SVN 认证凭证（统一接口）

    凭证读取优先级:
        用户名:
            1. 环境变量 SVN_USERNAME
            2. scm.svn.username 配置
            3. svn.username 旧配置

        密码:
            1. scm.svn.password_env (环境变量名)
            2. scm.svn.password_file (文件路径)
            3. 环境变量 SVN_PASSWORD (向后兼容)

    配置示例:
        [scm.svn]
        url = "https://svn.example.com/repo"
        username = "user"
        password_env = "MY_SVN_PASSWORD"         # 从此环境变量读取密码
        password_file = "~/.secrets/svn.passwd"  # 或从文件读取密码

    Args:
        config: 可选的 Config 实例

    Returns:
        SVNAuth 实例，如果用户名和密码都未配置则返回 None

    Note:
        此函数不会将密码明文写入日志，仅记录凭证来源类型
    """
    if config is None:
        config = get_config()

    username: Optional[str] = None
    password: Optional[str] = None
    source_parts: list = []

    # === 读取用户名 ===

    # 1. 环境变量 SVN_USERNAME
    username = _read_secret_from_env("SVN_USERNAME")
    if username:
        source_parts.append("username:env:SVN_USERNAME")
        _auth_logger.info("SVN 用户名来源: 环境变量 SVN_USERNAME")

    # 2. scm.svn.username 配置
    if not username:
        username = config.get("scm.svn.username")
        if username:
            source_parts.append("username:config:scm.svn.username")
            _auth_logger.info("SVN 用户名来源: 配置项 scm.svn.username")

    # 3. svn.username 旧配置（向后兼容）
    if not username:
        username = config.get("svn.username")
        if username:
            source_parts.append("username:config:svn.username")
            _auth_logger.info("SVN 用户名来源: 配置项 svn.username (向后兼容)")

    # === 读取密码 ===

    # 1. scm.svn.password_env 环境变量
    password_env = config.get("scm.svn.password_env")
    if password_env:
        password = _read_secret_from_env(password_env)
        if password:
            source_parts.append(f"password:env:{password_env}")
            _auth_logger.info("SVN 密码来源: 环境变量 %s", password_env)

    # 2. scm.svn.password_file 文件
    if not password:
        password_file = config.get("scm.svn.password_file")
        if password_file:
            password = _read_secret_from_file(password_file)
            if password:
                source_parts.append("password:file")
                _auth_logger.info("SVN 密码来源: 文件")

    # 3. 环境变量 SVN_PASSWORD（向后兼容）
    if not password:
        password = _read_secret_from_env("SVN_PASSWORD")
        if password:
            source_parts.append("password:env:SVN_PASSWORD")
            _auth_logger.info("SVN 密码来源: 环境变量 SVN_PASSWORD (向后兼容)")

    if not username and not password:
        _auth_logger.debug("未配置 SVN 认证凭证")
        return None

    source = ", ".join(source_parts) if source_parts else "none"
    return SVNAuth(username=username, password=password, source=source)
