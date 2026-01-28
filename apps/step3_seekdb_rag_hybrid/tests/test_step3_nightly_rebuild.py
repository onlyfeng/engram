#!/usr/bin/env python3
"""
test_step3_nightly_rebuild.py - Step3 Nightly Rebuild 单元测试

测试内容：
1. NightlyRebuildResult 数据结构
2. gate_profile 字段在 JSON 输出中的存在性
3. rollback 指令包含 project_key
4. CLI 参数解析
"""

import os
import sys
from pathlib import Path
from typing import Dict, Any

import pytest

# 添加父目录到路径
_step3_path = Path(__file__).parent.parent
if str(_step3_path) not in sys.path:
    sys.path.insert(0, str(_step3_path))

_scripts_path = _step3_path / "scripts"
if str(_scripts_path) not in sys.path:
    sys.path.insert(0, str(_scripts_path))

from step3_seekdb_rag_hybrid.scripts.step3_nightly_rebuild import (
    NightlyRebuildResult,
    parse_args,
)
from step3_seekdb_rag_hybrid.gate_profiles import (
    GateProfile,
    load_gate_profile,
    THRESHOLD_SOURCE_CLI,
    THRESHOLD_SOURCE_DEFAULT,
)


# ============ Fixtures ============


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """每个测试前清理环境变量"""
    env_vars_to_clear = [
        "STEP3_GATE_PROFILE",
        "STEP3_GATE_PROFILE_VERSION",
        "STEP3_NIGHTLY_QUERY_SET",
        "STEP3_NIGHTLY_MIN_OVERLAP",
        "STEP3_NIGHTLY_TOP_K",
        "PROJECT_KEY",
    ]
    for var in env_vars_to_clear:
        monkeypatch.delenv(var, raising=False)
    
    yield


# ============ NightlyRebuildResult 测试 ============


class TestNightlyRebuildResult:
    """NightlyRebuildResult 数据结构测试"""

    def test_to_dict_contains_gate_profile_field(self):
        """JSON 输出应包含 gate_profile 字段"""
        result = NightlyRebuildResult()
        d = result.to_dict()
        
        assert "gate_profile" in d
        # 默认应为 None
        assert d["gate_profile"] is None

    def test_to_dict_with_gate_profile(self):
        """设置 gate_profile 后 JSON 输出应包含完整信息"""
        profile = GateProfile(
            name="nightly_default",
            version="20260129T120000",
            source=THRESHOLD_SOURCE_DEFAULT,
            min_overlap=0.5,
            top_k=10,
        )
        
        result = NightlyRebuildResult()
        result.gate_profile = profile.to_dict()
        
        d = result.to_dict()
        
        assert d["gate_profile"] is not None
        assert d["gate_profile"]["name"] == "nightly_default"
        assert d["gate_profile"]["version"] == "20260129T120000"
        assert d["gate_profile"]["source"] == THRESHOLD_SOURCE_DEFAULT
        assert "thresholds" in d["gate_profile"]
        assert d["gate_profile"]["thresholds"]["min_overlap"] == 0.5
        assert d["gate_profile"]["thresholds"]["top_k"] == 10

    def test_to_dict_gate_profile_contains_required_fields(self):
        """gate_profile 应包含 name/version/source + thresholds snapshot"""
        profile = load_gate_profile("nightly_default")
        
        result = NightlyRebuildResult()
        result.gate_profile = profile.to_dict()
        
        d = result.to_dict()
        gp = d["gate_profile"]
        
        # 检查必需字段
        assert "name" in gp
        assert "version" in gp
        assert "source" in gp
        assert "thresholds" in gp
        
        # 检查 thresholds snapshot
        thresholds = gp["thresholds"]
        assert "min_overlap" in thresholds
        assert "top_k" in thresholds
        assert "query_set" in thresholds


class TestRollbackCommand:
    """回滚指令测试"""

    def test_rollback_command_includes_project_key(self):
        """rollback 指令应包含 project_key（若设置）"""
        result = NightlyRebuildResult()
        result.old_collection = "test_collection_v1"
        
        # 模拟带 project_key 的回滚指令
        project_key = "my_project"
        rollback_cmd = f'python -m seek_indexer --mode rollback --collection "{result.old_collection}"'
        rollback_cmd += f" --project-key {project_key}"
        result.rollback_command = rollback_cmd
        
        d = result.to_dict()
        
        assert d["rollback"] is not None
        assert d["rollback"]["command"] is not None
        assert "--project-key my_project" in d["rollback"]["command"]
        assert f'--collection "{result.old_collection}"' in d["rollback"]["command"]

    def test_rollback_command_without_project_key(self):
        """rollback 指令在无 project_key 时也应正确生成"""
        result = NightlyRebuildResult()
        result.old_collection = "test_collection_v1"
        
        rollback_cmd = f'python -m seek_indexer --mode rollback --collection "{result.old_collection}"'
        result.rollback_command = rollback_cmd
        
        d = result.to_dict()
        
        assert d["rollback"] is not None
        assert d["rollback"]["command"] is not None
        assert "--project-key" not in d["rollback"]["command"]

    def test_no_rollback_when_no_old_collection(self):
        """无 old_collection 时不应生成回滚指令"""
        result = NightlyRebuildResult()
        result.old_collection = None
        
        d = result.to_dict()
        
        assert d["rollback"] is None


