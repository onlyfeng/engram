#!/usr/bin/env python3
"""
source_id.py - 统一的 source_id 构建模块

提供各类 SCM 对象的 source_id 生成函数，确保格式一致性。

source_id 格式规范:
- SVN revision: svn:<repo_id>:<rev_num>
- Git commit:   git:<repo_id>:<commit_sha>
- MR/PR:        mr:<repo_id>:<iid>
- Review event: review:<repo_id>:<mr_iid>:<event_id>

使用:
    from engram.logbook.source_id import build_mr_source_id, build_svn_source_id
    
    source_id = build_mr_source_id(repo_id=1, mr_iid=42)
    # => "mr:1:42"
"""

from typing import Union


def build_mr_source_id(repo_id: int, mr_iid: int) -> str:
    """
    构建 MR/PR 的 source_id

    格式: mr:<repo_id>:<iid>

    Args:
        repo_id: 数据库中的仓库 ID
        mr_iid: MR 在仓库内的 IID（GitLab 项目内唯一 ID）

    Returns:
        source_id 字符串
    """
    return f"mr:{repo_id}:{mr_iid}"


def build_svn_source_id(repo_id: int, rev_num: int) -> str:
    """
    构建 SVN revision 的 source_id

    格式: svn:<repo_id>:<rev_num>

    Args:
        repo_id: 数据库中的仓库 ID
        rev_num: SVN revision 号

    Returns:
        source_id 字符串
    """
    return f"svn:{repo_id}:{rev_num}"


def build_git_source_id(repo_id: int, commit_sha: str) -> str:
    """
    构建 Git commit 的 source_id

    格式: git:<repo_id>:<commit_sha>

    Args:
        repo_id: 数据库中的仓库 ID
        commit_sha: Git commit SHA

    Returns:
        source_id 字符串
    """
    return f"git:{repo_id}:{commit_sha}"


def build_review_event_source_id(repo_id: int, mr_iid: int, event_id: Union[int, str]) -> str:
    """
    构建 review event 的 source_id

    格式: review:<repo_id>:<mr_iid>:<event_id>

    Args:
        repo_id: 数据库中的仓库 ID
        mr_iid: MR 在仓库内的 IID
        event_id: 事件 ID（可以是数字或字符串）

    Returns:
        source_id 字符串
    """
    return f"review:{repo_id}:{mr_iid}:{event_id}"


def parse_mr_source_id(source_id: str) -> tuple:
    """
    解析 MR source_id

    Args:
        source_id: source_id 字符串，格式为 mr:<repo_id>:<iid>

    Returns:
        (repo_id, mr_iid) 元组

    Raises:
        ValueError: 格式不正确时
    """
    if not source_id or not source_id.startswith("mr:"):
        raise ValueError(f"Invalid MR source_id format: {source_id}")
    
    parts = source_id.split(":")
    if len(parts) != 3:
        raise ValueError(f"Invalid MR source_id format: {source_id}")
    
    try:
        repo_id = int(parts[1])
        mr_iid = int(parts[2])
        return (repo_id, mr_iid)
    except ValueError:
        raise ValueError(f"Invalid MR source_id format: {source_id}")


# 支持的 source_id 类型及其预期分段数
# 格式: type -> (min_parts, max_parts, description)
_SOURCE_ID_TYPES = {
    "svn": (3, 3, "svn:<repo_id>:<rev_num>"),
    "git": (3, 3, "git:<repo_id>:<commit_sha>"),
    "mr": (3, 3, "mr:<repo_id>:<iid>"),
    "review": (4, 4, "review:<repo_id>:<mr_iid>:<event_id>"),
}


