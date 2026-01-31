"""
Gateway 配置管理模块

从环境变量读取配置，并进行必填项校验。
配置项参考: templates/gateway.env.example

线程安全与可测试性说明 (ADR: Gateway DI 与入口边界统一):
============================================================

1. 线程安全性 (Thread Safety):
   - GatewayConfig 实例: 线程安全（不可变 dataclass，初始化后不修改）
   - _config 全局单例: 使用模块级变量，依赖 Python GIL 基本安全
   - 高并发场景下首次初始化可能出现竞态，但结果一致（幂等）

2. 可重入性 (Reentrancy):
   - get_config(): 幂等操作，多次调用返回同一实例
   - load_config(): 每次调用都会重新读取环境变量，创建新实例
   - reset_config(): 非幂等，会清除全局单例

3. 可测试替换 (Test Override):
   - 方式一: 使用 override_config(mock_config) 临时替换全局单例
   - 方式二: 测试完成后调用 reset_config() 恢复默认行为
   - 方式三: 在测试中设置环境变量，然后调用 reset_config() + get_config()
   - 方式四: 直接构造 GatewayConfig 实例传入 GatewayDeps.for_testing()

构造参数来源说明 (环境变量):
   必填:
   - PROJECT_KEY: 项目标识
   - POSTGRES_DSN: PostgreSQL 连接字符串

   可选:
   - OPENMEMORY_BASE_URL: OpenMemory 服务地址（默认 http://localhost:8080）
   - OPENMEMORY_API_KEY / OM_API_KEY: API 密钥
   - GATEWAY_PORT: 服务端口（默认 8787）
   - AUTO_MIGRATE_ON_STARTUP: 启动时自动迁移（默认 false）
   - UNKNOWN_ACTOR_POLICY: 未知用户处理策略（reject/degrade/auto_create，默认 degrade）
   - 更多配置项参见 load_config() 函数
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
    """
    Gateway 配置

    线程安全: 是（dataclass 实例初始化后不修改，可安全跨线程共享）
    可重入: 是（只读访问）

    注意: __post_init__ 会修改 default_team_space 和 openmemory_base_url，
         但这发生在构造阶段，构造完成后实例是有效不可变的。
    """

    # 必填项（允许无参构造以便测试读取 env）
    project_key: str = field(default_factory=lambda: os.environ.get("PROJECT_KEY", ""))
    postgres_dsn: str = field(default_factory=lambda: os.environ.get("POSTGRES_DSN", ""))
    openmemory_base_url: str = field(
        default_factory=lambda: os.environ.get("OPENMEMORY_BASE_URL", "http://localhost:8080")
    )

    # 可选项
    openmemory_api_key: Optional[str] = None
    gateway_port: int = 8787
    default_team_space: Optional[str] = None  # 未设置时根据 project_key 生成
    private_space_prefix: str = "private:"

    # Logbook DB 迁移相关配置
    auto_migrate_on_startup: bool = False  # 启动时如检测到 DB 缺失是否自动执行迁移
    logbook_check_on_startup: bool = True  # 启动时是否检查 Logbook DB 结构

    # 治理管理相关配置
    governance_admin_key: Optional[str] = None  # 治理管理密钥（用于更新 settings）

    # 未知用户处理策略配置
    # reject: 拒绝请求
    # degrade: 降级到 private:unknown 空间
    # auto_create: 自动创建用户
    unknown_actor_policy: str = "degrade"  # 默认降级策略

    # MinIO Audit Webhook 配置
    minio_audit_webhook_auth_token: Optional[str] = None  # MinIO audit webhook 认证 Token
    minio_audit_max_payload_size: int = 1024 * 1024  # 最大 payload 大小（默认 1MB）

    # Evidence Refs 校验配置
    validate_evidence_refs: bool = False  # 是否校验 evidence refs 结构（默认 False，向后兼容）
    strict_mode_enforce_validate_refs: bool = True  # strict 模式下是否强制启用校验（默认 True）

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
        # 兼容无 OpenMemory 环境（默认占位）
        openmemory_base_url = "http://localhost:8080"

    # 如果有缺失项，抛出异常
    if missing:
        raise ConfigError("缺少必填环境变量:\n  - " + "\n  - ".join(missing))

    # 类型断言（前面已经校验过，不会为 None）
    assert project_key is not None
    assert postgres_dsn is not None

    # 解析可选项
    gateway_port_str = _get_optional_env("GATEWAY_PORT", "8787")
    try:
        gateway_port = int(gateway_port_str)
    except ValueError:
        raise ConfigError(f"GATEWAY_PORT 必须是整数，当前值: {gateway_port_str}")

    # 解析 Logbook DB 迁移相关配置
    auto_migrate_str = _get_optional_env("AUTO_MIGRATE_ON_STARTUP", "false").lower()
    auto_migrate_on_startup = auto_migrate_str in ("true", "1", "yes")

    logbook_check_str = _get_optional_env("LOGBOOK_CHECK_ON_STARTUP", "true").lower()
    logbook_check_on_startup = logbook_check_str in ("true", "1", "yes")

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
        _get_optional_env("OPENMEMORY_API_KEY") or _get_optional_env("OM_API_KEY") or None
    )

    # 解析 MinIO Audit Webhook 配置
    minio_audit_max_payload_size_str = _get_optional_env("MINIO_AUDIT_MAX_PAYLOAD_SIZE", "1048576")
    try:
        minio_audit_max_payload_size = int(minio_audit_max_payload_size_str)
    except ValueError:
        raise ConfigError(
            f"MINIO_AUDIT_MAX_PAYLOAD_SIZE 必须是整数，当前值: {minio_audit_max_payload_size_str}"
        )

    # 解析 Evidence Refs 校验配置
    validate_evidence_refs_str = _get_optional_env("VALIDATE_EVIDENCE_REFS", "false").lower()
    validate_evidence_refs = validate_evidence_refs_str in ("true", "1", "yes")

    strict_mode_enforce_str = _get_optional_env("STRICT_MODE_ENFORCE_VALIDATE_REFS", "true").lower()
    strict_mode_enforce_validate_refs = strict_mode_enforce_str in ("true", "1", "yes")

    return GatewayConfig(
        project_key=project_key,
        postgres_dsn=postgres_dsn,
        openmemory_base_url=openmemory_base_url,
        openmemory_api_key=openmemory_api_key,
        gateway_port=gateway_port,
        default_team_space=_get_optional_env("DEFAULT_TEAM_SPACE") or None,
        private_space_prefix=_get_optional_env("PRIVATE_SPACE_PREFIX", "private:"),
        auto_migrate_on_startup=auto_migrate_on_startup,
        logbook_check_on_startup=logbook_check_on_startup,
        governance_admin_key=_get_optional_env("GOVERNANCE_ADMIN_KEY") or None,
        unknown_actor_policy=unknown_actor_policy,
        minio_audit_webhook_auth_token=_get_optional_env("MINIO_AUDIT_WEBHOOK_AUTH_TOKEN") or None,
        minio_audit_max_payload_size=minio_audit_max_payload_size,
        validate_evidence_refs=validate_evidence_refs,
        strict_mode_enforce_validate_refs=strict_mode_enforce_validate_refs,
    )


# 全局配置实例（延迟加载）
_config: Optional[GatewayConfig] = None


def get_config() -> GatewayConfig:
    """
    获取全局配置实例（单例模式）

    线程安全: 基本安全（依赖 Python GIL，高并发下可能多次初始化但结果一致）
    可重入: 是（幂等操作）

    首次调用时从环境变量加载配置，后续调用返回缓存的实例。

    Returns:
        GatewayConfig 配置对象

    Raises:
        ConfigError: 配置加载失败（缺少必填环境变量等）
    """
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reset_config() -> None:
    """
    重置全局配置实例（用于测试）

    线程安全: 否（建议在单线程环境下调用，如测试 setup/teardown）

    调用后下次 get_config() 将重新从环境变量加载。
    """
    global _config
    _config = None


def override_config(config: GatewayConfig) -> None:
    """
    覆盖全局配置实例（测试专用）

    线程安全: 否（建议在单线程环境下调用，如测试 setup）

    允许测试代码注入自定义配置，替换全局单例。
    测试完成后应调用 reset_config() 恢复默认行为。

    Args:
        config: 要设置的 GatewayConfig 实例

    Usage:
        # 在测试 setup 中
        test_config = GatewayConfig(
            project_key="test-project",
            postgres_dsn="postgresql://test@localhost/test",
            openmemory_base_url="http://mock:8080",
        )
        override_config(test_config)

        # 测试代码...

        # 在测试 teardown 中
        reset_config()
    """
    global _config
    _config = config


def get_config_or_none() -> Optional[GatewayConfig]:
    """
    获取全局配置实例（如果已初始化）

    线程安全: 是（只读操作）

    用于检查全局单例状态，不触发延迟初始化。

    Returns:
        已初始化的 GatewayConfig 实例，或 None
    """
    return _config


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
        raise ConfigError("POSTGRES_DSN 格式无效，应以 postgresql:// 或 postgres:// 开头")

    # 验证 OPENMEMORY_BASE_URL 格式
    if not config.openmemory_base_url.startswith(("http://", "https://")):
        raise ConfigError("OPENMEMORY_BASE_URL 格式无效，应以 http:// 或 https:// 开头")

    # 验证端口范围
    if not (1 <= config.gateway_port <= 65535):
        raise ConfigError(f"GATEWAY_PORT 端口范围无效: {config.gateway_port}，应在 1-65535 之间")

    return True


# ===================== ValidateRefs 决策相关 =====================


@dataclass
class ValidateRefsDecision:
    """
    ValidateRefs 决策结果

    用于表示 evidence_refs 校验开关的最终决策及原因。
    支持可追溯性：reason 字段说明决策依据。
    """

    effective: bool  # 最终生效的值
    reason: str  # 决策原因（用于日志和调试）

    def to_dict(self) -> dict:
        """转换为字典格式，用于响应或日志"""
        return {
            "validate_refs_effective": self.effective,
            "validate_refs_reason": self.reason,
        }


def resolve_validate_refs(
    mode: Optional[str] = None,
    config: Optional[GatewayConfig] = None,
    caller_override: Optional[bool] = None,
) -> ValidateRefsDecision:
    """
    解析 validate_refs 的最终有效值

    决策逻辑:
    - strict 模式 + enforce=True: 强制 True，忽略所有 override
    - strict 模式 + enforce=False: 允许环境变量或调用方 override
    - compat 模式: 使用环境变量配置，允许调用方 override

    Args:
        mode: 模式 ("strict" / "compat" / None)，None 默认为 compat，不区分大小写
        config: 配置对象，None 时使用全局配置
        caller_override: 调用方显式指定的值，None 表示使用配置默认

    Returns:
        ValidateRefsDecision: 包含 effective 和 reason
    """
    # 获取配置
    if config is None:
        config = get_config()

    # 标准化 mode
    mode_normalized = (mode or "compat").lower()

    if mode_normalized == "strict":
        # strict 模式
        if config.strict_mode_enforce_validate_refs:
            # enforce=True: 强制启用，忽略所有 override
            return ValidateRefsDecision(
                effective=True,
                reason="strict_enforced",
            )
        else:
            # enforce=False: 允许 override
            if caller_override is not None:
                return ValidateRefsDecision(
                    effective=caller_override,
                    reason="strict_caller_override",
                )
            else:
                return ValidateRefsDecision(
                    effective=config.validate_evidence_refs,
                    reason="strict_env_override",
                )
    else:
        # compat 模式（默认）
        if caller_override is not None:
            return ValidateRefsDecision(
                effective=caller_override,
                reason="compat_caller_override",
            )
        else:
            return ValidateRefsDecision(
                effective=config.validate_evidence_refs,
                reason="compat_default",
            )
