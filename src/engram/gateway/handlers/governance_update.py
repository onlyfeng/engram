"""
governance_update handler - governance_update 工具核心实现

提供 governance_update_impl 函数，处理：
1. 鉴权校验（admin_key 或 allowlist_users）
2. 读取当前设置
3. 更新设置（合并变更）
4. 写入审计日志
5. 返回更新后的设置

依赖注入支持：
- 推荐：通过 deps 参数传入 GatewayDeps (推荐)
- [DEPRECATED v0.9] 函数签名包含可选的 _config, _db 参数（向后兼容）
- [DEPRECATED v0.9] 如果不传入任何依赖参数，使用模块级函数获取（不推荐）

弃用计划：
- v0.9（当前）：兼容期，使用 legacy 参数时产生 DeprecationWarning
- v1.0：移除 _config/_db 参数，deps 参数变为必需
"""

import logging
import warnings
from typing import TYPE_CHECKING, Any, Dict, Optional

from pydantic import BaseModel

from ..config import GatewayConfig, get_config
from ..di import GatewayDeps, GatewayDepsProtocol

# NOTE: logbook_db.get_db 已弃用，新代码应通过 deps.db 或 deps.logbook_adapter 获取数据库操作

if TYPE_CHECKING:
    # LogbookDatabase 类型仅用于向后兼容，新代码应使用 LogbookAdapter
    from ..logbook_db import LogbookDatabase

# 导入统一错误码
try:
    from engram.logbook.errors import ErrorCode
except ImportError:

    class ErrorCode:
        GOVERNANCE_UPDATE_MISSING_CREDENTIALS = "governance_update:missing_credentials"
        GOVERNANCE_UPDATE_ADMIN_KEY_NOT_CONFIGURED = "governance_update:admin_key_not_configured"
        GOVERNANCE_UPDATE_INVALID_ADMIN_KEY = "governance_update:invalid_admin_key"
        GOVERNANCE_UPDATE_USER_NOT_IN_ALLOWLIST = "governance_update:user_not_in_allowlist"
        GOVERNANCE_UPDATE_ADMIN_KEY = "governance_update:admin_key"
        GOVERNANCE_UPDATE_ALLOWLIST_USER = "governance_update:allowlist_user"
        GOVERNANCE_UPDATE_INTERNAL_ERROR = "governance_update:internal_error"


logger = logging.getLogger("gateway.handlers.governance_update")


class GovernanceSettingsUpdateResponse(BaseModel):
    """governance_update 响应模型"""

    ok: bool
    action: str  # allow / reject
    settings: Optional[Dict[str, Any]] = None
    message: Optional[str] = None


