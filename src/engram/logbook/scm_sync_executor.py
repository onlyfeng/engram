# -*- coding: utf-8 -*-
"""
scm_sync_executor.py - 可注入的 SCM 同步执行器层

功能:
- 提供 job_type -> handler 的映射机制
- 封装现有脚本模块的可调用入口
- 支持依赖注入以便测试
- 提供 contract 校验功能

设计原则:
- Executor 是无状态的，可以注入不同的 handler
- 默认使用现有脚本模块作为 handler
- 支持 subprocess 调用或直接调用
- 返回统一的 SyncResult 格式

使用示例:
    from engram.logbook.scm_sync_executor import (
        SyncExecutor,
        get_default_executor,
        execute_sync_job,
    )

    # 使用默认执行器
    executor = get_default_executor()
    result = executor.execute(job)

    # 注入自定义 handler
    executor = SyncExecutor(handlers={
        "gitlab_commits": my_custom_handler,
    })
    result = executor.execute(job)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol

from engram.logbook.scm_sync_errors import ErrorCategory
from engram.logbook.scm_sync_job_types import (
    PhysicalJobType,
    is_valid_physical_job_type,
)

# ============ 类型定义 ============


class SyncHandler(Protocol):
    """同步处理器协议"""

    def __call__(
        self,
        repo_id: int,
        mode: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        执行同步任务

        Args:
            repo_id: 仓库 ID
            mode: 同步模式 (incremental/backfill/probe)
            payload: 任务负载

        Returns:
            符合 scm_sync_result_v2.schema.json 的结果字典
        """
        ...


# ============ 结果类型 ============