class TestJSONOutputStructure:
    """JSON 输出结构测试"""

    def test_json_output_has_all_required_sections(self):
        """JSON 输出应包含所有必需的 section"""
        result = NightlyRebuildResult()
        d = result.to_dict()
        
        required_sections = [
            "success",
            "phase",
            "collection",
            "rebuild",
            "gate",
            "gate_profile",
            "timing",
        ]
        
        for section in required_sections:
            assert section in d, f"缺少必需字段: {section}"

    def test_json_output_gate_section_structure(self):
        """gate section 应包含正确的字段"""
        result = NightlyRebuildResult()
        result.gate_passed = True
        result.gate_total_queries = 5
        result.gate_pass_count = 4
        result.gate_warn_count = 1
        result.gate_fail_count = 0
        result.gate_error_count = 0
        result.gate_worst_recommendation = "pass"
        
        d = result.to_dict()
        gate = d["gate"]
        
        assert gate["passed"] is True
        assert gate["total_queries"] == 5
        assert gate["pass_count"] == 4
        assert gate["warn_count"] == 1
        assert gate["fail_count"] == 0
        assert gate["error_count"] == 0
        assert gate["worst_recommendation"] == "pass"


class TestCLIArguments:
    """CLI 参数测试"""

    def test_gate_profile_argument_default(self, monkeypatch):
        """--gate-profile 默认值应为 None（除非设置环境变量）"""
        monkeypatch.setattr(sys, 'argv', ['step3_nightly_rebuild.py', '--json'])
        args = parse_args()
        
        assert args.gate_profile is None

    def test_gate_profile_from_env(self, monkeypatch):
        """--gate-profile 应从环境变量 STEP3_GATE_PROFILE 读取"""
        monkeypatch.setenv("STEP3_GATE_PROFILE", "nightly_default")
        monkeypatch.setattr(sys, 'argv', ['step3_nightly_rebuild.py', '--json'])
        args = parse_args()
        
        assert args.gate_profile == "nightly_default"

    def test_gate_profile_cli_overrides_env(self, monkeypatch):
        """CLI --gate-profile 应覆盖环境变量"""
        monkeypatch.setenv("STEP3_GATE_PROFILE", "nightly_default")
        monkeypatch.setattr(sys, 'argv', [
            'step3_nightly_rebuild.py',
            '--gate-profile', 'nightly_default',
            '--json'
        ])
        args = parse_args()
        
        assert args.gate_profile == "nightly_default"

    def test_gate_profile_version_argument(self, monkeypatch):
        """--gate-profile-version 参数解析"""
        monkeypatch.setattr(sys, 'argv', [
            'step3_nightly_rebuild.py',
            '--gate-profile', 'nightly_default',
            '--gate-profile-version', '1.2.3',
            '--json'
        ])
        args = parse_args()
        
        assert args.gate_profile == "nightly_default"
        assert args.gate_profile_version == "1.2.3"

    def test_gate_profile_version_from_env(self, monkeypatch):
        """--gate-profile-version 应从环境变量读取"""
        monkeypatch.setenv("STEP3_GATE_PROFILE_VERSION", "2.0.0")
        monkeypatch.setattr(sys, 'argv', ['step3_nightly_rebuild.py', '--json'])
        args = parse_args()
        
        assert args.gate_profile_version == "2.0.0"


class TestGateProfileIntegration:
    """Gate Profile 集成测试"""

    def test_load_gate_profile_and_set_to_result(self):
        """加载 gate_profile 并设置到 result"""
        profile = load_gate_profile("nightly_default")
        
        result = NightlyRebuildResult()
        result.gate_profile = profile.to_dict()
        
        d = result.to_dict()
        
        assert d["gate_profile"]["name"] == "nightly_default"
        assert d["gate_profile"]["source"] in ["default", "env", "cli"]
        assert "version" in d["gate_profile"]
        assert d["gate_profile"]["version"]  # 应非空

    def test_gate_profile_with_cli_overrides(self):
        """使用 CLI 覆盖加载 gate_profile"""
        profile = load_gate_profile(
            "nightly_default",
            overrides={"min_overlap": 0.7, "top_k": 15}
        )
        
        result = NightlyRebuildResult()
        result.gate_profile = profile.to_dict()
        
        d = result.to_dict()
        gp = d["gate_profile"]
        
        assert gp["source"] == THRESHOLD_SOURCE_CLI
        assert gp["thresholds"]["min_overlap"] == 0.7
        assert gp["thresholds"]["top_k"] == 15
        assert "min_overlap" in gp["cli_overrides"]
        assert "top_k" in gp["cli_overrides"]

    def test_gate_profile_version_override(self):
        """版本可被手动覆盖"""
        profile = load_gate_profile("nightly_default")
        
        # 手动设置版本
        custom_version = "v1.0.0-custom"
        profile.version = custom_version
        
        result = NightlyRebuildResult()
        result.gate_profile = profile.to_dict()
        
        d = result.to_dict()
        
        assert d["gate_profile"]["version"] == custom_version


class TestErrorHandling:
    """错误处理测试"""

    def test_error_phase_recorded(self):
        """错误阶段应被记录"""
        result = NightlyRebuildResult()
        result.error = "门禁检查失败"
        result.error_phase = "gate"
        
        d = result.to_dict()
        
        assert d["error"] is not None
        assert d["error"]["message"] == "门禁检查失败"
        assert d["error"]["phase"] == "gate"

    def test_activation_blocked_info(self):
        """激活被阻止的信息应被记录"""
        result = NightlyRebuildResult()
        result.activation_blocked = True
        result.activation_blocked_info = {"reason": "STEP3_ALLOW_ACTIVE_COLLECTION_SWITCH not set"}
        result.how_to_enable = "设置环境变量 STEP3_ALLOW_ACTIVE_COLLECTION_SWITCH=1"
        result.how_to_manual_activate = "python -m seek_indexer --mode validate-switch ..."
        
        d = result.to_dict()
        
        assert d["activation_blocked"] is not None
        assert d["activation_blocked"]["blocked"] is True
        assert d["activation_blocked"]["how_to_enable"] is not None
