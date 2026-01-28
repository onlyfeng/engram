"""
环境变量兼容层模块

提供统一的环境变量读取接口，支持：
- 多名称别名（canonical + legacy）
- CLI 参数优先级
- 废弃警告
- 冲突检测

优先级（从高到低）：
1. CLI 显式参数（由调用者传入）
2. canonical 环境变量
3. legacy 环境变量（会触发 deprecation warning）
4. 计算/回退值（如从 PG* 组合）
5. 默认值

使用示例:
    from env_compat import get_str, get_int, get_bool, get_choice
    
    # 基本使用
    dsn = get_str("STEP3_PGVECTOR_DSN", default="")
    
    # 带 legacy 别名
    host = get_str(
        "PGHOST",
        deprecated_aliases=["PG_HOST", "POSTGRES_HOST"],
        default="localhost"
    )
    
    # 带 CLI 覆盖
    port = get_int("PGPORT", cli_value=args.port, default=5432)
    
    # 带值别名（用于 bool 转换等）
    enabled = get_bool(
        "STEP3_DUAL_WRITE",
        deprecated_aliases=["DUAL_WRITE_ENABLED"],
        default=False
    )
    
    # 带选项限制
    strategy = get_choice(
        "STEP3_STRATEGY",
        choices=["single_table", "per_table", "routing"],
        default="per_table"
    )
"""

from __future__ import annotations

import os
import sys
import warnings
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set, TypeVar, Union

__all__ = [
    "get_str",
    "get_int",
    "get_bool",
    "get_float",
    "get_choice",
    "get_list",
    "EnvConflictError",
    "set_allow_conflict",
    "reset_deprecation_warnings",
    "check_env_conflicts",
]

T = TypeVar("T")

# ============ 全局状态 ============

# 已警告的废弃变量名，避免重复打印
_warned_deprecated: Set[str] = set()

# 已警告的废弃值，避免重复打印（用于 get_choice 的 deprecated_value_aliases）
_warned_deprecated_values: Set[str] = set()

# 是否允许冲突（仅 warn 而非报错），可通过环境变量或函数设置
_allow_conflict: Optional[bool] = None


def _get_allow_conflict() -> bool:
    """获取是否允许冲突的设置"""
    global _allow_conflict
    if _allow_conflict is not None:
        return _allow_conflict
    # 从环境变量读取，默认不允许（报错）
    return os.environ.get("STEP3_ENV_ALLOW_CONFLICT", "0").lower() in ("1", "true", "yes")


def set_allow_conflict(allow: bool) -> None:
    """
    设置是否允许 canonical 与 legacy 环境变量冲突
    
    Args:
        allow: True 表示冲突时仅警告，False 表示冲突时报错
    """
    global _allow_conflict
    _allow_conflict = allow


def reset_deprecation_warnings() -> None:
    """重置已警告的废弃变量/值集合（主要用于测试）"""
    global _warned_deprecated, _warned_deprecated_values
    _warned_deprecated = set()
    _warned_deprecated_values = set()


# ============ 异常 ============

class EnvConflictError(ValueError):
    """当 canonical 和 legacy 环境变量同时设置且值冲突时抛出"""
    
    def __init__(
        self,
        canonical: str,
        legacy: str,
        canonical_value: str,
        legacy_value: str,
    ):
        self.canonical = canonical
        self.legacy = legacy
        self.canonical_value = canonical_value
        self.legacy_value = legacy_value
        super().__init__(
            f"环境变量冲突: {canonical}={canonical_value!r} 与 "
            f"{legacy}={legacy_value!r} 同时设置但值不同。"
            f"请删除废弃的 {legacy} 变量，或设置 STEP3_ENV_ALLOW_CONFLICT=1 以忽略此错误。"
        )


# ============ 内部工具函数 ============

