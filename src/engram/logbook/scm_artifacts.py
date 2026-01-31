# -*- coding: utf-8 -*-
"""
scm_artifacts - SCM 路径与制品工具

本模块提供 SCM（源代码管理）相关的路径构建与制品写入功能。

功能:
- SCM 制品路径构建
- 兼容旧版路径格式
- 制品写入辅助函数

设计原则:
- 纯业务逻辑，不依赖根目录模块
- 使用 engram.logbook.artifact_store 进行制品存储
- 使用 engram.logbook.hashing 进行哈希计算
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional, Union

from engram.logbook.artifact_store import LocalArtifactsStore
from engram.logbook.config import get_effective_artifacts_root
from engram.logbook.hashing import sha256 as compute_sha256

# ============ 常量定义 ============

# SCM 制品扩展名
SCM_EXT_DIFF = "diff"
SCM_EXT_DIFFSTAT = "diffstat"
SCM_EXT_MINISTAT = "ministat"
SCM_EXTS = {SCM_EXT_DIFF, SCM_EXT_DIFFSTAT, SCM_EXT_MINISTAT}

# SCM 源类型
SCM_TYPE_GIT = "git"
SCM_TYPE_SVN = "svn"
SCM_TYPE_GITLAB = "gitlab"


# ============ 路径构建函数 ============


def build_scm_artifact_path(
    *,
    project_key: str,
    repo_id: Union[str, int],
    source_type: str,
    rev_or_sha: str,
    sha256: str,
    ext: str = SCM_EXT_DIFF,
) -> str:
    """
    构建 SCM 制品路径

    Args:
        project_key: 项目标识
        repo_id: 仓库 ID
        source_type: 源类型（git/svn/gitlab）
        rev_or_sha: revision 或 commit SHA
        sha256: 内容 SHA256 哈希
        ext: 文件扩展名

    Returns:
        制品路径字符串

    Raises:
        ValueError: 参数无效
    """
    if not project_key:
        raise ValueError("project_key 不能为空")
    if not source_type:
        raise ValueError("source_type 不能为空")
    source_type = source_type.strip().lower()
    if source_type not in {SCM_TYPE_SVN, SCM_TYPE_GIT, SCM_TYPE_GITLAB}:
        raise ValueError("source_type 无效")
    if ext not in SCM_EXTS:
        raise ValueError("ext 无效")

    repo_id = str(repo_id)
    sha256 = sha256.lower()

    if source_type == SCM_TYPE_SVN:
        if not rev_or_sha.startswith("r") or not rev_or_sha[1:].isdigit():
            raise ValueError("SVN rev_or_sha 格式错误，必须为 r<rev>")
    else:
        if len(rev_or_sha) < 7:
            raise ValueError("Git/GitLab rev_or_sha 格式错误：至少 7 位")
        if not re.match(r"^[a-fA-F0-9]+$", rev_or_sha):
            raise ValueError("Git/GitLab rev_or_sha 格式错误：必须为十六进制")

    return f"scm/{project_key}/{repo_id}/{source_type}/{rev_or_sha}/{sha256}.{ext}"


def build_legacy_scm_path(
    *,
    repo_id: Union[str, int],
    source_type: str,
    rev_or_sha: str,
    ext: str = SCM_EXT_DIFF,
) -> str:
    """
    构建旧版 SCM 路径（兼容用途）

    Args:
        repo_id: 仓库 ID
        source_type: 源类型（git/svn/gitlab）
        rev_or_sha: revision 或 commit SHA
        ext: 文件扩展名

    Returns:
        制品路径字符串

    Raises:
        ValueError: 参数无效
    """
    if not source_type:
        raise ValueError("source_type 不能为空")
    source_type = source_type.strip().lower()
    if source_type not in {SCM_TYPE_SVN, SCM_TYPE_GIT, SCM_TYPE_GITLAB}:
        raise ValueError("source_type 无效")
    if ext not in SCM_EXTS:
        raise ValueError("ext 无效")

    repo_id = str(repo_id)

    if source_type == SCM_TYPE_SVN:
        rev = rev_or_sha
        if not rev.startswith("r"):
            rev = f"r{rev}"
        return f"scm/{repo_id}/svn/{rev}.{ext}"
    return f"scm/{repo_id}/git/commits/{rev_or_sha}.{ext}"


def get_scm_path(repo_id: Union[str, int], source_type: str, subdir: str, filename: str) -> str:
    """
    获取 SCM 路径

    Args:
        repo_id: 仓库 ID
        source_type: 源类型
        subdir: 子目录
        filename: 文件名

    Returns:
        完整路径字符串
    """
    return f"scm/{repo_id}/{source_type}/{subdir}/{filename}"


# ============ 制品根目录函数 ============


def get_artifacts_root() -> Path:
    """
    获取制品根目录

    优先级:
    1. engram.logbook.config.get_effective_artifacts_root()
    2. 环境变量 ENGRAM_ARTIFACTS_ROOT
    3. 默认值 ./.agentx/artifacts

    Returns:
        制品根目录 Path 对象
    """
    try:
        root = get_effective_artifacts_root()
    except Exception:
        root = os.environ.get("ENGRAM_ARTIFACTS_ROOT", "./.agentx/artifacts")
    return Path(root)


# ============ 制品写入函数 ============


def write_text_artifact(
    *,
    project_key: str,
    repo_id: Union[str, int],
    source_type: str,
    rev_or_sha: str,
    content: Union[str, bytes],
    sha256: Optional[str] = None,
    ext: str = SCM_EXT_DIFF,
    artifacts_root: Optional[Union[str, Path]] = None,
) -> dict:
    """
    写入文本制品

    Args:
        project_key: 项目标识
        repo_id: 仓库 ID
        source_type: 源类型
        rev_or_sha: revision 或 commit SHA
        content: 制品内容
        sha256: 内容 SHA256 哈希（可选，会自动计算）
        ext: 文件扩展名
        artifacts_root: 制品根目录（可选）

    Returns:
        {uri, sha256, size_bytes} 字典
    """
    if isinstance(content, str):
        content_bytes = content.encode("utf-8")
    else:
        content_bytes = content

    sha256 = sha256 or compute_sha256(content_bytes)
    path = build_scm_artifact_path(
        project_key=project_key,
        repo_id=repo_id,
        source_type=source_type,
        rev_or_sha=rev_or_sha,
        sha256=sha256,
        ext=ext,
    )
    store = LocalArtifactsStore(root=artifacts_root or get_artifacts_root())
    return store.put(path, content_bytes)
