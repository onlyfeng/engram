# -*- coding: utf-8 -*-
"""
test_scm_path_spec.py - SCM 路径规范单元测试

测试覆盖:
1. build_scm_artifact_path: 新版路径构建
2. build_legacy_scm_path: 旧版路径构建
3. parse_scm_artifact_path: 路径解析（新旧格式）
4. resolve_scm_artifact_path: 路径解析与回退
5. 扩展名 (diff/diffstat/ministat) 支持

隔离策略:
- 使用 pytest tmp_path fixture 提供临时 artifacts_root
- 不依赖外部资源
"""

import os
import sys
from pathlib import Path

import pytest

# 添加 scripts 目录到 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from artifacts import (
    build_scm_artifact_path,
    build_legacy_scm_path,
    SCM_EXT_DIFF,
    SCM_EXT_DIFFSTAT,
    SCM_EXT_MINISTAT,
)
from engram.logbook.uri import (
    parse_scm_artifact_path,
    resolve_scm_artifact_path,
    ScmArtifactPath,
)


class TestBuildScmArtifactPath:
    """测试 build_scm_artifact_path 函数"""

    def test_build_basic_path(self):
        """测试基本路径构建"""
        path = build_scm_artifact_path(
            project_key="proj_a",
            repo_id="1",
            source_type="svn",
            rev_or_sha="r100",
            sha256="abc123def456",
            ext="diff",
        )
        assert path == "scm/proj_a/1/svn/r100/abc123def456.diff"

    def test_build_git_path(self):
        """测试 Git 路径构建"""
        path = build_scm_artifact_path(
            project_key="proj_b",
            repo_id="2",
            source_type="git",
            rev_or_sha="abc123def",
            sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            ext="diff",
        )
        assert path == "scm/proj_b/2/git/abc123def/e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855.diff"

    def test_build_diffstat_path(self):
        """测试 diffstat 扩展名"""
        path = build_scm_artifact_path(
            project_key="proj_a",
            repo_id="1",
            source_type="svn",
            rev_or_sha="r200",
            sha256="abc123",
            ext="diffstat",
        )
        assert path.endswith(".diffstat")
        assert "abc123.diffstat" in path

    def test_build_ministat_path(self):
        """测试 ministat 扩展名"""
        path = build_scm_artifact_path(
            project_key="proj_a",
            repo_id="1",
            source_type="git",
            rev_or_sha="abcdef1",  # Git SHA 至少 7 位，必须为十六进制
            sha256="abc123",
            ext="ministat",
        )
        assert path.endswith(".ministat")

    def test_source_type_normalized(self):
        """测试 source_type 大小写规范化"""
        path = build_scm_artifact_path(
            project_key="proj_a",
            repo_id="1",
            source_type="SVN",  # 大写
            rev_or_sha="r100",
            sha256="abc123",
            ext="diff",
        )
        assert "/svn/" in path  # 转为小写

    def test_sha256_normalized_lowercase(self):
        """测试 sha256 转为小写"""
        path = build_scm_artifact_path(
            project_key="proj_a",
            repo_id="1",
            source_type="svn",
            rev_or_sha="r100",
            sha256="ABC123DEF",  # 大写
            ext="diff",
        )
        assert "abc123def.diff" in path

    def test_invalid_project_key_raises(self):
        """测试空 project_key 抛出异常"""
        with pytest.raises(ValueError, match="project_key"):
            build_scm_artifact_path(
                project_key="",
                repo_id="1",
                source_type="svn",
                rev_or_sha="r100",
                sha256="abc123",
            )

    def test_invalid_source_type_raises(self):
        """测试无效 source_type 抛出异常"""
        with pytest.raises(ValueError, match="source_type"):
            build_scm_artifact_path(
                project_key="proj_a",
                repo_id="1",
                source_type="hg",  # 无效
                rev_or_sha="r100",
                sha256="abc123",
            )

    def test_invalid_ext_raises(self):
        """测试无效扩展名抛出异常"""
        with pytest.raises(ValueError, match="ext"):
            build_scm_artifact_path(
                project_key="proj_a",
                repo_id="1",
                source_type="svn",
                rev_or_sha="r100",
                sha256="abc123",
                ext="patch",  # 无效
            )

    def test_repo_id_as_int(self):
        """测试 repo_id 为整数"""
        path = build_scm_artifact_path(
            project_key="proj_a",
            repo_id=123,  # 整数
            source_type="svn",
            rev_or_sha="r100",
            sha256="abc123",
        )
        assert "/123/" in path