def _warn_deprecated(deprecated_name: str, canonical_name: str) -> None:
    """
    打印废弃变量警告（每个变量名只警告一次）
    
    Args:
        deprecated_name: 废弃的环境变量名
        canonical_name: 推荐使用的新名称
    """
    global _warned_deprecated
    if deprecated_name in _warned_deprecated:
        return
    _warned_deprecated.add(deprecated_name)
    
    msg = (
        f"[DEPRECATION] 环境变量 {deprecated_name} 已废弃，"
        f"请改用 {canonical_name}。此警告仅显示一次。"
    )
    # 使用 warnings 模块，便于测试时捕获
    warnings.warn(msg, DeprecationWarning, stacklevel=4)
    # 同时输出到 stderr，确保用户看到
    print(f"Warning: {msg}", file=sys.stderr)


def _check_conflict(
    canonical_name: str,
    canonical_value: str,
    deprecated_name: str,
    deprecated_value: str,
) -> None:
    """
    检查 canonical 与 deprecated 值是否冲突
    
    Args:
        canonical_name: canonical 环境变量名
        canonical_value: canonical 环境变量值
        deprecated_name: deprecated 环境变量名
        deprecated_value: deprecated 环境变量值
        
    Raises:
        EnvConflictError: 如果冲突且不允许冲突
    """
    if canonical_value == deprecated_value:
        return
    
    if _get_allow_conflict():
        # 仅警告
        msg = (
            f"[ENV_CONFLICT] {canonical_name}={canonical_value!r} 与 "
            f"{deprecated_name}={deprecated_value!r} 冲突，使用 {canonical_name} 的值。"
        )
        warnings.warn(msg, UserWarning, stacklevel=4)
        print(f"Warning: {msg}", file=sys.stderr)
    else:
        raise EnvConflictError(
            canonical=canonical_name,
            legacy=deprecated_name,
            canonical_value=canonical_value,
            legacy_value=deprecated_value,
        )