def validate_source_id(source_id: str) -> bool:
    """
    验证 source_id 格式是否有效

    支持的格式:
    - svn:<repo_id>:<rev_num>
    - git:<repo_id>:<commit_sha>
    - mr:<repo_id>:<iid>
    - review:<repo_id>:<mr_iid>:<event_id>

    Args:
        source_id: 待验证的 source_id 字符串

    Returns:
        True 如果格式有效，False 否则
    """
    if not source_id or not isinstance(source_id, str):
        return False
    
    parts = source_id.split(":")
    if len(parts) < 2:
        return False
    
    source_type = parts[0]
    if source_type not in _SOURCE_ID_TYPES:
        return False
    
    min_parts, max_parts, _ = _SOURCE_ID_TYPES[source_type]
    if not (min_parts <= len(parts) <= max_parts):
        return False
    
    # 验证 repo_id 必须是数字
    try:
        int(parts[1])
    except ValueError:
        return False
    
    # 类型特定验证
    if source_type == "svn":
        # rev_num 必须是数字
        try:
            int(parts[2])
        except ValueError:
            return False
    elif source_type == "git":
        # commit_sha 不能为空
        if not parts[2]:
            return False
    elif source_type == "mr":
        # iid 必须是数字
        try:
            int(parts[2])
        except ValueError:
            return False
    elif source_type == "review":
        # mr_iid 必须是数字，event_id 不能为空
        try:
            int(parts[2])
        except ValueError:
            return False
        if not parts[3]:
            return False
    
    return True


def parse_source_id(source_id: str) -> dict:
    """
    解析 source_id 为结构化字典

    支持的格式:
    - svn:<repo_id>:<rev_num>     -> {"type": "svn", "repo_id": int, "rev_num": int}
    - git:<repo_id>:<commit_sha>  -> {"type": "git", "repo_id": int, "commit_sha": str}
    - mr:<repo_id>:<iid>          -> {"type": "mr", "repo_id": int, "iid": int}
    - review:<repo_id>:<mr_iid>:<event_id> -> {"type": "review", "repo_id": int, "mr_iid": int, "event_id": str}

    Args:
        source_id: source_id 字符串

    Returns:
        包含解析结果的字典

    Raises:
        ValueError: 格式无效时
    """
    if not validate_source_id(source_id):
        raise ValueError(f"Invalid source_id format: {source_id}")
    
    parts = source_id.split(":")
    source_type = parts[0]
    repo_id = int(parts[1])
    
    if source_type == "svn":
        return {
            "type": "svn",
            "repo_id": repo_id,
            "rev_num": int(parts[2]),
        }
    elif source_type == "git":
        return {
            "type": "git",
            "repo_id": repo_id,
            "commit_sha": parts[2],
        }
    elif source_type == "mr":
        return {
            "type": "mr",
            "repo_id": repo_id,
            "iid": int(parts[2]),
        }
    elif source_type == "review":
        return {
            "type": "review",
            "repo_id": repo_id,
            "mr_iid": int(parts[2]),
            "event_id": parts[3],
        }
    else:
        # 不应该到达这里，因为 validate_source_id 已经检查过
        raise ValueError(f"Unknown source_id type: {source_type}")


# ============ URL 构建工具函数 ============


def normalize_url(url: str) -> str:
    """
    规范化 URL：去除首尾空白和尾随斜杠

    Args:
        url: 原始 URL 字符串

    Returns:
        规范化后的 URL
    """
    if not url:
        return ""
    return url.strip().rstrip("/")


def build_gitlab_repo_url(gitlab_url: str, project_id_or_path: Union[int, str]) -> str:
    """
    构建 GitLab 仓库的规范 URL

    对于 namespace/project 格式的项目路径，返回: https://gitlab.example.com/namespace/project
    对于数字 ID，返回: https://gitlab.example.com/-/projects/<id>

    Args:
        gitlab_url: GitLab 实例 URL (如 https://gitlab.example.com)
        project_id_or_path: 项目 ID (数字) 或项目路径 (namespace/project 格式)

    Returns:
        规范化的仓库 URL

    Examples:
        >>> build_gitlab_repo_url("https://gitlab.example.com/", "namespace/project")
        'https://gitlab.example.com/namespace/project'
        >>> build_gitlab_repo_url("https://gitlab.example.com", 123)
        'https://gitlab.example.com/-/projects/123'
    """
    base = normalize_url(gitlab_url)
    project_str = str(project_id_or_path).strip()
    
    if "/" in project_str:
        # namespace/project 格式，去除项目路径的首尾斜杠
        project_path = project_str.strip("/")
        return f"{base}/{project_path}"
    else:
        # 数字 ID 格式
        return f"{base}/-/projects/{project_str}"
