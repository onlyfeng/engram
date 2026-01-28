#!/usr/bin/env python3
"""
scm_integrity_check.py - SCM 数据完整性检查工具

检查 scm schema 下的数据完整性问题：

1. source_id 格式检查
   - scm.svn_revisions: source_id 应为 svn:<repo_id>:<rev_num>
   - scm.git_commits: source_id 应为 git:<repo_id>:<commit_sha>
   - scm.mrs: source_id 应为 mr:<repo_id>:<iid>

2. MR 重复检测
   - 检测同一 (repo_id, iid) 是否存在多个 mr_id

3. Repo URL 重复检测
   - 检测同一 GitLab 项目是否存在多个 URL 形态

4. patch_blobs 完整性检查
   - evidence_uri 缺失或格式无效
   - uri 引用的制品不存在（--check-artifacts）
   - sha256 与实际文件内容不匹配（--verify-sha256）

使用:
    # 仅检查，输出问题报告
    python scm_integrity_check.py

    # 检查并修复（在事务内执行）
    python scm_integrity_check.py --fix

    # 输出详细信息
    python scm_integrity_check.py -v

    # 输出 JSON 格式
    python scm_integrity_check.py --json

    # 检查制品文件是否存在
    python scm_integrity_check.py --check-artifacts

    # 验证 sha256（限制 100 条，避免全量校验过慢）
    python scm_integrity_check.py --check-artifacts --verify-sha256 --limit 100
"""

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import psycopg
from psycopg.rows import dict_row

from engram_step1.config import Config, add_config_argument, get_config
from engram_step1.db import get_connection
from engram_step1.errors import DatabaseError, EngramError
from engram_step1.source_id import (
    build_git_source_id,
    build_mr_source_id,
    build_svn_source_id,
    validate_source_id,
)
from engram_step1.uri import parse_evidence_uri
from artifacts import artifact_exists, get_artifact_info

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ============ 数据类定义 ============


@dataclass
class SourceIdIssue:
    """source_id 问题记录"""
    table: str
    pk_column: str
    pk_value: Any
    repo_id: int
    current_source_id: Optional[str]
    expected_source_id: str
    issue_type: str  # 'missing' | 'invalid_format' | 'wrong_value'


@dataclass
class DuplicateMrIssue:
    """MR 重复问题记录"""
    repo_id: int
    iid: int
    mr_ids: List[str]
    recommended_mr_id: str  # 保留的 mr_id（通常是最早创建的）
    duplicate_mr_ids: List[str]  # 需要合并/删除的 mr_id


@dataclass
class DuplicateRepoUrlIssue:
    """Repo URL 重复问题记录"""
    project_key: str
    urls: List[str]
    repo_ids: List[int]
    normalized_url: str
    recommended_repo_id: int  # 推荐保留的 repo_id
    duplicate_repo_ids: List[int]  # 需要合并的 repo_id


@dataclass
class PatchBlobIssue:
    """patch_blobs 问题记录"""
    blob_id: int
    source_type: str
    source_id: Optional[str]
    uri: Optional[str]
    evidence_uri: Optional[str]
    sha256: Optional[str]
    issue_type: str  # 'missing_evidence_uri' | 'invalid_evidence_uri' | 'uri_not_resolvable' | 'artifact_not_found' | 'sha256_mismatch'
    details: Optional[str] = None  # 问题详情


@dataclass
class IntegrityCheckResult:
    """完整性检查结果"""
    source_id_issues: List[SourceIdIssue] = field(default_factory=list)
    duplicate_mr_issues: List[DuplicateMrIssue] = field(default_factory=list)
    duplicate_repo_url_issues: List[DuplicateRepoUrlIssue] = field(default_factory=list)
    patch_blob_issues: List[PatchBlobIssue] = field(default_factory=list)

    @property
    def has_issues(self) -> bool:
        return bool(
            self.source_id_issues or
            self.duplicate_mr_issues or
            self.duplicate_repo_url_issues or
            self.patch_blob_issues
        )

    @property
    def issue_count(self) -> int:
        return (
            len(self.source_id_issues) +
            len(self.duplicate_mr_issues) +
            len(self.duplicate_repo_url_issues) +
            len(self.patch_blob_issues)
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "has_issues": self.has_issues,
            "issue_count": self.issue_count,
            "source_id_issues": [
                {
                    "table": i.table,
                    "pk_column": i.pk_column,
                    "pk_value": i.pk_value,
                    "repo_id": i.repo_id,
                    "current_source_id": i.current_source_id,
                    "expected_source_id": i.expected_source_id,
                    "issue_type": i.issue_type,
                }
                for i in self.source_id_issues
            ],
            "duplicate_mr_issues": [
                {
                    "repo_id": i.repo_id,
                    "iid": i.iid,
                    "mr_ids": i.mr_ids,
                    "recommended_mr_id": i.recommended_mr_id,
                    "duplicate_mr_ids": i.duplicate_mr_ids,
                }
                for i in self.duplicate_mr_issues
            ],
            "duplicate_repo_url_issues": [
                {
                    "project_key": i.project_key,
                    "urls": i.urls,
                    "repo_ids": i.repo_ids,
                    "normalized_url": i.normalized_url,
                    "recommended_repo_id": i.recommended_repo_id,
                    "duplicate_repo_ids": i.duplicate_repo_ids,
                }
                for i in self.duplicate_repo_url_issues
            ],
            "patch_blob_issues": [
                {
                    "blob_id": i.blob_id,
                    "source_type": i.source_type,
                    "source_id": i.source_id,
                    "uri": i.uri,
                    "evidence_uri": i.evidence_uri,
                    "sha256": i.sha256,
                    "issue_type": i.issue_type,
                    "details": i.details,
                }
                for i in self.patch_blob_issues
            ],
        }


