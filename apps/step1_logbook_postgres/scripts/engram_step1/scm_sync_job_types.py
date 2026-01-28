"""
engram_step1.scm_sync_job_types - SCM 同步 job_type 归一化模块

功能:
- 定义 logical_job_type（逻辑任务类型）与 physical_job_type（物理任务类型）
- 提供 logical -> physical 的映射函数（根据 repo_type 决定）
- 确保同一语义任务在队列中有唯一标识

设计原则:
- scheduler 入队时使用 physical_job_type（确保唯一键语义清晰）
- worker 处理和过滤也以 physical_job_type 为主
- policy/cursor 等内部逻辑可用 logical_job_type（但在边界处转换）

逻辑任务类型 (logical_job_type):
- commits: 提交记录同步（Git 或 SVN）
- mrs: Merge Request 同步（仅 Git）
- reviews: Review 事件同步（仅 Git）
- svn: SVN 专用（等同于 commits 但显式指定）

物理任务类型 (physical_job_type):
- gitlab_commits: GitLab 提交记录
- gitlab_mrs: GitLab Merge Requests
- gitlab_reviews: GitLab Review 事件
- svn: SVN 提交记录

使用示例:
    from engram_step1.scm_sync_job_types import (
        logical_to_physical,
        physical_to_logical,
        get_physical_job_types_for_repo,
        PhysicalJobType,
        LogicalJobType,
    )
    
    # 根据 repo_type 获取 physical job_type
    physical = logical_to_physical("commits", repo_type="git")  # -> "gitlab_commits"
    physical = logical_to_physical("commits", repo_type="svn")  # -> "svn"
    
    # 获取仓库支持的所有 physical job_types
    job_types = get_physical_job_types_for_repo("git")  # -> ["gitlab_commits", "gitlab_mrs", "gitlab_reviews"]
    job_types = get_physical_job_types_for_repo("svn")  # -> ["svn"]
"""

from enum import Enum
from typing import List, Optional


# ============ 枚举定义 ============


class LogicalJobType(str, Enum):
    """
    逻辑任务类型
    
    表示抽象的同步任务语义，与具体 SCM 类型无关。
    用于 policy 层和配置中。
    """
    COMMITS = "commits"      # 提交记录（Git commits 或 SVN revisions）
    MRS = "mrs"              # Merge Requests（仅 Git）
    REVIEWS = "reviews"      # Review 事件（仅 Git）
    SVN = "svn"              # SVN（显式指定，等同于 commits for svn）


class PhysicalJobType(str, Enum):
    """
    物理任务类型
    
    表示实际执行的同步任务类型，与具体 SCM 实现绑定。
    用于 scheduler 入队、worker 处理、队列唯一键。
    """
    GITLAB_COMMITS = "gitlab_commits"    # GitLab 提交记录
    GITLAB_MRS = "gitlab_mrs"            # GitLab Merge Requests
    GITLAB_REVIEWS = "gitlab_reviews"    # GitLab Review 事件
    SVN = "svn"                          # SVN 提交记录


class RepoType(str, Enum):
    """仓库类型"""
    GIT = "git"
    SVN = "svn"


# ============ 常量定义 ============


# 所有物理任务类型列表
ALL_PHYSICAL_JOB_TYPES: List[str] = [
    PhysicalJobType.GITLAB_COMMITS.value,
    PhysicalJobType.GITLAB_MRS.value,
    PhysicalJobType.GITLAB_REVIEWS.value,
    PhysicalJobType.SVN.value,
]

# Git 仓库支持的物理任务类型
GIT_PHYSICAL_JOB_TYPES: List[str] = [
    PhysicalJobType.GITLAB_COMMITS.value,
    PhysicalJobType.GITLAB_MRS.value,
    PhysicalJobType.GITLAB_REVIEWS.value,
]

# SVN 仓库支持的物理任务类型
SVN_PHYSICAL_JOB_TYPES: List[str] = [
    PhysicalJobType.SVN.value,
]

# 所有逻辑任务类型列表
ALL_LOGICAL_JOB_TYPES: List[str] = [
    LogicalJobType.COMMITS.value,
    LogicalJobType.MRS.value,
    LogicalJobType.REVIEWS.value,
    LogicalJobType.SVN.value,
]

# Git 仓库支持的逻辑任务类型（用于 policy 层）
GIT_LOGICAL_JOB_TYPES: List[str] = [
    LogicalJobType.COMMITS.value,
    LogicalJobType.MRS.value,
    LogicalJobType.REVIEWS.value,
]

# SVN 仓库支持的逻辑任务类型
SVN_LOGICAL_JOB_TYPES: List[str] = [
    LogicalJobType.COMMITS.value,  # SVN 的 commits 映射为 svn physical type
]


