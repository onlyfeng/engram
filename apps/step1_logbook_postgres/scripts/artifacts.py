"""
artifacts.py - 制品（Artifact）存储工具模块

功能:
- 写入文本制品并计算哈希
- artifacts-domain 的路径拼装与文件 I/O
- SHA256 流式计算
- 支持多后端存储（local/file/object）

URI 规则委托给 engram_step1.uri 模块处理。

SCM 路径规范（v2 新版）:
    scm/<project_key>/<repo_id>/<source_type>/<rev_or_sha>/<sha256>.<ext>

    路径层级:
    - scm/: 固定前缀
    - <project_key>/: 项目标识（如 proj_a）
    - <repo_id>/: 仓库 ID
    - <source_type>/: SCM 类型（svn/git/gitlab）
    - <rev_or_sha>/: 版本标识（r100 或 abc123def）
    - <sha256>.<ext>: 文件名，ext 可选 diff/diffstat/ministat

SCM 旧版路径（向后兼容，只读）:
    SVN: scm/<repo_id>/svn/r<rev>.<ext>
    Git: scm/<repo_id>/git/commits/<sha>.<ext>

示例目录结构:
    scm/
    ├── proj_a/
    │   ├── 1/
    │   │   ├── svn/
    │   │   │   └── r100/
    │   │   │       └── abc123...diff
    │   │   └── git/
    │   │       └── def456.../
    │   │           └── e3b0c4...diff
    │   └── 2/
    │       └── gitlab/
    │           └── ...
    └── proj_b/
        └── ...

后端配置:
    通过环境变量 ENGRAM_ARTIFACTS_BACKEND 选择后端:
    - local: 本地文件系统（默认）
    - file: file:// URI 直读写
    - object: 对象存储（S3/MinIO）

    详见 engram_step1/artifact_store.py 和 config.example.toml
"""

import hashlib
import os
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Union

# 导入 URI 规范化函数（委托给 engram_step1.uri 模块）
from engram_step1.uri import normalize_uri, build_artifact_uri

# 导入 ArtifactStore 抽象层
from engram_step1.artifact_store import (
    ArtifactStore,
    LocalArtifactsStore,
    get_artifact_store,
    get_default_store,
)

# 文件读取/写入缓冲区大小（64KB）
BUFFER_SIZE = 65536

# 默认制品根目录（可通过环境变量覆盖）
ENV_ARTIFACTS_ROOT = "ENGRAM_ARTIFACTS_ROOT"
DEFAULT_ARTIFACTS_ROOT = "./.agentx/artifacts"

# SCM 类型常量
SCM_TYPE_SVN = "svn"
SCM_TYPE_GIT = "git"
SCM_TYPE_GITLAB = "gitlab"

VALID_SCM_TYPES = {SCM_TYPE_SVN, SCM_TYPE_GIT, SCM_TYPE_GITLAB}

# SCM 文件扩展名常量
SCM_EXT_DIFF = "diff"
SCM_EXT_DIFFSTAT = "diffstat"
SCM_EXT_MINISTAT = "ministat"

VALID_SCM_EXTENSIONS = {SCM_EXT_DIFF, SCM_EXT_DIFFSTAT, SCM_EXT_MINISTAT}


class Sha256Stream:
    """
    SHA256 流式计算器

    支持增量更新哈希值，适用于流式处理大文件。

    用法:
        hasher = Sha256Stream()
        for chunk in data_source:
            hasher.update(chunk)
        digest = hasher.hexdigest()
        size = hasher.size_bytes
    """

    def __init__(self):
        self._hasher = hashlib.sha256()
        self._size_bytes = 0

    def update(self, data: bytes) -> "Sha256Stream":
        """
        更新哈希值

        Args:
            data: 字节数据

        Returns:
            self，支持链式调用
        """
        self._hasher.update(data)
        self._size_bytes += len(data)
        return self

    def hexdigest(self) -> str:
        """返回十六进制哈希字符串"""
        return self._hasher.hexdigest()

    @property
    def size_bytes(self) -> int:
        """已处理的字节总数"""
        return self._size_bytes

    def copy(self) -> "Sha256Stream":
        """创建当前状态的副本"""
        new_stream = Sha256Stream()
        new_stream._hasher = self._hasher.copy()
        new_stream._size_bytes = self._size_bytes
        return new_stream


def sha256_stream() -> Sha256Stream:
    """
    创建新的 SHA256 流式计算器

    Returns:
        Sha256Stream 实例

    用法:
        hasher = sha256_stream()
        hasher.update(b"hello ")
        hasher.update(b"world")
        print(hasher.hexdigest())  # 输出完整的 SHA256
        print(hasher.size_bytes)   # 输出 11
    """
    return Sha256Stream()


# normalize_uri 已从 engram_step1.uri 模块导入


