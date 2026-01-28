"""
Gateway 配置管理模块

从环境变量读取配置，并进行必填项校验。
配置项参考: templates/gateway.env.example
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


class ConfigError(Exception):
    """配置错误异常"""
    pass


@dataclass
class GatewayConfig:
    """Gateway 配置"""
    
    # 必填项
    project_key: str
    postgres_dsn: str
    openmemory_base_url: str
    
    # 可选项
    openmemory_api_key: Optional[str] = None
    gateway_port: int = 8787
    default_team_space: Optional[str] = None  # 未设置时根据 project_key 生成
    private_space_prefix: str = "private:"
    
    # Step1 DB 迁移相关配置
    auto_migrate_on_startup: bool = False  # 启动时如检测到 DB 缺失是否自动执行迁移
    step1_check_on_startup: bool = True    # 启动时是否检查 Step1 DB 结构
    
    # 治理管理相关配置
    governance_admin_key: Optional[str] = None  # 治理管理密钥（用于更新 settings）
    
    # 未知用户处理策略配置
    # reject: 拒绝请求
    # degrade: 降级到 private:unknown 空间
    # auto_create: 自动创建用户
    unknown_actor_policy: str = "degrade"  # 默认降级策略
    
    def __post_init__(self):
        """初始化后处理"""
        # 如果未设置 default_team_space，使用 project_key 生成
        if not self.default_team_space:
            self.default_team_space = f"team:{self.project_key}"
        
        # 清理 openmemory_base_url 末尾斜杠
        self.openmemory_base_url = self.openmemory_base_url.rstrip("/")


def _get_required_env(name: str, description: str = "") -> str:
    """获取必填环境变量，不存在时抛出异常"""
    value = os.environ.get(name)
    if not value:
        msg = f"缺少必填环境变量: {name}"
        if description:
            msg += f" ({description})"
        raise ConfigError(msg)
    return value


def _get_optional_env(name: str, default: str = "") -> str:
    """获取可选环境变量"""
    return os.environ.get(name, default)


def load_config() -> GatewayConfig:
    """
    从环境变量加载配置
    
    必填环境变量:
    - PROJECT_KEY: 项目标识
    - POSTGRES_DSN: PostgreSQL 连接字符串
    - OPENMEMORY_BASE_URL: OpenMemory 服务地址
    
    可选环境变量:
    - OPENMEMORY_API_KEY: OpenMemory API 密钥（优先）
    - OM_API_KEY: OpenMemory API 密钥（回退，与 unified 栈保持一致）
    - GATEWAY_PORT: Gateway 服务端口（默认 8787）
    - DEFAULT_TEAM_SPACE: 默认团队空间（默认 team:<PROJECT_KEY>）
    - PRIVATE_SPACE_PREFIX: 私有空间前缀（默认 private:）
    
    Returns:
        GatewayConfig 配置对象
        
    Raises:
        ConfigError: 缺少必填环境变量
    """
    missing = []
    
    # 收集所有缺失的必填项
    project_key = os.environ.get("PROJECT_KEY")
    if not project_key:
        missing.append("PROJECT_KEY (项目标识)")
    
    postgres_dsn = os.environ.get("POSTGRES_DSN")
    if not postgres_dsn:
        missing.append("POSTGRES_DSN (PostgreSQL 连接字符串)")
    
    openmemory_base_url = os.environ.get("OPENMEMORY_BASE_URL")
    if not openmemory_base_url:
        missing.append("OPENMEMORY_BASE_URL (OpenMemory 服务地址)")
    
    # 如果有缺失项，抛出异常
    if missing:
        raise ConfigError(
            f"缺少必填环境变量:\n  - " + "\n  - ".join(missing)
        )
    
    # 解析可选项
    gateway_port_str = _get_optional_env("GATEWAY_PORT", "8787")
    try:
        gateway_port = int(gateway_port_str)
    except ValueError:
        raise ConfigError(f"GATEWAY_PORT 必须是整数，当前值: {gateway_port_str}")
    
    # 解析 Step1 DB 迁移相关配置
    auto_migrate_str = _get_optional_env("AUTO_MIGRATE_ON_STARTUP", "false").lower()
    auto_migrate_on_startup = auto_migrate_str in ("true", "1", "yes")
    
    step1_check_str = _get_optional_env("STEP1_CHECK_ON_STARTUP", "true").lower()
    step1_check_on_startup = step1_check_str in ("true", "1", "yes")
    
    # 解析未知用户处理策略
    unknown_actor_policy = _get_optional_env("UNKNOWN_ACTOR_POLICY", "degrade").lower()
    valid_policies = ("reject", "degrade", "auto_create")
    if unknown_actor_policy not in valid_policies:
        raise ConfigError(
            f"UNKNOWN_ACTOR_POLICY 值无效: {unknown_actor_policy}，"
            f"应为: {', '.join(valid_policies)}"
        )
    
    # API Key 兼容读取：OPENMEMORY_API_KEY 优先，否则回退 OM_API_KEY
    openmemory_api_key = (
        _get_optional_env("OPENMEMORY_API_KEY") or 
        _get_optional_env("OM_API_KEY") or 
        None
    )
    
    return GatewayConfig(
        project_key=project_key,
        postgres_dsn=postgres_dsn,
        openmemory_base_url=openmemory_base_url,
        openmemory_api_key=openmemory_api_key,
        gateway_port=gateway_port,
        default_team_space=_get_optional_env("DEFAULT_TEAM_SPACE") or None,
        private_space_prefix=_get_optional_env("PRIVATE_SPACE_PREFIX", "private:"),
        auto_migrate_on_startup=auto_migrate_on_startup,
        step1_check_on_startup=step1_check_on_startup,
        governance_admin_key=_get_optional_env("GOVERNANCE_ADMIN_KEY") or None,
        unknown_actor_policy=unknown_actor_policy,
    )


# 全局配置实例（延迟加载）
_config: Optional[GatewayConfig] = None


def get_config() -> GatewayConfig:
    """
    获取全局配置实例（单例模式）
    
    首次调用时加载配置，后续调用返回缓存的实例。
    
    Returns:
        GatewayConfig 配置对象
        
    Raises:
        ConfigError: 配置加载失败
    """
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reset_config() -> None:
    """重置全局配置实例（用于测试）"""
    global _config
    _config = None


def validate_config() -> bool:
    """
    验证配置是否有效
    
    Returns:
        True 如果配置有效
        
    Raises:
        ConfigError: 配置无效
    """
    config = get_config()
    
    # 验证 POSTGRES_DSN 格式
    if not config.postgres_dsn.startswith(("postgresql://", "postgres://")):
        raise ConfigError(
            f"POSTGRES_DSN 格式无效，应以 postgresql:// 或 postgres:// 开头"
        )
    
    # 验证 OPENMEMORY_BASE_URL 格式
    if not config.openmemory_base_url.startswith(("http://", "https://")):
        raise ConfigError(
            f"OPENMEMORY_BASE_URL 格式无效，应以 http:// 或 https:// 开头"
        )
    
    # 验证端口范围
    if not (1 <= config.gateway_port <= 65535):
        raise ConfigError(
            f"GATEWAY_PORT 端口范围无效: {config.gateway_port}，应在 1-65535 之间"
        )
    
    return True
