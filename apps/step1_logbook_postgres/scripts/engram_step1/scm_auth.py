#!/usr/bin/env python3
"""
scm_auth.py - SCM 认证模块

功能:
- TokenProvider 抽象基类，统一 token 获取接口
- 支持从 env/file/exec 读取 token，带缓存与最小刷新间隔
- 提供 get_token() 与 invalidate()（收到 401/403 时触发一次刷新）
- 对 token 做基本校验（非空、去空白），日志只输出 token 前后缀的 hash 或长度

使用示例:
    # 静态 token
    provider = StaticTokenProvider("glpat-xxx")
    token = provider.get_token()

    # 从环境变量读取
    provider = EnvTokenProvider("GITLAB_TOKEN")
    token = provider.get_token()

    # 从文件读取
    provider = FileTokenProvider("/path/to/.gitlab_token")
    token = provider.get_token()

    # 从命令执行获取
    provider = ExecTokenProvider("vault read -field=token secret/gitlab")
    token = provider.get_token()

    # 工厂函数（自动选择）
    provider = create_token_provider(
        token_env="GITLAB_TOKEN",
        min_refresh_interval=60,
    )
    token = provider.get_token()

    # 收到 401/403 时触发刷新
    provider.invalidate()
    token = provider.get_token()  # 会重新获取
"""

import hashlib
import logging
import os
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 默认配置
DEFAULT_MIN_REFRESH_INTERVAL = 60  # 最小刷新间隔（秒）
DEFAULT_EXEC_TIMEOUT = 10          # exec 命令超时（秒）


class TokenValidationError(Exception):
    """Token 校验错误"""
    pass


def mask_token(token: Optional[str]) -> str:
    """
    对 token 做安全遮蔽，只输出长度和前后缀 hash
    
    格式: len=N, prefix_hash=xxx, suffix_hash=yyy
    
    Args:
        token: token 字符串
        
    Returns:
        遮蔽后的字符串（用于日志）
    """
    if not token:
        return "empty"
    
    length = len(token)
    
    # 提取前后缀用于 hash
    prefix = token[:4] if len(token) >= 4 else token
    suffix = token[-4:] if len(token) >= 4 else token
    
    # 计算短 hash
    prefix_hash = hashlib.sha256(prefix.encode()).hexdigest()[:8]
    suffix_hash = hashlib.sha256(suffix.encode()).hexdigest()[:8]
    
    return f"len={length}, prefix_hash={prefix_hash}, suffix_hash={suffix_hash}"


# ============ 敏感信息脱敏 ============

import re
from typing import Dict, Any, Union

# 敏感信息正则模式
_SENSITIVE_PATTERNS = [
    # GitLab token 模式 (glpat-xxx, glptt-xxx 等)
    (re.compile(r'\b(glp[a-z]{1,2}-[A-Za-z0-9_-]{10,})\b'), '[GITLAB_TOKEN]'),
    # Bearer token (包括后面的 JWT 或其他 token 值)
    (re.compile(r'(Bearer\s+)[A-Za-z0-9_.\-=]+', re.IGNORECASE), r'\1[TOKEN]'),
    # Authorization header 值 (匹配 "Authorization: <scheme> <credentials>" 格式)
    (re.compile(r'(Authorization[:\s]+)(\S+\s+)?(\S+)', re.IGNORECASE), r'\1[REDACTED]'),
    # PRIVATE-TOKEN header 值
    (re.compile(r'(PRIVATE-TOKEN[:\s]+)[^\s,;]+', re.IGNORECASE), r'\1[REDACTED]'),
    # 通用 token/password 模式 (URL 中的 password 参数)
    (re.compile(r'(password[=:\s]+)[^\s&;,]+', re.IGNORECASE), r'\1[REDACTED]'),
    (re.compile(r'(token[=:\s]+)[^\s&;,]+', re.IGNORECASE), r'\1[REDACTED]'),
    # URL 中的用户凭证 (user:pass@host)
    (re.compile(r'(://[^:]+:)[^@]+(@)'), r'\1[REDACTED]\2'),
]