def _resolve_env(
    name: str,
    aliases: Optional[List[str]] = None,
    deprecated_aliases: Optional[List[str]] = None,
    fallback_fn: Optional[Callable[[], Optional[str]]] = None,
    cli_value: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """
    解析环境变量值，返回 (value, source)
    
    优先级：CLI > canonical > legacy > fallback
    
    Args:
        name: canonical 环境变量名
        aliases: 同等优先级的别名（视为 canonical）
        deprecated_aliases: 废弃的别名（低优先级，触发警告）
        fallback_fn: 回退计算函数（如从 PG* 组合）
        cli_value: CLI 传入的值（最高优先级）
        
    Returns:
        (value, source): value 是最终值，source 是值来源描述
    """
    # 1. CLI 显式参数优先
    if cli_value is not None:
        return (cli_value, "cli")
    
    # 2. canonical 环境变量
    canonical_names = [name] + (aliases or [])
    canonical_value = None
    canonical_source = None
    
    for n in canonical_names:
        val = os.environ.get(n)
        if val is not None:
            if canonical_value is not None and val != canonical_value:
                # 多个 canonical 别名冲突
                _check_conflict(canonical_source, canonical_value, n, val)
            else:
                canonical_value = val
                canonical_source = n
    
    # 3. legacy 环境变量
    deprecated_value = None
    deprecated_source = None
    
    for dep_name in (deprecated_aliases or []):
        val = os.environ.get(dep_name)
        if val is not None:
            if deprecated_value is not None and val != deprecated_value:
                # 多个 deprecated 别名冲突，使用第一个
                pass
            else:
                deprecated_value = val
                deprecated_source = dep_name
    
    # 检查 canonical 与 deprecated 冲突
    if canonical_value is not None and deprecated_value is not None:
        _check_conflict(canonical_source, canonical_value, deprecated_source, deprecated_value)
        # 冲突已处理（warn 模式），使用 canonical
        return (canonical_value, f"env:{canonical_source}")
    
    if canonical_value is not None:
        return (canonical_value, f"env:{canonical_source}")
    
    if deprecated_value is not None:
        # 触发废弃警告
        _warn_deprecated(deprecated_source, name)
        return (deprecated_value, f"env:{deprecated_source}(deprecated)")
    
    # 4. fallback 计算
    if fallback_fn is not None:
        fallback_val = fallback_fn()
        if fallback_val is not None:
            return (fallback_val, "fallback")
    
    return (None, None)


# ============ 公开 API ============

def get_str(
    name: str,
    *,
    aliases: Optional[List[str]] = None,
    deprecated_aliases: Optional[List[str]] = None,
    fallback_fn: Optional[Callable[[], Optional[str]]] = None,
    cli_value: Optional[str] = None,
    default: Optional[str] = None,
    required: bool = False,
) -> Optional[str]:
    """
    获取字符串类型的环境变量
    
    Args:
        name: canonical 环境变量名
        aliases: 同等优先级的别名
        deprecated_aliases: 废弃的别名（会触发警告）
        fallback_fn: 回退计算函数
        cli_value: CLI 传入的值（最高优先级）
        default: 默认值
        required: 是否必需（为 None 时报错）
        
    Returns:
        环境变量值或默认值
        
    Raises:
        ValueError: 如果 required=True 且值为 None
    """
    value, source = _resolve_env(
        name=name,
        aliases=aliases,
        deprecated_aliases=deprecated_aliases,
        fallback_fn=fallback_fn,
        cli_value=cli_value,
    )
    
    if value is None:
        value = default
    
    if required and value is None:
        raise ValueError(f"必需的环境变量 {name} 未设置")
    
    return value


def get_int(
    name: str,
    *,
    aliases: Optional[List[str]] = None,
    deprecated_aliases: Optional[List[str]] = None,
    fallback_fn: Optional[Callable[[], Optional[str]]] = None,
    cli_value: Optional[int] = None,
    default: Optional[int] = None,
    required: bool = False,
) -> Optional[int]:
    """
    获取整数类型的环境变量
    
    Args:
        name: canonical 环境变量名
        aliases: 同等优先级的别名
        deprecated_aliases: 废弃的别名（会触发警告）
        fallback_fn: 回退计算函数
        cli_value: CLI 传入的值（最高优先级）
        default: 默认值
        required: 是否必需
        
    Returns:
        整数值或默认值
        
    Raises:
        ValueError: 如果值无法转换为整数或 required=True 且值为 None
    """
    cli_str = str(cli_value) if cli_value is not None else None
    
    value, source = _resolve_env(
        name=name,
        aliases=aliases,
        deprecated_aliases=deprecated_aliases,
        fallback_fn=fallback_fn,
        cli_value=cli_str,
    )
    
    if value is None:
        if required and default is None:
            raise ValueError(f"必需的环境变量 {name} 未设置")
        return default
    
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"环境变量 {name} 的值 {value!r} 无法转换为整数")


def get_float(
    name: str,
    *,
    aliases: Optional[List[str]] = None,
    deprecated_aliases: Optional[List[str]] = None,
    fallback_fn: Optional[Callable[[], Optional[str]]] = None,
    cli_value: Optional[float] = None,
    default: Optional[float] = None,
    required: bool = False,
) -> Optional[float]:
    """
    获取浮点数类型的环境变量
    
    Args:
        name: canonical 环境变量名
        aliases: 同等优先级的别名
        deprecated_aliases: 废弃的别名（会触发警告）
        fallback_fn: 回退计算函数
        cli_value: CLI 传入的值（最高优先级）
        default: 默认值
        required: 是否必需
        
    Returns:
        浮点数值或默认值
        
    Raises:
        ValueError: 如果值无法转换为浮点数或 required=True 且值为 None
    """
    cli_str = str(cli_value) if cli_value is not None else None
    
    value, source = _resolve_env(
        name=name,
        aliases=aliases,
        deprecated_aliases=deprecated_aliases,
        fallback_fn=fallback_fn,
        cli_value=cli_str,
    )
    
    if value is None:
        if required and default is None:
            raise ValueError(f"必需的环境变量 {name} 未设置")
        return default
    
    try:
        return float(value)
    except ValueError:
        raise ValueError(f"环境变量 {name} 的值 {value!r} 无法转换为浮点数")