class TestBuildLegacyScmPath:
    """测试 build_legacy_scm_path 函数"""

    def test_legacy_svn_path(self):
        """测试旧版 SVN 路径格式"""
        path = build_legacy_scm_path(
            repo_id="1",
            source_type="svn",
            rev_or_sha="100",
            ext="diff",
        )
        assert path == "scm/1/svn/r100.diff"

    def test_legacy_git_path(self):
        """测试旧版 Git 路径格式"""
        path = build_legacy_scm_path(
            repo_id="1",
            source_type="git",
            rev_or_sha="abc123def",
            ext="diff",
        )
        assert path == "scm/1/git/commits/abc123def.diff"

    def test_legacy_diffstat_ext(self):
        """测试旧版路径 diffstat 扩展名"""
        path = build_legacy_scm_path(
            repo_id="1",
            source_type="svn",
            rev_or_sha="100",
            ext="diffstat",
        )
        assert path == "scm/1/svn/r100.diffstat"


class TestParseScmArtifactPath:
    """测试 parse_scm_artifact_path 函数"""

    def test_parse_new_format(self):
        """测试解析新版路径格式"""
        result = parse_scm_artifact_path("scm/proj_a/1/svn/r100/abc123.diff")
        
        assert result is not None
        assert result.project_key == "proj_a"
        assert result.repo_id == "1"
        assert result.source_type == "svn"
        assert result.rev_or_sha == "r100"
        assert result.sha256 == "abc123"
        assert result.ext == "diff"
        assert result.is_legacy is False

    def test_parse_new_format_git(self):
        """测试解析新版 Git 路径格式"""
        result = parse_scm_artifact_path("scm/proj_b/2/git/abc123def/e3b0c44.diffstat")
        
        assert result is not None
        assert result.project_key == "proj_b"
        assert result.repo_id == "2"
        assert result.source_type == "git"
        assert result.rev_or_sha == "abc123def"
        assert result.sha256 == "e3b0c44"
        assert result.ext == "diffstat"
        assert result.is_legacy is False

    def test_parse_legacy_svn_format(self):
        """测试解析旧版 SVN 路径格式"""
        result = parse_scm_artifact_path("scm/1/svn/r100.diff")
        
        assert result is not None
        assert result.project_key is None
        assert result.repo_id == "1"
        assert result.source_type == "svn"
        assert result.rev_or_sha == "100"
        assert result.sha256 is None
        assert result.ext == "diff"
        assert result.is_legacy is True

    def test_parse_legacy_git_format(self):
        """测试解析旧版 Git 路径格式"""
        result = parse_scm_artifact_path("scm/1/git/commits/abc123.diff")
        
        assert result is not None
        assert result.project_key is None
        assert result.repo_id == "1"
        assert result.source_type == "git"
        assert result.rev_or_sha == "abc123"
        assert result.sha256 is None
        assert result.ext == "diff"
        assert result.is_legacy is True

    def test_parse_invalid_path_returns_none(self):
        """测试解析无效路径返回 None"""
        assert parse_scm_artifact_path("invalid/path") is None
        assert parse_scm_artifact_path("other/prefix/1/svn/r100.diff") is None
        assert parse_scm_artifact_path("scm") is None
        assert parse_scm_artifact_path("") is None

    def test_parse_normalized_path(self):
        """测试解析带反斜杠的路径"""
        result = parse_scm_artifact_path("scm\\proj_a\\1\\svn\\r100\\abc123.diff")
        
        assert result is not None
        assert result.project_key == "proj_a"