# 敏感 header 名称列表（用于 dict 脱敏）
_SENSITIVE_HEADERS = {
    'authorization',
    'private-token',
    'x-private-token',
    'x-gitlab-token',
    'cookie',
    'set-cookie',
}


def redact(text: Union[str, None]) -> str:
    """
    对文本中的敏感信息进行脱敏
    
    处理的敏感信息类型:
    - GitLab token (glpat-xxx, glptt-xxx 等)
    - Bearer token
    - Authorization header 值
    - PRIVATE-TOKEN header 值
    - URL 中的 password/token 参数
    - URL 中的用户凭证 (user:pass@host)
    
    Args:
        text: 待脱敏的文本
        
    Returns:
        脱敏后的文本，None 输入返回空字符串
        
    Example:
        >>> redact("PRIVATE-TOKEN: glpat-xxx123456789")
        'PRIVATE-TOKEN: [REDACTED]'
        >>> redact("https://user:pass123@gitlab.com/api")
        'https://user:[REDACTED]@gitlab.com/api'
    """
    if not text:
        return ""
    
    result = str(text)
    for pattern, replacement in _SENSITIVE_PATTERNS:
        result = pattern.sub(replacement, result)
    
    return result


def redact_dict(
    data: Dict[str, Any],
    sensitive_keys: Optional[set] = None,
    deep: bool = True,
) -> Dict[str, Any]:
    """
    对字典中的敏感字段进行脱敏
    
    处理的敏感字段:
    - 键名匹配敏感 header 名称的字段
    - 嵌套字典和列表中的敏感内容
    - 字符串值中的敏感信息模式
    
    Args:
        data: 待脱敏的字典
        sensitive_keys: 额外的敏感键名集合（可选）
        deep: 是否递归处理嵌套结构（默认 True）
        
    Returns:
        脱敏后的新字典（不修改原字典）
        
    Example:
        >>> redact_dict({"Authorization": "Bearer xxx", "url": "/api/v4"})
        {'Authorization': '[REDACTED]', 'url': '/api/v4'}
    """
    if not data:
        return {}
    
    # 合并敏感键
    all_sensitive_keys = _SENSITIVE_HEADERS.copy()
    if sensitive_keys:
        all_sensitive_keys.update(k.lower() for k in sensitive_keys)
    
    result = {}
    for key, value in data.items():
        key_lower = key.lower() if isinstance(key, str) else str(key).lower()
        
        # 检查键名是否敏感
        if key_lower in all_sensitive_keys:
            result[key] = "[REDACTED]"
        elif isinstance(value, dict) and deep:
            result[key] = redact_dict(value, sensitive_keys, deep)
        elif isinstance(value, list) and deep:
            result[key] = [
                redact_dict(item, sensitive_keys, deep) if isinstance(item, dict)
                else redact(item) if isinstance(item, str)
                else item
                for item in value
            ]
        elif isinstance(value, str):
            result[key] = redact(value)
        else:
            result[key] = value
    
    return result


def redact_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """
    对 HTTP headers 字典进行脱敏
    
    专门用于处理 HTTP 请求/响应头，对敏感 header 的值进行脱敏。
    
    Args:
        headers: HTTP headers 字典
        
    Returns:
        脱敏后的新字典
        
    Example:
        >>> redact_headers({"PRIVATE-TOKEN": "glpat-xxx", "Accept": "application/json"})
        {'PRIVATE-TOKEN': '[REDACTED]', 'Accept': 'application/json'}
    """
    if not headers:
        return {}
    
    result = {}
    for key, value in headers.items():
        key_lower = key.lower()
        if key_lower in _SENSITIVE_HEADERS:
            result[key] = "[REDACTED]"
        else:
            result[key] = value
    
    return result


