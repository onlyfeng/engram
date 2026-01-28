#!/usr/bin/env python3
"""
identity_resolve.py - 身份解析模块

功能:
- 根据账户类型和账户信息解析对应的 user_id
- 支持通过 username、email、display_name 进行匹配
- 提供缓存机制减少数据库查询
- 返回解析结果包含 user_id 和匹配方式

使用:
    from identity_resolve import resolve_user_id, IdentityResolver

    # 简单调用
    result = resolve_user_id("svn", username="zhangsan")

    # 使用解析器实例（带缓存）
    resolver = IdentityResolver(config=config)
    result = resolver.resolve("gitlab", username="zhangsan", email="zhangsan@example.com")
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

import psycopg

from engram_step1.config import Config
from engram_step1.db import get_connection

logger = logging.getLogger(__name__)


@dataclass
class ResolveResult:
    """身份解析结果"""
    user_id: Optional[str] = None
    match_type: Optional[str] = None  # username, email, display, alias
    match_value: Optional[str] = None  # 匹配到的值
    confidence: float = 0.0  # 匹配置信度 (0.0 - 1.0)
    account_verified: bool = False  # 账户是否已验证

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        if not self.user_id:
            return {}
        return {
            "resolved_user_id": self.user_id,
            "match_type": self.match_type,
            "match_value": self.match_value,
            "confidence": self.confidence,
            "account_verified": self.account_verified,
        }

    @property
    def resolved(self) -> bool:
        """是否成功解析"""
        return self.user_id is not None


class IdentityResolver:
    """
    身份解析器

    支持缓存的身份解析，减少数据库查询次数。

    用法:
        resolver = IdentityResolver(config=config)
        result = resolver.resolve("svn", username="zhangsan")
        if result.resolved:
            print(f"解析成功: {result.user_id}")
    """

    def __init__(self, config: Optional[Config] = None, cache_enabled: bool = True):
        """
        初始化解析器

        Args:
            config: 配置实例
            cache_enabled: 是否启用缓存
        """
        self._config = config
        self._cache_enabled = cache_enabled
        # 缓存结构: {(account_type, username): ResolveResult}
        self._username_cache: Dict[tuple, ResolveResult] = {}
        # 缓存结构: {email: ResolveResult}
        self._email_cache: Dict[str, ResolveResult] = {}
        # 缓存结构: {display_name: ResolveResult}
        self._display_cache: Dict[str, ResolveResult] = {}

    def clear_cache(self) -> None:
        """清空缓存"""
        self._username_cache.clear()
        self._email_cache.clear()
        self._display_cache.clear()

    def resolve(
        self,
        account_type: str,
        username: Optional[str] = None,
        email: Optional[str] = None,
        display: Optional[str] = None,
    ) -> ResolveResult:
        """
        解析用户身份

        解析优先级:
        1. account_type + username 精确匹配
        2. email 匹配
        3. username 别名匹配
        4. display_name 模糊匹配

        Args:
            account_type: 账户类型 (svn, gitlab, git, email)
            username: 用户名
            email: 邮箱地址
            display: 显示名称

        Returns:
            ResolveResult 解析结果
        """
        # 1. 尝试通过 username 精确匹配
        if username:
            cache_key = (account_type, username.lower())
            if self._cache_enabled and cache_key in self._username_cache:
                return self._username_cache[cache_key]

            result = self._resolve_by_username(account_type, username)
            if result.resolved:
                if self._cache_enabled:
                    self._username_cache[cache_key] = result
                return result

        # 2. 尝试通过 email 匹配
        if email:
            email_lower = email.lower()
            if self._cache_enabled and email_lower in self._email_cache:
                return self._email_cache[email_lower]

            result = self._resolve_by_email(email)
            if result.resolved:
                if self._cache_enabled:
                    self._email_cache[email_lower] = result
                return result

        # 3. 尝试通过 username 别名匹配
        if username:
            result = self._resolve_by_alias(account_type, username)
            if result.resolved:
                if self._cache_enabled:
                    cache_key = (account_type, username.lower())
                    self._username_cache[cache_key] = result
                return result

        # 4. 尝试通过 display_name 模糊匹配
        if display:
            display_lower = display.lower()
            if self._cache_enabled and display_lower in self._display_cache:
                return self._display_cache[display_lower]

            result = self._resolve_by_display(display)
            if result.resolved:
                if self._cache_enabled:
                    self._display_cache[display_lower] = result
                return result

        # 未能解析
        return ResolveResult()

    def _resolve_by_username(
        self,
        account_type: str,
        username: str,
    ) -> ResolveResult:
        """通过账户类型和用户名解析"""
        conn = get_connection(config=self._config)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT user_id, verified
                    FROM identity.accounts
                    WHERE account_type = %s AND LOWER(account_name) = LOWER(%s)
                    LIMIT 1
                    """,
                    (account_type, username),
                )
                row = cur.fetchone()
                if row:
                    return ResolveResult(
                        user_id=row[0],
                        match_type="username",
                        match_value=username,
                        confidence=1.0,
                        account_verified=row[1] if row[1] is not None else False,
                    )
        except psycopg.Error as e:
            logger.warning(f"解析用户名失败 ({account_type}/{username}): {e}")
        finally:
            conn.close()

        return ResolveResult()

    def _resolve_by_email(self, email: str) -> ResolveResult:
        """通过邮箱解析"""
        conn = get_connection(config=self._config)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT user_id, verified, account_type
                    FROM identity.accounts
                    WHERE LOWER(email) = LOWER(%s)
                    LIMIT 1
                    """,
                    (email,),
                )
                row = cur.fetchone()
                if row:
                    return ResolveResult(
                        user_id=row[0],
                        match_type="email",
                        match_value=email,
                        confidence=0.9,  # email 匹配置信度略低于精确 username
                        account_verified=row[1] if row[1] is not None else False,
                    )
        except psycopg.Error as e:
            logger.warning(f"解析邮箱失败 ({email}): {e}")
        finally:
            conn.close()

        return ResolveResult()

    def _resolve_by_alias(
        self,
        account_type: str,
        username: str,
    ) -> ResolveResult:
        """通过别名解析"""
        conn = get_connection(config=self._config)
        try:
            with conn.cursor() as cur:
                # 在 aliases_json 中查找匹配
                cur.execute(
                    """
                    SELECT user_id, verified
                    FROM identity.accounts
                    WHERE account_type = %s
                      AND aliases_json::jsonb ? %s
                    LIMIT 1
                    """,
                    (account_type, username),
                )
                row = cur.fetchone()
                if row:
                    return ResolveResult(
                        user_id=row[0],
                        match_type="alias",
                        match_value=username,
                        confidence=0.8,  # 别名匹配置信度较低
                        account_verified=row[1] if row[1] is not None else False,
                    )
        except psycopg.Error as e:
            logger.warning(f"解析别名失败 ({account_type}/{username}): {e}")
        finally:
            conn.close()

        return ResolveResult()

    def _resolve_by_display(self, display: str) -> ResolveResult:
        """通过显示名称解析"""
        conn = get_connection(config=self._config)
        try:
            with conn.cursor() as cur:
                # 精确匹配 display_name
                cur.execute(
                    """
                    SELECT user_id
                    FROM identity.users
                    WHERE LOWER(display_name) = LOWER(%s)
                    LIMIT 1
                    """,
                    (display,),
                )
                row = cur.fetchone()
                if row:
                    return ResolveResult(
                        user_id=row[0],
                        match_type="display",
                        match_value=display,
                        confidence=0.7,  # 显示名称匹配置信度最低
                        account_verified=False,
                    )
        except psycopg.Error as e:
            logger.warning(f"解析显示名称失败 ({display}): {e}")
        finally:
            conn.close()

        return ResolveResult()


# 模块级默认解析器实例
_default_resolver: Optional[IdentityResolver] = None


def get_resolver(config: Optional[Config] = None) -> IdentityResolver:
    """
    获取解析器实例

    Args:
        config: 配置实例

    Returns:
        IdentityResolver 实例
    """
    global _default_resolver
    if _default_resolver is None:
        _default_resolver = IdentityResolver(config=config)
    return _default_resolver


def resolve_user_id(
    account_type: str,
    username: Optional[str] = None,
    email: Optional[str] = None,
    display: Optional[str] = None,
    config: Optional[Config] = None,
) -> ResolveResult:
    """
    解析用户 ID

    便捷函数，使用默认解析器解析用户身份。

    Args:
        account_type: 账户类型 (svn, gitlab, git, email)
        username: 用户名
        email: 邮箱地址
        display: 显示名称
        config: 配置实例

    Returns:
        ResolveResult 解析结果

    示例:
        # SVN 用户解析
        result = resolve_user_id("svn", username="zhangsan")

        # GitLab 用户解析（包含邮箱）
        result = resolve_user_id("gitlab", username="zhangsan", email="zhangsan@example.com")

        # 检查解析结果
        if result.resolved:
            print(f"解析成功: user_id={result.user_id}, 匹配方式={result.match_type}")
        else:
            print("未能解析用户身份")
    """
    resolver = get_resolver(config)
    return resolver.resolve(account_type, username, email, display)


def resolve_and_enrich_meta(
    meta_json: Dict[str, Any],
    account_type: str,
    username: Optional[str] = None,
    email: Optional[str] = None,
    display: Optional[str] = None,
    config: Optional[Config] = None,
) -> Dict[str, Any]:
    """
    解析用户身份并将结果填充到 meta_json

    Args:
        meta_json: 原始 meta_json 字典
        account_type: 账户类型
        username: 用户名
        email: 邮箱地址
        display: 显示名称
        config: 配置实例

    Returns:
        更新后的 meta_json（添加了 identity_resolved 字段）

    示例:
        meta_json = {"changed_paths": [...]}
        meta_json = resolve_and_enrich_meta(
            meta_json,
            "svn",
            username="zhangsan",
        )
        # meta_json 现在包含:
        # {
        #     "changed_paths": [...],
        #     "identity_resolved": {
        #         "resolved_user_id": "user123",
        #         "match_type": "username",
        #         "match_value": "zhangsan",
        #         "confidence": 1.0,
        #         "account_verified": true
        #     }
        # }
    """
    result = resolve_user_id(account_type, username, email, display, config)

    if result.resolved:
        meta_json["identity_resolved"] = result.to_dict()
    else:
        # 记录未解析的原始信息，便于后续处理
        meta_json["identity_resolved"] = {
            "resolved_user_id": None,
            "match_attempted": {
                "account_type": account_type,
                "username": username,
                "email": email,
                "display": display,
            },
        }

    return meta_json


def reset_resolver() -> None:
    """
    重置默认解析器

    用于测试或需要刷新缓存时
    """
    global _default_resolver
    if _default_resolver:
        _default_resolver.clear_cache()
    _default_resolver = None
