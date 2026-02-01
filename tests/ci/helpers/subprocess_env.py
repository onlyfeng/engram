"""
tests/ci/helpers/subprocess_env.py

提供子进程环境变量构造工具，确保 subprocess.run 调用使用最小化、可控的环境。

设计目的：
1. 避免测试受外部环境变量污染（如 PYTHONPATH 冲突）
2. 确保必要的环境变量（PATH、HOME、PYTHONPATH）正确设置
3. 提供统一的环境构造逻辑，便于维护和调试

使用示例：
    from tests.ci.helpers.subprocess_env import get_subprocess_env

    result = subprocess.run(
        [sys.executable, str(script_path), ...],
        capture_output=True,
        text=True,
        env=get_subprocess_env(project_root),
        cwd=str(project_root),
    )
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# ============================================================================
# 环境变量白名单
# ============================================================================

# 必须保留的环境变量（系统运行必需）
REQUIRED_ENV_VARS: frozenset[str] = frozenset(
    {
        "PATH",  # 系统路径，用于查找可执行文件
        "HOME",  # 用户主目录
        "USER",  # 用户名
        "LANG",  # 语言设置（影响字符编码）
        "LC_ALL",  # 覆盖所有 locale 设置
        "LC_CTYPE",  # 字符分类和大小写转换
        "TERM",  # 终端类型
        "SHELL",  # Shell 类型
    }
)

# Python 相关环境变量（白名单）
PYTHON_ENV_VARS: frozenset[str] = frozenset(
    {
        "PYTHONPATH",  # Python 模块搜索路径
        "PYTHONDONTWRITEBYTECODE",  # 不生成 .pyc 文件
        "PYTHONUNBUFFERED",  # 不缓冲 stdout/stderr
        "PYTHONHASHSEED",  # 哈希种子（用于可重复测试）
        "VIRTUAL_ENV",  # 虚拟环境路径
        "CONDA_PREFIX",  # Conda 环境路径
        "CONDA_DEFAULT_ENV",  # Conda 默认环境名
    }
)

# macOS 特定环境变量
MACOS_ENV_VARS: frozenset[str] = frozenset(
    {
        "TMPDIR",  # 临时目录（macOS 使用）
        "__CF_USER_TEXT_ENCODING",  # macOS 文本编码
        "XPC_FLAGS",  # macOS XPC 标志
        "XPC_SERVICE_NAME",  # macOS XPC 服务名
    }
)

# Linux 特定环境变量
LINUX_ENV_VARS: frozenset[str] = frozenset(
    {
        "TMPDIR",  # 临时目录
        "TMP",  # 临时目录（备选）
        "TEMP",  # 临时目录（备选）
        "XDG_RUNTIME_DIR",  # XDG 运行时目录
        "XDG_CONFIG_HOME",  # XDG 配置目录
        "XDG_DATA_HOME",  # XDG 数据目录
        "XDG_CACHE_HOME",  # XDG 缓存目录
    }
)

# 合并所有白名单
ENV_VAR_WHITELIST: frozenset[str] = (
    REQUIRED_ENV_VARS | PYTHON_ENV_VARS | MACOS_ENV_VARS | LINUX_ENV_VARS
)


# ============================================================================
# 环境构造函数
# ============================================================================


def get_subprocess_env(
    project_root: Path | str | None = None,
    *,
    extra_vars: dict[str, str] | None = None,
    include_pythonpath: bool = True,
) -> dict[str, str]:
    """
    构造子进程环境变量字典。

    Args:
        project_root: 项目根目录路径。如果提供，会将 src/ 目录添加到 PYTHONPATH。
        extra_vars: 额外的环境变量，会覆盖白名单中的同名变量。
        include_pythonpath: 是否自动设置 PYTHONPATH。默认 True。

    Returns:
        dict[str, str]: 过滤后的环境变量字典。

    Example:
        >>> env = get_subprocess_env(Path("/path/to/project"))
        >>> result = subprocess.run([...], env=env)
    """
    # 从当前环境复制白名单变量
    env: dict[str, str] = {}
    for var in ENV_VAR_WHITELIST:
        if var in os.environ:
            env[var] = os.environ[var]

    # 设置 PYTHONPATH
    if include_pythonpath:
        pythonpath_parts: list[str] = []

        # 如果提供了 project_root，添加 src/ 目录
        if project_root is not None:
            root = Path(project_root).resolve()
            src_dir = root / "src"
            if src_dir.exists():
                pythonpath_parts.append(str(src_dir))
            # 同时添加项目根目录（用于 scripts 等顶层模块）
            pythonpath_parts.append(str(root))

        # 保留原有 PYTHONPATH 中的路径（如果有）
        existing_pythonpath = os.environ.get("PYTHONPATH", "")
        if existing_pythonpath:
            pythonpath_parts.append(existing_pythonpath)

        if pythonpath_parts:
            env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)

    # 默认禁用 .pyc 字节码缓存，避免缓存导致的测试隔离问题
    # 可通过 extra_vars 覆盖此行为
    if "PYTHONDONTWRITEBYTECODE" not in env:
        env["PYTHONDONTWRITEBYTECODE"] = "1"

    # 应用额外变量（覆盖）
    if extra_vars:
        env.update(extra_vars)

    return env


def get_minimal_subprocess_env(
    project_root: Path | str | None = None,
) -> dict[str, str]:
    """
    构造最小化子进程环境变量字典。

    只包含 PATH、HOME 和 PYTHONPATH，用于需要严格隔离的测试场景。

    Args:
        project_root: 项目根目录路径。如果提供，会将 src/ 目录添加到 PYTHONPATH。

    Returns:
        dict[str, str]: 最小化环境变量字典。
    """
    env: dict[str, str] = {}

    # 只保留最基本的系统变量
    for var in ("PATH", "HOME", "USER", "LANG"):
        if var in os.environ:
            env[var] = os.environ[var]

    # 设置 PYTHONPATH
    if project_root is not None:
        root = Path(project_root).resolve()
        pythonpath_parts: list[str] = []

        src_dir = root / "src"
        if src_dir.exists():
            pythonpath_parts.append(str(src_dir))
        pythonpath_parts.append(str(root))

        env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)

    return env