def validate_token(token: Optional[str]) -> str:
    """
    校验 token 有效性
    
    - 非 None
    - 去除首尾空白
    - 非空
    
    Args:
        token: 原始 token 字符串
        
    Returns:
        校验后的 token（已去空白）
        
    Raises:
        TokenValidationError: token 无效
    """
    if token is None:
        raise TokenValidationError("Token 为 None")
    
    token = token.strip()
    
    if not token:
        raise TokenValidationError("Token 为空或仅含空白")
    
    return token


@dataclass
class CachedToken:
    """缓存的 token"""
    value: str
    fetched_at: float  # timestamp


class TokenProvider(ABC):
    """
    Token 提供者抽象基类
    
    所有 token 获取方式都应实现此接口，确保：
    1. get_token() 返回有效 token
    2. invalidate() 标记 token 失效，触发下次刷新
    """
    
    @abstractmethod
    def get_token(self) -> str:
        """
        获取 token
        
        Returns:
            有效的 token 字符串
            
        Raises:
            TokenValidationError: token 无效
        """
        pass
    
    @abstractmethod
    def invalidate(self) -> None:
        """
        标记 token 失效，触发一次刷新
        
        用于收到 401/403 响应时，下次 get_token() 调用会重新获取
        """
        pass


class StaticTokenProvider(TokenProvider):
    """
    静态 token 提供者
    
    直接传入 token 值，不支持刷新。
    """
    
    def __init__(self, token: str):
        """
        初始化静态 token provider
        
        Args:
            token: token 字符串
            
        Raises:
            TokenValidationError: token 无效
        """
        self._token = validate_token(token)
        logger.debug(f"StaticTokenProvider 初始化: {mask_token(self._token)}")
    
    def get_token(self) -> str:
        return self._token
    
    def invalidate(self) -> None:
        # 静态 token 无法刷新，仅记录警告
        logger.warning(
            "StaticTokenProvider: invalidate() 被调用，但静态 token 无法刷新"
        )


class EnvTokenProvider(TokenProvider):
    """
    环境变量 token 提供者
    
    从指定环境变量读取 token，带缓存和最小刷新间隔。
    """
    
    def __init__(
        self,
        env_var: str,
        min_refresh_interval: int = DEFAULT_MIN_REFRESH_INTERVAL,
    ):
        """
        初始化环境变量 token provider
        
        Args:
            env_var: 环境变量名
            min_refresh_interval: 最小刷新间隔（秒），避免频繁读取
        """
        self._env_var = env_var
        self._min_refresh_interval = min_refresh_interval
        self._cache: Optional[CachedToken] = None
        self._invalidated = False
    
    def get_token(self) -> str:
        now = time.time()
        
        # 检查缓存是否有效
        if self._cache and not self._invalidated:
            age = now - self._cache.fetched_at
            if age < self._min_refresh_interval:
                return self._cache.value
        
        # 刷新 token
        self._invalidated = False
        value = os.environ.get(self._env_var)
        token = validate_token(value)
        
        self._cache = CachedToken(value=token, fetched_at=now)
        logger.debug(
            f"EnvTokenProvider: 从 ${self._env_var} 读取 token: {mask_token(token)}"
        )
        
        return token
    
    def invalidate(self) -> None:
        logger.info(
            f"EnvTokenProvider: token 失效标记，下次获取将重新读取 ${self._env_var}"
        )
        self._invalidated = True