@dataclass
class FixResult:
    """修复执行结果"""
    source_id_fixed: int = 0
    source_id_skipped: int = 0
    mr_deduplicated: int = 0
    repo_merge_sql: List[str] = field(default_factory=list)  # Repo 合并需要手动执行

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_id_fixed": self.source_id_fixed,
            "source_id_skipped": self.source_id_skipped,
            "mr_deduplicated": self.mr_deduplicated,
            "repo_merge_sql": self.repo_merge_sql,
        }


# ============ 检查函数 ============


def check_svn_source_ids(conn: psycopg.Connection) -> List[SourceIdIssue]:
    """
    检查 scm.svn_revisions 的 source_id

    Returns:
        问题记录列表
    """
    issues = []
    
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT svn_rev_id, repo_id, COALESCE(rev_num, rev_id) as rev_num, source_id
            FROM scm.svn_revisions
        """)
        
        for row in cur.fetchall():
            svn_rev_id = row["svn_rev_id"]
            repo_id = row["repo_id"]
            rev_num = row["rev_num"]
            current_source_id = row["source_id"]
            
            expected_source_id = build_svn_source_id(repo_id, rev_num)
            
            if current_source_id is None:
                issues.append(SourceIdIssue(
                    table="scm.svn_revisions",
                    pk_column="svn_rev_id",
                    pk_value=svn_rev_id,
                    repo_id=repo_id,
                    current_source_id=None,
                    expected_source_id=expected_source_id,
                    issue_type="missing",
                ))
            elif not validate_source_id(current_source_id):
                issues.append(SourceIdIssue(
                    table="scm.svn_revisions",
                    pk_column="svn_rev_id",
                    pk_value=svn_rev_id,
                    repo_id=repo_id,
                    current_source_id=current_source_id,
                    expected_source_id=expected_source_id,
                    issue_type="invalid_format",
                ))
            elif current_source_id != expected_source_id:
                issues.append(SourceIdIssue(
                    table="scm.svn_revisions",
                    pk_column="svn_rev_id",
                    pk_value=svn_rev_id,
                    repo_id=repo_id,
                    current_source_id=current_source_id,
                    expected_source_id=expected_source_id,
                    issue_type="wrong_value",
                ))
    
    return issues


def check_git_source_ids(conn: psycopg.Connection) -> List[SourceIdIssue]:
    """
    检查 scm.git_commits 的 source_id

    Returns:
        问题记录列表
    """
    issues = []
    
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT git_commit_id, repo_id, COALESCE(commit_sha, commit_id) as commit_sha, source_id
            FROM scm.git_commits
        """)
        
        for row in cur.fetchall():
            git_commit_id = row["git_commit_id"]
            repo_id = row["repo_id"]
            commit_sha = row["commit_sha"]
            current_source_id = row["source_id"]
            
            expected_source_id = build_git_source_id(repo_id, commit_sha)
            
            if current_source_id is None:
                issues.append(SourceIdIssue(
                    table="scm.git_commits",
                    pk_column="git_commit_id",
                    pk_value=git_commit_id,
                    repo_id=repo_id,
                    current_source_id=None,
                    expected_source_id=expected_source_id,
                    issue_type="missing",
                ))
            elif not validate_source_id(current_source_id):
                issues.append(SourceIdIssue(
                    table="scm.git_commits",
                    pk_column="git_commit_id",
                    pk_value=git_commit_id,
                    repo_id=repo_id,
                    current_source_id=current_source_id,
                    expected_source_id=expected_source_id,
                    issue_type="invalid_format",
                ))
            elif current_source_id != expected_source_id:
                issues.append(SourceIdIssue(
                    table="scm.git_commits",
                    pk_column="git_commit_id",
                    pk_value=git_commit_id,
                    repo_id=repo_id,
                    current_source_id=current_source_id,
                    expected_source_id=expected_source_id,
                    issue_type="wrong_value",
                ))
    
    return issues


