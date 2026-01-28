#!/usr/bin/env python3
"""
step1_cli.py - Step1 统一 CLI 入口

使用 Typer 构建的 CLI 工具，提供 SCM 相关子命令：
    scm ensure-repo           确保仓库存在（upsert）
    scm sync-svn              同步 SVN 日志
    scm sync-gitlab-commits   同步 GitLab commits
    scm sync-gitlab-mrs       同步 GitLab merge requests
    scm sync-gitlab-reviews   同步 GitLab MR reviews

每个命令都会在 logbook.events 中记录开始/结束/错误事件。

使用示例:
    python step1_cli.py scm ensure-repo --repo-type git --repo-url https://gitlab.com/ns/proj --project-key my_project
    python step1_cli.py scm sync-svn --repo-url svn://example.com/repo --dry-run
    python step1_cli.py scm sync-gitlab-commits --project-id 123 --from 2024-01-01 --to 2024-12-31
"""

import json
import logging
import sys
import traceback
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional

import typer

from engram_step1.config import Config, get_config
from engram_step1.db import add_event, create_item, get_connection
from engram_step1.errors import EngramError

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ============ CLI 应用定义 ============

app = typer.Typer(
    name="step1",
    help="Step1 Logbook/SCM 统一 CLI 工具",
    no_args_is_help=True,
)

scm_app = typer.Typer(
    name="scm",
    help="SCM 相关操作（仓库、同步）",
    no_args_is_help=True,
)

artifacts_app = typer.Typer(
    name="artifacts",
    help="制品（Artifact）存储管理",
    no_args_is_help=True,
)

identity_app = typer.Typer(
    name="identity",
    help="身份管理操作（用户、账户同步）",
    no_args_is_help=True,
)

app.add_typer(scm_app, name="scm")
app.add_typer(artifacts_app, name="artifacts")
app.add_typer(identity_app, name="identity")


# ============ Enums ============

class RepoType(str, Enum):
    """仓库类型"""
    svn = "svn"
    git = "git"


# ============ 公共参数与工具函数 ============

def load_config(config_path: Optional[str] = None) -> Config:
    """加载配置"""
    config = get_config(config_path, reload=config_path is not None)
    config.load()
    return config


def output_json(data: Dict[str, Any], pretty: bool = False) -> None:
    """输出 JSON 格式结果"""
    indent = 2 if pretty else None
    print(json.dumps(data, ensure_ascii=False, indent=indent, default=str))


def make_ok_result(**kwargs) -> Dict[str, Any]:
    """构造成功结果 (ok: true)"""
    return {"ok": True, **kwargs}


