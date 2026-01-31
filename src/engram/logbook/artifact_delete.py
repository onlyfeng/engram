"""
engram_logbook.artifact_delete - 制品删除抽象层

提供统一的删除入口，支持：
- delete_artifact_key: 删除逻辑键（local/object 后端）
- delete_physical_uri: 删除物理 URI（file:// / s3://）
- safe_delete_artifact: 自动检测 URI 类型的便捷函数

安全特性:
- file:// 删除复用 FileUriStore 的 parse+validate
- 支持 --trash-prefix 软删除（移动而非直接删除）
- object 删除前强制 ops 凭证检查（与 ENGRAM_S3_USE_OPS 语义对齐）

与 artifact_gc.py 的关系:
- artifact_gc.py 使用内部删除函数（接受 full_path/key）
- 本模块提供公共 API（接受 uri），是推荐的外部调用入口
- 两者语义对齐：trash_prefix、MetadataDirective='COPY'、require_ops

使用示例:
    from engram.logbook.artifact_delete import (
        delete_artifact_key,
        delete_physical_uri,
        safe_delete_artifact,
        ArtifactDeleteResult,
    )

    # 删除逻辑键
    result = delete_artifact_key("scm/proj/1/r100.diff")

    # 软删除到 trash
    result = delete_artifact_key("scm/proj/1/r100.diff", trash_prefix=".trash/")

    # 删除物理 URI
    result = delete_physical_uri("file:///mnt/artifacts/test.txt")

    # object 后端删除（需要 ops 凭证）
    result = delete_physical_uri("s3://bucket/key", require_ops=True)

    # 自动检测 URI 类型
    result = safe_delete_artifact("scm/proj/1/r100.diff")
    result = safe_delete_artifact("s3://bucket/key")
"""

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .artifact_ops_audit import (
    write_delete_audit_event,
)
from .artifact_store import (
    ArtifactError,
    ArtifactStore,
    FileUriPathError,
    FileUriStore,
    LocalArtifactsStore,
    ObjectStore,
    PathTraversalError,
    get_artifact_store,
    get_default_store,
)
from .config import (
    get_gc_require_ops_default,
)
from .uri import (
    is_physical_uri,
    normalize_uri,
    parse_uri,
)

# =============================================================================
# 错误定义
# =============================================================================


class ArtifactDeleteError(ArtifactError):
    """制品删除错误"""

    error_type = "ARTIFACT_DELETE_ERROR"


class ArtifactDeleteOpsCredentialsRequiredError(ArtifactDeleteError):
    """删除操作需要 ops 凭证但未提供"""

    error_type = "ARTIFACT_DELETE_OPS_CREDENTIALS_REQUIRED"


class ArtifactDeleteNotSupportedError(ArtifactDeleteError):
    """当前存储类型不支持删除操作"""

    error_type = "ARTIFACT_DELETE_NOT_SUPPORTED"


# =============================================================================
# 删除结果数据类
# =============================================================================


@dataclass
class ArtifactDeleteResult:
    """删除操作结果"""

    uri: str  # 删除的 URI
    deleted: bool  # 是否成功删除
    existed: bool  # 删除前是否存在
    trashed: bool = False  # 是否为软删除（移动到 trash）
    trash_path: Optional[str] = None  # 软删除的目标路径
    error: Optional[str] = None  # 错误信息


# =============================================================================
# 本地文件删除（复用 LocalArtifactsStore 路径验证）
# =============================================================================


def _delete_local_artifact(
    store: LocalArtifactsStore,
    uri: str,
    trash_prefix: Optional[str] = None,
) -> ArtifactDeleteResult:
    """
    删除本地制品文件

    复用 LocalArtifactsStore 的路径验证逻辑确保安全。

    Args:
        store: LocalArtifactsStore 实例
        uri: 规范化的 artifact key
        trash_prefix: 软删除目标前缀（相对于 artifacts_root）

    Returns:
        ArtifactDeleteResult 删除结果
    """
    normalized_uri = normalize_uri(uri)

    # 使用 store 的 resolve 方法获取完整路径（复用路径验证）
    try:
        full_path_str = store.resolve(normalized_uri)
        full_path = Path(full_path_str)
    except PathTraversalError as e:
        return ArtifactDeleteResult(
            uri=normalized_uri,
            deleted=False,
            existed=False,
            error=str(e),
        )

    # 检查文件是否存在
    if not full_path.exists():
        return ArtifactDeleteResult(
            uri=normalized_uri,
            deleted=True,  # 不存在视为成功
            existed=False,
        )

    try:
        if trash_prefix:
            # 软删除：移动到 trash 目录
            trash_path = store.root / trash_prefix / normalized_uri

            # 确保 trash 目录存在
            trash_path.parent.mkdir(parents=True, exist_ok=True)

            # 如果目标已存在，添加时间戳
            if trash_path.exists():
                timestamp = int(time.time())
                trash_path = trash_path.with_name(
                    f"{trash_path.stem}.{timestamp}{trash_path.suffix}"
                )

            shutil.move(str(full_path), str(trash_path))

            return ArtifactDeleteResult(
                uri=normalized_uri,
                deleted=True,
                existed=True,
                trashed=True,
                trash_path=str(trash_path),
            )
        else:
            # 硬删除
            full_path.unlink()

            return ArtifactDeleteResult(
                uri=normalized_uri,
                deleted=True,
                existed=True,
            )

    except OSError as e:
        return ArtifactDeleteResult(
            uri=normalized_uri,
            deleted=False,
            existed=True,
            error=str(e),
        )