# 布尔值的真/假映射
_BOOL_TRUE_VALUES = frozenset(("1", "true", "yes", "on", "enabled"))
_BOOL_FALSE_VALUES = frozenset(("0", "false", "no", "off", "disabled", ""))


def get_bool(
    name: str,
    *,
    aliases: Optional[List[str]] = None,
    deprecated_aliases: Optional[List[str]] = None,
    fallback_fn: Optional[Callable[[], Optional[str]]] = None,
    cli_value: Optional[bool] = None,
    default: Optional[bool] = None,
    required: bool = False,
    value_aliases: Optional[Dict[str, bool]] = None,
) -> Optional[bool]:
    """
    获取布尔类型的环境变量
    
    支持的真值: 1, true, yes, on, enabled（不区分大小写）
    支持的假值: 0, false, no, off, disabled, ""（不区分大小写）
    
    Args:
        name: canonical 环境变量名
        aliases: 同等优先级的别名
        deprecated_aliases: 废弃的别名（会触发警告）
        fallback_fn: 回退计算函数
        cli_value: CLI 传入的值（最高优先级）
        default: 默认值
        required: 是否必需
        value_aliases: 额外的值到布尔的映射，如 {"enable": True, "disable": False}
        
    Returns:
        布尔值或默认值
        
    Raises:
        ValueError: 如果值无法转换为布尔或 required=True 且值为 None
    """
    if cli_value is not None:
        return cli_value
    
    value, source = _resolve_env(
        name=name,
        aliases=aliases,
        deprecated_aliases=deprecated_aliases,
        fallback_fn=fallback_fn,
        cli_value=None,
    )
    
    if value is None:
        if required and default is None:
            raise ValueError(f"必需的环境变量 {name} 未设置")
        return default
    
    value_lower = value.lower().strip()
    
    # 检查自定义值别名
    if value_aliases:
        for alias_val, bool_result in value_aliases.items():
            if value_lower == alias_val.lower():
                return bool_result
    
    # 检查标准真/假值
    if value_lower in _BOOL_TRUE_VALUES:
        return True
    if value_lower in _BOOL_FALSE_VALUES:
        return False
    
    raise ValueError(
        f"环境变量 {name} 的值 {value!r} 无法转换为布尔值。"
        f"支持的真值: {', '.join(sorted(_BOOL_TRUE_VALUES))}; "
        f"假值: {', '.join(sorted(_BOOL_FALSE_VALUES))}"
    )


def get_choice(
    name: str,
    choices: List[str],
    *,
    aliases: Optional[List[str]] = None,
    deprecated_aliases: Optional[List[str]] = None,
    fallback_fn: Optional[Callable[[], Optional[str]]] = None,
    cli_value: Optional[str] = None,
    default: Optional[str] = None,
    required: bool = False,
    value_aliases: Optional[Dict[str, str]] = None,
    deprecated_value_aliases: Optional[Dict[str, str]] = None,
    case_sensitive: bool = False,
) -> Optional[str]:
    """
    获取枚举类型的环境变量（必须是指定选项之一）
    
    Args:
        name: canonical 环境变量名
        choices: 允许的值列表
        aliases: 同等优先级的别名
        deprecated_aliases: 废弃的别名（会触发警告）
        fallback_fn: 回退计算函数
        cli_value: CLI 传入的值（最高优先级）
        default: 默认值
        required: 是否必需
        value_aliases: 值别名映射，如 {"st": "single_table", "pt": "per_table"}
        deprecated_value_aliases: 废弃的值别名映射（会触发警告），如 {"single": "single_table"}
        case_sensitive: 是否大小写敏感（默认不敏感）
        
    Returns:
        选项值或默认值
        
    Raises:
        ValueError: 如果值不在 choices 中或 required=True 且值为 None
    """
    value, source = _resolve_env(
        name=name,
        aliases=aliases,
        deprecated_aliases=deprecated_aliases,
        fallback_fn=fallback_fn,
        cli_value=cli_value,
    )
    
    if value is None:
        if required and default is None:
            raise ValueError(f"必需的环境变量 {name} 未设置")
        return default
    
    # 处理值别名
    check_value = value if case_sensitive else value.lower().strip()
    original_value = value  # 保存原始值用于警告
    
    # 先检查废弃的值别名（会触发警告）
    if deprecated_value_aliases:
        for alias_val, canonical_val in deprecated_value_aliases.items():
            alias_check = alias_val if case_sensitive else alias_val.lower()
            if check_value == alias_check:
                # 打印废弃警告
                _warn_deprecated_value(name, original_value, canonical_val)
                value = canonical_val
                check_value = value if case_sensitive else value.lower().strip()
                break
    
    # 再检查普通值别名（不触发警告）
    if value_aliases:
        for alias_val, canonical_val in value_aliases.items():
            alias_check = alias_val if case_sensitive else alias_val.lower()
            if check_value == alias_check:
                value = canonical_val
                check_value = value if case_sensitive else value.lower().strip()
                break
    
    # 检查是否在允许的选项中
    if case_sensitive:
        valid = value in choices
    else:
        choices_lower = [c.lower() for c in choices]
        if check_value in choices_lower:
            # 返回原始 choices 中的值（保持大小写）
            idx = choices_lower.index(check_value)
            value = choices[idx]
            valid = True
        else:
            valid = False
    
    if not valid:
        raise ValueError(
            f"环境变量 {name} 的值 {value!r} 不在允许的选项中: {choices}"
        )
    
    return value


