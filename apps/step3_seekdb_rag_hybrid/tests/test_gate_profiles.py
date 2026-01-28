#!/usr/bin/env python3
"""
test_gate_profiles.py - Gate Profile 单元测试

测试内容：
1. GateProfile 数据类的基本功能
2. to_dict() / from_dict() 序列化
3. load_gate_profile() 的三级优先级覆盖
4. 内置 profile 的正确性
5. version 和 source 追踪
"""

import os
import sys
from pathlib import Path

import pytest

# 添加父目录到路径
_step3_path = Path(__file__).parent.parent
if str(_step3_path) not in sys.path:
    sys.path.insert(0, str(_step3_path))

from step3_seekdb_rag_hybrid.gate_profiles import (
    GateProfile,
    GATE_PROFILE_SCHEMA_VERSION,
    BUILTIN_PROFILES,
    load_gate_profile,
    get_available_profiles,
    get_profile_from_env,
    ENV_NIGHTLY_MIN_OVERLAP,
    ENV_NIGHTLY_TOP_K,
    ENV_NIGHTLY_QUERY_SET,
    ENV_NIGHTLY_RBO_MIN,
    ENV_NIGHTLY_SCORE_DRIFT_P95_MAX,
    ENV_NIGHTLY_RANK_P95_MAX,
    ENV_GATE_PROFILE,
)
from step3_seekdb_rag_hybrid.dual_read_compare import (
    THRESHOLD_SOURCE_CLI,
    THRESHOLD_SOURCE_DEFAULT,
    THRESHOLD_SOURCE_ENV,
)


# ============ Fixtures ============


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """每个测试前清理环境变量"""
    env_vars_to_clear = [
        ENV_NIGHTLY_MIN_OVERLAP,
        ENV_NIGHTLY_TOP_K,
        ENV_NIGHTLY_QUERY_SET,
        ENV_NIGHTLY_RBO_MIN,
        ENV_NIGHTLY_SCORE_DRIFT_P95_MAX,
        ENV_NIGHTLY_RANK_P95_MAX,
        ENV_GATE_PROFILE,
    ]
    for var in env_vars_to_clear:
        monkeypatch.delenv(var, raising=False)
    
    yield


# ============ GateProfile 基本功能测试 ============


class TestGateProfileBasic:
    """GateProfile 数据类基本功能测试"""

    def test_default_values(self):
        """测试默认值"""
        profile = GateProfile()
        
        assert profile.name == "default"
        assert profile.source == THRESHOLD_SOURCE_DEFAULT
        assert profile.min_overlap == 0.5
        assert profile.top_k == 10
        assert profile.query_set == "nightly_default"
        assert profile.rbo_min == 0.6
        assert profile.score_drift_p95_max == 0.1
        assert profile.rank_p95_max == 5

    def test_version_auto_generated(self):
        """测试版本自动生成"""
        profile = GateProfile()
        
        assert profile.version != ""
        # 版本格式应该是 YYYYMMDDTHHMMSS 或 YYYYMMDDTHHMMSS-gitsha
        assert len(profile.version) >= 15

    def test_field_sources_auto_initialized(self):
        """测试 field_sources 自动初始化"""
        profile = GateProfile()
        
        assert "min_overlap" in profile.field_sources
        assert "top_k" in profile.field_sources
        assert profile.field_sources["min_overlap"] == THRESHOLD_SOURCE_DEFAULT


# ============ to_dict / from_dict 序列化测试 ============


class TestGateProfileSerialization:
    """GateProfile 序列化测试"""

    def test_to_dict_contains_required_fields(self):
        """测试 to_dict 包含必需字段"""
        profile = GateProfile(name="test_profile")
        d = profile.to_dict()
        
        # 必须包含 version 和 source
        assert "version" in d
        assert "source" in d
        assert d["version"] != ""
        assert d["source"] == THRESHOLD_SOURCE_DEFAULT
        
        # 必须包含 name 和 schema_version
        assert d["name"] == "test_profile"
        assert d["schema_version"] == GATE_PROFILE_SCHEMA_VERSION
        
        # 必须包含 thresholds
        assert "thresholds" in d
        assert "min_overlap" in d["thresholds"]
        assert "top_k" in d["thresholds"]

    def test_to_dict_from_dict_roundtrip(self):
        """测试序列化往返"""
        original = GateProfile(
            name="roundtrip_test",
            min_overlap=0.7,
            top_k=15,
            query_set="custom_set",
            rbo_min=0.75,
            score_drift_p95_max=0.05,
            rank_p95_max=3,
        )
        
        d = original.to_dict()
        restored = GateProfile.from_dict(d)
        
        assert restored.name == original.name
        assert restored.min_overlap == original.min_overlap
        assert restored.top_k == original.top_k
        assert restored.query_set == original.query_set
        assert restored.rbo_min == original.rbo_min
        assert restored.score_drift_p95_max == original.score_drift_p95_max
        assert restored.rank_p95_max == original.rank_p95_max

    def test_to_dict_includes_source_tracking(self):
        """测试 to_dict 包含来源追踪"""
        profile = GateProfile(
            field_sources={"min_overlap": THRESHOLD_SOURCE_CLI},
            cli_overrides=["min_overlap"],
        )
        d = profile.to_dict()
        
        assert "field_sources" in d
        assert "cli_overrides" in d
        assert d["field_sources"]["min_overlap"] == THRESHOLD_SOURCE_CLI
        assert "min_overlap" in d["cli_overrides"]