# =============================================================================
# File URI 删除（复用 FileUriStore 的 parse+validate）
# =============================================================================


def _delete_file_uri_artifact(
    uri: str,
    trash_prefix: Optional[str] = None,
    allowed_roots: Optional[list] = None,
) -> ArtifactDeleteResult:
    """
    删除 file:// URI 指向的文件

    复用 FileUriStore 的 _parse_file_uri 和 _validate_allowed_root 确保安全。

    Args:
        uri: file:// 格式的 URI
        trash_prefix: 软删除目标前缀（相对于文件所在的 allowed_root）
        allowed_roots: 允许操作的根路径列表

    Returns:
        ArtifactDeleteResult 删除结果
    """
    # 创建 FileUriStore 实例以复用其路径验证逻辑
    store = FileUriStore(allowed_roots=allowed_roots)

    try:
        # 复用 FileUriStore 的 URI 解析和验证
        file_uri = store._ensure_file_uri(uri)
        file_path = store._parse_file_uri(file_uri)
        store._validate_allowed_root(file_path)
    except (FileUriPathError, PathTraversalError) as e:
        return ArtifactDeleteResult(
            uri=uri,
            deleted=False,
            existed=False,
            error=str(e),
        )

    # 检查文件是否存在
    if not file_path.exists():
        return ArtifactDeleteResult(
            uri=uri,
            deleted=True,  # 不存在视为成功
            existed=False,
        )

    try:
        if trash_prefix:
            # 软删除：移动到 trash 目录
            # 查找文件所属的 allowed_root
            trash_root = None
            relative_path = None

            if store.allowed_roots:
                for root in store.allowed_roots:
                    root_path = Path(root)
                    try:
                        if root_path.exists():
                            resolved_root = root_path.resolve()
                        else:
                            resolved_root = root_path.absolute()
                        resolved_file = (
                            file_path.resolve() if file_path.exists() else file_path.absolute()
                        )

                        if str(resolved_file).startswith(str(resolved_root) + os.sep):
                            trash_root = resolved_root
                            relative_path = resolved_file.relative_to(resolved_root)
                            break
                    except (OSError, ValueError):
                        continue

            if trash_root is None:
                # 无法确定 trash root，回退到文件所在目录的父目录
                trash_root = file_path.parent.parent
                relative_path = Path(file_path.parent.name) / file_path.name

            trash_path = trash_root / trash_prefix / relative_path

            # 确保 trash 目录存在
            trash_path.parent.mkdir(parents=True, exist_ok=True)

            # 如果目标已存在，添加时间戳
            if trash_path.exists():
                timestamp = int(time.time())
                trash_path = trash_path.with_name(
                    f"{trash_path.stem}.{timestamp}{trash_path.suffix}"
                )

            shutil.move(str(file_path), str(trash_path))

            return ArtifactDeleteResult(
                uri=uri,
                deleted=True,
                existed=True,
                trashed=True,
                trash_path=str(trash_path),
            )
        else:
            # 硬删除
            file_path.unlink()

            return ArtifactDeleteResult(
                uri=uri,
                deleted=True,
                existed=True,
            )

    except OSError as e:
        return ArtifactDeleteResult(
            uri=uri,
            deleted=False,
            existed=True,
            error=str(e),
        )


# =============================================================================
# Object Store 删除（强制 ops 凭证检查）
# =============================================================================