def _warn_deprecated_value(var_name: str, deprecated_value: str, canonical_value: str) -> None:
    """
    打印废弃值警告（每个变量+值组合只警告一次）
    
    Args:
        var_name: 环境变量名
        deprecated_value: 废弃的值
        canonical_value: 推荐使用的新值
    """
    global _warned_deprecated_values
    warn_key = f"{var_name}:{deprecated_value}"
    if warn_key in _warned_deprecated_values:
        return
    _warned_deprecated_values.add(warn_key)
    
    msg = (
        f"[DEPRECATION] 环境变量 {var_name} 的值 '{deprecated_value}' 已废弃，"
        f"已自动映射为 '{canonical_value}'。请更新配置使用新值。此警告仅显示一次。"
    )
    # 使用 warnings 模块，便于测试时捕获
    warnings.warn(msg, DeprecationWarning, stacklevel=4)
    # 同时输出到 stderr，确保用户看到
    print(f"Warning: {msg}", file=sys.stderr)


def get_list(
    name: str,
    *,
    aliases: Optional[List[str]] = None,
    deprecated_aliases: Optional[List[str]] = None,
    fallback_fn: Optional[Callable[[], Optional[str]]] = None,
    cli_value: Optional[List[str]] = None,
    default: Optional[List[str]] = None,
    required: bool = False,
    separator: str = ",",
    strip: bool = True,
    filter_empty: bool = True,
) -> Optional[List[str]]:
    """
    获取列表类型的环境变量（逗号分隔）
    
    Args:
        name: canonical 环境变量名
        aliases: 同等优先级的别名
        deprecated_aliases: 废弃的别名（会触发警告）
        fallback_fn: 回退计算函数
        cli_value: CLI 传入的值（最高优先级）
        default: 默认值
        required: 是否必需
        separator: 分隔符（默认逗号）
        strip: 是否去除每项首尾空白
        filter_empty: 是否过滤空字符串
        
    Returns:
        字符串列表或默认值
    """
    if cli_value is not None:
        return cli_value
    
    value, source = _resolve_env(
        name=name,
        aliases=aliases,
        deprecated_aliases=deprecated_aliases,
        fallback_fn=fallback_fn,
        cli_value=None,
    )
    
    if value is None:
        if required and default is None:
            raise ValueError(f"必需的环境变量 {name} 未设置")
        return default
    
    items = value.split(separator)
    
    if strip:
        items = [item.strip() for item in items]
    
    if filter_empty:
        items = [item for item in items if item]
    
    return items


# ============ 便捷函数：常用 PG 变量回退 ============