def get_artifacts_root() -> Path:
    """
    获取制品根目录

    优先级:
    1. 环境变量 ENGRAM_ARTIFACTS_ROOT
    2. 配置文件 [artifacts].root（推荐）
    3. 配置文件 [paths].artifacts_root（已弃用，向后兼容）
    4. 配置文件 artifacts_root（顶层，已弃用，向后兼容）
    5. 默认值 ./.agentx/artifacts

    Returns:
        制品根目录的 Path 对象
    """
    # 使用统一的配置获取入口
    try:
        from engram_step1.config import get_effective_artifacts_root
        return Path(get_effective_artifacts_root())
    except ImportError:
        # config 模块不可用，回退到环境变量或默认值
        env_root = os.environ.get(ENV_ARTIFACTS_ROOT)
        if env_root:
            return Path(env_root)
        return Path(DEFAULT_ARTIFACTS_ROOT)


def get_scm_path(repo_id: str, scm_type: str, *sub_paths: str) -> str:
    """
    生成 SCM 制品路径（旧版兼容接口）

    Args:
        repo_id: 仓库 ID
        scm_type: SCM 类型（svn, git, gitlab）
        *sub_paths: 子路径组件

    Returns:
        规范化的 SCM 路径

    Raises:
        ValueError: 如果 scm_type 无效

    示例:
        get_scm_path("repo-001", "git", "commits", "abc123.json")
        # => "scm/repo-001/git/commits/abc123.json"

    注意:
        此函数为旧版兼容接口，新代码建议使用 build_scm_artifact_path()
    """
    if scm_type not in VALID_SCM_TYPES:
        raise ValueError(
            f"无效的 SCM 类型: {scm_type}，有效值: {', '.join(sorted(VALID_SCM_TYPES))}"
        )

    return build_artifact_uri("scm", repo_id, scm_type, *sub_paths)


def build_scm_artifact_path(
    project_key: str,
    repo_id: str,
    source_type: str,
    rev_or_sha: str,
    sha256: str,
    ext: str = SCM_EXT_DIFF,
) -> str:
    """
    构建统一的 SCM 制品存储路径

    新版路径规范:
        scm/<project_key>/<repo_id>/<source_type>/<rev_or_sha>/<sha256>.<ext>

    Args:
        project_key: 项目标识符（如 "proj_a"）
        repo_id: 仓库 ID（数字或字符串）
        source_type: 源类型（svn, git, gitlab）
        rev_or_sha: 版本标识，格式规范：
            - SVN: 必须以 "r" 前缀 + revision 号，如 "r100"、"r12345"
            - Git/GitLab: 完整 40 位 commit SHA，如 "abc123def..."
        sha256: 内容的 SHA256 哈希值
        ext: 文件扩展名（diff, diffstat, ministat），默认 "diff"

    Returns:
        规范化的 SCM 制品路径

    Raises:
        ValueError: 参数无效（包括 SVN rev_or_sha 格式不符合 r<rev> 规范）

    示例:
        # SVN: rev_or_sha 必须为 "r<rev>" 格式
        build_scm_artifact_path("proj_a", "1", "svn", "r100", "abc123...", "diff")
        # => "scm/proj_a/1/svn/r100/abc123....diff"

        build_scm_artifact_path("proj_a", "1", "svn", "r12345", "def456...", "diffstat")
        # => "scm/proj_a/1/svn/r12345/def456....diffstat"

        # Git: rev_or_sha 为完整 40 位 SHA
        build_scm_artifact_path("proj_a", "2", "git", "abc123def456789...", "e3b0c4...", "diff")
        # => "scm/proj_a/2/git/abc123def456789.../e3b0c4....diff"

    路径层级说明:
        - scm/: 固定前缀，标识 SCM 相关制品
        - <project_key>/: 项目标识，支持多项目隔离
        - <repo_id>/: 仓库 ID，支持同项目多仓库
        - <source_type>/: SCM 类型（svn/git/gitlab）
        - <rev_or_sha>/: 版本标识
            * SVN: r<rev> 格式（如 r100）
            * Git: 完整 40 位 SHA（如 abc123def...）
        - <sha256>.<ext>: 文件名，使用内容哈希确保唯一性

    向后兼容:
        旧版路径格式仍可通过 resolve_scm_artifact_path() 读取（只读）
    """
    # 参数验证
    if not project_key or not project_key.strip():
        raise ValueError("project_key 不能为空")
    if not repo_id or not str(repo_id).strip():
        raise ValueError("repo_id 不能为空")
    if not rev_or_sha or not rev_or_sha.strip():
        raise ValueError("rev_or_sha 不能为空")
    if not sha256 or not sha256.strip():
        raise ValueError("sha256 不能为空")

    # 规范化 source_type
    source_type = source_type.strip().lower()
    if source_type not in VALID_SCM_TYPES:
        raise ValueError(
            f"无效的 source_type: {source_type}，有效值: {', '.join(sorted(VALID_SCM_TYPES))}"
        )

    # 规范化 ext
    ext = ext.strip().lower()
    if ext not in VALID_SCM_EXTENSIONS:
        raise ValueError(
            f"无效的 ext: {ext}，有效值: {', '.join(sorted(VALID_SCM_EXTENSIONS))}"
        )

    # 规范化其他参数
    project_key = project_key.strip()
    repo_id = str(repo_id).strip()
    rev_or_sha = rev_or_sha.strip()
    sha256 = sha256.strip().lower()

    # rev_or_sha 格式验证
    if source_type == SCM_TYPE_SVN:
        # SVN: 必须以 "r" 前缀 + 数字
        if not rev_or_sha.startswith("r"):
            raise ValueError(
                f"SVN rev_or_sha 格式错误: {rev_or_sha!r}，必须以 'r' 前缀（如 'r100'）"
            )
        rev_part = rev_or_sha[1:]
        if not rev_part.isdigit():
            raise ValueError(
                f"SVN rev_or_sha 格式错误: {rev_or_sha!r}，'r' 后必须为数字（如 'r100'）"
            )
    elif source_type in (SCM_TYPE_GIT, SCM_TYPE_GITLAB):
        # Git/GitLab: 建议使用完整 40 位 SHA，但允许短 SHA（最少 7 位）
        if len(rev_or_sha) < 7:
            raise ValueError(
                f"Git/GitLab rev_or_sha 格式错误: {rev_or_sha!r}，SHA 长度至少 7 位"
            )
        # 验证是否为有效的十六进制字符
        if not all(c in "0123456789abcdefABCDEF" for c in rev_or_sha):
            raise ValueError(
                f"Git/GitLab rev_or_sha 格式错误: {rev_or_sha!r}，必须为十六进制 SHA"
            )

    # 构建文件名
    filename = f"{sha256}.{ext}"

    # 构建完整路径
    return build_artifact_uri("scm", project_key, repo_id, source_type, rev_or_sha, filename)