def check_mr_source_ids(conn: psycopg.Connection) -> List[SourceIdIssue]:
    """
    检查 scm.mrs 的 source_id

    mr_id 格式为 <repo_id>:<iid>，从中解析 iid 来构建期望的 source_id

    Returns:
        问题记录列表
    """
    issues = []
    
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT mr_id, repo_id, source_id
            FROM scm.mrs
        """)
        
        for row in cur.fetchall():
            mr_id = row["mr_id"]
            repo_id = row["repo_id"]
            current_source_id = row["source_id"]
            
            # 从 mr_id 解析 iid
            # mr_id 格式: <repo_id>:<iid>
            parts = mr_id.split(":")
            if len(parts) >= 2:
                try:
                    iid = int(parts[-1])
                except ValueError:
                    # 无法解析 iid，跳过
                    logger.warning(f"无法从 mr_id={mr_id} 解析 iid")
                    continue
            else:
                logger.warning(f"mr_id 格式异常: {mr_id}")
                continue
            
            expected_source_id = build_mr_source_id(repo_id, iid)
            
            if current_source_id is None:
                issues.append(SourceIdIssue(
                    table="scm.mrs",
                    pk_column="mr_id",
                    pk_value=mr_id,
                    repo_id=repo_id,
                    current_source_id=None,
                    expected_source_id=expected_source_id,
                    issue_type="missing",
                ))
            elif not validate_source_id(current_source_id):
                issues.append(SourceIdIssue(
                    table="scm.mrs",
                    pk_column="mr_id",
                    pk_value=mr_id,
                    repo_id=repo_id,
                    current_source_id=current_source_id,
                    expected_source_id=expected_source_id,
                    issue_type="invalid_format",
                ))
            elif current_source_id != expected_source_id:
                issues.append(SourceIdIssue(
                    table="scm.mrs",
                    pk_column="mr_id",
                    pk_value=mr_id,
                    repo_id=repo_id,
                    current_source_id=current_source_id,
                    expected_source_id=expected_source_id,
                    issue_type="wrong_value",
                ))
    
    return issues


def check_duplicate_mrs(conn: psycopg.Connection) -> List[DuplicateMrIssue]:
    """
    检测同一 (repo_id, iid) 是否存在多个 mr_id

    通过解析 mr_id（格式 <repo_id>:<iid>）来检测重复

    Returns:
        重复问题记录列表
    """
    issues = []
    
    with conn.cursor(row_factory=dict_row) as cur:
        # 获取所有 MR 并按 (repo_id, iid) 分组检测重复
        cur.execute("""
            SELECT mr_id, repo_id, created_at
            FROM scm.mrs
            ORDER BY created_at ASC
        """)
        
        # 按 (repo_id, iid) 分组
        mr_groups: Dict[Tuple[int, int], List[Dict]] = {}
        
        for row in cur.fetchall():
            mr_id = row["mr_id"]
            repo_id = row["repo_id"]
            created_at = row["created_at"]
            
            # 从 mr_id 解析 iid
            parts = mr_id.split(":")
            if len(parts) >= 2:
                try:
                    iid = int(parts[-1])
                except ValueError:
                    continue
            else:
                continue
            
            key = (repo_id, iid)
            if key not in mr_groups:
                mr_groups[key] = []
            mr_groups[key].append({
                "mr_id": mr_id,
                "created_at": created_at,
            })
        
        # 检测重复
        for (repo_id, iid), mrs in mr_groups.items():
            if len(mrs) > 1:
                # 按创建时间排序，保留最早的
                mrs_sorted = sorted(mrs, key=lambda x: x["created_at"] or "")
                recommended = mrs_sorted[0]["mr_id"]
                all_mr_ids = [m["mr_id"] for m in mrs_sorted]
                duplicates = [m["mr_id"] for m in mrs_sorted[1:]]
                
                issues.append(DuplicateMrIssue(
                    repo_id=repo_id,
                    iid=iid,
                    mr_ids=all_mr_ids,
                    recommended_mr_id=recommended,
                    duplicate_mr_ids=duplicates,
                ))
    
    return issues


def normalize_gitlab_url(url: str) -> str:
    """
    规范化 GitLab URL

    处理以下变体：
    - http vs https
    - 有无尾随斜杠
    - .git 后缀
    - 大小写（路径部分）

    Returns:
        规范化后的 URL（小写，https，无尾随斜杠，无 .git）
    """
    if not url:
        return ""
    
    url = url.strip().rstrip("/")
    
    # 移除 .git 后缀
    if url.endswith(".git"):
        url = url[:-4]
    
    # 解析 URL
    parsed = urlparse(url)
    
    # 统一 scheme 为 https
    scheme = "https"
    
    # 路径小写
    path = parsed.path.lower().rstrip("/")
    
    # 主机名小写
    netloc = parsed.netloc.lower()
    
    return f"{scheme}://{netloc}{path}"


def check_duplicate_repo_urls(conn: psycopg.Connection) -> List[DuplicateRepoUrlIssue]:
    """
    检测同一 GitLab 项目是否存在多个 URL 形态

    检测规则：
    - 同一 project_key 下的不同 URL
    - URL 规范化后相同的记录

    Returns:
        重复问题记录列表
    """
    issues = []
    
    with conn.cursor(row_factory=dict_row) as cur:
        # 获取所有 git 类型的仓库
        cur.execute("""
            SELECT repo_id, url, project_key, created_at
            FROM scm.repos
            WHERE repo_type = 'git'
            ORDER BY created_at ASC
        """)
        
        # 按规范化 URL 分组
        url_groups: Dict[str, List[Dict]] = {}
        
        for row in cur.fetchall():
            repo_id = row["repo_id"]
            url = row["url"]
            project_key = row["project_key"]
            created_at = row["created_at"]
            
            normalized = normalize_gitlab_url(url)
            
            if normalized not in url_groups:
                url_groups[normalized] = []
            url_groups[normalized].append({
                "repo_id": repo_id,
                "url": url,
                "project_key": project_key,
                "created_at": created_at,
            })
        
        # 检测规范化后重复的 URL
        for normalized_url, repos in url_groups.items():
            if len(repos) > 1:
                # 按创建时间排序，保留最早的
                repos_sorted = sorted(repos, key=lambda x: x["created_at"] or "")
                recommended = repos_sorted[0]["repo_id"]
                all_urls = [r["url"] for r in repos_sorted]
                all_repo_ids = [r["repo_id"] for r in repos_sorted]
                duplicates = [r["repo_id"] for r in repos_sorted[1:]]
                project_key = repos_sorted[0]["project_key"]
                
                issues.append(DuplicateRepoUrlIssue(
                    project_key=project_key,
                    urls=all_urls,
                    repo_ids=all_repo_ids,
                    normalized_url=normalized_url,
                    recommended_repo_id=recommended,
                    duplicate_repo_ids=duplicates,
                ))
    
    return issues


def check_patch_blobs(
    conn: psycopg.Connection,
    check_artifacts: bool = False,
    verify_sha256: bool = False,
    verify_limit: Optional[int] = None,
) -> List[PatchBlobIssue]:
    """
    检查 scm.patch_blobs 的数据完整性

    检查项:
    1. evidence_uri 缺失或无效
    2. uri 不可解析或制品不存在（check_artifacts=True 时）
    3. sha256 与实际文件内容不匹配（verify_sha256=True 时）

    Args:
        conn: 数据库连接
        check_artifacts: 是否检查制品文件存在性
        verify_sha256: 是否验证 sha256 哈希值
        verify_limit: 验证数量限制（仅在 verify_sha256=True 时生效）

    Returns:
        问题记录列表
    """
    issues = []
    
    with conn.cursor(row_factory=dict_row) as cur:
        # 构建查询
        query = """
            SELECT blob_id, source_type, source_id, uri, evidence_uri, sha256
            FROM scm.patch_blobs
        """
        if verify_limit and verify_sha256:
            query += f" LIMIT {int(verify_limit)}"
        
        cur.execute(query)
        
        for row in cur.fetchall():
            blob_id = row["blob_id"]
            source_type = row["source_type"]
            source_id = row["source_id"]
            uri = row["uri"]
            evidence_uri = row["evidence_uri"]
            sha256_value = row["sha256"]
            
            # 检查 1: evidence_uri 缺失
            if not evidence_uri:
                issues.append(PatchBlobIssue(
                    blob_id=blob_id,
                    source_type=source_type,
                    source_id=source_id,
                    uri=uri,
                    evidence_uri=None,
                    sha256=sha256_value,
                    issue_type="missing_evidence_uri",
                    details="evidence_uri 字段为空",
                ))
                continue
            
            # 检查 2: evidence_uri 格式无效
            parsed = parse_evidence_uri(evidence_uri)
            if parsed is None:
                issues.append(PatchBlobIssue(
                    blob_id=blob_id,
                    source_type=source_type,
                    source_id=source_id,
                    uri=uri,
                    evidence_uri=evidence_uri,
                    sha256=sha256_value,
                    issue_type="invalid_evidence_uri",
                    details=f"无法解析 evidence_uri: {evidence_uri}",
                ))
                continue
            
            # 检查 3: 制品存在性（仅在 check_artifacts=True 时）
            if check_artifacts and uri:
                if not artifact_exists(uri):
                    issues.append(PatchBlobIssue(
                        blob_id=blob_id,
                        source_type=source_type,
                        source_id=source_id,
                        uri=uri,
                        evidence_uri=evidence_uri,
                        sha256=sha256_value,
                        issue_type="artifact_not_found",
                        details=f"制品文件不存在: {uri}",
                    ))
                    continue
                
                # 检查 4: sha256 验证（仅在 verify_sha256=True 时）
                if verify_sha256 and sha256_value:
                    try:
                        artifact_info = get_artifact_info(uri)
                        actual_sha256 = artifact_info.get("sha256", "")
                        if actual_sha256 != sha256_value:
                            issues.append(PatchBlobIssue(
                                blob_id=blob_id,
                                source_type=source_type,
                                source_id=source_id,
                                uri=uri,
                                evidence_uri=evidence_uri,
                                sha256=sha256_value,
                                issue_type="sha256_mismatch",
                                details=f"sha256 不匹配: 数据库={sha256_value[:16]}..., 实际={actual_sha256[:16]}...",
                            ))
                    except Exception as e:
                        issues.append(PatchBlobIssue(
                            blob_id=blob_id,
                            source_type=source_type,
                            source_id=source_id,
                            uri=uri,
                            evidence_uri=evidence_uri,
                            sha256=sha256_value,
                            issue_type="uri_not_resolvable",
                            details=f"无法读取制品: {e}",
                        ))
            elif check_artifacts and not uri:
                # uri 为空
                issues.append(PatchBlobIssue(
                    blob_id=blob_id,
                    source_type=source_type,
                    source_id=source_id,
                    uri=None,
                    evidence_uri=evidence_uri,
                    sha256=sha256_value,
                    issue_type="uri_not_resolvable",
                    details="uri 字段为空",
                ))
    
    return issues


def run_integrity_check(
    conn: psycopg.Connection,
    check_artifacts: bool = False,
    verify_sha256: bool = False,
    verify_limit: Optional[int] = None,
) -> IntegrityCheckResult:
    """
    执行完整的完整性检查

    Args:
        conn: 数据库连接
        check_artifacts: 是否检查制品文件存在性
        verify_sha256: 是否验证 sha256 哈希值
        verify_limit: 验证数量限制（仅在 verify_sha256=True 时生效）

    Returns:
        检查结果
    """
    result = IntegrityCheckResult()
    
    logger.info("检查 scm.svn_revisions source_id...")
    result.source_id_issues.extend(check_svn_source_ids(conn))
    
    logger.info("检查 scm.git_commits source_id...")
    result.source_id_issues.extend(check_git_source_ids(conn))
    
    logger.info("检查 scm.mrs source_id...")
    result.source_id_issues.extend(check_mr_source_ids(conn))
    
    logger.info("检测 MR 重复...")
    result.duplicate_mr_issues = check_duplicate_mrs(conn)
    
    logger.info("检测 Repo URL 重复...")
    result.duplicate_repo_url_issues = check_duplicate_repo_urls(conn)
    
    # 检查 patch_blobs（始终检查 evidence_uri）
    logger.info("检查 scm.patch_blobs...")
    if verify_sha256:
        limit_info = f"（限制 {verify_limit} 条）" if verify_limit else "（全量）"
        logger.info(f"  启用 sha256 验证{limit_info}")
    result.patch_blob_issues = check_patch_blobs(
        conn,
        check_artifacts=check_artifacts,
        verify_sha256=verify_sha256,
        verify_limit=verify_limit,
    )
    
    return result


# ============ 修复函数 ============


def fix_source_ids(conn: psycopg.Connection, issues: List[SourceIdIssue]) -> Tuple[int, int]:
    """
    修复 source_id 问题

    在事务内执行，仅修复 missing 和 invalid_format 类型的问题

    Args:
        conn: 数据库连接
        issues: source_id 问题列表

    Returns:
        (fixed_count, skipped_count)
    """
    fixed = 0
    skipped = 0
    
    with conn.cursor() as cur:
        for issue in issues:
            # 仅修复 missing 和 invalid_format 类型
            if issue.issue_type in ("missing", "invalid_format"):
                if issue.table == "scm.svn_revisions":
                    cur.execute(
                        """
                        UPDATE scm.svn_revisions
                        SET source_id = %s
                        WHERE svn_rev_id = %s
                        """,
                        (issue.expected_source_id, issue.pk_value),
                    )
                elif issue.table == "scm.git_commits":
                    cur.execute(
                        """
                        UPDATE scm.git_commits
                        SET source_id = %s
                        WHERE git_commit_id = %s
                        """,
                        (issue.expected_source_id, issue.pk_value),
                    )
                elif issue.table == "scm.mrs":
                    cur.execute(
                        """
                        UPDATE scm.mrs
                        SET source_id = %s
                        WHERE mr_id = %s
                        """,
                        (issue.expected_source_id, issue.pk_value),
                    )
                
                fixed += 1
                logger.debug(
                    f"修复 {issue.table} {issue.pk_column}={issue.pk_value}: "
                    f"{issue.current_source_id} -> {issue.expected_source_id}"
                )
            else:
                # wrong_value 类型需要人工确认
                skipped += 1
                logger.warning(
                    f"跳过 {issue.table} {issue.pk_column}={issue.pk_value}: "
                    f"当前值 {issue.current_source_id} 与期望值 {issue.expected_source_id} 不一致，需人工确认"
                )
    
    return fixed, skipped


def generate_mr_dedup_sql(issues: List[DuplicateMrIssue]) -> List[str]:
    """
    生成 MR 去重的修复 SQL

    策略：
    1. 将重复 MR 的 review_events 迁移到保留的 MR
    2. 删除重复的 MR 记录

    Args:
        issues: MR 重复问题列表

    Returns:
        SQL 语句列表
    """
    sql_statements = []
    
    for issue in issues:
        recommended = issue.recommended_mr_id
        for dup_mr_id in issue.duplicate_mr_ids:
            # 迁移 review_events
            sql_statements.append(
                f"-- 迁移 review_events: {dup_mr_id} -> {recommended}\n"
                f"UPDATE scm.review_events\n"
                f"SET mr_id = '{recommended}'\n"
                f"WHERE mr_id = '{dup_mr_id}'\n"
                f"AND source_event_id NOT IN (\n"
                f"  SELECT source_event_id FROM scm.review_events WHERE mr_id = '{recommended}'\n"
                f");"
            )
            
            # 删除已迁移的重复 review_events（若有冲突）
            sql_statements.append(
                f"-- 删除冲突的 review_events\n"
                f"DELETE FROM scm.review_events WHERE mr_id = '{dup_mr_id}';"
            )
            
            # 删除重复的 MR
            sql_statements.append(
                f"-- 删除重复 MR: {dup_mr_id}\n"
                f"DELETE FROM scm.mrs WHERE mr_id = '{dup_mr_id}';"
            )
    
    return sql_statements


def generate_repo_merge_sql(issues: List[DuplicateRepoUrlIssue]) -> List[str]:
    """
    生成 Repo 合并的修复 SQL

    策略：
    1. 更新所有外键引用（svn_revisions, git_commits, mrs）
    2. 删除重复的 repo 记录

    注意：此操作涉及大量数据迁移，建议手动执行并验证

    Args:
        issues: Repo URL 重复问题列表

    Returns:
        SQL 语句列表
    """
    sql_statements = []
    
    for issue in issues:
        recommended = issue.recommended_repo_id
        for dup_repo_id in issue.duplicate_repo_ids:
            sql_statements.append(
                f"-- ====== 合并 repo_id {dup_repo_id} -> {recommended} ======\n"
                f"-- 项目: {issue.project_key}\n"
                f"-- URL: {issue.normalized_url}\n"
            )
            
            # 更新 svn_revisions
            sql_statements.append(
                f"-- 迁移 svn_revisions\n"
                f"UPDATE scm.svn_revisions\n"
                f"SET repo_id = {recommended},\n"
                f"    source_id = 'svn:' || {recommended} || ':' || COALESCE(rev_num, rev_id)\n"
                f"WHERE repo_id = {dup_repo_id};"
            )
            
            # 更新 git_commits
            sql_statements.append(
                f"-- 迁移 git_commits\n"
                f"UPDATE scm.git_commits\n"
                f"SET repo_id = {recommended},\n"
                f"    source_id = 'git:' || {recommended} || ':' || COALESCE(commit_sha, commit_id)\n"
                f"WHERE repo_id = {dup_repo_id};"
            )
            
            # 更新 mrs（需要重新生成 mr_id）
            sql_statements.append(
                f"-- 迁移 mrs（需要更新 mr_id 和 source_id）\n"
                f"-- 注意：此操作可能导致主键冲突，需要手动处理\n"
                f"UPDATE scm.mrs\n"
                f"SET repo_id = {recommended},\n"
                f"    mr_id = {recommended} || ':' || split_part(mr_id, ':', 2),\n"
                f"    source_id = 'mr:' || {recommended} || ':' || split_part(mr_id, ':', 2)\n"
                f"WHERE repo_id = {dup_repo_id}\n"
                f"AND NOT EXISTS (\n"
                f"  SELECT 1 FROM scm.mrs m2\n"
                f"  WHERE m2.repo_id = {recommended}\n"
                f"  AND split_part(m2.mr_id, ':', 2) = split_part(scm.mrs.mr_id, ':', 2)\n"
                f");"
            )
            
            # 删除重复的 repo
            sql_statements.append(
                f"-- 删除重复 repo（仅当所有引用都已迁移后执行）\n"
                f"-- DELETE FROM scm.repos WHERE repo_id = {dup_repo_id};"
            )
    
    return sql_statements


def run_fix(
    conn: psycopg.Connection,
    check_result: IntegrityCheckResult,
    auto_fix_source_id: bool = True,
) -> FixResult:
    """
    执行修复操作

    在事务内执行：
    1. 修复 source_id 问题（auto_fix_source_id=True 时）
    2. 生成 MR 去重 SQL（不自动执行，因为可能影响数据完整性）
    3. 生成 Repo 合并 SQL（不自动执行，因为影响范围大）

    Args:
        conn: 数据库连接
        check_result: 检查结果
        auto_fix_source_id: 是否自动修复 source_id

    Returns:
        修复结果
    """
    result = FixResult()
    
    if auto_fix_source_id and check_result.source_id_issues:
        logger.info(f"修复 {len(check_result.source_id_issues)} 个 source_id 问题...")
        fixed, skipped = fix_source_ids(conn, check_result.source_id_issues)
        result.source_id_fixed = fixed
        result.source_id_skipped = skipped
    
    # 生成 MR 去重 SQL
    if check_result.duplicate_mr_issues:
        logger.info(f"生成 {len(check_result.duplicate_mr_issues)} 个 MR 去重 SQL...")
        result.repo_merge_sql.extend(generate_mr_dedup_sql(check_result.duplicate_mr_issues))
    
    # 生成 Repo 合并 SQL
    if check_result.duplicate_repo_url_issues:
        logger.info(f"生成 {len(check_result.duplicate_repo_url_issues)} 个 Repo 合并 SQL...")
        result.repo_merge_sql.extend(generate_repo_merge_sql(check_result.duplicate_repo_url_issues))
    
    return result


# ============ 报告输出 ============


def print_report(check_result: IntegrityCheckResult, fix_result: Optional[FixResult] = None):
    """
    打印检查报告
    """
    print("\n" + "=" * 60)
    print("SCM 数据完整性检查报告")
    print("=" * 60)
    
    # source_id 问题
    print(f"\n【source_id 问题】共 {len(check_result.source_id_issues)} 个")
    if check_result.source_id_issues:
        # 按表分组统计
        by_table: Dict[str, Dict[str, int]] = {}
        for issue in check_result.source_id_issues:
            if issue.table not in by_table:
                by_table[issue.table] = {}
            if issue.issue_type not in by_table[issue.table]:
                by_table[issue.table][issue.issue_type] = 0
            by_table[issue.table][issue.issue_type] += 1
        
        for table, counts in by_table.items():
            print(f"  {table}:")
            for issue_type, count in counts.items():
                print(f"    - {issue_type}: {count}")
    
    # MR 重复问题
    print(f"\n【MR 重复问题】共 {len(check_result.duplicate_mr_issues)} 个")
    for issue in check_result.duplicate_mr_issues[:10]:  # 仅显示前 10 个
        print(f"  repo_id={issue.repo_id}, iid={issue.iid}")
        print(f"    所有 mr_id: {issue.mr_ids}")
        print(f"    推荐保留: {issue.recommended_mr_id}")
    if len(check_result.duplicate_mr_issues) > 10:
        print(f"  ... 还有 {len(check_result.duplicate_mr_issues) - 10} 个")
    
    # Repo URL 重复问题
    print(f"\n【Repo URL 重复问题】共 {len(check_result.duplicate_repo_url_issues)} 个")
    for issue in check_result.duplicate_repo_url_issues[:10]:  # 仅显示前 10 个
        print(f"  project_key: {issue.project_key}")
        print(f"    URL 变体: {issue.urls}")
        print(f"    规范化 URL: {issue.normalized_url}")
        print(f"    推荐保留 repo_id: {issue.recommended_repo_id}")
    if len(check_result.duplicate_repo_url_issues) > 10:
        print(f"  ... 还有 {len(check_result.duplicate_repo_url_issues) - 10} 个")
    
    # patch_blobs 问题
    print(f"\n【patch_blobs 问题】共 {len(check_result.patch_blob_issues)} 个")
    if check_result.patch_blob_issues:
        # 按问题类型分组统计
        by_type: Dict[str, int] = {}
        for issue in check_result.patch_blob_issues:
            by_type[issue.issue_type] = by_type.get(issue.issue_type, 0) + 1
        
        for issue_type, count in sorted(by_type.items()):
            print(f"  - {issue_type}: {count}")
        
        # 显示前几个详细问题
        print(f"\n  详细问题（前 10 个）:")
        for issue in check_result.patch_blob_issues[:10]:
            print(f"    blob_id={issue.blob_id}, type={issue.issue_type}")
            if issue.details:
                print(f"      {issue.details}")
        if len(check_result.patch_blob_issues) > 10:
            print(f"    ... 还有 {len(check_result.patch_blob_issues) - 10} 个")
    
    # 修复结果
    if fix_result:
        print("\n" + "-" * 60)
        print("修复结果")
        print("-" * 60)
        print(f"  source_id 已修复: {fix_result.source_id_fixed}")
        print(f"  source_id 已跳过: {fix_result.source_id_skipped}")
        
        if fix_result.repo_merge_sql:
            print(f"\n  生成的修复 SQL（{len(fix_result.repo_merge_sql)} 条）：")
            print("  " + "-" * 40)
            for sql in fix_result.repo_merge_sql[:5]:  # 仅显示前 5 条
                print(f"  {sql[:100]}..." if len(sql) > 100 else f"  {sql}")
            if len(fix_result.repo_merge_sql) > 5:
                print(f"  ... 还有 {len(fix_result.repo_merge_sql) - 5} 条")
    
    print("\n" + "=" * 60)
    if check_result.has_issues:
        print(f"总计发现 {check_result.issue_count} 个问题")
    else:
        print("未发现问题，数据完整性良好")
    print("=" * 60 + "\n")


# ============ CLI 部分 ============


def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="SCM 数据完整性检查工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # 仅检查，输出问题报告
    python scm_integrity_check.py

    # 检查并修复（在事务内执行）
    python scm_integrity_check.py --fix

    # 输出详细信息
    python scm_integrity_check.py -v

    # 输出 JSON 格式
    python scm_integrity_check.py --json

    # 将修复 SQL 输出到文件
    python scm_integrity_check.py --fix --sql-output fix.sql

    # 检查制品文件是否存在
    python scm_integrity_check.py --check-artifacts

    # 验证 sha256 哈希值（限制 100 条）
    python scm_integrity_check.py --check-artifacts --verify-sha256 --limit 100

    # 全量 sha256 验证（可能较慢）
    python scm_integrity_check.py --check-artifacts --verify-sha256
        """,
    )
    
    add_config_argument(parser)
    
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="显示详细输出",
    )
    
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出结果",
    )
    
    parser.add_argument(
        "--fix",
        action="store_true",
        help="执行修复操作（在事务内）",
    )
    
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="模拟执行，不实际提交更改",
    )
    
    parser.add_argument(
        "--sql-output",
        type=str,
        default=None,
        help="将生成的修复 SQL 输出到指定文件",
    )
    
    # patch_blobs 相关检查选项
    parser.add_argument(
        "--check-artifacts",
        action="store_true",
        help="检查 patch_blobs 引用的制品文件是否存在",
    )
    
    parser.add_argument(
        "--verify-sha256",
        action="store_true",
        help="验证 patch_blobs 的 sha256 与实际文件内容是否匹配（需配合 --check-artifacts）",
    )
    
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="限制 sha256 验证的记录数量（默认不限制，全量可能较慢）",
    )
    
    # Step3 索引一致性检查选项
    parser.add_argument(
        "--check-index",
        action="store_true",
        help="检查 Step3 索引一致性（chunking_version、制品完整性）",
    )
    
    parser.add_argument(
        "--chunking-version",
        type=str,
        default=None,
        help="要检查的分块版本号（--check-index 时使用，默认使用当前版本）",
    )
    
    parser.add_argument(
        "--project-key",
        type=str,
        default=None,
        help="按项目标识筛选（--check-index 时使用）",
    )
    
    parser.add_argument(
        "--sample-ratio",
        type=float,
        default=None,
        help="抽样比例 (0.0-1.0)，用于 --check-index",
    )
    
    return parser.parse_args()