def make_pg_dsn_fallback(
    host_var: str = "PGHOST",
    port_var: str = "PGPORT",
    user_var: str = "PGUSER",
    password_var: str = "PGPASSWORD",
    database_var: str = "PGDATABASE",
    default_host: str = "localhost",
    default_port: str = "5432",
    default_user: str = "postgres",
    default_database: str = "engram",
) -> Callable[[], Optional[str]]:
    """
    创建一个从 PG* 环境变量组合 DSN 的回退函数
    
    Returns:
        回退函数，当 PG* 变量可用时返回组合的 DSN，否则返回 None
    """
    def fallback() -> Optional[str]:
        host = os.environ.get(host_var, default_host)
        port = os.environ.get(port_var, default_port)
        user = os.environ.get(user_var, default_user)
        password = os.environ.get(password_var, "")
        database = os.environ.get(database_var, default_database)
        
        # 只有当至少有一个非默认值时才认为是有效配置
        has_custom = any([
            os.environ.get(host_var),
            os.environ.get(port_var),
            os.environ.get(user_var),
            os.environ.get(password_var),
            os.environ.get(database_var),
        ])
        
        if not has_custom:
            return None
        
        if password:
            return f"postgresql://{user}:{password}@{host}:{port}/{database}"
        else:
            return f"postgresql://{user}@{host}:{port}/{database}"
    
    return fallback


# ============ 调试工具 ============

def debug_env_resolution(
    name: str,
    *,
    aliases: Optional[List[str]] = None,
    deprecated_aliases: Optional[List[str]] = None,
    fallback_fn: Optional[Callable[[], Optional[str]]] = None,
) -> Dict[str, Any]:
    """
    调试工具：显示环境变量解析过程
    
    Returns:
        包含解析详情的字典
    """
    result = {
        "canonical": name,
        "aliases": aliases or [],
        "deprecated_aliases": deprecated_aliases or [],
        "values_found": {},
        "resolved_value": None,
        "resolved_source": None,
    }
    
    # 检查所有变量
    for n in [name] + (aliases or []):
        val = os.environ.get(n)
        if val is not None:
            result["values_found"][n] = {"value": val, "type": "canonical"}
    
    for n in (deprecated_aliases or []):
        val = os.environ.get(n)
        if val is not None:
            result["values_found"][n] = {"value": val, "type": "deprecated"}
    
    if fallback_fn:
        fb_val = fallback_fn()
        if fb_val is not None:
            result["values_found"]["<fallback>"] = {"value": fb_val, "type": "fallback"}
    
    # 解析最终值
    value, source = _resolve_env(
        name=name,
        aliases=aliases,
        deprecated_aliases=deprecated_aliases,
        fallback_fn=fallback_fn,
    )
    result["resolved_value"] = value
    result["resolved_source"] = source
    
    return result


# ============ 冲突检测 API ============

@dataclass
class EnvConflictInfo:
    """环境变量冲突信息"""
    canonical: str
    conflicting_var: str
    canonical_value: str
    conflicting_value: str
    conflict_type: str  # "canonical_vs_deprecated" 或 "canonical_vs_canonical"