def build_legacy_scm_path(
    repo_id: str,
    source_type: str,
    rev_or_sha: str,
    ext: str = SCM_EXT_DIFF,
) -> str:
    """
    构建旧版 SCM 制品路径（用于兼容读取）

    旧版路径格式:
        - SVN: scm/<repo_id>/svn/r<rev>.<ext>
        - Git: scm/<repo_id>/git/commits/<sha>.<ext>

    Args:
        repo_id: 仓库 ID
        source_type: 源类型（svn, git）
        rev_or_sha: SVN revision 号或 Git commit SHA
        ext: 文件扩展名（diff, diffstat），默认 "diff"

    Returns:
        旧版格式的 SCM 制品路径

    注意:
        此函数仅用于读取旧版数据，新写入应使用 build_scm_artifact_path()
    """
    source_type = source_type.strip().lower()
    ext = ext.strip().lower() if ext else SCM_EXT_DIFF

    if source_type == "svn":
        # 旧版 SVN 格式: scm/<repo_id>/svn/r<rev>.<ext>
        return build_artifact_uri("scm", repo_id, "svn", f"r{rev_or_sha}.{ext}")
    else:
        # 旧版 Git 格式: scm/<repo_id>/git/commits/<sha>.<ext>
        return build_artifact_uri("scm", repo_id, "git", "commits", f"{rev_or_sha}.{ext}")


def write_text_artifact(
    rel_path: Union[str, Path],
    content_iter_or_bytes: Union[bytes, str, Iterator[bytes]],
    artifacts_root: Optional[Union[str, Path]] = None,
    encoding: str = "utf-8",
    store: Optional[ArtifactStore] = None,
) -> Dict[str, Any]:
    """
    写入文本制品并返回元数据

    支持三种输入类型:
    - bytes: 直接写入
    - str: 编码后写入
    - Iterator[bytes]: 流式写入（适用于大文件）

    Args:
        rel_path: 相对路径（相对于制品根目录）
        content_iter_or_bytes: 内容（bytes、str 或 bytes 迭代器）
        artifacts_root: 制品根目录（可选，仅用于 local 后端兼容）
        encoding: 字符串编码（默认 utf-8）
        store: ArtifactStore 实例（可选，默认使用全局 store）

    Returns:
        包含以下字段的字典:
        - uri: 规范化的制品 URI
        - sha256: 内容的 SHA256 哈希
        - size_bytes: 内容大小（字节）

    示例:
        # 写入字符串
        result = write_text_artifact("scm/repo-001/git/info.json", '{"key": "value"}')
        # => {"uri": "scm/repo-001/git/info.json", "sha256": "...", "size_bytes": 16}

        # 流式写入
        def generate_chunks():
            yield b"chunk1"
            yield b"chunk2"
        result = write_text_artifact("large_file.txt", generate_chunks())

        # 使用自定义 store
        from engram_step1.artifact_store import ObjectStore
        s3_store = ObjectStore(bucket="my-bucket")
        result = write_text_artifact("data.json", '{}', store=s3_store)
    """
    # 获取 store 实例
    if store is not None:
        _store = store
    elif artifacts_root is not None:
        # 兼容旧 API: 如果指定了 artifacts_root，使用 LocalArtifactsStore
        _store = LocalArtifactsStore(root=artifacts_root)
    else:
        _store = get_default_store()

    # 规范化 URI
    uri = normalize_uri(rel_path)

    # 使用 store 写入
    return _store.put(uri, content_iter_or_bytes, encoding=encoding)