def _delete_object_store_artifact(
    store: ObjectStore,
    uri: str,
    trash_prefix: Optional[str] = None,
    require_ops: bool = True,
) -> ArtifactDeleteResult:
    """
    删除对象存储中的制品

    在执行删除前强制检查 ops 凭证（与 ENGRAM_S3_USE_OPS 语义对齐）。

    Args:
        store: ObjectStore 实例
        uri: 规范化的 artifact key
        trash_prefix: 软删除目标前缀
        require_ops: 是否强制要求 ops 凭证（默认 True）

    Returns:
        ArtifactDeleteResult 删除结果

    Raises:
        ArtifactDeleteOpsCredentialsRequiredError: 需要 ops 凭证但未提供
    """
    normalized_uri = normalize_uri(uri)

    # 强制 ops 凭证检查
    if require_ops and not store.is_ops_credentials():
        raise ArtifactDeleteOpsCredentialsRequiredError(
            "对象存储删除操作需要 ops 凭证。\n"
            "请设置 ENGRAM_S3_USE_OPS=true 并配置 ENGRAM_S3_OPS_ACCESS_KEY/SECRET_KEY",
            {
                "uri": normalized_uri,
                "using_ops_credentials": store.using_ops_credentials,
                "hint": "设置 ENGRAM_S3_USE_OPS=true 来使用 ops 凭证",
            },
        )

    try:
        # 获取对象 key（复用路径验证）
        key = store._object_key(normalized_uri)
    except PathTraversalError as e:
        return ArtifactDeleteResult(
            uri=normalized_uri,
            deleted=False,
            existed=False,
            error=str(e),
        )

    # 检查对象是否存在
    existed = store.exists(normalized_uri)
    if not existed:
        return ArtifactDeleteResult(
            uri=normalized_uri,
            deleted=True,  # 不存在视为成功
            existed=False,
        )

    try:
        client = store._get_client()

        if trash_prefix:
            # 软删除：复制到 trash 前缀然后删除原文件
            trash_key = f"{trash_prefix.rstrip('/')}/{key}"

            # 使用 MetadataDirective='COPY' 保留所有用户元数据
            client.copy_object(
                Bucket=store.bucket,
                CopySource={"Bucket": store.bucket, "Key": key},
                Key=trash_key,
                MetadataDirective="COPY",
            )

            # 删除原对象
            client.delete_object(Bucket=store.bucket, Key=key)

            return ArtifactDeleteResult(
                uri=normalized_uri,
                deleted=True,
                existed=True,
                trashed=True,
                trash_path=f"s3://{store.bucket}/{trash_key}",
            )
        else:
            # 硬删除
            client.delete_object(Bucket=store.bucket, Key=key)

            return ArtifactDeleteResult(
                uri=normalized_uri,
                deleted=True,
                existed=True,
            )

    except Exception as e:
        return ArtifactDeleteResult(
            uri=normalized_uri,
            deleted=False,
            existed=existed,
            error=str(e),
        )


# =============================================================================
# 统一入口：delete_artifact_key
# =============================================================================


