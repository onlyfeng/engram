"""
governance_update handler - governance_update 工具核心实现

提供 governance_update_impl 函数，处理：
1. 鉴权校验（admin_key 或 allowlist_users）
2. 读取当前设置
3. 更新设置（合并变更）
4. 写入审计日志
5. 返回更新后的设置

依赖注入（v1.0）：
- deps 参数为必传，通过 GatewayDeps 容器提供所有依赖
- 所有数据库操作统一使用 deps.db / deps.logbook_adapter
- 配置统一使用 deps.config
"""

import logging
from typing import Any, Dict, Optional

from pydantic import BaseModel

# 导入统一错误码
from engram.logbook.errors import ErrorCode

from ..audit_event import AuditWriteError
from ..di import GatewayDepsProtocol
from ..services.audit_service import write_audit_or_raise

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
    *,
    correlation_id: Optional[str] = None,
    deps: GatewayDepsProtocol,
) -> GovernanceSettingsUpdateResponse:
    """
    governance_update 核心实现（v1.0）

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
        deps: GatewayDeps 依赖容器（必传）
    """
    # 统一从 deps 获取配置和数据库实例
    config = deps.config
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

        # 使用传入的 correlation_id 或生成新的（用于追踪）
        from ..mcp_rpc import generate_correlation_id

        corr_id = correlation_id or generate_correlation_id()

        # 写入审计日志（拒绝）- audit-first 策略：失败时阻断主操作
        try:
            write_audit_or_raise(
                db=db,
                actor_user_id=actor_user_id,
                target_space=f"governance:{config.project_key}",
                action="reject",
                reason=reject_reason,
                payload_sha=None,
                evidence_refs_json={
                    "source": "gateway",
                    "operation": "governance_update",
                    "auth_method_attempted": "admin_key" if admin_key else "allowlist",
                    "correlation_id": corr_id,
                },
                correlation_id=corr_id,
            )
        except AuditWriteError as e:
            logger.error(f"governance_update 审计写入失败: {e}, correlation_id={corr_id}")
            return GovernanceSettingsUpdateResponse(
                ok=False,
                action="error",
                settings=None,
                message=f"审计写入失败，操作已阻断 (correlation_id={corr_id})",
            )

        logger.warning(f"governance_update 鉴权失败: {reject_reason}, actor={actor_user_id}")

        return GovernanceSettingsUpdateResponse(
            ok=False,
            action="reject",
            settings=None,
            message=f"鉴权失败: {reject_reason}",
        )

    # 鉴权通过，执行更新
    # 使用传入的 correlation_id 或生成新的（用于追踪）
    from ..mcp_rpc import generate_correlation_id

    corr_id = correlation_id or generate_correlation_id()

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

        # 写入审计日志（允许）- audit-first 策略：失败时阻断主操作
        try:
            write_audit_or_raise(
                db=db,
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
                    "correlation_id": corr_id,
                },
                correlation_id=corr_id,
            )
        except AuditWriteError as e:
            logger.error(f"governance_update 审计写入失败: {e}, correlation_id={corr_id}")
            return GovernanceSettingsUpdateResponse(
                ok=False,
                action="error",
                settings=None,
                message=f"审计写入失败，操作已阻断 (correlation_id={corr_id})",
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

    except AuditWriteError:
        # AuditWriteError 已在上方处理，此处重新抛出以避免被下方 catch-all 捕获
        raise

    except Exception as e:
        logger.exception(f"governance_update 执行失败: {e}")

        # 写入审计日志（错误）- audit-first 策略：失败时返回 error
        try:
            write_audit_or_raise(
                db=db,
                actor_user_id=actor_user_id,
                target_space=f"governance:{config.project_key}",
                action="reject",
                reason=ErrorCode.GOVERNANCE_UPDATE_INTERNAL_ERROR,
                payload_sha=None,
                evidence_refs_json={
                    "source": "gateway",
                    "operation": "governance_update",
                    "error": str(e)[:500],
                    "correlation_id": corr_id,
                },
                correlation_id=corr_id,
            )
        except AuditWriteError as audit_err:
            logger.error(
                f"governance_update 错误审计写入也失败: {audit_err}, correlation_id={corr_id}"
            )
            return GovernanceSettingsUpdateResponse(
                ok=False,
                action="error",
                settings=None,
                message=f"操作失败且审计写入失败 (correlation_id={corr_id})",
            )

        return GovernanceSettingsUpdateResponse(
            ok=False,
            action="reject",
            settings=None,
            message=f"更新失败: {str(e)}",
        )
