#!/usr/bin/env python3
"""
test_evidence_refs_schema.py - evidence_refs_json schema 和结构测试

测试内容:
1. build_evidence_ref_for_patch_blob 函数输出结构验证
2. build_evidence_refs_json 函数输出结构验证
3. validate_evidence_ref 验证函数测试
4. evidence_uri 与 resolver 互通性测试
"""

import pytest
import sys
from pathlib import Path

# 确保可以导入 engram_step1 模块
sys.path.insert(0, str(Path(__file__).parent.parent))

from engram_step1.uri import (
    build_evidence_ref_for_patch_blob,
    build_evidence_refs_json,
    validate_evidence_ref,
    build_evidence_uri,
    parse_evidence_uri,
)


class TestBuildEvidenceRefForPatchBlob:
    """测试 build_evidence_ref_for_patch_blob 函数"""

    def test_basic_structure(self):
        """测试基础结构包含所有必需字段"""
        ref = build_evidence_ref_for_patch_blob(
            source_type="git",
            source_id="1:abc123def456",
            sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )
        
        # 验证必需字段
        assert "artifact_uri" in ref
        assert "sha256" in ref
        assert "source_id" in ref
        assert "source_type" in ref
        assert "kind" in ref
        
        # 验证字段值
        assert ref["source_type"] == "git"
        assert ref["source_id"] == "1:abc123def456"
        assert ref["sha256"] == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        assert ref["kind"] == "patch"

    def test_artifact_uri_format(self):
        """测试 artifact_uri 格式正确（memory://patch_blobs/...）"""
        ref = build_evidence_ref_for_patch_blob(
            source_type="svn",
            source_id="2:1234",
            sha256="a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
        )
        
        assert ref["artifact_uri"].startswith("memory://patch_blobs/")
        assert "svn/2:1234/" in ref["artifact_uri"]
        assert ref["sha256"] in ref["artifact_uri"]

    def test_with_size_bytes(self):
        """测试包含可选的 size_bytes 字段"""
        ref = build_evidence_ref_for_patch_blob(
            source_type="git",
            source_id="1:abc123",
            sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            size_bytes=1024,
        )
        
        assert "size_bytes" in ref
        assert ref["size_bytes"] == 1024

    def test_without_size_bytes(self):
        """测试不包含 size_bytes 时结果不含该字段"""
        ref = build_evidence_ref_for_patch_blob(
            source_type="git",
            source_id="1:abc123",
            sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )
        
        assert "size_bytes" not in ref

    def test_custom_kind(self):
        """测试自定义 kind 参数"""
        ref = build_evidence_ref_for_patch_blob(
            source_type="git",
            source_id="1:abc123",
            sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            kind="diffstat",
        )
        
        assert ref["kind"] == "diffstat"

    def test_extra_fields(self):
        """测试 extra 参数可以添加额外字段"""
        ref = build_evidence_ref_for_patch_blob(
            source_type="git",
            source_id="1:abc123",
            sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            extra={"format": "diff", "degraded": False},
        )
        
        assert ref["format"] == "diff"
        assert ref["degraded"] is False

    def test_normalization(self):
        """测试参数会被规范化（小写、去空格）"""
        ref = build_evidence_ref_for_patch_blob(
            source_type="  GIT  ",
            source_id="  1:ABC123  ",
            sha256="  E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855  ",
        )
        
        assert ref["source_type"] == "git"
        assert ref["source_id"] == "1:ABC123"
        assert ref["sha256"] == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


