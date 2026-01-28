"""
policy - 策略决策模块

根据 governance.settings 中的配置进行写入策略决策：
- team_write_enabled 开关检查
- policy_json 中的具体策略规则

决策结果:
- allow: 允许写入目标空间
- redirect: 重定向到 private 空间
- reject: 拒绝写入（当前实现优先 redirect）
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional


class PolicyAction(Enum):
    """策略决策结果"""
    ALLOW = "allow"
    REDIRECT = "redirect"
    REJECT = "reject"


@dataclass
class PolicyDecision:
    """策略决策结果"""
    action: PolicyAction
    reason: str
    original_space: str
    final_space: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action.value,
            "reason": self.reason,
            "original_space": self.original_space,
            "final_space": self.final_space,
        }


class PolicyEngine:
    """策略引擎"""

    # 默认策略配置
    DEFAULT_POLICY = {
        "allowlist_users": [],  # 允许直接写 team 的用户列表
        "allowed_kinds": ["PROCEDURE", "REVIEW_GUIDE", "PITFALL", "DECISION"],  # 允许的知识类型
        "require_evidence": True,  # 是否强制证据链
        "max_chars": 1200,  # 最大字符数
        "bulk_mode": "very_short",  # 对 is_bulk 提交的处理
    }

    def __init__(
        self,
        team_write_enabled: bool = False,
        policy_json: Optional[Dict[str, Any]] = None,
        project_key: Optional[str] = None,
    ):
        """
        初始化策略引擎

        Args:
            team_write_enabled: 团队可写开关
            policy_json: 策略配置
            project_key: 项目标识
        """
        self.team_write_enabled = team_write_enabled
        self.policy = {**self.DEFAULT_POLICY, **(policy_json or {})}
        self.project_key = project_key

    def decide(
        self,
        target_space: str,
        actor_user_id: Optional[str] = None,
        payload_md: str = "",
        kind: Optional[str] = None,
        evidence_refs: Optional[List[str]] = None,
        is_bulk: bool = False,
        meta_json: Optional[Dict[str, Any]] = None,
    ) -> PolicyDecision:
        """
        执行策略决策

        Args:
            target_space: 请求的目标空间 (team:<project> / private:<user> / org:shared)
            actor_user_id: 操作者用户 ID
            payload_md: 记忆内容（Markdown）
            kind: 知识类型 (FACT/PROCEDURE/PITFALL/DECISION/REVIEW_GUIDE)
            evidence_refs: 证据链引用列表
            is_bulk: 是否为批量提交
            meta_json: 其他元数据

        Returns:
            PolicyDecision 决策结果
        """
        # 确定私有空间（用于 redirect）
        private_space = f"private:{actor_user_id}" if actor_user_id else "private:unknown"

        # 如果请求的是 private 空间，直接允许
        if target_space.startswith("private:"):
            return PolicyDecision(
                action=PolicyAction.ALLOW,
                reason="private_space",
                original_space=target_space,
                final_space=target_space,
            )

        # 如果请求的是 team/org 空间，需要检查策略
        if target_space.startswith("team:") or target_space.startswith("org:"):
            return self._check_team_policy(
                target_space=target_space,
                private_space=private_space,
                actor_user_id=actor_user_id,
                payload_md=payload_md,
                kind=kind,
                evidence_refs=evidence_refs,
                is_bulk=is_bulk,
            )

        # 未知空间类型，拒绝
        return PolicyDecision(
            action=PolicyAction.REJECT,
            reason="unknown_space_type",
            original_space=target_space,
            final_space=target_space,
        )

    def _check_team_policy(
        self,
        target_space: str,
        private_space: str,
        actor_user_id: Optional[str],
        payload_md: str,
        kind: Optional[str],
        evidence_refs: Optional[List[str]],
        is_bulk: bool,
    ) -> PolicyDecision:
        """检查团队空间写入策略"""

        # 1. 检查 team_write_enabled 开关
        if not self.team_write_enabled:
            return PolicyDecision(
                action=PolicyAction.REDIRECT,
                reason="team_write_disabled",
                original_space=target_space,
                final_space=private_space,
            )

        # 2. 检查用户白名单
        allowlist_users: List[str] = self.policy.get("allowlist_users", [])
        if allowlist_users and actor_user_id not in allowlist_users:
            return PolicyDecision(
                action=PolicyAction.REDIRECT,
                reason="user_not_in_allowlist",
                original_space=target_space,
                final_space=private_space,
            )

        # 3. 检查知识类型
        allowed_kinds: List[str] = self.policy.get("allowed_kinds", [])
        if kind and allowed_kinds and kind not in allowed_kinds:
            return PolicyDecision(
                action=PolicyAction.REDIRECT,
                reason=f"kind_not_allowed:{kind}",
                original_space=target_space,
                final_space=private_space,
            )

        # 4. 检查证据链
        require_evidence: bool = self.policy.get("require_evidence", True)
        if require_evidence and (not evidence_refs or len(evidence_refs) == 0):
            return PolicyDecision(
                action=PolicyAction.REDIRECT,
                reason="missing_evidence",
                original_space=target_space,
                final_space=private_space,
            )

        # 5. 检查字符数限制
        max_chars: int = self.policy.get("max_chars", 1200)
        if len(payload_md) > max_chars:
            return PolicyDecision(
                action=PolicyAction.REDIRECT,
                reason=f"exceeds_max_chars:{len(payload_md)}>{max_chars}",
                original_space=target_space,
                final_space=private_space,
            )

        # 6. 检查批量提交模式
        if is_bulk:
            bulk_mode: str = self.policy.get("bulk_mode", "very_short")
            if bulk_mode == "very_short" and len(payload_md) > 200:
                return PolicyDecision(
                    action=PolicyAction.REDIRECT,
                    reason="bulk_too_long",
                    original_space=target_space,
                    final_space=private_space,
                )
            elif bulk_mode == "reject":
                return PolicyDecision(
                    action=PolicyAction.REDIRECT,
                    reason="bulk_not_allowed",
                    original_space=target_space,
                    final_space=private_space,
                )

        # 全部检查通过
        return PolicyDecision(
            action=PolicyAction.ALLOW,
            reason="policy_passed",
            original_space=target_space,
            final_space=target_space,
        )


def create_engine_from_settings(settings: Dict[str, Any]) -> PolicyEngine:
    """
    从 governance.settings 创建策略引擎

    Args:
        settings: 从 Step1Database.get_settings() 获取的设置

    Returns:
        PolicyEngine 实例
    """
    return PolicyEngine(
        team_write_enabled=settings.get("team_write_enabled", False),
        policy_json=settings.get("policy_json", {}),
        project_key=settings.get("project_key"),
    )


def decide_write(
    actor_user_id: str,
    requested_space: str,
    kind: Optional[str] = None,
    is_bulk: bool = False,
    payload_md: str = "",
    evidence_refs: Optional[List[str]] = None,
    *,
    policy_engine: Optional[PolicyEngine] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    写入策略决策便捷函数

    根据提供的参数决定是否允许写入、重定向或拒绝。

    Args:
        actor_user_id: 操作者用户 ID
        requested_space: 请求的目标空间 (team:<project> / private:<user> / org:shared)
        kind: 知识类型 (FACT/PROCEDURE/PITFALL/DECISION/REVIEW_GUIDE)
        is_bulk: 是否为批量提交
        payload_md: 记忆内容（Markdown）
        evidence_refs: 证据链引用列表
        policy_engine: 可选，已创建的策略引擎实例
        settings: 可选，governance.settings 配置（用于创建引擎）

    Returns:
        {
            "action": "allow" | "redirect" | "reject",
            "target_space": 最终目标空间,
            "reason": 决策原因
        }

    Note:
        - 如果未提供 policy_engine，则使用 settings 创建；
        - 如果两者都未提供，则使用默认策略（team_write_enabled=False）
    """
    # 获取或创建策略引擎
    if policy_engine is None:
        if settings is not None:
            policy_engine = create_engine_from_settings(settings)
        else:
            # 使用默认策略（保守模式：不允许团队写入）
            policy_engine = PolicyEngine(team_write_enabled=False)

    # 执行策略决策
    decision = policy_engine.decide(
        target_space=requested_space,
        actor_user_id=actor_user_id,
        payload_md=payload_md,
        kind=kind,
        evidence_refs=evidence_refs,
        is_bulk=is_bulk,
    )

    # 返回简化格式
    return {
        "action": decision.action.value,
        "target_space": decision.final_space,
        "reason": decision.reason,
    }