def run_index_consistency_check(
    conn: psycopg.Connection,
    chunking_version: Optional[str] = None,
    project_key: Optional[str] = None,
    sample_ratio: Optional[float] = None,
    limit: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """
    执行 Step3 索引一致性检查

    Args:
        conn: 数据库连接
        chunking_version: 分块版本号
        project_key: 项目标识
        sample_ratio: 抽样比例
        limit: 最大记录数

    Returns:
        检查结果字典，如果模块不可用返回 None
    """
    try:
        from step3_seekdb_rag_hybrid.seek_consistency_check import run_consistency_check
        from step3_seekdb_rag_hybrid.step3_chunking import CHUNKING_VERSION
        
        version = chunking_version or CHUNKING_VERSION
        logger.info(f"执行 Step3 索引一致性检查 (版本={version})")
        
        result = run_consistency_check(
            conn=conn,
            chunking_version=version,
            project_key=project_key,
            sample_ratio=sample_ratio,
            limit=limit,
            check_artifacts=True,
            verify_sha256=True,
        )
        
        return result.to_dict()
    except ImportError:
        logger.warning("Step3 模块不可用，跳过索引一致性检查")
        return None


def main() -> int:
    """主入口"""
    args = parse_args()
    
    # 设置日志级别
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    try:
        # 加载配置
        config = get_config(args.config_path)
        config.load()
        
        # 获取数据库连接
        conn = get_connection(config=config)
        
        try:
            # 执行检查
            logger.info("开始执行 SCM 数据完整性检查...")
            check_result = run_integrity_check(
                conn,
                check_artifacts=args.check_artifacts,
                verify_sha256=args.verify_sha256,
                verify_limit=args.limit,
            )
            
            fix_result = None
            index_check_result = None
            
            if args.fix and check_result.has_issues:
                logger.info("执行修复操作...")
                fix_result = run_fix(conn, check_result)
                
                if args.dry_run:
                    logger.info("模拟执行模式，回滚所有更改")
                    conn.rollback()
                else:
                    logger.info("提交更改")
                    conn.commit()
            
            # 执行 Step3 索引一致性检查
            if args.check_index:
                index_check_result = run_index_consistency_check(
                    conn=conn,
                    chunking_version=args.chunking_version,
                    project_key=args.project_key,
                    sample_ratio=args.sample_ratio,
                    limit=args.limit,
                )
            
            # 输出结果
            if args.json:
                output = {
                    "check_result": check_result.to_dict(),
                }
                if fix_result:
                    output["fix_result"] = fix_result.to_dict()
                if index_check_result:
                    output["index_check_result"] = index_check_result
                print(json.dumps(output, default=str, ensure_ascii=False, indent=2))
            else:
                print_report(check_result, fix_result)
                # 打印索引检查结果
                if index_check_result:
                    print("\n" + "=" * 60)
                    print("Step3 索引一致性检查结果")
                    print("=" * 60)
                    summary = index_check_result.get("summary", {})
                    print(f"  检查记录数: {summary.get('total_checked', 0)}")
                    print(f"  发现问题数: {summary.get('total_issues', 0)}")
                    issue_counts = summary.get("issue_counts", {})
                    if issue_counts:
                        print("  问题分类:")
                        for issue_type, count in issue_counts.items():
                            print(f"    - {issue_type}: {count}")
            
            # 输出修复 SQL 到文件
            if fix_result and fix_result.repo_merge_sql and args.sql_output:
                with open(args.sql_output, "w", encoding="utf-8") as f:
                    f.write("-- SCM 数据完整性修复 SQL\n")
                    f.write("-- 自动生成，请在执行前仔细检查\n\n")
                    f.write("BEGIN;\n\n")
                    for sql in fix_result.repo_merge_sql:
                        f.write(sql + "\n\n")
                    f.write("-- COMMIT; -- 确认无误后取消注释并执行\n")
                    f.write("ROLLBACK; -- 默认回滚，确认无误后删除此行\n")
                logger.info(f"修复 SQL 已写入: {args.sql_output}")
            
            # 确定返回码
            has_scm_issues = check_result.has_issues
            has_index_issues = (
                index_check_result and 
                index_check_result.get("summary", {}).get("has_issues", False)
            )
            
            return 0 if not (has_scm_issues or has_index_issues) else 1
            
        except psycopg.Error as e:
            conn.rollback()
            raise DatabaseError(
                f"数据库操作失败: {e}",
                {"error": str(e)},
            )
        finally:
            conn.close()
    
    except EngramError as e:
        if args.json:
            print(json.dumps(e.to_dict(), default=str, ensure_ascii=False))
        else:
            logger.error(f"{e.error_type}: {e.message}")
            if args.verbose and e.details:
                logger.error(f"详情: {e.details}")
        return e.exit_code
    
    except Exception as e:
        logger.exception(f"未预期的错误: {e}")
        if args.json:
            print(json.dumps({
                "error": True,
                "type": "UNEXPECTED_ERROR",
                "message": str(e),
            }, default=str, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