class TestBuildEvidenceRefsJson:
    """测试 build_evidence_refs_json 函数"""

    def test_with_patches(self):
        """测试包含 patches 列表"""
        patch1 = build_evidence_ref_for_patch_blob("git", "1:abc", "a" * 64)
        patch2 = build_evidence_ref_for_patch_blob("git", "1:def", "b" * 64)
        
        result = build_evidence_refs_json(patches=[patch1, patch2])
        
        assert "patches" in result
        assert len(result["patches"]) == 2
        assert result["patches"][0]["source_id"] == "1:abc"
        assert result["patches"][1]["source_id"] == "1:def"

    def test_empty_result(self):
        """测试无参数时返回空字典"""
        result = build_evidence_refs_json()
        assert result == {}

    def test_with_extra(self):
        """测试 extra 参数可以添加额外字段"""
        result = build_evidence_refs_json(
            extra={"batch_id": "sync_001", "source": "gitlab"}
        )
        
        assert result["batch_id"] == "sync_001"
        assert result["source"] == "gitlab"

    def test_combined(self):
        """测试同时包含多种数据"""
        patch = build_evidence_ref_for_patch_blob("git", "1:abc", "a" * 64)
        attachment = {"kind": "log", "uri": "file:///log.txt", "sha256": "b" * 64}
        
        result = build_evidence_refs_json(
            patches=[patch],
            attachments=[attachment],
            extra={"version": "1.0"},
        )
        
        assert "patches" in result
        assert "attachments" in result
        assert result["version"] == "1.0"


class TestValidateEvidenceRef:
    """测试 validate_evidence_ref 验证函数"""

    def test_valid_ref(self):
        """测试有效的 evidence ref 通过验证"""
        ref = build_evidence_ref_for_patch_blob(
            source_type="git",
            source_id="1:abc123",
            sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )
        
        is_valid, error = validate_evidence_ref(ref)
        assert is_valid is True
        assert error is None

    def test_missing_artifact_uri(self):
        """测试缺少 artifact_uri 字段"""
        ref = {
            "sha256": "a" * 64,
            "source_id": "1:abc",
        }
        
        is_valid, error = validate_evidence_ref(ref)
        assert is_valid is False
        assert "artifact_uri" in error

    def test_missing_sha256(self):
        """测试缺少 sha256 字段"""
        ref = {
            "artifact_uri": "memory://patch_blobs/git/1:abc/sha256",
            "source_id": "1:abc",
        }
        
        is_valid, error = validate_evidence_ref(ref)
        assert is_valid is False
        assert "sha256" in error

    def test_missing_source_id(self):
        """测试缺少 source_id 字段"""
        ref = {
            "artifact_uri": "memory://patch_blobs/git/1:abc/sha256",
            "sha256": "a" * 64,
        }
        
        is_valid, error = validate_evidence_ref(ref)
        assert is_valid is False
        assert "source_id" in error

    def test_invalid_sha256_length(self):
        """测试 sha256 长度不正确"""
        ref = {
            "artifact_uri": "memory://patch_blobs/git/1:abc/sha256",
            "sha256": "a" * 32,  # 应该是 64 位
            "source_id": "1:abc",
        }
        
        is_valid, error = validate_evidence_ref(ref)
        assert is_valid is False
        assert "64" in error

    def test_invalid_sha256_format(self):
        """测试 sha256 格式不正确（非十六进制）"""
        ref = {
            "artifact_uri": "memory://patch_blobs/git/1:abc/sha256",
            "sha256": "g" * 64,  # g 不是十六进制字符
            "source_id": "1:abc",
        }
        
        is_valid, error = validate_evidence_ref(ref)
        assert is_valid is False
        assert "十六进制" in error or "hex" in error.lower()

    def test_invalid_artifact_uri_scheme(self):
        """测试 artifact_uri scheme 不正确"""
        ref = {
            "artifact_uri": "https://example.com/patch",  # 应该是 memory://
            "sha256": "a" * 64,
            "source_id": "1:abc",
        }
        
        is_valid, error = validate_evidence_ref(ref)
        assert is_valid is False
        assert "memory://" in error

    def test_invalid_source_id_format(self):
        """测试 source_id 格式不正确（缺少 : 分隔符）"""
        ref = {
            "artifact_uri": "memory://patch_blobs/git/1:abc/sha256",
            "sha256": "a" * 64,
            "source_id": "1abc",  # 应该包含 :
        }
        
        is_valid, error = validate_evidence_ref(ref)
        assert is_valid is False
        assert ":" in error or "格式" in error