def make_err_result(code: str, message: str, detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """构造错误结果 (ok: false)"""
    return {"ok": False, "code": code, "message": message, "detail": detail or {}}


def get_or_create_sync_item(
    sync_type: str,
    repo_url: Optional[str] = None,
    project_id: Optional[str] = None,
    config: Optional[Config] = None,
) -> int:
    """
    获取或创建同步任务的 logbook item

    对于同步操作，使用固定的 item_type 和 title 格式
    """
    item_type = f"scm.sync.{sync_type}"
    
    if repo_url:
        title = f"SCM Sync: {sync_type} - {repo_url}"
    elif project_id:
        title = f"SCM Sync: {sync_type} - project:{project_id}"
    else:
        title = f"SCM Sync: {sync_type}"
    
    scope_json = {
        "sync_type": sync_type,
        "repo_url": repo_url,
        "project_id": project_id,
    }
    
    item_id = create_item(
        item_type=item_type,
        title=title,
        scope_json=scope_json,
        status="running",
        config=config,
    )
    
    return item_id


def log_sync_start(
    item_id: int,
    sync_type: str,
    params: Dict[str, Any],
    config: Optional[Config] = None,
) -> int:
    """记录同步开始事件"""
    event_id = add_event(
        item_id=item_id,
        event_type=f"{sync_type}.start",
        payload_json={
            "started_at": datetime.utcnow().isoformat() + "Z",
            "params": params,
        },
        source="step1_cli",
        config=config,
    )
    return event_id


def log_sync_end(
    item_id: int,
    sync_type: str,
    result: Dict[str, Any],
    success: bool = True,
    config: Optional[Config] = None,
) -> int:
    """记录同步结束事件"""
    event_type = f"{sync_type}.complete" if success else f"{sync_type}.error"
    status_to = "completed" if success else "error"
    
    event_id = add_event(
        item_id=item_id,
        event_type=event_type,
        payload_json={
            "ended_at": datetime.utcnow().isoformat() + "Z",
            "result": result,
            "success": success,
        },
        status_from="running",
        status_to=status_to,
        source="step1_cli",
        config=config,
    )
    return event_id


def log_sync_error(
    item_id: int,
    sync_type: str,
    error: Exception,
    config: Optional[Config] = None,
) -> int:
    """记录同步错误事件"""
    error_info = {
        "ended_at": datetime.utcnow().isoformat() + "Z",
        "error_type": type(error).__name__,
        "error_message": str(error),
        "traceback": traceback.format_exc(),
    }
    
    if isinstance(error, EngramError):
        error_info["error_details"] = error.to_dict()
    
    event_id = add_event(
        item_id=item_id,
        event_type=f"{sync_type}.error",
        payload_json=error_info,
        status_from="running",
        status_to="error",
        source="step1_cli",
        config=config,
    )
    return event_id


# ============ SCM 子命令 ============


@scm_app.command("ensure-repo")
def scm_ensure_repo(
    repo_type: RepoType = typer.Option(
        ..., "--repo-type", "-t",
        help="仓库类型 (svn/git)",
    ),
    repo_url: str = typer.Option(
        ..., "--repo-url", "-u",
        help="仓库 URL",
    ),
    project_key: str = typer.Option(
        ..., "--project-key", "-k",
        help="项目标识",
    ),
    default_branch: Optional[str] = typer.Option(
        None, "--default-branch", "-b",
        help="默认分支（仅 git）",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-n",
        help="只检查，不实际创建/更新",
    ),
    config_path: Optional[str] = typer.Option(
        None, "--config", "-c",
        help="配置文件路径",
    ),
    pretty: bool = typer.Option(
        False, "--pretty", "-p",
        help="美化 JSON 输出",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v",
        help="详细输出",
    ),
):
    """
    确保仓库存在（upsert）

    根据 (repo_type, url) 进行 upsert，返回 repo_id。
    """
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    try:
        config = load_config(config_path)
        
        # 导入模块
        from scm_repo import ensure_repo, get_repo_by_url
        
        if dry_run:
            # Dry-run 模式：只检查是否存在
            existing = get_repo_by_url(repo_type.value, repo_url, config=config)
            if existing:
                output_json(make_ok_result(
                    dry_run=True,
                    action="would_update",
                    repo_id=existing["repo_id"],
                    repo_type=repo_type.value,
                    url=repo_url,
                    project_key=project_key,
                    existing=existing,
                ), pretty=pretty)
            else:
                output_json(make_ok_result(
                    dry_run=True,
                    action="would_create",
                    repo_type=repo_type.value,
                    url=repo_url,
                    project_key=project_key,
                ), pretty=pretty)
            raise typer.Exit(0)
        
        # 创建 logbook item 和开始事件
        item_id = get_or_create_sync_item(
            sync_type="ensure_repo",
            repo_url=repo_url,
            config=config,
        )
        
        params = {
            "repo_type": repo_type.value,
            "url": repo_url,
            "project_key": project_key,
            "default_branch": default_branch,
        }
        log_sync_start(item_id, "ensure_repo", params, config=config)
        
        try:
            # 先检查是否已存在
            existing = get_repo_by_url(repo_type.value, repo_url, config=config)
            was_created = existing is None
            
            repo_id = ensure_repo(
                repo_type=repo_type.value,
                url=repo_url,
                project_key=project_key,
                default_branch=default_branch,
                config=config,
            )
            
            result = {
                "repo_id": repo_id,
                "created": was_created,
                "repo_type": repo_type.value,
                "url": repo_url,
                "project_key": project_key,
            }
            
            log_sync_end(item_id, "ensure_repo", result, success=True, config=config)
            
            output_json(make_ok_result(
                item_id=item_id,
                **result,
            ), pretty=pretty)
            
        except Exception as e:
            log_sync_error(item_id, "ensure_repo", e, config=config)
            raise
        
    except typer.Exit:
        raise
    except EngramError as e:
        output_json(make_err_result(
            code=e.error_type,
            message=e.message,
            detail=e.details,
        ), pretty=pretty)
        raise typer.Exit(e.exit_code)
    except Exception as e:
        output_json(make_err_result(
            code="UNEXPECTED_ERROR",
            message=str(e),
            detail={"error_type": type(e).__name__},
        ), pretty=pretty)
        raise typer.Exit(1)


@scm_app.command("sync-svn")
def scm_sync_svn(
    repo_url: Optional[str] = typer.Option(
        None, "--repo-url", "-u",
        help="SVN 仓库 URL（覆盖配置）",
    ),
    repo_id: Optional[int] = typer.Option(
        None, "--repo-id", "-r",
        help="仓库 ID（与 --repo-url 二选一）",
    ),
    project_id: Optional[str] = typer.Option(
        None, "--project-id",
        help="项目标识（用于创建仓库记录）",
    ),
    from_rev: Optional[int] = typer.Option(
        None, "--from",
        help="起始 revision（覆盖游标）",
    ),
    to_rev: Optional[int] = typer.Option(
        None, "--to",
        help="结束 revision（默认 HEAD）",
    ),
    batch_size: Optional[int] = typer.Option(
        None, "--batch-size", "-b",
        help="每次同步的最大 revision 数",
    ),
    overlap: Optional[int] = typer.Option(
        None, "--overlap",
        help="重叠 revision 数",
    ),
    fetch_patches: bool = typer.Option(
        False, "--fetch-patches",
        help="同步 patch（获取 diff 内容）",
    ),
    loop: bool = typer.Option(
        False, "--loop",
        help="循环同步直到全部完成",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-n",
        help="只检查，不实际同步",
    ),
    config_path: Optional[str] = typer.Option(
        None, "--config", "-c",
        help="配置文件路径",
    ),
    pretty: bool = typer.Option(
        False, "--pretty", "-p",
        help="美化 JSON 输出",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v",
        help="详细输出",
    ),
):
    """
    同步 SVN 日志

    从 SVN 仓库拉取日志并写入 scm.svn_revisions。
    """
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    try:
        config = load_config(config_path)
        
        # 获取 SVN URL
        svn_url = repo_url or config.get("svn.url")
        if not svn_url:
            output_json(make_err_result(
                code="VALIDATION_ERROR",
                message="缺少 SVN URL，请使用 --repo-url 或在配置文件中设置 svn.url",
            ), pretty=pretty)
            raise typer.Exit(6)
        
        project_key = project_id or config.get("project.project_key", "default")
        
        # 导入模块
        from scm_sync_svn import SyncConfig, sync_svn_revisions, get_svn_head_revision, get_last_synced_revision, get_or_create_repo
        
        if dry_run:
            # Dry-run 模式：检查同步状态
            conn = get_connection(config=config)
            try:
                rid = get_or_create_repo(conn, svn_url, project_key)
                last_synced = get_last_synced_revision(rid, config)
                head_rev = get_svn_head_revision(svn_url)
                
                output_json(make_ok_result(
                    dry_run=True,
                    repo_id=rid,
                    svn_url=svn_url,
                    last_synced_revision=last_synced,
                    head_revision=head_rev,
                    pending_revisions=max(0, head_rev - last_synced),
                    from_rev=from_rev,
                    to_rev=to_rev,
                ), pretty=pretty)
            finally:
                conn.close()
            raise typer.Exit(0)
        
        # 创建 logbook item 和开始事件
        item_id = get_or_create_sync_item(
            sync_type="svn",
            repo_url=svn_url,
            config=config,
        )
        
        params = {
            "svn_url": svn_url,
            "project_key": project_key,
            "from_rev": from_rev,
            "to_rev": to_rev,
            "batch_size": batch_size,
            "overlap": overlap,
            "fetch_patches": fetch_patches,
            "loop": loop,
        }
        log_sync_start(item_id, "sync_svn", params, config=config)
        
        try:
            # 构建同步配置
            sync_config = SyncConfig(
                svn_url=svn_url,
                batch_size=batch_size or config.get("svn.batch_size", 100),
                overlap=overlap if overlap is not None else config.get("svn.overlap", 0),
            )
            
            # 执行同步
            total_synced = 0
            loop_count = 0
            max_loops = 1000
            all_results = []
            
            while True:
                loop_count += 1
                if loop_count > max_loops:
                    logger.warning(f"达到最大循环次数限制 ({max_loops})，退出")
                    break
                
                result = sync_svn_revisions(
                    sync_config,
                    project_key,
                    config,
                    verbose=verbose,
                    fetch_patches=fetch_patches,
                )
                total_synced += result.get("synced_count", 0)
                all_results.append(result)
                
                if not loop or not result.get("has_more", False):
                    break
                
                logger.info(f"继续同步下一批（第 {loop_count + 1} 轮）...")
            
            # 提取最后一批结果的关键统计字段
            last_result = all_results[-1] if all_results else {}
            final_result = {
                "repo_id": last_result.get("repo_id"),
                "synced_count": total_synced,
                "start_rev": all_results[0].get("start_rev") if all_results else None,
                "end_rev": last_result.get("end_rev"),
                "last_rev": last_result.get("last_rev"),
                "has_more": last_result.get("has_more", False),
                "remaining": last_result.get("remaining"),
                "bulk_count": sum(r.get("bulk_count", 0) for r in all_results),
                "loop_count": loop_count,
            }
            
            log_sync_end(item_id, "sync_svn", final_result, success=True, config=config)
            
            output_json(make_ok_result(
                item_id=item_id,
                **final_result,
            ), pretty=pretty)
            
        except Exception as e:
            log_sync_error(item_id, "sync_svn", e, config=config)
            raise
        
    except typer.Exit:
        raise
    except EngramError as e:
        output_json(make_err_result(
            code=e.error_type,
            message=e.message,
            detail=e.details,
        ), pretty=pretty)
        raise typer.Exit(e.exit_code)
    except Exception as e:
        output_json(make_err_result(
            code="UNEXPECTED_ERROR",
            message=str(e),
            detail={"error_type": type(e).__name__},
        ), pretty=pretty)
        raise typer.Exit(1)


@scm_app.command("sync-gitlab-commits")
def scm_sync_gitlab_commits(
    repo_url: Optional[str] = typer.Option(
        None, "--repo-url", "-u",
        help="GitLab 实例 URL（覆盖配置）",
    ),
    repo_id: Optional[int] = typer.Option(
        None, "--repo-id", "-r",
        help="仓库 ID",
    ),
    project_id: Optional[str] = typer.Option(
        None, "--project-id",
        help="GitLab 项目 ID 或路径",
    ),
    token: Optional[str] = typer.Option(
        None, "--token",
        help="GitLab Private Token",
    ),
    ref_name: Optional[str] = typer.Option(
        None, "--ref-name",
        help="分支/tag 名称",
    ),
    from_date: Optional[str] = typer.Option(
        None, "--from",
        help="起始时间 (ISO 8601 格式)",
    ),
    to_date: Optional[str] = typer.Option(
        None, "--to",
        help="结束时间 (ISO 8601 格式)",
    ),
    batch_size: Optional[int] = typer.Option(
        None, "--batch-size", "-b",
        help="每次同步的最大 commit 数",
    ),
    no_diff: bool = typer.Option(
        False, "--no-diff",
        help="不获取 diff 内容",
    ),
    loop: bool = typer.Option(
        False, "--loop",
        help="循环同步直到全部完成",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-n",
        help="只检查，不实际同步",
    ),
    config_path: Optional[str] = typer.Option(
        None, "--config", "-c",
        help="配置文件路径",
    ),
    pretty: bool = typer.Option(
        False, "--pretty", "-p",
        help="美化 JSON 输出",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v",
        help="详细输出",
    ),
):
    """
    同步 GitLab Commits

    从 GitLab 拉取 commits 并写入 scm.git_commits。
    """
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    try:
        config = load_config(config_path)
        
        # 获取配置
        gitlab_url = repo_url or config.get("gitlab.url")
        gitlab_project_id = project_id or config.get("gitlab.project_id")
        private_token = token or config.get("gitlab.private_token")
        
        if not gitlab_url:
            output_json(make_err_result(
                code="VALIDATION_ERROR",
                message="缺少 GitLab URL，请使用 --repo-url 或在配置文件中设置 gitlab.url",
            ), pretty=pretty)
            raise typer.Exit(6)
        
        if not gitlab_project_id:
            output_json(make_err_result(
                code="VALIDATION_ERROR",
                message="缺少 GitLab 项目 ID，请使用 --project-id 或在配置文件中设置 gitlab.project_id",
            ), pretty=pretty)
            raise typer.Exit(6)
        
        if not private_token:
            output_json(make_err_result(
                code="VALIDATION_ERROR",
                message="缺少 GitLab Private Token，请使用 --token 或在配置文件中设置 gitlab.private_token",
            ), pretty=pretty)
            raise typer.Exit(6)
        
        project_key = config.get("project.project_key", "default")
        
        # 导入模块
        from scm_sync_gitlab_commits import SyncConfig, sync_gitlab_commits, get_last_sync_cursor, get_or_create_repo
        
        if dry_run:
            # Dry-run 模式：检查同步状态
            conn = get_connection(config=config)
            try:
                rid = get_or_create_repo(
                    conn, gitlab_url, str(gitlab_project_id), project_key, ref_name
                )
                cursor = get_last_sync_cursor(rid, config)
                
                output_json(make_ok_result(
                    dry_run=True,
                    repo_id=rid,
                    gitlab_url=gitlab_url,
                    project_id=gitlab_project_id,
                    last_sync_cursor=cursor,
                    from_date=from_date,
                    to_date=to_date,
                ), pretty=pretty)
            finally:
                conn.close()
            raise typer.Exit(0)
        
        # 创建 logbook item 和开始事件
        item_id = get_or_create_sync_item(
            sync_type="gitlab_commits",
            repo_url=gitlab_url,
            project_id=str(gitlab_project_id),
            config=config,
        )
        
        params = {
            "gitlab_url": gitlab_url,
            "project_id": gitlab_project_id,
            "ref_name": ref_name,
            "from_date": from_date,
            "to_date": to_date,
            "batch_size": batch_size,
            "no_diff": no_diff,
            "loop": loop,
        }
        log_sync_start(item_id, "sync_gitlab_commits", params, config=config)
        
        try:
            # 构建同步配置
            sync_config = SyncConfig(
                gitlab_url=gitlab_url,
                project_id=str(gitlab_project_id),
                private_token=private_token,
                batch_size=batch_size or config.get("gitlab.batch_size", 100),
                ref_name=ref_name or config.get("gitlab.ref_name"),
                request_timeout=config.get("gitlab.request_timeout", 60),
            )
            
            # 执行同步
            total_synced = 0
            total_diffs = 0
            loop_count = 0
            max_loops = 1000
            all_results = []
            
            while True:
                loop_count += 1
                if loop_count > max_loops:
                    logger.warning(f"达到最大循环次数限制 ({max_loops})，退出")
                    break
                
                result = sync_gitlab_commits(
                    sync_config,
                    project_key,
                    config,
                    verbose=verbose,
                    fetch_diffs=not no_diff,
                )
                total_synced += result.get("synced_count", 0)
                total_diffs += result.get("diff_count", 0)
                all_results.append(result)
                
                if not loop or not result.get("has_more", False):
                    break
                
                logger.info(f"继续同步下一批（第 {loop_count + 1} 轮）...")
            
            # 提取最后一批结果的关键统计字段
            last_result = all_results[-1] if all_results else {}
            final_result = {
                "repo_id": last_result.get("repo_id"),
                "synced_count": total_synced,
                "diff_count": total_diffs,
                "since": all_results[0].get("since") if all_results else None,
                "last_commit_sha": last_result.get("last_commit_sha"),
                "last_commit_ts": last_result.get("last_commit_ts"),
                "has_more": last_result.get("has_more", False),
                "bulk_count": sum(r.get("bulk_count", 0) for r in all_results),
                "loop_count": loop_count,
            }
            
            log_sync_end(item_id, "sync_gitlab_commits", final_result, success=True, config=config)
            
            output_json(make_ok_result(
                item_id=item_id,
                **final_result,
            ), pretty=pretty)
            
        except Exception as e:
            log_sync_error(item_id, "sync_gitlab_commits", e, config=config)
            raise
        
    except typer.Exit:
        raise
    except EngramError as e:
        output_json(make_err_result(
            code=e.error_type,
            message=e.message,
            detail=e.details,
        ), pretty=pretty)
        raise typer.Exit(e.exit_code)
    except Exception as e:
        output_json(make_err_result(
            code="UNEXPECTED_ERROR",
            message=str(e),
            detail={"error_type": type(e).__name__},
        ), pretty=pretty)
        raise typer.Exit(1)


@scm_app.command("sync-gitlab-mrs")
def scm_sync_gitlab_mrs(
    repo_url: Optional[str] = typer.Option(
        None, "--repo-url", "-u",
        help="GitLab 实例 URL（覆盖配置）",
    ),
    repo_id: Optional[int] = typer.Option(
        None, "--repo-id", "-r",
        help="仓库 ID",
    ),
    project_id: Optional[str] = typer.Option(
        None, "--project-id",
        help="GitLab 项目 ID 或路径",
    ),
    token: Optional[str] = typer.Option(
        None, "--token",
        help="GitLab Private Token",
    ),
    state: Optional[str] = typer.Option(
        None, "--state",
        help="MR 状态过滤 (all/opened/closed/merged)",
    ),
    from_date: Optional[str] = typer.Option(
        None, "--from",
        help="起始更新时间 (ISO 8601 格式)",
    ),
    to_date: Optional[str] = typer.Option(
        None, "--to",
        help="结束更新时间 (ISO 8601 格式)",
    ),
    batch_size: Optional[int] = typer.Option(
        None, "--batch-size", "-b",
        help="每次同步的最大 MR 数",
    ),
    fetch_details: bool = typer.Option(
        False, "--fetch-details",
        help="获取每个 MR 的详细信息",
    ),
    loop: bool = typer.Option(
        False, "--loop",
        help="循环同步直到全部完成",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-n",
        help="只检查，不实际同步",
    ),
    config_path: Optional[str] = typer.Option(
        None, "--config", "-c",
        help="配置文件路径",
    ),
    pretty: bool = typer.Option(
        False, "--pretty", "-p",
        help="美化 JSON 输出",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v",
        help="详细输出",
    ),
):
    """
    同步 GitLab Merge Requests

    从 GitLab 拉取 MRs 并写入 scm.mrs。
    """
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    try:
        config = load_config(config_path)
        
        # 获取配置
        gitlab_url = repo_url or config.get("gitlab.url")
        gitlab_project_id = project_id or config.get("gitlab.project_id")
        private_token = token or config.get("gitlab.private_token")
        
        if not gitlab_url:
            output_json({
                "success": False,
                "error": "缺少 GitLab URL，请使用 --repo-url 或在配置文件中设置 gitlab.url",
            }, pretty=pretty)
            raise typer.Exit(1)
        
        if not gitlab_project_id:
            output_json({
                "success": False,
                "error": "缺少 GitLab 项目 ID，请使用 --project-id 或在配置文件中设置 gitlab.project_id",
            }, pretty=pretty)
            raise typer.Exit(1)
        
        if not private_token:
            output_json({
                "success": False,
                "error": "缺少 GitLab Private Token，请使用 --token 或在配置文件中设置 gitlab.private_token",
            }, pretty=pretty)
            raise typer.Exit(1)
        
        project_key = config.get("project.project_key", "default")
        
        # 导入模块
        from scm_sync_gitlab_mrs import SyncConfig, sync_gitlab_mrs, get_last_sync_cursor, get_or_create_repo
        
        if dry_run:
            # Dry-run 模式：检查同步状态
            conn = get_connection(config=config)
            try:
                rid = get_or_create_repo(
                    conn, gitlab_url, str(gitlab_project_id), project_key
                )
                cursor = get_last_sync_cursor(rid, config)
                
                output_json(make_ok_result(
                    dry_run=True,
                    repo_id=rid,
                    gitlab_url=gitlab_url,
                    project_id=gitlab_project_id,
                    last_sync_cursor=cursor,
                    state_filter=state,
                    from_date=from_date,
                    to_date=to_date,
                ), pretty=pretty)
            finally:
                conn.close()
            raise typer.Exit(0)
        
        # 创建 logbook item 和开始事件
        item_id = get_or_create_sync_item(
            sync_type="gitlab_mrs",
            repo_url=gitlab_url,
            project_id=str(gitlab_project_id),
            config=config,
        )
        
        params = {
            "gitlab_url": gitlab_url,
            "project_id": gitlab_project_id,
            "state": state,
            "from_date": from_date,
            "to_date": to_date,
            "batch_size": batch_size,
            "fetch_details": fetch_details,
            "loop": loop,
        }
        log_sync_start(item_id, "sync_gitlab_mrs", params, config=config)
        
        try:
            # 构建同步配置
            sync_config = SyncConfig(
                gitlab_url=gitlab_url,
                project_id=str(gitlab_project_id),
                private_token=private_token,
                batch_size=batch_size or config.get("gitlab.batch_size", 100),
                request_timeout=config.get("gitlab.request_timeout", 60),
                state_filter=state or config.get("gitlab.mr_state_filter"),
            )
            
            # 执行同步
            total_synced = 0
            loop_count = 0
            max_loops = 1000
            all_results = []
            
            while True:
                loop_count += 1
                if loop_count > max_loops:
                    logger.warning(f"达到最大循环次数限制 ({max_loops})，退出")
                    break
                
                result = sync_gitlab_mrs(
                    sync_config,
                    project_key,
                    config,
                    verbose=verbose,
                    fetch_details=fetch_details,
                )
                total_synced += result.get("synced_count", 0)
                all_results.append(result)
                
                if not loop or not result.get("has_more", False):
                    break
                
                logger.info(f"继续同步下一批（第 {loop_count + 1} 轮）...")
            
            # 提取最后一批结果的关键统计字段
            last_result = all_results[-1] if all_results else {}
            # 累计 inserted/updated/skipped
            total_inserted = sum(r.get("inserted", 0) for r in all_results)
            total_updated = sum(r.get("updated", 0) for r in all_results)
            total_skipped = sum(r.get("skipped", 0) for r in all_results)
            
            final_result = {
                "repo_id": last_result.get("repo_id"),
                "inserted": total_inserted,
                "updated": total_updated,
                "skipped": total_skipped,
                "has_more": last_result.get("has_more", False),
                "loop_count": loop_count,
            }
            
            log_sync_end(item_id, "sync_gitlab_mrs", final_result, success=True, config=config)
            
            output_json(make_ok_result(
                item_id=item_id,
                **final_result,
            ), pretty=pretty)
            
        except Exception as e:
            log_sync_error(item_id, "sync_gitlab_mrs", e, config=config)
            raise
        
    except typer.Exit:
        raise
    except EngramError as e:
        output_json(make_err_result(
            code=e.error_type,
            message=e.message,
            detail=e.details,
        ), pretty=pretty)
        raise typer.Exit(e.exit_code)
    except Exception as e:
        output_json(make_err_result(
            code="UNEXPECTED_ERROR",
            message=str(e),
            detail={"error_type": type(e).__name__},
        ), pretty=pretty)
        raise typer.Exit(1)


@scm_app.command("refresh-vfacts")
def scm_refresh_vfacts(
    concurrently: bool = typer.Option(
        False, "--concurrently", "-C",
        help="使用 CONCURRENTLY 刷新（需要 v_facts 上存在唯一索引）",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-n",
        help="只检查，不实际刷新",
    ),
    config_path: Optional[str] = typer.Option(
        None, "--config", "-c",
        help="配置文件路径",
    ),
    pretty: bool = typer.Option(
        False, "--pretty", "-p",
        help="美化 JSON 输出",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v",
        help="详细输出",
    ),
):
    """
    刷新 scm.v_facts 物化视图

    可选使用 CONCURRENTLY 模式避免阻塞读取（需要唯一索引已存在）。
    """
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    try:
        config = load_config(config_path)
        conn = get_connection(config=config)
        
        try:
            with conn.cursor() as cur:
                # 获取刷新前的状态
                cur.execute("""
                    SELECT 
                        COUNT(*) as row_count,
                        (SELECT reltuples::bigint FROM pg_class 
                         WHERE relnamespace = (SELECT oid FROM pg_namespace WHERE nspname = 'scm')
                           AND relname = 'v_facts') as estimated_rows
                    FROM scm.v_facts
                """)
                before_row = cur.fetchone()
                before_count = before_row[0] if before_row else 0
                
                # 获取最后刷新时间（通过 pg_stat_user_tables）
                cur.execute("""
                    SELECT last_vacuum, last_analyze
                    FROM pg_stat_user_tables
                    WHERE schemaname = 'scm' AND relname = 'v_facts'
                """)
                stat_row = cur.fetchone()
                
                if dry_run:
                    output_json(make_ok_result(
                        dry_run=True,
                        action="would_refresh",
                        concurrently=concurrently,
                        current_row_count=before_count,
                        last_vacuum=str(stat_row[0]) if stat_row and stat_row[0] else None,
                        last_analyze=str(stat_row[1]) if stat_row and stat_row[1] else None,
                    ), pretty=pretty)
                    raise typer.Exit(0)
                
                # 执行刷新
                start_time = datetime.utcnow()
                
                if concurrently:
                    # CONCURRENTLY 模式需要唯一索引
                    cur.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY scm.v_facts")
                else:
                    cur.execute("REFRESH MATERIALIZED VIEW scm.v_facts")
                
                conn.commit()
                
                end_time = datetime.utcnow()
                duration_ms = (end_time - start_time).total_seconds() * 1000
                
                # 获取刷新后的行数
                cur.execute("SELECT COUNT(*) FROM scm.v_facts")
                after_row = cur.fetchone()
                after_count = after_row[0] if after_row else 0
                
                output_json(make_ok_result(
                    refreshed=True,
                    concurrently=concurrently,
                    before_row_count=before_count,
                    after_row_count=after_count,
                    duration_ms=round(duration_ms, 2),
                    refreshed_at=end_time.isoformat() + "Z",
                ), pretty=pretty)
                
        finally:
            conn.close()
        
    except typer.Exit:
        raise
    except EngramError as e:
        output_json(make_err_result(
            code=e.error_type,
            message=e.message,
            detail=e.details,
        ), pretty=pretty)
        raise typer.Exit(e.exit_code)
    except Exception as e:
        output_json(make_err_result(
            code="REFRESH_ERROR",
            message=str(e),
            detail={"error_type": type(e).__name__},
        ), pretty=pretty)
        raise typer.Exit(1)


@scm_app.command("sync-gitlab-reviews")
def scm_sync_gitlab_reviews(
    repo_url: Optional[str] = typer.Option(
        None, "--repo-url", "-u",
        help="GitLab 实例 URL（覆盖配置）",
    ),
    repo_id: Optional[int] = typer.Option(
        None, "--repo-id", "-r",
        help="仓库 ID",
    ),
    project_id: Optional[str] = typer.Option(
        None, "--project-id",
        help="GitLab 项目 ID 或路径",
    ),
    token: Optional[str] = typer.Option(
        None, "--token",
        help="GitLab Private Token",
    ),
    from_date: Optional[str] = typer.Option(
        None, "--from",
        help="起始更新时间 (ISO 8601 格式)",
    ),
    to_date: Optional[str] = typer.Option(
        None, "--to",
        help="结束更新时间 (ISO 8601 格式)",
    ),
    batch_size: Optional[int] = typer.Option(
        None, "--batch-size", "-b",
        help="每次同步的最大 MR 数",
    ),
    loop: bool = typer.Option(
        False, "--loop",
        help="循环同步直到全部完成",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-n",
        help="只检查，不实际同步",
    ),
    config_path: Optional[str] = typer.Option(
        None, "--config", "-c",
        help="配置文件路径",
    ),
    pretty: bool = typer.Option(
        False, "--pretty", "-p",
        help="美化 JSON 输出",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v",
        help="详细输出",
    ),
):
    """
    同步 GitLab MR Reviews

    从 GitLab 拉取 MR 的 discussions/notes/approvals 并写入 scm.review_events。
    """
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    try:
        config = load_config(config_path)
        
        # 获取配置
        gitlab_url = repo_url or config.get("gitlab.url")
        gitlab_project_id = project_id or config.get("gitlab.project_id")
        private_token = token or config.get("gitlab.private_token")
        
        if not gitlab_url:
            output_json({
                "success": False,
                "error": "缺少 GitLab URL，请使用 --repo-url 或在配置文件中设置 gitlab.url",
            }, pretty=pretty)
            raise typer.Exit(1)
        
        if not gitlab_project_id:
            output_json({
                "success": False,
                "error": "缺少 GitLab 项目 ID，请使用 --project-id 或在配置文件中设置 gitlab.project_id",
            }, pretty=pretty)
            raise typer.Exit(1)
        
        if not private_token:
            output_json({
                "success": False,
                "error": "缺少 GitLab Private Token，请使用 --token 或在配置文件中设置 gitlab.private_token",
            }, pretty=pretty)
            raise typer.Exit(1)
        
        project_key = config.get("project.project_key", "default")
        
        # 导入模块
        from scm_sync_gitlab_reviews import SyncConfig, sync_gitlab_reviews, get_last_sync_cursor, get_or_create_repo
        
        if dry_run:
            # Dry-run 模式：检查同步状态
            conn = get_connection(config=config)
            try:
                rid = get_or_create_repo(
                    conn, gitlab_url, str(gitlab_project_id), project_key
                )
                cursor = get_last_sync_cursor(rid, config)
                
                output_json(make_ok_result(
                    dry_run=True,
                    repo_id=rid,
                    gitlab_url=gitlab_url,
                    project_id=gitlab_project_id,
                    last_sync_cursor=cursor,
                    from_date=from_date,
                    to_date=to_date,
                ), pretty=pretty)
            finally:
                conn.close()
            raise typer.Exit(0)
        
        # 创建 logbook item 和开始事件
        item_id = get_or_create_sync_item(
            sync_type="gitlab_reviews",
            repo_url=gitlab_url,
            project_id=str(gitlab_project_id),
            config=config,
        )
        
        params = {
            "gitlab_url": gitlab_url,
            "project_id": gitlab_project_id,
            "from_date": from_date,
            "to_date": to_date,
            "batch_size": batch_size,
            "loop": loop,
        }
        log_sync_start(item_id, "sync_gitlab_reviews", params, config=config)
        
        try:
            # 构建同步配置
            sync_config = SyncConfig(
                gitlab_url=gitlab_url,
                project_id=str(gitlab_project_id),
                private_token=private_token,
                batch_size=batch_size or config.get("gitlab.batch_size", 50),
                request_timeout=config.get("gitlab.request_timeout", 60),
            )
            
            # 执行同步
            total_mr_synced = 0
            total_events_synced = 0
            loop_count = 0
            max_loops = 1000
            all_results = []
            
            while True:
                loop_count += 1
                if loop_count > max_loops:
                    logger.warning(f"达到最大循环次数限制 ({max_loops})，退出")
                    break
                
                result = sync_gitlab_reviews(
                    sync_config,
                    project_key,
                    config,
                    verbose=verbose,
                )
                total_mr_synced += result.get("synced_mr_count", 0)
                total_events_synced += result.get("synced_event_count", 0)
                all_results.append(result)
                
                if not loop or not result.get("has_more", False):
                    break
                
                logger.info(f"继续同步下一批（第 {loop_count + 1} 轮）...")
            
            # 提取最后一批结果的关键统计字段
            last_result = all_results[-1] if all_results else {}
            # 累计 inserted/skipped 并按类型汇总
            total_inserted = sum(r.get("inserted", 0) for r in all_results)
            total_skipped = sum(r.get("skipped", 0) for r in all_results)
            # 合并 by_type 统计
            by_type: Dict[str, int] = {}
            for r in all_results:
                for event_type, count in r.get("by_type", {}).items():
                    by_type[event_type] = by_type.get(event_type, 0) + count
            
            final_result = {
                "repo_id": last_result.get("repo_id"),
                "mr_id": last_result.get("mr_id"),
                "inserted": total_inserted,
                "skipped": total_skipped,
                "by_type": by_type,
                "has_more": last_result.get("has_more", False),
                "loop_count": loop_count,
            }
            
            log_sync_end(item_id, "sync_gitlab_reviews", final_result, success=True, config=config)
            
            output_json(make_ok_result(
                item_id=item_id,
                **final_result,
            ), pretty=pretty)
            
        except Exception as e:
            log_sync_error(item_id, "sync_gitlab_reviews", e, config=config)
            raise
        
    except typer.Exit:
        raise
    except EngramError as e:
        output_json(make_err_result(
            code=e.error_type,
            message=e.message,
            detail=e.details,
        ), pretty=pretty)
        raise typer.Exit(e.exit_code)
    except Exception as e:
        output_json(make_err_result(
            code="UNEXPECTED_ERROR",
            message=str(e),
            detail={"error_type": type(e).__name__},
        ), pretty=pretty)
        raise typer.Exit(1)


# ============ Artifacts 子命令 ============


@artifacts_app.command("write")
def artifacts_write(
    path: Optional[str] = typer.Option(
        None, "--path", "-p",
        help="逻辑路径（artifact key），如 scm/proj/1/svn/r100/abc.diff",
    ),
    uri: Optional[str] = typer.Option(
        None, "--uri", "-u",
        help="物理 URI（file://、s3:// 等），用于特殊场景",
    ),
    content: Optional[str] = typer.Option(
        None, "--content", "-c",
        help="要写入的内容（字符串）",
    ),
    input_file: Optional[str] = typer.Option(
        None, "--input", "-i",
        help="从文件读取内容",
    ),
    config_path: Optional[str] = typer.Option(
        None, "--config",
        help="配置文件路径",
    ),
    pretty: bool = typer.Option(
        False, "--pretty",
        help="美化 JSON 输出",
    ),
):
    """
    写入制品

    通过 --path 指定逻辑路径，或通过 --uri 指定物理 URI。
    内容可通过 --content 直接传入，或通过 --input 从文件读取。
    """
    # 验证参数
    if not path and not uri:
        output_json(make_err_result(
            code="INVALID_ARGS",
            message="必须指定 --path 或 --uri",
        ), pretty=pretty)
        raise typer.Exit(1)

    if not content and not input_file:
        output_json(make_err_result(
            code="INVALID_ARGS",
            message="必须指定 --content 或 --input",
        ), pretty=pretty)
        raise typer.Exit(1)

    try:
        from artifacts import write_text_artifact
        from engram_step1.artifact_store import (
            FileUriStore, get_default_store,
            PathTraversalError, ArtifactHashMismatchError, ArtifactOverwriteDeniedError,
            ArtifactWriteDisabledError,
        )
        from engram_step1.uri import is_physical_uri

        # 读取内容
        if input_file:
            with open(input_file, "rb") as f:
                data = f.read()
        else:
            data = content.encode("utf-8") if content else b""

        # 确定使用的路径/URI
        target = uri if uri else path

        # 根据 URI 类型选择 store
        if uri and is_physical_uri(uri):
            # 物理 URI，使用 FileUriStore
            store = FileUriStore()
            result = store.put(uri, data)
        else:
            # 逻辑路径，使用默认 store
            result = write_text_artifact(target, data)

        output_json(make_ok_result(**result), pretty=pretty)

    except PathTraversalError as e:
        output_json(make_err_result(
            code="PATH_TRAVERSAL",
            message=e.message,
            detail=e.details,
        ), pretty=pretty)
        raise typer.Exit(2)
    except ArtifactWriteDisabledError as e:
        output_json(make_err_result(
            code="WRITE_DISABLED",
            message=e.message,
            detail=e.details,
        ), pretty=pretty)
        raise typer.Exit(13)
    except ArtifactHashMismatchError as e:
        output_json(make_err_result(
            code="CHECKSUM_MISMATCH",
            message=e.message,
            detail=e.details,
        ), pretty=pretty)
        raise typer.Exit(12)
    except ArtifactOverwriteDeniedError as e:
        output_json(make_err_result(
            code="FILE_EXISTS",
            message=e.message,
            detail=e.details,
        ), pretty=pretty)
        raise typer.Exit(12)
    except Exception as e:
        output_json(make_err_result(
            code="WRITE_ERROR",
            message=str(e),
            detail={"error_type": type(e).__name__},
        ), pretty=pretty)
        raise typer.Exit(1)


@artifacts_app.command("read")
def artifacts_read(
    path: Optional[str] = typer.Option(
        None, "--path", "-p",
        help="逻辑路径（artifact key）",
    ),
    uri: Optional[str] = typer.Option(
        None, "--uri", "-u",
        help="物理 URI（file://、s3:// 等）",
    ),
    output_file: Optional[str] = typer.Option(
        None, "--output", "-o",
        help="输出到文件（不指定则输出到 stdout）",
    ),
    config_path: Optional[str] = typer.Option(
        None, "--config",
        help="配置文件路径",
    ),
    json_output: bool = typer.Option(
        False, "--json",
        help="以 JSON 格式输出元数据（不输出内容）",
    ),
    pretty: bool = typer.Option(
        False, "--pretty",
        help="美化 JSON 输出",
    ),
):
    """
    读取制品

    通过 --path 指定逻辑路径，或通过 --uri 指定物理 URI。
    """
    if not path and not uri:
        output_json(make_err_result(
            code="INVALID_ARGS",
            message="必须指定 --path 或 --uri",
        ), pretty=pretty)
        raise typer.Exit(1)

    try:
        from artifacts import read_artifact, get_artifact_info
        from engram_step1.artifact_store import (
            FileUriStore, PathTraversalError, ArtifactNotFoundError,
        )
        from engram_step1.uri import is_physical_uri

        target = uri if uri else path

        # 根据 URI 类型选择 store
        if uri and is_physical_uri(uri):
            store = FileUriStore()
            if json_output:
                info = store.get_info(uri)
                output_json(make_ok_result(**info), pretty=pretty)
            else:
                data = store.get(uri)
                if output_file:
                    with open(output_file, "wb") as f:
                        f.write(data)
                else:
                    sys.stdout.buffer.write(data)
        else:
            if json_output:
                info = get_artifact_info(target)
                output_json(make_ok_result(**info), pretty=pretty)
            else:
                data = read_artifact(target)
                if output_file:
                    with open(output_file, "wb") as f:
                        f.write(data)
                else:
                    sys.stdout.buffer.write(data)

    except PathTraversalError as e:
        output_json(make_err_result(
            code="PATH_TRAVERSAL",
            message=e.message,
            detail=e.details,
        ), pretty=pretty)
        raise typer.Exit(2)
    except ArtifactNotFoundError as e:
        output_json(make_err_result(
            code="NOT_FOUND",
            message=e.message,
            detail=e.details,
        ), pretty=pretty)
        raise typer.Exit(11)
    except Exception as e:
        if json_output:
            output_json(make_err_result(
                code="READ_ERROR",
                message=str(e),
                detail={"error_type": type(e).__name__},
            ), pretty=pretty)
        else:
            print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(1)


@artifacts_app.command("exists")
def artifacts_exists(
    path: Optional[str] = typer.Option(
        None, "--path", "-p",
        help="逻辑路径（artifact key）",
    ),
    uri: Optional[str] = typer.Option(
        None, "--uri", "-u",
        help="物理 URI（file://、s3:// 等）",
    ),
    config_path: Optional[str] = typer.Option(
        None, "--config",
        help="配置文件路径",
    ),
    pretty: bool = typer.Option(
        False, "--pretty",
        help="美化 JSON 输出",
    ),
):
    """
    检查制品是否存在
    """
    if not path and not uri:
        output_json(make_err_result(
            code="INVALID_ARGS",
            message="必须指定 --path 或 --uri",
        ), pretty=pretty)
        raise typer.Exit(1)

    try:
        from artifacts import artifact_exists
        from engram_step1.artifact_store import FileUriStore, PathTraversalError
        from engram_step1.uri import is_physical_uri

        target = uri if uri else path

        if uri and is_physical_uri(uri):
            store = FileUriStore()
            exists = store.exists(uri)
        else:
            exists = artifact_exists(target)

        output_json(make_ok_result(
            path=target,
            exists=exists,
        ), pretty=pretty)

        # 不存在时返回非零退出码
        if not exists:
            raise typer.Exit(1)

    except typer.Exit:
        raise
    except PathTraversalError as e:
        output_json(make_err_result(
            code="PATH_TRAVERSAL",
            message=e.message,
            detail=e.details,
        ), pretty=pretty)
        raise typer.Exit(2)
    except Exception as e:
        output_json(make_err_result(
            code="EXISTS_ERROR",
            message=str(e),
            detail={"error_type": type(e).__name__},
        ), pretty=pretty)
        raise typer.Exit(2)


@artifacts_app.command("delete")
def artifacts_delete(
    path: Optional[str] = typer.Option(
        None, "--path", "-p",
        help="逻辑路径（artifact key）",
    ),
    uri: Optional[str] = typer.Option(
        None, "--uri", "-u",
        help="物理 URI（file://、s3:// 等）",
    ),
    force: bool = typer.Option(
        False, "--force", "-f",
        help="强制删除，不存在时不报错",
    ),
    config_path: Optional[str] = typer.Option(
        None, "--config",
        help="配置文件路径",
    ),
    pretty: bool = typer.Option(
        False, "--pretty",
        help="美化 JSON 输出",
    ),
):
    """
    删除制品
    """
    if not path and not uri:
        output_json(make_err_result(
            code="INVALID_ARGS",
            message="必须指定 --path 或 --uri",
        ), pretty=pretty)
        raise typer.Exit(1)

    try:
        from artifacts import delete_artifact, artifact_exists
        from engram_step1.artifact_store import FileUriStore, PathTraversalError
        from engram_step1.uri import is_physical_uri

        target = uri if uri else path

        if uri and is_physical_uri(uri):
            store = FileUriStore()
            existed = store.exists(uri)
            if existed:
                # FileUriStore 不直接支持删除，需要手动实现
                from pathlib import Path
                file_path = Path(store.resolve(uri).replace("file://", ""))
                if file_path.exists():
                    file_path.unlink()
                    deleted = True
                else:
                    deleted = False
            else:
                deleted = False
        else:
            existed = artifact_exists(target)
            if existed:
                deleted = delete_artifact(target)
            else:
                deleted = False

        if not existed and not force:
            output_json(make_err_result(
                code="NOT_FOUND",
                message=f"制品不存在: {target}",
            ), pretty=pretty)
            raise typer.Exit(1)

        output_json(make_ok_result(
            path=target,
            deleted=deleted or existed,
        ), pretty=pretty)

    except typer.Exit:
        raise
    except PathTraversalError as e:
        output_json(make_err_result(
            code="PATH_TRAVERSAL",
            message=e.message,
            detail=e.details,
        ), pretty=pretty)
        raise typer.Exit(2)
    except Exception as e:
        output_json(make_err_result(
            code="DELETE_ERROR",
            message=str(e),
            detail={"error_type": type(e).__name__},
        ), pretty=pretty)
        raise typer.Exit(1)


@artifacts_app.command("audit")
def artifacts_audit(
    table: str = typer.Option(
        "all", "--table", "-t",
        help="审计目标表 (patch_blobs/attachments/all)",
    ),
    limit: Optional[int] = typer.Option(
        None, "--limit", "-l",
        help="最大审计记录数（每个表）",
    ),
    since: Optional[str] = typer.Option(
        None, "--since",
        help="增量审计起始时间 (ISO 格式)",
    ),
    sample_rate: float = typer.Option(
        1.0, "--sample-rate",
        help="采样率 (0.0-1.0，默认 1.0 全量)",
    ),
    max_bytes_per_sec: Optional[int] = typer.Option(
        None, "--max-bytes-per-sec",
        help="读取速率限制（字节/秒）",
    ),
    fail_on_mismatch: bool = typer.Option(
        False, "--fail-on-mismatch",
        help="发现不匹配时立即退出",
    ),
    artifacts_root: Optional[str] = typer.Option(
        None, "--artifacts-root",
        help="制品根目录（覆盖配置）",
    ),
    config_path: Optional[str] = typer.Option(
        None, "--config",
        help="配置文件路径",
    ),
    pretty: bool = typer.Option(
        False, "--pretty",
        help="美化 JSON 输出",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v",
        help="详细输出",
    ),
):
    """
    审计制品完整性

    连接数据库验证 patch_blobs 和 attachments 表中记录的制品 SHA256 是否与实际文件匹配。
    """
    try:
        from artifact_audit import ArtifactAuditor, SUPPORTED_TABLES

        # 验证 table 参数
        if table not in SUPPORTED_TABLES:
            output_json(make_err_result(
                code="INVALID_ARGS",
                message=f"无效的表名: {table}，有效值: {', '.join(SUPPORTED_TABLES)}",
            ), pretty=pretty)
            raise typer.Exit(1)

        # 验证采样率
        if not 0.0 <= sample_rate <= 1.0:
            output_json(make_err_result(
                code="INVALID_ARGS",
                message=f"采样率必须在 0.0 到 1.0 之间，当前值: {sample_rate}",
            ), pretty=pretty)
            raise typer.Exit(1)

        # 解析 since 参数
        since_dt = None
        if since:
            try:
                since_dt = datetime.fromisoformat(since)
            except ValueError:
                output_json(make_err_result(
                    code="INVALID_ARGS",
                    message=f"无效的时间格式: {since}，请使用 ISO 格式",
                ), pretty=pretty)
                raise typer.Exit(1)

        # 确定要审计的表
        if table == "all":
            tables = ["patch_blobs", "attachments"]
        else:
            tables = [table]

        # 创建审计器
        auditor = ArtifactAuditor(
            artifacts_root=artifacts_root,
            max_bytes_per_sec=max_bytes_per_sec,
            sample_rate=sample_rate,
            verbose=verbose,
        )

        try:
            summary = auditor.run_audit(
                tables=tables,
                limit=limit,
                since=since_dt,
                fail_on_mismatch=fail_on_mismatch,
            )

            output_json(make_ok_result(**summary.to_dict()), pretty=pretty)

            if summary.has_issues:
                raise typer.Exit(1)

        finally:
            auditor.close()

    except typer.Exit:
        raise
    except Exception as e:
        output_json(make_err_result(
            code="AUDIT_ERROR",
            message=str(e),
            detail={"error_type": type(e).__name__},
        ), pretty=pretty)
        raise typer.Exit(1)


@artifacts_app.command("gc")
def artifacts_gc(
    prefix: str = typer.Option(
        ..., "--prefix",
        help="扫描的前缀（必须），如 scm/ 或 attachments/",
    ),
    delete: bool = typer.Option(
        False, "--delete",
        help="执行实际删除操作（默认 dry-run 模式）",
    ),
    older_than_days: Optional[int] = typer.Option(
        None, "--older-than-days",
        help="仅删除指定天数之前的文件",
    ),
    trash_prefix: Optional[str] = typer.Option(
        None, "--trash-prefix",
        help="软删除目标前缀（移动而非删除）",
    ),
    allowed_prefixes: Optional[str] = typer.Option(
        None, "--allowed-prefixes",
        help="允许操作的前缀列表（逗号分隔）",
    ),
    dsn: Optional[str] = typer.Option(
        None, "--dsn",
        help="数据库连接字符串",
    ),
    backend: Optional[str] = typer.Option(
        None, "--backend",
        help="存储后端类型 (local/file/object)",
    ),
    artifacts_root: Optional[str] = typer.Option(
        None, "--artifacts-root",
        help="制品根目录（local 后端）",
    ),
    config_path: Optional[str] = typer.Option(
        None, "--config",
        help="配置文件路径",
    ),
    pretty: bool = typer.Option(
        False, "--pretty",
        help="美化 JSON 输出",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v",
        help="详细输出",
    ),
):
    """
    垃圾回收未引用的制品

    扫描指定前缀下的制品文件，删除数据库中没有引用的制品。
    默认为 dry-run 模式，仅显示待删除列表。
    """
    try:
        from artifact_gc import run_gc, GCPrefixError, GCDatabaseError, GCError

        # 解析 allowed_prefixes
        allowed_list = None
        if allowed_prefixes:
            allowed_list = [p.strip() for p in allowed_prefixes.split(",") if p.strip()]

        result = run_gc(
            prefix=prefix,
            dry_run=not delete,
            delete=delete,
            older_than_days=older_than_days,
            trash_prefix=trash_prefix,
            dsn=dsn,
            backend=backend,
            artifacts_root=artifacts_root,
            allowed_prefixes=allowed_list,
            verbose=verbose,
        )

        # 构建输出结果
        output_data = {
            "scanned_count": result.scanned_count,
            "referenced_count": result.referenced_count,
            "protected_count": result.protected_count,
            "candidates_count": result.candidates_count,
            "skipped_by_age": result.skipped_by_age,
            "deleted_count": result.deleted_count,
            "trashed_count": result.trashed_count,
            "failed_count": result.failed_count,
            "total_size_bytes": result.total_size_bytes,
            "deleted_size_bytes": result.deleted_size_bytes,
            "dry_run": not delete,
        }

        if result.errors:
            output_data["errors"] = result.errors

        output_json(make_ok_result(**output_data), pretty=pretty)

        if result.failed_count > 0:
            raise typer.Exit(1)

    except typer.Exit:
        raise
    except (GCPrefixError, GCDatabaseError, GCError) as e:
        output_json(make_err_result(
            code=type(e).__name__.upper(),
            message=str(e),
        ), pretty=pretty)
        raise typer.Exit(1)
    except Exception as e:
        output_json(make_err_result(
            code="GC_ERROR",
            message=str(e),
            detail={"error_type": type(e).__name__},
        ), pretty=pretty)
        raise typer.Exit(1)


@artifacts_app.command("migrate")
def artifacts_migrate(
    source_backend: str = typer.Option(
        ..., "--source-backend",
        help="源存储后端类型 (local/file/object)",
    ),
    target_backend: str = typer.Option(
        ..., "--target-backend",
        help="目标存储后端类型 (local/file/object)",
    ),
    prefix: Optional[str] = typer.Option(
        None, "--prefix",
        help="迁移的前缀范围（可选）",
    ),
    source_root: Optional[str] = typer.Option(
        None, "--source-root",
        help="源存储根目录（local 后端）",
    ),
    target_root: Optional[str] = typer.Option(
        None, "--target-root",
        help="目标存储根目录（local 后端）",
    ),
    dry_run: bool = typer.Option(
        True, "--dry-run/--no-dry-run",
        help="仅显示待迁移文件，不实际执行",
    ),
    verify: bool = typer.Option(
        False, "--verify",
        help="迁移后校验 SHA256 和大小",
    ),
    update_db: bool = typer.Option(
        False, "--update-db",
        help="更新数据库中的 URI 引用（scm.patch_blobs, logbook.attachments）",
    ),
    delete_source: bool = typer.Option(
        False, "--delete-source",
        help="迁移成功后删除源文件",
    ),
    trash_prefix: Optional[str] = typer.Option(
        None, "--trash-prefix",
        help="软删除目标前缀（移动而非删除源文件）",
    ),
    limit: Optional[int] = typer.Option(
        None, "--limit", "-l",
        help="最大迁移文件数量",
    ),
    concurrency: int = typer.Option(
        1, "--concurrency", "-j",
        help="并发迁移数（默认 1）",
    ),
    config_path: Optional[str] = typer.Option(
        None, "--config",
        help="配置文件路径",
    ),
    pretty: bool = typer.Option(
        False, "--pretty",
        help="美化 JSON 输出",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v",
        help="详细输出",
    ),
):
    """
    迁移制品到不同存储后端

    将制品从一个存储后端迁移到另一个后端。支持：
    - local -> local：本地目录之间迁移
    - local -> object：本地迁移到 S3/MinIO
    - object -> local：S3/MinIO 迁移到本地
    - object -> object：跨 bucket 或跨区域迁移

    功能特性：
    - 流式读写，支持大文件
    - SHA256 校验确保数据完整性（--verify）
    - 可选更新数据库 URI 引用（--update-db）
    - 可选删除源文件或移动到 trash
    - 并发迁移支持（--concurrency）
    """
    try:
        from artifact_migrate import run_migration, MigrationError
        from engram_step1.artifact_store import VALID_BACKENDS

        # 验证后端类型
        if source_backend not in VALID_BACKENDS:
            output_json(make_err_result(
                code="INVALID_ARGS",
                message=f"无效的源后端类型: {source_backend}，有效值: {', '.join(sorted(VALID_BACKENDS))}",
            ), pretty=pretty)
            raise typer.Exit(1)

        if target_backend not in VALID_BACKENDS:
            output_json(make_err_result(
                code="INVALID_ARGS",
                message=f"无效的目标后端类型: {target_backend}，有效值: {', '.join(sorted(VALID_BACKENDS))}",
            ), pretty=pretty)
            raise typer.Exit(1)

        # 加载配置
        config = None
        if config_path:
            config = load_config(config_path)

        if verbose:
            logging.getLogger().setLevel(logging.DEBUG)

        # 执行迁移
        result = run_migration(
            source_backend=source_backend,
            target_backend=target_backend,
            source_root=source_root,
            target_root=target_root,
            prefix=prefix,
            dry_run=dry_run,
            verify=verify,
            update_db=update_db,
            delete_source=delete_source,
            trash_prefix=trash_prefix,
            limit=limit,
            concurrency=concurrency,
            config=config,
            verbose=verbose,
        )

        # 构建输出
        output_data = result.to_dict()
        output_data["source_backend"] = source_backend
        output_data["target_backend"] = target_backend
        if prefix:
            output_data["prefix"] = prefix

        output_json(make_ok_result(**output_data), pretty=pretty)

        if result.failed_count > 0:
            raise typer.Exit(1)

    except typer.Exit:
        raise
    except MigrationError as e:
        output_json(make_err_result(
            code=e.error_type,
            message=e.message,
            detail=e.details if hasattr(e, 'details') else {},
        ), pretty=pretty)
        raise typer.Exit(1)
    except Exception as e:
        output_json(make_err_result(
            code="MIGRATE_ERROR",
            message=str(e),
            detail={"error_type": type(e).__name__},
        ), pretty=pretty)
        raise typer.Exit(1)


# ============ Identity 子命令 ============


@identity_app.command("sync")
def identity_sync(
    repo_root: str = typer.Option(
        ".", "--repo-root", "-r",
        help="仓库根目录（默认当前目录，扫描 .agentx/users/*.yaml）",
    ),
    strict: bool = typer.Option(
        False, "--strict",
        help="严格模式：.agentx/users 目录不存在时报错而非警告",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-n",
        help="只检查配置文件，不写入数据库",
    ),
    config_path: Optional[str] = typer.Option(
        None, "--config", "-c",
        help="配置文件路径",
    ),
    pretty: bool = typer.Option(
        False, "--pretty", "-p",
        help="美化 JSON 输出",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v",
        help="详细输出",
    ),
    quiet: bool = typer.Option(
        False, "--quiet", "-q",
        help="静默模式",
    ),
):
    """
    同步身份信息到数据库

    从 .agentx/users/*.yaml 扫描用户配置，
    从 ~/.agentx/user.config.yaml 加载本地覆盖（可选），
    将用户信息写入 identity.users 和 identity.accounts 表。

    示例:
        step1 identity sync --repo-root /path/to/repo
        step1 identity sync --strict  # 目录不存在时报错
        step1 identity sync --dry-run  # 只检查不写入
    """
    from pathlib import Path

    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        # 加载配置
        config = None
        if config_path:
            config = load_config(config_path)

        # 解析仓库根目录
        repo_path = Path(repo_root).resolve()
        if not repo_path.exists():
            output_json(make_err_result(
                code="PATH_NOT_FOUND",
                message=f"仓库根目录不存在: {repo_path}",
                detail={"path": str(repo_path)},
            ), pretty=pretty)
            raise typer.Exit(1)

        # 导入 identity_sync 模块
        from identity_sync import (
            sync_identities,
            scan_user_configs,
            scan_role_profiles,
            load_home_user_config,
            merge_user_configs,
            AgentXDirectoryNotFoundError,
        )

        if dry_run:
            # Dry-run 模式：只扫描和检查配置
            try:
                user_configs = scan_user_configs(
                    repo_path, quiet=quiet, verbose=verbose, strict=strict
                )
            except AgentXDirectoryNotFoundError as e:
                output_json(make_err_result(
                    code=e.error_type,
                    message=e.message,
                    detail=e.details,
                ), pretty=pretty)
                raise typer.Exit(e.exit_code)

            home_config = load_home_user_config(quiet=quiet, verbose=verbose)
            if home_config and home_config.user_id in user_configs:
                user_configs[home_config.user_id] = merge_user_configs(
                    user_configs[home_config.user_id], home_config
                )
            elif home_config:
                user_configs[home_config.user_id] = home_config

            role_profiles = scan_role_profiles(
                repo_path, quiet=quiet, verbose=verbose, strict=False
            )

            # 输出扫描结果
            output_json(make_ok_result(
                dry_run=True,
                repo_root=str(repo_path),
                users_count=len(user_configs),
                users=[{
                    "user_id": u.user_id,
                    "display_name": u.display_name,
                    "accounts": list(u.accounts.keys()),
                    "source": u.source_path,
                } for u in user_configs.values()],
                role_profiles_count=len(role_profiles),
                role_profiles=[{
                    "user_id": p.user_id,
                    "source": p.source_path,
                } for p in role_profiles.values()],
                home_config_loaded=home_config is not None,
            ), pretty=pretty)
            raise typer.Exit(0)

        # 执行同步
        try:
            stats = sync_identities(
                repo_path,
                config=config,
                quiet=quiet,
                verbose=verbose,
                strict=strict,
            )
        except AgentXDirectoryNotFoundError as e:
            output_json(make_err_result(
                code=e.error_type,
                message=e.message,
                detail=e.details,
            ), pretty=pretty)
            raise typer.Exit(e.exit_code)

        output_json(make_ok_result(
            repo_root=str(repo_path),
            **stats.to_dict(),
            summary=stats.summary(),
        ), pretty=pretty)

    except typer.Exit:
        raise
    except EngramError as e:
        output_json(make_err_result(
            code=e.error_type,
            message=e.message,
            detail=e.details,
        ), pretty=pretty)
        raise typer.Exit(e.exit_code)
    except Exception as e:
        output_json(make_err_result(
            code="UNEXPECTED_ERROR",
            message=str(e),
            detail={"error_type": type(e).__name__},
        ), pretty=pretty)
        raise typer.Exit(1)


# ============ 主入口 ============

def main():
    """主入口"""
    app()


if __name__ == "__main__":
    main()
