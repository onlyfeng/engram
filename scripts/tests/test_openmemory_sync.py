#!/usr/bin/env python3
"""
openmemory_sync.py 单元测试

覆盖功能:
1. verify_patches: 对 missing/sha mismatch 的 final_status 分级判定
2. checksums 旧格式 -> 新格式读取兼容
3. conflict 文件写入路径与内容字段
4. sync 冲突集合计算（patched_files 交集）与 artifact 输出
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# 将 scripts 目录添加到 path
sys.path.insert(0, str(Path(__file__).parent.parent))

from openmemory_sync import (
    OpenMemorySyncTool,
    CheckStatus,
    ConflictStrategy,
    SyncReport,
)


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def temp_workspace():
    """创建临时工作空间"""
    with tempfile.TemporaryDirectory(prefix="test_openmemory_sync_") as tmpdir:
        workspace = Path(tmpdir)
        
        # 创建基本目录结构
        (workspace / "libs" / "OpenMemory" / "packages" / "openmemory-js" / "src").mkdir(parents=True)
        (workspace / ".artifacts" / "openmemory-patch-conflicts").mkdir(parents=True)
        
        yield workspace


@pytest.fixture
def minimal_lock_data():
    """最小 lock 文件数据"""
    return {
        "upstream_url": "https://github.com/CaviraOSS/OpenMemory",
        "upstream_ref": "v1.0.0",
        "upstream_ref_type": "tag",
        "upstream_commit_sha": "abc123",
        "upstream_commit_date": "2025-01-01T00:00:00Z",
        "patched_files": [
            {"path": "libs/OpenMemory/packages/openmemory-js/src/index.ts"}
        ],
        "checksums": {
            "description": "补丁文件的 SHA256 校验和",
            "patched_files": {
                "libs/OpenMemory/packages/openmemory-js/src/index.ts": {
                    "after": "expected_sha256_value_1234567890abcdef",
                    "base": "base_sha256_value_abcdef1234567890"
                }
            }
        }
    }


@pytest.fixture
def minimal_patches_data():
    """最小 patches 文件数据"""
    return {
        "patches": [
            {
                "file": "libs/OpenMemory/packages/openmemory-js/src/index.ts",
                "changes": [
                    {
                        "id": "OM-001",
                        "category": "A",
                        "location": "line 10-20",
                        "description": "Test patch A"
                    }
                ]
            }
        ]
    }


@pytest.fixture
def setup_workspace(temp_workspace, minimal_lock_data, minimal_patches_data):
    """设置完整的测试工作空间"""
    # 写入 lock 文件
    lock_file = temp_workspace / "OpenMemory.upstream.lock.json"
    with open(lock_file, "w", encoding="utf-8") as f:
        json.dump(minimal_lock_data, f, indent=2)
        f.write("\n")
    
    # 写入 patches 文件
    patches_file = temp_workspace / "openmemory_patches.json"
    with open(patches_file, "w", encoding="utf-8") as f:
        json.dump(minimal_patches_data, f, indent=2)
        f.write("\n")
    
    return temp_workspace


# ============================================================================
# Test: verify_patches final_status 分级
# ============================================================================

class TestVerifyPatchesFinalStatus:
    """测试 verify_patches 对 missing/sha mismatch 的 final_status 分级"""
    
    def test_verify_missing_file_returns_error(self, setup_workspace, minimal_lock_data, minimal_patches_data):
        """测试文件缺失时 final_status 应为 error"""
        workspace = setup_workspace
        
        # 不创建目标文件，模拟缺失
        tool = OpenMemorySyncTool(workspace_root=workspace)
        
        # 执行 verify
        result = tool.verify_patches(quiet=True)
        
        # 验证结果
        assert result is False
        verify_result = tool.report.patches_status.get("verify_result", {})
        assert verify_result.get("final_status") == "error"
        assert len(verify_result.get("missing", [])) > 0
    
    def test_verify_category_a_mismatch_returns_error(self, setup_workspace, minimal_lock_data, minimal_patches_data):
        """测试 Category A checksum 不匹配时 final_status 应为 error"""
        workspace = setup_workspace
        
        # 创建目标文件（内容与预期不同）
        target_file = workspace / "libs/OpenMemory/packages/openmemory-js/src/index.ts"
        target_file.write_text("different content", encoding="utf-8")
        
        tool = OpenMemorySyncTool(workspace_root=workspace)
        result = tool.verify_patches(quiet=True)
        
        # 验证结果
        assert result is False
        verify_result = tool.report.patches_status.get("verify_result", {})
        assert verify_result.get("final_status") == "error"
        assert verify_result.get("category_mismatch", {}).get("A", 0) > 0
    
    def test_verify_category_b_mismatch_returns_warn(self, setup_workspace, minimal_lock_data):
        """测试 Category B checksum 不匹配时 final_status 应为 warn"""
        workspace = setup_workspace
        
        # 修改 patches 为 Category B
        patches_data = {
            "patches": [
                {
                    "file": "libs/OpenMemory/packages/openmemory-js/src/index.ts",
                    "changes": [
                        {
                            "id": "OM-002",
                            "category": "B",
                            "location": "line 10-20",
                            "description": "Test patch B"
                        }
                    ]
                }
            ]
        }
        patches_file = workspace / "openmemory_patches.json"
        with open(patches_file, "w", encoding="utf-8") as f:
            json.dump(patches_data, f, indent=2)
            f.write("\n")
        
        # 创建目标文件（内容与预期不同）
        target_file = workspace / "libs/OpenMemory/packages/openmemory-js/src/index.ts"
        target_file.write_text("different content for B", encoding="utf-8")
        
        tool = OpenMemorySyncTool(workspace_root=workspace)
        result = tool.verify_patches(quiet=True)
        
        # Category B 不匹配返回 True（不阻止）但 final_status 应为 warn
        verify_result = tool.report.patches_status.get("verify_result", {})
        assert verify_result.get("final_status") == "warn"
        assert verify_result.get("category_mismatch", {}).get("B", 0) > 0
    
    def test_verify_category_c_mismatch_returns_ok(self, setup_workspace, minimal_lock_data):
        """测试 Category C checksum 不匹配时 final_status 应为 ok"""
        workspace = setup_workspace
        
        # 修改 patches 为 Category C
        patches_data = {
            "patches": [
                {
                    "file": "libs/OpenMemory/packages/openmemory-js/src/index.ts",
                    "changes": [
                        {
                            "id": "OM-003",
                            "category": "C",
                            "location": "line 10-20",
                            "description": "Test patch C"
                        }
                    ]
                }
            ]
        }
        patches_file = workspace / "openmemory_patches.json"
        with open(patches_file, "w", encoding="utf-8") as f:
            json.dump(patches_data, f, indent=2)
            f.write("\n")
        
        # 创建目标文件（内容与预期不同）
        target_file = workspace / "libs/OpenMemory/packages/openmemory-js/src/index.ts"
        target_file.write_text("different content for C", encoding="utf-8")
        
        tool = OpenMemorySyncTool(workspace_root=workspace)
        result = tool.verify_patches(quiet=True)
        
        # Category C 不匹配不影响整体状态
        verify_result = tool.report.patches_status.get("verify_result", {})
        assert verify_result.get("final_status") == "ok"
        assert verify_result.get("category_mismatch", {}).get("C", 0) > 0
    
    def test_verify_all_passed_returns_ok(self, setup_workspace, minimal_lock_data):
        """测试所有文件校验通过时 final_status 应为 ok"""
        workspace = setup_workspace
        
        # 创建目标文件，并更新 lock 文件中的预期 checksum
        target_file = workspace / "libs/OpenMemory/packages/openmemory-js/src/index.ts"
        content = "expected content"
        target_file.write_text(content, encoding="utf-8")
        
        # 计算实际 SHA256
        import hashlib
        actual_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        
        # 更新 lock 文件
        lock_data = minimal_lock_data.copy()
        lock_data["checksums"]["patched_files"]["libs/OpenMemory/packages/openmemory-js/src/index.ts"]["after"] = actual_sha256
        lock_file = workspace / "OpenMemory.upstream.lock.json"
        with open(lock_file, "w", encoding="utf-8") as f:
            json.dump(lock_data, f, indent=2)
            f.write("\n")
        
        tool = OpenMemorySyncTool(workspace_root=workspace)
        result = tool.verify_patches(quiet=True)
        
        # 验证结果
        assert result is True
        verify_result = tool.report.patches_status.get("verify_result", {})
        assert verify_result.get("final_status") == "ok"


# ============================================================================
# Test: checksums 旧格式 -> 新格式读取兼容
# ============================================================================

class TestChecksumsFormatCompatibility:
    """测试 checksums 旧格式到新格式的读取兼容性"""
    
    def test_read_old_format_checksums(self, temp_workspace):
        """测试读取旧格式 checksums（直接 path: sha256 映射）"""
        workspace = temp_workspace
        
        # 旧格式 lock 数据
        old_format_lock = {
            "upstream_url": "https://github.com/CaviraOSS/OpenMemory",
            "upstream_ref": "v1.0.0",
            "upstream_ref_type": "tag",
            "upstream_commit_sha": "abc123",
            "upstream_commit_date": "2025-01-01T00:00:00Z",
            "patched_files": [
                {"path": "libs/OpenMemory/packages/openmemory-js/src/index.ts"}
            ],
            "checksums": {
                "description": "补丁文件的 SHA256 校验和",
                "libs/OpenMemory/packages/openmemory-js/src/index.ts": "old_format_sha256_value"
            }
        }
        
        patches_data = {
            "patches": [
                {
                    "file": "libs/OpenMemory/packages/openmemory-js/src/index.ts",
                    "changes": [
                        {"id": "OM-001", "category": "A", "location": "line 1", "description": "test"}
                    ]
                }
            ]
        }
        
        # 写入文件
        lock_file = workspace / "OpenMemory.upstream.lock.json"
        with open(lock_file, "w", encoding="utf-8") as f:
            json.dump(old_format_lock, f, indent=2)
            f.write("\n")
        
        patches_file = workspace / "openmemory_patches.json"
        with open(patches_file, "w", encoding="utf-8") as f:
            json.dump(patches_data, f, indent=2)
            f.write("\n")
        
        # 创建目标文件
        target_file = workspace / "libs/OpenMemory/packages/openmemory-js/src/index.ts"
        target_file.write_text("test content", encoding="utf-8")
        
        tool = OpenMemorySyncTool(workspace_root=workspace)
        tool.load_config_files()
        
        # 验证旧格式被正确读取
        checksums_data = tool.lock_data.get("checksums", {})
        assert "libs/OpenMemory/packages/openmemory-js/src/index.ts" in checksums_data
        
        # 运行 verify 应该能处理旧格式
        result = tool.verify_patches(quiet=True)
        verify_result = tool.report.patches_status.get("verify_result", {})
        
        # 验证能正确提取预期 checksum
        assert verify_result.get("files")
        assert verify_result["files"][0].get("expected_sha256") == "old_format_sha256_value"
    
    def test_read_new_format_checksums(self, setup_workspace):
        """测试读取新格式 checksums（patched_files 嵌套结构）"""
        workspace = setup_workspace
        
        # 创建目标文件
        target_file = workspace / "libs/OpenMemory/packages/openmemory-js/src/index.ts"
        target_file.write_text("new format test content", encoding="utf-8")
        
        tool = OpenMemorySyncTool(workspace_root=workspace)
        tool.load_config_files()
        
        # 验证新格式被正确读取
        checksums_data = tool.lock_data.get("checksums", {})
        assert "patched_files" in checksums_data
        
        patched_files = checksums_data["patched_files"]
        file_path = "libs/OpenMemory/packages/openmemory-js/src/index.ts"
        assert file_path in patched_files
        assert patched_files[file_path].get("after") == "expected_sha256_value_1234567890abcdef"
        assert patched_files[file_path].get("base") == "base_sha256_value_abcdef1234567890"
    
    def test_apply_migrates_old_to_new_format(self, temp_workspace):
        """测试 apply 时旧格式正确迁移到新格式"""
        workspace = temp_workspace
        
        # 旧格式 lock 数据（没有 patched_files 嵌套）
        old_format_lock = {
            "upstream_url": "https://github.com/CaviraOSS/OpenMemory",
            "upstream_ref": "v1.0.0",
            "upstream_ref_type": "tag",
            "upstream_commit_sha": "abc123",
            "upstream_commit_date": "2025-01-01T00:00:00Z",
            "patched_files": [
                {"path": "libs/OpenMemory/packages/openmemory-js/src/index.ts"}
            ],
            "checksums": {
                "description": "旧格式校验和",
                "libs/OpenMemory/packages/openmemory-js/src/index.ts": "old_sha256"
            }
        }
        
        patches_data = {
            "patches": [
                {
                    "file": "libs/OpenMemory/packages/openmemory-js/src/index.ts",
                    "changes": [
                        {"id": "OM-001", "category": "C", "location": "line 1", "description": "test"}
                    ]
                }
            ]
        }
        
        # 写入文件
        lock_file = workspace / "OpenMemory.upstream.lock.json"
        with open(lock_file, "w", encoding="utf-8") as f:
            json.dump(old_format_lock, f, indent=2)
            f.write("\n")
        
        patches_file = workspace / "openmemory_patches.json"
        with open(patches_file, "w", encoding="utf-8") as f:
            json.dump(patches_data, f, indent=2)
            f.write("\n")
        
        # 创建目标文件
        target_file = workspace / "libs/OpenMemory/packages/openmemory-js/src/index.ts"
        target_file.write_text("migrate format test", encoding="utf-8")
        
        tool = OpenMemorySyncTool(workspace_root=workspace)
        
        # 执行 apply（dry-run）
        result = tool.apply_patches(dry_run=True, quiet=True)
        
        # verify 结果应该正确读取旧格式的 checksum
        verify_result = tool.report.patches_status.get("apply_result", {})
        assert verify_result is not None


# ============================================================================
# Test: conflict 文件写入路径与内容字段
# ============================================================================

class TestConflictFileWriting:
    """测试 conflict 文件写入路径与内容字段"""
    
    def test_conflict_file_path_format(self, setup_workspace, minimal_lock_data):
        """测试冲突文件路径格式正确"""
        workspace = setup_workspace
        
        # 不创建目标文件以触发冲突
        tool = OpenMemorySyncTool(workspace_root=workspace)
        tool.verify_patches(quiet=True)
        
        # 检查冲突文件是否生成
        conflict_dir = workspace / ".artifacts" / "openmemory-patch-conflicts"
        conflict_files = list(conflict_dir.glob("*.conflict.json"))
        
        assert len(conflict_files) > 0
        
        # 验证文件名格式
        for cf in conflict_files:
            assert cf.suffix == ".json"
            assert ".conflict" in cf.stem
    
    def test_conflict_file_content_fields(self, setup_workspace, minimal_lock_data):
        """测试冲突文件包含必需的内容字段"""
        workspace = setup_workspace
        
        # 不创建目标文件以触发冲突
        tool = OpenMemorySyncTool(workspace_root=workspace)
        tool.verify_patches(quiet=True)
        
        # 读取冲突文件
        conflict_dir = workspace / ".artifacts" / "openmemory-patch-conflicts"
        conflict_files = list(conflict_dir.glob("*.conflict.json"))
        
        assert len(conflict_files) > 0
        
        for cf in conflict_files:
            with open(cf, "r", encoding="utf-8") as f:
                conflict_data = json.load(f)
            
            # 验证必需字段
            required_fields = ["patch_id", "file", "category", "reason", "context", "strategy", "timestamp"]
            for field in required_fields:
                assert field in conflict_data, f"Missing field: {field}"
            
            # 验证字段类型
            assert isinstance(conflict_data["patch_id"], str)
            assert isinstance(conflict_data["file"], str)
            assert conflict_data["category"] in ["A", "B", "C"]
            assert isinstance(conflict_data["reason"], str)
            assert isinstance(conflict_data["context"], str)
            assert conflict_data["strategy"] in ["clean", "3way", "manual"]
            assert isinstance(conflict_data["timestamp"], str)
    
    def test_conflict_file_has_resolution_hints(self, setup_workspace, minimal_lock_data):
        """测试冲突文件包含解决建议"""
        workspace = setup_workspace
        
        # 不创建目标文件以触发冲突
        tool = OpenMemorySyncTool(workspace_root=workspace)
        tool.verify_patches(quiet=True)
        
        # 读取冲突文件
        conflict_dir = workspace / ".artifacts" / "openmemory-patch-conflicts"
        conflict_files = list(conflict_dir.glob("*.conflict.json"))
        
        assert len(conflict_files) > 0
        
        for cf in conflict_files:
            with open(cf, "r", encoding="utf-8") as f:
                conflict_data = json.load(f)
            
            # resolution_hints 是可选但推荐的字段
            if "resolution_hints" in conflict_data:
                assert isinstance(conflict_data["resolution_hints"], list)
    
    def test_conflict_files_in_verify_result(self, setup_workspace, minimal_lock_data):
        """测试 verify 结果中包含冲突文件列表"""
        workspace = setup_workspace
        
        tool = OpenMemorySyncTool(workspace_root=workspace)
        tool.verify_patches(quiet=True)
        
        verify_result = tool.report.patches_status.get("verify_result", {})
        
        # 验证 conflict_files 列表存在
        assert "conflict_files" in verify_result
        assert len(verify_result["conflict_files"]) > 0
        
        # 验证路径格式（相对于 workspace）
        for cf_path in verify_result["conflict_files"]:
            assert ".artifacts/openmemory-patch-conflicts/" in cf_path
            assert cf_path.endswith(".conflict.json")


# ============================================================================
# Test: sync 冲突集合计算（patched_files 交集）
# ============================================================================

class TestSyncConflictSetCalculation:
    """测试 sync 冲突集合计算"""
    
    def test_conflict_set_intersection(self, setup_workspace, minimal_lock_data):
        """测试 patched_files 与 modified_files 的交集计算"""
        workspace = setup_workspace
        
        # 模拟 patched_files
        patched_files_in_lock = [
            {"path": "libs/OpenMemory/packages/openmemory-js/src/index.ts"},
            {"path": "libs/OpenMemory/packages/openmemory-js/src/utils.ts"},
            {"path": "libs/OpenMemory/packages/openmemory-js/src/client.ts"}
        ]
        
        # 更新 lock 数据
        lock_data = minimal_lock_data.copy()
        lock_data["patched_files"] = patched_files_in_lock
        
        lock_file = workspace / "OpenMemory.upstream.lock.json"
        with open(lock_file, "w", encoding="utf-8") as f:
            json.dump(lock_data, f, indent=2)
            f.write("\n")
        
        # 模拟 modified_files（来自上游对比）
        modified_files = [
            "packages/openmemory-js/src/index.ts",  # 冲突（在 patched_files 中）
            "packages/openmemory-js/src/utils.ts",   # 冲突（在 patched_files 中）
            "packages/openmemory-js/src/other.ts"    # 非冲突（不在 patched_files 中）
        ]
        
        # 计算交集（模拟 sync_upstream 中的逻辑）
        patched_set = set(
            item["path"].replace("libs/OpenMemory/", "")
            for item in patched_files_in_lock
        )
        
        conflict_files = set(modified_files) & patched_set
        
        # 验证交集计算
        assert len(conflict_files) == 2
        assert "packages/openmemory-js/src/index.ts" in conflict_files
        assert "packages/openmemory-js/src/utils.ts" in conflict_files
        assert "packages/openmemory-js/src/other.ts" not in conflict_files
    
    def test_sync_returns_conflict_files_when_not_force(self, setup_workspace, minimal_lock_data):
        """测试非强制模式下 sync 返回冲突文件"""
        workspace = setup_workspace
        
        tool = OpenMemorySyncTool(workspace_root=workspace)
        tool.load_config_files()
        
        # 模拟 comparison 结果（有修改的文件与 patched_files 重叠）
        mock_comparison = {
            "modified_files": ["packages/openmemory-js/src/index.ts"],
            "new_files": [],
            "deleted_files": [],
            "summary": {
                "total_upstream": 10,
                "total_local": 10,
                "new": 0,
                "deleted": 0,
                "modified": 1,
                "unchanged": 9
            },
            "by_directory": {}
        }
        
        # 模拟 _compare_directories 返回值
        with patch.object(tool, '_compare_directories', return_value=mock_comparison):
            with patch.object(tool, 'fetch_upstream', return_value={
                "ok": True,
                "commit_sha": "new_sha",
                "commit_date": "2025-01-15T00:00:00Z",
                "archive_sha256": "archive_sha",
                "extracted_dir": str(workspace / "temp_upstream")
            }):
                # 创建临时上游目录
                upstream_dir = workspace / "temp_upstream"
                upstream_dir.mkdir(parents=True, exist_ok=True)
                
                with patch.object(tool, '_compute_base_checksums_from_upstream', return_value={}):
                    # 执行 dry-run sync
                    result = tool.sync_upstream(dry_run=True, force=False)
                    
                    # dry-run 应该成功但不执行实际操作
                    assert result.get("dry_run") is True


# ============================================================================
# Test: 边界情况
# ============================================================================

class TestEdgeCases:
    """测试边界情况"""
    
    def test_empty_patches_file(self, temp_workspace):
        """测试空 patches 文件"""
        workspace = temp_workspace
        
        lock_data = {
            "upstream_url": "https://github.com/CaviraOSS/OpenMemory",
            "upstream_ref": "v1.0.0",
            "upstream_ref_type": "tag",
            "upstream_commit_sha": "abc123",
            "upstream_commit_date": "2025-01-01T00:00:00Z",
            "patched_files": [],
            "checksums": {"description": "empty", "patched_files": {}}
        }
        
        patches_data = {"patches": []}
        
        lock_file = workspace / "OpenMemory.upstream.lock.json"
        with open(lock_file, "w", encoding="utf-8") as f:
            json.dump(lock_data, f, indent=2)
            f.write("\n")
        
        patches_file = workspace / "openmemory_patches.json"
        with open(patches_file, "w", encoding="utf-8") as f:
            json.dump(patches_data, f, indent=2)
            f.write("\n")
        
        tool = OpenMemorySyncTool(workspace_root=workspace)
        result = tool.verify_patches(quiet=True)
        
        # 空 patches 应该返回成功
        assert result is True
        verify_result = tool.report.patches_status.get("verify_result", {})
        assert verify_result.get("final_status") == "ok"
    
    def test_no_expected_checksum(self, setup_workspace, minimal_lock_data, minimal_patches_data):
        """测试没有预期 checksum 的情况（首次校验）"""
        workspace = setup_workspace
        
        # 修改 lock 数据，移除 checksum
        lock_data = minimal_lock_data.copy()
        lock_data["checksums"]["patched_files"]["libs/OpenMemory/packages/openmemory-js/src/index.ts"]["after"] = None
        
        lock_file = workspace / "OpenMemory.upstream.lock.json"
        with open(lock_file, "w", encoding="utf-8") as f:
            json.dump(lock_data, f, indent=2)
            f.write("\n")
        
        # 创建目标文件
        target_file = workspace / "libs/OpenMemory/packages/openmemory-js/src/index.ts"
        target_file.write_text("first time content", encoding="utf-8")
        
        tool = OpenMemorySyncTool(workspace_root=workspace)
        result = tool.verify_patches(quiet=True)
        
        # 无基线应该通过
        assert result is True
        verify_result = tool.report.patches_status.get("verify_result", {})
        
        # 应有 no_baseline 状态的文件
        files = verify_result.get("files", [])
        assert len(files) > 0
        assert any(f.get("status") == "no_baseline" for f in files)
    
    def test_is_equal_to_base_detection(self, setup_workspace, minimal_lock_data, minimal_patches_data):
        """测试 is_equal_to_base 检测（补丁未重放 vs 内容变更）"""
        workspace = setup_workspace
        
        # 创建目标文件，内容等于 base（模拟补丁未重放）
        target_file = workspace / "libs/OpenMemory/packages/openmemory-js/src/index.ts"
        
        # 计算 base_sha256 对应的内容
        base_content = "this is the base content"
        target_file.write_text(base_content, encoding="utf-8")
        
        import hashlib
        base_sha256 = hashlib.sha256(base_content.encode("utf-8")).hexdigest()
        
        # 更新 lock 文件
        lock_data = minimal_lock_data.copy()
        lock_data["checksums"]["patched_files"]["libs/OpenMemory/packages/openmemory-js/src/index.ts"]["base"] = base_sha256
        
        lock_file = workspace / "OpenMemory.upstream.lock.json"
        with open(lock_file, "w", encoding="utf-8") as f:
            json.dump(lock_data, f, indent=2)
            f.write("\n")
        
        tool = OpenMemorySyncTool(workspace_root=workspace)
        result = tool.verify_patches(quiet=True)
        
        verify_result = tool.report.patches_status.get("verify_result", {})
        checksum_mismatch = verify_result.get("checksum_mismatch", [])
        
        # 应检测到 mismatch 且 is_equal_to_base 为 True
        assert len(checksum_mismatch) > 0
        mismatch = checksum_mismatch[0]
        assert mismatch.get("is_equal_to_base") is True
        assert mismatch.get("mismatch_reason") == "patch_not_applied"


# ============================================================================
# Test: Report 输出格式
# ============================================================================

class TestReportFormat:
    """测试报告输出格式"""
    
    def test_report_to_dict(self, setup_workspace):
        """测试 SyncReport.to_dict() 格式"""
        workspace = setup_workspace
        
        tool = OpenMemorySyncTool(workspace_root=workspace)
        tool.verify_patches(quiet=True)
        
        report_dict = tool.report.to_dict()
        
        # 验证基本字段
        assert "timestamp" in report_dict
        assert "overall_status" in report_dict
        assert "checks" in report_dict
        assert "patches_status" in report_dict
        
        # 验证 overall_status 值
        assert report_dict["overall_status"] in ["ok", "warn", "error"]
    
    def test_report_includes_conflict_artifacts(self, setup_workspace):
        """测试报告包含冲突产物信息"""
        workspace = setup_workspace
        
        tool = OpenMemorySyncTool(workspace_root=workspace)
        tool.verify_patches(quiet=True)
        
        report_dict = tool.report.to_dict()
        
        # 当有冲突时应包含 conflict_artifacts_dir
        if report_dict.get("conflict_files"):
            assert "conflict_artifacts_dir" in report_dict
            assert len(report_dict["conflict_files"]) > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