def delete_artifact_key(
    uri: str,
    store: Optional[ArtifactStore] = None,
    trash_prefix: Optional[str] = None,
    require_ops: Optional[bool] = None,
    audit: bool = True,
) -> ArtifactDeleteResult:
    """
    删除逻辑键（artifact key）

    根据 store 类型自动路由到对应的删除实现。

    Args:
        uri: 制品 URI（逻辑键）
        store: ArtifactStore 实例（可选，默认使用全局 store）
        trash_prefix: 软删除目标前缀（可选）
        require_ops: object 后端是否强制要求 ops 凭证
                     None: 使用默认配置（环境变量或 config.toml）
                     True: 显式要求
                     False: 显式禁用
        audit: 是否写入审计事件（默认 True）

    Returns:
        ArtifactDeleteResult 删除结果

    Raises:
        ArtifactDeleteOpsCredentialsRequiredError: object 后端需要 ops 凭证但未提供
        ArtifactDeleteNotSupportedError: 当前存储类型不支持删除操作

    治理开关:
        require_ops 参数支持三态逻辑:
        - True: 显式要求 ops 凭证
        - False: 显式禁用 ops 凭证检查
        - None: 使用默认配置（环境变量 ENGRAM_GC_REQUIRE_OPS_DEFAULT 或 config.toml）

        默认配置优先级: 环境变量 > config.toml > False

    示例:
        # 使用默认 store 删除（使用默认治理开关）
        result = delete_artifact_key("scm/proj/1/r100.diff")

        # 软删除
        result = delete_artifact_key("scm/proj/1/r100.diff", trash_prefix=".trash/")

        # 显式要求 ops 凭证
        result = delete_artifact_key("scm/proj/1/r100.diff", require_ops=True)

        # 使用自定义 store
        from engram.logbook.artifact_store import LocalArtifactsStore
        store = LocalArtifactsStore(root="/mnt/artifacts")
        result = delete_artifact_key("test.txt", store=store)
    """
    if store is None:
        store = get_default_store()

    # === 应用治理开关默认值 ===
    # require_ops: None 时从配置读取默认值
    if require_ops is None:
        require_ops = get_gc_require_ops_default()

    normalized_uri = normalize_uri(uri)

    # 确定后端类型和相关元信息
    backend: Optional[str] = None
    bucket: Optional[str] = None
    object_key: Optional[str] = None
    using_ops: Optional[bool] = None

    if isinstance(store, LocalArtifactsStore):
        backend = "local"
        result = _delete_local_artifact(store, normalized_uri, trash_prefix)
    elif isinstance(store, ObjectStore):
        backend = "object"
        bucket = store.bucket
        object_key = store._object_key(normalized_uri) if normalized_uri else None
        using_ops = store.is_ops_credentials()
        result = _delete_object_store_artifact(store, normalized_uri, trash_prefix, require_ops)
    elif isinstance(store, FileUriStore):
        # FileUriStore 通常不用于逻辑键，但支持兼容
        # 此处简化处理：将 uri 转换为 file:// 格式
        raise ArtifactDeleteNotSupportedError(
            "FileUriStore 不支持逻辑键删除，请使用 delete_physical_uri",
            {"uri": normalized_uri, "store_type": type(store).__name__},
        )
    else:
        raise ArtifactDeleteNotSupportedError(
            f"当前存储类型不支持删除操作: {type(store).__name__}",
            {"uri": normalized_uri, "store_type": type(store).__name__},
        )

    # 写入审计事件
    if audit and result.existed:  # 只在文件曾存在时记录
        write_delete_audit_event(
            uri=normalized_uri,
            backend=backend or "unknown",
            success=result.deleted,
            trashed=result.trashed,
            bucket=bucket,
            object_key=object_key,
            trash_prefix=trash_prefix,
            using_ops_credentials=using_ops,
            require_ops=require_ops if backend == "object" else None,
            error=result.error,
        )

    return result


# =============================================================================
# 统一入口：delete_physical_uri
# =============================================================================