# ============ 映射函数 ============


def logical_to_physical(
    logical_job_type: str,
    repo_type: str,
) -> str:
    """
    将 logical_job_type 转换为 physical_job_type
    
    根据 repo_type 决定实际的物理任务类型。
    
    Args:
        logical_job_type: 逻辑任务类型 (commits/mrs/reviews/svn)
        repo_type: 仓库类型 (git/svn)
        
    Returns:
        对应的 physical_job_type
        
    Raises:
        ValueError: 无效的组合（如 mrs + svn）
        
    Examples:
        >>> logical_to_physical("commits", "git")
        "gitlab_commits"
        >>> logical_to_physical("commits", "svn")
        "svn"
        >>> logical_to_physical("mrs", "git")
        "gitlab_mrs"
        >>> logical_to_physical("svn", "svn")
        "svn"
    """
    # 规范化输入
    logical = logical_job_type.lower().strip()
    repo = repo_type.lower().strip()
    
    # SVN 显式类型
    if logical == LogicalJobType.SVN.value:
        if repo != RepoType.SVN.value:
            raise ValueError(
                f"logical_job_type='svn' 仅适用于 repo_type='svn'，当前 repo_type='{repo_type}'"
            )
        return PhysicalJobType.SVN.value
    
    # Git 仓库映射
    if repo == RepoType.GIT.value:
        mapping = {
            LogicalJobType.COMMITS.value: PhysicalJobType.GITLAB_COMMITS.value,
            LogicalJobType.MRS.value: PhysicalJobType.GITLAB_MRS.value,
            LogicalJobType.REVIEWS.value: PhysicalJobType.GITLAB_REVIEWS.value,
        }
        if logical in mapping:
            return mapping[logical]
        raise ValueError(
            f"无效的 logical_job_type='{logical_job_type}' for repo_type='git'，"
            f"有效值: {list(mapping.keys())}"
        )
    
    # SVN 仓库映射
    if repo == RepoType.SVN.value:
        if logical == LogicalJobType.COMMITS.value:
            return PhysicalJobType.SVN.value
        raise ValueError(
            f"logical_job_type='{logical_job_type}' 不适用于 SVN 仓库，"
            f"SVN 仅支持 'commits' 或 'svn'"
        )
    
    raise ValueError(f"无效的 repo_type='{repo_type}'，有效值: git, svn")


def physical_to_logical(physical_job_type: str) -> str:
    """
    将 physical_job_type 转换为 logical_job_type
    
    用于从队列任务反向映射到逻辑类型（如日志、监控）。
    
    Args:
        physical_job_type: 物理任务类型
        
    Returns:
        对应的 logical_job_type
        
    Raises:
        ValueError: 无效的 physical_job_type
        
    Examples:
        >>> physical_to_logical("gitlab_commits")
        "commits"
        >>> physical_to_logical("svn")
        "commits"  # SVN 也是 commits 的实现
    """
    physical = physical_job_type.lower().strip()
    
    mapping = {
        PhysicalJobType.GITLAB_COMMITS.value: LogicalJobType.COMMITS.value,
        PhysicalJobType.GITLAB_MRS.value: LogicalJobType.MRS.value,
        PhysicalJobType.GITLAB_REVIEWS.value: LogicalJobType.REVIEWS.value,
        PhysicalJobType.SVN.value: LogicalJobType.COMMITS.value,  # SVN 也是 commits
    }
    
    if physical in mapping:
        return mapping[physical]
    
    raise ValueError(
        f"无效的 physical_job_type='{physical_job_type}'，"
        f"有效值: {list(mapping.keys())}"
    )


def get_physical_job_types_for_repo(repo_type: str) -> List[str]:
    """
    获取仓库类型支持的所有 physical_job_type
    
    用于 scheduler 决定应该为仓库创建哪些任务。
    
    Args:
        repo_type: 仓库类型 (git/svn)
        
    Returns:
        该仓库类型支持的 physical_job_type 列表
        
    Examples:
        >>> get_physical_job_types_for_repo("git")
        ["gitlab_commits", "gitlab_mrs", "gitlab_reviews"]
        >>> get_physical_job_types_for_repo("svn")
        ["svn"]
    """
    repo = repo_type.lower().strip()
    
    if repo == RepoType.GIT.value:
        return list(GIT_PHYSICAL_JOB_TYPES)
    elif repo == RepoType.SVN.value:
        return list(SVN_PHYSICAL_JOB_TYPES)
    else:
        raise ValueError(f"无效的 repo_type='{repo_type}'，有效值: git, svn")


