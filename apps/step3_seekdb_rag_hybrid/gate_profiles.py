"""
gate_profiles.py - Gate Profile 定义与加载

本模块定义用于 Nightly Rebuild 和 Dual-Read 测试的 Gate Profile 数据结构。
Gate Profile 包含一组预定义的阈值配置，用于门禁检查。

## 设计目标

1. **可配置性**: 支持从内置 profile、环境变量、CLI 参数三级覆盖
2. **可追溯性**: 每个 profile 携带 version 和 source 信息
3. **可序列化**: 支持 to_dict() / from_dict() 用于 JSON 输出
4. **兼容性**: 与现有 CompareThresholds 配合使用

## 内置 Profile

- nightly_default: 与 nightly.yml 行为等价的默认配置

## 优先级（从高到低）

1. CLI 参数（overrides dict）
2. 环境变量（env dict 或 os.environ）
3. Profile 默认值

## 使用示例

```python
from gate_profiles import load_gate_profile, GateProfile

# 加载默认 profile
profile = load_gate_profile("nightly_default")
print(profile.to_dict())

# 带 CLI 覆盖
profile = load_gate_profile(
    "nightly_default",
    overrides={"min_overlap": 0.6, "top_k": 15}
)

# 带环境变量覆盖
profile = load_gate_profile(
    "nightly_default",
    env={"STEP3_NIGHTLY_MIN_OVERLAP": "0.55"}
)
```
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

try:
    from .dual_read_compare import (
        CompareThresholds,
        ThresholdsSource,
        THRESHOLD_SOURCE_CLI,
        THRESHOLD_SOURCE_DEFAULT,
        THRESHOLD_SOURCE_ENV,
    )
except ImportError:
    from dual_read_compare import (
        CompareThresholds,
        ThresholdsSource,
        THRESHOLD_SOURCE_CLI,
        THRESHOLD_SOURCE_DEFAULT,
        THRESHOLD_SOURCE_ENV,
    )


# =============================================================================
# Profile 版本常量
# =============================================================================

# 当前 profile schema 版本
GATE_PROFILE_SCHEMA_VERSION = "1.0.0"


# =============================================================================
# 环境变量名称常量
# =============================================================================

# Nightly 门禁配置环境变量
ENV_NIGHTLY_MIN_OVERLAP = "STEP3_NIGHTLY_MIN_OVERLAP"
ENV_NIGHTLY_TOP_K = "STEP3_NIGHTLY_TOP_K"
ENV_NIGHTLY_QUERY_SET = "STEP3_NIGHTLY_QUERY_SET"
ENV_NIGHTLY_RBO_MIN = "STEP3_NIGHTLY_RBO_MIN"
ENV_NIGHTLY_SCORE_DRIFT_P95_MAX = "STEP3_NIGHTLY_SCORE_DRIFT_P95_MAX"
ENV_NIGHTLY_RANK_P95_MAX = "STEP3_NIGHTLY_RANK_P95_MAX"

# Profile 选择环境变量
ENV_GATE_PROFILE = "STEP3_GATE_PROFILE"


# =============================================================================
# GateProfile 数据类
# =============================================================================

@dataclass
class GateProfile:
    """
    Gate Profile 配置
    
    定义一组门禁检查的阈值配置，用于 Nightly Rebuild 和 Dual-Read 测试。
    
    Attributes:
        name: Profile 名称标识
        version: Profile 版本（时间戳或 git SHA）
        source: 主要来源标识（default/env/cli）
        
        min_overlap: 最小命中重叠率（0.0-1.0）
        top_k: 检索结果数量
        query_set: 查询集名称
        
        rbo_min: RBO 最小值（0.0-1.0）
        score_drift_p95_max: P95 分数漂移上限
        rank_p95_max: P95 排名漂移上限
        
        field_sources: 各字段的来源追踪
        env_keys_used: 环境变量中实际使用的键
        cli_overrides: CLI 覆盖的字段列表
    """
    # 基本信息
    name: str = "default"
    version: str = ""
    source: str = THRESHOLD_SOURCE_DEFAULT
    
    # 核心阈值
    min_overlap: float = 0.5
    top_k: int = 10
    query_set: str = "nightly_default"
    
    # 扩展阈值（与 CompareThresholds 对应）
    rbo_min: float = 0.6
    score_drift_p95_max: float = 0.1
    rank_p95_max: int = 5
    
    # 来源追踪
    field_sources: Dict[str, str] = field(default_factory=dict)
    env_keys_used: List[str] = field(default_factory=list)
    cli_overrides: List[str] = field(default_factory=list)
    
    def __post_init__(self):
        """初始化后处理"""
        if not self.version:
            self.version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        if not self.field_sources:
            # 默认所有字段来源为 default
            self.field_sources = {
                "min_overlap": THRESHOLD_SOURCE_DEFAULT,
                "top_k": THRESHOLD_SOURCE_DEFAULT,
                "query_set": THRESHOLD_SOURCE_DEFAULT,
                "rbo_min": THRESHOLD_SOURCE_DEFAULT,
                "score_drift_p95_max": THRESHOLD_SOURCE_DEFAULT,
                "rank_p95_max": THRESHOLD_SOURCE_DEFAULT,
            }
    
    def to_dict(self) -> Dict[str, Any]:
        """
        转换为字典格式
        
        输出包含 version 和 source 信息，用于 JSON 序列化和审计追踪。
        
        Returns:
            字典格式的 profile 配置
        """
        return {
            "name": self.name,
            "version": self.version,
            "source": self.source,
            "schema_version": GATE_PROFILE_SCHEMA_VERSION,
            "thresholds": {
                "min_overlap": self.min_overlap,
                "top_k": self.top_k,
                "query_set": self.query_set,
                "rbo_min": self.rbo_min,
                "score_drift_p95_max": self.score_drift_p95_max,
                "rank_p95_max": self.rank_p95_max,
            },
            "field_sources": self.field_sources,
            "env_keys_used": self.env_keys_used,
            "cli_overrides": self.cli_overrides,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GateProfile":
        """
        从字典构建 GateProfile
        
        Args:
            data: 包含 profile 配置的字典
            
        Returns:
            GateProfile 实例
        """
        thresholds = data.get("thresholds", {})
        return cls(
            name=data.get("name", "default"),
            version=data.get("version", ""),
            source=data.get("source", THRESHOLD_SOURCE_DEFAULT),
            min_overlap=thresholds.get("min_overlap", 0.5),
            top_k=thresholds.get("top_k", 10),
            query_set=thresholds.get("query_set", "nightly_default"),
            rbo_min=thresholds.get("rbo_min", 0.6),
            score_drift_p95_max=thresholds.get("score_drift_p95_max", 0.1),
            rank_p95_max=thresholds.get("rank_p95_max", 5),
            field_sources=data.get("field_sources", {}),
            env_keys_used=data.get("env_keys_used", []),
            cli_overrides=data.get("cli_overrides", []),
        )
    
    def to_compare_thresholds(self) -> CompareThresholds:
        """
        转换为 CompareThresholds 实例
        
        用于与 evaluate() 函数配合使用。
        
        Returns:
            CompareThresholds 实例
        """
        thresholds_source = ThresholdsSource(
            primary_source=self.source,
            field_sources=self.field_sources,
            version=self.version,
            env_keys_used=self.env_keys_used,
            cli_overrides=self.cli_overrides,
        )
        
        return CompareThresholds(
            hit_overlap_min_fail=self.min_overlap,
            hit_overlap_min_warn=self.min_overlap + 0.1,  # warn 阈值比 fail 高 0.1
            rbo_min_fail=self.rbo_min,
            rbo_min_warn=self.rbo_min + 0.1,
            score_drift_p95_max=self.score_drift_p95_max,
            rank_p95_max_fail=self.rank_p95_max,
            rank_p95_max_warn=max(1, self.rank_p95_max - 2),  # warn 阈值比 fail 低 2
            source=thresholds_source,
        )


# =============================================================================
# 内置 Profile 定义
# =============================================================================

# nightly_default: 与 nightly.yml 行为等价的配置
# 参考 nightly.yml:
#   - STEP3_NIGHTLY_MIN_OVERLAP: "0.5"
#   - STEP3_NIGHTLY_TOP_K: "10"
#   - --dual-read-min-overlap 0.5
#   - --top-k 10
BUILTIN_PROFILES: Dict[str, GateProfile] = {
    "nightly_default": GateProfile(
        name="nightly_default",
        source=THRESHOLD_SOURCE_DEFAULT,
        # 核心阈值（与 nightly.yml 一致）
        min_overlap=0.5,
        top_k=10,
        query_set="nightly_default",
        # 扩展阈值（更严格的门禁）
        rbo_min=0.6,
        score_drift_p95_max=0.1,
        rank_p95_max=5,
    ),
    # 可添加更多 profile，例如:
    # "strict": 更严格的门禁配置
    # "relaxed": 更宽松的门禁配置（用于开发调试）
}


# =============================================================================
# Profile 加载函数
# =============================================================================

def load_gate_profile(
    name: str = "nightly_default",
    overrides: Optional[Dict[str, Any]] = None,
    env: Optional[Dict[str, str]] = None,
) -> GateProfile:
    """
    加载 Gate Profile
    
    支持三级优先级覆盖：CLI > 环境变量 > Profile 默认值
    
    Args:
        name: Profile 名称，必须是 BUILTIN_PROFILES 中的 key
        overrides: CLI 参数覆盖字典，支持的 key:
            - min_overlap: float
            - top_k: int
            - query_set: str
            - rbo_min: float
            - score_drift_p95_max: float
            - rank_p95_max: int
        env: 环境变量字典，如果为 None 则使用 os.environ
            支持的环境变量:
            - STEP3_NIGHTLY_MIN_OVERLAP
            - STEP3_NIGHTLY_TOP_K
            - STEP3_NIGHTLY_QUERY_SET
            - STEP3_NIGHTLY_RBO_MIN
            - STEP3_NIGHTLY_SCORE_DRIFT_P95_MAX
            - STEP3_NIGHTLY_RANK_P95_MAX
    
    Returns:
        GateProfile 实例，携带 version 和 source 信息
    
    Raises:
        ValueError: 如果 profile name 不存在
    
    Example:
        >>> profile = load_gate_profile("nightly_default")
        >>> profile.to_dict()["source"]
        'default'
        
        >>> profile = load_gate_profile("nightly_default", overrides={"min_overlap": 0.6})
        >>> profile.min_overlap
        0.6
        >>> profile.to_dict()["source"]
        'cli'
    """
    # 1. 获取基础 profile
    if name not in BUILTIN_PROFILES:
        available = ", ".join(BUILTIN_PROFILES.keys())
        raise ValueError(f"未知的 profile 名称: {name}，可用的 profile: {available}")
    
    base_profile = BUILTIN_PROFILES[name]
    
    # 2. 准备环境变量
    env_dict = env if env is not None else dict(os.environ)
    
    # 3. 初始化结果值和来源追踪
    field_sources: Dict[str, str] = {}
    env_keys_used: List[str] = []
    cli_overrides: List[str] = []
    
    overrides = overrides or {}
    
    # 4. 解析各字段（优先级: CLI > env > default）
    
    # min_overlap
    if "min_overlap" in overrides:
        min_overlap = float(overrides["min_overlap"])
        field_sources["min_overlap"] = THRESHOLD_SOURCE_CLI
        cli_overrides.append("min_overlap")
    elif ENV_NIGHTLY_MIN_OVERLAP in env_dict:
        min_overlap = float(env_dict[ENV_NIGHTLY_MIN_OVERLAP])
        field_sources["min_overlap"] = THRESHOLD_SOURCE_ENV
        env_keys_used.append(ENV_NIGHTLY_MIN_OVERLAP)
    else:
        min_overlap = base_profile.min_overlap
        field_sources["min_overlap"] = THRESHOLD_SOURCE_DEFAULT
    
    # top_k
    if "top_k" in overrides:
        top_k = int(overrides["top_k"])
        field_sources["top_k"] = THRESHOLD_SOURCE_CLI
        cli_overrides.append("top_k")
    elif ENV_NIGHTLY_TOP_K in env_dict:
        top_k = int(env_dict[ENV_NIGHTLY_TOP_K])
        field_sources["top_k"] = THRESHOLD_SOURCE_ENV
        env_keys_used.append(ENV_NIGHTLY_TOP_K)
    else:
        top_k = base_profile.top_k
        field_sources["top_k"] = THRESHOLD_SOURCE_DEFAULT
    
    # query_set
    if "query_set" in overrides:
        query_set = str(overrides["query_set"])
        field_sources["query_set"] = THRESHOLD_SOURCE_CLI
        cli_overrides.append("query_set")
    elif ENV_NIGHTLY_QUERY_SET in env_dict:
        query_set = str(env_dict[ENV_NIGHTLY_QUERY_SET])
        field_sources["query_set"] = THRESHOLD_SOURCE_ENV
        env_keys_used.append(ENV_NIGHTLY_QUERY_SET)
    else:
        query_set = base_profile.query_set
        field_sources["query_set"] = THRESHOLD_SOURCE_DEFAULT
    
    # rbo_min
    if "rbo_min" in overrides:
        rbo_min = float(overrides["rbo_min"])
        field_sources["rbo_min"] = THRESHOLD_SOURCE_CLI
        cli_overrides.append("rbo_min")
    elif ENV_NIGHTLY_RBO_MIN in env_dict:
        rbo_min = float(env_dict[ENV_NIGHTLY_RBO_MIN])
        field_sources["rbo_min"] = THRESHOLD_SOURCE_ENV
        env_keys_used.append(ENV_NIGHTLY_RBO_MIN)
    else:
        rbo_min = base_profile.rbo_min
        field_sources["rbo_min"] = THRESHOLD_SOURCE_DEFAULT
    
    # score_drift_p95_max
    if "score_drift_p95_max" in overrides:
        score_drift_p95_max = float(overrides["score_drift_p95_max"])
        field_sources["score_drift_p95_max"] = THRESHOLD_SOURCE_CLI
        cli_overrides.append("score_drift_p95_max")
    elif ENV_NIGHTLY_SCORE_DRIFT_P95_MAX in env_dict:
        score_drift_p95_max = float(env_dict[ENV_NIGHTLY_SCORE_DRIFT_P95_MAX])
        field_sources["score_drift_p95_max"] = THRESHOLD_SOURCE_ENV
        env_keys_used.append(ENV_NIGHTLY_SCORE_DRIFT_P95_MAX)
    else:
        score_drift_p95_max = base_profile.score_drift_p95_max
        field_sources["score_drift_p95_max"] = THRESHOLD_SOURCE_DEFAULT
    
    # rank_p95_max
    if "rank_p95_max" in overrides:
        rank_p95_max = int(overrides["rank_p95_max"])
        field_sources["rank_p95_max"] = THRESHOLD_SOURCE_CLI
        cli_overrides.append("rank_p95_max")
    elif ENV_NIGHTLY_RANK_P95_MAX in env_dict:
        rank_p95_max = int(env_dict[ENV_NIGHTLY_RANK_P95_MAX])
        field_sources["rank_p95_max"] = THRESHOLD_SOURCE_ENV
        env_keys_used.append(ENV_NIGHTLY_RANK_P95_MAX)
    else:
        rank_p95_max = base_profile.rank_p95_max
        field_sources["rank_p95_max"] = THRESHOLD_SOURCE_DEFAULT
    
    # 5. 确定主来源
    if cli_overrides:
        primary_source = THRESHOLD_SOURCE_CLI
    elif env_keys_used:
        primary_source = THRESHOLD_SOURCE_ENV
    else:
        primary_source = THRESHOLD_SOURCE_DEFAULT
    
    # 6. 生成版本号（时间戳 + git SHA 如果可用）
    version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    try:
        import subprocess
        git_sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        if git_sha:
            version = f"{version}-{git_sha}"
    except Exception:
        pass  # git 不可用时忽略
    
    # 7. 构建并返回 profile
    return GateProfile(
        name=name,
        version=version,
        source=primary_source,
        min_overlap=min_overlap,
        top_k=top_k,
        query_set=query_set,
        rbo_min=rbo_min,
        score_drift_p95_max=score_drift_p95_max,
        rank_p95_max=rank_p95_max,
        field_sources=field_sources,
        env_keys_used=env_keys_used,
        cli_overrides=cli_overrides,
    )


def get_available_profiles() -> List[str]:
    """
    获取所有可用的内置 profile 名称
    
    Returns:
        Profile 名称列表
    """
    return list(BUILTIN_PROFILES.keys())


def get_profile_from_env(
    overrides: Optional[Dict[str, Any]] = None,
    env: Optional[Dict[str, str]] = None,
) -> GateProfile:
    """
    从环境变量获取 profile（便捷函数）
    
    首先检查 STEP3_GATE_PROFILE 环境变量确定 profile 名称，
    然后加载该 profile 并应用覆盖。
    
    Args:
        overrides: CLI 参数覆盖
        env: 环境变量字典
        
    Returns:
        GateProfile 实例
    """
    env_dict = env if env is not None else dict(os.environ)
    profile_name = env_dict.get(ENV_GATE_PROFILE, "nightly_default")
    return load_gate_profile(profile_name, overrides=overrides, env=env)