def read_artifact(
    rel_path: Union[str, Path],
    artifacts_root: Optional[Union[str, Path]] = None,
    store: Optional[ArtifactStore] = None,
) -> bytes:
    """
    读取制品内容

    Args:
        rel_path: 相对路径
        artifacts_root: 制品根目录（可选，仅用于 local 后端兼容）
        store: ArtifactStore 实例（可选，默认使用全局 store）

    Returns:
        文件内容（字节）

    Raises:
        ArtifactNotFoundError: 制品不存在
    """
    # 获取 store 实例
    if store is not None:
        _store = store
    elif artifacts_root is not None:
        _store = LocalArtifactsStore(root=artifacts_root)
    else:
        _store = get_default_store()

    uri = normalize_uri(rel_path)
    return _store.get(uri)


def artifact_exists(
    rel_path: Union[str, Path],
    artifacts_root: Optional[Union[str, Path]] = None,
    store: Optional[ArtifactStore] = None,
) -> bool:
    """
    检查制品是否存在

    Args:
        rel_path: 相对路径
        artifacts_root: 制品根目录（可选，仅用于 local 后端兼容）
        store: ArtifactStore 实例（可选，默认使用全局 store）

    Returns:
        制品是否存在
    """
    # 获取 store 实例
    if store is not None:
        _store = store
    elif artifacts_root is not None:
        _store = LocalArtifactsStore(root=artifacts_root)
    else:
        _store = get_default_store()

    uri = normalize_uri(rel_path)
    return _store.exists(uri)


def get_artifact_info(
    rel_path: Union[str, Path],
    artifacts_root: Optional[Union[str, Path]] = None,
    store: Optional[ArtifactStore] = None,
) -> Dict[str, Any]:
    """
    获取制品元数据（流式计算哈希，避免大文件内存问题）

    Args:
        rel_path: 相对路径
        artifacts_root: 制品根目录（可选，仅用于 local 后端兼容）
        store: ArtifactStore 实例（可选，默认使用全局 store）

    Returns:
        包含 uri, sha256, size_bytes 的字典

    Raises:
        ArtifactNotFoundError: 制品不存在
    """
    # 获取 store 实例
    if store is not None:
        _store = store
    elif artifacts_root is not None:
        _store = LocalArtifactsStore(root=artifacts_root)
    else:
        _store = get_default_store()

    uri = normalize_uri(rel_path)
    return _store.get_info(uri)


def delete_artifact(
    rel_path: Union[str, Path],
    artifacts_root: Optional[Union[str, Path]] = None,
    store: Optional[ArtifactStore] = None,
) -> bool:
    """
    删除制品

    Args:
        rel_path: 相对路径
        artifacts_root: 制品根目录（可选，仅用于 local 后端兼容）
        store: ArtifactStore 实例（可选，默认使用全局 store）

    Returns:
        是否删除成功（True 表示文件已删除或不存在）

    Raises:
        ArtifactError: 删除失败
    """
    from engram_step1.artifact_store import ArtifactNotFoundError, ArtifactError

    # 获取 store 实例
    if store is not None:
        _store = store
    elif artifacts_root is not None:
        _store = LocalArtifactsStore(root=artifacts_root)
    else:
        _store = get_default_store()

    uri = normalize_uri(rel_path)

    # 检查是否存在
    if not _store.exists(uri):
        return True  # 文件不存在视为成功

    # 根据 store 类型执行删除
    if isinstance(_store, LocalArtifactsStore):
        full_path = Path(_store.resolve(uri))
        try:
            full_path.unlink()
            return True
        except OSError as e:
            raise ArtifactError(
                f"删除制品失败: {uri}",
                {"uri": uri, "path": str(full_path), "error": str(e)},
            )
    else:
        # 对于其他 store 类型，暂不支持删除
        raise ArtifactError(
            f"当前 store 类型不支持删除操作: {type(_store).__name__}",
            {"uri": uri, "store_type": type(_store).__name__},
        )
