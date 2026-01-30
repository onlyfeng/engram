#!/usr/bin/env python3
"""
artifacts - 兼容的 SCM 路径与制品工具
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional, Union

from engram.logbook.artifact_store import LocalArtifactsStore
from engram.logbook.config import get_effective_artifacts_root
from engram.logbook.hashing import sha256 as compute_sha256

SCM_EXT_DIFF = "diff"
SCM_EXT_DIFFSTAT = "diffstat"
SCM_EXT_MINISTAT = "ministat"
SCM_EXTS = {SCM_EXT_DIFF, SCM_EXT_DIFFSTAT, SCM_EXT_MINISTAT}

SCM_TYPE_GIT = "git"
SCM_TYPE_SVN = "svn"
SCM_TYPE_GITLAB = "gitlab"


def build_scm_artifact_path(
    *,
    project_key: str,
    repo_id: Union[str, int],
    source_type: str,
    rev_or_sha: str,
    sha256: str,
    ext: str = SCM_EXT_DIFF,
) -> str:
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
    return f"scm/{repo_id}/{source_type}/{subdir}/{filename}"


def get_artifacts_root() -> Path:
    try:
        root = get_effective_artifacts_root()
    except Exception:
        root = os.environ.get("ENGRAM_ARTIFACTS_ROOT", "./.agentx/artifacts")
    return Path(root)


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