# ============ load_gate_profile 测试 ============


class TestLoadGateProfile:
    """load_gate_profile 函数测试"""

    def test_load_nightly_default(self):
        """测试加载 nightly_default profile"""
        profile = load_gate_profile("nightly_default")
        
        assert profile.name == "nightly_default"
        assert profile.min_overlap == 0.5
        assert profile.top_k == 10
        assert profile.source == THRESHOLD_SOURCE_DEFAULT

    def test_load_unknown_profile_raises(self):
        """测试加载未知 profile 抛出异常"""
        with pytest.raises(ValueError) as exc_info:
            load_gate_profile("unknown_profile")
        
        assert "未知的 profile 名称" in str(exc_info.value)
        assert "nightly_default" in str(exc_info.value)

    def test_cli_overrides(self):
        """测试 CLI 参数覆盖"""
        profile = load_gate_profile(
            "nightly_default",
            overrides={"min_overlap": 0.6, "top_k": 15}
        )
        
        assert profile.min_overlap == 0.6
        assert profile.top_k == 15
        assert profile.source == THRESHOLD_SOURCE_CLI
        assert "min_overlap" in profile.cli_overrides
        assert "top_k" in profile.cli_overrides
        assert profile.field_sources["min_overlap"] == THRESHOLD_SOURCE_CLI
        assert profile.field_sources["top_k"] == THRESHOLD_SOURCE_CLI

    def test_env_overrides(self):
        """测试环境变量覆盖"""
        env = {
            ENV_NIGHTLY_MIN_OVERLAP: "0.55",
            ENV_NIGHTLY_TOP_K: "12",
        }
        profile = load_gate_profile("nightly_default", env=env)
        
        assert profile.min_overlap == 0.55
        assert profile.top_k == 12
        assert profile.source == THRESHOLD_SOURCE_ENV
        assert ENV_NIGHTLY_MIN_OVERLAP in profile.env_keys_used
        assert ENV_NIGHTLY_TOP_K in profile.env_keys_used
        assert profile.field_sources["min_overlap"] == THRESHOLD_SOURCE_ENV
        assert profile.field_sources["top_k"] == THRESHOLD_SOURCE_ENV

    def test_cli_overrides_env(self):
        """测试 CLI 优先级高于环境变量"""
        env = {
            ENV_NIGHTLY_MIN_OVERLAP: "0.55",
            ENV_NIGHTLY_TOP_K: "12",
        }
        overrides = {
            "min_overlap": 0.7,  # CLI 覆盖
        }
        profile = load_gate_profile("nightly_default", overrides=overrides, env=env)
        
        # CLI 覆盖的字段
        assert profile.min_overlap == 0.7
        assert profile.field_sources["min_overlap"] == THRESHOLD_SOURCE_CLI
        
        # 环境变量覆盖的字段
        assert profile.top_k == 12
        assert profile.field_sources["top_k"] == THRESHOLD_SOURCE_ENV
        
        # 主来源应该是 CLI（因为有 CLI 覆盖）
        assert profile.source == THRESHOLD_SOURCE_CLI

    def test_default_fallback(self):
        """测试默认值回退"""
        profile = load_gate_profile("nightly_default", env={})
        
        assert profile.min_overlap == 0.5  # 默认值
        assert profile.field_sources["min_overlap"] == THRESHOLD_SOURCE_DEFAULT

    def test_all_env_vars(self):
        """测试所有环境变量"""
        env = {
            ENV_NIGHTLY_MIN_OVERLAP: "0.65",
            ENV_NIGHTLY_TOP_K: "20",
            ENV_NIGHTLY_QUERY_SET: "custom_query_set",
            ENV_NIGHTLY_RBO_MIN: "0.7",
            ENV_NIGHTLY_SCORE_DRIFT_P95_MAX: "0.05",
            ENV_NIGHTLY_RANK_P95_MAX: "3",
        }
        profile = load_gate_profile("nightly_default", env=env)
        
        assert profile.min_overlap == 0.65
        assert profile.top_k == 20
        assert profile.query_set == "custom_query_set"
        assert profile.rbo_min == 0.7
        assert profile.score_drift_p95_max == 0.05
        assert profile.rank_p95_max == 3

    def test_version_format(self):
        """测试版本格式"""
        profile = load_gate_profile("nightly_default")
        
        # 版本应该以时间戳开头
        assert profile.version[0:4].isdigit()  # 年份
        # 可能包含 git SHA
        if "-" in profile.version:
            timestamp, sha = profile.version.split("-", 1)
            assert len(timestamp) == 15  # YYYYMMDDTHHMMSS