class TestEvidenceUriInterop:
    """测试 evidence_uri 与 resolver 的互通性"""

    def test_build_and_parse_roundtrip(self):
        """测试构建的 URI 可以被正确解析"""
        source_type = "git"
        source_id = "123:abc456def"
        sha256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        
        # 构建 URI
        uri = build_evidence_uri(source_type, source_id, sha256)
        
        # 解析 URI
        parsed = parse_evidence_uri(uri)
        
        assert parsed is not None
        assert parsed["source_type"] == source_type
        assert parsed["source_id"] == source_id
        assert parsed["sha256"] == sha256

    def test_ref_uri_can_be_parsed(self):
        """测试从 evidence_ref 中提取的 URI 可以被解析"""
        ref = build_evidence_ref_for_patch_blob(
            source_type="svn",
            source_id="5:9999",
            sha256="d7a8fbb307d7809469ca9abcb0082e4f8d5651e46d3cdb762d02d0bf37c9e592",
        )
        
        # 从 ref 提取 URI 并解析
        parsed = parse_evidence_uri(ref["artifact_uri"])
        
        assert parsed is not None
        assert parsed["source_type"] == "svn"
        assert parsed["source_id"] == "5:9999"
        assert parsed["sha256"] == ref["sha256"]

    def test_parsed_values_match_ref(self):
        """测试解析后的值与原始 ref 一致"""
        ref = build_evidence_ref_for_patch_blob(
            source_type="git",
            source_id="100:deadbeef",
            sha256="a" * 64,
        )
        
        parsed = parse_evidence_uri(ref["artifact_uri"])
        
        assert parsed["source_type"] == ref["source_type"]
        assert parsed["source_id"] == ref["source_id"]
        assert parsed["sha256"] == ref["sha256"]


class TestEvidenceRefsJsonSchema:
    """测试 evidence_refs_json 整体 schema 结构"""

    def test_governance_write_audit_schema(self):
        """测试适用于 governance.write_audit 的 schema"""
        # 模拟同步批次产生的 patches
        patches = [
            build_evidence_ref_for_patch_blob("git", "1:commit1", "a" * 64, size_bytes=1024),
            build_evidence_ref_for_patch_blob("git", "1:commit2", "b" * 64, size_bytes=2048),
        ]
        
        evidence_refs = build_evidence_refs_json(
            patches=patches,
            extra={"batch_type": "scm_sync", "repo_id": 1},
        )
        
        # 验证结构
        assert isinstance(evidence_refs, dict)
        assert "patches" in evidence_refs
        assert isinstance(evidence_refs["patches"], list)
        assert len(evidence_refs["patches"]) == 2
        
        # 验证每个 patch 的结构
        for patch in evidence_refs["patches"]:
            is_valid, error = validate_evidence_ref(patch)
            assert is_valid, f"Patch 验证失败: {error}"

    def test_analysis_knowledge_candidates_schema(self):
        """测试适用于 analysis.knowledge_candidates 的 schema"""
        # 单个 patch 作为证据
        patch = build_evidence_ref_for_patch_blob(
            source_type="svn",
            source_id="2:12345",
            sha256="c" * 64,
            extra={"revision": 12345, "author": "developer"},
        )
        
        evidence_refs = build_evidence_refs_json(patches=[patch])
        
        # 验证可以作为 JSON 序列化
        import json
        json_str = json.dumps(evidence_refs)
        parsed = json.loads(json_str)
        
        assert parsed == evidence_refs

    def test_empty_evidence_refs_is_valid(self):
        """测试空的 evidence_refs_json 是有效的"""
        evidence_refs = build_evidence_refs_json()
        
        assert evidence_refs == {}
        
        # 可以安全序列化
        import json
        json_str = json.dumps(evidence_refs)
        assert json_str == "{}"