async def governance_update_impl(
    team_write_enabled: Optional[bool] = None,
    policy_json: Optional[Dict[str, Any]] = None,
    admin_key: Optional[str] = None,
    actor_user_id: Optional[str] = None,
    # 依赖注入参数（推荐方式）
    deps: Optional[GatewayDepsProtocol] = None,
    # [DEPRECATED v0.9 -> 移除于 v1.0] 以下参数已弃用，请使用 deps 参数
    # 弃用计划：
    #   - v0.9（当前）：兼容期，使用时产生 DeprecationWarning
    #   - v1.0：移除这些参数
    # 迁移指南：docs/architecture/adr_gateway_di_and_entry_boundary.md
    _config: Optional[GatewayConfig] = None,
    _db: Optional["LogbookDatabase"] = None,
) -> GovernanceSettingsUpdateResponse:
    """
    governance_update 核心实现

    鉴权方式（满足其一即可）：
    1. admin_key 与环境变量 GOVERNANCE_ADMIN_KEY 匹配
    2. actor_user_id 在 policy_json.allowlist_users 中

    流程:
    1. 鉴权校验
    2. 读取当前设置
    3. 更新设置（合并变更）
    4. 写入审计日志
    5. 返回更新后的设置

    Args:
        team_write_enabled: 是否启用团队写入
        policy_json: 策略 JSON
        admin_key: 管理密钥
        actor_user_id: 执行操作的用户标识
        deps: 可选的 GatewayDeps 依赖容器，优先使用其中的依赖
        _config: 可选的 GatewayConfig 对象，用于向后兼容（推荐使用 deps.config）
        _db: 可选的 LogbookDatabase 对象，用于向后兼容（推荐使用 deps.db）
    """
    # [DEPRECATED v0.9] 弃用警告：_config/_db 参数将在 v1.0 移除
    # 弃用计划：
    #   - v0.9（当前）：兼容期，使用时产生 DeprecationWarning
    #   - v1.0：移除这些参数，强制使用 deps 参数
    if _config is not None or _db is not None:
        warnings.warn(
            "[Gateway v0.9 弃用警告] governance_update_impl 的 _config/_db 参数已弃用，"
            "将在 v1.0 移除。请使用 deps=GatewayDeps.create() 或 deps=GatewayDeps.for_testing(...) 替代。"
            "迁移指南：docs/architecture/adr_gateway_di_and_entry_boundary.md",
            DeprecationWarning,
            stacklevel=2,
        )

    # 获取配置（支持依赖注入）：deps 优先 > _config 参数 > 全局 getter
    if deps is not None:
        config = deps.config
    elif _config is not None:
        config = _config
    else:
        # DI-BOUNDARY-ALLOW: legacy fallback (v0.9 兼容期，v1.0 移除)
        config = get_config()

    # 确保有可用的 deps 对象（用于后续依赖统一获取）
    # DI-BOUNDARY-ALLOW: legacy fallback (v0.9 兼容期，v1.0 移除)
    if deps is None:
        # DI-BOUNDARY-ALLOW: legacy fallback (v0.9 兼容期，v1.0 移除)
        deps = GatewayDeps.create(config=config)
        # [DEPRECATED v0.9 -> 移除于 v1.0] 兼容分支：如果有显式传入的 _db，注入到 deps 中
        # 此路径仅为向后兼容保留，将在 v1.0 移除，新代码应完全使用 deps 参数
        if _db is not None:
            deps._db = _db

    # 获取 DB 实例（统一通过 deps 获取）
    db = deps.db

    # 读取当前设置
    current_settings = db.get_or_create_settings(config.project_key)
    current_policy = current_settings.get("policy_json") or {}
    allowlist_users = current_policy.get("allowlist_users", [])

    # 鉴权校验
    auth_passed = False
    auth_method = None
    reject_reason = None

    # 方式 1: admin_key 匹配
    if admin_key and config.governance_admin_key:
        if admin_key == config.governance_admin_key:
            auth_passed = True
            auth_method = "admin_key"

    # 方式 2: actor_user_id 在 allowlist_users 中
    if not auth_passed and actor_user_id:
        if actor_user_id in allowlist_users:
            auth_passed = True
            auth_method = "allowlist_user"

    # 如果鉴权失败
    if not auth_passed:
        if not admin_key and not actor_user_id:
            reject_reason = ErrorCode.GOVERNANCE_UPDATE_MISSING_CREDENTIALS
        elif admin_key and not config.governance_admin_key:
            reject_reason = ErrorCode.GOVERNANCE_UPDATE_ADMIN_KEY_NOT_CONFIGURED
        elif admin_key:
            reject_reason = ErrorCode.GOVERNANCE_UPDATE_INVALID_ADMIN_KEY
        else:
            reject_reason = ErrorCode.GOVERNANCE_UPDATE_USER_NOT_IN_ALLOWLIST

        # 写入审计日志（拒绝）
        db.insert_audit(
            actor_user_id=actor_user_id,
            target_space=f"governance:{config.project_key}",
            action="reject",
            reason=reject_reason,
            payload_sha=None,
            evidence_refs_json={
                "source": "gateway",
                "operation": "governance_update",
                "auth_method_attempted": "admin_key" if admin_key else "allowlist",
            },
        )

        logger.warning(f"governance_update 鉴权失败: {reject_reason}, actor={actor_user_id}")

        return GovernanceSettingsUpdateResponse(
            ok=False,
            action="reject",
            settings=None,
            message=f"鉴权失败: {reject_reason}",
        )

    # 鉴权通过，执行更新
    try:
        # 合并策略变更
        new_team_write_enabled = (
            team_write_enabled
            if team_write_enabled is not None
            else current_settings.get("team_write_enabled", False)
        )

        # 合并 policy_json
        if policy_json is not None:
            new_policy = {**current_policy, **policy_json}
        else:
            new_policy = current_policy

        # 通过 deps.logbook_adapter 获取 adapter（禁止使用 logbook_adapter.get_adapter()）
        # 确保 settings/audit 操作使用统一的 adapter 实例
        adapter = deps.logbook_adapter

        # 执行更新
        success = adapter.upsert_settings(
            project_key=config.project_key,
            team_write_enabled=new_team_write_enabled,
            policy_json=new_policy,
            updated_by=actor_user_id,
        )

        if not success:
            raise RuntimeError("upsert_settings 返回失败")

        # 读取更新后的设置
        updated_settings = db.get_settings(config.project_key)

        # 根据认证方式选择 reason
        if auth_method == "admin_key":
            auth_reason = ErrorCode.GOVERNANCE_UPDATE_ADMIN_KEY
        else:
            auth_reason = ErrorCode.GOVERNANCE_UPDATE_ALLOWLIST_USER

        # 写入审计日志（允许）
        db.insert_audit(
            actor_user_id=actor_user_id,
            target_space=f"governance:{config.project_key}",
            action="allow",
            reason=auth_reason,
            payload_sha=None,
            evidence_refs_json={
                "source": "gateway",
                "operation": "governance_update",
                "auth_method": auth_method,
                "changes": {
                    "team_write_enabled": team_write_enabled,
                    "policy_json_updated": policy_json is not None,
                },
            },
        )

        logger.info(
            f"governance_update 成功: project={config.project_key}, actor={actor_user_id}, auth_method={auth_method}"
        )

        return GovernanceSettingsUpdateResponse(
            ok=True,
            action="allow",
            settings=updated_settings,
            message=None,
        )

    except Exception as e:
        logger.exception(f"governance_update 执行失败: {e}")

        # 写入审计日志（错误）
        db.insert_audit(
            actor_user_id=actor_user_id,
            target_space=f"governance:{config.project_key}",
            action="reject",
            reason=ErrorCode.GOVERNANCE_UPDATE_INTERNAL_ERROR,
            payload_sha=None,
            evidence_refs_json={
                "source": "gateway",
                "operation": "governance_update",
                "error": str(e)[:500],
            },
        )

        return GovernanceSettingsUpdateResponse(
            ok=False,
            action="reject",
            settings=None,
            message=f"更新失败: {str(e)}",
        )