class FileTokenProvider(TokenProvider):
    """
    文件 token 提供者
    
    从指定文件读取 token，带缓存和最小刷新间隔。
    文件内容应为单行 token，会自动去除首尾空白。
    """
    
    def __init__(
        self,
        file_path: str,
        min_refresh_interval: int = DEFAULT_MIN_REFRESH_INTERVAL,
    ):
        """
        初始化文件 token provider
        
        Args:
            file_path: token 文件路径
            min_refresh_interval: 最小刷新间隔（秒）
        """
        self._file_path = Path(file_path)
        self._min_refresh_interval = min_refresh_interval
        self._cache: Optional[CachedToken] = None
        self._invalidated = False
    
    def get_token(self) -> str:
        now = time.time()
        
        # 检查缓存是否有效
        if self._cache and not self._invalidated:
            age = now - self._cache.fetched_at
            if age < self._min_refresh_interval:
                return self._cache.value
        
        # 刷新 token
        self._invalidated = False
        
        if not self._file_path.exists():
            raise TokenValidationError(f"Token 文件不存在: {self._file_path}")
        
        content = self._file_path.read_text(encoding="utf-8")
        token = validate_token(content)
        
        self._cache = CachedToken(value=token, fetched_at=now)
        logger.debug(
            f"FileTokenProvider: 从 {self._file_path} 读取 token: {mask_token(token)}"
        )
        
        return token
    
    def invalidate(self) -> None:
        logger.info(
            f"FileTokenProvider: token 失效标记，下次获取将重新读取 {self._file_path}"
        )
        self._invalidated = True