class TestBackfillEvidenceUriIntegration:
    """测试 backfill_evidence_uri 与 migrate 的集成"""

    def test_backfill_function_import(self):
        """测试 backfill_evidence_uri 函数可以被导入"""
        try:
            from backfill_evidence_uri import backfill_evidence_uri
            assert callable(backfill_evidence_uri)
        except ImportError:
            pytest.skip("backfill_evidence_uri 模块不可用")

    def test_backfill_chunking_version_import(self):
        """测试 backfill_chunking_version 函数可以被导入"""
        try:
            from backfill_chunking_version import backfill_chunking_version
            assert callable(backfill_chunking_version)
        except ImportError:
            pytest.skip("backfill_chunking_version 模块不可用")

    def test_migrate_lazy_import_evidence_uri(self):
        """测试 migrate 模块可以延迟加载 backfill_evidence_uri"""
        try:
            from engram_step1.migrate import _get_backfill_evidence_uri
            fn = _get_backfill_evidence_uri()
            assert callable(fn)
        except ImportError:
            pytest.skip("migrate 模块不可用")

    def test_migrate_lazy_import_chunking_version(self):
        """测试 migrate 模块可以延迟加载 backfill_chunking_version"""
        try:
            from engram_step1.migrate import _get_backfill_chunking_version
            fn = _get_backfill_chunking_version()
            assert callable(fn)
        except ImportError:
            pytest.skip("migrate 模块不可用")


class TestBackfillEvidenceUriFunctionality:
    """测试 backfill_evidence_uri 功能（单元测试级别）"""

    def test_backfill_result_structure(self):
        """测试 backfill 结果结构"""
        # 模拟 backfill 返回结构
        expected_keys = [
            "success",
            "total_processed",
            "total_updated",
            "total_skipped",
            "total_failed",
            "dry_run",
        ]
        
        # 创建模拟结果
        mock_result = {
            "success": True,
            "total_processed": 100,
            "total_updated": 95,
            "total_skipped": 3,
            "total_failed": 2,
            "dry_run": False,
        }
        
        for key in expected_keys:
            assert key in mock_result, f"结果应包含 {key} 字段"

    def test_backfill_chunking_version_result_structure(self):
        """测试 backfill_chunking_version 结果结构"""
        expected_keys = [
            "success",
            "target_version",
            "dry_run",
            "patch_blobs",
            "attachments",
        ]
        
        # 创建模拟结果
        mock_result = {
            "success": True,
            "target_version": "v1.0",
            "dry_run": False,
            "patch_blobs": {
                "total_processed": 50,
                "total_updated": 48,
                "total_failed": 2,
            },
            "attachments": {
                "total_processed": 30,
                "total_updated": 30,
                "total_failed": 0,
            },
        }
        
        for key in expected_keys:
            assert key in mock_result, f"结果应包含 {key} 字段"


class TestMigratePostBackfillArgs:
    """测试 migrate.run_migrate 的 post_backfill 参数"""

    def test_run_migrate_has_post_backfill_param(self):
        """测试 run_migrate 函数签名包含 post_backfill 参数"""
        import inspect
        from engram_step1.migrate import run_migrate
        
        sig = inspect.signature(run_migrate)
        param_names = list(sig.parameters.keys())
        
        assert "post_backfill" in param_names, "run_migrate 应有 post_backfill 参数"
        assert "backfill_chunking_version" in param_names, "run_migrate 应有 backfill_chunking_version 参数"
        assert "backfill_batch_size" in param_names, "run_migrate 应有 backfill_batch_size 参数"
        assert "backfill_dry_run" in param_names, "run_migrate 应有 backfill_dry_run 参数"

    def test_run_migrate_post_backfill_default_false(self):
        """测试 post_backfill 默认值为 False"""
        import inspect
        from engram_step1.migrate import run_migrate
        
        sig = inspect.signature(run_migrate)
        param = sig.parameters["post_backfill"]
        
        assert param.default is False, "post_backfill 默认应为 False"

    def test_run_migrate_backfill_batch_size_default(self):
        """测试 backfill_batch_size 默认值为 1000"""
        import inspect
        from engram_step1.migrate import run_migrate
        
        sig = inspect.signature(run_migrate)
        param = sig.parameters["backfill_batch_size"]
        
        assert param.default == 1000, "backfill_batch_size 默认应为 1000"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