def delete_physical_uri(
    uri: str,
    trash_prefix: Optional[str] = None,
    allowed_roots: Optional[list] = None,
    require_ops: Optional[bool] = None,
    audit: bool = True,
) -> ArtifactDeleteResult:
    """
    删除物理 URI（file:// / s3:// 等）

    根据 URI scheme 自动路由到对应的删除实现。

    Args:
        uri: 物理 URI（file:// / s3:// 等）
        trash_prefix: 软删除目标前缀（可选）
        allowed_roots: file:// 后端允许操作的根路径列表
        require_ops: s3:// 后端是否强制要求 ops 凭证
                     None: 使用默认配置（环境变量或 config.toml）
                     True: 显式要求
                     False: 显式禁用
        audit: 是否写入审计事件（默认 True）

    Returns:
        ArtifactDeleteResult 删除结果

    Raises:
        ArtifactDeleteOpsCredentialsRequiredError: s3 后端需要 ops 凭证但未提供
        ArtifactDeleteNotSupportedError: 不支持的 URI scheme

    治理开关:
        require_ops 参数支持三态逻辑:
        - True: 显式要求 ops 凭证
        - False: 显式禁用 ops 凭证检查
        - None: 使用默认配置（环境变量 ENGRAM_GC_REQUIRE_OPS_DEFAULT 或 config.toml）

        默认配置优先级: 环境变量 > config.toml > False

    示例:
        # 删除 file:// URI
        result = delete_physical_uri("file:///mnt/artifacts/test.txt")

        # 软删除
        result = delete_physical_uri(
            "file:///mnt/artifacts/test.txt",
            trash_prefix=".trash/",
            allowed_roots=["/mnt/artifacts"]
        )

        # 删除 s3:// URI（使用默认治理开关）
        result = delete_physical_uri("s3://bucket/key")

        # 显式要求 ops 凭证
        result = delete_physical_uri("s3://bucket/key", require_ops=True)
    """
    # === 应用治理开关默认值 ===
    # require_ops: None 时从配置读取默认值
    if require_ops is None:
        require_ops = get_gc_require_ops_default()

    parsed = parse_uri(uri)

    # 用于审计事件的元信息
    backend: Optional[str] = None
    bucket: Optional[str] = None
    object_key: Optional[str] = None
    using_ops: Optional[bool] = None

    if parsed.scheme == "file" or (not parsed.scheme and uri.startswith("file://")):
        # file:// URI
        backend = "file"
        result = _delete_file_uri_artifact(uri, trash_prefix, allowed_roots)

    elif parsed.scheme in ("s3", "gs"):
        # S3/GCS URI - 需要从环境变量构造 ObjectStore
        # 解析 bucket 和 key
        # 格式: s3://bucket/key 或 gs://bucket/key
        if parsed.netloc:
            bucket = parsed.netloc
            key = parsed.path.lstrip("/")
        else:
            raise ArtifactDeleteNotSupportedError(
                f"无效的 {parsed.scheme}:// URI 格式",
                {"uri": uri, "scheme": parsed.scheme},
            )

        # 从环境变量获取 ObjectStore 配置
        store = get_artifact_store("object")
        if not isinstance(store, ObjectStore):
            raise ArtifactDeleteNotSupportedError(
                "对象存储未配置，无法删除 s3:// URI",
                {"uri": uri},
            )

        # 验证 bucket 匹配
        if store.bucket != bucket:
            raise ArtifactDeleteNotSupportedError(
                f"URI bucket ({bucket}) 与配置的 bucket ({store.bucket}) 不匹配",
                {"uri": uri, "uri_bucket": bucket, "store_bucket": store.bucket},
            )

        # 移除 store.prefix 获取逻辑 URI
        if store.prefix:
            prefix = store.prefix.rstrip("/") + "/"
            if key.startswith(prefix):
                logical_uri = key[len(prefix) :]
            else:
                logical_uri = key
        else:
            logical_uri = key

        backend = "object"
        object_key = key
        using_ops = store.is_ops_credentials()
        result = _delete_object_store_artifact(store, logical_uri, trash_prefix, require_ops)

    else:
        raise ArtifactDeleteNotSupportedError(
            f"不支持的 URI scheme: {parsed.scheme or '(empty)'}",
            {"uri": uri, "scheme": parsed.scheme},
        )

    # 写入审计事件
    if audit and result.existed:  # 只在文件曾存在时记录
        write_delete_audit_event(
            uri=uri,
            backend=backend or "unknown",
            success=result.deleted,
            trashed=result.trashed,
            bucket=bucket,
            object_key=object_key,
            trash_prefix=trash_prefix,
            using_ops_credentials=using_ops,
            require_ops=require_ops if backend == "object" else None,
            error=result.error,
        )

    return result


# =============================================================================
# 便捷函数
# =============================================================================


def safe_delete_artifact(
    uri: str,
    store: Optional[ArtifactStore] = None,
    trash_prefix: Optional[str] = None,
    force: bool = False,
    require_ops: Optional[bool] = None,
    audit: bool = True,
) -> ArtifactDeleteResult:
    """
    安全删除制品（自动检测 URI 类型）

    自动检测 URI 类型并路由到 delete_artifact_key 或 delete_physical_uri。

    Args:
        uri: 制品 URI（逻辑键或物理 URI）
        store: ArtifactStore 实例（可选，仅用于逻辑键）
        trash_prefix: 软删除目标前缀（可选）
        force: 是否强制删除（不存在时不报错）
        require_ops: object 后端是否强制要求 ops 凭证
                     None: 使用默认配置（环境变量或 config.toml）
                     True: 显式要求
                     False: 显式禁用
        audit: 是否写入审计事件（默认 True）

    Returns:
        ArtifactDeleteResult 删除结果

    治理开关:
        require_ops 参数支持三态逻辑:
        - True: 显式要求 ops 凭证
        - False: 显式禁用 ops 凭证检查
        - None: 使用默认配置（环境变量 ENGRAM_GC_REQUIRE_OPS_DEFAULT 或 config.toml）

    示例:
        # 自动检测 URI 类型（使用默认治理开关）
        result = safe_delete_artifact("scm/proj/1/r100.diff")
        result = safe_delete_artifact("file:///mnt/artifacts/test.txt")
        result = safe_delete_artifact("s3://bucket/key")

        # 显式禁用 ops 凭证检查
        result = safe_delete_artifact("s3://bucket/key", require_ops=False)
    """
    if is_physical_uri(uri):
        return delete_physical_uri(
            uri, trash_prefix=trash_prefix, require_ops=require_ops, audit=audit
        )
    else:
        return delete_artifact_key(
            uri, store=store, trash_prefix=trash_prefix, require_ops=require_ops, audit=audit
        )