def get_logical_job_types_for_repo(repo_type: str) -> List[str]:
    """
    获取仓库类型支持的所有 logical_job_type
    
    用于 policy 层决定应该调度哪些逻辑任务。
    
    Args:
        repo_type: 仓库类型 (git/svn)
        
    Returns:
        该仓库类型支持的 logical_job_type 列表
        
    Examples:
        >>> get_logical_job_types_for_repo("git")
        ["commits", "mrs", "reviews"]
        >>> get_logical_job_types_for_repo("svn")
        ["commits"]
    """
    repo = repo_type.lower().strip()
    
    if repo == RepoType.GIT.value:
        return list(GIT_LOGICAL_JOB_TYPES)
    elif repo == RepoType.SVN.value:
        return list(SVN_LOGICAL_JOB_TYPES)
    else:
        raise ValueError(f"无效的 repo_type='{repo_type}'，有效值: git, svn")


def is_valid_physical_job_type(job_type: str) -> bool:
    """
    验证是否为有效的 physical_job_type
    
    Args:
        job_type: 待验证的任务类型
        
    Returns:
        是否有效
    """
    return job_type.lower().strip() in ALL_PHYSICAL_JOB_TYPES


def is_valid_logical_job_type(job_type: str) -> bool:
    """
    验证是否为有效的 logical_job_type
    
    Args:
        job_type: 待验证的任务类型
        
    Returns:
        是否有效
    """
    return job_type.lower().strip() in ALL_LOGICAL_JOB_TYPES


def normalize_job_type(
    job_type: str,
    repo_type: Optional[str] = None,
) -> str:
    """
    规范化任务类型为 physical_job_type
    
    接受 logical 或 physical 类型，统一返回 physical 类型。
    如果输入已经是 physical 类型则直接返回。
    如果输入是 logical 类型则需要 repo_type 进行转换。
    
    Args:
        job_type: 任务类型（logical 或 physical）
        repo_type: 仓库类型（当 job_type 是 logical 时必需）
        
    Returns:
        规范化后的 physical_job_type
        
    Examples:
        >>> normalize_job_type("gitlab_commits")
        "gitlab_commits"
        >>> normalize_job_type("commits", repo_type="git")
        "gitlab_commits"
    """
    normalized = job_type.lower().strip()
    
    # 如果已经是 physical 类型，直接返回
    if normalized in ALL_PHYSICAL_JOB_TYPES:
        return normalized
    
    # 如果是 logical 类型，需要 repo_type 进行转换
    if normalized in ALL_LOGICAL_JOB_TYPES:
        if repo_type is None:
            raise ValueError(
                f"job_type='{job_type}' 是 logical 类型，需要提供 repo_type 进行转换"
            )
        return logical_to_physical(normalized, repo_type)
    
    raise ValueError(
        f"无效的 job_type='{job_type}'，"
        f"有效的 physical 类型: {ALL_PHYSICAL_JOB_TYPES}，"
        f"有效的 logical 类型: {ALL_LOGICAL_JOB_TYPES}"
    )


def get_repo_type_for_physical_job_type(physical_job_type: str) -> str:
    """
    根据 physical_job_type 推断 repo_type
    
    Args:
        physical_job_type: 物理任务类型
        
    Returns:
        对应的 repo_type
        
    Examples:
        >>> get_repo_type_for_physical_job_type("gitlab_commits")
        "git"
        >>> get_repo_type_for_physical_job_type("svn")
        "svn"
    """
    physical = physical_job_type.lower().strip()
    
    if physical in GIT_PHYSICAL_JOB_TYPES:
        return RepoType.GIT.value
    elif physical in SVN_PHYSICAL_JOB_TYPES:
        return RepoType.SVN.value
    else:
        raise ValueError(f"无效的 physical_job_type='{physical_job_type}'")


# ============ 默认优先级 ============


# 默认的 physical_job_type 优先级（数值越小优先级越高）
DEFAULT_PHYSICAL_JOB_TYPE_PRIORITY = {
    PhysicalJobType.GITLAB_COMMITS.value: 100,
    PhysicalJobType.GITLAB_MRS.value: 200,
    PhysicalJobType.GITLAB_REVIEWS.value: 300,
    PhysicalJobType.SVN.value: 100,
}


def get_job_type_priority(physical_job_type: str) -> int:
    """
    获取 physical_job_type 的默认优先级
    
    Args:
        physical_job_type: 物理任务类型
        
    Returns:
        优先级数值（越小越优先）
    """
    physical = physical_job_type.lower().strip()
    return DEFAULT_PHYSICAL_JOB_TYPE_PRIORITY.get(physical, 500)