def check_env_conflicts(
    var_definitions: List[Dict[str, Any]],
    *,
    raise_on_conflict: Optional[bool] = None,
) -> List[EnvConflictInfo]:
    """
    批量检测多个环境变量定义之间的冲突
    
    用于启动时一次性检测所有关键变量的冲突情况，而不是等到运行时才发现。
    
    Args:
        var_definitions: 变量定义列表，每个定义是一个字典，包含:
            - name: canonical 环境变量名（必需）
            - aliases: 同等优先级的别名列表（可选）
            - deprecated_aliases: 废弃的别名列表（可选）
        raise_on_conflict: 是否在发现冲突时抛出异常
            - True: 抛出 EnvConflictError
            - False: 仅返回冲突列表
            - None: 使用 _get_allow_conflict() 的设置（默认）
    
    Returns:
        冲突信息列表（如果没有冲突则为空列表）
    
    Raises:
        EnvConflictError: 如果 raise_on_conflict=True 且发现冲突
    
    Example:
        >>> conflicts = check_env_conflicts([
        ...     {"name": "STEP3_CHUNKING_VERSION", "deprecated_aliases": ["CHUNKING_VERSION"]},
        ...     {"name": "STEP3_PG_SCHEMA", "deprecated_aliases": ["STEP3_SCHEMA"]},
        ... ])
        >>> if conflicts:
        ...     print(f"发现 {len(conflicts)} 个冲突")
    """
    conflicts: List[EnvConflictInfo] = []
    
    for var_def in var_definitions:
        name = var_def.get("name")
        if not name:
            continue
        
        aliases = var_def.get("aliases") or []
        deprecated_aliases = var_def.get("deprecated_aliases") or []
        
        # 获取 canonical 值
        canonical_value = None
        canonical_source = None
        
        for n in [name] + aliases:
            val = os.environ.get(n)
            if val is not None:
                if canonical_value is not None and val != canonical_value:
                    # canonical 别名之间冲突
                    conflicts.append(EnvConflictInfo(
                        canonical=canonical_source,
                        conflicting_var=n,
                        canonical_value=canonical_value,
                        conflicting_value=val,
                        conflict_type="canonical_vs_canonical",
                    ))
                else:
                    canonical_value = val
                    canonical_source = n
        
        # 检查 deprecated 别名
        for dep_name in deprecated_aliases:
            dep_val = os.environ.get(dep_name)
            if dep_val is not None and canonical_value is not None:
                if dep_val != canonical_value:
                    # canonical 与 deprecated 冲突
                    conflicts.append(EnvConflictInfo(
                        canonical=canonical_source,
                        conflicting_var=dep_name,
                        canonical_value=canonical_value,
                        conflicting_value=dep_val,
                        conflict_type="canonical_vs_deprecated",
                    ))
    
    # 处理冲突
    if conflicts:
        should_raise = raise_on_conflict if raise_on_conflict is not None else not _get_allow_conflict()
        
        if should_raise:
            # 抛出第一个冲突的异常
            first = conflicts[0]
            raise EnvConflictError(
                canonical=first.canonical,
                legacy=first.conflicting_var,
                canonical_value=first.canonical_value,
                legacy_value=first.conflicting_value,
            )
        else:
            # 仅打印警告
            for conflict in conflicts:
                msg = (
                    f"[ENV_CONFLICT] {conflict.canonical}={conflict.canonical_value!r} 与 "
                    f"{conflict.conflicting_var}={conflict.conflicting_value!r} 冲突，"
                    f"使用 {conflict.canonical} 的值。"
                )
                warnings.warn(msg, UserWarning)
                print(f"Warning: {msg}", file=sys.stderr)
    
    return conflicts


# ============ 双读对比阈值废弃别名 ============

# 用于统一管理 CompareThresholds 相关的废弃环境变量别名
# 当使用 from_env() 加载阈值时，会检查这些废弃别名并映射到 canonical 名称

DUAL_READ_THRESHOLD_DEPRECATED_ALIASES: Dict[str, List[str]] = {
    # canonical: [deprecated_aliases...]
    # 命中重叠率阈值（HIT_OVERLAP -> OVERLAP）
    "STEP3_DUAL_READ_OVERLAP_MIN_WARN": ["STEP3_DUAL_READ_HIT_OVERLAP_MIN_WARN"],
    "STEP3_DUAL_READ_OVERLAP_MIN_FAIL": ["STEP3_DUAL_READ_HIT_OVERLAP_MIN_FAIL"],
}


def get_dual_read_deprecated_aliases() -> Dict[str, List[str]]:
    """
    获取双读对比阈值的废弃别名映射
    
    Returns:
        字典，键为 canonical 环境变量名，值为废弃别名列表
        
    Example:
        >>> aliases = get_dual_read_deprecated_aliases()
        >>> print(aliases["STEP3_DUAL_READ_OVERLAP_MIN_WARN"])
        ["STEP3_DUAL_READ_HIT_OVERLAP_MIN_WARN"]
    """
    return DUAL_READ_THRESHOLD_DEPRECATED_ALIASES.copy()