class TestResolveScmArtifactPath:
    """测试 resolve_scm_artifact_path 函数"""

    def test_resolve_new_path_exists(self, tmp_path: Path):
        """测试解析存在的新版路径"""
        # 创建新版路径结构
        new_path = tmp_path / "scm" / "proj_a" / "1" / "svn" / "r100"
        new_path.mkdir(parents=True)
        (new_path / "abc123.diff").write_text("diff content")
        
        result = resolve_scm_artifact_path(
            project_key="proj_a",
            repo_id="1",
            source_type="svn",
            rev_or_sha="r100",
            sha256="abc123",
            ext="diff",
            artifacts_root=tmp_path,
        )
        
        assert result is not None
        assert Path(result).exists()
        assert "abc123.diff" in result

    def test_resolve_fallback_to_legacy_svn(self, tmp_path: Path):
        """测试回退到旧版 SVN 路径"""
        # 只创建旧版路径
        legacy_path = tmp_path / "scm" / "1" / "svn"
        legacy_path.mkdir(parents=True)
        (legacy_path / "r100.diff").write_text("legacy diff content")
        
        result = resolve_scm_artifact_path(
            project_key="proj_a",
            repo_id="1",
            source_type="svn",
            rev_or_sha="100",
            sha256="abc123",
            ext="diff",
            artifacts_root=tmp_path,
        )
        
        assert result is not None
        assert Path(result).exists()
        assert "r100.diff" in result

    def test_resolve_fallback_to_legacy_git(self, tmp_path: Path):
        """测试回退到旧版 Git 路径"""
        # 只创建旧版路径
        legacy_path = tmp_path / "scm" / "1" / "git" / "commits"
        legacy_path.mkdir(parents=True)
        (legacy_path / "abc123.diff").write_text("legacy diff content")
        
        result = resolve_scm_artifact_path(
            project_key="proj_a",
            repo_id="1",
            source_type="git",
            rev_or_sha="abc123",
            sha256="e3b0c44",
            ext="diff",
            artifacts_root=tmp_path,
        )
        
        assert result is not None
        assert Path(result).exists()
        assert "abc123.diff" in result

    def test_resolve_new_path_priority(self, tmp_path: Path):
        """测试新版路径优先于旧版路径"""
        # 同时创建新版和旧版路径
        new_path = tmp_path / "scm" / "proj_a" / "1" / "svn" / "r100"
        new_path.mkdir(parents=True)
        (new_path / "abc123.diff").write_text("new format content")
        
        legacy_path = tmp_path / "scm" / "1" / "svn"
        legacy_path.mkdir(parents=True)
        (legacy_path / "r100.diff").write_text("legacy content")
        
        result = resolve_scm_artifact_path(
            project_key="proj_a",
            repo_id="1",
            source_type="svn",
            rev_or_sha="r100",
            sha256="abc123",
            ext="diff",
            artifacts_root=tmp_path,
        )
        
        assert result is not None
        # 应该返回新版路径
        assert "proj_a" in result
        assert "abc123.diff" in result

    def test_resolve_nonexistent_returns_none(self, tmp_path: Path):
        """测试不存在的路径返回 None"""
        result = resolve_scm_artifact_path(
            project_key="proj_a",
            repo_id="999",
            source_type="svn",
            rev_or_sha="r100",
            sha256="abc123",
            ext="diff",
            artifacts_root=tmp_path,
        )
        
        assert result is None


class TestExtensionConstants:
    """测试扩展名常量"""

    def test_ext_constants_defined(self):
        """测试扩展名常量已定义"""
        assert SCM_EXT_DIFF == "diff"
        assert SCM_EXT_DIFFSTAT == "diffstat"
        assert SCM_EXT_MINISTAT == "ministat"


class TestScmArtifactPathRepr:
    """测试 ScmArtifactPath 的字符串表示"""

    def test_repr_format(self):
        """测试 __repr__ 格式"""
        parsed = parse_scm_artifact_path("scm/proj_a/1/svn/r100/abc123.diff")
        repr_str = repr(parsed)
        
        assert "ScmArtifactPath" in repr_str
        assert "proj_a" in repr_str
        assert "svn" in repr_str


# ---------- 运行测试的入口 ----------

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