class ExecTokenProvider(TokenProvider):
    """
    命令执行 token 提供者
    
    通过执行命令获取 token，带缓存和最小刷新间隔。
    命令的标准输出应为 token，会自动去除首尾空白。
    
    适用于从 vault、secret manager 等动态获取 token 的场景。
    """
    
    def __init__(
        self,
        command: str,
        min_refresh_interval: int = DEFAULT_MIN_REFRESH_INTERVAL,
        timeout: int = DEFAULT_EXEC_TIMEOUT,
    ):
        """
        初始化命令执行 token provider
        
        Args:
            command: 获取 token 的命令（会通过 shell 执行）
            min_refresh_interval: 最小刷新间隔（秒）
            timeout: 命令执行超时（秒）
        """
        self._command = command
        self._min_refresh_interval = min_refresh_interval
        self._timeout = timeout
        self._cache: Optional[CachedToken] = None
        self._invalidated = False
    
    def get_token(self) -> str:
        now = time.time()
        
        # 检查缓存是否有效
        if self._cache and not self._invalidated:
            age = now - self._cache.fetched_at
            if age < self._min_refresh_interval:
                return self._cache.value
        
        # 刷新 token
        self._invalidated = False
        
        try:
            result = subprocess.run(
                self._command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
            
            if result.returncode != 0:
                raise TokenValidationError(
                    f"Token 命令执行失败 (exit={result.returncode}): {result.stderr}"
                )
            
            token = validate_token(result.stdout)
            
            self._cache = CachedToken(value=token, fetched_at=now)
            logger.debug(
                f"ExecTokenProvider: 从命令获取 token: {mask_token(token)}"
            )
            
            return token
            
        except subprocess.TimeoutExpired:
            raise TokenValidationError(
                f"Token 命令超时 ({self._timeout}s): {self._command}"
            )
    
    def invalidate(self) -> None:
        logger.info(
            "ExecTokenProvider: token 失效标记，下次获取将重新执行命令"
        )
        self._invalidated = True


def create_token_provider(
    token: Optional[str] = None,
    token_env: Optional[str] = None,
    token_file: Optional[str] = None,
    token_exec: Optional[str] = None,
    min_refresh_interval: int = DEFAULT_MIN_REFRESH_INTERVAL,
) -> TokenProvider:
    """
    创建 token provider 工厂函数
    
    根据传入参数自动选择合适的 provider 类型。
    
    优先级: token > token_env > token_file > token_exec > 默认(GITLAB_TOKEN)
    
    Args:
        token: 静态 token 值
        token_env: 环境变量名
        token_file: token 文件路径
        token_exec: 获取 token 的命令
        min_refresh_interval: 最小刷新间隔（秒）
        
    Returns:
        TokenProvider 实例
        
    Raises:
        TokenValidationError: 无法创建有效的 provider
    """
    if token:
        return StaticTokenProvider(token)
    
    if token_env:
        return EnvTokenProvider(token_env, min_refresh_interval)
    
    if token_file:
        return FileTokenProvider(token_file, min_refresh_interval)
    
    if token_exec:
        return ExecTokenProvider(token_exec, min_refresh_interval)
    
    # 默认尝试 GITLAB_TOKEN 环境变量，如果不存在则回退到 GITLAB_PRIVATE_TOKEN
    if os.environ.get("GITLAB_TOKEN"):
        logger.debug("未指定 token 来源，默认使用环境变量 GITLAB_TOKEN")
        return EnvTokenProvider("GITLAB_TOKEN", min_refresh_interval)
    elif os.environ.get("GITLAB_PRIVATE_TOKEN"):
        logger.debug("未指定 token 来源，回退到环境变量 GITLAB_PRIVATE_TOKEN")
        return EnvTokenProvider("GITLAB_PRIVATE_TOKEN", min_refresh_interval)
    else:
        # 返回 GITLAB_TOKEN provider（get_token 时会抛出 TokenValidationError）
        logger.debug("未指定 token 来源，默认使用环境变量 GITLAB_TOKEN")
        return EnvTokenProvider("GITLAB_TOKEN", min_refresh_interval)


def create_gitlab_token_provider(
    config=None,
    private_token: Optional[str] = None,
) -> TokenProvider:
    """
    创建 GitLab token provider（便捷函数）
    
    读取配置优先级:
    1. 直接传入的 private_token
    2. 配置文件 scm.gitlab.token / gitlab.private_token
    3. 配置文件 scm.gitlab.token_env / gitlab.token_env
    4. 配置文件 scm.gitlab.token_file / gitlab.token_file
    5. 配置文件 scm.gitlab.token_exec / gitlab.token_exec
    6. 环境变量 GITLAB_TOKEN
    
    Args:
        config: Config 实例
        private_token: 直接传入的 token（覆盖配置）
        
    Returns:
        TokenProvider 实例
    """
    # 直接传入的 token 优先
    if private_token:
        return StaticTokenProvider(private_token)
    
    # 从配置读取
    if config:
        # 优先读取新键 scm.gitlab.*，回退旧键 gitlab.*
        token = config.get("scm.gitlab.token") or config.get("gitlab.private_token")
        if token:
            return StaticTokenProvider(token)
        
        token_env = config.get("scm.gitlab.token_env") or config.get("gitlab.token_env")
        if token_env:
            min_interval = config.get("scm.gitlab.token_refresh_interval", DEFAULT_MIN_REFRESH_INTERVAL)
            return EnvTokenProvider(token_env, min_interval)
        
        token_file = config.get("scm.gitlab.token_file") or config.get("gitlab.token_file")
        if token_file:
            min_interval = config.get("scm.gitlab.token_refresh_interval", DEFAULT_MIN_REFRESH_INTERVAL)
            return FileTokenProvider(token_file, min_interval)
        
        token_exec = config.get("scm.gitlab.token_exec") or config.get("gitlab.token_exec")
        if token_exec:
            min_interval = config.get("scm.gitlab.token_refresh_interval", DEFAULT_MIN_REFRESH_INTERVAL)
            timeout = config.get("scm.gitlab.token_exec_timeout", DEFAULT_EXEC_TIMEOUT)
            return ExecTokenProvider(token_exec, min_interval, timeout)
    
    # 默认使用环境变量 GITLAB_TOKEN，如果不存在则回退到 GITLAB_PRIVATE_TOKEN
    if os.environ.get("GITLAB_TOKEN"):
        return EnvTokenProvider("GITLAB_TOKEN", DEFAULT_MIN_REFRESH_INTERVAL)
    elif os.environ.get("GITLAB_PRIVATE_TOKEN"):
        logger.debug("GITLAB_TOKEN 未设置，回退到 GITLAB_PRIVATE_TOKEN")
        return EnvTokenProvider("GITLAB_PRIVATE_TOKEN", DEFAULT_MIN_REFRESH_INTERVAL)
    else:
        # 返回 GITLAB_TOKEN provider（get_token 时会抛出 TokenValidationError）
        return EnvTokenProvider("GITLAB_TOKEN", DEFAULT_MIN_REFRESH_INTERVAL)


def _normalize_instance_key_for_config(instance_key: str) -> str:
    """
    规范化 instance_key（用于配置键查找）
    
    将点号、冒号等特殊字符替换为下划线，便于作为配置键使用。
    
    Args:
        instance_key: 原始 instance key (如 gitlab.example.com:8080)
        
    Returns:
        规范化的 key (如 gitlab_example_com_8080)
        
    Note:
        此函数是内部使用的配置键规范化函数。
        对于外部使用，请使用 engram_step1.scm_sync_keys.normalize_instance_key
    """
    if not instance_key:
        return ""
    # 替换常见分隔符为下划线
    result = instance_key.replace(".", "_").replace(":", "_").replace("-", "_")
    # 移除多余的下划线
    while "__" in result:
        result = result.replace("__", "_")
    return result.strip("_").lower()


# 重新导出 normalize_instance_key 以保持 API 兼容性
def normalize_instance_key(instance_key: str) -> str:
    """
    规范化 instance_key（用于配置键查找）
    
    将点号、冒号等特殊字符替换为下划线，便于作为配置键使用。
    
    Args:
        instance_key: 原始 instance key (如 gitlab.example.com:8080)
        
    Returns:
        规范化的 key (如 gitlab_example_com_8080)
    """
    return _normalize_instance_key_for_config(instance_key)


def create_token_provider_for_instance(
    instance_key: Optional[str] = None,
    tenant_id: Optional[str] = None,
    payload_token: Optional[str] = None,
    config=None,
) -> TokenProvider:
    """
    按 instance_key/tenant_id 选择 token 的工厂方法
    
    优先级: payload 指定 > config 映射 > env 默认
    
    Config 映射格式示例:
        # 按 GitLab 实例配置 token
        [scm.gitlab.instances.gitlab_example_com]
        token = "glpat-xxx"
        # 或使用环境变量
        token_env = "GITLAB_EXAMPLE_TOKEN"
        # 或使用文件
        token_file = "/path/to/token"
        
        # 按租户配置 token
        [scm.gitlab.tenants.tenant1]
        token = "glpat-yyy"
        token_env = "TENANT1_GITLAB_TOKEN"
    
    Args:
        instance_key: GitLab 实例的 key (如 gitlab.example.com)
        tenant_id: 租户 ID
        payload_token: payload 中直接指定的 token（优先级最高）
        config: Config 实例
        
    Returns:
        TokenProvider 实例
        
    Example:
        # 优先使用 payload token
        provider = create_token_provider_for_instance(
            instance_key="gitlab.example.com",
            payload_token="glpat-xxx",  # 最高优先级
        )
        
        # 从配置读取特定实例的 token
        provider = create_token_provider_for_instance(
            instance_key="gitlab.example.com",
            config=cfg,
        )
        
        # 从配置读取特定租户的 token
        provider = create_token_provider_for_instance(
            tenant_id="tenant-a",
            config=cfg,
        )
    """
    # 1. 优先使用 payload 指定的 token
    if payload_token:
        logger.debug(f"使用 payload 指定的 token: {mask_token(payload_token)}")
        return StaticTokenProvider(payload_token)
    
    # 2. 从配置中按 instance_key 或 tenant_id 查找
    if config:
        # 2a. 按 instance_key 查找
        if instance_key:
            normalized_key = _normalize_instance_key_for_config(instance_key)
            # 尝试多种配置键格式
            config_prefixes = [
                f"scm.gitlab.instances.{normalized_key}",
                f"scm.gitlab.instances.{instance_key}",  # 原始 key
            ]
            
            for prefix in config_prefixes:
                provider = _try_create_provider_from_config(config, prefix)
                if provider:
                    logger.debug(f"使用配置 [{prefix}] 的 token")
                    return provider
        
        # 2b. 按 tenant_id 查找
        if tenant_id:
            prefix = f"scm.gitlab.tenants.{tenant_id}"
            provider = _try_create_provider_from_config(config, prefix)
            if provider:
                logger.debug(f"使用配置 [{prefix}] 的 token")
                return provider
    
    # 3. 回退到默认的 create_gitlab_token_provider
    logger.debug(
        f"未找到 instance_key={instance_key} / tenant_id={tenant_id} 的专用配置，"
        "使用默认 token provider"
    )
    return create_gitlab_token_provider(config)


def _try_create_provider_from_config(config, prefix: str) -> Optional[TokenProvider]:
    """
    尝试从配置前缀创建 TokenProvider
    
    Args:
        config: Config 实例
        prefix: 配置键前缀 (如 scm.gitlab.instances.gitlab_example_com)
        
    Returns:
        TokenProvider 实例，如果配置不存在则返回 None
    """
    # 检查是否有该配置块
    token = config.get(f"{prefix}.token")
    if token:
        return StaticTokenProvider(token)
    
    token_env = config.get(f"{prefix}.token_env")
    if token_env:
        min_interval = config.get(f"{prefix}.token_refresh_interval", DEFAULT_MIN_REFRESH_INTERVAL)
        return EnvTokenProvider(token_env, min_interval)
    
    token_file = config.get(f"{prefix}.token_file")
    if token_file:
        min_interval = config.get(f"{prefix}.token_refresh_interval", DEFAULT_MIN_REFRESH_INTERVAL)
        return FileTokenProvider(token_file, min_interval)
    
    token_exec = config.get(f"{prefix}.token_exec")
    if token_exec:
        min_interval = config.get(f"{prefix}.token_refresh_interval", DEFAULT_MIN_REFRESH_INTERVAL)
        timeout = config.get(f"{prefix}.token_exec_timeout", DEFAULT_EXEC_TIMEOUT)
        return ExecTokenProvider(token_exec, min_interval, timeout)
    
    return None


class GitLabAuthenticatedSession:
    """
    GitLab 认证会话封装
    
    封装 requests.Session，自动处理 token 头和 401/403 重试。
    """
    
    def __init__(
        self,
        token_provider: TokenProvider,
        max_auth_retries: int = 1,
    ):
        """
        初始化认证会话
        
        Args:
            token_provider: token 提供者
            max_auth_retries: 认证失败时最大重试次数
        """
        import requests
        
        self._token_provider = token_provider
        self._max_auth_retries = max_auth_retries
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/json",
        })
    
    def _update_auth_header(self) -> None:
        """更新认证头"""
        token = self._token_provider.get_token()
        self._session.headers["PRIVATE-TOKEN"] = token
    
    def request(
        self,
        method: str,
        url: str,
        **kwargs,
    ):
        """
        发送 HTTP 请求
        
        自动处理 401/403 响应，触发 token 刷新并重试。
        
        Args:
            method: HTTP 方法
            url: 请求 URL
            **kwargs: requests 参数
            
        Returns:
            requests.Response
        """
        import requests
        
        # 确保有认证头
        self._update_auth_header()
        
        retries = 0
        while True:
            response = self._session.request(method, url, **kwargs)
            
            # 检查是否需要刷新 token
            if response.status_code in (401, 403):
                if retries < self._max_auth_retries:
                    logger.warning(
                        f"收到 {response.status_code} 响应，触发 token 刷新并重试"
                    )
                    self._token_provider.invalidate()
                    self._update_auth_header()
                    retries += 1
                    continue
            
            return response
    
    def get(self, url: str, **kwargs):
        """GET 请求"""
        return self.request("GET", url, **kwargs)
    
    def post(self, url: str, **kwargs):
        """POST 请求"""
        return self.request("POST", url, **kwargs)
    
    def put(self, url: str, **kwargs):
        """PUT 请求"""
        return self.request("PUT", url, **kwargs)
    
    def delete(self, url: str, **kwargs):
        """DELETE 请求"""
        return self.request("DELETE", url, **kwargs)
    
    def close(self) -> None:
        """关闭会话"""
        self._session.close()
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
