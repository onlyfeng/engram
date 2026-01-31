"""
engram.logbook - Logbook 事实账本模块

提供与 Logbook PostgreSQL 数据库交互的核心功能。

约定:
- 所有 CLI 输出默认为结构化 JSON（stdout）
- 错误输出到 stderr
- 错误时返回非 0 exit code
- 支持 --config 参数和 ENGRAM_LOGBOOK_CONFIG 环境变量配置

模块:
- config: 配置管理
- db: 数据库连接和操作
- hashing: 哈希计算工具
- io: CLI I/O 工具
- errors: 错误定义
- outbox: OpenMemory 补偿队列
- governance: 治理功能
- scm_sync_runner: SCM 同步运行器核心
- scm_sync_status: SCM 同步状态摘要核心
- scm_sync_worker_core: SCM Worker 核心
- scm_sync_reaper_core: SCM Reaper 核心
- scm_sync_scheduler_core: SCM 调度器核心
"""

__version__ = "0.1.0"
__author__ = "engram"

# 导出核心类和函数
from .config import (
    ENV_CONFIG_PATH,
    Config,
    add_config_argument,
    get_config,
)
from .db import (
    Database,
    add_event,
    attach,
    create_item,
    get_database,
    get_kv,
    reset_database,
    set_kv,
)
from .errors import (
    ConfigError,
    ConfigNotFoundError,
    ConfigParseError,
    DatabaseError,
    DbConnectionError,
    EngramError,
    EngramIOError,
    FileHashNotFoundError,
    HashingError,
    QueryError,
    ValidationError,
)
from .hashing import (
    get_file_info,
    hash_bytes,
    hash_file,
    hash_stream,
    hash_string,
    md5,
    sha1,
    sha256,
    verify_file_hash,
)
from .io import (
    add_output_arguments,
    cli_wrapper,
    exit_success,
    exit_with_error,
    output_error,
    output_json,
    output_success,
)
from .outbox import (
    enqueue_memory,
    increment_retry,
    mark_dead,
    mark_sent,
)
from .outbox import (
    get_by_id as get_outbox_by_id,
)
from .outbox import (
    get_pending as get_pending_outbox,
)
from .uri import (
    ParsedUri,
    UriType,
    build_artifact_uri,
    classify_uri,
    get_uri_path,
    is_local_uri,
    is_remote_uri,
    normalize_uri,
    parse_uri,
    resolve_to_local_path,
)

__all__ = [
    # 版本信息
    "__version__",
    "__author__",
    # config
    "Config",
    "get_config",
    "add_config_argument",
    "ENV_CONFIG_PATH",
    # db
    "Database",
    "get_database",
    "reset_database",
    "create_item",
    "add_event",
    "attach",
    "set_kv",
    "get_kv",
    # outbox
    "enqueue_memory",
    "mark_sent",
    "mark_dead",
    "get_pending_outbox",
    "get_outbox_by_id",
    "increment_retry",
    # errors
    "EngramError",
    "ConfigError",
    "ConfigNotFoundError",
    "ConfigParseError",
    "DatabaseError",
    "DbConnectionError",
    "QueryError",
    "HashingError",
    "FileHashNotFoundError",
    "EngramIOError",
    "ValidationError",
    # hashing
    "hash_bytes",
    "hash_string",
    "hash_file",
    "hash_stream",
    "verify_file_hash",
    "get_file_info",
    "sha256",
    "sha1",
    "md5",
    # io
    "output_json",
    "output_success",
    "output_error",
    "exit_with_error",
    "exit_success",
    "cli_wrapper",
    "add_output_arguments",
    # uri
    "ParsedUri",
    "UriType",
    "parse_uri",
    "normalize_uri",
    "classify_uri",
    "is_remote_uri",
    "is_local_uri",
    "resolve_to_local_path",
    "get_uri_path",
    "build_artifact_uri",
]