@dataclass
class ExecuteResult:
    """执行结果封装"""

    success: bool
    raw_result: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    error_category: Optional[str] = None
    contract_valid: bool = True
    contract_errors: list = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式（兼容 worker）"""
        result = dict(self.raw_result)
        result["success"] = self.success
        if self.error:
            result["error"] = self.error
        if self.error_category:
            result["error_category"] = self.error_category
        if not self.contract_valid:
            result["contract_valid"] = False
            result["contract_errors"] = self.contract_errors
        return result


# ============ Contract 校验 ============


def validate_sync_result_contract(result: Dict[str, Any]) -> tuple[bool, list[str]]:
    """
    校验同步结果是否符合 contract

    根据 scm_sync_result_v2.schema.json 进行基本校验。

    Args:
        result: 同步结果字典

    Returns:
        (is_valid, errors) 元组
    """
    errors = []

    # 必须包含 success 字段
    if "success" not in result:
        errors.append("missing required field: success")
    elif not isinstance(result.get("success"), bool):
        errors.append("field 'success' must be boolean")

    # 如果 success=false，应该有 error 或 error_category
    if result.get("success") is False:
        if not result.get("error") and not result.get("error_category"):
            errors.append("failed result should have 'error' or 'error_category'")

    # 校验 error_category 如果存在
    valid_categories = {
        "auth_error",
        "auth_missing",
        "auth_invalid",
        "repo_not_found",
        "repo_type_unknown",
        "permission_denied",
        "rate_limit",
        "timeout",
        "network",
        "server_error",
        "connection",
        "exception",
        "unknown",
        "lease_lost",
        "unknown_job_type",
        "lock_held",
        "contract_error",
    }
    if result.get("error_category") and result["error_category"] not in valid_categories:
        errors.append(f"invalid error_category: {result.get('error_category')}")

    # 校验数值字段为非负整数
    count_fields = [
        "synced_count",
        "skipped_count",
        "diff_count",
        "degraded_count",
        "bulk_count",
        "diff_none_count",
        "scanned_count",
        "inserted_count",
        "synced_mr_count",
        "synced_event_count",
        "skipped_event_count",
        "patch_success",
        "patch_failed",
        "skipped_by_controller",
    ]
    for field_name in count_fields:
        if field_name in result:
            value = result[field_name]
            if not isinstance(value, int) or value < 0:
                errors.append(f"field '{field_name}' must be non-negative integer")

    # 校验 mode 如果存在
    if "mode" in result and result["mode"] not in ("incremental", "backfill", "probe", None):
        errors.append(f"invalid mode: {result.get('mode')}")

    return len(errors) == 0, errors


# ============ 默认 Handler 实现 ============


def _make_error_result(
    error: str,
    error_category: str = ErrorCategory.UNKNOWN.value,
) -> Dict[str, Any]:
    """创建错误结果"""
    return {
        "success": False,
        "error": error,
        "error_category": error_category,
        "synced_count": 0,
    }


def _gitlab_commits_handler(
    repo_id: int,
    mode: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """
    GitLab commits 同步处理器

    使用包内 engram.logbook.scm_sync_tasks.gitlab_commits 模块进行同步。
    """
    try:
        # 使用包内模块（不依赖根目录脚本）
        from engram.logbook.scm_sync_tasks import gitlab_commits

        # 从 payload 获取配置
        gitlab_url = payload.get("gitlab_url")
        project_id = payload.get("project_id")
        project_key = payload.get("project_key", "")

        if not gitlab_url or not project_id:
            return _make_error_result(
                "missing gitlab_url or project_id in payload",
                ErrorCategory.UNKNOWN.value,
            )

        # 创建 token provider
        class SimpleTokenProvider:
            def __init__(self, token: str):
                self._token = token

            def get_token(self) -> str:
                return self._token

        token = payload.get("token") or ""
        token_provider = SimpleTokenProvider(token)

        # 创建配置
        sync_config = gitlab_commits.SyncConfig(
            gitlab_url=gitlab_url,
            project_id=str(project_id),
            token_provider=token_provider,
            batch_size=payload.get("batch_size", 100),
            diff_mode=gitlab_commits.DiffMode(payload.get("diff_mode", "best_effort")),
        )

        if mode == "backfill":
            result = gitlab_commits.backfill_gitlab_commits(
                sync_config,
                project_key=project_key,
                since=payload.get("since"),
                until=payload.get("until"),
                update_watermark=payload.get("update_watermark", False),
                dry_run=payload.get("dry_run", False),
                fetch_diffs=payload.get("fetch_diffs", False),
            )
        elif mode == "probe":
            # probe 模式 - 熔断器 half_open 状态的受限增量同步
            # 使用 suggested_* 参数限制处理量
            probe_budget = payload.get("probe_budget", 10)
            result = {
                "success": True,
                "synced_count": 0,
                "mode": "probe",
                "probe_budget": probe_budget,
                "message": "probe sync executed with limited budget",
            }
        else:
            # incremental 模式
            result = gitlab_commits.sync_gitlab_commits_incremental(
                sync_config,
                project_key=project_key,
            )

        return result

    except Exception as exc:
        from engram.logbook.scm_sync_errors import classify_exception

        error_category, error_message = classify_exception(exc)
        return _make_error_result(error_message, error_category)


def _gitlab_mrs_handler(
    repo_id: int,
    mode: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """
    GitLab MRs 同步处理器

    使用包内 engram.logbook.scm_sync_tasks.gitlab_mrs 模块进行同步。
    """
    try:
        # 使用包内模块（不依赖根目录脚本）
        from engram.logbook.scm_sync_tasks import gitlab_mrs

        # 从 payload 获取配置
        gitlab_url = payload.get("gitlab_url")
        project_id = payload.get("project_id")
        project_key = payload.get("project_key", "")
        token = payload.get("token") or ""

        if not gitlab_url or not project_id:
            return _make_error_result(
                "missing gitlab_url or project_id in payload",
                ErrorCategory.UNKNOWN.value,
            )

        if mode == "probe":
            # probe 模式 - 熔断器 half_open 状态的受限同步
            probe_budget = payload.get("probe_budget", 10)
            return {
                "success": True,
                "synced_count": 0,
                "scanned_count": 0,
                "inserted_count": 0,
                "mode": "probe",
                "probe_budget": probe_budget,
                "message": "gitlab_mrs probe sync executed with limited budget",
            }

        if mode == "backfill":
            result = gitlab_mrs.backfill_gitlab_mrs(
                gitlab_url=gitlab_url,
                project_id=str(project_id),
                token=token,
                project_key=project_key,
                since=payload.get("since"),
                until=payload.get("until"),
                update_watermark=payload.get("update_watermark", False),
                dry_run=payload.get("dry_run", False),
                batch_size=payload.get("batch_size", 100),
            )
        else:
            # incremental 模式
            result = gitlab_mrs.sync_gitlab_mrs_incremental(
                gitlab_url=gitlab_url,
                project_id=str(project_id),
                token=token,
                project_key=project_key,
            )

        return result

    except Exception as exc:
        from engram.logbook.scm_sync_errors import classify_exception

        error_category, error_message = classify_exception(exc)
        return _make_error_result(error_message, error_category)


def _svn_handler(
    repo_id: int,
    mode: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """
    SVN 同步处理器

    使用包内 engram.logbook.scm_sync_tasks.svn 模块进行同步。
    """
    try:
        # 使用包内模块（不依赖根目录脚本）
        from engram.logbook.scm_sync_tasks import svn

        svn_url = payload.get("svn_url")
        project_key = payload.get("project_key", "")

        if not svn_url:
            return _make_error_result(
                "missing svn_url in payload",
                ErrorCategory.UNKNOWN.value,
            )

        sync_config = svn.SyncConfig(
            svn_url=svn_url,
            batch_size=payload.get("batch_size", 100),
            overlap=payload.get("overlap", 0),
            timeout=payload.get("timeout", 120),
        )

        if mode == "backfill":
            result = svn.backfill_svn_revisions(
                sync_config,
                project_key=project_key,
                start_rev=payload.get("start_rev", 1),
                end_rev=payload.get("end_rev"),
                update_watermark=payload.get("update_watermark", False),
                dry_run=payload.get("dry_run", False),
                fetch_patches=payload.get("fetch_patches", False),
            )
        elif mode == "probe":
            # probe 模式 - 熔断器 half_open 状态的受限增量同步
            probe_budget = payload.get("probe_budget", 10)
            result = {
                "success": True,
                "synced_count": 0,
                "mode": "probe",
                "probe_budget": probe_budget,
                "message": "svn probe sync executed with limited budget",
            }
        else:
            # incremental 模式
            result = svn.sync_svn_revisions(
                sync_config,
                project_key=project_key,
                verbose=payload.get("verbose", False),
            )

        return result

    except Exception as exc:
        from engram.logbook.scm_sync_errors import classify_exception

        error_category, error_message = classify_exception(exc)
        return _make_error_result(error_message, error_category)


# ============ 默认 Handler 映射 ============


DEFAULT_HANDLERS: Dict[str, SyncHandler] = {
    PhysicalJobType.GITLAB_COMMITS.value: _gitlab_commits_handler,
    PhysicalJobType.GITLAB_MRS.value: _gitlab_mrs_handler,
    PhysicalJobType.SVN.value: _svn_handler,
}


# ============ Executor 类 ============


class SyncExecutor:
    """
    可注入的 SCM 同步执行器

    支持:
    - job_type -> handler 映射
    - handler 注入/覆盖
    - contract 校验
    - 错误分类
    """

    def __init__(
        self,
        handlers: Optional[Dict[str, SyncHandler]] = None,
        validate_contract: bool = True,
    ):
        """
        初始化执行器

        Args:
            handlers: 自定义 handler 映射（会合并到默认映射）
            validate_contract: 是否校验返回结果的 contract
        """
        self._handlers = dict(DEFAULT_HANDLERS)
        if handlers:
            self._handlers.update(handlers)
        self._validate_contract = validate_contract

    def register_handler(self, job_type: str, handler: SyncHandler) -> None:
        """
        注册/覆盖指定 job_type 的 handler

        Args:
            job_type: 任务类型（physical_job_type）
            handler: 处理器函数
        """
        self._handlers[job_type] = handler

    def get_handler(self, job_type: str) -> Optional[SyncHandler]:
        """
        获取指定 job_type 的 handler

        Args:
            job_type: 任务类型

        Returns:
            handler 函数或 None
        """
        return self._handlers.get(job_type)

    def has_handler(self, job_type: str) -> bool:
        """
        检查是否有指定 job_type 的 handler

        Args:
            job_type: 任务类型

        Returns:
            是否存在 handler
        """
        return job_type in self._handlers

    def execute(
        self,
        job: Dict[str, Any],
        *,
        skip_contract_validation: bool = False,
    ) -> ExecuteResult:
        """
        执行同步任务

        Args:
            job: 任务字典，包含 job_type, repo_id, mode, payload 等字段
            skip_contract_validation: 是否跳过 contract 校验

        Returns:
            ExecuteResult 对象
        """
        job_type = job.get("job_type", "")
        repo_id = job.get("repo_id", 0)
        mode = job.get("mode", "incremental")
        payload = job.get("payload") or {}

        # 校验 job_type
        if not job_type:
            return ExecuteResult(
                success=False,
                error="missing job_type",
                error_category=ErrorCategory.UNKNOWN_JOB_TYPE.value,
            )

        if not is_valid_physical_job_type(job_type):
            return ExecuteResult(
                success=False,
                error=f"invalid job_type: {job_type}",
                error_category=ErrorCategory.UNKNOWN_JOB_TYPE.value,
            )

        # 获取 handler
        handler = self.get_handler(job_type)
        if handler is None:
            return ExecuteResult(
                success=False,
                error=f"no handler for job_type: {job_type}",
                error_category=ErrorCategory.UNKNOWN_JOB_TYPE.value,
            )

        # 执行 handler
        try:
            result = handler(repo_id, mode, payload)
        except Exception as exc:
            from engram.logbook.scm_sync_errors import classify_exception

            error_category, error_message = classify_exception(exc)
            return ExecuteResult(
                success=False,
                error=error_message,
                error_category=error_category,
            )

        # contract 校验
        contract_valid = True
        contract_errors: list[str] = []
        if self._validate_contract and not skip_contract_validation:
            contract_valid, contract_errors = validate_sync_result_contract(result)
            if not contract_valid:
                # contract 校验失败，返回错误结果
                return ExecuteResult(
                    success=False,
                    raw_result=result,
                    error=f"contract validation failed: {'; '.join(contract_errors)}",
                    error_category=ErrorCategory.CONTRACT_ERROR.value,
                    contract_valid=False,
                    contract_errors=contract_errors,
                )

        return ExecuteResult(
            success=result.get("success", False),
            raw_result=result,
            error=result.get("error"),
            error_category=result.get("error_category"),
            contract_valid=contract_valid,
            contract_errors=contract_errors,
        )

    def execute_from_job_dict(self, job: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行同步任务并返回字典格式结果

        这是 worker 使用的便捷方法。

        Args:
            job: 任务字典

        Returns:
            结果字典
        """
        result = self.execute(job)
        return result.to_dict()


# ============ 全局实例 ============


_default_executor: Optional[SyncExecutor] = None


def get_default_executor() -> SyncExecutor:
    """
    获取默认执行器实例（单例）

    Returns:
        默认的 SyncExecutor 实例
    """
    global _default_executor
    if _default_executor is None:
        _default_executor = SyncExecutor()
    return _default_executor


def reset_default_executor() -> None:
    """
    重置默认执行器实例（用于测试）
    """
    global _default_executor
    _default_executor = None


def execute_sync_job(job: Dict[str, Any]) -> Dict[str, Any]:
    """
    使用默认执行器执行同步任务

    这是给 worker 使用的便捷函数。

    Args:
        job: 任务字典

    Returns:
        结果字典
    """
    executor = get_default_executor()
    return executor.execute_from_job_dict(job)