# ============ to_compare_thresholds 测试 ============


class TestToCompareThresholds:
    """to_compare_thresholds 方法测试"""

    def test_basic_conversion(self):
        """测试基本转换"""
        profile = GateProfile(
            min_overlap=0.5,
            rbo_min=0.6,
            score_drift_p95_max=0.1,
            rank_p95_max=5,
        )
        thresholds = profile.to_compare_thresholds()
        
        assert thresholds.hit_overlap_min_fail == 0.5
        assert thresholds.hit_overlap_min_warn == 0.6  # +0.1
        assert thresholds.rbo_min_fail == 0.6
        assert thresholds.rbo_min_warn == 0.7  # +0.1
        assert thresholds.score_drift_p95_max == 0.1
        assert thresholds.rank_p95_max_fail == 5
        assert thresholds.rank_p95_max_warn == 3  # -2

    def test_source_preserved(self):
        """测试来源信息保留"""
        profile = GateProfile(
            source=THRESHOLD_SOURCE_CLI,
            field_sources={"min_overlap": THRESHOLD_SOURCE_CLI},
            cli_overrides=["min_overlap"],
        )
        thresholds = profile.to_compare_thresholds()
        
        assert thresholds.source is not None
        assert thresholds.source.primary_source == THRESHOLD_SOURCE_CLI
        assert thresholds.source.cli_overrides == ["min_overlap"]


# ============ 辅助函数测试 ============


class TestHelperFunctions:
    """辅助函数测试"""

    def test_get_available_profiles(self):
        """测试 get_available_profiles"""
        profiles = get_available_profiles()
        
        assert "nightly_default" in profiles
        assert isinstance(profiles, list)

    def test_get_profile_from_env_default(self):
        """测试 get_profile_from_env 默认行为"""
        profile = get_profile_from_env(env={})
        
        assert profile.name == "nightly_default"

    def test_get_profile_from_env_with_profile_name(self):
        """测试通过环境变量指定 profile"""
        env = {ENV_GATE_PROFILE: "nightly_default"}
        profile = get_profile_from_env(env=env)
        
        assert profile.name == "nightly_default"

    def test_get_profile_from_env_with_overrides(self):
        """测试 get_profile_from_env 带覆盖"""
        env = {ENV_NIGHTLY_MIN_OVERLAP: "0.6"}
        overrides = {"top_k": 15}
        profile = get_profile_from_env(overrides=overrides, env=env)
        
        assert profile.min_overlap == 0.6
        assert profile.top_k == 15


# ============ 内置 Profile 验证 ============


class TestBuiltinProfiles:
    """内置 Profile 验证测试"""

    def test_nightly_default_exists(self):
        """测试 nightly_default profile 存在"""
        assert "nightly_default" in BUILTIN_PROFILES

    def test_nightly_default_values(self):
        """测试 nightly_default profile 值与 nightly.yml 一致"""
        profile = BUILTIN_PROFILES["nightly_default"]
        
        # 与 nightly.yml 中的配置一致
        assert profile.min_overlap == 0.5  # STEP3_NIGHTLY_MIN_OVERLAP
        assert profile.top_k == 10  # STEP3_NIGHTLY_TOP_K
        assert profile.query_set == "nightly_default"  # STEP3_NIGHTLY_QUERY_SET

    def test_all_builtin_profiles_are_valid(self):
        """测试所有内置 profile 都是有效的 GateProfile"""
        for name, profile in BUILTIN_PROFILES.items():
            assert isinstance(profile, GateProfile)
            assert profile.name == name
            # 验证可以转换为 dict
            d = profile.to_dict()
            assert "version" in d
            assert "source" in d


# ============ 边界情况测试 ============


class TestEdgeCases:
    """边界情况测试"""

    def test_empty_overrides(self):
        """测试空覆盖"""
        profile = load_gate_profile("nightly_default", overrides={})
        
        assert profile.source == THRESHOLD_SOURCE_DEFAULT

    def test_none_overrides(self):
        """测试 None 覆盖"""
        profile = load_gate_profile("nightly_default", overrides=None)
        
        assert profile.source == THRESHOLD_SOURCE_DEFAULT

    def test_partial_overrides(self):
        """测试部分覆盖"""
        overrides = {"min_overlap": 0.6}
        profile = load_gate_profile("nightly_default", overrides=overrides)
        
        assert profile.min_overlap == 0.6  # 覆盖
        assert profile.top_k == 10  # 默认
        assert profile.field_sources["min_overlap"] == THRESHOLD_SOURCE_CLI
        assert profile.field_sources["top_k"] == THRESHOLD_SOURCE_DEFAULT

    def test_type_conversion(self):
        """测试类型转换"""
        overrides = {
            "min_overlap": "0.7",  # 字符串应该转换为 float
            "top_k": "15",  # 字符串应该转换为 int
        }
        profile = load_gate_profile("nightly_default", overrides=overrides)
        
        assert profile.min_overlap == 0.7
        assert isinstance(profile.min_overlap, float)
        assert profile.top_k == 15
        assert isinstance(profile.top_k, int)
